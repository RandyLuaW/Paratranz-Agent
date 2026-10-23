from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_api import FakeParaTranz  # noqa: E402
from paratranz_agent.config import AgentConfig  # noqa: E402
from fake_llm import FakeLLM  # noqa: E402
from paratranz_agent.store import Store  # noqa: E402


@pytest.fixture
def fake() -> FakeParaTranz:
    return FakeParaTranz()


@pytest.fixture
def client(fake):
    c = fake.client()
    yield c
    c.close()


@pytest.fixture
def cfg(tmp_path) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "project": {"id": 6896, "name": "Test Project", "style": "口语自然"},
            "api": {"token": "test-token"},
            "llm": {"api_key": "", "batch_size": 5, "concurrency": 2},
            "translate": {"batch_size": 5, "tm_top_k": 3},
            "review": {"batch_size": 5, "llm_second_pass": True},
            "tasks": {"max_strings_per_task": 10, "max_files_per_task": 2, "pin": False},
            "glossary": {"min_occurrences": 1, "max_terms": 50},
            "state_dir": str(tmp_path / "state"),
        }
    ).resolve()


@pytest.fixture
def store(cfg):
    s = Store(cfg.state_path / "agent.db")
    yield s
    s.close()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()
