"""
Tests for llm_assessment.llm_client's markdown-fence stripping. Mocks the
anthropic SDK's response shape rather than making a real API call.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from llm_assessment.llm_client import AnthropicLlmClient


def _mock_response(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def test_strips_json_code_fence():
    client = AnthropicLlmClient(api_key="fake", model="claude-haiku-4-5-20251001")
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _mock_response('```json\n{"tier": "Low"}\n```')
        result = client.complete_json("system", "user")
    assert result == {"tier": "Low"}


def test_strips_bare_code_fence_without_json_language_tag():
    client = AnthropicLlmClient(api_key="fake", model="claude-haiku-4-5-20251001")
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _mock_response('```\n{"tier": "High"}\n```')
        result = client.complete_json("system", "user")
    assert result == {"tier": "High"}


def test_plain_json_with_no_fence_still_works():
    client = AnthropicLlmClient(api_key="fake", model="claude-haiku-4-5-20251001")
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _mock_response('{"tier": "Medium"}')
        result = client.complete_json("system", "user")
    assert result == {"tier": "Medium"}
