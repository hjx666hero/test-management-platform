"""Agent 工具箱:4 个工具函数实现 + OpenAI function calling Schema 定义。

工具清单(与需求一一对应):
1. get_git_diff    获取项目一最近一次代码提交的差异(subprocess 执行 git diff)
2. read_file       读取项目一指定文件内容(带行号,支持行区间)
3. generate_patch  生成 unified diff 修复补丁并入库(状态 pending_review,不改源码)
4. run_pytest      执行项目一 pytest 用例(可选:临时应用补丁验证,运行后自动还原)

安全设计:
- 所有文件操作限制在项目一根目录内(防路径逃逸);
- 源码永不落盘修改:补丁验证采用"备份 → 临时替换 → 运行 → 还原"模式;
- 工具输出统一截断,防止撑爆 LLM 上下文。

与 TMS 架构的兼容:
- run_pytest 与平台任务执行(services/executor.py)同一条链路:
  subprocess 在项目一目录下运行真实 pytest(自动加载其 pytest.ini/
  conftest/数据驱动 YAML),支持 -k 关键字或 node id 精确执行;
- env_url 通过 BASE_URL 环境变量注入,支持指定被测环境。
"""
import difflib
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from .. import config as app_config
from . import db
from .config import MAX_TOOL_OUTPUT_CHARS, PYTEST_TIMEOUT
from .models import ToolResult

logger = logging.getLogger("tms.agent.tools")


# ==================== 公共辅助 ====================

def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """截断工具输出,防止大文件/长日志把 LLM 上下文 token 撑爆。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[输出已截断,原长度 {len(text)} 字符]"


def _resolve_path(file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """把工具入参路径解析为绝对路径,并校验必须位于项目一目录内。

    支持绝对路径,也支持相对项目一根目录的路径(如 testcases/test_login.py)。
    返回 (绝对路径, 错误信息);出错时路径为 None。
    """
    root = Path(app_config.PYTEST_FRAMEWORK_PATH).resolve()
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    # 只允许访问项目一目录内的文件(防路径逃逸读取系统任意文件)
    if not path.is_relative_to(root):
        return None, f"路径越界,仅允许访问项目一目录({root})内 的文件: {file_path}"
    return path, None


# ==================== 工具 1:get_git_diff ====================

def get_git_diff() -> dict:
    """获取项目一最近一次代码提交的差异。

    实现策略(两次尝试,取到即用):
    a. `git diff HEAD~1 HEAD` —— 最近一次提交引入的变更;
    b. `git diff HEAD`        —— 工作区未提交变更(调试现场,常是引入 bug 的位置)。
    """
    root = app_config.PYTEST_FRAMEWORK_PATH
    outputs = []
    for args, desc in (
        (["git", "diff", "HEAD~1", "HEAD"], "最近一次提交差异 (HEAD~1..HEAD)"),
        (["git", "diff", "HEAD"], "工作区未提交差异 (vs HEAD)"),
    ):
        try:
            proc = subprocess.run(
                args, cwd=root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
            out = (proc.stdout or "").strip()
            if out:
                outputs.append(f"===== {desc} =====\n{out}")
            elif proc.returncode != 0 and proc.stderr:
                outputs.append(f"[{desc}] 执行失败: {proc.stderr.strip()[:200]}")
        except Exception as exc:  # noqa: BLE001 git 不存在/超时等,不影响其他命令
            outputs.append(f"[{desc}] 执行异常: {exc}")

    if not any(o.startswith("=====") for o in outputs):
        return {"success": False, "output": "未获取到 git diff 内容(项目可能不是 git 仓库或无提交)。\n" + "\n".join(outputs)}
    return {"success": True, "output": _truncate("\n\n".join(outputs))}


# ==================== 工具 2:read_file ====================

def read_file(file_path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> dict:
    """读取项目一中指定文件内容,返回带行号文本(行号便于 LLM 定位与引用代码)。"""
    path, err = _resolve_path(file_path)
    if err:
        return {"success": False, "output": err}
    if not path.is_file():
        return {"success": False, "output": f"文件不存在: {file_path}"}

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    # 行区间兜底:start>=1,end<=总行数,end 缺省=读到末尾
    start = max(1, start_line or 1)
    end = min(total, end_line or total)
    if start > end:
        return {"success": False, "output": f"行区间无效: {start}-{end}(文件共 {total} 行)"}

    numbered = "\n".join(f"{i:>4} | {lines[i - 1]}" for i in range(start, end + 1))
    header = f"文件: {path}(共 {total} 行,展示第 {start}-{end} 行)"
    return {"success": True, "output": _truncate(f"{header}\n{numbered}")}


# ==================== 工具 3:generate_patch ====================

def generate_patch(
    file_path: str,
    original_code: str,
    fixed_code: str,
    explanation: str,
    case_name: Optional[str] = None,
    task_id: Optional[int] = None,
    result_id: Optional[int] = None,
) -> dict:
    """生成最小修复补丁(unified diff)并保存为待审核建议。

    关键校验:original_code 必须与文件当前内容完全一致且唯一——
    1. 保证补丁可被精确应用(替换目标明确);
    2. 逼着 LLM 先 read_file 取证,避免凭记忆写补丁。

    本工具不修改源码,只入库(diff + 原文 + 修复文 + 理由),
    状态固定 pending_review,由人审核后应用。
    """
    path, err = _resolve_path(file_path)
    if err:
        return {"success": False, "output": err}
    if not path.is_file():
        return {"success": False, "output": f"文件不存在: {file_path}"}

    content = path.read_text(encoding="utf-8", errors="replace")

    # 校验 1:原文必须存在于文件中
    if original_code not in content:
        return {"success": False, "output": "original_code 与文件当前内容不匹配(请先 read_file 获取逐字符一致的原文,注意缩进与空行)"}
    # 校验 2:原文必须唯一,否则补丁应用位置有歧义
    if content.count(original_code) > 1:
        return {"success": False, "output": "original_code 在文件中出现多次,请扩大代码片段范围(带上下文)使其唯一"}

    # 生成 unified diff(a/... b/... 头 + @@ 行号块),与 git diff 格式一致
    rel_path = path.relative_to(Path(app_config.PYTEST_FRAMEWORK_PATH).resolve()).as_posix()
    diff = "".join(difflib.unified_diff(
        original_code.splitlines(keepends=True),
        fixed_code.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    ))

    patch_id = db.save_fix_suggestion(
        case_name=case_name or "(unknown)",
        file_path=rel_path,
        original_code=original_code,
        fixed_code=fixed_code,
        diff=diff,
        explanation=explanation,
        task_id=task_id,
        result_id=result_id,
    )
    logger.info("补丁已生成入库: patch_id=%s file=%s", patch_id, rel_path)
    return {
        "success": True,
        "output": _truncate(
            f"补丁已生成并保存(id={patch_id},状态 pending_review,未修改源码)。\n"
            f"可调用 run_pytest 并传 patch_id={patch_id} 临时应用验证修复效果。\n\n{diff}"
        ),
        "data": {"patch_id": patch_id},
    }


# ==================== 工具 4:run_pytest ====================

def _temporarily_apply_patch(patch_id: int) -> Tuple[Optional[object], Optional[str]]:
    """临时应用补丁:备份原内容 → 替换 original_code 为 fixed_code → 返回还原函数。

    通过闭包 + 调用方 try/finally 保证:无论测试运行结果如何,
    源码最终都会还原——"验证修复效果"与"不修改源码"两全。
    """
    row = db.get_suggestion(patch_id)
    if not row:
        return None, f"补丁不存在: patch_id={patch_id}"

    path, err = _resolve_path(row["file_path"])
    if err or not path.is_file():
        return None, f"补丁目标文件不可访问: {row['file_path']}"

    content = path.read_text(encoding="utf-8", errors="replace")
    if row["original_code"] not in content:
        return None, "补丁无法应用: original_code 与当前文件内容不匹配(文件可能已变更)"

    backup = content  # 备份原文
    path.write_text(content.replace(row["original_code"], row["fixed_code"], 1), encoding="utf-8")

    def _restore() -> None:
        """还原源码(finally 中调用,保证零残留)。"""
        path.write_text(backup, encoding="utf-8")
        logger.info("补丁 %s 验证完成,源码已还原: %s", patch_id, path)

    return _restore, None


def _run_pytest_subprocess(args: list, env_url: Optional[str] = None) -> Tuple[bool, str]:
    """在项目一目录下执行 pytest 通用封装,返回 (是否全部通过, 输出末尾摘要)。

    - `-o addopts=` 清掉项目一 pytest.ini 的 addopts(reruns 重跑拖慢验证、
      alluredir 污染项目一目录);
    - env_url 注入 BASE_URL 环境变量,支持指定被测环境(不传走项目一默认)。
    """
    cmd = [sys.executable, "-m", "pytest", *args, "-o", "addopts=", "--no-header", "-q", "--tb=short"]
    env = {**os.environ}
    if env_url:
        env["BASE_URL"] = env_url
    try:
        proc = subprocess.run(
            cmd, cwd=app_config.PYTEST_FRAMEWORK_PATH, env=env,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=PYTEST_TIMEOUT,
        )
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        # pytest 输出可能很长,只保留末尾(结果摘要与堆栈在末尾)
        tail = "\n".join(output.strip().splitlines()[-60:])
        return proc.returncode == 0, tail
    except subprocess.TimeoutExpired:
        return False, f"pytest 执行超时(>{PYTEST_TIMEOUT}s)"


def run_pytest(
    test_path: Optional[str] = None,
    test_name: Optional[str] = None,
    patch_id: Optional[int] = None,
    env_url: Optional[str] = None,
) -> dict:
    """执行 pytest 测试用例并返回结果;可临时应用补丁验证修复效果。

    两种用法:
    a. test_path(+ test_name):组装 node id(如 testcases/test_login.py::test_xxx),
       在项目一目录下执行真实 pytest(自动加载其 pytest.ini);
       仅 test_name 时用 -k 关键字匹配(支持参数化后缀,如 normal_title);
    b. patch_id:运行前临时应用该补丁,结束后自动还原源码,并把
       验证结论回写补丁记录(db.mark_verified)。
    """
    restore = None
    try:
        # 步骤 1:如指定补丁,先临时应用(源码被替换,finally 中还原)
        if patch_id:
            restore, err = _temporarily_apply_patch(patch_id)
            if err:
                return {"success": False, "output": err}

        # 步骤 2:确定执行目标
        if test_path:
            # 文件[::用例名] 组装 node id
            node_id = test_path + (f"::{test_name}" if test_name else "")
            passed, tail = _run_pytest_subprocess([node_id], env_url)
        elif test_name:
            # 仅用例名:-k 关键字匹配(函数名或参数化 id 均可命中)
            passed, tail = _run_pytest_subprocess(["testcases/", "-k", test_name], env_url)
        else:
            return {"success": False, "output": "请至少提供 test_path 或 test_name 之一"}

        # 步骤 3:回写补丁验证结论(若本次是带补丁的验证运行)
        if patch_id:
            db.mark_verified(patch_id, passed, tail)
            logger.info("补丁 %s 验证结论: %s", patch_id, "通过" if passed else "未通过")

        verdict = "通过" if passed else "未通过"
        return {
            "success": True,  # 工具执行成功(用例本身通过与否见 output 与 data.passed)
            "output": _truncate(f"运行结果: {verdict}\n{tail}"),
            "data": {"passed": passed},
        }
    finally:
        # 无论成功/异常/超时,都还原源码——Agent 全程不真实修改被测代码
        if restore:
            restore()


# ==================== OpenAI function calling Schema ====================
# 严格遵循 OpenAI tools 参数格式:[{"type": "function", "function": {name, description, parameters}}]
# parameters 使用 JSON Schema;LLM 据此生成 tool_calls,Agent 端按名分发执行。

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_git_diff",
            "description": "获取项目一(pytest-realworld-framework)最近一次代码提交的差异,用于判断用例失败是否由近期代码变更引入。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取项目一中指定文件的内容(返回带行号文本)。用于查看失败用例源码与被测代码,获取补丁所需的精确原文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径,相对项目一根目录(如 testcases/test_login.py)或绝对路径",
                    },
                    "start_line": {"type": "integer", "description": "起始行号(可选,从 1 开始)"},
                    "end_line": {"type": "integer", "description": "结束行号(可选,含该行)"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_patch",
            "description": (
                "生成最小修复补丁(unified diff)并保存为待审核建议(不修改源码)。"
                "original_code 必须与文件当前内容逐字符一致且唯一(先 read_file 获取),"
                "fixed_code 为修复后的代码片段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "目标文件路径(相对项目一根目录)"},
                    "original_code": {"type": "string", "description": "被替换的原始代码片段,必须与文件内容完全一致(含缩进)"},
                    "fixed_code": {"type": "string", "description": "修复后的代码片段"},
                    "explanation": {"type": "string", "description": "修复理由:根因分析 + 为什么此改动最小且安全"},
                },
                "required": ["file_path", "original_code", "fixed_code", "explanation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_pytest",
            "description": (
                "执行项目一的 pytest 测试用例并返回结果。"
                "test_path+test_name 组成 node id 精确执行(如 testcases/test_articles.py::test_create_article);"
                "仅传 test_name 时按关键字匹配(支持参数化 id,如 normal_title);"
                "传 patch_id 可临时应用补丁验证修复效果(运行后自动还原源码)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_path": {"type": "string", "description": "测试文件路径(相对项目一根目录),如 testcases/test_login.py"},
                    "test_name": {"type": "string", "description": "用例名:pytest 函数名或参数化 id,如 test_login_success 或 normal_title"},
                    "patch_id": {"type": "integer", "description": "要临时应用验证的补丁 ID(generate_patch 的返回值)"},
                },
                "required": [],
            },
        },
    },
]

# 工具名 → 实现函数的注册表(execute_tool 按此分发)
TOOL_REGISTRY = {
    "get_git_diff": get_git_diff,
    "read_file": read_file,
    "generate_patch": generate_patch,
    "run_pytest": run_pytest,
}


def execute_tool(name: str, arguments: dict) -> ToolResult:
    """工具分发器:Agent 每轮解析出 tool_calls 后由此统一执行。

    统一捕获异常并转为文本 Observation 喂回 LLM——
    工具失败(如参数错误)时 LLM 可读错误信息并自行纠正,而不是整个 Agent 崩溃。
    """
    func = TOOL_REGISTRY.get(name)
    if not func:
        return ToolResult(tool=name, success=False, output=f"未知工具: {name}(可用: {list(TOOL_REGISTRY)})")

    logger.info("执行工具: %s(%s)", name, list(arguments or {}))
    try:
        result = func(**(arguments or {}))
    except TypeError as exc:
        # 参数名/参数个数不匹配——把签名错误反馈给 LLM 供其纠正
        return ToolResult(tool=name, success=False, output=f"工具参数错误: {exc}")
    except Exception as exc:  # noqa: BLE001 工具内部异常兜底
        logger.exception("工具执行异常: %s", name)
        return ToolResult(tool=name, success=False, output=f"工具执行异常: {exc}")

    return ToolResult(
        tool=name,
        success=bool(result.get("success")),
        output=result.get("output", ""),
        data=result.get("data"),
    )
