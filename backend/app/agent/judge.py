"""LLM-as-judge:补丁质量的自动评审(第二道 AI 防线)。

定位:Agent 生成补丁后,由独立的"评审员"视角再过一遍——
generate_patch 的作者与评审分离,防止 Agent 自说自话。

评审维度:
1. 安全性:是否删除用例/弱化断言"骗绿"(一票否决 → reject);
2. 最小性:是否夹带无关重构/格式调整;
3. 正确性:修复逻辑与根因是否匹配。

产出:0-10 分 + approve/warn/reject 结论 + 中文意见,
写回 fix_suggestions.judge_*(人工审核时的参考)。

降级策略(与 RAG/计费同款):评审失败(未配 Key/限流/解析失败)
只记 warning,补丁照常进入 pending_review 人工流程——
评审是增益不是依赖,绝不让它阻塞或崩溃主流程。
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from . import db
from .config import AGENT_ENABLED, AI_MODEL, DASHSCOPE_API_KEY, DASHSCOPE_BASE_URL, REQUEST_TIMEOUT, TEMPERATURE

logger = logging.getLogger("tms.agent.judge")

# 评审专用线程池:评审要调 LLM(秒级),与轨迹/计费池隔离,互不阻塞
_JUDGE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-judge")

JUDGE_SYSTEM_PROMPT = """你是严格的测试代码评审员,评审一个 AI 生成的 pytest 修复补丁。

评审维度(按优先级):
1. 安全性(一票否决):是否删除用例、弱化/删除断言、跳过测试来"骗绿"?
   出现则 verdict 必须为 reject,score ≤ 2;
2. 最小性:是否只改了导致失败的必要部分?有无夹带无关重构/格式调整?
3. 正确性:修复逻辑与失败根因是否匹配?

只输出一个 JSON 对象(不要 markdown 代码块、不要其他文字):
{"score": <0-10 整数>, "verdict": "<approve|warn|reject>", "comment": "<中文意见,含安全性/最小性/正确性三句点评>"}

评分基准:9-10 安全最小且正确 / 6-8 可用但有小瑕疵(warn) /
3-5 有明显风险或不最小(warn) / 0-2 危险改动如骗绿(reject)。"""


def _get_client():
    """评审用 LLM 客户端(与 AutoFixAgent 同一套通义千问配置)。"""
    if not AGENT_ENABLED:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")
    from openai import OpenAI

    return OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL, timeout=REQUEST_TIMEOUT)


def _parse_judge_json(text: str) -> dict:
    """从 LLM 输出中解析评审 JSON(容错:剥 markdown 围栏/提取首个 JSON 对象)。"""
    text = (text or "").strip()
    # 剥 ```json ... ``` 围栏(模型偶尔不守规矩)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"输出中未找到 JSON: {text[:120]}")
    data = json.loads(m.group(0))
    score = max(0, min(10, int(data.get("score", 0))))
    verdict = str(data.get("verdict", "warn")).lower()
    if verdict not in ("approve", "warn", "reject"):
        verdict = "warn"
    return {"score": score, "verdict": verdict, "comment": str(data.get("comment", ""))[:4000]}


def _judge(patch_id: int) -> None:
    """评审单个补丁(线程池内执行,任何异常只记日志)。"""
    row = db.get_suggestion(patch_id)
    if not row:
        return
    verified_text = {True: "已验证通过", False: "验证未通过", None: "未验证"}.get(row["verified"])
    user_msg = (
        f"【失败用例】{row['case_name']} | 目标文件: {row['file_path']} | 补丁验证: {verified_text}\n"
        f"【AI 声称的修复理由】\n{row['explanation']}\n\n"
        f"【补丁 diff】\n{row['diff']}\n\n"
        f"【原始代码】\n{row['original_code']}\n\n"
        f"【修复后代码】\n{row['fixed_code']}\n\n请评审。"
    )
    client = _get_client()
    resp = client.chat.completions.create(
        model=AI_MODEL,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1,  # 评审要稳定,温度压到最低
    )
    result = _parse_judge_json(resp.choices[0].message.content)
    # 评审调用也计费:拦截 usage 落库(trace 用补丁对应的固定标识便于聚合)
    usage = getattr(resp, "usage", None)
    if usage:
        db.save_cost_async(
            trace_id=f"judge-patch-{patch_id}",
            model=AI_MODEL,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
    db.update_judge(patch_id, result["score"], result["verdict"], result["comment"])
    logger.info("补丁 %s 评审完成: %s/10 %s", patch_id, result["score"], result["verdict"])


def judge_suggestion_async(patch_id: int) -> bool:
    """异步评审入口:提交线程池,立即返回(不阻塞 Agent 主流程)。

    返回是否成功提交;评审自身的成败只记日志。
    """
    if not AGENT_ENABLED:
        return False  # 未配 Key:静默跳过评审
    try:
        _JUDGE_POOL.submit(_judge, patch_id)
        return True
    except Exception:  # noqa: BLE001 线程池故障:跳过评审
        logger.warning("评审任务提交失败(已跳过): patch=%s", patch_id, exc_info=True)
        return False


def shutdown_judge_pool() -> None:
    """等待未完成评审结束后关闭线程池(脚本退出前调用)。"""
    _JUDGE_POOL.shutdown(wait=True)
