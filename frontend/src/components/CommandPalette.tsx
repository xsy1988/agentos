/**
 * ⌘K 命令面板（前端设计 §4）。
 * 全局搜索 + 快捷动作。
 */
import { useEffect } from "react";
import { Modal, Input, List, Typography } from "antd";
import { useNavigate } from "react-router-dom";
import {
  MessageOutlined,
  ScheduleOutlined,
  BookOutlined,
  SettingOutlined,
  ClockCircleOutlined,
  ExperimentOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  CodeOutlined,
  BulbOutlined,
} from "@ant-design/icons";
import { useUIStore } from "@/store/ui";

interface Command {
  label: string;
  icon: React.ReactNode;
  action: () => void;
  group: string;
}

export default function CommandPalette() {
  const { commandPaletteOpen, setCommandPaletteOpen } = useUIStore();
  const navigate = useNavigate();

  // ⌘K 全局快捷键
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCommandPaletteOpen(!commandPaletteOpen);
      }
      if (e.key === "Escape" && commandPaletteOpen) {
        setCommandPaletteOpen(false);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [commandPaletteOpen, setCommandPaletteOpen]);

  const commands: Command[] = [
    { label: "新会话", icon: <MessageOutlined />, group: "对话", action: () => navigate("/chat") },
    { label: "任务列表", icon: <ScheduleOutlined />, group: "任务", action: () => navigate("/runs") },
    { label: "知识库", icon: <BookOutlined />, group: "知识库", action: () => navigate("/knowledge") },
    { label: "MCP 服务", icon: <ThunderboltOutlined />, group: "能力", action: () => navigate("/capabilities/mcp") },
    { label: "工具", icon: <ToolOutlined />, group: "能力", action: () => navigate("/capabilities/tools") },
    { label: "插件", icon: <CodeOutlined />, group: "能力", action: () => navigate("/capabilities/plugins") },
    { label: "技能", icon: <BulbOutlined />, group: "能力", action: () => navigate("/capabilities/skills") },
    { label: "记忆", icon: <ExperimentOutlined />, group: "记忆", action: () => navigate("/memory") },
    { label: "自动化", icon: <ClockCircleOutlined />, group: "自动化", action: () => navigate("/automation") },
    { label: "设置", icon: <SettingOutlined />, group: "设置", action: () => navigate("/settings") },
  ];

  // 按 group 分组
  const groups = commands.reduce<Record<string, Command[]>>((acc, c) => {
    (acc[c.group] ??= []).push(c);
    return acc;
  }, {});

  return (
    <Modal
      open={commandPaletteOpen}
      onCancel={() => setCommandPaletteOpen(false)}
      footer={null}
      width={480}
      styles={{ body: { padding: 0 } }}
      title={
        <Input
          placeholder="输入命令或搜索…"
          bordered={false}
          autoFocus
          onChange={() => {
            // F1 简化版：不实现搜索过滤，直接列快捷动作
          }}
        />
      }
    >
      <List
        size="small"
        dataSource={Object.entries(groups)}
        renderItem={([group, cmds]) => (
          <>
            <List.Item style={{ padding: "4px 12px", background: "rgba(128,128,128,0.06)" }}>
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                {group}
              </Typography.Text>
            </List.Item>
            {cmds.map((c) => (
              <List.Item
                key={c.label}
                style={{ cursor: "pointer", padding: "8px 16px" }}
                onClick={() => {
                  c.action();
                  setCommandPaletteOpen(false);
                }}
              >
                <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  {c.icon}
                  {c.label}
                </span>
              </List.Item>
            ))}
          </>
        )}
      />
    </Modal>
  );
}
