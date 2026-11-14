"""连接器管理业务规则：状态流转、接入版本、字段校验与筛选口径都收在这里。

接入版本采用“追加式改版”：
- 每次改版只追加一条 ``待确认`` 的新版本，已确认版本上的参数不会被就地改写；
- 确认新版本后，旧版本标记为 ``已废弃``，但版本记录和字段快照仍然保留；
- 分发任务冻结的是版本记录里的字段，因此连接器之后再改版也不会影响已启动任务。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.auth import PermissionDenied, ensure_same_unit, ensure_values_unit
from app.store import store

MODULE = "connector"
VERSION_MODULE = "connector_version"
REQUIRED_FIELDS = ["连接器编号", "协议类型", "目标端点"]
STATUS_ORDER = ["草稿", "连通测试中", "已就绪", "已离线"]
ACTION_RULES = {"发起连通测试": "连通测试中", "标记就绪": "已就绪", "标记离线": "已离线"}
NEGATIVE_ACTIONS = []
VERSION_STATUSES = ["已确认", "待确认", "已废弃"]
VERSION_FIELDS = ["连接器编号", "协议类型", "目标端点", "数据编码", "认证参数", "重连次数", "心跳间隔", "超时阈值"]
CURRENT_FIELDS = VERSION_FIELDS[1:]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _clean(value: Any) -> str:
    return str(value or "").strip()


class ConnectorService:
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
            rows = [
                row for row in rows
                if keyword in str(row.get("连接器编号", ""))
                or keyword in str(row.get("目标端点", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_by_code(self, connector_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("连接器编号")) == connector_code:
                return row
        return None

    # ------------------------------------------------------------------ 版本
    def list_versions(self, connector_code: str) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(VERSION_MODULE)
                if str(row.get("连接器编号")) == connector_code]
        rows.sort(key=lambda row: int(row.get("版本序号", 0)))
        return rows

    def find_version(self, connector_code: str, version: str | None = None) -> dict[str, Any] | None:
        """按版本号查找；不指定版本号时返回序号最大的已确认版本。"""
        versions = self.list_versions(connector_code)
        if version:
            for row in versions:
                if str(row.get("接入版本")) == version:
                    return row
            return None
        confirmed = [row for row in versions if row.get("status") == "已确认"]
        if not confirmed:
            return None
        return max(confirmed, key=lambda row: int(row.get("版本序号", 0)))

    def latest_confirmed_label(self, connector_code: str) -> str:
        version = self.find_version(connector_code)
        return str(version.get("接入版本")) if version else ""

    def _next_version_label(self, connector_code: str) -> tuple[int, str]:
        rows = self.list_versions(connector_code)
        next_no = max((int(row.get("版本序号", 0)) for row in rows), default=0) + 1
        return next_no, f"v{next_no}"

    def _add_version(self, connector_code: str, values: dict[str, Any], *, status: str,
                     unit: str, created: str | None = None) -> dict[str, Any]:
        version_no, label = self._next_version_label(connector_code)
        rows = store.rows(VERSION_MODULE)
        version = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": status,
            "pending": status == "待确认",
            "abnormal": False,
            "连接器编号": connector_code,
            "接入版本": label,
            "版本序号": version_no,
            "占用任务号": [],
            "所属单位": unit,
            "创建时间": created or _now(),
            "确认时间": created or _now() if status == "已确认" else "",
        }
        for field in CURRENT_FIELDS:
            version[field] = _clean(values.get(field))
        rows.append(version)
        return version

    def _version_payload(self, connector: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for field in CURRENT_FIELDS:
            payload[field] = _clean(values.get(field)) or _clean(connector.get(field))
        return payload

    # ------------------------------------------------------------------ 写入
    def create_entry(
        self, values: dict[str, Any], actor=None
    ) -> tuple[dict[str, Any] | None, list[str], str]:
        """登记连接器。同一连接器编号重复提交时返回原记录，不再追加版本。"""
        missing = [field for field in REQUIRED_FIELDS if not _clean(values.get(field))]
        if missing:
            return None, missing, ""
        code = _clean(values.get("连接器编号"))
        existing = self.get_by_code(code)
        if existing is not None:
            return existing, [], "连接器编号已存在，未重复登记"

        unit = _clean(values.get("所属单位"))
        if actor is not None:
            ensure_values_unit(actor, values)
            unit = unit or actor.unit
        unit = unit or "数据一科"

        rows = store.rows(MODULE)
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "连接器编号": code,
            "接入版本": "v1",
            "版本状态": "已确认",
            "所属单位": unit,
            "数据源标识": code,
        }
        for field in CURRENT_FIELDS:
            entry[field] = _clean(values.get(field))
        rows.append(entry)
        self._add_version(code, values, status="已确认", unit=unit)
        return entry, [], ""

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None, actor=None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据连接器 {entry_id} 不存在或已归档"
        if actor is not None:
            ensure_same_unit(actor, _clean(entry.get("所属单位")), "数据连接器")

        if action in ACTION_RULES:
            target = ACTION_RULES[action]
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"数据连接器已{action}"

        code = _clean(entry.get("连接器编号"))
        if action == "改版":
            return self._revise(entry, code, values)
        if action == "确认版本":
            return self._confirm_version(entry, code, values)
        return None, f"动作「{action}」不属于连接器管理可执行范围"

    def _revise(self, entry: dict[str, Any], code: str, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """追加一个待确认版本；绝不改动已确认版本，也不触碰分发任务快照。"""
        payload = self._version_payload(entry, values)
        changed = any(payload[field] != _clean(entry.get(field)) for field in CURRENT_FIELDS)
        if not changed:
            return None, "接入参数没有变化，未生成新的接入版本"

        pending = [version for version in self.list_versions(code) if version.get("status") == "待确认"]
        if pending:
            return None, f"连接器已有待确认版本 {pending[-1].get('接入版本')}，请先确认或放弃后再改版"

        version = self._add_version(code, payload, status="待确认", unit=_clean(entry.get("所属单位")))
        for field in CURRENT_FIELDS:
            entry[field] = payload[field]
        entry["接入版本"] = version["接入版本"]
        entry["版本状态"] = "待确认"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        return entry, (
            f"已生成待确认接入版本 {version['接入版本']}，"
            "已启动分发任务仍读取各自冻结的数据源快照"
        )

    def _confirm_version(self, entry: dict[str, Any], code: str, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """确认待确认版本：直接改仓库里的版本记录，旧版本保留为已废弃。"""
        label = _clean(values.get("接入版本"))
        version_rows = [
            row for row in store.rows(VERSION_MODULE)
            if _clean(row.get("连接器编号")) == code
        ]
        version_rows.sort(key=lambda row: int(row.get("版本序号", 0)))
        candidates = [
            row for row in version_rows
            if _clean(row.get("status")) == "待确认" and (not label or _clean(row.get("接入版本")) == label)
        ]
        if not candidates:
            return None, "连接器没有待确认的接入版本，无需确认"
        target = candidates[-1]

        for version in version_rows:
            if _clean(version.get("status")) == "已确认" and version is not target:
                version["status"] = "已废弃"
                version["pending"] = False
        target["status"] = "已确认"
        target["pending"] = False
        target["确认时间"] = _now()

        for field in CURRENT_FIELDS:
            entry[field] = target.get(field)
        entry["接入版本"] = target.get("接入版本")
        entry["版本状态"] = "已确认"
        entry["pending"] = entry.get("status") != STATUS_ORDER[-1]
        entry["abnormal"] = False
        return entry, f"接入版本 {target.get('接入版本')} 已确认，可用于新的分发任务"

    # ------------------------------------------------------------------ 兼容旧口径
    def list_statuses(self) -> list[str]:
        return list(STATUS_ORDER)
