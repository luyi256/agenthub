"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");

const root = path.resolve(__dirname, "..");
const extensionSource = fs.readFileSync(
  path.join(root, "src", "extension.ts"),
  "utf8"
);

function loadTranspiledTypeScript(file) {
  const filename = path.join(root, file);
  const source = fs.readFileSync(filename, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      esModuleInterop: true
    },
    fileName: filename,
    reportDiagnostics: true
  });
  assert.deepEqual(output.diagnostics ?? [], []);
  const loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  loaded._compile(output.outputText, filename);
  return loaded.exports;
}

function loadBundledExtension() {
  const filename = path.join(root, "dist", "extension.js");
  const originalLoad = Module._load;
  Module._load = function mockVscode(request, parent, isMain) {
    if (request === "vscode") {
      return {};
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    delete require.cache[filename];
    return require(filename);
  } finally {
    Module._load = originalLoad;
  }
}

test("health socket names are validated before tmux use", () => {
  const { normalizeTmuxSocketName } = loadTranspiledTypeScript(
    "src/hubClient.ts"
  );
  assert.equal(normalizeTmuxSocketName("agenthub-custom_1"), "agenthub-custom_1");
  for (const value of ["", "default", "../default", "socket name", "x".repeat(49)]) {
    assert.throws(() => normalizeTmuxSocketName(value), /unsafe/);
  }
});

test("server URL parsing and loopback checks are explicit", () => {
  const { isLoopbackHost, parseServerEndpoint } = loadBundledExtension();
  assert.deepEqual(parseServerEndpoint("http://127.0.0.1:9876"), {
    baseUrl: "http://127.0.0.1:9876/",
    protocol: "http:",
    host: "127.0.0.1",
    port: 9876
  });
  assert.equal(isLoopbackHost("localhost"), true);
  assert.equal(isLoopbackHost("127.9.8.7"), true);
  assert.equal(isLoopbackHost("::1"), true);
  assert.equal(isLoopbackHost("0.0.0.0"), false);
  assert.equal(isLoopbackHost("example.com"), false);
  assert.throws(
    () => parseServerEndpoint("http://127.0.0.1:8766/api"),
    /scheme, host/
  );
});

test("tmux paths use the reported socket and clear inherited TMUX", () => {
  assert.match(
    extensionSource,
    /this\.serverSocketName = health\.tmux_socket_name/
  );
  assert.match(extensionSource, /shellPath: "\/usr\/bin\/env"/);
  assert.match(
    extensionSource,
    /shellArgs:\s*\[\s*"-u",\s*"TMUX",\s*"tmux",\s*"-L",\s*this\.serverSocketName/
  );
  assert.doesNotMatch(extensionSource, /shellPath:\s*"tmux"/);
  assert.doesNotMatch(extensionSource, /"-L",\s*"agenthub"/);
  assert.doesNotMatch(extensionSource, /`ah-\$\{/);
});

test("auto-start is singleflight and never force-respawns a live pane", () => {
  assert.match(extensionSource, /serverStartPromise\?: Promise<void>/);
  assert.match(extensionSource, /await this\.serverStartPromise/);
  assert.match(extensionSource, /#\{pane_dead\}/);
  assert.match(extensionSource, /HEALTH_WAIT_MILLISECONDS = 30_000/);
  assert.doesNotMatch(extensionSource, /"-k"/);
  assert.match(extensionSource, /configuration\.get<boolean>\("autoStartServer", true\)/);
  assert.match(extensionSource, /"\/home\/luyi\/agenthub"/);
  assert.doesNotMatch(
    extensionSource,
    /configuration\.get<string>\(\s*"serverProjectPath",\s*"\/home\/luyi\/generation"/
  );
});

test("one webview owns its session-bound send lifecycle", () => {
  assert.equal(
    (extensionSource.match(/registerWebviewViewProvider\(/g) ?? []).length,
    1
  );
  assert.doesNotMatch(extensionSource, /agenthub\.chatViewFallback/);
  assert.match(
    extensionSource,
    /sendMessage\(\s*sourceView,\s*String\(message\.text \?\? ""\),\s*String\(message\.sessionUid \?\? ""\),\s*String\(message\.requestId \?\? ""\)/
  );
  assert.match(extensionSource, /type: "sendStarted"/);
  assert.match(extensionSource, /type: "sendAccepted"/);
  assert.match(extensionSource, /type: "sendFailed"/);
  assert.match(extensionSource, /private refreshPromise\?: Promise<void>/);
});

test("unified session tabs keep tmux gen chat, naming, and attn controls", () => {
  assert.match(extensionSource, /\/api\/tmux\/gen\/windows/);
  assert.match(extensionSource, /conversationEntries/);
  assert.match(extensionSource, /data-entry-key/);
  assert.match(
    extensionSource,
    /path\.resolve\(window\.cwd\) === path\.resolve\(workspace\.cwd\)/
  );
  assert.match(
    extensionSource,
    /\.filter\(\(session\) => session\.transport !== "gen-tmux-relay"\)/
  );
  assert.match(extensionSource, /function historicalSessions\(\)/);
  assert.match(extensionSource, /id="historyDialog"/);
  assert.match(extensionSource, /已暂停，发送时自动恢复/);
  assert.match(extensionSource, /renameGenWindow/);
  assert.match(extensionSource, /setGenAttn/);
  assert.match(extensionSource, /openGenChat/);
  assert.match(extensionSource, /\/chat/);
  assert.match(extensionSource, /顶部选择会话，下面直接继续对话/);
  assert.match(extensionSource, /标红：待处理/);
  assert.match(extensionSource, /标黄：待验收/);
  assert.match(
    extensionSource,
    /"tmux",\s*"attach-session",\s*"-t",\s*window\.window_id/
  );
});

test("message rendering preserves scroll position and renders safe markdown", () => {
  assert.match(extensionSource, /const markdown = createMarkdownRenderer\(\)/);
  assert.match(extensionSource, /html: false/);
  assert.match(extensionSource, /rendered_content: markdown\.render/);
  assert.match(extensionSource, /captureScrollAnchor/);
  assert.match(extensionSource, /state\.stickToBottom/);
  assert.match(extensionSource, /state\.unreadCount/);
  assert.match(extensionSource, /回到底部/);
  assert.doesNotMatch(
    extensionSource,
    /function renderMessages\(\)[\s\S]{0,7000}root\.scrollTop = root\.scrollHeight;\s*}/
  );
});

test("top session tabs preserve manual horizontal scroll during polling", () => {
  assert.match(extensionSource, /tabScrollLeft: 0/);
  assert.match(extensionSource, /renderedTabKey: null/);
  assert.match(extensionSource, /const savedScrollLeft = state\.tabScrollLeft/);
  assert.match(
    extensionSource,
    /root\.scrollLeft = Math\.min\(savedScrollLeft, maximum\)/
  );
  assert.match(
    extensionSource,
    /\$\("conversationTabs"\)\.addEventListener\("scroll"/
  );
  assert.doesNotMatch(extensionSource, /selected\.scrollIntoView/);
});

test("top session windows flex with the sidebar width", () => {
  assert.match(extensionSource, /\.conversation-tab\s*\{[\s\S]*?flex: 1 1 160px/);
  assert.match(
    extensionSource,
    /min-width: clamp\(96px, 28vw, 140px\)/
  );
  assert.doesNotMatch(
    extensionSource,
    /\.conversation-tab\s*\{[\s\S]*?max-width: 185px/
  );
});

test("conversation layout can shrink after rendering wide content", () => {
  assert.match(
    extensionSource,
    /grid-template-columns: minmax\(0, 1fr\)/
  );
  assert.match(
    extensionSource,
    /body > \*\s*\{\s*min-width: 0;\s*max-width: 100%;/
  );
  assert.match(
    extensionSource,
    /\.transcript-shell\s*\{[\s\S]*?min-width: 0;[\s\S]*?overflow: hidden;/
  );
  assert.match(
    extensionSource,
    /\.transcript\s*\{[\s\S]*?max-width: 100%;[\s\S]*?overflow-x: hidden;/
  );
  assert.match(
    extensionSource,
    /\.composer\s*\{[\s\S]*?max-width: calc\(100% - 16px\)/
  );
  assert.match(extensionSource, /function stabilizeAfterResize\(\)/);
  assert.match(extensionSource, /new ResizeObserver\(stabilizeAfterResize\)/);
  assert.match(extensionSource, /document\.documentElement\.scrollLeft = 0/);
  assert.match(extensionSource, /\$\("transcript"\)\.scrollLeft = 0/);
});

test("session handoff is explicit user-message routing", () => {
  assert.match(extensionSource, /case "handoff"/);
  assert.match(
    extensionSource,
    /\/api\/sessions\/\$\{encodeURIComponent\(sourceSessionUid\)\}\/handoff/
  );
  assert.match(extensionSource, /target_session_uid: resolvedTargetUid/);
  assert.match(extensionSource, /发送给其他会话/);
  assert.match(extensionSource, /普通用户消息/);
  assert.match(
    extensionSource,
    /state\.handoffSending = true;\s*state\.handoffRequestId = id;\s*\$\("sendHandoff"\)\.disabled = true/
  );
});

test("CLI plan and tool activity is visible with collapsed result previews", () => {
  assert.match(extensionSource, /activities: HubActivity\[\]/);
  assert.match(extensionSource, /rendered_input:/);
  assert.match(extensionSource, /function timelineEntries\(\)/);
  assert.match(extensionSource, /function renderActivity\(/);
  assert.match(extensionSource, /details class="activity tool-activity"/);
  assert.match(extensionSource, /function firstLine\(value\)/);
  assert.match(extensionSource, /工具结果/);
  assert.match(extensionSource, /<strong>Plan<\/strong>/);
  assert.match(extensionSource, /case "loadActivity"/);
  assert.match(extensionSource, /type: "activityDetail"/);
  assert.match(extensionSource, /展开后加载完整参数和结果/);
  assert.match(extensionSource, /activity\.result_preview/);
});

test("messages can be added while the agent is running", () => {
  assert.match(extensionSource, /running \? "追加" : "发送"/);
  assert.match(extensionSource, /运行中可继续发送/);
  assert.match(extensionSource, /pendingSends: new Map\(\)/);
  assert.match(extensionSource, /message\.delivery === "steered"/);
  assert.match(extensionSource, /message\.delivery === "hub_queued"/);
  assert.match(extensionSource, /message\.delivery === "runtime_queued"/);
  assert.doesNotMatch(
    extensionSource,
    /\["running", "active", "waiting_approval", "starting"\]\.includes\(session\.status\)/
  );
});

test("sessions can be explicitly closed from the top tabs", () => {
  assert.match(extensionSource, /case "closeSession"/);
  assert.match(extensionSource, /private async closeSession\(/);
  assert.match(extensionSource, /data-close-entry/);
  assert.match(extensionSource, /data-window-action="close"/);
  assert.match(extensionSource, /关闭会话/);
  assert.match(extensionSource, /\.delete\(/);
  assert.match(extensionSource, /session\.status !== "closed"/);
});

test("new sessions expose model selection and show the active model", () => {
  assert.match(extensionSource, /id="newModel"/);
  assert.match(extensionSource, /id="newReasoning"/);
  assert.match(extensionSource, /runtime_options/);
  assert.match(extensionSource, /model:\s*\$\("newModel"\)\.value/);
  assert.match(
    extensionSource,
    /reasoning_effort:\s*\$\("newReasoning"\)\.value/
  );
  assert.match(extensionSource, /id="composerModel"/);
  assert.match(extensionSource, /function sessionModel\(/);
  assert.match(extensionSource, /metadata\?\.worker/);
});

test("transcript polling preserves an active text selection", () => {
  assert.match(extensionSource, /selectionInsideTranscript/);
  assert.match(extensionSource, /transcriptRenderPending/);
  assert.match(extensionSource, /document\.addEventListener\("selectionchange"/);
  assert.match(extensionSource, /\.bubble \*\s*\{\s*user-select: text/);
});

test("IME composition Enter commits text without sending the message", () => {
  assert.match(extensionSource, /compositionstart/);
  assert.match(extensionSource, /compositionend/);
  assert.match(extensionSource, /event\.isComposing/);
  assert.match(extensionSource, /event\.keyCode === 229/);
  assert.match(extensionSource, /lastCompositionEndAt/);
});

test("past records are searchable across the current workspace", () => {
  assert.match(extensionSource, /case "searchRecords"/);
  assert.match(extensionSource, /\/api\/search\/messages/);
  assert.match(extensionSource, /id="searchDialog"/);
  assert.match(extensionSource, /function renderSearchResults\(\)/);
  assert.match(extensionSource, /highlightSearchExcerpt/);
  assert.match(extensionSource, /包括已关闭会话/);
  assert.match(extensionSource, /event\.key\.toLocaleLowerCase\(\) === "f"/);
});
