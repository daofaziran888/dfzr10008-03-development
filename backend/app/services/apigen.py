"""API接口生成业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "apigen"
REQUIRED_FIELDS = ["接口编号", "接口名称", "请求方法"]
# 契约相关字段允许在接口配置保存后继续修订，修订本身不覆盖已发布的契约版本
CONTRACT_FIELDS = ["参数定义", "响应模板", "请求路径", "请求方法", "超时秒数"]
STATUS_ORDER = ["草稿", "测试中", "已发布", "已废弃"]
ACTION_RULES = {"发布接口": "草稿", "废弃接口": "已发布", "删除接口": "已废弃"}
NEGATIVE_ACTIONS = []


class ApigenService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("接口编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_code(self, api_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("接口编号") or "") == str(api_code):
                return row
        return None

    def update_definitions(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """修订参数定义/响应模板：只改接口配置草稿区，已发布契约版本保持不动。

        下一次契约发布时按新内容生成新版本，旧版本仍可通过契约清单复现原调用。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"接口配置 {entry_id} 不存在或已归档"
        touched = [field for field in CONTRACT_FIELDS if field in values]
        if not touched:
            return None, "没有可修订的契约字段（参数定义、响应模板等）"
        for field in touched:
            entry[field] = values[field]
        # 配置已被修订，回到草稿态等待重新发布；历史契约版本不受影响
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        return entry, f"接口配置已修订（{'、'.join(touched)}），重新发布契约时将只新增版本"

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"接口配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于API接口生成可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"接口配置已{action}"
