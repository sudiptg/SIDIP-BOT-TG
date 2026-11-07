"""Core engine: rename parser, URL helpers, aria2 download, ffprobe / ffmpeg, progress panels.

Every external command is started with an argument list (never through a shell), so file
names, URLs or metadata values can not inject shell commands.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from pyrogram.errors import FloodWait, MessageNotModified

import config
from tasks import Task, TaskCancelled, UserError
from utils import bar, btn, esc, format_bytes, format_time, kb, shorten

log = logging.getLogger("tools")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
KNOWN_EXTS = config.VIDEO_EXTS | config.AUDIO_EXTS | {".srt", ".ass", ".zip", ".rar", ".7z", ".pdf", ".apk"}

# =========================================================================== #
# 1. Rename engine
# =========================================================================== #
_SEASON_EP = re.compile(r"(?i)\bS(\d{1,2})[ ._-]?E(\d{1,4})\b")
_SEASON = re.compile(r"(?i)\b(?:S|Season)[ ._-]?(\d{1,2})\b")
_EPISODE = re.compile(r"(?i)(?:\bE(?:P|pisode)?[ ._-]?(\d{1,4})\b)|(?:\s-\s(\d{1,3})(?=[\s.\[(]|$))")
_QUALITY = re.compile(r"(?i)\b(\d{3,4}p|[248]k)\b")
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_CODEC = re.compile(r"(?i)\b(x264|x265|h\.?264|h\.?265|hevc|av1|xvid)\b")
_JUNK = re.compile(
    r"(?i)\b(web[ .-]?dl|web[ .-]?rip|blu[ .-]?ray|brrip|bdrip|hdrip|hdtv|dvdrip|hdcam|10bit|8bit|"
    r"aac(?:2\.0)?|ac3|eac3|ddp?[0-9.]*|dts|atmos|dual[ .-]?audio|e?subs?)\b"
)
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_VARIABLE = re.compile(r"\{(title|season|episode|quality|year|codec|original)\}", re.I)


def split_name(name: str):
    """Split ``name`` into (stem, extension) - only real file extensions are treated as such."""
    stem, ext = os.path.splitext(name)
    if ext.lower() in KNOWN_EXTS:
        return stem, ext
    return name, ""


def parse_media_info(original: str, remove_words=()) -> dict:
    stem, _ = split_name(original)
    work = stem
    for word in remove_words:
        if word.strip():
            work = re.sub(re.escape(word.strip()), " ", work, flags=re.I)

    season = episode = ""
    match = _SEASON_EP.search(work)
    if match:
        season, episode = match.group(1), match.group(2)
        work = work.replace(match.group(0), " ", 1)
    else:
        match = _SEASON.search(work)
        if match:
            season = match.group(1)
            work = work.replace(match.group(0), " ", 1)
        match = _EPISODE.search(work)
        if match:
            episode = match.group(1) or match.group(2)
            work = work.replace(match.group(0), " ", 1)

    def first(pattern) -> str:
        found = pattern.search(stem)
        return found.group(1).lower() if found else ""

    quality, year, codec = first(_QUALITY), first(_YEAR), first(_CODEC)

    title = work
    for pattern in (_QUALITY, _CODEC, _JUNK, _BRACKETS):
        title = pattern.sub(" ", title)
    title = title.replace(".", " ").replace("_", " ")
    title = re.sub(r"\s+-\s+|^-+|-+$", " ", title)

    def tidy(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    title = tidy(_YEAR.sub(" ", title)) or tidy(title)  # keep year if the title IS a number (e.g. "1917")
    return {
        "title": title,
        "season": season.zfill(2) if season else "",
        "episode": episode.zfill(2) if episode else "",
        "quality": quality,
        "year": year,
        "codec": codec,
        "original": stem,
    }


def has_variables(fmt: str) -> bool:
    return bool(_VARIABLE.search(fmt))


def apply_format(fmt: str, info: dict) -> str:
    template = fmt
    if not info["season"] and not info["episode"]:
        template = re.sub(r"(?i)S?\{season\}[\s._-]*E?\{episode\}", "", template)
    if not info["season"]:
        template = re.sub(r"(?i)\bS\{season\}", "", template)
    if not info["episode"]:
        template = re.sub(r"(?i)\bEP?\{episode\}", "", template)
    result = _VARIABLE.sub(lambda m: info.get(m.group(1).lower(), ""), template)
    result = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", result)
    result = re.sub(r"\s{2,}", " ", result)
    return re.sub(r"[\s\-_.]+$", "", result).strip()


def sanitize_filename(name: str, max_bytes: int = 200) -> str:
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    stem, ext = os.path.splitext(name)
    while len((stem + ext).encode("utf-8")) > max_bytes and stem:
        stem = stem[:-1]
    return (stem + ext) or "file"


def build_new_name(original: str, fmt: str, remove_words=(), custom_name: str = None) -> str:
    """Return the final file name (with extension) for ``original``."""
    _, ext = split_name(original)
    ext = ext or ".mp4"
    if custom_name:
        stem = custom_name
    elif has_variables(fmt):
        stem = apply_format(fmt, parse_media_info(original, remove_words))
    else:
        stem = fmt
    if stem.lower().endswith(ext.lower()):
        stem = stem[: -len(ext)]
    return sanitize_filename(stem.strip() or "file") + ext


def build_caption(template: str, filename: str, size=0, duration=0, width=0, height=0) -> str:
    values = {
        "filename": esc(filename),
        "size": format_bytes(size) if size else "N/A",
        "duration": format_time(duration) if duration else "N/A",
        "resolution": f"{width}x{height}" if width and height else "N/A",
    }
    template = template or config.DEFAULT_CAPTION
    return re.sub(r"\{(filename|size|duration|resolution)\}", lambda m: values[m.group(1)], template)


# =========================================================================== #
# 2. Status panel / progress
# =========================================================================== #
_ICONS = {"queued": "⏳", "downloading": "📥", "processing": "⚙️", "uploading": "📤"}


def cancel_markup(task: Task):
    return kb([[btn("❌ Cancel Task", f"t:cancel:{task.task_id}", "danger")]])


def queued_text(task: Task, position: int) -> str:
    return (
        "⏳ <b>Added to queue</b>\n"
        f"<blockquote>📄 <code>{esc(shorten(task.name, 60))}</code>\n"
        f"🆔 <b>Task:</b> <code>#{task.task_id}</code>   📍 <b>Position:</b> {position}</blockquote>"
    )


def progress_text(task: Task, action: str, current=0, total=0, speed=0.0, note: str = "") -> str:
    lines = [f"{_ICONS.get(task.state, '⏳')} <b>{action}</b>", f"<code>{esc(shorten(task.name, 60))}</code>", ""]
    body = []
    if total > 0:
        percent = min(100.0, current * 100.0 / total)
        lines.append(f"<code>[{bar(percent)}]</code>  <b>{percent:.1f}%</b>\n")
        body.append(f"💾 <b>Size:</b> {format_bytes(current)} / {format_bytes(total)}")
    elif current > 0:
        body.append(f"💾 <b>Done:</b> {format_bytes(current)}")
    if speed > 0:
        body.append(f"⚡ <b>Speed:</b> {format_bytes(speed)}/s")
        if total > current:
            body.append(f"⏱ <b>ETA:</b> {format_time((total - current) / speed)}")
    body.append(f"🕒 <b>Elapsed:</b> {format_time(time.time() - (task.started or task.created))}")
    body.append(f"🆔 <b>Task:</b> <code>#{task.task_id}</code>")
    if note:
        body.append(note)
    lines.append("<blockquote>" + "\n".join(body) + "</blockquote>")
    return "\n".join(lines)


async def edit_status(task: Task, text: str, force: bool = False, markup="default") -> None:
    now = time.time()
    if not force and now - task.last_edit < config.EDIT_INTERVAL:
        return
    task.last_edit = now
    try:
        await task.status_msg.edit_text(text, reply_markup=cancel_markup(task) if markup == "default" else markup)
    except MessageNotModified:
        pass
    except FloodWait as exc:
        task.last_edit = now + float(exc.value)
    except Exception as exc:  # editing is cosmetic - never break the task for it
        log.debug("status edit failed: %s", exc)


async def set_state(task: Task, state: str, action: str) -> None:
    task.state = state
    await edit_status(task, progress_text(task, action), force=True)


async def telegram_progress(current, total, task: Task, action: str, started: float) -> None:
    """Pyrogram progress callback (download + upload)."""
    if task.cancelled:
        task.client.stop_transmission()  # raises StopTransmission inside Pyrogram
    if time.time() - task.last_edit < config.EDIT_INTERVAL:
        return
    elapsed = time.time() - started
    speed = current / elapsed if elapsed > 0 else 0
    await edit_status(task, progress_text(task, action, current, total, speed), force=True)


# =========================================================================== #
# 3. URL helpers + aria2 download
# =========================================================================== #
def is_url(text: str) -> bool:
    return bool(re.match(r"(?i)^(https?|ftp)://\S+$", text.strip()))


def _name_from_headers(headers) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", disposition)
    if match:
        return unquote(match.group(1)).strip('"')
    match = re.search(r'filename="?([^";]+)"?', disposition)
    return unquote(match.group(1)).strip() if match else ""


async def get_url_info(url: str):
    """Return ``(file_name, size_in_bytes)``; either value may be empty / 0."""
    name, size, final_url = "", 0, url
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        parsed = urlparse(url)
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout) as session:
            if "pixeldrain" in parsed.netloc.lower():
                file_id = parsed.path.rstrip("/").split("/")[-1]
                async with session.get(f"https://pixeldrain.com/api/file/{file_id}/info") as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return data.get("name") or "", int(data.get("size") or 0)
                return "", 0
            try:
                async with session.head(url, allow_redirects=True) as resp:
                    name = _name_from_headers(resp.headers)
                    size = int(resp.headers.get("Content-Length") or 0)
                    final_url = str(resp.url)
            except aiohttp.ClientError:
                pass
            if not size:  # some servers reject HEAD - ask for a single byte instead
                async with session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as resp:
                    total = resp.headers.get("Content-Range", "").rpartition("/")[2]
                    if total.isdigit():
                        size = int(total)
                    elif resp.status == 200:
                        size = int(resp.headers.get("Content-Length") or 0)
                    name = name or _name_from_headers(resp.headers)
                    final_url = str(resp.url)
    except Exception as exc:
        log.debug("get_url_info failed: %s", exc)
    if not name:
        name = unquote(urlparse(final_url).path.rsplit("/", 1)[-1])
    return name, size


_ARIA_LINE = re.compile(
    r"\[#\w+\s+([\d.]+)([KMGT]?i?B)/([\d.]+)([KMGT]?i?B)(?:\(\d+%\))?.*?DL:([\d.]+)([KMGT]?i?B)"
)
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def _to_bytes(value: str, unit: str) -> float:
    return float(value) * _UNITS.get(unit, 1)


def parse_aria_line(line: str):
    """Return ``(downloaded, total, speed)`` in bytes from an aria2 summary line, or None."""
    match = _ARIA_LINE.search(line)
    if not match:
        return None
    done, done_u, total, total_u, speed, speed_u = match.groups()
    return _to_bytes(done, done_u), _to_bytes(total, total_u), _to_bytes(speed, speed_u)


async def download_from_url(task: Task, url: str, dest: Path, size_hint: int = 0) -> Path:
    parsed = urlparse(url)
    if "pixeldrain" in parsed.netloc.lower():
        url = f"https://pixeldrain.com/api/file/{parsed.path.rstrip('/').split('/')[-1]}"
    conns = str(config.ARIA2_CONNECTIONS)
    cmd = [
        "aria2c", f"--dir={dest.parent}", f"--out={dest.name}", "--summary-interval=1",
        "--console-log-level=warn", "--download-result=hide", "--allow-overwrite=true",
        "--auto-file-renaming=false", "--file-allocation=none", f"--max-connection-per-server={conns}",
        f"--split={conns}", "--min-split-size=1M", "--max-tries=3", "--retry-wait=3",
        "--connect-timeout=20", "--timeout=60", f"--user-agent={USER_AGENT}", url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError:
        raise UserError("aria2c is not installed on this server.")
    task.proc = proc
    started = time.time()
    tail = deque(maxlen=5)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
            except asyncio.TimeoutError:
                task.check()
                continue
            if not raw:
                break
            task.check()
            line = raw.decode("utf-8", errors="ignore").strip()
            stats = parse_aria_line(line)
            if stats:
                done, total, speed = stats
                await edit_status(task, progress_text(task, "Downloading", done, total or size_hint, speed))
            elif line and not line.startswith(("*", "=", "-", "FILE:", "Download Results")):
                tail.append(line)
        await proc.wait()
    finally:
        task.proc = None
        if proc.returncode is None:
            proc.kill()
    task.check()
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        reason = " | ".join(tail)[-300:] or f"aria2 exit code {proc.returncode}"
        raise UserError(f"Download failed - the link may be expired or blocked.\n{reason}")
    log.info("Downloaded %s in %s", dest.name, format_time(time.time() - started))
    return dest


# =========================================================================== #
# 4. ffprobe / ffmpeg
# =========================================================================== #
async def run_cmd(*args, task: Task = None):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if task:
        task.proc = proc
    try:
        out, err = await proc.communicate()
    finally:
        if task:
            task.proc = None
    return proc.returncode, out, err


async def probe(path: Path):
    """Return media info dict or ``None`` when ffprobe can not read the file."""
    try:
        code, out, _ = await run_cmd(
            "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path
        )
    except FileNotFoundError:
        return None
    if code != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    streams, width, height, has_video = [], 0, 0, False
    for s in data.get("streams", []):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        kind = s.get("codec_type")
        streams.append({"index": s.get("index"), "type": kind, "lang": (tags.get("language") or "").lower()})
        if kind == "video" and not has_video and not (s.get("disposition") or {}).get("attached_pic"):
            has_video, width, height = True, int(s.get("width") or 0), int(s.get("height") or 0)
    try:
        duration = int(float(data.get("format", {}).get("duration") or 0))
    except ValueError:
        duration = 0
    return {"duration": duration, "width": width, "height": height, "has_video": has_video, "streams": streams}


def _clean(value) -> str:
    return str(value).replace("\n", " ").strip() if value else ""


_GLOBAL_TAGS = (
    ("desc", "description"), ("artist", "artist"), ("album", "album"), ("author", "author"),
    ("official", "OFFICIAL_SITE"), ("copyright", "copyright"), ("publisher", "publisher"),
    ("genre", "genre"), ("encodedby", "encoded_by"), ("comment", "comment"),
)
# MP4 can only store these natively; the others need the extended "mdta" tag format.
_MP4_EXTENDED_ONLY = ("author", "official", "publisher", "encodedby")


def resolve_metadata(meta: dict, fallback_title: str) -> dict:
    """Merge per-field values with 'Change All'. Only non-empty values are returned."""
    change_all = _clean(meta.get("all"))
    values = {key: _clean(meta.get(key)) or change_all for key, _, _ in config.METADATA_FIELDS}
    values["title"] = _clean(meta.get("title")) or change_all or fallback_title
    return {key: value for key, value in values.items() if value}


def build_metadata_args(values: dict, is_mp4: bool = False) -> list:
    args = ["-metadata", f"title={values['title']}"]
    for key, tag in _GLOBAL_TAGS:
        if key in values:
            args += ["-metadata", f"{tag}={values[key]}"]
    if "year" in values:
        args += ["-metadata", f"date={values['year']}", "-metadata", f"year={values['year']}"]

    def track_title(selector: str, text: str) -> None:
        args.extend([f"-metadata:s:{selector}", f"title={text}"])
        if is_mp4:  # MP4 stores track names in handler_name
            args.extend([f"-metadata:s:{selector}", f"handler_name={text}"])

    track_title("v:0", values["title"])
    if "audiotitle" in values:
        track_title("a", values["audiotitle"])
    if "subtitle" in values:
        track_title("s", values["subtitle"])
    if "encoder" in values:
        # The muxer always overwrites the global 'encoder' tag, so it is written per stream as well.
        args += ["-metadata", f"encoder={values['encoder']}", "-metadata:s", f"encoder={values['encoder']}"]
    if is_mp4 and any(key in values for key in _MP4_EXTENDED_ONLY):
        args += ["-movflags", "+use_metadata_tags"]
    return args


_BULK_ALIASES = {"all": "all", "changeall": "all", "description": "desc", "site": "official",
                 "website": "official", "audio": "audiotitle", "sub": "subtitle", "subtitletitle": "subtitle"}


def parse_bulk_metadata(text: str):
    """Parse ``key: value`` lines. Returns ``(values, unknown_keys)``; a value of None means 'clear'."""
    aliases = dict(_BULK_ALIASES)
    for key, label, _ in config.METADATA_FIELDS:
        aliases[key] = key
        aliases[re.sub(r"[^a-z]", "", label.lower())] = key
    values, unknown = {}, []
    for line in text.splitlines():
        match = re.match(r"^\s*([^:=]+?)\s*[:=]\s*(.*?)\s*$", line)
        if not match:
            continue
        raw_key, value = match.groups()
        field = aliases.get(re.sub(r"[^a-z]", "", raw_key.lower()))
        if not field:
            unknown.append(raw_key.strip())
        elif value:
            values[field] = None if value == "-" else value[:200]
    return values, unknown


async def process_media(task: Task, src: Path, dst: Path, info: dict, settings: dict, meta: dict) -> bool:
    """Remux ``src`` -> ``dst`` (stream copy) applying audio/subtitle filters and metadata.

    Several strategies are tried before giving up, so metadata is applied reliably:
      1. everything (subtitles + attachments)  2. without subtitles  3. MP4 only: audio -> AAC.
    Returns False when ffmpeg could not handle the file (caller then keeps the original bytes).
    """
    streams = info["streams"]
    audio = [s for s in streams if s["type"] == "audio"]
    chosen = audio
    codes = config.AUDIO_LANGUAGES.get(settings["audio"], ("", ()))[1]
    if codes:
        matched = [s for s in audio if s["lang"] in codes]
        chosen = matched or audio  # never drop every audio track when the language is missing
    wants_subs = bool(settings["sub"]) and any(s["type"] == "subtitle" for s in streams)
    is_mp4 = dst.suffix.lower() in config.MP4_FAMILY
    metadata = []
    if settings["meta_enabled"]:
        metadata = build_metadata_args(resolve_metadata(meta, dst.stem), is_mp4)

    plans = [(True, False)] if wants_subs else []     # (with_subs, audio_to_aac)
    plans.append((False, False))
    if is_mp4 and audio:
        plans.append((False, True))

    for with_subs, to_aac in plans:
        cmd = ["ffmpeg", "-nostdin", "-y", "-i", src, "-map", "0:v?"]
        for stream in chosen:
            cmd += ["-map", f"0:{stream['index']}"]
        if with_subs:
            cmd += ["-map", "0:s?"]
        if dst.suffix.lower() == ".mkv" and with_subs:
            cmd += ["-map", "0:t?"]  # keep attached fonts (ASS subtitles)
        cmd += ["-dn"] if not with_subs else []
        cmd += metadata + ["-c", "copy"]
        if to_aac:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        if with_subs and is_mp4:
            cmd += ["-c:s", "mov_text"]
        cmd.append(dst)
        code, _, err = await run_cmd(*cmd, task=task)
        task.check()
        if code == 0 and dst.exists() and dst.stat().st_size > 0:
            if to_aac:
                task.notes.append("Audio was converted to AAC because the original codec can not be stored in MP4.")
            return True
        log.warning("ffmpeg failed (subs=%s, aac=%s): %s", with_subs, to_aac, err.decode(errors="ignore")[-300:])
        dst.unlink(missing_ok=True)
    return False


async def make_thumbnail(src: Path, dst: Path, at: int = None):
    """Create a Telegram-friendly JPEG thumbnail (max 320 px) from a video or an image."""
    cmd = ["ffmpeg", "-nostdin", "-y"]
    if at is not None:
        cmd += ["-ss", str(at)]
    cmd += ["-i", src, "-vframes", "1", "-vf", "scale='min(320,iw)':-2", "-q:v", "5", dst]
    try:
        code, _, _ = await run_cmd(*cmd)
    except FileNotFoundError:
        return None
    return dst if code == 0 and dst.exists() and dst.stat().st_size > 0 else None


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
