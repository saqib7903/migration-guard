"""Trajectory recorder.

Every agent step is appended to runs/<run_id>/trajectory.jsonl and can be rendered to
markdown. This is what a reviewer (or a judge) reads to answer "what did the agent do,
what did the tool say back, and what did it do next?".
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .config import SETTINGS


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


class Tracer:
    def __init__(self, run_id: str | None = None, root: Path | None = None, label: str = ""):
        self.run_id = run_id or new_run_id()
        self.root = (root or SETTINGS.runs_dir) / self.run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "trajectory.jsonl"
        self.label = label
        self._t0 = time.time()
        self.step_no = 0

    # --- recording -------------------------------------------------------
    def _write(self, kind: str, agent: str, payload: dict[str, Any]) -> None:
        self.step_no += 1
        rec = {
            "step": self.step_no,
            "t": round(time.time() - self._t0, 3),
            "kind": kind,
            "agent": agent,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    def agent_instructions(self, agent: str, system_prompt: str, task: str) -> None:
        self._write("instructions", agent, {"system_prompt": system_prompt, "task": task})

    def thought(self, agent: str, text: str) -> None:
        self._write("thought", agent, {"text": text})

    def tool_call(self, agent: str, tool: str, args: dict[str, Any]) -> None:
        self._write("tool_call", agent, {"tool": tool, "args": args})

    def tool_result(self, agent: str, tool: str, result: Any, ok: bool = True) -> None:
        self._write("tool_result", agent, {"tool": tool, "ok": ok, "result": result})

    def llm(self, agent: str, prompt_hash: str, cached: bool, tokens_in: int, tokens_out: int, raw: str) -> None:
        self._write(
            "llm_response",
            agent,
            {
                "prompt_hash": prompt_hash,
                "cached": cached,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "raw_preview": raw[:1200],
            },
        )

    def checkpoint(self, agent: str, name: str, detail: str) -> None:
        """A human checkpoint / approval gate."""
        self._write("human_checkpoint", agent, {"name": name, "detail": detail})

    def retry(self, agent: str, reason: str, attempt: int) -> None:
        self._write("retry", agent, {"reason": reason, "attempt": attempt})

    def decision(self, agent: str, payload: dict[str, Any]) -> None:
        self._write("decision", agent, payload)

    # --- rendering -------------------------------------------------------
    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def render_markdown(self) -> str:
        out = [f"# Trajectory `{self.run_id}` {self.label}", ""]
        for r in self.records():
            head = f"### step {r['step']} · t={r['t']}s · **{r['agent']}** · {r['kind']}"
            out.append(head)
            k = r["kind"]
            if k == "instructions":
                out.append("<details><summary>agent instructions (system prompt)</summary>\n")
                out.append("```text\n" + r["system_prompt"].strip() + "\n```")
                out.append("</details>\n")
                out.append("**task**\n")
                out.append("```text\n" + r["task"].strip()[:2000] + "\n```")
            elif k == "tool_call":
                out.append(f"calls `{r['tool']}` with:")
                out.append("```json\n" + json.dumps(r["args"], indent=2, default=str)[:1500] + "\n```")
            elif k == "tool_result":
                out.append(f"`{r['tool']}` responded (ok={r['ok']}):")
                out.append("```json\n" + json.dumps(r["result"], indent=2, default=str)[:2500] + "\n```")
            elif k == "llm_response":
                out.append(
                    f"model replied (cached={r['cached']}, in={r['tokens_in']} out={r['tokens_out']} tok):"
                )
                out.append("```json\n" + r["raw_preview"] + "\n```")
            elif k == "human_checkpoint":
                out.append(f"⛔ **HUMAN CHECKPOINT — {r['name']}**: {r['detail']}")
            elif k == "retry":
                out.append(f"🔁 retry #{r['attempt']}: {r['reason']}")
            elif k == "thought":
                out.append("> " + r["text"])
            else:
                out.append("```json\n" + json.dumps({k2: v for k2, v in r.items() if k2 not in ("step", "t", "kind", "agent")}, indent=2, default=str)[:2500] + "\n```")
            out.append("")
        return "\n".join(out)

    def save_markdown(self) -> Path:
        p = self.root / "trajectory.md"
        p.write_text(self.render_markdown(), encoding="utf-8")
        return p
