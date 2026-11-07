"""Small shared helpers: text formatting and the colourful button factory."""
import html
import inspect
import time

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config

# --------------------------------------------------------------------------- #
# Colourful buttons
#
# Telegram (Bot API 9.4+) supports coloured inline buttons:
#   primary = blue, success = green, danger = red.
# The installed Pyrogram fork must expose ``InlineKeyboardButton(style=...)``
# (e.g. ``pyrotgfork``). On forks without it (e.g. ``pyrofork``) the bot falls
# back automatically to coloured emoji dots so the UI still looks colourful.
# --------------------------------------------------------------------------- #
try:
    from pyrogram.enums import ButtonStyle
except Exception:  # pragma: no cover - depends on installed fork
    ButtonStyle = None

NATIVE_COLORS = False
if ButtonStyle is not None:
    try:
        NATIVE_COLORS = "style" in inspect.signature(InlineKeyboardButton.__init__).parameters
    except (TypeError, ValueError):
        NATIVE_COLORS = False

COLOR_MODE = "native colored buttons" if NATIVE_COLORS else "emoji colored buttons (fallback)"
_DOTS = {"success": "🟢", "danger": "🔴", "primary": "🔵"}


def btn(text: str, data: str = None, style: str = None, url: str = None) -> InlineKeyboardButton:
    """Create an inline button. ``style`` is one of: primary, success, danger or None."""
    kwargs = {"url": url} if url else {"callback_data": data}
    if style and NATIVE_COLORS:
        member = getattr(ButtonStyle, style.upper(), None)
        if member is not None:
            try:
                return InlineKeyboardButton(text, style=member, **kwargs)
            except TypeError:
                pass
    if style in _DOTS:
        text = f"{_DOTS[style]} {text}"
    return InlineKeyboardButton(text, **kwargs)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def esc(text) -> str:
    return html.escape(str(text), quote=False)


def shorten(text, limit: int = 40) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_bytes(size) -> str:
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return "0 B"


def format_time(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def bar(percent: float, width: int = 12) -> str:
    filled = max(0, min(width, int(width * percent / 100)))
    return "▰" * filled + "▱" * (width - filled)


def uptime() -> str:
    return format_time(time.time() - config.START_TIME)
