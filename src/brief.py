"""
Plain-English incident briefs, and the guard that makes them safe to ship.

Everywhere else in this pipeline the model is kept away from decisions. This is
the one place it is used constructively, and the distinction is worth stating
precisely: it is asked to *phrase* facts, never to establish them.

A triage incident is a correct, complete, and largely unreadable object. The
person who has to act on it is often not the person who built the pipeline, and
handing a merchant's finance lead a JSON blob with a reason code in it is
technically a report and practically an obstacle. Turning already-computed facts
into two sentences someone can act on is real work, it is work language models
are genuinely good at, and no judgement is delegated in doing it: every fact in
the brief was decided by deterministic code before the model saw it.

The risk is equally precise. A model asked to write about numbers may write a
number that was not given to it, and a fabricated figure in a finance document
is worse than no document. So the same discipline used for narration proposals
applies here:

    the model drafts, and deterministic code verifies before anything is used

verify_brief() extracts every number from the generated text and checks each one
against the facts the model was given. One invented figure and the brief is
discarded in favour of the deterministic action line, which was always correct
and merely less readable. A rejected draft costs nothing; an unchecked one could
cost a controller their credibility.

Requires OPENAI_API_KEY. Without it, every incident falls back to its
deterministic action text and the report says so.
"""

import os
import re

from pydantic import BaseModel

try:
    import openai
except ImportError:
    openai = None

MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = """You write two-sentence briefs for a merchant's finance team \
about reconciliation exceptions.

You are given facts that have already been established. Your job is to phrase \
them so a non-technical person understands what happened and what to do next.

Rules you must not break:
- Use ONLY the facts given. Never introduce a number, an amount, a count, a \
date or an order id that is not in the facts.
- Quote every figure exactly as it appears. Do not round, abbreviate or \
convert.
- Write amounts as bare numbers, exactly as given. Do NOT add a currency \
symbol or a currency name. The facts carry no currency and neither should you.
- Do not speculate about causes beyond what the facts state.
- Do not claim anything has been fixed, booked or resolved.

Write plainly. No greeting, no sign-off, no bullet points."""


class IncidentBrief(BaseModel):
    brief: str


NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# A currency the facts never stated is a fabrication like any other, and a
# worse one than most: a live run had the model write "$13381.5" for an amount
# in rupees, which is wrong by roughly the exchange rate. The number guard let
# it through because the digits were right. This does not.
CURRENCY_RE = re.compile(
    r"[$€£¥₩₽]|\b(?:USD|EUR|GBP|JPY|dollars?|euros?|pounds?|cents?)\b",
    re.IGNORECASE)


def _numbers_in(text):
    """Every numeric literal in a string, normalised so 1,234.50 == 1234.5."""
    found = set()
    for raw in NUMBER_RE.findall(str(text)):
        cleaned = raw.replace(",", "")
        try:
            found.add(float(cleaned))
        except ValueError:
            continue
    return found


def allowed_numbers(facts):
    """
    Every number the model is permitted to write, gathered from the facts it was
    given. Walks nested structures, since order ids carry digits too.
    """
    allowed = set()

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)
        elif isinstance(value, bool):
            return
        elif isinstance(value, (int, float)):
            allowed.add(float(value))
            allowed.add(float(round(value, 2)))
        elif isinstance(value, str):
            # update(), not |=, which would rebind `allowed` as a local
            allowed.update(_numbers_in(value))

    walk(facts)
    return allowed


def verify_brief(text, facts):
    """
    Returns (ok, invented) where invented lists everything the model introduced
    that was not in its input: numbers first, then any currency it named.

    This is the gate. A brief that invents a figure is discarded however well
    written it is, and a brief that invents a currency is discarded for the same
    reason. The facts state amounts as bare numbers because the pipeline does
    not know or care which currency they are in; a brief that decides they are
    dollars has added a fact nobody established.
    """
    permitted = allowed_numbers(facts)
    invented = sorted(n for n in _numbers_in(text) if n not in permitted)

    fact_text = str(facts)
    currencies = sorted({
        m.group(0) for m in CURRENCY_RE.finditer(text)
        if not CURRENCY_RE.search(fact_text)
    })
    return (not invented and not currencies), invented + currencies


def incident_facts(incident):
    """Exactly what the model is shown. Nothing derived, nothing added."""
    return {
        "reason_code": incident["reason_code"],
        "orders_affected": incident["order_count"],
        "value_at_risk": incident["value_at_risk"],
        "urgency": incident["urgency"],
        "who_fixes_it": incident["owner"],
        "recommended_action": incident["recommended_action"],
        "example_order_ids": incident["order_ids"][:3],
        "one_example_finding": incident["sample_basis"],
        "above_materiality_threshold": incident["material"],
    }


def draft_brief(incident):
    """
    Returns {"text", "source", "invented"}.

    source is "llm" when the model's draft passed verification, "deterministic"
    when it did not or could not be produced. The report always says which.
    """
    facts = incident_facts(incident)
    fallback = {"text": incident["recommended_action"],
                "source": "deterministic", "invented": []}

    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai or not api_key:
        return fallback

    try:
        client = openai.OpenAI(api_key=api_key)
        response = client.responses.parse(
            model=MODEL,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": str(facts)},
            ],
            text_format=IncidentBrief,
            max_output_tokens=600,
        )
        draft = response.output_parsed
        if draft is None or not draft.brief.strip():
            return fallback

        ok, invented = verify_brief(draft.brief, facts)
        if not ok:
            # A fabricated figure in a finance document is worse than a dull
            # one. Discard the draft and say so rather than shipping it.
            return {"text": incident["recommended_action"],
                    "source": "deterministic_after_rejected_draft",
                    "invented": invented}

        return {"text": draft.brief.strip(), "source": "llm", "invented": []}
    except Exception:
        return fallback


def attach_briefs(triage, limit=3):
    """
    Draft briefs for the most consequential incidents only. The queue is already
    ranked, and a brief for something below the triage threshold is a call
    nobody needed to make.
    """
    if not triage or not triage.get("incidents"):
        return triage

    drafted = 0
    for incident in triage["incidents"]:
        if drafted >= limit or not incident["material"]:
            continue
        result = draft_brief(incident)
        incident["brief"] = result["text"]
        incident["brief_source"] = result["source"]
        if result["invented"]:
            incident["brief_rejected_numbers"] = result["invented"]
        drafted += 1

    triage["briefs_drafted"] = drafted
    return triage
