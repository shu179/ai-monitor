import { useEffect, useMemo, useRef, useState } from "react";
import { Zap, Eye, ArrowUpRight, Map, Globe } from "lucide-react";
import { ChartArea } from "./Charts";
import { OcrFloatingWindow } from "./OcrFloatingWindow";
import { FailedTasksModal } from "./FailedTasksModal";
import type { DashboardSnapshot, TrendSnapshot } from "../lib/backend";
import { fetchDashboardTrend, fetchRecognitionStatus, recognitionAction, saveSettings } from "../lib/backend";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, ResponsiveContainer, Tooltip as RechartsTooltip,
  PieChart, Pie, Cell
} from 'recharts';

function formatHeaderDate(now: Date): string {
  return new Intl.DateTimeFormat("en", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(now).toUpperCase();
}

function buildCompactCalendar(now: Date) {
  const items = [];
  for (let offset = -3; offset <= 1; offset += 1) {
    const date = new Date(now);
    date.setDate(now.getDate() + offset);
    items.push({
      key: date.toISOString().slice(0, 10),
      date: date.getDate(),
      day: ["日", "一", "二", "三", "四", "五", "六"][date.getDay()],
      active: offset === 0,
    });
  }
  return items;
}

const _MODE_TITLE_TO_KEY: Record<string, string> = {
  "抓取模式": "browser",
  "识别模式": "recognition",
};

const DASHBOARD_MODE_CARD_DEFS: DashboardSnapshot["taskCards"] = [
  { key: "capture", title: "抓取模式", desc: "快速提取核心数据", icon: "zap", active: true },
  { key: "ocr", title: "识别模式", desc: "OCR视觉解析", icon: "eye", active: false },
];

function normalizeDashboardModeCardKey(card: Partial<DashboardSnapshot["taskCards"][number]> | null | undefined) {
  const key = String(card?.key || "").trim().toLowerCase();
  const title = String(card?.title || "").trim();
  if (key === "capture" || key === "browser" || title === "抓取模式") {
    return "capture";
  }
  if (key === "ocr" || key === "recognition" || title === "识别模式") {
    return "ocr";
  }
  return "";
}

function sanitizeDashboardModeCards(cards: DashboardSnapshot["taskCards"] | undefined) {
  const activeKey = (cards || [])
    .map((card) => ({ key: normalizeDashboardModeCardKey(card), active: Boolean(card?.active) }))
    .find((item) => item.key && item.active)?.key || "capture";
  return DASHBOARD_MODE_CARD_DEFS.map((card) => ({
    ...card,
    active: card.key === activeKey,
  }));
}

const DASHBOARD_DEEP = "#2F5A67";
const DASHBOARD_TEAL = "#1E7F95";
const DASHBOARD_CYAN = "#2FB8E6";
const DASHBOARD_CYAN_LIGHT = "#74D2EE";
const DASHBOARD_CYAN_SOFT = "#CBEFF9";
const MEDIA_STAT_WEEKDAY_LABELS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const FALLBACK_MEDIA_STATS = [
  { name: '1月', auth: 400, self: 240 },
  { name: '2月', auth: 300, self: 139 },
  { name: '3月', auth: 200, self: 980 },
  { name: '4月', auth: 278, self: 390 },
  { name: '5月', auth: 189, self: 480 },
  { name: '6月', auth: 239, self: 380 },
];

function parseMediaStatDate(dateText?: string) {
  const [yearText, monthText, dayText] = String(dateText || "").split("-");
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) {
    return null;
  }
  return new Date(year, month - 1, day);
}

function formatMediaStatAxisLabel(
  item: DashboardSnapshot["mediaStats"][number],
  index: number,
  total: number,
) {
  const parsedDate = parseMediaStatDate(item.date);
  if (!parsedDate) {
    return item.label || item.name;
  }

  const position = index + 1;
  const lastWeekStart = Math.max(total - 7, 7);
  if (position <= 7) {
    return `${parsedDate.getDate()}日`;
  }
  if (position > lastWeekStart) {
    return `${parsedDate.getMonth() + 1}/${parsedDate.getDate()}`;
  }
  return MEDIA_STAT_WEEKDAY_LABELS[parsedDate.getDay()] || item.name;
}

const INDUSTRY_CHART_COLORS = [
  DASHBOARD_DEEP,
  DASHBOARD_TEAL,
  "#3599B0",
  "#5CAFC0",
  DASHBOARD_CYAN,
  DASHBOARD_CYAN_LIGHT,
  "#A9E2F2",
  DASHBOARD_CYAN_SOFT,
  "rgba(47, 90, 103, 0.84)",
  "rgba(47, 184, 230, 0.9)",
  "rgba(47, 184, 230, 0.66)",
  "rgba(203, 239, 249, 0.92)",
];

export function CenterContent({
  localOCR = true,
  dashboard,
  runMessage,
  activeRegions,
  onDataChanged,
  suppressOcrWindow = false,
  onRunMessage,
}: {
  localOCR?: boolean;
  dashboard?: DashboardSnapshot;
  runMessage?: string;
  activeRegions?: string[];
  onDataChanged?: () => Promise<void> | void;
  suppressOcrWindow?: boolean;
  onRunMessage?: (message: string, durationMs?: number) => void;
}) {
  const now = new Date();
  const compactCalendar = buildCompactCalendar(now);

  // 从后端 taskCards 中找到 active 的卡片作为初始模式
  const initialModeCards = sanitizeDashboardModeCards(dashboard?.taskCards);
  const initialMode = initialModeCards.find(c => c.active)?.title || '抓取模式';
  const [activeMode, setActiveMode] = useState<string>(initialMode);
  const [showOcrWindow, setShowOcrWindow] = useState(false);
  const headerDate = dashboard?.dateLabel || formatHeaderDate(now);
  const greeting = dashboard?.greeting || "下午好";
  const userName = dashboard?.userName || "Saffron";
  const headline = dashboard?.headline || "系统运行平稳，今日已为您自动拦截 12 项异常请求。";
  const todayTaskCount = dashboard?.todayTaskCount ?? 42;
  const completedCount = dashboard?.completedCount ?? 38;
  const runningCount = dashboard?.runningCount ?? 4;
  const sourceBreakdown = dashboard?.sourceBreakdown || [];
  const modeCards = useMemo(() => sanitizeDashboardModeCards(dashboard?.taskCards), [dashboard?.taskCards]);
  const mediaStats = dashboard?.mediaStats || [];
  const failedTasks = dashboard?.failedTasks || [];
  const failedTaskCount = dashboard?.failedTaskCount ?? failedTasks.length;
  const [showFailedTasksModal, setShowFailedTasksModal] = useState(false);

  useEffect(() => {
    const nextMode = modeCards.find(c => c.active)?.title;
    if (nextMode) {
      setActiveMode(nextMode);
    }
  }, [modeCards]);

  const handleModeSelect = async (mode: { key: string; title: string; desc: string; icon: "zap" | "eye"; active: boolean }) => {
    setActiveMode(mode.title);
    const modeKey = _MODE_TITLE_TO_KEY[mode.title] || mode.key;
    await saveSettings({ detection_mode: modeKey });
    if (modeKey === "recognition") {
      await recognitionAction("start");
      const recognitionStatus = await fetchRecognitionStatus();
      const guide = recognitionStatus.status?.keyword_guide as { items?: unknown[] } | undefined;
      const overview = recognitionStatus.status?.task_overview as
        | {
            configured_task_count?: number;
            watchable_today_count?: number;
            completed_today_count?: number;
            available_task_count?: number;
          }
        | undefined;
      const hasRecognitionTasks = Array.isArray(guide?.items) && guide.items.length > 0;
      setShowOcrWindow(hasRecognitionTasks);
      if (!hasRecognitionTasks) {
        const watchableTodayCount = Number(overview?.watchable_today_count || 0);
        const completedTodayCount = Number(overview?.completed_today_count || 0);
        const configuredTaskCount = Number(overview?.configured_task_count || 0);
        const availableTaskCount = Number(overview?.available_task_count || 0);
        const message = availableTaskCount > 0 || failedTaskCount > 0
          ? `今日仍有 ${Math.max(availableTaskCount, failedTaskCount, 1)} 个任务待补齐，请继续补跑失败关键词或平台`
          : watchableTodayCount > 0 && completedTodayCount >= watchableTodayCount
            ? "今日任务已全部补齐，可前往品牌页开启测试"
            : configuredTaskCount > 0 && availableTaskCount <= 0
            ? "当前暂无可执行任务，请前往品牌页检查配置"
            : "当前暂无任务，可进入品牌页开启测试任务";
        onRunMessage?.(message);
      }
    } else {
      await recognitionAction("stop");
      setShowOcrWindow(false);
    }
  };

  return (
    <div className="flex-1 min-w-0 min-h-0 overflow-x-hidden overflow-y-auto bg-transparent px-6 py-4 xl:px-8 xl:py-5 flex flex-col custom-scrollbar">
      {/* 1. Header & Calendar */}
      <div className="flex flex-col gap-3 border-b border-gray-200/70 pb-4 mb-4 shrink-0 xl:flex-row xl:items-center xl:justify-between">
        <div className="flex flex-1 flex-col gap-1">
          <p className="text-[10px] text-slate-500 font-semibold tracking-[0.18em] uppercase mb-1">{headerDate}</p>
          <h1 className="app-display-heading text-[26px] font-semibold text-[#0f1835] tracking-[-0.035em]">
            {greeting}，{userName}。
          </h1>
          <p className="app-subtle-copy text-[13px] mt-1 max-w-[620px] leading-6 font-medium">
            {headline}
          </p>
        </div>

        <div className="flex flex-col gap-2 xl:items-end xl:pl-4">
          {runMessage && (
            <div className="hidden shrink-0 md:flex items-start gap-2 text-right">
              <span className="mt-[6px] h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: DASHBOARD_CYAN }} />
              <p className="whitespace-nowrap text-[12px] leading-5 font-medium text-slate-500">
                {runMessage}
              </p>
            </div>
          )}
          
          {/* Minimalist Calendar */}
          <div className="flex gap-4 pr-1 xl:translate-y-[-2px]">
            {compactCalendar.map((d) => (
              <div key={d.key} className="flex flex-col items-center justify-center relative min-w-[20px]">
                <span className={`text-[11px] mb-1.5 ${d.active ? 'font-bold text-gray-900' : 'font-medium text-gray-400'}`}>{d.day}</span>
                <span className={`text-[16px] ${d.active ? 'font-bold text-gray-900' : 'font-semibold text-gray-700'}`}>{d.date}</span>
                {d.active && <div className="absolute -bottom-2 w-1 h-1 rounded-full" style={{ backgroundColor: DASHBOARD_CYAN }}></div>}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="flex flex-col gap-3 min-h-max">
        {/* Row 1: Tasks & Modes */}
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-6 shrink-0">
          {/* Today's Tasks */}
          <div className="xl:col-span-4 flex flex-col justify-start">
            <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase mb-3">今日检测任务</h3>
            <div className="flex items-baseline gap-2 mb-3">
              <span className="text-[52px] font-medium text-gray-900 leading-none tracking-tighter">{todayTaskCount}</span>
              <span className="text-[13px] text-gray-500 font-medium">项</span>
            </div>
            <div className="flex gap-6 border-t border-gray-200/70 pt-3">
              <div className="flex flex-col gap-0.5">
                <span className="text-[11px] text-gray-400 font-medium">已完成</span>
                <span className="text-[18px] text-gray-900 font-bold">{completedCount}</span>
              </div>
              <div className="flex flex-col gap-0.5">
                <span className="text-[11px] text-gray-400 font-medium">进行中</span>
                <span className="text-[18px] text-gray-900 font-bold">{runningCount}</span>
              </div>
              <button
                type="button"
                onClick={() => setShowFailedTasksModal(true)}
                className="group flex flex-col gap-0.5 text-left shrink-0 transition-opacity hover:opacity-80"
              >
                <span className="text-[11px] text-gray-400 font-medium whitespace-nowrap">任务状态</span>
                <span className={`text-[18px] font-bold whitespace-nowrap ${failedTaskCount > 0 ? "text-red-600" : "text-gray-900"}`}>
                  {failedTaskCount > 0 ? "失败明细" : "运行正常"}
                </span>
              </button>
            </div>
          </div>

          {/* Detection Modes */}
          <div className="xl:col-span-8 flex flex-col">
            <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase mb-3">检测模式</h3>
            <div className="grid grid-cols-2 gap-4 h-full">
              {modeCards.map((mode) => (
                <ModeItem
                  key={mode.key}
                  onClick={() => { void handleModeSelect(mode); }}
                  icon={
                    mode.icon === "zap" ? <Zap className="w-3.5 h-3.5" /> :
                    <Eye className="w-3.5 h-3.5" />
                  }
                  title={mode.title}
                  desc={mode.desc}
                  active={activeMode === mode.title}
                />
              ))}
            </div>
          </div>
        </div>

        {/* Row 2: Trend & Pie Chart */}
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-5 border-t border-gray-200/70 pt-4 shrink-0 items-start">
          {/* AI Trend */}
          <AITrendSection initialTrend={dashboard?.trend} />

          {/* Industry Pie */}
          <div className="xl:col-span-4 flex flex-col">
            <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase mb-3">品牌行业来源</h3>
            <div className="w-full relative">
              <IndustryPieChart data={sourceBreakdown} />
            </div>
          </div>
        </div>

        {/* Row 3: Bar & Map */}
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-5 border-t border-gray-200/70 pt-3 shrink-0 items-start">
          {/* Media Bar */}
          <div className="xl:col-span-6 flex flex-col">
            <MediaBarChart data={mediaStats} />
          </div>

          {/* Region Map */}
          <div className="xl:col-span-6 flex flex-col">
            <OptimizationMap activeRegions={activeRegions} />
          </div>
        </div>
      </div>
      
      {showOcrWindow && !suppressOcrWindow && <OcrFloatingWindow onClose={() => setShowOcrWindow(false)} localOCR={localOCR} />}
      <FailedTasksModal
        isOpen={showFailedTasksModal}
        onClose={() => setShowFailedTasksModal(false)}
        failedTasks={failedTasks}
        onActionComplete={onDataChanged}
      />
    </div>
  );
}

// Subcomponents

function AITrendSection({
  initialTrend,
}: {
  initialTrend?: TrendSnapshot;
}) {
  const [timeRange, setTimeRange] = useState<'week' | 'month' | 'year'>('week');
  const [trend, setTrend] = useState<TrendSnapshot>(initialTrend || {
    timeRange: 'week',
    current: 0,
    avg: 0,
    peak: 0,
    delta: 0,
    data: [],
  });

  useEffect(() => {
    if (initialTrend) {
      setTrend(initialTrend);
      setTimeRange(initialTrend.timeRange || 'week');
    }
  }, [initialTrend]);

  useEffect(() => {
    let cancelled = false;
    void fetchDashboardTrend(timeRange).then((data) => {
      if (!cancelled) {
        setTrend(data);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [timeRange]);

  const currentData = trend.data || [];
  const current = trend.current ?? trend.avg ?? 0;
  const chartPeak = trend.peak ?? 0;
  const chartAvg = trend.avg ?? 0;
  const chartDelta = trend.delta ?? 0;
  const chartDeltaText = `${chartDelta > 0 ? '+' : ''}${chartDelta}%`;
  const deltaTone = chartDelta > 0 ? 'text-[#16a34a]' : chartDelta < 0 ? 'text-[#dc2626]' : 'text-gray-400';
  const deltaIconClass = chartDelta < 0 ? 'rotate-90' : chartDelta === 0 ? 'rotate-45' : '';

  return (
    <div className="xl:col-span-8 flex flex-col min-h-0">
      <div className="flex items-start justify-between mb-3">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-4">
            <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">AI 辅助优化趋势</h3>
            <div className="flex items-center gap-2">
              <button 
                onClick={() => setTimeRange('week')}
                className={`text-[10px] font-bold transition-colors ${timeRange === 'week' ? 'text-gray-900' : 'text-gray-400 hover:text-gray-700'}`}
              >
                周
              </button>
              <span className="w-px h-2.5 bg-gray-200"></span>
              <button 
                onClick={() => setTimeRange('month')}
                className={`text-[10px] font-bold transition-colors ${timeRange === 'month' ? 'text-gray-900' : 'text-gray-400 hover:text-gray-700'}`}
              >
                月
              </button>
              <span className="w-px h-2.5 bg-gray-200"></span>
              <button 
                onClick={() => setTimeRange('year')}
                className={`text-[10px] font-bold transition-colors ${timeRange === 'year' ? 'text-gray-900' : 'text-gray-400 hover:text-gray-700'}`}
              >
                年
              </button>
            </div>
          </div>
          <div className="flex items-end gap-4 mt-1">
            <div className="flex items-center gap-2">
              <span className="text-[24px] leading-none text-gray-900 font-medium">{current}<span className="text-[14px] text-gray-500">%</span></span>
              <span className={`${deltaTone} text-[11px] font-bold flex items-center gap-0.5 mb-0.5`}>
                <ArrowUpRight className={`w-3 h-3 transition-transform ${deltaIconClass}`} /> {chartDeltaText}
              </span>
            </div>
            
            <div className="flex items-center gap-4 ml-3 pl-4 border-l border-gray-200/80 mb-0.5">
              <div className="flex flex-col gap-0.5">
                <span className="text-[9px] text-gray-400 font-medium tracking-wider">峰值</span>
                <span className="text-[13px] text-gray-900 font-bold leading-none">{chartPeak}%</span>
              </div>
              <div className="flex flex-col gap-0.5">
                <span className="text-[9px] text-gray-400 font-medium tracking-wider">均值</span>
                <span className="text-[13px] text-gray-900 font-bold leading-none">{chartAvg}%</span>
              </div>
            </div>
          </div>
        </div>
        
        <div className="flex items-center gap-4 text-[11px] text-gray-500 font-medium mt-1">
            <div className="flex items-center gap-1.5">
              <div className="w-2 h-2 rounded-full border-[1.5px] bg-transparent" style={{ borderColor: DASHBOARD_CYAN_LIGHT }}></div>预测值
            </div>
            <div className="flex items-center gap-1.5">
              <div className="w-2 h-2 rounded-full" style={{ backgroundColor: DASHBOARD_CYAN }}></div>实际值
            </div>
        </div>
      </div>
      <div className="w-full h-[106px] relative">
        <ChartArea data={currentData.length ? currentData : [{ name: '', value: 0, predict: 0 }]} />
      </div>
    </div>
  );
}

function ModeItem({ icon, title, desc, active = false, onClick }: { icon: React.ReactNode, title: string, desc: string, active?: boolean, onClick?: () => void }) {
  return (
    <div onClick={onClick} className={`flex flex-col gap-1.5 py-1.5 pr-2 border-l-[3px] pl-3 transition-all cursor-pointer group relative overflow-hidden
      ${active ? 'border-transparent bg-transparent' : 'border-transparent hover:border-gray-300 hover:bg-gray-50/50'}
    `}>
      <div className={`transition-colors relative z-10 ${active ? 'text-gray-900' : 'text-gray-400 group-hover:text-gray-600'}`}>
        {icon}
      </div>
      {active && <div className="absolute inset-y-2 left-0 w-[3px] rounded-full" style={{ backgroundColor: DASHBOARD_CYAN }} />}
      <div className="relative z-10">
        <h4 className={`text-[13px] font-bold mb-0.5 transition-colors ${active ? 'text-gray-900' : 'text-gray-900'}`}>{title}</h4>
        <p className={`text-[11px] leading-relaxed transition-colors ${active ? 'text-gray-500' : 'text-gray-400'}`}>{desc}</p>
      </div>
    </div>
  );
}

function IndustryPieChart({ data }: { data?: { name: string; value: number }[] }) {
  const chartData = data && data.length ? data : [
    { name: '科技互联网', value: 45 },
    { name: '消费零售', value: 25 },
    { name: '金融医疗', value: 20 },
    { name: '汽车制造', value: 10 },
  ];

  return (
    <div className="w-full">
      <div className="relative h-[126px]">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={chartData}
              cx="50%"
              cy="50%"
              innerRadius={42}
              outerRadius={56}
              paddingAngle={2}
              dataKey="value"
              stroke="none"
            >
              {chartData.map((entry, index) => (
                <Cell key={`cell-${entry.name}-${index}`} fill={INDUSTRY_CHART_COLORS[index % INDUSTRY_CHART_COLORS.length]} />
              ))}
            </Pie>
            <RechartsTooltip 
              contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 12px rgba(0,0,0,0.05)', fontSize: '11px' }}
              itemStyle={{ color: '#111827' }}
            />
          </PieChart>
        </ResponsiveContainer>
        {/* Center Label — 叠加在图表中心 */}
        <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
          <span className="text-[18px] font-bold text-gray-900 leading-none">100<span className="text-[11px] font-medium text-gray-500">%</span></span>
          <span className="text-[9px] text-gray-400 mt-1 uppercase tracking-wider">总来源</span>
        </div>
      </div>
    </div>
  );
}

function MediaBarChart({ data }: { data?: DashboardSnapshot["mediaStats"] }) {
  const [filter, setFilter] = useState<'all' | 'auth' | 'self'>('all');
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const chartData = data && data.length ? data : FALLBACK_MEDIA_STATS;
  const displayData = chartData;
  const chartMinWidth = Math.max(520, displayData.length * 34 + 48);
  const labeledData = useMemo(
    () => displayData.map((item, index) => ({
      ...item,
      label: formatMediaStatAxisLabel(item, index, displayData.length),
    })),
    [displayData],
  );
  const rawMaxValue = displayData.reduce((max, item) => {
    const currentValue =
      filter === 'auth' ? item.auth :
      filter === 'self' ? item.self :
      item.auth + item.self;
    return Math.max(max, currentValue);
  }, 0);
  const yAxisMax = Math.max(1, rawMaxValue);
  const yTickStep = Math.max(1, Math.ceil(yAxisMax / 4));
  const yTicks: number[] = [];
  for (let value = 0; value < yAxisMax; value += yTickStep) {
    yTicks.push(value);
  }
  if (yTicks[yTicks.length - 1] !== yAxisMax) {
    yTicks.push(yAxisMax);
  }

  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      container.scrollLeft = Math.max(0, container.scrollWidth - container.clientWidth - 20);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [displayData, filter, chartMinWidth]);

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-start justify-between mb-3">
        <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">发布媒体统计</h3>
        
        {/* Filter */}
        <div className="flex items-center gap-2">
          <button 
            onClick={() => setFilter(filter === 'auth' ? 'all' : 'auth')}
            className={`text-[10px] font-bold transition-colors ${filter === 'auth' ? 'text-gray-900' : 'text-gray-400 hover:text-gray-700'}`}
          >
            权威媒体
          </button>
          <span className="w-px h-2.5 bg-gray-200"></span>
          <button 
            onClick={() => setFilter(filter === 'self' ? 'all' : 'self')}
            className={`text-[10px] font-bold transition-colors ${filter === 'self' ? 'text-gray-900' : 'text-gray-400 hover:text-gray-700'}`}
          >
            自媒体
          </button>
        </div>
      </div>

      <div className="w-full overflow-hidden">
        <div className="flex w-full">
          <div className="w-[40px] shrink-0">
            <div className="flex h-[132px] flex-col justify-between pt-[8px] pr-2 pb-0 text-right text-[10px] font-medium leading-none text-gray-400">
              {[...yTicks].reverse().map((tick, index) => (
                <div key={`${tick}-${index}`} className="tabular-nums">
                  {tick}
                </div>
              ))}
            </div>
            <div className="h-[24px]" />
          </div>

          <div ref={scrollContainerRef} className="flex-1 overflow-x-auto overflow-y-hidden custom-scrollbar">
            <div className="h-full" style={{ minWidth: chartMinWidth }}>
              <div className="h-[132px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={displayData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }} barCategoryGap="10%" barGap={2}>
                    <CartesianGrid key="grid" strokeDasharray="3 3" vertical={false} stroke="#f3f4f6" />
                    <XAxis
                      key="xaxis"
                      dataKey="name"
                      hide
                    />
                    <RechartsTooltip
                      key="tooltip"
                      cursor={{ fill: '#f8fafc' }}
                      contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 12px rgba(0,0,0,0.05)', fontSize: '11px' }}
                    />
                    <Bar key="bar-auth" hide={filter !== 'all' && filter !== 'auth'} dataKey="auth" name="权威媒体" fill={DASHBOARD_DEEP} radius={[4, 4, 0, 0]} maxBarSize={22} />
                    <Bar key="bar-self" hide={filter !== 'all' && filter !== 'self'} dataKey="self" name="自媒体" fill={DASHBOARD_CYAN} radius={[4, 4, 0, 0]} maxBarSize={22} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div
                className="grid h-[22px] items-start pr-[10px]"
                style={{ gridTemplateColumns: `repeat(${displayData.length || 1}, minmax(0, 1fr))` }}
              >
                {labeledData.map((item, index) => (
                  <div key={`${item.label || item.name}-${index}`} className="pt-1 text-center text-[10px] leading-none font-medium text-gray-400">
                    {item.label || item.name}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function OptimizationMap({ activeRegions = [] }: { activeRegions?: string[] }) {
  const [mapType, setMapType] = useState<'domestic' | 'international'>('domestic');

  // Domestic nodes covering all Chinese provinces
  const domesticNodes = [
    { id: 'xj', label: '新疆', x: '18%', y: '35%' },
    { id: 'xz', label: '西藏', x: '18%', y: '65%' },
    { id: 'qh', label: '青海', x: '35%', y: '48%' },
    { id: 'gs', label: '甘肃', x: '45%', y: '40%' },
    { id: 'nm', label: '内蒙古', x: '55%', y: '25%' },
    { id: 'hlj', label: '黑龙江', x: '82%', y: '15%' },
    { id: 'jl', label: '吉林', x: '85%', y: '25%' },
    { id: 'ln', label: '辽宁', x: '80%', y: '32%' },
    { id: 'bj', label: '北京', x: '70%', y: '35%' },
    { id: 'tj', label: '天津', x: '73%', y: '38%' },
    { id: 'he', label: '河北', x: '68%', y: '40%' },
    { id: 'sx', label: '山西', x: '60%', y: '45%' },
    { id: 'sn', label: '陕西', x: '55%', y: '52%' },
    { id: 'nx', label: '宁夏', x: '48%', y: '45%' },
    { id: 'sd', label: '山东', x: '75%', y: '46%' },
    { id: 'ha', label: '河南', x: '65%', y: '54%' },
    { id: 'js', label: '江苏', x: '80%', y: '56%' },
    { id: 'ah', label: '安徽', x: '75%', y: '60%' },
    { id: 'sh', label: '上海', x: '85%', y: '59%' },
    { id: 'zj', label: '浙江', x: '82%', y: '66%' },
    { id: 'jx', label: '江西', x: '75%', y: '70%' },
    { id: 'fj', label: '福建', x: '80%', y: '75%' },
    { id: 'tw', label: '台湾', x: '86%', y: '80%' },
    { id: 'hb', label: '湖北', x: '65%', y: '62%' },
    { id: 'hn', label: '湖南', x: '65%', y: '72%' },
    { id: 'gd', label: '广东', x: '70%', y: '85%' },
    { id: 'hk', label: '香港', x: '73%', y: '90%' },
    { id: 'mc', label: '澳门', x: '68%', y: '90%' },
    { id: 'hi', label: '海南', x: '65%', y: '96%' },
    { id: 'gx', label: '广西', x: '60%', y: '85%' },
    { id: 'gz', label: '贵州', x: '52%', y: '76%' },
    { id: 'sc', label: '四川', x: '45%', y: '66%' },
    { id: 'cq', label: '重庆', x: '53%', y: '66%' },
    { id: 'yn', label: '云南', x: '40%', y: '82%' },
  ].map(n => ({ ...n, active: activeRegions.includes(n.label) }));

  const internationalNodes = [
    { id: 'usa', label: '美国', x: '20%', y: '35%' },
    { id: 'japan', label: '日本', x: '85%', y: '30%' },
    { id: 'uk', label: '英国', x: '45%', y: '25%' },
    { id: 'singapore', label: '新加坡', x: '75%', y: '55%' },
    { id: 'germany', label: '德国', x: '52%', y: '30%' },
    { id: 'australia', label: '澳大利亚', x: '85%', y: '80%' },
    { id: 'france', label: '法国', x: '50%', y: '35%' },
    { id: 'canada', label: '加拿大', x: '20%', y: '20%' },
    { id: 'sk', label: '韩国', x: '80%', y: '35%' },
    { id: 'brazil', label: '巴西', x: '30%', y: '75%' },
    { id: 'india', label: '印度', x: '25%', y: '60%' },
    { id: 'russia', label: '俄罗斯', x: '70%', y: '20%' },
  ].map(n => ({ ...n, active: activeRegions.includes(n.label) }));

  const nodes = mapType === 'domestic' ? domesticNodes : internationalNodes;

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-start justify-between mb-3">
        <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">优化区域分布</h3>
        
        {/* Toggle */}
        <div className="flex items-center gap-4">
          <button 
            onClick={() => setMapType('domestic')}
            className={`flex items-center gap-1 text-[11px] font-medium transition-colors ${mapType === 'domestic' ? 'text-gray-900' : 'text-gray-400 hover:text-gray-600'}`}
          >
            <Map className="w-3 h-3" /> 国内
          </button>
          <span className="w-px h-2.5 bg-gray-200"></span>
          <button 
            onClick={() => setMapType('international')}
            className={`flex items-center gap-1 text-[11px] font-medium transition-colors ${mapType === 'international' ? 'text-gray-900' : 'text-gray-400 hover:text-gray-600'}`}
          >
            <Globe className="w-3 h-3" /> 国际
          </button>
        </div>
      </div>

      <div className="w-full bg-gray-50/50 rounded-2xl relative border border-gray-100/50 overflow-hidden" style={{ height: '152px' }}>
        {/* Grid Background to simulate tech map */}
        <div className="absolute inset-0 opacity-[0.03]" style={{ backgroundImage: 'linear-gradient(#000 1px, transparent 1px), linear-gradient(90deg, #000 1px, transparent 1px)', backgroundSize: '16px 16px' }}></div>
        
        {/* Abstract Connections */}
        <svg className="absolute inset-0 w-full h-full pointer-events-none opacity-20">
          {nodes.filter(n => n.active).map((node, i, arr) => {
            if (i === arr.length - 1) return null;
            const next = arr[i + 1];
            return (
              <line 
                key={`line-${i}`}
                x1={node.x} y1={node.y} x2={next.x} y2={next.y}
                stroke={DASHBOARD_CYAN} strokeWidth="1" strokeDasharray="3 3"
              />
            );
          })}
        </svg>

        {/* Nodes */}
        {nodes.map((node) => (
          <div 
            key={node.id}
            className="absolute transform -translate-x-1/2 -translate-y-1/2 flex flex-col items-center gap-1"
            style={{ left: node.x, top: node.y }}
          >
            {/* Dot */}
            <div className="relative flex items-center justify-center">
              {node.active && <div className="absolute w-5 h-5 rounded-full animate-ping" style={{ backgroundColor: "rgba(47, 184, 230, 0.16)" }}></div>}
              <div
                className={`w-2 h-2 rounded-full border-[1.5px] ${node.active ? 'border-white shadow-sm' : 'bg-gray-300 border-white'}`}
                style={node.active ? { backgroundColor: DASHBOARD_CYAN, borderColor: "#fff" } : undefined}
              ></div>
            </div>
            {/* Label */}
            <span className={`text-[9px] font-bold tracking-wider ${node.active ? 'text-gray-700' : 'text-gray-400'}`}>
              {node.label}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
