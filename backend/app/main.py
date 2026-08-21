"""FastAPI 主入口:应用装配、CORS、路由、建表。"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .database import Base, engine
from .routers import fixes, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
)

# 建表(幂等)
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="测试管理平台 (TMS)",
    description="管理测试任务、异步执行项目一用例、Allure 报告与 AI 根因分析",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tasks.router)
app.include_router(fixes.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "ai_enabled": config.AI_ENABLED}
