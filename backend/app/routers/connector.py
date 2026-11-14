"""连接器管理接口：维护数据连接器，覆盖连通测试、就绪/离线、接入版本改版与确认。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import Actor, current_actor, require_writer
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.connector import ConnectorService

router = APIRouter(prefix="/api/connector", tags=["连接器管理"])

service = ConnectorService()

LIST_FIELDS = ["连接器编号", "协议类型", "目标端点", "认证参数", "重连次数", "心跳间隔", "数据编码", "超时阈值"]
STATUSES = ["草稿", "连通测试中", "已就绪", "已离线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按连接器编号检索"),
    status: str | None = Query(default=None, description="草稿、连通测试中、已就绪、已离线"),
    page: int = 1,
    size: int = 20,
    actor: Actor = Depends(current_actor),
) -> PageResult[dict]:
    """按连接器编号与状态过滤连接器管理列表；只读账号也可以查看。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int, actor: Actor = Depends(current_actor)) -> dict:
    """读取单条数据连接器明细，附带接入版本列表。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据连接器 {entry_id} 不存在或已归档")
    detail = dict(entry)
    detail["接入版本列表"] = service.list_versions(str(entry.get("连接器编号") or ""))
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, actor: Actor = Depends(require_writer)) -> ActionResult:
    """登记一条数据连接器，缺字段时说明原因而不是静默丢弃。"""
    entry, missing, note = service.create_entry(payload.values, actor=actor)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry is None:
        return ActionResult(ok=False, message=note or "数据连接器登记失败")
    return ActionResult(ok=True, message=note or "数据连接器已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int, payload: EntryPayload, actor: Actor = Depends(require_writer)
) -> ActionResult:
    """执行连通测试、改版、确认版本等动作；越权和不支持的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values, actor=actor)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/versions", response_model=PageResult[dict])
def list_versions(entry_id: int, actor: Actor = Depends(current_actor)) -> PageResult[dict]:
    """列出连接器的全部接入版本，包含已废弃版本，便于追溯。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据连接器 {entry_id} 不存在或已归档")
    versions = service.list_versions(str(entry.get("连接器编号") or ""))
    return PageResult(items=versions, total=len(versions), page=1, size=len(versions) or 1)


@router.get("/export")
def export_entries(actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    """导出连接器管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "connector", "total": total, "items": items}
