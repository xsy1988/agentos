/**
 * 左侧导航菜单：固定品牌区 + 分组菜单（工作台 / 知识与资源 / 能力 / 系统）。
 *
 * 品牌区不随选中页切换文案（此前"Agent/平台"来回跳很怪）；分组标签
 * 用小字弱化，菜单选中态统一走 antd inline 语义。
 */
import { useNavigate, useLocation } from "react-router-dom";
import { Menu } from "antd";
import {
  MessageOutlined,
  ScheduleOutlined,
  BookOutlined,
  ThunderboltOutlined,
  ClockCircleOutlined,
  SettingOutlined,
  ToolOutlined,
  CodeOutlined,
  ExperimentOutlined,
  BulbOutlined,
  ProjectOutlined,
} from "@ant-design/icons";
import type { MenuProps } from "antd";
import { useUIStore } from "@/store/ui";

type MenuItem = Required<MenuProps>["items"][number];

const GROUPS: { label: string; items: MenuItem[] }[] = [
  {
    label: "工作台",
    items: [
      { key: "/chat", icon: <MessageOutlined />, label: "对话" },
      { key: "/tasks", icon: <ProjectOutlined />, label: "任务管理" },
      { key: "/runs", icon: <ScheduleOutlined />, label: "执行记录" },
    ],
  },
  {
    label: "知识与资源",
    items: [
      { key: "/knowledge", icon: <BookOutlined />, label: "知识库" },
      { key: "/memory", icon: <ExperimentOutlined />, label: "记忆" },
    ],
  },
  {
    label: "能力",
    items: [
      { key: "/capabilities/mcp", icon: <ThunderboltOutlined />, label: "MCP 服务" },
      { key: "/capabilities/tools", icon: <ToolOutlined />, label: "工具" },
      { key: "/capabilities/plugins", icon: <CodeOutlined />, label: "插件" },
      { key: "/capabilities/skills", icon: <BulbOutlined />, label: "技能" },
    ],
  },
  {
    label: "系统",
    items: [
      { key: "/automation", icon: <ClockCircleOutlined />, label: "自动化" },
      { key: "/settings", icon: <SettingOutlined />, label: "设置" },
    ],
  },
];

const ALL_KEYS = GROUPS.flatMap((g) =>
  g.items.map((i) => (i && "key" in i ? String(i.key) : "")),
).filter(Boolean);

export default function AppSidebar() {
  const navigate = useNavigate();
  const location = useLocation();
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed);

  // 前缀匹配找选中项（/capabilities/* 各页平铺在分组里，不再用折叠子菜单）
  const selectedKey =
    ALL_KEYS.find((k) => location.pathname.startsWith(k)) ?? "/chat";

  return (
    <>
      {/* 固定品牌区：图标 + 名称 + 副标语，不随页面切换；折叠态只留 logo */}
      <div
        className="sidebar-brand"
        style={sidebarCollapsed ? { padding: 0, justifyContent: "center" } : undefined}
      >
        <div className="sidebar-brand-logo">A</div>
        {!sidebarCollapsed && (
          <div>
            <div className="sidebar-brand-name">Agent 平台</div>
            <div className="sidebar-brand-sub">AgentOS Workspace</div>
          </div>
        )}
      </div>
      {GROUPS.map((group) => (
        <div key={group.label}>
          {!sidebarCollapsed && <div className="sidebar-group-title">{group.label}</div>}
          <Menu
            mode="inline"
            className="sidebar-menu"
            selectedKeys={[selectedKey]}
            items={group.items}
            onClick={({ key }) => navigate(key)}
            style={{ borderRight: 0 }}
          />
        </div>
      ))}
    </>
  );
}
