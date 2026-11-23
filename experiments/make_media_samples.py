"""Create tiny compatibility fixtures outside the checkout; requires system FFmpeg/ffprobe."""

import argparse
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/stage1-media"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    samples = {}
    for name, audio, pixel_format in (
        ("h264-aac", True, "yuv420p"),
        ("h264-silent", False, "yuv420p"),
        ("unsupported-10bit", False, "yuv420p10le"),
    ):
        path = args.output / f"{name}.mp4"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
        ]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        command += [
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "veryfast",
            "-pix_fmt",
            pixel_format,
        ]
        if pixel_format == "yuv420p":
            command += ["-profile:v", "main", "-level:v", "3.1"]
        if audio:
            command += ["-c:a", "aac", "-profile:a", "aac_low", "-b:a", "64k"]
        command += ["-movflags", "+faststart", str(path)]
        subprocess.run(command, check=True, timeout=30)
        probe = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ],
                timeout=10,
            )
        )
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        assert video["codec_name"] == "h264" and video["pix_fmt"] == pixel_format
        audios = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        assert bool(audios) == audio
        if audios:
            assert audios[0]["codec_name"] == "aac" and audios[0]["profile"] == "LC"
        samples[name] = {
            "video_codec": video["codec_name"],
            "pixel_format": video["pix_fmt"],
            "profile": video["profile"],
            "width": video["width"],
            "height": video["height"],
            "audio": "AAC-LC" if audio else None,
            "duration": float(probe["format"]["duration"]),
            "allowed_by_stage1_profile": pixel_format == "yuv420p",
        }
    report = {
        "ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
        "samples": samples,
    }
    (args.output / "probe.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
