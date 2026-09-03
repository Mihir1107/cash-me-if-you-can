"""
The close desk as paper.

The two download buttons used to hand back the raw artifacts -- a JSON report
and a JSONL trail. Both are exactly right for a machine and useless in the one
place these documents actually go: attached to a period close, mailed to a
controller, printed and initialled. So they render as PDFs here.

This module formats. It computes nothing. Every figure on the page is read out
of the report dict the pipeline produced, and the only arithmetic performed is
turning a value into the width of a bar. Percentages, residuals, thresholds and
counts are all taken as given -- if a number is wrong on the page it is wrong in
the report, which is the property that makes the document worth signing.
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, PageTemplate,
                               Paragraph, Spacer, Table, TableStyle)

# Paper, not screen. The dashboard is dark because it lives on a monitor; a
# close pack gets printed, and dark backgrounds print as a solid black page.
PAPER = colors.HexColor("#ffffff")
INK = colors.HexColor("#11161d")
INK_2 = colors.HexColor("#3d4753")
INK_3 = colors.HexColor("#78838f")
RULE = colors.HexColor("#dfe4ea")
WASH = colors.HexColor("#f5f7f9")
BLOCK = colors.HexColor("#a8232c")      # close blocked / at risk
CLEAR = colors.HexColor("#1b7a4b")      # close clear / confirmed
WARN = colors.HexColor("#a8641b")

SERIF = "Times-Roman"
SERIF_B = "Times-Bold"
SANS = "Helvetica"
SANS_B = "Helvetica-Bold"
MONO = "Courier"

PAGE_W, PAGE_H = A4
MARGIN = 17 * mm
CONTENT_W = PAGE_W - 2 * MARGIN


def _style(name, **kw):
    kw.setdefault("fontName", SANS)
    kw.setdefault("fontSize", 9)
    kw.setdefault("leading", kw["fontSize"] + 3.5)
    kw.setdefault("textColor", INK_2)
    return ParagraphStyle(name, **kw)


S = {
    "title": _style("title", fontName=SERIF_B, fontSize=26, leading=29, textColor=INK),
    "subtitle": _style("subtitle", fontSize=9.5, textColor=INK_3),
    "eyebrow": _style("eyebrow", fontName=SANS_B, fontSize=7.5, leading=11,
                      textColor=INK_3),
    "h2": _style("h2", fontName=SERIF_B, fontSize=14, leading=17, textColor=INK,
                 spaceBefore=2, spaceAfter=1),
    "lede": _style("lede", fontSize=8.5, textColor=INK_3, spaceAfter=4),
    "body": _style("body"),
    "small": _style("small", fontSize=7.8, leading=10.5, textColor=INK_3),
    "cell": _style("cell", fontSize=8.2, leading=10.8),
    "cellb": _style("cellb", fontName=SANS_B, fontSize=8.2, leading=10.8,
                    textColor=INK),
    "cellr": _style("cellr", fontSize=8.2, leading=10.8, alignment=TA_RIGHT),
    "th": _style("th", fontName=SANS_B, fontSize=7.2, leading=9.5, textColor=INK_3),
    "thr": _style("thr", fontName=SANS_B, fontSize=7.2, leading=9.5, textColor=INK_3,
                  alignment=TA_RIGHT),
    "mono": _style("mono", fontName=MONO, fontSize=7, leading=9.4, textColor=INK_2),
    "stamp": _style("stamp", fontName=SERIF_B, fontSize=21, leading=24),
}


# ---------------------------------------------------------------- primitives

class Rule(Flowable):
    """A hairline. Thinner than anything platypus draws for a table."""

    def __init__(self, width, thickness=0.5, color=RULE, space=0):
        Flowable.__init__(self)
        self.width, self.thickness, self.color, self.space = width, thickness, color, space
        self.height = thickness + space

    def draw(self):
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, self.space, self.width, self.space)


class Bar(Flowable):
    """A share-of-total bar, drawn to a fraction someone else computed."""

    def __init__(self, fraction, width, color=BLOCK, height=5):
        Flowable.__init__(self)
        self.fraction = max(0.0, min(1.0, fraction))
        self.width, self.height, self.color = width, height, color

    def draw(self):
        c = self.canv
        c.setFillColor(WASH)
        c.rect(0, 0, self.width, self.height, stroke=0, fill=1)
        if self.fraction <= 0:
            return                      # nothing to show is shown as nothing
        c.setFillColor(self.color)
        c.rect(0, 0, max(1.0, self.width * self.fraction), self.height,
               stroke=0, fill=1)


def _money(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _p(text, style="cell"):
    return Paragraph(str(text), S[style])


def _code(text):
    """A reason code, in the words of the report, made readable."""
    return str(text).replace("_", " ")


def _table(rows, widths, header=True, align_right=(), zebra=True, pad=5):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    cmds = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
    ]
    if header:
        cmds += [("LINEBELOW", (0, 0), (-1, 0), 0.8, INK_3),
                 ("BOTTOMPADDING", (0, 0), (-1, 0), 4)]
    if zebra:
        for i in range(1 if header else 0, len(rows)):
            if (i - (1 if header else 0)) % 2 == 1:
                cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fafbfc")))
        cmds += [("LEFTPADDING", (0, 0), (0, -1), 4),
                 ("RIGHTPADDING", (-1, 0), (-1, -1), 4)]
    for col in align_right:
        cmds.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    t.setStyle(TableStyle(cmds))
    return t


def _section(title, lede=None):
    out = [Spacer(1, 13), Paragraph(title, S["h2"]),
           Rule(CONTENT_W, 0.8, INK, space=3), Spacer(1, 5)]
    if lede:
        out.append(Paragraph(lede, S["lede"]))
    return out


# ------------------------------------------------------------------ chrome

class _Doc(BaseDocTemplate):
    """Every page carries the run it came from, so a loose sheet is placeable."""

    def __init__(self, path, kicker, run_line, **kw):
        BaseDocTemplate.__init__(self, path, pagesize=A4,
                                 leftMargin=MARGIN, rightMargin=MARGIN,
                                 topMargin=MARGIN + 10, bottomMargin=MARGIN + 8, **kw)
        self.kicker, self.run_line = kicker, run_line
        frame = Frame(MARGIN, self.bottomMargin, CONTENT_W,
                      PAGE_H - self.topMargin - self.bottomMargin, id="body",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="page", frames=[frame],
                                            onPage=self._furniture)])

    def _furniture(self, canv, doc):
        canv.saveState()
        y = PAGE_H - MARGIN + 2
        canv.setFont(SANS_B, 7)
        canv.setFillColor(INK_3)
        canv.drawString(MARGIN, y, self.kicker.upper())
        canv.setFont(MONO, 7)
        canv.drawRightString(PAGE_W - MARGIN, y, self.run_line)
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        canv.line(MARGIN, y - 5, PAGE_W - MARGIN, y - 5)

        foot = MARGIN - 4
        canv.line(MARGIN, foot + 11, PAGE_W - MARGIN, foot + 11)
        canv.setFont(SANS, 6.8)
        canv.drawString(MARGIN, foot,
                        "Every figure here was produced by the reconciliation run "
                        "named above. None was typed.")
        canv.drawRightString(PAGE_W - MARGIN, foot, f"Page {doc.page}")
        canv.restoreState()


def _cover(title, subtitle, meta_pairs):
    rows = [[Paragraph(k, S["eyebrow"]), Paragraph(v, S["small"])]
            for k, v in meta_pairs]
    meta = Table(rows, colWidths=[38 * mm, CONTENT_W - 38 * mm], hAlign="LEFT")
    meta.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [Paragraph(title, S["title"]),
            Spacer(1, 3),
            Paragraph(subtitle, S["subtitle"]),
            Spacer(1, 9), Rule(CONTENT_W, 1.2, INK, space=0), Spacer(1, 9),
            meta]


def _elapsed(ms):
    """Sub-second runs read as `0.0s`, which looks like a figure that failed."""
    if not ms:
        return "—"
    return f"{ms:,.0f} ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _now():
    return datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %H:%M %Z")


# ------------------------------------------------------------ report blocks

def _verdict_panel(report):
    gate = report.get("close_gate") or {}
    can = bool(gate.get("can_close"))
    tone = CLEAR if can else BLOCK
    stamp = "CLEARED TO CLOSE" if can else "CLOSE BLOCKED"

    passed = gate.get("conditions_passed") or []
    blockers = gate.get("blockers") or []
    checked = gate.get("conditions_checked") or (len(passed) + len(blockers))

    if can:
        line = (f"All {checked} close conditions hold. Nothing found in this run "
                f"stands between the books and a signed period close.")
    else:
        line = (f"{len(blockers)} of {checked} close conditions failed, holding "
                f"{_money(gate.get('value_blocking_close'))} against the close.")

    left = [Paragraph("PERIOD CLOSE DECISION", S["eyebrow"]), Spacer(1, 4),
            Paragraph(stamp, ParagraphStyle("st", parent=S["stamp"], textColor=tone)),
            Spacer(1, 5), Paragraph(line, S["body"])]
    if gate.get("note"):
        left += [Spacer(1, 4), Paragraph(gate["note"], S["small"])]

    cond_rows = []
    for name in passed:
        cond_rows.append([Paragraph("✓", ParagraphStyle("ok", parent=S["cell"],
                                                        fontName=SANS_B, textColor=CLEAR)),
                          _p(_code(name), "cell")])
    for b in blockers:
        cond_rows.append([Paragraph("✕", ParagraphStyle("no", parent=S["cell"],
                                                        fontName=SANS_B, textColor=BLOCK)),
                          _p(f"<b>{_code(b.get('condition'))}</b>", "cell")])
    conds = Table(cond_rows or [["", _p("no conditions reported")]],
                  colWidths=[10, 52 * mm - 10], hAlign="LEFT")
    conds.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 1.2),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    right = [Paragraph(f"{checked} CONDITIONS", S["eyebrow"]), Spacer(1, 4), conds]

    panel = Table([[left, right]],
                  colWidths=[CONTENT_W - 56 * mm, 56 * mm], hAlign="LEFT")
    panel.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), WASH),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, tone),
        ("LINEBEFORE", (1, 0), (1, -1), 0.5, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 11),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ("LEFTPADDING", (0, 0), (0, -1), 13),
        ("LEFTPADDING", (1, 0), (1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
    ]))
    return panel


def _kpis(report):
    money = report.get("money") or {}
    cells = [
        ("MATCH RATE", f"{report.get('match_rate_pct', 0)}%",
         f"{report.get('reconciled_orders', 0)} of {report.get('total_orders', 0)} orders, both legs"),
        ("VALUE CONFIRMED", _money(money.get("confirmed_value")),
         f"{money.get('value_match_rate_pct', 0)}% of exposure in the bank"),
        ("AT RISK", _money(money.get("at_risk_value")),
         f"{report.get('exception_count', 0)} exception records"),
        ("TOTAL EXPOSURE", _money(money.get("total_exposure")),
         (report.get("money") or {}).get("exposure_note", "") or "gross value in scope"),
    ]
    row_top, row_mid, row_bot = [], [], []
    for eyebrow, value, note in cells:
        tone = BLOCK if eyebrow == "AT RISK" else INK
        row_top.append(Paragraph(eyebrow, S["eyebrow"]))
        row_mid.append(Paragraph(value, ParagraphStyle(
            "kpi", fontName=SERIF_B, fontSize=17, leading=19, textColor=tone)))
        row_bot.append(Paragraph(note, S["small"]))
    w = CONTENT_W / 4.0
    t = Table([row_top, row_mid, row_bot], colWidths=[w] * 4, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
        ("TOPPADDING", (0, 2), (-1, 2), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LINEABOVE", (0, 0), (-1, 0), 0.8, INK),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
    ]))
    return t


def _blockers_block(report):
    gate = report.get("close_gate") or {}
    blockers = gate.get("blockers") or []
    if not blockers:
        return []
    rows = [[_p("CONDITION", "th"), _p("WHY IT BLOCKS", "th"),
             _p("WHAT CLEARS IT", "th"), _p("VALUE AT RISK", "thr")]]
    for b in blockers:
        rows.append([
            Paragraph(_code(b.get("condition")), ParagraphStyle(
                "bc", parent=S["cellb"], textColor=BLOCK)),
            _p(b.get("why", "")),
            _p(b.get("action", "")),
            _p(_money(b.get("value_at_risk")), "cellr"),
        ])
    widths = [30 * mm, CONTENT_W - 30 * mm - 47 * mm - 24 * mm, 47 * mm, 24 * mm]
    return _section("What is holding the close",
                    "Each row is a condition that failed, why it failed, and the "
                    "action that clears it.") + [_table(rows, widths, align_right=(3,))]


def _money_block(report):
    money = report.get("money") or {}
    if not money:
        return []
    by_reason = {k: v for k, v in (money.get("at_risk_by_reason") or {}).items() if v}
    ordered = sorted(by_reason.items(), key=lambda kv: -kv[1])
    top = ordered[0][1] if ordered else 0
    at_risk = money.get("at_risk_value") or 0

    rows = [[_p("REASON CODE", "th"), _p("SHARE", "th"), _p("VALUE AT RISK", "thr"),
             _p("% OF AT RISK", "thr")]]
    bar_w = CONTENT_W - 62 * mm - 30 * mm - 26 * mm
    for reason, value in ordered:
        share = (value / at_risk * 100) if at_risk else 0
        rows.append([
            _p(_code(reason), "cellb"),
            Bar(value / top if top else 0, bar_w),
            _p(_money(value), "cellr"),
            _p(f"{share:.1f}%", "cellr"),
        ])
    if money.get("unattributed_bank_credit_value"):
        rows.append([
            _p("cash held, not placeable", "cell"),
            Bar(0, bar_w, WARN),
            _p(_money(money["unattributed_bank_credit_value"]), "cellr"),
            _p("—", "cellr"),
        ])

    identity = money.get("identity") or {}
    holds = identity.get("holds")
    tone = CLEAR if holds else BLOCK
    stmt = identity.get("statement", "")
    banner = Table([[Paragraph(
        f"<b>{'IDENTITY HOLDS' if holds else 'IDENTITY BROKEN'}</b> &nbsp; {stmt} "
        f"&nbsp;·&nbsp; residual {identity.get('residual', 0):.2f}",
        ParagraphStyle("id", parent=S["cell"], textColor=INK_2))]],
        colWidths=[CONTENT_W], hAlign="LEFT")
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WASH),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, tone),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    return _section("Where the money is at risk",
                    "Exposure is the gross value in scope. It splits, exactly, "
                    "into what the bank confirmed and what is still at risk.") + [
        _table(rows, [62 * mm, bar_w, 30 * mm, 26 * mm], align_right=(2, 3)),
        Spacer(1, 8), banner]


def _triage_block(report):
    triage = report.get("triage") or {}
    incidents = triage.get("incidents") or []
    if not incidents:
        return []
    rows = [[_p("PRIORITY", "th"), _p("INCIDENT", "th"), _p("OWNER", "th"),
             _p("ORDERS", "thr"), _p("VALUE AT RISK", "thr")]]
    for i, inc in enumerate(incidents, 1):
        urgency = (inc.get("urgency") or "").lower()
        tone = BLOCK if urgency == "critical" else (WARN if urgency == "high" else INK_3)
        head = (f"<b>{_code(inc.get('reason_code'))}</b>"
                + ("" if inc.get("material") else " · below threshold"))
        body = inc.get("recommended_action", "")
        ids = ", ".join((inc.get("order_ids") or [])[:4])
        extra = len(inc.get("order_ids") or []) - 4
        if ids:
            body += f"<br/><font color='#78838f' size='7'>{ids}"
            body += f" +{extra} more</font>" if extra > 0 else "</font>"
        rows.append([
            Paragraph(f"{i}. {urgency or '—'}", ParagraphStyle(
                "ur", parent=S["cellb"], textColor=tone)),
            _p(f"{head}<br/>{body}"),
            _p(_code(inc.get("owner")), "cell"),
            _p(inc.get("order_count", ""), "cellr"),
            _p(_money(inc.get("value_at_risk")), "cellr"),
        ])
    widths = [22 * mm, CONTENT_W - 22 * mm - 30 * mm - 16 * mm - 26 * mm,
              30 * mm, 16 * mm, 26 * mm]

    owner_rows = [[_p("OWNER", "th"), _p("INCIDENTS", "thr"), _p("ORDERS", "thr"),
                   _p("VALUE AT RISK", "thr")]]
    for owner, d in sorted((triage.get("by_owner") or {}).items(),
                           key=lambda kv: -kv[1].get("value_at_risk", 0)):
        owner_rows.append([_p(_code(owner), "cellb"),
                           _p(d.get("incidents", ""), "cellr"),
                           _p(d.get("orders", ""), "cellr"),
                           _p(_money(d.get("value_at_risk")), "cellr")])

    lede = (f"{triage.get('incident_count', 0)} incidents from "
            f"{triage.get('exception_rows', 0)} exception rows, "
            f"{triage.get('material_incident_count', 0)} above the materiality "
            f"threshold of {_money(triage.get('materiality_threshold'))} "
            f"({triage.get('materiality_basis', '')}).")
    return _section("The queue: what to work today", lede) + [
        _table(rows, widths, align_right=(3, 4)),
        Spacer(1, 12),
        Paragraph("Routed to", S["eyebrow"]), Spacer(1, 4),
        _table(owner_rows, [CONTENT_W - 3 * 26 * mm, 26 * mm, 26 * mm, 26 * mm],
               align_right=(1, 2, 3))]


def _unattributed_block(report):
    credits = report.get("unattributed_bank_credits") or []
    if not credits:
        return []
    shown = credits[:22]
    rows = [[_p("TXN ID", "th"), _p("NARRATION AS THE BANK WROTE IT", "th"),
             _p("REASON", "th"), _p("AMOUNT", "thr")]]
    for c in shown:
        rows.append([_p(c.get("txn_id", ""), "cellb"),
                     _p(c.get("narration", ""), "cell"),
                     _p(_code(c.get("reason_code")), "cell"),
                     _p(_money(c.get("amount")), "cellr")])
    widths = [26 * mm, CONTENT_W - 26 * mm - 40 * mm - 26 * mm, 40 * mm, 26 * mm]
    out = _section(
        "What it would not guess",
        "Money in the bank that no tier could tie to a settlement. A credit "
        "that fits two outstanding settlements equally well cannot be attributed "
        "by any amount check, so it is reported rather than guessed at.") + [
        _table(rows, widths, align_right=(3,))]
    if len(credits) > len(shown):
        out.append(Spacer(1, 5))
        out.append(Paragraph(f"and {len(credits) - len(shown)} more, in the JSON "
                             f"report.", S["small"]))
    return out


def _method_block(report):
    tiers = report.get("narration_resolution") or {}
    stages = report.get("stages") or {}
    tp = report.get("throughput") or {}
    trail = report.get("audit_trail") or {}

    left_rows = [[_p("STAGE", "th"), _p("MATCHED", "thr"), _p("EXCEPTIONS", "thr")]]
    for key, label in (("ledger_settlement", "ledger → settlement"),
                       ("settlement_bank", "settlement → bank")):
        d = stages.get(key) or {}
        left_rows.append([_p(label, "cellb"), _p(d.get("matched", ""), "cellr"),
                          _p(d.get("exceptions", ""), "cellr")])

    # Tier 1 (the reference read straight out of the narration) is not counted
    # in the report -- it is the residue, not a tally -- so it is not invented here.
    tier_rows = [[_p("BANK NARRATION RESOLVED BY", "th"), _p("ROWS", "thr")]]
    for label, key in (("fuzzy recovery of a mangled reference (no model)",
                        "resolved_by_fuzzy_no_llm"),
                       ("a model call, proposing a reference only", "resolved_by_llm"),
                       ("nothing — reported, not guessed", "unresolved")):
        tier_rows.append([_p(label, "cell"), _p(tiers.get(key, 0), "cellr")])

    facts = [
        ("Model calls this run", str(tp.get("llm_calls", 0))),
        ("Wall clock", f"{tp.get('wall_clock_ms', 0):,.0f} ms over "
                       f"{tp.get('records_processed', 0):,} records"
                       if tp else "—"),
        ("Audit trail", (f"{trail.get('entries', 0)} entries, chain "
                         f"{'intact' if trail.get('intact') else 'BROKEN'}")
         if trail else "—"),
    ]
    fact_rows = [[_p(k, "cellb"), _p(v, "cell")] for k, v in facts]

    body = [_table(left_rows, [CONTENT_W - 2 * 26 * mm, 26 * mm, 26 * mm],
                   align_right=(1, 2)),
            Spacer(1, 10),
            _table(tier_rows, [CONTENT_W - 26 * mm, 26 * mm], align_right=(1,)),
            Spacer(1, 10),
            _table(fact_rows, [45 * mm, CONTENT_W - 45 * mm], header=False,
                   zebra=False, pad=3)]

    diagnostics = (report.get("source_diagnostics") or {}).get("checks") or []
    if diagnostics:
        drows = [[_p("DO THE THREE FILES DESCRIBE THE SAME MONEY?", "th"),
                  _p("VERDICT", "thr")]]
        for c in diagnostics:
            ok = c.get("ok")
            drows.append([_p(f"<b>{c.get('check', '')}</b><br/>"
                             f"<font size='7' color='#78838f'>{c.get('detail', '')}</font>"),
                          Paragraph("pass" if ok else "FAIL", ParagraphStyle(
                              "v", parent=S["cellr"], fontName=SANS_B,
                              textColor=CLEAR if ok else BLOCK))])
        body += [Spacer(1, 10),
                 _table(drows, [CONTENT_W - 22 * mm, 22 * mm], align_right=(1,))]

    return _section("How the number was reached",
                    "A match counts only when both legs confirm it. The ladder "
                    "below runs cheapest first, and a model is consulted only "
                    "where a bank mangled a reference — never to decide a "
                    "match, an amount or a status.") + body


def _breakdown_block(report):
    counts = report.get("exception_reason_counts") or {}
    if not counts:
        return []
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    top = ordered[0][1]
    bar_w = CONTENT_W - 62 * mm - 20 * mm
    rows = [[_p("REASON CODE", "th"), _p("SHARE OF EXCEPTION RECORDS", "th"),
             _p("RECORDS", "thr")]]
    for code, n in ordered:
        rows.append([_p(_code(code), "cellb"), Bar(n / top, bar_w, INK_3),
                     _p(n, "cellr")])
    extra = report.get("exception_count", 0) - report.get("unreconciled_orders", 0)
    note = ("An order that fails both legs carries two records, which is why "
            f"{report.get('exception_count', 0)} records cover "
            f"{report.get('unreconciled_orders', 0)} orders."
            if extra > 0 else "One record per unreconciled order.")
    return _section("Exception breakdown", note) + [
        _table(rows, [62 * mm, bar_w, 20 * mm], align_right=(2,))]


def build_report_pdf(payload, path):
    """Render the run the browser is holding as a close pack."""
    report = payload.get("report") or {}
    trail_text = payload.get("audit_trail") or ""
    run_id = _run_id_from_trail(trail_text) or "run id unavailable"
    source = report.get("source_dir", "uploaded files")
    tp = report.get("throughput") or {}

    doc = _Doc(str(path), "Reconciliation close pack", run_id,
               title="Reconciliation close pack", author="cash-me-if-you-can")

    story = _cover(
        "Reconciliation Close Desk",
        "Merchant ledger, Razorpay settlements and bank statement, checked "
        "three ways against each other.",
        [("SOURCES", source),
         ("GENERATED", _now()),
         ("RUN ID", run_id),
         ("SCOPE", f"{report.get('total_orders', 0):,} orders · "
                   f"{tp.get('bank_rows', 0):,} bank rows · reconciled in "
                   f"{_elapsed(tp.get('wall_clock_ms'))}")])
    story += [Spacer(1, 14), _verdict_panel(report), Spacer(1, 16), _kpis(report)]
    story += _blockers_block(report)
    story += _money_block(report)
    story += _triage_block(report)
    story += _breakdown_block(report)
    story += _unattributed_block(report)
    story += _method_block(report)

    scoring = report.get("scoring")
    if scoring:
        rows = [[_p(k.replace("_", " "), "cellb"), _p(v, "cellr")]
                for k, v in scoring.items()]
        story += _section(
            "Is the number honest?",
            "Scored against an answer key the pipeline never reads. It exists "
            "for the bundled samples only; a real month has no answer key.") + [
            _table(rows, [CONTENT_W - 40 * mm, 40 * mm], header=False,
                   align_right=(1,), pad=3)]

    doc.build(story)
    return path


# ------------------------------------------------------------- audit trail

def _run_id_from_trail(text):
    for line in text.splitlines():
        if line.strip():
            try:
                return json.loads(line).get("run_id")
            except json.JSONDecodeError:
                return None
    return None


def _verify(text):
    """
    Re-verify the chain being printed, rather than quoting the run's own claim.

    The document says the trail is intact; it should be the document's own
    finding about the bytes on its pages, not a summary copied from elsewhere.
    """
    from src.audit import verify_chain

    tmp = Path(tempfile.mkdtemp()) / "audit_trail.jsonl"
    tmp.write_text(text)
    try:
        return verify_chain(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)
        tmp.parent.rmdir()


def build_audit_pdf(payload, path):
    """Render the hash-chained trail: every decision, in the order it was made."""
    text = payload.get("audit_trail") or ""
    entries = []
    for line in text.splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    run_id = _run_id_from_trail(text) or "run id unavailable"
    status = _verify(text) if entries else {}
    intact = bool(status.get("intact"))

    doc = _Doc(str(path), "Audit trail", run_id, title="Audit trail",
               author="cash-me-if-you-can")

    story = _cover(
        "Audit Trail",
        "Every decision this run made, in the order it made them, each entry "
        "carrying the SHA-256 of the one before it.",
        [("RUN ID", run_id),
         ("GENERATED", _now()),
         ("ENTRIES", f"{len(entries):,}"),
         ("CHAIN", ("verified end to end — no entry was altered or removed"
                    if intact else
                    f"BROKEN{' at entry ' + str(status['broken_at']) if status.get('broken_at') else ''}"
                    f" — {status.get('reason', 'the chain did not verify')}"))])

    banner = Table([[Paragraph(
        ("<b>CHAIN INTACT.</b> Each entry's hash was recomputed from its content "
         "and the previous entry's hash. Changing any line below would break "
         "every hash after it." if intact else
         f"<b>CHAIN BROKEN.</b> {status.get('reason', '')}"),
        ParagraphStyle("ab", parent=S["cell"], textColor=INK_2))]],
        colWidths=[CONTENT_W], hAlign="LEFT")
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WASH),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, CLEAR if intact else BLOCK),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [Spacer(1, 14), banner]

    by_decision = {}
    for e in entries:
        d = e.get("decision", "—")
        by_decision[d] = by_decision.get(d, 0) + 1
    if by_decision:
        rows = [[_p("DECISION", "th"), _p("ENTRIES", "thr")]]
        for d, n in sorted(by_decision.items(), key=lambda kv: -kv[1]):
            rows.append([_p(_code(d), "cellb"), _p(n, "cellr")])
        story += _section("What is in the trail") + [
            _table(rows, [CONTENT_W - 26 * mm, 26 * mm], align_right=(1,), pad=3)]

    story += _section(
        "The entries",
        "Entry hash first, then the hash it chains back to. Both are truncated "
        "to 16 characters for the page; the full values are in the JSONL export.")

    rows = [[_p("#", "th"), _p("ORDER / STAGE", "th"), _p("DECISION", "th"),
             _p("BASIS", "th"), _p("HASH  ←  PREV", "th")]]
    for i, e in enumerate(entries, 1):
        decision = e.get("decision", "")
        tone = BLOCK if decision == "exception" else (
            CLEAR if decision == "matched" else INK_2)
        stamp = (e.get("timestamp") or "")[11:19]
        rows.append([
            _p(i, "cell"),
            _p(f"<b>{e.get('order_id') or '—'}</b><br/>"
               f"<font size='7' color='#78838f'>{_code(e.get('stage', ''))} · {stamp}</font>"),
            Paragraph(_code(decision), ParagraphStyle(
                "dc", parent=S["cellb"], textColor=tone)),
            _p(e.get("basis", "")),
            Paragraph(f"{(e.get('entry_hash') or '')[:16]}<br/>"
                      f"← {(e.get('prev_hash') or '')[:16]}", S["mono"]),
        ])
    widths = [11 * mm, 34 * mm, 20 * mm,
              CONTENT_W - 11 * mm - 34 * mm - 20 * mm - 34 * mm, 34 * mm]
    story.append(_table(rows, widths, pad=4))

    if not entries:
        story.append(Paragraph("This run produced no audit entries.", S["body"]))

    doc.build(story)
    return path
