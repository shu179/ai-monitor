import { useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, ChevronDown, Copy, Download, Image as ImageIcon, Loader2, Maximize2, Sparkles, X } from "lucide-react";
import {
  fetchImageGenerationConfig,
  generateImage,
  type GeneratedImage,
  type ImageGenerationConfig,
  type ImageGenerationModelId,
} from "../lib/backend";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import surfacedOtterDemo from "../../assets/surfaced-otter-demo.png";

type SelectOption = {
  value: string;
  label: string;
};

type GeneratedImageRecord = {
  id: string;
  image: GeneratedImage;
  modelLabel: string;
  prompt: string;
  request: Record<string, unknown> | null;
  createdAt: string;
};

type PreviewImageState = {
  src: string;
  title: string;
  subtitle?: string;
  image?: GeneratedImage;
  outputFormat?: string;
};

type AspectDisplay = {
  aspectRatio: string;
  maxWidth: number;
};

const ASPECT_OPTIONS: SelectOption[] = [
  { value: "1:1", label: "1:1" },
  { value: "3:2", label: "3:2" },
  { value: "2:3", label: "2:3" },
  { value: "4:3", label: "4:3" },
  { value: "3:4", label: "3:4" },
  { value: "16:9", label: "16:9" },
  { value: "9:16", label: "9:16" },
];

const CANVAS_QUALITY_OPTIONS: SelectOption[] = [
  { value: "standard", label: "标准" },
  { value: "hd", label: "高清" },
  { value: "ultra", label: "超清" },
  { value: "auto", label: "自动" },
];

const GENERATION_QUALITY_OPTIONS: SelectOption[] = [
  { value: "auto", label: "自动" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

const OUTPUT_FORMAT_OPTIONS: SelectOption[] = [
  { value: "png", label: "PNG" },
  { value: "jpeg", label: "JPEG" },
  { value: "webp", label: "WebP" },
];

const IMAGE_SURFACE_SHADOW = [
  "0 14px 30px -24px rgba(15,23,42,0.32)",
  "7px 0 18px -18px rgba(15,23,42,0.2)",
  "-7px 0 18px -18px rgba(15,23,42,0.2)",
  "0 0 0 1px rgba(226,232,240,0.82)",
].join(", ");

const RESULT_TRACK_MAX_WIDTH = 520;

const FALLBACK_MODEL_OPTIONS = [
  {
    id: "nano_banana_2" as ImageGenerationModelId,
    label: "Nano Banana 2",
    provider: "gemini" as const,
    model: "nano-banana-2",
    configured: false,
    has_key: false,
  },
  {
    id: "gpt_image_2" as ImageGenerationModelId,
    label: "GPT Image 2",
    provider: "openai" as const,
    model: "gpt-image-2",
    configured: false,
    has_key: false,
  },
];

const EXAMPLE_PROMPT = "一个安静、明亮的现代办公桌面，桌上有浅色陶瓷杯、蓝色便签和一台打开的数据看板电脑，真实摄影风格，自然光，干净背景";

function imageExtension(image: GeneratedImage, fallback: string) {
  const mime = String(image.mime_type || "").toLowerCase();
  if (mime.includes("jpeg") || mime.includes("jpg")) return "jpg";
  if (mime.includes("webp")) return "webp";
  if (mime.includes("png")) return "png";
  return fallback === "jpeg" ? "jpg" : fallback;
}

function formatGeneratedTime(value: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(value));
  } catch {
    return "";
  }
}

function isSameLocalDate(value: string, date: Date) {
  const nextDate = new Date(value);
  if (Number.isNaN(nextDate.getTime())) return false;
  return (
    nextDate.getFullYear() === date.getFullYear()
    && nextDate.getMonth() === date.getMonth()
    && nextDate.getDate() === date.getDate()
  );
}

function imageSource(image: GeneratedImage) {
  return image.data_url || image.url || "";
}

function parseAspectRatio(value: unknown) {
  const [rawWidth, rawHeight] = String(value || "1:1").split(":");
  const width = Number.parseInt(rawWidth || "1", 10);
  const height = Number.parseInt(rawHeight || "1", 10);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return { width: 1, height: 1 };
  }
  return { width, height };
}

function aspectFromRequest(request: Record<string, unknown> | null) {
  if (request?.aspect_ratio) {
    return String(request.aspect_ratio);
  }
  const size = String(request?.size || "");
  const match = size.match(/^(\d+)x(\d+)$/i);
  if (match) {
    return `${match[1]}:${match[2]}`;
  }
  return "1:1";
}

function imageDisplayForAspect(aspect: string): AspectDisplay {
  const { width, height } = parseAspectRatio(aspect);
  const ratio = width / height;
  let maxWidth = 420;

  if (ratio >= 1.7) {
    maxWidth = 520;
  } else if (ratio >= 1.28) {
    maxWidth = 480;
  } else if (ratio <= 0.6) {
    maxWidth = 300;
  } else if (ratio <= 0.78) {
    maxWidth = 340;
  }

  return {
    aspectRatio: `${width} / ${height}`,
    maxWidth,
  };
}

async function imageSourceToBlob(src: string) {
  if (!src) return null;
  const response = await fetch(src);
  if (!response.ok) return null;
  return response.blob();
}

export function ImageGenerationContent() {
  const [config, setConfig] = useState<ImageGenerationConfig | null>(null);
  const [modelId, setModelId] = useState<ImageGenerationModelId>("gpt_image_2");

  const [prompt, setPrompt] = useState("");
  const [aspectRatio, setAspectRatio] = useState("1:1");
  const [canvasQuality, setCanvasQuality] = useState("standard");
  const [generationQuality, setGenerationQuality] = useState("auto");
  const [outputFormat, setOutputFormat] = useState("png");
  const [generating, setGenerating] = useState(false);
  const [resultMessage, setResultMessage] = useState("");
  const [imageHistory, setImageHistory] = useState<GeneratedImageRecord[]>([]);
  const [lastRequest, setLastRequest] = useState<Record<string, unknown> | null>(null);
  const [previewImage, setPreviewImage] = useState<PreviewImageState | null>(null);
  const resultScrollerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchImageGenerationConfig().then((nextConfig) => {
      if (cancelled) return;
      setConfig(nextConfig);
      setModelId(nextConfig.default_model_id);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const modelOptions = useMemo(() => config?.models || FALLBACK_MODEL_OPTIONS, [config]);
  const selectedModel = useMemo(
    () => modelOptions.find((model) => model.id === modelId) || modelOptions[0] || null,
    [modelId, modelOptions],
  );
  const latestRecord = imageHistory[0] || null;
  const selectedImage = latestRecord?.image || null;
  const todayGeneratedCount = useMemo(() => {
    const today = new Date();
    return imageHistory.filter((record) => isSameLocalDate(record.createdAt, today)).length;
  }, [imageHistory]);
  const resultSummary = generating
    ? "等待生成"
    : `今日生成 ${todayGeneratedCount} 张 · 累计生成 ${imageHistory.length} 张`;
  const resultMessageIsSuccess = resultMessage === "图片已复制" || resultMessage === "图片链接已复制";

  const handleGenerate = async () => {
    const text = prompt.trim();
    if (!text || generating) {
      setResultMessage(text ? "" : "请先填写提示词");
      return;
    }
    if (!selectedModel?.configured) {
      setResultMessage("这个模型还没在后端配置 URL / API Key");
      return;
    }
    setGenerating(true);
    setResultMessage("");
    setLastRequest(null);
    const result = await generateImage({
      model_id: modelId,
      prompt: text,
      aspect_ratio: aspectRatio,
      canvas_quality: canvasQuality,
      quality: generationQuality,
      output_format: outputFormat,
    });
    setGenerating(false);
    if (!result.ok || !result.images?.length) {
      setResultMessage(result.message || "没有生成图片");
      return;
    }
    const createdAt = new Date().toISOString();
    const newRecords = result.images.map((image, index) => ({
      id: `${createdAt}-${index}-${Math.random().toString(36).slice(2, 8)}`,
      image,
      modelLabel: result.label || selectedModel?.label || "生图模型",
      prompt: text,
      request: result.request || null,
      createdAt,
    }));
    setImageHistory((history) => [...newRecords, ...history].slice(0, 40));
    setLastRequest(result.request || null);
    setResultMessage("");
    window.setTimeout(() => {
      resultScrollerRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    }, 40);
  };

  const handleDownload = (image: GeneratedImage, fallbackFormat = outputFormat) => {
    const href = imageSource(image);
    if (!href) return;
    const link = document.createElement("a");
    link.href = href;
    link.download = `surfaced-image-${Date.now()}.${imageExtension(image, fallbackFormat)}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  };

  const handleCopyImage = async (image: GeneratedImage) => {
    const src = imageSource(image);
    if (!src) return;
    try {
      const blob = await imageSourceToBlob(src);
      if (blob && navigator.clipboard && typeof ClipboardItem !== "undefined") {
        await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
        setResultMessage("图片已复制");
        window.setTimeout(() => setResultMessage(""), 1600);
        return;
      }
    } catch {
      // Fall through to copying the image URL/data URL as text.
    }
    try {
      await navigator.clipboard.writeText(src);
      setResultMessage("图片链接已复制");
      window.setTimeout(() => setResultMessage(""), 1600);
    } catch {
      setResultMessage("复制失败，请手动下载图片");
    }
  };

  return (
    <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-transparent px-8 py-8 xl:px-10">
      <div className="absolute inset-0 bg-[#fcfdff]" />

      <div className="relative z-10 mb-6 flex shrink-0 items-end justify-between border-b border-gray-200/70 pb-6">
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-blue-600" />
            <h1 className="app-wordmark-heading text-[20px]">生图</h1>
          </div>
          <span className="text-[12px] font-medium tracking-wide text-gray-500">统一接口生成品牌与内容素材</span>
        </div>
        <ModelStatusChip configured={Boolean(selectedModel?.configured)} />
      </div>

      <div className="relative z-10 grid min-h-0 flex-1 grid-cols-1 gap-5 overflow-hidden xl:grid-cols-[minmax(340px,1.05fr)_minmax(280px,0.95fr)] 2xl:grid-cols-[minmax(380px,420px)_minmax(0,1fr)] 2xl:gap-6">
        <div className="min-h-0 overflow-y-auto pr-2 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent">
          <section className="border-b border-gray-200/80 pb-5">
            <SelectField
              label="模型"
              value={modelId}
              onChange={(value) => setModelId(value as ImageGenerationModelId)}
              options={modelOptions.map((model) => ({ value: model.id, label: model.label }))}
            />
            <div className="mt-3 flex items-center justify-between gap-3 text-[11px] font-medium text-gray-400">
              <span>{selectedModel?.model || ""}</span>
              <span>{selectedModel?.configured ? "后端已配置" : "后端未配置"}</span>
            </div>
          </section>

          <section className="pt-5">
            <label className="mb-2 block text-[11px] font-bold text-gray-500">提示词</label>
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder={EXAMPLE_PROMPT}
              className="min-h-[156px] w-full resize-none rounded-none border-0 border-b border-gray-200 bg-transparent px-0 py-2 text-[13px] leading-6 text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-gray-900"
            />

            <div className="mt-5 grid grid-cols-2 gap-3">
              <SelectField label="画幅" value={aspectRatio} onChange={setAspectRatio} options={ASPECT_OPTIONS} />
              <SelectField label="画质" value={canvasQuality} onChange={setCanvasQuality} options={CANVAS_QUALITY_OPTIONS} />
              <SelectField label="生图质量" value={generationQuality} onChange={setGenerationQuality} options={GENERATION_QUALITY_OPTIONS} />
              <SelectField label="格式" value={outputFormat} onChange={setOutputFormat} options={OUTPUT_FORMAT_OPTIONS} />
            </div>

            <button
              type="button"
              onClick={handleGenerate}
              disabled={generating}
              className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-5 py-3 text-[13px] font-bold text-white shadow-sm shadow-blue-600/20 transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {generating ? <Loader2 className="h-4 w-4 animate-spin" /> : <ImageIcon className="h-4 w-4" />}
              {generating ? "生成中..." : "生成图片"}
            </button>
            {resultMessage ? (
              <div
                className={`mt-3 text-[12px] font-semibold ${
                  resultMessageIsSuccess ? "text-emerald-600" : "text-rose-600"
                }`}
              >
                {resultMessage}
              </div>
            ) : null}
          </section>
        </div>

        <section className="flex min-h-0 min-w-0 flex-col overflow-hidden border-l border-gray-200/70 pl-5 2xl:pl-6">
          <div
            className="mx-auto mb-4 flex w-full shrink-0 items-center justify-between gap-4"
            style={{ maxWidth: RESULT_TRACK_MAX_WIDTH }}
          >
            <div>
              <h2 className="text-[13px] font-bold text-gray-900">生成结果</h2>
              <div className="mt-1 text-[11px] font-medium text-gray-400">{resultSummary}</div>
            </div>
            {selectedImage ? (
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => void handleCopyImage(selectedImage)}
                  className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-600 transition-colors hover:text-gray-900"
                >
                  <Copy className="h-4 w-4" />
                  复制
                </button>
                <button
                  type="button"
                  onClick={() => handleDownload(selectedImage, String(latestRecord?.request?.output_format || outputFormat))}
                  className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-600 transition-colors hover:text-gray-900"
                >
                  <Download className="h-4 w-4" />
                  下载
                </button>
              </div>
            ) : null}
          </div>

          <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden bg-transparent">
            <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex justify-center px-4">
              <div
                className="h-px w-full bg-gray-300/95 shadow-[0_1px_0_rgba(255,255,255,0.9)]"
                style={{ maxWidth: RESULT_TRACK_MAX_WIDTH }}
              />
            </div>
            <div
              ref={resultScrollerRef}
              className="relative z-10 h-full overflow-y-auto px-4 pb-10 pt-8 scrollbar-thin scrollbar-thumb-gray-300 scrollbar-track-transparent"
              style={{ scrollSnapType: imageHistory.length ? "y proximity" : undefined }}
            >
              {generating ? (
                <div className="mx-auto mb-5 flex w-full max-w-[560px] flex-col items-center gap-3 border border-gray-200/80 bg-white px-5 py-7 text-center shadow-[0_18px_46px_-42px_rgba(15,23,42,0.45)]">
                  <div className="relative h-11 w-24 overflow-hidden border-x border-b border-gray-200 bg-gray-50/80">
                    <div className="absolute left-1/2 top-0 h-8 w-16 -translate-x-1/2 animate-[polaroidTinyPrint_1.35s_ease-in-out_infinite] bg-white shadow-sm" />
                  </div>
                  <div className="flex items-center gap-2 text-[12px] font-bold text-gray-500">
                    <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
                    出片中...
                  </div>
                </div>
              ) : null}

              {imageHistory.length ? (
                <div className="mx-auto flex w-full max-w-[580px] flex-col gap-5">
                  {imageHistory.map((record, index) => (
                    <PolaroidImageCard
                      key={record.id}
                      record={record}
                      isNewest={index === 0}
                      outputFormat={outputFormat}
                      onPreview={(nextPreview) => setPreviewImage(nextPreview)}
                      onCopy={handleCopyImage}
                      onDownload={handleDownload}
                    />
                  ))}
                </div>
              ) : !generating ? (
                <div className="mx-auto flex w-full max-w-[580px] flex-col gap-5">
                  <DemoPrintCard onPreview={(nextPreview) => setPreviewImage(nextPreview)} />
                </div>
              ) : null}
            </div>
          </div>

          {latestRecord?.image.revised_prompt ? (
            <div className="mt-4 shrink-0 border-t border-gray-200/70 pt-3 text-[11px] leading-5 text-gray-500">
              {latestRecord.image.revised_prompt}
            </div>
          ) : null}
        </section>
      </div>
      <style>{`
        @keyframes polaroidPrintDown {
          0% {
            opacity: 0;
            transform: translateY(-92px) scale(0.96) rotate(-0.6deg);
            clip-path: inset(0 0 100% 0 round 8px);
          }
          48% {
            opacity: 1;
            clip-path: inset(0 0 18% 0 round 8px);
          }
          72% {
            transform: translateY(8px) scale(1.01) rotate(0.28deg);
            clip-path: inset(0 0 0 0 round 8px);
          }
          100% {
            opacity: 1;
            transform: translateY(0) scale(1) rotate(0deg);
            clip-path: inset(0 0 0 0 round 8px);
          }
        }
        @keyframes polaroidTinyPrint {
          0% { transform: translate(-50%, -34px); opacity: 0; }
          28% { opacity: 1; }
          100% { transform: translate(-50%, 46px); opacity: 0; }
        }
        .polaroid-print-new {
          animation: polaroidPrintDown 860ms cubic-bezier(0.2, 0.88, 0.2, 1) both;
          transform-origin: top center;
        }
      `}</style>
      {previewImage ? (
        <ImagePreviewModal
          preview={previewImage}
          onClose={() => setPreviewImage(null)}
          onCopy={previewImage.image ? () => handleCopyImage(previewImage.image as GeneratedImage) : undefined}
          onDownload={previewImage.image ? () => handleDownload(previewImage.image as GeneratedImage, previewImage.outputFormat || outputFormat) : undefined}
        />
      ) : null}
    </main>
  );
}

function PolaroidImageCard({
  record,
  isNewest,
  outputFormat,
  onPreview,
  onCopy,
  onDownload,
}: {
  record: GeneratedImageRecord;
  isNewest: boolean;
  outputFormat: string;
  onPreview: (preview: PreviewImageState) => void;
  onCopy: (image: GeneratedImage) => void | Promise<void>;
  onDownload: (image: GeneratedImage, fallbackFormat?: string) => void;
}) {
  const imageSrc = imageSource(record.image);
  const displaySize = record.request?.size ? String(record.request.size) : "";
  const recordOutputFormat = String(record.request?.output_format || outputFormat);
  const subtitle = [displaySize, formatGeneratedTime(record.createdAt)].filter(Boolean).join(" · ");
  const display = imageDisplayForAspect(aspectFromRequest(record.request));

  return (
    <div
      className={`mx-auto w-full scroll-mt-12 overflow-visible px-6 pb-9 pt-3 ${
        isNewest ? "polaroid-print-new" : ""
      }`}
      style={{ maxWidth: display.maxWidth + 48, scrollSnapAlign: "start" }}
    >
      <button
        type="button"
        onClick={() => {
          if (!imageSrc) return;
          onPreview({
            src: imageSrc,
            title: record.modelLabel,
            subtitle,
            image: record.image,
            outputFormat: recordOutputFormat,
          });
        }}
        className="group relative block w-full overflow-hidden rounded-lg bg-white transition-transform hover:-translate-y-0.5"
        style={{ aspectRatio: display.aspectRatio, boxShadow: IMAGE_SURFACE_SHADOW }}
      >
        {imageSrc ? (
          <img src={imageSrc} alt="Generated" className="block h-full w-full object-contain" />
        ) : (
          <div className="flex h-full w-full items-center justify-center bg-[#f6f8fb] text-gray-300">
            <ImageIcon className="h-8 w-8" />
          </div>
        )}
        <span className="pointer-events-none absolute right-2 top-2 inline-flex h-8 w-8 items-center justify-center rounded-md border border-gray-200 bg-white/90 text-gray-500 opacity-0 shadow-sm transition-opacity group-hover:opacity-100">
          <Maximize2 className="h-4 w-4" />
        </span>
      </button>

      <div className="mt-3 flex flex-col gap-3 px-0.5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] font-bold text-gray-900">
            <span>{record.modelLabel}</span>
            {displaySize ? <span className="text-gray-400">{displaySize}</span> : null}
            <span className="text-gray-400">{formatGeneratedTime(record.createdAt)}</span>
          </div>
          <p className="mt-1 line-clamp-2 text-[11px] leading-5 text-gray-500">{record.prompt}</p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void onCopy(record.image)}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 bg-white/70 px-3 py-1.5 text-[11px] font-bold text-gray-600 transition-colors hover:border-gray-300 hover:bg-white hover:text-gray-900"
          >
            <Copy className="h-3.5 w-3.5" />
            复制
          </button>
          <button
            type="button"
            onClick={() => onDownload(record.image, recordOutputFormat)}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 bg-white/70 px-3 py-1.5 text-[11px] font-bold text-gray-600 transition-colors hover:border-gray-300 hover:bg-white hover:text-gray-900"
          >
            <Download className="h-3.5 w-3.5" />
            下载
          </button>
        </div>
      </div>
    </div>
  );
}

function DemoPrintCard({ onPreview }: { onPreview: (preview: PreviewImageState) => void }) {
  const display = imageDisplayForAspect("1:1");

  return (
    <div
      className="polaroid-print-new mx-auto w-full scroll-mt-12 overflow-visible px-6 pb-9 pt-3"
      style={{ maxWidth: display.maxWidth + 48, scrollSnapAlign: "start" }}
    >
      <button
        type="button"
        onClick={() => onPreview({ src: surfacedOtterDemo, title: "演示图" })}
        className="group relative block w-full overflow-hidden rounded-lg bg-white transition-transform hover:-translate-y-0.5"
        style={{ aspectRatio: display.aspectRatio, boxShadow: IMAGE_SURFACE_SHADOW }}
      >
        <img src={surfacedOtterDemo} alt="演示图" className="block h-full w-full object-cover" />
        <span className="pointer-events-none absolute right-2 top-2 inline-flex h-8 w-8 items-center justify-center rounded-md border border-gray-200 bg-white/90 text-gray-500 opacity-0 shadow-sm transition-opacity group-hover:opacity-100">
          <Maximize2 className="h-4 w-4" />
        </span>
      </button>
    </div>
  );
}

function ImagePreviewModal({
  preview,
  onClose,
  onCopy,
  onDownload,
}: {
  preview: PreviewImageState;
  onClose: () => void;
  onCopy?: () => void | Promise<void>;
  onDownload?: () => void;
}) {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/34 px-5 py-6 backdrop-blur-[2px] animate-in fade-in duration-150"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-[980px] min-w-0 flex-col overflow-hidden rounded-lg border border-gray-200/80 bg-white shadow-[0_24px_80px_-28px_rgba(15,23,42,0.42)] animate-in zoom-in-95 duration-150"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between gap-4 border-b border-gray-200/80 px-4 py-3">
          <div className="min-w-0">
            <div className="truncate text-[13px] font-bold text-gray-900">{preview.title}</div>
            {preview.subtitle ? <div className="mt-0.5 truncate text-[11px] font-medium text-gray-400">{preview.subtitle}</div> : null}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {onCopy ? (
              <button
                type="button"
                onClick={() => void onCopy()}
                className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-[11px] font-bold text-gray-600 transition-colors hover:border-gray-300 hover:bg-gray-50 hover:text-gray-900"
              >
                <Copy className="h-3.5 w-3.5" />
                复制
              </button>
            ) : null}
            {onDownload ? (
              <button
                type="button"
                onClick={onDownload}
                className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-3 py-1.5 text-[11px] font-bold text-gray-600 transition-colors hover:border-gray-300 hover:bg-gray-50 hover:text-gray-900"
              >
                <Download className="h-3.5 w-3.5" />
                下载
              </button>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              className="inline-flex h-8 w-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto bg-[#f8fafc] p-4">
          <img src={preview.src} alt="" className="mx-auto block max-h-[76vh] max-w-full object-contain shadow-[0_18px_60px_-44px_rgba(15,23,42,0.5)]" />
        </div>
      </div>
    </div>
  );
}

function ModelStatusChip({ configured }: { configured: boolean }) {
  return (
    <div
      className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-bold ${
        configured ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"
      }`}
    >
      {configured ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertCircle className="h-3.5 w-3.5" />}
      {configured ? "后端已配置" : "后端未配置"}
    </div>
  );
}

function SelectField({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
}) {
  const selectedOption = options.find((option) => option.value === value) || options[0];

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[11px] font-bold text-gray-500">{label}</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={label}
            className="group flex h-9 w-full items-center justify-between gap-2 border-0 border-b border-gray-200 bg-transparent px-0 text-left text-[12px] font-semibold text-gray-900 outline-none transition-colors hover:border-gray-300 focus-visible:border-gray-900"
          >
            <span className="min-w-0 truncate">{selectedOption?.label || "请选择"}</span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-gray-400 transition-transform group-data-[state=open]:rotate-180" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          sideOffset={8}
          className="w-[var(--radix-dropdown-menu-trigger-width)] min-w-[9rem] rounded-xl border-gray-100 bg-white/95 p-1.5 shadow-[0_18px_46px_-22px_rgba(15,23,42,0.45)] backdrop-blur"
        >
          <DropdownMenuRadioGroup value={value} onValueChange={onChange}>
            {options.map((option) => {
              const selected = option.value === value;
              return (
                <DropdownMenuRadioItem
                  key={option.value}
                  value={option.value}
                  className={`flex min-h-8 w-full items-center rounded-lg py-2 pr-3 text-[12px] leading-none transition-colors ${
                    selected
                      ? "bg-blue-50/50 font-bold text-blue-600"
                      : "font-medium text-gray-700 hover:bg-gray-50"
                  }`}
                >
                  <span className="min-w-0 truncate">{option.label}</span>
                </DropdownMenuRadioItem>
              );
            })}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
