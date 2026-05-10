# Windows 打包验证清单

本文档记录 Windows 打包前的自动化检查和人工验证项目。当前环境为 macOS，无法执行 Windows 真机验证。

## 1. 环境准备（Windows 真机）

### 1.1 系统要求
- Windows 10 (1809+) 或 Windows 11
- Python 3.10+ (建议 3.12 或 3.13)
- 8GB+ RAM
- 10GB+ 可用磁盘空间

### 1.2 依赖安装命令

```powershell
# 克隆代码
git clone <repo-url>
cd ai-monitor

# 创建虚拟环境
python -m venv venv
.\venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
pip install pyinstaller

# 确认 PyInstaller 版本
pyinstaller --version
# 预期: 6.x.x 或更新
```

### 1.3 PyInstaller 版本确认
```powershell
# 必须在 6.x 以上以支持 Python 3.10+
pyinstaller --version
```

## 2. 打包前命令（在 Windows 真机执行）

### 2.1 本地测试

```powershell
# 进入项目目录
cd C:\path\to\ai-monitor

# 激活虚拟环境
.\venv\Scripts\activate

# 运行核心测试（本地）
python -m pytest test_diagnostic_events.py test_browser_processes.py test_cloud_outbox.py test_cloud_outbox_reliability.py test_screenshot_cleanup.py test_notifier_cooldown_race.py test_windows_bootstrap.py -v

# 运行 preflight 检查
python scripts/preflight_release.py
```

### 2.2 云端测试

```powershell
cd cloud-server
python -m pytest tests/ -v
cd ..
```

### 2.3 预期测试结果
- 所有测试 PASS
- preflight 输出无 FAIL（允许 WARN）
- Exit code = 0

## 3. PostgreSQL Migration 验证（Staging DB）

### 3.1 前提条件
- 连接到 staging PostgreSQL 数据库
- 有备份权限

### 3.2 备份数据库
```powershell
# 使用 pg_dump 备份（生产级步骤）
pg_dump -h <staging-host> -U <user> -d <database> -F c -b -v -f staging_backup_$(Get-Date -Format "yyyyMMdd").dump
```

### 3.3 运行 Alembic Migration

```powershell
cd cloud-server

# 检查当前 migration 状态
alembic current

# 升级到最新
alembic upgrade head

# 验证升级成功
alembic current
```

### 3.4 验证 Unique Constraints

```sql
-- 检查 sync_events 表的 unique constraint
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'sync_events'::regclass
  AND contype = 'u';

-- 预期: 包含 (workspace_id, idempotency_key) 的 unique constraint

-- 检查 run_records 表
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'run_records'::regclass
  AND contype = 'u';

-- 检查 article_reference_events 表
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'article_reference_events'::regclass
  AND contype = 'u';
```

### 3.5 验证 Idempotency 行为

> **注意**: `sync_events` 表的 `workspace_id` 和 `user_id` 都是整数外键，以下 SQL 示例中的 `<id>` 需要替换为真实存在的 workspace/user ID。

#### 3.5.1 不同 workspace 可以使用相同的 idempotency_key（预期：都成功）

```sql
-- Workspace 1 插入
INSERT INTO sync_events (workspace_id, user_id, event_type, idempotency_key, payload_json)
VALUES (1, 1, 'test', 'dup-key-001', '{"msg": "workspace 1"}');

-- Workspace 2 使用相同的 idempotency_key（允许）
INSERT INTO sync_events (workspace_id, user_id, event_type, idempotency_key, payload_json)
VALUES (2, 1, 'test', 'dup-key-001', '{"msg": "workspace 2"}');

-- 预期: 两行都成功插入
```

#### 3.5.2 同 workspace 重复 idempotency_key 在裸 SQL 下会触发 constraint 错误

```sql
-- Workspace 1 再次插入相同的 idempotency_key
INSERT INTO sync_events (workspace_id, user_id, event_type, idempotency_key, payload_json)
VALUES (1, 1, 'test', 'dup-key-001', '{"msg": "duplicate"}');

-- 预期: ERROR: duplicate key value violates unique constraint "uq_sync_events_workspace_idempotency"
```

#### 3.5.3 应用服务层的 ON CONFLICT DO NOTHING 行为

在 Python 应用代码中使用 `session.insert().on_conflict_do_nothing()` 时：
- 同 workspace + 同 idempotency_key → 不报错，第一条保留（这是 dedup）
- 不同 workspace + 同 idempotency_key → 成功插入

这是**应用层**的行为，裸 SQL INSERT 会直接报 constraint 错误。

### 3.6 Downgrade 测试（仅 Staging）

> **注意**: 升级后允许不同 workspace 使用同一个 `idempotency_key`；如果这些数据已经产生，downgrade 恢复全局唯一约束前必须先处理这些跨 workspace 重复。

#### 3.6.1 检查三张表的全局 idempotency_key 重复

```sql
-- 检查 sync_events 全局重复
SELECT 'sync_events' AS tbl, idempotency_key, COUNT(*) AS cnt
FROM sync_events
GROUP BY idempotency_key
HAVING COUNT(*) > 1;

-- 检查 run_records 全局重复
SELECT 'run_records' AS tbl, idempotency_key, COUNT(*) AS cnt
FROM run_records
GROUP BY idempotency_key
HAVING COUNT(*) > 1;

-- 检查 article_reference_events 全局重复
SELECT 'article_reference_events' AS tbl, idempotency_key, COUNT(*) AS cnt
FROM article_reference_events
GROUP BY idempotency_key
HAVING COUNT(*) > 1;

-- 如果有结果，说明存在跨 workspace 的重复 key，需要先清理
```

#### 3.6.2 执行 downgrade

```powershell
# 如果上一步没有重复 key，可以尝试 downgrade
alembic downgrade -1

# 预期: 如果有跨 workspace 重复 key，downgrade 会抛出 duplicate key 错误
```

#### 3.6.3 清理重复 key（仅 Staging）

**不要直接批量删除生产数据**。如果 staging 验证时需要清理，请按以下步骤处理：

##### 步骤 1: 查看重复 key 明细

```sql
-- 查看 sync_events 重复 key 明细（只读查询）
SELECT id, workspace_id, user_id, idempotency_key, event_type, created_at
FROM sync_events
WHERE idempotency_key IN (
    SELECT idempotency_key
    FROM sync_events
    GROUP BY idempotency_key
    HAVING COUNT(*) > 1
)
ORDER BY idempotency_key, created_at;

-- run_records 重复 key 明细
SELECT id, workspace_id, idempotency_key, created_at
FROM run_records
WHERE idempotency_key IN (
    SELECT idempotency_key
    FROM run_records
    GROUP BY idempotency_key
    HAVING COUNT(*) > 1
)
ORDER BY idempotency_key, created_at;

-- article_reference_events 重复 key 明细
SELECT id, workspace_id, idempotency_key, created_at
FROM article_reference_events
WHERE idempotency_key IN (
    SELECT idempotency_key
    FROM article_reference_events
    GROUP BY idempotency_key
    HAVING COUNT(*) > 1
)
ORDER BY idempotency_key, created_at;
```

##### 步骤 2: 人工确认处理方式

根据业务需求决定保留哪条记录，然后：
- **方式 A**: 人工确认后，使用 `DELETE WHERE id = ?` 删除特定记录
- **方式 B**: 编写一次性迁移脚本处理

> **警告**: 不要在生产环境执行批量 DELETE。Staging 验证通过后，直接在生产应用新 migration 即可。

## 4. Windows 真机验证清单

### 4.1 应用启动验证

```powershell
# Qt 模式启动
python main.py --ui qt

# 验证:
# - 系统托盘图标出现
# - 无崩溃
# - 日志写入 logs/monitor.log
```

### 4.2 PyInstaller 打包

```powershell
# 打包（首次可能需要 5-10 分钟）
pyinstaller build.spec --clean

# 验证 dist 目录
dir dist
# 预期: Surfaced.exe 和依赖文件
```

### 4.3 EXE 启动验证

```powershell
# 进入 dist 目录
cd dist\Surfaced

# 启动 exe
.\Surfaced.exe

# 验证:
# - 启动成功
# - 无控制台窗口（console=False）
# - 托盘图标显示
```

### 4.4 DPI 缩放验证

在 125% 或 150% DPI 设置下：
- [ ] 托盘图标清晰
- [ ] 弹出菜单可读
- [ ] 无文字模糊或截断
- [ ] 窗口布局正常

### 4.5 信号处理验证

```powershell
# 测试 Ctrl+C
# 在运行中的 exe 按 Ctrl+C
# 预期: 优雅退出，日志记录 shutdown

# 测试关闭窗口
# 点击托盘图标关闭
# 预期: 触发 cleanup，logs/crash.log 不写入

# 测试 Ctrl+Break
# 预期: 同 Ctrl+C
```

### 4.6 异常处理验证

```powershell
# 模拟异常场景

# 1. 浏览器 profile 目录被占用
# - 打开一个浏览器实例
# - 再次运行 exe
# - 预期: diagnostics 记录失败原因，不崩溃

# 2. 配置文件损坏
# - 备份 config.yaml
# - 写入无效 YAML
# - 运行 exe
# - 预期: 使用默认配置，不崩溃
```

### 4.7 路径验证

验证打包后路径正确：
- [ ] `logs/` 在 exe 同级目录
- [ ] `screenshots/` 在数据目录
- [ ] `user_data/` 正确创建
- [ ] `diagnostics/` 目录存在

```powershell
# 检查诊断日志路径
# 运行后检查 logs/ 目录
dir logs
# 预期: monitor.log 存在
```

### 4.8 云端同步验证

```powershell
# 配置 config.yaml 的 cloud_server 相关配置
# 运行应用

# 验证:
# - cloud_outbox 能写入
# - sync_service 能连接
# - diagnostics 有记录
```

## 5. 发布阻断条件

满足以下任一条件，禁止发布：

### 5.1 Migration 相关
- [ ] Alembic upgrade head 失败
- [ ] Unique constraint 不存在
- [ ] 重复 key 测试失败

### 5.2 启动相关
- [ ] exe 启动 5 秒内崩溃
- [ ] 托盘图标不显示
- [ ] 无日志文件生成

### 5.3 信号处理相关
- [ ] Ctrl+C 不触发 cleanup
- [ ] 关闭窗口不触发 shutdown
- [ ] crash.log 异常时未写入

### 5.4 路径相关
- [ ] diagnostics 目录创建失败
- [ ] outbox 目录创建失败
- [ ] logs 目录不可写

### 5.5 测试相关
- [ ] 本地测试有任何 FAIL
- [ ] preflight有任何 FAIL
- [ ] 云端测试有任何 FAIL

## 6. 当前 macOS 环境状态

### 6.1 已完成
- [x] scripts/preflight_release.py 创建
- [x] test_preflight_release.py 创建
- [x] docs/windows_packaging_checklist.md 创建

### 6.2 待 Windows 真机验证

以下项目必须在 Windows 真机验证：

1. **PyInstaller 打包**
   ```powershell
   pyinstaller build.spec --clean
   ```

2. **EXE 启动测试**
   ```powershell
   .\dist\Surfaced\Surfaced.exe
   ```

3. **DPI 缩放测试**（125%, 150%）

4. **信号处理测试**（Ctrl+C, Ctrl+Break, 关闭窗口）

5. **PostgreSQL Migration**（在 staging DB 执行）

6. **路径正确性验证**（打包后）

## 7. 版本记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-05-10 | 1.2 | 修正 3.6.1 检查三张表全局重复、移除危险 DELETE SQL 改为只读 SELECT + 人工确认说明、修正"旧版单列 constraint"表述 |
| 2026-05-10 | 1.1 | 修正 SQL 字段名(payload→payload_json)、user_id 必填、同 workspace 重复 key 在裸 SQL 下会触发 constraint 错误、downgrade 检查改为全局 idempotency_key 重复、pg_dump 改为 PowerShell 语法 |
| 2026-05-10 | 1.0 | 初版 |
