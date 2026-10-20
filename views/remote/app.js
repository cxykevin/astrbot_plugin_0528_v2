/**
 * Alkaid0 全局配置视图。
 *
 * 后端（remote_config.py）负责 ACP 连接、脱敏、patch 还原和写入；这里只做
 * 「按 schema 画树 / 编辑草稿 / 与初始配置做 diff」。
 *
 * 关键约束：
 * - 只提交与初始配置不同的叶子；没动过的 ``***`` 不会进入 patch。
 * - 可视化树编辑草稿，JSON 模式编辑同一份草稿；应用 JSON 后再保存。
 * - 顶层 ``$schema`` 只是编辑器元数据，不展示、不提交。
 */

const SECRET_HINTS = [
  "key",
  "token",
  "secret",
  "password",
  "passwd",
  "authorization",
  "cookie",
  "credential",
];
const URL_SUFFIXES = ["url", "uri", "proxy"];

const TOP_LABELS = {
  Version: "配置版本",
  Model: "模型",
  Agent: "Agent",
  ThemeID: "主题",
  Server: "服务器",
  ignoreSignals: "忽略信号",
  Context: "上下文",
  DataMask: "数据脱敏",
  Feedback: "反馈",
  Python: "Python",
};

const KEY_LABELS = {
  ModelName: "模型名称",
  ModelID: "模型 ID",
  DefaultModelID: "默认模型",
  Provider: "Provider",
  ProviderKey: "API Key",
  ProviderURL: "API 地址",
  ProviderSpecificConfig: "Provider 专属配置",
  Models: "模型列表",
  AgentModel: "默认模型",
  AgentPrompt: "系统提示词",
  Agents: "子 Agent",
  AgentTools: "工具",
  AllowedTools: "允许的工具",
  ExcludedTools: "排除的工具",
  TerminalEnvs: "终端环境变量",
  FetchProxy: "抓取代理",
  Fetch: "网页抓取",
  RewriteHeaders: "改写请求头",
  host: "监听地址",
  port: "监听端口",
  key: "访问密钥",
  path: "ACP 路径",
  disableStdioServer: "禁用 stdio 服务",
  api_key: "API Key",
  token: "Token",
  proxy_url: "代理地址",
  Source: "pip 源",
  URL: "URL",
  Phrases: "短语",
  Short: "短标签",
  Text: "文本",
  Desc: "说明",
};

const state = {
  base: null,
  draft: null,
  schema: null,
  meta: null,
  mode: "tree",
  expanded: new Set(),
  busy: false,
  loaded: false,
  jsonDirty: false,
  jsonError: "",
  warning: "",
  dirtyCount: 0,
};

const els = {};
let bridge = null;

boot().catch((error) => {
  renderLoadError(error);
});

async function boot() {
  els.tree = document.getElementById("tree");
  els.jsonPanel = document.getElementById("json-panel");
  els.jsonEditor = document.getElementById("json-editor");
  els.jsonError = document.getElementById("json-error");
  els.banner = document.getElementById("banner");
  els.bannerText = document.getElementById("banner-text");
  els.status = document.getElementById("status");
  els.version = document.getElementById("version");
  els.server = document.getElementById("server");
  els.save = document.getElementById("save");
  els.reload = document.getElementById("reload");
  els.remoteReload = document.getElementById("remote-reload");
  els.expandAll = document.getElementById("expand-all");
  els.collapseAll = document.getElementById("collapse-all");
  els.modeTree = document.getElementById("mode-tree");
  els.modeJson = document.getElementById("mode-json");
  els.applyJson = document.getElementById("apply-json");
  els.formatJson = document.getElementById("format-json");
  els.syncJson = document.getElementById("sync-json");
  els.modal = document.getElementById("modal");
  els.modalText = document.getElementById("modal-text");
  els.modalOk = document.getElementById("modal-ok");
  els.modalCancel = document.getElementById("modal-cancel");

  bridge = await resolveBridge();
  await withTimeout(bridge.ready(), 6000, "bridge_timeout");
  applyStaticI18n();
  bindEvents();

  bridge.onContext(() => {
    applyStaticI18n();
    if (state.loaded) {
      renderTree();
      if (state.mode === "json" && !state.jsonDirty) {
        syncJsonEditor();
      }
    }
  });

  await loadConfig();
}

async function resolveBridge() {
  for (let index = 0; index < 100; index += 1) {
    const candidate = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (candidate) {
      return candidate;
    }
    await sleep(50);
  }
  throw new Error("bridge_unavailable");
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function withTimeout(promise, milliseconds, code) {
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      setTimeout(() => reject(new Error(code)), milliseconds);
    }),
  ]);
}

// ── i18n ──────────────────────────────────────────────────────────────
function T(key, fallback = "") {
  if (!bridge) {
    return fallback;
  }
  try {
    const value = bridge.t(key, fallback);
    return value === undefined || value === null ? fallback : String(value);
  } catch {
    return fallback;
  }
}

function applyStaticI18n() {
  const locale = bridge && bridge.getLocale ? bridge.getLocale() : "zh-CN";
  document.documentElement.lang = locale || "zh-CN";
  document.title = T("views.remote.title", "Alkaid0 全局配置");
  for (const node of document.querySelectorAll("[data-i18n]")) {
    const key = node.getAttribute("data-i18n");
    const fallback =
      node.getAttribute("data-fallback") || (node.textContent || "").trim();
    node.textContent = T(key, fallback);
  }
}

// ── 加载 / 保存 ───────────────────────────────────────────────────────
async function loadConfig() {
  setBusy(true);
  setStatus(T("views.remote.ui.loading", "正在从 alkaid0 读取配置…"));
  hideBanner();
  clearJsonError();
  try {
    const data = await bridge.apiGet("remote/config");
    if (
      !data ||
      typeof data !== "object" ||
      !isPlainObject(data.config) ||
      !isPlainObject(data.schema)
    ) {
      throw new Error("bad_response");
    }
    state.base = data.config;
    state.draft = deepClone(data.config);
    state.schema = data.schema;
    state.meta = data.meta || {};
    state.warning = "";
    state.loaded = true;
    state.jsonDirty = false;
    state.expanded.clear();
    for (const key of Object.keys(state.schema.properties || {})) {
      if (key !== "$schema") {
        state.expanded.add(pathKey([key]));
      }
    }
    for (const key of Object.keys(state.draft)) {
      if (key !== "$schema") {
        state.expanded.add(pathKey([key]));
      }
    }
    renderTree();
    updateHeader();
    setMode("tree", { force: true });
    setStatus("");
  } catch (error) {
    renderLoadError(error);
  } finally {
    setBusy(false);
    refreshDirtyMarks();
  }
}

async function saveConfig() {
  if (state.busy) {
    return;
  }
  if (state.mode === "json" && state.jsonDirty) {
    if (!applyJson()) {
      return;
    }
  }
  const patch = computePatch(state.base, state.draft);
  if (!patch) {
    setStatus(T("views.remote.ui.no_changes", "没有需要保存的改动。"));
    return;
  }

  const restart = changedRestartPaths();
  if (restart.length) {
    let message = T(
      "views.remote.ui.restart_confirm",
      "已修改需要重启 alkaid0 的字段：{paths}。\n保存后请重启 alkaid0 使配置生效。仍要保存吗？",
    )
      .split("{paths}")
      .join(restart.join("、"));
    if (restart.includes("Server.key")) {
      message +=
        "\n\n" +
        T(
          "views.remote.ui.key_confirm",
          "注意：改了 Server.key 之后，本插件的执行器面板 / 插件配置也要同步新的 ACP 地址，否则下一条消息会认证失败。",
        );
    }
    if (!(await askConfirm(message))) {
      return;
    }
  }

  setBusy(true);
  setStatus(T("views.remote.ui.saving", "正在保存…"));
  try {
    const result = await bridge.apiPost("remote/config", { patch });
    if (!result || !isPlainObject(result.config)) {
      throw new Error("bad_response");
    }
    state.base = result.config;
    state.draft = deepClone(result.config);
    state.schema = result.schema || state.schema;
    state.meta = result.meta || state.meta;
    state.warning = result.warning || "";
    state.loaded = true;
    state.jsonDirty = false;
    renderTree();
    updateHeader();
    setMode("tree", { force: true });
    const saved = Array.isArray(result.saved)
      ? result.saved.length
      : countLeaves(patch);
    if (result.warning === "refresh_failed") {
      setStatus(
        T(
          "views.remote.ui.saved_refresh_failed",
          "已保存，但回读配置失败；请点「重新读取」确认最新值。",
        ),
        "warning",
      );
    } else if (saved === 0) {
      setStatus(T("views.remote.ui.no_changes", "没有需要保存的改动。"));
    } else {
      setStatus(
        T("views.remote.ui.saved", "已保存 {n} 项。").replace(
          "{n}",
          () => String(saved),
        ),
        "ok",
      );
    }
  } catch (error) {
    setStatus(formatError(error), "error");
  } finally {
    setBusy(false);
    refreshDirtyMarks();
  }
}

async function reloadFromRemote() {
  if (state.busy) {
    return;
  }
  if (state.dirtyCount || state.jsonDirty) {
    const ok = await askConfirm(
      T(
        "views.remote.ui.discard_confirm",
        "当前有未保存的改动，重新读取会丢弃它们。继续吗？",
      ),
    );
    if (!ok) {
      return;
    }
  }
  await loadConfig();
}

async function reloadRemoteConfig() {
  if (state.busy) {
    return;
  }
  let message = T(
    "views.remote.ui.reload_remote_confirm",
    "让 alkaid0 从磁盘重新加载配置？这会覆盖内存里的当前值。",
  );
  if (state.dirtyCount || state.jsonDirty) {
    message +=
      "\n\n" +
      T(
        "views.remote.ui.discard_confirm",
        "当前有未保存的改动，重新读取会丢弃它们。继续吗？",
      );
  }
  if (!(await askConfirm(message))) {
    return;
  }
  setBusy(true);
  setStatus(T("views.remote.ui.loading", "正在从 alkaid0 读取配置…"));
  try {
    const result = await bridge.apiPost("remote/reload", {});
    if (!result || !isPlainObject(result.config)) {
      throw new Error("bad_response");
    }
    state.base = result.config;
    state.draft = deepClone(result.config);
    state.schema = result.schema || state.schema;
    state.meta = result.meta || state.meta;
    state.warning = "";
    state.loaded = true;
    state.jsonDirty = false;
    renderTree();
    updateHeader();
    setMode("tree", { force: true });
    setStatus(
      T("views.remote.ui.reloaded", "已从磁盘重载 alkaid0 配置。"),
      "ok",
    );
  } catch (error) {
    setStatus(formatError(error), "error");
  } finally {
    setBusy(false);
    refreshDirtyMarks();
  }
}

// ── 模式切换 / JSON 编辑 ──────────────────────────────────────────────
function setMode(mode, { force = false } = {}) {
  if (mode === "json") {
    state.mode = "json";
    if (!state.jsonDirty || force) {
      syncJsonEditor();
    }
    syncModeUi();
    return true;
  }
  if (state.mode === "json" && state.jsonDirty && !force) {
    return applyJson();
  }
  state.mode = "tree";
  syncModeUi();
  return true;
}

function syncModeUi() {
  const isTree = state.mode === "tree";
  if (els.tree) {
    els.tree.hidden = !isTree;
  }
  if (els.jsonPanel) {
    els.jsonPanel.hidden = isTree;
  }
  if (els.modeTree) {
    els.modeTree.classList.toggle("is-active", isTree);
    els.modeTree.setAttribute("aria-selected", String(isTree));
  }
  if (els.modeJson) {
    els.modeJson.classList.toggle("is-active", !isTree);
    els.modeJson.setAttribute("aria-selected", String(!isTree));
  }
}

function editableDraft() {
  const copy = deepClone(state.draft ?? {});
  // $schema 只是编辑器元数据：画树与 JSON 模式都不展示，也不参与提交。
  delete copy.$schema;
  return copy;
}

function syncJsonEditor() {
  if (!els.jsonEditor) {
    return;
  }
  els.jsonEditor.value = JSON.stringify(editableDraft(), null, 2);
  state.jsonDirty = false;
  clearJsonError();
}

function parseJsonDraft() {
  let parsed;
  try {
    parsed = JSON.parse(els.jsonEditor.value);
  } catch (error) {
    throw new Error(`json_parse:${error.message}`);
  }
  if (!isPlainObject(parsed)) {
    throw new Error("json_object_required");
  }
  const properties = (state.schema && state.schema.properties) || {};
  for (const key of Object.keys(parsed)) {
    if (key === "$schema") {
      continue;
    }
    if (!hasOwn(properties, key) && !hasOwn(state.base || {}, key)) {
      throw new Error(`unknown_field:${key}`);
    }
  }
  return parsed;
}

function applyJson() {
  let parsed;
  try {
    parsed = parseJsonDraft();
  } catch (error) {
    const message = formatError(error);
    showJsonError(message);
    setStatus(message, "error");
    return false;
  }
  state.draft = parsed;
  state.jsonDirty = false;
  clearJsonError();
  state.mode = "tree";
  syncModeUi();
  renderTree();
  updateHeader();
  setStatus(
    T("views.remote.ui.json_applied", "JSON 已应用到草稿，尚未保存。"),
    "warning",
  );
  refreshDirtyMarks();
  return true;
}

function formatJson() {
  try {
    const parsed = JSON.parse(els.jsonEditor.value);
    els.jsonEditor.value = JSON.stringify(parsed, null, 2);
    state.jsonDirty = true;
    clearJsonError();
    refreshDirtyMarks();
  } catch (error) {
    showJsonError(formatError(new Error(`json_parse:${error.message}`)));
  }
}

function syncJsonFromDraft() {
  syncJsonEditor();
  refreshDirtyMarks();
  setStatus(T("views.remote.ui.json_synced", "已恢复到当前草稿。"));
}

function showJsonError(message) {
  state.jsonError = message || "";
  if (els.jsonError) {
    els.jsonError.textContent = state.jsonError;
    els.jsonError.hidden = !state.jsonError;
  }
  if (els.jsonEditor) {
    els.jsonEditor.classList.toggle("invalid", Boolean(state.jsonError));
  }
}

function clearJsonError() {
  showJsonError("");
}

// ── 草稿 diff / 脏标记 ────────────────────────────────────────────────
function computePatch(base, draft, path = []) {
  if (deepEqual(base, draft)) {
    return undefined;
  }
  if (isPlainObject(base) && isPlainObject(draft)) {
    const result = {};
    const keys = new Set([...Object.keys(base), ...Object.keys(draft)]);
    for (const key of keys) {
      if (path.length === 0 && key === "$schema") {
        continue;
      }
      const inBase = hasOwn(base, key);
      const inDraft = hasOwn(draft, key);
      if (!inDraft) {
        result[key] = null;
        continue;
      }
      if (!inBase) {
        result[key] = deepClone(draft[key]);
        continue;
      }
      const child = computePatch(base[key], draft[key], [...path, key]);
      if (child !== undefined) {
        result[key] = child;
      }
    }
    return Object.keys(result).length ? result : undefined;
  }
  if (Array.isArray(base) || Array.isArray(draft)) {
    return deepClone(draft);
  }
  return deepClone(draft);
}

function countLeaves(value) {
  if (value === undefined) {
    return 0;
  }
  if (Array.isArray(value)) {
    return 1;
  }
  if (isPlainObject(value)) {
    const keys = Object.keys(value);
    if (!keys.length) {
      return 1;
    }
    let total = 0;
    for (const key of keys) {
      total += countLeaves(value[key]);
    }
    return total;
  }
  return 1;
}

function isDirtyPath(path) {
  return !deepEqual(getAt(state.base, path), getAt(state.draft, path));
}

function changedRestartPaths() {
  const paths = (state.meta && state.meta.restart_paths) || [];
  return paths.filter((dotted) => {
    const path = String(dotted).split(".");
    return !deepEqual(getAt(state.base, path), getAt(state.draft, path));
  });
}

function refreshDirtyMarks() {
  if (!state.loaded || !state.draft) {
    return;
  }
  const patch = computePatch(state.base, state.draft);
  state.dirtyCount = patch ? countLeaves(patch) : 0;

  if (els.tree) {
    for (const node of els.tree.querySelectorAll("[data-path]")) {
      try {
        const path = JSON.parse(node.dataset.path);
        node.classList.toggle("is-dirty", isDirtyPath(path));
      } catch {
        // 忽略被替换掉的旧节点。
      }
    }
  }

  updateSaveButton();
  updateBanner();

  if (state.busy || state.jsonError) {
    return;
  }
  if (state.mode === "json" && state.jsonDirty) {
    setStatus(
      T("views.remote.ui.json_unapplied", "JSON 有未应用的改动。"),
      "warning",
    );
  } else if (state.dirtyCount) {
    setStatus(
      T("views.remote.ui.unsaved", "有 {n} 项未保存。").replace(
        "{n}",
        () => String(state.dirtyCount),
      ),
      "warning",
    );
  }
}

function updateSaveButton() {
  if (!els.save) {
    return;
  }
  const canSave =
    state.dirtyCount > 0 || (state.mode === "json" && state.jsonDirty);
  els.save.disabled = state.busy || !canSave;
  const base = T("views.remote.ui.save", "保存");
  els.save.textContent = state.dirtyCount ? `${base} (${state.dirtyCount})` : base;
}

function setBusy(busy) {
  state.busy = busy;
  for (const button of [
    els.reload,
    els.remoteReload,
    els.expandAll,
    els.collapseAll,
    els.modeTree,
    els.modeJson,
    els.applyJson,
    els.formatJson,
    els.syncJson,
  ]) {
    if (button) {
      button.disabled = busy;
    }
  }
  updateSaveButton();
}

function setStatus(text, kind = "") {
  if (!els.status) {
    return;
  }
  els.status.textContent = text || "";
  els.status.className = `status${kind ? ` ${kind}` : ""}`;
}

function updateHeader() {
  const version = state.draft && state.draft.Version ? String(state.draft.Version) : "";
  if (els.version) {
    els.version.textContent = version ? `v${version}` : "";
    els.version.hidden = !version;
  }
  const server = state.meta && state.meta.server ? String(state.meta.server) : "";
  if (els.server) {
    els.server.textContent = server
      ? `${T("views.remote.ui.server", "远端")}: ${server}`
      : "";
    els.server.title = server;
    els.server.hidden = !server;
  }
}

// ── 提示 / 确认 ───────────────────────────────────────────────────────
function updateBanner() {
  if (!els.banner || !els.bannerText) {
    return;
  }
  if (state.warning === "refresh_failed") {
    setBanner(
      T(
        "views.remote.ui.refresh_failed",
        "配置已保存，但回读失败；请点「重新读取」确认最新值。",
      ),
      "warning",
    );
    return;
  }
  if (state.warning) {
    setBanner(state.warning, "warning");
    return;
  }
  const restart = changedRestartPaths();
  if (restart.length) {
    let text = T(
      "views.remote.ui.restart_warning",
      "已修改需要重启 alkaid0 的字段：{paths}。保存后请重启 alkaid0 使配置生效。",
    )
      .split("{paths}")
      .join(restart.join("、"));
    if (restart.includes("Server.key")) {
      text +=
        " " +
        T(
          "views.remote.ui.key_warning",
          "改 Server.key 后，本插件的执行器面板 / 插件配置也要同步新的 ACP 地址。",
        );
    }
    setBanner(text, "warning");
    return;
  }
  hideBanner();
}

function setBanner(text, kind) {
  if (!els.banner || !els.bannerText) {
    return;
  }
  els.banner.hidden = !text;
  els.banner.className = `banner${kind === "warning" ? " warning" : ""}`;
  els.bannerText.textContent = text || "";
}

function hideBanner() {
  if (els.banner) {
    els.banner.hidden = true;
  }
  if (els.bannerText) {
    els.bannerText.textContent = "";
  }
}

function askConfirm(message) {
  return new Promise((resolve) => {
    if (!els.modal || !els.modalText || !els.modalOk || !els.modalCancel) {
      resolve(window.confirm(message));
      return;
    }
    els.modalText.textContent = message;
    els.modal.hidden = false;
    els.modalOk.focus();

    const cleanup = (answer) => {
      els.modal.hidden = true;
      els.modalOk.removeEventListener("click", onOk);
      els.modalCancel.removeEventListener("click", onCancel);
      els.modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve(answer);
    };
    const onOk = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onKey = (event) => {
      if (event.key === "Escape") {
        cleanup(false);
      }
    };
    const onBackdrop = (event) => {
      if (event.target === els.modal) {
        cleanup(false);
      }
    };

    els.modalOk.addEventListener("click", onOk);
    els.modalCancel.addEventListener("click", onCancel);
    els.modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey);
  });
}

function formatError(error) {
  const raw = String((error && error.message) || error || "unknown");
  const separator = raw.indexOf(":");
  const code = separator >= 0 ? raw.slice(0, separator) : raw;
  const detail = separator >= 0 ? raw.slice(separator + 1) : "";

  const defaults = {
    bad_payload: "请求格式不正确。",
    bad_response: "远程配置接口返回了无法识别的数据。",
    bad_config: "alkaid0 返回的 config/get 结构不认识。",
    no_changes: "没有需要保存的改动。",
    unknown_field: "不认识的配置项：{detail}",
    unresolved_mask: "「{detail}」里的 *** 没有可还原的原值，请把完整值填进来。",
    too_large: "提交内容太大。",
    remote_unconfigured:
      "还没有配置 Alkaid0 的 ACP 地址。请先在执行器面板 / 插件配置里设置。",
    remote_unreachable: "连不上 alkaid0：{detail}",
    remote_timeout: "等待 alkaid0 超时。",
    remote_unsupported:
      "当前 alkaid0 不支持远程配置（需要 config/get|set|reload）。",
    remote_error: "alkaid0 返回错误：{detail}",
    bridge_unavailable:
      "找不到 AstrBot 插件页 bridge，请从插件页打开这个视图。",
    bridge_timeout: "连接 Dashboard 超时，请从插件页重新打开这个视图。",
    json_parse: "JSON 语法错误：{detail}",
    json_object_required: "JSON 顶层必须是一个对象。",
    unknown: "操作失败：{detail}",
  };

  let template = T(`views.remote.errors.${code}`, defaults[code] || "");
  if (!template) {
    template = defaults.unknown;
  }
  return template
    .split("{detail}")
    .join(detail || raw)
    .split("{path}")
    .join(detail || raw);
}

function renderLoadError(error) {
  state.loaded = false;
  state.base = null;
  state.draft = null;
  state.schema = null;
  state.meta = null;
  state.jsonDirty = false;
  state.dirtyCount = 0;
  state.mode = "tree";
  syncModeUi();
  hideBanner();
  clearJsonError();

  const message = formatError(error);
  if (els.tree) {
    els.tree.replaceChildren();
    els.tree.setAttribute("aria-busy", "false");
    const note = document.createElement("div");
    note.className = "placeholder error";
    note.textContent = message;
    if (bridge) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn ghost";
      retry.textContent = T("views.remote.ui.retry", "重试");
      retry.addEventListener("click", () => {
        void loadConfig();
      });
      note.append(document.createElement("br"), retry);
    }
    els.tree.append(note);
  }
  if (els.save) {
    els.save.disabled = true;
  }
  updateHeader();
  setStatus(message, "error");
}

// ── 渲染：树 ──────────────────────────────────────────────────────────
function renderTree() {
  if (!state.loaded || !state.draft || !els.tree) {
    return;
  }
  els.tree.replaceChildren();
  els.tree.setAttribute("aria-busy", "false");

  const rootSchema =
    state.schema && isPlainObject(state.schema)
      ? state.schema
      : { type: "object", properties: {} };
  const properties = rootSchema.properties || {};
  const seen = new Set();

  for (const key of Object.keys(state.draft)) {
    if (key === "$schema") {
      continue;
    }
    seen.add(key);
    const schema = properties[key] || inferSchema(state.draft[key]);
    els.tree.append(
      renderNodeSection(state.draft[key], [key], schema, { top: true }),
    );
  }
  for (const key of Object.keys(properties)) {
    if (key === "$schema" || seen.has(key)) {
      continue;
    }
    els.tree.append(
      renderNodeSection(undefined, [key], properties[key], {
        top: true,
        missing: true,
      }),
    );
  }
  if (!els.tree.childElementCount) {
    els.tree.append(emptyNote(T("views.remote.ui.empty", "没有可编辑的配置项。")));
  }
  updateHeader();
  refreshDirtyMarks();
}

function renderNodeSection(value, path, schema, options = {}) {
  const details = document.createElement("details");
  details.className = "node";
  details.dataset.path = pathKey(path);
  details.open = state.expanded.has(pathKey(path)) || Boolean(options.open);

  const summary = document.createElement("summary");
  const chevron = element("span", "chevron");
  chevron.textContent = "›";
  chevron.setAttribute("aria-hidden", "true");

  const dot = element("span", "dirty-dot");
  dot.setAttribute("aria-hidden", "true");

  const text = element("div", "summary-text");
  const titleLine = element("div", "summary-title");
  const title = element("span");
  title.textContent = labelFor(path, schema);
  titleLine.append(title);

  const count = childCount(value, schema);
  if (count) {
    const badge = element("span", "count");
    badge.textContent = String(count);
    titleLine.append(badge);
  }
  if (isReadOnlyPath(path)) {
    titleLine.append(
      badgeNode("readonly", T("views.remote.ui.readonly", "只读")),
    );
  }
  if (isSecretPath(path)) {
    titleLine.append(
      badgeNode("secret", T("views.remote.ui.secret", "敏感")),
    );
  }
  if (isRestartPath(path)) {
    titleLine.append(
      badgeNode("restart", T("views.remote.ui.restart", "需重启")),
    );
  }

  const desc = element("div", "summary-desc");
  desc.textContent = summaryHint(path, schema, options);
  text.append(titleLine, desc);
  summary.append(chevron, dot, text);
  details.append(summary);

  const body = element("div", "node-body");
  body.append(renderNode(value, path, schema, options));
  details.append(body);

  details.addEventListener("toggle", () => {
    if (details.open) {
      state.expanded.add(pathKey(path));
    } else {
      state.expanded.delete(pathKey(path));
    }
  });
  return details;
}

function renderNode(value, path, schema, options = {}) {
  const resolved = schema || inferSchema(value);
  if (resolved.type === "$unknown") {
    return renderJsonControl(value, path);
  }
  if (Array.isArray(value)) {
    if (resolved.type && resolved.type !== "array") {
      return renderJsonControl(value, path);
    }
    return renderArray(value, path, resolved);
  }
  if (isPlainObject(value)) {
    if (resolved.type && resolved.type !== "object") {
      return renderJsonControl(value, path);
    }
    return renderObject(value, path, resolved);
  }
  if (resolved.type === "array") {
    return renderArray(value, path, resolved);
  }
  if (resolved.type === "object") {
    return renderObject(value, path, resolved);
  }
  return renderLeafRow(value, path, resolved, options);
}

function renderObject(value, path, schema) {
  const obj = isPlainObject(value) ? value : {};
  const properties = schema.properties || {};
  const propKeys = Object.keys(properties);
  const extraKeys = Object.keys(obj).filter((key) => !hasOwn(properties, key));
  const container = element("div", "object-body");

  for (const key of propKeys) {
    const childPath = [...path, key];
    const childSchema = properties[key] || inferSchema(obj[key]);
    const childValue = obj[key];
    if (isComplexSchema(childSchema, childValue)) {
      container.append(renderNodeSection(childValue, childPath, childSchema));
    } else {
      container.append(renderLeafRow(childValue, childPath, childSchema));
    }
  }

  if (schema.additionalProperties) {
    container.append(
      renderMap(obj, path, schema, {
        keys: extraKeys,
        title: propKeys.length
          ? T("views.remote.ui.other_entries", "其它条目")
          : T("views.remote.ui.entries", "条目"),
      }),
    );
  } else if (extraKeys.length) {
    container.append(renderUnknownEntries(obj, path, extraKeys));
  }

  if (!propKeys.length && !extraKeys.length && !schema.additionalProperties) {
    container.append(emptyNote(T("views.remote.ui.empty_object", "这个对象还没有字段。")));
  }
  return container;
}

function renderUnknownEntries(obj, path, keys) {
  const block = element("div", "map-block");
  const head = element("div", "map-head");
  const title = element("span", "map-title");
  title.textContent = T("views.remote.ui.unknown_fields", "未在 schema 中的字段");
  head.append(title);
  block.append(head);
  for (const key of keys) {
    const childPath = [...path, key];
    block.append(
      renderLeafRow(obj[key], childPath, inferSchema(obj[key]), {
        onRemove: () => {
          const parent = getAt(state.draft, path);
          if (!isPlainObject(parent)) {
            return;
          }
          const next = { ...parent };
          delete next[key];
          setAt(state.draft, path, next);
          renderTree();
        },
      }),
    );
  }
  return block;
}

function renderLeafRow(value, path, schema, options = {}) {
  const resolved = schema || inferSchema(value);
  const leaf = element("div", "leaf");
  leaf.dataset.path = pathKey(path);
  leaf.classList.toggle("is-dirty", isDirtyPath(path));

  const head = element("div", "leaf-head");
  const dot = element("span", "dirty-dot");
  dot.setAttribute("aria-hidden", "true");
  const title = element("span", "leaf-title");
  title.textContent = options.title || labelFor(path, resolved);
  head.append(dot, title);

  if (isReadOnlyPath(path)) {
    head.append(badgeNode("readonly", T("views.remote.ui.readonly", "只读")));
  }
  if (isSecretPath(path)) {
    head.append(badgeNode("secret", T("views.remote.ui.secret", "敏感")));
  }
  if (isRestartPath(path)) {
    head.append(badgeNode("restart", T("views.remote.ui.restart", "需重启")));
  }

  const actions = element("div", "row-actions");
  if (!isReadOnlyPath(path)) {
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "btn ghost small";
    undo.textContent = T("views.remote.ui.undo", "撤销");
    undo.addEventListener("click", () => {
      undoPath(path);
    });
    actions.append(undo);
  }
  if (options.onRemove) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn danger small";
    remove.textContent = T("views.remote.ui.delete", "删除");
    remove.addEventListener("click", () => options.onRemove());
    actions.append(remove);
  }
  head.append(actions);

  const control = renderLeafControl(value, path, resolved);
  leaf.append(head, control);

  const hintText = leafHint(path, resolved, options);
  if (hintText) {
    const hint = element("div", "leaf-hint");
    hint.textContent = hintText;
    leaf.append(hint);
  }
  if (resolved.default !== undefined) {
    const meta = element("div", "leaf-meta");
    const item = document.createElement("span");
    item.textContent = `${T("views.remote.ui.default", "默认")}: `;
    const code = document.createElement("code");
    code.textContent = shortValue(resolved.default);
    item.append(code);
    meta.append(item);
    leaf.append(meta);
  }
  return leaf;
}

function renderLeafControl(value, path, schema) {
  const wrap = element("div", "leaf-control");
  const resolved = schema || inferSchema(value);

  if (isReadOnlyPath(path)) {
    const input = document.createElement("input");
    input.className = "input";
    input.value = value === null || value === undefined ? "" : String(value);
    input.disabled = true;
    wrap.append(input);
    return wrap;
  }

  if (resolved.type === "$unknown") {
    wrap.append(renderJsonControl(value, path));
    return wrap;
  }

  if (isPlainObject(value) || Array.isArray(value)) {
    wrap.append(renderJsonControl(value, path));
    return wrap;
  }

  const type = resolved.type || typeof value;
  if (
    Array.isArray(resolved.enum) &&
    ["string", "integer", "number"].includes(type)
  ) {
    wrap.append(renderEnumControl(value, path, resolved, type));
    return wrap;
  }
  if (type === "boolean") {
    wrap.append(renderBoolControl(value, path));
    return wrap;
  }
  if (type === "integer" || type === "number") {
    wrap.append(renderNumberControl(value, path, resolved, type));
    return wrap;
  }
  if (type === "string" || value === null || value === undefined) {
    wrap.append(renderStringControl(value, path, resolved));
    return wrap;
  }
  wrap.append(renderJsonControl(value, path));
  return wrap;
}

function renderEnumControl(value, path, schema, type) {
  const select = document.createElement("select");
  const values = Array.isArray(schema.enum) ? schema.enum.map(String) : [];
  const current = value === null || value === undefined ? "" : String(value);
  if (!values.includes(current)) {
    values.unshift(current);
  }
  for (const optionValue of values) {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = optionValue === "" ? T("views.remote.ui.empty", "空") : optionValue;
    select.append(option);
  }
  select.value = current;
  select.addEventListener("change", () => {
    let next = select.value;
    if (type === "integer") {
      next = Number.parseInt(next, 10);
    } else if (type === "number") {
      next = Number(next);
    }
    setAt(state.draft, path, next);
    refreshDirtyMarks();
  });
  return select;
}

function renderBoolControl(value, path) {
  const label = element("label", "switch");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(value);
  const track = element("span", "switch-track");
  const text = element("span", "switch-label");
  text.textContent = input.checked
    ? T("views.remote.ui.on", "开启")
    : T("views.remote.ui.off", "关闭");
  label.append(input, track, text);
  input.addEventListener("change", () => {
    text.textContent = input.checked
      ? T("views.remote.ui.on", "开启")
      : T("views.remote.ui.off", "关闭");
    setAt(state.draft, path, Boolean(input.checked));
    refreshDirtyMarks();
  });
  return label;
}

function renderNumberControl(value, path, schema, type) {
  const input = document.createElement("input");
  input.type = "number";
  input.className = "input mono";
  input.step = type === "integer" ? "1" : "any";
  if (schema.minimum !== undefined) {
    input.min = String(schema.minimum);
  }
  if (schema.maximum !== undefined) {
    input.max = String(schema.maximum);
  }
  input.value = value === null || value === undefined ? "" : String(value);
  if (schema.default !== undefined) {
    input.placeholder = String(schema.default);
  }
  input.addEventListener("input", () => {
    const raw = input.value;
    if (raw === "") {
      return;
    }
    const number = Number(raw);
    if (
      !Number.isFinite(number) ||
      (type === "integer" && !Number.isInteger(number))
    ) {
      input.classList.add("invalid");
      return;
    }
    input.classList.remove("invalid");
    setAt(state.draft, path, type === "integer" ? Math.trunc(number) : number);
    refreshDirtyMarks();
  });
  input.addEventListener("blur", () => {
    if (input.value === "") {
      input.classList.remove("invalid");
      input.value = "0";
      setAt(state.draft, path, 0);
      refreshDirtyMarks();
    }
  });
  return input;
}

function renderStringControl(value, path, schema) {
  const isSecret = isSecretPath(path);
  const isUrl = isUrlPath(path);
  const multiline =
    isMultilinePath(path) || (typeof value === "string" && value.length > 180);
  const input = document.createElement(multiline ? "textarea" : "input");
  input.className = `input${isSecret || isUrl ? " mono" : ""}`;
  input.autocomplete = "off";
  input.spellcheck = false;
  if (!multiline) {
    input.type = isSecret ? "password" : "text";
  }
  input.value = value === null || value === undefined ? "" : String(value);
  if (schema.default !== undefined && schema.default !== null) {
    input.placeholder = String(schema.default);
  }
  input.addEventListener("input", () => {
    setAt(state.draft, path, input.value);
    refreshDirtyMarks();
  });

  if (!isSecret) {
    return input;
  }
  const wrap = element("div", "input-wrap");
  const reveal = document.createElement("button");
  reveal.type = "button";
  reveal.className = "reveal";
  reveal.textContent = T("views.remote.ui.show", "显示");
  reveal.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    reveal.textContent = show
      ? T("views.remote.ui.hide", "隐藏")
      : T("views.remote.ui.show", "显示");
  });
  wrap.append(input, reveal);
  return wrap;
}

function renderJsonControl(value, path) {
  const wrap = document.createElement("div");
  const input = document.createElement("textarea");
  input.className = "input mono";
  input.spellcheck = false;
  input.rows = 6;
  input.value = JSON.stringify(value === undefined ? null : value, null, 2);
  const error = element("div", "field-error");
  error.hidden = true;
  input.addEventListener("change", () => {
    let parsed;
    try {
      parsed = JSON.parse(input.value);
    } catch (parseError) {
      input.classList.add("invalid");
      error.textContent = T(
        "views.remote.ui.json_value_invalid",
        "这里必须是合法 JSON：{detail}",
      )
        .split("{detail}")
        .join(parseError.message);
      error.hidden = false;
      return;
    }
    input.classList.remove("invalid");
    error.hidden = true;
    setAt(state.draft, path, parsed);
    refreshDirtyMarks();
  });
  wrap.append(input, error);
  return wrap;
}

function renderArray(value, path, schema) {
  const arr = Array.isArray(value) ? value : [];
  const itemSchema =
    schema.items || inferSchema(arr[0]) || { type: "$unknown" };
  const container = element("div", "array-body");
  const complexItems =
    itemSchema.type === "object" ||
    itemSchema.type === "array" ||
    Boolean(itemSchema.properties || itemSchema.additionalProperties);

  if (!arr.length) {
    container.append(emptyNote(T("views.remote.ui.empty_array", "还没有条目。")));
  } else if (complexItems) {
    arr.forEach((item, index) => {
      container.append(
        renderArrayObjectItem(item, path, index, arr, itemSchema),
      );
    });
  } else if (isPrimitiveSchema(itemSchema)) {
    arr.forEach((item, index) => {
      container.append(
        renderPrimitiveArrayItem(item, path, index, arr, itemSchema),
      );
    });
  } else {
    container.append(renderJsonControl(value, path));
    return container;
  }

  const actions = element("div", "node-actions");
  const add = document.createElement("button");
  add.type = "button";
  add.className = "btn ghost small";
  add.textContent = T("views.remote.ui.add_item", "添加一项");
  add.addEventListener("click", () => {
    const next = [...arr, defaultForSchema(itemSchema)];
    setAt(state.draft, path, next);
    state.expanded.add(pathKey([...path, arr.length]));
    renderTree();
  });
  actions.append(add);
  container.append(actions);
  return container;
}

function renderPrimitiveArrayItem(item, path, index, arr, itemSchema) {
  const itemPath = [...path, index];
  const row = element("div", "leaf");
  row.dataset.path = pathKey(itemPath);
  row.classList.toggle("is-dirty", isDirtyPath(itemPath));

  const head = element("div", "leaf-head");
  const dot = element("span", "dirty-dot");
  dot.setAttribute("aria-hidden", "true");
  const title = element("span", "leaf-title");
  title.textContent = `#${index + 1}`;
  head.append(dot, title);

  const actions = element("div", "row-actions");
  actions.append(
    arrayAction(T("views.remote.ui.move_up", "上移"), index === 0, () => {
      moveArrayItem(path, arr, index, index - 1);
    }),
    arrayAction(
      T("views.remote.ui.move_down", "下移"),
      index === arr.length - 1,
      () => {
        moveArrayItem(path, arr, index, index + 1);
      },
    ),
    arrayAction(T("views.remote.ui.delete", "删除"), false, () => {
      removeArrayItem(path, arr, index);
    }, true),
  );
  head.append(actions);

  row.append(head, renderLeafControl(item, itemPath, itemSchema));
  if (itemSchema.description) {
    const hint = element("div", "leaf-hint");
    hint.textContent = itemSchema.description;
    row.append(hint);
  }
  return row;
}

function renderArrayObjectItem(item, path, index, arr, itemSchema) {
  const itemPath = [...path, index];
  const details = document.createElement("details");
  details.className = "array-item";
  details.dataset.path = pathKey(itemPath);
  details.open = state.expanded.has(pathKey(itemPath));

  const summary = document.createElement("summary");
  const chevron = element("span", "chevron");
  chevron.textContent = "›";
  chevron.setAttribute("aria-hidden", "true");
  const dot = element("span", "dirty-dot");
  dot.setAttribute("aria-hidden", "true");
  const title = element("span", "entry-title");
  title.textContent = `#${index + 1}`;
  const hint = element("span", "entry-hint");
  hint.textContent = preview(item, itemSchema, itemPath);
  summary.append(chevron, dot, title, hint);
  details.append(summary);

  const body = element("div", "entry-body");
  const actions = element("div", "entry-actions");
  actions.append(
    arrayAction(T("views.remote.ui.move_up", "上移"), index === 0, () => {
      moveArrayItem(path, arr, index, index - 1);
    }),
    arrayAction(
      T("views.remote.ui.move_down", "下移"),
      index === arr.length - 1,
      () => {
        moveArrayItem(path, arr, index, index + 1);
      },
    ),
    arrayAction(T("views.remote.ui.delete", "删除"), false, () => {
      removeArrayItem(path, arr, index);
    }, true),
  );
  body.append(actions, renderNode(item, itemPath, itemSchema));
  details.append(body);

  details.addEventListener("toggle", () => {
    if (details.open) {
      state.expanded.add(pathKey(itemPath));
    } else {
      state.expanded.delete(pathKey(itemPath));
    }
  });
  return details;
}

function renderMap(mapValue, path, schema, options = {}) {
  const map = isPlainObject(mapValue) ? mapValue : {};
  const keys = options.keys || Object.keys(map);
  const valueSchema =
    schema.additionalProperties === true
      ? { type: "$unknown" }
      : schema.additionalProperties || { type: "$unknown" };
  const numeric = looksNumericMap(path, schema);
  const block = element("div", "map-block");

  const head = element("div", "map-head");
  const title = element("span", "map-title");
  title.textContent = options.title || T("views.remote.ui.entries", "条目");
  const count = element("span", "count");
  count.textContent = String(keys.length);
  head.append(title, count);
  block.append(head);

  if (!keys.length) {
    block.append(emptyNote(T("views.remote.ui.empty_map", "还没有条目。")));
  }
  for (const key of keys) {
    block.append(renderMapEntry(map, path, key, valueSchema, numeric));
  }
  if (options.add !== false) {
    block.append(renderAddKeyRow(map, path, valueSchema, numeric, keys));
  }
  return block;
}

function renderMapEntry(map, path, key, valueSchema, numeric) {
  const entryPath = [...path, key];
  const value = map[key];
  const details = document.createElement("details");
  details.className = "map-entry";
  details.dataset.path = pathKey(entryPath);
  details.open = state.expanded.has(pathKey(entryPath));

  const summary = document.createElement("summary");
  const chevron = element("span", "chevron");
  chevron.textContent = "›";
  chevron.setAttribute("aria-hidden", "true");
  const dot = element("span", "dirty-dot");
  dot.setAttribute("aria-hidden", "true");
  const title = element("span", "entry-title");
  title.textContent = numeric && /^\d+$/.test(String(key)) ? `#${key}` : String(key);
  const hint = element("span", "entry-hint");
  hint.textContent = preview(value, valueSchema, entryPath);
  summary.append(chevron, dot, title, hint);
  details.append(summary);

  const body = element("div", "entry-body");
  const keyRow = element("div", "key-row");
  const keyLabel = document.createElement("label");
  keyLabel.textContent = T("views.remote.ui.key_name", "键名");
  const keyInput = document.createElement("input");
  keyInput.className = "input key-input";
  keyInput.type = numeric ? "number" : "text";
  keyInput.value = String(key);
  if (numeric) {
    keyInput.min = "0";
    keyInput.step = "1";
  }
  keyInput.addEventListener("input", () => keyInput.classList.remove("invalid"));
  keyInput.addEventListener("change", () => {
    const ok = renameMapKey(path, key, keyInput.value, numeric);
    if (!ok) {
      keyInput.value = String(key);
    }
  });
  keyRow.append(keyLabel, keyInput);

  const actions = element("div", "entry-actions");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn danger small";
  remove.textContent = T("views.remote.ui.delete", "删除");
  remove.addEventListener("click", () => {
    const next = { ...map };
    delete next[key];
    setAt(state.draft, path, next);
    renderTree();
  });
  actions.append(remove);
  body.append(keyRow, actions);

  if (isComplexSchema(valueSchema, value)) {
    body.append(renderNode(value, entryPath, valueSchema));
  } else {
    body.append(
      renderLeafRow(value, entryPath, valueSchema, {
        title: T("views.remote.ui.value", "值"),
      }),
    );
  }
  details.append(body);

  details.addEventListener("toggle", () => {
    if (details.open) {
      state.expanded.add(pathKey(entryPath));
    } else {
      state.expanded.delete(pathKey(entryPath));
    }
  });
  return details;
}

function renderAddKeyRow(map, path, valueSchema, numeric, keys) {
  const row = element("div", "add-row");
  const input = document.createElement("input");
  input.className = "input key-input";
  input.type = numeric ? "number" : "text";
  input.placeholder = numeric
    ? String(nextNumericKey(keys))
    : T("views.remote.ui.new_key", "新键名");
  if (numeric) {
    input.min = "0";
    input.step = "1";
  }
  input.addEventListener("input", () => input.classList.remove("invalid"));

  const add = document.createElement("button");
  add.type = "button";
  add.className = "btn ghost small";
  add.textContent = T("views.remote.ui.add", "添加");
  const commit = () => {
    let newKey = String(input.value).trim();
    if (!newKey && numeric) {
      newKey = String(nextNumericKey(keys));
    }
    if (numeric) {
      const number = Number(newKey);
      if (!Number.isInteger(number) || number < 0) {
        input.classList.add("invalid");
        input.focus();
        return;
      }
      newKey = String(number);
    }
    if (!newKey) {
      input.classList.add("invalid");
      input.focus();
      return;
    }
    if (hasOwn(map, newKey)) {
      input.classList.add("invalid");
      input.focus();
      setStatus(
        T("views.remote.ui.key_exists", "这个键已经存在。"),
        "error",
      );
      return;
    }
    const next = { ...map, [newKey]: defaultForSchema(valueSchema) };
    setAt(state.draft, path, next);
    state.expanded.add(pathKey([...path, newKey]));
    renderTree();
  };
  add.addEventListener("click", commit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    }
  });
  row.append(input, add);
  return row;
}

// ── 交互辅助 ──────────────────────────────────────────────────────────
function undoPath(path) {
  const oldValue = getAt(state.base, path);
  if (oldValue === undefined) {
    deleteAt(state.draft, path);
  } else {
    setAt(state.draft, path, deepClone(oldValue));
  }
  renderTree();
}

function moveArrayItem(path, arr, from, to) {
  if (to < 0 || to >= arr.length) {
    return;
  }
  const next = [...arr];
  [next[from], next[to]] = [next[to], next[from]];
  setAt(state.draft, path, next);
  renderTree();
}

function removeArrayItem(path, arr, index) {
  const next = arr.filter((_, itemIndex) => itemIndex !== index);
  setAt(state.draft, path, next);
  renderTree();
}

function renameMapKey(path, oldKey, rawNewKey, numeric) {
  let newKey = String(rawNewKey).trim();
  if (!newKey) {
    return false;
  }
  if (numeric) {
    const number = Number(newKey);
    if (!Number.isInteger(number) || number < 0) {
      return false;
    }
    newKey = String(number);
  }
  if (newKey === oldKey) {
    return true;
  }
  const map = getAt(state.draft, path);
  if (!isPlainObject(map) || hasOwn(map, newKey)) {
    return false;
  }
  const next = {};
  for (const [key, value] of Object.entries(map)) {
    next[key === oldKey ? newKey : key] = value;
  }
  setAt(state.draft, path, next);
  state.expanded.delete(pathKey([...path, oldKey]));
  state.expanded.add(pathKey([...path, newKey]));
  renderTree();
  return true;
}

function arrayAction(text, disabled, handler, danger = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `btn ${danger ? "danger" : "ghost"} small`;
  button.textContent = text;
  button.disabled = Boolean(disabled);
  button.addEventListener("click", handler);
  return button;
}

function expandAllPaths() {
  const walk = (value, path) => {
    if (Array.isArray(value)) {
      state.expanded.add(pathKey(path));
      value.forEach((child, index) => walk(child, [...path, index]));
      return;
    }
    if (isPlainObject(value)) {
      state.expanded.add(pathKey(path));
      for (const [key, child] of Object.entries(value)) {
        walk(child, [...path, key]);
      }
    }
  };
  state.expanded.clear();
  walk(state.draft, []);
  renderTree();
}

function collapseAllPaths() {
  state.expanded.clear();
  renderTree();
}

// ── 小工具 ────────────────────────────────────────────────────────────
function element(tag, className) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  return node;
}

function badgeNode(kind, text) {
  const node = element("span", `badge ${kind}`);
  node.textContent = text;
  return node;
}

function emptyNote(text) {
  const node = element("div", "empty-note");
  node.textContent = text;
  return node;
}

function pathKey(path) {
  return JSON.stringify(path);
}

function labelFor(path, schema) {
  const key = String(path[path.length - 1]);
  if (path.length === 1 && TOP_LABELS[key]) {
    return TOP_LABELS[key];
  }
  if (KEY_LABELS[key]) {
    return KEY_LABELS[key];
  }
  if (schema && schema.title) {
    return String(schema.title);
  }
  if (schema && isLeafSchema(schema) && schema.description) {
    const description = String(schema.description).trim();
    if (description && description.length <= 32 && !/[。；;]$/.test(description)) {
      return description;
    }
  }
  return key;
}

function summaryHint(path, schema, options) {
  const parts = [];
  if (options.missing) {
    parts.push(T("views.remote.ui.not_set", "尚未设置；编辑后保存时创建。"));
  }
  if (schema && schema.description) {
    parts.push(String(schema.description));
  }
  if (isSecretPath(path)) {
    parts.push(T("views.remote.ui.secret_hint", "敏感值已脱敏"));
  } else if (isUrlPath(path)) {
    parts.push(T("views.remote.ui.url_hint", "URL 凭证会自动脱敏"));
  }
  if (isReadOnlyPath(path)) {
    parts.push(T("views.remote.ui.readonly_hint", "只读字段"));
  }
  return parts.join(" · ");
}

function leafHint(path, schema, options) {
  if (options && options.hint) {
    return String(options.hint);
  }
  const parts = [];
  if (schema && schema.description) {
    parts.push(String(schema.description));
  }
  if (isSecretPath(path)) {
    parts.push(
      T(
        "views.remote.ui.secret_leaf_hint",
        "*** 表示保留原值；不修改就不会提交这个字段。",
      ),
    );
  } else if (isUrlPath(path)) {
    parts.push(
      T(
        "views.remote.ui.url_leaf_hint",
        "只改主机 / 路径时，查询参数里的 *** 会自动还原。",
      ),
    );
  }
  if (isReadOnlyPath(path)) {
    parts.push(T("views.remote.ui.readonly_hint", "只读字段"));
  }
  return parts.join(" ");
}

function isLeafSchema(schema) {
  return Boolean(
    schema &&
      ["string", "integer", "number", "boolean", "null"].includes(schema.type),
  );
}

function isPrimitiveSchema(schema) {
  return isLeafSchema(schema);
}

function isComplexSchema(schema, value) {
  if (Array.isArray(value) || isPlainObject(value)) {
    return true;
  }
  if (!schema) {
    return false;
  }
  if (schema.type === "object" || schema.type === "array") {
    return true;
  }
  return Boolean(schema.properties || schema.items || schema.additionalProperties);
}

function childCount(value, schema) {
  if (Array.isArray(value)) {
    return value.length;
  }
  if (isPlainObject(value)) {
    return Object.keys(value).length;
  }
  if (schema && schema.type === "array") {
    return 0;
  }
  return 0;
}

function preview(value, schema, path) {
  if (path && isSecretPath(path)) {
    return T("views.remote.ui.masked", "已脱敏");
  }
  if (value === null || value === undefined) {
    return "—";
  }
  if (Array.isArray(value)) {
    return T("views.remote.ui.item_count", "{n} 项").replace(
      "{n}",
      () => String(value.length),
    );
  }
  if (isPlainObject(value)) {
    for (const key of ["ModelName", "Short", "Name", "Text", "Desc", "title"]) {
      const item = value[key];
      if (typeof item === "string" && item.trim()) {
        return truncate(item.trim(), 42);
      }
    }
    return T("views.remote.ui.field_count", "{n} 个字段").replace(
      "{n}",
      () => String(Object.keys(value).length),
    );
  }
  if (typeof value === "boolean") {
    return value
      ? T("views.remote.ui.on", "开启")
      : T("views.remote.ui.off", "关闭");
  }
  return truncate(String(value), 42) || T("views.remote.ui.empty", "空");
}

function shortValue(value) {
  if (value === undefined) {
    return "—";
  }
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return value === "" ? T("views.remote.ui.empty", "空") : truncate(value, 80);
  }
  if (typeof value === "object") {
    return truncate(JSON.stringify(value), 80);
  }
  return String(value);
}

function truncate(text, limit) {
  const raw = String(text);
  return raw.length > limit ? `${raw.slice(0, limit - 1)}…` : raw;
}

function schemaPathKnown(path) {
  let node = state.schema;
  for (const item of path) {
    if (!node || typeof node !== "object") {
      return false;
    }
    if (typeof item === "number") {
      const items = node.items;
      if (Array.isArray(items)) {
        if (item < 0 || item >= items.length) {
          return false;
        }
        node = items[item];
        continue;
      }
      if (items && typeof items === "object") {
        node = items;
        continue;
      }
      return false;
    }

    const key = String(item);
    const properties = node.properties;
    if (properties && hasOwn(properties, key)) {
      node = properties[key];
      continue;
    }

    const patterns = node.patternProperties;
    if (patterns && typeof patterns === "object") {
      let matched = false;
      for (const [pattern, child] of Object.entries(patterns)) {
        try {
          if (new RegExp(pattern).test(key)) {
            node = child;
            matched = true;
            break;
          }
        } catch {
          // 忽略不合法的正则。
        }
      }
      if (matched) {
        continue;
      }
    }

    const additional = node.additionalProperties;
    if (
      additional &&
      typeof additional === "object" &&
      Object.keys(additional).length
    ) {
      node = additional;
      continue;
    }
    return false;
  }
  return true;
}

function isSecretPath(path) {
  if (!path.length || !schemaPathKnown(path)) {
    return false;
  }
  const last = path[path.length - 1];
  if (typeof last === "number") {
    return false;
  }
  const lowered = String(last).toLowerCase();
  return SECRET_HINTS.some((hint) => lowered.includes(hint));
}

function isUrlPath(path) {
  if (!path.length || !schemaPathKnown(path)) {
    return false;
  }
  const last = path[path.length - 1];
  if (typeof last === "number") {
    return false;
  }
  const name = String(last);
  const lowered = name.toLowerCase();
  if (URL_SUFFIXES.some((suffix) => lowered.endsWith(suffix))) {
    return true;
  }
  return path.length >= 2 && path[path.length - 2] === "Python" && name === "Source";
}

function isReadOnlyPath(path) {
  return (
    path.length === 1 && (path[0] === "Version" || path[0] === "$schema")
  );
}

function isRestartPath(path) {
  const dotted = path.join(".");
  const restartPaths =
    (state.meta && state.meta.restart_paths) || [];
  return restartPaths.includes(dotted);
}

function isMultilinePath(path) {
  const key = String(path[path.length - 1] || "").toLowerCase();
  return [
    "agentprompt",
    "systemprompt",
    "prompt",
    "text",
    "description",
    "desc",
    "template",
  ].includes(key);
}

function looksNumericMap(path, schema) {
  const last = String(path[path.length - 1] || "").toLowerCase();
  if (last === "models") {
    return true;
  }
  const extra = schema && schema.additionalProperties;
  return Boolean(
    extra &&
      isPlainObject(extra) &&
      extra.properties &&
      (extra.properties.ModelName || extra.properties.ModelID),
  );
}

function nextNumericKey(keys) {
  const numbers = keys
    .map((key) => Number(key))
    .filter((number) => Number.isInteger(number) && number >= 0);
  return numbers.length ? Math.max(...numbers) + 1 : 0;
}

function defaultForSchema(schema) {
  if (!schema || typeof schema !== "object") {
    return null;
  }
  if (Object.prototype.hasOwnProperty.call(schema, "default")) {
    return deepClone(schema.default);
  }
  if (schema.type === "object") {
    if (schema.properties) {
      const result = {};
      for (const [key, child] of Object.entries(schema.properties)) {
        result[key] = defaultForSchema(child);
      }
      return result;
    }
    return {};
  }
  if (schema.type === "array") {
    return [];
  }
  if (schema.type === "string") {
    return "";
  }
  if (schema.type === "integer" || schema.type === "number") {
    return 0;
  }
  if (schema.type === "boolean") {
    return false;
  }
  return null;
}

function inferSchema(value) {
  if (Array.isArray(value)) {
    return {
      type: "array",
      items: value.length ? inferSchema(value[0]) : { type: "$unknown" },
    };
  }
  if (isPlainObject(value)) {
    return { type: "$unknown" };
  }
  if (typeof value === "string") {
    return { type: "string" };
  }
  if (typeof value === "number") {
    return { type: Number.isInteger(value) ? "integer" : "number" };
  }
  if (typeof value === "boolean") {
    return { type: "boolean" };
  }
  return { type: "$unknown" };
}

function getAt(root, path) {
  let node = root;
  for (const key of path) {
    if (node === null || node === undefined || typeof node !== "object") {
      return undefined;
    }
    node = node[key];
  }
  return node;
}

function setAt(root, path, value) {
  if (!path.length) {
    return value;
  }
  let node = root;
  for (let index = 0; index < path.length - 1; index += 1) {
    const key = path[index];
    if (!isPlainObject(node[key]) && !Array.isArray(node[key])) {
      node[key] = typeof path[index + 1] === "number" ? [] : {};
    }
    node = node[key];
  }
  node[path[path.length - 1]] = value;
  return root;
}

function deleteAt(root, path) {
  if (!path.length) {
    return;
  }
  const parent = getAt(root, path.slice(0, -1));
  const last = path[path.length - 1];
  if (Array.isArray(parent) && typeof last === "number") {
    parent.splice(last, 1);
  } else if (isPlainObject(parent)) {
    delete parent[last];
  }
}

function deepClone(value) {
  if (value === undefined) {
    return undefined;
  }
  if (typeof structuredClone === "function") {
    try {
      return structuredClone(value);
    } catch {
      // 退回 JSON 克隆。
    }
  }
  return JSON.parse(JSON.stringify(value));
}

function deepEqual(left, right) {
  if (left === right) {
    return true;
  }
  if (
    left === null ||
    right === null ||
    left === undefined ||
    right === undefined
  ) {
    return false;
  }
  if (typeof left !== typeof right) {
    return false;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false;
    }
    return left.every((item, index) => deepEqual(item, right[index]));
  }
  if (typeof left === "object") {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    if (leftKeys.length !== rightKeys.length) {
      return false;
    }
    return leftKeys.every(
      (key) => hasOwn(right, key) && deepEqual(left[key], right[key]),
    );
  }
  return false;
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasOwn(object, key) {
  return Boolean(
    object && Object.prototype.hasOwnProperty.call(object, key),
  );
}

// ── 事件 ──────────────────────────────────────────────────────────────
function bindEvents() {
  els.save.addEventListener("click", () => {
    void saveConfig();
  });
  els.reload.addEventListener("click", () => {
    void reloadFromRemote();
  });
  els.remoteReload.addEventListener("click", () => {
    void reloadRemoteConfig();
  });
  els.expandAll.addEventListener("click", () => {
    expandAllPaths();
  });
  els.collapseAll.addEventListener("click", () => {
    collapseAllPaths();
  });
  els.modeTree.addEventListener("click", () => {
    setMode("tree");
  });
  els.modeJson.addEventListener("click", () => {
    setMode("json");
  });
  els.applyJson.addEventListener("click", () => {
    applyJson();
  });
  els.formatJson.addEventListener("click", () => {
    formatJson();
  });
  els.syncJson.addEventListener("click", () => {
    syncJsonFromDraft();
  });
  els.jsonEditor.addEventListener("input", () => {
    state.jsonDirty = true;
    clearJsonError();
    refreshDirtyMarks();
  });
  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      void saveConfig();
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.dirtyCount || state.jsonDirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}
