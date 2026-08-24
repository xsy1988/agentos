/**
 * 左侧导航菜单（前端设计 §2.2 菜单结构）。
 */
import { useNavigate, useLocation } from "react-router-dom";
import { Menu } from "antd";
import {
  MessageOutlined,
  ScheduleOutlined,
  BookOutlined,
  AppstoreOutlined,
  ThunderboltOutlined,
  ClockCircleOutlined,
  SettingOutlined,
  ToolOutlined,
  CodeOutlined,
  ExperimentOutlined,
  BulbOutlined,
} from "@ant-design/icons";
import type { MenuProps } from "antd";

type MenuItem = Required<MenuProps>["items"][number];

const items: MenuItem[] = [
  { key: "/chat", icon: <MessageOutlined />, label: "对话" },
  { key: "/runs", icon: <ScheduleOutlined />, label: "任务" },
  { key: "/knowledge", icon: <BookOutlined />, label: "知识库" },
  {
    key: "capabilities",
    icon: <AppstoreOutlined />,
    label: "能力",
    children: [
      { key: "/capabilities/mcp", icon: <ThunderboltOutlined />, label: "MCP 服务" },
      { key: "/capabilities/tools", icon: <ToolOutlined />, label: "工具" },
      { key: "/capabilities/plugins", icon: <CodeOutlined />, label: "插件" },
      { key: "/capabilities/skills", icon: <BulbOutlined />, label: "技能" },
    ],
  },
  { key: "/memory", icon: <ExperimentOutlined />, label: "记忆" },
  { key: "/automation", icon: <ClockCircleOutlined />, label: "自动化" },
  { key: "/settings", icon: <SettingOutlined />, label: "设置" },
];

export default function AppSidebar() {
  const navigate = useNavigate();
  const location = useLocation();

  const found = items.find(
    (i) =>
      i &&
      "key" in i &&
      typeof i.key === "string" &&
      i.key !== "capabilities" &&
      location.pathname.startsWith(i.key),
  );
  const selectedKey: string = found && "key" in found ? String(found.key) : "/chat";

  const openKeys = location.pathname.startsWith("/capabilities")
    ? ["capabilities"]
    : [];

  return (
    <>
      <div
        style={{
          height: 48,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--ant-color-primary)",
          fontWeight: 600,
          fontSize: 16,
        }}
      >
        {selectedKey === "/chat" ? "Agent" : "平台"}
      </div>
      <Menu
        mode="inline"
        selectedKeys={[selectedKey]}
        defaultOpenKeys={openKeys}
        items={items}
        onClick={({ key }) => navigate(key)}
        style={{ borderRight: 0 }}
      />
    </>
  );
}
