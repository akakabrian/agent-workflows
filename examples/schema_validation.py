"""schema_validation.py — demonstrate JSON Schema enforcement on a single call.

Run offline:
    owf run examples/schema_validation.py --provider fake
    owf output latest
"""

from workflows import agent, log, meta, phase

meta(
    name="schema-validation",
    description="Extract structured data from a prompt with JSON Schema validation.",
    phases=["extract"],
)


SCHEMA = {
    "type": "object",
    "properties": {
        "title":    {"type": "string"},
        "language": {"type": "string"},
        "topics":   {"type": "array", "items": {"type": "string"}},
        "lines":    {"type": "integer"},
    },
    "required": ["title", "language", "topics", "lines"],
    "additionalProperties": False,
}


async def main(args):
    snippet = args.get("snippet", "def hello():\n    print('hello world')\n")

    phase("extract")
    result = await agent(
        f"Analyse this code snippet and return a structured summary:\n\n{snippet}",
        label="analyse-snippet",
        schema=SCHEMA,
    )

    if not result.ok:
        log("extraction failed", error=result.error, status=result.status)
        return {"ok": False, "error": result.error}

    # result.value is the validated JSON dict.
    summary = result.value
    log(
        "extracted",
        title=summary.get("title"),
        language=summary.get("language"),
        topics=summary.get("topics"),
        cache_status=result.cache_status,
    )
    return {"ok": True, "summary": summary}
