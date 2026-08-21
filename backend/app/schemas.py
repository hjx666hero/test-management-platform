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


# ==================== Auto-Fix Agent ====================

class AutoFixRequest(BaseModel):
    """触发 Auto-Fix Agent 的请求体。"""

    file_path: str = Field(..., description="失败用例所在文件,相对项目一根目录(如 testcases/test_login.py)")
    case_name: str = Field(..., max_length=255, description="用例名(pytest 用例函数名或 TMS 注册表用例名)")
    error_log: str = Field("", description="错误日志;可留空,Agent 会先运行用例复现失败")
    task_id: Optional[int] = Field(None, description="来源任务 ID(追溯用,可选)")
    result_id: Optional[int] = Field(None, description="来源用例结果 ID(追溯用,可选)")


class FixSuggestionOut(BaseModel):
    """修复建议(对外展示)。"""

    id: int
    task_id: Optional[int]
    result_id: Optional[int]
    case_name: str
    file_path: str
    original_code: str
    fixed_code: str
    diff: str
    explanation: str
    status: str = Field(..., description="pending_review/applied/rejected")
    verified: Optional[bool] = Field(None, description="None=未验证 / True/False=Agent 验证结论")
    verify_output: Optional[str]
    created_at: Optional[datetime]


class FixStatusUpdate(BaseModel):
    """人工审核动作。"""

    status: str = Field(..., description="applied=应用补丁到源文件 / rejected=拒绝")
