"""Web UI for the downloader.  Run:  python app.py  then open http://127.0.0.1:8000"""

from __future__ import annotations

import argparse
import os

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from downloader import DOWNLOAD_DIR, JobManager, check_ffmpeg, probe

app = Flask(__name__)
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


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="HLS / video downloader web UI")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other devices on your network")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()
    if not check_ffmpeg():
        print("WARNING: ffmpeg not found on PATH - merging/converting will fail. Install ffmpeg.")
    print(f"Downloads go to: {DOWNLOAD_DIR}")
    print(f"Open http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, threaded=True)
