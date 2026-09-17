# 语义情报分析调研：来源与方法说明

生成日期：2026-08-26

## 调研问题

如何把高噪声、多会话、碎片化的中文聊天消息转化为可审核的事件、主张、人物观点和趋势，同时避免不同事件被关键词或标题规则错误合并。

## 证据范围

- 学术端：优先采用 ACL Anthology、论文官方页面和论文作者公开代码，覆盖新闻流聚类、跨文档事件共指、事件与论元抽取、说话人归因、主张抽取、多文档摘要、事实一致性和人机协同评测。
- 应用端：优先查看项目官方 GitHub 仓库，关注其数据模型、来源追踪、人工复核和可部署性，而不是只比较模型名称。
- 本地端：只读核对当前仓库的聚类、标题、首页选择和详情渲染路径；没有改动生产代码，也没有用当前真实消息跑新模型基准。

## 核心论文

1. Saravanakumar et al. (2021), [Event-Driven News Stream Clustering using Entity-Aware Contextual Embeddings](https://aclanthology.org/2021.eacl-main.198/). 说明新闻流事件聚类应联合稀疏表示、稠密表示、实体信息和时间，而不是只用共享词或单一语义向量。
2. Örs et al. (2020), [Event Clustering within News Articles](https://aclanthology.org/2020.aespen-1.11/). 将“是否同一事件”建模为句对判断，再根据成对分数构造事件簇。
3. Upadhyay et al. (2023), [Cross-Document Event Coreference Resolution on Discourse Structure](https://aclanthology.org/2023.emnlp-main.294/). 强调跨文档事件共指中的篇章结构和一致性。
4. Bugert et al. (2020), [New Insights into Cross-Document Event Coreference](https://aclanthology.org/2020.nuse-1.1/). 分析事件提及识别、成对共指和文档预聚类的独立影响。
5. Bugert et al. (2021), [Generalizing Cross-Document Event Coreference Resolution Across Multiple Corpora](https://aclanthology.org/2021.cl-3.18/). 表明事件动作、时间等特征的重要性会随语料域变化，不能把一个固定阈值当作通用语义规则。
6. Gong et al. (2025), [EventRelBench](https://aclanthology.org/2025.findings-emnlp.482/). 覆盖共指、时间、因果、超/子事件四类关系；通用 LLM 在事件关系理解上仍明显不足。
7. Peng et al. (2023), [The Devil is in the Details: On the Pitfalls of Event Extraction Evaluation](https://aclanthology.org/2023.findings-acl.586/). 指出预处理、输出空间和缺少端到端管线评估会让局部指标看起来很好、真实管线却失败。
8. Peng et al. (2023), [OmniEvent](https://aclanthology.org/2023.emnlp-demo.46/). 将事件理解分为事件检测、事件论元抽取和事件关系抽取，并统一评估协议。
9. Zhong et al. (2024), [Who Said What: Formalization and Benchmarks for Quote Attribution](https://aclanthology.org/2024.lrec-main.1530/). 把发言内容与说话人配对作为独立任务，并包含中英文评测。
10. Deng et al. (2024), [Document-level Claim Extraction and Decontextualisation for Fact-Checking](https://aclanthology.org/2024.acl-long.645/). 先抽取值得核验的主张，再补齐脱离原文后仍需的上下文。
11. Xiao et al. (2022), [PRIMERA](https://aclanthology.org/2022.acl-long.360/). 面向多文档摘要学习跨文档聚合，但其前提仍是输入文档簇已经正确。
12. Nan et al. (2021), [Entity-level Factual Consistency of Abstractive Text Summarization](https://aclanthology.org/2021.eacl-main.235/). 直接讨论摘要中的实体幻觉和实体级一致性。
13. Laban et al. (2022), [SummaC](https://aclanthology.org/2022.tacl-1.10/). 用句子粒度的 NLI 聚合检测摘要与来源的不一致。
14. Fabbri et al. (2022), [QAFactEval](https://aclanthology.org/2022.naacl-main.187/). 用问答分解评估摘要事实一致性，适合把“谁、什么对象、何时、什么状态”转成可核对问题。
15. Belém et al. (2025), [From Single to Multi: How LLMs Hallucinate in Multi-Document Summarization](https://aclanthology.org/2025.findings-naacl.293/). 显示多文档摘要在不存在相关信息时仍可能生成内容，且错误常表现为不遵循指令或过度泛化。
16. Ribeiro et al. (2020), [Beyond Accuracy: Behavioral Testing of NLP Models with CheckList](https://arxiv.org/abs/2005.04118). 支持用能力矩阵与行为测试发现整体准确率掩盖的关键失败。
17. Weber & Plank (2023), [ActiveAED](https://aclanthology.org/2023.findings-acl.562/). 说明把人工纠错持续放回模型循环可以提高标注错误发现能力。
18. Zhao et al. (2025), [Richer EventCorefBank](https://aclanthology.org/2025.naacl-long.178/). 用事件提及解上下文把困难的文档级标注转成更可操作的句对标注。
19. Hu et al. (2025), [LLM-based Event Relation Extraction with Rationales](https://aclanthology.org/2025.coling-main.500/). 以分区和理由生成提高事件关系抽取的覆盖与可审计性，但仍需任务级验证。
20. Thielmann et al. (2024), [Human in the Loop: Coherent Topics with Few Labels](https://aclanthology.org/2024.lrec-main.736/). 少量人工标签加监督式小样本方法可在主题一致性上优于完全无监督主题模型。

## 重点开源项目

1. [BERTopic](https://github.com/MaartenGr/BERTopic)：主题发现、动态主题、在线/增量、多方面主题；适合作为宽召回和探索工具，不是同一事件裁决器。
2. [OmniEvent](https://github.com/THU-KEG/OmniEvent)：中英文事件检测、论元抽取、统一评估；适合借鉴事件结构和离线基线。
3. [DeepKE](https://github.com/zjunlp/DeepKE)：中文实体、关系和事件抽取，支持 DuEE；适合验证中文结构化抽取能力。
4. [DocEE](https://github.com/Spico197/DocEE)：文档级事件抽取研究工具；适合研究长上下文和跨句论元。
5. [HyperCoref CDCR](https://github.com/UKPLab/emnlp2021-hypercoref-cdcr)：跨文档事件共指语料与实现；适合研究和评测协议参考。
6. [SECURE](https://github.com/taolusi/SECURE)：LLM 与任务模型协作的跨文档事件共指实现；适合做离线对照，不宜未经中文域验证直接上线。
7. [cross-doc-event-coref](https://github.com/AlonEirew/cross-doc-event-coref)：成对预测、层次聚类和 CoNLL 评分的清晰参考实现。
8. [PRIMERA official code](https://github.com/allenai/primer)：多文档摘要参考；只应放在事件边界锁定之后。
9. [OpenCTI](https://github.com/OpenCTI-Platform/opencti)：以 STIX 风格结构、来源、首次/最后出现、置信度和关系图管理情报；值得借鉴数据模型与审计体验，不是聊天 NLP 引擎。
10. [IntelOwl](https://github.com/intelowlproject/IntelOwl)：模块化分析器、连接器、playbook、统一 schema 和人工评价；适合借鉴可重复分析编排。
11. [Argilla](https://github.com/argilla-io/argilla)：面向 NLP/LLM 的人工标注、反馈和持续评测；适合构建错误合并、错误归因和摘要失真反馈集。

## 本地代码核对

- `src/wechat_bridge/analysis.py:261-305`：标题规范化包含针对 GPT/重置/中转/封禁等词的确定性泛化标题规则。
- `src/wechat_bridge/analysis.py:3008-3028`：事件锚点、质量和域兼容性控制聚类，但事件身份仍主要从词项与域标签推断。
- `src/wechat_bridge/analysis.py:3030-3116`：证据、摘要、标题、细节和重要度在同一聚类结果上继续加工，因此上游误合并会被下游放大。
- `src/wechat_bridge/web/app.js:768-875`：`sameTopic` 在展示层继续参与本地与 AI 结果的合并/去重；AI 首页注入有严格来源判断。
- `src/wechat_bridge/web/app.js:951-1034`：`detail_points` 仍进入事件详情渲染。

## 方法限制

- 本轮是结构化文献与项目调研，不是模型性能复现；论文结果只说明方法在其数据集上的表现。
- 大部分跨文档事件共指基准来自新闻或英文语料，微信中文短消息、口语、省略、回复链和群聊语境存在明显域差异。
- GitHub 活跃度、依赖可安装性和许可证需在进入工程试验前重新核对。
- 没有使用当前真实聊天数据估算误合并率，因此报告中的实验样本量是建议起点，不是统计功效保证。
- 报告唯一图表只显示本轮来源对方法领域的覆盖条目数；同一来源可以跨领域计数，数量不代表方法优劣。项目比较仍使用表格，避免把主观“推荐度”画成伪精确分数。

## 图表映射

- 区段：证据覆盖不是性能排名
- 问题：本轮调研是否只偏向聚类，而忽略归因、摘要、评测和应用数据模型？
- 图表：单系列分类柱状图，领域为 x，调研条目数为 y；数据集 `evidence_coverage`
- 结论：证据覆盖了完整管线，事件身份/关系条目最多，但所有下游层均有独立来源
- 配色：单一色根，无图例，不用颜色表达优劣
- 限制：来源可重复计入多个领域，不用于统计推断或项目排名

## 报告渲染 QA 限制

Data Analytics 便携阅读器的桌面顶部栏使用视口宽度。在 Windows 非覆盖滚动条环境中，`100vw` 会包含滚动条宽度，官方浏览器验证因此报告约 8px 的页面级横向溢出。失败截图和元素诊断表明正文、表格与图表没有横向撑宽；本轮没有修改共享插件或在报告内注入覆盖样式。
