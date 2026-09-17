import { create } from "zustand";
import type { SidebarDescriptor } from "@/api/types";
import {
  DEFAULT_PANEL_WIDTH,
  DEFAULT_DETAIL_RATIO,
  WIDE_PANEL_MAX_RATIO,
  clampDetailRatio,
  clampPanelWidth,
  loadPanelMaxRatio,
  normalizePanelMaxRatio,
  savePanelMaxRatio,
} from "@/store/panelWidth";

interface UIState {
  sidebarCollapsed: boolean;
  contextPanelOpen: boolean;
  /** 右侧栏当前宽度（可拖拽，≤ 视口 × panelMaxRatio） */
  contextPanelWidth: number;
  /** 右栏宽度上限占屏比例（P1-8 起可配：半屏 ↔ 宽屏，持久化） */
  panelMaxRatio: number;
  /**
   * 活跃的 plugin 前端侧边栏描述符；null = 右栏只展示 run 详情。
   * **同时至多一个**（P1-8）：openSidebar 整体替换而非叠加，避免多份 plugin 状态并存。
   */
  sidebar: SidebarDescriptor | null;
  /** plugin 面与任务详情并存时，任务详情分栏是否折叠成一条标题栏（P1-8） */
  detailPaneCollapsed: boolean;
  /** 并存时任务详情占右栏高度的比例（0.15~0.8，可拖拽） */
  sidebarDetailRatio: number;
  commandPaletteOpen: boolean;
  toggleSidebar: () => void;
  toggleContextPanel: () => void;
  // 从消息流「任务详情」等入口打开右栏（已开则不动，区别于 toggle）
  openContextPanel: () => void;
  setContextPanelWidth: (w: number) => void;
  // 设置右栏宽度上限（半屏 ↔ 宽屏）：夹取后持久化，并把已开栏一并收窄
  setPanelMaxRatio: (ratio: number) => void;
  // 打开 plugin 前端侧边栏（interactive_decision 卡「去处理」）：展开右栏 + 按 width_hint 设宽
  openSidebar: (d: SidebarDescriptor) => void;
  // 关闭侧边栏（回到只用 run 详情的右栏，不收起右栏）
  closeSidebar: () => void;
  toggleDetailPane: () => void;
  setSidebarDetailRatio: (r: number) => void;
  setCommandPaletteOpen: (open: boolean) => void;
}

export const useUIStore = create<UIState>((set) => ({
  sidebarCollapsed: false,
  // 任务详情面板仅人工打开（点「任务详情/详情」），发消息/切任务等程序性事件一律不弹
  contextPanelOpen: false,
  contextPanelWidth: clampPanelWidth(DEFAULT_PANEL_WIDTH),
  panelMaxRatio: loadPanelMaxRatio(),
  sidebar: null,
  detailPaneCollapsed: false,
  sidebarDetailRatio: DEFAULT_DETAIL_RATIO,
  commandPaletteOpen: false,

  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  toggleContextPanel: () =>
    set((s) => ({ contextPanelOpen: !s.contextPanelOpen })),
  openContextPanel: () => set({ contextPanelOpen: true }),
  setContextPanelWidth: (w) =>
    set((s) => ({ contextPanelWidth: clampPanelWidth(w, s.panelMaxRatio) })),
  setPanelMaxRatio: (ratio) => {
    const next = normalizePanelMaxRatio(ratio);
    savePanelMaxRatio(next);
    // 上限调小后已开栏要立即收窄，否则改了上限看不出效果
    set((s) => ({
      panelMaxRatio: next,
      contextPanelWidth: clampPanelWidth(s.contextPanelWidth, next),
    }));
  },
  openSidebar: (d) =>
    set((s) => {
      // width_hint 是 plugin 的建议占比（契约 ≤0.5），再受用户配置的上限约束
      const hinted = d.width_hint && d.width_hint > 0 ? d.width_hint : 0;
      const target = hinted > 0 ? Math.floor(window.innerWidth * hinted) : s.contextPanelWidth;
      return {
        sidebar: d,
        contextPanelOpen: true,
        contextPanelWidth: clampPanelWidth(target, s.panelMaxRatio),
        // 新插件面进来时把任务详情也亮出来（并存即本项目的），但不重置用户拖过的比例
        detailPaneCollapsed: false,
      };
    }),
  closeSidebar: () => set({ sidebar: null }),
  toggleDetailPane: () => set((s) => ({ detailPaneCollapsed: !s.detailPaneCollapsed })),
  setSidebarDetailRatio: (r) => set({ sidebarDetailRatio: clampDetailRatio(r) }),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
}));

/** 宽度上限的可切换档位（半屏 ↔ 宽屏），供 TopBar 按钮使用。 */
export function panelMaxRatioToggleTarget(current: number): number {
  return current >= WIDE_PANEL_MAX_RATIO ? 0.5 : WIDE_PANEL_MAX_RATIO;
}
