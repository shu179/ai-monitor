import { useEffect, useState } from "react";
import { X, ChevronDown, Check } from "lucide-react";
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from "recharts";
import { apiFetch } from "../lib/backend";

interface BatchTestReportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  batchId: string;
}

interface ReportData {
  batch_id: string;
  brand: string;
  summary: {
    total_queries: number;
    success_queries: number;
    overall_display_rate: number;
  };
  keyword_stats: Array<{
    keyword: string;
    total_queries: number;
    brand_displays: number;
    display_rate: number;
    platform_stats: Array<{
      platform: string;
      queries: number;
      brand_displays: number;
      display_rate: number;
      media_stats: Record<string, { count: number; ratio: number }>;
    }>;
    cross_platform_media: Array<{
      media: string;
      platform_count: number;
      total_count: number;
      platforms: Array<{ platform: string; count: number; ratio: number }>;
    }>;
  }>;
  platform_stats: Array<{
    platform: string;
    total_queries: number;
    brand_displays: number;
    display_rate: number;
  }>;
  global_media_matrix: Array<{
    media: string;
    total_count: number;
    keyword_count: number;
    platform_count: number;
    matrix: Array<{
      keyword: string;
      platform: string;
      count: number;
      queries: number;
      ratio: number;
    }>;
  }>;
}

const DASHBOARD_CYAN = "#2FB8E6";

const CHART_COLORS = [
  "#2F5A67", "#1E7F95", "#3599B0", "#5CAFC0", "#74D2EE", "#64748b",
  "#475569", "#94a3b8", "#6b7280", "#8b9aab", "#7c8fa3", "#a0aec0",
];

export function BatchTestReportDialog({
  open,
  onOpenChange,
  batchId,
}: BatchTestReportDialogProps) {
  const [report, setReport] = useState<ReportData | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedPlatform, setSelectedPlatform] = useState<string>("");
  const [showPlatformDropdown, setShowPlatformDropdown] = useState(false);

  useEffect(() => {
    if (!open || !batchId) return;

    const fetchReport = async () => {
      setLoading(true);
      try {
        const response = await apiFetch(`/api/batch-test/${batchId}/report`);
        const data = await response.json();

        if (data.ok) {
          setReport(data.report);
          // 默认选择第一个平台
          const firstPlatform = data.report.platform_stats[0]?.platform;
          if (firstPlatform) {
            setSelectedPlatform(firstPlatform);
          }
        }
      } catch (error) {
        console.error("获取报告失败:", error);
      } finally {
        setLoading(false);
      }
    };

    fetchReport();
  }, [open, batchId]);

  const formatPercent = (value: number) => `${(value * 100).toFixed(1)}%`;

  // 获取所有平台列表
  const allPlatforms = report?.platform_stats.map(p => p.platform) || [];

  // 过滤当前平台的数据
  const filteredKeywordStats = report?.keyword_stats.map(kw => ({
    ...kw,
    platform_stats: kw.platform_stats.filter(p => p.platform === selectedPlatform),
  })).filter(kw => kw.platform_stats.length > 0);

  // 当前平台的统计
  const currentPlatformStat = report?.platform_stats.find(p => p.platform === selectedPlatform);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[110] flex items-center justify-center">
      <div
        className="absolute inset-0 bg-gray-900/20 backdrop-blur-sm"
        onClick={() => onOpenChange(false)}
      />

      <div className="relative bg-white rounded-2xl w-full max-w-[1200px] max-h-[90vh] overflow-hidden mx-4 flex flex-col" style={{ boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.15)" }}>
        <button
          onClick={() => onOpenChange(false)}
          className="absolute top-6 right-6 p-1 text-gray-400 hover:text-gray-900 transition-colors z-10"
        >
          <X className="w-4 h-4" />
        </button>

        {loading ? (
          <div className="flex-1 flex items-center justify-center py-12">
            <div className="w-8 h-8 border-2 border-gray-200 border-t-gray-900 rounded-full animate-spin" />
          </div>
        ) : !report ? (
          <div className="flex-1 flex items-center justify-center py-12 text-gray-400 text-[13px]">
            报告加载失败
          </div>
        ) : (
          <>
            {/* Header with Stats */}
            <div className="px-8 pt-8 pb-6 border-b border-gray-200/70">
              <p className="text-[10px] text-slate-500 font-semibold tracking-[0.18em] uppercase mb-4">BRAND ANALYSIS REPORT</p>

              <div className="grid grid-cols-4 gap-6">
                <div>
                  <span className="text-[11px] font-bold text-gray-400 tracking-widest uppercase block mb-2">品牌名称</span>
                  <span className="text-[28px] font-semibold text-gray-900 leading-none tracking-tight">{report.brand}</span>
                </div>
                <div>
                  <span className="text-[11px] font-bold text-gray-400 tracking-widest uppercase block mb-2">总查询次数</span>
                  <span className="text-[28px] font-medium text-gray-900 leading-none tracking-tighter">{report.summary.total_queries}</span>
                </div>
                <div>
                  <span className="text-[11px] font-bold text-gray-400 tracking-widest uppercase block mb-2">品牌展示次数</span>
                  <span className="text-[28px] font-medium leading-none tracking-tighter" style={{ color: "#1E7F95" }}>{report.summary.success_queries}</span>
                </div>
                <div>
                  <span className="text-[11px] font-bold text-gray-400 tracking-widest uppercase block mb-2">总体展示率</span>
                  <span className="text-[28px] font-medium leading-none tracking-tighter" style={{ color: DASHBOARD_CYAN }}>{formatPercent(report.summary.overall_display_rate)}</span>
                </div>
              </div>
            </div>

            {/* Platform Filter */}
            <div className="px-8 py-4 border-b border-gray-200/70 flex justify-between items-end">
              <div className="flex items-center gap-4">
                <h2 className="text-[13px] font-bold text-gray-900 tracking-tight">关键词 × 媒体分布</h2>
                {currentPlatformStat && (
                  <div className="text-[11px] text-gray-500">
                    <span className="font-medium">展示率</span>
                    <span className="text-[13px] font-bold text-gray-900 ml-1.5">{formatPercent(currentPlatformStat.display_rate)}</span>
                    <span className="text-gray-400 ml-1">({currentPlatformStat.brand_displays}/{currentPlatformStat.total_queries})</span>
                  </div>
                )}
              </div>

              <div className="relative">
                <button
                  onClick={() => setShowPlatformDropdown(!showPlatformDropdown)}
                  className={`flex items-center gap-1.5 px-0 pb-1 text-[12px] font-medium transition-colors border-b-2 ${
                    showPlatformDropdown ? 'text-[var(--brand-navy)] border-[var(--brand-navy)]' : 'text-gray-700 hover:text-gray-900 border-transparent'
                  }`}
                >
                  {selectedPlatform}
                  <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-200 ${
                    showPlatformDropdown ? 'rotate-180 text-[var(--brand-navy)]' : 'text-gray-400'
                  }`} />
                </button>

                {showPlatformDropdown && (
                  <div className="absolute top-[calc(100%+8px)] right-0 w-36 bg-white border border-gray-200 rounded-xl py-1.5 z-50 animate-in fade-in slide-in-from-top-2 duration-200">
                    {allPlatforms.map((platform) => (
                      <button
                        key={platform}
                        onClick={() => {
                          setSelectedPlatform(platform);
                          setShowPlatformDropdown(false);
                        }}
                        className="w-full flex items-center justify-between px-4 py-2.5 text-[12px] hover:bg-gray-50 transition-colors text-left group"
                      >
                        <span className={`font-medium ${
                          selectedPlatform === platform ? 'text-[var(--brand-navy)]' : 'text-gray-700 group-hover:text-gray-900'
                        }`}>
                          {platform}
                        </span>
                        {selectedPlatform === platform && (
                          <Check className="w-3.5 h-3.5 text-[var(--brand-navy)]" />
                        )}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Main Content - Keywords */}
            <div className="flex-1 overflow-y-auto px-8 py-6 custom-scrollbar">
              <div className="space-y-6">
                {filteredKeywordStats?.map((kw) => {
                  const plat = kw.platform_stats[0];
                  if (!plat) return null;

                  const mediaEntries = Object.entries(plat.media_stats).sort((a, b) => b[1].count - a[1].count);
                  const pieData = mediaEntries.map(([name, data]) => ({
                    name,
                    value: data.count,
                    ratio: data.ratio
                  }));

                  return (
                    <div key={kw.keyword} className="border-b border-gray-200/50 pb-6 last:border-0">
                      <div className="flex justify-between items-baseline mb-4">
                        <h3 className="text-[15px] font-semibold text-gray-900 tracking-tight">{kw.keyword}</h3>
                        <div className="text-[11px] text-gray-500 font-medium">
                          展示率 <span className="text-[14px] font-bold text-gray-900 ml-1">{formatPercent(plat.display_rate)}</span>
                          <span className="text-gray-400 ml-1">({plat.brand_displays}/{plat.queries})</span>
                        </div>
                      </div>

                      {mediaEntries.length > 0 ? (
                        <div className="flex gap-6">
                          <div className="w-40 h-40 flex-shrink-0">
                            <ResponsiveContainer width="100%" height="100%">
                              <PieChart>
                                <Pie
                                  data={pieData}
                                  cx="50%"
                                  cy="50%"
                                  innerRadius={36}
                                  outerRadius={64}
                                  paddingAngle={1}
                                  dataKey="value"
                                >
                                  {pieData.map((_, index) => (
                                    <Cell key={`cell-${index}`} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                                  ))}
                                </Pie>
                                <Tooltip
                                  content={({ active, payload }) => {
                                    if (active && payload && payload.length) {
                                      const data = payload[0].payload;
                                      return (
                                        <div className="bg-white px-2.5 py-1.5 shadow-lg border border-gray-200 text-[10px] max-w-[200px]">
                                          <div className="font-bold text-gray-900 break-words">{data.name}</div>
                                          <div className="text-gray-600 mt-0.5">{data.value}次 · {formatPercent(data.ratio)}</div>
                                        </div>
                                      );
                                    }
                                    return null;
                                  }}
                                />
                              </PieChart>
                            </ResponsiveContainer>
                          </div>
                          <div className={`flex-1 grid gap-x-4 gap-y-2 text-[11px] content-start ${
                            mediaEntries.length <= 4 ? 'grid-cols-2' :
                            mediaEntries.length <= 9 ? 'grid-cols-3' :
                            'grid-cols-4'
                          }`}>
                            {mediaEntries.map(([mediaName, data], idx) => (
                              <div key={mediaName} className="flex items-start gap-2">
                                <div
                                  className="w-2 h-2 flex-shrink-0 mt-1"
                                  style={{ backgroundColor: CHART_COLORS[idx % CHART_COLORS.length] }}
                                />
                                <div className="flex-1 min-w-0">
                                  <div className="text-gray-700 font-medium break-words leading-tight">{mediaName}</div>
                                  <div className="text-gray-500 font-mono text-[10px] mt-0.5">
                                    {data.count}次 · {formatPercent(data.ratio)}
                                  </div>
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : (
                        <div className="text-[11px] text-gray-400 text-center py-6 bg-gray-50/50">无媒体数据</div>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* Cross Analysis Section */}
              {report.global_media_matrix.length > 0 && (
                <div className="mt-8 pt-8 border-t border-gray-200">
                  <h2 className="text-[13px] font-bold text-gray-900 tracking-tight mb-1">跨平台 × 跨关键词媒体对比</h2>
                  <p className="text-[11px] text-gray-500 mb-5">显示在多个平台或多个关键词中同时出现的媒体</p>

                  <div className="space-y-5">
                    {report.global_media_matrix.map((media) => (
                      <div key={media.media} className="bg-gray-50/50 p-4">
                        <div className="flex justify-between items-baseline mb-3">
                          <h3 className="text-[13px] font-bold text-gray-900">{media.media}</h3>
                          <span className="text-[10px] text-gray-500 font-medium">
                            {media.keyword_count}关键词 · {media.platform_count}平台 · {media.total_count}次
                          </span>
                        </div>

                        <div className="grid grid-cols-1 gap-2">
                          {media.matrix.map((cell, idx) => (
                            <div key={idx} className="flex items-center gap-3 bg-white px-3 py-2 text-[11px]">
                              <span className="text-gray-900 font-semibold w-32 flex-shrink-0">{cell.keyword}</span>
                              <span className="text-gray-600 w-24 flex-shrink-0 font-medium">{cell.platform}</span>
                              <div className="flex-1 bg-gray-100 h-2 overflow-hidden min-w-0">
                                <div
                                  className="h-full"
                                  style={{
                                    width: `${cell.ratio * 100}%`,
                                    backgroundColor: "#74D2EE"
                                  }}
                                />
                              </div>
                              <span className="text-gray-600 w-12 text-right font-mono text-[10px] flex-shrink-0">{cell.count}次</span>
                              <span className="text-gray-500 w-14 text-right font-mono text-[10px] flex-shrink-0">{formatPercent(cell.ratio)}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </>
        )}

        <div className="px-8 py-5 border-t border-gray-200/70">
          <button
            onClick={() => onOpenChange(false)}
            className="w-full py-2.5 text-[12px] font-bold text-gray-600 hover:text-gray-900 transition-colors tracking-wide"
          >
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
