"""Explicit development runner for provider-assisted content-line extraction."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence

from .context_reconstruction import (
    load_jsonl_messages,
    reconstruct_context,
    select_one_direct_and_group,
    write_review_artifacts,
)
from .contextual_bundle_pipeline import AIProviderConfig
from .semantic_content_lines import enrich_review_content_lines
from .staged_deepseek_analyzer import OpenAICompatibleStageModel


EVALUATION_VERSION = "context_reconstruction_semantic_evaluation_v7"


def build_semantic_review(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    settings_path: str | Path = Path("data/workbench_settings.json"),
    reference_date: Optional[str] = None,
    max_cards: int | None = None,
    allow_provider_data_transfer: bool = False,
) -> dict[str, Path]:
    if not allow_provider_data_transfer:
        raise ValueError("provider_data_transfer_requires_explicit_opt_in")
    rows = load_jsonl_messages(input_path, require_development=True)
    selected = select_one_direct_and_group(rows)
    base = reconstruct_context(selected, reference_date=reference_date, source_scope="development")
    config = AIProviderConfig.from_workbench_settings_path(settings_path)
    if not config.configured:
        raise ValueError("semantic_provider_unconfigured")
    model = OpenAICompatibleStageModel(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        response_format_json=True,
    )
    enriched = enrich_review_content_lines(base, selected, model, max_cards=max_cards, max_output_tokens=4000)
    enriched["evaluation_version"] = EVALUATION_VERSION
    enriched.setdefault("review", {})["evaluation_version"] = EVALUATION_VERSION
    return write_review_artifacts(enriched, output_dir, source_messages=selected, source_name="development")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build an explicitly authorized semantic content-line review")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--settings", default="data/workbench_settings.json")
    parser.add_argument("--reference-date", default=None)
    parser.add_argument("--max-cards", type=int, default=None, help="optional diagnostic cap; omitted means all review cards")
    parser.add_argument("--allow-provider-data-transfer", action="store_true")
    args = parser.parse_args(argv)
    paths = build_semantic_review(
        args.input,
        args.output,
        settings_path=args.settings,
        reference_date=args.reference_date,
        max_cards=args.max_cards,
        allow_provider_data_transfer=args.allow_provider_data_transfer,
    )
    for key, path in paths.items():
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
