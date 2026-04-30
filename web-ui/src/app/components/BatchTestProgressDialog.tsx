import { useEffect, useState } from "react";
import { X, Loader2, XCircle, CheckCircle2 } from "lucide-react";
import { apiFetch } from "../lib/backend";

interface BatchTestProgressDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  batchId: string;
  onComplete: () => void;
}

interface ProgressData {
  status: string;
  total_queries: number;
  completed_queries: number;
  current_keyword: string;
  current_platform: string;
  status_text: string;
}

export function BatchTestProgressDialog({
  open,
  onOpenChange,
  batchId,
  onComplete,
}: BatchTestProgressDialogProps) {
  const [progress, setProgress] = useState<ProgressData>({
    status: "pending",
    total_queries: 0,
    completed_queries: 0,
    current_keyword: "",
    current_platform: "",
    status_text: "准备中...",
  });
  const [isCancelling, setIsCancelling] = useState(false);

  useEffect(() => {
    if (!open || !batchId) return;

    const pollProgress = async () => {
      try {
        const response = await apiFetch(`/api/batch-test/${batchId}/progress`);
        const data = await response.json();

        if (data.ok) {
          setProgress(data.progress);

          if (data.progress.status === "completed") {
            setTimeout(() => {
              onComplete();
              onOpenChange(false);
            }, 1000);
          } else if (data.progress.status === "failed" || data.progress.status === "cancelled") {
            setTimeout(() => {
              onOpenChange(false);
            }, 2000);
          }
        }
      } catch (error) {
        console.error("获取进度失败:", error);
      }
    };

    pollProgress();
    const interval = setInterval(pollProgress, 1000);

    return () => clearInterval(interval);
  }, [open, batchId, onComplete, onOpenChange]);

  const handleCancel = async () => {
    if (isCancelling) return;

    setIsCancelling(true);
    try {
      await apiFetch(`/api/batch-test/${batchId}/cancel`, { method: "POST" });
    } catch (error) {
      console.error("取消失败:", error);
    } finally {
      setIsCancelling(false);
    }
  };

  const progressPercent =
    progress.total_queries > 0
      ? (progress.completed_queries / progress.total_queries) * 100
      : 0;

  const isRunning = progress.status === "running" || progress.status === "pending";
  const isFailed = progress.status === "failed";
  const isCancelled = progress.status === "cancelled";
  const isCompleted = progress.status === "completed";

  const icon = isRunning ? (
    <Loader2 className="w-5 h-5 text-blue-600 animate-spin" />
  ) : isCompleted ? (
    <CheckCircle2 className="w-5 h-5 text-blue-600" />
  ) : isCancelled ? (
    <XCircle className="w-5 h-5 text-gray-500" />
  ) : (
    <XCircle className="w-5 h-5 text-red-600" />
  );

  const iconBg = isRunning || isCompleted
    ? "bg-blue-50 border-blue-100"
    : isCancelled
      ? "bg-gray-50 border-gray-200"
      : "bg-red-50 border-red-100";

  const progressClassName = isCompleted
    ? "bg-blue-600"
    : isCancelled
      ? "bg-gray-400"
      : isFailed
        ? "bg-red-500"
        : "bg-blue-500";

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[110] flex items-center justify-center">
      <div
        className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-200"
        onClick={() => !isRunning && onOpenChange(false)}
      />

      <div className="relative bg-white rounded-2xl shadow-xl border border-gray-200 w-full max-w-[420px] p-6 animate-in zoom-in-95 duration-200 mx-4">
        <button
          onClick={() => (isRunning ? handleCancel() : onOpenChange(false))}
          disabled={isCancelling}
          className="absolute top-4 right-4 p-1.5 text-gray-400 hover:text-gray-900 hover:bg-gray-100 rounded-lg transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
        >
          <X className="w-4 h-4" />
        </button>

        <div className="flex flex-col items-center text-center gap-4">
          <div className={`w-12 h-12 rounded-full flex items-center justify-center border shadow-sm ${iconBg}`}>
            {icon}
          </div>

          <div className="flex flex-col gap-1.5">
            <h3 className="text-[16px] font-bold text-gray-900 tracking-tight">批量测试进度</h3>
            <p className="text-[13px] text-gray-500 leading-relaxed max-w-[300px]">
              {isRunning ? "正在执行批量测试..." : isCompleted ? "测试已完成" : isCancelled ? "测试已取消" : "测试失败"}
            </p>
          </div>
        </div>

        <div className="mt-6 rounded-2xl border border-gray-200 bg-gray-50/70 p-4 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">当前进度</span>
            <span className="text-[13px] font-bold text-gray-900">
              {progress.completed_queries} / {progress.total_queries}
            </span>
          </div>

          <div className="h-2 rounded-full bg-white border border-gray-200 overflow-hidden">
            <div
              className={`h-full transition-all duration-300 ${progressClassName}`}
              style={{ width: `${progressPercent}%` }}
            />
          </div>

          <div className="flex items-center justify-between gap-4">
            <span className="text-[12px] text-gray-500">当前状态</span>
            <span className={`text-[12px] font-bold ${isCompleted ? "text-blue-600" : isCancelled ? "text-gray-600" : isFailed ? "text-red-600" : "text-blue-600"}`}>
              {progress.status_text}
            </span>
          </div>

          {isRunning && progress.current_keyword && (
            <div className="text-left rounded-xl bg-white border border-gray-200 px-3 py-2.5">
              <p className="text-[10px] font-bold text-gray-400 tracking-[0.12em] uppercase">当前执行</p>
              <p className="text-[11px] text-gray-500 leading-relaxed">
                {progress.current_platform} · {progress.current_keyword}
              </p>
            </div>
          )}
        </div>

        <div className="flex gap-3 mt-6">
          {isRunning ? (
            <button
              onClick={handleCancel}
              disabled={isCancelling}
              className="flex-1 px-4 py-2.5 rounded-xl border border-red-200 text-red-600 font-bold text-[13px] hover:bg-red-50 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {isCancelling ? "取消中..." : "取消测试"}
            </button>
          ) : (
            <button
              onClick={() => onOpenChange(false)}
              className="flex-1 px-4 py-2.5 rounded-xl border border-gray-200 text-gray-700 font-bold text-[13px] hover:bg-gray-50 transition-colors"
            >
              关闭
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
