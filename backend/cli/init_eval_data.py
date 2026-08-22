# -*- coding: utf-8 -*-
"""评估数据初始化:生成 60 条互不重复的失败场景,写入 eval_cases 表。

用法(在 backend 目录下):
    python cli/init_eval_data.py

四类场景各 15 条:
1. field_change    字段变更——代码取错字段/方法/变量名(KeyError/AttributeError/NameError),可注入
2. timeout         超时——requests 连接/读超时(环境问题,不应改代码),不注入
3. assertion_error 断言错误——期望值/断言逻辑写错(assert 失败),可注入
4. env_jitter      环境抖动——连接拒绝/5xx/SSL/DNS/重置/代理等(不应改代码),不注入

多样性保证:
- 可注入类:30 个锚点覆盖 test_articles.py 的 20+ 个不同断言/取值位置,以及
  article_data.yaml 的 3 组数据驱动用例;故障形态(误拼字段/大小写/驼峰蛇形/
  方法名/变量名/期望状态码/边界值/硬编码)各不相同,无任何重复;
- 环境类:30 条日志覆盖 Read/Connect 超时、连接拒绝、502/503/504/500、SSL 证书、
  DNS 解析、连接重置/中止、协议中断、分块解码、代理、过多重定向、空响应解码,
  且主机/端口/URL/超时值/所属用例均不同。

初始化包含"预检":逐条校验可注入锚点在目标文件中存在且唯一,
防止源文件漂移后评估悄悄失效。
"""
import os
import sys
from pathlib import Path

# 脚本自举:无论从哪里执行,都定位 backend/ 为导入根与 cwd(便于读 .env)
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from app import config as app_config  # noqa: E402
from app.agent import db as agent_db  # noqa: E402

ART = "testcases/test_articles.py"
YAML = "data/article_data.yaml"

# 环境类的"期望行为"说明(成功标准 = Agent 不产出代码补丁)
ENV_EXPECTED = "无需代码修复:属环境/网络问题,Agent 正确行为是识别并说明,不产出代码补丁"


# ==================== 失败日志构造器(仿真 pytest 输出) ====================

def _fail_log(file_path: str, case_node: str, frame_lines: list, summary: str) -> str:
    """拼一段仿真 pytest 失败输出:FAILURES 头 + 堆栈帧 + short summary。"""
    fn = case_node.split("::")[-1]
    return "\n".join([
        "================================== FAILURES ===================================",
        f"____ {fn} ____",
        *frame_lines,
        "=========================== short test summary info ===========================",
        f"FAILED {file_path}::{case_node} - {summary}",
    ])


def _code_frame(file_path: str, case_node: str, lineno: int, code_line: str, err_lines: list) -> list:
    """用户代码帧:文件:行号: in 函数名 + 源码行 + E 行。"""
    fn = case_node.split("::")[-1]
    return [
        f"{file_path}:{lineno}: in {fn}",
        f"    {code_line.strip()}",
        *err_lines,
    ]


def _lib_frame(err_lines: list) -> list:
    """三方库帧(requests/urllib3,路径示意)。"""
    return [
        r"..\..\lib\site-packages\requests\adapters.py:516: in send",
        *err_lines,
    ]


# ==================== 可注入类构造器 ====================

def _inj_case(title, error_type, file_path, case_name, lineno, anchor, buggy, err_lines, summary):
    """可注入场景:expected_patch = 正确代码(锚点本身)。失败日志展示故障代码(注入后的真实形态)。"""
    return {
        "title": title,
        "error_type": error_type,
        "file_path": file_path,
        "case_name": case_name,
        "failure_log": _fail_log(file_path, case_name, _code_frame(file_path, case_name, lineno, buggy, err_lines), summary),
        "expected_patch": anchor,
        "inject_original": anchor,
        "inject_buggy": buggy,
    }


# ==================== 环境类构造器 ====================

def _env_case(title, error_type, file_path, case_name, frame_lines, summary):
    """环境类场景:不注入;期望 Agent 识别为环境问题且不产出补丁。"""
    return {
        "title": title,
        "error_type": error_type,
        "file_path": file_path,
        "case_name": case_name,
        "failure_log": _fail_log(file_path, case_name, frame_lines, summary),
        "expected_patch": ENV_EXPECTED,
        "inject_original": None,
        "inject_buggy": None,
    }


def _timeout_case(title, file_path, case_name, exc, pool_desc):
    summary = f"requests.exceptions.{exc}: {pool_desc}"
    return _env_case(title, "timeout", file_path, case_name, _lib_frame([f"E   {summary}"]), summary)


# ==================== 1. 字段变更 field_change(15 条,可注入) ====================

FIELD_CHANGE_CASES = [
    _inj_case(
        title="字段变更-01 更新文章-响应字段 title 误拼为 titel(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_update_article", lineno=125,
        anchor='            assert article["title"].startswith("updated title"), f"标题应以 \'updated title\' 开头,实际: {article[\'title\']}"',
        buggy='            assert article["titel"].startswith("updated title"), f"标题应以 \'updated title\' 开头,实际: {article[\'title\']}"',
        err_lines=["E   KeyError: 'titel'"],
        summary="KeyError: 'titel'",
    ),
    _inj_case(
        title="字段变更-02 查询文章-响应字段 slug 误拼为 slugs(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_get_article", lineno=108,
        anchor='            assert article["slug"] == slug',
        buggy='            assert article["slugs"] == slug',
        err_lines=["E   KeyError: 'slugs'"],
        summary="KeyError: 'slugs'",
    ),
    _inj_case(
        title="字段变更-03 创建文章-DB落库断言字段 title 误拼为 titre(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_create_article[normal_title]", lineno=45,
        anchor="    assert row[\"title\"] == title, f\"DB title 与请求不一致:期望 {title!r},实际 {row['title']!r}\"",
        buggy="    assert row[\"titre\"] == title, f\"DB title 与请求不一致:期望 {title!r},实际 {row['title']!r}\"",
        err_lines=["E   KeyError: 'titre'"],
        summary="KeyError: 'titre'",
    ),
    _inj_case(
        title="字段变更-04 查询文章-响应字段 title 误写为首字母大写 Title(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_get_article", lineno=109,
        anchor='            assert article["title"] == "article to get"',
        buggy='            assert article["Title"] == "article to get"',
        err_lines=["E   KeyError: 'Title'"],
        summary="KeyError: 'Title'",
    ),
    _inj_case(
        title="字段变更-05 更新文章-响应字段 body 误写为 content(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_update_article", lineno=126,
        anchor='            assert article["body"] == "new body"',
        buggy='            assert article["content"] == "new body"',
        err_lines=["E   KeyError: 'content'"],
        summary="KeyError: 'content'",
    ),
    _inj_case(
        title="字段变更-06 Feed 列表-响应键 articles 误写为 article_list(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_feed_returns_articles", lineno=172,
        anchor='        articles = resp.json()["articles"]',
        buggy='        articles = resp.json()["article_list"]',
        err_lines=["E   KeyError: 'article_list'"],
        summary="KeyError: 'article_list'",
    ),
    _inj_case(
        title="字段变更-07 查询文章-响应包装键 article 误写为 data(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_get_article", lineno=107,
        anchor='            article = resp.json()["article"]\n            assert article["slug"] == slug',
        buggy='            article = resp.json()["data"]\n            assert article["slug"] == slug',
        err_lines=["E   KeyError: 'data'"],
        summary="KeyError: 'data'",
    ),
    _inj_case(
        title="字段变更-08 创建文章-清理时嵌套字段 slug 误写为 slug_id(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_create_article[normal_title]", lineno=95,
        anchor='            api.delete_article(resp.json()["article"]["slug"])',
        buggy='            api.delete_article(resp.json()["article"]["slug_id"])',
        err_lines=["E   KeyError: 'slug_id'"],
        summary="KeyError: 'slug_id'",
    ),
    _inj_case(
        title="字段变更-09 创建文章-DB落库断言字段 description 误写为 desc(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_create_article[normal_title]", lineno=46,
        anchor='    assert row["description"] == description, (',
        buggy='    assert row["desc"] == description, (',
        err_lines=["E   KeyError: 'desc'"],
        summary="KeyError: 'desc'",
    ),
    _inj_case(
        title="字段变更-10 创建文章-数据字段 title 误写为 titl(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_create_article[normal_title]", lineno=72,
        anchor='        title = case["title"].replace("{ts}", str(int(time.time())))',
        buggy='        title = case["titl"].replace("{ts}", str(int(time.time())))',
        err_lines=["E   KeyError: 'titl'"],
        summary="KeyError: 'titl'",
    ),
    _inj_case(
        title="字段变更-11 创建文章-数据字段 description 误写为 descr(KeyError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_create_article[normal_title]", lineno=77,
        anchor='            description=case["description"],',
        buggy='            description=case["descr"],',
        err_lines=["E   KeyError: 'descr'"],
        summary="KeyError: 'descr'",
    ),
    _inj_case(
        title="字段变更-12 删除文章-API 方法名 get_article 误写为 get_articles(AttributeError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_delete_article", lineno=143,
        anchor='            resp_get = api.get_article(slug)',
        buggy='            resp_get = api.get_articles(slug)',
        err_lines=["E   AttributeError: 'ArticleApi' object has no attribute 'get_articles'"],
        summary="AttributeError: 'ArticleApi' object has no attribute 'get_articles'",
    ),
    _inj_case(
        title="字段变更-13 Feed 前置-API 方法名 follow_user 误写为 follow_users(AttributeError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_feed_returns_articles", lineno=167,
        anchor='        ProfileApi(token=auth_token).follow_user(TEST_USERNAME)',
        buggy='        ProfileApi(token=auth_token).follow_users(TEST_USERNAME)',
        err_lines=["E   AttributeError: 'ProfileApi' object has no attribute 'follow_users'"],
        summary="AttributeError: 'ProfileApi' object has no attribute 'follow_users'",
    ),
    _inj_case(
        title="字段变更-14 查询文章-校验函数名 validate_article_schema 误写为复数(NameError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_get_article", lineno=112,
        anchor='            validate_article_schema(article)',
        buggy='            validate_article_schemas(article)',
        err_lines=["E   NameError: name 'validate_article_schemas' is not defined"],
        summary="NameError: name 'validate_article_schemas' is not defined",
    ),
    _inj_case(
        title="字段变更-15 异常标题-参数变量 description 误写为 descriptions(NameError)",
        error_type="field_change", file_path=ART,
        case_name="TestArticles::test_create_article_invalid_titles[empty_title]", lineno=188,
        anchor='        resp = api.create_article(title=title, description=description, body="test body")',
        buggy='        resp = api.create_article(title=title, description=descriptions, body="test body")',
        err_lines=["E   NameError: name 'descriptions' is not defined"],
        summary="NameError: name 'descriptions' is not defined",
    ),
]


# ==================== 2. 断言错误 assertion_error(15 条,可注入) ====================

ASSERTION_CASES = [
    _inj_case(
        title="断言错误-01 查询文章-期望状态码 200 误写为 201",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_get_article", lineno=106,
        anchor='            resp = api.get_article(slug)\n\n            assert resp.status_code == 200,f"期望200，实际{resp.status_code}:{resp.text[:200]}"',
        buggy='            resp = api.get_article(slug)\n\n            assert resp.status_code == 201,f"期望200，实际{resp.status_code}:{resp.text[:200]}"',
        err_lines=[
            'E   AssertionError: 期望200，实际200:{"article":{"slug":"article-to-get","title":"article to get"}}',
            "E   assert 200 == 201",
            "E    +  where 200 = <Response [200]>.status_code",
        ],
        summary="AssertionError: 期望200，实际200:{...}",
    ),
    _inj_case(
        title="断言错误-02 更新文章-期望状态码 200 误写为 404",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_update_article", lineno=123,
        anchor='            resp = api.update_article(slug, title=f"updated title {int(time.time())}", body="new body")\n\n            assert resp.status_code == 200,f"期望200，实际{resp.status_code}:{resp.text[:200]}"',
        buggy='            resp = api.update_article(slug, title=f"updated title {int(time.time())}", body="new body")\n\n            assert resp.status_code == 404,f"期望200，实际{resp.status_code}:{resp.text[:200]}"',
        err_lines=[
            'E   AssertionError: 期望200，实际200:{"article":{"title":"updated title 1787298000"}}',
            "E   assert 200 == 404",
            "E    +  where 200 = <Response [200]>.status_code",
        ],
        summary="AssertionError: 期望200，实际200:{...}",
    ),
    _inj_case(
        title="断言错误-03 Feed 鉴权-期望状态码 401 误写为 403",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_feed_requires_auth", lineno=153,
        anchor='        assert resp.status_code == 401, f"期望401,实际{resp.status_code}:{resp.text[:200]}"',
        buggy='        assert resp.status_code == 403, f"期望401,实际{resp.status_code}:{resp.text[:200]}"',
        err_lines=[
            "E   AssertionError: 期望401,实际401:{\"errors\":{\"body\":[\"unauthorized\"]}}",
            "E   assert 401 == 403",
            "E    +  where 401 = <Response [401]>.status_code",
        ],
        summary="AssertionError: 期望401,实际401:{...}",
    ),
    _inj_case(
        title="断言错误-04 异常标题-期望状态码组 (400,422) 误写为 (400,403)",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_create_article_invalid_titles[empty_title]", lineno=189,
        anchor='        assert resp.status_code in (400, 422), f"[{description}] 期望 4xx,实际 {resp.status_code}: {resp.text[:200]}"',
        buggy='        assert resp.status_code in (400, 403), f"[{description}] 期望 4xx,实际 {resp.status_code}: {resp.text[:200]}"',
        err_lines=[
            "E   AssertionError: [空标题] 期望 4xx,实际 422: {\"errors\":{\"title\":[\"can't be blank\"]}}",
            "E   assert 422 in (400, 403)",
            "E    +  where 422 = <Response [422]>.status_code",
        ],
        summary="AssertionError: [空标题] 期望 4xx,实际 422: {...}",
    ),
    _inj_case(
        title="断言错误-05 查询文章-slug 断言目标误加后缀拼接",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_get_article", lineno=108,
        anchor='            assert article["slug"] == slug',
        buggy='            assert article["slug"] == slug + "x"',
        err_lines=[
            "E   AssertionError: assert 'article-to-get' == 'article-to-getx'",
            "E    +  where 'article-to-get' = <Response [200]>.json()['article']['slug']",
        ],
        summary="AssertionError: assert 'article-to-get' == 'article-to-getx'",
    ),
    _inj_case(
        title="断言错误-06 更新文章-标题前缀断言大小写误写为 UPDATED",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_update_article", lineno=125,
        anchor='            assert article["title"].startswith("updated title"), f"标题应以 \'updated title\' 开头,实际: {article[\'title\']}"',
        buggy='            assert article["title"].startswith("UPDATED title"), f"标题应以 \'updated title\' 开头,实际: {article[\'title\']}"',
        err_lines=[
            "E   AssertionError: 标题应以 'updated title' 开头,实际: updated title 1787298000",
            "E   assert False",
            "E    +  where False = <built-in method str.startswith of str>",
        ],
        summary="AssertionError: 标题应以 'updated title' 开头,实际: updated title ...",
    ),
    _inj_case(
        title="断言错误-07 更新文章-正文期望值误加字符 s",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_update_article", lineno=126,
        anchor='            assert article["body"] == "new body"',
        buggy='            assert article["body"] == "new bodys"',
        err_lines=[
            "E   AssertionError: assert 'new body' == 'new bodys'",
            "E    +  where 'new body' = <Response [200]>.json()['article']['body']",
        ],
        summary="AssertionError: assert 'new body' == 'new bodys'",
    ),
    _inj_case(
        title="断言错误-08 Feed 列表-最少文章数断言 1 误写为 100",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_feed_returns_articles", lineno=173,
        anchor='        assert len(articles) >= 1, f"Feed 应至少返回 1 篇文章,实际: {len(articles)}"',
        buggy='        assert len(articles) >= 100, f"Feed 应至少返回 1 篇文章,实际: {len(articles)}"',
        err_lines=[
            "E   AssertionError: Feed 应至少返回 1 篇文章,实际: 3",
            "E   assert 3 >= 100",
            "E    +  where 3 = len([{'slug': 'feed-article', ...}, ...])",
        ],
        summary="AssertionError: Feed 应至少返回 1 篇文章,实际: 3",
    ),
    _inj_case(
        title="断言错误-09 数据驱动-正常标题期望状态码 201 误写为 200(YAML)",
        error_type="assertion_error", file_path=YAML,
        case_name="TestArticles::test_create_article[normal_title]", lineno=20,
        anchor="    expect_status: 201",
        buggy="    expect_status: 200",
        err_lines=[
            'E   AssertionError: [normal_title] 期望 200,实际 201:{"article":{"title":"数据驱动文章 1787298000"}}',
            "E   assert 201 in [200]",
            "E    +  where 201 = <Response [201]>.status_code",
            "E    +  and   [200] = _as_list(200)",
        ],
        summary="AssertionError: [normal_title] 期望 200,实际 201:{...}",
    ),
    _inj_case(
        title="断言错误-10 数据驱动-空标题期望状态码组 (400,422) 误写为 (401,403)(YAML)",
        error_type="assertion_error", file_path=YAML,
        case_name="TestArticles::test_create_article[empty_title]", lineno=29,
        anchor='  - id: empty_title\n    title: ""\n    description: "数据驱动:空标题"\n    body: "数据驱动:测试正文内容"\n    tag_list: []\n    expect_status: [400, 422]',
        buggy='  - id: empty_title\n    title: ""\n    description: "数据驱动:空标题"\n    body: "数据驱动:测试正文内容"\n    tag_list: []\n    expect_status: [401, 403]',
        err_lines=[
            "E   AssertionError: [empty_title] 期望 [401, 403],实际 422: {\"errors\":{\"title\":[\"can't be blank\"]}}",
            "E   assert 422 in [401, 403]",
        ],
        summary="AssertionError: [empty_title] 期望 [401, 403],实际 422: {...}",
    ),
    _inj_case(
        title="断言错误-11 数据驱动-超长标题期望状态码组 (400,422) 误写为 (200,201)(YAML)",
        error_type="assertion_error", file_path=YAML,
        case_name="TestArticles::test_create_article[long_title]", lineno=38,
        anchor='    description: "数据驱动:超长标题(500字符)"\n    body: "数据驱动:测试正文内容"\n    tag_list: []\n    expect_status: [400, 422]',
        buggy='    description: "数据驱动:超长标题(500字符)"\n    body: "数据驱动:测试正文内容"\n    tag_list: []\n    expect_status: [200, 201]',
        err_lines=[
            "E   AssertionError: [long_title] 期望 [200, 201],实际 422: {\"errors\":{\"title\":[\"is too long\"]}}",
            "E   assert 422 in [200, 201]",
        ],
        summary="AssertionError: [long_title] 期望 [200, 201],实际 422: {...}",
    ),
    _inj_case(
        title="断言错误-12 查询文章-标题字面量断言误写为 article to go",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_get_article", lineno=109,
        anchor='            assert article["title"] == "article to get"',
        buggy='            assert article["title"] == "article to go"',
        err_lines=[
            "E   AssertionError: assert 'article to get' == 'article to go'",
            "E    +  where 'article to get' = <Response [200]>.json()['article']['title']",
        ],
        summary="AssertionError: assert 'article to get' == 'article to go'",
    ),
    _inj_case(
        title="断言错误-13 删除文章-删除后查询期望 404 误写为 410",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_delete_article", lineno=144,
        anchor='            assert resp_get.status_code ==404,f"删除后查询应该404，实际{resp_get.status_code}"',
        buggy='            assert resp_get.status_code ==410,f"删除后查询应该404，实际{resp_get.status_code}"',
        err_lines=[
            "E   AssertionError: 删除后查询应该404，实际404",
            "E   assert 404 == 410",
            "E    +  where 404 = <Response [404]>.status_code",
        ],
        summary="AssertionError: 删除后查询应该404，实际404",
    ),
    _inj_case(
        title="断言错误-14 Feed 列表-期望状态码 200 误写为 201",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_feed_returns_articles", lineno=171,
        anchor='        assert resp.status_code == 200, f"期望 200,实际 {resp.status_code}: {resp.text[:200]}"',
        buggy='        assert resp.status_code == 201, f"期望 200,实际 {resp.status_code}: {resp.text[:200]}"',
        err_lines=[
            "E   AssertionError: 期望 200,实际 200: {\"articles\":[{\"slug\":\"feed-article\"}]}",
            "E   assert 200 == 201",
            "E    +  where 200 = <Response [200]>.status_code",
        ],
        summary="AssertionError: 期望 200,实际 200: {...}",
    ),
    _inj_case(
        title="断言错误-15 创建文章-期望值由数据驱动硬编码为 200",
        error_type="assertion_error", file_path=ART,
        case_name="TestArticles::test_create_article[normal_title]", lineno=83,
        anchor='        assert resp.status_code in _as_list(case["expect_status"]), (',
        buggy='        assert resp.status_code in _as_list(200), (',
        err_lines=[
            'E   AssertionError: [normal_title] 期望 200,实际 201:{"article":{"title":"数据驱动文章 1787298000"}}',
            "E   assert 201 in [200]",
            "E    +  where 201 = <Response [201]>.status_code",
            "E    +  and   [200] = _as_list(200)",
        ],
        summary="AssertionError: [normal_title] 期望 200,实际 201:{...}",
    ),
]


# ==================== 3. 超时 timeout(15 条,不注入) ====================

TIMEOUT_CASES = [
    _timeout_case(
        title="超时-01 Feed 列表读超时(localhost:8080, read timeout=10)",
        file_path=ART, case_name="TestArticles::test_feed_returns_articles", exc="ReadTimeout",
        pool_desc="HTTPConnectionPool(host='localhost', port=8080): Read timed out. (read timeout=10)",
    ),
    _timeout_case(
        title="超时-02 登录接口连接超时(127.0.0.1:8080, connect timeout=10)",
        file_path="testcases/test_login.py", case_name="TestLogin::test_login_success", exc="ConnectTimeout",
        pool_desc="HTTPConnectionPool(host='127.0.0.1', port=8080): Max retries exceeded with url: /api/users/login "
                  "(Caused by ConnectTimeoutError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>', "
                  "'Connection to 127.0.0.1 timed out. (connect timeout=10)'))",
    ),
    _timeout_case(
        title="超时-03 查询文章读超时(api.realworld.io, read timeout=30)",
        file_path=ART, case_name="TestArticles::test_get_article", exc="ReadTimeout",
        pool_desc="HTTPSConnectionPool(host='api.realworld.io', port=443): Read timed out. (read timeout=30)",
    ),
    _timeout_case(
        title="超时-04 更新文章连接超时(localhost:8080, connect timeout=5)",
        file_path=ART, case_name="TestArticles::test_update_article", exc="ConnectTimeout",
        pool_desc="HTTPConnectionPool(host='localhost', port=8080): Max retries exceeded with url: /api/articles/old-title "
                  "(Caused by ConnectTimeoutError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>', "
                  "'Connection to localhost timed out. (connect timeout=5)'))",
    ),
    _timeout_case(
        title="超时-05 创建评论读超时(192.168.1.100:8080, read timeout=15)",
        file_path="testcases/test_comments.py", case_name="TestComments::test_create_comment", exc="ReadTimeout",
        pool_desc="HTTPConnectionPool(host='192.168.1.100', port=8080): Read timed out. (read timeout=15)",
    ),
    _timeout_case(
        title="超时-06 关注用户连接超时(10.20.30.40:8080, connect timeout=8)",
        file_path="testcases/test_profiles.py", case_name="TestProfiles::test_follow_user", exc="ConnectTimeout",
        pool_desc="HTTPConnectionPool(host='10.20.30.40', port=8080): Max retries exceeded with url: /api/profiles/qa_tester/follow "
                  "(Caused by ConnectTimeoutError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>', "
                  "'Connection to 10.20.30.40 timed out. (connect timeout=8)'))",
    ),
    _timeout_case(
        title="超时-07 收藏文章读超时(localhost:8080, read timeout=10)",
        file_path="testcases/test_favorites.py", case_name="TestFavorites::test_favorite_article", exc="ReadTimeout",
        pool_desc="HTTPConnectionPool(host='localhost', port=8080): Read timed out. (read timeout=10)",
    ),
    _timeout_case(
        title="超时-08 更新用户名连接超时(api.realworld.io, connect timeout=20)",
        file_path="testcases/test_user.py", case_name="TestUser::test_update_username", exc="ConnectTimeout",
        pool_desc="HTTPSConnectionPool(host='api.realworld.io', port=443): Max retries exceeded with url: /api/user "
                  "(Caused by ConnectTimeoutError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>', "
                  "'Connection to api.realworld.io timed out. (connect timeout=20)'))",
    ),
    _timeout_case(
        title="超时-09 获取标签读超时(127.0.0.1:8080, read timeout=3)",
        file_path="testcases/test_tags.py", case_name="TestTags::test_get_tags_not_empty", exc="ReadTimeout",
        pool_desc="HTTPConnectionPool(host='127.0.0.1', port=8080): Read timed out. (read timeout=3)",
    ),
    _timeout_case(
        title="超时-10 数据驱动创建文章读超时(localhost:8080, read timeout=60)",
        file_path=ART, case_name="TestArticles::test_create_article[normal_title]", exc="ReadTimeout",
        pool_desc="HTTPConnectionPool(host='localhost', port=8080): Read timed out. (read timeout=60)",
    ),
    _timeout_case(
        title="超时-11 查询评论连接超时(172.16.0.10:8080, connect timeout=10)",
        file_path="testcases/test_comments.py", case_name="TestComments::test_get_comments", exc="ConnectTimeout",
        pool_desc="HTTPConnectionPool(host='172.16.0.10', port=8080): Max retries exceeded with url: /api/articles/slug-123/comments "
                  "(Caused by ConnectTimeoutError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>', "
                  "'Connection to 172.16.0.10 timed out. (connect timeout=10)'))",
    ),
    _timeout_case(
        title="超时-12 查询个人资料读超时(api.realworld.io, read timeout=10)",
        file_path="testcases/test_profiles.py", case_name="TestProfiles::test_get_profile_current_user", exc="ReadTimeout",
        pool_desc="HTTPSConnectionPool(host='api.realworld.io', port=443): Read timed out. (read timeout=10)",
    ),
    _timeout_case(
        title="超时-13 取消收藏连接超时(localhost:8081, connect timeout=10)",
        file_path="testcases/test_favorites.py", case_name="TestFavorites::test_unfavorite_article", exc="ConnectTimeout",
        pool_desc="HTTPConnectionPool(host='localhost', port=8081): Max retries exceeded with url: /api/articles/slug-123/favorite "
                  "(Caused by ConnectTimeoutError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>', "
                  "'Connection to localhost timed out. (connect timeout=10)'))",
    ),
    _timeout_case(
        title="超时-14 重复注册邮箱读超时(192.168.1.100:8080, read timeout=20)",
        file_path="testcases/test_user.py", case_name="TestUser::test_register_duplicate_email", exc="ReadTimeout",
        pool_desc="HTTPConnectionPool(host='192.168.1.100', port=8080): Read timed out. (read timeout=20)",
    ),
    _timeout_case(
        title="超时-15 错误密码登录读超时(localhost:8080, read timeout=10)",
        file_path="testcases/test_login.py", case_name="TestLogin::test_login_wrong_password", exc="ReadTimeout",
        pool_desc="HTTPConnectionPool(host='localhost', port=8080): Read timed out. (read timeout=10)",
    ),
]


# ==================== 4. 环境抖动 env_jitter(15 条,不注入) ====================

ENV_JITTER_CASES = [
    _env_case(
        error_type="env_jitter",
        title="环境抖动-01 登录连接拒绝(WinError 10061)",
        file_path="testcases/test_login.py", case_name="TestLogin::test_login_success",
        frame_lines=_lib_frame([
            "E   requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=8080): Max retries exceeded with url: /api/users/login "
            "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>: "
            "Failed to establish a new connection: [WinError 10061] 由于目标计算机积极拒绝，无法连接。'))",
        ]),
        summary="requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=8080): Max retries exceeded ...",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-02 查询文章网关 502 Bad Gateway",
        file_path=ART, case_name="TestArticles::test_get_article",
        frame_lines=_code_frame(ART, "TestArticles::test_get_article", 106,
            'assert resp.status_code == 200,f"期望200，实际{resp.status_code}:{resp.text[:200]}"',
            [
                "E   AssertionError: 期望200，实际502:<html><head><title>502 Bad Gateway</title></head><body><center><h1>502 Bad Gateway</h1></center></body></html>",
                "E   assert 502 == 200",
                "E    +  where 502 = <Response [502]>.status_code",
            ]),
        summary="AssertionError: 期望200，实际502:<html>...502 Bad Gateway...</html>",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-03 Feed 列表服务 503 Service Unavailable",
        file_path=ART, case_name="TestArticles::test_feed_returns_articles",
        frame_lines=_code_frame(ART, "TestArticles::test_feed_returns_articles", 171,
            'assert resp.status_code == 200, f"期望 200,实际 {resp.status_code}: {resp.text[:200]}"',
            [
                "E   AssertionError: 期望 200,实际 503: {\"error\":\"Service Unavailable\"}",
                "E   assert 503 == 200",
            ]),
        summary="AssertionError: 期望 200,实际 503: {...}",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-04 更新文章网关 504 Gateway Timeout",
        file_path=ART, case_name="TestArticles::test_update_article",
        frame_lines=_code_frame(ART, "TestArticles::test_update_article", 123,
            'assert resp.status_code == 200,f"期望200，实际{resp.status_code}:{resp.text[:200]}"',
            [
                "E   AssertionError: 期望200，实际504:{\"error\":\"Gateway Timeout\"}",
                "E   assert 504 == 200",
            ]),
        summary="AssertionError: 期望200，实际504:{...}",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-05 创建文章 SSL 证书校验失败",
        file_path=ART, case_name="TestArticles::test_create_article[normal_title]",
        frame_lines=_lib_frame([
            "E   requests.exceptions.SSLError: HTTPSConnectionPool(host='api.realworld.io', port=443): Max retries exceeded with url: /api/articles "
            "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] "
            "certificate verify failed: unable to get local issuer certificate (_ssl.c:1007)')))",
        ]),
        summary="requests.exceptions.SSLError: HTTPSConnectionPool(host='api.realworld.io', port=443) ...",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-06 创建评论连接被重置(WinError 10054)",
        file_path="testcases/test_comments.py", case_name="TestComments::test_create_comment",
        frame_lines=_lib_frame([
            "E   requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=8080): Max retries exceeded with url: /api/articles/slug-123/comments "
            "(Caused by ProtocolError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')))",
        ]),
        summary="requests.exceptions.ConnectionError: ... Remote end closed connection without response",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-07 获取标签 DNS 解析失败(getaddrinfo failed)",
        file_path="testcases/test_tags.py", case_name="TestTags::test_get_tags_returns_list",
        frame_lines=_lib_frame([
            "E   requests.exceptions.ConnectionError: HTTPConnectionPool(host='api.realwor1d.io', port=80): Max retries exceeded with url: /api/tags "
            "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>: "
            "Failed to establish a new connection: [Errno 11001] getaddrinfo failed'))",
        ]),
        summary="requests.exceptions.ConnectionError: ... [Errno 11001] getaddrinfo failed",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-08 关注用户连接中止(WinError 10053)",
        file_path="testcases/test_profiles.py", case_name="TestProfiles::test_follow_user",
        frame_lines=_lib_frame([
            "E   requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=8080): Max retries exceeded with url: /api/profiles/qa_tester/follow "
            "(Caused by ConnectionAbortedError('[WinError 10053] 你的主机中的软件中止了一个已建立的连接。'))",
        ]),
        summary="requests.exceptions.ConnectionError: ... [WinError 10053] ...",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-09 收藏文章响应传输中断(ProtocolError)",
        file_path="testcases/test_favorites.py", case_name="TestFavorites::test_favorite_article",
        frame_lines=_lib_frame([
            "E   requests.exceptions.ChunkedEncodingError: ('Connection broken: IncompleteRead(24 bytes read)', "
            "IncompleteRead(24 bytes read))",
        ]),
        summary="requests.exceptions.ChunkedEncodingError: Connection broken: IncompleteRead(24 bytes read)",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-10 查询评论分块解码错误(ChunkedEncodingError)",
        file_path="testcases/test_comments.py", case_name="TestComments::test_get_comments",
        frame_lines=_lib_frame([
            "E   requests.exceptions.ChunkedEncodingError: ('Connection broken: chunk encoding 不完整', "
            "ChunkedEncodingError('Response ended prematurely'))",
        ]),
        summary="requests.exceptions.ChunkedEncodingError: Response ended prematurely",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-11 更新用户名服务端 500 Internal Server Error",
        file_path="testcases/test_user.py", case_name="TestUser::test_update_username",
        frame_lines=_code_frame("testcases/test_user.py", "TestUser::test_update_username", 40,
            'assert resp.status_code == 200, f"期望200,实际{resp.status_code}:{resp.text[:200]}"',
            [
                'E   AssertionError: 期望200,实际500:{"timestamp":"2026-08-20T12:00:00.000+00:00","status":500,"error":"Internal Server Error","path":"/api/user"}',
                "E   assert 500 == 200",
            ]),
        summary="AssertionError: 期望200,实际500:{...Internal Server Error...}",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-12 登录不可达邮箱走代理失败(ProxyError)",
        file_path="testcases/test_login.py", case_name="TestLogin::test_login_nonexistent_email",
        frame_lines=_lib_frame([
            "E   requests.exceptions.ProxyError: HTTPSConnectionPool(host='api.realworld.io', port=443): "
            "Max retries exceeded with url: /api/users/login (Caused by ProxyError('Unable to connect to proxy', "
            "OSError(0, 'Error')))",
        ]),
        summary="requests.exceptions.ProxyError: Unable to connect to proxy",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-13 删除文章连接拒绝(端口 8081 不可达)",
        file_path=ART, case_name="TestArticles::test_delete_article",
        frame_lines=_lib_frame([
            "E   requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=8081): Max retries exceeded with url: /api/articles/slug-123 "
            "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x0000021A3F4B5C40>: "
            "Failed to establish a new connection: [WinError 10061] 由于目标计算机积极拒绝，无法连接。'))",
        ]),
        summary="requests.exceptions.ConnectionError: HTTPConnectionPool(host='localhost', port=8081) ...",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-14 查询不存在用户发生过多重定向(TooManyRedirects)",
        file_path="testcases/test_profiles.py", case_name="TestProfiles::test_get_profile_nonexistent_user",
        frame_lines=_lib_frame([
            "E   requests.exceptions.TooManyRedirects: Exceeded 30 redirects.",
        ]),
        summary="requests.exceptions.TooManyRedirects: Exceeded 30 redirects.",
    ),
    _env_case(
        error_type="env_jitter",
        title="环境抖动-15 查询文章响应体为空导致 JSON 解析失败(伴随 502)",
        file_path=ART, case_name="TestArticles::test_get_article",
        frame_lines=_code_frame(ART, "TestArticles::test_get_article", 107,
            'article = resp.json()["article"]',
            [
                "E   json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
                "E   (上游 502 返回了空响应体,resp.json() 解析失败)",
            ]),
        summary="json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
    ),
]


# ==================== 汇总:60 条 + 完整性校验 ====================

ALL_CASES = FIELD_CHANGE_CASES + ASSERTION_CASES + TIMEOUT_CASES + ENV_JITTER_CASES


def _validate(cases: list) -> None:
    """数据完整性校验:总量 60、四类各 15、标题唯一、注入组合唯一。"""
    assert len(cases) == 60, f"总量应为 60,实际 {len(cases)}"
    by_type = {}
    for c in cases:
        by_type[c["error_type"]] = by_type.get(c["error_type"], 0) + 1
    assert by_type == {"field_change": 15, "assertion_error": 15, "timeout": 15, "env_jitter": 15}, f"四类应各 15 条,实际 {by_type}"
    titles = [c["title"] for c in cases]
    assert len(set(titles)) == 60, "存在重复标题"
    inj = [(c["file_path"], c["inject_original"], c["inject_buggy"]) for c in cases if c["inject_original"]]
    assert len(set(inj)) == len(inj) == 30, "可注入场景应为 30 条且组合唯一"


def _preflight(cases: list) -> tuple:
    """预检:可注入锚点必须在目标文件中恰好出现一次(防源文件漂移)。"""
    root = Path(app_config.PYTEST_FRAMEWORK_PATH).resolve()
    ok = bad = 0
    for c in cases:
        if not c.get("inject_original"):
            continue
        p = root / c["file_path"]
        if not p.is_file():
            print(f"  [X] {c['title']}: 目标文件不存在 {c['file_path']}")
            bad += 1
            continue
        n = p.read_text(encoding="utf-8").count(c["inject_original"])
        if n == 1:
            ok += 1
        else:
            print(f"  [X] {c['title']}: 锚点在文件中匹配 {n} 次(要求唯一),请核对源文件")
            bad += 1
    return ok, bad


def main() -> None:
    _validate(ALL_CASES)

    print("===== 锚点预检(可注入场景) =====")
    ok, bad = _preflight(ALL_CASES)
    print(f"锚点可用: {ok}/30" + (f",异常: {bad}" if bad else ""))

    count = agent_db.reset_eval_cases(ALL_CASES)
    print(f"\n===== 写入数据库 =====")
    print(f"eval_cases 共写入 {count} 条(旧数据已清空)")
    for t in ("field_change", "assertion_error", "timeout", "env_jitter"):
        rows = agent_db.list_eval_cases(t)
        print(f"  {t:16s} {len(rows)} 条")

    print("\n完成。下一步: python cli/run_eval.py")


if __name__ == "__main__":
    main()

