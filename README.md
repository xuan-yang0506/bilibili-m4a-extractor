# Bilibili M4A Extractor

Extract the highest available inline Bilibili DASH audio stream from a video URL,
remux it into an Apple Music friendly `.m4a`, and embed the video cover as album
art.

The intended workflow is: pass a Bilibili or `b23.tv` link, get a tagged `.m4a`
ready to import into Apple Music.

## Contents

- `extract_bilibili_audio.py` - downloader/extractor script.
- `download_bilibili_avatar.py` - user avatar downloader.
- `Makefile` - convenience targets for extracting and importing.
- `requirements.txt` - Python dependencies.
- `.gitignore` - keeps generated audio, cover images, source streams, dumps, and virtualenv files out of Git.

Generated files are written to `outputs/` by default. That directory is ignored
because extracted audio, cover images, and source streams can be copyrighted
material and should not be committed to this repository.

## Usage

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
make extract SOURCE="https://b23.tv/okSKQj7"
```

The input can be a plain Bilibili URL, a `b23.tv` short link, or copied share
text that contains a URL. Shell-escaped URL punctuation like `\?`, `\&`, and
`\=` is normalized automatically.

This creates:

- `outputs/<artist> - <track>.m4a`
- `outputs/<artist> - <track>.cover.jpg`
- `outputs/<artist> - <track>.source-<audio-id>.m4s`

By default, each song is tagged as its own single: `<track> - Single`. For
example, `月光` becomes the album `月光 - Single`. The generated `.m4a` is tagged
as track `1 of 1` and disc `1 of 1`.

Use `--album "Album Name"` only when you intentionally want multiple tracks in
the same album.

To import the final `.m4a` into macOS Music.app after it exists:

```sh
make import SOURCE="https://b23.tv/okSKQj7"
```

If `outputs/<artist> - <track>.m4a` already exists, the script skips downloading
and remuxing, then imports that existing file directly. Music.app handles any
Apple Music library sync/upload after import, if sync is enabled for your account.
The import is idempotent: if a matching Music track already exists, the script
prints its existing cloud status instead of adding a duplicate.
Music.app track and disc fields are also normalized to `1 of 1`.
If the track was previously imported under an older album name, the import step
updates it to the current single album instead of adding a duplicate.

To download the original avatar image for a Bilibili user:

```sh
make avatar SOURCE="https://space.bilibili.com/3493271057730096/dynamic"
```

This calls Bilibili's public user card API, reads the `face` field, removes any
image resizing suffix, and writes the avatar to `outputs/`.

To wait for Music.app to report that the track is cloud-ready:

```sh
.venv/bin/python extract_bilibili_audio.py "https://b23.tv/okSKQj7" --import-to-music --wait-for-cloud
```

This polls Music's `cloud status` until it is `uploaded`, `matched`,
`purchased`, or `subscription`, or until `--cloud-timeout` seconds elapse.

The script also accepts a saved Bilibili page dump containing `window.__playinfo__`
and `window.__INITIAL_STATE__`:

```sh
.venv/bin/python extract_bilibili_audio.py path/to/bilibili-page.dump \
  --source-url "https://www.bilibili.com/video/BV169dFBrEDt/"
```

On macOS, `afconvert` is used to remux the source AAC stream without re-encoding.
If `afconvert` is unavailable, the script falls back to copying the source stream
before writing tags.

## Example Audio

- Codec: AAC-LC (`mp4a.40.2`)
- Sample rate: 48 kHz
- Channels: stereo
- Bitrate: about 190 kbps
- Duration: 245.89 seconds
- Container: Apple/iTunes `.m4a`
