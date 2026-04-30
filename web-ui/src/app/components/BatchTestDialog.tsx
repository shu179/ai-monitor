import { useState } from "react";
import { X, Plus, Trash2, BarChart2, Target, Layers, Brain, Monitor } from "lucide-react";
import { apiFetch } from "../lib/backend";

interface BatchTestDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onStart: (batchId: string) => void;
}

interface KeywordConfig {
  keyword: string;
  platforms: string[];
  deepThinkPlatforms: string[];
  queryCount: number;
}

const PLATFORMS = [
  { id: "doubao", label: "豆包" },
  { id: "deepseek", label: "DeepSeek" },
  { id: "kimi", label: "Kimi" },
  { id: "yuanbao", label: "元宝" },
  { id: "tongyi", label: "通义" },
  { id: "wenxin", label: "文心" },
];

export function BatchTestDialog({ open, onOpenChange, onStart }: BatchTestDialogProps) {
  const [brand, setBrand] = useState("");
  const [keywords, setKeywords] = useState<KeywordConfig[]>([
    { keyword: "", platforms: [], deepThinkPlatforms: [], queryCount: 5 },
  ]);
  const [inspect, setInspect] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const addKeyword = () => {
    setKeywords([...keywords, { keyword: "", platforms: [], deepThinkPlatforms: [], queryCount: 5 }]);
  };

  const removeKeyword = (index: number) => {
    setKeywords(keywords.filter((_, i) => i !== index));
  };

  const updateKeyword = (index: number, field: keyof KeywordConfig, value: any) => {
    const updated = [...keywords];
    updated[index] = { ...updated[index], [field]: value };
    setKeywords(updated);
  };

  const togglePlatform = (keywordIndex: number, platformId: string) => {
    const updated = [...keywords];
    const platforms = updated[keywordIndex].platforms;
    if (platforms.includes(platformId)) {
      updated[keywordIndex].platforms = platforms.filter((p) => p !== platformId);
      // 如果取消平台选择，也取消该平台的深度思考
      updated[keywordIndex].deepThinkPlatforms = updated[keywordIndex].deepThinkPlatforms.filter((p) => p !== platformId);
    } else {
      updated[keywordIndex].platforms = [...platforms, platformId];
    }
    setKeywords(updated);
  };

  const toggleDeepThink = (keywordIndex: number, platformId: string) => {
    const updated = [...keywords];
    const deepThinkPlatforms = updated[keywordIndex].deepThinkPlatforms;
    if (deepThinkPlatforms.includes(platformId)) {
      updated[keywordIndex].deepThinkPlatforms = deepThinkPlatforms.filter((p) => p !== platformId);
    } else {
      updated[keywordIndex].deepThinkPlatforms = [...deepThinkPlatforms, platformId];
    }
    setKeywords(updated);
  };

  const handleStart = async () => {
    if (!brand.trim()) {
      alert("请输入品牌名称");
      return;
    }

    const validKeywords = keywords.filter(
      (kw) => kw.keyword.trim() && kw.platforms.length > 0 && kw.queryCount > 0
    );

    if (validKeywords.length === 0) {
      alert("至少需要一个有效的关键词配置");
      return;
    }

    setIsSubmitting(true);

    try {
      const response = await apiFetch("/api/batch-test/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          brand: brand.trim(),
          inspect,
          keywords: validKeywords.map((kw) => ({
            keyword: kw.keyword.trim(),
            platforms: kw.platforms,
            deep_think_platforms: kw.deepThinkPlatforms,
            query_count: kw.queryCount,
          })),
        }),
      });

      const data = await response.json();

      if (data.ok) {
        onStart(data.batch_id);
        onOpenChange(false);
        setBrand("");
        setInspect(false);
        setKeywords([{ keyword: "", platforms: [], deepThinkPlatforms: [], queryCount: 5 }]);
      } else {
        alert(data.message || "启动失败");
      }
    } catch (error) {
      console.error("启动批量测试失败:", error);
      alert("启动失败，请重试");
    } finally {
      setIsSubmitting(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[110] flex items-center justify-center">
      <div
        className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-200"
        onClick={() => onOpenChange(false)}
      />

      <div className="relative bg-white rounded-2xl shadow-xl border border-gray-200 w-full max-w-[900px] max-h-[85vh] overflow-hidden animate-in zoom-in-95 duration-200 mx-4 flex flex-col">
        <button
          onClick={() => onOpenChange(false)}
          className="absolute top-4 right-4 p-1.5 text-gray-400 hover:text-gray-900 hover:bg-gray-100 rounded-lg transition-colors z-10"
        >
          <X className="w-4 h-4" />
        </button>

        {/* Header */}
        <div className="px-8 pt-8 pb-6 pr-14 border-b border-gray-200/70">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-2 mb-1.5">
                <BarChart2 className="w-5 h-5 text-blue-600" />
                <h1 className="app-wordmark-heading text-[20px]">品牌展示率批量测试</h1>
              </div>
              <span className="text-[12px] font-medium text-gray-500 tracking-wide">
                配置品牌、关键词和平台进行批量测试，统计品牌展示率和媒体抓取情况
              </span>
            </div>

            <button
              type="button"
              role="switch"
              aria-checked={inspect}
              onClick={() => setInspect((value) => !value)}
              className="flex h-8 shrink-0 items-center gap-2 border-b border-gray-200 px-0 text-left transition-colors hover:border-gray-900"
            >
              <Monitor className={`h-3.5 w-3.5 shrink-0 ${inspect ? "text-blue-600" : "text-gray-400"}`} />
              <span className={`text-[11px] font-bold ${inspect ? "text-gray-900" : "text-gray-500"}`}>
                有头浏览器
              </span>
              <span
                className={`ml-1 flex h-3.5 w-7 shrink-0 items-center rounded-full p-[2px] transition-colors ${
                  inspect ? "bg-blue-600" : "bg-gray-200"
                }`}
              >
                <span className={`h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${inspect ? "translate-x-[14px]" : "translate-x-0"}`} />
              </span>
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-8 py-6 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent">
          {/* 品牌名称 */}
          <div className="mb-8">
            <div className="flex items-center gap-2 mb-4">
              <div className="w-1 h-3.5 bg-blue-600 rounded-full"></div>
              <h2 className="text-[14px] font-bold text-gray-900 tracking-tight">品牌名称</h2>
              <div className="h-px flex-1 bg-gray-100 ml-2"></div>
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-[11px] font-bold text-gray-500">品牌</label>
              <input
                type="text"
                value={brand}
                onChange={(e) => setBrand(e.target.value)}
                placeholder="输入品牌名称..."
                className="w-full bg-transparent border-0 border-b border-gray-200 rounded-none text-[13px] px-0 py-2 outline-none focus:border-gray-900 transition-colors text-gray-800 placeholder-gray-400"
              />
            </div>
          </div>

          {/* 关键词配置 */}
          <div>
            <div className="flex items-center gap-2 mb-4">
              <div className="w-1 h-3.5 bg-blue-600 rounded-full"></div>
              <h2 className="text-[14px] font-bold text-gray-900 tracking-tight">关键词配置</h2>
              <div className="h-px flex-1 bg-gray-100 ml-2"></div>
              <button
                onClick={addKeyword}
                className="flex items-center gap-1.5 px-0 py-1 text-[11px] font-bold text-gray-700 hover:text-gray-900 transition-colors"
              >
                <Plus className="w-3.5 h-3.5" />
                添加关键词
              </button>
            </div>

            <div className="space-y-6">
              {keywords.map((kw, idx) => (
                <div key={idx} className="border-b border-gray-200/80 pb-6 last:border-0">
                  <div className="grid grid-cols-1 lg:grid-cols-[120px_minmax(0,1fr)] gap-5">
                    {/* 左侧标签 */}
                    <div className="flex items-start gap-3">
                      <div className="w-9 h-9 rounded-lg bg-blue-50 flex items-center justify-center font-black text-[14px] text-blue-600">
                        {idx + 1}
                      </div>
                      <div className="min-w-0">
                        <div className="text-[14px] font-bold text-gray-900 leading-none">关键词 {idx + 1}</div>
                        <div className="mt-1 text-[10px] text-gray-400 font-medium">
                          {kw.platforms.length} 平台 · {kw.queryCount} 次
                        </div>
                        {keywords.length > 1 && (
                          <button
                            onClick={() => removeKeyword(idx)}
                            className="mt-3 inline-flex items-center gap-1 text-[11px] text-red-600 font-bold hover:text-red-700 transition-colors"
                          >
                            <Trash2 className="w-3 h-3" />
                            删除
                          </button>
                        )}
                      </div>
                    </div>

                    {/* 右侧配置 */}
                    <div className="space-y-5">
                      {/* 关键词输入 */}
                      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_140px] gap-5 items-start">
                        <div className="flex flex-col gap-1.5">
                          <label className="text-[11px] font-bold text-gray-500 flex items-center gap-1">
                            <Target className="w-3 h-3" /> 关键词
                          </label>
                          <input
                            type="text"
                            value={kw.keyword}
                            onChange={(e) => updateKeyword(idx, "keyword", e.target.value)}
                            placeholder="输入搜索关键词..."
                            className="w-full bg-transparent border-0 border-b border-gray-200 rounded-none text-[12px] px-0 py-2 outline-none focus:border-gray-900 transition-colors text-gray-800 placeholder-gray-400"
                          />
                        </div>

                        <div className="flex flex-col gap-1.5">
                          <label className="text-[11px] font-bold text-gray-500">查询次数</label>
                          <input
                            type="number"
                            min="1"
                            max="50"
                            value={kw.queryCount}
                            onChange={(e) => updateKeyword(idx, "queryCount", parseInt(e.target.value) || 1)}
                            className="w-full bg-transparent border-0 border-b border-gray-200 rounded-none text-[12px] px-0 py-2 outline-none focus:border-gray-900 transition-colors text-gray-800"
                          />
                        </div>
                      </div>

                      {/* 平台选择 */}
                      <div className="flex flex-col gap-1.5">
                        <label className="text-[11px] font-bold text-gray-500 flex items-center gap-1">
                          <Layers className="w-3 h-3" /> 选择平台
                        </label>
                        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 border-b border-gray-200 py-2">
                          {PLATFORMS.map((platform) => {
                            const isActive = kw.platforms.includes(platform.id);
                            const isDeepThink = kw.deepThinkPlatforms.includes(platform.id);
                            return (
                              <div
                                key={platform.id}
                                className={`grid grid-cols-[minmax(0,1fr)_1px_auto] items-center gap-2 pl-0.5 pr-2 py-1 transition-all min-w-0 ${
                                  isActive ? 'bg-transparent text-gray-900' : 'bg-transparent opacity-60'
                                }`}
                              >
                                <div
                                  className="flex items-center gap-1.5 cursor-pointer min-w-0 pr-1.5"
                                  onClick={() => togglePlatform(idx, platform.id)}
                                >
                                  <div className={`w-1.5 h-1.5 rounded-full transition-colors ${isActive ? 'bg-[var(--brand-cyan)]' : 'bg-gray-300'}`} />
                                  <span className={`text-[11px] font-bold transition-colors ${isActive ? 'text-gray-900' : 'text-gray-500'}`}>{platform.label}</span>
                                </div>

                                <div className="w-px h-3.5 bg-gray-200 self-center"></div>

                                <div
                                  className={`flex items-center gap-1 shrink-0 pl-1.5 justify-self-end ${isActive ? 'cursor-pointer' : 'cursor-not-allowed'}`}
                                  onClick={() => isActive && toggleDeepThink(idx, platform.id)}
                                >
                                  <Brain className={`w-3.5 h-3.5 transition-colors ${isDeepThink && isActive ? 'text-[var(--brand-cyan)]' : 'text-gray-300'}`} />
                                  <div
                                    className={`w-6 h-3 rounded-full flex items-center p-[2px] transition-colors ${
                                      !isActive ? 'bg-gray-200' : isDeepThink ? 'bg-[var(--brand-cyan)]' : 'bg-gray-200'
                                    }`}
                                  >
                                    <div className={`w-2 h-2 rounded-full bg-white transition-transform shadow-sm ${isDeepThink && isActive ? 'translate-x-[12px]' : 'translate-x-0'}`} />
                                  </div>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="px-8 py-6 border-t border-gray-200/70 flex justify-end gap-3">
          <button
            onClick={() => onOpenChange(false)}
            disabled={isSubmitting}
            className="px-0 py-2 text-[12px] font-bold text-gray-700 hover:text-gray-900 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            取消
          </button>
          <button
            onClick={handleStart}
            disabled={isSubmitting || !brand.trim()}
            className="px-0 py-2 text-[12px] font-bold text-blue-600 hover:text-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isSubmitting ? "启动中..." : "启动测试"}
          </button>
        </div>
      </div>
    </div>
  );
}
