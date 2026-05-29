"""parallel_research.py — fan out research calls in parallel with schema aggregation.

Run offline:
    owf run examples/parallel_research.py --provider fake
    owf output latest
"""

from workflows import agent, log, meta, parallel, phase

meta(
    name="parallel-research",
    description="Fan out research queries across several topics in parallel, then aggregate.",
    phases=["research", "aggregate"],
)


FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "topic":      {"type": "string"},
        "summary":    {"type": "string"},
        "confidence": {"type": "number"},
        "sources":    {"type": "array", "items": {"type": "string"}},
    },
    "required": ["topic", "summary", "confidence"],
    "additionalProperties": False,
}


async def main(args):
    topics = args.get("topics") or ["climate change", "renewable energy", "carbon capture"]
    if isinstance(topics, str):
        topics = [t.strip() for t in topics.split(",") if t.strip()]

    phase("research")
    log("starting fan-out", topic_count=len(topics))

    results = await parallel(
        [
            lambda t=t: agent(
                f"Provide a brief research summary for the topic: {t}",
                label=f"research:{t}",
                schema=FINDING_SCHEMA,
            )
            for t in topics
        ],
        concurrency=4,
        fail_fast=False,
    )

    phase("aggregate")
    findings = []
    errors = []
    for topic, result in zip(topics, results):
        if result.ok:
            finding = result.value
            # Ensure the topic field is set even if the model omits it.
            if not finding.get("topic"):
                finding["topic"] = topic
            findings.append(finding)
            log("finding ok", topic=topic, cache_status=result.cache_status)
        else:
            errors.append({"topic": topic, "error": result.error})
            log("finding failed", topic=topic, error=result.error)

    # Sort by confidence descending so the most certain findings appear first.
    findings.sort(key=lambda f: f.get("confidence", 0), reverse=True)

    log(
        "aggregation complete",
        total=len(topics),
        succeeded=len(findings),
        failed=len(errors),
    )
    return {
        "findings": findings,
        "errors": errors,
        "total": len(topics),
        "succeeded": len(findings),
    }
