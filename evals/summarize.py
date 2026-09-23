"""Distribution of what the eval questions require.

    uv run python evals/summarize.py

Answers the question the set was built for: how much of the work is plain
retrieval, and how much is arithmetic a QueryPlan does not describe.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

QUESTIONS = Path(__file__).with_name("questions.yaml")

#: Anything beyond fetching values for coordinates the plan already carries.
BEYOND_RETRIEVAL = {
    "aggregation",
    "ranking",
    "growth",
    "ratio",
    "cross_company_total",
    "multi_step",
}


def _bar(count: int, total: int, width: int = 28) -> str:
    filled = 0 if not total else round(width * count / total)
    return "#" * filled + "." * (width - filled)


def main() -> None:
    document = yaml.safe_load(QUESTIONS.read_text("utf-8"))
    questions = document["questions"]
    total = len(questions)

    expect = Counter(value for q in questions for value in q.get("expect", []))
    untriaged = [q for q in questions if not q.get("expect")]
    needs = Counter(tag for q in questions for tag in q.get("needs", []))
    blocked = Counter(tag for q in questions for tag in q.get("blocked_by", []))
    exercises = Counter(tag for q in questions for tag in q.get("exercises", []))

    answerable = [q for q in questions if "answered" in q.get("expect", [])]
    plain = [q for q in answerable if not (set(q.get("needs", [])) & BEYOND_RETRIEVAL)]

    print(f"{total} questions\n")

    print("expect (one entry per thing asked for)")
    for name, count in expect.most_common():
        print(f"  {name:<22}{count:>3}  {_bar(count, total)}")

    print("\nneeds (questions expecting at least one answer)")
    for name, count in needs.most_common():
        print(f"  {name:<22}{count:>3}  {_bar(count, len(answerable))}")

    if blocked:
        print("\nblocked_by")
        for name, count in blocked.most_common():
            print(f"  {name:<22}{count:>3}")

    if exercises:
        print("\nexercises")
        for name, count in exercises.most_common():
            print(f"  {name:<22}{count:>3}")

    if untriaged:
        print(f"\nuntriaged ({len(untriaged)}) -- excluded from the tally below")
        for q in untriaged:
            print(f"  {q['id']}  {q['question'][:66]}")

    print("\n--- the decision this set exists to inform ---")
    print(f"  answerable or partial      {len(answerable):>3}")
    print(f"  ... of those, retrieval only {len(plain):>3}")
    print(f"  ... needing more than that   {len(answerable) - len(plain):>3}")
    if answerable:
        share = 100 * (len(answerable) - len(plain)) / len(answerable)
        print(
            f"\n  {share:.0f}% of answerable questions need arithmetic the plan does not describe."
        )


if __name__ == "__main__":
    main()
