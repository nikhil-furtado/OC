#!/usr/bin/env python3
"""AssetBot (host deploy version)

Telegram bot for group-only asset downloads.
- Only responds in whitelisted group chat_ids
- Ignores DMs
- Trigger: any message containing Envato/Freepik links
- Downloads to /tmp, uploads back, deletes
- Rate limits and max file size

Env vars:
- TELEGRAM_BOT_TOKEN
- ALLOWED_CHAT_IDS (comma-separated)
- ENVATO_COOKIES_JSON
- FREEPIK_COOKIES_JSON (optional; Freepik wiring placeholder)
- MAX_FILE_MB (default 500)
- RATE_LIMIT_WINDOW_SEC (default 3600)
- RATE_LIMIT_MAX (default 10)
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Tuple

import requests
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ContextTypes, MessageHandler, filters

ENVATO_ITEM_RE = re.compile(r"https?://(?:www\.)?elements\.envato\.com/[^\s/]+-([A-Z0-9]{6,})", re.I)
FREEPIK_RE = re.compile(r"https?://(?:www\.)?freepik\.com/[^\s]+", re.I)


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name)
    if v is None:
        if default is None:
            raise RuntimeError(f"Missing env var: {name}")
        return default
    return v


def parse_allowed_chat_ids() -> set[int]:
    raw = env("ALLOWED_CHAT_IDS")
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


@dataclass
class RateLimiter:
    window_sec: int
    max_hits: int
    hits: dict[tuple[int, int], list[float]]

    def __init__(self, window_sec: int, max_hits: int):
        self.window_sec = window_sec
        self.max_hits = max_hits
        self.hits = {}

    def allow(self, chat_id: int, user_id: int) -> bool:
        now = time.time()
        key = (chat_id, user_id)
        arr = [t for t in self.hits.get(key, []) if now - t < self.window_sec]
        if len(arr) >= self.max_hits:
            self.hits[key] = arr
            return False
        arr.append(now)
        self.hits[key] = arr
        return True


def load_cookiejar_from_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        cookies_raw = json.load(f)
    return {c["name"]: c["value"] for c in cookies_raw}


def envato_get_item_uuid(item_id: str) -> str:
    api = f"https://elements.envato.com/api/v1/items/{item_id}.json"
    r = requests.get(api, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["attributes"]["itemUuid"]


def envato_download(item_url: str, cookies_path: str, max_mb: int) -> Tuple[str, str]:
    m = ENVATO_ITEM_RE.search(item_url)
    if not m:
        raise ValueError("Could not extract Envato item id from URL")
    item_id = m.group(1).upper()
    item_uuid = envato_get_item_uuid(item_id)

    candidate_types = [
        "video-templates",
        "wordpress",
        "graphics",
        "presentation-templates",
        "fonts",
        "photos",
        "stock-video",
        "music",
        "sound-effects",
        "add-ons",
        "web-templates",
        "cms-templates",
        "3d",
    ]

    cookies = load_cookiejar_from_json(cookies_path)
    headers_base = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": "https://app.envato.com",
    }

    signed_url = None
    for item_type in candidate_types:
        headers = dict(headers_base)
        headers["Referer"] = f"https://app.envato.com/{item_type}/{item_uuid}"
        data = f"itemUuid={item_uuid}&itemType={item_type}"
        r = requests.post("https://app.envato.com/download.data", headers=headers, data=data, cookies=cookies, timeout=30)
if r.status_code != 200:
            continue
        m2 = re.search(r'"(https://[^\"]+envatousercontent\.com/[^\"]+)"', r.text)
        if m2:
            signed_url = m2.group(1)
            break

    if not signed_url:
        raise RuntimeError("Envato: could not obtain signed download URL (cookies expired?)")

    resp = requests.get(signed_url, stream=True, timeout=300)
    resp.raise_for_status()

    filename = f"envato-{item_id}.zip"
    out = f"/tmp/{filename}"
    total = 0
    limit = max_mb * 1024 * 1024

    with open(out, "wb") as f:
        for chunk in resp.iter_content(1024 * 256):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise RuntimeError(f"File too large (> {max_mb} MB).")
            f.write(chunk)

    return out, filename


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    chat = update.effective_chat

    if chat.type == ChatType.PRIVATE:
        return

    allowed: set[int] = context.bot_data["allowed_chat_ids"]
    if chat.id not in allowed:
        return

    user = update.effective_user
    if user:
        limiter: RateLimiter = context.bot_data["rate_limiter"]
        if not limiter.allow(chat.id, user.id):
            await update.message.reply_text("Rate limit hit. Try again later.")
            return

    text = update.message.text or ""
    max_mb = int(os.environ.get("MAX_FILE_MB", "500"))

    envato_m = ENVATO_ITEM_RE.search(text)
    freepik_m = FREEPIK_RE.search(text)

    try:
        if envato_m:
            cookies_path = env("ENVATO_COOKIES_JSON")
            file_path, filename = await asyncio.to_thread(envato_download, envato_m.group(0), cookies_path, max_mb)
            await update.message.reply_document(document=open(file_path, "rb"), filename=filename, caption=f"Envato: {filename}")
            try:
                os.remove(file_path)
            except OSError:
                pass
            return

        if freepik_m:
            await update.message.reply_text("Freepik support not wired yet (Envato is live).")
            return

    except Exception as e:
        await update.message.reply_text(f"Download failed: {str(e)[:200]}")


def main() -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    allowed = parse_allowed_chat_ids()

    window = int(os.environ.get("RATE_LIMIT_WINDOW_SEC", "3600"))
    max_hits = int(os.environ.get("RATE_LIMIT_MAX", "10"))

    app = Application.builder().token(token).build()
    app.bot_data["allowed_chat_ids"] = allowed
    app.bot_data["rate_limiter"] = RateLimiter(window, max_hits)

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
PY
