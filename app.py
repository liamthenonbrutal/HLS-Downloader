"""Web UI for the downloader.  Run:  python app.py  then open http://127.0.0.1:8000"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import webbrowser

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from downloader import BUNDLE_DIR, DOWNLOAD_DIR, FROZEN, JobManager, check_ffmpeg, probe

app = Flask(__name__, template_folder=str(BUNDLE_DIR / "templates"))
manager = JobManager(max_parallel=int(os.environ.get("MAX_PARALLEL", "3")))

OPTION_KEYS = ("quality", "container", "referer", "user_agent", "cookies", "filename", "playlist")


def _options(data: dict) -> dict:
    opts = {k: data[k] for k in OPTION_KEYS if k in data and data[k] not in (None, "")}
    if "playlist" in opts:
        opts["playlist"] = bool(opts["playlist"])
    return opts


@app.get("/")
def index():
    return render_template("index.html", ffmpeg=check_ffmpeg())


@app.post("/api/probe")
def api_probe():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="URL is required"), 400
    try:
        opts = _options(data)
        opts.pop("playlist", None)
        opts.pop("filename", None)
        return jsonify(probe(url, **opts))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e).replace("ERROR: ", "")), 400


@app.post("/api/download")
def api_download():
    data = request.get_json(force=True)
    urls = [u.strip() for u in (data.get("url") or "").splitlines() if u.strip()]
    if not urls:
        return jsonify(error="URL is required"), 400
    opts = _options(data)
    if len(urls) > 1:
        opts.pop("filename", None)  # one name can't apply to many files
    jobs = [manager.submit(u, **opts).public() for u in urls]
    return jsonify(jobs=jobs)


@app.get("/api/jobs")
def api_jobs():
    return jsonify(jobs=manager.list())


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id):
    return jsonify(ok=manager.cancel(job_id))


@app.delete("/api/jobs/<job_id>")
def api_remove(job_id):
    return jsonify(ok=manager.remove(job_id))


@app.get("/files/<path:name>")
def files(name):
    if not (DOWNLOAD_DIR / name).is_file():
        abort(404)
    return send_from_directory(DOWNLOAD_DIR, name, as_attachment=True)


def _free_port(host: str, preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 50)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) != 0:
                return port
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="HLS / video downloader web UI")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other devices on your network")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    a = p.parse_args()
    port = _free_port(a.host, a.port) or a.port
    url = f"http://{'127.0.0.1' if a.host == '0.0.0.0' else a.host}:{port}"
    if not check_ffmpeg():
        print("WARNING: ffmpeg not found on PATH - merging/converting will fail. Install ffmpeg.")
    print("HLS Downloader is running.")
    print(f"  Open:       {url}")
    print(f"  Downloads:  {DOWNLOAD_DIR}")
    if FROZEN:
        print("  Close this window to quit.")
    if not a.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host=a.host, port=port, threaded=True)


if __name__ == "__main__":
    main()
