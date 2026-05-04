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
- 公网只开放 `80` 和 `443`，不要长期开放 `8080`

## 环境变量

必须修改：

```text
SURFACED_CLOUD_SECRET_KEY
SURFACED_CLOUD_DATABASE_URL
POSTGRES_PASSWORD
SURFACED_CLOUD_PUBLIC_UPDATE_BASE_URL
SURFACED_CLOUD_EMAIL_PROVIDER
```

测试阶段可以保留：

```text
SURFACED_CLOUD_DOCS_ENABLED=true
```

正式上线前建议改为：

```text
SURFACED_CLOUD_DOCS_ENABLED=false
```

管理员自注册启用真实邮箱验证码时，需要保持：

```text
SURFACED_CLOUD_ADMIN_SELF_REGISTER_ENABLED=true
SURFACED_CLOUD_EMAIL_VERIFICATION_REQUIRED=true
```

SMTP 可接腾讯云 SES、阿里云 DirectMail 或其他支持 SMTP 的邮件服务。发信域名需要在 DNS 中配置服务商提供的 SPF/DKIM/DMARC 记录，否则验证码邮件容易进垃圾箱。

腾讯云个人认证账号不支持 SMTP 时，使用腾讯云 SES API：

```text
SURFACED_CLOUD_EMAIL_PROVIDER=tencent_ses
SURFACED_CLOUD_TENCENT_SES_SECRET_ID=腾讯云 API SecretId
SURFACED_CLOUD_TENCENT_SES_SECRET_KEY=腾讯云 API SecretKey
SURFACED_CLOUD_TENCENT_SES_REGION=ap-hongkong
SURFACED_CLOUD_TENCENT_SES_ENDPOINT=ses.tencentcloudapi.com
SURFACED_CLOUD_TENCENT_SES_FROM=Surfaced <no-reply@mail.surfacedlab.com>
SURFACED_CLOUD_TENCENT_SES_TEMPLATE_ID=腾讯云邮件模板 ID
```

验证码邮件模板需要在腾讯云邮件推送里创建并审核通过，模板类型选择“触发类”，模板变量使用 `{{code}}`。示例正文：

```text
你的 Surfaced 管理账号验证码是：{{code}}
验证码 10 分钟内有效。若非本人操作，请忽略这封邮件。
```

如果以后改用企业账号 SMTP，把 `SURFACED_CLOUD_EMAIL_PROVIDER` 改为 `smtp`，再填写 `SURFACED_CLOUD_SMTP_HOST`、`SURFACED_CLOUD_SMTP_USERNAME`、`SURFACED_CLOUD_SMTP_PASSWORD`、`SURFACED_CLOUD_SMTP_FROM`。

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
