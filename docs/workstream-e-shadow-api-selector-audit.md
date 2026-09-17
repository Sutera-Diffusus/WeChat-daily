# Workstream E：Shadow API 与选择器独立审计

状态：2026-08-27，独立契约审计，待生产接入。

本审计只检查公开代码入口和 synthetic contract tests，不读取
`data/private/`、`frozen/` 或任何私有正文；不修改生产 Python、前端或模型
实现。目的不是把当前 AI 接口改名，而是先冻结一条可审阅、可回放、可拒绝
误标的 shadow 读取边界。

## 1. 当前入口与具体接入点

| 文件 / 位置 | 当前行为 | shadow 接入点 | 当前风险 |
| --- | --- | --- | --- |
| `src/wechat_bridge/web.py:684-685` | `/api/overview` 进入 `_overview` | 在窗口计算和本地分析之后增加独立 shadow read handler；默认不调用 | 首页载荷只有 `source: local_sqlite`，没有 `analysis_run_id`、`provider_status` 或显式接受标记，无法区分一次计算、缓存回放和降级 |
| `src/wechat_bridge/web.py:712-727` | `/api/insights` 直接调用 `analyze_messages` | 保留为 legacy/local 入口；shadow 不能暗中替换它 | 规则结果没有运行身份，调用者不能选择或重放指定 run |
| `src/wechat_bridge/web.py:989-1077` | `_overview` 组装首页数据 | 将统一 envelope 只挂到新 shadow endpoint；首页仍消费 legacy overview | 当前首页与 shadow 没有边界，未来若直接替换会造成默认行为漂移 |
| `src/wechat_bridge/web.py:1625-1642` | `_ai_status` 只返回 configured、model 等状态 | 映射到无密钥的 `provider_status`，不能用 `configured` 代替成功 | `configured` 不是一次推理成功；blocked、timeout、schema failure 没有稳定公开状态 |
| `src/wechat_bridge/web.py:1958-2040,2549-2566` | `_ai_analysis` 返回 `ai_assisted`、`ai_assisted_with_local_fallback` 或 `rules_fallback` | shadow API 应在这里之外建立 run envelope，并给 fallback 明确 `llm_accepted: false` | 当前成功/失败 payload 缺 `analysis_run_id`、`provider_status`、`llm_accepted`；`rules_fallback` 会被客户端当作未定义状态处理 |
| `src/wechat_bridge/analysis.py:3939-4099` | `build_ai_context` 与 `analyze_messages` 提供候选和 rules_v1 分析 | 仅作为 shadow 的 candidate/baseline 输入，不负责生成 run id | 纯分析函数没有运行元数据，直接在此增加状态会把计算层和服务选择层耦合 |
| `src/wechat_bridge/ai.py:99-205` | `OpenAIAnalysisGenerator` 以 `configured` 和异常表示 provider 能力 | 由 orchestration 层把异常映射为 `blocked` / `failed`；返回结果需经过 acceptance gate | configured 只表示有 key；provider 失败、固定 schema 拒绝、人工禁用尚未分层 |
| `src/wechat_bridge/web/app.js:1274-1290` | `refresh` 请求 overview/insights、messages 和 `/api/ai-latest` | 默认 feature flag off 时，refresh 禁止请求 shadow | 当前没有 shadow flag；任何新增请求若放在 refresh 会让首页隐式切换语义 |
| `src/wechat_bridge/web/app.js:789-818` | `collectEvents` 合并本地 events 与 AI findings | 统一使用 `llm_accepted` + provider status 的谓词 | 第 801 行 `source !== "ai_assisted"` 是 strict equality，会漏掉有效复合 source；也没有 acceptance gate |
| `src/wechat_bridge/web/app.js:1084-1100` | `renderOverview` 直接渲染 `state.insights` | 继续只渲染 legacy snapshot；shadow 只能由 review 选择器另取 | 当前不存在 run selector；把 shadow 写入 `state.insights` 会污染首页和自动刷新 |
| `src/wechat_bridge/web/app.js:1334-1342` | `runAiAnalysis` 直接 POST `/api/ai-analysis` 并把结果写入 `state.aiResult` | 迁移时需保存 run id/source/status，并拒绝 fallback 作为 LLM accepted | 当前没有验证响应 envelope，网络成功不等于模型被接受 |

因此，最小的生产接入顺序应是：服务端 run envelope → provider/acceptance 映射 →
review-only selector → 最后才考虑任何 UI 展示；不应从首页 `collectEvents` 反向推导
shadow 是否有效。

## 2. 冻结的 Shadow API envelope

建议的独立读取端点为 `/api/shadow-analysis`（如需兼容，可提供别名，但必须共享
同一 envelope 和选择规则）。每次返回必须包含以下公开字段：

```json
{
  "ok": true,
  "analysis_run_id": "run-<opaque-id>",
  "source": "shadow_llm+rules_repair",
  "provider_status": "succeeded",
  "llm_accepted": true,
  "fallback_reason": null,
  "window": {"start": "...", "end": "...", "timezone": "Asia/Shanghai"}
}
```

契约含义：

* `analysis_run_id` 是一次可重放计算的稳定不透明标识；同一缓存/replay 不得每次
  请求随机换 id。它不是消息 id，也不暴露聊天正文或 provider request id。
* `source` 是描述性字符串，允许复合值（例如 `shadow_llm+rules_repair`）。它不能
  单独决定 UI 是否接受结果。
* `provider_status` 只允许公开枚举：`disabled`、`configured`、`succeeded`、
  `blocked`、`failed`。不得包含 key、URL 中的凭证、原始异常或正文。`configured`
  不是成功，`blocked` 不是 AI 已完成。
* `llm_accepted` 是唯一的模型结果接受闸门：只有 fixed schema、证据引用和完整性
  校验均通过且 provider 实际成功时才可为 `true`。规则结果或 provider fallback
  永远为 `false`，即使 `source` 字符串含有 `ai`。
* `fallback_reason` 在降级时必须非空（例如 `provider_blocked`、`timeout`、
  `schema_rejected`），正常成功时为 `null`。它补充 source，不取代
  `provider_status`。

参考选择谓词为：

```text
accepted_for_llm_view =
    llm_accepted == true
    AND provider_status == "succeeded"
    AND source not in {"rules_fallback", "local_rules_fallback"}
```

该谓词有意不对 source 做单值 equality；复合 source 可被接受，但 fallback 不能
满足 `llm_accepted`。UI 不得以 `source === "ai_assisted"` 代替它，也不得因为
HTTP 200 或 `configured=true` 就把结果当成模型判断。

## 3. Feature flag、首页与审阅选择器

冻结 flag 名为 `shadow_analysis_enabled`，默认值必须是 `false`，服务端和客户端
均应 fail-closed。默认首页行为如下：

1. `refresh()` 继续只请求 `/api/overview`（兼容 `/api/insights`）及现有 legacy
   数据；不请求 `/api/shadow-analysis`，不读取 shadow run，也不把 shadow 结果写入
   `state.insights`。
2. 自动刷新、首次加载、无配置 provider、provider blocked 都不能隐式打开 flag。
3. flag 为 off 时，shadow API 可以返回明确的 `disabled` envelope，但不能伪装成
   `llm_accepted=true`。

审阅页是唯一允许显式查看 shadow 的入口。它必须：

* 展示可选择的 `analysis_run_id`（而非按“最新”偷偷替换）；
* 将选中的 id 作为请求参数，读取同一个 run 的 envelope 和证据索引；
* 在切换 run 时不改变首页的 `state.insights`、自动刷新 cursor 或 legacy source；
* 对 `blocked`、`failed`、`disabled` 和 `llm_accepted=false` 显示不可接受状态，仍
  保留可审阅的原因和回放线索。

## 4. 失败与回退边界

| 情况 | provider_status | source 示例 | llm_accepted | 客户端动作 |
| --- | --- | --- | --- | --- |
| flag 关闭 | `disabled` | `shadow_disabled` | `false` | 不请求（显式 review 也只显示 disabled） |
| 没有 provider 配置 | `blocked` | `rules_fallback` | `false` | 保留 rules baseline，禁止显示为 AI |
| provider 网络/配额失败 | `failed` | `rules_fallback` | `false` | 显示失败原因，可重试同一输入，不生成伪 AI run |
| provider 成功、固定 schema 与证据通过 | `succeeded` | `shadow_llm` | `true` | review 可标记为 accepted |
| provider 成功但局部由规则修复 | `succeeded` | `shadow_llm+rules_repair` | 由最终校验决定 | 只有显式 true 才可进入 accepted 视图；不能靠 source 猜测 |

沉默、时间邻近、same-segment 或 HTTP 成功均不改变这张表；它们不是 provider
acceptance 证据。

## 5. Synthetic contract tests 与当前结果

独立测试文件为
`tests/test_shadow_api_selector_contract.py`。测试只读取上述四个公开源码文件和
`index.html`，并用合成 envelope 验证：

| 契约 | 测试 | 当前审计判断 |
| --- | --- | --- |
| source 与 acceptance 解耦、复合 source 可接受、fallback 必拒 | `test_synthetic_shadow_envelope_separates_source_and_acceptance` | 通过（测试内契约） |
| shadow endpoint 与 run/source/provider/acceptance 字段 | `test_shadow_api_declares_run_source_provider_and_acceptance_fields` | 待接入 |
| fallback 显式 `llm_accepted=false` 和 provider status | `test_fallback_branch_explicitly_forbids_llm_acceptance` | 待接入 |
| `shadow_analysis_enabled=false`，首页 refresh 不读 shadow | `test_shadow_flag_defaults_off_and_home_refresh_does_not_read_shadow` | 首页不读 shadow 的部分通过；flag 部分待接入 |
| review 显式选择 `analysis_run_id` | `test_review_surface_can_explicitly_select_an_analysis_run` | 待接入 |
| 不再以单一 source strict equality 过滤 | `test_source_selection_is_not_strict_single_source_equality` | 待接入 |
| 四个公开语义入口仍被覆盖 | `test_public_semantic_entrypoints_are_identified_for_shadow_wiring` | 通过 |

“待接入”是故意的红灯，不应改成 skip 或 mock 以制造绿色；实现完成后必须在无私有
数据的 synthetic 环境中转绿，并保留这些测试作为生产接入门。

## 6. 最低验收门与风险关闭标准

进入生产默认路径前必须全部满足：

1. 每个 shadow response 100% 有 `analysis_run_id`、`source`、`provider_status`、
   `llm_accepted`；run 可重放且没有正文泄漏。
2. provider blocked/failed、schema rejected、规则降级的 `llm_accepted` 为 0；
   不能通过 source 拼接、HTTP 200 或缓存状态绕过。
3. `shadow_analysis_enabled` 的默认值和缺省配置均为 off；首页网络审计证明零次
   shadow 请求，且首页状态不被 review 选择污染。
4. review 选择器可指定任意已存在 run，切换 run 可回放；未知 id 返回可解释的
   `failed`/`disabled`，不回填最新 run。
5. composite source synthetic case 通过；代码中不存在对唯一
   `ai_assisted` 的 strict equality 过滤；选择器只使用 acceptance predicate。
6. `provider_status=blocked`、`llm_accepted=false`、`fallback_reason` 的 UI/API
   快照与日志一致，不能将 fallback 标成 AI accepted。
7. 本测试文件全部通过，并补充 API response、review selector 和 home refresh 的
   无正文 E2E 检查；任一门槛不达标继续迭代，不接入首页生产流量。

