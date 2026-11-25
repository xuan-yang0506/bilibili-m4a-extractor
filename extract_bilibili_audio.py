#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import urllib.request
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse

from mutagen.mp4 import MP4, MP4Cover


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
READY_CLOUD_STATUSES = {"uploaded", "matched", "purchased", "subscription"}
DEFAULT_TRACK_NUMBER = 1
DEFAULT_TRACK_COUNT = 1
DEFAULT_DISC_NUMBER = 1
DEFAULT_DISC_COUNT = 1
BILIBILI_COOKIE: str | None = None
CHROME_COOKIE_NAMES = {
    "CURRENT_FNVAL",
    "CURRENT_QUALITY",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "SESSDATA",
    "_uuid",
    "b_nut",
    "bili_jct",
    "buvid3",
    "buvid4",
}


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


def clean_source_text(value: str) -> str:
    value = html.unescape(value.strip())
    return re.sub(r"\\([:/?&=#%._~+-])", r"\1", value)


def extract_source(value: str) -> str:
    value = clean_source_text(value)
    match = re.search(r"https?://[^\s\"'<>，。】）)]+", value)
    if match:
        return match.group(0).rstrip(".,，。")
    return value


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
    source = extract_source(source)
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
        return extract_source(explicit_url)
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


def fetch_playinfo_api(initial_state: dict, source_url: str) -> dict:
    video_data = initial_state.get("videoData") or {}
    bvid = video_data.get("bvid")
    cid = video_data.get("cid")
    if not cid and video_data.get("pages"):
        cid = video_data["pages"][0].get("cid")
    if not bvid or not cid:
        raise ValueError("Could not find bvid/cid for Bilibili playurl API")

    params = urlencode(
        {
            "bvid": bvid,
            "cid": cid,
            "qn": "0",
            "fnval": "4048",
            "fnver": "0",
            "fourk": "1",
        }
    )
    req = urllib.request.Request(
        f"https://api.bilibili.com/x/player/playurl?{params}",
        headers={
            **request_headers(source_url),
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Could not fetch Bilibili playurl API: {exc}") from exc

    if data.get("code") != 0:
        raise RuntimeError(f"Bilibili playurl API failed: {data.get('message')}")
    return data


def load_playinfo(
    page: str,
    initial_state: dict,
    source_url: str,
    prefer_api: bool = False,
) -> dict:
    if prefer_api:
        try:
            return fetch_playinfo_api(initial_state, source_url)
        except Exception as exc:
            log(f"warning: authenticated playurl API failed, falling back to inline playinfo: {exc}")
    try:
        return load_inline_json(page, "window.__playinfo__=")
    except ValueError:
        return fetch_playinfo_api(initial_state, source_url)


def stream_url(stream: dict) -> str | None:
    urls = stream_urls(stream)
    return urls[0] if urls else None


def stream_urls(stream: dict) -> list[str]:
    urls: list[str] = []
    for key in ("baseUrl", "base_url"):
        value = stream.get(key)
        if value:
            urls.append(normalize_url(value))
    for key in ("backupUrl", "backup_url"):
        values = stream.get(key) or []
        if isinstance(values, str):
            values = [values]
        urls.extend(normalize_url(value) for value in values if value)

    unique_urls = []
    seen = set()
    for url in urls:
        if url not in seen:
            unique_urls.append(url)
            seen.add(url)
    return unique_urls


def stream_bandwidth(stream: dict) -> int:
    return int(stream.get("bandwidth") or 0)


def best_stream(streams: list[dict]) -> dict | None:
    available = [stream for stream in streams if stream_url(stream)]
    if not available:
        return None
    return max(available, key=stream_bandwidth)


def choose_best_audio_stream(playinfo: dict) -> tuple[dict, str]:
    dash = playinfo["data"]["dash"]

    flac_audio = (dash.get("flac") or {}).get("audio")
    if isinstance(flac_audio, dict) and stream_url(flac_audio):
        return flac_audio, "Hi-Res FLAC"

    dolby_audio = (dash.get("dolby") or {}).get("audio")
    if isinstance(dolby_audio, dict):
        dolby_audio = [dolby_audio]
    if isinstance(dolby_audio, list):
        selected_dolby = best_stream(dolby_audio)
        if selected_dolby:
            return selected_dolby, "Dolby audio"

    selected_audio = best_stream(dash.get("audio") or [])
    if selected_audio:
        return selected_audio, "DASH audio"
    raise ValueError("No downloadable audio stream found in Bilibili playinfo")


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


def default_album_title(track_title: str) -> str:
    return f"{track_title} - Single"


def request_headers(referer: str) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
    }
    if BILIBILI_COOKIE:
        headers["Cookie"] = BILIBILI_COOKIE
    return headers


def chrome_cookie_db(profile: str = "Default") -> Path:
    base = Path.home() / "Library/Application Support/Google/Chrome" / profile
    candidates = [base / "Cookies", base / "Network" / "Cookies"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find Chrome cookie database for profile {profile!r}")


def chrome_safe_storage_secret() -> bytes:
    commands = [
        ["security", "find-generic-password", "-w", "-a", "Chrome", "-s", "Chrome Safe Storage"],
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
    ]
    last_error = ""
    for command in commands:
        result = subprocess.run(command, capture_output=True)
        if result.returncode == 0:
            return result.stdout.rstrip(b"\n")
        last_error = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
    details = f": {last_error}" if last_error else ""
    raise RuntimeError(f"Could not read Chrome Safe Storage from Keychain{details}")


def decrypt_chrome_cookie(host_key: str, encrypted_value: bytes, secret: bytes) -> str:
    if not encrypted_value:
        return ""
    data = bytes(encrypted_value)
    if data.startswith((b"v10", b"v11")):
        data = data[3:]

    key = hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", 1003, 16)
    iv = b" " * 16
    result = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-128-cbc",
            "-K",
            key.hex(),
            "-iv",
            iv.hex(),
        ],
        input=data,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not decrypt Chrome cookie with Keychain secret")

    value = result.stdout
    host_digest = hashlib.sha256(host_key.encode("utf-8")).digest()
    if value.startswith(host_digest):
        value = value[len(host_digest) :]
    return value.decode("utf-8", errors="replace")


def load_chrome_bilibili_cookie(profile: str = "Default") -> str:
    if sys.platform != "darwin":
        raise RuntimeError("--cookies-from-browser chrome is currently implemented for macOS Chrome")

    source_db = chrome_cookie_db(profile)
    secret = chrome_safe_storage_secret()
    with tempfile.TemporaryDirectory() as tmp_dir:
        cookie_db = Path(tmp_dir) / "Cookies"
        shutil.copy2(source_db, cookie_db)
        conn = sqlite3.connect(cookie_db)
        try:
            rows = conn.execute(
                """
                select host_key, name, value, encrypted_value
                from cookies
                where host_key in ('.bilibili.com', 'www.bilibili.com')
                order by host_key, name
                """
            ).fetchall()
        finally:
            conn.close()

    cookies: dict[str, str] = {}
    for host_key, name, value, encrypted_value in rows:
        if name not in CHROME_COOKIE_NAMES:
            continue
        cookie_value = value or decrypt_chrome_cookie(host_key, encrypted_value, secret)
        if cookie_value:
            cookies[name] = cookie_value

    required = {"SESSDATA", "bili_jct", "DedeUserID"}
    missing = sorted(required - cookies.keys())
    if missing:
        raise RuntimeError(f"Chrome is missing required Bilibili cookies: {', '.join(missing)}")
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def log(message: str) -> None:
    print(f"[extract] {message}", file=sys.stderr, flush=True)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def download(url: str | list[str], dest: Path, referer: str, label: str | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    label = label or dest.name
    urls = [url] if isinstance(url, str) else url
    if not urls:
        raise ValueError(f"No download URLs available for {label}")
    last_error: str | None = None
    for attempt in range(1, 4):
        for url_index, candidate_url in enumerate(urls, start=1):
            if tmp.exists():
                tmp.unlink()
            req = urllib.request.Request(candidate_url, headers=request_headers(referer))
            try:
                with urllib.request.urlopen(req, timeout=90) as response, tmp.open("wb") as fh:
                    total_header = response.headers.get("Content-Length")
                    total = int(total_header) if total_header and total_header.isdigit() else None
                    source_label = f"{label} url {url_index}/{len(urls)}"
                    if total:
                        log(f"downloading {source_label}: {format_size(total)}")
                    else:
                        log(f"downloading {source_label}: size unknown")

                    downloaded = 0
                    last_report = time.monotonic()
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
                        downloaded += len(chunk)

                        now = time.monotonic()
                        if now - last_report >= 1:
                            if total:
                                percent = downloaded / total * 100
                                log(
                                    f"{label}: {format_size(downloaded)} / "
                                    f"{format_size(total)} ({percent:.1f}%)"
                                )
                            else:
                                log(f"{label}: {format_size(downloaded)} downloaded")
                            last_report = now

                    if total and downloaded != total:
                        last_error = (
                            f"{source_label} ended before advertised size: "
                            f"{format_size(downloaded)} of {format_size(total)}"
                        )
                        log(f"warning: {last_error}")
                        continue
                    log(f"downloaded {label}: {format_size(downloaded)}")
                    tmp.replace(dest)
                    return
            except (HTTPError, URLError, OSError) as exc:
                last_error = f"{label} url {url_index}/{len(urls)} failed: {exc}"
                log(f"warning: {last_error}")
        if attempt < 3:
            log(f"retrying {label} download, attempt {attempt + 1}/3")
    if tmp.exists():
        tmp.unlink()
    raise RuntimeError(last_error or f"Download failed for {url}")


def remux_audio(source: Path, dest: Path, codec: str | None = None) -> None:
    tmp = dest.with_name(dest.stem + ".tmp.m4a")
    if tmp.exists():
        tmp.unlink()

    afconvert = shutil.which("afconvert")
    if codec and codec.lower() == "flac":
        if not afconvert:
            raise RuntimeError("Hi-Res FLAC output requires macOS afconvert to create Apple Music compatible ALAC")
        log("converting Hi-Res FLAC to Apple Lossless ALAC for Music.app compatibility")
        subprocess.run(
            [afconvert, "-f", "m4af", "-d", "alac", str(source), str(tmp)],
            check=True,
        )
        tmp.replace(dest)
        return

    if afconvert:
        result = subprocess.run(
            [afconvert, "-f", "m4af", "-d", "0", str(source), str(tmp)],
            capture_output=True,
        )
        if result.returncode != 0:
            log("warning: afconvert could not remux this stream; copying source audio without re-encoding")
            shutil.copy2(source, tmp)
    else:
        shutil.copy2(source, tmp)
    tmp.replace(dest)


def set_m4a_single_metadata(audio_file: Path, album: str) -> None:
    audio = MP4(audio_file)
    if audio.tags is None:
        audio.add_tags()
    audio["\xa9alb"] = [album]
    audio["trkn"] = [(DEFAULT_TRACK_NUMBER, DEFAULT_TRACK_COUNT)]
    audio["disk"] = [(DEFAULT_DISC_NUMBER, DEFAULT_DISC_COUNT)]
    audio.save()


def run_osascript(script: str, *args: str) -> str:
    osascript = shutil.which("osascript")
    if not osascript:
        raise RuntimeError("Music.app automation requires macOS osascript")
    result = subprocess.run(
        [osascript, "-e", script, *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        details = stderr or stdout or f"exit status {result.returncode}"
        raise RuntimeError(f"osascript failed: {details}")
    return result.stdout.strip()


def parse_music_track_info(value: str) -> dict[str, str] | None:
    if not value:
        return None
    persistent_id, cloud_status, location = (value.split("\t", 2) + ["", ""])[:3]
    return {
        "persistent_id": persistent_id,
        "cloud_status": cloud_status,
        "location": location,
    }


def find_music_track(
    track_title: str,
    artist: str,
    album: str,
    source_key: str,
) -> dict[str, str] | None:
    script = """
on run argv
    set trackName to item 1 of argv
    set trackArtist to item 2 of argv
    set trackAlbum to item 3 of argv
    set sourceKey to item 4 of argv
    tell application "Music"
        set candidates to every track of library playlist 1 whose name is trackName and artist is trackArtist
        repeat with candidate in candidates
            if (sourceKey is not "" and comment of candidate contains sourceKey) or (sourceKey is "" and album of candidate is trackAlbum) then
                set pid to persistent ID of candidate
                set cstatus to cloud status of candidate
                set trackLocation to ""
                try
                    set trackLocation to POSIX path of (location of candidate as alias)
                end try
                return (pid as text) & tab & (cstatus as text) & tab & trackLocation
            end if
        end repeat
    end tell
    return ""
end run
"""
    return parse_music_track_info(
        run_osascript(script, track_title, artist, album, source_key)
    )


def add_file_to_music(audio_file: Path) -> dict[str, str]:
    script = """
on run argv
    set audioPath to item 1 of argv
    tell application "Music"
        set addedTracks to add POSIX file audioPath
        try
            set addedTrack to item 1 of addedTracks
        on error
            set addedTrack to addedTracks
        end try
        set pid to persistent ID of addedTrack
        set cstatus to cloud status of addedTrack
        set trackLocation to ""
        try
            set trackLocation to POSIX path of (location of addedTrack as alias)
        end try
        return (pid as text) & tab & (cstatus as text) & tab & trackLocation
    end tell
end run
"""
    track_info = parse_music_track_info(
        run_osascript(script, str(audio_file.resolve()))
    )
    if not track_info:
        raise RuntimeError("Music.app did not return the imported track")
    return track_info


def get_music_cloud_status(persistent_id: str) -> str:
    script = """
on run argv
    set pid to item 1 of argv
    tell application "Music"
        set candidate to first track of library playlist 1 whose persistent ID is pid
        return (cloud status of candidate as text)
    end tell
end run
"""
    return run_osascript(script, persistent_id)


def try_set_music_single_metadata(persistent_id: str, album: str) -> None:
    script = """
on run argv
    set pid to item 1 of argv
    set trackAlbum to item 2 of argv
    tell application "Music"
        set candidate to first track of library playlist 1 whose persistent ID is pid
        set album of candidate to trackAlbum
        set track number of candidate to 1
        set track count of candidate to 1
        set disc number of candidate to 1
        set disc count of candidate to 1
    end tell
end run
"""
    run_osascript(script, persistent_id, album)


def set_music_single_metadata(
    persistent_id: str,
    album: str,
    retries: int = 6,
) -> str | None:
    last_error = None
    for _ in range(retries):
        try:
            try_set_music_single_metadata(persistent_id, album)
            return None
        except RuntimeError as exc:
            last_error = exc
            time.sleep(1)
    return f"Could not set Music album/track numbers: {last_error}"


def wait_for_music_cloud(persistent_id: str, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    last_status = get_music_cloud_status(persistent_id)
    while last_status not in READY_CLOUD_STATUSES and time.monotonic() < deadline:
        time.sleep(5)
        last_status = get_music_cloud_status(persistent_id)
    return last_status


def import_to_music(
    audio_file: Path,
    track_title: str,
    artist: str,
    album: str,
    source_key: str,
) -> tuple[str, dict[str, str]]:
    existing = find_music_track(track_title, artist, album, source_key)
    if existing:
        return "exists", existing
    return "imported", add_file_to_music(audio_file)


def image_format(data: bytes) -> int:
    if data.startswith(b"\xff\xd8\xff"):
        return MP4Cover.FORMAT_JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return MP4Cover.FORMAT_PNG
    raise ValueError("Cover image is neither JPEG nor PNG")


def find_existing_source_audio(out_dir: Path, stem: str) -> Path | None:
    prefix = f"{stem}.source-"
    for candidate in sorted(out_dir.glob("*.m4s")):
        if candidate.name.startswith(prefix):
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract the best Bilibili DASH audio stream to an Apple Music friendly .m4a."
    )
    parser.add_argument("source", help="Bilibili/b23 URL or a saved Bilibili page dump")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--album", help="Album title (default: '<track> - Single')")
    parser.add_argument(
        "--source-url",
        help="Original Bilibili URL to write into metadata when source is a local dump",
    )
    parser.add_argument(
        "--cookies-from-browser",
        choices=["chrome"],
        default="chrome",
        help="Read Bilibili login cookies from a supported browser",
    )
    parser.add_argument(
        "--no-cookies-from-browser",
        action="store_true",
        help="Do not read login cookies from Chrome",
    )
    parser.add_argument(
        "--import-to-music",
        action="store_true",
        help="Import the final .m4a into macOS Music.app after it exists",
    )
    parser.add_argument(
        "--wait-for-cloud",
        action="store_true",
        help="After importing, wait for Music.app to report a cloud-ready status",
    )
    parser.add_argument(
        "--cloud-timeout",
        type=int,
        default=300,
        help="Seconds to wait with --wait-for-cloud (default: 300)",
    )
    args = parser.parse_args()
    if args.wait_for_cloud and not args.import_to_music:
        raise ValueError("--wait-for-cloud requires --import-to-music")
    if args.no_cookies_from_browser:
        args.cookies_from_browser = None
    if args.cookies_from_browser == "chrome":
        global BILIBILI_COOKIE
        log("loading Bilibili cookies from Chrome")
        BILIBILI_COOKIE = load_chrome_bilibili_cookie()

    log("loading Bilibili page")
    page, fetched_url = load_page(args.source)
    log("parsing page metadata")
    initial_state = load_inline_json(page, "window.__INITIAL_STATE__=")
    json_ld = load_json_ld(page)
    source_url = infer_source_url(args.source_url, fetched_url, initial_state, json_ld)

    video_data = initial_state.get("videoData") or {}
    video_key = video_data.get("bvid") or source_url
    video_title = (
        json_ld.get("name")
        or video_data.get("title")
        or "Bilibili audio"
    )
    track_title = choose_track_title(video_title)
    album = args.album or default_album_title(track_title)
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
    cover_path = args.out_dir / f"{stem}.cover.jpg"
    final_path = args.out_dir / f"{stem}.m4a"
    raw_audio = find_existing_source_audio(args.out_dir, stem)
    audio_id = None
    bandwidth = None
    audio_kind = None

    if final_path.exists():
        log(f"final file already exists: {final_path}")
        status = "exists"
    else:
        log("loading play information")
        playinfo = load_playinfo(page, initial_state, source_url, prefer_api=bool(BILIBILI_COOKIE))
        best_audio, audio_kind = choose_best_audio_stream(playinfo)
        audio_url = stream_url(best_audio)
        if not audio_url:
            raise ValueError("Selected audio stream did not include a URL")

        audio_id = best_audio.get("id")
        bandwidth = best_audio.get("bandwidth")
        raw_audio = args.out_dir / f"{stem}.source-{audio_id or 'audio'}.m4s"

        referer = source_url
        log(
            "selected audio stream: "
            f"kind={audio_kind} id={audio_id or 'unknown'} "
            f"bandwidth={bandwidth or 'unknown'} codec={best_audio.get('codecs') or 'unknown'}"
        )
        download(stream_urls(best_audio), raw_audio, referer, f"{audio_kind} stream")
        download(cover_url, cover_path, referer, "cover image")
        log("remuxing audio to m4a")
        remux_audio(raw_audio, final_path, best_audio.get("codecs"))

        log("writing m4a metadata and cover art")
        cover_data = cover_path.read_bytes()
        audio = MP4(final_path)
        if audio.tags is None:
            audio.add_tags()
        audio["\xa9nam"] = [track_title]
        audio["\xa9ART"] = [artist]
        audio["aART"] = [artist]
        audio["\xa9alb"] = [album]
        if year:
            audio["\xa9day"] = [year]
        audio["\xa9gen"] = ["Cover"]
        audio["trkn"] = [(DEFAULT_TRACK_NUMBER, DEFAULT_TRACK_COUNT)]
        audio["disk"] = [(DEFAULT_DISC_NUMBER, DEFAULT_DISC_COUNT)]
        audio["\xa9cmt"] = [f"{video_title}\n{source_url}"]
        if description:
            audio["desc"] = [description]
        audio["covr"] = [MP4Cover(cover_data, imageformat=image_format(cover_data))]
        audio.save()
        status = "generated"

    log("normalizing single-track metadata")
    set_m4a_single_metadata(final_path, album)

    if args.import_to_music:
        log("importing into Music.app")
        import_status, music_track = import_to_music(
            final_path,
            track_title,
            artist,
            album,
            video_key,
        )
        log("normalizing Music.app metadata")
        music_metadata_warning = set_music_single_metadata(
            music_track["persistent_id"],
            album,
        )
        print(f"music_imported={final_path}")
        print(f"music_import_status={import_status}")
        print(f"music_track_id={music_track['persistent_id']}")
        print(f"music_album={album}")
        print("music_track_number=1/1")
        print("music_disc_number=1/1")
        if music_metadata_warning:
            print(f"warning={music_metadata_warning}", file=sys.stderr)
        print(f"music_cloud_status={music_track['cloud_status']}")
        if music_track["location"]:
            print(f"music_location={music_track['location']}")
        if args.wait_for_cloud:
            log(f"waiting for Music.app cloud status for up to {args.cloud_timeout} seconds")
            cloud_status = wait_for_music_cloud(
                music_track["persistent_id"],
                args.cloud_timeout,
            )
            print(f"music_cloud_status_after_wait={cloud_status}")

    print(f"final={final_path}")
    print(f"status={status}")
    if raw_audio:
        print(f"source_audio={raw_audio}")
    if cover_path.exists():
        print(f"cover={cover_path}")
    print(f"track={track_title}")
    print(f"album={album}")
    print(f"artist={artist}")
    print(f"source_url={source_url}")
    if audio_kind:
        print(f"audio_kind={audio_kind}")
    if audio_id or bandwidth:
        print(f"audio_id={audio_id} bandwidth={bandwidth}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
