"""Dry run: a pre-baseline inspection of the system under optimization.

Design: ``docs/specs/2026-10-19-dry-run-design.md``. The coding agent reads the whole system
(capability, agent/runner code, orchestration, scorer) and writes ``findings.json``; this module
does the mechanical part around that judgement:

* resolve what the stage reads and snapshot the ORIGINAL seed (``dry_run/original_seed``);
* validate the findings and decide, mechanically, which ones are inside the editable surface
  (a finding's ``editable`` is computed here, never trusted from the agent);
* apply fixes: editable ones to a COPY of the seed (``dry_run/fixed_seed``, the user's seed dir is
  never touched), approved non-editable ones in place, each recorded with its diff and approver;
* the targeted smoke check: before/after on a few TRAIN cases per fix (the feedback stratum in
  ``split_mode: pooled``), attribution re-runs for shared cases, automatic revert of a harmful
  editable fix;
* hand the result to baseline (``dry_run/state.json``) and to the optimizer (as_is findings become
  hints through ``{{DRY_RUN_FINDINGS}}``).

Fixes are exact-string edits (``{"file", "old", "new"}``), not patches: they apply and revert in
pure Python, and the unified diff shown to the user is rendered from them.
"""

from __future__ import annotations

import contextlib
import difflib
import fnmatch
import json
import os
import random
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .cache import hash_candidate_dir
from .rundir import RunDir, _atomic_write

KINDS = ("flow", "bug")
SEVERITIES = ("major", "minor")
DECISIONS = ("fix", "as_is", "pending")
APPROVERS = ("auto", "user")
VERIFY_MODES = ("off", "targeted", "full")
UNATTENDED_POLICIES = ("stop", "continue")

#: Control tasks added to the targeted check: cases the fixes should not affect.
N_CONTROLS = 2
#: A case "got worse" when its mean reward over trials drops by more than this.
_EPS = 1e-9

#: Candidate ids / rollout tags the dry run writes into the run dir.
TAG_BEFORE = "dry_run_before"
TAG_AFTER = "dry_run_after"
CAND_ORIGINAL = "dry_run_original"
CAND_FIXED = "dry_run_fixed"
CAND_PRE = "seed_pre_dry_run"       # baseline's ``full`` comparison scores the original seed under this id


class DryRunError(RuntimeError):
    """The findings or the fix set cannot be applied as written."""


@dataclass(frozen=True)
class DryRunConfig:
    enabled: bool = False
    verify: str = "targeted"
    unattended: str = "stop"
    cases_per_fix: int = 3
    max_cases: int = 12

    @classmethod
    def from_spec(cls, spec: dict | None) -> DryRunConfig:
        spec = spec or {}
        on = spec.get("dry_run", False)
        enabled = on is True or str(on).strip().lower() in ("true", "1", "yes", "on")
        verify = str(spec.get("dry_run_verify") or "targeted").strip().lower()
        if verify in ("false", "none", "no"):
            verify = "off"
        if verify not in VERIFY_MODES:
            raise ValueError(f"dry_run_verify must be one of {VERIFY_MODES}, got {verify!r}")
        policy = str(spec.get("dry_run_unattended") or "stop").strip().lower()
        if policy not in UNATTENDED_POLICIES:
            raise ValueError(f"dry_run_unattended must be one of {UNATTENDED_POLICIES}, got {policy!r}")
        return cls(enabled=enabled, verify=verify, unattended=policy,
                   cases_per_fix=max(0, int(spec.get("dry_run_verify_cases_per_fix") or 3)),
                   max_cases=max(0, int(spec.get("dry_run_verify_max_cases") or 12)))


# ---- paths -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def for_project(cls, project) -> Paths:
        return cls(Path(project).resolve() / "dry_run")

    @property
    def findings(self) -> Path:
        return self.root / "findings.json"

    @property
    def markdown(self) -> Path:
        return self.root / "FINDINGS.md"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def context(self) -> Path:
        return self.root / "context.json"

    @property
    def original_seed(self) -> Path:
        return self.root / "original_seed"

    @property
    def fixed_seed(self) -> Path:
        return self.root / "fixed_seed"

    @property
    def agent_workdir(self) -> Path:
        return self.root / "agent"


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(Path(path), json.dumps(data, indent=2) + "\n")


def read_state(project) -> dict:
    return _read_json(Paths.for_project(project).state, {}) or {}


def _write_state(paths: Paths, state: dict) -> None:
    _write_json(paths.state, state)


# ---- prepare ---------------------------------------------------------------------------------

def _list_files(d: Path, limit: int = 400) -> list[str]:
    if not d.is_dir():
        return [str(d)] if d.is_file() else []
    out = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts:
            out.append(str(p))
            if len(out) >= limit:
                break
    return out


def resolve_capability_dir(project, capability) -> Path:
    """The seed capability dir, resolved the way baseline resolves it (as given, else project-relative)."""
    cap = Path(str(capability))
    if not cap.exists():
        cand = Path(project) / str(capability)
        if cand.exists():
            cap = cand
    return cap.resolve()


def prepare(project, spec: dict, capability, *, run_dir: RunDir, allowed_ids: list | None = None) -> dict:
    """Snapshot the original seed and write ``context.json`` (what the stage reads, where it starts).

    The search starts at what the project already records (adapter, ``runner_repo_path``,
    ``capability_sources``, the capability) and the agent follows imports and calls from there.
    ``allowed_ids`` are the task ids the targeted check may pick from (train, or F when pooled).
    """
    from .specfile import resolve_project_path

    project = Path(project).resolve()
    paths = Paths.for_project(project)
    paths.root.mkdir(parents=True, exist_ok=True)
    cap = resolve_capability_dir(project, capability)
    cap.mkdir(parents=True, exist_ok=True)
    if paths.original_seed.exists():
        shutil.rmtree(paths.original_seed)
    shutil.copytree(cap, paths.original_seed)

    repo = str(spec.get("runner_repo_path") or "").strip()
    sources = spec.get("capability_sources") or []
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    caps = spec.get("capabilities") or []
    if isinstance(caps, str):
        caps = [c.strip() for c in caps.split(",") if c.strip()]
    cfg = DryRunConfig.from_spec(spec)
    context = {
        "seed_hash": hash_candidate_dir(cap),
        "roots": {
            "capability": str(cap),
            "project": str(project),
            "adapter": str(project / "adapters"),
            "runner_repo": str(resolve_project_path(project, repo).resolve()) if repo else "",
        },
        "capabilities": [str(c) for c in caps],
        "editable_surface": ("files under roots.capability, except protected_paths; everything else "
                             "(agent/runner code, the adapter, the scorer, the task data) is NOT editable"),
        "files": {
            "capability": _list_files(cap),
            "adapter": _list_files(project / "adapters"),
            "capability_sources": [str(resolve_project_path(project, s)) for s in sources],
            "project_md": str(project / "PROJECT.md"),
        },
        "verify": cfg.verify,
        "verify_cases_per_fix": cfg.cases_per_fix,
        "verify_max_cases": cfg.max_cases,
        "verify_split": _check_split(run_dir),
        "verify_candidate_ids": list(allowed_ids or []),
        "run_dir": str(run_dir.root.resolve()),
        "findings_path": str(paths.findings),
    }
    _write_json(paths.context, context)
    state = read_state(project)
    state.update(run_dir=str(run_dir.root.resolve()), ready=False, prepared_at=time.time(),
                 original_seed=str(paths.original_seed), capability=str(cap),
                 seed_hash=context["seed_hash"], verify=cfg.verify)
    state.pop("applied_run_dir", None)
    _write_state(paths, state)
    run_dir.log_event("dry_run_prepare", capability=str(cap), verify=cfg.verify,
                      n_capability_files=len(context["files"]["capability"]))
    return context


def _check_split(run_dir: RunDir) -> str | None:
    """Where the targeted check samples: train. ``None`` under ``split_mode: pooled``.

    Pooled mode partitions train + val into F and C once, stratified by the SEED's pass rates, which
    exist only after baseline. Drawing cases before that would either fix an unstratified partition
    or touch tasks that may land in the hidden confirm stratum, so the targeted check does not run
    there (``dry_run_verify: full`` does, on F)."""
    try:
        from . import gatecfg
        return None if gatecfg.GateConfig.for_run(run_dir).split_mode == "pooled" else "train"
    except Exception:  # noqa: BLE001
        return "train"


def check_ids(run_dir: RunDir) -> tuple[str | None, list[str]]:
    """(split, ids) the targeted check may draw cases from. Never val, never test."""
    split = _check_split(run_dir)
    if split is None:
        return None, []
    return split, [str(t) for t in run_dir.read_splits().train]


# ---- validation ------------------------------------------------------------------------------

def _inside(p: Path, d: Path) -> bool:
    try:
        p.resolve().relative_to(d.resolve())
        return True
    except ValueError:
        return False


def resolve_edit_path(file: str, roots: dict) -> Path:
    """An edit's ``file``: absolute as given; relative is tried against the capability, the project and
    the cwd, first existing wins (else project-relative)."""
    p = Path(str(file))
    if p.is_absolute():
        return p
    for base in (roots.get("capability"), roots.get("project"), os.getcwd(), roots.get("runner_repo")):
        if base and (Path(base) / p).exists():
            return (Path(base) / p).resolve()
    return (Path(roots.get("project") or ".") / p).resolve()


def _protected(rel: str, spec: dict | None) -> bool:
    from .harness import parse_protected_paths
    pats = parse_protected_paths((spec or {}).get("protected_paths")) or ()
    return any(fnmatch.fnmatch(rel, pat) for pat in pats)


def _render_diff(path: Path, before: str, after: str, label: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{label}", tofile=f"b/{label}"))


def validate(findings_doc: dict, context: dict, spec: dict | None = None) -> tuple[dict, list[str]]:
    """Normalize ``findings.json`` in place-ish (returns a new dict) and list every problem.

    Computes ``editable`` from the edit locations (inside the capability dir and not protected) and
    forces ``touches_scorer`` findings out of the editable surface: the optimizer may never change
    what is measured. Renders each fix's diff against the ORIGINAL seed / the file in place.
    """
    roots = context.get("roots") or {}
    cap = Path(roots.get("capability") or ".")
    orig = Path(context.get("original_seed") or Paths.for_project(roots.get("project") or ".").original_seed)
    problems: list[str] = []
    doc = dict(findings_doc or {})
    out = []
    seen = set()
    for i, raw in enumerate(doc.get("findings") or []):
        f = dict(raw or {})
        fid = str(f.get("id") or f"F{i + 1}")
        f["id"] = fid
        where = f"finding {fid}"
        if fid in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(fid)
        if f.get("kind") not in KINDS:
            problems.append(f"{where}: kind must be one of {KINDS}")
        if f.get("severity") not in SEVERITIES:
            problems.append(f"{where}: severity must be one of {SEVERITIES}")
        loc = f.get("location")
        f["location"] = [str(x) for x in (loc if isinstance(loc, list) else [loc] if loc else [])]
        if not f["location"]:
            problems.append(f"{where}: location (file:line) is required")
        if not str(f.get("what_happens") or "").strip():
            problems.append(f"{where}: what_happens (the concrete runtime effect) is required")
        f["touches_scorer"] = bool(f.get("touches_scorer"))
        dec = f.get("decision") or "pending"
        if dec not in DECISIONS:
            problems.append(f"{where}: decision must be one of {DECISIONS}")
        f["decision"] = dec
        if f.get("approved_by") not in (None, *APPROVERS):
            problems.append(f"{where}: approved_by must be one of {APPROVERS} or null")

        fix = dict(f.get("proposed_fix") or {})
        edits = []
        diffs = []
        editable = True
        for j, e in enumerate(fix.get("edits") or []):
            if not isinstance(e, dict) or not e.get("file") or not isinstance(e.get("old"), str) \
                    or not isinstance(e.get("new"), str) or not e["old"]:
                problems.append(f"{where}: edit {j} needs file, a non-empty old string and new")
                continue
            path = resolve_edit_path(e["file"], roots)
            in_cap = _inside(path, cap)
            rel = str(path.resolve().relative_to(cap.resolve())) if in_cap else ""
            if not in_cap or _protected(rel, spec):
                editable = False
            src = (orig / rel) if in_cap and (orig / rel).exists() else path
            text = src.read_text(encoding="utf-8") if src.exists() else None
            if text is None:
                problems.append(f"{where}: edit {j}: file not found: {path}")
            elif text.count(e["old"]) != 1 and not (e["new"] and text.count(e["new"]) == 1):
                problems.append(f"{where}: edit {j}: `old` must occur exactly once in {path} "
                                f"(found {text.count(e['old'])} times)")
            else:
                after = text.replace(e["old"], e["new"], 1) if e["old"] in text else text
                before = text if e["old"] in text else text.replace(e["new"], e["old"], 1)
                diffs.append(_render_diff(path, before, after, rel or str(path)))
            edits.append({"file": str(path), "rel": rel, "old": e["old"], "new": e["new"]})
        if not edits:
            editable = bool(f.get("editable", False)) and not f["touches_scorer"]
        if f["touches_scorer"]:
            editable = False
        if "editable" in raw and bool(raw.get("editable")) != editable:
            f.setdefault("notes", []).append(
                f"editable recomputed as {editable} from the fix locations (agent said {raw.get('editable')})")
        f["editable"] = editable
        fix["edits"] = edits
        fix["diff"] = "".join(diffs)
        fix.setdefault("summary", "")
        f["proposed_fix"] = fix
        cases = []
        for c in f.get("verify_cases") or []:
            c = c if isinstance(c, dict) else {"task_id": c}
            if c.get("task_id") is not None:
                cases.append({"task_id": str(c["task_id"]), "reason": str(c.get("reason") or "")})
        f["verify_cases"] = cases
        out.append(f)
    doc["findings"] = out
    doc["controls"] = [
        {"task_id": str(c.get("task_id") if isinstance(c, dict) else c),
         "reason": str((c.get("reason") if isinstance(c, dict) else "") or "")}
        for c in (doc.get("controls") or [])]
    doc["seed_hash"] = context.get("seed_hash")
    doc["version"] = 1
    return doc, problems


def has_fix(f: dict) -> bool:
    return bool((f.get("proposed_fix") or {}).get("edits"))


def normalize_decisions(doc: dict) -> dict:
    """Apply the approval rule: inside the editable surface no human is needed; outside it one is.

    * editable with a fix -> ``fix`` / ``auto`` (applied without a question, listed with its diff);
    * editable without a fix -> ``as_is`` (becomes an optimizer hint);
    * non-editable -> a ``fix`` only a human approved (``approved_by: user``) stands; anything else
      is ``pending`` until a human decides;
    * a fix the targeted check reverted stays reverted.
    """
    for f in doc.get("findings") or []:
        if f.get("reverted"):
            f["decision"] = "as_is"
            continue
        if f.get("editable"):
            if f["decision"] == "as_is" and f.get("approved_by") == "user":
                continue                                    # the user chose to leave it
            f["decision"], f["approved_by"] = ("fix", "auto") if has_fix(f) else ("as_is", "auto")
        else:
            if f["decision"] == "fix" and f.get("approved_by") != "user":
                f["decision"], f["approved_by"] = "pending", None
            if f["decision"] == "fix" and not has_fix(f):
                f["decision"] = "as_is"
    return doc


def strip_agent_approvals(doc: dict) -> dict:
    """An unattended agent cannot approve anything outside the editable surface: whatever decision it
    wrote for a non-editable finding is reset to ``pending`` (judged on the computed ``editable``)."""
    for f in doc.get("findings") or []:
        if not f.get("editable"):
            f["decision"], f["approved_by"] = "pending", None
    doc["written_by"] = "agent"
    return doc


def pending(doc: dict) -> list[dict]:
    return [f for f in doc.get("findings") or [] if f.get("decision") == "pending"]


def decide(project, fid: str, choice: str) -> dict:
    """Record a human's answer for one finding (``fix`` / ``as_is``), or for a harmed non-editable fix
    after the targeted check (``keep`` / ``revert``)."""
    paths = Paths.for_project(project)
    doc = _read_json(paths.findings)
    if doc is None:
        raise DryRunError(f"no findings at {paths.findings}")
    f = next((x for x in doc.get("findings") or [] if str(x.get("id")) == fid), None)
    if f is None:
        raise DryRunError(f"no finding {fid!r}")
    if choice in ("fix", "as_is"):
        if choice == "fix" and not has_fix(f):
            raise DryRunError(f"finding {fid} has no proposed edits to apply")
        f["decision"], f["approved_by"] = choice, "user"
        if not pending(doc):
            doc.pop("written_by", None)             # every open question now has a human's answer
    elif choice in ("keep", "revert"):
        v = f.get("verify") or {}
        if not v.get("review_needed"):
            raise DryRunError(f"finding {fid} has no pending post-check review")
        v["review_needed"] = False
        v["review"] = choice
        f["verify"] = v
        if choice == "revert":
            f["revert_requested"] = True
    else:
        raise DryRunError("choice must be fix | as_is | keep | revert")
    _write_json(paths.findings, doc)
    return f


# ---- applying fixes --------------------------------------------------------------------------

def _apply_text(text: str, old: str, new: str, where: str) -> str:
    if text.count(old) == 1:
        return text.replace(old, new, 1)
    raise DryRunError(f"cannot apply edit to {where}: `old` occurs {text.count(old)} times")


def touches_seed(f: dict) -> bool:
    """Does any edit of this fix land inside the capability dir (so in the seed copy)?"""
    return any(e.get("rel") for e in (f.get("proposed_fix") or {}).get("edits") or [])


def build_fixed_seed(paths: Paths, fixes: list[dict]) -> Path:
    """``fixed_seed`` = the original seed + every edit the given fixes make inside the capability dir,
    rebuilt from scratch every time (a revert is a rebuild without that fix, never an inverse edit).

    The user's seed dir is never modified: an approved fix to a protected file inside the capability
    lands here too, next to the editable ones."""
    if paths.fixed_seed.exists():
        shutil.rmtree(paths.fixed_seed)
    shutil.copytree(paths.original_seed, paths.fixed_seed)
    apply_to_copy(paths.fixed_seed, fixes)
    return paths.fixed_seed


def apply_to_copy(dest: Path, fixes: list[dict]) -> None:
    """Apply the fixes' edits that live inside the capability dir to the seed copy ``dest``."""
    for f in fixes:
        for e in f["proposed_fix"]["edits"]:
            if not e.get("rel"):
                continue
            target = dest / e["rel"]
            target.write_text(_apply_text(target.read_text(encoding="utf-8"), e["old"], e["new"],
                                          str(target)), encoding="utf-8")


class InPlace:
    """Approved non-editable fixes, applied to the real files and togglable for the before/after runs.

    Only edits OUTSIDE the capability dir are applied in place (edits inside it go to the seed copy,
    see ``apply_to_copy``).

    Each applied fix records every file's content before and after it, so a revert restores the
    exact bytes when nothing else touched the file since, and falls back to an inverse string edit
    otherwise. The record lives in ``state.json`` so a later session (or ``--resume``) knows what is
    currently applied."""

    def __init__(self, paths: Paths, state: dict):
        self.paths = paths
        self.state = state
        self.rec: dict = state.setdefault("in_place", {})

    def applied(self, fid: str) -> bool:
        return bool((self.rec.get(fid) or {}).get("applied"))

    def apply(self, f: dict) -> None:
        fid = f["id"]
        if self.applied(fid):
            return
        files = {}
        for e in _outside(f):
            p = Path(e["file"])
            before = p.read_text(encoding="utf-8")
            after = _apply_text(before, e["old"], e["new"], str(p))
            p.write_text(after, encoding="utf-8")
            files.setdefault(str(p), {"before": before})["after"] = after
        self.rec[fid] = {"applied": True, "files": files}
        self._save()

    def revert(self, f: dict) -> None:
        fid = f["id"]
        if not self.applied(fid):
            return
        files = (self.rec.get(fid) or {}).get("files") or {}
        for e in reversed(_outside(f)):
            p = Path(e["file"])
            cur = p.read_text(encoding="utf-8")
            snap = files.get(str(p)) or {}
            if snap.get("after") == cur:
                p.write_text(snap["before"], encoding="utf-8")
                snap["after"] = snap["before"]      # later edits on the same file are now no-ops
            elif e["new"] and cur.count(e["new"]) == 1:
                p.write_text(cur.replace(e["new"], e["old"], 1), encoding="utf-8")
            elif e["old"] in cur:
                pass                                 # already back to the original text
            else:
                raise DryRunError(f"cannot revert {fid} in {p}: the file changed since it was applied")
        self.rec[fid] = {"applied": False}
        self._save()

    def set(self, wanted: set, fixes: list[dict]) -> None:
        for f in fixes:
            if f["id"] not in wanted:
                self.revert(f)
        for f in fixes:
            if f["id"] in wanted:
                self.apply(f)

    def _save(self) -> None:
        _write_state(self.paths, self.state)


def _outside(f: dict) -> list[dict]:
    return [e for e in (f.get("proposed_fix") or {}).get("edits") or [] if not e.get("rel")]


@contextlib.contextmanager
def _no_ledger():
    """The dry-run evals must not pool into the evidence ledger: a system-code fix leaves the capability
    bytes (the ledger key) unchanged while changing what they measure."""
    old = os.environ.get("CAPEVOLVE_EVAL_LEDGER")
    os.environ["CAPEVOLVE_EVAL_LEDGER"] = "0"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("CAPEVOLVE_EVAL_LEDGER", None)
        else:
            os.environ["CAPEVOLVE_EVAL_LEDGER"] = old


def _per_task(result) -> dict:
    from .loop import has_valid_trials
    return {str(pt.get("task_id")): float(pt.get("reward") or 0.0)
            for pt in (result.per_task or []) if has_valid_trials(pt)}


def select_cases(doc: dict, fixes: list[dict], allowed: list[str], cfg: DryRunConfig,
                 seed: int = 0) -> tuple[dict, list[str], list[str]]:
    """Per-fix cases (only from ``allowed``), the controls, and notes on what was dropped or short."""
    allow = set(allowed)
    notes: list[str] = []
    per_fix: dict[str, list[str]] = {}
    total: list[str] = []
    order = sorted(fixes, key=lambda f: 0 if f.get("severity") == "major" else 1)
    for f in order:
        picked = []
        for c in f.get("verify_cases") or []:
            t = c["task_id"]
            if t not in allow:
                notes.append(f"{f['id']}: case {t} is not in the check split — dropped (val and test "
                             "are never used here)")
                continue
            if t in picked or len(picked) >= cfg.cases_per_fix:
                continue
            if t not in total and len(total) >= cfg.max_cases:
                notes.append(f"{f['id']}: case {t} dropped (dry_run_verify_max_cases={cfg.max_cases})")
                continue
            picked.append(t)
            if t not in total:
                total.append(t)
        if len(picked) < cfg.cases_per_fix:
            notes.append(f"{f['id']}: {len(picked)} relevant case(s) checked (wanted {cfg.cases_per_fix})")
        per_fix[f["id"]] = picked
    controls = [c["task_id"] for c in doc.get("controls") or []
                if c["task_id"] in allow and c["task_id"] not in total][:N_CONTROLS]
    if len(controls) < N_CONTROLS:
        rest = sorted(t for t in allow if t not in total and t not in controls)
        random.Random(seed).shuffle(rest)
        auto = rest[:N_CONTROLS - len(controls)]
        if auto:
            notes.append(f"control task(s) auto-picked: {', '.join(auto)}")
        controls += auto
    return per_fix, controls, notes


def _eval(adapter, run_dir: RunDir, cand: Path, split: str, ids: list[str], tag: str, n_trials: int,
          workers) -> dict:
    from .harness import evaluate_candidate
    with _no_ledger():
        res = evaluate_candidate(adapter, cand, run_dir=run_dir, split=split, n_trials=n_trials,
                                 tag=tag, ids=list(ids), fixed_seeds=True, trial_offset=0,
                                 workers=workers)
    return _per_task(res)


def targeted_check(adapter, run_dir: RunDir, paths: Paths, doc: dict, inplace: InPlace,
                   cfg: DryRunConfig, *, n_trials: int, workers=None) -> dict:
    """Run before → apply → after on each fix's train cases + controls, judge each fix, attribute harm.

    Returns a summary; each fix's ``verify`` block is written into ``doc``. The caller acts on harm.
    """
    from .rundir import BudgetExhausted

    fixes = [f for f in doc["findings"] if f["decision"] == "fix"]
    measured = [f for f in fixes if not f["touches_scorer"]]
    split, allowed = check_ids(run_dir)
    per_fix, controls, notes = select_cases(doc, fixes, allowed, cfg, seed=run_dir.read_splits().seed)
    ids = sorted({t for v in per_fix.values() for t in v} | set(controls))
    summary = {"split": split, "n_trials": n_trials, "cases": per_fix, "controls": controls,
               "notes": notes, "ran": False}
    if split is None:
        summary["notes"].append("targeted check not run: split_mode is pooled (use dry_run_verify: full)")
        inplace.set({f["id"] for f in fixes if not f["editable"]}, [f for f in fixes if not f["editable"]])
        return summary
    if not fixes or not ids:
        summary["notes"].append("nothing to check" if not fixes else "no case available in the check split")
        return summary
    system = [f for f in fixes if not f["editable"]]
    scorer = {f["id"] for f in fixes if f["touches_scorer"]}
    run_dir.snapshot(CAND_ORIGINAL, paths.original_seed)
    try:
        # BEFORE: the system as it was (in-place fixes of an earlier session are taken back out).
        inplace.set(set(), system)
        before = _eval(adapter, run_dir, run_dir.candidate_dir(CAND_ORIGINAL), split, ids, TAG_BEFORE,
                       n_trials, workers)
        # With a scorer fix in the set, "after" is graded by a different scorer than "before". The other
        # fixes are then judged against the original system graded by the NEW scorer, so a measurement
        # change is never read as harm or help.
        ref = before
        if scorer and measured:
            inplace.set(scorer, system)
            ref = _eval(adapter, run_dir, run_dir.candidate_dir(CAND_ORIGINAL), split, ids,
                        TAG_BEFORE + "_rescored", n_trials, workers)
        # AFTER: every fix applied.
        build_fixed_seed(paths, fixes)
        run_dir.snapshot(CAND_FIXED, paths.fixed_seed)
        inplace.set({f["id"] for f in system}, system)
        after = _eval(adapter, run_dir, run_dir.candidate_dir(CAND_FIXED), split, ids, TAG_AFTER,
                      n_trials, workers)
    except BudgetExhausted as e:
        inplace.set({f["id"] for f in system}, system)
        summary["notes"].append(f"targeted check skipped: budget ({e})")
        run_dir.log_event("dry_run_verify_skipped", reason=str(e)[:300])
        return summary
    summary["ran"] = True

    def worse(t, b=None, a=None):
        b = ref if b is None else b
        a = after if a is None else a
        return t in b and t in a and a[t] < b[t] - _EPS

    def better(t):
        return t in ref and t in after and after[t] > ref[t] + _EPS

    # Owners of a case: the fixes that listed it; a control belongs to every measured fix.
    owners: dict[str, list[str]] = {}
    for fid, cases in per_fix.items():
        if any(f["id"] == fid for f in measured):
            for t in cases:
                owners.setdefault(t, []).append(fid)
    for t in controls:
        owners[t] = [f["id"] for f in measured]
    blamed: dict[str, set] = {f["id"]: set() for f in measured}
    by_id = {f["id"]: f for f in fixes}
    shared = [t for t in ids if worse(t) and len(owners.get(t) or []) > 1]
    for t in ids:
        if worse(t) and len(owners.get(t) or []) == 1:
            blamed[owners[t][0]].add(t)
    if shared:
        # Attribution: re-run each shared harmed case with each of its fixes alone.
        alone_results: dict[str, dict] = {}
        try:
            for fid in sorted({o for t in shared for o in owners[t]}):
                f = by_id[fid]
                cid = f"dry_run_only_{fid}"
                dst = run_dir.candidates / cid
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(paths.original_seed, dst)
                apply_to_copy(dst, [f])
                inplace.set(({fid} if not f["editable"] else set()) | scorer, system)
                cases = [t for t in shared if fid in owners[t]]
                alone_results[fid] = _eval(adapter, run_dir, dst, split, cases, cid, n_trials, workers)
        finally:
            inplace.set({f["id"] for f in system}, system)
        for t in shared:
            hit = [o for o in owners[t] if worse(t, ref, alone_results.get(o, {}))]
            for o in (hit or owners[t]):            # no single culprit = an interaction: blame them all
                blamed[o].add(t)
        summary["attribution"] = {t: {o: alone_results.get(o, {}).get(t) for o in owners[t]} for t in shared}

    for f in fixes:
        cases = per_fix.get(f["id"], [])
        base = before if f["touches_scorer"] else ref
        rows = [{"task_id": t, "before": base.get(t), "after": after.get(t),
                 "control": False} for t in cases]
        rows += [{"task_id": t, "before": base.get(t), "after": after.get(t), "control": True}
                 for t in controls]
        if f["touches_scorer"]:
            delta = [after[t] - before[t] for t in cases if t in before and t in after]
            verdict = "measurement_change"
            extra = {"mean_change": round(sum(delta) / len(delta), 6) if delta else None}
        else:
            harmed = sorted(blamed.get(f["id"]) or [])
            verdict = ("harmed" if harmed else
                       "helped" if any(better(t) for t in cases) else "neutral")
            extra = {"harmed_cases": harmed}
        f["verify"] = {"mode": "targeted", "split": split, "n_trials": n_trials, "verdict": verdict,
                       "cases": rows, **extra}
    run_dir.log_event("dry_run_verify", split=split, n_cases=len(ids), n_controls=len(controls),
                      verdicts={f["id"]: f["verify"]["verdict"] for f in fixes})
    return summary


# ---- the whole apply step --------------------------------------------------------------------

def apply(project, spec: dict, *, adapter, run_dir: RunDir, unattended: bool, n_trials: int = 1,
          workers=None) -> dict:
    """Decide, apply, verify, act on harm, hand off to baseline. Returns ``{"status", ...}``.

    ``status``: ``ready`` (baseline may run), ``needs_decision`` (interactive: a non-editable finding
    is still pending), ``needs_review`` (interactive: a harmed non-editable fix needs keep/revert),
    ``blocked`` (unattended ``stop``: a major non-editable finding needs approval).
    """
    paths = Paths.for_project(project)
    cfg = DryRunConfig.from_spec(spec)
    context = _read_json(paths.context)
    if context is None:
        raise DryRunError(f"no {paths.context} — run the prepare step first")
    context.setdefault("original_seed", str(paths.original_seed))
    raw = _read_json(paths.findings)
    if raw is None:
        raise DryRunError(f"no findings at {paths.findings} — the inspection has not written them yet")
    doc, problems = validate(raw, context, spec)
    if problems:
        raise DryRunError("findings.json is invalid:\n  - " + "\n  - ".join(problems))
    state = read_state(project)
    inplace = InPlace(paths, state)
    here = str(run_dir.root.resolve())
    for f in doc["findings"]:
        if (f.get("verify") or {}).get("verified_run_dir") not in (None, here):
            _forget_run(f)                          # a previous run's check: this run measures again
    review = [f for f in doc["findings"] if (f.get("verify") or {}).get("verified_run_dir") == here]
    if review:
        return _finish_review(project, spec, paths, doc, state, inplace, run_dir, unattended)

    normalize_decisions(doc)
    blocked = []
    if unattended:
        for f in pending(doc):
            if cfg.unattended == "stop" and f["severity"] == "major":
                blocked.append(f)
            else:
                f["decision"], f["approved_by"] = "as_is", None
                f.setdefault("notes", []).append(
                    "unattended run: nobody to approve a change outside the editable surface — left as is")
    elif pending(doc):
        _save_doc(paths, doc, cfg)
        return {"status": "needs_decision", "pending": [f["id"] for f in pending(doc)],
                "findings": str(paths.findings)}
    if blocked:
        _save_doc(paths, doc, cfg)
        msg = (f"BLOCKED: dry-run found {len(blocked)} major issue(s) needing approval, "
               f"see {paths.markdown}")
        _note_project_md(project, doc, msg)
        run_dir.log_event("dry_run_blocked", n=len(blocked), ids=[f["id"] for f in blocked])
        return {"status": "blocked", "message": msg, "ids": [f["id"] for f in blocked]}

    fixes = [f for f in doc["findings"] if f["decision"] == "fix"]
    system = [f for f in fixes if not f["editable"]]
    if cfg.verify == "targeted" and fixes:
        summary = targeted_check(adapter, run_dir, paths, doc, inplace, cfg, n_trials=n_trials,
                                 workers=workers)
        doc["verify_summary"] = summary
    else:
        inplace.set({f["id"] for f in system}, system)
        doc["verify_summary"] = {"ran": False, "mode": cfg.verify}
    for f in fixes:
        f.setdefault("verify", {})["verified_run_dir"] = str(run_dir.root.resolve())

    for f in fixes:
        if (f.get("verify") or {}).get("verdict") != "harmed":
            continue
        if f["editable"] or unattended:
            _revert(f, inplace, run_dir, why="harmed its targeted cases")
        else:
            f["verify"]["review_needed"] = True
    if not unattended and any((f.get("verify") or {}).get("review_needed") for f in fixes):
        _save_doc(paths, doc, cfg)
        return {"status": "needs_review",
                "review": [f["id"] for f in fixes if f["verify"].get("review_needed")],
                "findings": str(paths.findings)}
    return _handoff(project, spec, paths, doc, state, inplace, run_dir)


def _finish_review(project, spec, paths, doc, state, inplace, run_dir, unattended) -> dict:
    """Second ``apply`` after an interactive keep/revert review: act on the answers, then hand off."""
    cfg = DryRunConfig.from_spec(spec)
    for f in doc["findings"]:
        if f.pop("revert_requested", False):
            _revert(f, inplace, run_dir, why="reverted by the user after the targeted check")
    left = [f["id"] for f in doc["findings"] if (f.get("verify") or {}).get("review_needed")]
    if left:
        _save_doc(paths, doc, cfg)
        return {"status": "needs_review", "review": left, "findings": str(paths.findings)}
    return _handoff(project, spec, paths, doc, state, inplace, run_dir)


def _forget_run(f: dict) -> None:
    """Drop one run's verification results from a finding; a user's approval survives a revert."""
    if f.pop("reverted", False) and f.pop("decision_before_revert", None) == "fix":
        f["decision"] = "fix"
    for k in ("verify", "applied", "revert_requested"):
        f.pop(k, None)


def _revert(f: dict, inplace: InPlace, run_dir: RunDir, *, why: str) -> None:
    if not f["editable"]:
        inplace.revert(f)
    f["decision_before_revert"] = f["decision"]
    f["decision"] = "as_is"
    f["reverted"] = True
    f.setdefault("notes", []).append(why)
    run_dir.log_event("dry_run_revert", id=f["id"], editable=f["editable"], why=why)


def _handoff(project, spec, paths: Paths, doc: dict, state: dict, inplace: InPlace,
             run_dir: RunDir) -> dict:
    cfg = DryRunConfig.from_spec(spec)
    fixes = [f for f in doc["findings"] if f["decision"] == "fix"]
    editable = [f for f in fixes if f["editable"]]
    system = [f for f in fixes if not f["editable"]]
    inplace.set({f["id"] for f in system}, [f for f in doc["findings"] if not f["editable"] and has_fix(f)])
    for f in doc["findings"]:
        f["applied"] = f["decision"] == "fix"
    seed_dir = None
    if any(touches_seed(f) for f in fixes):
        seed_dir = str(build_fixed_seed(paths, fixes))
    elif paths.fixed_seed.exists():
        shutil.rmtree(paths.fixed_seed)
    _save_doc(paths, doc, cfg)
    state.update(ready=True, applied_run_dir=str(run_dir.root.resolve()), seed_dir=seed_dir,
                 n_fixes=len(fixes), n_capability_fixes=len(editable), n_system_fixes=len(system),
                 n_scorer_fixes=sum(1 for f in system if f["touches_scorer"]),
                 n_reverted=sum(1 for f in doc["findings"] if f.get("reverted")),
                 original_seed_hash=hash_candidate_dir(paths.original_seed),
                 fixed_seed_hash=hash_candidate_dir(Path(seed_dir)) if seed_dir else None,
                 verify=cfg.verify)
    _write_state(paths, state)
    # The run dir keeps its own copy: report, the optimizer hints and agent mode read it from there.
    dst = run_dir.root / "dry_run"
    dst.mkdir(parents=True, exist_ok=True)
    for p in (paths.findings, paths.markdown, paths.state):
        if p.exists():
            shutil.copy2(p, dst / p.name)
    counts = {k: sum(1 for f in doc["findings"] if f["kind"] == k) for k in KINDS}
    run_dir.log_event("dry_run", n_findings=len(doc["findings"]), by_kind=counts,
                      n_fixes=len(fixes), n_capability_fixes=len(editable), n_system_fixes=len(system),
                      n_reverted=state["n_reverted"], verify=cfg.verify, seed_dir=seed_dir)
    return {"status": "ready", "state": str(paths.state), "seed_dir": seed_dir, "n_fixes": len(fixes),
            "n_reverted": state["n_reverted"], "findings": str(paths.findings)}


def _save_doc(paths: Paths, doc: dict, cfg: DryRunConfig) -> None:
    _write_json(paths.findings, doc)
    paths.markdown.write_text(render_markdown(doc, cfg), encoding="utf-8")


def _note_project_md(project, doc: dict, msg: str) -> None:
    p = Path(project) / "PROJECT.md"
    lines = ["", "## Dry run — findings needing approval", "", msg, ""]
    for f in doc["findings"]:
        if not f["editable"]:
            lines.append(f"- `{f['id']}` ({f['kind']}, {f['severity']}) {', '.join(f['location'])}: "
                         f"{f['what_happens']} — decision: {f['decision']}")
    try:
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(text.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---- rendering -------------------------------------------------------------------------------

def _where(f: dict) -> str:
    if f.get("touches_scorer"):
        return "scorer"
    return "capability" if f.get("editable") else "system code"


def render_markdown(doc: dict, cfg: DryRunConfig | None = None) -> str:
    fs = doc.get("findings") or []
    auto = [f for f in fs if f["decision"] == "fix" and f.get("approved_by") == "auto"]
    user = [f for f in fs if f["decision"] == "fix" and f.get("approved_by") == "user"]
    out = ["# Dry run findings", ""]
    out.append(f"{len(fs)} finding(s): {sum(f['kind'] == 'flow' for f in fs)} flow, "
               f"{sum(f['kind'] == 'bug' for f in fs)} bug. Fixed: {len(auto) + len(user)} "
               f"({len(auto)} automatic, {len(user)} user-approved). Left as is: "
               f"{sum(f['decision'] == 'as_is' for f in fs)}. Pending: {sum(f['decision'] == 'pending' for f in fs)}.")
    if any(f.get("touches_scorer") and f["decision"] == "fix" for f in fs):
        out += ["", "> **Measurement change.** A scorer/metric fix is applied: it changes *what is "
                "measured*, not the agent, so a before/after comparison across it does not show that "
                "the agent improved."]
    vs = doc.get("verify_summary") or {}
    if vs.get("ran"):
        out += ["", f"Targeted check: smoke check on {len({t for v in vs['cases'].values() for t in v})} "
                f"{vs['split']} case(s) + {len(vs['controls'])} control(s), {vs['n_trials']} trial(s) — "
                "**not a measured gain**."]
    for note in vs.get("notes") or []:
        out.append(f"- {note}")
    for f in fs:
        fix = f.get("proposed_fix") or {}
        out += ["", f"## {f['id']} — {f['kind']}, {f['severity']} ({_where(f)})", "",
                f"- Location: {', '.join(f['location'])}",
                f"- What happens: {f['what_happens']}",
                f"- Decision: **{f['decision']}**" + (f" (approved by {f['approved_by']})" if f.get("approved_by") else "")
                + (" — reverted" if f.get("reverted") else "")]
        if fix.get("summary"):
            out.append(f"- Proposed fix: {fix['summary']}")
        v = f.get("verify") or {}
        if v.get("verdict"):
            rows = ", ".join(f"{r['task_id']}{' (control)' if r['control'] else ''}: {r['before']} → {r['after']}"
                             for r in v.get("cases") or [])
            label = "measurement change" if v["verdict"] == "measurement_change" else v["verdict"]
            out.append(f"- Targeted check: **{label}** — {rows}")
            if v.get("review_needed"):
                out.append("- **Needs your review:** keep or revert this fix.")
        for n in f.get("notes") or []:
            out.append(f"- Note: {n}")
        if fix.get("diff"):
            out += ["", "```diff", fix["diff"].rstrip(), "```"]
    return "\n".join(out) + "\n"


def hints_block(doc: dict | None) -> str:
    """The ``{{DRY_RUN_FINDINGS}}`` block: every finding left as is, framed by who may act on it."""
    fs = [f for f in (doc or {}).get("findings") or [] if f.get("decision") == "as_is"]
    if not fs:
        return ""
    yours = [f for f in fs if f.get("editable")]
    known = [f for f in fs if not f.get("editable")]
    out = ["## Dry-run findings (from reading the system before baseline)", ""]
    if yours:
        out.append("Inside your editable surface — you may fix these (the val gate still decides):")
        out += [f"- {', '.join(f['location'])}: {f['what_happens']}" for f in yours]
    if known:
        if yours:
            out.append("")
        out.append("Known system behavior outside your editable surface — do NOT edit it; work with it:")
        out += [f"- {', '.join(f['location'])}: {f['what_happens']}" for f in known]
    return "\n".join(out) + "\n"


def hints_for_run(run_dir) -> str:
    """``hints_block`` for a run (a ``RunDir`` or its path); "" when the dry run did not run."""
    if run_dir is None:
        return ""
    root = run_dir.root if isinstance(run_dir, RunDir) else Path(run_dir)   # (a Path has its own .root)
    return hints_block(_read_json(root / "dry_run" / "findings.json"))


def for_baseline(state_path) -> dict:
    """What baseline needs from a finished dry run; raises when it is not finished."""
    state = _read_json(Path(state_path))
    if not state:
        raise DryRunError(f"no dry-run state at {state_path}")
    if not state.get("ready"):
        raise DryRunError(f"the dry run at {state_path} has not finished (pending decisions or review)")
    return state


def report_summary(run_dir) -> dict:
    """The dry-run part of the report's JSON summary (all ``None``/empty when the phase did not run)."""
    root = Path(run_dir.root)
    doc = _read_json(root / "dry_run" / "findings.json")
    base = _read_json(root / "baseline.json", {}) or {}
    pre = base.get("baseline_pre_dry_run") or None
    out = {"dry_run_ran": doc is not None, "dry_run_fixes": [], "dry_run_reverted": [],
           "dry_run_verify": None, "dry_run_smoke": None,
           "baseline_pre_dry_run": pre.get("reward") if isinstance(pre, dict) else None,
           "baseline_pre_dry_run_stderr": pre.get("stderr") if isinstance(pre, dict) else None}
    if doc is None:
        return out
    state = _read_json(root / "dry_run" / "state.json", {}) or {}
    out["dry_run_verify"] = state.get("verify") or (doc.get("verify_summary") or {}).get("mode")
    out["dry_run_fixes"] = [{"id": f["id"], "where": _where(f), "location": f["location"],
                             "approved_by": f.get("approved_by")}
                            for f in doc["findings"] if f.get("applied")]
    out["dry_run_reverted"] = [f["id"] for f in doc["findings"] if f.get("reverted")]
    if (doc.get("verify_summary") or {}).get("ran"):
        out["dry_run_smoke"] = {f["id"]: {"verdict": f["verify"]["verdict"], "reverted": bool(f.get("reverted")),
                                          "cases": f["verify"]["cases"]}
                                for f in doc["findings"] if (f.get("verify") or {}).get("verdict")}
    out["dry_run_counts"] = {k: sum(1 for f in doc["findings"] if f["kind"] == k) for k in KINDS}
    out["dry_run_as_is"] = sum(1 for f in doc["findings"] if f["decision"] == "as_is")
    return out
