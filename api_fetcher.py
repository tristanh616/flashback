# api_fetcher.py
import os, random, requests
from typing import List, Dict

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"
LANG = "en-US"

_session = requests.Session()
_cfg = None  # cached /configuration

def _tmdb(path: str, params: dict = None):
    if params is None:
        params = {}
    params["api_key"] = TMDB_API_KEY
    return _session.get(f"{TMDB_BASE}{path}", params=params, timeout=10)

def _ensure_config():
    global _cfg
    if _cfg: return _cfg
    r = _tmdb("/configuration")
    r.raise_for_status()
    _cfg = r.json()["images"]
    return _cfg

def _img_url(path: str, kind: str = "poster", size_pref: str = "w500") -> str:
    """
    kind: 'poster' | 'backdrop' | 'profile'
    size_pref: pick a middle-large size available
    """
    cfg = _ensure_config()
    base = cfg["secure_base_url"]
    sizes = {
        "poster": cfg.get("poster_sizes", []),
        "backdrop": cfg.get("backdrop_sizes", []),
        "profile": cfg.get("profile_sizes", []),
    }[kind]
    size = size_pref if size_pref in sizes else sizes[-2] if len(sizes) >= 2 else sizes[-1]
    return f"{base}{size}{path}"

def _pick_popular_movies(n=50) -> List[Dict]:
    """Pull a pool of popular movies with enough metadata."""
    r = _tmdb("/trending/movie/day", {"language": LANG})
    r.raise_for_status()
    results = r.json().get("results", [])
    # Fallback to discover if trending is thin
    if len(results) < 20:
        d = _tmdb("/discover/movie", {
            "language": LANG,
            "sort_by": "popularity.desc",
            "vote_count.gte": 200
        })
        d.raise_for_status()
        results = d.json().get("results", [])
    random.shuffle(results)
    return results[:n]

# -----------------------
# Generators per mode
# -----------------------

def gen_tmdb_poster_zoom(count=3) -> List[Dict]:
    pool = _pick_popular_movies(60)
    out = []
    for i, m in enumerate(pool):
        if len(out) >= count: break
        poster = m.get("poster_path")
        title = m.get("title") or m.get("name")
        if not poster or not title: continue
        out.append({
            "id": f"movie_poster_{i:04d}",
            "mode": "movie_poster",
            "type": "image",
            "prompt": None,
            "media_url": _img_url(poster, "poster"),
            "answer": title,
            "meta": {"year": (m.get("release_date") or "????")[:4]}
        })
    return out

def gen_tmdb_tagline(count=3) -> List[Dict]:
    pool = _pick_popular_movies(80)
    out = []
    for i, m in enumerate(pool):
        if len(out) >= count: break
        mid = m.get("id")
        if not mid: continue
        d = _tmdb(f"/movie/{mid}", {"language": LANG})
        if d.status_code != 200: continue
        j = d.json()
        tagline = (j.get("tagline") or "").strip()
        title = j.get("title")
        if not tagline or not title: continue
        # Filter very short/weird taglines
        if len(tagline) < 8: continue
        out.append({
            "id": f"movie_tagline_{i:04d}",
            "mode": "movie_tagline",
            "type": "text",
            "prompt": tagline,
            "media_url": None,
            "answer": title,
            "meta": {"year": (j.get("release_date") or "????")[:4]}
        })
    return out

def gen_tmdb_backdrop(count=3) -> List[Dict]:
    pool = _pick_popular_movies(80)
    out = []
    for i, m in enumerate(pool):
        if len(out) >= count: break
        mid = m.get("id")
        if not mid: continue
        imgs = _tmdb(f"/movie/{mid}/images", {"include_image_language": f"{LANG.split('-')[0]},{LANG},null"})
        if imgs.status_code != 200: continue
        backs = (imgs.json().get("backdrops") or [])
        random.shuffle(backs)
        # pick a reasonable size/backdrop
        pick = next((b for b in backs if b.get("file_path")), None)
        if not pick: continue
        title = m.get("title") or m.get("name")
        out.append({
            "id": f"movie_still_{i:04d}",
            "mode": "movie_still",
            "type": "image",
            "prompt": None,
            "media_url": _img_url(pick["file_path"], "backdrop"),
            "answer": title,
            "meta": {"aspect_ratio": pick.get("aspect_ratio")}
        })
    return out

def gen_tmdb_cast_only(count=3) -> List[Dict]:
    pool = _pick_popular_movies(100)
    out = []
    for i, m in enumerate(pool):
        if len(out) >= count: break
        mid = m.get("id")
        if not mid: continue
        credits = _tmdb(f"/movie/{mid}/credits", {"language": LANG})
        if credits.status_code != 200: continue
        cast = [c for c in credits.json().get("cast", []) if c.get("profile_path")]
        if len(cast) < 2: continue
        random.shuffle(cast)
        picks = cast[:min(3, len(cast))]
        faces = [_img_url(c["profile_path"], "profile") for c in picks]
        title = m.get("title") or m.get("name")
        out.append({
            "id": f"movie_cast_{i:04d}",
            "mode": "movie_cast",
            "type": "image",
            "prompt": None,
            "media_url": None,  # we’ll send faces via meta to render a small grid
            "answer": title,
            "meta": { "faces": faces, "names": [c["name"] for c in picks] }
        })
    return out

# PUBLIC API
def generate_questions_for_mode(mode: str, count: int = 3) -> List[Dict]:
    if not TMDB_API_KEY:
        # Fail loudly; your UI should show a friendly setup message if needed
        raise RuntimeError("TMDB_API_KEY not set in environment")
    table = {
        "movie_poster": gen_tmdb_poster_zoom,
        "movie_tagline": gen_tmdb_tagline,
        "movie_still": gen_tmdb_backdrop,
        "movie_cast": gen_tmdb_cast_only,
    }
    fn = table.get(mode)
    return fn(count) if fn else []
