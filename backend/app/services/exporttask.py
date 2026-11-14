"""数据分发业务规则：冻结式数据源快照、投递清单与可恢复状态流转。

关键约束：
- 提交分发任务时冻结数据源，快照记录连接器的目标端点、协议类型、数据编码与接入版本；
- 快照一旦生成即不可改写，连接器后续改版只能生成待确认版本，不会影响已启动任务；
- 同一任务重复提交只复用同一份快照，不追加新快照；
- 任务取消时释放版本占用，但快照记录与分片清单保留为历史；
- 已取消任务重新冻结必须换用新的任务号；
- 启动分发前发现连接器离线时，任务停在「待恢复」并说明原因。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.auth import ensure_same_unit, ensure_values_unit
from app.services.connector import ConnectorService
from app.store import store

MODULE = "exporttask"
SNAPSHOT_MODULE = "data_source_snapshot"
CONNECTOR_MODULE = "connector"
REQUIRED_FIELDS = ["任务编号", "数据源标识", "目标系统"]
STATUS_ORDER = ["草稿", "任务审批中", "执行中", "待恢复", "已完成", "已取消"]
ACTIVE_STATUSES = ["草稿", "任务审批中", "执行中", "待恢复"]
ACTION_RULES = {
    "提交任务审批": "任务审批中",
    "启动分发": "执行中",
    "恢复分发": "执行中",
    "完成分发": "已完成",
    "取消分发": "已取消",
}
NEGATIVE_ACTIONS = []
SNAPSHOT_STATUSES = ["已冻结", "已释放", "已归档"]
DEFAULT_FRAGMENT_TOTAL = 8


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _unsent_sequence(total: int, sent: int) -> list[int]:
    if total <= sent:
        return []
    return list(range(sent + 1, total + 1))


class ExporttaskService:
    def __init__(self) -> None:
        self.connectors = ConnectorService()

    # ------------------------------------------------------------------ 查询
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
                if keyword in str(row.get("任务编号", ""))
                or keyword in str(row.get("数据源标识", ""))
                or keyword in str(row.get("数据源快照编号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        detail = self._decorate(row)
        snapshot = self._snapshot_of(row)
        detail["数据源快照"] = snapshot
        if snapshot is not None:
            detail["校验"] = self._checks(row, snapshot)
        return detail

    def get_by_code(self, task_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("任务编号")) == task_code:
                return self.get_entry(int(row.get("id", 0)))
        return None

    def _snapshot_of(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        snapshot_no = _clean(entry.get("数据源快照编号"))
        if not snapshot_no:
            return None
        for snapshot in store.rows(SNAPSHOT_MODULE):
            if _clean(snapshot.get("快照编号")) == snapshot_no:
                return dict(snapshot)
        return None

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        """投递清单视图：补齐快照引用、未发送分片与展示用状态。"""
        item = dict(row)
        item.setdefault("数据源快照编号", "")
        item.setdefault("接入版本", "")
        item.setdefault("连接器编号", "")
        item.setdefault("压缩前数据源标识", "")
        item.setdefault("冻结目标端点", "")
        item.setdefault("冻结协议类型", "")
        item.setdefault("冻结数据编码", "")
        item.setdefault("快照状态", "未冻结")
        item.setdefault("暂停原因", "")
        item.setdefault("未发送分片序号", [])
        item["投递状态"] = item.get("status")
        item["分发任务状态"] = item.get("status")
        return item

    # ------------------------------------------------------------------ 固定字段
    def _resolve_connector(self, values: dict[str, Any], entry: dict[str, Any] | None = None) -> dict[str, Any] | None:
        code = _clean(values.get("连接器编号"))
        if not code and entry is not None:
            code = _clean(entry.get("连接器编号"))
        if not code:
            identifier = _clean(values.get("数据源标识"))
            if not identifier and entry is not None:
                identifier = _clean(entry.get("数据源标识"))
            code = identifier
        connector = self.connectors.get_by_code(code) if code else None
        if connector is None:
            identifier = _clean(values.get("数据源标识"))
            if identifier:
                for row in store.rows(CONNECTOR_MODULE):
                    if identifier in {_clean(row.get("连接器编号")), _clean(row.get("数据源标识"))}:
                        connector = row
                        break
        return connector

    def _version_for(self, connector: dict[str, Any], requested: str) -> tuple[dict[str, Any] | None, str]:
        code = _clean(connector.get("连接器编号"))
        if requested:
            version = self.connectors.find_version(code, requested)
            if version is None:
                return None, f"连接器 {code} 不存在接入版本 {requested}"
            if _clean(version.get("status")) != "已确认":
                return None, (
                    f"接入版本 {requested} 当前为「{version.get('status')}」，"
                    "只能生成新的待确认版本，不能用于新的分发任务"
                )
            return version, ""
        version = self.connectors.find_version(code)
        if version is None:
            return None, f"连接器 {code} 没有已确认的接入版本，无法冻结数据源快照"
        return version, ""

    def _fragment_total(self, raw: Any) -> int:
        try:
            total = int(str(raw).strip() or 0)
        except ValueError:
            total = 0
        return total if total > 0 else DEFAULT_FRAGMENT_TOTAL

    # ------------------------------------------------------------------ 写入
    def create_entry(
        self, values: dict[str, Any], actor=None
    ) -> tuple[dict[str, Any] | None, list[str], str]:
        missing = [field for field in REQUIRED_FIELDS if not _clean(values.get(field))]
        if missing:
            return None, missing, ""
        task_code = _clean(values.get("任务编号"))
        rows = store.rows(MODULE)
        if any(_clean(row.get("任务编号")) == task_code for row in rows):
            return None, [], f"分发任务编号 {task_code} 已存在；重复提交同一任务不会产生新的数据源快照"

        unit = _clean(values.get("所属单位"))
        if actor is not None:
            ensure_values_unit(actor, values)
            unit = unit or actor.unit

        connector = self._resolve_connector(values)
        if connector is None:
            return None, [], f"数据源标识「{values.get('数据源标识')}」未匹配到任何数据连接器，登记已拒绝"
        if actor is not None:
            ensure_same_unit(actor, _clean(connector.get("所属单位")), "数据连接器")
            unit = unit or _clean(connector.get("所属单位"))
        unit = unit or "数据一科"

        source_id = _clean(connector.get("数据源标识")) or _clean(connector.get("连接器编号"))
        requested = _clean(values.get("数据源标识"))
        if requested and requested not in {source_id, _clean(connector.get("连接器编号"))}:
            return None, [], (
                f"数据源标识「{requested}」与数据连接器 {connector.get('连接器编号')} "
                f"的标识「{source_id}」不一致，登记已拒绝"
            )

        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "任务编号": task_code,
            "数据源标识": source_id,
            "连接器编号": _clean(connector.get("连接器编号")),
            "目标系统": _clean(values.get("目标系统")),
            "传输协议": _clean(values.get("传输协议")),
            "压缩格式": _clean(values.get("压缩格式")),
            "分片大小MB": _clean(values.get("分片大小MB")),
            "加密开关": _clean(values.get("加密开关")),
            "校验方式": _clean(values.get("校验方式")),
            "所属单位": unit,
            "数据源快照编号": "",
            "接入版本": "",
            "压缩前数据源标识": "",
            "冻结目标端点": "",
            "冻结协议类型": "",
            "冻结数据编码": "",
            "快照状态": "未冻结",
            "分片总数": self._fragment_total(values.get("分片总数")),
            "已发送分片": 0,
            "未发送分片序号": _unsent_sequence(self._fragment_total(values.get("分片总数")), 0),
        }
        rows.append(entry)
        return entry, [], ""

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None, actor=None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分发任务 {entry_id} 不存在或已归档"
        if actor is not None:
            ensure_same_unit(actor, _clean(entry.get("所属单位")), "分发任务")
            ensure_values_unit(actor, values)

        status = _clean(entry.get("status"))
        if action == "提交任务审批":
            return self._submit(entry, values)
        if action == "重新冻结":
            return None, (
                "重新冻结必须产生新的任务号：已冻结/已取消的任务不能原地重冻，"
                "请登记新的分发任务后再提交审批"
            )
        if action == "启动分发":
            return self._start(entry)
        if action == "恢复分发":
            return self._resume(entry)
        if action == "完成分发":
            return self._complete(entry)
        if action == "取消分发":
            return self._cancel(entry, values)
        if action == "改写数据源标识":
            return self._rewrite_identifier(entry, values)

        if action in ACTION_RULES:
            target = ACTION_RULES[action]
            if status in {"已完成", "已取消"}:
                return None, f"分发任务 {entry.get('任务编号')} 当前为「{status}」，不能再执行「{action}」"
            entry["status"] = target
            entry["pending"] = target not in {"已完成", "已取消"}
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"分发任务已{action}"
        return None, f"动作「{action}」不属于数据分发可执行范围"

    # ------------------------------------------------------------------ 提交与冻结
    def _submit(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        status = _clean(entry.get("status"))
        if status == "已取消":
            return None, (
                f"分发任务 {entry.get('任务编号')} 已取消，版本占用已释放；"
                "重新冻结必须使用新的任务号登记分发任务"
            )
        if status in {"执行中", "待恢复", "已完成"}:
            return None, f"分发任务 {entry.get('任务编号')} 已开始分发，不能重复提交审批"

        snapshot = self._snapshot_of(entry)
        if snapshot is not None and _clean(snapshot.get("status")) == "已释放":
            return None, (
                f"任务 {entry.get('任务编号')} 的数据源快照已释放，"
                "重新冻结必须产生新的任务号"
            )
        if snapshot is not None:
            # 重复提交同一任务：只复用同一份快照，不追加记录。
            entry["status"] = "任务审批中"
            entry["pending"] = True
            entry["abnormal"] = False
            return entry, (
                f"重复提交已复用数据源快照 {snapshot.get('快照编号')}（接入版本 {snapshot.get('接入版本')}），"
                "未产生新快照"
            )

        connector = self._resolve_connector(values, entry)
        if connector is None:
            return None, "数据源标识未匹配到数据连接器，无法冻结数据源快照"
        version, error = self._version_for(connector, _clean(values.get("接入版本")))
        if error:
            return None, error

        source_id = _clean(connector.get("数据源标识")) or _clean(connector.get("连接器编号"))
        task_identifier = _clean(entry.get("数据源标识"))
        if task_identifier and task_identifier not in {source_id, _clean(connector.get("连接器编号"))}:
            return None, (
                f"分发任务的数据源标识「{task_identifier}」与连接器 "
                f"{connector.get('连接器编号')} 的标识「{source_id}」不一致，冻结已拒绝"
            )

        snapshot_no = self._next_snapshot_no(_clean(entry.get("任务编号")))
        snapshot = {
            "id": max((int(row.get("id", 0)) for row in store.rows(SNAPSHOT_MODULE)), default=0) + 1,
            "status": "已冻结",
            "pending": False,
            "abnormal": False,
            "快照编号": snapshot_no,
            "任务编号": _clean(entry.get("任务编号")),
            "连接器编号": _clean(connector.get("连接器编号")),
            "接入版本": _clean(version.get("接入版本")),
            "版本状态": _clean(version.get("status")),
            "压缩前数据源标识": source_id,
            "协议类型": _clean(version.get("协议类型")),
            "目标端点": _clean(version.get("目标端点")),
            "数据编码": _clean(version.get("数据编码")),
            "认证参数": _clean(version.get("认证参数")),
            "所属单位": _clean(entry.get("所属单位")) or _clean(connector.get("所属单位")),
            "冻结时间": _now(),
            "释放时间": "",
            "释放原因": "",
            "占用状态": "占用中",
        }
        store.rows(SNAPSHOT_MODULE).append(snapshot)

        occupied = version.setdefault("占用任务号", [])
        task_code = _clean(entry.get("任务编号"))
        if task_code not in occupied:
            occupied.append(task_code)

        entry["数据源标识"] = source_id
        entry["连接器编号"] = snapshot["连接器编号"]
        entry["数据源快照编号"] = snapshot_no
        entry["接入版本"] = snapshot["接入版本"]
        entry["压缩前数据源标识"] = source_id
        entry["冻结目标端点"] = snapshot["目标端点"]
        entry["冻结协议类型"] = snapshot["协议类型"]
        entry["冻结数据编码"] = snapshot["数据编码"]
        entry["快照状态"] = "已冻结"
        if not _clean(entry.get("冻结时间")):
            entry["冻结时间"] = snapshot["冻结时间"]
        if not int(entry.get("分片总数") or 0):
            total = self._fragment_total(values.get("分片总数"))
            entry["分片总数"] = total
            entry["已发送分片"] = 0
            entry["未发送分片序号"] = _unsent_sequence(total, 0)
        entry["status"] = "任务审批中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["暂停原因"] = ""
        pending_version = any(
            _clean(item.get("status")) == "待确认"
            for item in self.connectors.list_versions(_clean(connector.get("连接器编号")))
        )
        note = "；连接器存在待确认改版，本任务仍固定在该已确认版本" if pending_version else ""
        return entry, (
            f"已冻结数据源快照 {snapshot_no}（连接器 {snapshot['连接器编号']}，"
            f"接入版本 {snapshot['接入版本']}，协议 {snapshot['协议类型']}，编码 {snapshot['数据编码']}）{note}"
        )

    def _next_snapshot_no(self, task_code: str) -> str:
        seq = sum(1 for row in store.rows(SNAPSHOT_MODULE) if _clean(row.get("任务编号")) == task_code) + 1
        return f"DSNAP-{task_code}-{seq:02d}"

    # ------------------------------------------------------------------ 状态流转
    def _connector_live(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        code = _clean(entry.get("连接器编号"))
        return self.connectors.get_by_code(code) if code else None

    def _start(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        status = _clean(entry.get("status"))
        if status == "已取消":
            return None, f"分发任务 {entry.get('任务编号')} 已取消，重新分发必须使用新的任务号"
        if status == "已完成":
            return None, f"分发任务 {entry.get('任务编号')} 已完成，不能再次启动"
        if status not in {"任务审批中", "待恢复"}:
            return None, f"分发任务 {entry.get('任务编号')} 当前为「{status}」，请先提交任务审批并冻结数据源快照"
        if self._snapshot_of(entry) is None:
            return None, "分发任务尚未冻结数据源快照，请先提交任务审批"

        connector = self._connector_live(entry)
        reason = ""
        if connector is None:
            reason = f"连接器 {entry.get('连接器编号')} 已不存在，无法在分发前完成连通检查"
        elif _clean(connector.get("status")) == "已离线":
            reason = (
                f"连接器 {entry.get('连接器编号')} 在实际分发前已离线，"
                f"分发停在第 {int(entry.get('已发送分片') or 0)} 片之后的可恢复位置"
            )
        if reason:
            unsent = entry.get("未发送分片序号")
            if not unsent:
                total = int(entry.get("分片总数") or 0)
                sent = int(entry.get("已发送分片") or 0)
                entry["未发送分片序号"] = _unsent_sequence(total, sent)
            entry["status"] = "待恢复"
            entry["pending"] = True
            entry["abnormal"] = True
            entry["暂停原因"] = reason
            entry["暂停位置"] = "分发前连通检查"
            entry["暂停时间"] = _now()
            entry["可恢复"] = True
            return entry, f"{reason}；未发送分片 {entry.get('未发送分片序号')} 已保留，连接器恢复后可继续"

        entry["status"] = "执行中"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["暂停原因"] = ""
        entry["可恢复"] = False
        entry["启动时间"] = _now()
        return entry, (
            f"分发任务已启动，读取冻结快照 {entry.get('数据源快照编号')}，"
            "连接器端点后续变化不会影响本次任务"
        )

    def _resume(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if _clean(entry.get("status")) != "待恢复":
            return None, f"分发任务 {entry.get('任务编号')} 当前不在待恢复状态"
        connector = self._connector_live(entry)
        if connector is None or _clean(connector.get("status")) == "已离线":
            entry["暂停原因"] = f"连接器 {entry.get('连接器编号')} 仍处于离线状态，暂不能恢复分发"
            entry["pending"] = True
            entry["abnormal"] = True
            return None, (
                f"{entry['暂停原因']}；任务停在可恢复位置，"
                f"未发送分片 {entry.get('未发送分片序号')} 继续保留"
            )
        return self._start(entry)

    def _complete(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if _clean(entry.get("status")) not in {"执行中", "任务审批中"}:
            return None, f"分发任务 {entry.get('任务编号')} 当前为「{entry.get('status')}」，不能完成分发"
        total = int(entry.get("分片总数") or 0)
        entry["已发送分片"] = total
        entry["未发送分片序号"] = []
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["完成时间"] = _now()
        self._release_version(entry)
        snapshot = self._snapshot_of(entry)
        if snapshot is not None:
            entry["快照状态"] = "已归档（历史保留）"
            self._update_snapshot(snapshot["快照编号"], status="已归档", reason="分发完成")
        return entry, f"分发任务已完成，快照 {entry.get('数据源快照编号')} 作为历史保留"

    def _cancel(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        status = _clean(entry.get("status"))
        if status == "已取消":
            return None, f"分发任务 {entry.get('任务编号')} 已取消，重复取消不会产生新的数据源快照"
        if status == "已完成":
            return None, f"分发任务 {entry.get('任务编号')} 已完成，不能取消"
        if not entry.get("未发送分片序号"):
            total = int(entry.get("分片总数") or 0)
            sent = int(entry.get("已发送分片") or 0)
            if total:
                entry["未发送分片序号"] = _unsent_sequence(total, sent)
        entry["取消前状态"] = status
        entry["status"] = "已取消"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["取消时间"] = _now()
        entry["取消原因"] = _clean(values.get("reason")) or "操作人取消分发"
        self._release_version(entry)
        snapshot = self._snapshot_of(entry)
        if snapshot is not None:
            self._update_snapshot(snapshot["快照编号"], status="已释放", reason="分发任务取消")
            entry["快照状态"] = "已释放（历史保留）"
        return entry, (
            f"分发任务已取消，接入版本占用已释放；分片清单保留未发送片序号 "
            f"{entry.get('未发送分片序号')}，快照 {entry.get('数据源快照编号') or '—'} 作为历史保留。"
            "重新冻结必须使用新的任务号"
        )

    def _rewrite_identifier(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if _clean(entry.get("status")) != "草稿" or self._snapshot_of(entry) is not None:
            return None, "已冻结或已开始的分发任务不能改写数据源标识；如需换源请登记新的分发任务"
        requested = _clean(values.get("数据源标识"))
        if not requested:
            return None, "改写数据源标识需要提供新的数据源标识"
        probe = {"数据源标识": requested}
        connector = self._resolve_connector(probe)
        if connector is None:
            return None, f"数据源标识「{requested}」未匹配到任何数据连接器"
        source_id = _clean(connector.get("数据源标识")) or _clean(connector.get("连接器编号"))
        entry["数据源标识"] = source_id
        entry["连接器编号"] = _clean(connector.get("连接器编号"))
        return entry, f"数据源标识已改写为 {source_id}；提交审批时将按新连接器冻结快照"

    # ------------------------------------------------------------------ 快照历史
    def _update_snapshot(self, snapshot_no: str, *, status: str, reason: str) -> None:
        for row in store.rows(SNAPSHOT_MODULE):
            if _clean(row.get("快照编号")) == snapshot_no:
                row["status"] = status
                row["占用状态"] = "已释放" if status in {"已释放", "已归档"} else "占用中"
                row["释放时间"] = _now()
                row["释放原因"] = reason
                return

    def _release_version(self, entry: dict[str, Any]) -> None:
        """释放接入版本上的任务占用，但不动版本记录与快照字段。"""
        code = _clean(entry.get("连接器编号"))
        label = _clean(entry.get("接入版本"))
        task_code = _clean(entry.get("任务编号"))
        if not code or not label:
            return
        for version in store.rows("connector_version"):
            if _clean(version.get("连接器编号")) == code and _clean(version.get("接入版本")) == label:
                occupied = version.get("占用任务号")
                if isinstance(occupied, list) and task_code in occupied:
                    occupied.remove(task_code)
                return

    # ------------------------------------------------------------------ 投递清单与校验
    def list_snapshots(self, task_code: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(SNAPSHOT_MODULE)]
        if task_code:
            rows = [row for row in rows if _clean(row.get("任务编号")) == task_code]
        return rows

    def manifest(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        snapshot = entry.get("数据源快照") or None
        connector = self.connectors.get_by_code(_clean(entry.get("连接器编号")))
        version = None
        if connector is not None and entry.get("接入版本"):
            version = self.connectors.find_version(_clean(connector.get("连接器编号")), _clean(entry.get("接入版本")))
        checks = self._checks(entry, snapshot) if snapshot else {
            "快照引用一致": False,
            "接入版本一致": False,
            "数据源标识一致": False,
            "结论": "任务尚未冻结数据源快照",
        }
        return {
            "任务编号": entry.get("任务编号"),
            "分发任务状态": entry.get("status"),
            "投递状态": entry.get("投递状态"),
            "数据源快照编号": entry.get("数据源快照编号"),
            "连接器编号": entry.get("连接器编号"),
            "接入版本": entry.get("接入版本"),
            "压缩前数据源标识": entry.get("压缩前数据源标识"),
            "未发送分片序号": entry.get("未发送分片序号"),
            "暂停原因": entry.get("暂停原因", ""),
            "task": entry,
            "snapshot": snapshot,
            "version": version,
            "connector": ({
                "连接器编号": connector.get("连接器编号"),
                "接入版本": connector.get("接入版本"),
                "版本状态": connector.get("版本状态"),
                "目标端点": connector.get("目标端点"),
                "协议类型": connector.get("协议类型"),
                "数据编码": connector.get("数据编码"),
                "status": connector.get("status"),
            } if connector else None),
            "checks": checks,
        }

    def _checks(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        reference_ok = _clean(entry.get("数据源快照编号")) == _clean(snapshot.get("快照编号")) and \
            _clean(snapshot.get("任务编号")) == _clean(entry.get("任务编号"))
        version_ok = _clean(entry.get("接入版本")) == _clean(snapshot.get("接入版本"))
        source_ok = _clean(entry.get("压缩前数据源标识") or entry.get("数据源标识")) == \
            _clean(snapshot.get("压缩前数据源标识"))
        endpoint_ok = _clean(entry.get("冻结目标端点")) == _clean(snapshot.get("目标端点"))
        encoding_ok = _clean(entry.get("冻结数据编码")) == _clean(snapshot.get("数据编码"))
        protocol_ok = _clean(entry.get("冻结协议类型")) == _clean(snapshot.get("协议类型"))
        connector = self.connectors.get_by_code(_clean(snapshot.get("连接器编号")))
        endpoint_changed = bool(
            connector is not None and _clean(connector.get("目标端点")) != _clean(snapshot.get("目标端点"))
        )
        consistent = reference_ok and version_ok and source_ok and endpoint_ok and encoding_ok and protocol_ok
        return {
            "快照引用一致": reference_ok,
            "接入版本一致": version_ok,
            "数据源标识一致": source_ok,
            "冻结端点未被改写": endpoint_ok,
            "冻结协议一致": protocol_ok,
            "冻结编码一致": encoding_ok,
            "连接器端点是否已变化": endpoint_changed,
            "任务状态": entry.get("status"),
            "快照状态": snapshot.get("status"),
            "版本占用任务号": next(
                (list(row.get("占用任务号") or []) for row in store.rows("connector_version")
                 if _clean(row.get("连接器编号")) == _clean(snapshot.get("连接器编号"))
                 and _clean(row.get("接入版本")) == _clean(snapshot.get("接入版本"))),
                [],
            ),
            "结论": "一致：投递清单、接入版本与分发任务状态引用同一份数据源快照" if consistent
            else "不一致：请检查任务与快照引用",
        }
