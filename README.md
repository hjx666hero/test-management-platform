# 测试管理平台 (Test Management Platform)

> 基于 FastAPI + Vue 3 的接口自动化测试管理平台,将 pytest 接口自动化框架作为「库」直接调用执行用例,支持任务编排、标签筛选、Allure 报告与通义千问 AI 根因分析。

## 项目介绍

本平台与配套的 [pytest-realworld-framework](https://github.com/hjx666hero/pytest-realworld-framework)(接口自动化测试框架)形成完整闭环:

- **框架侧**:负责「怎么测」——四层架构、数据驱动、数据库精准校验、CI/CD、性能压测
- **平台侧**:负责「管什么、结果去哪」——测试任务管理、异步执行、报告沉淀、AI 辅助缺陷定位

平台核心亮点:

1. **不依赖命令行**:不通过 `subprocess` 调 pytest,而是将框架目录加入 `sys.path`,直接在 Python 代码中 `import api/` 与 `utils/db_util.py`,实例化接口类调用方法执行用例——pytest 被当作普通库使用。
2. **异步任务执行**:基于 `fastapi.BackgroundTasks` 后台执行,创建任务后立即返回,执行状态(排队/执行中/通过/失败)实时更新到 MySQL,前端轮询展示。
3. **数据库精准校验**:用例内直接调用框架的 `fetch_one` 查库断言,验证接口响应与数据库落库值一致(如创建文章后校验 `articles` 表字段)。
4. **Allure 报告解析**:执行结果为每条用例生成 Allure 标准 `*-result.json`,报告解析服务按此提取失败用例,与 Allure 生态完全兼容。
5. **AI 根因分析**:失败用例的请求/响应快照自动发送给通义千问,生成根因分析与修复建议,存入数据库并在前端展示。
6. **灵活的任务筛选**:创建任务时按「环境 + 标签(P0/P1/P2、模块)」选择用例范围,空标签 = 全量执行。

## 架构设计

```
┌──────────────────────────────────────────────────────────────┐
│                    前端 Vue3 (5173)                            │
│  任务列表 │ 创建任务(环境+标签) │ 报告详情(失败列表+AI分析)      │
└──────────────────────────┬───────────────────────────────────┘
                           │ /api (Vite 代理)
┌──────────────────────────▼───────────────────────────────────┐
│               后端 FastAPI (8000)                              │
│  ┌────────────┐ ┌──────────────┐ ┌──────────────────────────┐ │
│  │ 任务路由     │ │ 异步执行器    │ │ Allure 解析 + AI 分析     │ │
│  │ tasks.py   │ │ executor.py  │ │ allure_parser/ai_analyzer│ │
│  └────────────┘ └──────┬───────┘ └──────────────────────────┘ │
│                         │ 直接 import(无 subprocess)          │
│  ┌──────────────────────▼───────────────────────────────────┐ │
│  │  项目一 pytest-realworld-framework(sys.path 注入)          │ │
│  │  api/*.py(接口层)  utils/request_util.py  utils/db_util.py│ │
│  └──────────────────────┬───────────────────────────────────┘ │
│                         │ HTTP                          │ SQL │
└─────────────────────────┼───────────────────────────────┼─────┘
                          ▼                               ▼
                  被测后端 (8080)                      MySQL (3307)
```

## 技术栈

| 层级 | 技术 | 用途 |
|---|---|---|
| 前端 | Vue 3 + Vite | SPA 框架与构建 |
| 前端 | Element Plus | UI 组件库(表格/表单/抽屉/弹窗) |
| 前端 | Vue Router + Pinia | 路由与状态管理 |
| 前端 | Axios | HTTP 请求(Vite 代理 `/api` → 8000) |
| 后端 | FastAPI | Web 框架与自动 API 文档 |
| 后端 | SQLAlchemy 2.x + PyMySQL | ORM 与 MySQL 驱动 |
| 后端 | Uvicorn | ASGI 服务器 |
| 后端 | FastAPI BackgroundTasks | 异步任务执行 |
| AI | 通义千问(DashScope) | 失败用例根因分析 |
| 外部 | 被测后端(RealWorld SpringBoot) | 被测试系统 |
| 外部 | MySQL | 平台数据 + 被测库(共用实例) |

## 项目结构

```
test-management-platform/
├── backend/                       # FastAPI 后端
│   ├── app/
│   │   ├── config.py              # 平台配置(DB/框架路径/通义千问/CORS)
│   │   ├── database.py            # SQLAlchemy 引擎 + 自动建库建表
│   │   ├── models.py              # TestTask / TestCaseResult ORM
│   │   ├── schemas.py             # Pydantic 请求/响应模型
│   │   ├── main.py                # 应用入口(路由/CORS/建表)
│   │   ├── routers/
│   │   │   └── tasks.py           # 任务 CRUD + 报告详情 API
│   │   └── services/
│   │       ├── executor.py        # 核心执行器(import 框架 api/db_util 跑用例)
│   │       ├── allure_parser.py   # 解析 Allure result JSON 提取失败用例
│   │       └── ai_analyzer.py     # 通义千问根因分析 + 落库
│   ├── requirements.txt
│   ├── run.py                     # 后端启动入口
│   └── .env.example               # 环境变量模板
└── frontend/                      # Vue3 前端
    ├── vite.config.js             # 开发代理 /api → 127.0.0.1:8000
    └── src/
        ├── api/index.js           # Axios 封装
        ├── router/index.js        # 路由
        └── views/
            ├── TaskList.vue       # 任务列表(状态/通过率/操作)
            ├── CreateTask.vue     # 创建任务(环境 + 标签)
            └── ReportDetail.vue   # 报告详情(失败列表 + 请求响应快照 + AI 分析)
```

## 快速开始

### 环境要求

| 组件 | 要求 |
|---|---|
| Python | 3.10+ |
| Node.js | 18+ |
| MySQL | 8.x(复用项目一所在实例) |
| 被测后端 | 已启动的 RealWorld 后端(`http://localhost:8080/api`) |

### 第 1 步:准备被测环境

平台执行用例需要被测后端与 MySQL 可用。若使用项目一的 `docker-compose`:

```powershell
cd F:\pytest-realworld-framework
docker compose up -d --build
# 等待后端就绪:curl http://localhost:8080/api/tags 返回 200
```

### 第 2 步:配置后端

```powershell
cd F:\test-management-platform\backend
copy .env.example .env
# 编辑 .env,至少确认:
#   TMS_DB_HOST / TMS_DB_PORT / TMS_DB_USER / TMS_DB_PASSWORD
#   PYTEST_FRAMEWORK_PATH=F:/pytest-realworld-framework
#   TMS_DEFAULT_BASE_URL=http://localhost:8080/api
#   DASHSCOPE_API_KEY=<你的通义千问 key>(不填则跳过 AI 分析)
```

安装依赖并启动:

```powershell
pip install -r requirements.txt
python run.py          # 后端启动于 http://127.0.0.1:8000
# 验证:http://127.0.0.1:8000/api/health 返回 {"status":"ok","ai_enabled":true}
```

> 自动建库:启动时若 `test_platform` 数据库不存在会自动创建,并幂等建表。

### 第 3 步:启动前端

```powershell
cd F:\test-management-platform\frontend
npm install
npm run dev            # 前端启动于 http://localhost:5173
```

### 第 4 步:创建并执行任务

1. 打开 http://localhost:5173,进入「创建任务」
2. 填写任务名(如 `全量回归-0805`)
3. 选择环境(默认本地 `http://localhost:8080/api`)
4. 勾选用例标签(P0/P1/P2 或模块);不勾选 = 执行全部 11 条用例
5. 点击「创建并执行」→ 自动跳转报告页,前端每 3 秒轮询刷新状态

### 第 5 步:查看报告与 AI 分析

- 报告页展示:任务状态、通过率、耗时、用例结果列表
- 点击失败用例「详情」→ 抽屉展示:
  - **错误信息**(断言/异常详情)
  - **请求快照**(method/url/payload)
  - **响应快照**(status_code/body)
  - **AI 根因分析**(通义千问生成的根因 + 修复建议,绿色高亮)

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查(含 `ai_enabled`) |
| `POST` | `/api/tasks` | 创建任务(异步执行),Body: `{name, env_url, tags}` |
| `GET` | `/api/tasks` | 任务列表(新→旧,最多 100) |
| `GET` | `/api/tasks/{id}` | 任务详情 |
| `GET` | `/api/tasks/{id}/report` | 报告详情(任务汇总 + 全部用例结果 + AI 分析) |

交互式文档:启动后端后访问 `http://127.0.0.1:8000/docs`(FastAPI 自动生成)。

## 内置用例(11 条)

| 模块 | 用例 | 标签 |
|---|---|---|
| 用户 | 登录-正确账号返回 Token | user, P0 |
| 用户 | 注册-重复邮箱返回 4xx | user, P1 |
| 文章 | 创建并落库后清理(含 DB 校验) | articles, P0 |
| 文章 | 列表返回 200 | articles, P0 |
| 文章 | 更新标题与正文 | articles, P1 |
| 文章 | 删除后查询 404 | articles, P0 |
| 文章 | Feed 未登录应 401 | articles, P1 |
| 标签 | 列表非空 | tags, P2 |
| Profile | 关注后取关 | profiles, P1 |
| 评论 | 增删评论 | comments, P1 |
| 收藏 | 收藏后取消 | favorites, P2 |

## 设计要点

1. **框架作为库复用**:执行器不复制框架代码,通过 `sys.path` 注入后直接 import,平台升级即框架升级,单一事实来源。
2. **多环境支持**:通过「替换 `client.base_url`」绕开框架 config 模块级常量,同一后端实例可对任意被测环境发起测试。
3. **失败快照闭环**:执行器包装 HttpClient 自动记录请求/响应 → 落库 → 解析 → AI 分析 → 前端展示,失败定位全链路无需人工干预。
4. **AI 优雅降级**:未配置 `DASHSCOPE_API_KEY` 时跳过 AI 分析,不影响任务执行与报告生成。

## 关联项目

- [pytest-realworld-framework](https://github.com/hjx666hero/pytest-realworld-framework):本平台执行用例所依赖的接口自动化测试框架(四层架构、数据驱动、数据库校验、CI/CD、性能压测)。

## License

MIT
