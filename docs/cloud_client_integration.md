# Surfaced 本地端云同步接入记录

当前阶段已接入本地端云同步基础能力和 Web 调试/管理入口。

## 新增模块

- `core/cloud_client.py`
  - 访问 Surfaced Cloud Server。
  - 支持登录、刷新 token、退出、批量上传 sync events、查询任务运行记录。

- `core/cloud_session_store.py`
  - 本地保存云端登录态。
  - 文件位置：`user_data/cloud_session.json`。
  - 不写入 `config.yaml`，避免被旧整包同步或配置导出带走。

- `core/cloud_outbox.py`
  - 本地待上传事件队列。
  - 文件位置：`user_data/cloud_outbox.json`。
  - 通过 `idempotency_key` 去重。

- `core/cloud_run_sync.py`
  - 把本地历史记录转换成云端 `run_record` 事件。
  - 只在显式存在 `cloud_task_id` 时入队，避免把本地 legacy task id 误当成云端任务 ID。

- `core/cloud_task_sync.py`
  - 拉取当前云端账号可见的品牌任务。
  - 合并到本地 `config.yaml` 的 `tasks`，写入 `cloud_task_id`、`cloud_task_key`、`cloud_access_level`。
  - 浏览账号下发的任务默认设为本地不可运行，只保留展示/后续读取能力。
  - 原本云端下发、但当前账号已经不可见的任务会被本地禁用并标记 `cloud_access_level: revoked`。

## 本地 Web Backend 调试接口

- `GET /api/cloud/status`
- `POST /api/cloud/login`
- `POST /api/cloud/logout`
- `POST /api/cloud/flush-outbox`
- `POST /api/cloud/pull-tasks`

这些接口仍受本地 `X-Surfaced-Session-Token` 保护。

## 本地运行记录上传规则

`record_result_history()` 写入本地历史后，会尝试把记录加入云端 outbox。

入队条件：

- `result.cloud_task_id` 或 `result.cloudTaskId` 是正整数。
- 或本地任务 ID 是 `cloud_<云端任务ID>`。
- 或运行器从任务配置上的 `cloud_task_id` 传入。

不会上传：

- 截图路径
- 截图文件
- 手动测试运行里的失败/无命中记录。它们不写本地历史，避免污染趋势和日报。

会上传：

- 平台
- 关键词
- 品牌
- rank / success
- error_message
- run_started_at
- references / body_references

## 下一步

1. 在设置页增加云端登录、拉取任务、立即上传的操作面板。
2. 增加任务列表里的云端状态标识：运营 / 浏览 / 已收回。
3. 增加后台定时同步线程，网络恢复后自动上传 outbox。
4. 增加云端文章归类失败队列的确定性规则处理。
5. 管理员删除/禁用账号后，刷新 token 失败时清空本地登录态。
