from pathlib import Path
from typing import ClassVar

import pytest
from pydantic_settings import SettingsConfigDict

from app.core.config import Settings


class IsolatedSettings(Settings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(env_file=None)


REQUIRED_SETTINGS = {
    "ENABLE_DOMAIN_GATE",
    "DOMAIN_GATE_MODE",
    "DOMAIN_ONTOLOGY_PATH",
    "DOMAIN_GATE_MIN_CONFIDENCE",
    "DOMAIN_GATE_FALLBACK_ON_EMPTY",
    "GRAPH_DIFFUSION_ENABLED",
    "GRAPH_RELATION_WEIGHTS",
    "GRAPH_DEPTH_DECAY",
    "GRAPH_MAX_CANDIDATES",
    "ENABLE_RERANKER",
    "RERANK_PROVIDER",
    "RERANK_MODEL",
    "RERANK_BASE_URL",
    "RERANK_API_KEY",
    "RERANK_TOP_N",
    "RERANK_TIMEOUT_SECONDS",
    "DOMAIN_WEIGHT",
    "RERANK_WEIGHT",
}


def test_settings_defaults_keep_external_reranker_disabled() -> None:
    settings = _settings_without_env_file()

    assert settings.ENABLE_DOMAIN_GATE is True
    assert settings.DOMAIN_GATE_MODE == "soft"
    assert settings.DOMAIN_ONTOLOGY_PATH == "configs/discipline_ontology.yaml"
    assert settings.DOMAIN_GATE_MIN_CONFIDENCE == 0.3
    assert settings.DOMAIN_GATE_FALLBACK_ON_EMPTY is True
    assert settings.GRAPH_DIFFUSION_ENABLED is True
    assert settings.GRAPH_RELATION_WEIGHTS == ""
    assert settings.GRAPH_DEPTH_DECAY == 0.5
    assert settings.GRAPH_MAX_CANDIDATES == 200
    assert settings.ENABLE_RERANKER is False
    assert settings.RERANK_PROVIDER == "heuristic"
    assert settings.RERANK_MODEL == ""
    assert settings.RERANK_BASE_URL == ""
    assert settings.RERANK_API_KEY == ""
    assert settings.RERANK_TOP_N == 50
    assert settings.RERANK_TIMEOUT_SECONDS == 30.0
    assert settings.DOMAIN_WEIGHT == 0.15
    assert settings.RERANK_WEIGHT == 0.20


def test_settings_parse_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    overrides = {
        "ENABLE_DOMAIN_GATE": "false",
        "DOMAIN_GATE_MODE": "hard",
        "DOMAIN_ONTOLOGY_PATH": "configs/custom_ontology.yaml",
        "DOMAIN_GATE_MIN_CONFIDENCE": "0.75",
        "DOMAIN_GATE_FALLBACK_ON_EMPTY": "false",
        "GRAPH_DIFFUSION_ENABLED": "false",
        "GRAPH_RELATION_WEIGHTS": "has_research_area=1.0,similar_research=0.8",
        "GRAPH_DEPTH_DECAY": "0.25",
        "GRAPH_MAX_CANDIDATES": "25",
        "ENABLE_RERANKER": "true",
        "RERANK_PROVIDER": "external-test",
        "RERANK_MODEL": "rerank-model",
        "RERANK_BASE_URL": "https://reranker.invalid/v1",
        "RERANK_API_KEY": "test-placeholder-key",
        "RERANK_TOP_N": "12",
        "RERANK_TIMEOUT_SECONDS": "5.5",
        "DOMAIN_WEIGHT": "0.30",
        "RERANK_WEIGHT": "0.40",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    settings = _settings_without_env_file()

    assert settings.ENABLE_DOMAIN_GATE is False
    assert settings.DOMAIN_GATE_MODE == "hard"
    assert settings.DOMAIN_ONTOLOGY_PATH == "configs/custom_ontology.yaml"
    assert settings.DOMAIN_GATE_MIN_CONFIDENCE == 0.75
    assert settings.DOMAIN_GATE_FALLBACK_ON_EMPTY is False
    assert settings.GRAPH_DIFFUSION_ENABLED is False
    assert settings.GRAPH_RELATION_WEIGHTS == "has_research_area=1.0,similar_research=0.8"
    assert settings.GRAPH_DEPTH_DECAY == 0.25
    assert settings.GRAPH_MAX_CANDIDATES == 25
    assert settings.ENABLE_RERANKER is True
    assert settings.RERANK_PROVIDER == "external-test"
    assert settings.RERANK_MODEL == "rerank-model"
    assert settings.RERANK_BASE_URL == "https://reranker.invalid/v1"
    assert settings.RERANK_API_KEY == "test-placeholder-key"
    assert settings.RERANK_TOP_N == 12
    assert settings.RERANK_TIMEOUT_SECONDS == 5.5
    assert settings.DOMAIN_WEIGHT == 0.30
    assert settings.RERANK_WEIGHT == 0.40


def test_env_example_mirrors_required_settings_without_secrets() -> None:
    env_values = _read_env_example()

    assert REQUIRED_SETTINGS <= set(env_values)
    assert env_values["ENABLE_RERANKER"] == "false"
    assert env_values["RERANK_PROVIDER"] == "heuristic"
    assert env_values["RERANK_MODEL"] == ""
    assert env_values["RERANK_BASE_URL"] == ""
    assert env_values["RERANK_API_KEY"] == ""
    assert env_values["LLM_API_KEY"] == ""
    assert "..." not in Path(".env.example").read_text(encoding="utf-8")


def _read_env_example() -> dict[str, str]:
    env_path = Path(".env.example")
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"Invalid .env.example line: {raw_line}"
        values[key] = value
    return values


def _settings_without_env_file() -> Settings:
    return IsolatedSettings()
