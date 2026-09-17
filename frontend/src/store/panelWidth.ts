/**
 * 右侧栏宽度口径（前端设计 §3.5 / P1-8）。
 *
 * 抽成纯函数模块的原因：夹取规则由「事件回调」变成「可配上限 + 视口」两个输入，
 * 脱离 window 才好单测；store 与拖拽手柄只做取值与转发。
 */

export const DEFAULT_PANEL_WIDTH = 360;
export const MIN_PANEL_WIDTH = 300;

/** 上限默认半屏；P1-8 起可在 [MIN, MAX] 内配置（宽屏显示器上同屏看插件面 + 任务详情）。 */
export const DEFAULT_PANEL_MAX_RATIO = 0.5;
export const WIDE_PANEL_MAX_RATIO = 0.7;
export const MIN_PANEL_MAX_RATIO = 0.3;
export const MAX_PANEL_MAX_RATIO = 0.8;

/** 并存时任务详情占右栏高度的比例区间（上下分栏，避免把插件面挤没）。 */
export const MIN_DETAIL_RATIO = 0.15;
export const MAX_DETAIL_RATIO = 0.8;
export const DEFAULT_DETAIL_RATIO = 0.45;

const STORAGE_KEY = "agentos.ui.panelMaxRatio";

/** 把上限比例夹到可用区间；非有限值（NaN/∞/缺失）回落默认。 */
export function normalizePanelMaxRatio(r: unknown): number {
  const n = typeof r === "number" ? r : Number(r);
  if (!Number.isFinite(n)) return DEFAULT_PANEL_MAX_RATIO;
  return Math.min(MAX_PANEL_MAX_RATIO, Math.max(MIN_PANEL_MAX_RATIO, n));
}

/** 夹取右栏宽度到 [MIN_PANEL_WIDTH, viewportWidth × maxRatio]。 */
export function clampPanelWidth(
  w: number,
  maxRatio: number = DEFAULT_PANEL_MAX_RATIO,
  viewportWidth: number = window.innerWidth,
): number {
  const max = Math.max(MIN_PANEL_WIDTH, Math.floor(viewportWidth * normalizePanelMaxRatio(maxRatio)));
  if (!Number.isFinite(w)) return Math.min(DEFAULT_PANEL_WIDTH, max);
  return Math.max(MIN_PANEL_WIDTH, Math.min(Math.round(w), max));
}

/** 夹取任务详情分栏高度比例。 */
export function clampDetailRatio(r: number): number {
  if (!Number.isFinite(r)) return DEFAULT_DETAIL_RATIO;
  return Math.min(MAX_DETAIL_RATIO, Math.max(MIN_DETAIL_RATIO, r));
}

/** 读持久化的宽度上限（无 localStorage / 值非法时回落默认，不抛）。 */
export function loadPanelMaxRatio(): number {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw === null ? DEFAULT_PANEL_MAX_RATIO : normalizePanelMaxRatio(Number(raw));
  } catch {
    return DEFAULT_PANEL_MAX_RATIO;
  }
}

/** 写持久化的宽度上限（隐私模式等写入失败时静默忽略）。 */
export function savePanelMaxRatio(ratio: number): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, String(normalizePanelMaxRatio(ratio)));
  } catch {
    /* 私有模式/禁用存储：仅本次会话生效 */
  }
}
