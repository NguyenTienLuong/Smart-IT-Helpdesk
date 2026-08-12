/**
 * Test cho `useNotificationSocket` (ADR-0009).
 *
 * Không cần backend thật — giả lập `WebSocket` toàn cục và kiểm tra hook làm
 * đúng 2 việc: (1) mở đúng URL kèm token, tự reconnect khi rớt; (2) áp đúng
 * 3 loại sự kiện vào cache react-query mà không gọi lại REST.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/api/client";
import type { AppNotification, Page } from "@/types";
import { useNotificationSocket } from "./useNotificationSocket";

/** Giả lập tối thiểu của WebSocket trình duyệt — đủ cho những gì hook dùng
 *  tới (`onopen`, `onmessage`, `onclose`, `onerror`, `close()`). Mỗi instance
 *  tạo ra được ghi vào `instances` để test kiểm tra/điều khiển từ bên ngoài. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close() {
    this.closed = true;
    this.onclose?.();
  }

  emitMessage(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

function Probe({ enabled = true }: { enabled?: boolean }) {
  useNotificationSocket(enabled);
  return null;
}

function renderWithClient(queryClient: QueryClient, enabled = true) {
  return render(
    <QueryClientProvider client={queryClient}>
      <Probe enabled={enabled} />
    </QueryClientProvider>,
  );
}

const mot: AppNotification = {
  id: "n-1",
  type: "TICKET_ASSIGNED",
  title: "Bạn được giao ticket HD-1",
  body: null,
  entityType: "ticket",
  entityId: "t-1",
  isRead: false,
  readAt: null,
  createdAt: "2026-08-11T00:00:00Z",
};

function trangDanhSach(items: AppNotification[]): Page<AppNotification> {
  return {
    data: items,
    pagination: {
      page: 1,
      pageSize: 20,
      totalItems: items.length,
      totalPages: 1,
    },
  };
}

describe("useNotificationSocket", () => {
  let queryClient: QueryClient;
  let originalWebSocket: typeof WebSocket;

  beforeEach(() => {
    FakeWebSocket.instances = [];
    originalWebSocket = globalThis.WebSocket;
    // @ts-expect-error -- gán giả lập cho môi trường test, kiểu không khớp 100% là cố ý
    globalThis.WebSocket = FakeWebSocket;
    tokenStore.set("token-gia-lap");
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
  });

  afterEach(() => {
    globalThis.WebSocket = originalWebSocket;
    tokenStore.set(null);
    vi.useRealTimers();
  });

  it("không mở kết nối khi chưa đăng nhập", () => {
    tokenStore.set(null);
    renderWithClient(queryClient);

    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("không mở kết nối khi enabled=false", () => {
    renderWithClient(queryClient, false);

    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("mở kết nối tới đúng URL kèm token", async () => {
    renderWithClient(queryClient);

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(FakeWebSocket.instances[0].url).toContain("token=token-gia-lap");
    expect(FakeWebSocket.instances[0].url).toContain("/notifications/ws");
  });

  it("notification:new — tăng số chưa đọc và thêm vào đầu danh sách", async () => {
    queryClient.setQueryData(["notifications", "unread-count"], { count: 2 });
    queryClient.setQueryData(["notifications", "list"], trangDanhSach([]));
    renderWithClient(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    act(() => {
      FakeWebSocket.instances[0].emitMessage({
        event: "notification:new",
        data: mot,
      });
    });

    expect(queryClient.getQueryData(["notifications", "unread-count"])).toEqual(
      { count: 3 },
    );
    expect(
      (
        queryClient.getQueryData([
          "notifications",
          "list",
        ]) as Page<AppNotification>
      ).data,
    ).toEqual([mot]);
  });

  it("notification:read — giảm số chưa đọc và đánh dấu đúng item", async () => {
    queryClient.setQueryData(["notifications", "unread-count"], { count: 1 });
    queryClient.setQueryData(["notifications", "list"], trangDanhSach([mot]));
    renderWithClient(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    act(() => {
      FakeWebSocket.instances[0].emitMessage({
        event: "notification:read",
        data: { id: "n-1" },
      });
    });

    expect(queryClient.getQueryData(["notifications", "unread-count"])).toEqual(
      { count: 0 },
    );
    const list = queryClient.getQueryData([
      "notifications",
      "list",
    ]) as Page<AppNotification>;
    expect(list.data[0].isRead).toBe(true);
  });

  it("notification:read_all — đưa số chưa đọc về 0 và đánh dấu mọi item", async () => {
    queryClient.setQueryData(["notifications", "unread-count"], { count: 5 });
    queryClient.setQueryData(
      ["notifications", "list"],
      trangDanhSach([mot, { ...mot, id: "n-2" }]),
    );
    renderWithClient(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    act(() => {
      FakeWebSocket.instances[0].emitMessage({
        event: "notification:read_all",
        data: {},
      });
    });

    expect(queryClient.getQueryData(["notifications", "unread-count"])).toEqual(
      { count: 0 },
    );
    const list = queryClient.getQueryData([
      "notifications",
      "list",
    ]) as Page<AppNotification>;
    expect(list.data.every((n) => n.isRead)).toBe(true);
  });

  it("số chưa đọc không xuống dưới 0 dù nhận sự kiện read thừa", async () => {
    queryClient.setQueryData(["notifications", "unread-count"], { count: 0 });
    renderWithClient(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    act(() => {
      FakeWebSocket.instances[0].emitMessage({
        event: "notification:read",
        data: { id: "x" },
      });
    });

    expect(queryClient.getQueryData(["notifications", "unread-count"])).toEqual(
      { count: 0 },
    );
  });

  it("gói tin hỏng (không phải JSON) bị bỏ qua, không crash", async () => {
    queryClient.setQueryData(["notifications", "unread-count"], { count: 1 });
    renderWithClient(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    act(() => {
      FakeWebSocket.instances[0].onmessage?.({
        data: "khong-phai-json",
      } as MessageEvent<string>);
    });

    expect(queryClient.getQueryData(["notifications", "unread-count"])).toEqual(
      { count: 1 },
    );
  });

  it("mất kết nối thì tự mở lại kết nối mới", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderWithClient(queryClient);
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    act(() => {
      FakeWebSocket.instances[0].close();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500); // backoff lần đầu ~1s
    });

    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);
  });

  it("unmount thì đóng kết nối và KHÔNG tự mở lại", async () => {
    const { unmount } = renderWithClient(queryClient);
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const soKetNoiTruoc = FakeWebSocket.instances.length;

    unmount();
    await new Promise((r) => setTimeout(r, 20));

    expect(FakeWebSocket.instances[0].closed).toBe(true);
    expect(FakeWebSocket.instances.length).toBe(soKetNoiTruoc); // không mở thêm cái nào
  });
});
