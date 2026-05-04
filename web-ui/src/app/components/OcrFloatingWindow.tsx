import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, MouseEvent as ReactMouseEvent } from "react";
import { ArrowLeftRight, Camera, Globe, Loader2, Search } from "lucide-react";
import {
  ARTICLE_DATA_CHANGED_EVENT,
  fetchArticles,
  fetchBootstrap,
  fetchRecognitionStatus,
  importArticle,
  recognitionAction,
} from "../lib/backend";
import surfacedAppIcon from "../../assets/surfaced-app-icon1.png";

type KeywordEntry = {
  keyword: string;
  platforms: string[];
  screenshotCount: number;
  screenshotTotal: number;
  completedKeywordCount: number;
  totalKeywordCount: number;
};

type TaskGroup = {
  brandName: string;
  keywords: KeywordEntry[];
};

type GuideItem = {
  task_name: string;
  keyword: string;
  brands?: string[];
  platforms?: string[];
  screenshot_count?: number;
  screenshot_total?: number;
  completed_keyword_count?: number;
  total_keyword_count?: number;
};

type PlatformOption = {
  id: string;
  label: string;
  short: string;
};

type FloatingAnchor = {
  x: number;
  y: number;
};

type RecognitionViewState = {
  taskGroups: TaskGroup[];
  currentGroupIdx: number;
  currentKwIdx: number;
  detailText: string;
  manualMode: boolean;
  controlsVisible: boolean;
  actionLabel: string | null;
  actionEnabled: boolean;
  running: boolean;
  groupCount: number;
  guideIndex: number;
  lastStatusMessage: string;
  error: string | null;
};

type ResidentSummary = {
  todayArticleCount: number;
  completedTaskCount: number;
  todayTaskCount: number;
  runningTaskCount: number;
};

const PLATFORM_NAME_MAP: Record<string, string> = {
  doubao: "豆包",
  deepseek: "DeepSeek",
  ark_deepseek: "方舟 DeepSeek",
  kimi: "Kimi",
  tongyi: "通义千问",
  wenxin: "文心一言",
  yuanbao: "元宝",
  chatgpt: "ChatGPT",
  claude: "Claude",
  gemini: "Gemini",
  perplexity: "Perplexity",
};

const BROWSER_PLATFORM_OPTIONS: PlatformOption[] = [
  { id: "doubao", label: "豆包", short: "豆" },
  { id: "deepseek", label: "DeepSeek", short: "D" },
  { id: "kimi", label: "Kimi", short: "K" },
  { id: "yuanbao", label: "元宝", short: "元" },
  { id: "tongyi", label: "通义", short: "通" },
  { id: "wenxin", label: "文心", short: "文" },
];

const PRIMARY_CLUSTER_GAP = 10;
const SECONDARY_PANEL_GAP = 8;
const FLOATING_BALL_SIZE = 40;
const PRIMARY_PANEL_WIDTH = 184;
const SECONDARY_PANEL_WIDTH = 120;
const FLOATING_COLLAPSE_DELAY_MS = 500;
const PLATFORM_ROW_HEIGHT = 34;
const PLATFORM_ROW_GAP = 2;
const PLATFORM_PANEL_VERTICAL_PADDING = 16;
const SECONDARY_PANEL_HEIGHT =
  BROWSER_PLATFORM_OPTIONS.length * PLATFORM_ROW_HEIGHT
  + Math.max(0, BROWSER_PLATFORM_OPTIONS.length - 1) * PLATFORM_ROW_GAP
  + PLATFORM_PANEL_VERTICAL_PADDING;
const PRIMARY_PANEL_ESTIMATED_HEIGHT = 182;
const BRAND_PILL_WIDTH = PRIMARY_PANEL_WIDTH - FLOATING_BALL_SIZE - PRIMARY_CLUSTER_GAP;
const EXPANDED_GROUP_WIDTH = PRIMARY_PANEL_WIDTH + SECONDARY_PANEL_WIDTH + SECONDARY_PANEL_GAP;
const EXPANDED_GROUP_HEIGHT = FLOATING_BALL_SIZE + PRIMARY_CLUSTER_GAP + Math.max(SECONDARY_PANEL_HEIGHT, PRIMARY_PANEL_ESTIMATED_HEIGHT);
const FLOATING_GROUP_MARGIN = 16;
const FLOATING_ANCHOR_STORAGE_KEY = "ocr-floating-window-anchor-v1";
const FLOATING_SURFACE_SELECTOR = "[data-ocr-floating-surface='true']";
const PLATFORM_SURFACE_SELECTOR = "[data-ocr-platform-surface='true']";
const PLATFORM_BRIDGE_SELECTOR = "[data-ocr-platform-bridge='true']";
const BALL_MENU_SELECTOR = "[data-ocr-ball-menu='true']";
const BALL_TRIGGER_SELECTOR = "[data-ocr-ball-trigger='true']";

function closestElement(target: EventTarget | null, selector: string): Element | null {
  if (typeof Element === "undefined" || !(target instanceof Element)) {
    return null;
  }
  return target.closest(selector);
}

function formatPlatformName(platform: string): string {
  const normalized = String(platform || "").trim();
  if (!normalized) {
    return "";
  }
  return PLATFORM_NAME_MAP[normalized] || normalized;
}

function toFiniteCount(value: unknown): number {
  const count = Number(value);
  return Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
}

function currentLocalDateKey(): string {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function articleDateKey(article: { ts?: string; published_at?: string }): string {
  return String(article.published_at || article.ts || "").slice(0, 10);
}

export function OcrFloatingWindow({
  onClose,
  localOCR = true,
  stopOnClose = false,
  resident = false,
}: {
  onClose: () => void;
  localOCR?: boolean;
  stopOnClose?: boolean;
  resident?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [taskGroups, setTaskGroups] = useState<TaskGroup[]>([]);
  const [currentGroupIdx, setCurrentGroupIdx] = useState(0);
  const [currentKwIdx, setCurrentKwIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detailText, setDetailText] = useState("");
  const [manualMode, setManualMode] = useState(false);
  const [controlsVisible, setControlsVisible] = useState(true);
  const [actionLabel, setActionLabel] = useState<string | null>(null);
  const [actionEnabled, setActionEnabled] = useState(true);
  const [running, setRunning] = useState(false);
  const [groupCount, setGroupCount] = useState(0);
  const [guideIndex, setGuideIndex] = useState(0);
  const [lastStatusMessage, setLastStatusMessage] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [platformPanelOpen, setPlatformPanelOpen] = useState(false);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [ballMenuOpen, setBallMenuOpen] = useState(false);
  const [anchor, setAnchor] = useState<FloatingAnchor>({ x: 0, y: FLOATING_GROUP_MARGIN });
  const [residentArticleInput, setResidentArticleInput] = useState("");
  const [residentImporting, setResidentImporting] = useState(false);
  const [residentSummary, setResidentSummary] = useState<ResidentSummary>({
    todayArticleCount: 0,
    completedTaskCount: 0,
    todayTaskCount: 0,
    runningTaskCount: 0,
  });
  const hasLoadedOnceRef = useRef(false);
  const collapseTimerRef = useRef<number | null>(null);
  const platformTimerRef = useRef<number | null>(null);
  const copyTimerRef = useRef<number | null>(null);
  const messageTimerRef = useRef<number | null>(null);
  const ballClickTimerRef = useRef<number | null>(null);
  const ballClickCountRef = useRef(0);
  const statusRequestRef = useRef<Promise<void> | null>(null);
  const platformRequestRef = useRef<string | null>(null);
  const platformBrowserCloseRequestedRef = useRef(false);
  const platformBrowserOpenedRef = useRef(false);
  const residentInputFocusedRef = useRef(false);
  const viewStateSignatureRef = useRef("");
  const onCloseRef = useRef(onClose);
  const dragStateRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
    moved: boolean;
  } | null>(null);
  const suppressBallClickRef = useRef(false);

  const clampAnchor = useCallback((nextX: number, nextY: number, _nextExpanded: boolean): FloatingAnchor => {
    if (typeof window === "undefined") {
      return { x: nextX, y: nextY };
    }
    const minX = FLOATING_GROUP_MARGIN + (EXPANDED_GROUP_WIDTH - FLOATING_BALL_SIZE);
    const maxX = window.innerWidth - FLOATING_BALL_SIZE - FLOATING_GROUP_MARGIN;
    const maxY = window.innerHeight - EXPANDED_GROUP_HEIGHT - FLOATING_GROUP_MARGIN;
    return {
      x: Math.min(Math.max(nextX, minX), Math.max(minX, maxX)),
      y: Math.min(Math.max(nextY, FLOATING_GROUP_MARGIN), Math.max(FLOATING_GROUP_MARGIN, maxY)),
    };
  }, []);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const clearCollapseTimer = useCallback(() => {
    if (collapseTimerRef.current !== null) {
      window.clearTimeout(collapseTimerRef.current);
      collapseTimerRef.current = null;
    }
  }, []);

  const clearCopyTimer = useCallback(() => {
    if (copyTimerRef.current !== null) {
      window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = null;
    }
  }, []);

  const clearPlatformTimer = useCallback(() => {
    if (platformTimerRef.current !== null) {
      window.clearTimeout(platformTimerRef.current);
      platformTimerRef.current = null;
    }
  }, []);

  const clearMessageTimer = useCallback(() => {
    if (messageTimerRef.current !== null) {
      window.clearTimeout(messageTimerRef.current);
      messageTimerRef.current = null;
    }
  }, []);

  const flashActionMessage = useCallback((message: string | null) => {
    clearMessageTimer();
    setActionMessage(message);
    if (!message) {
      return;
    }
    messageTimerRef.current = window.setTimeout(() => {
      setActionMessage(null);
      messageTimerRef.current = null;
    }, 2200);
  }, [clearMessageTimer]);

  const loadResidentSummary = useCallback(async () => {
    if (!resident) {
      return;
    }
    try {
      const [articleResult, bootstrap] = await Promise.all([
        fetchArticles({ limit: 5000 }),
        fetchBootstrap(),
      ]);
      const todayKey = currentLocalDateKey();
      const apiTodayTotal = Number(articleResult.today_total);
      const todayArticleCount = Number.isFinite(apiTodayTotal)
        ? Math.max(0, Math.floor(apiTodayTotal))
        : (articleResult.articles || []).filter(
          (article) => articleDateKey(article) === todayKey,
        ).length;
      setResidentSummary({
        todayArticleCount,
        completedTaskCount: toFiniteCount(bootstrap.dashboard?.completedCount),
        todayTaskCount: toFiniteCount(bootstrap.dashboard?.todayTaskCount),
        runningTaskCount: toFiniteCount(bootstrap.dashboard?.runningCount),
      });
    } catch {
      // Keep the previous resident summary if a refresh fails.
    }
  }, [resident]);

  const collapseWindow = useCallback(() => {
    clearCollapseTimer();
    clearPlatformTimer();
    setExpanded(false);
    setPlatformPanelOpen(false);
    setBallMenuOpen(false);
  }, [clearCollapseTimer, clearPlatformTimer]);

  const closePlatformBrowser = useCallback(async () => {
    if (platformBrowserCloseRequestedRef.current) {
      return;
    }
    platformBrowserCloseRequestedRef.current = true;
    await recognitionAction("close_platform_browser");
    platformBrowserOpenedRef.current = false;
  }, []);

  const hideFloatingWindow = useCallback(() => {
    collapseWindow();
    onCloseRef.current();
  }, [collapseWindow]);

  const dismissFloatingWindow = useCallback(async (options: { closeBrowser?: boolean; stop?: boolean } = {}) => {
    const closeBrowser = options.closeBrowser !== false;
    if (closeBrowser) {
      await closePlatformBrowser();
    }
    if (options.stop) {
      await recognitionAction("stop");
    }
    hideFloatingWindow();
  }, [closePlatformBrowser, hideFloatingWindow]);

  const scheduleWindowCollapse = useCallback(() => {
    if (residentInputFocusedRef.current) {
      return;
    }
    clearCollapseTimer();
    collapseTimerRef.current = window.setTimeout(() => {
      if (residentInputFocusedRef.current) {
        return;
      }
      collapseTimerRef.current = null;
      collapseWindow();
    }, FLOATING_COLLAPSE_DELAY_MS);
  }, [clearCollapseTimer, collapseWindow]);

  const closeWindowFromSurfaceLeave = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    if (closestElement(event.relatedTarget, `${FLOATING_SURFACE_SELECTOR},${PLATFORM_SURFACE_SELECTOR},${PLATFORM_BRIDGE_SELECTOR},${BALL_MENU_SELECTOR}`)) {
      return;
    }
    scheduleWindowCollapse();
  }, [scheduleWindowCollapse]);

  const closePlatformPanelFromSurfaceLeave = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    const nextTargetIsFloatingSurface = closestElement(event.relatedTarget, FLOATING_SURFACE_SELECTOR);
    if (!nextTargetIsFloatingSurface) {
      scheduleWindowCollapse();
      return;
    }
    if (!closestElement(event.relatedTarget, `${PLATFORM_SURFACE_SELECTOR},${PLATFORM_BRIDGE_SELECTOR}`)) {
      setPlatformPanelOpen(false);
    }
  }, [scheduleWindowCollapse]);

  const closePlatformBridgeFromLeave = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    if (closestElement(event.relatedTarget, `${PLATFORM_SURFACE_SELECTOR},${PLATFORM_BRIDGE_SELECTOR}`)) {
      return;
    }
    if (closestElement(event.relatedTarget, FLOATING_SURFACE_SELECTOR)) {
      setPlatformPanelOpen(false);
      return;
    }
    scheduleWindowCollapse();
  }, [scheduleWindowCollapse]);

  const expandWindow = useCallback(() => {
    clearCollapseTimer();
    setBallMenuOpen(false);
    setExpanded(true);
  }, [clearCollapseTimer]);

  const openPlatformPanel = useCallback(() => {
    clearCollapseTimer();
    clearPlatformTimer();
    setPlatformPanelOpen(true);
  }, [clearCollapseTimer, clearPlatformTimer]);

  const applyViewState = useCallback((nextState: RecognitionViewState) => {
    const nextSignature = JSON.stringify(nextState);
    if (nextSignature === viewStateSignatureRef.current) {
      return;
    }
    viewStateSignatureRef.current = nextSignature;
    setTaskGroups(nextState.taskGroups);
    setCurrentGroupIdx(nextState.currentGroupIdx);
    setCurrentKwIdx(nextState.currentKwIdx);
    setDetailText(nextState.detailText);
    setManualMode(nextState.manualMode);
    setControlsVisible(nextState.controlsVisible);
    setActionLabel(nextState.actionLabel);
    setActionEnabled(nextState.actionEnabled);
    setRunning(nextState.running);
    setGroupCount(nextState.groupCount);
    setGuideIndex(nextState.guideIndex);
    setLastStatusMessage(nextState.lastStatusMessage);
    setError(nextState.error);
  }, []);

  const loadTasks = useCallback(async (silent = false) => {
    if (statusRequestRef.current) {
      return statusRequestRef.current;
    }
    const request = (async () => {
    try {
      if (!silent || !hasLoadedOnceRef.current) {
        setLoading(true);
      }
      setError(null);
      const res = await fetchRecognitionStatus({ compact: true, passive: resident });
      if (!res.ok || !res.status) {
        applyViewState({
          taskGroups: [],
          currentGroupIdx: 0,
          currentKwIdx: 0,
          detailText: "",
          manualMode: false,
          controlsVisible: true,
          actionLabel: null,
          actionEnabled: true,
          running: false,
          groupCount: 0,
          guideIndex: 0,
          lastStatusMessage: "无法读取识别状态",
          error: null,
        });
        return;
      }

      const status = res.status as Record<string, unknown>;
      const isRunning = Boolean(status.running);

      if (!isRunning && hasLoadedOnceRef.current && !resident) {
        await dismissFloatingWindow();
        return;
      }

      const guide = status.keyword_guide as
        | {
            items: GuideItem[];
            index: number;
            manual_mode?: boolean;
            detail_text?: string;
            controls_visible?: boolean;
            action_label?: string | null;
            action_enabled?: string | boolean | null;
          }
        | undefined;

      if (!guide || !guide.items || guide.items.length === 0) {
        applyViewState({
          taskGroups: [],
          currentGroupIdx: 0,
          currentKwIdx: 0,
          detailText: String(guide?.detail_text || "").trim(),
          manualMode: false,
          controlsVisible: true,
          actionLabel: null,
          actionEnabled: true,
          running: isRunning,
          groupCount: 0,
          guideIndex: 0,
          lastStatusMessage: isRunning ? "识别模式已启动，但暂时还没有可展示的关键词" : "识别模式当前未运行",
          error: null,
        });
        return;
      }

      const totalItems = guide.items.length;
      const normalizedGuideIndex = Math.max(
        0,
        Math.min(typeof guide.index === "number" ? guide.index : 0, totalItems - 1),
      );
      const nextDetailText = String(guide.detail_text || "").trim();
      const nextManualMode = Boolean(guide.manual_mode);
      const nextControlsVisible = guide.controls_visible !== false;
      const nextActionLabel = guide.action_label ? String(guide.action_label) : null;
      const nextActionEnabled = guide.action_enabled !== "disabled" && guide.action_enabled !== false;

      const groupMap = new Map<string, TaskGroup>();
      const groupOrder: string[] = [];
      for (const item of guide.items) {
        const key = item.task_name;
        if (!groupMap.has(key)) {
          groupMap.set(key, {
            brandName: item.brands?.find((brand) => Boolean(brand?.trim())) || item.task_name || "未命名品牌",
            keywords: [],
          });
          groupOrder.push(key);
        }
        groupMap.get(key)!.keywords.push({
          keyword: item.keyword,
          platforms: Array.from(
            new Set(
              (item.platforms || [])
                .map((platform) => String(platform || "").trim())
                .filter(Boolean),
            ),
          ),
          screenshotCount: toFiniteCount(item.screenshot_count),
          screenshotTotal: Math.max(1, toFiniteCount(item.screenshot_total) || 1),
          completedKeywordCount: toFiniteCount(item.completed_keyword_count),
          totalKeywordCount: toFiniteCount(item.total_keyword_count),
        });
      }

      const groups = groupOrder.map((key) => groupMap.get(key)!);
      let nextGroupIdx = 0;
      let nextKwIdx = 0;

      if (totalItems > 0) {
        let accumulated = 0;
        let matchedPosition = false;
        for (let groupIndex = 0; groupIndex < groups.length; groupIndex += 1) {
          if (normalizedGuideIndex < accumulated + groups[groupIndex].keywords.length) {
            nextGroupIdx = groupIndex;
            nextKwIdx = normalizedGuideIndex - accumulated;
            matchedPosition = true;
            break;
          }
          accumulated += groups[groupIndex].keywords.length;
        }
        if (!matchedPosition && groups.length > 0) {
          const lastGroupIdx = groups.length - 1;
          nextGroupIdx = lastGroupIdx;
          nextKwIdx = Math.max(0, groups[lastGroupIdx].keywords.length - 1);
        }
      }
      applyViewState({
        taskGroups: groups,
        currentGroupIdx: nextGroupIdx,
        currentKwIdx: nextKwIdx,
        detailText: nextDetailText,
        manualMode: nextManualMode,
        controlsVisible: nextControlsVisible,
        actionLabel: nextActionLabel,
        actionEnabled: nextActionEnabled,
        running: isRunning,
        groupCount: totalItems,
        guideIndex: normalizedGuideIndex,
        lastStatusMessage: "",
        error: null,
      });
    } catch {
      applyViewState({
        taskGroups: [],
        currentGroupIdx: 0,
        currentKwIdx: 0,
        detailText: "",
        manualMode: false,
        controlsVisible: true,
        actionLabel: null,
        actionEnabled: true,
        running: false,
        groupCount: 0,
        guideIndex: 0,
        lastStatusMessage: "后端识别状态接口不可用",
        error: "无法连接后端",
      });
    } finally {
      hasLoadedOnceRef.current = true;
      setLoading(false);
      statusRequestRef.current = null;
    }
    })();
    statusRequestRef.current = request;
    return request;
  }, [applyViewState, dismissFloatingWindow, resident]);

  useEffect(() => {
    if (!resident) {
      void recognitionAction("start");
    }
    void loadTasks(false);
  }, [loadTasks, resident]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadTasks(true);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [loadTasks]);

  useEffect(() => {
    if (!resident) {
      return;
    }
    void loadResidentSummary();
    const handleArticleDataChanged = () => {
      void loadResidentSummary();
    };
    window.addEventListener(ARTICLE_DATA_CHANGED_EVENT, handleArticleDataChanged);
    const timer = window.setInterval(() => {
      void loadResidentSummary();
    }, 5000);
    return () => {
      window.removeEventListener(ARTICLE_DATA_CHANGED_EVENT, handleArticleDataChanged);
      window.clearInterval(timer);
    };
  }, [loadResidentSummary, resident]);

  useEffect(() => {
    const warmupTimers = [200, 700, 1500].map((delay) =>
      window.setTimeout(() => {
        void loadTasks(true);
      }, delay),
    );
    return () => {
      warmupTimers.forEach((timer) => window.clearTimeout(timer));
    };
  }, [loadTasks]);

  useEffect(() => () => {
    clearCollapseTimer();
    clearPlatformTimer();
    clearCopyTimer();
    clearMessageTimer();
    if (ballClickTimerRef.current !== null) {
      window.clearTimeout(ballClickTimerRef.current);
      ballClickTimerRef.current = null;
    }
    if (!resident) {
      void closePlatformBrowser();
    }
  }, [clearCollapseTimer, clearPlatformTimer, clearCopyTimer, clearMessageTimer, closePlatformBrowser, resident]);

  useEffect(() => {
    if (!ballMenuOpen) {
      return;
    }
    const closeMenuFromPointer = (event: PointerEvent) => {
      if (closestElement(event.target, `${BALL_MENU_SELECTOR},${BALL_TRIGGER_SELECTOR}`)) {
        return;
      }
      setBallMenuOpen(false);
    };
    const closeMenuFromEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setBallMenuOpen(false);
      }
    };
    window.addEventListener("pointerdown", closeMenuFromPointer);
    window.addEventListener("keydown", closeMenuFromEscape);
    return () => {
      window.removeEventListener("pointerdown", closeMenuFromPointer);
      window.removeEventListener("keydown", closeMenuFromEscape);
    };
  }, [ballMenuOpen]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const savedAnchor = window.localStorage.getItem(FLOATING_ANCHOR_STORAGE_KEY);
    if (savedAnchor) {
      try {
        const parsed = JSON.parse(savedAnchor) as Partial<FloatingAnchor>;
        if (typeof parsed.x === "number" && typeof parsed.y === "number") {
          setAnchor(clampAnchor(parsed.x, parsed.y, false));
          return;
        }
      } catch {
        // Ignore malformed persisted state and fall back to default.
      }
    }
    setAnchor(
      clampAnchor(
        window.innerWidth - FLOATING_BALL_SIZE - FLOATING_GROUP_MARGIN,
        FLOATING_GROUP_MARGIN,
        false,
      ),
    );
  }, [clampAnchor]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    window.localStorage.setItem(FLOATING_ANCHOR_STORAGE_KEY, JSON.stringify(anchor));
  }, [anchor]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const handleResize = () => {
      setAnchor((current) => clampAnchor(current.x, current.y, expanded));
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [clampAnchor, expanded]);

  const hasGroups = taskGroups.length > 0;
  const currentGroup = hasGroups ? taskGroups[currentGroupIdx] : null;
  const totalKeywords = currentGroup ? currentGroup.keywords.length : 0;
  const currentKeywordEntry = currentGroup ? currentGroup.keywords[currentKwIdx] : null;
  const currentKeyword = currentKeywordEntry?.keyword || "";
  const currentPlatforms = currentKeywordEntry?.platforms || [];
  const currentPlatformSet = new Set(currentPlatforms);
  const screenshotCount = currentKeywordEntry?.screenshotCount ?? 0;
  const screenshotTotal = Math.max(1, currentKeywordEntry?.screenshotTotal ?? 1);
  const keywordProgressTotal = Math.max(0, currentKeywordEntry?.totalKeywordCount ?? totalKeywords);
  const keywordProgressDone = Math.min(
    keywordProgressTotal,
    Math.max(0, currentKeywordEntry?.completedKeywordCount ?? 0),
  );
  const totalGuideItems = Math.max(
    groupCount,
    hasGroups ? taskGroups.reduce((sum, group) => sum + group.keywords.length, 0) : 0,
  );
  const isResidentToolMode = resident && !running;

  const isFirst = guideIndex <= 0;
  const isLast = totalGuideItems <= 0 || guideIndex >= totalGuideItems - 1;
  const shouldCompleteOnForward = isLast || manualMode;
  const disablePrev = !hasGroups || isFirst || !controlsVisible;
  const disableForward = !hasGroups || !controlsVisible || (shouldCompleteOnForward && !actionEnabled);

  const handleCopy = useCallback(async () => {
    if (!currentKeyword) {
      return;
    }
    try {
      await navigator.clipboard.writeText(currentKeyword);
      clearCopyTimer();
      setCopied(true);
      copyTimerRef.current = window.setTimeout(() => {
        setCopied(false);
        copyTimerRef.current = null;
      }, 1400);
    } catch {
      flashActionMessage("复制失败");
    }
  }, [clearCopyTimer, currentKeyword, flashActionMessage]);

  const handleNext = useCallback(async () => {
    const result = await recognitionAction("next");
    if (result.ok) {
      setCopied(false);
      await loadTasks();
      return;
    }
    flashActionMessage(result.message || "切换失败");
  }, [flashActionMessage, loadTasks]);

  const handlePrev = useCallback(async () => {
    const result = await recognitionAction("prev");
    if (result.ok) {
      setCopied(false);
      await loadTasks();
      return;
    }
    flashActionMessage(result.message || "切换失败");
  }, [flashActionMessage, loadTasks]);

  const handleComplete = useCallback(async () => {
    const result = await recognitionAction("complete");
    if (result.ok) {
      setCopied(false);
      await loadTasks();
      if (!running && taskGroups.length === 0) {
        await dismissFloatingWindow();
      }
      return;
    }
    flashActionMessage(result.message || "操作失败");
  }, [dismissFloatingWindow, flashActionMessage, loadTasks, running, taskGroups.length]);

  const handleOpenPlatform = useCallback(async (platformId: string) => {
    if (platformRequestRef.current) {
      return;
    }
    platformRequestRef.current = platformId;
    setBusyAction(platformId);
    try {
      const result = await recognitionAction("open_platform", { platform: platformId });
      flashActionMessage(result.ok ? `已打开 ${formatPlatformName(platformId)}` : result.message || "打开平台失败");
      if (result.ok) {
        platformBrowserCloseRequestedRef.current = false;
        platformBrowserOpenedRef.current = true;
      }
    } finally {
      platformRequestRef.current = null;
      setBusyAction(null);
    }
  }, [flashActionMessage]);

  const handleTriggerScreenshot = useCallback(async () => {
    setBusyAction("screenshot");
    const result = await recognitionAction("screenshot");
    setBusyAction(null);
    flashActionMessage(result.ok ? (result.message || "请框选截图区域") : result.message || "截图失败");
  }, [flashActionMessage]);

  const handleTogglePlatformBrowser = useCallback(async () => {
    platformBrowserCloseRequestedRef.current = false;
    const result = await recognitionAction("toggle_platform_browser");
    flashActionMessage(result.ok ? (result.message || "已切换浏览器窗口") : result.message || "浏览器窗口切换失败");
    if (result.ok) {
      platformBrowserOpenedRef.current = true;
    }
  }, [flashActionMessage]);

  const handleResidentArticleSubmit = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const url = residentArticleInput.trim();
    if (!url || residentImporting) {
      return;
    }
    setResidentImporting(true);
    try {
      const result = await importArticle(url);
      if (result.ok || result.duplicate || result.article) {
        setResidentArticleInput("");
        flashActionMessage(result.duplicate ? "文章已存在" : "文章已录入");
        window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
        await loadResidentSummary();
        return;
      }
      flashActionMessage(result.message || "录入失败");
    } catch {
      flashActionMessage("录入失败");
    } finally {
      setResidentImporting(false);
    }
  }, [flashActionMessage, loadResidentSummary, residentArticleInput, residentImporting]);

  const handleClose = useCallback(async () => {
    await dismissFloatingWindow({ stop: stopOnClose });
  }, [dismissFloatingWindow, stopOnClose]);

  const handleDisableToday = useCallback(async () => {
    setBallMenuOpen(false);
    platformBrowserCloseRequestedRef.current = true;
    hideFloatingWindow();
    const result = await recognitionAction("suppress_current_task_today");
    if (!result.ok) {
      await recognitionAction("close_platform_browser");
    }
    if (stopOnClose) {
      await recognitionAction("stop");
    }
  }, [hideFloatingWindow, stopOnClose]);

  const handleDisable = useCallback(async () => {
    setBallMenuOpen(false);
    platformBrowserCloseRequestedRef.current = true;
    hideFloatingWindow();
    const result = await recognitionAction("disable_recognition");
    if (!result.ok) {
      await recognitionAction("close_platform_browser");
    }
  }, [hideFloatingWindow]);

  const handleStepSwitch = useCallback(async (backward: boolean) => {
    if (backward) {
      await handlePrev();
      return;
    }
    if (shouldCompleteOnForward) {
      await handleComplete();
      return;
    }
    await handleNext();
  }, [handleComplete, handleNext, handlePrev, shouldCompleteOnForward]);

  const handleBallPointerDown = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) {
      return;
    }
    dragStateRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: anchor.x,
      originY: anchor.y,
      moved: false,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }, [anchor.x, anchor.y]);

  const handleBallPointerMove = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    const dragState = dragStateRef.current;
    if (!dragState || dragState.pointerId !== event.pointerId) {
      return;
    }
    const deltaX = event.clientX - dragState.startX;
    const deltaY = event.clientY - dragState.startY;
    if (!dragState.moved && Math.hypot(deltaX, deltaY) > 3) {
      dragState.moved = true;
      suppressBallClickRef.current = true;
    }
    if (!dragState.moved) {
      return;
    }
    setAnchor(clampAnchor(dragState.originX + deltaX, dragState.originY + deltaY, expanded));
  }, [clampAnchor, expanded]);

  const handleBallPointerUp = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    const dragState = dragStateRef.current;
    if (!dragState || dragState.pointerId !== event.pointerId) {
      return;
    }
    dragStateRef.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    if (dragState.moved) {
      window.setTimeout(() => {
        suppressBallClickRef.current = false;
      }, 0);
    }
  }, []);

  const handleBallContextMenu = useCallback((event: ReactMouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (isResidentToolMode) {
      return;
    }
    clearCollapseTimer();
    setExpanded(false);
    setPlatformPanelOpen(false);
    setBallMenuOpen(true);
  }, [clearCollapseTimer, isResidentToolMode]);

  const handleBallClick = useCallback(() => {
    if (suppressBallClickRef.current) {
      return;
    }
    if (ballMenuOpen) {
      setBallMenuOpen(false);
      return;
    }
    ballClickCountRef.current += 1;
    if (ballClickTimerRef.current !== null) {
      window.clearTimeout(ballClickTimerRef.current);
    }
    ballClickTimerRef.current = window.setTimeout(() => {
      const clickCount = ballClickCountRef.current;
      ballClickCountRef.current = 0;
      ballClickTimerRef.current = null;
      if (clickCount >= 2) {
        void handleTogglePlatformBrowser();
        collapseWindow();
        return;
      }
      if (expanded) {
        collapseWindow();
        return;
      }
      expandWindow();
    }, 320);
  }, [ballMenuOpen, collapseWindow, expandWindow, expanded, handleTogglePlatformBrowser]);

  const statusDetailMessage = detailText.trim();
  const helperMessage = actionMessage
    || (!isResidentToolMode && statusDetailMessage)
    || (isResidentToolMode
      ? (error ? "后端连接异常，部分工具不可用" : "常驻工具 · 未启动识别监听")
      : (!hasGroups
      ? (lastStatusMessage || "识别模式")
      : (manualMode ? "Shift 返回" : "")));

  const renderPlatformPanel = () => (
    <div
      className="rounded-[18px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.86)_0%,rgba(255,255,255,0.74)_44%,rgba(248,250,252,0.64)_100%)] px-3 py-2 shadow-[0_0_0_1px_rgba(255,255,255,0.92),0_44px_90px_-30px_rgba(15,23,42,0.46),0_24px_44px_-20px_rgba(15,23,42,0.32),0_10px_20px_-10px_rgba(15,23,42,0.26),0_3px_8px_rgba(15,23,42,0.12)] backdrop-blur-2xl backdrop-saturate-150"
      style={{ width: `${SECONDARY_PANEL_WIDTH}px` }}
    >
      <div className="space-y-0.5">
        {BROWSER_PLATFORM_OPTIONS.map((platform) => {
          const isBusy = busyAction === platform.id;
          const isRelevant = !isResidentToolMode && currentPlatformSet.has(platform.id);
          return (
            <button
              key={platform.id}
              type="button"
              disabled={Boolean(busyAction)}
              onClick={() => void handleOpenPlatform(platform.id)}
              className="group relative flex h-8.5 w-full items-center gap-2 rounded-lg px-2 text-left text-gray-800 transition hover:bg-slate-900/[0.055] hover:text-gray-950 disabled:cursor-wait disabled:opacity-60"
            >
              <span
                className={`flex h-[21px] w-[21px] shrink-0 items-center justify-center rounded-full text-[9.5px] font-semibold ${
                  isRelevant ? "bg-[var(--brand-navy)] text-white" : "bg-gray-100 text-gray-600"
                }`}
              >
                {isBusy ? <Loader2 className="h-[13px] w-[13px] animate-spin" /> : platform.short}
              </span>
              <span className="min-w-0 flex-1 truncate text-[11.5px] font-medium tracking-[-0.012em]">{platform.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );

  const renderBrandPill = () => {
    if (isResidentToolMode) {
      return (
        <form
          onSubmit={handleResidentArticleSubmit}
          className="flex h-10 items-center rounded-full bg-[linear-gradient(180deg,#ffffff_0%,#fbfcfe_28%,#f8fafc_100%)] px-3 shadow-[0_30px_62px_-28px_rgba(15,23,42,0.31),0_15px_28px_-17px_rgba(15,23,42,0.21),0_7px_14px_-9px_rgba(15,23,42,0.17),0_2px_6px_rgba(15,23,42,0.08)] transition focus-within:shadow-[0_30px_62px_-28px_rgba(15,23,42,0.38),0_0_0_1px_rgba(15,23,42,0.08)]"
          style={{ width: `${BRAND_PILL_WIDTH}px` }}
        >
          <input
            value={residentArticleInput}
            onChange={(event) => setResidentArticleInput(event.target.value)}
            onFocus={() => {
              residentInputFocusedRef.current = true;
              clearCollapseTimer();
            }}
            onBlur={() => {
              residentInputFocusedRef.current = false;
            }}
            disabled={residentImporting}
            placeholder="录入文章链接"
            className="min-w-0 flex-1 bg-transparent text-[12px] font-semibold tracking-[-0.01em] text-gray-800 outline-none placeholder:text-gray-400 disabled:cursor-wait"
            title="录入文章链接"
          />
          {residentImporting ? (
            <Loader2 className="ml-2 h-[13px] w-[13px] shrink-0 animate-spin text-gray-400" />
          ) : null}
        </form>
      );
    }
    return (
      <button
        type="button"
        onClick={expandWindow}
        className="flex h-10 items-center justify-center rounded-full bg-[linear-gradient(180deg,#ffffff_0%,#fbfcfe_28%,#f8fafc_100%)] px-4 text-center shadow-[0_30px_62px_-28px_rgba(15,23,42,0.31),0_15px_28px_-17px_rgba(15,23,42,0.21),0_7px_14px_-9px_rgba(15,23,42,0.17),0_2px_6px_rgba(15,23,42,0.08)] transition hover:bg-[linear-gradient(180deg,#ffffff_0%,#fcfdff_30%,#f9fbfd_100%)]"
        style={{ width: `${BRAND_PILL_WIDTH}px` }}
        title={currentGroup?.brandName || "当前品牌"}
      >
        <span className="truncate text-[12.5px] font-semibold tracking-[-0.013em] text-gray-800">
          {currentGroup?.brandName || "未命名"}
        </span>
      </button>
    );
  };

  const renderBody = () => {
    if (loading) {
      return (
        <div
          className="rounded-[20px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.86)_0%,rgba(255,255,255,0.74)_44%,rgba(248,250,252,0.64)_100%)] px-4 py-5 text-center shadow-[0_0_0_1px_rgba(255,255,255,0.92),0_34px_70px_-28px_rgba(15,23,42,0.34),0_18px_34px_-18px_rgba(15,23,42,0.24),0_8px_16px_-9px_rgba(15,23,42,0.2),0_2px_6px_rgba(15,23,42,0.09)] backdrop-blur-2xl backdrop-saturate-150"
          style={{ width: `${PRIMARY_PANEL_WIDTH}px` }}
        >
          <div className="flex flex-col items-center gap-2">
            <Loader2 className="h-[19px] w-[19px] animate-spin text-gray-400" />
            <div className="text-[12.5px] font-medium text-gray-500">
              {resident ? "加载常驻工具中" : "加载识别任务中"}
            </div>
          </div>
        </div>
      );
    }

    if (error && !isResidentToolMode) {
      return (
        <div
          className="rounded-[20px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.86)_0%,rgba(255,255,255,0.74)_44%,rgba(248,250,252,0.64)_100%)] px-4 py-5 text-center shadow-[0_0_0_1px_rgba(255,255,255,0.92),0_34px_70px_-28px_rgba(15,23,42,0.34),0_18px_34px_-18px_rgba(15,23,42,0.24),0_8px_16px_-9px_rgba(15,23,42,0.2),0_2px_6px_rgba(15,23,42,0.09)] backdrop-blur-2xl backdrop-saturate-150"
          style={{ width: `${PRIMARY_PANEL_WIDTH}px` }}
        >
          <div className="text-[12.5px] font-medium text-gray-500">{error}</div>
          <button
            onClick={() => void loadTasks(false)}
            className="mt-3 rounded-full border border-white/70 px-3 py-1.5 text-[12.5px] font-medium text-gray-700 transition hover:bg-slate-900/[0.055] hover:text-gray-950"
          >
            重试
          </button>
        </div>
      );
    }

    if (isResidentToolMode) {
      const taskTotal = Math.max(0, residentSummary.todayTaskCount, residentSummary.completedTaskCount, residentSummary.runningTaskCount);
      const taskDone = Math.min(taskTotal, Math.max(0, residentSummary.completedTaskCount));
      return (
        <div
          className="rounded-[18px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.86)_0%,rgba(255,255,255,0.74)_44%,rgba(248,250,252,0.64)_100%)] px-2.5 py-2 shadow-[0_0_0_1px_rgba(255,255,255,0.92),0_34px_70px_-28px_rgba(15,23,42,0.34),0_18px_34px_-18px_rgba(15,23,42,0.24),0_8px_16px_-9px_rgba(15,23,42,0.2),0_2px_6px_rgba(15,23,42,0.09)] backdrop-blur-2xl backdrop-saturate-150"
          style={{ width: `${PRIMARY_PANEL_WIDTH}px` }}
        >
          <div className="space-y-1">
            <div className="flex h-8 w-full items-center justify-between gap-1.5 rounded-lg px-2 text-left text-gray-800">
              <span className="flex min-w-0 flex-1 items-center gap-2.5">
                <Search className="h-[15px] w-[15px] shrink-0 text-gray-500" />
                <span className="min-w-0 flex-1 truncate whitespace-nowrap text-[12.5px] font-semibold tracking-[-0.013em]">
                  今日文章
                </span>
              </span>
              <span className="w-9 shrink-0 text-right text-[10.5px] font-semibold tabular-nums text-gray-500">
                {residentSummary.todayArticleCount}
              </span>
            </div>

            <div className="flex h-8 w-full items-center justify-between gap-1.5 rounded-lg px-2 text-left text-gray-800">
              <span className="flex items-center gap-2.5">
                <ArrowLeftRight className="h-[15px] w-[15px] shrink-0 text-gray-500" />
                <span className="text-[12.5px] font-semibold tracking-[-0.013em]">今日任务</span>
              </span>
              <span className="w-11 shrink-0 text-right text-[10.5px] font-semibold tabular-nums text-gray-500">
                {taskDone}/{taskTotal}
              </span>
            </div>

            <div
              onMouseEnter={() => {
                openPlatformPanel();
                clearCollapseTimer();
              }}
            >
              <button
                type="button"
                onClick={() => setPlatformPanelOpen((prev) => !prev)}
                className="group relative flex h-8 w-full items-center gap-2.5 rounded-lg px-2 text-left text-gray-800 transition hover:bg-slate-900/[0.055] hover:text-gray-950"
              >
                <Globe className="h-[15px] w-[15px] shrink-0 text-gray-500" />
                <span className="text-[12.5px] font-semibold tracking-[-0.013em]">打开平台</span>
              </button>
            </div>

            <button
              type="button"
              onClick={() => void handleTriggerScreenshot()}
              disabled={busyAction === "screenshot"}
              className={`group relative flex h-8 w-full items-center gap-2.5 rounded-lg px-2 text-left transition ${
                busyAction === "screenshot"
                  ? "cursor-wait text-gray-500"
                  : "text-gray-800 hover:bg-slate-900/[0.055] hover:text-gray-950"
              }`}
            >
              {busyAction === "screenshot" ? <Loader2 className="h-[15px] w-[15px] shrink-0 animate-spin text-gray-500" /> : <Camera className="h-[15px] w-[15px] shrink-0 text-gray-500" />}
              <span className="text-[12.5px] font-semibold tracking-[-0.013em]">截图</span>
            </button>
          </div>

          {helperMessage ? (
            <div className="px-0 pt-2.5 text-[9.5px] font-medium leading-4 tracking-[0.01em] text-gray-400">
              {helperMessage}
            </div>
          ) : null}
        </div>
      );
    }

    return (
      <div
        className="rounded-[18px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.86)_0%,rgba(255,255,255,0.74)_44%,rgba(248,250,252,0.64)_100%)] px-2.5 py-2 shadow-[0_0_0_1px_rgba(255,255,255,0.92),0_34px_70px_-28px_rgba(15,23,42,0.34),0_18px_34px_-18px_rgba(15,23,42,0.24),0_8px_16px_-9px_rgba(15,23,42,0.2),0_2px_6px_rgba(15,23,42,0.09)] backdrop-blur-2xl backdrop-saturate-150"
        style={{ width: `${PRIMARY_PANEL_WIDTH}px` }}
      >
          <div className="space-y-1">
          <button
            type="button"
            onClick={() => void handleCopy()}
            disabled={!currentKeyword}
            className={`group relative flex h-8 w-full items-center justify-between gap-1.5 rounded-lg px-2 text-left transition ${
              currentKeyword
                ? "text-gray-800 hover:bg-slate-900/[0.055] hover:text-gray-950"
                : "cursor-not-allowed text-gray-300"
            }`}
          >
            <span className="flex min-w-0 flex-1 items-center gap-2.5">
              <Search className="h-[15px] w-[15px] shrink-0 text-gray-500" />
              <span className="min-w-0 flex-1 truncate whitespace-nowrap text-[12.5px] font-semibold tracking-[-0.013em]" title={currentKeyword}>
                {currentKeyword || "关键词"}
              </span>
            </span>
            <span className="relative h-4.5 w-9 shrink-0 whitespace-nowrap text-right text-[10.5px] font-semibold tabular-nums">
              <span className={`absolute inset-0 text-right text-gray-400 transition-all duration-150 ${copied ? "opacity-0" : "opacity-100 group-hover:opacity-0"}`}>
                {screenshotCount}/{screenshotTotal}
              </span>
              <span className={`absolute inset-0 text-right text-gray-700 transition-all duration-150 ${copied ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}>
                {copied ? "已复制" : "复制"}
              </span>
            </span>
          </button>

          <button
            type="button"
            onClick={(event) => void handleStepSwitch(event.shiftKey)}
            disabled={disableForward && disablePrev}
            title="点击切换到下一个，按住 Shift 点击返回上一个"
            className={`group relative flex h-8 w-full items-center justify-between gap-1.5 rounded-lg px-2 text-left transition ${
              disableForward && disablePrev
                ? "cursor-not-allowed text-gray-300"
                : "text-gray-800 hover:bg-slate-900/[0.055] hover:text-gray-950"
            }`}
          >
            <span className="flex items-center gap-2.5">
              <ArrowLeftRight className="h-[15px] w-[15px] shrink-0 text-gray-500" />
              <span className="text-[12.5px] font-semibold tracking-[-0.013em]">上下切换</span>
            </span>
            <span className="w-8 shrink-0 text-right text-[10.5px] font-semibold tabular-nums text-gray-500">
              {keywordProgressDone}/{keywordProgressTotal || totalKeywords}
            </span>
          </button>

          <div
            className=""
            onMouseEnter={() => {
              openPlatformPanel();
              clearCollapseTimer();
            }}
          >
            <button
              type="button"
              onClick={() => setPlatformPanelOpen((prev) => !prev)}
              className="group relative flex h-8 w-full items-center gap-2.5 rounded-lg px-2 text-left text-gray-800 transition hover:bg-slate-900/[0.055] hover:text-gray-950"
            >
              <Globe className="h-[15px] w-[15px] shrink-0 text-gray-500" />
              <span className="text-[12.5px] font-semibold tracking-[-0.013em]">打开平台</span>
            </button>
          </div>

          <button
            type="button"
            onClick={() => void handleTriggerScreenshot()}
            disabled={busyAction === "screenshot"}
            className={`group relative flex h-8 w-full items-center gap-2.5 rounded-lg px-2 text-left transition ${
              busyAction === "screenshot"
                ? "cursor-wait text-gray-500"
                : "text-gray-800 hover:bg-slate-900/[0.055] hover:text-gray-950"
            }`}
          >
            {busyAction === "screenshot" ? <Loader2 className="h-[15px] w-[15px] shrink-0 animate-spin text-gray-500" /> : <Camera className="h-[15px] w-[15px] shrink-0 text-gray-500" />}
            <span className="text-[12.5px] font-semibold tracking-[-0.013em]">截图</span>
          </button>
          </div>

          {helperMessage ? (
            <div className="px-0 pt-2.5 text-[9.5px] font-medium leading-4 tracking-[0.01em] text-gray-400">
              {helperMessage}
            </div>
          ) : null}
      </div>
    );
  };

  return (
    <div
      className="fixed z-[100] select-none pointer-events-none"
      style={{
        left: `${anchor.x - (EXPANDED_GROUP_WIDTH - FLOATING_BALL_SIZE)}px`,
        top: `${anchor.y}px`,
        width: `${EXPANDED_GROUP_WIDTH}px`,
        height: `${EXPANDED_GROUP_HEIGHT}px`,
      }}
    >
      <div className="relative h-full w-full">
        <div
          className="pointer-events-none absolute inset-0"
        >
        {expanded ? (
          <>
            <div
              aria-hidden="true"
              data-ocr-floating-surface="true"
              className="absolute pointer-events-auto"
              style={{
                right: `${FLOATING_BALL_SIZE}px`,
                top: "0px",
                width: `${PRIMARY_CLUSTER_GAP}px`,
                height: `${FLOATING_BALL_SIZE}px`,
              }}
              onMouseEnter={expandWindow}
              onMouseLeave={closeWindowFromSurfaceLeave}
            />
            <div
              aria-hidden="true"
              data-ocr-floating-surface="true"
              className="absolute pointer-events-auto"
              style={{
                right: "0px",
                top: `${FLOATING_BALL_SIZE}px`,
                width: `${PRIMARY_PANEL_WIDTH}px`,
                height: `${PRIMARY_CLUSTER_GAP}px`,
              }}
              onMouseEnter={expandWindow}
              onMouseLeave={closeWindowFromSurfaceLeave}
            />
          </>
        ) : null}
        {expanded && platformPanelOpen ? (
          <div
            aria-hidden="true"
            data-ocr-floating-surface="true"
            data-ocr-platform-bridge="true"
            className="absolute pointer-events-auto"
            style={{
              right: `${PRIMARY_PANEL_WIDTH}px`,
              top: `${FLOATING_BALL_SIZE + PRIMARY_CLUSTER_GAP}px`,
              width: `${SECONDARY_PANEL_GAP}px`,
              height: `${SECONDARY_PANEL_HEIGHT}px`,
            }}
            onMouseEnter={openPlatformPanel}
            onMouseLeave={closePlatformBridgeFromLeave}
          />
        ) : null}
        <div
          className={`absolute origin-top-right overflow-visible will-change-transform transition-[opacity,transform] duration-240 ease-[cubic-bezier(0.16,1,0.3,1)] ${
            expanded && platformPanelOpen ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
          }`}
          data-ocr-floating-surface="true"
          data-ocr-platform-surface="true"
          style={{
            right: `${PRIMARY_PANEL_WIDTH + SECONDARY_PANEL_GAP}px`,
            top: `${FLOATING_BALL_SIZE + PRIMARY_CLUSTER_GAP}px`,
            width: `${SECONDARY_PANEL_WIDTH}px`,
            transform: expanded && platformPanelOpen
              ? "translate3d(0, 0, 0) scale(1)"
              : `translate3d(${SECONDARY_PANEL_WIDTH + SECONDARY_PANEL_GAP}px, 0, 0) scale(0.96)`,
          }}
          onMouseEnter={openPlatformPanel}
          onMouseLeave={closePlatformPanelFromSurfaceLeave}
        >
          {renderPlatformPanel()}
        </div>
        <div
          className={`absolute origin-top-right overflow-visible will-change-transform transition-[opacity,transform] duration-240 ease-[cubic-bezier(0.16,1,0.3,1)] ${
            expanded ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
          }`}
          data-ocr-floating-surface="true"
          style={{
            right: `${FLOATING_BALL_SIZE + PRIMARY_CLUSTER_GAP}px`,
            top: "0px",
            width: `${BRAND_PILL_WIDTH}px`,
            transform: expanded
              ? "translate3d(0, 0, 0) scale(1)"
              : `translate3d(${FLOATING_BALL_SIZE + PRIMARY_CLUSTER_GAP}px, ${Math.round(FLOATING_BALL_SIZE * 0.16)}px, 0) scale(0.35)`,
          }}
          onMouseEnter={expandWindow}
          onMouseLeave={closeWindowFromSurfaceLeave}
        >
          {renderBrandPill()}
        </div>
        <div
          className={`absolute origin-top-right overflow-visible will-change-transform transition-[opacity,transform] duration-240 ease-[cubic-bezier(0.16,1,0.3,1)] ${
            expanded ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
          }`}
          data-ocr-floating-surface="true"
          style={{
            right: "0px",
            top: `${FLOATING_BALL_SIZE + PRIMARY_CLUSTER_GAP}px`,
            width: `${PRIMARY_PANEL_WIDTH}px`,
            transform: expanded
              ? "translate3d(0, 0, 0) scale(1)"
              : `translate3d(0, -${FLOATING_BALL_SIZE + PRIMARY_CLUSTER_GAP}px, 0) scale(0.32)`,
          }}
          onMouseEnter={expandWindow}
          onMouseLeave={closeWindowFromSurfaceLeave}
        >
          {renderBody()}
        </div>
        </div>

        {ballMenuOpen && !isResidentToolMode ? (
          <div
            data-ocr-floating-surface="true"
            data-ocr-ball-menu="true"
            className="pointer-events-auto absolute right-0 top-12 z-20 w-[92px] rounded-[14px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.88)_0%,rgba(255,255,255,0.76)_48%,rgba(248,250,252,0.68)_100%)] p-1 shadow-[0_24px_54px_-24px_rgba(15,23,42,0.42),0_12px_24px_-14px_rgba(15,23,42,0.28),0_3px_8px_rgba(15,23,42,0.12)] backdrop-blur-2xl backdrop-saturate-150"
            onMouseEnter={clearCollapseTimer}
            onMouseLeave={closeWindowFromSurfaceLeave}
            onContextMenu={(event) => event.preventDefault()}
          >
            <button
              type="button"
              className="block h-8 w-full rounded-[10px] px-2 text-left text-[12px] font-semibold text-gray-700 transition hover:bg-slate-900/[0.055] hover:text-gray-950"
              onClick={() => void handleDisableToday()}
            >
              今天停用
            </button>
            <button
              type="button"
              className="block h-8 w-full rounded-[10px] px-2 text-left text-[12px] font-semibold text-gray-700 transition hover:bg-slate-900/[0.055] hover:text-gray-950"
              onClick={() => void handleDisable()}
            >
              停用
            </button>
          </div>
        ) : null}

        <button
          type="button"
          onClick={handleBallClick}
          onContextMenu={handleBallContextMenu}
          onPointerDown={handleBallPointerDown}
          onPointerMove={handleBallPointerMove}
          onPointerUp={handleBallPointerUp}
          onPointerCancel={handleBallPointerUp}
          onMouseEnter={expandWindow}
          onMouseLeave={closeWindowFromSurfaceLeave}
          data-ocr-floating-surface="true"
          data-ocr-ball-trigger="true"
          className="pointer-events-auto absolute right-0 top-0 z-10 flex h-10 w-10 items-center justify-center overflow-hidden rounded-full bg-[linear-gradient(180deg,rgba(255,255,255,0.86)_0%,rgba(255,255,255,0.74)_44%,rgba(248,250,252,0.64)_100%)] shadow-[0_46px_78px_-28px_rgba(15,23,42,0.52),0_26px_42px_-20px_rgba(15,23,42,0.36),0_11px_20px_-10px_rgba(15,23,42,0.34),0_3px_8px_rgba(15,23,42,0.16)] backdrop-blur-2xl backdrop-saturate-150 transition-[box-shadow,transform] duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] cursor-grab active:scale-[0.98] active:cursor-grabbing hover:scale-[1.035] hover:shadow-[0_56px_90px_-28px_rgba(15,23,42,0.6),0_32px_50px_-20px_rgba(15,23,42,0.42),0_14px_24px_-10px_rgba(15,23,42,0.38),0_4px_10px_rgba(15,23,42,0.18)]"
          title={isResidentToolMode ? "单击展开/收起，双击最小化或恢复已打开的平台浏览器" : "单击展开/收起，双击最小化或恢复浏览器，右键停用"}
        >
          <span className="absolute inset-[-5px] translate-y-[-2px] overflow-hidden rounded-full">
            <img
              src={surfacedAppIcon}
              alt="Surfaced"
              className="h-full w-full rounded-full object-cover object-center mix-blend-multiply"
              draggable={false}
            />
          </span>
        </button>
      </div>
    </div>
  );
}
