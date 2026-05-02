# 服务器部署草案

## 适合的服务器

免费试跑：

- Oracle OCI Singapore West Always Free
- Ubuntu 22.04/24.04
- ARM 2 OCPU / 12GB RAM 足够

正式上线：

- 香港或新加坡官方云 2C2G 起步
- 数据库和 API 同机即可
- 更新包继续放 Cloudflare R2

## 端口

- 内部 API：`8080`
- PostgreSQL：只允许容器内访问，生产环境不要开放公网 `5432`
- HTTPS：建议用 Caddy 或 Nginx 反代

## 环境变量

必须修改：

```text
SURFACED_CLOUD_SECRET_KEY
SURFACED_CLOUD_DATABASE_URL
POSTGRES_PASSWORD
SURFACED_CLOUD_PUBLIC_UPDATE_BASE_URL
```

## 启动

```bash
cd cloud-server
cp .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
```

## 备份

第一版可以每天执行：

```bash
docker compose exec postgres pg_dump -U surfaced surfaced_cloud > surfaced_cloud.sql
```

正式生产建议把备份上传到 R2 `backups/`，并保留最近 7 天每日备份和最近 6 个月月度备份。

