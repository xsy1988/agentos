/**
 * 主布局：三栏——左侧导航 + 主内容区 + 右侧上下文面板。
 * 顶栏：⌘K 搜索 | 通知铃铛 | 三盏状态灯 | 深色切换 | 用户菜单
 */
import { Outlet } from "react-router-dom";
import { Layout } from "antd";
import { useUIStore } from "@/store/ui";
import AppSidebar from "@/components/AppSidebar";
import TopBar from "@/components/TopBar";
import ContextPanel from "@/components/ContextPanel";

const { Header, Sider, Content } = Layout;

export default function MainLayout() {
  const { sidebarCollapsed, contextPanelOpen } = useUIStore();

  return (
    <Layout style={{ height: "100vh", overflow: "hidden" }}>
      <Sider
        collapsible
        collapsed={sidebarCollapsed}
        trigger={null}
        width={200}
        collapsedWidth={48}
        style={{ overflow: "auto", height: "100vh", position: "fixed", left: 0, top: 0, bottom: 0, zIndex: 10 }}
      >
        <AppSidebar />
      </Sider>
      <Layout
        style={{
          marginLeft: sidebarCollapsed ? 48 : 200,
          transition: "margin-left 0.2s",
          height: "100%",
          overflow: "hidden",
        }}
      >
        <Header
          style={{
            padding: "0 16px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            zIndex: 9,
            height: 48,
            flex: "none",
          }}
        >
          <TopBar />
        </Header>
        <Content style={{ display: "flex", overflow: "hidden", flex: 1, minHeight: 0 }}>
          <div style={{ flex: 1, overflow: "auto", padding: "16px", minWidth: 0, height: "100%" }}>
            <Outlet />
          </div>
          {contextPanelOpen && (
            <div
              style={{
                width: 360,
                borderLeft: "1px solid rgba(128,128,128,0.2)",
                overflow: "auto",
                flex: "none",
              }}
            >
              <ContextPanel />
            </div>
          )}
        </Content>
      </Layout>
    </Layout>
  );
}
