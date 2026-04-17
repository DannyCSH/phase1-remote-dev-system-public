import "./style.css";

import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

type AccessMode = "workspace_only" | "custom_roots" | "full_computer";
type MaskableFieldId =
  | "qq-app-id"
  | "qq-app-secret"
  | "qq-open-id"
  | "minimax-api-key"
  | "model";

interface ConfigForm {
  qqAppId: string;
  qqAppSecret: string;
  qqOpenId: string;
  minimaxApiKey: string;
  model: string;
  msgFormat: string;
  qqEnabled: boolean;
  autostartEnabled: boolean;
  accessMode: AccessMode;
  customRoots: string[];
}

interface LoadedConfig {
  sourcePath: string;
  localExists: boolean;
  projectRoot: string;
  systemRoots: string[];
  form: ConfigForm;
}

interface CommandResult {
  ok: boolean;
  summary: string;
  stdout: string;
  stderr: string;
  data?: unknown;
}

interface RuntimeStatus {
  gateway?: { state?: string; pid?: number | string };
  worker?: { state?: string; phase?: string };
  admin_relay?: { state?: string };
  tooling?: {
    nanobot?: { exists?: boolean };
    claude?: { exists?: boolean };
    codex?: { exists?: boolean };
  };
}

const MASKABLE_FIELDS: MaskableFieldId[] = [
  "qq-app-id",
  "qq-app-secret",
  "qq-open-id",
  "minimax-api-key",
  "model",
];

const EYE_ICON = `
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M2.2 12c2.4-4.3 5.7-6.5 9.8-6.5s7.4 2.2 9.8 6.5c-2.4 4.3-5.7 6.5-9.8 6.5S4.6 16.3 2.2 12Z"></path>
    <circle cx="12" cy="12" r="3.3"></circle>
  </svg>
`;

const state: {
  config: LoadedConfig | null;
  busy: string | null;
  lastOutput: string;
  status: RuntimeStatus | null;
  reveal: Record<MaskableFieldId, boolean>;
} = {
  config: null,
  busy: null,
  lastOutput: "准备就绪，先读取当前配置。",
  status: null,
  reveal: {
    "qq-app-id": false,
    "qq-app-secret": false,
    "qq-open-id": false,
    "minimax-api-key": false,
    model: false,
  },
};

const app = document.querySelector<HTMLDivElement>("#app");

if (!app) {
  throw new Error("App root not found.");
}

app.innerHTML = `
  <div class="shell">
    <header class="hero">
      <div>
        <p class="eyebrow">Phase 1 Desktop Configurator</p>
        <h1>先把配置填好，再启动整套链路</h1>
        <p class="hero-copy">
          这个窗口只负责三件事：读取当前配置、保存本地密钥与权限范围、检查并启动 Phase 1。
          后续给客户发布下载版时，固定入口就应该是这个桌面窗口或它的快捷方式，而不是终端命令。
        </p>
      </div>
      <div class="hero-meta">
        <div class="meta-card">
          <span class="meta-label">配置来源</span>
          <strong id="meta-config-source">读取中</strong>
        </div>
        <div class="meta-card">
          <span class="meta-label">项目根目录</span>
          <strong id="meta-project-root">读取中</strong>
        </div>
        <div class="meta-card">
          <span class="meta-label">固定打开方式</span>
          <strong id="meta-launch-entry">双击 Open-Phase1Configurator.vbs</strong>
        </div>
      </div>
    </header>

    <main class="layout">
      <section class="panel panel-form">
        <div class="panel-head">
          <div>
            <p class="panel-kicker">基本配置</p>
            <h2>账号、模型与访问范围</h2>
          </div>
          <div class="chip-row">
            <span class="chip" id="chip-local-config">本地配置未检测</span>
            <span class="chip" id="chip-access-mode">权限未知</span>
          </div>
        </div>

        <div class="form-grid">
          <label class="field">
            <span class="field-label">QQ App ID</span>
            <div class="input-shell">
              <input id="qq-app-id" type="password" placeholder="QQ 开放平台 -> 机器人详情页 -> App ID" />
              <button id="toggle-qq-app-id" class="eye-button" data-target="qq-app-id" type="button" aria-label="显示 QQ App ID" title="显示">${EYE_ICON}</button>
            </div>
          </label>

          <label class="field">
            <span class="field-label">QQ App Secret</span>
            <div class="input-shell">
              <input id="qq-app-secret" type="password" placeholder="QQ 开放平台 -> 凭证页 -> App Secret" />
              <button id="toggle-qq-app-secret" class="eye-button" data-target="qq-app-secret" type="button" aria-label="显示 QQ App Secret" title="显示">${EYE_ICON}</button>
            </div>
          </label>

          <label class="field">
            <span class="field-label">授权 QQ OpenID</span>
            <div class="input-shell">
              <input id="qq-open-id" type="password" placeholder="先给机器人发一条消息，再从授权聊天或状态脚本里拿 OpenID" />
              <button id="toggle-qq-open-id" class="eye-button" data-target="qq-open-id" type="button" aria-label="显示 QQ OpenID" title="显示">${EYE_ICON}</button>
            </div>
          </label>

          <label class="field">
            <span class="field-label">MiniMax API Key</span>
            <div class="input-shell">
              <input id="minimax-api-key" type="password" placeholder="MiniMax 控制台 -> API Keys -> 创建并复制 Key" />
              <button id="toggle-minimax-api-key" class="eye-button" data-target="minimax-api-key" type="button" aria-label="显示 MiniMax API Key" title="显示">${EYE_ICON}</button>
            </div>
          </label>

          <label class="field">
            <span class="field-label">默认模型</span>
            <div class="input-shell">
              <input id="model" type="password" placeholder="可留空；默认 MiniMax-M2.7，也可改成你自己的模型名" />
              <button id="toggle-model" class="eye-button" data-target="model" type="button" aria-label="显示默认模型" title="显示">${EYE_ICON}</button>
            </div>
          </label>

          <label class="field">
            <span class="field-label">QQ 消息格式</span>
            <select id="msg-format">
              <option value="plain">plain</option>
              <option value="markdown">markdown</option>
            </select>
          </label>
        </div>

        <div class="toggle-row">
          <label class="toggle">
            <input id="qq-enabled" type="checkbox" />
            <span>启用 QQ 通道</span>
          </label>
          <label class="toggle">
            <input id="autostart-enabled" type="checkbox" />
            <span>保留开机自启配置</span>
          </label>
        </div>

        <section class="access-section">
          <div class="section-copy">
            <p class="panel-kicker">访问范围</p>
            <h3>默认先保守，再按你的需要放大</h3>
            <p>
              你现在可以在三档之间切换：只限工作区、自定义目录、整个计算机。
              如果你要让授权 QQ 能搜整机或从整机回传文件，就选整机模式。
            </p>
          </div>

          <div class="access-modes">
            <label class="mode-card">
              <input type="radio" name="access-mode" value="workspace_only" checked />
              <span class="mode-title">工作区模式</span>
              <span class="mode-copy">只允许仓库与 workspace，适合公开版默认安装。</span>
            </label>
            <label class="mode-card">
              <input type="radio" name="access-mode" value="custom_roots" />
              <span class="mode-title">自定义目录模式</span>
              <span class="mode-copy">把你选中的目录同步放大到项目、搜索、附件与回传权限。</span>
            </label>
            <label class="mode-card">
              <input type="radio" name="access-mode" value="full_computer" />
              <span class="mode-title">整个计算机模式</span>
              <span class="mode-copy">把当前机器上全部本地磁盘加入可搜索、可回传的授权范围。</span>
            </label>
          </div>

          <div class="roots-box" id="roots-box" data-mode="workspace_only">
            <div class="roots-head">
              <div>
                <h4 id="roots-title">额外授权目录</h4>
                <p id="roots-description">工作区模式下不额外放开其它目录。</p>
              </div>
              <button id="add-root" class="button button-ghost" type="button">选择目录</button>
            </div>
            <p id="roots-mode-note" class="roots-mode-note"></p>
            <ul id="root-list" class="root-list"></ul>
          </div>
        </section>

        <div class="actions">
          <button id="reload-config" class="button button-ghost" type="button">重新读取配置</button>
          <button id="save-config" class="button button-primary" type="button">保存到 nanobot.local.json</button>
        </div>
      </section>

      <aside class="panel panel-side">
        <div class="panel-head">
          <div>
            <p class="panel-kicker">运行状态</p>
            <h2>环境、自检与控制</h2>
          </div>
        </div>

        <div class="status-stack">
          <div class="status-card" data-state="loading">
            <span>Gateway</span>
            <strong id="status-gateway">读取中</strong>
          </div>
          <div class="status-card" data-state="loading">
            <span>Worker</span>
            <strong id="status-worker">读取中</strong>
          </div>
          <div class="status-card" data-state="loading">
            <span>Admin Relay</span>
            <strong id="status-admin">读取中</strong>
          </div>
          <div class="status-card" data-state="loading">
            <span>工具链</span>
            <strong id="status-tooling">读取中</strong>
          </div>
        </div>

        <div class="actions vertical">
          <button id="check-env" class="button button-secondary" type="button">检查环境</button>
          <button id="refresh-status" class="button button-secondary" type="button">获取状态</button>
          <button id="create-bundle" class="button button-secondary" type="button">生成诊断包</button>
          <button id="start-stack" class="button button-primary" type="button">启动整套链路</button>
          <button id="stop-gateway" class="button button-ghost" type="button">停止 Gateway</button>
        </div>

        <div class="console-box">
          <div class="console-head">
            <h3>输出窗口</h3>
            <span id="busy-badge" class="busy-idle">空闲</span>
          </div>
          <pre id="console-output"></pre>
        </div>
      </aside>
    </main>
  </div>
`;

function byId<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing element: ${id}`);
  }
  return element as T;
}

function updateConsole(text: string): void {
  state.lastOutput = text;
  byId<HTMLPreElement>("console-output").textContent = text;
}

function setBusy(label: string | null): void {
  state.busy = label;
  const badge = byId<HTMLSpanElement>("busy-badge");
  badge.textContent = label ?? "空闲";
  badge.className = label ? "busy-live" : "busy-idle";
  document.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
    if (button.id === "add-root") {
      return;
    }
    button.disabled = Boolean(label);
  });
}

function dedupeRoots(roots: string[]): string[] {
  const seen = new Set<string>();
  const items: string[] = [];
  for (const rawRoot of roots) {
    const root = rawRoot.trim();
    if (!root) {
      continue;
    }
    const key = root.toLowerCase();
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    items.push(root);
  }
  return items;
}

function getAccessMode(): AccessMode {
  const checked = document.querySelector<HTMLInputElement>('input[name="access-mode"]:checked');
  if (checked?.value === "custom_roots") {
    return "custom_roots";
  }
  if (checked?.value === "full_computer") {
    return "full_computer";
  }
  return "workspace_only";
}

function describeAccessMode(mode: AccessMode): string {
  switch (mode) {
    case "custom_roots":
      return "已放大到自定义目录";
    case "full_computer":
      return "已放大到整个计算机";
    default:
      return "工作区安全模式";
  }
}

function getVisibleRoots(config: LoadedConfig): string[] {
  if (config.form.accessMode === "full_computer") {
    return config.systemRoots;
  }
  return config.form.customRoots;
}

function renderRoots(config: LoadedConfig | null): void {
  const list = byId<HTMLUListElement>("root-list");
  const title = byId<HTMLElement>("roots-title");
  const description = byId<HTMLElement>("roots-description");
  const note = byId<HTMLElement>("roots-mode-note");
  const addRootButton = byId<HTMLButtonElement>("add-root");

  list.innerHTML = "";

  if (!config) {
    title.textContent = "额外授权目录";
    description.textContent = "读取配置后再显示。";
    note.textContent = "";
    addRootButton.disabled = true;
    return;
  }

  const mode = config.form.accessMode;
  const visibleRoots = getVisibleRoots(config);

  if (mode === "workspace_only") {
    title.textContent = "额外授权目录";
    description.textContent = "当前只允许仓库、workspace 与 runtime/media，不额外放开其它目录。";
    note.textContent = "这是公开版默认推荐模式。";
    addRootButton.disabled = true;
  } else if (mode === "full_computer") {
    title.textContent = "整机授权磁盘";
    description.textContent = "这个模式会把当前机器上检测到的全部本地磁盘加入项目、搜索、附件与回传权限。";
    note.textContent =
      visibleRoots.length > 0
        ? `当前检测到 ${visibleRoots.length} 个本地磁盘：${visibleRoots.join("、")}`
        : "当前没有检测到可用的本地磁盘。";
    addRootButton.disabled = true;
  } else {
    title.textContent = "自定义根目录";
    description.textContent = "例如简历目录、图片素材库、下载目录。只建议加你明确愿意授权给 QQ 的路径。";
    note.textContent =
      visibleRoots.length > 0
        ? `已添加 ${visibleRoots.length} 个目录。`
        : "还没有添加目录。";
    addRootButton.disabled = false;
  }

  if (visibleRoots.length === 0) {
    const empty = document.createElement("li");
    empty.className = "root-empty";
    empty.textContent =
      mode === "full_computer"
        ? "当前没有检测到本地磁盘。"
        : mode === "workspace_only"
          ? "当前没有额外授权目录。"
          : "还没有添加目录。";
    list.appendChild(empty);
    return;
  }

  visibleRoots.forEach((root, index) => {
    const item = document.createElement("li");
    item.className = "root-item";
    if (mode === "full_computer") {
      item.classList.add("root-item-system");
    }

    const text = document.createElement("span");
    text.textContent = root;
    item.appendChild(text);

    if (mode === "custom_roots") {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "button button-remove";
      remove.textContent = "移除";
      remove.dataset.index = String(index);
      remove.addEventListener("click", () => {
        if (!state.config) {
          return;
        }
        state.config.form.customRoots.splice(index, 1);
        state.config.form.customRoots = dedupeRoots(state.config.form.customRoots);
        renderFromState();
      });
      item.appendChild(remove);
    } else if (mode === "full_computer") {
      const badge = document.createElement("span");
      badge.className = "root-badge";
      badge.textContent = "整机";
      item.appendChild(badge);
    }

    list.appendChild(item);
  });
}

function cardTone(value: string): string {
  const normalized = value.toLowerCase();
  if (!normalized || normalized === "读取中") {
    return "loading";
  }
  if (normalized.includes("running") || normalized.includes("healthy")) {
    return "good";
  }
  if (normalized.includes("idle") || normalized.includes("ready")) {
    return "neutral";
  }
  if (normalized.includes("stopped") || normalized.includes("missing")) {
    return "warn";
  }
  if (normalized.includes("failed") || normalized.includes("error")) {
    return "bad";
  }
  return "neutral";
}

function setStatusCard(id: string, value: string): void {
  const target = byId<HTMLElement>(id);
  target.textContent = value;
  target.closest<HTMLElement>(".status-card")?.setAttribute("data-state", cardTone(value));
}

function updateStatusCards(status: RuntimeStatus | null): void {
  if (!status) {
    setStatusCard("status-gateway", "读取中");
    setStatusCard("status-worker", "读取中");
    setStatusCard("status-admin", "读取中");
    setStatusCard("status-tooling", "读取中");
    return;
  }

  const gateway = status.gateway?.state ?? "未知";
  const workerState = status.worker?.state ?? "未知";
  const workerPhase = status.worker?.phase ?? "";
  const admin = status.admin_relay?.state ?? "未知";
  const toolingBits = [
    status.tooling?.nanobot?.exists ? "NanoBot" : "",
    status.tooling?.claude?.exists ? "Claude" : "",
    status.tooling?.codex?.exists ? "Codex" : "",
  ].filter(Boolean);

  setStatusCard("status-gateway", gateway);
  setStatusCard("status-worker", workerPhase ? `${workerState} / ${workerPhase}` : workerState);
  setStatusCard("status-admin", admin);
  setStatusCard(
    "status-tooling",
    toolingBits.length > 0 ? toolingBits.join(" + ") : "未检测到可用工具",
  );
}

function renderMeta(config: LoadedConfig | null): void {
  byId<HTMLElement>("meta-config-source").textContent = config
    ? config.localExists
      ? "nanobot.local.json"
      : "nanobot.example.json"
    : "未读取";
  byId<HTMLElement>("meta-project-root").textContent = config?.projectRoot ?? "未读取";
  byId<HTMLElement>("chip-local-config").textContent = config?.localExists
    ? "检测到本地配置"
    : "当前使用模板配置";
  byId<HTMLElement>("chip-access-mode").textContent = config
    ? describeAccessMode(config.form.accessMode)
    : "权限未知";

  const launchEntry = byId<HTMLElement>("meta-launch-entry");
  launchEntry.textContent = "双击 Open-Phase1Configurator.vbs";
  if (config) {
    launchEntry.title = `${config.projectRoot}\\Open-Phase1Configurator.vbs`;
  }
}

function syncAccessUi(mode: AccessMode): void {
  document
    .querySelectorAll<HTMLInputElement>('input[name="access-mode"]')
    .forEach((radio) => {
      radio.checked = radio.value === mode;
    });
  byId<HTMLDivElement>("roots-box").dataset.mode = mode;
}

function renderFieldVisibility(): void {
  MASKABLE_FIELDS.forEach((fieldId) => {
    const input = byId<HTMLInputElement>(fieldId);
    const revealed = state.reveal[fieldId];
    input.type = revealed ? "text" : "password";

    const button = byId<HTMLButtonElement>(`toggle-${fieldId}`);
    button.dataset.revealed = String(revealed);
    button.title = revealed ? "隐藏" : "显示";
    button.setAttribute("aria-label", revealed ? "隐藏内容" : "显示内容");
  });
}

function renderFromState(): void {
  const config = state.config;
  renderMeta(config);
  updateStatusCards(state.status);
  renderFieldVisibility();
  renderRoots(config);

  if (!config) {
    return;
  }

  byId<HTMLInputElement>("qq-app-id").value = config.form.qqAppId;
  byId<HTMLInputElement>("qq-app-secret").value = config.form.qqAppSecret;
  byId<HTMLInputElement>("qq-open-id").value = config.form.qqOpenId;
  byId<HTMLInputElement>("minimax-api-key").value = config.form.minimaxApiKey;
  byId<HTMLInputElement>("model").value = config.form.model;
  byId<HTMLSelectElement>("msg-format").value = config.form.msgFormat;
  byId<HTMLInputElement>("qq-enabled").checked = config.form.qqEnabled;
  byId<HTMLInputElement>("autostart-enabled").checked = config.form.autostartEnabled;
  syncAccessUi(config.form.accessMode);
  renderFieldVisibility();
  renderRoots(config);
}

function collectForm(): ConfigForm {
  return {
    qqAppId: byId<HTMLInputElement>("qq-app-id").value.trim(),
    qqAppSecret: byId<HTMLInputElement>("qq-app-secret").value.trim(),
    qqOpenId: byId<HTMLInputElement>("qq-open-id").value.trim(),
    minimaxApiKey: byId<HTMLInputElement>("minimax-api-key").value.trim(),
    model: byId<HTMLInputElement>("model").value.trim(),
    msgFormat: byId<HTMLSelectElement>("msg-format").value,
    qqEnabled: byId<HTMLInputElement>("qq-enabled").checked,
    autostartEnabled: byId<HTMLInputElement>("autostart-enabled").checked,
    accessMode: getAccessMode(),
    customRoots: dedupeRoots(state.config?.form.customRoots ?? []),
  };
}

function formatCommandResult(result: CommandResult): string {
  const parts = [result.summary];
  if (result.stdout.trim()) {
    parts.push("", "[stdout]", result.stdout.trim());
  }
  if (result.stderr.trim()) {
    parts.push("", "[stderr]", result.stderr.trim());
  }
  return parts.join("\n");
}

async function refreshStatus(options?: {
  silentConsole?: boolean;
  showBusy?: boolean;
}): Promise<void> {
  const silentConsole = options?.silentConsole ?? false;
  const showBusy = options?.showBusy ?? true;

  if (showBusy) {
    setBusy("读取状态");
  }

  try {
    const result = await invoke<CommandResult>("get_status");
    if (result.data) {
      state.status = result.data as RuntimeStatus;
      renderFromState();
    }
    if (!silentConsole) {
      updateConsole(formatCommandResult(result));
    }
  } catch (error) {
    if (!silentConsole) {
      updateConsole(`状态刷新失败。\n${String(error)}`);
    }
  } finally {
    if (showBusy) {
      setBusy(null);
    }
  }
}

async function loadConfig(): Promise<void> {
  setBusy("读取配置");
  try {
    const payload = await invoke<LoadedConfig>("load_config");
    state.config = payload;
    renderFromState();
    updateConsole(
      [
        "配置读取成功。",
        `来源: ${payload.localExists ? "config\\\\nanobot.local.json" : "config\\\\nanobot.example.json"}`,
        `项目根目录: ${payload.projectRoot}`,
        `当前模式: ${describeAccessMode(payload.form.accessMode)}`,
      ].join("\n"),
    );
  } catch (error) {
    updateConsole(`读取配置失败。\n${String(error)}`);
  } finally {
    setBusy(null);
  }

  if (state.config) {
    await refreshStatus({ silentConsole: true, showBusy: false });
  }
}

async function saveConfig(): Promise<void> {
  if (!state.config) {
    return;
  }
  setBusy("保存配置");
  try {
    const form = collectForm();
    const payload = await invoke<LoadedConfig>("save_config", { form });
    state.config = payload;
    renderFromState();
    updateConsole(
      [
        "配置已保存到 config\\nanobot.local.json。",
        `权限模式: ${describeAccessMode(form.accessMode)}`,
        `自定义目录数量: ${form.customRoots.length}`,
      ].join("\n"),
    );
  } catch (error) {
    updateConsole(`保存配置失败。\n${String(error)}`);
  } finally {
    setBusy(null);
  }

  await refreshStatus({ silentConsole: true, showBusy: false });
}

async function runCommand(action: string, command: string): Promise<void> {
  setBusy(action);
  try {
    const result = await invoke<CommandResult>(command);
    updateConsole(formatCommandResult(result));
  } catch (error) {
    updateConsole(`${action}失败。\n${String(error)}`);
  } finally {
    setBusy(null);
  }

  if (command === "start_stack" || command === "stop_gateway") {
    await refreshStatus({ silentConsole: true, showBusy: false });
  }
}

function bindRevealButtons(): void {
  MASKABLE_FIELDS.forEach((fieldId) => {
    byId<HTMLButtonElement>(`toggle-${fieldId}`).addEventListener("click", () => {
      state.reveal[fieldId] = !state.reveal[fieldId];
      renderFieldVisibility();
    });
  });
}

function bindFormState(): void {
  const simpleIds = [
    "qq-app-id",
    "qq-app-secret",
    "qq-open-id",
    "minimax-api-key",
    "model",
    "msg-format",
    "qq-enabled",
    "autostart-enabled",
  ];

  simpleIds.forEach((id) => {
    byId<HTMLElement>(id).addEventListener("input", () => {
      if (!state.config) {
        return;
      }
      state.config.form = collectForm();
      renderMeta(state.config);
    });
    byId<HTMLElement>(id).addEventListener("change", () => {
      if (!state.config) {
        return;
      }
      state.config.form = collectForm();
      renderMeta(state.config);
    });
  });

  document
    .querySelectorAll<HTMLInputElement>('input[name="access-mode"]')
    .forEach((radio) =>
      radio.addEventListener("change", () => {
        if (!state.config) {
          return;
        }
        state.config.form.accessMode = getAccessMode();
        renderFromState();
      }),
    );

  byId<HTMLButtonElement>("reload-config").addEventListener("click", () => {
    void loadConfig();
  });

  byId<HTMLButtonElement>("save-config").addEventListener("click", () => {
    void saveConfig();
  });

  byId<HTMLButtonElement>("check-env").addEventListener("click", () => {
    void runCommand("环境检查", "check_environment");
  });

  byId<HTMLButtonElement>("refresh-status").addEventListener("click", () => {
    void refreshStatus();
  });

  byId<HTMLButtonElement>("create-bundle").addEventListener("click", () => {
    void runCommand("生成诊断包", "create_diagnostic_bundle");
  });

  byId<HTMLButtonElement>("start-stack").addEventListener("click", () => {
    void runCommand("启动链路", "start_stack");
  });

  byId<HTMLButtonElement>("stop-gateway").addEventListener("click", () => {
    void runCommand("停止 Gateway", "stop_gateway");
  });

  byId<HTMLButtonElement>("add-root").addEventListener("click", async () => {
    if (!state.config || state.config.form.accessMode !== "custom_roots") {
      return;
    }
    const selected = await open({
      directory: true,
      multiple: false,
      title: "选择要授权给 Phase 1 的目录",
    });
    if (typeof selected !== "string") {
      return;
    }
    state.config.form.customRoots = dedupeRoots([
      ...state.config.form.customRoots,
      selected,
    ]);
    renderFromState();
  });
}

bindRevealButtons();
bindFormState();
renderFromState();
void loadConfig();
