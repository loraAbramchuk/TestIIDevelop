"""Strict probe validation, bounded subprocesses and one compatible output profile."""

import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from fractions import Fraction

from app.processing.errors import LeaseLost, ProcessingError, TransientProcessingError


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    audio: bool


def validate_probe(document: dict, settings) -> MediaInfo:
    try:
        streams = document["streams"]
        videos = [s for s in streams if s["codec_type"] == "video"]
        audios = [s for s in streams if s["codec_type"] == "audio"]
        if len(videos) != 1 or len(audios) > 1 or len(streams) != len(videos) + len(audios):
            raise ValueError
        video = videos[0]
        format_ = document["format"]
        if "mp4" not in format_["format_name"].split(","):
            raise ValueError
        brand = format_.get("tags", {}).get("major_brand", "").strip()
        if brand not in {
            "isom",
            "iso2",
            "iso3",
            "iso4",
            "iso5",
            "iso6",
            "mp41",
            "mp42",
            "avc1",
            "M4V",
            "MSNV",
            "dash",
        }:
            raise ValueError
        if (
            video["codec_name"] != "h264"
            or video["pix_fmt"] != "yuv420p"
            or video["profile"] not in ("Baseline", "Constrained Baseline", "Main", "High")
            or not 0 < int(video["level"]) <= 42
            or video.get("field_order") != "progressive"
            or video.get("disposition", {}).get("attached_pic", 0)
        ):
            raise ValueError
        width, height = int(video["width"]), int(video["height"])
        if (
            min(width, height) < 1
            or max(width, height) > settings.max_video_long_side
            or min(width, height) > settings.max_video_short_side
        ):
            raise ProcessingError("video_dimensions_exceeded")
        for key in ("avg_frame_rate", "r_frame_rate"):
            rate = Fraction(video[key])
            if not 0 < rate <= settings.max_video_fps:
                raise ProcessingError("video_frame_rate_exceeded")
        duration = float(format_["duration"])
        if not math.isfinite(duration) or not 0 < duration <= settings.max_video_duration_seconds:
            raise ProcessingError("video_duration_exceeded")
        for stream in streams:
            if "duration" in stream:
                length = float(stream["duration"])
                if (
                    not math.isfinite(length)
                    or not 0 < length <= settings.max_video_duration_seconds
                ):
                    raise ProcessingError("video_duration_exceeded")
        if audios:
            audio = audios[0]
            if (
                audio["codec_name"] != "aac"
                or audio["profile"] != "LC"
                or int(audio["channels"]) not in (1, 2)
                or int(audio["sample_rate"]) not in (44100, 48000)
            ):
                raise ValueError
        rotations = [
            float(item["rotation"])
            for item in video.get("side_data_list", [])
            if "rotation" in item
        ]
        if any(not math.isfinite(r) or r % 90 != 0 for r in rotations):
            raise ValueError
        if rotations and rotations[0] % 180:
            width, height = height, width
        return MediaInfo(duration, width, height, bool(audios))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise ProcessingError("unsupported_media") from None


class MediaProcessor:
    def __init__(self, settings, descriptors, renew):
        self.settings = settings
        self.descriptors = descriptors
        self.renew = renew
        self.deadline = time.monotonic() + settings.processing_timeout_seconds
        self.heartbeat = 0.0

    def check(self):
        now = time.monotonic()
        if now >= self.deadline:
            raise ProcessingError("processing_timeout")
        if now - self.heartbeat >= self.settings.processing_heartbeat_seconds:
            if not self.renew():
                raise LeaseLost("processing_lease_lost")
            self.heartbeat = time.monotonic()

    def run(self, tool, arguments, *, maximum, timeout=None, output_limit=65536):
        self.check()
        deadline = min(self.deadline, time.monotonic() + timeout) if timeout else self.deadline
        command = [
            sys.executable,
            "-m",
            "app.processing.sandbox",
            str(os.getpid()),
            str(self.settings.processing_memory_bytes),
            str(self.settings.processing_timeout_seconds + 1),
            str(maximum),
            tool,
            *arguments,
        ]
        output = bytearray()
        # Every media process inherits locks. Even an orphan cannot overlap another writer.
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            pass_fds=self.descriptors,
            start_new_session=True,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    self.check()
                    if time.monotonic() >= deadline:
                        raise ProcessingError("media_probe_timeout")
                    for key, _ in selector.select(timeout=0.2):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > output_limit:
                            raise ProcessingError("media_output_limit")
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            self.check()
            if process.returncode:
                if any(
                    message in output
                    for message in (b"No space left on device", b"Disk quota exceeded")
                ):
                    raise TransientProcessingError("processing_storage_full")
                if any(
                    message in output
                    for message in (
                        b"Read-only file system",
                        b"Permission denied",
                        b"Input/output error",
                    )
                ):
                    raise TransientProcessingError("processing_storage_unavailable")
                code = (
                    "processing_budget_exceeded"
                    if process.returncode in (-signal.SIGXFSZ, -signal.SIGKILL, -signal.SIGXCPU)
                    or b"File too large" in output
                    or b"Cannot allocate memory" in output
                    else "invalid_media"
                )
                raise ProcessingError(code)
            return bytes(output)
        except subprocess.TimeoutExpired:
            raise ProcessingError("processing_timeout") from None
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            process.stdout.close()

    @staticmethod
    def input_options():
        return [
            "-threads",
            "1",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "mov",
            "-enable_drefs",
            "0",
            "-use_absolute_path",
            "0",
        ]

    def probe(self, source):
        raw = self.run(
            "ffprobe",
            [
                "-v",
                "error",
                "-max_alloc",
                "67108864",
                *self.input_options(),
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                source,
            ],
            maximum=1,
            timeout=self.settings.ffprobe_timeout_seconds,
            output_limit=1024**2,
        )
        try:
            return validate_probe(json.loads(raw), self.settings)
        except (ValueError, UnicodeDecodeError):
            raise ProcessingError("invalid_media") from None

    def prepare(self, source, destination, source_size, reserved_budget):
        info = self.probe(source)
        budget = min(source_size + self.settings.preview_budget_bytes, reserved_budget)
        if budget <= 0:
            raise ProcessingError("processing_budget_exceeded")
        common = [
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-max_alloc",
            "67108864",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
        ]
        self.run(
            "ffmpeg",
            [
                *common,
                "-xerror",
                "-err_detect",
                "explode",
                *self.input_options(),
                "-i",
                source,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                destination.path("playback.part"),
            ],
            maximum=budget,
        )
        prepared = self.probe(destination.path("playback.part"))
        if abs(prepared.duration - info.duration) > 0.1:
            raise ProcessingError("invalid_media")
        # Decode the complete output; a valid header does not prove valid H.264/AAC packets.
        self.run(
            "ffmpeg",
            [
                *common,
                "-xerror",
                "-err_detect",
                "explode",
                *self.input_options(),
                "-i",
                destination.path("playback.part"),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-threads",
                "1",
                "-f",
                "null",
                "/dev/null",
            ],
            maximum=1,
        )
        remaining = min(
            self.settings.preview_budget_bytes, budget - destination.size("playback.part")
        )
        if remaining <= 0:
            raise ProcessingError("processing_budget_exceeded")
        self.run(
            "ffmpeg",
            [
                *common,
                *self.input_options(),
                "-ss",
                str(min(info.duration / 2, 1)),
                "-i",
                source,
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                "scale=w='min(640,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease",
                "-threads",
                "1",
                "-c:v",
                "mjpeg",
                "-q:v",
                "3",
                "-f",
                "image2",
                destination.path("thumbnail.part"),
            ],
            maximum=remaining,
        )
        if destination.size("thumbnail.part") <= 0 or destination.size("playback.part") <= 0:
            raise ProcessingError("invalid_media")
        destination.publish()
        return prepared
