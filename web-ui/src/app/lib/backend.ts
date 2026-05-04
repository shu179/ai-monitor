export type TrendPoint = {
  name: string;
  value: number;
  predict: number;
};

export type TrendSnapshot = {
  timeRange: "week" | "month" | "year";
  current: number;
  avg: number;
  peak: number;
  delta: number;
  data: TrendPoint[];
};

export type ModeCard = {
  key: string;
  title: string;
  desc: string;
  icon: "zap" | "eye" | "code" | "cpu";
  active: boolean;
};

export type DashboardSnapshot = {
  dateLabel: string;
  weekdayLabel: string;
  greeting: string;
  userName: string;
  headline: string;
  todayTaskCount: number;
  completedCount: number;
  runningCount: number;
  failedTaskCount?: number;
  failedTasks?: Array<{
    taskId: string;
    taskName: string;
    brand: string;
    status: string;
    statusMessage: string;
    failedModes: string[];
    failedUpdatedAt: string;
    failureKind: string;
    issueType: string;
    issueTitle: string;
    issueDescription: string;
    failedQueries: Array<{
      keyword: string;
      platform: string;
      mode: string;
      error_message: string;
      ts: string;
    }>;
    sendableSuccessCount?: number;
    canForceSendSuccess?: boolean;
  }>;
  todayIntercepted: number;
  trend: TrendSnapshot;
  sourceBreakdown: Array<{
    name: string;
    value: number;
  }>;
  taskCards: ModeCard[];
  mediaStats: Array<{
    name: string;
    auth: number;
    self: number;
    date?: string;
    label?: string;
  }>;
};

export type SidebarSnapshot = {
  userName: string;
  role: string;
  avatar: string;
  online: boolean;
};

export type AssistantSnapshot = {
  name: string;
  level: string;
  status: string;
  platform: string;
  model: string;
};

export type VersionSnapshot = {
  appName: string;
  slug: string;
  version: string;
  channel: string;
  label: string;
  full: string;
};

export type AppUpdateSettingsSnapshot = {
  channel: string;
  manifest_url: string;
  download_page_url: string;
  auto_check_enabled: boolean;
  manifest_sha256?: string;
  manifest_public_key?: string;
  require_signature?: boolean;
  require_package_hash?: boolean;
};

export type AppUpdateStatusSnapshot = {
  ok: boolean;
  configured: boolean;
  checked: boolean;
  update_available: boolean;
  message: string;
  current: VersionSnapshot;
  settings: AppUpdateSettingsSnapshot;
  latest?: {
    version: string;
    channel: string;
    label: string;
    published_at?: string;
    notes?: string;
  } | null;
  download_url?: string;
  download_page_url?: string;
  package_sha256?: string;
  manifest_source?: string;
  platform_keys?: string[];
  security?: Record<string, unknown>;
};

export type MonitoringSnapshot = {
  enabled: boolean;
  running: boolean;
  statusMessage: string;
};

export type ArticleSnapshot = {
  id: string | number;
  source: string;
  title: string;
  type: "media" | "self-media";
  category?: string;
  url?: string;
  platform?: string;
  media_name?: string;
  account_id?: string;
  account_name?: string;
  account_url?: string;
  account_platform?: string;
  account_platform_label?: string;
  ts?: string;
  published_at?: string;
  imported_at?: string;
  fetch_method?: string;
  matchedTasks?: string[];
  exportKeywords?: string[];
  reasonLines?: string[];
  classificationStatus?: "matched" | "unmatched";
  classificationMessage?: string;
  unmatchedReason?: string;
  referenced?: boolean;
  referencedTasks?: string[];
  lastReferencedAt?: string;
};

export type ArticleReferenceRankingPlatform = {
  id: string;
  label: string;
  configured: boolean;
  raw_count: number;
  event_count: number;
};

export type ArticleReferenceRankingDailyPoint = {
  date: string;
  article_count: number;
  event_count: number;
  raw_count: number;
};

export type ArticleReferenceRankingItem = {
  rank: number;
  score: number;
  article: {
    id: string;
    title: string;
    url: string;
    source: string;
    type: "media" | "self-media";
  };
  raw_ref_count: number;
  effective_event_count: number;
  weighted_mention_count: number;
  active_days: number;
  span_days: number;
  platform_count: number;
  platforms: ArticleReferenceRankingPlatform[];
  first_referenced_at: string;
  last_referenced_at: string;
};

export type ArticleReferenceRankingResponse = {
  ok?: boolean;
  algorithm_version: string;
  computed_at: string;
  data_coverage: {
    body_references_since: string;
    answer_text_fallback: boolean;
  };
  available_platforms: ArticleReferenceRankingPlatform[];
  daily_points: ArticleReferenceRankingDailyPoint[];
  items: ArticleReferenceRankingItem[];
  total: number;
  message?: string;
};

export type SelectorHealCandidate = {
  selector: string;
  score?: number;
  reason?: string;
  verified?: boolean;
  verify_reason?: string;
  verify_error?: string;
  text?: string;
  aria_label?: string;
  test_id?: string;
  tag?: string;
};

export type SelectorHealFieldResult = {
  ok?: boolean;
  platform: string;
  field: string;
  intent?: string;
  current_selector?: string;
  current_status?: string;
  field_risk_level?: string;
  apply_allowed?: boolean;
  verify_status?: string;
  verified_selector?: string;
  verify_reason?: string;
  selector?: string;
  previous_selector?: string;
  saved?: boolean;
  save_error?: string;
  candidates?: SelectorHealCandidate[];
  selector_agent_used?: boolean;
  selector_agent_platform?: string;
  selector_agent_model?: string;
  selector_agent_confidence?: number;
  selector_agent_reason?: string;
  selector_agent_error?: string;
  selector_agent_selected_selector?: string;
  selector_agent_image_used?: boolean;
  selector_agent_image_supported?: boolean;
};

export type SelectorHealResponse = {
  ok: boolean;
  platform?: string;
  verify?: boolean;
  auto_apply?: boolean;
  vision?: boolean;
  runtime_safe?: boolean;
  blocking_reason?: string;
  message?: string;
  results?: SelectorHealFieldResult[];
};

export type SelectorHealApplyResponse = {
  ok: boolean;
  platform?: string;
  field?: string;
  selector?: string;
  previous_selector?: string;
  field_risk_level?: string;
  apply_allowed?: boolean;
  runtime_safe?: boolean;
  blocking_reason?: string;
  message?: string;
};

export type SelectorPauseStateResponse = {
  ok: boolean;
  platform?: string;
  field?: string;
  runtime_safe?: boolean;
  blocking_reason?: string;
  message?: string;
  prompt?: string;
  verified_selector?: string;
  selector?: string;
  previous_selector?: string;
  saved?: boolean;
  save_error?: string;
  log_path?: string;
  submit_error?: string;
  summary?: {
    sample_count?: number;
    generating_sample_count?: number;
    saw_pause_state?: boolean;
    suggested_selector?: string;
    top_selectors?: Array<[string, number]>;
    top_path_prefixes?: Array<[string, number]>;
    first_generating_sample?: Record<string, unknown> | null;
    last_sample?: Record<string, unknown> | null;
  };
};

export type SelectorAgentSettingsSnapshot = {
  enabled: boolean;
  platform: string;
  model: string;
};

export type ArticleTableImportResult = {
  ok: boolean;
  message?: string;
  import_id?: string;
  file_name?: string;
  added_count?: number;
  updated_count?: number;
  duplicate_count?: number;
  skipped_count?: number;
  articles?: ArticleSnapshot[];
  details?: Record<string, unknown>;
};

export type KeywordImportResult = {
  ok: boolean;
  message?: string;
  file_name?: string;
  keywords?: string[];
  count?: number;
  details?: Record<string, unknown>;
};

export type PendingArticleImportBatch = {
  import_id: string;
  file_name?: string;
  message?: string;
  added_count?: number;
  updated_count?: number;
  duplicate_count?: number;
  skipped_count?: number;
  created_at?: string;
};

export type ArticleAccountSnapshot = {
  id: string;
  name: string;
  url: string;
  platform: string;
  platform_label?: string;
  enabled: boolean;
  last_crawled_at?: string;
  last_status?: string;
  last_message?: string;
  last_added_count?: number;
  last_fetched_count?: number;
  last_excluded_count?: number;
  last_excluded_links?: ExcludedArticleLinkSnapshot[];
};

export type ExcludedArticleLinkSnapshot = {
  url: string;
  title?: string;
  source?: string;
  excluded_at?: string;
  updated_at?: string;
  count?: number;
  account_id?: string;
  account_name?: string;
};

export type AccountCrawlingSnapshot = {
  enabled: boolean;
  frequency_minutes: number;
  rsshub_base_url: string;
  rsshub_base_urls?: string[];
  max_items_per_account: number;
  accounts: ArticleAccountSnapshot[];
  last_auto_run_at?: string;
  last_manual_run_at?: string;
  last_excluded_count?: number;
  last_excluded_links?: ExcludedArticleLinkSnapshot[];
  excluded_links?: ExcludedArticleLinkSnapshot[];
};

export type AccountCrawlResult = {
  ok: boolean;
  message?: string;
  fetched_count?: number;
  added_count?: number;
  duplicate_count?: number;
  excluded_count?: number;
  excluded_links?: ExcludedArticleLinkSnapshot[];
  results?: Array<Record<string, unknown>>;
};

export const ARTICLE_DATA_CHANGED_EVENT = "article-updated";
export const TASK_DATA_CHANGED_EVENT = "task-updated";

const BOOTSTRAP_CACHE_TTL_MS = 2500;
const TASKS_FULL_CACHE_TTL_MS = 5000;
const SETTINGS_CACHE_TTL_MS = 30000;

let bootstrapCache: { data: BootstrapPayload; updatedAt: number } | null = null;
let bootstrapInFlight: Promise<BootstrapPayload> | null = null;
let bootstrapCacheVersion = 0;
let tasksFullCache: { tasks: TaskFull[]; updatedAt: number } | null = null;
let tasksFullInFlight: Promise<TaskFull[]> | null = null;
let tasksFullCacheVersion = 0;
let settingsCache: { data: Record<string, unknown>; updatedAt: number } | null = null;
let settingsInFlight: Promise<Record<string, unknown>> | null = null;
let settingsCacheVersion = 0;
let settingsRequestSeq = 0;

export type TodoSnapshot = {
  id?: string | number;
  text: string;
  done: boolean;
  completedAt?: string;
};

type TodoCacheState = {
  todos: TodoSnapshot[];
  pending: boolean;
  updatedAt: string;
};

const TODO_CACHE_KEY = "ai-monitor.todo-cache.v1";
const SESSION_TOKEN_STORAGE_KEY = "surfaced.session-token.v1";
const SESSION_TOKEN_HEADER = "X-Surfaced-Session-Token";
const WEB_APP_NAME = "Surfaced";
const WEB_APP_SLUG = "surfaced";
const WEB_ASSISTANT_NAME = "Surfaced.Bot";

export type BootstrapPayload = {
  session?: {
    token: string;
    header: string;
  };
  version: VersionSnapshot;
  branding: {
    appName: string;
    brandName: string;
    subtitle: string;
  };
  sidebar: SidebarSnapshot;
  assistant: AssistantSnapshot;
  dashboard: DashboardSnapshot;
  platforms: Array<{
    id: string;
    name: string;
    enabled: boolean;
    model: string;
    userDataDir: string;
  }>;
  tasks: Array<{
    id: string;
    name: string;
    brand: string;
    mode: string;
    schedule: string;
    status: string;
    statusLabel?: string;
    platforms: string[];
  }>;
  todos: TodoSnapshot[];
  articles: ArticleSnapshot[];
  recentEvents: Array<{
    id: string;
    ts: string;
    category: string;
    message: string;
  }>;
  account_crawling: AccountCrawlingSnapshot;
  config?: {
    recognition?: {
      floating_window_resident_enabled?: boolean;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  pendingReviews: Array<Record<string, unknown>>;
  stats: {
    enabledTasks: number;
    totalTasks: number;
    todayRecords: number;
    hitRecords: number;
    errorRecords: number;
  };
  monitoring: MonitoringSnapshot;
  lastRun?: Record<string, unknown> | null;
  availableModels?: Record<string, string[]>;
  regionTags?: string[];
  industryTags?: string[];
};

export const FALLBACK_BOOTSTRAP: BootstrapPayload = {
  version: {
    appName: "Surfaced",
    slug: "surfaced",
    version: "2026.04.12",
    channel: "stable",
    label: "v2026.04.12",
    full: "Surfaced v2026.04.12",
  },
  branding: {
    appName: "Surfaced",
    brandName: "Surfaced",
    subtitle: "",
  },
  sidebar: {
    userName: "林见鹿",
    role: "系统运营",
    avatar: "",
    online: true,
  },
  assistant: {
    name: "Surfaced.Bot",
    level: "Lv. 42 / 状态良好",
    status: "监控运行中",
    platform: "doubao",
    model: "doubao-seed-2-0-pro-260215",
  },
  dashboard: {
    dateLabel: "22 MAR 2026",
    weekdayLabel: "日",
    greeting: "下午好",
    userName: "AI 运营",
    headline: "系统运行平稳，今日已为您自动拦截 12 项异常请求。",
    todayTaskCount: 42,
    completedCount: 38,
    runningCount: 4,
    todayIntercepted: 12,
    trend: {
      timeRange: "week",
      current: 0,
      avg: 0,
      peak: 0,
      delta: 0,
      data: [],
    },
    sourceBreakdown: [
      { name: "科技互联网", value: 45 },
      { name: "消费零售", value: 25 },
      { name: "金融医疗", value: 20 },
      { name: "汽车制造", value: 10 },
    ],
    taskCards: [
      { key: "capture", title: "抓取模式", desc: "快速提取核心数据", icon: "zap", active: false },
      { key: "ocr", title: "识别模式", desc: "OCR视觉解析", icon: "eye", active: false },
      { key: "api", title: "接口模式", desc: "API实时同步", icon: "code", active: false },
      { key: "smart", title: "智能模式", desc: "AI混合调度", icon: "cpu", active: true },
    ],
    mediaStats: [
      { name: "1日", auth: 0, self: 0 },
      { name: "2日", auth: 0, self: 0 },
      { name: "3日", auth: 0, self: 0 },
      { name: "4日", auth: 0, self: 0 },
      { name: "5日", auth: 0, self: 0 },
      { name: "6日", auth: 0, self: 0 },
    ],
  },
  platforms: [],
  tasks: [],
  todos: [
    { id: 1, text: "审查最新提交的 API 数据格式", done: true },
    { id: 2, text: "更新系统检测模块至 v2.4", done: true },
    { id: 3, text: "修复首页图表的高度 Bug", done: false },
    { id: 4, text: "同步设计规范到前端代码库", done: false },
  ],
  articles: [
    { id: 1, source: "TechCrunch", title: "2026年用户体验设计的十大趋势解析...", type: "media" },
    { id: 2, source: "少数派", title: "设计系统从0到1：如何建立可持续的...", type: "self-media" },
    { id: 3, source: "UX Collective", title: "The psychology behind great onbo...", type: "self-media" },
    { id: 4, source: "36氪", title: "AI 辅助设计工具的崛起与设计师的未...", type: "media" },
    { id: 5, source: "Medium", title: "Building minimalist interfaces tha...", type: "self-media" },
  ],
  account_crawling: {
    enabled: false,
    frequency_minutes: 60,
    rsshub_base_url: "https://rsshub.app",
    rsshub_base_urls: ["https://rsshub.app"],
    max_items_per_account: 20,
    accounts: [],
    last_auto_run_at: "",
    last_manual_run_at: "",
    last_excluded_count: 0,
    last_excluded_links: [],
    excluded_links: [],
  },
  recentEvents: [],
  pendingReviews: [],
  stats: {
    enabledTasks: 6,
    totalTasks: 8,
    todayRecords: 24,
    hitRecords: 18,
    errorRecords: 1,
  },
  monitoring: {
    enabled: false,
    running: false,
    statusMessage: "定时任务已关闭",
  },
  lastRun: null,
  availableModels: {},
  regionTags: [],
  industryTags: [],
};

function sanitizeTodoSnapshot(todo: TodoSnapshot): TodoSnapshot {
  return {
    id: todo.id,
    text: String(todo.text || "").trim(),
    done: Boolean(todo.done),
    completedAt: todo.completedAt ? String(todo.completedAt) : undefined,
  };
}

function isTodoSnapshotArray(value: unknown): value is TodoSnapshot[] {
  return Array.isArray(value) && value.every((item) => {
    if (!item || typeof item !== "object") return false;
    const todo = item as TodoSnapshot;
    return typeof todo.text === "string" && typeof todo.done === "boolean";
  });
}

function normalizeTodoForCompare(todo: TodoSnapshot): string {
  const safeTodo = sanitizeTodoSnapshot(todo);
  return JSON.stringify({
    id: safeTodo.id ?? "",
    text: safeTodo.text,
    done: safeTodo.done,
    completedAt: safeTodo.completedAt ?? "",
  });
}

export function areTodosEqual(left: TodoSnapshot[] | null | undefined, right: TodoSnapshot[] | null | undefined): boolean {
  const leftTodos = Array.isArray(left) ? left.map(normalizeTodoForCompare) : [];
  const rightTodos = Array.isArray(right) ? right.map(normalizeTodoForCompare) : [];
  if (leftTodos.length !== rightTodos.length) {
    return false;
  }
  return leftTodos.every((item, index) => item === rightTodos[index]);
}

function normalizeArticleForCompare(article: ArticleSnapshot): string {
  return JSON.stringify({
    id: article.id ?? "",
    source: String(article.source || "").trim(),
    title: String(article.title || "").trim(),
    type: article.type,
    url: String(article.url || "").trim(),
    platform: String(article.platform || "").trim(),
    media_name: String(article.media_name || "").trim(),
    ts: String(article.ts || "").trim(),
    published_at: String(article.published_at || "").trim(),
    fetch_method: String(article.fetch_method || "").trim(),
    matchedTasks: Array.isArray(article.matchedTasks) ? [...article.matchedTasks] : [],
    exportKeywords: Array.isArray(article.exportKeywords) ? [...article.exportKeywords] : [],
    reasonLines: Array.isArray(article.reasonLines) ? [...article.reasonLines] : [],
    classificationStatus: article.classificationStatus || "",
    classificationMessage: String(article.classificationMessage || "").trim(),
    unmatchedReason: String(article.unmatchedReason || "").trim(),
    referenced: Boolean(article.referenced),
    referencedTasks: Array.isArray(article.referencedTasks) ? [...article.referencedTasks] : [],
    lastReferencedAt: String(article.lastReferencedAt || "").trim(),
  });
}

export function areArticlesEqual(
  left: ArticleSnapshot[] | null | undefined,
  right: ArticleSnapshot[] | null | undefined,
): boolean {
  const leftArticles = Array.isArray(left) ? left.map(normalizeArticleForCompare) : [];
  const rightArticles = Array.isArray(right) ? right.map(normalizeArticleForCompare) : [];
  if (leftArticles.length !== rightArticles.length) {
    return false;
  }
  return leftArticles.every((item, index) => item === rightArticles[index]);
}

export function readTodoCache(): TodoCacheState | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(TODO_CACHE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<TodoCacheState>;
    if (!isTodoSnapshotArray(parsed.todos)) {
      return null;
    }
    return {
      todos: parsed.todos.map(sanitizeTodoSnapshot),
      pending: Boolean(parsed.pending),
      updatedAt: String(parsed.updatedAt || ""),
    };
  } catch {
    return null;
  }
}

export function writeTodoCache(todos: TodoSnapshot[], pending: boolean): void {
  if (typeof window === "undefined") {
    return;
  }
  const payload: TodoCacheState = {
    todos: todos.map(sanitizeTodoSnapshot),
    pending,
    updatedAt: new Date().toISOString(),
  };
  window.localStorage.setItem(TODO_CACHE_KEY, JSON.stringify(payload));
}

export async function fetchBootstrap(): Promise<BootstrapPayload> {
  const now = Date.now();
  if (bootstrapCache && now - bootstrapCache.updatedAt < BOOTSTRAP_CACHE_TTL_MS) {
    return bootstrapCache.data;
  }
  if (bootstrapInFlight) {
    return bootstrapInFlight;
  }

  const requestVersion = bootstrapCacheVersion;
  bootstrapInFlight = (async () => {
    try {
      const hasSessionToken = Boolean(readSessionToken());
      const fetcher = hasSessionToken ? apiFetch : window.fetch;
      const response = await fetcher("/api/bootstrap", {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) {
        return FALLBACK_BOOTSTRAP;
      }
      const data = (await response.json()) as Partial<BootstrapPayload>;
      storeSessionToken(data.session);
      const merged = mergeBootstrap(data);
      if (requestVersion === bootstrapCacheVersion) {
        bootstrapCache = { data: merged, updatedAt: Date.now() };
      }
      return merged;
    } catch {
      return FALLBACK_BOOTSTRAP;
    } finally {
      bootstrapInFlight = null;
    }
  })();

  return bootstrapInFlight;
}

export function invalidateBootstrapCache() {
  bootstrapCache = null;
  bootstrapInFlight = null;
  bootstrapCacheVersion += 1;
}

export function warmBootstrapCache() {
  void fetchBootstrap();
}

export function readTasksFullCache(): TaskFull[] | null {
  return tasksFullCache?.tasks ?? null;
}

export function invalidateTasksFullCache() {
  tasksFullCache = null;
  tasksFullInFlight = null;
  tasksFullCacheVersion += 1;
}

export function warmTasksFullCache() {
  void fetchTasksFull();
}

function storeSessionToken(session: BootstrapPayload["session"] | undefined) {
  if (typeof window === "undefined" || !session?.token) {
    return;
  }
  window.sessionStorage.setItem(SESSION_TOKEN_STORAGE_KEY, session.token);
}

function readSessionToken() {
  if (typeof window === "undefined") {
    return "";
  }
  return window.sessionStorage.getItem(SESSION_TOKEN_STORAGE_KEY) || "";
}

function shouldAttachSessionToken(input: RequestInfo | URL) {
  const value = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (value.startsWith("/api/")) {
    return true;
  }
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.pathname.startsWith("/api/");
  } catch {
    return false;
  }
}

async function ensureSessionToken() {
  const existing = readSessionToken();
  if (existing) {
    return existing;
  }
  try {
    await fetchBootstrap();
    return readSessionToken();
  } catch {
    return "";
  }
}

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  if (!shouldAttachSessionToken(input)) {
    return window.fetch(input, init);
  }
  const token = await ensureSessionToken();
  if (!token) {
    return window.fetch(input, init);
  }
  const headers = new Headers(init.headers || {});
  headers.set(SESSION_TOKEN_HEADER, token);
  return window.fetch(input, { ...init, headers });
}

export async function triggerRunAll(): Promise<{ queued: boolean; message: string }> {
  try {
    const response = await apiFetch("/api/actions/run-all", { method: "POST" });
    if (!response.ok) {
      return { queued: false, message: "触发失败" };
    }
    return (await response.json()) as { queued: boolean; message: string };
  } catch {
    return { queued: false, message: "触发失败" };
  }
}

export async function setMonitoringEnabled(enabled: boolean): Promise<{ ok: boolean; enabled: boolean; message: string }> {
  try {
    const response = await apiFetch("/api/actions/monitoring", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!response.ok) {
      return { ok: false, enabled, message: "切换失败" };
    }
    return (await response.json()) as { ok: boolean; enabled: boolean; message: string };
  } catch {
    return { ok: false, enabled, message: "切换失败" };
  }
}

export function mergeBootstrap(data: Partial<BootstrapPayload> | null | undefined): BootstrapPayload {
  if (!data) {
    return FALLBACK_BOOTSTRAP;
  }
  const merged = {
    ...FALLBACK_BOOTSTRAP,
    ...data,
    version: {
      ...FALLBACK_BOOTSTRAP.version,
      ...(data.version || {}),
    },
    branding: {
      ...FALLBACK_BOOTSTRAP.branding,
      ...(data.branding || {}),
    },
    sidebar: {
      ...FALLBACK_BOOTSTRAP.sidebar,
      ...(data.sidebar || {}),
    },
    assistant: {
      ...FALLBACK_BOOTSTRAP.assistant,
      ...(data.assistant || {}),
    },
    dashboard: {
      ...FALLBACK_BOOTSTRAP.dashboard,
      ...(data.dashboard || {}),
      trend: {
        ...FALLBACK_BOOTSTRAP.dashboard.trend,
        ...((data.dashboard || {}).trend || {}),
        data: ((data.dashboard || {}).trend?.data as TrendPoint[] | undefined) || FALLBACK_BOOTSTRAP.dashboard.trend.data,
      },
      sourceBreakdown: ((data.dashboard || {}).sourceBreakdown as DashboardSnapshot["sourceBreakdown"] | undefined) || FALLBACK_BOOTSTRAP.dashboard.sourceBreakdown,
      taskCards: ((data.dashboard || {}).taskCards as ModeCard[] | undefined) || FALLBACK_BOOTSTRAP.dashboard.taskCards,
      mediaStats: ((data.dashboard || {}).mediaStats as DashboardSnapshot["mediaStats"] | undefined) || FALLBACK_BOOTSTRAP.dashboard.mediaStats,
    },
    platforms: data.platforms || FALLBACK_BOOTSTRAP.platforms,
    tasks: data.tasks || FALLBACK_BOOTSTRAP.tasks,
    todos: data.todos ?? FALLBACK_BOOTSTRAP.todos,
    articles: data.articles ?? FALLBACK_BOOTSTRAP.articles,
    account_crawling: {
      ...FALLBACK_BOOTSTRAP.account_crawling,
      ...(data.account_crawling || {}),
      accounts: data.account_crawling?.accounts || FALLBACK_BOOTSTRAP.account_crawling.accounts || [],
      last_excluded_links: data.account_crawling?.last_excluded_links || [],
      excluded_links: data.account_crawling?.excluded_links || [],
    },
    recentEvents: data.recentEvents || FALLBACK_BOOTSTRAP.recentEvents,
    pendingReviews: data.pendingReviews || FALLBACK_BOOTSTRAP.pendingReviews,
    stats: {
      ...FALLBACK_BOOTSTRAP.stats,
      ...(data.stats || {}),
    },
    monitoring: {
      ...FALLBACK_BOOTSTRAP.monitoring,
      ...(data.monitoring || {}),
    },
    lastRun: data.lastRun ?? FALLBACK_BOOTSTRAP.lastRun,
    availableModels: data.availableModels || FALLBACK_BOOTSTRAP.availableModels,
    regionTags: data.regionTags || FALLBACK_BOOTSTRAP.regionTags,
    industryTags: data.industryTags || FALLBACK_BOOTSTRAP.industryTags,
  };

  // Keep the web branding stable even if an older backend process is still responding.
  merged.version.appName = WEB_APP_NAME;
  merged.version.slug = WEB_APP_SLUG;
  merged.version.full = `${WEB_APP_NAME} ${merged.version.label}`;
  merged.branding.appName = WEB_APP_NAME;
  merged.branding.brandName = WEB_APP_NAME;
  merged.branding.subtitle = "";
  if (!merged.assistant.name || merged.assistant.name.includes("Saffron")) {
    merged.assistant.name = WEB_ASSISTANT_NAME;
  }

  return merged;
}

// ── Platform API Key Management ─────────────────────────────

export type PlatformKeyInfo = {
  api_key_masked: string;
  has_key: boolean;
  has_access?: boolean;
  requires_api_key?: boolean;
  api_model: string;
  api_fast_model?: string;
  api_deep_model?: string;
  model_options: string[];
  api_test_status?: "idle" | "success" | "error";
  default_model: string;
  supports_split_models?: boolean;
  fast_model_default?: string;
  deep_model_default?: string;
  split_model_note?: string;
};

export type LocalModelStatus = {
  provider: string;
  base_url: string;
  healthy: boolean;
  status: string;
  status_message: string;
  last_error: string;
  binary_path: string;
  binary_source?: string;
  bundled_binary_path?: string;
  bundled_binary_available?: boolean;
  runtime_binary_path?: string;
  runtime_binary_prepared?: boolean;
  service_process_running: boolean;
  pull_process_running: boolean;
  pulling_model: string;
  pulling_model_ready?: boolean | null;
  models: string[];
  runtime_models_path?: string;
  bundled_models_path?: string;
  bundled_models_available?: boolean;
  bundled_models_seeded?: boolean;
};

export type BrowserAuthProfileSnapshot = {
  id: string;
  label: string;
  relative_path: string;
  absolute_path: string;
  created_at: string;
  updated_at: string;
  last_used_at: string;
  kind: string;
  has_files: boolean;
  authenticated?: boolean;
  auth_state?: string;
  is_active: boolean;
};

export type BrowserAuthDebugSnapshot = {
  profile_path: string;
  tracked_open: boolean;
  external_open: boolean;
  raw_profile_in_use: boolean;
  owner_pids: number[];
  cookies_path: string;
  cookies_exists: boolean;
  cookies_updated_at: string;
  login_data_path: string;
  login_data_exists: boolean;
  login_data_updated_at: string;
  diagnosis: string;
  auth_state: string;
  authenticated: boolean;
  session_external_in_use?: boolean;
};

export type BrowserAuthPlatformSnapshot = {
  platform: string;
  active_profile_id: string;
  active_profile?: BrowserAuthProfileSnapshot | null;
  profiles: BrowserAuthProfileSnapshot[];
  profile_busy?: boolean;
  login_window_open?: boolean;
  login_opened_at?: string;
  debug?: BrowserAuthDebugSnapshot;
};

export async function fetchPlatformKeys(): Promise<Record<string, PlatformKeyInfo>> {
  try {
    const res = await apiFetch("/api/platforms/keys");
    if (!res.ok) return {};
    const data = await res.json();
    return data.platforms || {};
  } catch {
    return {};
  }
}

export async function savePlatformConfig(platforms: Record<string, { api_key?: string; api_model?: string; api_fast_model?: string; api_deep_model?: string; model_options?: string[] }>): Promise<{ ok: boolean }> {
  try {
    const res = await apiFetch("/api/platforms/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platforms }),
    });
    const data = await res.json();
    if (data?.ok) {
      invalidateSettingsCache();
      void fetchSettings({ force: true });
    }
    return data;
  } catch {
    return { ok: false };
  }
}

export async function testPlatformConnection(platformId: string, apiKey: string, model: string): Promise<{ ok: boolean; message: string; latency_ms?: number }> {
  try {
    const res = await apiFetch(`/api/platforms/${platformId}/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey, model }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

// ── Task CRUD ───────────────────────────────────────────────

export type TaskFull = {
  id: string;
  name: string;
  brand: string;
  enabled: boolean;
  mode: string;
  schedule: string;
  status: string;
  status_label?: string;
  brand_status?: string;
  sent_today?: boolean;
  sent_at?: string;
  scheduled_today?: boolean;
  formal_started?: boolean;
  formal_running?: boolean;
  has_gap?: boolean;
  gap_reasons?: string[];
  failed_today: boolean;
  failed_modes_today: string[];
  failed_updated_at: string;
  failure_kind_today: string;
  status_message: string;
  completed_keywords_today?: string[];
  detected_platforms_today?: string[];
  actual_screenshot_count_today?: number;
  fixed_screenshot_target_today?: number;
  completed_by_quota_today?: boolean;
  test_failure_notice?: {
    message: string;
    updatedAt: string;
    expiresAt: string;
  } | null;
  platforms: string[];
  keywords: Array<{
    keyword: string;
    brand: string;
    platforms: string[];
    mode: string;
    deep_think?: Record<string, boolean>;
  }>;
  webhook_url: string;
  weekdays: number[];
  industry_tags: string[];
  region_tags: string[];
  inspect: boolean;
  recognition_enabled: boolean;
  recognition_brands: string;
  recognition_batch_size: number;
  extract_references_enabled: boolean;
  fixed_screenshot_enabled: boolean;
  fixed_screenshot_count: number;
  optimization_start_date: string;
  optimization_end_date: string;
  created_at?: string;
  total_records: number;
  success_records: number;
  success_rate: number;
  article_count: number;
  optimization_trend?: Array<{
    value: number;
  }>;
};

export type BrandDraftKeyword = {
  keyword: string;
  brand: string;
  platforms: string[];
  mode: "browser" | "recognition" | "api" | "smart";
  deep_think_platforms: string[];
};

export type BrandTaskDraft = {
  name: string;
  brand: string;
  recognition_brands: string[];
  industry_tags: string[];
  region_tags: string[];
  webhook_url: string;
  optimization_start_date: string;
  optimization_end_date: string;
  weekdays: number[];
  enabled: boolean;
  inspect: boolean;
  recognition_enabled: boolean;
  recognition_batch_size: number;
  extract_references_enabled: boolean;
  fixed_screenshot_enabled: boolean;
  fixed_screenshot_count: number;
  platforms: string[];
  deep_think_platforms: string[];
  keywords: BrandDraftKeyword[];
  notes: string[];
  missing_info: string[];
};

export type QuickTodoDraftItem = {
  text: string;
  done: boolean;
};

export async function fetchTasksFull(options: { force?: boolean } = {}): Promise<TaskFull[]> {
  const now = Date.now();
  if (!options.force && tasksFullCache && now - tasksFullCache.updatedAt < TASKS_FULL_CACHE_TTL_MS) {
    return tasksFullCache.tasks;
  }
  if (tasksFullInFlight) {
    return tasksFullInFlight;
  }

  const requestVersion = tasksFullCacheVersion;
  tasksFullInFlight = (async () => {
    try {
      const res = await apiFetch("/api/tasks/full");
      if (!res.ok) return tasksFullCache?.tasks ?? [];
      const data = await res.json();
      const tasks = Array.isArray(data.tasks) ? data.tasks : [];
      if (requestVersion === tasksFullCacheVersion) {
        tasksFullCache = { tasks, updatedAt: Date.now() };
      }
      return tasks;
    } catch {
      return tasksFullCache?.tasks ?? [];
    } finally {
      tasksFullInFlight = null;
    }
  })();

  return tasksFullInFlight;
}

async function mutateTasksFullCache<T>(operation: () => Promise<T>): Promise<T> {
  try {
    const result = await operation();
    invalidateTasksFullCache();
    invalidateBootstrapCache();
    return result;
  } catch (error) {
    invalidateTasksFullCache();
    throw error;
  }
}

export async function createTask(task: Record<string, unknown>): Promise<{ ok: boolean; task_id?: string; message?: string }> {
  try {
    return await mutateTasksFullCache(async () => {
      const res = await apiFetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(task),
      });
      return await res.json();
    });
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function generateBrandTaskDraft(instruction: string): Promise<{ ok: boolean; draft?: BrandTaskDraft; message?: string; raw_reply?: string }> {
  try {
    const res = await apiFetch("/api/brand-draft/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function generateQuickTodosDraft(instruction: string): Promise<{ ok: boolean; todos?: QuickTodoDraftItem[]; message?: string; raw_reply?: string }> {
  try {
    const res = await apiFetch("/api/todo-draft/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function updateTask(taskId: string, task: Record<string, unknown>): Promise<{ ok: boolean; message?: string }> {
  try {
    return await mutateTasksFullCache(async () => {
      const res = await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(task),
      });
      return await res.json();
    });
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function deleteTask(taskId: string): Promise<{ ok: boolean; message?: string }> {
  try {
    return await mutateTasksFullCache(async () => {
      const res = await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
      return await res.json();
    });
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function forceSendSuccessfulTaskResults(taskId: string): Promise<{ ok: boolean; message?: string; actualScreenshotCount?: number }> {
  try {
    return await mutateTasksFullCache(async () => {
      const res = await apiFetch(`/api/tasks/${taskId}/force-send-success`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      return await res.json();
    });
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function testRunTask(taskId: string): Promise<{ ok: boolean; queryCount?: number; report?: string; message?: string }> {
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/test-run`, { method: "POST" });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export type TestRunStartResponse = {
  ok: boolean;
  runId?: string;
  recognitionTest?: boolean;
  message?: string;
  taskId?: string;
  taskName?: string;
};

export type TestRunFailureDetail = {
  keyword?: string;
  platform?: string;
  brand?: string;
  mode?: string;
  errorMessage?: string;
  failureType?: string;
};

export type TestRunStatus = {
  ok: boolean;
  runId?: string;
  taskId?: string;
  taskName?: string;
  status?: "queued" | "running" | "success" | "failed" | "cancelled";
  currentQuery?: number;
  completedQueries?: number;
  totalQueries?: number;
  hitQueries?: number;
  currentAttempt?: number;
  totalAttempts?: number;
  currentKeyword?: string;
  currentPlatform?: string;
  message?: string;
  result?: "success" | "failed" | "cancelled" | "";
  startedAt?: string;
  updatedAt?: string;
  finishedAt?: string;
  errorMessage?: string;
  failureDetails?: TestRunFailureDetail[];
  cancelRequested?: boolean;
};

export type TestRunCancelResponse = {
  ok: boolean;
  runId?: string;
  status?: "queued" | "running" | "success" | "failed" | "cancelled";
  message?: string;
  errorMessage?: string;
};

export async function startTestRunTask(taskId: string): Promise<TestRunStartResponse> {
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/test-run/start`, { method: "POST" });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function fetchTestRunStatus(runId: string): Promise<TestRunStatus> {
  try {
    const res = await apiFetch(`/api/test-runs/${runId}`);
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function cancelTestRunTask(runId: string): Promise<TestRunCancelResponse> {
  try {
    const res = await apiFetch(`/api/test-runs/${runId}/cancel`, { method: "POST" });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

// ── AI Chat ─────────────────────────────────────────────────

export type ChatMessage = { role: string; content: string };

export type AssistantTool = {
  name: string;
  description: string;
  params?: Record<string, string>;
  schema?: Record<string, unknown>;
};

export type AssistantActionResponse = {
  ok: boolean;
  action?: string;
  message?: string;
  result?: Record<string, unknown>;
  snapshot?: BootstrapPayload;
};

export type SearchUploadedFile = {
  id: string;
  name: string;
  path: string;
  size: number;
  uploaded_at?: string;
};

export type SearchBrandRankResponse = {
  ok: boolean;
  message?: string;
  brand?: string;
  input?: string;
  output?: string;
  file_name?: string;
  download_url?: string;
  summary?: {
    processed_rows?: number;
    rank_counts?: Record<string, number>;
    review_count?: number;
    sheet?: string;
    samples?: Array<Record<string, unknown>>;
  };
};

type ChatStreamHandlers = {
  onDelta?: (delta: string, fullReply: string) => void;
  onThinkingDelta?: (delta: string, fullThinking: string) => void;
  signal?: AbortSignal;
};

export async function sendChatMessage(
  platform: string,
  model: string,
  messages: ChatMessage[],
  deepThink?: boolean,
): Promise<{ ok: boolean; reply?: string; message?: string }> {
  try {
    const res = await apiFetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, model, messages, deep_think: deepThink }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function sendChatMessageStream(
  platform: string,
  model: string,
  messages: ChatMessage[],
  handlers?: ChatStreamHandlers,
  deepThink?: boolean,
): Promise<{ ok: boolean; reply: string; thinking: string; message?: string; aborted?: boolean }> {
  let reply = "";
  let thinking = "";
  try {
    const res = await apiFetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, model, messages, deep_think: deepThink }),
      signal: handlers?.signal,
    });

    if (!res.ok || !res.body) {
      const fallback = await sendChatMessage(platform, model, messages, deepThink);
      return {
        ok: fallback.ok,
        reply: fallback.reply || "",
        thinking: "",
        message: fallback.message,
      };
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) {
          continue;
        }
        let event: { type?: string; delta?: string; message?: string } | null = null;
        try {
          event = JSON.parse(trimmed);
        } catch {
          event = null;
        }
        if (!event) {
          continue;
        }
        if (event.type === "thinking_delta") {
          const delta = event.delta || "";
          thinking += delta;
          handlers?.onThinkingDelta?.(delta, thinking);
          continue;
        }
        if (event.type === "delta") {
          const delta = event.delta || "";
          reply += delta;
          handlers?.onDelta?.(delta, reply);
          continue;
        }
        if (event.type === "error") {
          return { ok: false, reply, thinking, message: event.message || "请求失败" };
        }
        if (event.type === "done") {
          return { ok: true, reply, thinking };
        }
      }
    }

    const tail = buffer.trim();
    if (tail) {
      try {
        const event = JSON.parse(tail) as { type?: string; delta?: string; message?: string };
        if (event.type === "thinking_delta") {
          const delta = event.delta || "";
          thinking += delta;
          handlers?.onThinkingDelta?.(delta, thinking);
        } else if (event.type === "delta") {
          const delta = event.delta || "";
          reply += delta;
          handlers?.onDelta?.(delta, reply);
        } else if (event.type === "error") {
          return { ok: false, reply, thinking, message: event.message || "请求失败" };
        }
      } catch {
        // ignore invalid tail
      }
    }

    return { ok: true, reply, thinking };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      return { ok: false, reply, thinking, message: "已停止生成", aborted: true };
    }
    return { ok: false, reply, thinking, message: "网络错误" };
  }
}

export async function fetchAssistantTools(): Promise<AssistantTool[]> {
  try {
    const res = await apiFetch("/api/assistant/tools");
    const data = await res.json();
    return Array.isArray(data.tools) ? data.tools : [];
  } catch {
    return [];
  }
}

export async function runAssistantAction(action: string, params: Record<string, unknown>): Promise<AssistantActionResponse> {
  try {
    const res = await apiFetch("/api/assistant/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, params }),
    });
    return await res.json();
  } catch {
    return { ok: false, action, message: "网络错误" };
  }
}

export async function uploadSearchFile(file: File): Promise<{ ok: boolean; file?: SearchUploadedFile; message?: string }> {
  try {
    const res = await apiFetch(`/api/search/upload-file?name=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    return await res.json();
  } catch {
    return { ok: false, message: "上传失败" };
  }
}

export async function runSearchBrandRank(fileId: string, brand: string): Promise<SearchBrandRankResponse> {
  try {
    const res = await apiFetch("/api/search/brand-rank", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_id: fileId, brand }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "品牌排名处理失败" };
  }
}

// ── Settings ────────────────────────────────────────────────

export function readSettingsCache(): Record<string, unknown> | null {
  return settingsCache?.data ?? null;
}

export function invalidateSettingsCache() {
  settingsCache = null;
  settingsInFlight = null;
  settingsCacheVersion += 1;
}

export function warmSettingsCache() {
  void fetchSettings();
}

export async function fetchSettings(options: { force?: boolean } = {}): Promise<Record<string, unknown>> {
  const now = Date.now();
  if (!options.force && settingsCache && now - settingsCache.updatedAt < SETTINGS_CACHE_TTL_MS) {
    return settingsCache.data;
  }
  if (!options.force && settingsInFlight) {
    return settingsInFlight;
  }

  const requestVersion = settingsCacheVersion;
  const requestSeq = ++settingsRequestSeq;
  const request = (async () => {
    try {
      const res = await apiFetch("/api/settings", {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!res.ok) return settingsCache?.data ?? {};
      const data = await res.json();
      if (requestVersion === settingsCacheVersion) {
        settingsCache = { data, updatedAt: Date.now() };
      }
      return data;
    } catch {
      return settingsCache?.data ?? {};
    } finally {
      if (settingsRequestSeq === requestSeq) {
        settingsInFlight = null;
      }
    }
  })();

  settingsInFlight = request;
  return request;
}

export async function saveSettings(settings: Record<string, unknown>): Promise<{ ok: boolean }> {
  try {
    const res = await apiFetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
    const data = await res.json();
    if (data?.ok) {
      invalidateSettingsCache();
      void fetchSettings({ force: true });
    }
    return data;
  } catch {
    return { ok: false };
  }
}

export async function fetchBrowserAuth(): Promise<{ platforms: Record<string, BrowserAuthPlatformSnapshot> }> {
  try {
    const res = await apiFetch("/api/browser-auth", {
      cache: "no-store",
    });
    if (!res.ok) {
      return { platforms: {} };
    }
    return await res.json();
  } catch {
    return { platforms: {} };
  }
}

export async function browserAuthAction(
  action: string,
  platform: string,
  profileId?: string,
): Promise<{ ok: boolean; message?: string; browser_auth?: { platforms: Record<string, BrowserAuthPlatformSnapshot> } }> {
  try {
    const res = await apiFetch("/api/browser-auth/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action,
        platform,
        profile_id: profileId || "",
      }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function diagnoseSelectorHeal(payload: {
  platform: string;
  fields: string[];
  verify?: boolean;
  auto_apply?: boolean;
}): Promise<SelectorHealResponse> {
  try {
    const res = await apiFetch("/api/selector-heal/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误", results: [] };
  }
}

export async function applySelectorHeal(payload: {
  platform: string;
  field: string;
  candidate_selector: string;
}): Promise<SelectorHealApplyResponse> {
  try {
    const res = await apiFetch("/api/selector-heal/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data?.ok) {
      invalidateSettingsCache();
      void fetchSettings({ force: true });
    }
    return data;
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function diagnoseSelectorPauseState(payload: {
  platform: string;
}): Promise<SelectorPauseStateResponse> {
  try {
    const res = await apiFetch("/api/selector-heal/pause-state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function fetchLocalModelStatus(): Promise<{ ok: boolean; local_model?: LocalModelStatus; message?: string }> {
  try {
    const res = await apiFetch("/api/local-model/status", {
      cache: "no-store",
    });
    if (!res.ok) {
      return { ok: false, message: "获取本地模型状态失败" };
    }
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function prepareLocalModel(model?: string): Promise<{ ok: boolean; queued?: boolean; message?: string; local_model?: LocalModelStatus }> {
  try {
    const res = await apiFetch("/api/local-model/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(model ? { model } : {}),
    });
    if (!res.ok) {
      return { ok: false, message: "启动本地模型准备失败" };
    }
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function testLocalModel(model?: string): Promise<{ ok: boolean; message?: string; model?: string; reply?: string; local_model?: LocalModelStatus }> {
  try {
    const res = await apiFetch("/api/local-model/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(model ? { model } : {}),
    });
    if (!res.ok) {
      return { ok: false, message: "本地模型测试失败" };
    }
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function refreshContextSnapshots(force = true): Promise<Record<string, unknown>> {
  try {
    const res = await apiFetch("/api/actions/refresh-context-snapshots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force }),
    });
    if (!res.ok) {
      return { ok: false, message: "刷新失败" };
    }
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function testSchedulerNotificationWebhook(webhookUrl: string): Promise<{ ok: boolean; message: string }> {
  try {
    const res = await apiFetch("/api/actions/test-scheduler-notification-webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ webhook_url: webhookUrl }),
    });
    const data = await res.json();
    if (!data.ok && String(data.message || "").trim() === "not found") {
      return { ok: false, message: "当前 Web 后端还没重启到最新版本，请重启 Web UI / 后端后再试。" };
    }
    return data;
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function checkAppUpdate(appUpdate?: Partial<AppUpdateSettingsSnapshot>): Promise<AppUpdateStatusSnapshot> {
  try {
    const res = await apiFetch("/api/actions/check-update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(appUpdate ? { app_update: appUpdate } : {}),
    });
    if (!res.ok) {
      return {
        ok: false,
        configured: false,
        checked: true,
        update_available: false,
        message: "检查更新失败",
        current: FALLBACK_BOOTSTRAP.version,
        settings: {
          channel: "stable",
          manifest_url: "",
          download_page_url: "",
          auto_check_enabled: false,
        },
      };
    }
    return await res.json();
  } catch {
    return {
      ok: false,
      configured: false,
      checked: true,
      update_available: false,
      message: "网络错误",
      current: FALLBACK_BOOTSTRAP.version,
      settings: {
        channel: "stable",
        manifest_url: "",
        download_page_url: "",
        auto_check_enabled: false,
      },
    };
  }
}

export async function fetchLocalUpdatePlan(sourceDir: string): Promise<{ ok: boolean; message?: string; plan?: Record<string, unknown> }> {
  try {
    const target = `/api/update/local-plan?source_dir=${encodeURIComponent(sourceDir)}`;
    const res = await apiFetch(target, {
      headers: { Accept: "application/json" },
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function prepareAppUpdate(appUpdate?: Partial<AppUpdateSettingsSnapshot>): Promise<{
  ok: boolean;
  message: string;
  status?: AppUpdateStatusSnapshot;
  source_dir?: string;
  plan?: Record<string, unknown>;
  download?: Record<string, unknown>;
}> {
  try {
    const res = await apiFetch("/api/actions/prepare-update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(appUpdate ? { app_update: appUpdate } : {}),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function startLocalUpdate(payload: {
  source_dir: string;
  cleanup_source?: boolean;
  restart_after_update?: boolean;
  exit_after_launch?: boolean;
}): Promise<{ ok: boolean; message: string; pid?: number; exitScheduled?: boolean }> {
  try {
    const res = await apiFetch("/api/actions/start-local-update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function pickDirectory(title = "选择目录"): Promise<{ ok: boolean; path?: string; message: string }> {
  try {
    const res = await apiFetch("/api/actions/pick-directory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

// ── Todo ────────────────────────────────────────────────────

export async function syncTodos(todos: TodoSnapshot[]): Promise<{ ok: boolean; todos?: TodoSnapshot[]; message?: string }> {
  try {
    const res = await apiFetch("/api/todos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ todos }),
    });
    if (!res.ok) {
      try {
        const data = await res.json();
        return {
          ok: false,
          todos: Array.isArray(data.todos) ? data.todos : undefined,
          message: data.message || "待办保存失败",
        };
      } catch {
        return { ok: false, message: "待办保存失败" };
      }
    }
    return await res.json();
  } catch {
    return { ok: false, message: "待办保存失败" };
  }
}

export async function fetchTodos(): Promise<TodoSnapshot[] | null> {
  try {
    const res = await apiFetch("/api/todos");
    if (!res.ok) return null;
    const data = await res.json();
    return data.todos || [];
  } catch {
    return null;
  }
}

// ── Articles ────────────────────────────────────────────────

export async function fetchArticles(params?: { type?: string; limit?: number; task_name?: string; includeExportKeywords?: boolean }): Promise<{ articles: ArticleSnapshot[]; total: number; today_total: number }> {
  try {
    const qs = new URLSearchParams();
    if (params?.type) qs.set("type", params.type);
    if (params?.limit) qs.set("limit", String(params.limit));
    if (params?.task_name) qs.set("task_name", params.task_name);
    if (params?.includeExportKeywords) qs.set("include_export_keywords", "1");
    const url = "/api/articles" + (qs.toString() ? "?" + qs.toString() : "");
    const res = await apiFetch(url, { cache: "no-store" });
    if (!res.ok) return { articles: [], total: 0, today_total: 0 };
    return await res.json();
  } catch {
    return { articles: [], total: 0, today_total: 0 };
  }
}

export async function fetchTaskMonthlyStats(taskId: string): Promise<{ months: { name: string; value: number; auth?: number; self?: number }[] }> {
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/monthly-stats`);
    if (!res.ok) return { months: [] };
    return await res.json();
  } catch {
    return { months: [] };
  }
}

export async function fetchTaskArticleReferenceRanking(
  taskId: string,
  params?: { platform?: string; date_from?: string; date_to?: string },
): Promise<ArticleReferenceRankingResponse> {
  try {
    const qs = new URLSearchParams();
    if (params?.platform) qs.set("platform", params.platform);
    if (params?.date_from) qs.set("date_from", params.date_from);
    if (params?.date_to) qs.set("date_to", params.date_to);
    const url = `/api/tasks/${taskId}/article-reference-ranking${qs.toString() ? `?${qs.toString()}` : ""}`;
    const res = await apiFetch(url, { cache: "no-store" });
    if (!res.ok) {
      return {
        ok: false,
        algorithm_version: "article_ref_weight_v1",
        computed_at: "",
        data_coverage: {
          body_references_since: "",
          answer_text_fallback: true,
        },
        available_platforms: [],
        daily_points: [],
        items: [],
        total: 0,
        message: "获取引用排名失败",
      };
    }
    return await res.json();
  } catch {
    return {
      ok: false,
      algorithm_version: "article_ref_weight_v1",
      computed_at: "",
      data_coverage: {
        body_references_since: "",
        answer_text_fallback: true,
      },
      available_platforms: [],
      daily_points: [],
      items: [],
      total: 0,
      message: "网络错误",
    };
  }
}

export async function exportTaskArticlesToWecom(
  taskId: string,
  params?: { type?: string; start?: string; end?: string },
): Promise<{ ok: boolean; message?: string; fileName?: string; count?: number }> {
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/articles/export-wecom`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params || {}),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function fetchTaskTrend(taskId: string, range: "week" | "month" | "year"): Promise<TrendSnapshot> {
  try {
    const res = await apiFetch(`/api/tasks/${taskId}/trend?range=${range}`);
    if (!res.ok) return FALLBACK_BOOTSTRAP.dashboard.trend;
    return (await res.json()) as TrendSnapshot;
  } catch {
    return FALLBACK_BOOTSTRAP.dashboard.trend;
  }
}

export async function fetchDashboardTrend(range: "week" | "month" | "year"): Promise<TrendSnapshot> {
  try {
    const res = await apiFetch(`/api/dashboard/trend?range=${range}`);
    if (!res.ok) return FALLBACK_BOOTSTRAP.dashboard.trend;
    return (await res.json()) as TrendSnapshot;
  } catch {
    return FALLBACK_BOOTSTRAP.dashboard.trend;
  }
}

export async function importArticle(url: string): Promise<{ ok: boolean; article?: ArticleSnapshot; message?: string; duplicate?: boolean }> {
  try {
    const res = await apiFetch("/api/articles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function importArticlesFromFile(file: File): Promise<ArticleTableImportResult> {
  try {
    const url = `/api/articles/import-file?name=${encodeURIComponent(file.name || "articles.xlsx")}`;
    const res = await apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function importKeywordsFromFile(file: File): Promise<KeywordImportResult> {
  try {
    const url = `/api/keywords/import-file?name=${encodeURIComponent(file.name || "keywords.xlsx")}`;
    const res = await apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function fetchPendingArticleTableImports(): Promise<{ ok: boolean; batches: PendingArticleImportBatch[]; message?: string }> {
  try {
    const res = await apiFetch("/api/articles/import-batches", { cache: "no-store" });
    if (!res.ok) return { ok: false, batches: [] };
    return await res.json();
  } catch {
    return { ok: false, batches: [], message: "网络错误" };
  }
}

export async function confirmArticleTableImport(importId: string): Promise<{ ok: boolean; message?: string }> {
  try {
    const res = await apiFetch(`/api/articles/import-batches/${encodeURIComponent(importId)}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function undoArticleTableImport(importId: string): Promise<{ ok: boolean; message?: string; removed_count?: number }> {
  try {
    const res = await apiFetch(`/api/articles/import-batches/${encodeURIComponent(importId)}/undo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function runAccountArticleCrawl(accountIds?: string[]): Promise<AccountCrawlResult> {
  try {
    const res = await apiFetch("/api/account-crawling/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(accountIds?.length ? { account_ids: accountIds } : {}),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误", fetched_count: 0, added_count: 0, duplicate_count: 0 };
  }
}

export async function restoreAccountCrawlExclusions(
  urls: string[],
): Promise<{ ok: boolean; message?: string; removed_count?: number; urls?: string[]; total?: number; excluded_links?: ExcludedArticleLinkSnapshot[] }> {
  try {
    const res = await apiFetch("/api/account-crawling/exclusions/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误", removed_count: 0, urls: [] };
  }
}

export async function fetchAccountCrawlExclusions(): Promise<{ ok: boolean; total: number; excluded_links: ExcludedArticleLinkSnapshot[]; message?: string }> {
  try {
    const res = await apiFetch("/api/account-crawling/exclusions", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!res.ok) {
      return { ok: false, total: 0, excluded_links: [], message: "获取排除项失败" };
    }
    const data = await res.json();
    return {
      ok: Boolean(data.ok),
      total: Number(data.total || 0),
      excluded_links: Array.isArray(data.excluded_links) ? data.excluded_links : [],
      message: data.message,
    };
  } catch {
    return { ok: false, total: 0, excluded_links: [], message: "网络错误" };
  }
}

export async function deleteArticle(
  articleId: string | number,
  options?: { taskName?: string },
): Promise<{ ok: boolean; article?: ArticleSnapshot; message?: string; scoped?: boolean }> {
  try {
    const qs = new URLSearchParams();
    if (options?.taskName) qs.set("task_name", options.taskName);
    const url = `/api/articles/${articleId}${qs.toString() ? `?${qs.toString()}` : ""}`;
    const res = await apiFetch(url, { method: "DELETE" });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export type ArticleUpdatePatch = {
  media_name?: string;
  title?: string;
  published_at?: string;
  matched_tasks?: string[];
};

export async function updateArticle(
  articleId: string | number,
  patch: ArticleUpdatePatch,
): Promise<{ ok: boolean; article?: ArticleSnapshot; message?: string }> {
  try {
    const res = await apiFetch(`/api/articles/${articleId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

export async function updateArticleMediaType(
  articleId: string | number,
  mediaType: "authority" | "selfmedia",
): Promise<{ ok: boolean; article?: ArticleSnapshot; message?: string }> {
  try {
    const res = await apiFetch(`/api/articles/${articleId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_type: mediaType }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

// ── OCR Recognition ─────────────────────────────────────────

export async function fetchRecognitionStatus(options: { compact?: boolean; passive?: boolean } = {}): Promise<{ ok: boolean; status?: Record<string, unknown> }> {
  try {
    const params = new URLSearchParams();
    if (options.compact) params.set("compact", "1");
    if (options.passive) params.set("passive", "1");
    const query = params.toString() ? `?${params.toString()}` : "";
    const res = await apiFetch(`/api/recognition/status${query}`);
    return await res.json();
  } catch {
    return { ok: false };
  }
}

export async function recognitionAction(action: string, params: Record<string, unknown> = {}): Promise<{ ok: boolean; message?: string }> {
  try {
    const res = await apiFetch("/api/recognition/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...params }),
    });
    return await res.json();
  } catch {
    return { ok: false, message: "网络错误" };
  }
}

// ── Run Selected Tasks ──────────────────────────────────────

export async function triggerRunSelected(taskIds: string[]): Promise<{ queued: boolean; message: string }> {
  try {
    const res = await apiFetch("/api/actions/run-selected", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_ids: taskIds }),
    });
    return await res.json();
  } catch {
    return { queued: false, message: "触发失败" };
  }
}

// ── Save Profile ────────────────────────────────────────────

export async function saveProfile(profile: Record<string, string>): Promise<{ ok: boolean }> {
  try {
    const res = await apiFetch("/api/actions/save-profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    });
    return await res.json();
  } catch {
    return { ok: false };
  }
}
