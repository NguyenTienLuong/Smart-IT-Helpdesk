/**
 * Chuông thông báo (US-36).
 *
 * Realtime qua WebSocket (ADR-0009 — ghi đè ADR-0008 "polling 30 giây").
 * `unreadCount` REST chỉ còn gọi MỘT LẦN lúc mount, làm snapshot ban đầu và
 * phao dự phòng cho khoảng thời gian trước khi WS kịp kết nối — không còn
 * polling định kỳ, mọi cập nhật sau đó tới từ `useNotificationSocket`.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { notificationsApi } from "@/api/notifications";
import { useAuth } from "@/features/auth/AuthProvider";
import { useNotificationSocket } from "@/features/notifications/useNotificationSocket";
import { Button, LoadingBlock } from "@/components/ui";
import { cn } from "@/lib/utils";
import type { AppNotification, NotificationType } from "@/types";

/** Chấm màu theo mức khẩn — hai loại SLA phải nổi bật hơn phần còn lại. */
const DOT: Record<NotificationType, string> = {
  SLA_BREACHED: "bg-red-500",
  SLA_AT_RISK: "bg-amber-500",
  TICKET_ASSIGNED: "bg-blue-500",
  TICKET_RESOLVED: "bg-emerald-500",
  TICKET_STATUS_CHANGED: "bg-slate-400",
  TICKET_COMMENTED: "bg-slate-400",
  RATING_REQUESTED: "bg-violet-500",
};

function timeAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "vừa xong";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} phút trước`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} giờ trước`;
  return `${Math.floor(seconds / 86_400)} ngày trước`;
}

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();

  // ★ `user.id` nằm TRONG query key — không chỉ dựa vào `queryClient.clear()`
  // lúc đăng nhập/đăng xuất. Một request REST của user A đang bay giữa
  // chừng lúc B đăng nhập, nếu vì lý do gì đó (bug, race hiếm) vẫn ghi được
  // vào cache, thì cũng chỉ ghi vào Ô CỦA A — không có cách nào lẫn sang ô
  // B đang đọc, vì hai user không bao giờ chung một key.
  const countQuery = useQuery({
    queryKey: ["notifications", "unread-count", user?.id],
    queryFn: notificationsApi.unreadCount,
    // Không polling định kỳ (xem comment đầu file), nhưng BẮT BUỘC fetch lại
    // mỗi lần component này mount — component chỉ mount khi đã đăng nhập
    // (ProtectedRoute gate), nên "mount" ở đây đồng nghĩa "vừa vào phiên
    // mới".
    staleTime: Infinity,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    enabled: !!user,
  });

  const listQuery = useQuery({
    queryKey: ["notifications", "list", user?.id],
    queryFn: () => notificationsApi.list(1),
    enabled: open && !!user,
  });

  useNotificationSocket(true, user?.id);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const markRead = useMutation({
    mutationFn: notificationsApi.markRead,
    onSuccess: invalidate,
  });

  const markAllRead = useMutation({
    mutationFn: notificationsApi.markAllRead,
    onSuccess: invalidate,
  });

  // Bấm ra ngoài thì đóng bảng. Không có bước này, bảng che mất nội dung
  // trang và người dùng phải bấm đúng vào chuông mới đóng được.
  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (!panelRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const count = countQuery.data?.count ?? 0;

  const openNotification = (item: AppNotification) => {
    if (!item.isRead) markRead.mutate(item.id);
    setOpen(false);
    if (item.entityType === "ticket" && item.entityId) {
      navigate(`/tickets/${item.entityId}`);
    }
  };

  return (
    <div className="relative" ref={panelRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label={count > 0 ? `${count} thông báo chưa đọc` : "Thông báo"}
        className="relative rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-900"
      >
        <svg
          className="h-5 w-5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={1.8}
            d="M15 17h5l-1.4-1.4A2 2 0 0118 14.2V11a6 6 0 10-12 0v3.2c0 .5-.2 1-.6 1.4L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
          />
        </svg>
        {count > 0 && (
          <span
            className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center
                       rounded-full bg-red-600 px-1 text-[10px] font-semibold text-white"
          >
            {count > 99 ? "99+" : count}
          </span>
        )}
      </button>

      {open && (
        <div
          className="absolute right-0 z-20 mt-2 w-96 overflow-hidden rounded-lg border
                     border-slate-200 bg-white shadow-lg"
        >
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-2">
            <span className="text-sm font-medium text-slate-800">
              Thông báo
            </span>
            {count > 0 && (
              <Button
                variant="ghost"
                className="px-2 py-1 text-xs"
                loading={markAllRead.isPending}
                onClick={() => markAllRead.mutate()}
              >
                Đánh dấu tất cả đã đọc
              </Button>
            )}
          </div>

          <div className="max-h-96 overflow-auto">
            {listQuery.isLoading && <LoadingBlock />}

            {listQuery.data?.data.length === 0 && (
              <p className="px-4 py-10 text-center text-sm text-slate-500">
                Chưa có thông báo nào.
              </p>
            )}

            {listQuery.data?.data.map((item) => (
              <button
                key={item.id}
                onClick={() => openNotification(item)}
                className={cn(
                  "flex w-full gap-3 border-b border-slate-100 px-4 py-3 text-left last:border-0",
                  "hover:bg-slate-50",
                  !item.isRead && "bg-blue-50/50",
                )}
              >
                <span
                  className={cn(
                    "mt-1.5 h-2 w-2 shrink-0 rounded-full",
                    DOT[item.type],
                  )}
                />
                <span className="min-w-0 flex-1">
                  <span
                    className={cn(
                      "block truncate text-sm",
                      item.isRead
                        ? "text-slate-700"
                        : "font-medium text-slate-900",
                    )}
                  >
                    {item.title}
                  </span>
                  {item.body && (
                    <span className="mt-0.5 block truncate text-xs text-slate-500">
                      {item.body}
                    </span>
                  )}
                  <span className="mt-1 block text-xs text-slate-400">
                    {timeAgo(item.createdAt)}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
