from __future__ import annotations

from fastapi import APIRouter
from fastapi import Query

from app.api.deps import CurrentUser, DbSession
from app.schemas import ReferenceRankingResponse, RunRecordPublic, VisibleBrandTaskPublic
from app.services.sync_service import (
    REFERENCE_ALGORITHM_VERSION,
    build_reference_ranking,
    list_deleted_visible_tasks,
    list_task_run_records,
    list_visible_tasks,
)

router = APIRouter()


@router.get("", response_model=list[VisibleBrandTaskPublic])
def visible_tasks(current_user: CurrentUser, db: DbSession) -> list[VisibleBrandTaskPublic]:
    return list_visible_tasks(db, current_user)


@router.get("/", response_model=list[VisibleBrandTaskPublic], include_in_schema=False)
def visible_tasks_with_slash(current_user: CurrentUser, db: DbSession) -> list[VisibleBrandTaskPublic]:
    return list_visible_tasks(db, current_user)


@router.get("/deleted", response_model=list[VisibleBrandTaskPublic])
def deleted_visible_tasks(current_user: CurrentUser, db: DbSession) -> list[VisibleBrandTaskPublic]:
    return list_deleted_visible_tasks(db, current_user)


@router.get("/{task_id}/article-reference-ranking", response_model=ReferenceRankingResponse)
def article_reference_ranking(task_id: int, current_user: CurrentUser, db: DbSession) -> ReferenceRankingResponse:
    return ReferenceRankingResponse(
        algorithm_version=REFERENCE_ALGORITHM_VERSION,
        items=build_reference_ranking(db, current_user, task_id),
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
