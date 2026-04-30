import { useEffect, useMemo, useState } from "react";
import { ArrowRight, AtSign, KeyRound, ShieldCheck, UserRound } from "lucide-react";
import surfacedWordmark from "../../assets/surfaced-wordmark.svg";
import { PatternLoadingAnimation } from "./PatternLoadingAnimation";

export type LoginPayload =
  | {
      method: "email";
      email: string;
      code: string;
    }
  | {
      method: "account";
      account: string;
      password: string;
    };

type LoginScreenProps = {
  defaultIdentity?: string;
  onLogin: (payload: LoginPayload) => void | Promise<void>;
  variant?: "page" | "panel";
};

type LoginMethod = "email" | "account";

function inputClassName() {
  return "w-full rounded-[22px] border border-gray-200/80 bg-white px-4 py-3.5 text-[14px] font-medium text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-blue-200 focus:ring-4 focus:ring-blue-50";
}

function methodTabClassName(active: boolean) {
  return `inline-flex flex-1 items-center justify-center rounded-[18px] px-3 py-2.5 text-[13px] font-bold tracking-wide transition-all ${
    active ? "bg-[#111827] text-white shadow-[0_12px_28px_-18px_rgba(15,23,42,0.48)]" : "text-gray-500 hover:text-gray-900"
  }`;
}

function compactInputClassName() {
  return "w-full rounded-[16px] border border-gray-200 bg-white px-3.5 py-3 text-[13px] font-medium text-gray-900 outline-none transition-all placeholder:text-gray-400 focus:border-blue-200 focus:ring-4 focus:ring-blue-50";
}

function compactMethodTabClassName(active: boolean) {
  return `inline-flex flex-1 items-center justify-center rounded-[12px] px-2.5 py-2 text-[12px] font-semibold transition-all ${
    active ? "bg-[#111827] text-white" : "text-gray-500 hover:text-gray-900"
  }`;
}

export function LoginScreen({
  defaultIdentity,
  onLogin,
  variant = "page",
}: LoginScreenProps) {
  const isPanel = variant === "panel";
  const [method, setMethod] = useState<LoginMethod>("email");
  const [email, setEmail] = useState("");
  const [emailCode, setEmailCode] = useState("");
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [agree, setAgree] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    if (!defaultIdentity) {
      return;
    }
    if (defaultIdentity.includes("@")) {
      setMethod("email");
      setEmail(defaultIdentity);
      return;
    }
    setMethod("account");
    setAccount(defaultIdentity);
  }, [defaultIdentity]);

  const canSubmit = useMemo(() => {
    if (!agree || isSubmitting) {
      return false;
    }
    if (method === "email") {
      return email.trim().length > 0 && emailCode.trim().length > 0;
    }
    return account.trim().length > 0 && password.trim().length > 0;
  }, [account, agree, email, emailCode, isSubmitting, method, password]);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!agree) {
      setErrorMessage("请先阅读并同意使用说明");
      return;
    }

    if (method === "email") {
      const nextEmail = email.trim();
      const nextCode = emailCode.trim();
      if (!nextEmail || !nextCode) {
        setErrorMessage("请输入邮箱和验证码后再登录");
        return;
      }
      setIsSubmitting(true);
      setErrorMessage("");
      try {
        await onLogin({
          method: "email",
          email: nextEmail,
          code: nextCode,
        });
        setEmailCode("");
      } catch (error) {
        const message = error instanceof Error ? error.message : "登录失败，请稍后再试";
        setErrorMessage(message);
      } finally {
        setIsSubmitting(false);
      }
      return;
    }

    const nextAccount = account.trim();
    const nextPassword = password.trim();
    if (!nextAccount || !nextPassword) {
      setErrorMessage("请输入账号和密码后再登录");
      return;
    }

    setIsSubmitting(true);
    setErrorMessage("");
    try {
      await onLogin({
        method: "account",
        account: nextAccount,
        password: nextPassword,
      });
      setPassword("");
    } catch (error) {
      const message = error instanceof Error ? error.message : "登录失败，请稍后再试";
      setErrorMessage(message);
    } finally {
      setIsSubmitting(false);
    }
  };

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
          <span className="text-[11px] font-medium tracking-wide text-gray-400">恢复账号权限</span>
        </div>

        <div className="mt-4 rounded-[14px] bg-[#f5f7fb] p-1">
          <div className="flex items-center gap-1">
            <button type="button" className={compactMethodTabClassName(method === "email")} onClick={() => setMethod("email")}>
              邮箱登录
            </button>
            <button type="button" className={compactMethodTabClassName(method === "account")} onClick={() => setMethod("account")}>
              账号密码
            </button>
          </div>
        </div>

        <form className="mt-4 space-y-3" onSubmit={handleSubmit}>
          {method === "email" ? (
            <>
              <label className="block">
                <span className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-gray-500">
                  <AtSign className="h-3.5 w-3.5 text-blue-600" />
                  邮箱
                </span>
                <input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="请输入邮箱地址"
                  autoComplete="email"
                  className={compactInputClassName()}
                />
              </label>

              <label className="block">
                <div className="mb-1.5 flex items-center justify-between gap-3 text-[11px] font-semibold tracking-wide text-gray-500">
                  <span className="flex items-center gap-1.5">
                    <KeyRound className="h-3.5 w-3.5 text-blue-600" />
                    验证码
                  </span>
                  <button
                    type="button"
                    className="text-[11px] font-semibold text-blue-600 transition-colors hover:text-blue-700"
                    onClick={() => setErrorMessage("演示模式下未接入真实验证码发送，这里仅保留界面流程。")}
                  >
                    发送验证码
                  </button>
                </div>
                <input
                  type="text"
                  value={emailCode}
                  onChange={(event) => setEmailCode(event.target.value)}
                  placeholder="请输入邮箱验证码"
                  className={compactInputClassName()}
                />
              </label>
            </>
          ) : (
            <>
              <label className="block">
                <span className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-gray-500">
                  <UserRound className="h-3.5 w-3.5 text-blue-600" />
                  账号
                </span>
                <input
                  type="text"
                  value={account}
                  onChange={(event) => setAccount(event.target.value)}
                  placeholder="请输入账号"
                  autoComplete="username"
                  className={compactInputClassName()}
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
                  className={compactInputClassName()}
                />
              </label>
            </>
          )}

          <label className="flex items-start gap-2 rounded-[14px] border border-gray-100 bg-[#f8fafc] px-3.5 py-3 text-[11px] leading-5 text-gray-500">
            <button
              type="button"
              onClick={() => setAgree((prev) => !prev)}
              className={`mt-0.5 flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full border text-[10px] transition-colors ${
                agree ? "border-[#111827] bg-[#111827] text-white" : "border-gray-300 bg-white text-transparent"
              }`}
            >
              <span>✓</span>
            </button>
            <span>同意使用说明并恢复账号权限</span>
          </label>

          {errorMessage ? (
            <div className="rounded-[14px] border border-red-100 bg-red-50 px-3.5 py-2.5 text-[11px] font-medium text-red-600">
              {errorMessage}
            </div>
          ) : null}

          <button
            type="submit"
            disabled={!canSubmit}
            className="inline-flex w-full items-center justify-center gap-2 rounded-[18px] bg-[#111827] px-4 py-3 text-[14px] font-semibold text-white transition-all hover:bg-[#0f172a] disabled:cursor-not-allowed disabled:bg-gray-300"
          >
            {isSubmitting ? "登录中..." : "登录"}
            <ArrowRight className="h-4 w-4" />
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="app-canvas relative flex min-h-screen w-full items-center justify-center overflow-hidden bg-[#fcfdff] px-8 py-10 text-gray-900">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(96,165,250,0.12),transparent_34%),radial-gradient(circle_at_bottom_right,rgba(15,23,42,0.08),transparent_24%)]" />

      <div className="pointer-events-none absolute inset-0 opacity-70">
        <div className="absolute left-8 top-8 h-28 w-44 rounded-[26px] bg-white/72 shadow-[0_24px_60px_-42px_rgba(15,23,42,0.26)] blur-[1px]" />
        <div className="absolute bottom-10 right-10 h-36 w-48 rounded-[30px] bg-white/76 shadow-[0_24px_60px_-42px_rgba(15,23,42,0.26)] blur-[1px]" />
      </div>

      <div className="relative w-full max-w-[540px]">
        <div className="rounded-[36px] border border-white/80 bg-white/94 px-7 py-8 shadow-[0_45px_140px_-60px_rgba(15,23,42,0.36)] backdrop-blur-xl sm:px-9 sm:py-9">
          <div className="flex items-start justify-between gap-6">
            <div>
              <img
                src={surfacedWordmark}
                alt="Surfaced"
                className="h-auto w-[146px] select-none"
                draggable={false}
              />
              <div className="mt-6 text-[12px] font-semibold uppercase tracking-[0.26em] text-blue-600/80">
                Account Access
              </div>
              <h1 className="mt-3 text-[30px] font-black tracking-tight text-gray-900">
                登录后恢复账号权限
              </h1>
              <p className="mt-3 max-w-[34ch] text-[14px] leading-7 text-gray-500">
                退出后本地活动仍然保留，但账号信息和高级权限会收起。在这里重新登录后，就会恢复到你原来的账号状态。
              </p>
            </div>

            <div className="flex h-[104px] w-[104px] shrink-0 items-center justify-center rounded-[28px] bg-[linear-gradient(180deg,#f8fbff_0%,#eef4fb_100%)] shadow-[0_24px_60px_-42px_rgba(59,130,246,0.42)]">
              <PatternLoadingAnimation className="h-[68px] w-[68px]" />
            </div>
          </div>

          <div className="mt-8 rounded-[22px] border border-gray-200/80 bg-[#f8fafc] p-1.5">
            <div className="flex items-center gap-1.5">
              <button type="button" className={methodTabClassName(method === "email")} onClick={() => setMethod("email")}>
                邮箱登录
              </button>
              <button type="button" className={methodTabClassName(method === "account")} onClick={() => setMethod("account")}>
                账号密码登录
              </button>
            </div>
          </div>

          <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
            {method === "email" ? (
              <>
                <label className="block">
                  <span className="mb-2 flex items-center gap-2 text-[12px] font-bold tracking-wide text-gray-500">
                    <AtSign className="h-3.5 w-3.5 text-blue-600" />
                    邮箱
                  </span>
                  <input
                    type="email"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="请输入邮箱地址"
                    autoComplete="email"
                    className={inputClassName()}
                  />
                </label>

                <div>
                  <div className="mb-2 flex items-center justify-between text-[12px] font-bold tracking-wide text-gray-500">
                    <span className="flex items-center gap-2">
                      <KeyRound className="h-3.5 w-3.5 text-blue-600" />
                      邮箱验证码
                    </span>
                    <button
                      type="button"
                      className="text-[12px] font-semibold text-blue-600 transition-colors hover:text-blue-700"
                      onClick={() => setErrorMessage("演示模式下未接入真实验证码发送，这里仅保留界面流程。")}
                    >
                      发送验证码
                    </button>
                  </div>
                  <input
                    type="text"
                    value={emailCode}
                    onChange={(event) => setEmailCode(event.target.value)}
                    placeholder="请输入邮箱验证码"
                    className={inputClassName()}
                  />
                </div>
              </>
            ) : (
              <>
                <label className="block">
                  <span className="mb-2 flex items-center gap-2 text-[12px] font-bold tracking-wide text-gray-500">
                    <UserRound className="h-3.5 w-3.5 text-blue-600" />
                    账号
                  </span>
                  <input
                    type="text"
                    value={account}
                    onChange={(event) => setAccount(event.target.value)}
                    placeholder="请输入账号"
                    autoComplete="username"
                    className={inputClassName()}
                  />
                </label>

                <label className="block">
                  <span className="mb-2 flex items-center gap-2 text-[12px] font-bold tracking-wide text-gray-500">
                    <KeyRound className="h-3.5 w-3.5 text-blue-600" />
                    密码
                  </span>
                  <input
                    type="password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="请输入密码"
                    autoComplete="current-password"
                    className={inputClassName()}
                  />
                </label>
              </>
            )}

            <div className="flex items-center justify-between rounded-[22px] border border-gray-100 bg-[#f8fafc] px-4 py-3 text-[12px] text-gray-500">
              <label className="inline-flex items-center gap-2 select-none">
                <button
                  type="button"
                  onClick={() => setAgree((prev) => !prev)}
                  className={`flex h-5 w-5 items-center justify-center rounded-full border transition-colors ${
                    agree ? "border-[#111827] bg-[#111827] text-white" : "border-gray-300 bg-white text-transparent"
                  }`}
                >
                  <span className="text-[11px]">✓</span>
                </button>
                <span>同意使用说明并恢复账号权限</span>
              </label>
              <div className="flex items-center gap-1.5 font-semibold text-gray-400">
                <ShieldCheck className="h-3.5 w-3.5 text-blue-600" />
                访客模式
              </div>
            </div>

            {errorMessage ? (
              <div className="rounded-[18px] border border-red-100 bg-red-50 px-4 py-3 text-[12px] font-medium text-red-600">
                {errorMessage}
              </div>
            ) : null}

            <button
              type="submit"
              disabled={!canSubmit}
              className="inline-flex w-full items-center justify-center gap-2 rounded-[26px] bg-[linear-gradient(135deg,#111827_0%,#1f3a5f_100%)] px-5 py-3.5 text-[15px] font-bold text-white transition-all hover:brightness-105 disabled:cursor-not-allowed disabled:bg-gray-300 disabled:brightness-100"
            >
              {isSubmitting ? "登录中..." : "登录"}
              <ArrowRight className="h-4 w-4" />
            </button>
          </form>

          <div className="mt-7 grid grid-cols-3 gap-3">
            <MiniFeature title="本地活动" value="继续可见" />
            <MiniFeature title="账号信息" value="登录恢复" />
            <MiniFeature title="入口位置" value="账号页面" />
          </div>
        </div>
      </div>
    </div>
  );
}

function MiniFeature({ title, value }: { title: string; value: string }) {
  return (
    <div className="rounded-[20px] border border-gray-100 bg-[#f8fafc] px-4 py-4">
      <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-gray-400">{title}</div>
      <div className="mt-2 text-[14px] font-bold tracking-tight text-gray-900">{value}</div>
    </div>
  );
}
