# Surfaced Cloud Server

Surfaced 的云端服务应当作为独立项目维护，不和本地端的 `web_backend.py` 混在一起。这个目录只负责云端 API、账号权限、数据同步、文章归类共享、引用排名聚合、天气共享和更新包清单。

## 当前边界

- 本地端继续执行浏览器、平台登录、爬虫、截图和调度。
- 云端只接收结构化运行数据，不接收截图文件。
- 数据归属跟随品牌任务，不跟随某个普通账号。
- 多个管理员对应多个 workspace，workspace 之间隔离。
- 普通账号分为 `operator` 和 `viewer`。
- 更新包文件放 Cloudflare R2，云端只保存版本、hash、签名和下载地址。

## 目录结构

```text
cloud-server/
  app/
    api/              # HTTP 路由
    core/             # 配置、安全、通用工具
    db/               # 数据库连接
    services/         # 业务服务
    main.py           # FastAPI 入口
    models.py         # SQLAlchemy 数据模型
    schemas.py        # Pydantic API 模型
  alembic/            # 数据库迁移
  docker-compose.yml  # 服务器部署参考
  Dockerfile
  requirements.txt
  .env.example
```

## 本地运行

```bash
cd cloud-server
cp .env.example .env
docker compose up -d postgres
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

打开：

```text
http://127.0.0.1:8080/docs
```

## 服务器部署

最小服务器建议：

- 2C2G 起步，Ubuntu 22.04/24.04。
- Docker Compose 部署 `api + postgres`。
- R2 放安装包、更新包和备份。
- 后期数据量增长后，PostgreSQL 可以单独迁到云数据库或独立磁盘。

```bash
cd cloud-server
cp .env.example .env
# 修改 .env 中的 SECRET_KEY、POSTGRES_PASSWORD、数据库地址等
docker compose up -d --build
docker compose exec api alembic upgrade head
```

生产环境必须把 `.env` 里的 `SURFACED_CLOUD_SECRET_KEY` 和 `POSTGRES_PASSWORD` 改成高强度随机值。
测试完成后，可以把 `.env` 里的 `SURFACED_CLOUD_DOCS_ENABLED` 改成 `false`，然后重启服务以关闭公网 Swagger 文档。

## 第一阶段 API

- `GET /health`
- `POST /api/v1/auth/admin/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/me`
- `GET /api/v1/admin/users`
- `POST /api/v1/admin/users`
- `PATCH /api/v1/admin/users/{user_id}`
- `DELETE /api/v1/admin/users/{user_id}`
- `GET /api/v1/admin/tasks`
- `POST /api/v1/admin/tasks`
- `POST /api/v1/admin/tasks/{task_id}/members`
- `DELETE /api/v1/admin/tasks/{task_id}/members/operator`
- `POST /api/v1/sync/events`
- `GET /api/v1/tasks/{task_id}/article-reference-ranking`
- `GET /api/v1/tasks/{task_id}/run-records`
- `GET /api/v1/updates/manifest`

## 后续接入本地端

本地端后续只新增 `CloudClient` 层：

- 登录后保存 access token / refresh token / workspace 信息。
- 本地运行产生 outbox 事件。
- 网络恢复后批量上传事件。
- 按 cursor 拉取任务、权限、天气、归类结果和更新信息。
- 账号被管理员禁用或删除后，云端返回 401，本地清空登录状态。
