"""数据库连接与会话管理(SQLAlchemy 2.x + pymysql)。"""
import pymysql
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from . import config

# 建库兜底:连接前确保 test_platform 数据库存在
def _ensure_database() -> None:
    conn = pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.DB_NAME}` "
                "DEFAULT CHARACTER SET utf8mb4"
            )
        conn.commit()
    finally:
        conn.close()


_ensure_database()

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    """FastAPI 依赖:每个请求一个 Session,请求结束关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
