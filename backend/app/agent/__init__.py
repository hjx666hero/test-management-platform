"""Auto-Fix Agent 模块:ReAct 模式的失败用例自动修复助手。

模块结构:
- config.py  Agent 配置(通义千问 API/模型/最大迭代轮数/超时)
- models.py  Pydantic 数据模型(失败信息/工具结果/运行结果)
- tools.py   4 个工具实现 + OpenAI function calling Schema
- db.py      修复建议持久化(fix_suggestions 表,状态 pending_review)
- agent.py   Agent 核心类(ReAct 循环/LLM 调用/工具调度)

在 TMS 中的接入方式(与现有 BackgroundTasks 用法一致):

    from app.agent import auto_fix_case   # backend 目录下运行时
    background.add_task(
        auto_fix_case,
        task_id=task.id, result_id=row.id,
        case_name=row.case_name,
        file_path="testcases/test_login.py",
        error_log=row.error_message,
    )

程序化调用:

    from app.agent import AutoFixAgent, FailureInfo, run_auto_fix
    result = run_auto_fix(FailureInfo(
        file_path="testcases/test_login.py",
        case_name="test_login_success",
        error_log="AssertionError: ...",
    ))
"""
from .models import AgentIteration, AgentRunResult, FailureInfo, FixSuggestionBrief, ToolResult
from .agent import AutoFixAgent, auto_fix_case, run_auto_fix
from . import review  # noqa: F401  人工审核入口(应用/拒绝补丁)

# 导入即注册 FixSuggestion 到 Base.metadata 并幂等建表
# (确保即使 main.py 未显式导入 agent,表也已就绪)
from . import db  # noqa: F401,E402  (须在 agent 之后导入,依赖链:agent → tools → db)

__all__ = [
    "AutoFixAgent",
    "auto_fix_case",
    "run_auto_fix",
    "review",
    "FailureInfo",
    "ToolResult",
    "AgentIteration",
    "AgentRunResult",
    "FixSuggestionBrief",
    "db",
]
