import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { fetchTasksFull, readTasksFullCache, type TaskFull } from "../lib/backend";

export type ArticleEditValue = {
  mediaName: string;
  title: string;
  publishedAt: string;
  taskNames: string[];
};

type ArticleBrandOption = {
  taskName: string;
  brandName: string;
  label: string;
  searchText: string;
};

type ArticleEditModalProps = {
  isOpen: boolean;
  article: {
    id?: string | number;
    source?: string;
    mediaName?: string;
    title?: string;
    url?: string;
    publishedAt?: string;
    ts?: string;
    matchedTasks?: string[];
  } | null;
  saving?: boolean;
  onClose: () => void;
  onConfirm: (value: ArticleEditValue) => void | Promise<void>;
};

function normalizeSearchText(value: string) {
  return String(value || "").trim().toLocaleLowerCase();
}

function isNumericIdLike(value: string) {
  return /^\d+$/.test(String(value || "").trim());
}

function normalizeTaskNames(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const result: string[] = [];
  value.forEach((item) => {
    const text = String(item || "").trim();
    if (text && !result.includes(text)) {
      result.push(text);
    }
  });
  return result;
}

function taskToBrandOption(task: TaskFull): ArticleBrandOption | null {
  const source = task as TaskFull & Record<string, unknown>;
  const rawTaskName = String(
    source.name
    || source.taskName
    || source.task_name
    || source.task_id
    || source.id
    || "",
  ).trim();
  const rawBrandName = String(
    source.brand
    || source.brandName
    || source.brand_name
    || rawTaskName
    || "",
  ).trim();
  const taskName = rawTaskName;
  if (!taskName) {
    return null;
  }
  const brandName = (
    rawBrandName && !isNumericIdLike(rawBrandName)
      ? rawBrandName
      : taskName
  ).trim() || taskName;
  if (isNumericIdLike(taskName) && isNumericIdLike(brandName)) {
    return null;
  }
  const label = brandName === taskName ? brandName : `${brandName} (${taskName})`;
  return {
    taskName,
    brandName,
    label,
    searchText: normalizeSearchText(`${brandName} ${taskName} ${label}`),
  };
}

function buildBrandOptions(tasks: TaskFull[], currentTaskNames: string[] = []): ArticleBrandOption[] {
  const options: ArticleBrandOption[] = [];
  const seen = new Set<string>();

  tasks.forEach((task) => {
    const option = taskToBrandOption(task);
    if (!option || seen.has(option.taskName)) {
      return;
    }
    seen.add(option.taskName);
    options.push(option);
  });

  currentTaskNames.forEach((taskName) => {
    if (!taskName || seen.has(taskName)) {
      return;
    }
    seen.add(taskName);
    options.push({
      taskName,
      brandName: taskName,
      label: taskName,
      searchText: normalizeSearchText(taskName),
    });
  });

  return options.sort((left, right) => left.brandName.localeCompare(right.brandName, "zh-Hans-CN"));
}

function mergeCurrentBrandOptions(options: ArticleBrandOption[], currentTaskNames: string[] = []) {
  const merged = [...options];
  const seen = new Set(merged.map((option) => option.taskName));
  currentTaskNames.forEach((taskName) => {
    if (!taskName || seen.has(taskName)) {
      return;
    }
    seen.add(taskName);
    merged.push({
      taskName,
      brandName: taskName,
      label: taskName,
      searchText: normalizeSearchText(taskName),
    });
  });
  return merged.sort((left, right) => left.brandName.localeCompare(right.brandName, "zh-Hans-CN"));
}

function getBrandOptionLabel(taskName: string, options: ArticleBrandOption[]) {
  const option = options.find((item) => item.taskName === taskName);
  return option?.brandName || taskName;
}

function formatBrandInput(taskNames: string[], options: ArticleBrandOption[]) {
  return taskNames.map((taskName) => getBrandOptionLabel(taskName, options)).filter(Boolean).join("、");
}

function findExactBrandOption(input: string, options: ArticleBrandOption[]) {
  const normalized = normalizeSearchText(input);
  if (!normalized) {
    return null;
  }
  return options.find((option) => (
    normalizeSearchText(option.brandName) === normalized
    || normalizeSearchText(option.taskName) === normalized
    || normalizeSearchText(option.label) === normalized
  )) || null;
}

export function ArticleEditModal({
  isOpen,
  article,
  saving = false,
  onClose,
  onConfirm,
}: ArticleEditModalProps) {
  const [mediaName, setMediaName] = useState("");
  const [title, setTitle] = useState("");
  const [publishedAt, setPublishedAt] = useState("");
  const [brandInput, setBrandInput] = useState("");
  const [brandInputDirty, setBrandInputDirty] = useState(false);
  const [brandInputFocused, setBrandInputFocused] = useState(false);
  const [brandOptions, setBrandOptions] = useState<ArticleBrandOption[]>([]);
  const articleIdentity = [
    article?.id ?? "",
    article?.url ?? "",
    article?.title ?? "",
  ].map((value) => String(value || "").trim()).join("|");

  const initialTaskNames = useMemo(
    () => normalizeTaskNames(article?.matchedTasks),
    [article?.matchedTasks],
  );
  const effectiveBrandOptions = useMemo(
    () => mergeCurrentBrandOptions(brandOptions, initialTaskNames),
    [brandOptions, initialTaskNames],
  );

  useEffect(() => {
    if (!isOpen || !article) {
      return;
    }
    setMediaName(String(article.mediaName || article.source || "").trim());
    setTitle(String(article.title || "").trim());
    setPublishedAt(String(article.publishedAt || article.ts || "").slice(0, 10));
    setBrandInput(formatBrandInput(normalizeTaskNames(article.matchedTasks), effectiveBrandOptions));
    setBrandInputDirty(false);
  }, [articleIdentity, isOpen]);

  useEffect(() => {
    if (!isOpen || !article || brandInputDirty) {
      return;
    }
    setBrandInput(formatBrandInput(initialTaskNames, effectiveBrandOptions));
  }, [articleIdentity, brandInputDirty, effectiveBrandOptions, initialTaskNames, isOpen]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    let cancelled = false;
    const applyTasks = (tasks: TaskFull[]) => {
      if (cancelled) {
        return;
      }
      setBrandOptions(buildBrandOptions(tasks));
    };
    const cachedTasks = readTasksFullCache();
    if (cachedTasks?.length) {
      applyTasks(cachedTasks);
    }
    void fetchTasksFull().then(applyTasks);
    return () => {
      cancelled = true;
    };
  }, [isOpen]);

  if (!isOpen || !article || typeof document === "undefined") {
    return null;
  }

  const trimmedBrandInput = brandInput.trim();
  const exactBrandOption = findExactBrandOption(trimmedBrandInput, effectiveBrandOptions);
  const hasUnknownBrand = Boolean(trimmedBrandInput && !exactBrandOption && brandInputDirty);
  const brandSuggestions = trimmedBrandInput
    ? effectiveBrandOptions
        .map((option) => {
          const query = normalizeSearchText(trimmedBrandInput);
          const brandText = normalizeSearchText(option.brandName);
          const taskText = normalizeSearchText(option.taskName);
          const startsWithQuery = brandText.startsWith(query) || taskText.startsWith(query);
          const includesQuery = option.searchText.includes(query);
          if (!startsWithQuery && !includesQuery) {
            return null;
          }
          return { option, score: startsWithQuery ? 0 : 1 };
        })
        .filter((item): item is { option: ArticleBrandOption; score: number } => Boolean(item))
        .sort((left, right) => left.score - right.score || left.option.brandName.localeCompare(right.option.brandName, "zh-Hans-CN"))
        .slice(0, 6)
        .map((item) => item.option)
    : [];
  const resolvedTaskNames = (() => {
    if (!trimmedBrandInput) {
      return [];
    }
    if (exactBrandOption) {
      return [exactBrandOption.taskName];
    }
    if (!brandInputDirty && initialTaskNames.length) {
      return initialTaskNames;
    }
    return [];
  })();
  const canSubmit = Boolean(mediaName.trim() && title.trim()) && !hasUnknownBrand && !saving;

  return createPortal(
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-slate-950/18 px-4 backdrop-blur-[2px] animate-in fade-in duration-150">
      <div className="w-full max-w-[460px] overflow-hidden rounded-[24px] border border-gray-200/80 bg-white shadow-[0_24px_70px_-30px_rgba(15,23,42,0.38)] animate-in zoom-in-95 slide-in-from-bottom-2 duration-200">
        <div className="flex items-start justify-between gap-4 border-b border-gray-100 px-5 py-4">
          <div>
            <div className="text-[11px] font-black uppercase tracking-[0.22em] text-gray-400">修改文章</div>
            <div className="mt-1 text-[13px] font-medium text-gray-500">调整媒体名、标题、发表日期和所属品牌</div>
          </div>
          <button
            onClick={onClose}
            disabled={saving}
            className="flex h-8 w-8 items-center justify-center rounded-full text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-900 disabled:opacity-50"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 px-5 py-5">
          <label className="block">
            <span className="mb-1.5 block text-[11px] font-bold text-gray-500">媒体名</span>
            <input
              value={mediaName}
              onChange={(event) => setMediaName(event.target.value)}
              disabled={saving}
              className="h-10 w-full rounded-[14px] border border-gray-200/90 bg-gray-50/70 px-3 text-[13px] font-medium text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-blue-200 focus:bg-white focus:ring-4 focus:ring-blue-50 disabled:opacity-60"
              placeholder="请输入媒体名"
            />
          </label>

          <label className="relative block">
            <span className="mb-1.5 block text-[11px] font-bold text-gray-500">所属品牌</span>
            <input
              value={brandInput}
              onChange={(event) => {
                setBrandInput(event.target.value);
                setBrandInputDirty(true);
              }}
              onFocus={() => setBrandInputFocused(true)}
              onBlur={() => window.setTimeout(() => setBrandInputFocused(false), 120)}
              disabled={saving}
              className={`h-10 w-full rounded-[14px] border bg-gray-50/70 px-3 text-[13px] font-medium text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:bg-white focus:ring-4 disabled:opacity-60 ${
                hasUnknownBrand
                  ? "border-rose-200 focus:border-rose-300 focus:ring-rose-50"
                  : "border-gray-200/90 focus:border-blue-200 focus:ring-blue-50"
              }`}
              placeholder="输入品牌名"
            />
            {brandInputFocused && brandSuggestions.length > 0 ? (
              <div className="absolute left-0 right-0 top-[64px] z-[72] overflow-hidden rounded-[14px] border border-gray-200/90 bg-white shadow-[0_16px_36px_-24px_rgba(15,23,42,0.36)]">
                {brandSuggestions.map((option) => (
                  <button
                    key={option.taskName}
                    type="button"
                    onMouseDown={(event) => {
                      event.preventDefault();
                      setBrandInput(option.brandName);
                      setBrandInputDirty(true);
                      setBrandInputFocused(false);
                    }}
                    className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-[12px] font-semibold text-gray-700 transition-colors hover:bg-gray-50 hover:text-gray-950"
                  >
                    <span className="min-w-0 truncate">{option.brandName}</span>
                    {option.brandName !== option.taskName ? (
                      <span className="shrink-0 text-[10px] font-bold text-gray-400">{option.taskName}</span>
                    ) : null}
                  </button>
                ))}
              </div>
            ) : null}
            {hasUnknownBrand ? (
              <span className="mt-1.5 block text-[11px] font-medium text-rose-500">请选择已有品牌</span>
            ) : null}
          </label>

          <label className="block">
            <span className="mb-1.5 block text-[11px] font-bold text-gray-500">文章标题</span>
            <textarea
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              disabled={saving}
              rows={3}
              className="w-full resize-none rounded-[16px] border border-gray-200/90 bg-gray-50/70 px-3 py-2.5 text-[13px] font-medium leading-relaxed text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-blue-200 focus:bg-white focus:ring-4 focus:ring-blue-50 disabled:opacity-60"
              placeholder="请输入文章标题"
            />
          </label>

          <label className="block">
            <span className="mb-1.5 block text-[11px] font-bold text-gray-500">发表时间</span>
            <input
              type="date"
              value={publishedAt}
              onChange={(event) => setPublishedAt(event.target.value)}
              disabled={saving}
              className="h-10 w-full rounded-[14px] border border-gray-200/90 bg-gray-50/70 px-3 text-[13px] font-medium text-gray-900 outline-none transition-colors focus:border-blue-200 focus:bg-white focus:ring-4 focus:ring-blue-50 disabled:opacity-60"
            />
          </label>
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-100 bg-gray-50/70 px-5 py-4">
          <button
            onClick={onClose}
            disabled={saving}
            className="h-9 rounded-[13px] border border-gray-200/80 bg-white px-4 text-[12px] font-bold text-gray-600 transition-colors hover:bg-gray-50 hover:text-gray-900 disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={() => {
              if (canSubmit) {
                onConfirm({
                  mediaName: mediaName.trim(),
                  title: title.trim(),
                  publishedAt: publishedAt.trim(),
                  taskNames: resolvedTaskNames,
                });
              }
            }}
            disabled={!canSubmit}
            className="h-9 rounded-[13px] bg-blue-600 px-4 text-[12px] font-bold text-white shadow-[0_12px_24px_-16px_rgba(37,99,235,0.8)] transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300 disabled:shadow-none"
          >
            {saving ? "保存中..." : "确认"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
