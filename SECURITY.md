# Security Policy

## The most important thing to understand

**Workflow scripts are trusted local Python and run with no sandbox. Only run
scripts you trust.**

Open Agent Workflows loads a workflow script with `importlib` and executes its
`async main(args)` in your own Python process, with your user's full
privileges. A workflow script can do anything your account can do: read and
write files, make network calls, spawn processes, and (through an adapter)
drive a real model CLI or API. There is no sandbox, container, or permission
boundary between a workflow script and your machine.

Treat a workflow script exactly as you would treat any other Python program you
are about to run:

- Only run workflows you wrote or have reviewed.
- Do not run untrusted workflows from the internet, issues, or pull requests on
  a machine you care about.
- Be especially careful with workflows that declare `write_scope`,
  `isolation="worktree"`, or `mutates_files=True`, or that use an adapter wired
  to a real provider with tool/file access.

The `read_scope` / `write_scope` / `permissions` fields on a call are
declarative metadata for adapters and for human review. The core runtime does
**not** enforce them as an OS-level sandbox.

## Cache safety: mutating calls bypass the prompt-only cache

The run cache is keyed on the prompt and call parameters, not on filesystem
state. A cached output proves only that a prompt previously produced some text
or JSON — it does **not** prove that any side effects (file edits, commits,
external writes) still hold.

Therefore, calls that are **mutating** — those with a non-empty `write_scope`
or any `isolation` other than `"none"` — never read from or write to the
prompt-only cache. They re-execute on every run and resume, and are reported
with `cache_status="bypassed"`. Read-only calls memoize and may be reused
across resume. Keep this invariant intact: silently caching a mutating call
would let a resume skip real side effects.

## Secrets handling

- API keys, tokens, and endpoints are read by adapters from environment
  variables or a user-controlled config file.
- Credentials are **never** written to the SQLite run database, the run
  manifest, artifacts, summaries, or any other file the runtime persists. The
  run store records prompts, outputs, hashes, token/cost accounting, and
  metadata — not secrets.
- Do not place secrets in workflow `args`, prompts, `metadata`, or logs, since
  those *are* persisted to the run store and artifacts.
- Never commit secrets to the repository. See `.gitignore`; the local run store
  (`.workflows/`), virtualenvs, and build artifacts are ignored by default.

## Supported versions

This project is pre-1.0. Security fixes target the latest released version and
`main`. Older `0.x` versions are not separately maintained.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

- **Preferred:** open a private security advisory via GitHub
  ("Security" tab → "Report a vulnerability") so the report stays confidential
  until a fix is available.
- **Security contact:** TBD (a dedicated security contact address will be
  published here before the first stable release).

Please include a description of the issue, affected versions, and a minimal
reproduction if you can. We will acknowledge the report, investigate, and
coordinate a fix and disclosure timeline with you. Please give us a reasonable
opportunity to address the issue before any public disclosure.
