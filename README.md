# Auto-Rename Bot v3

Telegram auto-rename / leech bot (Pyrogram, aria2, ffmpeg) with colourful inline buttons,
a pro settings panel and ready-to-use deployment files for **Render Free**, **Render Paid** and a **VPS**.

## Features

- **Colourful buttons** - native blue / green / red buttons (Bot API 9.4+). Falls back to 🔵🟢🔴 emoji dots on forks without support.
- **Settings panel** - upload mode, audio language filter, subtitles, thumbnail mode (smart / frame / off), custom caption, rename format, advanced options, one-tap reset.
- **Metadata engine** - 15 editable tags + "Change All", master ON/OFF switch, reset, audio / subtitle track titles.
- **Smart rename** - `{title} {season} {episode} {quality} {year} {codec} {original}`; missing values vanish cleanly; live preview.
- **Leech** - `/l link` or `/l link | Custom Name` with live aria2 progress (real size, speed, ETA).
- **Queue** - `/queue`, per-task cancel buttons, cancel while queued / downloading / processing / uploading, 1-4 parallel tasks (live from `/admin`).
- **Admin** - `/admin`, `/stats`, `/auth`, `/unauth`, `/restart`.
- **Persistent data** - local JSON or MongoDB. Old `database.json` files are migrated automatically.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `API_ID`, `API_HASH` | yes | from https://my.telegram.org |
| `BOT_TOKEN` | yes | from @BotFather |
| `OWNER_ID` | yes | your numeric Telegram id |
| `AUTH_USERS` | no | extra allowed user ids, comma separated |
| `MONGO_URI` | **Render Free: yes** | MongoDB connection string (free Atlas cluster works) |
| `DATA_DIR` / `DOWNLOAD_DIR` | no | where `database.json` / temp files live |
| `MAX_CONCURRENT_TASKS` | no | parallel tasks (default 1) |
| `KEEP_ALIVE` | no | `true` on Render Free only |
| `APP_URL` | no | public URL for keep-alive (auto-detected on Render) |
| `PORT` | no | Render sets it; default 8080 |

Full list: see `.env.example`.

## Deploy 1 - Render FREE

1. Push this folder to a GitHub repository.
2. Render dashboard -> **New -> Web Service** -> pick the repo -> **Runtime: Docker** -> **Instance type: Free**.
   (or **New -> Blueprint** and set the blueprint path to `deploy/render-free.yaml`)
3. Add environment variables: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `MONGO_URI`, `KEEP_ALIVE=true`.
4. Health check path: `/health`. Deploy.

Free-tier facts: the service spins down after 15 minutes without inbound HTTP traffic (the keep-alive ping prevents it),
the filesystem is wiped on every restart (that is why `MONGO_URI` is required), and RAM / CPU are small -
keep `MAX_CONCURRENT_TASKS=1` and avoid very large files.

## Deploy 2 - Render PAID

1. **New -> Background Worker** (or Blueprint with `deploy/render-paid.yaml`) -> Runtime: Docker -> Starter plan or higher.
2. Attach a **Disk** mounted at `/data`.
3. Environment: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `DATA_DIR=/data`, `DOWNLOAD_DIR=/data/downloads`, `KEEP_ALIVE=false`.
   `MONGO_URI` is optional because the disk persists `database.json`.

## Deploy 3 - VPS

**Option A - Docker (recommended)**

```bash
git clone <your-repo> autorename-bot && cd autorename-bot
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

**Option B - systemd, no Docker (Ubuntu / Debian)**

```bash
sudo bash deploy/vps_install.sh
sudo nano /opt/autorename-bot/.env
sudo systemctl restart autorename-bot
sudo journalctl -u autorename-bot -f
```

## Important notes

- Run **only one instance per bot token** (do not deploy Render Free + Render Paid + VPS with the same token at the same time).
  Use a separate bot token for each deployment.
- Switching library: `pip uninstall -y pyrofork pyrogram` before installing `pyrotgfork` in an existing environment.
- Telegram bots can upload files up to 2 GB.
