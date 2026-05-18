#!/usr/bin/env python3
"""
One-shot runner for GitHub Actions.

Reads all config from environment variables (set as GitHub Actions secrets /
workflow env vars), calls check_once() from plaza_telegram_bot, then exits.

Usage:
    python plaza_github_once.py

Required env vars (set as GitHub Actions secrets):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Optional env vars (set in workflow YAML):
    LIST_URL              - Plaza listings URL
    DB_PATH               - SQLite state file (default: plaza_seen.sqlite3)
    CITY_FILTER           - e.g. Amsterdam (empty = all cities)
    MAX_TOTAL_RENT        - e.g. 1500 (empty = no rent filter)
    SEND_EXISTING_ON_FIRST_RUN - true/false
    HEADLESS              - true/false (default: true)
    BROWSER_LOCALE        - e.g. nl-NL
    SLOW_MO_MS            - e.g. 0
    DEBUG_DUMP_TEXT       - true/false (default: false)
"""

from __future__ import annotations

import os
import sys

# Load .env if present (local dev only; GitHub Actions uses env vars directly).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional for the runner itself

from plaza_telegram_bot import setup_db, check_once, DEFAULT_LIST_URL


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    list_url = os.getenv("LIST_URL", DEFAULT_LIST_URL).strip() or DEFAULT_LIST_URL
    db_path = os.getenv("DB_PATH", "plaza_seen.sqlite3").strip() or "plaza_seen.sqlite3"
    city_filter = os.getenv("CITY_FILTER", "").strip()
    max_rent = os.getenv("MAX_TOTAL_RENT", "").strip()

    # --- Validate required secrets (fail loudly but safely) ---
    if not token:
        print(
            "ERROR: TELEGRAM_BOT_TOKEN is not set. "
            "Add it as a GitHub Actions repository secret.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    if not chat_id:
        print(
            "ERROR: TELEGRAM_CHAT_ID is not set. "
            "Add it as a GitHub Actions repository secret.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    # --- Log non-secret config (safe to print) ---
    print(f"[plaza-once] list_url={list_url}", flush=True)
    print(f"[plaza-once] db_path={db_path}", flush=True)
    print(f"[plaza-once] city_filter={city_filter!r}", flush=True)
    print(f"[plaza-once] max_total_rent={max_rent!r}", flush=True)
    print(f"[plaza-once] send_existing_on_first_run={os.getenv('SEND_EXISTING_ON_FIRST_RUN', 'false')}", flush=True)
    # Never print token or chat_id

    # --- Run ---
    con = setup_db(db_path)

    try:
        discovered, matched, sent = check_once(con, token, chat_id, list_url)
    except Exception as exc:
        print(f"[plaza-once] FATAL: {exc!r}", file=sys.stderr, flush=True)
        return 1
    finally:
        con.close()

    print(
        f"[plaza-once] done — discovered_new={discovered} matched_filters={matched} sent={sent}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
