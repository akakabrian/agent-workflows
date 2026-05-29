# Open Agent Workflows

**Agent-agnostic dynamic workflows for Python.**

Open Agent Workflows is a zero-dependency, stdlib-only Python runtime for scripted multi-agent workflows: fan out agent calls, validate structured outputs, resume interrupted runs, and keep a durable local record of what happened.

It is inspired by the dynamic workflow pattern popularized by Anthropic's Claude: a script coordinates multiple agent calls, runs independent work in parallel, validates outputs, and synthesizes the result.

Open Agent Workflows keeps that ergonomic script style, but makes the runtime local and inspectable: every run is stored in SQLite, every call returns a structured `AgentResult`, mutating calls bypass unsafe prompt-only cache, and worktree isolation fails closed instead of silently falling back to in-place edits.

Open Agent Workflows is not affiliated with Anthropic. Anthropic and Claude are trademarks of Anthropic.

```python
from workflows import agent, parallel, phase, log, meta

meta(name="repo-health-check", description="Check repositories for missing project docs")

REPOS = ["hive", "books", "clarion"]

async def main(args):
    phase("Scan")

    results = await parallel([
        lambda repo=repo: agent(
            f"Check {repo} for README.md, AGENTS.md, and STATE.md. Report gaps.",
            label=f"scan:{repo}",
            schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "missing": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["repo", "missing"],
            },
        )
        for repo in REPOS
    ])

    phase("Report")
    gaps = [r.value for r in results if r.ok and r.value["missing"]]
    log("completed", repos=len(REPOS), gaps=len(gaps))

    return {"gaps": gaps}
```

Run it offline with the fake provider:

```bash
owf run examples/repo_health_check.py --provider fake
owf status latest
owf output latest
owf report latest --stdout
```

---

## Why it exists

Anthropic's Dynamic Workflows made a useful pattern visible: agents can do more when a script coordinates them.

But the pattern should not require one product, one model, or one hidden runtime.

Open Agent Workflows gives you a small local runtime for the core idea:

```text
script -> agent calls -> structured outputs -> cache/resume -> report
```

It is designed to be:

- **Agent-agnostic** — use the fake provider, Claude CLI, Codex CLI, or future adapters.
- **Script-first** — workflows are plain Python files with `async def main(args)`.
- **Durable** — runs, calls, events, artifacts, and cache records are stored in SQLite.
- **Resumable** — read-only calls can replay from cache after interruption.
- **Inspectable** — prompts, outputs, reports, and artifacts are saved locally.
- **Dependency-light** — the runtime uses only the Python standard library.
- **Honest about safety** — scripts are trusted local Python, not sandboxed.

---

## Where this goes further

Open Agent Workflows is not a clone of Claude Dynamic Workflows. It is a small, local runtime for the same workflow pattern, with several deliberate product improvements:

| Area | Claude Dynamic Workflows | Open Agent Workflows |
|---|---|---|
| Provider choice | Claude product/runtime | Agent-agnostic adapter layer |
| Runtime ownership | Product-managed | Local Python runtime |
| Run history | Product UI / managed runtime | SQLite run/call/event/artifact store |
| Return values | Final text or validated object | `AgentResult` with status, cache, provider, model, usage, artifacts, and worktree metadata |
| Cache behavior | Prompt/options memoization | Explicit cache policy with mutating calls bypassing prompt-only cache |
| Mutation safety | Worktree option, merge handled manually | Fail-closed worktree isolation; provider is not invoked if isolation fails |
| Offline onboarding | Product-dependent | `--provider fake` works with no model configured |
| Reports | Product UI | Local Markdown/HTML reports |
| Dependencies | Product runtime | Python standard library only |
| Extensibility | Claude environment | Add adapters for other agents, CLIs, APIs, or local models |

The goal is not to outdo Claude as a model product. The goal is to make the dynamic workflow pattern portable, inspectable, and safe enough for local developer use.

---

## Quickstart

```bash
git clone https://github.com/akakabrian/agent-workflows.git
cd agent-workflows
pip install -e .

owf new examples/my_first_workflow.py
owf run examples/my_first_workflow.py --provider fake
owf status latest
owf output latest
owf report latest --stdout
```

You just ran a durable, resumable agent workflow without configuring a model provider.

Python 3.11 or later is required. No runtime dependencies are required beyond the Python standard library.

---

## Installation

From source:

```bash
git clone https://github.com/akakabrian/agent-workflows.git
cd agent-workflows
pip install -e .
owf --help
```

PyPI package name, once published:

```bash
pip install open-agent-workflows
```

---

## What it gives you

### Claude-style dynamic workflow primitives

Import from `workflows` for a short script-friendly API:

```python
from workflows import agent, parallel, pipeline, phase, log, workflow, budget, meta
```

or from the canonical package:

```python
from agent_workflows import agent, parallel, pipeline
```

### A durable local run store

By default, workflow state is stored next to the script:

```text
<script_dir>/.workflows/
  workflow.sqlite
  runs/
    <run_id>/
      manifest.json
      summary.md
      output.json
      report.md
      report.html
      calls/
        <call_id>/
          prompt.txt
          output.txt
          output.json
          error.txt
```

### A provider-agnostic adapter layer

Initial providers:

| Provider | Aliases | How it works | Auth |
|---|---|---|---|
| `fake` | `fixture` | Offline deterministic provider for examples and tests. | None |
| `claude` | `anthropic`, `claude-cli` | Shells out to Claude CLI print mode. | Uses the CLI's auth. |
| `codex` | `openai`, `codex-cli` | Shells out to Codex CLI exec mode. | Uses the CLI's auth. |

Open Agent Workflows does not manage or store credentials. Provider CLIs keep their own authentication.

---

## Core API

### `meta()`

Declare workflow metadata:

```python
from workflows import meta

meta(
    name="research-scan",
    description="Fan out research tasks and synthesize findings",
    phases=["Plan", "Scan", "Synthesize"],
)
```

Metadata is stored in the run manifest and report.

---

### `await agent(prompt, ...)`

Run one agent call.

```python
result = await agent(
    "Summarize the following document in three bullets: ...",
    label="summarize:doc-1",
    phase="Summarize",
)
```

`agent()` returns an `AgentResult`, not just a string.

Important fields:

| Field | Meaning |
|---|---|
| `.ok` | Whether the call succeeded and schema validation passed. |
| `.status` | Stable status such as `done`, `failed`, `schema_failed`, `timeout`, `provider_failed`. |
| `.text` | Raw text output, when present. |
| `.value` | Validated JSON value, when a schema is used. |
| `.cache_status` | `hit`, `miss`, `bypassed`, or `disabled`. |
| `.provider` / `.model` | Provider and model used for this call. |
| `.input_tokens` / `.output_tokens` | Provider-reported token usage, when available. |
| `.estimated_cost_usd` | Provider-reported cost, when available. |
| `.prompt_path` | Local path to saved prompt artifact. |
| `.output_text_path` / `.output_json_path` | Local path to saved output artifact. |
| `.worktree_path` | Git worktree path, when worktree isolation was used and changes were made. |
| `.changed_files` | Changed files detected in the worktree. |

Convenience helpers:

```python
result.require_ok()
text = result.text_or_raise()
value = result.value_or_raise()
```

---

### Structured output with schemas

Pass a JSON Schema subset to request and validate structured output:

```python
FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "risks"],
}

result = await agent(
    "Review this design and return risks.",
    schema=FINDINGS_SCHEMA,
    label="review:design",
)

findings = result.value_or_raise()
```

The built-in validator intentionally supports a practical subset of JSON Schema so the runtime can stay dependency-free.

Supported basics include:

- `object`
- `array`
- `string`
- `number`
- `integer`
- `boolean`
- `null`
- `properties`
- `required`
- `items`
- `enum`

---

### `await parallel(thunks, concurrency=None, fail_fast=False)`

Run independent agent calls concurrently and preserve result order.

```python
topics = ["architecture", "security", "tests"]

results = await parallel(
    [
        lambda topic=topic: agent(
            f"Review the project for {topic} issues.",
            label=f"review:{topic}",
        )
        for topic in topics
    ],
    concurrency=3,
)
```

Use `parallel()` when subtasks are independent and speed matters.

With `fail_fast=True`, cancellation is best-effort: the first failed result or exception stops scheduling new work and cancels still-pending tasks. Calls that already finished are returned with their normal result; cancelled or unscheduled calls are returned as `AgentResult(status="cancelled")`.

---

### `await pipeline(items, fn, stop_on_error=False)`

Process items sequentially.

```python
results = await pipeline(
    documents,
    lambda doc: agent(f"Extract action items from: {doc}", label="extract"),
)
```

Use `pipeline()` when order matters, later steps depend on earlier results, or sequential execution is safer.

---

### `phase(name)` and `log(message, **metadata)`

Mark progress and emit structured events.

```python
phase("Scan")
log("starting", count=len(items))

# ...

phase("Synthesize")
log("completed", successful=sum(1 for r in results if r.ok))
```

Phases and logs appear in run records and reports.

---

### `await workflow(path, args=None)`

Invoke a child workflow script.

```python
child = await workflow("steps/fetch_sources.py", args={"query": "workflow runtimes"})
return {"child_output": child["output"]}
```

Child workflows share the parent's home, budget, and provider defaults. Nesting is limited to one child level for now.

---

### `budget`

Inspect and enforce run budget from inside the script.

```python
from workflows import budget

if budget.can_spend(20_000):
    result = await agent("Do the expensive review.", label="deep-review")

print(budget.spent_tokens, budget.remaining_tokens)
```

The runtime updates budget usage as provider adapters report tokens/cost.

---

## Agent call options

```python
result = await agent(
    prompt,
    label="scan:hive",
    phase="Scan",
    schema=FINDINGS_SCHEMA,
    provider="claude",
    model="claude-opus-4-5",
    agent_type="code-reviewer",
    isolation="worktree",
    cache_policy="auto",
    cache_namespace="v2",
    read_scope=["src/", "tests/"],
    write_scope=["src/auth.py"],
    permissions={"shell": "read-only"},
    timeout_seconds=600,
    metadata={"repo": "hive"},
)
```

| Option | Meaning |
|---|---|
| `label` | Human-readable call label shown in status/report output. |
| `phase` | Phase grouping. Defaults to the current `phase()`. |
| `schema` | JSON Schema subset for structured output validation. |
| `provider` | Per-call provider override. |
| `model` | Per-call model override. |
| `agent_type` | Optional provider/native agent profile name. |
| `isolation` | `none` or `worktree`. |
| `cache_policy` | `auto`, `disabled`, `read_only`, or `refresh`. |
| `cache_namespace` | Manual cache namespace for versioning or busting cache. |
| `read_scope` | Declarative read scope metadata. |
| `write_scope` | Declarative write scope; non-empty means mutating. |
| `permissions` | Declarative permission metadata for adapters/humans. |
| `timeout_seconds` | Provider call timeout. |
| `metadata` | Extra metadata stored with the call. |

`read_scope`, `write_scope`, and `permissions` are metadata for adapters and human review. They are not an OS sandbox.

---

## Cache and resume semantics

Open Agent Workflows uses local SQLite-backed cache records for safe replay.

### Read-only calls

Read-only calls are keyed by a hash of:

- runtime version
- script hash
- prompt
- schema
- provider
- model
- agent type
- cache namespace
- selected options

If the same read-only call appears in a later run or resume, it can replay instantly from cache.

```text
miss -> first live execution
hit  -> reused prior read-only result
```

### `cache_policy="refresh"`

`refresh` skips the cache read, runs the call live, and writes the result back to the same reusable cache key that `auto` / `read_only` use.

Use this when you want to recompute a read-only call without changing the prompt.

### Mutating calls

Mutating calls bypass prompt-only cache.

A call is mutating if it has:

- non-empty `write_scope`
- `isolation="worktree"`
- future write-capable adapter behavior

Mutating calls always re-execute on resume because a cached text result does not prove filesystem side effects still exist.

```text
bypassed -> mutating call; prompt-only cache is unsafe
```

Inspect cache decisions:

```bash
owf explain-cache latest
```

Example:

```text
miss      scan:hive: no prior cached result existed for this call key
hit       scan:books: reused a prior read-only result
bypassed  patch: mutating call; prompt-only cache is unsafe
```

---

## Worktree isolation

For mutating work, prefer:

```python
await agent(
    "Implement the fix in src/auth.py.",
    write_scope=["src/auth.py"],
    isolation="worktree",
)
```

Worktree isolation creates a fresh git worktree for the call. The adapter runs inside that worktree so file edits do not touch your current working tree.

After the call:

```python
print(result.worktree_path)
print(result.worktree_branch)
print(result.changed_files)
```

Nothing is auto-merged. Review and merge manually.

Worktree isolation fails closed: if the runtime cannot create a worktree, the provider is not invoked and the call records `AgentResult(ok=False, status="worktree_failed")`.

---

## CLI reference

```text
owf init                              Initialize local workflow store
owf new <path>                        Scaffold a starter workflow script
owf examples                          List bundled examples
owf doctor                            Run local environment diagnostics
owf validate <script>                 Parse and check workflow metadata/main()
owf dry-run <script> [OPTIONS]        Preview run manifest without execution
owf run <script> [OPTIONS]            Execute a workflow
owf resume <run_id|latest>            Replay a prior run, reusing safe cache hits
owf status <run_id|latest>            Show run summary
owf output <run_id|latest>            Print output.json
owf calls <run_id|latest>             List call records
owf explain-cache <run_id|latest>     Explain per-call cache decisions
owf report <run_id|latest>            Write Markdown or HTML report
owf artifacts <run_id|latest>         List stored artifacts
owf cat <call_id> [--prompt]          Print a call output or prompt
```

Common run options:

```text
--provider fake|claude|codex
--model MODEL
--budget-tokens N
--budget-cost-usd N.NN
--cache-policy auto|disabled|read_only|refresh
--args-json '{"key": "value"}'
--arg KEY=VALUE
--json
--home PATH
--debug
```

Examples:

```bash
owf run examples/hello_workflow.py --provider fake
owf run examples/parallel_research.py --provider claude
owf run examples/multi_model_review.py --provider codex --model gpt-5.5
owf report latest --html
```

---

## For agents: when to use a workflow

Use Open Agent Workflows when the task benefits from durable, structured, multi-agent execution.

Good workflow triggers:

- the user explicitly says "workflow", "dynamic workflow", "fan out", "parallelize", or "use multiple agents"
- the task can be split into independent subtasks
- the result should be resumable, auditable, or reported
- multiple files, repos, documents, models, test cases, or hypotheses need the same treatment
- one agent should produce work and another should verify it
- structured JSON outputs or schema validation would reduce ambiguity
- the work is long-running or interruption-prone

Do not use a workflow for simple one-shot answers, tiny edits, or tasks that need clarification before decomposition.

Safe default:

```text
Direct answer for simple tasks.
Workflow for parallel, long-running, schema-driven, or verification-heavy tasks.
Ask before mutating files or spending real provider tokens.
```

### Explicit mode

Use a workflow when the user says:

```text
use a workflow
make this a workflow
run a dynamic workflow
fan this out
parallelize this
use multiple agents
run separate agents on this
compare multiple approaches
have one agent implement and another verify
use owf
```

### Auto mode

When operating in an auto/high-effort mode, choose a workflow without waiting for the word "workflow" when the task is broad, decomposable, parallelizable, verification-heavy, or valuable to preserve as an auditable run.

Before executing a mutating or expensive workflow, describe the plan and ask for confirmation.

---

## Comparison with Claude Dynamic Workflows

| Capability | Claude Dynamic Workflows | Open Agent Workflows |
|---|---|---|
| Core workflow pattern | Script coordinates multiple agent calls | Same pattern, implemented as local Python |
| Provider | Claude | Agent-agnostic adapter layer |
| Visibility | Product-managed progress UI | SQLite + local artifacts + Markdown/HTML reports |
| Return value | Text/object | `AgentResult` with status, cache, usage, artifact, and worktree metadata |
| Resume/cache | Product-managed memoization | Explicit cache policy and `owf explain-cache` |
| Mutating calls | Worktree available, merge manual | Mutating calls never use prompt-only cache; worktree isolation fails closed |
| Offline mode | Product-dependent | Built-in fake provider |
| Extensibility | Claude environment | Add adapters for CLIs, APIs, or local models |

Claude made the pattern visible. Open Agent Workflows makes the pattern portable and inspectable.

---

## Safety model

Workflow scripts are trusted local Python.

The runtime loads a script with `importlib` and executes its `async main(args)` in your Python process with your user's privileges. There is no sandbox, container, or permission boundary.

Treat workflow scripts like any other Python program:

- only run scripts you wrote or reviewed
- do not run untrusted workflows from the internet
- assume prompts, args, outputs, metadata, and logs may be persisted
- do not put secrets in prompts, args, metadata, or logs
- use `--provider fake` for safe local examples
- use `isolation="worktree"` for file-mutating agent calls

Provider credentials are handled by the provider adapters or provider CLIs. Open Agent Workflows does not manage or store credentials.

See [`SECURITY.md`](SECURITY.md) for details.

---

## Examples

| File | What it shows |
|---|---|
| `examples/hello_workflow.py` | Minimal `agent()` + `log()` |
| `examples/schema_validation.py` | JSON Schema validation |
| `examples/parallel_research.py` | `parallel()` fan-out and aggregation |
| `examples/multi_model_review.py` | Same prompt across providers/models |

Run examples offline:

```bash
owf run examples/hello_workflow.py --provider fake
owf run examples/schema_validation.py --provider fake
owf run examples/parallel_research.py --provider fake
owf run examples/multi_model_review.py --provider fake
```

---

## Design principles

Open Agent Workflows should stay:

- small
- local
- scriptable
- provider-agnostic
- dependency-light
- honest about safety
- inspectable through files and SQLite
- practical for real agent workflows

It should not become:

- a hidden cloud service
- a heavy DAG framework
- a model vendor wrapper only
- a sandbox it cannot actually enforce
- a replacement for human review of mutating agent work

---

## Project status

Pre-1.0.

The runtime is useful, but the API and storage schema may still change. Pin versions for serious use.

Good early use cases:

- offline workflow prototyping with `--provider fake`
- multi-agent research scripts
- schema-validated extraction
- model/provider comparison
- repo/document scanning
- durable agent-call reports
- experiments inspired by Claude-style dynamic workflows

---

## License

MIT — see [`LICENSE`](LICENSE).
