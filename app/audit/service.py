from sqlalchemy import or_, select, tuple_

from app.models import AuditEvent
from app.pagination import decode_cursor, encode_cursor


def record(db, actor_id, action, object_type, object_id, reason, **details):
    # The caller owns the transaction; failure to write the audit rolls back its action.
    db.add(
        AuditEvent(
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            reason=reason,
            details=details,
        )
    )


def list_events(db, viewer_id, video_id, limit, cursor):
    scope = f"audit:{viewer_id}:{video_id or 'all'}"
    position = decode_cursor(cursor, scope)
    query = select(AuditEvent)
    if video_id:
        query = query.where(
            or_(
                (AuditEvent.object_type == "video") & (AuditEvent.object_id == video_id),
                AuditEvent.details["video_id"].astext == str(video_id),
            )
        )
    if position:
        query = query.where(tuple_(AuditEvent.created_at, AuditEvent.id) < position)
    rows = list(
        db.scalars(
            query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
        )
    )
    page = rows[:limit]
    return {
        "items": page,
        "next_cursor": encode_cursor(scope, page[-1].created_at, page[-1].id)
        if len(rows) > limit
        else None,
    }
