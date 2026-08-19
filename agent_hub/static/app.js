const state = {
  sessions: [],
  links: [],
  diagnostics: [],
  counts: {},
  messages: {},
  approvals: {},
  selectedUid: null,
  runtime: "all",
  search: "",
  ws: null,
};

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.style.background = isError ? "var(--error)" : "var(--text)";
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 2400);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function applySnapshot(snapshot) {
  state.sessions = snapshot.sessions || [];
  state.links = snapshot.links || [];
  state.diagnostics = snapshot.diagnostics || [];
  state.counts = snapshot.counts || {};
  if (!state.selectedUid || !state.sessions.some((x) => x.session_uid === state.selectedUid)) {
    state.selectedUid = state.sessions.find((x) => x.presence === "online")?.session_uid
      || state.sessions[0]?.session_uid
      || null;
  }
  render();
}

function connectWebSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${location.host}/ws`);
  state.ws = ws;
  ws.onmessage = (event) => {
    if (event.data === "pong") return;
    const message = JSON.parse(event.data);
    if (message.type === "snapshot") applySnapshot(message.data);
    if (message.type === "messages.changed") {
      state.messages[message.session_uid] = message.messages || [];
      state.approvals[message.session_uid] = message.approvals || [];
      if (message.session_uid === state.selectedUid) renderChat();
    }
    if (message.type === "message.delta") {
      const items = state.messages[message.session_uid] || [];
      const target = items.find((x) => x.message_id === message.message_id);
      if (target) target.content += message.delta;
      if (message.session_uid === state.selectedUid) renderChat();
    }
    if (message.type === "approval.requested") {
      const approvals = state.approvals[message.session_uid] || [];
      approvals.push(message.approval);
      state.approvals[message.session_uid] = approvals;
      if (message.session_uid === state.selectedUid) renderChat();
    }
  };
  ws.onclose = () => {
    $("scanStatus").textContent = "连接断开，正在重连…";
    setTimeout(connectWebSocket, 1500);
  };
  ws.onerror = () => ws.close();
}

function sessionName(session) {
  return session.alias || session.native_name || session.auto_native_name;
}

function selectedSession() {
  return state.sessions.find((x) => x.session_uid === state.selectedUid);
}

function filteredSessions() {
  const query = state.search.trim().toLowerCase();
  return state.sessions.filter((session) => {
    if (state.runtime !== "all" && session.runtime !== state.runtime) return false;
    if (!query) return true;
    const haystack = [
      sessionName(session),
      session.effective_title,
      session.project,
      session.cwd,
      session.runtime,
      session.role,
    ].join(" ").toLowerCase();
    return haystack.includes(query);
  });
}

function renderMetrics() {
  $("metricOnline").textContent = state.counts.online || 0;
  $("metricTotal").textContent = state.counts.total || 0;
  $("metricMessageable").textContent = state.counts.messageable || 0;
  $("metricLinks").textContent = state.counts.links || 0;
  $("scanStatus").textContent = `每 5 秒刷新 · ${new Date().toLocaleTimeString()}`;
}

function renderFilters() {
  const runtimes = ["all", "claude", "tclaude", "codex", "tcodex"];
  $("runtimeFilters").innerHTML = runtimes.map((runtime) => {
    const count = runtime === "all"
      ? state.sessions.length
      : state.sessions.filter((x) => x.runtime === runtime).length;
    return `<button class="filter-chip ${state.runtime === runtime ? "active" : ""}"
      data-runtime="${runtime}">${runtime} · ${count}</button>`;
  }).join("");
  document.querySelectorAll("[data-runtime]").forEach((button) => {
    button.onclick = () => {
      state.runtime = button.dataset.runtime;
      renderFilters();
      renderSessionList();
    };
  });
}

function renderSessionList() {
  const sessions = filteredSessions();
  const online = sessions.filter((x) => x.presence === "online");
  const history = sessions.filter((x) => x.presence !== "online");
  const renderGroup = (label, items) => {
    if (!items.length) return "";
    return `<div class="group-label">${label} · ${items.length}</div>` + items.map((session) => `
      <button class="session-row ${session.session_uid === state.selectedUid ? "active" : ""}"
        data-session="${session.session_uid}">
        <div class="session-row-top">
          <div class="session-name">${escapeHtml(sessionName(session))}</div>
          <span class="runtime-badge ${session.runtime}">${escapeHtml(session.runtime)}</span>
        </div>
        <div class="session-title">${escapeHtml(session.effective_title)}</div>
        <div class="session-row-bottom">
          <span class="presence-dot ${session.presence}"></span>
          <span>${escapeHtml(session.project)}</span>
          <span>·</span>
          <span>${escapeHtml(session.managed ? "Hub chat" : session.attach_state)}</span>
        </div>
      </button>
    `).join("");
  };
  $("sessionList").innerHTML =
    renderGroup("Online", online)
    + renderGroup("History / Offline", history)
    || `<div class="empty-state"><p>没有符合条件的 session</p></div>`;
  document.querySelectorAll("[data-session]").forEach((button) => {
    button.onclick = () => {
      state.selectedUid = button.dataset.session;
      renderSessionList();
      renderDetail();
      loadMessages(button.dataset.session);
    };
  });
}

function fact(label, value) {
  return `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "—")}</dd>`;
}

function renderDetail() {
  const session = selectedSession();
  $("emptyState").classList.toggle("hidden", Boolean(session));
  $("sessionDetail").classList.toggle("hidden", !session);
  if (!session) return;

  $("detailTitle").textContent = session.effective_title;
  $("detailSubtitle").textContent = session.session_uid;
  $("detailPresence").textContent = `${session.presence} · ${session.status}`;
  $("detailBadges").innerHTML = `
    <span class="runtime-badge ${session.runtime}">${escapeHtml(session.runtime)}</span>
    <span class="mini-badge">${escapeHtml(session.attach_state)}</span>
    <span class="mini-badge">${escapeHtml(session.project)}</span>
    ${session.managed ? '<span class="mini-badge">Hub-managed</span>' : ""}
  `;
  $("aliasInput").value = session.alias || "";
  $("titleInput").value = session.user_title || "";
  $("roleInput").value = session.role || "";
  $("nativeNameInput").value = session.native_name || session.auto_native_name;
  $("linkSource").textContent = sessionName(session);

  const targets = state.sessions.filter((x) => x.session_uid !== session.session_uid);
  $("linkTarget").innerHTML = targets.map((target) =>
    `<option value="${target.session_uid}">${escapeHtml(sessionName(target))} · ${escapeHtml(target.runtime)}</option>`
  ).join("");
  $("createLinkButton").disabled = targets.length === 0;

  $("sessionFacts").innerHTML = [
    fact("Runtime ID", session.runtime_id),
    fact("Runtime version", session.runtime_version),
    fact("PID", session.pid),
    fact("cwd", session.cwd),
    fact("Process kind", session.process_kind),
    fact("Attach state", session.attach_state),
    fact("First seen", session.first_seen_at),
    fact("Last seen", session.last_seen_at),
  ].join("");
  $("rawMetadata").textContent = JSON.stringify({
    capabilities: session.capabilities,
    metadata: session.metadata,
    managed_config: session.managed_config,
  }, null, 2);
  renderChat();
}

async function loadMessages(uid) {
  const session = state.sessions.find((x) => x.session_uid === uid);
  if (!session?.managed) {
    state.messages[uid] = [];
    state.approvals[uid] = [];
    renderChat();
    return;
  }
  try {
    const data = await api(`/api/sessions/${uid}/messages`);
    state.messages[uid] = data.messages || [];
    state.approvals[uid] = data.approvals || [];
    if (uid === state.selectedUid) renderChat();
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderChat() {
  const session = selectedSession();
  if (!session) return;
  const managed = Boolean(session.managed);
  $("chatUnavailable").classList.toggle("hidden", managed);
  $("chatMessages").classList.toggle("hidden", !managed);
  $("approvalList").classList.toggle("hidden", !managed);
  $("chatForm").classList.toggle("hidden", !managed);
  $("chatRuntimeState").textContent = managed
    ? `${session.status} · ${session.transport || "managed"}`
    : "read only";
  $("chatHint").textContent = managed
    ? "当前对话由 Hub 保存；服务重启后发送下一条消息会自动恢复 session。"
    : "外部 session 保持原使用方式，不会被 Hub 写入。";
  if (!managed) return;

  const messages = state.messages[session.session_uid] || [];
  $("chatMessages").innerHTML = messages.length
    ? messages.map((message) => `
      <div class="message ${escapeHtml(message.role)}">
        <div class="message-meta">
          ${message.role === "human" ? "YOU" : message.role.toUpperCase()}
          · ${escapeHtml(message.status)}
        </div>
        <div class="message-bubble ${message.status === "streaming" ? "typing-cursor" : ""}">
          ${escapeHtml(message.content || (message.status === "streaming" ? "正在思考" : ""))}
        </div>
      </div>
    `).join("")
    : `<div class="chat-unavailable"><strong>新对话已就绪</strong><p>发送第一条消息后，runtime 才会开始模型推理。</p></div>`;

  const approvals = state.approvals[session.session_uid] || [];
  $("approvalList").innerHTML = approvals.map((approval) => `
    <div class="approval-card">
      <strong>Agent 请求批准：${escapeHtml(approval.method)}</strong>
      <pre>${escapeHtml(JSON.stringify(approval.params, null, 2))}</pre>
      <div class="approval-actions">
        <button class="button secondary" data-approval="${approval.approval_id}" data-action="decline">拒绝</button>
        <button class="button primary" data-approval="${approval.approval_id}" data-action="accept">允许</button>
      </div>
    </div>
  `).join("");
  document.querySelectorAll("[data-approval]").forEach((button) => {
    button.onclick = () => resolveApproval(button.dataset.approval, button.dataset.action);
  });

  const busy = ["running", "waiting_approval", "active"].includes(session.status);
  $("chatInput").disabled = busy;
  $("sendMessageButton").disabled = busy;
  $("sendMessageButton").textContent = busy ? "运行中" : "发送";
  requestAnimationFrame(() => {
    const container = $("chatMessages");
    container.scrollTop = container.scrollHeight;
  });
}

function findSessionName(uid) {
  const session = state.sessions.find((x) => x.session_uid === uid);
  return session ? sessionName(session) : uid.slice(0, 14);
}

function renderLinks() {
  if (!state.links.length) {
    $("linkList").innerHTML = `<div class="diagnostic-card"><p>还没有连接草稿。选择一个 session 后创建。</p></div>`;
    return;
  }
  $("linkList").innerHTML = state.links.map((link) => `
    <div class="link-card">
      <div class="link-card-top">
        <span class="mini-badge">${escapeHtml(link.status)}</span>
        <span class="runtime-badge">${escapeHtml(link.mode)}</span>
      </div>
      <div class="link-path">
        ${escapeHtml(findSessionName(link.source_session_uid))}
        <span style="color:var(--accent)"> → </span>
        ${escapeHtml(findSessionName(link.target_session_uid))}
      </div>
      <div class="link-meta">${escapeHtml(link.trigger_kind)} · 当前仅保存在 Hub</div>
    </div>
  `).join("");
}

function renderDiagnostics() {
  if (!state.diagnostics.length) {
    $("diagnostics").innerHTML = `<div class="diagnostic-card"><p>暂无诊断信息。</p></div>`;
    return;
  }
  $("diagnostics").innerHTML = state.diagnostics.map((item) => `
    <div class="diagnostic-card ${escapeHtml(item.level)}">
      <span class="runtime-badge ${escapeHtml(item.runtime)}">${escapeHtml(item.runtime)}</span>
      <p>${escapeHtml(item.message)}</p>
    </div>
  `).join("");
}

function render() {
  renderMetrics();
  renderFilters();
  renderSessionList();
  renderDetail();
  renderLinks();
  renderDiagnostics();
}

async function sendChatMessage(event) {
  event.preventDefault();
  const session = selectedSession();
  const text = $("chatInput").value.trim();
  if (!session?.managed || !text) return;
  $("sendMessageButton").disabled = true;
  try {
    await api(`/api/sessions/${session.session_uid}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    $("chatInput").value = "";
    await loadMessages(session.session_uid);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    renderChat();
  }
}

async function resolveApproval(approvalId, action) {
  try {
    await api(`/api/approvals/${approvalId}`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    await loadMessages(state.selectedUid);
    showToast(action === "accept" ? "已允许" : "已拒绝");
  } catch (error) {
    showToast(error.message, true);
  }
}

function openNewSessionDialog() {
  $("newSessionDialog").showModal();
}

function closeNewSessionDialog() {
  $("newSessionDialog").close();
}

async function createManagedSession(event) {
  event.preventDefault();
  const button = $("createSessionButton");
  button.disabled = true;
  button.textContent = "创建中…";
  try {
    const session = await api("/api/managed-sessions", {
      method: "POST",
      body: JSON.stringify({
        runtime: $("newRuntime").value,
        cwd: $("newCwd").value.trim(),
        alias: $("newAlias").value.trim() || null,
        title: $("newTitle").value.trim() || null,
        role: $("newRole").value.trim() || null,
        permission_profile: $("newPermission").value,
      }),
    });
    closeNewSessionDialog();
    const snapshot = await api("/api/snapshot");
    applySnapshot(snapshot);
    state.selectedUid = session.session_uid;
    render();
    $("newAlias").value = "";
    $("newTitle").value = "";
    $("newRole").value = "";
    showToast("Hub 对话已创建");
    await loadMessages(session.session_uid);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "创建并打开";
  }
}

async function saveNaming() {
  const session = selectedSession();
  if (!session) return;
  try {
    await api(`/api/sessions/${session.session_uid}`, {
      method: "PATCH",
      body: JSON.stringify({
        alias: $("aliasInput").value.trim() || null,
        user_title: $("titleInput").value.trim() || null,
        role: $("roleInput").value.trim() || null,
      }),
    });
    showToast("名称已保存");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function createLink() {
  const source = selectedSession();
  const target = $("linkTarget").value;
  if (!source || !target) return;
  try {
    await api("/api/links", {
      method: "POST",
      body: JSON.stringify({
        source_session_uid: source.session_uid,
        target_session_uid: target,
        mode: $("linkMode").value,
        trigger_kind: $("linkTrigger").value,
      }),
    });
    showToast("连接草稿已创建；未向 Agent 发送消息");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function scanNow() {
  const button = $("scanButton");
  button.disabled = true;
  button.textContent = "扫描中…";
  try {
    applySnapshot(await api("/api/scan", { method: "POST", body: "{}" }));
    showToast("扫描完成");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "立即扫描";
  }
}

function setupTheme() {
  const saved = localStorage.getItem("agenthub-theme") || "dark";
  if (saved === "white") document.documentElement.dataset.theme = "white";
  $("themeButton").onclick = () => {
    const white = document.documentElement.dataset.theme !== "white";
    document.documentElement.dataset.theme = white ? "white" : "dark";
    localStorage.setItem("agenthub-theme", white ? "white" : "dark");
  };
}

async function boot() {
  setupTheme();
  $("searchInput").oninput = (event) => {
    state.search = event.target.value;
    renderSessionList();
  };
  $("scanButton").onclick = scanNow;
  $("newSessionButton").onclick = openNewSessionDialog;
  $("closeDialogButton").onclick = closeNewSessionDialog;
  $("cancelNewSessionButton").onclick = closeNewSessionDialog;
  $("newSessionForm").onsubmit = createManagedSession;
  $("saveNamingButton").onclick = saveNaming;
  $("createLinkButton").onclick = createLink;
  $("chatForm").onsubmit = sendChatMessage;
  $("chatInput").onkeydown = (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      $("chatForm").requestSubmit();
    }
  };
  try {
    applySnapshot(await api("/api/snapshot"));
    if (state.selectedUid) await loadMessages(state.selectedUid);
  } catch (error) {
    showToast(error.message, true);
  }
  connectWebSocket();
}

boot();
