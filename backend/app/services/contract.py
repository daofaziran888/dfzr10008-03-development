"""请求响应契约服务：把接口配置的参数定义与响应模板合成为可发布的契约。

契约发布规则在保存前逐条给出判定范围，共三组：
  A. 参数重名判定（同一请求路径、同一参数位置下的命名冲突，必须能确定唯一优先级）
  B. 响应结构完整性（成功结构、错误结构各自的必备字段齐全）
  C. 错误码适用标准（错误码必须落在 HTTP 标准状态码清单内，并带适用场景）

任何一组不通过都不允许保存契约；发布成功后生成不可变的版本化契约，
沙箱测试与文档自动生成共用同一份冻结契约。修改模板只会新增版本，旧版本继续可查。
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date
from hashlib import sha256
from typing import Any

from app.store import store

MODULE = "contract"

# 标准错误码适用范围：HTTP 状态码 4xx/5xx
STANDARD_ERROR_CODES = {
    "400", "401", "403", "404", "405", "406", "408", "409",
    "410", "412", "413", "414", "415", "416", "417", "422",
    "423", "424", "425", "426", "428", "429", "431", "451",
    "500", "501", "502", "503", "504", "505", "506", "507", "511",
}
# 参数位置适用范围
PARAMETER_LOCATIONS = ["query", "header", "path", "body"]
# 字段类型适用范围
FIELD_TYPES = ["string", "integer", "number", "boolean", "array", "object"]
# 冲突分组时位置的兜底排序（位置优先级：path > query > header > body）
LOCATION_RANK = {"path": 0, "query": 1, "header": 2, "body": 3}

RULE_GROUPS = [
    {
        "group": "A",
        "name": "参数重名判定",
        "scope": "同一请求路径 + 同一参数位置下，参数名按命名规范归一化（驼峰转下划线、小写）后重名即冲突；冲突必须能按「显式优先级 → 必填优先 → 位置优先级 → 声明顺序」确定唯一胜出者",
    },
    {
        "group": "B",
        "name": "响应结构完整性",
        "scope": "成功体与错误体均须存在；成功体须含 code/message/data，data 至少一个字段且每个字段含名称/类型/说明；错误体须含非空错误码清单，每条含错误码/提示信息/适用场景",
    },
    {
        "group": "C",
        "name": "错误码适用标准",
        "scope": "错误码必须是 4xx/5xx 的 HTTP 标准状态码（共 %d 个），适用场景非空，且提示信息非空" % len(STANDARD_ERROR_CODES),
    },
]


def normalize_name(name: str) -> str:
    """命名归一化：驼峰转下划线后小写，例如 pageSize / PageSize -> page_size。"""
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", str(name or "").strip())
    return text.lower()


def _check(check_id: str, name: str, scope: str, passed: bool, message: str) -> dict[str, Any]:
    return {"id": check_id, "name": name, "scope": scope, "passed": passed, "message": message}


class ContractService:
    """契约的校验、发布、版本查询，以及沙箱/文档两个消费方的共用取数口径。"""

    # ------------------------------------------------------------------ 校验
    def validate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """对单条接口配置执行全部发布规则，返回分组判定报告，不落库任何数据。"""
        groups = [dict(item, checks=[]) for item in RULE_GROUPS]
        group_map = {item["group"]: item for item in groups}

        params = entry.get("参数定义")
        if not isinstance(params, list):
            params = []
        template = entry.get("响应模板")
        if not isinstance(template, dict):
            template = {}

        self._validate_params(entry, params, group_map["A"]["checks"])
        self._validate_structure(template, group_map["B"]["checks"])
        self._validate_error_codes(template, group_map["C"]["checks"])

        report = {
            "接口编号": entry.get("接口编号"),
            "请求路径": entry.get("请求路径"),
            "passed": all(check["passed"] for group in groups for check in group["checks"]),
            "groups": groups,
        }
        report["冻结参数"] = self.resolve_params(entry, params)
        return report

    def _validate_params(self, entry: dict[str, Any], params: list[Any], checks: list[dict[str, Any]]) -> None:
        scope = group_scope(RULE_GROUPS, "A")

        if not params:
            checks.append(_check("A0", "参数清单非空", scope, False, "参数定义为空，无法构造请求契约"))
            return
        checks.append(_check("A0", "参数清单非空", scope, True, f"共声明 {len(params)} 个参数"))

        bad_rows: list[str] = []
        for index, raw in enumerate(params, start=1):
            if not isinstance(raw, dict):
                bad_rows.append(f"第{index}行不是结构化参数")
                continue
            name = str(raw.get("参数名") or "").strip()
            location = str(raw.get("参数位置") or "").strip()
            field_type = str(raw.get("类型") or "").strip()
            if not name:
                bad_rows.append(f"第{index}行缺少参数名")
            if location not in PARAMETER_LOCATIONS:
                bad_rows.append(f"参数 {name or '第%d行' % index} 的位置「{location}」不在 {PARAMETER_LOCATIONS} 范围内")
            if field_type not in FIELD_TYPES:
                bad_rows.append(f"参数 {name or '第%d行' % index} 的类型「{field_type}」不在 {FIELD_TYPES} 范围内")
        checks.append(_check(
            "A1", "参数属性完整且取值在适用范围内", scope,
            not bad_rows, "全部参数的名称/位置/类型合法" if not bad_rows else "；".join(bad_rows),
        ))

        # 同一路径 + 同一位置 + 归一化同名 -> 冲突分组
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for index, raw in enumerate(params):
            if not isinstance(raw, dict) or not raw.get("参数名"):
                continue
            location = str(raw.get("参数位置") or "").strip()
            key = (location, normalize_name(str(raw["参数名"])))
            buckets.setdefault(key, []).append({"index": index, "raw": raw})

        conflicts = {key: rows for key, rows in buckets.items() if len(rows) > 1}
        if not conflicts:
            checks.append(_check("A2", "同路径同位置参数重名冲突", scope, True, "未发现归一化后重名的参数"))
            checks.append(_check("A3", "重名参数可确定唯一优先级", scope, True, "无冲突，无需裁决"))
            return

        lines = [
            f"位置 {location} 下「{norm}」存在 {len(rows)} 种写法："
            + "、".join(str(item['raw'].get('参数名')) for item in rows)
            for (location, norm), rows in conflicts.items()
        ]
        checks.append(_check("A2", "同路径同位置参数重名冲突", scope, True, "；".join(lines)))

        unresolved: list[str] = []
        for (location, norm), rows in conflicts.items():
            ordered = sorted(rows, key=lambda item: self._priority_key(item["raw"], item["index"]))
            winner = ordered[0]["raw"]
            # 排序键完全相同意味着连声明顺序都无法区分（声明顺序已在键里，理论上不会相等），
            # 这里再显式比较前三个业务键，判断业务规则本身是否足以唯一裁决。
            business_keys = [self._priority_key(item["raw"], -1)[:3] for item in rows]
            if len({tuple(key) for key in business_keys}) != len(rows):
                unresolved.append(f"位置 {location} 下「{norm}」的显式优先级/必填/位置分不出高低，需人工指定")
                continue
            losers = "、".join(str(item["raw"].get("参数名")) for item in rows if item["raw"] is not winner)
            lines[-1] += f"；胜出：{winner.get('参数名')}（优先级 {self._priority_value(winner)}，舍弃 {losers}）"
        checks[-1]["message"] = "；".join(lines)
        checks.append(_check(
            "A3", "重名参数可确定唯一优先级", scope,
            not unresolved,
            "每个重名分组都按优先级规则确定了唯一胜出参数" if not unresolved else "；".join(unresolved),
        ))

    def _validate_structure(self, template: dict[str, Any], checks: list[dict[str, Any]]) -> None:
        scope = group_scope(RULE_GROUPS, "B")

        success = template.get("成功体")
        if not isinstance(success, dict):
            checks.append(_check("B1", "成功结构存在", scope, False, "响应模板缺少成功体"))
            return
        checks.append(_check("B1", "成功结构存在", scope, True, "成功体已定义"))

        missing = [key for key in ("code", "message", "data") if key not in success]
        checks.append(_check(
            "B2", "成功体含 code/message/data", scope,
            not missing, "成功体三大件齐全" if not missing else f"成功体缺少：{'、'.join(missing)}",
        ))

        data = success.get("data")
        fields = data.get("fields") if isinstance(data, dict) else None
        field_bad: list[str] = []
        if not isinstance(fields, list) or not fields:
            checks.append(_check("B3", "成功 data 至少一个完整字段", scope, False, "data.fields 为空，成功结构不完整"))
        else:
            for index, field in enumerate(fields, start=1):
                if not isinstance(field, dict):
                    field_bad.append(f"第{index}个字段不是结构化定义")
                    continue
                name = str(field.get("名称") or "").strip()
                field_type = str(field.get("类型") or "").strip()
                desc = str(field.get("说明") or "").strip()
                if not name:
                    field_bad.append(f"第{index}个字段缺少名称")
                if field_type not in FIELD_TYPES:
                    field_bad.append(f"字段 {name or index} 类型「{field_type}」不合法")
                if not desc:
                    field_bad.append(f"字段 {name or index} 缺少说明")
            checks.append(_check(
                "B3", "成功 data 至少一个完整字段", scope,
                not field_bad,
                f"data 含 {len(fields)} 个字段，名称/类型/说明齐全" if not field_bad else "；".join(field_bad),
            ))

        error_body = template.get("错误体")
        if not isinstance(error_body, dict):
            checks.append(_check("B4", "错误结构存在且含错误码清单", scope, False, "响应模板缺少错误体"))
            return
        error_codes = error_body.get("错误码清单")
        if not isinstance(error_codes, list) or not error_codes:
            checks.append(_check("B4", "错误结构存在且含错误码清单", scope, False, "错误体的错误码清单为空，错误结构不完整"))
            return
        row_bad: list[str] = []
        for index, row in enumerate(error_codes, start=1):
            if not isinstance(row, dict):
                row_bad.append(f"第{index}条不是结构化错误定义")
                continue
            if not str(row.get("错误码") or "").strip():
                row_bad.append(f"第{index}条缺少错误码")
            if not str(row.get("提示信息") or "").strip():
                row_bad.append(f"错误码 {row.get('错误码') or index} 缺少提示信息")
            if not str(row.get("适用场景") or "").strip():
                row_bad.append(f"错误码 {row.get('错误码') or index} 缺少适用场景")
        checks.append(_check(
            "B4", "错误结构存在且含错误码清单", scope,
            not row_bad,
            f"错误体含 {len(error_codes)} 条错误定义，错误码/提示/场景齐全" if not row_bad else "；".join(row_bad),
        ))

    def _validate_error_codes(self, template: dict[str, Any], checks: list[dict[str, Any]]) -> None:
        scope = group_scope(RULE_GROUPS, "C")
        error_body = template.get("错误体") if isinstance(template, dict) else None
        error_codes = error_body.get("错误码清单") if isinstance(error_body, dict) else None
        if not isinstance(error_codes, list) or not error_codes:
            checks.append(_check(
                "C1", "错误码属于 HTTP 标准 4xx/5xx", scope, False,
                "错误码清单缺失，无法判定错误码适用标准（先通过 B 组检查）",
            ))
            return

        invalid: list[str] = []
        for row in error_codes:
            if not isinstance(row, dict):
                continue
            code = str(row.get("错误码") or "").strip()
            if code not in STANDARD_ERROR_CODES:
                invalid.append(f"错误码 {code or '空'} 不在标准清单内")
        checks.append(_check(
            "C1", "错误码属于 HTTP 标准 4xx/5xx", scope,
            not invalid,
            f"{len(error_codes)} 个错误码均在标准状态码清单内" if not invalid else "；".join(invalid),
        ))

    # --------------------------------------------------------- 优先级与冻结
    @staticmethod
    def _priority_value(raw: dict[str, Any]) -> int:
        try:
            return int(raw.get("优先级", 100))
        except (TypeError, ValueError):
            return 100

    def _priority_key(self, raw: dict[str, Any], index: int) -> tuple[int, int, int, int]:
        # 数值越大优先级越高，排序取负让大值排前；必填优先；位置按 path>query>header>body；最后声明顺序
        return (
            -self._priority_value(raw),
            0 if raw.get("必填") else 1,
            LOCATION_RANK.get(str(raw.get("参数位置") or ""), 99),
            index,
        )

    def resolve_params(self, entry: dict[str, Any], params: list[Any] | None = None) -> list[dict[str, Any]]:
        """按重名裁决规则冻结参数：同路径同位置归一化同名时只保留唯一胜出者。"""
        if params is None:
            params = entry.get("参数定义") if isinstance(entry.get("参数定义"), list) else []
        winners: dict[tuple[str, str], dict[str, Any]] = {}
        for index, raw in enumerate(params):
            if not isinstance(raw, dict) or not raw.get("参数名"):
                continue
            location = str(raw.get("参数位置") or "").strip()
            key = (location, normalize_name(str(raw["参数名"])))
            if key not in winners or self._priority_key(raw, index) < self._priority_key(winners[key], winners[key].get("_idx", index)):
                frozen = copy.deepcopy(raw)
                frozen["归一化参数名"] = key[1]
                frozen["_idx"] = index
                winners[key] = frozen
        resolved = sorted(winners.values(), key=lambda item: item["_idx"])
        for item in resolved:
            item.pop("_idx", None)
        return resolved

    def _content_hash(self, entry: dict[str, Any]) -> str:
        payload = {
            "请求方法": entry.get("请求方法"),
            "请求路径": entry.get("请求路径"),
            "冻结参数": self.resolve_params(entry),
            "响应模板": entry.get("响应模板"),
            "超时秒数": entry.get("超时秒数"),
        }
        return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ 发布
    def publish(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """发布契约：规则不通过则拒绝保存；通过则新增不可变版本，旧版本转历史版本。"""
        report = self.validate(entry)
        if not report["passed"]:
            return None, report

        rows = store.rows(MODULE)
        api_code = str(entry.get("接口编号"))
        same_api = [row for row in rows if row.get("接口编号") == api_code]
        latest_hash = same_api[-1]["内容哈希"] if same_api else None
        content_hash = self._content_hash(entry)
        if latest_hash == content_hash:
            report["passed"] = False
            report["groups"].insert(0, {
                "group": "DUP",
                "name": "重复发布拦截",
                "scope": "与当前版本内容哈希完全一致的发布请求不产生新版本",
                "checks": [_check("DUP1", "契约内容发生变化", "与最近一次发布版本比对", False,
                                  "参数定义与响应模板均未变化，仍指向当前版本，无需重复发布")],
            })
            return None, report

        version_no = len(same_api) + 1
        for row in same_api:
            row["版本状态"] = "历史版本"
        contract = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "契约编号": f"CTR-{api_code.split('-')[-1]}-V{version_no}",
            "接口编号": api_code,
            "接口名称": entry.get("接口名称"),
            "版本号": f"v{version_no}",
            "版本状态": "当前版本",
            "请求方法": entry.get("请求方法"),
            "请求路径": entry.get("请求路径"),
            "超时秒数": entry.get("超时秒数"),
            "冻结参数": report["冻结参数"],
            "响应模板": copy.deepcopy(entry.get("响应模板")),
            "内容哈希": content_hash,
            "发布日期": date.today().isoformat(),
        }
        rows.append(contract)
        return contract, report

    # ------------------------------------------------------------------ 查询
    def list_versions(self, api_code: str) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(MODULE) if row.get("接口编号") == api_code]
        return sorted(rows, key=lambda item: int(str(item.get("版本号", "v0")).lstrip("v")), reverse=True)

    def get_contract(self, contract_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, contract_id)

    def latest(self, api_code: str) -> dict[str, Any] | None:
        versions = self.list_versions(api_code)
        return versions[0] if versions else None

    # ---------------------------------------------------------- 沙箱消费口径
    def build_sandbox_request(self, contract: dict[str, Any], scenario_code: str | None = None) -> dict[str, Any]:
        """按冻结参数构造可复现的请求规格；场景错误码用于选择预期响应。"""
        params = contract.get("冻结参数") or []
        path = str(contract.get("请求路径") or "")
        query: dict[str, str] = {}
        headers: dict[str, str] = {}
        body: dict[str, Any] = {}
        path_values: dict[str, str] = {}
        for param in params:
            name = str(param.get("参数名"))
            sample = self._sample_value(param)
            location = param.get("参数位置")
            if location == "path":
                path_values[name] = str(sample)
            elif location == "query":
                query[name] = str(sample)
            elif location == "header":
                headers[name] = str(sample)
            elif location == "body":
                body[name] = sample
        resolved_path = path
        for name, value in path_values.items():
            resolved_path = resolved_path.replace(f"{{{name}}}", value)
        query_string = "&".join(f"{key}={value}" for key, value in query.items())
        url = resolved_path + (f"?{query_string}" if query_string else "")

        error_codes = (contract.get("响应模板") or {}).get("错误体", {}).get("错误码清单", [])
        if scenario_code and any(str(row.get("错误码")) == str(scenario_code) for row in error_codes):
            expected = next(row for row in error_codes if str(row.get("错误码")) == str(scenario_code))
        else:
            expected = (contract.get("响应模板") or {}).get("成功体", {})
        return {
            "method": contract.get("请求方法"),
            "url": url,
            "headers": headers,
            "body": body,
            "expected": expected,
            "可选错误码": [str(row.get("错误码")) for row in error_codes],
        }

    @staticmethod
    def _sample_value(param: dict[str, Any]) -> Any:
        # 参数自带示例值时优先采用，保证沙箱请求与参数定义一致；没有示例值再按类型兜底
        sample = param.get("示例值")
        if sample not in (None, ""):
            return sample
        field_type = param.get("类型")
        if field_type == "integer":
            return 1
        if field_type == "number":
            return 1.0
        if field_type == "boolean":
            return True
        if field_type == "array":
            return []
        if field_type == "object":
            return {}
        return f"<{param.get('参数名')}>"

    # ---------------------------------------------------------- 文档消费口径
    def build_doc_fields(self, contract: dict[str, Any]) -> dict[str, Any]:
        """从同一份冻结契约提取请求参数表与响应字段表，保证文档与沙箱口径一致。"""
        request_fields = [
            {
                "参数名": item.get("参数名"),
                "参数位置": item.get("参数位置"),
                "必填": item.get("必填"),
                "类型": item.get("类型"),
                "说明": item.get("说明"),
            }
            for item in contract.get("冻结参数") or []
        ]
        success = (contract.get("响应模板") or {}).get("成功体", {})
        response_fields = list((success.get("data") or {}).get("fields") or [])
        error_codes = (contract.get("响应模板") or {}).get("错误体", {}).get("错误码清单", [])
        return {
            "请求参数表": request_fields,
            "响应字段表": response_fields,
            "错误码表": error_codes,
        }


def group_scope(groups: list[dict[str, Any]], group_id: str) -> str:
    for group in groups:
        if group["group"] == group_id:
            return str(group["scope"])
    return ""


contract_service = ContractService()
