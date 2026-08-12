import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, setUnauthenticatedHandler, tokenStore } from "@/api/client";
import type { CurrentUser } from "@/types";

interface AuthState {
  user: CurrentUser | null;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

interface LoginResponse {
  accessToken: string;
  expiresIn: number;
  user: CurrentUser;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const queryClient = useQueryClient();

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      tokenStore.set(null);
      setUser(null);
      // Huỷ request đang bay TRƯỚC khi xoá cache — nếu không, response của
      // user cũ trả về SAU `clear()` vẫn có thể tự ghi dữ liệu vào lại
      // đúng cache key mà user mới đang đọc (query key không phân biệt
      // theo user — xem thêm lớp phòng thủ thứ 2 ở NotificationBell).
      await queryClient.cancelQueries();
      queryClient.clear();
    }
  }, [queryClient]);

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await api.post<LoginResponse>(
        "/auth/login",
        { email, password },
        {
          skipAuth: true,
        },
      );
      // Cùng lý do với `logout()`: huỷ request còn treo của phiên trước
      // (nếu có) rồi mới dọn cache, tránh response trễ ghi đè dữ liệu vào
      // đúng lúc user mới vừa mount xong.
      await queryClient.cancelQueries();
      queryClient.clear();
      tokenStore.set(res.accessToken);
      setUser(res.user);
    },
    [queryClient],
  );

  // Khôi phục phiên khi tải lại trang (F5).
  // Access token nằm trong bộ nhớ nên mất khi refresh — cookie refresh token
  // vẫn còn, nên gọi /auth/refresh để lấy access token mới.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.post<LoginResponse>("/auth/refresh", undefined, {
          skipAuth: true,
        });
        if (!cancelled) {
          tokenStore.set(res.accessToken);
          setUser(res.user);
        }
      } catch {
        // Chưa đăng nhập — bình thường, không phải lỗi
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setUnauthenticatedHandler(() => {
      tokenStore.set(null);
      setUser(null);
      void queryClient.cancelQueries().then(() => queryClient.clear());
    });
  }, [queryClient]);

  const value = useMemo(
    () => ({ user, isLoading, login, logout }),
    [user, isLoading, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth phải nằm trong AuthProvider");
  return ctx;
}
