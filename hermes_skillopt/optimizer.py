from __future__ import annotations

"""Optimizer backends: reflection plus bounded skill edits."""

import json
import re
from pathlib import Path
from typing import Any, Protocol

from hermes_skillopt.env import EvalTask
from hermes_skillopt.state import CandidateSkill


class JsonBackend(Protocol):
    mode: str

    def json(self, prompt: str, schema_hint: dict[str, Any], repair_path: Path | None = None) -> dict[str, Any]: ...


def _frontmatter_split(text: str) -> tuple[str, str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            after = end + len("\n---")
            if after < len(text) and text[after] == "\n":
                after += 1
            return text[:after], text[after:]
    return "", text


def _apply_one(body: str, edit: dict[str, Any]) -> str:
    op = edit.get("op")
    if op == "append":
        text = str(edit.get("text", ""))
        heading = None
        m = re.search(r"^##\s+.+$", text.strip(), flags=re.M)
        if m:
            heading = re.escape(m.group(0).strip())
        if heading and re.search(rf"^({heading})$", body, flags=re.M):
            return re.sub(rf"\n*{heading}\n.*?(?=\n##\s|\Z)", "\n\n" + text.strip() + "\n", body, count=1, flags=re.S | re.M)
        return body.rstrip() + text + "\n"
    if op == "replace":
        old = str(edit.get("old", ""))
        new = str(edit.get("new", ""))
        return body.replace(old, new, 1) if old and old in body else body
    if op == "delete":
        old = str(edit.get("text") or edit.get("old") or "")
        return body.replace(old, "", 1) if old and old in body else body
    if op == "insert_after":
        anchor = str(edit.get("anchor", ""))
        text = str(edit.get("text", ""))
        return body.replace(anchor, anchor + text, 1) if anchor and anchor in body else body.rstrip() + text + "\n"
    return body


def apply_bounded_edits(current: str, edits: list[dict[str, Any]]) -> str:
    fm, body = _frontmatter_split(current)
    new_body = body
    for edit in edits:
        new_body = _apply_one(new_body, edit)
    return fm + new_body


class OptimizerBackend:
    """Reflection + bounded edit generator.

    The optimizer can inspect rollout/evaluation evidence and propose edits, but
    it never decides acceptance.  ValidationGate is the only accept/reject gate.
    """

    def __init__(self, backend: JsonBackend, edit_budget: int = 3):
        self.backend = backend
        self.edit_budget = max(0, int(edit_budget))

    def reflect(self, train_tasks: list[EvalTask], current_skill: str, current_eval: dict[str, Any], run_dir: Path, iteration: int) -> dict[str, Any]:
        prompt = (
            "Reflect on Hermes SkillOpt train rollouts. "
            "Skill document is trainable state; target executor is frozen.\n"
            "TRAIN_TASKS=" + json.dumps([t.__dict__ for t in train_tasks], ensure_ascii=False)[:10000] + "\n"
            "CURRENT_EVAL=" + json.dumps(current_eval, ensure_ascii=False)[:10000] + "\n"
            "CURRENT_SKILL=" + current_skill[:8000]
        )
        data = self.backend.json(prompt, {"kind": "reflect"}, run_dir / f"llm_reflect_repair_{iteration}.json")
        data["iteration"] = iteration
        data["optimizer_role"] = "reflection_only_no_acceptance"
        return data

    def propose(self, reflection: dict[str, Any], current_skill: str, run_dir: Path, iteration: int) -> CandidateSkill:
        prompt = (
            "Generate bounded edits for Hermes SKILL.md trainable state. "
            f"Allowed ops: append, replace, delete, insert_after. Max edits: {self.edit_budget}. "
            "Do not edit YAML frontmatter and do not write files directly.\n"
            "REFLECTION=" + json.dumps(reflection, ensure_ascii=False)[:10000] + "\n"
            "SKILL=" + current_skill[:12000]
        )
        data = self.backend.json(prompt, {"kind": "edit"}, run_dir / f"llm_edit_repair_{iteration}.json")
        edits = data.get("edits") if isinstance(data, dict) else []
        if not isinstance(edits, list):
            edits = []
        edits = edits[: self.edit_budget]
        candidate_text = apply_bounded_edits(current_skill, edits)
        return CandidateSkill(
            iteration=iteration,
            text=candidate_text,
            edits=edits,
            reflection=reflection,
            reasoning=str(data.get("reasoning", "")) if isinstance(data, dict) else None,
        )
