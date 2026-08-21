"""修复建议的人工审核动作:应用(applied) / 拒绝(rejected)。

设计要点:
1. Agent 只产建议(状态 pending_review),是否真正修改源码由人决定——
   本模块是"审核通过 → 落盘应用"的唯一入口;
2. 应用前做与 generate_patch 相同的一致性校验(原文存在且唯一),
   避免文件在审核等待期间被改动后发生错替;
3. 应用方式 = original_code 整段替换为 fixed_code(与 Agent 验证补丁时的
   临时替换逻辑完全一致,保证"验证过的就是应用的")。
"""
import logging
from pathlib import Path
from typing import Tuple

from .. import config as app_config
from . import db

logger = logging.getLogger("tms.agent.review")

# 合法的审核动作
ALLOWED_ACTIONS = ("applied", "rejected")


def review_suggestion(patch_id: int, action: str) -> Tuple[bool, str]:
    """审核一条修复建议。

    - rejected:仅更新状态,不动文件;
    - applied :一致性校验通过后把补丁写入源文件,再更新状态。
    返回 (是否成功, 提示信息)。
    """
    if action not in ALLOWED_ACTIONS:
        return False, f"非法审核动作: {action}(仅支持 {'/'.join(ALLOWED_ACTIONS)})"

    row = db.get_suggestion(patch_id)
    if not row:
        return False, f"修复建议不存在: {patch_id}"
    if row["status"] != "pending_review":
        return False, f"该建议当前状态为 {row['status']},已审核过,不可重复操作"

    # ---- 拒绝:直接改状态 ----
    if action == "rejected":
        db.update_status(patch_id, "rejected")
        logger.info("补丁 %s 已被拒绝", patch_id)
        return True, "已拒绝该修复建议"

    # ---- 应用:先校验,再落盘 ----
    root = Path(app_config.PYTEST_FRAMEWORK_PATH).resolve()
    path = (root / row["file_path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return False, f"目标文件不可访问: {row['file_path']}"

    content = path.read_text(encoding="utf-8", errors="replace")
    if row["original_code"] not in content:
        return False, "original_code 与文件当前内容不匹配(文件可能已被修改),请重新生成补丁"
    if content.count(row["original_code"]) > 1:
        return False, "original_code 在文件中出现多次,无法确定替换位置"

    path.write_text(
        content.replace(row["original_code"], row["fixed_code"], 1),
        encoding="utf-8",
    )
    db.update_status(patch_id, "applied")
    logger.info("补丁 %s 已应用到源文件: %s", patch_id, path)
    return True, f"补丁已应用到 {row['file_path']}"
