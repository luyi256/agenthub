const vscode = acquireVsCodeApi();
const state = {
  workspace: null,
  sessions: [],
  genWindows: [],
  genAttnAvailable: false,
  selected: null,
  selectedWindowId: null,
  currentMenuEntry: null,
  messages: [],
  activities: [],
  approvals: [],
  creating: false,
  pendingSends: new Map(),
  handoffSending: false,
  handoffRequestId: null,
  serverOnline: false,
  enablePublicRuntimes: false,
  runtimeOptions: {},
  stickToBottom: true,
  forceScrollBottom: true,
  unreadCount: 0,
  lastMessageSignature: "",
  renderedSessionUid: null,
  tabScrollLeft: 0,
  renderedTabKey: null,
  revealSelectedTab: true,
  tabRenderToken: 0,
  restoringTabScroll: false,
  lastViewportWidth: globalThis.innerWidth,
  resizeFrame: 0,
  transcriptRenderPending: false
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(
  /[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])
);

function post(type, payload = {}) {
  vscode.postMessage({ type, ...payload });
}

function toast(text) {
  const element = $("toast");
  element.textContent = text;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 3200);
}

function requestId() {
  return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}

function currentSession() {
  return state.sessions.find((session) => session.session_uid === state.selected);
}

function sessionModel(session = currentSession(), entry = currentEntry()) {
  if (entry?.kind === "gen" && entry.window?.model) return entry.window.model;
  const worker = session?.metadata?.worker || {};
  return worker.model || session?.metadata?.model ||
    session?.managed_config?.model ||
    state.runtimeOptions?.[session?.runtime]?.default_model ||
    "默认模型";
}

function sessionReasoning(session = currentSession(), entry = currentEntry()) {
  if (entry?.kind === "gen" && entry.window?.reasoning_effort) {
    return entry.window.reasoning_effort;
  }
  const worker = session?.metadata?.worker || {};
  return worker.reasoning_effort || session?.metadata?.reasoning_effort ||
    session?.managed_config?.reasoning_effort || "";
}

function sessionName(session) {
  return session?.alias || session?.effective_name || session?.effective_title || "未命名会话";
}

function runtimeMark(runtime) {
  return String(runtime || "agent").toLowerCase().includes("claude") ? "CL" : "CX";
}

function statusInfo(status) {
  return ({
    idle: ["可发送", "idle"],
    stopped: ["已暂停，发送时自动恢复", "idle"],
    running: ["正在回复", "busy"],
    active: ["正在回复", "busy"],
    waiting_approval: ["等待交互确认", "blocked"],
    error: ["运行出错", "error"],
    starting: ["正在启动", "starting"]
  })[status] || [status || "状态未知", "idle"];
}

function genStatusInfo(value) {
  return ({
    blocked: ["待处理", "blocked"],
    done: ["待验收", "done"],
    busy: ["运行中", "busy"],
    idle: ["空闲", "idle"]
  })[value] || [value || "状态未知", "idle"];
}

function conversationEntries() {
  const represented = new Set(
    state.genWindows.map((item) => item.chat_session_uid).filter(Boolean)
  );
  const genEntries = state.genWindows.map((window) => {
    const [label, stateClass] = genStatusInfo(window.state);
    return {
      kind: "gen",
      key: "gen:" + window.window_id,
      sessionUid: window.chat_session_uid || null,
      windowId: window.window_id,
      name: window.display_name,
      runtime: window.runtime,
      label,
      stateClass,
      selected:
        window.chat_session_uid === state.selected ||
        (!window.chat_session_uid && window.window_id === state.selectedWindowId),
      window
    };
  });
  const managedEntries = state.sessions
    .filter((session) => !represented.has(session.session_uid))
    .filter((session) => session.transport !== "gen-tmux-relay")
    .filter(
      (session) =>
        session.status !== "closed" &&
        (
          session.status !== "stopped" ||
          session.presence === "online" ||
          session.session_uid === state.selected
        )
    )
    .map((session) => {
      const [label, stateClass] = statusInfo(session.status);
      return {
        kind: "session",
        key: "session:" + session.session_uid,
        sessionUid: session.session_uid,
        windowId: null,
        name: sessionName(session),
        runtime: session.runtime,
        label,
        stateClass,
        selected: session.session_uid === state.selected,
        session
      };
    });
  return [...genEntries, ...managedEntries];
}

function historicalSessions() {
  return state.sessions.filter(
    (session) =>
      session.transport !== "gen-tmux-relay" &&
      session.status !== "closed" &&
      session.status === "stopped" &&
      session.presence !== "online"
  );
}

function currentEntry() {
  return conversationEntries().find((entry) => entry.selected) || null;
}

function renderTabs() {
  const root = $("conversationTabs");
  const entries = conversationEntries();
  const selectedEntry = entries.find((entry) => entry.selected) || null;
  const selectedKey = selectedEntry?.key || null;
  const selectionChanged = selectedKey !== state.renderedTabKey;
  const savedScrollLeft = state.tabScrollLeft;
  const renderToken = ++state.tabRenderToken;
  state.restoringTabScroll = true;
  if (!entries.length) {
    root.innerHTML = '<span class="tabs-empty">暂无运行中的会话</span>';
    state.renderedTabKey = null;
    state.tabScrollLeft = 0;
    state.restoringTabScroll = false;
    return;
  }
  root.innerHTML = entries.map((entry) => {
    const attn = entry.stateClass === "blocked"
      ? '<span class="tab-attn">!</span>'
      : entry.stateClass === "done"
        ? '<span class="tab-attn">✓</span>'
        : "";
    return '<button type="button" role="tab" aria-selected="' +
      (entry.selected ? "true" : "false") + '" class="conversation-tab ' +
      esc(entry.stateClass) + (entry.selected ? " selected" : "") +
      '" data-entry-key="' + esc(entry.key) + '" title="' +
      esc(entry.name + " · " + entry.label + " · " + entry.runtime) + '">' +
      '<span class="tab-status"></span><span class="tab-icon">' +
      esc(runtimeMark(entry.runtime)) + '</span><span class="tab-name">' +
      esc(entry.name) + '</span>' + attn +
      '<span class="tab-close" data-close-entry="' + esc(entry.key) +
      '" role="button" aria-label="关闭 ' + esc(entry.name) +
      '" title="关闭会话">×</span></button>';
  }).join("");
  root.querySelectorAll("[data-entry-key]").forEach((button) => {
    button.onclick = () => selectEntry(button.dataset.entryKey);
    button.oncontextmenu = (event) => {
      event.preventDefault();
      const entry = conversationEntries().find(
        (candidate) => candidate.key === button.dataset.entryKey
      );
      if (entry) openEntryMenu(entry, event.clientX, event.clientY);
    };
  });
  root.querySelectorAll("[data-close-entry]").forEach((close) => {
    close.onclick = (event) => {
      event.stopPropagation();
      const entry = conversationEntries().find(
        (candidate) => candidate.key === close.dataset.closeEntry
      );
      if (!entry) return;
      post("closeSession", {
        sessionUid: entry.sessionUid || "",
        windowId: entry.windowId || "",
        name: entry.name || ""
      });
    };
  });
  requestAnimationFrame(() => {
    if (renderToken !== state.tabRenderToken) return;
    const selected = root.querySelector(".conversation-tab.selected");
    if (selected && (selectionChanged || state.revealSelectedTab)) {
      const left = selected.offsetLeft;
      const right = left + selected.offsetWidth;
      if (left < root.scrollLeft) {
        root.scrollLeft = Math.max(0, left - 6);
      } else if (right > root.scrollLeft + root.clientWidth) {
        root.scrollLeft = right - root.clientWidth + 6;
      }
    } else {
      const maximum = Math.max(0, root.scrollWidth - root.clientWidth);
      root.scrollLeft = Math.min(savedScrollLeft, maximum);
    }
    state.tabScrollLeft = root.scrollLeft;
    state.renderedTabKey = selectedKey;
    state.revealSelectedTab = false;
    state.restoringTabScroll = false;
  });
}

function selectEntry(key) {
  const entry = conversationEntries().find((candidate) => candidate.key === key);
  if (!entry) return;
  for (const requestId of [...state.pendingSends.keys()]) {
    removePendingSend(requestId);
  }
  state.pendingSends.clear();
  state.messages = [];
  state.activities = [];
  state.approvals = [];
  state.unreadCount = 0;
  state.stickToBottom = true;
  state.forceScrollBottom = true;
  state.lastMessageSignature = "";
  state.revealSelectedTab = true;
  if (entry.kind === "gen") {
    state.selectedWindowId = entry.windowId;
    state.selected = entry.sessionUid || null;
    if (entry.sessionUid) {
      post("selectSession", { sessionUid: entry.sessionUid });
    } else {
      post("openGenWindow", { windowId: entry.windowId });
    }
  } else {
    state.selectedWindowId = null;
    state.selected = entry.sessionUid;
    post("selectSession", { sessionUid: entry.sessionUid });
  }
  render();
}

function renderHeader() {
  const session = currentSession();
  const entry = currentEntry();
  const status = entry
    ? entry.kind === "gen"
      ? genStatusInfo(entry.window.state)
      : statusInfo(session?.status)
    : ["尚未选择对话", "idle"];
  $("conversationTitle").textContent = entry?.name || "Agent Hub";
  $("agentMark").textContent = entry ? runtimeMark(entry.runtime) : "AH";
  $("status").textContent = status[0];
  $("runtimeDot").className =
    "status-dot " +
    (status[1] === "busy" || status[1] === "starting"
      ? "busy"
      : status[1] === "error" || status[1] === "blocked"
        ? "error"
        : "ready");
  $("sessionTags").textContent = entry
    ? entry.runtime +
      " · " + sessionModel(session, entry) +
      (sessionReasoning(session, entry)
        ? " · " + sessionReasoning(session, entry)
        : "") +
      (session?.managed_config?.permission_profile
        ? " · " + session.managed_config.permission_profile
        : "")
    : "";
  $("workspaceMeta").textContent = state.workspace
    ? state.workspace.name + " · " + state.workspace.cwd
    : "请先打开一个 VS Code 项目文件夹";
  $("serviceState").className =
    "service-state " + (state.serverOnline ? "online" : "offline");
  $("handoffOpen").disabled = !session || handoffTargets().length === 0;
  $("historySessions").classList.toggle(
    "hidden",
    historicalSessions().length === 0
  );
}

function isNearBottom(root = $("transcript")) {
  return root.scrollHeight - root.scrollTop - root.clientHeight < 90;
}

function messageKey(message, index) {
  return message.message_id || message.localRequestId || "local-" + index;
}

function activityKey(activity, index) {
  return activity.activity_id || "activity-" + index;
}

function captureScrollAnchor(root) {
  const rootTop = root.getBoundingClientRect().top;
  for (const element of root.querySelectorAll("[data-timeline-key]")) {
    const rect = element.getBoundingClientRect();
    if (rect.bottom >= rootTop + 2) {
      return {
        key: element.dataset.timelineKey,
        offset: rect.top - rootTop
      };
    }
  }
  return null;
}

function updateNewMessagesButton() {
  const button = $("newMessages");
  button.classList.toggle("hidden", state.stickToBottom || state.unreadCount === 0);
  button.textContent =
    state.unreadCount > 0
      ? "↓ " + state.unreadCount + " 条新消息"
      : "↓ 回到底部";
}

function fallbackMessageHtml(value) {
  const text = String(value || "");
  if (!text) return "";
  return "<p>" + esc(text).replace(/\n/g, "<br>") + "</p>";
}

function messageSignature(messages, activities) {
  const last = messages[messages.length - 1];
  const activity = activities[activities.length - 1];
  return [
    last
      ? [messageKey(last, messages.length - 1), last.status, String(last.content || "").length].join(":")
      : "",
    activity
      ? [
          activityKey(activity, activities.length - 1),
          activity.status,
          String(activity.result || "").length,
          safeStringify(activity.input).length
        ].join(":")
      : ""
  ].join("|");
}

function safeStringify(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value ?? "");
  }
}

function firstLine(value) {
  return String(value || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean) || "";
}

function timelineEntries() {
  const entries = [
    ...state.messages.map((message, index) => ({
      type: "message",
      key: messageKey(message, index),
      timestamp: Date.parse(
        message.role === "assistant" && !message.metadata?.imported
          ? message.updated_at || message.created_at || ""
          : message.created_at || message.updated_at || ""
      ) || 0,
      order: index,
      value: message
    })),
    ...state.activities.map((activity, index) => ({
      type: "activity",
      key: activityKey(activity, index),
      timestamp: Date.parse(activity.created_at || activity.updated_at || "") || 0,
      order: state.messages.length + index,
      value: activity
    }))
  ];
  entries.sort(
    (left, right) =>
      left.timestamp - right.timestamp || left.order - right.order
  );
  return entries;
}

function activityStatusLabel(status) {
  return ({
    running: "运行中",
    completed: "已完成",
    failed: "失败"
  })[status] || status || "";
}

function renderActivity(activity, key, expanded) {
  const status = esc(activityStatusLabel(activity.status));
  if (activity.kind === "plan") {
    return '<article class="activity plan-activity" data-timeline-key="' +
      esc(key) + '"><div class="activity-rail plan">P</div>' +
      '<div class="activity-content"><div class="activity-heading">' +
      '<strong>Plan</strong><span class="activity-status ' +
      esc(activity.status) + '">' + status + '</span></div>' +
      '<div class="plan-body bubble">' +
      (activity.rendered_input || fallbackMessageHtml(safeStringify(activity.input))) +
      '</div></div></article>';
  }
  if (activity.kind === "commentary") {
    return '<article class="activity commentary-activity" data-timeline-key="' +
      esc(key) + '"><div class="activity-rail commentary">›</div>' +
      '<div class="activity-content"><div class="activity-heading">' +
      '<strong>Agent 进度</strong></div><div class="commentary-body bubble">' +
      (activity.rendered_input || fallbackMessageHtml(safeStringify(activity.input))) +
      '</div></div></article>';
  }
  const detailsLoaded = Boolean(activity.details_loaded);
  const input = detailsLoaded ? safeStringify(activity.input) : "";
  const result = detailsLoaded ? String(activity.result || "") : "";
  const preview =
    activity.result_preview ||
    activity.input_preview ||
    firstLine(result) ||
    firstLine(input) ||
    (activity.status === "running" ? "等待工具返回" : "无输出");
  return '<details class="activity tool-activity" data-timeline-key="' +
    esc(key) + '" data-activity-id="' + esc(key) + '"' +
    (expanded ? " open" : "") + '><summary><span class="tool-chevron">›</span>' +
    '<span class="tool-icon">⌘</span><span class="tool-name">' +
    esc(activity.name || "Tool") + '</span><span class="activity-status ' +
    esc(activity.status) + '">' + status + '</span><span class="tool-preview">' +
    esc(preview) + '</span></summary><div class="tool-details">' +
    (!detailsLoaded && activity.has_details
      ? '<div class="tool-pending">展开后加载完整参数和结果…</div>'
      : "") +
    (detailsLoaded && input
      ? '<div class="tool-section"><span>调用参数</span><pre>' + esc(input) + '</pre></div>'
      : "") +
    (detailsLoaded && result
      ? '<div class="tool-section"><span>工具结果</span><pre>' + esc(result) + '</pre></div>'
      : detailsLoaded && activity.status === "running"
        ? '<div class="tool-pending">等待工具返回…</div>'
        : "") +
    '</div></details>';
}

function renderMessages() {
  const root = $("transcript");
  const session = currentSession();
  const activeSelection = globalThis.getSelection?.();
  const selectionInsideTranscript = Boolean(
    activeSelection &&
    !activeSelection.isCollapsed &&
    activeSelection.rangeCount &&
    (
      root.contains(activeSelection.anchorNode) ||
      root.contains(activeSelection.focusNode)
    )
  );
  const expandedActivities = new Set(
    [...root.querySelectorAll("details.tool-activity[open]")]
      .map((element) => element.dataset.activityId)
      .filter(Boolean)
  );
  const sessionChanged = state.renderedSessionUid !== state.selected;
  if (selectionInsideTranscript && !sessionChanged) {
    state.transcriptRenderPending = true;
    return;
  }
  state.transcriptRenderPending = false;
  const anchor = !state.stickToBottom && !sessionChanged
    ? captureScrollAnchor(root)
    : null;
  const oldTop = root.scrollTop;
  const wasNearBottom = isNearBottom(root);

  if (!session) {
    root.innerHTML =
      '<div class="welcome"><div class="mark">AH</div>' +
      '<h2>在一个界面管理所有 Agent</h2>' +
      '<p>顶部选择会话，下面直接继续对话。任务在后台 tmux 中持续运行，不需要打开终端。</p>' +
      '<button class="primary" id="welcomeNew" type="button">新建第一个对话</button></div>';
    $("welcomeNew").onclick = openNewDialog;
    state.renderedSessionUid = null;
    updateNewMessagesButton();
    return;
  }

  if (!state.messages.length && !state.activities.length) {
    root.innerHTML =
      '<div class="conversation-empty"><strong>对话已准备好</strong>' +
      '<span>在下方输入消息开始交流。关闭 VS Code 后，后台任务仍会继续运行。</span></div>';
  } else {
    const roleLabel = { human: "你", assistant: "Agent", system: "系统" };
    const statusLabel = {
      completed: "已完成",
      queued: "已排队",
      streaming: "回复中",
      failed: "失败",
      interrupted: "已中断"
    };
    root.innerHTML = timelineEntries().map((entry) => {
      if (entry.type === "activity") {
        return renderActivity(
          entry.value,
          entry.key,
          expandedActivities.has(entry.key)
        );
      }
      const message = entry.value;
      const roleClass = message.role === "human" ? "user" : message.role;
      const html = message.rendered_content ||
        fallbackMessageHtml(
          message.content || (message.status === "streaming" ? "正在思考…" : "")
        );
      return '<article class="message ' + esc(roleClass) +
        '" data-timeline-key="' + esc(entry.key) +
        '"><div class="message-inner"><div class="meta">' +
        esc(roleLabel[message.role] || message.role) + " · " +
        esc(statusLabel[message.status] || message.status) +
        '</div><div class="bubble ' +
        (message.status === "streaming" ? "typing" : "") + '">' +
        html + '</div></div></article>';
    }).join("");
  }

  wireRenderedMarkdown(root);
  const shouldBottom =
    state.forceScrollBottom || sessionChanged || state.stickToBottom || wasNearBottom;
  requestAnimationFrame(() => {
    if (shouldBottom) {
      root.scrollTop = root.scrollHeight;
      state.stickToBottom = true;
      state.unreadCount = 0;
    } else if (anchor) {
      const escapedKey = globalThis.CSS?.escape
        ? globalThis.CSS.escape(anchor.key)
        : anchor.key.replace(/["\\]/g, "\\$&");
      const restored = root.querySelector(
        '[data-timeline-key="' + escapedKey + '"]'
      );
      if (restored) {
        const rootTop = root.getBoundingClientRect().top;
        root.scrollTop += restored.getBoundingClientRect().top - rootTop - anchor.offset;
      } else {
        root.scrollTop = oldTop;
      }
    } else {
      root.scrollTop = oldTop;
    }
    state.forceScrollBottom = false;
    state.renderedSessionUid = state.selected;
    updateNewMessagesButton();
  });
}

function wireRenderedMarkdown(root) {
  root.querySelectorAll(".bubble a[href]").forEach((link) => {
    link.onclick = (event) => {
      event.preventDefault();
      post("openExternal", { href: link.href });
    };
  });
  root.querySelectorAll(".bubble pre").forEach((pre) => {
    if (pre.querySelector(".copy-code")) return;
    const code = pre.querySelector("code");
    if (!code) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-code";
    button.textContent = "复制";
    button.onclick = () => {
      post("copyText", { text: code.textContent || "" });
      button.textContent = "已复制";
      setTimeout(() => { button.textContent = "复制"; }, 1200);
    };
    pre.prepend(button);
  });
  root.querySelectorAll("details.tool-activity").forEach((details) => {
    const loadDetails = () => {
      if (!details.open) return;
      const activityId = details.dataset.activityId;
      const activity = state.activities.find(
        (item, index) => activityKey(item, index) === activityId
      );
      if (
        !activity ||
        !activity.has_details ||
        activity.details_loaded ||
        activity.details_loading
      ) {
        return;
      }
      activity.details_loading = true;
      const pending = details.querySelector(".tool-pending");
      if (pending) pending.textContent = "正在加载完整参数和结果…";
      post("loadActivity", {
        sessionUid: state.selected,
        activityId
      });
    };
    details.addEventListener("toggle", loadDetails);
    loadDetails();
  });
}

function renderApprovals() {
  const session = currentSession();
  const relayBlocked =
    session?.transport === "gen-tmux-relay" &&
    session?.status === "waiting_approval" &&
    state.approvals.length === 0;
  const blockedNotice = relayBlocked
    ? '<div class="interaction-blocked"><strong>此会话正在等待交互确认</strong>' +
      '<span>为了避免误触原 TUI 的默认选项，普通文字不会被直接送入确认界面。当前尚不能在插件内安全解析此类原生选择器；如必须立即处理，可从会话菜单的调试入口临时打开原终端。</span></div>'
    : "";
  $("approvals").innerHTML = blockedNotice + state.approvals.map((approval) => {
    const reason = approval.params?.reason || approval.params?.command || approval.method;
    return '<div class="approval"><div class="approval-title">需要你的授权</div>' +
      '<div class="approval-reason">' + esc(reason) + '</div>' +
      '<div class="approval-actions"><button data-apr="' + esc(approval.approval_id) +
      '" data-act="decline">拒绝</button><button class="allow" data-apr="' +
      esc(approval.approval_id) + '" data-act="accept">允许</button></div></div>';
  }).join("");
  document.querySelectorAll("[data-apr]").forEach((button) => {
    button.onclick = () => post("resolveApproval", {
      approvalId: button.dataset.apr,
      action: button.dataset.act
    });
  });
}

function renderComposer() {
  const session = currentSession();
  const entry = currentEntry();
  const waitingApproval = session?.status === "waiting_approval";
  const starting = session?.status === "starting";
  const running = Boolean(
    session && ["running", "active"].includes(session.status)
  );
  const disabled = !session || waitingApproval || starting;
  $("input").disabled = disabled;
  $("send").disabled = disabled;
  $("send").textContent = running ? "追加" : "发送";
  const model = session ? sessionModel(session, entry) : "未选择模型";
  const effort = session ? sessionReasoning(session, entry) : "";
  $("composerModel").textContent =
    model + (effort ? " · " + effort : "");
  $("composerHint").textContent = !session
    ? "请先新建或选择一个对话"
    : waitingApproval
      ? "请先处理上方的交互确认"
      : starting
        ? "Agent 正在启动"
      : running
        ? "运行中可继续发送；Agent 会在当前任务中自动处理"
      : state.pendingSends.size
        ? "消息正在提交"
        : "Enter 发送 · Shift+Enter 换行";
}

function render() {
  renderTabs();
  renderHeader();
  renderMessages();
  renderApprovals();
  renderComposer();
}

function removePendingSend(requestId) {
  const pending = state.pendingSends.get(requestId);
  if (pending?.ackTimer) clearTimeout(pending.ackTimer);
  if (pending?.acceptTimer) clearTimeout(pending.acceptTimer);
  state.pendingSends.delete(requestId);
}

function markPendingFailed(requestId, message) {
  removePendingSend(requestId);
  state.messages = state.messages.map((item) =>
    item.localRequestId === requestId
      ? {
          ...item,
          status: "failed",
          content: item.role === "human" ? item.content : message,
          rendered_content: null
        }
      : item
  );
  toast(message);
  render();
}

function openNewDialog() {
  if (!state.workspace) return;
  $("newCwd").value = state.workspace.cwd;
  const select = $("newRuntime");
  const selected = select.value;
  select.innerHTML =
    '<option value="tcodex">tcodex（推荐，适合编码和执行任务）</option>' +
    '<option value="tclaude">tclaude（适合分析和写作）</option>' +
    (state.enablePublicRuntimes
      ? '<option value="codex">codex</option><option value="claude">claude</option>'
      : "");
  if (Array.from(select.options).some((option) => option.value === selected)) {
    select.value = selected;
  }
  updateNewModelOptions();
  $("newDialog").showModal();
}

function updateNewModelOptions() {
  const runtime = $("newRuntime").value;
  const options = state.runtimeOptions?.[runtime] || {};
  const select = $("newModel");
  const previous = select.value;
  const defaultLabel = options.default_model
    ? "默认（" + options.default_model + "）"
    : "使用 Agent 默认模型";
  const rows = [{ id: "", label: defaultLabel }, ...(options.models || [])];
  select.innerHTML = rows.map((item) =>
    '<option value="' + esc(item.id) + '">' +
    esc(item.label || item.id) +
    (item.description ? " · " + esc(item.description) : "") +
    '</option>'
  ).join("");
  if (rows.some((item) => item.id === previous)) select.value = previous;
  const selectedModel = (options.models || []).find(
    (item) => item.id === select.value
  );
  const efforts = selectedModel?.reasoning_efforts?.length
    ? selectedModel.reasoning_efforts
    : (options.reasoning_efforts || []);
  const reasoning = $("newReasoning");
  const previousEffort = reasoning.value;
  reasoning.innerHTML =
    '<option value="">使用模型默认</option>' +
    efforts.map((effort) =>
      '<option value="' + esc(effort) + '">' + esc(effort) + '</option>'
    ).join("");
  if (efforts.includes(previousEffort)) reasoning.value = previousEffort;
  $("newReasoningLabel").classList.toggle("hidden", efforts.length === 0);
}

function handoffTargets() {
  const sourceUid = state.selected;
  return conversationEntries().filter((entry) =>
    entry.sessionUid !== sourceUid &&
    (!entry.session || entry.session.capabilities?.chat === true) &&
    (entry.kind === "gen"
      ? entry.window.state !== "blocked"
      : !["waiting_approval", "starting"].includes(
          entry.session.status
        ))
  );
}

function openHandoffDialog() {
  const targets = handoffTargets();
  if (!state.selected || !targets.length) {
    toast("当前没有可接收协作消息的其他会话。");
    return;
  }
  $("handoffTarget").innerHTML = targets.map((entry) =>
    '<option value="' + esc(entry.key) + '">' +
    esc(entry.name + " · " + entry.runtime + " · " + entry.label) +
    '</option>'
  ).join("");
  $("handoffText").value = "";
  $("handoffDialog").showModal();
  $("handoffText").focus();
}

function openHistoryDialog() {
  const sessions = historicalSessions();
  if (!sessions.length) {
    toast("当前没有可恢复的历史会话。");
    return;
  }
  $("historyList").innerHTML = sessions.map((session) =>
    '<button type="button" class="history-item" data-history-session="' +
    esc(session.session_uid) + '"><span class="history-item-mark">' +
    esc(runtimeMark(session.runtime)) + '</span><span class="history-item-name">' +
    esc(sessionName(session)) + '</span><span class="history-item-meta">' +
    esc(session.runtime + " · 已暂停，发送时自动恢复") +
    '</span></button>'
  ).join("");
  $("historyList").querySelectorAll("[data-history-session]").forEach((button) => {
    button.onclick = () => {
      state.selected = button.dataset.historySession;
      state.selectedWindowId = null;
      state.forceScrollBottom = true;
      state.stickToBottom = true;
      state.messages = [];
      state.activities = [];
      state.approvals = [];
      $("historyDialog").close();
      post("selectSession", { sessionUid: state.selected });
      render();
    };
  });
  $("historyDialog").showModal();
}

function latestAssistantReply() {
  return [...state.messages].reverse().find(
    (message) => message.role === "assistant" &&
      message.status === "completed" &&
      String(message.content || "").trim()
  );
}

function openEntryMenu(entry, x, y) {
  state.currentMenuEntry = entry;
  const menu = $("windowMenu");
  const hasGen = entry.kind === "gen";
  menu.querySelectorAll("[data-window-action='red'], [data-window-action='yellow'], [data-window-action='clear'], [data-window-action='terminal']")
    .forEach((button) => button.classList.toggle("hidden", !hasGen));
  menu.querySelectorAll(".window-menu-separator").forEach(
    (separator) => separator.classList.remove("hidden")
  );
  menu.style.left = Math.min(x, globalThis.innerWidth - 188) + "px";
  menu.style.top = Math.min(y, globalThis.innerHeight - 250) + "px";
  menu.classList.remove("hidden");
}

function openCurrentMenu() {
  const entry = currentEntry();
  if (!entry) {
    toast("请先选择一个会话。");
    return;
  }
  const button = $("currentMenuButton");
  const rect = button.getBoundingClientRect();
  openEntryMenu(entry, rect.right - 178, rect.bottom + 4);
}

window.addEventListener("message", (event) => {
  const message = event.data;
  if (message.type === "snapshot") {
    const previousSelected = state.selected;
    state.serverOnline = true;
    state.workspace = message.workspace;
    state.sessions = message.snapshot.sessions || [];
    state.runtimeOptions = message.snapshot.runtime_options || {};
    state.selected = message.selectedSessionUid;
    state.enablePublicRuntimes = Boolean(message.enablePublicRuntimes);
    if (previousSelected !== state.selected) {
      state.forceScrollBottom = true;
      state.stickToBottom = true;
      state.unreadCount = 0;
    }
    render();
  } else if (message.type === "genWindows") {
    state.genWindows = message.snapshot?.windows || [];
    state.genAttnAvailable = Boolean(message.snapshot?.attn?.available);
    renderTabs();
    renderHeader();
  } else if (message.type === "messages" && message.sessionUid === state.selected) {
    state.approvals = message.approvals || [];
    {
      const incoming = message.messages || [];
      const existingActivities = new Map(
        state.activities.map((activity, index) => [
          activityKey(activity, index),
          activity
        ])
      );
      const incomingActivities = (message.activities || []).map(
        (activity, index) => {
          const existing = existingActivities.get(activityKey(activity, index));
          const unchanged =
            existing &&
            existing.status === activity.status &&
            existing.updated_at === activity.updated_at;
          return existing?.details_loaded && unchanged
            ? {
                ...activity,
                input: existing.input,
                result: existing.result,
                details_loaded: true
              }
            : existing?.details_loading && unchanged
              ? { ...activity, details_loading: true }
            : activity;
        }
      );
      const incomingSignature = messageSignature(incoming, incomingActivities);
      const changed = Boolean(
        state.lastMessageSignature && incomingSignature !== state.lastMessageSignature
      );
      if (!state.stickToBottom && changed) state.unreadCount = 1;
      const optimistic = state.messages.filter(
        (item) =>
          item.localRequestId &&
          state.pendingSends.has(item.localRequestId) &&
          !incoming.some(
            (serverItem) =>
              serverItem.role === item.role &&
              serverItem.content?.trim() === item.content?.trim()
          )
      );
      state.messages = [...incoming, ...optimistic];
      state.activities = incomingActivities;
      state.lastMessageSignature = incomingSignature;
    }
    render();
  } else if (
    message.type === "activityDetail" &&
    message.sessionUid === state.selected
  ) {
    state.activities = state.activities.map((activity, index) =>
      activityKey(activity, index) === message.activity.activity_id
        ? {
            ...activity,
            ...message.activity,
            details_loaded: true,
            details_loading: false
          }
        : activity
    );
    render();
  } else if (
    message.type === "activityDetailFailed" &&
    message.sessionUid === state.selected
  ) {
    state.activities = state.activities.map((activity, index) =>
      activityKey(activity, index) === message.activityId
        ? { ...activity, details_loading: false }
        : activity
    );
    toast(message.message || "工具详情加载失败。");
  } else if (message.type === "sendStarted") {
    const pending = state.pendingSends.get(message.requestId);
    if (pending) {
      if (pending.ackTimer) clearTimeout(pending.ackTimer);
      pending.acceptTimer = setTimeout(() => {
        if (state.pendingSends.has(message.requestId)) {
          markPendingFailed(
            message.requestId,
            "Agent 启动或发送超时。请刷新确认后再重试。"
          );
        }
      }, 120000);
    }
  } else if (message.type === "sendAccepted") {
    removePendingSend(message.requestId);
    toast(
      message.delivery === "steered" || message.delivery === "steer"
        ? "消息已追加到当前任务"
        : message.delivery === "hub_queued"
          ? "消息已排到下一轮，当前任务结束后自动处理"
        : message.delivery === "runtime_queued"
          ? "消息已交给当前 Agent 排队处理"
        : message.delivery === "queued"
          ? "消息已排队"
          : "消息已发送"
    );
    render();
  } else if (message.type === "sendFailed") {
    markPendingFailed(
      message.requestId,
      message.message || "消息发送失败。"
    );
  } else if (message.type === "handoffStarted" && message.requestId === state.handoffRequestId) {
    state.handoffSending = true;
    $("sendHandoff").disabled = true;
    $("sendHandoff").textContent = "正在发送…";
  } else if (message.type === "handoffAccepted" && message.requestId === state.handoffRequestId) {
    state.handoffSending = false;
    state.handoffRequestId = null;
    $("sendHandoff").disabled = false;
    $("sendHandoff").textContent = "发送协作消息";
    $("handoffDialog").close();
    toast("协作消息已发送，目标 Agent 已开始处理。");
  } else if (message.type === "handoffFailed" && message.requestId === state.handoffRequestId) {
    state.handoffSending = false;
    state.handoffRequestId = null;
    $("sendHandoff").disabled = false;
    $("sendHandoff").textContent = "发送协作消息";
    toast(message.message || "协作消息发送失败。");
  } else if (message.type === "viewStale") {
    for (const requestId of [...state.pendingSends.keys()]) {
      markPendingFailed(
        requestId,
        message.message || "当前界面已失效，请重置界面。"
      );
    }
  } else if (message.type === "error") {
    toast(message.message);
  } else if (message.type === "serverOffline") {
    state.serverOnline = false;
    for (const requestId of [...state.pendingSends.keys()]) {
      markPendingFailed(requestId, "Agent Hub 服务未连接，请刷新。");
    }
  } else if (message.type === "workspaceMissing") {
    for (const requestId of [...state.pendingSends.keys()]) {
      markPendingFailed(requestId, "请先在 VS Code 中打开一个项目文件夹。");
    }
  } else if (message.type === "creatingSession") {
    $("create").disabled = message.value;
    $("create").textContent = message.value ? "正在创建…" : "创建对话";
  } else if (message.type === "focusComposer") {
    $("input").focus();
  } else if (message.type === "sessionClosed") {
    toast("会话已关闭");
  }
});

$("transcript").addEventListener("scroll", () => {
  const nearBottom = isNearBottom();
  state.stickToBottom = nearBottom;
  if (nearBottom) state.unreadCount = 0;
  updateNewMessagesButton();
}, { passive: true });

$("conversationTabs").addEventListener("scroll", () => {
  if (!state.restoringTabScroll) {
    state.tabScrollLeft = $("conversationTabs").scrollLeft;
  }
}, { passive: true });

function stabilizeAfterResize() {
  cancelAnimationFrame(state.resizeFrame);
  state.resizeFrame = requestAnimationFrame(() => {
    const width = document.documentElement.clientWidth;
    const shrinking =
      state.lastViewportWidth > 0 && width < state.lastViewportWidth;
    state.lastViewportWidth = width;
    document.documentElement.scrollLeft = 0;
    document.body.scrollLeft = 0;
    $("transcript").scrollLeft = 0;

    const tabs = $("conversationTabs");
    const selected = tabs.querySelector(".conversation-tab.selected");
    if (selected) {
      const left = selected.offsetLeft;
      const right = left + selected.offsetWidth;
      if (left < tabs.scrollLeft) {
        tabs.scrollLeft = Math.max(0, left - 6);
      } else if (right > tabs.scrollLeft + tabs.clientWidth) {
        tabs.scrollLeft = right - tabs.clientWidth + 6;
      }
      state.tabScrollLeft = tabs.scrollLeft;
    } else {
      const maximum = Math.max(0, tabs.scrollWidth - tabs.clientWidth);
      tabs.scrollLeft = Math.min(tabs.scrollLeft, maximum);
      state.tabScrollLeft = tabs.scrollLeft;
    }

    tabs.querySelectorAll(".tab-name").forEach((element) => {
      element.scrollLeft = 0;
    });
    if (shrinking) {
      document
        .querySelectorAll(
          ".bubble pre, .bubble table, .tool-section pre"
        )
        .forEach((element) => {
          element.scrollLeft = 0;
        });
    }
  });
}

if (globalThis.ResizeObserver) {
  const layoutObserver = new ResizeObserver(stabilizeAfterResize);
  layoutObserver.observe(document.documentElement);
}
window.addEventListener("resize", stabilizeAfterResize, { passive: true });

$("newMessages").onclick = () => {
  const root = $("transcript");
  state.stickToBottom = true;
  state.unreadCount = 0;
  root.scrollTo({ top: root.scrollHeight, behavior: "smooth" });
  updateNewMessagesButton();
};

$("newSession").onclick = openNewDialog;
$("newRuntime").onchange = updateNewModelOptions;
$("newModel").onchange = updateNewModelOptions;
$("historySessions").onclick = openHistoryDialog;
$("handoffOpen").onclick = openHandoffDialog;
$("refresh").onclick = () => post("refresh");
$("currentMenuButton").onclick = openCurrentMenu;
$("closeDialog").onclick = () => $("newDialog").close();
$("cancelCreate").onclick = () => $("newDialog").close();
$("closeHandoff").onclick = () => $("handoffDialog").close();
$("cancelHandoff").onclick = () => $("handoffDialog").close();
$("closeHistory").onclick = () => $("historyDialog").close();
$("useLatestReply").onclick = () => {
  const reply = latestAssistantReply();
  if (!reply) {
    toast("当前会话还没有已完成的 Agent 回复。");
    return;
  }
  $("handoffText").value = reply.content;
  $("handoffText").focus();
};

$("windowMenu").querySelectorAll("[data-window-action]").forEach((button) => {
  button.onclick = () => {
    const entry = state.currentMenuEntry;
    const action = button.dataset.windowAction;
    $("windowMenu").classList.add("hidden");
    if (!entry) return;
    if (action === "terminal" && entry.windowId) {
      post("openGenTerminal", { windowId: entry.windowId });
    } else if (action === "rename") {
      const current = entry.name || "";
      if (entry.kind === "gen") {
        post("renameGenWindow", {
          windowId: entry.windowId,
          currentName: current
        });
      } else {
        post("renameSession", {
          sessionUid: entry.sessionUid,
          currentName: current
        });
      }
    } else if (action === "handoff") {
      if (!entry.selected) selectEntry(entry.key);
      setTimeout(openHandoffDialog, 100);
    } else if (action === "close") {
      post("closeSession", {
        sessionUid: entry.sessionUid || "",
        windowId: entry.windowId || "",
        name: entry.name || ""
      });
    } else if (action === "reset") {
      post("resetView");
    } else if (entry.windowId) {
      post("setGenAttn", { windowId: entry.windowId, action });
    }
  };
});

$("newForm").onsubmit = (event) => {
  event.preventDefault();
  post("createSession", {
    payload: {
      runtime: $("newRuntime").value,
      model: $("newModel").value,
      reasoning_effort: $("newReasoning").value,
      alias: $("newAlias").value,
      cwd: $("newCwd").value,
      permission_profile: $("newPermission").value
    }
  });
};

$("handoffForm").onsubmit = (event) => {
  event.preventDefault();
  if (state.handoffSending) return;
  const source = currentSession();
  const targetKey = $("handoffTarget").value;
  const target = conversationEntries().find((entry) => entry.key === targetKey);
  const text = $("handoffText").value.trim();
  if (!source || !target || !text) return;
  const id = requestId();
  state.handoffSending = true;
  state.handoffRequestId = id;
  $("sendHandoff").disabled = true;
  $("sendHandoff").textContent = "正在发送…";
  post("handoff", {
    requestId: id,
    sourceSessionUid: source.session_uid,
    targetSessionUid: target.sessionUid || "",
    targetWindowId: target.windowId || "",
    text
  });
};

$("composer").onsubmit = (event) => {
  event.preventDefault();
  const text = $("input").value.trim();
  const session = currentSession();
  if (
    !text ||
    !session ||
    ["waiting_approval", "starting"].includes(session.status)
  ) return;
  const id = requestId();
  state.stickToBottom = true;
  state.forceScrollBottom = true;
  const running = ["running", "active"].includes(session.status);
  state.messages.push({
    role: "human",
    content: text,
    status: "completed",
    created_at: new Date().toISOString(),
    localRequestId: id
  });
  if (!running) {
    state.messages.push({
      role: "assistant",
      content: "",
      status: "streaming",
      created_at: new Date(Date.now() + 1).toISOString(),
      localRequestId: id
    });
  }
  $("input").value = "";
  const pending = { ackTimer: null, acceptTimer: null };
  state.pendingSends.set(id, pending);
  render();
  pending.ackTimer = setTimeout(() => {
    if (state.pendingSends.has(id)) {
      markPendingFailed(
        id,
        "消息没有送达 Agent Hub。请刷新后重试。"
      );
    }
  }, 10000);
  post("sendMessage", {
    text,
    sessionUid: session.session_uid,
    requestId: id
  });
};

$("input").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
};

document.addEventListener("click", (event) => {
  if (!event.target.closest("#windowMenu") && !event.target.closest("#currentMenuButton")) {
    $("windowMenu").classList.add("hidden");
  }
});

document.addEventListener("selectionchange", () => {
  const selection = globalThis.getSelection?.();
  if (
    state.transcriptRenderPending &&
    (!selection || selection.isCollapsed)
  ) {
    state.transcriptRenderPending = false;
    renderMessages();
  }
});

post("ready");
