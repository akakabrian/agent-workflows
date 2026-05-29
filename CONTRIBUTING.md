# Contributing to Open Agent Workflows

Thanks for your interest in contributing. This project is a small,
standard-library-first runtime for provider-agnostic dynamic agent workflows.
Contributions that keep it small, dependency-free, and well-tested are very
welcome.

## Project ground rules

- **Stdlib-only core is a hard rule.** The runtime (`src/agent_workflows`)
  must not add any third-party runtime dependency. No Rich, Click, Pydantic,
  Requests, jsonschema, or provider SDKs. If you think you need a dependency,
  open an issue first and we will discuss whether the feature belongs in core
  or in an optional adapter/extra.
- **Python >= 3.11.** Use modern typing (`X | None`, `from __future__ import
  annotations`) consistent with the existing code.
- **Provider-neutral.** Core code must not assume any specific model vendor.
  Vendor specifics live behind adapters.

## Development setup

The runtime has no dependencies, so you can work against the source tree
directly. A virtual environment is recommended but optional.

```bash
git clone <your-fork-url>
cd open-workflows

# Optional but recommended
python3 -m venv .venv
. .venv/bin/activate

# Editable install (gives you the `owf` CLI); dev extras are optional
pip install -e .
# or, if/when dev extras exist:
pip install -e ".[dev]"
```

You can also skip installation entirely and run everything via `PYTHONPATH`:

```bash
PYTHONPATH=src python3 -m agent_workflows --help   # same as `owf`
```

## Running the tests

Tests use only the standard-library `unittest` runner. No test dependencies are
required.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Quick byte-compile sanity check:

```bash
PYTHONPATH=src python3 -m compileall src tests
```

Please add or update tests for any behavior change. Cache-safety and
resume/replay semantics are easy to break subtly, so prefer a focused
regression test over a manual check.

## Coding conventions

- Keep modules small and cohesive; mirror the existing structure
  (`runtime.py`, `cli.py`, `models.py`, `storage.py`, `validation.py`,
  `adapters/`).
- Prefer dataclasses for records (see `models.py`).
- Keep CLI output stable and script-friendly; commands support `--json`.
- Comment the *why* for non-obvious safety invariants (e.g. why mutating calls
  bypass the prompt-only cache), not the *what*.
- No secrets, credentials, tokens, private keys, real home paths, or personal
  email addresses in code, tests, examples, or fixtures.

## Adding an adapter

Adapters translate a `WorkflowCallRequest` into an `AgentResult`. The reference
`FakeAdapter` lives in `src/agent_workflows/adapters/fake.py` and is the best
template.

An adapter is any object with an async `run` method:

```python
from dataclasses import dataclass
from ..models import AgentResult


@dataclass(slots=True)
class MyAdapter:
    name: str = "my-provider"

    async def run(self, request) -> AgentResult:
        # request is a WorkflowCallRequest (see models.py):
        #   request.prompt, request.schema, request.model, request.provider,
        #   request.read_scope, request.write_scope, request.isolation, ...
        text = call_my_backend(request.prompt)          # your integration
        return AgentResult(
            ok=True,
            status="done",
            text=text,
            provider=request.provider,
            model=request.model,
        )
```

Guidelines for adapters:

- Read API keys and endpoints from environment variables or a config file the
  user controls. **Never** hardcode credentials and never write them into the
  run database or artifacts.
- If the backend is a third-party SDK, gate it behind an optional install
  extra so the core stays stdlib-only. Import the SDK lazily inside the
  adapter, not at package import time.
- Populate token/cost fields on `AgentResult` when the backend reports them so
  budget accounting works.
- Respect `request.schema`: either return a structured `value`, or return
  `text` and let the runtime validate/coerce it.
- Honor `read_scope` / `write_scope` / `isolation` if your adapter actually
  touches the filesystem; the runtime treats write-scoped or worktree-isolated
  calls as mutating and will bypass the prompt-only cache.

## Pull request expectations

- One focused change per PR. Keep diffs reviewable.
- Tests pass: `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- No new runtime dependencies in core (this will be checked).
- Update `README.md` and `SKILL.md` when behavior or shape changes meaningfully.
- Describe the *why*, the user-visible effect, and how you verified it.
- By contributing, you agree your contributions are licensed under the MIT
  license in [LICENSE](LICENSE).

See [SECURITY.md](SECURITY.md) for the security model and how to report
vulnerabilities, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community
expectations.
