"""Auto-Fix Agent API:触发自动修复、修复建议查询、人工审核。

端点一览:
    POST  /api/fixes/auto-fix          提交失败用例给 Agent 后台处理(BackgroundTasks)
    GET   /api/fixes/suggestions       建议列表(可按状态/用例名过滤)
    GET   /api/fixes/suggestions/{id}  建议详情(含 diff/原文/修复文/验证输出)
    PATCH /api/fixes/suggestions/{id}  人工审核:applied=应用补丁 / rejected=拒绝
"""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from .. import schemas
from ..agent import auto_fix_case, db as agent_db, review as agent_review
from ..agent.config import AGENT_ENABLED

logger = logging.getLogger("tms.fixes")
router = APIRouter(prefix="/api/fixes", tags=["fixes"])


@router.post("/auto-fix", status_code=202)
def trigger_auto_fix(payload: schemas.AutoFixRequest, background: BackgroundTasks):
    """提交一条失败用例给 Auto-Fix Agent(ReAct 循环,最多 3 轮,后台异步执行)。"""
    if not AGENT_ENABLED:
        raise HTTPException(status_code=400, detail="未配置 DASHSCOPE_API_KEY,Agent 不可用")

    # 与平台任务执行(run_task)一致的 BackgroundTasks 接入方式
    background.add_task(
        auto_fix_case,
        task_id=payload.task_id,
        result_id=payload.result_id,
        case_name=payload.case_name,
        file_path=payload.file_path,
        error_log=payload.error_log,
    )
    logger.info("Auto-Fix 已提交后台: case=%s file=%s", payload.case_name, payload.file_path)
    return {"message": "Auto-Fix 已提交后台执行,请稍后在「修复建议」页查看结果"}


@router.get("/suggestions", response_model=list[schemas.FixSuggestionOut])
def list_suggestions(
    status: str = Query(None, description="按状态过滤:pending_review/applied/rejected"),
    case_name: str = Query(None, description="按用例名过滤"),
    limit: int = Query(100, ge=1, le=500),
):
    """修复建议列表(新到旧),供前端审核页轮询展示。"""
    return agent_db.list_suggestions(status=status, case_name=case_name, limit=limit)


@router.get("/suggestions/{sid}", response_model=schemas.FixSuggestionOut)
def get_suggestion(sid: int):
    """单条建议详情。"""
    row = agent_db.get_suggestion(sid)
    if not row:
        raise HTTPException(status_code=404, detail="修复建议不存在")
    return row


@router.patch("/suggestions/{sid}", response_model=schemas.FixSuggestionOut)
def review_suggestion(sid: int, payload: schemas.FixStatusUpdate):
    """人工审核:applied=把补丁写入源文件 / rejected=拒绝(不动文件)。"""
    if payload.status not in agent_review.ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail="status 仅支持 applied / rejected")

    ok, msg = agent_review.review_suggestion(sid, payload.status)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    return agent_db.get_suggestion(sid)
