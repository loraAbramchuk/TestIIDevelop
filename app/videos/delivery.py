"""Authorize every request, validate immutable output keys, delegate bytes to Nginx."""

import os
import re
import stat

from fastapi import Response

from app.videos.service import VideoError, visible_record

OUTPUT = re.compile(r"processing/([0-9a-f]{32})/(playback\.mp4|thumbnail\.jpg)")


def media_response(db, settings, video_id, viewer_id, *, thumbnail=False):
    video = visible_record(db, video_id, viewer_id)
    key = video.thumbnail_key if thumbnail else video.playback_key
    size = video.thumbnail_bytes if thumbnail else video.playback_bytes
    filename = "thumbnail.jpg" if thumbnail else "playback.mp4"
    match = OUTPUT.fullmatch(key or "")
    if not match or match[2] != filename or size is None or size <= 0:
        raise unavailable()
    descriptors = []
    try:
        directory = os.open(settings.media_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for name in ("processing", match[1]):
            directory = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            descriptors.append(directory)
        # O_NONBLOCK also fails closed for an unexpected FIFO/device in storage.
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise unavailable()
    except OSError:
        raise unavailable() from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    return Response(
        media_type="image/jpeg" if thumbnail else "video/mp4",
        headers={
            "X-Accel-Redirect": f"/_protected_media/{match[1]}/{filename}",
            "Cache-Control": "private, no-store",
        },
    )


def unavailable():
    return VideoError(503, "media_unavailable", "Файл временно недоступен. Попробуйте позже")
