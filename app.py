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
from PIL import Image
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

def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

def safe_json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return {}

def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,

                mode TEXT NOT NULL,
                source TEXT NOT NULL,
                players INTEGER NOT NULL DEFAULT 4,

                answer TEXT NOT NULL,
                meta_json TEXT NOT NULL,

                clues_json TEXT NOT NULL DEFAULT '[]',
                qr_png BLOB NOT NULL
            )
            """
        )

        cols = [r["name"] for r in conn.execute("PRAGMA table_info(cards)").fetchall()]
        cols_set = set(cols)

        if "players" not in cols_set:
            conn.execute("ALTER TABLE cards ADD COLUMN players INTEGER NOT NULL DEFAULT 4")
            cols_set.add("players")

        if "clues_json" not in cols_set:
            conn.execute("ALTER TABLE cards ADD COLUMN clues_json TEXT NOT NULL DEFAULT '[]'")
            cols_set.add("clues_json")

        if {"clue1", "clue2", "clue3"}.issubset(cols_set):
            rows = conn.execute(
                "SELECT id, clue1, clue2, clue3, clues_json FROM cards"
            ).fetchall()

            for row in rows:
                current = row["clues_json"] or "[]"
                try:
                    parsed = json.loads(current)
                except Exception:
                    parsed = []

                if not isinstance(parsed, list) or len(parsed) == 0:
                    clues = [row["clue1"], row["clue2"], row["clue3"]]
                    clues = [c for c in clues if c]
                    conn.execute(
                        "UPDATE cards SET clues_json = ? WHERE id = ?",
                        (safe_json_dumps(clues), row["id"]),
                    )

        conn.commit()

init_db()

def make_id(n: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))

def sanitize_base_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    p = urlparse(raw)
    if not p.scheme or not p.netloc:
        return raw.rstrip("/")
    return f"{p.scheme}://{p.netloc}"

def resolve_base_url(req_host_url: str) -> str:
    if BASE_URL:
        return sanitize_base_url(BASE_URL)
    return sanitize_base_url(req_host_url)

def make_qr_png_bytes(url: str) -> bytes:
    bg_path = "static/qr/qr_bg_927x597.png"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=8,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")

    bg = Image.open(bg_path).convert("RGBA")

    if bg.size != (927, 597):
        raise RuntimeError(f"QR background must be 927x597, got {bg.size}")

    qr_size = 220
    x = 927 - qr_size - 40
    y = 597 - qr_size - 40

    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
    bg.alpha_composite(qr_img, (x, y))

    out = BytesIO()
    bg.save(out, format="PNG")
    return out.getvalue()



def clamp_players(value: str) -> int:
    try:
        p = int((value or "4").strip())
    except ValueError:
        p = 4
    return max(2, min(p, 12))

def clue_count_for_players(players: int) -> int:
    return max(2, min(players, 12))

def timer_for_players(players: int) -> int:
    p = max(2, min(players, 12))
    if p <= 2:
        return max(5, BASE_TIMER_SECONDS - 1)
    if p <= 4:
        return BASE_TIMER_SECONDS
    if p <= 6:
        return BASE_TIMER_SECONDS + 1
    return BASE_TIMER_SECONDS + 2

def clamp_words(s: str, max_words: int) -> str:
    parts = (s or "").strip().split()
    return " ".join(parts[:max_words])

STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "from", "by", "at",
    "is", "it", "this", "that", "as", "are"
}

def normalize_tokens(s: str) -> List[str]:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    toks = [t for t in s.split() if t and t not in STOP_WORDS]
    return toks

def clue_leaks_answer(answer: str, clue: str, mode: str) -> bool:
    a = (answer or "").strip()
    c = (clue or "").strip()
    if not a or not c:
        return False

    if a.lower() in c.lower():
        return True

    if mode == "music" and " - " in a:
        track, artist = a.split(" - ", 1)
        track_toks = set(normalize_tokens(track))
        artist_toks = set(normalize_tokens(artist))
        clue_toks = set(normalize_tokens(c))

        if len(track_toks) >= 2 and len(track_toks & clue_toks) >= 2:
            return True
        if len(artist_toks) >= 2 and len(artist_toks & clue_toks) >= 2:
            return True

        if track.strip() and track.lower() in c.lower():
            return True

        return False

    title_toks = set(normalize_tokens(a))
    clue_toks = set(normalize_tokens(c))

    if len(title_toks) <= 2:
        if len(title_toks & clue_toks) >= 1 and len(title_toks) >= 1:
            return True
        return False

    if len(title_toks & clue_toks) >= 3:
        return True

    return False

FORBIDDEN_PATTERNS = [
    re.compile(r"\b(19|20)\d{2}\b"),
    re.compile(r"\b(oscar|academy award|grammy|emmy|golden globe|bafta)\b", re.I),
    re.compile(r"\b(released|premiered|debuted)\b", re.I),
]

FORBIDDEN_WORDS = {
    "imdb", "tmdb", "spotify", "netflix", "disney",
}


COMPLEX_WORDS = {
    "explores", "examine", "examines", "reflects", "depicts", "portrays", "juxtaposes",
    "metaphor", "allegory", "nuanced", "subtle", "satire", "commentary", "critique",
    "existential", "philosophical", "paradigm", "societal", "cultural", "narrative",
    "aesthetic", "cinematic", "iconography", "archetype"
}

def looks_like_list_or_keywords(s: str) -> bool:
    t = (s or "").strip()
    if not t:
        return True
    if ":" in t:
        return True
    if " / " in t or " | " in t or ";" in t:
        return True
    if len(t.split()) < 6:
        return True
    return False

def violates_sentence_rules(clue: str) -> bool:
    c = (clue or "").strip()
    if not c:
        return True

    for pat in FORBIDDEN_PATTERNS:
        if pat.search(c):
            return True

    toks = set(normalize_tokens(c))
    if any(w in toks for w in FORBIDDEN_WORDS):
        return True

    if any(w in toks for w in COMPLEX_WORDS):
        return True

    if looks_like_list_or_keywords(c):
        return True

    return False

def openai_generate_clues(mode: str, answer: str, meta: Dict[str, Any], n_clues: int) -> List[str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY env var.")

    n_clues = max(2, min(n_clues, 12))

    if n_clues <= 4:
        trivia_start = max(2, n_clues)  # last clue can be concrete
    elif n_clues <= 6:
        trivia_start = 4
    else:
        trivia_start = 6

    system = (
        "You write clue sentences for a party guessing game.\n"
        "Use plain, spoken English, like a friend talking.\n"
        "Avoid fancy vocabulary and critic tone.\n"
        "Never reveal the title.\n"
        "Do not use character names.\n"
        "Write one sentence per clue, not lists.\n"
        "Clues must get more helpful as they go."
    )

    def clue_role(i: int) -> str:
        if mode == "movie":
            if n_clues <= 4:
                if i == 1:
                    return "Vibe and general feel, but still useful."
                if i == 2:
                    return "Clear setting hint, no names."
                if i == 3:
                    return "Genre hint in simple words, or soundtrack vibe."
                return "One concrete hint: decade like 1990s OR one actor OR the director."
            else:
                if i == 1:
                    return "Broad vibe and what kind of night it feels like."
                if i == 2:
                    return "General setting hint without names."
                if i == 3:
                    return "Genre hint using simple words."
                if i == 4:
                    return "Soundtrack or audio vibe hint."
                if i == 5:
                    return "Clearer setting hint, still no names."
                if i >= trivia_start:
                    return "Concrete hint: decade like 1990s OR one actor OR the director."
                return "More familiar hint that narrows it, still no identifiers."

        if mode == "music":
            if n_clues <= 4:
                if i == 1:
                    return "Vibe and when people play it."
                if i == 2:
                    return "Sound description, simple words."
                if i == 3:
                    return "Decade hint like 2010s or 1990s."
                return "One concrete hint: mainstream vs niche, or what kind of playlist it shows up in."
            else:
                if i == 1:
                    return "Broad vibe and where it fits socially."
                if i == 2:
                    return "What it sounds like, instruments or production feel."
                if i == 3:
                    return "Genre-ish hint without strict labels."
                if i == 4:
                    return "Decade hint like 2010s or 1990s."
                if i == 5:
                    return "How people use it (party, gym, sad hours, throwback), keep it broad."
                return "Final hint that narrows the vibe without naming the artist."

        return "Broad hint that becomes clearer later."



    def build_user_prompt(extra_rule: str) -> str:
        role_lines = "\n".join([f"- Clue {i}: {clue_role(i)}" for i in range(1, n_clues + 1)])
        return (
            "Generate escalating clue sentences for a QR party guessing game.\n"
            "Return strict JSON only in this exact format:\n"
            "{\"clues\":[\"...\"]}\n"
            "Hard rules:\n"
            f"- Exactly {n_clues} clues\n"
            f"- Each clue is one single sentence, max {MAX_CLUE_WORDS} words\n"
            "- No lists, no bullet points, no colons\n"
            "- Use plain, spoken English. Keep it casual.\n"
            "- Avoid fancy words (no academic tone, no critic tone).\n"
            "- No names of people, characters, places, brands, or studios\n"
            "- No dates or years\n"
            "- Do not include the answer text or close paraphrases of it\n"
            "- Clues must be broad and indirect, but get gradually more suggestive as they progress\n"
            "- Even if the clues must be broad and indirect, it needs to give the person reading and idea of what the movie or show is, without directly saying it\n"
            "- The first clue should be broad and general, but as we move forward, it needs to have familiar hint that narrows it, still no identifiers\n"
            "- The hints needs to be easy for someone to remember, so keep the clues simple\n"
            "- You can use director name, in fact, for one of the hints, use 1-3 actors names, or you can say something like: the main actor played in this other movie\n"
            "- For another clue, use the decade that the movie was released (example: 90's)\n"
            f"- If MODE is movie: you may use ONE concrete hint starting at clue {trivia_start} (decade like 1990s, or one actor, or the director)\n"
            "- Do not use character names\n"
            "- Never mention character names.\n"
            "Clue role progression:\n"
            f"{role_lines}\n"
            f"{extra_rule}\n"
            f"MODE: {mode}\n"
            f"ANSWER (do not reveal): {answer}\n"
            f"META (do not quote; only use for vibe): {safe_json_dumps(meta)}\n"
        )

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    def extract_text(resp_json: Dict[str, Any]) -> str:
        t = (resp_json.get("output_text") or "").strip()
        if t:
            return t

        out = resp_json.get("output") or []
        parts: List[str] = []
        for item in out:
            for c in (item.get("content") or []):
                if c.get("type") in ("output_text", "text"):
                    txt = c.get("text") or ""
                    if txt:
                        parts.append(txt)
        return "\n".join(parts).strip()

    def try_parse_json(text: str) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except Exception:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])

        raise ValueError("No JSON object found in model output.")

    last_err = None
    last_preview = ""
    extra_rule = ""

    for _ in range(7):
        payload = {
            "model": OPENAI_MODEL,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": build_user_prompt(extra_rule)},
            ],
            "temperature": 0.65,
        }

        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=30)

        if r.status_code >= 400:
            last_err = RuntimeError(f"OpenAI error {r.status_code}: {r.text[:500]}")
            time.sleep(0.5)
            continue

        data = r.json()
        text = extract_text(data)
        last_preview = (text[:220] + ("..." if len(text) > 220 else "")) if text else "<empty>"

        try:
            obj = try_parse_json(text)
            clues = obj.get("clues")

            if not isinstance(clues, list) or len(clues) != n_clues:
                raise ValueError("Wrong JSON shape or wrong clue count.")

            cleaned: List[str] = []
            for c in clues:
                c = clamp_words(str(c).strip(), MAX_CLUE_WORDS)
                if not c:
                    raise ValueError("Empty clue.")
                if clue_leaks_answer(answer, c, mode):
                    raise ValueError("LEAK")
                if violates_sentence_rules(c):
                    raise ValueError("STYLE")
                cleaned.append(c)

            return cleaned

        except Exception as e:
            last_err = e

            if str(e) == "LEAK":
                extra_rule = (
                    "- Extra rule: avoid any proper nouns entirely. "
                    "Do not mention any names, titles, places, or organizations.\n"
                )
            elif str(e) == "STYLE":
                extra_rule = (
                    "- Extra rule: use simpler words. "
                    "No fancy vocabulary. "
                    "One plain sentence per clue.\n"
                )

            time.sleep(0.5)

    raise RuntimeError(f"Failed to generate clues. Last output preview: {last_preview}. Last error: {last_err}")

def tmdb_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not TMDB_API_KEY:
        raise RuntimeError("Missing TMDB_API_KEY env var.")
    base = "https://api.themoviedb.org/3"
    p = params or {}
    p["api_key"] = TMDB_API_KEY
    p.setdefault("language", TMDB_LANG)
    r = requests.get(f"{base}{path}", params=p, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"TMDb error {r.status_code}: {r.text[:500]}")
    return r.json()

def pick_random_tmdb_movie() -> (str, Dict[str, Any]):
    page = random.randint(1, 20)
    discover = tmdb_get(
        "/discover/movie",
        params={
            "sort_by": "popularity.desc",
            "include_adult": "true" if TMDB_INCLUDE_ADULT else "false",
            "page": page,
        },
    )
    results = discover.get("results") or []
    if not results:
        raise RuntimeError("TMDb returned no movies.")

    movie = random.choice(results)
    movie_id = movie.get("id")
    title = (movie.get("title") or movie.get("original_title") or "Unknown").strip()
    release_date = (movie.get("release_date") or "").strip()
    year = release_date[:4] if release_date else ""

    details = tmdb_get(f"/movie/{movie_id}", params={})
    credits = tmdb_get(f"/movie/{movie_id}/credits", params={})

    genres = [g.get("name") for g in (details.get("genres") or []) if g.get("name")]
    overview = (details.get("overview") or "").strip()

    cast = []
    for c in (credits.get("cast") or [])[:6]:
        n = c.get("name")
        if n:
            cast.append(n)

    director = ""
    for crew in (credits.get("crew") or []):
        if crew.get("job") == "Director":
            director = crew.get("name") or ""
            break

    meta = {
        "type": "movie",
        "title": title,
        "year": year,
        "genres": genres,
        "overview": overview,
        "director": director,
        "top_cast": cast,
        "tmdb_id": movie_id,
    }
    return title, meta

_SPOTIFY_TOKEN_CACHE = {"token": "", "expires_at": 0}

def spotify_get_token() -> str:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise RuntimeError("Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET env var.")

    now = int(time.time())
    if _SPOTIFY_TOKEN_CACHE["token"] and now < _SPOTIFY_TOKEN_CACHE["expires_at"] - 30:
        return _SPOTIFY_TOKEN_CACHE["token"]

    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Spotify token error {r.status_code}: {r.text[:500]}")

    data = r.json()
    token = data.get("access_token", "")
    expires_in = int(data.get("expires_in", 3600))
    _SPOTIFY_TOKEN_CACHE["token"] = token
    _SPOTIFY_TOKEN_CACHE["expires_at"] = now + expires_in
    return token

def spotify_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = spotify_get_token()
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"https://api.spotify.com/v1{path}", headers=headers, params=params or {}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Spotify error {r.status_code}: {r.text[:500]}")
    return r.json()

def pick_random_spotify_track_from_playlist(playlist_id: str) -> (str, Dict[str, Any]):
    if not playlist_id:
        raise RuntimeError("Missing playlist_id.")

    first = spotify_get(f"/playlists/{playlist_id}/tracks", params={"market": SPOTIFY_MARKET, "limit": 1, "offset": 0})
    total = int(first.get("total") or 0)
    if total <= 0:
        raise RuntimeError("Playlist empty or inaccessible.")

    limit = 100 if total >= 100 else total
    max_offset = max(0, total - limit)
    offset = random.randint(0, max_offset) if max_offset > 0 else 0

    page = spotify_get(
        f"/playlists/{playlist_id}/tracks",
        params={"market": SPOTIFY_MARKET, "limit": limit, "offset": offset},
    )

    items = page.get("items") or []
    tracks = []
    for it in items:
        t = (it.get("track") or {})
        if not t or t.get("type") != "track" or t.get("is_local"):
            continue
        name = (t.get("name") or "").strip()
        artists = [a.get("name") for a in (t.get("artists") or []) if a.get("name")]
        if name and artists:
            tracks.append(t)

    if not tracks:
        raise RuntimeError("No valid tracks found.")

    t = random.choice(tracks)
    name = (t.get("name") or "").strip()
    artists = [a.get("name") for a in (t.get("artists") or []) if a.get("name")]
    album = ((t.get("album") or {}).get("name") or "").strip()
    release_date = ((t.get("album") or {}).get("release_date") or "").strip()
    year = release_date[:4] if release_date else ""

    answer = f"{name} - {artists[0]}"
    meta = {
        "type": "music",
        "track_name": name,
        "artists": artists,
        "album": album,
        "year": year,
        "spotify_id": t.get("id"),
        "preview_url": t.get("preview_url"),
        "external_url": ((t.get("external_urls") or {}).get("spotify") or ""),
        "playlist_id": playlist_id,
    }
    return answer, meta

def create_card(mode: str, source: str, players: int, answer: str, meta: Dict[str, Any], base_url: str) -> str:
    n_clues = clue_count_for_players(players)

    try:
        clues = openai_generate_clues(mode=mode, answer=answer, meta=meta, n_clues=n_clues)
    except Exception:
        if mode == "movie":
            pool = [
                "People remember it more for its mood than its plot.",
                "It made a lot of people talk after they watched it.",
                "Even non fans have seen references to it.",
                "It feels familiar but still kind of weird.",
                "People argue about what it really means."
            ]
        else:
            pool = [
                "A lot of people know it before they can name it.",
                "The vibe is familiar even if you forget the details.",
                "It reminds people of a certain time without saying when.",
                "It has a part that gets stuck in your head.",
                "People bring it up in throwback playlists."
            ]
        clues = (pool * 3)[:n_clues]

    card_id = make_id()
    created_at = datetime.utcnow().isoformat()

    scan_url = f"{base_url}/c/{card_id}"
    qr_bytes = make_qr_png_bytes(scan_url)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO cards (id, created_at, mode, source, players, answer, meta_json, clues_json, qr_png)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                created_at,
                mode,
                source,
                players,
                answer,
                safe_json_dumps(meta),
                safe_json_dumps(clues),
                sqlite3.Binary(qr_bytes),
            ),
        )
        conn.commit()

    return card_id

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/create", methods=["GET", "POST"])
def create():
    if request.method == "GET":
        return render_template("create.html", default_playlist_id=DEFAULT_SPOTIFY_PLAYLIST_ID)

    kind = (request.form.get("kind") or "").strip()
    players = clamp_players(request.form.get("players") or "4")
    base_url = resolve_base_url(request.host_url)

    try:
        if kind == "tmdb_random_movie":
            answer, meta = pick_random_tmdb_movie()
            card_id = create_card("movie", "tmdb", players, answer, meta, base_url)
            return redirect(url_for("card_admin", card_id=card_id))

        if kind == "spotify_random_from_playlist":
            playlist_id = (request.form.get("playlist_id") or DEFAULT_SPOTIFY_PLAYLIST_ID).strip()
            answer, meta = pick_random_spotify_track_from_playlist(playlist_id)
            card_id = create_card("music", "spotify", players, answer, meta, base_url)
            return redirect(url_for("card_admin", card_id=card_id))

        if kind == "manual":
            mode = (request.form.get("mode") or "movie").strip()
            answer = (request.form.get("answer") or "").strip()
            if not answer:
                return "Missing answer.", 400
            meta = {"type": mode, "note": "manual_entry"}
            card_id = create_card(mode, "manual", players, answer, meta, base_url)
            return redirect(url_for("card_admin", card_id=card_id))

        return "Unknown kind.", 400

    except Exception as e:
        return f"Create failed: {type(e).__name__}: {e}", 500

@app.route("/admin/<card_id>")
def card_admin(card_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        abort(404)

    base_url = resolve_base_url(request.host_url)
    meta = safe_json_loads(row["meta_json"])
    clues = safe_json_loads(row["clues_json"])
    if not isinstance(clues, list):
        clues = []

    scan_url = f"{base_url}/c/{card_id}"
    qr_url = f"{base_url}/qr/{card_id}.png"

    return render_template(
        "admin.html",
        card_id=card_id,
        players=int(row["players"] or 4),
        mode=row["mode"],
        source=row["source"],
        answer=row["answer"],
        clues=clues,
        meta=meta,
        scan_url=scan_url,
        qr_url=qr_url,
    )


@app.route("/qr/<card_id>.png")
def qr_png(card_id):
    with db() as conn:
        row = conn.execute("SELECT qr_png FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        abort(404)

    data = row["qr_png"]
    if not data:
        abort(404)

    return send_file(BytesIO(data), mimetype="image/png", download_name=f"{card_id}.png")

@app.route("/c/<card_id>")
def card_view(card_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        abort(404)

    step_str = request.args.get("step", "0")
    try:
        step = int(step_str)
    except ValueError:
        step = 0
    step = max(0, min(step, 99))

    show = (request.args.get("show", "0").strip() == "1")

    players = int(row["players"] or 4)
    timer_seconds = timer_for_players(players)

    clues = safe_json_loads(row["clues_json"])
    if not isinstance(clues, list) or not clues:
        clues = []
    total_clues = len(clues)

    current_clue = None
    reveal_answer = False

    if show:
        if 1 <= step <= total_clues:
            current_clue = str(clues[step - 1])
        elif step == total_clues + 1:
            reveal_answer = True

    return render_template(
        "card.html",
        mode=row["mode"],
        card_id=card_id,
        players=players,
        timer_seconds=timer_seconds,
        answer=row["answer"],
        step=step,
        show=show,
        total_clues=total_clues,
        current_clue=current_clue,
        reveal_answer=reveal_answer,
    )

if __name__ == "__main__":
    app.run(debug=True)
