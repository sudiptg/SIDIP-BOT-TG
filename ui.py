"""Every panel (text + colourful keyboard) shown by the bot lives here."""
import os
import shutil

import config
import tools
from database import db
from tasks import TASKS, limiter
from utils import COLOR_MODE, btn, esc, format_bytes, kb, shorten, uptime

SAMPLE_NAME = "Naruto.Shippuden.S02E15.1080p.WEB-DL.x265.mkv"
THUMB_LABELS = {"smart": "Smart", "frame": "Frame", "off": "Off"}
STATE_LABELS = {"queued": "⏳ queued", "downloading": "📥 downloading", "processing": "⚙️ processing", "uploading": "📤 uploading"}


def _onoff(value: bool):
    return ("ON", "success") if value else ("OFF", "danger")


def _db_label() -> str:
    if config.IS_RENDER and db.backend == "json":
        return "json ⚠️ temporary - set MONGO_URI"
    return db.backend


def _fmt(uid) -> str:
    return db.user(uid)["rename_format"] or config.DEFAULT_RENAME_FORMAT


# --------------------------------------------------------------------------- #
# Start / help
# --------------------------------------------------------------------------- #
def start_panel(is_owner: bool):
    text = (
        f"👋 <b>Welcome to {esc(config.BOT_NAME)} Auto-Rename Bot!</b>\n\n"
        "<blockquote>🛠 <b>Commands</b>\n"
        "➜ /autorename - Set renaming format\n"
        "➜ /l [link] - Leech a direct link\n"
        "➜ /settings - Output, audio, thumbnail &amp; more\n"
        "➜ /metadata - Custom metadata engine\n"
        "➜ /queue - Running &amp; waiting tasks\n"
        "➜ /help - Full guide</blockquote>\n"
        "📤 <i>Just send me any file to rename it.</i>"
    )
    last = [btn("📖 Help", "go:help")]
    if is_owner:
        last.append(btn("🛠 Admin", "go:admin", "danger"))
    rows = [
        [btn("⚙️ Settings", "go:settings", "primary"), btn("🏷 Metadata", "go:metadata", "primary")],
        [btn("✏️ Rename Format", "go:rename", "success"), btn("📋 Queue", "go:queue", "primary")],
        last,
        [btn("❌ Close", "x:close", "danger")],
    ]
    return text, kb(rows)


def help_panel():
    text = (
        "📖 <b>How it works</b>\n"
        "<blockquote>1. Set a format with /autorename\n"
        "2. Send any video / document / audio - or use <code>/l link</code>\n"
        "3. The bot downloads, renames, rewrites metadata and uploads it back.</blockquote>\n"
        "<b>Format variables</b>\n"
        "<blockquote><code>{title}</code> <code>{season}</code> <code>{episode}</code> <code>{quality}</code>\n"
        "<code>{year}</code> <code>{codec}</code> <code>{original}</code></blockquote>\n"
        "<b>Caption variables</b>\n"
        "<blockquote><code>{filename}</code> <code>{size}</code> <code>{duration}</code> <code>{resolution}</code></blockquote>\n"
        "<b>Extras</b>\n"
        "<blockquote>• <code>/l link | My New Name</code> - leech with a custom name\n"
        "• Send a photo to set a custom thumbnail\n"
        "• /cancel [id|all] - cancel tasks</blockquote>"
    )
    return text, kb([[btn("⚙️ Settings", "go:settings", "primary"), btn("❌ Close", "x:close", "danger")]])


def prompt_panel(title: str, hint: str, cancel_target: str):
    text = (
        f"✏️ <b>{esc(title)}</b>\n<blockquote>{hint}</blockquote>\n"
        "<i>Send the new value now, or tap Cancel. Send</i> <code>-</code> <i>to clear it.</i>"
    )
    return text, kb([[btn("❌ Cancel", cancel_target, "danger")]])


def confirm_panel(question: str, yes_data: str, no_data: str):
    text = f"⚠️ <b>Are you sure?</b>\n<blockquote>{question}</blockquote>"
    return text, kb([[btn("✅ Yes, do it", yes_data, "success"), btn("✖️ No", no_data, "danger")]])


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def settings_main(uid):
    st, user = db.settings(uid), db.user(uid)
    sub_label, sub_style = _onoff(st["sub"])
    audio_label = config.AUDIO_LANGUAGES.get(st["audio"], ("All tracks",))[0]
    mode_label = "Video" if st["mode"] == "video" else "Document"
    custom = " + custom" if user["thumb_file_id"] else ""
    text = (
        "⚙️ <b>Settings</b>\n"
        "<blockquote>Fine-tune how your files are renamed, processed and uploaded.</blockquote>\n"
        f"🎬 <b>Upload as:</b> {mode_label}\n"
        f"🎵 <b>Audio:</b> {audio_label}\n"
        f"📝 <b>Subtitles:</b> {sub_label}\n"
        f"🖼 <b>Thumbnail:</b> {THUMB_LABELS[st['thumb_mode']]}{custom}\n"
        f"🏷 <b>Metadata:</b> {'ON' if st['meta_enabled'] else 'OFF'}\n"
        f"✏️ <b>Format:</b> <code>{esc(shorten(_fmt(uid), 60))}</code>"
    )
    rows = [
        [btn(f"🎬 Upload as: {mode_label}", "s:mode", "primary")],
        [btn(f"🎵 Audio: {audio_label}", "s:audio", "primary"), btn(f"📝 Subs: {sub_label}", "s:sub", sub_style)],
        [btn("🖼 Thumbnail", "s:thumb", "primary"), btn("💬 Caption", "s:cap", "primary")],
        [btn("✏️ Rename Format", "s:fmt", "success"), btn("🏷 Metadata", "m:open", "success")],
        [btn("🧰 Advanced", "s:adv", "primary"), btn("♻️ Reset", "s:reset", "danger")],
        [btn("❌ Close", "x:close", "danger")],
    ]
    return text, kb(rows)


def audio_panel(uid):
    current = db.settings(uid)["audio"]
    text = (
        "🎵 <b>Audio Track Filter</b>\n"
        "<blockquote>Keep only the chosen language. If a file has no track in that language, "
        "all tracks are kept so you never get a silent video.</blockquote>"
    )
    buttons = []
    for key, (label, _) in config.AUDIO_LANGUAGES.items():
        selected = key == current
        buttons.append(btn(f"✅ {label}" if selected else label, f"s:audio:{key}", "success" if selected else None))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("🔙 Back", "s:main")])
    return text, kb(rows)


def thumb_panel(uid):
    st, user = db.settings(uid), db.user(uid)
    has_custom = bool(user["thumb_file_id"])
    text = (
        "🖼 <b>Thumbnail</b>\n"
        f"<blockquote>Custom thumbnail: <b>{'saved ✅' if has_custom else 'not set'}</b>\n"
        "📷 Send any photo to this chat to set a custom thumbnail.</blockquote>\n"
        "<b>Modes</b>\n"
        "• <b>Smart</b> - custom thumbnail, else a video frame\n"
        "• <b>Frame</b> - always a frame from the video\n"
        "• <b>Off</b> - no thumbnail"
    )
    mode_buttons = []
    for key, label in THUMB_LABELS.items():
        selected = st["thumb_mode"] == key
        mode_buttons.append(btn(f"✅ {label}" if selected else label, f"s:tmode:{key}", "success" if selected else "primary"))
    rows = [mode_buttons]
    if has_custom:
        rows.append([btn("🗑 Delete custom thumbnail", "s:thumb:del", "danger")])
    rows.append([btn("🔙 Back", "s:main")])
    return text, kb(rows)


def caption_panel(uid):
    template = db.settings(uid)["caption"]
    preview = tools.build_caption(template, "Naruto S02E15 1080p.mkv", 734003200, 1440, 1920, 1080)
    text = (
        "💬 <b>Custom Caption</b>\n"
        f"<blockquote>Template: <code>{esc(template or config.DEFAULT_CAPTION)}</code></blockquote>\n"
        f"<b>Preview</b>\n{preview}\n\n"
        "<b>Variables:</b> <code>{filename}</code> <code>{size}</code> <code>{duration}</code> <code>{resolution}</code>\n"
        "<i>HTML tags like &lt;b&gt;, &lt;i&gt;, &lt;code&gt;, &lt;blockquote&gt; are supported.</i>"
    )
    rows = [
        [btn("✏️ Set Caption", "s:cap:set", "success"), btn("♻️ Reset", "s:cap:reset", "danger")],
        [btn("🔙 Back", "s:main")],
    ]
    return text, kb(rows)


def rename_panel(uid):
    fmt = _fmt(uid)
    sample = tools.build_new_name(SAMPLE_NAME, fmt, db.settings(uid)["remove_words"])
    text = (
        "✏️ <b>Auto-Rename Format</b>\n"
        f"<blockquote>Current: <code>{esc(fmt)}</code>\n"
        f"Preview: <code>{esc(sample)}</code>\n"
        f"<i>from</i> <code>{esc(SAMPLE_NAME)}</code></blockquote>\n"
        "<b>Variables</b>\n"
        "<code>{title}</code> <code>{season}</code> <code>{episode}</code> <code>{quality}</code> "
        "<code>{year}</code> <code>{codec}</code> <code>{original}</code>\n\n"
        "<i>Missing values (e.g. no season) are removed automatically.</i>\n"
        "Quick set: <code>/autorename [FlyTon] {title} S{season}E{episode} {quality}</code>"
    )
    rows = [
        [btn("✏️ Set Format", "s:fmt:set", "success"), btn("♻️ Default", "s:fmt:reset", "danger")],
        [btn("⚙️ Settings", "s:main"), btn("❌ Close", "x:close", "danger")],
    ]
    return text, kb(rows)


def advanced_panel(uid):
    st = db.settings(uid)
    del_label, del_style = _onoff(st["delete_source"])
    words = ", ".join(st["remove_words"]) or "none"
    text = (
        "🧰 <b>Advanced</b>\n"
        f"<blockquote>🗑 <b>Auto-delete source:</b> {del_label}\n"
        f"🚫 <b>Remove words:</b> <code>{esc(shorten(words, 80))}</code></blockquote>\n"
        "<i>Remove words are stripped from the original file name before renaming "
        "(site names, tags, spam...).</i>"
    )
    rows = [
        [btn(f"🗑 Delete source: {del_label}", "s:adv:delsrc", del_style)],
        [btn("🚫 Set Remove Words", "s:adv:words", "primary"), btn("🧹 Clear", "s:adv:wclear", "danger")],
        [btn("🔙 Back", "s:main")],
    ]
    return text, kb(rows)


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def metadata_panel(uid):
    meta = db.user(uid)["metadata"]
    enabled = db.settings(uid)["meta_enabled"]

    def value(key):
        stored = meta.get(key)
        return f"<code>{esc(shorten(stored, 40))}</code>" if stored else "<i>not set</i>"

    lines = [f"🔄 <b>Change All:</b> {value('all')}"]
    lines += [f"{emoji} <b>{label}:</b> {value(key)}" for key, label, emoji in config.METADATA_FIELDS]
    text = (
        "🏷 <b>Metadata Engine</b>\n"
        f"<blockquote>{chr(10).join(lines)}</blockquote>\n"
        "<i>'Change All' fills every tag that has no value of its own. 'Set Multiple' saves many tags in one message. "
        "MKV keeps every tag; MP4 switches to extended tags automatically when needed.</i>"
    )
    label, style = _onoff(enabled)
    rows = [[btn(f"🏷 Metadata: {label}", "m:toggle", style)], [btn("🔄 Change All", "m:edit:all", "success"), btn("📋 Set Multiple", "m:bulk", "success")]]
    fields = [btn(f"{emoji} {name}", f"m:edit:{key}", "primary") for key, name, emoji in config.METADATA_FIELDS]
    rows += [fields[i:i + 2] for i in range(0, len(fields), 2)]
    rows.append([btn("🧹 Reset All", "m:reset", "danger"), btn("⚙️ Settings", "s:main")])
    rows.append([btn("❌ Close", "x:close", "danger")])
    return text, kb(rows)


# --------------------------------------------------------------------------- #
# Queue / admin / stats
# --------------------------------------------------------------------------- #
def queue_panel(uid, is_owner: bool):
    tasks = sorted((t for t in TASKS.values() if is_owner or t.user_id == uid), key=lambda t: t.seq)
    if not tasks:
        return "📋 <b>Queue</b>\n<blockquote>No active tasks right now.</blockquote>", kb(
            [[btn("🔄 Refresh", "q:refresh", "primary"), btn("❌ Close", "x:close", "danger")]]
        )
    lines = [
        f"<code>#{t.task_id}</code> {STATE_LABELS.get(t.state, t.state)} - <code>{esc(shorten(t.name, 28))}</code>"
        for t in tasks[:10]
    ]
    text = f"📋 <b>Queue</b> ({len(tasks)})\n<blockquote>{chr(10).join(lines)}</blockquote>"
    rows = [[btn(f"❌ Cancel #{t.task_id}", f"t:cancel:{t.task_id}", "danger")] for t in tasks[:8]]
    rows.append([btn("🔄 Refresh", "q:refresh", "primary"), btn("🛑 Cancel All", "q:all", "danger")])
    rows.append([btn("❌ Close", "x:close", "danger")])
    return text, kb(rows)


def admin_panel():
    users = len(db.data["users"])
    auth = db.auth_list()
    text = (
        "🛠 <b>Admin Panel</b>\n"
        f"<blockquote>👥 <b>Users:</b> {users}   🔐 <b>Authorized:</b> {len(auth) + len(config.AUTH_USERS)}\n"
        f"⚙️ <b>Parallel tasks:</b> {limiter.limit}   🏃 <b>Running:</b> {limiter.active}\n"
        f"💾 <b>Database:</b> {_db_label()}   🌐 <b>Host:</b> {esc(config.PLATFORM)}</blockquote>\n"
        "<i>Tap a number below to change parallel tasks. Manage access with</i> "
        "<code>/auth id</code> <i>and</i> <code>/unauth id</code>"
    )
    concurrency = [
        btn(f"✅ {n}" if limiter.limit == n else str(n), f"a:conc:{n}", "success" if limiter.limit == n else "primary")
        for n in (1, 2, 3, 4)
    ]
    rows = [
        concurrency,
        [btn("📊 Stats", "a:stats", "primary"), btn("♻️ Restart", "a:restart", "danger")],
        [btn("❌ Close", "x:close", "danger")],
    ]
    return text, kb(rows)


def _read_int(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _stat_value(path: str, key: str) -> int:
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                name, _, value = line.partition(" ")
                if name == key:
                    return int(value)
    except (OSError, ValueError):
        pass
    return 0


def _container_memory():
    """Return (used, limit) bytes of THIS container (cgroup v2 / v1), or None when unlimited.

    psutil would show the whole host machine, which is misleading on Render / Docker.
    Page cache is excluded, like ``docker stats`` does.
    """
    layouts = (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat", "inactive_file"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.stat", "total_inactive_file"),
    )
    for limit_path, used_path, stat_path, cache_key in layouts:
        limit, used = _read_int(limit_path), _read_int(used_path)  # "max" -> None (no limit)
        if limit and used is not None and limit < (1 << 50):
            return max(0, used - _stat_value(stat_path, cache_key)), limit
    return None


def _folder_size(folder) -> int:
    try:
        return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    except OSError:
        return 0


def stats_panel():
    stats = db.data["global"]["stats"]
    lines = [
        f"⏱ <b>Uptime:</b> {uptime()}",
        f"📦 <b>Done:</b> {stats['done']}   ❌ <b>Failed:</b> {stats['failed']}",
        f"📤 <b>Processed:</b> {format_bytes(stats['bytes'])}",
        f"🏃 <b>Active:</b> {len(TASKS)}   👥 <b>Users:</b> {len(db.data['users'])}",
    ]
    memory = _container_memory()
    if memory:
        used, limit = memory
        lines.append(f"🧠 <b>RAM:</b> {format_bytes(used)} / {format_bytes(limit)} ({used * 100 / limit:.0f}%)")
    else:
        try:
            import psutil

            lines.append(f"🧠 <b>RAM:</b> {psutil.virtual_memory().percent:.0f}% (host)")
        except Exception:
            pass
    lines.append(f"💽 <b>Temp files:</b> {format_bytes(_folder_size(config.DOWNLOAD_DIR))}   "
                 f"free disk: {format_bytes(shutil.disk_usage(str(config.DOWNLOAD_DIR)).free)}")
    lines += [
        f"💾 <b>Database:</b> {_db_label()}",
        f"🌐 <b>Host:</b> {esc(config.PLATFORM)}   🎨 <b>Buttons:</b> {'native' if 'native' in COLOR_MODE else 'emoji'}",
        f"🔖 <b>Version:</b> {config.VERSION}   🐍 PID {os.getpid()}",
    ]
    text = f"📊 <b>Bot Stats</b>\n<blockquote>{chr(10).join(lines)}</blockquote>"
    return text, kb([[btn("🔄 Refresh", "a:stats", "primary"), btn("🛠 Admin", "a:main")],
                     [btn("❌ Close", "x:close", "danger")]])
