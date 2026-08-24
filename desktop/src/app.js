(() => {
  "use strict";

  const SERVICE_URL = "http://127.0.0.1:8765";
  const STATUS_URL = `${SERVICE_URL}/api/status`;
  const OPEN_URL = `${SERVICE_URL}/#overview`;
  const POLL_MS = 1000;

  const nodes = {
    dot: document.getElementById("status-dot"),
    title: document.getElementById("status-title"),
    message: document.getElementById("status-message"),
    detail: document.getElementById("status-detail"),
    progress: document.getElementById("progress"),
    retry: document.getElementById("retry"),
    browser: document.getElementById("open-browser"),
  };

  let timer = 0;
  let checking = false;
  let attempt = 0;

  function invoke(name) {
    const api = window.__TAURI__;
    if (!api || !api.core || typeof api.core.invoke !== "function") {
      return Promise.resolve(null);
    }
    return api.core.invoke(name).catch((error) => ({
      state: "error",
      message: "桌面生命周期命令失败。",
      detail: String(error || "未知错误"),
    }));
  }

  function setState(kind, title, message, detail) {
    nodes.dot.className = `status-dot ${kind || ""}`.trim();
    nodes.title.textContent = title;
    nodes.message.textContent = message;
    nodes.detail.textContent = detail || "";
    nodes.progress.classList.toggle("paused", kind === "error");
  }

  function schedule() {
    window.clearTimeout(timer);
    timer = window.setTimeout(checkStatus, POLL_MS);
  }

  async function checkStatus() {
    if (checking) return;
    checking = true;
    attempt += 1;
    try {
      const response = await fetch(`${STATUS_URL}?desktop_probe=${Date.now()}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error(`健康接口返回 HTTP ${response.status}`);
      }
      const payload = await response.json();
      if (payload && payload.started !== false) {
        setState("ready", "本地服务已就绪", "正在打开微语只读工作台…", `已完成第 ${attempt} 次检查`);
        nodes.retry.disabled = true;
        nodes.browser.hidden = false;
        window.clearTimeout(timer);
        window.setTimeout(() => window.location.replace(OPEN_URL), 220);
        return;
      }
      throw new Error("服务已响应，但尚未进入 started 状态");
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error || "未知错误");
      setState(
        "",
        "正在等待本机服务",
        "微语还没有准备好，桌面会继续检查 127.0.0.1:8765。",
        `${detail} · 第 ${attempt} 次检查`,
      );
      schedule();
    } finally {
      checking = false;
    }
  }

  async function startOrRetry(force) {
    nodes.retry.disabled = true;
    nodes.browser.hidden = true;
    setState(
      "",
      force ? "正在重启本桌面启动的服务" : "正在启动微语服务",
      "正在确认现有健康服务；如果没有，则使用项目 .venv Python（开发）或 PyInstaller sidecar（生产）。",
      "只会关闭本桌面自己启动的子进程",
    );
    await invoke(force ? "retry_backend" : "ensure_backend");
    nodes.retry.disabled = false;
    await checkStatus();
  }

  nodes.retry.addEventListener("click", () => startOrRetry(true));
  nodes.browser.addEventListener("click", () => window.location.assign(OPEN_URL));

  startOrRetry(false);
})();
