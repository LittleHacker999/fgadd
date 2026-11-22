#!/usr/bin/env python3
"""
Repack X Auto-Importer v7.9
===========================
- Dedup by TITLE + REPACKER from repackx.apps.mistral.run/api/games/all-slugs.
  Same title + same repacker -> SKIP. Same title, different repacker -> ADD.
- GOG v2 API (api.gog.com/v2/games/{id}): boxArtImage = cover, description,
  screenshots ({formatter}->1600), system requirements, exact title.
- System requirements format: {"minimum": "OS: ... CPU: ... RAM: ... GPU: ...",
                               "recommended": "..."}
- GOG Catalog API (title search, top-5 scoring): genres + tags only.
  No match -> v2 tags used as categories; cover still from boxArt.
- FitGirl: no system requirements (added manually). Only releases with links.
- Cycle aborts if all-slugs can't be loaded.
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
VERSION = "7.9"

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

SYSREQ_LABELS = {
    "system": "OS", "processor": "CPU", "memory": "RAM", "graphics": "GPU",
    "directx": "DirectX", "storage": "Storage", "sound": "Sound",
    "network": "Network", "other": "Other",
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


def fix_formatter_url(url: str) -> str:
    """Replace GOG {formatter} template with real size."""
    return url.replace("{formatter}", "1600") if "{formatter}" in url else url


def _extract_url(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for k in ("url", "image", "src", "href", "link", "full", "large"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # nested _links.self.href (GOG v2 screenshot format)
        href = safe_get(safe_get(safe_get(item, "_links", {}), "self", {}), "href")
        if isinstance(href, str) and href.strip():
            return href.strip()
    return ""


def filter_image_urls(urls: Any) -> list[str]:
    if urls is None:
        return []
    items = urls if isinstance(urls, (list, tuple)) else [urls]
    result = []
    for item in items:
        clean = safe_url(fix_formatter_url(_extract_url(item)))
        if not clean:
            continue
        path = clean.lower().split("?")[0].split("#")[0]
        if any(path.endswith(ext) for ext in IMAGE_EXTS):
            if clean not in result:
                result.append(clean)
    return result


# ─── Dedup: title + repacker pairs ──────────────────────────────────────────

def _title_to_key(title: Any) -> str:
    t = safe_str(title, "").strip().lower()
    t = re.sub(r"\s*\((?:fitgirl|gog|repack|fitgirl repack|gog repack)\)\s*$", "", t, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", t)


def _slug_to_pair(slug: str) -> tuple[str, str] | None:
    """Parse a site slug into (title_key, repacker).
    'void-reaver-fitgirl-repack' -> ('voidreaver', 'fitgirl')
    'gog-observer' / 'observer-gog-repack' -> ('observer', 'gog')
    plain 'cyberpunk-2077' -> ('cyberpunk2077', 'fitgirl')  (plain = fitgirl convention)
    """
    s = norm_slug(slug)
    if not s:
        return None
    rep = "fitgirl"
    core = s
    if s.startswith("gog-") or s.startswith("gog:"):
        rep = "gog"
        core = re.sub(r"^gog[-:]", "", s)
    elif re.search(r"[-_ ]gog[-_ ]repack$", s) or re.search(r"[-_ ]gog$", s):
        rep = "gog"
        core = re.sub(r"[-_ ]gog[-_ ]repack$", "", s)
        core = re.sub(r"[-_ ]gog$", "", core)
    else:
        core = re.sub(r"[-_ ]fitgirl[-_ ]repack$", "", s)
        core = re.sub(r"[-_ ]fitgirl$", "", core)
    core = re.sub(r"[-_ ]repack$", "", core)
    tk = re.sub(r"[^a-z0-9]", "", core)
    return (tk, rep) if tk else None


def _item_to_pairs(item: Any) -> list[tuple[str, str]]:
    """Parse one all-slugs item (string or object) into (title_key, repacker) pairs."""
    out: list[tuple[str, str]] = []
    if isinstance(item, str):
        p = _slug_to_pair(item)
        if p:
            out.append(p)
    elif isinstance(item, dict):
        title = safe_str(safe_get(item, "title", "")).strip()
        rep_raw = safe_str(safe_get(item, "repacker", "")).strip().lower()
        if title:
            tk = _title_to_key(title)
            if tk:
                if "gog" in rep_raw or "gog" in title.lower():
                    out.append((tk, "gog"))
                elif "fitgirl" in rep_raw or "fitgirl" in title.lower():
                    out.append((tk, "fitgirl"))
                else:
                    out.append((tk, "fitgirl"))  # unknown -> treat as fitgirl
        slug = safe_str(safe_get(item, "slug", "")).strip()
        if slug:
            p = _slug_to_pair(slug)
            if p and p not in out:
                out.append(p)
    return out


def fetch_existing_pairs(base: str) -> tuple[set[tuple[str, str]], set[str]] | None:
    """Fetch all-slugs -> (set of (title_key, repacker), set of exact slugs).
    Returns None if fetch failed or empty -> cycle must abort."""
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

    pairs: set[tuple[str, str]] = set()
    slugs: set[str] = set()
    for it in items or []:
        for p in _item_to_pairs(it):
            pairs.add(p)
        if isinstance(it, str):
            slugs.add(norm_slug(it))
        elif isinstance(it, dict):
            s = norm_slug(safe_get(it, "slug", ""))
            if s:
                slugs.add(s)

    if not pairs and not slugs:
        return None
    return pairs, slugs


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


def state_to_pairs(state: set[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for s in state:
        if s.startswith("fg:"):
            p = _slug_to_pair(s[3:])
            if p:
                pairs.add((p[0], "fitgirl"))
        elif s.startswith("gog:"):
            p = _slug_to_pair(s[4:])
            if p:
                pairs.add((p[0], "gog"))
    return pairs


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


# ─── GOG Catalog API (genres/tags via title search) ─────────────────────────

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
    """Top-5 title match for genres/tags. No match -> empty (no catalog data used)."""
    empty = {"genres": [], "tags": [], "screenshots": []}
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
        log.info("[gog] catalog: no good match for '%s' (best=%.2f)", query, best_score)
        return empty

    found_title = safe_str(safe_get(best, "title") or safe_get(best, "name")).strip()
    log.info("[gog] catalog match: '%s' -> '%s' (score=%.2f)", query, found_title, best_score)
    return {
        "genres": _extract_gog_names(safe_get(best, "genres")),
        "tags": _extract_gog_names(safe_get(best, "tags")),
        "screenshots": filter_image_urls(safe_get(best, "screenshots")),
    }


# ─── GOG v2 API (by exact ID: cover, description, screenshots, sysreqs) ─────

def gog_v2_info(gog_id: Any) -> dict:
    """Fetch api.gog.com/v2/games/{id} and extract everything we need."""
    empty = {"cover": "", "description": "", "screenshots": [], "title": "",
             "tags": [], "size_mb": None, "sysreq": {}}
    gid = safe_str(gog_id).strip()
    if not gid or gid == "0":
        return empty
    data = http_get(f"{GOG_V2_API}/{gid}", timeout=30)
    polite_pause(1.0, 2.5)
    if not isinstance(data, dict):
        return empty

    links = safe_get(data, "_links", {})
    # Cover = boxArtImage (user requirement)
    cover = safe_url(safe_get(safe_get(links, "boxArtImage", {}), "href", ""))
    if not cover:
        cover = safe_url(safe_get(safe_get(links, "backgroundImage", {}), "href", ""))

    description = safe_get(data, "description") or safe_get(data, "overview") or ""

    emb = safe_get(data, "_embedded", {})
    shots: list[str] = []
    ss = safe_get(emb, "screenshots")
    if isinstance(ss, list):
        for s in ss:
            href = safe_str(safe_get(safe_get(safe_get(s, "_links", {}), "self", {}), "href", "")).strip()
            if href:
                fixed = fix_formatter_url(href)
                if safe_url(fixed):
                    shots.append(fixed)

    title = safe_str(safe_get(safe_get(emb, "product", {}), "title", "")).strip()

    # v2 tags/properties (fallback categories if catalog has no match)
    tag_names: list[str] = []
    for t in (safe_get(emb, "tags") or []):
        n = safe_str(safe_get(t, "name", "")).strip()
        if n and n not in tag_names:
            tag_names.append(n)
    for p in (safe_get(emb, "properties") or []):
        n = safe_str(safe_get(p, "name", "")).strip()
        if n and n not in tag_names:
            tag_names.append(n)

    size_mb = data.get("size") if isinstance(data.get("size"), (int, float)) else None

    # ── System requirements (user's format) ──
    # _embedded.supportedOperatingSystems[] ->
    #   systemRequirements[] -> {type: "minimum"|"recommended",
    #     requirements: [{id: "processor", name: "Processor:", description: "1.8 GHz"}]}
    sysreq: dict[str, str] = {}
    sos = safe_get(emb, "supportedOperatingSystems")
    if isinstance(sos, list):
        for os_entry in sos:
            if not isinstance(os_entry, dict):
                continue
            reqs = safe_get(os_entry, "systemRequirements")
            if not isinstance(reqs, list):
                continue
            for r in reqs:
                if not isinstance(r, dict):
                    continue
                rtype = norm_slug(safe_get(r, "type", ""))
                key = "recommended" if "rec" in rtype else "minimum"
                parts: list[str] = []
                items = safe_get(r, "requirements")
                if isinstance(items, list):
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        desc = safe_str(safe_get(it, "description", "")).strip()
                        if not desc:
                            continue
                        rid = norm_slug(safe_get(it, "id", ""))
                        label = SYSREQ_LABELS.get(rid)
                        if not label:
                            label = safe_str(safe_get(it, "name", "")).strip().rstrip(":") or rid.title()
                        parts.append(f"{label}: {desc}")
                if not parts:
                    full = clean_description(safe_get(r, "description", ""))
                    if full:
                        parts = [full]
                if parts and key not in sysreq:
                    sysreq[key] = " ".join(parts)

    return {"cover": cover, "description": description, "screenshots": shots,
            "title": title, "tags": tag_names, "size_mb": size_mb, "sysreq": sysreq}


def gog_all_games() -> list[dict]:
    url = f"{GOG_SCRAPE}/api/all-games"
    if GOG_SCRAPE_KEY:
        url += f"?key={urllib.parse.quote(GOG_SCRAPE_KEY)}"
    data = http_get(url, timeout=30)
    if not isinstance(data, dict):
        return []
    games = safe_get(data, "games") or []
    return [g for g in games if isinstance(g, dict)]


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
    if code == 409 or (code == 400 and isinstance(body, str)
                       and ("exist" in body.lower() or "duplicate" in body.lower())):
        log.info("  - already exists on site: %s", payload.get("title"))
        return True, "duplicate"
    log.error("  ✗ create failed %s -> %s | slug=%s | repacker=%s | body: %s",
              code, payload.get("title"), payload.get("slug"),
              payload.get("repacker"), str(body)[:400])
    return False, f"error_{code}"


# ─── Payload builders ───────────────────────────────────────────────────────

def base_payload(*, title, download_url, slug, repacker, size_original, size_repack,
                 categories, description, cover_url, screenshots, system_requirements) -> dict:
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
        "system_requirements": system_requirements if isinstance(system_requirements, dict) else {},
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
        system_requirements={},  # added manually by site owner
    )


def build_gog_payload(game: dict, v2: dict, catalog: dict) -> dict:
    gog_id = safe_get(game, "id")
    gog_slug = safe_str(safe_get(game, "slug", "")).strip() or safe_str(gog_id)

    # Title: prefer exact v2 product title, fallback to scrape title
    raw_title = v2.get("title") or safe_str(safe_get(game, "title", ""))
    clean = clean_game_title(raw_title)

    # Categories: catalog genres+tags (title-matched); fallback to v2 tags
    cats = list(dict.fromkeys(
        safe_get(catalog, "genres", []) + safe_get(catalog, "tags", [])
    ))
    if not cats:
        cats = v2.get("tags", [])
    cats = [c for c in cats if c and c.lower() not in ("other", "none")]

    # Cover: v2 boxArt only (exact ID match, never wrong). No boxArt -> no cover.
    cover = v2.get("cover", "")

    # Screenshots: v2 first, catalog as fallback
    shots = v2.get("screenshots") or safe_get(catalog, "screenshots", [])

    # Description: scrape API first, then v2
    description = safe_get(game, "description", "") or v2.get("description", "")

    # Size: scrape API fields, fallback to v2 size (MB)
    size_repack = safe_get(game, "size") or "Unknown"
    if (not size_repack or size_repack == "Unknown") and v2.get("size_mb"):
        size_repack = f"{int(v2['size_mb'])} MB"

    return base_payload(
        title=f"{clean} (GOG Repack)",
        download_url=f"{DOWNLOAD_BASE}/gog-{gog_id}",
        slug=f"gog-{gog_slug}",
        repacker="GOG",
        size_original=safe_get(game, "size_original") or size_repack,
        size_repack=size_repack,
        categories=cats,
        description=description,
        cover_url=cover,
        screenshots=shots,
        system_requirements=v2.get("sysreq", {}),
    )


# ─── Import flows ───────────────────────────────────────────────────────────

NO_LINKS_SEEN: dict[str, float] = {}
STATS: dict[str, Any] = {"last_cycle": None, "last_result": None}


def import_fitgirl(pairs: set[tuple[str, str]], slugs: set[str], state: set[str]) -> dict:
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

            # Check 1 (cheap, from list): exact slug / state / title+repacker pair
            list_title = safe_str(safe_get(item, "title", "")).strip()
            list_tk = _title_to_key(list_title) if list_title else ""
            if key in slugs or f"fg:{key}" in state or (list_tk and (list_tk, "fitgirl") in pairs):
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

            # Check 2 (from detail): real title + real slug
            detail_title = safe_str(safe_get(detail, "title", slug)).strip()
            tk = _title_to_key(detail_title)
            real_slug = safe_str(safe_get(detail, "slug", "")).strip() or slug
            real_key = norm_slug(real_slug)
            if (real_key in slugs or f"fg:{real_key}" in state
                    or (tk and (tk, "fitgirl") in pairs)):
                skipped_dup += 1
                log.info("[fitgirl] skip (already on site as Fitgirl): %s", detail_title or slug)
                continue

            if not has_download_links(detail):
                NO_LINKS_SEEN[key] = time.time()
                skipped_no_links += 1
                log.info("[fitgirl] skip (no download links): %s", slug)
                continue

            if tk and any(k == tk for k, _ in pairs):
                log.info("[fitgirl] note: '%s' exists from another repacker — adding Fitgirl entry", detail_title)

            detail["slug"] = real_slug
            log.info("[fitgirl] importing: %s", real_slug)
            ok, status = create_game(build_fitgirl_payload(detail))
            if not ok:
                failures += 1
                continue

            slugs.add(real_key)
            slugs.add(key)
            slugs.add(f"{real_key}-fitgirl-repack")
            if tk:
                pairs.add((tk, "fitgirl"))
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


def import_gog(pairs: set[tuple[str, str]], slugs: set[str], state: set[str]) -> dict:
    imported = skipped_dup = failures = 0
    all_games = gog_all_games()
    if not all_games:
        log.info("[gog] no games found")
        return {"imported": 0, "skipped_dup": 0, "failures": 0}

    gog_done = sum(1 for g in all_games
                   if (_title_to_key(safe_get(g, "title", "")), "gog") in pairs)
    backlog = len(all_games) - gog_done
    if backlog <= 0:
        log.info("[gog] all %d games already imported — watching for new ones", len(all_games))
    else:
        log.info("[gog] backlog: %d/%d imported, %d remaining", gog_done, len(all_games), backlog)

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

            # Dedup by title+repacker (GOG) / exact slug / state
            if key in slugs or f"gog:{key}" in state or (tk and (tk, "gog") in pairs):
                skipped_dup += 1
                continue

            if tk and any(k == tk for k, _ in pairs):
                log.info("[gog] note: '%s' exists from another repacker — adding GOG entry", title)

            log.info("[gog] importing: %s", title)
            polite_pause(1.5, 3.0)
            v2 = gog_v2_info(safe_get(game, "id", 0))
            catalog = gog_catalog_info(v2.get("title") or title)
            ok, status = create_game(build_gog_payload(game, v2, catalog))
            if not ok:
                failures += 1
                continue

            slugs.add(key)
            slugs.add(f"{base_slug}-gog-repack")
            if tk:
                pairs.add((tk, "gog"))
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
    site = fetch_existing_pairs(SITE_URL)
    if site is None:
        log.error("could not load %s/api/games/all-slugs — ABORTING cycle to avoid duplicates", SITE_URL)
        return {"error": "slugs_unavailable"}

    pairs, slugs = site
    state = load_state()
    pairs |= state_to_pairs(state)  # secondary guard from local state

    fg_n = sum(1 for _, r in pairs if r == "fitgirl")
    gog_n = sum(1 for _, r in pairs if r == "gog")
    log.info("all-slugs loaded: %d slugs | known titles: fitgirl=%d gog=%d",
             len(slugs), fg_n, gog_n)
    log.info("slugs sample: %s", sorted(slugs)[:5])

    fg = import_fitgirl(pairs, slugs, state)
    gog = import_gog(pairs, slugs, state)
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
