"""请求身份与权限：解析操作人、角色和所属单位，守住写入口。

当前工程没有接入统一登录，身份由请求头传入，便于联调和评测：
- ``X-User`` / ``X-Operator`` / ``X-User-Name``：操作人
- ``X-Role`` / ``X-User-Role``：角色，支持 admin/operator/editor/writer
  以及中文的 管理员/操作员/编辑；readonly/只读/viewer 表示只读账号
- ``X-Unit`` / ``X-Tenant`` / ``X-Tenant-Id``：所属单位

同样的值也允许通过查询参数 ``operator``、``role``、``unit`` 传入，方便 curl。
不传身份时按本单位的值班管理员处理，保持本地开发体验不变。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Query, Request

DEFAULT_UNIT = "数据一科"
DEFAULT_OPERATOR = "值班管理员"

_WRITE_ROLES = {
    "admin",
    "administrator",
    "operator",
    "editor",
    "writer",
    "管理员",
    "操作员",
    "编辑",
    "运营",
}
_READONLY_ROLES = {
    "readonly",
    "read-only",
    "read_only",
    "viewer",
    "guest",
    "read",
    "只读",
    "只读账号",
    "观察者",
    "访客",
}


class PermissionDenied(Exception):
    """写权限、归属校验失败时抛出，由全局异常处理返回 403。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Actor:
    name: str
    role: str  # writer / readonly / unknown
    unit: str

    @property
    def is_readonly(self) -> bool:
        return self.role == "readonly"


def _first(*values: str | None) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def normalize_role(raw: str | None) -> str:
    if raw is None or not raw.strip():
        # 没有显式声明角色时按可写账号处理，兼容原有的本地使用方式。
        return "writer"
    role = raw.strip().lower()
    if role in _READONLY_ROLES:
        return "readonly"
    if role in _WRITE_ROLES:
        return "writer"
    return "unknown"


def current_actor(request: Request) -> Actor:
    """从请求头或查询参数解析当前操作人，不做写权限判断。"""
    headers = request.headers
    query = request.query_params
    name = _first(
        headers.get("x-user"),
        headers.get("x-operator"),
        headers.get("x-user-name"),
        query.get("operator"),
    ) or DEFAULT_OPERATOR
    role_raw = _first(
        headers.get("x-role"),
        headers.get("x-user-role"),
        query.get("role"),
    )
    unit = _first(
        headers.get("x-unit"),
        headers.get("x-tenant"),
        headers.get("x-tenant-id"),
        query.get("unit"),
    ) or DEFAULT_UNIT
    return Actor(name=name, role=normalize_role(role_raw), unit=unit)


def require_writer(actor: Actor = Depends(current_actor)) -> Actor:
    """所有写接口的统一闸门：只读账号和无法识别的角色一律拒绝。"""
    if actor.role == "readonly":
        raise PermissionDenied("只读账号只能查看，禁止修改共享数据连接器、分发任务与数据源标识")
    if actor.role != "writer":
        raise PermissionDenied(f"账号角色「{actor.role}」没有写权限，提交已拒绝")
    return actor


def ensure_same_unit(actor: Actor, owner_unit: str | None, subject: str) -> None:
    """共享数据只允许归属单位操作；跨单位提交直接拒绝。"""
    if owner_unit and owner_unit != actor.unit:
        raise PermissionDenied(
            f"{subject}归属单位「{owner_unit}」，当前账号属于「{actor.unit}」，越权提交已拒绝"
        )


def ensure_values_unit(actor: Actor, values: dict) -> None:
    """请求体里自带所属单位时，不允许把数据写到别的单位。"""
    unit = str(values.get("所属单位") or "").strip()
    if unit and unit != actor.unit:
        raise PermissionDenied(
            f"提交中的所属单位「{unit}」与当前账号单位「{actor.unit}」不一致，已拒绝"
        )
