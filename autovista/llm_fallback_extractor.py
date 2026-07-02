"""
LLM-assisted extraction for constructs no static parser can resolve:
Script Task source code (SsisTaskEntity.unparseable_body=True) and
dynamic SQL that sqlglot flagged unresolved (sql_lineage_parser
LineageResult.parse_status == "unresolved").

Hard rules, non-negotiable per the Discovery-phase contract:
  - Never the source of truth for row counts/sizes -- those only ever
    come from direct_metadata queries. This module is never called for
    that data.
  - Every result is mandatorily flagged for human review
    (needs_human_review=True) -- LLM output here is a starting point for
    a reviewer, not an accepted fact.
  - Deterministic guardrails: strict output schema (LlmExtractionResult),
    and a hard cap (config.max_objects_per_run) on how many objects get
    sent per run, since cost/latency scale with object count.
  - If no API key is configured, or the object count would exceed the
    cap, objects are marked `unresolved` (NOT silently skipped, NOT
    guessed) so they still show up for human triage.

Uses the Anthropic Python SDK when AUTOVISTA_LLM_FALLBACK_ENABLED=true
and ANTHROPIC_API_KEY is set. No network calls happen otherwise --
`FixtureLlmClient` / the disabled path both simply return "not attempted".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from autovista.config import LlmFallbackConfig

EXTRACTION_SYSTEM_PROMPT = """You are assisting a SQL Server / SSIS migration discovery pipeline.
You will be given either (a) a Script Task source code body from an SSIS package, or
(b) a T-SQL dynamic-SQL statement. Identify which database tables and stored procedures
it reads from or writes to, to the best of your ability from the text alone.

Respond ONLY with JSON matching this schema, no prose:
{"referenced_tables": ["schema.table", ...], "referenced_procs": ["schema.proc", ...],
 "confidence": "low"|"medium"|"high", "notes": "<=200 chars explaining your reasoning or caveats"}

If you cannot determine any references, return empty lists with confidence "low" and say why in notes."""


@dataclass
class LlmExtractionResult:
    referenced_tables: list[str] = field(default_factory=list)
    referenced_procs: list[str] = field(default_factory=list)
    confidence: str = "low"
    notes: str = ""
    needs_human_review: bool = True
    parse_status: str = "llm_inferred"  # or "unresolved" if not attempted


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


def build_llm_client(config: LlmFallbackConfig) -> LlmClient | None:
    if not config.enabled:
        return None
    if not config.api_key:
        return None
    return AnthropicLlmClient(api_key=config.api_key, model=config.model)


def extract_with_llm_fallback(client: LlmClient | None, object_id: str, source_text: str, objects_attempted_so_far: int, config: LlmFallbackConfig) -> LlmExtractionResult:
    if client is None:
        return LlmExtractionResult(
            confidence="low",
            notes="LLM fallback not attempted: disabled or ANTHROPIC_API_KEY not set. Flagged unresolved for human review.",
            needs_human_review=True,
            parse_status="unresolved",
        )

    if objects_attempted_so_far >= config.max_objects_per_run:
        return LlmExtractionResult(
            confidence="low",
            notes=f"LLM fallback not attempted: run cap of {config.max_objects_per_run} objects reached.",
            needs_human_review=True,
            parse_status="unresolved",
        )

    try:
        raw = client.complete_json(EXTRACTION_SYSTEM_PROMPT, source_text)
        return LlmExtractionResult(
            referenced_tables=list(raw.get("referenced_tables", [])),
            referenced_procs=list(raw.get("referenced_procs", [])),
            confidence=raw.get("confidence", "low"),
            notes=raw.get("notes", ""),
            needs_human_review=True,
            parse_status="llm_inferred",
        )
    except Exception as exc:  # noqa: BLE001 - one object's LLM failure must not fail the run
        return LlmExtractionResult(
            confidence="low",
            notes=f"LLM fallback call failed: {type(exc).__name__}: {exc}",
            needs_human_review=True,
            parse_status="unresolved",
        )
