"""ORM 模型:测试任务 / 用例结果 / AI 分析。"""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from .database import Base


class TestTask(Base):
    """一次测试执行任务。"""

    __tablename__ = "test_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)          # 任务名
    env_url = Column(String(255), nullable=False)       # 被测环境地址
    tags = Column(JSON, nullable=False, default=list)   # 选中的标签,如 ["articles","P0"]
    status = Column(String(16), nullable=False, default="pending")  # pending/running/success/failed/partial
    total = Column(Integer, default=0)
    passed = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    duration_s = Column(Integer, default=0)             # 执行耗时(秒)
    error = Column(Text, nullable=True)                 # 执行层错误(如登录失败)
    created_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime, nullable=True)

    results = relationship(
        "TestCaseResult",
        back_populates="task",
        cascade="all, delete-orphan",
    )


class TestCaseResult(Base):
    """单条用例执行结果(含请求/响应快照,供 AI 分析)。"""

    __tablename__ = "test_case_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("test_tasks.id", ondelete="CASCADE"), nullable=False)
    case_name = Column(String(255), nullable=False)      # 用例名
    status = Column(String(16), nullable=False)          # passed/failed
    duration_ms = Column(Integer, default=0)
    request_snapshot = Column(JSON, nullable=True)       # {method, url, payload}
    response_snapshot = Column(JSON, nullable=True)      # {status_code, body 截断}
    error_message = Column(Text, nullable=True)          # 断言/异常信息
    ai_analysis = Column(Text, nullable=True)            # 通义千问根因分析
    ai_status = Column(String(16), default="none")       # none/skipped/pending/done/failed

    task = relationship("TestTask", back_populates="results")