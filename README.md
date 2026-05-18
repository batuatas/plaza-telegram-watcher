# Plaza / NewNewNew Telegram Watcher

Watches [Plaza Resident Services](https://plaza.newnewnew.space/aanbod/wonen) for newly published housing listings and sends a Telegram notification for each match.

Uses **Playwright** (headless Chromium) because the listing pages are rendered by JavaScript.  
Runs as a **GitHub Actions cron job** every 5 minutes — no server required.

---

## How it works

1. Opens the Plaza listings page with a headless Chromium browser.
2. Collects all listing detail URLs.
3. Skips any listing IDs already stored in `plaza_seen.sqlite3`.
4. For each new listing: extracts title, city, rent, availability, etc.
5. Applies optional filters (`CITY_FILTER`, `MAX_TOTAL_RENT`).
6. Sends a formatted Telegram message for every match.
7. Saves the updated `plaza_seen.sqlite3` back to the repository so the next run remembers what was already seen.

---

## GitHub Actions setup (recommended — no server needed)

### 1. Fork / clone this repository

```bash
git clone https://github.com/batuatas/plaza-telegram-watcher.git
cd plaza-telegram-watcher
```

### 2. Add repository secrets

Go to your GitHub repository → **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | Your personal or group chat ID |

> **Never put real secrets in any file that is committed to git.**  
> `.env` is excluded by `.gitignore` and must stay local only.

### 3. (Optional) Adjust filters in the workflow

Edit `.github/workflows/plaza-check.yml` and change:

```yaml
CITY_FILTER: "Amsterdam"   # leave empty to watch all cities
MAX_TOTAL_RENT: ""         # e.g. "1500" to filter by max total rent
```

### 4. Trigger the workflow manually

Go to your repository on GitHub → **Actions → Plaza Telegram Watcher → Run workflow → Run workflow**.

The scheduled cron (`*/5 * * * *`) will also run automatically once the workflow is enabled.

> ⚠️ **Note:** GitHub may delay scheduled workflows by several minutes during high-load periods. The schedule is approximate, not real-time.

### 5. How seen listings are persisted

After each run, GitHub Actions commits the updated `plaza_seen.sqlite3` back to the repository with:

```
chore: update plaza_seen.sqlite3 [skip ci]
```

This file contains only public listing IDs and details — **no secrets**. It is intentionally tracked by git so state is preserved between runs.

---

## Local setup

### 1. Create a Telegram bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot` and follow the steps. Copy the bot token.
3. Send any message to your new bot.
4. Open this URL (replace `<TOKEN>`):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
5. Find your `chat.id` in the JSON response.

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and set your real values:

```text
TELEGRAM_BOT_TOKEN=<your token from BotFather>
TELEGRAM_CHAT_ID=<your chat id>
```

> **`.env` must never be committed.** It is already listed in `.gitignore`.

### 4. Run locally (continuous loop)

```bash
python plaza_telegram_bot.py
```

By default, the **first run** stores all currently visible listings without sending them — only future new publications are notified. To send everything on the first run:

```text
SEND_EXISTING_ON_FIRST_RUN=true
```

### 5. Run one-shot (same as GitHub Actions)

```bash
python plaza_github_once.py
```

Exits after one check. Useful for local testing of the Actions logic.

---

## Optional filters

In `.env` (local) or the workflow YAML (GitHub Actions):

```text
CITY_FILTER=Amsterdam       # only listings matching this city name
MAX_TOTAL_RENT=1500         # skip listings with total rent above this amount
```

Leave empty to receive notifications for all cities / all rent levels.

---

## Files

| File | Purpose |
|---|---|
| `plaza_telegram_bot.py` | Core scraper + local continuous-loop runner |
| `plaza_github_once.py` | One-shot runner used by GitHub Actions |
| `.github/workflows/plaza-check.yml` | GitHub Actions workflow (cron every 5 min) |
| `.env.example` | Template — copy to `.env` and fill in secrets |
| `.env` | **Local only** — never commit |
| `plaza_seen.sqlite3` | Persisted state — committed automatically by CI |
| `requirements.txt` | Python dependencies |

---

## Notes

- Do not set `CHECK_INTERVAL_SECONDS` below 30 in the local loop.
- The script does **not** log in and does **not** auto-apply for listings. It only watches public pages and sends notifications.
- If the Plaza website changes its frontend, the parsing labels in `plaza_telegram_bot.py` may need minor updates.
