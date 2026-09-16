/**
 * 主布局：三栏——左侧导航 + 主内容区 + 右侧上下文面板。
 * 顶栏：⌘K 搜索 | 通知铃铛 | 三盏状态灯 | 深色切换 | 用户菜单
 */
import { useEffect, useRef } from "react";
import { Outlet } from "react-router-dom";
import { Layout } from "antd";
import { useUIStore } from "@/store/ui";
import AppSidebar from "@/components/AppSidebar";
import TopBar from "@/components/TopBar";
import ContextPanel from "@/components/ContextPanel";

const { Header, Sider, Content } = Layout;

export default function MainLayout() {
  const {
    sidebarCollapsed,
    contextPanelOpen,
    contextPanelWidth,
    setContextPanelWidth,
  } = useUIStore();
  const dragging = useRef(false);

  // 拖拽改宽：按住右栏左缘手柄拖动，宽度实时夹取到 ≤半屏（§3.5）
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      setContextPanelWidth(window.innerWidth - e.clientX);
    };
    const onUp = () => {
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [setContextPanelWidth]);

  return (
    <Layout style={{ height: "100vh", overflow: "hidden" }}>
      <Sider
        collapsible
        collapsed={sidebarCollapsed}
        trigger={null}
        width={200}
        collapsedWidth={48}
        theme="light"
        style={{
          overflow: "auto",
          height: "100vh",
          position: "fixed",
          left: 0,
          top: 0,
          bottom: 0,
          zIndex: 10,
          // antd Sider 默认深色 #001529：不设浅色底时品牌区/分组标题带/菜单未填满的
          // 下缘全部透出深色，与白色菜单块黑白交加（浏览器实测确认）。
          background: "var(--ant-color-bg-container)",
          borderRight: "1px solid var(--ant-color-border-secondary)",
        }}
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
            background: "var(--ant-color-bg-container)",
            borderBottom: "1px solid var(--ant-color-border-secondary)",
          }}
        >
          <TopBar />
        </Header>
        <Content style={{ display: "flex", overflow: "hidden", flex: 1, minHeight: 0 }}>
          <div style={{ flex: 1, overflow: "auto", padding: 16, minWidth: 0, height: "100%" }}>
            <Outlet />
          </div>
          {contextPanelOpen && (
            <div
              style={{
                width: contextPanelWidth,
                borderLeft: "1px solid var(--ant-color-border-secondary)",
                flex: "none",
                position: "relative",
                display: "flex",
                minHeight: 0,
              }}
            >
              {/* 左缘拖拽手柄：可变宽，最大 ≤半屏 */}
              <div
                onMouseDown={() => {
                  dragging.current = true;
                  document.body.style.cursor = "col-resize";
                  document.body.style.userSelect = "none";
                }}
                style={{
                  position: "absolute",
                  left: -3,
                  top: 0,
                  bottom: 0,
                  width: 6,
                  cursor: "col-resize",
                  zIndex: 5,
                }}
              />
              <div style={{ flex: 1, overflow: "auto", minWidth: 0 }}>
                <ContextPanel />
              </div>
            </div>
          )}
        </Content>
      </Layout>
    </Layout>
  );
}
