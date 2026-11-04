"""Optimizer feedback: every trial's feedback reaches the prompt, its per-trial cap is
configurable, and ``feedback_split: train`` shows the proposer TRAIN while the gate stays
on val."""

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "core"
EXAMPLE = REPO / "examples" / "toy_calc"
MOCK_RUN = REPO / "skills" / "optimizers" / "run-optimizer" / "scripts" / "run.py"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(EXAMPLE))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CAPEVOLVE_CORE", str(CORE))
    monkeypatch.setenv("CAPEVOLVE_TOY_DATA", str(EXAMPLE))
    monkeypatch.setenv("CAPEVOLVE_MOCK_SCRIPT", str(EXAMPLE / "mock_script.json"))
    for k in ("CAPEVOLVE_FEEDBACK_SPLIT", "CAPEVOLVE_FEEDBACK_CHARS"):
        monkeypatch.delenv(k, raising=False)


def _toy_adapter():
    import importlib.util
    spec = importlib.util.spec_from_file_location("toy_calc_adapter_fb", EXAMPLE / "adapter.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.Adapter()


def _val(per_task, split="val"):
    from cap_evolve.loop import SplitResult
    return SplitResult.from_dict({"split": split, "reward": 0.5, "stderr": 0.1, "per_task": per_task})


def test_fmt_lists_every_trial_with_its_reward():
    from cap_evolve import harness
    pt = {"task_id": "a", "reward": 0.5, "feedback": "last only",
          "raw": {"trial_feedback": ["[trial 0, reward 1.000] fine", "[trial 1, reward 0.000] broke X"]}}
    line = harness._fmt(pt)
    assert "2 trials" in line and "fine" in line and "broke X" in line


def test_fmt_falls_back_to_last_feedback_and_caps_each_trial():
    from cap_evolve import harness
    assert harness._fmt({"task_id": "a", "reward": 0.0, "feedback": "only"}).endswith("only")
    long = "x" * 1000
    capped = harness._fmt({"task_id": "a", "reward": 0.0, "feedback": long}, max_chars=50)
    assert "x" * 50 + " …[truncated]" in capped and "x" * 51 not in capped
    assert "x" * 1000 in harness._fmt({"task_id": "a", "reward": 0.0, "feedback": long}, max_chars=0)


def test_feedback_chars_and_split_resolve_from_env(monkeypatch):
    from cap_evolve import harness
    assert harness.resolve_feedback_chars() == 400
    assert harness.resolve_feedback_split() == "val"
    monkeypatch.setenv("CAPEVOLVE_FEEDBACK_CHARS", "0")
    monkeypatch.setenv("CAPEVOLVE_FEEDBACK_SPLIT", "train")
    assert harness.resolve_feedback_chars() == 0
    assert harness.resolve_feedback_split() == "train"
    monkeypatch.setenv("CAPEVOLVE_FEEDBACK_SPLIT", "test")
    with pytest.raises(ValueError):
        harness.resolve_feedback_split()


def test_focus_instructions_name_the_feedback_split():
    from cap_evolve import harness
    per = [{"task_id": "a", "reward": 0.0, "feedback": "missed"}]
    on_val = harness._focus_instructions(_val(per), None, "all")
    on_train = harness._focus_instructions(_val(per, "train"), None, "all")
    assert "Current val reward" in on_val and "you are not shown" not in on_val
    assert "Current train reward" in on_train and "held-out val split you are not shown" in on_train


def test_evaluate_records_every_trials_feedback(tmp_path):
    from cap_evolve import Budget, RunDir, harness
    adapter = _toy_adapter()
    seed = tmp_path / "seed"
    shutil.copytree(EXAMPLE / "capability", seed)
    run_dir = RunDir.create(tmp_path / ".capevolve", ts="fb", budget=Budget(max_iterations=1, stall=3))
    harness.ensure_splits(adapter, run_dir, seed=0)
    res = harness.evaluate_candidate(adapter, seed, run_dir=run_dir, split="val", n_trials=2, tag="seed")
    pt = res.per_task[0]
    assert len(pt["raw"]["trial_feedback"]) == 2
    # the same list is rebuilt from disk on resume
    back = harness.split_result_from_rollouts(run_dir, "seed", "val")
    got = {p["task_id"]: p["raw"]["trial_feedback"] for p in back.per_task}
    assert got[pt["task_id"]] == pt["raw"]["trial_feedback"]


def test_feedback_split_train_shows_train_and_gates_on_val(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from cap_evolve import Budget, RunDir, harness
    monkeypatch.setenv("CAPEVOLVE_FEEDBACK_SPLIT", "train")
    adapter = _toy_adapter()
    seed = tmp_path / "seed"
    shutil.copytree(EXAMPLE / "capability", seed)
    run_dir = RunDir.create(tmp_path / ".capevolve", ts="fs", budget=Budget(max_iterations=1, stall=3))
    splits = harness.ensure_splits(adapter, run_dir, seed=0)
    base = harness.baseline(adapter, seed, run_dir=run_dir)
    optimizer = harness.optimizer_from_command(
        ["python3", str(MOCK_RUN), "--name", "mock", "--workdir", "{workdir}", "--prompt", "{prompt}"])
    harness.hill_climb_loop(adapter, run_dir=run_dir, optimizer=optimizer, current_val=base,
                            focus="all", max_iterations=1,
                            gate_kwargs={"mode": "significant", "k_se": 1.0},
                            algorithm="hill-climb", capabilities=["system-prompt"])
    work = run_dir.root / "work" / "cand_0001"
    shown = {p.name.split("__")[0] for p in (work / "trajectories").iterdir()}
    assert shown and shown <= set(splits.train) and not shown & set(splits.val)
    assert "Current train reward" in (work / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    evals = [json.loads(l) for l in run_dir.events_path.read_text().splitlines() if l.strip()]
    assert any(e.get("kind") == "evaluate" and e.get("split") == "val" and e.get("tag") == "cand_0001"
               for e in evals)
