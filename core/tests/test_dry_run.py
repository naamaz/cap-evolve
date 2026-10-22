"""The optional dry-run phase (docs/specs/2026-10-19-dry-run-design.md), on the real toy_calc project.

* the sequence with the dry run on and off (and ``--skip-dry-run``);
* unattended ``stop`` blocks before baseline; ``continue`` runs, applies only the editable fix, and
  baseline scores the fixed seed on the split the dry run froze (written once);
* the targeted check picks only from train;
* the full comparison writes both scores on the same split;
* ``{{DRY_RUN_FINDINGS}}`` renders empty, is optional, and carries as-is findings when present;
* the report's dry-run section and JSON keys.

The inspection agent of an unattended run is the offline ``mock`` optimizer: its edit script writes
``findings.json`` into the agent's working dir, exactly where the real agent is told to.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "core"
EXAMPLE = REPO / "examples" / "toy_calc"
TEMPLATE_SPEC = REPO / "templates" / "project" / "capevolve.yaml"
DRY_RUN = REPO / "skills" / "phases" / "dry-run" / "scripts"
BASELINE = REPO / "skills" / "phases" / "baseline" / "scripts"

sys.path.insert(0, str(CORE))

CAP_FIX = {"id": "F1", "kind": "flow", "severity": "major", "location": ["prompt.txt:1"],
           "what_happens": "the prompt never tells the agent to compute, so it guesses",
           "proposed_fix": {"summary": "ask for the exact number",
                            "edits": [{"file": "prompt.txt",
                                       "old": "Answer the user as best you can.",
                                       "new": "Answer the user as best you can. [CALC]"}]},
           "decision": "pending"}
SYS_FIX = {"id": "F2", "kind": "bug", "severity": "major", "location": ["adapters/adapter.py:58"],
           "what_happens": "the stand-in rambles instead of answering when the marker is missing",
           "proposed_fix": {"summary": "answer tersely",
                            "edits": [{"file": "adapters/adapter.py",
                                       "old": "I think {expr} is roughly some number.",
                                       "new": "I think {expr} is some number."}]},
           "decision": "fix", "approved_by": "user"}     # written by the agent: never an approval


@pytest.fixture(autouse=True)
def _env(tmp_path):
    old = dict(os.environ)
    os.environ["CAPEVOLVE_CORE"] = str(CORE)
    os.environ["CAPEVOLVE_SKILLS_DIR"] = str(REPO / "skills")
    os.environ["CAPEVOLVE_TOY_DATA"] = str(EXAMPLE)
    os.environ["CAPEVOLVE_MOCK_SCRIPT"] = str(tmp_path / "inspect_script.json")
    yield
    os.environ.clear()
    os.environ.update(old)


def _project(tmp_path: Path, *, mode="agent", **keys) -> tuple[Path, Path]:
    project = tmp_path / ".capevolve" / "project"
    (project / "adapters").mkdir(parents=True)
    shutil.copy(EXAMPLE / "adapter.py", project / "adapters" / "adapter.py")
    shutil.copytree(EXAMPLE / "capability", tmp_path / "seed_capability")
    text = TEMPLATE_SPEC.read_text(encoding="utf-8").replace(
        "orchestration_mode: deterministic", f"orchestration_mode: {mode}")
    for k, v in keys.items():
        line = next(ln for ln in text.splitlines() if ln.startswith(f"{k}:"))
        text = text.replace(line, f"{k}: {v}")
    spec = project / "capevolve.yaml"
    spec.write_text(text, encoding="utf-8")
    return project, spec


def _inspector_writes(tmp_path: Path, findings: list) -> None:
    """The mock 'inspection agent' writes these findings into its working dir."""
    (tmp_path / "inspect_script.json").write_text(json.dumps({"edits": [
        {"file": "findings.json", "op": "set", "text": json.dumps({"findings": findings})}]}),
        encoding="utf-8")


def _json_lines(out: str) -> list:
    objs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return objs


def _run(argv, capsys):
    from cap_evolve import cli
    rc = cli.main(argv)
    return rc, capsys.readouterr().out


# ---- sequence --------------------------------------------------------------------------------

@pytest.mark.parametrize("on,skip,expected", [("false", False, False), ("true", False, True),
                                              ("true", True, False)])
def test_sequence_with_the_dry_run_on_and_off(tmp_path, capsys, on, skip, expected):
    project, spec = _project(tmp_path, mode="deterministic", dry_run=on)
    argv = ["run", "--spec", str(spec), "--project", str(project), "--dashboard", "off", "--plan-only"]
    rc, out = _run(argv + (["--skip-dry-run"] if skip else []), capsys)
    assert rc == 0
    seq = json.loads(out)["sequence"]
    assert ("dry-run" in seq) is expected
    if expected:
        assert seq.index("implement-and-check") < seq.index("dry-run") < seq.index("baseline")


def test_orchestrate_plan_shows_the_dry_run_only_when_enabled():
    import importlib.util
    scripts = REPO / "skills" / "orchestrate" / "orchestrate" / "scripts"
    sys.path.insert(0, str(scripts))                  # for its `import _bootstrap`
    try:
        spec = importlib.util.spec_from_file_location("orchestrate_run_dry", scripts / "run.py")
        orchestrate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(orchestrate)
    finally:
        sys.path.remove(str(scripts))
    manifest = orchestrate._load_manifest(REPO / "skills")
    assert "dry-run" not in orchestrate.build_sequence({})
    seq = orchestrate.build_sequence({"dry_run": True})
    assert seq[seq.index("baseline") - 1] == "dry-run"
    assert orchestrate.validate_dag(seq, manifest)["ok"]


def test_bad_verify_mode_is_refused(tmp_path, capsys):
    project, spec = _project(tmp_path, dry_run="true", dry_run_verify="maybe")
    rc, out = _run(["run", "--spec", str(spec), "--project", str(project), "--dashboard", "off",
                    "--plan-only"], capsys)
    assert rc == 1 and "dry_run_verify" in out


# ---- unattended ------------------------------------------------------------------------------

def test_unattended_stop_blocks_before_baseline(tmp_path, capsys):
    project, spec = _project(tmp_path, dry_run="true")
    _inspector_writes(tmp_path, [CAP_FIX, SYS_FIX])
    rc, out = _run(["run", "--spec", str(spec), "--project", str(project), "--dashboard", "off",
                    "--run-ts", "stop"], capsys)
    assert rc == 1
    assert "BLOCKED: dry-run found 1 major issue(s) needing approval" in out
    run_dir = tmp_path / ".capevolve" / "run_stop"
    assert not (run_dir / "baseline.json").exists(), "baseline must not run past a BLOCKED dry run"
    assert "roughly some number" in (project / "adapters" / "adapter.py").read_text()
    assert "BLOCKED" in (project / "PROJECT.md").read_text()
    md = (project / "dry_run" / "FINDINGS.md").read_text()
    assert "F2" in md and "pending" in md


def test_unattended_continue_applies_only_editable_fixes(tmp_path, capsys):
    project, spec = _project(tmp_path, dry_run="true", dry_run_unattended="continue")
    _inspector_writes(tmp_path, [CAP_FIX, SYS_FIX])
    rc, out = _run(["run", "--spec", str(spec), "--project", str(project), "--dashboard", "off",
                    "--run-ts", "cont"], capsys)
    assert rc == 0, out
    handoff = next(o for o in _json_lines(out) if o.get("mode") == "agent")
    run_dir = tmp_path / ".capevolve" / "run_cont"
    assert handoff["dry_run_findings"].endswith("dry_run/FINDINGS.md")

    # the system fix was never applied; the user's seed is untouched; baseline scored the fixed copy
    assert "roughly some number" in (project / "adapters" / "adapter.py").read_text()
    assert "[CALC]" not in (tmp_path / "seed_capability" / "prompt.txt").read_text()
    assert "[CALC]" in (run_dir / "candidates" / "seed" / "prompt.txt").read_text()
    base = json.loads((run_dir / "baseline.json").read_text())
    assert base["val"]["reward"] == 1.0

    # split written once, by the dry run, and reused by baseline
    events = [json.loads(ln) for ln in (run_dir / "events.jsonl").read_text().splitlines()]
    kinds = [e.get("kind") or e.get("event") for e in events]
    assert kinds.count("splits") == 1
    assert kinds.index("splits") < kinds.index("dry_run") < kinds.index("baseline")

    # the targeted check ran on train only
    splits = json.loads((run_dir / "splits.json").read_text())
    checked = {p.name.split("__")[0] for p in (run_dir / "rollouts").rglob("*__dry_run_*.json")}
    assert checked and checked <= set(splits["train"])
    assert not list((run_dir / "rollouts" / "val").glob("*__dry_run_*"))

    # the declined system finding reaches the optimizer as known behavior, not as an invitation
    from cap_evolve import dryrun
    hints = dryrun.hints_for_run(run_dir)
    assert "work with it" in hints and "roughly" not in hints and "rambles" in hints


def test_resume_does_not_rerun_a_finished_dry_run(tmp_path, capsys):
    project, spec = _project(tmp_path, dry_run="true", dry_run_unattended="continue")
    _inspector_writes(tmp_path, [CAP_FIX])
    argv = ["run", "--spec", str(spec), "--project", str(project), "--dashboard", "off", "--run-ts", "r"]
    assert _run(argv, capsys)[0] == 0
    (tmp_path / "inspect_script.json").unlink()      # a second inspection would now fail loudly
    rc, out = _run(argv + ["--resume"], capsys)
    assert rc == 0, out


# ---- targeted check picks from train only -----------------------------------------------------

def test_select_cases_never_reaches_into_val_or_test():
    from cap_evolve import dryrun
    cfg = dryrun.DryRunConfig(enabled=True, cases_per_fix=3, max_cases=4)
    fixes = [{"id": "F1", "severity": "major",
              "verify_cases": [{"task_id": t} for t in ("tr1", "v1", "te1", "tr2")]},
             {"id": "F2", "severity": "minor",
              "verify_cases": [{"task_id": t} for t in ("tr3", "tr4", "tr5")]}]
    per_fix, controls, notes = dryrun.select_cases({"controls": [{"task_id": "v2"}]}, fixes,
                                                   ["tr1", "tr2", "tr3", "tr4", "tr5", "tr6", "tr7"], cfg)
    assert per_fix == {"F1": ["tr1", "tr2"], "F2": ["tr3", "tr4"]}      # max_cases caps the total
    assert set(controls) <= {"tr5", "tr6", "tr7"} and len(controls) == 2
    assert any("v1" in n for n in notes) and any("te1" in n for n in notes)


# ---- full comparison -------------------------------------------------------------------------

def _phase(script_dir: Path, argv: list, capsys) -> tuple[int, dict]:
    sys.path.insert(0, str(script_dir))
    sys.modules.pop("run", None)
    try:
        import run as mod
        rc = mod.main(argv)
    finally:
        sys.path.remove(str(script_dir))
        sys.modules.pop("run", None)
    out = capsys.readouterr().out
    return rc, json.loads(out[out.index("{"):]) if "{" in out else {}


def test_full_comparison_scores_both_seeds_on_the_same_split(tmp_path, capsys, monkeypatch):
    project, spec = _project(tmp_path, dry_run="true", dry_run_verify="full")
    monkeypatch.chdir(tmp_path)
    rc, prep = _phase(DRY_RUN, ["prepare", "--project", str(project), "--capability", "seed_capability",
                                "--base", str(tmp_path / ".capevolve"), "--run-ts", "full"], capsys)
    assert rc == 0, prep
    (project / "dry_run" / "findings.json").write_text(json.dumps({"findings": [CAP_FIX]}))
    rc, res = _phase(DRY_RUN, ["apply", "--project", str(project)], capsys)
    assert rc == 0 and res["status"] == "ready", res
    run_dir = Path(prep["run_dir"])
    assert not list((run_dir / "rollouts").rglob("*__dry_run_*")), "full mode runs no targeted check"

    rc, out = _phase(BASELINE, ["--base", str(tmp_path / ".capevolve"), "--project", str(project),
                                "--capability", "seed_capability", "--spec", str(spec),
                                "--dry-run-state", str(project / "dry_run" / "state.json")], capsys)
    assert rc == 0, out
    assert out["run_dir"] == str(run_dir)
    base = json.loads((run_dir / "baseline.json").read_text())
    assert base["val"]["reward"] == 1.0 and base["baseline_pre_dry_run"]["reward"] == 0.0
    pre_ids = {pt["task_id"] for pt in base["baseline_pre_dry_run"]["per_task"]}
    assert pre_ids == {pt["task_id"] for pt in base["val"]["per_task"]}
    assert pre_ids == set(json.loads((run_dir / "splits.json").read_text())["val"])

    sys.path.insert(0, str(REPO / "skills" / "phases" / "report" / "scripts"))
    sys.modules.pop("run", None)
    try:
        import run as report
        assert report.main(["--run-dir", str(run_dir), "--no-dashboard"]) == 0
    finally:
        sys.path.pop(0)
        sys.modules.pop("run", None)
    summary = json.loads(capsys.readouterr().out)
    assert summary["dry_run_ran"] and summary["dry_run_verify"] == "full"
    assert summary["baseline_pre_dry_run"] == 0.0 and summary["dry_run_smoke"] is None
    assert [f["id"] for f in summary["dry_run_fixes"]] == ["F1"]
    md = (run_dir / "report.md").read_text()
    assert "## Dry run" in md and "Dry-run effect, on val" in md


def test_report_without_a_dry_run_has_null_keys(tmp_path):
    from cap_evolve import RunDir, dryrun
    rd = RunDir.create(tmp_path, ts="x")
    s = dryrun.report_summary(rd)
    assert s["dry_run_ran"] is False and s["dry_run_smoke"] is None and s["baseline_pre_dry_run"] is None


# ---- {{DRY_RUN_FINDINGS}} --------------------------------------------------------------------

def _val():
    from cap_evolve.loop import SplitResult
    return SplitResult(split="val", reward=0.5, stderr=0.0,
                       per_task=[{"task_id": "a", "reward": 0.0, "feedback": "x"}])


def test_placeholder_renders_empty_and_is_optional(tmp_path):
    from cap_evolve.harness import _focus_instructions
    with_ph = tmp_path / "with.md"
    with_ph.write_text("{{FOCUS_SUMMARY}}\n{{DRY_RUN_FINDINGS}}\nend\n")
    without = tmp_path / "without.md"
    without.write_text("{{FOCUS_SUMMARY}}\nend\n")
    out = _focus_instructions(_val(), None, "t", instructions_file=with_ph)
    assert "{{" not in out and out.rstrip().endswith("end")
    assert _focus_instructions(_val(), None, "t", instructions_file=without).rstrip().endswith("end")
    hint = "## Dry-run findings\n- x.py:1: something\n"
    assert hint in _focus_instructions(_val(), None, "t", instructions_file=with_ph, dry_run_findings=hint)
    # a template that predates the placeholder still gets the block
    assert hint in _focus_instructions(_val(), None, "t", instructions_file=without, dry_run_findings=hint)


def test_shipped_templates_carry_the_placeholder():
    for name in ("INSTRUCTIONS.md", "INSTRUCTIONS.prompt-only.md"):
        assert "{{DRY_RUN_FINDINGS}}" in (REPO / "templates" / "project" / "optimizer" / name).read_text()


def test_hints_frame_findings_by_the_editable_surface():
    from cap_evolve import dryrun
    doc = {"findings": [
        {"decision": "as_is", "editable": True, "location": ["p.txt:1"], "what_happens": "A"},
        {"decision": "as_is", "editable": False, "location": ["run.py:9"], "what_happens": "B"},
        {"decision": "fix", "editable": True, "location": ["p.txt:2"], "what_happens": "C"}]}
    text = dryrun.hints_block(doc)
    mine, theirs = text.split("do NOT edit")
    assert "A" in mine and "you may fix" in mine and "B" in theirs and "C" not in text
    assert dryrun.hints_block({"findings": []}) == "" and dryrun.hints_block(None) == ""


def test_scorer_findings_are_never_editable(tmp_path):
    from cap_evolve import dryrun
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "score.py").write_text("return 1 if a == b else 0\n")
    ctx = {"roots": {"capability": str(cap), "project": str(tmp_path)}, "original_seed": str(cap)}
    f = {"id": "S", "kind": "bug", "severity": "major", "location": ["score.py:1"], "what_happens": "x",
         "touches_scorer": True,
         "proposed_fix": {"edits": [{"file": "score.py", "old": "a == b", "new": "a.strip() == b"}]}}
    doc, problems = dryrun.validate({"findings": [f]}, ctx)
    assert not problems and doc["findings"][0]["editable"] is False
    dryrun.normalize_decisions(doc)
    assert doc["findings"][0]["decision"] == "pending"
