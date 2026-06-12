"""Score View — CLI to query worst/best scoring calls from Redis.

Usage:
    # All categories, default 20 per bucket
    python -m eval.score_view

    # FHIR queries only
    python -m eval.score_view --category fhir_query

    # Specific prompt version
    python -m eval.score_view --prompt-id v2_system_prompt

    # JSON output for dashboarding
    python -m eval.score_view --json > scores.json
"""

import argparse
import json

from eval.redis_store import get_best_calls, get_worst_calls


def print_table(calls: list[dict], label: str) -> None:
    """Print a formatted table of calls."""
    sep = "─" * 80
    print(f"\n{sep}")
    print(f"  {label} ({len(calls)} calls)")
    print(sep)
    print(f"  {'Score':>6}  {'Category':14}  {'Model':16}  "
          f"{'Question (truncated)'}")
    print(sep)
    for c in calls:
        score = float(c.get("composite_score", 0))
        cat = c.get("request_category", "general")[:14]
        model = c.get("model", "")[:16]
        q = c.get("question", "")[:55]
        print(f"  {score:>6.3f}  {cat:14}  {model:16}  {q}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Ragas score view")
    parser.add_argument(
        "--category", help="Filter by request_category "
        "(fhir_query | hl7_transform | code_qa | general)",
    )
    parser.add_argument("--prompt-id", help="Filter by prompt_id")
    parser.add_argument(
        "--n", type=int, default=20,
        help="Number of calls per bucket (default: 20)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON instead of table",
    )
    args = parser.parse_args()

    worst = get_worst_calls(args.n, args.category, args.prompt_id)
    best = get_best_calls(args.n, args.category, args.prompt_id)

    if args.json:
        print(json.dumps({"worst": worst, "best": best}, indent=2))
    else:
        print_table(worst, "⚠  WORST SCORING CALLS")
        print_table(best, "✓  BEST SCORING CALLS")


if __name__ == "__main__":
    main()
