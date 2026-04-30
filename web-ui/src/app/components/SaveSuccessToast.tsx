import { Check } from "lucide-react";
import { createPortal } from "react-dom";

export function SaveSuccessToast({
  visible,
  message = "保存成功",
}: {
  visible: boolean;
  message?: string;
}) {
  if (typeof document === "undefined") {
    return null;
  }

  return createPortal(
    <div
      aria-hidden={!visible}
      aria-live="polite"
      className={`pointer-events-none fixed left-1/2 top-6 -translate-x-1/2 transition-all duration-200 ${
        visible ? "translate-y-0 opacity-100" : "-translate-y-2 opacity-0"
      }`}
      style={{ zIndex: 2147483647 }}
    >
      <div className="min-w-[168px] rounded-xl border border-blue-100 bg-white px-4 py-3 shadow-[0_18px_40px_-24px_rgba(62,121,230,0.5)]">
        <div className="flex items-center justify-center gap-2.5">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-blue-50 text-blue-600">
            <Check className="h-3.5 w-3.5" strokeWidth={3} />
          </span>
          <span className="text-[13px] font-bold tracking-wide text-gray-800">{message}</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
