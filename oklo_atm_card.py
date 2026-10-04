"""
오클로 2차 ATM '남은 물량' 추정 + 카드 이미지 생성 (oklo_atm_monitor.py 에서 호출)

추정 방식 (카드 하단에도 표기)
  · 속도: 1차 ATM 실측 속도 = $1B / 약 120일(2026-05-13 ~ 9월 초) ≈ 하루 $8.3M
          → 공시(10-Q 등)로 2차 판매액이 확인되면 그 실측 속도로 교체
  · 범위: 중앙 속도의 0.6배(실적 전 매도 중단 가능성) ~ 1.5배(자금 수요로 가속)
  · 주식 수 환산: 판매 구간의 평균 종가로 나눔, 남은 주식은 현재가로 나눔
  · 공시로 확정된 판매분은 그대로 쓰고, 그 이후 기간만 속도로 추정
"""
from datetime import date, datetime, timedelta

ATM_SIZE = 1_000_000_000
ATM2_START = date(2026, 9, 11)
ATM1_PACE = 1_000_000_000 / 120          # 1차 ATM 실측(약 4개월)
LOW_MULT, HIGH_MULT = 0.6, 1.5


def _d(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def avg_close(closes, start, end):
    vals = [c for d, c in closes if start <= d <= end]
    return sum(vals) / len(vals) if vals else None


def estimate(state, today, closes, price):
    """반환: dict (모든 금액 USD, 주식 수 float). price 없으면 None."""
    if not price:
        return None
    if state.get("completed"):
        return {"basis": "한도소진 공시 확인", "sold_usd": (ATM_SIZE,) * 3, "sold_sh": None,
                "remain_usd": (0, 0, 0), "remain_sh": (0, 0, 0), "exhaust": ("소진 완료",) * 2,
                "as_of": today, "pace": 0, "disclosed": True}

    anchor_usd = state.get("atm2_gross_usd") or 0
    anchor_sh = state.get("atm2_sold_shares") or 0
    if anchor_usd and state.get("atm2_as_of"):
        anchor_date = _d(state["atm2_as_of"])
        days_obs = max((anchor_date - ATM2_START).days, 1)
        pace = anchor_usd / days_obs
        basis = f"공시 확정분({state['atm2_as_of']}) + 이후 실측 속도 추정"
    else:
        anchor_date, pace = ATM2_START, ATM1_PACE
        basis = "1차 ATM 속도 적용 추정 (2차 판매량 공시 전)"

    days = max((today - anchor_date).days, 0)
    avg = avg_close(closes, anchor_date, today) or price
    out_usd, out_sh = [], []
    for mult in (LOW_MULT, 1.0, HIGH_MULT):
        extra = min(pace * mult * days, ATM_SIZE - anchor_usd)
        out_usd.append(anchor_usd + extra)
        out_sh.append(anchor_sh + extra / avg)
    remain_usd = [ATM_SIZE - u for u in out_usd]              # low 판매 → high 잔여
    remain_sh = [r / price for r in remain_usd]
    # 소진 시점: 빠른 속도(high) ~ 느린 속도(low)
    fast = today + timedelta(days=remain_usd[2] / (pace * HIGH_MULT)) if pace else None
    slow = today + timedelta(days=remain_usd[0] / (pace * LOW_MULT)) if pace else None
    return {"basis": basis, "sold_usd": tuple(out_usd), "sold_sh": tuple(out_sh),
            "remain_usd": tuple(remain_usd), "remain_sh": tuple(remain_sh),
            "exhaust": (fast, slow), "as_of": today, "pace": pace, "price": price,
            "disclosed": bool(anchor_usd)}


def _man(x):
    return f"{x/1e4:,.0f}만"


def _ym(d):
    return f"{d.year}년 {d.month}월" if isinstance(d, date) else str(d)


def rows(e):
    lo, c, hi = e["sold_usd"]
    if e["sold_sh"] is None:
        return [("소화된 물량", "한도 $1B 전액 소진"), ("남은 한도", "$0"),
                ("남은 주식", "0주"), ("소진 시점", "소진 완료")]
    slo, sc, shi = e["sold_sh"]
    r_hi, r_c, r_lo = e["remain_usd"]          # remain_usd[0]은 판매 low 기준(잔여 최대)
    rs_hi, rs_c, rs_lo = e["remain_sh"]
    fast, slow = e["exhaust"]
    period = _ym(fast) if _ym(fast) == _ym(slow) else f"{_ym(fast)} ~ {_ym(slow)}"
    return [
        ("소화된 물량", f"약 {_man(slo)}~{_man(shi)} 주 (한도의 {lo/ATM_SIZE*100:.0f}~{hi/ATM_SIZE*100:.0f}%)"),
        ("남은 한도", f"약 ${r_lo/1e8:.1f}억 ~ ${r_hi/1e8:.1f}억"),
        (f"현재가 ${e['price']:,.2f} 기준 남은 주식", f"약 {_man(rs_lo)}~{_man(rs_hi)} 주"),
        ("같은 속도라면 소진 시점", period),
    ]


def render_card(e, path, week_label):
    import logging
    import warnings
    from pathlib import Path

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import FancyBboxPatch

    # 한글 글꼴은 저장소의 파일을 직접 지정한다 (러너 시스템 글꼴·패키지에 의존하지 않음)
    font_dir = Path(__file__).parent / "fonts"
    regular, bold = font_dir / "NanumGothic.ttf", font_dir / "NanumGothicBold.ttf"
    for f in (regular, bold):
        if not f.exists():
            raise RuntimeError(f"한글 글꼴 파일 없음: {f}")
        font_manager.fontManager.addfont(str(f))
    family = font_manager.FontProperties(fname=str(regular)).get_name()
    plt.rcParams["font.family"] = family
    plt.rcParams["text.parse_math"] = False   # "$" 를 수식 기호로 해석하지 않음
    plt.rcParams["axes.unicode_minus"] = False

    # 글자가 빠지면(□) 조용히 보내지 말고 실패로 올린다
    missing = []

    class _GlyphTrap(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if "missing from" in msg or "does not have a glyph" in msg:
                missing.append(msg)

    trap = _GlyphTrap()
    logging.getLogger("matplotlib").addHandler(trap)
    warnings.simplefilter("always")
    _warn_ctx = warnings.catch_warnings(record=True)
    _caught = _warn_ctx.__enter__()

    BG, CARD, LINE = "#141416", "#1C1C20", "#2C2C33"
    INK, INK2, MUTED = "#F2F2F5", "#C9C9D1", "#8A8A96"
    ACC, ACC_SOFT = "#4FA3FF", "#2A4E7A"

    fig = plt.figure(figsize=(8.0, 5.0), dpi=200)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 100); ax.set_ylim(0, 62.5); ax.axis("off")

    ax.text(4, 58.6, "그래서 남은 물량은", color=INK, fontsize=17, fontweight="bold", va="center")
    ax.text(4, 54.6, f"오클로(OKLO) 2차 ATM · 한도 $1B · {week_label}", color=MUTED, fontsize=9.5, va="center")

    ax.add_patch(FancyBboxPatch((3, 17.5), 94, 33.5, boxstyle="round,pad=0,rounding_size=1.6",
                                fc=CARD, ec=LINE, lw=1))
    ax.text(50, 47.6, "추정치" if not e.get("disclosed") else "공시 + 추정", color=INK2, fontsize=10.5, va="center")
    ax.plot([3, 97], [44.6, 44.6], color=LINE, lw=1)
    y = 41.2
    for i, (k, v) in enumerate(rows(e)):
        ax.text(5.5, y, k, color=INK2, fontsize=10.5, va="center")
        ax.text(50, y, v, color=INK, fontsize=10.5, va="center", fontweight="bold" if i == 2 else "normal")
        if i < 3:
            ax.plot([3, 97], [y - 3.4, y - 3.4], color=LINE, lw=1)
        y -= 6.8

    # 진행 막대: 0 ~ $1B 위에 소화 추정 범위(진한 = 중앙값까지, 옅은 = 상단 범위)
    x0, x1, yb = 5, 95, 11.5
    lo, c, hi = e["sold_usd"]
    ax.add_patch(FancyBboxPatch((x0, yb - 1.1), x1 - x0, 2.2, boxstyle="round,pad=0,rounding_size=1.1", fc=LINE, ec="none"))
    sx = lambda u: x0 + (x1 - x0) * min(u, ATM_SIZE) / ATM_SIZE  # noqa: E731
    ax.add_patch(FancyBboxPatch((x0, yb - 1.1), max(sx(hi) - x0, 0.01), 2.2, boxstyle="round,pad=0,rounding_size=1.1", fc=ACC_SOFT, ec="none"))
    ax.add_patch(FancyBboxPatch((x0, yb - 1.1), max(sx(c) - x0, 0.01), 2.2, boxstyle="round,pad=0,rounding_size=1.1", fc=ACC, ec="none"))
    ax.text(x0, yb + 3.0, f"소화 추정 {lo/1e8:.1f}~{hi/1e8:.1f}억 달러 (중앙 {c/1e8:.1f}억)", color=INK2, fontsize=9, va="center")
    ax.text(x1, yb + 3.0, "$10억", color=MUTED, fontsize=9, va="center", ha="right")

    ax.text(4, 4.6, f"근거: {e['basis']} · 범위 = 중앙 속도의 0.6~1.5배 · 확정치 아님 · 내부 투자검토용",
            color=MUTED, fontsize=7.6, va="center")
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    _warn_ctx.__exit__(None, None, None)
    logging.getLogger("matplotlib").removeHandler(trap)
    missing += [str(w.message) for w in _caught if "glyph" in str(w.message).lower()]
    if missing:
        raise RuntimeError(f"카드 글자 깨짐 감지 — 발송 중단: {missing[0][:120]}")
    return path
