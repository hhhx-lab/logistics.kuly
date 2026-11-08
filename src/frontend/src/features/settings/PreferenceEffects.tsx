import { getAnonymousLocale, setInterfaceLocale } from "../../i18n/runtime";
// 把当前身份的 P01 偏好应用到全局显示：时间展示时区与动效模式。
// 只影响显示，不参与任何查询边界换算；身份切换后随查询键失效自动重取。
import { useEffect } from "react";
import { setDisplayTimezone } from "../../components/Status";
import { useSession } from "../../stores/session";
import { usePreferences } from "./hooks";

export function usePreferenceEffects() {
  const identity = useSession((s) => s.identity);
  const pref = usePreferences();
  const tz = pref.data?.timezone;
  const motion = pref.data?.motion;
  const locale = pref.data?.interface_language;
  useEffect(()=>{setInterfaceLocale(identity && locale ? locale : getAnonymousLocale());},[identity,locale]);

  useEffect(() => {
    // 未登录或偏好尚未取到时回落到浏览器时区，不沿用前一身份的值
    setDisplayTimezone(identity ? tz : undefined);
  }, [identity, tz]);

  useEffect(() => {
    const root = document.documentElement;
    if (!identity || !motion || motion === "system") root.removeAttribute("data-motion");
    else root.setAttribute("data-motion", motion);
  }, [identity, motion]);
}

export function PreferenceEffects() {
  usePreferenceEffects();
  return null;
}
