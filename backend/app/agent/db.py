"""Agent 数据库操作:修复建议/评估场景/ReAct 轨迹的持久化与查询。

核心约束(需求 5):
    所有修复建议入库状态固定为 pending_review —— Agent 只"建议",
    绝不直接修改被测源代码;由人工在平台审核后决定应用(applied)或拒绝(rejected)。

设计要点:
1. 复用 TMS 的 database.py 引擎/会话工厂,与 test_tasks 等表同库;
2. ORM 模型(FixSuggestion/EvalCase/AgentTrajectory)定义在此(数据库职责内聚);
3. 模块导入时幂等建表——即使 main.py 尚未导入 agent,单独调用也能建表;
4. 每个函数自管 Session(与 ai_analyzer.py 风格一致,适合后台任务);
5. 轨迹写入走后台线程池(异步),任何失败只记日志,绝不影响 Agent 主流程。
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from ..database import Base, SessionLocal, engine

logger = logging.getLogger("tms.agent.db")

# 轨迹异步写入线程池:LLM 每轮思考的落库不阻塞 ReAct 循环
_TRAJ_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-traj")


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
    # LLM-as-judge 自动评审结果(生成补丁后异步评审,全降级不影响主流程)
    judge_score = Column(Integer, nullable=True)              # 0-10 分(越高越好)
    judge_verdict = Column(String(16), nullable=True)         # approve / warn / reject
    judge_comment = Column(Text, nullable=True)               # 评审意见(安全性/最小性/正确性)
    created_at = Column(DateTime, default=datetime.now)


def _ensure_columns(table: str, columns: dict) -> None:
    """幂等加列:表已存在时补齐缺失列(create_all 不会给已有表加新列)。

    MySQL 不支持 ADD COLUMN IF NOT EXISTS,故查 information_schema 判断。
    失败只记日志(评审字段缺失时 judge 功能自然降级,不影响主流程)。
    """
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            existing = {
                row[0] for row in conn.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                    ),
                    {"t": table},
                )
            }
            for col, ddl in columns.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                    logger.info("已为 %s 补充列: %s", table, col)
            conn.commit()
    except Exception:  # noqa: BLE001 迁移失败不影响启动
        logger.warning("列迁移检查失败(已忽略): %s", table, exc_info=True)


# fix_suggestions 评审列幂等迁移(老库升级自动补列)
_ensure_columns("fix_suggestions", {
    "judge_score": "INT NULL",
    "judge_verdict": "VARCHAR(16) NULL",
    "judge_comment": "TEXT NULL",
})


# ==================== 评估场景表 ====================

class EvalCase(Base):
    """评估场景:一条预置失败用例(用于度量 Agent 的自动修复能力)。

    分两类:
    - 可注入类(field_change/assertion_error):inject_original/inject_buggy 非空,
      run_eval 先把"正确代码"替换成"故障代码"再跑 Agent → Agent 面对的是真实失败;
    - 环境类(timeout/env_jitter):不注入,考察 Agent 能否识别为环境问题并
      克制地不产出补丁(expected_patch 为空 = 无需代码修复)。
    """

    __tablename__ = "eval_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)            # 场景标题(全局唯一)
    error_type = Column(String(32), nullable=False, index=True)  # 四类:field_change/timeout/assertion_error/env_jitter
    file_path = Column(String(512), nullable=False)        # 失败用例所在文件(相对项目一根目录)
    case_name = Column(String(255), nullable=False)        # pytest node 后缀(含参数化,如 TestArticles::test_create_article[normal_title])
    failure_log = Column(Text, nullable=False)             # 模拟真实 pytest 报错文本(Agent 的输入)
    expected_patch = Column(Text)                          # 期望修复:可注入类=正确代码;环境类=空(不应改代码)
    inject_original = Column(Text)                         # 注入锚点:文件中的正确代码片段(必须唯一)
    inject_buggy = Column(Text)                            # 注入替换:故障代码片段
    created_at = Column(DateTime, default=datetime.now)


class AgentTrajectory(Base):
    """Agent ReAct 轨迹:每轮 LLM 调用前后的思考与工具结果(可观测性/回放)。

    - trace_id:一次 Agent 运行的唯一标识,关联该次运行的全部轮次;
    - patch_id:本轮产出补丁时回填(关联 fix_suggestions);
    - eval_case_id:评估运行时回填(关联 eval_cases)。
    """

    __tablename__ = "agent_trajectories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String(64), nullable=False, index=True)
    round_num = Column(Integer, nullable=False)            # 轮次(0=启动)
    thought = Column(Text)                                 # 本轮 LLM 思考内容(或阶段标记)
    tool_result = Column(Text)                             # 本轮工具执行结果(JSON 文本)
    patch_id = Column(Integer, nullable=True, index=True)
    eval_case_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)


class AgentCost(Base):
    """LLM 调用成本:每次调用通义千问后异步写入(token 用量与人民币成本)。

    - trace_id:同一(次 Agent 运行的所有 LLM 调用共享;
    - 计价:通义千问 qwen-plus 官方价 输入￥0.0008/千token、输出￥0.002/千token;
    - cost_rmb 保留 6 位小数(单次调用通常远小于 0.01 元)。
    """

    __tablename__ = "agent_costs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String(64), nullable=False, index=True)
    task_id = Column(Integer, nullable=True, index=True)   # 来源任务(追溯)
    model = Column(String(64), nullable=False)             # 实际使用的模型名
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cost_rmb = Column(String(20), nullable=False, default="0")  # DECIMAL 更严谨,这里用字符串存精确值
    created_at = Column(DateTime, default=datetime.now, index=True)


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


def update_judge(patch_id: int, score: int, verdict: str, comment: str) -> None:
    """回写 LLM-as-judge 评审结果(评分/结论/意见)。"""
    db = SessionLocal()
    try:
        row = db.get(FixSuggestion, patch_id)
        if row:
            row.judge_score = score
            row.judge_verdict = verdict
            row.judge_comment = (comment or "")[:4000]
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
        "judge_score": getattr(row, "judge_score", None),
        "judge_verdict": getattr(row, "judge_verdict", None),
        "judge_comment": getattr(row, "judge_comment", None),
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


# ==================== 评估场景(EvalCase) ====================

def _eval_to_dict(row: EvalCase) -> dict:
    """EvalCase 行 → dict(Session 关闭前取出全部字段)。"""
    return {
        "id": row.id,
        "title": row.title,
        "error_type": row.error_type,
        "file_path": row.file_path,
        "case_name": row.case_name,
        "failure_log": row.failure_log,
        "expected_patch": row.expected_patch,
        "inject_original": row.inject_original,
        "inject_buggy": row.inject_buggy,
        "created_at": row.created_at,
    }


def reset_eval_cases(cases: list) -> int:
    """清空并批量写入评估场景(幂等初始化),返回写入条数。

    cases: [{title, error_type, file_path, case_name, failure_log,
             expected_patch, inject_original, inject_buggy}]
    """
    db = SessionLocal()
    try:
        db.query(EvalCase).delete()
        db.commit()
        for c in cases:
            db.add(EvalCase(
                title=c["title"],
                error_type=c["error_type"],
                file_path=c["file_path"],
                case_name=c["case_name"],
                failure_log=c["failure_log"],
                expected_patch=c.get("expected_patch"),
                inject_original=c.get("inject_original"),
                inject_buggy=c.get("inject_buggy"),
            ))
        db.commit()
        return len(cases)
    finally:
        db.close()


def list_eval_cases(error_type: Optional[str] = None) -> list:
    """查询评估场景(id 升序);可按 error_type 过滤。"""
    db = SessionLocal()
    try:
        query = db.query(EvalCase)
        if error_type:
            query = query.filter(EvalCase.error_type == error_type)
        rows = query.order_by(EvalCase.id.asc()).all()
        return [_eval_to_dict(r) for r in rows]
    finally:
        db.close()


# ==================== Agent 轨迹(AgentTrajectory) ====================

def save_trajectory_async(
    trace_id: str,
    round_num: int,
    thought: str,
    tool_result: Optional[str] = None,
    patch_id: Optional[int] = None,
    eval_case_id: Optional[int] = None,
) -> None:
    """异步写轨迹:提交到后台线程落库。

    任何失败(DB 不可用/字段超长等)只在后台线程记 warning,
    绝不抛回 Agent 主流程——可观测性不能以牺牲可用性为代价。
    """
    def _write():
        session = SessionLocal()
        try:
            session.add(AgentTrajectory(
                trace_id=trace_id,
                round_num=round_num,
                thought=(thought or "")[:8000],
                tool_result=(tool_result or "")[:16000],
                patch_id=patch_id,
                eval_case_id=eval_case_id,
            ))
            session.commit()
        except Exception:  # noqa: BLE001 轨迹失败绝不影响主流程
            logger.warning("轨迹写入失败(已忽略): trace=%s round=%s", trace_id, round_num, exc_info=True)
        finally:
            session.close()

    _TRAJ_POOL.submit(_write)


def shutdown_trajectory_pool() -> None:
    """等待所有待写轨迹落库后关闭线程池(脚本退出前调用)。"""
    _TRAJ_POOL.shutdown(wait=True)


def list_trajectories_by_patch(patch_id: int, limit: int = 200) -> list:
    """按补丁 ID 查询关联的 ReAct 轨迹(供 /ask 对话接口拼装上下文)。

    patch_id 在轨迹生成补丁后回填,因此返回的是"产出该补丁的那次运行"
    的全部轮次(启动/LLM请求/思考/工具结果/最终结论)。
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(AgentTrajectory)
            .filter(AgentTrajectory.patch_id == patch_id)
            .order_by(AgentTrajectory.id.asc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "trace_id": r.trace_id,
                "round_num": r.round_num,
                "thought": r.thought,
                "tool_result": r.tool_result,
                "eval_case_id": r.eval_case_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    finally:
        db.close()


def list_trajectories_by_trace(trace_id: str, after_id: int = 0, limit: int = 100) -> list:
    """按 trace_id 查询轨迹(id 升序,可增量)。SSE 实时思考流的游标查询。

    after_id:只返回 id 大于它的记录(上次已推送的不再重复推)。
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(AgentTrajectory)
            .filter(AgentTrajectory.trace_id == trace_id, AgentTrajectory.id > after_id)
            .order_by(AgentTrajectory.id.asc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "round_num": r.round_num,
                "thought": r.thought,
                "tool_result": r.tool_result,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


# ==================== 成本统计(AgentCost) ====================

# 通义千问计价(元/千token):从平台配置读,换模型时改 .env 即可
from .. import config as _app_config  # noqa: E402  (避免循环导入,延迟到模块尾段)

PRICE_INPUT_PER_1K = _app_config.AI_PRICE_INPUT_PER_1K
PRICE_OUTPUT_PER_1K = _app_config.AI_PRICE_OUTPUT_PER_1K


def calc_cost_rmb(input_tokens: int, output_tokens: int) -> str:
    """按官方计价计算单次调用成本(元),返回字符串保留 6 位小数。"""
    cost = input_tokens / 1000 * PRICE_INPUT_PER_1K + output_tokens / 1000 * PRICE_OUTPUT_PER_1K
    return f"{cost:.6f}"


def save_cost_async(trace_id: str, model: str, input_tokens: int, output_tokens: int,
                    task_id: Optional[int] = None) -> None:
    """异步写一条 LLM 调用成本(与轨迹共用线程池,失败只记日志)。

    计费属旁路能力:任何 DB 故障绝不影响 Agent 主流程。
    """
    def _write():
        session = SessionLocal()
        try:
            session.add(AgentCost(
                trace_id=trace_id,
                task_id=task_id,
                model=model,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cost_rmb=calc_cost_rmb(int(input_tokens or 0), int(output_tokens or 0)),
            ))
            session.commit()
        except Exception:  # noqa: BLE001 计费失败绝不影响主流程
            logger.warning("成本写入失败(已忽略): trace=%s", trace_id, exc_info=True)
        finally:
            session.close()

    _TRAJ_POOL.submit(_write)


def get_today_stats() -> dict:
    """今日看板数据:运行次数(去重 trace)、总花费、平均耗时(秒)。

    - 运行次数:今日有成本记录的不同 trace_id 数(一次 Agent 运行多次 LLM 调用算 1);
    - 总花费:今日全部调用成本求和;
    - 平均耗时:从 agent_trajectories 按 trace 聚合(首条 created_at → 末条),
      取"今日有过活动"的 trace 计算平均时长。
    """
    from datetime import date

    db = SessionLocal()
    try:
        today = date.today()
        rows = db.query(AgentCost).filter(
            AgentCost.created_at >= datetime(today.year, today.month, today.day),
        ).all()
        trace_ids = {r.trace_id for r in rows}
        total_cost = sum(float(r.cost_rmb or 0) for r in rows)

        # 平均耗时:轨迹表按 trace 聚合(当日活跃的 trace)
        trajs = db.query(AgentTrajectory).filter(
            AgentTrajectory.trace_id.in_(trace_ids),
        ).order_by(AgentTrajectory.id.asc()).all() if trace_ids else []
        spans: dict = {}
        for t in trajs:
            s = spans.setdefault(t.trace_id, [t.created_at, t.created_at])
            if t.created_at < s[0]:
                s[0] = t.created_at
            if t.created_at > s[1]:
                s[1] = t.created_at
        durations = [(s[1] - s[0]).total_seconds() for s in spans.values() if s[0] and s[1]]

        return {
            "date": str(today),
            "runs": len(trace_ids),                                   # 今日运行次数
            "total_cost_rmb": round(total_cost, 6),                    # 今日总花费(元)
            "avg_duration_s": round(sum(durations) / len(durations), 1) if durations else 0.0,  # 平均耗时(秒)
        }
    finally:
        db.close()


# 幂等建表:放在所有 ORM 模型定义之后,导入本模块即确保三张表全部就绪
Base.metadata.create_all(bind=engine)
