from pydantic import BaseModel

from app.channels.schemas import ChannelSummary
from app.video_contracts import VideoView


class CatalogVideo(VideoView):
    author: ChannelSummary


class CatalogPage(BaseModel):
    items: list[CatalogVideo]
    next_cursor: str | None = None
