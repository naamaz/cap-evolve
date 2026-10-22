---
name: dry-run
description: Inspect the system under optimization before any eval budget is spent. Use after implement-and-check and before baseline, only when the spec sets dry_run true. Reads the whole system (capability, agent and runner code, orchestration, scorer), reports major flow flaws and obvious bugs, fixes on its own what the optimizer is allowed to edit, asks the user before changing anything else, and optionally smoke-checks the fixes on a few train cases. Not a code review, not a linter, and not a replacement for diagnose, which works from traces.
component: phase
argument-hint: "prepare|apply|decide|auto --project DIR [--capability DIR] [--n-trials N] [--unattended]"
allowed-tools: Read, Write, Bash
provides: [dry_run_findings]
needs: [project, checked]
sources: []
---

# dry-run — read the system before you measure it

The loop learns only from failed traces. It sees *that* tasks fail and guesses *why*. Some
flaws are in the flow itself, and traces show them poorly: the agent scores its own answer,
gets the top score, and still runs a "fix" step that can damage a correct answer and always
costs tokens. In traces that is noise spread over many tasks; in the code it is one `if`
that is missing. Find those before baseline, while no budget is spent.

## Contents
- The steps
- What to look for
- What is out of scope
- Writing findings.json
- Who approves a fix
- Checking the fixes
- Unattended runs

## The steps
```
python scripts/run.py prepare --project P --capability CAP [--base .capevolve --seed N --ratios a,b,c]
#   -> creates the run dir, freezes the split once, snapshots the original seed,
#      writes P/dry_run/context.json (the roots to read from, the editable surface,
#      the train ids the targeted check may use)
#   YOU: read the system, write P/dry_run/findings.json
python scripts/run.py apply --project P --n-trials N
#   exit 0 ready for baseline | 2 waiting for a human | 3 BLOCKED (unattended)
python scripts/run.py decide --project P --id F3 --choice fix|as_is|keep|revert
```
Then run baseline with `--dry-run-state P/dry_run/state.json`. `cap-evolve run` does all of
this itself when `dry_run: true` (subcommand `auto`).

## What to look for
Start at `context.json` → `roots` (the capability, the adapter, `runner_repo_path`,
`capability_sources`) and follow imports and calls from there. Read the **whole system**,
not only what cap-evolve may edit.

1. **Flow issues — the main target.** Trace the process end to end: steps, branches,
   loops, the conditions between them, and what each step does with the previous step's
   output.
   - *Ignored signals:* a score, check or verdict is computed and never changes what
     happens next (the motivating example).
   - *Missing exits:* a loop or retry with no stop-on-success, or no cap.
   - *Dead or unreachable paths.*
   - *Contradictions:* the prompt or a skill says one thing, the code enforces another.
   - *Wasted work:* a step whose output nothing reads.
2. **Bugs — obvious ones only.** A defect any reviewer accepts on sight: a wrong variable,
   an inverted condition, a swallowed exception that hides failures, an off-by-one.

## What is out of scope
Style, refactors, speculative issues and "could be better". **If a finding needs an
argument to justify it, it is not obvious** — drop it, or write it with no fix so it
becomes a hint. A list of twenty findings trains the user to click past the two real ones.

- **Good:** `F1 flow major agent/loop.py:88 — when evaluate() returns 10/10 the fixer still
  runs and rewrites the answer; fix: return early when score == max`.
- **Good:** `F2 bug major runner/retry.py:41 — except Exception: pass swallows the timeout,
  so a hung task is scored as an empty answer`.
- **Bad:** "the prompt could be more concise", "consider extracting this helper",
  "this might fail on unicode input".

## Writing findings.json
```json
{"findings": [{
  "id": "F1", "kind": "flow", "severity": "major",
  "location": ["agent/loop.py:88"],
  "what_happens": "when the evaluator returns 10/10, the fixer still runs and rewrites the answer",
  "touches_scorer": false,
  "proposed_fix": {"summary": "stop when the evaluation is the top score",
                   "edits": [{"file": "agent/loop.py", "old": "    answer = fix(answer)\n",
                              "new": "    if score < MAX:\n        answer = fix(answer)\n"}]},
  "verify_cases": [{"task_id": "t17", "reason": "evaluator scores this one 10/10"}],
  "decision": "pending"}],
 "controls": [{"task_id": "t03", "reason": "never reaches the evaluator"}]}
```
- An edit is an exact string replacement: `old` must occur **exactly once** in `file`.
  Relative paths resolve against the capability dir, then the project. Leave
  `proposed_fix.edits` empty when there is no clear fix — the finding becomes a hint.
- `editable` is **computed by the script** (inside the capability dir and not in
  `protected_paths`), never taken from you. Set `touches_scorer` for any scorer, metric or
  gating change: such a finding is never editable.
- `verify_cases`: up to `verify_cases_per_fix` train tasks whose run goes **through** the
  changed code, each with a one-line reason. Pick only from `verify_candidate_ids` (train).
  `controls`: 2 train tasks the fixes should not affect.

## Who approves a fix
The same boundary the loop enforces: inside what the optimizer may edit, no human is needed.

- **Editable** (the capability's prompts, tools, skills): applied **automatically**, to a
  copy — `dry_run/fixed_seed/`. The user's seed dir is never modified; baseline scores the
  copy. Show the user the automatic fixes once, as one list with their diffs.
- **Not editable** (agent/runner code, the adapter, the scorer, metrics, the task data or
  splits): ask. Show the finding and its exact diff (`dry_run/FINDINGS.md`) and ask **apply
  this fix, or continue as is?** One at a time, most severe first; once more than three are
  left, offer "fix all remaining / skip all remaining". Record each answer with `decide`. An
  approved fix is applied in place.
- **A scorer or metric fix carries a warning, every time you show it:** it changes *what is
  measured*, not the agent, so a before/after across it does not show the agent improved.
- **As is** (declined, or no clear fix): the finding goes to the optimizer as a hint —
  "you may fix this" when editable, "known system behavior: work with it" otherwise.

Never fix silently: every applied fix is in `FINDINGS.md` and the report with its diff and
who approved it (`auto` or `user`), so the loop's gain is never confused with the dry run's.

## Checking the fixes
`dry_run_verify` (asked at intake):
- `targeted` (default): `apply` runs the chosen train cases + controls **before** the fixes,
  applies them all, runs them **after** (same `n_trials`), and judges each fix on its own
  cases: `helped` / `neutral` / `harmed`. A harmed case shared by several fixes is re-run with
  each fix alone to find the culprit. A harmed **editable** fix is reverted automatically; a
  harmed non-editable fix needs the user: show before/after, `decide --choice keep|revert`,
  then `apply` again. This is a **smoke check**, not a measured gain — say so. Val and test
  are never used; under `split_mode: pooled` it does not run (use `full`).
- `full`: baseline scores the original seed and the fixed seed on val, same split and
  trials, and records `baseline_pre_dry_run`. Capability fixes only.
- `off`: the fixes go in unmeasured.

Scorer fixes get no harm verdict: before and after are graded by different scorers. Their
score change is reported as a "measurement change".

## Unattended runs
`cap-evolve run` (subcommand `auto`) starts the configured optimizer CLI with this file as
its brief, unless a `findings.json` from an interactive session for the same seed exists with
no `pending` decision — then it is reused. Its cost is optimizer spend. Editable fixes apply as
usual; non-editable ones are **never** applied. With `dry_run_unattended: stop` (default) a
**major** non-editable finding stops the run: `BLOCKED: dry-run found N major issue(s)
needing approval`. Minor ones never block. With `continue` they all become hints.
On `--resume` the dry run does not run again once it finished for that run.

## References
- `references/concepts.md` — why flow flaws hide in traces, why dry-run fixes are recorded
  apart from the loop's gain, and why scorer fixes are flagged.
