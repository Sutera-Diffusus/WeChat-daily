# 微信日报算法 · 勘察证据附录

> 日期：2026-09-22
> 配套主报告：[daily-brief-reconnaissance-2026-09-22.md](daily-brief-reconnaissance-2026-09-22.md)
> 性质：只读勘察的证据明细，供复查与动手前定位。行号对应 2026-09-22 快照。
> 隐私：不含聊天原文、真实姓名、wxid、手机号；人名以抽象指代。

---

## A. 代码地图与规模

实测（`src/**/*.py`、`tests/*.py`、`tools/*.py` 逐文件行数求和）：

| 分组 | 文件 | 行数 | 占比 |
|---|---:|---:|---:|
| `src/wechat_bridge` 合计 | 80 | 116,709 | 100% |
| ├ **研究脚手架**（与日报零耦合） | 55 | **95,249** | **81.6%** |
| ├ **日报生产链**（`analysis`+`web`+`ai`+`editorial_reducer`） | 4 | **12,038** | **10.3%** |
| └ 其余（store / cli / engine / settings / models / media / voice / image_key / diagnostics / timeutil / `__init__` / `__main__` / adapters/* 等 20 个） | 21 | 9,422 | 8.1% |
| `tests/` 合计 | 133 | 59,963 | 100% |
| ├ 一次性 audit / 实验脚本 | 42 | **27,342** | **45.6%** |
| └ 常规回归测试 | 91 | 32,621 | 54.4% |
| `tools/` | 4 | 1,994 | — |

> 注：`web.py` 5,931 行中含大量非日报功能（媒体服务、同步、设置、静态资源）。12,038 是日报链的上界。

### A.1 研究脚手架 55 文件清单

```
active_learning_round1.py        active_period_runner.py          annotation_tools.py
bundle_semantics.py              compact_context_packets.py        compact_stage_a_development_pilot_v3.py
compact_stage_a_protocol.py      compact_stage_a_protocol_health.py
compact_stage_a_protocol_health_v3.py                              compact_stage_a_protocol_health_v3_runner.py
compact_stage_a_protocol_v3.py   context_packet_compact_development_runner.py
context_packet_development_runner.py                               context_packet_stage_adapter.py
context_packets.py               context_reconstruction.py         context_reconstruction_blind_runner.py
contextual_bundle_pipeline.py    contextual_bundle_pipeline_runner.py
contextual_bundle_pipeline_v2_9_runner.py                          contextual_bundle_pipeline_v2_10_runner.py
contextual_bundle_pipeline_v2_11_runner.py                         contextual_bundle_provider_capabilities.py
contextual_bundle_scheduler_v2_9.py                                contextual_fragments.py
cross_date_experiment.py         dialogue_bundle.py                dialogue_segments.py
discourse_event_candidates.py    linear_stage_a_development_pilot.py
linear_stage_a_pilot.py          linear_stage_a_protocol_diagnostic.py
linear_stage_a_protocol_health_v2.py                               linear_stage_packet_development_runner.py
linear_stage_packet_development_stratified_runner.py               linear_stage_packets.py
persistent_call_budget.py        selection_strata.py               semantic_content_line_runner.py
semantic_content_lines.py        semantic_frame.py                 semantic_gate.py
semantic_gold.py                 semantic_pipeline.py              semantic_registry.py
semantic_wire.py                 semantic_wire_v2_11_tsv.py        semantic_wire_v3_compact.py
shadow_run_artifact.py           shadow_run_v28.py                 shadow_semantic.py
stage1_context.py                stage1_runner.py                  staged_context_packet_adapter.py
user_profile_calibration.py
```

体积最大的几个：`compact_stage_a_development_pilot_v3.py` 10,913、`semantic_pipeline.py` 7,632、`cross_date_experiment.py` 6,297、`context_reconstruction.py` 3,863、`compact_context_packets.py` 3,655。

### A.2 版本蔓延形态

同一功能存在 3–5 代并存：`compact_*` / `linear_stage_*` / `contextual_bundle_*` / `shadow_*` / `semantic_wire_*`（`_v2_9` / `_v2_10` / `_v2_11` / `_v3` / `_k16`…`_k31` 后缀）。
`tests/` 另有 42 个一次性脚本（`audit_*_private.py` 20 个、`build_p0*_split.py` 3 个、`run_p0*_final_eval.py` 2 个、`reconcile_*` 1 个等）。

---

## B. 日报生产链 file:line 清单

| 环节 | 位置 |
|---|---|
| 消息读取 | `store.py` |
| 切分：交流段 | `analysis.py:_split_conversation_blocks`；`_should_split` 判据 15 min gap + 60 s meaningful_pause |
| 切分：交流过程 | `analysis.py:_discussion_episodes`、`_episode`（`:5190-5191`） |
| 事件候选 | `analysis.py:_event_briefs`（lane 分配 `:3081-3087`，上限 80 `:3164-3165`） |
| 消息打分 | `analysis.py:_score_message`（`:1076-1153`） |
| 候选化/截断 | `analysis.py:build_ai_context`（`:3972-4135`） |
| 标题模板 | `analysis.py:198-316`（`is_editorial_title:232`、`normalize_editorial_title:261`、fallback `:315`）；`analysis.py:4991-5011` 基线导语 |
| 生成器 | `ai.py:384` `OpenAIAnalysisGenerator.analyze`；schema `:214-334`；system 规则手册 `:493-565`；user `:472-487` |
| DeepSeek 补丁 | `ai.py:354-362`、`:610-661`、`:19-45`、`:700-705` |
| 编排主流程 | `web.py:_ai_analysis`（`:4280-5682`） |
| 证据校验 | `web.py:4570-4830` |
| 多次抽取合并 | `web.py:_merge_draw_findings`（`:3901-3917`）；draws `:4349` |
| 恢复通道 | `web.py:4933-5240`（证据仅限候选集） |
| 占位句 / 边界 | `web.py:4221-4279` `_bound_ai_finding` |
| 罐头标题库 | `web.py:163-309` |
| 筛选去重 | `web.py:2834-3604` |
| 线索化 | `web.py:_finding_as_leads`（`:2914`） |
| 晋级门槛 | `web.py:5387-5410` |
| importance 重打分 | `web.py:5364-5385` |
| 最终主编 | `editorial_reducer.py:reduce_verified_findings`（`:104`）；`REDUCER_MAX_TOKENS=12288`（`:19`）；**仅 packet_mode**（`web.py:4398`） |
| payload / masthead | `web.py:5555-5682` |
| 前端渲染 | `web/app.js`：可见闸 `:887, 899-901`；模板标题 `:1059-1074`；版面阈值 `:1140, 1195-1227`；masthead `:1213-1215`；线索速览 `:1432-1441`；定时触发 `:1484-1490`；packet_mode 默认 `:1449` |
| `/api/insights` | `web.py:1523-1547` |
| 路由表 | `web.py:1426-1698` |

**零耦合旁链**：

| 模块 | 身份 | 证据 |
|---|---|---|
| `daily_brief_selector.py` | CLI 研究选择器（`main:744-830`） | 全仓库无 importer；手工 codebook 强制 `formal_briefing_allowed=False` |
| `staged_deepseek_analyzer.py` | K3 分段分析器 | 仅 `context_packet_stage_adapter.py:31-40`、`linear_stage_a_pilot.py:31`、`semantic_content_line_runner.py:17` 引用 |
| `selection_strata.py` | K28 body-free 页面选择 side-car | 5 canonical strata、稀有分层覆盖采样 |
| `semantic_gate.py` | 4 通道可逆路由器 | `immediate`/`pending_context`/`background`/`cold_recoverable`；classify `:243-281` |
| `semantic_registry.py` | Workstream-A 注册边界 | `PUBLIC_MESSAGE_FIELDS` 白名单、frozen payload、`stable_hash` |
| `/api/shadow-analysis` | review-only 影子面 | `web.py` 注释 "The homepage never calls this endpoint" |
| `models.py` | — | 仅 `IncomingMessage/SendResult/HealthStatus/SyncRun/ReplyDecision/ReplyTask`，**无 Candidate/Finding** |
| `settings.py:68,133` | — | `shadow_analysis_enabled=False` 强制归一化 ⇒ `semantic_*` 面 fail-closed |

---

## C. 阈值 / 权重 / 门槛总表

### C.1 选择与车道

| 项 | 值 | 位置 |
|---|---|---|
| **for_me 判定** | `has_private OR mentions_me OR action_for_me OR has_self OR matched_profile` | `analysis.py:3026-3033` |
| `matched_profile` | 纯子串 `term in combined_content` | `analysis.py:3032` |
| lane 分配 | `for_me` / `trending` / `pending` | `analysis.py:3081-3087`（前两分支等价，死代码） |
| `event_briefs` 上限 | 80 | `analysis.py:3164-3165` |
| `primary_insights` | `= event_briefs[:8]`（覆盖此前按 topic 的构建） | `analysis.py:4977` |
| **领域门槛特权** | `minimum_priority = 45 if 命中 priority_domains else 65` | `web.py:5096` |
| `priority_domains` | `{"wechat","gpt_service","codex_service","developer_account"}` | `web.py:5083` |

### C.2 打分

| 项 | 公式 / 值 | 位置 |
|---|---|---|
| topic importance | `avg_score + min(10, msg//20) + min(8, chat*2) + 6*resource` | `analysis.py:4917-4920` |
| topic confidence | `45 + min(30, chat*10) + 10*resource` | 同上 |
| episode importance | `max_score + min(40, len(items)*8)` | `analysis.py:5190-5191` |
| `rule_level` 映射 | `high → 80`，否则 `→ 45` | `web.py:5152` |
| 默认 importance | `62 if refs≥2 else 55` | `web.py:4915-4916` |
| 默认 confidence | `68 if refs≥2 else 56` | `web.py:4917-4918` |
| **importance 重打分** | `flat` 或 `<50` 或 `∈{55,62}` 或（有私聊证据且 `<58`）→ 完全覆盖为 `editorial_score`；否则 `score*.65 + editorial*.35` | `web.py:5379-5383` |
| **私聊地板** | `personal_evidence → importance ≥ 55` | `web.py:5384-5385` |
| 全私聊加成 | `editorial_score += 8` | `web.py:5376-5378` |
| **card 车道加分** | `min(18, refs*5)` + `12 if private` + `8 if concrete` | `web.py:3577-3580` |
| **lead 车道加分** | `30 if private` + `10 if concrete` + `min(8, quotes*2)` | `web.py:3623-3626` |
| 恢复卡 confidence | `min(65, max(confidence,55))` | `web.py:3799` |
| 恢复卡 importance | `max(35, min(90, local_importance or 55))` | `web.py:5029` |
| fallback 线索分 | `max(30, min(90, score or 45))` | `web.py:5217` |

### C.3 截断、门槛与上限

| 项 | 值 | 位置 |
|---|---|---|
| 候选样本 | Top-120（API 上限 200） | `analysis.py:3972-4135` |
| 候选正文截断 | 360 字 | 同上 |
| 邻近上下文 | 最多 8 条 × 240 字 | 同上 |
| 晋级成卡 | `≥2 证据 或 _self_contained_single` | `web.py:5387-5410` |
| 卡片 / 线索上限 | 10 / 15 | `web.py:5418 / 5421-5425` |
| 前端可见闸 | 55 / 65 | `app.js:887, 899-901` |
| 主编标题 | 4–24 字；`brief_claim_ids` ≤3 | `editorial_reducer.py:23-48` |
| 主编字段截断 | title 48 / summary 360 / 其余 240 / evidence 8 | `editorial_reducer.py:119-127` |
| `REDUCER_MAX_TOKENS` | 12,288 | `editorial_reducer.py:19` |
| 事实核验 | max_claims 8/6、搜索 5、match 0.12 / direct 0.25、2 域名 | `fact_check.py:377-393` |
| draws | 默认 2，上限 3 | `web.py:4349` |
| packet_draws | 默认 1 | `web.py:4359-4386` |
| packet_workers | 默认 4，上限 8 | `web.py:4358` |
| packet_max_span | 默认 45 min，上限 24 h | `web.py:4354-4356` |
| 合并阈值 | 证据重叠 ≥ 较小集合的 50% | `web.py:3901-3917` |
| refs⊆ 即删 | — | `web.py:3159-3177` |

### C.4 画像与 prompt

| 项 | 值 | 位置 |
|---|---|---|
| `profile.self_name` | 截断 60 字 | `settings.py:225` |
| 画像列表（roles/projects/organizations/key_contacts/topics/suggestions） | 每项 120 字 × 100 条 | `settings.py:226-230` |
| 画像消费点 1 | system prompt 文本 | `ai.py:464-467`、`:525-527` |
| **画像消费点 2** | **`for_me` 车道硬判定** | `analysis.py:3032-3033` |
| system prompt 私聊校准 | 「私聊…importance 不得低于 **60**」 | `ai.py:559` |
| system prompt 含真实人名示例 | — | `ai.py:539` |
| system 前缀稳定性 | 固定编辑规则整体入 system 做逐字节稳定前缀（提示词缓存） | `ai.py:493-565` |
| `max_findings` | 动态 | `ai.py:563` |

### C.5 已知的数值自指环与不一致

| 问题 | 证据 |
|---|---|
| **魔法数自指环** | `web.py:4916` 生成默认值 55/62 → `web.py:5380` 把 `∈{55,62}` 判为不可信并完全覆盖 ⇒ `:4915-4916` 的赋值实际永远无效 |
| **私聊下限三处不一致** | prompt 60（`ai.py:559`）/ 代码地板 55（`web.py:5385`）/ `∈{55,62}` 覆盖（`web.py:5380`） |
| **死分支** | `analysis.py:3081-3087` 前两分支结果同为 `for_me` |
| **计数器无行为** | `accepted_headline_count` 在 `web.py:5472-5503` 仅计数，无分支依据它改变行为 |

---

## D. 错误映射：E1–E10 → 代码位置

来源：`docs/daily-brief-research-roadmap-v2.md` §9 错误分类表 + `data/private/daily_brief_research/error_ledger.private.jsonl`（EL-001…EL-015，15 条，覆盖 09-07 / 09-08 / 09-16）。

| 错误 | 定义 | 代码机制 | 台账 |
|---|---|---|---|
| **E1** 交流边界错误 | 两话题切一片段 / 一片段被拆散 | `dialogue_segments._should_split`（60 s / 15 min 阈值）+ `_build_conversation_flows` 的 `shared_tokens`（bag-of-tokens）宽词重合误合并 | EL-001/002/003（**三天各一次**） |
| **E2** 说话者/主体错误 | 把用户的话归给联系人 | speaker 直取源 `participant_ref`/`sender_name`，**无交叉校验**；`_relation.shared_subject` / `shared_people` 错挂主体 | EL-004 |
| **E3** 关系方向错误 | 双向交接写成单向 | `_relation` 的 `ANSWERS`/`CONTINUES`/`ELABORATES` **只按重合定类型、不编码交接方向** | EL-005 |
| **E4** 对象污染 | 家教广告混入 AI 模型主题 | `_relation.person_object_history`（`shared_people`+`shared_action` 跨 object 合并）+ `_parallel_topic_families` **仅 3 个硬编码族** | EL-006 |
| **E5** 重要内容漏选 | 高价值主线已抽取但未进报告 | 候选化截断（`build_ai_context` Top-120）+ `web.py:3159` refs⊆ 即删 | EL-007 |
| **E6** 琐碎内容误选 | 人员名单 / 普通提醒进正文 | 私聊无条件 `for_me` + lead `+30` 特权 | EL-008 |
| **E7** 无证据结论 | 群友体验写成平台事实 | 证据门拒绝率高（09-08：77/161；09-16：81/182，其中 79 为 `packet_no_supported_claims`），但**最终仍走 fallback 出报** | EL-009/010 |
| **E8** 重复与冗余 | 同一事项拆成多条 | 去重只看 refs 重叠，**不看标题 / story 身份** | EL-011/012/013（同一标题重复 4/4/3 次） |
| **E9** 隐私边界错误 | 个人信息进可转发正文 | E6 的下游；`_redact_ai_content` 只脱敏路径/wxid/MD5/邮箱/手机号，**姓名不脱敏** | EL-014 |
| **E10** 表达质量 | 结构/语言/顺序不可读 | `_merge_draw_findings` 分号拼接 claims 成 `narrative`；三处罐头标题库 | EL-015 |

### D.1 `error_ledger` 计数

`freeze_manifest.error_ledger.error_counts_by_type`：
`E1:3, E2:1, E3:1, E4:1, E5:1, E6:1, E7:2, E8:3, E9:1, E10:1`（共 15）
计数单位是**已核验的代表性错误实例**，不是所有底层消息或所有被门禁拒绝的候选数量。

---

## E. 上游重建：每级压缩判据与信息损失

> 本节描述的是**研究侧**重建链（零生产耦合）。生产侧切分判据见 §B。

| 级 | 判据 | 信息损失点 |
|---|---|---|
| 交流段 | 15 min 时间 gap + 60 s `meaningful_pause` + topic-family 不相交 + 显式话题转移正则 + 显式回复覆盖 | 阈值切分对跨午夜 / 长间隔但同题讨论不鲁棒 |
| 交流过程 | `_episode_score_components` 打分 | 打分维度混合边界与价值，边界错会被价值分掩盖 |
| 交流流 | union-find，判据 `shared_tokens`（bag-of-tokens）或 `explicit_reply` 且 gap ≤ 7 天 | **宽泛词重合是 E1/E4 的直接来源** |
| 交流卡 | 由 `threads` 建（**非由 flows**） | flows 与 cards 是并行分支，`402 > 76` 是扩张而非压缩 |
| 语义内容线 | `semantic_evidence_eligible` 门 + 槽位抽取 | 见下 |

**媒体 / 语音占位**：无 OCR/ASR ⇒ `semantic_evidence_eligible=False` ⇒ `topic_layer ∈ {unclassified, context_only, media_placeholder}` ⇒ 语义丢失。
**@ 关系**：靠 `participant_names` 解析，易错。
**speaker**：直取源 `participant_ref` / `sender_name`，**无交叉校验**。

### E.1 Contextual Bundle 线的失败根因（应停止投入）

| 版本 | 结果 |
|---|---|
| v2.8 | 851 次模型调用，成功 **8**，失败 843 |
| v2.9 Flash / Pro | complete bundle 4 / 3；`schema-valid` 1.0 但样本极小 |
| v2.10 | 11 attempts，成功 **0**；9 × `compact_invalid_json`，2 × `output_token_limit_exceeded`，3 × `capacity_deferred`；有效语义编码 **0** |
| v2.11 | 11 attempts 全失败；`provider_incompatible=true` / `production_blocked=true` / `provider_trials_stopped=true` |

根因三层：
1. **容量层**：固定 `max 8 items / input 2,000 / output 400` 与包切分不匹配（大包 215 messages，input 5,405 > 2,000，output 下界 6,654 > 400）；
2. **协议层**：Flash compact JSON invalid/limit；TSV wire shape/enum 不合 schema；
3. **验收层**：无完整有效输出时，structural coverage 不能补齐语义质量（269/280 structural ≠ semantic）。

**缺失的是 provider 的 strict JSON schema / tool-call 能力，不是协议参数。**

---

## F. 评测与金标现状

### F.1 三份金标并存，口径不一

| 版本 | 位置 | 规模 | must |
|---|---|---|---|
| round1 | `data/private/daily_brief_research/2026-09-16/acu_gold.private.jsonl` | 148 ACU | **28** |
| round2 | `…/acu_gold.round2.private.jsonl` | 1,124 evidence binding | — |
| **仲裁版** | `…/acu_gold.adjudicated.private.jsonl` | **1,102** ACU | **14** |
| 独立 value gate 复核 | `…/value_gate_audit.private.json` | 复核 14 must + 扫描 429 may | **8 must / 3 story** |

`label_adjudication_summary.private.json`：
- old：`exclude 741 / must 77 / may 284`
- new：`exclude 659 / may 429 / must 14`
- **206 行标签被仲裁改写（18.7%）**；`must` 由 77 → 14

**独立 value gate 复核结论**：14 条 `must` 中 **9 条应降级、4 条漏标**。
降级理由分布（`may_scan.not_promoted_reason_counts`）：

| 理由 | 条数 |
|---|---:|
| `single_or_unverified_group_experience` | 146 |
| `same_mainline_support_or_routine_admin` | 103 |
| `third_party_private_or_background` | 91 |
| `open_question_or_plan_without_decision_or_result` | 52 |
| `useful_but_not_material_or_reusable_enough` | 37 |

⇒ **这 4–5 类即现成的 B2 收口规则**，且有 429 条 may 作负例回归。

### F.2 指标实现现状

| roadmap §8 指标 | 状态 | 位置 |
|---|---|---|
| `acu_precision` / `acu_recall` | ✅ 真算 | `tools/daily_brief_eval.py:311-370` |
| `must_recall` | ✅ 真算 | 同上 |
| `trivial_inclusion_rate` | ✅ 真算 | 同上 |
| `redundancy_rate` | ✅ 真算 | 同上 |
| `unsupported_claim_rate` | ✅ 真算 | 同上 |
| `slot_accuracy`（actor/action/object/status） | ⚠️ **词面精确匹配**，O-G 复制金标即得 1.0 | 同上 |
| **weighted ACU coverage** | ❌ 未实现 | — |
| **关系方向错误率** | ❌ 未实现 | — |
| **unknown 被强行确定率** | ❌ 未实现 | — |
| bootstrap 置信区间 | ❌ 零实现 | — |
| 逐日配对比较 | ❌ 零实现 | — |

评测脚本**拒绝合成总分**（正确做法）。双轮对齐工具 `tools/daily_brief_acu_align.py:221-387` 按精确证据 ID 交集对齐，many-to-many 保持 unresolved，**但无 κ 一致性系数**。

### F.3 标注独立性

| 轮次 | 做法 | 问题 |
|---|---|---|
| 2026-08-25 | `ANN_A` / `ANN_B` 两个**正则脚本**互标，`adjudicate_pilot_v2.py` 脚本仲裁 14,349 条字段分歧 | 纯自证 |
| 2026-09-16 | **同一人不同时间**双轮（codebook v1 明文许可） | round1 148 vs round2 1,124，精确证据交集仅对上 **10 条**；18.7% 标签被仲裁改写 |
| 独立 value gate | 第三方复核 14 must | 9 降级 / 4 漏标 |

**三道复核层层层推翻前一层** ⇒ 根因是 `must` 判据本身是高判断负荷的主观阈值，对策是**降维成可执行规则**（见 §F.1 的 4–5 类）。

### F.4 金标的条件性漏洞

`docs/daily-brief-annotation-codebook-v2.md` §6 自陈：

> 本日原始 DB 有 3,925 条消息；两轮入口覆盖 **411 个 content-line candidate** 和去重后的 1,442 个直接 evidence ID。因此本金标准覆盖的是**已进入两轮标注的研究入口**，不声称对全部 3,925 条消息逐条人工标注。

⇒ **上游未进候选的消息永远不会成为 `must`**。以它为分母的 `acu_recall` / `must_recall` **原理上测不出 E5（漏选）与 H1a（重建丢上下文）**。
对策见主报告行动清单 #16（随机抽样无偏探针）。

### F.5 oracle 实验完成度

| 条件 | 状态 | 产物 |
|---|---|---|
| **B0** 当前基线 | ✅ 已跑 | `runs/b0-20260916/{candidate_summary,metrics,mapping_notes}` |
| **O-G** Generation Oracle | ✅ 已跑 | `runs/o-g-20260916/{candidate_summary,metrics_vs_round1,mapping_notes,oracle_report}` |
| **O-S** Selection Oracle | ❌ 未跑 | — |
| **O-R** Reconstruction Oracle | ❌ 未跑 | — |
| **B1** 扁平 Query Baseline | ❌ 未跑 | — |
| **B2** Reconstruction + Query Selection | ⚠️ 单独试跑过 | `output/daily-brief-query-selection-20260916/`（411 → 78 选中） |

`runs/` 目录**缺协议 §8 要求的 `config.private.json` 与 `errors.private.jsonl`**。

### F.6 测试质量

76 个测试函数中约 **69 个真守行为契约**，7 个断言实现细节（pin 死中文文案、schema `minLength`/`maxLength` 常量、路径 / `.gitignore` 源文本、时间窗常量）。
roadmap §10 三底线中「**关系方向**」**零测试**。

---

## G. 实验产物组织度

对照 `docs/daily-brief-oracle-experiment-v1.md` §8 输出目录约定：

| 目录类 | freeze_manifest | error_ledger | 统一 metrics | per-run config/errors | 可做配对比较 |
|---|:--:|:--:|:--:|:--:|:--:|
| `data/private/daily_brief_research/` | ✅ | ✅ | ✅ | ❌ 缺 | ⚠️ 部分 |
| `output/daily-chat-analysis-*` | ❌ | ❌ | ❌ | ❌ | ❌ |
| `output/cross_date_experiment_*` | ❌ | ❌ | ❌ | ❌ | ❌ |
| `output/active_learning_round1_20260831/` | ❌ | ❌ | ❌ | ❌ | ❌ |
| `output/user-profile-calibration-test-20260905/` | ❌ | ❌ | ❌ | ❌ | ❌ |
| `data/private/gold_standard/2026-08-25/` | ⚠️ 有 manifest/aggregate/cost/errors，但早于 §8，口径不一致 | | | | ❌ |

结论：**目前不能支撑消融与时间隔离盲测的配对比较**（roadmap §10 第 4 条无法执行）。

### G.1 三天回归集现状

`freeze_manifest.known_gaps` 自陈：

| 日期 | 输入快照 | 金标 | 可重放 | 其他 |
|---|---|---|---|---|
| 2026-09-07 | ❌ 无独立原始 SQLite 快照 | ❌ | **否** | 2,144 消息 / 29 会话 / 252 卡 |
| 2026-09-08 | ✅ `tmp/briefing_2026-09-08.db` | **❌ 完全无 ACU 标注** | 部分 | **29 个 `briefing_*` 分支产物，无 canonical winner** |
| 2026-09-16 | ⚠️ `output/briefing_2026-09-16.db` **不可读** | ✅（但 must 不稳） | **否** | 3,925 消息 / 63 会话 / 402 卡 |

09-08 的 29 个分支产物：`single_draw` / `single_draw_v2` / `title` / `title_v2` / `title_fixed` / `title_fixed_v2` / `title_fixed_v3` / `selected_final` / `official_style` / `verified` / `accepted` / `final` / `fact_pipeline_*` × 7 …

---

## H. 数据规模与样本代表性

| 项 | 值 |
|---|---|
| `data/wechat_bridge.db` 消息总数 | **60,803** |
| 会话数 | 167（群 67 / 私 100） |
| 发言者 | 904 |
| 日期跨度 | **2025-03-07 ~ 2026-09-15**（约 18 个月） |
| 语音消息 | 270（**0.44%**） |
| `voice_transcripts` 行数 | **1** ⇒ 转写覆盖 ≈ **0.4%** |
| `sync_runs` / `reply_tasks` / `brief_feedback` / `send_attempts` | 25 / 14 / **2** / 0 |

2026-09-16 日窗口（3,925 条 / 63 会话）来自 `message__message_2.db`，bridge 库无当日数据。

$$\text{标注覆盖} = \frac{1 \text{ 天}}{18 \text{ 个月}} \approx 1.8\%$$

**样本代表性**：全部 ACU 金标、oracle、错误账本仅覆盖 2026-09-16 一天（09-07/08 有错误账本但无金标）。任何 `must_recall` / `acu_precision` 数字**只有单日意义，不可外推**——`user-profile-calibration-test-20260905` 的 `coverage_notice` 亦自陈「不可外推」（单日 08-25、30→20 卡样本）。

**反馈闭环现状**：`output/active_learning_round1_20260831/` —— 24 包 debug（4 complete / 20 error）、blind **未执行**、`human_labels` 为**空 `labels{}`**。
⇒ `brief_feedback` 全库仅 2 行，`human_labels` 为空 ⇒ **B2 的 query-conditioned selection 无反馈可依据**（主报告行动 #18 是其前置依赖）。

---

## I. 迭代史年表

| 时段 | 代号 | 在做什么 | 结果 |
|---|---|---|---|
| 2026-08-25 ~ 08-30 | K1-K31 / P0-P0.1.4 / contextual-bundle v1-v2.11 | **协议工程期**：全在单日 2026-08-25 上绕 wire 格式、容量、授权门迭代 | D9 真实金标评测自 08-28 起一直 `blocked`；**零内容选择质量度量**。K7 失败：20 根 → 6,095 叶、证据恢复 0/32、60.6 MB（`docs/recovery-k8-checkpoint-2026-08-28.md`） |
| 2026-08-31 ~ 09-04 | conversation-reconstruction dev→blind→eval v2-v7、cross_date、active_learning | 重建评测期 | 一次性实验，无统一评测入口；blind 轮 `semantic_accuracy` 故意不报 |
| 2026-09-05 | — | 用户审阅意见（`docs/resume-weiyu-optimization-2026-09-05.md`） | 三项待办：取样去重、筛选系统备忘录落地、用户画像试点 |
| 2026-09-16 ~ 09-18 | B0 / O-G / ACU 两轮+仲裁 / error_ledger / freeze_manifest | 日报单日深挖期 | **只完成 2/6 oracle 条件**；`editorial-selection-quality-gate-v1.md` 于 09-18 明文废弃，roadmap v2 取代 |
| 2026-09-17 ~ 09-22 | `daily_brief_selector` + tools + tests + docs | 日报选择器工作线 | **全部未提交** |

**git 提交分布**（22 个）：约 18 个为 docs / rebrand / chore；代码提交仅 3 个（`91695f7` semantic review contracts、`890f4fd` expose semantic failures、`132f5c8` capture tool and editorial pipeline）；`chore: publish wei daily 0.1.4` 一次性灌入 176,672 行。

---

## J. 路径与副本定性

| 路径 | 性质 | 证据 | 处置 |
|---|---|---|---|
| **`D:\Zcode\WeChat_CatchCatch`** | **唯一 git 仓库 = 权威开发树** | `.git/config` origin = `Sutera-Diffusus/WeChat-daily`；含 `src` `tests` `docs` `tools` `desktop` `plugins` `web` 资产 + 最新 `daily_brief_selector.py` | **只在此开发** |
| `D:\Project_Codex\Project_WeChatMoreFunction` | 2026-09-17 的 `*.py` **运行/部署副本** | `.venv/Scripts/wechat-bridge.exe`（pip 安装）、`server.stderr.log`、**3.9 MB 活跃 `wechat_bridge.db-wal`**；缺 `web/`、缺 `daily_brief_selector`、缺 tests/docs；76 个共同模块与主树**逐字等价**（差异仅 CRLF/LF） | 只读历史快照。**WAL 仍活跃，不可当开发树修改** |
| `D:\Project_Codex\_release-upload-0.1.4` | GitHub Release 上传暂存 | portable.zip 101.5 MB + installer.exe 101 MB（4 周前） | 忽略 |
| `D:\Project_Codex\weiyu_promotion_work` | 推广工作区 | awesome 列表收录条目、产品档案、仓库健康记录 | 忽略 |
| `D:\DeepseekHarness_WorkSpace\WeChat_CatchCatch` | **不存在** | — | — |

**发布链与主树强耦合**：`desktop/scripts/build-sidecar.ps1` 引用 `<主树>\.venv`、`<主树>\src`、`src\wechat_bridge\web` ⇒ 构建必须在主树执行。

**待清理**：`.git/worktrees/recover_worktree/gitdir` 指向已删除的 `D:/Project_Codex/Project_WeChatMoreFunction/tmp/recover_worktree/.git` ⇒ stale 元数据，`git worktree prune` 即可。

---

## K. 勘察方法与来源

7 路并行深审 + 主勘察方逐行复核。每条结论均可回溯到具体文件行号或数据产物。

| 切片 | 覆盖范围 | 状态 | 关键产出 |
|---|---|---|---|
| 迭代史考古与方法论诊断 | `docs/**`、`tests/audit_*`、git log/reflog | ✅ | 三段迭代史、方法论根因排序、git 提交分布 |
| 下游成稿链路 | `web.py:_ai_analysis`、`ai.py`、`editorial_reducer.py`、`app.js` | ✅ | 主编默认不跑、主线丢失在候选化截断、三处罐头库在模型之后 |
| 上游重建与切分链路 | `context_*`、`dialogue_*`、`compact_*`、`linear_*`、`semantic_*` | ✅ | 零生产耦合、E1–E4 代码映射、B1 判断 |
| 内容选择 / 分层 / 评分门槛 | `selection_strata`、`semantic_gate`、`analysis.py` 选择逻辑 | ⚠️ **部分** | 子智能体被 provider `content-filter` 中断（读到真实语料触发风控）；缺口由主勘察方以纯代码检索补全 |
| 评测与金标基础设施 | `tools/daily_brief_*`、`annotation_tools`、`data/qa/**` | ✅ | 尺子半成品、must 9/14 误判、指标真伪对照 |
| 真实数据与产出实况 | `data/**`、`output/**`、`artifact/**`、版面截图 | ✅ | 18 个月 60,803 条只标 1 天、四方法对照、版面装饰加厚内容为空 |
| 多副本 / 工作区关系 | 两棵树 + 发布链 + 推广工作区 | ✅ | 权威树确认、无语义分叉、工作线未提交 + 金标在 gitignore 区 |

### K.1 收敛性证据

`data/private/daily_brief_research/freeze_manifest.private.json` 的 `top_bottlenecks`（**勘察开始前已存在，由项目自产**）：

| rank | name | next_experiment |
|---|---|---|
| 1 | 全局内容选择与重复压缩 | 先做 Selection Oracle 和 ACU must/may/exclude 标注，不改篇幅上限 |
| 2 | 交流边界、对象和关系归因 | 做 Reconstruction Oracle 并单独测 actor/action/object/status 槽位 |
| 3 | 证据绑定与拒绝后的回退语义 | **禁止回退冒充正式结果** |

与本轮**独立的 O-G 消融分析**得出的排序完全一致。两条独立路径收敛 ⇒ 结论可信度高于单一来源。

### K.2 局限

- 全部为**静态代码与产物证据**；未运行评测、未改动生产代码或配置。
- 模板标题在实际产出中的占比分布为 **[INFERENCE]**，需脱敏统计脚本验证。
- 选择层阈值盘点（约 40 项）可能仍有遗漏（见切片状态 ⚠️）。
- `output/briefing_2026-09-16.db` 不可读，未能核对其内容。
- 行号对应 2026-09-22 快照，后续改动会漂移。
