"""Standalone async batch clients (Anthropic + OpenAI).

Batch APIs run a large set of independent prompts asynchronously (up to ~24h)
at roughly half the synchronous price. This module is intentionally decoupled
from the workflow runtime: you submit a JSONL of prompts, get a batch id, and
fetch results later. Costs reported by ``fetch`` apply the ~50% batch discount.

Everything is standard-library only (``urllib`` via :mod:`._common`). API keys
are read from the environment and never persisted; a small local record under
``<home>/batches/<batch_id>.json`` remembers which provider/model a batch used
so ``status``/``fetch`` work without re-passing flags.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ._common import (
    AdapterError,
    estimate_cost,
    post_json,
    post_multipart,
    request_text,
)

BATCH_DISCOUNT = 0.5  # batch APIs bill ~50% of synchronous rates.

_DEFAULTS = {
    "anthropic": {"base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-4-6"},
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "model": "gpt-5.4-mini"},
}


# ---------------------------------------------------------------------------
# Local batch record store
# ---------------------------------------------------------------------------

def batches_dir(home: str | Path | None) -> Path:
    base = Path(home).expanduser() if home else Path.home() / ".workflows"
    path = base / "batches"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_path(home: str | Path | None, batch_id: str) -> Path:
    safe = batch_id.replace("/", "_")
    return batches_dir(home) / f"{safe}.json"


def save_record(home: str | Path | None, record: dict[str, Any]) -> Path:
    path = _record_path(home, record["batch_id"])
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_record(home: str | Path | None, batch_id: str) -> dict[str, Any]:
    path = _record_path(home, batch_id)
    if not path.is_file():
        raise AdapterError(f"no local record for batch {batch_id!r} (looked in {path.parent})")
    return json.loads(path.read_text(encoding="utf-8"))


def list_records(home: str | Path | None) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(batches_dir(home).glob("*.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


# ---------------------------------------------------------------------------
# Prompt loading + normalization
# ---------------------------------------------------------------------------

def load_prompts(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL file of prompt requests.

    Each line is an object: ``{"prompt": "...", "custom_id"?: str, "model"?: str,
    "system"?: str, "max_tokens"?: int}``. A bare JSON string is also accepted as
    shorthand for ``{"prompt": <string>}``.
    """
    items: list[dict[str, Any]] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if isinstance(obj, str):
            obj = {"prompt": obj}
        if not isinstance(obj, dict) or "prompt" not in obj:
            raise AdapterError(f"{path}:{lineno}: each line needs a 'prompt' field")
        obj.setdefault("custom_id", f"req-{len(items)}")
        items.append(obj)
    if not items:
        raise AdapterError(f"{path}: no prompts found")
    return items


def _key(provider: str, key_env: str) -> str:
    key = os.environ.get(key_env)
    if not key:
        raise AdapterError(f"batch provider {provider!r} needs an API key in ${key_env}")
    return key


# ---------------------------------------------------------------------------
# Anthropic Message Batches (inline requests)
# ---------------------------------------------------------------------------

class AnthropicBatchClient:
    provider = "anthropic"

    def __init__(self) -> None:
        cfg = _DEFAULTS["anthropic"]
        self.base_url = cfg["base_url"]
        self.key_env = cfg["key_env"]
        self.default_model = cfg["model"]
        self.api_version = "2023-06-01"

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": _key(self.provider, self.key_env), "anthropic-version": self.api_version}

    def submit(self, prompts: list[dict[str, Any]], *, model: str | None, max_tokens: int = 4096) -> str:
        requests = []
        for item in prompts:
            params: dict[str, Any] = {
                "model": item.get("model") or model or self.default_model,
                "max_tokens": int(item.get("max_tokens") or max_tokens),
                "messages": [{"role": "user", "content": item["prompt"]}],
            }
            if item.get("system"):
                params["system"] = item["system"]
            requests.append({"custom_id": item["custom_id"], "params": params})
        res = post_json(f"{self.base_url}/v1/messages/batches", {"requests": requests}, self._headers(), timeout=120)
        if res.status not in (200, 201):
            raise AdapterError(f"anthropic batch submit failed: HTTP {res.status}: {res.body[:300]}")
        return json.loads(res.body)["id"]

    def status(self, batch_id: str) -> dict[str, Any]:
        res = request_text(f"{self.base_url}/v1/messages/batches/{batch_id}", self._headers())
        if res.status != 200:
            raise AdapterError(f"anthropic batch status failed: HTTP {res.status}: {res.body[:300]}")
        obj = json.loads(res.body)
        ended = obj.get("processing_status") == "ended"
        return {"done": ended, "raw_status": obj.get("processing_status"), "counts": obj.get("request_counts"), "results_url": obj.get("results_url")}

    def fetch(self, batch_id: str, *, model: str | None) -> list[dict[str, Any]]:
        st = self.status(batch_id)
        if not st["done"] or not st.get("results_url"):
            return []
        res = request_text(st["results_url"], self._headers())
        if res.status != 200:
            raise AdapterError(f"anthropic batch results failed: HTTP {res.status}")
        results = []
        for line in res.body.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            cid = obj.get("custom_id")
            result = obj.get("result") or {}
            if result.get("type") == "succeeded":
                message = result.get("message") or {}
                text = "".join(
                    b.get("text", "") for b in (message.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"
                )
                usage = message.get("usage") or {}
                in_tok, out_tok = usage.get("input_tokens"), usage.get("output_tokens")
                cost = estimate_cost(message.get("model") or model or self.default_model, in_tok, out_tok)
                results.append({
                    "custom_id": cid, "ok": True, "text": text.strip(),
                    "input_tokens": in_tok, "output_tokens": out_tok,
                    "cost_usd": round(cost * BATCH_DISCOUNT, 9) if cost is not None else None,
                })
            else:
                err = result.get("error") or result.get("type") or "errored"
                results.append({"custom_id": cid, "ok": False, "error": json.dumps(err) if not isinstance(err, str) else err})
        return results


# ---------------------------------------------------------------------------
# OpenAI Batches (file upload + /v1/batches)
# ---------------------------------------------------------------------------

class OpenAIBatchClient:
    provider = "openai"

    def __init__(self) -> None:
        cfg = _DEFAULTS["openai"]
        self.base_url = cfg["base_url"]
        self.key_env = cfg["key_env"]
        self.default_model = cfg["model"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {_key(self.provider, self.key_env)}"}

    def submit(self, prompts: list[dict[str, Any]], *, model: str | None, max_tokens: int | None = None) -> str:
        lines = []
        for item in prompts:
            body: dict[str, Any] = {
                "model": item.get("model") or model or self.default_model,
                "messages": [{"role": "user", "content": item["prompt"]}],
            }
            if item.get("system"):
                body["messages"].insert(0, {"role": "system", "content": item["system"]})
            if item.get("max_tokens") or max_tokens:
                body["max_tokens"] = int(item.get("max_tokens") or max_tokens)
            lines.append(json.dumps({"custom_id": item["custom_id"], "method": "POST", "url": "/v1/chat/completions", "body": body}))
        jsonl = ("\n".join(lines) + "\n").encode("utf-8")

        upload = post_multipart(f"{self.base_url}/files", {"purpose": "batch"}, "file", "batch.jsonl", jsonl, self._headers())
        if upload.status not in (200, 201):
            raise AdapterError(f"openai file upload failed: HTTP {upload.status}: {upload.body[:300]}")
        file_id = json.loads(upload.body)["id"]

        create = post_json(
            f"{self.base_url}/batches",
            {"input_file_id": file_id, "endpoint": "/v1/chat/completions", "completion_window": "24h"},
            self._headers(),
            timeout=60,
        )
        if create.status not in (200, 201):
            raise AdapterError(f"openai batch create failed: HTTP {create.status}: {create.body[:300]}")
        return json.loads(create.body)["id"]

    def status(self, batch_id: str) -> dict[str, Any]:
        res = request_text(f"{self.base_url}/batches/{batch_id}", self._headers())
        if res.status != 200:
            raise AdapterError(f"openai batch status failed: HTTP {res.status}: {res.body[:300]}")
        obj = json.loads(res.body)
        return {"done": obj.get("status") == "completed", "raw_status": obj.get("status"), "counts": obj.get("request_counts"), "output_file_id": obj.get("output_file_id")}

    def fetch(self, batch_id: str, *, model: str | None) -> list[dict[str, Any]]:
        st = self.status(batch_id)
        if not st["done"] or not st.get("output_file_id"):
            return []
        res = request_text(f"{self.base_url}/files/{st['output_file_id']}/content", self._headers())
        if res.status != 200:
            raise AdapterError(f"openai batch output failed: HTTP {res.status}")
        results = []
        for line in res.body.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            cid = obj.get("custom_id")
            response = obj.get("response") or {}
            body = response.get("body") or {}
            if response.get("status_code") == 200 and body.get("choices"):
                text = (body["choices"][0].get("message", {}).get("content") or "").strip()
                usage = body.get("usage") or {}
                in_tok, out_tok = usage.get("prompt_tokens"), usage.get("completion_tokens")
                cost = estimate_cost(body.get("model") or model or self.default_model, in_tok, out_tok)
                results.append({
                    "custom_id": cid, "ok": True, "text": text,
                    "input_tokens": in_tok, "output_tokens": out_tok,
                    "cost_usd": round(cost * BATCH_DISCOUNT, 9) if cost is not None else None,
                })
            else:
                results.append({"custom_id": cid, "ok": False, "error": json.dumps(obj.get("error") or response.get("status_code"))})
        return results


_CLIENTS = {"anthropic": AnthropicBatchClient, "openai": OpenAIBatchClient}


def build_batch_client(provider: str):
    factory = _CLIENTS.get((provider or "").lower())
    if factory is None:
        raise AdapterError(f"batch is not supported for provider {provider!r}; supported: {', '.join(sorted(_CLIENTS))}")
    return factory()


# ---------------------------------------------------------------------------
# High-level operations used by the CLI
# ---------------------------------------------------------------------------

def submit_batch(provider: str, prompts_path: str | Path, *, model: str | None, home: str | Path | None) -> dict[str, Any]:
    client = build_batch_client(provider)
    prompts = load_prompts(prompts_path)
    batch_id = client.submit(prompts, model=model)
    record = {
        "batch_id": batch_id,
        "provider": client.provider,
        "model": model or client.default_model,
        "count": len(prompts),
        "prompts": {item["custom_id"]: item["prompt"] for item in prompts},
    }
    save_record(home, record)
    return {"batch_id": batch_id, "provider": client.provider, "count": len(prompts), "model": record["model"]}


def batch_status(batch_id: str, *, home: str | Path | None) -> dict[str, Any]:
    record = load_record(home, batch_id)
    client = build_batch_client(record["provider"])
    return {"batch_id": batch_id, "provider": record["provider"], **client.status(batch_id)}


def fetch_batch(batch_id: str, *, home: str | Path | None) -> dict[str, Any]:
    record = load_record(home, batch_id)
    client = build_batch_client(record["provider"])
    st = client.status(batch_id)
    if not st["done"]:
        return {"batch_id": batch_id, "done": False, "raw_status": st.get("raw_status"), "counts": st.get("counts"), "results": []}
    results = client.fetch(batch_id, model=record.get("model"))
    prompts = record.get("prompts") or {}
    for row in results:
        row["prompt"] = prompts.get(row.get("custom_id"))
    total_cost = round(sum(r.get("cost_usd") or 0.0 for r in results), 9)
    return {"batch_id": batch_id, "done": True, "provider": record["provider"], "results": results, "total_cost_usd": total_cost}
