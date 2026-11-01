"""Tests for the three ported report-integrity tools.

Two properties matter more than coverage here:

1. **The gates must not cry wolf.** A gate that fires on a correct report gets
   ignored, and then it protects nothing. So the false-positive cases —
   negated claims, label numbers, technical terms — are asserted explicitly,
   not left to coverage.
2. **The TTM rebuild must refuse rather than guess.** A wrong TTM is worse than
   no TTM, so the non-contiguous case asserts an error, not a number.
"""

from __future__ import annotations

import pytest

from src.tools.report_evidence_gates_tool import (
    LABEL_NUMBERS,
    _affirmed,
    _close,
    _nums,
    gate_catalyst_coverage,
    gate_earnings_quality,
    gate_provenance,
    gate_range_claim,
    gate_session_freshness,
    gate_timeframe,
    gate_valuation_dual,
    gate_volume_awareness,
    run_all,
)
from src.tools.report_claim_audit_tool import (
    audit_claims,
    _is_assertion,
)
from src.tools.edgar_ttm_eps_tool import ttm_eps_from_filings


# --------------------------------------------------------------- 基础工具


def test_affirmed_distinguishes_assertion_from_denial():
    assert _affirmed("股价处于历史高位", r"(历史高位|处于高位)")
    # The report denying a high is correct, not a violation.
    assert not _affirmed("当前不构成历史高位", r"(历史高位|处于高位)")
    assert not _affirmed("并非处于高位", r"(历史高位|处于高位)")


def test_label_numbers_are_not_evidence():
    assert 52 in LABEL_NUMBERS
    assert 20 in LABEL_NUMBERS
    assert 343.5 not in LABEL_NUMBERS


def test_close_respects_tolerance():
    assert _close(100.0, {100.0})
    assert _close(100.4, {100.0})     # within 0.5%
    assert not _close(130.0, {100.0})


def test_extracts_negated_and_comma_numbers():
    assert -1.5 in _nums("回撤 -1.5%")
    assert 1234.5 in _nums("市值 1,234.5")


# ------------------------------------------------------------- provenance 门


def test_provenance_blocks_without_evidence():
    r = gate_provenance("收盘价 343.50", {})
    assert not r.passed
    assert r.severity == "BLOCK"


def test_provenance_flags_unsourced_number():
    ev = {"get_price_snapshot": {"data": {"close": 343.50}}}
    r = gate_provenance("收盘 343.50,情绪评分 6.5/10", ev)
    assert not r.passed
    assert 6.5 in r.evidence["unsupported"]


def test_provenance_passes_label_numbers():
    ev = {"get_price_snapshot": {"data": {"close": 343.50}}}
    r = gate_provenance("52 周高 408.61,近 14 天,MA20", ev)
    # 408.61 is unsourced here, but the label numbers alone must not trip it.
    assert 52 not in r.evidence.get("unsupported", [])
    assert 14 not in r.evidence.get("unsupported", [])


# ------------------------------------------------------------ 其他门的正反例


def test_session_freshness_blocks_stale_bar_presented_as_today():
    ev = {"get_price_snapshot": {"data": {"market_status": "WEEKEND_STALE",
                                          "bar_date": "2026-10-02"}}}
    blocked = gate_session_freshness("今日收盘 343.50", ev)
    assert not blocked.passed
    # Correctly disclosing the closure is not a violation.
    ok = gate_session_freshness("今日休市,最近交易日收盘 343.50", ev)
    assert ok.passed


def test_session_freshness_allows_dated_prior_session():
    """Naming a prior session is the compliant way to cite a stale bar.

    Regression guard: this phrasing was blocked before, which would have
    taught a reader to ignore the gate entirely.
    """
    ev = {"get_price_snapshot": {"data": {"market_status": "WEEKEND_STALE",
                                          "bar_date": "2026-10-02"}}}
    for phrasing in ("最近交易日收盘 343.50",
                     "上一交易日收盘 343.50",
                     "最近收盘价为 343.50"):
        assert gate_session_freshness(phrasing, ev).passed, phrasing
    # An undated same-day claim is still blocked.
    assert not gate_session_freshness("今日收盘 343.50", ev).passed


def test_valuation_dual_requires_both_horizons():
    ev = {"get_valuation": {"data": {"trailing_pe": 16.98, "forward_pe": 22.79}}}
    assert not gate_valuation_dual("PE(TTM) 16.98 倍", ev).passed
    ok = gate_valuation_dual("trailing PE 16.98,forward PE 22.79", ev)
    assert ok.passed


def test_timeframe_blocks_cross_horizon():
    ev = {"get_risk_reward": {"data": {"warning": "上行 1 个月 / 下行 12 个月,跨框架",
                                        "verdict": "跨时间框架,拒绝计算"}}}
    r = gate_timeframe("赔率 12.8", ev)
    assert not r.passed
    assert r.severity == "BLOCK"


def test_catalyst_coverage_blocks_unverified_no_catalyst():
    ev = {"get_news": {"n_rows": 15},
          "get_catalyst_calendar": {"data": {"n_upcoming": 1}}}
    r = gate_catalyst_coverage("当前没有强催化", ev)
    assert not r.passed
    # Missing the news call is itself a block, not a pass.
    r2 = gate_catalyst_coverage("当前没有强催化", {})
    assert not r2.passed
    assert r2.severity == "BLOCK"


def test_earnings_quality_requires_disclosure():
    ev = {"get_earnings_quality": {"data": {"warnings": [{"q": "2026Q2"}]}}}
    assert not gate_earnings_quality("PE 17.0 倍,估值合理", ev).passed
    ok = gate_earnings_quality("TTM 含一次性收益,trailing PE 失真", ev)
    assert ok.passed


def test_range_claim_uses_measured_position():
    ev = {"get_range_position": {"data": {"pct_off_52w_high": -15.93,
                                           "range_position_pct": 62.3}}}
    assert not gate_range_claim("股价处于历史高位", ev).passed
    # Denying the high is fine.
    assert gate_range_claim("当前不构成历史高位", ev).passed


def test_volume_awareness_catches_thin_rally():
    ev = {"get_volume_profile": {"data": {"vol_ratio_vs_ma20": 0.867}}}
    r = gate_volume_awareness("买盘承接积极,抛压极轻", ev)
    assert not r.passed
    assert "缩量" in r.message


# ---------------------------------------------------------------- run_all


def test_run_all_survives_a_broken_gate(monkeypatch):
    import src.tools.report_evidence_gates_tool as mod

    def boom(*a, **k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(mod, "GATES", [boom, mod.gate_data_coverage])
    out = mod.run_all("测试 343.50", {"get_price_snapshot": {"data": {"close": 343.50}}})
    # A crashing gate must not take the run down.
    assert out["n_warnings"] == 1


def test_run_all_reports_blockers():
    out = run_all("PE 30.2 倍,PEG 1.3-1.5", {"get_price_snapshot": {"data": {"close": 343.5}}})
    assert not out["passed"]
    assert out["n_blockers"] >= 1
    assert "不通过" in out["verdict"]


# -------------------------------------------------------- 凭空发明检测


def test_detects_invented_sentiment_score():
    out = audit_claims("情绪强度评分 6.5 / 10(中性偏乐观)。")
    assert out["n_invented"] == 1
    assert out["invented"][0]["label"] == "情绪评分"
    assert out["invented"][0]["value"] == "6.5"


def test_detects_invented_peg_and_confidence():
    out = audit_claims("PEG 1.3-1.5 属于合理区间。置信度:0.68,风险评分:0.62")
    labels = {f["label"] for f in out["invented"]}
    assert "PEG 估算" in labels
    assert "自报置信度" in labels
    assert "自报风险分" in labels


def test_question_and_term_lines_are_not_assertions():
    assert not _is_assertion("是否放量?")
    assert not _is_assertion("MACD 高位死叉")
    assert _is_assertion("买盘承接积极")


def test_overstated_needs_evidence_to_annotate():
    out = audit_claims("买盘承接积极,抛压极轻。", {"get_volume_profile": {"data": {}}})
    assert out["n_overstated"] >= 1


def test_clean_report_has_no_invented_findings():
    out = audit_claims("收盘价 343.50,trailing PE 16.98,forward PE 22.79。")
    assert out["n_invented"] == 0


# ------------------------------------------------------------ TTM 重建


def test_ttm_refuses_unknown_ticker():
    out = ttm_eps_from_filings("ZZZZNOTAREALTICKER")
    assert out["ok"] is False
    assert out.get("error")


@pytest.mark.parametrize("symbol", ["GOOGL", "AAPL"])
def test_ttm_rebuild_is_contiguous_or_errors(symbol):
    """Real filings: either a contiguous TTM, or an explicit refusal.

    Never a number built across a gap — a plausible TTM assembled from
    non-adjacent quarters is the failure this tool exists to prevent.
    """
    out = ttm_eps_from_filings(symbol)
    if out.get("ok"):
        assert len(out["quarters"]) == 4
        assert all(80 <= g <= 100 for g in out["gaps_days"])
        assert out["ttm_eps"] > 0
    else:
        assert out.get("error")
        assert "ttm_eps" not in out
