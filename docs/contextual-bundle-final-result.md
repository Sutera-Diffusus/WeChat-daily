# Contextual Bundle 最终交付结果（2026-08-28）

## 结论（先说结果）

**本轮完成了 development-only 的 Contextual Bundle 架构、契约、影子接口和 body-free 审计产物，但没有完成生产交付。D9 真实金标准评测和 D11 API/DOM/screenshot E2E 尚未通过，D12 保持 `blocked`；首页、通知、发送路径和生产表均未切换。** v2.11 已明确写入 `provider_incompatible=true`、`production_blocked=true`、`provider_trials_stopped=true`，不能用结构覆盖率、HTTP 成功、provider 已配置或 57.8% 规则基线放行。

本报告只使用公开代码、文档、synthetic tests，以及 development artifact 的 manifest/aggregate/cost/capacity/error/audit summary 元数据；不读取或复述任何私有正文、身份或 frozen/frozen_test 内容。artifact 的共同来源标记为 `split=development`、`frozen_read=false`、`gold_loaded=false`。私有 JSON/JSONL 文件名中的 `private` 仅表示其存储位置；本报告只引用其中的 body-free 汇总字段。

权威设计和实施状态见：[design-review.md](D:/Project_Codex/Project_WeChatMoreFunction/docs/design-review.md)、[gold-standard-2026-08-25-contract.md](D:/Project_Codex/Project_WeChatMoreFunction/docs/gold-standard-2026-08-25-contract.md)、[contextual-bundle-implementation-plan.md](D:/Project_Codex/Project_WeChatMoreFunction/docs/contextual-bundle-implementation-plan.md)。

## D1-D12 状态

| 交付物 | 当前结论 | 证据与未完成项 |
| --- | --- | --- |
| D1 契约/schema | `accepted (contract-only)` | 分层、字段、枚举、unknown、evidence、scope 和零容忍规则已冻结；不等于生产实现准入 |
| D2 source/registry | `development implemented / not accepted` | 不可变 source、权威 metadata、fingerprint/replay 和 registry artifact 可检查；没有 D2 全量发布门记录 |
| D3 Fragment/Person/Argument/Claim | `partial / blocked` | 角色、对象、状态、claim 和未知边界已进入 schema/审计；没有真实标注上的 span/角色/object/state 评分 |
| D4 四通道 gate | `development implemented / blocked` | `immediate|pending_context|background|cold_recoverable`、预算和 activation cue 可见；尚无完整 D4 重激活发布门 |
| D5 DialogueBundle/snapshot | `development implemented / blocked` | bundle、open snapshot、W0-W4 和多 bundle 证据存在；v2.10/v2.11 有效语义编码为 0 |
| D6 多尺度召回 | `development implemented / not real-recall accepted` | scheduler 估算 269/280 structural coverage；没有真实 recall scorer，time-only/same-segment 仍不是强证据 |
| D7 bundle-level LLM | `experimental / blocked` | Flash/Pro 有部分 schema/evidence 输出但均有 wire/容量失败；v2.11 provider incompatible |
| D8 thread/event 派生 | `not accepted / blocked` | 未完成真实 thread/event 质量评测；沉默、未回复、topic shift 和窗口结束不能推出 `resolved` |
| D9 真实金标准/A-B-C-D | `blocked` | v1 仅为 57.8% rules-only baseline；本轮 development-only，且 v2.10/v2.11 失败 |
| D10 人工审计/replay | `partial / blocked` | v2.8/v2.9 有 body-free 审计、哈希和回放记录；反馈闭环与真实金标准验收未完成 |
| D11 Shadow API/DOM/screenshot E2E | `partial / blocked` | synthetic API/selector contract tests 通过；固定输入的 API→DOM→截图一致性尚未验收 |
| D12 只读生产接入 | `blocked` | D9、D11 未通过且 v2.11 明确 production blocked；不切首页 |

上述状态已同步到实施计划 §10.4；任何后续改善必须追加新版本 artifact 和状态，不得覆盖本快照。

## 版本化真实结果（body-free 汇总）

### v1 Stage1 baseline

v1 人工审计的 **57.8% 只作为 rules-only 基线**，用于判断后续候选是否优于规则对照；它不是生产质量、不是 D9 通过分数，也不能抵销模型失败或 zero-tolerance 失败。v1 汇总位置：[stage1_context_development_v1/audit_summary.private.json](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/stage1_context_development_v1/audit_summary.private.json)。

### v2.8 Flash-like development run

入口产物：[manifest](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_8/manifest.private.json)、[aggregate](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_8/aggregate.private.json)、[cost](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_8/cost.private.json)、[errors](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_8/errors.private.jsonl)、[audit summary](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_8/audit/audit_summary.private.json)。

| 指标 | 结果 |
| --- | --- |
| 输入/候选 | 280 messages；848 candidate decisions |
| 状态 | complete 8，pending 840；审计请求 complete 10，实际 8，故 `blocked_by_coverage_gap` |
| provider accounting | request attempts 14；successful model outputs 8；failed attempts 5；unfinished 1；budget deferred 837 |
| 成本/延迟 | legacy semantic stats：model calls 851、success 8、failure 843、12,246 input / 3,096 output tokens、26,569.53 ms total、31.222 ms average；budget ledger 14 reserved calls、15,774 / 4,691 tokens、25,967.596 ms |
| 口径 | 851 是 candidate/semantic decisions 口径，不是 provider requests；`calls_used=14` 也是旧预算 reservation 口径，不能替代 attempts |
| audit | complete 8、pending sample 20；v1 mapping 0/48（N/A）；complete evidence coverage 100%，但覆盖不足不能作为 D9 通过 |

v2.8 的 source/status marker 为 `model_wire`、`model_failed`、`budget` 等；它们描述流水线来源，不是 LLM acceptance。

### v2.9 Flash vs Pro A/B

两次运行保持相同 280-message development input hash、848 decisions、scheduler selected 14 packages 和 `max_provider_calls=14`；因此可以比较运行/协议行为，但因为没有 frozen/holdout gold 评分，不能声称语义质量 A/B 已完成。

| 项目 | Flash | Pro |
| --- | ---: | ---: |
| model | `deepseek-v4-flash` | `deepseek-v4-pro` |
| complete canonical bundles | 4 | 3 |
| pending | 844 | 845 |
| provider request attempts | 13 | 14 |
| successful model outputs | 4 | 3 |
| failed attempts / unfinished or pending | 8 / 1 unfinished（另有 3 provider rows deferred） | 9 / 1 pending、1 unfinished |
| schema-valid rate | 4/4 = 1.0 | 3/3 = 1.0 |
| evidence coverage | 4/4 = 1.0 | 2/3 = 0.6667 |
| input/output tokens | 14,331 / 4,302 | 13,153 / 3,791 |
| provider latency | 32,277.02 ms | 46,471.784 ms |
| 主要错误 | input limit 2；coreference shape 1；invalid JSON 1；object entity 3；state enum 3 | input limit 2；output limit 1；claim type 1；modality enum 7；state enum 1 |
| body-free audit | 4 complete + 20 pending；complete 2 fail/2 uncertain；pending sample 20/20 cue executable | 本次只作同包 A/B 对照；evidence 仍有缺口 |

Flash 审计的 selected package 为 14/14；scheduler 估算 structural coverage 为 269/280 = 0.9607，valuable coverage 为 221/226 = 0.9779。19/19 sampled valuable pending 保留可执行重激活线索，0 条被误当成 discarded。**这些是开发期结构/抽样结果，不是有效语义编码覆盖率，也不是生产准入。**

v2.9 scheduler 的 opaque run 标识为 `contextual_bundle_scheduler_v2_9`；source markers 按结果分为 Flash 的 `budget=823`、`model_failed=5`、`model_wire=4`、`scheduler_not_selected=16`，Pro 的 `budget=823`、`model_failed=6`、`model_wire=3`、`scheduler_not_selected=16`。它们是可追溯来源状态，不是 acceptance 标签。

相关 body-free 入口：[Flash aggregate](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9/aggregate.private.json)、[Flash cost](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9/cost.private.json)、[Flash errors](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9/errors.private.jsonl)、[Flash audit summary](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9/audit/audit_summary.private.json)、[same-package A/B map](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9/audit/selected_package_audit_map.private.jsonl)、[Pro aggregate](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9_pro/aggregate.private.json)、[Pro errors](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9_pro/errors.private.jsonl)。

### v2.10 JSON failure

产物：[manifest](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_10/manifest.private.json)、[aggregate](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_10/aggregate.private.json)、[capacity report](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_10/capacity_report.private.json)、[errors](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_10/errors.private.jsonl)。

- scheduler selected 14，但容量筛选后只有 11 个可 encode；3 个 `capacity_deferred`；848 decisions 全部 pending。
- provider request attempts 11，successful outputs 0，failed 9，pending 2；complete canonical bundle 0，有效语义编码消息数 0、有效覆盖 0。
- tokens 9,198 input / 5,662 output，provider latency 28,959.933 ms，14-call budget 中实际使用 11。
- 9 个 `compact_invalid_json`，2 个 `output_token_limit_exceeded`，3 个 `capacity_deferred`；结构上仍可报告 269/280，但实际 semantic coverage 为 0。

### v2.11 TSV failure and hard block

产物：[manifest](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_11/manifest.private.json)、[aggregate](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_11/aggregate.private.json)、[capacity report](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_11/capacity_report.private.json)、[errors](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_11/errors.private.jsonl)。

- manifest 明确：`provider_incompatible=true`、`production_blocked=true`、`provider_trials_stopped=true`；health probe 未运行（0 calls）。
- 11 provider attempts 全部 failed，0 successful outputs；848 decisions 全部 pending；complete canonical bundle 0；有效语义编码覆盖 0。
- tokens 4,187 input / 819 output，latency 16,047.596 ms；14-call budget 实际使用 11。
- TSV validator 错误包括 blank/whitespace、claim column count、claim type enum、mentioned-person duplicate/handle、target handle、uncertainty enum；不能把“返回了 TSV”当作 schema 成功。
- `semantic_audit.status=not_scored`，原因是 protocol compatibility only；没有任何生产接入或首页切换。

## Capacity 根因与错误分类

固定 bundle LLM v1 约束为每个包最多 8 messages/claims/fragments/evidence handles，input 2,000 tokens、output 400 tokens。14 个 scheduler package 中 3 个超过可安全编码容量：

| package 类型 | body-free 容量估算 | 结果 |
| --- | --- | --- |
| 大包 | 215 messages/claims；input 5,405 > 2,000；output lower bound 6,654 > 400 | `split_by_claim`，deferred |
| 中包 | 36 messages/claims；input 989 可接受；output lower bound 1,283 > 400 | `split_by_claim`，deferred |
| 边界包 | 9 messages/claims；output lower bound 399 ≤ 400，但超过推荐 shape/cap | `split_by_claim`，deferred |

因此，scheduler 的 14 selected、269/280 structural envelope 和 generic message capacity upper bound 112 都只是“可召回/可组织”能力；capacity-applied upper bound 39，更不能等同于模型实际完成。实际路径是 11 个 package 进入 provider，随后 JSON/TSV wire failures 使 v2.10/v2.11 的 effective semantic encoding 仍为 0。根因分三层：

1. **容量层**：package 分割与固定 2,000/400 预算不匹配；
2. **协议层**：Flash compact JSON invalid/limit，TSV wire shape/enum 不符合固定 schema；
3. **验收层**：没有完整有效 output 时，schema/evidence/semantic quality 应为 N/A 或失败，不能用 structural coverage 补齐。

成本报告目前没有可核验的货币价格字段；这里仅报告 provider attempts、tokens、latency 和 retries，货币成本记为 N/A，不把 N/A 写成零。

## Zero-tolerance、unknown 与门槛差距

v2.9 body-free audit 报告的抽样 zero-tolerance 为全 0：cross-chat relation、time-only strong link、unsafe same-segment strong link、silence terminal、fallback accepted 均为 0；activation cue sample 为 20/20，valuable pending reactivation 为 19/19。unknown policy 仍生效：start/end unknown 合法，silence 不是 terminal，unknown 不是 resolved，time/same-segment 不能单独强连。

v2.10/v2.11 没有 complete semantic output；其 structural validator counters 为 0 violation 不能证明语义正确，semantic zero-tolerance 应记为 **N/A（无可验收输出）**，不是 pass。D9 的真实 precision/F1、D8 的 thread/event 质量、D11 的 DOM/screenshot 一致性仍未达门。

## Shadow API、feature flag 与 analysis_run_id 选择

公开入口与约束已经落到以下位置，但它们只提供 review-only shadow surface，不等于首页生产接入：

- 服务端 endpoint 为 `/api/shadow-analysis`：[web.py:670](D:/Project_Codex/Project_WeChatMoreFunction/src/wechat_bridge/web.py:670)，实现位于 [web.py:1632](D:/Project_Codex/Project_WeChatMoreFunction/src/wechat_bridge/web.py:1632)。响应必须带 `analysis_run_id`、`source`、`provider_status`、`llm_accepted`、`fallback_reason` 和 window；`source_marker` 只作来源说明。
- flag 名为 `shadow_analysis_enabled`，默认 false：[settings.py:68](D:/Project_Codex/Project_WeChatMoreFunction/src/wechat_bridge/settings.py:68)，异常/字符串值归一化为 false：[settings.py:128](D:/Project_Codex/Project_WeChatMoreFunction/src/wechat_bridge/settings.py:128)。客户端默认同样为 false：[app.js:29](D:/Project_Codex/Project_WeChatMoreFunction/src/wechat_bridge/web/app.js:29)。
- `llm_accepted` 由 provider succeeded + schema/evidence gate 决定；fallback 永远 false。客户端接受谓词位于 [app.js:723-729](D:/Project_Codex/Project_WeChatMoreFunction/src/wechat_bridge/web/app.js:723)，不能依赖 `source === "ai_assisted"`。
- 没有 query id 时只返回 index/available ids；必须显式请求 `/api/shadow-analysis?analysis_run_id=<opaque-id>`；未知 id 返回 failed/404，不回退到 latest。[web.py:1667](D:/Project_Codex/Project_WeChatMoreFunction/src/wechat_bridge/web.py:1667)
- review selector 只在工作台显式刷新/选择 run：[app.js:765](D:/Project_Codex/Project_WeChatMoreFunction/src/wechat_bridge/web/app.js:765)，不写入首页 `state.insights`。首页默认不请求 shadow；source 复合值只能通过 acceptance predicate 判断。

当前 synthetic shadow contract test 结果为 7 passed：[test_shadow_api_selector_contract.py](D:/Project_Codex/Project_WeChatMoreFunction/tests/test_shadow_api_selector_contract.py)。仍缺真实固定输入 API→DOM→screenshot E2E；已有历史 review screenshot 的路径仅作定位，未读取、不作为本轮验收证据：[shadow_run_v2_8_review.png](D:/Project_Codex/Project_WeChatMoreFunction/data/private/gold_standard/2026-08-25/shadow_run_v2_8/shadow_run_v2_8_review.png)。

## 运行命令与可复核产物

```powershell
py -m pytest -q tests/test_contextual_bundle_v2_9_acceptance_contract.py tests/test_contextual_bundle_e2e_contract.py
# 23 passed

py -m pytest -q tests/test_shadow_api_selector_contract.py
# 7 passed

py tests/audit_contextual_bundle_pipeline_v2_8_private.py `
  --artifact-dir data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_8 `
  --input data/private/gold_standard/2026-08-25/working/p014_evaluation_split_v1/development/messages.private.jsonl `
  --v1-dir data/private/gold_standard/2026-08-25/stage1_context_development_v1

py tests/audit_contextual_bundle_pipeline_v2_9_flash_private.py `
  --artifact-dir data/private/gold_standard/2026-08-25/contextual_bundle_pipeline_v2_9 `
  --input data/private/gold_standard/2026-08-25/working/p014_evaluation_split_v1/development/messages.private.jsonl

py -m py_compile tests/audit_contextual_bundle_pipeline_v2_8_private.py `
  tests/audit_contextual_bundle_pipeline_v2_9_flash_private.py `
  tests/test_contextual_bundle_v2_9_acceptance_contract.py
```

审计脚本只产生 body-free summary/opaque audit artifact；本报告不以私有正文作为证据。`git diff --check` 及新增文档的 trailing-whitespace 检查通过。

## 最小解除阻塞条件与精确重跑步骤

### Provider 最小能力

换 provider 之前，能力探针必须真实证明以下全部条件：

1. 稳定的 strict JSON schema 或 tool/function-call JSON；禁止自由文本、Markdown、TSV 作为 canonical wire，且 response-format 不能只在请求中声明而不校验返回；
2. 在固定 2,000 input / 400 output budget 下可稳定完成，或在进入 provider 前按同一 selected package map 确定性拆包；
3. 保留 typed evidence handles、scope/window/channel 和 bounded span/field refs，不生成 provider-authoritative message IDs；
4. 枚举、数组、重复/空值、`unknown`/`insufficient` 和跨 scope 约束可验证；
5. 返回可用的 finish/status/usage metadata，支持幂等重试、replay/hash 和健康探针；provider error 不得泄漏正文/密钥，也不得把 fallback 伪装成 `llm_accepted`。

### 换 provider 后的重跑协议

1. 仅替换 provider/model，保留同一 development input SHA、同一 `analysis_run_id` 选择规则、同一 14 selected package opaque map、prompt/schema/ruleset 版本和 `max_provider_calls=14`；不得借机删除难例或改变 source marker。
2. 先运行 strict JSON/tool health probe，落盘 body-free capability/health artifact；probe 失败则直接 `provider_incompatible`，不进入语义调用。
3. 在同一容量政策下重跑（`max_input_tokens=2000`、`max_output_tokens=400`）；若需要 split，先固定 split manifest 和 package evidence，再开始 provider calls，保持 candidate 与 call 口径分栏。
4. 生成 manifest、aggregate、cost、errors、capacity、requests 和 audit summary；明确记录 `provider_request_attempts`、`successful_model_outputs`、`failed_provider_attempts`、`unfinished_provider_attempts`、`candidate_decisions`、`budget_deferred`，不再使用旧 `semantic_model_calls` 代替调用数。
5. 先跑 Flash，再用相同 14 package、同一 input hash 做 Pro A/B。最低继续条件为：selected ≤14、pending/cold activation cue coverage 100%、schema valid 100%、非 unknown evidence coverage 100%、effective semantic coverage >0、zero-tolerance 全 0；structural 269/280 单独不够。
6. 只有 development 修复后在授权 frozen/holdout 完成 D9 真实 scorer，并完成 D11 API/DOM/screenshot 固定输入 E2E，才可提交 D12 proposal。此前 `shadow_analysis_enabled=false`、首页不读 shadow、production tables 不写入。

## 交付判定

当前最准确的交付标签是：`development artifacts complete enough for review; semantic release blocked; production_blocked`。下一轮应从 capacity split、provider strict-schema/tool/json 能力和完整 frozen/holdout scorer 继续，而不是从首页切换开始。
