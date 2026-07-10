"""
Minimal Anthropic client wrapper for the LLM Assessment phase. Same
shape/contract as autovista/llm_fallback_extractor.py's LlmClient/
AnthropicLlmClient (complete_json(system_prompt, user_text) -> dict), but
a fresh, self-contained implementation rather than an import -- this
package reads only the Discovery JSON contract, never autovista's
internals, same "phase packages are self-contained" rule
assessment/ and lakebridge_assessment/ already follow.

No network calls unless a client is actually constructed with an API key
(see config.py/build_llm_client).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

# Some models (observed with Haiku 4.5) wrap JSON output in a markdown code
# fence despite an explicit "no prose" instruction -- strip ```json / ```
# fences before parsing rather than relying on prompt wording alone.
_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LlmClient(Protocol):
    def complete_json(self, system_prompt: str, user_text: str) -> dict: ...


@dataclass
class AnthropicLlmClient:
    api_key: str
    model: str
    max_tokens: int = 400
    # A single hung request must not stall an entire ~100+-object run
    # indefinitely -- the SDK's own default timeout is much longer than
    # this bulk-classification use case ever needs.
    request_timeout_seconds: float = 30.0
    _client: object = field(default=None, repr=False, compare=False)

    def _get_client(self):
        import anthropic  # imported lazily so the SDK is only required when actually used

        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=self.request_timeout_seconds)
        return self._client

    def complete_json(self, system_prompt: str, user_text: str) -> dict:
        response = self._get_client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        text = _CODE_FENCE.sub("", text).strip()
        return json.loads(text)
