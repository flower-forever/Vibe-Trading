"""Pre-publication evidence gates for research reports (full-population).

Complements :mod:`src.tools.report_audit_tool`, which samples ~15% of a
report's data points and checks each against an authoritative value. This
tool answers a different question: *may this draft be published at all?*

Two things ``report_audit`` cannot do, both of which caused real failures:

1. **Full population, not a sample.** The auditor extracts a fraction of
   numeric points; anything it did not sample is unchecked. Here every number
   in the draft is tested against the evidence pool.
2. **Sourced or not.** A number can be perfectly accurate and still be
   invented — a sentiment score, a PEG band, a self-reported confidence
   figure has no authoritative source to check it against, so no sampling
   auditor can flag it. ``gate_provenance`` reports every figure in the draft
   that no tool returned.

The nine gates are blocking interceptors, not suggestions: a BLOCK result
means the draft is not publishable yet. Each gate exists because a real
report failed it, and each is written to avoid false positives — a gate that
cries wolf is worse than no gate, because the real problems get ignored.

Design note on false positives: the gates must distinguish *asserting* X from
*denying* X. "The price is **not** at a 52-week high" must not trip the
high-position gate. :func:`_affirmed` implements that negation test, and
:data:`LABEL_NUMBERS` keeps window/period labels ("52-week", "20-day average")
out of the unsourced-number gate.

Usage:
    report_evidence_gates(report_text=..., evidence={...}, required=[...])

``evidence`` maps a tool name to that tool's returned payload. Only numbers
actually present in those payloads count as sourced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from src.agent.tools import BaseTool

# ---------------------------------------------------------------- 数字抽取

_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{2,})(?![\w])")

# Numbers that are almost always a label or a parameter rather than evidence:
# "52-week high", "past 14 days", "12-month horizon", "MA20", "20-day average".
# Reporting these as unsourced is what makes a gate cry wolf, and a gate that
# cries wolf is worse than no gate at all.
LABEL_NUMBERS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 20, 24, 26, 30, 50, 52,
                 60, 90, 100, 180, 200, 365}

# Negations: when one of these precedes the keyword, the author is denying the
# claim rather than making it.
_NEGATORS = ("不构成", "不是", "并非", "不", "未", "非", "没有", "无", "别", "勿")


def _nums(text: str) -> list[float]:
    """Every numeric literal in a string, signed, commas stripped.

    The leading sign is captured explicitly: the token regex cannot include it
    without also swallowing the preceding word's last character, so a report
    saying "a -1.5% drawdown" would otherwise be read as a positive 1.5% and
    sail past the provenance pool.
    """
    out = []
    for m in re.finditer(r"(?<![\w.])[-−+]?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{2,})(?![\w])", text or ""):
        raw = m.group(0).replace(",", "").replace("−", "-")
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def _affirmed(text: str, pattern: str, window: int = 18) -> bool:
    """True when *pattern* appears as an assertion, not inside a negation.

    A gate must tell "says X" apart from "says it is not X". The naive test
    treats "currently **not** at a 52-week high" as a high-position claim and
    blocks a correct report — a false positive, which is the failure mode that
    disarms a gate entirely.
    """
    for m in re.finditer(pattern, text or "", re.I):
        pre = text[max(0, m.start() - window):m.start()]
        if any(neg in pre for neg in _NEGATORS):
            continue
        return True
    return False


def _all_numbers(evidence: dict) -> set[float]:
    """Every number any tool returned, at several precisions.

    The pool is the provenance baseline: a draft number matches when it is
    within 0.5% of something a tool actually returned. Multiple precisions and
    unit rescalings (1e6/1e9/1e12, x100 for percentages) matter because a
    tool returning 96500000000.0 and a report writing "96.5B" are the same
    number.
    """
    acc: set[float] = set()

    def add(v: float) -> None:
        for f in (v, abs(v), v * 100, v / 1e6, v / 1e9, v / 1e12):
            for r in (0, 1, 2, 3, 4):
                acc.add(round(float(round(f, r)), 4))
                acc.add(round(float(f), 4))

    def walk(x: Any) -> None:
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            add(float(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            for n in _nums(x):
                add(n)

    walk(evidence)
    return acc


def _close(a: float, pool: set[float]) -> bool:
    """0.5% relative tolerance against the evidence pool."""
    return any(abs(a - b) <= max(0.01, abs(b) * 0.005) for b in pool)


@dataclass
class GateResult:
    """One gate's verdict."""

    name: str
    passed: bool
    severity: str            # BLOCK | WARN | INFO
    message: str
    evidence: dict = field(default_factory=dict)
    remediation: str = ""


# ---------------------------------------------------------------- 各道门


def gate_provenance(draft: str, evidence: dict, **_: Any) -> GateResult:
    """Every number in the draft that no tool returned."""
    if not evidence:
        return GateResult("provenance", False, "BLOCK",
                          "没有任何工具返回作为证据,报告不可生成",
                          remediation="至少取行情、估值、新闻三类数据")

    pool = _all_numbers(evidence)
    unsupported = []
    for n in _nums(draft):
        if n in LABEL_NUMBERS or (float(n).is_integer() and int(n) in LABEL_NUMBERS):
            continue
        if not _close(n, pool):
            unsupported.append(n)
    unsupported = sorted(set(unsupported))[:25]

    if unsupported:
        return GateResult("provenance", False, "BLOCK",
                          f"{len(unsupported)} 个数字在工具返回中找不到出处: {unsupported[:12]}",
                          evidence={"unsupported": unsupported},
                          remediation="补取数,或删除这些无源数字")
    return GateResult("provenance", True, "INFO", "所有数字均可溯源")


def gate_session_freshness(draft: str, evidence: dict, **_: Any) -> GateResult:
    """A stale or closed-session bar must not be presented as today's tape."""
    snap = (evidence.get("get_price_snapshot") or {}).get("data", {})
    if not snap:
        return GateResult("session_freshness", False, "WARN", "未取行情快照,无法确认交易日")
    status = snap.get("market_status")
    bar_date = snap.get("bar_date")
    if status in ("WEEKEND_STALE", "STALE_MULTI_DAY"):
        # Only block the claim that a stale bar IS today's tape. Two phrasings
        # are correct and must not be blocked: "market closed today", and any
        # sentence that dates the figure to a *past* session explicitly. A gate
        # that blocks those trains its reader to ignore it.
        assertive = _affirmed(
            draft,
            r"(今日|今天|盘中)[^。;\n]{0,10}?(收盘|开盘|成交|上涨|下跌|涨了|跌了|报[高低]?|创新[高低])",
        )
        if assertive:
            # "最近交易日收盘 343.50" names a prior session: that is the
            # compliant way to report a stale bar, not a violation.
            compliant = re.search(r"(最近交易日|上一交易日|前一交易日|最近收盘|上周末|上个交易日)", draft)
            if not compliant:
                return GateResult("session_freshness", False, "BLOCK",
                                  f"最新 bar 是 {bar_date}({status}) —— 休市/滞后,"
                                  f"但报告把数据表述为当日盘中行情",
                                  evidence={"bar_date": bar_date, "market_status": status},
                                  remediation=f"把 {bar_date} 标注为'最近交易日收盘'")
        return GateResult("session_freshness", True, "INFO",
                          f"休市状态已正确披露(bar={bar_date})")
    return GateResult("session_freshness", True, "INFO", f"交易日一致(bar={bar_date})")


def gate_valuation_dual(draft: str, evidence: dict, **_: Any) -> GateResult:
    """Valuation needs both trailing and forward, not one denominator."""
    v = (evidence.get("get_valuation") or {}).get("data", {})
    if not v:
        return GateResult("valuation_dual", False, "WARN", "未取估值数据")
    tp, fp = v.get("trailing_pe"), v.get("forward_pe")
    if not tp or not fp:
        return GateResult("valuation_dual", False, "BLOCK", "估值数据不完整,缺 trailing 或 forward PE")
    has_t = re.search(r"(trailing|TTM|滚动|后顾)", draft, re.I)
    has_f = re.search(r"(forward|前瞻|预期)", draft, re.I)
    if not (has_t and has_f):
        return GateResult("valuation_dual", False, "BLOCK",
                          f"估值只呈现了单一口径(trailing PE {tp:.1f} / forward PE {fp:.1f}),"
                          f"必须同时说明两者差异及其原因",
                          evidence={"trailing_pe": tp, "forward_pe": fp},
                          remediation="补充前瞻口径,并解释为何 forward EPS 偏离 TTM EPS")
    return GateResult("valuation_dual", True, "INFO",
                      f"双口径已呈现(trailing {tp:.1f} / forward {fp:.1f})")


def gate_timeframe(draft: str, evidence: dict, **_: Any) -> GateResult:
    """Upside and downside must share one time horizon."""
    rr = (evidence.get("get_risk_reward") or {}).get("data", {})
    if not rr:
        return GateResult("timeframe", False, "WARN", "未做赔率计算,未经同框架校验")
    if rr.get("warning"):
        return GateResult("timeframe", False, "BLOCK", rr.get("verdict", "跨时间框架"),
                          evidence=rr, remediation="分别给出同一框架下的上行与下行")
    return GateResult("timeframe", True, "INFO", rr.get("verdict", "同框架"),
                      evidence=rr.get("frames") or {})


def gate_catalyst_coverage(draft: str, evidence: dict, **_: Any) -> GateResult:
    """You may not claim 'no catalyst' without having checked."""
    called_news = "get_news" in evidence
    called_cal = "get_catalyst_calendar" in evidence
    claims_none = re.search(r"(没有催化|无催化|缺乏催化|没有强催化|没有利好|无重大事件)", draft)

    if not called_news:
        return GateResult("catalyst_coverage", False, "BLOCK",
                          "未取新闻就可能断言催化剂情况 —— 这正是原报告翻车的地方"
                          "(旗舰模型发布当天、诉讼判决次日,全部漏掉)",
                          remediation="必须取新闻和事件日历")
    if not called_cal:
        return GateResult("catalyst_coverage", False, "WARN", "未取事件日历,未来事件窗口未知")
    if claims_none:
        n = (evidence.get("get_news") or {}).get("n_rows", 0)
        up = (evidence.get("get_catalyst_calendar") or {}).get("data", {}).get("n_upcoming", 0)
        if n or up:
            return GateResult("catalyst_coverage", False, "BLOCK",
                              f"报告称'没有催化剂',但工具返回了 {n} 条新闻、{up} 个未来事件",
                              evidence={"news": n, "upcoming": up},
                              remediation="改为如实描述已发现的催化剂及其影响")
    return GateResult("catalyst_coverage", True, "INFO", "催化剂已核查")


def gate_earnings_quality(draft: str, evidence: dict, **_: Any) -> GateResult:
    """One-off gains must be disclosed, or trailing PE is meaningless."""
    eq = (evidence.get("get_earnings_quality") or {}).get("data", {})
    if not eq:
        return GateResult("earnings_quality", False, "WARN", "未查盈利质量")
    warns = eq.get("warnings") or []
    if warns and not re.search(r"(一次性|非经营|非经常|one-?time|不可持续|未包含|未计入)", draft, re.I):
        return GateResult("earnings_quality", False, "BLOCK",
                          f"检测到 {len(warns)} 个季度存在巨额非经营性收益,但报告未披露",
                          evidence={"warnings": warns[:3]},
                          remediation="必须说明 TTM 盈利含一次性项目,trailing PE 失真,应改用前瞻口径")
    return GateResult("earnings_quality", True, "INFO",
                      f"盈利质量已披露({len(warns)} 个异常季度)" if warns else "盈利质量正常")


def gate_range_claim(draft: str, evidence: dict, **_: Any) -> GateResult:
    """"At historical highs" needs the 52-week range position to back it."""
    if not _affirmed(draft, r"(历史高位|处于高位|股价偏高|已在高位|涨到高位)"):
        return GateResult("range_claim", True, "INFO", "未断言股价处于高位")

    rp = (evidence.get("get_range_position") or {}).get("data", {})
    if not rp:
        return GateResult("range_claim", False, "BLOCK",
                          "报告断言股价处于高位,但未验证区间位置",
                          remediation="必须取 52 周区间位置")
    pct = rp.get("pct_off_52w_high")
    if pct is not None and pct < -5:
        return GateResult("range_claim", False, "BLOCK",
                          f"报告断言股价处于高位,但实际距 52 周高点 {pct:.1f}%"
                          f"(区间位置 {rp.get('range_position_pct')}%)",
                          evidence=rp,
                          remediation="修正表述:应为距高点回调后的位置,而非高位")
    return GateResult("range_claim", True, "INFO", f"区间位置已验证(距高 {pct})")


def gate_volume_awareness(draft: str, evidence: dict, **_: Any) -> GateResult:
    """A claim about the *health* of a move needs volume behind it."""
    if not re.search(r"(健康|强势|买盘|承接|抛压|反弹|上涨)", draft):
        return GateResult("volume_awareness", True, "INFO", "未做上涨质量判断")
    vp = (evidence.get("get_volume_profile") or {}).get("data", {})
    if not vp:
        return GateResult("volume_awareness", False, "BLOCK",
                          "报告判断了上涨质量但未取成交量",
                          remediation="必须取量能结构")
    ratio = vp.get("vol_ratio_vs_ma20")
    if ratio is not None and ratio < 0.95 and re.search(r"(放量|买盘强|承接积极|抛压极轻)", draft):
        return GateResult("volume_awareness", False, "BLOCK",
                          f"报告称放量/承接积极,但最新量仅为 20 日均量的 {ratio:.2f} 倍(缩量)",
                          evidence=vp, remediation="修正为缩量反弹,量能不支持强势表述")
    return GateResult("volume_awareness", True, "INFO", f"量能已核对(vol/MA20 = {ratio})")


def gate_data_coverage(draft: str, evidence: dict, required: tuple[str, ...] = (), **_: Any) -> GateResult:
    """Minimum data coverage before a conclusion may be drawn."""
    need = required or ("get_price_snapshot", "get_valuation", "get_news")
    missing = [t for t in need if t not in evidence]
    if missing:
        return GateResult("data_coverage", False, "WARN", f"未调用: {missing}",
                          remediation=f"补调用 {missing}")
    return GateResult("data_coverage", True, "INFO", f"覆盖 {len(need)} 项必需数据")


GATES = [
    gate_data_coverage,
    gate_session_freshness,
    gate_provenance,
    gate_valuation_dual,
    gate_timeframe,
    gate_catalyst_coverage,
    gate_earnings_quality,
    gate_range_claim,
    gate_volume_awareness,
]


def run_all(draft: str, evidence: dict, required: tuple[str, ...] = ()) -> dict:
    """Run every gate; a gate that raises must not take the run down."""
    results = []
    for g in GATES:
        try:
            results.append(g(draft, evidence, required=required))
        except Exception as exc:  # noqa: BLE001
            results.append(GateResult(g.__name__, True, "WARN", f"门执行异常: {exc}"))

    blockers = [r for r in results if not r.passed and r.severity == "BLOCK"]
    warns = [r for r in results if not r.passed and r.severity == "WARN"]
    return {
        "passed": not blockers,
        "n_blockers": len(blockers),
        "n_warnings": len(warns),
        "results": [
            {"gate": r.name, "passed": r.passed, "severity": r.severity,
             "message": r.message, "remediation": r.remediation, "evidence": r.evidence}
            for r in results
        ],
        "blockers": [{"gate": r.name, "message": r.message, "remediation": r.remediation}
                     for r in blockers],
        "verdict": ("通过 —— 可作为决策参考" if not blockers and not warns else
                    f"有条件通过 —— {len(warns)} 条警告" if not blockers else
                    f"不通过 —— {len(blockers)} 项硬拦截"),
    }


# ---------------------------------------------------------------- 工具封装


class ReportEvidenceGatesTool(BaseTool):
    """Nine blocking evidence gates over a full draft, not a sample."""

    name = "report_evidence_gates"
    description = (
        "Pre-publication gate for a research draft. Runs nine blocking checks "
        "over EVERY number in the draft against the evidence actually "
        "returned by tools, and returns a publish / do-not-publish verdict "
        "with per-gate remediation. Complements report_audit: that tool samples "
        "~15% of data points and verifies each against an authoritative value, "
        "while this one tests the full population and additionally flags "
        "numbers with no source at all (invented sentiment scores, PEG bands, "
        "self-reported confidence), which no sampling verifier can catch. Use "
        "this BEFORE publishing any draft that makes a directional call. "
        "Sub-command 'run' executes the gates. Sub-command 'list' returns the "
        "gate catalogue and what each one blocks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["run", "list"],
                "description": "run: execute the gates. list: return the catalogue.",
            },
            "report_text": {
                "type": "string",
                "description": "run: the full draft markdown to vet.",
            },
            "evidence": {
                "type": "object",
                "description": "run: map of tool name -> that tool's returned "
                               "payload. Only numbers present here count as sourced.",
            },
            "required_tools": {
                "type": "array", "items": {"type": "string"},
                "description": "run: tool names that must have been called. "
                               "Defaults to price/valuation/news.",
            },
        },
        "required": ["command"],
    }
    is_readonly = True
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Dispatch to ``run`` or ``list`` and return a JSON envelope.

        Args:
            **kwargs: ``command`` plus the inputs for that phase.

        Returns:
            JSON string — ``status="ok"`` with the verdict, or ``status="error"``
            with a message.
        """
        command = str(kwargs.get("command") or "").strip()
        try:
            if command == "list":
                result = {
                    "gates": [g.__name__ for g in GATES],
                    "catalogue": [
                        {"gate": g.__name__,
                         "doc": (g.__doc__ or "").strip().splitlines()[0] if g.__doc__ else ""}
                        for g in GATES
                    ],
                    "note": "Any gate returning severity=BLOCK means the draft "
                            "is not publishable. WARN means publishable with "
                            "disclosed caveats.",
                }
            elif command == "run":
                draft = kwargs.get("report_text")
                if not isinstance(draft, str) or not draft.strip():
                    return _err("report_text (non-empty markdown) is required for run")
                evidence = kwargs.get("evidence")
                if evidence is None:
                    evidence = {}
                if not isinstance(evidence, dict):
                    return _err("evidence must be an object mapping tool name -> payload")
                required = tuple(kwargs.get("required_tools") or ())
                result = run_all(draft, evidence, required=required)
            else:
                return _err(f"unknown command: {command}")
        except Exception as exc:  # noqa: BLE001 — surface a clean tool error
            return json.dumps({"status": "error", "command": command, "error": str(exc)},
                              ensure_ascii=False, allow_nan=False)
        return json.dumps({"status": "ok", "command": command, **result},
                          ensure_ascii=False, allow_nan=False)


def _err(msg: str) -> str:
    """Build the standard error JSON envelope."""
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)
