# -*- coding: utf-8 -*-
"""Auto-Fix Agent 评估脚本:循环执行 eval_cases,度量自动修复成功率,输出 eval_report.json。

用法(在 backend 目录下):
    python cli/run_eval.py                                   # 全量 60 条
    python cli/run_eval.py --type field_change --limit 5     # 只跑字段变更前 5 条
    python cli/run_eval.py --case-ids 1,16,31,46             # 指定场景
    python cli/run_eval.py --output reports/my_report.json   # 指定报告路径

评估方法(可注入类是"真实修复验证",非文本相似度):
1. 故障注入:把 inject_original(正确代码)替换为 inject_buggy(故障代码),
   Agent 面对的是真实复现的失败(与线上事故同构);
2. 调用 agent.auto_fix_case 跑 ReAct 循环(每轮思考/工具结果实时落 agent_trajectories);
3. 预检:注入后目标用例必须真实失败,否则该场景标记 skipped(不计入成功率);
4. 真值验证:取 Agent 首选补丁(优先 verified) → 真实应用到源文件 → 重跑目标用例
   → 通过才算修复成功(比"生成了补丁"严格得多);
5. 环境类(timeout/env_jitter):不注入;成功标准 = Agent 克制地未产出补丁
   (对环境问题乱改代码记为"误修复");
6. 全程 finally 备份回写,评估结束后源码零残留。

报告字段说明:
- status: success/failed/skipped/error
- trace_id: 关联 agent_trajectories,可回放该次运行的全部思考过程
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 脚本自举:定位 backend/ 为导入根与 cwd(便于读 .env)
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app import config as app_config  # noqa: E402
from app.agent import db as agent_db  # noqa: E402
from app.agent.agent import auto_fix_case  # noqa: E402
from app.agent.config import AGENT_ENABLED, AI_MODEL, MAX_ITERATIONS  # noqa: E402

# 这两类"应该修代码";另两类(timeout/env_jitter)"不应该改代码"
PATCH_EXPECTED_TYPES = {"field_change", "assertion_error"}
PYTEST_TIMEOUT = 240  # 单次目标用例执行超时(秒)


def _resolve(file_path: str):
    """把相对项目一的路径解析为绝对路径(限制在项目一目录内)。"""
    root = Path(app_config.PYTEST_FRAMEWORK_PATH).resolve()
    p = Path(file_path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    return p if p.is_relative_to(root) else None


def _pytest_args_for(case: dict) -> list:
    """把评估场景解析为 pytest 目标参数(关键:数据文件场景不能直接当节点跑)。

    - file_path 是 .py 测试文件 → node id:文件::用例名(精确执行);
    - file_path 是数据文件(.yaml/.yml/.json)→ 补丁落在数据上,真正的测试在
      testcases/ 下 → 用 -k "函数名 and 参数id" 匹配数据驱动的那一条
      (case_name 形如 TestArticles::test_create_article[normal_title])。
    """
    if case["file_path"].endswith(".py"):
        return [f"{case['file_path']}::{case['case_name']}"]
    cn = case["case_name"]
    func = cn.split("::")[-1].split("[")[0]                 # test_create_article
    param = cn.split("[")[-1].rstrip("]") if "[" in cn else ""  # normal_title
    expr = f"{func} and {param}" if param else func
    return ["testcases/", "-k", expr]


def _run_node(case: dict, timeout: int = PYTEST_TIMEOUT):
    """执行评估场景对应的 pytest 目标,返回 (是否通过, 输出摘要)。"""
    cmd = [
        sys.executable, "-m", "pytest", *_pytest_args_for(case),
        "-o", "addopts=", "--no-header", "-q", "--tb=line",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=app_config.PYTEST_FRAMEWORK_PATH,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return proc.returncode == 0, "\n".join(out.strip().splitlines()[-12:])
    except subprocess.TimeoutExpired:
        return False, f"pytest 执行超时(>{timeout}s)"


def _pick_suggestion(result):
    """取 Agent 首选补丁:优先自身验证通过的,否则取第一条。"""
    if not result.suggestions:
        return None
    for s in result.suggestions:
        if s.verified:
            return s
    return result.suggestions[0]


def _verify_patch(case: dict, result) -> tuple:
    """真值验证:真实应用 Agent 首选补丁 → 重跑目标用例 → 通过才算修复成功。

    应用/还原都在本函数内闭环(finally 备份回写),不影响外层注入还原。
    """
    sug = _pick_suggestion(result)
    if not sug:
        return False, "未生成任何补丁"
    row = agent_db.get_suggestion(sug.id)
    path = _resolve(row["file_path"])
    if path is None or not path.is_file():
        return False, f"补丁目标文件不可访问: {row['file_path']}"
    content = path.read_text(encoding="utf-8")
    if row["original_code"] not in content:
        return False, "补丁 original_code 与文件不匹配,无法应用"
    if content.count(row["original_code"]) > 1:
        return False, "补丁 original_code 在文件中不唯一,无法应用"

    backup = content
    path.write_text(content.replace(row["original_code"], row["fixed_code"], 1), encoding="utf-8")
    try:
        passed, out = _run_node(case)
        if passed:
            return True, f"补丁(patch_id={sug.id})应用后目标用例通过"
        return False, f"补丁(patch_id={sug.id})应用后目标用例仍失败: {out[-200:]}"
    finally:
        path.write_text(backup, encoding="utf-8")  # 验证后立即还原


def run_case(case: dict) -> dict:
    """执行单条评估场景,返回结果记录。"""
    rec = {
        "id": case["id"],
        "title": case["title"],
        "error_type": case["error_type"],
        "file_path": case["file_path"],
        "case_name": case["case_name"],
    }
    injectable = bool(case["inject_original"] and case["inject_buggy"])
    path = _resolve(case["file_path"]) if injectable else None
    backup = None

    # ---- 阶段 1:故障注入(仅可注入类) ----
    if injectable:
        if path is None or not path.is_file():
            return {**rec, "status": "skipped", "reason": f"目标文件不存在: {case['file_path']}"}
        content = path.read_text(encoding="utf-8")
        if content.count(case["inject_original"]) != 1:
            return {**rec, "status": "skipped", "reason": "注入锚点不存在或不唯一(源文件已变更,请重跑 init 预检)"}
        backup = content
        path.write_text(content.replace(case["inject_original"], case["inject_buggy"], 1), encoding="utf-8")

    try:
        # ---- 阶段 2:预检(注入必须让目标用例真实失败,否则场景无效) ----
        if injectable:
            pre_pass, pre_out = _run_node(case)
            if pre_pass:
                rec.update(status="skipped", reason="注入未生效(目标用例仍通过),场景无效")
                return rec

        # ---- 阶段 3:调用 Agent(auto_fix_case,轨迹实时落库) ----
        t0 = time.time()
        result = auto_fix_case(
            task_id=None,
            result_id=None,
            eval_case_id=case["id"],
            case_name=case["case_name"],
            file_path=case["file_path"],
            error_log=case["failure_log"],
        )
        rec["duration_s"] = round(time.time() - t0, 1)
        rec["trace_id"] = result.trace_id
        rec["agent_success"] = result.success
        rec["agent_verified"] = result.verified
        rec["suggestions"] = [{"id": s.id, "verified": s.verified} for s in result.suggestions]
        rec["final_answer"] = (result.final_answer or "")[:400]
        if result.error:
            rec["agent_error"] = result.error[:300]

        # ---- 阶段 4:评分 ----
        if case["error_type"] in PATCH_EXPECTED_TYPES:
            # 可修复类:补丁真实应用后目标用例通过才算成功
            ok, reason = _verify_patch(case, result)
            rec["status"] = "success" if ok else "failed"
            rec["reason"] = reason
        else:
            # 环境类:正确行为 = 不产出代码补丁(不乱改代码)
            if result.suggestions:
                rec["status"] = "failed"
                rec["reason"] = f"误修复:对环境类失败产出了 {len(result.suggestions)} 个代码补丁"
            else:
                rec["status"] = "success"
                rec["reason"] = "正确识别为环境问题,未修改代码"
    except Exception as exc:  # noqa: BLE001 单条异常不中断整体评估
        rec["status"] = "error"
        rec["reason"] = str(exc)[:300]
    finally:
        # ---- 阶段 5:无条件还原(评估对源码零残留) ----
        if backup is not None:
            path.write_text(backup, encoding="utf-8")
    return rec


def _summarize(records: list) -> dict:
    """汇总总体/分类成功率。skipped 不计入分母(场景无效,非 Agent 之过)。"""
    by_type = {}
    for r in records:
        d = by_type.setdefault(
            r["error_type"],
            {"total": 0, "success": 0, "failed": 0, "skipped": 0, "error": 0},
        )
        d["total"] += 1
        d[r.get("status", "error")] += 1
    for d in by_type.values():
        executed = d["total"] - d["skipped"]
        d["success_rate"] = round(d["success"] / executed, 4) if executed else None

    total = len(records)
    skipped = sum(d["skipped"] for d in by_type.values())
    success = sum(d["success"] for d in by_type.values())
    executed = total - skipped
    return {
        "total": total,
        "executed": executed,
        "success": success,
        "success_rate": round(success / executed, 4) if executed else None,
        "by_type": by_type,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-Fix Agent 评估")
    parser.add_argument("--type", dest="error_type",
                        choices=["field_change", "timeout", "assertion_error", "env_jitter"],
                        help="只评估指定类型")
    parser.add_argument("--limit", type=int, help="最多执行 N 条")
    parser.add_argument("--case-ids", type=str, help="逗号分隔的场景 ID,如 1,16,31,46")
    parser.add_argument("--output", default="eval_report.json", help="报告输出路径")
    args = parser.parse_args()

    if not AGENT_ENABLED:
        print("[错误] 未配置 DASHSCOPE_API_KEY,无法运行评估")
        sys.exit(1)

    cases = agent_db.list_eval_cases(args.error_type)
    if args.case_ids:
        wanted = {int(x) for x in args.case_ids.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("[错误] 没有可执行的评估场景(先运行 python cli/init_eval_data.py)")
        sys.exit(1)

    print(f"待评估: {len(cases)} 条 | 模型: {AI_MODEL} | 最大轮数: {MAX_ITERATIONS}")
    started_at = datetime.now()
    t0 = time.time()
    records = []

    try:
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] #{case['id']} [{case['error_type']}] {case['title']}")
            rec = run_case(case)
            records.append(rec)
            print(f"    -> {rec.get('status')}: {rec.get('reason', '')[:120]}")
    except KeyboardInterrupt:
        print("\n[中断] 已完成部分场景,仍输出报告")
    finally:
        agent_db.shutdown_trajectory_pool()  # 等待全部轨迹落库再退出
        from app.agent.judge import shutdown_judge_pool
        shutdown_judge_pool()  # 等待未完成的 LLM-as-judge 评审

    summary = _summarize(records)
    report = {
        "meta": {
            "model": AI_MODEL,
            "max_iterations": MAX_ITERATIONS,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now().isoformat(),
            "duration_s": round(time.time() - t0, 1),
        },
        "summary": summary,
        "cases": records,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 评估完成 =====")
    rate = summary["success_rate"]
    if rate is not None:
        print(f"总体: {summary['success']}/{summary['executed']} 成功率 {rate:.1%}(skipped {summary['total'] - summary['executed']} 条)")
    else:
        print("无有效执行场景(全部 skipped)")
    for t, d in summary["by_type"].items():
        r = d["success_rate"]
        executed = d["total"] - d["skipped"]
        rate_txt = f"{r:.0%}" if r is not None else "N/A"
        print(f"  {t:16s} {d['success']}/{executed} ({rate_txt})  skipped={d['skipped']}")
    print(f"报告: {out.resolve()}")


if __name__ == "__main__":
    main()
