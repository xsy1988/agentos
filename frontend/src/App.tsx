import { useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider, theme as antdTheme, App as AntdApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import { useAuthStore } from "@/store/auth";
import MainLayout from "@/layouts/MainLayout";
import LoginPage from "@/pages/Login";
import ChatPage from "@/pages/Chat";
import RunsPage from "@/pages/Runs";
import TaskManagePage from "@/pages/TaskManage";
import TaskDetailPage from "@/pages/TaskManage/TaskDetail";
import KnowledgePage from "@/pages/Knowledge";
import McpServicesPage from "@/pages/Capabilities/McpServices";
import ToolsPage from "@/pages/Capabilities/Tools";
import PluginsPage from "@/pages/Capabilities/Plugins";
import SkillsPage from "@/pages/Capabilities/Skills";
import WorkersPage from "@/pages/Capabilities/Workers";
import WorkerDetailPage from "@/pages/Capabilities/Workers/WorkerDetail";
import MemoryPage from "@/pages/Memory";
import AutomationPage from "@/pages/Automation";
import SettingsPage from "@/pages/Settings";
import CommandPalette from "@/components/CommandPalette";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 10_000,
    },
  },
});

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuthStore();
  if (loading) return null;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  const { fetchMe, user, loading } = useAuthStore();

  useEffect(() => {
    fetchMe();
  }, [fetchMe]);

  if (loading) return null;
  const token = localStorage.getItem("token");
  if (token && !user) {
    // fetchMe 进行中
    return null;
  }

  return (
    <QueryClientProvider client={queryClient}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          cssVar: true,
          // 统一浅色主题（Kimi/WorkBuddy 式）：不再提供深浅切换
          algorithm: antdTheme.defaultAlgorithm,
          token: {
            borderRadius: 8,
            fontSize: 14,
            colorPrimary: "#4f6ef7",
            colorBgLayout: "#f5f6fa",
          },
        }}
      >
        <AntdApp>
          <BrowserRouter>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route
                path="/"
                element={
                  <ProtectedRoute>
                    <MainLayout />
                  </ProtectedRoute>
                }
              >
                <Route index element={<ChatPage />} />
                <Route path="chat" element={<ChatPage />} />
                <Route path="runs" element={<RunsPage />} />
                {/* 任务管理：主任务实例列表 + 详情页（Worker 定义已拆到 /capabilities/workers） */}
                <Route path="tasks" element={<TaskManagePage />} />
                <Route path="tasks/:taskId" element={<TaskDetailPage />} />
                <Route path="task-types" element={<Navigate to="/capabilities/workers" replace />} />
                <Route path="knowledge" element={<KnowledgePage />} />
                <Route path="knowledge/:folderId" element={<KnowledgePage />} />
                <Route path="capabilities/mcp" element={<McpServicesPage />} />
                <Route path="capabilities/tools" element={<ToolsPage />} />
                <Route path="capabilities/plugins" element={<PluginsPage />} />
                <Route path="capabilities/workers" element={<WorkersPage />} />
                <Route path="capabilities/workers/:name" element={<WorkerDetailPage />} />
                <Route path="capabilities/skills" element={<SkillsPage />} />
                <Route path="memory" element={<MemoryPage />} />
                <Route path="automation" element={<AutomationPage />} />
                <Route path="settings" element={<SettingsPage />} />
              </Route>
            </Routes>
            <CommandPalette />
          </BrowserRouter>
        </AntdApp>
      </ConfigProvider>
    </QueryClientProvider>
  );
}
