"""LLM fallback for receipts that escape rule-based and template extractors.

Calls the Claude Code CLI in print mode (`claude -p`) as a subprocess.
Batches ~15 receipts per call. Demands JSON-Lines output for stable parsing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "claude")  # path to the Claude Code CLI
MODEL_NAME = "claude-haiku-4-5-20251001"
BATCH_SIZE = 15
BODY_CHARS = 2000  # body excerpt sent to LLM (totals usually appear early)
TIMEOUT_S = 300

PROMPT_HEADER = """You will receive numbered email receipts. For each one, extract the total
amount actually charged to the customer and a short merchant name.

OUTPUT FORMAT: One JSON object per line, in the same order as inputs. No prose.
Schema per line: {"id": <int>, "amount_cents": <int|null>, "merchant_norm": "<string|null>", "confidence": <float 0-1>}

RULES:
- amount_cents is the FINAL CHARGED amount (incl tax + tip), as integer cents (e.g. $12.34 -> 1234).
- For refunds, return a NEGATIVE integer.
- If the email is a shipping/delivery notification, marketing, or contains no charge, return amount_cents=null, confidence=1.0.
- merchant_norm is lowercase, no punctuation. Use the brand the user would recognize.
- confidence: 0.9+ if the total is unambiguous in the body; 0.6-0.9 if you had to infer; <0.6 if uncertain.

EXAMPLES:
{"id": 1, "amount_cents": 1234, "merchant_norm": "doordash", "confidence": 0.95}
{"id": 2, "amount_cents": null, "merchant_norm": "amazon", "confidence": 1.0}
{"id": 3, "amount_cents": -2500, "merchant_norm": "amazon", "confidence": 0.9}

RECEIPTS:
"""


def _format_receipt(idx: int, subject: str, sender_domain: str, body: str) -> str:
    excerpt = (body or "")[:BODY_CHARS]
    excerpt = re.sub(r"\n{3,}", "\n\n", excerpt)
    return (
        f"--- RECEIPT {idx} ---\n"
        f"From domain: {sender_domain}\n"
        f"Subject: {subject}\n"
        f"Body excerpt:\n{excerpt}\n"
    )


def _call_claude(prompt: str) -> str | None:
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", "--model", MODEL_NAME],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"  [llm] TIMEOUT after {TIMEOUT_S}s", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [llm] subprocess error: {e}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"  [llm] exit {result.returncode}: {result.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return result.stdout


def _parse_jsonl(output: str, expected_ids: set[int]) -> dict[int, dict]:
    """Parse JSONL response, indexed by id. Tolerant of extra text around the JSON lines."""
    parsed: dict[int, dict] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = obj.get("id")
        if isinstance(rid, int) and rid in expected_ids:
            parsed[rid] = obj
    return parsed


def extract_batch(receipts: list[dict]) -> list[dict | None]:
    """Run one LLM call on a batch.
    Each input receipt is a dict {subject, sender_domain, body}.
    Returns list aligned to input order; element is None if LLM didn't produce a usable result.
    """
    if not receipts:
        return []

    body = PROMPT_HEADER + "\n".join(
        _format_receipt(i + 1, r.get("subject", ""), r.get("sender_domain", ""), r.get("body", ""))
        for i, r in enumerate(receipts)
    )

    output = _call_claude(body)
    if output is None:
        return [None] * len(receipts)

    parsed = _parse_jsonl(output, expected_ids=set(range(1, len(receipts) + 1)))

    results: list[dict | None] = []
    for i, _ in enumerate(receipts, start=1):
        obj = parsed.get(i)
        if obj is None:
            results.append(None)
            continue

        amount = obj.get("amount_cents")
        merchant = obj.get("merchant_norm")
        conf = obj.get("confidence", 0.5)

        if amount is None:
            # LLM judged this is not a charge -- mark as 'none' with high confidence
            results.append({
                "amount_cents": None,
                "merchant_norm": merchant.lower() if isinstance(merchant, str) else None,
                "merchant_raw": None,
                "txn_date": None,
                "confidence": float(conf),
                "method": "llm",
                "notes": "llm: no charge",
            })
            continue

        try:
            amount_int = int(amount)
        except (TypeError, ValueError):
            results.append(None)
            continue

        results.append({
            "amount_cents": amount_int,
            "merchant_norm": merchant.lower() if isinstance(merchant, str) else None,
            "merchant_raw": None,
            "txn_date": None,
            "confidence": float(conf),
            "method": "llm",
            "notes": None,
        })

    return results
