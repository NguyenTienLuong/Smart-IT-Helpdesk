import { api, wsUrl } from "@/api/client";
import type { AppNotification, Page } from "@/types";

export const notificationsApi = {
  list: (page = 1, unreadOnly = false) =>
    api.get<Page<AppNotification>>(
      `/notifications?page=${page}&pageSize=20&unreadOnly=${unreadOnly}`,
    ),

  /** Snapshot lúc tải trang / phao dự phòng khi WS chưa kết nối được — xem
   *  ADR-0009. Không còn bị polling định kỳ. */
  unreadCount: () => api.get<{ count: number }>("/notifications/unread-count"),

  markRead: (id: string) =>
    api.post<{ updated: number }>(`/notifications/${id}/read`),

  markAllRead: () => api.post<{ updated: number }>("/notifications/read-all"),

  /** Kênh đẩy realtime (ADR-0009). Token trên query string — WebSocket
   *  handshake của trình duyệt không gắn được header tuỳ ý. */
  wsUrl: (token: string) => wsUrl("/notifications/ws", token),
};
