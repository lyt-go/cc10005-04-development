"""数据分发接口：维护分发任务、冻结式数据源快照与投递清单。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import Actor, current_actor, require_writer
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.exporttask import ExporttaskService

router = APIRouter(prefix="/api/exporttask", tags=["数据分发"])

service = ExporttaskService()

LIST_FIELDS = ["任务编号", "数据源标识", "目标系统", "传输协议", "压缩格式", "分片大小MB", "加密开关", "校验方式"]
STATUSES = ["草稿", "任务审批中", "执行中", "待恢复", "已完成", "已取消"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号或数据源快照检索"),
    status: str | None = Query(default=None, description="草稿、任务审批中、执行中、待恢复、已完成、已取消"),
    page: int = 1,
    size: int = 20,
    actor: Actor = Depends(current_actor),
) -> PageResult[dict]:
    """按任务编号与状态过滤数据分发列表；投递清单展示本次任务引用的快照与状态。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    """导出数据分发投递清单：返回全量任务及其快照引用。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "exporttask", "total": total, "items": items}


@router.get("/snapshots", response_model=PageResult[dict])
def list_snapshots(
    task: str | None = Query(default=None, description="按任务编号过滤"),
    actor: Actor = Depends(current_actor),
) -> PageResult[dict]:
    """数据源快照历史：已释放的快照仍保留，便于追溯当时使用的接入版本。"""
    items = service.list_snapshots(task_code=task)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int, actor: Actor = Depends(current_actor)) -> dict:
    """读取单条分发任务明细，附带冻结快照与一致性校验。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分发任务 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/manifest", response_model=dict)
def get_manifest(entry_id: int, actor: Actor = Depends(current_actor)) -> dict:
    """投递清单：核对任务状态、接入版本与数据源快照是否引用同一份记录。"""
    manifest = service.manifest(entry_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"分发任务 {entry_id} 不存在或已归档")
    return manifest


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, actor: Actor = Depends(require_writer)) -> ActionResult:
    """登记一条分发任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing, note = service.create_entry(payload.values, actor=actor)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if entry is None:
        return ActionResult(ok=False, message=note or "分发任务登记失败")
    return ActionResult(ok=True, message=note or "分发任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int, payload: EntryPayload, actor: Actor = Depends(require_writer)
) -> ActionResult:
    """提交审批（冻结快照）、启动/恢复分发、完成或取消分发；非法流转会被拦下。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values, actor=actor)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
