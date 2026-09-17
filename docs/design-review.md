# 微语：第三版权威设计基线

> 状态：当前权威设计，2026-08-27 起生效；本次 Contextual Bundle 修订与实施计划为新增上位约束。
>
> 本文区分“当前实现”“设计目标”“实验候选”和“禁止上线”。第一版采集、安全与身份边界继续有效；第二版语义聚类与展示方案已被本轮端到端复盘降级为待替换的 legacy 基线，不能再作为准确性已达标的证据。

> 2026-08-31 路线修订：语义入口改为 `ContentLedger → ConversationEpisode（交流形态） → TopicThread（话题流，可选） → DeepRead`。本修订优先于早期“主题分类”及本地语义抽取措辞；Stage B/C、首页契约和 `production_blocked` 状态不变。输入重建未通过人工可审阅验收前，不得发起任何模型调用。

本版总原则是：**可以延迟理解，但不能提前遗忘；可以暂不昂贵编码，但必须保留重新激活线索。** 原始消息不可变，元数据字段由消息源/适配器提供的权威投影负责；任何语义理解都是带版本、可丢弃、可重算的派生物。完整交付、真实评测、影子链路、生产接入和 E2E 不是本次文档修改即已完成的事项，统一按 [Contextual Bundle 实施计划](D:/Project_Codex/Project_WeChatMoreFunction/docs/contextual-bundle-implementation-plan.md) 的 12 个交付物和闸门推进。

## 结论

采集与安全方向仍然成立：适配器、统一消息模型、SQLite、历史档案、可解释分析和发送安全边界继续解耦。当前已经验证的基础范围是：

`微信本地数据库 → 跨分片日/周全量导入 → SQLite 去重 → 情报分析与证据回链`

当前运行模式硬锁 `send_enabled=false`：读取、导入和分析都不会调用发送适配器。产品边界为永久只读；本文不把自动发送、批量发送或恢复发送列入实施计划。

语义情报层的现行结论不同：当前 `rules_v1/event_v2`、AI fallback、标题规则、首页来源选择和前端 `sameTopic` 组成的链路没有通过真实数据金标准和最终界面端到端审核。它只能作为 legacy 对照基线。第三版先实现独立、纯函数、可重放的 `semantic_pipeline.py` 与 v2 DTO，通过离线实验和影子模式验证后，才允许讨论生产切换。

## 已落地能力

### 适配器

- `wechatauto_db` 是当前微信 4.1.12.26 的主路线：只读解密本地数据库，跨 `message_*.db` 分片合并监听。发送代码路径和发送后本地回读确认逻辑已经存在，但当前运行态 `send_enabled=false`，不能把发送能力描述为当前可用能力。
- 统一适配器接口已定义 `send_image`、`send_file`、`get_chat_history`、`list_accounts`；其中历史读取和账号枚举已用于当前路线，发送相关接口只是历史代码兼容面，不属于产品能力或后续路线。
- `wechatauto_db` 另提供 `list_message_chats` 和 `get_history_range`，全量历史路径按日期跨所有可读会话读取，每个数据库分片只扫描一次。
- Hook HTTP 保留为候选适配器。公开项目的目标版本与当前微信不匹配，本项目不负责 DLL 下载、注入或微信目录改造。
- wxauto4 只作为显式回退路径，不能因版本字符串相近就宣称兼容。

### 消息、规则和 AI

- 消息模型包含消息 ID、聊天、发送者、类型、时间、自发消息判断、原始数据、适配器版本和 `is_group`。
- SQLite 用 `(adapter_name, message_id)` 去重，任务记录状态、错误、发送尝试和确认信息。
- 规则支持关键词、正则、聊天/发送者、消息类型、时间范围、启停和 Asia/Shanghai 时区；规则按顺序匹配第一条。
- AI 使用懒加载的 OpenAI Responses API 生成器，缺少 `OPENAI_API_KEY`、SDK、服务响应或空输出时都转为明确失败，不发送空消息。
- AI 预览接口保证不入库、不入队、不发送。
- `analysis.py` 提供本地规则分析：窗口统计、小时分布、主题、重点线索、行动候选和事件时间窗；每条分析对象保留 `message_id` 证据，不把规则结果伪装成事实。

### 控制台与 Codex 接口

- `run --dashboard` 启动仅绑定 `127.0.0.1` 的“微信情报站”，展示日期窗口、消息档案、重点线索、行动候选和运行边界。
- `/api/status`、`/api/messages`、`/api/insights`、`/api/chats`、`/api/sync-range`、`/api/sync-status`、`/api/preview` 等组成当前本地 API；`/api/send-text` 在只读模式下硬性拒绝。
- `plugins/wechat-bridge` 通过 MCP 把本地 API 暴露为 Codex 工具；发送工具仅保留兼容接口，当前只读运行态下全部阻断，不能因为存在 `confirm` 参数就视为已授权发送。

## 安全状态机

```text
历史范围选择
  ↓
适配器按日期跨分片读取
  ↓
统一标准化 / SQLite 去重
  ↓
窗口统计、重点、行动候选、事件时间窗
  ↓
保留证据 message_id，等待人工判断
  ↓
当前 send_enabled=false → 任何发送入口直接拒绝
```

界面必须区分：

- `dry_run`：演练，未发送；
- `paused`：仍接收和记录，但不自动发送；
- `pending/sending`：队列或发送进行中；
- `succeeded + confirmed`：已发送并回读确认；
- `succeeded + unconfirmed`：接口接受但微信侧未确认，不能当作完全成功；
- `failed`：失败并可能已耗尽重试；
- `skipped`：业务安全跳过，不等于系统故障。

## 第一版验收顺序

按当前审计结果，第一版验收项不能全部标记为通过：历史日/周读取、跨分片去重和只读发送闸门已有实测证据；全账号实时监听、媒体可回看、复杂事件分析和 Hook 兼容性仍属待验证。下面的顺序是验收计划，不是已完成清单。

1. `m0-check` 记录 Python、wxauto4、微信进程和完整版本号。
2. 使用 `wechatauto_db` 验证今天范围全量导入：覆盖会话、消息数、去重和导入耗时。
3. 使用近 7 天范围验证跨日边界、跨分片读取和重复导入不增加重复消息。
4. 在控制台检查窗口统计、重点线索、行动候选和原消息证据回链。
5. 验证 `send_enabled=false` 时自动任务和 `/api/send-text` 都不会触碰适配器发送方法。
6. 最后再评估图片、文件、群聊、多账号和 Hook 的独立适配器验收，不能把它们混入只读历史验证。

## 仍需后续补强

- 人工确认目前是“分析结果待确认”，还不是完整的 `pending_review/approved/rejected` 持久化审批状态；发送保持锁定，后续要做逐条审批 token、过期和审计。
- 同步任务状态目前主要保存在进程内；后续应增加 `sync_runs` 表，保存范围、断点、覆盖质量和重跑记录。
- 当前分析是 `rules_v1`，可以发现线索但不会可靠识别复杂事件、责任人和承诺状态；AI 只能作为带证据的可选二次分析。
- 发送“已接受但未确认”还应单独进入不确定状态，避免重试造成重复消息。
- 暂停状态和冷却窗口目前是进程内状态；如要跨重启保持，需要加入 settings 表。
- 规则在线编辑目前只替换内存规则；要持久化需要版本、校验、原子保存和回滚。
- 多账号目前是适配器启动时选择一个账号，不支持热切换；数据库表还没有独立 account_id 维度。
- 群聊模型已支持标记，但默认发送闸门会拒绝群聊；群聊防循环需要独立策略和现场测试。
- Hook 仍需要针对微信完整版本的外部兼容构建物和回调认证，当前不做逆向注入。

## 第三版语义情报架构（2026-08-26）

### 1. 文档状态与实施冻结线

第三版将语义情报层定义为一条可独立评测、可重放、可回滚的派生管线。当前生产页面继续属于 legacy 基线，但不得再用零散规则修补后宣称语义问题已经解决。

本轮第 1 阶段先补齐 `ContentLedger → ConversationEpisode` 的交流过程输入，再在验收后进入 DeepRead 的人物、对象、状态和碎片化上下文子层。它不改变既有五分类事件关系、事件之后的标题/摘要策略或前端契约；上下文关系只能帮助组织证据，不能直接创建或合并 `event_id`。

在满足本文上线门槛之前：

- 不把第三版结果接入首页、主题页或主卡片；
- 不引入新的模型依赖，不下载模型，不训练或微调模型；
- 不迁移数据库，不改变现有表和生产 API；
- 不以 API 返回结构正确代替最终 DOM、截图和证据回链验收；
- 不删除原始消息，不覆盖 legacy 派生结果，不改变永久只读边界。

第一实施阶段只新增 `src/wechat_bridge/semantic_pipeline.py` 的纯函数管线、v2 DTO、固定样例和离线评分入口。它读取显式传入的数据并返回新对象，不读取数据库、不访问网络、不依赖全局配置、不写文件、不调用现有首页聚合函数。

### 2. 失败复盘与不可违反的设计约束

2026-08-26 的复盘确认了四层脱节：

1. 候选聚类只因共享 GPT、账号、重置、消耗等宽泛词而过度合并；
2. 标题规则把错误簇包装成看似确定的编辑标题，掩盖内部冲突；
3. 首页来源门槛和 `sameTopic` 再次选择、去重或合并，使后端候选与实际主卡片不一致；
4. 前端继续渲染 `detail_points`，导致已经声称删除的细节区仍然存在。

因此第三版遵守以下不变量：

- 语义相关不等于同一事件；共享父主题不能覆盖核心对象、动作、状态或诉求冲突。
- 标题、摘要、趋势和版面选择都是事件之后的派生物，不得反向创造事件身份。
- “谁—针对什么—说了什么—依据什么”是主数据，不是摘要附件。
- 每个用户可见判断都必须回到具体主张、消息和证据片段。
- 前端只负责稳定 ID 的排版与交互，不执行语义合并、事件共指或来源替换。
- 原始消息不可变；所有语义派生物带版本，可丢弃、重算和并行比较。
- 局部单元测试、接口字段或候选数量均不能代替端到端验收。

### 3. 产品目标与永久边界

产品目标仍是面向个人决策的信息收集、筛选、理解和人工处理工作台。它帮助用户回答“发生了什么、谁说了什么、哪些仍待核实、原始证据在哪里”，而不是替用户制造一个唯一结论。

以下决策永久保留：

- `send_enabled=false`，不自动回复、不自动发送，AI 不创建发送任务；
- 历史同步、实时监听和恢复数据分别标记，不把历史导入冒充实时全量；
- 原文、时间、会话、发言人、回复/引用关系、链接和媒体转写完整保留；
- `account_id + chat_id + source_member_id` 是群成员身份作用域，禁止全局数字索引猜人；
- 无法确认的身份、上下文、事件关系或事实状态显示 `unknown`、`context_needed` 或 `insufficient_context`；
- 重点、趋势和行动在人工确认前都是候选；规则或模型排序不等于事实。

### 4. 权威术语与层级

第三版固定使用以下单向派生层级；2026-08-31 起以交流过程重建为入口。旧 `fragment/claim/discourse_thread/event` 字段仍作为证据与下游兼容 artifact，不再表示“先按主题分类”：

```text
ContentLedger
      ↓
ConversationEpisode / 交流形态
      ↓  （输入验收后，话题可无）
TopicThread / 话题流（可选）
      ↓
DeepRead（DeepSeek；沿用 Stage B → Stage C）
      ↓
optional event → topic_family / trend / presentation
```

展开后是 `ContentLedger → ConversationEpisode (1..n, 可开放) → TopicThread (0..n, 可为空) → DeepRead`；DeepRead 再按现有 Stage B/C 产出 `fragment/claim/discourse_thread` 证据和可选 event。`mention` 仍是 fragment/claim 内可定位的 span 标注，不是独立的事件合并层；一个问候、回应或上下文片段可以没有 claim、TopicThread 或 event，也不能因此被丢弃。主题缺失是合法结果，`topic_family` 只用于可选导航/观察，不是每条消息的必填标签。

| 对象 | 定义 | 允许的作用 | 禁止的作用 |
| --- | --- | --- | --- |
| `ContentLedger` | 不可变 message、权威 metadata、正文引用/digest、媒体可用性和证据索引 | 本地登记、召回、预处理和证据根 | 推断 topic/claim/state，或以媒体缺失替代内容证据 |
| `ConversationEpisode` | 按顺序、reply/quote、fragment 和互动候选重建的交流过程单元 | 保留闲聊/互动作用、内容 refs、边界和价值的可审阅输入 | 充当 TopicThread/event 真值，因日历边界硬切 |
| `TopicThread` | 一个或多个 episode 之间可选的语义内容流 | DeepSeek 语义判断后的内容组织和回链 | 强制覆盖每个 episode，或充当 event 真值 |
| `DeepRead` | 对已验收 episode/packet/topic 候选进行的 DeepSeek 语义读取 | 输出已有 Stage B/C 约束下的 claim、关系、state 和不确定性 | 接收未验收输入、无 evidence 结论或生产写入 |
| `message` | 不可变的源消息及其来源、会话、人物、时间和回复关系 | 作为一切证据的根 | 被摘要或人工反馈覆盖 |
| `fragment` | 消息中可独立参与对话理解的最小连续片段；可为陈述、问候、回应、上下文或媒体占位 | 保存局部人物、对象、状态、价值和证据 | 因没有完整事件而删除，或直接当作事件 |
| `mention` | fragment/claim 中实体、动作、时间、状态、诉求等可定位提及 | 提供 span 证据和结构化抽取 | 单独决定话语线程或事件合并 |
| `claim` | 某个说话人对对象或事件作出的事实陈述、观点、问题、建议或假设；可由 fragment 产生，也可为空 | 保留归因、情态、立场和证据片段 | 被无主体摘要替代，或把问题/观点改写成事实 |
| `discourse_thread` | 围绕局部主体、对象、状态或诉求，由碎片/主张和上下文关系组成的可审计对话链 | 组织跨消息证据、承载开放或不完整上下文 | 充当事件真值，或因沉默自动关闭 |
| `event` | 特定时间范围内，围绕明确对象发生或被报告的一次状态变化、问题、决策或行动 | 承载同一现实事件的提及与主张 | 因同属主题族而自动合并 |
| `topic_family` | 长期、宽泛的讨论领域，例如 AI 服务或账号与平台 | 导航、宽召回和主题观察 | 充当事件 ID |
| `trend` | 同一指标、主张类型、对象或主题族随时间重复或变化的候选信号 | 展示变化并回链组成项 | 取代组成它的事件或主张 |
| `presentation` | 首页卡片、栏目、lead、标题、摘要和详情布局 | 排版已经确定边界的数据 | 修改事件身份或跨 ID 合并 |

`AI 服务` 可以是一个 `topic_family`；`GPT 额度重置`、`中转站成本比较`、`multica 消耗异常` 是不同事件或问题；“某人说 multica 消耗很高”是一条带人物、对象和证据的 `claim`。它们可以同栏展示，但不能折叠成一条事实。

#### 4.1 人物、对象和状态先于事件

上下文抽取先记录角色，再讨论事件：

- `speaker`（`speaker_id`）是实际发送该 message 的人；`mentioned_person`（`mentioned_person_ids`）是文本中被提及、引用或转述的人；`subject`（`subject_id`）是该片段正在陈述其动作或状态的主体。三者可以相同，也可以不同，未知时分别保留 `unknown`，不得把 speaker 自动当作 subject。
- `object` 是被谈论、操作或受影响的对象。每个 fragment/claim 必须显式标记 `object_resolution=explicit|inherited|unknown`：`explicit` 必须有本片段 span；`inherited` 必须记录承接它的 fragment/claim 和上下文关系；`unknown` 是正常结果，不能用父主题、关键词或同群聊静默补齐。
- 状态是独立槽位，不因对象被识别就推断。第一阶段固定六值 `unknown|planned|ongoing|resolved|failed|cancelled`；允许从 `unknown` 进入有证据的状态，`closure_reason` 只在终态填写（被替代可用 `state=cancelled, closure_reason=superseded`）。旧 `planned` 直接映射为 `planned`，旧 `historical` 进入 `temporal_qualifier=historical`，不能把 historical 当终态。
- 只有同一对象/实例上出现明确完成、恢复、失败或取消的证据，才能写入 `resolved|failed|cancelled`；`closure_reason=superseded` 也必须有明确替代证据。最后一条消息、没有后续消息、改聊别的主题、对方未回复或沉默都不能当作 `resolved`；没有终态证据就保持 `unknown|planned|ongoing` 及相应不确定性。
- `start_time`、`end_time` 和语义边界分别记录证据状态 `explicit|inherited|unknown`。截断窗口、从中途开始的线程和没有收尾的线程可以两端都是 `unknown`；消息时间戳不自动等于事件起止时间，也不得用首尾采集消息伪造头尾。

`information_value` 与 `event_completeness` 是两个独立轴：前者表示片段对当前对话理解/后续行动的价值，后者表示事件身份槽位是否足够。问候通常是 `information_value=low`、`event_completeness=not_applicable`，但“早上好，接口还没好吗？”应拆成 `conversation_opener` 加问题 fragment；“又坏了”可以价值高而完整性仍为 `partial|unknown`。低价值不等于删除，高价值也不等于已经构成完整事件。对 `not_applicable` 的非事件片段，事件完整性指标记 N/A，不把它当作错误或负例。

#### 4.2 碎片类型与对话开场

`conversation_opener` 专门表示“你好/早上好/在吗”等打开会话或重新接话的礼貌性片段。它保留 message、speaker 和 evidence，但默认不创建 event；若同一 message 还包含对象、动作、状态或诉求，必须切成多个 fragment，不能让问候掩盖后续 claim。开场片段可以成为 `discourse_thread` 的起点，也可以缺失；两者都不影响未知边界规则。

#### 4.3 话语上下文关系

fragment/claim 之间的上下文关系独立于事件关系，允许且只允许使用以下标签：

| 关系 | 含义 |
| --- | --- |
| `continues` | 延续同一话题中的主体、对象、状态或未完诉求 |
| `elaborates` | 为前一片段补充细节、理由、范围或证据，不要求新增事件实例 |
| `answers` | 回应前一问题/请求；可由语义和对象/状态兼容性判断 |
| `contrasts` | 对同一讨论对象或状态提出相反、对照或冲突表述 |
| `topic_shift` | 明确转向新的主体、对象、状态或诉求；不表示旧线程已解决 |
| `possibly_related` | 有弱关联线索但不足以确认上述关系，只建立候选边 |
| `insufficient` | 证据不足以选择前六类，不自动连边或升级；它与事件关系的 `insufficient_context` 分开 |

显式 `reply_to_message_id` 是强证据，应在 provenance 中保留，但不是任何上下文关系的必需条件；没有显式 reply 时，至少需要两个相互独立的语义信号（连续指代、问题—答复、对象继承、主体/状态延续等）才能建立非 `possibly_related` 的承接关系。时间邻近只是 `weak` 证据，不能单独创建关系或继承对象/状态；长间隔也不是硬切断，只要有内容证据仍可承接。显式 reply 同样不能覆盖明确对象/状态冲突，也不能单独证明同一 event。

`evidence_strength` 与关系 label 分开：`strong` 可来自显式 reply/引用或明确对象继承，`medium` 通常需要两个独立语义信号，`weak` 只表示单一线索（包括时间邻近），`none` 用于 `insufficient`。强度不能把弱关系升级为 event，也不能替代 typed evidence。

### 5. v2 DTO 与稳定身份

第三版 DTO 必须是显式、可序列化、无数据库对象引用的数据结构。第一阶段可用冻结 dataclass、TypedDict 或等价结构实现，但字段语义不得由页面临时猜测。

#### 5.1 通用元数据

每个派生对象至少包含：

- 稳定 ID：`fragment_id`、`person_ref_id`、`argument_id`、`mention_id`、`claim_id`、`discourse_thread_id`、`context_relation_id`、`event_id`、`topic_family_id`、`trend_id` 或 `presentation_id`；
- `schema_version="semantic_v2"`；
- `context_schema_version="dialogue_context_v1"`；
- `pipeline_version`、`ruleset_version`；若使用模型，再增加 `model_id/model_version/prompt_version`；
- `analysis_run_id`、`created_at`；
- `source_message_ids` 和可定位的 `evidence_refs`；
- `provenance`：输入对象 ID、产生该对象的阶段、参数/阈值版本和人工修订记录；
- `confidence` 与 `uncertainties`，两者不能互相替代。

稳定 ID 基于规范化输入身份和版本产生；同一输入、同一版本必须可重放得到相同 ID。版面顺序、标题文字和重要度变化不得改变 `event_id`。

#### 5.2 `FragmentV2`

至少保存：

```text
fragment_id
message_id
span_start/span_end/evidence_text
fragment_type             # conversation_opener/statement/question/request/answer/ack/context/media/unknown
speaker_id
mentioned_person_ids
subject_id/subject_type
object_id/object_resolution/object_evidence_refs
state/state_evidence       # six values: unknown/planned/ongoing/resolved/failed/cancelled
closure_reason            # resolved/failed/cancelled/superseded/unknown
start_time/end_time       # each: explicit|inherited|unknown, value may be absent
information_value         # none/low/medium/high/unknown
event_completeness        # not_applicable/partial/sufficient/unknown
claim_ids
evidence_refs
uncertainties
```

每个非 `unknown` 的人物、对象和状态槽位必须有可定位证据；`inherited` 还必须记录来源 fragment/claim 与 context relation。`conversation_opener` 不因价值低而过滤。

#### 5.3 `PersonV2`

人物角色必须作为独立对象保存，不能用一个无角色的 participant 列表替代：

```text
person_ref_id
person_id/resolution       # resolution: explicit|inherited|unknown
role                       # speaker/mentioned_person/subject
message_id
fragment_id
claim_id                   # optional
span_start/span_end        # nullable for metadata-derived speaker
evidence_refs
source                     # message_metadata/text/reply_context/unknown
confidence
```

同一 fragment 内允许一个 speaker、多个 mentioned_person 和一个或多个 subject；`subject` 可以是对象而非人物，此时由 `ArgumentV2` 记录。角色未解析时保留 `unknown`，不能把 speaker 推成 subject。

#### 5.4 `ArgumentV2`

argument 是 fragment/claim 的角色槽位，用来保持人物与对象在主语、受事等位置上的区分：

```text
argument_id
fragment_id
claim_id                    # optional
role                        # subject/object/agent/recipient/source/target/unknown
entity_id/entity_type       # person/object/group/organization/unknown
resolution                  # explicit|inherited|unknown
evidence_refs
inherited_from_id           # required when resolution=inherited
confidence
```

`object` 仍必须额外写 `object_resolution=explicit|inherited|unknown`；`ArgumentV2` 不为缺失的对象创造默认值。

#### 5.5 `MentionV2`

至少保存 `message_id`、`span_start/span_end/evidence_text`、`mention_type`、规范化对象 ID、原始表述、动作、时间、状态、诉求和抽取置信度。证据片段必须能够在原消息中定位。

#### 5.6 `ClaimV2`

至少保存：

```text
speaker_id
fragment_id
mentioned_person_ids
subject_id/subject_type
object_id/object_resolution/object_evidence_refs
claim_text
claim_type               # fact/opinion/question/suggestion/hypothesis
target_entity_ids
event_mention_ids
stance_or_polarity
status_or_modality
state/state_evidence       # six values: unknown/planned/ongoing/resolved/failed/cancelled
closure_reason             # resolved/failed/cancelled/superseded/unknown
start_time/end_time       # unknown is valid and expected
information_value
event_completeness
timestamp
message_id
reply_to_message_id
evidence_span
confidence
```

人物未解析时保留会话内稳定的未知身份，不得删除主语后改写成无主体事实。

#### 5.7 `DiscourseThreadV2`

至少保存：

```text
discourse_thread_id
fragment_ids
claim_ids
conversation_opener_fragment_ids
speaker_ids/mentioned_person_ids/subject_ids
object_refs               # each explicit|inherited|unknown
state_sequence
closure_reason
start_fragment_id/end_fragment_id   # nullable
start_time/end_time                  # explicit|inherited|unknown
information_value
event_completeness
context_relation_ids
evidence_refs
uncertainties
```

线程可从任意中间片段开始，也可没有收尾；`start_*` 或 `end_*` 为 `unknown` 是正常数据状态。`discourse_thread` 只有在可审计关系和足够身份槽位下才提出 event candidate，不能把开放线程或沉默改写成已解决事件。

#### 5.8 `ContextRelationV2`

上下文关系必须带 `left_anchor_id`、`right_anchor_id`（`fragment|claim`）、上述七类 `label`、`evidence_refs`、`supporting_slot_codes`、`evidence_strength=strong|medium|weak|none`、`explicit_reply_present`、`time_evidence=strong|weak|none`、`confidence` 和 `provenance`。`explicit_reply_present=true` 只是一项强支持信号；关系裁决仍须说明对象/主体/状态连续性或冲突。时间邻近最多是 `weak`，不能单独创建关系或继承对象/状态。

旧 shadow 实现中的 `continuation`、`question_answer`、`contrast` 是兼容别名，导出金标准时分别规范化为 `continues`、`answers`、`contrasts`；`reply`、`object_inheritance`、`state_transition` 只能作为 supporting signal，不能替代七类 canonical label。

#### 5.9 `EventV2`

保存事件类型、核心对象、动作、时间区间、状态、诉求、参与者、`claim_ids`、`mention_ids`、支持/冲突证据、事件关系、置信度和不确定性。事件标题和摘要不属于事件身份字段。

#### 5.10 legacy 兼容

第一阶段提供显式适配函数，例如 `legacy_messages_to_v2(...)` 和 `v2_result_to_legacy_preview(...)`。兼容层只做字段映射：

- 不调用 legacy `sameTopic`、标题泛化或现有事件合并函数；
- 不把 legacy 结果当真值；
- 不修改生产接口响应；
- 缺失字段保留 `unknown` 并记录兼容告警，不静默补写；
- 兼容预览必须标记 `source="semantic_v2_shadow"`，不得伪装成 `ai_assisted`。

### 6. 目标处理流水线

2026-08-31 起，本节中的 fragment/claim/thread 细步骤均视为 `ConversationEpisode` 输入验收后的 DeepRead 下游细化；入口必须先经过 `ContentLedger → ConversationEpisode` 的无模型重建，不能再把 topic 分类当作第一步。职责与闸门以 §16.6 为准。

```text
不可变消息与会话上下文
  ↓
fragment 边界与 conversation_opener 识别
  ↓
speaker / mentioned_person / subject 归因
  ↓
object explicit|inherited|unknown 与 state 生命周期抽取
  ↓
claim 类型、情态、价值与 event_completeness 标注
  ↓
discourse_thread 构建与 context relation 裁决
  ↓
高召回候选生成
  ↓
硬冲突门槛
  ↓
候选对五分类裁决
  ↓
事件候选及其五分类关系（可为空）
  ↓
事件图构建
  ↓
受约束标题、摘要与事实核验
  ↓
topic_family / trend / presentation 派生
  ↓
人工审核、反馈与可重放评测
```

#### 6.1 碎片、人物、对象和状态抽取

先按可定位 span 切分 fragment，再填 speaker、mentioned_person、subject、object 和 state；不得先聚成 event 再倒推这些槽位。没有完整句法、明确对象或时间范围时保留 `unknown`，不使用常识补全。消息时间只作消息证据，不直接填入语义 start/end。

#### 6.2 discourse_thread 与上下文关系

按 `continues`、`elaborates`、`answers`、`contrasts`、`topic_shift`、`possibly_related`、`insufficient` 组织 fragment/claim。显式 reply 作为强证据但不设为前置条件；无 reply 的问题—回答只有在至少两个独立语义信号支持时才可建立 `answers`，时间接近本身不计入充分信号。跨长间隔仍可承接；对象或状态冲突应降级为 `contrasts`、`possibly_related` 或 `insufficient`。`topic_shift` 不关闭旧 thread，旧 thread 状态仍按终态证据判断。

#### 6.3 信息价值与事件完整性

对每个 fragment、claim 和 thread 独立记录 `information_value` 与 `event_completeness`。评分器必须允许低价值/高完整性、高价值/低完整性、低价值/低完整性和高价值/高完整性四种组合；任何一个轴都不能充当另一个轴的默认值或过滤器。

#### 6.4 高召回候选

关键词、稀疏表示、稠密向量、实体重合、时间邻近、回复链和同一链接可以生成候选。BERTopic、HDBSCAN 或相似工具若进入实验，也只能工作在候选召回或主题族层，不能直接产出最终事件。

#### 6.5 硬冲突门槛

候选对在以下关键槽位发生明确冲突时，默认禁止判为同一事件：

- 动作/事件类型：重置、注册、计费、封禁、故障、价格咨询不可仅凭相似词合并；
- 核心对象：GPT、Codex、multica、中转站、GitHub、邮箱分别规范化；
- 时间与状态：已恢复、仍等待、反复发生、历史经验保留差异；
- 诉求：问价格、报告故障、寻求注册帮助、表达风险判断分别保留；
- 身份与来源：发言人冲突、转述和原始经历必须可区分。

显式回复、引用、同一具体链接、相同错误码或同一具体动作是强合并证据，但仍不能覆盖明确的对象或状态冲突，也不是判为 `same_event` 的必需条件或单独充分条件。

#### 6.6 五分类事件关系

每个候选对必须输出且只输出一种关系：

```text
same_event
related_event
same_topic_only
unrelated
insufficient_context
```

裁决同时返回支持槽位、冲突槽位、证据引用、置信度和规则/模型版本。`related_event` 与 `same_topic_only` 只创建图边，不合并 `event_id`。`insufficient_context` 不得通过默认阈值自动升级。

#### 6.7 事件图

事件、主张、人物、对象和来源形成可审计图。共指、时间先后、因果、父/子事件、相关、反驳和转述是不同边类型。图中所有边保存来源与裁决版本；不得用一个 `cluster_score` 同时表达这些关系。

#### 6.8 标题、摘要与事实核验

摘要只能在事件边界确定之后运行，并遵循“先抽取、后生成、再验证”：

1. 只读取单一事件中的结构化 claim 和 evidence；
2. 标题采用事实型表达，不使用关键词命中后套用的泛化模板；
3. 每个可见句返回支持它的 `claim_id/message_id`；
4. 人物、对象、动作、时间、状态和不确定性逐项核对；
5. 无法对齐证据的句子删除或标为待核实。

未来可离线比较实体一致性、NLI 或 QA 校验，但第一阶段不引入模型。

### 7. 规则、向量与 AI 的职责

在新的交流过程入口下，本地规则仍只生成召回/拼接/媒体预处理/证据整理 artifact；最终交流形态、TopicThread、claim、state 和信息价值由已验收输入上的 DeepSeek 判断。具体边界以 §16.6 为准。

- 结构化规则基线负责可观察 span/metadata 的整理、候选召回、硬约束/证据格式校验和稳定回归，不输出最终语义真值。
- 向量或主题模型只负责候选召回和主题族发现；相似度不能成为事件身份真值。
- LLM 若进入后续实验，只处理经过脱敏的歧义候选对，输出五分类关系、槽位、证据和不确定性；它没有无约束最终合并权。
- AI 默认人工触发，不上传整个消息库，不写生产表，不创建发送任务。
- 模型理由不等于证据；只有可定位到输入消息的片段才算 provenance。

### 8. API、展示契约与前端不变量

第三版后端结果必须显式返回各层稳定 ID、schema/pipeline 版本、来源、证据和运行 ID。生产接入前先定义 v2 只读预览契约，不复用含义不明确的 `themes/events/detail_points` 拼装结构。

前端必须遵守：

- 不执行 `sameTopic` 或任何基于关键词、标题、摘要的语义合并；
- 不因来源字符串严格相等而静默丢弃另一条已声明来源的结果；来源采用受控枚举，并对未知值明确报错或降级；
- `lead` 只是一种 `presentation_role`，不会改变或吞并 `event_id`；
- 去重只允许对完全相同的稳定 `presentation_id` 或服务端声明的替代关系执行；
- 所有卡片都能回到 claim、message 和必要前后文；
- 废弃字段必须从 DTO、渲染器、主题详情页和契约测试共同移除，不能只改后端命名；
- 页面显示它实际采用的 `analysis_run_id/source/pipeline_version`，便于核对 API 与 DOM。

### 9. 金标准、评分协议与验收门槛

#### 9.1 金标准

从脱敏真实数据建立版本化数据集。标注单位不是“标题看起来是否合理”，而是：

2026-08-25 微信记录的私有数据目录、脱敏、抽样、JSONL schema、双人标注、must-not-link、仲裁、manifest 和完成定义统一遵循 [2026-08-25 微信记录金标准落地契约](D:/Project_Codex/Project_WeChatMoreFunction/docs/gold-standard-2026-08-25-contract.md)。真实消息及其脱敏派生物不得进入 Git；本文只引用契约，不包含真实原文。

- 消息中的 fragment、conversation_opener、人物角色、对象解析、动作、时间和状态；
- 说话人—主张—对象及 claim 类型、情态、价值、事件完整性和证据片段；
- discourse_thread 及 `continues`/`elaborates`/`answers`/`contrasts`/`topic_shift`/`possibly_related`/`insufficient` 上下文关系；
- 候选对的五分类事件关系；
- 最终应出现的 event/topic/trend/presentation 及证据回链。

必须刻意加入难负例：同一产品不同问题、同一关键词不同对象、相同事件不同状态、跨会话转述、长时间显式回复、无 reply 的问答、人物意见冲突、问候与实质问题同消息、只有父类词相同的无关消息。划分训练/开发/冻结测试集；冻结测试集不能被规则调参反复窥视。

#### 9.2 指标

- 事件边界：pairwise precision/recall/F1、B-Cubed、过度合并率、过度切分率；
- 灾难性错误：不同核心对象/动作/诉求被合成一张卡的比例，单独报告；
- 上下文结构：fragment span/type F1、speaker/mentioned_person/subject 角色准确率、object explicit/inherited/unknown 准确率、state/生命周期转移准确率；
- discourse_thread：上下文关系 macro-F1（按 label 与 `evidence_strength` 分层）、`insufficient` 保守率、无 reply `answers` 召回率、仅靠时间创建关系率（必须为 0）和 `topic_shift` 不误关旧线程率；
- 结构抽取：mention/论元 F1、说话人—主张—对象准确率、时间/情态/claim 类型准确率；
- 证据保真：可见句证据覆盖率、错误归因率、丢失实体率、无证据新信息率；
- 上下文证据：非 `unknown` 槽位 typed evidence 覆盖率、继承链完整率、未知原因记录率；
- 价值/完整性解耦：四种 `information_value × event_completeness` 组合的保留率和误过滤率；
- 摘要：逐主张 entailment、QA 一致性、矛盾状态保留和覆盖；
- 端到端：固定输入下 API、DOM、截图的卡片数量、稳定 ID、来源、标题、详情和证据一致性。

#### 9.3 首轮闸门

在金标准尚未形成前，所有数值门槛标记为“待基线后确定”，不得拍脑袋声称达标。但以下零容忍不变量立即生效：原消息丢失为 0；人物角色静默互换为 0；未知对象被关键词补齐为 0；无证据可见句为 0；沉默被标为 `resolved` 为 0；价值/完整性轴互相充当过滤器为 0；前端语义合并为 0；未知来源静默替换为 0；生产写入和发送调用为 0。

2026-08-27 的 Contextual Bundle 修订补充了 §15.8 的最低验收地板：它们是后续真实评测必须达到的下限，不把“待基线后确定”解释为可以低于地板。Stage1 v1 人工审计的 57.8% 只记录为规则基线，不能作为生产准入、不能用来抵消任一零容忍失败，也不能通过删样本或扩大 N/A 改善。

基线完成后，由冻结测试集确定灾难性错误合并、错误归因、证据覆盖和端到端一致性的最低门槛。任何一个关键门槛失败都阻止进入下一阶段，不能用平均分抵消。

#### 9.4 上下文证据与验收矩阵

下表只验收本阶段的人物、对象、状态和碎片上下文，不把它扩展为新的事件、标题或前端准入门槛：

| 能力 | 最小证据 | 通过条件 | 失败处理 |
| --- | --- | --- | --- |
| fragment 切分与开场 | `message_id`、span、`fragment_type`、evidence | 每个 fragment 可回到原消息；问候标为 `conversation_opener`，同消息实质内容另切 fragment | 标 `unknown` 并保留消息，不丢弃或强制成 event |
| 人物角色 | `speaker_id`、`mentioned_person_ids`、`subject_id` 及各自证据 | speaker、mentioned_person、subject 可不同；未知各自记录 | 禁止用 speaker 覆盖 subject，进入人工复核 |
| 对象解析 | `object_resolution`、object id、span 或继承来源 | `explicit` 有本片段 span；`inherited` 有来源 ID 和关系；无证据为 `unknown` | 不用主题/关键词补齐，降为 unknown |
| 状态生命周期 | state、state_evidence、transition evidence | 终态只由同一实例的完成/恢复/失败/取消/替代证据触发 | 沉默、topic shift、无后续保持开放或 unknown |
| 起止边界 | start/end value、source、evidence | start/end 可为 `unknown`；不把消息时间或采集首尾冒充语义边界 | 保留 unknown 与不确定性 |
| 上下文关系 | label、两端 anchor、typed evidence、provenance | 七类 label 合法；每条关系有证据；无 reply 仍可 `answers` | 证据不足用 `insufficient`，不自动升级 |
| 信息价值/完整性 | 两个独立枚举及证据 | 四种组合均可保留；低价值不等于删除，高价值不等于完整 event | 只修正对应轴，不联动改写另一轴 |
| 重放与审计 | pipeline/ruleset、输入 ID、evidence refs | 同输入同版本产生相同 fragment/thread/relation ID | 标记版本漂移，禁止静默覆盖 |

Stage 1 的 N/A 口径固定为：纯 `conversation_opener`、acknowledgement、reaction 或 context-only fragment 的 `event_completeness` 不进入事件完整性分母；没有状态证据的片段只记 `state=unknown`，不把未知当生命周期错误；无候选关系的片段不计关系召回，`insufficient` 单独报告；起止边界缺证据使用 `unknown`，不以 N/A 或首尾消息掩盖缺失。所有 N/A 必须保留原因。

### 10. A/B/C/D 离线实验

四个方案使用同一金标准、同一候选集、同一评分脚本和同一错误分类：

| 方案 | 机制 | 主要问题 | 风险 |
| --- | --- | --- | --- |
| A 结构化规则基线 | 实体/动作/时间/诉求硬门槛 + 回复链 | 修正事件身份定义本身能改善多少 | 召回偏低、别名不足 |
| B 实体与时间感知召回 | 稀疏 + 稠密 + 实体重合 + 时间衰减 | 提高召回是否会恶化误合并 | 父类语义压过细粒度冲突 |
| C LLM 歧义裁决 | 只处理候选对，输出五分类、槽位和证据 | 能否识别同主题不同事件 | 不稳定、成本高、理由可能不忠实 |
| D 混合方案 | 规则硬否决 + 向量召回 + LLM/交叉编码器裁决 | 能否达到关键门槛 | 管线复杂、版本治理要求高 |

第一阶段只实现 A 和用于其评测的公共 DTO/评分骨架；B/C/D 仅保留实验接口，不安装依赖、不调用外部模型。

### 11. 人工审计、反馈与重放

离线候选审计视图应并排显示原文、会话前后文、fragment、speaker/mentioned_person/subject、object resolution、state 生命周期、mention/claim 槽位、上下文关系、支持与冲突证据和裁决结果。人工可选择继续、补充、回答、对照、转题、可能相关或上下文不足；事件五分类仍按独立契约执行。

每次修订保存操作者、时间、原值、新值、理由和数据集版本。人工修订进入下一版金标准或开发集，不能静默改写原消息，也不能直接修改冻结测试标签。每次规则、阈值或模型更新均应支持按 `analysis_run_id` 重放。

### 12. 影子模式、上线与回滚

实施顺序固定为：

1. 纯函数 DTO 与 schema 契约；
2. 脱敏金标准和统一评分器；
3. A/B/C/D 离线对照与错误分析；
4. 候选审计页；
5. `semantic_v2_shadow` 影子运行；
6. 固定输入的 API/DOM/截图三方验收；
7. 仅在全部门槛通过后提出生产切换变更。

影子模式不影响首页排序、卡片、详情、通知或 legacy 结果。它的输出使用独立来源、运行 ID 和存储位置；若只能写现有生产表，则不得启用影子模式。

生产切换必须具有功能开关和可逆映射，旧结果保留到观察期结束。发生稳定 ID 漂移、灾难性误合并、错误归因、无证据句、来源错配、前端二次合并或性能超限时立即回退。回滚只切换派生读取源，不修改或删除原始消息。

### 13. 分阶段实施计划

#### P0：文档、DTO 与纯函数骨架

- 建立 `semantic_pipeline.py`，定义 v2 输入输出、fragment/discourse_thread/context relation 稳定 ID、provenance 和五分类事件关系；
- 实现 legacy 消息输入兼容与 v2 预览输出，不迁移数据库；
- 固定 GPT 重置/中转/multica/GitHub 注册等反例；
- 为确定性、不可变输入、证据定位、人物角色不混淆、对象继承、状态终止判据、沉默不等于 resolved、价值/完整性解耦和零生产副作用写测试。

验收：导入模块不会访问数据库、网络或生产配置；同输入同版本输出相同；测试运行不改变现有 API 和文件。

#### P1：金标准与评分器

- 制定脱敏、标注、复核和分歧仲裁说明；
- 建立 fragment/mention/claim/discourse_thread/context relation/五分类事件关系/最终展示标签；
- 输出逐错误类型报告和零容忍不变量。

#### P2：离线 A/B/C/D

- 先完成 A，再依次评估 B/C/D；
- 固定依赖、版本、阈值、成本和延迟；
- 任何模型引入都需单独评审许可证、中文域、Windows/CPU 和隐私边界。

#### P3：审计工具与反馈闭环

- 候选对和事件图人工审计；
- 保存判定版本和操作审计；
- 支持离线重放，不接生产展示。

#### P4：影子模式与端到端验收

- 使用独立来源运行，不影响用户页面；
- API、DOM、截图和证据回链一致；
- 验证前端没有语义合并和来源偷换。

#### P5：生产接入提案

只有 P0—P4 全部通过后才能形成单独的生产切换提案。提案必须列明门槛结果、观察期、功能开关、回滚步骤和数据兼容性；本文不预先授权切换。

### 14. 开放问题与研究限制

- 主产品究竟优先呈现事件日报、主题观察，还是人物观点与行动线索；三者共用证据层，但合并和版面标准不同。
- 当前优先把错误合并成本设为高于漏掉小事件；具体权重在金标准基线后确认。
- 私聊、群聊和跨群转述何时可以共享 `event_id`，必须由强证据和真实样本确定。
- 标题默认事实型；是否允许更强编辑化表达，应在证据审核成熟后单独决策。
- 多数公开事件共指基准来自新闻或英文文本，不能把论文分数外推到中文微信短消息。
- 图片、语音、表情、反讽、谐音和群体黑话是独立评测问题，基础事件身份达标前不扩展。

## 15. Contextual Bundle 可逆理解架构修订（2026-08-27）

本节是第三版语义架构的新增权威修订；它补足“如何在不提前丢失上下文的前提下分配理解成本”。如与本文件较早的候选路由描述冲突，以本节和 [Contextual Bundle 实施计划](D:/Project_Codex/Project_WeChatMoreFunction/docs/contextual-bundle-implementation-plan.md) 为准。它仍然不授权当前生产 API、首页、事件标题或前端切换。

### 15.1 不可变消息、权威元数据与分层边界

原始消息是不可变事实：源消息正文/媒体引用、来源消息 ID、会话作用域、采集顺序、适配器版本和原始消息哈希一旦登记，只能追加校正记录，不能被摘要、LLM 输出、人工反馈或后续解析覆盖。脱敏 release 可以只提供脱敏正文，但其 `message_id`、`body_digest` 和 `source_snapshot_fingerprint` 必须能够回到不可变源记录；脱敏不是对源消息的就地修改。

`account_id`、`chat_id`、`chat_type`、`speaker_id`、`direction`、消息时间、`sequence_in_chat`、`reply_to_message_id`、`source_mode` 和媒体类型由源适配器/消息注册表的元数据投影权威提供。正文中的自称、转述或推测不能覆盖消息元数据中的 speaker；如果适配器元数据冲突，保留冲突及版本并将受影响语义槽位标为 `unknown`，不得静默选择一个值。

固定分层为：

```text
immutable message + authoritative metadata
        ↓
low-cost message registry
        ↓
adaptive semantic gate
        ↓
fragment / person / argument / claim
        ↓
multi-scale candidate windows
        ↓
DialogueBundle + open_context_snapshot
        ↓
discourse_thread + typed context relations
        ↓
optional event candidate / event
```

它与新的 `ContentLedger → ConversationEpisode → TopicThread（可选） → DeepRead` 入口一致：registry 和 gate 只负责保留、索引和调度；bundle 只是可重建的上下文视图；`event` 仍是有充分证据时才产生的可选派生物。任何层都不能跳过 evidence，直接从关键词或标题创建 event。

### 15.2 低成本 message registry 与接口

message registry 是每条新消息都应进入的低成本、可追加元数据索引，不是语义真值，也不存放另一份可变正文。它至少记录：

```text
message_id
account_id/chat_id/chat_type
speaker_id/direction/reply_to_message_id
source_mode/adapter_version/sequence_in_chat
event_time/event_time_precision
message_type/body_digest/body_length_estimate/language_hint
metadata_fingerprint/metadata_revision
registry_state              # registered|gated|replayed|error
gate_channel/gate_reason_codes
activation_cues             # reply, entity, state, query, bridge, etc.
artifact_refs               # fragment/bundle/snapshot IDs, nullable
schema_version/pipeline_version/created_at
```

正文只通过不可变 `message_id` 或受权限保护的 `content_ref` 读取；registry 的写入应是幂等的。建议接口如下，均返回新版本或只读快照，不原地修改源记录：

```text
register_message(source_message, authoritative_metadata) -> RegistryEntry
get_message(message_id, scope) -> ImmutableMessageView
get_registry_entry(message_id, scope) -> RegistryEntry
append_metadata_revision(message_id, correction, reason) -> RegistryEntry
record_gate_decision(message_id, decision) -> RegistryEntry
```

registry 写入失败时不能丢消息或假装已完成：将源记录留在可恢复输入队列，重试使用同一幂等键；若仅语义索引失败，仍保留消息和权威元数据，并把语义状态设为 `error`/`cold_recoverable`。

### 15.3 可逆 adaptive semantic gate

gate 只决定“何时、以什么成本重新看”，不决定“消息属于什么事件”。四个且仅四个路由通道如下：

| channel | 进入条件与作用 | 默认成本 | 可逆激活线索 |
| --- | --- | --- | --- |
| `immediate` | 当前 turn、显式 reply、明确对象/状态变化、用户查询或高风险硬约束命中；尽快建 fragment/claim/bundle | registry + 稀疏规则/索引；默认不调用 LLM | 新消息、reply、对象/状态证据、用户查询 |
| `pending_context` | 有可解释的承接候选但证据不够，等待未来消息或更大窗口 | 只保留候选、未决槽位和激活线索 | 后续承接、对象补全、问题回答、状态转移 |
| `background` | 不阻塞当前交互、值得在预算内异步扩展的 bundle | 稀疏优先，必要时 dense 召回，再做受限 pairwise | 新证据、预算空闲、人工复核 |
| `cold_recoverable` | 当前不值得计算或暂时失败，但以后仍可被查询/桥接重新激活 | 仅 registry 元数据、哈希、scope 和 cue；不删除正文/证据 | 查询、显式 bridge、同一消息再次出现、重放任务 |

每一条 gate 决策必须保存 `from_channel`、`to_channel`、触发器、成本预算、输入版本、时间和可重放 `reason_codes`。典型状态机为：

```text
registered
   ├─> immediate ───────────────┐
   ├─> pending_context ──┐      │
   ├─> background ───────┼─> replay/reevaluate ─> immediate|pending|background
   └─> cold_recoverable ─┘      │
                                └─> cold_recoverable (budget/error/expiry)
```

任何通道都不能表示 `resolved`，也不能删除 fragment、claim 或未决线程。`pending_context` 不是失败，`cold_recoverable` 不是无关；两者都必须保留最小重新激活线索。时间邻近只能是弱 cue，不能单独把消息升为 `immediate` 的语义承接或 `same_event`。

### 15.4 动态 DialogueBundle 与 `open_context_snapshot`

`DialogueBundle` 是一次分析运行的动态、可重建上下文包，不是永久事件簇。它以一个或多个 fragment/claim 为 anchor，带候选窗口、未决槽位、关系候选、预算、scope 和 provenance。建议最小字段为：

```text
bundle_id
analysis_run_id/schema_version/pipeline_version
anchor_fragment_ids/anchor_claim_ids
candidate_window_id/window_scale
member_message_ids/member_fragment_ids/member_claim_ids
member_bundle_ids              # fragment/claim 可多包归属
open_context_snapshot_id
speaker_refs/mentioned_person_refs/subject_refs/object_refs
state_refs/unresolved_slot_codes
candidate_pair_ids/context_relation_ids
chat_scope/cross_chat_bridge_refs
evidence_refs
budget_class/gate_channel
status: open|provisional|abstained|materialized|superseded
uncertainties/provenance
```

每条 fragment 和 claim 都可以属于多个 bundle；bundle 之间不复制或重写源消息，必要时只引用同一证据。`primary_bundle_id` 只能是展示/调度方便的派生字段，不能限制真实的多包证据关系。一个 fragment 在不同窗口中可分别支持 `continues`、`elaborates` 或 `possibly_related`，裁决要带 bundle 与证据版本。

`open_context_snapshot` 是接收新消息或重放时，对当时仍开放的 discourse thread、未决人物/对象/状态槽位、最近证据、已知排除项和下一步 activation cue 的不可变快照：

```text
snapshot_id
captured_at/analysis_run_id
open_thread_ids
unresolved_slots: [{thread_id, slot, value, resolution, evidence_refs}]
recent_fragment_ids/recent_claim_ids
pending_relation_candidates
activation_cues
excluded_candidate_reasons
snapshot_version/provenance
```

快照只是“当时还开放什么”的证据，不是把沉默解释为关闭；新证据应生成新快照并保留旧快照，不能覆盖历史判断。

候选窗口使用多尺度、逐级扩展且同一 scope 内优先：

1. `W0`：同一 message 内的相邻 fragment/claim；
2. `W1`：同 chat 的局部前后文，允许跨 opener、ack、context-only 或插入句；不能只 zip 相邻 fragment；
3. `W2`：同 chat 的会话/日级窗口和 open snapshot；
4. `W3`：更长时间的稀疏召回窗口，长间隔不是硬切断；
5. `W4`：跨 chat 的显式 bridge 窗口，只在 reply/quote/forward/稳定外部引用等证据下建立候选。

跨 `chat_id` 默认硬隔离：同一 speaker、关键词、时间或 embedding 不能创建上下文关系或合并 event。即使存在 bridge，也只能形成可审计候选；跨 chat 的 `same_event` 需要明确 bridge、对象/实例兼容和独立正证据，默认进入人工仲裁。不同 `account_id` 绝不共享身份作用域。

### 15.5 bundle 级 LLM 固定 schema 与检索/裁决职责

若实验引入 LLM，它一次只接收一个已脱敏的 `DialogueBundle` 及 bundle 内有限候选对，必须输出固定 JSON schema；不得输出自由文本标题、未带证据的结论或直接写入 event 表。最小输出结构为：

```json
{
  "bundle_id": "BUNDLE_...",
  "schema_version": "bundle_llm_v1",
  "abstain": false,
  "slot_updates": [
    {"slot": "object|subject|state|time|intent", "value": "...",
     "resolution": "explicit|inherited|unknown",
     "evidence_refs": ["..."], "confidence": "high|medium|low"}
  ],
  "context_relations": [
    {"left_anchor_id": "...", "right_anchor_id": "...",
     "label": "continues|elaborates|answers|contrasts|topic_shift|possibly_related|insufficient",
     "evidence_refs": ["..."], "evidence_strength": "strong|medium|weak|none"}
  ],
  "pairwise_decisions": [
    {"candidate_pair_id": "...", "label": "same_event|related_event|same_topic_only|unrelated|insufficient_context",
     "evidence_refs": ["..."], "confidence": "high|medium|low"}
  ],
  "unresolved_slot_codes": ["..."],
  "model_provenance": {"model_id": "...", "prompt_version": "..."}
}
```

`abstain=true`、空值或 `unknown` 是合法结果；每个非未知槽位都必须回到 bundle 中的 typed evidence。schema 校验失败、超时、内容越界或模型不可用时，整包降为 `insufficient`/`pending_context`，保留原 bundle，不部分采纳自由文本。

检索和裁决职责固定为：稀疏表示优先，dense 只按需用于候选召回，LLM 只做 bundle 内 pairwise 歧义裁决，embedding 永远不是事件身份判定器。规则只负责元数据归一化、scope/硬约束、schema/证据校验和模型/服务失败 fallback；规则分数、same-segment、同 speaker、时间接近和主题词不能单独生成上下文关系或 event。

### 15.6 模块、API 与数据流

实现阶段应保持模块边界和幂等输入输出；本节是接口契约而非本次代码变更：

```text
message_registry.register/get
  → semantic_gate.route/reevaluate
  → fragment_claim.extract
  → candidate_window.expand/retrieve
  → dialogue_bundle.build
  → context_snapshot.open
  → context_relation.decide
  → bundle_llm.judge_pairwise (optional, budgeted)
  → discourse_thread.materialize
  → event.materialize_if_sufficient (optional)
  → evaluator.replay/report
```

建议 API：

```text
route_message(registry_entry, open_snapshot, budget) -> GateDecision
extract_fragments(message_view, metadata) -> FragmentSet
build_candidate_windows(anchor, scales, scope, budget) -> CandidateSet
build_dialogue_bundle(anchor, candidates, snapshot) -> DialogueBundle
open_context_snapshot(threads, unresolved_slots, cues) -> Snapshot
judge_bundle(bundle, candidate_pairs, model_config) -> FixedBundleDecision
validate_and_materialize(bundle, decision) -> ThreadUpdate/EventCandidate
replay(run_id, dataset_version, component_versions) -> EvaluationReport
```

所有返回值都带稳定 ID、schema/pipeline/ruleset 版本、scope、证据和不确定性；任何写入使用 `analysis_run_id + input_fingerprint` 幂等。`event.materialize_if_sufficient` 失败时只保留 fragment/claim/thread，不能为了满足下游字段伪造 event。

### 15.7 状态生命周期、边界和信息价值

人物、对象、状态仍先于 event：`speaker`、`mentioned_person`、`subject` 独立记录；对象解析固定为 `explicit|inherited|unknown`；state 固定六值 `unknown|planned|ongoing|resolved|failed|cancelled`。`historical` 只进入 `temporal_qualifier`，不能成为 state 或终态。

状态只能按同一对象/实例的证据转移：

```text
unknown ──(计划证据)────> planned
unknown ──(开始/进行证据)─> ongoing
planned/ongoing ──(完成/恢复)─> resolved
planned/ongoing ──(明确失败)──> failed
planned/ongoing ──(明确取消/替代)─> cancelled
```

终态证据必须能回链到同一实例；沉默、没有后续消息、对方未回复、topic shift、窗口结束或采集结束均不改变状态。没有终态证据时保持 `unknown|planned|ongoing`。fragment/thread 可以只有开头、只有结尾或两端均 `unknown`，这不是数据缺陷。`information_value` 与 `event_completeness` 独立评分；纯 `conversation_opener`/ack/context-only 的完整性为 `not_applicable`，高价值片段仍可能是 partial/unknown。

### 15.8 成本预算、语义评估矩阵与零容忍项

成本按消息、候选、bundle 和模型调用分别核算，不以一个“总相似度”代替预算：

```text
C_total = C_registry + C_gate + C_sparse + C_dense + C_llm + C_evaluation
C_llm = (T_in / 1e6) * P_in + (T_out / 1e6) * P_out + N_request * P_request
C_dense = N_embed * P_embed + C_vector_index
C_cpu = N_message * c_registry + N_candidate * c_sparse
```

其中 `P_*` 由实际服务价目/本地计算基准注入，不能写死到语义判断。每 1,000 条新消息的默认软预算（可由 manifest 覆盖但必须记录）为：全部消息完成 registry 与稀疏索引；每消息最多保留 20 个稀疏候选；dense 仅覆盖最多 200 个歧义 bundle、每包 top-20 召回；LLM 最多处理 50 个 bundle、每包最多 2,000 input/400 output tokens；`cold_recoverable` 默认 0 次模型调用，只有查询或明确 bridge 才激活。预算超限时扩展到下一通道，而不是删除消息或改成 unrelated。

最低验收地板与指标分开记录成本和语义：

| 层 | 语义指标/最低地板 | 成本与运行指标 | 阻断条件 |
| --- | --- | --- | --- |
| 源与 registry | 原消息、权威元数据、scope、hash 保留率 100%；跨 chat 泄漏 0 | registry 写入成功率 100%，幂等重复率 0 | 任一源覆盖、scope 串台或元数据静默改写 |
| gate | 四通道可逆转移可重放率 100%；冷存重新激活不丢证据 | immediate 不阻塞导入；每千消息预算不超默认软上限 | 不可逆丢弃、失败后无 cue、预算超限仍强行调用 |
| fragment/claim | span/type ≥98%；speaker/mentioned/subject 角色准确率 ≥0.95；object resolution macro-F1 ≥0.90；六值 state macro-F1 ≥0.90；终态误报 0 | 单消息 registry+稀疏处理 p95 ≤ 100 ms（基准机记录） | 角色静默互换、未知被补齐、沉默→resolved |
| discourse/bundle | opener 保留 100%；无 reply 承接 precision ≥0.90；context macro-F1 ≥0.85；仅时间建边 0；typed evidence 覆盖 100% | bundle 构建 p95 ≤ 500 ms（不含可选 LLM） | same-segment/time-only 强连、bundle 无证据、跨 chat 偷连 |
| LLM（实验） | 固定 schema 合法率 100%；非未知槽位证据覆盖 100%；abstain 可重放 | 按 token/request 预算；超时率和每包 token 单独报告 | 自由文本被采纳、schema 失败仍写 event |
| event/真实评测 | `same_event` precision ≥0.98；灾难性错误合并 0；五分类 macro-F1 ≥0.85 | dense/LLM 只在候选上运行，成本与召回曲线同时报告 | 任一 MNL 穿透或证据缺失 |
| API/DOM/E2E | 稳定 ID、来源、标题/详情、证据链一致率 100%；前端语义合并 0 | 固定输入 p95、失败率、重放差异均记录 | DOM 与 API 不一致、来源偷换、生产写入 |

上述地板是发布前最低要求；样本不足或 N/A 不能被算成通过。Stage1 v1 人工审计的 57.8% 只作为 `rules_v1` 规则基线，不能接生产、不能稀释地板，也不能用平均分掩盖零容忍错误。零容忍项包括：原消息丢失/覆盖、权威元数据串台、跨 chat 无 bridge 建边、仅时间或 same-segment 强连、未知对象/人物/状态被强填、沉默标 `resolved`、无证据可见结论、LLM 自由文本落库、前端二次语义合并、生产发送/写入副作用。

### 15.9 失败回退与降级顺序

失败回退必须保留事实和重新激活线索，并可通过同一 `input_fingerprint` 重放：

| 失败 | 回退 | 不允许 |
| --- | --- | --- |
| metadata 缺失/冲突 | 保留 message，槽位为 `unknown`，追加 metadata revision | 用正文或同 chat 默认值覆盖权威字段 |
| registry/索引暂时失败 | 可恢复队列 + 幂等重试，必要时 `cold_recoverable` | 确认已分析或丢掉消息 |
| dense/向量不可用 | 使用稀疏候选；保留 activation cue | 用 embedding 相似度直接判 unrelated/same_event |
| bundle 超预算 | 缩小窗口、转 `pending_context` 或 background | 删除未决 fragment/claim |
| LLM 超时/越界/schema 失败 | `abstain` + `insufficient`/pending，保留 bundle | 采纳部分自由文本或默认合并 |
| context relation 冲突 | 降为 `possibly_related`/`insufficient`，保留双方证据 | 强行选 continues/answers 或关闭 thread |
| event materialization 失败 | 输出 fragment/claim/thread，等待重放 | 伪造事件标题或补齐对象/状态 |
| E2E/API/DOM 不一致 | 阻断发布，回读独立 artifact | 让前端自行合并或静默替换来源 |

### 15.10 十二项完整交付与最终链路

最终交付拆为以下 12 项，必须按计划逐项产生 artifact、评测记录和验收签名；完成文档不代表它们已经完成：

1. **契约冻结**：message/metadata、Fragment/Person/Argument/Claim、Bundle、Thread、Event、relation 和固定 LLM schema；
2. **不可变源与 registry**：源消息保留、权威元数据、幂等索引、scope/哈希与重放指纹；
3. **上下文抽取**：fragment 边界、conversation_opener、人物角色、对象解析、六值 state 和未知边界；
4. **adaptive gate**：四通道路由、预算、激活 cue、失败回退和可逆状态机；
5. **动态 bundle**：多尺度窗口、跨 opener/context-only 承接、多 bundle/claim 证据、open snapshot；
6. **候选召回**：稀疏优先、dense 按需、跨 chat 硬隔离和候选窗口审计；
7. **bundle 级裁决**：固定 schema、LLM pairwise、可 abstain、证据/版本/成本记录；
8. **图与事件派生**：context relation、discourse_thread、足够证据时的 event，及状态/边界不变量；
9. **真实金标准评测**：脱敏真实样本、合成回归集、A/B/C/D、成本+语义矩阵和错误分层；
10. **人工审计与重放**：抽样审计、反馈、版本化修订、冻结测试隔离和全链路 replay；
11. **影子/API/DOM/screenshot E2E**：独立来源、固定输入一致性、证据回链和无前端语义合并；
12. **生产接入与运维**：只读功能开关、观察期、成本/质量监控、回滚、数据兼容与 incident runbook。

### 15.11 分阶段最低门与未达标继续迭代规则

每个交付物按 `planned → in_progress → evaluated → accepted|blocked` 管理；只有当前项的最低门和所有零容忍项通过，才能进入下一项。未达标时：

1. 冻结本次 artifact、输入指纹、版本、成本和错误码，不覆盖上一版；
2. 按错误类型补充 synthetic 回归样例；涉及真实评测时只从授权脱敏集抽样，不能读取或发布私有正文；
3. 先在 development 调整，再用未见的 frozen_test/holdout 重跑；不得删难例、扩大 N/A、改写原消息或窥视 frozen test 调参；
4. 若失败来自预算，先降低 dense/LLM 通道或转 pending/cold；若来自语义，保留 unknown/insufficient 并修复证据/窗口，不用关键词阈值硬填；
5. 重新报告“本项门槛、回归样例、成本变化、零容忍状态和剩余风险”。同一关键门连续失败时继续迭代，不得以 57.8% 规则基线、平均分或产品急迫性豁免；只有所有门通过并完成真实评测、影子链路和 E2E，才可提交生产切换提案。

实施细节、每项输入/输出 artifact、默认预算、负责人角色、依赖和退出条件见 [docs/contextual-bundle-implementation-plan.md](D:/Project_Codex/Project_WeChatMoreFunction/docs/contextual-bundle-implementation-plan.md)。

## 16. Workstream K1：ContextPacket 与分阶段 DeepSeek 契约（2026-08-28）

> 状态：设计/契约冻结，尚未授权实现或生产接入。本节是对 §15 中实验性 bundle LLM 描述的收敛：K1 的本地职责是上下文工程和高召回候选，不是最终语义裁决。`bundle_llm_v1` 只作为旧 artifact 的读取兼容格式，新的实现不得继续生成一次包含全部语义槽位的 17 字段大 schema。

### 16.1 职责边界与分层

本地管线只负责保留不可变消息、权威微信元数据、fragment/claim 引用、跨尺度候选窗口和可解释激活线索。它可以扩大召回、保留重叠上下文和报告候选原因，但不能仅凭规则、时间、same-segment、关键词、embedding 或本地分数决定最终 topic/thread/claim 真值，也不能直接创建、合并或关闭 event。

本修订的固定主分层（旧字段名只作兼容）为：

```text
ContentLedger（不可变内容账本）
          ↓  无模型输入重建
ConversationEpisode / 交流形态（过程证据包）
          ↓  仅在输入验收 accepted 后
TopicThread / 话题流（可选的语义流）
          ↓
DeepRead（DeepSeek 语义判断；沿用 Stage B → Stage C）
```

`ContentLedger` 是消息注册表与不可变内容引用的概念合并名；`ConversationEpisode` 只重建“消息如何交流”，不把交流作用、内容和价值压成一个主题标签。`TopicThread` 是 DeepSeek 判断后的可选话题流，闲聊、纯确认、反应或证据不足时可以没有它；没有主题不是失败。`ContextPacket` 仍是跨层、可丢弃且可重建的输入工程产物，不是新的语义真值层。现有 Stage B 仍按已有 topic/thread 输入输出带证据的 claim 槽位，Stage C 仍只做一致性审计；两者 schema 与禁止项不变，三阶段都不能跳过 evidence，也不能直接把结果写成生产 event/title。

### 16.2 `ContextPacket v1` 双区契约

每个 packet 必须明确分隔 `authoritative_facts` 与 `candidate_context`。前者只能来自消息源/适配器的权威投影或可定位的原始 span；后者全部是候选召回线索，带原因和置信度，但不等于真值。

```json
{
  "packet_id": "PACKET_...",
  "context_packet_version": "context_packet_v1",
  "analysis_run_id": "RUN_...",
  "scope": {"account_id": "...", "chat_id": "..."},
  "anchor_fragment_ids": ["..."],
  "anchor_claim_ids": ["..."],
  "boundary": {
    "start": {"resolution": "explicit|unknown", "message_id": "...", "evidence_ref": "..."},
    "end": {"resolution": "explicit|unknown", "message_id": "...", "evidence_ref": "..."}
  },
  "window": {"scale": "W0|W1|W2|W3|W4", "message_ids": ["..."], "fragment_ids": ["..."], "claim_ids": ["..."]},
  "authoritative_facts": {
    "message_metadata": [{"message_id": "...", "chat_id": "...", "speaker_id": "...", "direction": "...", "message_type": "...", "event_time": "...", "sequence_in_chat": 0}],
    "reply_edges": [{"message_id": "...", "reply_to_message_id": "...", "evidence_ref": "..."}],
    "quote_edges": [{"message_id": "...", "quoted_message_id": "...", "span_start": 0, "span_end": 0, "evidence_ref": "..."}],
    "fragment_spans": [{"fragment_id": "...", "message_id": "...", "span_start": 0, "span_end": 0, "evidence_ref": "..."}],
    "media": [{"message_id": "...", "media_type": "...", "availability": "available|unavailable|not_present|unknown", "preprocess_status": "...", "missing_reason": "...", "semantic_evidence": false}]
  },
  "candidate_context": {
    "continuity_candidates": [{"left_id": "...", "right_id": "...", "reason_codes": ["..."], "confidence": "high|medium|low"}],
    "qa_candidates": [{"question_id": "...", "answer_id": "...", "reason_codes": ["..."], "confidence": "high|medium|low"}],
    "person_history": [{"person_ref_id": "...", "message_ids": ["..."], "reason_codes": ["..."]}],
    "object_history": [{"object_ref_id": "...", "message_ids": ["..."], "resolution": "explicit|inherited|unknown", "reason_codes": ["..."]}],
    "state_history": [{"state_ref": "...", "message_ids": ["..."], "reason_codes": ["..."]}],
    "open_threads": [{"discourse_thread_id": "...", "unresolved_slot_codes": ["..."], "reason_codes": ["..."]}],
    "activation_cues": [{"cue": "reply|quote|object|state|query|bridge|replay", "source_id": "...", "confidence": "high|medium|low"}],
    "candidate_reasons": [{"candidate_id": "...", "reason_codes": ["..."], "confidence": "high|medium|low", "evidence_refs": ["..."]}]
  },
  "overlap_group_id": "...",
  "status": "open|pending|complete|abstained",
  "evidence_refs": ["..."],
  "provenance": {"input_fingerprint": "...", "pipeline_version": "...", "ruleset_version": "..."}
}
```

字段约束：

- `authoritative_facts.message_metadata` 的 chat、speaker、方向、时间、顺序、reply、quote 和 span 只可追加修订，不得被 candidate_context 或模型输出覆盖；权威冲突必须保留 revision 并影响到 `unknown`。
- `candidate_context` 可以包含连续窗口、问题—回答候选、同人物/对象历史、状态历史、未决 thread 和 activation cue，但每项都必须带 `reason_codes`/`confidence`，不能把候选 ID 当事实 ID。
- 一个 fragment/claim 可以进入多个 packet；packet 可以重叠，`overlap_group_id` 只用于审计和去重，不表示同一 event。`start`、`end` 均为 `unknown`，或只有一端可知，都是合法且常见的截断/开放边界。
- packet 默认优先同一 `account_id/chat_id`。跨 chat 只能保留显式 bridge 候选，跨 account 不共享身份作用域；时间、same-segment、同 speaker 或主题词不能单独建立强承接。

### 16.3 DeepSeek 三阶段小 schema

每阶段使用独立 request、独立 response schema、独立缓存命名空间和独立失败状态。禁止把 mapping、人物/对象/状态/claim 和 consistency 一次塞进单个 17 字段大 schema。

| 阶段 | 输入/唯一职责 | 允许输出 | 明确禁止 |
| --- | --- | --- | --- |
| Stage A：TopicThread mapping（可选） | 已通过输入重建验收的 `ContextPacket v1` 与候选 IDs | 可选 `topic_id`/`discourse_thread_id`/`message_id`/`fragment/claim/context ID` 分组，以及未映射、无主题和未知 ID；`topic_groups=[]` 是合法结果 | 未验收输入、强制生成 topic、人物、对象、动作、状态、claim 真值、event/title 或自由文本理由 |
| Stage B：per-topic claims | packet + Stage A topic/thread IDs | 每 topic 的 speaker/mentioned/subject、object、action、state、claim_type、modality 和 evidence refs | 跨 topic 自动合并、无证据补全、直接写 event/title |
| Stage C：consistency audit | packet + A/B versioned outputs | 冲突、漏项、错误合并、unknown 保留项及其证据/审计状态 | 用审计结果偷偷修正 A/B、把 silence 变 resolved、生成最终 event |

Stage A 的最小 response 形状为（接口名 `stage_a_map_topic_threads`；旧 `stage_a_map_topics` 仅作兼容别名）：

```json
{
  "decision_id": "A_...",
  "packet_id": "PACKET_...",
  "schema_version": "stage_a_topic_thread_v1",
  "topic_groups": [{"topic_id": "...", "message_ids": ["..."], "context_ids": ["..."]}],
  "thread_groups": [{"discourse_thread_id": "...", "message_ids": ["..."], "context_ids": ["..."]}],
  "topic_absent_episode_ids": ["..."],
  "unmapped_message_ids": ["..."],
  "unknown_context_ids": ["..."],
  "evidence_refs": ["..."],
  "validation_status": "valid|invalid|timeout|unavailable",
  "fallback_action": "complete|pending_context|abstained"
}
```

`topic_absent_episode_ids` 是对旧 Stage A wire 的可选增量字段；旧读取器缺失该字段时，仍以空 `topic_groups` 和 `unmapped_message_ids` 表示无主题。无主题、未知主题和待补上下文必须可区分，且都不得被当作 `unrelated`。

Stage B 的最小 response 形状为：

```json
{
  "decision_id": "B_...",
  "packet_id": "PACKET_...",
  "topic_id": "...",
  "schema_version": "stage_b_claims_v1",
  "claims": [{
    "claim_id": "...",
    "speaker_ref": "...|unknown",
    "mentioned_person_refs": ["..."],
    "subject_ref": "...|unknown",
    "object_ref": {"id": "...|unknown", "resolution": "explicit|inherited|unknown"},
    "action": "...|unknown",
    "state": "unknown|planned|ongoing|resolved|failed|cancelled",
    "claim_type": "fact|opinion|question|suggestion|hypothesis|unknown",
    "modality": "certain|probable|possible|required|desired|unknown",
    "evidence_refs": ["..."],
    "unknown_field_codes": ["..."]
  }],
  "validation_status": "valid|invalid|timeout|unavailable",
  "fallback_action": "complete|pending_context|abstained"
}
```

Stage C 的最小 response 形状为：

```json
{
  "decision_id": "C_...",
  "packet_id": "PACKET_...",
  "schema_version": "stage_c_consistency_v1",
  "stage_a_decision_id": "A_...",
  "stage_b_decision_ids": ["B_..."],
  "conflicts": [{"kind": "slot|attribution|state|scope", "left_ref": "...", "right_ref": "...", "evidence_refs": ["..."]}],
  "omissions": [{"expected_ref": "...", "reason_code": "...", "evidence_refs": ["..."]}],
  "merge_errors": [{"left_ref": "...", "right_ref": "...", "reason_code": "...", "evidence_refs": ["..."]}],
  "unknown_preservation": [{"ref": "...", "slot": "...", "evidence_refs": ["..."]}],
  "audit_status": "pass|fail|uncertain|pending",
  "validation_status": "valid|invalid|timeout|unavailable",
  "fallback_action": "complete|pending_context|abstained"
}
```

### 16.4 Evidence、缓存 key 与失败回 pending

所有阶段使用 typed evidence handle：`message`、`fragment`、`claim`、`reply`、`quote` 或带半开区间的 `span`。Stage A 的证据只证明 ID/分组来自 packet，不允许凭 ID 解释人物或状态；Stage B 每个非 `unknown` 的人物、对象、动作、state、claim_type 和 modality 都必须至少有一个可回到 packet 的 typed evidence；Stage C 每个冲突、漏项、错误合并和 unknown 保留结论都必须引用相关阶段输出及其输入证据。没有 evidence 就返回 `unknown`/`uncertain`，不能用自然语言理由替代。

阶段缓存 key 独立计算，且禁止跨 model、prompt、schema 或 ruleset 复用：

```text
K_stage = H(
  stage_name + stage_schema_version + canonical_context_packet_fingerprint +
  input_stage_decision_ids + model_id + model_version + prompt_version + ruleset_version
)
```

只有 schema 合法、evidence 校验通过且状态为 `complete` 的结果才可 `put`；invalid、timeout、provider unavailable、超预算、输出越界和 `pending_context` 不写入完成缓存。Stage A 失败保留 packet 并回 `pending_context`；Stage B 失败保留已验证的 A 结果但 B 为 pending；Stage C 失败保留 A/B 并将 consistency 记为 `unknown|pending`。所有失败都必须记录 reason code、输入 fingerprint、预算、重试次数和 activation cue，便于可逆重放。

### 16.5 K1 调试指标与 N/A 规则

每次 run 必须分开报告 packet、candidate、stage request、validated output、pending 和 token/latency，不能把候选数当调用数，也不能把阶段失败伪装成 `unrelated`：

| 指标 | 定义 | N/A/诊断规则 |
| --- | --- | --- |
| `packet_context_recall` | 金标准需要的 message/fragment/claim context units 中，至少出现在任一合规 packet 的比例 | 没有已标注 context units 时 N/A；不得用“无候选”代替 0 |
| `distractor_rate` | packet 内无关 candidate units / packet candidate units，总体及 W0-W4、chat scope 分层 | packet 无 candidate units 时 N/A；必须同时报告绝对计数 |
| topic split/merge | Stage A 将一个 gold topic 拆分或把多个 topic 合并的比例，分别报告 split 与 merge | 无 topic gold 或仅单 topic 样本时对应项 N/A；不能用 Stage B claim 分数抵消 |
| claim/evidence | claim 字段准确率、非未知字段 typed-evidence coverage、evidence handle precision/recall | 某槽位没有 evidence gold 时只对可评分槽位计算；未知保留单独计数 |
| stage success | 每阶段 validated complete outputs / started provider requests，并分 provider attempts、失败、pending、budget deferred | 零请求记 N/A 而非 100%；候选数不作分母 |
| cost | packets、topics、stage requests、input/output tokens、retries、latency、CPU/embedding 次数及每 1,000 message 成本 | provider 不可用时报告实际尝试和 0 output，不以估算成功替代 |

K1 的立即零容忍项仍为：权威 metadata/顺序/reply/span 丢失或覆盖、跨 chat/account 偷连、仅时间或 same-segment 强连、未知边界/对象/人物/state 被强填、沉默变 `resolved`、无 typed evidence 的非未知结论、Stage A/B/C 自由文本落库、失败输出进入完成缓存和任何生产写入。K1 仍为 `production_blocked`；上述指标用于调试和下一轮 gold/holdout 验收，不构成已达标声明。

### 16.6 交流过程重建、媒体缺失与无模型 prototype（2026-08-31）

本节是 K1 输入路线的最新约束，仅替换“先做主题分类”的入口语义；不改现有 Stage B/C schema、首页、发送边界或 `production_blocked`。其目的是先把可审阅的交流过程输入重建出来，再把有限的语义判断交给 DeepSeek。

#### 16.6.1 四层职责

| 层 | 契约 | 责任边界 |
| --- | --- | --- |
| `ContentLedger` | 每条消息一条不可变记录，带权威 scope、顺序、speaker、时间、reply/quote、正文引用或 digest | 本地登记、召回和证据根；不做 topic/claim/state 真值 |
| `ConversationEpisode` | 由消息顺序、reply/quote、可定位 fragment 和互动候选组成的交流过程单元 | 本地拼接与保留过程；`interaction_form_candidate`、`interaction_role_candidate`、内容 span 和价值标签都可为 `unknown`，不是最终语义结论 |
| `TopicThread` | 跨一个或多个 episode 的可选内容/话题流 | 只有输入验收后才由 DeepSeek 判断；不要求每个 episode 有 topic，不能以日历边界硬切 |
| `DeepRead` | 对已验收的 episode/packet/topic 候选做语义阅读 | DeepSeek 判断语义连续性、内容归属、claim、state、价值和不确定性；沿用 Stage B/C 的证据、缓存和失败回退 |

本地仅允许：候选召回、窗口/episode 拼接、媒体预处理（索引、可用性、OCR/ASR 产物登记，不解释内容）和证据整理。规则、关键词、embedding、时间邻近、same-segment 或本地分数都不能直接决定交流形态真值、TopicThread、claim、state 或信息价值。DeepSeek 只能接收已脱敏、已通过 `input_reconstruction_status=accepted` 的输入。

#### 16.6.2 交流作用、内容和信息价值必须分栏

`ConversationEpisode` 至少分开保存以下三个维度：

- `interaction_role_candidate`：消息在交流中的作用，如 `opener|social_smalltalk|acknowledgement|question|request|answer|reaction|continuation|elaboration|contrast|topic_shift|unknown`；这是过程信号，不是它所谈论的内容。
- `content_refs`：消息/fragment 中实际谈到的可定位 span、引用或已登记媒体产物；可以为空或 `unknown`，不强行改写成 topic label。
- `information_value`：对理解当前交流或后续行动的价值，固定为 `none|low|medium|high|unknown`，带来源和 evidence；它不能由闲聊标签、内容相似度或 event completeness 推导。

因此，闲聊可以有 `social_smalltalk` 作用而没有 `TopicThread`；有高价值的问答也可以暂时没有完整 topic；四种“低/高信息价值 × 有/无可形成语义流”的组合都必须保留。任何缺字段都写 `unknown`，不以默认 topic、默认 state 或沉默补齐。

prototype 中的 `interaction_form_candidate` 只表示待审阅过程信号；若需要最终 `interaction_form`，只能由人审或已验收输入上的 DeepSeek 产生，并携带 typed evidence，不改变 Stage B/C schema。

#### 16.6.3 可供人审阅的无模型 prototype 契约

公开仓库只允许使用 synthetic fixture；授权私有数据的 prototype 只在被忽略的 working/release 目录中保存，不读取或发布 frozen 正文。建议 artifact 为 `content_ledger.prototype.jsonl`、`conversation_episodes.prototype.jsonl` 和 `input_reconstruction_report.prototype.json`，最小字段如下：

```text
ContentLedgerV1
  content_ledger_id, message_id, scope, sequence_in_chat, event_time
  speaker_id, direction, reply_to_message_id, quote_refs
  content_ref/body_digest, fragment_refs, evidence_refs
  media: [{media_type, availability, preprocess_status,
           artifact_ref, missing_reason, semantic_evidence}]
  schema_version, input_fingerprint, provenance

ConversationEpisodeV1
  conversation_episode_id, ledger_message_ids, fragment_ids
  interaction_form_candidate: social_smalltalk|question_answer|
    request_response|coordination|debate|broadcast|monologue|mixed|unknown
  interaction_role_candidates: [{fragment_id, role, evidence_refs}]
  content_refs: [{fragment_id, span|artifact_ref, content_status, evidence_refs}]
  information_value: none|low|medium|high|unknown
  information_value_source: human_review|unknown
  boundary: {start, end, resolution, evidence_refs}
  view_refs: [{view_scale: today|yesterday|week, view_id}]
  status: review_required|accepted|rework|blocked
  uncertainties, provenance

InputReconstructionReportV1
  prototype_version, input_fingerprint, ledger_ids, episode_ids
  input_reconstruction_status: accepted|pending|rework|blocked
  checks, metric_values, reviewer_ids, reviewed_at
  model_call_allowed: false|true
```

`media.availability` 必须显式使用 `available|unavailable|not_present|unknown`；`unavailable` 必须有 `missing_reason`，且 `semantic_evidence=false`。媒体缺失占位、失败的 OCR/ASR 和文件路径本身不能作为语义证据；另有人工核验的脱敏 transcript 才能以独立文本证据登记。prototype 不输出最终 `topic_id`、TopicThread、claim、event/title 或非 `unknown` 的语义 state；人审可以标注“待补/不适用”及理由，但不能把缺失媒体猜成内容。

`today`、`yesterday`、`week` 是同一 `ConversationEpisode`/`TopicThread` 的查询视图字段，不是切分键：同一稳定 ID 可以出现在多个视图，跨日/跨周仍保留同一 thread；视图边界不得关闭、拆分或合并 thread。若窗口只覆盖一端，边界为 `unknown`。

#### 16.6.4 输入先验收，再允许模型

prototype review 必须先验证 ledger 覆盖、权威 metadata、scope、顺序/reply/quote、fragment span、媒体状态、三维字段分离、stable ID、evidence 回链和多尺度 view 一致性。只有 `input_reconstruction_status=accepted` 才能创建 Stage A provider request；`rework|blocked|pending` 不得调用 DeepSeek，且必须记录 reason code、fingerprint 和可重激活 cue。TopicThread 没有结果不构成 provider 失败。

契约不变量是 `model_call_allowed=true` 当且仅当 `input_reconstruction_status=accepted`；该布尔值由验收报告生成，不能由调用方或模型自行改写。

最低验收指标如下，数值指标按 synthetic 与授权脱敏 gold 分开报告：

| 指标 | 最低门 |
| --- | --- |
| ledger message/metadata/scope/reply/quote 保留率 | 100%；丢失、重复或静默改写为 0 |
| episode 可审阅覆盖与 evidence 回链 | 100%；每条消息可定位到 ledger，未决/未知有理由 |
| episode 边界/关系重建 | context macro-F1 ≥0.85；无显式 reply 的承接 precision ≥0.90；仅时间/same-segment 强连为 0 |
| 闲聊/互动作用、内容、信息价值分离 | 字段互不推导；四种组合保留率 100%；低价值不误过滤 |
| 主题可选性 | 强制 topic/TopicThread 率为 0；无主题/未知主题均可重放，topic coverage 仅诊断 |
| media missingness | 不可用媒体显式缺失率 100%；媒体占位/路径充当语义证据率 0 |
| today/yesterday/week 视图 | 同一 thread 稳定 ID 一致率 100%；仅因日历边界拆分/关闭率 0 |
| model gate | 未 `accepted` 输入的 provider request 数为 0；accepted 才能调用的合规率 100% |
| replay | 同输入、同 prototype/schema 版本得到相同 ID、scope、evidence 和状态率 100% |

任一硬门失败即 `rework` 或 `blocked`，不发起模型调用；所有指标与人审意见必须可回到 artifact。通过输入门只表示允许进入实验性 DeepSeek Stage A/B/C，不表示生产准入；`production_blocked`、首页和现有 Stage B/C 约束继续有效。

## 历史附录：第二版情报工作台基线（2026-08-21，已降级）

本节根据实际数据库、API、前端和只读运行状态的第二轮效能审计补充。它不是单纯的页面改版，而是后续数据模型、分析模型和交互模型的共同基线。

> 降级说明：本节只用于记录当时的采集、身份、页面和 API 设想。凡与第三版的事件身份、主张归因、前端不变量、离线实验和上线闸门冲突之处，以第三版为准。尤其不得继续执行本节中“围绕同一主题形成事件单元”或直接让 AI 生成主题/摘要的语义设计。

本节中的内容使用三种状态标记：

- 当前实现：已经在代码或运行状态中验证；
- 设计目标：作为后续实现契约，当前接口或页面可能尚未存在；
- 待验证：需要在真实微信版本、真实数据或现场交互中完成验收，不能提前宣称可用。

文档中的“支持”“提供”和“可用”仅在标明“当前实现”时代表现状；没有标记的 API、数据表和页面均属于设计目标。

### 1. 产品定位与不可越界边界

产品定位从“微信消息桥接控制台”调整为：

> 面向个人决策的信息收集、筛选、理解和人工处理工作台。

核心目标不是把所有消息堆在页面上，而是让用户在最短时间内回答四个问题：

1. 今天发生了什么？
2. 哪些事情可能影响我？
3. 哪些事项需要我判断或行动？
4. 我能否一键回到原始会话核对证据？

以下边界在第二版仍然保持不变：

- 当前默认只读，`send_enabled=false`；所有回复只能生成草稿或预览，不能触碰微信发送。
- 历史同步和实时监听必须在界面上分开标识，不能把历史已导入伪装成实时全量。
- “重点”“高价值”在用户确认前只能叫“重点候选”或“待确认事项”。
- AI 只能辅助解释和归纳，不能替代证据，也不能自动产生发送任务。
- 微信号、内部 ID、数据库路径不进入主展示层；它们只保留在诊断详情中。

### 2. 当前基线与指标定义

本次审计快照作为后续回归基线：

- SQLite 共 8,372 条消息，覆盖 42 个会话；消息 ID 当前无重复，`PRAGMA integrity_check` 通过。
- 近 7 天历史同步读取 8,095 条、42 个会话，新增 29 条，耗时约 79 秒。
- 2026-08-21 当日读取 1,859 条、12 个会话，其中入站 1,800 条。
- 当前实时监听范围仍是“文件传输助手”；全会话历史读取不能替代全账号实时监听。
- 非文本消息 2,438 条，其中 941 条没有可用媒体路径；图片、语音、表情等仍不能直接参与语义分析。

后续所有页面和 API 使用以下指标，不允许混用：

| 指标 | 定义 | 用途 |
| --- | --- | --- |
| `capture_completeness` | 已读取源端条数 / 源端应有条数；源端无应有条数时显示“未知” | 判断是否全量抓取 |
| `analysis_coverage` | 参与分析的入站文本候选 / 入站消息总数 | 判断分析覆盖面，不代表抓取完整 |
| `ingest_lag_seconds` | `ingested_at - event_time` | 判断消息进入本地库的延迟 |
| `identity_resolution_rate` | 具备可验证身份的入站消息 / 需要身份的入站消息；系统消息和本人消息单独统计 | 判断姓名展示质量 |
| `identity_conflict_rate` | 被人工或规则发现的错误归属消息 / 已解析身份消息 | 监控串台风险，目标为 0 |
| `media_path_coverage` | 有明确文件或缓存路径的媒体 / 媒体总数 | 判断是否能定位媒体 |
| `media_file_open_rate` | 实际存在且可打开的媒体 / 有路径媒体总数；无法验证时显示“未知” | 判断媒体是否真正可回看 |
| `review_conversion` | 用户确认的事项 / 重点候选事项 | 判断筛选是否真正产生价值 |

所有指标都必须带时间范围、数据范围、分母和数据来源。没有分母或来源的数字不进入首页主指标。

### 3. 第二版领域数据模型

当前 `messages` 表继续作为原始标准化消息表，但不能承担全部业务语义。第二版增加以下逻辑实体：

```text
account
  ├── chat
  │     └── chat_member
  ├── contact
  └── message
          ├── media_asset
          ├── insight_item
          └── review_action

sync_run
analysis_run
```

#### 3.1 `account`

保存账号边界和适配器来源。至少包含：

- `account_id`
- `account_display_name`
- `source_adapter`
- `source_adapter_version`
- `wechat_version`
- `last_health_at`

所有消息、会话、联系人和同步记录都必须带 `account_id`，为多账号扩展预留真实隔离键。

#### 3.2 `chat` 与 `contact`

`chat` 负责会话，不再用 `chat_name` 作为身份主键。至少包含：

- `chat_id`
- `chat_type`：`direct`、`group`、`filehelper`、`system`
- `display_name`
- `is_live_monitored`
- `last_event_at`
- `last_ingested_at`
- `unread_count`
- `capture_state`

`contact` 负责联系人身份，分别保存 `contact_remark`、`contact_nickname`、`wxid`，不把它们合并成一个不可追溯的名字字段。

#### 3.3 `chat_member`

群成员身份必须以 `(account_id, chat_id, source_member_id)` 为作用域。不能再使用全局数字发送者索引推断群成员。

建议保存：

- `group_nickname`
- `contact_remark`
- `contact_nickname`
- `display_name`
- `display_source`
- `display_confidence`
- `resolved_at`

展示规则必须区分“群内称呼”和“对象搜索名”，不能把多个名字字段互相覆盖：

- 群聊气泡和消息流主名称：当前群内昵称 > 通讯录备注 > 微信昵称 > “群成员·待确认”；
- 通讯录检索、对象详情和跨会话搜索：通讯录备注 > 群内昵称 > 微信昵称；
- 同时保留 `group_nickname`、`contact_remark` 和 `contact_nickname`，在详情中标明来源，不把它们压成一个不可追溯的字符串；
- 内部微信号只作为详情字段，不显示在信息流和聊天气泡中；
- 直接聊天必须以当前 `chat_id` 绑定的聊天对象为准，禁止使用全局数字发送者映射。

#### 3.4 `message` 的稳定身份

消息去重键不能只依赖 `(adapter_name, message_id)`。第二版应保存：

- `account_id`
- `source_adapter`
- `source_shard`
- `source_local_id`
- `source_server_id`
- `source_message_key`
- `event_time`
- `ingested_at`
- `content_hash`

跨适配器导入时，优先使用源端稳定 ID；只有源端 ID 缺失时才使用带时间、会话、方向和内容约束的保守哈希，避免 Hook 和本地数据库适配器重复产生同一条消息。

### 4. 情报处理流水线

第二版的处理链路固定为：

```text
源端读取
  ↓
历史 / 实时来源标记
  ↓
消息标准化与媒体索引
  ↓
联系人、群成员和会话消歧
  ↓
消息流与会话聚合
  ↓
事件窗口识别
  ↓
规则初筛
  ↓
AI 二次分析（人工触发）
  ↓
重点候选 / 待处理候选
  ↓
人工确认、忽略、延期或完成
  ↓
反馈修正规则和排序
```

#### 4.1 来源可信度

每条消息和每个会话都要标记：

- `source_mode`：`live`、`history`、`recovered`；
- `capture_state`：`fresh`、`stale`、`partial`、`unknown`；
- `sync_run_id`；
- `source_event_time` 和 `ingested_at`。

首页必须显示“实时监听范围”和“历史已同步范围”，并提供每个会话的最后抓取时间。同步任务不能只保存在进程内，增加 `sync_runs` 表保存范围、耗时、读取数、插入数、去重数、失败原因和重跑记录。

#### 4.2 会话和事件聚合

消息级关键词不足以判断“发生了什么”。第二版增加两个聚合层：

- 会话窗口：只在同一 `account_id + chat_id` 内聚合相邻消息，默认以“间隔超过 15 分钟”或“窗口达到 10 条消息”作为切分条件；阈值必须可配置，并保留实际切分原因；
- 事件单元：围绕同一主题、任务、风险或决定的一个或多个会话窗口。

每个事件单元至少包含：

- `event_title`
- `event_summary`
- `participants`
- `facts`
- `decisions`
- `open_questions`
- `actions`
- `importance_candidate`
- `confidence`
- `evidence_message_ids`
- `start_at`、`end_at`

没有原始证据的事件不进入重点列表。

#### 4.3 规则分析与 AI 分析的职责

规则负责稳定、可解释的初筛：

- 明确请求、责任承诺、期限、决策、风险、交易和问题；
- 过滤寒暄、单纯确认、孤立日期、孤立金额和纯媒体占位符；
- 判断必须结合上下文窗口，不因单个“改成”“明天”“多少钱”直接升级。

AI 只处理规则选出的候选上下文，不上传整个消息库。AI 返回结构固定为：

- `brief`
- `themes`
- `facts`
- `interpretations`
- `open_questions`
- `actions`
- `importance`
- `confidence`
- `evidence_refs`

其中 `importance` 和 `confidence` 必须是 0—1 的数值，`evidence_refs` 必须引用本次分析输入中存在的证据编号。模型输入不得包含微信号、内部消息 ID、媒体 MD5、Windows 路径、邮箱和手机号；这些字段只能在本地回链阶段恢复。

每个 AI 判断必须至少关联一条本地证据；无法绑定证据的内容丢弃。AI 仍保持手动触发、脱敏候选文本、结果本地留存、不创建发送任务的边界。AI 未配置时，规则分析仍正常运行，但页面必须明确显示“AI 未启用”，不能显示一个看似已经完成的 AI 结果。

#### 4.4 人工反馈闭环

重点候选和待处理候选持久化到 `insight_items`，状态至少包括：

```text
new → reviewing → confirmed
                  ↘ ignored
                  ↘ snoozed
                  ↘ completed
```

每次用户操作都写入 `review_actions`，保存操作人、时间、原状态、新状态和备注。用户标记“无价值”或“误判”后，后续排序和规则评估可以使用这些反馈，但不允许无审计地自动修改原始消息。

`snoozed` 必须同时保存 `snoozed_until`；状态变更接口必须具备幂等键，重复点击不能产生重复操作记录或重复事项。当前为单用户本地系统时，`actor_id` 可固定为本机用户，但字段仍需保留。

### 5. 工作台信息架构

主导航固定为：

```text
情报总览｜信息流｜会话｜通讯录｜待处理｜分析
```

系统状态、适配器、同步诊断和 AI 配置放入低频设置抽屉，不占据首页主层级。

#### 5.1 情报总览

首屏顺序固定为：

1. 状态条：实时范围、历史范围、最后同步、数据是否完整；
2. 今日简报：3—5 个事件卡片，回答“发生了什么”；
3. 待我判断：需要人工确认的重点候选；
4. 近期变化：最近更新的会话和对象；
5. 最新消息：倒序显示的少量消息，作为入口而不是主体；
6. 数据质量：分析覆盖、身份未解析、媒体不可回看等风险提示。

首页不再默认展示小时柱状图、主题标签等低优先级统计。它们进入“分析”页，除非它们直接解释当前变化。

#### 5.2 信息流中心

信息流是全局证据浏览器：

- 默认最新消息在上；
- 支持时间、会话、联系人、群聊、消息类型、重点候选、身份置信度筛选；
- 支持暂停实时更新；有新消息时显示“新增 N 条”，用户点击后再插入；
- 每条消息显示来源、会话、对象、时间、类型、候选标签和证据状态；
- 点击重点或 AI 证据后，打开右侧详情并定位到前后文，不只滚动到一行。

#### 5.3 会话中心

采用微信式三栏布局：

```text
会话列表 | 当前会话消息 | 情报与处理详情
```

会话列表显示：主名称、最后消息摘要、最后时间、未读数、重点数量、实时/历史状态。中间消息区保持旧消息在上、最新消息在下，并支持按会话加载更多历史。右侧显示该会话的事件摘要、对象信息、待处理事项和证据列表。

当前版本只提供“回复草稿”和批量确认，不提供发送按钮。

#### 5.4 通讯录中心

通讯录不是简单的联系人列表，而是对象索引：

- 主名称、备注和名称来源；
- 最近活跃时间和消息量；
- 关联会话和群聊；
- 近期主题；
- 未处理事项；
- 身份置信度和待确认提示。

群聊成员默认按群昵称定位，通讯录备注和微信昵称放入详情，不显示微信号作为主字段。

#### 5.5 待处理与分析

待处理中心只展示有明确证据的候选，并支持确认、忽略、延期、完成和批量操作。分析中心负责回答：

- 今天 / 本周发生了哪些事件；
- 哪些是事实，哪些只是推断；
- 哪些事项有明确责任人和期限；
- 哪些会话变化最大；
- 哪些内容被系统排除，排除原因是什么；
- AI 是否参与，使用了多少候选，结论绑定了哪些证据。

### 6. 第二版 API 契约方向

以下是第二版的目标接口，不代表当前已经全部实现；保留现有接口兼容层，逐步新增面向工作台的接口：

```text
GET  /api/overview?start=&end=
GET  /api/feed?cursor=&limit=&chat=&contact=&filter=
GET  /api/chats?sort=recent&unread_only=
GET  /api/chats/{chat_id}/messages?before=&limit=
GET  /api/contacts?query=&sort=
GET  /api/contacts/{contact_id}
GET  /api/insights?start=&end=&status=
GET  /api/reviews?status=&cursor=
POST /api/reviews/{insight_id}/transition
GET  /api/sync-runs?start=&end=
GET  /api/media/{asset_id}
POST /api/ai-analysis/preview
```

所有列表接口必须支持游标或服务端分页，默认不返回 50,000 条消息。消息接口要同时返回：

- `message_id`；
- `chat_id` 与 `chat_display_name`；
- `sender_display_name`；
- `display_source` 与 `display_confidence`；
- `message_type` 与 `media_state`；
- `event_time` 与 `ingested_at`；
- `candidate_state`；
- `evidence_refs`。

`/api/media/{asset_id}` 只能读取已登记且位于允许目录内的本地资源；必须校验真实路径、文件类型和文件存在性，禁止通过该接口访问任意 Windows 路径。媒体缺失时返回明确的 `unavailable` 状态，而不是 200 状态的空内容。

### 7. 分阶段实施和验收门槛

#### M0：指标与边界基线

- 增加数据质量摘要和同步运行记录；
- 明确“实时监听范围”和“历史同步范围”；
- 将 `analysis_coverage` 改名并与 `capture_completeness` 分离；
- 继续保持发送锁定。

验收：无法证明全量时，界面必须显示“完整度未知”，不能显示“全量完成”。

#### M1：全量历史与实时状态

- 历史同步按会话记录水位、范围、耗时和缺口；
- 优先验证将实时监听从文件传输助手扩展到全部可读会话；如果适配器做不到，必须保留“文件传输助手实时、其他会话历史”的明确状态，不得对外宣称全账号实时；
- 为每个会话提供最近事件时间和最近入库时间。

验收：同一天重复同步三次，源端可核对数量一致、数据库不产生重复；每个会话的状态可解释；实时监听范围和延迟可被单独验证。

#### M2：身份与媒体基础层

- 落地 `account`、`chat`、`contact`、`chat_member`、`media_asset`；
- 消除全局数字发送者映射；
- 保存身份来源和置信度；
- 媒体路径增加存在性、类型和可打开状态。

验收：直接聊天不出现跨联系人归属；群聊无法确认时显示待确认而不是猜测；主界面不出现微信号；媒体必须分别报告“有路径”和“可打开”，不能用目录存在代替文件可用。

#### M3：工作台 API 与前端骨架

- 先实现总览、倒序信息流、会话三栏、通讯录和待处理页面；
- 全局信息流倒序，聊天页正序；
- 增加游标、暂停刷新和新增消息提示；
- 让首页首屏先看到事件和待处理，不再被统计图占据。

验收：100 条测试消息严格按时间倒序；刷新不改变搜索、筛选、滚动位置和当前会话；首屏无需滚动即可看到重点候选；首屏请求不超过 200 条消息，新增消息使用游标或增量标记，不重复传输整个时间窗口。

#### M4：事件分析与人工处理

- 增加会话窗口和事件单元；
- 将重点改为候选状态；
- 增加确认、忽略、延期、完成和审计记录；
- 将用户反馈纳入规则评估。

验收：刷新和重启后状态不丢失；同一事件不会重复生成多个待处理项；每项都能回到原始证据；“忽略”和“误判”反馈能在后续分析中被统计，而不是静默丢失。

#### M5：AI 二次分析

- AI 只处理候选上下文；
- 默认人工触发；
- 输出事实、推断、开放问题、行动、置信度和证据；
- 无证据结论不展示；
- 不创建发送任务。

验收：AI 结论证据绑定率 100%；没有 API 配置时，界面明确显示“AI 未启用”，本地规则仍可用。

#### M6：媒体、多账号和插件

- 图片缩略图、文件路径、媒体存在性和可打开操作；
- 语音转写、图片 OCR 作为独立能力，不混入基础消息质量指标；
- 增加 `account_id` 隔离和多账号切换；
- 最后再稳定 Codex 插件接口。

### 8. 第二版明确暂不做

- 不开放自动发送和批量直接发送；
- 不把 Hook 兼容性当作当前主链路；
- 不在没有身份和同步质量门槛时扩展复杂 AI 场景；
- 不把图表数量当作情报能力；
- 不在没有人工反馈闭环时宣称算法已经“智能”。
