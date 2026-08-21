"""Agent 数据库操作:修复建议(fix_suggestions 表)的持久化与查询。

核心约束(需求 5):
    所有修复建议入库状态固定为 pending_review —— Agent 只"建议",
    绝不直接修改被测源代码;由人工在平台审核后决定应用(applied)或拒绝(rejected)。

设计要点:
1. 复用 TMS 的 database.py 引擎/会话工厂,与 test_tasks 等表同库;
2. ORM 模型 FixSuggestion 定义在此(数据库职责内聚);
3. 模块导入时幂等建表——即使 main.py 尚未导入 agent,单独调用也能建表;
4. 每个函数自管 Session(与 ai_analyzer.py 风格一致,适合后台任务)。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from ..database import Base, SessionLocal, engine


class FixSuggestion(Base):
    """一条 AI 生成的修复建议(unified diff + 修复理由 + 验证结论)。"""

    __tablename__ = "fix_suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, nullable=True, index=True)      # 来源任务(可追溯)
    result_id = Column(Integer, nullable=True, index=True)    # 来源用例结果(可追溯)
    case_name = Column(String(255), nullable=False)           # 失败用例名
    file_path = Column(String(512), nullable=False)           # 目标文件(相对项目一根目录)
    original_code = Column(Text, nullable=False)              # 被替换的原始代码片段
    fixed_code = Column(Text, nullable=False)                 # 修复后的代码片段
    diff = Column(Text, nullable=False)                       # unified diff 格式补丁
    explanation = Column(Text, nullable=False)                # AI 修复理由(根因+方案)
    # pending_review=待审核(初始) / applied=已应用 / rejected=已拒绝
    status = Column(String(16), nullable=False, default="pending_review")
    verified = Column(Boolean, nullable=True)                 # None=未验证 / True/False=run_pytest 验证结论
    verify_output = Column(Text, nullable=True)               # 验证运行时的 pytest 输出摘要
    created_at = Column(DateTime, default=datetime.now)


# 幂等建表:导入本模块即确保表存在(create_all 只建缺失的表,已存在则跳过)
Base.metadata.create_all(bind=engine)


# ==================== 写操作 ====================

def save_fix_suggestion(
    *,
    case_name: str,
    file_path: str,
    original_code: str,
    fixed_code: str,
    diff: str,
    explanation: str,
    task_id: Optional[int] = None,
    result_id: Optional[int] = None,
) -> int:
    """保存一条修复建议(状态固定 pending_review),返回补丁 ID。

    由 tools.generate_patch 调用;状态不作为参数传入,从源头保证
    "只建议、不修改"的安全约束。
    """
    db = SessionLocal()
    try:
        row = FixSuggestion(
            task_id=task_id,
            result_id=result_id,
            case_name=case_name,
            file_path=file_path,
            original_code=original_code,
            fixed_code=fixed_code,
            diff=diff,
            explanation=explanation,
            status="pending_review",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def mark_verified(patch_id: int, verified: bool, output: str = "") -> None:
    """回写补丁验证结论。

    由 tools.run_pytest 在"临时应用补丁跑测试"后调用:
    verified=True 表示应用补丁后用例通过,修复有效。
    """
    db = SessionLocal()
    try:
        row = db.get(FixSuggestion, patch_id)
        if row:
            row.verified = verified
            row.verify_output = output[:4000]
            db.commit()
    finally:
        db.close()


def update_status(patch_id: int, status: str) -> None:
    """人工审核后更新状态(applied / rejected)——预留给审核 API 使用。"""
    db = SessionLocal()
    try:
        row = db.get(FixSuggestion, patch_id)
        if row:
            row.status = status
            db.commit()
    finally:
        db.close()


# ==================== 读操作 ====================

def _to_dict(row: FixSuggestion) -> dict:
    """ORM 行 → dict(在 Session 关闭前取出所有字段,避免 DetachedInstanceError)。"""
    return {
        "id": row.id,
        "task_id": row.task_id,
        "result_id": row.result_id,
        "case_name": row.case_name,
        "file_path": row.file_path,
        "original_code": row.original_code,
        "fixed_code": row.fixed_code,
        "diff": row.diff,
        "explanation": row.explanation,
        "status": row.status,
        "verified": row.verified,
        "verify_output": row.verify_output,
        "created_at": row.created_at,
    }


def get_suggestion(patch_id: int) -> Optional[dict]:
    """按 ID 取补丁详情;不存在返回 None。"""
    db = SessionLocal()
    try:
        row = db.get(FixSuggestion, patch_id)
        return _to_dict(row) if row else None
    finally:
        db.close()


def list_suggestions(
    status: Optional[str] = None,
    limit: int = 100,
    case_name: Optional[str] = None,
) -> list:
    """查询建议列表(新到旧);可按状态/用例名过滤,默认全部。

    例:list_suggestions("pending_review") → 所有待审核建议。
    """
    db = SessionLocal()
    try:
        query = db.query(FixSuggestion)
        if status:
            query = query.filter(FixSuggestion.status == status)
        if case_name:
            query = query.filter(FixSuggestion.case_name == case_name)
        rows = query.order_by(FixSuggestion.id.desc()).limit(limit).all()
        return [_to_dict(r) for r in rows]
    finally:
        db.close()
