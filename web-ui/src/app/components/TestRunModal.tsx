import { CheckCircle2, Loader2, Send, X, XCircle } from "lucide-react";
import type { TestRunStatus } from "../lib/backend";

interface TestRunModalProps {
  isOpen: boolean;
  onBackgroundContinue: () => void;
  onAbort: () => void;
  onForceSend?: () => void;
  brandName: string;
  status: TestRunStatus | null;
  abortPending?: boolean;
  forceSendPending?: boolean;
  forceSendMessage?: string;
  forceSendError?: string;
}

export function TestRunModal({
  isOpen,
  onBackgroundContinue,
  onAbort,
  onForceSend,
  brandName,
  status,
  abortPending = false,
  forceSendPending = false,
  forceSendMessage = "",
  forceSendError = "",
}: TestRunModalProps) {
  if (!isOpen) return null;

  const runtimeStatus = status?.status || "queued";
  const isRunning = runtimeStatus === "queued" || runtimeStatus === "running";
  const isSuccess = runtimeStatus === "success";
  const isCancelled = runtimeStatus === "cancelled";
  const currentQuery = status?.currentQuery || 0;
  const totalQueries = status?.totalQueries || 0;
  const currentAttempt = status?.currentAttempt || 0;
  const totalAttempts = status?.totalAttempts || 0;
  const currentKeyword = String(status?.currentKeyword || "").trim();
  const currentPlatform = String(status?.currentPlatform || "").trim();
  const errorMessage = String(status?.errorMessage || "").trim();
  const sendableSuccessCount = Math.max(
    0,
    Number(status?.sendableSuccessCount ?? status?.actualScreenshotCount ?? 0) || 0,
  );
  const showForceSend = runtimeStatus === "failed" && sendableSuccessCount > 0 && Boolean(onForceSend);
  const failedDetails = Array.isArray(status?.failureDetails)
    ? status.failureDetails
        .map((item) => ({
          keyword: String(item?.keyword || "").trim(),
          platform: String(item?.platform || "").trim(),
          errorMessage: String(item?.errorMessage || "").trim(),
        }))
        .filter((item) => item.keyword || item.platform || item.errorMessage)
    : [];
  const fallbackFailureDetails = failedDetails.length > 0
    ? failedDetails
    : (currentKeyword || currentPlatform || errorMessage)
      ? [{
          keyword: currentKeyword,
          platform: currentPlatform,
          errorMessage,
        }]
      : [];
  const progressLabel = totalAttempts > 0
    ? `第 ${Math.max(1, currentAttempt)} / ${totalAttempts} 次`
    : totalQueries > 0
      ? `第 ${Math.max(1, currentQuery)} / ${totalQueries} 次`
      : "准备中";
  const progressPercent = totalAttempts > 0
    ? Math.min(100, Math.max((Math.max(1, currentAttempt) / totalAttempts) * 100, isRunning ? 8 : 0))
    : totalQueries > 0
    ? Math.min(100, Math.max(((status?.completedQueries || 0) / totalQueries) * 100, isRunning ? 8 : 0))
      : (isRunning ? 8 : 100);
  const detailText = isRunning
    ? (status?.message || "正在准备测试任务...")
    : isSuccess
      ? (status?.message || "测试成功")
      : isCancelled
        ? "测试已中断"
        : "测试失败";
  const resultLabel = isSuccess ? "成功" : isCancelled ? "已中断" : "失败";
  const resultClassName = isSuccess ? "text-blue-600" : isCancelled ? "text-gray-600" : "text-red-600";
  const progressClassName = isSuccess
    ? "bg-blue-600"
    : isCancelled
      ? "bg-gray-400"
      : runtimeStatus === "failed"
        ? "bg-red-500"
        : "bg-blue-500";
  const locationTitle = isRunning ? "当前执行" : isCancelled ? "中断位置" : "失败位置";
  const showLocation = Boolean(currentPlatform || currentKeyword) && !isSuccess;

  const icon = isRunning ? (
    <Loader2 className="w-5 h-5 text-blue-600 animate-spin" />
  ) : isSuccess ? (
    <CheckCircle2 className="w-5 h-5 text-blue-600" />
  ) : isCancelled ? (
    <XCircle className="w-5 h-5 text-gray-500" />
  ) : (
    <XCircle className="w-5 h-5 text-red-600" />
  );

  const iconBg = isRunning || isSuccess
    ? "bg-blue-50 border-blue-100"
    : isCancelled
      ? "bg-gray-50 border-gray-200"
    : "bg-red-50 border-red-100";
  const handleTopRightClose = isRunning ? onAbort : onBackgroundContinue;
  const topRightCloseLabel = isRunning ? "取消测试任务" : "关闭弹窗";

  return (
    <div className="fixed inset-0 z-[110] flex items-center justify-center">
      <div
        className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-200"
        onClick={onBackgroundContinue}
      />

      <div className="relative bg-white rounded-2xl shadow-xl border border-gray-200 w-full max-w-[420px] p-6 animate-in zoom-in-95 duration-200 mx-4">
        <button
          type="button"
          onClick={handleTopRightClose}
          disabled={abortPending || forceSendPending}
          aria-label={topRightCloseLabel}
          title={topRightCloseLabel}
          className="absolute top-4 right-4 p-1.5 text-gray-400 hover:text-gray-900 hover:bg-gray-100 rounded-lg transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
        >
          <X className="w-4 h-4" />
        </button>

        <div className="flex flex-col items-center text-center gap-4">
          <div className={`w-12 h-12 rounded-full flex items-center justify-center border shadow-sm ${iconBg}`}>
            {icon}
          </div>

          <div className="flex flex-col gap-1.5">
            <h3 className="text-[16px] font-bold text-gray-900 tracking-tight">品牌测试状态</h3>
            <p className="text-[13px] text-gray-500 leading-relaxed max-w-[300px]">
              正在测试品牌「{brandName}」
            </p>
          </div>
        </div>

        <div className="mt-6 rounded-2xl border border-gray-200 bg-gray-50/70 p-4 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">当前进度</span>
            <span className="text-[13px] font-bold text-gray-900">{progressLabel}</span>
          </div>

          <div className="h-2 rounded-full bg-white border border-gray-200 overflow-hidden">
            <div
              className={`h-full transition-all duration-300 ${progressClassName}`}
              style={{ width: `${progressPercent}%` }}
            />
          </div>

          <div className="flex items-center justify-between gap-4">
            <span className="text-[12px] text-gray-500">当前状态</span>
            <span className={`text-[12px] font-bold ${isSuccess ? "text-blue-600" : isCancelled ? "text-gray-600" : runtimeStatus === "failed" ? "text-red-600" : "text-blue-600"}`}>
              {detailText}
            </span>
          </div>

          {showLocation && (
            <div className="text-left rounded-xl bg-white border border-gray-200 px-3 py-2.5">
              <p className="text-[10px] font-bold text-gray-400 tracking-[0.12em] uppercase">{locationTitle}</p>
              <p className="text-[11px] text-gray-500 leading-relaxed">
                {currentPlatform ? `${currentPlatform} · ` : ""}
                {currentKeyword || ""}
              </p>
            </div>
          )}

          {!isRunning && !isSuccess && fallbackFailureDetails.length > 0 && (
            <div className={`text-left rounded-xl px-3 py-3 ${isCancelled ? "bg-gray-50 border border-gray-200" : "bg-red-50 border border-red-100"}`}>
              <p className={`text-[10px] font-bold tracking-[0.12em] uppercase ${isCancelled ? "text-gray-500" : "text-red-500"}`}>
                {isCancelled ? "中断信息" : "失败明细"}
              </p>
              <div className="mt-2 space-y-2 max-h-56 overflow-y-auto pr-1">
                {fallbackFailureDetails.map((item, index) => (
                  <div
                    key={`${item.keyword || "keyword"}-${item.platform || "platform"}-${index}`}
                    className={`rounded-lg border px-3 py-2 ${isCancelled ? "border-gray-200 bg-white" : "border-red-100 bg-white/80"}`}
                  >
                    <div className={`text-[12px] font-bold leading-relaxed ${isCancelled ? "text-gray-800" : "text-red-800"}`}>
                      {item.keyword || item.platform
                        ? `${item.keyword || "未定位到关键词"}${item.platform ? ` · ${item.platform}` : ""}`
                        : "状态说明"}
                    </div>
                    <div className={`mt-1 text-[11px] leading-relaxed ${isCancelled ? "text-gray-600" : "text-red-600"}`}>
                      {item.errorMessage || (isCancelled ? "任务在该位置被中断" : "未提供失败原因")}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {!isRunning && (
            <div className="flex items-center justify-between gap-4">
              <span className="text-[12px] text-gray-500">最终结果</span>
              <span className={`text-[13px] font-bold ${resultClassName}`}>
                {resultLabel}
              </span>
            </div>
          )}

          {showForceSend && (
            <div className="flex items-center justify-between gap-4">
              <span className="text-[12px] text-gray-500">可发送截图</span>
              <span className="text-[13px] font-bold text-blue-600">{sendableSuccessCount} 张</span>
            </div>
          )}
        </div>

        {(forceSendMessage || forceSendError) && (
          <div className={`mt-3 text-center text-[12px] font-medium ${forceSendError ? "text-red-600" : "text-emerald-600"}`}>
            {forceSendError || forceSendMessage}
          </div>
        )}

        <div className="flex gap-3 mt-6">
          <button
            onClick={onBackgroundContinue}
            disabled={abortPending || forceSendPending}
            className="flex-1 px-4 py-2.5 rounded-xl border border-gray-200 text-gray-700 font-bold text-[13px] hover:bg-gray-50 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {abortPending ? "正在中断..." : isRunning ? "后台继续" : "确认"}
          </button>
          {showForceSend && (
            <button
              type="button"
              onClick={() => onForceSend?.()}
              disabled={forceSendPending || abortPending}
              className="flex-1 inline-flex items-center justify-center gap-1.5 px-4 py-2.5 rounded-xl bg-blue-600 text-white font-bold text-[13px] hover:bg-blue-700 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {forceSendPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              {forceSendPending ? "发送中..." : "发送"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
