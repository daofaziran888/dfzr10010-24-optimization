"""源目标一致性校验：分段校验结果、续跑边界与迁移核对清单的唯一口径。

几个必须守住的规则：

* 大表分段重跑只从「下一次续跑位置」开始，已经校验过的段不再从头扫描；
* 异常段不会清空其它已校验段，已校验段始终能形成一条明确的边界；
* 迁移完成状态不允许越过任何失败段，只要还有异常段就保持「未完成」；
* 同一迁移任务、同一批次、同一分段明细只落一个校验版本，重复提交返回已有版本；
* 核对清单每个迁移任务只保留一条可继续的完整边界，不堆叠多个互相冲突的边界。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime
from typing import Any, Iterable

from app.store import store

MODULE = "migrate"

STATUS_VALIDATED = "已校验"
STATUS_ABNORMAL = "异常"
STATUS_PENDING = "待校验"

# 与 services/migrate.py 的状态序列保持同一口径（这里不反向 import，避免循环依赖）。
TASK_STATUS_COMPLETED = "已完成"
TASK_STATUS_ROLLED_BACK = "已回滚"
TASK_STATUS_RUNNING = "迁移中"

STRATEGY_STOP = "停在异常段之前"
STRATEGY_ISOLATE = "隔离异常段续跑"
STRATEGIES = (STRATEGY_STOP, STRATEGY_ISOLATE)
DEFAULT_STRATEGY = STRATEGY_ISOLATE

CANONICAL_FIELDS = [
    "任务编号",
    "源表名",
    "目标表名",
    "迁移方式",
    "数据范围",
    "校验规则",
    "迁移耗时",
    "任务状态",
]

# 迁移任务上由分段校验/核对清单维护的字段，上线前检查不把它们当成结构漂移。
VALIDATION_FIELDS = (
    "批次号",
    "分段总数",
    "分段行数",
    "分段明细",
    "依赖源库",
    "依赖目标环境",
    "快照编号",
    "异常段处理",
    "已迁移段",
    "异常段",
    "已校验段",
    "隔离段",
    "下一次续跑位置",
    "完整边界",
    "校验版本",
    "校验批次",
    "迁移完成状态",
)

# 同一进程里的并发提交共用一把锁：先查指纹，再算版本号，避免两个请求各落一版。
_LOCK = threading.Lock()

_SEGMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "段号": ("段号", "segment_no", "segment", "分片号"),
    "行区间": ("行区间", "row_range", "range"),
    "源端行数": ("源端行数", "source_rows", "源行数"),
    "目标端行数": ("目标端行数", "target_rows", "目标行数"),
    "源端 payment_id 唯一数": (
        "源端 payment_id 唯一数",
        "源端唯一数",
        "source_distinct",
        "source_payment_id_distinct",
    ),
    "目标端 payment_id 唯一数": (
        "目标端 payment_id 唯一数",
        "目标端唯一数",
        "target_distinct",
        "target_payment_id_distinct",
    ),
    "源端 amount 汇总": ("源端 amount 汇总", "源端金额汇总", "source_amount", "source_amount_sum"),
    "目标端 amount 汇总": (
        "目标端 amount 汇总",
        "目标端金额汇总",
        "target_amount",
        "target_amount_sum",
    ),
    "目标分区": ("目标分区", "partition", "target_partition"),
    "校验状态": ("校验状态", "status", "segment_status"),
    "异常原因": ("异常原因", "reason", "error"),
}


def _pick(values: dict[str, Any], aliases: Iterable[str], default: Any = None) -> Any:
    for key in aliases:
        if key in values and values[key] not in (None, ""):
            return values[key]
    return default


def _as_number(value: Any) -> float | None:
    """把示例数据里的中文样例、千分位或数字字符串都收敛成数值。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(value: Any, default: int = 0) -> int:
    number = _as_number(value)
    return int(number) if number is not None else default


def _money(value: float) -> str:
    """金额保留两位小数，避免浮点误差写进核对清单。"""
    return f"{value:.2f}"


def _same_number(left: float, right: float, *, tolerance: float = 0.01) -> bool:
    return abs(left - right) <= tolerance


def _task_by_id(entry_id: int) -> dict[str, Any] | None:
    return store.find(MODULE, entry_id)


def _segment_rows(task: dict[str, Any]) -> int:
    return max(_as_int(task.get("分段行数"), 0), 0)


def _row_range(segment_no: int, rows: int) -> str:
    if rows <= 0:
        return ""
    start = (segment_no - 1) * rows + 1
    return f"{start}-{start + rows - 1}"


def _resume_position(segment_no: int, rows: int) -> str:
    if rows <= 0:
        return f"第 {segment_no} 段"
    start = (segment_no - 1) * rows + 1
    return f"第 {segment_no} 段 / 行 {start}"


def _provided(raw: dict[str, Any], field: str) -> bool:
    """提交的旧明细里是否显式给出了这个字段（空字符串也算给了）。"""
    return any(alias in raw and raw[alias] is not None for alias in _SEGMENT_ALIASES[field])


def _normalize_segment(raw: dict[str, Any], index: int, rows: int) -> dict[str, Any]:
    """兼容旧明细：中文字段、英文字段、缺字段都能读，缺号时按顺序补齐。"""
    segment_no = _as_int(_pick(raw, _SEGMENT_ALIASES["段号"]), index)
    segment_no = segment_no if segment_no > 0 else index
    row_range = str(_pick(raw, _SEGMENT_ALIASES["行区间"], "") or "") or _row_range(segment_no, rows)
    source_rows = _as_number(_pick(raw, _SEGMENT_ALIASES["源端行数"]))
    target_rows = _as_number(_pick(raw, _SEGMENT_ALIASES["目标端行数"]))
    source_distinct = _as_number(_pick(raw, _SEGMENT_ALIASES["源端 payment_id 唯一数"]))
    target_distinct = _as_number(_pick(raw, _SEGMENT_ALIASES["目标端 payment_id 唯一数"]))
    source_amount = _as_number(_pick(raw, _SEGMENT_ALIASES["源端 amount 汇总"]))
    target_amount = _as_number(_pick(raw, _SEGMENT_ALIASES["目标端 amount 汇总"]))
    status = str(_pick(raw, _SEGMENT_ALIASES["校验状态"], "") or "").strip()
    reason = str(_pick(raw, _SEGMENT_ALIASES["异常原因"], "") or "").strip()

    diffs: list[str] = []
    if source_rows is not None and target_rows is not None and not _same_number(source_rows, target_rows):
        diffs.append(f"行数差 {int(source_rows - target_rows)}")
    if (
        source_distinct is not None
        and target_distinct is not None
        and not _same_number(source_distinct, target_distinct)
    ):
        diffs.append(f"payment_id 唯一数少 {int(source_distinct - target_distinct)}")
    if (
        source_amount is not None
        and target_amount is not None
        and not _same_number(source_amount, target_amount)
    ):
        diffs.append(f"amount 汇总少 {_money(source_amount - target_amount)}")

    if status not in (STATUS_VALIDATED, STATUS_ABNORMAL, STATUS_PENDING):
        # 旧明细没有状态时按源目标差异推断；只有可成对比较且无差异才算已校验。
        comparable_pairs = sum(
            1
            for left, right in (
                (source_rows, target_rows),
                (source_distinct, target_distinct),
                (source_amount, target_amount),
            )
            if left is not None and right is not None
        )
        if comparable_pairs == 0:
            status = STATUS_PENDING
        else:
            status = STATUS_ABNORMAL if diffs else STATUS_VALIDATED
    if status == STATUS_ABNORMAL and not reason:
        reason = "；".join(diffs) or "源目标一致性校验未通过"

    return {
        "段号": segment_no,
        "行区间": row_range,
        "源端行数": int(source_rows) if source_rows is not None else None,
        "目标端行数": int(target_rows) if target_rows is not None else None,
        "源端 payment_id 唯一数": int(source_distinct) if source_distinct is not None else None,
        "目标端 payment_id 唯一数": int(target_distinct) if target_distinct is not None else None,
        "源端 amount 汇总": round(source_amount, 2) if source_amount is not None else None,
        "目标端 amount 汇总": round(target_amount, 2) if target_amount is not None else None,
        "目标分区": str(_pick(raw, _SEGMENT_ALIASES["目标分区"], "") or ""),
        "校验状态": status,
        "异常原因": reason,
    }


def _segments_signature(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """指纹只取会影响校验结论的字段，避免前端传无关字段导致重复落版本。"""
    return [
        {
            "段号": item["段号"],
            "行区间": item["行区间"],
            "源端行数": item["源端行数"],
            "目标端行数": item["目标端行数"],
            "源端 payment_id 唯一数": item["源端 payment_id 唯一数"],
            "目标端 payment_id 唯一数": item["目标端 payment_id 唯一数"],
            "源端 amount 汇总": item["源端 amount 汇总"],
            "目标端 amount 汇总": item["目标端 amount 汇总"],
            "校验状态": item["校验状态"],
        }
        for item in segments
    ]


def _fingerprint(task: dict[str, Any], batch: str, strategy: str, segments: list[dict[str, Any]]) -> str:
    payload = {
        "任务编号": str(task.get("任务编号") or task.get("id")),
        "批次号": batch,
        "异常段处理": strategy,
        "校验规则": str(task.get("校验规则") or ""),
        "分段明细": _segments_signature(segments),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _summarize(
    task: dict[str, Any],
    *,
    batch: str,
    strategy: str,
    segments: list[dict[str, Any]],
    version: int,
    source: str,
    created_at: str,
    fingerprint: str,
) -> dict[str, Any]:
    total = len(segments) or _as_int(task.get("分段总数"), 0)
    rows = _segment_rows(task)
    validated = [item["段号"] for item in segments if item["校验状态"] == STATUS_VALIDATED]
    pending = [item["段号"] for item in segments if item["校验状态"] == STATUS_PENDING]
    abnormal = [item["段号"] for item in segments if item["校验状态"] == STATUS_ABNORMAL]
    status_by_no = {item["段号"]: item["校验状态"] for item in segments}
    # 明细没覆盖到的段不能当成已校验，按待校验处理，避免边界越过去。
    for segment_no in range(1, total + 1):
        if segment_no not in status_by_no and segment_no not in pending:
            pending.append(segment_no)
    pending.sort()

    validated_set = set(validated)
    # 完整前缀：从第 1 段起连续通过校验的边界；异常段一来就截断。
    prefix = 1
    while prefix <= total and prefix in validated_set:
        prefix += 1
    prefix_boundary = prefix - 1

    # 隔离策略下，异常段之后的独立已校验段可以提交，边界推进到这些已校验段；
    # 但边界不能跨过待校验段，也不影响完成状态受异常段约束。
    boundary = prefix_boundary
    if strategy == STRATEGY_ISOLATE:
        for segment_no in range(prefix_boundary + 1, total + 1):
            status = status_by_no.get(segment_no, STATUS_PENDING)
            if status == STATUS_VALIDATED:
                boundary = segment_no
            elif status == STATUS_ABNORMAL:
                continue
            else:
                break
    isolated = [no for no in abnormal if any(item > no for item in validated)]

    resume_segment = abnormal[0] if abnormal else (pending[0] if pending else boundary + 1)
    complete = not abnormal and not pending and boundary >= total
    resume_position = "无（已完成）" if complete else _resume_position(resume_segment, rows)

    # 已迁移 = 已校验且在可继续边界以内。停在异常段之前时，后面的已校验段只是
    # 备查结果，不并进本次已迁移范围；隔离策略下它们随边界一起提交。
    migrated_segments = [no for no in validated if no <= boundary]
    deferred_segments = [no for no in validated if no > boundary]

    migrated_ranges: list[str] = []
    run_start: int | None = None
    previous: int | None = None
    for no in migrated_segments:
        if run_start is None:
            run_start = previous = no
            continue
        if previous is not None and no == previous + 1:
            previous = no
            continue
        migrated_ranges.append(f"第 {run_start}-{previous} 段" if run_start != previous else f"第 {run_start} 段")
        run_start = previous = no
    if run_start is not None:
        migrated_ranges.append(
            f"第 {run_start}-{previous} 段" if run_start != previous else f"第 {run_start} 段"
        )
    if rows > 0 and migrated_segments:
        migrated_range_text = (
            f"{migrated_ranges[0]}（行 1-{max(migrated_segments) * rows}）"
            if len(migrated_ranges) == 1
            else "、".join(migrated_ranges)
        )
    else:
        migrated_range_text = "、".join(migrated_ranges) or "尚无已校验段"

    migrated_set = set(migrated_segments)
    migrated_rows = sum(
        item["源端行数"] or 0
        for item in segments
        if item["校验状态"] == STATUS_VALIDATED and item["段号"] in migrated_set
    )
    source_rows = sum(item["源端行数"] or 0 for item in segments)
    target_rows = sum(item["目标端行数"] or 0 for item in segments)
    row_delta = sum(
        (item["源端行数"] or 0) - (item["目标端行数"] or 0)
        for item in segments
        if item["校验状态"] != STATUS_PENDING
    )

    return {
        "任务编号": str(task.get("任务编号") or ""),
        "批次号": batch,
        "校验版本": version,
        "校验规则": str(task.get("校验规则") or ""),
        "迁移方式": str(task.get("迁移方式") or ""),
        "异常段处理": strategy,
        "分段总数": total,
        "分段行数": rows,
        "已校验段": validated,
        "异常段": abnormal,
        "待校验段": pending,
        "隔离段": isolated if strategy == STRATEGY_ISOLATE else [],
        "完整边界": boundary,
        "完整前缀边界": prefix_boundary,
        "已迁移段": migrated_segments,
        "超出边界段": deferred_segments,
        "已迁移范围": migrated_range_text,
        "已迁移行数": migrated_rows,
        "源端行数": source_rows,
        "目标端行数": target_rows,
        "行数差异": row_delta,
        "下一次续跑位置": resume_position,
        "下一次续跑段": resume_segment,
        "迁移完成状态": "已完成" if complete else "未完成",
        "校验结论": "一致" if not abnormal and not pending else f"{len(abnormal)} 个异常段待复核",
        "来源": source,
        "生成时间": created_at,
        "指纹": fingerprint,
        "分段明细": segments,
        "依赖源库": str(task.get("依赖源库") or ""),
        "依赖目标环境": str(task.get("依赖目标环境") or ""),
        "快照编号": str(task.get("快照编号") or ""),
    }


def _checklist_from_version(version: dict[str, Any]) -> dict[str, Any]:
    """核对清单只保留一个可继续边界和一组异常段，不存历史版本堆叠。"""
    checklist = {
        key: version.get(key)
        for key in (
            "任务编号",
            "批次号",
            "校验版本",
            "校验规则",
            "异常段处理",
            "分段总数",
            "分段行数",
            "已校验段",
            "异常段",
            "隔离段",
            "完整边界",
            "已迁移段",
            "已迁移范围",
            "已迁移行数",
            "下一次续跑位置",
            "下一次续跑段",
            "迁移完成状态",
            "校验结论",
            "生成时间",
        )
    }
    return checklist


def _refresh_checklist(task: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
    previous = store.checklists.get(int(task["id"]), {})
    checklist = _checklist_from_version(version)
    # 统计模块回写的结论在再次校验后仍保留，方便同一批次对照。
    for key in ("统计报告", "统计版本", "统计批次", "统计结论", "统计时间"):
        if key in previous:
            checklist[key] = previous[key]
    store.checklists[int(task["id"])] = checklist

    # 任务上的完成状态与清单保持同一口径：有失败段就绝不保留「已完成」。
    # 但校验通过也不会自动推进任务状态，归档仍需走显式动作。
    task["校验版本"] = version["校验版本"]
    task["校验批次"] = version["批次号"]
    task["下一次续跑位置"] = version["下一次续跑位置"]
    task["完整边界"] = version["完整边界"]
    task["异常段"] = version["异常段"]
    task["已校验段"] = version["已校验段"]
    task["迁移完成状态"] = version["迁移完成状态"]
    task["abnormal"] = bool(version["异常段"])
    if task.get("status") == TASK_STATUS_ROLLED_BACK:
        task["pending"] = False
    elif task.get("status") == TASK_STATUS_COMPLETED:
        # 已完成的任务又发现失败段：状态回退，完成状态不能越过异常段。
        if version["迁移完成状态"] != "已完成":
            task["status"] = TASK_STATUS_RUNNING
            task["任务状态"] = TASK_STATUS_RUNNING
            task["pending"] = True
        else:
            task["pending"] = False
    else:
        task["pending"] = True
    return checklist


def _segments_from_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    raw_segments = task.get("分段明细")
    rows = _segment_rows(task)
    if isinstance(raw_segments, list) and raw_segments:
        return [
            _normalize_segment(item, index, rows)
            for index, item in enumerate(raw_segments, start=1)
            if isinstance(item, dict)
        ]
    return []


def _build_version(
    task: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    batch: str,
    strategy: str,
    version: int,
    source: str,
    fingerprint: str,
) -> dict[str, Any]:
    return _summarize(
        task,
        batch=batch,
        strategy=strategy,
        segments=segments,
        version=version,
        source=source,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        fingerprint=fingerprint,
    )


def ensure_validation(task: dict[str, Any]) -> dict[str, Any] | None:
    """任务带分段明细时，读取迁移任务即生成/恢复第一版校验结果。

    读取是幂等的：已有版本直接返回，核对详情从列表返回后再打开仍是同一版本。
    """
    task_id = int(task["id"])
    versions = store.validations.get(task_id)
    if versions:
        active = store.active_validations.get(task_id)
        for item in versions:
            if active is not None and int(item["校验版本"]) == int(active):
                return item
        return versions[-1]
    segments = _segments_from_task(task)
    if not segments:
        return None
    batch = str(task.get("批次号") or f"{date.today():%Y%m%d}-01")
    strategy = str(task.get("异常段处理") or DEFAULT_STRATEGY)
    if strategy not in STRATEGIES:
        strategy = DEFAULT_STRATEGY
    fingerprint = _fingerprint(task, batch, strategy, segments)
    version = _build_version(
        task,
        segments,
        batch=batch,
        strategy=strategy,
        version=1,
        source="任务明细导入",
        fingerprint=fingerprint,
    )
    store.validations[task_id] = [version]
    store.active_validations[task_id] = 1
    _refresh_checklist(task, version)
    return version


def list_versions(entry_id: int) -> list[dict[str, Any]]:
    task = _task_by_id(entry_id)
    if task is None:
        return []
    ensure_validation(task)
    return list(store.validations.get(entry_id, []))


def active_version_no(entry_id: int) -> int | None:
    versions = list_versions(entry_id)
    if not versions:
        return None
    active = store.active_validations.get(entry_id)
    for item in versions:
        if active is not None and int(item["校验版本"]) == int(active):
            return int(active)
    return int(versions[-1]["校验版本"])


def get_version(entry_id: int, version: int | None = None) -> dict[str, Any] | None:
    versions = list_versions(entry_id)
    if not versions:
        return None
    active = store.active_validations.get(entry_id)
    for item in versions:
        if active is not None and int(item.get("校验版本", 0)) == int(active):
            latest = item
            break
    else:
        latest = versions[-1]
    if version is None:
        return latest
    for item in versions:
        if int(item.get("校验版本", 0)) == int(version):
            return item
    return None


def get_checklist(entry_id: int) -> dict[str, Any] | None:
    task = _task_by_id(entry_id)
    if task is None:
        return None
    latest = ensure_validation(task)
    if latest is None:
        return None
    # 清单与最新校验版本始终同步，避免历史版本把边界改回去。
    if store.checklists.get(entry_id, {}).get("校验版本") != latest["校验版本"]:
        _refresh_checklist(task, latest)
    return store.checklists.get(entry_id)


def submit_validation(
    entry_id: int,
    values: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """提交一次分段校验；同指纹并发提交只落一个版本。"""
    task = _task_by_id(entry_id)
    if task is None:
        return None, f"迁移任务 {entry_id} 不存在或已归档"
    with _LOCK:
        existing = ensure_validation(task)
        if existing is None:
            return None, "迁移任务缺少分段明细，无法执行源目标一致性校验"
        raw_segments = values.get("分段明细")
        has_segments = isinstance(raw_segments, list) and bool(raw_segments)
        if has_segments:
            # 兼容已有明细：没提交的段保留原结果，提交的段只覆盖它给出的字段；
            # 提交里没带校验状态/异常原因时按合并后的数值重新推断，
            # 避免半截明细被误判为已校验。
            merged: dict[int, dict[str, Any]] = {
                item["段号"]: dict(item) for item in existing["分段明细"]
            }
            for index, raw in enumerate(raw_segments, start=1):
                if not isinstance(raw, dict):
                    continue
                item = _normalize_segment(raw, index, _segment_rows(task))
                base = dict(merged.get(item["段号"], {}))
                for key, value in item.items():
                    if value not in (None, "") or _provided(raw, key):
                        base[key] = value
                if not _provided(raw, "校验状态"):
                    base.pop("校验状态", None)
                if not _provided(raw, "异常原因"):
                    base.pop("异常原因", None)
                merged[item["段号"]] = _normalize_segment(base, item["段号"], _segment_rows(task))
            segments = [merged[no] for no in sorted(merged)]
        else:
            segments = [dict(item) for item in existing["分段明细"]]
        batch = str(values.get("批次号") or task.get("批次号") or existing["批次号"]).strip()
        strategy = str(values.get("异常段处理") or existing["异常段处理"] or DEFAULT_STRATEGY).strip()
        if strategy not in STRATEGIES:
            return None, f"异常段处理「{strategy}」不在允许范围：{'、'.join(STRATEGIES)}"

        fingerprint = _fingerprint(task, batch, strategy, segments)
        for item in store.validations.get(entry_id, []):
            if item["指纹"] == fingerprint:
                # 同一批次同一明细重新选中时复用原版本，当前版本指针也跟着回到它。
                store.active_validations[entry_id] = int(item["校验版本"])
                _refresh_checklist(task, item)
                return item, "源目标一致性校验结果已存在，沿用同一版本"
        versions = store.validations.setdefault(entry_id, [])
        next_version = max((int(item["校验版本"]) for item in versions), default=0) + 1
        version = _build_version(
            task,
            segments,
            batch=batch,
            strategy=strategy,
            version=next_version,
            source=str(values.get("来源") or "迁移详情提交"),
            fingerprint=fingerprint,
        )
        versions.append(version)
        store.active_validations[entry_id] = next_version
        _refresh_checklist(task, version)
        return version, f"源目标一致性校验已落库，版本 {next_version}"


def apply_strategy(entry_id: int, strategy: str) -> tuple[dict[str, Any] | None, str]:
    """切换异常段处理口径，按当前分段明细重新形成唯一边界。"""
    if strategy not in STRATEGIES:
        return None, f"异常段处理「{strategy}」不在允许范围：{'、'.join(STRATEGIES)}"
    latest = get_version(entry_id)
    if latest is None:
        return None, "迁移任务还没有可用的源目标一致性校验结果"
    return submit_validation(entry_id, {"异常段处理": strategy, "批次号": latest["批次号"]})


def preflight(entry_id: int) -> dict[str, Any] | None:
    """上线前检查：源库/目标环境依赖、回退快照，以及本地与线上字段是否漂移。"""
    task = _task_by_id(entry_id)
    if task is None:
        return None

    snapshot_no = str(task.get("快照编号") or "")
    snapshot = None
    if snapshot_no:
        snapshot = next(
            (row for row in store.rows("snapshot") if str(row.get("快照编号")) == snapshot_no),
            None,
        )
    missing_fields = [
        field for field in CANONICAL_FIELDS if not str(task.get(field) or "").strip()
    ]
    # 只把迁移任务自身的字段视为漂移，校验过程回写/登记时带入的附加字段不算。
    mirrored_fields = set(CANONICAL_FIELDS) | set(VALIDATION_FIELDS) | {
        "id",
        "status",
        "pending",
        "abnormal",
    }
    extra_fields = [key for key in task if key not in mirrored_fields]
    warnings: list[str] = []
    if not str(task.get("依赖源库") or "").strip():
        warnings.append("缺少依赖源数据库配置")
    if not str(task.get("依赖目标环境") or "").strip():
        warnings.append("缺少依赖目标环境配置")
    if not snapshot_no:
        warnings.append("缺少上线前数据库快照，无法作为回退依据")
    elif snapshot is None:
        warnings.append(f"快照 {snapshot_no} 不在快照清单中，回退依据不完整")
    if missing_fields:
        warnings.append(f"迁移任务缺少字段：{'、'.join(missing_fields)}")
    if extra_fields:
        warnings.append(f"迁移任务存在非标准字段，可能与线上结构漂移：{'、'.join(extra_fields)}")

    ready = not warnings
    return {
        "任务编号": task.get("任务编号"),
        "依赖源库": task.get("依赖源库"),
        "依赖目标环境": task.get("依赖目标环境"),
        "快照编号": snapshot_no,
        "快照存在": snapshot is not None,
        "快照表": snapshot.get("表名称") if snapshot else None,
        "快照时间": snapshot.get("快照时间") if snapshot else None,
        "结构漂移": warnings,
        "可上线": ready,
        "结论": "依赖与回退快照齐备，可继续迁移" if ready else "上线前检查未通过，请先补齐依赖或快照",
    }


def mark_resumed(entry_id: int) -> tuple[dict[str, Any] | None, str]:
    """执行迁移时只从续跑位置继续，不从头扫描已校验段。"""
    latest = get_version(entry_id)
    task = _task_by_id(entry_id)
    if task is None:
        return None, f"迁移任务 {entry_id} 不存在或已归档"
    if latest is None:
        return None, "迁移任务还没有分段校验结果，无法确定续跑位置"
    task["status"] = "迁移中"
    task["任务状态"] = "迁移中"
    task["pending"] = True
    task["abnormal"] = bool(latest["异常段"])
    task["下一次续跑位置"] = latest["下一次续跑位置"]
    task["完整边界"] = latest["完整边界"]
    return task, (
        f"迁移任务从 {latest['下一次续跑位置']} 续跑，"
        f"已校验段 {latest['已校验段'] or '无'} 不重复扫描"
    )
