"""Shared event names for the cloud sync API."""

EVENT_PROFILE_UPDATE = "profile_update"
EVENT_RUN_RECORD = "run_record"
EVENT_ARTICLE_UPSERT = "article_upsert"
EVENT_ARTICLE_TASK_LINKS = "article_task_links"
EVENT_ARTICLE_REFERENCE = "article_reference_event"
EVENT_TASK_DAY_STATUS = "task_day_status"

EVENT_TASK_CHANGED = "task_changed"
EVENT_ASSIGNMENT_CHANGED = "assignment_changed"
EVENT_RUN_RECORD_CHANGED = "run_record_changed"
EVENT_TASK_DAY_STATUS_CHANGED = "task_day_status_changed"
EVENT_ARTICLE_CHANGED = "article_changed"
EVENT_REFERENCE_CHANGED = "reference_changed"
EVENT_WORKSPACE_CHANGED = "workspace_changed"

FULL_TASK_PULL_EVENT_NAMES = frozenset(
    {
        EVENT_TASK_CHANGED,
        EVENT_ASSIGNMENT_CHANGED,
        EVENT_WORKSPACE_CHANGED,
    }
)
