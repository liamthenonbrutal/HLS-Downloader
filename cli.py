"""Command-line version.

Examples:
  python cli.py "https://example.com/stream/master.m3u8"
  python cli.py URL1 URL2 -q 720 -c mkv --referer https://example.com/
  python cli.py "https://example.com/video" -q audio -c mp3
  python cli.py URL --info          # list formats only
"""

from __future__ import annotations

import argparse
import sys
import time

from downloader import DOWNLOAD_DIR, QUALITY_FORMATS, JobManager, check_ffmpeg, probe


def main() -> int:
    p = argparse.ArgumentParser(description="Download HLS (.m3u8), DASH (.mpd), direct media and site videos.")
    p.add_argument("urls", nargs="+")
    p.add_argument("-q", "--quality", default="best", choices=list(QUALITY_FORMATS))
    p.add_argument("-c", "--container", default="mp4", help="mp4, mkv, webm | mp3, m4a, opus, wav")
    p.add_argument("-o", "--filename", default="", help="output name without extension")
    p.add_argument("--referer", default="")
    p.add_argument("--user-agent", default="")
    p.add_argument("--cookies", default="", help='raw Cookie header, e.g. "a=1; b=2"')
    p.add_argument("-H", "--header", action="append", default=[], help='extra header "Name: value"')
    p.add_argument("--playlist", action="store_true", help="download every item of a playlist page")
    p.add_argument("--info", action="store_true", help="show title and formats, don't download")
    a = p.parse_args()

    headers = {}
    for h in a.header:
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()
    opts = dict(quality=a.quality, container=a.container, referer=a.referer,
                user_agent=a.user_agent, cookies=a.cookies, headers=headers)

    if a.info:
        for url in a.urls:
            info = probe(url, **opts)
            print(f"\n{info['title']}")
            for f in info["formats"]:
                print(f"  {f['id']:>12}  {f['ext']:>5}  {f['resolution'] or '':>12}  "
                      f"{(f['tbr'] or 0):>7.0f}k  {f['protocol'] or ''}")
        return 0

    if not check_ffmpeg():
        print("WARNING: ffmpeg not found - merging/converting will fail.", file=sys.stderr)

    mgr = JobManager(max_parallel=3)
    jobs = [mgr.submit(u, filename=a.filename if len(a.urls) == 1 else "",
                       playlist=a.playlist, **opts) for u in a.urls]
    try:
        while any(j.status in ("queued", "downloading", "processing") for j in jobs):
            line = " | ".join(
                f"{(j.title or j.url)[:30]}: {j.status} {j.progress:5.1f}% {j.speed}" for j in jobs
            )
            print("\r" + line[:200].ljust(200), end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        for j in jobs:
            mgr.cancel(j.id)
        print("\nCancelled.")
        return 130
    print()
    failed = 0
    for j in jobs:
        if j.status == "done":
            print(f"OK    {DOWNLOAD_DIR / j.filename}")
        else:
            failed += 1
            print(f"FAIL  {j.url}\n      {j.error}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
