import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import { Settings2, Save, RefreshCw, CalendarDays, Clock, X, Edit2, ChevronDown, Upload, Download, Send, ListFilter, RotateCcw, Wrench } from "lucide-react";
import { ImageWithFallback } from "./figma/ImageWithFallback";
import { ConfirmModal } from "./ConfirmModal";
import { browserAuthAction, checkAppUpdate, diagnoseSelectorHeal, diagnoseSelectorPauseState, fetchAccountCrawlExclusions, fetchBrowserAuth, fetchLocalModelStatus, fetchLocalUpdatePlan, fetchPlatformKeys, fetchSettings, pickDirectory, prepareAppUpdate, prepareLocalModel, readSettingsCache, restoreAccountCrawlExclusions, savePlatformConfig, saveSettings, saveProfile, startLocalUpdate, refreshContextSnapshots, testLocalModel, testSchedulerNotificationWebhook, type AppUpdateStatusSnapshot, type BrowserAuthPlatformSnapshot, type ExcludedArticleLinkSnapshot, type LocalModelStatus, type PlatformKeyInfo, type SelectorAgentSettingsSnapshot, type SelectorHealFieldResult, type SelectorPauseStateResponse } from "../lib/backend";
import { notifySaveSuccess } from "../lib/saveToast";
import { DatePickerField } from "./ui/date-picker-field";

type PlatformOption = {
  id: string;
  name: string;
  models: string[];
};

function getExcludedArticleUrl(link: ExcludedArticleLinkSnapshot): string {
  return String(link.url || "").trim();
}

function getExcludedArticleTitle(link: ExcludedArticleLinkSnapshot): string {
  return String(link.title || link.url || "已排除文章").trim();
}

type BrowserAutomationField = {
  key: string;
  label: string;
  placeholder?: string;
  legacyKeys?: string[];
  advanced?: boolean;
  inputType?: string;
};

type BrowserAutomationPlatformConfig = {
  summary: string;
  note: string;
  fields: BrowserAutomationField[];
};

const BROWSER_AUTOMATION_RUNTIME_FIELDS: BrowserAutomationField[] = [
  {
    key: "browser_locale",
    label: "浏览器语言 :",
    placeholder: "zh-CN",
    advanced: true,
  },
  {
    key: "browser_accept_language",
    label: "Accept-Language :",
    placeholder: "zh-CN,zh;q=0.9,en;q=0.8",
    advanced: true,
  },
  {
    key: "browser_timezone_id",
    label: "浏览器时区 :",
    placeholder: "Asia/Shanghai",
    advanced: true,
  },
  {
    key: "browser_user_agent",
    label: "User-Agent :",
    placeholder: "留空则自动匹配当前浏览器版本",
    advanced: true,
  },
  {
    key: "browser_proxy_server",
    label: "代理地址 :",
    placeholder: "http://127.0.0.1:7890",
    advanced: true,
  },
  {
    key: "browser_proxy_username",
    label: "代理用户名 :",
    placeholder: "选填",
    advanced: true,
  },
  {
    key: "browser_proxy_password",
    label: "代理密码 :",
    placeholder: "选填",
    advanced: true,
    inputType: "password",
  },
  {
    key: "browser_extra_args",
    label: "额外启动参数 :",
    placeholder: "--disable-features=Translate",
    advanced: true,
  },
  {
    key: "page_stabilize_wait_min_ms",
    label: "稳定等待最小值 :",
    placeholder: "1200",
    advanced: true,
  },
  {
    key: "page_stabilize_wait_max_ms",
    label: "稳定等待最大值 :",
    placeholder: "2600",
    advanced: true,
  },
  {
    key: "failure_backoff_base_seconds",
    label: "失败退避起始秒数 :",
    placeholder: "3",
    advanced: true,
  },
  {
    key: "failure_backoff_max_seconds",
    label: "失败退避上限秒数 :",
    placeholder: "20",
    advanced: true,
  },
];

const BROWSER_AUTOMATION_RUNTIME_DEFAULTS: Record<string, string> = {
  browser_locale: "",
  browser_accept_language: "",
  browser_timezone_id: "",
  browser_user_agent: "",
  browser_proxy_server: "",
  browser_proxy_username: "",
  browser_proxy_password: "",
  browser_extra_args: "",
  page_stabilize_wait_min_ms: "",
  page_stabilize_wait_max_ms: "",
  failure_backoff_base_seconds: "",
  failure_backoff_max_seconds: "",
};

function withBrowserRuntimeFields(fields: BrowserAutomationField[]): BrowserAutomationField[] {
  return [...fields, ...BROWSER_AUTOMATION_RUNTIME_FIELDS];
}

function splitBrowserAutomationFields(fields: BrowserAutomationField[]): {
  basicFields: BrowserAutomationField[];
  advancedFields: BrowserAutomationField[];
} {
  const basicFields = fields.filter((field) => !field.advanced);
  const advancedFields = fields.filter((field) => !!field.advanced);
  return { basicFields, advancedFields };
}

function hasConfiguredAdvancedBrowserFields(
  fields: BrowserAutomationField[],
  values: Record<string, string> | undefined,
): boolean {
  return fields.some((field) => {
    if (!field.advanced) return false;
    return Boolean(String(values?.[field.key] || "").trim());
  });
}

type ApiModeModelState = {
  fastModel: string;
  deepModel: string;
};

type QueryExecutionModeSettings = {
  strategy: string;
  sessionPoolDispatch: string;
  sessionPoolPlatformBatchSize: string;
  sessionTtlMinutesMin: string;
  sessionTtlMinutesMax: string;
  sessionMaxQueriesMin: string;
  sessionMaxQueriesMax: string;
  minQueriesWindowMinutes: string;
  minQueriesPerWindow: string;
  singleQueryTimeoutMinutes: string;
  noProgressTimeoutMinutes: string;
  minRestartCooldownMinutes: string;
  restartAfterManualRecovery: boolean;
  restartAfterStructuralFailures: string;
};

type LocalModelFormState = {
  defaultModel: string;
  binaryPath: string;
  baseUrl: string;
  autoStart: boolean;
  autoPull: boolean;
  autoPrepareOnLaunch: boolean;
};

type AvatarCropDraft = {
  src: string;
  fileName: string;
  width: number;
  height: number;
  baseScale: number;
  zoom: number;
  offsetX: number;
  offsetY: number;
};

const DEFAULT_PROFILE_AVATAR =
  "https://images.unsplash.com/photo-1624091844772-554661d10173?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxtaW5pbWFsaXN0JTIwcHJvZmVzc2lvbmFsJTIwcG9ydHJhaXQlMjBhc2lhbiUyMHdvbWFufGVufDF8fHx8MTc3NDU0MTU0MHww&ixlib=rb-4.1.0&q=80&w=1080&utm_source=figma&utm_medium=referral";
const AVATAR_CROP_VIEWPORT = 240;
const AVATAR_OUTPUT_SIZE = 320;
const AVATAR_MAX_ZOOM = 3;

const PLATFORM_LABELS: Record<string, string> = {
  local_model: "本地模型",
  doubao: "豆包",
  deepseek: "DeepSeek",
  ark_deepseek: "方舟 DeepSeek",
  kimi: "Kimi",
  yuanbao: "元宝",
  tongyi: "通义千问",
  wenxin: "文心一言",
  chatgpt: "ChatGPT",
  claude: "Claude",
  gemini: "Gemini",
  perplexity: "Perplexity",
};

const BROWSER_AUTOMATION_PLATFORM_IDS = ["doubao", "deepseek", "kimi", "yuanbao", "tongyi", "wenxin"] as const;
const SCHEDULER_DAYS = [0, 1, 2, 3, 4, 5, 6] as const;
type BrowserAutomationPlatformId = (typeof BROWSER_AUTOMATION_PLATFORM_IDS)[number];
type SchedulerDay = (typeof SCHEDULER_DAYS)[number];
type ActiveDaysState = Record<SchedulerDay, boolean>;
const API_MODE_SPLIT_PLATFORM_IDS = ["deepseek", "tongyi", "yuanbao"] as const;
const QUERY_EXECUTION_MODE_IDS = ["browser", "smart"] as const;
const QUERY_EXECUTION_STRATEGY_OPTIONS = [
  { value: "platform_serial", label: "按平台分组串行" },
  { value: "session_pool", label: "平台会话池（实验中）" },
];
const BROWSER_ANSWER_MODE_OPTIONS = [
  { value: "page", label: "页面原始截图" },
  { value: "dom", label: "DOM 文本生成" },
];
const RECOGNITION_MODE_OPTIONS = [
  { value: "screenshot", label: "截图 OCR 识别" },
  { value: "dom", label: "DOM 文本识别" },
];
const SESSION_POOL_DISPATCH_OPTIONS = [
  { value: "platform_batch", label: "按平台小批次轮转" },
  { value: "keyword_round_robin", label: "按关键词轮转平台" },
];
const UPDATE_CHANNEL_OPTIONS = [
  { value: "stable", label: "稳定版" },
  { value: "beta", label: "Beta 预览" },
];
const QUERY_EXECUTION_DEFAULTS: Record<(typeof QUERY_EXECUTION_MODE_IDS)[number], QueryExecutionModeSettings> = {
  browser: {
    strategy: "platform_serial",
    sessionPoolDispatch: "platform_batch",
    sessionPoolPlatformBatchSize: "2",
    sessionTtlMinutesMin: "120",
    sessionTtlMinutesMax: "150",
    sessionMaxQueriesMin: "110",
    sessionMaxQueriesMax: "140",
    minQueriesWindowMinutes: "30",
    minQueriesPerWindow: "10",
    singleQueryTimeoutMinutes: "12",
    noProgressTimeoutMinutes: "30",
    minRestartCooldownMinutes: "10",
    restartAfterManualRecovery: true,
    restartAfterStructuralFailures: "2",
  },
  smart: {
    strategy: "platform_serial",
    sessionPoolDispatch: "platform_batch",
    sessionPoolPlatformBatchSize: "2",
    sessionTtlMinutesMin: "120",
    sessionTtlMinutesMax: "150",
    sessionMaxQueriesMin: "110",
    sessionMaxQueriesMax: "140",
    minQueriesWindowMinutes: "30",
    minQueriesPerWindow: "10",
    singleQueryTimeoutMinutes: "12",
    noProgressTimeoutMinutes: "30",
    minRestartCooldownMinutes: "10",
    restartAfterManualRecovery: true,
    restartAfterStructuralFailures: "2",
  },
};

const BROWSER_AUTOMATION_CONFIGS: Record<(typeof BROWSER_AUTOMATION_PLATFORM_IDS)[number], BrowserAutomationPlatformConfig> = {
  doubao: {
    summary: "模式切换与新对话",
    note: "豆包是先点模式入口再选具体模式，建议优先维护 data-testid 这一类稳定 selector；下方也可以按平台单独覆盖语言、时区、代理等运行参数。",
    fields: withBrowserRuntimeFields([
      {
        key: "new_chat_selector",
        label: "新对话 Selector :",
        placeholder: "div[data-testid='create_conversation_button']",
      },
      {
        key: "deep_think_menu_trigger_selector",
        label: "模式入口 Selector :",
        placeholder: 'div[data-testid="deep-thinking-action-button"]',
        legacyKeys: ["mode_menu_trigger_selector"],
      },
      {
        key: "deep_think_think_selector",
        label: "思考菜单项 Selector :",
        placeholder: 'div[data-testid="deep-thinking-action-item-1"]',
        legacyKeys: ["think_option_selector"],
      },
      {
        key: "deep_think_quick_selector",
        label: "快速菜单项 Selector :",
        placeholder: '[role="menuitem"]:has-text("快速")',
        legacyKeys: ["quick_option_selector"],
      },
    ]),
  },
  deepseek: {
    summary: "新对话、深度思考与暂停态",
    note: "DeepSeek 的暂停态用于判断回答仍在生成；下方也可以按平台单独覆盖语言、时区、代理等运行参数。",
    fields: withBrowserRuntimeFields([
      {
        key: "new_chat_selector",
        label: "新对话 Selector :",
        placeholder: "button:has(path[d^='M8 0.599609'])",
      },
      {
        key: "deep_think_selector",
        label: "深度思考 Selector :",
        placeholder: "button:has-text('深度思考')",
      },
      {
        key: "generation_pause_selector",
        label: "暂停态 Selector :",
        placeholder: 'path[d^="M2 4.88"]',
      },
    ]),
  },
  kimi: {
    summary: "新对话入口",
    note: "Kimi 当前没有深度思考切换逻辑，这里保留新对话入口 selector 方便后续适配；下方也可以按平台单独覆盖语言、时区、代理等运行参数。",
    fields: withBrowserRuntimeFields([
      {
        key: "new_chat_selector",
        label: "新对话 Selector :",
        placeholder: "button:has(svg[name='AddConversation'])",
      },
    ]),
  },
  yuanbao: {
    summary: "新对话与深度思考",
    note: "元宝现已默认联网搜索，这里只保留新对话与深度思考相关 selector；下方也可以按平台单独覆盖语言、时区、代理等运行参数。",
    fields: withBrowserRuntimeFields([
      {
        key: "new_chat_selector",
        label: "新对话 Selector :",
        placeholder: ".yb-icon.icon-yb-ic_newchat_20",
      },
      {
        key: "deep_think_selector",
        label: "深度思考 Selector :",
        placeholder: "button:has-text('深度思考')",
      },
    ]),
  },
  tongyi: {
    summary: "新对话与深度思考",
    note: "通义通常是单独的深度思考按钮，改版时优先维护按钮 selector 即可；下方也可以按平台单独覆盖语言、时区、代理等运行参数。",
    fields: withBrowserRuntimeFields([
      {
        key: "new_chat_selector",
        label: "新对话 Selector :",
        placeholder: 'button:has-text("新建对话")',
      },
      {
        key: "deep_think_selector",
        label: "深度思考 Selector :",
        placeholder: 'button[aria-label="深度思考"]',
      },
    ]),
  },
  wenxin: {
    summary: "新对话与思考菜单",
    note: "文心是一层工具栏入口加一层菜单项开启，建议分别维护入口和菜单项 selector；下方也可以按平台单独覆盖语言、时区、代理等运行参数。",
    fields: withBrowserRuntimeFields([
      {
        key: "new_chat_selector",
        label: "新对话 Selector :",
        placeholder: 'img[alt="New Session Btn"]',
      },
      {
        key: "deep_think_menu_selector",
        label: "思考菜单入口 Selector :",
        placeholder: '[class*="inputToolbarLeft"] [class*="item__"]',
      },
      {
        key: "deep_think_enable_item_selector",
        label: "思考开启项 Selector :",
        placeholder: '[class*="dtModeItem__"]',
      },
    ]),
  },
};

const BROWSER_SELECTOR_DEFAULTS: Partial<Record<BrowserAutomationPlatformId, Record<string, string>>> = {
  doubao: {
    ...BROWSER_AUTOMATION_RUNTIME_DEFAULTS,
    new_chat_selector: "div[data-testid='create_conversation_button']",
    deep_think_menu_trigger_selector: 'div[data-testid="deep-thinking-action-button"], button:has(div[data-testid="deep-thinking-action-button"])',
    deep_think_think_selector: 'div[data-testid="deep-thinking-action-item-1"], [role="menuitem"]:has-text("思考")',
    deep_think_quick_selector: '[role="menuitem"]:has-text("快速")',
  },
  deepseek: {
    ...BROWSER_AUTOMATION_RUNTIME_DEFAULTS,
    new_chat_selector: "button:has(path[d^='M8 0.599609'])",
    deep_think_selector: "button:has-text('深度思考'), div:has-text('深度思考'), span:has-text('深度思考')",
    generation_pause_selector: 'path[d^="M2 4.88"], path[d^="M2 4.87988"], path[d^="M2 4.8"]',
  },
  kimi: {
    ...BROWSER_AUTOMATION_RUNTIME_DEFAULTS,
    new_chat_selector: "button:has(svg[name='AddConversation']), [role='button']:has(svg[name='AddConversation']), svg[name='AddConversation']",
  },
  yuanbao: {
    ...BROWSER_AUTOMATION_RUNTIME_DEFAULTS,
    new_chat_selector: ".yb-icon.icon-yb-ic_newchat_20",
    deep_think_selector: "button:has-text('Deep thinking'), div:has-text('Deep thinking'), span:has-text('Deep thinking'), button:has-text('深度思考'), div:has-text('深度思考')",
  },
  tongyi: {
    ...BROWSER_AUTOMATION_RUNTIME_DEFAULTS,
    new_chat_selector: 'button:has-text("新建对话"), button[aria-label="新建对话"], button[class*="newChat"], button[class*="new-chat"]',
    deep_think_selector: 'button[aria-label="深度思考"], button:has-text("深度思考")',
  },
  wenxin: {
    ...BROWSER_AUTOMATION_RUNTIME_DEFAULTS,
    new_chat_selector: 'img[alt="New Session Btn"]',
    deep_think_menu_selector: '[class*="inputToolbarLeft"] [class*="item__"]',
    deep_think_enable_item_selector: '[class*="dtModeItem__"]',
  },
};

function buildBrowserSelectorState(): Record<string, Record<string, string>> {
  return Object.fromEntries(
    BROWSER_AUTOMATION_PLATFORM_IDS.map((platformId) => [platformId, { ...(BROWSER_SELECTOR_DEFAULTS[platformId] || {}) }]),
  ) as Record<string, Record<string, string>>;
}

function buildBrowserAutomationPayload(
  browserSelectors: Record<string, Record<string, string>>,
): Record<string, Record<string, string>> {
  return Object.fromEntries(
    BROWSER_AUTOMATION_PLATFORM_IDS.map((platformId) => {
      const defaults = BROWSER_SELECTOR_DEFAULTS[platformId] || {};
      const current = browserSelectors[platformId] || {};
      const entries = BROWSER_AUTOMATION_CONFIGS[platformId].fields
        .map((field) => {
          const value = String(current[field.key] || "").trim();
          const defaultValue = String(defaults[field.key] || "").trim();
          if (!value || value === defaultValue) {
            return null;
          }
          return [field.key, value] as const;
        })
        .filter(Boolean) as Array<readonly [string, string]>;
      return [platformId, Object.fromEntries(entries)];
    }),
  );
}

function buildQueryExecutionState(): Record<string, QueryExecutionModeSettings> {
  return Object.fromEntries(
    QUERY_EXECUTION_MODE_IDS.map((mode) => [mode, { ...QUERY_EXECUTION_DEFAULTS[mode] }]),
  ) as Record<string, QueryExecutionModeSettings>;
}

function normalizeQueryExecutionStrategy(value: unknown): string {
  const normalized = String(value || "").trim();
  if (normalized === "single_query_isolated") {
    return "platform_serial";
  }
  return QUERY_EXECUTION_STRATEGY_OPTIONS.some((option) => option.value === normalized)
    ? normalized
    : "platform_serial";
}

function normalizeSessionPoolDispatch(value: unknown): string {
  const normalized = String(value || "").trim();
  return SESSION_POOL_DISPATCH_OPTIONS.some((option) => option.value === normalized)
    ? normalized
    : "platform_batch";
}

function buildQueryExecutionPayload(
  settings: Record<string, QueryExecutionModeSettings>,
): Record<string, Record<string, string | number | boolean>> {
  return Object.fromEntries(
    QUERY_EXECUTION_MODE_IDS.map((mode) => {
      const current = settings[mode] || QUERY_EXECUTION_DEFAULTS[mode];
      return [mode, {
        strategy: normalizeQueryExecutionStrategy(current.strategy || QUERY_EXECUTION_DEFAULTS[mode].strategy),
        session_pool_dispatch: normalizeSessionPoolDispatch(current.sessionPoolDispatch || QUERY_EXECUTION_DEFAULTS[mode].sessionPoolDispatch),
        session_pool_platform_batch_size: parseInt(current.sessionPoolPlatformBatchSize, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].sessionPoolPlatformBatchSize, 10),
        session_ttl_minutes_min: parseInt(current.sessionTtlMinutesMin, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].sessionTtlMinutesMin, 10),
        session_ttl_minutes_max: parseInt(current.sessionTtlMinutesMax, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].sessionTtlMinutesMax, 10),
        session_max_queries_min: parseInt(current.sessionMaxQueriesMin, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].sessionMaxQueriesMin, 10),
        session_max_queries_max: parseInt(current.sessionMaxQueriesMax, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].sessionMaxQueriesMax, 10),
        min_queries_window_minutes: parseInt(current.minQueriesWindowMinutes, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].minQueriesWindowMinutes, 10),
        min_queries_per_window: parseInt(current.minQueriesPerWindow, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].minQueriesPerWindow, 10),
        single_query_timeout_minutes: parseInt(current.singleQueryTimeoutMinutes, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].singleQueryTimeoutMinutes, 10),
        no_progress_timeout_minutes: parseInt(current.noProgressTimeoutMinutes, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].noProgressTimeoutMinutes, 10),
        min_restart_cooldown_minutes: parseInt(current.minRestartCooldownMinutes, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].minRestartCooldownMinutes, 10),
        restart_after_manual_recovery: current.restartAfterManualRecovery,
        restart_after_structural_failures: parseInt(current.restartAfterStructuralFailures, 10) || parseInt(QUERY_EXECUTION_DEFAULTS[mode].restartAfterStructuralFailures, 10),
      }];
    }),
  );
}

const VISION_MODEL_HINTS: Record<string, string[]> = {
  deepseek: ["vision", "vl", "janus", "image", "multi"],
};

function uniqStrings(values: string[]): string[] {
  return values.filter((value, index) => value && values.indexOf(value) === index);
}

function modelSupportsImageInput(platformId: string, model: string): boolean {
  const normalizedPlatform = String(platformId || "").trim().toLowerCase();
  const normalizedModel = String(model || "").trim().toLowerCase();
  const hints = VISION_MODEL_HINTS[normalizedPlatform];
  if (!hints || !normalizedModel) {
    return true;
  }
  return hints.some((hint) => normalizedModel.includes(hint));
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") {
        resolve(reader.result);
        return;
      }
      reject(new Error("读取头像失败"));
    };
    reader.onerror = () => reject(new Error("读取头像失败"));
    reader.readAsDataURL(file);
  });
}

function loadImage(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("头像解析失败"));
    img.src = dataUrl;
  });
}

function getAvatarCropMetrics(draft: AvatarCropDraft, zoom = draft.zoom) {
  const displayWidth = draft.width * draft.baseScale * zoom;
  const displayHeight = draft.height * draft.baseScale * zoom;
  return {
    displayWidth,
    displayHeight,
    maxOffsetX: Math.max(0, (displayWidth - AVATAR_CROP_VIEWPORT) / 2),
    maxOffsetY: Math.max(0, (displayHeight - AVATAR_CROP_VIEWPORT) / 2),
  };
}

function clampAvatarCropOffset(draft: AvatarCropDraft, offsetX: number, offsetY: number, zoom = draft.zoom) {
  const metrics = getAvatarCropMetrics(draft, zoom);
  return {
    offsetX: Math.min(metrics.maxOffsetX, Math.max(-metrics.maxOffsetX, offsetX)),
    offsetY: Math.min(metrics.maxOffsetY, Math.max(-metrics.maxOffsetY, offsetY)),
  };
}

async function buildAvatarCropDraft(file: File): Promise<AvatarCropDraft> {
  const src = await readFileAsDataUrl(file);
  const img = await loadImage(src);
  const width = img.naturalWidth || img.width;
  const height = img.naturalHeight || img.height;
  const baseScale = Math.max(AVATAR_CROP_VIEWPORT / width, AVATAR_CROP_VIEWPORT / height);
  return {
    src,
    fileName: file.name,
    width,
    height,
    baseScale,
    zoom: 1,
    offsetX: 0,
    offsetY: 0,
  };
}

async function renderAvatarFromCrop(draft: AvatarCropDraft): Promise<string> {
  const img = await loadImage(draft.src);
  const canvas = document.createElement("canvas");
  canvas.width = AVATAR_OUTPUT_SIZE;
  canvas.height = AVATAR_OUTPUT_SIZE;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return draft.src;
  }

  const ratio = AVATAR_OUTPUT_SIZE / AVATAR_CROP_VIEWPORT;
  const { displayWidth, displayHeight } = getAvatarCropMetrics(draft);
  const drawWidth = displayWidth * ratio;
  const drawHeight = displayHeight * ratio;
  const drawX = (AVATAR_OUTPUT_SIZE - drawWidth) / 2 + draft.offsetX * ratio;
  const drawY = (AVATAR_OUTPUT_SIZE - drawHeight) / 2 + draft.offsetY * ratio;

  ctx.fillStyle = "#f3f4f6";
  ctx.fillRect(0, 0, AVATAR_OUTPUT_SIZE, AVATAR_OUTPUT_SIZE);
  ctx.drawImage(img, drawX, drawY, drawWidth, drawHeight);
  return canvas.toDataURL("image/jpeg", 0.9);
}

function buildPlatformOptions(
  platformKeys: Record<string, PlatformKeyInfo>,
  {
    visionOnly = false,
    currentPlatform = "",
    currentModel = "",
  }: { visionOnly?: boolean; currentPlatform?: string; currentModel?: string } = {},
): PlatformOption[] {
  const options = Object.entries(platformKeys)
    .filter(([id, info]) => id === id.toLowerCase() && ((info.has_access ?? info.has_key) || id === "local_model"))
    .map(([id, info]) => {
      const baseModels = uniqStrings([
        String(info.api_model || "").trim(),
        String(info.api_fast_model || "").trim(),
        String(info.api_deep_model || "").trim(),
        ...((info.model_options || []).map((item) => String(item || "").trim())),
        String(info.fast_model_default || "").trim(),
        String(info.deep_model_default || "").trim(),
        String(info.default_model || "").trim(),
      ].filter(Boolean));
      const models = visionOnly
        ? baseModels.filter((model) => modelSupportsImageInput(id, model))
        : baseModels;
      return {
        id,
        name: PLATFORM_LABELS[id] || id,
        models,
      };
    })
    .filter((item) => item.models.length > 0)
    .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));

  if (currentPlatform && currentModel) {
    const existing = options.find((item) => item.id === currentPlatform);
    if (existing) {
      existing.models = withCurrentModel(existing.models, currentModel);
    }
  }

  return options;
}

function buildApiModeModelState(platformKeys: Record<string, PlatformKeyInfo> = {}): Record<string, ApiModeModelState> {
  return Object.fromEntries(
    API_MODE_SPLIT_PLATFORM_IDS
      .filter((id) => !!platformKeys[id]?.supports_split_models)
      .map((id) => [
        id,
        {
          fastModel: String(platformKeys[id]?.api_fast_model || "").trim(),
          deepModel: String(platformKeys[id]?.api_deep_model || "").trim(),
        },
      ]),
  );
}

const SCREENSHOT_TEMPLATE_DEFAULTS = {
  title: "{platform}",
  subtitle: "{brand}",
  footer: "MONITOR SNAPSHOT",
  accentColor: "#14C7F3",
  backgroundStart: "#FCFDFF",
  backgroundEnd: "#F7FAFF",
  headerStart: "#173A43",
  headerEnd: "#14C7F3",
  outerPadding: "28",
  headerHeight: "152",
  radius: "28",
  imageRadius: "22",
};

const DEFAULT_LOCAL_MODEL_CONFIG: LocalModelFormState = {
  defaultModel: "gemma4:e2b",
  binaryPath: "",
  baseUrl: "http://127.0.0.1:11434",
  autoStart: true,
  autoPull: true,
  autoPrepareOnLaunch: true,
};

const EMPTY_LOCAL_MODEL_STATUS: LocalModelStatus = {
  provider: "ollama",
  base_url: DEFAULT_LOCAL_MODEL_CONFIG.baseUrl,
  healthy: false,
  status: "idle",
  status_message: "本地模型未启动",
  last_error: "",
  binary_path: "",
  binary_source: "missing",
  bundled_binary_path: "",
  bundled_binary_available: false,
  runtime_binary_path: "",
  runtime_binary_prepared: false,
  service_process_running: false,
  pull_process_running: false,
  pulling_model: "",
  pulling_model_ready: null,
  models: [],
  runtime_models_path: "",
  bundled_models_path: "",
  bundled_models_available: false,
  bundled_models_seeded: false,
};

const LOCAL_MODEL_STATUS_META: Record<string, { label: string; className: string }> = {
  ready: {
    label: "已就绪",
    className: "text-emerald-700 bg-emerald-50 border border-emerald-200",
  },
  preparing: {
    label: "准备中",
    className: "text-amber-700 bg-amber-50 border border-amber-200",
  },
  starting: {
    label: "启动中",
    className: "text-blue-700 bg-blue-50 border border-blue-200",
  },
  error: {
    label: "异常",
    className: "text-red-700 bg-red-50 border border-red-200",
  },
  idle: {
    label: "未启动",
    className: "text-gray-700 bg-gray-100 border border-gray-200",
  },
};

function withCurrentModel(models: string[], current: string): string[] {
  if (!current) return models;
  return models.includes(current) ? models : [current, ...models];
}

function formatSnapshotTime(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) return "";
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatWeatherSnapshotLine(snapshot: Record<string, unknown> | undefined): string {
  if (!snapshot) return "";
  const summary = String(snapshot.summary || "").trim();
  const city = String(snapshot.city || "").trim();
  if (city && summary) return `${city} · ${summary}`;
  return city || summary;
}

function formatCalendarSnapshotLine(snapshot: Record<string, unknown> | undefined): string {
  if (!snapshot) return "";
  const holidayName = String(snapshot.holiday_name || "").trim();
  const isHoliday = Boolean(snapshot.is_holiday);
  const isMakeupWorkday = Boolean(snapshot.is_makeup_workday);
  const daysUntilRaw = snapshot.days_until;
  const daysUntil = typeof daysUntilRaw === "number" ? daysUntilRaw : Number(daysUntilRaw);

  if (!holidayName) {
    return "";
  }
  if (isMakeupWorkday) {
    return `${holidayName} 调休上班`;
  }
  if (isHoliday) {
    return `${holidayName} 假期中`;
  }
  if (!Number.isNaN(daysUntil)) {
    return `${holidayName} 还有 ${daysUntil} 天`;
  }
  return holidayName;
}

function normalizeLocalModelStatus(value: unknown): LocalModelStatus {
  const source = (value && typeof value === "object") ? value as Partial<LocalModelStatus> : {};
  return {
    ...EMPTY_LOCAL_MODEL_STATUS,
    ...source,
    provider: String(source.provider || EMPTY_LOCAL_MODEL_STATUS.provider),
    base_url: String(source.base_url || EMPTY_LOCAL_MODEL_STATUS.base_url),
    status: String(source.status || EMPTY_LOCAL_MODEL_STATUS.status),
    status_message: String(source.status_message || EMPTY_LOCAL_MODEL_STATUS.status_message),
    last_error: String(source.last_error || ""),
    binary_path: String(source.binary_path || ""),
    binary_source: String(source.binary_source || EMPTY_LOCAL_MODEL_STATUS.binary_source || "missing"),
    bundled_binary_path: String(source.bundled_binary_path || ""),
    bundled_binary_available: Boolean(source.bundled_binary_available),
    runtime_binary_path: String(source.runtime_binary_path || ""),
    runtime_binary_prepared: Boolean(source.runtime_binary_prepared),
    healthy: Boolean(source.healthy),
    service_process_running: Boolean(source.service_process_running),
    pull_process_running: Boolean(source.pull_process_running),
    pulling_model: String(source.pulling_model || ""),
    pulling_model_ready: typeof source.pulling_model_ready === "boolean" ? source.pulling_model_ready : null,
    models: Array.isArray(source.models) ? source.models.map((item) => String(item || "").trim()).filter(Boolean) : [],
    runtime_models_path: String(source.runtime_models_path || ""),
    bundled_models_path: String(source.bundled_models_path || ""),
    bundled_models_available: Boolean(source.bundled_models_available),
    bundled_models_seeded: Boolean(source.bundled_models_seeded),
  };
}

function normalizeLocalModelConfig(configValue: unknown, statusValue?: unknown): LocalModelFormState {
  const config = (configValue && typeof configValue === "object") ? configValue as Record<string, unknown> : {};
  const status = normalizeLocalModelStatus(statusValue);
  return {
    defaultModel: String(config.default_model || DEFAULT_LOCAL_MODEL_CONFIG.defaultModel).trim() || DEFAULT_LOCAL_MODEL_CONFIG.defaultModel,
    binaryPath: String(config.binary_path || "").trim(),
    baseUrl: String(config.base_url || status.base_url || DEFAULT_LOCAL_MODEL_CONFIG.baseUrl).trim() || DEFAULT_LOCAL_MODEL_CONFIG.baseUrl,
    autoStart: config.auto_start === undefined ? DEFAULT_LOCAL_MODEL_CONFIG.autoStart : Boolean(config.auto_start),
    autoPull: config.auto_pull === undefined ? DEFAULT_LOCAL_MODEL_CONFIG.autoPull : Boolean(config.auto_pull),
    autoPrepareOnLaunch: config.auto_prepare_on_launch === undefined
      ? DEFAULT_LOCAL_MODEL_CONFIG.autoPrepareOnLaunch
      : Boolean(config.auto_prepare_on_launch),
  };
}

export function SettingsContent({
  localOCR,
  setLocalOCR,
  onSaveSuccess,
  onLogout,
}: {
  localOCR?: boolean,
  setLocalOCR?: (val: boolean) => void,
  onSaveSuccess?: (message?: string) => void,
  onLogout?: () => void,
}) {
  const normalizeBrowserAnswerMode = useCallback((value: unknown) => {
    const mode = String(value || "page").trim().toLowerCase();
    return mode === "dom" ? "dom" : "page";
  }, []);
  const [platformKeys, setPlatformKeys] = useState<Record<string, PlatformKeyInfo>>({});
  const [activeDays, setActiveDays] = useState<ActiveDaysState>({
    0: true, 1: true, 2: true, 3: true, 4: true, 5: false, 6: false
  });
  const [times, setTimes] = useState(
    [0, 1, 2, 3, 4, 5, 6].reduce((acc, day) => ({ ...acc, [day]: { h: "09", m: "00" } }), {} as Record<number, {h:string, m:string}>)
  );
  
  const [autoFallback, setAutoFallback] = useState(false);
  const [schedulerNotificationWebhook, setSchedulerNotificationWebhook] = useState("");
  const [failureAlertThreshold, setFailureAlertThreshold] = useState("7");
  const [failureAlertCooldownMinutes, setFailureAlertCooldownMinutes] = useState("5");
  const [schedulerWebhookTesting, setSchedulerWebhookTesting] = useState(false);
  const [schedulerWebhookTestMessage, setSchedulerWebhookTestMessage] = useState("");
  const [internalLocalOCR, setInternalLocalOCR] = useState(true);
  
  // Use passed prop or fallback to internal state
  const isLocalOCREnabled = localOCR !== undefined ? localOCR : internalLocalOCR;
  
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);
  const setLocalOCRHandler = setLocalOCR || setInternalLocalOCR;

  const [aiAssist, setAiAssist] = useState(false);
  const [tavilyKey, setTavilyKey] = useState("");
  const [localModelConfig, setLocalModelConfig] = useState<LocalModelFormState>(DEFAULT_LOCAL_MODEL_CONFIG);
  const [localModelStatus, setLocalModelStatus] = useState<LocalModelStatus>(EMPTY_LOCAL_MODEL_STATUS);
  const [localModelActionMessage, setLocalModelActionMessage] = useState("");
  const [localModelPreparing, setLocalModelPreparing] = useState(false);
  const [localModelRefreshing, setLocalModelRefreshing] = useState(false);
  const [localModelTesting, setLocalModelTesting] = useState(false);
  const [localModelTestReply, setLocalModelTestReply] = useState("");
  const [browserAnswerMode, setBrowserAnswerMode] = useState("page");
  
  const [screenshotEnabled, setScreenshotEnabled] = useState(true);
  const [showTime, setShowTime] = useState(true);
  const [showFooter, setShowFooter] = useState(true);
  const [showHighlight, setShowHighlight] = useState(true);
  const [screenshotTitle, setScreenshotTitle] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.title);
  const [screenshotSubtitle, setScreenshotSubtitle] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.subtitle);
  const [screenshotFooter, setScreenshotFooter] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.footer);
  const [accentColor, setAccentColor] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.accentColor);
  const [backgroundStart, setBackgroundStart] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.backgroundStart);
  const [backgroundEnd, setBackgroundEnd] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.backgroundEnd);
  const [headerStart, setHeaderStart] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.headerStart);
  const [headerEnd, setHeaderEnd] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.headerEnd);
  const [outerPadding, setOuterPadding] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.outerPadding);
  const [headerHeight, setHeaderHeight] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.headerHeight);
  const [radius, setRadius] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.radius);
  const [imageRadius, setImageRadius] = useState(SCREENSHOT_TEMPLATE_DEFAULTS.imageRadius);
  const [weatherReminderEnabled, setWeatherReminderEnabled] = useState(true);
  const [weatherCity, setWeatherCity] = useState("");
  const [weatherToken, setWeatherToken] = useState("");
  const [weatherRefreshMinutes, setWeatherRefreshMinutes] = useState("180");
  const [calendarReminderEnabled, setCalendarReminderEnabled] = useState(true);
  const [calendarRefreshMinutes, setCalendarRefreshMinutes] = useState("720");
  const [weatherSnapshotLine, setWeatherSnapshotLine] = useState("");
  const [weatherSnapshotTime, setWeatherSnapshotTime] = useState("");
  const [weatherSnapshotError, setWeatherSnapshotError] = useState("");
  const [calendarSnapshotLine, setCalendarSnapshotLine] = useState("");
  const [calendarSnapshotTime, setCalendarSnapshotTime] = useState("");
  const [calendarSnapshotError, setCalendarSnapshotError] = useState("");
  const [contextRefreshMessage, setContextRefreshMessage] = useState("");
  const [refreshingContext, setRefreshingContext] = useState(false);
  const [appVersionLabel, setAppVersionLabel] = useState("v2026.04.12");
  const [updateChannel, setUpdateChannel] = useState("stable");
  const [updateManifestUrl, setUpdateManifestUrl] = useState("");
  const [updateDownloadPageUrl, setUpdateDownloadPageUrl] = useState("");
  const [updateAutoCheckEnabled, setUpdateAutoCheckEnabled] = useState(false);
  const [updateCheckPending, setUpdateCheckPending] = useState(false);
  const [updatePreparePending, setUpdatePreparePending] = useState(false);
  const [updateCheckMessage, setUpdateCheckMessage] = useState("");
  const [updateAvailable, setUpdateAvailable] = useState<boolean | null>(null);
  const [updateLatestLabel, setUpdateLatestLabel] = useState("");
  const [updatePublishedAt, setUpdatePublishedAt] = useState("");
  const [updateDownloadUrl, setUpdateDownloadUrl] = useState("");
  const [updatePackageSha256, setUpdatePackageSha256] = useState("");
  const [updateNotes, setUpdateNotes] = useState("");
  const [localUpdateSourceDir, setLocalUpdateSourceDir] = useState("");
  const [localUpdateCleanupSource, setLocalUpdateCleanupSource] = useState(true);
  const [localUpdateRestartAfterInstall, setLocalUpdateRestartAfterInstall] = useState(true);
  const [localUpdateExitAfterLaunch, setLocalUpdateExitAfterLaunch] = useState(true);
  const [localUpdatePlanMessage, setLocalUpdatePlanMessage] = useState("");
  const [localUpdatePlanSummary, setLocalUpdatePlanSummary] = useState("");
  const [localUpdatePending, setLocalUpdatePending] = useState(false);

  // New states for models
  const [smartPlatform, setSmartPlatform] = useState("");
  const [smartModel, setSmartModel] = useState("");

  const [smartVisionEnabled, setSmartVisionEnabled] = useState(false);
  const [smartVisionPlatform, setSmartVisionPlatform] = useState("");
  const [smartVisionModel, setSmartVisionModel] = useState("");

  const [selectorAgentEnabled, setSelectorAgentEnabled] = useState(false);
  const [selectorAgentPlatform, setSelectorAgentPlatform] = useState("");
  const [selectorAgentModel, setSelectorAgentModel] = useState("");

  const [ocrPlatform, setOcrPlatform] = useState("");
  const [ocrModel, setOcrModel] = useState("");
  const [recognitionMode, setRecognitionMode] = useState("screenshot");
  const [floatingWindowResidentEnabled, setFloatingWindowResidentEnabled] = useState(false);
  const [queryExecutionSettings, setQueryExecutionSettings] = useState<Record<string, QueryExecutionModeSettings>>(
    () => buildQueryExecutionState(),
  );

  const [searchModels, setSearchModels] = useState<string[]>(["sonar-pro", "sonar"]);
  const [newSearchModel, setNewSearchModel] = useState("");
  const [apiModeModels, setApiModeModels] = useState<Record<string, ApiModeModelState>>(() => buildApiModeModelState());
  const [browserSelectors, setBrowserSelectors] = useState<Record<string, Record<string, string>>>(() => buildBrowserSelectorState());
  const [apiModePanelsOpen, setApiModePanelsOpen] = useState<Record<string, boolean>>({
    deepseek: false,
    tongyi: false,
    yuanbao: false,
  });
  const [browserPanelsOpen, setBrowserPanelsOpen] = useState<Record<string, boolean>>({
    doubao: false,
    deepseek: false,
    kimi: false,
    yuanbao: false,
    tongyi: false,
    wenxin: false,
  });
  const [browserAdvancedPanelsOpen, setBrowserAdvancedPanelsOpen] = useState<Record<string, boolean>>({
    doubao: false,
    deepseek: false,
    kimi: false,
    yuanbao: false,
    tongyi: false,
    wenxin: false,
  });
  const [browserAuthPlatforms, setBrowserAuthPlatforms] = useState<Record<string, BrowserAuthPlatformSnapshot>>({});
  const [browserAuthPendingKey, setBrowserAuthPendingKey] = useState("");
  const [browserAuthMessages, setBrowserAuthMessages] = useState<Record<string, string>>({});
  const [selectorHealResult, setSelectorHealResult] = useState<SelectorHealFieldResult | null>(null);
  const [selectorHealMessage, setSelectorHealMessage] = useState("");
  const [selectorHealDiagnosing, setSelectorHealDiagnosing] = useState(false);
  const [selectorPauseStateResult, setSelectorPauseStateResult] = useState<SelectorPauseStateResponse | null>(null);
  const [selectorPauseStateMessage, setSelectorPauseStateMessage] = useState("");
  const [selectorPauseStateDiagnosing, setSelectorPauseStateDiagnosing] = useState(false);

  const handleAddSearchModel = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && newSearchModel.trim()) {
      e.preventDefault();
      if (!searchModels.includes(newSearchModel.trim())) {
        setSearchModels([...searchModels, newSearchModel.trim()]);
      }
      setNewSearchModel("");
    }
  };

  const removeSearchModel = (modelToRemove: string) => {
    setSearchModels(searchModels.filter(m => m !== modelToRemove));
  };

  const toggleApiModePanel = useCallback((platformId: string) => {
    setApiModePanelsOpen((prev) => ({ ...prev, [platformId]: !prev[platformId] }));
  }, []);

  const toggleBrowserPanel = useCallback((platformId: string) => {
    setBrowserPanelsOpen((prev) => ({ ...prev, [platformId]: !prev[platformId] }));
  }, []);

  const toggleBrowserAdvancedPanel = useCallback((platformId: string) => {
    setBrowserAdvancedPanelsOpen((prev) => ({ ...prev, [platformId]: !prev[platformId] }));
  }, []);

  const updateBrowserSelector = useCallback((platformId: string, field: string, value: string) => {
    setBrowserSelectors((prev) => ({
      ...prev,
      [platformId]: {
        ...(prev[platformId] || BROWSER_SELECTOR_DEFAULTS[platformId as BrowserAutomationPlatformId] || {}),
        [field]: value,
      },
    }));
  }, []);

  const updateApiModeModel = useCallback((platformId: string, mode: keyof ApiModeModelState, value: string) => {
    setApiModeModels((prev) => ({
      ...prev,
      [platformId]: {
        fastModel: prev[platformId]?.fastModel || "",
        deepModel: prev[platformId]?.deepModel || "",
        [mode]: value,
      },
    }));
  }, []);

  const updateQueryExecutionSetting = useCallback((
    mode: string,
    key: keyof QueryExecutionModeSettings,
    value: string | boolean,
  ) => {
    setQueryExecutionSettings((prev) => ({
      ...prev,
      [mode]: {
        ...(prev[mode] || QUERY_EXECUTION_DEFAULTS[mode as keyof typeof QUERY_EXECUTION_DEFAULTS]),
        [key]: value,
      },
    }));
  }, []);

  const applyContextSnapshotPayload = useCallback((data: Record<string, unknown>) => {
    const contextSnapshots = (data.context_snapshots as Record<string, unknown> | undefined) || {};
    const weatherSettings = (contextSnapshots.weather as Record<string, unknown> | undefined) || {};
    const calendarSettings = (contextSnapshots.calendar as Record<string, unknown> | undefined) || {};
    const weatherSnapshot = (data.weather_snapshot as Record<string, unknown> | undefined) || {};
    const calendarSnapshot = (data.calendar_snapshot as Record<string, unknown> | undefined) || {};

    setWeatherReminderEnabled(weatherSettings.enabled !== false);
    setWeatherCity(String(weatherSettings.city || "").trim());
    setWeatherToken(String(weatherSettings.token || "").trim());
    if (weatherSettings.ttl_minutes !== undefined) {
      setWeatherRefreshMinutes(String(weatherSettings.ttl_minutes || 180));
    }
    setCalendarReminderEnabled(calendarSettings.enabled !== false);
    if (calendarSettings.ttl_minutes !== undefined) {
      setCalendarRefreshMinutes(String(calendarSettings.ttl_minutes || 720));
    }

    setWeatherSnapshotLine(formatWeatherSnapshotLine(weatherSnapshot));
    setWeatherSnapshotTime(formatSnapshotTime(weatherSnapshot.updated_at));
    setWeatherSnapshotError(String(weatherSettings.last_error || "").trim());
    setCalendarSnapshotLine(formatCalendarSnapshotLine(calendarSnapshot));
    setCalendarSnapshotTime(formatSnapshotTime(calendarSnapshot.updated_at));
    setCalendarSnapshotError(String(calendarSettings.last_error || "").trim());
  }, []);

  const applyBrowserAuthPayload = useCallback((payload?: Record<string, BrowserAuthPlatformSnapshot> | null) => {
    setBrowserAuthPlatforms(payload || {});
  }, []);

  const applyUpdateStatusPayload = useCallback((payload?: AppUpdateStatusSnapshot | Record<string, unknown> | null) => {
    const status = (payload || {}) as AppUpdateStatusSnapshot;
    const current = (status.current || {}) as AppUpdateStatusSnapshot["current"];
    const settings = (status.settings || {}) as AppUpdateStatusSnapshot["settings"];
    const latest = (status.latest || null) as AppUpdateStatusSnapshot["latest"];

    if (current?.label) {
      setAppVersionLabel(String(current.label));
    }
    if (settings) {
      if (settings.channel) setUpdateChannel(String(settings.channel));
      setUpdateManifestUrl(String(settings.manifest_url || ""));
      setUpdateDownloadPageUrl(String(settings.download_page_url || ""));
      setUpdateAutoCheckEnabled(Boolean(settings.auto_check_enabled));
    }
    setUpdateCheckMessage(String(status.message || ""));
    setUpdateAvailable(typeof status.update_available === "boolean" ? status.update_available : null);
    setUpdateLatestLabel(String(latest?.label || ""));
    setUpdatePublishedAt(String(latest?.published_at || ""));
    setUpdateDownloadUrl(String(status.download_url || status.download_page_url || ""));
    setUpdatePackageSha256(String(status.package_sha256 || ""));
    setUpdateNotes(String(latest?.notes || ""));
  }, []);

  // Profile states
  const [isEditingProfile, setIsEditingProfile] = useState(false);
  const [profile, setProfile] = useState({
    name: "林见鹿",
    role: "资深 AI 运营",
    birthday: "1996-08-12",
    hireDate: "2022-10-24",
    avatar: DEFAULT_PROFILE_AVATAR,
  });
  const [editProfile, setEditProfile] = useState(profile);
  const [avatarUploadMessage, setAvatarUploadMessage] = useState("");
  const [avatarCropDraft, setAvatarCropDraft] = useState<AvatarCropDraft | null>(null);
  const avatarDragRef = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null);
  const [accountCrawlEnabled, setAccountCrawlEnabled] = useState(false);
  const [accountCrawlFrequencyMinutes, setAccountCrawlFrequencyMinutes] = useState("60");
  const [accountCrawlMaxItems, setAccountCrawlMaxItems] = useState("20");
  const [accountCrawlRsshubBaseUrl, setAccountCrawlRsshubBaseUrl] = useState("https://rsshub.app");
  const [articleExportShowKeywordCategory, setArticleExportShowKeywordCategory] = useState(false);
  const [articleExportShowSelfMediaAccount, setArticleExportShowSelfMediaAccount] = useState(true);
  const [accountExcludedLinks, setAccountExcludedLinks] = useState<ExcludedArticleLinkSnapshot[]>([]);
  const [accountExcludedTotal, setAccountExcludedTotal] = useState(0);
  const [accountExclusionsOpen, setAccountExclusionsOpen] = useState(false);
  const [accountExclusionSelectedUrls, setAccountExclusionSelectedUrls] = useState<string[]>([]);
  const [accountExclusionLoading, setAccountExclusionLoading] = useState(false);
  const [accountExclusionRestoring, setAccountExclusionRestoring] = useState(false);
  const [accountExclusionMessage, setAccountExclusionMessage] = useState("");
  const [savingScope, setSavingScope] = useState<string | null>(null);
  const [settingsReady, setSettingsReady] = useState(false);
  const lastSavedSettingsRef = useRef<Record<string, string>>({});
  const settingsSnapshotInitializedRef = useRef(false);

  const refreshAccountExclusions = useCallback(async (silent = false) => {
    if (!silent) {
      setAccountExclusionLoading(true);
      setAccountExclusionMessage("");
    }
    try {
      const result = await fetchAccountCrawlExclusions();
      setAccountExcludedLinks(result.excluded_links || []);
      setAccountExcludedTotal(Number(result.total || result.excluded_links?.length || 0));
      setAccountExclusionSelectedUrls((current) => (
        current.filter((url) => (result.excluded_links || []).some((link) => getExcludedArticleUrl(link) === url))
      ));
      if (!result.ok && !silent) {
        setAccountExclusionMessage(result.message || "获取排除项失败");
      }
    } finally {
      if (!silent) {
        setAccountExclusionLoading(false);
      }
    }
  }, []);

  const applySettingsPayload = useCallback((
    data: Record<string, unknown>,
    platformKeyData?: Record<string, PlatformKeyInfo>,
    browserAuthData?: { platforms: Record<string, BrowserAuthPlatformSnapshot> },
  ) => {
      settingsSnapshotInitializedRef.current = false;
      if (platformKeyData && Object.keys(platformKeyData).length > 0) {
        setPlatformKeys(platformKeyData);
        setApiModeModels(buildApiModeModelState(platformKeyData));
      }
      if (browserAuthData?.platforms) {
        applyBrowserAuthPayload(browserAuthData.platforms);
      }
      if (!data || Object.keys(data).length === 0) {
        setSettingsReady(true);
        return;
      }

      const version = data.version as Record<string, unknown> | undefined;
      if (version?.label) {
        setAppVersionLabel(String(version.label));
      }
      const appUpdate = data.app_update as Record<string, unknown> | undefined;
      if (appUpdate) {
        if (appUpdate.channel) setUpdateChannel(String(appUpdate.channel));
        if (appUpdate.manifest_url !== undefined) setUpdateManifestUrl(String(appUpdate.manifest_url || ""));
        if (appUpdate.download_page_url !== undefined) setUpdateDownloadPageUrl(String(appUpdate.download_page_url || ""));
        if (typeof appUpdate.auto_check_enabled === "boolean") setUpdateAutoCheckEnabled(appUpdate.auto_check_enabled);
      }
      applyUpdateStatusPayload(data.update_status as Record<string, unknown> | undefined);

      // scheduler.weekly_times -> activeDays + times
      const wt = (data.scheduler as Record<string, unknown> | undefined)?.weekly_times as Record<string, string | null> | undefined;
      if (wt) {
        const newActiveDays = {} as ActiveDaysState;
        const newTimes: Record<number, { h: string; m: string }> = {};
        for (const day of SCHEDULER_DAYS) {
          const val = wt[String(day)];
          if (val && typeof val === "string") {
            newActiveDays[day] = true;
            const parts = val.split(":");
            newTimes[day] = { h: (parts[0] || "09").padStart(2, "0"), m: (parts[1] || "00").padStart(2, "0") };
          } else {
            newActiveDays[day] = false;
            newTimes[day] = { h: "09", m: "00" };
          }
        }
        setActiveDays(newActiveDays);
        setTimes(newTimes);
      }
      const scheduler = data.scheduler as Record<string, unknown> | undefined;
      if (scheduler && typeof scheduler.auto_continue_after_default_failure === "boolean") {
        setAutoFallback(scheduler.auto_continue_after_default_failure);
      }
      if (scheduler && scheduler.notification_webhook_url !== undefined) {
        setSchedulerNotificationWebhook(String(scheduler.notification_webhook_url || ""));
      }
      const defaultNotification = data.default_notification as Record<string, unknown> | undefined;
      if (defaultNotification) {
        setFailureAlertThreshold(String(defaultNotification.failure_alert_threshold ?? "7"));
        setFailureAlertCooldownMinutes(String(defaultNotification.failure_alert_cooldown_minutes ?? "5"));
      }
      const accountCrawling = data.account_crawling as Record<string, unknown> | undefined;
      if (accountCrawling) {
        setAccountCrawlEnabled(Boolean(accountCrawling.enabled));
        setAccountCrawlFrequencyMinutes(String(accountCrawling.frequency_minutes ?? "60"));
        setAccountCrawlMaxItems(String(accountCrawling.max_items_per_account ?? "20"));
        const excludedLinks = Array.isArray(accountCrawling.excluded_links)
          ? accountCrawling.excluded_links as ExcludedArticleLinkSnapshot[]
          : [];
        setAccountExcludedLinks(excludedLinks);
        setAccountExcludedTotal(excludedLinks.length);
        void refreshAccountExclusions(true);
        const rsshubBaseUrls = Array.isArray(accountCrawling.rsshub_base_urls)
          ? accountCrawling.rsshub_base_urls.map((item) => String(item || "").trim()).filter(Boolean)
          : [];
        setAccountCrawlRsshubBaseUrl(
          rsshubBaseUrls.length
            ? rsshubBaseUrls.join(", ")
            : String(accountCrawling.rsshub_base_url || "https://rsshub.app"),
        );
      }
      const articleExport = data.article_export as Record<string, unknown> | undefined;
      if (articleExport) {
        setArticleExportShowKeywordCategory(Boolean(articleExport.show_keyword_category));
        setArticleExportShowSelfMediaAccount(Boolean(articleExport.show_selfmedia_account ?? true));
      }

      // ai_assistant -> smartPlatform, smartModel
      const ai = data.ai_assistant as Record<string, unknown> | undefined;
      if (ai) {
        if (ai.platform) setSmartPlatform(String(ai.platform));
        if (ai.model) setSmartModel(String(ai.model));
      }

      const smartVision = data.smart_vision as Record<string, unknown> | undefined;
      if (smartVision) {
        if (typeof smartVision.enabled === "boolean") setSmartVisionEnabled(smartVision.enabled);
        if (smartVision.platform) setSmartVisionPlatform(String(smartVision.platform));
        if (smartVision.model) setSmartVisionModel(String(smartVision.model));
      }

      const selectorAgent = data.selector_agent as SelectorAgentSettingsSnapshot | Record<string, unknown> | undefined;
      if (selectorAgent) {
        if (typeof selectorAgent.enabled === "boolean") setSelectorAgentEnabled(selectorAgent.enabled);
        if (selectorAgent.platform) setSelectorAgentPlatform(String(selectorAgent.platform));
        if (selectorAgent.model) setSelectorAgentModel(String(selectorAgent.model));
      }

      // recognition -> ocrPlatform, ocrModel
      const rec = data.recognition as Record<string, unknown> | undefined;
      if (rec) {
        if (typeof rec.safe_mode_ocr_enabled === "boolean") setLocalOCRHandler(rec.safe_mode_ocr_enabled);
        if (typeof rec.ai_fallback_enabled === "boolean") setAiAssist(rec.ai_fallback_enabled);
        if (rec.platform) setOcrPlatform(String(rec.platform));
        if (rec.model) setOcrModel(String(rec.model));
        if (typeof rec.floating_window_resident_enabled === "boolean") {
          setFloatingWindowResidentEnabled(rec.floating_window_resident_enabled);
        }
        if (typeof rec.dom_render_mode === "boolean") {
          setRecognitionMode(rec.dom_render_mode ? "dom" : "screenshot");
        }
      }

      const search = data.search as Record<string, unknown> | undefined;
      if (search) {
        if (search.tavily_api_key) setTavilyKey(String(search.tavily_api_key));
        if (Array.isArray(search.model_pool)) {
          const models = search.model_pool.map((item) => String(item || "").trim()).filter(Boolean);
          if (models.length > 0) setSearchModels(models);
        }
      } else {
        const tav = data.tavily as Record<string, unknown> | undefined;
        if (tav && tav.api_key) setTavilyKey(String(tav.api_key));
      }

      setLocalModelConfig(normalizeLocalModelConfig(
        data.local_model as Record<string, unknown> | undefined,
        data.local_model_status,
      ));
      if (data.local_model_status) {
        setLocalModelStatus(normalizeLocalModelStatus(data.local_model_status));
      }

      const browserAutomation = data.browser_automation as Record<string, unknown> | undefined;
      if (browserAutomation) {
        setBrowserSelectors(() => {
          const next = buildBrowserSelectorState();
          for (const platformId of BROWSER_AUTOMATION_PLATFORM_IDS) {
            const platformAutomation = (browserAutomation[platformId] as Record<string, unknown> | undefined) || {};
            for (const field of BROWSER_AUTOMATION_CONFIGS[platformId].fields) {
              const candidateKeys = [field.key, ...(field.legacyKeys || [])];
              const matchedKey = candidateKeys.find((key) => platformAutomation[key] !== undefined);
              if (matchedKey !== undefined) {
                next[platformId][field.key] = String(platformAutomation[matchedKey] || "");
              }
            }
          }
          return next;
        });
        setBrowserAdvancedPanelsOpen(() => {
          const next = {
            doubao: false,
            deepseek: false,
            kimi: false,
            yuanbao: false,
            tongyi: false,
            wenxin: false,
          } as Record<string, boolean>;
          for (const platformId of BROWSER_AUTOMATION_PLATFORM_IDS) {
            const platformAutomation = (browserAutomation[platformId] as Record<string, unknown> | undefined) || {};
            next[platformId] = hasConfiguredAdvancedBrowserFields(
              BROWSER_AUTOMATION_CONFIGS[platformId].fields,
              Object.fromEntries(
                Object.entries(platformAutomation).map(([key, value]) => [key, String(value || "")]),
              ),
            );
          }
          return next;
        });
      }

      const browserAuth = data.browser_auth as Record<string, BrowserAuthPlatformSnapshot> | undefined;
      if (browserAuth) {
        applyBrowserAuthPayload(browserAuth);
      }

      const queryExecution = data.query_execution as Record<string, Record<string, unknown>> | undefined;
      if (queryExecution) {
        setQueryExecutionSettings(() => {
          const next = buildQueryExecutionState();
          for (const mode of QUERY_EXECUTION_MODE_IDS) {
            const current = (queryExecution[mode] as Record<string, unknown> | undefined) || {};
            next[mode] = {
              strategy: normalizeQueryExecutionStrategy(current.strategy || next[mode].strategy),
              sessionPoolDispatch: normalizeSessionPoolDispatch(current.session_pool_dispatch || next[mode].sessionPoolDispatch),
              sessionPoolPlatformBatchSize: String(current.session_pool_platform_batch_size ?? next[mode].sessionPoolPlatformBatchSize),
              sessionTtlMinutesMin: String(current.session_ttl_minutes_min ?? next[mode].sessionTtlMinutesMin),
              sessionTtlMinutesMax: String(current.session_ttl_minutes_max ?? next[mode].sessionTtlMinutesMax),
              sessionMaxQueriesMin: String(current.session_max_queries_min ?? next[mode].sessionMaxQueriesMin),
              sessionMaxQueriesMax: String(current.session_max_queries_max ?? next[mode].sessionMaxQueriesMax),
              minQueriesWindowMinutes: String(current.min_queries_window_minutes ?? next[mode].minQueriesWindowMinutes),
              minQueriesPerWindow: String(current.min_queries_per_window ?? next[mode].minQueriesPerWindow),
              singleQueryTimeoutMinutes: String(current.single_query_timeout_minutes ?? next[mode].singleQueryTimeoutMinutes),
              noProgressTimeoutMinutes: String(current.no_progress_timeout_minutes ?? next[mode].noProgressTimeoutMinutes),
              minRestartCooldownMinutes: String(current.min_restart_cooldown_minutes ?? next[mode].minRestartCooldownMinutes),
              restartAfterManualRecovery: current.restart_after_manual_recovery === undefined
                ? next[mode].restartAfterManualRecovery
                : Boolean(current.restart_after_manual_recovery),
              restartAfterStructuralFailures: String(current.restart_after_structural_failures ?? next[mode].restartAfterStructuralFailures),
            };
          }
          return next;
        });
      }

      applyContextSnapshotPayload(data as Record<string, unknown>);

      const screenshotConfig = data.screenshot as Record<string, unknown> | undefined;
      setBrowserAnswerMode(normalizeBrowserAnswerMode(screenshotConfig?.browser_answer_mode));

      // screenshot_template -> screenshotEnabled, showTime, showFooter, showHighlight
      const ss = data.screenshot_template as Record<string, unknown> | undefined;
      if (ss) {
        if (typeof ss.enabled === "boolean") setScreenshotEnabled(ss.enabled);
        if (typeof ss.show_time === "boolean") setShowTime(ss.show_time);
        if (typeof ss.show_footer === "boolean") setShowFooter(ss.show_footer);
        if (typeof ss.show_highlight === "boolean") setShowHighlight(ss.show_highlight);
        if (ss.title) setScreenshotTitle(String(ss.title));
        if (ss.subtitle) setScreenshotSubtitle(String(ss.subtitle));
        if (ss.footer) setScreenshotFooter(String(ss.footer));
        if (ss.accent_color) setAccentColor(String(ss.accent_color));
        if (ss.background_start) setBackgroundStart(String(ss.background_start));
        if (ss.background_end) setBackgroundEnd(String(ss.background_end));
        if (ss.header_start) setHeaderStart(String(ss.header_start));
        if (ss.header_end) setHeaderEnd(String(ss.header_end));
        if (ss.outer_padding !== undefined) setOuterPadding(String(ss.outer_padding));
        if (ss.header_height !== undefined) setHeaderHeight(String(ss.header_height));
        if (ss.radius !== undefined) setRadius(String(ss.radius));
        if (ss.image_radius !== undefined) setImageRadius(String(ss.image_radius));
      }

      // profile -> profile state
      const prof = data.profile as Record<string, string> | undefined;
      if (prof) {
        setProfile((previousProfile) => {
          const newProfile = {
            name: prof.name || previousProfile.name,
            role: prof.role || previousProfile.role,
            birthday: prof.birthday || previousProfile.birthday,
            hireDate: prof.hire_date || previousProfile.hireDate,
            avatar: prof.avatar || previousProfile.avatar,
          };
          setEditProfile(newProfile);
          return newProfile;
        });
      }
      setSettingsReady(true);
    }, [applyBrowserAuthPayload, applyContextSnapshotPayload, applyUpdateStatusPayload, normalizeBrowserAnswerMode, setLocalOCRHandler]);

  // Load settings from backend on mount. Use the last real settings payload immediately,
  // then refresh in the background so re-entering the page does not flash defaults.
  useEffect(() => {
    let cancelled = false;
    const cachedSettings = readSettingsCache();
    const hasCachedSettings = Boolean(cachedSettings && Object.keys(cachedSettings).length > 0);

    if (cachedSettings && hasCachedSettings) {
      applySettingsPayload(cachedSettings);
    }

    Promise.all([
      fetchSettings({ force: hasCachedSettings }),
      fetchPlatformKeys(),
      fetchBrowserAuth(),
    ]).then(([data, platformKeyData, browserAuthData]) => {
      if (cancelled) {
        return;
      }
      applySettingsPayload(data, platformKeyData, browserAuthData);
    });

    return () => {
      cancelled = true;
    };
  }, [applySettingsPayload]);

  const toggleAccountExcludedUrl = useCallback((url: string) => {
    const normalizedUrl = String(url || "").trim();
    if (!normalizedUrl) {
      return;
    }
    setAccountExclusionSelectedUrls((current) => (
      current.includes(normalizedUrl)
        ? current.filter((item) => item !== normalizedUrl)
        : [...current, normalizedUrl]
    ));
    setAccountExclusionMessage("");
  }, []);

  const restoreSelectedAccountExclusions = useCallback(async () => {
    const urls = accountExclusionSelectedUrls.filter(Boolean);
    if (!urls.length) {
      setAccountExclusionMessage("请先选择要恢复的排除项");
      return;
    }
    setAccountExclusionRestoring(true);
    setAccountExclusionMessage("");
    try {
      const result = await restoreAccountCrawlExclusions(urls);
      if (result.excluded_links) {
        setAccountExcludedLinks(result.excluded_links);
        setAccountExcludedTotal(Number(result.total || result.excluded_links.length || 0));
      } else {
        await refreshAccountExclusions(true);
      }
      if (result.ok) {
        setAccountExclusionSelectedUrls([]);
      }
      setAccountExclusionMessage(result.message || (result.ok ? "已恢复所选排除项" : "恢复排除项失败"));
    } finally {
      setAccountExclusionRestoring(false);
    }
  }, [accountExclusionSelectedUrls, refreshAccountExclusions]);

  const refreshLocalModelRuntimeStatus = useCallback(async (silent = false) => {
    if (!silent) {
      setLocalModelRefreshing(true);
      setLocalModelActionMessage("");
    }
    try {
      const result = await fetchLocalModelStatus();
      if (result.local_model) {
        setLocalModelStatus(normalizeLocalModelStatus(result.local_model));
      }
      if (!result.ok && !silent) {
        setLocalModelActionMessage(String(result.message || "获取本地模型状态失败"));
      }
    } finally {
      if (!silent) {
        setLocalModelRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refreshLocalModelRuntimeStatus(true);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [refreshLocalModelRuntimeStatus]);

  const aiPlatforms = useMemo(
    () => buildPlatformOptions(platformKeys, { currentPlatform: smartPlatform, currentModel: smartModel }),
    [platformKeys, smartPlatform, smartModel],
  );
  const apiModePlatforms = useMemo(
    () => API_MODE_SPLIT_PLATFORM_IDS
      .filter((id) => !!platformKeys[id]?.supports_split_models)
      .map((id) => {
        const info = platformKeys[id]!;
        return ({
        id,
        name: PLATFORM_LABELS[id] || id,
        hasKey: !!info.has_key,
        note: String(info.split_model_note || "").trim(),
        fastDefault: String(info.fast_model_default || "").trim() || String(info.default_model || "").trim(),
        deepDefault: String(info.deep_model_default || "").trim() || String(info.default_model || "").trim(),
        models: uniqStrings([
          String(info.api_fast_model || "").trim(),
          String(info.api_deep_model || "").trim(),
          String(info.api_model || "").trim(),
          ...((info.model_options || []).map((item) => String(item || "").trim())),
          String(info.fast_model_default || "").trim(),
          String(info.deep_model_default || "").trim(),
          String(info.default_model || "").trim(),
        ].filter(Boolean)),
      });
      })
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN")),
    [platformKeys],
  );
  const visionPlatforms = useMemo(
    () => buildPlatformOptions(platformKeys, { visionOnly: true, currentPlatform: smartVisionPlatform, currentModel: smartVisionModel }),
    [platformKeys, smartVisionPlatform, smartVisionModel],
  );
  const selectorAgentPlatforms = useMemo(
    () => buildPlatformOptions(platformKeys, { currentPlatform: selectorAgentPlatform, currentModel: selectorAgentModel }),
    [platformKeys, selectorAgentPlatform, selectorAgentModel],
  );
  const recognitionVisionPlatforms = useMemo(
    () => buildPlatformOptions(platformKeys, { visionOnly: true, currentPlatform: ocrPlatform, currentModel: ocrModel }),
    [platformKeys, ocrPlatform, ocrModel],
  );

  useEffect(() => {
    if (!smartPlatform && aiPlatforms[0]) {
      setSmartPlatform(aiPlatforms[0].id);
      setSmartModel(aiPlatforms[0].models[0] || "");
      return;
    }
    const current = aiPlatforms.find((item) => item.id === smartPlatform);
    if (!current && aiPlatforms[0]) {
      setSmartPlatform(aiPlatforms[0].id);
      setSmartModel(aiPlatforms[0].models[0] || "");
    } else if (current && !current.models.includes(smartModel)) {
      setSmartModel(current.models[0] || "");
    }
  }, [aiPlatforms, smartPlatform, smartModel]);

  useEffect(() => {
    if (!smartVisionPlatform && visionPlatforms[0]) {
      setSmartVisionPlatform(visionPlatforms[0].id);
      setSmartVisionModel(visionPlatforms[0].models[0] || "");
      return;
    }
    const current = visionPlatforms.find((item) => item.id === smartVisionPlatform);
    if (!current && visionPlatforms[0]) {
      setSmartVisionPlatform(visionPlatforms[0].id);
      setSmartVisionModel(visionPlatforms[0].models[0] || "");
    } else if (current && !current.models.includes(smartVisionModel)) {
      setSmartVisionModel(current.models[0] || "");
    }
  }, [visionPlatforms, smartVisionPlatform, smartVisionModel]);

  useEffect(() => {
    if (!ocrPlatform && recognitionVisionPlatforms[0]) {
      setOcrPlatform(recognitionVisionPlatforms[0].id);
      setOcrModel(recognitionVisionPlatforms[0].models[0] || "");
      return;
    }
    const current = recognitionVisionPlatforms.find((item) => item.id === ocrPlatform);
    if (!current && recognitionVisionPlatforms[0]) {
      setOcrPlatform(recognitionVisionPlatforms[0].id);
      setOcrModel(recognitionVisionPlatforms[0].models[0] || "");
    } else if (current && !current.models.includes(ocrModel)) {
      setOcrModel(current.models[0] || "");
    }
  }, [recognitionVisionPlatforms, ocrPlatform, ocrModel]);

  useEffect(() => {
    if (!selectorAgentPlatform && selectorAgentPlatforms[0]) {
      setSelectorAgentPlatform(selectorAgentPlatforms[0].id);
      setSelectorAgentModel(selectorAgentPlatforms[0].models[0] || "");
      return;
    }
    const current = selectorAgentPlatforms.find((item) => item.id === selectorAgentPlatform);
    if (!current && selectorAgentPlatforms[0]) {
      setSelectorAgentPlatform(selectorAgentPlatforms[0].id);
      setSelectorAgentModel(selectorAgentPlatforms[0].models[0] || "");
    } else if (current && !current.models.includes(selectorAgentModel)) {
      setSelectorAgentModel(current.models[0] || "");
    }
  }, [selectorAgentPlatforms, selectorAgentPlatform, selectorAgentModel]);

  const buildSchedulerSettingsPayload = useCallback(() => {
    const weekly_times: Record<string, string | null> = {};
    for (const day of [0, 1, 2, 3, 4, 5, 6]) {
      if (activeDays[day as keyof typeof activeDays]) {
        weekly_times[String(day)] = `${times[day as keyof typeof times].h}:${times[day as keyof typeof times].m}`;
      } else {
        weekly_times[String(day)] = null;
      }
    }
    return {
      scheduler: {
        weekly_times,
        auto_continue_after_default_failure: autoFallback,
        notification_webhook_url: schedulerNotificationWebhook,
      },
      default_notification: {
        failure_alert_threshold: parseInt(failureAlertThreshold, 10) || 7,
        failure_alert_cooldown_minutes: parseInt(failureAlertCooldownMinutes, 10) || 5,
      },
    };
  }, [
    activeDays,
    autoFallback,
    failureAlertCooldownMinutes,
    failureAlertThreshold,
    schedulerNotificationWebhook,
    times,
  ]);

  const buildAccountCrawlingSettingsPayload = useCallback(() => {
    const rsshubBaseUrls = accountCrawlRsshubBaseUrl
      .split(/[\s,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    return {
      account_crawling: {
        enabled: accountCrawlEnabled,
        frequency_minutes: parseInt(accountCrawlFrequencyMinutes, 10) || 60,
        max_items_per_account: parseInt(accountCrawlMaxItems, 10) || 20,
        rsshub_base_url: rsshubBaseUrls[0] || "https://rsshub.app",
        rsshub_base_urls: rsshubBaseUrls,
      },
    };
  }, [
    accountCrawlEnabled,
    accountCrawlFrequencyMinutes,
    accountCrawlMaxItems,
    accountCrawlRsshubBaseUrl,
  ]);

  const buildArticleExportSettingsPayload = useCallback(() => ({
    article_export: {
      show_keyword_category: articleExportShowKeywordCategory,
      show_selfmedia_account: articleExportShowSelfMediaAccount,
    },
  }), [articleExportShowKeywordCategory, articleExportShowSelfMediaAccount]);

  const buildRecognitionSettingsPayload = useCallback(() => ({
    recognition: {
      safe_mode_ocr_enabled: isLocalOCREnabled,
      ai_fallback_enabled: aiAssist,
      platform: ocrPlatform,
      model: ocrModel,
      dom_render_mode: recognitionMode === "dom",
      floating_window_resident_enabled: floatingWindowResidentEnabled,
    },
  }), [aiAssist, floatingWindowResidentEnabled, isLocalOCREnabled, ocrModel, ocrPlatform, recognitionMode]);

  const buildBrowserModeSettingsPayload = useCallback(() => ({
    screenshot: {
      browser_answer_mode: browserAnswerMode,
    },
    query_execution: {
      browser: buildQueryExecutionPayload({ browser: queryExecutionSettings.browser } as Record<string, QueryExecutionModeSettings>).browser,
    },
    browser_automation: Object.fromEntries(
      Object.entries(buildBrowserAutomationPayload(browserSelectors)),
    ),
  }), [browserAnswerMode, browserSelectors, queryExecutionSettings]);

  const buildSmartModeSettingsPayload = useCallback(() => ({
    ai_assistant: { platform: smartPlatform, model: smartModel },
    smart_vision: {
      enabled: smartVisionEnabled,
      platform: smartVisionPlatform,
      model: smartVisionModel,
    },
    query_execution: {
      smart: buildQueryExecutionPayload({ smart: queryExecutionSettings.smart } as Record<string, QueryExecutionModeSettings>).smart,
    },
  }), [queryExecutionSettings, smartModel, smartPlatform, smartVisionEnabled, smartVisionModel, smartVisionPlatform]);

  const buildSelectorAgentSettingsPayload = useCallback(() => ({
    selector_agent: {
      enabled: selectorAgentEnabled,
      platform: selectorAgentPlatform.trim(),
      model: selectorAgentModel.trim(),
    },
  }), [selectorAgentEnabled, selectorAgentModel, selectorAgentPlatform]);

  const buildSearchSettingsPayload = useCallback(() => ({
    search: {
      provider: "tavily",
      tavily_api_key: tavilyKey,
      model_pool: searchModels,
    },
  }), [searchModels, tavilyKey]);

  const buildLocalModelSettingsPayload = useCallback(() => ({
    local_model: {
      default_model: localModelConfig.defaultModel.trim() || DEFAULT_LOCAL_MODEL_CONFIG.defaultModel,
      binary_path: localModelConfig.binaryPath.trim(),
      base_url: localModelConfig.baseUrl.trim() || DEFAULT_LOCAL_MODEL_CONFIG.baseUrl,
      auto_start: localModelConfig.autoStart,
      auto_pull: localModelConfig.autoPull,
      auto_prepare_on_launch: localModelConfig.autoPrepareOnLaunch,
    },
  }), [localModelConfig]);

  const buildContextSnapshotSettingsPayload = useCallback(() => ({
    context_snapshots: {
      weather: {
        enabled: weatherReminderEnabled,
        city: weatherCity.trim(),
        token: weatherToken.trim(),
        ttl_minutes: parseInt(weatherRefreshMinutes, 10) || 180,
      },
      calendar: {
        enabled: calendarReminderEnabled,
        ttl_minutes: parseInt(calendarRefreshMinutes, 10) || 720,
      },
    },
  }), [
    calendarRefreshMinutes,
    calendarReminderEnabled,
    weatherCity,
    weatherRefreshMinutes,
    weatherReminderEnabled,
    weatherToken,
  ]);

  const buildAppUpdateSettingsPayload = useCallback(() => ({
    app_update: {
      channel: updateChannel,
      manifest_url: updateManifestUrl.trim(),
      download_page_url: updateDownloadPageUrl.trim(),
      auto_check_enabled: updateAutoCheckEnabled,
    },
  }), [updateAutoCheckEnabled, updateChannel, updateDownloadPageUrl, updateManifestUrl]);

  const buildScreenshotTemplatePayload = useCallback(() => ({
    screenshot_template: {
      enabled: screenshotEnabled,
      title: screenshotTitle,
      subtitle: screenshotSubtitle,
      footer: screenshotFooter,
      show_time: showTime,
      show_footer: showFooter,
      show_highlight: showHighlight,
      accent_color: accentColor,
      background_start: backgroundStart,
      background_end: backgroundEnd,
      header_start: headerStart,
      header_end: headerEnd,
      outer_padding: parseInt(outerPadding, 10) || 0,
      header_height: parseInt(headerHeight, 10) || 0,
      radius: parseInt(radius, 10) || 0,
      image_radius: parseInt(imageRadius, 10) || 0,
    },
  }), [
    accentColor,
    backgroundEnd,
    backgroundStart,
    headerEnd,
    headerHeight,
    headerStart,
    imageRadius,
    outerPadding,
    radius,
    screenshotEnabled,
    screenshotFooter,
    screenshotSubtitle,
    screenshotTitle,
    showFooter,
    showHighlight,
    showTime,
  ]);

  const buildApiModePayload = useCallback(() => (
    Object.fromEntries(
      apiModePlatforms.map((platform) => [
        platform.id,
        {
          api_fast_model: apiModeModels[platform.id]?.fastModel || "",
          api_deep_model: apiModeModels[platform.id]?.deepModel || "",
        },
      ]),
    )
  ), [apiModeModels, apiModePlatforms]);

  const captureCurrentSettingsSnapshot = useCallback((): Record<string, string> => ({
    scheduler: JSON.stringify(buildSchedulerSettingsPayload()),
    account_crawling: JSON.stringify(buildAccountCrawlingSettingsPayload()),
    article_export: JSON.stringify(buildArticleExportSettingsPayload()),
    browser: JSON.stringify(buildBrowserModeSettingsPayload()),
    recognition: JSON.stringify(buildRecognitionSettingsPayload()),
    api_mode: JSON.stringify(buildApiModePayload()),
    smart: JSON.stringify(buildSmartModeSettingsPayload()),
    selector_agent: JSON.stringify(buildSelectorAgentSettingsPayload()),
    search: JSON.stringify(buildSearchSettingsPayload()),
    local_model: JSON.stringify(buildLocalModelSettingsPayload()),
    context: JSON.stringify(buildContextSnapshotSettingsPayload()),
    app_update: JSON.stringify(buildAppUpdateSettingsPayload()),
    screenshot: JSON.stringify(buildScreenshotTemplatePayload()),
  }), [
    buildAccountCrawlingSettingsPayload,
    buildArticleExportSettingsPayload,
    buildAppUpdateSettingsPayload,
    buildApiModePayload,
    buildBrowserModeSettingsPayload,
    buildContextSnapshotSettingsPayload,
    buildLocalModelSettingsPayload,
    buildRecognitionSettingsPayload,
    buildSchedulerSettingsPayload,
    buildScreenshotTemplatePayload,
    buildSearchSettingsPayload,
    buildSmartModeSettingsPayload,
    buildSelectorAgentSettingsPayload,
  ]);

  useEffect(() => {
    if (!settingsReady || settingsSnapshotInitializedRef.current) {
      return;
    }
    lastSavedSettingsRef.current = captureCurrentSettingsSnapshot();
    settingsSnapshotInitializedRef.current = true;
  }, [captureCurrentSettingsSnapshot, settingsReady]);

  const dirtySectionCount = useMemo(() => {
    if (!settingsReady || !settingsSnapshotInitializedRef.current) {
      return 0;
    }
    const currentSnapshot = captureCurrentSettingsSnapshot();
    const previousSnapshot = lastSavedSettingsRef.current;
    return Object.keys(currentSnapshot).filter(
      (section) => currentSnapshot[section] !== previousSnapshot[section],
    ).length;
  }, [captureCurrentSettingsSnapshot, settingsReady]);

  const handleBrowserAuthAction = useCallback(async (
    action: string,
    platformId: string,
    profileId?: string,
  ) => {
    const pendingKey = `${platformId}:${action}:${profileId || ""}`;
    setBrowserAuthPendingKey(pendingKey);
    setBrowserAuthMessages((prev) => ({
      ...prev,
      [platformId]: "",
    }));
    try {
      const result = await browserAuthAction(action, platformId, profileId);
      if (result.browser_auth?.platforms) {
        applyBrowserAuthPayload(result.browser_auth.platforms);
      } else {
        const refreshed = await fetchBrowserAuth();
        applyBrowserAuthPayload(refreshed.platforms || {});
      }
      const message = String(result.message || (result.ok ? "操作已完成" : "操作失败"));
      setBrowserAuthMessages((prev) => ({
        ...prev,
        [platformId]: message,
      }));
      if (result.ok) {
        notifySaveSuccess(onSaveSuccess, message);
      }
    } finally {
      setBrowserAuthPendingKey("");
    }
  }, [applyBrowserAuthPayload, onSaveSuccess]);

  const syncSavedBrowserSelector = useCallback((platformId: string, field: string, selector: string) => {
    setBrowserSelectors((previousSelectors) => {
      const nextSelectors = {
        ...previousSelectors,
        [platformId]: {
          ...(previousSelectors[platformId] || BROWSER_SELECTOR_DEFAULTS[platformId as BrowserAutomationPlatformId] || {}),
          [field]: selector,
        },
      };
      lastSavedSettingsRef.current = {
        ...lastSavedSettingsRef.current,
        browser: JSON.stringify({
          screenshot: {
            browser_answer_mode: browserAnswerMode,
          },
          query_execution: {
            browser: buildQueryExecutionPayload({ browser: queryExecutionSettings.browser } as Record<string, QueryExecutionModeSettings>).browser,
          },
          browser_automation: Object.fromEntries(
            Object.entries(buildBrowserAutomationPayload(nextSelectors)),
          ),
        }),
      };
      return nextSelectors;
    });
  }, [browserAnswerMode, queryExecutionSettings.browser]);

  const handleDiagnoseDeepSeekNewChatSelector = useCallback(async () => {
    if (selectorHealDiagnosing || selectorPauseStateDiagnosing) {
      return;
    }
    setSelectorHealDiagnosing(true);
    setSelectorHealMessage("");
    setSelectorHealResult(null);
    try {
      const result = await diagnoseSelectorHeal({
        platform: "deepseek",
        fields: ["new_chat_selector"],
        verify: true,
        auto_apply: true,
      });
      const fieldResult = result.results?.[0] || null;
      if (fieldResult) {
        setSelectorHealResult(fieldResult);
      }
      if (!result.ok) {
        setSelectorHealMessage(result.blocking_reason || result.message || "诊断失败");
        return;
      }
      const selector = String(fieldResult?.selector || fieldResult?.verified_selector || "").trim();
      if (selector && fieldResult?.saved) {
        syncSavedBrowserSelector("deepseek", "new_chat_selector", selector);
      }
      setSelectorHealMessage(
        fieldResult?.save_error
          ? fieldResult.save_error
          : fieldResult?.saved
          ? "已找到并自动保存 selector"
          : fieldResult?.verify_status === "passed"
            ? "已找到通过验证的 selector，等待自动保存"
            : "诊断完成，未找到可直接保存的 selector",
      );
      if (fieldResult?.saved) {
        notifySaveSuccess(onSaveSuccess, "DeepSeek 新对话 selector 已保存");
      }
    } finally {
      setSelectorHealDiagnosing(false);
    }
  }, [onSaveSuccess, selectorHealDiagnosing, selectorPauseStateDiagnosing, syncSavedBrowserSelector]);

  const handleDiagnoseDeepSeekPauseState = useCallback(async () => {
    if (selectorPauseStateDiagnosing || selectorHealDiagnosing) {
      return;
    }
    setSelectorPauseStateDiagnosing(true);
    setSelectorPauseStateMessage("");
    setSelectorPauseStateResult(null);
    try {
      const result = await diagnoseSelectorPauseState({ platform: "deepseek" });
      setSelectorPauseStateResult(result);
      if (!result.ok) {
        setSelectorPauseStateMessage(result.blocking_reason || result.message || "暂停态诊断失败");
        return;
      }
      const selector = String(result.selector || result.verified_selector || result.summary?.suggested_selector || "").trim();
      if (selector && result.saved) {
        syncSavedBrowserSelector("deepseek", "generation_pause_selector", selector);
      }
      setSelectorPauseStateMessage(result.save_error || result.message || (result.saved ? "已抓到并保存 DeepSeek 暂停态 selector" : "已抓到 DeepSeek 生成中的暂停态表达"));
      if (result.saved) {
        notifySaveSuccess(onSaveSuccess, "DeepSeek 暂停态 selector 已保存");
      }
    } finally {
      setSelectorPauseStateDiagnosing(false);
    }
  }, [onSaveSuccess, selectorHealDiagnosing, selectorPauseStateDiagnosing, syncSavedBrowserSelector]);

  const handleSaveSettings = useCallback(async () => {
    if (savingScope) {
      return;
    }
    const currentSnapshot = captureCurrentSettingsSnapshot();
    const previousSnapshot = lastSavedSettingsRef.current;
    const changedSections = Object.keys(currentSnapshot).filter(
      (section) => currentSnapshot[section] !== previousSnapshot[section],
    );

    if (changedSections.length === 0) {
      notifySaveSuccess(onSaveSuccess, "没有需要保存的更改");
      return;
    }

    const mergedSettingsPayload: Record<string, unknown> = {};
    if (changedSections.includes("scheduler")) {
      Object.assign(mergedSettingsPayload, buildSchedulerSettingsPayload());
    }
    if (changedSections.includes("account_crawling")) {
      Object.assign(mergedSettingsPayload, buildAccountCrawlingSettingsPayload());
    }
    if (changedSections.includes("article_export")) {
      Object.assign(mergedSettingsPayload, buildArticleExportSettingsPayload());
    }
    if (changedSections.includes("browser")) {
      Object.assign(mergedSettingsPayload, buildBrowserModeSettingsPayload());
    }
    if (changedSections.includes("recognition")) {
      Object.assign(mergedSettingsPayload, buildRecognitionSettingsPayload());
    }
    if (changedSections.includes("smart")) {
      Object.assign(mergedSettingsPayload, buildSmartModeSettingsPayload());
    }
    if (changedSections.includes("selector_agent")) {
      Object.assign(mergedSettingsPayload, buildSelectorAgentSettingsPayload());
    }
    if (changedSections.includes("search")) {
      Object.assign(mergedSettingsPayload, buildSearchSettingsPayload());
    }
    if (changedSections.includes("local_model")) {
      Object.assign(mergedSettingsPayload, buildLocalModelSettingsPayload());
    }
    if (changedSections.includes("context")) {
      Object.assign(mergedSettingsPayload, buildContextSnapshotSettingsPayload());
    }
    if (changedSections.includes("app_update")) {
      Object.assign(mergedSettingsPayload, buildAppUpdateSettingsPayload());
    }
    if (changedSections.includes("screenshot")) {
      Object.assign(mergedSettingsPayload, buildScreenshotTemplatePayload());
    }

    setSavingScope("all");
    try {
      const saveJobs: Promise<unknown>[] = [];
      if (Object.keys(mergedSettingsPayload).length > 0) {
        saveJobs.push(saveSettings(mergedSettingsPayload));
      }
      if (changedSections.includes("api_mode")) {
        saveJobs.push(savePlatformConfig(buildApiModePayload()));
      }
      await Promise.all(saveJobs);
      lastSavedSettingsRef.current = currentSnapshot;

      if (changedSections.includes("context")) {
        setContextRefreshMessage("设置已保存，天气与节日提醒正在后台刷新...");
        void refreshContextSnapshots(true)
          .then((refreshResult) => {
            applyContextSnapshotPayload(refreshResult);
            setContextRefreshMessage(String(refreshResult.message || "设置已保存"));
          })
          .catch(() => {
            setContextRefreshMessage("设置已保存，天气与节日提醒后台刷新失败");
          });
      }

      notifySaveSuccess(
        onSaveSuccess,
        changedSections.length === 1 ? "已保存 1 项修改" : `已保存 ${changedSections.length} 项修改`,
      );
    } finally {
      setSavingScope(null);
    }
  }, [
    applyContextSnapshotPayload,
    buildAccountCrawlingSettingsPayload,
    buildArticleExportSettingsPayload,
    buildApiModePayload,
    buildAppUpdateSettingsPayload,
    buildBrowserModeSettingsPayload,
    buildContextSnapshotSettingsPayload,
    buildLocalModelSettingsPayload,
    buildRecognitionSettingsPayload,
    buildSchedulerSettingsPayload,
    buildScreenshotTemplatePayload,
    buildSearchSettingsPayload,
    buildSmartModeSettingsPayload,
    buildSelectorAgentSettingsPayload,
    captureCurrentSettingsSnapshot,
    onSaveSuccess,
    savingScope,
  ]);

  const saving = Boolean(savingScope);

  const handleTestSchedulerWebhook = useCallback(async () => {
    setSchedulerWebhookTesting(true);
    setSchedulerWebhookTestMessage("");
    try {
      const result = await testSchedulerNotificationWebhook(schedulerNotificationWebhook);
      setSchedulerWebhookTestMessage(String(result.message || (result.ok ? "测试消息已发送" : "测试消息发送失败")));
    } finally {
      setSchedulerWebhookTesting(false);
    }
  }, [schedulerNotificationWebhook]);

  const handleRefreshContext = useCallback(async () => {
    setRefreshingContext(true);
    setContextRefreshMessage("");
    try {
      const result = await refreshContextSnapshots(true);
      applyContextSnapshotPayload(result);
      setContextRefreshMessage(String(result.message || "天气与节日提醒已刷新"));
    } finally {
      setRefreshingContext(false);
    }
  }, [applyContextSnapshotPayload]);

  const handleCheckAppUpdate = useCallback(async () => {
    setUpdateCheckPending(true);
    setUpdateCheckMessage("");
    try {
      const result = await checkAppUpdate({
        channel: updateChannel,
        manifest_url: updateManifestUrl.trim(),
        download_page_url: updateDownloadPageUrl.trim(),
        auto_check_enabled: updateAutoCheckEnabled,
      });
      applyUpdateStatusPayload(result);
    } finally {
      setUpdateCheckPending(false);
    }
  }, [
    applyUpdateStatusPayload,
    updateAutoCheckEnabled,
    updateChannel,
    updateDownloadPageUrl,
    updateManifestUrl,
  ]);

  const handlePrepareAppUpdate = useCallback(async () => {
    setUpdatePreparePending(true);
    setLocalUpdatePlanMessage("");
    setLocalUpdatePlanSummary("");
    try {
      const result = await prepareAppUpdate({
        channel: updateChannel,
        manifest_url: updateManifestUrl.trim(),
        download_page_url: updateDownloadPageUrl.trim(),
        auto_check_enabled: updateAutoCheckEnabled,
      });
      if (result.status) {
        applyUpdateStatusPayload(result.status);
      }
      if (!result.ok) {
        setLocalUpdatePlanMessage(String(result.message || "准备更新包失败"));
        return;
      }
      const sourceDir = String(result.source_dir || "").trim();
      if (sourceDir) {
        setLocalUpdateSourceDir(sourceDir);
      }
      const plan = (result.plan || {}) as Record<string, unknown>;
      setLocalUpdatePlanMessage(String(result.message || "更新包已准备好"));
      setLocalUpdatePlanSummary(
        `来源目录：${String(plan.sourceDir || sourceDir)}\n安装目标：${String(plan.targetDir || "")}\n策略：${String(plan.strategy || "external_updater")}`,
      );
      notifySaveSuccess(onSaveSuccess, "更新包已下载并校验");
    } finally {
      setUpdatePreparePending(false);
    }
  }, [
    applyUpdateStatusPayload,
    onSaveSuccess,
    updateAutoCheckEnabled,
    updateChannel,
    updateDownloadPageUrl,
    updateManifestUrl,
  ]);

  const handleLocalModelConfigChange = useCallback((
    key: keyof LocalModelFormState,
    value: string | boolean,
  ) => {
    setLocalModelConfig((prev) => ({
      ...prev,
      [key]: value,
    }));
  }, []);

  const handleRefreshLocalModelStatus = useCallback(async () => {
    await refreshLocalModelRuntimeStatus(false);
  }, [refreshLocalModelRuntimeStatus]);

  const handlePrepareLocalModel = useCallback(async () => {
    setLocalModelPreparing(true);
    setLocalModelActionMessage("");
    setLocalModelTestReply("");
    const payload = buildLocalModelSettingsPayload();
    try {
      const saveResult = await saveSettings(payload);
      if (!saveResult.ok) {
        setLocalModelActionMessage("保存本地模型配置失败，请重试");
        return;
      }
      lastSavedSettingsRef.current = {
        ...lastSavedSettingsRef.current,
        local_model: JSON.stringify(payload),
      };
      const result = await prepareLocalModel(payload.local_model.default_model);
      if (result.local_model) {
        setLocalModelStatus(normalizeLocalModelStatus(result.local_model));
      }
      setLocalModelActionMessage(String(result.message || (result.ok ? "本地模型准备任务已启动" : "本地模型准备失败")));
      if (result.ok) {
        notifySaveSuccess(onSaveSuccess, "本地模型准备已启动");
      }
    } finally {
      setLocalModelPreparing(false);
    }
  }, [buildLocalModelSettingsPayload, onSaveSuccess]);

  const handleTestLocalModel = useCallback(async () => {
    setLocalModelTesting(true);
    setLocalModelActionMessage("");
    setLocalModelTestReply("");
    try {
      const result = await testLocalModel(localModelConfig.defaultModel.trim() || DEFAULT_LOCAL_MODEL_CONFIG.defaultModel);
      if (result.local_model) {
        setLocalModelStatus(normalizeLocalModelStatus(result.local_model));
      }
      setLocalModelActionMessage(String(result.message || (result.ok ? "本地模型测试成功" : "本地模型测试失败")));
      setLocalModelTestReply(String(result.reply || "").trim());
    } finally {
      setLocalModelTesting(false);
    }
  }, [localModelConfig.defaultModel]);

  const handleBuildLocalUpdatePlan = useCallback(async () => {
    const sourceDir = localUpdateSourceDir.trim();
    if (!sourceDir) {
      setLocalUpdatePlanMessage("请先填写已解压的新版本目录");
      setLocalUpdatePlanSummary("");
      return;
    }
    setLocalUpdatePending(true);
    setLocalUpdatePlanMessage("");
    try {
      const result = await fetchLocalUpdatePlan(sourceDir);
      if (!result.ok) {
        setLocalUpdatePlanMessage(String(result.message || "生成更新计划失败"));
        setLocalUpdatePlanSummary("");
        return;
      }
      const plan = (result.plan || {}) as Record<string, unknown>;
      setLocalUpdatePlanMessage("更新计划已生成，可以继续安装");
      setLocalUpdatePlanSummary(
        `来源目录：${String(plan.sourceDir || "")}\n安装目标：${String(plan.targetDir || "")}\n策略：${String(plan.strategy || "external_updater")}`,
      );
    } finally {
      setLocalUpdatePending(false);
    }
  }, [localUpdateSourceDir]);

  const handlePickLocalUpdateDirectory = useCallback(async () => {
    setLocalUpdatePending(true);
    try {
      const result = await pickDirectory("选择已解压的新版本目录");
      if (!result.ok) {
        setLocalUpdatePlanMessage(String(result.message || "选择目录失败"));
        return;
      }
      const nextPath = String(result.path || "").trim();
      setLocalUpdateSourceDir(nextPath);
      setLocalUpdatePlanMessage("已选择新版本目录");
      setLocalUpdatePlanSummary("");
    } finally {
      setLocalUpdatePending(false);
    }
  }, []);

  const handleStartLocalUpdate = useCallback(async () => {
    const sourceDir = localUpdateSourceDir.trim();
    if (!sourceDir) {
      setLocalUpdatePlanMessage("请先填写已解压的新版本目录");
      return;
    }
    setLocalUpdatePending(true);
    setLocalUpdatePlanMessage("");
    try {
      const result = await startLocalUpdate({
        source_dir: sourceDir,
        cleanup_source: localUpdateCleanupSource,
        restart_after_update: localUpdateRestartAfterInstall,
        exit_after_launch: localUpdateExitAfterLaunch,
      });
      setLocalUpdatePlanMessage(String(result.message || (result.ok ? "更新器已启动" : "启动更新失败")));
      if (result.ok) {
        notifySaveSuccess(onSaveSuccess, result.exitScheduled ? "更新器已启动，程序即将退出" : "更新器已启动");
      }
    } finally {
      setLocalUpdatePending(false);
    }
  }, [
    localUpdateCleanupSource,
    localUpdateExitAfterLaunch,
    localUpdateRestartAfterInstall,
    localUpdateSourceDir,
    onSaveSuccess,
  ]);

  const handleResetScreenshotTemplate = useCallback(() => {
    setScreenshotEnabled(true);
    setShowTime(true);
    setShowFooter(true);
    setShowHighlight(true);
    setScreenshotTitle(SCREENSHOT_TEMPLATE_DEFAULTS.title);
    setScreenshotSubtitle(SCREENSHOT_TEMPLATE_DEFAULTS.subtitle);
    setScreenshotFooter(SCREENSHOT_TEMPLATE_DEFAULTS.footer);
    setAccentColor(SCREENSHOT_TEMPLATE_DEFAULTS.accentColor);
    setBackgroundStart(SCREENSHOT_TEMPLATE_DEFAULTS.backgroundStart);
    setBackgroundEnd(SCREENSHOT_TEMPLATE_DEFAULTS.backgroundEnd);
    setHeaderStart(SCREENSHOT_TEMPLATE_DEFAULTS.headerStart);
    setHeaderEnd(SCREENSHOT_TEMPLATE_DEFAULTS.headerEnd);
    setOuterPadding(SCREENSHOT_TEMPLATE_DEFAULTS.outerPadding);
    setHeaderHeight(SCREENSHOT_TEMPLATE_DEFAULTS.headerHeight);
    setRadius(SCREENSHOT_TEMPLATE_DEFAULTS.radius);
    setImageRadius(SCREENSHOT_TEMPLATE_DEFAULTS.imageRadius);
  }, []);

  // Save profile to backend
  const handleSaveProfile = useCallback(async () => {
    let nextProfile = editProfile;
    if (avatarCropDraft) {
      try {
        const avatar = await renderAvatarFromCrop(avatarCropDraft);
        nextProfile = { ...editProfile, avatar };
        setEditProfile(nextProfile);
      } catch {
        setAvatarUploadMessage("头像裁切失败，请重新上传后再试");
        return;
      }
    }
    setProfile(nextProfile);
    setIsEditingProfile(false);
    setAvatarCropDraft(null);
    setAvatarUploadMessage("");
    await saveProfile({
      name: nextProfile.name,
      role: nextProfile.role,
      avatar: nextProfile.avatar,
      birthday: nextProfile.birthday,
      hire_date: nextProfile.hireDate,
    });
    notifySaveSuccess(onSaveSuccess, "保存成功");
  }, [avatarCropDraft, editProfile, onSaveSuccess]);

  const handleAvatarFileChange = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) {
      return;
    }
    if (!file.type.startsWith("image/")) {
      setAvatarUploadMessage("请选择图片文件");
      return;
    }
    try {
      const draft = await buildAvatarCropDraft(file);
      setAvatarCropDraft(draft);
      setAvatarUploadMessage(`已载入头像：${file.name}，请拖动并裁切`);
    } catch {
      setAvatarUploadMessage("头像处理失败，请换一张图片再试");
    }
  }, []);

  const handleApplyAvatarCrop = useCallback(async () => {
    if (!avatarCropDraft) {
      return;
    }
    try {
      const avatar = await renderAvatarFromCrop(avatarCropDraft);
      setEditProfile((prev) => ({ ...prev, avatar }));
      setAvatarCropDraft(null);
      setAvatarUploadMessage(`已应用头像裁切：${avatarCropDraft.fileName}`);
    } catch {
      setAvatarUploadMessage("头像裁切失败，请重新上传后再试");
    }
  }, [avatarCropDraft]);

  const handleAvatarCropZoomChange = useCallback((value: string) => {
    const zoom = Number(value);
    setAvatarCropDraft((prev) => {
      if (!prev) {
        return prev;
      }
      const nextOffset = clampAvatarCropOffset(prev, prev.offsetX, prev.offsetY, zoom);
      return { ...prev, zoom, ...nextOffset };
    });
  }, []);

  const handleAvatarCropPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!avatarCropDraft) {
      return;
    }
    avatarDragRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      originX: avatarCropDraft.offsetX,
      originY: avatarCropDraft.offsetY,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }, [avatarCropDraft]);

  const handleAvatarCropPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = avatarDragRef.current;
    if (!drag) {
      return;
    }
    const deltaX = event.clientX - drag.startX;
    const deltaY = event.clientY - drag.startY;
    setAvatarCropDraft((prev) => {
      if (!prev) {
        return prev;
      }
      const nextOffset = clampAvatarCropOffset(prev, drag.originX + deltaX, drag.originY + deltaY);
      return { ...prev, ...nextOffset };
    });
  }, []);

  const handleAvatarCropPointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    avatarDragRef.current = null;
  }, []);

  // Calculate days
  const hireDays = useMemo(() => {
    const start = new Date(profile.hireDate);
    const now = new Date();
    if (isNaN(start.getTime())) return 0;
    const diffTime = Math.abs(now.getTime() - start.getTime());
    return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
  }, [profile.hireDate]);

  const smartModelOptions = withCurrentModel(
    aiPlatforms.find((p) => p.id === smartPlatform)?.models || [],
    smartModel,
  );
  const smartVisionModelOptions = withCurrentModel(
    visionPlatforms.find((p) => p.id === smartVisionPlatform)?.models || [],
    smartVisionModel,
  );
  const selectorAgentModelOptions = withCurrentModel(
    selectorAgentPlatforms.find((p) => p.id === selectorAgentPlatform)?.models || [],
    selectorAgentModel,
  );
  const ocrModelOptions = withCurrentModel(
    recognitionVisionPlatforms.find((p) => p.id === ocrPlatform)?.models || [],
    ocrModel,
  );
  const localModelStatusMeta = LOCAL_MODEL_STATUS_META[localModelStatus.status] || LOCAL_MODEL_STATUS_META.idle;
  const localDownloadedModels = useMemo(
    () => uniqStrings([
      ...(localModelStatus.models || []),
      localModelConfig.defaultModel.trim(),
    ].filter(Boolean)),
    [localModelConfig.defaultModel, localModelStatus.models],
  );
  const localBinarySourceLabel = useMemo(() => {
    switch (localModelStatus.binary_source) {
      case "configured":
        return "手动指定";
      case "environment":
        return "环境变量";
      case "system":
        return "系统已安装";
      case "bundled_runtime":
        return "应用内置";
      default:
        return "未发现";
    }
  }, [localModelStatus.binary_source]);
  const localTargetModel = useMemo(
    () => localModelConfig.defaultModel.trim() || DEFAULT_LOCAL_MODEL_CONFIG.defaultModel,
    [localModelConfig.defaultModel],
  );
  const localTargetModelReady = useMemo(
    () => localModelStatus.models.includes(localTargetModel),
    [localModelStatus.models, localTargetModel],
  );
  const localModelSelfCheck = useMemo(() => {
    if (localModelStatus.healthy && localTargetModelReady) {
      return {
        tone: "text-emerald-800 border-emerald-300",
        title: "自检结论：可直接使用",
        detail: `本地服务已连通，默认模型 ${localTargetModel} 已就绪。识别、智能模式和搜搜都可以直接选用本地模型。`,
        action: `现在可以直接测试，或在功能里切到本地模型（${localTargetModel}）。`,
      };
    }
    if (localModelStatus.pull_process_running || localModelStatus.status === "preparing") {
      return {
        tone: "text-amber-900 border-amber-300",
        title: "自检结论：首次准备中",
        detail: `程序正在准备本地模型资源${localModelStatus.pulling_model ? `：${localModelStatus.pulling_model}` : ""}。首次启动可能需要一点时间，准备完成后就能直接用。`,
        action: "保持当前页面即可，稍后刷新状态或直接点一次测试本地模型。",
      };
    }
    if (localModelStatus.service_process_running && !localModelStatus.healthy) {
      return {
        tone: "text-blue-800 border-blue-300",
        title: "自检结论：服务正在启动",
        detail: "程序已经拉起本地模型服务，但还没完全就绪。稍后刷新状态，或直接点击测试本地模型。",
        action: "通常不用再配路径，等几秒后重试即可。",
      };
    }
    if (localModelStatus.binary_source === "system" && localModelStatus.healthy && !localTargetModelReady) {
      return {
        tone: "text-blue-800 border-blue-300",
        title: "自检结论：已检测到系统 Ollama，只差模型",
        detail: `已经连上你本机已安装的 Ollama，但默认模型 ${localTargetModel} 还没准备好。点一次“准备本地模型”，程序就会继续补齐。`,
        action: "不用手动找可执行文件，直接点“准备本地模型”就行。",
      };
    }
    if (localModelStatus.binary_source === "system" && !localModelStatus.healthy) {
      return {
        tone: "text-blue-800 border-blue-300",
        title: "自检结论：已检测到系统 Ollama",
        detail: localTargetModelReady
          ? `你机器上已经装了 Ollama，而且默认模型 ${localTargetModel} 也在。现在只是本地服务还没连上，点一次“准备本地模型”就会自动拉起。`
          : `你机器上已经装了 Ollama，但当前还没确认到可用服务，也还没确认到默认模型 ${localTargetModel}。点一次“准备本地模型”即可自动继续处理。`,
        action: "这一步通常不需要再安装 Ollama，也不用手动填路径。",
      };
    }
    if (!localModelStatus.binary_path) {
      if (localModelStatus.bundled_binary_available) {
        return {
          tone: "text-amber-900 border-amber-300",
          title: "自检结论：内置引擎待部署",
          detail: "当前版本已经带了本地引擎资源，但还没成功部署到运行目录。点击“准备本地模型”后，程序会自动完成部署并继续启动服务。",
          action: "这是正常首启状态，点一次“准备本地模型”即可。",
        };
      }
      return {
        tone: "text-gray-700 border-gray-200",
        title: "自检结论：当前版本还不能直接用",
        detail: "这不是模型没选对，而是当前这份程序还没带上 Ollama 引擎文件，所以程序没法自动装好本地服务。正式打包时把内置引擎一起分发后，首启就能自动部署。",
        action: "开发阶段可以先装本机 Ollama；正式打包阶段再把内置引擎一起带上。",
      };
    }
    return {
      tone: "text-gray-700 border-gray-200",
      title: "自检结论：还不能直接用",
      detail: "当前还没确认到可用的本地模型服务或目标模型。点击“准备本地模型”后，程序会自动尝试启动并补齐资源。",
      action: "如果你已经装过 Ollama，这里通常点一次“准备本地模型”就够了。",
    };
  }, [
    localModelStatus.healthy,
    localModelStatus.models,
    localModelStatus.binary_path,
    localModelStatus.binary_source,
    localModelStatus.bundled_binary_available,
    localModelStatus.pull_process_running,
    localModelStatus.pulling_model,
    localModelStatus.service_process_running,
    localModelStatus.status,
    localTargetModel,
    localTargetModelReady,
  ]);
  const browserAuthCards = useMemo(
    () => BROWSER_AUTOMATION_PLATFORM_IDS.map((platformId) => {
      const snapshot = browserAuthPlatforms[platformId];
      const activeProfile = snapshot?.active_profile || null;
      const authState = String(activeProfile?.auth_state || "");
      const loggedIn = authState === "authenticated";
      const needsConfirmation = authState === "needs_confirmation";
      const profileBusy = Boolean(snapshot?.profile_busy ?? snapshot?.debug?.raw_profile_in_use);
      const statusText = snapshot?.login_window_open
        ? "登录窗口已打开"
        : loggedIn
          ? "已登录"
          : profileBusy
            ? "账号环境被占用"
          : needsConfirmation
            ? "待确认"
            : "未登录";
      const statusTone = snapshot?.login_window_open
        ? "text-blue-600 bg-blue-50"
        : loggedIn
          ? "text-emerald-600 bg-emerald-50"
          : profileBusy
            ? "text-amber-700 bg-amber-50"
          : needsConfirmation
            ? "text-amber-700 bg-amber-50"
            : "text-red-600 bg-red-50";
      return {
        id: platformId,
        label: PLATFORM_LABELS[platformId] || platformId,
        snapshot,
        statusText,
        statusTone,
        loggedIn,
        profileBusy,
        needsConfirmation,
        message: browserAuthMessages[platformId] || "",
      };
    }),
    [browserAuthMessages, browserAuthPlatforms],
  );

  return (
    <div className="flex-1 h-full overflow-hidden bg-transparent px-8 py-8 xl:px-10 flex flex-col">
      {/* Header */}
      <div className="flex justify-between items-end border-b border-gray-200/70 pb-6 mb-6 shrink-0">
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <Settings2 className="w-5 h-5 text-blue-600" />
            <h1 className="app-wordmark-heading text-[20px]">系统设置</h1>
          </div>
          <span className="text-[12px] font-medium text-gray-500 tracking-wide">配置全局运行参数与系统行为</span>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={handleSaveSettings}
            disabled={saving || dirtySectionCount === 0}
            className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 text-white px-5 py-2 rounded-lg text-[12px] font-bold transition-colors shadow-sm shadow-blue-600/20 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Save className="w-4 h-4" /> {saving ? "保存中..." : dirtySectionCount > 0 ? `保存 ${dirtySectionCount} 项修改` : "暂无修改"}
          </button>
        </div>
      </div>

      {/* Content Scroll Area */}
      <div className="flex-1 overflow-y-auto pr-3 -mr-3 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent pb-10">
        <div className="max-w-4xl space-y-6">
          
          {/* Section 1: Auto Query Time & Notification */}
          <Section title="自动查询时间及通知">
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              任务只决定在哪几天参与自动查询；每天具体几点开始，由这里统一控制。
            </p>

            <div className="mb-5 border-b border-gray-200/80 pb-4">
              <ControlledTextInput
                label="调度通知 Webhook"
                value={schedulerNotificationWebhook}
                onChange={setSchedulerNotificationWebhook}
                placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
              />
              <p className="mt-2 text-[11px] text-gray-500 leading-relaxed">
                用于模式汇总、整轮完成汇总和实时异常提醒。这个 webhook 独立于品牌任务自己的截图通知 webhook。
              </p>
              <div className="mt-3 flex items-center gap-3">
                <button
                  type="button"
                  onClick={handleTestSchedulerWebhook}
                  disabled={schedulerWebhookTesting}
                  className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Send className="h-3.5 w-3.5" />
                  {schedulerWebhookTesting ? "发送中..." : "发送测试消息"}
                </button>
                <span className="text-[11px] text-gray-500">会向当前填写的调度 webhook 发送一条测试文本。</span>
              </div>
              {schedulerWebhookTestMessage ? (
                <div className="mt-2 text-[12px] font-medium text-blue-600">{schedulerWebhookTestMessage}</div>
              ) : null}
            </div>

            <div className="mb-5 grid grid-cols-1 md:grid-cols-2 gap-4 border-b border-gray-200/80 pb-4">
              <div className="flex items-center gap-3 text-[12px]">
                <span className="w-32 font-bold text-gray-700">连续失败告警 :</span>
                <NumberInput
                  value={failureAlertThreshold}
                  onChange={setFailureAlertThreshold}
                  min={1}
                  max={50}
                />
                <span className="text-gray-500 font-medium">次后发送</span>
              </div>
              <div className="flex items-center gap-3 text-[12px]">
                <span className="w-32 font-bold text-gray-700">失败告警冷却 :</span>
                <NumberInput
                  value={failureAlertCooldownMinutes}
                  onChange={setFailureAlertCooldownMinutes}
                  min={0}
                  max={120}
                />
                <span className="text-gray-500 font-medium">分钟</span>
              </div>
              <p className="md:col-span-2 text-[11px] text-gray-500 leading-relaxed">
                同一类失败连续累计到阈值后会立刻推送调度 webhook；发送后会进入短冷却，避免刷屏。默认是连续 7 次失败、冷却 5 分钟。
              </p>
            </div>
            
            <CheckboxRow 
              checked={autoFallback} 
              onChange={setAutoFallback}
              label="默认模式失败后自动启动后续模式"
              subtext="关闭时，系统会先发主页消息提醒；你确认后才显示可继续的后续模式选项。"
              className="mb-5"
            />

            <div className="mb-5 border-t border-gray-200/80 pt-4">
              <div className="mb-3">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <div className="text-[12px] font-bold text-gray-900">文章抓取</div>
                  <span className="text-[11px] font-medium text-gray-400">已排除 {accountExcludedTotal} 篇文章</span>
                  <button
                    type="button"
                    onClick={() => {
                      setAccountExclusionsOpen((open) => !open);
                      if (!accountExclusionsOpen) {
                        void refreshAccountExclusions();
                      }
                    }}
                    className="inline-flex items-center gap-1 px-0 py-0.5 text-[11px] font-bold text-gray-500 transition-colors hover:text-gray-900"
                  >
                    <ListFilter className="h-3 w-3" />
                    {accountExclusionsOpen ? "收起排除项" : "查看排除项"}
                  </button>
                </div>
                <div>
                  <p className="mt-1 text-[11px] text-gray-500 leading-relaxed">
                    用于账号页维护的自媒体账号主页链接（搜狐号、头条号、知乎、博客园等）；定时只抓首页 / RSS / 接口返回的最新列表。RSSHub 会自动记住可用源，并短期跳过连续失败源。
                  </p>
                </div>
              </div>
              <CheckboxRow
                checked={accountCrawlEnabled}
                onChange={setAccountCrawlEnabled}
                label="开启账号文章自动抓取"
                subtext="关闭后仍可在“全部文章汇总”里手动点击抓取。"
                className="mb-4"
              />
              <CheckboxRow
                checked={articleExportShowKeywordCategory}
                onChange={setArticleExportShowKeywordCategory}
                label="导出文章表格显示关键词大类"
                subtext="开启后，导出表格最前一列显示文章对应关键词；同一关键词下按发布时间从早到晚排列。"
                className="mb-4"
              />
              <CheckboxRow
                checked={articleExportShowSelfMediaAccount}
                onChange={setArticleExportShowSelfMediaAccount}
                label="导出文章表格显示自媒体具体账号"
                subtext="关闭后，来源列只显示今日头条、搜狐等平台名称，不附加括号内账号名。"
                className="mb-4"
              />
              <div className={`grid grid-cols-1 md:grid-cols-3 gap-4 ${accountCrawlEnabled ? "" : "opacity-60"}`}>
                <ControlledTextInput
                  label="抓取间隔（分钟）:"
                  value={accountCrawlFrequencyMinutes}
                  onChange={setAccountCrawlFrequencyMinutes}
                  placeholder="60"
                />
                <ControlledTextInput
                  label="单账号最多导入 :"
                  value={accountCrawlMaxItems}
                  onChange={setAccountCrawlMaxItems}
                  placeholder="20"
                />
                <ControlledTextInput
                  label="RSSHub 地址（可多个）:"
                  value={accountCrawlRsshubBaseUrl}
                  onChange={setAccountCrawlRsshubBaseUrl}
                  placeholder="https://rsshub.app, https://rsshub.akr.moe"
                />
              </div>
              {accountExclusionsOpen ? (
                <div className="mt-4 border-t border-gray-200/80 pt-3">
                  <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="text-[11px] font-bold tracking-wide text-gray-500">排除项列表</div>
                      <div className="mt-1 text-[11px] leading-relaxed text-gray-500">
                        共 {accountExcludedTotal} 篇文章；恢复后，下次文章抓取会重新导入对应链接。
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      <button
                        type="button"
                        onClick={() => { void refreshAccountExclusions(); }}
                        disabled={accountExclusionLoading}
                        className="inline-flex items-center gap-1 px-0 py-1 text-[11px] font-bold text-gray-500 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <RefreshCw className={`h-3 w-3 ${accountExclusionLoading ? "animate-spin" : ""}`} />
                        刷新
                      </button>
                      <button
                        type="button"
                        onClick={() => { void restoreSelectedAccountExclusions(); }}
                        disabled={accountExclusionRestoring || accountExclusionSelectedUrls.length === 0}
                        className="inline-flex items-center gap-1 px-0 py-1 text-[11px] font-bold text-gray-500 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:text-gray-300"
                      >
                        <RotateCcw className="h-3 w-3" />
                        {accountExclusionRestoring ? "恢复中..." : `恢复所选 ${accountExclusionSelectedUrls.length || ""}`}
                      </button>
                    </div>
                  </div>
                  {accountExclusionMessage ? (
                    <div className="mb-3 text-[12px] font-medium text-blue-600">{accountExclusionMessage}</div>
                  ) : null}
                  <div className="max-h-[280px] overflow-y-auto pr-1">
                    {accountExcludedLinks.length > 0 ? (
                      <div className="border-t border-gray-100">
                        {accountExcludedLinks.map((link) => {
                          const url = getExcludedArticleUrl(link);
                          const checked = accountExclusionSelectedUrls.includes(url);
                          return (
                            <label
                              key={url}
                              className="flex cursor-pointer items-start gap-2 border-b border-gray-100 py-2.5 transition-colors hover:bg-gray-50/60"
                            >
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleAccountExcludedUrl(url)}
                                className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-blue-600"
                              />
                              <span className="min-w-0 flex-1">
                                <span className="block truncate text-[12px] font-semibold text-gray-800">
                                  {getExcludedArticleTitle(link)}
                                </span>
                                <span className="mt-0.5 block truncate text-[10px] font-medium text-gray-400">
                                  {url}
                                </span>
                                <span className="mt-1 block text-[10px] font-medium text-gray-400">
                                  {link.updated_at || link.excluded_at ? `排除时间：${link.updated_at || link.excluded_at}` : "手动清除后加入排除"}
                                </span>
                              </span>
                            </label>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="border-t border-gray-100 py-5 text-[12px] font-medium text-gray-400">
                        暂无已排除文章
                      </div>
                    )}
                  </div>
                </div>
              ) : null}
            </div>

            <div className="space-y-2 border-t border-gray-200/80 pt-4">
              {[
                { day: 0, label: '周一' }, { day: 1, label: '周二' }, { day: 2, label: '周三' },
                { day: 3, label: '周四' }, { day: 4, label: '周五' }, { day: 5, label: '周六' }, { day: 6, label: '周日' }
              ].map(({day, label}) => (
                <div key={day} className="flex items-center gap-4 text-[12px]">
                  <Checkbox 
                    checked={activeDays[day as keyof typeof activeDays]} 
                    onChange={(val) => setActiveDays(prev => ({...prev, [day]: val}))} 
                    label={label}
                    className="w-20 font-bold"
                  />
                  <div className="flex items-center gap-2">
                    <span className="text-gray-500 font-medium">小时</span>
                    <NumberInput 
                      value={times[day as keyof typeof times].h} 
                      onChange={(val) => setTimes(prev => ({...prev, [day]: { ...prev[day as keyof typeof times], h: val }}))} 
                      min={0} max={23} 
                    />
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-gray-500 font-medium">分钟</span>
                    <NumberInput 
                      value={times[day as keyof typeof times].m} 
                      onChange={(val) => setTimes(prev => ({...prev, [day]: { ...prev[day as keyof typeof times], m: val }}))} 
                      min={0} max={59} 
                    />
                  </div>
                </div>
              ))}
            </div>
          </Section>

          <Section title="抓取模式">
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              这里统一管理各平台抓取模式下的浏览器自动化参数。点击平台名可展开对应配置，点击保存更改后下次生效。
            </p>

            <div className="mb-6 border-b border-gray-200/80 pb-5">
              <SelectInput
                label="回答生成方式 :"
                value={browserAnswerMode}
                onChange={setBrowserAnswerMode}
                options={BROWSER_ANSWER_MODE_OPTIONS}
              />
              <p className="mt-2 text-[11px] text-gray-400 leading-relaxed border-l-2 border-gray-200 pl-3">
                页面原始截图会继续走下方的页面截图装饰模板；DOM 文本生成会直接提取回答 DOM，并固定使用 Surfaced 模版重排，表格等结构也会尽量保留。
              </p>
            </div>

            <QueryExecutionSettingsCard
              title="抓取模式查询规格"
              description="默认按平台分组串行执行，同一平台的查询会尽量连着跑完后再切下一个；实验中的平台会话池策略暂时不要当作稳定能力使用。"
              settings={queryExecutionSettings.browser}
              onChange={(key, value) => updateQueryExecutionSetting("browser", key, value)}
            />

            <div className="space-y-3">
              {BROWSER_AUTOMATION_PLATFORM_IDS.map((platformId) => {
                const isOpen = !!browserPanelsOpen[platformId];
                const isAdvancedOpen = !!browserAdvancedPanelsOpen[platformId];
                const label = PLATFORM_LABELS[platformId] || platformId;
                const { basicFields, advancedFields } = splitBrowserAutomationFields(BROWSER_AUTOMATION_CONFIGS[platformId].fields);
                return (
                  <div key={platformId} className="border-b border-gray-200/80 overflow-hidden">
                    <button
                      type="button"
                      onClick={() => toggleBrowserPanel(platformId)}
                      className="w-full grid grid-cols-[minmax(0,1fr)_24px] md:grid-cols-[150px_minmax(0,1fr)_24px] gap-4 px-0 py-4 text-left hover:bg-transparent transition-colors"
                    >
                      <span className="text-[12px] font-bold text-gray-900 pt-0.5">{label}</span>
                      <span className="text-[11px] text-gray-500 leading-relaxed">
                        {BROWSER_AUTOMATION_CONFIGS[platformId].summary}
                      </span>
                      <ChevronDown className={`w-4 h-4 text-gray-400 transition-transform mt-0.5 ${isOpen ? "rotate-180" : ""}`} />
                    </button>

                    {isOpen && (
                      <div className="grid grid-cols-1 md:grid-cols-[150px_minmax(0,1fr)] gap-4 border-t border-gray-200/80 pt-4 pb-4">
                        <div className="hidden md:block" />
                        <div className="space-y-4">
                          {basicFields.map((field) => {
                            const newChatHealEnabled = platformId === "deepseek" && field.key === "new_chat_selector";
                            const pauseStateHealEnabled = platformId === "deepseek" && field.key === "generation_pause_selector";
                            const selectorHealEnabled = newChatHealEnabled || pauseStateHealEnabled;
                            return (
                              <div key={`${platformId}-${field.key}`} className="space-y-2">
                                <div className={selectorHealEnabled ? "grid grid-cols-[minmax(0,1fr)_auto] items-end gap-3" : ""}>
                                  <ControlledTextInput
                                    label={field.label}
                                    value={browserSelectors[platformId]?.[field.key] || ""}
                                    onChange={(value) => updateBrowserSelector(platformId, field.key, value)}
                                    placeholder={field.placeholder}
                                    type={field.inputType}
                                  />
                                  {selectorHealEnabled ? (
                                    <div className="flex h-9 items-center gap-3">
                                      {newChatHealEnabled ? (
                                        <button
                                          type="button"
                                          onClick={handleDiagnoseDeepSeekNewChatSelector}
                                          disabled={selectorHealDiagnosing || selectorPauseStateDiagnosing}
                                          className="inline-flex items-center gap-1.5 border-b border-gray-300 px-0 text-[12px] font-bold text-gray-700 transition-colors hover:border-gray-900 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                                        >
                                          <Wrench className={`h-3.5 w-3.5 ${selectorHealDiagnosing ? "animate-spin" : ""}`} />
                                          {selectorHealDiagnosing ? "抓取中" : "自动抓取"}
                                        </button>
                                      ) : null}
                                      {pauseStateHealEnabled ? (
                                        <button
                                          type="button"
                                          onClick={handleDiagnoseDeepSeekPauseState}
                                          aria-label="诊断 DeepSeek 暂停态 selector"
                                          title="诊断 DeepSeek 暂停态 selector"
                                          disabled={selectorPauseStateDiagnosing || selectorHealDiagnosing}
                                          className="inline-flex items-center gap-1.5 border-b border-gray-300 px-0 text-[12px] font-bold text-gray-700 transition-colors hover:border-gray-900 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                                        >
                                          <Wrench className={`h-3.5 w-3.5 ${selectorPauseStateDiagnosing ? "animate-spin" : ""}`} />
                                          {selectorPauseStateDiagnosing ? "诊断中" : "诊断"}
                                        </button>
                                      ) : null}
                                    </div>
                                  ) : null}
                                </div>
                                {newChatHealEnabled ? (
                                  <div className="space-y-2">
                                    <SelectorHealResultPanel
                                      result={selectorHealResult}
                                      message={selectorHealMessage}
                                    />
                                  </div>
                                ) : null}
                                {pauseStateHealEnabled ? (
                                  <SelectorPauseStatePanel
                                    result={selectorPauseStateResult}
                                    message={selectorPauseStateMessage}
                                  />
                                ) : null}
                              </div>
                            );
                          })}
                          {advancedFields.length > 0 && (
                            <div className="border-t border-dashed border-gray-200/80 pt-1">
                              <button
                                type="button"
                                onClick={() => toggleBrowserAdvancedPanel(platformId)}
                                className="flex w-full items-center justify-between gap-3 px-0 py-3 text-left"
                              >
                                <div className="flex items-center gap-2">
                                  <span className="text-[12px] font-bold text-gray-900">高级设置</span>
                                  <span className="text-[11px] text-gray-400">
                                    语言、时区、代理、稳定等待、失败退避
                                  </span>
                                </div>
                                <ChevronDown className={`w-4 h-4 shrink-0 text-gray-400 transition-transform ${isAdvancedOpen ? "rotate-180" : ""}`} />
                              </button>
                              {isAdvancedOpen && (
                                <div className="space-y-4 pb-1">
                                  <p className="text-[11px] text-gray-400 leading-relaxed border-l-2 border-gray-200 pl-3">
                                    这部分通常保持默认即可，只有特殊站点、网络环境或需要放慢查询节奏时才需要覆盖。
                                  </p>
                                  {advancedFields.map((field) => (
                                    <ControlledTextInput
                                      key={`${platformId}-${field.key}`}
                                      label={field.label}
                                      value={browserSelectors[platformId]?.[field.key] || ""}
                                      onChange={(value) => updateBrowserSelector(platformId, field.key, value)}
                                      placeholder={field.placeholder}
                                      type={field.inputType}
                                    />
                                  ))}
                                </div>
                              )}
                            </div>
                          )}
                          <p className="text-[11px] text-gray-400 leading-relaxed border-l-2 border-gray-200 pl-3">
                            {BROWSER_AUTOMATION_CONFIGS[platformId].note} 点击保存更改后下次生效。
                          </p>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="mt-6 border-t border-gray-200/80 pt-5">
              <div className="mb-4">
                <div className="text-[12px] font-bold text-gray-900">平台账号环境</div>
                <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                  每个平台默认只保留当前这一套登录状态。重新登录或退出登录时，会尽量重建这个平台在程序里的本地浏览器环境，再重新打开登录页手动登录。
                </p>
              </div>

              <div className="space-y-3">
                {browserAuthCards.map(({ id, label, snapshot, statusText, statusTone, loggedIn, profileBusy, needsConfirmation, message }) => {
                  const platformPending = browserAuthPendingKey.startsWith(`${id}:`);
                  const isLoggedIn = Boolean(loggedIn);
                  const loginWindowOpen = Boolean(snapshot?.login_window_open);
                  const showBusyActions = profileBusy && !loginWindowOpen && !isLoggedIn;
                  const loginActionLabel = isLoggedIn ? "打开登录页" : "登录账号";
                  const showOpenAction = !loginWindowOpen && !showBusyActions;
                  const showConfirmAction = !isLoggedIn && (loginWindowOpen || needsConfirmation || showBusyActions);
                  const showCloseAction = loginWindowOpen || showBusyActions;
                  const closeActionLabel = loginWindowOpen ? "关闭窗口" : "清理占用";
                  return (
                    <div key={`auth-${id}`} className="border-b border-gray-200/80 pb-4 last:border-b-0 last:pb-0">
                      <div className="grid grid-cols-1 gap-4 md:grid-cols-[150px_minmax(0,1fr)]">
                        <div className="text-[12px] font-bold text-gray-900 pt-0.5">{label}</div>
                        <div className="space-y-3">
                          <div className="space-y-1">
                            <div>
                              <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-bold ${statusTone}`}>
                                {statusText}
                              </span>
                            </div>
                            <div className="text-[11px] text-gray-500">
                              {snapshot?.login_window_open
                                ? "程序当前正在跟踪这次登录窗口，登录完成后点“已完成登录”即可。"
                                : showBusyActions
                                  ? "该账号环境正被浏览器或后台进程占用；如果前台没窗口，可先点“清理占用”，再重新打开登录页。"
                                : statusText === "已登录"
                                  ? "当前平台已有可用登录状态。"
                                  : needsConfirmation
                                    ? "检测到旧浏览器资料，但还没有新的显式登录确认。"
                                  : "当前平台还没有保存登录状态。"}
                            </div>
                          </div>

                          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                            {showOpenAction ? (
                              <button
                                type="button"
                                onClick={() => handleBrowserAuthAction("open", id)}
                                disabled={platformPending}
                                className="text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                {platformPending ? "处理中..." : loginActionLabel}
                              </button>
                            ) : null}
                            {showConfirmAction ? (
                              <button
                                type="button"
                                onClick={() => handleBrowserAuthAction("confirm", id)}
                                disabled={platformPending}
                                className="text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                {loginWindowOpen ? "已完成登录" : "确认已登录"}
                              </button>
                            ) : null}
                            {!loginWindowOpen && isLoggedIn ? (
                              <button
                                type="button"
                                onClick={() => handleBrowserAuthAction("replace", id)}
                                disabled={platformPending}
                                className="text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                重新登录
                              </button>
                            ) : null}
                            {!loginWindowOpen && isLoggedIn ? (
                              <button
                                type="button"
                                onClick={() => handleBrowserAuthAction("logout", id)}
                                disabled={platformPending}
                                className="text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                退出登录
                              </button>
                            ) : null}
                            {showCloseAction ? (
                              <button
                                type="button"
                                onClick={() => handleBrowserAuthAction("close", id)}
                                disabled={platformPending}
                                className="text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                {closeActionLabel}
                              </button>
                            ) : null}
                          </div>

                          {message ? (
                            <div className="text-[11px] text-gray-500">{message}</div>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </Section>

          <Section
            title="本地模型"
            action={(
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={handleRefreshLocalModelStatus}
                  disabled={localModelRefreshing}
                  className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${localModelRefreshing ? "animate-spin" : ""}`} />
                  {localModelRefreshing ? "刷新中..." : "刷新状态"}
                </button>
                <button
                  type="button"
                  onClick={handlePrepareLocalModel}
                  disabled={localModelPreparing}
                  className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-blue-700 transition-colors hover:text-blue-900 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${localModelPreparing ? "animate-spin" : ""}`} />
                  {localModelPreparing ? "准备中..." : "准备本地模型"}
                </button>
                <button
                  type="button"
                  onClick={handleTestLocalModel}
                  disabled={localModelTesting}
                  className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Send className="h-3.5 w-3.5" />
                  {localModelTesting ? "测试中..." : "测试本地模型"}
                </button>
              </div>
            )}
          >
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              这里控制打包版内置本地模型运行时。程序会优先连接现有 Ollama 服务，也可以按下方配置自动拉起服务并按需下载当前默认模型。
            </p>

            <div className="space-y-5">
              <div className="border-b border-gray-200/80 pb-5 space-y-4">
                <div className={`border-l-2 pl-3 py-1 text-[12px] leading-relaxed ${localModelSelfCheck.tone}`}>
                  <div className="font-bold">{localModelSelfCheck.title}</div>
                  <div className="mt-1">{localModelSelfCheck.detail}</div>
                  {localModelSelfCheck.action ? (
                    <div className="mt-1 text-[11px] opacity-80">{localModelSelfCheck.action}</div>
                  ) : null}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <span className={`inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-bold ${localModelStatusMeta.className}`}>
                    {localModelStatusMeta.label}
                  </span>
                  <span className={`inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-bold ${
                    localModelStatus.healthy
                      ? "text-emerald-700 bg-emerald-50 border border-emerald-200"
                      : "text-gray-700 bg-gray-100 border border-gray-200"
                  }`}>
                    {localModelStatus.healthy ? "服务已连通" : "服务未连通"}
                  </span>
                  {localModelStatus.pull_process_running ? (
                    <span className="inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-bold text-amber-700 bg-amber-50 border border-amber-200">
                      下载中
                    </span>
                  ) : null}
                </div>

                <div className="grid grid-cols-1 md:grid-cols-[120px_minmax(0,1fr)] gap-3 text-[12px]">
                  <div className="text-[11px] font-bold tracking-wide text-gray-400">当前状态</div>
                  <div className="space-y-2">
                    <div className="text-gray-800">{localModelStatus.status_message || "暂无状态"}</div>
                    <div className="text-[11px] text-gray-500 break-all">服务地址：{localModelStatus.base_url || localModelConfig.baseUrl}</div>
                    <div className="text-[11px] text-gray-500">引擎来源：{localBinarySourceLabel}</div>
                    <div className="text-[11px] text-gray-500 break-all">
                      可执行文件：{localModelStatus.binary_path || localModelConfig.binaryPath || "当前未发现可用引擎"}
                    </div>
                    <div className="text-[11px] text-gray-500 break-all">
                      内置引擎：{localModelStatus.bundled_binary_available ? "已检测到，首启可自动部署" : "当前版本未内置"}
                    </div>
                    {localModelStatus.runtime_binary_path ? (
                      <div className="text-[11px] text-gray-500 break-all">
                        运行目录：{localModelStatus.runtime_binary_path}
                      </div>
                    ) : null}
                    {localModelStatus.pulling_model ? (
                      <div className="text-[11px] text-amber-700">正在处理模型：{localModelStatus.pulling_model}</div>
                    ) : null}
                    {localModelStatus.bundled_models_available ? (
                      <div className="text-[11px] text-gray-500 break-all">
                        随包模型：{localModelStatus.bundled_models_seeded ? "已同步到运行目录" : "已检测到，待同步"}
                      </div>
                    ) : null}
                    {localModelStatus.last_error ? (
                      <div className="text-[11px] text-red-600 leading-relaxed">最近错误：{localModelStatus.last_error}</div>
                    ) : null}
                    {!localModelStatus.healthy && localModelStatus.binary_source === "system" ? (
                      <div className="text-[11px] text-blue-700 leading-relaxed">
                        已识别到系统已安装的 Ollama，无需再手动配置路径。
                      </div>
                    ) : null}
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-[120px_minmax(0,1fr)] gap-3 text-[12px]">
                  <div className="text-[11px] font-bold tracking-wide text-gray-400">已发现模型</div>
                  <div className="flex flex-wrap gap-1.5">
                    {localDownloadedModels.length > 0 ? localDownloadedModels.map((model) => (
                      <span
                        key={model}
                        className={`inline-flex items-center rounded-md px-2 py-1 text-[11px] font-mono ${
                          localModelStatus.models.includes(model)
                            ? "bg-gray-100 text-gray-800"
                            : "bg-blue-50 text-blue-700"
                        }`}
                      >
                        {model}
                      </span>
                    )) : (
                      <span className="text-[11px] text-gray-500">还没有发现可用模型</span>
                    )}
                  </div>
                </div>

                {localModelActionMessage ? (
                  <div className="border-l-2 border-blue-300 pl-3 py-1 text-[12px] text-blue-800 leading-relaxed">
                    {localModelActionMessage}
                  </div>
                ) : null}

                {localModelTestReply ? (
                  <div className="grid grid-cols-1 md:grid-cols-[120px_minmax(0,1fr)] gap-3 text-[12px]">
                    <div className="text-[11px] font-bold tracking-wide text-gray-400">测试回包</div>
                    <div className="text-gray-800 break-words">{localModelTestReply}</div>
                  </div>
                ) : null}
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <ControlledTextInput
                  label="默认模型 :"
                  value={localModelConfig.defaultModel}
                  onChange={(value) => handleLocalModelConfigChange("defaultModel", value)}
                  placeholder="gemma4:e2b"
                />
                <ControlledTextInput
                  label="服务地址 :"
                  value={localModelConfig.baseUrl}
                  onChange={(value) => handleLocalModelConfigChange("baseUrl", value)}
                  placeholder="http://127.0.0.1:11434"
                />
              </div>

              <ControlledTextInput
                label="Ollama 可执行文件路径 :"
                value={localModelConfig.binaryPath}
                onChange={(value) => handleLocalModelConfigChange("binaryPath", value)}
                placeholder="留空即可优先尝试内置引擎，没有内置时再填系统路径"
              />

              <div className="space-y-2">
                <CheckboxRow
                  checked={localModelConfig.autoStart}
                  onChange={(value) => handleLocalModelConfigChange("autoStart", value)}
                  label="需要时自动启动本地模型服务"
                  subtext="正式打包时如果内置了 Ollama，引擎会先自动部署，再自动启动，用户不需要手动开终端。"
                />
                <CheckboxRow
                  checked={localModelConfig.autoPull}
                  onChange={(value) => handleLocalModelConfigChange("autoPull", value)}
                  label="首次缺模型时自动下载"
                  subtext={`开启后缺少 ${localTargetModel} 时会自动拉取；首次准备会稍慢。`}
                />
                <CheckboxRow
                  checked={localModelConfig.autoPrepareOnLaunch}
                  onChange={(value) => handleLocalModelConfigChange("autoPrepareOnLaunch", value)}
                  label="启动程序后自动准备本地模型"
                  subtext="建议保留开启，让打包版启动后先把服务和模型状态准备好。"
                />
              </div>

              <p className="text-[11px] text-gray-400 leading-relaxed border-l-2 border-gray-200 pl-3">
                小白直接可用的正确做法不是让他自己装 Ollama，而是正式打包时把本地引擎和模型资源一起带上。程序首启会自动把内置资源部署到运行目录；如果当前这里显示“未内置”，说明这份开发版本本身还没把资源打进去。
              </p>
            </div>
          </Section>

          {/* Section 4: Recognition Mode */}
          <Section title="识别模式">
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              这里控制识别模式是否优先使用本地 OCR，以及本地 OCR 未命中时是否启用 AI 辅助复核。
            </p>

            <div className="mb-6 border-b border-gray-200/80 pb-5">
              <SelectInput
                label="识别方式 :"
                value={recognitionMode}
                onChange={setRecognitionMode}
                options={RECOGNITION_MODE_OPTIONS}
              />
              <p className="mt-2 text-[11px] text-gray-400 leading-relaxed border-l-2 border-gray-200 pl-3">
                截图 OCR 识别会监听剪贴板截图并使用 OCR 识别品牌；DOM 文本识别会监听剪贴板文本，直接正则匹配品牌后渲染为截图展示。
              </p>
            </div>

            <div className="space-y-3 mb-6">
              <CheckboxRow checked={isLocalOCREnabled} onChange={setLocalOCRHandler} label="启用本地 OCR 自动识别" />
              <CheckboxRow checked={aiAssist} onChange={setAiAssist} label="本地 OCR 未命中时启用 AI 识别辅助" />
              <CheckboxRow
                checked={floatingWindowResidentEnabled}
                onChange={setFloatingWindowResidentEnabled}
                label="悬浮窗常驻"
                subtext="开启后，识别任务未运行时悬浮窗会保留为文章录入与快捷工具；识别任务启动后自动切回关键词引导。"
              />
            </div>
            
            <div className={`transition-opacity duration-300 ${!aiAssist ? 'opacity-50 pointer-events-none' : ''}`}>
              <div className="flex items-center gap-2 mb-3">
                <label className="text-[12px] font-bold text-gray-900">大模型配置</label>
                <span className="text-[11px] text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded font-medium">必须支持视觉能力</span>
              </div>
              <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
                <SelectInput 
                  label="AI 平台 :" 
                  value={ocrPlatform} 
                  onChange={(val) => {
                    setOcrPlatform(val);
                    const platform = recognitionVisionPlatforms.find(p => p.id === val);
                    if (platform) setOcrModel(platform.models[0]);
                  }}
                  options={recognitionVisionPlatforms.map(p => ({ value: p.id, label: p.name }))}
                />
                <SelectInput 
                  label="调用模型 :" 
                  value={ocrModel} 
                  onChange={setOcrModel}
                  options={ocrModelOptions.map(m => ({ value: m, label: m }))}
                />
              </div>
            </div>

            <p className="text-[11px] text-gray-400 mt-5 leading-relaxed border-l-2 border-gray-200 pl-3">
              关闭本地 OCR 后，识别模式会进入人工推进；开启 AI 辅助后，会在 OCR 未识别到目标品牌时再调用 AI 复核。
            </p>
          </Section>

          <Section title="接口模式">
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              这里只展示已确认“快速模式”和“深度思考”分别对应不同模型的平台。任务在接口模式下运行时，会根据关键词里的深度思考开关自动选择下方对应模型。
            </p>
            <p className="text-[11px] text-gray-400 mb-4 leading-relaxed border-l-2 border-gray-200 pl-3">
              如果某一项留空，系统会继续回退到 API 配置页里的主模型；只有同时填了快速模型和深度思考模型，才会完全按双模型切换。
            </p>

            {apiModePlatforms.length === 0 ? (
              <div className="text-[12px] text-gray-500 border-l-2 border-gray-200 pl-3 py-1">
                当前没有需要单独区分快速模型与深度思考模型的平台。
              </div>
            ) : (
              <div className="space-y-3">
                {apiModePlatforms.map((platform) => {
                  const currentModels = apiModeModels[platform.id] || { fastModel: "", deepModel: "" };
                  const isOpen = !!apiModePanelsOpen[platform.id];
                  return (
                    <div key={platform.id} className="border-b border-gray-200/80 overflow-hidden">
                      <button
                        type="button"
                        onClick={() => toggleApiModePanel(platform.id)}
                        className="w-full grid grid-cols-[minmax(0,1fr)_24px] md:grid-cols-[150px_minmax(0,1fr)_24px] gap-4 px-0 py-4 text-left hover:bg-transparent transition-colors"
                      >
                        <span className="text-[12px] font-bold text-gray-900 pt-0.5">{platform.name}</span>
                        <div className="min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-[10px] font-bold text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                              双模型
                            </span>
                            <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${platform.hasKey ? "text-emerald-600 bg-emerald-50" : "text-gray-500 bg-gray-100"}`}>
                              {platform.hasKey ? "已配置 Key" : "未配置 Key"}
                            </span>
                          </div>
                          <p className="mt-2 text-[11px] text-gray-500 leading-relaxed">
                            {platform.note || "该平台会按快速模式和深度思考分别调用不同模型。"}
                          </p>
                        </div>
                        <ChevronDown className={`w-4 h-4 shrink-0 text-gray-400 mt-0.5 transition-transform ${isOpen ? "rotate-180" : ""}`} />
                      </button>

                      {isOpen && (
                        <div className="grid grid-cols-1 md:grid-cols-[150px_minmax(0,1fr)] gap-4 border-t border-gray-200/80 pt-4 pb-4">
                          <div className="hidden md:block" />
                          <div>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                              <ControlledTextInput
                                label="快速模式模型 :"
                                value={currentModels.fastModel}
                                onChange={(value) => updateApiModeModel(platform.id, "fastModel", value)}
                                placeholder={platform.fastDefault || "填写快速模式模型名"}
                              />
                              <ControlledTextInput
                                label="深度思考模型 :"
                                value={currentModels.deepModel}
                                onChange={(value) => updateApiModeModel(platform.id, "deepModel", value)}
                                placeholder={platform.deepDefault || "填写深度思考模型名"}
                              />
                            </div>

                            {platform.models.length > 0 && (
                              <div className="mt-4 border-l-2 border-gray-200 pl-3 py-1">
                                <p className="text-[11px] text-gray-400 mb-2">可参考的已登记模型</p>
                                <div className="flex flex-wrap gap-1.5">
                                  {platform.models.map((model) => (
                                    <span key={`${platform.id}-${model}`} className="bg-gray-50 text-gray-700 px-2 py-1 rounded-md text-[11px] font-mono">
                                      {model}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Section>

          {/* Section 3: Smart Mode Configuration */}
          <Section title="智能模式">
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              智能模式下，系统会根据这里配置的大模型，自动处理非特定结构化的问答与查询请求。
            </p>
            <QueryExecutionSettingsCard
              title="智能模式查询规格"
              description="默认按平台分组串行执行，单个平台会在当前批次里持续复用到切换平台时再关闭；实验中的平台会话池策略暂时不要当作稳定能力使用。"
              settings={queryExecutionSettings.smart}
              onChange={(key, value) => updateQueryExecutionSetting("smart", key, value)}
            />
            <div className="flex flex-col gap-5">
              <div className="flex flex-col gap-3">
                <div className="flex items-center gap-2">
                  <label className="text-[12px] font-bold text-gray-900">文本大模型</label>
                </div>
                <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
                  <SelectInput 
                    label="AI 平台 :" 
                      value={smartPlatform} 
                      onChange={(val) => {
                        setSmartPlatform(val);
                        const platform = aiPlatforms.find(p => p.id === val);
                        if (platform) setSmartModel(platform.models[0]);
                      }}
                      options={aiPlatforms.map(p => ({ value: p.id, label: p.name }))}
                    />
                  <SelectInput 
                    label="调用模型 :" 
                    value={smartModel} 
                    onChange={setSmartModel}
                    options={smartModelOptions.map(m => ({ value: m, label: m }))}
                  />
                </div>
              </div>

              <div className="flex flex-col gap-3 pt-4 border-t border-gray-100">
                <CheckboxRow checked={smartVisionEnabled} onChange={setSmartVisionEnabled} label="启用视觉能力（多模态增强）" subtext="开启后，在智能模式下如果传入了图片或截屏，将自动调用下方配置的视觉模型进行处理。" />
                <div className={`transition-opacity duration-300 ${!smartVisionEnabled ? 'opacity-50 pointer-events-none' : ''}`}>
                  <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4 ml-6">
                    <SelectInput 
                      label="AI 平台 :" 
                      value={smartVisionPlatform} 
                      onChange={(val) => {
                        setSmartVisionPlatform(val);
                        const platform = visionPlatforms.find(p => p.id === val);
                        if (platform) setSmartVisionModel(platform.models[0]);
                      }}
                      options={visionPlatforms.map(p => ({ value: p.id, label: p.name }))}
                    />
                    <SelectInput 
                      label="调用模型 :" 
                      value={smartVisionModel} 
                      onChange={setSmartVisionModel}
                      options={smartVisionModelOptions.map(m => ({ value: m, label: m }))}
                    />
                  </div>
                </div>
              </div>
            </div>
          </Section>

          <Section title="Selector Agent">
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              这里单独配置浏览器自动抓取用的大模型，不占用智能模式或识别模式的账号。后续 selector 自动诊断、抽取和写回都会优先读这组配置。
            </p>

            <div className="space-y-3 mb-6">
              <CheckboxRow
                checked={selectorAgentEnabled}
                onChange={setSelectorAgentEnabled}
                label="启用 selector agent 自动抓取"
                subtext="关闭后只保留配置，不参与自动诊断或自动写入。"
              />
            </div>

            {selectorAgentPlatforms.length === 0 ? (
              <div className="text-[12px] text-gray-500 border-l-2 border-gray-200 pl-3 py-1">
                当前没有可用的平台模型，请先在 API 配置里补充至少一个可调用的模型。
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
                  <SelectInput
                    label="AI 平台 :"
                    value={selectorAgentPlatform}
                    onChange={(val) => {
                      setSelectorAgentPlatform(val);
                      const platform = selectorAgentPlatforms.find((item) => item.id === val);
                      if (platform) setSelectorAgentModel(platform.models[0] || "");
                    }}
                    options={selectorAgentPlatforms.map((item) => ({ value: item.id, label: item.name }))}
                  />
                  <SelectInput
                    label="调用模型 :"
                    value={selectorAgentModel}
                    onChange={setSelectorAgentModel}
                    options={selectorAgentModelOptions.map((model) => ({ value: model, label: model }))}
                  />
                </div>
                <p className="text-[11px] text-gray-400 leading-relaxed border-l-2 border-gray-200 pl-3">
                  这组配置只负责 selector 诊断和自动抓取时的模型选择，和智能模式、识别模式彼此独立。
                </p>
              </div>
            )}
          </Section>

          {/* Section 6: Search Plugin */}
          <Section title="搜搜与联网配置">
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              联网搜索桥接优先走 Tavily；此外，还可配置搜搜模式支持调用的大模型列表。
            </p>
            
            {/* Search Models Setup */}
            <div className="flex flex-col gap-2 mb-6">
              <label className="text-[12px] font-bold text-gray-900">搜搜模式大模型池 :</label>
              <div className="flex flex-wrap gap-1.5 border-b border-gray-200 py-2 min-h-[42px] items-center transition-colors">
                {searchModels.map(model => (
                  <span key={model} className="flex items-center gap-1 bg-gray-50 text-gray-700 px-2 py-1 rounded-md text-[11px] font-mono group/tag transition-colors">
                    {model}
                    <button onClick={() => removeSearchModel(model)} className="text-gray-400 hover:text-red-500 opacity-0 group-hover/tag:opacity-100 transition-opacity">
                      <X className="w-3 h-3" />
                    </button>
                  </span>
                ))}
                <input 
                  type="text"
                  value={newSearchModel}
                  onChange={e => setNewSearchModel(e.target.value)}
                  onKeyDown={handleAddSearchModel}
                  placeholder="输入模型名称按回车添加..."
                  className="flex-1 min-w-[150px] bg-transparent text-[12px] font-mono outline-none placeholder-gray-400 px-1 text-gray-800"
                />
              </div>
            </div>

            <div className="flex flex-col gap-2">
              <label className="text-[12px] font-bold text-gray-900">
                Tavily API Key <span className="text-gray-400 font-normal">(Bearer Token, 以 tvly- 开头) :</span>
              </label>
              <input 
                type="text" 
                value={tavilyKey}
                onChange={e => setTavilyKey(e.target.value)}
                className="w-full bg-transparent border-0 border-b border-gray-200 rounded-none text-[12px] px-0 py-2 outline-none focus:border-gray-900 transition-colors font-mono text-gray-800"
              />
              <span className="text-[11px] text-gray-400 mt-1">未填写时，会自动回退到内置公开搜索桥接。</span>
            </div>
          </Section>

          {/* Section 7: Decoration Template */}
          <Section title="页面原始截图装饰模板">
            <p className="text-[12px] text-gray-500 mb-4 leading-relaxed">
              这里只作用于“页面原始截图”。DOM 文本生成会固定走 Surfaced 模版，不会套用这里的旧装饰样式。
            </p>
            
            <div className="mb-6">
              <CheckboxRow checked={screenshotEnabled} onChange={setScreenshotEnabled} label="启用页面截图装饰模板" />
            </div>

            <div className={`transition-opacity duration-300 space-y-6 ${!screenshotEnabled ? 'opacity-50 pointer-events-none' : ''}`}>
              
              {/* Text Inputs */}
              <div className="space-y-4">
                <TextInput label="标题 (支持 {brand} / {platform} / {keyword} / {time}) :" value={screenshotTitle} onChange={setScreenshotTitle} />
                <TextInput label="副标题 :" value={screenshotSubtitle} onChange={setScreenshotSubtitle} />
                <TextInput label="页脚文案 :" value={screenshotFooter} onChange={setScreenshotFooter} />
              </div>

              {/* Toggles */}
              <div className="flex items-center gap-6">
                <Checkbox checked={showTime} onChange={setShowTime} label="显示时间" />
                <Checkbox checked={showFooter} onChange={setShowFooter} label="显示页脚" />
                <Checkbox checked={showHighlight} onChange={setShowHighlight} label="绘制高亮框" />
              </div>

              {/* Colors & Dimensions Grid */}
              <div className="grid grid-cols-2 gap-x-10 gap-y-4 pt-4 border-t border-gray-100">
                <ColorInput label="强调色 :" value={accentColor} onChange={setAccentColor} />
                <ColorInput label="背景起始 :" value={backgroundStart} onChange={setBackgroundStart} />
                <ColorInput label="背景结束 :" value={backgroundEnd} onChange={setBackgroundEnd} />
                <ColorInput label="头部起始 :" value={headerStart} onChange={setHeaderStart} />
                <ColorInput label="头部结束 :" value={headerEnd} onChange={setHeaderEnd} />
              </div>

              <div className="grid grid-cols-2 gap-x-10 gap-y-4 pt-4 border-t border-gray-100">
                <DimensionInput label="外边距 :" value={outerPadding} onChange={setOuterPadding} />
                <DimensionInput label="头部高度 :" value={headerHeight} onChange={setHeaderHeight} />
                <DimensionInput label="卡片圆角 :" value={radius} onChange={setRadius} />
                <DimensionInput label="图片圆角 :" value={imageRadius} onChange={setImageRadius} />
              </div>

              {/* Reset Action */}
              <div className="flex items-center gap-3 pt-6">
                <button onClick={handleResetScreenshotTemplate} className="flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-700 hover:text-gray-900 transition-colors bg-transparent">
                  <RefreshCw className="w-3.5 h-3.5" /> 恢复默认装饰
                </button>
                <span className="text-[11px] text-gray-400">改完保存后，后续截图会默认使用新样式。</span>
              </div>
            </div>
          </Section>

          <Section
            title="天气与节日"
            action={(
              <button
                type="button"
                onClick={handleRefreshContext}
                disabled={refreshingContext}
                className="flex items-center gap-1.5 text-[12px] font-bold text-gray-600 hover:text-gray-900 transition-colors px-0 py-1.5 disabled:opacity-50"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${refreshingContext ? "animate-spin" : ""}`} />
                {refreshingContext ? "刷新中..." : "立即刷新"}
              </button>
            )}
          >
            <div className="space-y-5">
              <div className="border-b border-gray-200/80 pb-5 space-y-4">
                <CheckboxRow
                  checked={weatherReminderEnabled}
                  onChange={setWeatherReminderEnabled}
                  label="启用天气提醒"
                  subtext="关闭后首页不会再显示天气文案。"
                />
                <ControlledTextInput
                  label="天气城市 :"
                  value={weatherCity}
                  onChange={setWeatherCity}
                  placeholder="例如：武汉 / 上海 / Beijing"
                />
                <ControlledTextInput
                  label="彩云 Token :"
                  value={weatherToken}
                  onChange={setWeatherToken}
                  placeholder="填写彩云天气开放平台 Token"
                />
                <div className="flex items-center gap-3 text-[12px]">
                  <span className="w-24 font-bold text-gray-700">刷新间隔 :</span>
                  <NumberInput
                    value={weatherRefreshMinutes}
                    onChange={setWeatherRefreshMinutes}
                    min={15}
                    max={1440}
                  />
                  <span className="text-gray-500 font-medium">分钟</span>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-[120px_minmax(0,1fr)] gap-3 pt-2">
                  <div className="text-[11px] font-bold tracking-wide text-gray-400">天气快照</div>
                  <div>
                    <div className="text-[12px] text-gray-700 leading-relaxed">
                      {weatherSnapshotLine || "暂未获取天气快照"}
                    </div>
                    {weatherSnapshotTime ? (
                      <div className="mt-1 text-[11px] text-gray-400">更新时间：{weatherSnapshotTime}</div>
                    ) : null}
                    {weatherSnapshotError ? (
                      <div className="mt-1 text-[11px] text-amber-600">接口状态：{weatherSnapshotError}</div>
                    ) : null}
                  </div>
                </div>
              </div>

              {contextRefreshMessage ? (
                <div className="text-[12px] font-medium text-blue-600">{contextRefreshMessage}</div>
              ) : null}
            </div>
          </Section>

          <Section title="应用更新">
            <div className="space-y-5">
              <div className="border-b border-gray-200/80 pb-4 space-y-3">
                <div className="text-[12px] font-bold text-gray-900">当前版本</div>
                <div className="text-[18px] font-black tracking-tight text-gray-900">{appVersionLabel}</div>
                <div className="text-[11px] text-gray-500 leading-relaxed">
                  打包后的程序运行中不能直接覆盖自己；更稳的做法是检查到新版本后，跳转下载页或启动独立 updater，在程序退出后完成替换。
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <SelectInput
                  label="更新通道 :"
                  value={updateChannel}
                  onChange={setUpdateChannel}
                  options={UPDATE_CHANNEL_OPTIONS}
                />
                <CheckboxRow
                  checked={updateAutoCheckEnabled}
                  onChange={setUpdateAutoCheckEnabled}
                  label="启动后自动检查更新"
                  subtext="当前先只保存配置和检查能力，后续再接入正式升级器。"
                />
              </div>

              <ControlledTextInput
                label="升级清单地址 :"
                value={updateManifestUrl}
                onChange={setUpdateManifestUrl}
                placeholder="https://example.com/surfaced-manifest.json 或本地文件路径"
              />
              <ControlledTextInput
                label="下载页地址 :"
                value={updateDownloadPageUrl}
                onChange={setUpdateDownloadPageUrl}
                placeholder="https://example.com/surfaced/releases"
              />

              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={handleCheckAppUpdate}
                  disabled={updateCheckPending}
                  className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${updateCheckPending ? "animate-spin" : ""}`} />
                  {updateCheckPending ? "检查中..." : "立即检查更新"}
                </button>
                <span className="text-[11px] text-gray-500">
                  支持远程 JSON manifest，也支持本地文件路径调试。
                </span>
              </div>

              {updateCheckMessage ? (
                <div className={`border-l-2 pl-3 py-1 text-[12px] leading-relaxed ${
                  updateAvailable === true
                    ? "border-emerald-300 text-emerald-800"
                    : updateAvailable === false
                      ? "border-gray-200 text-gray-700"
                      : "border-amber-300 text-amber-900"
                }`}>
                  {updateCheckMessage}
                </div>
              ) : null}

              {(updateLatestLabel || updateDownloadUrl || updateNotes) ? (
                <div className="border-t border-gray-200/80 pt-4 space-y-2">
                  <div className="flex items-center justify-between gap-3">
                    <div className="text-[12px] font-bold text-gray-900">最新版本</div>
                    {updateLatestLabel ? (
                      <span className="text-[11px] font-bold text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                        {updateLatestLabel}
                      </span>
                    ) : null}
                  </div>
                  {updatePublishedAt ? (
                    <div className="text-[11px] text-gray-500">发布时间：{updatePublishedAt}</div>
                  ) : null}
                  {updateNotes ? (
                    <div className="text-[12px] text-gray-700 leading-relaxed whitespace-pre-wrap">{updateNotes}</div>
                  ) : null}
                  {updateDownloadUrl ? (
                    <div className="text-[11px] text-gray-500 break-all">下载地址：{updateDownloadUrl}</div>
                  ) : null}
                  {updatePackageSha256 ? (
                    <div className="text-[11px] text-gray-500 break-all">安装包 SHA256：{updatePackageSha256}</div>
                  ) : null}
                  {updateDownloadUrl ? (
                    <button
                      type="button"
                      onClick={handlePrepareAppUpdate}
                      disabled={updateCheckPending || updatePreparePending}
                      className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-900 transition-colors hover:text-black disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <Download className={`h-3.5 w-3.5 ${updatePreparePending ? "animate-pulse" : ""}`} />
                      {updatePreparePending ? "下载校验中..." : "下载并准备安装包"}
                    </button>
                  ) : null}
                </div>
              ) : null}

              <div className="border-t border-gray-200/80 pt-4 space-y-4">
                <div className="flex flex-col gap-1">
                  <div className="text-[12px] font-bold text-gray-900">本地目录安装更新</div>
                  <div className="text-[11px] text-gray-500 leading-relaxed">
                    先把新版本完整解压到一个目录，再把这个目录路径填到这里。后续接云端下载时，底层仍会复用同一个 updater。
                  </div>
                </div>

                <ControlledTextInput
                  label="新版本目录 :"
                  value={localUpdateSourceDir}
                  onChange={setLocalUpdateSourceDir}
                  placeholder="/path/to/Surfaced-new"
                />

                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={handlePickLocalUpdateDirectory}
                    disabled={localUpdatePending || updatePreparePending}
                    className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <Upload className="h-3.5 w-3.5" />
                    选择目录
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setLocalUpdateSourceDir("");
                      setLocalUpdatePlanSummary("");
                      setLocalUpdatePlanMessage("已清空更新目录");
                    }}
                    disabled={localUpdatePending || updatePreparePending}
                    className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-400 transition-colors hover:text-gray-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <X className="h-3.5 w-3.5" />
                    清空
                  </button>
                  <span className="text-[11px] text-gray-500">
                    桌面模式下会弹出原生目录选择器；其他运行方式仍可手动填写路径。
                  </span>
                </div>

                <div className="space-y-2">
                  <CheckboxRow
                    checked={localUpdateCleanupSource}
                    onChange={setLocalUpdateCleanupSource}
                    label="安装完成后清理解压目录"
                    subtext="适合新版本是临时解压目录的情况。"
                  />
                  <CheckboxRow
                    checked={localUpdateRestartAfterInstall}
                    onChange={setLocalUpdateRestartAfterInstall}
                    label="安装完成后自动重启程序"
                    subtext="关闭后只完成替换，不自动重新打开。"
                  />
                  <CheckboxRow
                    checked={localUpdateExitAfterLaunch}
                    onChange={setLocalUpdateExitAfterLaunch}
                    label="启动更新器后自动退出当前程序"
                    subtext="桌面模式下建议开启，这样 updater 能在程序退出后继续安装。"
                  />
                </div>

                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={handleBuildLocalUpdatePlan}
                    disabled={localUpdatePending || updatePreparePending}
                    className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-700 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <RefreshCw className={`h-3.5 w-3.5 ${localUpdatePending ? "animate-spin" : ""}`} />
                    预览安装计划
                  </button>
                  <button
                    type="button"
                    onClick={handleStartLocalUpdate}
                    disabled={localUpdatePending || updatePreparePending}
                    className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-900 transition-colors hover:text-black disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <Save className="h-3.5 w-3.5" />
                    启动安装更新
                  </button>
                </div>

                {localUpdatePlanMessage ? (
                  <div className="border-l-2 border-blue-300 pl-3 py-1 text-[12px] text-blue-800 leading-relaxed">
                    {localUpdatePlanMessage}
                  </div>
                ) : null}

                {localUpdatePlanSummary ? (
                  <pre className="border-l-2 border-gray-200 pl-3 py-1 text-[11px] text-gray-600 whitespace-pre-wrap break-all font-mono">
                    {localUpdatePlanSummary}
                  </pre>
                ) : null}
              </div>
            </div>
          </Section>

          <div className="pt-2 text-center text-[11px] font-medium tracking-[0.08em] text-gray-400">
            Copyright &copy; Saffron<span className="text-[#14C7F3]">.</span> All rights reserved.
          </div>
        </div>
      </div>
      
      {/* Modals */}
      <ConfirmModal 
        isOpen={showLogoutConfirm}
        onClose={() => setShowLogoutConfirm(false)}
        onConfirm={() => {
          setShowLogoutConfirm(false);
          onLogout?.();
        }}
        title="确认退出登录？"
        message="退出后会切换为访客模式，本地活动仍可查看，但账号信息与高级权限会暂时收起，确认退出吗？"
        confirmText="退出"
        type="danger"
      />
    </div>
  );
}

// --- Subcomponents ---

function QueryExecutionSettingsCard({
  title,
  description,
  settings,
  onChange,
}: {
  title: string,
  description: string,
  settings: QueryExecutionModeSettings,
  onChange: (key: keyof QueryExecutionModeSettings, value: string | boolean) => void,
}) {
  const experimentalPool = settings.strategy === "session_pool";

  return (
    <div className="mb-6 border-b border-gray-200/80 pb-5 space-y-4">
      <div className="flex flex-col gap-1">
        <span className="text-[12px] font-bold text-gray-900">{title}</span>
        <span className="text-[11px] text-gray-500 leading-relaxed">{description}</span>
      </div>

      <SelectInput
        label="执行策略 :"
        value={settings.strategy}
        onChange={(value) => onChange("strategy", value)}
        options={QUERY_EXECUTION_STRATEGY_OPTIONS}
      />

      {!experimentalPool ? (
        <div className="border-l-2 border-emerald-300 pl-3 text-[11px] text-emerald-800 leading-relaxed">
          当前稳定策略会按平台分组串行执行，同一平台尽量连续完成后再切换，不再按单条查询反复重启浏览器。
        </div>
      ) : (
        <div className="border-l-2 border-amber-300 pl-3 text-[11px] text-amber-900 leading-relaxed">
          平台会话池仍处于实验阶段，适合单独验证，不建议当作日常稳定策略长期开启。
        </div>
      )}

      <div className={`space-y-4 transition-opacity duration-200 ${experimentalPool ? "opacity-100" : "opacity-50 pointer-events-none"}`}>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <SelectInput
            label="池内调度方式 :"
            value={settings.sessionPoolDispatch}
            onChange={(value) => onChange("sessionPoolDispatch", value)}
            options={SESSION_POOL_DISPATCH_OPTIONS}
          />
          <LabeledNumberInput
            label="平台批次大小 :"
            value={settings.sessionPoolPlatformBatchSize}
            onChange={(value) => onChange("sessionPoolPlatformBatchSize", value)}
            suffix="条"
            min={1}
            max={20}
          />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <LabeledNumberInput
            label="会话寿命最小值 :"
            value={settings.sessionTtlMinutesMin}
            onChange={(value) => onChange("sessionTtlMinutesMin", value)}
            suffix="分钟"
            min={10}
            max={600}
          />
          <LabeledNumberInput
            label="会话寿命最大值 :"
            value={settings.sessionTtlMinutesMax}
            onChange={(value) => onChange("sessionTtlMinutesMax", value)}
            suffix="分钟"
            min={10}
            max={600}
          />
          <LabeledNumberInput
            label="最大查询数最小值 :"
            value={settings.sessionMaxQueriesMin}
            onChange={(value) => onChange("sessionMaxQueriesMin", value)}
            suffix="次"
            min={1}
            max={500}
          />
          <LabeledNumberInput
            label="最大查询数最大值 :"
            value={settings.sessionMaxQueriesMax}
            onChange={(value) => onChange("sessionMaxQueriesMax", value)}
            suffix="次"
            min={1}
            max={500}
          />
          <LabeledNumberInput
            label="最少查询统计窗口 :"
            value={settings.minQueriesWindowMinutes}
            onChange={(value) => onChange("minQueriesWindowMinutes", value)}
            suffix="分钟"
            min={1}
            max={180}
          />
          <LabeledNumberInput
            label="窗口内最少查询数 :"
            value={settings.minQueriesPerWindow}
            onChange={(value) => onChange("minQueriesPerWindow", value)}
            suffix="次"
            min={1}
            max={100}
          />
          <LabeledNumberInput
            label="单次查询超时阈值 :"
            value={settings.singleQueryTimeoutMinutes}
            onChange={(value) => onChange("singleQueryTimeoutMinutes", value)}
            suffix="分钟"
            min={1}
            max={180}
          />
          <LabeledNumberInput
            label="无进展重启阈值 :"
            value={settings.noProgressTimeoutMinutes}
            onChange={(value) => onChange("noProgressTimeoutMinutes", value)}
            suffix="分钟"
            min={1}
            max={180}
          />
          <LabeledNumberInput
            label="重启冷却时间 :"
            value={settings.minRestartCooldownMinutes}
            onChange={(value) => onChange("minRestartCooldownMinutes", value)}
            suffix="分钟"
            min={0}
            max={180}
          />
          <LabeledNumberInput
            label="结构性异常重启阈值 :"
            value={settings.restartAfterStructuralFailures}
            onChange={(value) => onChange("restartAfterStructuralFailures", value)}
            suffix="次"
            min={1}
            max={20}
          />
        </div>

        <CheckboxRow
          checked={settings.restartAfterManualRecovery}
          onChange={(value) => onChange("restartAfterManualRecovery", value)}
          label="人工恢复后优先轮换平台会话"
          subtext="命中验证码或人工接管后，本次查询结束会优先重启该平台会话。"
        />
      </div>
    </div>
  );
}

function LabeledNumberInput({
  label,
  value,
  onChange,
  min,
  max,
  suffix,
}: {
  label: string,
  value: string,
  onChange: (val: string) => void,
  min: number,
  max: number,
  suffix: string,
}) {
  return (
    <div className="flex items-center gap-3 text-[12px]">
      <span className="w-36 shrink-0 font-bold text-gray-700">{label}</span>
      <NumberInput value={value} onChange={onChange} min={min} max={max} />
      <span className="text-gray-500 font-medium">{suffix}</span>
    </div>
  );
}

function Section({
  title,
  action,
  children,
  collapsible = false,
  defaultCollapsed = false,
}: {
  title: string,
  action?: React.ReactNode,
  children: React.ReactNode,
  collapsible?: boolean,
  defaultCollapsed?: boolean,
}) {
  const [collapsed, setCollapsed] = useState(collapsible ? defaultCollapsed : false);

  return (
    <section className="pb-8 border-b border-gray-200/80 last:border-b-0">
      <div className={`flex items-center justify-between ${collapsed ? "" : "mb-5"}`}>
        <button
          type="button"
          onClick={() => {
            if (collapsible) {
              setCollapsed((prev) => !prev);
            }
          }}
          className={`flex items-center gap-2 text-left ${collapsible ? "cursor-pointer" : "cursor-default"}`}
        >
          <div className="w-1 h-3.5 bg-blue-600 rounded-full"></div>
          <h2 className="text-[14px] font-bold text-gray-900 tracking-tight">{title}</h2>
        </button>
        <div className="flex items-center gap-2">
          {action && <div>{action}</div>}
          {collapsible ? (
            <button
              type="button"
              onClick={() => setCollapsed((prev) => !prev)}
              className="flex items-center justify-center w-8 h-8 rounded-md border border-gray-200 bg-white hover:bg-gray-50 transition-colors"
              aria-label={collapsed ? `展开${title}` : `收起${title}`}
            >
              <ChevronDown className={`w-4 h-4 text-gray-400 transition-transform ${collapsed ? "" : "rotate-180"}`} />
            </button>
          ) : null}
        </div>
      </div>
      {!collapsed ? <div>{children}</div> : null}
    </section>
  );
}

function Checkbox({ checked, onChange, label, className = "" }: { checked: boolean, onChange: (val: boolean) => void, label: string, className?: string }) {
  const toggle = useCallback(() => onChange(!checked), [checked, onChange]);
  return (
    <label
      className={`flex items-center gap-2 cursor-pointer group ${className}`}
      onClick={(event) => {
        event.preventDefault();
        toggle();
      }}
      onKeyDown={(event) => {
        if (event.key === " " || event.key === "Enter") {
          event.preventDefault();
          toggle();
        }
      }}
      tabIndex={0}
      role="checkbox"
      aria-checked={checked}
    >
      <div className={`w-4 h-4 rounded border flex items-center justify-center transition-colors ${
        checked ? 'bg-blue-600 border-blue-600' : 'bg-white border-gray-300 group-hover:border-blue-400'
      }`}>
        {checked && <svg width="10" height="8" viewBox="0 0 10 8" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M1 4L3.5 6.5L9 1" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>}
      </div>
      <span className="text-[12px] text-gray-900 font-medium select-none group-hover:text-blue-600 transition-colors">{label}</span>
    </label>
  );
}

function CheckboxRow({ checked, onChange, label, subtext, className = "" }: { checked: boolean, onChange: (val: boolean) => void, label: string, subtext?: string, className?: string }) {
  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <Checkbox checked={checked} onChange={onChange} label={label} />
      {subtext && <span className="text-[11px] text-gray-500 ml-6">{subtext}</span>}
    </div>
  );
}

function NumberInput({ value, onChange, min, max }: { value: string, onChange: (val: string) => void, min: number, max: number }) {
  const handleBlur = () => {
    let num = parseInt(value, 10);
    if (isNaN(num)) num = min;
    if (num < min) num = min;
    if (num > max) num = max;
    onChange(num.toString().padStart(2, '0'));
  };

  const handleStep = (step: number) => {
    let num = parseInt(value, 10) || 0;
    num += step;
    if (num < min) num = max;
    if (num > max) num = min;
    onChange(num.toString().padStart(2, '0'));
  };

  return (
    <div className="flex items-center border-b border-gray-200 bg-transparent overflow-hidden h-[26px]">
      <input 
        type="text" 
        value={value} 
        onChange={(e) => onChange(e.target.value)}
        onBlur={handleBlur}
        className="w-8 text-center text-[12px] font-mono outline-none text-gray-900 bg-transparent"
      />
      <div className="flex flex-col border-l border-gray-200/70">
        <button onClick={() => handleStep(1)} className="px-1 h-[13px] flex items-center justify-center hover:bg-gray-50 border-b border-gray-200/70 text-gray-500">
          <svg width="6" height="4" viewBox="0 0 6 4" fill="currentColor"><path d="M3 0L6 4H0L3 0Z"/></svg>
        </button>
        <button onClick={() => handleStep(-1)} className="px-1 h-[13px] flex items-center justify-center hover:bg-gray-50 text-gray-500">
          <svg width="6" height="4" viewBox="0 0 6 4" fill="currentColor"><path d="M3 4L0 0H6L3 4Z"/></svg>
        </button>
      </div>
    </div>
  );
}

function ControlledTextInput({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string,
  value: string,
  onChange: (val: string) => void,
  type?: string,
  placeholder?: string,
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-[12px] font-bold text-gray-500">{label}</label>
      <input 
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-transparent border-0 border-b border-gray-200 rounded-none text-[12px] px-0 py-2 outline-none focus:border-gray-900 transition-colors text-gray-800"
      />
    </div>
  );
}

function SelectorHealResultPanel({
  result,
  message,
}: {
  result: SelectorHealFieldResult | null,
  message: string,
}) {
  const candidates = (result?.candidates || []).slice(0, 3);
  if (!result && !message) {
    return null;
  }
  const currentStatus = String(result?.current_status || "").trim();
  const currentStatusLabel = result?.saved
    ? "已修复并保存"
    : currentStatus === "healthy"
      ? "当前 selector 正常"
      : currentStatus === "missing"
        ? "当前 selector 失效"
        : currentStatus
          ? `当前状态：${currentStatus}`
          : "";
  const selectorAgentLabel = result?.selector_agent_used
    ? `模型辅助：${result.selector_agent_platform || "unknown"}${result.selector_agent_model ? ` / ${result.selector_agent_model}` : ""}${result.selector_agent_confidence ? `（${Math.round(result.selector_agent_confidence * 100)}%）` : ""}`
    : result?.selector_agent_error
      ? `模型辅助失败：${result.selector_agent_error}`
      : result?.selector_agent_reason
        ? `模型辅助跳过：${result.selector_agent_reason}`
      : "";
  const verifiedSelector = String(
    result?.selector
    || result?.verified_selector
    || candidates.find((candidate) => Boolean(candidate.verified))?.selector
    || "",
  ).trim();
  return (
    <div className="border-l-2 border-gray-200 pl-3 text-[11px] text-gray-500">
      {message ? (
        <div className="mb-2 leading-relaxed text-gray-600">{message}</div>
      ) : null}
      {result ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {currentStatusLabel ? (
              <span className="font-bold text-gray-700">{currentStatusLabel}</span>
            ) : null}
            <span className="text-gray-400">风险：{result.field_risk_level || "unknown"}</span>
            <span className={result.saved ? "text-emerald-700" : result.save_error ? "text-rose-700" : result.verify_status === "passed" ? "text-emerald-700" : "text-gray-400"}>
              {result.saved ? "已自动保存" : result.save_error ? "保存失败" : result.verify_status === "passed" ? "已验证待保存" : "仅诊断"}
            </span>
            {selectorAgentLabel ? (
              <span className={result.selector_agent_used ? "text-sky-700" : "text-amber-700"}>{selectorAgentLabel}</span>
            ) : null}
          </div>
          {verifiedSelector ? (
            <div className="border-t border-gray-100 pt-2">
              <div className="min-w-0">
                <div className="mb-1 font-bold text-gray-700">已验证 selector</div>
                <code className="break-all rounded bg-gray-50 px-1.5 py-0.5 text-[11px] text-gray-800">{verifiedSelector}</code>
              </div>
            </div>
          ) : null}
          {!verifiedSelector && candidates.length > 0 ? (
            <div className="border-t border-gray-100 pt-2 leading-relaxed text-amber-700">
              已找到候选，但没有通过点击验证。
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function SelectorPauseStatePanel({
  result,
  message,
}: {
  result: SelectorPauseStateResponse | null,
  message: string,
}) {
  if (!result && !message) {
    return null;
  }
  const selector = String(result?.verified_selector || result?.summary?.suggested_selector || "").trim();
  const sampleCount = Number(result?.summary?.sample_count || 0);
  const generatingSampleCount = Number(result?.summary?.generating_sample_count || 0);
  const topPrefix = String(result?.summary?.top_path_prefixes?.[0]?.[0] || "").trim();
  return (
    <div className="border-l-2 border-blue-200 pl-3 text-[11px] text-gray-500">
      {message ? (
        <div className={`mb-2 leading-relaxed ${result?.ok ? "text-blue-700" : "text-amber-700"}`}>{message}</div>
      ) : null}
      {result ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className={result.ok ? "font-bold text-blue-700" : "font-bold text-amber-700"}>
              {result.ok ? (result.saved ? "暂停态已确认并保存" : "暂停态已确认") : "暂停态未确认"}
            </span>
            {sampleCount > 0 ? <span className="text-gray-400">采样 {sampleCount} 次</span> : null}
            {generatingSampleCount > 0 ? <span className="text-gray-400">生成态 {generatingSampleCount} 次</span> : null}
          </div>
          {selector ? (
            <div className="border-t border-gray-100 pt-2">
              <div className="mb-1 font-bold text-gray-700">暂停态表达</div>
              <code className="break-all rounded bg-gray-50 px-1.5 py-0.5 text-[11px] text-gray-800">{selector}</code>
            </div>
          ) : null}
          {topPrefix ? (
            <div className="border-t border-gray-100 pt-2">
              <div className="mb-1 font-bold text-gray-700">SVG path 前缀</div>
              <code className="break-all rounded bg-gray-50 px-1.5 py-0.5 text-[11px] text-gray-800">{topPrefix}</code>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function AvatarCropViewport({
  draft,
  onPointerDown,
  onPointerMove,
  onPointerUp,
}: {
  draft: AvatarCropDraft,
  onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => void,
  onPointerMove: (event: React.PointerEvent<HTMLDivElement>) => void,
  onPointerUp: (event: React.PointerEvent<HTMLDivElement>) => void,
}) {
  const { displayWidth, displayHeight } = getAvatarCropMetrics(draft);

  return (
    <div
      className="relative rounded-[28px] overflow-hidden border border-gray-200 bg-white shrink-0 select-none touch-none cursor-grab active:cursor-grabbing"
      style={{ width: AVATAR_CROP_VIEWPORT, height: AVATAR_CROP_VIEWPORT }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      <img
        src={draft.src}
        alt="Avatar Crop"
        draggable={false}
        className="absolute max-w-none pointer-events-none"
        style={{
          width: displayWidth,
          height: displayHeight,
          left: `calc(50% + ${draft.offsetX}px)`,
          top: `calc(50% + ${draft.offsetY}px)`,
          transform: "translate(-50%, -50%)",
        }}
      />
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,transparent_56%,rgba(17,24,39,0.12)_100%)] pointer-events-none" />
      <div className="absolute inset-[18px] border border-white/90 rounded-[22px] shadow-[0_0_0_1px_rgba(17,24,39,0.08)] pointer-events-none" />
      <div className="absolute left-1/2 top-1/2 w-10 h-10 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/90 pointer-events-none" />
    </div>
  );
}

function TextInput({ label, value, onChange }: { label: string, value: string, onChange: (val: string) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-[12px] font-bold text-gray-500">{label}</label>
      <input 
        type="text" 
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-transparent border-0 border-b border-gray-200 rounded-none text-[12px] px-0 py-2 outline-none focus:border-gray-900 transition-colors text-gray-800"
      />
    </div>
  );
}

function ColorInput({ label, value, onChange }: { label: string, value: string, onChange: (val: string) => void }) {
  return (
    <div className="flex items-center justify-between">
      <label className="text-[12px] font-bold text-gray-500">{label}</label>
      <div className="flex items-center gap-2">
        <input 
          type="text" 
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-20 bg-transparent border-0 border-b border-gray-200 rounded-none text-[12px] px-0 py-1 outline-none focus:border-gray-900 transition-colors text-gray-800 font-mono uppercase text-right"
        />
        <div className="w-5 h-5 rounded border border-gray-200/50 shrink-0" style={{ backgroundColor: value }}></div>
      </div>
    </div>
  );
}

function DimensionInput({ label, value, onChange }: { label: string, value: string, onChange: (val: string) => void }) {
  return (
    <div className="flex items-center justify-between">
      <label className="text-[12px] font-bold text-gray-500">{label}</label>
      <NumberInput value={value} onChange={onChange} min={0} max={500} />
    </div>
  );
}

function SelectInput({ label, value, onChange, options }: { label: string, value: string, onChange: (val: string) => void, options: {value: string, label: string}[] }) {
  return (
    <div className="flex items-center gap-2">
      <label className="text-[12px] font-bold text-gray-500 whitespace-nowrap">{label}</label>
      <select 
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-transparent border-0 border-b border-gray-200 rounded-none text-[12px] px-0 py-1.5 outline-none focus:border-gray-900 transition-colors text-gray-800 cursor-pointer"
      >
        {options.map(opt => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
    </div>
  );
}
