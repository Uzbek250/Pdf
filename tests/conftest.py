"""Umumiy pytest fixture'lari."""
from __future__ import annotations

import sys
from pathlib import Path

# `app` paketi import qilinishi uchun loyiha ildizini sys.path'ga qo'shamiz
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from config.settings import Settings


@pytest.fixture
def test_settings() -> Settings:
    """Testlar uchun cache'ni o'chirilgan, kichik batch o'lchamli sozlamalar."""
    return Settings(
        GEMINI_API_KEY="test-key",
        CACHE_ENABLED=False,
        BATCH_PARAGRAPH_SIZE=2,
        WORK_DIR=Path("/tmp/doc_translator_test"),
    )
