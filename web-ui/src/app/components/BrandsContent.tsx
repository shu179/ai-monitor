import { Component, lazy, Suspense, useState, useRef, useEffect, useCallback, useMemo, type ErrorInfo, type ReactNode } from "react";
import { Search, Plus, ChevronDown, Zap, Edit2, Brain, X, Download, Calendar, Landmark, Check, Loader2 } from "lucide-react";
import { AreaChart, Area, ResponsiveContainer, YAxis } from "recharts";
import { ConfirmModal } from "./ConfirmModal";
import { AnimatedLoadingText } from "./AnimatedLoadingText";
import { ARTICLE_DATA_CHANGED_EVENT, CLOUD_ADMIN_USERS_CHANGED_EVENT, TASK_DATA_CHANGED_EVENT, TASK_DATA_CHANGED_SOURCE_BRANDS, fetchDeletedTasks, fetchTasksFull, readTasksFullCache, writeTasksFullCache, deleteTask, startTestRunTask, fetchTestRunStatus, cancelTestRunTask, forceSendSuccessfulTaskResults, restoreDeletedTask, syncCloudAdminTask, updateTask, type CloudAdminTaskSnapshot, type CloudUserSnapshot, type DeletedTaskSnapshot, type TaskFull, type TestRunStatus } from "../lib/backend";
import {
  ensureCloudAdminTasks,
  ensureCloudAdminUsers,
  invalidateCloudAdminTasksCache,
  readCloudAdminCache,
  subscribeCloudAdminCache,
  upsertCloudAdminTaskCache,
} from "../lib/cloudAdminCache";
import { notifySaveSuccess } from "../lib/saveToast";

const ArticleSummaryModal = lazy(() => import("./ArticleSummaryModal").then((module) => ({ default: module.ArticleSummaryModal })));
const BrandEditModal = lazy(() => import("./BrandEditModal").then((module) => ({ default: module.BrandEditModal })));
const TestRunModal = lazy(() => import("./TestRunModal").then((module) => ({ default: module.TestRunModal })));

// Color palette for brand cards — cycles through
const LOGO_COLORS = [
  "bg-blue-50 text-blue-600",
  "bg-orange-50 text-orange-600",
  "bg-emerald-50 text-emerald-600",
  "bg-purple-50 text-purple-600",
  "bg-green-50 text-green-600",
  "bg-cyan-50 text-cyan-600",
  "bg-rose-50 text-rose-600",
  "bg-amber-50 text-amber-600",
];
const CHART_COLORS = ["var(--brand-cyan)", "#f97316", "#10b981", "#8b5cf6", "#22c55e", "#06b6d4", "#f43f5e", "#f59e0b"];
const ALL_PLATFORMS = ["豆包", "DeepSeek", "Kimi", "通义千问", "文心一言", "元宝", "ChatGPT", "Claude", "Gemini"];
const PLATFORM_ID_TO_NAME: Record<string, string> = {
  doubao: "豆包",
  deepseek: "DeepSeek",
  kimi: "Kimi",
  tongyi: "通义千问",
  wenxin: "文心一言",
  yuanbao: "元宝",
  chatgpt: "ChatGPT",
  claude: "Claude",
  gemini: "Gemini",
};

type ActiveTestRun = { runId: string; brandName: string; taskId: string };

const ACTIVE_TEST_RUN_STORAGE_KEY = "surfaced-active-test-run";
const TASK_EVENT_SOURCE = TASK_DATA_CHANGED_SOURCE_BRANDS;

function emitTaskDataChanged() {
  window.dispatchEvent(new CustomEvent(TASK_DATA_CHANGED_EVENT, { detail: { source: TASK_EVENT_SOURCE } }));
}

function areTaskListsSame(left: TaskFull[], right: TaskFull[]) {
  if (left === right) {
    return true;
  }
  if (left.length !== right.length) {
    return false;
  }
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

function upsertTask(tasks: TaskFull[], incoming: TaskFull) {
  const incomingId = String(incoming.id || "").trim();
  if (!incomingId) {
    return tasks;
  }
  let found = false;
  const next = tasks.map((task) => {
    if (String(task.id || "").trim() !== incomingId) {
      return task;
    }
    found = true;
    return incoming;
  });
  if (!found) {
    next.push(incoming);
  }
  return next;
}

function readStoredActiveTestRun(): ActiveTestRun | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(ACTIVE_TEST_RUN_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ActiveTestRun>;
    const runId = String(parsed.runId || "").trim();
    const taskId = String(parsed.taskId || "").trim();
    const brandName = String(parsed.brandName || "").trim();
    if (!runId || !taskId) return null;
    return { runId, taskId, brandName: brandName || "当前品牌" };
  } catch {
    return null;
  }
}

function writeStoredActiveTestRun(run: ActiveTestRun | null) {
  if (typeof window === "undefined") return;
  try {
    if (!run?.runId || !run.taskId) {
      window.localStorage.removeItem(ACTIVE_TEST_RUN_STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(ACTIVE_TEST_RUN_STORAGE_KEY, JSON.stringify(run));
  } catch {
    // Storage can be unavailable in restricted browser contexts.
  }
}

function clearStoredActiveTestRun() {
  writeStoredActiveTestRun(null);
}

function isTerminalTestRunStatus(status?: TestRunStatus | null) {
  return status?.status === "success" || status?.status === "failed" || status?.status === "cancelled";
}

function getForceSendCount(status?: TestRunStatus | null) {
  return Math.max(0, Number(status?.sendableSuccessCount ?? status?.actualScreenshotCount ?? 0) || 0);
}

function mergeForceSendStatus(status: TestRunStatus, fallback?: TestRunStatus | null): TestRunStatus {
  if (status.status !== "failed") {
    return status;
  }
  const count = Math.max(getForceSendCount(status), getForceSendCount(fallback));
  if (count <= 0) {
    return status;
  }
  return {
    ...status,
    sendableSuccessCount: count,
    actualScreenshotCount: Math.max(0, Number(status.actualScreenshotCount || 0), count),
    canForceSendSuccess: true,
  };
}

function taskToBrand(task: TaskFull, idx: number) {
  const brand = task.brand || task.name;
  // Build a 2-char logo from the brand name
  const logo = brand.length >= 2 ? brand.slice(0, 2).toUpperCase() : brand.toUpperCase();
  const logoColor = LOGO_COLORS[idx % LOGO_COLORS.length];
  const chartColor = CHART_COLORS[idx % CHART_COLORS.length];

  // Format dates: "2026-01-10" -> "2026.01.10"
  const start = (task.optimization_start_date || "").replace(/-/g, ".");
  const end = (task.optimization_end_date || "").replace(/-/g, ".");

  // Calculate duration in days
  let duration = "0";
  if (task.optimization_start_date && task.optimization_end_date) {
    const d1 = new Date(task.optimization_start_date);
    const d2 = new Date(task.optimization_end_date);
    const diff = Math.round((d2.getTime() - d1.getTime()) / (1000 * 60 * 60 * 24));
    duration = String(Math.max(0, diff));
  }

  const industry = task.industry_tags?.[0] || "未分类";
  const region = task.region_tags?.[0] || "未知";

  return {
    id: task.id,
    name: brand,
    taskName: task.name,
    industry,
    region,
    logo,
    logoColor,
    start,
    end,
    duration,
    articles: task.article_count ?? 0,
    isTest: task.enabled,
    chartColor,
    platforms: task.platforms || [],
    keywords: task.keywords || [],
    mode: task.mode || "",
    optimizationTrend: task.optimization_trend || [],
    brandStatus: task.brand_status || task.status || "pending",
    sentToday: !!task.sent_today,
    scheduledToday: !!task.scheduled_today,
    formalStarted: !!task.formal_started,
    formalRunning: !!task.formal_running,
    hasGap: !!task.has_gap,
    gapReasons: task.gap_reasons || [],
    failedToday: !!task.failed_today,
    failedModes: task.failed_modes_today || [],
    failedUpdatedAt: task.failed_updated_at || "",
    failureKindToday: task.failure_kind_today || "",
    statusMessage: task.status_message || "",
    completedKeywordsToday: task.completed_keywords_today || [],
    actualScreenshotCountToday: Number(task.actual_screenshot_count_today || 0),
    fixedScreenshotTargetToday: Number(task.fixed_screenshot_target_today || 0),
    completedByQuotaToday: !!task.completed_by_quota_today,
    testFailureNotice: task.test_failure_notice || null,
    cloudTaskId: Number(task.cloud_task_id || 0) || null,
    cloudAssignedOperatorUserId: Number(task.cloud_assigned_operator_user_id || 0) || null,
    cloudAssignedOperatorUsername: task.cloud_assigned_operator_username || "",
    deletePending: Boolean(task.delete_pending),
    deletePendingAt: task.delete_pending_at || "",
    deletePendingExpiresAt: task.delete_pending_expires_at || "",
    deletePendingError: task.delete_pending_error || "",
  };
}

function isBrandTaskSuccessful(brand: ReturnType<typeof taskToBrand>) {
  return brand.sentToday
    || brand.completedByQuotaToday
    || brand.brandStatus === "success"
    || brand.brandStatus === "sent";
}

function getBrandSortBucket(brand: ReturnType<typeof taskToBrand>) {
  // 已关闭任务始终最后；已成功任务优先；当天未完成任务次之；非当天未完成任务再往后。
  if (!brand.isTest) {
    return 3;
  }
  if (isBrandTaskSuccessful(brand)) {
    return 0;
  }
  if (brand.scheduledToday) {
    return 1;
  }
  return 2;
}

function getBrandSortUpdatedAt(brand: ReturnType<typeof taskToBrand>) {
  const parsed = Date.parse(brand.failedUpdatedAt || "");
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function BrandsContent({
  currentDetectionMode = "browser",
  cloudRole = "",
  onSaveSuccess,
  onRecognitionTestStart,
}: {
  currentDetectionMode?: "browser" | "recognition";
  cloudRole?: string;
  onSaveSuccess?: (message?: string) => void;
  onRecognitionTestStart?: (payload: { taskId: string; taskName: string }) => void;
}) {
  const cachedTasksOnOpen = readTasksFullCache();
  const [tasks, setTasks] = useState<TaskFull[]>(() => cachedTasksOnOpen ?? []);
  const [loading, setLoading] = useState(() => !(cachedTasksOnOpen?.length));
  const [selectedBrand, setSelectedBrand] = useState<{name: string, articles: number, taskId: string, taskName: string} | null>(null);
  const [editingBrand, setEditingBrand] = useState<TaskFull | null>(null);
  const [isCreatingBrand, setIsCreatingBrand] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [testBrandConfirm, setTestBrandConfirm] = useState<{ id: string; name: string } | null>(null);
  const [deleteBrandConfirm, setDeleteBrandConfirm] = useState<string | null>(null);
  const [activeTestRun, setActiveTestRun] = useState<ActiveTestRun | null>(null);
  const [testRunStatus, setTestRunStatus] = useState<TestRunStatus | null>(null);
  const [testRunModalOpen, setTestRunModalOpen] = useState(false);
  const [testRunAbortPending, setTestRunAbortPending] = useState(false);
  const [testRunForceSending, setTestRunForceSending] = useState(false);
  const [testRunForceSendMessage, setTestRunForceSendMessage] = useState("");
  const [testRunForceSendError, setTestRunForceSendError] = useState("");
  const [cloudAdminEnabled, setCloudAdminEnabled] = useState(false);
  const [cloudOperators, setCloudOperators] = useState<CloudUserSnapshot[]>([]);
  const [cloudAdminTasks, setCloudAdminTasks] = useState<CloudAdminTaskSnapshot[]>([]);
  const [deletedTasks, setDeletedTasks] = useState<DeletedTaskSnapshot[]>([]);
  const [currentPage, setCurrentPage] = useState(1);
  const PAGE_SIZE = 6;
  const terminalTestRunToastRef = useRef<string>("");
  const isCloudViewer = cloudRole === "viewer";

  // Dropdown states
  const [selectedIndustry, setSelectedIndustry] = useState<string>('行业筛选');
  const [selectedRegion, setSelectedRegion] = useState<string>('地址筛选');
  const [activeDropdown, setActiveDropdown] = useState<'industry' | 'region' | null>(null);

  const dropdownRef = useRef<HTMLDivElement>(null);
  const loadTasksRequestRef = useRef(0);
  const displayedTasksRef = useRef<TaskFull[]>(cachedTasksOnOpen ?? []);
  const mutationVersionRef = useRef(0);

  useEffect(() => {
    displayedTasksRef.current = tasks;
  }, [tasks]);

  // Fetch tasks on mount
  const loadTasks = useCallback(async (options: { showLoadingState?: boolean; force?: boolean } = {}) => {
    const { showLoadingState = true, force = false } = options;
    const requestId = loadTasksRequestRef.current + 1;
    loadTasksRequestRef.current = requestId;
    const hasCachedTasks = Boolean(readTasksFullCache()?.length);
    if (showLoadingState && !hasCachedTasks) {
      setLoading(true);
    }
    const result = await fetchTasksFull({ force });
    if (requestId !== loadTasksRequestRef.current || requestId < mutationVersionRef.current) {
      return;
    }
    setTasks((current) => (areTaskListsSame(current, result) ? current : result));
    setLoading(false);
  }, []);

  useEffect(() => {
    const hasCachedTasks = Boolean(readTasksFullCache()?.length);
    if (!hasCachedTasks) {
      void loadTasks({ showLoadingState: true, force: false });
      return;
    }
    const timer = window.setTimeout(() => {
      void loadTasks({ showLoadingState: false, force: true });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [loadTasks]);

  useEffect(() => {
    const handleDataChanged = (event: Event) => {
      if (event instanceof CustomEvent && event.detail?.source === TASK_EVENT_SOURCE) {
        return;
      }
      void loadTasks({ showLoadingState: false, force: true });
    };
    window.addEventListener(ARTICLE_DATA_CHANGED_EVENT, handleDataChanged);
    window.addEventListener(TASK_DATA_CHANGED_EVENT, handleDataChanged);
    return () => {
      window.removeEventListener(ARTICLE_DATA_CHANGED_EVENT, handleDataChanged);
      window.removeEventListener(TASK_DATA_CHANGED_EVENT, handleDataChanged);
    };
  }, [loadTasks]);

  const refreshCloudAdminContext = useCallback(async () => {
    const isAdmin = cloudRole === "admin";
    setCloudAdminEnabled(isAdmin);
    if (!isAdmin) {
      setCloudOperators([]);
      setCloudAdminTasks([]);
      setDeletedTasks([]);
      return;
    }
    const cache = readCloudAdminCache();
    if (cache.usersLoaded) {
      setCloudOperators(cache.users.filter((user) => user.role === "operator"));
    }
    if (cache.tasksLoaded) {
      setCloudAdminTasks(cache.tasks);
    }
    const [usersSnapshot, tasksSnapshot, deletedResult] = await Promise.all([
      ensureCloudAdminUsers(),
      ensureCloudAdminTasks(),
      fetchDeletedTasks(),
    ]);
    setCloudOperators((usersSnapshot.users || []).filter((user) => user.role === "operator"));
    setCloudAdminTasks(tasksSnapshot.tasks || []);
    setDeletedTasks(deletedResult.tasks || []);
  }, [cloudRole]);

  useEffect(() => {
    const unsubscribe = subscribeCloudAdminCache((snapshot) => {
      setCloudOperators(snapshot.users.filter((user) => user.role === "operator"));
      setCloudAdminTasks(snapshot.tasks);
    });
    void refreshCloudAdminContext();
    return unsubscribe;
  }, [refreshCloudAdminContext]);

  useEffect(() => {
    const handleCloudUsersChanged = () => {
      void ensureCloudAdminUsers();
    };
    window.addEventListener(CLOUD_ADMIN_USERS_CHANGED_EVENT, handleCloudUsersChanged);
    return () => window.removeEventListener(CLOUD_ADMIN_USERS_CHANGED_EVENT, handleCloudUsersChanged);
  }, []);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setActiveDropdown(null);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Derive industries and brand addresses from task data
  const industries = useMemo(() => {
    const tags = new Set<string>();
    tasks.forEach(t => (t.industry_tags || []).forEach(tag => tags.add(tag)));
    return ['全部', ...Array.from(tags)];
  }, [tasks]);

  const regions = useMemo(() => {
    const tags = new Set<string>();
    tasks.forEach(t => (t.region_tags || []).forEach(tag => tags.add(tag)));
    return ['全部', ...Array.from(tags)];
  }, [tasks]);

  // Map tasks to brand card data
  const allBrandsData = useMemo(() => tasks.map((t, i) => taskToBrand(t, i)), [tasks]);
  const cloudAdminTaskById = useMemo(() => {
    const map = new Map<number, CloudAdminTaskSnapshot>();
    for (const task of cloudAdminTasks) {
      map.set(task.id, task);
    }
    return map;
  }, [cloudAdminTasks]);
  const cloudOperatorById = useMemo(() => {
    const map = new Map<number, CloudUserSnapshot>();
    for (const user of cloudOperators) {
      const id = Number(user.id || 0);
      if (id > 0) {
        map.set(id, user);
      }
    }
    return map;
  }, [cloudOperators]);
  const hasAnyFormalRunningTask = useMemo(() => allBrandsData.some((item) => item.formalRunning), [allBrandsData]);

  // Compute stats from real data
  const totalBrands = allBrandsData.length;
  const totalRecords = useMemo(() => tasks.reduce((sum, t) => sum + (t.success_records ?? 0), 0), [tasks]);

  const filteredBrands = useMemo(() => allBrandsData
    .filter(brand => {
      const matchIndustry = selectedIndustry === '行业筛选' || selectedIndustry === '全部' || brand.industry === selectedIndustry;
      const matchRegion = selectedRegion === '地址筛选' || selectedRegion === '全部' || brand.region === selectedRegion;
      const matchSearch = brand.name.toLowerCase().includes(searchQuery.toLowerCase());
      return matchIndustry && matchRegion && matchSearch;
    })
    .sort((a, b) => {
      const aBucket = getBrandSortBucket(a);
      const bBucket = getBrandSortBucket(b);
      if (aBucket !== bBucket) {
        return aBucket - bBucket;
      }

      const aNeedsAttention = a.failedToday || !!a.testFailureNotice;
      const bNeedsAttention = b.failedToday || !!b.testFailureNotice;
      if (aNeedsAttention !== bNeedsAttention) {
        return aNeedsAttention ? -1 : 1;
      }
      if (aBucket !== 3 && a.failedToday !== b.failedToday) {
        return a.failedToday ? -1 : 1;
      }

      const aUpdatedAt = getBrandSortUpdatedAt(a);
      const bUpdatedAt = getBrandSortUpdatedAt(b);
      if (aUpdatedAt !== bUpdatedAt) {
        return bUpdatedAt - aUpdatedAt;
      }
      return a.name.localeCompare(b.name, "zh-Hans-CN");
    }), [allBrandsData, searchQuery, selectedIndustry, selectedRegion]);

  // Pagination
  const totalPages = Math.max(1, Math.ceil(filteredBrands.length / PAGE_SIZE));
  const safePage = Math.min(currentPage, totalPages);
  const pagedBrands = filteredBrands.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const resolveBrandName = useCallback((taskId: string, fallback = "当前品牌") => {
    return allBrandsData.find((item) => item.id === taskId)?.name || fallback || "当前品牌";
  }, [allBrandsData]);

  // Reset to page 1 when filters change
  useEffect(() => { setCurrentPage(1); }, [searchQuery, selectedIndustry, selectedRegion]);

  // Real API handlers
  const handleTestRun = useCallback(async (taskId: string) => {
    const brand = allBrandsData.find((item) => item.id === taskId);
    const brandName = brand?.name || "当前品牌";
    setTestRunAbortPending(false);
    setTestRunForceSending(false);
    setTestRunForceSendMessage("");
    setTestRunForceSendError("");
    const result = await startTestRunTask(taskId);
    if (result.ok && result.recognitionTest) {
      onRecognitionTestStart?.({
        taskId: result.taskId || taskId,
        taskName: result.taskName || brand?.taskName || brandName,
      });
      notifySaveSuccess(onSaveSuccess, result.message || `${brandName} 识别模式测试已启动`);
      setTestBrandConfirm(null);
      return;
    }

    if (!result.ok || !result.runId) {
      setActiveTestRun({ runId: "", brandName, taskId });
      setTestRunStatus({
        ok: false,
        status: "failed",
        message: result.message || "测试启动失败",
        errorMessage: result.message || "测试启动失败",
        result: "failed",
      });
      setTestRunModalOpen(true);
      setTestBrandConfirm(null);
      return;
    }

    terminalTestRunToastRef.current = "";
    const nextRun = { runId: result.runId, brandName, taskId };
    setActiveTestRun(nextRun);
    writeStoredActiveTestRun(nextRun);
    setTestRunStatus({
      ok: true,
      runId: result.runId,
      status: "queued",
      message: "测试准备中",
      result: "",
      currentQuery: 0,
      completedQueries: 0,
      totalQueries: 0,
    });
    setTestRunModalOpen(true);
    setTestBrandConfirm(null);
  }, [allBrandsData, onRecognitionTestStart, onSaveSuccess]);

  const handleBackgroundContinue = useCallback(() => {
    setTestRunModalOpen(false);
    if (isTerminalTestRunStatus(testRunStatus) || !activeTestRun?.runId) {
      clearStoredActiveTestRun();
      setActiveTestRun(null);
      setTestRunAbortPending(false);
    }
  }, [activeTestRun?.runId, testRunStatus]);

  const handleAbortTestRun = useCallback(async () => {
    if (!activeTestRun?.runId || testRunAbortPending) {
      setTestRunModalOpen(false);
      return;
    }
    setTestRunAbortPending(true);
    const result = await cancelTestRunTask(activeTestRun.runId);
    setTestRunAbortPending(false);
    if (!result.ok) {
      setTestRunStatus((prev) => ({
        ...(prev || { ok: false }),
        ok: false,
        status: prev?.status || "running",
        message: result.message || "中断测试失败",
        errorMessage: result.message || "中断测试失败",
      }));
      return;
    }
    setTestRunStatus((prev) => ({
      ...(prev || { ok: true }),
      ok: true,
      runId: result.runId || prev?.runId || activeTestRun.runId,
      status: result.status || prev?.status || "running",
      message: result.message || prev?.message || "正在中断测试任务...",
      errorMessage: result.errorMessage || prev?.errorMessage || "",
    }));
    setTestRunModalOpen(false);
  }, [activeTestRun?.runId, testRunAbortPending]);

  const handleForceSendTestRun = useCallback(async () => {
    if (!activeTestRun?.taskId || testRunForceSending) {
      return;
    }
    setTestRunForceSending(true);
    setTestRunForceSendMessage("");
    setTestRunForceSendError("");
    try {
      const result = await forceSendSuccessfulTaskResults(activeTestRun.taskId);
      if (!result.ok) {
        setTestRunForceSendError(result.message || "发送失败");
        return;
      }
      const sentCount = Math.max(0, Number(result.actualScreenshotCount || 0));
      const message = result.message || `已发送 ${sentCount} 张成功截图，并将任务改判为成功`;
      setTestRunForceSendMessage(message);
      setTestRunStatus((prev) => ({
        ...(prev || { ok: true }),
        ok: true,
        status: "success",
        message,
        result: "success",
        errorMessage: "",
        failureDetails: [],
        sendableSuccessCount: sentCount,
        actualScreenshotCount: sentCount,
        canForceSendSuccess: false,
      }));
      clearStoredActiveTestRun();
      emitTaskDataChanged();
      await loadTasks({ showLoadingState: false, force: true });
      notifySaveSuccess(onSaveSuccess, message);
    } finally {
      setTestRunForceSending(false);
    }
  }, [activeTestRun?.taskId, loadTasks, onSaveSuccess, testRunForceSending]);

  const handleOpenActiveTestRunModal = useCallback(async () => {
    if (!activeTestRun?.taskId) {
      return;
    }
    setTestRunForceSending(false);
    setTestRunForceSendMessage("");
    setTestRunForceSendError("");
    setTestRunModalOpen(true);
    if (!activeTestRun.runId) {
      return;
    }
    const status = await fetchTestRunStatus(activeTestRun.runId);
    if (!status.ok) {
      return;
    }
    setTestRunStatus((prev) => mergeForceSendStatus(status, prev));
    if (isTerminalTestRunStatus(status)) {
      clearStoredActiveTestRun();
    } else {
      writeStoredActiveTestRun(activeTestRun);
    }
  }, [activeTestRun]);

  const handleOpenTestFailureNotice = useCallback(async (brand: ReturnType<typeof taskToBrand>) => {
    const noticeMessage = String(brand.testFailureNotice?.message || "测试失败").trim() || "测试失败";
    const runId = String(brand.testFailureNotice?.runId || "").trim();
    const sendableCount = Math.max(0, Number(brand.actualScreenshotCountToday || 0));
    const nextRun = { runId, brandName: brand.name || "当前品牌", taskId: brand.id };
    const fallbackStatus: TestRunStatus = {
      ok: true,
      runId,
      taskId: brand.id,
      taskName: brand.taskName,
      status: "failed",
      message: noticeMessage,
      result: "failed",
      errorMessage: noticeMessage,
      failureDetails: [],
      sendableSuccessCount: sendableCount,
      actualScreenshotCount: sendableCount,
      canForceSendSuccess: sendableCount > 0,
    };

    setActiveTestRun(nextRun);
    setTestRunStatus(fallbackStatus);
    setTestRunAbortPending(false);
    setTestRunForceSending(false);
    setTestRunForceSendMessage("");
    setTestRunForceSendError("");
    setTestRunModalOpen(true);
    if (!runId) {
      return;
    }

    const status = await fetchTestRunStatus(runId);
    if (!status.ok) {
      return;
    }
    setTestRunStatus(mergeForceSendStatus(status, fallbackStatus));
    if (isTerminalTestRunStatus(status)) {
      clearStoredActiveTestRun();
      return;
    }
    writeStoredActiveTestRun(nextRun);
  }, []);

  const handleOpenTaskProgress = useCallback((brand: ReturnType<typeof taskToBrand>) => {
    const sendableCount = Math.max(0, Number(brand.actualScreenshotCountToday || 0));
    const completedCount = Math.max(0, Number(brand.completedKeywordsToday?.length || 0));
    const noticeMessage = String(brand.testFailureNotice?.message || "").trim();
    const message = noticeMessage
      || (sendableCount > 0
        ? `测试已完成 ${completedCount || sendableCount} 项，企业微信尚未发送`
        : `测试已完成 ${completedCount} 项，暂无可发送截图`);
    const runId = String(brand.testFailureNotice?.runId || "").trim();
    const nextRun = { runId, brandName: brand.name || "当前品牌", taskId: brand.id };
    setActiveTestRun(nextRun);
    setTestRunStatus({
      ok: true,
      runId,
      taskId: brand.id,
      taskName: brand.taskName,
      status: "failed",
      message,
      result: "failed",
      errorMessage: message,
      failureDetails: [],
      sendableSuccessCount: sendableCount,
      actualScreenshotCount: sendableCount,
      canForceSendSuccess: sendableCount > 0,
    });
    setTestRunAbortPending(false);
    setTestRunForceSending(false);
    setTestRunForceSendMessage("");
    setTestRunForceSendError("");
    setTestRunModalOpen(true);
    if (runId) {
      void handleOpenTestFailureNotice(brand);
    }
  }, [handleOpenTestFailureNotice]);

  useEffect(() => {
    if (activeTestRun?.runId) {
      return;
    }
    const stored = readStoredActiveTestRun();
    if (!stored?.runId) {
      return;
    }
    let cancelled = false;
    const restore = async () => {
      const status = await fetchTestRunStatus(stored.runId);
      if (cancelled) {
        return;
      }
      if (!status.ok || isTerminalTestRunStatus(status)) {
        clearStoredActiveTestRun();
        return;
      }
      const restoredRun = {
        ...stored,
        brandName: resolveBrandName(stored.taskId, stored.brandName),
      };
      setActiveTestRun(restoredRun);
      setTestRunStatus((prev) => mergeForceSendStatus(status, prev));
      setTestRunAbortPending(false);
      setTestRunForceSending(false);
      setTestRunForceSendMessage("");
      setTestRunForceSendError("");
      writeStoredActiveTestRun(restoredRun);
    };
    void restore();
    return () => {
      cancelled = true;
    };
  }, [activeTestRun?.runId, resolveBrandName]);

  useEffect(() => {
    if (!activeTestRun?.runId) return;

    let cancelled = false;
    const poll = async () => {
      const status = await fetchTestRunStatus(activeTestRun.runId);
      if (cancelled) return;
      setTestRunStatus((prev) => mergeForceSendStatus(status, prev));
      if (status.status === "success" || status.status === "failed" || status.status === "cancelled") {
        clearStoredActiveTestRun();
        void loadTasks({ showLoadingState: false, force: true });
        emitTaskDataChanged();
        if (!testRunModalOpen && terminalTestRunToastRef.current !== activeTestRun.runId) {
          terminalTestRunToastRef.current = activeTestRun.runId;
          const brandName = activeTestRun.brandName || "当前品牌";
          const toastMessage = status.status === "success"
            ? `${brandName} 测试已完成`
            : status.status === "cancelled"
              ? `${brandName} 测试已中断`
              : `${brandName} 测试失败`;
          notifySaveSuccess(onSaveSuccess, toastMessage);
        }
        if (!testRunModalOpen) {
          setActiveTestRun(null);
        }
        setTestRunAbortPending(false);
        return;
      }
      window.setTimeout(poll, 1000);
    };

    void poll();
    return () => {
      cancelled = true;
    };
  }, [activeTestRun?.runId, activeTestRun?.brandName, loadTasks, onSaveSuccess, testRunModalOpen]);

  const handleToggleEnabled = useCallback(async (taskId: string, currentEnabled: boolean) => {
    await updateTask(taskId, { enabled: !currentEnabled });
    setTasks(prev => prev.map(t => t.id === taskId ? { ...t, enabled: !currentEnabled } : t));
    emitTaskDataChanged();
  }, []);

  const handleDelete = useCallback(async (taskId: string) => {
    const result = await deleteTask(taskId);
    if (!result.ok) {
      notifySaveSuccess(onSaveSuccess, result.message || "删除失败");
      setDeleteBrandConfirm(null);
      return;
    }
    if (result.message) {
      notifySaveSuccess(onSaveSuccess, result.message);
    }
    invalidateCloudAdminTasksCache();
    await loadTasks({ showLoadingState: false, force: true });
    await Promise.all([
      ensureCloudAdminTasks({ force: true }),
      fetchDeletedTasks().then((payload) => setDeletedTasks(payload.tasks || [])),
    ]);
    setEditingBrand((current) => (current?.id === taskId ? null : current));
    setIsCreatingBrand(false);
    setDeleteBrandConfirm(null);
    emitTaskDataChanged();
  }, [loadTasks, onSaveSuccess]);

  const handleRestoreDeletedTask = useCallback(async (deletedTaskId: string, brandName: string) => {
    const result = await restoreDeletedTask({ deletedTaskId, brandName });
    if (result.ok) {
      invalidateCloudAdminTasksCache();
      await loadTasks({ showLoadingState: false, force: true });
      await Promise.all([
        ensureCloudAdminTasks({ force: true }),
        fetchDeletedTasks().then((payload) => setDeletedTasks(payload.tasks || [])),
      ]);
      emitTaskDataChanged();
      notifySaveSuccess(onSaveSuccess, result.message || "品牌配置已恢复");
    }
    return { ok: result.ok, message: result.message || "" };
  }, [loadTasks, onSaveSuccess]);

  const handleSyncCloudTaskFromBrand = useCallback(async (localTaskId: string, operatorUserId?: number) => {
    if (!cloudAdminEnabled) {
      return { ok: true, message: "" };
    }
    const result = await syncCloudAdminTask({ localTaskId, operatorUserId });
    if (result.ok && result.task) {
      upsertCloudAdminTaskCache(result.task);
    } else if (result.ok) {
      await ensureCloudAdminTasks({ force: true });
    }
    return {
      ok: result.ok,
      message: result.message || "",
      task: result.task,
      localTask: result.localTask,
    };
  }, [cloudAdminEnabled]);

  return (
    <div className="flex-1 h-full overflow-hidden bg-transparent px-8 py-8 xl:px-10 flex flex-col relative">
      {/* 1. Header & Filters */}
      <div className="grid grid-cols-[248px_minmax(0,1fr)] items-end gap-3 border-b border-gray-200/70 pb-4 mb-5 shrink-0">
        <div className="grid grid-cols-[68px_1px_128px] items-end gap-3 shrink-0">
          <div className="flex min-w-[68px] flex-col gap-1">
            <span className="text-[11px] font-bold text-gray-400 tracking-[0.16em] uppercase">总品牌数</span>
            <span className="text-[32px] font-black text-gray-900 leading-none tracking-[-0.04em] tabular-nums">{totalBrands}</span>
          </div>
          <div className="w-px h-11 bg-gray-200/80" />
          <div className="flex min-w-[128px] flex-col gap-1">
            <span className="text-[11px] font-bold text-gray-400 tracking-[0.16em] uppercase">总优化数</span>
            <span className="min-w-[112px] text-[32px] font-black text-gray-900 leading-none tracking-[-0.04em] tabular-nums">{totalRecords.toLocaleString()}</span>
          </div>
        </div>

        <div className="flex items-center justify-end gap-1.5 min-w-0">
          <div ref={dropdownRef} className="flex h-10 items-center border-b border-gray-200 relative shrink-0">
            <button 
              type="button"
              onClick={() => setActiveDropdown(activeDropdown === 'industry' ? null : 'industry')}
              className={`flex h-8 items-center gap-1.5 px-0 py-1.5 text-[12px] font-medium transition-colors ${
                activeDropdown === 'industry' || selectedIndustry !== '行业筛选' ? 'text-[var(--brand-navy)]' : 'text-gray-700 hover:text-gray-900'
              }`}
            >
              {selectedIndustry} <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-200 ${activeDropdown === 'industry' ? 'rotate-180 text-[var(--brand-navy)]' : 'text-gray-400'}`} />
            </button>
            
            {/* Industry Dropdown */}
            {activeDropdown === 'industry' && (
              <div className="absolute top-[calc(100%+8px)] left-0 w-40 bg-white border border-gray-200 rounded-xl py-1.5 z-50 animate-in fade-in slide-in-from-top-2 duration-200">
                {industries.map(ind => (
                  <button
                    type="button"
                    key={ind}
                    onClick={() => {
                      setSelectedIndustry(ind === '全部' ? '行业筛选' : ind);
                      setActiveDropdown(null);
                    }}
                    className="w-full flex items-center justify-between px-4 py-2.5 text-[12px] hover:bg-gray-50 transition-colors text-left group"
                  >
                    <span className={`font-medium ${selectedIndustry === ind || (ind === '全部' && selectedIndustry === '行业筛选') ? 'text-[var(--brand-navy)]' : 'text-gray-700 group-hover:text-gray-900'}`}>
                      {ind}
                    </span>
                    {(selectedIndustry === ind || (ind === '全部' && selectedIndustry === '行业筛选')) && (
                      <Check className="w-3.5 h-3.5 text-[var(--brand-navy)]" />
                    )}
                  </button>
                ))}
              </div>
            )}
            <div className="w-px h-4 bg-gray-200 mx-3"></div>
            
            <button 
              type="button"
              onClick={() => setActiveDropdown(activeDropdown === 'region' ? null : 'region')}
              className={`flex h-8 items-center gap-1.5 px-0 py-1.5 text-[12px] font-medium transition-colors ${
                activeDropdown === 'region' || selectedRegion !== '地址筛选' ? 'text-[var(--brand-navy)]' : 'text-gray-700 hover:text-gray-900'
              }`}
            >
              {selectedRegion} <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-200 ${activeDropdown === 'region' ? 'rotate-180 text-[var(--brand-navy)]' : 'text-gray-400'}`} />
            </button>

            {/* Region Dropdown */}
            {activeDropdown === 'region' && (
              <div className="absolute top-[calc(100%+8px)] right-0 w-36 bg-white border border-gray-200 rounded-xl py-1.5 z-50 animate-in fade-in slide-in-from-top-2 duration-200">
                {regions.map(reg => (
                  <button
                    type="button"
                    key={reg}
                    onClick={() => {
                      setSelectedRegion(reg === '全部' ? '地址筛选' : reg);
                      setActiveDropdown(null);
                    }}
                    className="w-full flex items-center justify-between px-4 py-2.5 text-[12px] hover:bg-gray-50 transition-colors text-left group"
                  >
                    <span className={`font-medium ${selectedRegion === reg || (reg === '全部' && selectedRegion === '地址筛选') ? 'text-[var(--brand-navy)]' : 'text-gray-700 group-hover:text-gray-900'}`}>
                      {reg}
                    </span>
                    {(selectedRegion === reg || (reg === '全部' && selectedRegion === '地址筛选')) && (
                      <Check className="w-3.5 h-3.5 text-[var(--brand-navy)]" />
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="flex h-10 min-w-[168px] max-w-[256px] flex-1 items-center border-b border-gray-200 px-0 focus-within:border-gray-900 transition-colors">
            <Search className="w-4 h-4 text-gray-400" />
            <input 
              type="text" 
              placeholder="搜索品牌..." 
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-transparent text-[12px] outline-none px-1.5 py-1.5 placeholder-gray-400 text-gray-900 font-medium"
            />
          </div>

          {!isCloudViewer && (
            <button
              type="button"
              onClick={() => setIsCreatingBrand(true)}
              className="flex h-10 shrink-0 items-center gap-1.5 text-gray-900 px-0 text-[12px] font-bold transition-colors hover:text-black"
            >
              <Plus className="w-4 h-4" strokeWidth={2.5} /> 新建品牌
            </button>
          )}
        </div>
      </div>

      {/* 2. Grid of Brand Cards */}
      <div className="flex-1 min-h-0 overflow-y-auto pb-2 pr-2 -mr-2 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent">
        {loading ? (
          <div className="flex flex-col items-center justify-center h-full text-gray-400 gap-3">
            <AnimatedLoadingText className="text-[13px] font-medium text-gray-500" />
          </div>
        ) : pagedBrands.length > 0 ? (
          <div className="space-y-4 xl:space-y-5">
            {pagedBrands.map((brand, idx) => {
              const cloudTask = brand.cloudTaskId ? cloudAdminTaskById.get(brand.cloudTaskId) : undefined;
              const operatorUserId = Number(cloudTask?.assigned_operator_user_id || brand.cloudAssignedOperatorUserId || 0) || 0;
              const operatorUser = operatorUserId > 0 ? cloudOperatorById.get(operatorUserId) : undefined;
              const operatorUsername = String(
                operatorUser?.display_name
                || operatorUser?.username
                || cloudTask?.assigned_operator_display_name
                || cloudTask?.assigned_operator_username
                || brand.cloudAssignedOperatorUsername
                || "",
              ).trim();
              return (
                <BrandCardErrorBoundary
                  key={brand.id}
                  brandName={brand.name}
                >
                  <BrandCard
                  taskId={brand.id}
                  name={brand.name}
                  industry={brand.industry}
                  region={brand.region}
                  logo={brand.logo}
                  logoColor={brand.logoColor}
                  start={brand.start}
                  end={brand.end}
                  duration={brand.duration}
                  articles={brand.articles}
                  isTest={brand.isTest}
                  chartColor={brand.chartColor}
                  taskPlatforms={brand.platforms}
                  optimizationTrend={brand.optimizationTrend}
                  taskKeywords={brand.keywords}
                  brandStatus={brand.brandStatus}
                  sentToday={brand.sentToday}
                  scheduledToday={brand.scheduledToday}
                  formalStarted={brand.formalStarted}
                  formalRunning={brand.formalRunning}
                  hasGap={brand.hasGap}
                  gapReasons={brand.gapReasons}
                  failedToday={brand.failedToday}
                  failedModes={brand.failedModes}
                  failureKindToday={brand.failureKindToday}
                  statusMessage={brand.statusMessage}
                  completedKeywordsToday={brand.completedKeywordsToday}
                  actualScreenshotCountToday={brand.actualScreenshotCountToday}
                  fixedScreenshotTargetToday={brand.fixedScreenshotTargetToday}
                  completedByQuotaToday={brand.completedByQuotaToday}
                  testFailureNotice={brand.testFailureNotice}
                  deletePending={brand.deletePending}
                  deletePendingError={brand.deletePendingError}
                  showOperatorBadge={cloudAdminEnabled}
                  operatorUserId={operatorUserId}
                  operatorUsername={operatorUsername}
                  viewOnly={isCloudViewer}
                  testRunState={
                    activeTestRun?.taskId === brand.id
                      ? (
                        testRunAbortPending || testRunStatus?.cancelRequested
                          ? "cancelling"
                          : (testRunStatus?.status === "queued" || testRunStatus?.status === "running")
                            ? "running"
                            : null
                      )
                      : null
                  }
                  testBlockedGlobally={hasAnyFormalRunningTask}
                  onTestRunStatusClick={
                    activeTestRun?.taskId === brand.id
                      ? () => void handleOpenActiveTestRunModal()
                      : undefined
                  }
                  onTestFailureNoticeClick={
                    brand.testFailureNotice
                      ? () => void handleOpenTestFailureNotice(brand)
                      : undefined
                  }
                  onProgressClick={
                    !brand.sentToday && (brand.completedKeywordsToday.length > 0 || brand.actualScreenshotCountToday > 0)
                      ? () => handleOpenTaskProgress(brand)
                      : undefined
                  }
                  onArticlesClick={() => setSelectedBrand({ name: brand.name, articles: brand.articles, taskId: brand.id, taskName: brand.taskName })}
                  onEditClick={isCloudViewer ? undefined : () => setEditingBrand(tasks.find(t => t.id === brand.id) || null)}
                  onTestClick={() => {
                    if (isCloudViewer) {
                      return;
                    }
                    if (hasAnyFormalRunningTask) {
                      setActiveTestRun({ runId: "", brandName: brand.name, taskId: brand.id });
                      setTestRunStatus({
                        ok: false,
                        status: "failed",
                        message: "当前已有进行中的正式任务，请等待结束后再启动测试任务。",
                        errorMessage: "当前已有进行中的正式任务，请等待结束后再启动测试任务。",
                        result: "failed",
                      });
                      setTestRunModalOpen(true);
                      return;
                    }
                    setTestBrandConfirm({ id: brand.id, name: brand.name });
                  }}
                  onDeleteClick={isCloudViewer ? undefined : () => setDeleteBrandConfirm(brand.id)}
                  onToggleEnabled={isCloudViewer ? undefined : (enabled) => handleToggleEnabled(brand.id, enabled)}
                  />
                </BrandCardErrorBoundary>
              );
            })}
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center h-full text-gray-400 gap-3">
            <Search className="w-8 h-8 opacity-20" />
            <p className="text-[13px] font-medium">没有找到符合条件的品牌</p>
          </div>
        )}
      </div>
        
      {/* Pagination / Footer (Fixed at bottom) */}
      <div className="flex items-center justify-between pt-5 mt-4 border-t border-gray-200/70 shrink-0 bg-transparent">
        <span className="text-[12px] text-gray-500 font-medium">
          显示 {filteredBrands.length > 0 ? (safePage - 1) * PAGE_SIZE + 1 : 0} 至 {Math.min(safePage * PAGE_SIZE, filteredBrands.length)} 项，共 {filteredBrands.length} 项
        </span>
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
            disabled={safePage <= 1}
            className={`px-0 py-1.5 text-[12px] font-medium transition-colors ${
              safePage <= 1 ? 'text-gray-300 cursor-not-allowed' : 'text-gray-700 hover:text-gray-900'
            }`}
          >上一页</button>
          {Array.from({ length: totalPages }, (_, i) => i + 1)
            .filter(p => p === 1 || p === totalPages || Math.abs(p - safePage) <= 1)
            .reduce<(number | "ellipsis")[]>((acc, p, i, arr) => {
              if (i > 0 && p - (arr[i - 1] as number) > 1) acc.push("ellipsis");
              acc.push(p);
              return acc;
            }, [])
            .map((item, i) =>
              item === "ellipsis" ? (
                <span key={`e${i}`} className="px-2 py-1.5 text-gray-400">...</span>
              ) : (
                <button
                  type="button"
                  key={item}
                  onClick={() => setCurrentPage(item as number)}
                  className={`px-2 py-1.5 text-[12px] font-bold transition-colors ${
                    safePage === item
                      ? 'text-[var(--brand-navy)]'
                      : 'text-gray-700 hover:text-gray-900'
                  }`}
                >{item}</button>
              )
            )
          }
          <button
            type="button"
            onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
            disabled={safePage >= totalPages}
            className={`px-0 py-1.5 text-[12px] font-medium transition-colors ${
              safePage >= totalPages ? 'text-gray-300 cursor-not-allowed' : 'text-gray-700 hover:text-gray-900'
            }`}
          >下一页</button>
        </div>
      </div>

      {/* Modal */}
      {selectedBrand && (
        <Suspense fallback={null}>
          <ArticleSummaryModal
            brandName={selectedBrand.name}
            taskId={selectedBrand.taskId}
            taskName={selectedBrand.taskName}
            onClose={() => setSelectedBrand(null)}
          />
        </Suspense>
      )}

      {/* Edit/Create Modal */}
      {(editingBrand || isCreatingBrand) && (
        <ModalErrorBoundary
          modalTitle={editingBrand?.name || "品牌配置"}
          onClose={() => {
            setEditingBrand(null);
            setIsCreatingBrand(false);
          }}
        >
          <Suspense fallback={null}>
            <BrandEditModal
              brandName={editingBrand?.name || ""}
              brand={editingBrand || undefined}
              existingTasks={tasks}
              deletedTasks={deletedTasks}
              currentDetectionMode={currentDetectionMode}
              isNew={isCreatingBrand}
              cloudAdminEnabled={cloudAdminEnabled}
              cloudOperators={cloudOperators}
              canDelete={cloudAdminEnabled && !isCreatingBrand}
              initialOperatorUserId={
                editingBrand?.cloud_task_id
                  ? Number(cloudAdminTaskById.get(Number(editingBrand.cloud_task_id))?.assigned_operator_user_id || editingBrand.cloud_assigned_operator_user_id || 0) || 0
                  : 0
              }
              onCloudSync={handleSyncCloudTaskFromBrand}
              onDeleteClick={() => {
                if (editingBrand?.id) {
                  setDeleteBrandConfirm(editingBrand.id);
                }
              }}
              onRestoreDeletedTask={handleRestoreDeletedTask}
              onClose={() => {
                setEditingBrand(null);
                setIsCreatingBrand(false);
              }}
              onSave={async (savedTask, savedCloudTask, options) => {
                if (savedTask?.id) {
                  mutationVersionRef.current = loadTasksRequestRef.current + 1;
                  const nextTasks = upsertTask(displayedTasksRef.current, savedTask);
                  displayedTasksRef.current = nextTasks;
                  writeTasksFullCache(nextTasks);
                  setTasks(nextTasks);
                  if (isCreatingBrand) {
                    setCurrentPage(1);
                  }
                } else if (!savedCloudTask) {
                  await loadTasks({ showLoadingState: false, force: true });
                }
                if (savedCloudTask?.id) {
                  setCloudAdminTasks((prev) => {
                    const exists = prev.some((task) => task.id === savedCloudTask.id);
                    return exists
                      ? prev.map((task) => (task.id === savedCloudTask.id ? savedCloudTask : task))
                      : [...prev, savedCloudTask];
                  });
                }
                emitTaskDataChanged();
                if (options?.notify !== false) {
                  notifySaveSuccess(onSaveSuccess, options?.message || "保存成功");
                }
              }}
            />
          </Suspense>
        </ModalErrorBoundary>
      )}

      {/* Confirm Test Modal */}
      <ConfirmModal
        isOpen={!!testBrandConfirm}
        onClose={() => setTestBrandConfirm(null)}
        onConfirm={() => { if (testBrandConfirm) handleTestRun(testBrandConfirm.id); }}
        title="立即发起测试任务？"
        message="确定要对该品牌发起一次测试运行吗？系统将消耗相应的额度并生成测试报告。"
        confirmText="开始测试"
        type="primary"
      />

      {testRunModalOpen && (
        <Suspense fallback={null}>
          <TestRunModal
            isOpen={testRunModalOpen}
            onBackgroundContinue={handleBackgroundContinue}
            onAbort={handleAbortTestRun}
            onForceSend={handleForceSendTestRun}
            brandName={activeTestRun?.brandName || testBrandConfirm?.name || "当前品牌"}
            status={testRunStatus}
            abortPending={testRunAbortPending}
            forceSendPending={testRunForceSending}
            forceSendMessage={testRunForceSendMessage}
            forceSendError={testRunForceSendError}
          />
        </Suspense>
      )}

      {/* Confirm Delete Modal */}
      <ConfirmModal
        isOpen={!!deleteBrandConfirm}
        onClose={() => setDeleteBrandConfirm(null)}
        onConfirm={() => { if (deleteBrandConfirm) handleDelete(deleteBrandConfirm); }}
        title="确认删除品牌？"
        message="删除后品牌配置会保留三天可恢复备份；如果正式任务正在运行，会在运行结束并同步数据后自动删除。确定要继续吗？"
        confirmText="删除"
        type="danger"
      />
    </div>
  );
}

type ModalErrorBoundaryProps = {
  children: ReactNode;
  modalTitle: string;
  onClose: () => void;
};

type ModalErrorBoundaryState = {
  error: Error | null;
};

class ModalErrorBoundary extends Component<ModalErrorBoundaryProps, ModalErrorBoundaryState> {
  state: ModalErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ModalErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("BrandEditModal render error:", error, info);
  }

  render() {
    if (!this.state.error) {
      return this.props.children;
    }

    return (
      <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/20 backdrop-blur-[2px] p-6">
        <div className="w-full max-w-[620px] rounded-2xl border border-red-200 bg-white p-6 shadow-[0_20px_60px_-15px_rgba(0,0,0,0.12)]">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[16px] font-black text-gray-900">编辑窗口打开失败</div>
              <div className="mt-1 text-[12px] text-gray-500">{this.props.modalTitle}</div>
            </div>
            <button
              type="button"
              onClick={this.props.onClose}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-[12px] font-bold text-gray-700 hover:bg-gray-50"
            >
              关闭
            </button>
          </div>
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-[12px] text-red-700 whitespace-pre-wrap break-words">
            {this.state.error.message || "前端渲染异常"}
          </div>
          <div className="mt-3 text-[11px] text-gray-400">
            错误详情已输出到浏览器控制台。
          </div>
        </div>
      </div>
    );
  }
}

type BrandCardErrorBoundaryProps = {
  children: ReactNode;
  brandName: string;
};

type BrandCardErrorBoundaryState = {
  error: Error | null;
};

class BrandCardErrorBoundary extends Component<BrandCardErrorBoundaryProps, BrandCardErrorBoundaryState> {
  state: BrandCardErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BrandCardErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`BrandCard render error (${this.props.brandName}):`, error, info);
  }

  render() {
    if (!this.state.error) {
      return this.props.children;
    }

    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50/70 p-5 shadow-[0_8px_24px_-18px_rgba(217,119,6,0.22)]">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-[15px] font-bold text-amber-900">{this.props.brandName}</div>
            <div className="mt-1 text-[12px] text-amber-700">这张品牌卡片加载失败，其他品牌不受影响。</div>
          </div>
          <div className="rounded-lg border border-amber-200 bg-white px-2.5 py-1 text-[10px] font-bold tracking-[0.12em] text-amber-700">
            CARD ERROR
          </div>
        </div>
        <div className="mt-4 rounded-xl border border-amber-200 bg-white/80 px-4 py-3 text-[12px] leading-relaxed text-amber-800 whitespace-pre-wrap break-words">
          {this.state.error.message || "品牌卡片渲染异常"}
        </div>
      </div>
    );
  }
}

// ---- Subcomponents ----

function BrandCard({
  taskId, name, industry, region, logo, logoColor, start, end, duration, articles, isTest, chartColor, taskPlatforms, optimizationTrend, taskKeywords, brandStatus, sentToday, scheduledToday, formalStarted, formalRunning, hasGap, gapReasons, failedToday, failedModes, failureKindToday, statusMessage, completedKeywordsToday, actualScreenshotCountToday, fixedScreenshotTargetToday, completedByQuotaToday, testFailureNotice, deletePending, deletePendingError, showOperatorBadge, operatorUserId, operatorUsername, viewOnly, testRunState, testBlockedGlobally, onTestRunStatusClick, onTestFailureNoticeClick, onProgressClick, onArticlesClick, onEditClick, onTestClick, onDeleteClick, onToggleEnabled
}: {
  taskId: string, name: string, industry: string, region: string, logo: string, logoColor: string,
  start: string, end: string, duration: string, articles: number, isTest: boolean, chartColor: string,
  taskPlatforms: string[],
  optimizationTrend?: Array<{ value: number }>,
  taskKeywords?: Array<{ keyword: string; brand: string; platforms: string[]; mode: string; deep_think?: Record<string, boolean> }>,
  brandStatus: string,
  sentToday: boolean,
  scheduledToday: boolean,
  formalStarted: boolean,
  formalRunning: boolean,
  hasGap: boolean,
  gapReasons: string[],
  failedToday: boolean,
  failedModes: string[],
  failureKindToday?: string,
  statusMessage?: string,
  completedKeywordsToday: string[],
  actualScreenshotCountToday: number,
  fixedScreenshotTargetToday: number,
  completedByQuotaToday: boolean,
  testFailureNotice?: { message: string; updatedAt: string; expiresAt: string; runId?: string } | null,
  deletePending?: boolean,
  deletePendingError?: string,
  showOperatorBadge?: boolean,
  operatorUserId?: number,
  operatorUsername?: string,
  viewOnly?: boolean,
  testRunState?: "running" | "cancelling" | null,
  testBlockedGlobally?: boolean,
  onTestRunStatusClick?: () => void,
  onTestFailureNoticeClick?: () => void,
  onProgressClick?: () => void,
  onArticlesClick?: () => void, onEditClick?: () => void, onTestClick?: () => void, onDeleteClick?: () => void,
  onToggleEnabled?: (currentEnabled: boolean) => void
}) {
  const [taskActive, setTaskActive] = useState(isTest);
  useEffect(() => {
    setTaskActive(isTest);
  }, [isTest]);

  const platforms = useMemo(() => {
    const activePlatformNames = new Set((taskPlatforms || []).map(id => PLATFORM_ID_TO_NAME[id] || id));
    const deepPlatformNames = new Set<string>();
    for (const kw of (taskKeywords || [])) {
      for (const [pname, val] of Object.entries(kw.deep_think || {})) {
        if (val) deepPlatformNames.add(pname);
      }
    }
    return ALL_PLATFORMS.map(name => ({
      name,
      active: activePlatformNames.has(name),
      deep: deepPlatformNames.has(name),
    }));
  }, [taskKeywords, taskPlatforms]);

  const isSendFailure = failureKindToday === "notification";
  const isNoScreenshotFailure = failureKindToday === "no_screenshot";
  const failureBadgeClassName = isSendFailure
    ? "bg-amber-100/90 text-amber-700 border-amber-200/80"
    : isNoScreenshotFailure
      ? "bg-orange-100/90 text-orange-700 border-orange-200/80"
    : "bg-red-100/90 text-red-600 border-red-200/80";
  const failureLabel = isSendFailure ? "待补发" : isNoScreenshotFailure ? "待补图" : "待补跑";
  const failureSummary = isSendFailure
    ? "发送状态：企业微信发送未成功"
    : isNoScreenshotFailure
      ? "发送状态：暂无可发送图片"
    : `失败模式：${failedModes.length > 0 ? failedModes.join("、") : "今日任务失败"}`;
  const completedToday = sentToday;
  const configuredKeywordCount = useMemo(() => {
    const keywords = new Set<string>();
    for (const item of taskKeywords || []) {
      const keyword = String(item?.keyword || "").trim();
      if (keyword) keywords.add(keyword);
    }
    return keywords.size;
  }, [taskKeywords]);
  const completedKeywordCount = completedKeywordsToday.length || (completedToday ? configuredKeywordCount : 0);
  const successProgressSummary = fixedScreenshotTargetToday > 0
    ? `今日进度：已完成 ${completedKeywordCount} 词，截图 ${actualScreenshotCountToday}/${fixedScreenshotTargetToday}${completedByQuotaToday ? "，已补齐" : ""}`
    : completedKeywordCount > 0
      ? `今日进度：已完成 ${completedKeywordCount} 个关键词`
      : "";
  const progressToneClassName = sentToday
    ? "text-emerald-600 hover:text-emerald-700"
    : "text-amber-700 hover:text-amber-800";
  const testFailureMessage = String(testFailureNotice?.message || "").trim();
  const deletePendingMessage = String(deletePendingError || "").trim() || (deletePending ? "删除已排队，正式任务结束后自动处理" : "");
  const isTesting = testRunState === "running";
  const isCancelling = testRunState === "cancelling";
  const isTestBlocked = !!testBlockedGlobally && !isTesting && !isCancelling;
  const testStatusBadge = isCancelling
    ? { className: "bg-amber-100/90 text-amber-700 border-amber-200/80", label: "正在中断" }
    : isTesting
      ? { className: "bg-slate-100/90 text-[var(--brand-navy)] border-slate-300/80", label: "测试中" }
      : null;
  const showFormalGap = !sentToday && !formalRunning && brandStatus === "gap";
  const showPendingBadge = brandStatus === "pending" && scheduledToday;
  const brandStatusBadge = sentToday
    ? { className: "bg-emerald-100/90 text-emerald-700 border-emerald-200/80", label: "今日已发送" }
    : formalRunning || brandStatus === "running"
      ? { className: "bg-sky-100/90 text-sky-700 border-sky-200/80", label: "运行中" }
      : showFormalGap
        ? { className: "bg-red-100/90 text-red-600 border-red-200/80", label: "失败待补齐" }
        : brandStatus === "success"
          ? { className: "bg-amber-100/90 text-amber-700 border-amber-200/80", label: "待发送" }
          : showPendingBadge
            ? { className: "bg-gray-100/90 text-gray-600 border-gray-200/80", label: "未运行" }
            : null;
  const rootClassName = isCancelling
    ? "relative border-b border-amber-300/90 py-6"
    : isTesting
      ? "relative border-b border-slate-300/90 py-6"
      : (showFormalGap || failedToday)
        ? "border-b border-red-200/80 py-6"
        : "border-b border-gray-200/80 py-6";

  return (
    <div className={rootClassName}>
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_220px] gap-6">
        <div className="min-w-0">
          <div className="flex items-start gap-3.5">
            <div className={`w-11 h-11 rounded-lg flex items-center justify-center font-black text-[16px] tracking-tighter ${logoColor}`}>
              {logo}
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex flex-wrap items-center gap-1.5">
                  <span className="text-[16px] font-bold text-gray-900 leading-none">{name}</span>
                  <span className="px-1.5 py-0.5 bg-gray-100 text-gray-500 text-[9px] rounded font-bold tracking-wider">{industry}</span>
                  <span className="px-1.5 py-0.5 bg-gray-100 text-gray-500 text-[9px] rounded font-bold tracking-wider">{region}</span>
                  {testStatusBadge && (
                    <button
                      type="button"
                      onClick={onTestRunStatusClick}
                      disabled={!onTestRunStatusClick}
                      className={`px-1.5 py-0.5 text-[9px] rounded font-bold tracking-wider border transition-colors disabled:cursor-default ${testStatusBadge.className}`}
                    >
                      {testStatusBadge.label}
                    </button>
                  )}
                  {brandStatusBadge && (
                    <span className={`px-1.5 py-0.5 text-[9px] rounded font-bold tracking-wider border ${brandStatusBadge.className}`}>
                      {brandStatusBadge.label}
                    </span>
                  )}
                  {failedToday && (
                    <span className={`px-1.5 py-0.5 text-[9px] rounded font-bold tracking-wider border ${failureBadgeClassName}`}>
                      {failureLabel}
                    </span>
                  )}
                  {deletePending && (
                    <span className="px-1.5 py-0.5 bg-rose-50 text-rose-600 text-[9px] rounded font-bold tracking-wider border border-rose-100">
                      待删除
                    </span>
                  )}
                </div>
                {showOperatorBadge && (
                  <span className="shrink-0 px-1.5 py-0.5 bg-slate-50 text-slate-600 text-[9px] rounded font-bold tracking-wider border border-slate-200">
                    运营 {operatorUserId ? `#${operatorUserId}` : "未分配"}{operatorUsername ? ` · ${operatorUsername}` : ""}
                  </span>
                )}
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-2 text-[11px] text-gray-500">
                <span className="font-medium font-mono text-gray-900">{start} - {end}</span>
                <span>共 {duration} 天</span>
                <button
                  type="button"
                  onClick={onArticlesClick}
                  className={`font-medium transition-colors ${
                    failedToday ? "text-red-400 hover:text-red-600" : "text-gray-400 hover:text-gray-900"
                  }`}
                >
                  已发表文章 <span className="text-gray-900 font-bold ml-0.5">{articles.toLocaleString()}</span> 篇
                </button>
              </div>

              {(failedToday || successProgressSummary || testStatusBadge || testFailureMessage || deletePendingMessage || (showFormalGap && gapReasons.length > 0)) && (
                <div className="mt-3 space-y-1.5">
                  {failedToday && (
                    <div className={`text-[11px] font-medium ${isNoScreenshotFailure ? "text-orange-600/90" : "text-red-500/90"}`}>
                      {failureSummary}
                      {statusMessage ? ` · ${statusMessage}` : ""}
                    </div>
                  )}
                  {!failedToday && showFormalGap && gapReasons.length > 0 && (
                    <div className="text-[11px] text-red-500/90 font-medium">
                      缺口：{gapReasons.join("；")}
                    </div>
                  )}
                  {successProgressSummary && (
                    <button
                      type="button"
                      onClick={onProgressClick}
                      disabled={!onProgressClick}
                      className={`block text-left text-[11px] font-medium transition-colors disabled:cursor-default ${progressToneClassName}`}
                    >
                      {successProgressSummary}
                    </button>
                  )}
                  {testStatusBadge && (
                    <button
                      type="button"
                      onClick={onTestRunStatusClick}
                      disabled={!onTestRunStatusClick}
                      className={`inline-flex items-center gap-1 text-left text-[11px] font-medium transition-colors disabled:cursor-default ${isCancelling ? "text-amber-700 hover:text-amber-800" : "text-[var(--brand-navy)] hover:text-blue-700"}`}
                    >
                      <Loader2 className="w-3 h-3 animate-spin" />
                      {isCancelling ? "测试任务正在安全中断，当前步骤结束后会停止" : "测试任务正在后台执行，关闭弹窗后仍会继续"}
                    </button>
                  )}
                  {!failedToday && testFailureMessage && (
                    <button
                      type="button"
                      onClick={onTestFailureNoticeClick}
                      className="block text-left text-[11px] text-amber-600 font-medium transition-colors hover:text-amber-700"
                    >
                      测试失败提醒：{testFailureMessage}
                    </button>
                  )}
                  {deletePendingMessage && (
                    <div className="text-[11px] text-rose-500 font-medium">
                      {deletePendingMessage}
                    </div>
                  )}
                </div>
              )}

              <div className="mt-4">
                <div className="min-w-0">
                  <div className="text-[9px] font-bold text-gray-400 tracking-widest uppercase">优化平台与深度思考</div>
                  <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1.5">
                    {platforms.map((p) => (
                      <PlatformItem key={p.name} name={p.name} defaultActive={p.active} defaultDeep={p.deep} />
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="xl:pl-2 xl:border-l xl:border-gray-200/70">
          <div className="flex items-center justify-between xl:justify-start xl:gap-5">
            {!viewOnly && (
              <div className="flex items-center gap-2">
                <button 
                  type="button"
                  onClick={onTestClick}
                  disabled={isTesting || isCancelling}
                  className={`flex items-center gap-1 text-[10px] font-bold transition-colors ${
                    isTesting || isCancelling
                      ? "text-gray-400 cursor-not-allowed"
                      : isTestBlocked
                        ? "text-amber-600 hover:text-amber-700"
                      : "text-gray-500 hover:text-[var(--brand-navy)]"
                }`}
              >
                <Zap size={10} className={isTesting || isCancelling ? "text-gray-400" : isTestBlocked ? "text-amber-600" : "text-[var(--brand-navy)]"} /> {isCancelling ? "中断中" : isTesting ? "测试中" : isTestBlocked ? "测试受限" : "测试"}
              </button>
              <span className="text-gray-200">/</span>
              <button 
                type="button"
                onClick={onEditClick}
                className="flex items-center gap-1 text-[10px] font-bold text-gray-500 hover:text-gray-900 transition-colors"
              >
                <Edit2 size={10} /> 编辑
              </button>
              </div>
            )}
            {!viewOnly && (
              <div className="flex items-center gap-1.5 cursor-pointer" onClick={() => { setTaskActive(!taskActive); onToggleEnabled?.(taskActive); }}>
                <span className={`text-[9px] font-bold uppercase tracking-widest transition-colors ${taskActive ? 'text-gray-500' : 'text-gray-400'}`}>
                  {taskActive ? '任务进行中' : '任务已暂停'}
                </span>
                <TinySwitch checked={taskActive} onChange={() => { setTaskActive(!taskActive); onToggleEnabled?.(taskActive); }} />
              </div>
            )}
          </div>

          <div className="mt-4 space-y-4">
            <div>
              <div className="text-[9px] font-bold text-gray-400 tracking-widest uppercase">地址分布</div>
              <div className="mt-2 h-[54px] relative overflow-hidden border-b border-gray-100/80">
                <MiniMap />
              </div>
            </div>
            <div>
              <div className="text-[9px] font-bold text-gray-400 tracking-widest uppercase">优化趋势</div>
              <div className="mt-2 h-[54px] relative border-b border-gray-100/80">
                <div className="absolute inset-y-0 left-1.5 right-1.5">
                  <MiniTrendChart color={chartColor} data={optimizationTrend || []} />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function PlatformItem({ name, defaultActive, defaultDeep }: { name: string, defaultActive: boolean, defaultDeep: boolean }) {
  const [active, setActive] = useState(defaultActive);
  const [deep, setDeep] = useState(defaultDeep);

  return (
    <div className={`grid grid-cols-[minmax(0,1fr)_1px_auto] items-center gap-2 pl-0.5 pr-2 py-1 transition-all min-w-0 ${
      active ? 'bg-transparent text-gray-900' : 'bg-transparent opacity-60'
    }`}>
      <div 
        className="flex items-center gap-1.5 cursor-pointer min-w-0 pr-1.5" 
        onClick={() => setActive(!active)}
      >
        <div className={`w-1.5 h-1.5 rounded-full transition-colors ${active ? 'bg-[var(--brand-cyan)]' : 'bg-gray-300'}`} />
        <span className={`text-[10px] font-bold transition-colors ${active ? 'text-gray-900' : 'text-gray-500'}`}>{name}</span>
      </div>
      
      <div className="w-px h-3.5 bg-gray-200 self-center"></div>
      
      <div 
        className={`flex items-center gap-1 shrink-0 pl-1.5 justify-self-end ${active ? 'cursor-pointer' : 'cursor-not-allowed'}`} 
        onClick={() => active && setDeep(!deep)}
      >
        <Brain className={`w-3 h-3 transition-colors ${deep && active ? 'text-[var(--brand-cyan)]' : 'text-gray-300'}`} />
        <MicroSwitch checked={deep} onChange={() => active && setDeep(!deep)} disabled={!active} />
      </div>
    </div>
  );
}

function MicroSwitch({ checked, onChange, disabled }: { checked: boolean, onChange: () => void, disabled?: boolean }) {
  return (
    <div 
      className={`w-5 h-2.5 rounded-full flex items-center p-[1px] transition-colors ${
        disabled ? 'bg-gray-200' : checked ? 'bg-[var(--brand-cyan)]' : 'bg-gray-200'
      }`}
    >
      <div className={`w-2 h-2 rounded-full bg-white transition-transform shadow-sm ${checked && !disabled ? 'translate-x-[10px]' : 'translate-x-0'}`} />
    </div>
  )
}

function TinySwitch({ checked, onChange }: { checked: boolean, onChange: () => void }) {
  return (
    <div 
      className={`w-[26px] h-[14px] rounded-full flex items-center p-[2px] transition-colors ${checked ? 'bg-[var(--brand-cyan)]' : 'bg-gray-200'}`}
    >
      <div className={`w-[10px] h-[10px] rounded-full bg-white transition-transform shadow-sm ${checked ? 'translate-x-[12px]' : 'translate-x-0'}`} />
    </div>
  )
}

function MiniTrendChart({ color, data }: { color: string, data: Array<{ value: number }> }) {
  const safeData = data.length > 0 ? data : [
    { value: 0 },
    { value: 0 },
    { value: 0 },
    { value: 0 },
    { value: 0 },
    { value: 0 },
  ];
  const values = safeData.map(item => item.value);
  const maxValue = Math.max(...values, 1);
  const gradientId = `color-${color.replace(/[^a-zA-Z0-9_-]/g, "")}-${safeData.length}-${Math.round(maxValue)}`;

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={safeData} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={color} stopOpacity={0.2}/>
            <stop offset="95%" stopColor={color} stopOpacity={0}/>
          </linearGradient>
        </defs>
        <YAxis domain={[0, maxValue]} hide />
        <Area 
          type="monotone" 
          dataKey="value" 
          stroke={color} 
          strokeWidth={1.5}
          fillOpacity={1} 
          fill={`url(#${gradientId})`} 
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function MiniMap() {
  const nodes = [
    { x: '20%', y: '30%' }, { x: '50%', y: '60%' }, { x: '80%', y: '40%' }, { x: '35%', y: '75%' }
  ];
  return (
    <>
      <div className="absolute inset-0 opacity-[0.05]" style={{ backgroundImage: 'linear-gradient(#000 1px, transparent 1px), linear-gradient(90deg, #000 1px, transparent 1px)', backgroundSize: '8px 8px' }}></div>
      <svg className="absolute inset-0 w-full h-full pointer-events-none opacity-30">
        <line x1="20%" y1="30%" x2="50%" y2="60%" stroke="var(--brand-cyan)" strokeWidth="1" strokeDasharray="2 2" />
        <line x1="50%" y1="60%" x2="80%" y2="40%" stroke="var(--brand-cyan)" strokeWidth="1" strokeDasharray="2 2" />
        <line x1="50%" y1="60%" x2="35%" y2="75%" stroke="var(--brand-cyan)" strokeWidth="1" strokeDasharray="2 2" />
      </svg>
      {nodes.map((n, i) => (
        <div key={i} className="absolute w-1.5 h-1.5 bg-[var(--brand-cyan)] border-[1.5px] border-white rounded-full shadow-sm transform -translate-x-1/2 -translate-y-1/2" style={{ left: n.x, top: n.y }}></div>
      ))}
    </>
  );
}
