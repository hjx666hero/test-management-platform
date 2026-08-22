"""通义千问 AI 根因分析服务。

流程(与用户需求一致):
    1. 解析任务 allure-results 目录拿到失败用例清单(allure_parser);
    2. 从 DB 取该失败用例的请求/响应快照;
    3. 组装 Prompt 调通义千问(OpenAI 兼容接口);
    4. 将根因分析文本写入 test_case_results.ai_analysis,标记 ai_status。

未配置 DASHSCOPE_API_KEY 时自动跳过(AI 失败不影响任务主流程)。
"""
import json
import logging

import requests

from .. import config
from ..database import SessionLocal
from ..models import TestCaseResult
from .allure_parser import parse_failures

logger = logging.getLogger("tms.ai_analyzer")

# 入口随 config 走(公共 DashScope 或专属 MaaS 实例),与 Agent 共用同一模型配置
DASHSCOPE_URL = f"{config.AI_BASE_URL.rstrip('/')}/chat/completions"


def _call_qwen(request_snapshot: dict, response_snapshot: dict, error_message: str) -> str:
    """调用通义千问,返回根因分析文本。"""
    prompt = (
        "你是资深接口测试工程师。下面是一条接口自动化用例失败信息,"
        "请给出简洁的根因分析(200字内):可能原因、建议修复方向。\n\n"
        f"【请求】\n{json.dumps(request_snapshot, ensure_ascii=False, indent=2)}\n\n"
        f"【响应】\n{json.dumps(response_snapshot, ensure_ascii=False, indent=2)}\n\n"
        f"【断言/异常】\n{error_message}\n"
    )
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    resp = requests.post(DASHSCOPE_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def analyze_task_failures(task_id: int) -> None:
    """对任务的所有失败用例做 AI 根因分析并落库。"""
    if not config.AI_ENABLED:
        logger.info("未配置 DASHSCOPE_API_KEY,跳过 AI 根因分析")
        return

    failures = parse_failures(task_id)
    if not failures:
        return

    db = SessionLocal()
    try:
        for fail in failures:
            # 匹配同任务下同名的失败用例结果
            row = (
                db.query(TestCaseResult)
                .filter(
                    TestCaseResult.task_id == task_id,
                    TestCaseResult.case_name == fail["name"],
                    TestCaseResult.status == "failed",
                )
                .order_by(TestCaseResult.id.desc())
                .first()
            )
            if not row:
                continue

            row.ai_status = "pending"
            db.commit()
            try:
                analysis = _call_qwen(
                    row.request_snapshot or {},
                    row.response_snapshot or {},
                    row.error_message or fail["message"],
                )
                row.ai_analysis = analysis
                row.ai_status = "done"
                logger.info("AI 分析完成: %s", fail["name"])
            except Exception as exc:  # noqa: BLE001 AI 单条失败不影响其他
                logger.exception("AI 分析失败: %s", fail["name"])
                row.ai_status = "failed"
            db.commit()
    finally:
        db.close()
