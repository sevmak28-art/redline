#!/usr/bin/env python3
"""REDLINE Listings API — standard-library server for authorized provider feeds.

No marketplace HTML scraping is performed. Providers must supply a permitted
JSON feed and explicitly authorize the reuse of photos/contact fields.
"""
from __future__ import annotations

import datetime as dt
import difflib
import html
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("REDLINE_DATA_DIR", ROOT / "server_data")).resolve()
DB_PATH = DATA_DIR / "listings.sqlite3"
CONFIG_PATH = Path(os.environ.get("REDLINE_PROVIDERS_CONFIG", ROOT / "providers.json")).resolve()
ADMIN_TOKEN = os.environ.get("REDLINE_ADMIN_TOKEN", "")
POLL_SECONDS = max(60, int(os.environ.get("REDLINE_POLL_SECONDS", "600")))
DEFAULT_MAX_AGE_MINUTES = 15
USER_AGENT = "REDLINE-Listings-Connector/1.0 (authorized marketplace feed client)"
PHONE_RE = re.compile(r"^[+0-9()\s.-]{6,24}$")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(dt_value: dt.datetime | None = None) -> str:
    return (dt_value or utcnow()).astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_dt(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def host_allowed(url: Any, allowed_hosts: list[str]) -> str | None:
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        p = urllib.parse.urlparse(url.strip())
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            return None
        host = p.hostname.lower().rstrip(".")
        if not any(host == x.lower().lstrip(".") or host.endswith("." + x.lower().lstrip(".")) for x in allowed_hosts if isinstance(x, str)):
            return None
        return urllib.parse.urlunparse(("https", p.netloc, p.path, "", p.query, ""))
    except ValueError:
        return None



SEARCH_ALIASES = {
    "бмв": "bmw", "бэха": "bmw", "бимер": "bmw",
    "мерс": "mercedes", "мерседес": "mercedes", "гелик": "g class mercedes",
    "тойота": "toyota", "хонда": "honda", "ниссан": "nissan",
    "порше": "porsche", "додж": "dodge", "хеллкет": "hellcat",
    "фольксваген": "volkswagen", "фольц": "volkswagen",
    "лексус": "lexus", "субару": "subaru", "киа": "kia",
    "хендай": "hyundai", "хундай": "hyundai", "хавал": "haval", "чери": "chery",
    "камри": "camry", "прадо": "prado", "джолион": "jolion", "патрол": "patrol",
    "солярис": "solaris", "сантафе": "santa fe", "джип": "jeep", "крузак": "land cruiser",
    "джили": "geely", "мазда": "mazda", "мицубиси": "mitsubishi",
}
_RU_LAT = str.maketrans({
    "а":"a", "б":"b", "в":"v", "г":"g", "д":"d", "е":"e", "ё":"e",
    "ж":"zh", "з":"z", "и":"i", "й":"i", "к":"k", "л":"l", "м":"m",
    "н":"n", "о":"o", "п":"p", "р":"r", "с":"s", "т":"t", "у":"u",
    "ф":"f", "х":"x", "ц":"ts", "ч":"ch", "ш":"sh", "щ":"shch",
    "ъ":"", "ы":"y", "ь":"", "э":"e", "ю":"yu", "я":"ya"
})

def normalize_search(value: str) -> str:
    value = (value or "").casefold().replace("ё", "е")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", value)

def query_matches(query: str, fields: list[str]) -> bool:
    q = normalize_search(query)
    if not q:
        return True
    # Replace common Russian names and nicknames with official brand/model spellings.
    expanded = q
    expanded_tokens = [SEARCH_ALIASES.get(token, token) for token in expanded.split()]
    expanded = " ".join(expanded_tokens)
    hay = normalize_search(" ".join(fields))
    hay_lat = normalize_search(hay.translate(_RU_LAT))
    query_forms = [q, expanded, normalize_search(q.translate(_RU_LAT)), normalize_search(expanded.translate(_RU_LAT))]
    words = hay.split()
    words_lat = hay_lat.split()
    for form in query_forms:
        if form and (form in hay or form in hay_lat):
            return True
        tokens = form.split()
        if tokens and all(any(token == word or token in word or (len(token) >= 3 and difflib.SequenceMatcher(None, token, word).ratio() >= 0.78) for word in words + words_lat) for token in tokens):
            return True
    return False


def sanitize_item(provider: dict[str, Any], item: dict[str, Any], checked_at: dt.datetime, now: dt.datetime) -> dict[str, Any] | None:
    """Return a safe public listing only when provider has verified it recently."""
    max_age = min(60, max(1, int(provider.get("max_age_minutes", DEFAULT_MAX_AGE_MINUTES))))
    age = (now - checked_at).total_seconds()
    if age < -60 or age > max_age * 60:
        return None
    if str(item.get("status", "")).strip().lower() != "active":
        return None
    listing_id = str(item.get("id", "")).strip()[:160]
    title = str(item.get("title", "")).strip()[:240]
    source_url = host_allowed(item.get("sourceUrl"), provider.get("source_domains", []))
    if not listing_id or not title or not source_url:
        return None

    expires_at = parse_dt(item.get("expiresAt")) if item.get("expiresAt") else None
    if item.get("expiresAt") and (expires_at is None or expires_at <= now):
        return None

    photo = None
    if provider.get("photo_reuse_allowed") is True and item.get("photoReuseAllowed") is True:
        photo = host_allowed(item.get("photo"), provider.get("photo_domains", provider.get("source_domains", [])))

    contact_url = None
    if provider.get("contact_link_allowed", True) is True:
        contact_url = host_allowed(item.get("contactUrl"), provider.get("source_domains", []))

    phone = None
    if (provider.get("phone_reuse_allowed") is True and item.get("phoneReuseAllowed") is True
            and item.get("phonePublic") is True):
        candidate = str(item.get("phone", "")).strip()
        if PHONE_RE.fullmatch(candidate):
            phone = candidate

    price = str(item.get("price", "")).strip()[:80] or "Цена не указана"
    currency = str(item.get("currency", "KGS")).strip()[:8]
    return {
        "id": listing_id,
        "title": title,
        "price": price,
        "priceValue": item.get("priceValue") if isinstance(item.get("priceValue"), (int, float)) and item.get("priceValue") >= 0 else None,
        "currency": currency,
        "location": str(item.get("location", "")).strip()[:100],
        "year": item.get("year") if isinstance(item.get("year"), (int, str)) else "",
        "brand": str(item.get("brand", "")).strip()[:80],
        "model": str(item.get("model", "")).strip()[:120],
        "mileage": str(item.get("mileage", "")).strip()[:80],
        "condition": str(item.get("condition", "")).strip()[:100],
        "photo": photo,
        "photoReuseAllowed": bool(photo),
        "source": str(provider.get("name", provider.get("id", "Источник")))[:80],
        "sourceUrl": source_url,
        "contactUrl": contact_url,
        "phone": phone,
        "phonePublic": bool(phone),
        "phoneReuseAllowed": bool(phone),
        "status": "active",
        "publishedAt": str(item.get("publishedAt", ""))[:40],
        "expiresAt": iso(expires_at) if expires_at else None,
        "verifiedAt": iso(checked_at),
    }


class Store:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS providers (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
              last_attempt TEXT, last_success TEXT, last_feed_checked TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS listings (
              id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, payload TEXT NOT NULL,
              provider_checked_at TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL,
              stored_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_listings_provider ON listings(provider_id);
            CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
            """)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def provider_state(self, provider: dict[str, Any], *, attempt: str | None = None,
                       success: str | None = None, checked: str | None = None, error: str | None = None):
        with self.lock, self.connect() as db:
            db.execute("""INSERT INTO providers(id,name,enabled,last_attempt,last_success,last_feed_checked,last_error)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled,
              last_attempt=COALESCE(excluded.last_attempt,providers.last_attempt),
              last_success=COALESCE(excluded.last_success,providers.last_success),
              last_feed_checked=COALESCE(excluded.last_feed_checked,providers.last_feed_checked),
              last_error=excluded.last_error""",
              (provider["id"], provider.get("name", provider["id"]), int(provider.get("enabled") is True),
               attempt, success, checked, error))

    def replace_provider_items(self, provider: dict[str, Any], entries: list[dict[str, Any]], checked_at: dt.datetime, complete: bool):
        now_s = iso()
        present: list[str] = []
        with self.lock, self.connect() as db:
            for entry in entries:
                key = f"{provider['id']}:{entry['id']}"
                present.append(key)
                exp = entry.get("expiresAt")
                db.execute("""INSERT INTO listings(id,provider_id,payload,provider_checked_at,expires_at,status,stored_at)
                  VALUES(?,?,?,?,?,'active',?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                  provider_checked_at=excluded.provider_checked_at, expires_at=excluded.expires_at,
                  status='active', stored_at=excluded.stored_at""",
                  (key, provider["id"], json.dumps(entry, ensure_ascii=False), iso(checked_at), exp, now_s))
            if complete:
                if present:
                    marks = ",".join("?" for _ in present)
                    db.execute(f"UPDATE listings SET status='missing' WHERE provider_id=? AND id NOT IN ({marks})", [provider["id"], *present])
                else:
                    db.execute("UPDATE listings SET status='missing' WHERE provider_id=?", (provider["id"],))

    def cleanup_stale(self):
        now = utcnow()
        cutoff = iso(now - dt.timedelta(minutes=DEFAULT_MAX_AGE_MINUTES))
        now_s = iso(now)
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM listings WHERE status!='active' OR provider_checked_at<? OR (expires_at IS NOT NULL AND expires_at<=?)", (cutoff, now_s))

    def listings(self, q: str = "", region: str = "", limit: int = 100, sort_by: str = "newest") -> list[dict[str, Any]]:
        self.cleanup_stale()
        now = utcnow()
        out = []
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT payload,provider_checked_at,expires_at,status FROM listings WHERE status='active' ORDER BY stored_at DESC LIMIT 1000").fetchall()
        for row in rows:
            checked = parse_dt(row["provider_checked_at"])
            expiry = parse_dt(row["expires_at"]) if row["expires_at"] else None
            if not checked or (now - checked).total_seconds() > DEFAULT_MAX_AGE_MINUTES * 60 or (now - checked).total_seconds() < -60:
                continue
            if expiry and expiry <= now:
                continue
            item = json.loads(row["payload"])
            fields = [str(item.get(k, "")) for k in ("title", "brand", "model", "source")]
            if not query_matches(q, fields):
                continue
            if region and region.casefold() not in ("кыргызстан", "весь кыргызстан") and region.casefold() not in item.get("location", "").casefold():
                continue
            out.append(item)
        if sort_by in ("price_asc", "price_desc"):
            direction = 1 if sort_by == "price_asc" else -1
            out.sort(key=lambda x: (str(x.get("currency", "")), x.get("priceValue") is None, (x.get("priceValue") or 0) * direction))
        else:
            out.sort(key=lambda x: x.get("verifiedAt", ""), reverse=True)
        return out[:limit]

    def counts(self) -> dict[str, int]:
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT id,provider_checked_at,expires_at FROM listings WHERE status='active'").fetchall()
        now = utcnow()
        active = 0
        for row in rows:
            checked = parse_dt(row["provider_checked_at"])
            expiry = parse_dt(row["expires_at"]) if row["expires_at"] else None
            if checked and -60 <= (now - checked).total_seconds() <= DEFAULT_MAX_AGE_MINUTES * 60 and (not expiry or expiry > now):
                active += 1
        return {"activeListings": active}

    def source_states(self) -> list[dict[str, Any]]:
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT * FROM providers ORDER BY name").fetchall()
        return [dict(row) for row in rows]


STORE = Store()
SYNC_LOCK = threading.Lock()


def load_config() -> list[dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return []
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        providers = data.get("providers", []) if isinstance(data, dict) else data
        return [p for p in providers if isinstance(p, dict) and p.get("id")]
    except (OSError, json.JSONDecodeError):
        return []


def fetch_provider(provider: dict[str, Any]) -> tuple[dict[str, Any], str]:
    feed_url = str(provider.get("feed_url", "")).strip()
    parsed = urllib.parse.urlparse(feed_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Не настроен HTTPS URL разрешённой JSON-выгрузки")
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    token_env = provider.get("token_env")
    if token_env:
        token = os.environ.get(str(token_env), "")
        if not token:
            raise ValueError(f"Не задан секрет в переменной окружения {token_env}")
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(feed_url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != parsed.hostname:
            raise ValueError("Feed перенаправил запрос на другой домен — отклонено")
        raw = response.read(5_000_001)
    if len(raw) > 5_000_000:
        raise ValueError("JSON-выгрузка превышает 5 МБ")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Неверный формат: ожидается JSON-объект с массивом items")
    return payload, iso()


def sync_provider(provider: dict[str, Any]) -> dict[str, Any]:
    attempted = iso()
    STORE.provider_state(provider, attempt=attempted, error=None)
    try:
        payload, fetched_at = fetch_provider(provider)
        checked_at = parse_dt(payload.get("checkedAt"))
        if not checked_at:
            raise ValueError("В выгрузке отсутствует корректное checkedAt — нельзя подтвердить свежесть")
        max_age = min(60, max(1, int(provider.get("max_age_minutes", DEFAULT_MAX_AGE_MINUTES))))
        age = (utcnow() - checked_at).total_seconds()
        if age < -60 or age > max_age * 60:
            raise ValueError(f"Данные источника устарели: checkedAt старше {max_age} минут")
        cleaned = []
        for item in payload["items"]:
            if isinstance(item, dict):
                result = sanitize_item(provider, item, checked_at, utcnow())
                if result:
                    cleaned.append(result)
        STORE.replace_provider_items(provider, cleaned, checked_at, payload.get("complete") is True)
        STORE.provider_state(provider, success=fetched_at, checked=iso(checked_at), error=None)
        return {"id": provider["id"], "ok": True, "accepted": len(cleaned), "checkedAt": iso(checked_at)}
    except Exception as exc:
        STORE.provider_state(provider, error=str(exc)[:500])
        return {"id": provider["id"], "ok": False, "error": str(exc)[:500]}


def sync_all() -> list[dict[str, Any]]:
    if not SYNC_LOCK.acquire(blocking=False):
        return [{"ok": False, "error": "Синхронизация уже выполняется"}]
    try:
        providers = load_config()
        results = []
        for provider in providers:
            if provider.get("enabled") is True:
                results.append(sync_provider(provider))
            else:
                STORE.provider_state(provider, error="Источник отключён: требуется разрешённая API/JSON-выгрузка")
        STORE.cleanup_stale()
        return results
    finally:
        SYNC_LOCK.release()


def scheduler():
    while True:
        try:
            sync_all()
        except Exception:
            pass
        time.sleep(POLL_SECONDS)


class Handler(SimpleHTTPRequestHandler):
    server_version = "REDLINE-Server/1.1"

    def end_headers(self):
        # Defense-in-depth headers. TLS itself is terminated by the hosting proxy.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' https: data: blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self' https:"
        )
        # Render (and similar reverse proxies) terminates TLS and forwards HTTP internally.
        if self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https" or os.environ.get("REDLINE_FORCE_HTTPS") == "1":
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, max-age=0")
        elif path.startswith("/assets/"):
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def send_json(self, status: int, payload: dict[str, Any]):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def public_base_url(self) -> str:
        configured = os.environ.get("REDLINE_PUBLIC_URL", "").strip().rstrip("/")
        if configured:
            parsed = urllib.parse.urlparse(configured)
            if (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                    and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
                return f"https://{parsed.netloc}"
        # For Render's default onrender.com address; forwarded host is used only
        # for presentation metadata, never for redirects or access control.
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "localhost")
        host = host.split(",", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9.:-]{1,255}", host):
            host = "localhost"
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip().lower()
        scheme = "https" if proto == "https" else "http"
        return f"{scheme}://{host}"

    def send_text(self, status: int, content_type: str, body: str):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_sitemap(self):
        base = self.public_base_url()
        public_pages = []
        for path in sorted(ROOT.glob("*.html")):
            if path.name in {"favorites.html", "compare.html", "brand-mercedes.html"}:
                continue
            public_pages.append(path.name)
        public_pages.insert(0, "index.html") if "index.html" not in public_pages else None
        urls = "\n".join(
            f"  <url><loc>{html.escape(base + ('/' if name == 'index.html' else '/' + name), quote=True)}</loc></url>" for name in public_pages
        )
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f'{urls}\n</urlset>\n')
        self.send_text(200, "application/xml; charset=utf-8", body)

    def send_robots(self):
        base = self.public_base_url()
        body = "User-agent: *\nAllow: /\nDisallow: /docs/\nDisallow: /api/\n"
        body += f"Sitemap: {base}/sitemap.xml\n"
        self.send_text(200, "text/plain; charset=utf-8", body)

    def send_html_with_canonical(self, file_path: Path, request_path: str):
        try:
            body = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self.send_error(404, "File not found")
        base = self.public_base_url()
        canonical_path = "/" if request_path in ("", "/", "/index.html") else request_path
        canonical = f'<link rel="canonical" href="{html.escape(base + canonical_path, quote=True)}">'
        if "rel=\"canonical\"" not in body and "rel='canonical'" not in body:
            body = body.replace("</head>", canonical + "\n</head>", 1)
        body = body.replace('content="assets/cover.png"', f'content="{html.escape(base + "/assets/cover.png", quote=True)}"')
        self.send_text(200, "text/html; charset=utf-8", body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/sitemap.xml":
            return self.send_sitemap()
        if parsed.path == "/robots.txt":
            return self.send_robots()
        if parsed.path == "/" or parsed.path.endswith(".html"):
            # Use SimpleHTTPRequestHandler's safe path translation to prevent traversal.
            html_request_path = "/index.html" if parsed.path == "/" else parsed.path
            file_path = Path(self.translate_path(html_request_path)).resolve()
            if file_path.parent == ROOT.resolve() and file_path.is_file() and file_path.suffix.lower() == ".html":
                canonical_path = "/" if parsed.path == "/" else parsed.path
                return self.send_html_with_canonical(file_path, canonical_path)
        if parsed.path == "/api/health":
            cfg = load_config()
            enabled = [p for p in cfg if p.get("enabled") is True]
            stored = {row["id"]: row for row in STORE.source_states()}
            now = utcnow()
            states = []
            for provider in cfg:
                state = stored.get(provider["id"], {})
                last_success = parse_dt(state.get("last_success"))
                last_feed_checked = parse_dt(state.get("last_feed_checked"))
                healthy = bool(provider.get("enabled") is True and last_success and last_feed_checked
                               and -60 <= (now - last_success).total_seconds() <= max(900, POLL_SECONDS * 2)
                               and -60 <= (now - last_feed_checked).total_seconds() <= DEFAULT_MAX_AGE_MINUTES * 60)
                states.append({"id": provider["id"], "name": provider.get("name", provider["id"]),
                               "enabled": provider.get("enabled") is True, "healthy": healthy,
                               "last_success": state.get("last_success")})
            healthy_count = sum(1 for state in states if state["healthy"])
            return self.send_json(200, {"ok": True, "service": "REDLINE Listings API", "checkedAt": iso(),
                "configuredProviders": len(cfg), "enabledProviders": len(enabled), "healthyProviders": healthy_count, **STORE.counts(),
                "integrationReady": bool(healthy_count), "refreshEverySeconds": POLL_SECONDS,
                "message": "Поставщики не подключены" if not enabled else ("Нет свежих подтверждений от источников" if not healthy_count else "Источники обновляются"), "sources": states})
        if parsed.path == "/api/listings":
            q = query.get("q", [""])[0][:120]
            region = query.get("region", [""])[0][:80]
            try:
                limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
            except ValueError:
                limit = 50
            sort_by = query.get("sort", ["newest"])[0]
            if sort_by not in ("newest", "price_asc", "price_desc"): sort_by = "newest"
            items = STORE.listings(q=q, region=region, limit=limit, sort_by=sort_by)
            return self.send_json(200, {"checkedAt": iso(), "items": items, "count": len(items),
                "sources": STORE.source_states(), "note": "Показываются только активные объявления с недавней проверкой источника."})
        if parsed.path == "/api/sources":
            return self.send_json(200, {"sources": STORE.source_states(), "providers": [
                {"id": p["id"], "name": p.get("name", p["id"]), "enabled": p.get("enabled") is True} for p in load_config()]})
        if parsed.path.startswith("/api/"):
            return self.send_json(404, {"error": "API endpoint not found"})
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/admin/sync":
            return self.send_json(404, {"error": "API endpoint not found"})
        supplied = self.headers.get("X-REDLINE-ADMIN", "")
        if not ADMIN_TOKEN or not hmac.compare_digest(supplied, ADMIN_TOKEN):
            return self.send_json(401, {"error": "Unauthorized"})
        results = sync_all()
        self.send_json(200, {"checkedAt": iso(), "results": results})


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=scheduler, daemon=True, name="redline-feed-scheduler").start()
    # Hosting platforms inject PORT and require binding to 0.0.0.0.
    # Local Windows development keeps the safer localhost:8000 default.
    platform_port = os.environ.get("PORT")
    host = os.environ.get("REDLINE_HOST", "0.0.0.0" if platform_port else "127.0.0.1")
    port = int(platform_port or os.environ.get("REDLINE_PORT", "8000"))
    print(f"REDLINE serving {ROOT} on {host}:{port}")
    print(f"Provider config: {CONFIG_PATH}")
    if not ADMIN_TOKEN:
        print("WARNING: REDLINE_ADMIN_TOKEN is not set; manual admin sync is disabled.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
