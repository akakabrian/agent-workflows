---
name: open-agent-workflows
description: >
  Use this skill when you need to author, run, inspect, or debug a dynamic
  agent workflow using Open Agent Workflows (owf). Reach for it whenever a
  task requires orchestrating multiple LLM calls — fan-out research, staged
  pipelines, multi-provider review, schema-validated extraction — with
  durability, resumability, and offline testability. The runtime is
  stdlib-only Python (>=3.11), works fully offline with the fake provider,
  and shells out to the claude or codex CLIs for real model calls.
---

# Open Agent Workflows skill

## When to use this

Use `owf` (Open Agent Workflows) when you need to:

- Fan out several agent calls in parallel and aggregate results.
- Run a staged pipeline where each step feeds the next.
- Extract structured JSON from a model response with schema validation.
- Run the same prompt through multiple providers or models for comparison.
- Build a workflow that is resumable after failure or interruption.
- Test orchestration logic offline (fake provider, no API keys, deterministic).

Do not use it for single one-off prompts; just call the CLI directly for those.

---

## Scaffold a new script

```bash
owf new my_workflow.py        # writes a starter template
```

Or write from scratch — a workflow is any `.py` file with `async def main(args)`.

---

## Script structure

```python
from workflows import agent, log, meta, parallel, phase

# Declare metadata (optional but recommended).
meta(
    name="my-workflow",
    description="What this workflow does",
    phases=["fetch", "analyse"],
)

async def main(args):
    # args is a dict passed via --arg KEY=VALUE or --args-json '...'
    topic = args.get("topic", "general AI")

    phase("fetch")
    result = await agent(
        f"Summarise the latest developments in: {topic}",
        label="summary",
        schema={
            "type": "object",
            "properties": {
                "summary":     {"type": "string"},
                "key_points":  {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "key_points"],
        },
    )

    if not result.ok:
        log("call failed", error=result.error)
        return {"error": result.error}

    # result.value is the validated JSON dict when schema is given.
    log("fetched", points=len(result.value["key_points"]))
    return result.value
```

### Key agent() options

| option | type | default | meaning |
|---|---|---|---|
| `label` | `str` | — | Human-readable name in reports |
| `phase` | `str` | current phase | Group calls in the report |
| `schema` | `dict` | — | JSON Schema; `.value` is validated on success |
| `provider` | `str` | run default | Override per call: `"fake"`, `"claude"`, `"codex"` |
| `model` | `str` | run default | E.g. `"claude-opus-4-8"`, `"gpt-5.5"` |
| `isolation` | `str` | `"none"` | `"worktree"` → fresh git tree, never auto-merged |
| `cache_policy` | `str` | `"auto"` | `"auto"`, `"disabled"`, `"read_only"`, `"refresh"` |
| `write_scope` | `list[str]` | `[]` | Non-empty → mutating; cache always bypassed |
| `timeout_seconds` | `int` | 600 | Adapter timeout |

### parallel() fan-out

```python
topics = ["climate", "economy", "health"]
results = await parallel(
    [lambda t=t: agent(f"One sentence on: {t}", label=t) for t in topics],
    concurrency=3,
    fail_fast=False,
)
summaries = [r.text for r in results if r.ok]
```

### pipeline() sequential

```python
results = await pipeline(
    documents,
    lambda doc: agent(f"Review this document: {doc}", label="review"),
    stop_on_error=True,
)
```

### Budget guard

```python
from workflows import budget

if budget.can_spend(2000):
    result = await agent("Expensive prompt …", label="big-call")
else:
    log("budget low, skipping")
```

---

## Run with a provider

```bash
# Offline — fake adapter, deterministic, no keys needed (default)
owf run my_workflow.py --provider fake

# Local CLIs — reuse the CLI's own auth, no API key in the environment
owf run my_workflow.py --provider claude --model claude-opus-4-8
owf run my_workflow.py --provider codex  --model gpt-5.5

# Direct HTTP APIs — read the key from the environment
OPENAI_API_KEY=...     owf run my_workflow.py --provider openai     --model gpt-5.4-mini
ANTHROPIC_API_KEY=...  owf run my_workflow.py --provider anthropic  --model claude-sonnet-4-6
GEMINI_API_KEY=...     owf run my_workflow.py --provider gemini     --model gemini-3.5-flash
DEEPSEEK_API_KEY=...   owf run my_workflow.py --provider deepseek
OPENROUTER_API_KEY=... owf run my_workflow.py --provider openrouter --model anthropic/claude-opus-4.8

# Pass args, set a token budget
owf run my_workflow.py --provider fake --arg topic="renewable energy" --budget-tokens 50000

# Dry-run: preview the manifest without executing
owf dry-run my_workflow.py --provider claude --json
```

Provider naming: `claude`/`codex` are the **local CLI** adapters (no key
needed); `openai`/`anthropic`/`gemini`/`deepseek`/`openrouter` are **HTTP API**
adapters that read a key from the environment. The `openai` adapter is generic
OpenAI-compatible, so it also targets Groq/Together/Ollama/etc. via base URL +
key. Keys are never persisted to the run store. When a call has a `schema`, API
adapters use native structured output (JSON mode / tool-use) and re-prompt once
on a validation failure. Run `owf providers` to see what's available. Register
extra endpoints without code via `~/.workflows/providers.json` or
`OWF_PROVIDER_<NAME>_{BASE_URL,API_KEY_ENV,MODEL,KIND}` env vars.

### Scaling and cost

- **Fan-out** is capped per provider to avoid rate limits: tune with
  `OWF_PROVIDER_<NAME>_CONCURRENCY` or `OWF_MAX_CONCURRENCY` (default 8).
- **Cost**: API calls populate `estimated_cost_usd`; `owf usage` rolls up
  tokens/cost across runs; `owf prices [--refresh]` shows/updates the price table.
- **Batch (~50% off)** for large independent prompt sets:
  `owf batch submit prompts.jsonl --provider anthropic|openai`, then
  `owf batch fetch <id> --out results.jsonl`. Standalone — not a workflow run.

---

## Inspect a run

```bash
owf status  latest              # run summary: status, script, call count
owf output  latest              # print output.json (return value of main())
owf calls   latest              # list every call with index, status, label
owf explain-cache latest        # per-call cache decision (hit/miss/bypassed)
owf report  latest --stdout     # Markdown report
owf report  latest --html --out report.html
owf artifacts latest            # list stored files (prompts, outputs, etc.)
owf cat <call_id>               # print a call's output text or JSON
owf cat <call_id> --prompt      # print the prompt that was sent
```

Use a specific `run_id` in place of `latest` for any of these commands.

---

## Resume after failure or interruption

```bash
owf resume latest
# or
owf resume 20260528-143201-a3f1
```

Read-only calls whose cache key matches replay instantly from SQLite.
Mutating calls (write_scope set or isolation="worktree") always re-execute —
the runtime never caches side effects.

---

## Cache-safety rule

**Mutating calls are always re-executed on resume; they are never cached.**

A call is mutating when `write_scope` is non-empty or `isolation="worktree"`.
The runtime reports these as `cache_status="bypassed"`. This is intentional:
caching a mutating call would let a resume silently skip real filesystem
side effects.

Read-only calls (`write_scope=[]`, `isolation="none"`) are cached and reused
across runs and resumes as long as the prompt, schema, provider, model, and
script content match.

---

## Trusted-script caveat

Workflow scripts run as ordinary Python in your process with no sandbox. Only
run scripts you wrote or have reviewed. Do not run untrusted scripts from the
internet without reading them first. See SECURITY.md for the full security model.

---

## MCP server (use tools instead of the CLI)

If `owf` is registered as an MCP server (`owf mcp`), prefer its tools over
shelling out:

| Tool | Use it to |
|---|---|
| `owf_run_workflow` | Run a script (`path`, optional `args`, `provider`, `model`, budgets, `cache_policy`, `home`) |
| `owf_validate_workflow` | Parse a script + return its meta without running it |
| `owf_dry_run` | Draft a run manifest (no execution) — preview before spending |
| `owf_status` / `owf_calls` | Run summary / per-call records (`run_id` or `"latest"`) |
| `owf_output` | Get the value returned by `main()` |
| `owf_explain_cache` | Why each call hit/missed/bypassed the cache |
| `owf_report` / `owf_artifacts` | Render a report / list stored artifact files |
| `owf_read_artifact` | Read one file under the run dir (bounded: `offset`/`max_bytes`, returns `truncated`) |
| `owf_list_runs` / `owf_resume` | List runs / resume a prior run |
| `owf_new_workflow` | Scaffold a script (writes confined to `workspace_root` unless `allow_absolute`) |

Safe loop: `owf_validate_workflow` → `owf_dry_run` → `owf_run_workflow`. The
tools share the CLI's durability, caching, and resume semantics, and default to
the offline `fake` provider. Register with `claude mcp add owf -- owf mcp`. The
same trusted-script caveat applies — the MCP server is a tool surface, not a
sandbox.

---

## AgentResult reference

```python
result.ok                  # bool — True if call succeeded + schema validated
result.status              # "done" | "failed" | "schema_failed" | "timeout" | "provider_failed"
result.text                # raw text (no schema) or None
result.value               # validated JSON value (schema given) or None
result.cache_status        # "hit" | "miss" | "bypassed" | "disabled"
result.input_tokens        # int | None
result.output_tokens       # int | None
result.estimated_cost_usd  # float | None
result.worktree_path       # str | None (isolation="worktree" only)
result.changed_files       # list[str] (isolation="worktree" only)

result.require_ok()        # raises RuntimeError if not ok
result.value_or_raise()    # returns value or raises
result.text_or_raise()     # returns text or raises
```
