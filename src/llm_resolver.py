"""
Tier 3, and the ONLY place an LLM touches this pipeline: free-text bank
narration that neither the regex matcher nor the fuzzy tier could resolve.

The boundary, stated precisely:

  * The LLM reads one narration string and proposes what reference it seems to
    quote. That's it.
  * It is never shown the list of valid UTRs. It cannot pattern-match its way
    to a plausible-looking answer -- it has to read one out of the text, and
    then matcher.py checks whether that reference exists at all.
  * It never sees settlement amounts, never compares them, and never decides
    that money matches. Amount, batch total and date-window verification all
    happen in matcher.py, after this module has returned.
  * Its output is a link proposal, not a match. A proposal only decides which
    settlement a bank credit gets compared against.

Requires ANTHROPIC_API_KEY in the environment. If it isn't set, every row is
routed straight to the exception bucket instead of guessing -- a missing key
degrades the system, it doesn't make it lie.
"""

import json
import os

from src.fuzzy_resolver import _normalize, _numeric_core

try:
    import anthropic
except ImportError:
    anthropic = None

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You extract a candidate payment reference (UTR) or order id from a \
single bank narration string.

You do NOT decide if amounts match. You do NOT confirm anything is reconciled. You \
ONLY report what reference the narration appears to quote, reading it out of the text \
in front of you.

Never invent a reference that is not present in the narration. If the narration quotes \
no reference you can read, return nulls and confidence 0. Returning nothing is a correct \
and useful answer -- a guess is not."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "utr_candidate": {
            "type": ["string", "null"],
            "description": "The payment reference quoted in the narration, or null.",
        },
        "order_id_candidate": {
            "type": ["string", "null"],
            "description": "An order id quoted in the narration, or null.",
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1. How clearly the narration quotes this reference.",
        },
    },
    "required": ["utr_candidate", "order_id_candidate", "confidence"],
    "additionalProperties": False,
}

CONFIDENCE_THRESHOLD = 0.7


def resolve_narration(narration: str) -> dict:
    """
    Ask the model what reference this narration quotes. Returns
    {"utr_candidate", "order_id_candidate", "confidence"[, "note"]}.

    Deliberately takes only the narration -- no settlement data is in scope here.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not anthropic or not api_key:
        return {"utr_candidate": None, "order_id_candidate": None, "confidence": 0.0,
                "llm_invoked": False,
                "note": "LLM unavailable — no ANTHROPIC_API_KEY set, routed to exception"}

    try:
        # client construction is inside the try too: a bad base_url or a broken
        # SDK config must degrade this row, not take the whole batch down
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": narration}],
            # structured output: the response is schema-valid JSON, so there is
            # no fence-stripping or best-effort parsing to get wrong
            output_config={
                "effort": "low",  # reading one short string; no need to spend more
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
        )
        text = next(b.text for b in resp.content if b.type == "text")
        parsed = json.loads(text)
        parsed.setdefault("confidence", 0.0)
        parsed["llm_invoked"] = True
        return parsed
    except Exception as e:
        # Any failure -- network, auth, quota, malformed response -- is an
        # exception for that row, never a fallback guess.
        return {"utr_candidate": None, "order_id_candidate": None, "confidence": 0.0,
                "llm_invoked": True,
                "note": f"LLM call failed cleanly, routed to exception: {e}"}


def verify_candidate(candidate, known_utrs):
    """
    Check the model's proposal against settlement data. This is the gate the
    LLM cannot open by itself: a proposal only survives if it names a UTR that
    genuinely exists, and only if it names exactly one.
    """
    if not candidate:
        return None

    normalized = _normalize(candidate)
    if not normalized:
        return None

    exact = [u for u in known_utrs if _normalize(u) == normalized]
    if len(exact) == 1:
        return exact[0]

    # the model may return the digits without the prefix, as banks often do
    core = _numeric_core(normalized)
    if not core:
        return None
    hits = [u for u in known_utrs if _numeric_core(u) == core]
    return hits[0] if len(hits) == 1 else None


def resolve_unresolved_bank_rows(unresolved_rows, known_utrs):
    """
    Returns (links, still_exceptions).
      links: [{"txn_id", "bank_row", "utr_candidate", "confidence", "basis"}]
             -- proposals for matcher.py to verify, NOT matches
      still_exceptions: rows nothing could resolve, reported honestly
    """
    links = []
    still_exceptions = []

    for bank_row in unresolved_rows:
        result = resolve_narration(bank_row["narration"])
        candidate = result.get("utr_candidate")
        confidence = float(result.get("confidence") or 0.0)

        verified_utr = verify_candidate(candidate, known_utrs)
        invoked = bool(result.get("llm_invoked"))

        if verified_utr and confidence >= CONFIDENCE_THRESHOLD:
            links.append({
                "llm_invoked": invoked,
                "txn_id": bank_row["txn_id"],
                "bank_row": bank_row,
                "utr_candidate": verified_utr,
                "confidence": confidence,
                "basis": (f"LLM read '{candidate}' from narration -> verified against "
                          f"known UTR list as {verified_utr} (conf={confidence:.2f}); "
                          f"amount and date still checked deterministically"),
            })
        else:
            if "note" in result:
                basis = result["note"]
            elif candidate and not verified_utr:
                basis = (f"LLM proposed '{candidate}' but no settlement carries that "
                         f"UTR — proposal rejected, not force-matched")
            else:
                basis = (f"LLM confidence {confidence:.2f} below threshold "
                         f"{CONFIDENCE_THRESHOLD}")
            still_exceptions.append({
                "llm_invoked": invoked,
                "bank_row": bank_row,
                "reason_code": "narration_unresolved",
                "basis": basis,
            })

    return links, still_exceptions
