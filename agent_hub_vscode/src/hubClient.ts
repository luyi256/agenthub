import * as http from "node:http";
import * as https from "node:https";
import { URL } from "node:url";

export type JsonValue = unknown;
export type HubHealth = {
  ok: boolean;
  mode: string;
  db: string;
  scan_interval: number;
  tmux_socket_name: string;
};

export function normalizeTmuxSocketName(value: unknown): string {
  const socketName = typeof value === "string" ? value.trim() : "";
  if (
    socketName === "default" ||
    !/^[A-Za-z0-9_.-]{1,48}$/.test(socketName)
  ) {
    throw new Error(
      "Agent Hub returned an unsafe tmux_socket_name; refusing to run tmux."
    );
  }
  return socketName;
}

export class HubClient {
  constructor(readonly baseUrl: string) {}

  async get<T>(path: string, timeoutMilliseconds?: number): Promise<T> {
    return this.request<T>("GET", path, undefined, timeoutMilliseconds);
  }

  async post<T>(
    path: string,
    body: JsonValue,
    timeoutMilliseconds?: number
  ): Promise<T> {
    return this.request<T>("POST", path, body, timeoutMilliseconds);
  }

  async patch<T>(
    path: string,
    body: JsonValue,
    timeoutMilliseconds?: number
  ): Promise<T> {
    return this.request<T>("PATCH", path, body, timeoutMilliseconds);
  }

  async delete<T>(
    path: string,
    timeoutMilliseconds?: number
  ): Promise<T> {
    return this.request<T>("DELETE", path, undefined, timeoutMilliseconds);
  }

  async health(timeoutMilliseconds?: number): Promise<boolean> {
    try {
      const result = await this.healthDetails(timeoutMilliseconds);
      return Boolean(result.ok);
    } catch {
      return false;
    }
  }

  async healthDetails(timeoutMilliseconds?: number): Promise<HubHealth> {
    const health = await this.get<HubHealth>(
      "/api/health",
      timeoutMilliseconds
    );
    return {
      ...health,
      tmux_socket_name: normalizeTmuxSocketName(health.tmux_socket_name)
    };
  }

  private request<T>(
    method: string,
    path: string,
    body?: JsonValue,
    timeoutMilliseconds = 120_000
  ): Promise<T> {
    const url = new URL(path, this.baseUrl.endsWith("/") ? this.baseUrl : `${this.baseUrl}/`);
    const transport = url.protocol === "https:" ? https : http;
    const payload = body === undefined ? undefined : Buffer.from(JSON.stringify(body));

    return new Promise<T>((resolve, reject) => {
      const request = transport.request(
        url,
        {
          method,
          headers: payload
            ? {
                "Content-Type": "application/json",
                "Content-Length": payload.byteLength
              }
            : undefined,
          timeout: timeoutMilliseconds
        },
        (response) => {
          const chunks: Buffer[] = [];
          response.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
          response.on("end", () => {
            const text = Buffer.concat(chunks).toString("utf8");
            let parsed: any = {};
            try {
              parsed = text ? JSON.parse(text) : {};
            } catch {
              reject(new Error(`Agent Hub returned invalid JSON: ${text.slice(0, 300)}`));
              return;
            }
            const status = response.statusCode ?? 500;
            if (status < 200 || status >= 300) {
              reject(new Error(parsed.error || `Agent Hub HTTP ${status}`));
              return;
            }
            resolve(parsed as T);
          });
        }
      );
      request.on("timeout", () => request.destroy(new Error("Agent Hub request timed out")));
      request.on("error", reject);
      if (payload) {
        request.write(payload);
      }
      request.end();
    });
  }
}
