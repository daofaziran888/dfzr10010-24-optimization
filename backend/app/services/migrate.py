"""数据迁移管理业务规则：状态流转、字段校验与筛选口径都收在这里。

分段重跑的关键约束收在 services/migration_validation.py：
* 执行迁移只从「下一次续跑位置」续跑，已校验段不从头扫描；
* 有异常段时迁移完成状态不能越过异常段，归档会被拒绝；
* 迁移详情、校验结果、核对清单读的是同一份源目标一致性校验版本。
"""
from __future__ import annotations

from typing import Any

from app.services import migration_validation as validation
from app.store import store

MODULE = "migrate"
REQUIRED_FIELDS = ["任务编号", "源表名", "目标表名"]
STATUS_ORDER = ["待执行", "迁移中", "已完成", "已回滚"]
ACTION_RULES = {"执行迁移": "待执行", "回滚迁移": "迁移中", "归档任务": "已完成"}
NEGATIVE_ACTIONS = ["回滚迁移"]


class MigrateService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            validation.ensure_validation(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            validation.ensure_validation(entry)
        return entry

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
        for field in ("迁移方式", "数据范围", "校验规则", "迁移耗时", "批次号", "依赖源库", "依赖目标环境", "快照编号"):
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        if isinstance(values.get("分段明细"), list) and values["分段明细"]:
            entry["分段明细"] = values["分段明细"]
            try:
                entry["分段总数"] = int(values.get("分段总数") or len(values["分段明细"]))
            except (TypeError, ValueError):
                return None, ["分段总数"]
        if values.get("分段行数") not in (None, ""):
            try:
                entry["分段行数"] = int(values.get("分段行数") or 0)
            except (TypeError, ValueError):
                return None, ["分段行数"]
        entry["异常段处理"] = str(values.get("异常段处理") or validation.DEFAULT_STRATEGY)
        rows.append(entry)
        validation.ensure_validation(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"迁移任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据迁移管理可执行范围"

        latest = validation.get_version(entry_id)
        if action == "执行迁移":
            # 分段任务从续跑位置继续，且必须先通过上线前依赖/快照检查。
            if latest is not None:
                preflight = validation.preflight(entry_id)
                if preflight is None or not preflight.get("可上线"):
                    detail = "；".join((preflight or {}).get("结构漂移") or []) or "上线前检查未通过"
                    return None, f"迁移任务暂不能继续：{detail}"
                entry, message = validation.mark_resumed(entry_id)
                return entry, message
            entry["status"] = "迁移中"
            entry["pending"] = True
            entry["abnormal"] = False
            return entry, "迁移任务已执行迁移（该任务没有分段明细，按整表一次迁移）"

        if action == "归档任务":
            # 归档等于宣告完成：任何异常段、待校验段或未达完整边界都不允许越过。
            if latest is not None and latest["迁移完成状态"] != "已完成":
                return None, (
                    f"迁移任务存在异常段 {latest['异常段'] or '无'}，"
                    f"完整边界第 {latest['完整边界']} 段，完成状态不能越过异常段；"
                    f"请先复核后从 {latest['下一次续跑位置']} 续跑"
                )
            if latest is not None and latest["完整边界"] < latest["分段总数"]:
                return None, (
                    f"迁移任务只到达第 {latest['完整边界']} 段（共 {latest['分段总数']} 段），"
                    f"完成状态不能越过未校验段"
                )
            entry["status"] = "已完成"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["迁移完成状态"] = "已完成"
            return entry, "迁移任务已归档任务"

        entry["status"] = "已回滚"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["迁移完成状态"] = "已回滚"
        return entry, "迁移任务已回滚迁移，可按快照回退并保留已校验段边界"
