"""核心执行器:将项目一 pytest-realworld-framework 作为"库"直接调用。

设计要点(对应平台价值):
1. 不通过 subprocess 调 pytest 命令,而是把项目一加入 sys.path,
   直接 import 其 api/ 模块与 utils/request_util.py、utils/db_util.py,
   实例化接口类调用方法执行用例,并用 fetch_one 做数据库落库校验。
2. 每个用例通过"替换 client.base_url"支持任意被测环境(登录/请求均走任务
   指定的 env_url),绕开项目一 config 模块级常量限制。
3. 执行结果同时落库(MySQL)与生成 Allure result JSON,
   供"报告解析 + 通义千问根因分析"使用。
"""
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime

from .. import config
from ..database import SessionLocal
from ..models import TestCaseResult, TestTask

logger = logging.getLogger("tms.executor")

# ===== 被测账号(与项目一 .env 保持一致,登录失败会自动注册兜底)=====
TEST_EMAIL = os.environ.get("TMS_TEST_EMAIL", "qa_tester@example.com")
TEST_PASSWORD = os.environ.get("TMS_TEST_PASSWORD", "Test@1234")
TEST_USERNAME = os.environ.get("TMS_TEST_USERNAME", "qa_tester")


def _ensure_framework_path() -> None:
    """把项目一加入 sys.path,使其 api/utils 可被 import。"""
    path = config.PYTEST_FRAMEWORK_PATH
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)
        logger.info("已加入项目一路径: %s", path)


class ExecContext:
    """用例执行上下文:提供指向指定环境的 HttpClient 工厂,并记录请求/响应快照。"""

    def __init__(self, env_url: str):
        self.env_url = env_url.rstrip("/")
        self.history = []  # [{method, url, payload, status_code, body}] 供失败用例 AI 分析

    def client(self, token: str = None):
        """构造 base_url=任务环境 的请求客户端,并包装记录每次请求/响应快照。"""
        from utils.request_util import HttpClient  # 惰性 import 项目一
        client = HttpClient(base_url=self.env_url, token=token)
        orig_request = client._request

        def _record(method, path, **kwargs):
            resp = orig_request(method, path, **kwargs)
            self.history.append({
                "method": method,
                "url": f"{self.env_url}{path}",
                "payload": kwargs.get("json"),
                "status_code": resp.status_code,
                "body": resp.text[:500],
            })
            return resp

        client._request = _record
        return client

    def last_snapshot(self):
        """最近一次请求/响应快照;无历史时返回 (None, None)。"""
        if not self.history:
            return None, None
        last = self.history[-1]
        req = {"method": last["method"], "url": last["url"], "payload": last["payload"]}
        rsp = {"status_code": last["status_code"], "body": last["body"]}
        return req, rsp

    def login_token(self) -> str:
        """登录拿 Token;账号不存在则先注册再登录(与项目一 conftest 兜底一致)。"""
        client = self.client()
        resp = client.post(
            "/users/login",
            json={"user": {"email": TEST_EMAIL, "password": TEST_PASSWORD}},
        )
        if resp.status_code in (401, 404, 422):
            client.post(
                "/users",
                json={"user": {
                    "username": TEST_USERNAME,
                    "email": TEST_EMAIL,
                    "password": TEST_PASSWORD,
                }},
            )
            resp = client.post(
                "/users/login",
                json={"user": {"email": TEST_EMAIL, "password": TEST_PASSWORD}},
            )
        resp.raise_for_status()
        return resp.json()["user"]["token"]


# ==================== 用例注册表 ====================
# 每个用例: {name, tags, func(ctx, token) -> None(失败即抛异常)}
# tags 供"创建任务选标签"过滤;func 内直接用 api 层实例化调用并断言。
# 请求/响应快照由 api.client 的底层记录,执行时写入结果。


def _api_login(ctx: ExecContext, token: str):
    from api.user_api import UserApi
    api = UserApi()
    api.client = ctx.client()
    resp = api.login(TEST_EMAIL, TEST_PASSWORD)
    assert resp.status_code == 200, f"登录失败: {resp.status_code} {resp.text[:200]}"
    assert resp.json()["user"]["email"] == TEST_EMAIL


def _api_register_duplicate(ctx: ExecContext, token: str):
    from api.user_api import UserApi
    api = UserApi()
    api.client = ctx.client()
    resp = api.register(TEST_USERNAME, TEST_EMAIL, TEST_PASSWORD)
    assert resp.status_code in (400, 422, 409), f"重复注册应失败: {resp.status_code}"


def _api_create_article(ctx: ExecContext, token: str):
    import time as _t
    from api.article_api import ArticleApi
    api = ArticleApi()
    api.client = ctx.client(token)
    title = f"tms-{int(_t.time())}"
    description = "tms desc"
    resp = api.create_article(title=title, description=description, body="tms body")
    assert resp.status_code == 201, f"创建文章失败: {resp.status_code} {resp.text[:200]}"
    slug = resp.json()["article"]["slug"]

    # 数据库落库校验:直接 import 项目一 utils/db_util,按 slug 查 articles 表,
    # 断言 title/description 真实落库(接口层无法覆盖的数据一致性盲区)。
    from utils.db_util import fetch_one, has_column
    soft_delete = " AND deleted_at IS NULL" if has_column("articles", "deleted_at") else ""
    row = fetch_one(
        f"SELECT title, description FROM articles WHERE slug = %s{soft_delete}",
        (slug,),
    )
    assert row, f"DB 未查询到文章 slug={slug},落库校验失败"
    assert row["title"] == title, f"DB title 与请求不一致: 期望 {title!r},实际 {row['title']!r}"
    assert row["description"] == description, (
        f"DB description 与请求不一致: 期望 {description!r},实际 {row['description']!r}"
    )

    api.delete_article(slug)  # 环境干净


def _api_get_article(ctx: ExecContext, token: str):
    from api.article_api import ArticleApi
    api = ArticleApi()
    api.client = ctx.client(token)
    resp = api.list_articles()
    assert resp.status_code == 200, f"文章列表失败: {resp.status_code}"
    assert isinstance(resp.json()["articles"], list)


def _api_update_article(ctx: ExecContext, token: str):
    import time as _t
    from api.article_api import ArticleApi
    api = ArticleApi()
    api.client = ctx.client(token)
    resp = api.create_article(title=f"upd-{int(_t.time())}", description="d", body="b")
    assert resp.status_code == 201
    slug = resp.json()["article"]["slug"]
    api.update_article(slug, title="updated", body="new body")
    api.delete_article(slug)


def _api_delete_article(ctx: ExecContext, token: str):
    import time as _t
    from api.article_api import ArticleApi
    api = ArticleApi()
    api.client = ctx.client(token)
    resp = api.create_article(title=f"del-{int(_t.time())}", description="d", body="b")
    assert resp.status_code == 201
    slug = resp.json()["article"]["slug"]
    api.delete_article(slug)
    resp_get = api.get_article(slug)
    assert resp_get.status_code == 404, f"删除后应 404: {resp_get.status_code}"


def _api_feed_requires_auth(ctx: ExecContext, token: str):
    from api.article_api import ArticleApi
    api = ArticleApi()
    api.client = ctx.client()  # 不带 Token
    resp = api.feed_articles()
    assert resp.status_code == 401, f"未登录 Feed 应 401: {resp.status_code}"


def _api_tags(ctx: ExecContext, token: str):
    from api.tag_api import TagApi
    api = TagApi()
    api.client = ctx.client()
    resp = api.get_tags()
    assert resp.status_code == 200, f"标签列表失败: {resp.status_code}"
    assert "tags" in resp.json()


def _api_follow_profile(ctx: ExecContext, token: str):
    from api.profile_api import ProfileApi
    from config.config import TEST_USERNAME as PU  # noqa: 复用项目一配置
    api = ProfileApi()
    api.client = ctx.client(token)
    resp = api.follow_user(TEST_USERNAME)
    assert resp.status_code == 200, f"关注失败: {resp.status_code}"
    api.unfollow_user(TEST_USERNAME)


def _api_comment(ctx: ExecContext, token: str):
    import time as _t
    from api.article_api import ArticleApi
    from api.comment_api import CommentApi
    aapi = ArticleApi()
    aapi.client = ctx.client(token)
    resp = aapi.create_article(title=f"cmt-{int(_t.time())}", description="d", body="b")
    assert resp.status_code == 201
    slug = resp.json()["article"]["slug"]
    capi = CommentApi()
    capi.client = ctx.client(token)
    c = capi.create_comment(slug, body="tms comment")
    assert c.status_code in (200, 201), f"评论失败: {c.status_code}"
    capi.delete_comment(slug, c.json()["comment"]["id"])
    aapi.delete_article(slug)


def _api_favorite(ctx: ExecContext, token: str):
    import time as _t
    from api.article_api import ArticleApi
    from api.favorite_api import FavoriteApi
    aapi = ArticleApi()
    aapi.client = ctx.client(token)
    resp = aapi.create_article(title=f"fav-{int(_t.time())}", description="d", body="b")
    assert resp.status_code == 201
    slug = resp.json()["article"]["slug"]
    fapi = FavoriteApi()
    fapi.client = ctx.client(token)
    fav = fapi.favorite_article(slug)
    assert fav.status_code == 200, f"收藏失败: {fav.status_code}"
    fapi.unfavorite_article(slug)
    aapi.delete_article(slug)


# (name, tags, func) —— tags 供前端"标签"筛选
CASE_REGISTRY = [
    {"name": "登录-正确账号返回Token", "tags": ["user", "P0"], "func": _api_login},
    {"name": "注册-重复邮箱返回4xx", "tags": ["user", "P1"], "func": _api_register_duplicate},
    {"name": "文章-创建并落库后清理", "tags": ["articles", "P0"], "func": _api_create_article},
    {"name": "文章-列表返回200", "tags": ["articles", "P0"], "func": _api_get_article},
    {"name": "文章-更新标题与正文", "tags": ["articles", "P1"], "func": _api_update_article},
    {"name": "文章-删除后查询404", "tags": ["articles", "P0"], "func": _api_delete_article},
    {"name": "Feed-未登录应401", "tags": ["articles", "P1"], "func": _api_feed_requires_auth},
    {"name": "标签-列表非空", "tags": ["tags", "P2"], "func": _api_tags},
    {"name": "Profile-关注后取关", "tags": ["profiles", "P1"], "func": _api_follow_profile},
    {"name": "评论-增删评论", "tags": ["comments", "P1"], "func": _api_comment},
    {"name": "收藏-收藏后取消", "tags": ["favorites", "P2"], "func": _api_favorite},
]


def _match_tags(case_tags, selected):
    """标签过滤:selected 为空=全跑;否则用例需命中任一选中标签。"""
    if not selected:
        return True
    return bool(set(case_tags) & set(selected))


# ==================== Allure result JSON 生成 ====================
def _write_allure_result(task_dir, case_name, status, start_ms, end_ms, message=None):
    """按 Allure2 规范写一条 {uuid}-result.json,供报告解析/Allure CLI 使用。"""
    allure_dir = task_dir / "allure-results"
    allure_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "uuid": str(uuid.uuid4()),
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
        json.dump(result, f, ensure_ascii=False)


# ==================== 任务执行主流程 ====================
def run_task(task_id: int) -> None:
    """后台执行任务:登录 → 按标签跑用例 → 落库 + 写 Allure JSON → 更新任务状态。"""
    _ensure_framework_path()

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
            ctx = ExecContext(task.env_url)
            token = ctx.login_token()  # 登录失败会抛异常,直接走到外层 except
            # 登录结果由注册表中的 _api_login 用例产出,这里不重复计

            selected = task.tags or []
            passed = failed = 0
            for case in CASE_REGISTRY:
                if not _match_tags(case["tags"], selected):
                    continue
                start_ms = time.time() * 1000
                try:
                    case["func"](ctx, token)
                    _write_result(db, task, case["name"], "passed", int((time.time() * 1000) - start_ms), None, None, None, None)
                    passed += 1
                except Exception as exc:  # noqa: BLE001 用例失败统一捕获
                    logger.warning("用例失败 [%s]: %s", case["name"], exc)
                    req_snap, rsp_snap = ctx.last_snapshot()
                    _write_result(db, task, case["name"], "failed", int((time.time() * 1000) - start_ms), str(exc), req_snap, rsp_snap, None)
                    failed += 1
                finally:
                    pass

            task.total = passed + failed
            task.passed = passed
            task.failed = failed
            task.status = "success" if failed == 0 else "failed"
            logger.info("任务 %s 完成: %s/%s 通过, %s 失败", task_id, passed, task.total, failed)
        except Exception as exc:  # noqa: BLE001 登录/环境级错误
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


def _write_result(db, task, case_name, status, duration_ms, error_message,
                  request_snapshot, response_snapshot, ai_analysis):
    """写一条用例结果到 DB,并同步生成 Allure result JSON。"""
    row = TestCaseResult(
        task_id=task.id,
        case_name=case_name,
        status=status,
        duration_ms=duration_ms,
        error_message=error_message,
        request_snapshot=request_snapshot,
        response_snapshot=response_snapshot,
        ai_analysis=ai_analysis,
        ai_status="pending" if status == "failed" else "none",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # 同步生成 Allure result JSON(失败用例供 AI 服务解析)
    task_dir = config.TASKS_DIR / str(task.id)
    start_ms = int((datetime.now().timestamp() - (duration_ms / 1000)) * 1000)
    end_ms = int(datetime.now().timestamp() * 1000)
    _write_allure_result(task_dir, case_name, status, start_ms, end_ms, error_message)
    return row
