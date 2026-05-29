"""multi_model_review.py — run the same prompt through multiple provider/model
combinations and aggregate their reviews.

By default all slots use provider="fake" so the script runs offline.
To use real providers, edit the REVIEWERS list or pass --arg reviewers=... and
--provider / --model overrides.

Run offline:
    owf run examples/multi_model_review.py --provider fake
    owf output latest
"""

from workflows import agent, log, meta, parallel, phase

meta(
    name="multi-model-review",
    description="Run the same code review prompt through several provider/model slots and compare.",
    phases=["review", "compare"],
)


# Each entry is (label, provider, model).  All default to "fake" so the example
# works offline.  Swap in real providers once you have the CLIs installed.
REVIEWERS = [
    ("reviewer-a", "fake", "fake"),
    ("reviewer-b", "fake", "fake"),
    ("reviewer-c", "fake", "fake"),
]

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict":        {"type": "string", "enum": ["approve", "request_changes", "comment"]},
        "summary":        {"type": "string"},
        "issues":         {"type": "array", "items": {"type": "string"}},
        "suggestions":    {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "summary"],
    "additionalProperties": False,
}

DEFAULT_CODE = """\
def divide(a, b):
    return a / b
"""


async def main(args):
    code = args.get("code", DEFAULT_CODE)
    context = args.get("context", "Python utility function")

    prompt = (
        f"Review the following {context} for correctness, safety, and style.\n\n"
        f"```python\n{code}\n```\n\n"
        "Return a structured review."
    )

    phase("review")
    log("dispatching reviews", reviewer_count=len(REVIEWERS))

    results = await parallel(
        [
            lambda label=label, prov=prov, mdl=mdl: agent(
                prompt,
                label=label,
                schema=REVIEW_SCHEMA,
                provider=prov,
                model=mdl,
            )
            for label, prov, mdl in REVIEWERS
        ],
        concurrency=len(REVIEWERS),
        fail_fast=False,
    )

    phase("compare")
    reviews = []
    errors = []
    for (label, provider, model), result in zip(REVIEWERS, results):
        if result.ok:
            review = result.value
            review["_reviewer"] = label
            review["_provider"] = provider
            review["_model"] = model
            review["_cache_status"] = result.cache_status
            reviews.append(review)
            log("review ok", label=label, verdict=review.get("verdict"))
        else:
            errors.append({"reviewer": label, "error": result.error})
            log("review failed", label=label, error=result.error)

    # Tally verdicts.
    verdict_counts: dict[str, int] = {}
    for r in reviews:
        v = r.get("verdict", "unknown")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1

    log("comparison complete", verdicts=verdict_counts, errors=len(errors))
    return {
        "reviews": reviews,
        "errors": errors,
        "verdict_counts": verdict_counts,
        "consensus": max(verdict_counts, key=verdict_counts.get) if verdict_counts else None,
    }
