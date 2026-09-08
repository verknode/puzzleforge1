"""Retune an existing local profile without reallocating its keyspace."""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

from .benchmark import BenchmarkReport, save_report
from .local import load_profile, save_profile


def apply_retune(profile_path: Path, report: BenchmarkReport, report_path: Path) -> dict:
    profile = load_profile(profile_path)
    save_report(report_path, report)
    baseline = next((item for item in report.results if item.tuning == profile.tuning), None)
    def stable(item):
        return (item is not None and item.successful_runs == report.repeats
                and item.failed_runs == 0 and math.isfinite(item.median_keys_per_second)
                and item.median_keys_per_second > 0
                and item.relative_spread <= report.maximum_relative_spread)
    if not stable(baseline) or report.repeats < 2:
        return {"applied": False, "changed_tuning": False,
                "reason": "No stable repeated baseline; saved report only."}
    best = report.best
    improve = (stable(best) and best.tuning != baseline.tuning
               and best.median_keys_per_second >= baseline.median_keys_per_second * 1.05
               and best.minimum_keys_per_second > baseline.maximum_keys_per_second)
    chosen = best if improve else baseline
    backup = report_path.with_suffix(".profile-backup.json")
    if backup.exists():
        raise FileExistsError(f"Retune backup already exists: {backup}")
    save_profile(backup, profile)
    save_profile(profile_path, replace(
        profile, tuning=chosen.tuning,
        measured_rate_keys_per_second=chosen.median_keys_per_second,
        benchmark_relative_spread=chosen.relative_spread,
        benchmark_report=str(report_path.resolve()),
    ))
    return {"applied": True, "changed_tuning": improve,
            "baseline_rate": baseline.median_keys_per_second,
            "selected_rate": chosen.median_keys_per_second,
            "reason": "Stable improvement measured." if improve else "Kept current tuning; no clear improvement.",
            "backup": str(backup)}
