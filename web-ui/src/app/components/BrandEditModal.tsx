import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { X, Plus, Brain, Save, Trash2, Settings2, Check, Map as MapIcon, Globe, ArrowUpRight, Calendar, Download, ChevronDown } from "lucide-react";
import { ChartArea } from "./Charts";
import { createTask, fetchTaskTrend, generateBrandTaskDraft, importKeywordsFromFile, updateTask, type BrandTaskDraft, type CloudAdminTaskSnapshot, type CloudUserSnapshot, type DeletedTaskSnapshot, type TaskFull, type TrendSnapshot } from "../lib/backend";
import { DatePickerField } from "./ui/date-picker-field";

type PlatformState = {
  name: string;
  active: boolean;
  deep: boolean;
};

const DEFAULT_PLATFORMS: PlatformState[] = [
  { name: "豆包", active: true, deep: false },
  { name: "DeepSeek", active: true, deep: true },
  { name: "Kimi", active: false, deep: false },
  { name: "通义千问", active: true, deep: true },
  { name: "文心一言", active: false, deep: false },
  { name: "元宝", active: false, deep: false },
  { name: "ChatGPT", active: true, deep: true },
  { name: "Claude", active: true, deep: true },
  { name: "Gemini", active: false, deep: false },
];

const INDUSTRY_OPTIONS = [
  "科技互联网",
  "人工智能与软件服务",
  "电子信息与通信",
  "教育培训",
  "医疗健康",
  "医药生物",
  "农林牧渔",
  "食品饮料",
  "餐饮服务",
  "旅游文旅与酒店",
  "金融保险",
  "房地产与建筑工程",
  "工业制造",
  "汽车与交通出行",
  "能源电力",
  "环保与新能源",
  "化工与新材料",
  "消费零售",
  "电商与本地生活",
  "服饰鞋包",
  "美妆个护",
  "母婴宠物",
  "家居家电",
  "文化传媒",
  "广告营销",
  "娱乐体育",
  "游戏动漫",
  "物流供应链",
  "企业服务",
  "法律财税与咨询",
  "人力资源",
  "政府公共服务",
  "公益社会组织",
  "安全安防",
  "其他行业",
];

type Keyword = {
  id: string;
  text: string;
  mode: "browser" | "recognition" | "api" | "smart";
  customConfig: boolean;
  platforms: PlatformState[];
};

type ImportNotice = {
  tone: "success" | "error";
  message: string;
};

type SaveCallbackOptions = {
  notify?: boolean;
  message?: string;
};

export function BrandEditModal({
  brandName,
  brand,
  existingTasks = [],
  deletedTasks = [],
  currentDetectionMode = "smart",
  onClose,
  onSave,
  isNew = false,
  initialInstruction = "",
  cloudAdminEnabled = false,
  cloudOperators = [],
  initialOperatorUserId = 0,
  onCloudSync,
  canDelete = false,
  onDeleteClick,
  onRestoreDeletedTask,
}: {
  brandName?: string,
  brand?: TaskFull,
  existingTasks?: TaskFull[],
  deletedTasks?: DeletedTaskSnapshot[],
  currentDetectionMode?: "browser" | "recognition" | "api" | "smart",
  onClose: () => void,
  onSave?: (task?: TaskFull, cloudTask?: CloudAdminTaskSnapshot, options?: SaveCallbackOptions) => void | Promise<void>,
  isNew?: boolean,
  initialInstruction?: string,
  cloudAdminEnabled?: boolean,
  cloudOperators?: CloudUserSnapshot[],
  initialOperatorUserId?: number,
  onCloudSync?: (localTaskId: string, operatorUserId?: number) => Promise<{ ok: boolean; message?: string; task?: CloudAdminTaskSnapshot; localTask?: TaskFull }>,
  canDelete?: boolean,
  onDeleteClick?: () => void,
  onRestoreDeletedTask?: (deletedTaskId: string, brandName: string) => Promise<{ ok: boolean; message?: string }>,
}) {
  const getMostCommonWebhook = (tasks: TaskFull[]) => {
    const counts = new Map<string, number>();
    const order: string[] = [];
    for (const taskItem of tasks) {
      const webhookUrl = String(taskItem?.webhook_url || "").trim();
      if (!webhookUrl || webhookUrl.includes("YOUR_KEY_HERE")) {
        continue;
      }
      if (!counts.has(webhookUrl)) {
        order.push(webhookUrl);
      }
      counts.set(webhookUrl, (counts.get(webhookUrl) || 0) + 1);
    }
    let best = "";
    let bestCount = 0;
    for (const webhookUrl of order) {
      const count = counts.get(webhookUrl) || 0;
      if (count > bestCount) {
        best = webhookUrl;
        bestCount = count;
      }
    }
    return best;
  };

  const toStringArray = (value: unknown): string[] => {
    if (Array.isArray(value)) {
      return value.map((item) => String(item || "").trim()).filter(Boolean);
    }
    const text = String(value || "").trim();
    if (!text) {
      return [];
    }
    return text.split(",").map((item) => item.trim()).filter(Boolean);
  };

  const toNumberArray = (value: unknown): number[] => {
    if (!Array.isArray(value)) {
      return [];
    }
    return value
      .map((item) => Number(item))
      .filter((item) => Number.isFinite(item));
  };

  const countActivePlatforms = (platforms: PlatformState[]) => platforms.filter((item) => item.active).length;

  const computeDefaultScreenshotCount = (keywordItems: Keyword[], globalItems: PlatformState[]) => {
    if (!keywordItems.length) {
      return 1;
    }
    const globalCount = countActivePlatforms(globalItems);
    const total = keywordItems.reduce((sum, keywordItem) => {
      const platformCount = keywordItem.customConfig
        ? countActivePlatforms(keywordItem.platforms)
        : globalCount;
      return sum + Math.max(0, platformCount);
    }, 0);
    return Math.max(1, total || globalCount || 1);
  };

  const defaultWebhook = getMostCommonWebhook(existingTasks);
  const brandIndustryTags = toStringArray(brand?.industry_tags);
  const brandRegionTags = toStringArray(brand?.region_tags);
  const brandRecognitionAliases = Array.isArray(brand?.recognition_brands)
    ? brand.recognition_brands.map((item) => String(item || "").trim()).filter(Boolean).join(", ")
    : String(brand?.recognition_brands || "").trim();
  const brandPlatforms = toStringArray(brand?.platforms);
  const brandWeekdays = toNumberArray(brand?.weekdays);
  const brandKeywords = Array.isArray(brand?.keywords) ? brand.keywords : [];
  const [saving, setSaving] = useState(false);
  const [draftInstruction, setDraftInstruction] = useState(initialInstruction);
  const [draftNotes, setDraftNotes] = useState<string[]>([]);
  const [draftMissingInfo, setDraftMissingInfo] = useState<string[]>([]);
  const [draftError, setDraftError] = useState("");
  const [isGeneratingDraft, setIsGeneratingDraft] = useState(false);
  const [name, setName] = useState(brand?.name ?? (isNew ? "" : (brandName || "")));
  const [aliases, setAliases] = useState(brandRecognitionAliases || (isNew ? "" : ""));
  const [industry, setIndustry] = useState(brandIndustryTags.join(", ") || (isNew ? "" : ""));
  const [regions, setRegions] = useState(brandRegionTags.join(", ") || (isNew ? "" : ""));
  const [webhook, setWebhook] = useState(brand?.webhook_url ?? (isNew ? defaultWebhook : ""));
  const [webhookEdited, setWebhookEdited] = useState(Boolean(brand?.webhook_url));
  const [cloudOperatorUserId, setCloudOperatorUserId] = useState(Number(initialOperatorUserId || 0));
  const [saveNotice, setSaveNotice] = useState<ImportNotice | null>(null);
  const [ignoredRestoreCandidateId, setIgnoredRestoreCandidateId] = useState("");
  const [restoringDeletedTask, setRestoringDeletedTask] = useState(false);

  // Optimization Time Period
  const [startDate, setStartDate] = useState(brand?.optimization_start_date ?? (isNew ? "" : ""));
  const [endDate, setEndDate] = useState(brand?.optimization_end_date ?? (isNew ? "" : ""));
  const initDuration = (() => {
    if (brand?.optimization_start_date && brand?.optimization_end_date) {
      const d1 = new Date(brand.optimization_start_date);
      const d2 = new Date(brand.optimization_end_date);
      const diff = Math.round((d2.getTime() - d1.getTime()) / (1000 * 3600 * 24));
      return diff > 0 ? diff : 0;
    }
    return 0;
  })();
  const [duration, setDuration] = useState(initDuration);

  const calculateDays = (start: string, end: string) => {
    if (!start || !end) return 0;
    const d1 = new Date(start);
    const d2 = new Date(end);
    return Math.round((d2.getTime() - d1.getTime()) / (1000 * 3600 * 24));
  };

  const addDays = (date: string, days: number) => {
    if (!date) return "";
    const d = new Date(date);
    d.setDate(d.getDate() + days);
    return d.toISOString().split('T')[0];
  };

  const handleStartDateChange = (newStart: string) => {
    setStartDate(newStart);
    if (endDate) {
      const diff = calculateDays(newStart, endDate);
      setDuration(diff > 0 ? diff : 0);
    }
  };

  const handleEndDateChange = (newEnd: string) => {
    setEndDate(newEnd);
    if (startDate) {
      const diff = calculateDays(startDate, newEnd);
      setDuration(diff > 0 ? diff : 0);
    }
  };

  const handleDurationChange = (e: ChangeEvent<HTMLInputElement>) => {
    const newDuration = parseInt(e.target.value, 10) || 0;
    setDuration(newDuration);
    if (startDate) {
      setEndDate(addDays(startDate, newDuration));
    }
  };
  
  // Schedule
  const initDays = (() => {
    if (brandWeekdays.length > 0) {
      // weekdays from backend are 0-6 (Mon=0..Sun=6) or 1-7 depending on backend
      // Map to boolean array [Mon..Sun]
      const arr = [false, false, false, false, false, false, false];
      for (const d of brandWeekdays) {
        // Backend uses 0-6 for Mon-Sun
        if (d >= 0 && d < 7) arr[d] = true;
      }
      return arr;
    }
    return isNew ? [true, true, true, true, true, true, true] : [true, true, true, true, true, true, true];
  })();
  const [days, setDays] = useState(initDays);
  const dayNames = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

  // Global Platforms
  const PLATFORM_ID_TO_NAME: Record<string, string> = {
    doubao: "豆包", deepseek: "DeepSeek", kimi: "Kimi", tongyi: "通义千问",
    wenxin: "文心一言", yuanbao: "元宝", chatgpt: "ChatGPT", claude: "Claude", gemini: "Gemini",
  };
  const PLATFORM_NAME_TO_ID: Record<string, string> = Object.fromEntries(
    Object.entries(PLATFORM_ID_TO_NAME).map(([id, label]) => [label, id]),
  );
  const MODE_LABELS: Record<Keyword["mode"], string> = {
    browser: "抓取模式",
    recognition: "识别模式",
    api: "接口模式",
    smart: "智能模式",
  };
  const globalModeLabel = MODE_LABELS[currentDetectionMode];
  const initPlatforms = (() => {
    if (brandPlatforms.length > 0) {
      // 后端可能存 ID（"doubao"）或显示名（"豆包"），统一转为显示名后匹配
      const activePlatformNames = new Set(
        brandPlatforms.map(p => PLATFORM_ID_TO_NAME[p] || p)
      );
      // 从所有关键词的 deep_think 聚合出全局 deep 状态（取并集）
      const globalDeepSet = new Set<string>();
      for (const kw of brandKeywords) {
        for (const [pname, val] of Object.entries(kw.deep_think || {})) {
          if (val) globalDeepSet.add(PLATFORM_ID_TO_NAME[pname] || pname);
        }
      }
      return DEFAULT_PLATFORMS.map(p => ({
        ...p,
        active: activePlatformNames.has(p.name),
        deep: globalDeepSet.has(p.name),
      }));
    }
    return DEFAULT_PLATFORMS.map(p => ({ ...p }));
  })();
  const [globalPlatforms, setGlobalPlatforms] = useState<PlatformState[]>(initPlatforms);

  // Keywords
  const [keywordInput, setKeywordInput] = useState("");
  const initKeywords: Keyword[] = (() => {
    if (brandKeywords.length > 0) {
      return brandKeywords.map((kw, idx) => {
        const keywordPlatformNames = toStringArray(kw.platforms).map((platform) => PLATFORM_ID_TO_NAME[platform] || platform);
        const keywordDeepThink = Object.fromEntries(
          Object.entries(kw.deep_think || {}).map(([platform, enabled]) => [PLATFORM_ID_TO_NAME[platform] || platform, Boolean(enabled)]),
        );
        const hasCustomPlatforms = keywordPlatformNames.length > 0 &&
          JSON.stringify([...keywordPlatformNames].sort()) !== JSON.stringify([...brandPlatforms.map((platform) => PLATFORM_ID_TO_NAME[platform] || platform)].sort());
        return {
          id: String(idx + 1),
          text: kw.keyword,
          mode: (kw.mode as Keyword["mode"]) || "browser",
          customConfig: hasCustomPlatforms,
          platforms: hasCustomPlatforms
            ? DEFAULT_PLATFORMS.map(p => ({
                ...p,
                active: keywordPlatformNames.includes(p.name),
                deep: keywordDeepThink[p.name] ?? false,
              }))
            : [...initPlatforms],
        };
      });
    }
      return [];
  })();
  const [keywords, setKeywords] = useState<Keyword[]>(initKeywords);
  const [isImportingKeywords, setIsImportingKeywords] = useState(false);
  const [keywordImportNotice, setKeywordImportNotice] = useState<ImportNotice | null>(null);
  const keywordFileInputRef = useRef<HTMLInputElement | null>(null);
  const industryDropdownRef = useRef<HTMLDivElement | null>(null);
  const cloudOperatorDropdownRef = useRef<HTMLDivElement | null>(null);
  const [industryDropdownOpen, setIndustryDropdownOpen] = useState(false);
  const [cloudOperatorDropdownOpen, setCloudOperatorDropdownOpen] = useState(false);

  const clonePlatformStates = (items: PlatformState[]) => items.map((item) => ({ ...item }));

  const buildKeywordItem = (
    text: string,
    indexSeed: string | number,
    platforms: PlatformState[],
    customConfig = false,
  ): Keyword => ({
    id: `${Date.now()}-${indexSeed}`,
    text,
    mode: currentDetectionMode,
    customConfig,
    platforms: clonePlatformStates(platforms),
  });

  const buildPlatformState = (activeIds: string[], deepIds: string[]) => {
    const activeSet = new Set(activeIds);
    const deepSet = new Set(deepIds);
    return DEFAULT_PLATFORMS.map((platform) => {
      const platformId = PLATFORM_NAME_TO_ID[platform.name] || platform.name;
      const active = activeSet.has(platformId);
      return {
        ...platform,
        active,
        deep: active && deepSet.has(platformId),
      };
    });
  };

  const computeDuration = (start: string, end: string) => {
    if (!start || !end) return 0;
    const d1 = new Date(start);
    const d2 = new Date(end);
    const diff = Math.round((d2.getTime() - d1.getTime()) / (1000 * 3600 * 24));
    return diff > 0 ? diff : 0;
  };

  const applyGeneratedDraft = (draft: BrandTaskDraft) => {
    const nextBrandName = draft.brand || draft.name || "";
    const nextTaskName = draft.name || nextBrandName;
    const nextGlobalPlatforms = buildPlatformState(draft.platforms || [], draft.deep_think_platforms || []);

    const globalPlatformSignature = JSON.stringify({
      active: nextGlobalPlatforms.filter((item) => item.active).map((item) => item.name).sort(),
      deep: nextGlobalPlatforms.filter((item) => item.active && item.deep).map((item) => item.name).sort(),
    });

    const nextKeywords: Keyword[] = (draft.keywords || []).map((keyword, index) => {
      const keywordPlatforms = buildPlatformState(keyword.platforms || draft.platforms || [], keyword.deep_think_platforms || []);
      const keywordSignature = JSON.stringify({
        active: keywordPlatforms.filter((item) => item.active).map((item) => item.name).sort(),
        deep: keywordPlatforms.filter((item) => item.active && item.deep).map((item) => item.name).sort(),
      });
      return {
        id: `${Date.now()}-${index}`,
        text: keyword.keyword,
        mode: currentDetectionMode,
        customConfig: keywordSignature !== globalPlatformSignature,
        platforms: keywordPlatforms,
      };
    });

    setName(nextTaskName);
    setAliases((draft.recognition_brands || []).join(", "));
    setIndustry((draft.industry_tags || []).join(", "));
    setRegions((draft.region_tags || []).join(", "));
    setWebhook(draft.webhook_url || defaultWebhook);
    setStartDate(draft.optimization_start_date || "");
    setEndDate(draft.optimization_end_date || "");
    setDuration(computeDuration(draft.optimization_start_date || "", draft.optimization_end_date || ""));
    setDays([0, 1, 2, 3, 4, 5, 6].map((index) => (draft.weekdays || []).includes(index)));
    setGlobalPlatforms(nextGlobalPlatforms);
    setKeywords(nextKeywords);
    setTaskEnabled(Boolean(draft.enabled));
    setCheckMode(Boolean(draft.inspect));
    setScreenshotCount(computeDefaultScreenshotCount(nextKeywords, nextGlobalPlatforms));
    setWebhookEdited(Boolean(draft.webhook_url));
    setScreenshotCountEdited(false);
    setExtractReferencesEnabled(Boolean(draft.extract_references_enabled));
    setFixedScreenshotMode(Boolean(draft.fixed_screenshot_enabled));
    setFixedScreenshotCount(Math.max(1, draft.fixed_screenshot_count || 1));
    setDraftNotes(draft.notes || []);
    setDraftMissingInfo(draft.missing_info || []);
    if (!nextTaskName && nextBrandName) {
      setName(nextBrandName);
    }
  };

  // Task Settings
  const [taskEnabled, setTaskEnabled] = useState(brand?.enabled ?? (isNew ? false : true));
  const [checkMode, setCheckMode] = useState(brand?.inspect ?? false);
  const [screenshotCount, setScreenshotCount] = useState(
    brand?.recognition_batch_size ?? (isNew ? computeDefaultScreenshotCount(initKeywords, initPlatforms) : 3)
  );
  const [screenshotCountEdited, setScreenshotCountEdited] = useState(Boolean(brand?.recognition_batch_size));
  const [extractReferencesEnabled, setExtractReferencesEnabled] = useState(brand?.extract_references_enabled ?? false);
  const [fixedScreenshotMode, setFixedScreenshotMode] = useState(brand?.fixed_screenshot_enabled ?? false);
  const [fixedScreenshotCount, setFixedScreenshotCount] = useState(brand?.fixed_screenshot_count ?? (isNew ? 3 : (brand?.recognition_batch_size ?? 3)));
  
  // Reports
  const [weeklyReport, setWeeklyReport] = useState(isNew ? false : true);
  const [monthlyReport, setMonthlyReport] = useState(isNew ? false : true);

  useEffect(() => {
    if (!isNew || webhookEdited) {
      return;
    }
    setWebhook(defaultWebhook);
  }, [defaultWebhook, isNew, webhookEdited]);

  useEffect(() => {
    if (!isNew || screenshotCountEdited) {
      return;
    }
    setScreenshotCount(computeDefaultScreenshotCount(keywords, globalPlatforms));
  }, [globalPlatforms, isNew, keywords, screenshotCountEdited]);

  useEffect(() => {
    if (!keywordImportNotice) {
      return;
    }
    const timer = window.setTimeout(() => setKeywordImportNotice(null), 3200);
    return () => window.clearTimeout(timer);
  }, [keywordImportNotice]);

  const handleAddKeyword = () => {
    if (keywordInput.trim() && !keywords.find(k => k.text === keywordInput.trim())) {
      setKeywords([...keywords, buildKeywordItem(keywordInput.trim(), "manual", globalPlatforms)]);
      setKeywordInput("");
    }
  };

  const handleKeywordFileImport = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null;
    event.target.value = "";
    if (!file || isImportingKeywords) {
      return;
    }
    setIsImportingKeywords(true);
    setKeywordImportNotice(null);
    try {
      const result = await importKeywordsFromFile(file);
      if (!result.ok || !result.keywords?.length) {
        setKeywordImportNotice({
          tone: "error",
          message: result.message || "关键词导入失败",
        });
        return;
      }

      const existingTexts = new Set(keywords.map((item) => item.text.trim().toLowerCase()).filter(Boolean));
      const imported = result.keywords.map((item) => item.trim()).filter(Boolean);
      const uniqueNewKeywords: string[] = [];
      let duplicateCount = 0;
      for (const keyword of imported) {
        const key = keyword.toLowerCase();
        if (existingTexts.has(key) || uniqueNewKeywords.some((item) => item.toLowerCase() === key)) {
          duplicateCount += 1;
          continue;
        }
        uniqueNewKeywords.push(keyword);
      }

      if (!uniqueNewKeywords.length) {
        setKeywordImportNotice({
          tone: "error",
          message: `文件里的 ${imported.length} 个关键词已存在，没有新增内容`,
        });
        return;
      }

      const importedKeywords = uniqueNewKeywords.map((keyword, index) =>
        buildKeywordItem(keyword, `import-${index}`, globalPlatforms),
      );
      setKeywords((current) => [...current, ...importedKeywords]);
      setKeywordImportNotice({
        tone: "success",
        message: `已导入 ${uniqueNewKeywords.length} 个关键词，默认跟随全局平台配置${duplicateCount ? `，已跳过重复 ${duplicateCount} 个` : ""}`,
      });
    } catch (error) {
      setKeywordImportNotice({
        tone: "error",
        message: error instanceof Error ? error.message : "关键词导入失败",
      });
    } finally {
      setIsImportingKeywords(false);
    }
  };

  const handleSave = async () => {
    if (saving) return;
    setSaving(true);
    setSaveNotice(null);
    try {
      const activePlatformNames = globalPlatforms.filter(p => p.active).map(p => p.name);
      const weekdays = days.map((active, idx) => active ? idx : -1).filter(d => d >= 0);
      const industryTags = industry ? industry.split(",").map(s => s.trim()).filter(Boolean) : [];
      const regionTags = regions ? regions.split(",").map(s => s.trim()).filter(Boolean) : [];

      const keywordsPayload = keywords.map(kw => {
        const kwPlatforms = kw.customConfig
          ? kw.platforms.filter(p => p.active).map(p => p.name)
          : activePlatformNames;
        const deepThink: Record<string, boolean> = {};
        const platforms = kw.customConfig ? kw.platforms : globalPlatforms;
        for (const p of platforms) {
          if (p.active && p.deep) {
            deepThink[p.name] = true;
          }
        }
        return {
          keyword: kw.text,
          brand: name,
          platforms: kwPlatforms,
          mode: currentDetectionMode,
          deep_think: deepThink,
        };
      });

      const taskPayload: Record<string, unknown> = {
        name,
        brand: name,
        keywords: keywordsPayload,
        platforms: activePlatformNames,
        webhook_url: webhook,
        weekdays,
        enabled: taskEnabled,
        industry_tags: industryTags,
        region_tags: regionTags,
        optimization_start_date: startDate,
        optimization_end_date: endDate,
        inspect: checkMode,
        recognition_enabled: true,
        recognition_brands: aliases,
        recognition_batch_size: Math.max(1, screenshotCount || 1),
        extract_references_enabled: extractReferencesEnabled,
        fixed_screenshot_enabled: fixedScreenshotMode,
        fixed_screenshot_count: Math.max(1, fixedScreenshotCount || 1),
      };

      let localTaskId = brand?.id || "";
      let localResult: { ok: boolean; task_id?: string; task?: TaskFull; message?: string };
      if (isNew) {
        localResult = await createTask(taskPayload);
        localTaskId = localResult.task_id || "";
      } else if (brand?.id) {
        localResult = await updateTask(brand.id, taskPayload);
      } else {
        localResult = { ok: false, message: "缺少本地任务 ID" };
      }
      if (!localResult.ok) {
        setSaveNotice({ tone: "error", message: localResult.message || "本地品牌保存失败" });
        return;
      }

      const cloudSync = onCloudSync;
      const shouldSyncCloud = Boolean(cloudAdminEnabled && cloudSync && localTaskId);
      const savedTask = localResult.task;
      await onSave?.(savedTask, undefined, {
        message: shouldSyncCloud ? "本地保存成功，云端同步中" : "保存成功",
      });
      onClose();
      if (shouldSyncCloud && cloudSync) {
        void (async () => {
          const cloudResult = await cloudSync(localTaskId, cloudOperatorUserId || 0);
          if (!cloudResult.ok) {
            await onSave?.(undefined, undefined, {
              message: cloudResult.message ? `本地已保存，云端同步失败：${cloudResult.message}` : "本地已保存，云端同步失败",
            });
            return;
          }
          await onSave?.(cloudResult.localTask || savedTask, cloudResult.task, {
            notify: Boolean(cloudResult.message),
            message: cloudResult.message || "云端同步完成",
          });
        })();
      }
    } finally {
      setSaving(false);
    }
  };

  const handleGenerateDraft = async () => {
    const instruction = draftInstruction.trim();
    if (!instruction || isGeneratingDraft) {
      return;
    }

    setIsGeneratingDraft(true);
    setDraftError("");
    try {
      const result = await generateBrandTaskDraft(instruction);
      if (!result.ok || !result.draft) {
        setDraftError(result.message || "AI 自动填写失败");
        return;
      }
      applyGeneratedDraft(result.draft);
    } finally {
      setIsGeneratingDraft(false);
    }
  };

  const updateKeywordPlatforms = (kwId: string, updatedPlatforms: PlatformState[]) => {
    setKeywords(keywords.map(kw => kw.id === kwId ? { ...kw, platforms: updatedPlatforms } : kw));
  };

  useEffect(() => {
    if (!industryDropdownOpen) {
      return;
    }
    function handlePointerDown(event: MouseEvent) {
      if (industryDropdownRef.current && !industryDropdownRef.current.contains(event.target as Node)) {
        setIndustryDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [industryDropdownOpen]);

  useEffect(() => {
    if (!cloudOperatorDropdownOpen) {
      return;
    }
    function handlePointerDown(event: MouseEvent) {
      if (cloudOperatorDropdownRef.current && !cloudOperatorDropdownRef.current.contains(event.target as Node)) {
        setCloudOperatorDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [cloudOperatorDropdownOpen]);

  const serializePlatforms = (items: PlatformState[]) => JSON.stringify(
    items.map((item) => ({
      name: item.name,
      active: Boolean(item.active),
      deep: Boolean(item.active && item.deep),
    })),
  );

  const serializeKeywords = (items: Keyword[]) => JSON.stringify(
    items.map((item) => ({
      text: item.text.trim(),
      mode: item.mode,
      customConfig: Boolean(item.customConfig),
      platforms: item.customConfig ? JSON.parse(serializePlatforms(item.platforms)) : [],
    })),
  );

  const initialSerializedState = useMemo(() => {
    return {
      basic: JSON.stringify({
        name: (brand?.name ?? (isNew ? "" : (brandName || ""))).trim(),
        aliases: (brandRecognitionAliases || (isNew ? "" : "")).trim(),
        industry: (brandIndustryTags.join(", ") || (isNew ? "" : "")).trim(),
        regions: (brandRegionTags.join(", ") || (isNew ? "" : "")).trim(),
        webhook: String(brand?.webhook_url ?? (isNew ? defaultWebhook : "")).trim(),
        startDate: String(brand?.optimization_start_date ?? (isNew ? "" : "")).trim(),
        endDate: String(brand?.optimization_end_date ?? (isNew ? "" : "")).trim(),
      }),
      schedule: JSON.stringify(initDays),
      platforms: serializePlatforms(initPlatforms),
      keywords: serializeKeywords(initKeywords),
      runtime: JSON.stringify({
        taskEnabled: Boolean(brand?.enabled ?? (isNew ? false : true)),
        checkMode: Boolean(brand?.inspect ?? false),
        screenshotCount: Number(brand?.recognition_batch_size ?? (isNew ? computeDefaultScreenshotCount(initKeywords, initPlatforms) : 3)),
        extractReferencesEnabled: Boolean(brand?.extract_references_enabled ?? false),
        fixedScreenshotMode: Boolean(brand?.fixed_screenshot_enabled ?? false),
        fixedScreenshotCount: Number(brand?.fixed_screenshot_count ?? (isNew ? 3 : (brand?.recognition_batch_size ?? 3))),
      }),
      cloud: JSON.stringify({
        operatorUserId: Number(initialOperatorUserId || 0),
      }),
    };
  }, [
    brand,
    brandIndustryTags,
    brandName,
    brandRecognitionAliases,
    brandRegionTags,
    defaultWebhook,
    initDays,
    initKeywords,
    initPlatforms,
    initialOperatorUserId,
    isNew,
  ]);

  const dirtySectionCount = useMemo(() => {
    const currentBasic = JSON.stringify({
      name: name.trim(),
      aliases: aliases.trim(),
      industry: industry.trim(),
      regions: regions.trim(),
      webhook: webhook.trim(),
      startDate: startDate.trim(),
      endDate: endDate.trim(),
    });
    const currentSchedule = JSON.stringify(days);
    const currentPlatforms = serializePlatforms(globalPlatforms);
    const currentKeywords = serializeKeywords(keywords);
    const currentRuntime = JSON.stringify({
      taskEnabled: Boolean(taskEnabled),
      checkMode: Boolean(checkMode),
      screenshotCount: Number(screenshotCount || 1),
      extractReferencesEnabled: Boolean(extractReferencesEnabled),
      fixedScreenshotMode: Boolean(fixedScreenshotMode),
      fixedScreenshotCount: Number(fixedScreenshotCount || 1),
    });
    const currentCloud = JSON.stringify({ operatorUserId: Number(cloudOperatorUserId || 0) });

    return [
      currentBasic !== initialSerializedState.basic,
      currentSchedule !== initialSerializedState.schedule,
      currentPlatforms !== initialSerializedState.platforms || currentKeywords !== initialSerializedState.keywords,
      currentRuntime !== initialSerializedState.runtime,
      cloudAdminEnabled && currentCloud !== initialSerializedState.cloud,
    ].filter(Boolean).length;
  }, [
    aliases,
    checkMode,
    cloudAdminEnabled,
    cloudOperatorUserId,
    days,
    extractReferencesEnabled,
    fixedScreenshotCount,
    fixedScreenshotMode,
    globalPlatforms,
    industry,
    initialSerializedState,
    keywords,
    name,
    regions,
    screenshotCount,
    startDate,
    endDate,
    taskEnabled,
    webhook,
  ]);

  const hasDirtyChanges = dirtySectionCount > 0;
  const selectedCloudOperatorLabel = useMemo(() => {
    if (!cloudOperatorUserId) {
      return "暂不分配";
    }
    const selected = cloudOperators.find((user) => Number(user.id || 0) === Number(cloudOperatorUserId));
    const label = String(selected?.display_name || selected?.username || `运营账号 ${cloudOperatorUserId}`).trim();
    return `#${cloudOperatorUserId} · ${label}`;
  }, [cloudOperatorUserId, cloudOperators]);
  const restoreCandidate = useMemo(() => {
    if (!isNew || !name.trim()) {
      return null;
    }
    const target = name.trim().toLocaleLowerCase();
    return (deletedTasks || []).find((item) => {
      if (!item?.can_restore) {
        return false;
      }
      if (ignoredRestoreCandidateId && item.id === ignoredRestoreCandidateId) {
        return false;
      }
      const brand = String(item.brand || "").trim().toLocaleLowerCase();
      const itemName = String(item.name || "").trim().toLocaleLowerCase();
      return brand === target || itemName === target;
    }) || null;
  }, [deletedTasks, ignoredRestoreCandidateId, isNew, name]);

  const handleRestoreDeletedTask = async () => {
    if (!restoreCandidate || restoringDeletedTask || !onRestoreDeletedTask) {
      return;
    }
    setRestoringDeletedTask(true);
    try {
      const result = await onRestoreDeletedTask(restoreCandidate.id, restoreCandidate.brand || restoreCandidate.name || name);
      if (!result.ok) {
        setSaveNotice({ tone: "error", message: result.message || "恢复品牌配置失败" });
        return;
      }
      await onSave?.();
      onClose();
    } finally {
      setRestoringDeletedTask(false);
    }
  };

  const inputClass = "w-full border-0 border-b border-gray-200 bg-transparent px-0 py-2.5 text-[12px] text-gray-900 font-medium outline-none transition-colors placeholder:text-gray-400 focus:border-[var(--brand-navy)]";
  const alignedInputClass = `${inputClass} h-10 py-0 leading-10`;
  const selectorButtonClass = "flex h-10 w-full items-center justify-between gap-3 border-b border-gray-200 bg-transparent px-0 text-left text-[12px] font-medium text-gray-900 outline-none transition-colors hover:border-gray-300 focus:border-[var(--brand-navy)]";
  const operatorButtonClass = "flex h-10 w-full items-center justify-between gap-3 border-b border-gray-200 bg-transparent px-0 text-left text-[12px] font-medium text-gray-900 outline-none transition-colors hover:border-gray-300 focus:border-[var(--brand-navy)]";
  const subtleInputClass = "w-full border-0 border-b border-gray-200 bg-transparent px-0 py-2.5 text-[12px] text-gray-900 font-medium outline-none transition-colors placeholder:text-gray-400 focus:border-[var(--brand-navy)]";
  const helperTextClass = "text-[11px] text-gray-400 leading-5";

  return (
    <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/20 backdrop-blur-[2px] p-6 transition-opacity animate-in fade-in duration-200">
      <div className="w-full max-w-[920px] h-full max-h-[86vh] bg-white rounded-[22px] shadow-[0_24px_56px_-40px_rgba(15,23,42,0.34)] border border-gray-200/70 flex flex-col overflow-hidden animate-in slide-in-from-bottom-4 duration-300">
        
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-5 border-b border-gray-100 shrink-0 bg-white z-10">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <div className="w-2 h-4 bg-[var(--brand-cyan)] rounded-sm"></div>
              <h2 className="text-[18px] font-black text-gray-900 tracking-tight leading-none">
                {isNew ? '新建品牌配置' : '编辑品牌配置'}
              </h2>
              {!isNew && (
                <>
                  <span className="text-[18px] font-bold text-gray-300 leading-none">/</span>
                  <span className="text-[14px] font-bold text-gray-500 tracking-widest uppercase mt-0.5">{brandName}</span>
                </>
              )}
            </div>
          </div>
          <button 
            type="button"
            onClick={onClose}
            className="w-8 h-8 rounded-full flex items-center justify-center text-gray-400 hover:text-gray-900 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Scrollable Body */}
        <div className="flex-1 overflow-y-auto scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent bg-white">
          <div className="p-6 md:p-8 flex flex-col gap-9">

            {isNew && (
              <section className="flex flex-col gap-4">
                <SectionTitle title="AI 品牌接入" />
                <div className="border-b border-slate-200/80 pb-6">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex flex-col gap-1.5">
                      <div className="flex items-center gap-2">
                        <div className="w-8 h-8 rounded-full bg-[var(--brand-navy)] text-white flex items-center justify-center">
                          <Brain className="w-4 h-4" />
                        </div>
                        <div>
                          <h3 className="text-[14px] font-black text-gray-900 tracking-tight">整段需求自动填写</h3>
                          <p className="text-[12px] text-gray-500 font-medium">输入一段完整需求，AI 会识别品牌、关键词、平台和运行设置，并自动填写下面表单。</p>
                        </div>
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={handleGenerateDraft}
                      disabled={isGeneratingDraft || !draftInstruction.trim()}
                      className="shrink-0 flex items-center gap-1.5 px-4 py-2 bg-[var(--brand-navy)] hover:bg-[#102a30] text-white text-[12px] font-bold transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <Brain className="w-3.5 h-3.5" />
                      {isGeneratingDraft ? "填写中..." : "自动填写"}
                    </button>
                  </div>

                  <div className="mt-4 flex flex-col gap-3">
                    <textarea
                      value={draftInstruction}
                      onChange={(event) => setDraftInstruction(event.target.value)}
                      placeholder="例如：帮我新增一个追踪品牌“影石Insta360”，重点看新品传播和口碑评价。品牌词、产品词、竞品对比词都要覆盖，先上豆包、DeepSeek、通义，适合分析型词就开深度思考，工作日自动跑，截图识别也要留好。"
                      className="min-h-[128px] w-full resize-y border-b border-slate-200 bg-slate-50/[0.55] px-4 py-3 text-[13px] text-gray-900 font-medium leading-6 outline-none transition-colors placeholder:text-gray-400 focus:border-[var(--brand-navy)]"
                    />
                    <p className={helperTextClass}>
                      AI 只会自动填写表单，不会直接保存。你可以继续微调关键词、平台和运行配置后再保存。
                    </p>
                    {draftError && (
                      <div className="border-l-2 border-red-300 bg-red-50/70 px-3.5 py-2.5 text-[12px] font-medium text-red-600">
                        {draftError}
                      </div>
                    )}
                    {(draftNotes.length > 0 || draftMissingInfo.length > 0) && (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <div className="border-l-2 border-gray-200 px-4 py-1">
                          <div className="text-[11px] font-bold text-gray-500 tracking-widest uppercase mb-2">AI 判断</div>
                          {draftNotes.length > 0 ? (
                            <div className="flex flex-col gap-1.5">
                              {draftNotes.map((note, index) => (
                                <p key={`${note}-${index}`} className="text-[12px] text-gray-700 leading-5">{note}</p>
                              ))}
                            </div>
                          ) : (
                            <p className="text-[12px] text-gray-400">本次自动填写没有额外说明。</p>
                          )}
                        </div>
                        <div className="border-l-2 border-amber-300 px-4 py-1">
                          <div className="text-[11px] font-bold text-amber-700 tracking-widest uppercase mb-2">建议补充</div>
                          {draftMissingInfo.length > 0 ? (
                            <div className="flex flex-col gap-1.5">
                              {draftMissingInfo.map((item, index) => (
                                <p key={`${item}-${index}`} className="text-[12px] text-amber-800 leading-5">{item}</p>
                              ))}
                            </div>
                          ) : (
                            <p className="text-[12px] text-amber-700">当前信息已经足够先落一个初版任务。</p>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </section>
            )}

            {/* 0. 数据洞察 (仅在编辑时显示) */}
            {!isNew && (
              <section className="flex flex-col gap-4">
                <SectionTitle title="数据洞察" />
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6 border-b border-gray-100 pb-6">
                  <BrandAITrendSection taskId={brand?.id} />
                  <BrandOptimizationMap regions={regions} />
                </div>
              </section>
            )}

            {restoreCandidate && (
              <div className="flex flex-col items-start gap-3 border-l-2 border-amber-300 bg-amber-50/55 px-3.5 py-3 animate-in fade-in slide-in-from-top-2 duration-200 md:flex-row md:items-center md:justify-between">
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-amber-700">可恢复配置</span>
                  <span className="text-[12px] font-medium text-gray-700">
                    是否恢复「{restoreCandidate.brand || restoreCandidate.name}」品牌配置及数据？
                  </span>
                  <span className="text-[11px] font-semibold text-gray-500">
                    删除时间 {restoreCandidate.deleted_at || "未知"} · 保留至 {restoreCandidate.expires_at || "三天内"}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-2 self-end md:self-auto">
                  <button
                    type="button"
                    onClick={() => setIgnoredRestoreCandidateId(restoreCandidate.id)}
                    disabled={restoringDeletedTask}
                    className="inline-flex h-7 items-center gap-1.5 px-2 text-[11px] font-bold text-gray-500 transition-colors hover:text-gray-800 disabled:cursor-not-allowed disabled:text-gray-300"
                  >
                    无视
                  </button>
                  <button
                    type="button"
                    onClick={() => { void handleRestoreDeletedTask(); }}
                    disabled={restoringDeletedTask}
                    className="inline-flex h-7 items-center gap-1.5 px-2 text-[11px] font-bold text-gray-700 transition-colors hover:text-emerald-700 disabled:cursor-not-allowed disabled:text-gray-300"
                  >
                    <Check className={`w-3.5 h-3.5 ${restoringDeletedTask ? "animate-spin" : ""}`} />
                    {restoringDeletedTask ? "恢复中..." : "恢复"}
                  </button>
                </div>
              </div>
            )}
            
            {/* 1. 基本信息 */}
            <section className={`flex flex-col gap-4 ${isNew ? "" : "-mt-1"}`}>
              <SectionTitle title="基本信息" />
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div className="flex flex-col gap-1.5">
                  <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">品牌主名称</label>
                  <input 
                    type="text" 
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder="请输入品牌名称"
                    className={inputClass}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">
                    识别品牌别名 <span className="text-gray-400 font-normal normal-case tracking-normal">(可选，逗号分隔)</span>
                  </label>
                  <input 
                    type="text" 
                    value={aliases}
                    onChange={e => setAliases(e.target.value)}
                    placeholder="例如：TechNova_CN, 科技新星"
                    className={inputClass}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">
                    所属行业
                  </label>
                  <div ref={industryDropdownRef} className="relative">
                    <button
                      type="button"
                      aria-expanded={industryDropdownOpen}
                      onClick={() => setIndustryDropdownOpen((open) => !open)}
                      className={selectorButtonClass}
                    >
                      <span className={`min-w-0 truncate ${industry ? "text-gray-900" : "text-gray-400"}`}>
                        {industry || "请选择所属行业"}
                      </span>
                      <ChevronDown className={`h-4 w-4 shrink-0 text-gray-500 transition-transform ${industryDropdownOpen ? "rotate-180" : ""}`} strokeWidth={2.4} />
                    </button>
                    {industryDropdownOpen && (
                      <div className="absolute left-0 right-0 top-[calc(100%+6px)] z-50 max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white py-1 shadow-[0_16px_36px_-24px_rgba(15,23,42,0.35)]">
                        {INDUSTRY_OPTIONS.map((option) => {
                          const selected = industry === option;
                          return (
                            <button
                              key={option}
                              type="button"
                              onClick={() => {
                                setIndustry(option);
                                setIndustryDropdownOpen(false);
                              }}
                              className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-[12px] font-bold transition-colors hover:bg-gray-50 ${selected ? "text-[var(--brand-navy)]" : "text-gray-700"}`}
                            >
                              <span className="min-w-0 truncate">{option}</span>
                              {selected && <Check className="h-3.5 w-3.5 shrink-0" strokeWidth={2.8} />}
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex flex-col gap-1.5">
                  <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">
                    重点优化区域 <span className="text-gray-400 font-normal normal-case tracking-normal">(可选，逗号分隔)</span>
                  </label>
                  <input 
                    type="text" 
                    value={regions}
                    onChange={e => setRegions(e.target.value)}
                    placeholder="例如：北京, 上海, 广东..."
                    className={inputClass}
                  />
                </div>
                <div className="flex flex-col gap-1.5 md:col-span-2">
                  <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">优化时间周期</label>
                  <div className="flex flex-col sm:flex-row items-center gap-3 border-b border-gray-200 pb-2">
                    <DatePickerField
                      value={startDate}
                      onChange={handleStartDateChange}
                      fromYear={1990}
                      toYear={new Date().getFullYear() + 10}
                      placeholder="开始日期"
                      variant="compact"
                      icon={<Calendar className="w-4 h-4 text-[var(--brand-navy)] shrink-0" />}
                      className="flex-1 w-full"
                      triggerClassName="font-bold tracking-wide"
                    />
                    <span className="text-gray-300 text-[14px] font-bold">-</span>
                    <DatePickerField
                      value={endDate}
                      onChange={handleEndDateChange}
                      fromYear={1990}
                      toYear={new Date().getFullYear() + 10}
                      placeholder="结束日期"
                      variant="compact"
                      icon={<Calendar className="w-4 h-4 text-[var(--brand-navy)] shrink-0" />}
                      className="flex-1 w-full"
                      triggerClassName="font-bold tracking-wide"
                    />
                    <div className="hidden sm:block w-px h-6 bg-gray-200/80 mx-1"></div>
                    <div className="flex items-center w-full sm:w-[140px] border-b border-gray-200 px-2 py-2 focus-within:border-[var(--brand-navy)] transition-colors">
                       <span className="text-gray-400 text-[11px] font-bold mr-2 whitespace-nowrap">总时长</span>
                       <input type="number" min={0} value={duration} onChange={handleDurationChange} className="bg-transparent text-[13px] text-gray-900 font-black outline-none w-full text-right" />
                       <span className="text-gray-500 text-[11px] font-bold ml-1.5">天</span>
                    </div>
                  </div>
                </div>

                <div className={`flex flex-col gap-1.5 ${cloudAdminEnabled ? "" : "md:col-span-2"}`}>
                  <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">企微机器人 Webhook</label>
                  <input 
                    type="text" 
                    value={webhook}
                    onChange={e => {
                      setWebhookEdited(true);
                      setWebhook(e.target.value);
                    }}
                    placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
                    className={`${alignedInputClass} font-mono`}
                  />
                </div>
                {cloudAdminEnabled && (
                  <div className="flex flex-col gap-1.5">
                    <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">归属运营账号</label>
                    <div ref={cloudOperatorDropdownRef} className="relative">
                      <button
                        type="button"
                        aria-expanded={cloudOperatorDropdownOpen}
                        onClick={() => setCloudOperatorDropdownOpen((open) => !open)}
                        className={operatorButtonClass}
                      >
                        <span className="min-w-0 truncate">{selectedCloudOperatorLabel}</span>
                        <ChevronDown className={`h-4 w-4 shrink-0 text-gray-500 transition-transform ${cloudOperatorDropdownOpen ? "rotate-180" : ""}`} strokeWidth={2.4} />
                      </button>
                      {cloudOperatorDropdownOpen && (
                        <div className="absolute left-0 right-0 top-[calc(100%+6px)] z-50 max-h-56 overflow-y-auto rounded-lg border border-gray-200 bg-white py-1 shadow-[0_16px_36px_-24px_rgba(15,23,42,0.35)]">
                          <button
                            type="button"
                            onClick={() => {
                              setCloudOperatorUserId(0);
                              setCloudOperatorDropdownOpen(false);
                            }}
                            className={`flex w-full items-center justify-between px-3 py-2 text-left text-[12px] font-bold transition-colors hover:bg-gray-50 ${cloudOperatorUserId ? "text-gray-700" : "text-[var(--brand-navy)]"}`}
                          >
                            <span>暂不分配</span>
                            {!cloudOperatorUserId && <Check className="h-3.5 w-3.5" />}
                          </button>
                          {cloudOperators.map((user) => {
                            const id = Number(user.id || 0);
                            const label = String(user.display_name || user.username || `运营账号 ${id}`).trim();
                            const selected = Number(cloudOperatorUserId) === id;
                            return (
                              <button
                                key={id}
                                type="button"
                                onClick={() => {
                                  setCloudOperatorUserId(id);
                                  setCloudOperatorDropdownOpen(false);
                                }}
                                className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-[12px] font-bold transition-colors hover:bg-gray-50 ${selected ? "text-[var(--brand-navy)]" : "text-gray-700"}`}
                              >
                                <span className="min-w-0 truncate">#{id} · {label}</span>
                                {selected && <Check className="h-3.5 w-3.5 shrink-0" />}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </section>

            {/* 2. 关键词与平台配置 */}
            <section className="flex flex-col gap-4 border-t border-gray-100 pt-7">
              <div className="flex items-center justify-between">
                <SectionTitle title="关键词与搜索平台" />
              </div>
              
              {/* 全局默认平台 */}
              <div className="border-b border-gray-100 pb-4 flex flex-col gap-3">
                <div className="flex items-center gap-2">
                  <Settings2 className="w-4 h-4 text-[var(--brand-navy)]" />
                  <span className="text-[12px] font-bold text-gray-900">全局平台配置</span>
                  <span className="text-[11px] text-gray-400 font-medium ml-2">将应用于所有未开启独立配置的关键词</span>
                </div>
                <div className="w-full h-px bg-gray-100"></div>
                <PlatformSelector platforms={globalPlatforms} onChange={setGlobalPlatforms} />
              </div>

              {/* 关键词列表 */}
              <div className="flex flex-col gap-2 mt-2">
                <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">检索关键词管理</label>
                <div className="flex gap-2">
                  <input
                    ref={keywordFileInputRef}
                    type="file"
                    accept=".xlsx,.xlsm,.csv,.docx,.doc,.txt"
                    className="hidden"
                    onChange={handleKeywordFileImport}
                  />
                  <div className="flex-1 relative">
                    <input 
                      type="text" 
                      value={keywordInput}
                      onChange={e => setKeywordInput(e.target.value)}
                      onKeyDown={e => e.key === 'Enter' && handleAddKeyword()}
                      placeholder="输入关键词，按回车添加..."
                      className={subtleInputClass}
                    />
                  </div>
                  <button 
                    type="button"
                    onClick={handleAddKeyword}
                    className="text-[12px] font-bold text-[var(--brand-navy)] hover:text-[#102a30] transition-colors whitespace-nowrap flex items-center gap-1 self-end pb-2"
                  >
                    <Plus className="w-3.5 h-3.5" /> 添加
                  </button>
                  <button
                    type="button"
                    onClick={() => keywordFileInputRef.current?.click()}
                    disabled={isImportingKeywords}
                    className="text-[12px] font-bold text-gray-500 hover:text-gray-900 transition-colors whitespace-nowrap flex items-center gap-1 self-end pb-2 disabled:cursor-not-allowed disabled:text-gray-300"
                  >
                    <Download className="w-3.5 h-3.5" /> {isImportingKeywords ? "导入中..." : "导入"}
                  </button>
                </div>

                {keywordImportNotice && (
                  <div
                    className={`flex items-start justify-between gap-3 border-l-2 px-3 py-2.5 animate-in fade-in slide-in-from-top-2 duration-200 ${
                      keywordImportNotice.tone === "success"
                        ? "border-emerald-300 bg-emerald-50/50"
                        : "border-rose-300 bg-rose-50/55"
                    }`}
                  >
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <span className={`text-[10px] font-bold tracking-[0.18em] uppercase ${
                        keywordImportNotice.tone === "success" ? "text-emerald-600" : "text-rose-600"
                      }`}>
                        {keywordImportNotice.tone === "success" ? "操作成功" : "操作失败"}
                      </span>
                      <span className="text-[12px] font-medium text-gray-700">
                        {keywordImportNotice.message}
                      </span>
                    </div>
                    <button
                      type="button"
                      onClick={() => setKeywordImportNotice(null)}
                      className="shrink-0 rounded-full p-1 text-gray-400 transition-colors hover:bg-white/70 hover:text-gray-700"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                )}

                {/* 关键词卡片列表 */}
                <div className="flex flex-col gap-3 mt-3">
                  {keywords.length === 0 && (
                    <div className="border-b border-gray-100 pb-4 text-[12px] text-gray-400">
                      还没有添加关键词。先输入一个检索关键词，后面可以再单独调整它的平台和深度思考策略。
                    </div>
                  )}
                  {keywords.map(kw => (
                    <div key={kw.id} className="border-b border-gray-100 pb-4 flex flex-col gap-3">
                      <div className="flex items-start justify-between gap-4">
                        <div className="flex flex-col gap-2 min-w-0 flex-1">
                          <span className="text-[13px] font-black text-gray-900 tracking-tight">{kw.text}</span>
                          <div className="text-[11px] font-bold text-[var(--brand-navy)] inline-flex items-center">
                            当前跟随看板模式：{globalModeLabel}
                          </div>

                          <label className="flex items-center gap-1.5 cursor-pointer group">
                            <CustomCheckbox
                              checked={kw.customConfig}
                              onChange={() => setKeywords(keywords.map(k => k.id === kw.id ? {...k, customConfig: !k.customConfig} : k))}
                            />
                            <span className={`text-[11px] font-bold transition-colors ${kw.customConfig ? 'text-[var(--brand-navy)]' : 'text-gray-500 group-hover:text-gray-700'}`}>
                              例外状态：开启独立平台与深度思考配置
                            </span>
                          </label>
                        </div>
                        <button
                          type="button"
                          onClick={() => setKeywords(keywords.filter(k => k.id !== kw.id))}
                          className="text-gray-400 hover:text-red-500 p-1.5 transition-colors shrink-0"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>

                      {/* 独立配置展开区域 */}
                      {kw.customConfig && (
                        <div className="pl-3 border-l-2 border-slate-300 mt-1 animate-in fade-in slide-in-from-top-2 duration-200">
                          <PlatformSelector
                            platforms={kw.platforms}
                            onChange={(newPlatforms) => updateKeywordPlatforms(kw.id, newPlatforms)}
                          />
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </section>

            {/* 3. 调度与运行设置 */}
            <section className="flex flex-col gap-4 border-t border-gray-100 pt-7">
              <SectionTitle title="调度与运行设置" />
              <div className="flex flex-col gap-5">
                
                {/* 星期选择 */}
                <div className="flex flex-col gap-2 border-b border-gray-100 pb-4">
                  <label className="text-[12px] font-bold text-gray-900">自动查询星期：</label>
                  <div className="flex flex-wrap gap-4 items-center mt-1">
                    {dayNames.map((day, i) => (
                      <label key={day} className="flex items-center gap-1.5 cursor-pointer group">
                        <CustomCheckbox 
                          checked={days[i]} 
                          onChange={() => {
                            const newDays = [...days];
                            newDays[i] = !newDays[i];
                            setDays(newDays);
                          }}
                        />
                        <span className="text-[12px] font-medium text-gray-700 group-hover:text-gray-900">{day}</span>
                      </label>
                    ))}
                  </div>
                  <p className={helperTextClass}>具体执行时刻由“调度设置”里的每周时间统一控制，这里只决定任务在哪几天参与自动查询。</p>
                </div>

                {/* 开关组 */}
                <div className="flex flex-col gap-4">
                  <div className="flex items-center justify-between border-b border-gray-100 pb-4">
                    <label className="flex items-center gap-2 cursor-pointer group">
                      <CustomCheckbox checked={taskEnabled} onChange={() => setTaskEnabled(!taskEnabled)} />
                      <span className="text-[13px] font-bold text-gray-900">启用此任务</span>
                    </label>

                    <div className="flex items-center gap-6">
                      <label className="flex items-center gap-1.5 cursor-pointer group">
                        <span className={`text-[12px] font-bold transition-colors ${weeklyReport ? 'text-[var(--brand-navy)]' : 'text-gray-500'}`}>生成周报</span>
                        <TinySwitch checked={weeklyReport} onChange={() => setWeeklyReport(!weeklyReport)} />
                      </label>
                      <label className="flex items-center gap-1.5 cursor-pointer group">
                        <span className={`text-[12px] font-bold transition-colors ${monthlyReport ? 'text-[var(--brand-navy)]' : 'text-gray-500'}`}>生成月报</span>
                        <TinySwitch checked={monthlyReport} onChange={() => setMonthlyReport(!monthlyReport)} />
                      </label>
                    </div>
                  </div>

                  <div className="flex flex-col gap-1.5 border-b border-gray-100 pb-4 md:flex-row md:items-center md:gap-4">
                    <label className="flex items-center gap-2 cursor-pointer group shrink-0">
                      <CustomCheckbox checked={checkMode} onChange={() => setCheckMode(!checkMode)} />
                      <span className="text-[12px] font-medium text-gray-900">检查模式（运行时显示浏览器）</span>
                    </label>
                    <span className={helperTextClass}>开启后任务运行时浏览器前台显示，关闭后自动后台运行</span>
                  </div>

                  <div className="flex flex-col gap-1.5 border-b border-gray-100 pb-4 md:flex-row md:items-center md:gap-4">
                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-[12px] font-medium text-gray-900">识别模式每组截图数：</span>
                      <div className="flex items-center border-b border-gray-200 overflow-hidden h-7 w-[72px]">
                        <input 
                          type="number" 
                          min={1} 
                          max={10} 
                          value={screenshotCount}
                          onChange={(e) => {
                            setScreenshotCountEdited(true);
                            setScreenshotCount(parseInt(e.target.value, 10) || 1);
                          }}
                          className="w-full h-full text-center text-[12px] font-bold text-gray-900 outline-none"
                        />
                      </div>
                    </div>
                    <span className={helperTextClass}>达到数量后会汇总确认并排队发送</span>
                  </div>

                  <div className="flex flex-col gap-1.5 border-b border-gray-100 pb-4 md:flex-row md:items-center md:gap-4">
                    <label className="flex items-center gap-2 cursor-pointer group shrink-0">
                      <CustomCheckbox checked={extractReferencesEnabled} onChange={() => setExtractReferencesEnabled(!extractReferencesEnabled)} />
                      <span className="text-[12px] font-medium text-gray-900">抓取引用链接</span>
                    </label>
                    <span className={helperTextClass}>关闭时只保留正文和截图，不额外提取回答里的引用来源；普通模式默认关闭</span>
                  </div>

                  <div className="flex flex-col gap-1.5 border-b border-gray-100 pb-4 md:flex-row md:items-center md:gap-4">
                    <label className="flex items-center gap-2 cursor-pointer group shrink-0">
                      <CustomCheckbox checked={fixedScreenshotMode} onChange={() => setFixedScreenshotMode(!fixedScreenshotMode)} />
                      <span className="text-[12px] font-medium text-gray-900">启用固定截图策略</span>
                    </label>
                    <span className={helperTextClass}>
                      非识别模式下优先覆盖所有平台，再尽量补齐不同关键词；若还差 1 到 2 张，会让已命中过的关键词换平台补足
                    </span>
                  </div>

                  {fixedScreenshotMode && (
                    <div className="flex flex-col gap-1.5 animate-in fade-in duration-200 md:flex-row md:items-center md:gap-4">
                      <div className="flex items-center gap-2 shrink-0">
                        <span className="text-[12px] font-medium text-gray-900">固定截图目标张数：</span>
                        <div className="flex items-center border-b border-gray-200 overflow-hidden h-7 w-[72px]">
                          <input
                            type="number"
                            min={1}
                            max={20}
                            value={fixedScreenshotCount}
                            onChange={(e) => setFixedScreenshotCount(parseInt(e.target.value, 10) || 1)}
                            className="w-full h-full text-center text-[12px] font-bold text-gray-900 outline-none"
                          />
                        </div>
                      </div>
                      <span className={helperTextClass}>
                        非识别模式会累计到这个张数后停止；若目标小于平台数，系统会自动按平台数兜底
                      </span>
                    </div>
                  )}

                  {canDelete && !isNew && (
                    <div className="flex flex-col gap-1.5 border-t border-rose-100 pt-4 md:flex-row md:items-center md:justify-between md:gap-4">
                      <div className="flex flex-col gap-1">
                        <span className="text-[12px] font-bold text-rose-600">删除此品牌任务</span>
                        <span className={helperTextClass}>只保留三天备份；如果正式任务正在运行，会等运行结束并同步数据后再删除。</span>
                      </div>
                      <button
                        type="button"
                        onClick={onDeleteClick}
                        className="inline-flex h-8 shrink-0 items-center gap-1.5 self-start px-0 text-[12px] font-bold text-rose-600 transition-colors hover:text-rose-700 md:self-auto"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        删除任务
                      </button>
                    </div>
                  )}
	                </div>

              </div>
            </section>
            
            {/* Bottom padding for scroll */}
            <div className="h-4"></div>
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-gray-100 bg-white flex items-center justify-end gap-3 shrink-0 z-10">
          {saveNotice && (
            <div className={`mr-auto text-[12px] font-medium ${saveNotice.tone === "error" ? "text-rose-600" : "text-emerald-600"}`}>
              {saveNotice.message}
            </div>
          )}
          <button 
            type="button"
            onClick={onClose}
            className="px-5 py-2 text-[12px] font-bold text-gray-600 hover:text-gray-900 transition-colors"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={saving || !hasDirtyChanges}
            className="flex items-center gap-1.5 bg-[var(--brand-cyan)] hover:bg-[#16A6C8] text-white px-5 py-2 rounded-lg text-[12px] font-bold transition-colors shadow-[0_10px_24px_-18px_rgba(var(--brand-cyan-rgb),0.55)] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Save className="w-4 h-4" /> {saving ? '保存中...' : hasDirtyChanges ? `保存 ${dirtySectionCount} 项修改` : '暂无修改'}
          </button>
        </div>

      </div>
    </div>
  );
}

// --- Subcomponents ---

function SectionTitle({ title }: { title: string }) {
  return (
    <div className="flex items-center gap-2">
      <div className="w-1 h-3.5 bg-[var(--brand-navy)] rounded-sm"></div>
      <h3 className="text-[13px] font-black text-gray-900 tracking-[0.14em]">{title}</h3>
    </div>
  );
}

function CustomCheckbox({ checked, onChange }: { checked: boolean, onChange: () => void }) {
  return (
    <div 
      className={`w-4 h-4 rounded-[4px] border flex items-center justify-center shrink-0 transition-colors ${
        checked ? 'bg-[var(--brand-navy)] border-[var(--brand-navy)] text-white' : 'bg-white border-gray-300'
      }`}
      onClick={(e) => { e.preventDefault(); onChange(); }}
    >
      {checked && <Check className="w-3 h-3" strokeWidth={3} />}
    </div>
  );
}

function TinySwitch({ checked, onChange }: { checked: boolean, onChange: () => void }) {
  return (
    <div 
      onClick={(e) => { e.preventDefault(); onChange(); }}
      className={`w-[26px] h-[14px] rounded-full flex items-center p-[2px] transition-colors cursor-pointer ${checked ? 'bg-[var(--brand-navy)]' : 'bg-gray-200'}`}
    >
      <div className={`w-[10px] h-[10px] rounded-full bg-white transition-transform shadow-sm ${checked ? 'translate-x-[12px]' : 'translate-x-0'}`} />
    </div>
  )
}

function PlatformSelector({ platforms, onChange }: { platforms: PlatformState[], onChange: (p: PlatformState[]) => void }) {
  const toggleActive = (idx: number) => {
    const newPlatforms = platforms.map((p, i) =>
      i === idx ? { ...p, active: !p.active, deep: p.active ? false : p.deep } : p
    );
    onChange(newPlatforms);
  };

  const toggleDeep = (idx: number) => {
    if (!platforms[idx].active) return;
    const newPlatforms = platforms.map((p, i) =>
      i === idx ? { ...p, deep: !p.deep } : p
    );
    onChange(newPlatforms);
  };

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-1.5">
      {platforms.map((p, idx) => (
        <div key={p.name} className={`grid grid-cols-[minmax(0,1fr)_1px_auto] items-center gap-2 py-1 min-w-0 border-b border-gray-100/80 transition-opacity ${
          p.active ? 'text-gray-900' : 'opacity-60'
        }`}>
          <div 
            className="flex items-center gap-1.5 cursor-pointer min-w-0 pr-1.5" 
            onClick={() => toggleActive(idx)}
          >
            <div className={`w-1.5 h-1.5 rounded-full transition-colors ${p.active ? 'bg-[var(--brand-cyan)]' : 'bg-gray-300'}`} />
            <span className={`text-[11px] font-bold transition-colors ${p.active ? 'text-gray-900' : 'text-gray-500'}`}>{p.name}</span>
          </div>
          
          <div className="w-px h-3.5 bg-gray-200 self-center"></div>
          
          <div 
            className={`flex items-center gap-1 pl-1.5 justify-self-end transition-opacity ${p.active ? 'cursor-pointer opacity-100' : 'cursor-not-allowed opacity-50'}`} 
            onClick={() => toggleDeep(idx)}
          >
            <Brain className={`w-3.5 h-3.5 transition-colors ${p.deep && p.active ? 'text-[var(--brand-cyan)]' : 'text-gray-300'}`} />
            <div 
              className={`w-6 h-3 rounded-full flex items-center p-[2px] transition-colors ${
                !p.active ? 'bg-gray-200' : p.deep ? 'bg-[var(--brand-cyan)]' : 'bg-gray-200'
              }`}
            >
              <div className={`w-2 h-2 rounded-full bg-white transition-transform shadow-sm ${p.deep && p.active ? 'translate-x-[12px]' : 'translate-x-0'}`} />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// --- Specific Insights Components for Brand ---

function BrandAITrendSection({ taskId }: { taskId?: string }) {
  const [timeRange, setTimeRange] = useState<'week' | 'month' | 'year'>('week');
  const [trend, setTrend] = useState<TrendSnapshot>({
    timeRange: 'week',
    current: 0,
    avg: 0,
    peak: 0,
    delta: 0,
    data: [],
  });

  useEffect(() => {
    if (!taskId) return;
    let cancelled = false;
    void fetchTaskTrend(taskId, timeRange).then((data) => {
      if (!cancelled) {
        setTrend(data);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [taskId, timeRange]);

  const currentData = trend.data || [];
  const current = trend.current ?? trend.avg ?? 0;
  const peak = trend.peak ?? 0;
  const avg = trend.avg ?? 0;
  const delta = trend.delta ?? 0;
  const deltaText = `${delta > 0 ? '+' : ''}${delta}%`;
  const deltaTone = delta > 0 ? 'text-[#16a34a]' : delta < 0 ? 'text-[#dc2626]' : 'text-gray-400';
  const deltaIconClass = delta < 0 ? 'rotate-90' : delta === 0 ? 'rotate-45' : '';

  return (
    <div className="grid h-[220px] min-h-0 grid-rows-[24px_auto_1fr]">
      <div className="flex items-start justify-between gap-4">
        <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">AI 辅助优化趋势</h3>
        <div className="flex items-center gap-2">
          <button 
            type="button"
            onClick={() => setTimeRange('week')}
            className={`text-[10px] font-bold transition-colors ${timeRange === 'week' ? 'text-[var(--brand-navy)]' : 'text-gray-400 hover:text-gray-700'}`}
          >
            周
          </button>
          <span className="w-px h-2.5 bg-gray-200"></span>
          <button 
            type="button"
            onClick={() => setTimeRange('month')}
            className={`text-[10px] font-bold transition-colors ${timeRange === 'month' ? 'text-[var(--brand-navy)]' : 'text-gray-400 hover:text-gray-700'}`}
          >
            月
          </button>
          <span className="w-px h-2.5 bg-gray-200"></span>
          <button 
            type="button"
            onClick={() => setTimeRange('year')}
            className={`text-[10px] font-bold transition-colors ${timeRange === 'year' ? 'text-[var(--brand-navy)]' : 'text-gray-400 hover:text-gray-700'}`}
          >
            年
          </button>
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-6 gap-y-2 pt-2">
        <div className="flex items-end gap-4">
          <div className="flex items-center gap-2">
            <span className="text-[24px] leading-none text-gray-900 font-medium">{current}<span className="text-[14px] text-gray-500">%</span></span>
            <span className={`${deltaTone} text-[11px] font-bold flex items-center gap-0.5 mb-0.5`}>
              <ArrowUpRight className={`w-3 h-3 transition-transform ${deltaIconClass}`} /> {deltaText}
            </span>
          </div>
          
          <div className="flex items-center gap-4 ml-3 pl-4 border-l border-gray-200/80 mb-0.5">
            <div className="flex flex-col gap-0.5">
              <span className="text-[9px] text-gray-400 font-medium tracking-wider">峰值</span>
              <span className="text-[13px] text-gray-900 font-bold leading-none">{peak}%</span>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[9px] text-gray-400 font-medium tracking-wider">均值</span>
              <span className="text-[13px] text-gray-900 font-bold leading-none">{avg}%</span>
            </div>
          </div>
        </div>

        <div className="flex flex-col items-start gap-2 justify-self-end pt-0.5 text-[11px] text-gray-500 font-medium">
          <div className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full border-[1.5px] border-[var(--brand-navy)] bg-transparent"></div>预测值
          </div>
          <div className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full bg-[var(--brand-navy)]"></div>实际值
          </div>
        </div>
      </div>

      <div className="relative mt-1 min-h-0 w-full -ml-2 translate-y-[8px]">
        <div className="h-full w-full">
          <ChartArea
            data={currentData.length ? currentData : [{ name: '', value: 0, predict: 0 }]}
            margin={{ top: 4, bottom: 0 }}
            xAxisDy={2}
          />
        </div>
      </div>
    </div>
  );
}

function BrandOptimizationMap({ regions }: { regions: string }) {
  const [mapType, setMapType] = useState<'domestic' | 'international'>('domestic');

  const checkActive = (label: string) => regions.includes(label);

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
  ].map(n => ({ ...n, active: checkActive(n.label) }));

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
  ].map(n => ({ ...n, active: checkActive(n.label) }));

  const nodes = mapType === 'domestic' ? domesticNodes : internationalNodes;

  return (
    <div className="grid h-[220px] grid-rows-[24px_1fr] gap-1 pl-0 md:-mt-[2px] md:pl-6 md:border-l border-gray-100">
      <div className="flex items-start justify-between">
        <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">优化区域分布</h3>
        
        {/* Toggle */}
        <div className="flex items-center gap-4">
          <button 
            type="button"
            onClick={() => setMapType('domestic')}
            className={`flex items-center gap-1 text-[11px] font-medium transition-colors ${mapType === 'domestic' ? 'text-[var(--brand-navy)]' : 'text-gray-400 hover:text-gray-600'}`}
          >
            <MapIcon className="w-3 h-3" /> 国内
          </button>
          <span className="w-px h-2.5 bg-gray-200"></span>
          <button 
            type="button"
            onClick={() => setMapType('international')}
            className={`flex items-center gap-1 text-[11px] font-medium transition-colors ${mapType === 'international' ? 'text-[var(--brand-navy)]' : 'text-gray-400 hover:text-gray-600'}`}
          >
            <Globe className="w-3 h-3" /> 国际
          </button>
        </div>
      </div>

      <div className="w-full bg-gray-50/40 relative border-b border-gray-100 overflow-hidden h-full">
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
                stroke="var(--brand-cyan)" strokeWidth="1" strokeDasharray="3 3"
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
              {node.active && <div className="absolute w-5 h-5 bg-[rgba(var(--brand-cyan-rgb),0.22)] rounded-full animate-ping"></div>}
              <div className={`w-2 h-2 rounded-full border-[1.5px] ${node.active ? 'bg-[var(--brand-cyan)] border-white shadow-sm' : 'bg-gray-300 border-white'}`}></div>
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
