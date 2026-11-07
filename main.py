"""Auto-Rename Bot - entrypoint.

Handlers, the processing pipeline (download -> rename/remux -> upload) and the
startup / shutdown logic live here. Panels are in ``ui.py``, the engine is in ``tools.py``.
"""
import asyncio

# The event loop must exist before the Pyrogram client is created.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import functools
import logging
import os
import re
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import BotCommand

import config
import tools
import ui
from database import db
from tasks import TASKS, Task, TaskCancelled, UserError, limiter, queue_position
from utils import COLOR_MODE, btn, esc, kb
from webserver import keep_alive_loop, start_web_server

config.validate()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

app = Client(
    "AutoRenameBot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    in_memory=True,  # no session file -> works on read-only / ephemeral disks
    max_concurrent_transmissions=config.MAX_TRANSMISSIONS,
    parse_mode=ParseMode.HTML,
    sleep_threshold=30,
)

INPUT_TTL = 300
input_state: dict = {}   # uid -> {action, chat_id, msg_id, expires}
BACKGROUND: set = set()  # keeps references to running tasks
_seen: OrderedDict = OrderedDict()


def spawn(coro) -> None:
    """Run a long job in the background so Pyrogram's dispatcher is never blocked."""
    task = asyncio.get_running_loop().create_task(coro)
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)


# --------------------------------------------------------------------------- #
# Access-control decorators
# --------------------------------------------------------------------------- #
def guarded(func):
    @functools.wraps(func)
    async def wrapper(client, message):
        user = message.from_user
        if not user:
            return
        if not db.is_authorized(user.id):
            if message.command and message.command[0] == "start":
                await message.reply("🔒 <b>This is a private bot.</b>\nAsk the owner to authorize you.")
            return
        key = (message.chat.id, message.id)
        if key in _seen:
            return
        _seen[key] = True
        if len(_seen) > 2000:
            _seen.popitem(last=False)
        return await func(client, message)
    return wrapper


def owner_only(func):
    @functools.wraps(func)
    async def wrapper(client, message):
        if not db.is_owner(message.from_user.id):
            return
        return await func(client, message)
    return guarded(wrapper)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@app.on_message(filters.command("start") & filters.private)
@guarded
async def start_cmd(client, message):
    text, markup = ui.start_panel(db.is_owner(message.from_user.id))
    try:
        await message.reply_photo(photo=config.WELCOME_PIC, caption=text, reply_markup=markup)
    except Exception:
        await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("help") & filters.private)
@guarded
async def help_cmd(client, message):
    text, markup = ui.help_panel()
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("settings") & filters.private)
@guarded
async def settings_cmd(client, message):
    text, markup = ui.settings_main(message.from_user.id)
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("metadata") & filters.private)
@guarded
async def metadata_cmd(client, message):
    text, markup = ui.metadata_panel(message.from_user.id)
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("autorename") & filters.private)
@guarded
async def autorename_cmd(client, message):
    uid = message.from_user.id
    parts = message.text.split(None, 1)
    prefix = ""
    if len(parts) > 1:
        db.user(uid)["rename_format"] = parts[1].strip()[:200]
        db.touch()
        prefix = "✅ <b>Rename format saved!</b>\n\n"
    text, markup = ui.rename_panel(uid)
    await message.reply(prefix + text, reply_markup=markup)


@app.on_message(filters.command("queue") & filters.private)
@guarded
async def queue_cmd(client, message):
    uid = message.from_user.id
    text, markup = ui.queue_panel(uid, db.is_owner(uid))
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("cancel") & filters.private)
@guarded
async def cancel_cmd(client, message):
    uid = message.from_user.id
    if input_state.pop(uid, None):
        return await message.reply("✅ <b>Input cancelled.</b>")
    arg = message.command[1].lstrip("#") if len(message.command) > 1 else ""
    mine = [t for t in TASKS.values() if t.user_id == uid or db.is_owner(uid)]
    if arg == "all":
        for task in mine:
            task.cancel()
        await limiter.wake()
        return await message.reply(f"🛑 <b>Cancelling {len(mine)} task(s)...</b>")
    if arg.isdigit():
        task = TASKS.get(arg)
        if task and task in mine:
            task.cancel()
            await limiter.wake()
            return await message.reply(f"🛑 <b>Cancelling task</b> <code>#{arg}</code>...")
        return await message.reply("⚠️ <b>Task not found.</b>")
    text, markup = ui.queue_panel(uid, db.is_owner(uid))
    await message.reply("Usage: <code>/cancel [id|all]</code>\n\n" + text, reply_markup=markup)


@app.on_message(filters.command("thumb") & filters.private)
@guarded
async def thumb_cmd(client, message):
    file_id = db.user(message.from_user.id)["thumb_file_id"]
    if not file_id:
        return await message.reply("🖼 <b>No custom thumbnail yet.</b>\nSend me any photo to set one.")
    markup = kb([[btn("🖼 Thumbnail Settings", "s:thumb", "primary"), btn("🗑 Delete", "s:thumb:del", "danger")]])
    await message.reply_photo(file_id, caption="🖼 <b>Your custom thumbnail</b>", reply_markup=markup)


@app.on_message(filters.command("ping") & filters.private)
@guarded
async def ping_cmd(client, message):
    started = time.time()
    reply = await message.reply("🏓 <b>Pinging...</b>")
    await reply.edit_text(f"🏓 <b>Pong!</b> <code>{(time.time() - started) * 1000:.0f} ms</code>")


@app.on_message(filters.command("stats") & filters.private)
@owner_only
async def stats_cmd(client, message):
    text, markup = ui.stats_panel()
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("admin") & filters.private)
@owner_only
async def admin_cmd(client, message):
    text, markup = ui.admin_panel()
    await message.reply(text, reply_markup=markup)


@app.on_message(filters.command("auth") & filters.private)
@owner_only
async def auth_cmd(client, message):
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        users = ", ".join(f"<code>{u}</code>" for u in db.auth_list()) or "none"
        return await message.reply(f"🔐 <b>Authorized users:</b> {users}\nUsage: <code>/auth user_id</code>")
    db.add_auth(int(message.command[1]))
    await message.reply(f"✅ <code>{message.command[1]}</code> <b>is now authorized.</b>")


@app.on_message(filters.command("unauth") & filters.private)
@owner_only
async def unauth_cmd(client, message):
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await message.reply("Usage: <code>/unauth user_id</code>")
    removed = db.remove_auth(int(message.command[1]))
    await message.reply("✅ <b>Removed.</b>" if removed else "⚠️ <b>That user was not in the list.</b>")


@app.on_message(filters.command("restart") & filters.private)
@owner_only
async def restart_cmd(client, message):
    await message.reply("♻️ <b>Restarting...</b>")
    await restart_bot()


async def restart_bot() -> None:
    await db.flush()
    for task in list(TASKS.values()):
        task.cancel()
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable, *sys.argv])


# --------------------------------------------------------------------------- #
# Leech + incoming files + thumbnail
# --------------------------------------------------------------------------- #
@app.on_message(filters.command(["l", "leech"]) & filters.private)
@guarded
async def leech_cmd(client, message):
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        return await message.reply(
            "⚠️ <b>Usage:</b> <code>/l [direct link]</code>\n"
            "<i>Custom name:</i> <code>/l [link] | My New Name</code>"
        )
    link, _, custom = parts[1].partition("|")
    link, custom = link.strip(), custom.strip()
    if not tools.is_url(link):
        return await message.reply("⚠️ <b>Please send a valid http(s) / ftp direct link.</b>")
    guessed = link.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "downloaded_video.mp4"
    spawn(process_task(client, message, guessed, url=link, custom_name=custom or None))


@app.on_message((filters.document | filters.video | filters.audio) & filters.private)
@guarded
async def media_handler(client, message):
    media = message.document or message.video or message.audio
    if (getattr(media, "file_size", 0) or 0) > config.MAX_FILE_SIZE:
        return await message.reply("⚠️ <b>File size exceeds the limit!</b>")
    default_ext = ".mp3" if message.audio else ".mp4"
    name = getattr(media, "file_name", None) or f"file_{message.id}{default_ext}"
    spawn(process_task(client, message, name, media=media))


@app.on_message(filters.photo & filters.private)
@guarded
async def save_thumb(client, message):
    db.user(message.from_user.id)["thumb_file_id"] = message.photo.file_id
    db.touch()
    markup = kb([[btn("🖼 Thumbnail Settings", "s:thumb", "primary")]])
    await message.reply(
        "✅ <b>Custom thumbnail saved!</b>\n<blockquote>It is used for your next uploads "
        "(Thumbnail mode: Smart).</blockquote>", reply_markup=markup
    )


# --------------------------------------------------------------------------- #
# Processing pipeline
# --------------------------------------------------------------------------- #
async def process_task(client, message, original_name, url=None, media=None, custom_name=None):
    uid = message.from_user.id
    settings = db.settings(uid)
    task = Task(user_id=uid, chat_id=message.chat.id, name=original_name,
                source="url" if url else "telegram", client=client)
    TASKS[task.task_id] = task
    workdir = config.DOWNLOAD_DIR / f"{uid}_{task.task_id}"
    outcome = None  # None = cancelled, True = ok, False = failed
    uploaded_bytes = 0
    try:
        task.status_msg = await message.reply(
            tools.queued_text(task, queue_position(task)), reply_markup=tools.cancel_markup(task)
        )
        size = getattr(media, "file_size", 0) or 0
        if url:
            await tools.edit_status(task, "🔎 <b>Fetching link info...</b>", force=True)
            real_name, size = await tools.get_url_info(url)
            original_name = real_name or original_name
        if size and size > config.MAX_FILE_SIZE:
            raise UserError(f"File is too large ({size / 1e9:.2f} GB). Limit: {config.MAX_FILE_SIZE / 1e9:.2f} GB.")

        fmt = db.user(uid)["rename_format"] or config.DEFAULT_RENAME_FORMAT
        new_name = tools.build_new_name(original_name, fmt, settings["remove_words"], custom_name)
        task.name = new_name
        ext = tools.split_name(original_name)[1] or ".mp4"

        async with limiter.slot(task):
            task.started = time.time()
            workdir.mkdir(parents=True, exist_ok=True)
            source, final = workdir / f"source{ext}", workdir / new_name

            # 1) download ------------------------------------------------------
            await tools.set_state(task, "downloading", "Downloading")
            if url:
                await tools.download_from_url(task, url, source, size)
            else:
                path = await message.download(
                    file_name=str(source), progress=tools.telegram_progress,
                    progress_args=(task, "Downloading", time.time()),
                )
                task.check()
                if not path:
                    raise UserError("Telegram download failed. Please try again.")
                source = Path(path)

            # 2) rename / remux ------------------------------------------------
            await tools.set_state(task, "processing", "Processing")
            is_media = ext.lower() in config.VIDEO_EXTS | config.AUDIO_EXTS
            info = await tools.probe(source) if is_media else None
            processed = bool(info) and await tools.process_media(
                task, source, final, info, settings, db.user(uid)["metadata"]
            )
            if processed:
                source.unlink(missing_ok=True)
            else:  # not a media file (or ffmpeg refused it): rename only, keep original bytes
                shutil.move(str(source), str(final))
                if is_media:
                    task.notes.append("Metadata / audio / subtitle settings could NOT be applied "
                                      "(ffmpeg could not process this file). It was only renamed.")
            task.check()
            info = info or {"duration": 0, "width": 0, "height": 0, "has_video": False}

            # 3) thumbnail -----------------------------------------------------
            thumb = None
            if settings["thumb_mode"] != "off":
                custom_id = db.user(uid)["thumb_file_id"]
                if custom_id and settings["thumb_mode"] == "smart":
                    try:
                        raw = await client.download_media(custom_id, file_name=str(workdir / "custom_thumb"))
                        thumb = await tools.make_thumbnail(Path(raw), workdir / "thumb_custom.jpg") if raw else None
                    except Exception as exc:
                        log.warning("Custom thumbnail failed: %s", exc)
                if not thumb and info["has_video"]:
                    at = min(15, max(1, info["duration"] // 3))
                    thumb = await tools.make_thumbnail(final, workdir / "thumb_frame.jpg", at)

            # 4) upload --------------------------------------------------------
            await tools.set_state(task, "uploading", "Uploading")
            uploaded_bytes = final.stat().st_size
            caption = tools.build_caption(settings["caption"], new_name, uploaded_bytes,
                                          info["duration"], info["width"], info["height"])
            common = dict(caption=caption, progress=tools.telegram_progress,
                          progress_args=(task, "Uploading", time.time()))
            thumb_arg = str(thumb) if thumb else None
            suffix = final.suffix.lower()
            if settings["mode"] == "video" and suffix in config.VIDEO_EXTS and info["has_video"]:
                sent = await client.send_video(
                    task.chat_id, str(final), duration=info["duration"], width=info["width"],
                    height=info["height"], thumb=thumb_arg, supports_streaming=True, **common)
            elif settings["mode"] == "video" and suffix in config.AUDIO_EXTS:
                sent = await client.send_audio(
                    task.chat_id, str(final), duration=info["duration"], thumb=thumb_arg, **common)
            else:
                sent = await client.send_document(task.chat_id, str(final), thumb=thumb_arg, **common)
            task.check()
            if sent is None:
                raise UserError("Upload failed. Please try again.")

        outcome = True
        try:
            await task.status_msg.delete()
        except Exception:
            pass
        if task.notes:
            try:
                await sent.reply("⚠️ <b>Note</b>\n<blockquote>" + "\n".join(esc(n) for n in task.notes) + "</blockquote>")
            except Exception:
                pass
        if settings["delete_source"] and media is not None:
            try:
                await message.delete()
            except Exception:
                pass

    except TaskCancelled:
        await tools.edit_status(task, "❌ <b>Task cancelled by user.</b>", force=True, markup=None)
    except UserError as exc:
        outcome = False
        await tools.edit_status(task, f"❌ <b>Failed</b>\n<blockquote>{esc(exc)}</blockquote>", force=True, markup=None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        outcome = False
        log.exception("Task #%s crashed", task.task_id)
        await tools.edit_status(
            task, f"❌ <b>Unexpected error</b>\n<blockquote>{esc(type(exc).__name__)}: {esc(str(exc)[:200])}</blockquote>",
            force=True, markup=None)
    finally:
        TASKS.pop(task.task_id, None)
        tools.cleanup(workdir)
        if outcome is not None:
            db.record_result(uid, outcome, uploaded_bytes if outcome else 0)


# --------------------------------------------------------------------------- #
# Text input (values typed after tapping an "Edit" button)
# --------------------------------------------------------------------------- #
@app.on_message(filters.private & filters.text & ~filters.regex(r"^/"))
@guarded
async def text_input(client, message):
    uid = message.from_user.id
    state = input_state.pop(uid, None)
    if not state or state["expires"] < time.time():
        return
    value, action = message.text.strip(), state["action"]
    clear = value.lower() in ("-", "clear")
    st, user = db.settings(uid), db.user(uid)
    rollback = None

    notice = ""
    if action == "metabulk":
        values, unknown = tools.parse_bulk_metadata(message.text)
        if not values:
            input_state[uid] = {**state, "expires": time.time() + INPUT_TTL}  # let the user retry
            return await message.reply("⚠️ <b>No valid line found.</b>\nUse one tag per line, for example:\n"
                                       "<code>title: My Show\nartist: FlyTon</code>")
        for key, val in values.items():
            if val is None:
                user["metadata"].pop(key, None)
            else:
                user["metadata"][key] = val
        if unknown:
            notice = "⚠️ <b>Ignored unknown keys:</b> " + ", ".join(f"<code>{esc(k)}</code>" for k in unknown[:10])
        builder = ui.metadata_panel
    elif action.startswith("meta:"):
        key = action.split(":", 1)[1]
        if clear:
            user["metadata"].pop(key, None)
        else:
            user["metadata"][key] = value[:200]
        builder = ui.metadata_panel
    elif action == "fmt":
        user["rename_format"] = "" if clear else value[:200]
        builder = ui.rename_panel
    elif action == "caption":
        previous = st["caption"]
        rollback = lambda: st.__setitem__("caption", previous)
        st["caption"] = "" if clear else value[:700]
        builder = ui.caption_panel
    elif action == "words":
        words = [] if clear else [w.strip()[:40] for w in re.split(r"[,\n]", value) if w.strip()][:30]
        st["remove_words"] = words
        builder = ui.advanced_panel
    else:
        return
    db.touch()
    try:
        await message.delete()
    except Exception:
        pass
    try:
        text, markup = builder(uid)
        await client.edit_message_text(state["chat_id"], state["msg_id"], text, reply_markup=markup)
        if notice:
            await client.send_message(state["chat_id"], notice)
    except MessageNotModified:
        pass
    except Exception as exc:
        if rollback:
            rollback()
            db.touch()
        log.info("Panel refresh failed after input: %s", exc)
        await client.send_message(
            state["chat_id"], "⚠️ <b>That value could not be saved</b> (invalid HTML?). The previous value was kept.")


# --------------------------------------------------------------------------- #
# Callback queries
# --------------------------------------------------------------------------- #
async def edit(cq, panel) -> None:
    text, markup = panel
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except MessageNotModified:
        pass


async def ask(cq, uid, action, title, hint, cancel_target) -> None:
    input_state[uid] = {
        "action": action, "chat_id": cq.message.chat.id, "msg_id": cq.message.id,
        "expires": time.time() + INPUT_TTL,
    }
    await edit(cq, ui.prompt_panel(title, hint, cancel_target))


@app.on_callback_query()
async def on_callback(client, cq):
    uid = cq.from_user.id
    if not db.is_authorized(uid):
        return await cq.answer("⛔ This bot is private.", show_alert=True)
    input_state.pop(uid, None)  # any button press leaves the "waiting for input" mode
    data = cq.data or ""
    try:
        result = await route_callback(client, cq, uid, data.split(":"))
    except MessageNotModified:
        result = None
    except Exception as exc:
        log.exception("Callback failed: %s", data)
        result = (f"⚠️ {type(exc).__name__}: {str(exc)[:80]}", True)
    try:
        if result:
            await cq.answer(result[0], show_alert=result[1])
        else:
            await cq.answer()
    except Exception:
        pass


async def route_callback(client, cq, uid, parts):
    """Returns ``(popup_text, show_alert)`` or None."""
    ns = parts[0]
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    owner = db.is_owner(uid)
    st, user = db.settings(uid), db.user(uid)
    if cq.message is None:
        return None

    if ns == "x":
        await cq.message.delete()
        return None

    if ns == "go":  # open a panel as a NEW message (keeps the /start card intact)
        panels = {"settings": ui.settings_main, "metadata": ui.metadata_panel, "rename": ui.rename_panel}
        if action in panels:
            text, markup = panels[action](uid)
        elif action == "help":
            text, markup = ui.help_panel()
        elif action == "queue":
            text, markup = ui.queue_panel(uid, owner)
        elif action == "admin" and owner:
            text, markup = ui.admin_panel()
        else:
            return ("Not available.", True)
        await cq.message.reply(text, reply_markup=markup)
        return None

    if ns == "s":
        if action == "main":
            await edit(cq, ui.settings_main(uid))
        elif action == "mode":
            st["mode"] = "document" if st["mode"] == "video" else "video"
            db.touch()
            await edit(cq, ui.settings_main(uid))
        elif action == "sub":
            st["sub"] = not st["sub"]
            db.touch()
            await edit(cq, ui.settings_main(uid))
        elif action == "audio":
            if arg in config.AUDIO_LANGUAGES:
                st["audio"] = arg
                db.touch()
            await edit(cq, ui.audio_panel(uid))
        elif action == "thumb":
            if arg == "del":
                user["thumb_file_id"] = ""
                db.touch()
                await edit(cq, ui.thumb_panel(uid))
                return ("Custom thumbnail deleted.", False)
            await edit(cq, ui.thumb_panel(uid))
        elif action == "tmode":
            if arg in ui.THUMB_LABELS:
                st["thumb_mode"] = arg
                db.touch()
            await edit(cq, ui.thumb_panel(uid))
        elif action == "cap":
            if arg == "set":
                await ask(cq, uid, "caption", "Custom Caption",
                          "Variables: <code>{filename}</code> <code>{size}</code> <code>{duration}</code> "
                          "<code>{resolution}</code>\nHTML tags are supported.", "s:cap")
                return None
            if arg == "reset":
                st["caption"] = ""
                db.touch()
            await edit(cq, ui.caption_panel(uid))
        elif action == "fmt":
            if arg == "set":
                await ask(cq, uid, "fmt", "Rename Format",
                          "Example: <code>[FlyTon] {title} S{season}E{episode} {quality}</code>", "s:fmt")
                return None
            if arg == "reset":
                user["rename_format"] = ""
                db.touch()
            await edit(cq, ui.rename_panel(uid))
        elif action == "adv":
            if arg == "words":
                await ask(cq, uid, "words", "Remove Words",
                          "Comma separated words to strip from original names.\nExample: <code>www.site.com, x264, [Spam]</code>",
                          "s:adv")
                return None
            if arg == "delsrc":
                st["delete_source"] = not st["delete_source"]
            elif arg == "wclear":
                st["remove_words"] = []
            db.touch()
            await edit(cq, ui.advanced_panel(uid))
        elif action == "reset":
            if arg == "yes":
                user["settings"] = {}
                user["rename_format"] = ""
                db.touch()
                await edit(cq, ui.settings_main(uid))
                return ("Settings reset to defaults.", False)
            await edit(cq, ui.confirm_panel(
                "This resets all settings and the rename format. Metadata and thumbnail are kept.",
                "s:reset:yes", "s:main"))
        return None

    if ns == "m":
        if action == "open":
            await edit(cq, ui.metadata_panel(uid))
        elif action == "toggle":
            st["meta_enabled"] = not st["meta_enabled"]
            db.touch()
            await edit(cq, ui.metadata_panel(uid))
        elif action == "edit":
            label = "Change All" if arg == "all" else next((n for k, n, _ in config.METADATA_FIELDS if k == arg), None)
            if label is None:
                return ("Unknown field.", True)
            current = user["metadata"].get(arg)
            await ask(cq, uid, f"meta:{arg}", label,
                      f"Current: <code>{esc(current)}</code>" if current else "Current: <i>not set</i>", "m:open")
        elif action == "bulk":
            await ask(cq, uid, "metabulk", "Set Multiple Tags",
                      "One tag per line:\n<code>title: My Show\nartist: FlyTon\nyear: 2024</code>\n"
                      "<b>Keys:</b> all, title, desc, artist, album, year, author, official, copyright, "
                      "publisher, genre, encodedby, comment, encoder, audiotitle, subtitle\n"
                      "Use <code>key: -</code> to clear a tag.", "m:open")
        elif action == "reset":
            if arg == "yes":
                user["metadata"] = {}
                db.touch()
                await edit(cq, ui.metadata_panel(uid))
                return ("All metadata values cleared.", False)
            await edit(cq, ui.confirm_panel("Clear every saved metadata value?", "m:reset:yes", "m:open"))
        return None

    if ns == "t" and action == "cancel":
        task = TASKS.get(arg)
        if not task or (task.user_id != uid and not owner):
            return ("Task not found or already finished.", True)
        task.cancel()
        await limiter.wake()
        return ("Cancelling...", False)

    if ns == "q":
        if action == "all":
            for task in [t for t in TASKS.values() if owner or t.user_id == uid]:
                task.cancel()
            await limiter.wake()
            return ("Cancelling all your tasks...", False)
        await edit(cq, ui.queue_panel(uid, owner))
        return None

    if ns == "a" and owner:
        if action == "main":
            await edit(cq, ui.admin_panel())
        elif action == "stats":
            await edit(cq, ui.stats_panel())
        elif action == "conc" and arg.isdigit() and 1 <= int(arg) <= 4:
            await limiter.set_limit(int(arg))
            db.data["global"]["max_tasks"] = int(arg)
            db.touch()
            await edit(cq, ui.admin_panel())
        elif action == "restart":
            if arg == "yes":
                await cq.answer("Restarting...")
                await cq.message.edit_text("♻️ <b>Restarting...</b>")
                await restart_bot()
                return None
            await edit(cq, ui.confirm_panel("The bot will restart and running tasks will be cancelled.",
                                            "a:restart:yes", "a:main"))
        return None

    return ("Unknown or expired button.", False)


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #
COMMANDS = [
    ("start", "Start the bot"), ("autorename", "Set the rename format"), ("settings", "Open settings"),
    ("metadata", "Metadata engine"), ("l", "Leech a direct link"), ("queue", "Running tasks"),
    ("cancel", "Cancel tasks"), ("thumb", "Show custom thumbnail"), ("help", "Full guide"), ("ping", "Check latency"),
]


async def start_client() -> None:
    while True:
        try:
            await app.start()
            return
        except FloodWait as exc:
            log.warning("FloodWait while logging in - sleeping %ss", exc.value)
            await asyncio.sleep(float(exc.value) + 1)


async def main() -> None:
    log.info("Starting %s v%s on %s", config.BOT_NAME, config.VERSION, config.PLATFORM)
    shutil.rmtree(config.DOWNLOAD_DIR, ignore_errors=True)  # leftovers from a crash
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    await db.connect()
    await limiter.set_limit(db.data["global"].get("max_tasks", config.MAX_CONCURRENT_TASKS))
    runner = await start_web_server()
    keep_task = None
    if config.KEEP_ALIVE:
        if config.APP_URL:
            keep_task = asyncio.get_running_loop().create_task(keep_alive_loop())
        else:
            log.warning("KEEP_ALIVE is on but APP_URL / RENDER_EXTERNAL_URL is empty - ping skipped.")

    await start_client()
    me = await app.get_me()
    log.info("Logged in as @%s | database=%s | %s", me.username, db.backend, COLOR_MODE)
    try:
        await app.set_bot_commands([BotCommand(name, desc) for name, desc in COMMANDS])
    except Exception as exc:
        log.warning("Could not set bot commands: %s", exc)
    if config.NOTIFY_OWNER_ON_START:
        warning = ""
        if config.IS_RENDER and db.backend == "json":
            warning = ("\n\n⚠️ <b>Database is temporary!</b> Render wipes the disk on every restart, so your "
                       "settings and metadata will be lost. Add the <code>MONGO_URI</code> variable.")
        try:
            await app.send_message(
                config.OWNER_ID,
                f"✅ <b>{esc(config.BOT_NAME)} is online!</b>\n<blockquote>🌐 {esc(config.PLATFORM)}\n"
                f"💾 Database: {db.backend}\n🎨 {COLOR_MODE}</blockquote>{warning}")
        except Exception as exc:
            log.info("Owner start notification skipped: %s", exc)

    await idle()

    log.info("Shutting down...")
    for task in list(TASKS.values()):
        task.cancel()
    pending = list(BACKGROUND)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if keep_task:
        keep_task.cancel()
    await app.stop()
    await db.close()
    await runner.cleanup()


if __name__ == "__main__":
    LOOP.run_until_complete(main())
