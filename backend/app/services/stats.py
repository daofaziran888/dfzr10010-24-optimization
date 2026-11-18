"""表统计信息业务规则：状态流转、字段校验与筛选口径都收在这里。

统计模块消费数据迁移管理提供的同一份源目标一致性校验结果：
* 按批次记录行数与异常段，异常分段与已通过校验的分段同时出现在一个批次里时，
* 与迁移核对清单保持同一个校验版本，不回写历史版本；
* 处理结论回写到迁移核对清单（stats 相关字段单独存放，不覆盖迁移边界）。
"""
from __future__ import annotations

from typing import Any

from app.services import migration_validation as validation
from app.store import store

MODULE = "stats"
REQUIRED_FIELDS = ["报告编号", "表名称", "行总数"]
STATUS_ORDER = ["草稿", "已采集", "已过期", "已清理"]
ACTION_RULES = {"采集统计": "草稿", "标记过期": "已采集", "清理报告": "已过期"}
NEGATIVE_ACTIONS = []


class StatsService:
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
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

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
            return None, f"统计报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表统计信息可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"统计报告已{action}"

    # ------------------------------------------------------------------
    # 迁移一致性校验结果的统计消费路径
    # ------------------------------------------------------------------

    def _resolve_task(self, task_ref: str | int | None) -> dict[str, Any] | None:
        """统计报告按任务编号或 id 关联迁移任务；两边都找不到时不猜。"""
        if task_ref in (None, ""):
            return None
        text = str(task_ref).strip()
        if text.isdigit():
            task = store.find("migrate", int(text))
            if task is not None:
                return task
        for row in store.rows("migrate"):
            if str(row.get("任务编号")) == text:
                return row
        return None

    def _task_ref(self, payload: dict[str, Any], fallback: dict[str, Any] | None = None) -> str:
        return str(
            payload.get("任务编号")
            or payload.get("迁移任务")
            or payload.get("迁移任务编号")
            or (fallback or {}).get("迁移任务编号")
            or ""
        ).strip()

    def get_validation(
        self, entry_id: int, task_ref: str | None = None, version: int | None = None
    ) -> dict[str, Any] | None:
        """读取与迁移模块同一份校验结果，用于统计报告核对当前版本或旧批次。"""
        report = self.get_entry(entry_id)
        if report is None:
            return None
        ref = task_ref or report.get("迁移任务编号")
        task = self._resolve_task(ref)
        if task is None:
            return None
        return validation.get_version(int(task["id"]), version)

    def collect_batch(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """按批次采集统计：行数、异常段都来自同一份校验版本，结论回写核对清单。"""
        report = self.get_entry(entry_id)
        if report is None:
            return None, f"统计报告 {entry_id} 不存在或已归档"
        ref = self._task_ref(values, report)
        task = self._resolve_task(ref)
        if task is None:
            return None, "统计报告未关联有效的迁移任务，无法读取源目标一致性校验结果"

        version_no = values.get("校验版本")
        try:
            requested = int(version_no) if version_no not in (None, "") else None
        except (TypeError, ValueError):
            return None, f"校验版本「{version_no}」不是有效版本号"
        version = validation.get_version(int(task["id"]), requested)
        if version is None:
            if requested is not None:
                return None, f"迁移任务 {task.get('任务编号')} 不存在校验版本 {requested}"
            return None, "迁移任务还没有源目标一致性校验结果，无法按批次采集"
        if requested is not None:
            latest = validation.get_version(int(task["id"]))
            if latest is not None and version["校验版本"] != latest["校验版本"]:
                return None, (
                    f"校验版本 {version['校验版本']} 已被版本 {latest['校验版本']} 取代，"
                    "统计报告需要重新读取当前版本"
                )

        batch = str(values.get("批次号") or version["批次号"]).strip()
        if batch != version["批次号"]:
            return None, f"批次号 {batch} 与校验版本 {version['校验版本']} 的批次 {version['批次号']} 不一致"

        # 一个批次里同时出现异常段和已通过校验的分段时，冲突只保留一个可继续边界，
        # 其它独立分段按校验版本的隔离口径记录，不另造边界。
        conflicts = [
            no
            for no in version["异常段"]
            if any(item > no for item in version["已校验段"])
        ]
        batch_key = (entry_id, int(task["id"]), batch)
        record = store.stats_batches.get(batch_key) or {
            "批次号": batch,
            "校验版本": version["校验版本"],
            "任务编号": version["任务编号"],
            "分段总数": version["分段总数"],
            "分段行数": version["分段行数"],
            "已校验段": version["已校验段"],
            "已迁移段": version["已迁移段"],
            "异常段": version["异常段"],
            "隔离段": version["隔离段"],
            "冲突段": conflicts,
            "行数": {
                "源端": version["源端行数"],
                "目标端": version["目标端行数"],
                "差异": version["行数差异"],
            },
            "已迁移行数": version["已迁移行数"],
            "完整边界": version["完整边界"],
            "下一次续跑位置": version["下一次续跑位置"],
            "迁移完成状态": version["迁移完成状态"],
            "校验结论": version["校验结论"],
            "统计报告": str(report.get("报告编号") or ""),
            "采集时间": version["生成时间"],
        }
        # 同一批次重复采集只刷新结论，不新增批次记录。
        record.update({
            "校验版本": version["校验版本"],
            "已校验段": version["已校验段"],
            "已迁移段": version["已迁移段"],
            "异常段": version["异常段"],
            "隔离段": version["隔离段"],
            "冲突段": conflicts,
            "行数": {
                "源端": version["源端行数"],
                "目标端": version["目标端行数"],
                "差异": version["行数差异"],
            },
            "已迁移行数": version["已迁移行数"],
            "完整边界": version["完整边界"],
            "下一次续跑位置": version["下一次续跑位置"],
            "迁移完成状态": version["迁移完成状态"],
            "校验结论": version["校验结论"],
            "采集时间": version["生成时间"],
        })
        store.stats_batches[batch_key] = record

        report["行总数"] = str(version["目标端行数"])
        report["统计时间"] = version["生成时间"]
        report["迁移任务编号"] = version["任务编号"]
        report["校验版本"] = version["校验版本"]
        report["统计批次"] = batch
        report["异常段"] = version["异常段"]
        report["下次续跑位置"] = version["下一次续跑位置"]

        checklist = store.checklists.get(int(task["id"]))
        if checklist is not None:
            stats_conclusion = (
                f"批次 {batch} 版本 {version['校验版本']}："
                f"{len(version['异常段'])} 个异常段、{len(version['已校验段'])} 个已校验段，"
                f"下一续跑 {version['下一次续跑位置']}"
            )
            checklist["统计报告"] = str(report.get("报告编号") or "")
            checklist["统计版本"] = version["校验版本"]
            checklist["统计批次"] = batch
            checklist["统计结论"] = stats_conclusion
            checklist["统计时间"] = version["生成时间"]
            record["回写结论"] = stats_conclusion
        else:
            record["回写结论"] = "迁移核对清单尚未生成，结论只在统计报告中保留"
        return record, f"统计报告已按批次 {batch} 采集校验版本 {version['校验版本']}"

    def list_batches(self, entry_id: int, task_ref: str | None = None) -> list[dict[str, Any]]:
        task = self._resolve_task(self._task_ref({"任务编号": task_ref or ""}, self.get_entry(entry_id)))
        if task is None:
            return []
        return [
            dict(record)
            for (report_id, task_id, _), record in store.stats_batches.items()
            if report_id == entry_id and task_id == int(task["id"])
        ]
