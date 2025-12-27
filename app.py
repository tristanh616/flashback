import os
import json
import time
import random
import secrets
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List

import requests
import qrcode
from flask import Flask, render_template, redirect, url_for, request, abort


# ----------------------------
# Configuration (ENV VARS)
# ----------------------------
# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()  # pick a cheaper model for clue generation

# TMDb
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()  # v3 API key
TMDB_LANG = os.getenv("TMDB_LANG", "en-US").strip()
TMDB_INCLUDE_ADULT = os.getenv("TMDB_INCLUDE_ADULT", "false").strip().lower() == "true"

# Spotify
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
DEFAULT_SPOTIFY_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID", "").strip()  # optional convenience default
SPOTIFY_MARKET = os.getenv("SPOTIFY_MARKET", "CA").strip()

# App
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "game.db")
QR_DIR = os.path.join(APP_DIR, "static", "qr")

# If you want phone scanning on the same Wi-Fi:
# Set BASE_URL to your PC IP + port, ex: http://192.168.0.25:5000
BASE_URL = os.getenv("BASE_URL", "").strip()  # if empty, we'll use request.host_url (works on same device)


app = Flask(__name__)
os.makedirs(QR_DIR, exist_ok=True)


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

                answer TEXT NOT NULL,            -- hidden answer shown at step=4
                meta_json TEXT NOT NULL,         -- JSON metadata about the item

                clue1 TEXT NOT NULL,
                clue2 TEXT NOT NULL,
                clue3 TEXT NOT NULL
            )
            """
        )
        conn.commit()


# ----------------------------
# Utilities
# ----------------------------
def make_id(n=6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def resolve_base_url(req_host_url: str) -> str:
    if BASE_URL:
        return BASE_URL.rstrip("/")
    return req_host_url.strip("/")


def make_qr(card_id: str, base_url: str) -> str:
    """
    Returns the absolute path of the generated QR PNG on disk.
    """
    url = f"{base_url}/c/{card_id}"
    img = qrcode.make(url)
    path = os.path.join(QR_DIR, f"{card_id}.png")
    img.save(path)
    return path


def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def safe_json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return {}


# ----------------------------
# OpenAI (Responses API) clue generation
# ----------------------------
def openai_generate_clues(mode: str, answer: str, meta: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    Generates 3 clues in a ladder:
      clue1: broad
      clue2: narrower
      clue3: almost giveaway
    Must not contain the answer text or near-identical forms of it.
    """

    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY env var.")

    # Build a compact context the model can use.
    # For movies we include year, genres, cast, director if available, plot.
    # For music we include year, artists, album, genres if available.
    meta_compact = {
        "mode": mode,
        "answer": answer,
        "meta": meta,
        "rules": [
            "Return JSON only.",
            "Produce exactly 3 clues: clue1, clue2, clue3.",
            "Clues must not include the answer text (title, artist, etc).",
            "Clue3 can be near-giveaway but still must not directly say the answer.",
            "Each clue should be 1 short sentence (max ~18 words).",
            "Avoid quotation marks around the answer.",
        ],
    }

    system = (
        "You are generating party-game clues. The goal is fun, fair difficulty, and zero spoilers. "
        "Do not reveal the answer explicitly."
    )

    user = (
        "Generate three escalating clues for a QR party guessing game.\n"
        "Output strict JSON: {\"clue1\":\"...\",\"clue2\":\"...\",\"clue3\":\"...\"}\n"
        f"DATA:\n{safe_json_dumps(meta_compact)}"
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Keep it stable. You can tune later.
        "temperature": 0.7,
    }

    # Simple retry because sometimes models output stray text.
    last_err = None
    for _ in range(3):
        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=30)
        if r.status_code >= 400:
            last_err = RuntimeError(f"OpenAI error {r.status_code}: {r.text[:500]}")
            time.sleep(0.6)
            continue

        data = r.json()

        # Responses API can return content in different shapes; this tries common patterns safely.
        text = ""
        try:
            # Most often: output_text is present
            text = data.get("output_text", "") or ""
        except Exception:
            text = ""

        if not text:
            # Fallback: attempt to collect text from output blocks
            try:
                output = data.get("output", [])
                parts = []
                for item in output:
                    for c in item.get("content", []):
                        if c.get("type") in ("output_text", "text"):
                            parts.append(c.get("text", ""))
                text = "\n".join(p for p in parts if p).strip()
            except Exception:
                text = ""

        # Parse JSON
        try:
            obj = json.loads(text)
            c1 = (obj.get("clue1") or "").strip()
            c2 = (obj.get("clue2") or "").strip()
            c3 = (obj.get("clue3") or "").strip()
            if not all([c1, c2, c3]):
                raise ValueError("Missing clue fields.")
            # Very basic anti-spoiler check
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
    """
    Returns (answer, meta)
    answer: movie title (and year embedded in meta)
    meta: info to help clue generation
    """
    # Use discover for filtering/sorting. We'll pick a random page and random result.
    # Keep it mainstream-ish by popularity, exclude adult unless you explicitly allow.
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

    # Get extra details for better clues
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

    answer = title  # Keep answer to title only; year stays in meta.
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
    """
    Client Credentials Flow.
    This is enough for many read-only endpoints, including reading public playlists.
    """
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
    """
    Returns (answer, meta)
    answer: "Song Title - Artist"
    meta: info to help clue generation
    """
    if not playlist_id:
        raise RuntimeError("Missing playlist_id for Spotify track selection.")

    # We fetch up to 100 items from a random offset chunk.
    # Many playlists are smaller. We do two calls: first to get total, then pick offset.
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
        if not t:
            continue
        if t.get("type") != "track":
            continue
        if t.get("is_local"):
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

    # Answer format: Song - Artist (first artist)
    answer = f"{name} - {artists[0]}"

    meta = {
        "type": "music",
        "track_name": name,
        "artists": artists,
        "album": album,
        "year": year,
        "spotify_id": t.get("id"),
        "preview_url": t.get("preview_url"),
        # External URLs can help in admin view, not needed for clue gen
        "external_url": ((t.get("external_urls") or {}).get("spotify") or ""),
    }
    return answer, meta


# ----------------------------
# Card creation
# ----------------------------
def create_card(mode: str, source: str, answer: str, meta: Dict[str, Any], base_url: str) -> str:
    """
    Creates a DB record + QR, returns card_id
    """
    # Generate clues (pre-gen)
    c1, c2, c3 = openai_generate_clues(mode=mode, answer=answer, meta=meta)

    card_id = make_id()
    created_at = datetime.utcnow().isoformat()

    with db() as conn:
        conn.execute(
            """
            INSERT INTO cards (id, created_at, mode, source, answer, meta_json, clue1, clue2, clue3)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                created_at,
                mode,
                source,
                answer,
                safe_json_dumps(meta),
                c1,
                c2,
                c3,
            ),
        )
        conn.commit()

    make_qr(card_id, base_url)
    return card_id


# ----------------------------
# Routes
# ----------------------------
@app.route("/")
def index():
    """
    Minimal home. Your templates can show buttons:
      - Create random TMDb movie card
      - Create random Spotify playlist track card
      - Manual fallback
    """
    return render_template("index.html")


@app.route("/create", methods=["GET", "POST"])
def create():
    """
    One unified create endpoint.
    GET: shows a form (your HTML decides the UI)
    POST: chooses source:
      - tmdb_random_movie
      - spotify_random_from_playlist
      - manual
    """
    if request.method == "GET":
        return render_template("create.html", default_playlist_id=DEFAULT_SPOTIFY_PLAYLIST_ID)

    kind = (request.form.get("kind") or "").strip()
    req_host = request.host_url
    base_url = resolve_base_url(req_host)

    try:
        if kind == "tmdb_random_movie":
            mode = "movie"
            answer, meta = pick_random_tmdb_movie()
            card_id = create_card(mode=mode, source="tmdb", answer=answer, meta=meta, base_url=base_url)
            return redirect(url_for("card_admin", card_id=card_id))

        if kind == "spotify_random_from_playlist":
            mode = "music"
            playlist_id = (request.form.get("playlist_id") or DEFAULT_SPOTIFY_PLAYLIST_ID).strip()
            answer, meta = pick_random_spotify_track_from_playlist(playlist_id)
            # Store playlist_id for traceability
            meta["playlist_id"] = playlist_id
            card_id = create_card(mode=mode, source="spotify", answer=answer, meta=meta, base_url=base_url)
            return redirect(url_for("card_admin", card_id=card_id))

        if kind == "manual":
            # Manual answer, AI-generated clues from minimal meta
            mode = (request.form.get("mode") or "movie").strip()
            answer = (request.form.get("answer") or "").strip()
            if not answer:
                return "Missing answer.", 400
            meta = {"type": mode, "note": "manual_entry"}
            card_id = create_card(mode=mode, source="manual", answer=answer, meta=meta, base_url=base_url)
            return redirect(url_for("card_admin", card_id=card_id))

        return "Unknown kind. Expected tmdb_random_movie, spotify_random_from_playlist, or manual.", 400

    except Exception as e:
        # In a real app you'd render a friendly error page.
        return f"Create failed: {type(e).__name__}: {e}", 500


@app.route("/admin/<card_id>")
def card_admin(card_id):
    """
    Host view for printing/testing.
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        abort(404)

    meta = safe_json_loads(row["meta_json"])
    qr_path = f"/static/qr/{card_id}.png"

    # Plain JSON response (easy debugging). You can swap to render_template if you prefer.
    return {
        "card_id": card_id,
        "qr_image": qr_path,
        "scan_url": f"{resolve_base_url(request.host_url)}/c/{card_id}",
        "mode": row["mode"],
        "source": row["source"],
        "answer": row["answer"],
        "clues": [row["clue1"], row["clue2"], row["clue3"]],
        "meta": meta,
    }


@app.route("/c/<card_id>")
def card_view(card_id):
    """
    Player-facing:
      step=0 -> show nothing
      step=1..3 -> show ONLY the current clue (no previous)
      step=4 -> show answer
    """
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

    clues = [row["clue1"], row["clue2"], row["clue3"]]
    current_clue = None
    reveal_answer = False

    if 1 <= step <= 3:
        current_clue = clues[step - 1]
    elif step == 4:
        reveal_answer = True

    # Your card.html must use:
    # - mode
    # - step
    # - current_clue
    # - reveal_answer
    # - answer
    # - card_id
    return render_template(
        "card.html",
        mode=row["mode"],
        step=step,
        current_clue=current_clue,
        reveal_answer=reveal_answer,
        answer=row["answer"],
        card_id=card_id,
    )

@app.route("/_initdb")
def initdb_route():
    init_db()
    return "DB initialized. cards table created (if missing)."


# ----------------------------
# Entry
# ----------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
