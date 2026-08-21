"""核心执行器:subprocess 执行项目一 pytest-realworld-framework 的全部用例。

设计要点(对应平台价值):
1. 通过 subprocess 在项目一目录下运行真实 pytest(自动加载其 pytest.ini/
   conftest/数据驱动 YAML/fixture),TMS 任务执行的就是项目一真实用例
   (当前 38 条,含参数化),而非 TMS 自建的用例子集;
2. 任务环境 env_url 通过 BASE_URL 环境变量注入——项目一 config.py 从
   os.environ 读取且 load_dotenv 不覆盖已有变量,注入优先生效;
3. 用例标签映射为 pytest -m marker 表达式(前端 tags 与项目一 pytest.ini
   注册的 markers 一致:P0/P1/P2/articles/user/tags/profiles/comments/favorites);
4. 结果通过 --junitxml 落盘解析,逐条写 MySQL + 生成 Allure result JSON,
   供"报告解析 + 通义千问根因分析"使用。
"""
import logging
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from datetime import datetime
from pathlib import Path

from .. import config
from ..database import SessionLocal
from ..models import TestCaseResult, TestTask

logger = logging.getLogger("tms.executor")

# subprocess 执行 pytest 的超时(秒):全量 38 条含网络请求,留足余量
PYTEST_TIMEOUT = int(getattr(config, "TMS_PYTEST_TIMEOUT", "600"))


# ==================== pytest 命令构建与执行 ====================

def _build_pytest_cmd(tags: list, junit_path: Path) -> list:
    """组装 pytest 命令。

    - `-o addopts=`:清空项目一 pytest.ini 的 addopts(去掉 --reruns 失败重跑
      与 --alluredir,避免拖慢执行、污染项目一目录);
    - 标签过滤映射为 `-m "tag1 or tag2"`(marker 表达式);
    - `--junitxml`:结果输出到 TMS 任务目录,由 _parse_junit 解析落库。
    """
    cmd = [
        sys.executable, "-m", "pytest", "testcases/",
        "-o", "addopts=",          # 覆盖 ini 的 addopts(reruns/alluredir)
        f"--junitxml={junit_path}",
        "--no-header", "-q", "--tb=short",
    ]
    if tags:
        # ["articles","P0"] → "articles or P0"(命中任一 marker 即执行)
        expr = " or ".join(tags)
        cmd += ["-m", expr]
    return cmd


def _run_pytest(cmd: list, env_url: str, junit_path: Path) -> str:
    """在项目一目录下执行 pytest,返回合并后的控制台输出。

    env_url 通过 BASE_URL 环境变量注入,项目一的请求全部指向该环境。
    """
    env = {**os.environ, "BASE_URL": env_url}
    try:
        proc = subprocess.run(
            cmd,
            cwd=config.PYTEST_FRAMEWORK_PATH,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PYTEST_TIMEOUT,
        )
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"pytest 执行超时(>{PYTEST_TIMEOUT}s)")

    # junit 文件兜底校验:pytest 收集期错误(如语法错误)可能不生成该文件
    if not junit_path.is_file():
        raise RuntimeError(f"pytest 未生成结果文件,可能存在收集错误:\n{output[-2000:]}")
    return output


# ==================== junit xml 解析 ====================

def _parse_junit(junit_path: Path) -> list:
    """解析 junit xml,返回用例结果列表。

    返回: [{name, status, duration_ms, message}]。
    - name 形如 test_create_article[normal_title](参数化用例带数据 id 后缀,
      与 Agent 修复时填写的 case_name 一致);
    - duration_ms 取 junit 的 time 属性(秒 → 毫秒)。
    """
    root = ElementTree.parse(junit_path).getroot()
    # xunit1:根为 testsuites/testsuite;xunit2:根为 testsuite——统一找 testcase
    cases = []
    for tc in root.iter("testcase"):
        name = tc.attrib.get("name", "unknown")
        duration_ms = int(float(tc.attrib.get("time", "0")) * 1000)

        # 子元素判定结果:failure/error → failed;skipped → skipped;无子元素 → passed
        failure = tc.find("failure")
        error = tc.find("error")
        if failure is not None:
            # message 属性为断言摘要,text 为完整堆栈——拼接最有信息量
            message = (failure.attrib.get("message") or "") or (failure.text or "")
            status = "failed"
        elif error is not None:
            message = (error.attrib.get("message") or "") or (error.text or "")
            status = "failed"
        elif tc.find("skipped") is not None:
            message = "skipped"
            status = "passed"  # 跳过不计失败,保持平台 passed/failed 二态
        else:
            message = None
            status = "passed"

        cases.append({
            "name": name,
            "status": status,
            "duration_ms": duration_ms,
            "message": message[:2000] if message else None,
        })
    return cases


# ==================== Allure result JSON 生成 ====================

def _write_allure_result(task_dir, case_name, status, start_ms, end_ms, message=None):
    """按 Allure2 规范写一条 {uuid}-result.json,供报告解析/Allure CLI 使用。"""
    import uuid as _uuid

    allure_dir = task_dir / "allure-results"
    allure_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "uuid": str(_uuid.uuid4()),
        "name": case_name,
        "fullName": case_name,
        "status": status,  # passed / failed
        "start": int(start_ms),
        "stop": int(end_ms),
        "labels": [
            {"name": "framework", "value": "pytest"},
            {"name": "host", "value": "test-management-platform"},
        ],
    }
    if status == "failed" and message:
        result["statusDetails"] = {"message": message, "trace": message}
    with open(allure_dir / f"{result['uuid']}-result.json", "w", encoding="utf-8") as f:
        import json as _json

        _json.dump(result, f, ensure_ascii=False)


# ==================== 任务执行主流程 ====================

def run_task(task_id: int) -> None:
    """后台执行任务:subprocess 跑项目一 pytest → 解析 junit → 落库 + 写 Allure JSON。"""
    db = SessionLocal()
    try:
        task = db.get(TestTask, task_id)
        if not task:
            logger.error("任务不存在: %s", task_id)
            return

        task.status = "running"
        task.error = None
        task.created_at = datetime.now()
        db.commit()

        started = time.time()
        try:
            # 任务结果目录:<TASKS_DIR>/<task_id>/(junit.xml + allure-results/)
            task_dir = config.TASKS_DIR / str(task.id)
            task_dir.mkdir(parents=True, exist_ok=True)
            junit_path = task_dir / "junit.xml"

            cmd = _build_pytest_cmd(task.tags or [], junit_path)
            logger.info("任务 %s 开始执行: %s", task_id, " ".join(cmd))
            output = _run_pytest(cmd, task.env_url, junit_path)
            logger.info("任务 %s pytest 输出摘要:\n%s", task_id, output[-800:])

            # 解析 junit → 逐条落库 + Allure JSON
            cases = _parse_junit(junit_path)
            passed = failed = 0
            for case in cases:
                _write_result(
                    db, task, case["name"], case["status"],
                    case["duration_ms"], case["message"],
                )
                if case["status"] == "passed":
                    passed += 1
                else:
                    failed += 1

            task.total = passed + failed
            task.passed = passed
            task.failed = failed
            task.status = "success" if failed == 0 else "failed"
            logger.info("任务 %s 完成: %s/%s 通过, %s 失败", task_id, passed, task.total, failed)
        except Exception as exc:  # noqa: BLE001 环境/执行级错误
            logger.exception("任务执行异常")
            task.status = "failed"
            task.error = str(exc)[:2000]

        task.duration_s = int(time.time() - started)
        task.finished_at = datetime.now()
        db.commit()
        final_status = task.status  # 在 session 关闭前取出,避免 DetachedInstanceError
    finally:
        db.close()

    # 执行完成后,异步触发 AI 根因分析(独立会话,避免长事务)
    if final_status == "failed":
        from .ai_analyzer import analyze_task_failures
        try:
            analyze_task_failures(task_id)
        except Exception:  # noqa: BLE001 AI 失败不影响主流程
            logger.exception("AI 分析失败")


def _write_result(db, task, case_name, status, duration_ms, error_message):
    """写一条用例结果到 DB,并同步生成 Allure result JSON。

    subprocess 执行模式下无进程内请求/响应快照(快照字段留空,
    AI 根因分析主要依赖 error_message,ai_analyzer 已对空快照容错)。
    """
    row = TestCaseResult(
        task_id=task.id,
        case_name=case_name,
        status=status,
        duration_ms=duration_ms,
        error_message=error_message,
        request_snapshot=None,
        response_snapshot=None,
        ai_status="pending" if status == "failed" else "none",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # 同步生成 Allure result JSON(失败用例供 AI 服务解析)
    task_dir = config.TASKS_DIR / str(task.id)
    now_ms = int(datetime.now().timestamp() * 1000)
    _write_allure_result(task_dir, case_name, status, now_ms - duration_ms, now_ms, error_message)
    return row
