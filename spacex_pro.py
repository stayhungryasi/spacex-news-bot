"""SpaceX "pro" news bot — breaking + daily briefings for Telegram.

Same architecture as ``anthropic_pro.py`` (mode branching, multi-source
collection, a Claude "editor" scoring pass, cluster de-duplication, publish-date
freshness filtering, posted-state tracking, chunked Telegram delivery) with the
sources, editor rubric, and copy retargeted at SpaceX.

Usage:
    python spacex_pro.py --mode breaking
    python spacex_pro.py --mode daily
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

# --- UTF-8 stdout/stderr (reused from anthropic_pro.py) --------------------
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
# Korean summarization.
MODEL = "claude-sonnet-4-6"

# Telegram hard-caps messages at 4096 chars; keep a small margin for safety
# since HTML entities can expand.
TELEGRAM_CHUNK_LIMIT = 4000

# Editor scoring only sees this many candidates (keeps the prompt bounded).
# Anything beyond this budget stays unscored and is dropped before selection —
# an unscored article keeps the neutral 5/5 default, which must never be allowed
# to outrank a genuinely scored one.
MAX_SCORING_CANDIDATES = 40

# Per-source candidate caps applied *before* scoring. Google News alone returns
# ~100 items per run (mostly near-duplicate stock coverage), which would eat the
# whole scoring budget; the trade feeds carry far more editorial value per item,
# so they are trimmed last. Within a source the newest items are kept.
COLLECTOR_LIMITS = {
    "media": 40,
    "ll2": 10,
    "newsdata": 20,
    "google": 30,
}

# When the scoring budget is still oversubscribed after per-source caps, sources
# are trimmed from the back of this list first (lowest editorial value first).
COLLECTOR_PRIORITY = ("media", "ll2", "newsdata", "google")

# Routine launches (regular Starlink batches, repeat Falcon 9 missions) are
# demoted by this much when ranking. Selection also tiers non-routine above
# routine outright, so the penalty only orders items *within* a tier.
ROUTINE_PENALTY = 8

# Breaking-mode freshness ceiling: an article older than this (by publish time)
# is never sent as breaking, even if it passes the importance/official gate.
BREAKING_MAX_AGE_HOURS = 48

# Breaking gate thresholds. Kept in sync with the editor prompt on purpose: the
# prompt tells the editor to score its high-signal categories (신기록, 폭발/이상,
# 유인 미션, 대형 계약 …) at 8~10, so the gate has to admit the bottom of that
# band. At >= 9 the band's floor was unreachable and record-setting launches
# scored 8 by an obedient editor were silently blocked.
BREAKING_MIN_IMPORTANCE = 8
BREAKING_MIN_RELEVANCE = 7

# Daily-mode lookback window: the briefing only considers articles published
# within this window.
DAILY_WINDOW_HOURS = 168  # 7 days

# Separate state file from the Anthropic bot — the two must not share history.
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_FILE = os.path.join(STATE_DIR, "spacex_posted.json")
STATE_RETENTION_DAYS = 30

# Space-trade RSS feeds. All are general space/tech feeds (no SpaceX-scoped
# tag feed exists for these outlets), so every item must mention one of
# SPACEX_KEYWORDS in its title or body to survive collection. Every URL below
# was fetched and confirmed to return a parseable feed; any that fails at
# runtime is skipped rather than aborting the run.
MEDIA_FEEDS = (
    {"name": "Spaceflight Now", "url": "https://spaceflightnow.com/feed/"},
    {"name": "NASASpaceflight", "url": "https://www.nasaspaceflight.com/feed/"},
    {"name": "SpaceNews", "url": "https://spacenews.com/feed/"},
    {"name": "Ars Technica", "url": "https://arstechnica.com/feed/"},
)

# Word-boundary matched so "Dragon Age" / "Falcon Northwest" style false hits in
# the general tech feeds stay rare. Borderline matches that slip through are
# knocked down by the editor's relevance score.
SPACEX_KEYWORDS = ("SpaceX", "Starship", "Starlink", "Falcon", "Dragon", "Raptor")
_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in SPACEX_KEYWORDS) + r")\b", re.IGNORECASE
)

# Launch Library 2 — one request per run (the free tier is rate-limited).
#
# Deviation from the literal spec URL, documented on purpose: with
# ``ordering=-net`` alone the first 10 results are TBD placeholders years out
# (2029-2031 "To Be Determined" entries), which are useless as news and would
# also read as "fresh" to the publish-date filters. Adding ``net__lte=<now>``
# keeps the single request but returns the 10 most recent *flown* launches,
# which is what the freshness windows below actually want.
LL2_URL = "https://ll.thespacedevs.com/2.2.0/launch/"
LL2_PARAMS = {"search": "SpaceX", "limit": 10, "ordering": "-net"}

# Human-readable launch page on The Space Devs' own consumer site, keyed by the
# ``slug`` every LL2 result carries. Used as the link fallback because a bare
# ``launch/<id>/`` API URL renders as raw JSON in a subscriber's browser.
LL2_PAGE_URL = "https://spacelaunchnow.me/launch/{slug}/"

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?"
    "q=SpaceX%20when%3A2d&hl=en-US&gl=US&ceid=US:en"
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spacex-pro-bot")


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


def _mentions_spacex(title: str, body: str) -> bool:
    return bool(_KEYWORD_RE.search(f"{title}\n{body}"))


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
    collector: str,
) -> dict:
    return {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "source": source,
        "body": (body or "").strip(),
        "is_official_source": official,
        "published": published or _now_iso(),
        "published_reliable": bool(published_reliable),
        "collector": collector,  # which COLLECTOR_LIMITS bucket this belongs to
    }


# --- Source collectors -----------------------------------------------------
def collect_media() -> list[dict]:
    """Space-trade RSS feeds; skip feeds that fail to fetch/parse."""
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
            if not _mentions_spacex(title, body):
                continue  # general feed → require a SpaceX-family keyword
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
                    collector="media",
                )
            )
            kept += 1
        log.info("%s: kept %d/%d item(s)", feed["name"], kept, len(entries))
    return candidates


def collect_launch_library() -> list[dict]:
    """Launch Library 2 → one request per run, recent launches as candidates.

    Each launch becomes a candidate headline ("<name> — <status>") with the
    mission blurb as the body and ``net`` (the launch time) as the publish
    date, so the same freshness windows apply as to news articles.

    ``is_official_source`` stays False on purpose: LL2 is authoritative launch
    *data*, not a SpaceX press release, and the breaking gate treats
    ``is_official`` as an automatic pass — flagging routine Starlink batches
    official would push every one of them out as a breaking alert.
    """
    params = dict(LL2_PARAMS)
    params["net__lte"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = _http_get(LL2_URL, params=params)
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError:
        log.warning("Launch Library 2 returned non-JSON response.")
        return []

    results = data.get("results") or []
    log.info("Launch Library 2 returned %d launch(es)", len(results))
    candidates: list[dict] = []
    for r in results:
        name = (r.get("name") or "").strip()
        net = r.get("net")
        if not name or not net:
            continue
        pub = _parse_iso(net.replace("Z", "+00:00"))
        if pub is None:
            continue
        status = (r.get("status") or {}).get("name") or "Status unknown"
        mission = r.get("mission") or {}
        pad = r.get("pad") or {}
        pad_name = " / ".join(
            p for p in (pad.get("name"), (pad.get("location") or {}).get("name")) if p
        )
        body_parts = [
            f"Launch: {name}",
            f"Status: {status}",
            f"NET: {net}",
        ]
        if pad_name:
            body_parts.append(f"Pad: {pad_name}")
        if mission.get("description"):
            body_parts.append(mission["description"])
        if r.get("failreason"):
            body_parts.append(f"Failure reason: {r['failreason']}")

        # LL2 list responses carry no article link. Prefer a mission info/video
        # URL, then the human-readable launch page built from ``slug``, and only
        # fall back to the launch's own API URL when there is no slug at all —
        # in practice ``info_urls``/``vid_urls`` come back empty for every SpaceX
        # launch, so the slug page is what subscribers actually get.
        slug = (r.get("slug") or "").strip()
        link = LL2_PAGE_URL.format(slug=slug) if slug else r.get("url", "")
        for key in ("info_urls", "vid_urls"):
            urls = mission.get(key) or []
            if urls and urls[0].get("url"):
                link = urls[0]["url"]
                break

        candidates.append(
            _make_candidate(
                f"{name} — {status}",
                link,
                "Launch Library 2",
                "\n".join(body_parts),
                official=False,
                published=pub.isoformat(),
                published_reliable=True,
                collector="ll2",
            )
        )
    return candidates


def collect_newsdata() -> list[dict]:
    """NewsData.io Latest endpoint; skipped entirely without an API key."""
    if not NEWSDATA_API_KEY:
        log.info("NEWSDATA_API_KEY not set — skipping NewsData.io source.")
        return []
    resp = _http_get(
        "https://newsdata.io/api/1/latest",
        params={
            "apikey": NEWSDATA_API_KEY,
            "q": "SpaceX OR Starship",
            "language": "en",
        },
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
                collector="newsdata",
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


# Google News' internal RPC for turning an /rss/articles/ id into the publisher
# URL. The old plain-302 redirect no longer exists: the article page now returns
# 200 and resolves the target in JavaScript, so following redirects just lands
# back on news.google.com.
GOOGLE_BATCHEXECUTE = (
    "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"
)
_GNEWS_ID_RE = re.compile(
    r'data-n-a-id="([^"]+)"[^>]*data-n-a-ts="(\d+)"[^>]*data-n-a-sg="([^"]+)"'
)
_GNEWS_RESULT_RE = re.compile(r'garturlres\\?",\\?"(https?://[^"\\]+)')


def _resolve_google_link(url: str) -> str:
    """Resolve a Google News link to the original publisher URL.

    Called only for *selected* articles (see ``resolve_links``), never for the
    whole feed — the old blanket resolution cost ~2 minutes per run for ~100
    items that were almost all discarded before ever being shown.

    Two steps: fetch the article page for its ``data-n-a-{id,ts,sg}`` triple,
    then ask Google's ``batchexecute`` RPC to trade that signed triple for the
    publisher URL. Any failure keeps the original link rather than dropping the
    article — a Google News link still opens the right story, just uglier.
    """
    if not url:
        return url
    try:
        page = requests.get(
            url, headers=REQUEST_HEADERS, timeout=15, allow_redirects=True
        )
        # Some older links still 302 straight through to the publisher.
        if page.url and "news.google.com" not in page.url:
            return page.url
        page.raise_for_status()

        match = _GNEWS_ID_RE.search(page.text)
        if match is None:
            log.warning("Google News: no signature found for %s", url[:80])
            return url
        article_id, timestamp, signature = match.groups()

        inner = json.dumps(
            [
                "garturlreq",
                [
                    ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                     None, None, None, None, None, 0, 1],
                    "X",
                    "X",
                    1,
                    [1, 1, 1],
                    1,
                    1,
                    None,
                    0,
                    0,
                    None,
                    0,
                ],
                article_id,
                int(timestamp),
                signature,
            ]
        )
        payload = json.dumps([[["Fbv4je", inner, None, "generic"]]])
        resp = requests.post(
            GOOGLE_BATCHEXECUTE,
            headers=REQUEST_HEADERS,
            data={"f.req": payload},
            timeout=20,
        )
        resp.raise_for_status()
        result = _GNEWS_RESULT_RE.search(resp.text)
        if result is None:
            log.warning("Google News: RPC returned no URL for %s", url[:80])
            return url
        return result.group(1)
    except requests.RequestException as exc:
        log.warning("Google News link resolve failed: %s", exc)
    except (ValueError, TypeError) as exc:
        log.warning("Google News link resolve parse error: %s", exc)
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
                # Left as the news.google.com redirect here; resolved after
                # selection so only the handful of articles actually sent pay
                # the per-link HTTP cost.
                e.get("link", ""),
                source,
                _entry_body(e),
                official=False,
                published=pub,
                published_reliable=reliable,
                collector="google",
            )
        )
    return candidates


def resolve_links(candidates: list[dict]) -> None:
    """Resolve Google News redirects on the final selection, in place."""
    for c in candidates:
        if "news.google.com" in c["url"]:
            resolved = _resolve_google_link(c["url"])
            if resolved != c["url"]:
                log.info("Resolved Google News link: %s", resolved[:100])
            c["url"] = resolved


def collect_all() -> list[dict]:
    candidates: list[dict] = []
    candidates += collect_media()
    candidates += collect_launch_library()
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


# --- Pre-scoring source caps -----------------------------------------------
def limit_by_source(candidates: list[dict]) -> list[dict]:
    """Trim candidates to the scoring budget, source by source.

    Two stages, both logged rather than silent:
      1. Each collector is capped at its COLLECTOR_LIMITS entry, keeping the
         newest items in that bucket.
      2. If the total still exceeds MAX_SCORING_CANDIDATES, buckets are trimmed
         from the back of COLLECTOR_PRIORITY (Google News first) until it fits,
         so the trade press is never starved by aggregator volume.
    """
    buckets: dict[str, list[dict]] = {}
    for c in candidates:
        buckets.setdefault(c["collector"], []).append(c)

    for name, items in buckets.items():
        # Newest first — ISO UTC strings sort chronologically as plain text.
        items.sort(key=lambda c: c["published"], reverse=True)
        cap = COLLECTOR_LIMITS.get(name, MAX_SCORING_CANDIDATES)
        if len(items) > cap:
            log.info("Source cap: %s %d -> %d", name, len(items), cap)
            buckets[name] = items[:cap]

    total = sum(len(v) for v in buckets.values())
    # Trim the lowest-priority sources until the batch fits the scoring budget.
    for name in reversed(COLLECTOR_PRIORITY):
        if total <= MAX_SCORING_CANDIDATES:
            break
        items = buckets.get(name)
        if not items:
            continue
        keep = max(0, len(items) - (total - MAX_SCORING_CANDIDATES))
        log.info(
            "Scoring budget (%d): trimming %s %d -> %d",
            MAX_SCORING_CANDIDATES,
            name,
            len(items),
            keep,
        )
        total -= len(items) - keep
        buckets[name] = items[:keep]

    # Rebuild in priority order so the scoring batch is deterministic.
    out: list[dict] = []
    for name in COLLECTOR_PRIORITY:
        out.extend(buckets.get(name, []))
    for name, items in buckets.items():  # any collector not in the priority list
        if name not in COLLECTOR_PRIORITY:
            out.extend(items)
    log.info(
        "After source caps: %d candidate(s) [%s]",
        len(out),
        ", ".join(f"{n}={len(buckets.get(n, []))}" for n in COLLECTOR_PRIORITY),
    )
    return out


# --- Claude editor scoring -------------------------------------------------
def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = [l for l in text.splitlines() if not l.startswith("```")]
        text = "\n".join(lines).strip()
    return text


def _loads_json(text: str, opener: str = "{"):
    """Parse model output as JSON, tolerating fences and stray prose.

    Straight ``json.loads`` fails whenever the model adds a preamble or a
    trailing sentence ("Extra data" errors). The fallback slices out the widest
    span between the first opening and last matching closing bracket and retries.
    ``opener`` is "{" for an object (summaries) or "[" for an array (scoring).
    """
    text = _strip_code_fence(text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    closer = "}" if opener == "{" else "]"
    match = re.search(
        rf"{re.escape(opener)}.*{re.escape(closer)}", text, re.DOTALL
    )
    if match is None:
        raise ValueError(f"no JSON {opener}{closer} block found in model output")
    return json.loads(match.group(0))


def score_candidates(client: anthropic.Anthropic, candidates: list[dict]) -> bool:
    """Attach relevance/importance/cluster/is_routine/is_official in place.

    Returns True when the editor pass ran, False when it failed outright. On
    failure every candidate keeps the neutral 5/5 default and the caller keeps
    them all (degraded but not silent); on success, candidates the editor did
    not return a score for stay ``scored=False`` and are dropped before
    selection so a neutral 5/5 can never outrank a real score.
    """
    # Neutral defaults up front so every candidate always has the fields.
    for i, c in enumerate(candidates):
        c["id"] = i
        c["relevance"] = 5
        c["importance"] = 5
        c["cluster"] = f"c{i}"
        c["is_routine"] = False
        c["is_official"] = c["is_official_source"]
        c["scored"] = False

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

    # The importance bands here are load-bearing: the breaking gate admits
    # importance >= BREAKING_MIN_IMPORTANCE, so a category scored below that
    # band can never alert. The record-setting rule is spelled out with explicit
    # anchors because the editor otherwise scored genuine "shortest ever" records
    # at 6~7 and they were silently blocked. The record clause also states
    # its precedence over the routine clause: a record-setting flight is
    # is_routine=False even when the payload is a regular Starlink batch,
    # since is_routine both disqualifies breaking and costs ROUTINE_PENALTY.
    system = (
        "우주·항공 산업 전문 뉴스 편집자. 정기 스타링크 배치·반복 팰컨9 발사 같은 "
        "일상 루틴은 importance 낮게(2~4) 매기고 is_routine=true. 스타십 시험비행·"
        "폭발/이상·유인 미션·대형 계약·규제/소송·신형 로켓은 높게(8~10).\n"
        "기록 달성은 다음 기준으로 구분하라. 발사 간격·회수 횟수·연간 발사 수 등에서 "
        "'사상 최초/최단/최다' 기록을 세운 경우 importance 9~10. "
        "예: 두 발사 간 최단 간격 신기록 = 9, 스타십 첫 궤도비행/첫 십 캐치 = 10, "
        "부스터 통산 N00번째 착륙 같은 단순 누적 이정표 = 6~7.\n"
        "'최다'는 연간 발사 수처럼 기간이 한정된 기록만 해당하고, 통산 N번째 "
        "비행·착륙은 숫자가 아무리 커도 기록이 아니라 이정표다. "
        "기록 조항은 루틴 조항보다 우선한다 — 신기록을 세운 발사는 페이로드가 "
        "정기 스타링크 배치여도 is_routine=false로 매겨라.\n"
        "주가 등락·밸류에이션·투자 유치·지분 공개 관련 단신으로 snippet에 실질 내용이 "
        "없고 헤드라인 수준의 반복에 그치는 경우 importance 3~5로 낮게 매겨라. "
        "단 실적 발표·대형 계약·IPO급 이벤트는 기존대로 높게.\nJSON만."
    )
    user = (
        "다음 후보 기사들을 채점하라. 각 기사에 대해 relevance(SpaceX 관련성 0-10), "
        "importance(뉴스 중요도 0-10), cluster(같은 사건이면 동일 문자열), "
        "is_routine(정기·반복 발사 등 일상 루틴 여부 bool), "
        "is_official(SpaceX 공식 발표 여부 bool)을 매겨라.\n\n"
        f"후보(JSON):\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "출력은 JSON 배열만. 스키마: "
        '[{"id": 0, "relevance": 0, "importance": 0, "cluster": "이벤트식별자", '
        '"is_routine": false, "is_official": false}]'
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        raw = next((b.text for b in response.content if b.type == "text"), "")
        scores = _loads_json(raw, opener="[")
    except Exception:
        log.exception("Editor scoring failed — using neutral fallback scores.")
        return False

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
        c["is_routine"] = bool(s.get("is_routine"))
        c["is_official"] = bool(s.get("is_official")) or c["is_official_source"]
        c["scored"] = True
    log.info(
        "Editor scored %d/%d candidate(s)",
        sum(1 for c in candidates if c["scored"]),
        len(candidates),
    )
    return True


# --- Dedup pass 2 (cluster) ------------------------------------------------
def dedupe_clusters(candidates: list[dict]) -> list[dict]:
    """Keep the single highest-importance article per cluster.

    Ties break toward official sources, then higher relevance. The surviving
    article carries the best (cited) source for that event — e.g. a Spaceflight
    Now write-up wins over the bare Launch Library entry for the same flight.
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
def _score(c: dict) -> int:
    """Ranking score: relevance + importance, minus the routine-launch penalty."""
    return c["relevance"] + c["importance"] - (ROUTINE_PENALTY if c["is_routine"] else 0)


def drop_unscored(candidates: list[dict]) -> list[dict]:
    """Remove candidates the editor never scored (they hold neutral 5/5)."""
    out = [c for c in candidates if c["scored"]]
    dropped = len(candidates) - len(out)
    if dropped:
        log.info("Dropped %d unscored candidate(s) before selection", dropped)
    return out


def select_breaking(candidates: list[dict]) -> list[dict]:
    """Breaking gate + freshness ceiling.

    An article must clear the gate — importance >= BREAKING_MIN_IMPORTANCE and
    relevance >= BREAKING_MIN_RELEVANCE while not being a routine launch, or an
    official SpaceX announcement — AND have a reliable publish date within
    BREAKING_MAX_AGE_HOURS. Articles whose date fell back to collection time
    (``published_reliable`` False) are excluded so a stale story with an unknown
    date can't masquerade as breaking news.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=BREAKING_MAX_AGE_HOURS)
    picked: list[dict] = []
    gated = dropped_unreliable = dropped_stale = 0
    for c in candidates:
        high = (
            c["importance"] >= BREAKING_MIN_IMPORTANCE
            and c["relevance"] >= BREAKING_MIN_RELEVANCE
            and not c["is_routine"]
        )
        if not (high or c["is_official"]):
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
    # Non-routine first (a routine launch that slipped through via is_official
    # must never head a breaking alert), then by penalized score.
    picked.sort(key=lambda c: (not c["is_routine"], _score(c)), reverse=True)
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
    # Tier 1: non-routine always above routine, so a regular Starlink batch can
    # only fill slots real news left empty.
    # Tier 2: penalized relevance + importance.
    # Tie-break: newest published first (UTC ISO strings sort chronologically).
    fresh.sort(
        key=lambda c: (not c["is_routine"], _score(c), c["published"]), reverse=True
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


# --- Korean summarization --------------------------------------------------
# CJK ideographs. The summarizer occasionally reaches for a hanja (著名, 中國)
# in otherwise-Korean prose, which reads as a defect in a subscriber briefing.
# The prompt forbids it and this catches the leaks the prompt misses.
#
# Scope note: this is the U+4E00-U+9FFF block only. Japanese kana and the rarer
# CJK extension blocks are deliberately not matched — widen the class here if
# those ever show up in practice.
_HANJA_RE = re.compile(r"[一-鿿]")


def _summary_texts(parsed: dict) -> list[str]:
    return [str(parsed.get("korean_title") or "")] + [
        str(s) for s in (parsed.get("summary") or [])
    ]


def _hanja_found(parsed: dict) -> str:
    """Distinct hanja across a summary payload, in first-seen order ("" if clean)."""
    hits = _HANJA_RE.findall(" ".join(_summary_texts(parsed)))
    return "".join(dict.fromkeys(hits))


def _scrub_hanja(text: str) -> str:
    """Drop hanja, then close the whitespace the removal leaves behind."""
    return re.sub(r"\s{2,}", " ", _HANJA_RE.sub("", text)).strip()


def _scrub_summary(parsed: dict) -> dict:
    """Last resort when a retry still comes back with hanja: strip the chars.

    Bullets that scrub down to nothing are dropped rather than rendered as an
    empty "- " line; a title that empties out lets the caller fall back to the
    original headline.
    """
    out = dict(parsed)
    if out.get("korean_title"):
        out["korean_title"] = _scrub_hanja(str(out["korean_title"]))
    out["summary"] = [
        scrubbed
        for s in (out.get("summary") or [])
        if (scrubbed := _scrub_hanja(str(s)))
    ]
    return out


def _summarize_once(
    client: anthropic.Anthropic, system: str, user: str
) -> dict:
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    return _loads_json(raw, opener="{")


def summarize(client: anthropic.Anthropic, title: str, body: str) -> dict:
    system = (
        "You are a Korean news summarization assistant for SpaceX and space "
        "industry articles. Translate the headline into natural Korean and "
        "produce exactly 3 concise, factual bullet points in Korean. Stick "
        "strictly to facts present in the article — do not speculate, "
        "embellish, or add outside information. Keep rocket, mission, and "
        "vehicle names in their commonly used Korean forms (팰컨9, 스타십, "
        "스타링크, 드래건). Each bullet should be a single sentence.\n"
        "요약은 반드시 한국어로만 작성하라. 한자·중국어·일본어 문자(CJK 한자 범위)는 "
        "절대 사용하지 말 것 — 한자어는 모두 한글로 표기하라 (예: 著名 → 저명)."
    )
    user = (
        f"Article title: {title}\n\n"
        f"Article body:\n{body}\n\n"
        "Respond with JSON only, no other text or markdown fences. Schema:\n"
        '{"korean_title": "한국어로 번역한 제목", '
        '"summary": ["요약 1", "요약 2", "요약 3"]}'
    )

    parsed = _summarize_once(client, system, user)
    found = _hanja_found(parsed)
    if not found:
        return parsed

    # One retry, naming the offending characters back to the model.
    log.warning("Hanja %s in summary — retrying once: %s", found, title[:60])
    retry_user = (
        f"{user}\n\n이전 응답에 한자가 포함되어 있었다: {found}\n"
        "한자를 단 하나도 쓰지 말고 순수 한글로 다시 작성하라."
    )
    try:
        retried = _summarize_once(client, system, retry_user)
    except Exception:
        # Keep the first attempt and scrub it rather than losing the summary.
        log.exception("Hanja retry failed — stripping the first attempt instead.")
    else:
        parsed = retried
        found = _hanja_found(parsed)

    if found:
        log.warning("Hanja %s survived the retry — stripping the characters.", found)
        parsed = _scrub_summary(parsed)
    return parsed


# --- Telegram delivery -----------------------------------------------------
def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _header(mode: str) -> str:
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    if mode == "breaking":
        return f"🚀🔴 <b>SpaceX 속보</b> ({now_kst})"
    return f"🚀 <b>SpaceX 브리핑</b> (최근 7일 · {now_kst})"


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
    parser = argparse.ArgumentParser(description="SpaceX news bot (breaking/daily).")
    parser.add_argument(
        "--mode",
        choices=["breaking", "daily"],
        required=True,
        help="breaking: 속보 게이트 / daily: 상위 5개 브리핑",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="수집·채점·선정까지만 실행하고 텔레그램 발송과 상태 저장은 생략",
    )
    args = parser.parse_args()
    mode = args.mode
    dry_run = args.dry_run
    log.info(
        "=== SpaceX pro bot starting (mode=%s%s) ===",
        mode,
        ", DRY RUN" if dry_run else "",
    )

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY is required for scoring and summarization.")
        return 1
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 1) Collect, basic dedupe, then trim to the scoring budget.
    candidates = dedupe_basic(collect_all())
    if not candidates:
        log.info("No candidates collected — nothing to do.")
        return 0
    candidates = limit_by_source(candidates)

    # 2) Editor scoring, then cluster dedupe.
    if score_candidates(client, candidates):
        candidates = drop_unscored(candidates)
    else:
        # Editor pass failed entirely — keep the neutral-scored candidates
        # rather than going silent, and say so.
        log.warning("Proceeding with unscored candidates (editor pass failed).")
    if dry_run:
        log.info("--- Editor scores (all %d candidates) ---", len(candidates))
        for c in sorted(
            candidates, key=lambda c: (not c["is_routine"], _score(c)), reverse=True
        ):
            log.info(
                "  score=%3d rel=%2d imp=%2d routine=%-5s official=%-5s [%s] %s | %s",
                _score(c),
                c["relevance"],
                c["importance"],
                c["is_routine"],
                c["is_official"],
                c["cluster"][:24],
                _display_date(c["published"]),
                c["title"][:80],
            )
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
            "  %d. [score=%d rel=%d imp=%d routine=%s official=%s pub=%s] %s",
            i,
            _score(c),
            c["relevance"],
            c["importance"],
            c["is_routine"],
            c["is_official"],
            _display_date(c["published"]),
            c["title"][:80],
        )

    if not selected:
        if mode == "daily":
            # Daily always posts once a day — send an explicit "no news" line
            # so the channel gets its briefing either way.
            notice = f"{_header('daily')}\n\n지난 7일간 새 주요 소식 없음."
            if dry_run:
                log.info("[dry-run] would send:\n%s", notice)
            else:
                log.info("No in-window articles — sending daily 'no news' notice.")
                send_telegram([notice])
            log.info("Done.")
            return 0
        # For breaking, silence is the correct outcome when nothing qualifies.
        log.info("Nothing qualifies for mode=%s — exiting without sending.", mode)
        return 0

    # 5) Resolve Google News redirects, then summarize — selected articles only.
    resolve_links(selected)

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
    if dry_run:
        log.info("[dry-run] %d message chunk(s), not sent:", len(chunks))
        for i, chunk in enumerate(chunks, 1):
            log.info("--- chunk %d (%d chars) ---\n%s", i, len(chunk), chunk)
        log.info("[dry-run] state file left untouched: %s", STATE_FILE)
        log.info("Done.")
        return 0

    log.info("Sending %d Telegram message(s) to %s", len(chunks), TELEGRAM_CHANNEL)
    send_telegram(chunks)

    # 7) Persist posted state.
    update_state(state, selected)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
