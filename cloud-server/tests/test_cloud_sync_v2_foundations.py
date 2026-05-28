from __future__ import annotations

import sys
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("SURFACED_CLOUD_SECRET_KEY", "test-secret-key-for-sync-v2-foundations")

from app.models import (  # noqa: E402
    AgentCommand,
    ObjectManifest,
    ObjectUploadPart,
    ObjectUploadSession,
    SyncBatchItem,
    SyncWorkerShardLease,
    WorkspaceRateLimit,
    WorkspaceChangeLog,
)
from app.schemas import SyncBatchEventIn  # noqa: E402
from app.services.change_log_service import (  # noqa: E402
    _decode_notify_payload,
    diff_change_streams,
    event_id_for_change_snapshot,
)
from app.services.sync_v2_service import (  # noqa: E402
    LIMITS,
    _partition_key_for_event,
    _reserve_idempotency_key,
    _reset_soft_rate_limit_buckets_for_tests,
    _retry_after_for_queue_depth,
    build_state_delta,
    consume_workspace_rate_limit,
    make_bootstrap_cursor,
    make_reset_token,
    parse_bootstrap_cursor,
    parse_reset_token,
    shard_advisory_lock_key,
    throttle_headers,
    workspace_bucket_advisory_lock_key,
    _stream_for_event,
    _virtual_shard,
    cloud_capabilities,
)
from app.services.sync_v2_worker import (  # noqa: E402
    _apply_statement_timeout,
    _block_partition_pending_items,
    _mark_item_dead_letter,
    _mark_item_done,
    _mirror_legacy_sync_event,
    _run_worker_maintenance_if_due,
    _release_item_for_retry,
    claim_sync_batch_items,
    list_pending_sync_item_shards,
    process_sync_batch_items_once,
    renew_sync_worker_shard_leases,
    run_sync_v2_worker,
)


class CloudSyncV2ModelTests(unittest.TestCase):
    def test_partitioned_change_log_primary_key_contains_created_at(self) -> None:
        self.assertEqual(
            [column.name for column in WorkspaceChangeLog.__table__.primary_key.columns],
            ["workspace_id", "stream", "seq", "created_at"],
        )

    def test_partitioned_sync_item_primary_key_contains_created_at(self) -> None:
        self.assertEqual(
            [column.name for column in SyncBatchItem.__table__.primary_key.columns],
            ["id", "created_at"],
        )

    def test_agent_commands_primary_key_contains_created_at(self) -> None:
        self.assertEqual(
            [column.name for column in AgentCommand.__table__.primary_key.columns],
            ["id", "created_at"],
        )

    def test_multipart_upload_tables_include_resume_fields(self) -> None:
        session_cols = ObjectUploadSession.__table__.columns
        part_cols = ObjectUploadPart.__table__.columns
        self.assertIn("storage_provider_upload_id", session_cols)
        self.assertIn("parts_completed", session_cols)
        self.assertIn("etag", part_cols)
        self.assertIn("completed_at", part_cols)

    def test_object_manifest_dedup_is_workspace_scoped(self) -> None:
        constraints = [
            tuple(column.name for column in constraint.columns)
            for constraint in ObjectManifest.__table__.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        ]
        self.assertIn(("workspace_id", "sha256"), constraints)

    def test_shard_lease_table_exposes_visible_worker_ownership(self) -> None:
        self.assertEqual([column.name for column in SyncWorkerShardLease.__table__.primary_key.columns], ["virtual_shard"])
        self.assertIn("worker_id", SyncWorkerShardLease.__table__.columns)
        self.assertIn("leased_until", SyncWorkerShardLease.__table__.columns)

    def test_rate_limit_bucket_uses_workspace_and_bucket_key(self) -> None:
        self.assertEqual(
            [column.name for column in WorkspaceRateLimit.__table__.primary_key.columns],
            ["workspace_id", "bucket"],
        )
        self.assertIn("tokens", WorkspaceRateLimit.__table__.columns)
        self.assertIn("refill_rate_per_second", WorkspaceRateLimit.__table__.columns)


class ChangeLogServiceTests(unittest.TestCase):
    def test_diff_change_streams_returns_only_advanced_streams(self) -> None:
        self.assertEqual(
            diff_change_streams({"articles": 2, "runs": 5}, {"articles": 3, "runs": 5, "tasks": 1}),
            ["articles", "tasks"],
        )

    def test_event_id_for_change_snapshot_is_compact_and_stable(self) -> None:
        self.assertEqual(event_id_for_change_snapshot({"runs": 2, "articles": 5}), "changes:articles:5,runs:2")

    def test_notify_payload_keeps_only_tiny_routing_data(self) -> None:
        payload = _decode_notify_payload('{"workspace_id":1,"stream":"articles","seq":9}')
        self.assertEqual(payload, {"workspace_id": 1, "stream": "articles", "seq": 9})


class SyncV2ServiceTests(unittest.TestCase):
    def test_capability_limits_expose_fixed_thresholds(self) -> None:
        payload = cloud_capabilities()
        self.assertIn("sync-v2", payload["capabilities"])
        self.assertEqual(payload["limits"]["inline_blob_max_bytes"], 32 * 1024)
        self.assertEqual(payload["limits"]["single_put_max_bytes"], 5 * 1024 * 1024)
        self.assertEqual(payload["limits"]["object_storage_total_quota_bytes"], 10 * 1024 * 1024 * 1024)
        self.assertEqual(payload["limits"]["object_storage_workspace_quota_bytes"], 5 * 1024 * 1024 * 1024)
        self.assertEqual(payload["limits"]["object_storage_max_file_bytes"], 512 * 1024 * 1024)
        self.assertEqual(payload["limits"]["object_storage_min_free_bytes"], 8 * 1024 * 1024 * 1024)
        self.assertEqual(payload["ttl_seconds"]["multipart_upload_session"], 24 * 60 * 60)
        self.assertEqual(payload["object_storage_backend"], "local")

    def test_partition_key_prefers_entity_identity(self) -> None:
        event = SyncBatchEventIn(
            event_type="article_task_links",
            idempotency_key="links-001",
            payload={"url_hash": "abc"},
        )
        self.assertEqual(_partition_key_for_event(7, event), "7:article_task_links:url_hash:abc")

    def test_v2_batch_event_maps_legacy_event_type_to_stream(self) -> None:
        event = SyncBatchEventIn(
            event_type="article_upsert",
            idempotency_key="article-001",
            payload={"url_hash": "abc"},
        )
        self.assertEqual(_stream_for_event(event), "articles")

    def test_virtual_shard_is_stable_and_bounded(self) -> None:
        shard_a = _virtual_shard("workspace:article:1")
        shard_b = _virtual_shard("workspace:article:1")
        self.assertEqual(shard_a, shard_b)
        self.assertGreaterEqual(shard_a, 0)
        self.assertLess(shard_a, LIMITS["virtual_shards"])

    def test_advisory_lock_keys_are_stable_int63_values(self) -> None:
        key_a = shard_advisory_lock_key(17)
        key_b = shard_advisory_lock_key(17)
        bucket_key = workspace_bucket_advisory_lock_key(3, "sync_metadata")
        self.assertEqual(key_a, key_b)
        self.assertGreaterEqual(key_a, 0)
        self.assertLess(key_a, 2**63)
        self.assertGreaterEqual(bucket_key, 0)
        self.assertLess(bucket_key, 2**63)

    def test_queue_backpressure_returns_retry_after_headers(self) -> None:
        self.assertEqual(_retry_after_for_queue_depth(10), 0)
        self.assertGreater(_retry_after_for_queue_depth(5_000), 0)
        headers = throttle_headers(retry_after_seconds=10, queue_depth_hint=5_100)
        self.assertEqual(headers["Retry-After"], "10")
        self.assertEqual(headers["X-Queue-Depth-Hint"], "5100")
        self.assertEqual(headers["X-Throttle-Bucket"], "sync_metadata")

    def test_rate_limit_uses_distributed_advisory_lock_and_token_row(self) -> None:
        _reset_soft_rate_limit_buckets_for_tests()
        db = MagicMock()
        db.execute.side_effect = [
            MagicMock(),
            MagicMock(mappings=MagicMock(return_value=MagicMock(first=MagicMock(return_value={
                "tokens": 100.0,
                "capacity": 100.0,
                "refill_rate_per_second": 10.0,
            })))),
            MagicMock(mappings=MagicMock(return_value=MagicMock(first=MagicMock(return_value={
                "tokens": 90.0,
            })))),
        ]

        result = consume_workspace_rate_limit(
            db,
            workspace_id=3,
            bucket="sync_metadata",
            cost=10,
            capacity=100,
            refill_rate_per_second=10,
        )

        self.assertTrue(result["allowed"])
        lock_sql = str(db.execute.call_args_list[0].args[0]).lower()
        self.assertIn("pg_advisory_xact_lock", lock_sql)
        bucket_sql = str(db.execute.call_args_list[1].args[0]).lower()
        self.assertIn("insert into workspace_rate_limits", bucket_sql)
        self.assertIn("on conflict (workspace_id, bucket)", bucket_sql)

    def test_rate_limit_reuses_soft_bucket_before_db_lock(self) -> None:
        _reset_soft_rate_limit_buckets_for_tests()
        db = MagicMock()
        db.execute.side_effect = [
            MagicMock(),
            MagicMock(mappings=MagicMock(return_value=MagicMock(first=MagicMock(return_value={
                "tokens": 100.0,
                "capacity": 100.0,
                "refill_rate_per_second": 10.0,
            })))),
            MagicMock(mappings=MagicMock(return_value=MagicMock(first=MagicMock(return_value={
                "tokens": 80.0,
            })))),
        ]

        first = consume_workspace_rate_limit(
            db,
            workspace_id=3,
            bucket="sync_metadata",
            cost=10,
            capacity=100,
            refill_rate_per_second=10,
        )
        second = consume_workspace_rate_limit(
            db,
            workspace_id=3,
            bucket="sync_metadata",
            cost=10,
            capacity=100,
            refill_rate_per_second=10,
        )

        self.assertTrue(first["allowed"])
        self.assertEqual(first["source"], "db")
        self.assertTrue(second["allowed"])
        self.assertEqual(second["source"], "soft")
        self.assertEqual(db.execute.call_count, 3)

    def test_reserve_idempotency_key_uses_postgres_on_conflict(self) -> None:
        db = MagicMock()
        db.scalar.return_value = "event-key-001"
        self.assertTrue(_reserve_idempotency_key(db, 3, scope="sync_batch_item", idempotency_key="event-key-001"))
        stmt = db.scalar.call_args.args[0]
        self.assertIn("ON CONFLICT", str(stmt.compile()).upper())

    def test_reset_token_and_bootstrap_cursor_roundtrip(self) -> None:
        token = make_reset_token(7, ["articles", "runs"])
        self.assertEqual(parse_reset_token(token)["workspace_id"], 7)
        self.assertEqual(parse_reset_token(token)["streams"], ["articles", "runs"])

        cursor = make_bootstrap_cursor(3)
        self.assertEqual(parse_bootstrap_cursor(cursor)["index"], 3)
        cursor = make_bootstrap_cursor(1, after_id=42)
        self.assertEqual(parse_bootstrap_cursor(cursor)["after_id"], 42)

    @patch("app.services.sync_v2_service._stale_cursor_streams", return_value=["articles"])
    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"articles": 10})
    def test_state_delta_returns_reset_required_for_stale_cursor(self, _snapshot, _stale) -> None:
        db = MagicMock()
        user = SimpleNamespace(id=9, workspace_id=7, role="admin", view_all_tasks=True, username="shu", email=None, email_verified=False, avatar=None, birthday=None, hire_date=None, display_name=None, enabled=True, updated_at=None)

        result = build_state_delta(db, user, cursors={"articles": 1})  # type: ignore[arg-type]

        self.assertTrue(result["reset_required"])
        self.assertTrue(result["has_more"])
        self.assertEqual(parse_reset_token(result["reset_token"])["streams"], ["articles"])

    @patch("app.services.sync_v2_service.compact_change_snapshot", return_value={"articles": 10, "runs": 3})
    def test_state_delta_reset_token_pages_bootstrap_streams(self, _snapshot) -> None:
        db = MagicMock()
        user = SimpleNamespace(
            id=9,
            workspace_id=7,
            role="admin",
            view_all_tasks=True,
            username="shu",
            email=None,
            email_verified=False,
            avatar=None,
            birthday=None,
            hire_date=None,
            display_name=None,
            enabled=True,
            updated_at=None,
        )
        token = make_reset_token(7, ["articles", "runs"])

        first = build_state_delta(db, user, cursors={}, reset_token=token)  # type: ignore[arg-type]
        second = build_state_delta(
            db,
            user,  # type: ignore[arg-type]
            cursors={},
            reset_token=token,
            bootstrap_cursor=first["bootstrap_cursor"],
        )

        self.assertEqual(first["changes"][0]["stream"], "articles")
        self.assertEqual(first["changes"][0]["kind"], "articles.bootstrap_complete")
        self.assertTrue(first["has_more"])
        self.assertEqual(second["changes"][0]["stream"], "runs")
        self.assertEqual(second["changes"][0]["kind"], "runs.bootstrap_complete")
        self.assertFalse(second["has_more"])


class SyncV2WorkerSqlTests(unittest.TestCase):
    def test_apply_statement_timeout_uses_integer_literal_sql(self) -> None:
        db = MagicMock()

        _apply_statement_timeout(db, 60_000)

        sql = str(db.execute.call_args.args[0])
        self.assertEqual(sql, "SET statement_timeout = 60000")
        self.assertEqual(db.execute.call_args.args[1:], ())

    @patch("app.services.sync_v2_worker.run_cloud_maintenance", return_value={"dry_run": False})
    def test_worker_maintenance_runs_under_advisory_lock(self, maintenance) -> None:
        db = MagicMock()
        db.scalar.return_value = True

        self.assertTrue(_run_worker_maintenance_if_due(db, worker_id="w1"))

        maintenance.assert_called_once_with(db, dry_run=False)
        sql_calls = "\n".join(str(call.args[0]).lower() for call in db.execute.call_args_list)
        self.assertIn("pg_advisory_unlock", sql_calls)
        self.assertGreaterEqual(db.commit.call_count, 1)

    @patch("app.services.sync_v2_worker.run_cloud_maintenance")
    def test_worker_maintenance_skips_when_lock_is_busy(self, maintenance) -> None:
        db = MagicMock()
        db.scalar.return_value = False

        self.assertFalse(_run_worker_maintenance_if_due(db, worker_id="w1"))

        maintenance.assert_not_called()

    @patch("app.services.sync_v2_worker.process_sync_batch_items_once", return_value={"claimed": 0})
    @patch("app.services.sync_v2_worker._run_worker_maintenance_if_due", return_value=True)
    @patch("app.services.sync_v2_worker.time.sleep")
    def test_run_worker_triggers_periodic_maintenance(self, _sleep, maintenance, process_once) -> None:
        db = MagicMock()
        db.__enter__.return_value = db
        session_factory = MagicMock(return_value=db)

        run_sync_v2_worker(
            session_factory=session_factory,
            worker_id="w1",
            shard_ids=[1],
            stop_after=1,
            maintenance_interval_seconds=1,
        )

        maintenance.assert_called_once_with(db, worker_id="w1")
        process_once.assert_called_once()

    @patch("app.services.sync_v2_worker.renew_sync_worker_shard_leases", return_value=[1, 2])
    def test_claim_uses_cte_not_update_limit_and_matches_created_at(self, _renew) -> None:
        db = MagicMock()
        db.execute.side_effect = [
            [(1,), (2,)],
            [],
        ]
        claim_sync_batch_items(db, worker_id="w1", shard_ids=[1, 2], limit=10)

        sql = str(db.execute.call_args_list[1].args[0]).lower()
        self.assertIn("with candidates as", sql)
        self.assertIn("for update of item skip locked", sql)
        self.assertIn("limit :limit", sql)
        self.assertNotIn("update sync_batch_items limit", sql)
        self.assertIn("item.created_at = candidates.created_at", sql)
        self.assertIn("virtual_shard = any", sql)
        self.assertIn("not exists", sql)
        self.assertIn("prior.seq < item.seq", sql)
        self.assertIn("blocker.status in ('dead_letter', 'blocked')", sql)
        self.assertIn("blocker.seq < item.seq", sql)
        self.assertIn("prior.status <> 'done'", sql)

    @patch("app.services.sync_v2_worker.renew_sync_worker_shard_leases")
    def test_claim_skips_shard_lease_when_queue_has_no_pending_items(self, renew) -> None:
        db = MagicMock()
        db.execute.return_value = []
        claim_sync_batch_items(db, worker_id="w1", shard_ids=[1, 2], limit=10)

        renew.assert_not_called()

    def test_pending_shard_probe_is_distinct_and_limited(self) -> None:
        db = MagicMock()
        db.execute.return_value = [(7,), (9,)]

        self.assertEqual(list_pending_sync_item_shards(db, shard_ids=[7, 9], limit=10), [7, 9])

        sql = str(db.execute.call_args.args[0]).lower()
        self.assertIn("select distinct item.virtual_shard", sql)
        self.assertIn("status = 'pending'", sql)
        self.assertIn("leased_until < now()", sql)
        self.assertIn("limit :limit", sql)

    def test_renew_shard_leases_uses_advisory_lock_and_visible_lease_rows(self) -> None:
        db = MagicMock()
        db.scalar.return_value = True
        db.execute.return_value.fetchone.return_value = (7,)

        owned = renew_sync_worker_shard_leases(db, worker_id="w1", shard_ids=[7], lease_seconds=60)

        self.assertEqual(owned, [7])
        lock_sql = str(db.scalar.call_args.args[0]).lower()
        self.assertIn("pg_try_advisory_xact_lock", lock_sql)
        lease_sql = str(db.execute.call_args.args[0]).lower()
        self.assertIn("insert into sync_worker_shard_leases", lease_sql)
        self.assertIn("on conflict (virtual_shard) do update", lease_sql)
        self.assertIn("leased_until < now()", lease_sql)

    def test_mark_item_done_matches_worker_and_created_at(self) -> None:
        db = MagicMock()
        db.execute.return_value.rowcount = 1
        item = {"id": 7, "created_at": "2026-05-26T00:00:00Z"}

        self.assertEqual(_mark_item_done(db, item=item, worker_id="w1"), 1)

        sql = str(db.execute.call_args.args[0]).lower()
        self.assertIn("status = 'done'", sql)
        self.assertIn("created_at = :created_at", sql)
        self.assertIn("worker_id = :worker_id", sql)

    def test_release_item_for_retry_restores_pending_status(self) -> None:
        db = MagicMock()
        db.execute.return_value.rowcount = 1
        item = {"id": 7, "created_at": "2026-05-26T00:00:00Z"}

        self.assertEqual(_release_item_for_retry(db, item=item, worker_id="w1", error="boom"), 1)

        sql = str(db.execute.call_args.args[0]).lower()
        self.assertIn("status = 'pending'", sql)
        self.assertIn("last_error = :error", sql)

    def test_dead_letter_writes_attempt_history_and_marks_item(self) -> None:
        db = MagicMock()
        db.scalar.return_value = 55
        db.execute.return_value.rowcount = 1
        item = {
            "id": 7,
            "created_at": "2026-05-26T00:00:00Z",
            "workspace_id": 3,
            "batch_id": "batch-1",
            "event_type": "article_upsert",
            "idempotency_key": "article-001",
            "partition_key": "3:article:abc",
            "attempts": 5,
        }

        self.assertEqual(_mark_item_dead_letter(db, item=item, worker_id="w1", error="boom"), 1)

        sql_calls = "\n".join(str(call.args[0]).lower() for call in db.execute.call_args_list)
        self.assertIn("insert into dead_letter_attempts", sql_calls)
        self.assertIn("status = 'dead_letter'", sql_calls)
        self.assertIn("status = 'blocked'", sql_calls)

    def test_block_partition_pending_items_marks_same_entity_chain_blocked(self) -> None:
        db = MagicMock()
        db.execute.return_value.rowcount = 3
        item = {
            "id": 7,
            "created_at": "2026-05-26T00:00:00Z",
            "workspace_id": 3,
            "partition_key": "3:article:abc",
            "seq": 2,
        }

        self.assertEqual(_block_partition_pending_items(db, item=item, error="boom"), 3)

        sql = str(db.execute.call_args.args[0]).lower()
        params = db.execute.call_args.args[1]
        self.assertIn("status = 'blocked'", sql)
        self.assertIn("partition_key = :partition_key", sql)
        self.assertIn("status = 'pending'", sql)
        self.assertIn("seq > :seq", sql)
        self.assertIn("id > :id", sql)
        self.assertEqual(params["workspace_id"], 3)
        self.assertEqual(params["partition_key"], "3:article:abc")
        self.assertEqual(params["seq"], 2)
        self.assertEqual(params["id"], 7)
        self.assertIn("blocked by earlier dead-lettered sync item", params["error"])

    def test_mirror_legacy_sync_event_returns_false_for_duplicate(self) -> None:
        db = MagicMock()
        db.scalar.return_value = None
        user = SimpleNamespace(id=2, workspace_id=3)
        event = SyncBatchEventIn(event_type="article_upsert", idempotency_key="article-001", payload={})

        self.assertFalse(_mirror_legacy_sync_event(db, user=user, event=event))  # type: ignore[arg-type]

    @patch("app.services.sync_v2_worker._mark_item_done")
    @patch("app.services.sync_v2_worker._materialize_claimed_item")
    @patch("app.services.sync_v2_worker.claim_sync_batch_items")
    def test_process_once_marks_successful_item_done(self, claim, materialize, mark_done) -> None:
        db = MagicMock()
        claim.return_value = [{"id": 1, "created_at": "now", "batch_id": "batch-1"}]

        stats = process_sync_batch_items_once(db, worker_id="w1", shard_ids=[1], limit=10)

        self.assertEqual(stats["claimed"], 1)
        self.assertEqual(stats["done"], 1)
        self.assertIn("elapsed_ms", stats)
        self.assertIn("max_item_ms", stats)
        materialize.assert_called_once()
        mark_done.assert_called_once()
        self.assertGreaterEqual(db.commit.call_count, 2)

    @patch("app.services.sync_v2_worker._release_item_for_retry")
    @patch("app.services.sync_v2_worker._materialize_claimed_item", side_effect=RuntimeError("boom"))
    @patch("app.services.sync_v2_worker.claim_sync_batch_items")
    def test_process_once_releases_failed_item_before_max_attempts(self, claim, _materialize, release) -> None:
        db = MagicMock()
        claim.return_value = [{"id": 1, "created_at": "now", "batch_id": "batch-1", "attempts": 2}]

        stats = process_sync_batch_items_once(db, worker_id="w1", shard_ids=[1], limit=10, max_attempts=5)

        self.assertEqual(stats["retry"], 1)
        release.assert_called_once()
        db.rollback.assert_called_once()
        db.commit.assert_called()

    @patch("app.services.sync_v2_worker._mark_item_dead_letter")
    @patch("app.services.sync_v2_worker._materialize_claimed_item", side_effect=RuntimeError("boom"))
    @patch("app.services.sync_v2_worker.claim_sync_batch_items")
    def test_process_once_dead_letters_item_at_max_attempts(self, claim, _materialize, dead_letter) -> None:
        db = MagicMock()
        claim.return_value = [{"id": 1, "created_at": "now", "batch_id": "batch-1", "attempts": 5}]

        stats = process_sync_batch_items_once(db, worker_id="w1", shard_ids=[1], limit=10, max_attempts=5)

        self.assertEqual(stats["dead_letter"], 1)
        dead_letter.assert_called_once()


class MigrationDefinitionTests(unittest.TestCase):
    def test_partitioned_primary_keys_include_created_at(self) -> None:
        migration = (ROOT / "alembic/versions/20260526_0011_cloud_sync_v2_foundations.py").read_text()
        self.assertIn('"0010_workspace_idempotency", "0010_hot_path_indexes"', migration)
        self.assertIn("PRIMARY KEY (workspace_id, stream, seq, created_at)", migration)
        self.assertIn("PRIMARY KEY (id, created_at)", migration)
        self.assertIn("PARTITION BY RANGE (created_at)", migration)

    def test_migration_creates_default_partitions_and_lookup_indexes(self) -> None:
        migration = (ROOT / "alembic/versions/20260526_0011_cloud_sync_v2_foundations.py").read_text()
        self.assertIn("PARTITION OF workspace_change_log DEFAULT", migration)
        self.assertIn("PARTITION OF sync_batch_items DEFAULT", migration)
        self.assertIn("idx_workspace_change_log_default_lookup", migration)
        self.assertIn("idx_sync_batch_items_default_claim", migration)
        self.assertIn('"sync_worker_shard_leases"', migration)
        self.assertIn('"workspace_rate_limits"', migration)
        self.assertIn("idx_sync_batch_items_default_workspace_status", migration)


if __name__ == "__main__":
    unittest.main()
