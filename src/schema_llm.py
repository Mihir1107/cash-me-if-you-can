"""
Tier 3, for schemas: the model proposes what a column means, code checks it.

The alias table in `schema.py` covers the headers I thought to write down.
There are thousands of accounting packages and every one of them names things
slightly differently, so the table will always be behind. Reading "Withdrawal
Amt." and knowing it means money leaving the account IS a language problem, and
this project already has a settled answer for those.

It is the same answer as the narration tier, and the same boundary:

  * The model proposes a mapping. It never applies one.
  * `schema.verify_mapping()` then checks that proposal against the actual file
    -- do the amount columns hold amounts, do the dates parse, does the
    settlement foot against its own parts. A proposal that does not survive
    those checks is discarded, however confident it was.
  * Anything left unmapped goes to the human, as before.

That verification is not decoration. A wrong column mapping is the quietest
failure in this system: swap gross and settled and every downstream check still
passes while every figure is wrong. So the model's output is treated exactly
like a fuzzy narration proposal -- as a suggestion about where to look, never as
a conclusion.

**What is sent.** Column HEADERS only, and the file's name. No rows, no amounts,
no customer names, no narrations. A header is the one part of a finance export
that carries no financial data, which is what makes this tier acceptable in a
tool whose main promise is that the ledger stays on your machine. The verify
step reads the data, and it runs locally.

No key, or the tier turned off, and this returns nothing -- the alias table and
the mapping screen carry it, exactly as they did before.
"""

import os

try:
    import openai
except ImportError:  # optional; the caller degrades to the alias table
    openai = None

from pydantic import BaseModel

from src.schema import FIELDS

MODEL = "gpt-4o-mini"
CONFIDENCE_THRESHOLD = 0.7

# What each field means, in the terms a finance export would use. The model gets
# these rather than bare field names, because `utr` means nothing on its own and
# "the bank reference for the payout" means everything.
DESCRIPTIONS = {
    "ledger_id": "the merchant's own row identifier for this booking (voucher no, entry id)",
    "order_id": "the order or invoice reference shared between systems",
    "amount": "the money value of the row",
    "date": "the date of the row",
    "customer": "the customer or party name",
    "status": "the payment or order status text",
    "settlement_id": "the gateway's identifier for this settlement or payout",
    "payment_id": "the gateway's identifier for the payment",
    "gross_amount": "the full transaction value BEFORE fees are deducted",
    "fee": "the gateway's commission or MDR charged on the transaction",
    "tax": "tax charged on the fee, usually GST",
    "refund_amount": "any amount refunded to the customer",
    "settled_amount": "the NET amount actually paid out after fee, tax and refund",
    "settlement_date": "the date the payout was made",
    "utr": "the bank reference number for the payout (UTR, RRN, reference no)",
    "txn_id": "the bank statement's row identifier (sr no, cheque no, reference)",
    "narration": "the bank's free-text description of the transaction",
    "type": "whether the row is a debit or a credit",
}

SYSTEM_PROMPT = """\
You map column headers from a financial export onto a fixed set of fields.

You are given the name of a file, what kind of file it is, and its column headers. \
You are NOT given any data. Decide, from the header names alone, which column holds \
each requested field.

Rules:
- Use each column at most once.
- Return null for a field with no plausible column. A missing field is handled \
downstream; a wrong one is not.
- Be especially careful between gross and net amounts. "Gross", "Transaction Amount" \
and "Payment Amount" are the value before fees; "Settled", "Net", "Payout" and \
"Credit Amount" are the value after fees are deducted. Confusing these two is the \
single most damaging mistake you can make here.
- Distinguish the fee itself from tax charged on the fee. "GST", "Tax", "GST on \
commission" are tax; "Commission", "MDR", "Charges", "Fee" are the fee.
- confidence is 0..1 and should reflect how certain the header name makes you. A \
header that could plausibly be two different fields deserves a low confidence, not \
a coin flip.

Your answer is checked against the file's actual contents before it is used, so a \
guess that looks right and is wrong will be caught and thrown away. Say null when \
you do not know."""


class ColumnChoice(BaseModel):
    field: str
    column: str | None
    confidence: float


class ProposedMapping(BaseModel):
    """Strict structured output; schema-valid by construction."""

    choices: list[ColumnChoice]


def propose_mapping(columns, source, filename="uploaded file"):
    """
    Ask the model which column holds each field.

    Returns {"mapping": {field: column}, "llm_invoked": bool, "note": str|None}.
    The mapping is a PROPOSAL. It is not usable until schema.verify_mapping()
    has checked it against the file, and the caller is responsible for that.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai or not api_key:
        return {"mapping": {}, "llm_invoked": False,
                "note": "no OPENAI_API_KEY set — using the alias table only"}

    if source not in FIELDS:
        raise KeyError(f"unknown source {source!r}")

    spec = FIELDS[source]
    wanted = list(spec["required"]) + list(spec["optional"])
    columns = [str(c) for c in columns]

    ask = (f"File: {filename}\nKind: {source}\n"
           f"Columns: {columns}\n\nFields to map:\n"
           + "\n".join(f"- {f}: {DESCRIPTIONS.get(f, f)}"
                       f"{'  (required)' if f in spec['required'] else ''}"
                       for f in wanted))

    try:
        client = openai.OpenAI(api_key=api_key)
        resp = client.responses.parse(
            model=MODEL,
            input=[{"role": "system", "content": SYSTEM_PROMPT},
                   {"role": "user", "content": ask}],
            text_format=ProposedMapping,
            max_output_tokens=2000,
        )
        proposed = resp.output_parsed
    except Exception as e:
        return {"mapping": {}, "llm_invoked": True,
                "note": f"schema proposal failed, using the alias table only: {e}"}

    if proposed is None:
        return {"mapping": {}, "llm_invoked": True,
                "note": "the model returned nothing parsable"}

    # Discard anything that names a field this source does not have, a column
    # the file does not contain, or that the model itself was unsure about. None
    # of these are judgement calls -- they are the proposal failing to be
    # well-formed, which is checked before it is checked for being right.
    known, seen, mapping = set(wanted), set(), {}
    for choice in proposed.choices:
        if choice.field not in known or choice.column is None:
            continue
        if choice.column not in columns or choice.column in seen:
            continue
        if (choice.confidence or 0.0) < CONFIDENCE_THRESHOLD:
            continue
        mapping[choice.field] = choice.column
        seen.add(choice.column)

    return {"mapping": mapping, "llm_invoked": True, "note": None}
