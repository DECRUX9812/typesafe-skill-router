"""TypeSafe skill router — name the one skill worth loading, before the model call.

A ``pre_llm_call`` hook sends the request to TypeSafe (Jev) before the model runs and appends
a single ``<skill_relevance>`` line naming the fitting skill to the **user message** — never
the system prompt, which is the prompt-cache prefix and has to stay byte-stable for the life
of a conversation. Nothing at all is injected when nothing fits.

Settings live under ``plugins.entries.typesafe-skill-router.settings``;
``hermes typesafe-skill-router {on,off,status,suggest,check}`` drives them.

Stdlib only. Every failure path (missing key, no roster, timeout, transport error) logs one
line and returns ``None``; a router problem must never break a turn.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Hermes imports this file as a package: ``hermes_plugins.typesafe_skill_router``.
    from .typesafe_router.cache import JsonCache
    from .typesafe_router.client import SystemOneClient, load_env_file
    from .typesafe_router.roster import Skill, load_roster, skills_root, stats
    from .typesafe_router.router import FITS_MARGIN, FITS_THRESHOLD, GATE_THRESHOLD, suggest
except ImportError:  # imported as a bare module (tooling that loads __init__.py by path)
    import sys as _sys

    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in _sys.path:
        _sys.path.insert(0, _HERE)
    from typesafe_router.cache import JsonCache
    from typesafe_router.client import SystemOneClient, load_env_file
    from typesafe_router.roster import Skill, load_roster, skills_root, stats
    from typesafe_router.router import FITS_MARGIN, FITS_THRESHOLD, GATE_THRESHOLD, suggest

PLUGIN_NAME = "typesafe-skill-router"
PLUGIN_VERSION = "1.1.0"

#: Every setting is optional; these are what a fresh install runs with. Thresholds are the
#: cookbook's starting points, measured against a live roster before changing them.
DEFAULTS: Dict[str, Any] = {
    "enabled": False,          # opt-in: nothing is sent anywhere until you turn it on
    "backend": "typesafe",    # typesafe (hosted) or laya (local laya.cpp server)
    "gate": GATE_THRESHOLD,    # mean of the three request judgments; below -> suggest nothing
    "fits": FITS_THRESHOLD,    # winner's own "does this fit" judgment; below -> nothing
    "fits_margin": FITS_MARGIN,  # lead fits needs over the Choice winner's own to override it
    "shortlist": 3,            # candidates carried into the second request
    "chunk": 240,              # the API caps one Choice at 255 options
    "excerpt": 700,            # SKILL.md characters each candidate brings
    "timeout": 10.0,           # wall-clock seconds one routing decision may take
    "suggest_chars": 4000,     # longer user messages are left alone
    "model": "jev-latest",
    "base_url": "",            # default https://api.typesafe.ai
    "roster_dir": "",          # default <hermes home>/skills
    "cache_path": "",          # default <hermes home>/plugins/<plugin>/cache.json
    "log_path": "",            # default <hermes home>/logs/<plugin>.log
}

logger = logging.getLogger(__name__)

_ROSTER_TTL = 300.0
_roster_cache: Dict[str, tuple] = {}  # resolved roster dir -> (built_at, unfiltered scan)
_roster_lock = threading.RLock()
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_notified: set = set()


# ─── settings ────────────────────────────────────────────────────────────────
def hermes_home() -> Path:
    """The active Hermes home. Read per call — one process can serve several profiles."""
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else (Path.home() / ".hermes")


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _setting(ctx: Any, key: str) -> Any:
    try:
        return ctx.get_config(key, DEFAULTS[key])
    except Exception:  # a ctx without get_config (tests, probes) falls back to the default
        return DEFAULTS[key]


def _settings(ctx: Any) -> Dict[str, Any]:
    backend = str(_setting(ctx, "backend") or DEFAULTS["backend"]).strip().lower()
    if backend not in {"typesafe", "laya"}:
        raise ValueError("backend must be one of: typesafe, laya")
    return {
        "enabled": _as_bool(_setting(ctx, "enabled"), DEFAULTS["enabled"]),
        "backend": backend,
        "gate": _as_float(_setting(ctx, "gate"), DEFAULTS["gate"]),
        "fits": _as_float(_setting(ctx, "fits"), DEFAULTS["fits"]),
        "fits_margin": max(0.0, _as_float(_setting(ctx, "fits_margin"), DEFAULTS["fits_margin"])),
        "shortlist": max(1, _as_int(_setting(ctx, "shortlist"), DEFAULTS["shortlist"])),
        "chunk": max(1, _as_int(_setting(ctx, "chunk"), DEFAULTS["chunk"])),
        "excerpt": max(80, _as_int(_setting(ctx, "excerpt"), DEFAULTS["excerpt"])),
        "timeout": max(1.0, _as_float(_setting(ctx, "timeout"), DEFAULTS["timeout"])),
        "suggest_chars": max(0, _as_int(_setting(ctx, "suggest_chars"), DEFAULTS["suggest_chars"])),
        "model": str(_setting(ctx, "model") or DEFAULTS["model"]),
        "base_url": str(_setting(ctx, "base_url") or ""),
        "roster_dir": str(_setting(ctx, "roster_dir") or ""),
        "cache_path": str(_setting(ctx, "cache_path") or ""),
        "log_path": str(_setting(ctx, "log_path") or ""),
    }


def _state_dir() -> Path:
    return hermes_home() / "plugins" / PLUGIN_NAME


def cache_path(ctx: Any) -> Path:
    configured = _settings(ctx)["cache_path"]
    return Path(configured).expanduser() if configured else _state_dir() / "cache.json"


def log_path(ctx: Any) -> Path:
    configured = _settings(ctx)["log_path"]
    return Path(configured).expanduser() if configured else hermes_home() / "logs" / f"{PLUGIN_NAME}.log"


def roster_dir(ctx: Any) -> Path:
    configured = _settings(ctx)["roster_dir"]
    return Path(configured).expanduser() if configured else skills_root()


# ─── plumbing ────────────────────────────────────────────────────────────────
def _log(path: Path, kind: str, detail: str) -> None:
    """One line per decision. Never raises: the log is diagnostics, not the feature."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{kind}\t{detail}\n")
    except OSError:
        pass


def _notify_once(path: Path, key: str, kind: str, detail: str) -> None:
    with _roster_lock:
        if key in _notified:
            return
        _notified.add(key)
    _log(path, kind, detail)


def _executor_handle() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="typesafe-router")
        return _executor


def _within(seconds: float, fn):
    """Run *fn* with a wall-clock budget.

    The pool is shared and never shut down here: exiting a ``ThreadPoolExecutor`` context
    waits for the worker, which would turn the budget into a lie. The worker is bounded by the
    HTTP client's own timeout, so a timed-out routing lands one abandoned thread at worst.
    """
    future = _executor_handle().submit(fn)
    try:
        return future.result(timeout=seconds)
    except FutureTimeoutError:
        future.cancel()
        raise TimeoutError(f"routing exceeded the {seconds:.0f}s budget")


# ─── config.yaml: skills.disabled ────────────────────────────────────────────
#: Hermes never offers these skills to the agent (agent/skill_utils.get_disabled_skill_names),
#: so the roster must not suggest them either — a disabled name is one ``skill_view`` refuses.
#: ``hermes-agent`` mirrors Hermes' own ESSENTIAL_SKILLS carve-out.
ESSENTIAL_SKILLS = frozenset({"hermes-agent"})


def _config_string_set(value: Any) -> "set[str]":
    """A ``disabled``-style config entry as a set of names. Mirrors agent/skill_utils'
    ``parse_config_string_list`` + ``_normalize_string_set``: a scalar is one name, a
    ``"[...]"`` string is a serialized list (``hermes config set`` stores those), and a
    real list/tuple/set is itemized. Anything else contributes nothing."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = ast.literal_eval(stripped)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, list):
                return {name for name in (str(item).strip() for item in parsed) if name}
        return {stripped} if stripped else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {name for name in (str(item).strip() for item in value) if name}
    return set()


def _session_env(name: str) -> str:
    """A session var the way Hermes reads it (``gateway.session_context.get_session_env``):
    the session-bound ContextVar wins, then the process environment. The gateway binds
    ``HERMES_SESSION_PLATFORM`` per *session* — concurrent sessions carry different
    platforms without mutating ``os.environ``, so a plain env read misses them. Outside
    a Hermes process (or a host without the helper) this is just the env var."""
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "")
    except Exception:
        return os.environ.get(name, "")


def _disabled_skill_names() -> "set[str]":
    """Names disabled in ``<hermes home>/config.yaml``: the global ``skills.disabled``
    list unioned with the session platform's ``skills.platform_disabled`` list — the
    same union Hermes applies when building the agent's own skill index. Best-effort
    in both directions: a config that cannot be read disables nothing, which matches
    pre-filter behaviour exactly."""
    try:
        import yaml

        config = yaml.safe_load((hermes_home() / "config.yaml").read_text(encoding="utf-8")) or {}
        skills_cfg = config.get("skills") or {}
        if not isinstance(skills_cfg, dict):
            return set()
        disabled = _config_string_set(skills_cfg.get("disabled"))
        platform = os.environ.get("HERMES_PLATFORM") or _session_env("HERMES_SESSION_PLATFORM")
        per_platform = skills_cfg.get("platform_disabled")
        if platform and isinstance(per_platform, dict):
            disabled |= _config_string_set(per_platform.get(platform))
        return disabled - ESSENTIAL_SKILLS
    except Exception:
        return set()


def _roster(ctx: Any) -> List[Skill]:
    """The live roster. The directory scan is cached for ``_ROSTER_TTL`` seconds
    (installs change under us), but the disabled-skill filter runs on every call —
    the cache key is only the directory, while the filtered result also depends on
    config.yaml and the session platform, both mutable inside the TTL."""
    directory = roster_dir(ctx)
    key = str(directory)
    now = time.time()
    with _roster_lock:
        hit = _roster_cache.get(key)
        scanned = hit[1] if hit and now - hit[0] < _ROSTER_TTL else None
    if scanned is None:
        scanned = load_roster(directory)
        with _roster_lock:
            _roster_cache[key] = (now, scanned)
    # Keep the roster aligned with the agent's index: never suggest a disabled skill.
    disabled = _disabled_skill_names()
    return [s for s in scanned if s.name not in disabled]


def _client(ctx: Any, settings: Dict[str, Any]) -> SystemOneClient:
    backend = settings["backend"]
    if backend == "laya":
        base_url = settings["base_url"] or "http://127.0.0.1:8080"
        api_key_env = "LAYA_API_KEY"
        require_api_key = False
        model = "laya-latest" if settings["model"] == DEFAULTS["model"] else settings["model"]
    else:
        base_url = settings["base_url"] or "https://api.typesafe.ai"
        api_key_env = "TYPESAFE_API_KEY"
        require_api_key = True
        model = settings["model"]
    return SystemOneClient(
        api_key_env=api_key_env,
        require_api_key=require_api_key,
        base_url=base_url,
        model=model,
        cache=JsonCache(cache_path(ctx)),
        cache_namespace=f"{backend}:{base_url.rstrip('/')}",
        timeout=settings["timeout"],
    )


def api_key_present(settings: Optional[Dict[str, Any]] = None) -> bool:
    backend = (settings or {}).get("backend", "typesafe")
    if backend == "laya":
        return True  # loopback laya.cpp accepts unauthenticated requests by default
    if os.environ.get("TYPESAFE_API_KEY", "").strip():
        return True
    load_env_file()
    return bool(os.environ.get("TYPESAFE_API_KEY", "").strip())


# ─── the hook ────────────────────────────────────────────────────────────────
def route(ctx: Any, request: str) -> Optional[dict]:
    """One routing decision. Returns the block to inject, or ``None`` for silence."""
    settings = _settings(ctx)
    path = log_path(ctx)
    if not settings["enabled"]:
        return None
    if not isinstance(request, str):
        return None  # multimodal content parts: not a routing question
    text = request.strip()
    if not text or text.startswith("/"):
        return None  # slash commands select a skill themselves
    if settings["suggest_chars"] and len(text) > settings["suggest_chars"]:
        return None
    if not api_key_present(settings):
        _notify_once(
            path, "no-key", "skip",
            "TYPESAFE_API_KEY is not set (put it in <hermes home>/.env); staying quiet",
        )
        return None

    skills = _roster(ctx)
    if not skills:
        _notify_once(path, "no-roster", "skip", f"no skills found under {roster_dir(ctx)}")
        return None

    client = _client(ctx, settings)
    started = time.perf_counter()
    result = _within(
        settings["timeout"],
        lambda: suggest(
            client,
            text,
            skills,
            shortlist=settings["shortlist"],
            excerpt=settings["excerpt"],
            gate_threshold=settings["gate"],
            fits_threshold=settings["fits"],
            fits_margin=settings["fits_margin"],
            chunk=settings["chunk"],
            strategy=settings["backend"],
        ),
    )
    elapsed = time.perf_counter() - started
    _log(
        path,
        "suggest",
        f"{result.skill or '-'} gate={result.gate:.4g} {elapsed:.3f}s "
        f"cached={result.usage.cached_calls}",
    )
    return {"context": result.block()} if result.names else None


def pre_llm_call(user_message: Any = None, **kwargs: Any) -> Optional[dict]:
    """Hook body bound to *ctx* by :func:`register`."""
    ctx = kwargs.get("_typesafe_ctx")
    try:
        return route(ctx, user_message)
    except Exception as exc:  # a router failure must never break the turn
        try:
            _log(log_path(ctx), "error", f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        return None


# ─── CLI ─────────────────────────────────────────────────────────────────────
def _cli_setup(ctx: Any):
    def setup(parser) -> None:
        sub = parser.add_subparsers(dest="action", metavar="ACTION")
        sub.add_parser("on", help="turn routing on (uses the configured backend)")
        sub.add_parser("off", help="turn routing off")
        sub.add_parser("status", help="settings, roster, cache, key")
        one = sub.add_parser("suggest", help="route one request now")
        one.add_argument("text", help="the request to route")
        one.add_argument("--json", action="store_true", help="compact machine-readable output")
        check = sub.add_parser("check", help="self-check without spending inference")
        check.add_argument("--live", action="store_true", help="also spend one tiny live request")

    return setup


def _cli_handler(ctx: Any):
    def handler(args) -> int:
        action = getattr(args, "action", None) or "status"
        path = log_path(ctx)

        if action in {"on", "off"}:
            ctx.set_config("enabled", action == "on")
            print(f"{PLUGIN_NAME}: {action}")
            if action == "on":
                print(
                    "note: if this plugin was installed while Hermes was already running, restart\n"
                    "      that process (hermes gateway restart / the desktop backend) so the\n"
                    "      pre_llm_call hook is loaded — settings changes alone do not wire hooks."
                )
            return 0

        if action == "status":
            settings = _settings(ctx)
            skills = _roster(ctx)
            info = stats(skills) if skills else {"skills": 0, "categories": 0}
            cache = JsonCache(cache_path(ctx))
            endpoint = (
                settings["base_url"]
                or ("http://127.0.0.1:8080" if settings["backend"] == "laya"
                    else "https://api.typesafe.ai")
            )
            print(f"{PLUGIN_NAME} {PLUGIN_VERSION}")
            print(f"  enabled   : {settings['enabled']}  ({'on' if settings['enabled'] else 'off'})")
            print(f"  backend   : {settings['backend']}")
            print(f"  thresholds: gate {settings['gate']:.2f} / fits {settings['fits']:.2f} "
                  f"/ margin {settings['fits_margin']:.2f} / shortlist {settings['shortlist']}")
            print(f"  roster    : {roster_dir(ctx)} — {info['skills']} skills, "
                  f"{info['categories']} categories")
            print(f"  model     : {_client(ctx, settings).model} @ {endpoint}")
            key_label = "optional (LAYA_API_KEY)" if settings["backend"] == "laya" else (
                "present" if api_key_present(settings) else "MISSING (TYPESAFE_API_KEY)"
            )
            print(f"  api key   : {key_label}")
            print(f"  cache     : {cache_path(ctx)} — {len(cache)} cached answers")
            print(f"  log       : {path}")
            print(f"  budget    : {settings['timeout']:.0f}s per turn, "
                  f"skips messages over {settings['suggest_chars']} chars")
            return 0

        if action == "suggest":
            text = args.text
            skills = _roster(ctx)
            settings = _settings(ctx)
            client = _client(ctx, settings)
            result = suggest(
                client, text, skills,
                shortlist=settings["shortlist"], excerpt=settings["excerpt"],
                gate_threshold=settings["gate"], fits_threshold=settings["fits"],
                fits_margin=settings["fits_margin"], chunk=settings["chunk"],
                strategy=settings["backend"],
            )
            payload = result.as_dict()
            print(json.dumps(payload, separators=(",", ":")) if args.json
                  else json.dumps(payload, indent=2))
            return 0 if result.names else 1

        if action == "check":
            problems = []
            settings = _settings(ctx)
            key = api_key_present(settings)
            if not key:
                problems.append("TYPESAFE_API_KEY is not set (<hermes home>/.env)")
            skills = _roster(ctx)
            if not skills:
                problems.append(f"no skills found under {roster_dir(ctx)}")
            try:
                cache_path(ctx).parent.mkdir(parents=True, exist_ok=True)
                (cache_path(ctx).parent / ".write-test").touch()
                (cache_path(ctx).parent / ".write-test").unlink()
            except OSError as exc:
                problems.append(f"cache dir is not writable: {exc}")
            print(f"  roster  : {len(skills)} skills under {roster_dir(ctx)}")
            print(f"  backend : {settings['backend']}")
            print(f"  api key : {'optional' if settings['backend'] == 'laya' else ('present' if key else 'MISSING')}")
            print(f"  cache   : {cache_path(ctx)}")
            print(f"  log     : {log_path(ctx)}")
            if getattr(args, "live", False):
                load_env_file()
                probe = _client(ctx, settings).probe()
                print(f"  live    : model {probe['model']} answered probe "
                      f"{probe['answer']:.3f} ({probe['usage']['cost_usd']} USD)")
            for problem in problems:
                print(f"  problem : {problem}")
            print("check: ok" if not problems else "check: problems above")
            return 0 if not problems else 1

        print(f"{PLUGIN_NAME}: unknown action {action!r}")
        return 2

    return handler


def register(ctx) -> None:
    """Wire the hook and the CLI. Nothing here touches the network or the filesystem."""
    def handler(**kwargs: Any) -> Optional[dict]:
        return pre_llm_call(_typesafe_ctx=ctx, **kwargs)

    ctx.register_hook("pre_llm_call", handler)
    ctx.register_cli_command(
        PLUGIN_NAME,
        "TypeSafe (Jev) skill routing: on/off/status/suggest/check",
        _cli_setup(ctx),
        _cli_handler(ctx),
        description="Route each request to the one skill in your live roster that fits it.",
    )
