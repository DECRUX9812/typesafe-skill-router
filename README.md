# TypeSafe skill router

A Hermes Agent plugin that names **the one skill worth loading** — before the model call.

Hermes shows the model a one-line description of every installed skill. With a few hundred of
them, the model reads past the one that would have done the job. This plugin sends the request
to [TypeSafe](https://typesafe.ai) (model `jev-latest`) first and, when a skill genuinely fits,
appends a single line to the **user message**:

```
<skill_relevance>
Relevant to the current request: gmail-inbox-cleanup. Ignore this if it does not fit what the
user actually asked for.
</skill_relevance>
```

It is **opt-in** (`enabled: false` by default), injects **nothing** when nothing fits, and every
failure path — no API key, no roster, timeout, transport error — logs one line and returns
`None`. A routing problem can never break a turn.

## Requirements

- Hermes Agent `>= 0.21`
- A TypeSafe API key (`TYPESAFE_API_KEY`) — create one at <https://console.typesafe.ai/settings/keys>
- Python 3.10+. Standard library only: no dependencies to install.

## Install

From the catalog:

```bash
hermes plugins install typesafe-skill-router   # then follow the prompt to enable it
```

Or straight from this repo:

```bash
git clone https://github.com/DECRUX9812/typesafe-skill-router ~/.hermes/plugins/typesafe-skill-router
hermes plugins enable typesafe-skill-router
hermes typesafe-skill-router on
```

Put the key where Hermes keeps its other secrets:

```bash
echo 'TYPESAFE_API_KEY=ts_...' >> ~/.hermes/.env     # chmod 600
```

If Hermes was already running when you installed the plugin, restart that process once so the
hook is loaded (`hermes gateway restart`, or the service that runs your chat backend). Switching
it on and off afterwards takes effect immediately.

## Settings

Under `plugins.entries.typesafe-skill-router.settings` in `config.yaml` (all optional):

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Master switch. Nothing is sent anywhere until this is true. |
| `gate` | `0.30` | Mean of the three request judgments. Below it, nothing is suggested and no second request is spent. |
| `fits` | `0.40` | The winner's own "does this skill do the specific thing asked for" judgment. Below it, nothing is injected. |
| `fits_margin` | `0.15` | When `fits` prefers a different candidate than the `Choice` winner, it takes over only by leading the winner's own `fits` by this much, clearing `fits` itself, and facing a winner that also clears `fits`. |
| `shortlist` | `3` | Candidates carried into the second request. |
| `chunk` | `240` | Skills per request. The API caps a question at 255 options. |
| `excerpt` | `700` | Characters of `SKILL.md` shown per shortlisted candidate. |
| `timeout` | `10.0` | Wall-clock budget for one routing decision. |
| `suggest_chars` | `4000` | Requests longer than this are left alone. |
| `router_model` / `base_url` | `jev-latest` / TypeSafe API | Endpoint overrides (`model` is reserved by Hermes for core settings). |
| `roster_dir` | `<hermes home>/skills` | Where the roster is read from. |
| `cache_path` | `<hermes home>/cache/typesafe-skill-router.json` | Answers are cached outside the buildable plugin tree so cache writes do not invalidate Hermes's dependency stamp. |
| `log_path` | `<hermes home>/logs/typesafe-skill-router.log` | One line per routed turn. |

When upgrading, rename a configured `model` setting to `router_model`; Hermes reserves the
plugin-relative `model` root, so older versions could not read it. The default cache path
also changes without automatically migrating old answers. To keep cached answers (and
avoid paying for those requests again), stop active router processes and copy the old
`<hermes home>/plugins/typesafe-skill-router/cache.json` to the new default path before
restarting. Do not keep using the old path as `cache_path` on a buildable plugin: cache
writes there invalidate Hermes's dependency stamp and can trigger repeated rebuilds.

Thresholds live in code, and the answers are cached, so re-tuning them costs nothing: change a
number, replay the same requests, compare. Slash commands are never routed, and multi-part
(multimodal) messages are left alone.

One caveat on tuning: thresholds measured on English requests sit differently elsewhere. An
independent check found the same request scoring ~0.14 lower on `fits` in Spanish than in
English — at `0.40`, the difference between a suggestion and silence.

## What leaves your machine

Routing is a network call to a third-party service, so here is the whole of it:

- **Sent:** the request text of the current turn, the names and one-line descriptions of your
  installed skills (in chunks of `chunk`), plus — for the top few candidates only — their full
  description and the first `excerpt` characters of `SKILL.md`.
- **Never sent:** conversation history, files, memory, credentials, tool output, the system
  prompt, or anything from previous turns. `recent_context` is sent empty.
- **Kept locally:** the roster scan, the thresholds, the shortlist, the cache, the log.

Turn it off at any time with `hermes typesafe-skill-router off` (or `enabled: false`).

## Cost and latency

Measured on one machine with a 292-skill roster: **~0.6–1.2 s per routed turn**, **~$0.001 per
request**, 3 API calls (2 chunks + the shortlist). Output tokens are not negligible — a
240-option `Choice` returns a 240-number distribution, so budget for them. A second,
independent measurement on a 131-skill roster (single chunk, n=12) came in lower on cost and
higher on latency: **~$0.0002 per routed turn, p50 1.93 s, max 8.56 s**. Latency is the real
budget item — it lands on every routed turn.

The vendor publishes an agent-level effect for the same idea (315 graded turns, block placed in
the system prompt): wrong skill loads 16.8% → 7.3%, needless loads 9.8% → 4.0%, 37 turns fixed
against 7 broken. Treat that as direction from *their* roster, not as a result from yours.

## Observed in live use

Not a benchmark — the raw log lines from one operator's first few turns on a 292-skill roster
(Ubuntu 24.04, desktop + CLI). `-` means nothing was injected.

```
23:03:36  suggest  -                             gate=0.6067  <- silent on a request whose fitting skill was hermes-plugin-development (a miss)
23:26:28  suggest  -                             gate=0.2967  <- silent on a conversational turn (correctly quiet)
23:35:34  suggest  tldr-communication            gate=0.3667  <- "explain in simple terms" (correct)
23:49:36  suggest  hermes-agent-skill-authoring  gate=0.5100  <- off-target: that turn needed no skill authoring
```

Four turns is an anecdote, not a rate, and that is the point: both failure modes this two-stage
design exists to control — a wrong suggestion and a missed one — showed up inside the first few
turns, while the gate stayed quiet on the turn that needed nothing. The off-target line is also
the design working as intended: the injected block says *ignore this if it does not fit*, and it
was ignored.

That is why the plugin logs one line per decision. The log is half of a scorecard (what was
suggested); the session store is the other half (what the turn actually loaded), so a report pass
can score real turns before anyone tunes a threshold — rather than tuning on vibes.

## How it works

Two requests, thresholds in code:

1. **Wide.** One `Choice` per chunk of `chunk` skills over the same 60-character index lines the
   model sees, each chunk also offering `none_of_these`; plus three `Noul` judgments on the
   request (does it act on the user's own material / does it follow a documented procedure /
   would prose alone satisfy it). The gate is the mean of those three, with the third inverted.
2. **Narrow.** One `Choice` over the top `shortlist` plus `none_of_these`, and one `Noul` per
   candidate: *does this skill do the specific thing the request asks for?*

The suggestion is the `Choice` winner only when it is also the best-fitting candidate (a tie
counts). When `fits` prefers a different candidate, that one is suggested instead — but only
if it clears `fits` itself, leads the winner's own `fits` by `fits_margin`, *and* the winner
also clears `fits`: an override resolves a disagreement between two signals that each found
a fitting skill, and a winner below the bar means stage two simply found no fit. Any other
disagreement is silence: both signals were paid for in the same request, and a wrong name
costs more than no name.

A chunk cut off by `P(none_of_these) >= 0.50` nominates nobody, except the best chunk, so a
shortlist is never empty when something fits. Candidates are ranked by `(-probability, name)`,
which keeps replays and cache keys bit-stable when the probabilities tie at `0.0`. Stages are
told apart by the payload state, never by question names, because with a chunked roster the gate
judgments ride the first chunk only.

## Commands

```
hermes typesafe-skill-router on        # enable routing
hermes typesafe-skill-router off       # stop (the hook stays loaded, it just stays quiet)
hermes typesafe-skill-router status    # settings, roster, cache, key presence
hermes typesafe-skill-router suggest "queue the new Burial record on the kitchen speaker"
hermes typesafe-skill-router check [--live]
```

## Development

```bash
python -m pytest              # offline: no key, no network, no writes to your real log
hermes plugins validate .     # the same gate catalog admission runs
```

The tests drive the real router with a scripted client that returns real `Response` objects, so
chunk filtering, tie-breaking and the thresholds are exercised exactly as they run live.

MIT — see `LICENSE`.
