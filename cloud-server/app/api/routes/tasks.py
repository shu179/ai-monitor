from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas import ReferenceRankingResponse
from app.services.sync_service import REFERENCE_ALGORITHM_VERSION, build_reference_ranking

router = APIRouter()


@router.get("/{task_id}/article-reference-ranking", response_model=ReferenceRankingResponse)
def article_reference_ranking(task_id: int, current_user: CurrentUser, db: DbSession) -> ReferenceRankingResponse:
    return ReferenceRankingResponse(
        algorithm_version=REFERENCE_ALGORITHM_VERSION,
        items=build_reference_ranking(db, current_user, task_id),
    )

