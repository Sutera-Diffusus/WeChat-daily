# DeepSeek 缓存命中低诊断：交接不是已证实根因

## 技术摘要

当前证据不支持“交接/传输损坏导致模型看不懂”这一因果解释。已验证的是：provider 侧前缀缓存与应用侧 `VersionedBundleCache` 是两层；应用 cache 是纯内存、按 runner run 新建，key 绑定输入 hash、schema、model、prompt、ruleset，并且只有验证通过的 `complete` 输出才写入。

交接属于开发编排/进程生命周期边界，交接内容没有进入 DeepSeek 请求。若交接触发 runner 重启，纯内存 cache 会自然丢失，这是可解释的运行时副作用；但它不是请求传输损坏的证据。低命中更可能来自跨 run 的 protocol/prompt/model 变化、fresh process，以及 provider prefix-cache 未被观测或未被复用。v2.10/v2.11 的主要失败更接近容量和 wire/schema 遵从问题，而不是“模型没收到交接内容”。

本报告只支持边界、口径和有限 development 诊断，不支持 production quality 或 provider cache SLA。

## 关键发现与证据分层

### Verified

- `VersionedBundleCache` 是 in-memory dictionary；`BundleSemanticEncoder` 每次编码前构造 request 和 cache key，key 包含 `input_sha256`、`schema_version`、`model_version`、`prompt_version`、`ruleset_version`。
- `BundleSemanticEncoder.encode` 只有在模型输出规范化且 validation 通过后才 `put`；失败、重试耗尽和 `pending` 不写 cache。
- v2.9 runner 每次 run 新建 `VersionedBundleCache` 和 pipeline，除非外部显式持久化，跨进程没有本地 cache 生命周期连续性。
- `SemanticWireBundleModel` 向 provider 发送固定 system/instructions 与动态 user/input payload；symbol-table hash、wire schema/prompt 版本进入 request context。交接文本不在该请求构造路径中。

### Likely

- 只要输入 hash、schema、model、prompt 或 ruleset 任一变化，key 就变化。v2.8 至 v2.11 的 runner、wire schema/prompt、model variant 持续变化，因此不能预期跨版本复用本地 cache。
- 如果 handoff 伴随 runner 进程重启，会使纯内存 cache 冷启动；这可以解释本地 miss，但不等同于传输损坏。
- provider/gateway 若没有保留或暴露 prefix-cache metadata，应用侧 miss 观察也可能与 provider 前缀复用脱钩。

### Not causal / unsupported

- 没有证据表明 Codex 子智能体交接文本被拼进 provider user payload、改写消息字节或造成 DeepSeek 解析损坏。
- HTTP 成功、provider 已配置、同一 source 名称或本地 cache miss，都不能单独证明 provider prefix-cache miss 或 LLM acceptance。

### Unresolved

- provider/gateway 的 prefix-cache read/write token、cache-control、prefix 命中条件和跨请求保留时间未在现有 body-free artifact 中暴露。
- 尚未完成同一进程、同一 key、连续两次 identical request 的 provider-side instrumented replay；handoff 时具体 runner 是否被终止/重启也未由本批 artifact 记录。

## 精确对照表

| Run | Model | Cache 口径 | Local hits | Local misses | Provider attempts | Successful outputs | Latency (ms) | Observed result |
|---|---|---|---:|---:|---:|---:|---:|---|
| v2.8 | deepseek-v4-flash | semantic stats | 0 | 851 | 14 | 8 | 26569.530 | 8 complete; 840 pending |
| v2.9 Flash | deepseek-v4-flash | runner bundle counter | 0 | 0 | 13 | 4 | 32277.020 | 4 complete; 844 pending; wire/schema failures |
| v2.9 Pro | deepseek-v4-pro | runner bundle counter | 0 | 0 | 14 | 3 | 46471.784 | 3 complete; 845 pending; evidence/schema gaps |
| v2.10 JSON | deepseek-v4-flash | runner bundle counter | 0 | 0 | 11 | 0 | 28959.933 | 0 complete; 0 effective semantic coverage |
| v2.11 TSV | deepseek-v4-flash | runner bundle counter | 0 | 0 | 11 | 0 | 16047.596 | 0 complete; provider_incompatible; production_blocked |

表中 `Local hits/misses` 是各 runner 自报的本地计数，不是 DeepSeek provider prefix-cache 计数；v2.9-v2.11 的 `0/0` 表示没有记录到可复用本地条目，不表示 provider 命中率为 100%。`Provider attempts`、`Successful outputs` 与 candidate decisions 分开统计；只有实际成功且通过 wire/schema/evidence 校验的输出才计入后者。

## 范围、指标与方法

比较单位是同一 280-message development 输入及对应 runner artifact；表格跨 v2.8、v2.9 Flash/Pro、v2.10 JSON、v2.11 TSV。v2.8 的 851 semantic model calls 与 14 provider attempts 并存，表明模型决策/重试/预算 bookkeeping 与真实 provider request 不是同一指标。

本诊断只读取公开源码以及 body-free development manifest/aggregate/cost/capacity/error 汇总；没有读取或复述消息正文、身份、frozen 或 frozen_test。结论按 `verified`、`likely`、`not causal`、`unresolved` 分级，未知处不强填。

## 为什么失败形态更像协议/容量问题

- v2.9 Flash/Pro 都有 provider attempts，但仅少量 successful outputs，并出现 wire JSON、enum、evidence 类错误。
- v2.10 固定 2,000 input / 400 output budget 时出现 capacity deferred、compact JSON invalid 和 output-token-limit，最终 effective semantic encoding 为 0。
- v2.11 TSV 的 11 次 attempts 全部失败，validator 报告列数、枚举、handle 和空白行问题，并显式标记 `provider_incompatible=true`、`production_blocked=true`。

这些错误与固定 schema、预算和证据 handle 的遵从直接相关；交接是否发生不能解释这些具体 wire validator 错误。

## 建议行动

1. **P0：先修 wire/capacity。** 对 selected package 做确定性 split，保留 package map、evidence handles 和 2,000/400 预算；provider health probe 必须证明 strict JSON/tool schema。
2. **P1：补 cache 可观测性。** 同时记录 local cache key、input/request hash、process/run identity 和 provider cache metadata；禁止跨 model、prompt、schema 或 ruleset 复用。
3. **P1：隔离 handoff 假设。** 在同一 runner 进程内做 identical replay，再在明确重启/交接后重放，比较“同 key 同进程”“同 key 新进程”“版本变化”三组。
4. **P2：再做 provider A/B。** 固定 input hash、selected package、prompt/schema/ruleset 与 14-call budget，分列 attempts、successful outputs、candidate decisions 和 budget deferred。

在 provider cache metadata 和 process continuity 未被测量前，不应把低 local-cache hit 归咎于交接，也不应据此切换首页生产流量。

## 最小复核协议

固定同一 input fingerprint、schema/prompt/ruleset/model，启动一个 runner 进程并提交相同 bundle 两次：第二次只有在相同 key 且第一条 complete 已写入时才应出现 local hit。随后结束进程/模拟交接再提交第三次；若仅第三次 miss，支持 in-memory lifetime 解释；若同进程第二次也 miss，应检查 canonicalization、symbol-table hash 或 adapter。最后只改变 model、prompt 或 schema 各一项，确认 key 变化且不会读取旧结果。

复核产物应只保留 body-free run manifest、cache key digest、process/run id、request hash、provider cache metadata、status/usage 与错误码。

## 局限与 portable 交付状态

本批只有 5 个 run-level rows，样本太小且不是时间序列；按要求不生成图表，只保留上面一张精确表。该限制已写入 canonical artifact 的 source note。v2.9 的 reactivation/zero-tolerance 只来自 development audit 摘要，v2.10/v2.11 没有 complete semantic output，因此语义质量应记为 N/A，而非通过。

按 `data-analytics:build-report` portable 命令执行：

```text
node "C:\Users\Suter\.codex\plugins\cache\openai-curated-remote\data-analytics\0.2.8-13ceeea1f599\skills\build-report\scripts\deliver_portable_artifact.mjs" --input "docs/cache-diagnostic-report/artifact.json" --output "docs/cache-diagnostic-report/report.html"
```

结果为：

```json
{"ok":false,"stage":"package","code":"delivery_failed","error":"$.manifest.blocks must include at least one chart block for report artifacts"}
```

该工具的 report surface 校验强制要求至少一个 chart block，与本报告的“5 行小样本不适合图表、只用一张精确表”要求冲突。为避免伪造/隐藏无意义图表，未添加 synthetic chart；因此 `report.html` 未生成，`artifact.json` 仍是 table-only canonical 草稿，本文是同目录的可读交付替代物。该 blocker 是打包器契约问题，不是 provider 或交接证据。

## Source notes

- 代码证据：[`src/wechat_bridge/bundle_semantics.py`](../../src/wechat_bridge/bundle_semantics.py) 中的 `VersionedBundleCache`、`BundleSemanticEncoder._cache_key` 与 `encode`；[`src/wechat_bridge/contextual_bundle_pipeline_v2_9_runner.py`](../../src/wechat_bridge/contextual_bundle_pipeline_v2_9_runner.py) 中的 per-run cache construction 和 provider-attempt accounting；[`src/wechat_bridge/semantic_wire.py`](../../src/wechat_bridge/semantic_wire.py) 中的 `SemanticWireBundleModel.cache_context` 与 `encode_bundle`。
- 数值证据：[`data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_8/aggregate.private.json`](../../data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_8/aggregate.private.json)、[`data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9/aggregate.private.json`](../../data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9/aggregate.private.json)、[`data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9_pro/aggregate.private.json`](../../data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9_pro/aggregate.private.json)、[`data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_10/capacity_report.private.json`](../../data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_10/capacity_report.private.json)、[`data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_11/manifest.private.json`](../../data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_11/manifest.private.json)。这些引用仅指向 body-free development metadata；未读取正文/frozen/frozen_test。
- 交接边界：本报告把 handoff 视为开发编排/进程生命周期事实；现有 provider request builder 中没有 handoff payload 字段。具体 handoff 时进程是否重启仍是 unresolved，需要上述 replay 记录确认。
