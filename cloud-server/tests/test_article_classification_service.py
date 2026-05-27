from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import ClassificationJob, ClassificationStatus  # noqa: E402
from app.services.article_classification_service import (  # noqa: E402
    auto_classify_article,
    list_article_classification_jobs,
    resolve_article_classification_job,
    sync_article_classification_state,
)


class ArticleClassificationServiceTests(unittest.TestCase):
    def test_sync_article_classification_state_creates_unresolved_job_without_links(self) -> None:
        db = Mock()
        db.scalar.side_effect = [None, None, None, None]

        sync_article_classification_state(
            db,
            workspace_id=1,
            article_id=7,
            actor_user_id=2,
            reason={"source": "article_upsert"},
        )

        db.add.assert_called_once()
        job = db.add.call_args.args[0]
        self.assertIsInstance(job, ClassificationJob)
        self.assertEqual(job.workspace_id, 1)
        self.assertEqual(job.article_id, 7)
        self.assertEqual(job.status, ClassificationStatus.unresolved)
        self.assertEqual(job.reason_json["source"], "article_upsert")

    def test_sync_article_classification_state_auto_classifies_before_unresolved_queue(self) -> None:
        db = Mock()
        db.scalar.return_value = None

        with (
            patch("app.services.article_classification_service.auto_classify_article", return_value=[42]) as classify,
            patch("app.services.article_classification_service._resolve_open_jobs") as resolve_jobs,
        ):
            sync_article_classification_state(
                db,
                workspace_id=1,
                article_id=7,
                actor_user_id=2,
                reason={"source": "article_upsert"},
            )

        classify.assert_called_once()
        resolve_jobs.assert_called_once()
        db.add.assert_not_called()

    def test_auto_classify_article_links_matching_task_by_brand_keyword(self) -> None:
        article = SimpleNamespace(
            id=7,
            workspace_id=9,
            canonical_url="https://example.com/jisou",
            title="即搜AI 武汉GEO优化公司案例",
            source="媒体",
            payload_json={"excerpt": "这是一篇关于武汉GEO优化公司的文章"},
        )
        task = SimpleNamespace(
            id=42,
            workspace_id=9,
            brand="即搜AI",
            name="即搜AI",
            enabled=True,
            deleted_at=None,
            config_json={
                "keywords": [
                    {"keyword": "武汉GEO优化公司", "brand": "即搜AI"},
                ],
                "local_task": {
                    "industry_tags": ["科技互联网"],
                    "region_tags": ["湖北"],
                },
            },
        )
        db = Mock()
        db.scalar.return_value = article
        db.scalars.return_value = [task]

        linked = auto_classify_article(db, workspace_id=9, article_id=7, actor_user_id=2)

        self.assertEqual(linked, [42])
        self.assertEqual(db.execute.call_count, 2)

    def test_sync_article_classification_state_does_not_reopen_ignored_article(self) -> None:
        db = Mock()
        db.scalar.side_effect = [None, None, None, 3]

        sync_article_classification_state(db, workspace_id=1, article_id=7)

        db.add.assert_not_called()

    def test_resolve_article_classification_job_adds_link_and_marks_resolved(self) -> None:
        now = datetime(2026, 5, 6, tzinfo=timezone.utc)
        admin = SimpleNamespace(id=1, workspace_id=9)
        job = SimpleNamespace(
            id=5,
            workspace_id=9,
            article_id=7,
            status=ClassificationStatus.unresolved,
            reason_json={},
            resolved_task_id=None,
            resolved_by=None,
            created_at=now,
            updated_at=now,
        )
        article = SimpleNamespace(
            id=7,
            workspace_id=9,
            canonical_url="https://example.com/a",
            url_hash="hash-a",
            title="文章",
            source="媒体",
            media_type="selfmedia",
            published_at=None,
            payload_json={},
            created_at=now,
            updated_at=now,
        )
        task = SimpleNamespace(id=42)
        db = Mock()
        db.scalar.return_value = task

        with (
            patch("app.services.article_classification_service._get_workspace_job_with_article", return_value=(job, article)),
            patch("app.services.article_classification_service._links_by_article_id", return_value={7: []}),
            patch("app.services.article_classification_service.record_workspace_change") as record_change,
        ):
            result = resolve_article_classification_job(
                db,
                admin,  # type: ignore[arg-type]
                job_id=5,
                task_id=42,
                reason="确认属于品牌",
            )

        self.assertEqual(job.status, ClassificationStatus.resolved)
        self.assertEqual(job.resolved_task_id, 42)
        self.assertEqual(job.resolved_by, 1)
        self.assertEqual(result["status"], "resolved")
        record_change.assert_called_once()
        self.assertEqual(record_change.call_args.kwargs["stream"], "articles")
        db.commit.assert_called_once()

    def test_list_article_classification_jobs_includes_article_payload(self) -> None:
        now = datetime(2026, 5, 6, tzinfo=timezone.utc)
        admin = SimpleNamespace(workspace_id=9)
        job = SimpleNamespace(
            id=5,
            workspace_id=9,
            article_id=7,
            status=ClassificationStatus.unresolved,
            reason_json={"source": "article_upsert"},
            resolved_task_id=None,
            resolved_by=None,
            created_at=now,
            updated_at=now,
        )
        article = SimpleNamespace(
            id=7,
            workspace_id=9,
            canonical_url="https://example.com/a",
            url_hash="hash-a",
            title="文章",
            source="媒体",
            media_type="selfmedia",
            published_at=None,
            payload_json={"excerpt": "摘要"},
            created_at=now,
            updated_at=now,
        )
        db = Mock()
        db.execute.return_value = [(job, article)]
        with patch("app.services.article_classification_service._links_by_article_id", return_value={7: []}):
            jobs = list_article_classification_jobs(db, admin, status_filter="unresolved")

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["article"]["title"], "文章")
        self.assertEqual(jobs[0]["reason_json"]["source"], "article_upsert")


if __name__ == "__main__":
    unittest.main()
