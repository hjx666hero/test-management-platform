# -*- coding: utf-8 -*-
"""后端启动入口: python run.py
先复制 .env.example 为 .env 并填入配置,再启动。
reload 仅在开发时开启;沙箱/CI 环境请使用 reload=False。
"""
import os
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=os.environ.get("TMS_RELOAD", "0") == "1",
    )
