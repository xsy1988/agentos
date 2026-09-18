/**
 * 外部等待 API（P0-4）：等待清单 + 人工撤销。
 *
 * 真源：backend/app/modules/awaits/router.py
 * - GET  /awaits?run_id=&status=  等待清单（status 为 Query 别名，值域见 AWAIT_STATUSES）
 * - POST /awaits/{await_id}/cancel 撤销等待（引擎按结构化失败 await_cancelled 继续）
 * 状态机：waiting → granted | expired | cancelled（后三者终态）。
 */
import { api } from "./client";
import type { AwaitCancelOut, AwaitListOut } from "./types";

export interface AwaitListParams {
  run_id?: string;
  /** waiting / granted / expired / cancelled；省略 = 全部 */
  status?: string;
  limit?: number;
}

export const awaitsApi = {
  /** 等待清单（按创建时间倒序）。 */
  list: (params?: AwaitListParams) =>
    api.get<AwaitListOut>(
      "/awaits",
      params as Record<string, string | number | boolean | undefined>,
    ),
  /**
   * 撤销等待：后端 CAS waiting→cancelled 成功后唤醒 run（注入结构化失败，不静默）。
   * cancelled=false 表示该等待已被别的路径落定（幂等语义，不报错）。
   */
  cancel: (awaitId: string) => api.post<AwaitCancelOut>(`/awaits/${awaitId}/cancel`),
  /** 撤销某 run 当前全部 waiting 等待（清单 → 逐条 cancel；一次执行只会挂一条等待）。 */
  cancelWaitingForRun: async (runId: string): Promise<AwaitCancelOut[]> => {
    const { items } = await awaitsApi.list({ run_id: runId, status: "waiting" });
    return Promise.all(items.map((item) => awaitsApi.cancel(item.await_id)));
  },
};
