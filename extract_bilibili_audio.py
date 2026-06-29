#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from mutagen.mp4 import MP4, MP4Cover


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def load_inline_json(page: str, marker: str) -> dict:
    start = page.find(marker)
    if start < 0:
        raise ValueError(f"Could not find {marker!r} in page dump")

    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(page[start + len(marker) :])
    if not isinstance(value, dict):
        raise ValueError(f"{marker!r} did not contain a JSON object")
    return value


def load_json_ld(page: str) -> dict:
    match = re.search(
        r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
        page,
        flags=re.DOTALL,
    )
    if not match:
        return {}
    return json.loads(html.unescape(match.group(1)))


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def fetch_page(url: str) -> tuple[str, str]:
    req = urllib.request.Request(url, headers=request_headers("https://www.bilibili.com/"))
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip":
                data = gzip.decompress(data)
            elif encoding == "deflate":
                data = zlib.decompress(data)
            charset = response.headers.get_content_charset() or "utf-8"
            page = data.decode(charset, errors="replace")
            return page, response.geturl()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Could not fetch Bilibili page {url}: {exc}") from exc


def load_page(source: str) -> tuple[str, str | None]:
    if is_url(source):
        return fetch_page(source)
    return Path(source).read_text(encoding="utf-8"), None


def infer_source_url(
    explicit_url: str | None,
    fetched_url: str | None,
    initial_state: dict,
    json_ld: dict,
) -> str:
    if explicit_url:
        return explicit_url
    if fetched_url:
        return fetched_url
    video_data = initial_state.get("videoData") or {}
    bvid = video_data.get("bvid")
    if bvid:
        return f"https://www.bilibili.com/video/{bvid}/"
    embed_url = json_ld.get("embedUrl") or ""
    match = re.search(r"bvid=([^&]+)", embed_url)
    if match:
        return f"https://www.bilibili.com/video/{match.group(1)}/"
    return "https://www.bilibili.com/"


def normalize_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    return url


def original_bili_image(url: str) -> str:
    url = normalize_url(url)
    if "hdslb.com" in url and "@" in url:
        return url.split("@", 1)[0]
    return url


def safe_filename(value: str, fallback: str = "bilibili-audio") -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n]+', " ", value).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.strip(". ")
    return value or fallback


def choose_track_title(video_title: str) -> str:
    match = re.search(r"《([^》]+)》", video_title)
    if match:
        return match.group(1).strip()
    return video_title.split("|", 1)[0].strip() or video_title


def request_headers(referer: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
    }


def download(url: str, dest: Path, referer: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers=request_headers(referer))
    try:
        with urllib.request.urlopen(req, timeout=90) as response, tmp.open("wb") as fh:
            shutil.copyfileobj(response, fh)
    except (HTTPError, URLError) as exc:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f"Download failed for {url}: {exc}") from exc
    tmp.replace(dest)


def remux_audio(source: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.stem + ".tmp.m4a")
    if tmp.exists():
        tmp.unlink()

    afconvert = shutil.which("afconvert")
    if afconvert:
        subprocess.run(
            [afconvert, "-f", "m4af", "-d", "0", str(source), str(tmp)],
            check=True,
        )
    else:
        shutil.copy2(source, tmp)
    tmp.replace(dest)


def image_format(data: bytes) -> int:
    if data.startswith(b"\xff\xd8\xff"):
        return MP4Cover.FORMAT_JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return MP4Cover.FORMAT_PNG
    raise ValueError("Cover image is neither JPEG nor PNG")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract the best Bilibili DASH audio stream to an Apple Music friendly .m4a."
    )
    parser.add_argument("source", help="Bilibili/b23 URL or a saved Bilibili page dump")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--album", default="Bilibili")
    parser.add_argument(
        "--source-url",
        help="Original Bilibili URL to write into metadata when source is a local dump",
    )
    args = parser.parse_args()

    page, fetched_url = load_page(args.source)
    playinfo = load_inline_json(page, "window.__playinfo__=")
    initial_state = load_inline_json(page, "window.__INITIAL_STATE__=")
    json_ld = load_json_ld(page)
    source_url = infer_source_url(args.source_url, fetched_url, initial_state, json_ld)

    audio_streams = playinfo["data"]["dash"]["audio"]
    best_audio = max(audio_streams, key=lambda item: int(item.get("bandwidth") or 0))
    audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
    if not audio_url:
        raise ValueError("Best audio stream did not include a URL")

    video_data = initial_state.get("videoData") or {}
    video_title = (
        json_ld.get("name")
        or video_data.get("title")
        or "Bilibili audio"
    )
    track_title = choose_track_title(video_title)
    artist = (
        (json_ld.get("author") or {}).get("name")
        or (video_data.get("owner") or {}).get("name")
        or "Unknown Artist"
    )
    upload_date = json_ld.get("uploadDate") or str(video_data.get("pubdate") or "")
    year = upload_date[:4] if re.match(r"\d{4}", upload_date) else ""
    description = json_ld.get("description") or video_data.get("desc") or ""

    thumbnails = json_ld.get("thumbnailUrl") or []
    if isinstance(thumbnails, str):
        thumbnails = [thumbnails]
    if video_data.get("pic"):
        thumbnails.append(video_data["pic"])
    if not thumbnails:
        raise ValueError("No cover image found in page metadata")
    cover_url = original_bili_image(thumbnails[0])

    stem = safe_filename(f"{artist} - {track_title}")
    raw_audio = args.out_dir / f"{stem}.source-{best_audio.get('id', 'audio')}.m4s"
    cover_path = args.out_dir / f"{stem}.cover.jpg"
    final_path = args.out_dir / f"{stem}.m4a"

    referer = source_url
    download(audio_url, raw_audio, referer)
    download(cover_url, cover_path, referer)
    remux_audio(raw_audio, final_path)

    cover_data = cover_path.read_bytes()
    audio = MP4(final_path)
    if audio.tags is None:
        audio.add_tags()
    audio["\xa9nam"] = [track_title]
    audio["\xa9ART"] = [artist]
    audio["aART"] = [artist]
    audio["\xa9alb"] = [args.album]
    if year:
        audio["\xa9day"] = [year]
    audio["\xa9gen"] = ["Cover"]
    audio["\xa9cmt"] = [f"{video_title}\n{source_url}"]
    if description:
        audio["desc"] = [description]
    audio["covr"] = [MP4Cover(cover_data, imageformat=image_format(cover_data))]
    audio.save()

    print(f"final={final_path}")
    print(f"source_audio={raw_audio}")
    print(f"cover={cover_path}")
    print(f"track={track_title}")
    print(f"artist={artist}")
    print(f"source_url={source_url}")
    print(f"audio_id={best_audio.get('id')} bandwidth={best_audio.get('bandwidth')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
