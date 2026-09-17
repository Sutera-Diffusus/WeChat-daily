# Contextual Bundle 实施计划

> 状态：第三版语义管线的实施计划草案，2026-08-27 起与 `design-review.md`、`gold-standard-2026-08-25-contract.md` 一起作为权威契约。
>
> 本计划只定义完整交付路线、接口、预算和验收门，不代表任何实现已经完成，也不授权现在接入生产。当前任务只更新文档；后续实现必须逐项通过本计划的门槛。

> 2026-08-31 路线修订：实施入口固定为 `ContentLedger → ConversationEpisode（交流形态） → TopicThread（话题流，可选） → DeepRead`。先验收无模型输入重建，再允许 DeepSeek；主题不是强制结果。本修订优先于早期主题分类/本地语义抽取措辞；Stage B/C、首页契约和 `production_blocked` 状态保持不变。

## 0. 目标、原则和范围

最终目标是完成一条可重放、可审计、可回滚的链路：

```text
ContentLedger（不可变原始消息 + 权威元数据）
  → ConversationEpisode / 交流形态（无模型输入重建）
  → TopicThread / 话题流（验收后可选 DeepSeek 判断）
  → DeepRead（沿用 Stage B → Stage C）
  → 有充分证据时的 event
  → 真实金标准评测 / 人工审计 / 影子运行
  → API / DOM / screenshot E2E
  → 只读生产接入、观测和可逆回滚
```

设计原则固定为：**可以延迟理解，但不能提前遗忘；可以暂不昂贵编码，但必须保留重新激活线索。** 这意味着：

- 原始消息不可变；正文、媒体引用、来源 ID、会话作用域、时间、顺序和回复关系只能追加校正记录，不能被摘要、模型或人工标签覆盖；
- 适配器/消息 registry 的 `account_id`、`chat_id`、`chat_type`、`speaker_id`、`direction`、时间、`reply_to_message_id` 和 `source_mode` 是权威元数据；正文中的自称、转述和推断不能改写它们；
- 理解可以延迟、降级或 abstain，但任何低成本路径都必须保留 message ID、hash、scope、未决槽位和 activation cue；
- `ContentLedger → ConversationEpisode → TopicThread（可选） → DeepRead` 是新的语义入口；旧 `fragment/claim/discourse_thread/event` 作为 DeepRead 兼容 artifact。fragment/claim/thread 可以没有 event，bundle 不是 event；
- `speaker`、`mentioned_person`、`subject`、`object` 和 `state` 先于 event；人物角色独立，对象解析为 `explicit|inherited|unknown`，状态固定六值 `unknown|planned|ongoing|resolved|failed|cancelled`；
- `conversation_opener`、只有开头、只有结尾、两端未知、无显式 reply 和未完线程都是正常数据状态；沉默、无后续、topic shift 和窗口结束不能写成 `resolved`；
- `information_value` 与 `event_completeness` 两个轴独立，价值低不删除，价值高不等于 event 完整；
- 交流作用（闲聊/问答/请求/回应等）、实际聊到的内容（content refs）和信息价值是三个独立字段；主题/TopicThread 可缺失，不为填满主题而丢掉过程证据；
- 本地只做候选召回、窗口/episode 拼接、媒体预处理和证据整理；DeepSeek 才做语义判断；媒体不可用必须显式记录缺失，不能充当语义 evidence；
- 真实评测、生产接入和 E2E 都是必须完成的交付，不因文档、单元测试或局部指标通过而跳过。

本计划不包括自动发送、批量发送、媒体识别扩展或前端语义合并。任何跨 chat 关联都必须经过硬约束和可审计 bridge；不同 account 永不共享身份作用域。

## 1. 权威数据与低成本索引

### 1.1 不可变 message 与 metadata authority

源消息进入只读 source 层后生成稳定 `message_id`、`body_digest` 和 `source_snapshot_fingerprint`。脱敏 release 的正文是标注输入，不是源消息的就地修改。元数据冲突不静默裁决：追加 `metadata_revision`、记录冲突代码，并将受影响语义槽位置为 `unknown`。

消息注册必须幂等：`(account_id, chat_id, source_message_id, source_snapshot)` 唯一确定一条逻辑消息；重采集、重放和跨分片导入不得创建第二条逻辑 message。派生对象不得反向写入 source 或 authoritative metadata。

### 1.2 message registry

registry 是每条消息必经的低成本路由索引，不保存未脱敏正文，不承担事件真值。最小字段：

```text
registry_id, message_id
account_id, chat_id, chat_type, speaker_id, direction
reply_to_message_id, source_mode, adapter_version, sequence_in_chat
event_time, event_time_precision, message_type
body_digest, body_length_estimate, language_hint
metadata_fingerprint, metadata_revision
registry_state, gate_channel, gate_reason_codes, activation_cues
artifact_refs, input_fingerprint
schema_version, pipeline_version, created_at
```

接口契约：

```text
register_message(source_message, authoritative_metadata) -> RegistryEntry
get_message(message_id, scope) -> ImmutableMessageView
get_registry_entry(message_id, scope) -> RegistryEntry
append_metadata_revision(message_id, correction, reason) -> RegistryEntry
record_gate_decision(message_id, decision) -> RegistryEntry
```

registry 写失败时消息留在可恢复输入队列，重试使用相同幂等键；仅语义索引失败时保留 registry 和 message，并降为 `error` 或 `cold_recoverable`，不能报告为已分析或 `unrelated`。

## 2. Adaptive semantic gate

### 2.1 四通道

gate 是成本调度状态机，不是分类器。四个通道固定为：

| 通道 | 目的 | 默认工作 | 激活/退出 |
| --- | --- | --- | --- |
| `immediate` | 当前 turn、显式 reply、用户查询、高风险硬约束或明确状态变化 | registry、结构化元数据、稀疏候选；默认不调用 LLM | 新消息/显式证据进入；超预算可转 pending/background |
| `pending_context` | 证据未足但未来可能补全的开放候选 | 保存未决对象/主体/状态/关系和 cue | 后续承接、对象补全、问答或状态证据升级；过期也只转 cold |
| `background` | 不阻塞当前交互的异步扩大理解 | 稀疏优先，dense 按需，受预算约束的 pairwise | 预算可用或人工审计激活；失败转 pending/cold |
| `cold_recoverable` | 当前不计算但保留重新激活线索 | 只保留 registry 元数据、hash、scope、cue、artifact refs | 查询、显式 bridge、重放或新证据重新激活 |

每次转移产生追加式 `gate_decision`：`from_channel`、`to_channel`、trigger/reason、预算类、cue、输入指纹、版本和时间。所有转移可逆、可重放；通道绝不表示 event 的终态，`cold_recoverable` 绝不等于无关。

### 2.2 路由状态机

```text
registered
  ├─> immediate ─────────────┐
  ├─> pending_context ───────┼─> reevaluate/replay ─> immediate|pending|background
  ├─> background ────────────┘
  └─> cold_recoverable <────── budget/error/expiry ───┘
```

路由触发器是元数据和可解释 cue 的集合：explicit reply/quote/forward、对象 span、状态变化、未决问题、用户 query、稳定外部 bridge、人工复核请求。时间邻近只能是弱 cue，不能独立升级为语义承接。

## 3. Contextual Bundle 与多尺度窗口

### 3.1 DialogueBundle

`DialogueBundle` 是一次分析运行的动态上下文视图。它包含 anchor、候选窗口、成员 message/fragment/claim、人物/对象/状态槽位、未决关系、scope、预算和证据，但不取得 event 身份。fragment/claim 可以属于多个 bundle；任何 `primary_bundle_id` 只是调度/展示便利。

最小契约：

```text
bundle_id, analysis_run_id, schema_version, pipeline_version
anchor_fragment_ids, anchor_claim_ids
candidate_window_id, window_scale
member_message_ids, member_fragment_ids, member_claim_ids
member_bundle_ids, open_context_snapshot_id
speaker_refs, mentioned_person_refs, subject_refs, object_refs
state_refs, unresolved_slot_codes
candidate_pair_ids, context_relation_ids
chat_scope, cross_chat_bridge_refs
evidence_refs, budget_class, gate_channel
status: open|provisional|abstained|materialized|superseded
uncertainties, provenance
```

### 3.2 open_context_snapshot

在新消息到达或重放时，`open_context_snapshot` 固化当时开放的 discourse thread、未决槽位、最近证据、待定关系、排除理由和下一步 activation cue。快照不可变，新证据生成新版本；它表示“仍开放”，不表示关闭或 `resolved`。

```text
snapshot_id, captured_at, analysis_run_id
open_thread_ids
unresolved_slots[{thread_id, slot, value, resolution, evidence_refs}]
recent_fragment_ids, recent_claim_ids
pending_relation_candidates, activation_cues
excluded_candidate_reasons
snapshot_version, provenance
```

### 3.3 多尺度候选窗口

候选窗口逐级扩展，范围优先服从 `account_id/chat_id`：

- `W0`：同一 message 内相邻 fragment/claim；
- `W1`：同 chat 局部前后文，允许跨 opener、ack、context-only 和插入句，不能只 zip 相邻 fragment；
- `W2`：同 chat 会话/日级窗口及 open snapshot；
- `W3`：长间隔的稀疏召回窗口；长间隔不是硬切断；
- `W4`：仅由显式 reply/quote/forward/稳定外部引用等 bridge 产生的跨 chat 候选。

同一 speaker、主题词、时间、same-segment 或 embedding 不能单独跨 chat 建边。跨 chat `same_event` 需要 bridge、对象/实例兼容、独立正证据和人工仲裁。跨 account 不允许共享 person/object identity scope。

## 4. 语义职责与固定 LLM schema

### 4.1 稀疏、dense、LLM 的边界

1. 稀疏表示、结构化元数据、显式回复和实体索引优先，用于低成本高召回；
2. dense/embedding 仅用于候选召回和窗口扩展，不输出上下文关系或 event identity；
3. LLM 只处理一个已脱敏 bundle 内的有限 candidate pairs，执行 pairwise 歧义裁决；
4. 规则只负责元数据归一化、scope/硬约束、schema/证据校验、路由和 fallback；规则分数不能成为语义真值；
5. 任意层都可 abstain/unknown/insufficient，不能为填满 schema 而补齐人物、对象、状态或事件。

### 4.2 固定 bundle-level LLM 输出

LLM 接收一个 `DialogueBundle`，输出 `bundle_llm_v1` 固定 JSON。最小结构：

```json
{
  "bundle_id": "BUNDLE_...",
  "schema_version": "bundle_llm_v1",
  "abstain": false,
  "slot_updates": [
    {"slot": "subject|object|state|time|intent", "value": "...",
     "resolution": "explicit|inherited|unknown", "evidence_refs": ["..."],
     "confidence": "high|medium|low"}
  ],
  "context_relations": [
    {"left_anchor_id": "...", "right_anchor_id": "...",
     "label": "continues|elaborates|answers|contrasts|topic_shift|possibly_related|insufficient",
     "evidence_refs": ["..."], "evidence_strength": "strong|medium|weak|none"}
  ],
  "pairwise_decisions": [
    {"candidate_pair_id": "...",
     "label": "same_event|related_event|same_topic_only|unrelated|insufficient_context",
     "evidence_refs": ["..."], "confidence": "high|medium|low"}
  ],
  "unresolved_slot_codes": ["..."],
  "model_provenance": {"model_id": "...", "model_version": "...", "prompt_version": "..."}
}
```

非 `unknown` 槽位和每条关系必须有 typed evidence。schema 无效、超时、越界或模型不可用时，完整回退为 `abstain` + `pending_context`/`insufficient`，不得部分采纳自由文本，不得直接写 event/title/state。

## 5. 模块、接口与数据流

### 5.1 模块边界

| 模块 | 输入 | 输出 | 允许副作用 |
| --- | --- | --- | --- |
| `message_registry` | source message + authoritative metadata | immutable view / registry entry | 追加 registry、revision；不得改 source |
| `semantic_gate` | registry + open snapshot + budget | reversible gate decision | 追加决策和队列状态 |
| `fragment_claim` | message view + metadata | Fragment/Person/Argument/Claim | 只写 versioned artifact |
| `candidate_window` | anchor + scope + scales | candidate set | 召回索引，不下语义结论 |
| `dialogue_bundle` | anchors + candidates + snapshot | DialogueBundle | 追加 bundle |
| `context_relation` | bundle + evidence | seven-label relation | typed evidence only |
| `bundle_llm` | redacted bundle + candidate pairs | fixed schema decision | 记录 token、模型版本和 fallback |
| `discourse_thread` | fragments/claims/relations | open/updated thread | 不因沉默关闭 |
| `event_materializer` | sufficient thread evidence | optional event candidate | 证据不足只保留 thread |
| `evaluator/replay` | versioned artifacts + gold | metrics/error/cost report | 不改输入和 frozen labels |

### 5.2 建议纯函数 API

```text
route_message(registry_entry, open_snapshot, budget) -> GateDecision
extract_fragments(message_view, authoritative_metadata) -> FragmentSet
build_candidate_windows(anchor, scales, scope, budget) -> CandidateSet
build_dialogue_bundle(anchor, candidates, open_snapshot) -> DialogueBundle
open_context_snapshot(open_threads, unresolved_slots, cues) -> Snapshot
judge_bundle(bundle, candidate_pairs, model_config) -> FixedBundleDecision
validate_and_materialize(bundle, decision) -> ThreadUpdate|EventCandidate
replay(run_id, dataset_version, component_versions) -> EvaluationReport
```

所有返回值带稳定 ID、版本、scope、evidence refs、confidence 和 uncertainties；写入采用 `analysis_run_id + input_fingerprint` 幂等。当前实施阶段禁止这些接口读取全局数据库、访问网络、调用发送适配器或修改现有生产 API。

### 5.3 端到端数据流

```text
source adapter (read-only)
  → immutable message + metadata authority
  → registry (always)
  → gate route (immediate/pending/background/cold)
  → fragments/persons/arguments/claims
  → W0..W4 candidate retrieval
  → dynamic bundle + open snapshot
  → sparse evidence / optional dense recall
  → optional fixed-schema LLM pairwise
  → typed context relations
  → discourse thread update
  → optional event materialization
  → gold scorer + human audit + replay
  → shadow API/DOM/screenshot E2E
  → read-only production proposal
```

## 6. 成本预算与默认额度

成本必须与语义质量同一 run 记录，不能用平均成本掩盖昂贵长尾。定义：

```text
C_total = C_registry + C_gate + C_sparse + C_dense + C_llm + C_evaluation
C_cpu = N_message*c_registry + N_candidate*c_sparse + N_bundle*c_bundle
C_dense = N_embedding*P_embedding + C_vector_index
C_llm = (T_input/1e6)*P_input + (T_output/1e6)*P_output + N_request*P_request
```

`P_*` 从实际服务价目或本地 CPU 基准注入，写入 manifest；不把价格常量埋进语义规则。默认每 1,000 条新消息的安全软预算：

| 项目 | 默认额度 | 超限动作 |
| --- | --- | --- |
| registry/稀疏入口 | 100% 消息；每消息最多 20 个 sparse candidates | 保留 registry，扩大理解不阻塞导入 |
| `immediate` | 无默认 LLM；只做 metadata、fragment 和必要 sparse | 转 pending/background，不丢消息 |
| dense | 最多 200 个歧义 bundle；每包 top-20 | 回 sparse 或 pending |
| LLM | 最多 50 个 bundle；每包 ≤2,000 input/400 output tokens | abstain，转 pending/cold |
| `cold_recoverable` | 默认 0 次模型调用 | 仅查询/显式 bridge 激活 |
| 延迟 | registry + sparse p95 ≤100 ms/message；bundle 构建 p95 ≤500 ms（不含 LLM） | 记录基准，阻断扩大窗口 |

默认额度可以按 manifest 调整，但必须同时记录调整原因、实际 request/token/CPU、p50/p95/p99、命中率、回退率和语义变化。预算失败不等于 `unrelated`。

## 7. 语义+成本评估矩阵与最低门

所有阶段必须输出语义和成本两组指标；不允许平均分抵消零容忍项。最低地板如下：

| 阶段/层 | 语义最低门 | 成本/运行门 | 零容忍 |
| --- | --- | --- | --- |
| Source/registry | message、权威 metadata、scope、hash 保留率 100% | 幂等成功率 100%；重复逻辑消息 0 | 原消息覆盖/丢失、metadata 静默替换、跨 scope 泄漏 |
| Gate | 四通道转移/重激活可重放率 100% | 预算、队列、回退可解释 | 不可逆删除、失败无 cue、cold 被算 unrelated |
| Fragment/role/object/state | span/type ≥98%；speaker/mentioned/subject ≥0.95；object macro-F1 ≥0.90；state macro-F1 ≥0.90 | registry+sparse p95 ≤100 ms | 角色互换、未知强填、沉默→resolved |
| Bundle/context | opener 保留 100%；context macro-F1 ≥0.85；无 reply 承接 precision ≥0.90；typed evidence 100% | bundle p95 ≤500 ms（不含 LLM） | 仅时间/same-segment 强连；跨 chat 无 bridge |
| LLM（实验） | fixed schema valid 100%；非 unknown evidence 100% | 50 bundles/1,000 messages 默认上限 | 自由文本落库；invalid 仍写 event |
| Event/真实评测 | same_event precision ≥0.98；灾难性合并 0；五分类 macro-F1 ≥0.85 | dense/LLM 仅候选调用 | MNL 穿透、无证据 event |
| API/DOM/screenshot E2E | 稳定 ID、来源、证据链一致率 100%；前端语义合并 0 | fixed input latency/failure recorded | DOM/API 不一致、来源偷换、生产写入/发送 |

N/A 规则：纯 opener、ack、reaction、context-only 的 `event_completeness` 不进入事件完整性分母；没有状态证据只记 `state=unknown`；没有候选关系不计关系召回；start/end 缺证据记 `unknown`。每个 N/A 必须有原因，不能通过删除难例制造通过。

Stage1 v1 人工审计 57.8% 只登记为 `rules_v1` 规则基线，不是生产质量，不是任何最低门的通过证明，也不允许接入生产。

## 8. 十二项完整交付

每项必须提交输入/输出 artifact、版本、成本报告、语义报告和验收记录。下一项不能以“前一项大致可用”提前开始生产准入。

### D1. 权威契约与 schema 冻结

冻结 message/metadata、registry、gate、Fragment/Person/Argument/Claim、Bundle、Snapshot、Thread、ContextRelation、Event 和固定 LLM schema；定义 `unknown`、N/A、evidence 和 scope 规则。

输出：契约文档、JSON schema/校验清单、版本策略、合成回归样例。

最低门：所有枚举/外键/unknown/证据形式可解析；不存在跨 chat 隐式身份字段；文档与金标准契约一致。

### D2. 不可变源与 message registry

实现只读源快照、稳定哈希、权威 metadata、追加 revision、幂等 registry 和可恢复输入队列。

输出：registry artifact、fingerprint/replay 证明、冲突/失败样例、成本/延迟基准。

最低门：100% message 有 registry；原消息/metadata 保留 100%；重复导入逻辑重复 0%；跨 scope 泄漏 0。

### D3. Fragment/Person/Argument/Claim 上下文抽取

按 span 做可回链的候选切分，先保留 speaker、mentioned_person、subject、object resolution、conversation_opener 的证据，再把 claim、state、价值和完整性留给已验收输入上的 DeepSeek/人审语义阶段；本地不把候选写成最终真值。

输出：结构化 artifact、未知边界样例、角色/对象/状态错误报告。

最低门：span ≥98%、角色 ≥0.95、object/state macro-F1 ≥0.90；终态误报 0；只有开头/结尾和无头无尾均可重放。

### D4. Adaptive gate 与四通道状态机

实现 `immediate|pending_context|background|cold_recoverable` 路由、budget、trigger/cue、追加决策和可逆重激活。

输出：gate log、状态转移图、重激活回放、超预算/服务失败回退报告。

最低门：转移可重放 100%；失败无消息丢失；cold 不丢 hash/scope/cue；通道不冒充语义 label。

### D5. 动态 DialogueBundle 与 open snapshot

实现 bundle build、版本化 snapshot、未决槽位和证据 refs；支持一个 fragment/claim 多 bundle 归属。

输出：W0/W1/W2/W3/W4 bundle fixture、open snapshot diff、跨 opener/context-only/插入句回归。

最低门：opener 保留 100%；跨非语义插入仍可承接；bundle 不创建 event；typed evidence 和 scope 完整。

### D6. 多尺度候选召回

实现 sparse-first 的 W0-W4 召回、同 chat 优先、显式 bridge 的跨 chat 候选，以及候选成本上限。

输出：候选召回曲线、跨 chat/must-not-link 报告、same-segment/time-only 反例报告。

最低门：候选召回达到真实基线目标且不穿透硬隔离；仅时间建边 0；跨 chat 无 bridge 建边 0；dense 不能直接判 event。

### D7. Bundle-level LLM pairwise 裁决（实验）

仅对已通过 `input_reconstruction_status=accepted` 的脱敏 bundle 中有限候选对调用模型，使用固定 schema、版本、token 和 evidence refs；允许 abstain。输入重建未通过时 provider request 必须为 0。

输出：schema validation、pairwise confusion、成本/延迟、timeout/invalid fallback 报告。

最低门：schema 合法率 100%；非 unknown 证据覆盖 100%；invalid/timeout 全量 fallback；LLM 不直写生产 event/title。

### D8. DiscourseThread 与 Event 派生

按七类 context relation 生成可开放 thread；仅在对象/主体/状态/证据充分时物化 event candidate；状态生命周期和未知边界不可越界。

输出：thread graph、event candidate provenance、MNL/状态/closure 报告。

最低门：context macro-F1 ≥0.85、无 reply 承接 precision ≥0.90；沉默/未回复/topic shift 不产生 resolved；MNL 0 穿透。

### D9. 真实金标准与 A/B/C/D 评测

在授权脱敏真实数据上完成 release、双人标注、冻结 split、synthetic regression、统一 scorer，并比较规则、召回、LLM、混合方案。

输出：manifest、语义+成本矩阵、错误分层、真实评测报告；冻结测试不参与调参。

最低门：隐私扫描和 split 隔离通过；真实评测达到 event `same_event` precision ≥0.98、灾难性合并 0、五分类 macro-F1 ≥0.85，或明确 `blocked` 并进入继续迭代。

### D10. 人工审计、反馈和全链路 replay

提供 bundle/thread/event 证据视图，支持 unknown/insufficient、关系修订、错误码和版本化反馈；每次 run 可用同一输入指纹重放。

输出：审计记录、错误 taxonomy、修订 diff、replay 对照和 N/A 统计。

最低门：每个可见判断证据覆盖 100%；人工修订不改 source/frozen labels；重放稳定 ID/版本/证据一致。

### D11. Shadow API/DOM/screenshot E2E

以独立来源和独立 `analysis_run_id/source` 运行影子链路，固定输入逐层比对 API、DOM、截图、证据和稳定 ID；前端只排版，不语义合并。

输出：API/DOM/screenshot 对照、性能和来源报告、前端不变量回归。

最低门：ID、来源、证据一致 100%；前端语义合并 0；shadow 不改变 legacy 首页、通知、发送或生产表。

### D12. 只读生产接入、观察期、回滚和运维

仅在 D1-D11 全部通过后提交生产提案：功能开关、只读存储、数据兼容、SLO、成本/质量监控、告警、观察期、incident runbook 和一键回滚。

输出：生产变更提案、回滚演练、监控面板/日志字段、兼容和数据保留计划。

最低门：所有零容忍项为 0；fixed input E2E 通过；观察期内质量/成本在预算内；回滚只切换派生读取源，不改原始消息、不发送消息。任一门未过，D12 保持 `blocked`，不得灰度。

## 9. 失败回退、版本与回滚

| 故障 | 立即回退 | 必须保留 |
| --- | --- | --- |
| 元数据缺失/冲突 | 槽位 `unknown`，追加 metadata revision | 原 message、scope、冲突代码 |
| registry/索引失败 | 可恢复队列 + 幂等重试；必要时 cold | body hash、metadata、activation cue |
| sparse/dense 服务不可用 | sparse-only 或 pending | 候选窗口、未决关系、版本 |
| 超预算 | 缩窗、转 pending/background/cold | bundle、claim evidence、重激活线索 |
| LLM timeout/invalid | abstain + insufficient/pending | bundle、请求版本、token、错误码 |
| relation 冲突 | possibly_related/insufficient | 两端证据和冲突槽位 |
| event materialize 失败 | 只保留 fragment/claim/thread | event candidate provenance |
| API/DOM/screenshot 不一致 | 阻断发布，保留上一版派生读取源 | run、diff、审计 artifact |
| 生产质量/成本超限 | 关闭功能开关并回到上一派生版本 | 原始消息、旧版结果和回滚原因 |

所有 artifact 使用 `schema_version`、`pipeline_version`、`ruleset_version`、`analysis_run_id` 和 `input_fingerprint`；新版本追加，不原地覆盖。规则/模型升级须能重放 development 和 holdout，冻结测试只在里程碑评测使用。

## 10. 闸门状态与未达标继续迭代

### 10.1 状态机

```text
planned → in_progress → evaluated → accepted
                         └────────→ blocked → remediation → evaluated
```

`accepted` 只对当前交付物生效，不等于生产准入。`blocked` 不是停止项目，而是锁定 artifact、错误码和下一轮修复范围。

### 10.2 未达标协议

任一最低门或零容忍项失败时：

1. 保留该版 artifact、输入指纹、成本、错误码和评测日志，不覆盖上一版；
2. 按错误码补充 synthetic 回归样例；真实数据只使用授权脱敏集，不在文档、日志或测试中复制私有正文；
3. 仅在 development 调整规则/窗口/预算，随后在 frozen_test 或 holdout 重跑；禁止通过删难例、扩大 N/A、改变 gold 标签或窥视冻结正文达标；
4. 成本失败优先降低 dense/LLM 或转 pending/cold；语义失败优先保留 unknown/insufficient、修复 evidence/window，不用宽泛关键词强填；
5. 报告门槛变化、错误分布、成本变化、回归样例和剩余风险；同一关键错误连续存在就继续迭代，不能用 57.8% 规则基线、平均分或产品压力豁免；
6. 只有 D1-D11 全部 accepted、D9 真实评测通过、D11 E2E 通过，才允许进入 D12 生产提案审查。

### 10.3 最终完成定义

完整交付必须同时满足：

- D1-D12 均有版本化 artifact、评测报告和验收记录；
- message/metadata 不可变、registry 可重放、四通道可逆且 cold/pending 线索完整；
- fragment/claim/thread/event 证据链完整，未知边界和状态生命周期不越界；
- 真实金标准、synthetic regression、人工审计、成本+语义矩阵和 frozen/holdout 评测完成；
- shadow API/DOM/screenshot E2E 一致，前端没有语义合并；
- 生产接入仅在只读、可观测、可回滚和观察期条件下批准；未达标则明确 `blocked` 并继续执行第 10.2 节。

### 10.4 2026-08-28 当前交付状态（body-free 证据）

下表是当前 development artifact、synthetic contract tests 和公开入口审计的状态快照。`implemented` 只表示开发链路/接口已有可检查产物，不表示真实金标准通过或已获生产准入；`partial`、`blocked` 和 `N/A` 必须按第 10.2 节继续迭代，不能用 57.8% 规则基线替代。

| 交付物 | 当前状态 | 依据/缺口 |
| --- | --- | --- |
| D1 契约与 schema | `accepted (contract-only)` | [design-review.md](D:/Project_Codex/Project_WeChatMoreFunction/docs/design-review.md)、[gold contract](D:/Project_Codex/Project_WeChatMoreFunction/docs/gold-standard-2026-08-25-contract.md)、synthetic tests；未宣称生产实现完成 |
| D2 不可变 source + registry | `development implemented / not accepted` | development registry、fingerprint/replay 和 synthetic checks 可审计；尚无 D2 全量发布门记录 |
| D3 Fragment/Person/Argument/Claim | `partial / blocked` | 字段和未知边界已落 artifact；尚无真实标注上的 span/角色/object/state 评分，完整性仍不足 |
| D4 四通道 adaptive gate | `development implemented / blocked` | pending/cold cue 与预算状态可回放；尚无完整 D4 重激活发布门，不能把 channel 当语义标签 |
| D5 DialogueBundle/open snapshot | `development implemented / blocked` | W0-W4、bundle、snapshot artifact 存在；v2.10/2.11 有效语义编码为 0，未达动态 bundle 门 |
| D6 多尺度候选召回 | `development implemented / not real-recall accepted` | scheduler 估算 structural coverage 269/280；没有真实 recall scorer，time-only/same-segment 仍只能作弱/零权证据 |
| D7 bundle-level LLM | `experimental / blocked` | Flash/Pro A/B 均有 wire/schema/evidence 缺口；v2.11 明确 provider incompatible；不能直写生产 event/title |
| D8 Thread/Event 派生 | `not accepted / blocked` | 未完成真实 thread/event 质量评测；无 reply、沉默和 topic shift 不得推出 resolved |
| D9 真实金标准与 A/B/C/D | `blocked` | v1 仅 57.8% 规则基线；本轮均为 development、`frozen_read=false`、`gold_loaded=false`，v2.10/v2.11 失败 |
| D10 人工审计/replay | `partial / blocked` | body-free v2.8/v2.9 audit、hash/replay 记录存在；未完成完整真实金标准反馈闭环 |
| D11 Shadow API/DOM/screenshot E2E | `partial / blocked` | API/selector contract tests 通过；尚无固定输入 API→DOM→截图一致性验收，故不能放行 |
| D12 只读生产接入 | `blocked` | D9、D11 未通过且 v2.11 `production_blocked=true`；首页、通知、发送和生产表不切换 |

状态更新时间以本节为准；若新一轮 artifact 改善，必须追加新版本状态，不得覆盖本快照或把实验状态倒写成 accepted。

## 11. Workstream K1：ContextPacket 与分阶段 DeepSeek 实施路线（2026-08-28）

> 本节是 D1-D12 之上的实施收敛，状态为 contract-only。K1 的本地代码只做上下文工程和高召回候选，不做最终语义裁决；没有 K1 的真实金标准、shadow API/DOM/screenshot E2E 和生产闸门记录，不得接入首页或生产表。

### 11.0 输入重建先行与无模型 prototype

K1 的进入顺序固定为：

```text
ContentLedger → ConversationEpisode / 交流形态
              → TopicThread / 话题流（可选）
              → DeepRead（DeepSeek；沿用 Stage B → Stage C）
```

`ContentLedger` 是不可变 message、权威 metadata、正文引用/digest、顺序、reply/quote 和媒体状态的证据根。`ConversationEpisode` 是本地只依据可观察顺序、reply/quote、fragment span 和候选互动信号拼接的交流过程单元；它不是 topic、event 或语义真值。`TopicThread` 是输入验收后由 DeepSeek 判断的可选内容流，闲聊、纯确认、反应和证据不足均可无 topic；`DeepRead` 才负责语义连续性、内容归属、claim、state、信息价值和不确定性。

本地实现仅允许候选召回、窗口/episode 拼接、媒体预处理（索引、可用性、OCR/ASR 产物登记，不解释内容）和证据整理。规则、关键词、embedding、时间邻近和 same-segment 不得直接输出最终交流形态、TopicThread、claim、state 或 information value。交流作用、聊到的内容和信息价值必须物理分栏：`interaction_role_candidate`、`content_refs`、`information_value`；低价值不删除，主题不强制。

prototype 的 `interaction_form_candidate` 只是待审阅过程信号；最终 `interaction_form`（如需要）只能由人审或已验收输入上的 DeepSeek 产生并带 typed evidence，且不改变 Stage B/C schema。

#### 11.0.1 prototype artifact 与字段

无模型 prototype 只接受 synthetic fixture 或授权脱敏 working/release，不读取或发布 frozen 正文。建议三个 artifact：`content_ledger.prototype.jsonl`、`conversation_episodes.prototype.jsonl`、`input_reconstruction_report.prototype.json`。

```text
ContentLedgerV1
  content_ledger_id, message_id, scope, sequence_in_chat, event_time
  speaker_id, direction, reply_to_message_id, quote_refs
  content_ref/body_digest, fragment_refs, evidence_refs
  media: [{media_type, availability, preprocess_status,
           artifact_ref, missing_reason, semantic_evidence}]

ConversationEpisodeV1
  conversation_episode_id, ledger_message_ids, fragment_ids
  interaction_form_candidate: social_smalltalk|question_answer|
    request_response|coordination|debate|broadcast|monologue|mixed|unknown
  interaction_role_candidates, content_refs
  information_value: none|low|medium|high|unknown
  information_value_source: human_review|unknown
  boundary, view_refs[{view_scale: today|yesterday|week, view_id}]
  evidence_refs, uncertainties, status: review_required|accepted|rework|blocked

InputReconstructionReportV1
  prototype_version, input_fingerprint, ledger_ids, episode_ids
  input_reconstruction_status: accepted|pending|rework|blocked
  checks, metric_values, reviewer_ids, reviewed_at
  model_call_allowed: false|true
```

`media.availability` 必须是 `available|unavailable|not_present|unknown`；`unavailable` 必填 `missing_reason` 且 `semantic_evidence=false`。媒体占位、路径、类型、失败 OCR/ASR 不是语义 evidence；独立人工核验的脱敏 transcript 才能作为文本 evidence。`today|yesterday|week` 是同一 episode/thread 的查询视图，不是切分键；跨日/跨周保留稳定 thread ID，不因日历边界拆分或关闭。

#### 11.0.2 输入验收 gate 与指标

只有 `input_reconstruction_status=accepted` 才能创建 Stage A provider request；`rework|blocked|pending` 的 provider request 必须为 0，并保留 reason code、input fingerprint 和 activation cue。Stage A 无 topic 不是失败，也不能改写成 `unrelated`。最低门：

`model_call_allowed=true` 当且仅当 `input_reconstruction_status=accepted`；该值由输入验收报告生成，不由 provider、模型或调用方覆盖。

| 指标 | 最低门 |
| --- | --- |
| ledger message/metadata/scope/order/reply/quote 保留 | 100%；丢失、重复和静默改写为 0 |
| episode 可审阅覆盖、外键和 evidence 回链 | 100% |
| episode 边界/关系 | context macro-F1 ≥0.85；无 reply 承接 precision ≥0.90；仅时间/same-segment 强连为 0 |
| 交流作用、content_refs、information value 分离 | 字段互不推导；四种组合保留率 100%；低价值不误过滤 |
| 主题可选 | 强制 topic/TopicThread 率为 0；topic coverage 仅诊断 |
| 媒体缺失 | unavailable 显式缺失率 100%；媒体占位/路径充当语义 evidence 率 0 |
| 日/周视图 | 同一 thread 稳定 ID 一致率 100%；仅日历边界拆分/关闭率 0 |
| 模型前置闸门与 replay | 未 accepted 的 request 为 0；accepted 才调用合规率 100%；同输入版本 ID/scope/evidence/status 一致率 100% |

任一硬门失败即 `rework` 或 `blocked`，不进入 DeepSeek；通过输入门只允许实验性 Stage A/B/C，仍不改变首页或生产闸门。

### 11.1 本地职责、权威边界与禁止事项

本地链路负责：`ContentLedger` 中不可变 message 与权威微信元数据、fragment 引用、跨尺度候选窗口、`ConversationEpisode` 过程拼接、packet 组装、重叠包、媒体预处理、激活线索、候选原因和可重放 artifact。它不负责：最终 topic/thread/claim 真值、event identity、标题/摘要、自动关闭 thread 或任何生产展示语义；最终语义判断交给 DeepSeek。

权威事实与候选上下文必须在数据结构中物理分区：

```text
authoritative_facts = source/registry facts only
  metadata + chat scope + speaker + order + reply + quote + exact span

candidate_context = reversible retrieval hints only
  continuity + QA + person/object/state history + open thread + cues + reasons/confidence
```

规则、时间邻近、same-segment、关键词、同 speaker、主题词和 embedding 只能影响候选召回或路由，不能直接输出最终语义结论。任何无法确认的值保留 `unknown`；错误、超时、预算不足和 provider 不可用不转写成 `unrelated`。

### 11.2 `ContextPacket v1` 建造步骤

新增逻辑模块（本阶段只定义接口，不在本任务实现）：

```text
message_registry.get_immutable_view
  → conversation_episode_builder.build
  → input_reconstruction_validator.accept
  → context_packet_builder.build
  → candidate_window.retrieve(W0..W4)
  → context_packet_validator.validate
  → stage_scheduler.enqueue(A/B/C)
```

建议 API：

```text
build_context_packet(
  message_view,
  authoritative_metadata,
  accepted_conversation_episode,
  anchor_fragment_ids,
  anchor_claim_ids,
  candidate_windows,
  open_context_snapshot,
) -> ContextPacketV1

build_conversation_episode(ledger_view, candidate_windows) -> ConversationEpisodeV1
validate_input_reconstruction(ledger, episodes) -> InputReconstructionReportV1

validate_context_packet(packet) -> PacketValidation
fingerprint_context_packet(packet) -> input_fingerprint
split_packet_for_budget(packet, limits) -> ContextPacketV1[]
```

`ContextPacketV1` 必须至少包括：

```text
packet_id, context_packet_version, analysis_run_id, scope
anchor_fragment_ids, anchor_claim_ids
boundary.start/boundary.end: explicit|unknown + evidence ref
window.scale: W0|W1|W2|W3|W4
authoritative_facts:
  message_metadata, sequence/order, reply_edges, quote_edges, fragment_spans,
  media availability/preprocess status (unavailable => semantic_evidence=false)
candidate_context:
  continuity_candidates, qa_candidates
  person_history, object_history, state_history
  open_threads, activation_cues, candidate_reasons
overlap_group_id, status, evidence_refs, provenance
```

`authoritative_facts` 只能追加 metadata revision，不能由模型覆盖；`candidate_context` 的每一项都必须标候选原因/置信度，不能把 candidate ID 或相似度当事实。一个 fragment/claim 可进入多个 packet；packet 可以重叠；`start`、`end` 一端或两端为 `unknown` 都是正常边界。跨 chat 只保留显式 bridge 候选，跨 account 永不共享身份作用域。`split_packet_for_budget` 必须按完整证据边界切分，不能截断 span、丢 reply/quote 或静默删除未决线索。

### 11.3 DeepSeek 三阶段调度

K1 每阶段使用独立 prompt、schema、cache namespace、预算和 artifact；禁止一次 17 字段大 schema：

| 阶段 | 本地输入 | DeepSeek 允许输出 | 后续消费者 |
| --- | --- | --- | --- |
| A：TopicThread mapping（可选） | 已通过输入重建验收的一个或多个 ContextPacket + candidate IDs | 仅可选 topic/thread/message/fragment/claim/context IDs 的分组，以及无主题、unmapped/unknown IDs；`topic_groups=[]` 合法 | 未验收输入、强制生成 topic、人物/对象/state/claim 真值 |
| B：per-topic claims | packet + A 的一个 topic/thread 分组 | speaker、mentioned_person、subject、object resolution、action、六值 state、claim_type、modality、typed evidence | 生成每 topic claim artifact；不合并 topic，不生成 event/title |
| C：consistency audit | packet + A/B 已验证输出 | conflicts、omissions、merge_errors、unknown preservation、审计状态及证据 | 修复/回放队列；不偷偷改写 A/B，不生成 event |

阶段 API：

```text
stage_a_map_topic_threads(packet, stage_config) -> StageATopicThreadResult
  # old stage_a_map_topics is compatibility alias only
stage_b_extract_claims(packet, topic_group, stage_config) -> StageBTopicClaimsResult
stage_c_audit_consistency(packet, stage_a_result, stage_b_results, stage_config)
  -> StageCConsistencyResult
```

每个 response 的最小字段固定为：

```text
Stage A: decision_id, packet_id, topic_groups, thread_groups,
         topic_absent_episode_ids, unmapped_message_ids,
         unknown_context_ids, evidence_refs,
         validation_status, fallback_action
Stage B: decision_id, packet_id, topic_id, claims[
           claim_id, speaker_id, mentioned_person_ids, subject_id,
           object_id, object_resolution, action, state, claim_type,
           modality, evidence_refs, unknown_field_codes],
         validation_status, fallback_action
Stage C: decision_id, packet_id, stage_a_decision_id,
         stage_b_decision_ids, conflicts, omissions, merge_errors,
         unknown_preservation, audit_status, validation_status,
         fallback_action
```

Stage A 不得出现人物、对象、动作、状态、claim/event/title；无主题不是 `unrelated`。Stage B 的六值 `state` 固定为 `unknown|planned|ongoing|resolved|failed|cancelled`，`historical` 只能是 temporal qualifier；Stage C 只审计，不升格为最终语义裁决。Stage B/C 的 schema 和禁止项保持不变。

### 11.4 证据、缓存和状态机

证据句柄统一为 `message|fragment|claim|reply|quote|span`。Stage A 的 evidence 只证明 packet 中的 ID/分组；Stage B 每个非 `unknown` 的人物、对象、动作、state、claim_type 和 modality 必须回到 packet 中的 typed evidence；Stage C 每个冲突、漏项、错误合并和 unknown 记录必须关联 A/B 输出及其输入证据。自由文本理由不算 evidence。

阶段 cache key 不共享：

```text
K_stage = H(
  stage_name + stage_schema_version + canonical_packet_fingerprint +
  input_stage_decision_ids + model_id + model_version +
  prompt_version + ruleset_version
)
```

缓存写入条件是 schema 合法、证据校验通过、状态 `complete`；failed/pending/timeout/unavailable/over-budget/越界结果禁止 put。典型状态机：

```text
input_reconstruction_pending
  → input_reconstruction_accepted → packet_built
  ↘ rework/blocked (no provider request)
packet_built
  → stage_a_pending → stage_a_complete
  → stage_b_pending → stage_b_complete
  → stage_c_pending → stage_c_complete
       ↘ any failure/over-budget → pending_context
       ↘ expiry/no current work → cold_recoverable
```

任何失败都保留 packet、已验证前置阶段、input fingerprint、reason code、预算、attempt、版本和 activation cue。A 失败不调用 B；B 失败保留 A；C 失败保留 A/B 并将 consistency 记 `unknown|pending`。新消息、显式 reply/quote、对象/状态补全、用户查询、人工复核或 replay 可逆激活 pending/cold；沉默、窗口结束和 topic shift 不关闭 thread。

### 11.5 成本分配与失败回退

K1 成本单独拆分，不把 candidate 数当 provider call：

```text
C_K1 = C_registry + C_packet + C_sparse + C_dense + C_A + C_B + C_C + C_replay
C_stage = N_request * P_request
        + (T_input/1e6) * P_input
        + (T_output/1e6) * P_output
```

当前 280-message development replay 的默认软预算沿用 14 次 provider attempts；三阶段预算在 run manifest 中预先分配（建议默认 A=4、B=7、C=3），阶段不得互相挪用。每次请求建议上限为 2,000 input / 400 output tokens；packet 必须先按完整证据切分，超限则 pending，不截断或丢弃。每 1,000 条新消息的长期预算仍为 registry/稀疏 100% 覆盖、dense 仅按需召回、LLM 只处理 selected topics，实际偏离必须记录。

| 故障 | 必须回退 | 禁止行为 |
| --- | --- | --- |
| packet metadata 缺失/冲突 | 保留权威 revision，受影响槽位 unknown，packet 可重放 | 用正文自称或同 chat 默认值改 metadata |
| candidate/embedding 不可用 | sparse 候选 + activation cue | embedding 直接判同 topic/event |
| A/B/C schema/evidence 失败 | 当前阶段 pending，保留前阶段 artifact | 部分采纳自由文本或写 event |
| 预算/token 超限 | 完整边界切 packet，或 pending/background/cold | 静默删 candidate、改成 unrelated |
| provider unavailable/timeout | 记录真实 attempt，整阶段 pending | 将 fallback 冒充 AI accepted |

### 11.6 K1 调试指标与验收门

每个 run 必须按 packet、candidate、stage request、validated output、pending、budget deferred、tokens、retry、latency 分栏：

| 指标 | 计算 | N/A/门槛 |
| --- | --- | --- |
| `packet_context_recall` | gold 所需 context units 进入至少一个合规 packet 的比例 | 无 context gold units 为 N/A；不以无候选冒充 0 |
| `distractor_rate` | packet 中无关 candidate units / 全部 candidate units，按 W0-W4、scope、overlap 分层 | 无 candidate units 为 N/A，并报绝对数 |
| input reconstruction | ledger/metadata/scope/order/reply/quote 保留率、episode 可审阅覆盖与 evidence 回链 | 保留与回链 100%；丢失/重复/静默改写为 0 |
| episode boundary/interaction | episode 边界和 context relation macro-F1、无 reply 承接 precision | macro-F1 ≥0.85；无 reply precision ≥0.90；仅 time/same-segment 强连 0 |
| topic optionality | 强制 topic/TopicThread 率、无主题/未知主题重放率 | 强制率 0；topic coverage 仅诊断 |
| media missingness | unavailable 显式缺失率、媒体占位/路径充当 semantic evidence 率 | 缺失率 100%；证据率 0 |
| view identity | today/yesterday/week across same thread stable ID consistency、calendar split/close rate | ID 一致 100%；因日历硬切 0 |
| model gate | 未 `accepted` 输入的 provider requests、accepted-only compliance | request 0；合规率 100% |
| claim/evidence | Stage B claim 槽位准确率、非未知 typed-evidence coverage、handle precision/recall | 只对有 gold evidence 的槽位评分，unknown 另报 |
| stage success | 各阶段 validated complete / started provider requests，另报 failed/pending/budget | 0 request 为 N/A；candidate 不作分母 |
| cost | 每阶段 request、tokens、retry、latency、CPU/embedding、每千消息成本 | provider 不可用时报告实际尝试和 0 output |

K1 最低门：权威 metadata/顺序/reply/quote/span 保留率 100%；packet scope 泄漏 0；未知边界/人物/对象/state 强填 0；仅 time/same-segment 建强边 0；沉默→`resolved` 0；非未知 Stage B 槽位 typed evidence 100%；A/B/C schema validation 100%；失败结果进入 pending 且可按 cue 重激活 100%。数值质量门须在 gold/holdout 基线后冻结，不能用 N/A、删难例或 57.8% `rules_v1` 基线替代。

### 11.7 分阶段实施与继续迭代规则

1. **K1-contract**：冻结 ContentLedger、ConversationEpisode prototype、ContextPacket v1、A/B/C schema、evidence、cache key、状态机和指标；只改文档/契约，不写生产代码。
2. **K1-local**：实现 message registry → episode builder → packet builder → W0-W4 candidate/reason/cue 的纯函数 synthetic replay；先通过 input reconstruction gate，验证多 packet、重叠包、unknown 边界、media missingness、三种字段分离和跨 chat hard block。
3. **K1-A**：仅在 input reconstruction `accepted` 后进入 development shadow 的可选 TopicThread mapping，检查 ID-only schema、topic optionality、packet recall、pending/reactivation 和预算。
4. **K1-B**：在 A 已验证 topic 上执行 per-topic claims；检查人物角色分离、object resolution、六值 state、claim/evidence 与 unknown。
5. **K1-C**：执行 consistency audit；检查冲突、漏项、错误合并、unknown 保留和不偷偷修正前阶段。
6. **K1-replay**：固定 input fingerprint、版本和 selected packet，重放所有阶段，输出成本+语义调试矩阵；随后才进入真实金标准和 holdout 评测。

任一最低门、zero-tolerance、schema/evidence、成本或可重激活门失败：冻结当前 artifact，不覆盖上一版；按错误码新增 synthetic regression；仅在 development 修复后用未见 gold/holdout 重跑。连续失败不得降级为 accepted，不得把任何 Stage A/B/C 结果直写 event/title 或生产 API。

### 11.8 生产状态

K1 的所有模块、接口、阶段输出和指标都属于 development/实验 contract。D9 真实评测、D10 replay/人工审计、D11 shadow API/DOM/screenshot E2E 未全部 accepted 前，D12 始终 `blocked`；首页、通知、现有 production tables 和发送路径保持不变。任何 provider 不兼容、strict schema/tool/json 不稳定或 evidence handle 不完整，必须保持 `provider_blocked`，不能用 fallback 伪装成模型成功。
