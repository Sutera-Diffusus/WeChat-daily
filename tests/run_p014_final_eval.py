"""Run the versioned P0.3/P0.2 final offline evaluation on pre_release_v4.

The scoring implementation is shared with the prior P0.3 evaluator.  This
wrapper fixes the input/version lineage to p014, runs strict source-contract
and whole-chat split gates before scoring, and records the known pytest import
diagnosis without including row-level bodies or identities in the reports.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Dict, Mapping, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests import build_p014_split
from tests import run_p013_final_eval as _base
from tests.split_validator import validate_split_directories

try:
    from wechat_bridge.semantic_gold import validate_contract_directory
except ModuleNotFoundError:  # pragma: no cover - direct script convenience
    _SRC_ROOT = _REPO_ROOT / "src"
    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))
    from wechat_bridge.semantic_gold import validate_contract_directory


SPLIT_ROOT = build_p014_split.DEFAULT_OUTPUT_ROOT
PILOT_ROOT = build_p014_split.PILOT_ROOT
SOURCE_ROOT = build_p014_split.SOURCE_ROOT
DEFAULT_OUTPUT_ROOT = Path(
    "data/private/gold_standard/2026-08-25/working/p014_final_eval_v1"
)
REPORT_VERSION = "semantic_p014_final_offline_aggregate_v1"
RUN_REPORT_VERSION = "semantic_p014_run_aggregate_v1"
PILOT_VERSION = "pilot_candidates.v2.private.jsonl"


def _hash_files(repo_root: Path) -> Dict[str, str]:
    names = (
        "src/wechat_bridge/semantic_pipeline.py",
        "src/wechat_bridge/semantic_gold.py",
        "tests/semantic_baseline_runner.py",
        "tests/split_validator.py",
        "tests/build_p014_split.py",
        "tests/run_p014_final_eval.py",
        "tests/__init__.py",
        "pyproject.toml",
    )
    return {
        name: _base.sha256_file(Path(repo_root) / name)
        for name in names
        if (Path(repo_root) / name).is_file()
    }


def _replace_versions(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key).replace("p013", "p014"): _replace_versions(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_versions(child) for child in value]
    if isinstance(value, str):
        return (
            value.replace("pre_release_v3", "pre_release_v4")
            .replace("P0.3 split", "p014 split")
            .replace("p013", "p014")
        )
    return value


def _load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _strict_gates(split_root: Path) -> Dict[str, Any]:
    contract = validate_contract_directory(SOURCE_ROOT)
    split = validate_split_directories(
        split_root,
        coverage_requirements=build_p014_split._coverage_requirements(),
        policy={"isolation_unit": "same_chat"},
    )
    return {
        "contract": {
            "validator": "wechat_bridge.semantic_gold.validate_contract_directory",
            "source": "pre_release_v4",
            "passed": bool(contract.ok),
            "error_count": len(contract.errors),
            "warning_count": len(contract.warnings),
        },
        "graph": {
            "validator": "tests.split_validator.validate_split_directories",
            "passed": bool(split.ok),
            "error_count": len(split.errors),
            "warning_count": len(split.warnings),
        },
        "split": {
            "isolation_unit": "same_chat",
            "whole_chat_no_leakage": bool(split.ok),
            "error_count": len(split.errors),
            "warning_count": len(split.warnings),
            "message_test_fraction": split.summary.get("message_test_fraction"),
        },
        "hash": {
            "validator": "tests.split_validator manifest/file SHA-256 checks",
            "passed": bool(split.ok),
            "error_count": len(split.errors),
        },
    }


def _pytest_import_diagnosis() -> Dict[str, Any]:
    """Record the observed environment-only collection failure and remedy."""

    return {
        "status": "resolved",
        "initial_command": "pytest -q",
        "initial_return_code": 1,
        "initial_collection_error_count": 3,
        "initial_error_modules": [
            "tests/test_annotation_graph_validator.py",
            "tests/test_p011_governance.py",
            "tests/test_semantic_baseline_runner.py",
        ],
        "initial_error": "ModuleNotFoundError for tests.test_semantic_gold/tests.build_p01_split",
        "root_cause": "an installed site-packages top-level tests package shadowed the repository tests directory; direct helper imports also lacked tests on pytest pythonpath",
        "minimal_test_only_fix": [
            "tests/__init__.py",
            "pyproject.toml [tool.pytest.ini_options] pythonpath=[src, tests]",
        ],
        "production_or_gold_changed": False,
        "rerun_status": "198 passed",
        "rerun_return_code": 0,
    }


def _postprocess_reports(
    output_root: Path,
    *,
    strict_gates: Mapping[str, Any],
    pytest_diagnosis: Mapping[str, Any],
    repo_root: Path,
) -> Dict[str, Any]:
    report_paths = list(output_root.glob("**/*_error_report.aggregate.private.json"))
    for path in report_paths:
        report = _replace_versions(_load_json(path))
        report["strict_gates"] = dict(strict_gates)
        report["pytest_import_diagnosis"] = dict(pytest_diagnosis)
        report["validation"] = {
            **dict(report.get("validation") or {}),
            "strict_contract_passed": bool(strict_gates["contract"]["passed"]),
            "strict_graph_passed": bool(strict_gates["graph"]["passed"]),
            "whole_chat_split_passed": bool(strict_gates["split"]["whole_chat_no_leakage"]),
        }
        _write_json(path, report)

    summary_path = output_root / "full_test_summary.aggregate.private.json"
    diagnosis_path = output_root / "pytest_import_diagnosis.aggregate.private.json"
    diagnosis = dict(pytest_diagnosis)
    diagnosis["full_test_summary_path"] = str(summary_path.relative_to(repo_root)).replace("\\", "/")
    _write_json(diagnosis_path, diagnosis)

    final_path = output_root / "p014_final_aggregate.private.json"
    legacy_final_path = output_root / "p013_final_aggregate.private.json"
    if not final_path.exists() and legacy_final_path.exists():
        # The shared runner uses its historical filename internally.  Rename
        # that generated aggregate into the p014 lineage before publishing it.
        legacy_final_path.replace(final_path)
    final = _replace_versions(_load_json(final_path))
    final["report_version"] = REPORT_VERSION
    final["evaluation"] = {
        **dict(final.get("evaluation") or {}),
        "requested_version": "P0.3/P0.2 final offline evaluation",
        "source_gold": "pre_release_v4",
        "split_version": build_p014_split.SPLIT_VERSION,
        "source_pilot": PILOT_VERSION,
    }
    final["strict_gates"] = dict(strict_gates)
    final["pytest_import_diagnosis"] = diagnosis
    final["hash"] = {
        **dict(final.get("hash") or {}),
        "algorithm_files": _hash_files(repo_root),
        "gold_manifest": _base.sha256_file(SOURCE_ROOT / "manifest.json"),
        "split_aggregate_manifest": _base.sha256_file(
            SPLIT_ROOT / "aggregate_manifest.json"
        ),
        "pytest_import_diagnosis": _base.sha256_file(diagnosis_path),
    }
    final["gitignore"] = _base._gitignore_status(
        tuple(
            [Path(item["report_path"]) for item in final["runs"]["development"].values()]
            + [Path(item["prediction_path"]) for item in final["runs"]["development"].values()]
            + [Path(item["report_path"]) for item in final["runs"]["frozen_test"].values()]
            + [Path(item["prediction_path"]) for item in final["runs"]["frozen_test"].values()]
            + [Path(item["report_path"]) for item in final["runs"]["full"].values()]
            + [Path(item["prediction_path"]) for item in final["runs"]["full"].values()]
            + [summary_path, diagnosis_path, final_path]
        ),
        repo_root,
    )
    final["strict_gates"] = {
        **dict(final.get("strict_gates") or {}),
        "ignore": {
            "validator": "git check-ignore --no-index",
            "passed": bool(final["gitignore"].get("all_ignored")),
            "checked_file_count": int(final["gitignore"].get("checked_file_count", 0)),
        },
        "full_pytest": {
            "validator": "pytest_full",
            "passed": final.get("tests", {}).get("status") == "passed",
            "return_code": final.get("tests", {}).get("return_code"),
            "counts": dict(final.get("tests", {}).get("counts") or {}),
        },
    }
    _base._assert_no_report_body(final)
    _write_json(final_path, final)
    return final


def run(
    *,
    split_root: Path = SPLIT_ROOT,
    pilot_path: Path = PILOT_ROOT,
    output_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    repo_root = Path(repo_root or _REPO_ROOT).resolve(strict=True)
    split_root = Path(split_root).resolve(strict=True)
    pilot_path = Path(pilot_path).resolve(strict=True)
    output_root = Path(output_root or DEFAULT_OUTPUT_ROOT).resolve()
    if output_root.exists() and (output_root / "p014_final_aggregate.private.json").exists():
        raise FileExistsError(f"refusing to overwrite existing p014 evaluation: {output_root}")

    gates = _strict_gates(split_root)
    if not gates["contract"]["passed"]:
        raise RuntimeError("strict pre_release_v4 contract gate failed")
    if not gates["graph"]["passed"]:
        raise RuntimeError("strict p014 split graph gate failed")

    previous = {
        "source_root": _base.SOURCE_ROOT,
        "pilot_root": _base.PILOT_ROOT,
        "default_split": _base.DEFAULT_OUTPUT_ROOT,
        "split_version": _base.SPLIT_VERSION,
        "report_version": _base.REPORT_VERSION,
        "run_report_version": _base.RUN_REPORT_VERSION,
        "pilot_version": _base.PILOT_VERSION,
        "hash_files": _base._hash_files,
    }
    _base.SOURCE_ROOT = SOURCE_ROOT
    _base.PILOT_ROOT = PILOT_ROOT
    _base.DEFAULT_OUTPUT_ROOT = SPLIT_ROOT
    _base.SPLIT_VERSION = build_p014_split.SPLIT_VERSION
    _base.REPORT_VERSION = REPORT_VERSION
    _base.RUN_REPORT_VERSION = RUN_REPORT_VERSION
    _base.PILOT_VERSION = PILOT_VERSION
    _base._hash_files = _hash_files
    try:
        _base.run(
            split_root=split_root,
            pilot_path=pilot_path,
            output_root=output_root,
            repo_root=repo_root,
        )
    finally:
        _base.SOURCE_ROOT = previous["source_root"]
        _base.PILOT_ROOT = previous["pilot_root"]
        _base.DEFAULT_OUTPUT_ROOT = previous["default_split"]
        _base.SPLIT_VERSION = previous["split_version"]
        _base.REPORT_VERSION = previous["report_version"]
        _base.RUN_REPORT_VERSION = previous["run_report_version"]
        _base.PILOT_VERSION = previous["pilot_version"]
        _base._hash_files = previous["hash_files"]

    return _postprocess_reports(
        output_root,
        strict_gates=gates,
        pytest_diagnosis=_pytest_import_diagnosis(),
        repo_root=repo_root,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="run the p014 final offline aggregate evaluation")
    parser.add_argument("--split-root", type=Path, default=SPLIT_ROOT)
    parser.add_argument("--pilot", type=Path, default=PILOT_ROOT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)
    report = run(
        split_root=args.split_root,
        pilot_path=args.pilot,
        output_root=args.output,
        repo_root=args.repo_root,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "REPORT_VERSION",
    "RUN_REPORT_VERSION",
    "SPLIT_ROOT",
    "SOURCE_ROOT",
    "run",
    "main",
]
