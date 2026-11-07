"""Docker HEALTHCHECK helper: exits 0 when the bot's health endpoint answers."""
import os
import sys
import urllib.request

try:
    urllib.request.urlopen(f"http://127.0.0.1:{os.getenv('PORT', '8080')}/health", timeout=5)
except Exception:
    sys.exit(1)
