from __future__ import annotations

from fastapi import APIRouter
from fastapi import Query

from app.api.deps import CurrentUser, DbSession
from app.schemas import (
    ArticlesResponse,
    ReferenceRankingResponse,
    RunRecordPublic,
    RunRecordsBatchRequest,
    RunRecordsBatchResponse,
    TaskDayStatusEventsBatchRequest,
    TaskDayStatusEventsBatchResponse,
    VisibleBrandTaskPublic,
)
from app.services.sync_service import (
    REFERENCE_ALGORITHM_VERSION,
    build_reference_ranking,
    list_visible_articles,
    list_deleted_visible_tasks,
    list_task_day_status_events_batch,
    list_task_run_records_batch,
    list_task_run_records,
    list_visible_tasks,
)

router = APIRouter()


@router.get("", response_model=list[VisibleBrandTaskPublic])
def visible_tasks(
    current_user: CurrentUser,
    db: DbSession,
    ids: list[int] | None = Query(default=None),
) -> list[VisibleBrandTaskPublic]:
    return list_visible_tasks(db, current_user, ids)


@router.get("/", response_model=list[VisibleBrandTaskPublic], include_in_schema=False)
def visible_tasks_with_slash(
    current_user: CurrentUser,
    db: DbSession,
    ids: list[int] | None = Query(default=None),
) -> list[VisibleBrandTaskPublic]:
    return list_visible_tasks(db, current_user, ids)


@router.get("/deleted", response_model=list[VisibleBrandTaskPublic])
def deleted_visible_tasks(current_user: CurrentUser, db: DbSession) -> list[VisibleBrandTaskPublic]:
    return list_deleted_visible_tasks(db, current_user)


@router.get("/articles", response_model=ArticlesResponse)
def visible_articles(
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=5000, ge=1, le=10000),
    updated_after: str = Query(default="", max_length=64),
    updated_after_id: int = Query(default=0, ge=0),
) -> ArticlesResponse:
    return ArticlesResponse(**list_visible_articles(
        db,
        current_user,
        limit=limit,
        updated_after=updated_after,
        updated_after_id=updated_after_id,
    ))


@router.get("/{task_id}/article-reference-ranking", response_model=ReferenceRankingResponse)
def article_reference_ranking(task_id: int, current_user: CurrentUser, db: DbSession) -> ReferenceRankingResponse:
    return ReferenceRankingResponse(
        algorithm_version=REFERENCE_ALGORITHM_VERSION,
        items=build_reference_ranking(db, current_user, task_id),
    )


@router.post("/day-status-events/batch", response_model=TaskDayStatusEventsBatchResponse)
def task_day_status_events_batch(
    payload: TaskDayStatusEventsBatchRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> TaskDayStatusEventsBatchResponse:
    return TaskDayStatusEventsBatchResponse(
        events=list_task_day_status_events_batch(
            db,
            current_user,
            payload.task_cursors,
            limit_per_task=payload.limit_per_task,
        )
    )


@router.post("/run-records/batch", response_model=RunRecordsBatchResponse)
def task_run_records_batch(
    payload: RunRecordsBatchRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> RunRecordsBatchResponse:
    return RunRecordsBatchResponse(
        records=list_task_run_records_batch(
            db,
            current_user,
            payload.task_cursors,
            limit_per_task=payload.limit_per_task,
        )
    )


@router.get("/{task_id}/run-records", response_model=list[RunRecordPublic])
def task_run_records(
    task_id: int,
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=10000),
    since_id: int = Query(default=0, ge=0),
) -> list[RunRecordPublic]:
    return list_task_run_records(db, current_user, task_id, limit=limit, since_id=since_id)
