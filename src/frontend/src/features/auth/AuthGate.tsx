// 身份门禁：先取 A02 身份，未登录显示首页（登录入口在右上角弹窗）；401 时统一回到首页。
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { authApi } from "../../api/auth";
import { isApiError, onUnauthorized } from "../../api/client";
import { preferencesApi } from "../../api/preferences";
import { Loading } from "../../components/Status";
import { useSession } from "../../stores/session";
import { HomePage } from "../home/HomePage";

export function AuthGate({ children }: { children: ReactNode }) {
  const { status, setIdentity, setAnonymous, generation, setPanelWidth } = useSession();
  const qc = useQueryClient();

  useEffect(() => {
    onUnauthorized(() => {
      if (useSession.getState().status !== "anonymous") {
        setAnonymous();
        qc.clear();
      }
    });
  }, [setAnonymous, qc]);

  const me = useQuery({
    queryKey: ["me", generation],
    queryFn: authApi.me,
    retry: false,
    staleTime: 5 * 60_000,
  });

  useEffect(() => {
    if (me.data) setIdentity(me.data);
    else if (me.error && isApiError(me.error) && me.error.status === 401 && useSession.getState().status !== "anonymous") setAnonymous();
  }, [me.data, me.error, setIdentity, setAnonymous]);

  // 偏好里的复核区宽度与动效设置在登录后立即生效
  const prefs = useQuery({
    queryKey: ["preferences", generation],
    queryFn: preferencesApi.get,
    enabled: status === "authenticated",
    staleTime: 5 * 60_000,
  });

  useEffect(() => {
    if (!prefs.data) return;
    setPanelWidth(prefs.data.panel_width);
    document.documentElement.dataset.motion = prefs.data.motion === "system" ? "" : prefs.data.motion;
  }, [prefs.data, setPanelWidth]);

  if (status === "loading" && me.isPending) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loading text="正在确认登录状态" />
      </div>
    );
  }
  if (status !== "authenticated") return <HomePage />;
  return <>{children}</>;
}
