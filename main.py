import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from dotenv import load_dotenv

# Force UTF-8 on stdout/stderr so Korean log lines render correctly when the
# process is piped to a file or running under a non-UTF-8 Windows codepage
# (default cp949 mangles Hangul and turns characters like em-dash into
# \uXXXX backslash escapes).
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="backslashreplace")

load_dotenv()

NEWS_API_KEY = os.environ["NEWS_API_KEY"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@stayhungry_asi")

KST = timezone(timedelta(hours=9))
QUERY = "SpaceX OR Starship OR Starlink OR Falcon"
TOP_N = 5
MODEL = "claude-sonnet-4-6"
TELEGRAM_CHUNK_LIMIT = 3800

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("spacex-news-bot")


def _newsapi_request(params: dict) -> dict:
    resp = requests.get(
        "https://newsapi.org/v2/everything", params=params, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"NewsAPI error: {data}")
    return data


PRIMARY_KEYWORD = "spacex"
SECONDARY_KEYWORDS = ("starship", "starlink", "falcon 9", "falcon heavy", "crew dragon")

# GDPR / cookie-consent boilerplate that NewsAPI sometimes returns in place of
# a real description (Yahoo, MSN, etc.). Match case-insensitively.
GDPR_MARKERS = (
    "accept all",
    "iab transparency",
    "consent framework",
    "store and / or access information on a device",
)
# NewsAPI free-tier truncation suffix, e.g. "[+1234 chars]". Cosmetic in
# content; treated as pollution if it appears in description (description
# should be a clean summary, never truncated).
TRUNCATION_RE = re.compile(r"\[\+\d+\s*chars\]")


def _has_gdpr_markers(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(m in lowered for m in GDPR_MARKERS)


def _clean_body(article: dict) -> str:
    """Return a usable body for the article, or '' if nothing clean exists.

    Description is preferred; if it's empty or polluted (GDPR boilerplate or
    a truncation marker), fall back to content with the truncation suffix
    stripped. Content is dropped only if it carries GDPR boilerplate.
    """
    description = (article.get("description") or "").strip()
    if (
        description
        and not _has_gdpr_markers(description)
        and not TRUNCATION_RE.search(description)
    ):
        return description
    content = (article.get("content") or "").strip()
    content = TRUNCATION_RE.sub("", content).strip()
    if content and not _has_gdpr_markers(content):
        return content
    return ""


def _haystack(article: dict) -> str:
    parts = [
        article.get("title") or "",
        article.get("description") or "",
        article.get("content") or "",
    ]
    return "\n".join(parts).lower()


def _match_tier(article: dict) -> str | None:
    text = _haystack(article)
    if PRIMARY_KEYWORD in text:
        return "primary"
    if any(kw in text for kw in SECONDARY_KEYWORDS):
        return "secondary"
    return None


def filter_spacex(articles: list[dict]) -> list[tuple[str, dict]]:
    """Return all SpaceX-related candidates, primary tier first.

    - primary: title or body contains "SpaceX" (case-insensitive)
    - secondary: contains a SpaceX product term (Starship / Starlink /
      Falcon 9 / Falcon Heavy / Crew Dragon)
    Articles mentioning only "Elon Musk" / "Tesla" / bare "Falcon" are dropped.
    Returns the full ranked candidate pool, not capped — caller picks TOP_N
    after a separate body-pollution check so polluted articles can be
    replaced.
    """
    primary: list[dict] = []
    secondary: list[dict] = []
    seen_titles: set[str] = set()
    for article in articles:
        title = (article.get("title") or "").strip()
        if not title or title == "[Removed]" or title in seen_titles:
            continue
        seen_titles.add(title)
        tier = _match_tier(article)
        if tier == "primary":
            primary.append(article)
        elif tier == "secondary":
            secondary.append(article)

    return [("primary", a) for a in primary] + [("secondary", a) for a in secondary]


def fetch_news() -> list[dict]:
    now = datetime.now(timezone.utc)
    base = {
        "q": QUERY,
        "language": "en",
        "pageSize": 100,
        "apiKey": NEWS_API_KEY,
    }

    # Primary attempt: last 24h, sorted by relevancy (per spec).
    since_24h = now - timedelta(hours=24)
    primary = {
        **base,
        "from": since_24h.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "sortBy": "relevancy",
    }
    data = _newsapi_request(primary)
    log.info("Primary window (24h) totalResults=%d", data.get("totalResults", 0))

    # Fallback: NewsAPI free tier can have a multi-week index lag.
    # If 24h returns nothing, widen to 30 days and sort by publishedAt to get
    # the freshest articles the account can actually see.
    if not data.get("articles"):
        log.info("24h window empty — falling back to 30-day window sorted by publishedAt.")
        since_30d = now - timedelta(days=30)
        fallback = {
            **base,
            "from": since_30d.strftime("%Y-%m-%dT%H:%M:%S"),
            "sortBy": "publishedAt",
        }
        data = _newsapi_request(fallback)
        log.info("Fallback window (30d) totalResults=%d", data.get("totalResults", 0))

    return data.get("articles", [])


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = [l for l in text.splitlines() if not l.startswith("```")]
        text = "\n".join(lines).strip()
    return text


def summarize(client: anthropic.Anthropic, title: str, body: str) -> dict:
    system = (
        "You are a Korean news summarization assistant for SpaceX-related articles. "
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
    raw = _strip_code_fence(raw)
    return json.loads(raw)


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_messages(items: list[dict]) -> list[str]:
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    header = f"🚀 <b>SpaceX 뉴스 브리핑</b> ({now_kst})"

    blocks: list[str] = []
    for i, item in enumerate(items, 1):
        title = _html_escape(item["title"])
        source = _html_escape(item["source"])
        url = _html_escape(item["url"])
        lines = [f"<b>{i}. {title}</b>"]
        for s in item["summary"]:
            lines.append(f"- {_html_escape(s)}")
        lines.append(f'📰 출처: {source} | 🔗 <a href="{url}">원문 링크</a>')
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
        # Surface the Telegram error description before raising — Telegram puts
        # the actionable message in the JSON body, not the HTTP status.
        if not resp.ok:
            try:
                body = resp.json()
                desc = body.get("description") or body
            except ValueError:
                desc = resp.text
            raise RuntimeError(
                f"Telegram {resp.status_code}: {desc}"
            )
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")


def main() -> int:
    log.info("Fetching news from NewsAPI...")
    raw_articles = fetch_news()
    log.info("Fetched %d raw articles", len(raw_articles))

    candidates = filter_spacex(raw_articles)
    log.info("=== SpaceX filter passed %d candidate(s) ===", len(candidates))

    if not candidates:
        log.info("No SpaceX articles after filtering — sending 'no news' message.")
        now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
        empty_msg = (
            f"🚀 <b>SpaceX 뉴스 브리핑</b> ({now_kst})\n\n"
            "오늘 새 SpaceX 뉴스가 없습니다."
        )
        send_telegram([empty_msg])
        log.info("Done.")
        return 0

    # Pollution triage: split candidates into clean (usable body) and polluted
    # (no usable body). Polluted ones are kept as title-only fallbacks if we
    # can't fill TOP_N from the clean pool.
    clean: list[tuple[str, dict, str]] = []   # (tier, article, body)
    polluted: list[tuple[str, dict]] = []
    for tier, article in candidates:
        body = _clean_body(article)
        if body:
            clean.append((tier, article, body))
        else:
            polluted.append((tier, article))
    log.info(
        "오염 필터로 제외된 기사 %d건 (clean=%d, polluted=%d)",
        len(polluted),
        len(clean),
        len(polluted),
    )

    # Fill TOP_N: clean candidates first, then polluted as title-only.
    selected: list[tuple[str, dict, str]] = list(clean[:TOP_N])
    if len(selected) < TOP_N:
        for tier, article in polluted[: TOP_N - len(selected)]:
            selected.append((tier, article, ""))  # empty body → title-only

    log.info("=== Final selection: %d article(s) ===", len(selected))
    for i, (tier, article, body) in enumerate(selected, 1):
        flag = "title-only" if not body else "body"
        log.info(
            "  %d. [%s][%s] %s",
            i,
            tier,
            flag,
            (article.get("title") or "")[:80],
        )

    use_claude = bool(ANTHROPIC_API_KEY)
    if use_claude:
        log.info("ANTHROPIC_API_KEY set — using Claude Korean summarization.")
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    else:
        log.info("ANTHROPIC_API_KEY not set — passing through English title + description.")
        client = None

    items: list[dict] = []
    for _tier, article, body in selected:
        eng_title = article.get("title") or ""
        title_preview = eng_title[:60]
        source = (article.get("source") or {}).get("name") or "Unknown"
        url = article.get("url") or ""

        if not body:
            # Title-only mode — no body to summarize, send headline + link only.
            items.append(
                {"title": eng_title, "summary": [], "source": source, "url": url}
            )
            continue

        if use_claude:
            log.info("Summarizing: %s", title_preview)
            try:
                parsed = summarize(client, eng_title, body)
                title = parsed.get("korean_title") or eng_title
                summary = parsed.get("summary") or []
                if not summary:
                    log.warning("Empty summary for: %s", title_preview)
                    continue
            except Exception:
                log.exception("Summarization failed for: %s", title_preview)
                continue
            summary = summary[:3]
        else:
            title = eng_title
            summary = [body]

        items.append(
            {"title": title, "summary": summary, "source": source, "url": url}
        )

    if not items:
        log.error("No items to send after processing.")
        return 1

    chunks = build_messages(items)
    log.info("Sending %d Telegram message(s) to %s", len(chunks), TELEGRAM_CHANNEL)
    send_telegram(chunks)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
