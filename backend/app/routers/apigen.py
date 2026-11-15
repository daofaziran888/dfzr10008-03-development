"""API接口生成接口：维护接口配置，并提供请求响应契约的校验、发布与版本化能力。

契约相关端点：
  GET  /{entry_id}/contracts/validate   保存前给出全部判定条件与范围
  POST /{entry_id}/contracts/publish    规则不通过则拒绝保存，通过则新增冻结版本
  GET  /{entry_id}/contracts            契约版本清单（旧版本持续可查）
  PUT  /{entry_id}/definitions          修订参数/响应模板，只影响下一次发布
  POST /{entry_id}/contracts/{cid}/sandbox  按冻结参数构造沙箱请求
  POST /{entry_id}/contracts/{cid}/docgen   按同一冻结契约生成文档
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.apigen import ApigenService
from app.services.contract import RULE_GROUPS, contract_service
from app.services.docgen import DocgenService
from app.services.sandbox import SandboxService

router = APIRouter(prefix="/api/apigen", tags=["API接口生成"])

service = ApigenService()
sandbox_service = SandboxService()
docgen_service = DocgenService()

LIST_FIELDS = ["接口编号", "接口名称", "请求方法", "请求路径", "参数定义", "响应模板", "超时秒数", "接口状态"]
STATUSES = ["草稿", "测试中", "已发布", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按接口编号检索"),
    status: str | None = Query(default=None, description="草稿、测试中、已发布、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按接口编号与状态过滤API接口生成列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/contract-rules")
def contract_rules() -> dict[str, Any]:
    """契约发布规则手册：保存前明确每条判定条件的适用范围。"""
    return {"module": "apigen", "rules": RULE_GROUPS}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出API接口生成清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "apigen", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条接口配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"接口配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条接口配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="接口配置已登记", entry=entry)


@router.put("/{entry_id}/definitions", response_model=ActionResult)
def update_definitions(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修订参数定义与响应模板；已发布契约不受影响，重新发布只会新增版本。"""
    entry, message = service.update_definitions(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/contracts/validate")
def validate_contract(entry_id: int) -> dict[str, Any]:
    """保存前校验：返回 A/B/C 三组规则的判定范围、逐条结果与冻结参数预览，不产生任何保存动作。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"接口配置 {entry_id} 不存在或已归档")
    return contract_service.validate(entry)


@router.post("/{entry_id}/contracts/publish", response_model=ActionResult)
def publish_contract(entry_id: int) -> ActionResult:
    """合成为可发布契约：任一判定条件不满足标准时拒绝保存，不产生版本。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"接口配置 {entry_id} 不存在或已归档")
    contract, report = contract_service.publish(entry)
    if contract is None:
        failed = [
            f"[{group['name']}] {check['name']}：{check['message']}"
            for group in report["groups"]
            for check in group["checks"]
            if not check["passed"]
        ]
        return ActionResult(
            ok=False,
            message="契约未通过发布规则，不允许保存：" + "；".join(failed),
            entry={"validation": report},
        )
    entry["status"] = "已发布"
    entry["pending"] = False
    entry["接口状态"] = "已发布"
    return ActionResult(
        ok=True,
        message=f"契约 {contract['契约编号']} 已发布并冻结，沙箱测试与文档生成共用该版本",
        entry={"contract": contract, "validation": report},
    )


@router.get("/{entry_id}/contracts")
def list_contracts(entry_id: int) -> dict[str, Any]:
    """契约版本清单：新版本在前，历史版本继续可查、可复现原调用。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"接口配置 {entry_id} 不存在或已归档")
    versions = contract_service.list_versions(str(entry.get("接口编号")))
    return {
        "接口编号": entry.get("接口编号"),
        "total": len(versions),
        "versions": versions,
    }


@router.post("/{entry_id}/contracts/{contract_id}/sandbox", response_model=ActionResult)
def sandbox_run(entry_id: int, contract_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """沙箱测试：按契约冻结参数构造请求；可传 scenario_code 选择错误场景。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"接口配置 {entry_id} 不存在或已归档")
    contract = contract_service.get_contract(contract_id)
    if contract is None or contract.get("接口编号") != entry.get("接口编号"):
        return ActionResult(ok=False, message=f"契约 {contract_id} 不属于接口 {entry.get('接口编号')}，无法构造沙箱请求")
    scenario = None
    if payload is not None:
        scenario = payload.values.get("scenario_code")
    request_spec = contract_service.build_sandbox_request(contract, scenario)
    sandbox_entry = sandbox_service.create_from_contract(contract, request_spec)
    return ActionResult(
        ok=True,
        message=f"已按契约 {contract['契约编号']} 的冻结参数建立沙箱实例 {sandbox_entry['实例编号']}",
        entry={"sandbox": sandbox_entry, "request": request_spec},
    )


@router.post("/{entry_id}/contracts/{contract_id}/docgen", response_model=ActionResult)
def docgen_run(entry_id: int, contract_id: int) -> ActionResult:
    """文档自动生成：从同一冻结契约提取请求参数表、响应字段表与错误码表。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"接口配置 {entry_id} 不存在或已归档")
    contract = contract_service.get_contract(contract_id)
    if contract is None or contract.get("接口编号") != entry.get("接口编号"):
        return ActionResult(ok=False, message=f"契约 {contract_id} 不属于接口 {entry.get('接口编号')}，无法生成文档")
    doc_fields = contract_service.build_doc_fields(contract)
    doc_entry = docgen_service.create_from_contract(contract, doc_fields)
    return ActionResult(
        ok=True,
        message=f"已基于契约 {contract['契约编号']} 生成文档 {doc_entry['模板编号']}，响应字段与沙箱口径一致",
        entry={"docgen": doc_entry, "fields": doc_fields},
    )


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条接口配置执行发布接口、废弃接口、删除接口；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
