"""Agent 人机对话 API:针对修复建议的流式问答。

端点:
    POST /api/agent/ask   输入 patch_id + question → 基于该补丁的 ReAct 轨迹
                          (AgentTrajectory)+ 补丁内容拼装 Prompt 调通义千问,
                          以 StreamingResponse 流式返回解释。

设计要点:
1. 上下文 = 补丁本身(diff/理由/验证结论) + 产出该补丁那次运行的完整
   思考轨迹(每轮思考/工具结果),回答有据可依,不编造;
2. 流式输出:openai SDK stream=True 逐 chunk 透传,前端可打字机式展示;
3. 轨迹在 DB 查询完成后才开始流式生成——生成器内不再碰 DB,
   避免长连接占用会话。
"""
import json
import logging
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import schemas
from ..agent import db as agent_db
from ..agent.agent import _get_llm_client
from ..agent.config import AGENT_ENABLED, AI_MODEL, TEMPERATURE

logger = logging.getLogger("tms.agent.ask")
router = APIRouter(prefix="/api/agent", tags=["agent"])

# 轨迹中代表"运行已结束"的阶段标记(出现任一即推送 [DONE] 并关闭流)
_TERMINAL_MARKS = ("[最终结论]", "[早停]", "[轮次用尽]", "[LLM调用失败]")
# SSE 总时长上限(秒)与"轨迹一直不出现"的放弃阈值(秒)
_SSE_MAX_SECONDS = 600
_SSE_FIRST_WAIT_SECONDS = 60

# 轨迹上下文拼装上限(字符):防止多轮轨迹把 Prompt 撑爆
_MAX_TRAJ_CHARS = 12000
_MAX_DIFF_CHARS = 4000

ASK_SYSTEM_PROMPT = (
    "你是测试平台的 AI 修复助手。用户会针对某个自动修复补丁提问,"
    "请基于给定的「补丁信息」与「Agent 思考轨迹」用中文清晰回答:"
    "解释根因、修复思路、验证结论;引用轨迹时注明轮次。"
    "只依据给定材料作答,不要编造轨迹之外的事实。"
)


@router.get("/stats")
def agent_stats():
    """成本看板:今日运行次数、今日总花费(元)、平均耗时(秒)。

    统计自身故障(如 DB 不可用)时返回全 0 结构,不让看板白屏。
    """
    try:
        return agent_db.get_today_stats()
    except Exception:  # noqa: BLE001 看板属旁路,失败降级为零值
        logger.exception("/stats 统计失败,已降级返回零值")
        return {"date": None, "runs": 0, "total_cost_rmb": 0.0, "avg_duration_s": 0.0, "degraded": True}


@router.get("/trace/{trace_id}/events")
def stream_trace(trace_id: str):
    """SSE 实时思考流:推送该 trace 的 ReAct 轨迹(逐轮思考/工具结果)。

    实现:增量轮询 agent_trajectories(游标 after_id),新轨迹即时推送;
    遇到终止标记([最终结论]/[早停]/[轮次用尽]/[LLM调用失败])推 [DONE] 收尾。
    前端用 EventSource 订阅——比 5 秒轮询列表"看到补丁出现"的体验高一个层级:
    能看到 Agent 当轮在想什么、调了什么工具。
    """
    def _sse(data: str) -> str:
        return f"data: {data}\n\n"

    def event_gen():
        cursor = 0
        started = time.time()
        saw_any = False
        try:
            while time.time() - started < _SSE_MAX_SECONDS:
                try:
                    rows = agent_db.list_trajectories_by_trace(trace_id, after_id=cursor)
                except Exception:  # noqa: BLE001 DB 抖动:等下一轮重试
                    logger.warning("SSE 轨迹查询失败(重试中): %s", trace_id, exc_info=True)
                    rows = []
                for r in rows:
                    saw_any = True
                    cursor = max(cursor, r["id"])
                    yield _sse(json.dumps({
                        "id": r["id"],
                        "round": r["round_num"],
                        "thought": r["thought"],
                        "tool_result": r["tool_result"],
                    }, ensure_ascii=False))
                    # 终止标记:运行已结束,收尾关闭
                    if any((r["thought"] or "").startswith(m) for m in _TERMINAL_MARKS):
                        yield _sse("[DONE]")
                        return
                # 迟迟等不到首条轨迹(任务没启动/trace 无效):放弃,避免连接悬挂
                if not saw_any and time.time() - started > _SSE_FIRST_WAIT_SECONDS:
                    yield _sse(json.dumps({"error": f"60 秒内未收到任何轨迹(trace={trace_id})"}, ensure_ascii=False))
                    yield _sse("[DONE]")
                    return
                time.sleep(2)  # 轨迹写入是异步线程池,2s 轮询足够"近实时"
            yield _sse("[DONE]")  # 总时长到限:兜底收尾
        finally:
            logger.info("SSE 思考流结束: %s(推送至轨迹 id=%s)", trace_id, cursor)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/ask")
def ask_agent(payload: schemas.AgentAskRequest):
    """针对补丁的流式问答:轨迹历史 + 补丁内容 → 通义千问 → 流式解释。"""
    if not AGENT_ENABLED:
        raise HTTPException(status_code=400, detail="未配置 DASHSCOPE_API_KEY,Agent 不可用")

    sug = agent_db.get_suggestion(payload.patch_id)
    if not sug:
        raise HTTPException(status_code=404, detail=f"修复建议不存在: {payload.patch_id}")

    # ---- 1. 拉取该补丁的 ReAct 轨迹(AgentTrajectory) ----
    trajs = agent_db.list_trajectories_by_patch(payload.patch_id)
    if trajs:
        lines = []
        for t in trajs:
            entry = f"第{t['round_num']}轮 | {t['thought'] or ''}"
            if t["tool_result"]:
                entry += f"\n  工具结果: {t['tool_result'][:500]}"
            lines.append(entry)
        traj_text = "\n".join(lines)[:_MAX_TRAJ_CHARS]
    else:
        traj_text = "(无轨迹记录,仅基于补丁内容回答)"

    # ---- 2. 拼装 Prompt(补丁信息 + 轨迹 + 用户问题) ----
    verified_text = {True: "验证通过(PASSED)", False: "验证未通过", None: "未验证"}.get(sug["verified"])
    user_msg = (
        f"【补丁信息】\n"
        f"补丁 #{sug['id']} | 用例: {sug['case_name']} | 文件: {sug['file_path']} | 验证: {verified_text}\n"
        f"修复理由: {sug['explanation']}\n"
        f"diff:\n{sug['diff'][:_MAX_DIFF_CHARS]}\n\n"
        f"【Agent 思考轨迹】\n{traj_text}\n\n"
        f"【用户问题】\n{payload.question}"
    )
    logger.info("/ask: patch=%s question=%r 轨迹 %s 条", payload.patch_id, payload.question[:50], len(trajs))

    # ---- 3. 流式调用通义千问,逐 chunk 透传 ----
    def _stream():
        try:
            client = _get_llm_client()
            stream = client.chat.completions.create(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": ASK_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=TEMPERATURE,
                stream=True,  # 流式:token 级返回
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as exc:  # noqa: BLE001 流中途失败:把错误作为尾段吐给前端
            logger.exception("/ask 流式生成失败: patch=%s", payload.patch_id)
            yield f"\n[生成失败] {exc}"

    return StreamingResponse(_stream(), media_type="text/plain; charset=utf-8")
