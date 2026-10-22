"""dry-run — inspect the system before baseline; fix what the optimizer may edit, ask about the rest.

The judgement (reading the code, writing findings, picking the cases each fix touches) belongs to
the coding agent. This script is the mechanical part, as subcommands:

  prepare   create the run dir, freeze the split (the same seeded code baseline uses, written
            once), snapshot the original seed, write dry_run/context.json for the inspection
  apply     validate dry_run/findings.json, apply the fixes (editable ones to dry_run/fixed_seed,
            approved non-editable ones in place), run the targeted check, revert harmful fixes,
            and hand off to baseline (dry_run/state.json). Exit 0 = ready for baseline,
            2 = waiting for a human (pending decision or post-check review), 3 = BLOCKED (unattended)
  decide    record a human's answer for one finding (fix | as_is | keep | revert)
  auto      the unattended path `cap-evolve run` takes: prepare, start the optimizer agent CLI
            with SKILL.md as its brief (unless an interactive findings.json is reusable), apply

Prints one JSON document on stdout.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401

from cap_evolve import Budget, RunDir, dryrun, harness
from cap_evolve.check import load_adapter, run_check

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"
EXIT_WAITING = 2
EXIT_BLOCKED = 3


def _spec(args) -> tuple[Path, dict]:
    from cap_evolve.specfile import read_yaml
    path = Path(args.spec) if getattr(args, "spec", None) else Path(args.project) / "capevolve.yaml"
    return path, (read_yaml(path.read_text(encoding="utf-8")) if path.is_file() else {})


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, indent=2))
    return code


def _add_common(p) -> None:
    p.add_argument("--project", required=True, help="dir with adapters/adapter.py")
    p.add_argument("--spec", default=None, help="capevolve.yaml (default: <project>/capevolve.yaml)")


def _add_prepare(p) -> None:
    p.add_argument("--base", default=".capevolve", help="dir under which run_* is created")
    p.add_argument("--capability", required=True, help="seed capability dir")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ratios", default="0.5,0.25,0.25")
    p.add_argument("--split-ids", default=None)
    p.add_argument("--run-ts", default=None)
    p.add_argument("--resume", action="store_true")
    for flag, typ in (("--max-iterations", int), ("--stall", int), ("--max-metric-calls", int),
                      ("--max-usd", float), ("--max-optimizer-usd", float), ("--stop-at-reward", float)):
        p.add_argument(flag, type=typ, default=0 if typ is int else 0.0)


def _add_apply(p) -> None:
    p.add_argument("--n-trials", type=int, default=1)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--unattended", action="store_true",
                   help="nobody to ask: non-editable fixes are never applied (see dry_run_unattended)")


def _prepare(args, spec_path: Path, spec: dict) -> tuple[RunDir | None, dict]:
    rep = run_check(Path(args.project))
    if not rep.ok:
        return None, {"step": "dry-run", "error": "check failed", "report": rep.to_dict()}
    Path(args.base).mkdir(parents=True, exist_ok=True)
    budget = Budget(max_iterations=args.max_iterations or 10, stall=args.stall,
                    max_metric_calls=args.max_metric_calls, max_usd=args.max_usd,
                    max_optimizer_usd=args.max_optimizer_usd, stop_at_reward=args.stop_at_reward)
    run_dir = RunDir.create(Path(args.base), ts=args.run_ts, budget=budget, exist_ok=args.resume)
    # Pin the spec now, exactly as baseline does: split_mode and the budget switches resolve the spec
    # from the run's events, and this phase is the first to touch the run dir.
    if spec_path.is_file():
        text = run_dir.events_path.read_text(encoding="utf-8") if run_dir.events_path.exists() else ""
        if '"spec_resolved"' not in text:
            run_dir.log_event("spec_resolved", spec=str(spec_path.resolve()),
                              project=str(Path(args.project).resolve()))
    adapter = load_adapter(Path(args.project))
    split_ids = None
    if args.split_ids:
        sp = Path(args.split_ids)
        if not sp.exists() and (Path(args.project) / args.split_ids).exists():
            sp = Path(args.project) / args.split_ids
        split_ids = json.loads(sp.read_text(encoding="utf-8"))
    # Frozen here, once, with the same seed/ratios/ids baseline would use; baseline reloads it.
    splits = harness.ensure_splits(adapter, run_dir, seed=args.seed,
                                   ratios=tuple(float(x) for x in args.ratios.split(",")),
                                   split_ids=split_ids)
    if split_ids is None and harness.refuse_degenerate_split(splits, run_dir, step="dry-run"):
        return None, {"step": "dry-run", "error": "degenerate split"}
    _, allowed = dryrun.check_ids(run_dir)
    ctx = dryrun.prepare(args.project, spec, args.capability, run_dir=run_dir, allowed_ids=allowed)
    return run_dir, {"run_dir": str(run_dir.root), "context": str(dryrun.Paths.for_project(args.project).context),
                     "findings": ctx["findings_path"], "verify": ctx["verify"],
                     "splits": {"train": len(splits.train), "val": len(splits.val), "test": len(splits.test)}}


def _apply(args, spec: dict, run_dir: RunDir) -> int:
    adapter = load_adapter(Path(args.project))
    try:
        res = dryrun.apply(args.project, spec, adapter=adapter, run_dir=run_dir,
                           unattended=args.unattended, n_trials=args.n_trials, workers=args.workers)
    except (dryrun.DryRunError, ValueError) as e:
        return _emit({"step": "dry-run", "error": str(e)}, 1)
    res["run_dir"] = str(run_dir.root)
    res["markdown"] = str(dryrun.Paths.for_project(args.project).markdown)
    if res["status"] == "blocked":
        print(res["message"], file=sys.stderr)
        return _emit(res, EXIT_BLOCKED)
    return _emit(res, 0 if res["status"] == "ready" else EXIT_WAITING)


def _open_run(args) -> RunDir:
    state = dryrun.read_state(args.project)
    if not state.get("run_dir"):
        raise dryrun.DryRunError("no dry-run state — run `prepare` first")
    return RunDir.open(Path(state["run_dir"]))


def _brief(context: dict) -> str:
    """The unattended inspection brief: SKILL.md (the discipline) + this run's facts."""
    return (SKILL_MD.read_text(encoding="utf-8")
            + "\n\n---\n\n# This run (unattended)\n\n"
            "Nobody is available to answer questions. Read the system starting from the roots below, "
            "then write your findings to `./findings.json` in your working directory, in the format "
            "SKILL.md specifies, and STOP. Do not edit any file outside your working directory: the "
            "fixes are applied by the dry-run script, not by you. Leave every finding's `decision` as "
            "`pending`; decisions outside the editable surface need a human and will be made later.\n\n"
            "```json\n" + json.dumps(context, indent=2) + "\n```\n")


def _auto(args, spec_path: Path, spec: dict) -> int:
    paths = dryrun.Paths.for_project(args.project)
    state = dryrun.read_state(args.project)
    if args.resume and state.get("ready") and state.get("applied_run_dir"):
        # The dry run finished in this run before the interruption: do not inspect or re-apply.
        return _emit({"status": "ready", "resumed": True, "run_dir": state["applied_run_dir"],
                      "state": str(paths.state), "n_fixes": state.get("n_fixes", 0)})
    run_dir, out = _prepare(args, spec_path, spec)
    if run_dir is None:
        return _emit(out, 1)
    context = json.loads(paths.context.read_text(encoding="utf-8"))
    prior = None
    try:
        prior = json.loads(paths.findings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    reusable = (prior is not None and prior.get("seed_hash") == context["seed_hash"]
                and not any(f.get("decision") == "pending" for f in prior.get("findings") or [])
                and prior.get("written_by") != "agent")
    if reusable:
        run_dir.log_event("dry_run_reuse", findings=str(paths.findings))
    else:
        if not args.optimizer_cmd:
            return _emit({"step": "dry-run", "error": "no findings.json to reuse and no --optimizer-cmd "
                                                      "to start an inspection agent with"}, 1)
        rc = _inspect(args, run_dir, paths, context)
        if rc:
            return rc
    return _apply(args, spec, run_dir)


def _inspect(args, run_dir: RunDir, paths, context: dict) -> int:
    """Start the configured optimizer agent CLI on the brief; its spend is optimizer spend."""
    work = paths.agent_workdir
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    optimizer = harness.optimizer_from_command(shlex.split(args.optimizer_cmd))
    t0 = time.time()
    run_dir.log_event("dry_run_inspect_start", workdir=str(work))
    report = None
    try:
        report = optimizer(work, _brief(context)) or {}
    except RuntimeError as e:
        cost = getattr(e, "cost", None) or {}
        run_dir.update_spent(optimizer_seconds=time.time() - t0,
                             optimizer_usd=float(cost.get("cost_usd") or 0.0),
                             optimizer_tokens=int(cost.get("tokens") or 0))
        return _emit({"step": "dry-run", "error": f"inspection agent failed: {e}"[:2000]}, 1)
    run_dir.update_spent(optimizer_seconds=time.time() - t0,
                         optimizer_usd=float(report.get("cost_usd") or 0.0),
                         optimizer_tokens=int(report.get("tokens") or 0))
    run_dir.log_event("dry_run_inspect", seconds=round(time.time() - t0, 2),
                      optimizer_usd=float(report.get("cost_usd") or 0.0))
    written = work / "findings.json"
    try:
        doc = json.loads(written.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _emit({"step": "dry-run", "error": f"the inspection agent wrote no valid ./findings.json ({e})"}, 1)
    context = dict(context, original_seed=str(paths.original_seed))
    doc, _problems = dryrun.validate(doc, context, None)
    dryrun.strip_agent_approvals(doc)
    paths.findings.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dry-run")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("prepare", help="create the run dir, freeze the split, write context.json")
    _add_common(pp)
    _add_prepare(pp)
    pa = sub.add_parser("apply", help="apply + verify the findings, hand off to baseline")
    _add_common(pa)
    _add_apply(pa)
    pd = sub.add_parser("decide", help="record a human's decision for one finding")
    _add_common(pd)
    pd.add_argument("--id", required=True)
    pd.add_argument("--choice", required=True, choices=("fix", "as_is", "keep", "revert"))
    pu = sub.add_parser("auto", help="unattended: prepare + inspection agent + apply")
    _add_common(pu)
    _add_prepare(pu)
    _add_apply(pu)
    pu.add_argument("--optimizer-cmd", default="",
                    help="optimizer CLI command with {workdir} {prompt} placeholders (as hill-climb gets)")
    args = p.parse_args(argv)
    spec_path, spec = _spec(args)
    if args.cmd == "auto":
        args.unattended = True

    try:
        if args.cmd == "prepare":
            run_dir, out = _prepare(args, spec_path, spec)
            return _emit(out, 0 if run_dir is not None else 1)
        if args.cmd == "apply":
            return _apply(args, spec, _open_run(args))
        if args.cmd == "decide":
            f = dryrun.decide(args.project, args.id, args.choice)
            return _emit({"id": f["id"], "decision": f["decision"], "approved_by": f.get("approved_by"),
                          "verify": f.get("verify")})
        return _auto(args, spec_path, spec)
    except dryrun.DryRunError as e:
        return _emit({"step": "dry-run", "error": str(e)}, 1)


if __name__ == "__main__":
    sys.exit(main())
