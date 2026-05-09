import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type UIEvent } from "react";
import { createPortal } from "react-dom";
import { X, Download, Calendar, Landmark, Zap, ArrowUpRight, RefreshCw, Trash2, Pencil, Upload, Check, Undo2 } from "lucide-react";
import { Bar, BarChart, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, ResponsiveContainer } from "recharts";
import { ConfirmModal } from "./ConfirmModal";
import type { ArticleReferenceRankingDailyPoint, ArticleReferenceRankingItem, ArticleReferenceRankingPlatform, ArticleReferenceRankingResponse, ArticleSnapshot } from "../lib/backend";
import { ARTICLE_DATA_CHANGED_EVENT, confirmArticleTableImport, deleteArticle, exportTaskArticlesToWecom, fetchArticles, fetchPendingArticleTableImports, fetchSettings, fetchTaskArticleReferenceRanking, importArticlesFromFile, runAccountArticleCrawl, undoArticleTableImport, updateArticle, updateArticleMediaType } from "../lib/backend";
import type { ArticleEditValue } from "./ArticleEditModal";
import { ArticleEditModal } from "./ArticleEditModal";
import { getMediaBranding } from "../lib/mediaBranding";
import { DatePickerField } from "./ui/date-picker-field";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "./ui/context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

type Article = {
  id: string;
  source: string;
  mediaName?: string;
  accountName?: string;
  title: string;
  type: "media" | "self-media";
  date: string;
  ts: string;
  publishedAt?: string;
  url: string;
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

const ARTICLE_MENU_CONTENT_CLASS = "w-[228px] rounded-[14px] border border-gray-200/85 bg-white/98 p-1 shadow-[0_12px_28px_rgba(15,23,42,0.10)] backdrop-blur-md";
const ARTICLE_MENU_LABEL_CLASS = "px-2.5 pb-0.5 pt-1.5 text-[9px] font-bold tracking-[0.18em] uppercase text-gray-400";
const ARTICLE_MENU_INFO_CLASS = "min-h-0 cursor-default items-start rounded-[10px] px-2.5 py-1.5 text-[10px] leading-[1.45] text-gray-500 whitespace-normal focus:bg-transparent focus:text-gray-500 data-[disabled]:opacity-100";
const ARTICLE_MENU_ACTION_CLASS = "h-8 gap-1.5 rounded-[10px] px-2.5 py-0 text-[10px] font-semibold text-gray-500 focus:bg-gray-50 focus:text-gray-900";

type ExportNotice = {
  tone: "success" | "error";
  message: string;
};

type PendingArticleImport = {
  importId: string;
  fileName: string;
  addedCount: number;
  updatedCount: number;
  duplicateCount: number;
  skippedCount: number;
  message: string;
};

type MonthlyChartPoint = {
  name: string;
  value: number;
  auth: number;
  self: number;
};

const HEADER_ACTION_CLASS = "inline-flex h-8 items-center gap-1.5 px-0 text-[12px] font-bold text-gray-500 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:text-gray-300";
const FILTER_ACTION_BASE_CLASS = "flex items-center gap-1 text-[11px] font-medium transition-colors";
const FILTER_ACTION_ACTIVE_CLASS = "text-[var(--brand-navy)]";
const FILTER_ACTION_IDLE_CLASS = "text-gray-400 hover:text-gray-600";
const ARTICLE_FETCH_LIMIT = 5000;
const ARTICLE_INITIAL_RENDER_COUNT = 180;
const ARTICLE_RENDER_BATCH_SIZE = 160;

type ArticleSummaryModalProps = {
  brandName: string;
  taskId?: string;
  taskName?: string;
  scope?: "task" | "all";
  onClose: () => void;
};

function sortArticlesDesc(left: Article, right: Article) {
  const leftKey = left.ts || left.date || "";
  const rightKey = right.ts || right.date || "";
  if (leftKey !== rightKey) {
    return rightKey.localeCompare(leftKey);
  }
  return right.id.localeCompare(left.id);
}

function sortArticlesAsc(left: Article, right: Article) {
  const leftKey = left.ts || left.date || "";
  const rightKey = right.ts || right.date || "";
  if (leftKey !== rightKey) {
    return leftKey.localeCompare(rightKey);
  }
  return left.id.localeCompare(right.id);
}

function normalizeArticleDate(ts: string) {
  const raw = String(ts || "").trim();
  return raw ? raw.slice(0, 10) : "";
}

function normalizeExternalArticleHref(url?: string) {
  const raw = String(url || "").trim();
  if (!raw) {
    return "";
  }
  return /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(raw) ? raw : `https://${raw}`;
}

function openExternalArticleUrl(url?: string) {
  const href = normalizeExternalArticleHref(url);
  if (!href || typeof document === "undefined") {
    return false;
  }
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  return true;
}

function articleSnapshotToSummaryArticle(article: ArticleSnapshot): Article {
  const publishedAt = String(article.published_at || "");
  const ts = String(publishedAt || article.ts || "");
  return {
    id: String(article.id),
    source: article.source,
    mediaName: String(article.media_name || article.source || ""),
    accountName: String(article.account_name || ""),
    title: article.title,
    type: article.type as "media" | "self-media",
    date: normalizeArticleDate(ts),
    ts,
    publishedAt,
    url: String(article.url || ""),
    matchedTasks: Array.isArray(article.matchedTasks) ? article.matchedTasks : [],
    exportKeywords: Array.isArray(article.exportKeywords) ? article.exportKeywords : [],
    reasonLines: Array.isArray(article.reasonLines) ? article.reasonLines : [],
    classificationStatus: article.classificationStatus,
    classificationMessage: article.classificationMessage,
    unmatchedReason: article.unmatchedReason,
    referenced: Boolean(article.referenced),
    referencedTasks: Array.isArray(article.referencedTasks) ? article.referencedTasks : [],
    lastReferencedAt: article.lastReferencedAt,
  };
}

function formatDateSectionLabel(dateText: string) {
  const normalized = String(dateText || "").trim();
  if (!normalized) {
    return "未标注日期";
  }
  return normalized.replace(/-/g, ".");
}

function escapeCsvCell(value: string) {
  const text = String(value ?? "");
  if (/[",\n]/.test(text)) {
    return `"${text.replace(/"/g, "\"\"")}"`;
  }
  return text;
}

function formatArticleExportBrand(article: Article) {
  const names = (article.matchedTasks || [])
    .map((name) => String(name || "").trim())
    .filter(Boolean);
  return names.length ? names.join("、") : "未归类";
}

function formatArticleExportKeywords(article: Article) {
  const labels = (article.exportKeywords || [])
    .map((name) => String(name || "").trim())
    .filter(Boolean);
  return labels.length ? labels : ["未识别关键词"];
}

function mergeArticleExportKeywords(baseArticles: Article[], keywordArticles: Article[]) {
  const keywordsById = new Map<string, string[]>();
  for (const article of keywordArticles) {
    if (!article.id) {
      continue;
    }
    keywordsById.set(article.id, article.exportKeywords || []);
  }
  return baseArticles.map((article) => {
    const exportKeywords = keywordsById.get(article.id);
    if (!exportKeywords) {
      return article;
    }
    return { ...article, exportKeywords };
  });
}

function formatRankingDate(value: string) {
  const raw = String(value || "").trim();
  return raw ? raw.slice(0, 10) : "--";
}

function formatRankingPlatformLabel(platform: ArticleReferenceRankingPlatform) {
  return `${platform.label || platform.id}`;
}

function formatRankingPlatformNames(platforms: ArticleReferenceRankingPlatform[]) {
  if (!platforms.length) {
    return "未记录平台";
  }
  return platforms
    .map((platform) => formatRankingPlatformLabel(platform))
    .filter(Boolean)
    .join("、");
}

function downloadRankingCsv(
  fileBaseName: string,
  items: ArticleReferenceRankingItem[],
  dateRange: { start: string; end: string },
  platformFilterLabel: string,
) {
  const headers = [
    "排名",
    "标题",
    "来源",
    "链接",
    "平台",
    "首次引用",
    "最近引用",
    "引用范围开始",
    "引用范围结束",
    "平台筛选",
  ];
  const rows = [
    headers,
    ...items.map((item) => [
      String(item.rank || ""),
      item.article.title,
      item.article.source,
      item.article.url,
      formatRankingPlatformNames(item.platforms),
      formatRankingDate(item.first_referenced_at),
      formatRankingDate(item.last_referenced_at),
      dateRange.start || "全部",
      dateRange.end || "全部",
      platformFilterLabel,
    ]),
  ];
  const csv = `\ufeff${rows.map((row) => row.map((cell) => escapeCsvCell(cell)).join(",")).join("\n")}`;
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${fileBaseName}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadArticlesCsv(
  fileBaseName: string,
  articles: Article[],
  includeBrand = false,
  includeKeywordCategory = false,
  includeSelfMediaAccount = true,
) {
  const articleRows = includeKeywordCategory
    ? articles.flatMap((article) => formatArticleExportKeywords(article).map((keyword) => ({ article, keyword })))
    : articles.map((article) => ({ article, keyword: "" }));
  const orderedRows = articleRows.sort((left, right) => {
    if (includeKeywordCategory && left.keyword !== right.keyword) {
      return left.keyword.localeCompare(right.keyword);
    }
    return sortArticlesAsc(left.article, right.article);
  });
  const headers = [
    ...(includeKeywordCategory ? ["关键词大类"] : []),
    ...(includeBrand ? ["品牌"] : []),
    "来源",
    "类型",
    "标题",
    "发布时间",
    "链接",
  ];
  const rows = [
    headers,
    ...orderedRows.map(({ article, keyword }) => {
      const row = [
        formatArticleExportSource(article, includeSelfMediaAccount),
        article.type === "self-media" ? "自媒体" : "权威媒体",
        article.title,
        article.date,
        article.url,
      ];
      if (includeBrand) {
        row.unshift(formatArticleExportBrand(article));
      }
      if (includeKeywordCategory) {
        row.unshift(keyword || "未识别关键词");
      }
      return row;
    }),
  ];
  const csv = `\ufeff${rows.map((row) => row.map((cell) => escapeCsvCell(cell)).join(",")).join("\n")}`;
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${fileBaseName}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function formatArticleExportSource(article: Article, includeSelfMediaAccount = true) {
  const source = String(article.source || article.mediaName || "").trim();
  const accountName = String(article.accountName || "").trim();
  if (
    includeSelfMediaAccount &&
    source &&
    accountName &&
    source !== accountName &&
    !source.includes(`（${accountName}）`)
  ) {
    return `${source}（${accountName}）`;
  }
  return source;
}

function toMonthKey(dateText: string) {
  const normalized = String(dateText || "").trim();
  if (!normalized || normalized.length < 7) {
    return "";
  }
  return normalized.slice(0, 7);
}

function addMonths(monthKey: string, delta: number) {
  const [yearText, monthText] = monthKey.split("-");
  const year = Number(yearText);
  const month = Number(monthText);
  if (!Number.isFinite(year) || !Number.isFinite(month) || month < 1 || month > 12) {
    return monthKey;
  }
  const cursor = new Date(year, month - 1 + delta, 1);
  const nextYear = cursor.getFullYear();
  const nextMonth = cursor.getMonth() + 1;
  return `${nextYear}-${String(nextMonth).padStart(2, "0")}`;
}

function getMonthlyRangeKeys(startMonthKey: string, endMonthKey: string) {
  if (!startMonthKey || !endMonthKey || startMonthKey > endMonthKey) {
    return [];
  }
  const months: string[] = [];
  let cursor = startMonthKey;
  while (cursor <= endMonthKey) {
    months.push(cursor);
    const next = addMonths(cursor, 1);
    if (next === cursor) {
      break;
    }
    cursor = next;
  }
  return months;
}

function resolveChartMonthRange(articles: Article[], dateRange: { start: string; end: string }) {
  const articleMonthKeys = articles
    .map((article) => toMonthKey(article.date || article.ts || ""))
    .filter(Boolean)
    .sort();
  const firstArticleMonth = articleMonthKeys[0] || "";
  const lastArticleMonth = articleMonthKeys[articleMonthKeys.length - 1] || "";
  const startMonthKey = toMonthKey(dateRange.start) || firstArticleMonth;
  const endMonthKey = toMonthKey(dateRange.end) || lastArticleMonth || startMonthKey;
  return {
    startMonthKey,
    endMonthKey,
  };
}

function buildMonthlyChartData(articles: Article[], dateRange: { start: string; end: string }): MonthlyChartPoint[] {
  const bucket = new Map<string, MonthlyChartPoint>();
  for (const article of articles) {
    const monthKey = toMonthKey(article.date || article.ts || "");
    if (!monthKey) {
      continue;
    }
    if (!bucket.has(monthKey)) {
      bucket.set(monthKey, {
        name: monthKey.replace("-", "/"),
        value: 0,
        auth: 0,
        self: 0,
      });
    }
    const current = bucket.get(monthKey)!;
    current.value += 1;
    if (article.type === "self-media") {
      current.self += 1;
    } else {
      current.auth += 1;
    }
  }

  const { startMonthKey, endMonthKey } = resolveChartMonthRange(articles, dateRange);
  const monthKeys = getMonthlyRangeKeys(startMonthKey, endMonthKey);
  if (monthKeys.length > 0) {
    return monthKeys.map((monthKey) => (
      bucket.get(monthKey) || {
        name: monthKey.replace("-", "/"),
        value: 0,
        auth: 0,
        self: 0,
      }
    ));
  }

  return Array.from(bucket.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([, value]) => value);
}

function groupArticlesByDate(articles: Article[]) {
  const groups = new Map<string, Article[]>();
  for (const article of articles) {
    const key = article.date || "未标注日期";
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key)!.push(article);
  }
  return Array.from(groups.entries()).map(([date, items]) => ({
    date,
    label: formatDateSectionLabel(date),
    count: items.length,
    items,
  }));
}

export function ArticleSummaryModal({
  brandName,
  taskId = "",
  taskName = "",
  scope = "task",
  onClose,
}: ArticleSummaryModalProps) {
  const isAllScope = scope === "all";
  const canShowRanking = Boolean(taskId) && !isAllScope;
  const [filterType, setFilterType] = useState<"all" | "media" | "self-media">("all");
  const [dateRange, setDateRange] = useState({ start: "", end: "" });
  const [viewMode, setViewMode] = useState<"detail" | "ranking">("detail");
  const [platformFilter, setPlatformFilter] = useState("all");
  const [showExportConfirm, setShowExportConfirm] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [isCrawling, setIsCrawling] = useState(false);
  const [isImporting, setIsImporting] = useState(false);
  const [isLoadingArticles, setIsLoadingArticles] = useState(true);
  const [isLoadingRanking, setIsLoadingRanking] = useState(false);
  const [pendingImportAction, setPendingImportAction] = useState<"confirm" | "undo" | null>(null);
  const [exportNotice, setExportNotice] = useState<ExportNotice | null>(null);
  const [pendingArticleImport, setPendingArticleImport] = useState<PendingArticleImport | null>(null);
  const [articles, setArticles] = useState<Article[]>([]);
  const [rankingData, setRankingData] = useState<ArticleReferenceRankingResponse | null>(null);
  const [visibleArticleCount, setVisibleArticleCount] = useState(ARTICLE_INITIAL_RENDER_COUNT);
  const [visibleRankingCount, setVisibleRankingCount] = useState(ARTICLE_INITIAL_RENDER_COUNT);
  const [editingArticle, setEditingArticle] = useState<Article | null>(null);
  const [articleEditSaving, setArticleEditSaving] = useState(false);
  const [articleExportShowKeywordCategory, setArticleExportShowKeywordCategory] = useState(false);
  const [articleExportShowSelfMediaAccount, setArticleExportShowSelfMediaAccount] = useState(true);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const articleListRef = useRef<HTMLDivElement | null>(null);
  const platformFilterMenuRef = useRef<HTMLDivElement | null>(null);
  const [isPlatformFilterMenuOpen, setIsPlatformFilterMenuOpen] = useState(false);
  const [rankingSinglePlatformId, setRankingSinglePlatformId] = useState("");

  const isRankingMode = canShowRanking && viewMode === "ranking";
  const modalTitle = isAllScope ? "全部文章" : brandName;
  const exportFileBaseName = isAllScope
    ? "全部文章汇总"
    : isRankingMode
      ? `${brandName}-引用排名`
      : `${brandName}-文章汇总`;

  useEffect(() => {
    if (!exportNotice) {
      return;
    }
    const timer = window.setTimeout(() => setExportNotice(null), 2600);
    return () => window.clearTimeout(timer);
  }, [exportNotice]);

  useEffect(() => {
    if (!canShowRanking && viewMode === "ranking") {
      setViewMode("detail");
    }
  }, [canShowRanking, viewMode]);

  useEffect(() => {
    if (!isRankingMode) {
      setIsPlatformFilterMenuOpen(false);
    }
  }, [isRankingMode]);

  useEffect(() => {
    if (!isPlatformFilterMenuOpen) {
      return;
    }
    if (platformFilter === "all") {
      setIsPlatformFilterMenuOpen(false);
    }
  }, [isPlatformFilterMenuOpen, platformFilter]);

  useEffect(() => {
    setPlatformFilter("all");
    setRankingData(null);
  }, [taskId]);

  useEffect(() => {
    let cancelled = false;
    void fetchSettings().then((settings) => {
      if (cancelled) {
        return;
      }
      const articleExport = settings.article_export as Record<string, unknown> | undefined;
      setArticleExportShowKeywordCategory(Boolean(articleExport?.show_keyword_category));
      setArticleExportShowSelfMediaAccount(Boolean(articleExport?.show_selfmedia_account ?? true));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setIsLoadingArticles(true);
      try {
        const result = await fetchArticles({
          limit: ARTICLE_FETCH_LIMIT,
          task_name: taskName || undefined,
        });
        const mapped: Article[] = (result.articles || [])
          .map(articleSnapshotToSummaryArticle)
          .sort(sortArticlesDesc);
        if (cancelled) {
          return;
        }
        setArticles(mapped);
      } finally {
        if (!cancelled) {
          setIsLoadingArticles(false);
        }
      }
    };

    void load();
    const handleArticleDataChanged = () => {
      void load();
    };
    window.addEventListener(ARTICLE_DATA_CHANGED_EVENT, handleArticleDataChanged);
    return () => {
      cancelled = true;
      window.removeEventListener(ARTICLE_DATA_CHANGED_EVENT, handleArticleDataChanged);
    };
  }, [taskName]);

  useEffect(() => {
    if (!canShowRanking || !isRankingMode || !taskId) {
      return;
    }
    let cancelled = false;
    const loadRanking = async () => {
      setIsLoadingRanking(true);
      try {
        const result = await fetchTaskArticleReferenceRanking(taskId, {
          platform: platformFilter,
          date_from: dateRange.start,
          date_to: dateRange.end,
        });
        if (cancelled) {
          return;
        }
        setRankingData(result);
      } finally {
        if (!cancelled) {
          setIsLoadingRanking(false);
        }
      }
    };
    void loadRanking();
    return () => {
      cancelled = true;
    };
  }, [canShowRanking, dateRange.end, dateRange.start, isRankingMode, platformFilter, taskId]);

  useEffect(() => {
    if (!isAllScope) {
      return;
    }
    const loadPendingImport = async () => {
      const result = await fetchPendingArticleTableImports();
      const latest = result.batches?.[0];
      if (!result.ok || !latest?.import_id) {
        return;
      }
      setPendingArticleImport({
        importId: latest.import_id,
        fileName: latest.file_name || "表格导入",
        addedCount: Number(latest.added_count || 0),
        updatedCount: Number(latest.updated_count || 0),
        duplicateCount: Number(latest.duplicate_count || 0),
        skippedCount: Number(latest.skipped_count || 0),
        message: latest.message || "表格导入已完成，等待确认",
      });
    };
    void loadPendingImport();
  }, [isAllScope]);

  const filteredArticles = useMemo(() => (
    articles.filter((article) => {
      if (filterType !== "all" && article.type !== filterType) {
        return false;
      }
      const articleDate = String(article.date || "").slice(0, 10);
      if (dateRange.start && articleDate && articleDate < dateRange.start) {
        return false;
      }
      if (dateRange.end && articleDate && articleDate > dateRange.end) {
        return false;
      }
      return true;
    })
  ), [articles, dateRange.end, dateRange.start, filterType]);

  useEffect(() => {
    setVisibleArticleCount(ARTICLE_INITIAL_RENDER_COUNT);
    articleListRef.current?.scrollTo({ top: 0 });
  }, [articles, dateRange.end, dateRange.start, filterType, isRankingMode]);

  const visibleArticles = useMemo(
    () => filteredArticles.slice(0, visibleArticleCount),
    [filteredArticles, visibleArticleCount],
  );
  const articleCountByDate = useMemo(() => {
    const counts = new Map<string, number>();
    for (const article of filteredArticles) {
      const key = article.date || "未标注日期";
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    return counts;
  }, [filteredArticles]);
  const visibleGroupedArticles = useMemo(
    () => groupArticlesByDate(visibleArticles).map((group) => ({
      ...group,
      count: articleCountByDate.get(group.date) || group.count,
    })),
    [articleCountByDate, visibleArticles],
  );
  const allGroupedArticleCount = useMemo(
    () => articleCountByDate.size,
    [articleCountByDate],
  );
  const hasMoreVisibleArticles = visibleArticles.length < filteredArticles.length;
  const loadMoreVisibleArticles = useCallback(() => {
    setVisibleArticleCount((current) => Math.min(
      current + ARTICLE_RENDER_BATCH_SIZE,
      filteredArticles.length,
    ));
  }, [filteredArticles.length]);

  const rankingItems = useMemo(() => rankingData?.items || [], [rankingData]);
  const rankingDailyPoints = useMemo(() => rankingData?.daily_points || [], [rankingData]);
  const rankingAvailablePlatforms = useMemo(() => rankingData?.available_platforms || [], [rankingData]);
  const hasMultipleRankingPlatforms = rankingAvailablePlatforms.length > 1;
  const visibleRankingItems = useMemo(
    () => rankingItems.slice(0, visibleRankingCount),
    [rankingItems, visibleRankingCount],
  );
  const hasMoreVisibleRanking = visibleRankingItems.length < rankingItems.length;
  const loadMoreVisibleRanking = useCallback(() => {
    setVisibleRankingCount((current) => Math.min(
      current + ARTICLE_RENDER_BATCH_SIZE,
      rankingItems.length,
    ));
  }, [rankingItems.length]);
  const handleArticleListScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    const target = event.currentTarget;
    if (isRankingMode) {
      if (!hasMoreVisibleRanking) {
        return;
      }
      if (target.scrollHeight - target.scrollTop - target.clientHeight < 260) {
        loadMoreVisibleRanking();
      }
      return;
    }
    if (!hasMoreVisibleArticles) {
      return;
    }
    if (target.scrollHeight - target.scrollTop - target.clientHeight < 260) {
      loadMoreVisibleArticles();
    }
  }, [hasMoreVisibleArticles, hasMoreVisibleRanking, isRankingMode, loadMoreVisibleArticles, loadMoreVisibleRanking]);

  const rankingTotalEvents = useMemo(
    () => rankingItems.reduce((sum, item) => sum + (item.effective_event_count || 0), 0),
    [rankingItems],
  );
  const rankingCrossArticleCount = useMemo(
    () => rankingItems.filter((item) => (item.platform_count || 0) >= 2).length,
    [rankingItems],
  );
  const rankingActiveDays = useMemo(() => {
    const days = new Set<string>();
    for (const point of rankingDailyPoints) {
      if ((point.event_count || 0) > 0) {
        days.add(point.date);
      }
    }
    return days.size;
  }, [rankingDailyPoints]);
  const rankingSelectedPlatformLabel = useMemo(() => {
    if (platformFilter === "all") {
      return "全部平台";
    }
    if (platformFilter === "cross") {
      return "跨平台";
    }
    return rankingAvailablePlatforms.find((platform) => platform.id === platformFilter)?.label || platformFilter;
  }, [platformFilter, rankingAvailablePlatforms]);
  const rankingDisplayedPlatform = useMemo(() => {
    const displayId = platformFilter !== "all" && platformFilter !== "cross"
      ? platformFilter
      : rankingSinglePlatformId;
    return rankingAvailablePlatforms.find((platform) => platform.id === displayId) || rankingAvailablePlatforms[0] || null;
  }, [platformFilter, rankingAvailablePlatforms, rankingSinglePlatformId]);

  useEffect(() => {
    if (!hasMultipleRankingPlatforms) {
      setIsPlatformFilterMenuOpen(false);
    }
  }, [hasMultipleRankingPlatforms]);

  useEffect(() => {
    if (!rankingAvailablePlatforms.length) {
      setRankingSinglePlatformId("");
      return;
    }
    setRankingSinglePlatformId((current) => {
      if (current && rankingAvailablePlatforms.some((platform) => platform.id === current)) {
        return current;
      }
      return rankingAvailablePlatforms[0].id;
    });
    if (rankingAvailablePlatforms.length === 1 && platformFilter !== rankingAvailablePlatforms[0].id) {
      setPlatformFilter(rankingAvailablePlatforms[0].id);
    }
  }, [platformFilter, rankingAvailablePlatforms]);

  useEffect(() => {
    if (!isPlatformFilterMenuOpen) {
      return;
    }
    const handlePointerDown = (event: MouseEvent) => {
      if (platformFilterMenuRef.current && !platformFilterMenuRef.current.contains(event.target as Node)) {
        setIsPlatformFilterMenuOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsPlatformFilterMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isPlatformFilterMenuOpen]);
  useEffect(() => {
    setVisibleRankingCount(ARTICLE_INITIAL_RENDER_COUNT);
    if (isRankingMode) {
      articleListRef.current?.scrollTo({ top: 0 });
    }
  }, [dateRange.end, dateRange.start, isRankingMode, platformFilter, rankingData]);

  const selfMediaCount = useMemo(
    () => filteredArticles.filter((article) => article.type === "self-media").length,
    [filteredArticles],
  );
  const mediaCount = useMemo(
    () => filteredArticles.filter((article) => article.type === "media").length,
    [filteredArticles],
  );
  const detailSummaryText = filterType === "media"
    ? `${mediaCount} 篇权威媒体`
    : filterType === "self-media"
      ? `${selfMediaCount} 篇自媒体`
      : `${mediaCount} 篇权威媒体 / ${selfMediaCount} 篇自媒体`;
  const currentExportCount = isRankingMode ? rankingItems.length : filteredArticles.length;

  const exportArticles = async () => {
    if (isRankingMode) {
      if (!rankingItems.length) {
        return;
      }
      setIsExporting(true);
      try {
        downloadRankingCsv(exportFileBaseName, rankingItems, dateRange, rankingSelectedPlatformLabel);
        setExportNotice({
          tone: "success",
          message: `已导出 ${rankingItems.length} 条引用排名`,
        });
      } catch (error) {
        setExportNotice({
          tone: "error",
          message: error instanceof Error ? error.message : "导出失败",
        });
      } finally {
        setIsExporting(false);
      }
      return;
    }

    if (!filteredArticles.length) {
      return;
    }
    setIsExporting(true);
    try {
      if (!isAllScope && taskId) {
        const result = await exportTaskArticlesToWecom(taskId, {
          type: filterType === "all" ? "" : filterType,
          start: dateRange.start,
          end: dateRange.end,
        });
        setExportNotice({
          tone: result.ok ? "success" : "error",
          message: result.message || (result.ok ? "已发送到企业微信" : "发送失败"),
        });
        return;
      }

      let exportRows = filteredArticles;
      if (articleExportShowKeywordCategory) {
        const result = await fetchArticles({
          limit: ARTICLE_FETCH_LIMIT,
          task_name: taskName || undefined,
          includeExportKeywords: true,
        });
        const keywordArticles = (result.articles || []).map(articleSnapshotToSummaryArticle);
        exportRows = mergeArticleExportKeywords(filteredArticles, keywordArticles);
      }

      downloadArticlesCsv(
        exportFileBaseName,
        exportRows,
        isAllScope,
        articleExportShowKeywordCategory,
        articleExportShowSelfMediaAccount,
      );
      setExportNotice({
        tone: "success",
        message: `已导出 ${filteredArticles.length} 篇文章记录`,
      });
    } catch (error) {
      setExportNotice({
        tone: "error",
        message: error instanceof Error ? error.message : "导出失败",
      });
    } finally {
      setIsExporting(false);
    }
  };

  const crawlLatestAccountArticles = async () => {
    if (!isAllScope || isCrawling) {
      return;
    }
    setIsCrawling(true);
    setExportNotice(null);
    try {
      const result = await runAccountArticleCrawl();
      setExportNotice({
        tone: result.ok ? "success" : "error",
        message: result.message || (result.ok ? "抓取完成" : "抓取失败"),
      });
      window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
    } catch (error) {
      setExportNotice({
        tone: "error",
        message: error instanceof Error ? error.message : "抓取失败",
      });
    } finally {
      setIsCrawling(false);
    }
  };

  const handleArticleTableImport = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null;
    event.target.value = "";
    if (!file || isImporting) {
      return;
    }
    setIsImporting(true);
    setExportNotice(null);
    try {
      const result = await importArticlesFromFile(file);
      if (!result.ok || !result.import_id) {
        setExportNotice({
          tone: "error",
          message: result.message || "表格导入失败",
        });
        return;
      }
      setPendingArticleImport({
        importId: result.import_id,
        fileName: result.file_name || file.name,
        addedCount: Number(result.added_count || 0),
        updatedCount: Number(result.updated_count || 0),
        duplicateCount: Number(result.duplicate_count || 0),
        skippedCount: Number(result.skipped_count || 0),
        message: result.message || "表格导入已完成，等待确认",
      });
      window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
    } catch (error) {
      setExportNotice({
        tone: "error",
        message: error instanceof Error ? error.message : "表格导入失败",
      });
    } finally {
      setIsImporting(false);
    }
  };

  const confirmPendingArticleImport = async () => {
    if (!pendingArticleImport || pendingImportAction) {
      return;
    }
    setPendingImportAction("confirm");
    try {
      const result = await confirmArticleTableImport(pendingArticleImport.importId);
      if (!result.ok) {
        setExportNotice({ tone: "error", message: result.message || "确认失败" });
        return;
      }
      setPendingArticleImport(null);
      setExportNotice({ tone: "success", message: result.message || "已确认本次导入" });
      window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
    } finally {
      setPendingImportAction(null);
    }
  };

  const undoPendingArticleImport = async () => {
    if (!pendingArticleImport || pendingImportAction) {
      return;
    }
    setPendingImportAction("undo");
    try {
      const result = await undoArticleTableImport(pendingArticleImport.importId);
      if (!result.ok) {
        setExportNotice({ tone: "error", message: result.message || "撤销失败" });
        return;
      }
      setPendingArticleImport(null);
      setExportNotice({ tone: "success", message: result.message || "已撤销本次导入" });
      window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
    } finally {
      setPendingImportAction(null);
    }
  };

  const handleArticleMediaTypeChange = async (articleId: string, mediaType: "authority" | "selfmedia") => {
    const result = await updateArticleMediaType(articleId, mediaType);
    if (!result.ok) {
      setExportNotice({ tone: "error", message: result.message || "媒体类型更新失败" });
      return;
    }
    setExportNotice({ tone: "success", message: mediaType === "authority" ? "已设为权威媒体" : "已设为自媒体" });
    window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
  };

  const handleArticleRemove = async (articleId: string) => {
    if (!isAllScope && !taskName) {
      setExportNotice({ tone: "error", message: "缺少当前品牌信息，无法移除文章" });
      return;
    }
    const result = await deleteArticle(articleId, isAllScope ? undefined : { taskName });
    if (!result.ok) {
      setExportNotice({ tone: "error", message: result.message || "清除文章失败" });
      return;
    }
    setArticles((current) => current.filter((article) => article.id !== articleId));
    setExportNotice({
      tone: "success",
      message: result.scoped ? "已从当前品牌文章汇总移除" : "文章已清除，下次账号抓取会自动排除这个链接",
    });
    window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
  };

  const handleArticleEditConfirm = async (value: ArticleEditValue) => {
    if (!editingArticle) {
      return;
    }
    setArticleEditSaving(true);
    try {
      const result = await updateArticle(editingArticle.id, {
        media_name: value.mediaName,
        title: value.title,
        published_at: value.publishedAt,
        matched_tasks: value.taskNames,
      });
      if (!result.ok || !result.article) {
        setExportNotice({ tone: "error", message: result.message || "文章信息修改失败" });
        return;
      }
      const updatedArticle = articleSnapshotToSummaryArticle(result.article);
      setArticles((current) => (
        current
          .map((article) => (article.id === updatedArticle.id ? updatedArticle : article))
          .sort(sortArticlesDesc)
      ));
      setEditingArticle(null);
      setExportNotice({ tone: "success", message: "文章信息已修改" });
      window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
    } finally {
      setArticleEditSaving(false);
    }
  };

  if (typeof document === "undefined") {
    return null;
  }

  return createPortal(
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/18 backdrop-blur-[2px] p-3 transition-opacity animate-in fade-in duration-200 sm:p-6 lg:p-12">
        <div
          className="flex h-full min-w-0 flex-col overflow-hidden rounded-2xl border border-gray-200/80 bg-white shadow-[0_20px_60px_-18px_rgba(15,23,42,0.16)] animate-in slide-in-from-bottom-4 duration-300"
          style={{
            width: "min(880px, calc(100vw - 24px))",
            maxHeight: "min(760px, calc(100vh - 24px))",
          }}
        >
          <div className="flex shrink-0 flex-col gap-3 border-b border-gray-100 bg-white px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:px-6 sm:py-5 z-10">
            <div className="flex min-w-0 flex-col gap-1">
              <div className="flex min-w-0 items-center gap-2">
                <div className="w-2 h-4 bg-[var(--brand-cyan)] rounded-sm shrink-0"></div>
                <h2 className="text-[18px] font-black text-gray-900 tracking-tight leading-none">
                  文章汇总
                </h2>
                <span className="text-[18px] font-bold text-gray-300 leading-none">/</span>
                <span className="min-w-0 truncate text-[14px] font-bold text-gray-500 tracking-widest uppercase mt-0.5">
                  {modalTitle}
                </span>
              </div>
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 sm:justify-end">
              {isAllScope ? (
                <>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".xlsx,.xlsm,.csv"
                    className="hidden"
                    onChange={handleArticleTableImport}
                  />
                  <button
                    onClick={() => fileInputRef.current?.click()}
                    disabled={isImporting}
                    className={HEADER_ACTION_CLASS}
                  >
                    <Download className="w-3.5 h-3.5" />
                    {isImporting ? "导入中..." : "导入表格"}
                  </button>
                  <span className="hidden text-gray-200 sm:inline">/</span>
                  <button
                    onClick={() => { void crawlLatestAccountArticles(); }}
                    disabled={isCrawling}
                    className={HEADER_ACTION_CLASS}
                  >
                    <RefreshCw className={`w-3.5 h-3.5 ${isCrawling ? "animate-spin" : ""}`} />
                    {isCrawling ? "抓取中..." : "抓取账号新文章"}
                  </button>
                  <span className="hidden text-gray-200 sm:inline">/</span>
                </>
              ) : null}
              <button
                onClick={() => setShowExportConfirm(true)}
                disabled={!currentExportCount || isExporting}
                className={HEADER_ACTION_CLASS}
              >
                <Upload className="w-3.5 h-3.5" />
                {isExporting ? "导出中..." : isRankingMode ? "导出当前排名" : "导出当前数据"}
              </button>
              <span className="hidden text-gray-200 sm:inline">/</span>
              <button
                onClick={onClose}
                className={HEADER_ACTION_CLASS}
              >
                <X className="w-4 h-4" />
                关闭
              </button>
            </div>
          </div>

          {pendingArticleImport && (
            <div className="shrink-0 px-4 pt-3 sm:px-6">
              <div className="flex flex-col items-start gap-3 border-l-2 border-amber-300 bg-amber-50/55 px-3 py-2.5 animate-in fade-in slide-in-from-top-2 duration-200 sm:flex-row sm:justify-between">
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-[10px] font-bold tracking-[0.18em] uppercase text-amber-700">
                    待确认导入
                  </span>
                  <span className="text-[12px] font-medium text-gray-700">
                    {pendingArticleImport.message}
                  </span>
                  <span className="text-[11px] font-semibold text-gray-500">
                    {pendingArticleImport.fileName} · 新增 {pendingArticleImport.addedCount} 篇
                    {pendingArticleImport.updatedCount ? ` · 更新 ${pendingArticleImport.updatedCount} 篇` : ""}
                    {pendingArticleImport.duplicateCount ? ` · 重复 ${pendingArticleImport.duplicateCount} 行` : ""}
                    {pendingArticleImport.skippedCount ? ` · 跳过 ${pendingArticleImport.skippedCount} 行` : ""}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-2 self-end sm:self-auto">
                  <button
                    onClick={() => { void undoPendingArticleImport(); }}
                    disabled={Boolean(pendingImportAction)}
                    className="inline-flex h-7 items-center gap-1.5 px-2 text-[11px] font-bold text-gray-500 transition-colors hover:text-rose-600 disabled:cursor-not-allowed disabled:text-gray-300"
                  >
                    <Undo2 className={`w-3.5 h-3.5 ${pendingImportAction === "undo" ? "animate-spin" : ""}`} />
                    {pendingImportAction === "undo" ? "撤销中..." : "撤销"}
                  </button>
                  <button
                    onClick={() => { void confirmPendingArticleImport(); }}
                    disabled={Boolean(pendingImportAction)}
                    className="inline-flex h-7 items-center gap-1.5 px-2 text-[11px] font-bold text-gray-700 transition-colors hover:text-emerald-700 disabled:cursor-not-allowed disabled:text-gray-300"
                  >
                    <Check className={`w-3.5 h-3.5 ${pendingImportAction === "confirm" ? "animate-spin" : ""}`} />
                    {pendingImportAction === "confirm" ? "确认中..." : "确认"}
                  </button>
                </div>
              </div>
            </div>
          )}

          {exportNotice && (
            <div className="shrink-0 px-4 pt-3 sm:px-6">
              <div
                className={`flex min-w-0 items-start justify-between gap-3 border-l-2 px-3 py-2.5 animate-in fade-in slide-in-from-top-2 duration-200 ${
                  exportNotice.tone === "success"
                    ? "border-emerald-300 bg-emerald-50/50"
                    : "border-rose-300 bg-rose-50/55"
                }`}
              >
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className={`text-[10px] font-bold tracking-[0.18em] uppercase ${
                    exportNotice.tone === "success" ? "text-emerald-600" : "text-rose-600"
                  }`}>
                    {exportNotice.tone === "success" ? "操作成功" : "操作失败"}
                  </span>
                  <span className="text-[12px] font-medium text-gray-700">
                    {exportNotice.message}
                  </span>
                </div>
                <button
                  onClick={() => setExportNotice(null)}
                  className="shrink-0 rounded-full p-1 text-gray-400 transition-colors hover:bg-white/70 hover:text-gray-700"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>
          )}

          <div className="min-h-0 min-w-0 flex-1 overflow-hidden bg-white">
            <div className="grid h-full min-h-0 min-w-0 grid-cols-1 overflow-hidden lg:grid-cols-[284px_minmax(0,1fr)]">
              <aside className="flex min-h-0 min-w-0 flex-col px-4 py-5 sm:px-6 lg:border-r lg:border-gray-200/70">
                <section className="flex min-h-0 flex-1 flex-col">
                  <div className="mb-5 flex items-center gap-2.5">
                    <div className="h-5 w-1.5 rounded-full bg-[var(--brand-navy)]" />
                    <h3 className="text-[16px] font-black tracking-[0.08em] text-gray-900">
                      {isRankingMode ? "引用趋势" : "数据洞察"}
                    </h3>
                  </div>

                  {isRankingMode ? (
                    <>
                      <div className="mb-4 flex flex-wrap items-start justify-between gap-3 sm:gap-4">
                        <div>
                          <div className="text-[12px] font-black tracking-[0.16em] text-gray-400">引用平台</div>
                        </div>
                        <div className="flex flex-wrap items-center gap-2 pt-0.5">
                          {hasMultipleRankingPlatforms ? (
                            <>
                              <button
                                type="button"
                                onClick={() => setPlatformFilter("all")}
                                className={`${FILTER_ACTION_BASE_CLASS} ${platformFilter === "all" ? FILTER_ACTION_ACTIVE_CLASS : FILTER_ACTION_IDLE_CLASS}`}
                              >
                                全部平台
                              </button>
                              <span className="w-px h-2.5 bg-gray-200"></span>
                              <button
                                type="button"
                                onClick={() => setPlatformFilter(platformFilter === "cross" ? "all" : "cross")}
                                className={`${FILTER_ACTION_BASE_CLASS} ${platformFilter === "cross" ? FILTER_ACTION_ACTIVE_CLASS : FILTER_ACTION_IDLE_CLASS}`}
                              >
                                跨平台
                              </button>
                              <span className="w-px h-2.5 bg-gray-200"></span>
                            </>
                          ) : null}
                          <div ref={platformFilterMenuRef} className="relative shrink-0">
                            {hasMultipleRankingPlatforms ? (
                              <button
                                type="button"
                                onClick={() => {
                                  if (platformFilter !== "all") {
                                    setIsPlatformFilterMenuOpen((current) => !current);
                                  }
                                }}
                                disabled={platformFilter === "all"}
                                className={`${FILTER_ACTION_BASE_CLASS} ${
                                  platformFilter === "all" ? FILTER_ACTION_IDLE_CLASS : FILTER_ACTION_ACTIVE_CLASS
                                }`}
                                aria-haspopup="menu"
                                aria-expanded={platformFilter !== "all" && isPlatformFilterMenuOpen}
                              >
                                {rankingDisplayedPlatform?.label || "单平台"}
                                {rankingDisplayedPlatform ? (
                                  <span className="ml-0.5 text-[10px] font-bold tabular-nums text-gray-300">
                                    {rankingDisplayedPlatform.event_count || 0}
                                  </span>
                                ) : null}
                              </button>
                            ) : (
                              <span className={`${FILTER_ACTION_BASE_CLASS} ${FILTER_ACTION_ACTIVE_CLASS}`}>
                                {rankingDisplayedPlatform?.label || "单平台"}
                                {rankingDisplayedPlatform ? (
                                  <span className="ml-0.5 text-[10px] font-bold tabular-nums text-gray-300">
                                    {rankingDisplayedPlatform.event_count || 0}
                                  </span>
                                ) : null}
                              </span>
                            )}

                            {hasMultipleRankingPlatforms && platformFilter !== "all" && isPlatformFilterMenuOpen ? (
                              <div className={`${ARTICLE_MENU_CONTENT_CLASS} absolute right-0 top-full z-30 mt-2`}>
                                <div className={ARTICLE_MENU_LABEL_CLASS}>
                                  切换平台
                                </div>
                                {rankingAvailablePlatforms.map((platform) => {
                                  const isSelected = rankingDisplayedPlatform?.id === platform.id;
                                  return (
                                    <button
                                      key={platform.id}
                                      type="button"
                                      onClick={() => {
                                        setRankingSinglePlatformId(platform.id);
                                        setPlatformFilter(platform.id);
                                        setIsPlatformFilterMenuOpen(false);
                                      }}
                                      title={platform.id}
                                      className={`${ARTICLE_MENU_ACTION_CLASS} flex w-full items-center justify-between`}
                                    >
                                      <span className={`min-w-0 truncate ${isSelected ? "text-[var(--brand-navy)]" : "text-gray-700"}`}>
                                        {platform.label || platform.id}
                                      </span>
                                      <span className="shrink-0 text-[10px] font-bold tabular-nums text-gray-300">
                                        {platform.event_count || 0}
                                      </span>
                                    </button>
                                  );
                                })}
                              </div>
                            ) : null}
                          </div>
                        </div>
                      </div>

                      <div className="mb-4 grid grid-cols-2 gap-3">
                        <div className="border-b border-gray-100 pb-2">
                          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">被引文章数</div>
                          <div className="mt-1 text-[18px] font-black leading-none text-gray-900 tabular-nums">
                            {rankingItems.length}
                          </div>
                        </div>
                        <div className="border-b border-gray-100 pb-2">
                          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">总引用事件</div>
                          <div className="mt-1 text-[18px] font-black leading-none text-gray-900 tabular-nums">
                            {rankingTotalEvents}
                          </div>
                        </div>
                        <div className="border-b border-gray-100 pb-2">
                          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">跨平台文章数</div>
                          <div className="mt-1 text-[18px] font-black leading-none text-gray-900 tabular-nums">
                            {rankingCrossArticleCount}
                          </div>
                        </div>
                        <div className="border-b border-gray-100 pb-2">
                          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">活跃天数</div>
                          <div className="mt-1 text-[18px] font-black leading-none text-gray-900 tabular-nums">
                            {rankingActiveDays}
                          </div>
                        </div>
                      </div>

                      <ReferenceTrendChart
                        items={rankingItems}
                        dailyPoints={rankingDailyPoints}
                        loading={isLoadingRanking}
                        className="flex min-h-0 flex-1 flex-col"
                        chartHeight={158}
                      />

                      <div className="mt-3 border-b border-gray-200 pb-2.5 transition-colors focus-within:border-[var(--brand-cyan)]">
                        <div className="flex items-center gap-3">
                          <div className="shrink-0 text-[10px] font-bold tracking-[0.16em] text-gray-400 uppercase">
                            引用发生日
                          </div>
                          <div className="flex min-w-0 flex-1 items-center justify-end gap-1.5">
                            <DatePickerField
                              value={dateRange.start}
                              onChange={(value) => setDateRange({ ...dateRange, start: value })}
                              fromYear={2020}
                              toYear={new Date().getFullYear() + 1}
                              placeholder="开始"
                              variant="compact"
                              icon={<Calendar className="w-3 h-3 text-gray-300 shrink-0" />}
                              className="min-w-0 flex-1"
                              triggerClassName="h-7 w-full justify-end !border-0 !rounded-none !bg-transparent !px-0 !py-0 text-[12px] font-bold tracking-wide !shadow-none hover:!bg-transparent hover:!shadow-none focus:!border-0 focus:!ring-0"
                              contentClassName="shadow-[0_12px_30px_-26px_rgba(15,23,42,0.24)]"
                              showTodayShortcut={false}
                              surface="plain"
                            />
                            <span className="shrink-0 text-[11px] font-bold text-gray-300">—</span>
                            <DatePickerField
                              value={dateRange.end}
                              onChange={(value) => setDateRange({ ...dateRange, end: value })}
                              fromYear={2020}
                              toYear={new Date().getFullYear() + 1}
                              placeholder="结束"
                              variant="compact"
                              icon={<Calendar className="w-3 h-3 text-gray-300 shrink-0" />}
                              className="min-w-0 flex-1"
                              triggerClassName="h-7 w-full justify-end !border-0 !rounded-none !bg-transparent !px-0 !py-0 text-[12px] font-bold tracking-wide !shadow-none hover:!bg-transparent hover:!shadow-none focus:!border-0 focus:!ring-0"
                              contentClassName="shadow-[0_12px_30px_-26px_rgba(15,23,42,0.24)]"
                              showTodayShortcut={false}
                              surface="plain"
                            />
                          </div>
                        </div>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="mb-4 flex flex-wrap items-start justify-between gap-3 sm:gap-4">
                        <div>
                          <div className="text-[12px] font-black tracking-[0.16em] text-gray-400">发布趋势</div>
                        </div>
                        <div className="flex flex-wrap items-center gap-3 pt-0.5 sm:gap-4">
                          <button
                            onClick={() => setFilterType(filterType === "media" ? "all" : "media")}
                            className={`${FILTER_ACTION_BASE_CLASS} ${filterType === "media" ? FILTER_ACTION_ACTIVE_CLASS : FILTER_ACTION_IDLE_CLASS}`}
                          >
                            <Landmark className="w-3.5 h-3.5" /> 权威媒体
                          </button>
                          <span className="w-px h-2.5 bg-gray-200"></span>
                          <button
                            onClick={() => setFilterType(filterType === "self-media" ? "all" : "self-media")}
                            className={`${FILTER_ACTION_BASE_CLASS} ${filterType === "self-media" ? FILTER_ACTION_ACTIVE_CLASS : FILTER_ACTION_IDLE_CLASS}`}
                          >
                            <Zap className="w-3.5 h-3.5" /> 自媒体
                          </button>
                        </div>
                      </div>

                      <BrandMediaChart
                        brandName={modalTitle}
                        filterType={filterType}
                        articles={filteredArticles}
                        dateRange={dateRange}
                        className="flex min-h-0 flex-1 flex-col"
                        chartHeight={158}
                      />

                      <div className="mt-3 border-b border-gray-200 pb-2.5 transition-colors focus-within:border-[var(--brand-navy)]">
                        <div className="flex items-center gap-3">
                          <div className="shrink-0 text-[10px] font-bold tracking-[0.16em] text-gray-400 uppercase">
                            时间范围
                          </div>
                          <div className="flex min-w-0 flex-1 items-center justify-end gap-1.5">
                            <DatePickerField
                              value={dateRange.start}
                              onChange={(value) => setDateRange({ ...dateRange, start: value })}
                              fromYear={2020}
                              toYear={new Date().getFullYear() + 1}
                              placeholder="开始"
                              variant="compact"
                              icon={<Calendar className="w-3 h-3 text-gray-300 shrink-0" />}
                              className="min-w-0 flex-1"
                              triggerClassName="h-7 w-full justify-end !border-0 !rounded-none !bg-transparent !px-0 !py-0 text-[12px] font-bold tracking-wide !shadow-none hover:!bg-transparent hover:!shadow-none focus:!border-0 focus:!ring-0"
                              contentClassName="shadow-[0_12px_30px_-26px_rgba(15,23,42,0.24)]"
                              showTodayShortcut={false}
                              surface="plain"
                            />
                            <span className="shrink-0 text-[11px] font-bold text-gray-300">—</span>
                            <DatePickerField
                              value={dateRange.end}
                              onChange={(value) => setDateRange({ ...dateRange, end: value })}
                              fromYear={2020}
                              toYear={new Date().getFullYear() + 1}
                              placeholder="结束"
                              variant="compact"
                              icon={<Calendar className="w-3 h-3 text-gray-300 shrink-0" />}
                              className="min-w-0 flex-1"
                              triggerClassName="h-7 w-full justify-end !border-0 !rounded-none !bg-transparent !px-0 !py-0 text-[12px] font-bold tracking-wide !shadow-none hover:!bg-transparent hover:!shadow-none focus:!border-0 focus:!ring-0"
                              contentClassName="shadow-[0_12px_30px_-26px_rgba(15,23,42,0.24)]"
                              showTodayShortcut={false}
                              surface="plain"
                            />
                          </div>
                        </div>
                      </div>
                    </>
                  )}
                </section>
              </aside>

              <section className="flex min-h-0 min-w-0 flex-col bg-white">
                {isRankingMode ? (
                  <>
                    <div className="border-b border-gray-200/70 px-4 py-4 sm:pl-6 sm:pr-8">
                      <div className="flex flex-wrap items-start justify-between gap-3 sm:gap-4">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-baseline gap-1.5 text-[12px] font-bold tracking-[0.16em] uppercase">
                            <button
                              type="button"
                              onClick={() => setViewMode("detail")}
                              className={`transition-colors ${
                                canShowRanking ? "text-gray-300 hover:text-gray-500 cursor-pointer" : "text-gray-300 cursor-default"
                              }`}
                              disabled={!canShowRanking}
                              aria-label="切换到文章明细"
                            >
                              文章明细
                            </button>
                            <span className="font-normal text-gray-200" aria-hidden="true">/</span>
                            <span className="text-gray-700">引用排名</span>
                          </div>
                        </div>
                        <div className="shrink-0 text-[11px] font-bold text-gray-500">
                          共 {rankingItems.length} 篇 · {rankingCrossArticleCount} 篇跨平台
                        </div>
                      </div>
                    </div>

                    <div className="grid min-w-0 shrink-0 grid-cols-[32px_minmax(0,1fr)_104px_74px] gap-2 px-4 py-2 text-[10px] font-bold tracking-[0.16em] text-gray-400 uppercase sm:grid-cols-[40px_minmax(0,1fr)_132px_90px] sm:gap-3 sm:pl-6 sm:pr-8">
                      <div className="min-w-0">排名</div>
                      <div>文章标题</div>
                      <div className="text-center">平台</div>
                      <div className="w-full text-right">最近引用</div>
                    </div>

                    <div
                      ref={articleListRef}
                      onScroll={handleArticleListScroll}
                      className="min-w-0 flex-1 overflow-y-auto px-4 pb-3 custom-scrollbar bg-white sm:pl-6 sm:pr-8"
                    >
                      {isLoadingRanking && !visibleRankingItems.length ? (
                        <div className="flex h-full min-h-[240px] items-center justify-center border border-dashed border-gray-200 bg-gray-50/40 text-[13px] font-medium text-gray-400">
                          正在计算引用排名...
                        </div>
                      ) : visibleRankingItems.length > 0 ? (
                        <div className="flex flex-col -mr-[10px]">
                          {visibleRankingItems.map((item) => (
                            <RankingRow
                              key={item.article.id || `${item.rank}-${item.article.url}`}
                              item={item}
                              onOpen={() => {
                                if (!item.article.url) {
                                  return;
                                }
                                const normalized = /^https?:\/\//i.test(item.article.url) ? item.article.url : `https://${item.article.url}`;
                                window.open(normalized, "_blank", "noopener,noreferrer");
                              }}
                            />
                          ))}
                          {hasMoreVisibleRanking ? (
                            <button
                              type="button"
                              onClick={loadMoreVisibleRanking}
                              className="mt-3 inline-flex h-9 items-center justify-center border border-gray-200 bg-white text-[12px] font-bold text-gray-500 transition-colors hover:border-gray-300 hover:text-gray-900"
                            >
                              继续加载 {Math.min(ARTICLE_RENDER_BATCH_SIZE, rankingItems.length - visibleRankingItems.length)} 条
                            </button>
                          ) : null}
                        </div>
                      ) : (
                        <div className="flex h-full min-h-[240px] items-center justify-center border border-dashed border-gray-200 bg-gray-50/40 text-[13px] font-medium text-gray-400">
                          当前筛选下暂无引用排名
                        </div>
                      )}
                    </div>
                  </>
                ) : (
                  <>
                    <div className="border-b border-gray-200/70 px-4 py-4 sm:pl-6 sm:pr-8">
                      <div className="flex flex-wrap items-center justify-between gap-3 sm:flex-nowrap sm:gap-4">
                        <div className="min-w-0 flex items-baseline gap-1.5 text-[12px] font-bold tracking-[0.16em] uppercase">
                          <span className="text-gray-700">文章明细</span>
                          {canShowRanking ? (
                            <>
                              <span className="font-normal text-gray-200" aria-hidden="true">/</span>
                              <button
                                type="button"
                                onClick={() => setViewMode("ranking")}
                                className="text-gray-300 transition-colors hover:text-gray-500 cursor-pointer"
                                aria-label="切换到引用排名"
                              >
                                引用排名
                              </button>
                            </>
                          ) : null}
                        </div>
                        <div className="flex min-w-0 flex-1 items-center justify-end gap-2.5">
                          <span className="min-w-0 truncate text-[11px] font-bold text-gray-500">
                            {detailSummaryText}
                          </span>
                          <span className="h-4 w-px shrink-0 bg-gray-200" aria-hidden="true" />
                          <span className="shrink-0 text-[11px] font-bold text-gray-500">
                            共 {allGroupedArticleCount} 组时间层
                          </span>
                        </div>
                      </div>
                    </div>

                    <div className="grid min-w-0 shrink-0 grid-cols-[minmax(112px,150px)_minmax(0,1fr)_auto] gap-3 px-4 py-2 text-[10px] font-bold tracking-[0.16em] text-gray-400 uppercase sm:grid-cols-[minmax(140px,180px)_minmax(0,1fr)_100px] sm:gap-4 sm:pl-6 sm:pr-8">
                      <div className="min-w-0">媒体 / 来源</div>
                      <div>文章标题</div>
                      <div className="w-[76px] text-right sm:w-[100px]">发布时间</div>
                    </div>

                    <div
                      ref={articleListRef}
                      onScroll={handleArticleListScroll}
                      className="min-w-0 flex-1 overflow-y-auto px-4 pb-3 custom-scrollbar bg-white sm:pl-6 sm:pr-8"
                    >
                      {isLoadingArticles && !visibleGroupedArticles.length ? (
                        <div className="flex h-full min-h-[240px] items-center justify-center border border-dashed border-gray-200 bg-gray-50/40 text-[13px] font-medium text-gray-400">
                          正在加载文章记录...
                        </div>
                      ) : visibleGroupedArticles.length > 0 ? (
                        <div className="flex flex-col">
                          {visibleGroupedArticles.map((group) => (
                            <div key={group.date || group.label} className="flex flex-col">
                              <div className="sticky top-0 z-10 flex items-center justify-between bg-white/95 py-2 backdrop-blur-sm">
                                <span className="text-[11px] font-bold tracking-[0.16em] uppercase text-gray-500">
                                  {group.label}
                                </span>
                                <span className="text-[11px] font-bold text-gray-400">
                                  {group.count} 篇
                                </span>
                              </div>
                              <div className="flex flex-col">
                                {group.items.map((article) => (
                                  <ArticleRow
                                    key={article.id}
                                    article={article}
                                    onTypeClick={(type) => setFilterType(filterType === type ? "all" : type)}
                                    onSetMediaType={(mediaType) => {
                                      void handleArticleMediaTypeChange(article.id, mediaType);
                                    }}
                                    onEdit={() => setEditingArticle(article)}
                                    onClear={() => {
                                      void handleArticleRemove(article.id);
                                    }}
                                  />
                                ))}
                              </div>
                            </div>
                          ))}
                          {hasMoreVisibleArticles ? (
                            <button
                              type="button"
                              onClick={loadMoreVisibleArticles}
                              className="mt-3 inline-flex h-9 items-center justify-center border border-gray-200 bg-white text-[12px] font-bold text-gray-500 transition-colors hover:border-gray-300 hover:text-gray-900"
                            >
                              继续加载 {Math.min(ARTICLE_RENDER_BATCH_SIZE, filteredArticles.length - visibleArticles.length)} 篇
                            </button>
                          ) : null}
                        </div>
                      ) : (
                        <div className="flex h-full min-h-[240px] items-center justify-center border border-dashed border-gray-200 bg-gray-50/40 text-[13px] font-medium text-gray-400">
                          当前筛选下暂无文章记录
                        </div>
                      )}
                    </div>
                  </>
                )}
              </section>
            </div>
          </div>
        </div>
      </div>

      <ConfirmModal
        isOpen={showExportConfirm}
        onClose={() => setShowExportConfirm(false)}
        onConfirm={() => { void exportArticles(); }}
        title={
          isRankingMode
            ? "确认导出当前引用排名？"
            : isAllScope
              ? "确认导出当前筛选文章？"
              : "确认发送当前筛选文章到企业微信？"
        }
        message={
          isRankingMode
            ? `将导出当前筛选的 ${rankingItems.length} 条引用排名。平台筛选：${rankingSelectedPlatformLabel}。引用发生日范围：${dateRange.start || "全部开始日期"} 至 ${dateRange.end || "全部结束日期"}。`
            : isAllScope
              ? `将导出当前筛选的 ${filteredArticles.length} 篇文章记录。筛选范围：${dateRange.start || "全部开始日期"} 至 ${dateRange.end || "全部结束日期"}。`
              : `将生成 Excel 表格，并通过当前品牌任务配置的 webhook 发送 ${filteredArticles.length} 篇文章记录。筛选范围：${dateRange.start || "全部开始日期"} 至 ${dateRange.end || "全部结束日期"}。`
        }
        confirmText={isExporting ? "处理中..." : isRankingMode ? "确认导出" : isAllScope ? "确认导出" : "确认发送"}
        type="primary"
      />
      <ArticleEditModal
        isOpen={Boolean(editingArticle)}
        article={editingArticle ? {
          id: editingArticle.id,
          source: editingArticle.source,
          mediaName: editingArticle.mediaName,
          title: editingArticle.title,
          url: editingArticle.url,
          publishedAt: editingArticle.publishedAt || editingArticle.date,
          ts: editingArticle.ts,
          matchedTasks: editingArticle.matchedTasks,
        } : null}
        saving={articleEditSaving}
        onClose={() => {
          if (!articleEditSaving) {
            setEditingArticle(null);
          }
        }}
        onConfirm={handleArticleEditConfirm}
      />
    </>,
    document.body,
  );
}

function ArticleRow({
  article,
  onTypeClick,
  onSetMediaType,
  onEdit,
  onClear,
}: {
  article: Article;
  onTypeClick?: (type: "media" | "self-media") => void;
  onSetMediaType?: (mediaType: "authority" | "selfmedia") => void;
  onEdit?: () => void;
  onClear?: () => void;
}) {
  const isSelfMedia = article.type === "self-media";
  const isMatched = article.classificationStatus === "matched" || Boolean(article.matchedTasks && article.matchedTasks.length > 0);
  const taskSummary = article.matchedTasks && article.matchedTasks.length > 0 ? article.matchedTasks.join("、") : "";
  const summaryText = isMatched
    ? (article.classificationMessage || (taskSummary ? `已归类到 ${taskSummary}` : "已归类"))
    : (article.unmatchedReason || article.classificationMessage || "暂未归类");
  const contextLines = article.reasonLines && article.reasonLines.length > 0
    ? article.reasonLines
    : [summaryText];
  const referencedTaskText = article.referencedTasks && article.referencedTasks.length > 0 ? article.referencedTasks.join("、") : "";
  const fullContextLines = article.referenced
    ? [...contextLines, referencedTaskText ? `引用状态：已被 ${referencedTaskText} 引用` : "引用状态：已引用"]
    : contextLines;
  const branding = getMediaBranding(article.source);
  const articleHref = normalizeExternalArticleHref(article.url);

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <a
          href={articleHref || undefined}
          target={articleHref ? "_blank" : undefined}
          rel={articleHref ? "noopener noreferrer" : undefined}
          className="grid min-w-0 grid-cols-[minmax(112px,150px)_minmax(0,1fr)_auto] items-center gap-3 border-t border-gray-100 py-3 text-inherit no-underline transition-colors group cursor-pointer first:border-t-0 hover:bg-gray-50/55 sm:grid-cols-[minmax(140px,180px)_minmax(0,1fr)_100px] sm:gap-4"
          onClick={(event) => {
            if (!articleHref) {
              event.preventDefault();
            }
          }}
          title={article.url ? "左键打开文章，右键查看归类原因" : "右键查看归类原因"}
        >
          <div className="flex min-w-0 items-center gap-2 sm:gap-3">
            <div className={`w-8 h-8 rounded-lg flex items-center justify-center font-black text-[12px] tracking-tighter shrink-0 ${branding.containerClassName}`}>
              {branding.initial}
            </div>
            <div className="flex flex-col gap-0.5 min-w-0">
              <div className="flex min-w-0 items-center gap-1.5">
                <span className="min-w-0 truncate text-[13px] font-bold leading-none text-gray-900 transition-colors group-hover:text-blue-600">
                  {article.source}
                </span>
                {article.referenced ? (
                  <span className="shrink-0 rounded border border-emerald-100 bg-emerald-50 px-1.5 py-0.5 text-[9px] font-bold leading-none tracking-wider text-emerald-600">
                    已引用
                  </span>
                ) : null}
                {!isMatched ? (
                  <span className="shrink-0 rounded border border-red-100 bg-red-50 px-1.5 py-0.5 text-[9px] font-bold leading-none tracking-wider text-red-600">
                    未归类
                  </span>
                ) : null}
              </div>
              <div className="flex">
                <span
                  onClick={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    onTypeClick?.(article.type);
                  }}
                  className={`text-[9px] font-bold tracking-wider uppercase leading-none cursor-pointer hover:opacity-80 transition-opacity ${
                    isSelfMedia ? "text-blue-600" : "text-gray-500"
                  }`}
                >
                  {isSelfMedia ? "自媒体" : "媒体"}
                </span>
              </div>
            </div>
          </div>

          <div className="flex min-w-0 items-center gap-2 pr-1 sm:pr-4">
            <p className="text-[13px] text-gray-700 font-medium truncate group-hover:text-blue-600 transition-colors">
              {article.title}
            </p>
            {article.url ? <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-gray-300 transition-colors group-hover:text-blue-500" /> : null}
          </div>

          <div className="flex w-[76px] justify-end text-right sm:w-[100px]">
            <span className="text-[12px] text-gray-400 font-mono font-medium group-hover:text-gray-600 transition-colors">
              {article.date || "--"}
            </span>
          </div>
        </a>
      </ContextMenuTrigger>
      <ContextMenuContent className={ARTICLE_MENU_CONTENT_CLASS}>
        <ContextMenuLabel className={ARTICLE_MENU_LABEL_CLASS}>
          归类原因
        </ContextMenuLabel>
        {fullContextLines.map((line, index) => (
          <ContextMenuItem
            key={`${line}-${index}`}
            disabled
            className={ARTICLE_MENU_INFO_CLASS}
          >
            {line}
          </ContextMenuItem>
        ))}
        <ContextMenuSeparator className="mx-1.5 my-1 bg-gray-100/90" />
        <ContextMenuItem
          onSelect={() => onSetMediaType?.("authority")}
          disabled={!isSelfMedia}
          className={ARTICLE_MENU_ACTION_CLASS}
        >
          <Landmark className="h-3 w-3 text-gray-400" />
          设为权威媒体
        </ContextMenuItem>
        <ContextMenuItem
          onSelect={() => onSetMediaType?.("selfmedia")}
          disabled={isSelfMedia}
          className={ARTICLE_MENU_ACTION_CLASS}
        >
          <Zap className="h-3 w-3 text-gray-400" />
          设为自媒体
        </ContextMenuItem>
        <ContextMenuSeparator className="mx-1.5 my-1 bg-gray-100/90" />
        <ContextMenuItem
          onSelect={() => onEdit?.()}
          className={ARTICLE_MENU_ACTION_CLASS}
        >
          <Pencil className="h-3 w-3 text-gray-400" />
          修改
        </ContextMenuItem>
        <ContextMenuItem
          onSelect={() => onClear?.()}
          className={ARTICLE_MENU_ACTION_CLASS}
        >
          <Trash2 className="h-3 w-3 text-gray-400" />
          清除
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}

function RankingRow({
  item,
  onOpen,
}: {
  item: ArticleReferenceRankingItem;
  onOpen?: () => void;
}) {
  const platforms = item.platforms || [];
  const visiblePlatforms = platforms.slice(0, 3);
  const hiddenPlatformCount = Math.max(0, platforms.length - visiblePlatforms.length);
  const hasPlatforms = visiblePlatforms.length > 0;
  return (
    <div
      className="grid w-full min-w-0 grid-cols-[32px_minmax(0,1fr)_104px_74px] items-start gap-2 border-t border-gray-100 py-3 transition-colors group cursor-pointer first:border-t-0 hover:bg-gray-50/55 sm:grid-cols-[40px_minmax(0,1fr)_132px_90px] sm:gap-3"
      onClick={() => {
        if (!openExternalArticleUrl(item.article.url)) {
          onOpen?.();
        }
      }}
      title={item.article.url ? "左键打开文章链接" : "暂无链接"}
    >
      <div className="flex items-start pt-0.5">
        <span className="text-[12px] font-black leading-none tracking-tight text-gray-400 tabular-nums">
          {item.rank}
        </span>
      </div>

      <div className="flex min-w-0 flex-col gap-0.5">
        <div className="min-w-0 truncate text-[13px] font-bold leading-tight text-gray-900 transition-colors group-hover:text-blue-600">
          {item.article.title}
        </div>
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="min-w-0 truncate text-[10px] font-medium text-gray-500">
            {item.article.source}
          </span>
          <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-bold leading-none tracking-wider ${
            item.article.type === "media"
              ? "border-sky-100 bg-sky-50 text-sky-600"
              : "border-cyan-100 bg-cyan-50 text-cyan-600"
          }`}>
            {item.article.type === "media" ? "权威" : "自媒体"}
          </span>
        </div>
      </div>

      <div className="flex w-[104px] min-w-0 justify-center justify-self-center pt-0.5 sm:w-[132px]">
        {hasPlatforms ? (
          <div className="flex min-w-0 flex-wrap items-center justify-center gap-1">
            {visiblePlatforms.map((platform) => (
              <span
                key={`${item.article.id}-${platform.id}`}
                title={platform.id}
                className="inline-flex max-w-full items-center rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[9px] font-bold leading-none tracking-wider text-gray-500"
              >
                <span className="truncate">{platform.label}</span>
              </span>
            ))}
            {hiddenPlatformCount > 0 ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    aria-label={`查看全部 ${platforms.length} 个平台`}
                    title={platforms.map((platform) => `${platform.label} (${platform.id})`).join(" · ")}
                    onClick={(event) => event.stopPropagation()}
                    className="inline-flex items-center rounded border border-gray-200 bg-white px-1.5 py-0.5 text-[9px] font-bold leading-none tracking-wider text-gray-400 transition-colors hover:border-gray-300 hover:text-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-200"
                  >
                    +{hiddenPlatformCount}
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  align="end"
                  sideOffset={6}
                  className="z-[80] w-[152px] rounded-[12px] border-gray-200/90 bg-white p-1 shadow-[0_14px_30px_rgba(15,23,42,0.12)]"
                  onClick={(event) => event.stopPropagation()}
                >
                  <DropdownMenuLabel className="px-2 py-1 text-[9px] font-bold tracking-[0.16em] text-gray-400">
                    全部平台
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator className="my-1 bg-gray-100" />
                  {platforms.map((platform) => (
                    <DropdownMenuItem
                      key={`${item.article.id}-${platform.id}-menu`}
                      title={platform.id}
                      className="h-7 rounded-[8px] px-2 text-[11px] font-semibold text-gray-600 focus:bg-gray-50 focus:text-gray-900"
                    >
                      <span className="min-w-0 truncate">{formatRankingPlatformLabel(platform)}</span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
          </div>
        ) : (
          <span
            title="仅保留了已引用标记，未记录具体平台"
            className="inline-flex items-center rounded border border-gray-200 bg-white px-1.5 py-0.5 text-[9px] font-bold leading-none tracking-wider text-gray-400"
          >
            未记录平台
          </span>
        )}
      </div>

      <div className="flex w-[74px] justify-end justify-self-end pt-0.5 text-right text-[11px] font-medium text-gray-500 tabular-nums sm:w-[90px]">
        {formatRankingDate(item.last_referenced_at)}
      </div>
    </div>
  );
}

function ReferenceTrendChart({
  items,
  dailyPoints,
  loading,
  className,
  chartHeight = 140,
}: {
  items: ArticleReferenceRankingItem[];
  dailyPoints: ArticleReferenceRankingDailyPoint[];
  loading?: boolean;
  className?: string;
  chartHeight?: number;
}) {
  const chartData = useMemo(
    () => dailyPoints.map((point) => ({
      name: point.date.replace(/-/g, "/"),
      value: point.article_count,
      eventCount: point.event_count,
      rawCount: point.raw_count,
    })),
    [dailyPoints],
  );
  const total = useMemo(
    () => chartData.reduce((sum, item) => sum + (item.value || 0), 0),
    [chartData],
  );
  const values = chartData.map((item) => Number(item.value || 0));
  const nonZeroValues = values.filter((value) => value > 0);
  const peak = nonZeroValues.length ? Math.max(...nonZeroValues) : 0;
  const avg = nonZeroValues.length ? Math.round(total / nonZeroValues.length) : 0;

  return (
    <div className={className || "px-6 py-5 border-b border-gray-100 shrink-0 bg-white"}>
      <div className="grid grid-cols-3 gap-3 border-b border-gray-100 pb-3">
        <div>
          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">当前</div>
          <div className="mt-1 flex items-end gap-0.5">
            <span className="text-[24px] font-black leading-none tracking-[-0.04em] text-gray-900 tabular-nums">
              {items.length.toLocaleString()}
            </span>
            <span className="mb-0.5 text-[11px] font-bold text-gray-500">篇</span>
          </div>
          <div className="mt-1 text-[10px] font-medium text-gray-400">
            日累计 {total.toLocaleString()} 次
          </div>
        </div>
        <div className="border-l border-gray-100 pl-3">
          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">峰值</div>
          <div className="mt-1 text-[18px] font-black leading-none text-gray-900">
            {peak}
            <span className="ml-0.5 text-[11px] font-bold text-gray-500">篇</span>
          </div>
        </div>
        <div className="border-l border-gray-100 pl-3">
          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">均值</div>
          <div className="mt-1 text-[18px] font-black leading-none text-gray-900">
            {avg}
            <span className="ml-0.5 text-[11px] font-bold text-gray-500">篇</span>
          </div>
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-1.5 text-[10px] font-bold text-gray-400">
          <span>按日被引用文章数</span>
          <ArrowUpRight className="h-3 w-3" />
          <span>{chartData.length} 个时间点</span>
        </div>
        <div className="flex items-center gap-3 text-[10px] font-bold text-gray-500">
          <div className="flex items-center gap-1.5">
            <div className="h-1.5 w-1.5 rounded-[2px] bg-[var(--brand-cyan)]"></div>
            文章数
          </div>
        </div>
      </div>

      <div className="w-full min-h-0 flex-1 -ml-3.5 -mr-1 pb-1" style={{ minHeight: `${chartHeight}px` }}>
        {loading && !chartData.length ? (
          <div className="flex h-full min-h-[140px] items-center justify-center text-[13px] font-medium text-gray-400">
            正在加载引用数据...
          </div>
        ) : chartData.length > 0 ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData} margin={{ top: 14, right: 10, left: -12, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f3f4f6" />
              <XAxis
                dataKey="name"
                axisLine={false}
                tickLine={false}
                tick={{ fontSize: 10, fill: "#9ca3af" }}
                interval={0}
                angle={-38}
                textAnchor="end"
                dy={7}
                height={44}
              />
              <YAxis
                axisLine={false}
                tickLine={false}
                tick={{ fontSize: 10, fill: "#9ca3af" }}
                dx={-5}
              />
              <RechartsTooltip
                cursor={{ fill: "#f8fafc" }}
                contentStyle={{ borderRadius: "8px", border: "none", boxShadow: "0 4px 12px rgba(0,0,0,0.05)", fontSize: "11px" }}
              />
              <Bar dataKey="value" name="被引文章数" fill="var(--brand-cyan)" radius={[4, 4, 0, 0]} maxBarSize={18} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full min-h-[140px] items-center justify-center text-[13px] font-medium text-gray-400">
            当前筛选下暂无引用数据
          </div>
        )}
      </div>
    </div>
  );
}

function BrandMediaChart({
  brandName,
  filterType,
  articles,
  dateRange,
  className,
  chartHeight = 140,
}: {
  brandName: string;
  filterType: "all" | "media" | "self-media";
  articles: Article[];
  dateRange: { start: string; end: string };
  className?: string;
  chartHeight?: number;
}) {
  const chartData = useMemo(
    () => buildMonthlyChartData(articles, dateRange),
    [articles, dateRange],
  );

  const metricKey = filterType === "media" ? "auth" : filterType === "self-media" ? "self" : "value";
  const metricLabel = filterType === "media" ? "权威媒体" : filterType === "self-media" ? "自媒体" : "全部文章";
  const data = chartData.map((item) => {
    const selected = Number(item[metricKey] || 0);
    return {
      ...item,
      selected,
    };
  });
  const values = data.map((item) => Number(item.selected || 0));
  const total = values.reduce((sum, value) => sum + value, 0);
  const nonZeroValues = values.filter((value) => value > 0);
  const peak = nonZeroValues.length ? Math.max(...nonZeroValues) : 0;
  const avg = nonZeroValues.length ? Math.round(total / nonZeroValues.length) : 0;

  return (
    <div className={className || "px-6 py-5 border-b border-gray-100 shrink-0 bg-white"}>
      <div className="grid grid-cols-3 gap-3 border-b border-gray-100 pb-3">
        <div>
          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">当前</div>
          <div className="mt-1 flex items-end gap-0.5">
            <span className="text-[24px] font-black leading-none tracking-[-0.04em] text-gray-900 tabular-nums">
              {total.toLocaleString()}
            </span>
            <span className="mb-0.5 text-[11px] font-bold text-gray-500">篇</span>
          </div>
        </div>
        <div className="border-l border-gray-100 pl-3">
          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">峰值</div>
          <div className="mt-1 text-[18px] font-black leading-none text-gray-900">{peak}<span className="ml-0.5 text-[11px] font-bold text-gray-500">篇</span></div>
        </div>
        <div className="border-l border-gray-100 pl-3">
          <div className="text-[9px] font-bold tracking-[0.14em] text-gray-400">均值</div>
          <div className="mt-1 text-[18px] font-black leading-none text-gray-900">{avg}<span className="ml-0.5 text-[11px] font-bold text-gray-500">篇</span></div>
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-1.5 text-[10px] font-bold text-gray-400">
          <span>{metricLabel}</span>
          <ArrowUpRight className="h-3 w-3" />
          <span>{data.length} 个时间点</span>
        </div>
        <div className="flex items-center gap-3 text-[10px] font-bold text-gray-500">
          <div className="flex items-center gap-1.5">
            <div className="h-1.5 w-1.5 rounded-[2px] bg-[var(--brand-navy)]"></div>
            权威
          </div>
          <div className="flex items-center gap-1.5">
            <div className="h-1.5 w-1.5 rounded-[2px] bg-[var(--brand-cyan)]"></div>
            自媒体
          </div>
        </div>
      </div>

      <div className="w-full min-h-0 flex-1 -ml-3.5 -mr-1 pb-1" title={brandName} style={{ minHeight: `${chartHeight}px` }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 14, right: 10, left: -12, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f3f4f6" />
            <XAxis
              dataKey="name"
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 10, fill: "#9ca3af" }}
              interval={0}
              angle={-38}
              textAnchor="end"
              dy={7}
              height={44}
            />
            <YAxis
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 10, fill: "#9ca3af" }}
              dx={-5}
            />
            <RechartsTooltip
              cursor={{ fill: "#f8fafc" }}
              contentStyle={{ borderRadius: "8px", border: "none", boxShadow: "0 4px 12px rgba(0,0,0,0.05)", fontSize: "11px" }}
            />
            <Bar hide={filterType !== "all" && filterType !== "media"} dataKey="auth" name="权威媒体" fill="var(--brand-navy)" radius={[4, 4, 0, 0]} maxBarSize={18} isAnimationActive={false} />
            <Bar hide={filterType !== "all" && filterType !== "self-media"} dataKey="self" name="自媒体" fill="var(--brand-cyan)" radius={[4, 4, 0, 0]} maxBarSize={18} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
