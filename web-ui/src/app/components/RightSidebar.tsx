import { lazy, Suspense, useEffect, useState, useRef } from "react";
import { Plus, Check, Link2, ArrowRight, Zap, Landmark, Trash2, Power, Loader2, Pencil } from "lucide-react";
import type { ArticleSnapshot, MonitoringSnapshot, TodoSnapshot } from "../lib/backend";
import { ARTICLE_DATA_CHANGED_EVENT, areArticlesEqual, syncTodos, importArticle, deleteArticle, fetchArticles, updateArticle, updateArticleMediaType, writeTodoCache } from "../lib/backend";
import type { ArticleEditValue } from "./ArticleEditModal";
import { ArticleEditModal } from "./ArticleEditModal";
import { getMediaBranding } from "../lib/mediaBranding";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "./ui/context-menu";

const ArticleSummaryModal = lazy(() => import("./ArticleSummaryModal").then((module) => ({ default: module.ArticleSummaryModal })));

const FALLBACK_ARTICLES: ArticleSnapshot[] = [
  { id: 1, source: "TechCrunch", title: "2026年用户体验设计的十大趋势解析...", type: "media" },
  { id: 2, source: "少数派", title: "设计系统从0到1：如何建立可持续的...", type: "self-media" },
  { id: 3, source: "UX Collective", title: "The psychology behind great onbo...", type: "self-media" },
  { id: 4, source: "36氪", title: "AI 辅助设计工具的崛起与设计师的未...", type: "media" },
  { id: 5, source: "Medium", title: "Building minimalist interfaces tha...", type: "self-media" },
];

const FALLBACK_TODOS: TodoSnapshot[] = [
  { id: 1, text: "审查最新提交的 API 数据格式", done: true },
  { id: 2, text: "更新系统检测模块至 v2.4", done: true },
  { id: 3, text: "修复首页图表的高度 Bug", done: false },
  { id: 4, text: "同步设计规范到前端代码库", done: false },
];

const TODO_LIST_VIEWPORT_CLASS = "h-[82px] overflow-y-auto pr-2 -mr-2 pb-1 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent";
const DELETE_MENU_CONTENT_CLASS = "w-[96px] rounded-xl border border-gray-200/80 bg-white/95 p-1 shadow-[0_10px_30px_rgba(15,23,42,0.10)] backdrop-blur-sm";
const DELETE_MENU_ITEM_CLASS = "h-8 gap-1.5 rounded-lg px-2.5 py-0 text-[11px] font-semibold text-gray-600 focus:bg-gray-50 focus:text-gray-900";
const ARTICLE_MENU_CONTENT_CLASS = "w-[228px] rounded-[14px] border border-gray-200/85 bg-white/98 p-1 shadow-[0_12px_28px_rgba(15,23,42,0.10)] backdrop-blur-md";
const ARTICLE_MENU_LABEL_CLASS = "px-2.5 pb-0.5 pt-1.5 text-[9px] font-bold tracking-[0.18em] uppercase text-gray-400";
const ARTICLE_MENU_INFO_CLASS = "min-h-0 cursor-default items-start rounded-[10px] px-2.5 py-1.5 text-[10px] leading-[1.45] text-gray-500 whitespace-normal focus:bg-transparent focus:text-gray-500 data-[disabled]:opacity-100";
const ARTICLE_MENU_DELETE_CLASS = "h-8 gap-1.5 rounded-[10px] px-2.5 py-0 text-[10px] font-semibold text-gray-500 focus:bg-gray-50 focus:text-gray-900";
const DUPLICATE_ARTICLE_MESSAGE = "链接已录入";

function toCount(value: unknown): number {
  const count = Number(value);
  return Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
}

function normalizeArticleUrl(url: string): string {
  const raw = String(url || "").trim();
  if (!raw) return "";

  const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(raw) ? raw : `https://${raw}`;
  try {
    const parsed = new URL(withScheme);
    const hostname = parsed.hostname.toLowerCase().replace(/^www\./, "");
    const pathname = parsed.pathname !== "/" ? parsed.pathname.replace(/\/+$/, "") : "";
    const params = new URLSearchParams(parsed.search);
    const filtered = new URLSearchParams();
    Array.from(params.entries())
      .filter(([key]) => {
        const normalized = key.trim().toLowerCase();
        return normalized && !normalized.startsWith("utm_") && !["fbclid", "gclid", "igshid", "mkt_tok", "mc_cid", "mc_eid", "spm"].includes(normalized);
      })
      .sort(([left], [right]) => left.localeCompare(right))
      .forEach(([key, value]) => filtered.append(key, value));
    const query = filtered.toString();
    return `${parsed.protocol.toLowerCase()}//${hostname}${pathname}${query ? `?${query}` : ""}`;
  } catch {
    return raw;
  }
}

function normalizeArticleHref(url?: string): string {
  const raw = String(url || "").trim();
  if (!raw) return "";
  return /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(raw) ? raw : `https://${raw}`;
}

export function RightSidebar({
  monitoring,
  cloudRole = "",
  todos,
  articles,
  stats,
  todoCacheIdentity = "",
  onMonitoringToggle,
  onTodosChange,
  onArticlesChange,
}: {
  monitoring?: MonitoringSnapshot;
  cloudRole?: string;
  todos?: TodoSnapshot[];
  articles?: ArticleSnapshot[];
  stats?: {
    enabledTasks: number;
    totalTasks: number;
    todayRecords: number;
    hitRecords: number;
    errorRecords: number;
  };
  todoCacheIdentity?: string;
  onMonitoringToggle?: (enabled: boolean) => void | Promise<void>;
  onTodosChange?: (todos: TodoSnapshot[]) => void;
  onArticlesChange?: (articles: ArticleSnapshot[]) => void;
}) {
  const [filterType, setFilterType] = useState<'all' | 'media' | 'self-media'>('all');
  const [todoInput, setTodoInput] = useState('');
  const [articleInput, setArticleInput] = useState('');
  const [articleLoading, setArticleLoading] = useState(false);
  const [articleMessage, setArticleMessage] = useState('');
  const [articleMessageTone, setArticleMessageTone] = useState<'default' | 'error' | 'warning'>('default');
  const [todoMessage, setTodoMessage] = useState('');
  const [toggleLoading, setToggleLoading] = useState(false);
  const [articleTodayCount, setArticleTodayCount] = useState(0);
  const [articleTotalCount, setArticleTotalCount] = useState(0);
  const [articleStatsLoaded, setArticleStatsLoaded] = useState(false);
  const [showAllArticlesModal, setShowAllArticlesModal] = useState(false);
  const [editingArticle, setEditingArticle] = useState<ArticleSnapshot | null>(null);
  const [articleEditSaving, setArticleEditSaving] = useState(false);
  const latestArticlesRef = useRef<ArticleSnapshot[]>(articles ?? FALLBACK_ARTICLES);
  const todoInputRef = useRef<HTMLInputElement>(null);
  const articleInputRef = useRef<HTMLInputElement>(null);
  const articleItems = articles ?? FALLBACK_ARTICLES;
  const filteredArticles = articleItems.filter(a => filterType === 'all' || a.type === filterType);
  const todoItems = todos ?? FALLBACK_TODOS;

  useEffect(() => {
    latestArticlesRef.current = articleItems;
  }, [articleItems]);

  useEffect(() => {
    let cancelled = false;
    const syncLatestArticles = async () => {
      const result = await fetchArticles({ limit: 20 });
      if (cancelled) {
        return;
      }
      if (result.articles && result.articles.length >= 0) {
        setArticleTotalCount(toCount(result.total));
        setArticleTodayCount(toCount(result.today_total));
        setArticleStatsLoaded(true);
        if (!areArticlesEqual(latestArticlesRef.current, result.articles)) {
          onArticlesChange?.(result.articles);
        }
      }
    };

    void syncLatestArticles();
    const handleArticleDataChanged = () => {
      void syncLatestArticles();
    };

    window.addEventListener(ARTICLE_DATA_CHANGED_EVENT, handleArticleDataChanged);
    return () => {
      cancelled = true;
      window.removeEventListener(ARTICLE_DATA_CHANGED_EVENT, handleArticleDataChanged);
    };
  }, [onArticlesChange]);

  const persistTodos = async (updated: TodoSnapshot[]) => {
    writeTodoCache(updated, true, todoCacheIdentity);
    onTodosChange?.(updated);
    const result = await syncTodos(updated);
    if (result.ok) {
      const syncedTodos = result.todos ?? updated;
      writeTodoCache(syncedTodos, false, todoCacheIdentity);
      onTodosChange?.(syncedTodos);
      setTodoMessage('');
      return;
    }
    setTodoMessage(result.message || '待办暂未同步到本地配置，已保留当前勾选状态');
  };

  const handleTodoToggle = async (index: number) => {
    const updated = todoItems.map((item, i) =>
      i === index
        ? {
            ...item,
            done: !item.done,
            completedAt: !item.done ? new Date().toISOString() : undefined,
          }
        : item
    );
    await persistTodos(updated);
  };

  const handleTodoAdd = async () => {
    const text = todoInput.trim();
    if (!text) return;
    const newTodo: TodoSnapshot = {
      id: Date.now(),
      text,
      done: false,
    };
    const updated = [...todoItems, newTodo];
    setTodoInput('');
    await persistTodos(updated);
  };

  const handleTodoRemove = async (index: number) => {
    const updated = todoItems.filter((_, i) => i !== index);
    await persistTodos(updated);
  };

  const handleArticleImport = async () => {
    const url = articleInput.trim();
    if (!url) return;
    setArticleLoading(true);
    try {
      const result = await importArticle(url);
      if (result.article) {
        const nextArticle: ArticleSnapshot = {
          id: result.article.id ?? Date.now(),
          source: result.article.source ?? new URL(url).hostname,
          title: result.article.title ?? url,
          type: result.article.type ?? 'self-media',
          url: result.article.url ?? url,
          platform: result.article.platform,
          media_name: result.article.media_name,
          account_id: result.article.account_id,
          account_name: result.article.account_name,
          account_url: result.article.account_url,
          account_platform: result.article.account_platform,
          account_platform_label: result.article.account_platform_label,
          ts: result.article.ts,
          published_at: result.article.published_at,
          imported_at: result.article.imported_at,
          fetch_method: result.article.fetch_method,
          matchedTasks: result.article.matchedTasks ?? [],
          reasonLines: result.article.reasonLines ?? [],
          classificationStatus: result.article.classificationStatus ?? 'unmatched',
          classificationMessage: result.article.classificationMessage,
          unmatchedReason: result.article.unmatchedReason,
          referenced: result.article.referenced,
          referencedTasks: result.article.referencedTasks ?? [],
          lastReferencedAt: result.article.lastReferencedAt,
        };
        const currentArticles = articles ?? FALLBACK_ARTICLES;
        const normalizedUrl = normalizeArticleUrl(nextArticle.url || url);
        const existingIndex = currentArticles.findIndex((article) => {
          const sameId = String(article.id) === String(nextArticle.id);
          const sameUrl = normalizedUrl && normalizeArticleUrl(article.url || "") === normalizedUrl;
          return sameId || sameUrl;
        });
        const updatedArticles = [...currentArticles];
        if (existingIndex >= 0) {
          updatedArticles.splice(existingIndex, 1, nextArticle);
        } else {
          updatedArticles.unshift(nextArticle);
        }
        if (result.ok && !result.duplicate && existingIndex < 0) {
          setArticleTotalCount((prev) => prev + 1);
          setArticleTodayCount((prev) => prev + 1);
          setArticleStatsLoaded(true);
        }
        onArticlesChange?.(updatedArticles);
        window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
        setArticleMessage(result.duplicate ? DUPLICATE_ARTICLE_MESSAGE : '');
        setArticleMessageTone('default');
        setArticleInput('');
        if (result.ok || result.duplicate) {
          return;
        }
      }
      setArticleMessage(result.message || "录入失败");
      setArticleMessageTone(result.duplicate ? 'default' : 'error');
    } catch {
      setArticleMessage("录入失败");
      setArticleMessageTone('error');
    } finally {
      setArticleLoading(false);
    }
  };

  const handleArticleRemove = async (articleId: string | number) => {
    const updated = articleItems.filter((article) => article.id !== articleId);
    onArticlesChange?.(updated);
    const result = await deleteArticle(articleId);
    if (!result.ok) {
      onArticlesChange?.(articleItems);
      return;
    }
    setArticleMessage("文章已清除，下次账号抓取会自动排除这个链接");
    setArticleMessageTone("default");
    window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
  };

  const handleArticleMediaTypeChange = async (
    articleId: string | number,
    mediaType: "authority" | "selfmedia",
  ) => {
    const result = await updateArticleMediaType(articleId, mediaType);
    if (!result.ok) {
      setArticleMessage(result.message || "媒体类型更新失败");
      setArticleMessageTone("error");
      return;
    }
    setArticleMessage(
      mediaType === "authority"
        ? "已设为权威媒体，并同步同主域名站点文章"
        : "已设为自媒体，并同步同主域名站点文章",
    );
    setArticleMessageTone("default");
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
        setArticleMessage(result.message || "文章信息修改失败");
        setArticleMessageTone("error");
        return;
      }
      const updatedArticles = articleItems.map((article) => (
        String(article.id) === String(result.article?.id)
          ? { ...article, ...result.article }
          : article
      ));
      onArticlesChange?.(updatedArticles);
      setEditingArticle(null);
      setArticleMessage("文章信息已修改");
      setArticleMessageTone("default");
      window.dispatchEvent(new CustomEvent(ARTICLE_DATA_CHANGED_EVENT));
    } finally {
      setArticleEditSaving(false);
    }
  };

  const handleArticleOpen = (url?: string) => {
    const normalized = normalizeArticleHref(url);
    if (!normalized) return;
    window.open(normalized, "_blank", "noopener,noreferrer");
  };

  const monitoringEnabled = Boolean(monitoring?.enabled);
  const monitoringRunning = Boolean(monitoring?.running);
  const isViewerAccount = cloudRole === "viewer";
  const handleMonitoringClick = async () => {
    if (toggleLoading || isViewerAccount) {
      return;
    }
    setToggleLoading(true);
    try {
      await onMonitoringToggle?.(!monitoringEnabled);
    } finally {
      setToggleLoading(false);
    }
  };

  return (
    <div className="w-[280px] xl:w-[320px] h-full bg-transparent border-l border-gray-200/80 px-5 py-3 xl:px-6 xl:py-4 flex flex-col shrink-0">
      <div className="flex flex-col shrink-0 mb-4 pb-4 border-b border-gray-200/70">
        <div className="mb-4 flex items-center justify-between gap-3">
          <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">自动调度</h3>
          <span
            className={`inline-flex shrink-0 items-center gap-1.5 text-[10px] font-semibold ${
              monitoringRunning
                ? "text-emerald-700"
                : monitoringEnabled
                  ? "text-sky-700"
                  : "text-gray-500"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                monitoringRunning
                  ? "bg-emerald-500"
                  : monitoringEnabled
                    ? "bg-sky-500"
                    : "bg-gray-400"
              }`}
            />
            {monitoringRunning ? "运行中" : monitoringEnabled ? "已开启" : "已关闭"}
          </span>
        </div>
        <button
          type="button"
          onClick={() => {
            void handleMonitoringClick();
          }}
          disabled={toggleLoading || isViewerAccount}
          className={`inline-flex w-full items-center justify-center gap-2 rounded-[12px] px-3 py-2.5 text-[12px] font-semibold transition-all ${
            isViewerAccount
              ? "bg-gray-100 text-gray-400"
              : monitoringEnabled
              ? "bg-emerald-50 text-emerald-700 hover:bg-emerald-100/80"
              : "bg-gray-100 text-gray-700 hover:bg-gray-200/80"
          } disabled:cursor-not-allowed disabled:opacity-80`}
        >
          {toggleLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Power className="h-3.5 w-3.5" />}
          {isViewerAccount ? "浏览账号仅查看" : toggleLoading ? "状态切换中..." : monitoringEnabled ? "关闭定时任务" : "开启定时任务"}
        </button>
      </div>

      {/* 5. 任务新增勾选 */}
      <div className="flex flex-col shrink-0">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">快速待办</h3>
          <span className="text-[10px] font-bold text-gray-500 bg-gray-100/80 px-2 py-0.5 rounded-sm">
            {Math.min(todoItems.filter((item) => item.done).length, todoItems.length)} / {todoItems.length}
          </span>
        </div>

        <div className="relative mb-4">
          <input
            ref={todoInputRef}
            type="text"
            placeholder="输入新任务，回车设置代办..."
            value={todoInput}
            onChange={(e) => setTodoInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                void handleTodoAdd();
              }
            }}
            className="w-full pb-2 border-b border-gray-200/80 bg-transparent text-[13px] text-gray-900 placeholder-gray-400 focus:outline-none focus:border-blue-500 transition-colors"
          />
          <button
            onClick={() => {
              void handleTodoAdd();
            }}
            className="absolute right-0 top-0 w-5 h-5 flex items-center justify-center text-gray-400 hover:text-blue-600 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
          </button>
        </div>
        {todoMessage && (
          <p className="mb-3 text-[11px] font-medium text-amber-600">
            {todoMessage}
          </p>
        )}

        <div className={TODO_LIST_VIEWPORT_CLASS}>
          <div className="space-y-3">
            {todoItems.map((item, index) => (
              <TodoItem
                key={item.id || item.text}
                text={item.text}
                done={item.done}
                onToggle={() => {
                  void handleTodoToggle(index);
                }}
                onClear={() => {
                  void handleTodoRemove(index);
                }}
              />
            ))}
          </div>
        </div>
      </div>

      <div className="w-full h-px bg-gray-200/70 my-5 shrink-0" />

      {/* 6. 文章历史记录 */}
      <div className="flex flex-col flex-1 min-h-0">
        {/* Header Section */}
        <div className="shrink-0 mb-4">
          <div className="flex items-center justify-between">
            <h3 className="text-[11px] font-bold text-gray-400 tracking-widest uppercase">文章记录</h3>
            <button 
              onClick={() => setShowAllArticlesModal(true)}
              className={`text-[11px] font-bold flex items-center gap-0.5 transition-colors uppercase tracking-widest ${filterType === 'all' ? 'text-blue-600' : 'text-gray-400 hover:text-gray-600'}`}
            >
              全部 <ArrowRight className="w-3 h-3" strokeWidth={2.5} />
            </button>
          </div>
        </div>

        {/* Input */}
        <div className="relative mb-4 shrink-0">
          <input
            ref={articleInputRef}
            type="text"
            placeholder="输入新链接，回车自动录入..."
            value={articleInput}
            onChange={(e) => {
              setArticleInput(e.target.value);
              if (articleMessage) {
                setArticleMessage('');
              }
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                handleArticleImport();
              }
            }}
            disabled={articleLoading}
            className="w-full border-b border-gray-200/80 bg-transparent pb-2 pr-6 text-[13px] text-gray-900 placeholder-gray-400 focus:outline-none focus:border-blue-500 transition-colors disabled:opacity-60"
          />
          <button
            onClick={() => {
              void handleArticleImport();
            }}
            disabled={articleLoading}
            className="absolute right-0 top-0 flex h-5 w-5 items-center justify-center text-gray-400 transition-colors hover:text-blue-600 disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="mb-4 shrink-0 text-[11px] text-gray-500 font-medium">
          当前 <span className="font-bold text-gray-900 ml-1">{articleStatsLoaded ? articleTodayCount : "..."}</span>
          <span className="mx-2 text-gray-300">/</span>
          总数 <span className="font-bold text-gray-900 ml-1">{articleStatsLoaded ? articleTotalCount : "..."}</span>
        </div>
        {articleMessage && (
          <p className={`mb-4 text-[11px] font-medium ${
            articleMessageTone === 'error'
              ? 'text-red-500'
              : articleMessageTone === 'warning'
                ? 'text-amber-600'
                : 'text-gray-500'
          }`}>
            {articleMessage}
          </p>
        )}

        {/* List - scrollable area */}
        <div
          className="flex-1 overflow-y-auto space-y-2 pb-4 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent"
          style={{ marginInlineEnd: "-18px", paddingInlineEnd: "18px" }}
        >
           {filteredArticles.map(article => (
             <ArticleItem
               key={article.id}
               articleId={article.id}
               source={article.source}
               title={article.title}
               type={article.type}
               url={article.url}
               matchedTasks={article.matchedTasks}
               reasonLines={article.reasonLines}
               classificationStatus={article.classificationStatus}
               classificationMessage={article.classificationMessage}
               unmatchedReason={article.unmatchedReason}
               referenced={article.referenced}
               referencedTasks={article.referencedTasks}
               onOpen={() => handleArticleOpen(article.url)}
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

        {/* Bottom Buttons */}
        <div className="grid grid-cols-2 gap-3 mt-2 pt-4 shrink-0 border-t border-gray-200/70">
          <button 
            onClick={() => setFilterType(filterType === 'self-media' ? 'all' : 'self-media')}
            className={`group inline-flex h-10 items-center justify-center gap-1.5 rounded-[14px] border px-3.5 text-[11px] font-semibold tracking-[0.01em] transition-all ${
              filterType === 'self-media' 
                ? 'border-[rgba(var(--brand-cyan-rgb),0.26)] bg-white text-[var(--brand-navy)] shadow-[0_10px_22px_-18px_rgba(15,23,42,0.3)]'
                : 'border-gray-200/80 text-gray-700 bg-white/76 hover:bg-white hover:border-gray-300 hover:text-[var(--brand-navy)] hover:shadow-[0_10px_22px_-18px_rgba(15,23,42,0.22)]'
            }`}
          >
            <Zap className={`w-3.5 h-3.5 transition-colors ${filterType === 'self-media' ? 'text-[var(--brand-cyan)]' : 'text-gray-400 group-hover:text-[var(--brand-cyan)]'}`} /> 自媒体
          </button>
          <button 
            onClick={() => setFilterType(filterType === 'media' ? 'all' : 'media')}
            className={`group inline-flex h-10 items-center justify-center gap-1.5 rounded-[14px] border px-3.5 text-[11px] font-semibold tracking-[0.01em] transition-all ${
              filterType === 'media' 
                ? 'border-[rgba(var(--brand-cyan-rgb),0.26)] bg-white text-[var(--brand-navy)] shadow-[0_10px_22px_-18px_rgba(15,23,42,0.3)]'
                : 'border-gray-200/80 text-gray-700 bg-white/76 hover:bg-white hover:border-gray-300 hover:text-[var(--brand-navy)] hover:shadow-[0_10px_22px_-18px_rgba(15,23,42,0.22)]'
            }`}
          >
            <Landmark className={`w-3.5 h-3.5 transition-colors ${filterType === 'media' ? 'text-[var(--brand-cyan)]' : 'text-gray-400 group-hover:text-[var(--brand-cyan)]'}`} /> 权威媒体
          </button>
        </div>
      </div>
      
      {showAllArticlesModal && (
        <Suspense fallback={null}>
          <ArticleSummaryModal
            brandName="全部文章"
            scope="all"
            onClose={() => setShowAllArticlesModal(false)}
          />
        </Suspense>
      )}
      <ArticleEditModal
        isOpen={Boolean(editingArticle)}
        article={editingArticle ? {
          id: editingArticle.id,
          source: editingArticle.source,
          mediaName: editingArticle.media_name,
          title: editingArticle.title,
          url: editingArticle.url,
          publishedAt: editingArticle.published_at,
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
    </div>
  );
}

function TodoItem({
  text,
  done = false,
  onToggle,
  onClear,
}: {
  text: string;
  done?: boolean;
  onToggle?: () => void;
  onClear?: () => void;
}) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          className="flex items-start gap-3 cursor-pointer group"
          onClick={() => onToggle?.()}
        >
          <div className={`w-3.5 h-3.5 mt-0.5 rounded border flex items-center justify-center shrink-0 transition-all ${
            done
              ? 'bg-[var(--brand-cyan)] border-[var(--brand-cyan)] text-white'
              : 'border-gray-300 bg-white group-hover:border-blue-500'
          }`}>
            {done && <Check className="w-2.5 h-2.5" strokeWidth={3} />}
          </div>
          <span className={`text-[12px] leading-snug flex-1 transition-colors ${
            done ? 'text-gray-400 line-through' : 'text-gray-700 font-medium group-hover:text-gray-900'
          }`}>
            {text}
          </span>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent className={DELETE_MENU_CONTENT_CLASS}>
        <ContextMenuItem
          onSelect={() => onClear?.()}
          className={DELETE_MENU_ITEM_CLASS}
        >
          <Trash2 className="w-3 h-3 text-gray-400" />
          清除
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}

function ArticleItem({
  articleId,
  source,
  title,
  type,
  url,
  matchedTasks,
  reasonLines,
  classificationStatus,
  classificationMessage,
  unmatchedReason,
  referenced,
  referencedTasks,
  onOpen,
  onSetMediaType,
  onEdit,
  onClear,
}: {
  articleId: string | number;
  source: string;
  title: string;
  type: 'media' | 'self-media';
  url?: string;
  matchedTasks?: string[];
  reasonLines?: string[];
  classificationStatus?: 'matched' | 'unmatched';
  classificationMessage?: string;
  unmatchedReason?: string;
  referenced?: boolean;
  referencedTasks?: string[];
  onOpen?: () => void;
  onSetMediaType?: (mediaType: "authority" | "selfmedia") => void;
  onEdit?: () => void;
  onClear?: () => void;
}) {
  const [isHovered, setIsHovered] = useState(false);
  const isSelfMedia = type === 'self-media';
  const branding = getMediaBranding(source);
  const isMatched = classificationStatus === 'matched' || Boolean(matchedTasks && matchedTasks.length > 0);
  const taskSummary = matchedTasks && matchedTasks.length > 0 ? matchedTasks.join("、") : "";
  const summaryText = isMatched
    ? (classificationMessage || (taskSummary ? `已归类到 ${taskSummary}` : "已归类"))
    : (unmatchedReason || classificationMessage || "暂未归类");
  const contextLines = reasonLines && reasonLines.length > 0
    ? reasonLines
    : [summaryText];
  const referencedTaskText = referencedTasks && referencedTasks.length > 0 ? referencedTasks.join("、") : "";
  const fullContextLines = referenced
    ? [...contextLines, referencedTaskText ? `引用状态：已被 ${referencedTaskText} 引用` : "引用状态：已引用"]
    : contextLines;
  const articleHref = normalizeArticleHref(url);
  
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <a
          href={articleHref || undefined}
          target={articleHref ? "_blank" : undefined}
          rel={articleHref ? "noopener noreferrer" : undefined}
          className="flex p-3 rounded-lg border border-gray-100/80 bg-white gap-3 text-inherit no-underline hover:border-gray-300 hover:shadow-sm transition-all cursor-pointer group"
          onClick={(event) => {
            if (!articleHref) {
              event.preventDefault();
              onOpen?.();
            }
          }}
          onMouseEnter={() => setIsHovered(true)}
          onMouseLeave={() => setIsHovered(false)}
          title={url ? "左键打开文章，右键查看归类原因" : undefined}
        >
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center font-black text-[16px] tracking-tighter shrink-0 transition-all ${
            isHovered ? branding.containerClassName : "bg-gray-100 text-gray-400"
          }`}>
            {branding.initial}
          </div>

          <div className="flex-1 min-w-0 flex flex-col justify-center">
            <div className="flex items-center justify-between gap-2 mb-0.5">
              <div className="flex min-w-0 items-center gap-1.5">
                <span className="min-w-0 truncate text-[13px] font-bold text-gray-900 transition-colors group-hover:text-blue-600">{source}</span>
                {referenced ? (
                  <span className="shrink-0 rounded border border-emerald-100 bg-emerald-50 px-1.5 py-0.5 text-[9px] font-bold leading-none tracking-wider text-emerald-600">
                    已引用
                  </span>
                ) : null}
              </div>
              <div className="flex items-center gap-1.5 shrink-0">
                <span className={`text-[9px] px-1.5 py-0.5 rounded font-bold shrink-0 tracking-wider uppercase ${
                  isSelfMedia 
                    ? 'bg-blue-50 text-blue-600' 
                    : 'bg-gray-100 text-gray-600'
                }`}>
                  {isSelfMedia ? '自媒体' : '媒体'}
                </span>
              </div>
            </div>
            <p className="truncate text-[11px] font-medium text-gray-400">
              {title}
            </p>
            <div className="mt-1 flex items-center gap-1.5 min-w-0">
              <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${
                isMatched ? 'bg-emerald-500/90' : 'bg-amber-500/90'
              }`} />
              <p className={`text-[10px] font-medium truncate ${
                isMatched ? 'text-gray-500' : 'text-amber-600'
              }`}>
                {summaryText}
              </p>
            </div>
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
          disabled={!articleId || !isSelfMedia}
          className={ARTICLE_MENU_DELETE_CLASS}
        >
          <Landmark className="h-3 w-3 text-gray-400" />
          设为权威媒体
        </ContextMenuItem>
        <ContextMenuItem
          onSelect={() => onSetMediaType?.("selfmedia")}
          disabled={!articleId || isSelfMedia}
          className={ARTICLE_MENU_DELETE_CLASS}
        >
          <Zap className="h-3 w-3 text-gray-400" />
          设为自媒体
        </ContextMenuItem>
        <ContextMenuSeparator className="mx-1.5 my-1 bg-gray-100/90" />
        <ContextMenuItem
          onSelect={() => onEdit?.()}
          className={ARTICLE_MENU_DELETE_CLASS}
        >
          <Pencil className="h-3 w-3 text-gray-400" />
          修改
        </ContextMenuItem>
        <ContextMenuItem
          onSelect={() => onClear?.()}
          className={ARTICLE_MENU_DELETE_CLASS}
        >
          <Trash2 className="h-3 w-3 text-gray-400" />
          清除
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}
