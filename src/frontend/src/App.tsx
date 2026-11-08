// 路由与外壳。公开文档页无需登录；其余地址经身份门禁，未登录时显示首页。
// 对话工作台自带左侧会话导航，其余页面共用应用导航。
import { Navigate, Route, Routes } from "react-router-dom";
import { ToastProvider } from "./components/Overlay";
import { AuthGate } from "./features/auth/AuthGate";
import { ConversationPage } from "./features/conversation/ConversationPage";
import { ExtensionsPage } from "./features/extensions/ExtensionsPage";
import { DocPage } from "./features/home/DocPage";
import { CaseDetailPage } from "./features/management/CaseDetailPage";
import { ManagementPage } from "./features/management/ManagementPage";
import { ReviewPage } from "./features/review/ReviewPage";
import { PreferenceEffects } from "./features/settings/PreferenceEffects";
import { SettingsPage } from "./features/settings/SettingsPage";
import { NotFoundPage } from "./features/shell/NotFoundPage";

export default function App() {
  return (
    <ToastProvider>
      <Routes>
        <Route path="/docs/:slug" element={<DocPage />} />
        <Route path="/docs" element={<Navigate to="/docs/guide" replace />} />
        <Route
          path="*"
          element={
            <AuthGate>
              <PreferenceEffects />
              <ProtectedRoutes />
            </AuthGate>
          }
        />
      </Routes>
    </ToastProvider>
  );
}

function ProtectedRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/conversation" replace />} />
      <Route path="/conversation" element={<ConversationPage />} />
      <Route path="/conversation/:conversationId" element={<ConversationPage />} />
      <Route path="/documents" element={<ManagementPage />} />
      <Route path="/documents/cases/:caseId" element={<CaseDetailPage />} />
      <Route path="/extensions" element={<ExtensionsPage />} />
      <Route path="/review" element={<ReviewPage />} />
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="/settings/memory" element={<SettingsPage />} />
      <Route path="/settings/conversations" element={<SettingsPage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
