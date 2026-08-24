import { create } from "zustand";

interface UIState {
  dark: boolean;
  sidebarCollapsed: boolean;
  contextPanelOpen: boolean;
  commandPaletteOpen: boolean;
  toggleDark: () => void;
  toggleSidebar: () => void;
  toggleContextPanel: () => void;
  // 从消息流「任务详情」等入口打开右栏（已开则不动，区别于 toggle）
  openContextPanel: () => void;
  setCommandPaletteOpen: (open: boolean) => void;
}

export const useUIStore = create<UIState>((set) => ({
  dark: true,
  sidebarCollapsed: false,
  contextPanelOpen: true,
  commandPaletteOpen: false,

  toggleDark: () => set((s) => ({ dark: !s.dark })),
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  toggleContextPanel: () =>
    set((s) => ({ contextPanelOpen: !s.contextPanelOpen })),
  openContextPanel: () => set({ contextPanelOpen: true }),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
}));
