# 微信日报 ACU 标注与仲裁规范 v2

## 1. 目的与适用范围

本规范用于把微信原始消息整理为可复核、可测量的 Atomic Content Unit（ACU），并仲裁不同轮次的粒度、证据绑定、槽位和纳入标签。ACU 金标准服务于研究实验与评估，不直接改变生产日报逻辑。

本版针对 2026-09-16 的两轮标注收口：第一轮 148 条 ACU，第二轮 1124 条 evidence binding，来自 411 个语义重建的 content-line candidate。仲裁结果保留所有输入来源，不按固定数量、Top-K、摘要篇幅或候选分数截断。

## 2. ACU 的边界

ACU 是一个能够由原始消息独立判真、判假或判定状态的命题，不是消息、候选标题、主题、证据绑定或最终日报句子。

一个 ACU 通常至少包含：

- actor：谁执行动作、作出决定或发表主张；
- action：动作、陈述、决定或状态变化；
- object_or_recipient：动作对象、接收者或被讨论对象；
- status：事实/体验/计划/未闭环等状态；
- modality/temporal boundary：建议、疑问、推测、已发生、未来计划及时间阶段（如适用）。

### 2.1 合并规则

只有多条消息支持同一个 actor-action-object/status/modality/temporal proposition 时才合并。合并只增加 `evidence_message_ids`，不得因为证据变多而把 `reported` 升级为客观 `confirmed`，也不得把局部体验升级为全局故障。

纯问题、回答、确认或补充说明若都围绕同一尚未变化的命题，可与主命题合并；若补充消息引入新的决定、结果、主体、状态变化或时间阶段，则拆开。

### 2.2 拆分规则

出现以下任一情形，拆成独立 ACU：

- actor、action、object_or_recipient 不同；
- 一个事实与另一个事实可以分别为真/假；
- 同一对象发生了独立的状态变化（例如提交申请与尚未获批）；
- 时间阶段、模态或结论不同（例如“过去可用”与“现在报错”）；
- 一条消息同时包含资源/建议、行动安排、结果或观点。

同一 evidence message 可以重复出现在多个拆分后的 ACU 中；这是共享证据，不是重复 ACU。相反，同一个 ACU 内不得重复绑定同一 evidence ID。

### 2.3 不靠数量收口

不得用固定 ACU 数、Top-K、候选排名或“每天只留若干条”的规则决定合并或删除。低价值但仍可独立判真的命题保留为 `exclude` 审计行；无法由直接消息支持的推断、不可读媒体中的内容和无法解析的指代，保留边界说明并降低为 `unknown`/`reported`，不能补造事实。

## 3. 字段与槽位

每条输出至少包含以下字段：

```json
{
  "acu_id": "acu-YYYYMMDD-adj-NNNN",
  "date": "YYYY-MM-DD",
  "atomic_content": "一个原子命题",
  "evidence_message_ids": ["当天原始 message_id"],
  "context_message_ids": [],
  "actor": "人物、组织、联合主体或 unknown",
  "action": "动作、陈述、决定、状态变化或 unknown",
  "object_or_recipient": "对象、接收者或 unknown",
  "status": "confirmed|reported|pending|resolved|unknown",
  "claim_type": "observed_action|reported_experience|opinion|question|hypothesis|resource",
  "user_relevance": "direct|decision_support|situational_awareness|background|none",
  "inclusion_label": "must|may|exclude",
  "exclusion_reason": null,
  "uncertainty": [],
  "annotator_note": "",
  "source_round1_acu_ids": [],
  "source_round2_acu_ids": [],
  "adjudication_decision": "carry|merge|split|merge_and_split",
  "label_adjudication_reason": "",
  "user_relevance_adjudication_reason": ""
}
```

`source_round1_acu_ids`、`source_round2_acu_ids` 和 `adjudication_decision` 是仲裁审计字段。它们不参与命题含义本身；一个来源 ID 出现在多个最终行表示拆分或跨轮次粗粒度映射，多个来源 ID 出现在一个最终行表示同一命题的证据合并。

### 3.1 actor 与归因

按以下顺序判断主体和方向：明确回复/引用关系 > 连续对话中的人称与动作 > sender 元数据 > 单句语气。元数据与语义冲突时使用 `unknown` 并在 `uncertainty` 记录冲突。不能把聊天对象、被 @ 的人或“我”自动当成动作主体；“我”在本项目输出中规范化为“用户”。

宁江月相关的团组织关系转接必须使用中性关系表示：

- `actor = 用户与宁江月`；
- `object_or_recipient = 用户与宁江月之间的团组织关系转接工作交接`；
- 在 `uncertainty` 说明单方消息无法可靠确定交出方/接收方。

第三方“把申请书交给江月”等句子保留实际说话主体和第三方动作，不因提到宁江月而并入中性交接 ACU。

### 3.2 status 与事实强度

- `confirmed`：原消息直接确认动作、决定或状态；
- `reported`：只能确认某人报告了体验、观点或局部观察；
- `pending`：询问、计划、等待回复或尚未闭环；
- `resolved`：原消息明确说明已完成、恢复或无需继续处理；
- `unknown`：证据不足以确定状态。

沉默、话题结束、没有后续或摘要标题结束，都不能推断为 `resolved`。问题和计划不因候选卡片完成而自动变成 `confirmed`。多人报告不等于外部系统已经确认故障。

### 3.3 user_relevance 与纳入标签

`direct` 表示直接改变用户要做的事或当前状态；`decision_support` 表示影响选择或判断；`situational_awareness` 表示值得知道的外部变化或群体信号；`background` 表示有内容但价值较弱；`none` 表示不构成用户日报价值。私聊、提到用户或用户本人发言都不是 `direct` 的充分条件。

`must` 用于遗漏会实质降低当天重要变化、决定、风险或可用知识理解的 ACU；`may` 用于有一定价值但取舍依赖上下文的 ACU；`exclude` 不进入主报告，但只要仍是可独立判真的命题就保留为审计行，并填写 `exclusion_reason`。

同一主线中的材料收集、询问、转发、致谢和一般支撑步骤不会因为主命题是 `must` 就自动成为 `must`；它们必须单独判断，通常为 `may` 或 `exclude`。单条辱骂、致谢、确认收到或礼貌回复不构成用户日报命题，除非消息同时包含独立的可执行事实。

排除原因包括：`trivial`、`duplicate`、`no_change`、`unresolved_reference`、`unsupported`、`private_low_utility`、`off_topic_for_user`、`pure_question`、`social_chatter`、`unavailable_media`。

## 4. 两轮仲裁流程

1. 冻结两轮 JSONL、语义重建和当天原始 DB；先验证所有 evidence ID 存在，不用摘要标题替代原始消息。
2. 以第二轮每条 evidence binding 为覆盖入口，读取直接消息与必要上下文，构造 proposition-level ACU。
3. 按第 2 节执行 merge/split；所有来源 ID 必须进入某个最终行，不能用静默删除解决分歧。
4. 按直接消息重新确定 actor、action、object_or_recipient、status、claim_type、user_relevance 和 uncertainty；冲突保留不确定性，不做多数投票升级。
5. 用直接 evidence overlap 连接第一轮来源。该连接允许多对多，不能假装是一对一匹配。
6. 写入 `adjudication_decision`：未改变粒度为 `carry`，多个来源合并为 `merge`，一个来源拆为多个命题为 `split`，两者同时发生为 `merge_and_split`。
7. 独立重判 `inclusion_label` 与 `user_relevance`：不读取两轮旧标签作为答案；为每行记录判定理由，must 只在物质影响/风险/阻断/确认里程碑成立时使用。
8. 生成完整 ledger，记录 merge、split、unmatched，并做机器校验与宁江月中性关系人工抽查。

本次仲裁的可复算入口是 `tmp/generate_acu_adjudicated.py`。该脚本只生成研究数据和说明文件，不修改生产代码。

本次全量价值重标的旧/新分布、变更行、每行理由和校验结果记录在 `data/private/daily_brief_research/2026-09-16/label_adjudication_summary.private.json`；`adjudication_notes.private.md` 同步保留可读版变更 ledger。

## 5. 一致性检查

机器检查必须包括：

- 最终 `acu_id` 唯一，日期和字段枚举合法；
- 每条 `must`/`may` 至少有直接 evidence；
- 每个 `exclude` 有 `exclusion_reason`；
- 所有 evidence ID 存在于当天 DB；
- 同一最终 ACU 内 evidence ID 不重复；同一 evidence 可支持多个不同 ACU；
- `pending`/`unknown` 未被改写成完成；
- actor/object 冲突项存在 `uncertainty`；
- 宁江月中性交接的 actor/object 组合正确，第三方转述没有误并；
- round1/round2 每个来源 ID 都有映射，或在 ledger 中明确列为 unmatched。

人工复核重点是原子性、主体方向、状态变化、群体体验与客观事实的边界、跨消息合并是否确实共享同一 proposition，以及不可读媒体是否被错误补全。

## 6. 覆盖限制与研究解释

本日原始 DB 有 3925 条消息；两轮入口覆盖 411 个 content-line candidate 和去重后的 1442 个直接 evidence ID。因此本金标准覆盖的是已进入两轮标注的研究入口，不声称对全部 3925 条消息逐条人工标注。最终行数由 proposition-level merge/split 决定，不是质量目标，也不能外推到其他日期。

ACU gold 只记录原始消息能够支持的命题。它不把用户观点写成产品客观优劣，不把局部可用性体验写成全局故障，不把“未见后续”写成完成，也不把不可见的图片、链接或卡片内容写进命题。
