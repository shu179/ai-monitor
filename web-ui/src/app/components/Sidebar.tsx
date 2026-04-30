import { LayoutDashboard, Hexagon, Bot, Webhook, Settings2, Moon, FileText, UserRound } from "lucide-react";
import { ImageWithFallback } from "./figma/ImageWithFallback";
import surfacedWordmark from "../../assets/surfaced-wordmark.svg";

export function Sidebar({ 
  activeTab, 
  onTabChange, 
  onTabPreload,
  onAccountClick,
  isDarkMode, 
  setIsDarkMode,
  branding,
  sidebar,
  isAuthenticated,
}: { 
  activeTab?: string, 
  onTabChange?: (tab: string) => void,
  onTabPreload?: (tab: string) => void,
  onAccountClick?: () => void,
  isDarkMode?: boolean,
  setIsDarkMode?: (val: boolean) => void,
  branding?: {
    appName?: string;
    brandName?: string;
  },
  sidebar?: {
    userName?: string;
    role?: string;
    avatar?: string;
    online?: boolean;
  },
  isAuthenticated?: boolean,
}) {
  const userName = sidebar?.userName || "林见鹿";
  const userRole = sidebar?.role || "系统运营";
  const avatar = sidebar?.avatar || "";

  return (
    <div className="w-[244px] border-r border-gray-100 h-full flex flex-col bg-[#fcfdff] shrink-0">
      <div className="shrink-0 pl-[35px] pr-8 pt-9 pb-9">
        <img
          src={surfacedWordmark}
          alt="Surfaced"
          className="block h-auto w-[134px] select-none"
          draggable={false}
        />
      </div>
      
      <div className="pb-6 flex-1 overflow-y-auto scrollbar-none">
        <div className="pl-6 pr-4 space-y-1.5">
          <NavItem icon={<LayoutDashboard size={20} strokeWidth={2.5} />} label="看板" active={activeTab === "看板"} onClick={() => onTabChange?.("看板")} onPreload={() => onTabPreload?.("看板")} primary />
          <NavItem icon={<Hexagon size={20} strokeWidth={2.5} />} label="品牌" active={activeTab === "品牌"} onClick={() => onTabChange?.("品牌")} onPreload={() => onTabPreload?.("品牌")} primary />
          <NavItem icon={<FileText size={20} strokeWidth={2.5} />} label="发稿" active={activeTab === "发稿"} onClick={() => onTabChange?.("发稿")} onPreload={() => onTabPreload?.("发稿")} primary trailingBadge={<BetaBadge />} />
          <NavItem icon={<Bot size={20} strokeWidth={2.5} />} label="搜搜" active={activeTab === "搜搜"} onClick={() => onTabChange?.("搜搜")} onPreload={() => onTabPreload?.("搜搜")} rightAction={<Badge label="⌘K" />} primary />
          <NavItem icon={<Webhook size={20} strokeWidth={2.5} />} label="API配置" active={activeTab === "API配置"} onClick={() => onTabChange?.("API配置")} onPreload={() => onTabPreload?.("API配置")} primary />
        </div>
      </div>
      
      <div className="pl-6 pr-4 pb-8 pt-8 border-t border-gray-100/90 flex flex-col gap-2">
        {/* Account Info */}
        <button
          type="button"
          onClick={() => onAccountClick?.()}
          className="flex min-h-[52px] items-center gap-3.5 px-4 py-3 rounded-2xl text-[14px] transition-all duration-200 group text-gray-600 hover:bg-gray-50 hover:text-gray-900 cursor-pointer"
        >
          <div className="w-5 flex items-center justify-center shrink-0">
            {isAuthenticated && avatar ? (
              <div className="w-5 h-5 rounded-full overflow-hidden shadow-sm border border-gray-200/50">
                <ImageWithFallback 
                  src={avatar}
                  alt="Avatar"
                  className="w-full h-full object-cover"
                />
              </div>
            ) : (
              <div className="flex h-5 w-5 items-center justify-center rounded-full border border-gray-200/70 bg-white text-gray-400 shadow-sm">
                <UserRound size={12} strokeWidth={2.4} />
              </div>
            )}
          </div>
          <div className="flex-1 text-left leading-tight">
            <div className="tracking-wide font-medium text-gray-900">{userName}</div>
            <div className="mt-1 text-[10px] text-gray-400">{userRole}</div>
          </div>
        </button>
        
        {/* Dark Mode Toggle */}
        <div className="flex min-h-[52px] items-center justify-between px-4 py-3 rounded-2xl text-[14px] transition-all duration-200 group text-gray-600 hover:bg-gray-50 hover:text-gray-900 cursor-pointer" onClick={() => setIsDarkMode?.(!isDarkMode)}>
          <div className="flex items-center gap-3.5">
            <span className="text-gray-400 group-hover:text-gray-600 transition-colors flex w-5 justify-center shrink-0">
              <Moon size={18} strokeWidth={2.5} />
            </span>
            <span className="font-medium tracking-wide">夜晚模式</span>
          </div>
          <button 
            className={`w-7 h-4 rounded-full p-0.5 transition-colors duration-200 ease-in-out flex items-center ${
              isDarkMode ? "bg-gray-900" : "bg-gray-200"
            }`}
          >
            <div className={`w-3 h-3 rounded-full bg-white shadow-sm transform transition-transform duration-200 ease-in-out ${
              isDarkMode ? "translate-x-3" : "translate-x-0"
            }`} />
          </button>
        </div>

        {/* System Settings */}
        <NavItem icon={<Settings2 size={19} strokeWidth={2.5} />} label="系统设置" active={activeTab === "系统设置"} onClick={() => onTabChange?.("系统设置")} onPreload={() => onTabPreload?.("系统设置")} />
      </div>
    </div>
  );
}

function NavItem({ 
  icon, 
  label, 
  active = false, 
  rightAction, 
  trailingBadge,
  primary = false,
  className,
  onClick,
  onPreload
}: { 
  icon: React.ReactNode; 
  label: string; 
  active?: boolean; 
  rightAction?: React.ReactNode;
  trailingBadge?: React.ReactNode;
  primary?: boolean;
  className?: string;
  onClick?: () => void;
  onPreload?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      onPointerEnter={onPreload}
      onFocus={onPreload}
      className={`w-full flex items-center ${primary ? "gap-4 px-4 py-[13px] text-[15px] min-h-[48px]" : "gap-4 px-4 py-[13px] text-[14px] min-h-[50px]"} rounded-[14px] transition-all duration-200 group ${
        active 
          ? "bg-[#f5f7fb] text-gray-900 font-bold" 
          : className || "text-gray-500 hover:bg-[#f5f7fb] hover:text-gray-900 font-medium"
      }`}
    >
      <span className={`flex shrink-0 items-center justify-center w-5 ${active ? "text-blue-600" : "text-gray-400 group-hover:text-gray-500 transition-colors"}`}>
        {icon}
      </span>
      <span className="min-w-0 flex-1 text-left tracking-wide">
        <span>{label}</span>
      </span>
      {trailingBadge && <span className="ml-2 shrink-0">{trailingBadge}</span>}
      {rightAction && <span className="ml-2 shrink-0">{rightAction}</span>}
    </button>
  );
}

function Badge({ label }: { label: string }) {
  return (
    <div className="px-1.5 py-0.5 rounded text-[10px] bg-white text-gray-400 uppercase tracking-widest border border-gray-100">
      {label}
    </div>
  );
}

function BetaBadge() {
  return (
    <span className="relative -top-[2px] inline-flex min-h-[16px] min-w-[30px] items-center justify-center rounded-[8px] border border-black/90 bg-[#111111] px-1.5 py-0 text-[8px] leading-none font-semibold uppercase tracking-[0.1em] text-white shadow-[0_8px_16px_-16px_rgba(15,23,42,0.38)]">
      Beta
    </span>
  );
}
