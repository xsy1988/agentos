/**
 * 外部等待面板（P0-4）：把平台代为持有的等待（await）摆到面上——
 * 等什么工具、最晚等到什么时候、以及「撤销等待」。
 *
 * 数据源 GET /awaits?status=waiting（真源 backend/app/modules/awaits/router.py），
 * 撤销走 POST /awaits/{await_id}/cancel：后端 CAS waiting→cancelled 并唤醒 run，
 * 让 run 以结构化失败 await_cancelled 收尾（不静默挂死）。cancelled=false 表示
 * 该等待已被超时/回传等别的路径落定，属幂等提示而非错误。
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Space, Tag, Tooltip, Typography, message as antdMessage } from "antd";
import { ClockCircleOutlined, StopOutlined } from "@ant-design/icons";
import { awaitsApi } from "@/api/awaits";
import type { AwaitOut, RunEventOut } from "@/api/types";

const { Text } = Typography;

/** 距离最晚时间还有多久（waiting 行后端未落定 waited_ms，只能按 deadline 倒数）。 */
function fmtRemaining(deadlineAt: string | null): string {
  if (!deadlineAt) return "未设最晚时间";
  const ms = new Date(deadlineAt).getTime() - Date.now();
  if (Number.isNaN(ms)) return "最晚时间未知";
  if (ms <= 0) return "已过最晚时间，等待超时收尾";
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `剩余 ${sec} 秒`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `剩余 ${min} 分 ${sec % 60} 秒`;
  return `剩余 ${Math.floor(min / 60)} 小时 ${min % 60} 分`;
}

function fmtClock(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString("zh-CN");
}

/** run 事件里是否还有未落定的等待（最后一个 await_* 事件是 await_started）。 */
export function hasUnresolvedAwait(events: Pick<RunEventOut, "event_type">[]): boolean {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const type = events[i].event_type;
    if (type === "await_started") return true;
    if (type === "await_resolved" || type === "await_expired") return false;
  }
  return false;
}

export default function AwaitPanel({
  runId,
  title = "等待外部回调",
}: {
  /** 省略 = 平台全部等待 */
  runId?: string;
  title?: string;
}) {
  const queryClient = useQueryClient();
  // 倒数文案需要秒级重渲染，数据本身 5s 轮询即可
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((v) => v + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const { data, isLoading } = useQuery({
    queryKey: ["awaits", "waiting", runId ?? "all"],
    queryFn: () => awaitsApi.list({ run_id: runId, status: "waiting" }),
    refetchInterval: 5000,
  });
  const items = data?.items ?? [];

  const cancelMutation = useMutation({
    mutationFn: (awaitId: string) => awaitsApi.cancel(awaitId),
    onSuccess: (out) => {
      if (out.cancelled) antdMessage.success("已撤销等待，run 将以「等待已被撤销」失败收尾");
      else antdMessage.info(`该等待已落定（${out.status}），无需撤销`);
      queryClient.invalidateQueries({ queryKey: ["awaits"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.invalidateQueries({ queryKey: ["run", out.run_id] });
      queryClient.invalidateQueries({ queryKey: ["run-events", out.run_id] });
    },
    onError: (err: Error) => antdMessage.error(`撤销等待失败：${err.message}`),
  });

  if (isLoading || items.length === 0) return null;

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: "#b7c0ff", background: "#f7f8ff" }}
      title={
        <Space size={6}>
          <ClockCircleOutlined style={{ color: "#4f6ef7" }} />
          <Text strong>{title}</Text>
          <Tag color="processing">{items.length} 条</Tag>
        </Space>
      }
    >
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        {items.map((item: AwaitOut) => (
          <Space key={item.await_id} size={8} wrap>
            <Tag color="processing">等待中</Tag>
            <Text strong>{item.tool}</Text>
            <Text type="secondary">
              最晚 {fmtClock(item.deadline_at)}（{fmtRemaining(item.deadline_at)}）
            </Text>
            {item.waited_ms > 0 && (
              <Text type="secondary">已等待 {Math.round(item.waited_ms / 1000)} 秒</Text>
            )}
            {item.attempts > 0 && <Text type="secondary">已通知 {item.attempts} 次</Text>}
            {!runId && (
              <Text type="secondary" copyable={{ text: item.run_id }}>
                run {item.run_id.slice(0, 8)}
              </Text>
            )}
            <Tooltip title="撤销后 run 立即以「等待已被撤销」失败收尾，不会静默挂死">
              <Button
                size="small"
                danger
                icon={<StopOutlined />}
                loading={
                  cancelMutation.isPending && cancelMutation.variables === item.await_id
                }
                onClick={() => cancelMutation.mutate(item.await_id)}
              >
                撤销等待
              </Button>
            </Tooltip>
          </Space>
        ))}
      </Space>
    </Card>
  );
}
