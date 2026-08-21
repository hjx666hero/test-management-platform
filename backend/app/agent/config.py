"""Agent 模块配置。

设计原则:
1. 复用平台全局配置(backend/app/config.py)——API Key、模型、项目一路径
   仍从同一份 .env 读取,避免配置分散;
2. 在此叠加 Agent 专属参数(最大迭代轮数/温度/超时/输出截断),
   均支持环境变量覆盖,方便调参不改代码。
"""
import os

from .. import config as app_config

# ===== 通义千问(OpenAI 兼容模式) =====
# API Key 从环境变量 DASHSCOPE_API_KEY 读取(与平台 AI 根因分析共用)
DASHSCOPE_API_KEY = app_config.DASHSCOPE_API_KEY
# OpenAI 兼容模式入口:用 openai SDK 指定 base_url 即可调通义千问
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
AI_MODEL = app_config.AI_MODEL                 # 默认 qwen-plus
AGENT_ENABLED = app_config.AI_ENABLED          # 未配置 Key 时 Agent 自动禁用

# ===== ReAct 循环参数 =====
# 最多 3 轮"思考-行动"循环(需求约定),可通过 AGENT_MAX_ITERATIONS 覆盖
MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "3"))
# 低温度:代码修复讲究确定性,减少发散
TEMPERATURE = float(os.environ.get("AGENT_TEMPERATURE", "0.2"))
# 单次 LLM 请求超时(秒)
REQUEST_TIMEOUT = int(os.environ.get("AGENT_REQUEST_TIMEOUT", "120"))

# ===== 工具执行参数 =====
# 工具输出截断上限(字符):防止大文件/长日志把上下文 token 撑爆
MAX_TOOL_OUTPUT_CHARS = int(os.environ.get("AGENT_MAX_TOOL_OUTPUT_CHARS", "4000"))
# run_pytest 执行超时(秒)
PYTEST_TIMEOUT = int(os.environ.get("AGENT_PYTEST_TIMEOUT", "300"))

# ===== 项目一(pytest-realworld-framework)根目录 =====
# 所有工具的文件操作均限制在该目录内(防路径逃逸)
FRAMEWORK_PATH = app_config.PYTEST_FRAMEWORK_PATH
