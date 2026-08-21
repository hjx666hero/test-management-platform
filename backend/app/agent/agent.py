"""Auto-Fix Agent 核心:ReAct(Reasoning + Acting)模式的失败用例自动修复。

运行时序(最多 config.MAX_ITERATIONS=3 轮):

    ┌─────────────────────────────────────────────────────────┐
    │ 1. Reasoning:LLM 分析当前上下文,输出思考 + tool_calls    │
    │ 2. Acting:   Agent 解析 tool_calls → execute_tool 执行   │
    │ 3. Observation:工具结果以 role=tool 消息回填对话历史      │
    └──────────────────────┬──────────────────────────────────┘
                           │ LLM 不再调用工具 → 输出最终答案,循环结束

产物与安全约束:
- 修复建议(unified diff + 理由)全部入库,状态 pending_review;
- 全程不修改被测源码(补丁验证为"临时替换 + finally 还原");
- 每轮轨迹(思考/动作/观察)记录在 AgentRunResult.iterations,可审计。

LLM 接入:通义千问 DashScope 的 OpenAI 兼容模式(openai SDK 指定 base_url),
工具调用遵循 OpenAI function calling 规范。
"""
import json
import logging

from . import db, tools
from .config import (
    AGENT_ENABLED,
    AI_MODEL,
    DASHSCOPE_API_KEY,
    DASHSCOPE_BASE_URL,
    MAX_ITERATIONS,
    REQUEST_TIMEOUT,
    TEMPERATURE,
)
from .models import (
    AgentIteration,
    AgentRunResult,
    FailureInfo,
    FixSuggestionBrief,
    ToolResult,
)

logger = logging.getLogger("tms.agent")

# System Prompt:定义 Agent 的角色、工作方式(ReAct)与修复原则
SYSTEM_PROMPT = """你是资深测试开发工程师,任务:自动修复失败的 pytest 接口测试用例。

工作方式(ReAct,最多 {max_iterations} 轮):
- 每轮先简述你的思考(根因判断/下一步计划),再决定调用哪些工具;
- 用工具收集证据(git diff 看变更、read_file 读源码、run_pytest 复现),不要凭空猜测;
- 证据充分后,调用 generate_patch 产出"最小且安全"的修复补丁;
- 生成补丁后,调用 run_pytest 并传 patch_id,临时应用补丁验证修复效果;
- 验证通过或确认无更多手段后,输出最终结论(根因 + 修复方案 + 验证结果),不再调用工具。

修复原则:
1. 最小改动:只改导致失败的必要部分,不做无关重构、不调整格式;
2. 安全:不删除用例、不弱化断言来"骗绿";若判断是被测系统 bug 而非用例问题,
   不改用例,明确说明并建议提交给开发;
3. generate_patch 的 original_code 必须先用 read_file 获取,保证与文件逐字符一致。

建议节奏:第 1 轮取证(git diff / read_file / run_pytest 复现),
第 2 轮生成补丁,第 3 轮验证并总结。"""


def _get_llm_client():
    """创建通义千问客户端(OpenAI 兼容模式)。

    - base_url 指向 DashScope 兼容入口,即可用 openai SDK 的
      chat.completions + tools(function calling)全套能力;
    - openai 为可选依赖:未安装/未配置 Key 时抛出带修复指引的 RuntimeError。
    """
    if not AGENT_ENABLED:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY,Agent 不可用(请在 backend/.env 中配置)")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("缺少 openai 依赖,请执行: pip install 'openai>=1.30'") from exc
    return OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL, timeout=REQUEST_TIMEOUT)


class AutoFixAgent:
    """失败用例自动修复 Agent(ReAct)。

    用法:
        result = AutoFixAgent(FailureInfo(
            file_path="testcases/test_login.py",
            case_name="test_login_success",
            error_log="AssertionError: 登录失败: 401 ...",
        )).run()
    """

    def __init__(self, failure: FailureInfo):
        self.failure = failure
        self.messages: list = []        # 完整对话历史(system/user/assistant/tool)
        self.iterations: list = []      # ReAct 轨迹(AgentIteration)
        self.suggestion_ids: list = []  # 本次运行产出的补丁 ID(汇总用)

    # ==================== 对话组装 ====================

    def _build_messages(self) -> None:
        """组装初始消息:system(角色与规则) + user(失败用例上下文)。"""
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(max_iterations=MAX_ITERATIONS)},
            {
                "role": "user",
                "content": (
                    "请修复以下失败用例:\n"
                    f"【用例名】{self.failure.case_name}\n"
                    f"【文件】{self.failure.file_path}\n"
                    f"【错误日志】\n{self.failure.error_log}\n"
                ),
            },
        ]

    # ==================== ReAct 主循环 ====================

    def run(self) -> AgentRunResult:
        """执行 ReAct 循环,返回运行结果(补丁已入库 pending_review)。"""
        # 前置检查:未配 Key / 缺依赖时快速失败,不进入循环
        if not AGENT_ENABLED:
            return self._result(success=False, error="未配置 DASHSCOPE_API_KEY,Agent 已禁用")
        try:
            client = _get_llm_client()
        except RuntimeError as exc:
            return self._result(success=False, error=str(exc))

        self._build_messages()
        final_answer = None

        # 最多 MAX_ITERATIONS 轮"思考-行动";for...else:循环跑满未 break 时兜底
        for round_no in range(1, MAX_ITERATIONS + 1):
            # ---- 1. Reasoning:调 LLM 决策本轮动作 ----
            try:
                resp = client.chat.completions.create(
                    model=AI_MODEL,
                    messages=self.messages,
                    tools=tools.OPENAI_TOOLS,   # OpenAI function calling 规范的 Schema
                    temperature=TEMPERATURE,
                )
                msg = resp.choices[0].message
            except Exception as exc:  # noqa: BLE001 网络/限流/超时等
                logger.exception("LLM 调用失败(第 %s 轮)", round_no)
                return self._result(success=False, error=f"LLM 调用失败: {exc}", final_answer=final_answer)

            # ---- 无 tool_calls:模型给出最终答案,ReAct 结束 ----
            if not msg.tool_calls:
                final_answer = msg.content
                self.iterations.append(AgentIteration(
                    index=round_no, thought=msg.content, action=None, observation=None,
                ))
                break

            # ---- 2. Acting:记录 assistant 消息(含 tool_calls)并逐个执行工具 ----
            # 手动构造 dict 而非直接 append msg 对象:显式、无 SDK 版本兼容问题
            self.messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            })

            actions, observations = [], []
            for tc in msg.tool_calls:
                result = self._execute_tool_call(tc)
                actions.append(result.tool)
                observations.append(f"[{result.tool}] {'OK' if result.success else 'FAIL'}: {result.output[:200]}")

                # ---- 3. Observation:工具结果以 role=tool 回填(tool_call_id 必须对应) ----
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"tool": result.tool, "success": result.success, "output": result.output},
                        ensure_ascii=False,
                    ),
                })

            self.iterations.append(AgentIteration(
                index=round_no,
                thought=msg.content,
                action=", ".join(actions),
                observation="\n".join(observations),
            ))
        else:
            # 跑满轮数仍无最终答案:兜底说明(补丁若已生成仍可在 DB 中查看)
            final_answer = "已达最大迭代次数,过程记录见 iterations;已生成的补丁建议见 suggestions。"

        logger.info(
            "Auto-Fix 完成: case=%s 补丁=%s 轮数=%s",
            self.failure.case_name, self.suggestion_ids, len(self.iterations),
        )
        return self._result(success=bool(self.suggestion_ids), final_answer=final_answer)

    # ==================== 单个工具调用执行 ====================

    def _execute_tool_call(self, tc) -> ToolResult:
        """解析并执行一次工具调用。

        对 generate_patch 额外注入失败上下文(case_name/task_id/result_id):
        这些字段用于补丁入库追溯,LLM 无需也不应关心。
        """
        name = tc.function.name
        try:
            arguments = json.loads(tc.function.arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("arguments 必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            return ToolResult(tool=name, success=False, output=f"工具参数不是合法 JSON 对象: {exc}")

        if name == "generate_patch":
            arguments.setdefault("case_name", self.failure.case_name)
            arguments.setdefault("task_id", self.failure.task_id)
            arguments.setdefault("result_id", self.failure.result_id)

        result = tools.execute_tool(name, arguments)

        # 收集补丁 ID 供结果汇总(verify 状态由 run_pytest 回写 DB)
        if name == "generate_patch" and result.success and result.data and "patch_id" in result.data:
            self.suggestion_ids.append(result.data["patch_id"])
        return result

    # ==================== 结果汇总 ====================

    def _result(self, success: bool, final_answer: str = None, error: str = None) -> AgentRunResult:
        """汇总运行结果:补丁明细从 DB 反查,verified 取最近一次验证结论。"""
        suggestions, verified = [], None
        for patch_id in self.suggestion_ids:
            row = db.get_suggestion(patch_id)
            if not row:
                continue
            suggestions.append(FixSuggestionBrief(
                id=row["id"],
                case_name=row["case_name"],
                file_path=row["file_path"],
                diff=row["diff"],
                explanation=row["explanation"],
                status=row["status"],
                verified=row["verified"],
                created_at=row["created_at"],
            ))
            if row["verified"] is not None:
                verified = row["verified"]

        return AgentRunResult(
            case_name=self.failure.case_name,
            file_path=self.failure.file_path,
            success=success and error is None,
            verified=verified,
            iterations=self.iterations,
            suggestions=suggestions,
            final_answer=final_answer,
            error=error,
        )


# ==================== 对外入口 ====================

def run_auto_fix(failure: FailureInfo) -> AgentRunResult:
    """程序化同步入口:执行一次 Auto-Fix 并返回完整结果。"""
    return AutoFixAgent(failure).run()


def auto_fix_case(
    task_id: int,
    result_id: int,
    case_name: str,
    file_path: str,
    error_log: str,
) -> None:
    """FastAPI BackgroundTasks 入口(与平台 run_task 用法一致,无返回值)。

    在路由中的接入示例:

        from app.agent import auto_fix_case

        @router.post("/{task_id}/results/{result_id}/auto-fix")
        def trigger_auto_fix(task_id: int, result_id: int, background: BackgroundTasks, ...):
            background.add_task(
                auto_fix_case,
                task_id=task_id, result_id=result_id,
                case_name=row.case_name, file_path="testcases/test_login.py",
                error_log=row.error_message or "",
            )

    修复建议与验证结论均已落库(fix_suggestions 表),前端可轮询查询。
    """
    result = AutoFixAgent(FailureInfo(
        task_id=task_id,
        result_id=result_id,
        case_name=case_name,
        file_path=file_path,
        error_log=error_log or "",
    )).run()
    logger.info(
        "auto_fix_case 完成: task=%s case=%s success=%s patches=%s verified=%s",
        task_id, case_name, result.success,
        [s.id for s in result.suggestions], result.verified,
    )
