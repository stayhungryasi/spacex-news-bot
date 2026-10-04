"""
오클로(OKLO) 2차 ATM 소화 현황 주간 모니터 → 텔레그램(Oklo_shafer_bot) 발송

무엇을 하나
  1) SEC EDGAR에서 오클로(CIK 0001849056)의 신규 공시를 확인한다.
  2) 10-Q / 10-K / 8-K / 424B 본문에서 2026-09-11 Equity Distribution Agreement($1B, 2차 ATM)
     관련 문단을 찾아 판매 주식 수·조달액·한도소진 여부를 추출한다.
  3) XBRL 표지 발행주식 수(dei:EntityCommonStockSharesOutstanding)로 기준 대비 증가분을 계산한다.
  4) 아래 조건이면 🚨 긴급 알림, 아니면 주간 현황을 보낸다.
       - 한도소진(완료·종료) 문구 감지
       - 2차 ATM 판매 주식 수(공시 기준 또는 발행주식 증가분 추정) ≥ 27,000,000주
       - 3차 ATM(새 Equity Distribution Agreement) 감지

한계 (메시지에도 표기)
  - ATM 판매량은 실시간 공시 의무가 없다. 보통 분기 보고서(10-Q·10-K)나 한도소진 8-K에서만 확인된다.
  - 발행주식 증가분에는 스톡옵션·RSU 등 ATM 외 발행도 섞인다 → '추정치(상한)'로만 쓴다.

필요한 환경변수 (GitHub Secrets)
  OKLO_BOT_TOKEN     — Oklo_shafer_bot 토큰 (BotFather)
  TELEGRAM_CHAT_ID   — Space8_Investment 채널 ID (예: -100xxxxxxxxxx 또는 @채널주소)
  SEC_USER_AGENT     — SEC 요구 형식 "이름 연락처이메일" (예: "Space8Investment bot you@example.com")
"""
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

import requests

CIK = "0001849056"
CIK_INT = "1849056"
ATM2_DATE_TEXT = "September 11, 2026"
ATM2_START = "2026-09-11"
ATM2_SIZE_USD = 1_000_000_000
ALERT_SHARES = 27_000_000

# 기준 발행주식 수: 2026-06-30 185,090,155주 + 1차 ATM 6/30 이후 발행 7,259,394주
# (출처: 2026-09-11 424B5 투자설명서 보충서)
BASELINE_SHARES = 185_090_155 + 7_259_394
BASELINE_NOTE = "6/30 185,090,155주 + 1차 ATM 잔여분 7,259,394주 (9/11 424B5)"

KST = timezone(timedelta(hours=9))
STATE_PATH = Path(__file__).parent / "state" / "oklo_atm_state.json"
FORMS_OF_INTEREST = {"10-Q", "10-K", "8-K", "424B5", "424B3", "S-3", "S-3ASR", "10-Q/A", "10-K/A", "8-K/A"}

COMPLETE_PATTERNS = [
    r"(?:sold|issued)[^.]{0,200}?all[^.]{0,80}?(?:shares|common stock)[^.]{0,120}?(?:Equity Distribution Agreement|ATM)",
    r"(?:Equity Distribution Agreement|ATM (?:program|offering))[^.]{0,200}?(?:completed|fully utilized|exhausted|no (?:further|additional) (?:shares|sales))",
    r"(?:completed|exhausted|fully utilized)[^.]{0,200}?(?:Equity Distribution Agreement|ATM (?:program|offering))",
    r"(?:terminated|termination of)[^.]{0,120}?Equity Distribution Agreement[^.]{0,80}?September 11, 2026",
]


# ───────────────────────── 공통 유틸 ─────────────────────────
def sec_get(url, as_json=False):
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError("SEC_USER_AGENT 미설정 — SEC는 '이름 이메일' 형식의 User-Agent를 요구합니다.")
    r = requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=40)
    r.raise_for_status()
    return r.json() if as_json else r.text


def html_to_text(html):
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def to_num(s):
    return float(s.replace(",", ""))


def fmt_shares(n):
    return f"{n:,.0f}주 (약 {n/1e4:,.0f}만 주)"


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen_accessions": [], "atm2_sold_shares": None, "atm2_gross_usd": None,
            "atm2_as_of": None, "atm2_source": None, "completed": False,
            "completed_alert_sent": False, "threshold_alert_sent": False,
            "atm3_alert_sent": False, "last_run": None}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_telegram(text):
    token = os.environ.get("OKLO_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[경고] OKLO_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 메시지를 출력만 합니다.\n")
        print(text)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                      timeout=30)
    if not r.ok:
        raise RuntimeError(f"텔레그램 발송 실패: {r.status_code} {r.text[:300]}")


# ───────────────────────── 데이터 수집 ─────────────────────────
def recent_filings():
    data = sec_get(f"https://data.sec.gov/submissions/CIK{CIK}.json", as_json=True)
    rec = data["filings"]["recent"]
    rows = []
    for i in range(len(rec["form"])):
        rows.append({"form": rec["form"][i], "date": rec["filingDate"][i],
                     "acc": rec["accessionNumber"][i], "doc": rec["primaryDocument"][i],
                     "report_date": rec.get("reportDate", [""] * len(rec["form"]))[i]})
    return rows


def filing_url(f):
    return f"https://www.sec.gov/Archives/edgar/data/{CIK_INT}/{f['acc'].replace('-', '')}/{f['doc']}"


def latest_shares_outstanding():
    """XBRL 표지 발행주식 수 중 가장 최근 값."""
    try:
        facts = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json", as_json=True)
        units = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
        best = max(units, key=lambda u: (u.get("end", ""), u.get("filed", "")))
        return int(best["val"]), best.get("end"), best.get("form")
    except Exception as e:  # noqa: BLE001
        print(f"[XBRL] 발행주식 수 조회 실패: {e}")
        return None, None, None


def latest_price():
    """야후 차트 API(무료·비공식). 실패해도 전체 흐름은 계속."""
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/OKLO",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"])
    except Exception as e:  # noqa: BLE001
        print(f"[가격] 조회 실패: {e}")
        return None


# ───────────────────────── 본문 분석 ─────────────────────────
def atm2_paragraphs(text):
    """2차 ATM(9/11 계약) 구간만 문장 단위로 잘라낸다.
    9/11 계약을 언급한 문장부터 시작해, 1차 ATM(5/13 계약)·과거 프로그램을 언급하는 문장이 나오면 멈춘다."""
    prior = re.compile(r"May 13, 2026|prior (?:equity distribution|ATM|sales) agreement|previous (?:equity distribution|ATM)", re.I)
    sentences = re.split(r"(?<=[.;])\s+(?=[A-Z(])", text)
    out = []
    for idx, sent in enumerate(sentences):
        if ATM2_DATE_TEXT not in sent:
            continue
        chunk = []
        for nxt in sentences[idx: idx + 8]:
            if chunk and prior.search(nxt):
                break
            if not chunk and prior.search(nxt) and not re.search(r"sold|issued", nxt.split(ATM2_DATE_TEXT, 1)[-1], re.I):
                chunk.append(nxt.split(ATM2_DATE_TEXT, 1)[-1])
                continue
            chunk.append(nxt)
        window = " ".join(chunk)
        if re.search(r"Equity Distribution Agreement|at[- ]the[- ]market|ATM", window, re.I):
            out.append(window)
    return out


def parse_sales(paragraphs):
    """판매 주식 수 / 총조달액 추출. 가장 큰 값을 누적치로 간주."""
    shares, gross = [], []
    for p in paragraphs:
        for m in re.finditer(r"(?:sold|issued)\s+(?:an aggregate of\s+|a total of\s+|approximately\s+)?([\d,]+(?:\.\d+)?)\s*(million)?\s+shares", p, re.I):
            v = to_num(m.group(1)) * (1e6 if m.group(2) else 1)
            if v >= 10_000:
                shares.append(v)
        for m in re.finditer(r"(?:gross|aggregate)\s+(?:sales\s+)?proceeds\s+(?:of\s+)?(?:approximately\s+)?\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?", p, re.I):
            v = to_num(m.group(1)) * {"million": 1e6, "billion": 1e9}.get((m.group(2) or "").lower(), 1)
            if v >= 1e6:
                gross.append(v)
    return (max(shares) if shares else None), (max(gross) if gross else None)


def detect_complete(text):
    for pat in COMPLETE_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return text[max(0, m.start() - 150): m.end() + 150]
    return None


def detect_new_atm(text):
    """9/11 이후 날짜의 새 Equity Distribution Agreement(3차 ATM)."""
    for m in re.finditer(r"Equity Distribution Agreement,?\s+dated(?: as of)?\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text):
        try:
            d = datetime.strptime(m.group(1), "%B %d, %Y").date()
        except ValueError:
            continue
        if d > datetime(2026, 9, 11).date():
            return m.group(1)
    return None


# ───────────────────────── 메인 ─────────────────────────
def main():
    state = load_state()
    now = datetime.now(KST)
    filings = [f for f in recent_filings() if f["date"] >= ATM2_START and f["form"] in FORMS_OF_INTEREST]
    new = [f for f in filings if f["acc"] not in state["seen_accessions"]]

    completion_snippet, atm3_date, new_lines = None, None, []
    for f in sorted(new, key=lambda x: x["date"]):
        new_lines.append(f"· {f['date']} {f['form']}")
        try:
            text = html_to_text(sec_get(filing_url(f)))
        except Exception as e:  # noqa: BLE001
            new_lines[-1] += f" (본문 조회 실패: {e})"
            continue
        paras = atm2_paragraphs(text)
        s, g = parse_sales(paras)
        if s and (state["atm2_sold_shares"] is None or s > state["atm2_sold_shares"]):
            snippet = next((p for p in paras if re.search(r"sold|issued", p, re.I)), "")
            state.update(atm2_sold_shares=s, atm2_as_of=f.get("report_date") or f["date"],
                         atm2_source=f"{f['form']} ({f['date']})", atm2_snippet=snippet[:300])
        if g and (state["atm2_gross_usd"] is None or g > state["atm2_gross_usd"]):
            state["atm2_gross_usd"] = g
        snip = detect_complete(" ".join(paras) if paras else text)
        if snip and paras:
            completion_snippet = snip
        a3 = detect_new_atm(text)
        if a3:
            atm3_date = a3
        state["seen_accessions"].append(f["acc"])

    if (state["atm2_gross_usd"] or 0) >= ATM2_SIZE_USD * 0.98:
        completion_snippet = completion_snippet or "공시상 누적 조달액이 $1B 한도의 98% 이상"
    if completion_snippet:
        state["completed"] = True

    so, so_date, so_form = latest_shares_outstanding()
    # 표지 발행주식 수가 2차 ATM 시작(9/11) 이전 날짜면 기준과 비교할 수 없다 (음수 착시 방지)
    comparable = bool(so and so_date and so_date >= ATM2_START)
    est_increase = (so - BASELINE_SHARES) if comparable else None
    price = latest_price()

    sold = state["atm2_sold_shares"]
    trigger_shares = max(v for v in [sold or 0, est_increase or 0])
    alerts = []
    if state["completed"] and not state["completed_alert_sent"]:
        alerts.append("🚨 2차 ATM 한도소진(완료) 공시 감지")
        state["completed_alert_sent"] = True
    if trigger_shares >= ALERT_SHARES and not state["threshold_alert_sent"]:
        alerts.append(f"🚨 2차 ATM 소화 물량 {ALERT_SHARES/1e4:,.0f}만 주 돌파 (근거: {'공시' if (sold or 0) >= ALERT_SHARES else '발행주식 증가분 추정'})")
        state["threshold_alert_sent"] = True
    if atm3_date and not state["atm3_alert_sent"]:
        alerts.append(f"🚨 3차 ATM 의심 — 새 Equity Distribution Agreement ({atm3_date})")
        state["atm3_alert_sent"] = True

    lines = []
    if alerts:
        lines += alerts + [""]
    lines.append(f"🔔 오클로(OKLO) 2차 ATM 주간 점검 — {now:%Y-%m-%d %H:%M} KST")
    lines.append("")
    lines.append("① 한도소진 공시: " + ("✅ 감지" if state["completed"] else "없음"))
    if completion_snippet:
        lines.append(f"   └ 근거 문구: “{completion_snippet[:260]}…”")
    lines.append("")
    lines.append("② 소화 물량 (2차 ATM, 한도 $1B)")
    if sold:
        pct = f" · 한도 대비 {state['atm2_gross_usd']/ATM2_SIZE_USD*100:.0f}%" if state["atm2_gross_usd"] else ""
        gross = f" / 조달 ${state['atm2_gross_usd']/1e6:,.0f}M" if state["atm2_gross_usd"] else ""
        lines.append(f"   · 공시 기준: {fmt_shares(sold)}{gross}{pct}")
        lines.append(f"     (기준일 {state['atm2_as_of']}, 출처 {state['atm2_source']})")
        if state.get("atm2_snippet"):
            lines.append(f"     근거 문구: “{state['atm2_snippet'][:240]}…”")
    else:
        lines.append("   · 공시 기준: 아직 판매량 공시 없음 (첫 확인 예정: 3분기 10-Q, 11월 10일 전후)")
    if so and comparable:
        lines.append(f"   · 발행주식 수: {so:,.0f}주 ({so_form} 표지, {so_date} 기준)")
        lines.append(f"   · 2차 ATM 소화 추정(발행주식 증가분·상한): {est_increase:+,.0f}주")
        lines.append(f"     기준 {BASELINE_SHARES:,.0f}주 = {BASELINE_NOTE}")
    elif so:
        lines.append(f"   · 발행주식 기준 추정: 비교 불가 — 최신 공시 발행주식 수({so:,.0f}주)가 {so_date} 기준으로 2차 ATM 시작(9/11) 이전 자료")
        lines.append("     → 3분기 10-Q 표지(11월)부터 계산됩니다")
    if price:
        lines.append(f"   · 현재가 ${price:,.2f} → $1B 완판 시 총 약 {ATM2_SIZE_USD/price/1e4:,.0f}만 주 필요")
        if state["atm2_gross_usd"]:
            remain = ATM2_SIZE_USD - state["atm2_gross_usd"]
            lines.append(f"     잔여 한도 ${remain/1e6:,.0f}M ≈ 약 {max(remain,0)/price/1e4:,.0f}만 주")
    lines.append(f"   · 알림 기준: {ALERT_SHARES/1e4:,.0f}만 주 도달 또는 한도소진 공시")
    lines.append("")
    lines.append("③ 이번 주 신규 공시")
    lines += new_lines if new_lines else ["   · 없음"]
    lines.append("")
    lines.append("※ ATM 판매량은 실시간 공시 의무가 없어 분기 보고서·한도소진 8-K에서만 확정됩니다. 발행주식 증가분에는 스톡옵션·RSU 발행도 섞여 있어 상한 추정치입니다.")
    lines.append("내부 투자검토용")

    send_telegram("\n".join(lines))
    state["last_run"] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        # 실패도 반드시 알린다 — 조용한 실패 방지
        try:
            send_telegram(f"⚠️ 오클로 ATM 주간 점검 실패: {e}\n(다음 주 자동 재시도, 원인 확인 필요)")
        finally:
            print(f"[오류] {e}", file=sys.stderr)
            sys.exit(1)
