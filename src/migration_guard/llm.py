"""Provider-agnostic LLM client with cassette record/replay.

Why this exists: judging requires that somebody else can re-run the evaluation and get
*the same numbers*. Every live model call is content-addressed by
sha256(model + system + user) and written to `cassettes/`. Re-running with
MG_LLM_PROVIDER=replay serves those recordings, so the whole benchmark reproduces
offline, deterministically, at zero cost and with no API key.

Providers
---------
claude_cli    : shells out to the `claude` CLI (uses an existing Claude Code login)
anthropic_api : direct HTTPS call to the Anthropic Messages API (ANTHROPIC_API_KEY)
replay        : cassettes only; raises if a call was never recorded
stub          : deterministic offline canned answers, for CI smoke tests without any model
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import SETTINGS


class LLMError(RuntimeError):
    pass


_FLAG_CACHE: set[str] | None = None


def _claude_cli_flags() -> set[str]:
    """Which optional flags this installed `claude` build accepts.

    Probed once from `claude --help`. CLI versions differ, and an unsupported flag makes
    the whole call fail with a message that looks nothing like the real problem.
    """
    global _FLAG_CACHE
    if _FLAG_CACHE is None:
        try:
            out = subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=60).stdout
        except Exception:
            out = ""
        _FLAG_CACHE = {tok.strip(",") for tok in out.split() if tok.startswith("--")}
    return _FLAG_CACHE


class CassetteMiss(LLMError):
    pass


@dataclass
class LLMResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False
    prompt_hash: str = ""
    cost_usd: float = 0.0


def _hash(model: str, system: str, user: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"\x00")
    h.update(system.encode())
    h.update(b"\x00")
    h.update(user.encode())
    return h.hexdigest()[:32]


class LLM:
    def __init__(self, settings=SETTINGS):
        self.s = settings
        self.s.cassette_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0

    # -- cassettes --------------------------------------------------------
    def _cassette_path(self, key: str) -> Path:
        return self.s.cassette_dir / f"{key}.json"

    def _load(self, key: str) -> LLMResponse | None:
        p = self._cassette_path(key)
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        return LLMResponse(
            d["text"], d.get("tokens_in", 0), d.get("tokens_out", 0), True, key, d.get("cost_usd", 0.0)
        )

    def _save(self, key: str, model: str, system: str, user: str, r: LLMResponse) -> None:
        self._cassette_path(key).write_text(
            json.dumps(
                {
                    "key": key,
                    "model": model,
                    "system": system,
                    "user": user,
                    "text": r.text,
                    "tokens_in": r.tokens_in,
                    "tokens_out": r.tokens_out,
                    "cost_usd": r.cost_usd,
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                indent=1,
            ),
            encoding="utf-8",
        )

    # -- providers --------------------------------------------------------
    def _call_claude_cli(self, system: str, user: str) -> LLMResponse:
        # Flags are held constant on purpose: a stable prefix keeps Anthropic prompt
        # caching warm across calls (~10x cheaper), and --restricted / --strict-mcp-config
        # guarantee the reviewer model has no tools of its own -- every tool result in a
        # trajectory came from MigrationGuard, not from the CLI wandering off.
        #
        # Only flags this particular CLI build actually supports are passed. Older
        # versions reject unknown options outright, which showed up as a bare
        # "claude cli failed after 4 attempts: error: unknown option '--restricted'".
        # None of these flags affect the prompt, so dropping one changes cost and
        # isolation, never the cassette key.
        cmd = ["claude", "-p", user, "--model", self.s.model]
        supported = _claude_cli_flags()
        for flag, value in (
            ("--system-prompt", system),
            ("--exclude-dynamic-system-prompt-sections", None),
            ("--restricted", None),
            ("--strict-mcp-config", None),
            ("--setting-sources", ""),
            ("--no-session-persistence", None),
        ):
            if flag in supported:
                cmd.append(flag)
                if value is not None:
                    cmd.append(value)
            elif flag == "--system-prompt":
                # every supported build has one of these two; prefer replacing, but
                # appending still produces a usable reviewer
                cmd += ["--append-system-prompt", system]
        cmd += ["--output-format", "json"]
        # The CLI fails transiently under concurrency (rate limits, transport resets). An
        # evaluation that silently turns those into "the system found no risks" is worse
        # than useless, so retry with backoff and let a persistent failure raise loudly.
        last, payload = "", None
        for attempt in range(4):
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if proc.returncode != 0 or not proc.stdout.strip():
                last = (proc.stderr or proc.stdout or "")[:400]
                time.sleep(min(2**attempt * 3, 30))
                continue
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                return LLMResponse(proc.stdout.strip())
            # The CLI exits 0 on API-level failures (rate limit, transport) and puts the
            # message in `result`. Treating that as a model answer would quietly record an
            # empty verdict as if the system had reviewed the migration and found nothing,
            # which is the worst possible failure for a safety tool. Retry, then raise.
            if payload.get("is_error"):
                last = str(payload.get("result"))[:200]
                payload = None
                time.sleep(min(2**attempt * 5, 60))
                continue
            break
        if payload is None:
            raise LLMError(f"claude cli failed after 4 attempts: {last}")
        text = payload.get("result") or ""
        usage = payload.get("usage") or {}
        r = LLMResponse(text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))
        r.cost_usd = float(payload.get("total_cost_usd") or 0.0)
        return r

    def _call_anthropic_api(self, system: str, user: str) -> LLMResponse:
        import httpx

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        r = httpx.post(
            f"{base}/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.s.model,
                "max_tokens": self.s.max_tokens,
                "temperature": self.s.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=300,
        )
        r.raise_for_status()
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []))
        u = d.get("usage", {})
        return LLMResponse(text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))

    def _call_stub(self, system: str, user: str) -> LLMResponse:
        """No-model fallback so `make smoke` works in a network-free CI box."""
        return LLMResponse(
            json.dumps(
                {
                    "decision": "needs_changes",
                    "summary": "stub provider: no model was called",
                    "findings": [],
                }
            ),
            0,
            0,
        )

    # -- public -----------------------------------------------------------
    def complete(self, system: str, user: str, agent: str = "agent", tracer=None) -> LLMResponse:
        key = _hash(self.s.model, system, user)
        cached = self._load(key)
        if cached is not None and self.s.provider != "stub":
            self.calls += 1
            if tracer:
                tracer.llm(agent, key, True, cached.tokens_in, cached.tokens_out, cached.text)
            return cached

        if self.s.provider == "replay":
            raise CassetteMiss(
                f"no cassette for {key} (model={self.s.model}). Re-record with "
                f"MG_LLM_PROVIDER=claude_cli, or check that the prompt is unchanged."
            )

        if self.s.provider == "claude_cli":
            resp = self._call_claude_cli(system, user)
        elif self.s.provider == "anthropic_api":
            resp = self._call_anthropic_api(system, user)
        elif self.s.provider == "stub":
            resp = self._call_stub(system, user)
        else:
            raise LLMError(f"unknown provider {self.s.provider!r}")

        resp.prompt_hash = key
        self.calls += 1
        self.tokens_in += resp.tokens_in
        self.tokens_out += resp.tokens_out
        self.cost_usd += resp.cost_usd
        if self.s.record and self.s.provider in ("claude_cli", "anthropic_api"):
            self._save(key, self.s.model, system, user, resp)
        if tracer:
            tracer.llm(agent, key, False, resp.tokens_in, resp.tokens_out, resp.text)
        return resp


# ---------------------------------------------------------------------------
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.S)


def extract_json(text: str) -> Any:
    """Models wrap JSON in prose and fences. Pull the first valid object/array out.

    Returns {} on failure rather than raising: callers treat an unparseable reply as an
    empty result and retry, which is recorded in the trajectory.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = _JSON_BLOCK.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # last resort: first balanced {...}
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return {}
