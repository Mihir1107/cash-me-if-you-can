"""
Tier 2 of narration resolution, sitting between the regex matcher and the LLM.

A bank that mangles "UTR:UTR100005" into "NEFT-RZPY-100005/settlemnt" has still
handed us the reference number -- it just dropped the prefix. Recovering that
is string work, not reasoning, so it happens here for free instead of costing
an LLM call. This is the deliberate "where I chose NOT to use a model" tier.

Two rules keep it honest:
  1. It can only ever return a UTR that already exists in the settlement data.
  2. If two known UTRs are plausible, it returns nothing and lets the next tier
     try. An ambiguous guess is worse than no guess.

Like every other tier, it only proposes which settlement a credit should be
compared against. matcher.py still does the amount and date verification.
"""

import re
from difflib import SequenceMatcher

# A recovered reference must be this close to a known UTR to count.
SIMILARITY_THRESHOLD = 0.85
# Confidence assigned when the UTR's digits are quoted verbatim in the narration.
EXACT_CORE_CONFIDENCE = 0.95
# Shorter numeric cores collide too easily to be evidence of anything.
MIN_CORE_LEN = 5


def _normalize(text):
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def _numeric_core(utr):
    """UTR100005 -> 100005. The part banks keep when they drop the prefix."""
    digits = re.sub(r"[^0-9]", "", str(utr))
    return digits if len(digits) >= MIN_CORE_LEN else None


def _tokens(narration):
    return [t for t in re.split(r"[^A-Za-z0-9]+", str(narration)) if len(t) >= MIN_CORE_LEN]


def build_core_index(known_utrs):
    """
    {core_length: {numeric_core: [utrs sharing it]}} — built once per batch so
    recovery costs narration length rather than a scan of every settlement.
    Cores are kept as lists, not overwritten, so a shared core still reads as
    ambiguous rather than silently resolving to whichever UTR was seen last.
    """
    by_len = {}
    for utr in known_utrs:
        core = _numeric_core(utr)
        if core:
            by_len.setdefault(len(core), {}).setdefault(core, []).append(utr)
    return by_len


def _core_hits(normalized, index):
    """Every known UTR whose numeric core is quoted verbatim in the narration."""
    hits = []
    for length, bucket in index.items():
        for start in range(len(normalized) - length + 1):
            found = bucket.get(normalized[start:start + length])
            if found:
                hits.extend(found)
    return list(dict.fromkeys(hits))  # de-duplicated, order preserved


def fuzzy_resolve_narration(narration, known_utrs, core_index=None):
    """
    Returns {"utr_candidate", "confidence", "basis"} or None when nothing is
    recoverable or more than one known UTR is equally plausible.
    """
    normalized = _normalize(narration)
    tokens = _tokens(narration)

    # Rule 1: the UTR's digits quoted verbatim, prefix dropped or mangled.
    if core_index is None:
        core_index = build_core_index(known_utrs)
    core_hits = _core_hits(normalized, core_index)

    if len(core_hits) == 1:
        return {
            "utr_candidate": core_hits[0],
            "confidence": EXACT_CORE_CONFIDENCE,
            "basis": (f"narration quotes reference digits of {core_hits[0]} verbatim "
                      f"(prefix mangled), no LLM call needed"),
        }
    if len(core_hits) > 1:
        return None  # ambiguous -- refuse rather than pick

    # Rule 2: a token that is nearly a known UTR (transposed/dropped character).
    # Length prefilter first -- SequenceMatcher is the expensive call here, and
    # strings differing in length by more than a couple of characters can never
    # clear the threshold anyway.
    normalized_tokens = [_normalize(tok) for tok in tokens]
    scored = []
    for utr in known_utrs:
        target = _normalize(utr)
        best = max(
            (SequenceMatcher(None, tok, target).ratio()
             for tok in normalized_tokens if abs(len(tok) - len(target)) <= 2),
            default=0.0,
        )
        if best >= SIMILARITY_THRESHOLD:
            scored.append((best, utr))

    if len(scored) != 1:
        return None  # nothing close enough, or two equally close -- next tier

    ratio, utr = scored[0]
    return {
        "utr_candidate": utr,
        "confidence": round(ratio, 2),
        "basis": f"narration token is a {ratio:.0%} string match to known UTR {utr}, no LLM call needed",
    }


def resolve_unresolved_bank_rows(unresolved_rows, known_utrs):
    """
    Returns (links, still_unresolved).
      links: [{"txn_id", "utr_candidate", "confidence", "basis", "bank_row"}]
      still_unresolved: bank rows the LLM tier should look at
    """
    links = []
    still_unresolved = []
    core_index = build_core_index(known_utrs)  # built once, not once per row

    for bank_row in unresolved_rows:
        result = fuzzy_resolve_narration(bank_row["narration"], known_utrs,
                                         core_index=core_index)
        if result:
            links.append({
                "txn_id": bank_row["txn_id"],
                "bank_row": bank_row,
                "utr_candidate": result["utr_candidate"],
                "confidence": result["confidence"],
                "basis": result["basis"],
            })
        else:
            still_unresolved.append(bank_row)

    return links, still_unresolved
