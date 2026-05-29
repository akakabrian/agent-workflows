# Open Agent Workflows

A zero-dependency, stdlib-only Python runtime for **provider-agnostic dynamic
agent workflows** — durable, resumable, and composable from a single script
file.

## Why it exists

Most workflow orchestrators either lock you into a specific model vendor, pull
in heavy SDKs, or mix orchestration logic with HTTP clients.  Open Agent
Workflows does none of that:

- **Provider-agnostic.** The same script runs against the `fake` adapter
  offline, against `claude` (Claude CLI), or against `codex` (Codex CLI)
  with a single `--provider` flag.
- **Zero runtime dependencies.** The core uses only the Python standard
  library — `asyncio`, `sqlite3`, `importlib`, `hashlib`, `json`. No pip
  extras are required to run or develop against it.
- **Durable and resumable.** Every run is recorded in SQLite. On interrupt or
  failure you can `owf resume <run_id>` — read-only calls replay instantly
  from cache; mutating calls re-execute safely.
- **Script-first.** A workflow is a plain `.py` file with an `async def main(args)`.
  No YAML, no DAG builder, no class hierarchy.

---

## A first workflow

```python
# examples/hello_workflow.py
from workflows import agent, log, meta, phase

meta(name="hello", description="A simple greeting workflow")

async def main(args):
    phase("greet")
    result = await agent(
        "Write a one-sentence welcome message for an AI workflow tool.",
        label="greeting",
    )
    log("done", greeting=result.text)
    return {"greeting": result.text}
```

Run it offline (no model required):

```bash
owf run examples/hello_workflow.py --provider fake --home .workflows
# run_id: 20260528-143201-a3f1
# status: done
# run_dir: .workflows/runs/20260528-143201-a3f1
```

Inspect the result:

```bash
owf status latest
owf output latest
owf calls  latest
owf explain-cache latest
owf report latest --stdout
```

Resume after an interruption:

```bash
owf resume 20260528-143201-a3f1
```

---

## Quickstart in 60 seconds

```bash
git clone https://github.com/akakabrian/agent-workflows.git
cd agent-workflows
pip install -e .

owf new examples/my_first_workflow.py
owf run examples/my_first_workflow.py --provider fake --home .workflows
owf status latest
owf output latest
owf report latest --stdout
```

You just ran a durable, resumable agent workflow without configuring a model provider.

---

## Primitives

Import from `workflows` (short alias) or `agent_workflows` (canonical name).

### `meta()`

Declare workflow name, description, and phases at module level:

```python
meta(name="research", description="Fan-out research workflow", phases=["fetch", "synthesise"])
```

### `await agent(prompt, ...)`

Make a single agent call.  Returns an `AgentResult`.

```python
result = await agent(
    "Summarise the following text: ...",
    label="summarise",         # human-readable name shown in calls/report
    phase="summarise",         # optional phase grouping
    schema={"type": "object",  # JSON Schema — result.value is validated JSON
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"]},
    provider="claude",         # override per-call; default is the run provider
    model="claude-opus-4-5",   # pass through to the provider CLI
    isolation="worktree",      # "none" (default) or "worktree" (fresh git tree)
    cache_policy="auto",       # "auto" | "disabled" | "read_only" | "refresh"
    read_scope=["docs/"],      # declarative, passed to the adapter
    write_scope=["src/"],      # non-empty → mutating; cache bypassed
    timeout_seconds=120,
    cache_namespace="v2",      # isolate cache keys across script versions
)
print(result.ok, result.text, result.cache_status)
# True  "Here is a summary..."  "miss"
```

Key `AgentResult` fields:

| field | type | meaning |
|---|---|---|
| `.ok` | `bool` | `True` when the call succeeded and schema (if any) validated |
| `.status` | `str` | `"done"`, `"failed"`, `"schema_failed"`, `"timeout"`, `"provider_failed"` |
| `.text` | `str \| None` | raw text output |
| `.value` | `Any \| None` | validated JSON value when a schema was given |
| `.cache_status` | `str` | `"hit"`, `"miss"`, `"bypassed"`, `"disabled"` |
| `.input_tokens` | `int \| None` | tokens consumed (populated by real providers) |
| `.output_tokens` | `int \| None` | tokens generated |
| `.estimated_cost_usd` | `float \| None` | cost reported by the provider |
| `.worktree_path` | `str \| None` | git worktree path when isolation was used |
| `.changed_files` | `list[str]` | files modified in the worktree |

Helpers: `.require_ok()`, `.value_or_raise()`, `.text_or_raise()`.

Schema validation intentionally supports a small JSON Schema subset rather than
full draft compliance: `type` checks for object, array, string, number,
integer, boolean, and null; object `properties` and `required`; array `items`;
and `enum`.

### `await parallel(thunks, concurrency=None, fail_fast=False)`

Fan out a list of zero-argument async callables and collect results in order:

```python
topics = ["climate", "economy", "health"]
results = await parallel(
    [lambda t=t: agent(f"Summarise recent news on: {t}", label=t) for t in topics],
    concurrency=3,
)
```

With `fail_fast=True`, cancellation is best-effort: the first failed result or
exception stops scheduling new work and cancels still-pending tasks. Calls that
already finished are returned with their normal result; cancelled or unscheduled
calls are returned as `AgentResult(status="cancelled")`.

### `await pipeline(items, fn, stop_on_error=False)`

Process a sequence one item at a time:

```python
results = await pipeline(documents, lambda doc: agent(f"Review: {doc}"))
```

### `phase(name)` and `log(message, **meta)`

Mark the current phase and emit structured log events:

```python
phase("analyse")
log("processing", count=len(items), source="arxiv")
```

### `await workflow(path, args)`

Invoke another workflow script as a nested call, sharing the parent's home
and budget (one level of nesting):

```python
sub = await workflow("steps/fetch.py", args={"url": url})
```

### `budget`

A module-level proxy for the run's token/cost budget:

```python
if budget.can_spend(2000):
    result = await agent("...", label="expensive")

print(budget.spent_tokens, budget.remaining_tokens)
```

---

## CLI reference

```
owf init                              # initialise the local run store
owf new <path>                        # scaffold a starter script
owf examples                          # list bundled examples
owf doctor                            # local environment diagnostics
owf validate <script>                 # parse + check meta/main
owf dry-run  <script> [OPTIONS]       # preview manifest, no execution
owf run      <script> [OPTIONS]       # execute a workflow
owf resume   <run_id>                 # replay, skipping cached read-only calls
owf status   <run_id|latest>          # run summary
owf output   <run_id|latest>          # print output.json
owf calls    <run_id|latest>          # list call records
owf explain-cache <run_id|latest>     # per-call cache decision explanation
owf report   <run_id|latest> [--html] [--out PATH] [--stdout]
owf artifacts <run_id|latest>         # list stored artifacts
owf cat      <call_id>  [--prompt]    # print a call's output or prompt
```

`run` and `dry-run` accept:

```
--provider {fake,claude,codex,anthropic,openai}
--model MODEL
--budget-tokens N
--budget-cost-usd N.NN
--cache-policy {auto,disabled,read_only,refresh}
--args-json '{"key": "value"}'
--arg KEY=VALUE            (repeatable)
--json                     (machine-readable output)
--home PATH                (override the .workflows home directory)
--debug                    (print Python tracebacks for errors)
```

`resume` additionally accepts `--provider` and `--model` to override the
original run's provider.

---

## Installation

From source (editable install, recommended for development):

```bash
git clone https://github.com/akakabrian/agent-workflows.git
cd agent-workflows
pip install -e .
owf --help
```

PyPI package name (once published): `open-agent-workflows`.

Python 3.11 or later is required. No other runtime dependencies.

---

## Providers

| Provider name | Aliases | How it works | Auth |
|---|---|---|---|
| `fake` | `fixture` | Offline, deterministic. Returns schema fixtures or echoes prompts. Used by all examples and tests. | None |
| `claude` | `anthropic`, `claude-cli` | Shells out to the `claude` CLI (`claude -p --output-format json`). | Reuses the CLI's own auth — no API key needed in the environment. |
| `codex` | `openai`, `codex-cli` | Shells out to the `codex exec` CLI with JSONL events. | Reuses the CLI's own auth. |

The `claude` and `codex` adapters require the respective CLI to be installed
and authenticated. Open Agent Workflows does not manage or store credentials.

---

## Cache and resume semantics

Every read-only `agent()` call is keyed on a hash of the prompt, options,
schema, provider, model, and script content. On a subsequent `owf run` or
`owf resume`, matching calls replay from the SQLite cache instantly and are
reported as `cache_status="hit"`.

**Mutating calls bypass the cache.** Any call with a non-empty `write_scope`
or `isolation="worktree"` is classified as mutating. The runtime never reads
from or writes to the prompt-only cache for mutating calls — the cached output
would not prove the filesystem side effects still hold — and reports
`cache_status="bypassed"`. Mutating calls always re-execute on resume.

`owf explain-cache <run_id>` prints a per-call explanation:

```
miss      greeting: no prior cached result existed for this call key
hit       summarise: reused a prior read-only result (prompt, options, schema, provider, and model matched)
bypassed  patch: mutating call (write scope or worktree isolation); prompt-only cache is unsafe
```

---

## Worktree isolation

Setting `isolation="worktree"` on an agent call creates a fresh git worktree
for that call. The adapter runs inside the worktree; its file edits never
touch your working tree. After the call, `result.worktree_path`,
`result.worktree_branch`, and `result.changed_files` tell you what changed.
Nothing is auto-merged — you review and merge manually.

Worktree isolation fails closed: if the script directory is not inside a git
repository, or if `git worktree add` fails, the provider is not invoked and
the call records `AgentResult(ok=False, status="worktree_failed")`. The runtime
will not silently run a worktree-isolated call in your current working tree.

---

## Artifact layout

```
<script_dir>/.workflows/          # default home (override with --home)
  workflow.sqlite                 # run index, calls, events, cache
  runs/
    <run_id>/
      manifest.json               # run parameters
      summary.md                  # human summary
      output.json                 # return value of main()
      report.md / report.html     # generated by owf report
      calls/
        <call_id>/
          prompt.txt
          output.txt | output.json
```

---

## Safety

Workflow scripts are **trusted local Python**. The runtime loads a script with
`importlib` and executes its `async main(args)` with your user's full
privileges. There is no sandbox, container, or permission boundary.

- Only run scripts you wrote or have reviewed.
- Do not run untrusted scripts from the internet without reading them first.
- API keys are read by adapters from the environment or the CLI's own auth.
  They are never written to the run database, manifests, or artifacts.
- Do not place secrets in prompts, `args`, or `metadata` — those are persisted
  to the run store.

See [SECURITY.md](SECURITY.md) for the full security model.

---

## Examples

| File | What it shows |
|---|---|
| `examples/hello_workflow.py` | Minimal `agent()` + `log()` |
| `examples/schema_validation.py` | JSON Schema enforcement on a single call |
| `examples/parallel_research.py` | `parallel()` fan-out with schema aggregation |
| `examples/multi_model_review.py` | Same prompt across multiple providers/models |

Run any example offline:

```bash
owf run examples/schema_validation.py   --provider fake
owf run examples/parallel_research.py  --provider fake
owf run examples/multi_model_review.py --provider fake
```

---

## License

MIT — see [LICENSE](LICENSE).
