import { create } from "zustand";
import type { SidebarDescriptor } from "@/api/types";

// 右侧栏宽度：默认 360，最小 300，最大 ≤ 半屏（§3.5，运行时按 window.innerWidth 夹取）
const DEFAULT_PANEL_WIDTH = 360;
const MIN_PANEL_WIDTH = 300;

/** 夹取宽度到 [MIN, 半屏]（§3.5：侧边栏最大 ≤ 50vw）。 */
function clampPanelWidth(w: number): number {
  const max = Math.floor(window.innerWidth * 0.5);
  return Math.max(MIN_PANEL_WIDTH, Math.min(w, max));
}

interface UIState {
  sidebarCollapsed: boolean;
  contextPanelOpen: boolean;
  /** 右侧栏当前宽度（可拖拽，≤半屏） */
  contextPanelWidth: number;
  /** 活跃的 plugin 前端侧边栏描述符；null = 右栏展示 run 详情（默认） */
  sidebar: SidebarDescriptor | null;
  commandPaletteOpen: boolean;
  toggleSidebar: () => void;
  toggleContextPanel: () => void;
  // 从消息流「任务详情」等入口打开右栏（已开则不动，区别于 toggle）
  openContextPanel: () => void;
  setContextPanelWidth: (w: number) => void;
  // 打开 plugin 前端侧边栏（interactive_decision 卡「去处理」）：展开右栏 + 按 width_hint 设宽
  openSidebar: (d: SidebarDescriptor) => void;
  // 关闭侧边栏（回到 run 详情视图，不收起右栏）
  closeSidebar: () => void;
  setCommandPaletteOpen: (open: boolean) => void;
}

export const useUIStore = create<UIState>((set) => ({
  sidebarCollapsed: false,
  // 任务详情面板仅人工打开（点「任务详情/详情」），发消息/切任务等程序性事件一律不弹
  contextPanelOpen: false,
  contextPanelWidth: DEFAULT_PANEL_WIDTH,
  sidebar: null,
  commandPaletteOpen: false,

  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  toggleContextPanel: () =>
    set((s) => ({ contextPanelOpen: !s.contextPanelOpen })),
  openContextPanel: () => set({ contextPanelOpen: true }),
  setContextPanelWidth: (w) => set(() => ({ contextPanelWidth: clampPanelWidth(w) })),
  openSidebar: (d) =>
    set((s) => {
      const hint =
        d.width_hint && d.width_hint > 0
          ? Math.floor(window.innerWidth * Math.min(d.width_hint, 0.5))
          : s.contextPanelWidth;
      return {
        sidebar: d,
        contextPanelOpen: true,
        contextPanelWidth: clampPanelWidth(hint),
      };
    }),
  closeSidebar: () => set({ sidebar: null }),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
}));
