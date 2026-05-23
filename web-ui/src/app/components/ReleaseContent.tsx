import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileText,
  Loader2,
  Pencil,
  Radio,
  Search,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { DayPicker } from "react-day-picker";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { ScrollArea } from "./ui/scroll-area";
import {
  TASK_DATA_CHANGED_EVENT,
  fetchTasksFull,
  readTasksFullCache,
  type TaskFull,
} from "../lib/backend";

type MediaCategory = "media" | "self_media" | "overseas";

type MediaItem = {
  id: string;
  name: string;
  category: MediaCategory;
  price: number;
  channel: string;
  turnaround: string;
  tags: string[];
};

type SelectedMedia = MediaItem & {
  priceOverride?: number;
  note?: string;
};

type PublishStatus = "submitting" | "publishing" | "published" | "failed";

type PublishRecord = {
  id: string;
  mediaId: string;
  mediaName: string;
  mediaCategory: MediaCategory;
  title: string;
  client: string;
  price: number;
  status: PublishStatus;
  createdAt: number;
  scheduledAt: number | null;
  publishedAt: number | null;
  attachments: number;
  remarks: string;
  note?: string;
  errorMessage?: string;
};

const STORAGE_KEY = "surfaced-release-records-v1";

const CATEGORY_LABEL: Record<MediaCategory, string> = {
  media: "媒体",
  self_media: "自媒体",
  overseas: "海外媒体",
};

const STATUS_LABEL: Record<PublishStatus, string> = {
  submitting: "提交中",
  publishing: "发布中",
  published: "已发布",
  failed: "失败",
};

const STATUS_DOT: Record<PublishStatus, string> = {
  submitting: "bg-amber-500",
  publishing: "bg-blue-500",
  published: "bg-emerald-500",
  failed: "bg-rose-500",
};

const MEDIA_CATALOG: MediaItem[] = [
  // 媒体
  { id: "m-001", name: "新浪财经", category: "media", price: 1800, channel: "门户网站", turnaround: "1 工作日", tags: ["头部门户", "财经"] },
  { id: "m-002", name: "凤凰网", category: "media", price: 1500, channel: "门户网站", turnaround: "1 工作日", tags: ["头部门户"] },
  { id: "m-003", name: "搜狐网", category: "media", price: 600, channel: "门户网站", turnaround: "当日", tags: ["综合"] },
  { id: "m-004", name: "网易科技", category: "media", price: 1200, channel: "门户网站", turnaround: "当日", tags: ["科技"] },
  { id: "m-005", name: "中国经济网", category: "media", price: 2400, channel: "央媒", turnaround: "2 工作日", tags: ["权威", "经济"] },
  { id: "m-006", name: "人民网", category: "media", price: 4800, channel: "央媒", turnaround: "2 工作日", tags: ["权威"] },
  { id: "m-007", name: "21 世纪经济报道", category: "media", price: 3200, channel: "报刊", turnaround: "2 工作日", tags: ["财经", "权威"] },
  { id: "m-008", name: "36 氪", category: "media", price: 2100, channel: "科技媒体", turnaround: "1 工作日", tags: ["科技", "创投"] },
  { id: "m-009", name: "钛媒体", category: "media", price: 1800, channel: "科技媒体", turnaround: "1 工作日", tags: ["科技"] },
  { id: "m-010", name: "界面新闻", category: "media", price: 2600, channel: "财经媒体", turnaround: "1 工作日", tags: ["财经"] },
  // 自媒体
  { id: "s-001", name: "公众号·商业观察家", category: "self_media", price: 4500, channel: "微信公众号", turnaround: "3 工作日", tags: ["头部号", "商业"] },
  { id: "s-002", name: "公众号·科技唆麻", category: "self_media", price: 3200, channel: "微信公众号", turnaround: "2 工作日", tags: ["科技"] },
  { id: "s-003", name: "小红书·消费研究所", category: "self_media", price: 2800, channel: "小红书", turnaround: "1 工作日", tags: ["消费"] },
  { id: "s-004", name: "抖音·城市笔记", category: "self_media", price: 5200, channel: "抖音", turnaround: "3 工作日", tags: ["视频"] },
  { id: "s-005", name: "B 站·量子位频道", category: "self_media", price: 3800, channel: "B 站", turnaround: "2 工作日", tags: ["科技", "视频"] },
  { id: "s-006", name: "知乎·机构号·硅基观察", category: "self_media", price: 1900, channel: "知乎", turnaround: "1 工作日", tags: ["科技"] },
  { id: "s-007", name: "微博·财经早餐", category: "self_media", price: 1600, channel: "微博", turnaround: "当日", tags: ["财经"] },
  // 海外媒体
  { id: "o-001", name: "Bloomberg Newsroom", category: "overseas", price: 18000, channel: "英文媒体", turnaround: "5 工作日", tags: ["头部", "财经"] },
  { id: "o-002", name: "Reuters Wire", category: "overseas", price: 16500, channel: "通讯社", turnaround: "4 工作日", tags: ["头部", "权威"] },
  { id: "o-003", name: "PR Newswire", category: "overseas", price: 6800, channel: "通稿", turnaround: "2 工作日", tags: ["通稿"] },
  { id: "o-004", name: "Yahoo Finance", category: "overseas", price: 5400, channel: "财经门户", turnaround: "2 工作日", tags: ["财经"] },
  { id: "o-005", name: "TechCrunch", category: "overseas", price: 12000, channel: "科技媒体", turnaround: "5 工作日", tags: ["科技"] },
  { id: "o-006", name: "Nikkei Asia", category: "overseas", price: 9800, channel: "财经媒体", turnaround: "4 工作日", tags: ["财经"] },
  { id: "o-007", name: "Forbes Asia", category: "overseas", price: 13500, channel: "财经媒体", turnaround: "5 工作日", tags: ["头部"] },
];

type BrandSummary = {
  name: string;
  weeklyTotal: number;
  totalSpend: number;
  publishCount: number;
};

function extractBrandsFromTasks(tasks: TaskFull[] | null | undefined): string[] {
  if (!tasks?.length) return [];
  const seen = new Set<string>();
  for (const task of tasks) {
    const brand = String(task.brand || "").trim();
    if (brand) seen.add(brand);
  }
  return Array.from(seen).sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
}

function formatPrice(value: number) {
  return `¥${value.toLocaleString("zh-CN")}`;
}

function formatPriceShort(value: number) {
  if (value >= 10000) return `¥${(value / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  if (value >= 1000) return `¥${(value / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return `¥${value}`;
}

function pad2(n: number) {
  return String(n).padStart(2, "0");
}

function toScheduledValue(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}T${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function parseScheduledValue(value: string): Date | null {
  if (!value) return null;
  const ts = new Date(value).getTime();
  return Number.isFinite(ts) ? new Date(ts) : null;
}

function suggestedScheduledTime(): Date {
  const date = new Date();
  date.setSeconds(0);
  date.setMilliseconds(0);
  date.setMinutes(date.getMinutes() + 30);
  const rem = date.getMinutes() % 5;
  if (rem !== 0) date.setMinutes(date.getMinutes() + (5 - rem));
  return date;
}

function formatScheduledDisplay(value: string): string {
  const date = parseScheduledValue(value);
  if (!date) return "";
  const now = new Date();
  const sameYear = date.getFullYear() === now.getFullYear();
  const datePart = `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  const timePart = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  return sameYear ? `${datePart} ${timePart}` : `${date.getFullYear()}-${datePart} ${timePart}`;
}

function formatScheduledLong(value: string): string {
  const date = parseScheduledValue(value);
  if (!date) return "";
  const weekday = ["日", "一", "二", "三", "四", "五", "六"][date.getDay()];
  return `${date.getFullYear()} 年 ${pad2(date.getMonth() + 1)} 月 ${pad2(date.getDate())} 日 (周${weekday}) · ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function formatTime(ts: number) {
  const date = new Date(ts);
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${m}-${d} ${hh}:${mm}`;
}

function formatTimeFull(ts: number) {
  const date = new Date(ts);
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${y}-${m}-${d} ${hh}:${mm}`;
}

function recordDateKey(ts: number): string {
  const d = new Date(ts);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function recordDateLabel(ts: number): string {
  const d = new Date(ts);
  return `${d.getFullYear()}.${pad2(d.getMonth() + 1)}.${pad2(d.getDate())}`;
}

function recordDateSubLabel(ts: number, nowTs: number): string {
  const d = new Date(ts);
  d.setHours(0, 0, 0, 0);
  const now = new Date(nowTs);
  now.setHours(0, 0, 0, 0);
  const diffDays = Math.round((now.getTime() - d.getTime()) / (24 * 3600 * 1000));
  if (diffDays === 0) return "今天";
  if (diffDays === 1) return "昨天";
  if (diffDays === 2) return "前天";
  if (diffDays > 0 && diffDays <= 6) return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][new Date(ts).getDay()];
  return "";
}

function isSameDay(a: number, b: number) {
  const da = new Date(a);
  const db = new Date(b);
  return da.getFullYear() === db.getFullYear() && da.getMonth() === db.getMonth() && da.getDate() === db.getDate();
}

function loadRecords(): PublishRecord[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed as PublishRecord[];
  } catch {
    return [];
  }
}

function persistRecords(records: PublishRecord[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
  } catch {
    // ignore
  }
}

export function ReleaseContent() {
  const [selectedMedia, setSelectedMedia] = useState<SelectedMedia[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [autoFormat, setAutoFormat] = useState(true);
  const [client, setClient] = useState<string>("");
  const [scheduledAt, setScheduledAt] = useState<string>("");
  const [remarks, setRemarks] = useState("");
  const [attachments, setAttachments] = useState<{ name: string; size: number }[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const contentRef = useRef<HTMLTextAreaElement | null>(null);

  const [showMediaPicker, setShowMediaPicker] = useState(false);
  const [showBrandPicker, setShowBrandPicker] = useState(false);
  const [availableBrands, setAvailableBrands] = useState<string[]>(() => extractBrandsFromTasks(readTasksFullCache()));
  const [records, setRecords] = useState<PublishRecord[]>(() => loadRecords());
  const [showStatusPanel, setShowStatusPanel] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitMessage, setSubmitMessage] = useState("");
  const [editingRecord, setEditingRecord] = useState<PublishRecord | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<PublishRecord | null>(null);

  useEffect(() => {
    persistRecords(records);
  }, [records]);

  // Auto-grow content textarea so long articles expand naturally instead of forcing the user to scroll inside a tiny box
  useLayoutEffect(() => {
    const el = contentRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [content]);

  // Load brand list from the account's tasks (already permission-filtered by backend)
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const tasks = await fetchTasksFull();
        if (cancelled) return;
        setAvailableBrands(extractBrandsFromTasks(tasks));
      } catch {
        // ignore — keep cached value
      }
    };
    refresh();
    const handler = () => {
      const cached = readTasksFullCache();
      if (cached) setAvailableBrands(extractBrandsFromTasks(cached));
    };
    window.addEventListener(TASK_DATA_CHANGED_EVENT, handler);
    return () => {
      cancelled = true;
      window.removeEventListener(TASK_DATA_CHANGED_EVENT, handler);
    };
  }, []);

  const brandSummaries = useMemo<BrandSummary[]>(() => {
    const weekAgo = Date.now() - 7 * 24 * 3600 * 1000;
    const map = new Map<string, BrandSummary>();
    for (const brand of availableBrands) {
      map.set(brand, { name: brand, weeklyTotal: 0, totalSpend: 0, publishCount: 0 });
    }
    for (const record of records) {
      if (record.status !== "published" || !record.client) continue;
      let summary = map.get(record.client);
      if (!summary) {
        // Historical client no longer in brand list — still surface stats
        summary = { name: record.client, weeklyTotal: 0, totalSpend: 0, publishCount: 0 };
        map.set(record.client, summary);
      }
      summary.totalSpend += record.price;
      summary.publishCount += 1;
      if (record.publishedAt && record.publishedAt >= weekAgo) {
        summary.weeklyTotal += record.price;
      }
    }
    return Array.from(map.values()).sort((a, b) => {
      if (b.weeklyTotal !== a.weeklyTotal) return b.weeklyTotal - a.weeklyTotal;
      if (b.totalSpend !== a.totalSpend) return b.totalSpend - a.totalSpend;
      return a.name.localeCompare(b.name, "zh-Hans-CN");
    });
  }, [availableBrands, records]);

  // Mock progression
  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = [];
    records.forEach((record) => {
      if (record.status === "submitting") {
        timers.push(
          setTimeout(() => {
            setRecords((prev) => prev.map((item) => (item.id === record.id && item.status === "submitting" ? { ...item, status: "publishing" } : item)));
          }, 1800 + Math.random() * 1400),
        );
      } else if (record.status === "publishing") {
        timers.push(
          setTimeout(() => {
            setRecords((prev) =>
              prev.map((item) => {
                if (item.id !== record.id || item.status !== "publishing") return item;
                const success = Math.random() > 0.12;
                return success
                  ? { ...item, status: "published", publishedAt: Date.now() }
                  : { ...item, status: "failed", errorMessage: "媒体侧审核未通过，请补充资质材料" };
              }),
            );
          }, 4200 + Math.random() * 3200),
        );
      }
    });
    return () => timers.forEach(clearTimeout);
  }, [records]);

  const todayPublishedCount = useMemo(() => {
    const now = Date.now();
    return records.filter((record) => record.status === "published" && record.publishedAt && isSameDay(record.publishedAt, now)).length;
  }, [records]);

  const inFlightCount = useMemo(
    () => records.filter((r) => r.status === "submitting" || r.status === "publishing").length,
    [records],
  );

  const selectedTotal = useMemo(
    () => selectedMedia.reduce((sum, item) => sum + (item.priceOverride ?? item.price), 0),
    [selectedMedia],
  );

  const toggleMedia = useCallback((media: MediaItem) => {
    setSelectedMedia((prev) => {
      const exists = prev.find((item) => item.id === media.id);
      if (exists) return prev.filter((item) => item.id !== media.id);
      return [...prev, { ...media }];
    });
  }, []);

  const updateSelected = useCallback((id: string, patch: Partial<SelectedMedia>) => {
    setSelectedMedia((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  }, []);

  const removeSelected = useCallback((id: string) => {
    setSelectedMedia((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const handleFiles = useCallback((files: FileList | null) => {
    if (!files) return;
    const next: { name: string; size: number }[] = [];
    Array.from(files).forEach((file) => next.push({ name: file.name, size: file.size }));
    setAttachments((prev) => [...prev, ...next]);
  }, []);

  const removeAttachment = useCallback((index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const resetForm = () => {
    setTitle("");
    setContent("");
    setRemarks("");
    setAttachments([]);
    setScheduledAt("");
    setSelectedMedia([]);
  };

  const handleSubmit = async () => {
    if (submitting) return;
    if (selectedMedia.length === 0) return setSubmitMessage("请先选择投放媒体");
    if (!title.trim()) return setSubmitMessage("请填写文章标题");
    if (!content.trim()) return setSubmitMessage("请填写正文内容");
    if (!client) return setSubmitMessage("请选择所属客户");
    setSubmitMessage("");
    setSubmitting(true);
    await new Promise((resolve) => setTimeout(resolve, 600));

    const now = Date.now();
    const scheduledTs = scheduledAt ? new Date(scheduledAt).getTime() : null;
    const newRecords: PublishRecord[] = selectedMedia.map((media, index) => ({
      id: `pub-${now}-${index}-${Math.random().toString(36).slice(2, 7)}`,
      mediaId: media.id,
      mediaName: media.name,
      mediaCategory: media.category,
      title: title.trim(),
      client,
      price: media.priceOverride ?? media.price,
      status: "submitting",
      createdAt: now,
      scheduledAt: scheduledTs,
      publishedAt: null,
      attachments: attachments.length,
      remarks: remarks.trim(),
      note: media.note,
    }));

    setRecords((prev) => [...newRecords, ...prev]);
    setSubmitting(false);
    setShowStatusPanel(true);
    resetForm();
  };

  const updateRecord = (id: string, patch: Partial<PublishRecord>) => {
    setRecords((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  };

  const removeRecord = (id: string) => {
    setRecords((prev) => prev.filter((item) => item.id !== id));
  };

  return (
    <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-transparent">
      <div className="absolute inset-0 bg-[#fcfdff]" />

      {/* Header */}
      <div className="relative z-10 mx-auto flex w-full max-w-[960px] shrink-0 items-end justify-between px-10 pt-10 pb-5">
        <div className="flex items-center gap-2">
          <FileText className="h-5 w-5 text-blue-600" />
          <h1 className="app-wordmark-heading text-[20px]">发稿</h1>
        </div>
        <div className="flex items-center gap-6 text-[12px] font-semibold text-gray-500">
          <span>
            今日已发布 <span className="text-gray-900">{todayPublishedCount}</span> 篇
          </span>
          <button
            type="button"
            onClick={() => setShowStatusPanel(true)}
            className="group inline-flex items-center gap-1.5 text-gray-900 transition-colors hover:text-black"
          >
            <Radio className="h-3.5 w-3.5" strokeWidth={2.4} />
            发布情况
            {inFlightCount > 0 ? (
              <span className="ml-0.5 inline-flex h-[16px] min-w-[16px] items-center justify-center rounded-full bg-blue-600 px-1 text-[10px] font-bold text-white">
                {inFlightCount}
              </span>
            ) : null}
            <ChevronRight className="h-3 w-3 text-gray-400 transition-colors group-hover:text-gray-900" />
          </button>
        </div>
      </div>

      {/* Writing area (scrollable) */}
      <div className="relative z-10 min-h-0 flex-1">
        <div className="absolute inset-0">
        <ScrollArea type="always" className="h-full w-full [&_[data-radix-scroll-area-viewport]>div]:!h-full [&_[data-slot=scroll-area-thumb]]:!bg-gray-300 [&_[data-slot=scroll-area-thumb]:hover]:!bg-gray-400 [&_[data-slot=scroll-area-scrollbar]]:!w-2">
        <div className="mx-auto flex min-h-full w-full max-w-[960px] flex-col px-10 pb-4">
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="标题"
          className="w-full border-0 bg-transparent px-0 pt-2 pb-3 text-[21px] font-semibold leading-[1.35] tracking-[-0.005em] text-gray-900 outline-none placeholder:font-normal placeholder:text-gray-300"
        />
        <div className="h-px w-full bg-gray-200/70" />

        <textarea
          ref={contentRef}
          value={content}
          onChange={(event) => setContent(event.target.value)}
          placeholder="开始写正文。每段之间留空行，开启自动排版后会统一标点与段落格式。"
          className="w-full resize-none border-0 bg-transparent px-0 pt-5 pb-5 text-[13.5px] leading-[1.85] text-gray-800 outline-none placeholder:text-gray-300"
          style={{ minHeight: "calc(100vh - 360px)", overflow: "hidden" }}
        />

        <div className="mt-auto">
        <div className="h-px w-full bg-gray-200/70" />

        {/* Meta row — 5 columns */}
        <div className="grid grid-cols-2 gap-x-10 gap-y-4 pt-4 md:grid-cols-3 lg:grid-cols-5">
          <FieldCell label="所属客户" onClick={() => setShowBrandPicker(true)}>
            {client ? (
              <span className="flex items-baseline gap-1.5">
                <span className="truncate text-gray-900">{client}</span>
                <ChevronRight className="h-3 w-3 shrink-0 self-center text-gray-300" />
              </span>
            ) : (
              <span className="text-gray-300">+ 选择品牌</span>
            )}
          </FieldCell>

          <FieldCell label="投放渠道" onClick={() => setShowMediaPicker(true)}>
            {selectedMedia.length === 0 ? (
              <span className="text-gray-300">+ 选择媒体</span>
            ) : (
              <span className="flex items-baseline gap-1.5">
                <span className="text-gray-900">{selectedMedia.length} 家</span>
                <span className="text-gray-400">·</span>
                <span className="text-gray-500">{formatPriceShort(selectedTotal)}</span>
                <ChevronRight className="h-3 w-3 self-center text-gray-300" />
              </span>
            )}
          </FieldCell>

          <FieldCell label="附件" onClick={() => fileInputRef.current?.click()}>
            {attachments.length === 0 ? (
              <span className="text-gray-300">+ 上传文件</span>
            ) : (
              <span className="flex items-center gap-1.5">
                <span className="text-gray-900">{attachments.length} 个</span>
                <span className="text-gray-300">·</span>
                <span className="truncate text-gray-500">{attachments[0]?.name}</span>
              </span>
            )}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={(event) => {
                handleFiles(event.target.files);
                if (fileInputRef.current) fileInputRef.current.value = "";
              }}
            />
          </FieldCell>

          <FieldCell label="发布时间">
            <ScheduledTimePicker value={scheduledAt} onChange={setScheduledAt} />
          </FieldCell>

          <FieldCell label="自动排版">
            <ToggleSwitch checked={autoFormat} onChange={setAutoFormat} />
          </FieldCell>
        </div>

        {attachments.length > 0 ? (
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[11px] font-medium text-gray-500">
            {attachments.map((file, index) => (
              <span key={`${file.name}-${index}`} className="inline-flex items-center gap-1.5">
                <span className="text-gray-700">{file.name}</span>
                <span className="text-gray-400">{(file.size / 1024).toFixed(1)} KB</span>
                <button
                  type="button"
                  onClick={() => removeAttachment(index)}
                  className="text-gray-300 transition-colors hover:text-rose-500"
                  aria-label="移除附件"
                >
                  <X className="h-3 w-3" strokeWidth={2.4} />
                </button>
              </span>
            ))}
          </div>
        ) : null}

        {/* Remarks */}
        <div className="mt-4 flex items-baseline gap-5">
          <span className="shrink-0 text-[11px] font-bold uppercase tracking-[0.16em] text-gray-400">备注</span>
          <input
            value={remarks}
            onChange={(event) => setRemarks(event.target.value)}
            placeholder="附加说明，例如发布时段、配图要求等"
            className="flex-1 border-0 bg-transparent p-0 text-[13px] text-gray-900 outline-none placeholder:text-gray-300"
          />
        </div>

        {/* Submit footer */}
        <div className="mt-4 flex items-center justify-between gap-6 border-t border-gray-200/70 pt-3">
          <div className="flex min-h-10 min-w-0 flex-1 flex-col justify-center text-[12px] font-medium text-gray-500">
            {selectedMedia.length > 0 ? (
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="shrink-0">
                  预估费用 <span className="text-[13px] font-bold text-gray-900">{formatPrice(selectedTotal)}</span>
                </span>
                <span className="shrink-0 text-gray-300">·</span>
                <span className="min-w-0 flex-1 truncate text-gray-700">
                  {selectedMedia
                    .slice(0, 3)
                    .map((media) => media.name)
                    .join("、")}
                  {selectedMedia.length > 3 ? ` 等 ${selectedMedia.length} 家` : ""}
                </span>
              </div>
            ) : (
              <span className="text-gray-400">尚未选择投放渠道</span>
            )}
            {submitMessage ? <div className="mt-1 text-rose-600">{submitMessage}</div> : null}
          </div>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={submitting}
            className="group inline-flex h-10 shrink-0 items-center gap-2 pt-px text-[13px] font-bold text-gray-900 transition-colors hover:text-black disabled:cursor-not-allowed disabled:text-gray-300"
          >
            {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {submitting ? "提交中" : "提交发布"}
            {!submitting ? <ChevronRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" /> : null}
          </button>
        </div>
        </div>
        </div>
        </ScrollArea>
        </div>
      </div>

      <MediaPickerDrawer
        open={showMediaPicker}
        onClose={() => setShowMediaPicker(false)}
        selected={selectedMedia}
        onToggle={toggleMedia}
        onUpdate={updateSelected}
        onRemove={removeSelected}
      />

      <BrandPickerDrawer
        open={showBrandPicker}
        onClose={() => setShowBrandPicker(false)}
        selected={client}
        brands={brandSummaries}
        onSelect={(name) => {
          setClient(name);
          setShowBrandPicker(false);
        }}
      />

      <StatusDrawer
        open={showStatusPanel}
        onClose={() => setShowStatusPanel(false)}
        records={records}
        todayCount={todayPublishedCount}
        inFlightCount={inFlightCount}
        onEdit={(record) => setEditingRecord(record)}
        onDelete={(record) => setConfirmDelete(record)}
      />

      <RecordEditDialog
        record={editingRecord}
        brands={brandSummaries}
        onClose={() => setEditingRecord(null)}
        onSave={(patch) => {
          if (editingRecord) updateRecord(editingRecord.id, patch);
          setEditingRecord(null);
        }}
      />

      <ConfirmDeleteDialog
        record={confirmDelete}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => {
          if (confirmDelete) removeRecord(confirmDelete.id);
          setConfirmDelete(null);
        }}
      />
    </main>
  );
}

function FieldCell({
  label,
  children,
  onClick,
}: {
  label: string;
  children: React.ReactNode;
  onClick?: () => void;
}) {
  const interactive = !!onClick;
  return (
    <div
      onClick={onClick}
      className={`flex min-w-0 flex-col gap-1.5 ${interactive ? "cursor-pointer" : ""}`}
    >
      <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-gray-400">{label}</span>
      <div className="min-w-0 truncate text-[13px] font-semibold text-gray-900">{children}</div>
    </div>
  );
}

function ToggleSwitch({ checked, onChange }: { checked: boolean; onChange: (next: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-[18px] w-[32px] items-center rounded-full transition-colors ${
        checked ? "bg-gray-900" : "bg-gray-200"
      }`}
      aria-pressed={checked}
    >
      <span
        className={`inline-block h-[14px] w-[14px] transform rounded-full bg-white shadow-sm transition-transform ${
          checked ? "translate-x-[16px]" : "translate-x-[2px]"
        }`}
      />
    </button>
  );
}

/* ---------- Media picker drawer ---------- */

function MediaPickerDrawer({
  open,
  onClose,
  selected,
  onToggle,
  onUpdate,
  onRemove,
}: {
  open: boolean;
  onClose: () => void;
  selected: SelectedMedia[];
  onToggle: (media: MediaItem) => void;
  onUpdate: (id: string, patch: Partial<SelectedMedia>) => void;
  onRemove: (id: string) => void;
}) {
  const [category, setCategory] = useState<MediaCategory>("media");
  const [query, setQuery] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return MEDIA_CATALOG.filter((item) => item.category === category).filter((item) => {
      if (!q) return true;
      return (
        item.name.toLowerCase().includes(q) ||
        item.channel.toLowerCase().includes(q) ||
        item.tags.some((tag) => tag.toLowerCase().includes(q))
      );
    });
  }, [category, query]);

  const total = useMemo(
    () => selected.reduce((sum, item) => sum + (item.priceOverride ?? item.price), 0),
    [selected],
  );

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button type="button" aria-label="关闭" onClick={onClose} className="absolute inset-0 bg-gray-900/30 backdrop-blur-[2px]" />
      <div className="relative flex h-full w-full max-w-[520px] flex-col bg-[#fcfdff]">
        {/* Header */}
        <div className="flex shrink-0 items-end justify-between px-8 pb-5 pt-8">
          <div className="flex flex-col gap-1.5">
            <h2 className="app-wordmark-heading text-[18px]">选择媒体</h2>
            <span className="text-[12px] font-medium text-gray-500">
              已选 <span className="font-bold text-gray-900">{selected.length}</span> 家 · 合计 <span className="font-bold text-gray-900">{formatPrice(total)}</span>
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-[12px] font-bold text-gray-900 transition-colors hover:text-black"
          >
            完成
          </button>
        </div>

        {/* Selected chips strip */}
        {selected.length > 0 ? (
          <div className="shrink-0 border-t border-gray-200/70 px-8 py-4">
            <ul className="flex flex-col gap-2">
              {selected.map((item) => {
                const price = item.priceOverride ?? item.price;
                const isEditing = editingId === item.id;
                return (
                  <li key={item.id} className="group flex items-center justify-between gap-3 text-[12px]">
                    <div className="flex min-w-0 flex-1 items-baseline gap-2">
                      <span className="truncate font-semibold text-gray-900">{item.name}</span>
                      {item.note ? <span className="truncate text-[11px] text-gray-400">备注：{item.note}</span> : null}
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      {isEditing ? (
                        <InlinePriceEditor
                          media={item}
                          onSave={(patch) => {
                            onUpdate(item.id, patch);
                            setEditingId(null);
                          }}
                          onCancel={() => setEditingId(null)}
                        />
                      ) : (
                        <>
                          <span className={`tabular-nums ${item.priceOverride !== undefined ? "font-bold text-blue-600" : "font-semibold text-gray-700"}`}>
                            {formatPrice(price)}
                          </span>
                          <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                            <button
                              type="button"
                              onClick={() => setEditingId(item.id)}
                              className="text-gray-400 transition-colors hover:text-gray-900"
                              title="修改"
                            >
                              <Pencil className="h-3 w-3" strokeWidth={2.4} />
                            </button>
                            <button
                              type="button"
                              onClick={() => onRemove(item.id)}
                              className="text-gray-400 transition-colors hover:text-rose-500"
                              title="移除"
                            >
                              <X className="h-3.5 w-3.5" strokeWidth={2.4} />
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : null}

        {/* Tabs + search */}
        <div className="shrink-0 border-t border-gray-200/70 px-8 pt-5">
          <div className="flex items-center gap-6">
            {(Object.keys(CATEGORY_LABEL) as MediaCategory[]).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setCategory(value)}
                className={`relative pb-3 text-[13px] font-semibold transition-colors ${
                  category === value ? "text-gray-900" : "text-gray-400 hover:text-gray-700"
                }`}
              >
                {CATEGORY_LABEL[value]}
                {category === value ? <span className="absolute inset-x-0 -bottom-px h-[2px] bg-gray-900" /> : null}
              </button>
            ))}
          </div>

          <div className="relative mt-2 flex items-center">
            <Search className="pointer-events-none absolute left-0 h-3.5 w-3.5 text-gray-400" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={`搜索${CATEGORY_LABEL[category]}名称、渠道或标签`}
              className="h-9 w-full border-0 bg-transparent pl-5 pr-2 text-[12px] text-gray-900 outline-none placeholder:text-gray-400"
            />
          </div>
        </div>

        {/* Results */}
        <div className="flex-1 overflow-y-auto px-8 pb-8 scrollbar-thin">
          {filtered.length === 0 ? (
            <div className="flex h-full items-center justify-center text-[12px] font-medium text-gray-400">无匹配媒体</div>
          ) : (
            <ul>
              {filtered.map((item) => {
                const isSelected = !!selected.find((s) => s.id === item.id);
                return (
                  <li key={item.id} className="border-b border-gray-100">
                    <button
                      type="button"
                      onClick={() => onToggle(item)}
                      className={`flex w-full items-center justify-between gap-4 py-3.5 text-left transition-colors ${
                        isSelected ? "" : "hover:bg-gray-50/60"
                      }`}
                    >
                      <div className="flex min-w-0 flex-1 flex-col gap-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-[13px] font-semibold text-gray-900">{item.name}</span>
                          {item.tags[0] ? (
                            <span className="text-[10px] font-bold text-gray-400">{item.tags[0]}</span>
                          ) : null}
                        </div>
                        <div className="flex items-center gap-1.5 text-[11px] font-medium text-gray-400">
                          <span>{item.channel}</span>
                          <span className="inline-block h-[3px] w-[3px] rounded-full bg-gray-300" />
                          <span>{item.turnaround}</span>
                        </div>
                      </div>
                      <div className="flex shrink-0 items-center gap-3">
                        <span className="tabular-nums text-[13px] font-semibold text-gray-900">{formatPrice(item.price)}</span>
                        <span
                          className={`flex h-[16px] w-[16px] items-center justify-center rounded-full transition-colors ${
                            isSelected ? "bg-gray-900 text-white" : "border border-gray-300 text-transparent"
                          }`}
                        >
                          <CheckCircle2 className="h-3 w-3" strokeWidth={2.6} />
                        </span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function InlinePriceEditor({
  media,
  onSave,
  onCancel,
}: {
  media: SelectedMedia;
  onSave: (patch: Partial<SelectedMedia>) => void;
  onCancel: () => void;
}) {
  const [price, setPrice] = useState(String(media.priceOverride ?? media.price));
  const [note, setNote] = useState(media.note || "");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const handleSave = () => {
    const parsed = Number(price);
    const isValid = Number.isFinite(parsed) && parsed >= 0;
    const override = isValid && parsed !== media.price ? parsed : undefined;
    onSave({
      priceOverride: override,
      note: note.trim() || undefined,
    });
  };

  return (
    <div className="flex items-center gap-2">
      <span className="text-gray-400">¥</span>
      <input
        ref={inputRef}
        type="number"
        min={0}
        value={price}
        onChange={(event) => setPrice(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") handleSave();
          if (event.key === "Escape") onCancel();
        }}
        className="w-20 border-b border-gray-300 bg-transparent p-0 text-right text-[12px] font-semibold text-gray-900 outline-none focus:border-gray-900"
      />
      <input
        value={note}
        onChange={(event) => setNote(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") handleSave();
          if (event.key === "Escape") onCancel();
        }}
        placeholder="备注"
        className="w-24 border-b border-gray-300 bg-transparent p-0 text-[11px] text-gray-700 outline-none placeholder:text-gray-300 focus:border-gray-900"
      />
      <button type="button" onClick={handleSave} className="text-[11px] font-bold text-gray-900">
        保存
      </button>
      <button type="button" onClick={onCancel} className="text-[11px] font-bold text-gray-400">
        取消
      </button>
    </div>
  );
}

/* ---------- Brand picker drawer ---------- */

function BrandPickerDrawer({
  open,
  onClose,
  selected,
  brands,
  onSelect,
  zIndex = 50,
}: {
  open: boolean;
  onClose: () => void;
  selected: string;
  brands: BrandSummary[];
  onSelect: (name: string) => void;
  zIndex?: number;
}) {
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (open) setQuery("");
  }, [open]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return brands;
    return brands.filter((brand) => brand.name.toLowerCase().includes(q));
  }, [brands, query]);

  const totals = useMemo(
    () =>
      brands.reduce(
        (acc, brand) => {
          acc.weekly += brand.weeklyTotal;
          acc.total += brand.totalSpend;
          return acc;
        },
        { weekly: 0, total: 0 },
      ),
    [brands],
  );

  if (!open) return null;
  return (
    <div className="fixed inset-0 flex justify-end" style={{ zIndex }}>
      <button type="button" aria-label="关闭" onClick={onClose} className="absolute inset-0 bg-gray-900/30 backdrop-blur-[2px]" />
      <div className="relative flex h-full w-full max-w-[520px] flex-col bg-[#fcfdff]">
        {/* Header */}
        <div className="flex shrink-0 items-end justify-between px-8 pb-5 pt-8">
          <div className="flex flex-col gap-1.5">
            <h2 className="app-wordmark-heading text-[18px]">所属客户</h2>
            <span className="text-[12px] font-medium text-gray-500">
              共 <span className="font-bold text-gray-900">{brands.length}</span> 个权限范围内品牌 · 本周 <span className="font-bold text-gray-900">{formatPrice(totals.weekly)}</span> / 总累计 <span className="font-bold text-gray-900">{formatPrice(totals.total)}</span>
            </span>
          </div>
          <button type="button" onClick={onClose} className="text-[12px] font-bold text-gray-900 transition-colors hover:text-black">
            关闭
          </button>
        </div>

        {/* Search */}
        <div className="shrink-0 border-t border-gray-200/70 px-8 pt-5">
          <div className="relative flex items-center">
            <Search className="pointer-events-none absolute left-0 h-3.5 w-3.5 text-gray-400" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索品牌名称"
              className="h-9 w-full border-0 bg-transparent pl-5 pr-2 text-[12px] text-gray-900 outline-none placeholder:text-gray-400"
            />
          </div>
        </div>

        {/* List */}
        <div className="flex-1 overflow-y-auto px-8 pb-8 scrollbar-thin">
          {brands.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
              <Sparkles className="h-7 w-7 text-gray-300" />
              <span className="text-[12px] font-semibold text-gray-400">当前账号下还没有可选品牌</span>
              <span className="text-[11px] text-gray-400">请在「品牌」页面创建或申请权限</span>
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex h-full items-center justify-center text-[12px] font-medium text-gray-400">无匹配品牌</div>
          ) : (
            <ul>
              {filtered.map((brand) => {
                const isSelected = selected === brand.name;
                return (
                  <li key={brand.name} className="border-b border-gray-100">
                    <button
                      type="button"
                      onClick={() => onSelect(brand.name)}
                      className={`flex w-full items-center justify-between gap-4 py-4 text-left transition-colors ${
                        isSelected ? "" : "hover:bg-gray-50/60"
                      }`}
                    >
                      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                        <span className="truncate text-[14px] font-semibold text-gray-900">{brand.name}</span>
                        <div className="flex items-center gap-3 text-[11px] font-medium text-gray-400">
                          <span>
                            本周投放 <span className="tabular-nums font-semibold text-gray-700">{formatPrice(brand.weeklyTotal)}</span>
                          </span>
                          <span className="inline-block h-[3px] w-[3px] rounded-full bg-gray-300" />
                          <span>
                            总累计 <span className="tabular-nums font-semibold text-gray-700">{formatPrice(brand.totalSpend)}</span>
                          </span>
                          {brand.publishCount > 0 ? (
                            <>
                              <span className="inline-block h-[3px] w-[3px] rounded-full bg-gray-300" />
                              <span>
                                <span className="tabular-nums font-semibold text-gray-700">{brand.publishCount}</span> 篇
                              </span>
                            </>
                          ) : null}
                        </div>
                      </div>
                      <span
                        className={`flex h-[18px] w-[18px] items-center justify-center rounded-full transition-colors ${
                          isSelected ? "bg-gray-900 text-white" : "border border-gray-300 text-transparent"
                        }`}
                      >
                        <Check className="h-3 w-3" strokeWidth={2.8} />
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

/* ---------- Status drawer ---------- */

function StatusDrawer({
  open,
  onClose,
  records,
  todayCount,
  inFlightCount,
  onEdit,
  onDelete,
}: {
  open: boolean;
  onClose: () => void;
  records: PublishRecord[];
  todayCount: number;
  inFlightCount: number;
  onEdit: (record: PublishRecord) => void;
  onDelete: (record: PublishRecord) => void;
}) {
  const [filter, setFilter] = useState<"all" | PublishStatus>("all");

  const filtered = useMemo(
    () => (filter === "all" ? records : records.filter((record) => record.status === filter)),
    [filter, records],
  );

  const counts = useMemo<Record<"all" | PublishStatus, number>>(
    () => ({
      all: records.length,
      submitting: records.filter((r) => r.status === "submitting").length,
      publishing: records.filter((r) => r.status === "publishing").length,
      published: records.filter((r) => r.status === "published").length,
      failed: records.filter((r) => r.status === "failed").length,
    }),
    [records],
  );

  const grouped = useMemo(() => {
    const sorted = [...filtered].sort((a, b) => b.createdAt - a.createdAt);
    const map = new Map<string, PublishRecord[]>();
    for (const record of sorted) {
      const key = recordDateKey(record.createdAt);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(record);
    }
    const now = Date.now();
    return Array.from(map.entries()).map(([key, items]) => ({
      key,
      label: recordDateLabel(items[0].createdAt),
      sub: recordDateSubLabel(items[0].createdAt, now),
      items,
    }));
  }, [filtered]);

  if (!open) return null;

  const FILTER_ITEMS: Array<{ key: "all" | PublishStatus; label: string }> = [
    { key: "all", label: "全部" },
    { key: "submitting", label: "提交中" },
    { key: "publishing", label: "发布中" },
    { key: "published", label: "已发布" },
    { key: "failed", label: "失败" },
  ];

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button type="button" aria-label="关闭" onClick={onClose} className="absolute inset-0 bg-gray-900/30 backdrop-blur-[2px]" />
      <div className="relative flex h-full w-full max-w-[640px] flex-col bg-[#fcfdff]">
        <div className="flex shrink-0 items-end justify-between px-10 pb-5 pt-8">
          <div className="flex flex-col gap-1.5">
            <h2 className="app-wordmark-heading text-[18px]">发布情况</h2>
            <span className="text-[12px] font-medium text-gray-500">
              今日 <span className="font-bold text-gray-900">{todayCount}</span> 篇 · 进行中 <span className="font-bold text-gray-900">{inFlightCount}</span> 条
            </span>
          </div>
          <button type="button" onClick={onClose} className="text-[12px] font-bold text-gray-900 transition-colors hover:text-black">
            关闭
          </button>
        </div>

        <div className="flex shrink-0 items-center gap-6 border-t border-gray-200/70 px-10 pt-4">
          {FILTER_ITEMS.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setFilter(item.key)}
              className={`relative pb-3 text-[12px] font-semibold transition-colors ${
                filter === item.key ? "text-gray-900" : "text-gray-400 hover:text-gray-700"
              }`}
            >
              {item.label} <span className="text-gray-400">{counts[item.key]}</span>
              {filter === item.key ? <span className="absolute inset-x-0 -bottom-px h-[2px] bg-gray-900" /> : null}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-10 pb-8 pt-1 scrollbar-thin">
          {filtered.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
              <Sparkles className="h-7 w-7 text-gray-300" />
              <span className="text-[12px] font-semibold text-gray-400">暂无发布记录</span>
            </div>
          ) : (
            <div className="flex flex-col">
              {grouped.map((group) => (
                <section key={group.key} className="flex flex-col">
                  <div className="sticky top-0 z-10 flex items-center justify-between bg-[#fcfdff]/95 py-2.5 backdrop-blur-sm">
                    <span className="flex items-baseline gap-2">
                      <span className="text-[11px] font-bold uppercase tracking-[0.16em] text-gray-500 tabular-nums">
                        {group.label}
                      </span>
                      {group.sub ? (
                        <span className="text-[10px] font-semibold text-gray-300">{group.sub}</span>
                      ) : null}
                    </span>
                    <span className="text-[11px] font-bold text-gray-400">{group.items.length} 篇</span>
                  </div>
                  <ul>
                    {group.items.map((record) => (
                      <RecordRow key={record.id} record={record} onEdit={() => onEdit(record)} onDelete={() => onDelete(record)} />
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function RecordRow({ record, onEdit, onDelete }: { record: PublishRecord; onEdit: () => void; onDelete: () => void }) {
  return (
    <li className="group border-b border-gray-100 py-5">
      <div className="flex items-start justify-between gap-6">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2.5">
            <StatusInline status={record.status} />
            <span className="text-[13px] font-bold text-gray-900">{record.mediaName}</span>
            <span className="text-[10px] font-bold uppercase tracking-[0.14em] text-gray-400">
              {CATEGORY_LABEL[record.mediaCategory]}
            </span>
          </div>
          <div className="truncate text-[13px] font-semibold text-gray-800">{record.title}</div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-medium text-gray-400">
            <span>{record.client}</span>
            <span>{formatPrice(record.price)}</span>
            <span>创建 {formatTime(record.createdAt)}</span>
            {record.scheduledAt ? <span>定时 {formatTime(record.scheduledAt)}</span> : null}
            {record.publishedAt ? <span className="text-emerald-600">已发布 {formatTime(record.publishedAt)}</span> : null}
            {record.attachments > 0 ? <span>附件 {record.attachments}</span> : null}
          </div>
          {record.remarks ? <div className="text-[11px] text-gray-500">备注：{record.remarks}</div> : null}
          {record.errorMessage ? (
            <div className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-rose-600">
              <AlertCircle className="h-3 w-3" />
              {record.errorMessage}
            </div>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-4 pt-1 text-[11px] font-bold text-gray-400 opacity-0 transition-opacity group-hover:opacity-100">
          <button type="button" onClick={onEdit} className="inline-flex items-center gap-1 transition-colors hover:text-gray-900">
            <Pencil className="h-3 w-3" strokeWidth={2.4} />
            修改
          </button>
          <button type="button" onClick={onDelete} className="inline-flex items-center gap-1 transition-colors hover:text-rose-500">
            <Trash2 className="h-3 w-3" strokeWidth={2.4} />
            删除
          </button>
        </div>
      </div>
    </li>
  );
}

function StatusInline({ status }: { status: PublishStatus }) {
  const animate = status === "submitting" || status === "publishing";
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] font-bold text-gray-700">
      <span className={`relative inline-flex h-2 w-2 items-center justify-center`}>
        <span className={`absolute inline-flex h-full w-full rounded-full opacity-60 ${STATUS_DOT[status]} ${animate ? "animate-ping" : ""}`} />
        <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${STATUS_DOT[status]}`} />
      </span>
      {STATUS_LABEL[status]}
    </span>
  );
}

/* ---------- Record edit / delete dialogs ---------- */

function RecordEditDialog({
  record,
  brands,
  onClose,
  onSave,
}: {
  record: PublishRecord | null;
  brands: BrandSummary[];
  onClose: () => void;
  onSave: (patch: Partial<PublishRecord>) => void;
}) {
  const [title, setTitle] = useState("");
  const [client, setClient] = useState("");
  const [remarks, setRemarks] = useState("");
  const [scheduledAt, setScheduledAt] = useState("");
  const [showBrandPicker, setShowBrandPicker] = useState(false);

  useEffect(() => {
    if (record) {
      setTitle(record.title);
      setClient(record.client);
      setRemarks(record.remarks || "");
      setScheduledAt(
        record.scheduledAt
          ? new Date(record.scheduledAt - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)
          : "",
      );
      setShowBrandPicker(false);
    }
  }, [record]);

  if (!record) return null;
  return (
    <DialogShell title="修改发布记录" subtitle={record.mediaName} onClose={onClose}>
      <div className="flex flex-col gap-5 px-8 pb-6">
        <DialogField label="标题">
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            className="w-full border-b border-gray-200 bg-transparent p-0 pb-1.5 text-[14px] font-semibold text-gray-900 outline-none focus:border-gray-900"
          />
        </DialogField>
        <DialogField label="所属客户">
          <button
            type="button"
            onClick={() => setShowBrandPicker(true)}
            className="flex w-full items-center justify-between gap-2 border-b border-gray-200 pb-1.5 text-left transition-colors hover:border-gray-900"
          >
            <span className={`truncate text-[13px] font-semibold ${client ? "text-gray-900" : "text-gray-300"}`}>
              {client || "选择品牌"}
            </span>
            <ChevronRight className="h-3 w-3 shrink-0 text-gray-300" />
          </button>
        </DialogField>
        <DialogField label="发布时间">
          <ScheduledTimePicker value={scheduledAt} onChange={setScheduledAt} align="end" />
        </DialogField>
        <DialogField label="备注">
          <input
            value={remarks}
            onChange={(event) => setRemarks(event.target.value)}
            className="w-full border-b border-gray-200 bg-transparent p-0 pb-1.5 text-[13px] text-gray-900 outline-none focus:border-gray-900"
          />
        </DialogField>
      </div>
      <div className="flex justify-end gap-6 px-8 pb-7">
        <button type="button" onClick={onClose} className="text-[12px] font-bold text-gray-400 transition-colors hover:text-gray-900">
          取消
        </button>
        <button
          type="button"
          onClick={() =>
            onSave({
              title: title.trim() || record.title,
              client: client || record.client,
              remarks: remarks.trim(),
              scheduledAt: scheduledAt ? new Date(scheduledAt).getTime() : null,
            })
          }
          className="inline-flex items-center gap-1 text-[12px] font-bold text-gray-900 transition-colors hover:text-black"
        >
          保存修改
          <ChevronRight className="h-3 w-3" />
        </button>
      </div>

      <BrandPickerDrawer
        open={showBrandPicker}
        onClose={() => setShowBrandPicker(false)}
        selected={client}
        brands={brands}
        onSelect={(name) => {
          setClient(name);
          setShowBrandPicker(false);
        }}
        zIndex={70}
      />
    </DialogShell>
  );
}

function ConfirmDeleteDialog({
  record,
  onClose,
  onConfirm,
}: {
  record: PublishRecord | null;
  onClose: () => void;
  onConfirm: () => void;
}) {
  if (!record) return null;
  return (
    <DialogShell title="确认删除" subtitle="删除后无法恢复" onClose={onClose}>
      <div className="px-8 pb-6 text-[13px] leading-6 text-gray-600">
        将删除「{record.mediaName}」的发布记录：
        <span className="ml-1 font-bold text-gray-900">{record.title}</span>
        <div className="mt-2 text-[11px] text-gray-400">创建于 {formatTimeFull(record.createdAt)}</div>
      </div>
      <div className="flex justify-end gap-6 px-8 pb-7">
        <button type="button" onClick={onClose} className="text-[12px] font-bold text-gray-400 transition-colors hover:text-gray-900">
          取消
        </button>
        <button type="button" onClick={onConfirm} className="text-[12px] font-bold text-rose-600 transition-colors hover:text-rose-700">
          删除
        </button>
      </div>
    </DialogShell>
  );
}

function DialogField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-2">
      <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-gray-400">{label}</span>
      {children}
    </label>
  );
}

function DialogShell({
  title,
  subtitle,
  onClose,
  children,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center px-6">
      <button type="button" aria-label="关闭" onClick={onClose} className="absolute inset-0 bg-gray-900/30 backdrop-blur-[2px]" />
      <div className="relative w-full max-w-[440px] bg-[#fcfdff] shadow-[0_30px_120px_-30px_rgba(15,23,42,0.5)]">
        <div className="flex items-start justify-between px-8 pb-5 pt-7">
          <div className="flex flex-col gap-1">
            <h3 className="text-[15px] font-bold text-gray-900">{title}</h3>
            {subtitle ? <span className="text-[11px] font-medium text-gray-400">{subtitle}</span> : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 transition-colors hover:text-gray-900"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/* ---------- Scheduled time picker (popover) ---------- */

function ScheduledTimePicker({
  value,
  onChange,
  align = "start",
}: {
  value: string;
  onChange: (next: string) => void;
  align?: "start" | "center" | "end";
}) {
  const [open, setOpen] = useState(false);
  const parsed = useMemo(() => parseScheduledValue(value), [value]);
  const baseDate = parsed ?? suggestedScheduledTime();
  const [viewMonth, setViewMonth] = useState<Date>(baseDate);

  useEffect(() => {
    if (open) {
      setViewMonth(parsed ?? suggestedScheduledTime());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const writeBack = (next: Date) => {
    onChange(toScheduledValue(next));
  };

  const handleSelectDate = (date: Date | undefined) => {
    if (!date) return;
    const merged = new Date(baseDate);
    merged.setFullYear(date.getFullYear(), date.getMonth(), date.getDate());
    writeBack(merged);
  };

  const handleHourChange = (nextHour: number) => {
    const merged = new Date(baseDate);
    merged.setHours(nextHour);
    writeBack(merged);
  };

  const handleMinuteChange = (nextMinute: number) => {
    const merged = new Date(baseDate);
    merged.setMinutes(nextMinute);
    writeBack(merged);
  };

  const handleQuick = (offsetMinutes: number) => {
    const next = new Date();
    next.setSeconds(0);
    next.setMilliseconds(0);
    next.setMinutes(next.getMinutes() + offsetMinutes);
    const rem = next.getMinutes() % 5;
    if (rem !== 0) next.setMinutes(next.getMinutes() + (5 - rem));
    writeBack(next);
  };

  const handleClear = (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    onChange("");
    setOpen(false);
  };

  const yearMonthLabel = `${viewMonth.getFullYear()} 年 ${viewMonth.getMonth() + 1} 月`;
  const displayText = parsed ? formatScheduledDisplay(value) : "立即发布";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex w-full items-center gap-2 bg-transparent p-0 text-left text-[13px] font-semibold text-gray-900 outline-none"
        >
          <span className={`min-w-0 flex-1 truncate ${parsed ? "text-gray-900" : "text-gray-300"}`}>{displayText}</span>
          {parsed ? (
            <span
              role="button"
              aria-label="清除"
              tabIndex={-1}
              onClick={handleClear}
              className="shrink-0 cursor-pointer text-gray-300 transition-colors hover:text-rose-500"
            >
              <X className="h-3 w-3" strokeWidth={2.4} />
            </span>
          ) : (
            <ChevronDown className="h-3 w-3 shrink-0 text-gray-300" />
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align={align}
        sideOffset={10}
        className="w-[320px] rounded-[22px] border-gray-200/90 bg-[linear-gradient(180deg,rgba(255,255,255,0.98),rgba(249,250,251,0.96))] p-3 shadow-[0_24px_56px_-28px_rgba(15,23,42,0.28)] backdrop-blur-sm"
      >
        {/* Header */}
        <div className="mb-2.5 rounded-[18px] border border-gray-100/90 bg-[linear-gradient(135deg,rgba(248,250,252,0.96),rgba(255,255,255,1))] px-3 py-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-gray-400">发布时间</div>
              <div className="mt-1 truncate text-[13px] font-semibold text-gray-900">
                {parsed ? formatScheduledLong(value) : "立即发布"}
              </div>
              <div className="mt-1 text-[11px] font-medium text-gray-400">{yearMonthLabel}</div>
            </div>
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-2xl border border-gray-200/80 bg-white shadow-sm">
              <CalendarDays className="h-3.5 w-3.5 text-gray-500" />
            </div>
          </div>
        </div>

        {/* Calendar */}
        <div className="rounded-[18px] border border-gray-100/90 bg-white/90 p-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
          <DayPicker
            mode="single"
            selected={parsed || undefined}
            month={viewMonth}
            onMonthChange={setViewMonth}
            onSelect={handleSelectDate}
            captionLayout="dropdown"
            fromYear={new Date().getFullYear()}
            toYear={new Date().getFullYear() + 5}
            showOutsideDays
            weekStartsOn={1}
            className="w-full"
            classNames={{
              months: "w-full",
              month: "w-full space-y-2.5",
              caption: "pt-1",
              caption_dropdowns: "grid grid-cols-2 gap-1.5 px-1 items-center",
              dropdown_month: "relative",
              dropdown_year: "relative",
              dropdown: "absolute inset-0 z-10 w-full h-full opacity-0 cursor-pointer appearance-none",
              caption_label:
                "flex h-8 items-center justify-between rounded-xl border border-gray-200 bg-[linear-gradient(180deg,#ffffff_0%,#f9fafb_100%)] px-2.5 text-[11px] font-semibold text-gray-700 shadow-[0_8px_20px_-16px_rgba(15,23,42,0.3)]",
              dropdown_icon: "w-2.5 h-2.5 text-gray-300 shrink-0",
              vhidden: "sr-only",
              table: "w-full border-collapse",
              head_row: "grid grid-cols-7 mb-1",
              head_cell:
                "mx-auto w-8 h-6 rounded-lg bg-gray-50 text-[9px] font-bold tracking-[0.1em] text-gray-400 flex items-center justify-center uppercase",
              row: "grid grid-cols-7 gap-y-1",
              cell: "p-0 text-center",
              day: "w-8 h-8 mx-auto rounded-[14px] text-[11px] font-semibold text-gray-700 hover:bg-gray-100 hover:text-gray-900 transition-all",
              day_selected:
                "bg-gray-900 text-white hover:bg-gray-900 hover:text-white shadow-[0_14px_24px_-18px_rgba(17,24,39,0.95)]",
              day_today: "text-blue-600 bg-blue-50 ring-1 ring-blue-100",
              day_outside: "text-gray-300 opacity-70",
              day_disabled: "text-gray-200 opacity-60",
              nav: "hidden",
            }}
          />
        </div>

        {/* Time */}
        <div className="mt-2.5 rounded-[18px] border border-gray-100/90 bg-white/90 p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
          <div className="flex items-center justify-between gap-3">
            <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-gray-400">时间</span>
            <div className="flex items-center gap-2 text-[20px] font-semibold tabular-nums text-gray-900">
              <TimeWheel value={baseDate.getHours()} length={24} step={1} onChange={handleHourChange} />
              <span className="text-gray-300">:</span>
              <TimeWheel value={Math.floor(baseDate.getMinutes() / 5) * 5} length={60} step={5} onChange={handleMinuteChange} />
            </div>
          </div>

          {/* Quick shortcuts */}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <QuickButton onClick={() => handleQuick(30)}>30 分钟后</QuickButton>
            <QuickButton onClick={() => handleQuick(60)}>1 小时后</QuickButton>
            <QuickButton
              onClick={() => {
                const tomorrow = new Date();
                tomorrow.setDate(tomorrow.getDate() + 1);
                tomorrow.setHours(9, 0, 0, 0);
                writeBack(tomorrow);
              }}
            >
              明早 9 点
            </QuickButton>
          </div>
        </div>

        {/* Footer */}
        <div className="mt-2.5 flex items-center justify-between gap-3 border-t border-gray-100/90 pt-2.5">
          <button
            type="button"
            onClick={() => {
              onChange("");
              setOpen(false);
            }}
            className="text-[11px] font-bold text-gray-500 transition-colors hover:text-gray-900"
          >
            立即发布
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="rounded-xl bg-gray-900 px-3 py-1.5 text-[11px] font-bold text-white transition-colors hover:bg-black"
          >
            完成
          </button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function TimeWheel({
  value,
  length,
  step,
  onChange,
}: {
  value: number;
  length: number;
  step: number;
  onChange: (next: number) => void;
}) {
  const options = useMemo(() => {
    const arr: number[] = [];
    for (let i = 0; i < length; i += step) arr.push(i);
    return arr;
  }, [length, step]);

  return (
    <div className="relative flex h-8 min-w-[44px] items-center justify-center rounded-xl border border-gray-200/80 bg-white shadow-[inset_0_1px_0_rgba(255,255,255,0.9)]">
      <span className="pointer-events-none px-2 tabular-nums">{pad2(value)}</span>
      <select
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="absolute inset-0 h-full w-full cursor-pointer appearance-none opacity-0"
        aria-label="时间"
      >
        {options.map((n) => (
          <option key={n} value={n}>
            {pad2(n)}
          </option>
        ))}
      </select>
    </div>
  );
}

function QuickButton({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-full border border-gray-200/80 bg-white px-2.5 py-1 text-[10px] font-bold text-gray-600 transition-colors hover:border-gray-900 hover:text-gray-900"
    >
      {children}
    </button>
  );
}
