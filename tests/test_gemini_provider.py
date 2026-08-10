"""Gemini JSON javobini parse qilish bo'yicha regression testlar."""
from __future__ import annotations

import pytest

from providers.gemini_provider import GeminiProvider, _JsonBatchParseError


@pytest.mark.parametrize(
    "raw_text",
    [
        '["Birinchi", "Ikkinchi"]',
        '```json\n["Birinchi", "Ikkinchi"]\n```',
        'Mana natija:\n["Birinchi", "Ikkinchi"]\nTayyor.',
        '["Birinchi", "Ikkinchi"]\nQo\'shimcha izoh',
    ],
)
def test_parse_json_array_response_accepts_wrapped_json(raw_text: str) -> None:
    result = GeminiProvider._parse_json_array_response(raw_text, expected_len=2)
    assert result == ["Birinchi", "Ikkinchi"]


def test_parse_json_array_response_reports_raw_response_on_failure() -> None:
    raw_text = 'Mana natija: {"not": "an array"}'

    with pytest.raises(_JsonBatchParseError) as exc_info:
        GeminiProvider._parse_json_array_response(raw_text, expected_len=1)

    assert exc_info.value.raw_text == raw_text
    assert "JSON" in str(exc_info.value)


def test_parse_json_array_response_rejects_wrong_batch_length() -> None:
    raw_text = '["Faqat bitta"]'

    with pytest.raises(_JsonBatchParseError, match="2 ta parcha"):
        GeminiProvider._parse_json_array_response(raw_text, expected_len=2)
