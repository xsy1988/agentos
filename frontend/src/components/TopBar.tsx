/**
 * 顶栏：⌘K 搜索 | 通知铃铛 | 三盏状态灯 | 任务详情面板开关 | 用户菜单。
 * 统一浅色主题，无深浅切换。
 */
import { Input, Tooltip, Dropdown, Avatar, Button } from "antd";
import {
  SearchOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  LogoutOutlined,
  UserOutlined,
  LayoutOutlined,
} from "@ant-design/icons";
import { useUIStore } from "@/store/ui";
import { useAuthStore } from "@/store/auth";
import StatusLights from "@/components/StatusLights";
import NotificationBell from "@/components/NotificationBell";

export default function TopBar() {
  const { toggleSidebar, sidebarCollapsed, setCommandPaletteOpen, contextPanelOpen, toggleContextPanel } = useUIStore();
  const { user, logout } = useAuthStore();

  const userMenu = {
    items: [
      {
        key: "profile",
        icon: <UserOutlined />,
        label: user?.display_name ?? user?.username ?? "用户",
        disabled: true,
      },
      { type: "divider" as const },
      {
        key: "settings",
        label: "设置",
        onClick: () => (window.location.href = "/settings"),
      },
      { type: "divider" as const },
      {
        key: "logout",
        icon: <LogoutOutlined />,
        label: "退出登录",
        onClick: logout,
      },
    ],
  };

  return (
    <>
      {/* 左侧：折叠按钮 + ⌘K 搜索 */}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Button
          type="text"
          icon={sidebarCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
          onClick={toggleSidebar}
          size="small"
        />
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索或跳转… (⌘K)"
          readOnly
          onClick={() => setCommandPaletteOpen(true)}
          style={{ width: 240, cursor: "pointer" }}
          size="small"
        />
      </div>

      {/* 右侧：状态灯 + 通知 + 主题 + 用户 */}
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <StatusLights />
        <NotificationBell />
        <Tooltip title={contextPanelOpen ? "收起任务详情面板" : "展开任务详情面板"}>
          <Button
            type="text"
            icon={<LayoutOutlined />}
            onClick={toggleContextPanel}
            size="small"
            style={contextPanelOpen ? { color: "var(--ant-color-primary)" } : undefined}
          />
        </Tooltip>
        <Dropdown menu={userMenu} placement="bottomRight">
          <Avatar size="small" icon={<UserOutlined />} style={{ cursor: "pointer" }} />
        </Dropdown>
      </div>
    </>
  );
}
