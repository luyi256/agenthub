import * as crypto from "node:crypto";
import { isIP } from "node:net";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { URL } from "node:url";
import * as vscode from "vscode";
import {
  HubClient,
  HubHealth,
  normalizeTmuxSocketName
} from "./hubClient";
import { currentWorkspaceIdentity, WorkspaceIdentity } from "./workspaceIdentity";

const MarkdownIt = require("markdown-it") as new (
  options?: Record<string, unknown>
) => {
  render(value: string): string;
  validateLink(value: string): boolean;
  renderer: {
    rules: Record<
      string,
      (
        tokens: any[],
        index: number,
        options: Record<string, unknown>,
        env: unknown,
        self: { renderToken(tokens: any[], index: number, options: unknown): string }
      ) => string
    >;
  };
};

type HubSession = {
  session_uid: string;
  runtime: string;
  runtime_id: string;
  alias?: string | null;
  effective_name: string;
  effective_title: string;
  status: string;
  presence: string;
  managed: boolean;
  transport?: string | null;
  cwd?: string | null;
  role?: string | null;
  capabilities?: Record<string, any>;
  metadata?: Record<string, any>;
  managed_config?: Record<string, any>;
};

type RuntimeModelOption = {
  id: string;
  label: string;
  description?: string;
  default?: boolean;
  reasoning_efforts?: string[];
  default_reasoning_effort?: string | null;
};

type RuntimeOption = {
  default_model?: string | null;
  models?: RuntimeModelOption[];
  custom_model?: boolean;
  reasoning_efforts?: string[];
};

type HubMessage = {
  message_id: string;
  role: "human" | "assistant" | "system";
  content: string;
  status: string;
  metadata?: Record<string, any>;
  rendered_content?: string;
};

type HubActivity = {
  activity_id: string;
  kind: "tool" | "plan" | "commentary";
  name: string;
  status: "running" | "completed" | "failed";
  input?: unknown;
  result?: string | null;
  input_preview?: string;
  result_preview?: string;
  has_details?: boolean;
  created_at: string;
  updated_at?: string;
  metadata?: Record<string, any>;
  rendered_input?: string;
};

type HubApproval = {
  approval_id: string;
  method: string;
  params: Record<string, any>;
  status: string;
};

type Snapshot = {
  sessions: HubSession[];
  counts: Record<string, number>;
  mode: string;
  runtime_options?: Record<string, RuntimeOption>;
};

type GenWindow = {
  tmux_session: "gen";
  window_id: string;
  window_index: number;
  tmux_name: string;
  display_name: string;
  custom_name?: string | null;
  active: boolean;
  pane_id: string;
  pane_pid: number;
  cwd: string;
  command: string;
  title: string;
  runtime: string;
  runtime_id: string;
  session_uid: string;
  adopted_session_uid?: string | null;
  chat_session_uid?: string | null;
  chat_status?: string | null;
  chat_transport?: string | null;
  state: "blocked" | "done" | "busy" | "idle";
  manual_attn?: "red" | "yellow" | null;
  attn_source?: "manual" | "automatic" | null;
  pane_count: number;
  model?: string | null;
  reasoning_effort?: string | null;
};

type GenSnapshot = {
  tmux_session: "gen";
  available: boolean;
  windows: GenWindow[];
  attn: {
    available: boolean;
    state_file: string;
    poll_seconds: number;
    hooked_server_pid?: string | null;
  };
};

type HubConnection = {
  client: HubClient;
  health?: HubHealth;
};

type ServerEndpoint = {
  baseUrl: string;
  protocol: "http:" | "https:";
  host: string;
  port: number;
};

type ProcessResult = {
  code: number;
  signal: NodeJS.Signals | null;
  stdout: string;
  stderr: string;
};

const COORDINATOR_TMUX_SESSION = "agenthub-mvp";
const HEALTH_WAIT_MILLISECONDS = 30_000;
const markdown = createMarkdownRenderer();

class AgentHubViewProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private readonly output: vscode.OutputChannel;
  private refreshTimer?: NodeJS.Timeout;
  private selectedSessionUid?: string;
  private workspace?: WorkspaceIdentity;
  private lastSnapshot?: Snapshot;
  private lastGenSnapshot?: GenSnapshot;
  private pendingCreate = false;
  private hubClient?: HubClient;
  private hubClientBaseUrl?: string;
  private serverHealth?: HubHealth;
  private serverSocketName?: string;
  private serverStartPromise?: Promise<void>;
  private refreshPromise?: Promise<void>;
  private refreshQueued = false;
  private refreshQueuedForceMessages = false;

  constructor(
    private readonly context: vscode.ExtensionContext,
    output: vscode.OutputChannel
  ) {
    this.output = output;
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.context.extensionUri]
    };
    view.webview.html = this.html(view.webview);
    view.webview.onDidReceiveMessage((message) =>
      this.handleMessage(view, message)
    );
    view.onDidChangeVisibility(() => {
      if (view.visible) {
        this.refreshSafely(true);
      }
    });
    this.startPolling();
    this.refreshSafely(true);
  }

  async reveal(): Promise<void> {
    await vscode.commands.executeCommand("agenthub.chatView.focus");
  }

  async createSessionFromCommand(): Promise<void> {
    await this.createSession({});
  }

  async openTmux(): Promise<void> {
    const workspace = currentWorkspaceIdentity();
    if (!workspace) {
      vscode.window.showWarningMessage("Open a workspace folder first.");
      return;
    }
    const connection = await this.connection();
    if (!connection.health || !this.serverSocketName) {
      throw new Error(this.offlineMessage());
    }
    const snapshot = await connection.client.get<Snapshot>("/api/snapshot");
    const sessions = this.sessionsForWorkspace(snapshot, workspace);
    this.lastSnapshot = { ...snapshot, sessions };
    const session =
      sessions.find(
        (candidate) => candidate.session_uid === this.selectedSessionUid
      ) ?? sessions[0];
    this.selectedSessionUid = session?.session_uid;
    const tmuxSession = session?.managed_config?.tmux_session;
    const tmuxWindow = session?.managed_config?.tmux_window;
    if (!tmuxSession) {
      vscode.window.showInformationMessage(
        "Create or select an Agent Hub session before opening its project tmux."
      );
      return;
    }
    const target = tmuxWindow
      ? `${tmuxSession}:${tmuxWindow}`
      : tmuxSession;
    const preflight = await runProcess("/usr/bin/env", [
      "-u",
      "TMUX",
      "tmux",
      "-L",
      this.serverSocketName,
      "has-session",
      "-t",
      tmuxSession
    ]);
    ensureExitCode(preflight, "Agent Hub project tmux lookup");
    const terminal = vscode.window.createTerminal({
      name: `Agent Hub · ${workspace.name}`,
      shellPath: "/usr/bin/env",
      shellArgs: [
        "-u",
        "TMUX",
        "tmux",
        "-L",
        this.serverSocketName,
        "attach-session",
        "-t",
        target
      ],
      cwd: vscode.Uri.file(workspace.cwd),
      iconPath: new vscode.ThemeIcon("terminal")
    });
    terminal.show(false);
  }

  async openGenTerminal(windowId: string): Promise<void> {
    const snapshot =
      this.lastGenSnapshot ??
      (await (await this.connectedClient()).get<GenSnapshot>(
        "/api/tmux/gen/windows"
      ));
    const window = snapshot.windows.find(
      (candidate) => candidate.window_id === windowId
    );
    if (!window) {
      throw new Error("该 tmux gen 会话已经关闭。");
    }
    const inspected = await runProcess("/usr/bin/env", [
      "-u",
      "TMUX",
      "tmux",
      "display-message",
      "-p",
      "-t",
      window.window_id,
      "#{session_name}\t#{window_id}\t#{pane_dead}"
    ]);
    ensureExitCode(inspected, "tmux gen 窗口检查");
    const [sessionName, actualWindowId, paneDead] = inspected.stdout
      .trim()
      .split("\t");
    if (
      sessionName !== "gen" ||
      actualWindowId !== window.window_id ||
      paneDead === "1"
    ) {
      throw new Error("该 tmux gen 会话已经关闭。");
    }
    const terminal = vscode.window.createTerminal({
      name: `gen:${window.window_index} · ${window.display_name}`,
      shellPath: "/usr/bin/env",
      shellArgs: [
        "-u",
        "TMUX",
        "tmux",
        "attach-session",
        "-t",
        window.window_id
      ],
      cwd: vscode.Uri.file(window.cwd),
      iconPath: new vscode.ThemeIcon("terminal")
    });
    terminal.show(false);
  }

  async openGenChat(windowId: string): Promise<void> {
    const result = await (await this.connectedClient()).post<{
      session: HubSession;
      messages: HubMessage[];
      window: GenWindow;
    }>(
      `/api/tmux/gen/windows/${encodeURIComponent(windowId)}/chat`,
      {}
    );
    this.selectedSessionUid = result.session.session_uid;
    await this.refresh(false);
    await this.loadMessages(this.selectedSessionUid);
    await this.refreshGenWindows(true);
    this.post({ type: "focusComposer" });
  }

  async renameGenWindow(
    windowId: string,
    currentName: string,
    providedName?: string
  ): Promise<void> {
    const name =
      providedName ??
      (await vscode.window.showInputBox({
        title: "给 tmux gen 会话命名",
        prompt: "名称只用于 Agent Hub 顶部标签；留空可恢复 tmux 原名。",
        value: currentName,
        validateInput: (value) =>
          value.trim().length > 40 ? "名称不能超过 40 个字符" : undefined
      }));
    if (name === undefined || name.trim().length > 40) {
      return;
    }
    await (await this.connectedClient()).patch(
      `/api/tmux/gen/windows/${encodeURIComponent(windowId)}`,
      { name: name.trim() || null }
    );
    await this.refreshGenWindows(true);
  }

  async setGenAttn(windowId: string, action: string): Promise<void> {
    if (!["red", "yellow", "clear"].includes(action)) {
      throw new Error("不支持的提醒操作。");
    }
    await (await this.connectedClient()).post(
      `/api/tmux/gen/windows/${encodeURIComponent(windowId)}/attn`,
      { action }
    );
    await this.refreshGenWindows(true);
  }

  async refresh(forceMessages = false): Promise<void> {
    this.workspace = currentWorkspaceIdentity();
    if (!this.workspace) {
      this.post({ type: "workspaceMissing" });
      return;
    }
    const connection = await this.connection();
    if (!connection.health) {
      this.post({ type: "serverOffline" });
      return;
    }
    const client = connection.client;
    await this.refreshGenWindows(false);
    const snapshot = await client.get<Snapshot>("/api/snapshot");
    const sessions = this.sessionsForWorkspace(snapshot, this.workspace);
    this.lastSnapshot = { ...snapshot, sessions };
    const liveRelaySessionUids = new Set(
      (this.lastGenSnapshot?.windows ?? [])
        .map((window) => window.chat_session_uid)
        .filter((uid): uid is string => Boolean(uid))
    );
    const selectableSessions = sessions.filter(
      (session) =>
        session.status !== "closed" &&
        (
          session.transport !== "gen-tmux-relay" ||
          liveRelaySessionUids.has(session.session_uid)
        )
    );
    if (
      !this.selectedSessionUid ||
      !selectableSessions.some(
        (session) => session.session_uid === this.selectedSessionUid
      )
    ) {
      this.selectedSessionUid =
        selectableSessions.find(
          (session) =>
            session.presence === "online" && session.status !== "stopped"
        )?.session_uid ?? selectableSessions[0]?.session_uid;
    }
    this.post({
      type: "snapshot",
      workspace: this.workspace,
      snapshot: this.lastSnapshot,
      selectedSessionUid: this.selectedSessionUid,
      enablePublicRuntimes: vscode.workspace
        .getConfiguration("agentHub")
        .get<boolean>("enablePublicRuntimes", false)
    });
    if (this.selectedSessionUid && (forceMessages || this.view?.visible)) {
      await this.loadMessages(this.selectedSessionUid);
    }
  }

  private async refreshGenWindows(force = false): Promise<void> {
    const rawSnapshot = await (await this.connectedClient()).get<GenSnapshot>(
      `/api/tmux/gen/windows${force ? "?refresh=1" : ""}`
    );
    const workspace = this.workspace ?? currentWorkspaceIdentity();
    const snapshot: GenSnapshot = {
      ...rawSnapshot,
      windows: workspace
        ? rawSnapshot.windows.filter(
            (window) =>
              path.resolve(window.cwd) === path.resolve(workspace.cwd)
          )
        : []
    };
    this.lastGenSnapshot = snapshot;
    this.post({ type: "genWindows", snapshot });
  }

  private async handleMessage(
    sourceView: vscode.WebviewView,
    message: any
  ): Promise<void> {
    if (sourceView !== this.view) {
      await this.postTo(sourceView, {
        type: "viewStale",
        message: "This Agent Hub view is stale. Reopen Agent Hub and try again."
      });
      return;
    }
    try {
      switch (message.type) {
        case "ready":
          await this.refresh(true);
          break;
        case "selectSession":
          this.selectedSessionUid = String(message.sessionUid);
          await this.loadMessages(this.selectedSessionUid);
          await this.refresh(false);
          break;
        case "createSession":
          await this.createSession(message.payload ?? {});
          break;
        case "sendMessage":
          await this.sendMessage(
            sourceView,
            String(message.text ?? ""),
            String(message.sessionUid ?? ""),
            String(message.requestId ?? "")
          );
          break;
        case "handoff":
          await this.handoff(
            sourceView,
            String(message.sourceSessionUid ?? ""),
            String(message.targetSessionUid ?? ""),
            String(message.targetWindowId ?? ""),
            String(message.text ?? ""),
            String(message.requestId ?? "")
          );
          break;
        case "copyText":
          await vscode.env.clipboard.writeText(String(message.text ?? ""));
          break;
        case "closeSession":
          await this.closeSession(
            String(message.sessionUid ?? ""),
            String(message.windowId ?? ""),
            String(message.name ?? "")
          );
          break;
        case "openExternal":
          await this.openExternal(String(message.href ?? ""));
          break;
        case "loadActivity":
          await this.loadActivity(
            sourceView,
            String(message.sessionUid ?? ""),
            String(message.activityId ?? "")
          );
          break;
        case "resolveApproval":
          await this.resolveApproval(
            String(message.approvalId),
            String(message.action)
          );
          break;
        case "openTmux":
          await this.openTmux();
          break;
        case "openGenWindow":
          await this.openGenChat(String(message.windowId ?? ""));
          break;
        case "openGenTerminal":
          await this.openGenTerminal(String(message.windowId ?? ""));
          break;
        case "renameGenWindow":
          await this.renameGenWindow(
            String(message.windowId ?? ""),
            String(message.currentName ?? ""),
            message.name === undefined ? undefined : String(message.name)
          );
          break;
        case "setGenAttn":
          await this.setGenAttn(
            String(message.windowId ?? ""),
            String(message.action ?? "")
          );
          break;
        case "refresh":
          await this.refreshGenWindows(true);
          await this.refresh(true);
          break;
        case "resetView":
          sourceView.webview.html = this.html(sourceView.webview);
          break;
        case "renameSession":
          await this.renameSession(
            String(message.sessionUid ?? ""),
            message.alias === undefined ? undefined : String(message.alias),
            String(message.currentName ?? "")
          );
          break;
      }
    } catch (error) {
      this.output.appendLine(String(error));
      await this.postTo(sourceView, {
        type: "error",
        message: errorMessage(error)
      });
    }
  }

  private async createSession(payload: Record<string, any>): Promise<void> {
    if (this.pendingCreate) {
      return;
    }
    const workspace = currentWorkspaceIdentity();
    if (!workspace) {
      vscode.window.showWarningMessage("Open a workspace folder first.");
      return;
    }
    this.pendingCreate = true;
    this.post({ type: "creatingSession", value: true });
    try {
      const configuration = vscode.workspace.getConfiguration("agentHub");
      const runtime =
        payload.runtime ??
        configuration.get<string>("defaultRuntime", "tcodex");
      const permission =
        payload.permission_profile ??
        configuration.get<string>("defaultPermission", "safe");
      const short = crypto.randomBytes(2).toString("hex");
      const alias =
        String(payload.alias ?? "").trim() ||
        `${aliasSlug(workspace.name)}/${runtime}-${short}`;
      const title =
        String(payload.title ?? "").trim() ||
        `${runtime} · ${workspace.name}`;
      const cwd =
        String(payload.cwd ?? "").trim() || workspace.cwd;
      const session = await (await this.connectedClient()).post<HubSession>(
        "/api/managed-sessions",
        {
          runtime,
          cwd,
          alias,
          title,
          role: String(payload.role ?? "").trim() || null,
          permission_profile: permission,
          model: String(payload.model ?? "").trim() || null,
          reasoning_effort:
            String(payload.reasoning_effort ?? "").trim() || null,
          workspace_id: workspace.id,
          workspace_name: workspace.name,
          use_tmux: true
        }
      );
      this.selectedSessionUid = session.session_uid;
      await this.refresh(true);
      this.post({ type: "focusComposer" });
    } finally {
      this.pendingCreate = false;
      this.post({ type: "creatingSession", value: false });
    }
  }

  private async sendMessage(
    sourceView: vscode.WebviewView,
    text: string,
    sessionUid: string,
    requestId: string
  ): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed) {
      throw new Error("Message cannot be empty.");
    }
    if (!sessionUid || !requestId) {
      throw new Error("The Agent Hub view sent an incomplete message request.");
    }
    await this.postTo(sourceView, {
      type: "sendStarted",
      requestId,
      sessionUid
    });
    try {
      const workspace = currentWorkspaceIdentity();
      if (!workspace) {
        throw new Error("Open a workspace folder first.");
      }
      const snapshot = await (await this.connectedClient()).get<Snapshot>(
        "/api/snapshot"
      );
      const sessions = this.sessionsForWorkspace(snapshot, workspace);
      if (!sessions.some((session) => session.session_uid === sessionUid)) {
        throw new Error(
          "The selected Agent Hub session no longer belongs to this workspace."
        );
      }
      this.selectedSessionUid = sessionUid;
      const result = await (await this.connectedClient()).post<{
        delivery?: string;
      }>(
        `/api/sessions/${encodeURIComponent(sessionUid)}/messages`,
        { text: trimmed }
      );
      await this.postTo(sourceView, {
        type: "sendAccepted",
        requestId,
        sessionUid,
        delivery: result.delivery ?? "turn"
      });
      await this.loadMessages(sessionUid, sourceView);
      await this.refresh(false);
    } catch (error) {
      await this.postTo(sourceView, {
        type: "sendFailed",
        requestId,
        sessionUid,
        message: errorMessage(error)
      });
      try {
        await this.loadMessages(sessionUid, sourceView);
      } catch {
        // Keep the explicit send failure visible when the server is unreachable.
      }
      throw error;
    }
  }

  private async loadMessages(
    sessionUid: string,
    targetView: vscode.WebviewView | undefined = this.view
  ): Promise<void> {
    const data = await (await this.connectedClient()).get<{
      messages: HubMessage[];
      activities: HubActivity[];
      approvals: HubApproval[];
    }>(
      `/api/sessions/${encodeURIComponent(sessionUid)}/messages`
    );
    await this.postTo(targetView, {
      type: "messages",
      sessionUid,
      messages: data.messages.map((message) => ({
        ...message,
        rendered_content: markdown.render(
          message.content ||
            (message.status === "streaming" ? "正在思考…" : "")
          )
      })),
      activities: (data.activities ?? []).map((activity) => ({
        ...activity,
        rendered_input:
          activity.kind === "plan" || activity.kind === "commentary"
            ? markdown.render(planText(activity.input))
            : undefined
      })),
      approvals: data.approvals
    });
  }

  private async handoff(
    sourceView: vscode.WebviewView,
    sourceSessionUid: string,
    targetSessionUid: string,
    targetWindowId: string,
    text: string,
    requestId: string
  ): Promise<void> {
    const trimmed = text.trim();
    if (
      !sourceSessionUid ||
      (!targetSessionUid && !targetWindowId) ||
      requestId.length === 0
    ) {
      throw new Error("协作消息缺少来源、目标或请求编号。");
    }
    if (!trimmed) {
      throw new Error("协作消息不能为空。");
    }
    await this.postTo(sourceView, {
      type: "handoffStarted",
      requestId,
      sourceSessionUid,
      targetSessionUid
    });
    let resolvedTargetUid = targetSessionUid;
    try {
      const workspace = currentWorkspaceIdentity();
      if (!workspace) {
        throw new Error("请先打开一个 VS Code 项目文件夹。");
      }
      const snapshot = await (await this.connectedClient()).get<Snapshot>(
        "/api/snapshot"
      );
      const sessions = this.sessionsForWorkspace(snapshot, workspace);
      const source = sessions.find(
        (session) => session.session_uid === sourceSessionUid
      );
      if (!source) {
        throw new Error("来源会话已关闭，或不属于当前项目。");
      }
      if (!resolvedTargetUid && targetWindowId) {
        if (
          !this.lastGenSnapshot?.windows.some(
            (window) => window.window_id === targetWindowId
          )
        ) {
          throw new Error("目标 tmux 会话已关闭，或不属于当前项目。");
        }
        const imported = await (await this.connectedClient()).post<{
          session: HubSession;
        }>(
          `/api/tmux/gen/windows/${encodeURIComponent(targetWindowId)}/chat`,
          {}
        );
        resolvedTargetUid = imported.session.session_uid;
      }
      const refreshedSnapshot = await (
        await this.connectedClient()
      ).get<Snapshot>("/api/snapshot");
      const refreshedSessions = this.sessionsForWorkspace(
        refreshedSnapshot,
        workspace
      );
      const target = refreshedSessions.find(
        (session) => session.session_uid === resolvedTargetUid
      );
      if (!target) {
        throw new Error("目标会话已关闭，或不属于当前项目。");
      }
      await (await this.connectedClient()).post(
        `/api/sessions/${encodeURIComponent(sourceSessionUid)}/handoff`,
        {
          target_session_uid: resolvedTargetUid,
          text: trimmed,
          mode: "user_message"
        }
      );
      await this.postTo(sourceView, {
        type: "handoffAccepted",
        requestId,
        sourceSessionUid,
        targetSessionUid: resolvedTargetUid
      });
      await this.refresh(false);
    } catch (error) {
      await this.postTo(sourceView, {
        type: "handoffFailed",
        requestId,
        sourceSessionUid,
        targetSessionUid: resolvedTargetUid,
        message: errorMessage(error)
      });
      throw error;
    }
  }

  private async loadActivity(
    sourceView: vscode.WebviewView,
    sessionUid: string,
    activityId: string
  ): Promise<void> {
    if (!sessionUid || !activityId) {
      throw new Error("工具详情请求不完整。");
    }
    const workspace = currentWorkspaceIdentity();
    if (!workspace) {
      throw new Error("请先打开一个 VS Code 项目文件夹。");
    }
    const snapshot = await (await this.connectedClient()).get<Snapshot>(
      "/api/snapshot"
    );
    if (
      !this.sessionsForWorkspace(snapshot, workspace).some(
        (session) => session.session_uid === sessionUid
      )
    ) {
      throw new Error("该会话已关闭，或不属于当前项目。");
    }
    try {
      const activity = await (await this.connectedClient()).get<HubActivity>(
        `/api/sessions/${encodeURIComponent(
          sessionUid
        )}/activities/${encodeURIComponent(activityId)}`
      );
      await this.postTo(sourceView, {
        type: "activityDetail",
        sessionUid,
        activity
      });
    } catch (error) {
      await this.postTo(sourceView, {
        type: "activityDetailFailed",
        sessionUid,
        activityId,
        message: errorMessage(error)
      });
      throw error;
    }
  }

  private async closeSession(
    sessionUid: string,
    windowId: string,
    name: string
  ): Promise<void> {
    if (!sessionUid && !windowId) {
      throw new Error("关闭请求缺少 session 或窗口标识。");
    }
    const workspace = currentWorkspaceIdentity();
    if (!workspace) {
      throw new Error("请先打开一个 VS Code 项目文件夹。");
    }
    const label = name.trim() || "当前会话";
    const confirmed = await vscode.window.showWarningMessage(
      `确定关闭「${label}」吗？其运行进程会停止，顶部标签会被移除。`,
      { modal: true },
      "关闭会话"
    );
    if (confirmed !== "关闭会话") {
      return;
    }
    if (windowId) {
      const window = this.lastGenSnapshot?.windows.find(
        (candidate) => candidate.window_id === windowId
      );
      if (!window || path.resolve(window.cwd) !== path.resolve(workspace.cwd)) {
        throw new Error("该 tmux gen 窗口已关闭，或不属于当前项目。");
      }
      await (await this.connectedClient()).delete(
        `/api/tmux/gen/windows/${encodeURIComponent(windowId)}`
      );
    } else {
      const snapshot = await (await this.connectedClient()).get<Snapshot>(
        "/api/snapshot"
      );
      if (
        !this.sessionsForWorkspace(snapshot, workspace).some(
          (session) => session.session_uid === sessionUid
        )
      ) {
        throw new Error("该会话已关闭，或不属于当前项目。");
      }
      await (await this.connectedClient()).delete(
        `/api/sessions/${encodeURIComponent(sessionUid)}`
      );
    }
    if (this.selectedSessionUid === sessionUid) {
      this.selectedSessionUid = undefined;
    }
    await this.refreshGenWindows(true);
    await this.refresh(true);
    this.post({ type: "sessionClosed", sessionUid, windowId });
  }

  private async resolveApproval(
    approvalId: string,
    action: string
  ): Promise<void> {
    await (await this.connectedClient()).post(
      `/api/approvals/${encodeURIComponent(approvalId)}`,
      { action }
    );
    if (this.selectedSessionUid) {
      await this.loadMessages(this.selectedSessionUid);
    }
  }

  private async renameSession(
    sessionUid: string,
    providedAlias?: string,
    currentName = ""
  ): Promise<void> {
    if (!sessionUid) {
      return;
    }
    const alias =
      providedAlias ??
      (await vscode.window.showInputBox({
        title: "修改会话名称",
        prompt: "名称用于 Agent Hub 顶部标签。",
        value: currentName,
        validateInput: (value) =>
          value.trim().length === 0
            ? "名称不能为空"
            : value.trim().length > 80
              ? "名称不能超过 80 个字符"
              : undefined
      }));
    if (alias === undefined || !alias.trim() || alias.trim().length > 80) {
      return;
    }
    const workspace = currentWorkspaceIdentity();
    if (!workspace) {
      throw new Error("请先打开一个 VS Code 项目文件夹。");
    }
    const snapshot = await (await this.connectedClient()).get<Snapshot>(
      "/api/snapshot"
    );
    if (
      !this.sessionsForWorkspace(snapshot, workspace).some(
        (session) => session.session_uid === sessionUid
      )
    ) {
      throw new Error("该会话已关闭，或不属于当前项目。");
    }
    await (await this.connectedClient()).patch(
      `/api/sessions/${encodeURIComponent(sessionUid)}`,
      { alias: alias.trim() }
    );
    await this.refresh(false);
  }

  private async openExternal(href: string): Promise<void> {
    let uri: vscode.Uri;
    try {
      uri = vscode.Uri.parse(href, true);
    } catch {
      throw new Error("链接格式无效。");
    }
    if (!["http", "https", "mailto"].includes(uri.scheme.toLowerCase())) {
      throw new Error("只允许打开 http、https 或 mailto 链接。");
    }
    await vscode.env.openExternal(uri);
  }

  private currentSession(): HubSession | undefined {
    return this.lastSnapshot?.sessions.find(
      (session) => session.session_uid === this.selectedSessionUid
    );
  }

  private sessionsForWorkspace(
    snapshot: Snapshot,
    workspace: WorkspaceIdentity
  ): HubSession[] {
    return snapshot.sessions.filter(
      (session) =>
        session.managed &&
        (session.managed_config?.workspace_id === workspace.id.slice(0, 10) ||
          path.resolve(session.cwd || "") === path.resolve(workspace.cwd))
    );
  }

  private configuredClient(baseUrl: string): HubClient {
    if (!this.hubClient || this.hubClientBaseUrl !== baseUrl) {
      this.hubClient = new HubClient(baseUrl);
      this.hubClientBaseUrl = baseUrl;
      this.serverHealth = undefined;
      this.serverSocketName = undefined;
    }
    return this.hubClient;
  }

  private async connection(): Promise<HubConnection> {
    const configuration = vscode.workspace.getConfiguration("agentHub");
    const endpoint = parseServerEndpoint(
      configuration.get<string>("serverUrl", "http://127.0.0.1:8766")
    );
    const client = this.configuredClient(endpoint.baseUrl);
    let health = await this.readHealth(client);
    if (
      !health &&
      configuration.get<boolean>("autoStartServer", true)
    ) {
      await this.startServerSingleflight(configuration, endpoint, client);
      health = await this.readHealth(client);
    }
    return { client, health };
  }

  private async connectedClient(): Promise<HubClient> {
    const connection = await this.connection();
    if (!connection.health) {
      throw new Error(this.offlineMessage());
    }
    return connection.client;
  }

  private async readHealth(
    client: HubClient,
    timeoutMilliseconds = 2_000
  ): Promise<HubHealth | undefined> {
    try {
      const health = await client.healthDetails(timeoutMilliseconds);
      if (!health.ok) {
        this.serverHealth = undefined;
        return undefined;
      }
      this.serverHealth = health;
      this.serverSocketName = health.tmux_socket_name;
      return health;
    } catch {
      this.serverHealth = undefined;
      return undefined;
    }
  }

  private async startServerSingleflight(
    configuration: vscode.WorkspaceConfiguration,
    endpoint: ServerEndpoint,
    client: HubClient
  ): Promise<void> {
    if (!this.serverStartPromise) {
      const start = this.startServer(configuration, endpoint, client);
      const tracked = start.finally(() => {
        if (this.serverStartPromise === tracked) {
          this.serverStartPromise = undefined;
        }
      });
      this.serverStartPromise = tracked;
    }
    await this.serverStartPromise;
  }

  private async startServer(
    configuration: vscode.WorkspaceConfiguration,
    endpoint: ServerEndpoint,
    client: HubClient
  ): Promise<void> {
    if (endpoint.protocol !== "http:" || !isLoopbackHost(endpoint.host)) {
      throw new Error(
        `Agent Hub auto-start is allowed only for loopback HTTP URLs; configured URL is ${endpoint.baseUrl}`
      );
    }
    const project = configuration.get<string>(
      "serverProjectPath",
      "/home/luyi/agenthub"
    );
    const socketName =
      this.serverSocketName ?? bootstrapTmuxSocketName(configuration);
    const runCommand = [
      "env",
      `AGENTHUB_TMUX_SOCKET=${shellQuote(socketName)}`,
      "bash",
      shellQuote(path.join(project, "agent_hub/run.sh")),
      "--host",
      shellQuote(endpoint.host),
      "--port",
      String(endpoint.port)
    ].join(" ");
    const hasSession = await runProcess("/usr/bin/env", [
      "-u",
      "TMUX",
      "tmux",
      "-L",
      socketName,
      "has-session",
      "-t",
      COORDINATOR_TMUX_SESSION
    ]);
    if (hasSession.code === 0) {
      const paneState = await runProcess("/usr/bin/env", [
        "-u",
        "TMUX",
        "tmux",
        "-L",
        socketName,
        "display-message",
        "-p",
        "-t",
        COORDINATOR_TMUX_SESSION,
        "#{pane_dead}"
      ]);
      ensureExitCode(paneState, "Agent Hub coordinator pane inspection");
      const paneDead = paneState.stdout.trim();
      if (paneDead === "1") {
        const respawn = await runProcess("/usr/bin/env", [
          "-u",
          "TMUX",
          "tmux",
          "-L",
          socketName,
          "respawn-pane",
          "-t",
          COORDINATOR_TMUX_SESSION,
          "-c",
          project,
          runCommand
        ]);
        if (respawn.code !== 0) {
          const racedState = await coordinatorPaneDead(socketName);
          if (racedState !== false) {
            ensureExitCode(respawn, "Agent Hub coordinator pane restart");
          }
        }
      } else if (paneDead !== "0") {
        throw new Error(
          `Agent Hub coordinator returned an invalid pane state: ${JSON.stringify(
            paneState.stdout
          )}`
        );
      } else {
        this.output.appendLine(
          "Agent Hub coordinator pane is already live; waiting for health without restarting it."
        );
      }
    } else {
      const created = await runProcess("/usr/bin/env", [
        "-u",
        "TMUX",
        "tmux",
        "-L",
        socketName,
        "new-session",
        "-d",
        "-s",
        COORDINATOR_TMUX_SESSION,
        "-c",
        project,
        runCommand
      ]);
      if (created.code !== 0) {
        const racedSession = await runProcess("/usr/bin/env", [
          "-u",
          "TMUX",
          "tmux",
          "-L",
          socketName,
          "has-session",
          "-t",
          COORDINATOR_TMUX_SESSION
        ]);
        if (racedSession.code !== 0) {
          ensureExitCode(created, "Agent Hub coordinator tmux creation");
        }
      }
    }
    const deadline = Date.now() + HEALTH_WAIT_MILLISECONDS;
    while (Date.now() < deadline) {
      if (await this.readHealth(client, 1_000)) {
        return;
      }
      await delay(250);
    }
    throw new Error(
      `Agent Hub server did not become healthy within ${
        HEALTH_WAIT_MILLISECONDS / 1000
      } seconds. The live coordinator was not force-restarted.`
    );
  }

  private offlineMessage(): string {
    const autoStart = vscode.workspace
      .getConfiguration("agentHub")
      .get<boolean>("autoStartServer", true);
    return autoStart
      ? "Agent Hub server is offline."
      : "Agent Hub server is offline and agentHub.autoStartServer is disabled.";
  }

  private startPolling(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
    }
    this.refreshTimer = setInterval(() => {
      if (this.view?.visible) {
        this.refreshSafely(true);
      }
    }, 1300);
    this.context.subscriptions.push({
      dispose: () => {
        if (this.refreshTimer) {
          clearInterval(this.refreshTimer);
        }
      }
    });
  }

  private refreshSafely(forceMessages: boolean): void {
    if (this.refreshPromise) {
      this.refreshQueued = true;
      this.refreshQueuedForceMessages ||= forceMessages;
      return;
    }
    const run = this.refresh(forceMessages)
      .catch((error) => {
        this.output.appendLine(errorMessage(error));
        this.post({ type: "error", message: errorMessage(error) });
      })
      .finally(() => {
        if (this.refreshPromise === run) {
          this.refreshPromise = undefined;
        }
        if (this.refreshQueued) {
          const queuedForce = this.refreshQueuedForceMessages;
          this.refreshQueued = false;
          this.refreshQueuedForceMessages = false;
          this.refreshSafely(queuedForce);
        }
      });
    this.refreshPromise = run;
  }

  private post(message: unknown): void {
    void this.postTo(this.view, message);
  }

  private async postTo(
    view: vscode.WebviewView | undefined,
    message: unknown
  ): Promise<boolean> {
    if (!view) {
      return false;
    }
    try {
      return await view.webview.postMessage(message);
    } catch (error) {
      this.output.appendLine(
        `Failed to update Agent Hub webview: ${errorMessage(error)}`
      );
      return false;
    }
  }

  private html(webview: vscode.Webview): string {
    const nonce = crypto.randomBytes(16).toString("base64");
    const version = String(this.context.extension.packageJSON.version);
    return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
  <style>${styles}</style>
</head>
<body>
  <header class="tab-bar">
    <div class="conversation-tabs" id="conversationTabs" role="tablist">
      <span class="tabs-loading">正在读取会话…</span>
    </div>
    <button class="icon-button history-sessions hidden" id="historySessions" type="button" title="恢复历史会话" aria-label="恢复历史会话">↶</button>
    <button class="icon-button add-session" id="newSession" type="button" title="新建对话" aria-label="新建对话">＋</button>
  </header>
  <section class="conversation-head">
    <div class="conversation-identity">
      <div class="agent-mark" id="agentMark">AH</div>
      <div class="conversation-title-wrap">
        <div class="conversation-title-row">
          <strong id="conversationTitle">Agent Hub</strong>
          <span class="version">v${version}</span>
        </div>
        <div class="conversation-subtitle">
          <span class="status-dot" id="runtimeDot"></span>
          <span id="status">正在连接…</span>
          <span class="subtitle-separator">·</span>
          <span id="sessionTags"></span>
        </div>
      </div>
    </div>
    <div class="head-actions">
      <button class="head-button" id="handoffOpen" type="button" title="发送协作消息给其他会话">协作</button>
      <button class="icon-button" id="refresh" type="button" title="刷新">↻</button>
      <button class="icon-button" id="currentMenuButton" type="button" title="会话设置" aria-label="会话设置">⋯</button>
    </div>
  </section>
  <div class="workspace-line">
    <span class="service-state" id="serviceState"><span class="service-dot"></span></span>
    <span id="workspaceMeta">正在连接…</span>
  </div>
  <div class="transcript-shell">
    <main class="transcript" id="transcript">
      <div class="welcome" id="welcome">
        <div class="mark">AH</div>
        <h2>在一个界面管理所有 Agent</h2>
        <p>顶部选择会话，下面直接继续对话。任务在后台 tmux 中持续运行，不需要打开终端。</p>
        <button class="primary" id="welcomeNew" type="button">新建第一个对话</button>
      </div>
    </main>
    <button class="new-messages hidden" id="newMessages" type="button">↓ 回到底部</button>
  </div>
  <section class="approvals" id="approvals"></section>
  <form class="composer" id="composer">
    <textarea id="input" rows="3" placeholder="给当前 Agent 发送消息…"></textarea>
        <div class="composer-footer">
      <div class="composer-context">
        <span class="composer-model" id="composerModel">未选择模型</span>
        <span class="composer-hint" id="composerHint">请先新建或选择一个对话</span>
      </div>
      <button class="send" id="send" type="submit">发送</button>
    </div>
  </form>
  <dialog id="newDialog">
    <form method="dialog" id="newForm">
      <div class="dialog-head">
        <div><strong>新建对话</strong><p>对话在当前项目的独立后台 tmux window 中持续运行，日常操作都在本界面完成。</p></div>
        <button type="button" id="closeDialog" aria-label="关闭">×</button>
      </div>
      <label>Agent 类型
        <select id="newRuntime">
          <option value="tcodex">tcodex（推荐，适合编码和执行任务）</option>
          <option value="tclaude">tclaude（适合分析和写作）</option>
        </select>
      </label>
      <label>模型
        <select id="newModel"></select>
      </label>
      <label id="newReasoningLabel">推理强度
        <select id="newReasoning"></select>
      </label>
      <label>对话名称
        <input id="newAlias" placeholder="例如：视频生成、Prompt 优化">
      </label>
      <label>工作目录
        <input id="newCwd">
      </label>
      <label>权限
        <select id="newPermission">
          <option value="safe">安全模式（推荐，需要时询问）</option>
          <option value="read-only">只读模式</option>
          <option value="full-access">完全访问（谨慎使用）</option>
        </select>
      </label>
      <div class="dialog-actions">
        <button class="secondary" type="button" id="cancelCreate">取消</button>
        <button class="primary create" type="submit" id="create">创建对话</button>
      </div>
    </form>
  </dialog>
  <dialog id="handoffDialog">
    <form method="dialog" id="handoffForm">
      <div class="dialog-head">
        <div><strong>发送给其他会话</strong><p>目标 Agent 会收到一条带来源标记的普通用户消息，不会伪装成系统指令。</p></div>
        <button type="button" id="closeHandoff" aria-label="关闭">×</button>
      </div>
      <label>目标会话
        <select id="handoffTarget"></select>
      </label>
      <label>要转发的内容
        <textarea id="handoffText" rows="6" placeholder="例如：Prompt 已生成，文件在 /path/to/prompt.md，请读取后继续生成视频。"></textarea>
      </label>
      <div class="handoff-tools">
        <button class="ghost" type="button" id="useLatestReply">填入当前 Agent 最新回复</button>
      </div>
      <div class="dialog-actions">
        <button class="secondary" type="button" id="cancelHandoff">取消</button>
        <button class="primary create" type="submit" id="sendHandoff">发送协作消息</button>
      </div>
    </form>
  </dialog>
  <dialog id="historyDialog">
    <div class="history-dialog">
      <div class="dialog-head">
        <div><strong>历史会话</strong><p>已暂停的会话不占顶部标签；选择后可查看历史，继续发送时会自动恢复后台 worker。</p></div>
        <button type="button" id="closeHistory" aria-label="关闭">×</button>
      </div>
      <div class="history-list" id="historyList"></div>
    </div>
  </dialog>
  <div class="toast" id="toast"></div>
  <div class="window-menu hidden" id="windowMenu" role="menu">
    <button type="button" data-window-action="rename">修改会话名称</button>
    <button type="button" data-window-action="handoff">发送给其他会话</button>
    <button class="danger-action" type="button" data-window-action="close">关闭会话</button>
    <div class="window-menu-separator"></div>
    <button type="button" data-window-action="red">标红：待处理</button>
    <button type="button" data-window-action="yellow">标黄：待验收</button>
    <button type="button" data-window-action="clear">清除提醒</button>
    <div class="window-menu-separator debug-only"></div>
    <button class="debug-only" type="button" data-window-action="terminal">调试：打开原 tmux 终端</button>
    <button type="button" data-window-action="reset">重置插件界面</button>
  </div>
  <script nonce="${nonce}">${script}</script>
</body>
</html>`;
  }
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const output = vscode.window.createOutputChannel("Agent Hub");
  output.appendLine(
    `Agent Hub extension ${String(context.extension.packageJSON.version)} activated.`
  );
  const provider = new AgentHubViewProvider(context, output);
  context.subscriptions.push(output);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("agenthub.chatView", provider, {
      webviewOptions: { retainContextWhenHidden: true }
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("agenthub.focus", () => provider.reveal()),
    vscode.commands.registerCommand("agenthub.newSession", () =>
      provider.createSessionFromCommand()
    ),
    vscode.commands.registerCommand("agenthub.openTmux", () =>
      provider.openTmux()
    ),
    vscode.commands.registerCommand("agenthub.refresh", () =>
      provider.refresh(true)
    ),
    vscode.commands.registerCommand("agenthub.openLogs", () => output.show())
  );
}

export function deactivate(): void {}

function aliasSlug(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 30) || "workspace"
  );
}

export function parseServerEndpoint(value: string): ServerEndpoint {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`Invalid agentHub.serverUrl: ${value}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(
      `agentHub.serverUrl must use http or https; received ${url.protocol}`
    );
  }
  if (url.username || url.password) {
    throw new Error("agentHub.serverUrl must not include credentials.");
  }
  if (url.pathname !== "/" || url.search || url.hash) {
    throw new Error(
      "agentHub.serverUrl must contain only scheme, host, and optional port."
    );
  }
  const port =
    url.port === ""
      ? url.protocol === "https:"
        ? 443
        : 80
      : Number(url.port);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`Invalid port in agentHub.serverUrl: ${value}`);
  }
  const host = url.hostname.replace(/^\[|\]$/g, "");
  url.pathname = "/";
  url.search = "";
  url.hash = "";
  return {
    baseUrl: url.toString(),
    protocol: url.protocol,
    host,
    port
  };
}

export function isLoopbackHost(host: string): boolean {
  const normalized = host.toLowerCase().replace(/^\[|\]$/g, "");
  if (normalized === "localhost") {
    return true;
  }
  const ipVersion = isIP(normalized);
  if (ipVersion === 4) {
    return normalized.startsWith("127.");
  }
  return ipVersion === 6 && normalized === "::1";
}

function bootstrapTmuxSocketName(
  configuration: vscode.WorkspaceConfiguration
): string {
  return normalizeTmuxSocketName(
    configuration.get<string>("tmuxSocketName", "agenthub")
  );
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function coordinatorPaneDead(
  socketName: string
): Promise<boolean | undefined> {
  const state = await runProcess("/usr/bin/env", [
    "-u",
    "TMUX",
    "tmux",
    "-L",
    socketName,
    "display-message",
    "-p",
    "-t",
    COORDINATOR_TMUX_SESSION,
    "#{pane_dead}"
  ]);
  if (state.code !== 0) {
    return undefined;
  }
  const value = state.stdout.trim();
  return value === "1" ? true : value === "0" ? false : undefined;
}

function runProcess(command: string, args: string[]): Promise<ProcessResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe"]
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout?.on("data", (chunk) => stdout.push(Buffer.from(chunk)));
    child.stderr?.on("data", (chunk) => stderr.push(Buffer.from(chunk)));
    child.once("error", reject);
    child.once("exit", (code, signal) =>
      resolve({
        code: code ?? 1,
        signal,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8")
      })
    );
  });
}

function ensureExitCode(result: ProcessResult, operation: string): void {
  if (result.code === 0) {
    return;
  }
  const details =
    result.stderr.trim() ||
    result.stdout.trim() ||
    (result.signal ? `signal ${result.signal}` : `exit code ${result.code}`);
  throw new Error(`${operation} failed: ${details}`);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function createMarkdownRenderer(): InstanceType<typeof MarkdownIt> {
  const renderer = new MarkdownIt({
    html: false,
    linkify: true,
    breaks: false,
    typographer: false
  });
  renderer.validateLink = (value: string) =>
    /^(https?:|mailto:)/i.test(value.trim());
  renderer.renderer.rules.link_open = (
    tokens,
    index,
    options,
    _env,
    self
  ) => {
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
    return self.renderToken(tokens, index, options);
  };
  return renderer;
}

function planText(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (!value || typeof value !== "object") {
    return String(value ?? "");
  }
  const record = value as Record<string, unknown>;
  const parts: string[] = [];
  if (typeof record.explanation === "string" && record.explanation.trim()) {
    parts.push(`> ${record.explanation.trim()}`);
  }
  if (Array.isArray(record.plan)) {
    const lines = record.plan.map((item) => {
      if (!item || typeof item !== "object") {
        return `- ${String(item)}`;
      }
      const planItem = item as Record<string, unknown>;
      const status = String(planItem.status ?? "pending");
      const marker =
        status === "completed"
          ? "x"
          : status === "in_progress"
            ? "~"
            : " ";
      return `- [${marker}] ${String(planItem.step ?? "")}`;
    });
    parts.push(lines.join("\n"));
  }
  if (parts.length) {
    return parts.join("\n\n");
  }
  try {
    return "```json\n" + JSON.stringify(value, null, 2) + "\n```";
  } catch {
    return String(value);
  }
}

const styles = String.raw`
:root {
  color-scheme: light dark;
  --bg: var(--vscode-sideBar-background);
  --panel: var(--vscode-editorWidget-background, var(--bg));
  --fg: var(--vscode-foreground);
  --muted: var(--vscode-descriptionForeground);
  --line: var(--vscode-widget-border, rgba(127, 127, 127, .25));
  --input: var(--vscode-input-background);
  --input-fg: var(--vscode-input-foreground);
  --button: var(--vscode-button-background);
  --button-fg: var(--vscode-button-foreground);
  --button-hover: var(--vscode-button-hoverBackground);
  --hover: var(--vscode-list-hoverBackground);
  --focus: var(--vscode-focusBorder);
  --success: var(--vscode-testing-iconPassed, #3fb950);
  --warning: var(--vscode-editorWarning-foreground, #d29922);
  --error: var(--vscode-errorForeground, #f85149);
  --code-bg: var(--vscode-textCodeBlock-background, rgba(127, 127, 127, .12));
  font-family: var(--vscode-font-family);
}

* { box-sizing: border-box; }

html,
body {
  width: 100%;
  height: 100%;
  margin: 0;
  overflow: hidden;
  background: var(--bg);
  color: var(--fg);
}

body {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: auto auto auto minmax(0, 1fr) auto auto;
  min-width: 0;
  max-width: 100vw;
  font-size: 13px;
}

body > * {
  min-width: 0;
  max-width: 100%;
}

button,
input,
select,
textarea { font: inherit; }

button { cursor: pointer; }

button,
.meta,
.activity-heading,
.composer-footer,
.conversation-head,
.tab-bar,
.workspace-line {
  user-select: none;
}

.transcript,
.bubble,
.bubble * {
  user-select: text;
}

button:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible {
  outline: 1px solid var(--focus);
  outline-offset: 1px;
}

.tab-bar {
  display: flex;
  align-items: stretch;
  min-width: 0;
  height: 43px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}

.conversation-tabs {
  display: flex;
  flex: 1;
  min-width: 0;
  gap: 2px;
  overflow-x: auto;
  overflow-y: hidden;
  padding: 5px 4px 0 6px;
  scrollbar-width: thin;
}

.tabs-loading,
.tabs-empty {
  align-self: center;
  padding: 0 6px 5px;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}

.conversation-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  flex: 1 1 160px;
  gap: 6px;
  min-width: clamp(96px, 28vw, 140px);
  height: 37px;
  padding: 0 10px;
  border: 0;
  border-radius: 7px 7px 0 0;
  background: transparent;
  color: var(--muted);
}

.conversation-tab:hover { background: var(--hover); color: var(--fg); }

.conversation-tab.selected {
  background: var(--bg);
  color: var(--fg);
}

.conversation-tab.selected::after {
  position: absolute;
  right: 7px;
  bottom: 0;
  left: 7px;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: var(--button);
  content: "";
}

.conversation-tab.blocked { color: var(--error); }
.conversation-tab.done { color: var(--warning); }

.tab-status {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 50%;
  background: var(--muted);
}

.conversation-tab.busy .tab-status,
.conversation-tab.starting .tab-status { background: var(--warning); }
.conversation-tab.blocked .tab-status,
.conversation-tab.error .tab-status { background: var(--error); }
.conversation-tab.done .tab-status { background: var(--warning); }
.conversation-tab.idle .tab-status { background: var(--success); }

.tab-icon {
  width: 17px;
  height: 17px;
  display: grid;
  place-items: center;
  flex: none;
  border: 1px solid var(--line);
  border-radius: 5px;
  color: var(--fg);
  font-size: 8px;
  font-weight: 800;
  letter-spacing: -.2px;
}

.tab-name {
  overflow: hidden;
  font-size: 11px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tab-attn {
  flex: none;
  font-size: 10px;
  font-weight: 800;
}

.tab-close {
  width: 17px;
  height: 17px;
  display: grid;
  place-items: center;
  flex: none;
  margin-right: -4px;
  border-radius: 4px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1;
  opacity: .35;
}

.conversation-tab:hover .tab-close,
.conversation-tab.selected .tab-close { opacity: .8; }
.tab-close:hover { background: var(--hover); color: var(--error); opacity: 1; }

.icon-button,
.head-button {
  border: 0;
  background: transparent;
  color: var(--muted);
}

.icon-button:hover,
.head-button:hover { background: var(--hover); color: var(--fg); }

.add-session,
.history-sessions {
  width: 37px;
  flex: none;
  margin: 5px 0;
  border-left: 1px solid var(--line);
  border-radius: 6px;
  color: var(--fg);
  font-size: 18px;
}

.add-session { margin-right: 5px; }
.history-sessions { font-size: 16px; }

.conversation-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-width: 0;
  gap: 10px;
  padding: 10px 10px 7px;
}

.conversation-identity {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 9px;
}

.agent-mark {
  width: 31px;
  height: 31px;
  display: grid;
  place-items: center;
  flex: none;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--panel);
  color: var(--fg);
  font-size: 10px;
  font-weight: 800;
}

.conversation-title-wrap { min-width: 0; }

.conversation-title-row {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 6px;
}

.conversation-title-row strong {
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.version {
  padding: 1px 5px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  font-size: 8px;
}

.conversation-subtitle {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 5px;
  margin-top: 3px;
  overflow: hidden;
  color: var(--muted);
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.subtitle-separator { opacity: .55; }

.status-dot,
.service-dot {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 50%;
  background: var(--muted);
}

.status-dot.ready,
.service-state.online .service-dot { background: var(--success); }
.status-dot.busy { background: var(--warning); }
.status-dot.error,
.service-state.offline .service-dot { background: var(--error); }

.head-actions {
  display: flex;
  align-items: center;
  flex: none;
  gap: 2px;
}

.head-actions .icon-button {
  width: 27px;
  height: 27px;
  border-radius: 6px;
  font-size: 16px;
}

.head-button {
  height: 27px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--fg);
  font-size: 10px;
}

.head-button:disabled { cursor: default; opacity: .4; }

.workspace-line {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 6px;
  padding: 0 11px 8px;
  overflow: hidden;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.service-state { display: inline-flex; align-items: center; }

.transcript-shell {
  position: relative;
  width: 100%;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

.transcript {
  width: 100%;
  max-width: 100%;
  height: 100%;
  min-height: 0;
  min-width: 0;
  overflow: auto;
  overflow-x: hidden;
  padding: 15px 11px 40px;
  scrollbar-width: thin;
  overflow-anchor: none;
}

.welcome {
  min-height: 260px;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  padding: 24px 14px;
  color: var(--muted);
  text-align: center;
}

.welcome .mark {
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  margin-bottom: 14px;
  border: 1px solid var(--line);
  border-radius: 13px;
  color: var(--fg);
  font-weight: 800;
}

.welcome h2 { margin: 0; color: var(--fg); font-size: 15px; }
.welcome p { max-width: 310px; margin: 8px 0 16px; font-size: 11px; line-height: 1.65; }

.primary,
.send {
  border: 0;
  background: var(--button);
  color: var(--button-fg);
  font-weight: 600;
}

.primary:hover,
.send:hover { background: var(--button-hover); }

.secondary,
.ghost {
  border: 1px solid var(--line);
  background: transparent;
  color: var(--fg);
}

.secondary:hover,
.ghost:hover { background: var(--hover); }
.ghost { color: var(--muted); }
.welcome .primary { padding: 7px 12px; border-radius: 6px; }

.conversation-empty,
.interaction-blocked {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin: 18px 2px;
  padding: 12px;
  border: 1px dashed var(--line);
  border-radius: 9px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
}

.conversation-empty strong,
.interaction-blocked strong { color: var(--fg); }

.interaction-blocked {
  border-style: solid;
  border-color: var(--warning);
  background: color-mix(in srgb, var(--warning) 7%, transparent);
}

.message {
  display: flex;
  margin: 0 0 20px;
}

.message.user { justify-content: flex-end; }
.message-inner { min-width: 0; max-width: 100%; }
.message.user .message-inner { max-width: 90%; }

.meta {
  margin: 0 2px 5px;
  color: var(--muted);
  font-size: 9px;
}

.message.user .meta { text-align: right; }

.bubble {
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
  color: var(--fg);
  line-height: 1.62;
}

.message.user .bubble {
  padding: 8px 10px;
  border: 1px solid var(--vscode-inputOption-activeBorder, var(--line));
  border-radius: 11px 11px 3px 11px;
  background: var(--vscode-inputOption-activeBackground, var(--input));
}

.message.system .bubble {
  padding-left: 9px;
  border-left: 2px solid var(--warning);
  color: var(--muted);
  font-size: 11px;
}

.activity {
  display: flex;
  gap: 8px;
  margin: 0 0 14px;
  color: var(--muted);
}

.activity-rail {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  flex: none;
  margin-top: 1px;
  border: 1px solid var(--line);
  border-radius: 6px;
  font-size: 9px;
  font-weight: 800;
}

.activity-rail.plan {
  border-color: color-mix(in srgb, var(--vscode-charts-purple, #a371f7) 55%, var(--line));
  color: var(--vscode-charts-purple, #a371f7);
}

.activity-rail.commentary {
  border-color: color-mix(in srgb, var(--vscode-charts-blue, #58a6ff) 55%, var(--line));
  color: var(--vscode-charts-blue, #58a6ff);
}

.activity-content { min-width: 0; flex: 1; }

.activity-heading {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 20px;
  font-size: 11px;
}

.activity-heading strong { color: var(--fg); font-weight: 650; }

.activity-status {
  flex: none;
  color: var(--muted);
  font-size: 9px;
}

.activity-status.running { color: var(--warning); }
.activity-status.completed { color: var(--success); }
.activity-status.failed { color: var(--error); }

.plan-activity {
  padding: 8px;
  border: 1px solid color-mix(in srgb, var(--vscode-charts-purple, #a371f7) 28%, var(--line));
  border-radius: 8px;
  background: color-mix(in srgb, var(--vscode-charts-purple, #a371f7) 5%, transparent);
}

.plan-body,
.commentary-body {
  margin-top: 5px;
  color: var(--fg);
  font-size: 11px;
}

.plan-body ul,
.plan-body ol { margin: .35em 0; }

.commentary-activity {
  padding: 2px 4px;
}

.commentary-body { color: var(--muted); }

details.tool-activity {
  display: block;
  min-width: 0;
  max-width: calc(100% - 27px);
  margin: 0 0 9px 27px;
  border-left: 1px solid var(--line);
  padding-left: 8px;
}

details.tool-activity > summary {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 6px;
  min-height: 24px;
  cursor: pointer;
  list-style: none;
  color: var(--muted);
}

details.tool-activity > summary::-webkit-details-marker { display: none; }

.tool-chevron {
  flex: none;
  color: var(--success);
  font-size: 15px;
  line-height: 1;
  transform-origin: center;
  transition: transform .12s;
}

details.tool-activity[open] .tool-chevron { transform: rotate(90deg); }

.tool-icon {
  width: 17px;
  height: 17px;
  display: grid;
  place-items: center;
  flex: none;
  border-radius: 5px;
  background: color-mix(in srgb, var(--success) 12%, transparent);
  color: var(--success);
  font-size: 9px;
}

.tool-name {
  flex: none;
  max-width: 34%;
  overflow: hidden;
  color: var(--fg);
  font-size: 10px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-preview {
  min-width: 0;
  margin-left: auto;
  overflow: hidden;
  color: var(--muted);
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-details {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  min-width: 0;
  max-width: 100%;
  gap: 7px;
  margin: 5px 0 10px 28px;
}

.tool-section > span {
  display: block;
  margin-bottom: 3px;
  color: var(--muted);
  font-size: 9px;
}

.tool-section { min-width: 0; max-width: 100%; }

.tool-section pre {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  max-height: 340px;
  margin: 0;
  overflow: auto;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--code-bg);
  color: var(--fg);
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: 10px;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.tool-pending {
  color: var(--muted);
  font-size: 10px;
}

.bubble > :first-child { margin-top: 0; }
.bubble > :last-child { margin-bottom: 0; }
.bubble p { margin: 0 0 .72em; }
.bubble h1,
.bubble h2,
.bubble h3,
.bubble h4 {
  margin: 1.15em 0 .55em;
  color: var(--fg);
  line-height: 1.3;
}
.bubble h1 { font-size: 1.32em; }
.bubble h2 { font-size: 1.2em; }
.bubble h3 { font-size: 1.1em; }
.bubble ul,
.bubble ol { margin: .45em 0 .75em; padding-left: 1.6em; }
.bubble li { margin: .2em 0; }
.bubble blockquote {
  margin: .7em 0;
  padding: .05em 0 .05em .85em;
  border-left: 3px solid var(--line);
  color: var(--muted);
}
.bubble a { color: var(--vscode-textLink-foreground); text-decoration: none; }
.bubble a:hover { text-decoration: underline; }
.bubble code {
  padding: .12em .35em;
  border-radius: 4px;
  background: var(--code-bg);
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: .92em;
}
.bubble pre {
  position: relative;
  max-width: 100%;
  margin: .75em 0;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--code-bg);
}
.bubble pre code {
  display: block;
  min-width: max-content;
  padding: 10px;
  background: transparent;
  white-space: pre;
}
.copy-code {
  position: sticky;
  top: 5px;
  float: right;
  z-index: 1;
  margin: 5px 5px -30px 0;
  padding: 3px 6px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--panel);
  color: var(--muted);
  font-size: 9px;
}
.bubble table {
  display: block;
  width: max-content;
  max-width: 100%;
  margin: .7em 0;
  overflow: auto;
  border-collapse: collapse;
}
.bubble th,
.bubble td { padding: 5px 7px; border: 1px solid var(--line); text-align: left; }
.bubble th { background: var(--panel); }
.bubble hr { border: 0; border-top: 1px solid var(--line); }
.bubble img { max-width: 100%; height: auto; }

.typing::after {
  display: inline-block;
  width: 6px;
  height: 12px;
  margin-left: 3px;
  vertical-align: -1px;
  background: var(--vscode-progressBar-background);
  animation: blink 1s step-end infinite;
  content: "";
}

@keyframes blink { 50% { opacity: 0; } }

.new-messages {
  position: absolute;
  right: 12px;
  bottom: 10px;
  z-index: 5;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--vscode-notifications-background, var(--panel));
  color: var(--vscode-notifications-foreground, var(--fg));
  box-shadow: 0 4px 14px rgba(0, 0, 0, .22);
  font-size: 10px;
}

.hidden { display: none !important; }

.approvals { min-width: 0; max-width: 100%; padding: 0 9px; }
.approval {
  margin-bottom: 7px;
  padding: 9px;
  border: 1px solid var(--warning);
  border-radius: 7px;
  background: var(--panel);
}
.approval-title { font-size: 11px; font-weight: 650; }
.approval-reason { margin: 5px 0; color: var(--muted); font-size: 10px; white-space: pre-wrap; }
.approval-actions { display: flex; justify-content: flex-end; gap: 6px; }
.approval button { padding: 4px 8px; border: 1px solid var(--line); border-radius: 5px; background: transparent; color: var(--fg); }
.approval button.allow { border: 0; background: var(--button); color: var(--button-fg); }

.composer {
  min-width: 0;
  max-width: calc(100% - 16px);
  margin: 0 8px 8px;
  overflow: hidden;
  border: 1px solid var(--vscode-input-border, var(--line));
  border-radius: 10px;
  background: var(--input);
}

.composer:focus-within { border-color: var(--focus); }

textarea {
  display: block;
  width: 100%;
  min-height: 66px;
  max-height: 190px;
  resize: none;
  padding: 10px;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--input-fg);
  line-height: 1.5;
}

.composer-footer {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 6px 6px;
}

.composer-context {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 1px;
}

.composer-model {
  overflow: hidden;
  color: var(--fg);
  font-size: 10px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-hint {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  color: var(--muted);
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.send {
  min-width: 58px;
  flex: none;
  padding: 6px 11px;
  border-radius: 6px;
}

.send:disabled,
.primary:disabled { cursor: default; opacity: .45; }

.toast {
  position: fixed;
  right: 10px;
  bottom: 92px;
  left: 10px;
  z-index: 40;
  padding: 9px 10px;
  transform: translateY(8px);
  border: 1px solid var(--vscode-notifications-border, var(--line));
  border-radius: 7px;
  background: var(--vscode-notifications-background, var(--panel));
  color: var(--vscode-notifications-foreground, var(--fg));
  opacity: 0;
  pointer-events: none;
  transition: .14s;
}

.toast.show { transform: none; opacity: 1; }

dialog {
  width: calc(100% - 22px);
  max-width: 430px;
  padding: 0;
  border: 1px solid var(--vscode-widget-border, var(--line));
  border-radius: 10px;
  background: var(--vscode-editorWidget-background, var(--panel));
  color: var(--fg);
}

dialog::backdrop { background: rgba(0, 0, 0, .5); }

#newForm,
#handoffForm,
.history-dialog {
  display: grid;
  gap: 12px;
  padding: 14px;
}

.dialog-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.dialog-head strong { font-size: 14px; }
.dialog-head p { margin: 4px 0 0; color: var(--muted); font-size: 10px; line-height: 1.5; }
.dialog-head button { border: 0; background: transparent; color: var(--fg); font-size: 18px; }

label {
  display: grid;
  gap: 5px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 600;
}

input,
dialog select {
  width: 100%;
  height: 31px;
  padding: 0 7px;
  border: 1px solid var(--vscode-input-border, var(--line));
  border-radius: 5px;
  background: var(--input);
  color: var(--input-fg);
}

#handoffText {
  min-height: 116px;
  border: 1px solid var(--vscode-input-border, var(--line));
  border-radius: 6px;
}

.handoff-tools { display: flex; }
.handoff-tools button { padding: 5px 8px; border-radius: 5px; font-size: 10px; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 7px; margin-top: 3px; }
.dialog-actions button { padding: 6px 11px; border-radius: 5px; }

.history-list { display: grid; gap: 6px; max-height: 55vh; overflow: auto; }
.history-item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 3px 8px;
  width: 100%;
  padding: 8px 9px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: transparent;
  color: var(--fg);
  text-align: left;
}
.history-item:hover { background: var(--hover); }
.history-item-mark {
  grid-row: 1 / span 2;
  align-self: center;
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 6px;
  font-size: 8px;
  font-weight: 800;
}
.history-item-name { overflow: hidden; font-size: 11px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.history-item-meta { color: var(--muted); font-size: 9px; }

.window-menu {
  position: fixed;
  z-index: 50;
  min-width: 178px;
  padding: 4px;
  border: 1px solid var(--vscode-menu-border, var(--line));
  border-radius: 7px;
  background: var(--vscode-menu-background, var(--panel));
  color: var(--vscode-menu-foreground, var(--fg));
  box-shadow: 0 8px 24px rgba(0, 0, 0, .28);
}

.window-menu button {
  display: block;
  width: 100%;
  padding: 6px 8px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: inherit;
  text-align: left;
  font-size: 11px;
}

.window-menu button:hover { background: var(--vscode-menu-selectionBackground, var(--hover)); }
.window-menu .danger-action { color: var(--error); }
.window-menu-separator { height: 1px; margin: 4px; background: var(--line); }
.window-menu .debug-only { color: var(--muted); font-size: 9px; }

@media (max-width: 290px) {
  .conversation-tab { min-width: 84px; padding: 0 7px; }
  .conversation-head { padding-right: 7px; padding-left: 7px; }
  .head-button { padding: 0 6px; }
  .version,
  .subtitle-separator,
  #sessionTags { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .typing::after,
  .toast { animation: none; transition: none; }
}
`;

const script = String.raw`
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
`;
