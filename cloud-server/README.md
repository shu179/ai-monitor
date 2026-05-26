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

### Cloud Sync v2 smoke test

在一次性测试库上验证 v1 `/sync/events` 已经合流到 v2 队列、worker 能消费、旧 `sync_events` 镜像和 `workspace_change_log` 都会写入：

```bash
cd cloud-server
docker compose up -d postgres
alembic upgrade head
SURFACED_CLOUD_ALLOW_SMOKE=1 python -m scripts.smoke_sync_v2
```

也可以通过 pytest 入口运行同一条链路：

```bash
SURFACED_CLOUD_RUN_POSTGRES_SMOKE=1 \
SURFACED_CLOUD_ALLOW_SMOKE=1 \
python -m pytest tests/test_sync_v2_postgres_smoke.py -q
```

这条 smoke 会直接向当前 `SURFACED_CLOUD_DATABASE_URL` 指向的数据库写入临时 workspace/user/task/run 记录，只能用于可丢弃的本地或测试库。

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
```

`docker compose` 会先运行一次 `migrate` 服务执行 `alembic upgrade head`，成功后再启动 `api` 和 `worker`。`api` 默认用 gunicorn + uvicorn worker 多进程运行，`worker` 独立消费 Cloud Sync v2 队列，避免后台 materialize 抢占 API 进程。

关键运行参数：

```text
SURFACED_CLOUD_API_WORKERS=2
SURFACED_CLOUD_DB_POOL_SIZE=5
SURFACED_CLOUD_DB_MAX_OVERFLOW=10
SURFACED_CLOUD_DB_STATEMENT_TIMEOUT_MS=5000
SURFACED_CLOUD_WORKER_DB_STATEMENT_TIMEOUT_MS=60000
SURFACED_CLOUD_WORKER_BATCH_LIMIT=100
```

API 保持 5s statement timeout，worker 使用 60s statement timeout；迁移进程不设置 statement timeout。生产多实例部署时建议再加 PgBouncer transaction pooling，避免 `API workers × pool_size + worker pool` 把 Postgres 连接数打满。

Cloud Sync v2 的对象上传接口走 S3/R2-compatible presigned URL。生产优先使用 R2 + CDN 域名作为 endpoint，下行 URL 不从 API 服务器转发大文件：

```text
SURFACED_CLOUD_OBJECT_STORAGE_ENDPOINT_URL=https://<cdn-or-r2-endpoint>
SURFACED_CLOUD_OBJECT_STORAGE_BUCKET=<bucket>
SURFACED_CLOUD_OBJECT_STORAGE_REGION=auto
SURFACED_CLOUD_OBJECT_STORAGE_ACCESS_KEY_ID=<access-key>
SURFACED_CLOUD_OBJECT_STORAGE_SECRET_ACCESS_KEY=<secret-key>
SURFACED_CLOUD_OBJECT_STORAGE_FORCE_PATH_STYLE=false
SURFACED_CLOUD_OBJECT_STORAGE_WORKSPACE_QUOTA_BYTES=107374182400
```

对象策略固定为：`<=32KB` 内联、`32KB-5MB` 单 PUT、`>5MB` multipart，storage key 为 `<workspace_id>/<sha[:2]>/<sha[2:4]>/<sha>`。`sha256` 始终是未压缩内容 hash，`storage_size_bytes` 记录实际存储大小。

如果暂时不接 R2/COS/S3，服务会自动回退到服务器本地磁盘对象存储。当前小盘服务器推荐保守上限：

```text
SURFACED_CLOUD_OBJECT_STORAGE_LOCAL_DIR=/opt/surfaced/object-data
SURFACED_CLOUD_OBJECT_STORAGE_TOTAL_QUOTA_BYTES=10737418240
SURFACED_CLOUD_OBJECT_STORAGE_WORKSPACE_QUOTA_BYTES=5368709120
SURFACED_CLOUD_OBJECT_STORAGE_MAX_FILE_BYTES=536870912
SURFACED_CLOUD_OBJECT_STORAGE_MIN_FREE_BYTES=8589934592
```

这组默认值表示对象总量最多 10GB、单 workspace 最多 5GB、单文件最多 512MB，并且上传后磁盘剩余空间低于 8GB 时直接拒绝。`docker-compose.yml` 会把 `/opt/surfaced/object-data` 挂进 API 和 worker 容器，避免容器重建后对象文件丢失。本地磁盘模式只支持服务端直传单 PUT，不启用 multipart；后续接 R2/COS 时再切回 presigned multipart。

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
- `GET /api/v2/capabilities`
- `POST /api/v2/sync/batches`
- `POST /api/v2/sync/state-delta`
- `POST /api/v2/objects/uploads`
- `POST /api/v2/objects/uploads/{session_id}/parts:presign`
- `POST /api/v2/objects/uploads/{session_id}/parts`
- `POST /api/v2/objects/uploads/{session_id}:complete`
- `POST /api/v2/objects/{object_id}:download`
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
