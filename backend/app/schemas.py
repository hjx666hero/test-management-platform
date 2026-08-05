"""Pydantic 请求/响应模型。"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    name: str = Field(..., max_length=128, description="任务名")
    env_url: Optional[str] = Field(None, description="被测环境地址,为空则用默认")
    tags: List[str] = Field(default_factory=list, description="用例标签,如 ['articles','P0']")


class TaskSummary(BaseModel):
    id: int
    name: str
    env_url: str
    tags: List[str]
    status: str
    total: int
    passed: int
    failed: int
    duration_s: int
    error: Optional[str]
    created_at: datetime
    finished_at: Optional[datetime]

    class Config:
        from_attributes = True


class CaseResultOut(BaseModel):
    id: int
    case_name: str
    status: str
    duration_ms: int
    request_snapshot: Optional[dict]
    response_snapshot: Optional[dict]
    error_message: Optional[str]
    ai_analysis: Optional[str]
    ai_status: str

    class Config:
        from_attributes = True


class ReportDetail(BaseModel):
    task: TaskSummary
    results: List[CaseResultOut]
