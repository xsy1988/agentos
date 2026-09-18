/** 剪贴板复制与「复制引用」文本（Runs/Chat 共用，避免各处重复实现）。 */
import type { ArtifactOut, RunArtifactRef, RunOut } from "@/api/types";

/**
 * 复制文本：优先 navigator.clipboard（需安全上下文），失败退回 textarea +
 * execCommand——局域网 http 下 clipboard API 不可用，但仍要能复制。
 */
export async function copyText(text: string): Promise<boolean> {
  const clipboard = navigator.clipboard;
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(text);
      return true;
    } catch {
      // 权限被拒或非安全上下文：走下面的兜底
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** run 引用文本：一行可粘贴的定位串（id + 状态/触发/创建时间）。 */
export function runReference(run: RunOut): string {
  return `run:${run.id} · ${run.status} · 触发 ${run.trigger} · 创建 ${run.created_at}`;
}

/** 产物引用文本：产物定位（有 run_id 时一并带上）。 */
export function artifactReference(
  artifact: RunArtifactRef | ArtifactOut,
): string {
  const parts = [`artifact:${artifact.id}`, artifact.kind, artifact.name];
  if ("run_id" in artifact && artifact.run_id) parts.push(`run:${artifact.run_id}`);
  return parts.filter(Boolean).join(" · ");
}
