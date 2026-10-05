#!/usr/bin/env python3
"""
Words API — только API. HTML отдаётся отдельным файлом web/index.html.
"""
import os
import secrets
import sqlite3
import time
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = os.getenv("DB_PATH", "/data/chat_parser.db")
KEYS_DB = os.getenv("KEYS_DB", "/data/api_keys.db")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.getenv("PORT", "8000"))

app = FastAPI(title="Words API", version="1.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def init_keys():
    os.makedirs(os.path.dirname(KEYS_DB) or ".", exist_ok=True)
    c = sqlite3.connect(KEYS_DB)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key TEXT PRIMARY KEY,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            requests INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            active INTEGER DEFAULT 1);
    """)
    c.commit()
    c.close()


def validate_key(key):
    if not key:
        return None
    try:
        c = sqlite3.connect(KEYS_DB, timeout=10)
        row = c.execute(
            "SELECT name FROM api_keys WHERE key=? AND active=1", (key,)
        ).fetchone()
        if not row:
            c.close()
            return None
        c.execute(
            "UPDATE api_keys SET requests=requests+1, last_used=CURRENT_TIMESTAMP WHERE key=?",
            (key,),
        )
        c.commit()
        c.close()
        return row[0]
    except Exception:
        return None


def require(key: str):
    if not validate_key(key):
        raise HTTPException(403, "Invalid or missing API key")


def db():
    if not os.path.exists(DB_PATH):
        raise HTTPException(503, "База не найдена на сервере")
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.row_factory = sqlite3.Row
    return c


@app.on_event("startup")
def startup():
    init_keys()
    boot = os.getenv("BOOTSTRAP_KEY", "").strip()
    if boot:
        c = sqlite3.connect(KEYS_DB)
        exists = c.execute("SELECT 1 FROM api_keys WHERE key=?", (boot,)).fetchone()
        if not exists:
            c.execute("INSERT INTO api_keys (key, name) VALUES (?, ?)", (boot, "bootstrap"))
            c.commit()
            print("[+] bootstrap key создан")
        c.close()


@app.get("/healthz")
def health():
    return {"ok": True, "db_present": os.path.exists(DB_PATH), "db_path": DB_PATH}


@app.get("/stats")
def stats(key: str = Query(...)):
    require(key)
    c = db()
    try:
        r = c.execute("""
            SELECT
              (SELECT COUNT(*) FROM words) AS total_words,
              (SELECT COUNT(DISTINCT word) FROM words) AS uniq_words,
              (SELECT COUNT(DISTINCT user_id) FROM words) AS uniq_users,
              (SELECT COUNT(DISTINCT chat_id) FROM words) AS uniq_chats
        """).fetchone()
        return dict(r)
    finally:
        c.close()


@app.get("/top")
def top_chat(chat_id: str, key: str = Query(...), limit: int = Query(50, ge=1, le=500)):
    require(key)
    c = db()
    try:
        rows = c.execute("""
            SELECT word, n FROM word_stats
            WHERE chat_id=? ORDER BY n DESC LIMIT ?
        """, (str(chat_id), limit)).fetchall()
        if not rows:
            rows = c.execute("""
                SELECT word, COUNT(*) AS n FROM words
                WHERE chat_id=? GROUP BY word ORDER BY n DESC LIMIT ?
            """, (str(chat_id), limit)).fetchall()
        if not rows:
            raise HTTPException(404, "chat not found or no words")
        return {"chat_id": chat_id, "count": len(rows), "words": [dict(r) for r in rows]}
    finally:
        c.close()


@app.get("/top_all")
def top_all(key: str = Query(...), limit: int = Query(100, ge=1, le=1000)):
    require(key)
    c = db()
    try:
        rows = c.execute("""
            SELECT word, SUM(n) AS total FROM word_stats
            GROUP BY word ORDER BY total DESC LIMIT ?
        """, (limit,)).fetchall()
        return {"count": len(rows), "words": [dict(r) for r in rows]}
    finally:
        c.close()


@app.get("/user/{user_id}")
def user_words(user_id: str, key: str = Query(...), limit: int = Query(50, ge=1, le=500)):
    require(key)
    c = db()
    try:
        rows = c.execute("""
            SELECT word, COUNT(*) AS n FROM words
            WHERE user_id=? GROUP BY word ORDER BY n DESC LIMIT ?
        """, (str(user_id), limit)).fetchall()
        if not rows:
            wrote = c.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=?", (str(user_id),)
            ).fetchone()[0]
            if wrote > 0:
                raise HTTPException(404, f"Юзер писал ({wrote} сообщ.), но words не разобрана. Запусти fill_words.py")
            raise HTTPException(404, "user not found or no words")
        return {"user_id": user_id, "count": len(rows), "words": [dict(r) for r in rows]}
    finally:
        c.close()


@app.get("/word/{word}")
def word_users(word: str, key: str = Query(...), limit: int = Query(50, ge=1, le=500)):
    require(key)
    word = word.lower().strip()
    c = db()
    try:
        rows = c.execute("""
            SELECT w.user_id, u.username, u.first_name, COUNT(*) AS n
            FROM words w
            LEFT JOIN users u ON u.chat_id=w.chat_id AND u.user_id=w.user_id
            WHERE w.word=? GROUP BY w.user_id ORDER BY n DESC LIMIT ?
        """, (word, limit)).fetchall()
        if not rows:
            raise HTTPException(404, "word not found")
        return {"word": word, "count": len(rows), "users": [dict(r) for r in rows]}
    finally:
        c.close()


@app.get("/chat/{chat_id}/word/{word}")
def word_in_chat(chat_id: str, word: str, key: str = Query(...)):
    require(key)
    word = word.lower().strip()
    c = db()
    try:
        total = c.execute(
            "SELECT COUNT(*) FROM words WHERE chat_id=? AND word=?",
            (str(chat_id), word),
        ).fetchone()[0]
        if total == 0:
            raise HTTPException(404, "word not found in chat")
        top = c.execute("""
            SELECT w.user_id, u.username, u.first_name, COUNT(*) AS n
            FROM words w
            LEFT JOIN users u ON u.chat_id=w.chat_id AND u.user_id=w.user_id
            WHERE w.chat_id=? AND w.word=?
            GROUP BY w.user_id ORDER BY n DESC LIMIT 10
        """, (str(chat_id), word)).fetchall()
        return {"chat_id": chat_id, "word": word, "total": total,
                "top_users": [dict(r) for r in top]}
    finally:
        c.close()


@app.get("/chats")
def list_chats(key: str = Query(...), limit: int = Query(500, ge=1, le=2000)):
    require(key)
    c = db()
    try:
        rows = c.execute("""
            SELECT c.chat_id, c.title, c.username,
                   (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id) AS msgs
            FROM chats c
            WHERE (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id) > 0
            ORDER BY msgs DESC LIMIT ?
        """, (limit,)).fetchall()
        return {"count": len(rows), "chats": [dict(r) for r in rows]}
    finally:
        c.close()


@app.get("/top_users")
def top_users(key: str = Query(...), limit: int = Query(30, ge=1, le=200)):
    require(key)
    c = db()
    try:
        rows = c.execute("""
            SELECT w.user_id, MAX(u.username) AS username, MAX(u.first_name) AS first_name,
                   COUNT(*) AS total
            FROM words w
            LEFT JOIN users u ON u.chat_id=w.chat_id AND u.user_id=w.user_id
            WHERE w.user_id != '?' AND w.user_id != ''
            GROUP BY w.user_id ORDER BY total DESC LIMIT ?
        """, (limit,)).fetchall()
        return {"count": len(rows), "users": [dict(r) for r in rows]}
    finally:
        c.close()


# ===== Раздача web/index.html на / =====
@app.get("/", response_class=HTMLResponse)
def index():
    idx = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(idx):
        with open(idx, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("""
    <html><body style="font-family:system-ui;background:#08080a;color:#f2f2f4;padding:40px">
    <h1>Words API</h1>
    <p>API работает. Панель не найдена — нужен файл <code>web/index.html</code> рядом с main.py.</p>
    <p><a href="/docs" style="color:#3b82f6">API Docs</a> · <a href="/healthz" style="color:#3b82f6">Health</a></p>
    </body></html>
    """)

# Отдача статики из web/ по пути /static/
if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "add-key":
        init_keys()
        name = sys.argv[2] if len(sys.argv) > 2 else f"client_{int(time.time())}"
        k = "wapi_" + secrets.token_urlsafe(32)
        c = sqlite3.connect(KEYS_DB)
        c.execute("INSERT INTO api_keys (key, name) VALUES (?, ?)", (k, name))
        c.commit()
        c.close()
        print(f"✅ {name}: {k}")
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=PORT)
