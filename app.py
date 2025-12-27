import os
import json
import time
import random
import secrets
import sqlite3
import re
from datetime import datetime
from typing import Optional, Dict, Any, List
from io import BytesIO
from urllib.parse import urlparse

import requests
import qrcode
from flask import Flask, render_template, redirect, url_for, request, abort, send_file

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_LANG = os.getenv("TMDB_LANG", "en-US").strip()
TMDB_INCLUDE_ADULT = os.getenv("TMDB_INCLUDE_ADULT", "false").strip().lower() == "true"

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
DEFAULT_SPOTIFY_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "").strip()
SPOTIFY_MARKET = os.getenv("SPOTIFY_MARKET", "CA").strip()

DB_PATH = os.path.join("/tmp", "game.db")
BASE_URL = os.getenv("BASE_URL", "").strip()

BASE_TIMER_SECONDS = int(os.getenv("BASE_TIMER_SECONDS", "8"))
MAX_CLUE_WORDS = int(os.getenv("MAX_CLUE_WORDS", "18"))

app = Flask(__name__)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def safe_json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

def safe_json_loads(s):
    try:
        return json.loads(s)
    except Exception:
        return {}

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                source TEXT NOT NULL,
                players INTEGER NOT NULL,
                answer TEXT NOT NULL,
                meta_json TEXT NOT NULL,
                clues_json TEXT NOT NULL,
                qr_png BLOB NOT NULL
            )
        """)
        conn.commit()

init_db()

def make_id(n=6):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))

def sanitize_base_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    p = urlparse(raw)
    if not p.scheme or not p.netloc:
        return raw.rstrip("/")
    return f"{p.scheme}://{p.netloc}"

def resolve_base_url(req_host_url):
    return sanitize_base_url(BASE_URL or req_host_url)

def make_qr_png_bytes(url):
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def clamp_players(value):
    try:
        p = int(value)
    except Exception:
        p = 4
    return max(2, min(p, 12))

def clue_count_for_players(players):
    return clamp_players(players)

def timer_for_players(players):
    p = clamp_players(players)
    if p <= 2:
        return max(5, BASE_TIMER_SECONDS - 1)
    if p <= 4:
        return BASE_TIMER_SECONDS
    if p <= 6:
        return BASE_TIMER_SECONDS + 1
    return BASE_TIMER_SECONDS + 2

def clamp_words(s, max_words):
    return " ".join((s or "").split()[:max_words])

STOP_WORDS = {"the","a","an","and","or","of","to","in","on","for","with","from","by","at","is","it","this","that","as","are"}

def normalize_tokens(s):
    s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
    return [t for t in s.split() if t and t not in STOP_WORDS]

def clue_leaks_answer(answer, clue, mode):
    if answer.lower() in clue.lower():
        return True
    return False

def violates_sentence_rules(clue):
    if len(clue.split()) < 6:
        return True
    if ":" in clue or ";" in clue:
        return True
    if re.search(r"\b(19|20)\d{2}\b", clue):
        return True
    return False

def openai_generate_clues(mode, answer, meta, n_clues):
    n_clues = max(2, min(n_clues, 12))
    system = (
        "Write clues like a normal person talking out loud.\n"
        "Use simple words.\n"
        "Do not sound poetic, academic, or polished.\n"
        "Do not explain things clearly.\n"
        "Sound slightly unsure or casual.\n"
        "Never use names, dates, or genres.\n"
        "Never reveal the answer."
    )

    def role(i):
        if i == 1:
            return "Very vague memory of the time or feeling."
        if i == 2:
            return "What people remember feeling or arguing about."
        if i == 3:
            return "Why people still bring it up."
        if i <= 6:
            return "More familiar hints without facts."
        return "Strong but indirect hints."

    prompt = (
        "Return strict JSON only:\n"
        "{\"clues\":[\"...\"]}\n"
        f"Exactly {n_clues} clues.\n"
        f"Each one sentence, max {MAX_CLUE_WORDS} words.\n"
    )
    for i in range(1, n_clues + 1):
        prompt += f"Clue {i}: {role(i)}\n"
    prompt += f"ANSWER: {answer}\nMETA: {safe_json_dumps(meta)}"

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    for _ in range(7):
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json={
                "model": OPENAI_MODEL,
                "input": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.65
            },
            timeout=30
        )
        text = r.json().get("output_text", "")
        try:
            obj = json.loads(text[text.find("{"):text.rfind("}") + 1])
            clues = obj.get("clues")
            if not isinstance(clues, list) or len(clues) != n_clues:
                continue
            out = []
            for c in clues:
                c = clamp_words(c.strip(), MAX_CLUE_WORDS)
                if violates_sentence_rules(c):
                    raise ValueError
                if clue_leaks_answer(answer, c, mode):
                    raise ValueError
                out.append(c)
            return out
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("Clue generation failed")

def create_card(mode, source, players, answer, meta, base_url):
    clues = openai_generate_clues(mode, answer, meta, clue_count_for_players(players))
    card_id = make_id()
    scan_url = f"{base_url}/c/{card_id}"
    qr = make_qr_png_bytes(scan_url)
    with db() as conn:
        conn.execute(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)",
            (
                card_id,
                datetime.utcnow().isoformat(),
                mode,
                source,
                players,
                answer,
                safe_json_dumps(meta),
                safe_json_dumps(clues),
                sqlite3.Binary(qr)
            )
        )
        conn.commit()
    return card_id
