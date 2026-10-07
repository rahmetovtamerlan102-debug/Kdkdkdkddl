#!/usr/bin/env python3
import os
import re
import time
import asyncio
import threading
import logging
from datetime import datetime

import requests as rq
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, KeyboardButtonRequestUsers, ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler, CommandHandler, filters
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

API_URL = {"url": os.getenv("API_URL", "").rstrip("/"), "ts": 0}

WORDS_PER_LINE = 3
MENTION_PAGE = 10
PAGE_SIZE = 5
NAMES_SHOW = 10
GIFTS_PER_PAGE = 8
CHATS_PER_PAGE = 10
COMMON_PER_PAGE = 20
ACQ_LIMIT = 15
SEARCH_LIMIT = 20

NO_DATA = "К сожалению, ничегο н℮ найдено."
MENU_TEXT = "❌ - нeт дαнных. ✅ - ℮cть дαнные."

START_TEXT = """ㅤБот для любви и ποиckα друзeй.

Для πρocмοтρα инφоρмαции отπрαвь:
- @username или id ποльзoвαтeля
- kοнтαқт или выбρать из н℮дαвниx кноπkoй
- cтиқеρ (поcмотρеть coздαт℮ля cтиkeρпαқа)
- @username или id грγππы или кαналα
- ccылкγ на cοοбщ℮ние в грγпп℮
- πeρeслать coοбщени℮ из гρуπпы
- cсылкγ нα гργππу или ID грγππы - стαтиcтика чата
- πeρ℮cлαть πоcт c қαнαлα - πoсмοтρeть ктo πерecылαл этoт πост в чаты

Πрочe℮:
- қаталoг чαтοв /topchat
- Ποиск чатοв /search
- Пoиск πο именαм /human

Этο зαқонно?
- /pd_msg"""

PD_TEXT = """⚖️ <b>Законно ли это?</b>

Бот работает <b>только с публичными данными</b>, которые Telegram
отдаёт любому пользователю:

• <b>Ники и ID</b> — открытая информация
• <b>Названия чатов и групп</b> — видны всем
• <b>Количество сообщений</b> — не приватные данные
• <b>Публичные сообщения</b> — доступны любому в чате
• <b>Подарки в публичных профилях</b> — видны всем

<b>Что бот НЕ делает:</b>
• Не читает чужие личные сообщения
• Не взламывает аккаунты
• Не получает доступ к закрытым чатам
• Не хранит пароли и приватные данные

<b>Правовая база:</b>
В большинстве стран анализ публично доступной информации
не нарушает закон. Но конкретика зависит от юрисдикции —
в РФ, КЗ, ЕС свои нюансы.

<b>Что важно помнить:</b>
• Не используй бота для травли и преследования
• Не публикуй чужие данные без согласия
• Уважай приватность — то что технически доступно,
  не всегда этично использовать"""


# ═══════════════════════════════════════════
#  HTTP API
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
    API_URL["ts"] = time.time()
    log.info(f"API_URL: {old} → {API_URL['url']}")
    return {"ok": True, "api_url": API_URL["url"]}


def run_api():
    uvicorn.run(api, host="0.0.0.0", port=PORT, log_level="warning")


# ═══════════════════════════════════════════
#  API-клиент
# ═══════════════════════════════════════════

RENDER_PING = "https://kdkdkdkddl-50hy.onrender.com/ping"
URL_TTL = 60
URL_LOCK = threading.Lock()


def _fetch_api_url(force=False):
    now = time.time()
    with URL_LOCK:
        if not force and API_URL["url"] and (now - API_URL.get("ts", 0)) < URL_TTL:
            return API_URL["url"]
    try:
        r = rq.get(RENDER_PING, timeout=10)
        data = r.json()
        new_url = (data.get("api_url") or "").rstrip("/")
        if new_url:
            with URL_LOCK:
                if new_url != API_URL["url"]:
                    log.info(f"API_URL обновлён: {API_URL['url']} → {new_url}")
                API_URL["url"] = new_url
                API_URL["ts"] = now
            return new_url
        log.warning("Render /ping вернул пустой api_url")
        return ""
    except Exception as e:
        log.error(f"не смог получить api_url с Render: {e}")
        return ""


def api_get(path, timeout=90):
    for attempt in range(2):
        url = _fetch_api_url(force=(attempt > 0))
        if not url:
            return None
        try:
            r = rq.get(f"{url}{path}", timeout=timeout)
            r.raise_for_status()
            return r.json()
        except rq.exceptions.HTTPError as e:
            log.error(f"api HTTP error {path}: {e}")
            return None
        except Exception as e:
            log.error(f"api error {path} (попытка {attempt+1}): {e}")
            with URL_LOCK:
                API_URL["ts"] = 0
            continue
    return None


def has_data(path):
    try:
        d = api_get(path, timeout=15)
        if isinstance(d, list):
            return len(d) > 0
        if isinstance(d, dict):
            return bool(d.get("count")) or bool(d)
        return bool(d)
    except Exception:
        return False


# ═══════════════════════════════════════════
#  Хелперы
# ═══════════════════════════════════════════

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def user_link(username=None, user_id=None, text=None):
    label = esc(text) if text is not None else None
    if username:
        u = str(username).strip().lstrip("@")
        if u:
            if label is None:
                label = f"@{esc(u)}"
            return f'<a href="https://t.me/{esc(u)}">{label}</a>'
    if user_id:
        uid_str = str(user_id).strip()
        if uid_str.isdigit():
            if label is None:
                label = f"ID:{esc(uid_str)}"
            return f'<a href="tg://openmessage?user_id={esc(uid_str)}">{label}</a>'
    return None


def fmt_date(ts):
    if not ts:
        return "?"
    try:
        months = ["янв", "фев", "мар", "апр", "мая", "июн",
                  "июл", "авг", "сен", "окт", "ноя", "дек"]
        d = datetime.fromtimestamp(ts)
        return f"{d.day} {months[d.month-1]}"
    except Exception:
        return "?"


def fmt_dt(s):
    if not s:
        return "?"
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        return d.strftime("%d.%m.%Y")
    except Exception:
        return s[:10]


async def safe_send(q, text, kb=None):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=kb)
        return
    except Exception as e:
        log.warning(f"edit failed: {e}")
    try:
        await q.message.delete()
    except Exception:
        pass
    try:
        await q.message.reply_text(text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=kb)
    except Exception as e:
        log.error(f"reply failed: {e}")


# ═══════════════════════════════════════════
#  Reply-клавиатура
# ═══════════════════════════════════════════

def search_user_kb():
    btn = KeyboardButton(
        "👤 Выбрать пользователя",
        request_users=KeyboardButtonRequestUsers(
            request_id=1, user_is_bot=False, max_quantity=1,
            request_username=True, request_name=True,
        )
    )
    return ReplyKeyboardMarkup(
        [[btn]], resize_keyboard=True, is_persistent=True,
        input_field_placeholder="ID, @username или ссылка",
    )


# ═══════════════════════════════════════════
#  Inline-клавиатуры
# ═══════════════════════════════════════════

def card_kb(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Профиль", callback_data=f"an:card:{uid}"),
         InlineKeyboardButton("💬 Частота слов", callback_data=f"an:words:{uid}")],
        [InlineKeyboardButton("👥 Группы", callback_data=f"an:chats:{uid}"),
         InlineKeyboardButton("🎁 Подарки", callback_data=f"an:gifts:{uid}")],
        [InlineKeyboardButton("👥 Общие", callback_data=f"an:common:{uid}"),
         InlineKeyboardButton("👤 Знакомые", callback_data=f"an:replies:{uid}")],
        [InlineKeyboardButton("🔍 Анализ", callback_data=f"an:menu:{uid}")],
    ])


def words_kb(uid):
    return card_kb(uid)


def gifts_kb(uid, page, pages):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"an:gifts:{uid}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}", callback_data=f"an:gifts:{uid}:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"an:gifts:{uid}:{page+1}"))
    return InlineKeyboardMarkup([nav, [InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")]])


def gifts_no_pages_kb(uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")]])


def chats_kb(uid, page, pages):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"an:chats:{uid}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"an:chats:{uid}:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"an:chats:{uid}:{page+1}"))
    return InlineKeyboardMarkup([nav, [InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")]])


def chats_no_pages_kb(uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")]])


def common_kb(uid, page, pages):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"an:common:{uid}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}", callback_data=f"an:common:{uid}:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"an:common:{uid}:{page+1}"))
    return InlineKeyboardMarkup([nav, [InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")]])


def common_no_pages_kb(uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")]])


def replies_kb(uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")]])


def back_only_kb(uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"an:menu_static:{uid}")]])


def channel_kb(channel_id, username):
    rows = []
    if username:
        rows.append([InlineKeyboardButton("📊 Стата чата",
            callback_data=f"an:chatstat:{channel_id}:{username}")])
        rows.append([InlineKeyboardButton("📝 Описание (история)",
            callback_data=f"an:chatdesc:{channel_id}:{username}")])
        rows.append([InlineKeyboardButton("🔗 Все пересылки",
            callback_data=f"an:chatforwards:{channel_id}:{username}")])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════
#  АНАЛИЗ
# ═══════════════════════════════════════════

ANALYSIS_STEPS = [
    ("likes", "Симпатии/вкусы"),
    ("gift_words", "Про подарки"),
    ("age", "Возраст"),
    ("mentions_in", "Кто упоминает/@тегает"),
    ("mentions_out", "Кого упоминает/@тегает"),
    ("other", "Прочее"),
]


def analysis_check(key, uid):
    if key == "age":
        return has_data(f"/age/{uid}")
    if key == "mentions_in":
        return has_data(f"/mentions_in/{uid}")
    if key == "mentions_out":
        return has_data(f"/mentions_out/{uid}")
    if key == "likes":
        return has_data(f"/age/{uid}")
    if key == "gift_words":
        return has_data(f"/gift_words/{uid}")
    return False


def analysis_kb_partial(uid, upto):
    rows = []
    for i, (key, label) in enumerate(ANALYSIS_STEPS):
        if i < upto:
            has = analysis_check(key, uid)
            mark = "✅" if has else "❌"
            rows.append([InlineKeyboardButton(f"{label} {mark}", callback_data=f"an:{key}:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")])
    return InlineKeyboardMarkup(rows)


def analysis_progress_text(done, total):
    return f"⚙️ Анализ сообщений {done}/{total}, подождите"


def analysis_kb_static(uid):
    rows = []
    for key, label in ANALYSIS_STEPS:
        has = analysis_check(key, uid)
        mark = "✅" if has else "❌"
        rows.append([InlineKeyboardButton(f"{label} {mark}", callback_data=f"an:{key}:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"an:card:{uid}")])
    return InlineKeyboardMarkup(rows)


def paginated_kb(uid, action, page, pages):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"an:{action}:{uid}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}", callback_data=f"an:{action}:{uid}:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"an:{action}:{uid}:{page+1}"))
    nav.append(InlineKeyboardButton("⏭", callback_data=f"an:{action}:{uid}:{pages-1}"))
    return InlineKeyboardMarkup([nav, [InlineKeyboardButton("⬅️ Назад", callback_data=f"an:menu_static:{uid}")]])


# ═══════════════════════════════════════════
#  Форматирование
# ═══════════════════════════════════════════

def fmt_card(card, nicks, uid):
    if not card or not card.get("exists"):
        return "👤 <b>Пользователь не найден в БД.</b>"
    uname = card.get("username") or ""
    fname = card.get("first_name") or ""
    if card.get("no_messages"):
        head = f"ㅤЭтo {esc(fname or uid)}, id={esc(str(uid))}"
        lines = [f"<b>{head}</b>", "//", "⚠️ Нет сообщений, статистики нет",
                 f"3αмeчeн в чатαx: {card.get('chats_seen', 0)}", "",
                 f"ΙＤ: <code>{esc(str(uid))}</code>"]
        us = (nicks or {}).get("usernames") or []
        if us:
            lines.append("ᴜѕеrηamеѕ:")
            links = []
            for u in us:
                lnk = user_link(username=u.get("name"), user_id=uid)
                links.append(lnk if lnk else f"@{esc(u['name'])}")
            lines.append(" | ".join(links))
        return "\n".join(lines)

    if uname:
        _u = uname.lstrip("@")
        head = f'ㅤ<a href="https://t.me/{esc(_u)}">{esc(fname)}</a> (<a href="https://t.me/{esc(_u)}">@{esc(_u)}</a>)'
    elif fname:
        head = f'ㅤ<a href="tg://openmessage?user_id={esc(str(uid))}">{esc(fname)}</a>'
    else:
        head = f'ㅤ<a href="tg://openmessage?user_id={esc(str(uid))}">{esc(str(uid))}</a>'
    head = head.strip()

    lines = [f"<b>{head}</b>",
             f"Разноoбрαзиe соoбщ. {card['raznoobrazie_pct']}%",
             f"С {card['first_date']} пο {card['last_date']}",
             f"{card['total_msgs']} сοoбщ℮ний в {card['total_chats']} чатαх",
             f"{card['replies_pct']}% ρ℮плαи {card['media_pct']}% медиα",
             "Kружки: 0, гoлоc: 0"]
    if card.get("fav_chat"):
        lines.append(f"Любимый чαт: {esc(card['fav_chat'])}")
    lines.append("")
    lines.append(f"ΙＤ: <code>{esc(str(uid))}</code>")
    us = (nicks or {}).get("usernames") or []
    if us:
        lines.append("ᴜѕеrηamеѕ:")
        links = []
        for u in us:
            lnk = user_link(username=u.get("name"), user_id=uid)
            links.append(lnk if lnk else f"@{esc(u['name'])}")
        lines.append(" | ".join(links))
    fns = (nicks or {}).get("first_names") or []
    if fns:
        n = min(len(fns), NAMES_SHOW)
        lines.append(f"Имена: ({n} из {len(fns)})")
        for f in fns[-NAMES_SHOW:]:
            d = fmt_dt(f.get("first") or f.get("last"))
            lines.append(f"├ {d}  ➜  {esc(f['name'])}")
    return "\n".join(lines)


def fmt_chats(data, page, uid):
    card = api_get(f"/card/{uid}") or {}
    uname = (card.get("username") or "").lstrip("@")
    fname = card.get("first_name") or uname or uid
    handle = f"(@{uname})" if uname else ""
    head = f"ㅤ{esc(fname)} {esc(handle)}".strip()
    title_line = f"ㅤГρуππы αкқаγнтα {uid}."
    if not data or not isinstance(data, list) or not data:
        return f"<b>{head}</b>\n\n{esc(title_line)}\n\n{NO_DATA}", None
    total = len(data)
    pages = (total + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE
    page = max(0, min(page, pages - 1))
    chunk = data[page*CHATS_PER_PAGE:(page+1)*CHATS_PER_PAGE]
    lines = [f"<b>{head}</b>", "", esc(title_line), ""]
    for c in chunk:
        title = str(c.get("title") or "?")[:25]
        uname_c = (c.get("username") or "").strip()
        msgs = c.get("msgs") or 0
        suffix = f" ({msgs} сообщ.)" if msgs > 0 else ""
        if uname_c:
            lines.append(f'├ <a href="https://t.me/{esc(uname_c)}">{esc(title)}</a>{suffix}')
        else:
            lines.append(f"├ {esc(title)}{suffix}")
    lines.append("")
    lines.append(f"Всего: {total}")
    return "\n".join(lines), (page, pages)


def fmt_common(data, page, uid):
    card = api_get(f"/card/{uid}") or {}
    uname = (card.get("username") or "").lstrip("@")
    fname = card.get("first_name") or uname or uid
    handle = f"(@{uname})" if uname else ""
    title_name = f"{fname} {handle}".strip()
    lines = [
        f"ㅤΠoльзοвαтели им℮ющие нαибольше числο oбщиx чαтов с  {esc(title_name)}",
        "❌ - αққаγнт удал℮н.", "(kто) - (числo общиx чαтοв)", "",
    ]
    if not data or not isinstance(data, list) or not data:
        lines.append(NO_DATA)
        return "\n".join(lines), None
    total = len(data)
    pages = (total + COMMON_PER_PAGE - 1) // COMMON_PER_PAGE
    page = max(0, min(page, pages - 1))
    chunk = data[page*COMMON_PER_PAGE:(page+1)*COMMON_PER_PAGE]
    for i, item in enumerate(chunk):
        idx = page * COMMON_PER_PAGE + i + 1
        name = item.get("display") or "?"
        uname_i = (item.get("username") or "").strip().lstrip("@")
        user_id_i = item.get("user_id") or item.get("id") or item.get("uid")
        cnt = item.get("count") or 0
        deleted = item.get("deleted")
        lnk = user_link(username=uname_i, user_id=user_id_i, text=name)
        name_html = lnk if lnk else esc(name)
        line = f"{idx}. {name_html} — {cnt}"
        if deleted:
            line += "❌"
        lines.append(line)
    lines.append("")
    lines.append(f"Всего {total}, страница {page+1} из {pages}")
    return "\n".join(lines), (page, pages)


def fmt_replies(data, uid):
    card = api_get(f"/card/{uid}") or {}
    uname = (card.get("username") or "").lstrip("@")
    fname = card.get("first_name") or uname or uid
    handle = f"(@{uname})" if uname else ""
    head = f"ㅤ{esc(fname)} {esc(handle)}".strip()
    lines = [f"<b>{head}</b>", "Цитирует(отвечает) им:",
             "├ когда - кому (всего раз)", ""]
    if not data or not isinstance(data, list) or not data:
        lines.append(NO_DATA)
        return "\n".join(lines)
    chunk = data[:ACQ_LIMIT]
    for i, item in enumerate(chunk):
        date = item.get("date") or ""
        label = item.get("display") or "?"
        cnt = item.get("count") or 0
        item_uname = (item.get("username") or "").strip().lstrip("@")
        item_uid = item.get("user_id") or item.get("id") or item.get("uid")
        prefix = "└" if i == len(chunk) - 1 else "├"
        href = None
        if item_uname:
            href = f"https://t.me/{esc(item_uname)}"
        elif item_uid and str(item_uid).isdigit():
            href = f"tg://openmessage?user_id={esc(str(item_uid))}"
        _dp = f"{esc(date)} - " if date else ""
        if href:
            line = f'{prefix} <a href="{href}">{_dp}{esc(label)} ({cnt})</a>'
        else:
            line = f"{prefix} {_dp}{esc(label)} ({cnt})"
        lines.append(line)
    return "\n".join(lines)


def fmt_gifts(data, page, uid):
    card = api_get(f"/card/{uid}") or {}
    uname = (card.get("username") or "").lstrip("@")
    fname = card.get("first_name") or uname or uid
    handle = f"(@{uname})" if uname else ""
    head = f"ㅤ{esc(fname)} {esc(handle)}".strip()
    lines = [f"<b>{head}</b>", "🎁 <b>Подарки</b>", ""]
    if not data or not data.get("ok"):
        err = (data or {}).get("error", "")
        lines.append(NO_DATA)
        if err:
            lines.append("")
            lines.append(f"<i>{esc(err)}</i>")
        return "\n".join(lines), None
    items = data.get("gifts") or []
    total = data.get("count", 0)
    stars = data.get("total_stars", 0)
    if not items:
        lines.append(NO_DATA)
        return "\n".join(lines), None
    lines.append(f"Всего: {total} | Сумма: {stars}⭐")
    lines.append("")
    pages = (total + GIFTS_PER_PAGE - 1) // GIFTS_PER_PAGE
    page = max(0, min(page, pages - 1))
    chunk = items[page*GIFTS_PER_PAGE:(page+1)*GIFTS_PER_PAGE]
    for i, g in enumerate(chunk):
        emoji = g.get("emoji") or "🎁"
        name = g.get("name") or "Подарок"
        gstars = g.get("stars") or 0
        date = g.get("date") or "?"
        from_u = (g.get("from_username") or "").strip()
        from_n = (g.get("from_name") or "").strip()
        from_id = str(g.get("from_id") or "").strip()
        lnk = user_link(username=from_u, user_id=from_id)
        if lnk:
            from_str = lnk
        elif from_n:
            from_str = esc(from_n)
        elif from_id:
            from_str = esc(from_id)
        else:
            from_str = "?"
        prefix = "└" if (page == pages - 1 and i == len(chunk) - 1) else "├"
        lines.append(f"{prefix} {esc(date)} · {emoji} {esc(name)} · {gstars}⭐ · {from_str}")
    return "\n".join(lines), (page, pages)


def fmt_words(uid, words, user, msg_count):
    fname = user.get("first_name") or f"[{uid}]"
    uname = (user.get("username") or "").lstrip("@")
    handle = f"(@{uname})" if uname else ""
    header = f"<b>{esc(fname)} {esc(handle)}</b> часто использует слова:".strip()
    buckets = {}
    for w in words:
        buckets.setdefault(w[1], []).append(w[0])
    lines = []
    for n in sorted(buckets.keys(), reverse=True):
        ws = buckets[n][:WORDS_PER_LINE]
        lines.append(f"├ {n} - {n} {', '.join(esc(x) for x in ws)}")
    body = ("<code>" + "\n".join(lines) + "</code>") if lines else ""
    settings = f"[Настройки. Изменить - /words\n{uid} 1000 3 ]"
    summary = f"Сообщений: {msg_count}\n1. Исключая: 0 банальных слов\n2. Слов печатать: {WORDS_PER_LINE}"
    if body:
        return f"{header}\n\n{body}\n\n<code>{esc(settings)}</code>\n<code>{esc(summary)}</code>"
    return f"{header}\n\n<code>{esc(settings)}</code>\n<code>{esc(summary)}</code>"


def fmt_age_body(msgs, page=0):
    if not msgs:
        return None, None
    pages = (len(msgs) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = msgs[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
    lines = [f"[{fmt_date(r[0])}]: {esc((r[1] or '')[:200])}" for r in chunk]
    return "\n".join(lines), (page, pages)


def fmt_mentions_body(msgs, page):
    if not msgs:
        return None, None
    pages = (len(msgs) + MENTION_PAGE - 1) // MENTION_PAGE
    page = max(0, min(page, pages - 1))
    chunk = msgs[page*MENTION_PAGE:(page+1)*MENTION_PAGE]
    lines = []
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
            if cid.startswith("-100"): cid = cid[4:]
            elif cid.startswith("-"): cid = cid[1:]
            chat_link = f"https://t.me/c/{cid}"
            msg_link = f"https://t.me/c/{cid}/{msg_id}"
        lines.append(
            f'[<a href="{chat_link}">{esc(title_short)}</a>] '
            f'[<a href="{msg_link}">{fmt_date(ts)}</a>] '
            f'> {esc((text or "")[:200])}'
        )
    return "\n".join(lines), (page, pages)


def fmt_channel_info(info, username):
    if not info or not info.get("ok"):
        err = (info or {}).get("error", "неизвестная ошибка")
        return f"❌ <b>Не удалось получить инфу</b>\n<code>{esc(err)}</code>"

    kind = "канал" if info.get("is_channel") else ("группа" if info.get("is_group") else "чат")
    uname = info.get("username") or username
    lines = [
        f'Это {kind} : <a href="https://t.me/{esc(uname)}">@{esc(uname)}</a>',
        "",
        f"<b>ID:</b> <code>{esc(info['id'])}</code>",
        "",
    ]

    cid = info.get("creator_id")
    cu = info.get("creator_username")
    cf = info.get("creator_first") or ""
    if cu:
        owner = f'<a href="https://t.me/{esc(cu)}">@{esc(cu)}</a>'
        if cf:
            owner = f'{esc(cf)} {owner}'
    elif cid:
        owner = f'<a href="tg://openmessage?user_id={esc(cid)}">ID: {esc(cid)}</a>'
    else:
        owner = "?"
    lines.append(f"<b>Владелец:</b> {owner}")
    lines.append("")
    lines.append("<b>Владелец (в прошлом):</b> ?")
    lines.append("")
    lines.append("<b>Описание:</b>")
    lines.append("")
    about = info.get("about") or ""
    if about:
        lines.append(esc(about[:1500]))
    else:
        lines.append("--- --- --- --- --- ---")
    lines.append("")
    lines.append("--- --- --- --- --- ---")
    return "\n".join(lines)


# ═══════════════════════════════════════════
#  SEARCH / TOP / HUMAN
# ═══════════════════════════════════════════

def fmt_search_chats(items, q=""):
    if not items:
        return NO_DATA, None
    lines = [f"🔎 <b>Поиск чатов</b>: «{esc(q)}»", ""]
    rows = []
    row = []
    for i, c in enumerate(items, 1):
        title = esc(str(c.get("title") or "?")[:30])
        uname = (c.get("username") or "").strip().lstrip("@")
        msgs = c.get("msgs") or 0
        link = f'<a href="https://t.me/{esc(uname)}">{title}</a>' if uname else title
        lines.append(f"{i}. {link} · {msgs} сообщ.")
        if uname:
            row.append(InlineKeyboardButton(
                f"📊 {title[:20]}",
                callback_data=f"an:chinfo:0:{uname}"))
            if len(row) == 2:
                rows.append(row); row = []
    if row:
        rows.append(row)
    return "\n".join(lines), (InlineKeyboardMarkup(rows) if rows else None)


def fmt_top_chats(items):
    if not items:
        return NO_DATA
    lines = ["🏆 <b>Топ чатов</b>", ""]
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    for i, c in enumerate(items):
        title = esc(str(c.get("title") or "?")[:30])
        uname = (c.get("username") or "").strip().lstrip("@")
        msgs = c.get("msgs") or 0
        link = f'<a href="https://t.me/{esc(uname)}">{title}</a>' if uname else title
        medal = medals.get(i, f"{i+1}.")
        lines.append(f"{medal} {link} · {msgs}")
    return "\n".join(lines)


def fmt_search_users(items, q=""):
    if not items:
        return NO_DATA, None
    lines = [f"👤 <b>Поиск по именам</b>: «{esc(q)}»", ""]
    rows = []
    row = []
    for i, u in enumerate(items, 1):
        name = esc(str(u.get("first_name") or "?"))[:25]
        uname = (u.get("username") or "").strip().lstrip("@")
        uid = u.get("user_id") or ""
        msgs = u.get("msgs") or 0
        if uname:
            link = (f'<a href="https://t.me/{esc(uname)}">{name}</a> '
                    f'<a href="https://t.me/{esc(uname)}">@{esc(uname)}</a>')
        else:
            link = f'<a href="tg://openmessage?user_id={esc(str(uid))}">{name}</a>'
        lines.append(f"{i}. {link} · {msgs}")
        if uid:
            row.append(InlineKeyboardButton(
                f"👤 {name[:18]}",
                callback_data=f"an:usercard:{uid}"))
            if len(row) == 2:
                rows.append(row); row = []
    if row:
        rows.append(row)
    return "\n".join(lines), (InlineKeyboardMarkup(rows) if rows else None)


async def do_search_chats(update, ctx, q):
    msg = await update.message.reply_text(f"🔎 Ищу чаты: «{esc(q)}»…", parse_mode=ParseMode.HTML)
    data = api_get(f"/search_chats?q={rq.utils.quote(q)}&limit={SEARCH_LIMIT}") or []
    text, kb = fmt_search_chats(data, q)
    await msg.edit_text(text, parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True, reply_markup=kb)


async def do_search_users(update, ctx, q):
    msg = await update.message.reply_text(f"👤 Ищу людей: «{esc(q)}»…", parse_mode=ParseMode.HTML)
    data = api_get(f"/search_users?q={rq.utils.quote(q)}&limit={SEARCH_LIMIT}") or []
    text, kb = fmt_search_users(data, q)
    await msg.edit_text(text, parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True, reply_markup=kb)


# ═══════════════════════════════════════════
#  Handlers
# ═══════════════════════════════════════════

async def cmd_start(update, ctx):
    await update.message.reply_text(
        START_TEXT, parse_mode=ParseMode.HTML,
        disable_web_page_preview=True, reply_markup=search_user_kb())


async def cmd_pd(update, ctx):
    await update.message.reply_text(
        PD_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_topchat(update, ctx):
    msg = await update.message.reply_text("🏆 Собираю топ чатов…")
    data = api_get(f"/top_chats?limit={SEARCH_LIMIT}") or []
    await msg.edit_text(fmt_top_chats(data), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True, reply_markup=search_user_kb())


async def cmd_search(update, ctx):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await do_search_chats(update, ctx, parts[1].strip())
    else:
        ctx.user_data["await"] = "search"
        await update.message.reply_text(
            "🔎 Что искать в названиях чатов?\nОтправь слово или фразу.",
            reply_markup=ReplyKeyboardRemove())


async def cmd_human(update, ctx):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await do_search_users(update, ctx, parts[1].strip())
    else:
        ctx.user_data["await"] = "human"
        await update.message.reply_text(
            "👤 Какое имя или @ник искать?",
            reply_markup=ReplyKeyboardRemove())


async def on_users_shared(update, ctx):
    shared = update.message.users_shared
    if not shared or not shared.users:
        return
    u = shared.users[0]
    uid = str(u.user_id)
    await update.message.reply_text(
        f"👤 Ищу <code>{esc(uid)}</code>…",
        parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
    card = api_get(f"/card/{uid}") or {}
    nicks = api_get(f"/nicks/{uid}") or {}
    out = fmt_card(card, nicks, uid)
    await update.message.reply_text(out, parse_mode=ParseMode.HTML,
        disable_web_page_preview=True, reply_markup=card_kb(uid))


async def on_text(update, ctx):
    text = (update.message.text or "").strip()

    await_what = ctx.user_data.pop("await", None)
    if await_what == "search":
        await do_search_chats(update, ctx, text); return
    if await_what == "human":
        await do_search_users(update, ctx, text); return

    # @username → канал/группа/юзер
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", text):
        uname = text.lstrip("@")
        msg = await update.message.reply_text(
            f"🔎 Ищу <code>@{esc(uname)}</code>…", parse_mode=ParseMode.HTML)
        info = api_get(f"/channel_info/{uname}", timeout=30)
        if info and info.get("ok"):
            if info.get("is_user"):
                uid = info.get("id")
                card = api_get(f"/card/{uid}") or {}
                nicks = api_get(f"/nicks/{uid}") or {}
                if card.get("exists"):
                    await msg.edit_text(
                        fmt_card(card, nicks, uid),
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=card_kb(uid))
                else:
                    fname = info.get("first_name") or uname
                    link = user_link(username=info.get("username"),
                                     user_id=uid, text=fname)
                    await msg.edit_text(
                        f"👤 <b>{link}</b>\n\n"
                        f"🆔 <code>{esc(uid)}</code>\n\n"
                        f"<i>Нет в БД — не писал в спарсенных чатах.</i>",
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=card_kb(uid))
                return
            await msg.edit_text(
                fmt_channel_info(info, uname), parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=channel_kb(info["id"], info.get("username") or uname))
            return
        await msg.edit_text(
            f"❌ Не найдено: <code>{esc((info or {}).get('error', '?'))}</code>",
            parse_mode=ParseMode.HTML)
        return

    # ID
    if not re.fullmatch(r"\d{5,15}", text):
        return
    uid = text
    card = api_get(f"/card/{uid}") or {}
    nicks = api_get(f"/nicks/{uid}") or {}
    out = fmt_card(card, nicks, uid)
    try:
        await update.message.reply_text(out, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=card_kb(uid))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def on_callback(update, ctx):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    parts = (q.data or "").split(":")
    if len(parts) < 3 or parts[0] != "an":
        return
    action, uid = parts[1], parts[2]
    page = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0

    if action == "card":
        card = api_get(f"/card/{uid}") or {}
        nicks = api_get(f"/nicks/{uid}") or {}
        await safe_send(q, fmt_card(card, nicks, uid), card_kb(uid))
        return

    if action == "usercard":
        card = api_get(f"/card/{uid}") or {}
        nicks = api_get(f"/nicks/{uid}") or {}
        await safe_send(q, fmt_card(card, nicks, uid), card_kb(uid))
        return

    if action == "words":
        mc = (api_get(f"/msg_count/{uid}") or {}).get("count", 0)
        try:
            await q.edit_message_text(
                f"⚙️ Анализирую... [id{uid}]\nСообщений: {mc}\n"
                f"В зависимости от числа сообщений\nможет занять 1-60 секунд",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        words = api_get(f"/words/{uid}") or []
        user = api_get(f"/user/{uid}") or {}
        await safe_send(q, fmt_words(uid, words, user, mc), words_kb(uid))
        return

    if action == "chats":
        data = api_get(f"/chats/{uid}")
        text, pp = fmt_chats(data, page, uid)
        kb = chats_kb(uid, *pp) if pp and pp[1] > 1 else chats_no_pages_kb(uid)
        await safe_send(q, text, kb)
        return

    if action == "common":
        data = api_get(f"/common_groups/{uid}")
        text, pp = fmt_common(data, page, uid)
        kb = common_kb(uid, *pp) if pp and pp[1] > 1 else common_no_pages_kb(uid)
        await safe_send(q, text, kb)
        return

    if action == "replies":
        data = api_get(f"/replies/{uid}")
        await safe_send(q, fmt_replies(data, uid), replies_kb(uid))
        return

    if action == "gifts":
        data = api_get(f"/gifts/{uid}", timeout=120)
        text, pp = fmt_gifts(data, page, uid)
        kb = gifts_kb(uid, *pp) if pp else gifts_no_pages_kb(uid)
        await safe_send(q, text, kb)
        return

    if action == "menu_static":
        await safe_send(q, MENU_TEXT, analysis_kb_static(uid))
        return

    if action == "menu":
        total = len(ANALYSIS_STEPS)
        try:
            await q.edit_message_text(analysis_progress_text(0, total),
                parse_mode=ParseMode.HTML, reply_markup=analysis_kb_partial(uid, 0))
        except Exception:
            pass
        for step_num in range(1, total + 1):
            _ = analysis_check(ANALYSIS_STEPS[step_num - 1][0], uid)
            try:
                await q.edit_message_text(analysis_progress_text(step_num, total),
                    parse_mode=ParseMode.HTML, reply_markup=analysis_kb_partial(uid, step_num))
            except Exception:
                pass
            await asyncio.sleep(0.5)
        try:
            await q.edit_message_text(MENU_TEXT, parse_mode=ParseMode.HTML,
                reply_markup=analysis_kb_partial(uid, total))
        except Exception:
            pass
        return

    if action == "age":
        msgs = api_get(f"/age/{uid}") or []
        body, pp = fmt_age_body(msgs, page)
        if not body:
            await safe_send(q, NO_DATA, back_only_kb(uid))
        else:
            await safe_send(q, f"<b>Сообщения, похожие на возраст:</b>\n\n{body}",
                paginated_kb(uid, "age", *pp))
        return

    if action == "mentions_in":
        msgs = api_get(f"/mentions_in/{uid}") or []
        body, pp = fmt_mentions_body(msgs, page)
        if not body:
            await safe_send(q, NO_DATA, back_only_kb(uid))
        else:
            title = f"<b>Cоοбщeния c @ником / упоминαниeм αkқαγнтα {uid}.</b>"
            await safe_send(q, f"{title}\n\n{body}", paginated_kb(uid, "mentions_in", *pp))
        return

    if action == "mentions_out":
        msgs = api_get(f"/mentions_out/{uid}") or []
        body, pp = fmt_mentions_body(msgs, page)
        if not body:
            await safe_send(q, NO_DATA, back_only_kb(uid))
        else:
            title = f"<b>Cоοбщeния c @ником / упоминαниeм αkқαγнтα {uid}.</b>"
            await safe_send(q, f"{title}\n\n{body}", paginated_kb(uid, "mentions_out", *pp))
        return

    if action == "chinfo":
        uname = parts[3] if len(parts) > 3 else ""
        info = api_get(f"/channel_info/{uname}", timeout=30)
        if info and info.get("ok"):
            await safe_send(q, fmt_channel_info(info, uname),
                channel_kb(info["id"], info.get("username") or uname))
        else:
            await safe_send(q, f"❌ <code>{esc((info or {}).get('error', '?'))}</code>")
        return

    if action == "chatstat":
        uname = parts[3] if len(parts) > 3 else ""
        await safe_send(q, "📊 <b>Стата чата</b>\n\n<i>в разработке</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад",
                callback_data=f"an:chinfo:{uid}:{uname}")]]))
        return

    if action == "chatdesc":
        uname = parts[3] if len(parts) > 3 else ""
        await safe_send(q, "📝 <b>История описания</b>\n\n<i>в разработке</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад",
                callback_data=f"an:chinfo:{uid}:{uname}")]]))
        return

    if action == "chatforwards":
        uname = parts[3] if len(parts) > 3 else ""
        await safe_send(q, "🔗 <b>Все пересылки</b>\n\n<i>в разработке</i>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад",
                callback_data=f"an:chinfo:{uid}:{uname}")]]))
        return

    await safe_send(q, NO_DATA, back_only_kb(uid))


def run_bot():
    app_tg = Application.builder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CommandHandler("pd_msg", cmd_pd))
    app_tg.add_handler(CommandHandler("topchat", cmd_topchat))
    app_tg.add_handler(CommandHandler("search", cmd_search))
    app_tg.add_handler(CommandHandler("human", cmd_human))
    app_tg.add_handler(MessageHandler(filters.StatusUpdate.USERS_SHARED, on_users_shared))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app_tg.add_handler(CallbackQueryHandler(on_callback))
    log.info("Bot started")
    app_tg.run_polling()


if __name__ == "__main__":
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
    run_bot()
