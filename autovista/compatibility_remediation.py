"""
LLM-assisted remediation notes for SQL-Server-feature compatibility flags.

compatibility_scanner.py only answers "is construct X present" (PIVOT,
MERGE, OPENJSON, LINKED_SERVER, ...) -- a named signal, not an explanation.
This module is a second, independent use of the LLM: for an object that
already has at least one compatibility_flags entry, ask the model for a
short plain-English note on why that construct is a migration risk on
Databricks/Spark SQL and roughly what rework it implies, purely to speed up
human triage of the (usually large) flagged-object list.

Hard rules, same non-negotiable contract as llm_fallback_extractor.py:
  - Never a source of truth for anything -- compatibility_flags themselves
    are still produced solely by compatibility_scanner.py's sqlglot AST /
    regex scan; this module only adds a note alongside an already-computed
    flag, it never adds, removes, or reinterprets a flag.
  - Every result is mandatorily flagged for human review
    (needs_human_review=True) -- a starting point for a reviewer, not an
    accepted migration plan.
  - Deterministic guardrails: strict output schema
    (CompatibilityRemediationResult), and a hard cap
    (config.max_objects_per_run) on how many objects get sent per run --
    tracked completely separately from LlmFallbackConfig's own cap, so
    enabling/disabling one feature never changes the other's budget.
  - If no API key is configured, or the object count would exceed the cap,
    the object is simply left with no note (NOT guessed) -- flags are
    still fully usable without a note, this is enrichment only.

Uses the Anthropic Python SDK when AUTOVISTA_LLM_COMPAT_NOTES_ENABLED=true
and ANTHROPIC_API_KEY is set. No network calls happen otherwise.

Reuses LlmClient/AnthropicLlmClient from llm_fallback_extractor.py --
that's an intra-engine reuse (both live under autovista/), not a violation
of the SQLGlot/Lakebridge independence rule, which is about the two
Discovery *engines* never sharing code with each other.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from autovista.config import CompatibilityRemediationConfig
from autovista.llm_fallback_extractor import AnthropicLlmClient, LlmClient

REMEDIATION_SYSTEM_PROMPT = """You are assisting a SQL Server -> Databricks/Spark SQL migration \
discovery pipeline. You will be given the name of a database object, a list of named \
SQL-Server-feature compatibility flags already detected in its definition (e.g. PIVOT, MERGE, \
OPENJSON, LINKED_SERVER, XP_CMDSHELL), and the object's source text.

For each flag, briefly explain why it is a migration risk on Databricks/Spark SQL and roughly what \
rework it implies. Be concise and specific to what's actually in the text, not generic.

Respond ONLY with JSON matching this schema, no prose:
{"note": "<=500 chars covering all flags", "confidence": "low"|"medium"|"high"}"""


@dataclass
class CompatibilityRemediationResult:
    note: str = ""
    confidence: str = "low"
    needs_human_review: bool = True
    status: str = "unresolved"  # "llm_generated" | "unresolved"


def build_compat_remediation_client(config: CompatibilityRemediationConfig) -> LlmClient | None:
    if not config.enabled:
        return None
    if not config.api_key:
        return None
    return AnthropicLlmClient(api_key=config.api_key, model=config.model)


def generate_remediation_note(
    client: LlmClient | None,
    object_id: str,
    compatibility_flags: list[str],
    source_text: str,
    objects_attempted_so_far: int,
    config: CompatibilityRemediationConfig,
) -> CompatibilityRemediationResult:
    if not compatibility_flags:
        return CompatibilityRemediationResult(status="unresolved", note="", confidence="low")

    if client is None:
        return CompatibilityRemediationResult(confidence="low", status="unresolved")

    if objects_attempted_so_far >= config.max_objects_per_run:
        return CompatibilityRemediationResult(confidence="low", status="unresolved")

    user_text = (
        f"Object: {object_id}\nFlags: {', '.join(compatibility_flags)}\n\nSource text:\n{source_text}"
    )
    try:
        raw = client.complete_json(REMEDIATION_SYSTEM_PROMPT, user_text)
        return CompatibilityRemediationResult(
            note=raw.get("note", ""),
            confidence=raw.get("confidence", "low"),
            needs_human_review=True,
            status="llm_generated",
        )
    except Exception:  # noqa: BLE001 - one object's LLM failure must not fail the run
        return CompatibilityRemediationResult(confidence="low", status="unresolved")
