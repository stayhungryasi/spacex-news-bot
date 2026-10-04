"""오클로 ATM 봇 자가진단 — 워크플로가 본 실행 전에 매번 돌린다. 하나라도 실패하면 발송하지 않고 실패 알림.

실전 상태 파일(state/oklo_atm_state.json)은 절대 건드리지 않는다: 모든 테스트는 임시 폴더의 상태 파일만 쓴다.
"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.pop("OKLO_BOT_TOKEN", None)          # 테스트 중 실제 발송 차단
os.environ.pop("TELEGRAM_CHAT_ID", None)

import oklo_atm_card as card  # noqa: E402
import oklo_atm_monitor as mon  # noqa: E402

REAL_STATE = mon.STATE_PATH
TENQ = ("<p>Under the Equity Distribution Agreement dated as of May 13, 2026, we sold 17,970,000 shares for gross "
        "proceeds of $1.0 billion and the program was completed.</p><p>On September 11, 2026, we entered into an "
        "Equity Distribution Agreement for up to $1.0 billion. Through September 30, 2026, we sold an aggregate of "
        "9,812,345 shares under the Equity Distribution Agreement for aggregate gross proceeds of $372.4 million.</p>")
DONE_8K = ("<p>On December 3, 2026, Oklo completed its at-the-market offering program under the Equity Distribution "
           "Agreement dated as of September 11, 2026, having sold an aggregate of 27,400,000 shares for aggregate "
           "gross proceeds of approximately $1.0 billion.</p>")
DONE_8K_NO_DATE = "<p>Oklo announced it has completed its at-the-market equity offering program and no further sales will be made.</p>"
ATM3 = ("<p>On January 9, 2027, we entered into an Equity Distribution Agreement, dated as of January 9, 2027, for up "
        "to $1.5 billion.</p>")
CLOSES = [(date(2026, 9, 11) + timedelta(days=i), 42 - 0.3 * i) for i in range(22)]


class Parsing(unittest.TestCase):
    def run_parse(self, html):
        t = mon.html_to_text(html)
        paras = mon.atm2_paragraphs(t)
        return mon.parse_sales(paras), paras, t

    def test_10q_separates_first_atm(self):
        (s, g), _, _ = self.run_parse(TENQ)
        self.assertEqual(s, 9_812_345)          # 1차 ATM 1,797만 주가 섞이면 실패
        self.assertAlmostEqual(g, 372.4e6)

    def test_completion_8k(self):
        (s, g), paras, _ = self.run_parse(DONE_8K)
        self.assertEqual(s, 27_400_000)
        self.assertTrue(mon.detect_complete(" ".join(paras)))

    def test_completion_8k_without_date(self):
        t = mon.html_to_text(DONE_8K_NO_DATE)
        self.assertTrue(mon.detect_complete(t))

    def test_atm3(self):
        self.assertEqual(mon.detect_new_atm(mon.html_to_text(ATM3)), "January 9, 2027")
        self.assertIsNone(mon.detect_new_atm(mon.html_to_text(TENQ)))


class Estimate(unittest.TestCase):
    def test_pre_disclosure_ranges_ordered(self):
        e = card.estimate({"completed": False}, date(2026, 10, 5), CLOSES, 35.87)
        lo, c, hi = e["sold_usd"]
        self.assertTrue(0 < lo < c < hi <= card.ATM_SIZE)
        self.assertTrue(all(r >= 0 for r in e["remain_usd"]))

    def test_never_exceeds_limit(self):
        e = card.estimate({"completed": False}, date(2028, 1, 1), CLOSES, 35.0)
        self.assertLessEqual(max(e["sold_usd"]), card.ATM_SIZE)

    def test_card_renders_korean_without_missing_glyphs(self):
        with tempfile.TemporaryDirectory() as d:
            e = card.estimate({"completed": False}, date(2026, 10, 5), CLOSES, 35.87)
            out = Path(d) / "c.png"
            card.render_card(e, out, "테스트 기준")
            self.assertGreater(out.stat().st_size, 10_000)

    def test_missing_glyph_aborts(self):
        orig = card.rows
        try:
            card.rows = lambda e: [("깨짐 🛰", "x")] + orig(e)[1:]
            with tempfile.TemporaryDirectory() as d, self.assertRaises(RuntimeError):
                card.render_card(card.estimate({"completed": False}, date(2026, 10, 5), CLOSES, 35.87), Path(d) / "c.png", "x")
        finally:
            card.rows = orig


class EndToEnd(unittest.TestCase):
    """main() 을 가짜 SEC 응답으로 돌려 '언제 무엇을 보내는가'를 검사."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mon.STATE_PATH = Path(self.tmp.name) / "state.json"
        os.environ["OKLO_CARD_PATH"] = str(Path(self.tmp.name) / "card.png")
        self.sent, self.photos = [], []
        self._orig = (mon.send_telegram, mon.send_telegram_photo, mon.recent_filings, mon.sec_get,
                      mon.latest_shares_outstanding, mon.price_history)
        mon.send_telegram = lambda t: self.sent.append(t)
        mon.send_telegram_photo = lambda p, c: self.photos.append(p)
        mon.latest_shares_outstanding = lambda: (186_017_650, "2026-08-04", "10-Q")
        mon.price_history = lambda: (CLOSES, 35.87)
        self.filings, self.docs = [], {}
        mon.recent_filings = lambda: list(self.filings)
        mon.sec_get = lambda url, as_json=False: self.docs[url.rsplit("/", 1)[-1]]

    def tearDown(self):
        (mon.send_telegram, mon.send_telegram_photo, mon.recent_filings, mon.sec_get,
         mon.latest_shares_outstanding, mon.price_history) = self._orig
        mon.STATE_PATH = REAL_STATE
        self.tmp.cleanup()

    def add(self, form, day, acc, doc, html, report=""):
        self.filings.append({"form": form, "date": day, "acc": acc, "doc": doc, "report_date": report})
        self.docs[doc] = html

    def test_weekly_once_then_silent(self):
        mon.main()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("주간 점검", self.sent[0])
        self.assertEqual(len(self.photos), 1)
        mon.main()                                   # 같은 주 두 번째 실행 → 사건 없으면 침묵
        self.assertEqual(len(self.sent), 1)

    def test_event_between_weeks_sent_immediately(self):
        mon.main()
        self.add("10-Q", "2026-11-10", "a1", "q3.htm", TENQ, "2026-09-30")
        mon.main()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("수시 점검", self.sent[1])
        self.assertIn("981만", self.sent[1])
        st = json.loads(mon.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(st["atm2_sold_shares"], 9_812_345)
        self.assertFalse(st["completed"])          # 1차 ATM '완료' 문구를 2차 완료로 오판하면 실패

    def test_completion_alert_exactly_once(self):
        mon.main()
        self.add("8-K", "2026-12-03", "a2", "done.htm", DONE_8K)
        mon.main()
        mon.main()
        alerts = [t for t in self.sent if "한도소진(완료) 공시 감지" in t]
        self.assertEqual(len(alerts), 1)

    def test_10q_parse_failure_warns(self):
        mon.main()
        self.add("10-Q", "2026-11-10", "a3", "weird.htm", "<p>Nothing about the program here.</p>", "2026-09-30")
        mon.main()
        self.assertTrue(any("자동 추출 실패" in t for t in self.sent))

    def test_real_state_untouched(self):
        before = REAL_STATE.read_bytes() if REAL_STATE.exists() else None
        mon.main()
        after = REAL_STATE.read_bytes() if REAL_STATE.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
