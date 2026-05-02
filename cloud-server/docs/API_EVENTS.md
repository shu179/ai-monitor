# Sync Event 草案

本地端后续通过 outbox 批量调用：

```http
POST /api/v1/sync/events
Authorization: Bearer <access_token>
```

## 通用格式

```json
{
  "events": [
    {
      "event_type": "run_record",
      "idempotency_key": "task-1:2026-05-02:kimi:abc",
      "payload": {}
    }
  ]
}
```

每个事件必须有稳定 `idempotency_key`，重试不会重复入库。

## run_record

```json
{
  "event_type": "run_record",
  "idempotency_key": "run:<local-record-id>",
  "payload": {
    "task_id": 1,
    "platform": "kimi",
    "keyword": "品牌关键词",
    "brand": "品牌",
    "mode": "browser",
    "executed_at": "2026-05-02T10:30:00+08:00",
    "result": {
      "rank": 1,
      "references": [],
      "body_references": []
    }
  }
}
```

不要上传截图路径或截图文件。

## article_reference_event

```json
{
  "event_type": "article_reference_event",
  "idempotency_key": "ref:<run-record-id>:<normalized-url>:<platform>:<day>",
  "payload": {
    "task_id": 1,
    "normalized_url": "https://example.com/article",
    "platform": "kimi",
    "record_day": "2026-05-02",
    "source_record_key": "run:<local-record-id>"
  }
}
```

后续会扩展：

- `article_upsert`
- `article_task_link`
- `classification_job`
- `weather_upsert`
- `client_snapshot_ack`

