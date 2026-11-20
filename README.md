# HLS Downloader

A small local app (web UI + command line) that downloads:

- **HLS playlists** (`.m3u8`): master playlists with several qualities, single media playlists, AES-128 encrypted streams
- **DASH manifests** (`.mpd`)
- **Direct media links** (`.mp4`, `.webm`, `.mp3`, …)
- **Video pages** on any site [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports

Segments are fetched in parallel and remuxed (no re-encode) into MP4/MKV/WebM, or converted to MP3/M4A/Opus/WAV for audio only.

## Setup

Needs Python 3.9+ and [ffmpeg](https://ffmpeg.org/download.html) on your PATH.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Web UI

```bash
python app.py
```

Open http://127.0.0.1:8000, paste one or more URLs (one per line) and press **Download**.
**Check formats** lists every quality a playlist offers. Under **Advanced** you can set
Referer, User-Agent and Cookie headers, which many protected HLS streams require
(copy them from your browser's DevTools → Network tab, on the `.m3u8` request).

Files are saved to `downloads/`. Use `--host 0.0.0.0` to reach it from other devices on your network.

## Command line

```bash
python cli.py "https://example.com/stream/master.m3u8"
python cli.py URL -q 720 -c mkv -o my-video --referer https://example.com/
python cli.py URL -q audio -c mp3
python cli.py URL --info                       # list formats only
python cli.py URL -H "Authorization: Bearer xyz"
```

## Limits

DRM-protected streams (Widevine, FairPlay, PlayReady) can't be downloaded. Only download content you have the right to.
Keep yt-dlp current (`pip install -U yt-dlp`) since sites change often.
