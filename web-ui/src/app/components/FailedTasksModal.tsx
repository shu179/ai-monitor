import { useState } from "react";
import { AlertTriangle, Search, Send, X } from "lucide-react";
import { TASK_DATA_CHANGED_EVENT, forceSendSuccessfulTaskResults, type DashboardSnapshot } from "../lib/backend";

type FailedTaskItem = NonNullable<DashboardSnapshot["failedTasks"]>[number];

interface FailedTasksModalProps {
  isOpen: boolean;
  onClose: () => void;
  failedTasks: FailedTaskItem[];
  onActionComplete?: () => Promise<void> | void;
}

export function FailedTasksModal({ isOpen, onClose, failedTasks, onActionComplete }: FailedTasksModalProps) {
  if (!isOpen) return null;

  const notificationIssueCount = failedTasks.filter((task) => task.issueType === "notification").length;
  const queryIssueCount = failedTasks.length - notificationIssueCount;

  return (
    <div className="fixed inset-0 z-[110] flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/20 backdrop-blur-[2px] animate-in fade-in duration-200"
        onClick={onClose}
      />

      <div className="relative mx-4 flex h-full max-h-[720px] w-full max-w-[880px] flex-col overflow-hidden rounded-2xl border border-gray-200/60 bg-white shadow-[0_20px_60px_-15px_rgba(0,0,0,0.1)] animate-in slide-in-from-bottom-4 duration-300">
        <button
          onClick={onClose}
          className="absolute right-5 top-4 z-10 inline-flex h-9 w-9 items-center justify-center rounded-full text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-900"
        >
          <X className="w-5 h-5" />
        </button>

        <div className="shrink-0 border-b border-gray-100 px-6 py-4.5">
          <div className="flex items-center gap-4 pr-10">
            <div className="flex min-w-0 flex-col gap-1">
              <div className="flex items-center gap-2">
                <div className="h-4 w-2 rounded-sm bg-blue-600" />
                <h2 className="text-[18px] font-black tracking-tight leading-none text-gray-900">今日检测任务异常概览</h2>
                <span className="text-[18px] font-bold leading-none text-gray-300">/</span>
                <span className="mt-0.5 text-[14px] font-bold uppercase tracking-widest text-gray-500">任务状态</span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] font-medium text-gray-500">
                <span>当前仍有</span>
                <span className="font-mono text-[14px] font-bold text-gray-900">{failedTasks.length}</span>
                <span>个异常任务</span>
                <span className="text-gray-300">•</span>
                <span>查询失败 {queryIssueCount}</span>
                <span className="text-gray-300">•</span>
                <span>待补发 {notificationIssueCount}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="shrink-0 border-b border-gray-100 bg-gray-50/35 px-6 py-2.5">
          <div className="mx-auto flex max-w-[820px] items-center justify-between gap-4 text-[11px] text-gray-500">
            <p>任务恢复成功后会自动从列表移除。发送失败但素材已成功获取的任务，可以直接补发。</p>
            <span className="shrink-0 font-medium text-gray-400">仅展示今日异常</span>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto bg-gray-50/30 px-6 py-3 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent">
          {failedTasks.length > 0 ? (
            <div className="mx-auto flex max-w-[820px] flex-col overflow-hidden rounded-[18px] border border-gray-200/80 bg-white">
              {failedTasks.map((task) => (
                <FailedTaskCard key={task.taskId} task={task} onActionComplete={onActionComplete} />
              ))}
            </div>
          ) : (
            <div className="flex h-full min-h-[220px] flex-col items-center justify-center gap-3 text-gray-400">
              <div className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-gray-200 bg-gray-50">
                <AlertTriangle className="h-4.5 w-4.5 opacity-30" />
              </div>
              <div className="text-center">
                <p className="text-[12px] font-medium text-gray-600">当前没有仍处于失败态的检测任务</p>
                <p className="mt-1 text-[11px] text-gray-400">系统看板中的异常任务已经全部恢复。</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function FailedTaskCard({
  task,
  onActionComplete,
}: {
  task: FailedTaskItem;
  onActionComplete?: () => Promise<void> | void;
}) {
  const failedCount = task.failedQueries?.length || 0;
  const isNotificationIssue = task.issueType === "notification";
  const [sending, setSending] = useState(false);
  const [actionMessage, setActionMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const badgeClassName = isNotificationIssue
    ? "border-amber-200/80 bg-amber-50/80 text-amber-700"
    : "border-red-200/80 bg-red-50/80 text-red-600";
  const badgeLabel = isNotificationIssue ? "待补发" : "任务失败";
  const countLabel = isNotificationIssue ? "待补发状态" : `${failedCount} 条失败查询`;
  const sendableSuccessCount = Math.max(0, Number(task.sendableSuccessCount || 0));
  const updatedAtText = task.failedUpdatedAt ? task.failedUpdatedAt.replace("T", " ") : "刚刚更新";

  const handleForceSend = async () => {
    if (sending) return;
    setSending(true);
    setActionMessage("");
    setActionError("");
    try {
      const result = await forceSendSuccessfulTaskResults(task.taskId);
      if (!result.ok) {
        setActionError(result.message || "发送失败");
        return;
      }
      setActionMessage(result.message || `已发送 ${result.actualScreenshotCount || sendableSuccessCount} 张成功截图`);
      window.dispatchEvent(new CustomEvent(TASK_DATA_CHANGED_EVENT));
      await onActionComplete?.();
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="w-full border-b border-gray-200/80 px-5 py-4 last:border-b-0">
      <div className="flex flex-col gap-3">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[15px] font-bold tracking-tight text-gray-900">{task.brand || task.taskName}</span>
              <span className={`rounded-full border px-2 py-0.5 text-[9px] font-bold tracking-wider ${badgeClassName}`}>
                {badgeLabel}
              </span>
              {task.failedModes?.map((mode) => (
                <span key={mode} className="rounded-full border border-gray-200/80 bg-gray-50 px-2 py-0.5 text-[9px] font-bold tracking-wider text-gray-500">
                  {mode}
                </span>
              ))}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-medium text-gray-500">
              <span>任务名称 {task.taskName}</span>
              <span className="text-gray-300">•</span>
              <span>最近更新 {updatedAtText}</span>
              <span className="text-gray-300">•</span>
              <span className={isNotificationIssue ? "text-amber-700" : "text-red-600"}>{countLabel}</span>
            </div>
          </div>
          <div className="shrink-0 text-right">
            <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">
              {isNotificationIssue ? "待补发状态" : "失败查询"}
            </div>
            <div className="mt-1 text-[20px] font-bold leading-none text-gray-900">
              {isNotificationIssue ? 1 : failedCount}
            </div>
          </div>
        </div>

        {task.canForceSendSuccess && (
          <div className="flex flex-col gap-2 border-t border-gray-100 pt-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="text-[11px] font-medium text-gray-600">
                已成功拿到 {sendableSuccessCount} 张截图，可直接发送并改判为成功。
              </p>
              {actionMessage && <p className="mt-1 text-[11px] font-medium text-emerald-600">{actionMessage}</p>}
              {actionError && <p className="mt-1 text-[11px] font-medium text-red-600">{actionError}</p>}
            </div>
            <button
              type="button"
              onClick={() => void handleForceSend()}
              disabled={sending}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-[12px] border border-gray-200/80 bg-white px-3 py-2 text-[11px] font-bold text-gray-700 transition-all hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Send className="w-3.5 h-3.5" />
              {sending ? "发送中..." : "直接发送"}
            </button>
          </div>
        )}

        {(task.issueTitle || task.issueDescription || task.statusMessage) && (
          <div className="text-[11px] leading-6 text-gray-500">
            <span className="mr-2 font-bold uppercase tracking-widest text-gray-400">异常说明</span>
            <span className={isNotificationIssue ? "text-amber-700" : "text-gray-600"}>
              {task.issueTitle || task.statusMessage}
              {task.issueDescription && task.issueDescription !== task.issueTitle ? ` · ${task.issueDescription}` : ""}
            </span>
          </div>
        )}

        {failedCount > 0 ? (
          <div className="border-t border-gray-100 pt-3">
            <div className="mb-2 flex items-center justify-between gap-3">
              <p className="text-[10px] font-bold tracking-widest text-gray-400 uppercase">失败明细</p>
              {sendableSuccessCount > 0 && (
                <span className="text-[10px] font-semibold text-gray-400">可补发 {sendableSuccessCount} 张</span>
              )}
            </div>
            <div className="flex flex-col">
              {task.failedQueries.map((item, index) => (
                <div
                  key={`${task.taskId}-${index}-${item.keyword}-${item.platform}-${item.mode}`}
                  className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-t border-gray-100 py-2.5 first:border-t-0"
                >
                  <div className="min-w-0">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-gray-900">
                        <Search className="w-3.5 h-3.5 text-gray-400" />
                        {item.keyword || "未定位到关键词"}
                      </span>
                      <span className="inline-flex rounded-full border border-gray-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-gray-700">
                        {item.platform || "未记录平台"}
                      </span>
                      <span className="rounded-full border border-gray-200 bg-white px-2 py-0.5 text-[9px] font-bold tracking-wider text-gray-500">
                        {item.mode || "未知模式"}
                      </span>
                    </div>
                    <p className="mt-1 text-[11px] leading-6 text-gray-500">
                      <span className="font-medium text-red-600">错误：</span>
                      {item.error_message}
                    </p>
                  </div>
                  <span className="shrink-0 text-[10px] font-mono text-gray-400">{item.ts || ""}</span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="border-t border-gray-100 pt-3">
            <p className="text-[11px] leading-relaxed text-gray-500">
              {isNotificationIssue
                ? "当前任务查询素材已补齐，但企业微信发送未成功。系统会在后续自动运行时继续尝试补发，发送成功后会自动从这里消失。"
                : "当前没有更细的失败查询明细，可能是任务在发送或调度阶段失败。"}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
