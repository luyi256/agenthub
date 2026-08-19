import { createHash } from "node:crypto";
import * as path from "node:path";
import * as vscode from "vscode";

export interface WorkspaceIdentity {
  id: string;
  name: string;
  cwd: string;
  folders: Array<{ name: string; uri: string; fsPath: string }>;
  remoteAuthority: string;
}

export function currentWorkspaceIdentity(): WorkspaceIdentity | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders?.length) {
    return undefined;
  }
  const activeUri = vscode.window.activeTextEditor?.document.uri;
  const activeFolder = activeUri
    ? vscode.workspace.getWorkspaceFolder(activeUri)
    : undefined;
  const primary = activeFolder ?? folders[0];
  const workspaceFile = vscode.workspace.workspaceFile?.toString() ?? "";
  const remoteAuthority = vscode.env.remoteName ?? "local";
  const normalized = [...folders]
    .map((folder) => folder.uri.toString())
    .sort()
    .join("\n");
  const id = createHash("sha256")
    .update(`${remoteAuthority}\n${workspaceFile}\n${normalized}`)
    .digest("hex")
    .slice(0, 16);
  const name = workspaceFile
    ? path.basename(vscode.workspace.workspaceFile!.fsPath, path.extname(vscode.workspace.workspaceFile!.fsPath))
    : primary.name;
  return {
    id,
    name,
    cwd: primary.uri.fsPath,
    remoteAuthority,
    folders: folders.map((folder) => ({
      name: folder.name,
      uri: folder.uri.toString(),
      fsPath: folder.uri.fsPath
    }))
  };
}
