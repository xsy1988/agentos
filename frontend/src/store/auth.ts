import { create } from "zustand";
import { api, setToken, clearToken } from "@/api/client";
import type { UserOut } from "@/api/types";

interface AuthState {
  user: UserOut | null;
  loading: boolean;
  initialized: boolean; // 是否已有账号
  fetchMe: () => Promise<void>;
  fetchInitStatus: () => Promise<boolean>;
  init: (username: string, password: string, display_name: string) => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  loading: true,
  initialized: true,

  fetchMe: async () => {
    // 无 token 时直接结束，不发请求（否则 401 触发整页重定向循环）
    if (!localStorage.getItem("token")) {
      set({ user: null, loading: false });
      return;
    }
    try {
      const user = await api.get<UserOut>("/auth/me");
      set({ user, loading: false });
    } catch {
      set({ user: null, loading: false });
    }
  },

  fetchInitStatus: async () => {
    // 尝试登录页打开时探测：如果 /auth/me 返回 401 说明需要登录
    // 但无法直接知道是否已初始化，所以先尝试 init（409 = 已有账号）
    try {
      await api.get<UserOut>("/auth/me");
      set({ initialized: true, loading: false });
      return true;
    } catch {
      set({ loading: false });
      return false;
    }
  },

  init: async (username, password, display_name) => {
    const user = await api.post<UserOut>("/auth/init", {
      username,
      password,
      display_name,
    });
    // init 不返回 token，需要再登录
    const token = await api.post<{ access_token: string }>("/auth/login", {
      username,
      password,
    });
    setToken(token.access_token);
    set({ user, initialized: true });
  },

  login: async (username, password) => {
    const token = await api.post<{ access_token: string }>("/auth/login", {
      username,
      password,
    });
    setToken(token.access_token);
    const user = await api.get<UserOut>("/auth/me");
    set({ user });
  },

  logout: () => {
    clearToken();
    set({ user: null });
    window.location.href = "/login";
  },
}));
