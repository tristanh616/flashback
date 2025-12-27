import os
import json
import time
import random
import secrets
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from io import BytesIO
from urllib.parse import urlparse

import requests
import qrcode
from flask import Flask, render_template, redirect, url_for, request, abort, send_file


# ----------------------------
# Configuration (ENV VARS)
# ----------------------------
# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

# TMDb
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_LANG = os.getenv("TMDB_LANG", "en-US").strip()
TMDB_INCLUDE_ADULT = os.getenv("TMDB_INCLUDE_ADULT", "false").strip().lower() == "true"

# Spotify
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
DEFAULT_SPOTIFY_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "").strip()
SPOTIFY_MARKET = os.getenv("SPOTIFY_MARKET", "CA").strip()

# App
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Heroku-friendly: do not depend on writing to your repo directory.
DB_PATH = os.path.join("/tmp", "game.db")

# If you set BASE_URL, use scheme+host only: https://your-app.herokuapp.com
BASE_URL = os.getenv("BASE_URL", "").strip()

# Timer tuning
BASE_TIMER_SECONDS = int(os.getenv("BASE_TIMER_SECONDS", "8"))

# Clue constraints
MAX_CLUE_WORDS = int(os.getenv("MAX_CLUE_WORDS", "8"))

app = Flask(__name__)


# ----------------------------
# Database
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,

                mode TEXT NOT NULL,              -- "movie" or "music"
                source TEXT NOT NULL,            -- "tmdb" or "spotify" or "manual"
                players INTEGER NOT NULL DEFAULT 4,

                answer TEXT NOT NULL,
                meta_json TEXT NOT NULL,

                clue1 TEXT NOT NULL,
                clue2 TEXT NOT NULL,
                clue3 TEXT NOT NULL,

                qr_png BLOB NOT NULL             -- QR image bytes stored in DB
            )
            """
        )

        # Migration for older DBs
        try:
            conn.execute("ALTER TABLE cards ADD COLUMN players INTEGER NOT NULL DEFAULT 4")
        except sqlite3.OperationalError:
            pass

        conn.commit()


# Ensure schema exists under gunicorn
init_db()


# ----------------------------
# Utilities
# ----------------------------
def make_id(n: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def safe_json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return {}


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
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def clamp_players(value: str) -> int:
    try:
        p = int((value or "4").strip())
    except ValueError:
        p = 4
    return max(2, min(p, 12))


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


# ----------------------------
# OpenAI clue generation
# ----------------------------
def openai_generate_clues(mode: str, answer: str, meta: Dict[str, Any]) -> Tuple[str, str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY env var.")

    meta_compact = {
        "mode": mode,
        "answer": answer,
        "meta": meta,
        "rules": [
            "Return JSON only.",
            "Exactly 3 clues: clue1, clue2, clue3.",
            "Clues must NOT include the answer text or near-identical forms.",
            f"Each clue must be at most {MAX_CLUE_WORDS} words.",
            "Prefer keywords, not full sentences.",
            "No commas. No semicolons. No parentheses.",
            "Clue difficulty must escalate: broad -> narrower -> almost giveaway.",
        ],
    }

    system = "You generate short party-game clues. Never reveal the answer."
    user = (
        "Generate three escalating clues for a QR party guessing game.\n"
        "Output strict JSON only: {\"clue1\":\"...\",\"clue2\":\"...\",\"clue3\":\"...\"}\n"
        f"DATA:\n{safe_json_dumps(meta_compact)}"
    )

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
    }

    last_err = None
    for _ in range(3):
        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=30)
        if r.status_code >= 400:
            last_err = RuntimeError(f"OpenAI error {r.status_code}: {r.text[:500]}")
            time.sleep(0.6)
            continue

        data = r.json()
        text = (data.get("output_text") or "").strip()

        if not text:
            try:
                output = data.get("output", [])
                parts = []
                for item in output:
                    for c in item.get("content", []):
                        if c.get("type") in ("output_text", "text"):
                            parts.append(c.get("text", ""))
                text = "\n".join([p for p in parts if p]).strip()
            except Exception:
                text = ""

        try:
            obj = json.loads(text)
            c1 = clamp_words((obj.get("clue1") or "").strip(), MAX_CLUE_WORDS)
            c2 = clamp_words((obj.get("clue2") or "").strip(), MAX_CLUE_WORDS)
            c3 = clamp_words((obj.get("clue3") or "").strip(), MAX_CLUE_WORDS)

            if not all([c1, c2, c3]):
                raise ValueError("Missing clue fields.")

            low_ans = answer.lower()
            if low_ans in c1.lower() or low_ans in c2.lower() or low_ans in c3.lower():
                raise ValueError("Clue contains answer text.")

            return c1, c2, c3
        except Exception as e:
            last_err = e
            time.sleep(0.6)

    raise RuntimeError(f"Failed to generate clues. Last error: {last_err}")


# ----------------------------
# TMDb helpers
# ----------------------------
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


def pick_random_tmdb_movie() -> Tuple[str, Dict[str, Any]]:
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

    answer = title
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
    return answer, meta


# ----------------------------
# Spotify helpers
# ----------------------------
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


def pick_random_spotify_track_from_playlist(playlist_id: str) -> Tuple[str, Dict[str, Any]]:
    if not playlist_id:
        raise RuntimeError("Missing playlist_id for Spotify track selection.")

    first = spotify_get(f"/playlists/{playlist_id}/tracks", params={"market": SPOTIFY_MARKET, "limit": 1, "offset": 0})
    total = int(first.get("total") or 0)
    if total <= 0:
        raise RuntimeError("Playlist seems empty or inaccessible.")

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
        if not name or not artists:
            continue
        tracks.append(t)

    if not tracks:
        raise RuntimeError("No valid tracks found in the sampled playlist range.")

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


# ----------------------------
# Card creation
# ----------------------------
def create_card(mode: str, source: str, players: int, answer: str, meta: Dict[str, Any], base_url: str) -> str:
    c1, c2, c3 = openai_generate_clues(mode=mode, answer=answer, meta=meta)

    card_id = make_id()
    created_at = datetime.utcnow().isoformat()

    scan_url = f"{base_url}/c/{card_id}"
    qr_bytes = make_qr_png_bytes(scan_url)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO cards (id, created_at, mode, source, players, answer, meta_json, clue1, clue2, clue3, qr_png)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                created_at,
                mode,
                source,
                players,
                answer,
                safe_json_dumps(meta),
                c1,
                c2,
                c3,
                sqlite3.Binary(qr_bytes),
            ),
        )
        conn.commit()

    return card_id


# ----------------------------
# Routes
# ----------------------------
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
            mode = "movie"
            answer, meta = pick_random_tmdb_movie()
            card_id = create_card(mode=mode, source="tmdb", players=players, answer=answer, meta=meta, base_url=base_url)
            return redirect(url_for("card_admin", card_id=card_id))

        if kind == "spotify_random_from_playlist":
            mode = "music"
            playlist_id = (request.form.get("playlist_id") or DEFAULT_SPOTIFY_PLAYLIST_ID).strip()
            answer, meta = pick_random_spotify_track_from_playlist(playlist_id)
            card_id = create_card(mode=mode, source="spotify", players=players, answer=answer, meta=meta, base_url=base_url)
            return redirect(url_for("card_admin", card_id=card_id))

        if kind == "manual":
            mode = (request.form.get("mode") or "movie").strip()
            answer = (request.form.get("answer") or "").strip()
            if not answer:
                return "Missing answer.", 400
            meta = {"type": mode, "note": "manual_entry"}
            card_id = create_card(mode=mode, source="manual", players=players, answer=answer, meta=meta, base_url=base_url)
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

    meta = safe_json_loads(row["meta_json"])
    base_url = resolve_base_url(request.host_url)

    return {
        "card_id": card_id,
        "players": int(row["players"] or 4),
        "qr_image": f"{base_url}/qr/{card_id}.png",
        "scan_url": f"{base_url}/c/{card_id}",
        "mode": row["mode"],
        "source": row["source"],
        "answer": row["answer"],
        "clues": [row["clue1"], row["clue2"], row["clue3"]],
        "meta": meta,
    }


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
    step = max(0, min(step, 4))

    players = int(row["players"] or 4)
    timer_seconds = timer_for_players(players)

    clues = [row["clue1"], row["clue2"], row["clue3"]]
    current_clue = None
    reveal_answer = False

    if 1 <= step <= 3:
        current_clue = clues[step - 1]
    elif step == 4:
        reveal_answer = True

    return render_template(
        "card.html",
        mode=row["mode"],
        step=step,
        current_clue=current_clue,
        reveal_answer=reveal_answer,
        answer=row["answer"],
        card_id=card_id,
        timer_seconds=timer_seconds,
        players=players,
    )


if __name__ == "__main__":
    app.run(debug=True)
