/** 客户端幂等键（P1-9）。 */

/**
 * 为一次「提交意图」生成 run 级幂等键：同一次提交的重试复用同一个键，后端据此
 * 返回首次的 run 而不是再建一个；内容变化即视为新意图（重新取键）。
 */
export function newClientMessageId(): string {
  // 非安全上下文（局域网 http）下 crypto.randomUUID 可能缺失，退回时间戳+随机数
  const uuid = globalThis.crypto?.randomUUID;
  if (uuid) return uuid.call(globalThis.crypto);
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
