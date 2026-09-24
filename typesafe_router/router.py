"""Two-stage skill router — the "skill suggestion" cookbook, pointed at this machine's roster.

Request 1 ranks the whole roster with one `Choice`, and scores the request with three
`Noul`s asking whether an action is wanted at all. Request 2 puts the same `Choice` to the
top three with each candidate's own SKILL.md text, plus one absolute `fits` `Noul` per
candidate. Two requests, two thresholds, at most one skill name back.

Question text, state shape, and both thresholds are copied from
https://docs.typesafe.ai/cookbooks/skill_suggestion.md so a change in the published recipe
can be diffed against this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .client import Response, SystemOneClient, Usage
from .primitives import choice, noul
from .roster import Skill, shortlist_criteria

SHORTLIST = 3  # candidates carried from the first request into the second
EXCERPT_CHARS = 700  # SKILL.md characters each candidate brings
GATE_THRESHOLD = 0.30  # mean of the three request nouls, below which nothing is suggested
FITS_THRESHOLD = 0.40  # winner's own "does this fit" noul, below which nothing is suggested
FITS_MARGIN = 0.15  # lead the best fits needs over the Choice winner's own to override it
# The cookbook ships 0.40 for neither threshold; it starts both at 0.30 and says to evaluate on
# your own data. Measured here (`tfl sweep`, 50 labelled requests, cache-only): gate 0.30 / fits
# 0.30 gives 88.5% top-1 and 12.5% needless suggestions; moving fits to 0.40 keeps top-1 at
# 88.5% while needless drops to 8.3% with no covered request lost (missed 0.0%). At fits 0.50
# the first legitimate suggestion starts being dropped (missed 3.9%), and any gate above 0.30
# only loses hits. So fits moved; gate stayed where the cookbook put it.
#
# The margin exists because the Choice and the fits nouls can disagree: the Choice picks a
# sibling while fits clearly prefers another (measured live: winner at 0.53-0.54 while the
# right skill sat at 0.68-0.85 — see issue #1). Deciding by max(fits) alone fixes those and
# introduces its own miss (an unrelated skill at 0.72 turning a correct silence into a wrong
# name), so the winner is only overridden when the fits leader clears the bar *and* leads by
# this margin *over a winner that also clears the bar*; a smaller gap is a coin flip between
# two signals, a wrong name costs more than silence, and a lead over a sub-threshold winner
# is just the largest number in a weak set — measured live at 0.73 over a 0.32 winner, a
# false positive no margin separates from the real 0.16-0.37 gaps. 0.15 sits under the
# measured real gaps without treating noise as a verdict.

# The API refuses a question with more than 255 choices ("Too many choices. Must have at most
# 255 choices."), discovered by running a 292-skill roster at it. The published cookbook's 182
# skills stayed under the cap, so one Choice over the whole roster is not an option here.
MAX_CHOICES = 255
CHUNK_CHOICES = 240
LAYA_CHUNK_CHOICES = 19  # laya.cpp recommends <=20 options; one slot is none_of_these
NONE_OPTION = "none_of_these"  # per-chunk no-match outcome, only when the roster is chunked
NONE_THRESHOLD = 0.50  # a chunk whose P(none) is at least this nominates no candidates

CHOICE_INSTRUCTIONS = (
    "Which of these skills, if any, is the right one to load to help with the "
    "user's latest request?"
)
GATE_QUESTIONS = {
    "acts_on_user_system": (
        "Is the assistant being asked to act on the user's files, accounts, devices, "
        "or online services, rather than only to explain or advise?"
    ),
    "would_follow_documented_procedure": (
        "Would a careful expert answering this consult a specific documented procedure "
        "or set of commands, rather than answering from general understanding?"
    ),
    "prose_suffices": (
        "Could a knowledgeable generalist fully satisfy this request in prose, with "
        "no tools, no documentation, and no access to the user's files or accounts?"
    ),
}
INVERTED = {"prose_suffices"}  # a yes here points away from needing a skill

RERANK_INSTRUCTIONS = (
    "Exactly one of these skills is the right one to load for the user's latest "
    "request. Which one? Read what each actually does, not just its name."
)


def document(request: str, recent_context: str = "") -> dict[str, str]:
    """The state every question in this recipe is asked over."""
    return {"request": request, "recent_context": recent_context}


# -- request 1 ------------------------------------------------------------------
def chunk_roster(roster: Sequence[Skill], size: int = CHUNK_CHOICES) -> list[list[Skill]]:
    """Split the roster into question-sized chunks.

    The API rejects a question with more than 255 choices ("Too many choices. Must have at
    most 255 choices." - found live, not in the docs that were read), and this machine's
    roster is 292 skills, so a single Choice over everything is impossible here. Chunks stay
    under the cap with headroom for a few skills to be added.
    """
    if size > MAX_CHOICES:
        raise ValueError(f"chunk size {size} exceeds the API cap of {MAX_CHOICES} choices")
    items = list(roster)
    if not items:
        return [[]]
    return [items[i : i + size] for i in range(0, len(items), size)]


def rank_wide(
    client: SystemOneClient,
    request: str,
    roster: Sequence[Skill],
    *,
    recent_context: str = "",
    chunk: int = CHUNK_CHOICES,
    shortlist: int = SHORTLIST,
    workers: int = 4,
) -> dict[str, Any]:
    """Rank the whole roster. Over the 255-choice cap, ask in chunks and take each one's best.

    Chunked rosters need care in two places, both found by running this at a 292-skill roster:

    * A `Choice` with no right answer still returns a spread. A chunk that holds nothing
      relevant happily returns 0.27 / 0.22 / 0.19 for three unrelated skills, and its "top
      three" is then noise that stage 2 has to reject. So each chunk - when the roster is
      actually chunked - offers `none_of_these`, and a chunk only nominates candidates while
      its own `P(none_of_these) < NONE_THRESHOLD`. The best chunk always nominates, so a
      shortlist is never empty when something fits.
    * The losing chunks are mostly exact 0.0 (239 of 240 in one measured case), and ties are
      broken by dict order, which makes the pick arbitrary. Sorting by `(-probability, name)`
      keeps replays and cache keys bit-stable.

    Probabilities from different chunks are still not a global ranking; the winner is decided
    by request 2, which sees full descriptions.
    """
    groups = chunk_roster(roster, chunk)
    chunked = len(groups) > 1
    calls: list[dict[str, dict]] = []
    for index, group in enumerate(groups):
        criteria = {skill.name: skill.index_description for skill in group}
        if chunked:
            criteria[NONE_OPTION] = "None of these skills fit the request."
        questions: dict[str, dict] = {"which": choice(CHOICE_INSTRUCTIONS, criteria)}
        if index == 0:
            for key, text in GATE_QUESTIONS.items():
                questions[f"gate::{key}"] = noul(text)
        calls.append(questions)

    state = document(request, recent_context)
    if len(calls) == 1:
        responses = [client.ask(state, calls[0])]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(calls)))) as pool:
            responses = list(pool.map(lambda q: client.ask(state, q), calls))

    per_chunk: list[list[tuple[str, float]]] = []
    none_pressure: list[float] = []
    for response in responses:
        probabilities = response.answers["which"].probabilities
        ranked = sorted(
            ((name, prob) for name, prob in probabilities.items() if name != NONE_OPTION),
            key=lambda kv: (-kv[1], kv[0]),
        )
        per_chunk.append(ranked)
        none_pressure.append(float(probabilities.get(NONE_OPTION, 0.0)))

    first = responses[0]
    values = {
        key.removeprefix("gate::"): answer.noul
        for key, answer in first.answers.items()
        if key.startswith("gate::")
    }
    oriented = [(1.0 - v) if k in INVERTED else v for k, v in values.items()]

    best_chunk = max(range(len(per_chunk)), key=lambda i: per_chunk[i][0][1] if per_chunk[i] else -1.0)
    shortlisted: list[str] = []
    for index, ranked in enumerate(per_chunk):
        if index != best_chunk and none_pressure[index] >= NONE_THRESHOLD:
            continue  # this chunk says nothing here fits; its top names are noise
        for name, _ in ranked[:shortlist]:
            if name not in shortlisted:
                shortlisted.append(name)

    merged = [pair for ranked in per_chunk for pair in ranked]
    return {
        "ranked": sorted(merged, key=lambda kv: (-kv[1], kv[0])),
        "per_chunk": per_chunk,
        "none_pressure": none_pressure,
        "best_chunk": best_chunk,
        "shortlist": tuple(shortlisted),
        "chunks": len(groups),
        "gate": sum(oriented) / len(oriented),
        "gate_values": values,
        "choice_confidence": first.answers["which"].confidence,
        "response": first,
        "responses": responses,
        "payload": client.build_payload(state, calls[0]),
    }


def rank_laya(
    client: SystemOneClient,
    request: str,
    roster: Sequence[Skill],
    *,
    recent_context: str = "",
    chunk: int = LAYA_CHUNK_CHOICES,
    shortlist: int = SHORTLIST,
    workers: int = 4,
) -> dict[str, Any]:
    """Rank with a bounded tournament suited to laya.cpp's small-choice calibration.

    Laya becomes noticeably less accurate with high-cardinality choices, so every match
    contains at most 19 skills plus ``none_of_these``. Up to two candidates advance from
    each qualifying match until no more than ``shortlist`` remain. The gate Nouls ride only
    the first request, keeping every request at four questions or fewer.
    """
    # At least three skills ensures that advancing two candidates makes progress.
    effective_chunk = min(max(3, chunk), LAYA_CHUNK_CHOICES)
    target_shortlist = min(max(1, shortlist), SHORTLIST)
    state = document(request, recent_context)
    current = list(roster)
    all_responses: list[Response] = []
    all_ranked: list[list[tuple[str, float]]] = []
    all_none_pressure: list[float] = []
    first_questions: dict[str, dict] | None = None
    gate_values: dict[str, float] = {}

    while current:
        groups = chunk_roster(current, effective_chunk)
        calls: list[dict[str, dict]] = []
        for group in groups:
            criteria = {skill.name: skill.index_description for skill in group}
            criteria[NONE_OPTION] = "None of these skills fit the request."
            questions: dict[str, dict] = {"which": choice(CHOICE_INSTRUCTIONS, criteria)}
            if not all_responses and not calls:
                for key, text in GATE_QUESTIONS.items():
                    questions[f"gate::{key}"] = noul(text)
            calls.append(questions)
        if first_questions is None:
            first_questions = calls[0]

        if len(calls) == 1:
            responses = [client.ask(state, calls[0])]
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(calls)))) as pool:
                responses = list(pool.map(lambda q: client.ask(state, q), calls))

        round_ranked: list[list[tuple[str, float]]] = []
        round_none: list[float] = []
        for response in responses:
            probabilities = response.answers["which"].probabilities
            ranked = sorted(
                ((name, prob) for name, prob in probabilities.items() if name != NONE_OPTION),
                key=lambda kv: (-kv[1], kv[0]),
            )
            round_ranked.append(ranked)
            round_none.append(float(probabilities.get(NONE_OPTION, 0.0)))

        if not gate_values:
            gate_values = {
                key.removeprefix("gate::"): answer.noul
                for key, answer in responses[0].answers.items()
                if key.startswith("gate::")
            }

        all_responses.extend(responses)
        all_ranked.extend(round_ranked)
        all_none_pressure.extend(round_none)

        qualifying = [i for i, pressure in enumerate(round_none) if pressure < NONE_THRESHOLD]
        if not qualifying:
            qualifying = [
                max(
                    range(len(round_ranked)),
                    key=lambda i: round_ranked[i][0][1] if round_ranked[i] else -1.0,
                )
            ]

        advanced_names: list[str] = []
        for index in qualifying:
            for name, _ in round_ranked[index][:2]:
                if name not in advanced_names:
                    advanced_names.append(name)

        if len(advanced_names) <= target_shortlist:
            finalists = advanced_names
            break
        by_name = {skill.name: skill for skill in current}
        current = [by_name[name] for name in advanced_names]
    else:
        finalists = []

    oriented = [
        (1.0 - value) if key in INVERTED else value
        for key, value in gate_values.items()
    ]
    first = all_responses[0]
    return {
        "ranked": sorted(
            (pair for ranked in all_ranked for pair in ranked),
            key=lambda kv: (-kv[1], kv[0]),
        ),
        "per_chunk": all_ranked,
        "none_pressure": all_none_pressure,
        "best_chunk": 0,
        "shortlist": tuple(finalists[:target_shortlist]),
        "chunks": len(all_responses),
        "gate": sum(oriented) / len(oriented),
        "gate_values": gate_values,
        "choice_confidence": first.answers["which"].confidence,
        "response": first,
        "responses": all_responses,
        "payload": client.build_payload(state, first_questions or {}),
    }


# -- request 2 ------------------------------------------------------------------
def rerank_questions(names: Sequence[str], by_name: dict[str, Skill], excerpt: int = EXCERPT_CHARS) -> dict:
    criteria = shortlist_criteria([by_name[n] for n in names], excerpt)
    criteria[NONE_OPTION] = "None of these skills fit the request."
    questions: dict[str, dict] = {"which": choice(RERANK_INSTRUCTIONS, criteria)}
    for name in names:
        questions[f"fits::{name}"] = noul(
            f"Does the skill '{name}' do the specific thing the user's request asks "
            f"for? It is described as: {by_name[name].description}"
        )
    return questions


def rerank(
    client: SystemOneClient,
    request: str,
    names: Sequence[str],
    by_name: dict[str, Skill],
    *,
    excerpt: int = EXCERPT_CHARS,
    recent_context: str = "",
) -> dict[str, Any]:
    """The same Choice over a shortlist, plus one absolute noul per candidate."""
    questions = rerank_questions(names, by_name, excerpt)
    state = document(request, recent_context)
    response = client.ask(state, questions)
    picked = response.answers["which"].choice
    return {
        "winner": None if picked in (None, NONE_OPTION) else picked,
        "picked": picked,
        "fits": {
            key.removeprefix("fits::"): answer.noul
            for key, answer in response.answers.items()
            if key.startswith("fits::")
        },
        "confidence": response.answers["which"].confidence,
        "response": response,
        "payload": client.build_payload(state, questions),
    }


def suggestion_block(names: Sequence[str]) -> str:
    """What gets appended after the roster, in the suggestion.

    This string is a measured input rather than prose: it goes to the agent, so it is part
    of every graded turn's cache key. Editing a word here silently invalidates the shipped
    results and costs a live re-run to restore them. Text copied verbatim from the cookbook.
    """
    body = (
        f"Relevant to the current request: {', '.join(names)}. Ignore this if it does not "
        "fit what the user actually asked for."
        if names
        else "No skill in the roster appears relevant to this request."
    )
    return f"\n\n<skill_relevance>\n{body}\n</skill_relevance>"


# -- the whole recipe -----------------------------------------------------------
@dataclass
class Suggestion:
    """At most one skill name, plus the evidence that produced it."""

    names: tuple[str, ...]
    request: str
    gate: float = 0.0
    gate_values: dict[str, float] = field(default_factory=dict)
    shortlist: tuple[str, ...] = ()
    fits: dict[str, float] = field(default_factory=dict)
    winner: str | None = None
    reason: str = ""
    usage: Usage = field(default_factory=Usage)
    trace: dict[str, Any] = field(default_factory=dict)
    chunks: int = 1  # first-stage requests spent (roster over the 255-choice cap)

    @property
    def skill(self) -> str | None:
        return self.names[0] if self.names else None

    def block(self) -> str:
        return suggestion_block(self.names)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "suggestion": list(self.names),
            "skill": self.skill,
            "reason": self.reason,
            "gate": round(self.gate, 4),
            "gate_values": {k: round(v, 4) for k, v in self.gate_values.items()},
            "shortlist": list(self.shortlist),
            "fits": {k: round(v, 4) for k, v in self.fits.items()},
            "winner": self.winner,
            "chunks": self.chunks,
            "usage": self.usage.as_dict(),
            "block": self.block(),
            "trace": self.trace,
        }


def suggest(
    client: SystemOneClient,
    request: str,
    roster: Sequence[Skill],
    *,
    shortlist: int = SHORTLIST,
    excerpt: int = EXCERPT_CHARS,
    gate_threshold: float = GATE_THRESHOLD,
    fits_threshold: float = FITS_THRESHOLD,
    fits_margin: float = FITS_MARGIN,
    recent_context: str = "",
    chunk: int = CHUNK_CHOICES,
    workers: int = 4,
    strategy: str = "typesafe",
) -> Suggestion:
    """Two requests, two thresholds, at most one skill name."""
    by_name = {skill.name: skill for skill in roster}
    ranker = rank_laya if strategy == "laya" else rank_wide
    if strategy not in {"typesafe", "laya"}:
        raise ValueError("strategy must be one of: typesafe, laya")
    wide = ranker(
        client,
        request,
        roster,
        recent_context=recent_context,
        chunk=chunk,
        shortlist=shortlist,
        workers=workers,
    )
    usage = Usage()
    for response in wide["responses"]:
        usage.add(response.usage)

    result = Suggestion(
        names=(),
        request=request,
        gate=wide["gate"],
        gate_values=wide["gate_values"],
        usage=usage,
        trace={"stage1": _trace(wide)},
    )
    result.chunks = wide["chunks"]

    if wide["gate"] < gate_threshold:
        result.reason = f"gate {wide['gate']:.2f} < {gate_threshold:.2f}: no skill wanted"
        return result

    result.shortlist = wide["shortlist"][: max(shortlist, shortlist * wide["chunks"])]
    if not result.shortlist:
        result.reason = "roster empty"
        return result

    second = rerank(
        client, request, result.shortlist, by_name, excerpt=excerpt, recent_context=recent_context
    )
    usage.add(second["response"].usage)
    result.fits = second["fits"]
    result.winner = second["winner"]
    result.trace["stage2"] = _trace(second)

    if second["winner"] is None:
        result.reason = f"stage 2 picked {second['picked'] or 'nothing'}: no skill fits"
        return result

    # The Choice and the fits nouls are two judgments of the same question, and both were
    # paid for — use both. The winner is suggested when it is (or ties) the fits argmax.
    # When fits prefers another candidate, that candidate takes over only by clearing the
    # bar itself *and* leading the winner's own fits by fits_margin *and* facing a winner
    # that also clears the bar — an override resolves a disagreement between two signals
    # that each found a fitting skill, so a sub-threshold winner leaves nothing to resolve:
    # the leader is then the largest number in a set of weak scores, not a rival verdict.
    winner = second["winner"]
    winner_fits = second["fits"].get(winner, 0.0)
    ranked_fits = sorted(second["fits"].items(), key=lambda kv: (-kv[1], kv[0]))
    best_name, best_fits = ranked_fits[0] if ranked_fits else (winner, 0.0)

    if winner_fits >= best_fits:
        if winner_fits < fits_threshold:
            result.reason = (
                f"winner {winner} fits {winner_fits:.2f} < {fits_threshold:.2f}: nothing fits"
            )
            return result
        result.names = (winner,)
        result.reason = f"shortlist winner with fits {winner_fits:.2f}"
        return result

    if (
        best_fits >= fits_threshold
        and winner_fits >= fits_threshold
        and best_fits - winner_fits >= fits_margin
    ):
        result.names = (best_name,)
        result.reason = (
            f"fits override: {best_name} {best_fits:.2f} leads winner {winner} "
            f"{winner_fits:.2f} by {best_fits - winner_fits:.2f}"
        )
        return result

    if best_fits < fits_threshold:
        result.reason = (
            f"winner {winner} fits {winner_fits:.2f}, best candidate {best_name} "
            f"{best_fits:.2f} < {fits_threshold:.2f}: nothing fits"
        )
    elif winner_fits < fits_threshold:
        result.reason = (
            f"winner {winner} fits {winner_fits:.2f} < {fits_threshold:.2f} "
            f"(best candidate {best_name} {best_fits:.2f}): nothing fits"
        )
    else:
        result.reason = (
            f"choice picked {winner} (fits {winner_fits:.2f}) but {best_name} fits "
            f"{best_fits:.2f}: lead under the {fits_margin:.2f} margin, staying silent"
        )
    return result


def _trace(stage: dict[str, Any]) -> dict[str, Any]:
    response: Response = stage["response"]
    out = {
        "answers": {k: a.raw for k, a in response.answers.items()},
        "usage": response.usage.as_dict(),
        "cached": response.cached,
    }
    if "ranked" in stage:
        out["ranked"] = [[name, round(prob, 5)] for name, prob in stage["ranked"][:12]]
    if stage.get("per_chunk"):
        out["chunks"] = stage.get("chunks", len(stage["per_chunk"]))
        out["per_chunk_top"] = {
            f"chunk{i + 1}": [[n, round(p, 5)] for n, p in ranked[:3]]
            for i, ranked in enumerate(stage["per_chunk"])
        }
    responses = stage.get("responses") or [response]
    if len(responses) > 1:  # a chunked first stage spends more than one request
        total = Usage()
        for item in responses:
            total.add(item.usage)
        out["usage"] = total.as_dict()
    return out


# -- batch ----------------------------------------------------------------------
def suggest_many(
    client: SystemOneClient,
    requests: Sequence[str],
    roster: Sequence[Skill],
    *,
    workers: int = 8,
    **kwargs: Any,
) -> list[Suggestion]:
    """Score many requests. Each is independent, so a small pool is enough to go fast."""
    from concurrent.futures import ThreadPoolExecutor

    if len(requests) == 1 or workers <= 1:
        return [suggest(client, r, roster, **kwargs) for r in requests]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda r: suggest(client, r, roster, **kwargs), requests))
