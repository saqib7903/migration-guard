# Raw agent trajectories

`docs/TRAJECTORIES.md` is the rendered, readable version. These are the underlying
`trajectory.jsonl` files exactly as the tracer wrote them during the recorded evaluation
run — one JSON object per step, in order:

| kind | meaning |
|---|---|
| `instructions` | the agent's system prompt and its task, verbatim |
| `thought` | the agent's stated reason for the next tool call |
| `tool_call` / `tool_result` | what it asked for and what the tool returned |
| `llm_response` | prompt hash, cached or live, token counts, raw reply |
| `retry` | a malformed reply, an unknown tool, or a rewrite that failed validation |
| `decision` | verifier keep/drop counts, final verdict |
| `human_checkpoint` | the approval gate that stops the pipeline |

Every review writes one of these. Render any of them with
`mg trajectory <run_id>`; `runs/` is git-ignored because it grows without bound.

The five here were chosen to show the interesting behaviour rather than the average case:
a reviewer whose verdict is overturned by the database, a case where relfilenode evidence
prevents a false claim, a full four-problem release migration, the baseline being
confidently wrong on the same input, and the linter's structural blind spot.
