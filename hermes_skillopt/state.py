from __future__ import annotations

"""Core SkillOpt state objects for the Hermes adapter.

In the SkillOpt abstraction, a skill document is the trainable state.  The
Hermes adapter keeps that state staged by default; artifact paths below always
point at a run directory under the current HERMES_HOME/profile.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillState:
    """Trainable state: one Hermes SKILL.md document under current profile."""

    name: str
    path: Path
    relpath: str
    text: str
    sha256: str
    hermes_home: Path


@dataclass(frozen=True)
class CandidateSkill:
    """A bounded candidate edit to the trainable SkillState."""

    iteration: int
    text: str
    edits: list[dict[str, Any]] = field(default_factory=list)
    reflection: dict[str, Any] = field(default_factory=dict)
    reasoning: str | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    candidate_id: str = "candidate-1-1"


@dataclass(frozen=True)
class SkillOptArtifacts:
    """Fixed artifact paths for a staged SkillOpt run."""

    run_id: str
    run_dir: Path
    original: Path
    current: Path
    proposed: Path
    diff: Path
    report: Path
    manifest: Path
    evidence: Path
    train: Path
    val: Path
    test: Path
    reflections: Path
    candidate_edits: Path
    candidate_summary: Path
    gate_results: Path
    rejected_edits: Path
    current_validation_results: Path
    candidate_validation_results: Path
    test_results: Path
    slow_meta: Path
    best: Path
    target_binding: Path
    provenance_binding: Path
    history: Path
    target_execution_evidence: Path
    reviewer_gate: Path

    @classmethod
    def for_run(cls, run_id: str, run_dir: Path) -> "SkillOptArtifacts":
        return cls(
            run_id=run_id,
            run_dir=run_dir,
            original=run_dir / "original_SKILL.md",
            current=run_dir / "current_SKILL.md",
            proposed=run_dir / "proposed_SKILL.md",
            diff=run_dir / "diff.patch",
            report=run_dir / "report.md",
            manifest=run_dir / "manifest.json",
            evidence=run_dir / "evidence.json",
            train=run_dir / "train_items.jsonl",
            val=run_dir / "val_items.jsonl",
            test=run_dir / "test_items.jsonl",
            reflections=run_dir / "reflections.json",
            candidate_edits=run_dir / "candidate_edits.json",
            candidate_summary=run_dir / "candidate_summary.json",
            gate_results=run_dir / "gate_results.json",
            rejected_edits=run_dir / "rejected_edits.jsonl",
            current_validation_results=run_dir / "current_validation_results.json",
            candidate_validation_results=run_dir / "candidate_validation_results.json",
            test_results=run_dir / "test_results.json",
            slow_meta=run_dir / "slow_meta.json",
            best=run_dir / "best_skill.md",
            target_binding=run_dir / "target_binding.json",
            provenance_binding=run_dir / "provenance_binding.json",
            history=run_dir / "history.json",
            target_execution_evidence=run_dir / "target_execution_evidence.json",
            reviewer_gate=run_dir / "reviewer_gate.json",
        )

    def manifest_files(self, include_best: bool) -> dict[str, str]:
        files = {
            "original": self.original.name,
            "current": self.current.name,
            "proposed": self.proposed.name,
            "diff": self.diff.name,
            "report": self.report.name,
            "evidence": self.evidence.name,
            "train": self.train.name,
            "val": self.val.name,
            "test": self.test.name,
            "reflections": self.reflections.name,
            "candidate_edits": self.candidate_edits.name,
            "candidate_summary": self.candidate_summary.name,
            "gate_results": self.gate_results.name,
            "rejected_edits": self.rejected_edits.name,
            "current_validation_results": self.current_validation_results.name,
            "candidate_validation_results": self.candidate_validation_results.name,
            "test_results": self.test_results.name,
            "slow_meta": self.slow_meta.name,
            "target_binding": self.target_binding.name,
            "provenance_binding": self.provenance_binding.name,
            "history": self.history.name,
            "target_execution_evidence": self.target_execution_evidence.name,
            "reviewer_gate": self.reviewer_gate.name,
        }
        if include_best:
            files["best"] = self.best.name
        return files
