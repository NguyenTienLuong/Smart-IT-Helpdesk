/**
 * Wrapper fetch dùng chung cho toàn bộ ứng dụng.
 *
 * Xử lý ở MỘT CHỖ DUY NHẤT:
 *  - Gắn Authorization header
 *  - Timeout 10 giây
 *  - Parse lỗi theo định dạng chuẩn của backend
 *  - Tự refresh token khi gặp 401 (nhiều request 401 đồng thời chỉ refresh MỘT lần)
 *  - Chỉ retry GET, không bao giờ retry POST
 */

import type { ApiErrorBody } from "@/types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";
const TIMEOUT_MS = 10_000;

/**
 * Dựng URL WebSocket từ cùng `BASE_URL` dùng cho REST — để đổi backend
 * (`VITE_API_BASE_URL`) chỉ cần sửa MỘT chỗ, không phải nhớ sửa thêm ở đây.
 *
 * `BASE_URL` có thể là đường dẫn tương đối (`/api/v1`, mặc định — đi qua
 * proxy Vite lúc dev) hoặc một URL tuyệt đối (`https://api.example.com/v1`
 * lúc build production trỏ thẳng backend). Cả hai trường hợp đều phải ra
 * đúng scheme `ws:`/`wss:` tương ứng `http:`/`https:`.
 */
export function wsUrl(path: string, token: string): string {
  const httpUrl = new URL(`${BASE_URL}${path}`, window.location.href);
  httpUrl.protocol = httpUrl.protocol === "https:" ? "wss:" : "ws:";
  httpUrl.searchParams.set("token", token);
  return httpUrl.toString();
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
    public requestId?: string,
    public details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let accessToken: string | null = null;
let refreshPromise: Promise<boolean> | null = null;
let onUnauthenticated: (() => void) | null = null;

export const tokenStore = {
  /** Access token nằm TRONG BỘ NHỚ, không phải localStorage.
   *  localStorage bị đọc bởi bất kỳ lỗ hổng XSS nào. */
  set: (t: string | null) => {
    accessToken = t;
  },
  get: () => accessToken,
};

export function setUnauthenticatedHandler(fn: () => void) {
  onUnauthenticated = fn;
}

async function parseError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as ApiErrorBody;
    return new ApiError(
      body.error?.code ?? "UNKNOWN",
      body.error?.message ?? "Đã có lỗi xảy ra",
      res.status,
      body.error?.requestId,
      body.error?.details,
    );
  } catch {
    return new ApiError("UNKNOWN", `Lỗi ${res.status}`, res.status);
  }
}

/** Gọi /auth/refresh. Nhiều request cùng lúc chỉ gây MỘT lần refresh. */
async function refreshToken(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;

  refreshPromise = (async () => {
    try {
      const res = await fetch(`${BASE_URL}/auth/refresh`, {
        method: "POST",
        credentials: "include", // gửi cookie HttpOnly chứa refresh token
      });
      if (!res.ok) return false;
      const data = await res.json();
      tokenStore.set(data.accessToken);
      return true;
    } catch {
      return false;
    } finally {
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  skipAuth?: boolean;
  _isRetry?: boolean;
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { body, skipAuth, _isRetry, headers, ...rest } = options;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  const finalHeaders: Record<string, string> = {
    "Content-Type": "application/json",
    ...(headers as Record<string, string>),
  };
  if (!skipAuth && accessToken) {
    finalHeaders.Authorization = `Bearer ${accessToken}`;
  }

  try {
    const res = await fetch(`${BASE_URL}${path}`, {
      ...rest,
      headers: finalHeaders,
      credentials: "include",
      signal: controller.signal,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    if (res.status === 401 && !skipAuth && !_isRetry) {
      const ok = await refreshToken();
      if (ok) return request<T>(path, { ...options, _isRetry: true });
      onUnauthenticated?.();
      throw new ApiError("UNAUTHENTICATED", "Phiên đăng nhập đã hết hạn", 401);
    }

    if (!res.ok) throw await parseError(res);
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(
        "TIMEOUT",
        "Yêu cầu quá thời gian chờ. Vui lòng thử lại.",
        408,
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Fetch cho luồng SSE — trả về Response THÔ để bên gọi tự đọc body theo dòng.
 *
 * Không dùng chung `request()` được vì hàm đó `await res.json()`, tức là chờ
 * toàn bộ phản hồi kết thúc — đúng thứ mà streaming sinh ra để tránh.
 *
 * Cũng KHÔNG dùng EventSource được: EventSource chỉ gửi GET và không đặt được
 * header, nên không mang theo được Bearer token.
 *
 * KHÔNG đặt timeout: một câu trả lời dài có thể stream lâu hơn 10 giây mà vẫn
 * hoàn toàn bình thường. Việc huỷ do `signal` bên gọi quyết định.
 */
export async function fetchStream(
  path: string,
  body: unknown,
  signal?: AbortSignal,
  _isRetry = false,
): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers,
    credentials: "include",
    signal,
    body: JSON.stringify(body),
  });

  if (res.status === 401 && !_isRetry) {
    const ok = await refreshToken();
    if (ok) return fetchStream(path, body, signal, true);
    onUnauthenticated?.();
    throw new ApiError("UNAUTHENTICATED", "Phiên đăng nhập đã hết hạn", 401);
  }
  if (!res.ok) throw await parseError(res);
  return res;
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "GET" }),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "POST", body }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "PATCH", body }),
  delete: <T>(path: string, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: "DELETE" }),
};
