"""baseline — create the run dir, freeze the splits, score the seed on val.

This establishes the starting point every algorithm compares against. It is the
first step that touches data, so it owns split creation (seeded, written once).
Prints the run-dir path and the baseline val score as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from cap_evolve import Budget, RunDir, harness
from cap_evolve.check import load_adapter, run_check


def _dry_run_comparison(adapter, run_dir, dry: dict, *, n_trials: int, workers, reuse_from) -> dict | None:
    """``dry_run_verify: full``: score the ORIGINAL (pre-fix) seed on the same split as the fixed seed.

    Same frozen split, same trial count, the same stratum the seed was just scored on (val, or F in
    ``split_mode: pooled``; the confirm stratum and test are never touched). Recorded as
    ``baseline_pre_dry_run`` in ``baseline.json``. Only capability fixes are measured: fixes applied to
    system code or the scorer are present on both sides. A ``--reuse-baseline`` prior run stands in for
    the "before" score only when its seed is byte-identical to the original seed and no system fix was
    applied (the prior run measured the system without it).
    """
    from cap_evolve import dryrun
    from cap_evolve.cache import hash_candidate_dir
    if dry.get("verify") != "full":
        return None
    path = run_dir.root / "baseline.json"
    base = json.loads(path.read_text(encoding="utf-8"))
    if not dry.get("seed_dir"):
        base["baseline_pre_dry_run_note"] = ("not measured: the dry run applied no capability fix, so "
                                             "the original and fixed seeds are the same bytes")
        path.write_text(json.dumps(base, indent=2), encoding="utf-8")
        return None
    split = base.get("split") or "val"
    pre = None
    if reuse_from is not None and not dry.get("n_system_fixes"):
        prior_seed = Path(reuse_from) / "candidates" / "seed"
        prior_base = Path(reuse_from) / "baseline.json"
        if (prior_seed.is_dir() and prior_base.exists()
                and hash_candidate_dir(prior_seed) == dry.get("original_seed_hash")):
            pre = (json.loads(prior_base.read_text(encoding="utf-8")).get("val") or None)
            if pre is not None:
                base["baseline_pre_dry_run_source"] = f"reused from {reuse_from} (same seed bytes)"
    if pre is None:
        run_dir.snapshot(dryrun.CAND_PRE, Path(dry["original_seed"]))
        res = harness.evaluate_candidate(adapter, run_dir.candidate_dir(dryrun.CAND_PRE), run_dir=run_dir,
                                         split=split, n_trials=n_trials, tag=dryrun.CAND_PRE,
                                         workers=workers)
        pre = res.to_dict()
    base["baseline_pre_dry_run"] = pre
    base["baseline_pre_dry_run_scope"] = "capability fixes only; system-code and scorer fixes are on both sides"
    path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    run_dir.log_event("baseline_pre_dry_run", val=pre.get("reward"), stderr=pre.get("stderr"),
                      split=split, fixed_val=(base.get("val") or {}).get("reward"))
    return pre


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="baseline")
    p.add_argument("--base", default=".capevolve", help="dir under which run_* is created")
    p.add_argument("--project", required=True, help="dir with adapters/adapter.py")
    p.add_argument("--capability", required=True, help="seed capability dir")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ratios", default="0.5,0.25,0.25")
    p.add_argument("--split-ids", default=None,
                   help="JSON file {train:[],val:[],test:[]} to pin the split explicitly")
    p.add_argument("--reuse-baseline", default=None,
                   help="prior run dir: reuse its splits/baseline/seed/val-rollouts and "
                        "SKIP the baseline eval (algorithm starts at iter 1 on it)")
    p.add_argument("--n-trials", type=int, default=1)
    p.add_argument("--workers", type=int, default=None,
                   help="concurrent rollouts for the seed evaluation (unset = CAPEVOLVE_WORKERS or serial)")
    p.add_argument("--max-iterations", type=int, default=10)
    p.add_argument("--stall", type=int, default=0)
    p.add_argument("--max-metric-calls", type=int, default=0, help="0 = unlimited")
    p.add_argument("--max-usd", type=float, default=0.0,
                   help="0 = unlimited; total spend cap (runner + optimizer + intake)")
    p.add_argument("--max-optimizer-usd", type=float, default=0.0,
                   help="0 = off; separate cap on optimizer spend alone")
    p.add_argument("--stop-at-reward", type=float, default=0.0,
                   help="0 = off; stop the loop as soon as the best val reward reaches this")
    p.add_argument("--run-ts", default=None, help="fixed timestamp for reproducible run dirs")
    p.add_argument("--resume", action="store_true",
                   help="reopen an existing run dir instead of failing; skip the baseline "
                        "eval when it already ran (baseline.json present)")
    p.add_argument("--spec", default=None,
                   help="path to capevolve.yaml spec (for observer config)")
    p.add_argument("--dry-run-state", default=None,
                   help="the dry-run phase's state.json: reuse its run dir and frozen split, and "
                        "score its fixed seed (when it applied capability fixes) instead of --capability")
    args = p.parse_args(argv)

    # The hard gate, on THIS path too. `cap-evolve run` checks the adapter before it
    # calls us (cli.py:721-726), but /cap-evolve:baseline is reachable directly and the
    # needs/provides DAG validates declared order, not runtime state — so without this
    # the standalone chain would freeze a split against a knowingly-broken adapter and
    # every number in the run would be measured against it (#358). Gate before the run
    # dir exists so a red check leaves nothing behind.
    rep = run_check(Path(args.project))
    if not rep.ok:
        print(json.dumps({"step": "baseline", "error": "check failed",
                          "report": rep.to_dict()}, indent=2), file=sys.stderr)
        return 1

    Path(args.base).mkdir(parents=True, exist_ok=True)
    budget = Budget(max_iterations=args.max_iterations, stall=args.stall,
                    max_metric_calls=args.max_metric_calls, max_usd=args.max_usd,
                    max_optimizer_usd=args.max_optimizer_usd, stop_at_reward=args.stop_at_reward)
    dry = None
    if args.dry_run_state:
        # The dry run already created the run dir (its evals and its split live there) and froze the
        # split. Reopen it; never create a second one.
        from cap_evolve import dryrun
        try:
            dry = dryrun.for_baseline(args.dry_run_state)
        except dryrun.DryRunError as e:
            print(json.dumps({"step": "baseline", "error": str(e)}, indent=2), file=sys.stderr)
            return 1
        run_dir = RunDir.open(Path(dry["applied_run_dir"]))
    else:
        run_dir = RunDir.create(Path(args.base), ts=args.run_ts, budget=budget, exist_ok=args.resume)

    # Pin the spec to the run NOW. Everything that reads config through specfile.spec_for_run (split_mode,
    # coverage/budget switches, the P2 keys) resolves the spec from the run dir's events, and the CLI logs
    # its run_config only AFTER this phase, so without this the baseline silently takes the legacy path.
    try:
        _spec = Path(args.spec) if args.spec else Path(args.project, "capevolve.yaml")
        if _spec.is_file() and not any(
                '"spec"' in ln and ('"run_config"' in ln or '"spec_resolved"' in ln)
                for ln in (run_dir.events_path.read_text(encoding="utf-8").splitlines()
                           if run_dir.events_path.exists() else [])):
            run_dir.log_event("spec_resolved", spec=str(_spec.resolve()), project=str(Path(args.project).resolve()))
    except Exception:  # noqa: BLE001 — provenance must never block a baseline
        pass

    try:
        try:
            from cap_evolve.specfile import read_yaml
            from capevolve_telemetry import load_observers, load_observers_from_state
            spec_path = Path(args.spec) if args.spec else Path(args.project, "capevolve.yaml")
            spec_text = spec_path.read_text(encoding="utf-8")
            full_spec = read_yaml(spec_text)
            obs_config = full_spec.get("observers")
            caps = full_spec.get("capabilities")
            run_name = str(full_spec.get("run_name", "")).strip()
            if not run_name:
                parts = [run_dir.root.name]
                for key in ("algorithm_skill", "optimizer_skill", "target_model"):
                    v = str(full_spec.get(key, "")).strip()
                    if v:
                        parts.append(v)
                if isinstance(caps, list):
                    parts.append("+".join(str(c) for c in caps))
                elif caps:
                    parts.append(str(caps))
                run_name = " | ".join(parts)
            run_tags = {}
            for key in ("algorithm_skill", "optimizer_skill", "optimizer_model",
                         "target_model", "max_iterations", "gate_mode", "num_trials",
                         "dataset_source"):
                v = full_spec.get(key)
                if v is not None and str(v).strip():
                    run_tags[key] = str(v)
            if caps:
                run_tags["capabilities"] = "+".join(str(c) for c in caps) if isinstance(caps, list) else str(caps)
            observers = (
                load_observers_from_state(run_dir.load_observer_state())
                if args.resume
                else load_observers(
                    obs_config,
                    run_dir_root=str(run_dir.root),
                    run_name=run_name,
                    run_tags=run_tags,
                )
            )
            for obs in observers:
                run_dir.add_observer(obs)
        except Exception:  # noqa: BLE001
            pass

        # Resume fast-path: baseline already ran → the split is frozen, the seed is scored,
        # best_id is set. Re-print the recorded baseline and skip the (expensive) eval so the
        # algorithm resumes straight from the current best. state.json is left untouched.
        if args.resume and (run_dir.root / "baseline.json").exists():
            splits = run_dir.read_splits()
            recorded = json.loads((run_dir.root / "baseline.json").read_text(encoding="utf-8"))
            print(json.dumps({
                "run_dir": str(run_dir.root),
                "splits": {"train": len(splits.train), "val": len(splits.val), "test": len(splits.test)},
                "baseline_val": recorded.get("val", {}),
                "resumed": True,
            }, indent=2))
            return 0

        adapter = load_adapter(Path(args.project))

        # A dry run that applied a fix hands baseline a different system: a NEW seed
        # (dry_run/fixed_seed) and/or changed system code. A prior run's baseline measured the old
        # one, so it cannot stand in for this run's seed score.
        dry_seed = (dry or {}).get("seed_dir")
        dry_changed = bool((dry or {}).get("n_fixes"))
        if args.reuse_baseline and dry_changed:
            run_dir.log_event("reuse_baseline_ignored", prior_run_dir=str(args.reuse_baseline),
                              reason="the dry run applied fixes; the seed is scored fresh")

        # --reuse-baseline: copy a prior run's frozen split + baseline + seed snapshot +
        # seed val rollouts into this fresh run dir and SKIP the (expensive) baseline eval.
        if args.reuse_baseline and not dry_changed:
            result = harness.reuse_baseline(Path(args.reuse_baseline), run_dir=run_dir)
            splits = run_dir.read_splits()
            print(json.dumps({
                "run_dir": str(run_dir.root),
                "splits": {"train": len(splits.train), "val": len(splits.val), "test": len(splits.test)},
                "baseline_val": result.to_dict(),
                "reused_baseline_from": str(args.reuse_baseline),
            }, indent=2))
            return 0

        ratios = tuple(float(x) for x in args.ratios.split(","))
        split_ids = None
        if args.split_ids:
            # Resolve the split-ids path robustly: as given (absolute or cwd-relative),
            # else relative to the project dir. `cap-evolve run` invokes baseline with
            # cwd=workdir, so a project-relative `split_ids_file: split_ids.json` in
            # capevolve.yaml would otherwise miss — this lets users author it naturally.
            sp = Path(args.split_ids)
            if not sp.exists():
                cand = Path(args.project) / args.split_ids
                if cand.exists():
                    sp = cand
            split_ids = json.loads(sp.read_text(encoding="utf-8"))
        splits = harness.ensure_splits(adapter, run_dir, seed=args.seed, ratios=ratios,
                                       split_ids=split_ids)
        # (A dry run froze — and already vetted — this split; baseline only reloads it.)
        if split_ids is None and dry is None and harness.refuse_degenerate_split(splits, run_dir, step="baseline"):
            return 1
        # Resolve the seed capability dir robustly: as given (absolute/cwd-relative),
        # else relative to the project dir. `cap-evolve run` invokes baseline with
        # cwd=workdir, so a project-relative `capability_path: seed_capability` in
        # capevolve.yaml would otherwise miss — let users author it naturally.
        cap_path = Path(args.capability)
        if not cap_path.exists():
            cand = Path(args.project) / args.capability
            if cand.exists():
                cap_path = cand
        if dry_seed:
            cap_path = Path(dry_seed)
        from cap_evolve import gatecfg
        if gatecfg.GateConfig.for_run(run_dir).split_mode == "pooled":
            # split_mode: pooled -> pool = train + val, partitioned once into F and C; the seed is
            # measured on F only (incumbent_trials per task), never on the hidden confirm stratum.
            from cap_evolve import promotion as _promotion
            from cap_evolve.specfile import spec_for_run as _spec_for_run
            _p2 = _promotion.P2Params.from_spec({**(_spec_for_run(run_dir) or {}), "promotion_rule": "P2"})
            seed_trials = max(args.n_trials, _p2.incumbent_trials)
            result = harness.baseline_pooled(adapter, cap_path, run_dir=run_dir,
                                             n_trials=seed_trials, workers=args.workers)
        else:
            seed_trials = args.n_trials
            result = harness.baseline(adapter, cap_path, run_dir=run_dir, n_trials=args.n_trials,
                                       workers=args.workers)
        pre = None
        if dry is not None:
            pre = _dry_run_comparison(adapter, run_dir, dry, n_trials=seed_trials, workers=args.workers,
                                      reuse_from=Path(args.reuse_baseline) if args.reuse_baseline else None)

        # Materialize ./guidance/<cap>/SKILL.md (+ diagnose + capability_sources) into the
        # RUN dir itself, once, here — regardless of orchestration_mode. Deterministic mode
        # already gets these per-iteration into the optimizer subprocess's workdir
        # (harness._inject_optimizer_context); agent mode has no such per-iteration step to
        # piggyback on, so without this a driving agent gets no capability-specific edit-menu
        # guidance unless it happens to go through agent-optimize/scripts/host.py. Re-reads
        # the spec rather than reusing `full_spec` above: that read is wrapped in its own
        # best-effort try/except and may not have completed.
        try:
            from cap_evolve.specfile import read_yaml as _read_yaml
            guidance_spec_path = Path(args.spec) if args.spec else Path(args.project, "capevolve.yaml")
            guidance_spec = _read_yaml(guidance_spec_path.read_text(encoding="utf-8"))
            harness.stage_capability_guidance(
                run_dir, run_dir.root,
                capabilities=guidance_spec.get("capabilities"),
                capability_sources=guidance_spec.get("capability_sources"),
                project_dir=Path(args.project),
            )
        except Exception as e:  # noqa: BLE001 — guidance is a nicety, baseline is the point
            run_dir.log_event("optimizer_context_warning", what="guidance (baseline)",
                              error=str(e)[:300])

        # Headroom: the budget decision this phase exists to make. Saturated => every
        # later Δ chases noise; floor => usually a broken adapter, not a hard task.
        # Emitted, not just advised, so `cap-evolve run` / orchestrate can stop on it
        # without a human reading the number. Non-fatal: recording it is the job.
        headroom = round(max(0.0, 1.0 - result.reward), 4)
        verdict = ("saturated" if result.reward + max(result.stderr, 0.0) >= 1.0
                   else "floor" if result.reward <= 0.0 else "ok")
        run_dir.log_event("headroom", headroom=headroom, verdict=verdict, val=result.reward)

        out = {
            "run_dir": str(run_dir.root),
            "splits": {"train": len(splits.train), "val": len(splits.val), "test": len(splits.test)},
            "baseline_val": result.to_dict(),
            "headroom": headroom,
            "headroom_verdict": verdict,
        }
        if dry is not None:
            out["dry_run"] = {"seed": str(cap_path), "n_fixes": dry.get("n_fixes", 0),
                              "baseline_pre_dry_run": (pre or {}).get("reward"),
                              "baseline_pre_dry_run_stderr": (pre or {}).get("stderr")}
        print(json.dumps(out, indent=2))
        return 0
    finally:
        close_observers = getattr(run_dir, "close_observers", None)
        if callable(close_observers):
            close_observers()


if __name__ == "__main__":
    sys.exit(main())
