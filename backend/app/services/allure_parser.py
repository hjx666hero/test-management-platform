"""解析任务生成的 Allure result JSON,提取失败用例信息。

用户需求要求"通过代码解析 Allure 生成的 result 目录下的 JSON 文件,
提取失败用例信息"——本服务即该环节,与 AI 分析服务解耦:
解析器只负责"读 Allure JSON → 返回失败用例清单",
AI 服务负责"取失败用例的请求/响应快照 → 调通义千问 → 落库"。
"""
import json
import logging
from pathlib import Path

from .. import config

logger = logging.getLogger("tms.allure_parser")


def _task_allure_dir(task_id) -> Path:
    return config.TASKS_DIR / str(task_id) / "allure-results"


def parse_failures(task_id) -> list:
    """解析 task 的 allure-results 目录,返回失败用例列表。

    返回: [{name, status, message}] ,按 start 时间升序。
    """
    allure_dir = _task_allure_dir(task_id)
    failures = []
    if not allure_dir.is_dir():
        logger.warning("任务 %s 无 allure-results 目录", task_id)
        return failures

    for result_file in sorted(allure_dir.glob("*-result.json")):
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("解析 %s 失败: %s", result_file.name, exc)
            continue
        if data.get("status") == "failed":
            details = data.get("statusDetails", {}) or {}
            failures.append({
                "name": data.get("name", result_file.stem),
                "status": "failed",
                "message": details.get("message") or details.get("trace") or "",
            })

    # 按 start 升序,保证与用例执行顺序一致
    return failures
