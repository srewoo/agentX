"""Unit tests for the free-fundamentals fallback chain.

Network-touching paths (`fetch_nse_quote`, `fetch_screener_in`) are exercised
by patching their dependencies — the goal here is to lock the *parsing* and
*merge* logic, not to assert against a live upstream.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from app.services import fundamentals_fallbacks as fb


# ── number coercion ──────────────────────────────────────────────────────

class TestNumberCoercion:
    def test_returns_none_for_dashes_and_n_a(self):
        assert fb._f("—") is None
        assert fb._f("n/a") is None
        assert fb._f("") is None
        assert fb._f(None) is None

    def test_strips_currency_and_percent(self):
        assert fb._f("₹ 108") == 108.0
        assert fb._f("13.0 %") == 13.0

    def test_handles_indian_cr_suffix(self):
        # "1,24,066 Cr." → 124066 × 10^7 paise…I mean rupees.
        assert fb._f("1,24,066 Cr.") == 124066 * 1e7

    def test_passthrough_numbers(self):
        assert fb._f(42) == 42.0
        assert fb._f(3.14) == 3.14


# ── merge_fundamentals ───────────────────────────────────────────────────

class TestMergeFundamentals:
    def test_primary_wins_when_value_present(self):
        primary = {"valuation": {"pe": 20.0}, "profitability": {"roe": 0.15}}
        fallback = {"valuation": {"pe": 99.0}, "profitability": {"roe": 0.99}}
        merged = fb.merge_fundamentals(primary, fallback)
        assert merged["valuation"]["pe"] == 20.0
        assert merged["profitability"]["roe"] == 0.15

    def test_fallback_fills_missing_fields(self):
        primary = {"valuation": {"pe": None, "pb": None}}
        fallback = {"valuation": {"pe": 18.5, "pb": 3.2}}
        merged = fb.merge_fundamentals(primary, fallback)
        assert merged["valuation"]["pe"] == 18.5
        assert merged["valuation"]["pb"] == 3.2

    def test_multiple_fallbacks_merge_in_order(self):
        primary = {"valuation": {}}
        f1 = {"valuation": {"pe": 10.0}}
        f2 = {"valuation": {"pe": 99.0, "pb": 2.0}}
        merged = fb.merge_fundamentals(primary, f1, f2)
        # f1 won the PE slot; f2 only contributes PB.
        assert merged["valuation"]["pe"] == 10.0
        assert merged["valuation"]["pb"] == 2.0

    def test_fills_top_level_sector_when_primary_missing(self):
        primary = {"sector": None, "industry": None}
        fallback = {"sector": "Banking", "industry": "Public Sector Bank"}
        merged = fb.merge_fundamentals(primary, fallback)
        assert merged["sector"] == "Banking"
        assert merged["industry"] == "Public Sector Bank"

    def test_clears_error_when_fallback_provides_data(self):
        primary = {
            "error": "Yahoo throttled",
            "valuation": {"pe": None},
        }
        fallback = {"valuation": {"pe": 12.5}}
        merged = fb.merge_fundamentals(primary, fallback)
        assert merged["valuation"]["pe"] == 12.5
        assert "error" not in merged


# ── fetch_nse_quote (mocked NSE client) ──────────────────────────────────

class TestFetchNseQuote:
    def _stub_nse(self, payload):
        nse = MagicMock()
        nse.quote.return_value = payload
        nse.exit.return_value = None
        return nse

    def test_extracts_pe_industry_market_cap(self):
        payload = {
            "metadata": {
                "pdSymbolPe": 7.34,
                "pdSectorPe": 7.5,
                "pdSectorInd": "NIFTY BANK",
                "industry": "Public Sector Bank",
            },
            "priceInfo": {"lastPrice": 108.0},
            "securityInfo": {"issuedSize": 11_492_943_268},
        }
        with patch.object(fb, "fetch_nse_quote", wraps=fb.fetch_nse_quote):
            with patch("nse.NSE", return_value=self._stub_nse(payload)):
                out = fb.fetch_nse_quote("PNB")
        assert out is not None
        assert out["valuation"]["pe"] == 7.34
        assert out["industry"] == "Public Sector Bank"
        assert out["sector"] == "Public Sector Bank"  # industry preferred over index name
        assert out["market_cap"] == 108.0 * 11_492_943_268

    def test_collapses_generic_index_to_industry(self):
        # NIFTY 50 alone is not an industry — only `industry` is meaningful.
        payload = {
            "metadata": {
                "pdSymbolPe": 20.3,
                "pdSectorInd": "NIFTY 50",
                "industry": "Refineries & Marketing",
            },
            "priceInfo": {},
            "securityInfo": {},
        }
        with patch("nse.NSE", return_value=self._stub_nse(payload)):
            out = fb.fetch_nse_quote("RELIANCE")
        assert out["sector"] == "Refineries & Marketing"

    def test_returns_none_on_exception(self):
        bad = MagicMock()
        bad.quote.side_effect = RuntimeError("boom")
        with patch("nse.NSE", return_value=bad):
            assert fb.fetch_nse_quote("XYZ") is None


# ── fetch_screener_in (mocked HTML) ──────────────────────────────────────

_SCREENER_HTML = """
<html><body>
<ul id="top-ratios">
  <li><span class="name">Market Cap</span><span class="nowrap value">₹ 1,24,066 Cr.</span></li>
  <li><span class="name">Current Price</span><span class="nowrap value">₹ 108</span></li>
  <li><span class="name">Stock P/E</span><span class="nowrap value">6.74</span></li>
  <li><span class="name">Book Value</span><span class="nowrap value">₹ 130</span></li>
  <li><span class="name">Dividend Yield</span><span class="nowrap value">2.66 %</span></li>
  <li><span class="name">ROCE</span><span class="nowrap value">6.06 %</span></li>
  <li><span class="name">ROE</span><span class="nowrap value">13.0 %</span></li>
</ul>
<title>PNB</title>
</body></html>
"""


class TestFetchScreenerIn:
    def test_parses_full_ratio_block(self):
        mock_resp = MagicMock(status_code=200, text=_SCREENER_HTML)
        with patch("requests.get", return_value=mock_resp):
            out = fb.fetch_screener_in("PNB")
        assert out is not None
        assert out["valuation"]["pe"] == 6.74
        # P/B = current_price / book_value = 108 / 130
        assert abs(out["valuation"]["pb"] - 108 / 130) < 1e-6
        # Percentages converted to fractions internally.
        assert abs(out["profitability"]["roe"] - 0.13) < 1e-9
        assert abs(out["dividends"]["dividend_yield"] - 0.0266) < 1e-9

    def test_returns_none_when_status_not_200(self):
        mock_resp = MagicMock(status_code=503, text="<html></html>")
        with patch("requests.get", return_value=mock_resp):
            assert fb.fetch_screener_in("XYZ") is None
