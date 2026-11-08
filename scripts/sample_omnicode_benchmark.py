"""Sample OmniCode/SWE-style benchmark instances with reproducible stratification.

The default sampler uses a deliberately simple complexity score, splits the
dataset into easy/medium/hard buckets, then samples within each bucket while
limiting dominance from large repositories.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SIMPLE_WEIGHTS = {
    "patch_churn": 0.25,
    "test_churn": 0.25,
    "patch_files": 0.25,
    "problem_chars": 0.25,
}

FULL_WEIGHTS = {
    "patch_files": 0.12,
    "patch_hunks": 0.14,
    "patch_churn": 0.18,
    "test_files": 0.07,
    "test_hunks": 0.08,
    "test_churn": 0.10,
    "problem_chars": 0.07,
    "fail_tests": 0.08,
    "pass_tests": 0.06,
    "bad_patches": 0.05,
    "style_reviews": 0.05,
}

ISSUE_ONLY_WEIGHTS = {
    "problem_chars": 0.30,
    "fail_tests": 0.25,
    "pass_tests": 0.15,
    "bad_patches": 0.20,
    "style_reviews": 0.10,
}

STRATEGY_RATIOS = {
    "balanced": {"easy": 1 / 3, "medium": 1 / 3, "hard": 1 / 3},
    "stress": {"easy": 0.20, "medium": 0.35, "hard": 0.45},
    "smoke": {"easy": 0.50, "medium": 0.35, "hard": 0.15},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print reproducibly sampled benchmark instance IDs."
    )
    parser.add_argument("--input", required=True, type=Path, help="Input JSON dataset.")
    parser.add_argument("--n", required=True, type=int, help="Number of instances to sample.")
    parser.add_argument("--seed", default=20260609, type=int, help="Random seed.")
    parser.add_argument(
        "--strategy",
        choices=sorted(STRATEGY_RATIOS),
        default="balanced",
        help="Complexity bucket ratio.",
    )
    parser.add_argument(
        "--score-profile",
        choices=["simple", "full", "issue-only"],
        default="simple",
        help=(
            "simple uses patch churn, test churn, touched files, and problem length; "
            "full uses more metadata; issue-only avoids gold patch size features "
            "and is safer when sampling should not inspect solutions."
        ),
    )
    parser.add_argument(
        "--max-repo-share",
        default=0.40,
        type=float,
        help="Soft upper bound for any one repository in the final sample.",
    )
    return parser.parse_args()


def diff_stats(diff_text: str) -> dict[str, int]:
    if not isinstance(diff_text, str):
        diff_text = ""
    lines = diff_text.splitlines()
    return {
        "files": len(re.findall(r"^diff --git ", diff_text, flags=re.MULTILINE)),
        "hunks": len(re.findall(r"^@@ ", diff_text, flags=re.MULTILINE)),
        "adds": sum(1 for line in lines if line.startswith("+") and not line.startswith("+++")),
        "dels": sum(1 for line in lines if line.startswith("-") and not line.startswith("---")),
        "lines": len(lines),
    }


def enrich_instances(instances: list[dict[str, Any]], score_profile: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(instances):
        patch = diff_stats(item.get("patch", ""))
        test_patch = diff_stats(item.get("test_patch", ""))
        rows.append(
            {
                "idx": idx,
                "repo": item["repo"],
                "instance_id": item["instance_id"],
                "patch_files": patch["files"],
                "patch_hunks": patch["hunks"],
                "patch_churn": patch["adds"] + patch["dels"],
                "patch_lines": patch["lines"],
                "test_files": test_patch["files"],
                "test_hunks": test_patch["hunks"],
                "test_churn": test_patch["adds"] + test_patch["dels"],
                "test_lines": test_patch["lines"],
                "problem_chars": len(item.get("problem_statement", "")),
                "fail_tests": len(item.get("FAIL_TO_PASS") or []),
                "pass_tests": len(item.get("PASS_TO_PASS") or []),
                "bad_patches": len(item.get("bad_patches") or []),
                "style_reviews": len(item.get("style_review") or []),
            }
        )

    weights_by_profile = {
        "simple": SIMPLE_WEIGHTS,
        "full": FULL_WEIGHTS,
        "issue-only": ISSUE_ONLY_WEIGHTS,
    }
    weights = weights_by_profile[score_profile]
    ranks = percentile_ranks(rows, list(weights))
    for i, row in enumerate(rows):
        row["complexity_score"] = sum(weights[name] * ranks[name][i] for name in weights)

    sorted_rows = sorted(rows, key=lambda row: (row["complexity_score"], row["instance_id"]))
    n = len(sorted_rows)
    for rank, row in enumerate(sorted_rows):
        if rank < n / 3:
            row["complexity_bucket"] = "easy"
        elif rank < 2 * n / 3:
            row["complexity_bucket"] = "medium"
        else:
            row["complexity_bucket"] = "hard"
    return rows


def percentile_ranks(rows: list[dict[str, Any]], features: list[str]) -> dict[str, list[float]]:
    ranks: dict[str, list[float]] = {}
    denominator = max(1, len(rows) - 1)
    for feature in features:
        values = sorted((math.log1p(row[feature]), i) for i, row in enumerate(rows))
        feature_ranks = [0.0] * len(rows)
        start = 0
        while start < len(values):
            end = start + 1
            while end < len(values) and values[end][0] == values[start][0]:
                end += 1

            average_rank = (start + end - 1) / 2
            percentile = average_rank / denominator
            for _, i in values[start:end]:
                feature_ranks[i] = percentile

            start = end
        ranks[feature] = feature_ranks
    return ranks


def bucket_targets(total: int, ratios: dict[str, float]) -> dict[str, int]:
    raw = {bucket: total * ratio for bucket, ratio in ratios.items()}
    targets = {bucket: int(math.floor(value)) for bucket, value in raw.items()}
    remainder = total - sum(targets.values())
    order = sorted(raw, key=lambda bucket: raw[bucket] - targets[bucket], reverse=True)
    for bucket in order[:remainder]:
        targets[bucket] += 1
    return targets


def sample_rows(
    rows: list[dict[str, Any]],
    total: int,
    strategy: str,
    seed: int,
    max_repo_share: float,
) -> list[dict[str, Any]]:
    if total <= 0:
        raise ValueError("--n must be positive")
    if total > len(rows):
        raise ValueError(f"--n={total} exceeds dataset size {len(rows)}")

    rng = random.Random(seed)
    repo_cap = max(1, math.ceil(total * max_repo_share))
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    repo_counts: Counter[str] = Counter()
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_bucket[row["complexity_bucket"]].append(row)

    for bucket, target in bucket_targets(total, STRATEGY_RATIOS[strategy]).items():
        chosen = pick_from_pool(
            by_bucket[bucket],
            target,
            rng,
            repo_counts,
            repo_cap,
            selected_ids,
        )
        selected.extend(chosen)

    if len(selected) < total:
        remaining = [row for row in rows if row["instance_id"] not in selected_ids]
        selected.extend(
            pick_from_pool(
                remaining,
                total - len(selected),
                rng,
                repo_counts,
                repo_cap,
                selected_ids,
                relax_repo_cap=True,
            )
        )

    bucket_order = {"easy": 0, "medium": 1, "hard": 2}
    selected.sort(
        key=lambda row: (
            bucket_order[row["complexity_bucket"]],
            row["repo"],
            row["instance_id"],
        )
    )
    return selected


def pick_from_pool(
    pool: list[dict[str, Any]],
    target: int,
    rng: random.Random,
    repo_counts: Counter[str],
    repo_cap: int,
    selected_ids: set[str],
    relax_repo_cap: bool = False,
) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    candidates = list(pool)
    rng.shuffle(candidates)

    while candidates and len(chosen) < target:
        available = [
            row
            for row in candidates
            if row["instance_id"] not in selected_ids
            and (relax_repo_cap or repo_counts[row["repo"]] < repo_cap)
        ]
        if not available and not relax_repo_cap:
            available = [row for row in candidates if row["instance_id"] not in selected_ids]
        if not available:
            break

        current_repo_totals = Counter(row["repo"] for row in available)
        weights = [
            1.0 / ((repo_counts[row["repo"]] + 1) * math.sqrt(current_repo_totals[row["repo"]]))
            for row in available
        ]
        row = rng.choices(available, weights=weights, k=1)[0]
        chosen.append(row)
        selected_ids.add(row["instance_id"])
        repo_counts[row["repo"]] += 1
        candidates = [candidate for candidate in candidates if candidate["instance_id"] != row["instance_id"]]

    return chosen


def main() -> None:
    args = parse_args()
    instances = json.loads(args.input.read_text(encoding="utf-8"))
    rows = enrich_instances(instances, args.score_profile)
    selected = sample_rows(rows, args.n, args.strategy, args.seed, args.max_repo_share)
    for row in selected:
        print(json.dumps(row["instance_id"]))


if __name__ == "__main__":
    main()
