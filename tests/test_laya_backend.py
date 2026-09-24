"""Laya transport and plugin configuration stay local, optional, and isolated."""

from __future__ import annotations

import json

import pytest

from helpers import StubContext, plugin_module
from typesafe_router.cache import JsonCache
from typesafe_router.client import MissingAPIKey, SystemOneClient


class _HTTPResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def _response(model: str, value: float) -> dict:
    return {
        "model": model,
        "answers": {"probe": {"type": "noul", "noul": value}},
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }


def test_local_laya_client_omits_authorization_when_no_key(monkeypatch):
    seen = {}

    def open_request(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        return _HTTPResponse(_response("laya", 0.9))

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    client = SystemOneClient(
        api_key="",
        api_key_env="LAYA_API_KEY",
        require_api_key=False,
        base_url="http://127.0.0.1:8080",
        cache_namespace="laya:http://127.0.0.1:8080",
        max_retries=0,
    )

    answer = client.probe()

    assert answer["model"] == "laya"
    assert seen["url"] == "http://127.0.0.1:8080/v1/systemone"
    assert "Authorization" not in seen["headers"]


def test_typesafe_client_still_requires_its_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    client = SystemOneClient(api_key="", require_api_key=True, max_retries=0)
    with pytest.raises(MissingAPIKey, match="TYPESAFE_API_KEY"):
        client.probe()


def test_laya_cache_namespace_cannot_reuse_typesafe_answer(tmp_path, monkeypatch):
    cache = JsonCache(tmp_path / "answers.json")
    remote = SystemOneClient(api_key="remote", cache=cache, max_retries=0)
    local = SystemOneClient(
        api_key="",
        require_api_key=False,
        base_url="http://127.0.0.1:8080",
        cache=cache,
        cache_namespace="laya:http://127.0.0.1:8080",
        max_retries=0,
    )
    monkeypatch.setattr(remote, "_post", lambda payload: (_response("jev-latest", 0.1), 0.01))
    monkeypatch.setattr(local, "_post", lambda payload: (_response("laya", 0.9), 0.01))
    question = {"probe": {"type": "noul", "instructions": "probe"}}

    assert remote.ask("same", question).model == "jev-latest"
    assert local.ask("same", question).model == "laya"
    assert len(cache) == 2


def test_laya_backend_defaults_to_loopback_and_needs_no_key(tmp_path, monkeypatch):
    module = plugin_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    ctx = StubContext({"backend": "laya"})
    settings = module._settings(ctx)

    client = module._client(ctx, settings)

    assert client.base_url == "http://127.0.0.1:8080"
    assert client.api_key == ""
    assert client.require_api_key is False
    assert module.api_key_present(settings) is True


def test_unknown_backend_fails_loudly():
    module = plugin_module()
    with pytest.raises(ValueError, match="backend must be one of"):
        module._settings(StubContext({"backend": "mystery"}))
