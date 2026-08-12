/**
 * Kênh đẩy realtime cho thông báo (ADR-0009 — ghi đè ADR-0008).
 *
 * Thay hoàn toàn `refetchInterval` 30s trước đây: mở một kết nối WebSocket
 * khi component mount, tự reconnect với backoff khi rớt mạng, và ghi thẳng
 * vào cache react-query — không gọi lại REST sau khi nhận sự kiện, vì dữ
 * liệu sự kiện đã đủ để cập nhật UI (đúng tinh thần "đẩy thay vì hỏi lại").
 *
 * Không export gì khác ngoài hook — nơi gọi (NotificationBell) không cần
 * biết có socket, có backoff, hay bất cứ chi tiết nào bên trong.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { notificationsApi } from "@/api/notifications";
import { tokenStore } from "@/api/client";
import type { AppNotification, NotificationSocketEvent, Page } from "@/types";

const MAX_BACKOFF_MS = 30_000;

function patchUnreadCount(
  queryClient: ReturnType<typeof useQueryClient>,
  update: (count: number) => number,
) {
  queryClient.setQueryData<{ count: number }>(
    ["notifications", "unread-count"],
    (old) => ({
      count: Math.max(0, update(old?.count ?? 0)),
    }),
  );
}

function patchList(
  queryClient: ReturnType<typeof useQueryClient>,
  update: (page: Page<AppNotification>) => Page<AppNotification>,
) {
  queryClient.setQueryData<Page<AppNotification>>(
    ["notifications", "list"],
    (old) => (old ? update(old) : old),
  );
}

function handleMessage(
  queryClient: ReturnType<typeof useQueryClient>,
  raw: string,
) {
  let message: NotificationSocketEvent;
  try {
    message = JSON.parse(raw);
  } catch {
    return; // gói tin hỏng — bỏ qua, không được làm crash UI
  }

  switch (message.event) {
    case "notification:new":
      patchUnreadCount(queryClient, (n) => n + 1);
      patchList(queryClient, (page) => ({
        ...page,
        data: [message.data, ...page.data],
        pagination: {
          ...page.pagination,
          totalItems: page.pagination.totalItems + 1,
        },
      }));
      break;

    case "notification:read":
      patchUnreadCount(queryClient, (n) => n - 1);
      patchList(queryClient, (page) => ({
        ...page,
        data: page.data.map((item) =>
          item.id === message.data.id ? { ...item, isRead: true } : item,
        ),
      }));
      break;

    case "notification:read_all":
      patchUnreadCount(queryClient, () => 0);
      patchList(queryClient, (page) => ({
        ...page,
        data: page.data.map((item) => ({ ...item, isRead: true })),
      }));
      break;
  }
}

export function useNotificationSocket(enabled: boolean): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) return;

    let closedByUs = false;
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const connect = () => {
      const token = tokenStore.get();
      if (!token) return; // chưa đăng nhập (hoặc vừa mất phiên) — không có gì để mở

      socket = new WebSocket(notificationsApi.wsUrl(token));

      socket.onopen = () => {
        attempt = 0;
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        handleMessage(queryClient, event.data);
      };

      socket.onclose = () => {
        if (closedByUs) return;
        // Backoff mũ, trần 30s — token có thể vừa hết hạn giữa các lần thử,
        // `connect()` tự đọc lại `tokenStore` mỗi lần nên tự phục hồi khi
        // access token mới được cấp (refresh) mà không cần logic riêng.
        const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt);
        attempt += 1;
        retryTimer = setTimeout(connect, delay);
      };

      socket.onerror = () => {
        socket?.close();
      };
    };

    connect();

    return () => {
      closedByUs = true;
      if (retryTimer) clearTimeout(retryTimer);
      socket?.close();
    };
  }, [enabled, queryClient]);
}
