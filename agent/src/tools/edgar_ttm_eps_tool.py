"""TTM diluted EPS rebuilt from SEC EDGAR XBRL, independently of any vendor.

Why this exists: every aggregator (yfinance, Yahoo, screeners) publishes a
"TTM EPS" number that nobody can audit. For GOOGL on 2026-10-02 the vendor
figure was 20.23 while the value rebuilt from the underlying filings was
19.91 — a 1.6% gap that silently inflates or deflates every PE derived from
it. Because trailing PE is the denominator of most valuation conclusions, an
unauditable denominator is a silent defect, not a rounding issue.

Two filing facts make a naive rebuild wrong:

1. **Q4 is never filed on its own.** It appears only inside the 10-K as a
   full-year figure. Skip the derivation and you are summing three quarters
   while calling it four.
2. **"Last four 80-100 day windows" is not four consecutive quarters.** EDGAR
   also holds annual (355-375d) and nine-month (260-285d) contexts. Taking
   the four most recent short windows can silently skip a Q4, yielding a
   plausible-looking but wrong total. This tool derives Q4 = full year −
   nine months, keyed on fiscal-year *start* (the full-year and 9M contexts
   share a start and differ in end), then refuses to emit a TTM at all if
   the resulting quarters are not contiguous.

Refusing to answer is the point. A gap in the series is a fact about the
filings, and a TTM computed across a gap is fiction.

Usage:
    edgar_ttm_eps(symbol="GOOGL")
    edgar_ttm_eps(symbol="GOOGL", concept="EarningsPerShareBasic")
"""

from __future__ import annotations

import json
from datetime import date as _date
from typing import Any

from src.agent.tools import BaseTool

SEC = "sec_edgar"
# company_tickers.json is served from the www host; XBRL facts from the data
# host. SEC requires a declared contact in the User-Agent and answers 403 to
# a generic one, so the placeholder below must be replaced with a real
# address before use.
_WWW = "https://www.sec.gov"
_BASE = "https://data.sec.gov"
_UA = "trading212-research/1.0 (contact: set TSR_CONTACT_EMAIL)"


def _contact() -> str:
    """Return a SEC-compliant User-Agent, honouring TSR_CONTACT_EMAIL."""
    import os

    email = (os.environ.get("TSR_CONTACT_EMAIL") or "").strip()
    return f"trading212-research/1.0 (research contact: {email or 'unset@example.com'})"


def _ticker_map() -> dict[str, int]:
    """Ticker -> CIK, from the SEC's own company_tickers.json.

    Served from ``data.sec.gov``; the ``www.sec.gov`` host returns 403 for
    this path and needs a browser-shaped UA that SEC discourages for scripts.
    """
    import httpx

    r = httpx.get(f"{_WWW}/files/company_tickers.json",
                  headers={"User-Agent": _contact()}, timeout=30.0)
    r.raise_for_status()
    return {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}


def resolve_cik(symbol: str) -> tuple[int | None, str | None]:
    """Resolve a ticker to its SEC CIK.

    Args:
        symbol: Equity ticker, case-insensitive.

    Returns:
        ``(cik, None)`` on success, ``(None, error)`` otherwise.
    """
    try:
        return _ticker_map().get(symbol.upper()), None
    except Exception as exc:  # noqa: BLE001 — network/parse failure
        return None, f"SEC ticker 解析失败: {exc}"


def xbrl_facts(symbol: str, concept: str, taxonomy: str = "us-gaap") -> list[dict]:
    """All duration facts for one XBRL concept.

    Args:
        symbol: Equity ticker.
        concept: XBRL concept name, e.g. ``EarningsPerShareDiluted``.
        taxonomy: XBRL taxonomy, default ``us-gaap``.

    Returns:
        One dict per fact with start/end/val/form/unit, or ``[]`` on failure.
    """
    cik, err = resolve_cik(symbol)
    if cik is None:
        raise RuntimeError(err)
    import httpx

    # The XBRL endpoints key on the zero-padded 10-digit CIK. Passing the bare
    # integer from company_tickers.json yields a 404.
    url = f"{_BASE}/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{concept}.json"
    r = httpx.get(url, headers={"User-Agent": _contact()}, timeout=30.0)
    r.raise_for_status()
    out: list[dict] = []
    for unit, items in r.json().get("units", {}).items():
        for it in items:
            if "start" not in it:
                continue
            out.append({
                "start": it["start"], "end": it["end"], "val": it["val"],
                "form": it.get("form"), "unit": unit,
            })
    out.sort(key=lambda x: x["end"])
    return out


def _period_map(facts: list[dict]) -> dict[tuple[str, str, int], float]:
    """Collapse facts into ``{(start, end, days): val}``, de-duplicated."""
    acc: dict[tuple[str, str, int], float] = {}
    for f in facts:
        try:
            days = (_date.fromisoformat(f["end"]) - _date.fromisoformat(f["start"])).days
        except (ValueError, KeyError):
            continue
        acc.setdefault((f["start"], f["end"], days), f["val"])
    return acc


def ttm_eps_from_filings(symbol: str, concept: str = "EarningsPerShareDiluted") -> dict:
    """Rebuild TTM EPS from the last four *contiguous* quarters.

    Args:
        symbol: Equity ticker.
        concept: Diluted-EPS concept by default.

    Returns:
        Dict with ``ok``, ``ttm_eps``, the contributing quarters and their
        basis (filed or derived), and the quarter-end gaps. On an
        incomplete or non-contiguous series, ``ok`` is False with an
        explanation and no TTM is emitted.
    """
    try:
        facts = xbrl_facts(symbol, concept)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"XBRL 拉取失败: {exc}", "source": SEC}
    if not facts:
        return {"ok": False, "error": f"EDGAR 无 {concept} 概念", "source": SEC}

    eps = _period_map(facts)
    quarters: dict[str, float] = {}
    basis: dict[str, str] = {}
    fy_total: dict[str, float] = {}
    m9_total: dict[str, float] = {}

    for (start, end, days), val in eps.items():
        if 80 <= days <= 100:            # already a single quarter
            quarters[end] = val
            basis[end] = "10-Q 直接申报"
        elif 355 <= days <= 375:         # full year
            fy_total[start] = val        # key on the period START
        elif 260 <= days <= 285:         # first nine months
            m9_total[start] = val

    # Derive Q4. The join key must be the fiscal-year START: the full-year
    # context and the 9M context of the same fiscal year share a start and
    # differ only in end, so keying on end matches nothing.
    for start in set(fy_total) & set(m9_total):
        fy_ends = [e for (s, e, d) in eps if s == start and 355 <= d <= 375]
        if not fy_ends:
            continue
        fy_end = max(fy_ends)
        quarters[fy_end] = round(fy_total[start] - m9_total[start], 2)
        basis[fy_end] = f"Q4 = 全年 − 前三季度累计(推导,财年起于 {start})"

    if len(quarters) < 4:
        return {"ok": False,
                "error": f"仅还原出 {len(quarters)} 个季度,不足 4 个",
                "source": SEC,
                "quarters_found": [{"end": e, "eps": quarters[e]} for e in sorted(quarters)]}

    last4 = sorted(quarters)[-4:]
    gaps = [(_date.fromisoformat(b) - _date.fromisoformat(a)).days for a, b in zip(last4, last4[1:])]
    if any(g < 80 or g > 100 for g in gaps):
        return {"ok": False,
                "error": f"季度不连续(gaps={gaps}),拒绝输出可能错误的 TTM",
                "source": SEC,
                "quarters_found": [{"end": e, "eps": quarters[e]} for e in last4]}

    return {
        "ok": True,
        "symbol": symbol.upper(),
        "ttm_eps": round(sum(quarters[e] for e in last4), 2),
        "source": SEC,
        "as_of": last4[-1],
        "concept": concept,
        "quarters": [{"end": e, "eps": quarters[e], "basis": basis.get(e, "?")} for e in last4],
        "gaps_days": gaps,
        "note": "EDGAR XBRL 季度稀释 EPS 求和;Q4 由全年−9M 推导;已校验季度连续性",
    }


class EdgarTtmEpsTool(BaseTool):
    """Rebuild TTM diluted EPS from SEC XBRL filings, vendor-independent."""

    name = "edgar_ttm_eps"
    description = (
        "Rebuild trailing-twelve-month diluted EPS directly from SEC EDGAR XBRL "
        "filings, independently of any data vendor, so the PE denominator can be "
        "audited rather than trusted. Derives Q4 as full-year minus nine-month "
        "cumulative (Q4 is never filed on its own) and verifies the last four "
        "quarters are contiguous, returning an error instead of a number when "
        "they are not. Use this to establish an authoritative TTM EPS before "
        "quoting any trailing PE — vendor TTM figures have been observed to "
        "differ from the filing-derived value by enough to move the conclusion."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Equity ticker, e.g. GOOGL.",
            },
            "concept": {
                "type": "string",
                "default": "EarningsPerShareDiluted",
                "description": "XBRL EPS concept. Basic or diluted, or a "
                               "different concept entirely.",
            },
        },
        "required": ["symbol"],
    }
    is_readonly = True
    repeatable = False

    def execute(self, **kwargs: Any) -> str:
        """Rebuild TTM EPS and return a JSON envelope.

        Args:
            **kwargs: ``symbol`` and optional ``concept``.

        Returns:
            JSON string — ``status="ok"`` with the rebuild, or
            ``status="error"`` with the reason no TTM could be produced.
        """
        symbol = str(kwargs.get("symbol") or "").strip()
        if not symbol:
            return _err("symbol is required")
        concept = str(kwargs.get("concept") or "EarningsPerShareDiluted")
        try:
            result = ttm_eps_from_filings(symbol, concept)
        except Exception as exc:  # noqa: BLE001 — surface a clean tool error
            return json.dumps({"status": "error", "error": str(exc)},
                              ensure_ascii=False, allow_nan=False)
        status = "ok" if result.get("ok") else "error"
        return json.dumps({"status": status, **result},
                          ensure_ascii=False, allow_nan=False)


def _err(msg: str) -> str:
    """Build the standard error JSON envelope."""
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)
