// 首页与公开文档页共用的登录弹窗：半透明磨砂面板叠在页面之上。
// 凭证只提交给服务端，不写入浏览器存储；注册方式由服务端配置决定。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate } from "react-router-dom";
import { authApi } from "../../api/auth";
import { errorText } from "../../api/client";
import { Icon } from "../../components/Icon";
import { useEscLayer, useFocusTrap, useReturnFocus } from "../../components/Overlay";
import { ErrorBar } from "../../components/Status";
import { useSession } from "../../stores/session";

export function LoginDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null);
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [resendAt, setResendAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const config = useQuery({ queryKey: ["registration-config"], queryFn: authApi.registrationConfig, enabled: open });
  const isRegister = mode === "register";
  const codeRequired = config.data?.email_code_required ?? true;
  const qc = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const resetForIdentityChange = useSession((s) => s.resetForIdentityChange);
  const setIdentity = useSession((s) => s.setIdentity);

  useEscLayer(open, onClose);
  useReturnFocus(open);
  useFocusTrap(open, panel);

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  useEffect(() => {
    if (!open || resendAt <= Date.now()) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [open, resendAt]);

  const sendCode = useMutation({
    mutationFn: () => authApi.sendRegistrationCode(email.trim()),
    onSuccess: (result) => { setNow(Date.now()); setResendAt(Date.now() + (result.resend_seconds ?? 60) * 1000); },
  });

  const login = useMutation({
    mutationFn: () => isRegister
      ? authApi.register(account.trim(), email.trim(), password, codeRequired ? code : undefined)
      : authApi.login(account.trim(), password),
    onSuccess: async (identity) => {
      resetForIdentityChange();
      qc.clear();
      setIdentity(identity);
      setPassword("");
      setConfirmPassword("");
      setCode("");
      onClose();
      // 从首页或公开文档页登录后进入对话工作台；其他受保护地址登录后留在原页
      if (location.pathname === "/" || location.pathname.startsWith("/docs")) navigate("/conversation", { replace: true });
      await qc.invalidateQueries();
    },
  });

  if (!open) return null;
  const remaining = Math.max(0, Math.ceil((resendAt - now) / 1000));
  const canSubmit = account.trim().length > 0 && password.length > 0 && !login.isPending && (!isRegister || (
    config.data?.enabled && email.trim().length > 0 && password === confirmPassword && (!codeRequired || /^[0-9]{6}$/.test(code))
  ));
  const switchMode = (next: "login" | "register") => {
    setMode(next); setPassword(""); setConfirmPassword(""); setCode(""); login.reset(); sendCode.reset();
  };

  return createPortal(
    <div className="fixed inset-0 z-modal flex items-center justify-center p-4 overflow-y-auto">
      <div className="glass-scrim absolute inset-0 anim-fade" onClick={onClose} aria-hidden="true" />
      <div ref={panel} role="dialog" aria-modal="true" aria-labelledby="login-title" className="glass-panel relative w-full max-w-[400px] rounded-2xl p-6 text-white anim-rise">
        <button type="button" className="glass-icon-btn absolute right-3 top-3" onClick={onClose} aria-label="关闭">
          <Icon name="close" size={16} />
        </button>

        <div className="flex items-center gap-2.5">
          <span className="brand-tile flex h-10 w-10 items-center justify-center rounded-[10px]">
            <Icon name="logo" size={24} />
          </span>
          <div>
            <h2 id="login-title" className="text-[16px] font-semibold">{isRegister ? "注册物流单证智能体" : "登录物流单证智能体"}</h2>
            <p className="text-[13px] text-white/75">识别、翻译与人工复核后交付</p>
          </div>
        </div>

        <form
          className="mt-5"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSubmit) login.mutate();
          }}
        >
          <label className="block">
            <span className="text-[13px] text-white/85">{isRegister ? "用户名" : "用户名或邮箱"}</span>
            <input className="glass-input mt-1.5" value={account} onChange={(e) => setAccount(e.target.value)} autoComplete="username" autoFocus name="account" required minLength={isRegister ? 3 : 1} maxLength={isRegister ? 32 : 254} pattern={isRegister ? "[A-Za-z0-9_][A-Za-z0-9_-]{2,31}" : undefined} placeholder={isRegister ? "3–32 位字母、数字、下划线或短横线" : "请输入用户名或邮箱"} />
          </label>
          {isRegister && <label className="mt-3 block">
            <span className="text-[13px] text-white/85">邮箱</span>
            <input className="glass-input mt-1.5" type="email" value={email} onChange={(e) => { setEmail(e.target.value); setCode(""); sendCode.reset(); }} autoComplete="email" name="email" required maxLength={254} placeholder="请输入邮箱" />
          </label>}
          <label className="mt-3 block">
            <span className="text-[13px] text-white/85">密码</span>
            <input className="glass-input mt-1.5" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete={isRegister ? "new-password" : "current-password"} name="password" required minLength={isRegister ? 8 : 1} maxLength={isRegister ? 128 : 256} placeholder={isRegister ? "至少 8 位，包含字母和数字" : "请输入密码"} />
          </label>

          {isRegister && <>
            <label className="mt-3 block">
              <span className="text-[13px] text-white/85">确认密码</span>
              <input className="glass-input mt-1.5" type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} autoComplete="new-password" name="confirm-password" required placeholder="请再次输入密码" />
            </label>
            {confirmPassword && confirmPassword !== password && <p className="mt-2 text-[13px] text-red-200">两次密码不一致</p>}
            {codeRequired && <label className="mt-3 block">
              <span className="text-[13px] text-white/85">邮箱验证码</span>
              <div className="mt-1.5 flex gap-2">
                <input className="glass-input min-w-0 flex-1" value={code} onChange={(e) => setCode(e.target.value.replace(/[^0-9]/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" name="verification-code" required pattern="[0-9]{6}" maxLength={6} placeholder="六位验证码" />
                <button type="button" className="btn btn-primary shrink-0" disabled={sendCode.isPending || remaining > 0 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())} onClick={() => sendCode.mutate()}>{sendCode.isPending ? "正在发送" : remaining > 0 ? `${remaining} 秒后重发` : "获取验证码"}</button>
              </div>
            </label>}
            {sendCode.data && codeRequired && <p role="status" className="mt-2 text-[13px] text-white/85">{sendCode.data.message}</p>}
            {sendCode.error && <div className="mt-3"><ErrorBar text={errorText(sendCode.error)} /></div>}
            <p className="mt-3 text-[12px] text-white/65">注册后创建独立个人空间，单证与对话按账号隔离。</p>
          </>}
          {login.error && <div className="mt-3"><ErrorBar text={errorText(login.error)} /></div>}

          <button type="submit" className="btn btn-primary btn-lg mt-5 w-full" disabled={!canSubmit}>
            {login.isPending ? (isRegister ? "正在注册" : "正在登录") : (isRegister ? "注册并登录" : "登录")}
          </button>
        </form>

        <p className="mt-4 text-center text-[13px] text-white/75">
          {isRegister ? "已有账号？" : config.data?.enabled ? "还没有账号？" : "账号由管理员开通"}
          {(isRegister || config.data?.enabled) && <button type="button" className="font-medium text-white underline underline-offset-2" disabled={login.isPending || sendCode.isPending} onClick={() => switchMode(isRegister ? "login" : "register")}>{isRegister ? "返回登录" : "立即注册"}</button>}
          {config.error && <span className="block mt-2">注册信息暂不可用，请稍后重试</span>}
        </p>
      </div>
    </div>,
    document.body,
  );
}
