"""任务管理 API:创建任务(后台异步执行)、任务列表、任务详情。"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from .. import config, schemas
from ..database import get_db
from ..models import TestTask
from ..services.executor import run_task

logger = logging.getLogger("tms.tasks")
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post("", response_model=schemas.TaskSummary, status_code=201)
def create_task(payload: schemas.TaskCreate, background: BackgroundTasks, db: Session = Depends(get_db)):
    """创建测试任务并后台异步执行。"""
    env_url = (payload.env_url or config.DEFAULT_BASE_URL).rstrip("/")
    task = TestTask(
        name=payload.name,
        env_url=env_url,
        tags=payload.tags or [],
        status="pending",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    logger.info("任务创建: id=%s name=%s env=%s tags=%s", task.id, task.name, env_url, task.tags)
    background.add_task(run_task, task.id)
    return task


@router.get("", response_model=list[schemas.TaskSummary])
def list_tasks(db: Session = Depends(get_db)):
    """任务列表(新到旧)。"""
    return db.query(TestTask).order_by(desc(TestTask.id)).limit(100).all()


@router.get("/{task_id}", response_model=schemas.TaskSummary)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(TestTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/{task_id}/report", response_model=schemas.ReportDetail)
def get_report(task_id: int, db: Session = Depends(get_db)):
    """报告详情:任务汇总 + 全部用例结果(含 AI 分析)。"""
    task = db.get(TestTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task, "results": task.results}
