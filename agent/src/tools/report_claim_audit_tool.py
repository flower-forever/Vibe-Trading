"""Invented-metric and overstated-claim detection for research reports.

The gap this fills: a number can be arithmetically plausible and still have no
source whatsoever. A sentiment score of 6.5/10, a PEG band of 1.3-1.5, a
self-reported confidence of 0.68 — no tool produced these, so no tool can be
asked to confirm them. A sampling auditor that checks "reported vs fetched"
has nothing to check these against and passes them by default.

Two finding classes, both judged against evidence the caller supplies:

- **INVENTED** — the metric has no corresponding data source at all. Matched by
  :data:`INVENTED_PATTERNS`, each carrying the reason the class is unsound, so
  the finding explains itself rather than just pointing at a number.
- **OVERSTATED** — a qualitative claim contradicted by the evidence, e.g.
  "volume confirmed the move" while volume ran 0.87x its 20-day average.
  Matched by :data:`OVERBOLD`.

False-positive control matters as much as detection here. Question sentences
("did volume expand?"), technical terms that merely contain a trigger word
("death cross", "overbought"), and hypothetical framing are excluded via
:data:`_NON_ASSERT`, because a detector that flags those stops being read.

Usage:
    report_claim_audit(report_text=..., evidence={...})
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from src.agent.tools import BaseTool

_SENT = re.compile(r"[^。\n；;]+[。；;]?")
_NUMTOK = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{2,})(?![\w])")


def _get(d: dict, *path: str, default: Any = None) -> Any:
    """Walk a nested dict, returning *default* at the first missing key."""
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


# Metrics that no connected data source produces. Each entry is
# (pattern, label, why this class of number is unsound).
INVENTED_PATTERNS: list[tuple[str, str, str]] = [
    (r"情绪(?:强度)?(?:评分|分数)[^。\n]{0,10}?(\d+\.?\d*)\s*/\s*10", "情绪评分",
     "情绪类数据源未接通,该分数是框架推演的主观赋值,不可作为任何论据"),
    (r"机构[^。\n]{0,6}?(\d+\.?\d*)\s*/\s*10", "机构情绪分",
     "机构情绪无对应数据源,该分数为主观赋值"),
    (r"散户[^。\n]{0,6}?(\d+\.?\d*)\s*/\s*10", "散户情绪分",
     "散户情绪无对应数据源,该分数为主观赋值"),
    (r"PEG[^。\n]{0,12}?(\d+\.?\d*)\s*[-–~到至]", "PEG 估算",
     "PEG 依赖盈利增速假设,报告未给出增速来源,属自行估算"),
    (r"历史均值[^。\n]{0,16}?(\d+\.?\d*)\s*倍", "历史均值 PE",
     "历史均值 PE 未标注区间、样本或来源,无法核验"),
    (r"置信度[:：]\s*(\d+\.\d+)", "自报置信度",
     "置信度由模型自行给出,不来自任何测量,无法核验"),
    (r"风险评分[:：]\s*(\d+\.\d+)", "自报风险分",
     "风险评分由模型自行给出,不来自任何测量,无法核验"),
]

# Qualitative claims the evidence can contradict outright.
OVERBOLD: list[tuple[str, str, Callable[[dict], str]]] = [
    (r"(股价处于历史高位|股价处于高位|处于历史高位|当前处于高位|已在历史高位|股价处于高位区间)",
     "range_claim",
     lambda ev: f"距 52 周高点 {_get(ev, 'get_range_position', 'data', 'pct_off_52w_high')}%,"
                f"区间位置 {_get(ev, 'get_range_position', 'data', 'range_position_pct')}%"),
    (r"(放量上涨|买盘承接积极|买盘强|承接积极|抛压极轻|资金推动)", "volume_awareness",
     lambda ev: f"最新量 / 20 日均量 = {_get(ev, 'get_volume_profile', 'data', 'vol_ratio_vs_ma20')}"
                f"(前一日 {_get(ev, 'get_volume_profile', 'data', 'prev_vol_ratio')})"),
    (r"(没有催化|无催化|缺乏催化|没有强催化|无重大事件)", "catalyst_coverage",
     lambda ev: f"近 14 天新闻 {_get(ev, 'get_news', 'n_rows', default=0)} 条;"
                f"未来 90 天事件 {_get(ev, 'get_catalyst_calendar', 'data', 'n_upcoming', default=0)} 个"),
]

# Question, hypothetical, and technical-term lines. A trigger word inside one of
# these is not an assertion — flagging it is the false positive that makes a
# detector get switched off.
_NON_ASSERT = re.compile(
    r"(是否|能否|有无|需警惕|需观察|为健康信号|更具持续性|建议|应该|如果|若|"
    r"死叉|金叉|超买|超卖|震荡|背离|指标|无法确认|无法计算|无法给出|待确认|"
    r"缺少|未提供|未返回|不虚构|不做虚构)"
)


def _is_assertion(line: str) -> bool:
    """False when the line is a question, a hypothesis, or a bare term."""
    return not _NON_ASSERT.search(line)


def _line_no(text: str, pos: int) -> int:
    """1-based line number of *pos* within *text*."""
    return text.count("\n", 0, pos) + 1


def audit_claims(text: str, evidence: dict | None = None) -> dict:
    """Scan every sentence for invented metrics and contradicted claims.

    Args:
        text: Full report markdown.
        evidence: Optional map of tool name -> payload, used to annotate
            OVERSTATED findings with the contradicting measurement.

    Returns:
        Dict with ``invented`` and ``overstated`` finding lists, each entry
        carrying the matched value, line number, and reason.
    """
    ev = evidence or {}
    invented: list[dict] = []
    overstated: list[dict] = []

    for sent in _SENT.finditer(text or ""):
        line = sent.group(0)
        if not line.strip():
            continue
        pos = sent.start()

        for pattern, label, reason in INVENTED_PATTERNS:
            m = re.search(pattern, line, re.I)
            if m:
                invented.append({
                    "kind": "INVENTED",
                    "label": label,
                    "value": m.group(1) if m.groups() else None,
                    "line": _line_no(text, pos + m.start()),
                    "raw_text": line.strip()[:160],
                    "reason": reason,
                })

        if _is_assertion(line):
            for pattern, label, fmt in OVERBOLD:
                m = re.search(pattern, line)
                if m:
                    try:
                        detail = fmt(ev)
                    except Exception as exc:  # noqa: BLE001
                        detail = f"(证据格式化失败: {exc})"
                    overstated.append({
                        "kind": "OVERSTATED",
                        "label": label,
                        "line": _line_no(text, pos + m.start()),
                        "raw_text": line.strip()[:160],
                        "evidence_says": detail,
                    })

    return {
        "n_invented": len(invented),
        "n_overstated": len(overstated),
        "invented": invented,
        "overstated": overstated,
    }


class ReportClaimAuditTool(BaseTool):
    """Flag invented metrics and claims the evidence contradicts."""

    name = "report_claim_audit"
    description = (
        "Detect numbers and claims in a research report that no data source can "
        "support. Two classes: (1) INVENTED — metrics with no corresponding "
        "source at all (sentiment scores out of 10, PEG bands, self-reported "
        "confidence or risk figures, unlabelled historical-average multiples), "
        "each reported with the reason the class is unsound; (2) OVERSTATED — "
        "qualitative claims the supplied evidence contradicts (calling a move "
        "'volume-confirmed' when volume ran below its 20-day average, or 'at "
        "historical highs' when the price sits well off the 52-week high, or "
        "'no catalyst' when news and the event calendar return any). Use "
        "alongside report_audit, which verifies reported-vs-fetched accuracy and "
        "therefore cannot see a number that has no source to verify against. "
        "Sub-command 'audit' runs the scan; 'patterns' lists the rule catalogue."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["audit", "patterns"],
                "description": "audit: run the scan. patterns: return the catalogue.",
            },
            "report_text": {
                "type": "string",
                "description": "audit: full markdown report to scan.",
            },
            "evidence": {
                "type": "object",
                "description": "audit: optional map of tool name -> payload, used "
                               "to annotate OVERSTATED findings with the contradicting "
                               "measurement.",
            },
        },
        "required": ["command"],
    }
    is_readonly = True
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Dispatch to ``audit`` or ``patterns`` and return a JSON envelope.

        Args:
            **kwargs: ``command`` plus the inputs for that phase.

        Returns:
            JSON string — ``status="ok"`` with findings, or ``status="error"``.
        """
        command = str(kwargs.get("command") or "").strip()
        try:
            if command == "patterns":
                result = {
                    "invented": [{"pattern": p, "label": lb, "reason": r}
                                 for p, lb, r in INVENTED_PATTERNS],
                    "overstated": [{"pattern": p, "label": lb} for p, lb, _ in OVERBOLD],
                    "exclusion_pattern": _NON_ASSERT.pattern,
                }
            elif command == "audit":
                text = kwargs.get("report_text")
                if not isinstance(text, str) or not text.strip():
                    return _err("report_text (non-empty markdown) is required for audit")
                evidence = kwargs.get("evidence")
                if evidence is not None and not isinstance(evidence, dict):
                    return _err("evidence must be an object mapping tool name -> payload")
                result = audit_claims(text, evidence or {})
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
