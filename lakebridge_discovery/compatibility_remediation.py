"""
LLM-assisted remediation notes for Lakebridge Discovery's own SQL-Server-
feature compatibility flags.

An INDEPENDENT reimplementation of autovista/compatibility_remediation.py --
never an import of that module, per this codebase's hard rule that SQLGlot
Discovery (autovista/) and Lakebridge Discovery (lakebridge_discovery/)
never share parsing/query/LLM-orchestration logic (see README.md and
compatibility_scanner.py's own docstring). Using the `anthropic` SDK
directly is fine (it's already an optional project dependency) -- importing
autovista's own wrapper module around it is not.

Same shape as compatibility_scanner.py's own relationship to autovista's
scanner: detects nothing new, only explains flags
compatibility_scanner.apply_compatibility_flags() already set on
result.tables/views/stored_procedures/functions/triggers, by re-reading
each object's already-exported SQL text at
<source_export_dir>/sql/{kind}__{schema}.{name}.sql.

Hard rules, same non-negotiable contract as autovista's LLM fallback/
remediation modules:
  - Never a source of truth -- compatibility_flags themselves are still
    produced solely by compatibility_scanner.py; this module only adds a
    note alongside an already-computed flag.
  - Every result is mandatorily flagged for human review
    (needs_human_review=True).
  - Deterministic guardrails: strict output schema
    (CompatibilityRemediationResult), and a hard cap
    (config.llm_compat_max_objects_per_run) on how many objects get sent
    per run.
  - If no API key is configured, or the object count would exceed the cap,
    the object is simply left with no note (NOT guessed).

Uses the Anthropic Python SDK when LAKEBRIDGE_LLM_COMPAT_NOTES_ENABLED=true
and ANTHROPIC_API_KEY is set. No network calls happen otherwise.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lakebridge_discovery.config import LakebridgeConfig
from lakebridge_discovery.dependency_extractor import _clean_sql
from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import LakebridgeDiscoveryResult

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


class LlmClient(Protocol):
    def complete_json(self, system_prompt: str, user_text: str) -> dict: ...


@dataclass
class AnthropicLlmClient:
    api_key: str
    model: str

    def complete_json(self, system_prompt: str, user_text: str) -> dict:
        import anthropic  # imported lazily so the SDK isn't a hard dependency in fixture mode

        client = anthropic.Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return json.loads(text)


def build_compat_remediation_client(config: LakebridgeConfig) -> LlmClient | None:
    if not config.llm_compat_notes_enabled:
        return None
    if not config.llm_api_key:
        return None
    return AnthropicLlmClient(api_key=config.llm_api_key, model=config.llm_model)


def generate_remediation_note(
    client: LlmClient | None,
    object_id: str,
    compatibility_flags: list[str],
    source_text: str,
    objects_attempted_so_far: int,
    config: LakebridgeConfig,
) -> CompatibilityRemediationResult:
    if not compatibility_flags:
        return CompatibilityRemediationResult(status="unresolved", note="", confidence="low")

    if client is None:
        return CompatibilityRemediationResult(confidence="low", status="unresolved")

    if objects_attempted_so_far >= config.llm_compat_max_objects_per_run:
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


# Same scanned-category set and same glob-by-suffix file lookup
# compatibility_scanner.apply_compatibility_flags() uses -- kept in sync
# deliberately, but this module never imports that function, only reads the
# same exported files independently.
_SCANNED_CATEGORIES = ("tables", "views", "stored_procedures", "functions", "triggers")


def apply_compatibility_remediation(result: LakebridgeDiscoveryResult, export_dir: Path, config: LakebridgeConfig) -> None:
    """Sets compatibility_notes on every LakebridgeObjectRef that already
    has a non-empty compatibility_flags list, by re-reading that object's
    matching exported file. Never raises: a missing export dir, unreadable
    file, or LLM failure just leaves that object's compatibility_notes
    unset, mirroring compatibility_scanner.py's own defensive style."""
    sql_dir = export_dir / "sql"
    if not sql_dir.is_dir():
        result.warnings.append(f"compatibility_remediation: source export dir {sql_dir} not found -- skipping")
        return

    client = build_compat_remediation_client(config)
    objects_attempted = 0
    notes_generated = 0

    for category in _SCANNED_CATEGORIES:
        for obj in getattr(result, category):
            if not obj.compatibility_flags:
                continue
            matches = sorted(sql_dir.glob(f"*__{obj.name}.sql"))
            if not matches:
                continue
            try:
                text = matches[0].read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.warnings.append(f"compatibility_remediation: could not read {matches[0]}: {exc}")
                continue
            remediation = generate_remediation_note(
                client, obj.name, obj.compatibility_flags, _clean_sql(text), objects_attempted, config,
            )
            objects_attempted += 1
            if remediation.note:
                obj.compatibility_notes = remediation.note
                notes_generated += 1

    logger.info("Compatibility remediation: %d objects sent, %d notes generated", objects_attempted, notes_generated)
