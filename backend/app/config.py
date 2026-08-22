"""平台配置:从 .env / 环境变量加载,集中管理。"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 加载 backend/.env(若存在)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ===== MySQL =====
DB_HOST = os.environ.get("TMS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("TMS_DB_PORT", "3307"))
DB_USER = os.environ.get("TMS_DB_USER", "root")
DB_PASSWORD = os.environ.get("TMS_DB_PASSWORD", "root123")
DB_NAME = os.environ.get("TMS_DB_NAME", "test_platform")

DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    "?charset=utf8mb4"
)

# ===== 项目一(被测框架)路径 =====
PYTEST_FRAMEWORK_PATH = os.environ.get("PYTEST_FRAMEWORK_PATH", "F:/pytest-realworld-framework")

# ===== 被测后端默认地址 =====
DEFAULT_BASE_URL = os.environ.get("TMS_DEFAULT_BASE_URL", "http://localhost:8080/api")

# ===== 通义千问 =====
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
AI_MODEL = os.environ.get("TMS_AI_MODEL", "qwen-plus")
# LLM 入口(OpenAI 兼容模式):
# - 公共 DashScope: https://dashscope.aliyuncs.com/compatible-mode/v1
# - 专属实例(MaaS): https://ws-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
AI_BASE_URL = os.environ.get(
    "TMS_AI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
# 计价(元/千token,用于 Agent 成本看板;不同模型单价不同,换模型时一并改)
AI_PRICE_INPUT_PER_1K = float(os.environ.get("TMS_AI_PRICE_INPUT", "0.0008"))
AI_PRICE_OUTPUT_PER_1K = float(os.environ.get("TMS_AI_PRICE_OUTPUT", "0.002"))
AI_ENABLED = bool(DASHSCOPE_API_KEY)

# ===== CORS =====
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("TMS_CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]

# ===== 报告/结果目录(backend/data/tasks/<task_id>/...) =====
DATA_DIR = BASE_DIR / "data"
TASKS_DIR = DATA_DIR / "tasks"
