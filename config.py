"""Central configuration.

Every value is read from environment variables (recommended for Render, Docker and VPS)
or from a local ``.env`` file. No secret is ever hard-coded in the source code.
"""
import os
import sys
import time
from pathlib import Path

VERSION = "3.0.0"
START_TIME = time.time()


# --------------------------------------------------------------------------- #
# Tiny .env loader (no extra dependency)
# --------------------------------------------------------------------------- #
def _load_dotenv(path: str = ".env") -> None:
    env_file = Path(path)
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _id_list(name: str) -> list:
    result = []
    for part in os.getenv(name, "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.append(int(part))
    return result


# --------------------------------------------------------------------------- #
# Telegram credentials (REQUIRED)
# --------------------------------------------------------------------------- #
API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = _int("OWNER_ID")
AUTH_USERS = _id_list("AUTH_USERS")  # extra users allowed from the very first start

# --------------------------------------------------------------------------- #
# Branding
# --------------------------------------------------------------------------- #
BOT_NAME = os.getenv("BOT_NAME", "FlyTon V.I.P")
WELCOME_PIC = os.getenv("WELCOME_PIC", "https://graph.org/file/eb42142e0573663a8a07c.jpg")
DEFAULT_RENAME_FORMAT = os.getenv("DEFAULT_RENAME_FORMAT", "[FlyTon] {title} S{season}E{episode} {quality}")
DEFAULT_CAPTION = "<blockquote>📁 <code>{filename}</code></blockquote>"
NOTIFY_OWNER_ON_START = _bool("NOTIFY_OWNER_ON_START", True)

# --------------------------------------------------------------------------- #
# Limits / performance
# --------------------------------------------------------------------------- #
MAX_FILE_SIZE = _int("MAX_FILE_SIZE", 2_000_000_000)          # bots can upload up to 2 GB
MAX_CONCURRENT_TASKS = max(1, _int("MAX_CONCURRENT_TASKS", 1))  # can be changed live from /admin
MAX_TRANSMISSIONS = _int("MAX_TRANSMISSIONS", 15)
ARIA2_CONNECTIONS = min(16, max(1, _int("ARIA2_CONNECTIONS", 16)))
EDIT_INTERVAL = 3.0  # seconds between progress edits (protects from FloodWait)

# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
DATA_DIR = Path(os.getenv("DATA_DIR", "data")).resolve()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).resolve()
DB_FILE = DATA_DIR / "database.json"
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "autorename_bot")

# --------------------------------------------------------------------------- #
# Web server + keep-alive (needed by Render Web Services)
# --------------------------------------------------------------------------- #
HOST = "0.0.0.0"
PORT = _int("PORT", 8080)  # Render injects PORT (default 10000)
PLATFORM = "Render" if os.getenv("RENDER") else "Self-hosted (VPS / Docker)"
APP_URL = (os.getenv("APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
KEEP_ALIVE = _bool("KEEP_ALIVE", default=bool(APP_URL))
PING_INTERVAL = max(60, _int("PING_INTERVAL", 240))

# --------------------------------------------------------------------------- #
# Static tables
# --------------------------------------------------------------------------- #
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi", ".ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".mka"}
MP4_FAMILY = {".mp4", ".m4v", ".mov"}

AUDIO_LANGUAGES = {
    "all": ("All tracks", ()),
    "hindi": ("Hindi", ("hin", "hi", "hindi")),
    "english": ("English", ("eng", "en", "english")),
    "japanese": ("Japanese", ("jpn", "ja", "japanese")),
    "tamil": ("Tamil", ("tam", "ta", "tamil")),
    "telugu": ("Telugu", ("tel", "te", "telugu")),
    "korean": ("Korean", ("kor", "ko", "korean")),
}

# (key, label, emoji) - order = order of the metadata panel
METADATA_FIELDS = [
    ("title", "Title", "📌"), ("desc", "Description", "📝"),
    ("artist", "Artist", "🎵"), ("album", "Album", "💿"),
    ("year", "Year", "📅"), ("author", "Author", "✍️"),
    ("official", "Official Site", "🌐"), ("copyright", "Copyright", "©️"),
    ("publisher", "Publisher", "🏢"), ("genre", "Genre", "🎭"),
    ("encodedby", "Encoded By", "🧑‍💻"), ("comment", "Comment", "💬"),
    ("encoder", "Encoder", "⚙️"), ("audiotitle", "Audio Title", "🔊"),
    ("subtitle", "Subtitle Title", "💭"),
]

DEFAULT_SETTINGS = {
    "mode": "video",        # video | document
    "audio": "all",         # key of AUDIO_LANGUAGES
    "sub": True,            # keep subtitle tracks
    "thumb_mode": "smart",  # smart (custom -> frame) | frame | off
    "caption": "",          # empty = DEFAULT_CAPTION
    "delete_source": False, # delete the user's original file message after upload
    "remove_words": [],     # words stripped from the original name before renaming
    "meta_enabled": True,   # inject metadata tags
}


def validate() -> None:
    """Stop with a clear message when a required variable is missing."""
    required = (("API_ID", API_ID), ("API_HASH", API_HASH), ("BOT_TOKEN", BOT_TOKEN), ("OWNER_ID", OWNER_ID))
    missing = [name for name, value in required if not value]
    if missing:
        sys.exit("Missing required environment variables: " + ", ".join(missing) +
                 "\nCopy .env.example to .env (or set them in your hosting dashboard).")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
