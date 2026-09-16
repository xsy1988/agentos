/**
 * API 基础客户端：fetch 封装 + JWT 认证头 + 错误处理。
 * 不用 axios——SSE 需要 ReadableStream 原生 fetch，保持一致。
 */

const API_BASE = "/api/v1";

function getToken(): string | null {
  return localStorage.getItem("token");
}

export function setToken(token: string): void {
  localStorage.setItem("token", token);
}

export function clearToken(): void {
  localStorage.removeItem("token");
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

async function request<T>(
  method: string,
  path: string,
  options?: {
    body?: unknown;
    params?: Record<string, string | number | boolean | undefined>;
    signal?: AbortSignal;
  },
): Promise<T> {
  const url = new URL(`${API_BASE}${path}`, window.location.origin);
  if (options?.params) {
    for (const [k, v] of Object.entries(options.params)) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
  }

  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // FormData 不设 Content-Type，让浏览器自动带 multipart boundary
  const isFormData = options?.body instanceof FormData;
  if (options?.body !== undefined && !isFormData)
    headers["Content-Type"] = "application/json";

  const res = await fetch(url.toString(), {
    method,
    headers,
    body: isFormData
      ? (options!.body as FormData)
      : options?.body !== undefined
        ? JSON.stringify(options.body)
        : undefined,
    signal: options?.signal,
  });

  if (res.status === 401) {
    // 仅带 token 的请求 401 才需清 token；且已在 /login 时不再跳转，避免同页重定向死循环
    if (token) {
      clearToken();
      if (!window.location.pathname.startsWith("/login")) {
        window.location.href = "/login";
      }
    }
    throw new ApiError(401, "认证过期，请重新登录");
  }

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  const data = text ? JSON.parse(text) : null;

  if (!res.ok) {
    const detail = data?.detail ?? `请求失败 (${res.status})`;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }

  return data as T;
}

export const api = {
  get: <T>(path: string, params?: Record<string, string | number | boolean | undefined>) =>
    request<T>("GET", path, { params }),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, { body }),
  put: <T>(
    path: string,
    body?: unknown,
    params?: Record<string, string | number | boolean | undefined>,
  ) => request<T>("PUT", path, { body, params }),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, { body }),
  del: <T>(path: string) => request<T>("DELETE", path),
  /** 用于文件上传 */
  upload: <T>(path: string, formData: FormData) =>
    request<T>("POST", path, { body: formData }),
  /** 原始 fetch（SSE 用），自带认证头 */
  raw: (path: string, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return fetch(`${API_BASE}${path}`, { ...init, headers });
  },
};
