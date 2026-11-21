#!/usr/bin/env python3
"""
Repack X Auto-Importer v7.3
===========================
- FitGirl and GOG are imported and deduplicated SEPARATELY.
  A game on both sources gets BOTH entries:
    FitGirl: title "X (Fitgirl Repack)", slug "x",      url .../fg-x
    GOG:     title "X (GOG Repack)",     slug "gog-x",  url .../gog-{id}
- FitGirl only imported when download links exist.
- Dedup by exact slug: site all-slugs + namespaced local state + in-cycle set.
- Cycle aborts if the site slug list can't be loaded.
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
VERSION = "7.3"

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
STATE_FILE = os.environ.get("IMPORTED_STATE_FILE", "imported_slugs.json")
NO_LINKS_RECHECK = int(os.environ.get("NO_LINKS_RECHECK_SECONDS", "21600"))
MAX_FAILURES = int(os.environ.get("MAX_FAILURES_PER_CYCLE", "3"))

log.info("%s v%s", APP_NAME, VERSION)
log.info("site=%s fitgirl=%s gog=%s", SITE_URL, FITGIRL_API, GOG_SCRAPE)
log.info("fitgirl_limit=%d gog_limit=%d interval=%ds", FITGIRL_LIMIT, GOG_LIMIT, INTERVAL)
if not ADMIN_SECRET:
    log.warning("no admin secret set")


# ─── HTTP helpers ────────────────────────────────────────────────────────────

def http_get(url: str, timeout: int = 45) -> Any | None:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        log.warning("GET %s -> %d", url.split("?")[0], e.code)
        return None
    except Exception as e:
        log.warning("GET %s -> %s", url.split("?")[0], e)
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


# ─── Utils ───────────────────────────────────────────────────────────────────

def compact(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).strip()


def norm_slug(s: str) -> str:
    return (s or "").strip().lower()


def clean_game_title(title: str) -> str:
    t = (title or "").strip()
    t = re.sub(r"[™®©]", "", t)
    t = re.sub(r"^[><!~]+", "", t)
    t = re.sub(r"[_~]+$", "", t)
    t = t.replace("_", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t.capitalize() if t else "Unknown"


# ─── Local state (namespaced: "fg:<slug>" and "gog:<slug>") ──────────────────

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
        elif s.startswith("gog-"):      # legacy entry -> GOG namespace
            out.add(f"gog:{s}")
        else:                            # legacy entry -> FitGirl namespace
            out.add(f"fg:{s}")
    return out


def save_state(state: set[str]) -> None:
    try:
        with _state_lock:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(state), f)
    except Exception as e:
        log.warning("state save failed: %s", e)


# ─── Download-link check (FitGirl) ──────────────────────────────────────────

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
    _collect_links(detail.get("downloadMirrors"), links)
    _collect_links(detail.get("torrents"), links)
    return len(links) > 0


# ─── FitGirl API ────────────────────────────────────────────────────────────

def fg_latest(page: int = 1) -> list[dict]:
    data = http_get(f"{FITGIRL_API}/latest?page={page}")
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("results") or data.get("data") or data.get("games") or []


def fg_detail(slug: str) -> dict | None:
    d = http_get(f"{FITGIRL_API}/repack/{slug}")
    return d if isinstance(d, dict) else None


# ─── GOG APIs ───────────────────────────────────────────────────────────────

def gog_catalog_info(title: str) -> dict:
    url = f"{GOG_CATALOG_API}?query={urllib.parse.quote(title)}&limit=1"
    data = http_get(url, timeout=30)
    polite_pause(1.0, 2.5)
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
    polite_pause(1.0, 2.5)
    if not isinstance(data, dict):
        return {}
    embedded = data.get("_embedded", {})
    supported = embedded.get("supportedOperatingSystems", {}).get("_embedded", {})
    reqs = supported.get("requirements", {}).get("_embedded", {}).get("systemRequirements", [])
    out: dict[str, dict[str, str]] = {}
    for entry in reqs:
        n, v = entry.get("name", ""), entry.get("value", "")
        if n and v:
            out[n] = {"recommended": v}
    return out


def gog_all_games() -> list[dict]:
    url = f"{GOG_SCRAPE}/api/all-games"
    if GOG_SCRAPE_KEY:
        url += f"?key={urllib.parse.quote(GOG_SCRAPE_KEY)}"
    data = http_get(url, timeout=30)
    if not isinstance(data, dict):
        return []
    return data.get("games") or []


# ─── RAWG (GOG only: description + screenshots) ─────────────────────────────

def _rawg_url(path: str, extra: str = "") -> str:
    url = f"{RAWG_API}{path}"
    params = []
    if extra:
        params.append(extra)
    if RAWG_KEY:
        params.append(f"key={urllib.parse.quote(RAWG_KEY)}")
    return url + ("?" + "&".join(params) if params else "")


def rawg_enrich(title: str) -> dict:
    short = re.sub(r"\s*\((?:GOG|Fitgirl|FitGirl|Repack)\).*$", "", title).strip()
    data = http_get(_rawg_url("/games", f"search={urllib.parse.quote(short)}&page_size=1"), timeout=30)
    polite_pause(1.0, 2.5)
    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        return {"description": "", "screenshots": []}
    rid, rslug = results[0].get("id"), results[0].get("slug", "")
    desc = ""
    if rid:
        d = http_get(_rawg_url(f"/games/{rid}"), timeout=30)
        polite_pause(1.0, 2.5)
        if isinstance(d, dict):
            desc = d.get("description_raw", "")
    shots: list[str] = []
    if rslug:
        s = http_get(_rawg_url(f"/games/{rslug}/screenshots"), timeout=30)
        polite_pause(1.0, 2.5)
        if isinstance(s, dict):
            shots = [r.get("image", "") for r in (s.get("results") or []) if r.get("image")]
    return {"description": compact(desc), "screenshots": shots}


# ─── Dedup: site slugs ───────────────────────────────────────────────────────

def fetch_existing_slugs(base: str) -> set[str] | None:
    """Normalized exact-slug set from the site, or None if fetch failed/empty."""
    data = http_get(f"{base}/api/games/all-slugs", timeout=45)
    if data is None:
        return None
    items = data if isinstance(data, list) else (
        data.get("slugs") or data.get("data") or data.get("games") or data.get("results") or []
    )
    if isinstance(items, dict):
        items = list(items.values()) if all(isinstance(v, str) for v in items.values()) else list(items.keys())
    slugs: set[str] = set()
    for it in items or []:
        if isinstance(it, str):
            slugs.add(norm_slug(it))
        elif isinstance(it, dict) and it.get("slug"):
            slugs.add(norm_slug(it["slug"]))
    return slugs if slugs else None


# ─── Create game ─────────────────────────────────────────────────────────────

def create_game(payload: dict) -> tuple[bool, str]:
    if DRY:
        log.info("[dry-run] %s", json.dumps(payload, ensure_ascii=False)[:400])
        return True, "dry-run"
    url = f"{SITE_URL}/api/games/admin?secret={urllib.parse.quote(ADMIN_SECRET)}"
    code, body = post_json(url, payload)
    if code in (200, 201):
        log.info("  ✓ created: %s", payload.get("title"))
        return True, "created"
    if code == 409:
        log.info("  - already exists on site: %s", payload.get("title"))
        return True, "duplicate"
    log.error("  ✗ create failed %s -> %s (slug=%s): %s",
              code, payload.get("title"), payload.get("slug"), str(body)[:400])
    return False, f"error_{code}"


# ─── Payload builders ───────────────────────────────────────────────────────

def base_payload(title, download_url, slug, repacker, size_original, size_repack,
                 categories, description, cover_url, screenshots, system_requirements) -> dict:
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
    cats: list[str] = []
    cat = detail.get("category")
    if isinstance(cat, str) and cat.strip().lower() not in ("", "none", "other"):
        cats.append(cat.strip())
    elif isinstance(cat, dict):
        n = (cat.get("name") or cat.get("title") or "").strip()
        if n:
            cats.append(n)
    for key in ("genres", "tags"):
        v = detail.get(key)
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            for item in v:
                n = (item.get("name") or item.get("title") or "") if isinstance(item, dict) else item
                if isinstance(n, str) and n.strip() and n.strip() not in cats:
                    cats.append(n.strip())
    return base_payload(
        title=f"{clean} (Fitgirl Repack)",
        download_url=f"{DOWNLOAD_BASE}/fg-{slug}",
        slug=slug, repacker="FitGirl",
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
    slug = f"gog-{game.get('slug') or gog_id}"
    clean = clean_game_title(game.get("title", ""))
    cats = list(dict.fromkeys(catalog.get("genres", []) + catalog.get("tags", [])))
    cats = [c for c in cats if c and c.lower() not in ("other", "none")]
    return base_payload(
        title=f"{clean} (GOG Repack)",
        download_url=f"{DOWNLOAD_BASE}/gog-{gog_id}",
        slug=slug, repacker="GOG",
        size_original=game.get("size_original") or "Unknown",
        size_repack=game.get("size") or "Unknown",
        categories=cats,
        description=rawg.get("description", ""),
        cover_url=catalog.get("cover", "") or "",
        screenshots=rawg.get("screenshots", []),
        system_requirements=sysreq,
    )


# ─── Import flows (independent of each other) ───────────────────────────────

NO_LINKS_SEEN: dict[str, float] = {}
STATS: dict[str, Any] = {"last_cycle": None, "last_result": None}


def import_fitgirl(site: set[str], state: set[str]) -> dict:
    """FitGirl namespace: plain slug. Never looks at GOG entries."""
    imported = skipped_no_links = failures = 0
    items = fg_latest(1)
    log.info("[fitgirl] page 1: %d releases", len(items))
    processed: set[str] = set()

    for item in items:
        if imported >= FITGIRL_LIMIT or failures >= MAX_FAILURES:
            break
        slug = (item.get("slug") or "").strip()
        key = norm_slug(slug)
        if not key or key in processed:
            continue
        processed.add(key)

        if key in site or f"fg:{key}" in state:
            continue  # this FitGirl entry already exists

        last = NO_LINKS_SEEN.get(key)
        if last and time.time() - last < NO_LINKS_RECHECK:
            continue

        polite_pause(1.5, 3.0)
        detail = fg_detail(slug)
        if not detail:
            log.warning("[fitgirl] detail fetch failed: %s", slug)
            continue

        if not has_download_links(detail):
            NO_LINKS_SEEN[key] = time.time()
            skipped_no_links += 1
            log.info("[fitgirl] skip (no download links): %s", slug)
            continue

        real_slug = (detail.get("slug") or slug).strip()
        real_key = norm_slug(real_slug)
        if real_key in site or f"fg:{real_key}" in state:
            continue
        detail["slug"] = real_slug

        log.info("[fitgirl] importing: %s", real_slug)
        ok, status = create_game(build_fitgirl_payload(detail))
        if not ok:
            failures += 1
            continue
        site.add(real_key)
        site.add(key)
        if status != "dry-run":
            state.add(f"fg:{real_key}")
            save_state(state)
        NO_LINKS_SEEN.pop(key, None)
        if status in ("created", "dry-run"):
            imported += 1
        polite_pause_long(CREATE_DELAY, CREATE_DELAY + 3)
    return {"imported": imported, "skipped_no_links": skipped_no_links, "failures": failures}


def import_gog(site: set[str], state: set[str]) -> dict:
    """GOG namespace: 'gog-' slug. Imported even if the same game exists on FitGirl."""
    imported = failures = 0
    all_games = gog_all_games()
    if not all_games:
        log.info("[gog] no games found")
        return {"imported": 0, "failures": 0}

    gog_on_site = {s for s in site if s.startswith("gog-")}
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
        gog_slug = f"gog-{game.get('slug') or game.get('id')}"
        key = norm_slug(gog_slug)
        if key in seen:
            continue
        seen.add(key)
        if key in site or f"gog:{key}" in state:
            continue  # this GOG entry already exists

        title = game.get("title", "")
        log.info("[gog] importing: %s", title)
        polite_pause(1.5, 3.0)
        catalog = gog_catalog_info(title)
        rawg = rawg_enrich(title)
        sysreq = gog_sysreqs(game.get("id", 0))
        ok, status = create_game(build_gog_payload(game, catalog, rawg, sysreq))
        if not ok:
            failures += 1
            continue
        site.add(key)
        if status != "dry-run":
            state.add(f"gog:{key}")
            save_state(state)
        if status in ("created", "dry-run"):
            imported += 1
        polite_pause_long(CREATE_DELAY, CREATE_DELAY + 3)
    return {"imported": imported, "failures": failures}


def run_cycle() -> dict:
    log.info("── cycle start ──")
    site = fetch_existing_slugs(SITE_URL)
    if site is None:
        log.error("could not load site slugs (all-slugs) — ABORTING cycle to avoid duplicates")
        return {"error": "slugs_unavailable"}

    state = load_state()
    gog_n = sum(1 for s in site if s.startswith("gog-"))
    log.info("site slugs: %d (fitgirl=%d gog=%d) | local state: %d",
             len(site), len(site) - gog_n, gog_n, len(state))

    fg = import_fitgirl(site, state)
    gog = import_gog(site, state)
    result = {"fitgirl": fg, "gog": gog}
    STATS["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATS["last_result"] = result
    log.info("── cycle done: fitgirl=%d (no-links skipped=%d) gog=%d ──",
             fg["imported"], fg["skipped_no_links"], gog["imported"])
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
