import { useState, useEffect, useCallback } from "react";
import { Key, Plus, X, CheckCircle2, AlertCircle, Loader2, Eye, EyeOff, Server, Link2, Save } from "lucide-react";
import { fetchPlatformKeys, savePlatformConfig, testPlatformConnection, type PlatformKeyInfo } from "../lib/backend";
import { notifySaveSuccess } from "../lib/saveToast";

type PlatformConfig = {
  id: string;
  name: string;
  logo: string;
  logoColor: string;
  region: "domestic" | "international";
  defaultModels: string[];
  apiKey?: string;
};

const initialPlatforms: PlatformConfig[] = [
  // ���内
  { id: "deepseek", name: "DeepSeek", logo: "DS", logoColor: "bg-blue-50 text-blue-600", region: "domestic", defaultModels: [] },
  { id: "doubao", name: "豆包", logo: "DB", logoColor: "bg-indigo-50 text-indigo-600", region: "domestic", defaultModels: [] },
  { id: "kimi", name: "Kimi", logo: "KM", logoColor: "bg-emerald-50 text-emerald-600", region: "domestic", defaultModels: [] },
  { id: "qwen", name: "通义千问", logo: "QW", logoColor: "bg-purple-50 text-purple-600", region: "domestic", defaultModels: [] },
  { id: "ernie", name: "文心一言", logo: "EN", logoColor: "bg-blue-50 text-blue-500", region: "domestic", defaultModels: [] },
  { id: "yuanbao", name: "腾讯元宝", logo: "YB", logoColor: "bg-teal-50 text-teal-600", region: "domestic", defaultModels: [] },
  // 国际
  { id: "chatgpt", name: "ChatGPT", logo: "CG", logoColor: "bg-green-50 text-green-600", region: "international", defaultModels: [] },
  { id: "claude", name: "Claude", logo: "CL", logoColor: "bg-orange-50 text-orange-600", region: "international", defaultModels: [] },
  { id: "gemini", name: "Gemini", logo: "GM", logoColor: "bg-blue-50 text-blue-600", region: "international", defaultModels: [] },
  { id: "perplexity", name: "Perplexity", logo: "PP", logoColor: "bg-gray-100 text-gray-900", region: "international", defaultModels: [] },
];

type PlatformState = {
  apiKey: string;
  models: string[];
  testStatus: "idle" | "testing" | "success" | "error";
  dirty: boolean;
};

export function APIContent({
  onSaved,
  onSaveSuccess,
}: {
  onSaved?: () => Promise<void> | void;
  onSaveSuccess?: (message?: string) => void;
}) {
  const domestic = initialPlatforms.filter(p => p.region === "domestic");
  const international = initialPlatforms.filter(p => p.region === "international");

  const [platformStates, setPlatformStates] = useState<Record<string, PlatformState>>(() => {
    const init: Record<string, PlatformState> = {};
    for (const p of initialPlatforms) {
      init[p.id] = { apiKey: "", models: [...p.defaultModels], testStatus: "idle", dirty: false };
    }
    return init;
  });
  const [saving, setSaving] = useState(false);
  const dirtyCount = Object.values(platformStates).filter((state) => state.dirty).length;

  useEffect(() => {
    fetchPlatformKeys().then((keys) => {
      setPlatformStates((prev) => {
        const next = { ...prev };
        for (const [id, info] of Object.entries(keys)) {
          if (next[id]) {
            const mergedModels = [...info.model_options];
            if (info.api_model && !mergedModels.includes(info.api_model)) {
              mergedModels.unshift(info.api_model);
            }
            next[id] = {
              ...next[id],
              apiKey: info.has_key ? info.api_key_masked : "",
              models: mergedModels,
              testStatus: info.has_key ? (info.api_test_status || "idle") : "idle",
              dirty: false,
            };
          }
        }
        return next;
      });
    });
  }, []);

  const updatePlatformState = useCallback((id: string, patch: Partial<PlatformState>) => {
    setPlatformStates((prev) => ({
      ...prev,
      [id]: {
        ...prev[id],
        ...patch,
        testStatus: ("apiKey" in patch || "models" in patch) ? "idle" : (patch.testStatus ?? prev[id].testStatus),
        dirty: true,
      },
    }));
  }, []);

  const setTestStatus = useCallback((id: string, status: PlatformState["testStatus"]) => {
    setPlatformStates((prev) => ({
      ...prev,
      [id]: { ...prev[id], testStatus: status },
    }));
  }, []);

  const handleSaveAll = async () => {
    if (saving || dirtyCount === 0) {
      return;
    }
    setSaving(true);
    const payload: Record<string, { api_key?: string; api_model?: string; model_options?: string[] }> = {};
    for (const [id, state] of Object.entries(platformStates)) {
      if (state.dirty) {
        payload[id] = {
          api_key: state.apiKey,
          api_model: state.models[0] || "",
          model_options: state.models,
        };
      }
    }
    const result = await savePlatformConfig(payload);
    setPlatformStates((prev) => {
      const next = { ...prev };
      for (const id of Object.keys(payload)) {
        next[id] = { ...next[id], dirty: false };
      }
      return next;
    });
    if (result.ok) {
      void onSaved?.();
      notifySaveSuccess(onSaveSuccess, "保存成功");
    }
    setSaving(false);
  };

  return (
    <div className="flex-1 h-full overflow-hidden bg-transparent px-8 py-8 xl:px-10 flex flex-col">
      {/* Header */}
      <div className="flex justify-between items-end border-b border-gray-200/70 pb-6 mb-6 shrink-0">
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <Server className="w-5 h-5 text-blue-600" />
            <h1 className="app-wordmark-heading text-[20px]">API 配置</h1>
          </div>
          <span className="text-[12px] font-medium text-gray-500 tracking-wide">管理各平台大模型密钥与可用模型列表</span>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={handleSaveAll}
            disabled={saving || dirtyCount === 0}
            className="flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-900 transition-colors hover:text-black disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Save className="w-4 h-4" /> {saving ? "保存中..." : dirtyCount > 0 ? `保存 ${dirtyCount} 项修改` : "暂无修改"}
          </button>
        </div>
      </div>

      {/* Content Grid */}
      <div className="flex-1 overflow-y-auto pr-2 -mr-2 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent pb-10">

        {/* 国内模型 */}
        <div className="mb-8">
          <div className="flex items-center gap-2 mb-4">
            <div className="w-1 h-3.5 bg-blue-600 rounded-full"></div>
            <h2 className="text-[14px] font-bold text-gray-900 tracking-tight">国内大模型</h2>
            <div className="h-px flex-1 bg-gray-100 ml-2"></div>
          </div>
          <div className="space-y-1">
            {domestic.map(platform => (
              <PlatformCard
                key={platform.id}
                platform={platform}
                state={platformStates[platform.id]}
                onUpdate={(patch) => updatePlatformState(platform.id, patch)}
                onSetTestStatus={(s) => setTestStatus(platform.id, s)}
              />
            ))}
          </div>
        </div>

        {/* 国际模型 */}
        <div className="mb-4">
          <div className="flex items-center gap-2 mb-4">
            <div className="w-1 h-3.5 bg-gray-900 rounded-full"></div>
            <h2 className="text-[14px] font-bold text-gray-900 tracking-tight">国际大模型</h2>
            <div className="h-px flex-1 bg-gray-100 ml-2"></div>
          </div>
          <div className="space-y-1">
            {international.map(platform => (
              <PlatformCard
                key={platform.id}
                platform={platform}
                state={platformStates[platform.id]}
                onUpdate={(patch) => updatePlatformState(platform.id, patch)}
                onSetTestStatus={(s) => setTestStatus(platform.id, s)}
              />
            ))}
          </div>
        </div>

      </div>
    </div>
  );
}

function PlatformCard({ platform, state, onUpdate, onSetTestStatus }: {
  platform: PlatformConfig;
  state: PlatformState;
  onUpdate: (patch: Partial<PlatformState>) => void;
  onSetTestStatus: (s: PlatformState["testStatus"]) => void;
}) {
  const { apiKey, models, testStatus } = state;
  const [showKey, setShowKey] = useState(false);
  const [newModel, setNewModel] = useState("");

  const handleAddModel = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && newModel.trim()) {
      e.preventDefault();
      if (!models.includes(newModel.trim())) {
        onUpdate({ models: [...models, newModel.trim()] });
      }
      setNewModel("");
    }
  };

  const removeModel = (modelToRemove: string) => {
    onUpdate({ models: models.filter(m => m !== modelToRemove) });
  };

  const handleTest = async () => {
    if (!apiKey) return;
    onSetTestStatus("testing");
    const result = await testPlatformConnection(platform.id, apiKey, models[0] || "");
    onSetTestStatus(result.ok ? "success" : "error");
  };

  return (
    <div className="border-b border-gray-200/80 py-5">
      <div className="grid grid-cols-1 lg:grid-cols-[160px_minmax(0,1fr)] gap-5">
        <div className="flex items-start gap-3">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center font-black text-[14px] tracking-tighter ${platform.logoColor}`}>
            {platform.logo}
          </div>
          <div className="min-w-0">
            <div className="text-[14px] font-bold text-gray-900 leading-none">{platform.name}</div>
            <div className="mt-1 text-[10px] text-gray-400 font-medium font-mono">{platform.id}</div>
            <div className="mt-3">
              {testStatus === "success" && <span className="inline-flex items-center gap-1 text-[11px] text-green-600 font-bold bg-green-50 px-2 py-1 rounded"><CheckCircle2 className="w-3 h-3" /> 已连接</span>}
              {testStatus === "error" && <span className="inline-flex items-center gap-1 text-[11px] text-red-600 font-bold bg-red-50 px-2 py-1 rounded"><AlertCircle className="w-3 h-3" /> 连接失败</span>}
              {testStatus === "idle" && apiKey && <span className="inline-flex items-center gap-1 text-[11px] text-gray-500 font-bold bg-gray-50 px-2 py-1 rounded">未测试</span>}
              {testStatus === "idle" && !apiKey && <span className="inline-flex items-center gap-1 text-[11px] text-gray-400 font-bold bg-gray-50 px-2 py-1 rounded">未配置</span>}
            </div>
          </div>
        </div>

        <div className="space-y-5">
          <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_140px] gap-5 items-start">
            <div className="flex flex-col gap-1.5">
              <label className="text-[11px] font-bold text-gray-500 flex items-center gap-1"><Key className="w-3 h-3" /> API Key</label>
              <div className="relative flex items-center">
                <input 
                  type={showKey ? "text" : "password"}
                  value={apiKey}
                  onChange={(e) => onUpdate({ apiKey: e.target.value })}
                  placeholder={`输入 ${platform.name} API Key...`}
                  className="w-full bg-transparent border-0 border-b border-gray-200 rounded-none text-[12px] px-0 py-2 outline-none focus:border-gray-900 transition-colors pr-8 font-mono text-gray-800 placeholder-gray-400"
                />
                <button 
                  onClick={() => setShowKey(!showKey)}
                  className="absolute right-0 text-gray-400 hover:text-gray-600 transition-colors"
                >
                  {showKey ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                </button>
              </div>
            </div>

            <div className="flex items-end xl:justify-end">
              <button 
                onClick={handleTest}
                disabled={!apiKey || testStatus === "testing"}
                className={`inline-flex items-center gap-1.5 px-0 py-2 text-[11px] font-bold transition-colors ${
                  !apiKey
                    ? "text-gray-300 cursor-not-allowed"
                    : testStatus === "testing"
                      ? "text-blue-600"
                      : "text-gray-700 hover:text-gray-900"
                }`}
              >
                {testStatus === "testing" ? (
                  <><Loader2 className="w-3.5 h-3.5 animate-spin" /> 测试中...</>
                ) : (
                  <><Link2 className="w-3.5 h-3.5" /> 测试连通性</>
                )}
              </button>
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-[11px] font-bold text-gray-500">支持的模型</label>
            <div className="flex flex-wrap gap-1.5 border-b border-gray-200 py-2 min-h-[38px] items-center">
              {models.map(model => (
                <span key={model} className="flex items-center gap-1 bg-gray-50 text-gray-700 px-1.5 py-0.5 rounded text-[11px] font-mono group/tag transition-colors">
                  {model}
                  <button onClick={() => removeModel(model)} className="text-gray-400 hover:text-red-500 opacity-0 group-hover/tag:opacity-100 transition-opacity">
                    <X className="w-3 h-3" />
                  </button>
                </span>
              ))}
              <input 
                type="text"
                value={newModel}
                onChange={e => setNewModel(e.target.value)}
                onKeyDown={handleAddModel}
                placeholder="输入模型按回车..."
                className="flex-1 min-w-[120px] bg-transparent text-[11px] font-mono outline-none placeholder-gray-400 px-1 text-gray-800"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
