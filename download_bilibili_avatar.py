#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def clean_source_text(value: str) -> str:
    value = html.unescape(value.strip())
    return re.sub(r"\\([:/?&=#%._~+-])", r"\1", value)


def extract_mid(value: str) -> str:
    value = clean_source_text(value)
    if re.fullmatch(r"\d+", value):
        return value

    match = re.search(r"space\.bilibili\.com/(\d+)", value)
    if match:
        return match.group(1)

    parsed = urlparse(value)
    query_match = re.search(r"(?:^|[?&])(?:mid|vmid|host_mid)=(\d+)", parsed.query)
    if query_match:
        return query_match.group(1)

    text_match = re.search(r"\b\d{5,}\b", value)
    if text_match:
        return text_match.group(0)

    raise ValueError("Could not find a Bilibili user id in SOURCE")


def request_headers(referer: str = "https://space.bilibili.com/") -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json,text/plain,*/*",
    }


def fetch_user_card(mid: str) -> dict:
    params = urlencode({"mid": mid})
    req = urllib.request.Request(
        f"https://api.bilibili.com/x/web-interface/card?{params}",
        headers=request_headers(f"https://space.bilibili.com/{mid}/"),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Could not fetch Bilibili user card: {exc}") from exc

    if payload.get("code") != 0:
        raise RuntimeError(
            f"Bilibili user card API failed: {payload.get('message')}"
        )
    return payload["data"]["card"]


def original_bili_image(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    if "hdslb.com" in url and "@" in url:
        return url.split("@", 1)[0]
    return url


def safe_filename(value: str, fallback: str = "bilibili-avatar") -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n]+', " ", value).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.strip(". ")
    return value or fallback


def extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return suffix
    return ".jpg"


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a Bilibili user's original avatar image."
    )
    parser.add_argument("source", help="Bilibili space URL, user id, or copied text")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--output", type=Path, help="Explicit output image path")
    args = parser.parse_args()

    mid = extract_mid(args.source)
    card = fetch_user_card(mid)
    face_url = original_bili_image(card.get("face") or "")
    if not face_url:
        raise ValueError("Bilibili user card did not include an avatar URL")

    name = safe_filename(card.get("name") or mid)
    output = args.output or (
        args.out_dir / f"{name}-{mid}.avatar{extension_from_url(face_url)}"
    )
    download(face_url, output, f"https://space.bilibili.com/{mid}/")

    print(f"avatar={output}")
    print(f"mid={mid}")
    print(f"name={card.get('name') or ''}")
    print(f"face_url={face_url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
