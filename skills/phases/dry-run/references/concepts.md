# Concepts — the dry run

> The dry run reads the system before baseline. This note explains why flow flaws are hard
> to see in traces, why the dry run's fixes are recorded apart from the loop's gain, and why
> scorer fixes are flagged. Implementation: `cap_evolve.dryrun` + `scripts/run.py`.
> Design: `docs/specs/2026-10-19-dry-run-design.md`.

## Why flow flaws hide in traces

Every algorithm in cap-evolve learns from failed eval traces: `diagnose` clusters failures
by where they happen and what was expected, and the optimizer edits the capability to remove
the biggest clusters. That works when a failure has a site. A flaw in the *flow* often has
none:

- A step that ignores a signal (the self-evaluation that scores 10/10, followed by a "fix"
  step that runs anyway) damages some answers and wastes tokens on all of them. In the traces
  that is a small loss spread over many tasks, with no common failure text to cluster on.
- A retry loop with no stop-on-success costs time and money on tasks that pass, so it never
  shows up as a failure at all.
- A prompt that contradicts the code makes the agent fight the harness; the trace shows the
  struggle, not the contradiction.

The optimizer can spend iterations working around such a flaw in the prompt without ever
naming it. A reader of the code sees it in minutes. The dry run is that reading, done once,
before any eval budget is spent.

## Why only the obvious

The dry run asks a human about everything outside the editable surface. Every question has
a cost: attention. If most findings are speculative, the user learns to approve or skip
without reading, and the one finding that matters gets the same treatment. So the bar for a
bug is "a reviewer accepts it on sight". Anything that needs an argument becomes a hint for
the optimizer, whose edits still have to pass the val gate.

## Why the editable surface needs no human

The rule is the boundary cap-evolve already enforces in the loop. The optimizer may edit the
capability's prompts, tools and skills without asking anyone; the gate measures each edit.
A dry-run fix inside that surface is an edit the optimizer could have made, so asking adds no
safety. It is still applied to a **copy** (`dry_run/fixed_seed`), never to the user's seed:
the original bytes stay valid for `reuse_baseline` and give the "before" score a seed to
measure.

Outside that surface — system code, the adapter, the task data, the scorer — the optimizer
has no permission, and neither does the dry run. A human approves each change.

## Why the dry run's fixes are kept apart from the loop's gain

The headline result is the loop's: best candidate against the seed, on the sealed test
split. If the dry run fixed the seed before baseline, the seed the loop starts from is
already better than the user's original. Folding the two together would credit the loop with
a gain it did not find, or hide that the dry run did most of the work. So:

- every applied fix is recorded with its diff and approver (`auto` / `user`);
- the report says "the seed going into the loop already includes these N fixes";
- the dry-run effect, when measured (`dry_run_verify: full`, `baseline_pre_dry_run`), is
  printed next to the headline and never added to it.

## Why the targeted check is a smoke check

A handful of train cases, often one trial each, cannot show that a fix improves the system:
the noise floor at that size is far above most real effects. It can show that a fix clearly
backfires, which is the question it answers. It runs on **train** so val stays clean for the
gate and test stays sealed. A real measurement is the `full` mode (on val, same split, same
trials) or, better, the loop's own gate.

## Why scorer fixes are flagged

A scorer or metric fix changes *what is measured*, not the agent. A before/after across it
compares two different rulers: the number can move a lot while the agent does exactly the
same thing. So a scorer fix always needs a human, never gets a harm verdict in the targeted
check (its score change is labelled "measurement change"), and is called out in the report.
