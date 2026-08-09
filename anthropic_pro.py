"""Anthropic "pro" news bot — breaking + daily briefings for Telegram.

This is a from-scratch redesign that reuses the proven utilities from
``anthropic_news.py`` (Telegram sending, Korean summarization, UTF-8 stdout
reconfigure) but replaces the single-source NewsAPI pipeline with a
multi-source collector, a Claude "editor" scoring pass, event de-duplication,
and per-mode selection with posted-state tracking.

Usage:
    python anthropic_pro.py --mode breaking
    python anthropic_pro.py --mode daily
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import anthropic
import feedparser
import requests
from dotenv import load_dotenv

# --- UTF-8 stdout/stderr (reused from anthropic_news.py) -------------------
# Force UTF-8 so Korean log lines render correctly under a non-UTF-8 Windows
# codepage (default cp949 mangles Hangul / turns em-dash into \uXXXX escapes).
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="backslashreplace")

load_dotenv()

# --- Configuration ---------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
NEWSDATA_API_KEY = os.environ.get("NEWSDATA_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@stayhungry_asi")

KST = timezone(timedelta(hours=9))

# claude-sonnet-4-6 is used both for the editor scoring pass (per spec) and for
# Korean summarization (matching the existing anthropic_news.py bot).
MODEL = "claude-sonnet-4-6"

# Telegram hard-caps messages at 4096 chars; keep a small margin for safety
# since HTML entities can expand.
TELEGRAM_CHUNK_LIMIT = 4000

# Editor scoring only sees this many candidates (keeps the prompt bounded).
MAX_SCORING_CANDIDATES = 40

# Breaking-mode freshness ceiling: an article older than this (by publish time)
# is never sent as breaking, even if it passes the importance/official gate.
BREAKING_MAX_AGE_HOURS = 48

# Daily-mode lookback window: the morning briefing only considers articles
# published within this window.
DAILY_WINDOW_HOURS = 168  # 7 days

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_FILE = os.path.join(STATE_DIR, "anthropic_posted.json")
STATE_RETENTION_DAYS = 30

# Official newsroom mirrors, tried in order.
OFFICIAL_MIRRORS = (
    "https://rsshub.bestblogs.dev/anthropic/news",
    "https://raw.githubusercontent.com/taobojlen/anthropic-rss-feed/main/anthropic_news_rss.xml",
)
ANTHROPIC_NEWS_HTML = "https://www.anthropic.com/news"

# First-tier media feeds. ``tag=True`` means the feed is already scoped to
# Anthropic (every item passes); ``tag=False`` feeds are general and only items
# mentioning "Anthropic" in the title/body survive.
MEDIA_FEEDS = (
    {"name": "TechCrunch", "url": "https://techcrunch.com/tag/anthropic/feed/", "tag": True},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "tag": False},
    {"name": "Ars Technica", "url": "https://arstechnica.com/feed/", "tag": False},
    {"name": "VentureBeat", "url": "https://venturebeat.com/category/ai/feed/", "tag": False},
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "tag": False},
)

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?"
    "q=Anthropic%20when%3A2d&hl=en-US&gl=US&ceid=US:en"
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic-pro-bot")


# --- HTTP / feed helpers ---------------------------------------------------
def _http_get(url: str, **kwargs) -> requests.Response | None:
    """GET with shared headers/timeout; returns None on any failure."""
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        log.warning("HTTP GET failed for %s: %s", url, exc)
        return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _parse_feed(content: bytes):
    """feedparser.parse over already-fetched bytes."""
    parsed = feedparser.parse(content)
    return parsed.entries or []


def _entry_body(entry: dict) -> str:
    parts = [entry.get("summary", "")]
    for c in entry.get("content", []) or []:
        parts.append(c.get("value", ""))
    return _strip_html("\n".join(p for p in parts if p))


def _mentions_anthropic(title: str, body: str) -> bool:
    return "anthropic" in (f"{title}\n{body}").lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entry_published(entry: dict) -> tuple[str, bool]:
    """RSS publish time as ``(utc_iso, reliable)``; collection time as fallback.

    feedparser normalizes dates into ``published_parsed`` / ``updated_parsed``
    (UTC ``time.struct_time``). All values are normalized to UTC so ISO strings
    sort chronologically as plain text. ``reliable`` is False when no real date
    was found and the collection time was substituted.
    """
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            return datetime(*tm[:6], tzinfo=timezone.utc).isoformat(), True
    return _now_iso(), False


def _parse_iso(iso: str) -> datetime | None:
    """Parse a UTC ISO string into an aware datetime, or None on failure."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _display_date(iso: str) -> str:
    """Render a UTC ISO timestamp as a KST calendar date (YYYY-MM-DD)."""
    dt = _parse_iso(iso)
    return dt.astimezone(KST).strftime("%Y-%m-%d") if dt else "?"


def _make_candidate(
    title: str,
    url: str,
    source: str,
    body: str,
    official: bool,
    published: str,
    published_reliable: bool,
) -> dict:
    return {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "source": source,
        "body": (body or "").strip(),
        "is_official_source": official,
        "published": published or _now_iso(),
        "published_reliable": bool(published_reliable),
    }


# --- Source collectors -----------------------------------------------------
def collect_official() -> list[dict]:
    """Anthropic newsroom: try RSS mirrors in order, else scrape the HTML."""
    for mirror in OFFICIAL_MIRRORS:
        resp = _http_get(mirror)
        if resp is None:
            continue
        entries = _parse_feed(resp.content)
        if entries:
            log.info("Official newsroom via mirror: %s (%d items)", mirror, len(entries))
            out = []
            for e in entries:
                pub, reliable = _entry_published(e)
                out.append(
                    _make_candidate(
                        e.get("title", ""),
                        e.get("link", ""),
                        "Anthropic",
                        _entry_body(e),
                        official=True,
                        published=pub,
                        published_reliable=reliable,
                    )
                )
            return out

    # Both RSS mirrors failed — scrape https://www.anthropic.com/news.
    log.info("Official RSS mirrors failed; scraping HTML from %s", ANTHROPIC_NEWS_HTML)
    resp = _http_get(ANTHROPIC_NEWS_HTML)
    if resp is None:
        return []
    candidates: list[dict] = []
    seen: set[str] = set()
    # Best-effort anchor extraction (no bs4 dependency).
    for match in re.finditer(
        r'<a[^>]+href="(/news/[^"#?]+)"[^>]*>(.*?)</a>', resp.text, re.DOTALL | re.IGNORECASE
    ):
        href, inner = match.group(1), _strip_html(match.group(2))
        if not inner or href in seen:
            continue
        seen.add(href)
        candidates.append(
            _make_candidate(
                inner,
                f"https://www.anthropic.com{href}",
                "Anthropic",
                "",
                official=True,
                published=_now_iso(),
                published_reliable=False,  # HTML listing carries no reliable date
            )
        )
    log.info("Scraped %d official article link(s) from HTML", len(candidates))
    return candidates


def collect_media() -> list[dict]:
    """First-tier media RSS feeds; skip feeds that fail to fetch/parse."""
    candidates: list[dict] = []
    for feed in MEDIA_FEEDS:
        resp = _http_get(feed["url"])
        if resp is None:
            continue
        entries = _parse_feed(resp.content)
        if not entries:
            log.warning("No entries from %s", feed["name"])
            continue
        kept = 0
        for e in entries:
            title = e.get("title", "")
            body = _entry_body(e)
            if not feed["tag"] and not _mentions_anthropic(title, body):
                continue  # general feed → require an Anthropic mention
            pub, reliable = _entry_published(e)
            candidates.append(
                _make_candidate(
                    title,
                    e.get("link", ""),
                    feed["name"],
                    body,
                    official=False,
                    published=pub,
                    published_reliable=reliable,
                )
            )
            kept += 1
        log.info("%s: kept %d/%d item(s)", feed["name"], kept, len(entries))
    return candidates


def collect_newsdata() -> list[dict]:
    """NewsData.io Latest endpoint; skipped entirely without an API key."""
    if not NEWSDATA_API_KEY:
        log.info("NEWSDATA_API_KEY not set — skipping NewsData.io source.")
        return []
    resp = _http_get(
        "https://newsdata.io/api/1/latest",
        params={"apikey": NEWSDATA_API_KEY, "q": "Anthropic", "language": "en"},
    )
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError:
        log.warning("NewsData.io returned non-JSON response.")
        return []
    results = data.get("results") or []
    log.info("NewsData.io returned %d result(s)", len(results))
    candidates = []
    for r in results:
        pub, reliable = _parse_newsdata_date(r.get("pubDate"))
        candidates.append(
            _make_candidate(
                r.get("title", ""),
                r.get("link", ""),
                r.get("source_id") or "NewsData",
                r.get("description", "") or "",
                official=False,
                published=pub,
                published_reliable=reliable,
            )
        )
    return candidates


def _parse_newsdata_date(pub_date: str | None) -> tuple[str, bool]:
    """NewsData.io pubDate ('YYYY-MM-DD HH:MM:SS', UTC) → (utc_iso, reliable)."""
    if pub_date:
        try:
            dt = datetime.strptime(pub_date, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).isoformat(), True
        except ValueError:
            pass
    return _now_iso(), False


def _resolve_google_link(url: str) -> str:
    """Follow Google News' 302 redirect to the original article URL."""
    if not url:
        return url
    try:
        resp = requests.get(
            url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True
        )
        if resp.url and "news.google.com" not in resp.url:
            return resp.url
    except requests.RequestException as exc:
        log.warning("Google News redirect resolve failed: %s", exc)
    return url  # keep original link on failure


def collect_google_news() -> list[dict]:
    resp = _http_get(GOOGLE_NEWS_RSS)
    if resp is None:
        return []
    entries = _parse_feed(resp.content)
    log.info("Google News RSS returned %d item(s)", len(entries))
    candidates = []
    for e in entries:
        title = e.get("title", "")
        # Google News titles are usually "Headline - Source"; use the trailing
        # source name when present.
        source = "Google News"
        if " - " in title:
            source = title.rsplit(" - ", 1)[1].strip() or source
        pub, reliable = _entry_published(e)
        candidates.append(
            _make_candidate(
                title,
                _resolve_google_link(e.get("link", "")),
                source,
                _entry_body(e),
                official=False,
                published=pub,
                published_reliable=reliable,
            )
        )
    return candidates


def collect_all() -> list[dict]:
    candidates: list[dict] = []
    candidates += collect_official()
    candidates += collect_media()
    candidates += collect_newsdata()
    candidates += collect_google_news()
    log.info("Collected %d raw candidate(s) across all sources", len(candidates))
    return candidates


# --- Dedup pass 1 (URL + title) --------------------------------------------
def _norm_url(url: str) -> str:
    url = (url or "").strip().lower()
    url = re.sub(r"[?#].*$", "", url)  # drop query/fragment (tracking params)
    return url.rstrip("/")


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def dedupe_basic(candidates: list[dict]) -> list[dict]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict] = []
    for c in candidates:
        if not c["title"] or not c["url"]:
            continue
        u, t = _norm_url(c["url"]), _norm_title(c["title"])
        if u in seen_urls or t in seen_titles:
            continue
        seen_urls.add(u)
        seen_titles.add(t)
        out.append(c)
    log.info("After basic dedupe: %d candidate(s)", len(out))
    return out


# --- Claude editor scoring -------------------------------------------------
def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = [l for l in text.splitlines() if not l.startswith("```")]
        text = "\n".join(lines).strip()
    return text


def score_candidates(client: anthropic.Anthropic, candidates: list[dict]) -> None:
    """Attach relevance/importance/cluster/is_official to each candidate in place.

    Falls back to neutral scores (5/5, unique cluster) if the model call or
    JSON parse fails so downstream selection still runs deterministically.
    """
    # Neutral defaults up front so every candidate always has the fields.
    for i, c in enumerate(candidates):
        c["id"] = i
        c["relevance"] = 5
        c["importance"] = 5
        c["cluster"] = f"c{i}"
        c["is_official"] = c["is_official_source"]

    batch = candidates[:MAX_SCORING_CANDIDATES]
    payload = [
        {
            "id": c["id"],
            "title": c["title"],
            "source": c["source"],
            "snippet": c["body"][:300],
            "is_official_source": c["is_official_source"],
        }
        for c in batch
    ]

    system = (
        "AI 산업 전문 뉴스 편집자로서 앤트로픽 관련성과 뉴스 중요도를 냉정히 채점. "
        "단순 언급·개인 블로그 체험기·패키지 등재는 낮게. JSON만 출력."
    )
    user = (
        "다음 후보 기사들을 채점하라. 각 기사에 대해 relevance(앤트로픽 관련성 0-10), "
        "importance(뉴스 중요도 0-10), cluster(같은 사건이면 동일 문자열), "
        "is_official(앤트로픽 공식 발표 여부 bool)을 매겨라.\n\n"
        f"후보(JSON):\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "출력은 JSON 배열만. 스키마: "
        '[{"id": 0, "relevance": 0, "importance": 0, "cluster": "이벤트식별자", "is_official": false}]'
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        raw = next((b.text for b in response.content if b.type == "text"), "")
        scores = json.loads(_strip_code_fence(raw))
    except Exception:
        log.exception("Editor scoring failed — using neutral fallback scores.")
        return

    by_id = {c["id"]: c for c in candidates}
    for s in scores:
        c = by_id.get(s.get("id"))
        if c is None:
            continue
        try:
            c["relevance"] = max(0, min(10, int(s.get("relevance", 5))))
            c["importance"] = max(0, min(10, int(s.get("importance", 5))))
        except (TypeError, ValueError):
            pass
        cluster = s.get("cluster")
        if cluster:
            c["cluster"] = str(cluster)
        c["is_official"] = bool(s.get("is_official")) or c["is_official_source"]
    log.info("Editor scored %d candidate(s)", len(scores))


# --- Dedup pass 2 (cluster) ------------------------------------------------
def dedupe_clusters(candidates: list[dict]) -> list[dict]:
    """Keep the single highest-importance article per cluster.

    Ties break toward official sources, then higher relevance. The surviving
    article carries the best (cited) source for that event.
    """
    best: dict[str, dict] = {}
    for c in candidates:
        key = c["cluster"]
        cur = best.get(key)
        if cur is None:
            best[key] = c
            continue
        cand_rank = (c["importance"], c["is_official"], c["relevance"])
        cur_rank = (cur["importance"], cur["is_official"], cur["relevance"])
        if cand_rank > cur_rank:
            best[key] = c
    out = list(best.values())
    log.info("After cluster dedupe: %d candidate(s)", len(out))
    return out


# --- Per-mode selection ----------------------------------------------------
def select_breaking(candidates: list[dict]) -> list[dict]:
    """Breaking gate + freshness ceiling.

    An article must pass the importance/official gate AND have a reliable
    publish date within BREAKING_MAX_AGE_HOURS. Articles whose date fell back
    to collection time (``published_reliable`` False) are excluded so a stale
    story with an unknown date can't masquerade as breaking news.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=BREAKING_MAX_AGE_HOURS)
    picked: list[dict] = []
    gated = dropped_unreliable = dropped_stale = 0
    for c in candidates:
        if not ((c["importance"] >= 8 and c["relevance"] >= 7) or c["is_official"]):
            continue
        gated += 1  # passed the importance/official gate
        if not c["published_reliable"]:
            dropped_unreliable += 1
            log.info("  breaking drop (unreliable date): %s", c["title"][:70])
            continue
        pub = _parse_iso(c["published"])
        if pub is None or pub < cutoff:
            dropped_stale += 1
            log.info(
                "  breaking drop (stale, pub=%s): %s",
                _display_date(c["published"]),
                c["title"][:70],
            )
            continue
        picked.append(c)
    log.info(
        "Breaking gate passed %d; after freshness filter (<= %dh): %d "
        "(dropped %d stale, %d unreliable-date)",
        gated,
        BREAKING_MAX_AGE_HOURS,
        len(picked),
        dropped_stale,
        dropped_unreliable,
    )
    picked.sort(key=lambda c: (c["importance"] + c["relevance"]), reverse=True)
    return picked


def select_daily(candidates: list[dict]) -> list[dict]:
    """Recent-window briefing: in-window candidates only, up to 5.

    Keep only articles with a reliable publish date within DAILY_WINDOW_HOURS,
    then rank by relevance+importance (tie-break: newest published first) and
    take at most 5. Fewer than 5 in-window means fewer picks — never padded
    with older stories.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=DAILY_WINDOW_HOURS)
    fresh: list[dict] = []
    dropped_stale = dropped_unreliable = 0
    for c in candidates:
        if not c["published_reliable"]:
            dropped_unreliable += 1
            continue
        pub = _parse_iso(c["published"])
        if pub is None or pub < cutoff:
            dropped_stale += 1
            continue
        fresh.append(c)
    log.info(
        "Daily window (<= %dh): %d in-window (dropped %d stale, %d unreliable-date)",
        DAILY_WINDOW_HOURS,
        len(fresh),
        dropped_stale,
        dropped_unreliable,
    )
    # Primary: relevance + importance. Tie-break: newest published first
    # (UTC ISO strings sort chronologically as plain text).
    fresh.sort(
        key=lambda c: (c["relevance"] + c["importance"], c["published"]), reverse=True
    )
    return fresh[:5]


# --- Posted-state tracking -------------------------------------------------
def load_state() -> list[dict]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def filter_already_posted(candidates: list[dict], state: list[dict]) -> list[dict]:
    posted_urls = {_norm_url(e.get("url", "")) for e in state}
    posted_titles = {_norm_title(e.get("title", "")) for e in state}
    out = [
        c
        for c in candidates
        if _norm_url(c["url"]) not in posted_urls
        and _norm_title(c["title"]) not in posted_titles
    ]
    log.info("After posted-state filter: %d candidate(s)", len(out))
    return out


def update_state(state: list[dict], sent: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    for item in sent:
        state.append(
            {
                "url": item["url"],
                "title": item["title"],
                "published": item["published"],
                "sent_at": now_iso,
            }
        )

    # Keep only the last STATE_RETENTION_DAYS worth of entries.
    cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    pruned = []
    for e in state:
        try:
            ts = datetime.fromisoformat(e.get("sent_at", ""))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            pruned.append(e)

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)
    log.info("State updated: %d entry(ies) retained", len(pruned))


# --- Korean summarization (reused pattern from anthropic_news.py) ----------
def summarize(client: anthropic.Anthropic, title: str, body: str) -> dict:
    system = (
        "You are a Korean news summarization assistant for Anthropic-related articles. "
        "Translate the headline into natural Korean and produce exactly 3 concise, "
        "factual bullet points in Korean. Stick strictly to facts present in the "
        "article — do not speculate, embellish, or add outside information. Each "
        "bullet should be a single sentence."
    )
    user = (
        f"Article title: {title}\n\n"
        f"Article body:\n{body}\n\n"
        "Respond with JSON only, no other text or markdown fences. Schema:\n"
        '{"korean_title": "한국어로 번역한 제목", '
        '"summary": ["요약 1", "요약 2", "요약 3"]}'
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(_strip_code_fence(raw))


# --- Telegram delivery (reused from anthropic_news.py) ---------------------
def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _header(mode: str) -> str:
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    if mode == "breaking":
        return f"🚨 <b>앤트로픽 속보</b> ({now_kst})"
    return f"🤖 <b>앤트로픽 브리핑</b> (최근 7일 · {now_kst})"


def build_messages(items: list[dict], mode: str) -> list[str]:
    header = _header(mode)

    blocks: list[str] = []
    for i, item in enumerate(items, 1):
        title = _html_escape(item["title"])
        source = _html_escape(item["source"])
        url = _html_escape(item["url"])
        lines = [f"<b>{i}. {title}</b>"]
        for s in item["summary"]:
            lines.append(f"- {_html_escape(s)}")
        date = _html_escape(_display_date(item["published"]))
        lines.append(
            f'📰 출처: {source} | 📅 {date} | 🔗 <a href="{url}">원문 링크</a>'
        )
        blocks.append("\n".join(lines))

    chunks: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}\n---"
        if len(candidate) > TELEGRAM_CHUNK_LIMIT:
            chunks.append(current)
            current = f"{header}\n\n{block}\n---"
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(chunks: list[str]) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in chunks:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHANNEL,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
            timeout=30,
        )
        if not resp.ok:
            try:
                body = resp.json()
                desc = body.get("description") or body
            except ValueError:
                desc = resp.text
            raise RuntimeError(f"Telegram {resp.status_code}: {desc}")
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")


# --- Main ------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Anthropic news bot (breaking/daily).")
    parser.add_argument(
        "--mode",
        choices=["breaking", "daily"],
        required=True,
        help="breaking: 속보 게이트 / daily: 상위 5개 브리핑",
    )
    args = parser.parse_args()
    mode = args.mode
    log.info("=== Anthropic pro bot starting (mode=%s) ===", mode)

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY is required for scoring and summarization.")
        return 1
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 1) Collect + basic dedupe.
    candidates = dedupe_basic(collect_all())
    if not candidates:
        log.info("No candidates collected — nothing to do.")
        return 0

    # 2) Editor scoring, then cluster dedupe.
    score_candidates(client, candidates)
    candidates = dedupe_clusters(candidates)

    # 3) Posted-state filter (essential for breaking to avoid re-alerting).
    state = load_state()
    candidates = filter_already_posted(candidates, state)

    # 4) Per-mode selection.
    if mode == "breaking":
        selected = select_breaking(candidates)
    else:
        selected = select_daily(candidates)

    log.info("=== Selected %d article(s) for mode=%s ===", len(selected), mode)
    for i, c in enumerate(selected, 1):
        log.info(
            "  %d. [rel=%d imp=%d official=%s pub=%s] %s",
            i,
            c["relevance"],
            c["importance"],
            c["is_official"],
            _display_date(c["published"]),
            c["title"][:80],
        )

    if not selected:
        if mode == "daily":
            # Daily always posts once a day — send an explicit "no news" line
            # so the channel gets its morning briefing either way.
            log.info("No in-window articles — sending daily 'no news' notice.")
            send_telegram([f"{_header('daily')}\n\n지난 7일간 새 주요 소식 없음."])
            log.info("Done.")
            return 0
        # For breaking, silence is the correct outcome when nothing qualifies.
        log.info("Nothing qualifies for mode=%s — exiting without sending.", mode)
        return 0

    # 5) Korean summaries for selected articles only.
    items: list[dict] = []
    for c in selected:
        try:
            parsed = summarize(client, c["title"], c["body"] or c["title"])
            title = parsed.get("korean_title") or c["title"]
            summary = (parsed.get("summary") or [])[:3]
        except Exception:
            log.exception("Summarization failed for: %s", c["title"][:80])
            title, summary = c["title"], []
        items.append(
            {
                "title": title,
                "summary": summary,
                "source": c["source"],
                "url": c["url"],
                "published": c["published"],
            }
        )

    # 6) Send.
    chunks = build_messages(items, mode)
    log.info("Sending %d Telegram message(s) to %s", len(chunks), TELEGRAM_CHANNEL)
    send_telegram(chunks)

    # 7) Persist posted state.
    update_state(state, selected)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
