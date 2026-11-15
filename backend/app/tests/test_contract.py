"""请求响应契约的自动化测试：覆盖重名裁决、结构完整性、错误码标准、
版本化发布、重复拦截、沙箱与文档共用同一冻结契约、旧版本可复现等规则。

运行：cd backend && PYTHONPATH=. .venv/bin/python -m unittest app.tests.test_contract -v
"""
from __future__ import annotations

import copy
import unittest

from app.services.apigen import ApigenService
from app.services.contract import STANDARD_ERROR_CODES, contract_service
from app.services.docgen import DocgenService
from app.services.sandbox import SandboxService
from app.store import store


class ContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = ApigenService()
        self.entry = self.service.find_by_code("APIG-0004")
        self.assertIsNotNone(self.entry)

    def _clone(self) -> dict:
        return copy.deepcopy(self.entry)

    # ------------------------------------------------------------ A 组：重名
    def test_01_rule_handbook_exposes_scope_before_save(self) -> None:
        from app.services.contract import RULE_GROUPS

        self.assertEqual([g["group"] for g in RULE_GROUPS], ["A", "B", "C"])
        for group in RULE_GROUPS:
            self.assertTrue(group["scope"], f"{group['group']} 组必须给出明确判定范围")

    def test_02_duplicate_param_names_resolve_unique_winner(self) -> None:
        report = contract_service.validate(self.entry)
        self.assertTrue(report["passed"])
        names = [p["参数名"] for p in report["冻结参数"]]
        # page 胜 Page（10 > 6），page_size 胜 pageSize（9 > 5）
        self.assertIn("page", names)
        self.assertNotIn("Page", names)
        self.assertIn("page_size", names)
        self.assertNotIn("pageSize", names)
        self.assertEqual(len(names), 4)
        a3 = next(c for g in report["groups"] if g["group"] == "A" for c in g["checks"] if c["id"] == "A3")
        self.assertTrue(a3["passed"])

    def test_03_unresolvable_conflict_blocks_save(self) -> None:
        bad = self._clone()
        for param in bad["参数定义"]:
            if param["参数名"] in ("page", "Page"):
                param["优先级"] = 5
        contract, report = contract_service.publish(bad)
        self.assertIsNone(contract)
        self.assertFalse(report["passed"])
        failed = [c for g in report["groups"] if g["group"] == "A" for c in g["checks"] if not c["passed"]]
        self.assertTrue(failed)
        self.assertEqual(store.rows("contract")[0]["契约编号"], "CTR-0004-V1")  # 没有新版本落库

    def test_04_illegal_location_reports_allowed_scope(self) -> None:
        bad = self._clone()
        bad["参数定义"][0]["参数位置"] = "cookie"
        _, report = contract_service.publish(bad)
        message = "".join(c["message"] for g in report["groups"] for c in g["checks"] if not c["passed"])
        self.assertIn("query", message)
        self.assertIn("header", message)

    # --------------------------------------------------------- B 组：结构完整性
    def test_05_missing_error_body_keeps_draft(self) -> None:
        bad = self._clone()
        del bad["响应模板"]["错误体"]
        contract, report = contract_service.publish(bad)
        self.assertIsNone(contract)
        failed = [c["id"] for g in report["groups"] if g["group"] == "B" for c in g["checks"] if not c["passed"]]
        self.assertIn("B4", failed)

    def test_06_incomplete_success_field_keeps_draft(self) -> None:
        bad = self._clone()
        del bad["响应模板"]["成功体"]["data"]["fields"][0]["说明"]
        contract, report = contract_service.publish(bad)
        self.assertIsNone(contract)
        failed = [c for g in report["groups"] if g["group"] == "B" for c in g["checks"] if not c["passed"]]
        self.assertTrue(any("缺少说明" in c["message"] for c in failed))

    # --------------------------------------------------------- C 组：错误码标准
    def test_07_non_standard_error_code_rejected(self) -> None:
        bad = self._clone()
        bad["响应模板"]["错误体"]["错误码清单"].append(
            {"错误码": "999", "提示信息": "未知", "适用场景": "测试"}
        )
        contract, report = contract_service.publish(bad)
        self.assertIsNone(contract)
        c1 = next(c for g in report["groups"] if g["group"] == "C" for c in g["checks"])
        self.assertFalse(c1["passed"])
        self.assertIn("999", c1["message"])

    def test_08_standard_code_catalog_contains_4xx_5xx(self) -> None:
        for code in ("400", "404", "429", "500", "503"):
            self.assertIn(code, STANDARD_ERROR_CODES)
        self.assertNotIn("200", STANDARD_ERROR_CODES)

    # ------------------------------------------------------------ 版本化发布
    def test_09_publish_creates_new_immutable_version(self) -> None:
        contract, _ = contract_service.publish(self.entry)
        self.assertIsNotNone(contract)
        self.assertEqual(contract["契约编号"], "CTR-0004-V2")
        self.assertEqual(contract["版本号"], "v2")
        self.assertEqual(contract["版本状态"], "当前版本")
        versions = contract_service.list_versions("APIG-0004")
        self.assertEqual([v["版本号"] for v in versions], ["v2", "v1"])
        self.assertEqual(versions[-1]["版本状态"], "历史版本")

    def test_10_duplicate_publish_without_change_blocked(self) -> None:
        contract_service.publish(self.entry)
        again, report = contract_service.publish(self.entry)
        self.assertIsNone(again)
        self.assertFalse(report["passed"])
        dup = next(g for g in report["groups"] if g["group"] == "DUP")
        self.assertFalse(dup["checks"][0]["passed"])

    def test_11_template_revision_only_adds_version(self) -> None:
        first, _ = contract_service.publish(self.entry)
        self.entry["参数定义"][1]["说明"] = "页码，从 1 开始（修订文案）"
        second, _ = contract_service.publish(self.entry)
        self.assertEqual(second["版本号"], "v3")
        self.assertNotEqual(first["内容哈希"], second["内容哈希"])
        versions = contract_service.list_versions("APIG-0004")
        self.assertEqual(len(versions), 3)
        # 旧版本内容保持冻结
        v1 = next(v for v in versions if v["版本号"] == "v1")
        self.assertEqual([p["参数名"] for p in v1["冻结参数"]], ["partner_id", "page", "page_size"])

    def test_12_definitions_update_does_not_touch_published_contracts(self) -> None:
        before = len(store.rows("contract"))
        updated, message = self.service.update_definitions(
            self.entry["id"], {"参数定义": self.entry["参数定义"]}
        )
        self.assertIsNotNone(updated)
        self.assertIn("修订", message)
        self.assertEqual(len(store.rows("contract")), before)
        self.assertEqual(updated["status"], "草稿")

    # ------------------------------------------------------- 沙箱与文档共用
    def test_13_sandbox_builds_request_from_frozen_params(self) -> None:
        contract, _ = contract_service.publish(self.entry)
        spec = contract_service.build_sandbox_request(contract)
        self.assertEqual(spec["method"], "GET")
        self.assertTrue(spec["url"].startswith("/partner/orders/summary?"))
        self.assertIn("page=1", spec["url"])
        self.assertIn("page_size=20", spec["url"])
        self.assertEqual(spec["headers"], {"partner_id": "PT-1001"})
        sandbox = SandboxService().create_from_contract(contract, spec)
        self.assertEqual(sandbox["契约编号"], contract["契约编号"])
        self.assertEqual(sandbox["契约版本"], "v2")

    def test_14_sandbox_error_scenario_uses_contract_error_code(self) -> None:
        contract, _ = contract_service.publish(self.entry)
        spec = contract_service.build_sandbox_request(contract, "429")
        self.assertEqual(spec["expected"]["错误码"], "429")
        self.assertIn("配额", spec["expected"]["适用场景"])

    def test_15_docgen_shares_same_response_fields(self) -> None:
        contract, _ = contract_service.publish(self.entry)
        doc_fields = contract_service.build_doc_fields(contract)
        doc = DocgenService().create_from_contract(contract, doc_fields)
        self.assertEqual(doc["契约编号"], contract["契约编号"])
        self.assertEqual(
            [f["名称"] for f in doc_fields["响应字段表"]],
            ["total_count", "page", "page_size", "orders"],
        )
        self.assertEqual(
            [c["错误码"] for c in doc_fields["错误码表"]],
            ["400", "401", "403", "404", "429", "500"],
        )

    def test_16_old_version_still_reproduces_original_call(self) -> None:
        contract_service.publish(self.entry)
        self.entry["参数定义"][1]["说明"] = "修订"
        contract_service.publish(self.entry)
        v1 = next(v for v in contract_service.list_versions("APIG-0004") if v["版本号"] == "v1")
        spec = contract_service.build_sandbox_request(v1)
        self.assertEqual(spec["url"], "/partner/orders/summary?page=1&page_size=20")
        self.assertEqual(v1["响应模板"]["错误体"]["错误码清单"][0]["错误码"], "400")


if __name__ == "__main__":
    unittest.main(verbosity=2)
