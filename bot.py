#!/usr/bin/env python3
import os
import re
import threading
import logging
from datetime import datetime

import requests as rq
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SECRET = os.getenv("SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise SystemExit("Нет BOT_TOKEN")
if not SECRET:
    raise SystemExit("Нет SECRET")

API_URL = {"url": os.getenv("API_URL", "").rstrip("/")}

WORDS_PER_LINE = 3
MENTION_PAGE = 10
PAGE_SIZE = 5


# ═══════════════════════════════════════════
#  HTTP API (FastAPI)
# ═══════════════════════════════════════════

api = FastAPI()


class SetAPI(BaseModel):
    url: str
    secret: str


@api.get("/")
@api.get("/ping")
def ping():
    return {"ok": True, "api_url": API_URL["url"]}


@api.post("/set-api")
def set_api(body: SetAPI):
    if body.secret != SECRET:
        log.warning("set-api: неверный secret")
        return {"ok": False, "error": "bad secret"}
    old = API_URL["url"]
    API_URL["url"] = body.url.rstrip("/")
    log.info(f"API_URL: {old} → {API_URL['url']}")
    return {"ok": True, "api_url": API_URL["url"]}


def run_api():
    uvicorn.run(api, host="0.0.0.0", port=PORT, log_level="warning")


# ═══════════════════════════════════════════
#  Хелперы
# ═══════════════════════════════════════════

def api_get(path, timeout=90):
    if not API_URL["url"]:
        log.warning("API_URL пустой")
        return None
    try:
        r = rq.get(f"{API_URL['url']}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"api error {path}: {e}")
        return None


def esc(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def fmt_date(ts):
    if not ts:
        return "?"
    try:
        months = ["янв", "фев", "мар", "апр", "мая", "июн",
                  "июл", "авг", "сен", "окт", "ноя", "дек"]
        d = datetime.fromtimestamp(ts)
        return f"{d.day} {months[d.month - 1]}"
    except Exception:
        return "?"


# ═══════════════════════════════════════════
#  Клавиатуры
# ═══════════════════════════════════════════

def main_kb(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Анализ", callback_data=f"an:menu:{uid}")]
    ])


def back_only_kb(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"an:menu:{uid}")]
    ])


def analysis_kb(uid):
    checks = [
        ("likes",        "8💠 Симпатии/вкусы"),
        ("gifts",        "1💠 Про подарки"),
        ("age",          "🙂 Возраст"),
        ("mentions_in",  "8💠 Кто упоминает"),
        ("mentions_out", "7💠 Кого упоминает"),
        ("other",        "8💠 Прочее"),
    ]
    rows = []
    for key, label in checks:
        rows.append([InlineKeyboardButton(label, callback_data=f"an:{key}:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"an:back:{uid}")])
    return InlineKeyboardMarkup(rows)


def paginated_kb(uid, action, page, pages):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"an:{action}:{uid}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}", callback_data=f"an:{action}:{uid}:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"an:{action}:{uid}:{page+1}"))
    nav.append(InlineKeyboardButton("⏭", callback_data=f"an:{action}:{uid}:{pages-1}"))
    back = [InlineKeyboardButton("⬅️ Назад", callback_data=f"an:menu:{uid}")]
    return InlineKeyboardMarkup([nav, back])


# ═══════════════════════════════════════════
#  Форматирование
# ═══════════════════════════════════════════

def fmt_words(uid, words, user, msg_count):
    fname = user.get("first_name") or f"[{uid}]"
    uname = (user.get("username") or "").lstrip("@")
    handle = f"(@{uname})" if uname else ""
    header = f"<b>{esc(fname)} {esc(handle)}</b> часто использует слова:".strip()

    buckets = {}
    for w in words:
        wrd, n = w[0], w[1]
        buckets.setdefault(n, []).append(wrd)

    lines = []
    for n in sorted(buckets.keys(), reverse=True):
        ws = buckets[n][:WORDS_PER_LINE]
        lines.append(f"├ {n} - {n} {', '.join(esc(x) for x in ws)}")

    body = ("<code>" + "\n".join(lines) + "</code>") if lines else ""
    settings = f"[Настройки. Изменить - /words\n{uid} 1000 3 ]"
    summary = (
        f"Сообщений: {msg_count}\n"
        f"1. Исключая: 0 банальных слов\n"
        f"2. Слов печатать: {WORDS_PER_LINE}"
    )
    if body:
        return f"{header}\n\n{body}\n\n<code>{esc(settings)}</code>\n<code>{esc(summary)}</code>"
    return f"{header}\n\n<code>{esc(settings)}</code>\n<code>{esc(summary)}</code>"


def fmt_age(msgs, page=0):
    if not msgs:
        return "🙂 <b>Сообщения, похожие на возраст:</b>\n\n❌ нет данных", None
    pages = (len(msgs) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = msgs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    lines = ["🙂 <b>Сообщения, похожие на возраст:</b>"]
    for row in chunk:
        ts, text = row[0], row[1]
        lines.append(f"[{fmt_date(ts)}]: {esc((text or '')[:200])}")
    return "\n".join(lines), (page, pages)


def fmt_mentions(msgs, page, title):
    """msgs: [[chat_id, msg_id, ts, text, chat_title, chat_username], ...]"""
    if not msgs:
        return f"{title}\n\n❌ нет данных", None
    pages = (len(msgs) + MENTION_PAGE - 1) // MENTION_PAGE
    page = max(0, min(page, pages - 1))
    chunk = msgs[page * MENTION_PAGE:(page + 1) * MENTION_PAGE]
    lines = [title]
    for row in chunk:
        chat_id = str(row[0]) if len(row) > 0 else ""
        msg_id = row[1] if len(row) > 1 else 0
        ts = row[2] if len(row) > 2 else None
        text = row[3] if len(row) > 3 else ""
        chat_title = row[4] if len(row) > 4 and row[4] else "?"
        chat_username = row[5] if len(row) > 5 and row[5] else ""

        title_short = str(chat_title)[:15]

        if chat_username:
            chat_link = f"https://t.me/{chat_username}"
            msg_link = f"https://t.me/{chat_username}/{msg_id}"
        else:
            cid = chat_id
            if cid.startswith("-100"):
                cid = cid[4:]
            elif cid.startswith("-"):
                cid = cid[1:]
            chat_link = f"https://t.me/c/{cid}"
            msg_link = f"https://t.me/c/{cid}/{msg_id}"

        line = (
            f'[<a href="{chat_link}">{esc(title_short)}</a>] '
            f'[<a href="{msg_link}">{fmt_date(ts)}</a>] '
            f'> {esc((text or "")[:200])}'
        )
        lines.append(line)
    return "\n".join(lines), (page, pages)


# ═══════════════════════════════════════════
#  Handlers
# ═══════════════════════════════════════════

async def on_text(update, ctx):
    text = (update.message.text or "").strip()
    if not re.fullmatch(r"\d{5,15}", text):
        return

    uid = text
    words = api_get(f"/words/{uid}") or []
    user = api_get(f"/user/{uid}") or {}
    mc = (api_get(f"/msg_count/{uid}") or {}).get("count", 0)

    out = fmt_words(uid, words, user, mc)
    try:
        await update.message.reply_text(
            out,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=main_kb(uid),
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def on_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":")
    if len(parts) < 3 or parts[0] != "an":
        return
    action, uid = parts[1], parts[2]
    page = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0

    if action == "back":
        try:
            await q.edit_message_reply_markup(reply_markup=main_kb(uid))
        except Exception:
            pass
        return

    if action == "menu":
        try:
            await q.edit_message_reply_markup(reply_markup=analysis_kb(uid))
        except Exception:
            pass
        return

    if action == "age":
        msgs = api_get(f"/age/{uid}") or []
        text, pp = fmt_age(msgs, page)
        kb = paginated_kb(uid, "age", *pp) if pp else back_only_kb(uid)
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=kb
            )
        except Exception:
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "mentions_in":
        msgs = api_get(f"/mentions_in/{uid}") or []
        text, pp = fmt_mentions(msgs, page, "8💠 <b>Кто упоминает:</b>")
        kb = paginated_kb(uid, "mentions_in", *pp) if pp else back_only_kb(uid)
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=kb
            )
        except Exception:
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == "mentions_out":
        msgs = api_get(f"/mentions_out/{uid}") or []
        text, pp = fmt_mentions(msgs, page, "7💠 <b>Кого упоминает:</b>")
        kb = paginated_kb(uid, "mentions_out", *pp) if pp else back_only_kb(uid)
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=kb
            )
        except Exception:
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    label = {"likes": "Симпатии/вкусы", "gifts": "Про подарки", "other": "Прочее"}.get(action, action)
    await q.message.reply_text(f"❌ <b>{esc(label)}</b> — нет данных", parse_mode=ParseMode.HTML)


def run_bot():
    app_tg = Application.builder().token(BOT_TOKEN).build()
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app_tg.add_handler(CallbackQueryHandler(on_callback))
    log.info("Bot started")
    app_tg.run_polling()


if __name__ == "__main__":
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
    run_bot()
