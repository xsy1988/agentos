/**
 * 右侧上下文面板（前端设计 §2.1）。
 * 点任何 run、任何一次工具调用，详情在右栏展开。
 * F1 骨架版：占位内容，F2/F3 实现实际内容。
 */
import { Empty, Typography } from "antd";
import { useSSEStore } from "@/store/sse";

export default function ContextPanel() {
  const { activeRunId, eventsByRun } = useSSEStore();
  const events = activeRunId ? eventsByRun[activeRunId] ?? [] : [];

  return (
    <div style={{ padding: "12px" }}>
      {activeRunId ? (
        <>
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            RUN · {activeRunId.slice(0, 8)}
          </Typography.Text>
          <div style={{ marginTop: 8 }}>
            <Typography.Text style={{ fontSize: 13 }}>
              {events.length} 条事件
            </Typography.Text>
          </div>
          {/* F2 实现：事件流详情、工具调用参数/结果、计划进度 */}
        </>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="选择一个任务查看详情"
          style={{ marginTop: 48 }}
        />
      )}
    </div>
  );
}
