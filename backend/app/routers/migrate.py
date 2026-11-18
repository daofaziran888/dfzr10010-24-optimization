"""数据迁移管理接口：维护迁移任务，覆盖执行迁移、回滚迁移、归档任务等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import migration_validation as validation
from app.services.migrate import MigrateService

router = APIRouter(prefix="/api/migrate", tags=["数据迁移管理"])

service = MigrateService()

LIST_FIELDS = ["任务编号", "源表名", "目标表名", "迁移方式", "数据范围", "校验规则", "迁移耗时", "任务状态"]
STATUSES = ["待执行", "迁移中", "已完成", "已回滚"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="待执行、迁移中、已完成、已回滚"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤数据迁移管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据迁移管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "migrate", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条迁移任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/validations", response_model=dict)
def list_validations(entry_id: int) -> dict:
    """读取迁移任务的源目标一致性校验结果；默认返回最新版本，可按版本号回看旧批次。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {entry_id} 不存在或已归档")
    versions = validation.list_versions(entry_id)
    return {
        "task": entry_id,
        "version": validation.active_version_no(entry_id),
        "total": len(versions),
        "items": versions,
    }


@router.get("/{entry_id}/validations/{version_no}", response_model=dict)
def get_validation(entry_id: int, version_no: int) -> dict:
    """按版本号读取校验结果，统计模块据此核对旧批次是否与当前版本一致。"""
    item = validation.get_version(entry_id, version_no)
    if item is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {entry_id} 的校验版本 {version_no} 不存在")
    return item


@router.post("/{entry_id}/validations", response_model=ActionResult)
def submit_validation(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交源目标一致性校验结果：同一批次同一明细只落一个版本，已有明细兼容合并。"""
    entry, message = validation.submit_validation(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/validation-strategy", response_model=ActionResult)
def switch_strategy(entry_id: int, payload: EntryPayload) -> ActionResult:
    """切换异常段处理口径：停在异常段之前，或隔离异常段继续提交独立分段。"""
    strategy = str(payload.values.get("异常段处理") or "").strip()
    entry, message = validation.apply_strategy(entry_id, strategy)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/checklist", response_model=dict)
def get_checklist(entry_id: int) -> dict:
    """迁移核对清单：已迁移范围、异常段、下一次续跑位置与唯一的可继续边界。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {entry_id} 不存在或已归档")
    checklist = validation.get_checklist(entry_id)
    if checklist is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {entry_id} 还没有源目标一致性校验结果")
    return checklist


@router.get("/{entry_id}/preflight", response_model=dict)
def preflight(entry_id: int) -> dict:
    """上线前检查：源库/目标环境依赖、回退快照与本地线上结构漂移。"""
    result = validation.preflight(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"迁移任务 {entry_id} 不存在或已归档")
    return result


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条迁移任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="迁移任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条迁移任务执行执行迁移、回滚迁移、归档任务；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
