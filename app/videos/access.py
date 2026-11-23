from uuid import UUID

from sqlalchemy import and_, or_, select

from app.videos.models import Video, VideoGrant


def viewable_by(viewer_id: UUID | None):
    """The same SQL policy is used for individual reads and future catalog queries.

    System roles intentionally do not participate. Ownership/grants never bypass
    readiness, moderation or deletion.
    """
    audience = Video.visibility == "public"
    if viewer_id is not None:
        grant = (
            select(VideoGrant.video_id)
            .where(VideoGrant.video_id == Video.id, VideoGrant.user_id == viewer_id)
            .exists()
        )
        audience = or_(audience, Video.owner_id == viewer_id, grant)
    return and_(
        Video.deleted_at.is_(None),
        Video.processing_status == "ready",
        Video.moderation_status == "visible",
        audience,
    )
