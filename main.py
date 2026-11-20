#!/usr/bin/env python3
"""
Repack X Auto-Importer v7.1 (Railway-safe)
============================================
Imports FitGirl and GOG games into Repack X database.

Anti-detection: random delays between ALL requests, human User-Agent,
low import rate per cycle.

Env:
  REPACK_X_ADMIN_SECRET   or  ADMIN_SECRET   (required)
  REPACK_X_SITE_URL       (default https://repackx.apps.mistral.run)
  REPACK_X_DOWNLOAD_BASE  (default https://repack-x-database.vercel.app)
  FITGIRL_API_BASE_URL    (default https://fitgirl-api-5rbz.onrender.com)
  RAWG_API_BASE_URL       (default https://api.rawg.io)
  RAWG_API_KEY            (optional)
  GOG_SCRAPE_API_URL      (default https://repackx-gscrape.onrender.com)
  GOG_SCRAPE_API_KEY      (default @Jithula2009)
  FITGIRL_IMPORT_LIMIT    (default 3)
  GOG_IMPORT_LIMIT        (default 1)
  FITGIRL_IMPORT_DELAY    (default 3.0)   -- seconds between creates
  FITGIRL_IMPORT_INTERVAL (default 3600)
  HEALTH_PORT / HEALTH_HOST
  FITGIRL_IMPORT_ONCE / FITGIRL_IMPORT_DRY_RUN
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
VERSION = "7.1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("repackx")

# ─── Human-like behavior ─────────────────────────────────────────────────────

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


def polite_pause(lo: float = 1.5, hi: float = 4.0) -> None:
    """Random human-like delay between requests."""
    delay = random.uniform(lo, hi)
    log.debug("  ⏳ polite pause %.1fs", delay)
    time.sleep(delay)


def polite_pause_long(lo: float = 3.0, hi: float = 8.0) -> None:
    """Longer pause between importing games."""
    delay = random.uniform(lo, hi)
    log.debug("  ⏳ long pause %.1fs", delay)
    time.sleep(delay)


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
RAWG_API = (_first_env("RAWG_API_BASE_URL") or "https://api.rawg.io").rstrip("/")
RAWG_KEY = (_first_env("RAWG_API_KEY") or "").strip()
GOG_SCRAPE = (_first_env("GOG_SCRAPE_API_URL") or "https://repackx-gscrape.onrender.com").rstrip("/")
GOG_SCRAPE_KEY = (_first_env("GOG_SCRAPE_API_KEY") or "@Jithula2009").strip()

ADMIN_SECRET = _first_env("REPACK_X_ADMIN_SECRET", "ADMIN_SECRET", default="") or ""

GOG_CATALOG_API = "https://catalog.gog.com/v1/catalog"
GOG_V2_API = "https://api.gog.com/v2/games"

FITGIRL_LIMIT = ARGS.fitgirl_limit or int(os.environ.get("FITGIRL_IMPORT_LIMIT", "3"))
GOG_LIMIT = ARGS.gog_limit or int(os.environ.get("GOG_IMPORT_LIMIT", "1"))
INTERVAL = ARGS.interval or int(os.environ.get("FITGIRL_IMPORT_INTERVAL", "3600"))
CREATE_DELAY = ARGS.delay if ARGS.delay is not None else float(os.environ.get("FITGIRL_IMPORT_DELAY", "3.0"))
HEALTH_PORT = ARGS.health_port or int(os.environ.get("HEALTH_PORT", "8080"))
HEALTH_HOST = ARGS.health_host or os.environ.get("HEALTH_HOST", "0.0.0.0")
ONCE = ARGS.once or os.environ.get("FITGIRL_IMPORT_ONCE", "").lower() in {"1", "true", "yes"}
DRY = ARGS.dry_run or os.environ.get("FITGIRL_IMPORT_DRY_RUN", "").lower() in {"1", "true", "yes"}

log.info("%s v%s", APP_NAME, VERSION)
log.info("site=%s  fitgirl=%s  gog=%s", SITE_URL, FITGIRL_API, GOG_SCRAPE)
log.info("fitgirl_limit=%d gog_limit=%d interval=%ds delay=%.1fs", FITGIRL_LIMIT, GOG_LIMIT, INTERVAL, CREATE_DELAY)
if not ADMIN_SECRET:
    log.warning("no admin secret set")

# ─── HTTP helpers (with polite pauses) ──────────────────────────────────────

def http_get(url: str, timeout: int = 45) -> Any | None:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        log.warning("GET %s -> %d", url, e.code)
        return None
    except Exception as e:
        log.warning("GET %s -> %s", url, e)
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


# ─── Small utils ─────────────────────────────────────────────────────────────

def compact(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).strip()


def clean_game_title(title: str) -> str:
    t = (title or "").strip()
    t = re.sub(r"[™®©]", "", t)
    t = re.sub(r"^[><!~]+", "", t)
    t = re.sub(r"[_~]+$", "", t)
    t = t.replace("_", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t.capitalize() if t else "Unknown"


# ─── FitGirl API ────────────────────────────────────────────────────────────

def fg_latest(page: int = 1) -> list[dict]:
    data = http_get(f"{FITGIRL_API}/latest?page={page}")
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("results") or data.get("data") or data.get("games") or []


def fg_detail(slug: str) -> dict | None:
    return http_get(f"{FITGIRL_API}/repack/{slug}")


# ─── GOG APIs (with polite pauses) ──────────────────────────────────────────

def gog_catalog_info(title: str) -> dict:
    q = urllib.parse.quote(title)
    url = f"{GOG_CATALOG_API}?query={q}&limit=1"
    data = http_get(url, timeout=30)
    polite_pause(1.0, 2.5)  # pause between GOG catalog calls
    if not isinstance(data, dict):
        return {}
    products = data.get("products") or []
    if not products:
        return {}
    p = products[0]
    return {
        "cover": p.get("coverVertical") or p.get("cover") or "",
        "genres": [g.get("name", "").strip() for g in (p.get("genres") or []) if g.get("name")],
        "tags": [t.get("name", "").strip() for t in (p.get("tags") or []) if t.get("name")],
    }


def gog_sysreqs(gog_id: int) -> dict:
    data = http_get(f"{GOG_V2_API}/{gog_id}", timeout=30)
    polite_pause(1.0, 2.5)  # pause between GOG sysreq calls
    if not isinstance(data, dict):
        return {}
    embedded = data.get("_embedded", {})
    supported = embedded.get("supportedOperatingSystems", {}).get("_embedded", {})
    reqs_wrapper = supported.get("requirements", {})
    reqs_embedded = reqs_wrapper.get("_embedded", {})
    sysreqs_list = reqs_embedded.get("systemRequirements", [])
    out: dict[str, dict[str, str]] = {}
    for entry in sysreqs_list:
        os_name = entry.get("name", "")
        value = entry.get("value", "")
        if os_name and value:
            out[os_name] = {"recommended": value}
    return out


def gog_all_games() -> list[dict]:
    url = f"{GOG_SCRAPE}/api/all-games"
    if GOG_SCRAPE_KEY:
        url += f"?key={urllib.parse.quote(GOG_SCRAPE_KEY)}"
    data = http_get(url, timeout=30)
    if not isinstance(data, dict):
        return []
    return data.get("games") or []


# ─── RAWG (minimal, with pauses) ────────────────────────────────────────────

def rawg_search(q: str) -> dict | None:
    url = f"{RAWG_API}/games?search={urllib.parse.quote(q)}&page_size=1"
    if RAWG_KEY:
        url += f"&key={urllib.parse.quote(RAWG_KEY)}"
    data = http_get(url, timeout=30)
    polite_pause(1.0, 2.5)  # pause between RAWG calls
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    return results[0] if results else None


def rawg_detail(rawg_id: int) -> dict:
    url = f"{RAWG_API}/games/{rawg_id}"
    if RAWG_KEY:
        url += f"?key={urllib.parse.quote(RAWG_KEY)}"
    data = http_get(url, timeout=30)
    polite_pause(1.0, 2.5)  # pause between RAWG calls
    return data if isinstance(data, dict) else {}


def rawg_screenshots(rawg_slug: str) -> list[str]:
    url = f"{RAWG_API}/games/{rawg_slug}/screenshots"
    if RAWG_KEY:
        url += f"?key={urllib.parse.quote(RAWG_KEY)}"
    data = http_get(url, timeout=30)
    polite_pause(1.0, 2.5)  # pause between RAWG calls
    if not isinstance(data, dict):
        return []
    return [r.get("image", "") for r in (data.get("results") or []) if r.get("image")]


def rawg_enrich(title: str) -> dict:
    """Minimal RAWG for GOG games: description + screenshots."""
    short = re.sub(r"\s*\((?:GOG|Fitgirl|FitGirl|Repack)\).*$", "", title).strip()
    game = rawg_search(short)
    if not game:
        return {"description": "", "screenshots": []}
    rid = game.get("id")
    rslug = game.get("slug", "")
    desc = rawg_detail(rid).get("description_raw", "") if rid else ""
    polite_pause(1.0, 2.5)
    shots = rawg_screenshots(rslug) if rslug else []
    return {"description": compact(desc), "screenshots": shots}


# ─── Dedup ───────────────────────────────────────────────────────────────────

def fetch_existing_slugs(base: str) -> set[str]:
    slugs: set[str] = set()
    for path in ("/api/games/all-slugs",):
        data = http_get(f"{base}{path}", timeout=30)
        if not data:
            continue
        items = data if isinstance(data, list) else (
            data.get("slugs") or data.get("data") or data.get("games") or data.get("results") or []
        )
        if isinstance(items, dict):
            items = list(items.values()) if all(isinstance(v, str) for v in items.values()) else []
        for it in items or []:
            if isinstance(it, str):
                slugs.add(it)
            elif isinstance(it, dict) and it.get("slug"):
                slugs.add(it["slug"])
    return slugs


# ─── Create game ─────────────────────────────────────────────────────────────

def create_game(payload: dict) -> tuple[bool, str]:
    if DRY:
        log.info("[dry-run] %s", json.dumps(payload, ensure_ascii=False)[:500])
        return True, "dry-run"
    url = f"{SITE_URL}/api/games/admin?secret={urllib.parse.quote(ADMIN_SECRET)}"
    code, body = post_json(url, payload)
    if code in (200, 201):
        log.info("  ✓ created: %s", payload.get("title"))
        return True, "created"
    if code in (400, 409):
        log.info("  - skip: %s", payload.get("title"))
        return True, "duplicate"
    log.error("  ✗ create failed %s -> %s: %s", code, payload.get("title"), str(body)[:300])
    return False, f"error_{code}"


# ─── Payload builders ───────────────────────────────────────────────────────

def base_payload(title: str, download_url: str, slug: str, repacker: str,
                 size_original: str, size_repack: str, categories: list[str],
                 description: str, cover_url: str, screenshots: list[str],
                 system_requirements: dict) -> dict:
    return {
        "title": title, "download_url": download_url, "slug": slug,
        "repacker": repacker, "size_original": size_original, "size_repack": size_repack,
        "categories": categories, "description": description, "cover_url": cover_url,
        "screenshot_urls": screenshots, "system_requirements": system_requirements,
        "featured": False, "trending": False, "is_published": True,
    }


def build_fitgirl_payload(detail: dict) -> dict:
    slug = detail.get("slug", "").strip()
    clean = clean_game_title(detail.get("title", slug))
    title = f"{clean} (Fitgirl Repack)"
    cats: list[str] = []
    cat = detail.get("category")
    if cat:
        if isinstance(cat, str) and cat.strip().lower() not in ("", "none", "other"):
            cats.append(cat.strip())
        elif isinstance(cat, dict):
            n = cat.get("name") or cat.get("title") or ""
            if n.strip():
                cats.append(n.strip())
    for key in ("genres", "tags"):
        v = detail.get(key)
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    n = item.get("name") or item.get("title") or ""
                    if n.strip() and n.strip() not in cats:
                        cats.append(n.strip())
                elif isinstance(item, str) and item.strip() and item.strip() not in cats:
                    cats.append(item.strip())
    return base_payload(
        title=title, download_url=f"{DOWNLOAD_BASE}/fg-{slug}", slug=slug, repacker="FitGirl",
        size_original=detail.get("originalSize", "Unknown"),
        size_repack=detail.get("repackSize", "Unknown"),
        categories=cats,
        description=compact(detail.get("gameDescription", "")),
        cover_url=detail.get("cover") or "",
        screenshots=detail.get("screenshots") or [],
        system_requirements={},
    )


def build_gog_payload(game: dict, catalog: dict, rawg: dict, sysreq: dict) -> dict:
    gog_id = game.get("id")
    slug = f"gog-{game.get('slug', gog_id)}"
    clean = clean_game_title(game.get("title", ""))
    title = f"{clean} (GOG Repack)"
    cats = list(dict.fromkeys(catalog.get("genres", []) + catalog.get("tags", [])))
    cats = [c for c in cats if c and c.lower() not in ("other", "none")]
    return base_payload(
        title=title, download_url=f"{DOWNLOAD_BASE}/gog-{gog_id}", slug=slug, repacker="GOG",
        size_original=game.get("size_original") or "Unknown",
        size_repack=game.get("size") or "Unknown",
        categories=cats,
        description=rawg.get("description", ""),
        cover_url=catalog.get("cover", "") or "",
        screenshots=rawg.get("screenshots", []),
        system_requirements=sysreq,
    )


# ─── Main cycle ─────────────────────────────────────────────────────────────

def import_fitgirl(existing: set[str]) -> int:
    imported = 0
    for page in (1,):
        if imported >= FITGIRL_LIMIT:
            break
        items = fg_latest(page)
        log.info("[fitgirl] page %d: %d releases", page, len(items))
        if not items:
            break
        for item in items:
            if imported >= FITGIRL_LIMIT:
                break
            slug = item.get("slug", "").strip()
            if not slug or slug in existing:
                continue
            log.info("[fitgirl] new release: %s", slug)
            polite_pause(1.5, 3.0)  # polite pause before fetching detail
            detail = fg_detail(slug)
            if not detail:
                log.warning("  fetch failed, skip")
                continue
            payload = build_fitgirl_payload(detail)
            ok, _ = create_game(payload)
            if ok:
                imported += 1
                existing.add(slug)
                polite_pause_long(CREATE_DELAY, CREATE_DELAY + 3)  # long pause after create
    return imported


def import_gog(existing: set[str]) -> int:
    imported = 0
    all_games = gog_all_games()
    if not all_games:
        log.info("[gog] no games found")
        return 0
    gog_existing = {s for s in existing if s.startswith("gog-")}
    total = len(all_games)
    backlog = total - len(gog_existing)
    if backlog <= 0:
        log.info("[gog] all %d games already imported", total)
    else:
        log.info("[gog] backlog: %d/%d imported, %d remaining", len(gog_existing), total, backlog)
    for game in all_games:
        if imported >= GOG_LIMIT:
            break
        slug = f"gog-{game.get('slug', game.get('id'))}"
        if slug in existing:
            continue
        title = game.get("title", "")
        log.info("[gog] new game: %s", title)
        polite_pause(1.5, 3.0)
        catalog = gog_catalog_info(title)
        rawg = rawg_enrich(title)
        sysreq = gog_sysreqs(game.get("id", 0))
        payload = build_gog_payload(game, catalog, rawg, sysreq)
        ok, _ = create_game(payload)
        if ok:
            imported += 1
            existing.add(slug)
            polite_pause_long(CREATE_DELAY, CREATE_DELAY + 3)
    return imported


def run_cycle() -> dict:
    log.info("── cycle start ──")
    try:
        existing = fetch_existing_slugs(SITE_URL)
        fg_count = len([s for s in existing if not s.startswith("gog-")])
        gog_count = len([s for s in existing if s.startswith("gog-")])
        log.info("site slugs: %d (fg=%d gog=%d)", len(existing), fg_count, gog_count)
    except Exception as e:
        log.error("failed to fetch slugs: %s", e)
        return {"error": str(e)}
    fg = import_fitgirl(existing)
    gog = import_gog(existing)
    log.info("── cycle done: fitgirl=%d gog=%d ──", fg, gog)
    return {"fitgirl": fg, "gog": gog}


# ─── Health server ──────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _json(self, code: int, obj: Any):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            self._json(200, {"working": True, "version": VERSION})
        elif self.path == "/api/status":
            self._json(200, {"working": True, "site": SITE_URL, "interval": INTERVAL})
        else:
            html = f"""<!DOCTYPE html><html><head><title>{APP_NAME}</title>
<style>body{{font-family:system-ui;background:#0b0b10;color:#ddd;display:grid;place-items:center;height:100vh}}
.card{{background:#12121a;padding:2rem;border-radius:12px;text-align:center}}</style></head>
<body><div class="card"><h2>🎮 {APP_NAME}</h2>
<p>Version {VERSION} · Running ✅</p>
<p style="color:#888">Railway-safe mode</p></div></body></html>"""
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
