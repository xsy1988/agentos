import { create } from "zustand";

interface UIState {
  dark: boolean;
  sidebarCollapsed: boolean;
  contextPanelOpen: boolean;
  commandPaletteOpen: boolean;
  toggleDark: () => void;
  toggleSidebar: () => void;
  toggleContextPanel: () => void;
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
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
}));
