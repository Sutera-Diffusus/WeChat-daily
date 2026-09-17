# 2026-08-25 微信记录金标准落地契约

> 状态：第三版语义管线 P1/P1.1 的权威数据契约，2026-08-27 起纳入 Contextual Bundle 修订。
>
> 本文只规定如何在本地构建、脱敏、标注和验收私有金标准，不包含、引用或复述任何真实聊天原文。语义架构以 [design-review.md](D:/Project_Codex/Project_WeChatMoreFunction/docs/design-review.md) 为上位设计。

> 2026-08-31 路线修订：金标准按 `ContentLedger → ConversationEpisode（交流形态） → TopicThread（话题流，可选） → DeepRead` 组织。主题不是强制结果；输入重建必须先完成人工可审阅验收，才允许 DeepSeek 调用。本修订优先于早期主题分类/本地语义抽取措辞；Stage B/C、首页契约和 `production_blocked` 状态保持不变。

## 1. 目标、范围与禁止事项

目标是在 2026-08-25 本地微信记录上建立一份可重放、可仲裁、可冻结的金标准，用于评测：

```text
ContentLedger → ConversationEpisode / 交流形态
             → TopicThread / 话题流（可选）
             → DeepRead（DeepSeek；沿用 Stage B → Stage C）
             → optional event / presentation
```

本轮数据集只服务离线 A/B/C/D 对照、错误分析和未来影子模式准入。第 1 阶段先冻结人物、对象、状态和碎片化上下文，不扩展事件/标题/前端契约。它不授权数据库迁移、模型引入、生产 API 修改或首页切换。

禁止事项：

- 真实消息、脱敏后消息、人工标签、私有 manifest、标注导出和截图不得进入 Git；
- 不把真实消息粘贴到 issue、PR、提交信息、文档、测试快照或外部服务；
- 不把原始姓名、微信号、群名、手机号、邮箱、路径、URL token、订单号、账号、密钥或媒体文件写入公开 fixture；
- 不使用可反查的普通 SHA/MD5 直接假名化低熵标识；
- 不为满足样本配额伪造真实样本或篡改冻结测试标签；
- 不将真实数据发送给在线模型。后续若评估外部模型，必须另行获得数据边界授权。

本契约的上下文层原则：`speaker`、`mentioned_person`、`subject`、`object` 和 `state` 先于任何 event 判断；fragment/claim 可以没有 event。`information_value` 与 `event_completeness` 必须分别标注，不能以价值低过滤问候或以价值高代替事件完整性。

本契约同时遵守以下不可逆越界约束：原始消息不可变，适配器/消息注册表的会话、发言人、时间、回复和来源元数据是权威字段；语义派生物只能追加、版本化和重算，不能覆盖源消息或把正文推测写回元数据。设计原则是“可以延迟理解，但不能提前遗忘；可以暂不昂贵编码，但必须保留重新激活线索”。低成本 registry、四通道 `immediate|pending_context|background|cold_recoverable`、动态 `DialogueBundle`、`open_context_snapshot` 和 bundle 级固定 LLM 输出均属于本契约的可审计派生 artifact，不是删除消息的理由。

本文件只规定数据、证据、成本和评测契约；它不表示真实数据已经达到算法门槛，也不授权生产接入。Stage1 v1 人工审计的 57.8% 只作为 `rules_v1` 规则基线，明确不接生产、不替代冻结测试、不抵消零容忍项。

## 2. 本地目录与 Git 隔离

私有根目录固定为：

```text
data/private/gold_standard/2026-08-25/
  source/                 # 只读导出或本地查询快照
  working/                # 脱敏、抽样和标注中间产物
  releases/v1/            # 冻结的私有数据集
  audit/                  # 质检、分歧和隐私扫描报告
```

仓库 `.gitignore` 已忽略整个 `data/`。本契约另增加 `private_fixtures/`、`gold-standard-private/`、`tests/fixtures/private/`、`*.private.jsonl` 和 `*.private.ndjson` 作为纵深防护。

公开仓库只允许保存：schema/契约文档、完全合成的测试 fixture、无敏感小样本计数的汇总指标和不含原文的评分代码。任何准备加入 Git 的 fixture 都必须声明 `data_origin="synthetic"`；没有该声明视为私有。

公开汇总中的任一会话、人物、稀有标签或交叉分层计数小于 5 时统一显示 `<5`，不得发布可与时间、角色或错误类别组合后反推出单个对话的明细。

开始标注前执行只读校验：

```powershell
git check-ignore -v data/private/gold_standard/2026-08-25/working/messages.private.jsonl
git status --short --ignored data/private/gold_standard/2026-08-25/
```

第一条必须命中 `.gitignore`。如果未命中，停止导出和标注，不用 `git add -f` 绕过。

## 3. 隐私角色与密钥边界

- 数据保管人：能够访问原始消息，只负责本地抽取、密钥和销毁策略；
- 脱敏执行人：生成工作集并完成隐私扫描，可与保管人为同一本机用户；
- 标注员 A/B：只访问脱敏工作集；
- 仲裁员：只在分歧需要时访问脱敏前后文，不默认访问源数据库；
- 评测执行人：优先只访问冻结 release，不访问 source。

假名化密钥为数据集专用随机密钥，保存在仓库外或操作系统凭据存储中。manifest 只保存 `pseudonym_key_id`，不保存密钥、密钥派生材料或明文映射表。明文映射表如确有必要，单独放在 `source/identity-map.private.json`，不得复制到 release。

## 4. 脱敏规则

### 4.1 稳定假名

账号、会话、人物、消息源 ID 使用带数据集专用密钥的 HMAC 产生本地稳定 token，再映射为顺序别名：

```text
ACCOUNT_001  CHAT_001  PERSON_001  MESSAGE_000001
ENTITY_001   URL_001   FILE_001
```

同一真实对象在同一数据集内保持一致；不同数据集版本默认复用同一 `pseudonym_key_id` 以支持 diff。对外分享或创建公开合成 fixture 时必须重新映射，避免跨数据集关联。

### 4.2 文本替换

按最长匹配优先替换，并记录替换类型但不记录原值：

| 类型 | 替换形式 | 规则 |
| --- | --- | --- |
| 人名、昵称、群名、公司内部名 | `[PERSON_n]`、`[CHAT_n]`、`[ORG_n]` | 同一实体保持一致；不保留罕见别称 |
| 微信号、账号、用户 ID | `[ACCOUNT_n]` | 包括文本中转述的 ID |
| 手机、座机、邮箱 | `[PHONE_n]`、`[EMAIL_n]` | 保留“手机号/邮箱”类型，不保留局部字符 |
| 身份证、地址、精确位置 | `[GOV_ID_n]`、`[ADDRESS_n]` | 城市仅在分析必要且经复核时保留到城市级 |
| URL | `[URL_n:DOMAIN_CLASS]` | 只保留人工定义的域类别；删除 path、query、fragment 和 token |
| 文件名、Windows 路径、媒体信息 | `[FILE_n:TYPE]` | 不保留用户名、目录、MD5 或原文件名 |
| 订单、交易、票据、设备号 | `[REFERENCE_n:TYPE]` | 数值仅按语义需要分桶 |
| API key、cookie、验证码、密码 | `[SECRET_REDACTED]` | 一律删除，禁止稳定映射 |
| 金额、精确数量 | `[AMOUNT:BUCKET]`、`[COUNT:BUCKET]` | 若金额差异是事件核心，保留预定义数量级桶 |

脱敏不得抹掉事件身份所需的语义类别。例如可以保留“邮箱收不到验证信息”这种结构，但邮箱地址必须替换；可以保留规范化产品类别，但私人账号和可定位链接必须替换。

### 4.3 时间与顺序

- `local_day` 固定为 `2026-08-25`；
- release 不保留绝对秒级时间，保存相对当日首条样本的 `time_offset_seconds` 与 `time_bucket`；
- 保留同会话内顺序和真实时间间隔，以评测回复链、长间隔和事件边界；
- 对外汇总只使用足够大的时间桶，避免稀有活动时间反向识别个人。

### 4.4 回复、引用、链接和媒体

- `reply_to_message_id` 只指向脱敏 `message_id`；引用文本在脱敏后才进入 release；
- 链接只保留 `URL_n` 与允许的域类别，不保留完整 URL；
- 图片、语音、文件和表情二进制不进入金标准 release；只保留媒体类型、是否有人工脱敏转写和占位符；
- OCR/转写若包含真实信息，适用同一套文本脱敏规则并单独复核。

媒体记录必须另有 `availability=available|unavailable|not_present|unknown` 和 `missing_reason`（不可用时必填）。`unavailable` 占位、文件路径、失败的 OCR/ASR 和媒体类型都不是语义证据；只有独立、人工核验的脱敏 transcript 才能按文本 evidence 登记。

### 4.5 脱敏质检

每次 release 前至少完成：规则扫描、人工抽检和秘密信息扫描。发现一条高危明文即阻止冻结。扫描报告只记录文件、行号、敏感类型和处理状态，不复制命中文本。

## 5. 抽样设计

抽样分为两个互不替代的层次：代表性覆盖样本和高风险难例增补。所有入选与排除都写入私有 manifest，不因消息“看起来没价值”而删除其证据角色。

### 5.1 代表性覆盖层

先按以下维度分层，再在层内确定性随机抽样；随机种子写入 manifest：

- 会话类型：私聊、群聊、文件助手/本人、系统；
- 方向：入站、本人发出、系统；
- 内容类型：文本、链接、媒体占位、回复/引用；
- 活跃度：低密度、中密度、高密度时间窗；
- 文本长度：短、中、长；
- 信息角色：事实陈述、观点、问题、请求、确认/反应、conversation_opener、上下文不足；
- 上下文状态：对象显式/继承/未知，state 开放/终态/未知，事件起止显式/未知；
- 时间位置：当日早/中/晚与跨长间隔回复。

### 5.2 难例增补层

定向加入以下行为切片：

- 同一产品或父主题下的不同问题；
- 相同关键词但不同核心对象、动作、状态或诉求；
- 同一事件的进展、恢复、反复发生和历史转述；
- 私聊与群聊之间的转述、引用和观点冲突；
- 长时间后显式回复、短句回指、指代不清；
- 无显式 reply 但语义上回答问题的消息；
- 同一 message 的问候 + 实质问题，只有 opener 或没有可识别头尾的片段；
- 人物观点与事实陈述易混淆；
- 应是 `related_event` 或 `same_topic_only` 而不是 `same_event` 的候选；
- legacy 已知误合并或前端显示不一致，但只记录错误类别和私有 ID，不在公开文档写原文。

### 5.3 起始规模与短缺处理

首个可评测 release 目标为：

- 至少 400 条完成脱敏和 message 审核的消息；
- 至少 200 条双人标注的 claim；
- 至少 240 个双人标注的候选关系对；
- 关系对中至少 120 个 hard negative，且五分类每类原则上不少于 20 个；
- 至少 40 个仲裁后的事件 cluster；
- 至少 30 个双人审核的 presentation 卡片或明确的“不应展示”判定。

如果当日真实记录不足以满足某类别，不得伪造或跨日偷偷补齐。manifest 记录 `coverage_shortfalls`，该 release 标为 `pilot_limited`；不足类别可在后续日期扩展集补充，但必须使用新的 dataset scope/version。

## 6. 文件布局与 JSONL 通用规则

私有 release 使用 UTF-8、每行一个 JSON 对象、键名 snake_case。数组顺序有语义时保持稳定，否则按 ID 排序。禁止 `NaN`、注释和尾随逗号。

```text
releases/v1/
  manifest.json
  messages.private.jsonl
  message_registry.private.jsonl
  gate_decisions.private.jsonl
  fragments.private.jsonl
  persons.private.jsonl
  arguments.private.jsonl
  mentions.private.jsonl
  claims.private.jsonl
  discourse_threads.private.jsonl
  context_relations.private.jsonl
  dialogue_bundles.private.jsonl
  open_context_snapshots.private.jsonl
  bundle_llm_decisions.private.jsonl
  relations.private.jsonl
  clusters.private.jsonl
  presentations.private.jsonl
  adjudications.private.jsonl
```

所有对象通用字段：

```json
{
  "schema_version": "gold_semantic_v1",
  "context_schema_version": "dialogue_context_v1",
  "dataset_version": "wechat-2026-08-25-v1",
  "record_id": "TYPE_000001",
  "annotation_status": "draft|double_annotated|adjudicated|frozen",
  "provenance": {
    "source_record_ids": ["..."],
    "created_by": "pipeline|ANN_A|ANN_B|ADJ_1",
    "guide_version": "annotation-guide-v1",
    "revision": 1
  }
}
```

以下 schema 是字段契约，不是包含真实内容的样例。带 `?` 的字段允许 `null`，但不得省略。

## 7. `messages.private.jsonl`

```text
message_id: string
account_id: string
chat_id: string
chat_type: direct|group|filehelper|system
speaker_id: string|unknown
direction: inbound|outbound|system
message_type: text|link|image|audio|file|sticker|system|other
local_day: YYYY-MM-DD
time_offset_seconds: integer >= 0
time_bucket: early|morning|afternoon|evening|late
sequence_in_chat: integer >= 0
reply_to_message_id: string?
redacted_text: string
redaction_types: string[]
media_state: none|placeholder|redacted_transcript|unavailable
source_mode: live|history|recovered|unknown
context_message_ids: string[]
split: development|frozen_test
```

约束：`redacted_text` 是唯一允许进入后续标注的正文；不得保留 `raw_text`、原始 ID、绝对路径或原始媒体引用。`context_message_ids` 只能引用同一 release 中的脱敏消息。

源消息在 source/working 层仍不可变；release 中的脱敏 `redacted_text` 是标注输入，不代表可以覆盖源正文。`account_id`、`chat_id`、`chat_type`、`speaker_id`、`direction`、消息时间、顺序、reply 和 `source_mode` 以消息元数据为权威；正文自称、转述和模型推断只能进入 fragment/claim 槽位，不能改写这些字段。元数据冲突必须保留 revision 和 `unknown` 影响范围。

## 7.0 `message_registry.private.jsonl`

registry 是低成本、追加式的消息路由索引，不是第二份正文，也不承担语义真值。每个 release 的每条 message 都必须有一条 registry 记录：

```text
registry_id: string
message_id: string
account_id: string
chat_id: string
chat_type: direct|group|filehelper|system
speaker_id: string|unknown
direction: inbound|outbound|system
reply_to_message_id: string?
source_mode: live|history|recovered|unknown
adapter_version: string
sequence_in_chat: integer
event_time_offset_seconds: integer|unknown
event_time_precision: exact|bucket|unknown
message_type: text|link|image|audio|file|sticker|system|other
body_digest: string
body_length_estimate: integer|unknown
language_hint: string|unknown
metadata_fingerprint: string
metadata_revision: integer
registry_state: registered|gated|replayed|error
gate_channel: immediate|pending_context|background|cold_recoverable
gate_reason_codes: string[]
activation_cues: string[]
artifact_refs: {fragment_ids: string[], bundle_ids: string[], snapshot_ids: string[]}
input_fingerprint: string
schema_version/pipeline_version: string
created_at: string
```

registry 不得保存未脱敏正文；`body_digest` 只用于完整性和幂等。`register → get → record_gate_decision` 必须可重放，重复导入不能产生第二条逻辑消息。source 元数据冲突不得由 registry 静默覆盖，必要时追加 metadata revision 并将语义槽位置为 `unknown`。

## 7.0.1 `gate_decisions.private.jsonl`

```text
gate_decision_id: string
message_id: string
from_channel: registered|immediate|pending_context|background|cold_recoverable
to_channel: immediate|pending_context|background|cold_recoverable
trigger_codes: string[]
reason_codes: string[]
budget_class: immediate|deferred|recovery
activation_cues: string[]
reversible: true
artifact_refs: string[]
input_fingerprint: string
analysis_run_id: string
pipeline_version: string
created_at: string
```

四个通道只表示成本和调度，不表示 event label 或 state terminal。预算失败、服务失败和暂时不理解都必须保留为可恢复状态；`cold_recoverable` 不能被统计为 `unrelated`。

## 7.1 `fragments.private.jsonl`

fragment 是 message 内最小、可独立参与对话理解的连续片段；一个 message 可以拆成多个 fragment，fragment 也可以没有 claim。

```text
fragment_id: string
message_id: string
span_start: integer
span_end: integer
fragment_text_redacted: string
fragment_type: conversation_opener|statement|question|request|answer|acknowledgement|reaction|context|media|unknown
speaker_id: string|unknown
mentioned_person_ids: string[]       # unresolved person uses "unknown"
subject_id: string|unknown
subject_type: person|group|organization|object|unknown
object_id: string|unknown
object_resolution: explicit|inherited|unknown
object_evidence_refs: [{type: mention|fragment|claim, id: string, span: {start: integer, end: integer}?}]
object_inherited_from_id: string?
state: unknown|planned|ongoing|resolved|failed|cancelled
state_evidence: explicit|inherited|unknown
closure_reason: resolved|failed|cancelled|superseded|unknown
temporal_qualifier: current|historical|unknown
start_time_offset_seconds: integer|unknown
start_time_source: explicit|inherited|unknown
end_time_offset_seconds: integer|unknown
end_time_source: explicit|inherited|unknown
information_value: none|low|medium|high|unknown
event_completeness: not_applicable|partial|sufficient|unknown
claim_ids: string[]
evidence_refs: [{type: message|mention|fragment|claim, id: string, span: {start: integer, end: integer}?}]
context_message_ids: string[]
uncertainties: string[]
```

`speaker_id`、`mentioned_person_ids` 和 `subject_id` 是不同角色：speaker 是消息发送者，mentioned_person 是被提及/引用/转述的人，subject 是片段正在陈述其动作或状态的主体；不得自动互换。`object_resolution=explicit` 必须有本片段 span；`inherited` 必须填写来源 ID 并由上下文关系支持；无可靠对象证据时保留 `unknown`，不可从父主题或关键词补齐。

`conversation_opener`（例如礼貌性问候或重新接话）必须保留证据，即使 `information_value=low`；若同一 message 同时含有实质问题或请求，必须拆出另一个 fragment。`start_*`、`end_*` 为 `unknown` 是正常状态，消息时间不自动成为语义起止时间。

状态生命周期第一阶段固定六值 `unknown|planned|ongoing|resolved|failed|cancelled`。旧 `planned` 直接映射为 `planned`，旧 `historical` 进入 `temporal_qualifier=historical` 而不改变 state；旧 `reported` 没有状态证据时映射为 `unknown`。被替代时使用 `state=cancelled, closure_reason=superseded`，且仍需明确替代证据。终态（`resolved|failed|cancelled`）只由同一对象/实例的明确完成、恢复、失败或取消证据触发。最后一条消息、没有后续消息、topic shift、未回复和沉默均不得写成 `resolved`；没有终态证据时保持 `unknown|planned|ongoing`。

## 7.2 `persons.private.jsonl`

人物角色单独建表，避免把 speaker、被提及的人和主语压成一个 participant：

```text
person_ref_id: string
person_id: string|unknown
resolution: explicit|inherited|unknown
role: speaker|mentioned_person|subject
message_id: string
fragment_id: string
claim_id: string?
span_start: integer?
span_end: integer?
surface_redacted: string?
source: message_metadata|text|reply_context|unknown
evidence_refs: [{type: message|mention|fragment|claim, id: string, span: {start: integer, end: integer}?}]
confidence: high|medium|low
```

speaker 可以来自消息元数据而没有正文 span；mentioned_person 必须有正文、引用或转述证据；subject 可为人物或非人物对象，非人物 subject 由 `arguments.private.jsonl` 表达。三种角色不能静默互换，未知角色必须保留 `unknown`。

## 7.3 `arguments.private.jsonl`

argument 保存 fragment/claim 中的语义角色槽位，先于 event 标注：

```text
argument_id: string
fragment_id: string
claim_id: string?
role: subject|object|agent|recipient|source|target|unknown
entity_id: string|unknown
entity_type: person|object|group|organization|unknown
resolution: explicit|inherited|unknown
evidence_refs: [{type: message|mention|fragment|claim, id: string, span: {start: integer, end: integer}?}]
inherited_from_id: string?
confidence: high|medium|low
```

对象参数必须与 fragment/claim 上的 `object_resolution=explicit|inherited|unknown` 一致：explicit 有本片段 span，inherited 有来源 argument/fragment/claim 与 context relation，缺证据为 unknown。没有对象不是错误，也不能用主题词补齐。

## 7.4 `discourse_threads.private.jsonl`

discourse_thread 是由 fragment/claim 和上下文关系组成的局部话语链，不等于 event；它可以开放、只有一端、没有 opener 或没有可识别事件。

```text
discourse_thread_id: string
fragment_ids: string[]
claim_ids: string[]
conversation_opener_fragment_ids: string[]
speaker_ids: string[]
mentioned_person_ids: string[]
subject_ids: string[]
object_refs: [{id: string, resolution: explicit|inherited|unknown, source_id: string?}]
state_sequence: string[]             # each state is one of six canonical values
closure_reason: resolved|failed|cancelled|superseded|unknown
start_fragment_id: string|unknown
end_fragment_id: string|unknown
start_time_offset_seconds: integer|unknown
start_time_source: explicit|inherited|unknown
end_time_offset_seconds: integer|unknown
end_time_source: explicit|inherited|unknown
information_value: none|low|medium|high|unknown
event_completeness: not_applicable|partial|sufficient|unknown
event_candidate: yes|no|unknown
context_relation_ids: string[]
evidence_refs: [{type: message|mention|fragment|claim, id: string, span: {start: integer, end: integer}?}]
uncertainties: string[]
```

线程可从采集窗口中间开始，也可没有收尾；`start_fragment_id`、`end_fragment_id` 或相应时间为 `unknown` 不视为标注缺陷。只有在人物/对象/状态和关系证据足够时才提出 event candidate；线程的 `event_completeness` 不得由 `information_value` 推导。

## 7.5 `dialogue_bundles.private.jsonl`

`DialogueBundle` 是可重建的分析上下文视图，不是永久 event cluster。它允许同一 fragment/claim 进入多个 bundle；`primary_bundle_id`（如存在）只是调度/展示字段，不能抹掉其他证据归属。

```text
bundle_id: string
analysis_run_id: string
schema_version: string
pipeline_version: string
anchor_fragment_ids: string[]
anchor_claim_ids: string[]
candidate_window_id: string
window_scale: W0|W1|W2|W3|W4
member_message_ids: string[]
member_fragment_ids: string[]
member_claim_ids: string[]
member_bundle_ids: string[]
open_context_snapshot_id: string?
speaker_refs: string[]
mentioned_person_refs: string[]
subject_refs: string[]
object_refs: [{id: string, resolution: explicit|inherited|unknown, source_id: string?}]
state_refs: string[]
unresolved_slot_codes: string[]
candidate_pair_ids: string[]
context_relation_ids: string[]
chat_scope: {account_id: string, chat_ids: string[]}
cross_chat_bridge_refs: string[]
evidence_refs: [{type: message|mention|fragment|claim, id: string, span: {start: integer, end: integer}?}]
budget_class: immediate|deferred|recovery
gate_channel: immediate|pending_context|background|cold_recoverable
status: open|provisional|abstained|materialized|superseded
uncertainties: string[]
provenance: object
```

候选窗口必须保留层级：`W0` 同 message，`W1` 同 chat 局部前后文（可跨 opener、ack、context-only 或插入句），`W2` 同 chat 会话/日级，`W3` 长间隔稀疏召回，`W4` 仅显式 bridge 的跨 chat 候选。不能只按相邻 fragment `zip`，也不能以 `same_segment` 或时间单独建立强关系。跨 chat 默认隔离；跨 chat `same_event` 需 bridge、对象/实例兼容和正证据，并默认列入人工仲裁。

## 7.6 `open_context_snapshots.private.jsonl`

```text
snapshot_id: string
captured_at: string
analysis_run_id: string
open_thread_ids: string[]
unresolved_slots: [{thread_id: string, slot: string, value: string|unknown, resolution: explicit|inherited|unknown, evidence_refs: string[]}]
recent_fragment_ids: string[]
recent_claim_ids: string[]
pending_relation_candidates: string[]
activation_cues: string[]
excluded_candidate_reasons: string[]
snapshot_version: string
provenance: object
```

快照只表示捕获时仍开放的线程和线索，不表示关闭或 `resolved`。新消息应生成新快照并保留旧快照；沉默、topic shift、窗口截断和未回复不能从快照中推导终态。

## 7.7 `bundle_llm_decisions.private.jsonl`

LLM（如进入实验）按 bundle 调用，bundle 内只提供有限候选对；输出固定 JSON，不允许自由文本直接写入 event、标题或状态：

```text
decision_id: string
bundle_id: string
schema_version: bundle_llm_v1
abstain: boolean
slot_updates: [{slot: subject|object|state|time|intent, value: string|unknown, resolution: explicit|inherited|unknown, evidence_refs: string[], confidence: high|medium|low}]
context_relations: [{left_anchor_id: string, right_anchor_id: string, label: continues|elaborates|answers|contrasts|topic_shift|possibly_related|insufficient, evidence_refs: string[], evidence_strength: strong|medium|weak|none}]
pairwise_decisions: [{candidate_pair_id: string, label: same_event|related_event|same_topic_only|unrelated|insufficient_context, evidence_refs: string[], confidence: high|medium|low}]
unresolved_slot_codes: string[]
model_id: string
model_version: string
prompt_version: string
input_tokens: integer
output_tokens: integer
validation_status: valid|invalid|timeout|unavailable
fallback_action: accepted|abstained|pending_context|insufficient|sparse_only
evidence_refs: string[]
created_at: string
```

`abstain=true`、`unknown` 和 `insufficient` 是合法标注；非未知槽位必须有 typed evidence。embedding 只能用于候选召回，LLM 只能 pairwise 裁决，规则只用于元数据、scope/硬约束、schema/证据校验和 fallback。LLM schema 无效或超时必须完整回退，不得部分采纳自由文本。

## 8. `mentions.private.jsonl`

```text
mention_id: string
message_id: string
fragment_id: string
mention_type: entity|event_trigger|action|time|state|intent|quantity|other
span_start: integer
span_end: integer
surface_redacted: string
normalized_id: string?
normalized_type: string?
entity_role: speaker|mentioned_person|subject|object|other|unknown
attributes: object
certainty: asserted|reported|hypothetical|negated|unknown
annotator_notes: string?
```

偏移量基于 Unicode code point，半开区间 `[span_start, span_end)`，且必须精确切出 `surface_redacted`。同一片段可有多个不同类型 mention，但重复同类型 mention 必须合并。

## 9. `claims.private.jsonl`

```text
claim_id: string
message_id: string
fragment_id: string
speaker_id: string|unknown
mentioned_person_ids: string[]
subject_id: string|unknown
subject_type: person|group|organization|object|unknown
object_id: string|unknown
object_resolution: explicit|inherited|unknown
object_evidence_refs: [{type: mention|fragment|claim, id: string, span: {start: integer, end: integer}?}]
object_inherited_from_id: string?
claim_type: fact|opinion|question|suggestion|hypothesis
claim_text_redacted: string
target_entity_ids: string[]
event_mention_ids: string[]
evidence_spans: [{start: integer, end: integer}]
stance: support|oppose|neutral|mixed|unknown
polarity: positive|negative|neutral|mixed|unknown
modality: certain|probable|possible|required|desired|unknown
status: reported|ongoing|resolved|failed|planned|historical|unknown
state: unknown|planned|ongoing|resolved|failed|cancelled
state_evidence: explicit|inherited|unknown
closure_reason: resolved|failed|cancelled|superseded|unknown
temporal_qualifier: current|historical|unknown
start_time_offset_seconds: integer|unknown
start_time_source: explicit|inherited|unknown
end_time_offset_seconds: integer|unknown
end_time_source: explicit|inherited|unknown
information_value: none|low|medium|high|unknown
event_completeness: not_applicable|partial|sufficient|unknown
attribution: direct|reply|quote|forwarded|inferred_context
timestamp_message_id: string
context_message_ids: string[]
```

一条消息可产生多条 claim。问题、观点和建议不得被改写为事实。说话人未知时保留 `unknown`，不删除主语后生成无主体结论。`claim_text_redacted` 只能做最小解上下文，不得加入原证据没有的新信息。`status` 是兼容字段，生命周期判断以 `state`、`state_evidence` 和同一实例的终态证据为准；沉默或缺少后续消息不能写成 `resolved`。`information_value` 与 `event_completeness` 独立标注，低价值不等于不完整，高价值不等于完整事件。

## 10. `relations.private.jsonl`

```text
relation_id: string
left_anchor_id: string
right_anchor_id: string
anchor_type: mention|claim|event_seed
label: same_event|related_event|same_topic_only|unrelated|insufficient_context
supporting_slot_codes: string[]
conflicting_slot_codes: string[]
evidence_message_ids: string[]
must_not_link: boolean
must_not_link_reason_codes: string[]
confidence: high|medium|low
annotator_a_label: string
annotator_b_label: string
adjudication_id: string?
```

关系对使用规范顺序：较小 ID 在左。同一对只能有一条 frozen 记录。`related_event` 和 `same_topic_only` 只建立关系边，绝不能因后处理被当成 `same_event`。

### 10.1 `context_relations.private.jsonl`

上下文关系与 `relations.private.jsonl` 的事件五分类分开保存。它们组织 fragment/claim 的话语连续性，不创建或合并 event。

```text
context_relation_id: string
left_anchor_id: string
right_anchor_id: string
anchor_type: fragment|claim
label: continues|elaborates|answers|contrasts|topic_shift|possibly_related|insufficient
supporting_slot_codes: string[]
conflicting_slot_codes: string[]
evidence_refs: [{type: message|mention|fragment|claim, id: string, span: {start: integer, end: integer}?}]
evidence_message_ids: string[]
explicit_reply_present: boolean
time_distance_seconds: number?
time_evidence: strong|weak|none
evidence_strength: strong|medium|weak|none
confidence: high|medium|low
annotator_a_label: string
annotator_b_label: string
adjudication_id: string?
provenance: object
```

关系对使用规范顺序：较小 ID 在左，同一对在同一关系层只能有一条 frozen 记录。`explicit_reply_present=true` 是强证据但不是必需条件；没有 reply 时，至少需要两个相互独立的语义信号（指代、对象继承、问题—答复语义、主体/状态延续等）才可标为 `continues|elaborates|answers`。有 reply 也不能覆盖明确的对象/状态冲突。`time_distance_seconds` 只能生成 `time_evidence=weak`，不能单独创建关系或继承对象/状态；长间隔也不自动切断内容承接。`topic_shift` 只说明话语转向，不关闭旧 thread；`insufficient` 代表证据不足，不得自动升级为其他关系。

旧 shadow 实现中的 `continuation`、`question_answer`、`contrast` 仅作为输入兼容别名，导出 release 时必须规范化为 `continues`、`answers`、`contrasts`；`reply`、`object_inheritance`、`state_transition` 只能写入 supporting signals，不得写成 canonical label。

`evidence_strength` 与 label 分开记录：`strong` 可来自显式 reply/引用或明确对象继承，`medium` 通常要求两个独立语义信号，`weak` 只表示单一线索（时间邻近也只能是 weak），`none` 只用于 `insufficient`。强度不能替代 typed evidence 或升级 event。

### 10.2 第三版 `same_event` 可观察性契约

第三版起，`label: same_event` 必须携带非空 `observable_support_refs`。每条
ref 使用显式 typed 形式 `{type: message|mention|claim, id: string,
support_code: string, side?: left|right|shared, span?: {start, end}}`，并且：

- `support_code` 必须对应 `supporting_slot_codes` 中的理由码；
- `mention` ref 的 span 必须落在该 mention，`claim` ref 的 span 必须落在
  `evidence_spans`；
- ref 必须属于关系两端或其 `evidence_message_ids`，不得只写标注员能理解的
  理由、同 block、时间接近或主题相似；
- 两端还必须有输入可读取的稳定实例信号：共享 instance 字段、明确回复边或
  同一 message；明确回复是强证据，但不是唯一或必需条件。共享动作/对象本身只是
  证据，不等同于事件实例身份。

缺少可观察身份时，不能保留 `same_event`；应在新的 versioned artifact 中降为
`related_event` 或 `insufficient_context`，保留原 relation/evidence/adjudication
provenance，并重建 cluster、presentation 和 MNL 外键。

## 11. must-not-link 契约

must-not-link（MNL）是防止灾难性错误合并的硬约束。以下任一明确冲突原则上设置 `must_not_link=true`：

- `CORE_OBJECT_CONFLICT`：核心产品、账号、平台、网站或流程对象不同；
- `ACTION_TYPE_CONFLICT`：重置、注册、计费、封禁、登录、故障、价格咨询等动作不同；
- `INTENT_CONFLICT`：报告问题、求助、比较成本、提出建议、表达风险不同；
- `TEMPORAL_INSTANCE_CONFLICT`：可以确认是不同发生实例，而不是同一事件进展；
- `STATUS_CONTRADICTION_WITHOUT_SHARED_INSTANCE`：状态冲突且没有证据表明在描述同一实例；
- `ATTRIBUTION_CONFLICT`：原始经历、转述和他人观点被错误当作同一主张；
- `TOPIC_ONLY_EVIDENCE`：唯一共同点是父主题或宽泛关键词；
- `PRESENTATION_ONLY_SIMILARITY`：仅标题/摘要相似，底层对象没有共指证据。

显式回复、同一具体链接或相同错误码可以作为强证据，但不是上下文或事件关系的必需条件，也不能自动取消 MNL。只有仲裁员在确认两端确指同一现实事件时可以覆盖，且必须写 `override_reason`、证据 ID 和 `adjudication_id`。任何 frozen cluster 内存在未覆盖的 MNL 对，数据集校验必须失败。

## 12. `clusters.private.jsonl`

`cluster` 是金标准中的事件分组，不等同于主题族。其 schema：

```text
cluster_id: string
cluster_type: event|non_event_context|insufficient_context
event_type: string|unknown
core_entity_ids: string[]
action_types: string[]
intent_types: string[]
state_sequence: string[]
mention_ids: string[]
claim_ids: string[]
member_message_ids: string[]
discourse_thread_ids: string[]
relation_ids: string[]
must_not_link_checked: boolean
start_message_id: string|unknown
end_message_id: string|unknown
start_time_offset_seconds: integer|unknown
end_time_offset_seconds: integer|unknown
topic_family_ids: string[]
summary_of_boundary_redacted: string
uncertainties: string[]
```

cluster 由仲裁后的 `same_event` 连通关系和人工边界共同确定，不能直接采用 legacy cluster ID。每个成员必须有证据路径；跨会话 cluster 必须至少有一条显式强证据或仲裁记录。`start_*`/`end_*` 是可观察边界，不是“首尾消息即事件起止”的推断；截断窗口或开放线程可以为 `unknown`，沉默不能补写终态。

## 13. `presentations.private.jsonl`

```text
presentation_id: string
presentation_type: event_card|topic_observation|trend_item|do_not_display
source_cluster_ids: string[]
source_claim_ids: string[]
title_redacted: string?
sentence_units: [{text_redacted: string, claim_ids: string[], message_ids: string[]}]
participant_ids: string[]
fact_claim_ids: string[]
opinion_claim_ids: string[]
question_claim_ids: string[]
status: string|unknown
uncertainties: string[]
detail_policy: evidence_only|summary_and_evidence|hidden
expected_order_group: integer?
must_remain_separate_from: string[]
display_decision_reason_codes: string[]
```

每个可见句至少绑定一个 claim 和一个 message。`lead`、排序和样式不改变 `cluster_id`。`must_remain_separate_from` 用于端到端检查页面没有把两个正确卡片再次合并。`do_not_display` 仍保留来源和原因，用于评测过滤是否可逆。

## 14. 标注说明

### 14.1 标注顺序

标注员严格按以下顺序工作，避免先看标题或事件边界再倒推上下文：

1. 审核不可变脱敏 message、权威 metadata、registry、回复链和必要前后文；
2. 核对 gate channel 与 activation cue；路由不作为语义标签，也不删除 cold/pending 证据；
3. 切分 fragment，先标 speaker、mentioned_person、subject、conversation_opener 和证据 span；
4. 标 mention 及规范化对象、动作、状态、时间和诉求；对象逐项标 `explicit|inherited|unknown`；
5. 标 claim、类型、情态、state 生命周期、价值、事件完整性和证据片段；
6. 按多尺度窗口建立 DialogueBundle，允许 fragment/claim 多 bundle 归属，并记录 `open_context_snapshot`；
7. 独立判断 fragment/claim 的七类上下文关系；无显式 reply 仍可承接，但须具备独立语义证据；
8. 独立判断候选关系的五分类与 MNL；embedding、时间和 same-segment 只能作为召回/弱证据；
9. 在关系仲裁后形成 discourse_thread，再形成 cluster；不以沉默关闭线程；
10. 最后标 presentation，不得用编辑标题改写底层边界。LLM 决策（如有）只能按固定 bundle schema 复核，不能替代人工金标准。

### 14.2 五分类决策

- `same_event`：两端指向同一现实事件实例；核心对象与动作兼容，时间/状态可形成同一进展链，并有正证据；
- `related_event`：事件不同，但存在因果、前后置、子事件或明确业务关系；
- `same_topic_only`：只共享长期主题、产品类别或讨论领域；
- `unrelated`：没有足够的事件或主题关系；
- `insufficient_context`：证据不足，不能可靠选择前四类。不得把它当作低置信度 `same_event`。

先检查 MNL，再寻找正证据。关键词、向量相似、同一说话人、时间接近或同一群聊都不能单独支持 `same_event`。

### 14.3 归因和不确定性

- 原说话人与转述对象分别标注；引用内容不自动归给当前发送者；`speaker`、`mentioned_person`、`subject` 三种角色不可互换；
- 事实、观点、问题、建议和假设严格分开；
- 矛盾观点可属于同一事件，但保持不同 claim 和 stance；
- 指代不明时使用 `unknown`/`insufficient_context`，不根据常识补全；对象继承必须有来源 fragment/claim 和关系证据；
- `conversation_opener` 保留为 fragment；问候价值低不构成删除理由，问候与实质问题同消息时拆分；
- 状态没有明确终态证据时保持 `unknown|planned|ongoing`；沉默、未回复和 topic shift 不能标为 `resolved`；旧 `historical` 仅写入 `temporal_qualifier`。
- 事件起止没有证据时标 `unknown`；首尾采集消息仅是窗口边界，不是语义头尾；
- 标注员备注只说明边界判断，不复制被删除的敏感信息。

### 14.4 上下文关系决策

- `continues`：主体、对象、状态或未完诉求延续；`elaborates`：补充细节或证据；`answers`：回答前一问题/请求；
- `contrasts`：对照或冲突表述；`topic_shift`：转向新主体、对象、状态或诉求，但不关闭旧 thread；
- `possibly_related`：只有弱关联线索；`insufficient`：证据不足，不能可靠选择前六类；
- `reply_to_message_id` 只提供强证据，不是上述关系的必需条件；无 reply 也可用语义连续性建立关系，有 reply 也不能覆盖对象/状态冲突。

## 15. 双人标注与仲裁

1. 抽样与脱敏完成后，A/B 获得相同的 message/context 和候选对，但看不到对方标签、legacy 聚类、AI 标题或当前首页结果。
2. A/B 独立完成 fragment、人物角色、对象解析、state、mention、claim、context relation、event relation/MNL 和 presentation。discourse_thread/cluster 在关系初标后分别生成候选边界。
3. 自动比较器只报告结构差异和 ID，不把一方答案覆盖另一方。
4. 以下情况必须仲裁：上下文关系标签不同；关系标签不同；任一方标 MNL；speaker/mentioned_person/subject/target 不同；object resolution 不同；state 或终态判定不同；claim 类型在 fact 与非 fact 间不同；thread/cluster 边界不同；presentation 合并/拆分或是否展示不同。
5. 仲裁员先看双方理由与证据 ID，再看脱敏上下文；裁决写入 `adjudications.private.jsonl`，保存双方原值、最终值、理由码、证据 ID、guide version 和时间。
6. 若争议源自指南缺口，先更新指南版本，再让 A/B 对受影响切片盲重标；不得只在单条上临时创造规则。
7. 冻结前报告原始一致率和仲裁后结果。推荐统计上下文关系与事件关系 Cohen's kappa、fragment/mention span F1、角色与对象解析一致率、claim 三元组一致率、state 终态一致率、MNL 一致率和 cluster B-Cubed；这些用于判断数据质量，不用于证明模型性能。

标注员身份使用 `ANN_A/ANN_B/ADJ_1` 等本地角色 ID，公开汇总不记录真实姓名。

## 16. split、版本与 manifest

### 16.1 数据切分

- `development` 用于规则编写、阈值选择和错误分析；
- `frozen_test` 只用于里程碑评测，不得查看原文后定向改规则；
- 同一事件、同一 discourse_thread、显式或隐式连续关系链及其近邻上下文不得跨 split；
- 先按仲裁后的 cluster 分组，再按会话/难例层次切分，避免证据泄漏；
- frozen test 一经发布，只能新增勘误版本，不能静默改标签。

### 16.2 `manifest.json`

manifest 至少包含：

```text
dataset_id
dataset_version
status: draft|pilot_limited|adjudicated|frozen|superseded
scope_local_day
schema_version
context_schema_version
annotation_guide_version
redaction_policy_version
pseudonym_key_id
sampling_seed
sampling_strata_and_targets
coverage_counts
coverage_shortfalls
split_policy
source_snapshot_fingerprint_hmac
file_sha256
record_counts_by_file
label_counts
context_label_counts
object_resolution_counts
state_counts
value_completeness_counts
unknown_boundary_counts
evidence_coverage
gate_channel_counts
bundle_window_scale_counts
bundle_membership_counts
llm_schema_validation_counts
cost_budget_defaults_and_actuals
latency_percentiles
must_not_link_count
double_annotation_coverage
adjudication_count
privacy_scan_status
created_at
frozen_at
supersedes
known_limitations
```

`file_sha256` 对 release 中每个文件单独计算，用于完整性，不用于假名化。`source_snapshot_fingerprint_hmac` 只证明源快照一致，不暴露数据库路径或原始 ID。

版本规则：

- 文本脱敏、抽样、标签或 split 变化提升 dataset version；
- 只改公开说明而不改私有数据，不提升 dataset version；
- schema 不兼容变化提升 `schema_version`；
- 指南变化提升 `annotation_guide_version`，并列出需重标的切片；
- frozen release 永不原地覆盖，新版本通过 `supersedes` 连接。

## 17. 上下文证据与验收矩阵

该矩阵是第 1 阶段冻结前的必要验收；它只覆盖人物、对象、状态和碎片化上下文，不新增事件、标题或前端要求。

| 验收项 | 必须保留的证据 | 通过条件 | 不通过时 |
| --- | --- | --- | --- |
| fragment 与 opener | message ID、Unicode span、`fragment_type`、evidence | span 可精确回到 `redacted_text`；问候为 `conversation_opener`；同消息实质内容单独切片 | 保留 message，fragment 标 `unknown` 或返工，不强制建 event |
| 人物角色 | `speaker_id`、`mentioned_person_ids`、`subject_id` 及证据 | 三种角色分别可为不同值；未知逐项记录 | 禁止角色互换，进入仲裁 |
| 对象解析 | `object_resolution`、object ID、explicit span 或继承来源 | `explicit` 有本 fragment span；`inherited` 有来源 ID + context relation；否则 `unknown` | 不得用主题词补齐，保留 unknown |
| 状态生命周期 | `state`、`state_evidence`、transition/terminal evidence | 终态只由同一对象/实例明确完成、恢复、失败、取消或替代触发 | 沉默、无后续、未回复、topic shift 保持开放或 unknown |
| 起止边界 | start/end 值、source、evidence | start/end 可以都是 `unknown`；不把消息时间或采集首尾当语义边界 | 保留 unknown 与不确定性 |
| 上下文关系 | 两端 fragment/claim、七类 label、typed evidence、reply/time flag、strength | 七类 label 合法；无 reply 但有两个独立语义信号仍可 `answers`；时间只能 weak；仅靠时间创建关系为 0 | evidence 不足用 `insufficient`，禁止自动升级 |
| 价值与完整性 | `information_value`、`event_completeness` 及各自 evidence | 四种轴组合均可存在；价值低不删除，价值高不代替完整 event | 只修正对应轴，不联动改写 |
| 可重放与外键 | pipeline/ruleset、输入 ID、relation/thread IDs、evidence refs | 同输入同版本稳定生成；外键完整；未知状态可重放 | 记录版本差异并阻止冻结 |

Stage 1 指标至少报告：fragment span/type F1；speaker/mentioned_person/subject 角色准确率；object `explicit|inherited|unknown` 准确率；六值 state 准确率、终态误报率和 closure evidence 覆盖率；上下文关系按七类及 `evidence_strength` 分层的 macro-F1；无 reply 的 `answers` 召回率；仅靠时间创建关系率（必须为 0）；typed evidence 覆盖率；四种 value/completeness 组合的保留率与误过滤率。数值门槛在首个 release 基线后确定，零容忍项不因平均分抵消。

N/A 口径：`conversation_opener`、纯 acknowledgement/reaction/context-only fragment 的 `event_completeness` 记 `not_applicable`，不进入事件完整性分母；没有 state 或 transition 证据的片段记 `state=unknown`，只在出现状态证据的切片上计算生命周期准确率；没有候选关系的片段不计入关系召回，`insufficient` 单独报告为保守性指标；start/end 缺证据使用 `unknown` 而不是 N/A。任何 N/A 都必须保留原因，不能通过删除记录制造好看的分母。

### 17.1 Contextual Bundle 成本与语义联合矩阵

每个 release 必须同时报告语义质量与成本/延迟，不能用低成本掩盖错误合并，也不能用平均质量掩盖预算失控：

```text
C_total = C_registry + C_gate + C_sparse + C_dense + C_llm + C_evaluation
C_llm = (input_tokens/1e6)*price_in + (output_tokens/1e6)*price_out + requests*price_request
C_dense = embeddings*price_embedding + vector_index_cost
```

默认每 1,000 条新消息：registry/稀疏处理覆盖 100%；每条最多 20 个稀疏候选；dense 最多覆盖 200 个歧义 bundle、每包 top-20；LLM 最多 50 个 bundle、每包 2,000 input/400 output tokens；`cold_recoverable` 默认不调用模型，仅在查询/显式 bridge 时激活。实际价格、CPU/延迟基准和偏离原因写入 manifest。

| 维度 | 最低验收地板 | N/A/失败口径 |
| --- | --- | --- |
| 源/registry | 消息及权威 metadata 保留率 100%；幂等重复 0；跨 chat/account 泄漏 0 | 缺源或 scope 冲突阻断 release，不可转 N/A |
| gate | 四通道转移与重激活可重放率 100%；cold 不丢 cue/evidence | 预算/服务错误必须有 fallback artifact，不得算 unrelated |
| fragment/person/object/state | fragment span ≥98%；角色准确率 ≥0.95；object resolution macro-F1 ≥0.90；state macro-F1 ≥0.90；终态误报 0 | 纯 opener/ack/context-only 的完整性 N/A；未知 state 不作终态错误 |
| bundle/context | opener 保留 100%；context macro-F1 ≥0.85；无 reply 承接 precision ≥0.90；typed evidence 100%；仅时间建边 0 | 无候选只报覆盖，不伪造 recall；证据不足标 `insufficient` |
| LLM（实验） | 固定 schema 合法率 100%；非未知槽位 evidence 100%；abstain 可回放 | timeout/invalid/unavailable 全量 fallback，不部分采纳 |
| event/真实评测 | `same_event` precision ≥0.98；灾难性合并 0；五分类 macro-F1 ≥0.85 | 冻结测试不足或任一 MNL 穿透即阻断 |
| E2E | API/DOM/screenshot 稳定 ID、来源、证据一致率 100%；前端语义合并 0 | 任何不一致阻断生产提案 |

零容忍清单：原消息丢失或覆盖、权威 metadata 静默替换、跨 chat 无 bridge 建边、仅时间或 same-segment 强连、未知对象/人物/状态强填、沉默→`resolved`、无 typed evidence 的可见判断、LLM 自由文本落库、前端二次语义合并、生产写入或发送调用。Stage1 v1 的 57.8% 只能作为规则基线，不能被计为任何一项通过。

## 17.2 Workstream K1：`ContextPacket v1` 与分阶段 DeepSeek release 契约

> K1 是上下文工程/高召回候选契约，不是最终语义裁决契约。`bundle_llm_decisions.private.jsonl` 中的 `bundle_llm_v1` 仅为旧 artifact 的读取兼容；新 release 不得使用一个包含所有人物、对象、状态、claim、关系和审计字段的 17 字段大 schema。K1 仍属于 development/实验阶段，不能接生产或首页。

### 17.2.1 `context_packets.private.jsonl`

每个 packet 都必须把不可变、可核验的 `authoritative_facts` 与候选召回用的 `candidate_context` 分开。前者不是模型可覆盖的预测；后者必须明确标为 candidate，`reason_codes` 和 `confidence` 不是事实真值。

```text
packet_id: string
context_packet_version: context_packet_v1
analysis_run_id: string
scope: {account_id: string, chat_id: string}
anchor_fragment_ids: string[]
anchor_claim_ids: string[]
boundary: {
  start: {resolution: explicit|unknown, message_id: string?, evidence_ref: string?},
  end: {resolution: explicit|unknown, message_id: string?, evidence_ref: string?}
}
window: {scale: W0|W1|W2|W3|W4, message_ids: string[], fragment_ids: string[], claim_ids: string[]}
authoritative_facts: {
  message_metadata: [{message_id, chat_id, speaker_id, direction, message_type, event_time, sequence_in_chat}],
  reply_edges: [{message_id, reply_to_message_id, evidence_ref}],
  quote_edges: [{message_id, quoted_message_id, span_start, span_end, evidence_ref}],
  fragment_spans: [{fragment_id, message_id, span_start, span_end, evidence_ref}],
  media: [{message_id, media_type, availability, preprocess_status,
           missing_reason, semantic_evidence}]
}
candidate_context: {
  continuity_candidates: [{left_id, right_id, reason_codes, confidence}],
  qa_candidates: [{question_id, answer_id, reason_codes, confidence}],
  person_history: [{person_ref_id, message_ids, reason_codes}],
  object_history: [{object_ref_id, message_ids, resolution, reason_codes}],
  state_history: [{state_ref, message_ids, reason_codes}],
  open_threads: [{discourse_thread_id, unresolved_slot_codes, reason_codes}],
  activation_cues: [{cue, source_id, confidence}],
  candidate_reasons: [{candidate_id, reason_codes, confidence, evidence_refs}]
}
overlap_group_id: string?
status: open|pending|complete|abstained
evidence_refs: [{type, id, span?}]
provenance: {input_fingerprint, pipeline_version, ruleset_version, created_at}
```

`authoritative_facts.message_metadata` 的聊天作用域、speaker、方向、消息时间、顺序、reply、quote 和 span 以 source/registry 为准，只能追加 metadata revision；冲突必须保留并将受影响槽位置为 `unknown`。`candidate_context` 可以带连续窗口、问题—回答、同人物/对象历史、状态历史、未决 thread 和 activation cue，但不得把候选 ID、相似度或时间邻近写成事实。

一个 fragment/claim 可以属于多个 packet；packet 可以重叠，`overlap_group_id` 只用于审计/去重，不表示同一 event。只有一端可知或两端均 `unknown` 的 start/end 是合法标注，不能用采集窗口首尾伪造语义头尾。跨 chat 只能保留显式 bridge 候选，跨 account 不共享身份作用域；same-segment、同 speaker、主题词和时间不能单独建立强边。

### 17.2.2 三阶段输出文件与小 schema

每个阶段单独写入、单独计费、单独验证和单独回退：

```text
stage_a_topic_thread.private.jsonl
stage_b_topic_claims.private.jsonl
stage_c_consistency_audit.private.jsonl
```

**Stage A：TopicThread mapping（可选）。** 仅在无模型输入重建已 `accepted` 后运行；只返回可选 topic/thread、message 和 context IDs，不返回人物、对象、动作、状态、claim 真值、event、title 或自由文本理由。无主题时 `topic_groups=[]` 是合法结果：

```text
decision_id: string
packet_id: string
schema_version: stage_a_topic_thread_v1
topic_groups: [{topic_id: string, message_ids: string[], context_ids: string[]}]
thread_groups: [{discourse_thread_id: string, message_ids: string[], context_ids: string[]}]
topic_absent_episode_ids: string[]
unmapped_message_ids: string[]
unknown_context_ids: string[]
evidence_refs: [{type: message|fragment|claim|reply|quote|span, id: string, span?}]
validation_status: valid|invalid|timeout|unavailable
fallback_action: complete|pending_context|abstained
```

`topic_absent_episode_ids` 是对旧 Stage A wire 的可选增量字段；无主题、未知主题和待补上下文必须分开记录，且都不得标为 `unrelated`。Stage B/C 的输入、输出和禁止项保持本契约原定义不变；Stage B 只对实际存在的 TopicThread 分组调用。

**Stage B：per-topic claims。** 以一个 topic 的 packet 和 Stage A IDs 为输入，逐条输出人物、对象、动作、状态、claim 和 evidence：

```text
decision_id: string
packet_id: string
topic_id: string
schema_version: stage_b_topic_claims_v1
claims: [{
  claim_id: string,
  speaker_id: string|unknown,
  mentioned_person_ids: string[],
  subject_id: string|unknown,
  object_id: string|unknown,
  object_resolution: explicit|inherited|unknown,
  action: string|unknown,
  state: unknown|planned|ongoing|resolved|failed|cancelled,
  claim_type: fact|opinion|question|suggestion|hypothesis|unknown,
  modality: certain|probable|possible|required|desired|unknown,
  evidence_refs: [{type, id, span?}],
  unknown_field_codes: string[]
}]
validation_status: valid|invalid|timeout|unavailable
fallback_action: complete|pending_context|abstained
```

`historical` 只进入 `temporal_qualifier`，不得进入六值 `state`。非 `unknown` 的 speaker/mentioned/subject/object/action/state/claim_type/modality 必须有 typed evidence；inherited object 必须有来源 ID 和 context relation。Stage B 不合并 topic，不创建 event/title。

**Stage C：consistency audit。** 只审计 A/B 与 packet 的冲突、漏项、错误合并和 unknown 保留，不偷偷修正 A/B 或产生 event：

```text
decision_id: string
packet_id: string
schema_version: stage_c_consistency_v1
stage_a_decision_id: string
stage_b_decision_ids: string[]
conflicts: [{kind: slot|attribution|state|scope, left_ref, right_ref, evidence_refs}]
omissions: [{expected_ref, reason_code, evidence_refs}]
merge_errors: [{left_ref, right_ref, reason_code, evidence_refs}]
unknown_preservation: [{ref, slot, evidence_refs}]
audit_status: pass|fail|uncertain|pending
validation_status: valid|invalid|timeout|unavailable
fallback_action: complete|pending_context|abstained
```

### 17.2.3 证据、缓存与失败回退

所有阶段的 evidence 都必须是可定位 typed handle：`message`、`fragment`、`claim`、`reply`、`quote` 或带半开区间的 `span`。Stage A 的 evidence 只能证明分组引用存在；Stage B 每个非未知槽位必须回到 packet 的 message/fragment span 或 reply/quote；Stage C 每条冲突、漏项、错误合并和 unknown 保留必须引用阶段输出及其输入证据。模型解释文本不算 evidence。

阶段缓存 key 必须隔离：

```text
K_stage = H(stage_name + stage_schema_version +
            canonical_context_packet_fingerprint + input_stage_decision_ids +
            model_id + model_version + prompt_version + ruleset_version)
```

不得跨 stage、model、prompt、schema 或 ruleset 复用。只有 schema 合法、evidence 完整且 `validation_status=valid` 的 `complete` 结果可写完成缓存；invalid、timeout、unavailable、超预算、越界和 pending 均不得 put。

Stage A 失败时保留 packet 并回 `pending_context`；Stage B 失败时保留已验证的 A、B 记 pending；Stage C 失败时保留 A/B、审计记 `unknown|pending`。所有回退记录 `reason_code`、input fingerprint、预算、attempt、模型/提示版本和 activation cue。失败不等于 unrelated，沉默不等于 resolved。

### 17.2.4 K1 调试指标、N/A 与最低门

manifest/评测报告必须分别记录 packet、candidate、stage request、validated output、pending、budget deferred、tokens、retry 和 latency。候选数量不能当 provider call 分母，零请求不能当成功：

| 指标 | 定义 | N/A 规则 |
| --- | --- | --- |
| `packet_context_recall` | gold 所需 context units 中，至少进入一个合规 packet 的比例 | 没有 context gold units 时 N/A；不得用无候选替代 0 |
| `distractor_rate` | packet 中无关 candidate units / 全部 candidate units，按 W0-W4、scope、overlap 分层 | packet 无候选时 N/A，并保留绝对计数 |
| topic split/merge | Stage A 对 gold topic 的拆分率与跨 topic 合并率，分开统计 | 无 topic gold 或单 topic 片段时对应项 N/A |
| claim/evidence | claim 槽位准确率、非未知 typed-evidence coverage、evidence handle precision/recall | 只对有 gold evidence 的槽位评分；unknown 保留另计 |
| stage success | 各阶段 validated complete / started provider requests，并分 provider failure/pending/budget | 0 request 为 N/A；candidate 不作分母 |
| cost | 每 packet/topic/阶段的请求数、输入/输出 token、重试、延迟、CPU/embedding 与每千消息成本 | provider 不可用时报告实际尝试和 0 output，不估算成功 |

Stage1 的发布前最低门仍包括：authoritative metadata/顺序/reply/span 保留率 100%；packet 不跨 chat/account 偷连；未知边界/对象/人物/state 不强填；仅 time/same-segment 强连为 0；沉默标 `resolved` 为 0；非未知 claim 槽位 evidence 覆盖率 100%；三阶段 schema 合法率 100%；失败结果不进入 complete cache。K1 指标尚未证明 production readiness，必须继续标记 `production_blocked`，且不能用 57.8% `rules_v1` 基线替代。

### 17.2.5 交流过程重建与无模型 prototype（2026-08-31）

本节只定义进入 DeepSeek 前的可审阅输入，不改变 Stage B/C、首页或生产闸门。金标准的语义入口固定为：

```text
ContentLedger → ConversationEpisode / 交流形态
              → TopicThread / 话题流（可选）
              → DeepRead（DeepSeek；Stage B → Stage C）
```

`ContentLedger` 是每条不可变 message、权威 metadata、正文引用/digest、顺序和媒体状态的证据根；`ConversationEpisode` 是本地仅依据可观察顺序、reply/quote、fragment span 和候选互动信号拼接出的交流过程单元。`TopicThread` 不是每条记录的必填结果，只有输入验收后才允许由 DeepSeek 做语义判断；闲聊、纯确认、反应或证据不足可以没有 topic。`DeepRead` 承载语义连续性、内容归属、claim、state、价值和不确定性判断，沿用现有 Stage B/C 小 schema。

本地职责严格限于候选召回、窗口/episode 拼接、媒体预处理和证据整理；本地规则、关键词、embedding、时间邻近或 same-segment 不得写入最终 topic/thread/claim/state/value 真值。交流作用、聊到的内容和信息价值必须分栏标注：

- 交流作用使用 `interaction_role_candidate`（如 `opener|social_smalltalk|acknowledgement|question|request|answer|reaction|continuation|elaboration|contrast|topic_shift|unknown`）；
- 聊到的内容使用 `content_refs`（可定位文本/引用 span 或独立媒体产物，允许 `unknown`），不改写成 topic；
- 信息价值使用 `information_value=none|low|medium|high|unknown`，记录来源和 evidence，不由闲聊标签、内容相似度或完整性推导。

prototype 中的 `interaction_form_candidate` 只是待审阅过程信号；最终 `interaction_form`（如需要）只能由人审或已验收输入上的 DeepSeek 产生并带 typed evidence，且不改变 Stage B/C schema。

无模型 prototype 只允许公开 synthetic fixture；私有 release 中建议保存 `content_ledger.prototype.private.jsonl`、`conversation_episodes.prototype.private.jsonl` 和 `input_reconstruction_report.private.json`。每条 episode 至少有稳定 ID、ledger/message/fragment 外键、候选交流形态、上述三类分栏（`information_value_source=human_review|unknown`）、边界 resolution、`today|yesterday|week` view refs、evidence、uncertainties 和 `status=review_required|accepted|rework|blocked`。prototype 不输出最终 topic/TopicThread、claim、event/title 或非 `unknown` 的语义 state。

媒体必须显式记录 `availability=available|unavailable|not_present|unknown`；`unavailable` 必填 `missing_reason`，并固定 `semantic_evidence=false`。不可用占位、路径、媒体类型和失败 OCR/ASR 不能成为语义 evidence；独立人工核验的脱敏 transcript 才能按文本 evidence 登记。

`today`、`yesterday`、`week` 是同一 episode/thread 的查询视图，不是切分键。同一稳定 thread ID 可出现在多个视图，跨日/跨周不因日历边界拆分或关闭；边界不明保持 `unknown`。

输入重建报告必须先于 provider request 产生：只有 `input_reconstruction_status=accepted` 才允许创建 Stage A request；状态枚举为 `accepted|pending|rework|blocked`，`pending|rework|blocked` 的 provider request 必须为 0，并保留 reason code、input fingerprint 和 activation cue。无 topic 结果不是 provider 失败。

不变量：`model_call_allowed=true` 当且仅当 `input_reconstruction_status=accepted`；该值由验收报告生成，不由 provider、模型或调用方覆盖。

| prototype 验收指标 | 最低门 |
| --- | --- |
| ledger message/metadata/scope/order/reply/quote 保留 | 100%；丢失、重复和静默改写为 0 |
| episode 可审阅覆盖、外键和 evidence 回链 | 100% |
| episode 边界/关系 | context macro-F1 ≥0.85；无 reply 承接 precision ≥0.90；仅时间/same-segment 强连为 0 |
| 交流作用、内容、信息价值分离 | 字段互不推导；低价值不误过滤；四种组合保留率 100% |
| 主题可选 | 强制 topic/TopicThread 率为 0；topic coverage 仅诊断 |
| 媒体缺失 | unavailable 显式缺失率 100%；媒体占位/路径充当语义 evidence 率 0 |
| 日/周视图 | 同一 thread 稳定 ID 一致率 100%；仅日历边界拆分/关闭率 0 |
| 模型前置闸门 | 未 accepted 的 provider request 为 0；accepted 才调用合规率 100% |
| replay | 同输入与版本的 ID、scope、evidence、状态一致率 100% |

任一硬门失败即 `rework`/`blocked`，不发起模型调用；通过输入门只授权实验性 DeepSeek Stage A/B/C，不代表生产准入。`production_blocked`、首页和现有 Stage B/C 约束继续有效。

## 18. 自动校验不变量

冻结前校验器必须全部通过：

- 所有 JSONL 可逐行解析，ID 在各自文件唯一，外键完整；
- message 不含原始 ID、绝对路径、完整 URL、明文联系人或高危秘密模式；
- fragment span 与 `redacted_text` 严格一致；`conversation_opener` 及无 claim fragment 不得被删除；
- `speaker`、`mentioned_person`、`subject` 分别可追溯，未知值不能静默互换；
- object 为 `explicit` 时有本片段 span，`inherited` 时有来源 ID 和 context relation，其他情况必须为 `unknown`；
- mention span 与脱敏文本严格一致；
- claim 至少有一个 evidence span，且 speaker/message/target 引用有效；state、start/end、value/completeness 的 evidence 与枚举合法；
- 状态终态必须有同一对象/实例的终态证据；沉默、无后续、未回复和 topic shift 不得单独产生 `resolved`；start/end 的 `unknown` 合法且不得用消息时间伪造；
- context relation 两端必须是 fragment/claim，label 只能为七类之一；每条 relation 有 typed evidence；`explicit_reply_present=false` 不得阻止 `answers`，true 也不得单独证明 event；
- `information_value` 与 `event_completeness` 独立校验，四种组合均可通过，不能互相推导或过滤；
- relation 对规范排序、五分类值合法、双人标签与仲裁状态完整；
- frozen cluster 内不存在未覆盖 MNL，`same_event` 与 MNL 不可同时成立；
- presentation 每个可见句都有 claim/message 证据，分离约束无冲突；
- 一个事件或回复链不跨 development/frozen_test；
- manifest 记录数、label counts 和文件 SHA-256 与实际一致；
- 每条 message 恰有一条 registry；registry 不含正文且 metadata fingerprint/版本可验证，重复导入幂等；
- gate 只能使用 `immediate|pending_context|background|cold_recoverable`，每次转移可逆、带 trigger/reason/cue；任何通道不得代表 resolved 或删除证据；
- DialogueBundle 的 window scale、chat scope、member 外键、open snapshot 和多 bundle 归属完整；W1 不得只按相邻 fragment zip，W4 跨 chat 必须有 bridge；
- bundle LLM 输出严格符合固定 schema；每个非未知 slot/relation/pairwise label 有 typed evidence，invalid/timeout 必须记录 fallback；embedding 不得写入事件身份；
- `ContentLedger` → `ConversationEpisode` prototype 可回链且先通过 `input_reconstruction_status=accepted`；交流作用、content_refs、information_value 不互相推导；主题缺失可保留；未 accepted 的输入不得产生 provider request；
- 媒体 `unavailable` 必有 `missing_reason` 且 `semantic_evidence=false`；媒体占位、路径和失败 OCR/ASR 不得作为语义 evidence；`today|yesterday|week` 只作同一 thread 的视图，不得按日历硬切；
- 默认成本预算、input/output token、请求数、延迟和失败回退均写入 manifest/运行报告；预算超限不得以丢消息或改标签解决；
- `git check-ignore` 确认 release 全部被忽略，`git ls-files` 不包含任何 private 路径。

## 19. 完成定义

2026-08-25 金标准 v1 只有同时满足以下条件才算完成：

1. 私有目录和所有导出均被 Git 忽略，隐私扫描零未处理高危项；
2. 脱敏规则、抽样 seed、源快照 HMAC、文件校验和与 coverage shortfall 均进入 manifest；
3. 达到第 5.3 节起始规模，或如实标为 `pilot_limited` 且不用于上线门槛；
4. message、fragment、mention、claim、discourse_thread、context_relation、relation、cluster、presentation 外键和 schema 校验全部通过；
5. 纳入样本的 fragment/mention/claim/context_relation/relation/presentation 100% 双人独立标注，所有规定分歧 100% 仲裁；
6. 所有 cluster 完成 MNL 校验，未覆盖冲突为 0；
7. 每个用户可见 presentation 句子的 claim/message 证据覆盖率为 100%，无证据新信息为 0；
8. development 与 frozen_test 按事件/回复链隔离，冻结测试未参与调参；
9. 一致率、分歧类型、coverage、局限和仲裁数量形成不含原文的私有报告；
10. 两名标注员与仲裁员签署 manifest 中的本地完成记录，数据保管人确认没有私有文件被 Git 跟踪；
11. registry、gate、DialogueBundle、open snapshot 和固定 LLM schema 的 artifact 与成本记录均通过第 17.1 节地板；
12. 第三版整链路的 12 项交付、真实评测、影子/API/DOM/screenshot E2E 和生产接入仍须按 [Contextual Bundle 实施计划](D:/Project_Codex/Project_WeChatMoreFunction/docs/contextual-bundle-implementation-plan.md) 逐项签收；本契约冻结不等于生产准入。

完成金标准只代表评测数据具备使用条件，不代表任何算法达标。A/B/C/D 的模型准入门槛必须在该数据集冻结后另行记录，不能反向修改金标准以适配某个方案。
