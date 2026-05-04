import { useEffect, useMemo, useState } from "react";
import { ArrowRight, ChevronLeft, KeyRound, RotateCcw, UserRound } from "lucide-react";
import surfacedWordmark from "../../assets/surfaced-wordmark.svg";

export type LoginPayload = {
  username: string;
  password: string;
  baseUrl: string;
};

export type RegisterPayload = {
  email: string;
  password: string;
  displayName: string;
  workspaceName: string;
  baseUrl: string;
};

export type VerifyEmailPayload = {
  email: string;
  code: string;
  baseUrl: string;
};

export type RequestPasswordResetPayload = {
  email: string;
  baseUrl: string;
};

export type ResetPasswordPayload = {
  email: string;
  code: string;
  password: string;
  baseUrl: string;
};

type LoginScreenProps = {
  defaultIdentity?: string;
  defaultBaseUrl?: string;
  onLogin: (payload: LoginPayload) => void | Promise<void>;
  onRegister?: (payload: RegisterPayload) => void | Promise<{ requiresEmailVerification?: boolean; email?: string } | void>;
  onVerifyEmail?: (payload: VerifyEmailPayload) => void | Promise<void>;
  onResendEmailCode?: (payload: { email: string; baseUrl: string }) => void | Promise<void>;
  onRequestPasswordReset?: (payload: RequestPasswordResetPayload) => void | Promise<void>;
  onResetPassword?: (payload: ResetPasswordPayload) => void | Promise<void>;
  variant?: "page" | "panel";
};

const DEFAULT_BASE_URL = "https://api.surfacedlab.com";

function pageInputClassName() {
  return "h-[46px] w-full rounded-[12px] border border-gray-200/80 bg-white/76 px-4 text-center text-[14px] font-semibold text-gray-900 shadow-[0_16px_36px_-32px_rgba(15,23,42,0.32)] outline-none transition-all placeholder:text-gray-400 focus:border-blue-200 focus:bg-white focus:ring-4 focus:ring-blue-50";
}

function panelInputClassName() {
  return "w-full rounded-[14px] border border-gray-200 bg-white px-3.5 py-3 text-[13px] font-medium text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-blue-200 focus:ring-4 focus:ring-blue-50";
}

export function LoginScreen({
  defaultIdentity,
  defaultBaseUrl = DEFAULT_BASE_URL,
  onLogin,
  onRegister,
  onVerifyEmail,
  onResendEmailCode,
  onRequestPasswordReset,
  onResetPassword,
  variant = "page",
}: LoginScreenProps) {
  const isPanel = variant === "panel";
  const [mode, setMode] = useState<"login" | "register" | "verify" | "forgot" | "reset">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [verificationCode, setVerificationCode] = useState("");
  const [pendingVerificationEmail, setPendingVerificationEmail] = useState("");
  const [pendingResetEmail, setPendingResetEmail] = useState("");
  const [baseUrl, setBaseUrl] = useState(defaultBaseUrl);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [statusMessage, setStatusMessage] = useState("");

  useEffect(() => {
    if (defaultIdentity) {
      setUsername(defaultIdentity);
    }
  }, [defaultIdentity]);

  useEffect(() => {
    setBaseUrl(defaultBaseUrl || DEFAULT_BASE_URL);
  }, [defaultBaseUrl]);

  const canSubmit = useMemo(() => {
    const hasIdentity = username.trim().length > 0;
    const hasPassword = password.length > 0;
    const hasBaseUrl = baseUrl.trim().length > 0;
    const hasRegisterPassword = mode === "login" || password.length >= 8;
    if (mode === "verify") {
      return verificationCode.trim().length >= 4 && pendingVerificationEmail && hasBaseUrl && !isSubmitting;
    }
    if (mode === "forgot") {
      return hasIdentity && hasBaseUrl && !isSubmitting;
    }
    if (mode === "reset") {
      return verificationCode.trim().length >= 4 && pendingResetEmail && password.length >= 8 && hasBaseUrl && !isSubmitting;
    }
    return hasIdentity && hasPassword && hasBaseUrl && hasRegisterPassword && !isSubmitting;
  }, [baseUrl, isSubmitting, mode, password, pendingResetEmail, pendingVerificationEmail, username, verificationCode]);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextUsername = username.trim();
    const nextBaseUrl = baseUrl.trim();
    if (mode === "verify") {
      if (!pendingVerificationEmail || !verificationCode.trim()) {
        setErrorMessage("请输入邮箱验证码");
        return;
      }
      setIsSubmitting(true);
      setErrorMessage("");
      try {
        await onVerifyEmail?.({
          email: pendingVerificationEmail,
          code: verificationCode.trim(),
          baseUrl: nextBaseUrl,
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : "邮箱验证失败，请稍后再试";
        setErrorMessage(message);
      } finally {
        setIsSubmitting(false);
      }
      return;
    }
    if (mode === "forgot") {
      if (!nextUsername || !nextBaseUrl) {
        setErrorMessage("请输入管理员邮箱");
        return;
      }
      setIsSubmitting(true);
      setErrorMessage("");
      setStatusMessage("");
      try {
        await onRequestPasswordReset?.({ email: nextUsername, baseUrl: nextBaseUrl });
        setPendingResetEmail(nextUsername);
        setVerificationCode("");
        setPassword("");
        setMode("reset");
        setStatusMessage("如果该邮箱已注册，验证码将发送至对应邮箱");
      } catch (error) {
        const message = error instanceof Error ? error.message : "找回密码请求失败";
        setErrorMessage(message);
      } finally {
        setIsSubmitting(false);
      }
      return;
    }
    if (mode === "reset") {
      if (!pendingResetEmail || !verificationCode.trim() || password.length < 8) {
        setErrorMessage("请输入验证码和至少 8 位新密码");
        return;
      }
      setIsSubmitting(true);
      setErrorMessage("");
      setStatusMessage("");
      try {
        await onResetPassword?.({
          email: pendingResetEmail,
          code: verificationCode.trim(),
          password,
          baseUrl: nextBaseUrl,
        });
        setUsername(pendingResetEmail);
        setPassword("");
        setVerificationCode("");
        setPendingResetEmail("");
        setMode("login");
        setStatusMessage("密码已重置，请使用新密码登录");
      } catch (error) {
        const message = error instanceof Error ? error.message : "密码重置失败，请稍后再试";
        setErrorMessage(message);
      } finally {
        setIsSubmitting(false);
      }
      return;
    }
    if (!nextUsername || !password || !nextBaseUrl) {
      setErrorMessage("请输入账号和密码");
      return;
    }
    if (mode === "register" && password.length < 8) {
      setErrorMessage("密码至少需要 8 位");
      return;
    }

    setIsSubmitting(true);
    setErrorMessage("");
    setStatusMessage("");
    try {
      if (mode === "register") {
        const result = await onRegister?.({
          email: nextUsername,
          password,
          displayName: displayName.trim(),
          workspaceName: displayName.trim() || nextUsername.split("@", 1)[0] || "Surfaced Workspace",
          baseUrl: nextBaseUrl,
        });
        if (result?.requiresEmailVerification) {
          setPendingVerificationEmail(result.email || nextUsername);
          setVerificationCode("");
          setMode("verify");
          setErrorMessage("");
          return;
        }
      } else {
        await onLogin({
          username: nextUsername,
          password,
          baseUrl: nextBaseUrl,
        });
      }
      setPassword("");
    } catch (error) {
      const message = error instanceof Error ? error.message : "登录失败，请稍后再试";
      setErrorMessage(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  const resetToLogin = () => {
    setMode("login");
    setErrorMessage("");
    setStatusMessage("");
    setVerificationCode("");
    setPendingVerificationEmail("");
    setPendingResetEmail("");
  };

  const enterRegisterMode = () => {
    setMode("register");
    setPassword("");
    setVerificationCode("");
    setErrorMessage("");
    setStatusMessage("");
  };

  const enterForgotMode = () => {
    setMode("forgot");
    setPassword("");
    setVerificationCode("");
    setErrorMessage("");
    setStatusMessage("");
  };

  const primaryButtonLabel = isSubmitting
    ? (mode === "verify" ? "验证中..." : mode === "register" ? "注册中..." : mode === "forgot" ? "发送中..." : mode === "reset" ? "重置中..." : "登录中...")
    : (mode === "verify" ? "验证并登录" : mode === "register" ? "注册" : mode === "forgot" ? "发送验证码" : mode === "reset" ? "重置密码" : "登录");

  if (isPanel) {
    return (
      <div className="w-full text-gray-900">
        <div className="flex items-center justify-between gap-3">
          <img
            src={surfacedWordmark}
            alt="Surfaced"
            className="h-auto w-[108px] select-none"
            draggable={false}
          />
          <span className="text-[11px] font-medium tracking-wide text-gray-400">云端账号</span>
        </div>

        <form className="mt-5 space-y-3" onSubmit={handleSubmit}>
          <label className="block">
            <span className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-gray-500">
              <UserRound className="h-3.5 w-3.5 text-blue-600" />
              账号
            </span>
            <input
              type="text"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="管理员邮箱或普通账号"
              autoComplete="username"
              className={panelInputClassName()}
            />
          </label>

          <label className="block">
            <span className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-gray-500">
              <KeyRound className="h-3.5 w-3.5 text-blue-600" />
              密码
            </span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="请输入密码"
              autoComplete="current-password"
              className={panelInputClassName()}
            />
          </label>

          {errorMessage ? (
            <div className="rounded-[14px] border border-red-100 bg-red-50 px-3.5 py-2.5 text-[11px] font-medium text-red-600">
              {errorMessage}
            </div>
          ) : null}

          <button
            type="submit"
            disabled={!canSubmit}
            className="inline-flex w-full items-center justify-center gap-2 rounded-[16px] bg-[#111827] px-4 py-3 text-[14px] font-semibold text-white transition-all hover:bg-[#0f172a] disabled:cursor-not-allowed disabled:bg-gray-300"
          >
            {isSubmitting ? "登录中..." : "登录"}
            <ArrowRight className="h-4 w-4" />
          </button>
        </form>
      </div>
    );
  }

  return (
    <div
      className="relative flex min-h-screen w-full overflow-hidden bg-[#fcfdff] text-gray-900"
      style={{
        background:
          "radial-gradient(circle at 50% 28%, rgba(20,199,243,0.10), transparent 34%), linear-gradient(180deg, #fcfdff 0%, #f7faff 100%)",
      }}
    >
      <div className="pointer-events-none absolute inset-x-8 top-0 h-px bg-gradient-to-r from-transparent via-blue-200/70 to-transparent" />

      <div className="absolute right-5 top-5 z-10 sm:right-8 sm:top-7">
        {mode === "login" ? (
          <div className="flex items-center rounded-full border border-gray-200/70 bg-white/68 px-1.5 py-1 text-[11px] font-semibold text-gray-400 shadow-[0_20px_60px_-46px_rgba(15,23,42,0.42)] backdrop-blur-md">
            <button
              type="button"
              className="rounded-full px-3 py-1.5 transition-colors hover:bg-white hover:text-[#173A43]"
              onClick={enterRegisterMode}
            >
              注册管理账号
            </button>
            <span className="h-3.5 w-px bg-gray-200/80" />
            <button
              type="button"
              className="rounded-full px-3 py-1.5 transition-colors hover:bg-white hover:text-[#173A43]"
              onClick={enterForgotMode}
            >
              忘记密码
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="inline-flex h-9 items-center gap-1.5 rounded-full border border-gray-200/70 bg-white/68 px-3.5 text-[12px] font-semibold text-gray-500 shadow-[0_20px_60px_-46px_rgba(15,23,42,0.42)] backdrop-blur-md transition-colors hover:bg-white hover:text-[#173A43]"
            onClick={resetToLogin}
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            返回登录
          </button>
        )}
      </div>

      <div className="absolute left-1/2 top-[39%] flex w-[min(328px,calc(100vw-40px))] -translate-x-1/2 -translate-y-1/2 flex-col items-center">
        <div className="flex w-full flex-col items-center">
          <img
            src={surfacedWordmark}
            alt="Surfaced"
            className="block h-auto w-[248px] translate-x-[3px] select-none"
            draggable={false}
          />

          <form className="mt-7 flex w-full flex-col items-center gap-2.5" onSubmit={handleSubmit}>
            {mode === "verify" || mode === "reset" ? (
              <div className="mb-1 flex min-h-9 w-full items-center justify-center rounded-full border border-blue-100/80 bg-white/58 px-4 text-center text-[12px] font-semibold text-[#173A43]/80 shadow-[0_14px_38px_-34px_rgba(20,199,243,0.55)] backdrop-blur-sm">
                {mode === "verify" ? pendingVerificationEmail : pendingResetEmail}
              </div>
            ) : null}

            {mode === "verify" || mode === "reset" ? null : (
              <label className="relative block w-full">
                <UserRound className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                <input
                  type="text"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  placeholder={mode === "register" || mode === "forgot" ? "管理员邮箱" : "管理员邮箱或普通账号"}
                  autoComplete="username"
                  className={`${pageInputClassName()} pl-10 pr-10`}
                />
              </label>
            )}

            {mode === "register" ? (
              <label className="relative block w-full">
                <UserRound className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                <input
                  type="text"
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder="管理员名称"
                  autoComplete="name"
                  className={`${pageInputClassName()} pl-10 pr-10`}
                />
              </label>
            ) : null}

            {mode === "forgot" ? null : (
              <label className="relative block w-full">
                <KeyRound className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                {mode === "verify" || mode === "reset" ? (
                  <input
                    type="text"
                    inputMode="numeric"
                    value={verificationCode}
                    onChange={(event) => setVerificationCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                    placeholder="邮箱验证码"
                    autoComplete="one-time-code"
                    className={`${pageInputClassName()} pl-10 pr-10`}
                  />
                ) : (
                  <input
                    type="password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder={mode === "register" ? "设置密码，至少 8 位" : "密码"}
                    autoComplete={mode === "register" ? "new-password" : "current-password"}
                    className={`${pageInputClassName()} pl-10 pr-10`}
                  />
                )}
              </label>
            )}

            {mode === "reset" ? (
              <label className="relative block w-full">
                <KeyRound className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="新密码，至少 8 位"
                  autoComplete="new-password"
                  className={`${pageInputClassName()} pl-10 pr-10`}
                />
              </label>
            ) : null}

            {errorMessage ? (
              <div className="w-full rounded-[12px] border border-red-100 bg-red-50/90 px-3.5 py-2.5 text-center text-[12px] font-medium text-red-600">
                {errorMessage}
              </div>
            ) : null}
            {statusMessage ? (
              <div className="w-full rounded-[12px] border border-blue-100 bg-blue-50/90 px-3.5 py-2.5 text-center text-[12px] font-medium text-[#173A43]">
                {statusMessage}
              </div>
            ) : null}

            <button
              type="submit"
              disabled={!canSubmit}
              className="inline-flex h-[46px] w-full items-center justify-center gap-2 rounded-[12px] border border-gray-200/80 bg-white/76 px-4 text-[14px] font-semibold text-gray-700 shadow-[0_18px_40px_-30px_rgba(23,58,67,0.38)] transition-all hover:border-blue-200 hover:bg-white hover:text-[#173A43] hover:shadow-[0_20px_42px_-30px_rgba(20,199,243,0.45)] disabled:cursor-not-allowed disabled:opacity-45"
            >
              {primaryButtonLabel}
              <ArrowRight className="h-4 w-4" />
            </button>
          </form>

          {mode === "verify" ? (
            <button
              type="button"
              className="mt-4 inline-flex items-center gap-1.5 text-[11px] font-semibold text-gray-400 transition-colors hover:text-[#173A43]"
              onClick={async () => {
                setErrorMessage("");
                try {
                  await onResendEmailCode?.({ email: pendingVerificationEmail, baseUrl });
                } catch (error) {
                  const message = error instanceof Error ? error.message : "验证码重发失败";
                  setErrorMessage(message);
                }
              }}
            >
              <RotateCcw className="h-3 w-3" />
              重新发送验证码
            </button>
          ) : null}
        </div>
      </div>

      <div className="absolute bottom-7 left-8 text-[11px] font-bold uppercase tracking-[0.16em] text-gray-400">
        Surfaced
      </div>
    </div>
  );
}
