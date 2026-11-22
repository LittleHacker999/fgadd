#!/usr/bin/env python3
"""
Repack X Auto-Importer v7.8
===========================
- NO system requirements (added manually by site owner).
- Dedup uses ONLY https://repackx.apps.mistral.run/api/games/all-slugs:
  exact slug match + TITLE-KEY match (site-generated slugs like
  "void-reaver-fitgirl-repack" are normalized back to the game title key).
  Local state file is a secondary guard.
- Cycle ABORTS if all-slugs can't be loaded (no duplicates possible).
- Repacker: exactly "Fitgirl" / "GOG".
- GOG Catalog: top-5 title matching; no match -> NO cover image.
- Screenshots: only URLs ending with image extensions.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

APP_NAME = "Repack X Auto-Importer"
VERSION = "7.8"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("repackx")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")


def polite_pause(lo: float = 1.5, hi: float = 4.0) -> None:
    time.sleep(random.uniform(lo, hi))


def polite_pause_long(lo: float = 3.0, hi: float = 8.0) -> None:
    time.sleep(random.uniform(lo, hi))


# ─── Env / CLI ───────────────────────────────────────────────────────────────

def _first_env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(APP_NAME)
    p.add_argument("--interval", type=int, default=None)
    p.add_argument("--delay", type=float, default=None)
    p.add_argument("--fitgirl-limit", type=int, default=None)
    p.add_argument("--gog-limit", type=int, default=None)
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--health-port", type=int, default=None)
    p.add_argument("--health-host", type=str, default=None)
    return p.parse_args()


ARGS = parse_args()

SITE_URL = (_first_env("REPACK_X_SITE_URL") or "https://repackx.apps.mistral.run").rstrip("/")
DOWNLOAD_BASE = (_first_env("REPACK_X_DOWNLOAD_BASE") or "https://repack-x-database.vercel.app").rstrip("/")
FITGIRL_API = (_first_env("FITGIRL_API_BASE_URL") or "https://fitgirl-api-5rbz.onrender.com").rstrip("/")
GOG_SCRAPE = (_first_env("GOG_SCRAPE_API_URL") or "https://repackx-gscrape.onrender.com").rstrip("/")
GOG_SCRAPE_KEY = (_first_env("GOG_SCRAPE_API_KEY") or "@Jithula2009").strip()
ADMIN_SECRET = _first_env("REPACK_X_ADMIN_SECRET", "ADMIN_SECRET", default="") or ""

GOG_CATALOG_API = "https://catalog.gog.com/v1/catalog"

FITGIRL_LIMIT = ARGS.fitgirl_limit or int(os.environ.get("FITGIRL_IMPORT_LIMIT", "3"))
GOG_LIMIT = ARGS.gog_limit or int(os.environ.get("GOG_IMPORT_LIMIT", "1"))
INTERVAL = ARGS.interval or int(os.environ.get("FITGIRL_IMPORT_INTERVAL", "3600"))
CREATE_DELAY = ARGS.delay if ARGS.delay is not None else float(os.environ.get("FITGIRL_IMPORT_DELAY", "3.0"))
HEALTH_PORT = ARGS.health_port or int(os.environ.get("HEALTH_PORT", "8080"))
HEALTH_HOST = ARGS.health_host or os.environ.get("HEALTH_HOST", "0.0.0.0")
ONCE = ARGS.once or os.environ.get("FITGIRL_IMPORT_ONCE", "").lower() in {"1", "true", "yes"}
DRY = ARGS.dry_run or os.environ.get("FITGIRL_IMPORT_DRY_RUN", "").lower() in {"1", "true", "yes"}
STATE_FILE = os.environ.get("IMPORTED_STATE_FILE", "imported_slugs.json")
NO_LINKS_RECHECK = int(os.environ.get("NO_LINKS_RECHECK_SECONDS", "21600"))
MAX_FAILURES = int(os.environ.get("MAX_FAILURES_PER_CYCLE", "3"))
GOG_MATCH_MIN = float(os.environ.get("GOG_CATALOG_MATCH_MIN", "0.5"))

log.info("%s v%s", APP_NAME, VERSION)
log.info("site=%s fitgirl=%s gog=%s", SITE_URL, FITGIRL_API, GOG_SCRAPE)
log.info("fitgirl_limit=%d gog_limit=%d interval=%ds", FITGIRL_LIMIT, GOG_LIMIT, INTERVAL)
if not ADMIN_SECRET:
    log.warning("no admin secret set")


# ─── HTTP helpers ────────────────────────────────────────────────────────────

def http_get(url: str, timeout: int = 45, retries: int = 1) -> Any | None:
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            log.warning("GET %s -> %d", url.split("?")[0], e.code)
            return None
        except Exception as e:
            log.warning("GET %s -> %s%s", url.split("?")[0], e,
                        " (retrying)" if attempt < retries else "")
            if attempt < retries:
                time.sleep(random.uniform(2.0, 4.0))
    return None


def post_json(url: str, payload: dict, timeout: int = 90) -> tuple[int, Any]:
    headers = dict(_HEADERS)
    headers["Content-Type"] = "application/json"
    headers["x-admin-secret"] = ADMIN_SECRET
    headers["Authorization"] = f"Bearer {ADMIN_SECRET}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, str(e)


# ─── Safe type helpers ───────────────────────────────────────────────────────

def safe_get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else default


def safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    if isinstance(val, str):
        return val
    if isinstance(val, (bool, int, float)):
        return str(val)
    if isinstance(val, (list, tuple)):
        parts = []
        for item in val:
            s = safe_str(item, "").strip()
            if s:
                parts.append(s)
        return ", ".join(parts)
    if isinstance(val, dict):
        for k in ("name", "title", "value", "text", "label", "url", "image", "href", "src"):
            v = val.get(k)
            if v and isinstance(v, (str, int, float)):
                return str(v)
            if v:
                return safe_str(v, default)
        return default
    return str(val) if val else default


def safe_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, (list, tuple)):
        out = []
        for item in val:
            if isinstance(item, (list, tuple)):
                out.extend(safe_str_list(item))
            else:
                s = safe_str(item, "").strip()
                if s:
                    out.append(s)
        return out
    s = safe_str(val, "").strip()
    return [s] if s else []


def safe_url(val: Any) -> str:
    s = safe_str(val, "").strip()
    return s if s.lower().startswith(("http://", "https://", "magnet:")) else ""


# ─── Utils ───────────────────────────────────────────────────────────────────

def norm_slug(s: Any) -> str:
    return safe_str(s, "").strip().lower()


def clean_game_title(title: Any) -> str:
    t = safe_str(title, "").strip()
    t = re.sub(r"[™®©]", "", t)
    t = re.sub(r"^[><!~]+", "", t)
    t = re.sub(r"[_~]+$", "", t)
    t = t.replace("_", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t.capitalize() if t else "Unknown"


def clean_description(text: Any) -> str:
    if isinstance(text, (list, tuple)):
        parts = [clean_description(item) for item in text]
        return "\n\n".join(p for p in parts if p)
    s = safe_str(text, "")
    s = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote|ul|ol)[^>]*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for old, new in [
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
        ("&#39;", "'"), ("&rsquo;", "'"), ("&lsquo;", "'"), ("&nbsp;", " "),
        ("&hellip;", "..."), ("&mdash;", "\u2014"), ("&ndash;", "\u2013"),
    ]:
        s = s.replace(old, new)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def _extract_url(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for k in ("url", "image", "src", "href", "link", "full", "large"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def filter_image_urls(urls: Any) -> list[str]:
    if urls is None:
        return []
    items = urls if isinstance(urls, (list, tuple)) else [urls]
    result = []
    for item in items:
        clean = safe_url(_extract_url(item))
        if not clean:
            continue
        path = clean.lower().split("?")[0].split("#")[0]
        if any(path.endswith(ext) for ext in IMAGE_EXTS):
            result.append(clean)
    return result


# ─── Dedup keys (title-based, built from all-slugs) ─────────────────────────

def _title_to_key(title: Any) -> str:
    """Game title -> alphanumeric key. 'Void Reaver (Fitgirl Repack)' -> 'voidreaver'"""
    t = safe_str(title, "").strip().lower()
    t = re.sub(r"\s*\((?:fitgirl|gog|repack|fitgirl repack|gog repack)\)\s*$", "", t, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", t)


def _is_gog_slug(s: str) -> bool:
    return s.startswith("gog-") or s.endswith("-gog-repack") or s.endswith("-gog")


def _site_slug_to_game_key(slug: str) -> str:
    """Site slug -> game key. Handles site-generated slugs:
    'void-reaver-fitgirl-repack' -> 'voidreaver'
    'gog-observer' -> 'observer'
    'observer-gog-repack' -> 'observer'
    'cyberpunk-2077' -> 'cyberpunk2077'
    """
    s = norm_slug(slug)
    s = re.sub(r"^(gog|fg)[-:]", "", s)
    s = re.sub(r"[-_ ](fitgirl|gog)[-_ ]repack$", "", s)
    s = re.sub(r"[-_ ](fitgirl|gog)$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


# ─── Local state (secondary guard) ──────────────────────────────────────────

_state_lock = threading.Lock()


def load_state() -> set[str]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.warning("state load failed: %s", e)
        return set()
    out: set[str] = set()
    for s in data if isinstance(data, list) else []:
        if not isinstance(s, str):
            continue
        s = norm_slug(s)
        if s.startswith(("fg:", "gog:")):
            out.add(s)
        elif s.startswith("gog-"):
            out.add(f"gog:{s}")
        else:
            out.add(f"fg:{s}")
    return out


def save_state(state: set[str]) -> None:
    try:
        with _state_lock:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(state), f)
    except Exception as e:
        log.warning("state save failed: %s", e)


# ─── Download-link check ────────────────────────────────────────────────────

def _collect_links(obj: Any, out: list[str]) -> None:
    if isinstance(obj, str):
        s = obj.strip()
        if s.lower().startswith(("http://", "https://", "magnet:")):
            out.append(s)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_links(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_links(v, out)


def has_download_links(detail: dict) -> bool:
    links: list[str] = []
    _collect_links(safe_get(detail, "downloadMirrors"), links)
    _collect_links(safe_get(detail, "torrents"), links)
    return len(links) > 0


# ─── FitGirl API ────────────────────────────────────────────────────────────

def fg_latest(page: int = 1) -> list[dict]:
    data = http_get(f"{FITGIRL_API}/latest?page={page}")
    if not data:
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        results = safe_get(data, "results") or safe_get(data, "data") or safe_get(data, "games") or []
        return [d for d in results if isinstance(d, dict)]
    return []


def fg_detail(slug: str) -> dict | None:
    d = http_get(f"{FITGIRL_API}/repack/{slug}")
    return d if isinstance(d, dict) else None


# ─── GOG APIs ───────────────────────────────────────────────────────────────

def _extract_gog_names(items: Any) -> list[str]:
    if not isinstance(items, (list, tuple)):
        return []
    out = []
    for item in items:
        if isinstance(item, dict):
            n = safe_str(safe_get(item, "name") or safe_get(item, "title") or safe_get(item, "label") or "").strip()
            if n:
                out.append(n)
        elif isinstance(item, (list, tuple)):
            n = safe_str(item[-1] if item else "").strip()
            if n:
                out.append(n)
        elif isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
    return out


def title_match_score(a: Any, b: Any) -> float:
    na = re.sub(r"[^a-z0-9]", "", safe_str(a).lower())
    nb = re.sub(r"[^a-z0-9]", "", safe_str(b).lower())
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    ta = {w for w in re.sub(r"[^a-z0-9 ]+", " ", safe_str(a).lower()).split() if w}
    tb = {w for w in re.sub(r"[^a-z0-9 ]+", " ", safe_str(b).lower()).split() if w}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def gog_catalog_info(title: str) -> dict:
    """Best title match from top 5 catalog results. No good match -> empty (no cover)."""
    empty = {"cover": "", "genres": [], "tags": []}
    query = clean_game_title(title)
    if not query or query == "Unknown":
        return empty
    url = f"{GOG_CATALOG_API}?query={urllib.parse.quote(query)}&limit=5"
    data = http_get(url, timeout=30)
    polite_pause(1.0, 2.5)
    if not isinstance(data, dict):
        return empty
    products = safe_get(data, "products")
    if not isinstance(products, (list, tuple)) or not products:
        log.info("[gog] catalog: no results for '%s'", query)
        return empty

    best: dict | None = None
    best_score = 0.0
    for p in products[:5]:
        if not isinstance(p, dict):
            continue
        ptitle = safe_get(p, "title") or safe_get(p, "name")
        score = title_match_score(query, ptitle)
        if score > best_score:
            best, best_score = p, score

    if best is None or best_score < GOG_MATCH_MIN:
        log.info("[gog] catalog: no good match for '%s' (best=%.2f) — no cover attached",
                 query, best_score)
        return empty

    found_title = safe_str(safe_get(best, "title") or safe_get(best, "name")).strip()
    log.info("[gog] catalog match: '%s' -> '%s' (score=%.2f)", query, found_title, best_score)
    return {
        "cover": safe_url(safe_get(best, "coverVertical") or safe_get(best, "cover")),
        "genres": _extract_gog_names(safe_get(best, "genres")),
        "tags": _extract_gog_names(safe_get(best, "tags")),
    }


def gog_all_games() -> list[dict]:
    url = f"{GOG_SCRAPE}/api/all-games"
    if GOG_SCRAPE_KEY:
        url += f"?key={urllib.parse.quote(GOG_SCRAPE_KEY)}"
    data = http_get(url, timeout=30)
    if not isinstance(data, dict):
        return []
    games = safe_get(data, "games") or []
    return [g for g in games if isinstance(g, dict)]


# ─── Site data: all-slugs is the ONLY source of truth ───────────────────────

def fetch_site_data(base: str) -> tuple[set[str], set[str], set[str]] | None:
    """
    Returns (exact_slugs, fitgirl_title_keys, gog_title_keys) from all-slugs.
    Returns None if the fetch fails or is empty -> cycle must abort.
    """
    data = http_get(f"{base}/api/games/all-slugs", timeout=45)
    if data is None:
        return None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = (safe_get(data, "slugs") or safe_get(data, "data") or
                 safe_get(data, "games") or safe_get(data, "results") or [])
    else:
        items = []
    if isinstance(items, dict):
        items = list(items.values())

    slugs: set[str] = set()
    fg_keys: set[str] = set()
    gog_keys: set[str] = set()

    def add_slug(s: str) -> None:
        n = norm_slug(s)
        if not n:
            return
        slugs.add(n)
        k = _site_slug_to_game_key(n)
        if not k:
            return
        if _is_gog_slug(n):
            gog_keys.add(k)
        else:
            fg_keys.add(k)

    for it in items or []:
        if isinstance(it, str):
            add_slug(it)
        elif isinstance(it, dict):
            add_slug(safe_str(safe_get(it, "slug", "")))
            t = safe_str(safe_get(it, "title", "")).strip()
            if t:
                tk = _title_to_key(t)
                tl = t.lower()
                if not tk:
                    continue
                if "gog" in tl:
                    gog_keys.add(tk)
                elif "fitgirl" in tl:
                    fg_keys.add(tk)
                else:
                    fg_keys.add(tk)
                    gog_keys.add(tk)

    if not slugs and not fg_keys and not gog_keys:
        return None
    return slugs, fg_keys, gog_keys


# ─── Create game ─────────────────────────────────────────────────────────────

def create_game(payload: dict) -> tuple[bool, str]:
    if DRY:
        log.info("[dry-run] %s", json.dumps(payload, ensure_ascii=False)[:500])
        return True, "dry-run"
    url = f"{SITE_URL}/api/games/admin?secret={urllib.parse.quote(ADMIN_SECRET)}"
    code, body = post_json(url, payload)
    if code in (200, 201):
        log.info("  ✓ created: %s | repacker=%s | slug=%s",
                 payload.get("title"), payload.get("repacker"), payload.get("slug"))
        return True, "created"
    if code == 409:
        log.info("  - already exists on site: %s", payload.get("title"))
        return True, "duplicate"
    log.error("  ✗ create failed %s -> %s | slug=%s | repacker=%s | body: %s",
              code, payload.get("title"), payload.get("slug"),
              payload.get("repacker"), str(body)[:400])
    return False, f"error_{code}"


# ─── Payload builders (NO system requirements) ──────────────────────────────

def base_payload(*, title, download_url, slug, repacker, size_original, size_repack,
                 categories, description, cover_url, screenshots) -> dict:
    return {
        "title": safe_str(title, "Unknown"),
        "download_url": safe_str(download_url, ""),
        "slug": safe_str(slug, ""),
        "repacker": safe_str(repacker, "Unknown"),
        "size_original": safe_str(size_original, "Unknown"),
        "size_repack": safe_str(size_repack, "Unknown"),
        "categories": [safe_str(c, "").strip() for c in (categories or []) if safe_str(c, "").strip()],
        "description": clean_description(description),
        "cover_url": safe_url(cover_url),
        "screenshot_urls": filter_image_urls(screenshots),
        "system_requirements": {},
        "featured": False,
        "trending": False,
        "is_published": True,
    }


def _extract_categories(detail: dict) -> list[str]:
    cats: list[str] = []
    cat = safe_get(detail, "category")
    if isinstance(cat, str):
        s = cat.strip()
        if s and s.lower() not in ("", "none", "other"):
            cats.append(s)
    elif isinstance(cat, dict):
        n = safe_str(safe_get(cat, "name") or safe_get(cat, "title") or "").strip()
        if n and n.lower() not in ("none", "other"):
            cats.append(n)
    for key in ("genres", "tags"):
        for s in safe_str_list(safe_get(detail, key)):
            if s and s.lower() not in ("none", "other") and s not in cats:
                cats.append(s)
    return cats


def build_fitgirl_payload(detail: dict) -> dict:
    slug = safe_str(safe_get(detail, "slug", "")).strip()
    clean = clean_game_title(safe_get(detail, "title", slug))
    return base_payload(
        title=f"{clean} (Fitgirl Repack)",
        download_url=f"{DOWNLOAD_BASE}/fg-{slug}",
        slug=slug,
        repacker="Fitgirl",
        size_original=safe_get(detail, "originalSize") or "Unknown",
        size_repack=safe_get(detail, "repackSize") or "Unknown",
        categories=_extract_categories(detail),
        description=safe_get(detail, "gameDescription", ""),
        cover_url=safe_get(detail, "cover"),
        screenshots=safe_get(detail, "screenshots"),
    )


def build_gog_payload(game: dict, catalog: dict) -> dict:
    gog_id = safe_get(game, "id")
    gog_slug = safe_str(safe_get(game, "slug", "")).strip() or safe_str(gog_id)
    clean = clean_game_title(safe_get(game, "title", ""))
    cats = list(dict.fromkeys(
        safe_get(catalog, "genres", []) + safe_get(catalog, "tags", [])
    ))
    cats = [c for c in cats if c and c.lower() not in ("other", "none")]
    return base_payload(
        title=f"{clean} (GOG Repack)",
        download_url=f"{DOWNLOAD_BASE}/gog-{gog_id}",
        slug=f"gog-{gog_slug}",
        repacker="GOG",
        size_original=safe_get(game, "size_original") or "Unknown",
        size_repack=safe_get(game, "size") or "Unknown",
        categories=cats,
        description=safe_get(game, "description", ""),
        cover_url=safe_get(catalog, "cover", ""),
        screenshots=safe_get(game, "screenshots"),
    )


# ─── Import flows ───────────────────────────────────────────────────────────

NO_LINKS_SEEN: dict[str, float] = {}
STATS: dict[str, Any] = {"last_cycle": None, "last_result": None}


def import_fitgirl(site_slugs: set[str], fg_keys: set[str], state: set[str]) -> dict:
    imported = skipped_no_links = skipped_dup = failures = 0
    items = fg_latest(1)
    log.info("[fitgirl] page 1: %d releases", len(items))
    processed: set[str] = set()

    for item in items:
        if imported >= FITGIRL_LIMIT or failures >= MAX_FAILURES:
            break
        try:
            slug = safe_str(safe_get(item, "slug", "")).strip()
            key = norm_slug(slug)
            if not key or key in processed:
                continue
            processed.add(key)

            # Check 1: exact slug OR title key from all-slugs / state
            list_title = safe_str(safe_get(item, "title", "")).strip()
            list_tk = _title_to_key(list_title) if list_title else _site_slug_to_game_key(key)
            if key in site_slugs or f"fg:{key}" in state or (list_tk and list_tk in fg_keys):
                skipped_dup += 1
                continue

            last = NO_LINKS_SEEN.get(key)
            if last and time.time() - last < NO_LINKS_RECHECK:
                continue

            polite_pause(1.5, 3.0)
            detail = fg_detail(slug)
            if not detail:
                log.warning("[fitgirl] detail fetch failed: %s", slug)
                continue

            # Check 2: with real title/slug from detail
            detail_title = safe_str(safe_get(detail, "title", slug)).strip()
            detail_tk = _title_to_key(detail_title)
            real_slug = safe_str(safe_get(detail, "slug", "")).strip() or slug
            real_key = norm_slug(real_slug)
            if (real_key in site_slugs or f"fg:{real_key}" in state
                    or (detail_tk and detail_tk in fg_keys)):
                skipped_dup += 1
                log.info("[fitgirl] skip (already on site): %s", detail_title or slug)
                continue

            if not has_download_links(detail):
                NO_LINKS_SEEN[key] = time.time()
                skipped_no_links += 1
                log.info("[fitgirl] skip (no download links): %s", slug)
                continue

            detail["slug"] = real_slug
            log.info("[fitgirl] importing: %s", real_slug)
            ok, status = create_game(build_fitgirl_payload(detail))
            if not ok:
                failures += 1
                continue

            # Mark as existing: our slug + possible site-generated slug + title key
            site_slugs.add(real_key)
            site_slugs.add(key)
            site_slugs.add(f"{real_key}-fitgirl-repack")
            if detail_tk:
                fg_keys.add(detail_tk)
            if status != "dry-run":
                state.add(f"fg:{real_key}")
                save_state(state)
            NO_LINKS_SEEN.pop(key, None)
            if status in ("created", "dry-run"):
                imported += 1
            polite_pause_long(CREATE_DELAY, CREATE_DELAY + 3)
        except Exception as e:
            log.error("[fitgirl] error processing item: %s", e)
            failures += 1
            continue
    return {"imported": imported, "skipped_no_links": skipped_no_links,
            "skipped_dup": skipped_dup, "failures": failures}


def import_gog(site_slugs: set[str], gog_keys: set[str], state: set[str]) -> dict:
    imported = skipped_dup = failures = 0
    all_games = gog_all_games()
    if not all_games:
        log.info("[gog] no games found")
        return {"imported": 0, "skipped_dup": 0, "failures": 0}

    gog_on_site = {s for s in site_slugs if _is_gog_slug(s)}
    backlog = len(all_games) - len(gog_on_site)
    if backlog <= 0:
        log.info("[gog] all %d games already imported — watching for new ones", len(all_games))
    else:
        log.info("[gog] backlog: %d/%d imported, %d remaining",
                 len(gog_on_site), len(all_games), backlog)

    seen: set[str] = set()
    for game in all_games:
        if imported >= GOG_LIMIT or failures >= MAX_FAILURES:
            break
        try:
            base_slug = safe_str(safe_get(game, "slug", "")).strip() or safe_str(safe_get(game, "id", ""))
            gog_slug = f"gog-{base_slug}"
            key = norm_slug(gog_slug)
            if key in seen:
                continue
            seen.add(key)

            title = safe_str(safe_get(game, "title", "")).strip()
            tk = _title_to_key(title)

            # Check: exact gog-slug OR title key among GOG entries on site
            if key in site_slugs or f"gog:{key}" in state or (tk and tk in gog_keys):
                skipped_dup += 1
                continue

            log.info("[gog] importing: %s", title)
            polite_pause(1.5, 3.0)
            catalog = gog_catalog_info(title)
            ok, status = create_game(build_gog_payload(game, catalog))
            if not ok:
                failures += 1
                continue

            site_slugs.add(key)
            site_slugs.add(f"{base_slug}-gog-repack")
            if tk:
                gog_keys.add(tk)
            if status != "dry-run":
                state.add(f"gog:{key}")
                save_state(state)
            if status in ("created", "dry-run"):
                imported += 1
            polite_pause_long(CREATE_DELAY, CREATE_DELAY + 3)
        except Exception as e:
            log.error("[gog] error processing game: %s", e)
            failures += 1
            continue
    return {"imported": imported, "skipped_dup": skipped_dup, "failures": failures}


def run_cycle() -> dict:
    log.info("── cycle start ──")
    site = fetch_site_data(SITE_URL)
    if site is None:
        log.error("could not load %s/api/games/all-slugs — ABORTING cycle to avoid duplicates", SITE_URL)
        return {"error": "slugs_unavailable"}

    site_slugs, fg_keys, gog_keys = site
    state = load_state()
    # Merge local state into title keys (secondary guard)
    for s in state:
        if s.startswith("fg:"):
            k = _site_slug_to_game_key(s[3:])
            if k:
                fg_keys.add(k)
        elif s.startswith("gog:"):
            k = _site_slug_to_game_key(s[4:])
            if k:
                gog_keys.add(k)

    gog_n = sum(1 for s in site_slugs if _is_gog_slug(s))
    log.info("all-slugs: %d entries (fitgirl=%d gog=%d) | title keys: fg=%d gog=%d",
             len(site_slugs), len(site_slugs) - gog_n, gog_n, len(fg_keys), len(gog_keys))
    log.info("all-slugs sample: %s", sorted(site_slugs)[:5])

    fg = import_fitgirl(site_slugs, fg_keys, state)
    gog = import_gog(site_slugs, gog_keys, state)
    result = {"fitgirl": fg, "gog": gog}
    STATS["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATS["last_result"] = result
    log.info("── cycle done: fitgirl=%d (dup=%d no-links=%d) gog=%d (dup=%d) ──",
             fg["imported"], fg["skipped_dup"], fg["skipped_no_links"],
             gog["imported"], gog["skipped_dup"])
    return result


# ─── Health server ──────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, code: int, obj: Any):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            self._json(200, {"working": True})
        elif self.path == "/api/status":
            self._json(200, {"working": True, "version": VERSION, "site": SITE_URL,
                             "interval": INTERVAL, **STATS})
        else:
            html = f"""<!DOCTYPE html><html><head><title>{APP_NAME}</title>
<style>body{{font-family:system-ui;background:#0b0b10;color:#ddd;display:grid;place-items:center;height:100vh}}
.card{{background:#12121a;padding:2rem;border-radius:12px;text-align:center}}</style></head>
<body><div class="card"><h2>🎮 {APP_NAME}</h2>
<p>Version {VERSION} · Running ✅</p>
<p style="color:#888">Last cycle: {STATS['last_cycle'] or 'pending'}</p></div></body></html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def start_health_server(port: int, host: str):
    def _run():
        try:
            srv = ThreadingHTTPServer((host, port), HealthHandler)
            log.info("health server on http://%s:%d", host, port)
            srv.serve_forever()
        except Exception as e:
            log.error("health server: %s", e)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ─── Entry ──────────────────────────────────────────────────────────────────

def main() -> int:
    start_health_server(HEALTH_PORT, HEALTH_HOST)
    if ONCE:
        run_cycle()
        return 0
    log.info("continuous mode, every %ds", INTERVAL)
    while True:
        try:
            run_cycle()
        except Exception as e:
            log.error("cycle error: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("bye")
