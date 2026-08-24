(function () {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const BEIJING_TZ = "Asia/Shanghai";
  const FEEDBACK_STORAGE_KEY = "wechat-workbench-event-feedback-v1";
  const SIDEBAR_STORAGE_KEY = "wei-daily-sidebar-hidden-v1";

  const TYPE_LABELS = {
    text: "文本", image: "图片", voice: "语音", video: "视频", emoji: "表情",
    location: "位置", link_or_file: "文件 / 链接", file: "文件", link: "链接",
    system: "系统", other: "其他", 文本: "文本", 图片: "图片", 语音: "语音",
    视频: "视频", 动画表情: "表情", 表情: "表情", 文件: "文件", 链接: "链接",
  };

  const VIEW_META = {
    overview: { title: "情报编辑台" },
    feed: { title: "信息流" },
    chats: { title: "会话" },
    workbench: { title: "工作台" },
  };

  const state = {
    view: "overview", period: "day", start: "", end: "",
    feedFilter: "all", feedSearch: "", feedChatId: "all", feedVisibleCount: 140,
    chatSearch: "", contactSearch: "", contactFilter: "all", taskFilter: "all",
    status: null, sync: null, aiStatus: null,
    settings: {
      display: { font_size: "normal", density: "compact", report_theme: "auto" },
      refresh: { enabled: true, interval_ms: 8000 },
      analysis: { auto_enabled: true, interval_ms: 600000, message_threshold: 20 },
      media: { cache_dir: "" },
      ai: { base_url: "", model: "gpt-5.2", api_key_configured: false },
      email: { host: "", port: 465, security: "ssl", username: "", sender: "", password_configured: false },
      voice: { enabled: false, provider: "doubao_asr_v2", app_id: "", resource_id: "volc.seedasr.auc", single_duration_threshold_seconds: 20, chat_cumulative_threshold_seconds: 60, low_confidence_threshold: 0.75 },
      profile: { roles: [], projects: [], organizations: [], key_contacts: [], topics: [], suggestions: [] },
    },
    insights: null, messages: [], chats: [], contacts: [], tasks: [],
    chatPayload: null, contactPayload: null, taskPayload: null,
    selectedChatId: null, paused: false, loading: false, initialized: false, rangeTransition: false,
    pendingSnapshot: null, pendingNewCount: 0, pollTimer: null,
    aiResult: null, focusMessageId: null, dataSignature: "", lastAiRun: 0, lastAiMessageCount: 0, aiRunning: false, progressTimer: null, aiProgressTimer: null, aiStatusHideTimer: null, unformedVisibleCount: 0,
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
    }[char]));
  }

  function formatNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString("zh-CN") : "—";
  }

  function toDate(value) {
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    if (value == null || value === "") return null;
    if (typeof value === "number" || /^\d+(\.\d+)?$/.test(String(value).trim())) {
      const number = Number(value);
      if (!Number.isFinite(number)) return null;
      return new Date(number < 100000000000 ? number * 1000 : number);
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function timestampValue(value) {
    const date = toDate(value);
    return date ? date.getTime() : 0;
  }

  function timestampOf(item) {
    return item && (item.timestamp || item.created_at || item.received_at || item.time || item.create_time || "");
  }

  function beijingDateInput(value) {
    const date = toDate(value) || new Date();
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: BEIJING_TZ, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(date);
    const pick = (type) => (parts.find((part) => part.type === type) || {}).value || "";
    return [pick("year"), pick("month"), pick("day")].join("-");
  }

  function formatTime(value, withDate) {
    const date = toDate(value);
    if (!date) return "—";
    return new Intl.DateTimeFormat("zh-CN", Object.assign({ timeZone: BEIJING_TZ }, withDate
      ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
      : { hour: "2-digit", minute: "2-digit" })).format(date);
  }

  function formatDateLabel(value) {
    const raw = String(value || "").slice(0, 10);
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw.slice(5).replace("-", "月") + "日";
    return raw || "未标注日期";
  }

  function rangeFor(period) {
    const end = beijingDateInput(new Date());
    const startDate = new Date(end + "T00:00:00+08:00");
    if (period === "yesterday") startDate.setDate(startDate.getDate() - 1);
    if (period === "week") startDate.setDate(startDate.getDate() - 6);
    const start = beijingDateInput(startDate);
    return { start, end: period === "yesterday" ? start : end };
  }

  function rangeLabel() {
    return state.start === state.end ? formatDateLabel(state.start) : formatDateLabel(state.start) + " – " + formatDateLabel(state.end);
  }

  function setRange(period) {
    state.period = period;
    const range = rangeFor(period);
    state.start = range.start;
    state.end = range.end;
    state.aiResult = null;
    state.lastAiRun = 0;
    state.lastAiMessageCount = 0;
    state.unformedVisibleCount = 0;
    if ($("#range-start")) $("#range-start").value = state.start;
    if ($("#range-end")) $("#range-end").value = state.end;
    $$(".range-button").forEach((button) => button.classList.toggle("active", button.dataset.period === period));
    renderRangeContext();
  }

  function queryString(extra) {
    const params = new URLSearchParams({ start: state.start, end: state.end });
    if (extra) Object.entries(extra).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") params.set(key, value);
    });
    return "?" + params.toString();
  }

  function showNotice(message, error) {
    const node = $("#notice");
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
    node.classList.toggle("error", Boolean(error));
  }

  function safeName(value, fallback) {
    const text = String(value == null ? "" : value).trim();
    if (!text || /^wxid_[a-z0-9_]+$/i.test(text) || /^gh_[a-z0-9_]+$/i.test(text) || /^\d+$/.test(text)) return fallback;
    if (/^(群成员|联系人|未命名会话|未命名|微信用户|unknown|unknown-chat)$/i.test(text)) return fallback;
    return text;
  }

  function isUsefulName(value) {
    return safeName(value, "") !== "";
  }

  function isGroupValue(item) {
    const chatId = String(item && (item.chat_id || item.chatId || item.conversation_id) || "");
    return Boolean(item && (item.is_group === true || item.chat_type === "group" || /@chatroom$/i.test(chatId)));
  }

  function firstUseful(values, fallback) {
    for (const value of values) if (isUsefulName(value)) return safeName(value, fallback);
    return fallback;
  }

  function chatDisplay(item) {
    const group = isGroupValue(item);
    return firstUseful([
      item && item.chat_remark, item && item.contact_remark, item && item.chat_name,
      item && item.chatName, item && item.conversation_name, item && item.conversationName,
      item && item.display_name, item && item.name,
    ], group ? "群聊" : "未命名会话");
  }

  function senderDisplay(item) {
    if (item && item.is_self === true) return "我";
    const group = isGroupValue(item);
    const values = group
      ? [item && item.sender_remark, item && item.contact_remark, item && item.remark, item && item.group_nickname, item && item.groupNickName, item && item.sender_name, item && item.senderName, item && item.from_name, item && item.nickname]
      : [item && item.sender_remark, item && item.contact_remark, item && item.remark, item && item.sender_name, item && item.senderName, item && item.from_name, item && item.nickname, item && item.name];
    return firstUseful(values, group ? "待识别成员" : chatDisplay(item));
  }

  function avatarText(value) {
    return Array.from(safeName(value, "·"))[0] || "·";
  }

  function refreshIntervalMs() {
    const value = Number(state.settings && state.settings.refresh && state.settings.refresh.interval_ms);
    return Number.isFinite(value) ? Math.max(3000, Math.min(300000, value)) : 8000;
  }

  function analysisIntervalMs() {
    const value = Number(state.settings && state.settings.analysis && state.settings.analysis.interval_ms);
    return Number.isFinite(value) ? Math.max(60000, Math.min(86400000, value)) : 600000;
  }

  function listSetting(value) {
    return Array.isArray(value) ? value.join("、") : String(value || "");
  }

  function stringList(value) {
    if (Array.isArray(value)) return value.flatMap((item) => stringList(item)).filter(Boolean);
    if (value == null || value === "") return [];
    if (typeof value === "string") return value.split(/[,，、;；|\n]+/).map((item) => item.trim()).filter(Boolean);
    return [String(value).trim()].filter(Boolean);
  }

  function isCensusRange() {
    return state.start === state.end && (state.period === "day" || state.period === "yesterday" || state.period === "custom");
  }

  function aiResultMatchesRange(value) {
    const windowValue = value && value.window || {};
    return String(windowValue.start || "") === state.start && String(windowValue.end || "") === state.end;
  }

  function delay(ms) { return new Promise((resolve) => window.setTimeout(resolve, ms)); }

  function setSidebarHidden(hidden, persist) {
    const value = Boolean(hidden);
    document.body.classList.toggle("sidebar-hidden", value);
    const collapse = $("#sidebar-collapse");
    const reveal = $("#sidebar-reveal");
    const sidebar = $(".sidebar");
    if (sidebar) sidebar.setAttribute("aria-hidden", String(value));
    if (collapse) collapse.setAttribute("aria-expanded", String(!value));
    if (reveal) { reveal.hidden = !value; reveal.setAttribute("aria-expanded", String(!value)); }
    if (persist !== false) {
      try { localStorage.setItem(SIDEBAR_STORAGE_KEY, value ? "1" : "0"); } catch (_error) { /* layout preference is optional */ }
    }
  }

  function restoreSidebarState() {
    let hidden = false;
    try { hidden = localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1"; } catch (_error) { /* use expanded default */ }
    setSidebarHidden(hidden, false);
  }

  function updateAiTaskStatus(label, detail, percent, mode) {
    const box = $("#ai-task-status");
    if (!box) return;
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    box.hidden = false;
    box.dataset.state = mode || "running";
    if (label) $("#ai-task-label").textContent = label;
    if (detail) $("#ai-task-detail").textContent = detail;
    $("#ai-task-bar").style.width = value + "%";
    $("#ai-task-percent").textContent = Math.round(value) + "%";
    box.querySelector('[role="progressbar"]').setAttribute("aria-valuenow", String(Math.round(value)));
  }

  function beginAiTaskStatus(silent) {
    if (state.aiProgressTimer) window.clearInterval(state.aiProgressTimer);
    if (state.aiStatusHideTimer) window.clearTimeout(state.aiStatusHideTimer);
    let progress = 8;
    updateAiTaskStatus(silent ? "自动分析正在更新日报" : "AI 正在分析当前日报", "准备消息、图片说明与语音转写", progress, "running");
    state.aiProgressTimer = window.setInterval(() => {
      progress = Math.min(86, progress + Math.max(1, Math.round((88 - progress) / 9)));
      const detail = progress < 30 ? "整理跨会话上下文" : progress < 58 ? "识别话题集群与人物关系" : "撰写简报并核对证据";
      updateAiTaskStatus(null, detail, progress, "running");
    }, 900);
  }

  function finishAiTaskStatus(detail, failed) {
    if (state.aiProgressTimer) window.clearInterval(state.aiProgressTimer);
    state.aiProgressTimer = null;
    updateAiTaskStatus(failed ? "AI 分析未完成" : "AI 分析完成", detail || (failed ? "请检查配置后重试" : "最新内容已编入日报"), 100, failed ? "error" : "done");
    state.aiStatusHideTimer = window.setTimeout(() => {
      const box = $("#ai-task-status");
      if (box) box.hidden = true;
    }, failed ? 6500 : 2600);
  }

  function beginOperation(label, detail, percent) {
    const box = $("#operation-progress"); if (!box) return;
    if (state.progressTimer) window.clearInterval(state.progressTimer);
    box.hidden = false; box.classList.add("is-active");
    updateOperation(label, detail, percent == null ? 8 : percent);
    state.progressTimer = window.setInterval(() => {
      const current = Number($("#operation-progress-value").dataset.value || 0);
      if (current < 82) updateOperation(null, null, Math.min(82, current + Math.max(1, Math.round((82 - current) / 7))));
    }, 650);
  }

  function updateOperation(label, detail, percent) {
    const box = $("#operation-progress"); if (!box || box.hidden) return;
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    if (label) $("#operation-progress-label").textContent = label;
    if (detail) $("#operation-progress-detail").textContent = detail;
    $("#operation-progress-bar").style.width = value + "%";
    $("#operation-progress-value").textContent = Math.round(value) + "%";
    $("#operation-progress-value").dataset.value = String(value);
    box.querySelector('[role="progressbar"]').setAttribute("aria-valuenow", String(Math.round(value)));
  }

  async function finishOperation(detail, failed) {
    const box = $("#operation-progress"); if (!box) return;
    if (state.progressTimer) window.clearInterval(state.progressTimer); state.progressTimer = null;
    box.classList.toggle("is-error", Boolean(failed));
    updateOperation(failed ? "处理未完成" : "处理完成", detail || (failed ? "请重试" : "版面已更新"), 100);
    await delay(failed ? 1200 : 420);
    box.hidden = true; box.classList.remove("is-active", "is-error");
  }

  async function waitForRefreshIdle(maxMs) {
    const started = Date.now();
    while (state.loading && Date.now() - started < (maxMs || 12000)) await delay(80);
    return !state.loading;
  }

  async function switchReportRange(button) {
    if (state.rangeTransition || !button) return;
    state.rangeTransition = true;
    const label = String(button.textContent || "所选日期").trim();
    const overlay = $("#edition-transition");
    beginOperation("切换「" + label + "」", "等待当前读取结束", 6);
    try {
      await waitForRefreshIdle(15000);
      if (overlay) { overlay.hidden = false; overlay.innerHTML = "<span>「" + escapeHtml(label) + "」版</span>"; }
      document.body.classList.add("report-switching");
      await delay(120);
      setRange(button.dataset.period); updateOperation(null, "读取消息与转写文本", 24);
      await waitForRefreshIdle(15000);
      await refresh({ showLoading: true, forceApply: true });
      updateOperation(null, "编排头版与栏目", 88);
      document.body.classList.add("report-switch-ready");
      await delay(260);
      await finishOperation("已切换到「" + label + "」", false);
    } catch (error) {
      showNotice("切换失败：" + error.message, true);
      await finishOperation(error.message, true);
    } finally {
      document.body.classList.remove("report-switching", "report-switch-ready");
      if (overlay) overlay.hidden = true;
      state.rangeTransition = false;
    }
  }

  function parseListSetting(value) {
    return String(value || "").split(/[，,、\n]/).map((item) => item.trim()).filter(Boolean).slice(0, 100);
  }

  function applySettings(value) {
    const settings = value && value.settings ? value.settings : (value || {});
    state.settings = Object.assign({}, state.settings, settings, {
      display: Object.assign({}, state.settings.display, settings.display || {}),
      refresh: Object.assign({}, state.settings.refresh, settings.refresh || {}),
      analysis: Object.assign({}, state.settings.analysis, settings.analysis || {}),
      media: Object.assign({}, state.settings.media, settings.media || {}),
      ai: Object.assign({}, state.settings.ai, settings.ai || {}),
      voice: Object.assign({}, state.settings.voice, settings.voice || {}),
      profile: Object.assign({}, state.settings.profile, settings.profile || {}),
      email: Object.assign({}, state.settings.email, settings.email || {}),
    });
    document.body.dataset.fontSize = state.settings.display.font_size || "normal";
    document.body.dataset.density = state.settings.display.density || "compact";
    document.body.dataset.reportTheme = state.settings.display.report_theme || "auto";
    const setValue = (selector, value) => { const node = $(selector); if (node) node.value = String(value == null ? "" : value); };
    const setChecked = (selector, value) => { const node = $(selector); if (node) node.checked = Boolean(value); };
    setValue("#font-size-setting", state.settings.display.font_size || "normal");
    setValue("#density-setting", state.settings.display.density || "compact");
    setValue("#report-theme-setting", state.settings.display.report_theme || "auto");
    setChecked("#auto-refresh-setting", state.settings.refresh.enabled !== false);
    setValue("#refresh-interval-setting", refreshIntervalMs());
    setChecked("#analysis-auto-setting", state.settings.analysis.auto_enabled !== false);
    setValue("#analysis-interval-setting", analysisIntervalMs());
    setValue("#analysis-threshold-setting", state.settings.analysis.message_threshold == null ? 20 : state.settings.analysis.message_threshold);
    setValue("#media-cache-setting", state.settings.media.cache_dir || "");
    setChecked("#voice-enabled-setting", state.settings.voice.enabled === true);
    setValue("#voice-app-id-setting", state.settings.voice.app_id || "");
    setValue("#voice-single-threshold-setting", state.settings.voice.single_duration_threshold_seconds == null ? 20 : state.settings.voice.single_duration_threshold_seconds);
    setValue("#voice-cumulative-threshold-setting", state.settings.voice.chat_cumulative_threshold_seconds == null ? 60 : state.settings.voice.chat_cumulative_threshold_seconds);
    if ($("#voice-setting-note")) $("#voice-setting-note").textContent = state.settings.voice.enabled ? "已启用" : "未启用";
    setValue("#profile-roles-setting", listSetting(state.settings.profile.roles));
    setValue("#profile-projects-setting", listSetting(state.settings.profile.projects));
    setValue("#profile-organizations-setting", listSetting(state.settings.profile.organizations));
    setValue("#profile-contacts-setting", listSetting(state.settings.profile.key_contacts));
    setValue("#profile-topics-setting", listSetting(state.settings.profile.topics));
    setValue("#ai-base-url-setting", state.settings.ai.base_url || "");
    setValue("#ai-model-setting", state.settings.ai.model || "gpt-5.2");
    setValue("#email-host-setting", state.settings.email.host || "");
    setValue("#email-port-setting", state.settings.email.port || 465);
    setValue("#email-security-setting", state.settings.email.security || "ssl");
    setValue("#email-username-setting", state.settings.email.username || "");
    setValue("#email-sender-setting", state.settings.email.sender || "");
    if ($("#settings-save-note")) $("#settings-save-note").textContent = state.settings.ai.api_key_configured ? "AI Key 已保存" : "保存在本机";
    if ($("#settings-cache-note")) $("#settings-cache-note").textContent = state.settings.media.cache_dir ? "缓存：" + state.settings.media.cache_dir : "原始微信媒体不移动。";
    if ($("#settings-refresh-note")) $("#settings-refresh-note").textContent = state.settings.refresh.enabled === false ? "已关闭" : Math.round(refreshIntervalMs() / 1000) + " 秒";
    renderAiStatus(state.settings.ai);
    updateRefreshControls();
    restartPoller();
  }

  async function loadSettings() {
    try { applySettings(await request("/api/settings")); }
    catch (error) { applySettings(state.settings); showNotice("设置读取失败，使用页面默认值", true); }
  }

  function openSettings() {
    const drawer = $("#settings-drawer");
    const backdrop = $("#settings-backdrop");
    if (!drawer || !backdrop) return;
    drawer.hidden = false; backdrop.hidden = false; drawer.setAttribute("aria-hidden", "false");
    window.requestAnimationFrame(() => { drawer.classList.add("is-open"); backdrop.classList.add("is-open"); });
    window.setTimeout(() => { const field = $("#font-size-setting"); if (field) field.focus(); }, 50);
  }

  function closeSettings() {
    const drawer = $("#settings-drawer");
    const backdrop = $("#settings-backdrop");
    if (!drawer || !backdrop) return;
    drawer.classList.remove("is-open"); backdrop.classList.remove("is-open"); drawer.setAttribute("aria-hidden", "true");
    window.setTimeout(() => { if (!drawer.classList.contains("is-open")) { drawer.hidden = true; backdrop.hidden = true; } }, 220);
  }

  async function saveSettings(event) {
    event.preventDefault();
    const button = $("#settings-save");
    const value = (selector) => ($(selector) && $(selector).value) || "";
    const apiKey = String(value("#ai-api-key-setting")).trim();
    const imageAesKey = String(value("#image-aes-key-setting")).trim();
    const ai = { base_url: String(value("#ai-base-url-setting")).trim(), model: String(value("#ai-model-setting")).trim(), clear_api_key: Boolean($("#ai-clear-key-setting") && $("#ai-clear-key-setting").checked) };
    if (apiKey) ai.api_key = apiKey;
    const media = { cache_dir: String(value("#media-cache-setting")).trim() };
    if (imageAesKey) media.image_aes_key = imageAesKey;
    const voiceToken = String(value("#voice-access-token-setting")).trim();
    const voiceSecret = String(value("#voice-secret-key-setting")).trim();
    const voice = { enabled: Boolean($("#voice-enabled-setting") && $("#voice-enabled-setting").checked), app_id: value("#voice-app-id-setting"), single_duration_threshold_seconds: Number(value("#voice-single-threshold-setting")), chat_cumulative_threshold_seconds: Number(value("#voice-cumulative-threshold-setting")) };
    if (voiceToken) voice.access_token = voiceToken;
    if (voiceSecret) voice.secret_key = voiceSecret;
    if ($("#voice-clear-keys-setting") && $("#voice-clear-keys-setting").checked) { voice.clear_access_token = true; voice.clear_secret_key = true; }
    const emailPassword = String(value("#email-password-setting")).trim();
    const email = { host: value("#email-host-setting"), port: Number(value("#email-port-setting")), security: value("#email-security-setting"), username: value("#email-username-setting"), sender: value("#email-sender-setting"), clear_password: Boolean($("#email-clear-password-setting") && $("#email-clear-password-setting").checked) };
    if (emailPassword) email.password = emailPassword;
    const profile = { roles: parseListSetting(value("#profile-roles-setting")), projects: parseListSetting(value("#profile-projects-setting")), organizations: parseListSetting(value("#profile-organizations-setting")), key_contacts: parseListSetting(value("#profile-contacts-setting")), topics: parseListSetting(value("#profile-topics-setting")) };
    const analysis = { auto_enabled: Boolean($("#analysis-auto-setting") && $("#analysis-auto-setting").checked), interval_ms: Number(value("#analysis-interval-setting")), message_threshold: Number(value("#analysis-threshold-setting")) };
    const payload = { display: { font_size: value("#font-size-setting"), density: value("#density-setting"), report_theme: value("#report-theme-setting") }, refresh: { enabled: Boolean($("#auto-refresh-setting") && $("#auto-refresh-setting").checked), interval_ms: Number(value("#refresh-interval-setting")) }, analysis, media, ai, voice, profile, email };
    button.disabled = true; button.textContent = "保存中…";
    try {
      const result = await request("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      applySettings(result.settings || result); $("#ai-api-key-setting").value = ""; $("#image-aes-key-setting").value = ""; $("#email-password-setting").value = ""; $("#ai-clear-key-setting").checked = false; $("#email-clear-password-setting").checked = false; closeSettings(); showNotice("设置已保存", false); renderOverview();
    } catch (error) { showNotice("设置保存失败：" + error.message, true); }
    finally { button.disabled = false; button.textContent = "保存"; }
  }

  async function request(path, options) {
    const requestOptions = Object.assign({}, options || {});
    const timeoutMs = Math.max(1000, Number(requestOptions.timeoutMs || 45000));
    delete requestOptions.timeoutMs;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, Object.assign({ cache: "no-store", signal: controller.signal }, requestOptions));
      let value;
      try { value = await response.json(); } catch (_error) { throw new Error("服务返回了无法解析的数据"); }
      if (!response.ok) { const error = new Error(value.message || value.error || "请求失败"); error.status = response.status; error.notFound = response.status === 404 || response.status === 405; throw error; }
      return value;
    } catch (error) {
      if (error && error.name === "AbortError") throw new Error("请求超过 " + Math.round(timeoutMs / 1000) + " 秒，已自动取消");
      throw error;
    } finally { window.clearTimeout(timer); }
  }

  async function requestFirst(paths, fallback) {
    let lastError = null;
    for (const path of paths) {
      try { return await request(path); }
      catch (error) { lastError = error; if (!error.notFound) throw error; }
    }
    if (arguments.length > 1) return fallback;
    throw lastError || new Error("没有可用的数据接口");
  }

  function itemsFrom(value) {
    if (Array.isArray(value)) return value;
    if (!value || typeof value !== "object") return [];
    for (const key of ["items", "messages", "results", "data", "conversations", "contacts", "tasks"]) if (Array.isArray(value[key])) return value[key];
    return [];
  }

  function normalizeType(value) {
    const raw = String(value || "other").trim();
    const aliases = { 文本: "text", 图片: "image", 语音: "voice", 视频: "video", 动画表情: "emoji", 表情: "emoji", 文件: "link_or_file", 链接: "link_or_file" };
    return aliases[raw] || raw || "other";
  }

  function numericDuration(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return 0;
    return number > 1000 ? number / 1000 : number;
  }

  function normalizeMessage(item, index) {
    const raw = item && typeof item === "object" ? item : {};
    const nested = raw.voice || raw.audio || raw.media || {};
    const voiceRecord = raw.voice_transcript && typeof raw.voice_transcript === "object" ? raw.voice_transcript : {};
    const chatId = String(raw.chat_id || raw.chatId || raw.conversation_id || raw.chat_name || "unknown-chat");
    const group = Boolean(raw.is_group === true || raw.chat_type === "group" || /@chatroom$/i.test(chatId));
    const id = String(raw.message_id || raw.id || raw.messageId || (chatId + ":" + (raw.timestamp || raw.time || index)));
    const isSelf = raw.is_self === true || raw.self === true || raw.direction === "outbound" || raw.direction === "sent";
    const type = normalizeType(raw.message_type || raw.type || "text");
    const content = String(raw.content == null ? (raw.text == null ? "" : raw.text) : raw.content);
    const transcript = String(voiceRecord.transcript || (typeof raw.voice_transcript === "string" ? raw.voice_transcript : "") || raw.transcript || raw.asr_text || raw.voice_text || raw.recognized_text || nested.transcript || "").trim();
    const duration = numericDuration(voiceRecord.duration_ms || raw.voice_duration || raw.duration_seconds || raw.duration_ms || raw.duration || nested.duration || nested.duration_ms);
    const confidence = Number(voiceRecord.confidence == null ? (raw.voice_confidence == null ? (raw.transcript_confidence == null ? nested.confidence : raw.transcript_confidence) : raw.voice_confidence) : voiceRecord.confidence);
    return Object.assign({}, raw, {
      message_id: id, chat_id: chatId, chat_name: chatDisplay(Object.assign({}, raw, { chat_id: chatId, is_group: group })),
      sender_name: isSelf ? "我" : senderDisplay(Object.assign({}, raw, { chat_id: chatId, is_group: group })),
      message_type: type, content, timestamp: raw.timestamp || raw.created_at || raw.received_at || raw.time || raw.create_time || "", is_self: isSelf, is_group: group,
      media_path: raw.media_path || raw.file_path || raw.path || nested.path || "", media_name: raw.media_name || raw.file_name || raw.filename || nested.name || "", media_md5: raw.media_md5 || raw.md5 || "",
      audio_url: voiceRecord.audio_path ? "/api/voice-audio?message_id=" + encodeURIComponent(id) : (raw.audio_url || raw.voice_url || raw.media_url || nested.url || ""), voice_duration: duration, voice_transcript: transcript,
      voice_status: String(voiceRecord.status || raw.voice_status || raw.asr_status || raw.transcription_status || nested.status || (transcript ? "completed" : "pending")), voice_confidence: Number.isFinite(confidence) ? confidence : null,
      voice_needs_review: Boolean(raw.voice_needs_review || raw.transcript_needs_review || nested.needs_review || (Number.isFinite(confidence) && confidence < .65)),
    });
  }

  function normalizeMessages(value) { return itemsFrom(value).map(normalizeMessage); }
  function newestFirst(items) { return items.slice().sort((left, right) => timestampValue(timestampOf(right)) - timestampValue(timestampOf(left))); }
  function oldestFirst(items) { return items.slice().sort((left, right) => timestampValue(timestampOf(left)) - timestampValue(timestampOf(right))); }
  function messageById(id) { return state.messages.find((item) => String(item.message_id) === String(id)); }

  function messageText(item) {
    if (!item) return "（无内容）";
    if (item.message_type === "voice" && item.voice_transcript) return item.voice_transcript;
    const content = String(item.content || "").trim();
    if (content && !/^\[(动画表情|图片|语音|视频|文件)\]$/.test(content)) return content;
    return item.message_type && item.message_type !== "text" ? "（" + (TYPE_LABELS[item.message_type] || "非文本消息") + "）" : "（无文本内容）";
  }

  function isMedia(item) { return Boolean(item && item.message_type && item.message_type !== "text" && item.message_type !== "system"); }
  function formatDuration(value) { const seconds = Math.max(0, Math.round(Number(value) || 0)); return seconds >= 60 ? Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0") : seconds + " 秒"; }
  function mediaUrl(item, kind) {
    const explicit = item.audio_url || item.media_url || item.file_url || "";
    if (explicit && !/^file:/i.test(explicit)) return explicit;
    return "/api/media?message_id=" + encodeURIComponent(String(item.message_id)) + "&kind=" + encodeURIComponent(kind || item.message_type || "media");
  }

  function mediaHtml(item) {
    const label = TYPE_LABELS[item.message_type] || "媒体";
    const path = String(item.media_path || "").trim();
    const name = String(item.media_name || "").trim();
    let details = name ? '<span class="media-name">' + escapeHtml(name) + "</span>" : "";
    if (path) details += '<span class="media-path" title="' + escapeHtml(path) + '">本地路径：' + escapeHtml(path) + "</span>";
    if (item.message_type === "voice") {
      const transcript = item.voice_transcript;
      const review = item.voice_needs_review || item.voice_status === "review";
      const transcriptHtml = transcript ? '<span class="voice-transcript">' + escapeHtml(transcript) + (review ? '<span class="voice-review-tag">待校对</span>' : "") + "</span>" : '<span class="voice-transcript pending">尚未转写</span><button class="voice-transcribe-button" type="button" data-voice-transcribe="' + escapeHtml(item.message_id) + '">转写语音</button>';
      const playable = Boolean(item.audio_url || item.media_path);
      const player = playable ? '<audio class="voice-player" data-voice-audio="' + escapeHtml(item.message_id) + '" controls preload="none" src="' + escapeHtml(mediaUrl(item, "voice")) + '"></audio>' : "";
      const mediaState = playable ? "可播放" : (transcript ? "微信原生转写" : "音频尚未提取");
      return '<div class="media-card voice-card"><span class="media-icon">◖</span><div class="media-details"><span class="media-title">语音' + (item.voice_duration ? " · " + escapeHtml(formatDuration(item.voice_duration)) : "") + "</span>" + details + player + '<span class="media-status" data-voice-status="' + escapeHtml(item.message_id) + '">' + mediaState + "</span>" + transcriptHtml + "</div></div>";
    }
    if (item.message_type === "image") {
      const id = escapeHtml(item.message_id);
      details += '<img class="media-thumb" data-media-thumb="' + id + '" src="' + escapeHtml(mediaUrl(item, "image")) + '" alt="微信图片" loading="lazy" /><button class="media-preview-button" type="button" data-preview-media="' + id + '">打开图片</button><span class="media-status" data-media-status="' + id + '">本地预览</span>';
    } else if (!path && item.media_md5) details += '<span class="media-path">本地索引：' + escapeHtml(item.media_md5) + "</span>";
    else if (!path) details += '<span class="media-path">已记录类型，等待媒体路径</span>';
    if (item.content && !/^\[(动画表情|图片|语音|视频|文件)\]$/.test(String(item.content).trim())) details += '<span class="media-caption">' + escapeHtml(item.content) + "</span>";
    return '<div class="media-card"><span class="media-icon">' + (item.message_type === "image" ? "▧" : "↗") + '</span><div class="media-details"><span class="media-title">' + escapeHtml(label) + "</span>" + details + "</div></div>";
  }

  function bindMediaControls() {
    $$('[data-media-thumb]').forEach((image) => {
      if (image.dataset.bound === "true") return;
      image.dataset.bound = "true";
      image.addEventListener("error", () => {
        image.classList.add("is-unavailable");
        const status = $$('[data-media-status]').find((node) => node.dataset.mediaStatus === image.dataset.mediaThumb);
        if (status) status.textContent = "图片暂不可解码，保留本地路径";
      });
    });
    $$('[data-voice-audio]').forEach((audio) => {
      if (audio.dataset.bound === "true") return;
      audio.dataset.bound = "true";
      audio.addEventListener("error", () => {
        const status = $$('[data-voice-status]').find((node) => node.dataset.voiceStatus === audio.dataset.voiceAudio);
        if (status) status.textContent = "音频暂不可播放，等待解码文件";
      });
    });
  }

  function fallbackInsights() {
    const inbound = state.messages.filter((item) => item.is_self !== true).length;
    return { window: { start_date: state.start, end_date: state.end, timezone: BEIJING_TZ }, narrative: "", summary: { messages: state.messages.length, inbound, self: state.messages.length - inbound, chats: new Set(state.messages.map((item) => item.chat_id)).size, events: 0, actions: 0, high_value: 0, unformed_dynamics: 0 }, quality: { analysis_coverage: 0 }, highlights: [], actions: [], events: [], primary_insights: [], topic_briefs: [], unformed_dynamics: [], topics: [], top_chats: [], hourly: [], freshness: { source: "本地消息库" } };
  }

  function normalizeInsights(value) {
    if (!value || typeof value !== "object") return fallbackInsights();
    return value.insights && typeof value.insights === "object" ? value.insights : value;
  }

  function buildChats(apiValue) {
    const map = new Map();
    itemsFrom(apiValue).forEach((item) => {
      const id = String(item.chat_id || item.chatId || item.conversation_id || item.chat_name || "unknown-chat");
      map.set(id, { chat_id: id, chat_name: chatDisplay(Object.assign({}, item, { chat_id: id })), is_group: Boolean(item.is_group === true || item.chat_type === "group" || /@chatroom$/i.test(id)), messages: Number(item.messages || item.message_count || 0), inbound: Number(item.inbound || item.received || 0), high_signal: Number(item.high_signal || item.high_value || 0), unread: Number(item.unread || item.unread_count || 0), last_at: item.last_timestamp || item.last_at || item.last_message_at || "", latest_text: item.latest_text || item.last_message || "" });
    });
    state.messages.forEach((item) => {
      const id = String(item.chat_id);
      const existing = map.get(id);
      const row = existing || { chat_id: id, chat_name: chatDisplay(item), is_group: isGroupValue(item), messages: 0, inbound: 0, high_signal: 0, unread: 0, last_at: "", latest_text: "" };
      row.chat_name = firstUseful([row.chat_name, item.chat_name], row.is_group ? "群聊" : "未命名会话");
      row.is_group = row.is_group || isGroupValue(item);
      if (!existing) { row.messages += 1; if (!item.is_self) row.inbound += 1; }
      if (!row.last_at || timestampValue(timestampOf(item)) > timestampValue(row.last_at)) { row.last_at = timestampOf(item); row.latest_text = messageText(item); }
      map.set(id, row);
    });
    return Array.from(map.values()).sort((left, right) => timestampValue(right.last_at) - timestampValue(left.last_at) || right.messages - left.messages);
  }

  function buildContacts() {
    return state.chats.map((chat) => ({ contact_id: chat.chat_id, name: chat.chat_name, type: chat.is_group ? "group" : "person", is_group: chat.is_group, messages: chat.messages, inbound: chat.inbound, high_signal: chat.high_signal, last_at: chat.last_at, latest_text: chat.latest_text }));
  }

  function normalizeContacts(value) {
    const items = itemsFrom(value);
    if (!items.length) return buildContacts();
    return items.map((item) => { const group = Boolean(item.is_group === true || item.chat_type === "group"); return { contact_id: String(item.contact_id || item.id || item.chat_id || item.display_name || "contact"), name: firstUseful([item.display_name, item.name, item.chat_remark, item.chat_name], group ? "群聊" : "联系人"), type: group ? "group" : "person", is_group: group, messages: Number(item.message_count || item.messages || 0), inbound: Number(item.inbound_count || item.inbound || 0), high_signal: Number(item.high_signal || 0), last_at: item.last_timestamp || item.last_at || item.last_message_at || "", latest_text: item.last_message || item.latest_text || "" }; });
  }

  function highlightIds() {
    const ids = new Set();
    const insights = state.insights || {};
    [...(insights.highlights || []), ...(insights.actions || [])].forEach((item) => { if (item && item.message_id) ids.add(String(item.message_id)); });
    collectEvents(insights).forEach((event) => evidenceFor(event).forEach((item) => { if (item.message_id) ids.add(String(item.message_id)); }));
    return ids;
  }

  function buildTasks(apiValue) {
    const result = []; const seen = new Set();
    const push = (item, fallback) => {
      const raw = Object.assign({}, fallback || {}, item || {});
      const id = String(raw.message_id || raw.source_message_id || raw.messageId || raw.id || "task:" + result.length);
      if (seen.has(id)) return; seen.add(id);
      const source = messageById(raw.message_id || raw.source_message_id || raw.messageId);
      const merged = Object.assign({}, source || {}, raw, { message_id: String(raw.message_id || raw.source_message_id || raw.messageId || (source && source.message_id) || id), chat_name: firstUseful([raw.chat_name, source && source.chat_name], "未命名会话"), sender_name: firstUseful([raw.sender_name, source && source.sender_name], "联系人"), content: raw.content || (source && messageText(source)) || "", timestamp: raw.timestamp || (source && source.timestamp) || "", status: raw.status || "待确认", tags: Array.isArray(raw.tags) ? raw.tags : [], importance: Number(raw.importance || raw.score || 0) });
      merged.is_high = highlightIds().has(merged.message_id) || raw.level === "high" || merged.importance >= 70; result.push(merged);
    };
    itemsFrom(apiValue).forEach((item) => push(item));
    (state.insights && state.insights.actions || []).forEach((item) => push(item));
    return result.sort((left, right) => Number(right.is_high) - Number(left.is_high) || timestampValue(timestampOf(right)) - timestampValue(timestampOf(left)));
  }

  function renderStatus(value, sync) {
    state.status = value || {}; state.sync = sync || (value && value.sync) || null;
    const adapter = state.status.adapter || {};
    const running = Boolean(state.status.started && adapter.ok !== false);
    const node = $("#service-state");
    node.innerHTML = '<span class="status-dot' + (running ? "" : " status-dot-warn") + '"></span><span>' + escapeHtml(running ? "本地服务正常" : "服务需要检查") + "</span>";
    node.classList.toggle("is-warn", !running);
    $("#sidebar-live-scope").textContent = safeName(state.status.live_scope, "—");
    $("#sidebar-history-scope").textContent = safeName(state.status.history_scope, "—");
    $("#sync-time").textContent = state.sync && state.sync.state === "running" ? "同步中…" : state.status.last_sync_at ? "更新 " + formatTime(state.status.last_sync_at, true) : "尚未同步";
    const details = [["服务", state.status.started ? "运行中" : "未启动"], ["监听", state.status.live_scope || "—"], ["历史", state.status.history_scope || "—"], ["发送", state.status.send_enabled ? "界面禁用" : "已锁定"], ["适配器", adapter.adapter_name || "—"], ["版本", adapter.adapter_version || "—"], ["时区", state.status.timezone || BEIJING_TZ]];
    const runtimeDetails = $("#runtime-details");
    if (runtimeDetails) runtimeDetails.innerHTML = details.map((item) => '<div class="detail-row"><span>' + escapeHtml(item[0]) + '</span><strong title="' + escapeHtml(item[1]) + '">' + escapeHtml(item[1]) + "</strong></div>").join("");
    renderRangeContext();
  }

  const EDITORIAL_MOTTO = "everywordmatter";

  function renderRangeContext() {
    const node = $("#range-readable");
    if (node) node.textContent = rangeLabel();
    const mode = $("#range-mode");
    if (mode) mode.textContent = isCensusRange() ? "全量普查" : "周报提炼";
    const heading = $("#daily-heading");
    if (heading) heading.textContent = state.period === "yesterday" ? "昨日发生了什么" : state.period === "week" ? "近七日发生了什么" : state.period === "day" ? "今天发生了什么" : "这段时间发生了什么";
    const trendingHeading = $("#trending-heading");
    if (trendingHeading) trendingHeading.textContent = state.period === "yesterday" ? "昨日话题" : state.period === "week" ? "七日要闻" : state.period === "day" ? "今日话题" : "期间话题";
    const trendingKicker = $("#trending-kicker");
    if (trendingKicker) trendingKicker.textContent = state.period === "yesterday" ? "YESTERDAY'S STORIES" : state.period === "week" ? "SEVEN-DAY STORIES" : state.period === "day" ? "TODAY'S STORIES" : "PERIOD STORIES";
    if ($("#newspaper-date")) $("#newspaper-date").textContent = rangeLabel();
    if ($("#newspaper-edition")) $("#newspaper-edition").textContent = isCensusRange() ? "DAILY CENSUS" : "SEVEN-DAY REVIEW";
    if ($("#newspaper-issue")) $("#newspaper-issue").textContent = String(state.end || "").replaceAll("-", "");
    if ($("#seasonal-verse")) $("#seasonal-verse").textContent = EDITORIAL_MOTTO;
    document.body.dataset.reportMode = isCensusRange() ? "census" : "digest";
  }

  function renderAiStatus(value) {
    state.aiStatus = value || {};
    const configured = Boolean(state.aiStatus.configured || state.aiStatus.api_key_configured);
    aiButtons().forEach((button) => {
      button.disabled = !configured || state.aiRunning;
      button.title = configured ? "手动发送脱敏候选文本；不会发送微信消息" : "未配置 AI 服务";
    });
    const note = $("#ai-status-note");
    if (note) note.textContent = configured ? "已配置 · 支持首页手动更新与自动分析" : "未配置 · 使用本地规则";
    const dot = $(".ai-status-line .status-dot");
    if (dot) dot.classList.toggle("status-dot-muted", !configured);
    renderAiFreshness();
  }

  function aiButtons() { return [$("#ai-analysis-button"), $("#ai-analysis-home-button")].filter(Boolean); }

  function renderAiFreshness(value) {
    const node = $("#analysis-refresh-time"); if (!node) return;
    if (state.aiRunning) { node.textContent = "分析中…"; node.title = "正在生成最新简报"; return; }
    const timestamp = value && value.generated_at ? Date.parse(value.generated_at) : state.lastAiRun;
    node.textContent = timestamp ? formatTime(new Date(timestamp).toISOString(), true) : "等待分析";
    node.title = timestamp ? "上次 AI 分析：" + new Date(timestamp).toLocaleString("zh-CN") : "尚未运行 AI 分析";
  }

  function updateHeader() {
    const meta = VIEW_META[state.view] || VIEW_META.overview;
    document.body.dataset.activeView = state.view;
    const eyebrow = $("#header-eyebrow"); if (eyebrow) eyebrow.textContent = meta.title;
    const title = $("#view-title"); if (title) title.textContent = meta.title;
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === state.view));
    $$(".view-panel").forEach((panel) => { const active = panel.dataset.viewPanel === state.view; panel.hidden = !active; panel.classList.toggle("active", active); });
  }

  function setView(view) {
    state.view = VIEW_META[view] ? view : "overview";
    if (location.hash.slice(1) !== state.view) history.replaceState(null, "", "#" + state.view);
    updateHeader();
    if (state.view === "feed") renderFeed();
    else if (state.view === "chats") renderChats();
    else if (state.view === "workbench") { renderTasks(); renderAnalysis(); }
  }

  function eventKey(event) {
    const ids = evidenceFor(event).map((item) => String(item.message_id || "")).filter(Boolean).sort();
    return String(event.id || event.event_id || (ids.length ? "event:" + ids.slice(0, 8).join("|") : "event:" + (event.title || event.summary || Math.random())));
  }

  function evidenceFor(event) {
    const rawValues = Array.isArray(event && event.evidence) ? event.evidence : (Array.isArray(event && event.evidence_samples) ? event.evidence_samples : []);
    return rawValues.map((raw) => {
      const value = typeof raw === "string" ? { message_id: raw } : Object.assign({}, raw || {});
      const source = value.message_id ? messageById(value.message_id) : null;
      const merged = Object.assign({}, source || {}, value);
      const sender = firstUseful([value.sender_name, value.sender, source && senderDisplay(source)], "待识别成员");
      const chat = firstUseful([value.chat_name, value.conversation_name, source && chatDisplay(source)], "会话");
      const quote = String(value.quote || value.statement || value.content || (source && messageText(source)) || "").trim();
      return Object.assign(merged, { message_id: String(value.message_id || (source && source.message_id) || ""), sender_name: sender, chat_name: chat, timestamp: value.timestamp || (source && source.timestamp) || "", quote: quote || "（无文本内容）", is_self: value.is_self === true || (source && source.is_self === true) });
    });
  }

  function eventLane(event) {
    const raw = String(event && event.lane || "").toLowerCase();
    if (raw === "for_me" || raw === "personal" || event.for_me === true) return "personal";
    if (raw === "trending" || raw === "group_hot" || event.trending === true) return "trending";
    if (raw === "pending" || raw === "pending_review" || event.pending === true) return "pending";
    const tags = stringList(event.tags);
    if (event.multi_attention || tags.includes("多人关注") || Number(event.related_chat_count || 0) >= 2) return "trending";
    return "pending";
  }

  function mergeEvidence(left, right) {
    const all = [...evidenceFor(left), ...evidenceFor(right)];
    const seen = new Set();
    return all.filter((item) => { const key = item.message_id || [item.sender_name, item.chat_name, item.timestamp, item.quote].join("|"); if (seen.has(key)) return false; seen.add(key); return true; }).slice(0, 16);
  }

  function topicTokens(event) {
    const text = [event.title, event.summary, ...(stringList(event.tags)), ...(stringList(event.keywords))].filter(Boolean).join(" ").toLowerCase();
    const stop = new Set(["讨论", "关注", "引发", "更新", "用户", "群聊", "多人", "相关", "问题", "情况", "消息", "风险", "建议", "进行", "成为", "出现"]);
    const tokens = new Set((text.match(/[a-z][a-z0-9._+-]{2,}/g) || []).filter((item) => !["the", "and", "with"].includes(item)));
    (text.match(/[\u4e00-\u9fff]{2,}/g) || []).forEach((run) => {
      for (let index = 0; index < run.length - 1; index += 1) {
        const token = run.slice(index, index + 2);
        if (!stop.has(token)) tokens.add(token);
      }
    });
    return tokens;
  }

  function sameTopic(left, right) {
    const leftIds = new Set(evidenceFor(left).map((item) => String(item.message_id || "")).filter(Boolean));
    if (evidenceFor(right).some((item) => leftIds.has(String(item.message_id || "")))) return true;
    const a = topicTokens(left); const b = topicTokens(right);
    let chineseMatches = 0;
    for (const token of a) {
      if (!b.has(token)) continue;
      if (/^[a-z0-9]/.test(token)) return true;
      chineseMatches += 1;
      if (chineseMatches >= 2) return true;
    }
    return false;
  }

  function collectEvents(insights) {
    const sources = [];
    const census = isCensusRange();
    ["for_me", "trending", "pending_review", "event_briefs", "primary_insights"].forEach((key) => (insights[key] || []).forEach((event) => {
      const title = String(event.title || "");
      if (!census && Number(event.importance || 0) < 65) return;
      if (!census && /(?:感觉|觉得|一样|聊明白|我昨天发现|会暴毙吗)/i.test(title)) return;
      sources.push(Object.assign({}, event, { lane: event.lane || (key === "for_me" ? "for_me" : key === "trending" ? "trending" : key === "pending_review" ? "pending" : "") }));
    }));
    if (!sources.length) (insights.events || []).forEach((event) => sources.push(event));
    const aiFindings = state.aiResult && state.aiResult.analysis && state.aiResult.analysis.findings || [];
    aiFindings.forEach((finding, index) => {
      if (!state.aiResult || state.aiResult.source !== "ai_assisted") return;
      const rawEvidence = Array.isArray(finding.evidence) ? finding.evidence : [];
      if (!finding.why_it_matters || !rawEvidence.length) return;
      const categoryWeight = ({ risk: 10, event: 8, progress: 7, knowledge: 5, theme: 4, resource: 2, question: 1 })[String(finding.category || "").toLowerCase()] || 3;
      const draftedPriority = 48 + Math.min(15, rawEvidence.length * 3) + categoryWeight + (String(finding.narrative || "").length >= 150 ? 4 : 0);
      const effectiveImportance = Number(finding.importance || 0) < 50 ? draftedPriority : Number(finding.importance || 0);
      if (effectiveImportance < 55 || Number(finding.confidence || 0) < 55) return;
      const evidence = rawEvidence.filter((item) => item && typeof item === "object").map((item) => Object.assign({}, item, { quote: item.content }));
      const chatIds = new Set(evidence.map((item) => item.chat_name).filter(Boolean));
      const participantIds = new Set(evidence.map((item) => item.sender_name).filter(Boolean));
      const personal = evidence.some((item) => { const source = messageById(item.message_id); return source && !source.is_group; });
      const groupDiscussion = !personal && participantIds.size >= 2;
       sources.push({ id: "ai:" + index + ":" + (finding.title || "finding"), title: finding.title, summary: finding.summary, narrative: finding.narrative || finding.summary, what_changed: finding.what_changed, why_it_matters: finding.why_it_matters, uncertainty: finding.uncertainty, next_step: finding.next_step, core_conclusion: finding.core_conclusion || finding.why_it_matters, importance: effectiveImportance, confidence: finding.confidence, status: "confirmed", lane: personal ? "for_me" : (chatIds.size >= 2 || groupDiscussion ? "trending" : "pending"), tags: stringList(finding.keywords).concat(stringList([finding.category, finding.value_type])), evidence, is_ai_brief: true });
    });
    const map = new Map();
    sources.forEach((event) => {
      if (!event || (!event.title && !event.summary && !event.evidence)) return;
      const ids = evidenceFor(event).map((item) => String(item.message_id || "")).filter(Boolean).sort();
      const key = String(event.id || event.event_id || (ids.length ? "evidence:" + ids.slice(0, 8).join("|") : "title:" + (event.title || event.summary)));
      const current = map.get(key);
      if (!current) map.set(key, Object.assign({}, event, { _key: key, _lane: eventLane(event), evidence: evidenceFor(event) }));
      else {
        const preferred = eventLane(event) === "personal" || (eventLane(event) === "trending" && current._lane === "pending");
        map.set(key, Object.assign({}, current, event, { _key: key, _lane: preferred ? eventLane(event) : current._lane, evidence: mergeEvidence(current, event) }));
      }
    });
    const values = Array.from(map.values());
    const aiItems = values.filter((item) => item.is_ai_brief);
    const localItems = [];
    values.filter((item) => !item.is_ai_brief).forEach((item) => {
      const duplicate = localItems.find((current) => sameTopic(current, item));
      if (!duplicate) { localItems.push(item); return; }
      const preferredLane = duplicate._lane === "personal" || item._lane !== "personal" ? duplicate._lane : "personal";
      const mergedEvidence = mergeEvidence(duplicate, item);
      if (Number(item.importance || 0) > Number(duplicate.importance || 0)) Object.assign(duplicate, item);
      Object.assign(duplicate, { _lane: preferredLane, evidence: mergedEvidence });
    });
    const consumedAi = new Set();
    const enrichedLocal = localItems.map((local) => {
      const localIds = new Set(evidenceFor(local).map((entry) => String(entry.message_id || "")).filter(Boolean));
      let best = null; let bestOverlap = 0;
      aiItems.forEach((ai) => {
        const overlap = evidenceFor(ai).filter((entry) => localIds.has(String(entry.message_id || ""))).length;
        const semantic = sameTopic(local, ai) ? 1 : 0;
        if (overlap + semantic > bestOverlap) { best = ai; bestOverlap = overlap + semantic; }
      });
      if (!best || bestOverlap === 0) return local;
      consumedAi.add(best._key);
      return Object.assign({}, local, best, { _key: local._key, _lane: local._lane === "personal" ? "personal" : best._lane, evidence: mergeEvidence(local, best), is_ai_brief: true });
    });
    const combined = enrichedLocal.concat(aiItems.filter((item) => !consumedAi.has(item._key)));
    return combined.sort((left, right) => { const rank = { personal: 3, trending: 2, pending: 1 }; return (rank[right._lane] || 0) - (rank[left._lane] || 0) || Number(right.importance || 0) - Number(left.importance || 0) || timestampValue(right.end || right.timestamp) - timestampValue(left.end || left.timestamp); });
  }

  function eventStatus(event) {
    const status = String(event.status || "").toLowerCase();
    if (status === "ongoing" || status === "progress") return ["ongoing", "持续进展"];
    if (status === "confirmed" || status === "done") return ["confirmed", "已确认"];
    return ["pending", "待核实"];
  }

  function feedbacks() {
    try { return JSON.parse(localStorage.getItem(FEEDBACK_STORAGE_KEY) || "{}"); } catch (_error) { return {}; }
  }

  function feedbackFor(key) { return feedbacks()[key] || ""; }

  async function setFeedback(key, value) {
    const data = feedbacks(); data[key] = value;
    try { localStorage.setItem(FEEDBACK_STORAGE_KEY, JSON.stringify(data)); } catch (_error) { /* local feedback is optional */ }
    await request("/api/brief-feedback", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_id: key, action: value }),
    });
    const card = $('[data-event-card="' + String(key).replace(/"/g, '\\"') + '"]');
    if (card) {
      card.dataset.feedbackState = value;
      card.querySelectorAll("[data-feedback]").forEach((node) => node.classList.toggle("selected", node.dataset.feedback === value));
    }
  }

  async function transcribeVoice(button) {
    const messageId = button.dataset.voiceTranscribe;
    button.disabled = true; button.textContent = "转写中…";
    try {
      await request("/api/voice-transcribe", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_id: messageId }),
      });
      showNotice("语音已转写", false); await refresh({ forceApply: true });
    } catch (error) {
      showNotice("语音转写失败：" + error.message, true);
      button.disabled = false; button.textContent = "重试转写";
    }
  }

  function evidenceHtml(item) {
    const target = item.message_id ? ' data-open-message="' + escapeHtml(item.message_id) + '"' : "";
    return '<article class="evidence-row"' + target + '><div class="evidence-avatar">' + escapeHtml(avatarText(item.sender_name)) + '</div><div class="evidence-main"><div class="evidence-top"><strong>' + escapeHtml(item.sender_name) + '</strong><span>' + escapeHtml(item.chat_name) + '</span><time>' + escapeHtml(formatTime(item.timestamp, true)) + '</time></div><p class="evidence-quote">' + escapeHtml(item.quote) + "</p></div></article>";
  }

  function editorialTitle(event) {
    const value = String(event && event.title || "").replace(/\s+/g, "").trim();
    const vague = /^(?:相关讨论|相关话题|某某话题|话题持续升温|引发关注|相关内容|讨论引关注|最新消息|今日动态|事件候选)/;
    const weakEnding = /(?:受关注|获认可|引关注|值得关注|持续升温|相关讨论|链接分享|问题待解|对比待解|待确认)$/;
    if (value.length >= 8 && value.length <= 24 && !vague.test(value) && !weakEnding.test(value) && !/^(?:我|你|他|她|我们|大家|昨天|今天|刚刚|但是|然后|感觉|觉得)/.test(value)) return value;
    const text = [value, event && event.summary, event && event.narrative, event && event.what_changed, event && event.why_it_matters, event && event.uncertainty, ...(stringList(event && event.tags))].filter(Boolean).join(" ");
    if (/gpt/i.test(text) && /封号|封禁/.test(text) && /成本|价格|贵|费用/.test(text)) return "GPT困局：封号与成本夹击";
    if (/claude/i.test(text) && /gpt/i.test(text) && /可靠|靠谱|耐用|信任/.test(text)) return "模型分野：Claude更受信任";
    if (/额度|goal|限额/i.test(text) && /重置|reset/i.test(text) && /消耗|用量|速率/.test(text)) return "额度重置：消耗焦虑紧随而来";
    if (/链接|网址|外链|资源/.test(text) && /不明|不足|待核|无法确认|缺少上下文/.test(text)) return "外链汇集：价值仍待核验";
    if (/对比|比较|哪个好|好用吗/.test(text) && /工具|api|接口|pool/i.test(text)) return "工具之问：两种方案尚待比较";
    const topic = /选课|课程|教务|课表|学分/.test(text) ? "选课安排" : /配置|config|statusline|session/.test(text) ? "配置设计" : /风险|故障|异常|失败|封禁|合规/.test(text) ? "账号风控" : /模型|GPT|Claude|DeepSeek|token|多模态/.test(text) ? "模型讨论" : /搜索|工具|插件|API|接口|agent/.test(text) ? "工具链" : /项目|平台|比赛|组队|方案/.test(text) ? "项目推进" : "讨论主线";
    const suffix = /风险|故障|异常|失败|封禁|合规/.test(text) ? "异常信号开始集中" : /改成|换成|采用|调整|更新|决定|确认/.test(text) ? "方案进入调整阶段" : /问题|疑问|请问|如何|怎么|是否/.test(text) ? "关键问题仍待核实" : /价格|成本|费用|额度|收费/.test(text) ? "成本与选择出现分歧" : "观点逐步形成共识";
    return topic + "：" + suffix;
  }

  function richActorText(value, evidence, event) {
    const eventNames = ["participants", "actors", "people", "related_people", "sender_names"].flatMap((key) => stringList(event && event[key]));
    const evidenceNames = (evidence || []).map((item) => String(item.sender_name || "").trim());
    const genericNames = new Set(["我", "你", "他", "她", "它", "我们", "你们", "他们", "大家", "自己", "群成员", "待识别成员"]);
    const names = Array.from(new Set(eventNames.concat(evidenceNames).filter((name) => name && !genericNames.has(name)))).sort((a, b) => b.length - a.length);
    const enrichFacts = (text) => String(text || "").split(/(\d+(?:\.\d+)?\s*(?:%|元|万|小时|分钟|天|次|条|人|美元|点|号|月|日|度)|\d{4}[/-]\d{1,2}(?:[/-]\d{1,2})?|今天|明天|昨日|本周|下周|截止|已经确认|已经决定|决定|确认|改为|调整为|重置|封号|上线)/g).map((part) => /^(?:\d+(?:\.\d+)?\s*(?:%|元|万|小时|分钟|天|次|条|人|美元|点|号|月|日|度)|\d{4}[/-]|今天|明天|昨日|本周|下周|截止|已经确认|已经决定|决定|确认|改为|调整为|重置|封号|上线)/.test(part) ? '<span class="key-fact">' + escapeHtml(part) + '</span>' : escapeHtml(part)).join("");
    if (!names.length) return enrichFacts(value);
    const escaped = names.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const matcher = new RegExp("(" + escaped.join("|") + ")", "g");
    return String(value || "").split(matcher).map((part) => names.includes(part) ? '<span class="actor-pill">' + escapeHtml(part) + "</span>" : enrichFacts(part)).join("");
  }

  function editorialArticle(event) {
    const clean = (value) => String(value || "").replace(/^(?:事实综述|编辑判断|边界|核心结论)[:：]?\s*/, "").replace(/值得注意的是|综上所述|具有重要意义/g, "").replace(/用户需要评估|建议用户关注|需要进一步关注/g, "后续重点在于").trim();
    const narrative = clean(event && (event.narrative || event.summary));
    if (narrative.length >= 140) return narrative.slice(0, 460);
    const additions = [event && event.what_changed, event && event.why_it_matters, event && event.uncertainty].map(clean).filter(Boolean);
    const parts = [];
    [narrative].concat(additions).forEach((part) => {
      if (!part) return;
      if (parts.some((current) => current.includes(part) || part.includes(current))) return;
      parts.push(part);
    });
    return parts.join(parts.length > 1 ? " " : "").slice(0, 460) || "现有证据尚不足以写成完整稿件。";
  }

  function explicitGroupNames(value, evidence) {
    const groupNames = Array.from(new Set((evidence || []).map((item) => String(item.chat_name || "").trim()).filter((name) => name && name !== "群聊" && !/@chatroom$/i.test(name))));
    if (groupNames.length !== 1) return String(value || "");
    const namedPlace = groupNames[0] + "中";
    return String(value || "").replace(/(?:这个|该)?群里/g, namedPlace).replace(/(?:这个|该)?群内/g, namedPlace);
  }

  function editorialConclusion(event) {
    return String(event && (event.core_conclusion || event.why_it_matters) || "")
      .replace(/^对用户来说[，,]?\s*/, "")
      .replace(/用户需要评估|建议用户关注|需要进一步关注/g, "后续重点在于")
      .replace(/^需要关注/, "关键在于")
      .trim();
  }

  function eventCardHtml(event, index) {
    const key = event._key || eventKey(event);
    const evidence = evidenceFor(event);
    const tags = Array.from(new Set((event._lane === "personal" ? ["与你相关"] : []).concat(stringList(event.tags)))).slice(0, 4);
    const feedback = feedbackFor(key);
    const narrative = explicitGroupNames(editorialArticle(event), evidence);
    const conclusion = explicitGroupNames(editorialConclusion(event), evidence);
    const meta = [event.confidence != null ? "可信 " + event.confidence : "", evidence.length ? evidence.length + " 条证据" : "", event.related_chat_count ? event.related_chat_count + " 个会话" : ""].filter(Boolean).join(" · ");
    const menu = '<div class="event-menu" data-event-menu="' + escapeHtml(key) + '" hidden><button type="button" data-feedback="valuable" data-event-key="' + escapeHtml(key) + '" class="' + (feedback === "valuable" ? "selected" : "") + '">有价值</button><button type="button" data-feedback="not_valuable" data-event-key="' + escapeHtml(key) + '" class="' + (feedback === "not_valuable" ? "selected" : "") + '">无价值</button><button type="button" data-feedback="wrong_merge" data-event-key="' + escapeHtml(key) + '" class="' + (feedback === "wrong_merge" ? "selected" : "") + '">错误合并</button><button type="button" data-feedback="missing_context" data-event-key="' + escapeHtml(key) + '" class="' + (feedback === "missing_context" ? "selected" : "") + '">缺少上下文</button></div>';
    const evidenceDetails = evidence.length ? '<details class="brief-evidence"><summary>证据附录 · ' + evidence.length + ' 条</summary><div class="event-evidence">' + evidence.map(evidenceHtml).join("") + "</div></details>" : "";
    const conclusionHtml = conclusion ? ' <span class="brief-conclusion-inline">' + richActorText(conclusion, evidence, event) + '</span>' : "";
    const importance = Number(event.importance || 0);
    const weightClass = importance >= 80 ? " is-major" : (importance >= 60 ? " is-feature" : " is-brief");
    return '<article class="event-card brief-card article-card' + (event.is_ai_brief ? " is-editorial" : " is-candidate") + weightClass + '" data-event-card="' + escapeHtml(key) + '"><div class="brief-card-top"><span class="brief-index">' + String(event._display_index || index + 1).padStart(2, "0") + '</span><span class="brief-meta">' + escapeHtml(meta) + '</span><button class="event-more" type="button" data-event-more="' + escapeHtml(key) + '" aria-label="评价简报">⋯</button>' + menu + '</div><h3>' + escapeHtml(editorialTitle(event)) + '</h3><p class="brief-narrative article-copy">' + richActorText(narrative, evidence, event) + conclusionHtml + '</p>' + (tags.length ? '<div class="event-tags">' + tags.map((tag) => '<span class="event-tag">' + escapeHtml(tag) + "</span>").join("") + "</div>" : "") + evidenceDetails + "</article>";
  }

  function unformedDynamicHtml(item, index) {
    const evidence = Array.isArray(item && item.evidence) ? item.evidence : [];
    const kindLabels = { small_matter: "小事", greeting: "寒暄", question: "问题", fragment: "碎片", note: "零散讨论", media: "媒体" };
    const people = stringList(item && item.people);
    const actor = people.length ? people.slice(0, 3).join("、") + (people.length > 3 ? "等" + people.length + "人" : "") : "相关成员";
    const target = evidence.length && evidence[0].message_id ? ' data-open-message="' + escapeHtml(evidence[0].message_id) + '"' : "";
    const details = evidence.length ? '<details class="dynamic-evidence"><summary>查看 ' + evidence.length + ' 条原文</summary><div class="event-evidence">' + evidence.map(evidenceHtml).join("") + '</div></details>' : "";
    return '<article class="unformed-entry small-matter"' + target + '><div class="unformed-main"><div class="unformed-meta"><b>' + escapeHtml(kindLabels[String(item && item.kind || "small_matter")] || "小事") + '</b><span>' + escapeHtml(actor) + '</span><time>' + escapeHtml(formatTime(item && item.end, true)) + '</time></div><p>' + richActorText(String(item && item.summary || "未取得可读内容"), evidence, item) + '</p>' + details + '</div></article>';
  }

  function renderUnformedDynamics(insights, formedEvents) {
    const formedIds = new Set((formedEvents || []).flatMap((event) => evidenceFor(event).map((entry) => String(entry.message_id || ""))).filter(Boolean));
    const sourceItems = Array.isArray(insights && insights.unformed_dynamics) ? insights.unformed_dynamics : [];
    // If every message represented by a small dynamic already appears as
    // evidence in an editorial brief, do not print it a second time.  Partial
    // clusters remain visible so the full census never silently drops context.
    const items = sourceItems.filter((item) => {
      const ids = stringList(item && item.message_ids).filter(Boolean);
      return !ids.length || !ids.every((id) => formedIds.has(id));
    });
    const section = $("#unformed-section");
    const list = $("#overview-unformed");
    if (!section || !list) return;
    section.hidden = items.length === 0;
    $("#unformed-count").textContent = formatNumber(items.length) + " 件";
    const batchSize = isCensusRange() ? 120 : 40;
    const visibleCount = Math.max(batchSize, Number(state.unformedVisibleCount || 0));
    const visible = items.slice(0, visibleCount);
    list.innerHTML = items.length ? visible.map(unformedDynamicHtml).join("") : '<div class="empty-state">今天没有需要补充的小事。</div>';
    const more = $("#unformed-more");
    if (more) {
      more.hidden = visible.length >= items.length;
      more.textContent = more.hidden ? "" : "继续加载（剩余 " + formatNumber(items.length - visible.length) + " 条）";
    }
  }

  function renderEventList(selector, items, emptyText) {
    const node = $(selector);
    if (!node) return;
    node.innerHTML = items.length ? items.map(eventCardHtml).join("") : '<div class="empty-state">' + escapeHtml(emptyText) + "</div>";
  }

  function renderOverview() {
    const insights = state.insights || fallbackInsights();
    const summary = insights.summary || {};
    const allEvents = collectEvents(insights);
    const events = allEvents.map((event, index) => Object.assign({}, event, { _display_index: index + 1 }));
    const editorial = events.filter((event) => event.is_ai_brief).sort((left, right) => {
      const score = (event) => Number(event.importance || 0) + Number(event.confidence || 0) * .15 + Math.min(6, evidenceFor(event).length) * 3 + (event._lane === "trending" ? 6 : 0) + (stringList(event.tags).some((tag) => /风险|合规|event|risk/i.test(tag)) ? 8 : 0);
      return score(right) - score(left);
    });
    const lead = editorial.find((event) => Number(event.importance || 0) >= 78 && Number(event.confidence || 0) >= 65 && evidenceFor(event).length >= 2)
      || events.find((event) => Number(event.importance || 0) >= 82 && evidenceFor(event).length >= 2)
      || null;
    const remaining = lead ? events.filter((event) => event._key !== lead._key) : events;
    const personal = remaining.filter((event) => event._lane === "personal");
    const topics = remaining.filter((event) => event.is_ai_brief ? Number(event.importance || 0) >= 60 : Number(event.importance || 0) >= 70);
    const trending = topics;
    const pending = remaining.filter((event) => event._lane === "pending");
    const reportPage = $("#overview");
    const activeCount = topics.length + (lead ? 1 : 0);
    const layout = lead ? "lead" : (activeCount === 0 ? "census" : "single");
    if (reportPage) {
      reportPage.dataset.editorialLayout = layout;
      const selectedTheme = state.settings.display.report_theme || "auto";
      const tagText = events.flatMap((event) => stringList(event.tags)).join(" ");
      const resolvedTheme = selectedTheme !== "auto" ? selectedTheme : (/风险|合规|安全|预警/i.test(tagText) ? "classic" : (/AI|模型|开发|工具|agent/i.test(tagText) ? "cobalt" : (personal.length > trending.length ? "forest" : "classic")));
      reportPage.dataset.theme = resolvedTheme;
    }
    if ($("#overview-layout-label")) $("#overview-layout-label").textContent = ({ census: "会话普查版", single: "单栏简讯版", split: "双栏要闻版", lead: "主线版", frontpage: "头版版" })[layout];
    $("#metric-messages").textContent = formatNumber(summary.messages == null ? state.messages.length : summary.messages);
    $("#metric-chats").textContent = formatNumber(summary.chats == null ? state.chats.length : summary.chats);
    $("#metric-high").textContent = formatNumber(events.filter((event) => event._lane !== "pending").length);
    $("#metric-actions").textContent = formatNumber(pending.length + Number(summary.actions || 0));
    const voiceTotal = Number(summary.voice_total == null ? state.messages.filter((item) => item.message_type === "voice").length : summary.voice_total);
    const voiceDone = Number(summary.voice_transcribed == null ? state.messages.filter((item) => item.message_type === "voice" && item.voice_transcript).length : summary.voice_transcribed);
    if ($("#metric-voice")) $("#metric-voice").textContent = voiceTotal ? formatNumber(voiceDone) + "/" + formatNumber(voiceTotal) : "—";
    $("#for-me-count").textContent = formatNumber(personal.length + (lead && lead._lane === "personal" ? 1 : 0)); $("#trending-count").textContent = formatNumber(topics.length + (lead ? 1 : 0)); $("#pending-count").textContent = formatNumber(pending.length) + " 条";
    const leadSection = $("#lead-story"); if (leadSection) leadSection.hidden = !lead;
    renderEventList("#overview-lead", lead ? [lead] : [], "暂无头版主线。");
    const pendingSection = $("#overview-pending").closest(".pending-section"); if (pendingSection) pendingSection.hidden = true;
    const visibleLimit = isCensusRange() ? 80 : 8;
    renderEventList("#overview-for-me", [], "");
    renderEventList("#overview-trending", topics.slice(0, visibleLimit), "当前没有形成值得展开的正式话题。");
    renderEventList("#overview-pending", pending.slice(0, 8), "暂无待核实事件。");
    renderUnformedDynamics(insights, events);
    renderConversationCensus();
    const personalPanel = $(".lane-personal"); const trendingPanel = $(".lane-trending"); const laneGrid = $(".lane-grid");
    if (personalPanel) personalPanel.hidden = true;
    if (trendingPanel) trendingPanel.hidden = topics.length === 0;
    if (laneGrid) laneGrid.hidden = topics.length === 0;
  }

  function renderConversationCensus() {
    const node = $("#overview-census"); if (!node) return;
    const byChat = new Map();
    state.messages.forEach((message) => {
      const id = String(message.chat_id || message.chat_name || ""); if (!id) return;
      const row = byChat.get(id) || { voice: 0, media: 0 };
      if (message.message_type === "voice") row.voice += 1;
      if (!["text", "system", "other"].includes(message.message_type)) row.media += 1;
      byChat.set(id, row);
    });
    const values = state.chats.slice(0, isCensusRange() ? 120 : 30);
    node.innerHTML = values.length ? values.map((chat, index) => {
      const mix = byChat.get(String(chat.chat_id)) || { voice: 0, media: 0 };
      const preview = String(chat.latest_text || "").replace(/^\[[^\]]+\]$/, "媒体消息");
      return '<button class="census-entry" data-select-chat="' + escapeHtml(chat.chat_id) + '" type="button"><span class="census-number">' + String(index + 1).padStart(2, "0") + '</span><span class="census-copy"><strong>' + escapeHtml(chat.chat_name) + '</strong><small>' + escapeHtml(preview || "仅记录到媒体或系统消息") + '</small></span><span class="census-stats"><b>' + formatNumber(chat.messages) + ' 条</b><small>' + (chat.is_group ? "群聊" : "私聊") + (mix.voice ? " · 语音 " + formatNumber(mix.voice) : "") + (mix.media && !mix.voice ? " · 媒体 " + formatNumber(mix.media) : "") + '</small><time>' + escapeHtml(formatTime(chat.last_at, true)) + '</time></span></button>';
    }).join("") : '<div class="empty-state">当前日期尚未同步到任何会话。</div>';
  }

  function filteredFeedMessages() {
    const high = highlightIds(); const query = state.feedSearch.trim().toLowerCase();
    return newestFirst(state.messages).filter((item) => {
      if (state.feedFilter === "inbound" && item.is_self === true) return false;
      if (state.feedFilter === "high" && !high.has(String(item.message_id))) return false;
      if (state.feedFilter === "media" && !isMedia(item)) return false;
      if (state.feedChatId !== "all" && String(item.chat_id) !== String(state.feedChatId)) return false;
      const haystack = [messageText(item), chatDisplay(item), senderDisplay(item), item.media_name, item.media_path].join(" ").toLowerCase();
      return !query || haystack.includes(query);
    });
  }

  function renderFeedChatOptions() {
    const select = $("#feed-chat-filter"); const current = state.feedChatId;
    select.innerHTML = '<option value="all">全部会话</option>' + state.chats.map((chat) => '<option value="' + escapeHtml(chat.chat_id) + '">' + escapeHtml(chat.chat_name) + "</option>").join("");
    select.value = state.chats.some((chat) => String(chat.chat_id) === String(current)) ? current : "all"; state.feedChatId = select.value;
  }

  function feedItemHtml(item, high) {
    const media = isMedia(item); const sender = senderDisplay(item); const chat = chatDisplay(item); const type = TYPE_LABELS[item.message_type] || "其他";
    return `<article class="feed-item${high ? " is-highlight" : ""}" data-open-message="${escapeHtml(item.message_id)}"><div class="avatar ${item.is_self ? "self" : ""}">${escapeHtml(avatarText(sender))}</div><div class="feed-item-main"><div class="feed-item-top"><strong>${escapeHtml(sender)}</strong><span class="chat-label">${escapeHtml(chat)}</span><span class="type-tag${media ? " media-tag" : ""}">${escapeHtml(type)}</span>${high ? '<span class="highlight-tag">重点</span>' : ""}</div>${media ? mediaHtml(item) : `<p class="feed-content">${escapeHtml(messageText(item))}</p>`}<div class="feed-item-foot">${item.is_self ? "我发" : "收到"}</div></div><time class="feed-time">${escapeHtml(formatTime(timestampOf(item), true))}</time></article>`;
  }

  function renderFeed() {
    renderFeedChatOptions(); const all = filteredFeedMessages(); const visible = all.slice(0, state.feedVisibleCount);
    $("#feed-meta").textContent = formatNumber(all.length) + " 条" + (state.feedFilter !== "all" || state.feedSearch || state.feedChatId !== "all" ? " · 已筛选" : ""); $("#feed-more").hidden = visible.length >= all.length;
    const high = highlightIds(); let day = ""; const chunks = [];
    visible.forEach((item) => { const currentDay = beijingDateInput(timestampOf(item)); if (currentDay !== day) { day = currentDay; chunks.push('<div class="feed-day">' + escapeHtml(formatDateLabel(currentDay)) + "</div>"); } chunks.push(feedItemHtml(item, high.has(String(item.message_id)))); });
    $("#feed-list").innerHTML = chunks.length ? chunks.join("") : '<div class="empty-state">当前筛选没有消息。</div>'; bindMediaControls();
    const summary = state.insights && state.insights.summary || {}; const quality = state.insights && state.insights.quality || {};
    $("#feed-side-stats").innerHTML = [["消息", summary.messages == null ? state.messages.length : summary.messages], ["收到", summary.inbound == null ? state.messages.filter((item) => !item.is_self).length : summary.inbound], ["媒体", state.messages.filter(isMedia).length], ["重点", (state.insights && state.insights.highlights || []).length], ["文本覆盖", quality.analysis_coverage == null ? "—" : Math.round(Number(quality.analysis_coverage) * 100) + "%"]].map((item) => '<div class="side-stat"><span>' + escapeHtml(item[0]) + '</span><strong>' + escapeHtml(typeof item[1] === "string" ? item[1] : formatNumber(item[1])) + "</strong></div>").join("");
  }

  function chatForId(id) { return state.chats.find((chat) => String(chat.chat_id) === String(id)) || null; }

  function renderChatList() {
    const query = state.chatSearch.trim().toLowerCase(); const list = state.chats.filter((chat) => !query || chat.chat_name.toLowerCase().includes(query));
    $("#chat-list-count").textContent = formatNumber(list.length);
    $("#chat-list").innerHTML = list.length ? list.map((chat) => { const selected = String(chat.chat_id) === String(state.selectedChatId); return '<article class="chat-list-item' + (selected ? " selected" : "") + '" data-select-chat="' + escapeHtml(chat.chat_id) + '"><div class="avatar chat-avatar">' + escapeHtml(avatarText(chat.chat_name)) + '</div><div class="chat-list-main"><div class="chat-list-row"><strong>' + escapeHtml(chat.chat_name) + '</strong><time>' + escapeHtml(formatTime(chat.last_at, false)) + '</time></div><p>' + escapeHtml(chat.latest_text || "暂无文本预览") + '</p><div class="chat-list-meta"><span>' + (chat.is_group ? "群聊" : "联系人") + " · " + formatNumber(chat.messages) + " 条</span>" + (chat.unread ? '<b class="unread-badge">' + formatNumber(chat.unread) + "</b>" : "") + "</div></div></article>"; }).join("") : '<div class="empty-state">没有匹配的会话。</div>';
  }

  function chatMessageHtml(item) {
    const self = item.is_self === true; const sender = senderDisplay(item); const content = isMedia(item) ? mediaHtml(item) : '<p class="chat-bubble-text">' + escapeHtml(messageText(item)) + "</p>";
    return '<article class="chat-message-row ' + (self ? "is-self" : "is-inbound") + '" data-open-message="' + escapeHtml(item.message_id) + '"><div class="avatar chat-avatar">' + escapeHtml(avatarText(sender)) + '</div><div class="chat-bubble-wrap">' + (item.is_group && !self ? '<span class="chat-sender-name">' + escapeHtml(sender) + "</span>" : "") + '<div class="chat-bubble' + (isMedia(item) ? " media-bubble" : "") + '">' + content + '</div><time class="chat-message-time">' + escapeHtml(formatTime(timestampOf(item), false)) + "</time></div></article>";
  }

  function renderChatDetail() {
    const chat = chatForId(state.selectedChatId);
    if (!chat) { $("#chat-detail-head").innerHTML = '<div class="empty-detail"><span class="empty-icon">□</span><strong>选择一个会话</strong></div>'; $("#chat-message-list").innerHTML = '<div class="empty-state">选择会话后显示消息。</div>'; $("#chat-context-pane").innerHTML = '<div class="empty-detail"><span class="empty-icon">i</span><strong>会话信息</strong></div>'; return; }
    const allMessages = oldestFirst(state.messages.filter((item) => String(item.chat_id) === String(chat.chat_id))); const max = 260; const visible = allMessages.slice(-max); const omitted = Math.max(0, allMessages.length - visible.length); const label = chat.chat_name;
    $("#chat-detail-head").innerHTML = '<div class="chat-head-avatar avatar">' + escapeHtml(avatarText(label)) + '</div><div><h3>' + escapeHtml(label) + '</h3><p>' + (chat.is_group ? "群聊" : "联系人") + " · " + formatNumber(allMessages.length) + " 条 · " + escapeHtml(formatTime(chat.last_at, true)) + '</p></div><span class="readonly-tag">只读</span>';
    let day = ""; const chunks = []; if (omitted) chunks.push('<div class="chat-history-note">前面还有 ' + formatNumber(omitted) + " 条历史</div>");
    visible.forEach((item) => { const currentDay = beijingDateInput(timestampOf(item)); if (currentDay !== day) { day = currentDay; chunks.push('<div class="chat-day-divider">' + escapeHtml(formatDateLabel(currentDay)) + "</div>"); } chunks.push(chatMessageHtml(item)); });
    $("#chat-message-list").innerHTML = chunks.length ? chunks.join("") : '<div class="empty-state">该会话没有消息。</div>'; bindMediaControls();
    const high = highlightIds(); const media = allMessages.filter(isMedia).length; const focus = allMessages.filter((item) => high.has(String(item.message_id))).length;
    $("#chat-context-pane").innerHTML = '<div class="context-title"><span class="eyebrow">CHAT</span><h3>' + escapeHtml(label) + '</h3></div><div class="context-stat-grid"><div><strong>' + formatNumber(allMessages.length) + '</strong><span>消息</span></div><div><strong>' + formatNumber(allMessages.filter((item) => !item.is_self).length) + '</strong><span>收到</span></div><div><strong>' + formatNumber(media) + '</strong><span>媒体</span></div><div><strong>' + formatNumber(focus) + '</strong><span>重点</span></div></div><div class="context-last"><span>最近消息</span><p>' + escapeHtml(allMessages.length ? messageText(allMessages[allMessages.length - 1]) : "暂无") + '</p></div>';
    window.requestAnimationFrame(() => { const panel = $("#chat-message-list"); if (panel) panel.scrollTop = panel.scrollHeight; });
  }

  function renderChats() { $("#chat-meta").textContent = formatNumber(state.chats.length) + " 个会话"; if (!state.selectedChatId || !chatForId(state.selectedChatId)) state.selectedChatId = state.chats[0] && state.chats[0].chat_id; renderChatList(); renderChatDetail(); }

  function renderContacts() {
    const query = state.contactSearch.trim().toLowerCase(); const list = state.contacts.filter((contact) => { if (state.contactFilter !== "all" && contact.type !== state.contactFilter) return false; return !query || [contact.name, contact.latest_text].join(" ").toLowerCase().includes(query); });
    $("#contact-meta").textContent = formatNumber(list.length) + " 个对象";
    $("#contact-grid").innerHTML = list.length ? list.map((contact) => '<article class="contact-card panel" data-select-chat="' + escapeHtml(contact.contact_id) + '"><div class="contact-card-head"><div class="avatar contact-avatar">' + escapeHtml(avatarText(contact.name)) + '</div><span class="contact-type">' + (contact.type === "group" ? "群聊" : "联系人") + '</span></div><h3>' + escapeHtml(contact.name) + '</h3><p class="contact-preview">' + escapeHtml(contact.latest_text || "当前窗口暂无文本预览") + '</p><div class="contact-meta"><span>' + formatNumber(contact.messages) + " 条消息</span><span>最近 " + escapeHtml(formatTime(contact.last_at, true)) + '</span></div>' + (contact.high_signal ? '<div class="contact-highlight">' + formatNumber(contact.high_signal) + " 条重点</div>" : "") + "</article>").join("") : '<div class="empty-state">没有匹配的联系人或群聊。</div>';
  }

  function renderTasks() {
    let list = state.tasks.slice(); if (state.taskFilter === "待确认" || state.taskFilter === "待核对") list = list.filter((item) => item.status === "待确认" || item.status === "待核对"); if (state.taskFilter === "high") list = list.filter((item) => item.is_high);
    $("#task-meta").textContent = formatNumber(list.length) + " 条"; $("#task-summary-count").textContent = formatNumber(state.tasks.length);
    $("#task-list").innerHTML = list.length ? list.map((item, index) => '<article class="task-row' + (item.is_high ? " is-high" : "") + '"' + (item.message_id ? ' data-open-message="' + escapeHtml(item.message_id) + '"' : "") + '><span class="task-index">' + String(index + 1).padStart(2, "0") + '</span><div class="task-main"><div class="task-row-top"><strong>' + escapeHtml(firstUseful([item.sender_name, item.chat_name], "联系人")) + '</strong><div><span class="task-status">' + escapeHtml(item.status || "待确认") + '</span><span class="task-type">' + escapeHtml(item.candidate_type === "risk" ? "风险" : "行动") + '</span></div></div><p>' + escapeHtml(item.content || messageText(item)) + '</p><div class="task-row-foot"><span>' + escapeHtml((item.tags || []).join(" · ") || "需要核对") + '</span><time>' + escapeHtml(formatTime(item.timestamp, true)) + '</time></div></div></article>').join("") : '<div class="empty-state">当前没有待处理事项。</div>';
  }

  function renderAnalysis() {
    const insights = state.insights || fallbackInsights(); const summary = insights.summary || {}; const events = collectEvents(insights); const quality = insights.quality || {};
    $("#analysis-meta").textContent = insights.method && insights.method.label || "本地规则";
    const metrics = [["消息", summary.messages], ["有内容", summary.substantive], ["事件", events.length], ["跨会话", summary.trending], ["待核实", summary.pending_review], ["媒体", summary.media]];
    $("#analysis-summary").innerHTML = metrics.map((item) => '<article class="analysis-metric"><strong>' + formatNumber(item[1] == null ? 0 : item[1]) + '</strong><span>' + escapeHtml(item[0]) + "</span></article>").join("");
    const situation = insights.situation || {}; const points = Array.isArray(situation.points) ? situation.points.filter(Boolean).slice(0, 4) : [];
    $("#analysis-situation").innerHTML = '<strong>' + escapeHtml(situation.headline || insights.narrative || "当前范围还没有形成可靠主线") + '</strong>' + (points.length ? "<ul>" + points.map((point) => "<li>" + escapeHtml(point) + "</li>").join("") + "</ul>" : "");
    renderEventList("#analysis-event-list", events.slice(0, 10), "当前范围没有事件候选。");
    renderTopicBriefs(insights.topic_briefs || []); renderHourChart(insights.hourly || []);
    const topChats = (insights.top_chats || []).slice(0, 8); $("#top-chat-list").innerHTML = topChats.length ? topChats.map((item) => '<div class="top-chat-row"><span>' + escapeHtml(firstUseful([item.chat_name, item.name], item.is_group ? "群聊" : "会话")) + '</span><strong>' + formatNumber(item.messages || 0) + "</strong></div>").join("") : '<div class="empty-state">暂无会话分布。</div>';
    if (quality.limitation) $("#analysis-situation").setAttribute("data-quality", quality.limitation);
  }

  function renderTopicBriefs(items) {
    const list = (items || []).filter((item) => item && item.topic && item.topic !== "其他讨论").slice(0, 10); const max = Math.max(1, ...list.map((item) => Number(item.message_count || 0)));
    $("#topic-brief-list").innerHTML = list.length ? list.map((item) => { const width = Math.max(7, Math.round(Number(item.message_count || 0) / max * 100)); return '<article class="topic-brief-item"><div class="topic-brief-top"><strong>' + escapeHtml(item.topic) + '</strong><span>' + formatNumber(item.message_count || 0) + ' 条</span></div><div class="topic-brief-track"><i style="width:' + width + '%"></i></div><p>' + escapeHtml(item.summary || "") + '</p><div class="topic-brief-meta"><span>' + formatNumber(item.chat_count || 0) + ' 个会话</span><span>' + formatNumber(item.high_information_count || 0) + ' 条高信息量</span></div></article>'; }).join("") : '<div class="empty-state">暂无主题。</div>';
  }

  function renderHourChart(items) {
    const list = Array.isArray(items) ? items : []; const max = Math.max(1, ...list.map((item) => Number(item.count || 0)));
    $("#hour-chart").innerHTML = list.length ? list.map((item) => { const height = Math.max(2, Math.round(Number(item.count || 0) / max * 82)); const active = Number(item.count || 0) === max && Number(item.count || 0) > 0 ? " active" : ""; return '<div class="hour-bar' + active + '" style="height:' + height + 'px" title="' + escapeHtml(String(item.hour).padStart(2, "0") + ":00 · " + item.count + " 条") + '"><span>' + (Number(item.hour) % 3 === 0 ? String(item.hour).padStart(2, "0") : "") + "</span></div>"; }).join("") : '<div class="muted">暂无活跃时段。</div>';
    const high = list.reduce((best, item) => Number(item.count || 0) > Number(best.count || 0) ? item : best, { hour: 0, count: 0 }); $("#hour-note").textContent = Number(high.count || 0) ? String(high.hour).padStart(2, "0") + ":00 附近消息最多" : "暂无数据";
  }

  function renderCounts() {
    const summary = state.insights && state.insights.summary || {};
    $("#nav-overview-count").textContent = formatNumber(summary.messages == null ? state.messages.length : summary.messages); $("#nav-feed-count").textContent = formatNumber(state.messages.length); $("#nav-chat-count").textContent = formatNumber(state.chats.length); $("#nav-workbench-count").textContent = formatNumber(state.tasks.length + collectEvents(state.insights || {}).length);
  }

  function renderAll() {
    state.chats = buildChats(state.chatPayload); state.contacts = normalizeContacts(state.contactPayload); state.tasks = buildTasks(state.taskPayload);
    renderRangeContext(); renderOverview(); renderCounts();
    if (state.view === "feed") renderFeed();
    else if (state.view === "chats") renderChats();
    else if (state.view === "workbench") { renderTasks(); renderAnalysis(); }
  }

  function snapshotMessageKeys(items) { return new Set(items.map((item) => String(item.message_id))); }
  function snapshotSignature(payload) {
    const messages = payload.messages || [];
    const tail = messages.slice(-8).map((item) => [item.message_id, item.voice_status, item.voice_transcript]).flat();
    const insights = normalizeInsights(payload.insights) || {};
    const events = collectEvents(insights).map((item) => [item._key, item.title, item.summary, item.confidence]).flat();
    return JSON.stringify([messages.length, tail, events, (payload.tasks || []).length]);
  }
  function applyDataSnapshot(payload) { state.messages = payload.messages; state.insights = normalizeInsights(payload.insights); state.chatPayload = payload.chats; state.contactPayload = payload.contacts; state.taskPayload = payload.tasks; state.dataSignature = snapshotSignature(payload); state.pendingSnapshot = null; state.pendingNewCount = 0; state.initialized = true; $("#new-messages-banner").hidden = true; renderAll(); }
  function stageNewSnapshot(payload, previousKeys) { const added = payload.messages.filter((item) => !previousKeys.has(String(item.message_id))); if (!added.length) return; state.pendingSnapshot = payload; state.pendingNewCount = added.length; $("#new-messages-count").textContent = formatNumber(added.length); $("#new-messages-banner").hidden = false; }

  async function refresh(options) {
    const settings = Object.assign({ forceApply: false, showLoading: false }, options || {});
    if (state.loading || !state.start || !state.end || state.end < state.start) return;
    state.loading = true;
    if (settings.showLoading) $("#feed-meta").textContent = "正在读取 " + rangeLabel();
    try {
      const query = queryString({ limit: 50000 });
      const [status, sync, messagesValue, insightsValue, chatsValue, contactsValue, tasksValue, aiStatus, aiLatest] = await Promise.all([
        requestFirst(["/api/status", "/api/health"]), requestFirst(["/api/sync-status", "/api/status"], null), requestFirst(["/api/messages" + query, "/api/messages/recent" + query]), requestFirst(["/api/overview" + query, "/api/insights" + query, "/api/analysis" + query], null), requestFirst(["/api/chats" + query, "/api/conversations" + query], null), requestFirst(["/api/contacts" + query], null), requestFirst(["/api/tasks?limit=500", "/api/actions?limit=500"], null), requestFirst(["/api/ai-status", "/api/ai/status"], null), requestFirst(["/api/ai-latest" + query], null),
      ]);
      renderStatus(status, sync && sync.state ? sync : status.sync); renderAiStatus(aiStatus || {});
      if (aiLatest && aiLatest.ok && aiResultMatchesRange(aiLatest)) { state.aiResult = aiLatest; state.lastAiRun = Date.parse(aiLatest.generated_at || "") || Date.now(); state.lastAiMessageCount = normalizeMessages(messagesValue).length; renderAiResult(aiLatest); renderAiFreshness(aiLatest); }
      else { state.aiResult = null; state.lastAiRun = 0; state.lastAiMessageCount = 0; renderAiFreshness(null); }
      const payload = { messages: normalizeMessages(messagesValue), insights: insightsValue, chats: chatsValue, contacts: contactsValue, tasks: tasksValue }; const previousKeys = snapshotMessageKeys(state.messages);
      const changed = snapshotSignature(payload) !== state.dataSignature;
      if (!state.initialized || settings.forceApply || (!state.paused && changed)) applyDataSnapshot(payload); else if (state.paused && changed) stageNewSnapshot(payload, previousKeys);
      maybeRunAiAnalysis(changed || !state.lastAiRun);
      const lastUpdated = $("#last-updated");
      if (lastUpdated) lastUpdated.textContent = "刷新 " + new Date().toLocaleTimeString("zh-CN");
      const syncState = sync && sync.state || status.sync && status.sync.state;
      if (syncState === "running") showNotice("同步中", false); else if (syncState === "failed") showNotice("历史同步失败：" + ((sync && sync.error) || "未知错误"), true); else if (!state.pendingSnapshot) showNotice("");
    } catch (error) { const unreachable = /failed to fetch|networkerror|network request|http 50[234]/i.test(String(error && error.message || error)); showNotice((unreachable ? "服务不可达：" : "界面处理失败：") + error.message, true); if (unreachable) { $("#service-state").innerHTML = '<span class="status-dot status-dot-warn"></span><span>服务不可达</span>'; $("#service-state").classList.add("is-warn"); } }
    finally { state.loading = false; }
  }

  async function syncRange() {
    const button = $("#sync-button"); button.disabled = true; button.innerHTML = "↻ 同步中…";
    beginOperation("同步「" + rangeLabel() + "」", "扫描本地微信数据库", 8);
    try { const started = await request("/api/sync-range", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ start: state.start, end: state.end, scope: "all", limit: 200000 }) }); const result = await waitForSyncCompletion(started.job_id, 120000); updateOperation(null, "整理 " + formatNumber(result.seen || 0) + " 条消息与语音转写", 86); await refresh({ forceApply: true }); showNotice("已同步 " + formatNumber(result.seen || 0) + " 条，新增 " + formatNumber(result.inserted || 0) + " 条", false); await finishOperation("全会话普查完成", false); }
    catch (error) { showNotice("历史同步失败：" + error.message, true); await finishOperation(error.message, true); }
    finally { button.disabled = false; button.innerHTML = "↻ 同步"; }
  }

  async function waitForSyncCompletion(jobId, timeoutMs) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      const value = await request("/api/sync-status");
      if (jobId && value.job_id && value.job_id !== jobId) throw new Error("同步任务已被另一任务替换");
      if (value.state === "succeeded") return value;
      if (value.state === "failed") throw new Error(value.error || "微信本地数据库读取失败");
      const elapsed = Date.now() - startedAt;
      updateOperation(null, elapsed < 8000 ? "枚举全部会话" : "读取消息分片与媒体索引", Math.min(78, 14 + elapsed / timeoutMs * 64));
      await delay(650);
    }
    throw new Error("全会话同步超过 " + Math.round(timeoutMs / 1000) + " 秒");
  }

  async function refreshNow() {
    const button = $("#refresh-now");
    button.disabled = true;
    try { await syncRange(); }
    finally { button.disabled = false; }
  }

  function renderAiResult(value) {
    const box = $("#ai-analysis-result"); const analysis = value.analysis || {}; const themes = Array.isArray(analysis.themes) ? analysis.themes : []; const findings = Array.isArray(analysis.findings) ? analysis.findings : [];
    const findingHtml = findings.map((item) => { const evidence = (Array.isArray(item.evidence) ? item.evidence : []).slice(0, 4).map((source) => source && source.message_id ? '<button class="ai-evidence" data-open-message="' + escapeHtml(source.message_id) + '" type="button">' + escapeHtml(source.evidence_ref || source.sender_name || "证据") + "</button>" : "").join(""); return '<article class="ai-finding"><div class="ai-finding-top"><strong>' + escapeHtml(item.title || "分析事项") + '</strong><span>' + escapeHtml(String(item.category || "分析")) + " · " + escapeHtml(String(item.confidence == null ? "" : item.confidence)) + '</span></div><p class="ai-finding-summary">' + escapeHtml(item.summary || item.reason || "") + '</p>' + (item.what_changed ? '<div class="ai-finding-detail"><b>变化</b><span>' + escapeHtml(item.what_changed) + "</span></div>" : "") + (item.why_it_matters ? '<div class="ai-finding-detail"><b>意义</b><span>' + escapeHtml(item.why_it_matters) + "</span></div>" : "") + (item.next_step ? '<div class="ai-finding-next">下一步：' + escapeHtml(item.next_step) + "</div>" : "") + (evidence ? '<div class="ai-finding-evidence">' + evidence + "</div>" : "") + "</article>"; }).join("");
    box.hidden = false; box.className = "ai-analysis-result"; box.innerHTML = '<div class="ai-result-head"><strong>' + escapeHtml(value.source === "ai_assisted_with_local_fallback" ? "AI + 本地结果" : "AI 辅助") + '</strong><span>' + escapeHtml(value.model || "模型") + " · " + formatNumber(value.candidate_count || 0) + " 条候选</span></div>" + (analysis.situation ? '<p class="ai-result-brief">' + escapeHtml(analysis.situation) + "</p>" : "") + (themes.length ? '<div class="ai-themes">' + themes.slice(0, 8).map((theme) => '<span class="ai-theme">' + escapeHtml(theme) + "</span>").join("") + "</div>" : "") + (findingHtml || '<p class="ai-result-brief">暂无新增判断。</p>');
  }

  async function runAiAnalysis(silent) {
    if (state.aiRunning || !state.aiStatus || !(state.aiStatus.configured || state.aiStatus.api_key_configured)) return; if (!silent && !window.confirm("AI 分析会发送脱敏候选文本，仅用于分析。继续吗？")) return;
    state.aiRunning = true;
    beginAiTaskStatus(Boolean(silent));
    if (!silent) beginOperation("AI 分析「" + rangeLabel() + "」", "准备脱敏候选与语音转写", 8);
    aiButtons().forEach((button) => { button.disabled = true; button.dataset.originalLabel = button.innerHTML; button.innerHTML = "<span>✦</span><b>分析中…</b>"; }); renderAiFreshness();
    try { if (!silent) updateOperation(null, "模型正在归纳主线", 36); updateAiTaskStatus(null, "模型正在归纳主线与跨会话联系", 42, "running"); const value = await request("/api/ai-analysis", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ start: state.start, end: state.end, limit: 120, confirm: true, force: !silent }), timeoutMs: 300000 }); state.aiResult = value; state.lastAiRun = Date.parse(value.generated_at || "") || Date.now(); state.lastAiMessageCount = state.messages.length; updateAiTaskStatus(null, "核对证据并重排日报版面", 92, "running"); if (!silent) updateOperation(null, "核对证据并编排版面", 91); renderAiResult(value); renderAiFreshness(value); renderOverview(); renderAnalysis(); finishAiTaskStatus("最新分析已写入当前日报", false); if (!silent) { showNotice("AI 分析已完成", false); await finishOperation("AI 简报已更新", false); } }
    catch (error) { $("#ai-analysis-result").hidden = false; $("#ai-analysis-result").className = "ai-analysis-result error"; $("#ai-analysis-result").textContent = error.message; showNotice("AI 分析未完成", true); finishAiTaskStatus(error.message, true); if (!silent) await finishOperation(error.message, true); }
    finally { state.aiRunning = false; const configured = Boolean(state.aiStatus && (state.aiStatus.configured || state.aiStatus.api_key_configured)); aiButtons().forEach((button) => { button.disabled = !configured; button.innerHTML = button.id === "ai-analysis-home-button" ? "<span>✦</span><b>AI 分析</b>" : "运行 AI 分析"; }); renderAiFreshness(); }
  }

  function maybeRunAiAnalysis(changed) {
    const config = state.settings.analysis || {};
    if (!changed || !state.messages.length || config.auto_enabled === false || !state.aiStatus || !(state.aiStatus.configured || state.aiStatus.api_key_configured)) return;
    const interval = Math.max(60000, Number(config.interval_ms || 600000));
    const threshold = Math.max(1, Number(config.message_threshold || 20));
    if (state.lastAiRun && Date.now() - state.lastAiRun < interval) return;
    if (state.lastAiRun && state.messages.length - state.lastAiMessageCount < threshold) return;
    runAiAnalysis(true);
  }

  function updateRefreshControls() {
    const enabled = !state.settings.refresh || state.settings.refresh.enabled !== false; const label = !enabled ? "开启" : (state.paused ? "恢复" : "暂停"); const button = $("#refresh-toggle-top"); button.textContent = label; button.title = enabled && !state.paused ? "暂停自动刷新" : "恢复自动刷新"; button.classList.toggle("is-paused", state.paused || !enabled);
  }

  function restartPoller() { if (state.pollTimer) window.clearInterval(state.pollTimer); state.pollTimer = null; const enabled = !state.settings.refresh || state.settings.refresh.enabled !== false; if (enabled) state.pollTimer = window.setInterval(() => { if (!state.paused && !state.rangeTransition) refresh({}); }, refreshIntervalMs()); }

  function toggleRefresh() {
    const enabled = !state.settings.refresh || state.settings.refresh.enabled !== false; if (!enabled) { openSettings(); return; }
    state.paused = !state.paused; updateRefreshControls();
    if (state.paused) showNotice("自动刷新已暂停", false); else if (state.pendingSnapshot) { applyDataSnapshot(state.pendingSnapshot); showNotice("已显示暂停期间的新消息", false); } else { showNotice("自动刷新已恢复", false); refresh({ forceApply: true }); }
  }

  function openMedia(messageId) {
    const item = messageById(messageId); const lightbox = $("#media-lightbox"); const image = $("#media-lightbox-image"); const stateNode = $("#media-lightbox-state"); if (!item || !lightbox || !image || !stateNode) return;
    const url = mediaUrl(item, "image"); $("#media-lightbox-title").textContent = chatDisplay(item) + " · 图片"; $("#media-lightbox-path").textContent = item.media_path || "未记录具体路径"; $("#media-lightbox-meta").textContent = formatTime(timestampOf(item), true) + " · 本地缓存"; image.hidden = false; stateNode.hidden = false; stateNode.className = "media-lightbox-state"; stateNode.textContent = "正在读取…"; lightbox.hidden = false; lightbox.setAttribute("aria-hidden", "false"); image.onload = () => { stateNode.hidden = true; }; image.onerror = async () => { image.hidden = true; stateNode.hidden = false; stateNode.className = "media-lightbox-state is-error"; try { const response = await fetch(url, { cache: "no-store" }); const value = await response.json(); stateNode.textContent = value.message || "图片当前无法解码；已保留路径。"; } catch (_error) { stateNode.textContent = "图片当前无法解码；已保留路径。"; } }; image.src = url + "&v=" + Date.now();
  }

  function closeMedia() { const lightbox = $("#media-lightbox"); const image = $("#media-lightbox-image"); if (!lightbox) return; lightbox.hidden = true; lightbox.setAttribute("aria-hidden", "true"); if (image) image.removeAttribute("src"); }

  function reportFilename() { return "wechat-intelligence-" + String(state.start || "report") + (state.end && state.end !== state.start ? "-to-" + state.end : ""); }

  async function buildReportDocument() {
    const page = $("#overview"); const metrics = $(".intelligence-dock");
    if (!page || !metrics) throw new Error("日报版面尚未就绪");
    const copies = [metrics.cloneNode(true), page.cloneNode(true)];
    copies.forEach((copy) => {
      copy.querySelectorAll(".event-more,.event-menu,.loading-state,.empty-state").forEach((node) => node.remove());
      copy.querySelectorAll("button").forEach((node) => { node.removeAttribute("type"); node.removeAttribute("data-select-chat"); });
      copy.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
    });
    const css = await Promise.all(["/assets/styles.css", "/assets/polish.css"].map((url) => fetch(url, { cache: "no-store" }).then((response) => { if (!response.ok) throw new Error("无法读取打印样式"); return response.text(); })));
    const theme = page.dataset.theme || "classic"; const layout = page.dataset.editorialLayout || "census";
    const exportCss = '.export-body{margin:0;padding:24px;background:#d9d4ca;color:#111}.export-sheet{max-width:1380px;margin:auto}.export-body .intelligence-dock{display:grid}.export-body .newspaper-page{max-width:none}.export-body .event-more,.export-body .event-menu{display:none!important}@media print{.export-body{padding:0;background:#fff}.export-body .intelligence-dock{display:grid!important;margin-bottom:10mm}.export-body .newspaper-page{display:block!important}}';
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + escapeHtml(reportFilename()) + '</title><style>' + css.join("\n") + exportCss + '</style></head><body class="export-body" data-font-size="' + escapeHtml(state.settings.display.font_size || "normal") + '" data-density="' + escapeHtml(state.settings.display.density || "compact") + '"><main class="export-sheet">' + copies[0].outerHTML + copies[1].outerHTML.replace('data-theme="' + theme + '"', 'data-theme="' + theme + '" data-editorial-layout="' + layout + '"') + '</main></body></html>';
  }

  async function reportBlob(format) {
    const html = await buildReportDocument();
    const response = await fetch("/api/report-render", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ html, format, filename: reportFilename() }) });
    if (!response.ok) { const value = await response.json().catch(() => ({})); throw new Error(value.message || "日报导出失败"); }
    return response.blob();
  }

  function downloadBlob(blob, filename) { const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename; document.body.appendChild(anchor); anchor.click(); anchor.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); }

  function tauriInvoke() {
    const api = window.__TAURI__;
    if (!api) return null;
    if (api.core && typeof api.core.invoke === "function") return api.core.invoke.bind(api.core);
    if (typeof api.invoke === "function") return api.invoke.bind(api);
    return null;
  }

  async function saveReportBlob(blob, format, filename) {
    const invoke = tauriInvoke();
    if (!invoke) {
      downloadBlob(blob, filename);
      return { mode: "browser" };
    }
    const bytes = Array.from(new Uint8Array(await blob.arrayBuffer()));
    const path = await invoke("save_export_file", { fileName: filename, format, bytes });
    if (!path) return { mode: "tauri", canceled: true };
    return { mode: "tauri", path: String(path) };
  }

  function openReportExport() { const dialog = $("#report-export-dialog"); if (!dialog) return; $("#report-export-range").textContent = rangeLabel() + " · " + formatNumber(state.messages.length) + " 条消息"; dialog.hidden = false; dialog.setAttribute("aria-hidden", "false"); }
  function closeReportExport() { const dialog = $("#report-export-dialog"); if (!dialog) return; dialog.hidden = true; dialog.setAttribute("aria-hidden", "true"); }

  async function downloadReport(format, button) {
    button.disabled = true; const original = button.textContent; button.textContent = "生成中…";
    try {
      const filename = reportFilename() + "." + format;
      const result = await saveReportBlob(await reportBlob(format), format, filename);
      if (result.canceled) showNotice("已取消保存", false);
      else if (result.path) showNotice(format.toUpperCase() + " 日报已保存到：" + result.path, false);
      else showNotice("浏览器已开始下载；文件通常在浏览器默认“下载”文件夹，可在下载列表中查看。", false);
    }
    catch (error) { showNotice("日报导出失败：" + error.message, true); }
    finally { button.disabled = false; button.textContent = original; }
  }

  async function emailReport() {
    const recipient = String($("#report-email-recipient").value || "").trim();
    const formats = [$("#report-format-html").checked ? "html" : "", $("#report-format-pdf").checked ? "pdf" : ""].filter(Boolean);
    if (!recipient || !formats.length) { showNotice("请填写收件人并选择附件格式", true); return; }
    if (!window.confirm("确认将当前日报发送给 " + recipient + "？")) return;
    const button = $("#report-email-send"); button.disabled = true; button.textContent = "发送中…";
    try {
      const html = await buildReportDocument();
      const result = await request("/api/report-email", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ html, filename: reportFilename(), recipient, formats, confirm: true }), timeoutMs: 180000 });
      closeReportExport(); showNotice("日报已发送至 " + result.recipient, false);
    } catch (error) { showNotice("邮件发送失败：" + error.message, true); }
    finally { button.disabled = false; button.textContent = "确认并发送邮件"; }
  }

  function openMessage(messageId) {
    if (!messageId) return;
    const targetId = String(messageId);
    state.focusMessageId = targetId;
    state.feedFilter = "all";
    state.feedSearch = "";
    state.feedChatId = "all";
    state.feedVisibleCount = Math.max(140, state.messages.length);
    if ($("#feed-search")) $("#feed-search").value = "";
    $$('[data-feed-filter]').forEach((button) => button.classList.toggle("active", button.dataset.feedFilter === "all"));
    setView("feed");
    window.setTimeout(() => {
      renderFeed();
      const target = Array.from(document.querySelectorAll('#feed-list [data-open-message]')).find((node) => node.dataset.openMessage === targetId);
      if (!target) {
        showNotice("原消息不在当前日期窗口，请切换日期后再定位", true);
        return;
      }
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("is-focused");
      window.setTimeout(() => target.classList.remove("is-focused"), 1800);
    }, 60);
  }

  function openChat(chatId) { state.selectedChatId = String(chatId); setView("chats"); renderChats(); }
  function closeEventMenus() { $$('[data-event-menu]').forEach((menu) => { menu.hidden = true; }); }

  function bindEvents() {
    $$(".range-button").forEach((button) => button.addEventListener("click", () => switchReportRange(button)));
    [$("#range-start"), $("#range-end")].forEach((input) => input.addEventListener("change", () => { state.period = "custom"; state.start = $("#range-start").value; state.end = $("#range-end").value; state.aiResult = null; state.lastAiRun = 0; state.lastAiMessageCount = 0; $$(".range-button").forEach((item) => item.classList.remove("active")); renderRangeContext(); refresh({ showLoading: true, forceApply: true }); }));
    $("#sync-button").addEventListener("click", syncRange); $("#refresh-now").addEventListener("click", refreshNow); $("#refresh-toggle-top").addEventListener("click", toggleRefresh); $("#sidebar-collapse").addEventListener("click", () => setSidebarHidden(true)); $("#sidebar-reveal").addEventListener("click", () => setSidebarHidden(false)); $("#quick-settings").addEventListener("click", openSettings); $("#settings-close").addEventListener("click", closeSettings); $("#settings-cancel").addEventListener("click", closeSettings); $("#settings-backdrop").addEventListener("click", closeSettings); $("#settings-form").addEventListener("submit", saveSettings); $("#media-lightbox-close").addEventListener("click", closeMedia); $("[data-close-media]").addEventListener("click", closeMedia); $("#report-export-button").addEventListener("click", openReportExport); $("#report-export-close").addEventListener("click", closeReportExport); $("[data-close-export]").addEventListener("click", closeReportExport); $("#report-download-html").addEventListener("click", (event) => downloadReport("html", event.currentTarget)); $("#report-download-pdf").addEventListener("click", (event) => downloadReport("pdf", event.currentTarget)); $("#report-email-send").addEventListener("click", emailReport); $("#unformed-more").addEventListener("click", () => { const batchSize = isCensusRange() ? 120 : 40; state.unformedVisibleCount = Math.max(batchSize, Number(state.unformedVisibleCount || 0)) + batchSize; const insights = state.insights || fallbackInsights(); renderUnformedDynamics(insights, collectEvents(insights)); });
    $("#show-new-messages").addEventListener("click", () => { if (state.pendingSnapshot) applyDataSnapshot(state.pendingSnapshot); setView("feed"); window.setTimeout(() => $("#feed-list").scrollIntoView({ behavior: "smooth", block: "start" }), 30); });
    $("#feed-search").addEventListener("input", (event) => { state.feedSearch = event.target.value; state.feedVisibleCount = 140; renderFeed(); }); $("#feed-chat-filter").addEventListener("change", (event) => { state.feedChatId = event.target.value; state.feedVisibleCount = 140; renderFeed(); });
    $$('[data-feed-filter]').forEach((button) => button.addEventListener("click", () => { state.feedFilter = button.dataset.feedFilter; state.feedVisibleCount = 140; $$('[data-feed-filter]').forEach((item) => item.classList.toggle("active", item === button)); renderFeed(); })); $("#feed-more").addEventListener("click", () => { state.feedVisibleCount += 140; renderFeed(); });
    $("#chat-search").addEventListener("input", (event) => { state.chatSearch = event.target.value; renderChatList(); }); $("#contact-search").addEventListener("input", (event) => { state.contactSearch = event.target.value; renderContacts(); });
    $$('[data-contact-filter]').forEach((button) => button.addEventListener("click", () => { state.contactFilter = button.dataset.contactFilter; $$('[data-contact-filter]').forEach((item) => item.classList.toggle("active", item === button)); renderContacts(); })); $$('[data-task-filter]').forEach((button) => button.addEventListener("click", () => { state.taskFilter = button.dataset.taskFilter; $$('[data-task-filter]').forEach((item) => item.classList.toggle("active", item === button)); renderTasks(); })); aiButtons().forEach((button) => button.addEventListener("click", () => runAiAnalysis(false)));
    document.addEventListener("click", (event) => {
      const nav = event.target.closest(".nav-item[data-view]"); if (nav) { event.preventDefault(); setView(nav.dataset.view); return; }
      const preview = event.target.closest("[data-preview-media]"); if (preview) { event.stopPropagation(); openMedia(preview.dataset.previewMedia); return; }
      const more = event.target.closest("[data-event-more]"); if (more) { event.stopPropagation(); const menu = $('[data-event-menu="' + more.dataset.eventMore.replace(/"/g, '\\"') + '"]'); closeEventMenus(); if (menu) menu.hidden = false; return; }
      const feedback = event.target.closest("[data-feedback]"); if (feedback) { event.stopPropagation(); closeEventMenus(); setFeedback(feedback.dataset.eventKey, feedback.dataset.feedback).then(() => showNotice("评价已记录", false)).catch((error) => showNotice("评价保存失败：" + error.message, true)); return; }
      const voice = event.target.closest("[data-voice-transcribe]"); if (voice) { event.stopPropagation(); transcribeVoice(voice); return; }
      const go = event.target.closest("[data-go-view]"); if (go) { setView(go.dataset.goView); return; }
      const selectChat = event.target.closest("[data-select-chat]"); if (selectChat) { openChat(selectChat.dataset.selectChat); return; }
      const open = event.target.closest("[data-open-message]"); if (open) { openMessage(open.dataset.openMessage); return; }
      if (!event.target.closest("[data-event-menu]")) closeEventMenus();
    });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeSettings(); closeMedia(); closeReportExport(); closeEventMenus(); } }); window.addEventListener("hashchange", () => setView(location.hash.slice(1) || "overview"));
  }

  async function start() { restoreSidebarState(); setRange("day"); setView(location.hash.slice(1) || "overview"); updateRefreshControls(); bindEvents(); await loadSettings(); await refresh({ showLoading: true }); restartPoller(); }
  start();
})();
