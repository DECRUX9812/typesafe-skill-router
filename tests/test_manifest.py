"""Manifest hygiene: what CI checks, plus the declared-vs-registered parity rule."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from helpers import StubContext, plugin_module

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
_UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_COMPARATOR = re.compile(r"^(>=|<=|==|!=|>|<)\s*\d+(\.\d+)*$")


def test_required_manifest_fields_are_present():
    for field in ("name", "version", "description"):
        assert str(MANIFEST.get(field) or "").strip(), f"plugin.yaml needs {field}"
    assert MANIFEST["name"] == "typesafe-skill-router"


def test_requires_hermes_clauses_parse():
    spec = str(MANIFEST.get("requires_hermes") or "")
    assert spec
    for clause in spec.split(","):
        assert _COMPARATOR.match(clause.strip()), f"bad clause {clause!r}"


def test_requires_env_is_upper_snake():
    for entry in MANIFEST.get("requires_env") or []:
        assert isinstance(entry, str) and _UPPER_SNAKE.match(entry), entry


def test_declared_hooks_match_what_register_actually_wires():
    """The admission probe fails on any undeclared registration; keep the two in lockstep."""
    module = plugin_module()
    ctx = StubContext({"enabled": False})
    module.register(ctx)
    assert set(MANIFEST["provides_hooks"]) == set(ctx.hooks)
    assert set(MANIFEST.get("provides_tools") or []) == set()
    assert set(MANIFEST.get("provides_middleware") or []) == set()


def test_config_schema_covers_exactly_the_settings_the_code_reads():
    module = plugin_module()
    assert set(MANIFEST["config_schema"]) == set(module.DEFAULTS)


def test_routing_is_opt_in_by_default():
    """Installing must not send anything anywhere until the user turns it on."""
    module = plugin_module()
    assert module.DEFAULTS["enabled"] is False


def test_thresholds_are_the_measured_ones():
    module = plugin_module()
    assert module.DEFAULTS["gate"] == pytest.approx(0.30)
    assert module.DEFAULTS["fits"] == pytest.approx(0.40)
    assert module.DEFAULTS["chunk"] <= 255  # the API rejects a Choice over more options
    assert module.DEFAULTS["backend"] == "typesafe"


def test_readme_exists_and_documents_the_privacy_boundary():
    readme = (ROOT / "README.md").read_text(encoding="utf-8") if (ROOT / "README.md").exists() else ""
    assert readme, "README.md is required for catalog review"
    assert "TYPESAFE_API_KEY" in readme
    assert "what leaves your machine" in readme.lower() or "what is sent" in readme.lower()
