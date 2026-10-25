/**
 * 0528 v2 配置面板。
 *
 * 页面只负责「画树 / 收集改动 / 调用两个接口」，字段元数据、校验与脱敏都在
 * 后端的 panel.py 里。分组使用原生 <details>，天然支持展开/收起与键盘操作；
 * 语言切换时先抓取未保存的输入，再用新语言重画，避免丢掉用户正在改的值。
 */

const state = {
  config: null,
  dirty: new Map(),
  controls: new Map(),
  expanded: new Set(),
  expandedReady: false,
  busy: false,
  reloadWarning: false,
  hookButtons: [],
};

const els = {};
let bridge = null;

boot().catch((error) => {
  renderFatal(error);
});

async function boot() {
  els.tree = document.getElementById("tree");
  els.banner = document.getElementById("banner");
  els.bannerText = document.getElementById("banner-text");
  els.status = document.getElementById("status");
  els.version = document.getElementById("version");
  els.save = document.getElementById("save");
  els.reload = document.getElementById("reload");
  els.expandAll = document.getElementById("expand-all");
  els.collapseAll = document.getElementById("collapse-all");

  bridge = await resolveBridge();
  await withTimeout(bridge.ready(), 6000, "bridge_timeout");
  applyStaticI18n();
  bindEvents();

  bridge.onContext(() => {
    captureDirty();
    applyStaticI18n();
    renderTree();
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
  document.title = T("views.settings.title", "配置面板");
  for (const node of document.querySelectorAll("[data-i18n]")) {
    const key = node.getAttribute("data-i18n");
    const fallback =
      node.getAttribute("data-fallback") || (node.textContent || "").trim();
    node.textContent = T(key, fallback);
  }
}

function groupTitle(group) {
  return T(`views.settings.groups.${group.key}.title`, group.title || group.key);
}

function groupDescription(group) {
  return T(
    `views.settings.groups.${group.key}.description`,
    group.description || "",
  );
}

function fieldTitle(field) {
  return T(`config.${field.key}.description`, field.title || field.key);
}

function fieldHint(field) {
  return T(`config.${field.key}.hint`, field.hint || "");
}

function onText() {
  return T("views.settings.ui.on", "开启");
}

function offText() {
  return T("views.settings.ui.off", "关闭");
}

function asBool(value) {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    return value !== 0;
  }
  if (typeof value === "string") {
    return ["1", "true", "yes", "on", "y", "t"].includes(
      value.trim().toLowerCase(),
    );
  }
  return Boolean(value);
}

// ── 加载 / 保存 ───────────────────────────────────────────────────────
async function loadConfig() {
  setBusy(true);
  setStatus(T("views.settings.ui.loading", "正在读取配置…"));
  try {
    const data = await bridge.apiGet("panel/config");
    if (!data || typeof data !== "object" || !Array.isArray(data.groups)) {
      throw new Error("bad_response");
    }
    state.config = data;
    state.dirty.clear();
    state.reloadWarning = false;
    renderTree();
    setStatus("");
  } catch (error) {
    renderLoadError(error);
  } finally {
    setBusy(false);
    updateDirtyUi();
  }
}

async function saveConfig() {
  if (state.busy) {
    return;
  }
  captureDirty();
  if (state.dirty.size === 0) {
    setStatus(T("views.settings.ui.no_changes", "没有需要保存的改动。"));
    return;
  }

  const updates = Object.fromEntries(state.dirty);
  const pending = state.dirty.size;
  setBusy(true);
  setStatus(T("views.settings.ui.saving", "正在保存…"));
  try {
    const result = await bridge.apiPost("panel/config", { updates });
    if (!result || typeof result !== "object" || !result.config) {
      throw new Error("bad_response");
    }
    state.config = result.config;
    state.dirty.clear();
    state.reloadWarning =
      state.reloadWarning || Boolean(result.reload_required);
    renderTree();
    const saved = Array.isArray(result.saved) ? result.saved.length : pending;
    if (saved === 0) {
      setStatus(T("views.settings.ui.no_changes", "没有需要保存的改动。"));
    } else {
      setStatus(
        T("views.settings.ui.saved", "已保存 {n} 项。").replace(
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
    updateDirtyUi();
  }
}

// ── 渲染 ──────────────────────────────────────────────────────────────
function renderTree() {
  if (!state.config) {
    return;
  }

  state.controls.clear();
  state.hookButtons = [];
  els.tree.replaceChildren();
  els.tree.setAttribute("aria-busy", "false");

  els.version.textContent = state.config.version || "";
  els.version.hidden = !state.config.version;

  if (!state.expandedReady) {
    for (const group of state.config.groups) {
      state.expanded.add(group.key);
    }
    state.expandedReady = true;
  }

  for (const group of state.config.groups) {
    els.tree.append(renderGroup(group));
  }
  if (state.config.groups.length === 0) {
    els.tree.append(emptyNote(T("views.settings.ui.empty", "没有可编辑的配置项。")));
  }

  refreshBanner();
  updateDirtyUi();
}

function renderGroup(group) {
  const details = document.createElement("details");
  details.className = "group";
  details.open = state.expanded.has(group.key);

  const summary = document.createElement("summary");
  const chevron = document.createElement("span");
  chevron.className = "chevron";
  chevron.setAttribute("aria-hidden", "true");
  chevron.textContent = "›";

  const summaryText = document.createElement("div");
  summaryText.className = "summary-text";
  const summaryTitle = document.createElement("div");
  summaryTitle.className = "summary-title";
  const title = document.createElement("span");
  title.textContent = groupTitle(group);
  const count = document.createElement("span");
  count.className = "count";
  count.textContent = String(group.fields.length);
  summaryTitle.append(title, count);

  const description = document.createElement("div");
  description.className = "summary-desc";
  description.textContent = groupDescription(group);

  summaryText.append(summaryTitle, description);
  summary.append(chevron, summaryText);
  details.append(summary);

  const body = document.createElement("div");
  body.className = "group-body";
  for (const field of group.fields) {
    body.append(renderField(field));
  }
  if (group.key === "wakeup") {
    const actions = renderHookActions();
    if (actions) {
      body.append(actions);
    }
  }
  if (group.fields.length === 0) {
    body.append(emptyNote(T("views.settings.ui.empty_group", "这个分组还没有配置项。")));
  }
  details.append(body);

  details.addEventListener("toggle", () => {
    if (details.open) {
      state.expanded.add(group.key);
    } else {
      state.expanded.delete(group.key);
    }
  });

  return details;
}

function renderField(field) {
  const row = document.createElement("div");
  row.className = "field";
  row.dataset.key = field.key;

  const initial = state.dirty.has(field.key)
    ? state.dirty.get(field.key)
    : field.value;

  const head = document.createElement("div");
  head.className = "field-head";

  const dirtyDot = document.createElement("span");
  dirtyDot.className = "dirty-dot";
  dirtyDot.setAttribute("aria-hidden", "true");

  const label = document.createElement("label");
  label.className = "field-title";
  label.htmlFor = `f_${field.key}`;
  label.textContent = fieldTitle(field);

  head.append(dirtyDot, label);

  if (field.requires_reload) {
    head.append(
      badge(
        "reload",
        T("views.settings.ui.badge_reload", "需重载"),
        T(
          "views.settings.ui.badge_reload_hint",
          "保存后需要重载插件才生效。",
        ),
      ),
    );
  }
  if (field.overridden) {
    head.append(
      badge(
        "override",
        T("views.settings.ui.badge_override", "面板覆盖中"),
        T(
          "views.settings.ui.badge_override_hint",
          "执行器面板里的同名字段优先；要让这里生效，请把面板里的值留空。",
        ),
      ),
    );
  }
  if (field.mask) {
    head.append(
      badge(
        "masked",
        T("views.settings.ui.badge_masked", "敏感字段"),
        T(
          "views.settings.ui.masked_hint",
          "*** 表示保留原值；只改主机、路径或其它参数时不会动到凭证。",
        ),
      ),
    );
  }

  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "btn ghost small";
  reset.textContent = T("views.settings.ui.reset_default", "恢复默认");
  reset.addEventListener("click", () => setFieldValue(field, field.default));
  head.append(reset);

  const control = buildControl(field, initial, row);

  const hint = document.createElement("div");
  hint.className = "field-hint";
  const hintText = fieldHint(field);
  if (hintText) {
    hint.textContent = hintText;
  }
  if (field.mask) {
    const note = document.createElement("div");
    note.className = "hint";
    note.textContent = T(
      "views.settings.ui.masked_hint",
      "*** 表示保留原值；只改主机、路径或其它参数时不会动到凭证。",
    );
    hint.append(note);
  }

  const meta = document.createElement("div");
  meta.className = "field-meta";
  meta.append(
    metaValue(
      T("views.settings.ui.default_value", "默认"),
      displayValue(field, field.default),
    ),
  );
  if (
    field.overridden ||
    String(field.effective) !== String(field.value)
  ) {
    meta.append(
      metaValue(
        T("views.settings.ui.effective_value", "生效"),
        displayValue(field, field.effective),
      ),
    );
  }
  if (field.overridden) {
    meta.append(
      metaValue(
        T("views.settings.ui.panel_value", "执行器面板"),
        displayValue(field, field.panel_value),
      ),
    );
  }

  row.append(head, control, hint, meta);

  const controlState = state.controls.get(field.key);
  if (controlState) {
    controlState.row = row;
  }
  return row;
}

// ── alkaid0 cron 钩子：手动按钮（保存配置不会自动同步）──────────────
function hookState() {
  const hook = state.config && state.config.hook;
  return hook && typeof hook === "object" ? hook : null;
}

function renderHookActions() {
  const hook = hookState();
  if (!hook) {
    return null;
  }

  const row = document.createElement("div");
  row.className = "field hook-actions";

  const head = document.createElement("div");
  head.className = "field-head";
  const title = document.createElement("span");
  title.className = "field-title";
  title.textContent = T(
    "views.settings.hook.title",
    "alkaid0 的 cron 唤醒钩子",
  );
  head.append(title);
  row.append(head);

  const hint = document.createElement("div");
  hint.className = "field-hint";
  hint.textContent = T(
    "views.settings.hook.description",
    "按钮才会改动 alkaid0 的配置（保存上面的设置不会自动同步）。",
  );
  row.append(hint);

  const control = document.createElement("div");
  control.className = "field-control";

  const install = document.createElement("button");
  install.type = "button";
  install.className = "btn primary small";
  install.textContent = T("views.settings.hook.install", "写入 / 更新钩子");
  install.disabled = state.busy || hook.usable === false;
  install.addEventListener("click", () => {
    void runHookAction("install");
  });

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn ghost small";
  remove.textContent = T("views.settings.hook.remove", "移除钩子");
  remove.disabled = state.busy || hook.installed === false;
  remove.addEventListener("click", () => {
    void runHookAction("remove");
  });

  control.append(install, remove);
  row.append(control);
  state.hookButtons = [install, remove];

  const status = document.createElement("div");
  status.className = "field-meta";
  const note =
    hook.note ||
    (hook.installed === true
      ? T("views.settings.hook.installed", "已写入")
      : hook.installed === false
        ? T("views.settings.hook.removed", "未写入")
        : T("views.settings.hook.never", "还没手动同步过"));
  const parts = [];
  if (hook.endpoint) {
    parts.push(
      `${T("views.settings.hook.endpoint", "唤醒端点")}: ${hook.endpoint}`,
    );
  }
  parts.push(note);
  status.textContent = parts.join(" · ");
  row.append(status);

  if (hook.usable === false) {
    const warning = document.createElement("div");
    warning.className = "field-hint";
    warning.textContent = T(
      "views.settings.hook.not_ready",
      "唤醒钩子未启用或没配 apikey：先把上面的开关和 apikey 填好并保存，再点写入。",
    );
    row.append(warning);
  }

  return row;
}

async function runHookAction(action) {
  if (state.busy) {
    return;
  }
  setBusy(true);
  setStatus(T("views.settings.hook.busy", "正在同步…"));
  try {
    const result = await bridge.apiPost("panel/hook", { action });
    if (!result || typeof result !== "object") {
      throw new Error("bad_response");
    }
    const previous = hookState() || {};
    state.config.hook = {
      ...previous,
      installed:
        typeof result.installed === "boolean"
          ? result.installed
          : previous.installed,
      note: String(result.note || ""),
      endpoint: String(result.endpoint || previous.endpoint || ""),
    };
    const ok = result.ok !== false;
    const done =
      action === "install"
        ? T(
            "views.settings.hook.done_install",
            "已写入 alkaid0 的 OnCronjobWakeup。",
          )
        : T(
            "views.settings.hook.done_remove",
            "已从 alkaid0 移除本插件的钩子。",
          );
    setStatus(
      ok
        ? result.note || done
        : result.note || T("views.settings.hook.failed", "同步失败，详情见日志。"),
      ok ? "ok" : "error",
    );
  } catch (error) {
    setStatus(formatError(error), "error");
  } finally {
    setBusy(false);
    if (state.config) {
      renderTree();
    }
  }
}

function buildControl(field, initial, row) {
  const wrap = document.createElement("div");
  wrap.className = "field-control";

  if (field.type === "bool") {
    const switchLabel = document.createElement("label");
    switchLabel.className = "switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = `f_${field.key}`;
    input.dataset.key = field.key;
    input.checked = asBool(initial);

    const track = document.createElement("span");
    track.className = "switch-track";
    const text = document.createElement("span");
    text.className = "switch-label";
    text.textContent = input.checked ? onText() : offText();

    switchLabel.append(input, track, text);
    wrap.append(switchLabel);

    const controlState = {
      field,
      input,
      switchLabel: text,
      row,
    };
    state.controls.set(field.key, controlState);
    input.addEventListener("change", () => onControlChanged(controlState));
    return wrap;
  }

  const input = document.createElement(field.multiline ? "textarea" : "input");
  input.id = `f_${field.key}`;
  input.dataset.key = field.key;
  input.className = "input";
  input.autocomplete = "off";
  input.spellcheck = false;

  if (field.type === "int" || field.type === "float") {
    input.type = "number";
    input.step = field.type === "int" ? "1" : "any";
    if (field.min !== null && field.min !== undefined) {
      input.min = String(field.min);
    }
    if (field.max !== null && field.max !== undefined) {
      input.max = String(field.max);
    }
    input.classList.add("mono");
  } else if (field.mask === "url") {
    input.classList.add("mono");
  } else if (field.mask === "secret") {
    input.type = "password";
  }

  input.value = initial === null || initial === undefined ? "" : String(initial);

  const controlState = {
    field,
    input,
    switchLabel: null,
    row,
  };
  state.controls.set(field.key, controlState);
  input.addEventListener("input", () => onControlChanged(controlState));

  if (field.mask === "secret") {
    const inputWrap = document.createElement("div");
    inputWrap.className = "input-wrap";
    const reveal = document.createElement("button");
    reveal.type = "button";
    reveal.className = "reveal";
    reveal.textContent = T("views.settings.ui.show", "显示");
    reveal.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      reveal.textContent = show
        ? T("views.settings.ui.hide", "隐藏")
        : T("views.settings.ui.show", "显示");
    });
    inputWrap.append(input, reveal);
    wrap.append(inputWrap);
  } else {
    wrap.append(input);
  }

  return wrap;
}

function onControlChanged(controlState) {
  const value = readControl(controlState);
  const key = controlState.field.key;
  if (valuesEqual(controlState.field, value, controlState.field.value)) {
    state.dirty.delete(key);
  } else {
    state.dirty.set(key, value);
  }
  if (controlState.switchLabel) {
    controlState.switchLabel.textContent = value ? onText() : offText();
  }
  if (controlState.row) {
    controlState.row.classList.toggle("is-dirty", state.dirty.has(key));
  }
  updateDirtyUi();
  updateUnsavedStatus();
}

function captureDirty() {
  for (const [key, controlState] of state.controls) {
    const value = readControl(controlState);
    if (valuesEqual(controlState.field, value, controlState.field.value)) {
      state.dirty.delete(key);
    } else {
      state.dirty.set(key, value);
    }
  }
}

function readControl(controlState) {
  if (controlState.field.type === "bool") {
    return Boolean(controlState.input.checked);
  }
  return controlState.input.value;
}

function valuesEqual(field, left, right) {
  if (field.type === "bool") {
    return asBool(left) === asBool(right);
  }
  if (field.type === "int" || field.type === "float") {
    const leftText = String(left ?? "").trim();
    const rightText = String(right ?? "").trim();
    if (!leftText || !rightText) {
      return leftText === rightText;
    }
    const leftNumber = Number(leftText);
    const rightNumber = Number(rightText);
    if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
      return leftNumber === rightNumber;
    }
  }
  return String(left ?? "") === String(right ?? "");
}

function setFieldValue(field, value) {
  const controlState = state.controls.get(field.key);
  if (!controlState) {
    return;
  }
  if (field.type === "bool") {
    controlState.input.checked = asBool(value);
  } else {
    controlState.input.value =
      value === null || value === undefined ? "" : String(value);
  }
  onControlChanged(controlState);
  controlState.input.focus();
}

// ── 状态 / 提示 ───────────────────────────────────────────────────────
function setBusy(busy) {
  state.busy = busy;
  for (const button of [
    els.reload,
    els.expandAll,
    els.collapseAll,
    ...state.hookButtons,
  ]) {
    if (button) {
      button.disabled = busy;
    }
  }
  updateDirtyUi();
}

function updateDirtyUi() {
  if (!els.save) {
    return;
  }
  const count = state.dirty.size;
  els.save.disabled = state.busy || count === 0;
  const base = T("views.settings.ui.save", "保存");
  els.save.textContent = count ? `${base} (${count})` : base;
  for (const [key, controlState] of state.controls) {
    if (controlState.row) {
      controlState.row.classList.toggle("is-dirty", state.dirty.has(key));
    }
  }
}

function updateUnsavedStatus() {
  if (state.busy) {
    return;
  }
  if (state.dirty.size === 0) {
    setStatus("");
    return;
  }
  setStatus(
    T("views.settings.ui.unsaved", "有 {n} 项未保存。").replace(
      "{n}",
      () => String(state.dirty.size),
    ),
    "warning",
  );
}

function setStatus(text, kind = "") {
  if (!els.status) {
    return;
  }
  els.status.textContent = text || "";
  els.status.className = `status${kind ? ` ${kind}` : ""}`;
}

function refreshBanner() {
  if (!els.banner) {
    return;
  }
  if (state.reloadWarning) {
    setBanner(
      T(
        "views.settings.ui.reload_warning",
        "前置插件相关设置已保存，需要重载插件后生效。",
      ),
      "warning",
    );
    return;
  }
  if (hasOverrides()) {
    setBanner(
      T(
        "views.settings.ui.override_banner",
        "执行器面板（AI → Agent 执行方式 → Alkaid0）里的值优先于插件配置；带「面板覆盖中」标记的字段改了也不会生效。",
      ),
      "",
    );
    return;
  }
  hideBanner();
}

function setBanner(text, kind) {
  els.banner.hidden = !text;
  els.banner.className = `banner${kind === "warning" ? " warning" : ""}`;
  els.bannerText.textContent = text || "";
}

function hideBanner() {
  els.banner.hidden = true;
  els.bannerText.textContent = "";
}

function hasOverrides() {
  if (!state.config || !Array.isArray(state.config.groups)) {
    return false;
  }
  return state.config.groups.some((group) =>
    group.fields.some((field) => Boolean(field.overridden)),
  );
}

function renderLoadError(error) {
  state.config = null;
  state.dirty.clear();
  state.controls.clear();
  els.tree.replaceChildren();
  els.tree.setAttribute("aria-busy", "false");
  const note = document.createElement("div");
  note.className = "placeholder";
  note.textContent = formatError(error);
  els.tree.append(note);
  setStatus(formatError(error), "error");
}

function renderFatal(error) {
  const tree = document.getElementById("tree");
  const status = document.getElementById("status");
  const message = formatError(error);
  if (tree) {
    tree.replaceChildren();
    const note = document.createElement("div");
    note.className = "placeholder";
    note.textContent = message;
    tree.append(note);
    tree.setAttribute("aria-busy", "false");
  }
  if (status) {
    status.textContent = message;
    status.className = "status error";
  }
}

function formatError(error) {
  const raw = String((error && error.message) || error || "unknown");
  const separator = raw.indexOf(":");
  const code = separator >= 0 ? raw.slice(0, separator) : raw;
  const key = separator >= 0 ? raw.slice(separator + 1) : "";
  const field = key ? findField(key) : null;
  const label = field ? fieldTitle(field) : key;

  const defaults = {
    bad_payload: "请求格式不正确。",
    bad_response: "配置接口返回了无法识别的数据。",
    no_changes: "没有需要保存的改动。",
    unknown_field: "不认识的配置项：{field}",
    invalid_type: "「{field}」的类型不正确。",
    out_of_range: "「{field}」超出允许范围。",
    too_long: "「{field}」太长。",
    invalid_url: "「{field}」不是合法的 WebSocket 地址（需要 ws:// 或 wss://）。",
    unresolved_mask: "「{field}」里的 *** 没有可还原的原值，请把完整值填进来。",
    not_writable: "当前插件配置不可写（缺少保存接口）。",
    save_failed: "写入配置文件失败，请查看 AstrBot 日志。",
    bridge_unavailable: "找不到 AstrBot 插件页 bridge，请从插件页打开这个视图。",
    bridge_timeout: "连接 Dashboard 超时，请从插件页重新打开这个视图。",
    unknown: "操作失败：{detail}",
  };

  let template = T(`views.settings.errors.${code}`, defaults[code] || "");
  if (!template) {
    template = defaults.unknown;
  }
  return template
    .replace("{field}", () => label || "")
    .replace("{detail}", () => raw);
}

function findField(key) {
  if (!state.config || !Array.isArray(state.config.groups)) {
    return null;
  }
  for (const group of state.config.groups) {
    for (const field of group.fields) {
      if (field.key === key) {
        return field;
      }
    }
  }
  return null;
}

// ── 小工具 ────────────────────────────────────────────────────────────
function badge(kind, text, title) {
  const node = document.createElement("span");
  node.className = `badge ${kind}`;
  node.textContent = text;
  if (title) {
    node.title = title;
  }
  return node;
}

function metaValue(label, value) {
  const node = document.createElement("span");
  node.textContent = `${label}: `;
  const code = document.createElement("code");
  code.textContent = value;
  node.append(code);
  return node;
}

function displayValue(field, value) {
  if (field.type === "bool") {
    return asBool(value) ? onText() : offText();
  }
  if (value === null || value === undefined || String(value) === "") {
    return "—";
  }
  return String(value);
}

function emptyNote(text) {
  const node = document.createElement("div");
  node.className = "empty-note";
  node.textContent = text;
  return node;
}

// ── 事件 ──────────────────────────────────────────────────────────────
function bindEvents() {
  els.save.addEventListener("click", () => {
    void saveConfig();
  });
  els.reload.addEventListener("click", () => {
    void loadConfig();
  });
  els.expandAll.addEventListener("click", () => {
    if (state.config) {
      for (const group of state.config.groups) {
        state.expanded.add(group.key);
      }
    }
    state.expandedReady = true;
    renderTree();
  });
  els.collapseAll.addEventListener("click", () => {
    state.expanded.clear();
    state.expandedReady = true;
    renderTree();
  });
  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      void saveConfig();
    }
  });
}
