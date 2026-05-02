# Surfaced 云端实现规划

这份规划对应当前代码库，而不是通用 SaaS 模板。当前本地端已经有运行历史、文章录入、引用排名和更新校验能力，云端第一阶段应当做“可靠同步与权限中心”，不要把本地浏览器执行链路搬上云。

## 关键判断

1. 云端单独放在 `cloud-server/`，和本地 `web_backend.py` 分离。
2. 本地继续负责浏览器、平台登录、截图、爬虫和人工介入。
3. 云端负责账号、workspace 隔离、任务权限、运行数据、文章关系、引用事件、天气和更新清单。
4. 上传数据必须是事件化和幂等的，不能沿用整包文件覆盖式同步。
5. 品牌任务必须使用云端稳定 ID，本地 `derive_task_id()` 只能作为迁移期兼容字段。

## 账号与权限

- `admin`：自行邮箱注册，创建自己的 workspace。
- `operator`：由管理员创建，能承接被分配任务、上传运行数据。
- `viewer`：由管理员创建，只能看被分配任务的数据展示。

管理员不是全局超级管理员，只能管理自己 workspace 下的数据。

管理员删除或禁用普通账号时：

- `users.enabled=false` 或删除用户。
- `token_version` 递增或 token 关联记录失效。
- 本地端下一次请求云端返回 `401` 后清空登录态。

## 任务分配

不要用单一 `brand_tasks.owner_id` 表示归属。更稳定的设计是：

- `brand_tasks` 保存任务本体。
- `task_members` 保存账号对任务的 `operate` / `view` 权限。
- `task_assignment_events` 记录任务流转审计。

当任务从 A 运营账号转给 B：

- `brand_tasks.id` 不变。
- 历史 `run_records`、`articles`、`article_task_links`、`article_reference_events` 都仍然绑定同一任务。
- A 的 `operate` 权限移除。
- B 获得 `operate` 权限。
- 旧数据自然跟随任务展示给 B。

## 文章归类

本地端现在的文章模块以 `logs/articles.json` 为主，里面包含匹配任务、未匹配原因、媒体类型等字段。云端不能只在 `articles` 表上放一个 `task_id`，否则后面会遇到：

- 一篇文章关联多个品牌。
- 本地归类失败后云端重新归类。
- 管理员确认后不能被自动规则覆盖。
- 品牌任务改名或转移账号。

因此云端拆为：

- `articles`：文章本体，按 URL 去重。
- `article_task_links`：文章与品牌任务的多对多关系。
- `classification_jobs`：本地归类失败或低置信度文章的云端重归类队列。

第一版归类规则不引入 AI：

1. 管理员确认结果优先级最高。
2. 云端确定规则其次。
3. 本地自动匹配结果再次。
4. 仍失败则进入 unresolved 队列。

## 引用排名

本地已有 `article_ref_weight_v1`。云端不应上传本地排名缓存，而应该上传引用事件：

- 引用文章 URL
- 归一化 URL hash
- 平台
- 日期
- 来源运行记录 key
- 关联任务 ID

云端用统一算法聚合，保证管理员、运营和浏览账号看到同一套排名结果。

当前骨架中的 `/api/v1/sync/tasks/{task_id}/article-reference-ranking` 是临时最小聚合版本，后续需要对齐本地 `core/article_reference_ranking.py` 的完整权重算法。

## 天气数据

天气由管理员填写后共享给 workspace 内普通账号：

- `weather_data.workspace_id`
- `weather_data.date`
- `weather_data.data_json`

后续在 pull 同步里按账号可见任务和日期范围下发。

## 更新包

R2 只放文件：

- `installers/`
- `updates/`
- `manifests/`
- `backups/`

云端数据库保存：

- 版本号
- channel
- platform key
- R2 下载 URL
- sha256
- signature
- notes

客户端必须继续做 hash / signature 校验。

## 推荐阶段

### 阶段 1：云端骨架

- FastAPI 服务
- PostgreSQL 数据库
- 管理员注册登录
- 普通账号管理
- 品牌任务创建与分配
- sync event 接收
- 更新 manifest 输出

### 阶段 2：本地 CloudClient

- 登录态保存
- refresh token
- 401 自动退出
- outbox 事件队列
- cursor pull

### 阶段 3：文章与引用接入

- 上传文章本体
- 上传 article_task_links
- 上传 classification_jobs
- 上传 article_reference_events
- 云端引用排名接口对齐本地算法

### 阶段 4：管理员后台

- 账号管理
- 品牌任务分配
- 未归类文章处理
- 天气填写
- 更新包登记

### 阶段 5：服务器生产化

- Nginx / Caddy 反代 HTTPS
- 数据库备份到 R2
- 日志轮转
- 监控健康检查
- 邮件服务接入

