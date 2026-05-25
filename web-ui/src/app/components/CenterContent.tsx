import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUpRight, ExternalLink, X } from "lucide-react";
import { ChartArea } from "./Charts";
import { FailedTasksModal } from "./FailedTasksModal";
import { AmapRegionMap } from "./AmapRegionMap";
import type { AihotDailyFeedSnapshot, DashboardSnapshot, MonthOverviewDay, MonthOverviewSnapshot, TrendSnapshot } from "../lib/backend";
import { fetchAihotDailyFeed, fetchDashboardTrend } from "../lib/backend";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, ResponsiveContainer, Tooltip as RechartsTooltip,
  PieChart, Pie, Cell
} from 'recharts';

const DASHBOARD_DEEP = "#2F5A67";
const DASHBOARD_TEAL = "#1E7F95";
const DASHBOARD_CYAN = "#2FB8E6";
const DASHBOARD_CYAN_LIGHT = "#74D2EE";
const DASHBOARD_CYAN_SOFT = "#CBEFF9";
const AIHOT_DAILY_FEED_URL = "https://aihot.virxact.com/feed/daily.xml";
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

function formatCompactNumber(value: number) {
  const safeValue = Number.isFinite(value) ? value : 0;
  if (Math.abs(safeValue) >= 10000) {
    const compact = safeValue / 10000;
    return `${Number.isInteger(compact) ? compact.toFixed(0) : compact.toFixed(1)}w`;
  }
  return Math.round(safeValue).toLocaleString();
}

function formatCurrency(value: number) {
  const safeValue = Number.isFinite(value) ? value : 0;
  if (Math.abs(safeValue) >= 10000) {
    const compact = safeValue / 10000;
    return `¥${Number.isInteger(compact) ? compact.toFixed(0) : compact.toFixed(1)}w`;
  }
  return `¥${Math.round(safeValue).toLocaleString()}`;
}

function formatMonthDayLabel(dateText: string) {
  const [, month = "", day = ""] = String(dateText || "").split("-");
  const monthNum = Number(month);
  const dayNum = Number(day);
  if (!monthNum || !dayNum) {
    return dateText || "今日";
  }
  return `${monthNum}月${dayNum}日`;
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
  dashboard,
  runMessage,
  activeRegions,
  onDataChanged,
}: {
  dashboard?: DashboardSnapshot;
  runMessage?: string;
  activeRegions?: string[];
  onDataChanged?: () => Promise<void> | void;
}) {
  const greeting = dashboard?.greeting || "下午好";
  const userName = dashboard?.userName || "Saffron";
  const headline = dashboard?.headline || "系统运行平稳，今日已为您自动拦截 12 项异常请求。";
  const todayTaskCount = dashboard?.todayTaskCount ?? 42;
  const completedCount = dashboard?.completedCount ?? 38;
  const runningCount = dashboard?.runningCount ?? 4;
  const sourceBreakdown = dashboard?.sourceBreakdown || [];
  const mediaStats = dashboard?.mediaStats || [];
  const failedTasks = dashboard?.failedTasks || [];
  const failedTaskCount = dashboard?.failedTaskCount ?? failedTasks.length;
  const [showFailedTasksModal, setShowFailedTasksModal] = useState(false);

  const todayFormatted = useMemo(() => {
    const now = new Date();
    return `${now.getMonth() + 1}月${now.getDate()}日`;
  }, []);

  return (
    <div className="flex-1 min-w-0 min-h-0 overflow-x-hidden overflow-y-auto bg-transparent px-6 py-4 xl:px-8 xl:py-5 flex flex-col custom-scrollbar">
      {/* 1. Header */}
      <div className="grid gap-5 border-b border-gray-200/70 pb-3 mb-3 pt-1 shrink-0 xl:grid-cols-[minmax(0,1fr)_300px] xl:items-start">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h1 className="app-display-heading text-[28px] font-semibold text-[#0f1835] tracking-[-0.035em]">
            {greeting}，{userName}
          </h1>
          <p className="app-subtle-copy text-[13px] mt-0.5 max-w-[720px] leading-6 font-medium">
            {headline}
          </p>
          {runMessage && (
            <div className="mt-1.5 flex items-center gap-2">
              <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: DASHBOARD_CYAN }} />
              <p className="text-[12px] leading-5 font-medium text-slate-500">
                {runMessage}
              </p>
            </div>
          )}
        </div>
        <AihotDailyFeedSection compact />
      </div>

      <div className="flex flex-col min-h-max">
        {/* Row 1: Today's Tasks + Month Overview */}
        <div className="flex flex-col gap-6 pb-5 shrink-0 xl:flex-row xl:items-start xl:justify-between xl:gap-8">
          {/* Today's Tasks — 与月度总览同款 [header] [metrics + 视觉锚点] 结构 */}
          <div className="flex w-full max-w-[310px] flex-col xl:w-[310px]">
            <div className="mb-4 flex items-baseline justify-between gap-3">
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-gray-400">今日检测任务</h3>
              <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-gray-50 border border-gray-100/80">
                <span className="text-[10px] font-bold tabular-nums text-gray-400 uppercase tracking-tight">{todayFormatted}</span>
              </div>
            </div>

            <div className="grid min-h-[120px] grid-cols-[minmax(0,1fr)_auto] items-center gap-6">
              <div className="grid grid-cols-2 gap-x-4 gap-y-7">
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-gray-400/80 whitespace-nowrap">今日任务</span>
                  <div className="flex items-baseline gap-1">
                    <span className="text-[19px] font-bold leading-none tabular-nums text-gray-900 tracking-tight">{todayTaskCount}</span>
                    <span className="shrink-0 text-[10px] font-bold text-gray-400">项</span>
                  </div>
                </div>
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-gray-400/80 whitespace-nowrap">已完成</span>
                  <span className="text-[19px] font-bold leading-none tabular-nums text-gray-900 tracking-tight">{completedCount}</span>
                </div>
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-gray-400/80 whitespace-nowrap">进行中</span>
                  <div className="flex items-center gap-1.5">
                    <span className="text-[19px] font-bold leading-none tabular-nums text-gray-900 tracking-tight">{runningCount}</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setShowFailedTasksModal(true)}
                  className="group flex min-w-0 flex-col gap-1 text-left transition-all hover:opacity-75 active:scale-95"
                >
                  <span className="text-[11px] font-bold uppercase tracking-wider text-gray-400/80 whitespace-nowrap">任务状态</span>
                  <span className={`text-[17px] font-bold leading-none whitespace-nowrap tracking-tight ${failedTaskCount > 0 ? "text-red-600" : "text-gray-900"}`}>
                    {failedTaskCount > 0 ? "失败明细" : "运行正常"}
                  </span>
                </button>
              </div>

              <CompletionRing completed={completedCount} total={todayTaskCount} />
            </div>
          </div>

          {/* Month Overview */}
          <MonthOverviewPanel overview={dashboard?.monthOverview} />
        </div>

        {/* Row 2: Trend & Pie Chart */}
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-5 border-t border-gray-200/70 pt-5 pb-1 shrink-0 items-start">
          {/* AI Trend */}
          <AITrendSection initialTrend={dashboard?.trend} />

          {/* Industry Pie */}
          <div className="xl:col-span-6 flex flex-col">
            <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase mb-3">品牌行业来源</h3>
            <div className="w-full relative">
              <IndustryPieChart data={sourceBreakdown} />
            </div>
          </div>
        </div>

        {/* Row 3: Bar & Map */}
        <div className="grid grid-cols-1 xl:grid-cols-12 gap-5 border-t border-gray-200/70 pt-7 shrink-0 items-start">
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

function CompletionRing({ completed, total }: { completed: number; total: number }) {
  const size = 108;
  const strokeWidth = 5;
  const radius = (size - strokeWidth) / 2;
  const circumference = radius * 2 * Math.PI;
  const safeTotal = total > 0 ? total : 0;
  const safeCompleted = Math.max(0, Math.min(completed, safeTotal));
  const percent = safeTotal > 0 ? Math.round((safeCompleted / safeTotal) * 100) : 0;
  const dashoffset = circumference - (percent / 100) * circumference;

  return (
    <div className="relative shrink-0 flex items-center justify-center select-none" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        {/* Track circle */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          stroke="#f1f5f9"
          strokeWidth={strokeWidth}
          fill="none"
        />
        {/* Progress circle */}
        {safeTotal > 0 && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            stroke={DASHBOARD_CYAN}
            strokeWidth={strokeWidth}
            fill="none"
            strokeDasharray={circumference}
            strokeDashoffset={dashoffset}
            strokeLinecap="round"
            className="transition-all duration-1000 cubic-bezier(0.4, 0, 0.2, 1)"
          />
        )}
      </svg>

      <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
        <div className="flex items-baseline gap-0.5 leading-none">
          <span className="text-[24px] font-bold text-gray-900 tracking-tight">{percent}</span>
          <span className="text-[12px] font-bold text-gray-400/80">%</span>
        </div>
        <div className="mt-1.5 flex items-center gap-1 px-2 py-0.5 rounded-full bg-gray-50 border border-gray-100/50">
          <span className="text-[10px] font-bold leading-none text-gray-500 tabular-nums">
            {safeCompleted}
          </span>
          <span className="text-[9px] font-medium text-gray-300">/</span>
          <span className="text-[10px] font-bold leading-none text-gray-400 tabular-nums">
            {safeTotal}
          </span>
        </div>
      </div>
    </div>
  );
}

function MonthOverviewPanel({ overview }: { overview?: MonthOverviewSnapshot }) {
  const days = overview?.days || [];
  const totals = overview?.totals;
  const metricItems = [
    { label: "文章发表", value: formatCompactNumber(totals?.articlePublishedTotal ?? 0), hint: "篇" },
    { label: "花费总额", value: formatCurrency(totals?.totalSpend ?? 0), hint: "" },
    { label: "工作天数", value: formatCompactNumber(totals?.workDays ?? 0), hint: "天" },
    { label: "待优化", value: formatCompactNumber(totals?.brandPendingOptimizationCount ?? 0), hint: "项" },
    { label: "引用总数", value: formatCompactNumber(totals?.referenceTotal ?? 0), hint: "次" },
    { label: "覆盖平台", value: formatCompactNumber(totals?.platformCoverage ?? 0), hint: "个" },
  ];

  return (
    <section className="w-full max-w-[420px] xl:ml-auto xl:w-[420px]">
      <div className="mb-4 flex items-baseline justify-between">
        <h3 className="text-[11px] font-bold uppercase tracking-wider text-gray-400">月度总览</h3>
        <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-gray-50 border border-gray-100/80">
          <span className="text-[10px] font-bold tabular-nums text-gray-400 uppercase tracking-tight">{overview?.monthLabel || "Monthly"}</span>
        </div>
      </div>

      <div className="grid min-h-[120px] grid-cols-[minmax(0,1fr)_auto] items-center gap-6">
        <div className="grid grid-cols-3 gap-x-4 gap-y-7">
          {metricItems.map((item) => (
            <div key={item.label} className="flex min-w-0 flex-col gap-1">
              <span className="text-[11px] font-bold uppercase tracking-wider text-gray-400/80 whitespace-nowrap">{item.label}</span>
              <div className="flex min-w-0 items-baseline gap-1">
                <span className="text-[19px] font-bold leading-none tabular-nums text-gray-900 tracking-tight">{item.value}</span>
                {item.hint && <span className="shrink-0 text-[10px] font-bold text-gray-400">{item.hint}</span>}
              </div>
            </div>
          ))}
        </div>

        <MiniMonthHeatmap days={days} todayDate={overview?.selectedDate} />
      </div>
    </section>
  );
}

function MiniMonthHeatmap({
  days,
  todayDate,
}: {
  days: MonthOverviewDay[];
  todayDate?: string;
}) {
  const activeDays = days.length ? days : [];
  const firstWeekday = activeDays[0]?.weekday ?? 0;

  // Determine current view month ("YYYY-MM") — used to mark days from the
  // previous/next month as "virtual" even when the backend sends them as real day objects.
  const viewMonth =
    (todayDate || activeDays[Math.floor(activeDays.length / 2)]?.date || "").slice(0, 7);

  // 1. Leading cells (not in this month)
  const leadingCells = Array.from({ length: firstWeekday }, (_item, index) => ({
    key: `leading-${index}`,
    day: null as MonthOverviewDay | null,
  }));

  const dayCells = activeDays.map((day) => ({
    key: day.date,
    day,
  }));

  // 2. Trailing cells to fill a complete rectangle (35 or 42 cells)
  const currentTotal = leadingCells.length + dayCells.length;
  const targetTotal = currentTotal <= 35 ? 35 : 42;
  const trailingCellCount = targetTotal - currentTotal;
  const trailingCells = Array.from({ length: trailingCellCount }, (_item, index) => ({
    key: `trailing-${index}`,
    day: null as MonthOverviewDay | null,
  }));

  const heatmapCells = [...leadingCells, ...dayCells, ...trailingCells];

  return (
    <div className="overflow-visible select-none">
      <div
        className="grid w-max gap-[5px]"
        style={{
          gridTemplateColumns: "repeat(7, 18px)",
        }}
      >
        {heatmapCells.map(({ key, day }) => {
          // Out of current view month: either a filler null cell, or a real
          // day object from previous/next month — both rendered as subtle ghost.
          if (!day || (viewMonth && day.date.slice(0, 7) !== viewMonth)) {
            return (
              <span
                key={key}
                className="h-[18px] w-[18px] rounded-[4px] bg-gray-100/30 border border-gray-200/30"
                aria-hidden="true"
              />
            );
          }

          // In month: Solid base color (including future/zero-intensity days)
          const toneClass = day.intensity >= 4
            ? "bg-[#2F5A67]"
            : day.intensity === 3
              ? "bg-[#1E7F95]"
              : day.intensity === 2
                ? "bg-[#2FB8E6]"
                : day.intensity === 1
                  ? "bg-[#74D2EE]"
                  : "bg-slate-100";

          const isToday = todayDate ? day.date === todayDate : false;

          return (
            <button
              key={day.date}
              type="button"
              title={`${formatMonthDayLabel(day.date)}：文章 ${day.articleCount}，展示 ${day.displayCount}，引用 ${day.referenceCount}`}
              aria-label={`${formatMonthDayLabel(day.date)}数据`}
              className={`group relative h-[18px] w-[18px] rounded-[4px] transition-all duration-300 hover:ring-2 hover:ring-[#2FB8E6]/40 hover:scale-110 hover:z-10 focus:outline-none focus:ring-2 focus:ring-[#2FB8E6]/50 ${isToday ? "ring-[2.5px] ring-[#2FB8E6] ring-offset-2 ring-offset-white z-10" : ""} ${toneClass}`}
            >
              <span className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-2 hidden w-max max-w-[220px] -translate-x-1/2 rounded-[8px] bg-[#0f1835] px-2.5 py-1.5 text-[11px] font-bold text-white shadow-xl animate-in fade-in zoom-in duration-200 group-hover:block group-focus:block">
                <div className="flex items-center justify-between gap-4 mb-1 border-b border-white/10 pb-1">
                  <span className="text-white/70">{formatMonthDayLabel(day.date)}</span>
                  {isToday && <span className="text-[#2FB8E6] text-[10px]">今日</span>}
                </div>
                <div className="space-y-0.5 opacity-90">
                  <div className="flex justify-between gap-3">
                    <span className="text-white/50 font-medium">发表</span>
                    <span className="font-mono">{day.articleCount} 篇</span>
                  </div>
                  <div className="flex justify-between gap-3">
                    <span className="text-white/50 font-medium">花费</span>
                    <span className="font-mono">{formatCurrency(day.spend)}</span>
                  </div>
                  <div className="flex justify-between gap-3 border-t border-white/5 pt-0.5 mt-0.5">
                    <span className="text-white/50 font-medium">展示</span>
                    <span className="font-mono">{day.displayCount}</span>
                  </div>
                </div>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

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
    <div className="xl:col-span-6 flex flex-col min-h-0">
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

        {/* 预测/实际 legend — 垂直堆叠，放在 header 右边 */}
        <div className="mr-[10px] mt-1 flex flex-col gap-1.5 text-[11px] text-gray-500 font-medium">
          <div className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full border-[1.5px] bg-transparent" style={{ borderColor: DASHBOARD_CYAN_LIGHT }}></div>
            <span>预测</span>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full" style={{ backgroundColor: DASHBOARD_CYAN }}></div>
            <span>实际</span>
          </div>
        </div>
      </div>
      <div className="w-full h-[116px] relative">
        <ChartArea
          data={currentData.length ? currentData : [{ name: '', value: 0, predict: 0 }]}
          yLabelAtLeft
          alignEdgeXTicks
        />
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

  const total = chartData.reduce((sum, item) => sum + (item.value || 0), 0);
  const MAX_LEGEND_ROWS = 5;
  const hasOverflow = chartData.length > MAX_LEGEND_ROWS;
  const visibleItems = hasOverflow ? chartData.slice(0, MAX_LEGEND_ROWS - 1) : chartData;
  const hiddenCount = chartData.length - visibleItems.length;

  return (
    <div className="flex w-full items-start gap-6">
      {/* 左侧环形图 — 固定尺寸 */}
      <div className="relative h-[126px] w-[126px] shrink-0">
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

      {/* 右侧分类 legend — 最多 5 行，超出用 "…还有 N 个分类" 顶替 */}
      <ul className="flex min-w-0 flex-1 flex-col gap-4 pt-1">
        {visibleItems.map((item, index) => {
          const pct = total > 0 ? Math.round(((item.value || 0) / total) * 100) : 0;
          const color = INDUSTRY_CHART_COLORS[index % INDUSTRY_CHART_COLORS.length];
          return (
            <li key={`legend-${item.name}-${index}`} className="flex items-center gap-3 text-[12px] leading-none">
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: color }} />
              <span className="truncate font-medium text-gray-600" title={item.name}>{item.name}</span>
              <span className="ml-auto shrink-0 tabular-nums font-semibold text-gray-900">{pct}%</span>
            </li>
          );
        })}
        {hasOverflow && (() => {
          const hiddenItemsList = chartData.slice(visibleItems.length);
          const hiddenSum = hiddenItemsList.reduce((s, item) => s + (item.value || 0), 0);
          const hiddenPct = total > 0 ? Math.round((hiddenSum / total) * 100) : 0;
          return (
            <li
              className="flex items-center gap-3 text-[12px] leading-none"
              title={hiddenItemsList.map((item) => item.name).join("、")}
            >
              <span className="h-2 w-2 shrink-0 rounded-full bg-gray-300" />
              <span className="truncate font-medium text-gray-400">其他 {hiddenCount} 项</span>
              <span className="ml-auto shrink-0 tabular-nums font-semibold text-gray-400">{hiddenPct}%</span>
            </li>
          );
        })()}
      </ul>
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
            <div className="flex h-[154px] flex-col justify-between pt-[8px] pr-2 pb-0 text-right text-[10px] font-medium leading-none text-gray-400">
              {[...yTicks].reverse().map((tick, index) => (
                <div key={`${tick}-${index}`} className="tabular-nums">
                  {tick}
                </div>
              ))}
            </div>
              <div className="h-[22px]" />
          </div>

          <div ref={scrollContainerRef} className="flex-1 overflow-x-auto overflow-y-hidden custom-scrollbar">
            <div className="h-full" style={{ minWidth: chartMinWidth }}>
              <div className="h-[154px]">
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
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-start justify-between mb-3">
        <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">品牌地址分布</h3>
      </div>

      <AmapRegionMap
        mapType="domestic"
        activeRegions={activeRegions}
        accentColor={DASHBOARD_CYAN}
        className="h-[176px] w-full rounded-[8px]"
      />
    </div>
  );
}

function AihotDailyFeedSection({ compact = false }: { compact?: boolean }) {
  const [feed, setFeed] = useState<AihotDailyFeedSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [showFeedDetail, setShowFeedDetail] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void fetchAihotDailyFeed()
      .then((data) => {
        if (!cancelled) {
          setFeed(data);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const items = feed?.items || [];
  const visibleItems = items.slice(0, 3);

  return (
    <section className={compact ? "flex flex-col min-w-0" : "border-t border-gray-200/70 pt-8 pb-4 shrink-0"}>
      <div className="mb-1 flex items-baseline justify-between">
        <div className="flex items-baseline gap-2">
          <h3 className="text-[11px] font-bold uppercase tracking-wider text-gray-400">今日速览</h3>
        </div>
        {visibleItems.length > 0 && (
          <button
            type="button"
            onClick={() => setShowFeedDetail(true)}
            className="group inline-flex items-center gap-1 text-[11px] font-medium text-gray-400 transition-colors hover:text-[#0f1835]"
          >
            查看全部
            <ArrowUpRight className="h-3 w-3 transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" />
          </button>
        )}
      </div>

      {loading ? (
        <ul className="flex flex-col">
          {[0, 1, 2].map((index) => (
            <li
              key={index}
              className={`py-2 ${index !== 2 ? "border-b border-gray-100/70" : ""}`}
            >
              <div className="h-3 w-3/4 animate-pulse rounded bg-gray-100" />
            </li>
          ))}
        </ul>
      ) : visibleItems.length > 0 ? (
        <ul className="flex flex-col">
          {visibleItems.map((item, index) => (
            <li
              key={`${item.link || item.title}-${index}`}
              className={`group cursor-pointer py-2 transition-colors ${index !== visibleItems.length - 1 ? "border-b border-gray-100/70" : ""}`}
              onClick={() => setShowFeedDetail(true)}
            >
              <p
                className="truncate text-[13px] font-semibold leading-6 tracking-tight text-[#0f1835] transition-colors duration-200 group-hover:text-[#1E7F95]"
                title={item.title}
              >
                {item.title}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="py-2 text-[12px] font-medium text-gray-300">暂无更新</p>
      )}
      {showFeedDetail && feed && <AihotDailyFeedDetail feed={feed} onClose={() => setShowFeedDetail(false)} />}
    </section>
  );
}

function AihotDailyFeedDetail({
  feed,
  onClose,
}: {
  feed: AihotDailyFeedSnapshot;
  onClose: () => void;
}) {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-[110] flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/20 backdrop-blur-[2px] animate-in fade-in duration-200"
        onClick={onClose}
      />

      <div className="relative mx-4 flex h-full max-h-[720px] w-full max-w-[680px] flex-col overflow-hidden rounded-2xl border border-gray-200/60 bg-white shadow-[0_20px_60px_-15px_rgba(0,0,0,0.1)] animate-in slide-in-from-bottom-4 duration-300">
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭"
          className="absolute right-5 top-5 z-10 inline-flex h-8 w-8 items-center justify-center rounded-full text-gray-300 transition-colors hover:bg-gray-50 hover:text-gray-700"
        >
          <X className="w-4 h-4" />
        </button>

        <div className="shrink-0 px-10 pt-10 pb-2">
          <h2 className="text-[22px] font-semibold leading-none tracking-tight text-[#0f1835]">今日速览</h2>
          <p className="mt-2 text-[12px] font-medium tabular-nums text-gray-400">
            {feed.items.length} 条
          </p>
        </div>

        <div className="flex-1 overflow-y-auto px-10 pb-10 custom-scrollbar">
          {feed.items.length > 0 ? (
            <ul className="flex flex-col">
              {feed.items.map((item, index) => (
                <AihotDailyFeedArticle
                  key={`${item.link || item.title}-${index}`}
                  item={item}
                  isLast={index === feed.items.length - 1}
                />
              ))}
            </ul>
          ) : (
            <p className="py-16 text-center text-[12px] font-medium text-gray-300">
              {feed.message || "暂无更新"}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function AihotDailyFeedArticle({
  item,
  isLast,
}: {
  item: AihotDailyFeedSnapshot["items"][number];
  isLast: boolean;
}) {
  const summaryText = String(item.summary || item.content || "").replace(/\s+/g, " ").trim();
  const author = item.author?.trim();

  const body = (
    <>
      <h3 className="text-[15px] font-semibold leading-snug tracking-tight text-[#0f1835] transition-colors duration-200 group-hover:text-[#1E7F95]">
        {item.title}
      </h3>
      {summaryText && (
        <p className="mt-2 line-clamp-2 text-[13px] font-normal leading-relaxed text-gray-500">
          {summaryText}
        </p>
      )}
      {author && (
        <p className="mt-3 text-[11px] font-medium tabular-nums text-gray-400">{author}</p>
      )}
    </>
  );

  const className = `group block py-5 ${isLast ? "" : "border-b border-gray-100"}`;

  return (
    <li>
      {item.link ? (
        <a href={item.link} target="_blank" rel="noreferrer" className={className}>
          {body}
        </a>
      ) : (
        <div className={className}>{body}</div>
      )}
    </li>
  );
}
