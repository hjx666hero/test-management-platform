# -*- coding: utf-8 -*-
"""TMS Auto-Fix Agent 部署验收脚本(独立运行,按顺序检查 6 项)。

用法(在 backend 目录下):
    python check_integration.py

检查项(任一失败立即红字报错并退出码 1):
  1. 数据库表检查    —— EvalCase / AgentTrajectory / AgentCost 三张表已建且有数据(或表结构正确)
  2. 评估脚本冒烟    —— 运行 run_eval.py 跑 1 条场景,确认生成 eval_report_check.json 且含 success_rate
                        (会真实调用通义千问,约 1-2 分钟,需 DASHSCOPE_API_KEY)
  3. 文件锁模拟      —— 两线程并发"应用补丁",验证互斥排队、互不崩溃
  4. 对话接口测试    —— POST /api/agent/ask 传假 patch_id,预期 404(非 500 即通过)
  5. RAG 目录检查    —— chroma_data/ 向量库目录存在
  6. 成本看板检查    —— GET /api/agent/stats 返回运行次数与总花费字段

依赖:仅后端已装依赖(sqlalchemy/fastapi/TestClient)——零新增安装。
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ==================== 初始化:自举 backend 路径 + 彩色输出 ====================

BASE = Path(__file__).resolve().parent          # backend/
sys.path.insert(0, str(BASE))
os.chdir(BASE)                                   # 便于读 .env 与固定报告路径

# Windows 控制台启用 ANSI 颜色 + 防 emoji 乱码(GBK 控制台)
os.system("")
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

PASS = f"{GREEN}✅ PASS{RESET}"
FAIL = f"{RED}❌ FAIL{RESET}"


def die(msg: str) -> None:
    """单项失败:红字报错并立即退出(约束:有一项失败就报错退出)。"""
    print(f"\n{RED}{'=' * 60}{RESET}")
    print(f"{RED}验收失败:{msg}{RESET}")
    print(f"{RED}{'=' * 60}{RESET}")
    sys.exit(1)


# ==================== 检查 1:数据库表 ====================

def check_tables() -> None:
    print("[1/6] 数据库表检查(EvalCase / AgentTrajectory / AgentCost)")
    from sqlalchemy import text

    from app.database import engine

    # 三张表的核心列(用于"无数据但结构正确"的降级判定)
    core_cols = {
        "eval_cases": {"id", "error_type", "failure_log", "expected_patch"},
        "agent_trajectories": {"id", "trace_id", "round_num", "thought", "tool_result"},
        "agent_costs": {"id", "trace_id", "input_tokens", "output_tokens", "cost_rmb"},
    }
    details = []
    with engine.connect() as conn:
        for table, need_cols in core_cols.items():
            try:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            except Exception as exc:  # noqa: BLE001 表不存在
                die(f"表 {table} 查询失败(可能未建表): {exc}")
            if count and count > 0:
                details.append(f"{table}={count} 条")
                continue
            # 0 条数据:降级检查表结构(information_schema)
            cols = {
                row[0] for row in conn.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                    ),
                    {"t": table},
                )
            }
            if not cols:
                die(f"表 {table} 不存在(且无数据)")
            missing = need_cols - cols
            if missing:
                die(f"表 {table} 结构不正确,缺列: {sorted(missing)}")
            details.append(f"{table}=0 条(表结构正确)")
    print(f"      {'; '.join(details)}")
    print(f"      {PASS}")


# ==================== 检查 2:评估脚本冒烟 ====================

def check_eval_smoke() -> None:
    print("[2/6] 评估脚本冒烟(run_eval.py 跑 1 条场景,约 1-2 分钟)")
    report = BASE / "eval_report_check.json"
    if report.is_file():
        report.unlink()  # 清掉旧报告,确保本次新鲜生成

    cmd = [sys.executable, "cli/run_eval.py", "--limit", "1", "--output", str(report)]
    try:
        proc = subprocess.run(
            cmd, cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=420,
        )
    except subprocess.TimeoutExpired:
        die("run_eval.py 执行超过 420 秒未结束(评估链路卡死)")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-8:])
        die(f"run_eval.py 退出码 {proc.returncode}\n------ 输出末尾 ------\n{tail}")
    if not report.is_file():
        die("run_eval.py 正常退出但未生成 eval_report_check.json")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"eval_report_check.json 不是合法 JSON: {exc}")

    summary = data.get("summary") or {}
    if "success_rate" not in summary:
        die(f"报告中缺少 summary.success_rate 字段,实际顶层键: {list(data.keys())}")
    rate = summary.get("success_rate")
    print(f"      报告已生成,success_rate = {rate},共执行 {summary.get('executed')} 条")
    print(f"      {PASS}")


# ==================== 检查 3:文件锁模拟(两线程并发应用补丁) ====================

def check_file_lock() -> None:
    print("[3/6] 文件锁模拟(两线程并发 apply_patch,预期排队不崩溃)")
    from app import config as app_config
    from app.agent import db as agent_db
    from app.agent.tools import LOCK_TIMEOUT, _file_lock, run_pytest

    # 构造"无操作"补丁:original == fixed(整文件内容必唯一),替换无实际效果
    target = Path(app_config.PYTEST_FRAMEWORK_PATH) / "testcases" / "test_tags.py"
    if not target.is_file():
        die(f"文件锁测试目标不存在: {target}")
    content = target.read_text(encoding="utf-8")
    patch_id = agent_db.save_fix_suggestion(
        case_name="integration-check-lock",
        file_path="testcases/test_tags.py",
        original_code=content,
        fixed_code=content,          # no-op:应用与还原都不改变文件
        diff="(no-op for lock check)",
        explanation="验收脚本构造的无操作补丁,仅用于并发文件锁验证",
    )

    # ---- 3a. 锁互斥验证:两线程抢同一把锁,持锁区间不得重叠 ----
    spans: list = []

    def lock_worker() -> None:
        lk = _file_lock(target)
        lk.acquire(timeout=LOCK_TIMEOUT)
        t0 = time.time()
        time.sleep(0.4)             # 模拟持锁干活
        spans.append((t0, time.time()))
        lk.release()

    t1 = threading.Thread(target=lock_worker)
    t2 = threading.Thread(target=lock_worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    if len(spans) != 2:
        die(f"锁互斥验证异常:两线程应各记录一次持锁区间,实际 {len(spans)} 次")
    (a0, a1), (b0, b1) = sorted(spans)
    overlap = a1 > b0               # 区间按起点排序后,前者结束晚于后者开始即重叠
    if overlap:
        die(f"文件锁未互斥!两线程持锁区间重叠: {spans}")
    print(f"      锁互斥:两线程持锁区间 {[(round(s, 2), round(e, 2)) for s, e in sorted(spans)]} 无重叠(排队生效)")

    # ---- 3b. 端到端并发:两线程同时 run_pytest(patch_id=...) ----
    # test_path 指向不存在的文件:pytest 快速失败,但"加锁→替换→运行→还原→释放"全流程真实走一遍
    outcomes: list = []

    def patch_worker() -> None:
        try:
            r = run_pytest(test_path="testcases/__no_such_check__.py", patch_id=patch_id)
            outcomes.append(("ok", r))
        except Exception as exc:  # noqa: BLE001 崩溃即验收失败
            outcomes.append(("exc", exc))

    t3 = threading.Thread(target=patch_worker)
    t4 = threading.Thread(target=patch_worker)
    t3.start(); t4.start(); t3.join(timeout=120); t4.join(timeout=120)
    crashes = [o for o in outcomes if o[0] == "exc"]
    if crashes or len(outcomes) != 2:
        die(f"并发应用补丁崩溃: {crashes or outcomes}")
    print(f"      并发 apply_patch ×2:均正常返回(工具 success={outcomes[0][1].get('success')}),无崩溃排队执行")
    print(f"      {PASS}")


# ==================== 检查 4:对话接口(假 patch_id) ====================

def check_ask_api() -> None:
    print("[4/6] 对话接口测试(POST /api/agent/ask,传假 patch_id)")
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post(
        "/api/agent/ask",
        json={"patch_id": 99999999, "question": "验收测试:为什么这样修改?"},
    )
    # 预期 404(补丁不存在)或 200;约束:只要不报 500 就算通过
    if r.status_code >= 500:
        die(f"/api/agent/ask 返回 {r.status_code}(服务端异常): {r.text[:200]}")
    detail = r.json().get("detail", "") if r.status_code != 200 else "(流式返回)"
    print(f"      HTTP {r.status_code} {detail}(<500 即通过,404=正确识别假补丁)")
    print(f"      {PASS}")


# ==================== 检查 5:RAG 目录 ====================

def check_rag_dir() -> None:
    print("[5/6] RAG 目录检查(chroma_data/ 本地向量库)")
    from app.agent.memory import CHROMA_DIR

    path = Path(CHROMA_DIR)
    if not path.is_dir():
        die(f"RAG 持久化目录不存在: {path}(预期修复成功至少一次后自动生成)")
    # 有实际内容更佳:chroma.sqlite3 或子目录
    has_content = any(path.iterdir())
    print(f"      目录存在: {path}{'(含数据)' if has_content else '(空目录,首次使用后写入)'}")
    print(f"      {PASS}")


# ==================== 检查 6:成本看板 ====================

def check_stats_api() -> None:
    print("[6/6] 成本看板检查(GET /api/agent/stats)")
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.get("/api/agent/stats")
    if r.status_code != 200:
        die(f"/api/agent/stats 返回 {r.status_code}: {r.text[:200]}")
    j = r.json()
    # 兼容字段名:实现为 runs/total_cost_rmb(等价于需求中的 today_runs/total_cost)
    has_runs = ("runs" in j) or ("today_runs" in j)
    has_cost = ("total_cost_rmb" in j) or ("total_cost" in j)
    if not (has_runs and has_cost):
        die(f"看板返回缺少运行次数/总花费字段,实际返回: {j}")
    print(f"      今日运行 {j.get('runs')} 次,总花费 ¥{j.get('total_cost_rmb')},平均耗时 {j.get('avg_duration_s')}s")
    print(f"      {PASS}")


# ==================== 主流程 ====================

def main() -> None:
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}TMS Auto-Fix Agent 部署验收{RESET}  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"说明:第 2 项会真实调用通义千问跑 1 条评估场景(约 1-2 分钟)\n")

    check_tables()
    check_eval_smoke()
    check_file_lock()
    check_ask_api()
    check_rag_dir()
    check_stats_api()

    print(f"\n{GREEN}{BOLD}{'=' * 60}{RESET}")
    print(f"{GREEN}{BOLD}验收通过:6/6 全部正常 ✅{RESET}")
    print(f"{GREEN}{BOLD}{'=' * 60}{RESET}")


if __name__ == "__main__":
    main()
