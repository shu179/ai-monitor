# Article SQLite Rollout

本文档用于 SQLite 文章主存储收口后的运维、回滚和定位。默认目标是让普通用户能先恢复可用，开发者再看诊断细节。

## 默认行为

- 未设置 `AIBRANDMONITOR_ARTICLE_STORE_BACKEND` 时，文章主存储默认是 `auto`。
- `auto` 会先检查 `logs/article_store.sqlite3` 的 schema、导入元信息和 `logs/articles.json` 签名。
- SQLite 缺失、损坏、签名过期或处于错误冷却时，运行时会回退到 JSON，并按 guard 后台迁移或修复 SQLite。
- 后台主存储迁移有 running guard 和最小间隔 guard，避免普通请求重复启动重建。

## SQLite 何时生效

SQLite 会成为主存储，当且仅当：

- `logs/article_store.sqlite3` 存在且 schema ready。
- SQLite 保存的 JSON source signature 或 file signature 与当前 `articles.json` 一致。
- 运行时健康状态没有进入 SQLite 错误冷却。

强制指定 SQLite 可用：

```bash
AIBRANDMONITOR_ARTICLE_STORE_BACKEND=sqlite python3 main.py
```

通常不要直接强制 SQLite；让默认 `auto` 完成检查和修复即可。

## JSON 回滚开关

如果用户启动后文章页异常、SQLite 诊断异常，先强制 JSON，保证可用：

```bash
AIBRANDMONITOR_ARTICLE_STORE_BACKEND=json python3 main.py
```

文章页 SQLite 影子读也可单独关闭：

```bash
AIBRANDMONITOR_ARTICLE_READ_BACKEND=json python3 main.py
AIBRANDMONITOR_ARTICLE_READ_BACKEND=off python3 main.py
AIBRANDMONITOR_ARTICLE_READ_BACKEND=disabled python3 main.py
```

这些开关不会删除 SQLite 文件，只会让读写路径回到 JSON 或跳过 SQLite 影子读。

## Doctor 检查

检查当前账号数据：

```bash
python3 scripts/article_store_doctor.py --use-current-data --check-only
```

检查指定数据目录：

```bash
python3 scripts/article_store_doctor.py --data-dir /path/to/data-dir --check-only
```

重点看：

- `status`: `healthy` 表示无需修复。
- `backend.effective_backend`: 期望为 `sqlite`。
- `backend.fallback_reason`: 空字符串表示没有回退原因。
- `db.readiness.ready`: 期望为 `true`。
- `freshness.fresh`: 期望为 `true`。
- `match_refresh_job.status`: 后台文章匹配刷新状态，常见值为 `idle`、`running`、`finished`、`error`、`interrupted`。
- `exit_code`: `0` 健康；`2` 可修复回退；`1` 严重错误。

## 修复和校验

从 JSON 重建 SQLite：

```bash
python3 scripts/article_store_doctor.py --use-current-data --rebuild-sqlite
```

抽样校验 JSON 和 SQLite：

```bash
python3 scripts/article_store_doctor.py --use-current-data --verify-json-sqlite --sample-size 100
```

全量校验：

```bash
python3 scripts/article_store_doctor.py --use-current-data --verify-json-sqlite --full-verify
```

导出 SQLite 到 JSON 备份：

```bash
python3 scripts/article_store_doctor.py \
  --use-current-data \
  --export-sqlite-json /path/to/articles-backup.json
```

如果输出文件已存在，确认要覆盖时再加 `--force`。

## 后台 match refresh

SQLite 主存储下，文章匹配刷新会优先通过后台 job 增量处理 dirty 文章，避免普通请求同步跑全量匹配。运行状态写在 SQLite meta 中，可通过 doctor 的 `match_refresh_job` 或 `/api/history-storage/status` 里的 `articleStore.match_refresh_job` 查看。

低性能或 smoke 环境可以用这两个环境变量放慢后台 job，验证 batch、sleep 和进度统计：

```bash
AIBRANDMONITOR_ARTICLE_MATCH_REFRESH_BATCH_SIZE=50
AIBRANDMONITOR_ARTICLE_MATCH_REFRESH_SLEEP_SECONDS=0.05
```

`article_scale_benchmark.py --wait-background-refresh` 会等待后台 job 完成，并在 `operations[].name == "schedule_background_match_refresh"` 中输出 `scheduled`、`status`、`processed_count`、`analyzed_count`、`updated_count`、`batch_size`、`sleep_seconds` 和 fd delta。

云同步上传文章快照时，如果匹配刷新刚被调度或已有 job 在跑，会先 deferred 并稍后重试；不会把旧的 `matched_tasks` 当作 fresh 结果上传。

## 10 万 benchmark

验收 auto 模式主存储、SQLite 影子分页、CRUD 和 match refresh：

```bash
python3 scripts/article_scale_benchmark.py \
  --count 100000 \
  --article-store-backend auto \
  --data-dir /private/tmp/ai-monitor-article-auto-100k \
  --force \
  --output /private/tmp/ai-monitor-article-auto-100k/summary.json
```

随后检查同一数据目录：

```bash
python3 scripts/article_store_doctor.py \
  --data-dir /private/tmp/ai-monitor-article-auto-100k \
  --check-only
```

benchmark 重点看：

- `rollout_guard.initial_effective_backend` 和 `final_effective_backend`。
- `rollout_guard.migration_completed`。
- `rollout_guard.page_read_timings_ms`。
- `rollout_guard.crud_timings_ms`。
- `rollout_guard.refresh_timings_seconds`。
- `standards.sqlite_fd_growth.max_sqlite_fd_delta`。
- `rollout_guard.doctor.status` 和 `rollout_guard.doctor.exit_code`。

## 出问题时怎么处理

普通用户优先恢复可用：

1. 用 `AIBRANDMONITOR_ARTICLE_STORE_BACKEND=json python3 main.py` 启动。
2. 如果只是文章列表页异常，再加 `AIBRANDMONITOR_ARTICLE_READ_BACKEND=json`。
3. 运行 doctor，把完整输出交给开发者。
4. 在开发者确认前，不要手动删除 `logs/articles.json` 或 SQLite 文件。

开发者定位顺序：

1. 跑 `article_store_doctor.py --use-current-data --check-only`。
2. 看 `/api/history-storage/status` 里的 `articleStore`、`articles` 和 `shadowDb`。
3. 如果 `fallback_reason` 是 stale 或 missing，先用 `--rebuild-sqlite` 修复。
4. 如果 doctor 显示 JSON 不可读，先保留现场，再考虑从 SQLite `--export-sqlite-json` 备份。
5. 如果发现 fd 增长，复查新增 SQLite 调用是否使用 context manager 或 finally close。
6. 如果普通文章页变慢，确认 `AIBRANDMONITOR_ARTICLE_READ_BACKEND` 没有被关闭，并检查 SQLite 影子索引 freshness。
