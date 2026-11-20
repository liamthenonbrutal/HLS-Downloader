"""Core download engine: wraps yt-dlp + ffmpeg in a small job manager.

Handles HLS (.m3u8 master/media playlists, AES-128), DASH (.mpd), direct media
files (.mp4, .webm, .mp3, ...) and pages from any site yt-dlp supports.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yt_dlp

DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"

QUALITY_FORMATS = {
    "best": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/b",
    "360": "bv*[height<=360]+ba/b[height<=360]/b",
    "audio": "ba/b",
}

VIDEO_CONTAINERS = {"mp4", "mkv", "webm"}
AUDIO_CODECS = {"mp3", "m4a", "opus", "wav"}


class Cancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    url: str
    status: str = "queued"  # queued | downloading | processing | done | error | cancelled
    title: str = ""
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    downloaded: str = ""
    total: str = ""
    filename: str = ""
    error: str = ""
    created: float = field(default_factory=time.time)
    cancel_requested: bool = False

    def public(self) -> dict:
        d = asdict(self)
        d.pop("cancel_requested")
        return d


def _fmt_bytes(n) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_eta(s) -> str:
    if s is None:
        return ""
    s = int(s)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_options(
    quality: str = "best",
    container: str = "mp4",
    referer: str = "",
    user_agent: str = "",
    cookies: str = "",
    headers: dict | None = None,
    filename: str = "",
    playlist: bool = False,
    out_dir: Path = DOWNLOAD_DIR,
) -> dict:
    """Translate simple UI choices into yt-dlp options."""
    out_dir.mkdir(parents=True, exist_ok=True)
    http_headers = dict(headers or {})
    if referer:
        http_headers["Referer"] = referer
    if user_agent:
        http_headers["User-Agent"] = user_agent
    if cookies:
        http_headers["Cookie"] = cookies

    stem = yt_dlp.utils.sanitize_filename(filename.strip(), restricted=False) if filename.strip() else ""
    template = f"{stem}.%(ext)s" if stem else "%(title).150B [%(id)s].%(ext)s"
    if playlist and stem:
        template = f"{stem} %(playlist_index)s.%(ext)s"

    opts: dict = {
        "outtmpl": str(out_dir / template),
        "format": QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"]),
        "noplaylist": not playlist,
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": True,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "http_headers": http_headers,
        # Let the generic extractor treat bare .m3u8/.mpd/.mp4 URLs as media.
        "default_search": "auto_warning",
    }

    if quality == "audio" or container in AUDIO_CODECS:
        codec = container if container in AUDIO_CODECS else "mp3"
        opts["format"] = QUALITY_FORMATS["audio"]
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "0"}
        ]
    else:
        container = container if container in VIDEO_CONTAINERS else "mp4"
        opts["merge_output_format"] = container
        # Remux (no re-encode) HLS .ts output into the chosen container.
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": container}]
        # Fix AAC bitstream from MPEG-TS segments when putting them in mp4.
        opts["fixup"] = "detect_or_warn"
    return opts


def probe(url: str, **kwargs) -> dict:
    """Return title + available formats without downloading."""
    opts = build_options(**kwargs)
    opts.update({"skip_download": True, "noplaylist": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        return {
            "title": info.get("title") or "Playlist",
            "is_playlist": True,
            "count": len(entries),
            "formats": [],
        }
    formats = []
    for f in info.get("formats") or []:
        formats.append(
            {
                "id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution") or ("audio only" if f.get("vcodec") == "none" else ""),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "tbr": f.get("tbr"),
                "protocol": f.get("protocol"),
                "size": _fmt_bytes(f.get("filesize") or f.get("filesize_approx")),
            }
        )
    return {
        "title": info.get("title"),
        "is_playlist": False,
        "duration": info.get("duration"),
        "is_live": info.get("is_live"),
        "thumbnail": info.get("thumbnail"),
        "formats": formats,
    }


class JobManager:
    def __init__(self, max_parallel: int = 3):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(max_parallel)

    def submit(self, url: str, **kwargs) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], url=url)
        with self.lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, kwargs), daemon=True).start()
        return job

    def list(self) -> list[dict]:
        with self.lock:
            return [j.public() for j in sorted(self.jobs.values(), key=lambda j: -j.created)]

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status in ("done", "error", "cancelled"):
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
        return True

    def remove(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.status in ("done", "error", "cancelled"):
                del self.jobs[job_id]
                return True
        return False

    def _run(self, job: Job, kwargs: dict) -> None:
        with self.slots:
            if job.cancel_requested:
                job.status = "cancelled"
                return
            job.status = "downloading"
            try:
                opts = build_options(**kwargs)
                opts["progress_hooks"] = [lambda d: self._on_progress(job, d)]
                opts["postprocessor_hooks"] = [lambda d: self._on_post(job, d)]
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(job.url, download=True)
                    job.title = job.title or (info or {}).get("title", "")
                    final = _final_path(info)
                    if final:
                        job.filename = Path(final).name
                job.progress = 100.0
                job.status = "done"
            except Cancelled:
                job.status = "cancelled"
            except Exception as e:  # noqa: BLE001 - surface any failure to the UI
                if job.cancel_requested:
                    job.status = "cancelled"
                else:
                    job.status = "error"
                    job.error = yt_dlp.utils.remove_terminal_sequences(str(e)).replace("ERROR: ", "")

    def _on_progress(self, job: Job, d: dict) -> None:
        if job.cancel_requested:
            raise Cancelled()
        info = d.get("info_dict") or {}
        job.title = info.get("title") or job.title
        if d["status"] == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                job.progress = min(99.0, done * 100 / total)
            elif d.get("fragment_count"):
                job.progress = min(99.0, (d.get("fragment_index") or 0) * 100 / d["fragment_count"])
            job.downloaded = _fmt_bytes(done)
            job.total = _fmt_bytes(total)
            job.speed = (_fmt_bytes(d.get("speed")) + "/s") if d.get("speed") else ""
            job.eta = _fmt_eta(d.get("eta"))
        elif d["status"] == "finished":
            job.filename = Path(d.get("filename", "")).name or job.filename

    def _on_post(self, job: Job, d: dict) -> None:
        if job.cancel_requested:
            raise Cancelled()
        if d["status"] == "started":
            job.status = "processing"
            job.speed = job.eta = ""


def _final_path(info: dict | None) -> str | None:
    if not info:
        return None
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        return _final_path(entries[-1]) if entries else None
    reqs = info.get("requested_downloads") or []
    if reqs:
        return reqs[-1].get("filepath") or reqs[-1].get("_filename")
    return info.get("filepath") or info.get("_filename")


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None
