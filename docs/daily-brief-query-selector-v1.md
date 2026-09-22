# 微信日报 query-conditioned selection v1

这是接在现有语义重建/内容线抽取之后的最小选择入口，不改写旧日报产物。当前没有自动 inclusion-labeler；该入口不能据此宣称通用生产选择器已经完成。

2026-09-16 的实际旧链路是：

```text
tmp/build_daily_reconstruction_20260916.py
  -> context_reconstruction.reconstruct_context
  -> write_review_artifacts
  -> output/daily-chat-analysis-20260916/reconstruction.json

tmp/build_daily_semantic_20260916.py
  -> enrich_review_content_lines
  -> output/daily-chat-analysis-20260916-semantic/reconstruction.json

tmp/render_briefing_2026_09_16_full.py
  -> output/briefing-2026-09-16/briefing-2026-09-16.html
  -> output/briefing-2026-09-16/briefing-2026-09-16.md
```

新入口只读取语义重建产物，扁平化 `review.sample_units[].content_line_candidates[]`，然后对每个候选保留：

- `actor`、`action`、`object_or_recipient`、`status`；
- `claim_type`、`user_relevance`；
- `evidence_message_ids`、`evidence_bindings` 和上下文证据。

候选的正文资格只能来自人工 ACU codebook 的 `must`/`may`/`exclude` 标签：`must` 与 `may` 在证据和结构校验通过后进入选择结果，`exclude` 只进入审计。没有标签、没有直接证据、证据无法绑定或证据无法唯一对齐的候选都不会被旧的 local fallback 补回。对齐只用直接 message ID 的精确非空交集，不使用标题、宽泛词或相似度。

canonical ACU JSONL 可以只有 `atomic_content`、`inclusion_label` 和 `evidence_message_ids`，没有单独的 `evidence_bindings`；选择器会安全地合成一条 `{statement: atomic_content, message_ids: evidence_message_ids}` binding。语义 raw candidate 即使有 `summary_candidate`/`support_message_ids`，缺少显式 `evidence_bindings` 仍 fail closed，不会套用该合成规则。

`unknown` 和 `pending` 原样保留，并在结果里标记 `completion_inference_forbidden=true`；选择器不会把它们改成已完成。没有合格候选时写 `selection_failure.json` 并返回退出码 2，不生成正文。

## 运行

从仓库根目录运行：

```powershell
$env:PYTHONPATH = "src"
python tools/daily_brief_selector.py `
  --reconstruction output/daily-chat-analysis-20260916-semantic/reconstruction.json `
  --codebook data/private/daily_brief_research/2026-09-16/acu_gold.private.jsonl `
  --enrich-from-codebook `
  --output-dir output/daily-brief-query-selection-20260916 `
  --source semantic-reconstruction-20260916
```

`--enrich-from-codebook` 是显式的 Oracle/标注实验开关，用于把直接对齐的人工标注槽位带入新结果；不传时，语义内容线缺失的槽位保持 `unknown`，不能冒充正式日报。

成功时新目录包含 `selection_summary.json`、`selected_claims.json`、`selected_claims.md` 和 `selected_claims.html`；失败时只包含 `selection_summary.json` 与 `selection_failure.json`。旧的 `output/briefing-2026-09-16` 不会被覆盖。

语义 reconstruction 本身标记为 `candidate_only/production_blocked` 时，即使选择器得到合格候选，新目录也会保留 `production_blocked=true` 和 `formal_briefing_allowed=false`；这表示可供 Selection Oracle/人工复核使用，不把实验候选伪装成正式日报。

因此，带人工 codebook/ACU 的 2026-09-16 输出是 Selection Oracle 工件，不是自动生产日报；结果会标记 `oracle.manual_codebook=true`、`oracle.automatic_inclusion_labeler=false` 和 `oracle.production=false`。在形成可泛化的自动 inclusion labeler、时间隔离盲测和生产门禁前，不得把该单日输出推广为通用生产能力。

安装包后也可使用：

```text
wechat-daily-brief-select --help
```
