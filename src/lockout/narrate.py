"""The one place a language model is used.

Lockout's decisions are made by deterministic SQL and comparisons — a safety interlock
that an LLM could talk out of its verdict is not a safety interlock, and the numbers on
screen have to be reproducible. So the model never decides anything.

What it does do is write the paragraph a human reads at 3am: turning
`lag_days=9, worst_day_rows=2, median_daily_rows=2257` into an explanation of what
broke, what it means for the model downstream, and what to check first. That text goes
into the incident description and the receipt on the model page.

Two rules enforced below, because they are what make the output trustworthy:

  * the model is given the already-computed facts and is told not to invent numbers;
  * if it is unavailable, or its output looks wrong, the deterministic summary is used
    instead. Narration is never on the critical path.
"""

from __future__ import annotations

import logging
import os
import re

from lockout.policy.decision import Permit

logger = logging.getLogger(__name__)

MODEL = os.environ.get("LOCKOUT_NARRATE_MODEL", "claude-sonnet-5")
MAX_TOKENS = 400

_PROMPT = """You are writing the incident description a data engineer will read when \
they find a model's training run was blocked.

Here are the facts. They were computed deterministically and are not negotiable — \
do not introduce any number that does not appear here, and do not soften or dramatise \
what happened.

Model: {model}
Verdict: {verdict}

Failing checks:
{evidence}

Write 3-5 sentences covering, in this order:
1. what actually broke, in plain language;
2. why it matters for this specific model;
3. the first thing to check.

No headings, no bullet points, no preamble, no sign-off. Do not restate the URNs. \
Write as though the reader is competent and busy."""


def _facts(permit: Permit) -> str:
    lines = []
    for e in permit.evidence:
        observed = ", ".join(f"{k}={v}" for k, v in e.observed.items())
        lines.append(
            f"- {e.rule} on {e.dataset_urn.split(',')[1]}.{e.column}: {e.description}\n"
            f"    observed: {observed}\n"
            f"    reached from the model in {e.hops} hop(s) via {' -> '.join(e.lineage_path)}\n"
            f"    features affected: {', '.join(e.features_affected) or 'unknown'}"
        )
    return "\n".join(lines)


def _numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,]*\.?\d*", text.replace(",", "")))


def narrate(permit: Permit, deterministic_fallback: str) -> str:
    """Return a human-readable incident narrative.

    Falls back to `deterministic_fallback` whenever the model is unavailable, errors, or
    produces text containing numbers that were not in the input facts.
    """
    if permit.granted or not permit.evidence:
        return deterministic_fallback
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.debug("ANTHROPIC_API_KEY unset — using the deterministic summary")
        return deterministic_fallback

    try:
        import anthropic
    except ImportError:
        logger.debug("anthropic not installed — install lockout[narrate]")
        return deterministic_fallback

    facts = _facts(permit)
    prompt = _PROMPT.format(
        model=permit.model_urn, verdict=permit.verdict, evidence=facts
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
    except Exception as exc:  # noqa: BLE001 — narration must never break a decision
        logger.warning("narration failed, using deterministic summary: %s", exc)
        return deterministic_fallback

    if not text:
        return deterministic_fallback

    # Guardrail: every number in the narrative must have come from the facts. This is
    # cheap to check and catches the one failure mode that would actually matter —
    # a plausible-sounding figure that nothing measured.
    invented = _numbers_in(text) - _numbers_in(facts) - _numbers_in(prompt)
    if invented:
        logger.warning("narration invented numbers %s — discarding it", invented)
        return deterministic_fallback

    return text
