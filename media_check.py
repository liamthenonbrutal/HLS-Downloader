"""Make sure every finished download is a real, playable media file.

Runs after yt-dlp has downloaded (and merged) a file:
  1. ffprobe it.
  2. If it isn't media, try to recover it (streams hidden behind a fake
     PNG/JPEG/GIF header, or junk before the MPEG-TS / MP4 data).
  3. Remux or convert it to the container the user asked for.
  4. If it still isn't media, delete it and explain what the server sent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import PostProcessingError

TS_PACKET = 188
# A "video" made only of these is really a picture (e.g. a stream disguised as PNG).
IMAGE_CODECS = {"png", "mjpeg", "gif", "bmp", "webp", "tiff", "apng", "jpegls"}
AUDIO_ARGS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "0"],
    "m4a": ["-c:a", "aac", "-b:a", "192k"],
    "opus": ["-c:a", "libopus", "-b:a", "160k"],
    "wav": ["-c:a", "pcm_s16le"],
}
# Hide the console windows ffmpeg would flash on Windows.
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PostProcessingError(f"{name} was not found. Install ffmpeg (it includes {name}).")
    return path


def probe_streams(path: Path) -> list[dict]:
    """Return the audio/video streams ffprobe finds, or [] if it isn't media."""
    try:
        out = subprocess.run(
            [_tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120, **_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return []
    if out.returncode != 0:
        return []
    try:
        streams = json.loads(out.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        return []
    media = [s for s in streams if s.get("codec_type") in ("video", "audio")
             and s.get("codec_name") not in IMAGE_CODECS]
    return media


def _find_media_start(data: bytes) -> int | None:
    """Offset where real MPEG-TS or MP4 data begins, if junk precedes it."""
    # MPEG-TS: 0x47 sync byte repeating every 188 bytes.
    for i in range(min(len(data), 64 * 1024)):
        if data[i] == 0x47 and all(
            i + k * TS_PACKET < len(data) and data[i + k * TS_PACKET] == 0x47 for k in range(1, 6)
        ):
            return i
    # MP4 / fMP4: box size + 'ftyp'/'styp'/'moof'.
    m = re.search(rb"....(ftyp|styp|moof)", data[: 64 * 1024], re.S)
    return m.start() if m else None


def _extract_ts_packets(src: Path, dst: Path) -> int:
    """Copy only real MPEG-TS packets, dropping junk anywhere in the file
    (e.g. a fake PNG header on every HLS segment). Returns packets kept."""
    kept = 0
    buf = b""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        eof = False
        while not eof or len(buf) >= TS_PACKET:
            if not eof and len(buf) < 4 * 1024 * 1024:
                chunk = fin.read(4 * 1024 * 1024)
                eof = not chunk
                buf += chunk
                continue
            out = bytearray()
            i, n = 0, len(buf)
            limit = n - 2 * TS_PACKET if not eof else n - TS_PACKET
            while i <= limit:
                nxt = i + TS_PACKET
                if buf[i] == 0x47 and (nxt >= n or buf[nxt] == 0x47 or (eof and nxt == n)):
                    out += buf[i:nxt]
                    i = nxt
                else:
                    j = buf.find(b"\x47", i + 1)
                    i = j if j != -1 else n
            kept += len(out) // TS_PACKET
            fout.write(out)
            buf = buf[i:]
            if eof:
                break
    return kept


def try_recover(path: Path) -> Path | None:
    """Recover media hidden behind fake headers / junk. Returns the repaired file or None."""
    with open(path, "rb") as f:
        head = f.read(64 * 1024 + TS_PACKET * 6)
    start = _find_media_start(head)
    if start is None:
        return None
    fixed = path.with_name(path.stem + ".recovered.ts")
    if head[start] == 0x47:
        _extract_ts_packets(path, fixed)
    elif start > 0:  # MP4 after junk
        fixed = path.with_name(path.stem + ".recovered.mp4")
        with open(path, "rb") as src, open(fixed, "wb") as dst:
            src.seek(start)
            shutil.copyfileobj(src, dst)
    else:
        return None
    if probe_streams(fixed):
        return fixed
    fixed.unlink(missing_ok=True)
    return None


def describe_bad_file(path: Path) -> str:
    """Human explanation of what the server sent instead of media."""
    size = path.stat().st_size if path.exists() else 0
    with open(path, "rb") as f:
        head = f.read(8192)
    text = head.decode("utf-8", "replace").lstrip("﻿").strip()
    low = text[:200].lower()
    links = re.findall(r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mpd|mp4|webm)(?:\?[^\s\"'<>\\]*)?", text)
    hint = ""
    if links:
        hint = " It contains these media links, try downloading one of them instead: " + "  ".join(dict.fromkeys(links[:3]))

    if size == 0:
        return "The server sent an empty file. The link has probably expired; copy a fresh one."
    if low.startswith(("<!doctype", "<html", "<?xml", "<head", "<body")) or "<html" in low:
        return ("The server sent a web page instead of video. Usually the link expired, or the site "
                "needs the Referer / cookies from your browser (see Advanced)." + hint)
    if low.startswith(("{", "[")):
        return "The server sent JSON data, not video. This is an API link, not the stream itself." + hint
    if low.startswith("#extm3u"):
        return "The server sent an HLS playlist that has no playable segments (possibly DRM-protected)."
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in head[:512])
    if head and printable / min(len(head), 512) > 0.95:
        return f"The server sent text, not video: \"{text[:150]}\"" + hint
    return (f"The downloaded data ({size // 1024} KB) isn't a recognisable video or audio format. "
            "It's probably encrypted or DRM-protected, or the link isn't a direct stream. "
            "Try the page URL instead, or copy the .m3u8 link from your browser's DevTools → Network tab.")


def convert(src: Path, target: str, audio_only: bool) -> Path:
    """Remux (video) or transcode (audio) src into target container, return new path."""
    dst = src.with_name(re.sub(r"\.recovered$", "", src.stem) + "." + target)
    if dst == src:
        return src
    tmp = dst.with_name(dst.stem + ".converting." + target)
    if audio_only:
        codec_args = AUDIO_ARGS[target]
        if target == "m4a" and any(s.get("codec_name") == "aac" for s in probe_streams(src)):
            codec_args = ["-c:a", "copy"]
        args = ["-vn", *codec_args]
    else:
        args = ["-map", "0:v?", "-map", "0:a?", "-c", "copy"]
        if target == "mp4":
            args += ["-movflags", "+faststart"]
    res = subprocess.run(
        [_tool("ffmpeg"), "-y", "-v", "error", "-i", str(src), *args, str(tmp)],
        capture_output=True, text=True, **_NO_WINDOW,
    )
    if res.returncode != 0 and not audio_only:
        # Codec not allowed in this container (e.g. VP9 in mp4 on old ffmpeg): re-encode.
        res = subprocess.run(
            [_tool("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-map", "0:v?", "-map", "0:a?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", str(tmp)],
            capture_output=True, text=True, **_NO_WINDOW,
        )
    if res.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise PostProcessingError("Converting failed: " + (res.stderr.strip().splitlines() or ["unknown error"])[-1])
    dst.unlink(missing_ok=True)
    tmp.replace(dst)
    src.unlink(missing_ok=True)
    return dst


def finalize(path: Path, target: str, audio_only: bool) -> Path:
    if not path.exists():
        raise PostProcessingError("The download produced no file.")
    first_error = None
    if probe_streams(path):
        try:
            return convert(path, target, audio_only)
        except PostProcessingError as e:
            first_error = e  # ffprobe was fooled; try recovering below
    recovered = try_recover(path)
    if recovered:
        try:
            out = convert(recovered, target, audio_only)
            if path.resolve() != out.resolve():
                path.unlink(missing_ok=True)
            return out
        except PostProcessingError:
            recovered.unlink(missing_ok=True)
    reason = describe_bad_file(path) if first_error is None else (
        "The file was downloaded but contains no playable audio/video (it may be encrypted or DRM-protected). "
        f"Details: {first_error}")
    path.unlink(missing_ok=True)
    raise PostProcessingError(reason)


class FinalizePP(PostProcessor):
    """yt-dlp post-processor wrapper so this runs once per downloaded item."""

    def __init__(self, target: str, audio_only: bool, on_start=None):
        super().__init__(None)
        self.target, self.audio_only, self.on_start = target, audio_only, on_start

    def run(self, info):
        if self.on_start:
            self.on_start()
        src = Path(info.get("filepath") or info["_filename"])
        final = finalize(src, self.target, self.audio_only)
        info["filepath"] = str(final)
        info["ext"] = final.suffix.lstrip(".")
        return [], info
