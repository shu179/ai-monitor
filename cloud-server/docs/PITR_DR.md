# PostgreSQL PITR 与 DR 演练

本项目当前按“先用服务器本地资源”落地：对象数据、普通备份、WAL 归档都放在服务器 `/opt/surfaced/*`。这不是跨机房灾备，但可以覆盖误删、坏迁移、误操作后的时间点恢复。以后接 R2/S3 时，再把 `/opt/surfaced/backups` 和 `/opt/surfaced/postgres-wal` 异步复制到外部存储。

## 目录

```text
/opt/surfaced/backups       # pg_dump 与 pg_basebackup
/opt/surfaced/postgres-wal  # PostgreSQL archive_command 写入的 WAL
```

`docker-compose.yml` 已为 Postgres 打开：

```text
wal_level=replica
archive_mode=on
archive_timeout=60s
archive_command=test -d /opt/surfaced/postgres-wal && (test ! -f /opt/surfaced/postgres-wal/%f && cp %p /opt/surfaced/postgres-wal/%f || test -f /opt/surfaced/postgres-wal/%f)
```

首次启动前在服务器上创建目录，并确保 Postgres 容器用户可写：

```bash
sudo install -d -m 700 -o 999 -g 999 /opt/surfaced/postgres-wal
sudo install -d -m 700 -o 999 -g 999 /opt/surfaced/backups
```

## 日常备份

每天保留一份逻辑备份：

```bash
cd /home/ubuntu/cloud-server
SURFACED_CLOUD_ALLOW_BACKUP=1 docker compose exec api \
  python scripts/backup_postgres.py
```

每天或每周保留一份 PITR base backup：

```bash
cd /home/ubuntu/cloud-server
SURFACED_CLOUD_ALLOW_BASEBACKUP=1 docker compose exec api \
  python scripts/basebackup_postgres.py
```

建议用 `cron` 在低峰执行，并定期确认磁盘空间。当前服务器总容量约 40GB，应用侧对象上传已经要求至少 8GB 空闲；WAL 和 base backup 也必须纳入同一个空间预算。

## DR 演练流程

演练只在临时恢复目录或临时服务器执行，不在生产主库上直接覆盖。

1. 记录目标恢复时间，例如 `2026-05-29 02:30:00+08`。
2. 停止临时恢复 Postgres，不停止生产主库。
3. 解开最近一次 `basebackup-*` 中的 `base.tar.gz` 到临时数据目录。
4. 写入 `postgresql.auto.conf`：

```text
restore_command = 'cp /opt/surfaced/postgres-wal/%f %p'
recovery_target_time = '2026-05-29 02:30:00+08'
recovery_target_action = 'pause'
```

5. 在临时数据目录创建 `recovery.signal`。
6. 用临时端口启动 Postgres，确认日志进入 recovery，并在目标时间点 pause。
7. 抽查核心数据：用户数、workspace 数、文章数、sync queue 深度、change log 最新 seq。
8. 记录 RTO/RPO：
   - RTO：从开始恢复到临时库可查询的时间。
   - RPO：目标时间点与最后可恢复事务时间的差距。
9. 演练通过后销毁临时恢复目录。

## 真故障切换

1. 先保留故障现场：不要删除旧 volume。
2. 在新数据目录按上面的 PITR 流程恢复到目标时间点。
3. 抽查通过后，把 `docker-compose.yml` 的 Postgres volume 指向新数据目录，或在新机器上切 DNS/反代。
4. API 启动后先跑：

```bash
docker compose exec api python scripts/sync_queue_doctor.py
docker compose exec api python scripts/storage_doctor.py
docker compose exec api python scripts/shadow_reconcile.py
```

5. 确认 v1/v2 同步、state-delta、对象本地盘路径均正常后再恢复用户访问。

## 验收记录

每次演练至少记录：

```text
drill_at=
basebackup_path=
wal_archive_dir=
target_time=
rto_minutes=
rpo_seconds=
restored_user_count=
restored_workspace_count=
restored_article_count=
restored_sync_pending_count=
notes=
```
