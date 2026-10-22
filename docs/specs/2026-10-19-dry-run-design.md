# Dry run: a pre-baseline inspection of the system under optimization

**Date:** 2026-10-19
**Status:** IMPLEMENTED (`skills/phases/dry-run/`, `core/cap_evolve/dryrun.py`).
**Scope:** new phase skill `skills/phases/dry-run/`, plus changes to `intake` (questions and
spec keys), `baseline` (reusing the dry run's split, optional before/after scoring), `report` (a dry-run section),
`templates/project/capevolve.yaml`, `skills/phases/intake/inputs/INPUTS.md`,
`core/cap_evolve/cli.py` (run sequence), `skills/orchestrate/orchestrate/SKILL.md`, `llms.txt`,
`docs/AGENT_ORCHESTRATION.md` and the registry manifest.

## Why

cap-evolve currently learns only from failed eval traces. The loop sees *that* tasks fail and
infers *why* from trajectories. Some problems are flaws in the flow itself, and the traces
show them poorly. The motivating example: the agent's process evaluates its own output, and
even when that evaluation gives the highest score it still goes on to a "fix" step. The fix
step can damage a correct answer and always costs tokens. In traces this appears as noise and
wasted cost spread across many tasks. The optimizer may then spend iterations working around
the flaw in the prompt without ever identifying it.

Someone who reads the code could spot that kind of flaw in minutes, before any eval budget is
spent. The dry run is that reading: an inspection pass that runs **before baseline**, reports
the major flow issues and obvious bugs, fixes on its own whatever the optimizer is allowed to
edit anyway, and asks the user before changing anything else.

## What the stage is

A new **optional** phase, `dry-run`, placed in the pipeline after `implement-and-check` and
before `baseline`:

```
intake → implement-and-check → [dry-run] → baseline → <algorithm> → finalize → report
```

It runs after the check gate because by then the adapter is wired, so the stage knows what
"the system" is: the agent entrypoint, the runner, the capability artifacts and the scorer.
It runs before baseline because no eval budget has been spent at that point.

### What it reads

The **whole system**, not just the capabilities cap-evolve is allowed to edit. That covers the
capability artifacts (prompts, tools, skills), the agent and runner code they run inside, the
orchestration and control flow of the process, and the scorer. The search starts at what
`PROJECT.md` and `capevolve.yaml` already record (`runner_repo_path`, `capability_path`,
`capability_sources`, the adapter) and follows imports and calls from there.

### What it looks for

1. **Flow issues (the main target).** The stage traces the process end to end: steps,
   branches, loops, the conditions between them, and what each step does with the previous
   step's output. It reports:
   - *Ignored signals:* a score, check or verdict is computed but never changes what happens
     next. The motivating example belongs here.
   - *Missing exits:* loops or retries with no stop-on-success, or with no cap.
   - *Dead or unreachable paths.*
   - *Contradictions:* a prompt or skill says one thing and the code enforces another.
   - *Wasted work:* steps whose output nothing reads.
2. **Bugs, obvious ones only.** These are defects any reviewer would accept on sight: a wrong
   variable, an inverted condition, a swallowed exception that hides failures, an off-by-one
   on an index. **Style, refactoring suggestions, speculative issues and "could be better"
   findings are out of scope.** If a finding needs an argument to justify it, it is not
   obvious, and it goes as a hint (below), not a bug.

### Finding format

Each finding is written to `.capevolve/project/dry_run/findings.json` (and rendered into
`dry_run/FINDINGS.md`) with:

- `id`, `kind` (`flow` | `bug`), `severity` (`major` | `minor`), `location` (file:line, one or
  more)
- `what_happens`: the concrete runtime effect ("when the evaluator returns 10/10, the fixer
  still runs and rewrites the answer")
- `editable`: whether the location is inside a capability cap-evolve optimizes, or in code
  outside the optimizer's reach
- `touches_scorer`: true if the location is in the scorer
- `proposed_fix`: a short description plus a diff when one can be made
- `decision`: `fix` | `as_is` | `pending`, plus `approved_by`: `auto` | `user` (see below)

### Who approves a fix: depends on whether the optimizer may make that change

The rule is the same boundary cap-evolve already enforces in the loop. Inside what the
optimizer is allowed to edit, no human is needed. Outside it, a human must approve.

- **Inside the editable surface** (`editable: true`, meaning the prompts, tools and skills
  listed in `capevolve.yaml: capabilities`): the fix is **applied automatically, with no
  question asked**. The optimizer could make this same edit in the loop, so asking adds no
  safety. The fix is applied to a **copy** of the seed, `.capevolve/project/dry_run/fixed_seed/`.
  The user's original seed directory is never modified, and baseline is pointed at the fixed
  copy. *(Builder-check call: this keeps the original seed's bytes, and so `reuse_baseline` and
  `reused_seed_test`, valid, and gives the "before" score a seed to measure.)* Each automatic
  fix is still listed in `dry_run/FINDINGS.md` and the report with its diff, so nothing changes
  unseen.
- **Outside the editable surface** (`editable: false`): **a human must approve**. This includes:
  - the **scorer, evaluation metrics, or the gating metric**,
  - agent or runner code the optimizer cannot edit,
  - the task data or splits.

  The stage shows the finding and the exact diff and asks: **apply this fix, or continue as
  is?** An approved fix is applied in place and listed separately in the report, because it
  changes the system or the measurement, not the capability. **A scorer or metric fix carries an
  explicit warning:** it changes *what is measured*, not the agent, so a before/after baseline
  across it does not show that the agent improved.
- **Continue as is** (a declined non-editable fix, or any finding that has no clear fix): the
  finding is kept and passed to the optimizer as a hint (see "Unfixed findings become optimizer
  hints").

Every applied fix is recorded with its diff and with who approved it (`auto` or `user`), so the
report can say "the seed going into the loop already includes these N fixes" and the loop's
gain is never confused with the dry run's.

### Unattended runs

When nobody is there to ask (`cap-evolve run`, or the `orchestrate` skill in non-interactive
mode):

- **Who does the reading:** `cap-evolve run` is a deterministic sequencer with no agent in it, so
  the dry-run phase script starts the configured optimizer agent CLI (`optimizer_cmd`, for
  example `claude -p`) with the dry-run `SKILL.md` as its brief. The agent writes
  `findings.json`. Its cost counts toward the run's budget (`max_optimizer_usd`) and is recorded
  in the spend accounting like any optimizer call. If `findings.json` already exists from an
  interactive session and has no `pending` decisions, it is reused and no agent is started.
  *(My call, following from automatic fixes needing no human.)*
- **Editable fixes** are applied automatically, exactly as in an interactive run.
- **Non-editable fixes are never applied.** They need a human. The stage writes them to
  `PROJECT.md` and `dry_run/FINDINGS.md`, then acts on the `dry_run_unattended` spec key:
  - `stop` (default): exit non-zero with `BLOCKED: dry-run found N major issue(s) needing
    approval, see dry_run/FINDINGS.md` when at least one non-editable finding is `major`.
    `minor` ones never block and are treated as `as_is`. This matches the existing
    "blocked-but-honest" behavior. *(Builder-check call: without a severity threshold, any
    minor finding would block every unattended run.)*
  - `continue`: non-editable findings are treated as `as_is` and passed on as optimizer hints.

## Both parts are optional, decided at intake

Intake asks these as part of its existing single numbered batch, each with a default:

1. **Run the dry run?** Spec key `dry_run: true | false`. *Default `false`*, so existing
   projects and specs behave exactly as before.
2. **If yes, how should the fixes be checked?** Spec key `dry_run_verify: off | targeted | full`.
   - `targeted`: a small check on the few tasks each fix is relevant to (below).
   - `full`: score the whole val split before and after the fixes.
   - `off`: no check. The fixes go in unmeasured.

   *Default `targeted` when the dry run is on (my call: it is cheap and catches a harmful fix).*
   Intake states the eval cost of each option in the question.

A third key, `dry_run_unattended: stop | continue` (default `stop`), is asked only when the
run will be unattended. Two tuning keys take defaults without a question:
`dry_run_verify_cases_per_fix` (default `3`) and `dry_run_verify_max_cases` (default `12`).

Intake records the answers in `capevolve.yaml` and logs each default in `PROJECT.md` along
with what it costs, as it already does for every RECOMMENDED input. The keys are added to
`INPUTS.md` → RECOMMENDED and to `templates/project/capevolve.yaml`, which stays flat so the
zero-dependency reader can parse it.

If `dry_run: false`, the phase is not in the sequence at all. `cap-evolve run --plan-only` and
the orchestrate plan show it only when it is enabled.

## Verifying the fixes: the targeted check (`dry_run_verify: targeted`)

The question here is not "is the system better overall?". The optimization loop answers that,
with the significance gate. The question is narrower: **did each fix do what it was meant to,
without breaking the cases it touches?** A small, targeted sample answers that, so the check
is a sub-stage of the dry run instead of a second full baseline.

### Steps

1. **Pick the cases.** For each applied fix, the agent picks the tasks whose run actually goes
   through the changed code or instruction. In the motivating example, those are tasks where the
   evaluate step can return a top score. It picks up to `dry_run_verify_cases_per_fix` of them,
   with at most `dry_run_verify_max_cases` in total. It also adds **2 control tasks** that the
   fix should not affect, as a cheap guard against side effects. *(My call.)* Each pick is
   recorded in `findings.json` under that fix (`verify_cases`), with a one-line reason.
2. **Pick only from train.** The cases come from the **train** split, so val stays untouched
   for the gate and test stays sealed. If train has fewer relevant tasks than needed, the check
   uses what it has and says so. It never reaches into val or test. This means the split must
   exist before the check: the dry run freezes it, using the same seeded code and spec keys that
   baseline would use, and writes `splits.json` once. Baseline then reuses that split instead
   of making one.
3. **Run "before", then apply, then run "after".** The selected cases are run on the system as
   it was. Then all the fixes are applied: the editable ones to `fixed_seed/`, and the approved
   non-editable ones in place. Then the same cases are run again, with the same `n_trials`. Fixes
   are applied *after* the "before" run, so **system-code fixes are measured as well**, not only
   capability fixes.
4. **Judge each fix on its own cases.** For each fix, compare before and after on its cases:
   - `helped`: at least one of its cases improved and none got worse
   - `neutral`: no change
   - `harmed`: any of its cases, or a control, got worse

   When a harmed case belongs to more than one fix, that case is re-run with each of those
   fixes alone to find which one caused it.
5. **Act on a harmful fix.**
   - Editable fix: **reverted automatically** and turned into an optimizer hint, matching the
     no-human rule for editable changes.
   - Non-editable fix: the user is shown the before/after for its cases and asked whether to
     keep it or revert it. In an unattended run it is reverted and listed in `FINDINGS.md`.

### What this check is, and is not

It is a **smoke check**: a few cases, often a single trial. It can catch a fix that clearly
backfires. It **cannot** show that a fix improves the system, and the report never presents it
that way. There is no significance test here; the real measure is the loop's val gate. Running
it on train keeps the gate's val decisions clean.

**Scorer or metric fixes are excluded from the harm verdict.** Before and after would be graded
by different scorers. The check still runs both and reports the score change, labelled
"measurement change".

Cost: (cases + controls) × `n_trials` × 2 runs, plus re-runs for attribution. It counts toward
the run's budget and is recorded in the spend accounting.

## Verifying the fixes: the full comparison (`dry_run_verify: full`)

For users who want a real val number for the dry-run effect:

- baseline scores the **original seed** (a snapshot taken before any dry-run fix) and the
  **fixed seed** on **val, on the same frozen split, with the same `n_trials`**. In
  `split_mode: pooled`, both are scored the way baseline already scores the seed in that mode
  (stratum F, `incumbent_trials`), and the confirm stratum is never touched. *(Builder-check
  call.)* The sealed test split is not touched; its one-time use stays reserved for finalize.
- The original seed's val score is stored as `baseline_pre_dry_run` in `baseline.json`, with
  its stderr.
- **Scope:** this compares the original seed with `fixed_seed`, so it covers the **capability
  fixes only**. Approved fixes to system code or the scorer are present in both measurements.
  The report lists them as "not measured here", and points to the targeted check if it also
  ran. *(My call: reverting system-code fixes for a full val run would need snapshots of code
  outside cap-evolve's control.)*
- Combined with `--reuse-baseline`: the reused baseline counts as the "before" score only if
  the prior run's seed bytes match the original (pre-fix) seed. Otherwise the "before" score is
  re-measured.

## In every mode

- The **fixed seed** (after any reverts) is the candidate every algorithm has to beat, and the
  headroom verdict is computed from it.
- If no fixes were applied, baseline behaves exactly as today.

## Unfixed findings become optimizer hints

Findings marked `as_is` are passed to the optimizer as context, without weakening any honesty
guardrail:

- **Deterministic mode:** they are added to the rendered optimizer instructions through a new
  `{{DRY_RUN_FINDINGS}}` placeholder. It renders empty when there are none, so existing
  `INSTRUCTIONS.md` files without the placeholder keep working, and the pipeline self-test
  accepts its absence.
- **Agent mode:** the agent-mode handoff points the driving agent at `dry_run/FINDINGS.md`.
- Only findings with `editable: true` are framed as "you may fix this". Findings outside the
  editable surface are framed as "known system behavior: work with it", so the optimizer is
  not invited to edit code it does not own.

A hint is only a hint. Any edit made because of it still goes through the val significance
gate like any other candidate.

## Report

`report` gains a "Dry run" section when the phase ran:

- the number of findings by kind, how many were fixed and how many left as is
- each applied fix, with its location and whether it was in a capability, in system code, or
  in the scorer
- `targeted`: per fix, its cases with before → after and the verdict (`helped` / `neutral` /
  `harmed`, and reverted or kept), labelled **smoke check on N train cases, not a measured
  gain**
- `full`: `baseline_pre_dry_run` → `baseline` (val, ± stderr), labelled as the **dry-run
  effect, on val**
- both are kept separate from the loop's **val → sealed-test** result
- a note when a scorer fix was applied, saying the before/after comparison spans a change in
  measurement

The headline number stays as it is: the loop's result is test against the (fixed) baseline.
The dry-run effect is reported next to it and never added to it.

The JSON summary gains `dry_run_ran`, `dry_run_fixes`, `dry_run_reverted`, `dry_run_verify`
(the mode), `dry_run_smoke` (per-fix verdicts, null when not run), `baseline_pre_dry_run` and
`baseline_pre_dry_run_stderr` (null when not measured).

## Phase skill shape

`skills/phases/dry-run/` follows the existing phases:

- `SKILL.md`: the inspection discipline (whole-system reading, flow-first, obvious bugs only,
  ask for each finding, never fix silently, scorer warning), with good and bad examples
  including the motivating example
- `meta.yaml`: `component: phase`, `needs: [project, checked]`, `provides: [dry_run_findings]`,
  compatible with all capabilities, optimizers and algorithms
- `scripts/run.py`: the mechanical part. It resolves the file set from the project, snapshots
  the original seed, writes and validates `findings.json`, freezes the split when the targeted
  check runs, runs the before/after evals through cap-evolve's own evaluator (so rollouts land in
  the run dir), applies and reverts fixes, and records the verdicts
- `scripts/check.py`, `scripts/abstract.py`: same role as in other phases
- `references/concepts.md`: why flow issues are hard to see in traces, why fixes are recorded
  apart from the loop's gain, why scorer fixes are flagged

The **judgement** (reading the code, writing findings, and picking which cases each fix is
relevant to) belongs to the coding agent, the same way intake's interview does. The script does
not try to find issues or pick cases itself.

`baseline` lists `dry_run_findings` and the dry run's `splits` as optional inputs. When they
exist, baseline reuses that run dir and split instead of creating them. When the phase did not
run, baseline needs nothing new.

## Goals

- Catch major flow flaws and obvious bugs before eval budget is spent.
- Fix what the optimizer may edit without bothering the user. Leave every change outside that
  surface (scorer, metrics, system code) to the user, and let them choose whether to measure
  the effect.
- Keep the dry run's effect measurable (when the user asks for it) and separate from the loop's.
- No behavior change for anyone who does not turn it on.

## Non-goals

- A general code review, a linter or a style pass.
- Proving that the fixes improve the system. The targeted check only guards against harm; the
  loop's val gate measures improvement.
- Replacing `diagnose`. The dry run looks at the code before any traces exist, and diagnose
  works from traces.

## Anti-goals (would be a failure even if it technically works)

- Changing the scorer, metrics or system code before baseline without a human's approval.
- Automatic fixes that are not listed with their diff in the findings and report.
- Flooding the user with low-value or speculative findings. That trains them to click past
  the real ones.
- Folding the dry-run gain into the loop's reported improvement.
- Touching the sealed test split, or using val, for the targeted check.
- Presenting the targeted check's few-case result as a measured gain.

## Left to the builder

- How the agent walks the code (subagent fan-out, file ordering) and how many findings to show
  at once. *My call:* ask about the non-editable findings one at a time, most severe first, with
  "fix all remaining / skip all remaining" shortcuts once there are more than three. Show the
  automatic fixes once, as a single summary list.
- How the findings file is versioned across `--resume`. *My call:* the dry run does not re-run
  on resume when `findings.json` exists and has no `pending` decisions.
- Whether `cap-evolve run` gets a `--skip-dry-run` override flag.
- How a "case got worse" is computed when `n_trials > 1` (mean drop, or any trial lost).
- Test coverage. At minimum: the sequence with the dry run on and off, the targeted check
  picking only from train and reverting a harmful editable fix, the split written once and
  reused by baseline, the full comparison writing both scores on the same split, the `{{DRY_RUN_FINDINGS}}` placeholder
  rendering empty and optional, and the unattended `stop` and `continue` paths.
