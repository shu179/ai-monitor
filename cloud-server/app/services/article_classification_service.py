from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.security import utc_now
from app.models import (
    Article,
    ArticleTaskLink,
    BrandTask,
    ClassificationJob,
    ClassificationStatus,
    User,
)
from app.services.change_log_service import record_workspace_change

_TOKEN_SPLIT_RE = re.compile(r"[\s,，、/|;；:：!！?？()（）\\[\\]【】\"'“”‘’<>《》]+")


def sync_article_classification_state(
    db: Session,
    *,
    workspace_id: int,
    article_id: int,
    actor_user_id: int | None = None,
    reason: dict[str, Any] | None = None,
) -> None:
    """Keep the unresolved-article queue aligned with current article links."""
    linked_task_id = db.scalar(
        select(ArticleTaskLink.task_id)
        .where(
            ArticleTaskLink.workspace_id == workspace_id,
            ArticleTaskLink.article_id == article_id,
        )
        .order_by(ArticleTaskLink.created_at.desc(), ArticleTaskLink.id.desc())
        .limit(1)
    )
    if linked_task_id:
        _resolve_open_jobs(
            db,
            workspace_id=workspace_id,
            article_id=article_id,
            resolved_task_id=int(linked_task_id),
            resolved_by=actor_user_id,
            reason=reason,
        )
        return

    auto_linked_task_ids = auto_classify_article(
        db,
        workspace_id=workspace_id,
        article_id=article_id,
        actor_user_id=actor_user_id,
        reason=reason,
    )
    if auto_linked_task_ids:
        _resolve_open_jobs(
            db,
            workspace_id=workspace_id,
            article_id=article_id,
            resolved_task_id=auto_linked_task_ids[0],
            resolved_by=actor_user_id,
            reason=_merge_job_reason(reason or {}, {"auto_classified": True}),
        )
        return

    _ensure_unresolved_job(
        db,
        workspace_id=workspace_id,
        article_id=article_id,
        reason=reason,
    )


def auto_classify_article(
    db: Session,
    *,
    workspace_id: int,
    article_id: int,
    actor_user_id: int | None = None,
    reason: dict[str, Any] | None = None,
    max_links: int = 5,
) -> list[int]:
    """Lightweight deterministic article-to-brand matching for cloud upserts."""
    article = db.scalar(
        select(Article).where(
            Article.id == int(article_id),
            Article.workspace_id == int(workspace_id),
        )
    )
    if article is None:
        return []

    article_text = _article_match_text(article)
    if not article_text:
        return []

    tasks = list(db.scalars(
        select(BrandTask)
        .where(
            BrandTask.workspace_id == int(workspace_id),
            BrandTask.deleted_at.is_(None),
            BrandTask.enabled.is_(True),
        )
        .order_by(BrandTask.id.asc())
    ))
    matches: list[tuple[int, int, list[str]]] = []
    for task in tasks:
        score, reasons = _score_article_for_task(article_text, task)
        if score <= 0:
            continue
        matches.append((score, int(task.id), reasons))

    if not matches:
        return []

    matches.sort(key=lambda item: (-item[0], item[1]))
    linked_task_ids: list[int] = []
    now = utc_now()
    for score, task_id, reasons in matches[: max(1, min(int(max_links or 5), 20))]:
        reason_json = _merge_job_reason(
            {
                "reasons": reasons[:5],
                "score": score,
                "source": "cloud_auto_rule",
            },
            reason,
        )
        db.execute(
            insert(ArticleTaskLink)
            .values(
                workspace_id=workspace_id,
                article_id=article_id,
                task_id=task_id,
                source="cloud_auto_rule",
                confidence=min(100, max(50, score)),
                reason_json=reason_json,
                confirmed_by=actor_user_id,
            )
            .on_conflict_do_update(
                index_elements=["article_id", "task_id"],
                set_={
                    "source": "cloud_auto_rule",
                    "confidence": min(100, max(50, score)),
                    "reason_json": reason_json,
                    "confirmed_by": actor_user_id,
                },
            )
        )
        linked_task_ids.append(task_id)
    db.execute(
        update(Article)
        .where(
            Article.workspace_id == workspace_id,
            Article.id == article_id,
        )
        .values(updated_at=now)
    )
    return linked_task_ids


def list_article_classification_jobs(
    db: Session,
    admin: User,
    *,
    status_filter: str = "unresolved",
    limit: int = 200,
) -> list[dict[str, Any]]:
    status_value = _normalize_classification_status(status_filter)
    safe_limit = min(max(int(limit or 200), 1), 1000)
    stmt = (
        select(ClassificationJob, Article)
        .join(Article, Article.id == ClassificationJob.article_id)
        .where(
            ClassificationJob.workspace_id == admin.workspace_id,
            Article.workspace_id == admin.workspace_id,
        )
        .order_by(ClassificationJob.updated_at.desc(), ClassificationJob.id.desc())
        .limit(safe_limit)
    )
    if status_value is not None:
        stmt = stmt.where(ClassificationJob.status == status_value)

    rows = db.execute(stmt)
    article_ids: list[int] = []
    pairs: list[tuple[ClassificationJob, Article]] = []
    for job, article in rows:
        pairs.append((job, article))
        article_ids.append(int(article.id))
    links_by_article_id = _links_by_article_id(db, admin.workspace_id, article_ids)
    return [
        _classification_job_payload(job, article, links_by_article_id.get(int(article.id), []))
        for job, article in pairs
    ]


def resolve_article_classification_job(
    db: Session,
    admin: User,
    *,
    job_id: int,
    task_id: int,
    reason: str = "",
) -> dict[str, Any]:
    job, article = _get_workspace_job_with_article(db, admin, job_id)
    task = db.scalar(
        select(BrandTask).where(
            BrandTask.id == int(task_id),
            BrandTask.workspace_id == admin.workspace_id,
            BrandTask.deleted_at.is_(None),
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand task not found")

    now = utc_now()
    reason_json = {"reasons": [str(reason or "管理员归类未归类文章").strip()[:256]]}
    db.execute(
        insert(ArticleTaskLink)
        .values(
            workspace_id=admin.workspace_id,
            article_id=article.id,
            task_id=task.id,
            source="admin_classification",
            confidence=100,
            reason_json=reason_json,
            confirmed_by=admin.id,
        )
        .on_conflict_do_update(
            index_elements=["article_id", "task_id"],
            set_={
                "source": "admin_classification",
                "confidence": 100,
                "reason_json": reason_json,
                "confirmed_by": admin.id,
            },
        )
    )
    job.status = ClassificationStatus.resolved
    job.resolved_task_id = task.id
    job.resolved_by = admin.id
    job.reason_json = _merge_job_reason(
        job.reason_json,
        {
            "resolved_by_admin": True,
            "resolved_reason": reason_json["reasons"][0],
        },
    )
    job.updated_at = now
    article.updated_at = now
    record_workspace_change(
        db,
        workspace_id=admin.workspace_id,
        stream="articles",
        kind="article.classification_resolved",
        ref_id=str(article.id),
    )
    db.commit()
    db.refresh(job)
    db.refresh(article)
    return _classification_job_payload(
        job,
        article,
        _links_by_article_id(db, admin.workspace_id, [int(article.id)]).get(int(article.id), []),
    )


def ignore_article_classification_job(
    db: Session,
    admin: User,
    *,
    job_id: int,
    reason: str = "",
) -> dict[str, Any]:
    job, article = _get_workspace_job_with_article(db, admin, job_id)
    now = utc_now()
    job.status = ClassificationStatus.ignored
    job.resolved_by = admin.id
    job.reason_json = _merge_job_reason(
        job.reason_json,
        {
            "ignored_by_admin": True,
            "ignored_reason": str(reason or "").strip()[:256],
        },
    )
    job.updated_at = now
    article.updated_at = now
    record_workspace_change(
        db,
        workspace_id=admin.workspace_id,
        stream="articles",
        kind="article.classification_ignored",
        ref_id=str(article.id),
    )
    db.commit()
    db.refresh(job)
    db.refresh(article)
    return _classification_job_payload(
        job,
        article,
        _links_by_article_id(db, admin.workspace_id, [int(article.id)]).get(int(article.id), []),
    )


def _ensure_unresolved_job(
    db: Session,
    *,
    workspace_id: int,
    article_id: int,
    reason: dict[str, Any] | None = None,
) -> None:
    existing = db.scalar(
        select(ClassificationJob)
        .where(
            ClassificationJob.workspace_id == workspace_id,
            ClassificationJob.article_id == article_id,
            ClassificationJob.status == ClassificationStatus.unresolved,
        )
        .limit(1)
    )
    now = utc_now()
    if existing is not None:
        existing.reason_json = _merge_job_reason(existing.reason_json, reason)
        existing.updated_at = now
        return
    ignored = db.scalar(
        select(ClassificationJob.id)
        .where(
            ClassificationJob.workspace_id == workspace_id,
            ClassificationJob.article_id == article_id,
            ClassificationJob.status == ClassificationStatus.ignored,
        )
        .limit(1)
    )
    if ignored is not None:
        return
    db.add(
        ClassificationJob(
            workspace_id=workspace_id,
            article_id=article_id,
            status=ClassificationStatus.unresolved,
            reason_json=_merge_job_reason({}, reason),
            updated_at=now,
        )
    )


def _resolve_open_jobs(
    db: Session,
    *,
    workspace_id: int,
    article_id: int,
    resolved_task_id: int,
    resolved_by: int | None,
    reason: dict[str, Any] | None = None,
) -> None:
    db.execute(
        update(ClassificationJob)
        .where(
            ClassificationJob.workspace_id == workspace_id,
            ClassificationJob.article_id == article_id,
            ClassificationJob.status == ClassificationStatus.unresolved,
        )
        .values(
            status=ClassificationStatus.resolved,
            resolved_task_id=resolved_task_id,
            resolved_by=resolved_by,
            reason_json=_merge_job_reason(reason or {}, {"auto_resolved": True}),
            updated_at=utc_now(),
        )
    )


def _article_match_text(article: Article) -> str:
    payload = article.payload_json if isinstance(article.payload_json, dict) else {}
    fields = [
        article.title,
        article.source,
        article.canonical_url,
        payload.get("excerpt"),
        payload.get("media_name"),
        payload.get("platform"),
        payload.get("account_name"),
        payload.get("account_url"),
    ]
    return _normalize_match_text(" ".join(str(item or "") for item in fields))


def _score_article_for_task(article_text: str, task: BrandTask) -> tuple[int, list[str]]:
    terms = _task_match_terms(task)
    if not terms:
        return 0, []
    score = 0
    reasons: list[str] = []
    seen_terms: set[str] = set()
    for term, weight, label in terms:
        if term in seen_terms:
            continue
        seen_terms.add(term)
        if term and term in article_text:
            score += weight
            reasons.append(label)
    return score, reasons


def _task_match_terms(task: BrandTask) -> list[tuple[str, int, str]]:
    terms: list[tuple[str, int, str]] = []

    def add(value: Any, *, weight: int, label_prefix: str) -> None:
        text = _normalize_match_text(value)
        if len(text) < 2:
            return
        terms.append((text, weight, f"{label_prefix}：{text[:40]}"))

    add(task.brand, weight=90, label_prefix="品牌命中")
    add(task.name, weight=70, label_prefix="任务名命中")
    config = task.config_json if isinstance(task.config_json, dict) else {}
    for keyword in _extract_config_keywords(config):
        add(keyword, weight=55, label_prefix="关键词命中")
    for tag in _extract_string_list(config.get("industry_tags")) + _extract_string_list(config.get("region_tags")):
        add(tag, weight=12, label_prefix="标签命中")
    return terms


def _extract_config_keywords(config: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("brand", "name"):
        if str(config.get(key) or "").strip():
            values.append(str(config.get(key) or "").strip())
    raw_keywords = config.get("keywords")
    if isinstance(raw_keywords, list):
        for item in raw_keywords:
            if isinstance(item, dict):
                for key in ("keyword", "brand", "name"):
                    text = str(item.get(key) or "").strip()
                    if text:
                        values.append(text)
            else:
                text = str(item or "").strip()
                if text:
                    values.append(text)
    local_task = config.get("local_task") if isinstance(config.get("local_task"), dict) else {}
    if local_task:
        values.extend(_extract_config_keywords(local_task))
    return _dedupe_texts(values)


def _extract_string_list(value: Any) -> list[str]:
    source = value if isinstance(value, list) else []
    return _dedupe_texts([str(item or "").strip() for item in source if str(item or "").strip()])


def _dedupe_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _normalize_match_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return _TOKEN_SPLIT_RE.sub(" ", text)


def _get_workspace_job_with_article(db: Session, admin: User, job_id: int) -> tuple[ClassificationJob, Article]:
    row = db.execute(
        select(ClassificationJob, Article)
        .join(Article, Article.id == ClassificationJob.article_id)
        .where(
            ClassificationJob.id == int(job_id),
            ClassificationJob.workspace_id == admin.workspace_id,
            Article.workspace_id == admin.workspace_id,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Classification job not found")
    return row[0], row[1]


def _links_by_article_id(db: Session, workspace_id: int, article_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not article_ids:
        return {}
    rows = db.scalars(
        select(ArticleTaskLink)
        .where(
            ArticleTaskLink.workspace_id == workspace_id,
            ArticleTaskLink.article_id.in_(article_ids),
        )
        .order_by(ArticleTaskLink.article_id.asc(), ArticleTaskLink.task_id.asc())
    )
    links: dict[int, list[dict[str, Any]]] = {}
    for link in rows:
        links.setdefault(int(link.article_id), []).append({
            "task_id": int(link.task_id),
            "source": link.source,
            "confidence": int(link.confidence or 0),
            "reason_json": link.reason_json or {},
            "created_at": link.created_at,
        })
    return links


def _classification_job_payload(
    job: ClassificationJob,
    article: Article,
    links: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": int(job.id),
        "workspace_id": int(job.workspace_id),
        "article_id": int(job.article_id),
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "reason_json": job.reason_json or {},
        "resolved_task_id": job.resolved_task_id,
        "resolved_by": job.resolved_by,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "article": {
            "id": int(article.id),
            "workspace_id": int(article.workspace_id),
            "canonical_url": article.canonical_url,
            "url_hash": article.url_hash,
            "title": article.title,
            "source": article.source,
            "media_type": article.media_type,
            "published_at": article.published_at,
            "payload_json": article.payload_json or {},
            "created_at": article.created_at,
            "updated_at": article.updated_at,
            "task_links": links,
        },
    }


def _normalize_classification_status(value: str) -> ClassificationStatus | None:
    text = str(value or "").strip().lower()
    if text in {"", "all"}:
        return None
    try:
        return ClassificationStatus(text)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid classification status")


def _merge_job_reason(current: Any, patch: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(current or {}) if isinstance(current, dict) else {}
    if isinstance(patch, dict):
        for key, value in patch.items():
            text_key = str(key or "").strip()[:64]
            if not text_key:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[text_key] = value
            elif isinstance(value, list):
                payload[text_key] = [str(item or "").strip()[:256] for item in value[:20]]
            elif isinstance(value, dict):
                payload[text_key] = {
                    str(k or "").strip()[:64]: str(v or "").strip()[:256]
                    for k, v in list(value.items())[:20]
                    if str(k or "").strip()
                }
    return payload
