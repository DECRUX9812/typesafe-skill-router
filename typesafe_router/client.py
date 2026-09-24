"""Thin client for POST /v1/systemone, with retries, disk cache, and usage accounting.

Deliberately dependency-free (stdlib only). The official `typesafe-sdk` is a fine choice
too; this client exists so the lab runs anywhere, replays cached answers offline, and
reports what each experiment actually cost.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .cache import JsonCache

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
ENDPOINT = "/v1/systemone"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 529}

def env_file() -> Path:
    """``<HERMES_HOME>/.env`` — where Hermes keeps ``KEY=value`` secrets.

    Resolved per call: one process serves several Hermes homes/profiles, so a path
    captured at import time would be the wrong home for the next turn.
    """
    home = os.environ.get("HERMES_HOME")
    root = Path(home) if home else (Path.home() / ".hermes")
    return root / ".env"

# Published in the re-ranking cookbook (token counts -> $0.0645 for 1,536,002 in /
# 25,200 out). Verify against https://console.typesafe.ai before quoting to anyone.
PRICE_PER_MTOK_IN = 0.04
PRICE_PER_MTOK_OUT = 0.13


class SystemOneError(RuntimeError):
    """Any non-retryable API failure."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class MissingCachedResponse(SystemOneError):
    """Offline mode, and this exact request was never run against the live API."""


class MissingAPIKey(SystemOneError):
    """No key in env or .env; nothing to run live with."""


def load_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs from .env into os.environ (real env wins). Returns what it saw."""
    target = Path(path) if path else env_file()
    found: dict[str, str] = {}
    if not target.exists():
        return found
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        found[key] = value
        if value:
            os.environ.setdefault(key, value)
    return found


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_calls: int = 0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_calls += other.cached_calls

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * PRICE_PER_MTOK_IN
            + self.output_tokens / 1_000_000 * PRICE_PER_MTOK_OUT
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class Answer:
    """One typed answer. `raw` keeps every field the API returned."""

    id: str
    kind: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def noul(self) -> float:
        return float(self.raw["noul"])

    @property
    def choice(self) -> str:
        return str(self.raw["choice"])

    @property
    def score(self) -> float:
        return float(self.raw["score"])

    @property
    def probabilities(self) -> dict[str, float]:
        return {k: float(v) for k, v in (self.raw.get("probabilities") or {}).items()}

    @property
    def confidence(self) -> float | None:
        value = self.raw.get("confidence")
        return None if value is None else float(value)

    @property
    def legend(self) -> dict[str, str]:
        return dict(self.raw.get("legend") or {})


@dataclass
class Response:
    model: str
    answers: dict[str, Answer]
    usage: Usage
    cached: bool = False
    latency_s: float = 0.0
    request: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Answer:
        return self.answers[key]

    def get(self, key: str) -> Optional[Answer]:
        return self.answers.get(key)

    @property
    def nouls(self) -> dict[str, Answer]:
        return {k: a for k, a in self.answers.items() if a.kind == "noul"}

    @property
    def choices(self) -> dict[str, Answer]:
        return {k: a for k, a in self.answers.items() if a.kind == "choice"}

    @property
    def scores(self) -> dict[str, Answer]:
        return {k: a for k, a in self.answers.items() if a.kind == "score"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "answers": {k: a.raw for k, a in self.answers.items()},
            "usage": self.usage.as_dict(),
            "cached": self.cached,
            "latency_s": round(self.latency_s, 3),
        }


class SystemOneClient:
    """One endpoint, typed answers, optional cache. Cheap enough to call per turn."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        api_key_env: str = "TYPESAFE_API_KEY",
        require_api_key: bool = True,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        cache: JsonCache | None = None,
        cache_namespace: str = "",
        offline: bool = False,
        timeout: float = 90.0,
        max_retries: int = 4,
        sleeper=time.sleep,
    ):
        load_env_file()
        resolved_key = api_key if api_key is not None else os.environ.get(api_key_env, "")
        self.api_key = str(resolved_key).strip()
        self.api_key_env = api_key_env
        self.require_api_key = require_api_key
        self.base_url = (base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("TYPESAFE_DEFAULT_MODEL") or DEFAULT_MODEL
        self.cache = cache
        self.cache_namespace = cache_namespace.strip()
        self.offline = offline
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleeper
        self.usage = Usage()

    # -- requests ---------------------------------------------------------------
    def build_payload(self, state: Any, questions: Mapping[str, dict], *, model: str | None = None) -> dict[str, Any]:
        """The exact request body `ask` sends (also what the playground link encodes)."""
        return {"state": state, "model": model or self.model, "questions": dict(questions)}

    def ask(
        self,
        state: Any,
        questions: Mapping[str, dict],
        *,
        model: str | None = None,
        offline: bool | None = None,
        use_cache: bool = True,
    ) -> Response:
        payload = self.build_payload(state, questions, model=model)
        cache_key = (
            {"_namespace": self.cache_namespace, "request": payload}
            if self.cache_namespace else payload
        )
        offline = self.offline if offline is None else offline

        if use_cache and self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                resp = self._parse(cached, payload, cached=True)
                self.usage.add(Usage(calls=1, cached_calls=1))
                return resp

        if offline:
            raise MissingCachedResponse(
                "offline mode: this request is not in the cache. Re-run without --offline "
                "(and with TYPESAFE_API_KEY set) to record it.",
                status=None,
            )

        raw, latency = self._post(payload)

        if use_cache and self.cache is not None:
            self.cache.put(cache_key, raw)

        resp = self._parse(raw, payload, cached=False)
        resp.latency_s = latency
        self.usage.add(Usage(calls=1, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens))
        return resp

    def probe(self) -> dict[str, Any]:
        """One tiny real request: proves key, endpoint, model name, and answer parsing.

        The docs define no GET endpoint, so this asks a single trivial Noul instead of
        guessing at one. It costs one small inference.
        """
        questions = {
            "probe": {
                "type": "noul",
                "instructions": "Is this a probe request?",
                "true": "A probe.",
                "false": "Not a probe.",
            }
        }
        response = self.ask({"probe": True}, questions)
        return {"model": response.model, "answer": response.nouls["probe"].noul,
                "usage": response.usage.as_dict()}

    # -- internals --------------------------------------------------------------
    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        url = f"{self.base_url}{ENDPOINT}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        started = time.perf_counter()
        raw = self._request(url, method="POST", body=body)
        return json.loads(raw), time.perf_counter() - started

    def _request(self, url: str, *, method: str, body: bytes | None) -> str:
        if self.require_api_key and not self.api_key:
            raise MissingAPIKey(
                f"{self.api_key_env} is not set. Put it in "
                f"{env_file()} ({self.api_key_env}=...) or export it.",
                status=None,
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "typesafe-skill-router/1.1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        attempt = 0
        while True:
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as handle:
                    return handle.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code in RETRY_STATUSES and attempt < self.max_retries:
                    self._sleep(min(2.0 ** attempt, 8.0))
                    attempt += 1
                    continue
                raise SystemOneError(
                    f"System One endpoint {exc.code} {exc.reason}: {detail[:400]}",
                    status=exc.code,
                    body=detail,
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    self._sleep(min(2.0 ** attempt, 8.0))
                    attempt += 1
                    continue
                raise SystemOneError(f"System One endpoint unreachable: {exc.reason}") from exc

    def _parse(self, raw: dict[str, Any], payload: dict[str, Any], *, cached: bool) -> Response:
        answers = {
            key: Answer(id=key, kind=str(value.get("type", "")), raw=dict(value))
            for key, value in (raw.get("answers") or {}).items()
        }
        usage = raw.get("usage") or {}
        return Response(
            model=str(raw.get("model", payload.get("model", self.model))),
            answers=answers,
            usage=Usage(
                calls=1,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            ),
            cached=cached,
            request=payload,
        )
