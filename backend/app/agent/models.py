"""Agent 模块 Pydantic 数据模型定义。

三类模型:
1. FailureInfo     —— Agent 的输入:一条失败用例的上下文(文件/用例名/日志)
2. ToolResult      —— 工具执行的标准化返回(ReAct 里的 Observation,喂回给 LLM)
3. AgentRunResult  —— 一次 Agent 运行的最终产出(思考轨迹/补丁清单/结论)
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ==================== 1. 输入:失败用例信息 ====================

class FailureInfo(BaseModel):
    """失败用例信息(Agent 的唯一输入)。"""

    file_path: str = Field(..., description="失败用例所在文件,相对项目一根目录(如 testcases/test_login.py)或绝对路径")
    case_name: str = Field(..., description="用例名(pytest 用例函数名,或 TMS 用例注册表中的中文名)")
    error_log: str = Field(..., description="失败日志(断言信息/异常堆栈)")
    task_id: Optional[int] = Field(None, description="来源任务 ID,便于补丁记录追溯(可选)")
    result_id: Optional[int] = Field(None, description="来源用例结果 ID(可选)")
    eval_case_id: Optional[int] = Field(None, description="评估场景 ID(评估运行时回填,便于轨迹关联)")


# ==================== 2. 工具执行结果(Observation) ====================

class ToolResult(BaseModel):
    """单个工具的一次执行结果。

    注意区分两个概念:
    - success: 工具调用本身是否正常完成(网络/参数错误为 False)
    - output 中的内容: 即使工具正常执行,测试仍可能失败——失败输出对 LLM 同样是有价值的信息
    """

    tool: str = Field(..., description="工具名")
    success: bool = Field(..., description="工具调用是否正常完成")
    output: str = Field("", description="工具输出(已截断,将作为 Observation 喂回 LLM)")
    data: Optional[dict] = Field(None, description="附加结构化数据(如 generate_patch 返回的 patch_id)")


# ==================== 3. Agent 运行过程与产出 ====================

class AgentIteration(BaseModel):
    """ReAct 的一轮记录:Thought(思考) → Action(工具) → Observation(观察)。"""

    index: int = Field(..., description="轮次,从 1 开始")
    thought: Optional[str] = Field(None, description="本轮 LLM 的思考内容(assistant 消息)")
    action: Optional[str] = Field(None, description="本轮调用的工具名(多个用逗号分隔)")
    observation: Optional[str] = Field(None, description="工具返回摘要")


class FixSuggestionBrief(BaseModel):
    """修复建议摘要(给前端/API 展示用,完整内容见 fix_suggestions 表)。"""

    id: int
    case_name: str
    file_path: str
    diff: str = Field(..., description="unified diff 格式补丁")
    explanation: str = Field(..., description="AI 修复理由")
    status: str = Field(..., description="pending_review / applied / rejected")
    verified: Optional[bool] = Field(None, description="补丁验证结果(None=未验证)")
    created_at: Optional[datetime] = None


class AgentRunResult(BaseModel):
    """一次 Auto-Fix Agent 运行的完整结果。"""

    case_name: str
    file_path: str
    trace_id: Optional[str] = Field(None, description="本次运行唯一标识(关联 agent_trajectories 表,可回放全过程)")
    success: bool = Field(..., description="是否产出至少一个修复补丁")
    verified: Optional[bool] = Field(None, description="补丁验证结果(None=Agent 未做验证)")
    iterations: list[AgentIteration] = Field(default_factory=list, description="ReAct 思考-行动轨迹")
    suggestions: list[FixSuggestionBrief] = Field(default_factory=list, description="生成的修复建议清单")
    final_answer: Optional[str] = Field(None, description="Agent 最终结论(根因+修复方案)")
    error: Optional[str] = Field(None, description="运行级错误(Agent 未正常跑完时)")
