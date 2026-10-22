"""Contract: the dry run fixes what the optimizer may edit without asking, never touches the user's
seed, asks before changing anything else, checks fixes only on train, reverts a harmful editable
fix, freezes the split once for baseline, and blocks an unattended run on a major non-editable issue.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

from cap_evolve import RunDir, dryrun
from cap_evolve.skillcheck import Checker, import_run, quiet

# The "agent": solves a task when the prompt carries [SOLVE]; [BREAK] makes it fail. The flow then
# runs a "fixer" that corrupts every answer it touches unless flow.cfg says to skip it — a flow flaw
# in system code (adapters/flow.cfg is outside the capability), invisible as a failure site.
ADAPTER = '''
from pathlib import Path
from cap_evolve import CapabilityAdapter
from cap_evolve.types import Task, Rollout, Score

HERE = Path(__file__).resolve().parent


class Adapter(CapabilityAdapter):
    def tasks(self, split):
        return [Task(id=f"t{i:02d}", input=str(i), target=str(i)) for i in range(16)]

    def run_target(self, task, ctx, *, seed=0):
        prompt = (Path(ctx) / "prompt.txt").read_text()
        out = task.target if "[SOLVE]" in prompt and "[BREAK]" not in prompt else "?"
        if "fixer: always" in (HERE / "flow.cfg").read_text() and int(task.input) % 2 == 0:
            out = out + " (fixed)"
        return Rollout(task_id=task.id, output=out)

    def score(self, task, rollout):
        return Score(task_id=task.id, reward=1.0 if rollout.output == task.target else 0.0)
'''


def project(tmp: Path) -> Path:
    proj = tmp / ".capevolve" / "project"
    (proj / "adapters").mkdir(parents=True)
    (proj / "adapters" / "adapter.py").write_text(ADAPTER, encoding="utf-8")
    (proj / "adapters" / "flow.cfg").write_text("fixer: always\n", encoding="utf-8")
    (proj / "seed").mkdir()
    (proj / "seed" / "prompt.txt").write_text("Answer the question.\n", encoding="utf-8")
    (proj / "capevolve.yaml").write_text("capabilities: [system-prompt]\ndry_run: true\n"
                                         "dry_run_verify: targeted\n", encoding="utf-8")
    return proj


def findings(proj: Path, run_dir: RunDir, *, harmful: bool = False) -> None:
    train = run_dir.read_splits().train
    odd = [t for t in train if int(t[1:]) % 2][:2]
    even = [t for t in train if not int(t[1:]) % 2][:2]
    doc = {"findings": [] if harmful else [
        {"id": "F1", "kind": "flow", "severity": "major", "location": ["seed/prompt.txt:1"],
         "what_happens": "the prompt never asks the agent to solve the task",
         "proposed_fix": {"summary": "ask for the answer",
                          "edits": [{"file": "prompt.txt", "old": "Answer the question.",
                                     "new": "Answer the question. [SOLVE]"}]},
         "verify_cases": [{"task_id": t, "reason": "goes through the prompt"} for t in odd],
         "editable": False},        # wrong on purpose: editable is computed, not trusted
        {"id": "F2", "kind": "flow", "severity": "major", "location": ["adapters/flow.cfg:1"],
         "what_happens": "the fixer rewrites answers even when they are already right",
         "proposed_fix": {"summary": "skip the fixer on a top score",
                          "edits": [{"file": "adapters/flow.cfg", "old": "fixer: always",
                                     "new": "fixer: only-below-max"}]},
         "verify_cases": [{"task_id": t, "reason": "even id: the fixer runs"} for t in even],
         "decision": "fix", "approved_by": "auto"},   # an agent cannot approve outside the surface
    ]}
    if harmful:
        doc["findings"].append(
            {"id": "F2", "kind": "flow", "severity": "minor", "location": ["adapters/flow.cfg:1"],
             "what_happens": "the fixer rewrites answers even when they are already right"})
    if harmful:
        doc["findings"].append(
            {"id": "F3", "kind": "bug", "severity": "minor", "location": ["seed/prompt.txt:1"],
             "what_happens": "a harmful edit (for the check)",
             "proposed_fix": {"edits": [{"file": "prompt.txt", "old": "question.",
                                         "new": "question. [BREAK]"}]},
             "verify_cases": [{"task_id": t, "reason": "x"} for t in odd]})
    (proj / "dry_run" / "findings.json").write_text(json.dumps(doc), encoding="utf-8")


def main() -> int:
    c = Checker("dry-run")
    run = import_run()
    c.require_main(run)

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        proj = project(tmp)
        base = str(tmp / ".capevolve")
        with quiet() as buf:
            rc = run.main(["prepare", "--project", str(proj), "--capability", str(proj / "seed"),
                           "--base", base, "--run-ts", "x"])
        out = json.loads(buf.getvalue())
        c.check(rc == 0, f"prepare failed: {out}")
        run_dir = RunDir.open(Path(out["run_dir"]))
        frozen = run_dir.read_splits()
        findings(proj, run_dir)

        with quiet() as buf:
            rc = run.main(["apply", "--project", str(proj)])
        res = json.loads(buf.getvalue())
        c.check(rc == 2 and res.get("pending") == ["F2"],
                f"a non-editable fix was not held for a human: rc={rc} {res}",
                note="non-editable fix waits for a human; an agent-written approval does not count")
        doc = json.loads((proj / "dry_run" / "findings.json").read_text())
        f1 = doc["findings"][0]
        c.check(f1["editable"] and f1["decision"] == "fix" and f1["approved_by"] == "auto",
                f"editable fix not auto-approved: {f1}", note="editable fix approved automatically")

        with quiet():
            run.main(["decide", "--project", str(proj), "--id", "F2", "--choice", "fix"])
        with quiet() as buf:
            rc = run.main(["apply", "--project", str(proj)])
        res = json.loads(buf.getvalue())
        c.check(rc == 0 and res["status"] == "ready", f"apply did not finish: {res}")
        c.check((proj / "seed" / "prompt.txt").read_text() == "Answer the question.\n",
                "the user's seed dir was modified", note="original seed untouched; fix went to fixed_seed")
        c.check("[SOLVE]" in (proj / "dry_run" / "fixed_seed" / "prompt.txt").read_text(),
                "fixed_seed lacks the editable fix")
        c.check("only-below-max" in (proj / "adapters" / "flow.cfg").read_text(),
                "approved system fix not applied in place", note="approved non-editable fix applied in place")
        doc = json.loads((proj / "dry_run" / "findings.json").read_text())
        verdicts = {f["id"]: f["verify"]["verdict"] for f in doc["findings"]}
        c.check(verdicts == {"F1": "helped", "F2": "helped"}, f"unexpected verdicts: {verdicts}",
                note="targeted check judges each fix on its own cases")
        used = {p.name.split("__")[0] for p in (run_dir.rollouts).rglob("*__dry_run_*")}
        c.check(used <= set(frozen.train) and not (run_dir.rollouts / "val").exists()
                and not (run_dir.rollouts / "test").exists(),
                "the targeted check touched val or test", note="targeted check uses train only")
        c.check(run_dir.read_splits().train == frozen.train, "split re-partitioned")
        hints = dryrun.hints_for_run(run_dir)
        c.check(hints == "", f"fixed findings leaked into the optimizer hints: {hints!r}")

        # A harmful editable fix is reverted automatically and becomes a hint. The seed now solves
        # the odd tasks, so breaking them is visible.
        (proj / "adapters" / "flow.cfg").write_text("fixer: always\n", encoding="utf-8")
        (proj / "seed" / "prompt.txt").write_text("Answer the question. [SOLVE]\n", encoding="utf-8")
        with quiet() as buf:
            run.main(["prepare", "--project", str(proj), "--capability", str(proj / "seed"),
                      "--base", base, "--run-ts", "y"])
        rd2 = RunDir.open(Path(json.loads(buf.getvalue())["run_dir"]))
        findings(proj, rd2, harmful=True)
        with quiet():
            run.main(["decide", "--project", str(proj), "--id", "F2", "--choice", "as_is"])
        with quiet() as buf:
            rc = run.main(["apply", "--project", str(proj)])
        doc = json.loads((proj / "dry_run" / "findings.json").read_text())
        f3 = next(f for f in doc["findings"] if f["id"] == "F3")
        c.check(rc == 0 and f3.get("reverted") and f3["decision"] == "as_is",
                f"harmful editable fix not reverted: rc={rc} {f3}",
                note="harmful editable fix reverted automatically")
        c.check(not (proj / "dry_run" / "fixed_seed").exists()
                and json.loads((proj / "dry_run" / "state.json").read_text())["seed_dir"] is None,
                "baseline would still score the reverted fix", note="no fix left -> baseline scores the original seed")
        c.check("work with it" in dryrun.hints_for_run(rd2) and "you may fix" in dryrun.hints_for_run(rd2),
                "as-is findings are not framed by who may act on them",
                note="as-is findings become optimizer hints, framed by the editable surface")

        # Unattended + stop: a major non-editable finding blocks before baseline.
        (proj / "seed" / "prompt.txt").write_text("Answer the question.\n", encoding="utf-8")
        with quiet() as buf:
            run.main(["prepare", "--project", str(proj), "--capability", str(proj / "seed"),
                      "--base", base, "--run-ts", "z"])
        rd3 = RunDir.open(Path(json.loads(buf.getvalue())["run_dir"]))
        findings(proj, rd3)
        with quiet() as buf:
            rc = run.main(["apply", "--project", str(proj), "--unattended"])
        res = json.loads(buf.getvalue())
        c.check(rc == 3 and res["status"] == "blocked" and "BLOCKED" in res["message"],
                f"unattended stop did not block: rc={rc} {res}",
                note="unattended stop blocks on a major non-editable finding")
        c.check("fixer: always" in (proj / "adapters" / "flow.cfg").read_text(),
                "an unattended run applied a non-editable fix")

    return c.emit()


if __name__ == "__main__":
    sys.exit(main())
