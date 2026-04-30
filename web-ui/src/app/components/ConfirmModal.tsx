import { AlertTriangle, Info, CheckCircle2, X } from "lucide-react";

interface ConfirmModalProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  type?: "danger" | "warning" | "info" | "primary";
}

export function ConfirmModal({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmText = "确认",
  cancelText = "取消",
  type = "primary",
}: ConfirmModalProps) {
  if (!isOpen) return null;

  const typeConfig = {
    danger: {
      icon: <AlertTriangle className="w-5 h-5 text-red-600" />,
      iconBg: "bg-red-50 border-red-100",
      btnBg: "bg-red-600 hover:bg-red-700 shadow-red-600/20",
    },
    warning: {
      icon: <AlertTriangle className="w-5 h-5 text-orange-600" />,
      iconBg: "bg-orange-50 border-orange-100",
      btnBg: "bg-orange-600 hover:bg-orange-700 shadow-orange-600/20",
    },
    info: {
      icon: <Info className="w-5 h-5 text-blue-600" />,
      iconBg: "bg-blue-50 border-blue-100",
      btnBg: "bg-blue-600 hover:bg-blue-700 shadow-blue-600/20",
    },
    primary: {
      icon: <CheckCircle2 className="w-5 h-5 text-blue-600" />,
      iconBg: "bg-blue-50 border-blue-100",
      btnBg: "bg-blue-600 hover:bg-blue-700 shadow-blue-600/20",
    },
  };

  const { icon, iconBg, btnBg } = typeConfig[type];

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center">
      {/* Backdrop */}
      <div 
        className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-200"
        onClick={onClose}
      />
      
      {/* Modal */}
      <div className="relative bg-white rounded-2xl shadow-xl border border-gray-200 w-full max-w-[400px] p-6 animate-in zoom-in-95 duration-200 mx-4">
        <button 
          onClick={onClose}
          className="absolute top-4 right-4 p-1.5 text-gray-400 hover:text-gray-900 hover:bg-gray-100 rounded-lg transition-colors"
        >
          <X className="w-4 h-4" />
        </button>

        <div className="flex flex-col items-center text-center gap-4">
          <div className={`w-12 h-12 rounded-full flex items-center justify-center border shadow-sm ${iconBg}`}>
            {icon}
          </div>
          
          <div className="flex flex-col gap-1.5">
            <h3 className="text-[16px] font-bold text-gray-900 tracking-tight">{title}</h3>
            <p className="text-[13px] text-gray-500 leading-relaxed max-w-[280px]">
              {message}
            </p>
          </div>
        </div>

        <div className="flex gap-3 mt-8">
          <button 
            onClick={onClose}
            className="flex-1 px-4 py-2.5 rounded-xl border border-gray-200 text-gray-700 font-bold text-[13px] hover:bg-gray-50 transition-colors"
          >
            {cancelText}
          </button>
          <button 
            onClick={() => {
              onConfirm();
              onClose();
            }}
            className={`flex-1 px-4 py-2.5 rounded-xl text-white font-bold text-[13px] transition-colors shadow-sm ${btnBg}`}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}