"""Shared fixtures: a scripted client and a stub plugin context.

The client answers from a plan instead of the network, but returns *real* ``Response``
objects, so the tests exercise the same parsing, tie-breaking and threshold code the plugin
runs live.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from typesafe_router.client import Answer, Response, Usage  # noqa: E402
from typesafe_router.roster import Skill  # noqa: E402

NONE = "none_of_these"


# ─── scripted client ─────────────────────────────────────────────────────────
class FakeClient:
    """Records every payload it is asked and answers with the plan's Response."""

    def __init__(self, plan: Callable[[Dict[str, Any]], Response]):
        self.plan = plan
        self.calls: List[Dict[str, Any]] = []
        self.usage = Usage()

    def build_payload(self, state: Any, questions: Dict[str, dict], *, model: Optional[str] = None) -> Dict[str, Any]:
        return {"state": state, "model": model or "jev-latest", "questions": dict(questions)}

    def ask(self, state: Any, questions: Dict[str, dict], **kwargs: Any) -> Response:
        payload = self.build_payload(state, questions)
        self.calls.append(payload)
        return self.plan(payload)

    @property
    def stage1_calls(self) -> List[Dict[str, Any]]:
        """Wide-ranking payloads — one per chunk when the roster is over the choice cap.

        Stage is told by the per-candidate questions, never by the question *names* or the
        state: with a chunked roster the gate Nouls ride the first chunk only, so chunk 2
        carries neither the gate judgments nor anything else stage-specific.
        """
        return [c for c in self.calls if not any(k.startswith("fits::") for k in c["questions"])]

    @property
    def stage2_calls(self) -> List[Dict[str, Any]]:
        """Shortlist payloads: one ``fits::<name>`` judgment per candidate."""
        return [c for c in self.calls if any(k.startswith("fits::") for k in c["questions"])]


def _answer(kind: str, key: str, raw: Dict[str, Any]) -> Answer:
    return Answer(id=key, kind=kind, raw=raw)


def stage1_plan(winner: Optional[str], gate=(0.95, 0.56, 0.27), none_prob: float = 0.0, tie_to: float = 0.0):
    """Answer a stage-1 payload: one Choice over the chunk plus the three gate Nouls."""

    def plan(payload: Dict[str, Any]) -> Response:
        questions = payload["questions"]
        criteria = list(questions["which"]["criteria"])
        probabilities = {name: tie_to for name in criteria}
        if winner in probabilities:
            probabilities[winner] = 1.0 - none_prob
        if NONE in probabilities:
            probabilities[NONE] = none_prob
        answers = {"which": _answer("choice", "which", {
            "choice": winner, "probabilities": probabilities, "confidence": 0.8,
        })}
        gate_keys = [k for k in questions if k.startswith("gate::")]
        for index, key in enumerate(gate_keys):
            answers[key] = _answer("noul", key, {"noul": gate[index]})
        return Response(model="jev-latest", answers=answers,
                        usage=Usage(calls=1, input_tokens=900, output_tokens=400))

    return plan


def stage2_plan(winner: Optional[str], fits: Dict[str, float]):
    """Answer a stage-2 payload: one Choice over the shortlist plus one Noul per candidate."""

    def plan(payload: Dict[str, Any]) -> Response:
        questions = payload["questions"]
        criteria = list(questions["which"]["criteria"])
        probabilities = {name: 0.0 for name in criteria}
        if winner in probabilities:
            probabilities[winner] = 1.0
        answers = {"which": _answer("choice", "which", {
            "choice": winner, "probabilities": probabilities, "confidence": 0.7,
        })}
        for key in (k for k in questions if k.startswith("fits::")):
            name = key.split("::", 1)[1]
            answers[key] = _answer("noul", key, {"noul": fits.get(name, 0.0)})
        return Response(model="jev-latest", answers=answers,
                        usage=Usage(calls=1, input_tokens=600, output_tokens=300))

    return plan


def scripted_client(*, stage1: Optional[str] = None, gate=(0.95, 0.56, 0.27),
                    none_prob: float = 0.0, tie_to: float = 0.0,
                    stage2: Optional[str] = None, fits: Optional[Dict[str, float]] = None,
                    none_prob_for: Optional[Callable[[List[str]], float]] = None) -> FakeClient:
    """A client that answers stage 1 then stage 2 as scripted.

    ``none_prob_for(criteria)`` lets one chunk declare "nothing fits" while another nominates.
    """
    stage2_answer = stage2_plan(stage2, fits or {})
    if none_prob_for is None and stage1 is not None:
        # Model the measured behaviour of an irrelevant chunk: the chunk holding the fitting
        # skill nominates it, the others say "none of these" (62% of them scored >= 0.50).
        none_prob_for = lambda criteria: 0.0 if stage1 in criteria else 0.9

    def plan(payload: Dict[str, Any]) -> Response:
        if any(k.startswith("fits::") for k in payload["questions"]):
            return stage2_answer(payload)
        chunk_none = none_prob if none_prob_for is None else none_prob_for(
            list(payload["questions"]["which"]["criteria"])
        )
        return stage1_plan(stage1, gate, chunk_none, tie_to)(payload)

    return FakeClient(plan)


# ─── roster helpers ──────────────────────────────────────────────────────────
def skill(name: str, category: str = "testing", description: Optional[str] = None,
          excerpt: str = "How to do the thing.") -> Skill:
    text = description or f"{name} handles {name.replace('-', ' ')} requests."
    index = text if len(text) <= 60 else text[:57] + "..."
    return Skill(name=name, category=category, description=text, index_description=index,
                 excerpt=excerpt, path=f"/tmp/skills/{category}/{name}/SKILL.md")


def roster(count: int, prefix: str = "skill") -> List[Skill]:
    return [skill(f"{prefix}-{i:03d}", category=f"cat{i % 3}") for i in range(count)]


# ─── plugin module + stub context ────────────────────────────────────────────


class StubContext:
    """The subset of ``PluginContext`` the plugin uses, with the same defaults semantics.

    Mirrors the admission probe: ``get_config`` hands back the caller's default when the
    key is absent, and nothing is written to disk.
    """

    plugin_config: Dict[str, Any] = {}
    profile_name = "default"
    plugin_id = "typesafe_skill_router_test"

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = dict(settings or {})
        self.hooks: Dict[str, Callable] = {}
        self.commands: Dict[str, Any] = {}

    RESERVED_ROOTS = frozenset({"model", "plugins", "security", "settings"})

    def _check_key(self, key: str) -> None:
        if not key or key.split(".")[0].lower() in self.RESERVED_ROOTS:
            raise ValueError(f"Rejected config path {key!r}")

    def get_config(self, key: str, default: Any = None) -> Any:
        self._check_key(key)
        return self.settings.get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        self._check_key(key)
        self.settings[key] = value

    def register_hook(self, name: str, callback: Callable) -> None:
        self.hooks[name] = callback

    def register_cli_command(
        self, name: str, help: str, setup_fn, handler_fn=None, description: str = ""
    ) -> None:
        self.commands[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}


_MODULE = None


def plugin_module():
    """Import the plugin the way Hermes does: as a package directory, not a library."""
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    spec = importlib.util.spec_from_file_location(
        "typesafe_skill_router_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    # Hermes' own loader sets both of these (hermes_cli/plugins_loader.py): without
    # ``__package__`` the plugin's relative imports fail, exactly as the probe would.
    module.__package__ = spec.name
    module.__path__ = [str(ROOT)]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _MODULE = module
    return module
