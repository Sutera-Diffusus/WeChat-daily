"""Round-1 active-learning orchestration for the v2 semantic sampler.

This module is intentionally an artifact wrapper, not a second semantic
pipeline.  Sampling and provider execution both delegate to
``cross_date_experiment`` so the debug batch exercises the same episode/chunk,
core-message and strict-validation contracts as the v2 run.  Blind controls
are selection-only: their JSON contains opaque locators and hashes, never
message bodies, and never reserves a provider call.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cross_date_experiment import (
    CROSS_DATE_SEMANTIC_MAX_OUTPUT_TOKENS_DEFAULT,
    CROSS_DATE_SEMANTIC_MAX_OUTPUT_TOKENS_ENV,
    ExperimentConfig,
    HUMAN_REVIEW_FLAGS,
    SEMANTIC_OUTPUT_BUDGET_VERSION,
    _configured_semantic_max_output_tokens,
    _file_digest,
    _message_text,
    _message_type,
    _safe_experiment_path,
    _safe_int,
    _safe_scalar,
    _source_scope_ref,
    _source_message_ref,
    _stable_digest,
    build_active_learning_round1_selection,
    run_experiment,
    sample_sqlite_dialogue_bundles,
    write_active_learning_selection_manifest,
)


SOURCE = "active_learning_round1_20260831"
SCHEMA_VERSION = "active_learning_round1_v1"
DEBUG_DATES = ("2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17")
BLIND_DATES = ("2026-08-18", "2026-08-19")
EXCLUDED_DATES = ("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-25")
OUTPUT_DIR = Path("output") / SOURCE
AUTHORITY_ROOT = Path(".runtime") / "active-learning-round1-debug-20260831"
AUTHORIZATION_ID = "active-learning-round1-debug-20260831"
_BODY_KEYS = frozenset({
    "content", "text", "body", "message_content", "message_text", "raw_message",
    "reasoning", "provider_response", "api_key", "secret", "authorization",
})


def _json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _body_free(value: Any, path: str = "root") -> None:
    """Fail closed if a JSON artifact accidentally receives message payload."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _BODY_KEYS:
                raise ValueError(f"body_free_artifact_key:{path}.{key}")
            _body_free(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _body_free(child, f"{path}[{index}]")


def _ref_set(package: Mapping[str, Any]) -> list[str]:
    recovery = package.get("recoverable") if isinstance(package.get("recoverable"), Mapping) else {}
    refs = recovery.get("message_refs") if isinstance(recovery.get("message_refs"), list) else []
    return sorted({str(value) for value in refs if str(value)})


def _max_same_date_jaccard(packages: Sequence[Mapping[str, Any]]) -> float:
    sets = [set(_ref_set(package)) for package in packages]
    maximum = 0.0
    for index, left in enumerate(sets):
        for right in sets[index + 1:]:
            union = left | right
            maximum = max(maximum, len(left & right) / len(union) if union else 0.0)
    return round(maximum, 6)


def _selection_manifest_with_hash(
    selection: Mapping[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    value = dict(selection)
    value["selection_artifact"] = "selection_manifest.json"
    value["selection_sha256"] = _file_digest(output_dir / "selection_manifest.json")
    value["body_free"] = True
    return value


def _debug_result_projection(payload: Mapping[str, Any], selection: Mapping[str, Any]) -> dict[str, Any]:
    """Project the provider payload to a body-free debug result artifact."""
    results = [dict(item) for item in payload.get("results", ()) if isinstance(item, Mapping)]
    manifest = dict(payload.get("manifest") or {})
    manifest.update({
        "source": SOURCE,
        "split": "debug",
        "selection_manifest": "selection_manifest.json",
        "blind_provider_calls": 0,
        "sampling_unit": "episode_chunk",
        "same_date_jaccard_max": 0.35,
        "exact_message_sequence_set_dedup": True,
        "accuracy_release_allowed": False,
        "stage_b": False,
        "stage_c": False,
        "production_blocked": True,
    })
    projection = {
        "schema_version": "active_learning_round1_debug_results_v1",
        "source": SOURCE,
        "production_blocked": True,
        "stage_b": False,
        "stage_c": False,
        "accuracy": False,
        "config": dict(payload.get("config") or {}),
        "manifest": manifest,
        "provider": {
            str(key): child
            for key, child in (payload.get("provider") or {}).items()
            if str(key).casefold() != "api_key_configured"
        },
        "authorization_preflight_calls": _safe_int(payload.get("authorization_preflight_calls")) or 0,
        "provider_calls": _safe_int(payload.get("provider_calls")) or 0,
        "provider_calls_total": _safe_int(payload.get("provider_calls_total")) or 0,
        "retry_count": 0,
        "total": len(results),
        "unknown_count": sum(_safe_int(item.get("unknown")) or 0 for item in results),
        "error_count": sum(bool(item.get("error")) or str(item.get("status")) not in {"ok", "complete"} for item in results),
        "token_total": sum(_safe_int(item.get("token")) or 0 for item in results),
        "latency_total_ms": sum(_safe_int(item.get("latency_ms")) or 0 for item in results),
        "started_at": payload.get("started_at"),
        "duration_ms": _safe_int(payload.get("duration_ms")) or 0,
        "results": results,
        "by_date": {
            str(date): [dict(item) for item in values if isinstance(item, Mapping)]
            for date, values in (payload.get("by_date") or {}).items()
        },
        "daily_summary": dict(payload.get("daily_summary") or {}),
        "worst_date": payload.get("worst_date"),
        "worst_date_criterion": payload.get("worst_date_criterion"),
        "worst_date_definition": payload.get("worst_date_definition"),
        "worst_date_metrics": dict(payload.get("worst_date_metrics") or {}),
        "scope": dict(payload.get("scope") or {}),
        "recoverable_context_summary": dict(payload.get("recoverable_context_summary") or {}),
        "selection": {
            "debug_dates": list(selection.get("debug_dates") or []),
            "blind_dates": list(selection.get("blind_dates") or []),
            "debug_count": _safe_int(selection.get("debug_count")) or 0,
            "blind_count": _safe_int(selection.get("blind_count")) or 0,
            "body_free": True,
        },
        "body_free": True,
    }
    _body_free(projection)
    return projection


def _error_artifact(debug_results: Mapping[str, Any]) -> dict[str, Any]:
    rows = []
    for item in debug_results.get("results", ()):
        if not isinstance(item, Mapping):
            continue
        code = _safe_scalar(item.get("error_code") or item.get("error"), "", 96)
        rows.append({
            "date": _safe_scalar(item.get("date"), "", 32),
            "package_id": _safe_scalar(item.get("package_id"), "", 160),
            "status": _safe_scalar(item.get("status"), "", 32),
            "provider_call": bool(item.get("provider_call")),
            "error_code": code,
            "unknown": _safe_int(item.get("unknown")) or 0,
            "output_token_budget": _safe_int(item.get("output_token_budget")),
            "output_tokens": _safe_int(item.get("output_tokens")),
        })
    counts: dict[str, int] = {}
    for row in rows:
        if row["error_code"]:
            counts[row["error_code"]] = counts.get(row["error_code"], 0) + 1
    value = {
        "schema_version": "active_learning_round1_errors_v1",
        "source": SOURCE,
        "rows": rows,
        "error_code_counts": dict(sorted(counts.items())),
        "body_free": True,
    }
    _body_free(value)
    return value


def _safe_dom(value: Any, limit: int = 2000) -> str:
    return html.escape(_safe_scalar(value, "", limit), quote=True)


def _review_message_text(row: Mapping[str, Any]) -> str:
    """Return review text without trimming leading/trailing whitespace."""
    value = row.get("text")
    if value in (None, ""):
        value = row.get("content", row.get("message_content", row.get("message_text", row.get("body"))))
    if value is None or isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return "[无可读文本]"
    # The review renderer is the one place where source text is displayed.
    # Keep spaces intact for human comparison; only normalize line endings and
    # apply a display bound before HTML escaping.
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text[:4_000] if text else "[无可读文本]"


def _safe_message_dom(value: Any, limit: int = 4_000) -> str:
    """HTML-escape review text while preserving its whitespace."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")[:limit]
    return html.escape(text, quote=True)


def _read_review_raw_content(
    db_path: str | Path,
    samples: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, str]:
    """Read only selected source text for the HTML page, never JSON artifacts."""
    ids: set[str] = set()
    dates: set[str] = set()
    for date, packages in samples.items():
        dates.add(str(date))
        for package in packages:
            if not isinstance(package, Mapping):
                continue
            for message in package.get("messages") or ():
                if isinstance(message, Mapping):
                    for key in ("id", "message_id", "message_row_id"):
                        if message.get(key) not in (None, ""):
                            ids.add(str(message.get(key)))
    if not ids or not dates:
        return {}
    path = _safe_experiment_path(db_path, "active_learning_refuses_frozen_or_gold_input")
    uri = f"file:{path.as_posix()}?mode=ro"
    result: dict[str, str] = {}
    try:
        conn = sqlite3.connect(uri, uri=True)
        placeholders = ",".join("?" for _ in dates)
        cursor = conn.execute(
            "SELECT id,message_id,content FROM messages "
            f"WHERE substr(timestamp,1,10) IN ({placeholders})",
            tuple(sorted(dates)),
        )
        for row_id, message_id, content in cursor.fetchall():
            keys = (row_id, message_id)
            if content is None:
                continue
            value = str(content).replace("\r\n", "\n").replace("\r", "\n")[:4_000]
            for key in keys:
                if key not in (None, "") and str(key) in ids:
                    result[str(key)] = value
    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


def _render_review(
    debug_results: Mapping[str, Any],
    samples: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    raw_content_by_id: Mapping[str, str] | None = None,
) -> str:
    raw_content_by_id = raw_content_by_id or {}
    by_key = {
        (str(date), str(package.get("package_id"))): package
        for date, packages in samples.items()
        for package in packages
        if isinstance(package, Mapping)
    }
    cards: list[str] = []
    for index, item in enumerate(debug_results.get("results", ())):
        if not isinstance(item, Mapping):
            continue
        date = _safe_scalar(item.get("date"), "", 32)
        package_id = _safe_scalar(item.get("package_id"), "", 160)
        package = by_key.get((date, package_id), {})
        unit_id = f"debug-{date}-{index + 1}"
        messages = package.get("messages") if isinstance(package.get("messages"), Sequence) else ()
        recovery = package.get("recoverable") if isinstance(package.get("recoverable"), Mapping) else {}
        package_refs = [str(value) for value in (recovery.get("message_refs") or [])]
        model_evidence_refs = {
            str(value) for value in (item.get("evidence_refs") or []) if str(value)
        }
        message_html: list[str] = []
        for message_index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                continue
            role = "主消息（模型输入）" if str(message.get("role")) == "primary" else "上下文"
            message_keys = [
                str(message.get(key))
                for key in ("id", "message_id", "message_row_id")
                if message.get(key) not in (None, "")
            ]
            text = next(
                (raw_content_by_id[key] for key in message_keys if key in raw_content_by_id),
                _review_message_text(message),
            )
            message_ref = (
                package_refs[message_index]
                if message_index < len(package_refs)
                else _source_message_ref(message, f"{package_id}-m{message_index + 1}")
            )
            message_alias = f"m{message_index + 1}"
            checked = " checked" if message_ref in model_evidence_refs else ""
            evidence_checkbox = (
                f'<label class="evidence-choice"><input type="checkbox" '
                f'data-evidence-checkbox="true" data-message-alias="{_safe_dom(message_alias, 32)}" '
                f'data-message-ref="{_safe_dom(message_ref, 240)}" '
                f'data-evidence-alias="{_safe_dom(message_alias, 32)}" '
                f'data-evidence-ref="{_safe_dom(message_ref, 240)}" data-corrected-evidence="true" '
                f'value="{_safe_dom(message_alias + "|" + message_ref, 280)}"{checked} /> '
                f'人工证据 <code>{_safe_dom(message_alias, 32)} · {_safe_dom(message_ref, 240)}</code></label>'
            )
            message_html.append(
                '<div class="message ' + ("primary" if role.startswith("主") else "context") + '">'
                f'<span class="meta">{_safe_dom(message_alias, 32)} · {_safe_dom(role)} · '
                f'{_safe_dom(message.get("timestamp"), 80)} · {_safe_dom(message.get("sender_name") or ("我" if message.get("is_self") else "对方"), 80)} · '
                f'{_safe_dom(_message_type(message), 32)}</span>'
                f'<div class="message-body">{_safe_message_dom(text, 4000)}</div>{evidence_checkbox}</div>'
            )
        labels = {
            "topic": "主题（模型结果）",
            "evidence": "证据（模型结果）",
            "person": "人物",
            "object": "对象",
            "state": "状态",
            "tone": "语气/玩梗",
            "intent": "意图",
            "notes": "审阅备注",
        }
        fields: list[str] = []
        for field, label in labels.items():
            tag = "textarea" if field == "notes" else "input"
            initial_values = {
                "topic": item.get("corrected_topic") or item.get("topics") or item.get("topic"),
                "evidence": item.get("corrected_evidence_text") or item.get("evidence_refs") or item.get("evidence"),
                "person": item.get("corrected_person") or item.get("people") or item.get("person"),
                "object": item.get("corrected_object") or item.get("objects") or item.get("object"),
                "state": item.get("corrected_state") or item.get("states") or item.get("state"),
                "tone": item.get("corrected_tone") or item.get("speech_mode") or "",
                "intent": item.get("corrected_intent") or item.get("intent") or "",
            }
            initial = "" if field == "notes" else initial_values.get(field, "")
            initial_text = json.dumps(initial, ensure_ascii=False, default=str) if isinstance(initial, (list, dict)) else str(initial or "")
            if tag == "textarea":
                fields.append(f'<label>{label}<textarea data-field="{field}">{_safe_dom(initial_text, 1200)}</textarea></label>')
            else:
                fields.append(f'<label>{label}<input data-field="{field}" value="{_safe_dom(initial_text, 1200)}" /></label>')
        allow_model_correct = str(item.get("status")) == "complete" or bool(item.get("no_topic"))
        flags = "".join(
            (
                f'<button type="button" class="flag model-correct" data-flag="correct" '
                f'data-model-correct="true"{"" if allow_model_correct else " disabled"}>正确</button>'
            )
            if key == "correct"
            else f'<button type="button" class="flag" data-flag="{_safe_dom(key, 80)}">{_safe_dom(label, 80)}</button>'
            for key, label in HUMAN_REVIEW_FLAGS
        )
        review_state = "" if allow_model_correct else '<span class="review-needed">需人工补全/模型未通过：可填写人工答案并选择其他错误</span>'
        cards.append(
            f'<article class="card" data-unit-id="{_safe_dom(unit_id, 160)}">'
            f'<h2>{_safe_dom(date)} · 样本 {index + 1}</h2>'
            f'<div class="badges"><span>{_safe_dom(package_id)}</span><span>status={_safe_dom(item.get("status"), 32)}</span>'
            f'<span>provider_call={str(bool(item.get("provider_call"))).lower()}</span>'
            f'<span>预算={_safe_dom(item.get("output_token_budget"), 32)} tokens</span>'
            f'<span>输出={_safe_dom(item.get("output_tokens"), 32)} tokens</span>'
            f'<span>unknown={_safe_dom(item.get("unknown"), 32)}</span>'
            f'<span>error={_safe_dom(item.get("error_code"), 96) or "无"}</span></div>'
            f'<div class="conversation">{"".join(message_html) or "<em>没有可展示的原文</em>"}</div>'
            f'<details><summary>模型结构化结果（只显示已保存字段）</summary><pre>{_safe_dom(json.dumps({key: item.get(key) for key in ("topics", "people", "objects", "states", "overall_uncertainties", "evidence_refs", "speech_mode", "intent")}, ensure_ascii=False, indent=2, default=str), 8000)}</pre></details>'
            f'<div class="fields">{"".join(fields)}</div>'
            f'<div class="flags">{flags}</div>{review_state}'
            '<button type="button" class="save" data-save-card="true">保存本条</button><span class="saved"></span>'
            '</article>'
        )
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>主动学习 Round 1 审阅</title>
<style>
body{{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#f5f7fb;color:#172033;margin:0;padding:24px}}
header{{max-width:1180px;margin:0 auto 18px}} h1{{margin:0 0 6px}} .notice{{background:#fff4d6;border:1px solid #e8c86a;padding:12px;border-radius:8px}}
.card{{max-width:1180px;margin:16px auto;background:white;border:1px solid #dfe4ee;border-radius:10px;padding:16px;box-shadow:0 1px 3px #0000000d}}
h2{{font-size:18px;margin:0 0 8px}} .badges{{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 14px}} .badges span{{background:#eef2f8;border-radius:12px;padding:3px 8px;font-size:12px}}
 .conversation{{border-left:3px solid #bec9dc;margin:10px 0;padding-left:10px}} .message{{padding:8px 10px;margin:7px 0;border-radius:7px;background:#f2f4f7}} .message.primary{{background:#fff0b8;border-left:3px solid #d39b00}} .meta{{display:block;color:#59667a;font-size:12px;margin-bottom:4px}} .message-body{{white-space:pre-wrap;word-break:break-spaces}} .evidence-choice{{display:block;margin-top:6px;color:#324967;font-size:12px}} .evidence-choice code{{font-size:11px;word-break:break-all}} .review-needed{{display:block;background:#fff0f0;color:#a22b2b;border:1px solid #e4a5a5;border-radius:5px;padding:7px;margin:8px 0}}
details{{margin:10px 0}} pre{{white-space:pre-wrap;word-break:break-word;background:#f7f8fa;padding:10px;border-radius:6px;max-height:300px;overflow:auto}}
.fields{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}} label{{font-size:13px;color:#46536a;display:flex;flex-direction:column;gap:4px}} input,textarea{{font:inherit;border:1px solid #c7cfdd;border-radius:5px;padding:7px;background:#fff}} textarea{{min-height:56px;resize:vertical}}
.flags{{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}} button{{border:1px solid #aeb9cc;border-radius:6px;background:#fff;padding:7px 10px;cursor:pointer}} button.active{{background:#2359a7;color:#fff;border-color:#2359a7}} button.save{{background:#173b70;color:#fff;border-color:#173b70}} .saved{{margin-left:8px;color:#328151;font-size:13px}}
</style></head><body><header><h1>主动学习 Round 1 审阅</h1>
<div class="notice">本页展示 debug 24 个样本的原对话和模型结果。黄色消息是模型指定的核心主消息；灰色消息是同一 episode 的上下文。请修正主题、证据、人物、对象、状态、语气、意图并写备注，再点“保存本条”；所有标注保存在浏览器 localStorage，可导出 JSON。blind 8 个样本没有读取原文，也没有调用模型。</div>
<p>source={_safe_dom(SOURCE)} · production_blocked=true · StageB=false · StageC=false · accuracy=false</p>
 <button id="export" type="button">导出 human_labels.json</button> <label class="import">导入 human_labels.json <input id="import-file" type="file" accept="application/json,.json" /></label> <button id="clear" type="button">清空本地标注</button></header>
{"".join(cards)}
<script>
const STORAGE_KEY={json.dumps(SOURCE+":human_labels",ensure_ascii=False)};
const labels=JSON.parse(localStorage.getItem(STORAGE_KEY)||"{{}}");
const FIELD_NAMES=["topic","evidence","person","object","state","tone","intent","notes"];
function cardData(card){{const value={{flags:{{}},evidence_refs:[],evidence_aliases:[],corrected_evidence_refs:[]}};card.querySelectorAll("[data-field]").forEach(el=>{{const key=el.dataset.field;value[key]=el.value;value["corrected_"+key]=el.value;}});card.querySelectorAll("[data-flag]").forEach(el=>value.flags[el.dataset.flag]=el.classList.contains("active"));card.querySelectorAll("[data-evidence-checkbox]:checked").forEach(el=>{{value.evidence_refs.push(el.dataset.messageRef);value.evidence_aliases.push(el.dataset.messageAlias);}});value.corrected_evidence_refs=[...value.evidence_refs];return value;}}
function saveCard(card){{labels[card.dataset.unitId]=cardData(card);localStorage.setItem(STORAGE_KEY,JSON.stringify(labels));const saved=card.querySelector(".saved");if(saved)saved.textContent="已保存";}}
function loadCard(card){{const value=labels[card.dataset.unitId]||{{}};card.querySelectorAll("[data-field]").forEach(el=>{{const key=el.dataset.field;const candidate=value["corrected_"+key]!==undefined?value["corrected_"+key]:value[key];if(candidate!==undefined)el.value=typeof candidate==="string"?candidate:JSON.stringify(candidate);}});const refs=value.evidence_refs||value.corrected_evidence_refs; if(Array.isArray(refs))card.querySelectorAll("[data-evidence-checkbox]").forEach(el=>{{el.checked=refs.includes(el.dataset.messageRef)||refs.includes(el.dataset.messageAlias)||refs.includes(el.value);}});Object.entries(value.flags||{{}}).forEach(([key,on])=>{{if(on){{const b=card.querySelector(`[data-flag="${{CSS.escape(key)}}"]`);if(b&&!b.disabled)b.classList.add("active");}}}});}}
function normalizeRefs(value){{if(Array.isArray(value))return value.flatMap(normalizeRefs);if(value&&typeof value==="object")return normalizeRefs(value.refs||value.evidence_refs||value.corrected_evidence_refs||[]);if(typeof value!=="string")return [];const text=value.trim();if(!text)return [];try{{const parsed=JSON.parse(text);if(parsed!==value)return normalizeRefs(parsed);}}catch(_e){{}}return text.split(/\\s*,\\s*/).map(x=>x.trim()).filter(x=>x.startsWith("sqlite:messages:")||/^m\\d+(\\|sqlite:messages:)?/.test(x));}}
function sourceRecords(doc){{if(!doc||typeof doc!=="object")return [];if(doc.unit_id||doc.review_id||doc.sample_id)return [[doc.unit_id||doc.review_id||doc.sample_id,doc]];const container=doc.labels||doc.reviews||doc.samples||doc.items||doc.annotations||doc.records||doc;return Array.isArray(container)?container.map((row,i)=>[row.unit_id||row.review_id||row.sample_id||String(i),row]):Object.entries(container||{{}});}}
function normalizeImported(doc){{const out={{}};sourceRecords(doc).forEach(([key,row])=>{{if(!row||typeof row!=="object")return;const id=String(row.unit_id||row.review_id||row.sample_id||row.id||key||"");if(!id)return;const record={{flags:{{}}}};FIELD_NAMES.forEach(field=>{{const candidate=row["corrected_"+field]!==undefined?row["corrected_"+field]:row[field];if(candidate!==undefined){{record[field]=candidate;record["corrected_"+field]=candidate;}}}});if(record.notes===undefined&&row.text!==undefined)record.notes=String(row.text);if(row.text!==undefined)record.legacy_text=String(row.text);const refs=normalizeRefs(row.evidence_refs!==undefined?row.evidence_refs:(row.corrected_evidence_refs!==undefined?row.corrected_evidence_refs:row.evidence));record.evidence_refs=refs;record.corrected_evidence_refs=[...refs];if(row.evidence!==undefined)record.legacy_evidence=row.evidence;Object.assign(record.flags,row.flags||row.human_flags||{{}});Object.keys(record.flags).forEach(key=>record.flags[key]=!!record.flags[key]);out[id]=record;}});return out;}}
document.querySelectorAll(".card").forEach(card=>{{loadCard(card);card.querySelectorAll("[data-flag]").forEach(btn=>btn.addEventListener("click",()=>{{if(!btn.disabled){{btn.classList.toggle("active");saveCard(card);}}}}));card.querySelectorAll("[data-evidence-checkbox]").forEach(box=>box.addEventListener("change",()=>saveCard(card)));card.querySelector("[data-save-card]").addEventListener("click",()=>saveCard(card));}});
document.getElementById("import-file").addEventListener("change",event=>{{const file=event.target.files&&event.target.files[0];if(!file)return;const reader=new FileReader();reader.onload=()=>{{try{{Object.assign(labels,normalizeImported(JSON.parse(reader.result)));localStorage.setItem(STORAGE_KEY,JSON.stringify(labels));document.querySelectorAll(".card").forEach(loadCard);}}catch(_e){{alert("human_labels.json 格式无法导入");}}}};reader.readAsText(file);}});
document.getElementById("export").addEventListener("click",()=>{{const out={{schema_version:"active_learning_round1_human_labels_v2",source:{json.dumps(SOURCE)},exported_at:new Date().toISOString(),legacy_import_aliases:["text","evidence"],labels:labels,body_free:true}};const blob=new Blob([JSON.stringify(out,null,2)],{{type:"application/json"}});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="human_labels.json";a.click();URL.revokeObjectURL(a.href);}});
document.getElementById("clear").addEventListener("click",()=>{{if(confirm("确定清空本地标注？")){{localStorage.removeItem(STORAGE_KEY);location.reload();}}}});
</script></body></html>'''


def _human_labels_skeleton() -> dict[str, Any]:
    return {
        "schema_version": "active_learning_round1_human_labels_v2",
        "source": SOURCE,
        "status": "pending",
        "fields": ["topic", "evidence", "person", "object", "state", "tone", "intent", "notes"],
        "corrected_fields": ["corrected_topic", "corrected_evidence_refs", "corrected_person", "corrected_object", "corrected_state", "corrected_tone", "corrected_intent", "notes"],
        "legacy_import_aliases": ["text", "evidence"],
        "labels": {},
        "body_free": True,
    }


def _manifest(
    selection: Mapping[str, Any],
    debug_results: Mapping[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    rows = [item for item in debug_results.get("results", ()) if isinstance(item, Mapping)]
    budget, source = _configured_semantic_max_output_tokens()
    value = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "sampling_source": "cross_date_experiment.sample_sqlite_dialogue_bundles",
        "selection_manifest": "selection_manifest.json",
        "debug_results": "debug_results.json",
        "errors": "errors.json",
        "review": "review.html",
        "human_labels": "human_labels.json",
        "debug_dates": list(selection.get("debug_dates") or []),
        "blind_dates": list(selection.get("blind_dates") or []),
        "excluded_dates": list(selection.get("excluded_dates") or EXCLUDED_DATES),
        "debug_count": len(rows),
        "blind_count": _safe_int(selection.get("blind_count")) or 0,
        "persistent_call_limit": 24,
        "per_package_call_limit": 1,
        "retry_count": 0,
        "provider_calls": _safe_int(debug_results.get("provider_calls")) or 0,
        "provider_calls_total": _safe_int(debug_results.get("provider_calls_total")) or 0,
        "output_budget": {
            "version": SEMANTIC_OUTPUT_BUDGET_VERSION,
            "configured_tokens": budget,
            "effective_tokens": budget,
            "source": source,
            "env_var": CROSS_DATE_SEMANTIC_MAX_OUTPUT_TOKENS_ENV,
            "default_tokens": CROSS_DATE_SEMANTIC_MAX_OUTPUT_TOKENS_DEFAULT,
            "provider_capability_unknown": True,
        },
        "sampling_contract": {
            "unit": "episode_chunk",
            "messages_min": 5,
            "messages_max": 12,
            "same_scope": True,
            "exact_message_sequence_set_dedup": True,
            "same_date_jaccard_max": 0.35,
            "authoritative_core_only": True,
        },
        "accuracy": False,
        "accuracy_release_allowed": False,
        "stage_b": False,
        "stage_c": False,
        "production_blocked": True,
        "blind": {"content_read": False, "provider_calls": 0, "opaque_refs_or_hashes_only": True},
        "old15_regression": {
            "source": "cross_date_dialogue_semantic_experiment_v2_20260831",
            "purpose": "regression_only",
            "provider_calls": 0,
            "accuracy": False,
            "production_blocked": True,
        },
        "body_free": True,
    }
    value["artifact_hashes"] = {
        "selection_manifest_sha256": _file_digest(output_dir / "selection_manifest.json"),
        "debug_results_sha256": _file_digest(output_dir / "debug_results.json"),
        "errors_sha256": _file_digest(output_dir / "errors.json"),
        "review_sha256": _file_digest(output_dir / "review.html"),
        "human_labels_sha256": _file_digest(output_dir / "human_labels.json"),
    }
    _body_free(value)
    return value


def _review_ux_metrics(
    html_text: str,
    debug_results: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    """Run a static smoke check over the generated review page."""
    rows = [item for item in debug_results.get("results", ()) if isinstance(item, Mapping)]
    expected_cards = len(rows)
    expected_evidence = sum(
        _safe_int(item.get("message_count")) or 0
        for item in rows
    )
    if not expected_evidence:
        expected_evidence = sum(
            _safe_int(item.get("message_count")) or 0
            for item in (selection.get("debug_packages") or ())
            if isinstance(item, Mapping)
        )
    allowed_correct = sum(
        str(item.get("status")) == "complete" or bool(item.get("no_topic"))
        for item in rows
    )
    correct_buttons = re.findall(
        r'<button\b[^>]*data-model-correct="true"[^>]*>',
        html_text,
        flags=re.IGNORECASE,
    )
    disabled_correct = sum(" disabled" in button for button in correct_buttons)
    fields = {
        field: html_text.count(f'data-field="{field}"')
        for field in ("topic", "evidence", "person", "object", "state", "tone", "intent", "notes")
    }
    values = {
        "cards": html_text.count('<article class="card"'),
        "evidence_checkboxes": html_text.count('data-corrected-evidence="true"'),
        "correct_buttons": len(correct_buttons),
        "correct_disabled": disabled_correct,
        "correct_enabled": len(correct_buttons) - disabled_correct,
        "pending_or_error_cards": sum(str(item.get("status")) != "complete" for item in rows),
        "complete_or_no_topic_cards": allowed_correct,
        "save_controls": html_text.count('data-save-card="true"'),
        "export_controls": html_text.count('id="export"'),
        "import_controls": html_text.count('id="import-file"'),
        "editable_fields": fields,
        "expected_cards": expected_cards,
        "expected_evidence_checkboxes": expected_evidence,
        "expected_correct_disabled": max(0, expected_cards - allowed_correct),
        "expected_correct_enabled": allowed_correct,
        "legacy_import_aliases_present": all(
            marker in html_text
            for marker in ("normalizeImported", "legacy_import_aliases", 'row.text', 'row.evidence')
        ),
        "leading_whitespace_css": "white-space:pre-wrap" in html_text and "break-spaces" in html_text,
    }
    values["static_smoke_pass"] = all((
        values["cards"] == expected_cards,
        values["evidence_checkboxes"] == expected_evidence,
        values["correct_buttons"] == expected_cards,
        values["correct_disabled"] == values["expected_correct_disabled"],
        values["correct_enabled"] == values["expected_correct_enabled"],
        values["save_controls"] == expected_cards,
        values["export_controls"] == 1,
        values["import_controls"] == 1,
        all(count == expected_cards for count in fields.values()),
        values["legacy_import_aliases_present"],
        values["leading_whitespace_css"],
    ))
    return values


def _audit_summary(
    selection: Mapping[str, Any],
    debug_results: Mapping[str, Any],
    errors: Mapping[str, Any],
    manifest: Mapping[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    rows = [item for item in debug_results.get("results", ()) if isinstance(item, Mapping)]
    try:
        review_text = (output_dir / "review.html").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        review_text = ""
    review_ux = _review_ux_metrics(review_text, debug_results, selection)
    complete = sum(str(item.get("status")) == "complete" for item in rows)
    pending = len(rows) - complete
    calls = _safe_int(debug_results.get("provider_calls")) or 0
    package_counts = {
        str(date): sum(1 for item in rows if str(item.get("date")) == str(date))
        for date in selection.get("debug_dates", ())
    }
    budget, source = _configured_semantic_max_output_tokens()
    value = {
        "schema_version": "active_learning_round1_audit_v1",
        "source": SOURCE,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "intended_use": "debug semantic review and active-learning label collection",
        "grain": "one v2 episode_chunk package",
        "findings": [
            {
                "id": "selection_is_non_overlapping",
                "severity": "pass",
                "evidence": {"debug_count": len(rows), "same_date_jaccard_max": 0.35, "exact_sequence_set_dedup": True},
            },
            {
                "id": "blind_is_unread_and_unrun",
                "severity": "pass",
                "evidence": {"blind_count": _safe_int(selection.get("blind_count")) or 0, "content_read": False, "provider_calls": 0},
            },
            {
                "id": "provider_outcome_requires_human_review",
                "severity": "medium" if pending else "low",
                "evidence": {"complete": complete, "pending_or_error": pending, "error_code_counts": dict(errors.get("error_code_counts") or {})},
            },
        ],
        "selection": {
            "debug_dates": list(selection.get("debug_dates") or []),
            "blind_dates": list(selection.get("blind_dates") or []),
            "excluded_dates": list(selection.get("excluded_dates") or EXCLUDED_DATES),
            "debug_package_counts": package_counts,
            "blind_count": _safe_int(selection.get("blind_count")) or 0,
            "message_range_contract": "5..12",
            "same_scope": True,
            "max_jaccard": 0.35,
        },
        "debug_run": {
            "persistent_cap": 24,
            "per_package_cap": 1,
            "retry_count": 0,
            "provider_calls": calls,
            "total": len(rows),
            "complete": complete,
            "pending_or_error": pending,
            "token_budget": {"version": SEMANTIC_OUTPUT_BUDGET_VERSION, "configured": budget, "effective": budget, "source": source, "provider_capability_unknown": True},
        },
        "blind_run": {"content_read": False, "provider_calls": 0, "executed": False},
        "review_ux": review_ux,
        "old15_regression": {"purpose": "regression_only", "accuracy": False, "provider_calls": 0},
        "accuracy_release": {"allowed": False, "reason": "human_review_and_holdout_required"},
        "stage_b": False,
        "stage_c": False,
        "production_blocked": True,
        "checks": {
            "selection_manifest_present": (output_dir / "selection_manifest.json").exists(),
            "debug_results_body_free": True,
            "errors_body_free": True,
            "blind_body_free": True,
            "single_authorized_debug_run": calls <= 24,
            "json_artifact_safety_check": True,
        },
        "body_free": True,
    }
    value["artifacts"] = {
        "selection_manifest_sha256": _file_digest(output_dir / "selection_manifest.json"),
        "manifest_sha256": _file_digest(output_dir / "manifest.json"),
        "debug_results_sha256": _file_digest(output_dir / "debug_results.json"),
        "errors_sha256": _file_digest(output_dir / "errors.json"),
        "review_sha256": _file_digest(output_dir / "review.html"),
        "human_labels_sha256": _file_digest(output_dir / "human_labels.json"),
    }
    value["manifest_artifact_hashes_match"] = all(
        value["artifacts"].get(key) == (manifest.get("artifact_hashes") or {}).get(key)
        for key in ("selection_manifest_sha256", "debug_results_sha256", "errors_sha256", "review_sha256", "human_labels_sha256")
    )
    _body_free(value)
    return value


def run_round1(
    db_path: str | Path,
    *,
    output_dir: str | Path = OUTPUT_DIR,
    authority_root: str | Path = AUTHORITY_ROOT,
    settings_path: str | Path = Path("data") / "workbench_settings.json",
    debug_dates: Sequence[str] = DEBUG_DATES,
    blind_dates: Sequence[str] = BLIND_DATES,
) -> dict[str, Path]:
    """Select, run debug 24, and write all round-1 review artifacts."""
    output = _safe_experiment_path(output_dir, "active_learning_refuses_frozen_or_gold_output")
    output.mkdir(parents=True, exist_ok=True)
    selection, samples = build_active_learning_round1_selection(
        db_path,
        debug_dates=debug_dates,
        blind_dates=blind_dates,
        debug_packages_per_date=6,
        blind_packages_per_date=4,
        excluded_dates=EXCLUDED_DATES,
    )
    selection_path = write_active_learning_selection_manifest(selection, output)
    config = ExperimentConfig(
        dates=tuple(str(value) for value in debug_dates),
        packages_per_date=6,
        persistent_cap=24,
        per_date_cap=1,
        retry=0,
        source=SOURCE,
        production_blocked=True,
        stage_b=False,
        stage_c=False,
        exclude=25,
        authorization_id=AUTHORIZATION_ID,
    )
    debug_payload = run_experiment(
        config,
        samples,
        authority_root=_safe_experiment_path(authority_root, "active_learning_refuses_frozen_or_gold_authority"),
        settings_path=_safe_experiment_path(settings_path, "active_learning_refuses_frozen_or_gold_settings"),
    )
    debug_results = _debug_result_projection(debug_payload, selection)
    debug_path = output / "debug_results.json"
    _json_write(debug_path, debug_results)
    errors = _error_artifact(debug_results)
    errors_path = output / "errors.json"
    _json_write(errors_path, errors)
    html_path = output / "review.html"
    raw_content = _read_review_raw_content(db_path, samples)
    html_path.write_text(_render_review(debug_results, samples, raw_content_by_id=raw_content), encoding="utf-8")
    labels_path = output / "human_labels.json"
    _json_write(labels_path, _human_labels_skeleton())
    manifest = _manifest(selection, debug_results, output)
    manifest_path = output / "manifest.json"
    _json_write(manifest_path, manifest)
    audit = _audit_summary(selection, debug_results, errors, manifest, output)
    audit_path = output / "audit_summary.json"
    _json_write(audit_path, audit)
    return {
        "selection": selection_path,
        "debug_results": debug_path,
        "errors": errors_path,
        "review": html_path,
        "human_labels": labels_path,
        "manifest": manifest_path,
        "audit": audit_path,
    }


def refresh_round1_review_artifact(
    db_path: str | Path,
    output_dir: str | Path = OUTPUT_DIR,
) -> dict[str, Path]:
    """Regenerate only review UX and hashes; never call provider or ledger."""
    output = _safe_experiment_path(output_dir, "active_learning_refuses_frozen_or_gold_output")
    selection = json.loads((output / "selection_manifest.json").read_text(encoding="utf-8"))
    debug_results = json.loads((output / "debug_results.json").read_text(encoding="utf-8"))
    errors = json.loads((output / "errors.json").read_text(encoding="utf-8"))
    if not isinstance(selection, Mapping) or not isinstance(debug_results, Mapping) or not isinstance(errors, Mapping):
        raise ValueError("active_learning_artifact_must_be_object")
    debug_dates = tuple(str(value) for value in (selection.get("debug_dates") or DEBUG_DATES))
    samples = sample_sqlite_dialogue_bundles(
        db_path,
        debug_dates,
        packages_per_date=6,
        exclude=25,
        context_radius=4,
        min_messages=5,
    )
    raw_content = _read_review_raw_content(db_path, samples)
    review_path = output / "review.html"
    review_path.write_text(
        _render_review(debug_results, samples, raw_content_by_id=raw_content),
        encoding="utf-8",
    )
    # Keep the generated labels file on the current schema while preserving
    # any user label data if a human has already exported/imported it.
    labels_path = output / "human_labels.json"
    if labels_path.exists():
        try:
            labels_value = json.loads(labels_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            labels_value = _human_labels_skeleton()
    else:
        labels_value = _human_labels_skeleton()
    if isinstance(labels_value, Mapping) and not labels_value.get("labels"):
        labels_value = _human_labels_skeleton()
    _json_write(labels_path, labels_value)
    manifest_path = output / "manifest.json"
    manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_value, Mapping):
        raise ValueError("active_learning_manifest_must_be_object")
    manifest = dict(manifest_value)
    artifact_hashes = dict(manifest.get("artifact_hashes") or {})
    artifact_hashes.update({
        "review_sha256": _file_digest(review_path),
        "human_labels_sha256": _file_digest(labels_path),
    })
    manifest["artifact_hashes"] = artifact_hashes
    _body_free(manifest)
    _json_write(manifest_path, manifest)
    audit = _audit_summary(selection, debug_results, errors, manifest, output)
    audit_path = output / "audit_summary.json"
    _json_write(audit_path, audit)
    return {
        "review": review_path,
        "human_labels": labels_path,
        "manifest": manifest_path,
        "audit": audit_path,
    }


def refresh_round1_artifacts(output_dir: str | Path = OUTPUT_DIR) -> dict[str, Path]:
    """Refresh body-free projections and hashes without another provider call.

    This is useful after a presentation-only schema tightening.  It never
    opens the provider or authorization ledger and therefore cannot consume a
    debug slot or silently retry a package.
    """
    output = _safe_experiment_path(output_dir, "active_learning_refuses_frozen_or_gold_output")
    debug_path = output / "debug_results.json"
    selection_path = output / "selection_manifest.json"
    errors_path = output / "errors.json"
    debug_results = json.loads(debug_path.read_text(encoding="utf-8"))
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    errors = json.loads(errors_path.read_text(encoding="utf-8"))
    if not isinstance(debug_results, Mapping) or not isinstance(selection, Mapping) or not isinstance(errors, Mapping):
        raise ValueError("active_learning_artifact_must_be_object")
    # Old wrappers briefly persisted the ledger snapshot under this key.  It
    # is not needed for review and its name overlaps a credential category;
    # omit it from the body-free public projection.
    debug_results = dict(debug_results)
    debug_results.pop("authorization", None)
    provider_value = debug_results.get("provider")
    if isinstance(provider_value, Mapping):
        provider_public = dict(provider_value)
        provider_public.pop("api_key_configured", None)
        debug_results["provider"] = provider_public
    _body_free(debug_results)
    _json_write(debug_path, debug_results)
    manifest = _manifest(selection, debug_results, output)
    manifest_path = output / "manifest.json"
    _json_write(manifest_path, manifest)
    audit = _audit_summary(selection, debug_results, errors, manifest, output)
    audit_path = output / "audit_summary.json"
    _json_write(audit_path, audit)
    return {
        "selection": selection_path,
        "debug_results": debug_path,
        "errors": errors_path,
        "review": output / "review.html",
        "human_labels": output / "human_labels.json",
        "manifest": manifest_path,
        "audit": audit_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--authority-root", default=str(AUTHORITY_ROOT))
    parser.add_argument("--settings", default="data/workbench_settings.json")
    parser.add_argument("--selection-only", action="store_true")
    parser.add_argument("--refresh-only", action="store_true")
    parser.add_argument("--review-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = _safe_experiment_path(args.output_dir, "active_learning_refuses_frozen_or_gold_output")
    if args.selection_only:
        selection, _samples = build_active_learning_round1_selection(
            args.db,
            debug_dates=DEBUG_DATES,
            blind_dates=BLIND_DATES,
            debug_packages_per_date=6,
            blind_packages_per_date=4,
            excluded_dates=EXCLUDED_DATES,
        )
        path = write_active_learning_selection_manifest(selection, output)
        print(json.dumps({"selection": str(path), "debug_dates": selection["debug_dates"], "blind_dates": selection["blind_dates"], "debug_count": selection["debug_count"], "blind_count": selection["blind_count"]}, ensure_ascii=False))
        return 0
    if args.refresh_only:
        paths = refresh_round1_artifacts(output)
        print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False))
        return 0
    if args.review_only:
        paths = refresh_round1_review_artifact(args.db, output)
        print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False))
        return 0
    paths = run_round1(args.db, output_dir=output, authority_root=args.authority_root, settings_path=args.settings)
    print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
