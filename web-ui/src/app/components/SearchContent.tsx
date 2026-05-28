import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type CSSProperties, type ReactElement } from "react";
import { Bot, Send, ChevronDown, Plus, Mic, Image as ImageIcon, BarChart2, Target, Briefcase, Copy, RefreshCw, Square, SquarePen, History, Trash2 } from "lucide-react";
import {
  formatRelativeTime as formatHistoryRelativeTime,
  generateConversationId,
  groupConversationsByTime,
  loadConversations,
  loadCurrentConversationId,
  saveConversations,
  saveCurrentConversationId,
  summarizeConversationTitle,
  type StoredConversation,
  type StoredMessage,
} from "../lib/searchChatHistory";
import {
  createTask,
  fetchAssistantTools,
  fetchTasksFull,
  generateBrandTaskDraft,
  generateQuickTodosDraft,
  runAssistantAction,
  runSearchBrandRank,
  sendChatMessageStream,
  syncTodos,
  uploadSearchFile,
  type AssistantTool,
  type BrandTaskDraft,
  type BootstrapPayload,
  type ChatMessage,
  type SearchUploadedFile,
  type TodoSnapshot,
} from "../lib/backend";

const ACTION_INTENT_PATTERNS = [
  /(帮我|给我|麻烦你|请你|请|麻烦)(.*?)(新增|添加|创建|新建|建立|加个|加一下|补个|补一下|接入|接一下|配一下|配个|保存|存一下|落一下)/,
  /(帮我|给我|麻烦你|请你|请|麻烦)(.*?)(修改|更新|调整|编辑|设置|配置|替换|改下|改一下|改个)/,
  /(帮我|给我|麻烦你|请你|请|麻烦)(.*?)(删除|移除|清空|停用|关闭|删掉|删了|去掉|关掉)/,
  /(帮我|给我|麻烦你|请你|请|麻烦)(.*?)(执行|运行|测试|触发|启动|跑一下|跑个|测一下|试一下|处理一下)/,
  /(帮我|给我|麻烦你|请你|请|麻烦)(.*?)(导入|同步|发送|推送|保存)/,
  /把(.+?)(新增|添加|创建|新建|保存|接入|配置|设置|替换|修改|更新|删除|移除|停用|关闭|导入|同步|发送|运行|测试|触发)/,
  /(新增|添加|创建|新建|建立|加个|加一下|补个|补一下).*(任务|关键词|品牌|平台|配置)/,
  /(修改|更新|调整|编辑|设置|配置|替换|改下|改一下).*(任务|关键词|品牌|平台|配置)/,
  /(删除|移除|停用|关闭|删掉|去掉).*(任务|关键词|品牌|平台|配置)/,
  /(执行|运行|测试|触发|启动|跑一下|测一下).*(任务|监控|程序|接口)/,
];

const GREETING_PATTERNS = [
  /^(你好|您好|嗨|哈喽|hello|hi|hey)\s*[!！,.，~～]*$/i,
  /^(早上好|上午好|中午好|下午好|晚上好|晚安|早安)\s*[!！,.，~～]*$/,
  /^(在吗|在不在|忙吗|有人吗)\s*[?？!！,.，~～]*$/,
  /^(谢谢|多谢|谢了|辛苦了|收到|好的|好嘞|ok|okay)\s*[!！,.，~～]*$/i,
  /^(你是谁|你能干嘛|你会什么|介绍一下自己)\s*[?？!！,.，~～]*$/,
];

const ANALYSIS_INTENT_PATTERNS = [
  /查看(一下)?(数据|报表|报告|运行情况|运行概况|监控情况|监测情况)/,
  /(运营|系统|监控|监测|任务|文章|平台|品牌).*(分析|复盘|报告|总结|诊断)/,
  /(命中率|稳定性|异常|空白|趋势).*(分析|复盘|报告|总结|诊断|看看|查看)/,
  /基于当前系统上下文.*运营分析/,
];

const CHAT_MODE_MARKERS = {
  plain_chat: "[SURFACED_CHAT_MODE:plain]",
  greeting: "[SURFACED_CHAT_MODE:greeting]",
  action: "[SURFACED_CHAT_MODE:action]",
  analysis: "[SURFACED_CHAT_MODE:analysis]",
} as const;

const BRAND_RANK_CONFIRM_RE = /^确认\s*(.+?)\s*品牌排名\s*[。.!！]*$/;

// Distance from the bottom (px) within which auto-follow during streaming
// stays engaged. Tuned so a small overscroll bounce still counts as "at end".
const SEARCH_CHAT_STICKY_THRESHOLD = 48;
// Breathing room (px) left above the user bubble when a new turn anchors it
// near the top of the chat viewport.
const SEARCH_CHAT_ANCHOR_OFFSET = 24;

const extractBrandRankBrand = (text: string) => {
  const match = text.trim().match(BRAND_RANK_CONFIRM_RE);
  return match ? match[1].trim() : "";
};

const formatFileSize = (size: number) => {
  if (size >= 1024 * 1024) {
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }
  return `${Math.max(1, Math.round(size / 1024))} KB`;
};

interface Message {
  id: string;
  role: 'user' | 'bot';
  content: string;
  thinking?: string;
  rawContent?: string;
  rawThinking?: string;
  streaming?: boolean;
}

type ShortcutSuggestion = {
  icon: ReactElement;
  text: string;
  desc: string;
  prompt?: string;
  assistantAction?: {
    action: string;
    params: Record<string, unknown>;
    successReply: string;
  };
};

type AnimatedBotIconProps = {
  className?: string;
  style?: CSSProperties;
  blinking?: boolean;
  spinning?: boolean;
  searching?: boolean;
  strokeWidth?: number;
};

function AnimatedBotIcon({
  className = "",
  style,
  blinking = false,
  spinning = false,
  searching = false,
  strokeWidth = 2.28,
}: AnimatedBotIconProps) {
  const antennaStrokeWidth = strokeWidth + (searching ? 0.52 : 1.58);
  const antennaJointRadius = searching ? 0.74 : 1.14;
  const antennaTipPath = "M12 4H8";

  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={style}
      aria-hidden="true"
    >
      <path
        className="bot-antenna-stem"
        d="M12 8V4"
        strokeWidth={antennaStrokeWidth}
      />
      <circle
        className="bot-antenna-joint"
        cx="12"
        cy="4"
        r={antennaJointRadius}
        fill="currentColor"
        stroke="none"
      />
      <path
        className={`bot-antenna-tip${spinning ? " spinning" : ""}${searching ? " searching" : ""}`}
        d={antennaTipPath}
        strokeWidth={antennaStrokeWidth}
      />
      <rect width="16" height="12" x="4" y="8" rx="2" />
      <path d="M2 14h2" />
      <path d="M20 14h2" />
      <path
        className={blinking ? "bot-eye blinking" : "bot-eye"}
        d="M15 13v2"
      />
      <path
        className={blinking ? "bot-eye blinking" : "bot-eye"}
        d="M9 13v2"
      />
    </svg>
  );
}

const PLATFORM_LABELS: Record<string, string> = {
  local_model: "本地模型",
  doubao: "豆包",
  deepseek: "DeepSeek",
  kimi: "Kimi",
  tongyi: "通义千问",
  wenxin: "文心一言",
  yuanbao: "元宝",
  chatgpt: "ChatGPT",
  claude: "Claude",
  gemini: "Gemini",
  perplexity: "Perplexity",
  ark_deepseek: "方舟 DeepSeek",
};

type ModelOption = {
  key: string;
  model: string;
  platform: string;
  label: string;
};

type ConversationMode = "plain_chat" | "greeting" | "action" | "analysis";

function formatToolSchema(tool: AssistantTool): string {
  const schema = tool.schema as { required?: string[]; properties?: Record<string, unknown> } | undefined;
  const properties = schema?.properties;
  if (!properties || Object.keys(properties).length === 0) {
    return "";
  }
  const required = new Set(schema?.required || []);
  const parts = Object.entries(properties).map(([key, rawValue]) => {
    const value = rawValue as {
      type?: string;
      enum?: unknown[];
      items?: { type?: string };
    };
    let typeLabel = value.type || "any";
    if (typeLabel === "array") {
      typeLabel = `${value.items?.type || "any"}[]`;
    }
    if (Array.isArray(value.enum) && value.enum.length > 0) {
      typeLabel += `(${value.enum.join("/")})`;
    }
    return `${key}=${typeLabel}${required.has(key) ? " required" : ""}`;
  });
  return parts.length > 0 ? ` Schema: ${parts.join(", ")}` : "";
}

function shouldPrefixPlatform(model: string, duplicateCount: number): boolean {
  const normalized = model.trim().toLowerCase();
  if (duplicateCount > 1) {
    return true;
  }
  return normalized.includes("deepseek");
}

function hasExplicitActionIntent(userText: string): boolean {
  const normalized = userText.trim();
  if (!normalized) {
    return false;
  }
  return ACTION_INTENT_PATTERNS.some((pattern) => pattern.test(normalized));
}

function isGreetingLike(userText: string): boolean {
  const normalized = userText.trim();
  if (!normalized || normalized.length > 24) {
    return false;
  }
  return GREETING_PATTERNS.some((pattern) => pattern.test(normalized));
}

function hasAnalysisIntent(userText: string): boolean {
  const normalized = userText.trim();
  if (!normalized) {
    return false;
  }
  return ANALYSIS_INTENT_PATTERNS.some((pattern) => pattern.test(normalized));
}

function inferConversationMode(
  userText: string,
  options?: { forcedMode?: ConversationMode },
): ConversationMode {
  if (options?.forcedMode) {
    return options.forcedMode;
  }
  if (hasExplicitActionIntent(userText)) {
    return "action";
  }
  if (hasAnalysisIntent(userText)) {
    return "analysis";
  }
  if (isGreetingLike(userText)) {
    return "greeting";
  }
  return "plain_chat";
}

function splitAssistantDisplayParts(rawContent: string, rawThinking = ""): { content: string; thinking: string } {
  const source = rawContent || "";
  const answerParts: string[] = [];
  const thinkParts: string[] = [];
  const openTag = /<(think|thinking)>/i;
  const closeTag = /<\/(think|thinking)>/i;
  let remaining = source;

  while (remaining) {
    const openMatch = remaining.match(openTag);
    if (!openMatch || openMatch.index === undefined) {
      answerParts.push(remaining);
      break;
    }
    answerParts.push(remaining.slice(0, openMatch.index));
    remaining = remaining.slice(openMatch.index + openMatch[0].length);

    const closeMatch = remaining.match(closeTag);
    if (!closeMatch || closeMatch.index === undefined) {
      thinkParts.push(remaining);
      remaining = "";
      break;
    }
    thinkParts.push(remaining.slice(0, closeMatch.index));
    remaining = remaining.slice(closeMatch.index + closeMatch[0].length);
  }

  if (rawThinking.trim()) {
    thinkParts.unshift(rawThinking);
  }

  const mergedThinking = thinkParts
    .map((item) => item.trim())
    .filter(Boolean)
    .join("\n\n")
    .trim();

  return {
    content: answerParts.join("").trim(),
    thinking: mergedThinking,
  };
}

function formatStreamElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

type SearchContentProps = {
  availableModels?: Record<string, string[]>;
  bootstrap?: BootstrapPayload;
  onDataChanged?: () => Promise<void> | void;
};

export function SearchContent({ availableModels, bootstrap, onDataChanged }: SearchContentProps) {
  // 从后端 availableModels 构建模型列表；同名模型在多个平台出现时，显示“平台 / 模型”
  const derivedModels = (() => {
    if (!availableModels || Object.keys(availableModels).length === 0) {
      return [] as ModelOption[];
    }
    const counts: Record<string, number> = {};
    for (const [, modelList] of Object.entries(availableModels)) {
      for (const m of modelList) {
        counts[m] = (counts[m] || 0) + 1;
      }
    }

    const list: ModelOption[] = [];
    for (const [platform, modelList] of Object.entries(availableModels)) {
      for (const model of modelList) {
        const platformLabel = PLATFORM_LABELS[platform] || platform;
        const needsPlatformPrefix = shouldPrefixPlatform(model, counts[model] || 0);
        list.push({
          key: `${platform}::${model}`,
          model,
          platform,
          label: needsPlatformPrefix ? `${platformLabel} / ${model}` : model,
        });
      }
    }
    return list;
  })();

  const [messages, setMessages] = useState<Message[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [selectedModelKey, setSelectedModelKey] = useState(derivedModels[0]?.key ?? "");
  const [showModelMenu, setShowModelMenu] = useState(false);
  const [assistantTools, setAssistantTools] = useState<AssistantTool[]>([]);
  const [composerMode, setComposerMode] = useState<"default" | "brand_task" | "quick_todos">("default");
  // Conversation history (persisted to localStorage via ../lib/searchChatHistory).
  // `currentConversationId === null` means the user is on a fresh "new chat"
  // slate — an id is minted lazily the first time we persist a non-empty
  // conversation.
  const [conversations, setConversations] = useState<StoredConversation[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [isHydrated, setIsHydrated] = useState(false);
  const historyButtonRef = useRef<HTMLButtonElement | null>(null);
  const historyPopoverRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mascotRef = useRef<HTMLDivElement>(null);
  const resultRef = useRef<Record<string, { reply: string; thinking: string }>>({});
  const thinkingScrollRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const chatScrollContainerRef = useRef<HTMLDivElement | null>(null);
  // Tracks the last user-message id we've anchored to the top of the
  // viewport, so each new turn anchors exactly once instead of on every
  // streaming token.
  const anchoredTurnIdRef = useRef<string | null>(null);
  // `true` only while the user is sitting at the bottom of the conversation.
  // We auto-follow new tokens only when this is true — once they scroll up
  // to read earlier context, the view stays put.
  const stickToBottomRef = useRef(false);
  const streamAbortRef = useRef<AbortController | null>(null);
  const idleTimerRef = useRef<number | null>(null);
  const blinkTimerRef = useRef<number | null>(null);
  const settleTimerRef = useRef<number | null>(null);
  const antennaSpinTimerRef = useRef<number | null>(null);
  const resumeAfterSpinRef = useRef(false);
  const [hoveredSuggestion, setHoveredSuggestion] = useState<number | null>(null);
  const [isMascotSpinning, setIsMascotSpinning] = useState(false);
  const [streamStartedAt, setStreamStartedAt] = useState<number | null>(null);
  const [streamElapsedMs, setStreamElapsedMs] = useState(0);
  const [expandedThinkingIds, setExpandedThinkingIds] = useState<Record<string, boolean>>({});
  const [attachments, setAttachments] = useState<SearchUploadedFile[]>([]);
  const [isUploadingAttachment, setIsUploadingAttachment] = useState(false);
  const [attachmentMessage, setAttachmentMessage] = useState("");
  const [mascotPose, setMascotPose] = useState({
    x: 0,
    y: 0,
    rotate: 0,
    scale: 1,
    aura: 0.22,
    blink: false,
  });

  const suggestions: ShortcutSuggestion[] = [
    {
      icon: <Briefcase className="w-5 h-5 text-blue-500" />,
      text: "添加品牌",
      desc: "拆解并创建任务",
      prompt:
        "请把我接下来这段品牌需求拆解成具体任务字段，并直接创建品牌任务。",
    },
    {
      icon: <ImageIcon className="w-5 h-5 text-purple-500" />,
      text: "截图发送",
      desc: "识别归组发送",
      assistantAction: {
        action: "recognition_action",
        params: { action: "start" },
        successReply:
          "已进入截图识别发送流程。现在你继续提供截图即可，后台会先识别截图中的品牌名，再按品牌分配到对应任务组；只有该品牌任务组截图数补满后，才会发送企业微信。未识别到品牌、未匹配到任务组，或截图数未满时，截图会先暂存，不会直接发送。",
      },
    },
    {
      icon: <Target className="w-5 h-5 text-emerald-500" />,
      text: "新设任务",
      desc: "拆解并添加待办",
      prompt:
        "请把我接下来这段安排拆解成几条快速待办，并直接添加到待办列表。",
    },
    {
      icon: <BarChart2 className="w-5 h-5 text-orange-500" />,
      text: "查看数据",
      desc: "深度聚合报告",
      prompt:
        "请基于当前系统上下文做一次运营分析。优先阅读现有任务、今日统计、最近文章、待办和助手配置，输出：1. 当前运行概况；2. 值得关注的异常或空白；3. 接下来最值得做的 3 个动作；4. 如果我要提升命中率/稳定性，你最建议先改什么。",
    }
  ];

  const clearMascotTimers = () => {
    if (idleTimerRef.current !== null) {
      window.clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
    }
    if (blinkTimerRef.current !== null) {
      window.clearTimeout(blinkTimerRef.current);
      blinkTimerRef.current = null;
    }
    if (settleTimerRef.current !== null) {
      window.clearTimeout(settleTimerRef.current);
      settleTimerRef.current = null;
    }
    if (antennaSpinTimerRef.current !== null) {
      window.clearTimeout(antennaSpinTimerRef.current);
      antennaSpinTimerRef.current = null;
    }
  };

  const settleMascot = (overrides?: Partial<typeof mascotPose>) => {
    setMascotPose((prev) => ({
      ...prev,
      x: 0,
      y: 0,
      rotate: 0,
      scale: 1,
      aura: 0.22,
      blink: false,
      ...overrides,
    }));
  };

  const pointMascotTo = (clientX: number, clientY: number, intensity = 1) => {
    if (isMascotSpinning) {
      return;
    }
    const rect = mascotRef.current?.getBoundingClientRect();
    if (!rect) {
      return;
    }

    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;
    const normalizedX = Math.max(-1, Math.min(1, (clientX - centerX) / (rect.width * 0.72)));
    const normalizedY = Math.max(-1, Math.min(1, (clientY - centerY) / (rect.height * 0.78)));

    setMascotPose((prev) => ({
      ...prev,
      x: normalizedX * 4.6 * intensity,
      y: normalizedY * 3.2 * intensity,
      rotate: normalizedX * 6.5 * intensity,
      scale: 1 + 0.018 * intensity,
      aura: 0.23 + (Math.abs(normalizedX) * 0.06 + Math.abs(normalizedY) * 0.04) * intensity,
      blink: false,
    }));
  };

  const focusSuggestion = (index: number) => {
    if (isMascotSpinning) {
      return;
    }
    setHoveredSuggestion(index);
    const targetPoses = [
      { x: -4.4, y: 1.4, rotate: -5.3, scale: 1.018, aura: 0.31 },
      { x: -1.6, y: 2.2, rotate: -2.1, scale: 1.015, aura: 0.295 },
      { x: 1.6, y: 2.1, rotate: 2.1, scale: 1.015, aura: 0.295 },
      { x: 4.3, y: 1.3, rotate: 5.1, scale: 1.018, aura: 0.31 },
    ];

    setMascotPose((prev) => ({
      ...prev,
      ...targetPoses[index],
      blink: false,
    }));
  };

  const releaseSuggestionFocus = () => {
    if (isMascotSpinning) {
      return;
    }
    setHoveredSuggestion(null);
    settleMascot();
  };

  const handleMascotClick = () => {
    clearMascotTimers();
    setHoveredSuggestion(null);
    settleMascot({ aura: 0.22 });
    setIsMascotSpinning(true);
    resumeAfterSpinRef.current = true;
    antennaSpinTimerRef.current = window.setTimeout(() => {
      setIsMascotSpinning(false);
    }, 2000);
  };

  const models = derivedModels;
  const selectedModel = models.find((item) => item.key === selectedModelKey) || null;
  const activeStreamingMessage = [...messages].reverse().find(
    (message) => message.role === "bot" && message.streaming,
  ) || null;
  const activeReplyLength = (activeStreamingMessage?.content || "").length;
  const activeThinkingLength = (activeStreamingMessage?.thinking || "").length;

  const applyBotStreamState = useCallback(
    (
      messageId: string,
      rawContent: string,
      rawThinking = "",
      options?: { streaming?: boolean },
    ) => {
      const display = splitAssistantDisplayParts(rawContent, rawThinking);
      setMessages((prev) => prev.map((message) => {
        if (message.id !== messageId) {
          return message;
        }
        return {
          ...message,
          content: display.content,
          thinking: display.thinking,
          rawContent,
          rawThinking,
          streaming: options?.streaming ?? false,
        };
      }));
    },
    [],
  );

  const getCopyableMessageContent = (message: Message) => {
    const display = splitAssistantDisplayParts(
      message.rawContent || message.content || "",
      message.rawThinking || message.thinking || "",
    );
    return display.content.trim();
  };

  const stopStreaming = useCallback(() => {
    streamAbortRef.current?.abort();
  }, []);

  const syncTextareaHeight = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  }, []);

  useEffect(() => {
    if (models.length === 0) {
      if (selectedModelKey) {
        setSelectedModelKey("");
      }
      return;
    }
    if (!selectedModel || !models.some((item) => item.key === selectedModelKey)) {
      setSelectedModelKey(models[0].key);
    }
  }, [models, selectedModel, selectedModelKey]);

  const handleChatScroll = useCallback(() => {
    const container = chatScrollContainerRef.current;
    if (!container) return;
    const distanceFromBottom =
      container.scrollHeight - container.clientHeight - container.scrollTop;
    stickToBottomRef.current = distanceFromBottom <= SEARCH_CHAT_STICKY_THRESHOLD;
  }, []);

  // Anchor on a new turn, otherwise follow the stream only when the user is
  // already pinned to the bottom. Prior context stays visible until the user
  // chooses to scroll past it.
  useEffect(() => {
    const container = chatScrollContainerRef.current;
    if (!container) return;

    let latestUserMessage: Message | undefined;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "user") {
        latestUserMessage = messages[index];
        break;
      }
    }
    if (!latestUserMessage) return;

    if (latestUserMessage.id !== anchoredTurnIdRef.current) {
      anchoredTurnIdRef.current = latestUserMessage.id;
      stickToBottomRef.current = false;
      const node = container.querySelector<HTMLElement>(
        `[data-search-chat-user-id="${latestUserMessage.id}"]`,
      );
      if (node) {
        const frame = window.requestAnimationFrame(() => {
          const containerRect = container.getBoundingClientRect();
          const nodeRect = node.getBoundingClientRect();
          const target =
            container.scrollTop +
            (nodeRect.top - containerRect.top) -
            SEARCH_CHAT_ANCHOR_OFFSET;
          container.scrollTop = Math.max(0, target);
        });
        return () => window.cancelAnimationFrame(frame);
      }
      return;
    }

    if (!stickToBottomRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight - container.clientHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages]);

  // Hydrate persisted conversations + the active conversation on mount.
  // Runs once; sets `isHydrated` so the persistence effect below stays inert
  // until we know we wouldn't overwrite real data with empty initial state.
  useEffect(() => {
    const stored = loadConversations();
    setConversations(stored);
    const activeId = loadCurrentConversationId();
    if (activeId) {
      const active = stored.find((conv) => conv.id === activeId);
      if (active) {
        setCurrentConversationId(active.id);
        setMessages(
          active.messages.map((m) => ({
            id: m.id,
            role: m.role,
            content: m.content,
            thinking: m.thinking,
            rawContent: m.rawContent,
            rawThinking: m.rawThinking,
          })),
        );
        anchoredTurnIdRef.current = null;
      } else {
        saveCurrentConversationId(null);
      }
    }
    setIsHydrated(true);
  }, []);

  // Persist the active conversation whenever a turn completes. We skip
  // mid-stream writes (partial replies aren't worth saving) and skip empty
  // slates (avoids creating an empty "新对话" row on first mount).
  useEffect(() => {
    if (!isHydrated) return;
    if (isGenerating) return;
    if (messages.length === 0) return;

    const id = currentConversationId ?? generateConversationId();
    const now = Date.now();
    const storedMessages: StoredMessage[] = messages.map((m) => ({
      id: m.id,
      role: m.role,
      content: m.content,
      thinking: m.thinking,
      rawContent: m.rawContent,
      rawThinking: m.rawThinking,
    }));
    const title = summarizeConversationTitle(storedMessages);

    setConversations((prev) => {
      const existing = prev.find((conv) => conv.id === id);
      const createdAt = existing?.createdAt ?? now;
      const updated: StoredConversation = {
        id,
        title,
        messages: storedMessages,
        createdAt,
        updatedAt: now,
      };
      const next = [updated, ...prev.filter((conv) => conv.id !== id)];
      saveConversations(next);
      return next;
    });

    if (currentConversationId !== id) {
      setCurrentConversationId(id);
      saveCurrentConversationId(id);
    }
  }, [isHydrated, isGenerating, messages, currentConversationId]);

  // Close the history popover when clicking outside of it.
  useEffect(() => {
    if (!historyOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (historyPopoverRef.current?.contains(target)) return;
      if (historyButtonRef.current?.contains(target)) return;
      setHistoryOpen(false);
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [historyOpen]);

  // Close on Escape — keeps keyboard parity with mature chat UIs.
  useEffect(() => {
    if (!historyOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setHistoryOpen(false);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [historyOpen]);

  const handleStartNewChat = useCallback(() => {
    streamAbortRef.current?.abort();
    setMessages([]);
    setCurrentConversationId(null);
    saveCurrentConversationId(null);
    anchoredTurnIdRef.current = null;
    stickToBottomRef.current = false;
    setHistoryOpen(false);
  }, []);

  const handleSelectConversation = useCallback(
    (id: string) => {
      setHistoryOpen(false);
      if (id === currentConversationId) return;
      const target = conversations.find((conv) => conv.id === id);
      if (!target) return;
      streamAbortRef.current?.abort();
      setMessages(
        target.messages.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          thinking: m.thinking,
          rawContent: m.rawContent,
          rawThinking: m.rawThinking,
        })),
      );
      setCurrentConversationId(target.id);
      saveCurrentConversationId(target.id);
      anchoredTurnIdRef.current = null;
      stickToBottomRef.current = false;
    },
    [conversations, currentConversationId],
  );

  const handleDeleteConversation = useCallback(
    (id: string) => {
      setConversations((prev) => {
        const next = prev.filter((conv) => conv.id !== id);
        saveConversations(next);
        return next;
      });
      if (id === currentConversationId) {
        setMessages([]);
        setCurrentConversationId(null);
        saveCurrentConversationId(null);
        anchoredTurnIdRef.current = null;
        stickToBottomRef.current = false;
      }
    },
    [currentConversationId],
  );

  const sortedConversations = useMemo(
    () => [...conversations].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)),
    [conversations],
  );
  const conversationGroups = useMemo(
    () => groupConversationsByTime(sortedConversations),
    [sortedConversations],
  );

  useEffect(() => {
    const activeThinkingMessage = [...messages].reverse().find(
      (message) => message.role === "bot" && message.streaming && Boolean(message.thinking),
    );
    if (!activeThinkingMessage) {
      return;
    }
    const container = thinkingScrollRefs.current[activeThinkingMessage.id];
    if (!container) {
      return;
    }
    window.requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
    });
  }, [messages]);

  useEffect(() => {
    if (!isGenerating || !streamStartedAt) {
      setStreamElapsedMs(0);
      return;
    }
    setStreamElapsedMs(Date.now() - streamStartedAt);
    const timer = window.setInterval(() => {
      setStreamElapsedMs(Date.now() - streamStartedAt);
    }, 250);
    return () => window.clearInterval(timer);
  }, [isGenerating, streamStartedAt]);

  useEffect(() => {
    syncTextareaHeight();
  }, [inputValue, syncTextareaHeight]);

  useEffect(() => {
    fetchAssistantTools().then(setAssistantTools);
  }, []);

  useEffect(() => {
    if (isMascotSpinning || !resumeAfterSpinRef.current) {
      return;
    }

    setMascotPose((prev) => ({
      ...prev,
      x: 0,
      y: -1.2,
      rotate: 1.4,
      scale: 1.014,
      aura: 0.27,
      blink: true,
    }));

    settleTimerRef.current = window.setTimeout(() => {
      settleMascot({ aura: 0.23 });
    }, 320);
  }, [isMascotSpinning]);

  useEffect(() => {
    if (messages.length !== 0) {
      clearMascotTimers();
      return;
    }

    let active = true;
    const idleMoves = [
      { x: -2.2, y: -1.2, rotate: -3.2, scale: 1.01, aura: 0.24 },
      { x: 2.4, y: -0.8, rotate: 3.6, scale: 1.012, aura: 0.245 },
      { x: -1.6, y: 1.8, rotate: -2.4, scale: 1.008, aura: 0.238 },
      { x: 1.4, y: 1.3, rotate: 2.2, scale: 1.01, aura: 0.242 },
    ];

    const queueIdleMotion = () => {
      const nextDelay = resumeAfterSpinRef.current ? 1100 : 2600 + Math.random() * 2600;
      resumeAfterSpinRef.current = false;
      idleTimerRef.current = window.setTimeout(() => {
        if (!active) {
          return;
        }
        if (hoveredSuggestion !== null || isMascotSpinning) {
          queueIdleMotion();
          return;
        }

        const pose = idleMoves[Math.floor(Math.random() * idleMoves.length)];
        setMascotPose((prev) => ({ ...prev, ...pose, blink: false }));

        blinkTimerRef.current = window.setTimeout(() => {
          if (!active) {
            return;
          }
          setMascotPose((prev) => ({ ...prev, blink: true }));
          settleTimerRef.current = window.setTimeout(() => {
            if (!active) {
              return;
            }
            settleMascot();
          }, 260);
        }, 180 + Math.random() * 360);

        queueIdleMotion();
      }, nextDelay);
    };

    queueIdleMotion();

    return () => {
      active = false;
      clearMascotTimers();
    };
  }, [messages.length, hoveredSuggestion, isMascotSpinning]);

  const buildProgramContext = (
    bootstrap?: BootstrapPayload,
    options?: {
      includeAssistant?: boolean;
      includeStats?: boolean;
      includePlatforms?: boolean;
      includeTasks?: boolean;
      includeArticles?: boolean;
      includeTodos?: boolean;
    },
  ) => {
    if (!bootstrap) return "";

    const {
      includeAssistant = true,
      includeStats = true,
      includePlatforms = true,
      includeTasks = true,
      includeArticles = true,
      includeTodos = true,
    } = options || {};

    const lines: string[] = [];
    const stats = bootstrap.stats;
    const assistant = bootstrap.assistant;
    const enabledPlatforms = (bootstrap.platforms || []).filter((item) => item.enabled);
    const tasks = bootstrap.tasks || [];
    const articles = bootstrap.articles || [];
    const todos = bootstrap.todos || [];

    if (includeAssistant) {
      lines.push(`当前助手平台: ${PLATFORM_LABELS[assistant.platform] || assistant.platform} / ${assistant.model}`);
    }
    if (includeStats) {
      lines.push(`任务统计: 启用 ${stats.enabledTasks} / 总计 ${stats.totalTasks} / 今日记录 ${stats.todayRecords} / 命中 ${stats.hitRecords} / 错误 ${stats.errorRecords}`);
    }

    if (includePlatforms && enabledPlatforms.length > 0) {
      lines.push(`已启用平台: ${enabledPlatforms.map((item) => `${item.name}${item.model ? `(${item.model})` : ""}`).join("、")}`);
    }

    if (includeTasks && tasks.length > 0) {
      lines.push("当前任务:");
      tasks.slice(0, 12).forEach((task) => {
        lines.push(`- ${task.name} | 品牌 ${task.brand || "未填"} | 模式 ${task.mode || "unknown"} | 平台 ${(task.platforms || []).join("、") || "未配置"} | 状态 ${task.statusLabel || task.status || "unknown"}`);
      });
    }

    if (includeArticles && articles.length > 0) {
      lines.push("最近文章:");
      articles.slice(0, 8).forEach((article) => {
        lines.push(`- ${article.source}: ${article.title}`);
      });
    }

    if (includeTodos && todos.length > 0) {
      lines.push("待办事项:");
      todos.slice(0, 8).forEach((todo) => {
        lines.push(`- [${todo.done ? "x" : " "}] ${todo.text}`);
      });
    }

    return lines.join("\n");
  };

  const buildSystemSections = (
    userText: string,
    options?: { forcedMode?: ConversationMode },
  ) => {
    const conversationMode = inferConversationMode(userText, options);
    const allowActions = conversationMode === "action";
    const allowAnalysis = conversationMode === "analysis";
    const systemSections: string[] = [CHAT_MODE_MARKERS[conversationMode]];

    if (conversationMode === "greeting") {
      systemSections.push(
        "这是招呼或寒暄，请简短自然地回应；不要输出 ACTION 标记，也不要主动讲系统配置。",
      );
    } else if (allowAnalysis) {
      systemSections.push(
        "你是 Surfaced 的 WebUI 数据助手，这条消息是在查看数据或做运营分析。",
        "优先结合当前程序上下文里的真实任务、统计、文章和待办做判断，给出结论而不是空泛套话。",
        "输出尽量围绕运行概况、异常或空白、下一步动作、优化建议这几个重点。",
        "不要输出 ACTION 标记，也不要主动改动程序状态。",
      );
    } else if (allowActions) {
      systemSections.push(
        "你是 Surfaced 的 WebUI 智能助手，这条消息包含明确的应用操作意图。",
        "先理解用户要修改什么，再给出最小必要的执行结果或确认，不要泛泛而谈。",
        "只有在对象清晰、用户确实要改程序状态时，才允许执行系统操作。",
        "如果用户明确要求你执行系统操作，可以在回复末尾输出一个动作标记，格式必须是 [ACTION:tool_name:{\"key\":\"value\"}]。",
        "删除、覆盖、停用之类风险操作，只有在用户明确要求时才能执行。",
        "涉及修改配置时，优先使用最细粒度的工具；只有用户明确要求整页替换时，才使用 update_task、save_settings、save_platform_config 这类整页覆盖工具。",
        "一次最多只输出一个 ACTION 标记。",
      );
    }

    if (allowActions && assistantTools.length > 0) {
      systemSections.push(
        "当前可调用工具如下：\n" +
        assistantTools.map((tool) => {
          const paramsDesc = tool.params && Object.keys(tool.params).length > 0
            ? ` 参数: ${Object.entries(tool.params).map(([key, value]) => `${key}=${value}`).join(", ")}`
            : "";
          return `- ${tool.name}: ${tool.description}${paramsDesc}${formatToolSchema(tool)}`;
        }).join("\n")
      );
    }

    const programContext = allowActions || allowAnalysis
      ? buildProgramContext(bootstrap)
      : "";
    if (programContext) {
      systemSections.push(`当前程序上下文如下：\n${programContext}`);
    }

    return systemSections;
  };

  const buildConversationMessages = (
    userText: string,
    options?: { forcedMode?: ConversationMode; historySource?: Message[] },
  ): ChatMessage[] => {
    const conversationMode = inferConversationMode(userText, options);
    if (conversationMode === "plain_chat" || conversationMode === "greeting") {
      return [{ role: "user", content: userText }];
    }

    const systemSections = buildSystemSections(userText, options);
    const historySource = options?.historySource ?? messages;

    const historyMessages: ChatMessage[] = historySource.map((m) => ({
      role: m.role === "bot" ? "assistant" : "user",
      content: m.content,
    }));

    return [
      { role: "system", content: systemSections.join("\n\n") },
      ...historyMessages,
      { role: "user", content: userText },
    ];
  };

  const extractActionFromReply = (reply: string): { action?: string; params: Record<string, unknown>; cleaned: string } => {
    const match = reply.match(/\[ACTION:([a-zA-Z0-9_]+)(?::(\{[\s\S]*\}))?\]\s*$/);
    if (!match) {
      return { params: {}, cleaned: reply.trim() };
    }
    let params: Record<string, unknown> = {};
    if (match[2]) {
      try {
        params = JSON.parse(match[2]);
      } catch {
        params = {};
      }
    }
    return {
      action: match[1],
      params,
      cleaned: reply.replace(match[0], "").trim(),
    };
  };

  const formatAssistantActionResult = (action: string, response: Awaited<ReturnType<typeof runAssistantAction>>): string => {
    if (!response.ok) {
      return `系统执行失败：${response.message || action}`;
    }
    const resultMessage = typeof response.result?.message === "string" ? response.result.message : "";
    const queued = response.result?.queued === true ? "已加入执行队列。" : "";
    const okText = resultMessage || queued || "操作已执行。";
    return `系统执行结果：${okText}`;
  };

  const getMostCommonWebhook = (tasks: Array<{ webhook_url?: string }>) => {
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

  const computeDraftRecognitionBatchSize = (draft: BrandTaskDraft) => {
    const fallbackPlatformCount = (draft.platforms || []).filter(Boolean).length;
    const total = (draft.keywords || []).reduce((sum, keyword) => {
      const platformCount = (keyword.platforms || draft.platforms || []).filter(Boolean).length;
      return sum + Math.max(0, platformCount || fallbackPlatformCount);
    }, 0);
    return Math.max(1, total || fallbackPlatformCount || 1);
  };

  const draftToTaskPayload = (
    draft: BrandTaskDraft,
    existingTasks: Array<{ webhook_url?: string }>,
  ): Record<string, unknown> => ({
    name: draft.name || draft.brand,
    brand: draft.brand || draft.name,
    keywords: (draft.keywords || []).map((keyword) => ({
      keyword: keyword.keyword,
      brand: keyword.brand || draft.brand || draft.name,
      platforms: keyword.platforms || draft.platforms || [],
      mode: keyword.mode || "browser",
      deep_think: Object.fromEntries((keyword.deep_think_platforms || []).map((platform) => [platform, true])),
    })),
    platforms: draft.platforms || [],
    webhook_url: draft.webhook_url || getMostCommonWebhook(existingTasks),
    weekdays: draft.weekdays || [],
    enabled: Boolean(draft.enabled),
    industry_tags: draft.industry_tags || [],
    region_tags: draft.region_tags || [],
    optimization_start_date: draft.optimization_start_date || "",
    optimization_end_date: draft.optimization_end_date || "",
    inspect: Boolean(draft.inspect),
    recognition_enabled: true,
    recognition_brands: (draft.recognition_brands || []).join(", "),
    recognition_batch_size: (
      draft.recognition_batch_size && draft.recognition_batch_size > 1
        ? Math.max(1, draft.recognition_batch_size)
        : computeDraftRecognitionBatchSize(draft)
    ),
    fixed_screenshot_enabled: Boolean(draft.fixed_screenshot_enabled),
    fixed_screenshot_count: Math.max(1, draft.fixed_screenshot_count || 1),
  });

  const formatBrandTaskCreateResult = (draft: BrandTaskDraft, taskId?: string) => {
    const platformLabels = (draft.platforms || []).map((platform) => PLATFORM_LABELS[platform] || platform);
    const keywordLabels = (draft.keywords || []).map((item) => item.keyword).filter(Boolean);
    const summaryLines = [
      `已按这段需求创建品牌任务「${draft.name || draft.brand}」${taskId ? `（ID: ${taskId}）` : ""}。`,
      `品牌：${draft.brand || draft.name}`,
      `平台：${platformLabels.join("、") || "未指定"}`,
      `关键词：${keywordLabels.slice(0, 8).join("、") || "未解析到关键词"}`,
    ];
    summaryLines.push(`识别模式单组截图数：${Math.max(1, draft.recognition_batch_size || 1)}`);
    return summaryLines.join("\n");
  };

  const requestBrandTaskCreation = async (instruction: string): Promise<{ ok: boolean; message: string }> => {
    const draftResult = await generateBrandTaskDraft(instruction);
    if (!draftResult.ok || !draftResult.draft) {
      return { ok: false, message: draftResult.message || "品牌任务拆解失败" };
    }

    const existingTasks = await fetchTasksFull();
    const createResult = await createTask(draftToTaskPayload(draftResult.draft, existingTasks));
    if (!createResult.ok) {
      return { ok: false, message: createResult.message || "品牌任务创建失败" };
    }

    if (onDataChanged) {
      await onDataChanged();
    }

    return {
      ok: true,
      message: formatBrandTaskCreateResult(draftResult.draft, createResult.task_id),
    };
  };

  const requestQuickTodoCreation = async (instruction: string): Promise<{ ok: boolean; message: string }> => {
    const draftResult = await generateQuickTodosDraft(instruction);
    if (!draftResult.ok || !draftResult.todos?.length) {
      return { ok: false, message: draftResult.message || "快速待办拆解失败" };
    }

    const existingTodos = bootstrap?.todos || [];
    const appended: TodoSnapshot[] = draftResult.todos.map((item, index) => ({
      id: `ai-todo-${Date.now()}-${index}`,
      text: item.text,
      done: false,
    }));
    const syncResult = await syncTodos([...existingTodos, ...appended]);
    if (!syncResult.ok) {
      return { ok: false, message: syncResult.message || "快速待办保存失败" };
    }

    if (onDataChanged) {
      await onDataChanged();
    }

    return {
      ok: true,
      message: [
        `已按这段需求添加 ${draftResult.todos.length} 条快速待办。`,
        ...draftResult.todos.slice(0, 6).map((item) => `- ${item.text}`),
      ].join("\n"),
    };
  };

  const requestAssistantReply = async (
    conversation: ChatMessage[],
    userText: string,
    botMessageId: string,
  ): Promise<{ ok: boolean; message: string }> => {
    const controller = new AbortController();
    streamAbortRef.current = controller;
    setStreamStartedAt(Date.now());
    const result = await sendChatMessageStream(
      selectedModel?.platform || "",
      selectedModel?.model || "",
      conversation,
      {
        signal: controller.signal,
        onDelta: (_, fullReply) => {
          applyBotStreamState(botMessageId, fullReply, resultRef.current[botMessageId]?.thinking || "", { streaming: true });
          resultRef.current[botMessageId] = {
            reply: fullReply,
            thinking: resultRef.current[botMessageId]?.thinking || "",
          };
        },
        onThinkingDelta: (_, fullThinking) => {
          applyBotStreamState(botMessageId, resultRef.current[botMessageId]?.reply || "", fullThinking, { streaming: true });
          resultRef.current[botMessageId] = {
            reply: resultRef.current[botMessageId]?.reply || "",
            thinking: fullThinking,
          };
        },
      },
    );
    if (!result.ok) {
      const partialReply = ((result.reply || "") || resultRef.current[botMessageId]?.reply || "").trim();
      const partialThinking = (result.thinking || "") || resultRef.current[botMessageId]?.thinking || "";
      const errorText = partialReply
        ? `${partialReply}\n\n${result.aborted ? "已停止生成" : `请求中断：${result.message || "未知错误"}`}`
        : result.aborted
          ? "已停止生成"
        : `抱歉，请求失败：${result.message}`;
      applyBotStreamState(botMessageId, errorText, partialThinking, { streaming: false });
      delete resultRef.current[botMessageId];
      if (streamAbortRef.current === controller) {
        streamAbortRef.current = null;
      }
      setStreamStartedAt(null);
      return { ok: false, message: errorText };
    }
    const rawReply = result.reply || "（无回复）";
    const parsed = extractActionFromReply(rawReply);
    if (!parsed.action) {
      const finalText = parsed.cleaned || rawReply;
      applyBotStreamState(botMessageId, finalText, result.thinking || "", { streaming: false });
      delete resultRef.current[botMessageId];
      if (streamAbortRef.current === controller) {
        streamAbortRef.current = null;
      }
      setStreamStartedAt(null);
      return { ok: true, message: finalText };
    }

    if (!hasExplicitActionIntent(userText)) {
      const finalText = parsed.cleaned || rawReply;
      applyBotStreamState(botMessageId, finalText, result.thinking || "", { streaming: false });
      delete resultRef.current[botMessageId];
      if (streamAbortRef.current === controller) {
        streamAbortRef.current = null;
      }
      setStreamStartedAt(null);
      return { ok: true, message: finalText };
    }

    const actionResult = await runAssistantActionWithConfirm(parsed.action, parsed.params);
    if (onDataChanged) {
      await onDataChanged();
    }
    const actionText = formatAssistantActionResult(parsed.action, actionResult);
    const finalText = [parsed.cleaned, actionText].filter(Boolean).join("\n\n");
    applyBotStreamState(botMessageId, finalText, result.thinking || "", { streaming: false });
    delete resultRef.current[botMessageId];
    if (streamAbortRef.current === controller) {
      streamAbortRef.current = null;
    }
    setStreamStartedAt(null);
    return { ok: true, message: finalText };
  };

  const runAssistantActionWithConfirm = async (
    action: string,
    params: Record<string, unknown>,
  ) => {
    if (action === "delete_task") {
      const taskLabel = String(params.task_name || params.task_id || "").trim();
      const confirmed = window.confirm(
        taskLabel
          ? `确认删除任务“${taskLabel}”吗？此操作不可撤销。`
          : "确认删除这个任务吗？此操作不可撤销。",
      );
      if (!confirmed) {
        return { ok: false, action, message: "已取消删除操作" };
      }
      return runAssistantAction(action, { ...params, confirm: true });
    }
    return runAssistantAction(action, params);
  };

  const handleAttachmentPick = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) {
      return;
    }
    if (!/\.(xlsx|xlsm)$/i.test(file.name)) {
      setAttachmentMessage("只支持上传 .xlsx/.xlsm 表格");
      return;
    }

    setIsUploadingAttachment(true);
    setAttachmentMessage("正在上传表格...");
    try {
      const result = await uploadSearchFile(file);
      if (!result.ok || !result.file) {
        setAttachmentMessage(result.message || "上传失败");
        return;
      }
      setAttachments([result.file]);
      setAttachmentMessage("表格已上传");
    } finally {
      setIsUploadingAttachment(false);
    }
  };

  const formatBrandRankResult = (result: Awaited<ReturnType<typeof runSearchBrandRank>>) => {
    if (!result.ok) {
      return `品牌排名处理失败：${result.message || "未知错误"}`;
    }
    const summary = result.summary || {};
    const rankCounts = summary.rank_counts || {};
    const countText = Object.entries(rankCounts)
      .sort(([a], [b]) => Number(a) - Number(b))
      .map(([rank, count]) => `${rank}=${count}`)
      .join("，");
    return [
      result.message || `已完成「${result.brand || ""}」品牌排名`,
      `处理行数：${summary.processed_rows ?? 0}`,
      countText ? `排名分布：${countText}` : "",
      `复核建议：${summary.review_count ?? 0} 行`,
      result.download_url ? `文件：[${result.file_name || "下载处理好的文件"}](${result.download_url})` : "",
    ].filter(Boolean).join("\n");
  };

  const handleBrandRankRequest = async (trimmed: string, brand: string) => {
    const messageSeed = Date.now().toString();
    const attachment = attachments[0];
    const userContent = attachment ? `${trimmed}\n附件：${attachment.name}` : trimmed;
    const userMessage: Message = { id: `${messageSeed}-user`, role: "user", content: userContent };
    const botMessageId = `${messageSeed}-bot`;

    setMessages(prev => [
      ...prev,
      userMessage,
      { id: botMessageId, role: "bot", content: "", streaming: true },
    ]);
    setInputValue("");
    setIsGenerating(true);

    try {
      const finalText = attachment
        ? formatBrandRankResult(await runSearchBrandRank(attachment.id, brand))
        : "请先点左侧 + 上传表格，然后输入“确认xx品牌排名”。";
      setMessages(prev => prev.map(message => (
        message.id === botMessageId
          ? { ...message, content: finalText, streaming: false }
          : message
      )));
      if (attachment) {
        setAttachments([]);
        setAttachmentMessage("");
      }
    } finally {
      setStreamStartedAt(null);
      setIsGenerating(false);
    }
  };

  const handleSend = async (
    text: string = inputValue,
    options?: {
      forceBrandCreate?: boolean;
      forcedMode?: ConversationMode;
      forcedComposerMode?: "brand_task" | "quick_todos";
    },
  ) => {
    const trimmed = text.trim();
    if (!trimmed || isGenerating) return;

    const mode = options?.forceBrandCreate
      ? "brand_task"
      : options?.forcedComposerMode || composerMode;
    const brandRankBrand = mode === "default" ? extractBrandRankBrand(trimmed) : "";
    if (brandRankBrand) {
      await handleBrandRankRequest(trimmed, brandRankBrand);
      return;
    }
    const messageSeed = Date.now().toString();
    const newUserMsg: Message = { id: `${messageSeed}-user`, role: 'user', content: trimmed };
    const botMessageId = `${messageSeed}-bot`;
    if (mode === "default") {
      resultRef.current[botMessageId] = { reply: "", thinking: "" };
      setMessages(prev => [
        ...prev,
        newUserMsg,
        { id: botMessageId, role: "bot", content: "", thinking: "", rawContent: "", rawThinking: "", streaming: true },
      ]);
    } else {
      setMessages(prev => [...prev, newUserMsg]);
    }
    setInputValue("");
    setIsGenerating(true);

    try {
      const botResult = mode === "brand_task"
        ? await requestBrandTaskCreation(trimmed)
        : mode === "quick_todos"
          ? await requestQuickTodoCreation(trimmed)
          : {
            ...(await requestAssistantReply(
              buildConversationMessages(trimmed, { forcedMode: options?.forcedMode }),
              trimmed,
              botMessageId,
            )),
            ok: true,
          };
      setComposerMode(mode === "default" ? "default" : (botResult.ok ? "default" : mode));
      if (mode !== "default") {
        const newBotMsg: Message = { id: `${Date.now()}-bot`, role: 'bot', content: botResult.message };
        setMessages(prev => [...prev, newBotMsg]);
      }
    } finally {
      if (mode !== "default") {
        setStreamStartedAt(null);
      }
      setIsGenerating(false);
    }
  };

  const handleSuggestionClick = async (suggestion: ShortcutSuggestion) => {
    if (suggestion.text === "添加品牌") {
      if (inputValue.trim()) {
        await handleSend(inputValue, { forceBrandCreate: true });
        return;
      }
      setComposerMode("brand_task");
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now().toString(),
          role: "bot",
          content: "把你要新增品牌任务的一整段需求直接发给我，我会只按你写的内容拆解并创建到品牌任务里，不再单独弹配置窗。",
        },
      ]);
      window.setTimeout(() => textareaRef.current?.focus(), 0);
      return;
    }

    if (suggestion.text === "新设任务") {
      if (inputValue.trim()) {
        setComposerMode("quick_todos");
        await handleSend(inputValue, { forcedComposerMode: "quick_todos" });
        return;
      }
      setComposerMode("quick_todos");
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now().toString(),
          role: "bot",
          content: "把你的一整段任务安排直接发给我，我会按你写的内容拆成快速待办并加入右侧待办列表。",
        },
      ]);
      window.setTimeout(() => textareaRef.current?.focus(), 0);
      return;
    }

    if (suggestion.assistantAction) {
      setComposerMode("default");
      if (isGenerating) return;

      const userMessage: Message = {
        id: Date.now().toString(),
        role: "user",
        content: suggestion.text,
      };
      setMessages(prev => [...prev, userMessage]);
      setIsGenerating(true);

      try {
        const actionResult = await runAssistantActionWithConfirm(
          suggestion.assistantAction.action,
          suggestion.assistantAction.params,
        );
        if (onDataChanged) {
          await onDataChanged();
        }
        const botMessage: Message = {
          id: (Date.now() + 1).toString(),
          role: "bot",
          content: actionResult.ok
            ? suggestion.assistantAction.successReply
            : `截图发送入口启动失败：${actionResult.message || "未知错误"}`,
        };
        setMessages(prev => [...prev, botMessage]);
      } finally {
        setStreamStartedAt(null);
        setIsGenerating(false);
      }
      return;
    }

    if (suggestion.prompt) {
      setComposerMode("default");
      await handleSend(suggestion.prompt, {
        forcedMode: suggestion.text === "查看数据" ? "analysis" : undefined,
      });
    }
  };

  const handleRegenerate = async (msgId: string) => {
    const msgIndex = messages.findIndex(m => m.id === msgId);
    if (msgIndex < 0) return;
    // Find the user message before this bot message
    const prevMessages = messages.slice(0, msgIndex);
    let lastUserIndex = -1;
    for (let index = prevMessages.length - 1; index >= 0; index -= 1) {
      if (prevMessages[index]?.role === "user") {
        lastUserIndex = index;
        break;
      }
    }
    if (lastUserIndex < 0) return;
    const lastUserMsg = prevMessages[lastUserIndex];
    const historyBeforeLastUser = prevMessages.slice(0, lastUserIndex);
    // Remove the bot message and regenerate
    const regeneratedBotId = `${Date.now()}-regen-bot`;
    resultRef.current[regeneratedBotId] = { reply: "", thinking: "" };
    setMessages(prev => [
      ...prev.filter(m => m.id !== msgId),
      { id: regeneratedBotId, role: "bot", content: "", thinking: "", rawContent: "", rawThinking: "", streaming: true },
    ]);
    setIsGenerating(true);
    try {
      await requestAssistantReply(
        buildConversationMessages(lastUserMsg.content, {
          historySource: historyBeforeLastUser,
        }),
        lastUserMsg.content,
        regeneratedBotId,
      );
    } finally {
      setIsGenerating(false);
    }
  };

  const handleCopyMessage = (message: Message) => {
    navigator.clipboard.writeText(getCopyableMessageContent(message));
  };

  const renderStreamingCursor = (tone: "blue" | "slate" = "blue") => (
    <span
      className={`streaming-caret ml-1 inline-block h-[1.05em] w-[0.58ch] rounded-[2px] align-[-0.18em] ${
        tone === "slate" ? "bg-slate-400/85" : "bg-blue-500/85"
      }`}
      aria-hidden="true"
    />
  );

  const renderInlineContent = (
    text: string,
    accentTerminalPeriod = false,
    options?: { showCursor?: boolean; cursorTone?: "blue" | "slate"; muted?: boolean },
  ) => {
    let workingText = text;
    let terminalPeriod = "";

    if (accentTerminalPeriod) {
      const periodMatch = workingText.match(/[。.]$/);
      if (periodMatch) {
        terminalPeriod = periodMatch[0];
        workingText = workingText.slice(0, -terminalPeriod.length);
      }
    }

    const parts = workingText.split(/(\[[^\]]+\]\([^)]+\)|\*\*.*?\*\*|\`.*?\`)/g);
    const nodes = parts.map((part, j) => {
      const linkMatch = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (linkMatch) {
        return (
          <a
            key={j}
            href={linkMatch[2]}
            className="font-semibold text-blue-600 underline decoration-blue-300 underline-offset-2 transition-colors hover:text-blue-700"
          >
            {linkMatch[1]}
          </a>
        );
      }
      if (part.startsWith('**') && part.endsWith('**')) {
        return <strong key={j} className={`${options?.muted ? "text-gray-700" : "text-gray-900"} font-semibold`}>{part.slice(2, -2)}</strong>;
      }
      if (part.startsWith('`') && part.endsWith('`')) {
        return (
          <code
            key={j}
            className={`px-1.5 py-0.5 rounded text-[12px] font-mono border ${
              options?.muted
                ? "bg-gray-100 text-gray-600 border-gray-200"
                : "bg-blue-50 text-blue-700 border-blue-100"
            }`}
          >
            {part.slice(1, -1)}
          </code>
        );
      }
      return <span key={j}>{part}</span>;
    });

    if (terminalPeriod) {
      nodes.push(
        <span key="terminal-period" className="text-[var(--brand-cyan)]">
          {terminalPeriod}
        </span>,
      );
    }

    if (options?.showCursor) {
      nodes.push(
        <span key="streaming-cursor">
          {renderStreamingCursor(options.cursorTone || "blue")}
        </span>,
      );
    }

    return nodes;
  };

  // Modernized markdown renderer
  const renderMarkdown = (
    text: string,
    options?: { showCursor?: boolean; cursorTone?: "blue" | "slate"; muted?: boolean },
  ) => {
    const lines = text.split('\n');
    let lastContentLineIndex = -1;
    for (let idx = lines.length - 1; idx >= 0; idx -= 1) {
      if (lines[idx].trim() !== '') {
        lastContentLineIndex = idx;
        break;
      }
    }

    return lines.map((line, i) => {
      const accentTerminalPeriod = i === lastContentLineIndex;
      const showCursor = Boolean(options?.showCursor && i === lastContentLineIndex);
      if (line.startsWith('### ')) {
        return (
          <h3
            key={i}
            className={`text-[14px] font-bold mt-5 mb-3 tracking-wide flex items-center gap-2 ${
              options?.muted ? "text-gray-600" : "text-gray-900"
            }`}
          >
            <div className={`w-1 h-3.5 rounded-full ${options?.muted ? "bg-gray-300" : "bg-blue-600"}`}></div>
            {renderInlineContent(line.replace('### ', ''), accentTerminalPeriod, {
              showCursor,
              cursorTone: options?.cursorTone,
              muted: options?.muted,
            })}
          </h3>
        );
      }
      if (line.startsWith('- ')) {
        let content = line.replace('- ', '');
        
        return (
          <div key={i} className="flex items-start gap-2 mb-2.5">
            <div className={`w-1.5 h-1.5 rounded-full mt-2 shrink-0 ${options?.muted ? "bg-gray-300" : "bg-blue-400"}`}></div>
            <div className={`${options?.muted ? "text-[13px] text-gray-500" : "text-[14px] text-gray-700"} leading-relaxed`}>
              {renderInlineContent(content, accentTerminalPeriod, {
                showCursor,
                cursorTone: options?.cursorTone,
                muted: options?.muted,
              })}
            </div>
          </div>
        );
      }
      if (line.trim() === '') {
        if (showCursor) {
          return <div key={i} className="h-3">{renderStreamingCursor(options?.cursorTone || "blue")}</div>;
        }
        return <div key={i} className="h-3"></div>;
      }

      return (
        <p key={i} className={`mb-3 leading-relaxed ${options?.muted ? "text-[13px] text-gray-500" : "text-[14px] text-gray-700"}`}>
          {renderInlineContent(line, accentTerminalPeriod, {
            showCursor,
            cursorTone: options?.cursorTone,
            muted: options?.muted,
          })}
        </p>
      );
    });
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-[#fcfdff] overflow-hidden relative">
      <style>
        {`
          @keyframes botEyeBlink {
            0% { transform: translateY(0) scaleY(1); opacity: 1; }
            35% { transform: translateY(0.34px) scaleY(0.14); opacity: 0.94; }
            100% { transform: translateY(0) scaleY(1); opacity: 1; }
          }

          @keyframes botAntennaSpin {
            0% { transform: rotate(0deg); }
            18% { transform: rotate(34deg); }
            36% { transform: rotate(-28deg); }
            54% { transform: rotate(22deg); }
            72% { transform: rotate(-14deg); }
            100% { transform: rotate(0deg); }
          }

          @keyframes botAntennaSearchSweep {
            0% { transform: rotate(0deg); }
            14% { transform: rotate(0deg); }
            28% { transform: rotate(-44deg); }
            44% { transform: rotate(-44deg); }
            58% { transform: rotate(34deg); }
            74% { transform: rotate(34deg); }
            88% { transform: rotate(0deg); }
            100% { transform: rotate(0deg); }
          }

          @keyframes streamingCaretBlink {
            0%, 42% { opacity: 1; }
            43%, 100% { opacity: 0.12; }
          }

          .bot-icon {
            overflow: visible;
          }

          .streaming-caret {
            animation: streamingCaretBlink 0.92s steps(1, end) infinite;
            box-shadow: 0 0 0 1px rgba(255,255,255,0.16), 0 0 14px rgba(59,130,246,0.18);
          }

          .bot-antenna-stem {
            vector-effect: non-scaling-stroke;
          }

          .bot-eye {
            transform-box: fill-box;
            transform-origin: center;
          }

          .bot-eye.blinking {
            animation: botEyeBlink 220ms ease;
          }

          .bot-antenna-tip {
            transform-box: fill-box;
            transform-origin: right center;
            transition: transform 240ms cubic-bezier(0.22, 1, 0.36, 1);
            vector-effect: non-scaling-stroke;
          }

          .bot-antenna-tip.spinning {
            animation: botAntennaSpin 1.45s cubic-bezier(0.22, 1, 0.36, 1);
          }

          .bot-antenna-tip.searching {
            transform-origin: right center;
            animation: botAntennaSearchSweep 1.7s cubic-bezier(0.4, 0, 0.2, 1) infinite;
          }
        `}
      </style>

      {/* Conversation toolbar — absolutely positioned so it never displaces
          the empty-state's vertical centering. Two icon-only buttons wrapped
          in a hairline pill so they read as a single coherent control. */}
      <div className="absolute right-5 top-3 z-20 flex items-center rounded-xl border border-gray-100 bg-white/80 backdrop-blur-sm shadow-[0_1px_4px_-1px_rgba(15,24,53,0.04)] xl:right-7">
        <button
          type="button"
          onClick={handleStartNewChat}
          aria-label="新建对话"
          title="新建对话"
          className="inline-flex h-8 w-9 items-center justify-center rounded-l-xl text-gray-500 transition-colors duration-150 hover:bg-gray-50 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-200"
        >
          <SquarePen className="h-[15px] w-[15px]" strokeWidth={1.75} />
        </button>
        <span className="h-4 w-px bg-gray-100" aria-hidden="true" />
        <div className="relative">
          <button
            ref={historyButtonRef}
            type="button"
            onClick={() => setHistoryOpen((open) => !open)}
            aria-label="历史对话"
            aria-expanded={historyOpen}
            aria-haspopup="menu"
            title="历史对话"
            className={`inline-flex h-8 w-9 items-center justify-center rounded-r-xl transition-colors duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-200 ${
              historyOpen
                ? "bg-gray-100/70 text-gray-900"
                : "text-gray-500 hover:bg-gray-50 hover:text-gray-900"
            }`}
          >
            <History className="h-[15px] w-[15px]" strokeWidth={1.75} />
          </button>
          {historyOpen && (
            <div
              ref={historyPopoverRef}
              role="menu"
              aria-label="历史对话"
              className="absolute right-0 top-full mt-2 w-[300px] overflow-hidden rounded-xl border border-gray-100 bg-white shadow-[0_10px_28px_-10px_rgba(15,24,53,0.16)] py-1.5 z-30"
            >
              <div className="px-3.5 pt-2 pb-1.5 text-[11px] font-medium text-gray-500">
                历史对话
              </div>

              <div className="max-h-[24rem] overflow-y-auto custom-scrollbar pb-1">
                {sortedConversations.length === 0 ? (
                  <div className="px-3.5 py-6 text-center text-[12px] text-gray-400">
                    暂无历史对话
                  </div>
                ) : (
                  conversationGroups.map((group) => (
                    <div key={group.key} className="mb-0.5 last:mb-0">
                      <div className="px-3.5 pt-2 pb-1 text-[10.5px] font-medium text-gray-400">
                        {group.label}
                      </div>
                      <ul className="flex flex-col px-1.5">
                        {group.items.map((conv) => {
                          const isActive = conv.id === currentConversationId;
                          return (
                            <li key={conv.id}>
                              <div
                                role="menuitem"
                                tabIndex={0}
                                onClick={() => handleSelectConversation(conv.id)}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter" || event.key === " ") {
                                    event.preventDefault();
                                    handleSelectConversation(conv.id);
                                  }
                                }}
                                className={`group flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 transition-colors duration-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-200 ${
                                  isActive ? "bg-gray-100/70" : "hover:bg-gray-50"
                                }`}
                              >
                                <div className="min-w-0 flex-1">
                                  <div
                                    className={`truncate text-[13px] leading-snug ${
                                      isActive ? "font-medium text-gray-900" : "text-gray-700"
                                    }`}
                                  >
                                    {conv.title || "新对话"}
                                  </div>
                                  <div className="mt-0.5 text-[11px] text-gray-400">
                                    {formatHistoryRelativeTime(conv.updatedAt)}
                                  </div>
                                </div>
                                <button
                                  type="button"
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    handleDeleteConversation(conv.id);
                                  }}
                                  aria-label="删除对话"
                                  title="删除对话"
                                  className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-gray-300 opacity-0 transition-opacity duration-100 hover:text-gray-600 group-hover:opacity-100 focus:opacity-100 focus:outline-none"
                                >
                                  <Trash2 className="h-[13px] w-[13px]" strokeWidth={1.75} />
                                </button>
                              </div>
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Scrollable Content Area */}
      <div
        ref={chatScrollContainerRef}
        onScroll={handleChatScroll}
        className="min-h-0 flex-1 overflow-y-auto w-full scrollbar-thin scrollbar-thumb-gray-200 relative z-0 pb-6"
      >
        
        {/* Refined Empty State */}
        {messages.length === 0 && (
          <div
            className="flex flex-col items-center justify-center min-h-full px-8 pt-20 pb-6 max-w-4xl mx-auto w-full animate-in fade-in duration-700"
            onMouseMove={(event) => {
              if (hoveredSuggestion !== null) {
                return;
              }
              pointMascotTo(event.clientX, event.clientY, 1);
            }}
            onMouseLeave={() => {
              if (hoveredSuggestion !== null) {
                return;
              }
              settleMascot();
            }}
          >
            <div className="mb-3 flex items-center justify-center">
              <div
                ref={mascotRef}
                className="relative flex h-[92px] w-[92px] cursor-pointer items-center justify-center"
                onClick={handleMascotClick}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    handleMascotClick();
                  }
                }}
                role="button"
                tabIndex={0}
                aria-label="与搜搜互动"
                style={{
                  transform: `translate3d(${mascotPose.x}px, ${mascotPose.y}px, 0) rotate(${mascotPose.rotate}deg) scale(${mascotPose.scale})`,
                  transition: "transform 560ms cubic-bezier(0.22, 1, 0.36, 1)",
                }}
              >
                <div className="relative z-10 flex h-full w-full items-center justify-center">
                  <AnimatedBotIcon
                    className="bot-icon h-[54px] w-[54px] text-[var(--brand-cyan)]"
                    strokeWidth={2.28}
                    blinking={mascotPose.blink}
                    spinning={isMascotSpinning}
                    style={{
                      transform: isMascotSpinning ? "translate3d(0, 0, 0)" : `translate3d(${mascotPose.x * 0.12}px, ${mascotPose.y * 0.12}px, 0)`,
                      transition: "transform 560ms cubic-bezier(0.22, 1, 0.36, 1), color 280ms ease",
                    }}
                  />
                </div>
              </div>
            </div>
            <h1 className="text-[28px] font-black mb-1 text-gray-900 tracking-tight text-center">
              需要处理什么任务？
            </h1>
            <p className="text-[14px] text-gray-500 mb-6 text-center max-w-md">
              Surfaced 已准备就绪。选择下方快捷指令，或直接输入您的需求。
            </p>
            
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3.5 w-full">
              {suggestions.map((s, i) => (
                <button 
                  key={i} 
                  onClick={() => {
                    void handleSuggestionClick(s);
                  }}
                  onMouseEnter={() => focusSuggestion(i)}
                  onMouseLeave={releaseSuggestionFocus}
                  onFocus={() => focusSuggestion(i)}
                  onBlur={releaseSuggestionFocus}
                  className="flex flex-col items-start p-5 bg-white border border-gray-100 hover:border-blue-200 rounded-2xl hover:shadow-[0_10px_26px_-12px_rgba(91,137,214,0.22)] transition-all duration-300 group"
                >
                  <div className="p-2.5 bg-gray-50 group-hover:bg-blue-50/50 rounded-xl mb-4 transition-colors">
                    {s.icon}
                  </div>
                  <span className="text-[14px] font-bold text-gray-900 mb-1">{s.text}</span>
                  <span className="text-[12px] text-gray-400 font-medium">{s.desc}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Sophisticated Chat List */}
        {messages.length > 0 && (
          <div className="max-w-4xl mx-auto w-full px-8 pt-14 pb-40 space-y-10">
            {messages.map(msg => (
              <div
                key={msg.id}
                data-search-chat-user-id={msg.role === 'user' ? msg.id : undefined}
                className={`flex gap-5 group ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}
              >
                {/* Refined Avatar */}
                {msg.role === 'bot' && (
                  <div className="w-8 h-8 flex items-center justify-center shrink-0 mt-1">
                    <Bot className="w-6 h-6 text-blue-600 drop-shadow-sm" strokeWidth={2.5} />
                  </div>
                )}
                
                {/* Content */}
                <div className={`flex-1 min-w-0 flex flex-col ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
                  {msg.role === 'user' ? (
                    <div className="bg-gray-50 px-5 py-3.5 rounded-2xl rounded-tr-sm text-[14px] text-gray-900 leading-relaxed border border-gray-100 max-w-[75%] shadow-sm">
                      {msg.content}
                    </div>
                  ) : (
                    <div className="px-2 py-1 w-full max-w-[85%]">
                      <div className="font-bold text-[11px] text-gray-400 mb-3 flex items-center gap-1.5 uppercase tracking-widest">
                        {selectedModel?.label || "未选择模型"} 
                        <span className="w-1 h-1 rounded-full bg-blue-500 ml-1"></span>
                        {msg.streaming && (
                          <span className="ml-2 normal-case tracking-normal text-[11px] font-medium text-blue-500">
                            生成中 {formatStreamElapsed(streamElapsedMs)}
                          </span>
                        )}
                      </div>
                      <div className="bg-white">
                        {msg.thinking && (() => {
                          const thinkingExpanded = msg.streaming || Boolean(expandedThinkingIds[msg.id]);
                          return (
                            <div className="mb-4 overflow-hidden rounded-lg border border-gray-200 bg-gray-50/70">
                              <button
                                type="button"
                                className={`flex w-full items-center justify-between gap-3 px-3.5 py-2.5 text-left transition-colors ${
                                  msg.streaming ? "cursor-default" : "hover:bg-gray-100/70"
                                }`}
                                onClick={() => {
                                  if (msg.streaming) {
                                    return;
                                  }
                                  setExpandedThinkingIds((prev) => ({
                                    ...prev,
                                    [msg.id]: !prev[msg.id],
                                  }));
                                }}
                                aria-expanded={thinkingExpanded}
                              >
                                <span className="flex min-w-0 items-center gap-2">
                                  <span
                                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                                      msg.streaming ? "bg-blue-500 animate-pulse" : "bg-gray-300"
                                    }`}
                                  />
                                  <span className="truncate text-[12px] font-semibold text-gray-600">
                                    {msg.streaming ? "思考中" : "已完成思考"}
                                  </span>
                                  <span className="shrink-0 text-[11px] font-medium text-gray-400">
                                    {msg.streaming ? formatStreamElapsed(streamElapsedMs) : `${msg.thinking.length} 字`}
                                  </span>
                                </span>
                                <ChevronDown
                                  className={`h-4 w-4 shrink-0 text-gray-400 transition-transform ${
                                    thinkingExpanded ? "rotate-180" : ""
                                  }`}
                                />
                              </button>
                              {thinkingExpanded && (
                                <div
                                  ref={(node) => {
                                    thinkingScrollRefs.current[msg.id] = node;
                                  }}
                                  className={`custom-scrollbar overflow-y-auto overscroll-contain border-t border-gray-200/70 px-3.5 py-3 ${
                                    msg.streaming ? "h-[188px]" : "max-h-[320px]"
                                  }`}
                                >
                                  {renderMarkdown(msg.thinking, {
                                    showCursor: msg.streaming,
                                    cursorTone: "slate",
                                    muted: true,
                                  })}
                                </div>
                              )}
                            </div>
                          );
                        })()}
                        {renderMarkdown(msg.content, { showCursor: msg.streaming, cursorTone: "blue" })}
                        {msg.streaming && !msg.content && !msg.thinking && (
                          <p className="text-[14px] text-gray-400 leading-relaxed">
                            正在生成...{renderStreamingCursor("blue")}
                          </p>
                        )}
                      </div>
                      
                      {/* Action Bar for Bot Message */}
                      <div className="flex items-center gap-2 mt-4 opacity-0 group-hover:opacity-100 transition-opacity">
                        <button onClick={() => handleCopyMessage(msg)} className="px-2 py-1 rounded text-[11px] font-medium text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors flex items-center gap-1"><Copy className="w-3 h-3" />复制</button>
                        <button
                          onClick={() => handleRegenerate(msg.id)}
                          disabled={msg.streaming || isGenerating}
                          className="px-2 py-1 rounded text-[11px] font-medium text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors flex items-center gap-1 disabled:cursor-not-allowed disabled:opacity-40"
                        ><RefreshCw className="w-3 h-3" />重新生成</button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            ))}
            
            {/* Elegant Generating Skeleton */}
            {isGenerating && !messages.some((msg) => msg.role === "bot" && msg.streaming) && (
              <div className="flex gap-5">
                <div className="w-8 h-8 flex items-center justify-center shrink-0 mt-1">
                  <AnimatedBotIcon
                    className="bot-icon h-7 w-7 text-[var(--brand-cyan)]"
                    strokeWidth={2.5}
                    searching
                    style={{
                      transform: "translate3d(0, 0, 0)",
                    }}
                  />
                </div>
                <div className="flex-1 min-w-0 flex flex-col items-start px-2 py-1">
                  <div className="font-bold text-[11px] text-gray-400 mb-4 flex items-center gap-1.5 uppercase tracking-widest">
                    {selectedModel?.label || "未选择模型"} <span className="w-1 h-1 rounded-full bg-blue-500 ml-1 animate-pulse"></span>
                  </div>
                  <div className="flex flex-col gap-3.5 w-72">
                    <div className="h-2 bg-gray-100 rounded-full animate-pulse w-full"></div>
                    <div className="h-2 bg-gray-100 rounded-full animate-pulse w-5/6"></div>
                    <div className="h-2 bg-gray-100 rounded-full animate-pulse w-4/6"></div>
                  </div>
                </div>
              </div>
            )}
            
            <div className="h-24" />
          </div>
        )}
      </div>

      {/* Sleek Input Area (Sticky Bottom) */}
      <div
        className="relative shrink-0 bg-[#fcfdff] pt-3 pb-8 px-8 z-10 pointer-events-none"
      >
        <div className="max-w-4xl mx-auto w-full pointer-events-auto">
          <div className="bg-white border border-gray-200 rounded-2xl shadow-[0_4px_24px_-6px_rgba(0,0,0,0.06)] focus-within:shadow-[0_8px_32px_-8px_rgba(37,99,235,0.12)] focus-within:border-blue-400 transition-all duration-300">
            
            {/* Minimalist Model Selector */}
            <div className="px-3 pt-3 flex min-h-9 items-center relative border-b border-gray-50 pb-2 mb-1">
              {models.length === 0 ? (
                <span className="inline-flex items-center px-2 py-1 text-[11px] font-bold leading-none text-amber-500 uppercase tracking-wider">
                  请先在系统设置的 API 密钥与模型里配置大模型
                </span>
              ) : (
                <button
                  onClick={() => setShowModelMenu(!showModelMenu)}
                  className="inline-flex min-h-6 items-center gap-1.5 rounded px-2 py-1 text-[11px] font-bold leading-none text-gray-500 uppercase tracking-wider transition-colors hover:text-gray-900"
                >
                  <span className="block leading-none">{selectedModel?.label || "选择模型"}</span>
                  <ChevronDown className="h-3 w-3 shrink-0 self-center" />
                </button>
              )}

              {showModelMenu && models.length > 0 && (
                <div className="absolute bottom-full left-3 mb-2 w-48 bg-white border border-gray-100 rounded-xl shadow-[0_12px_40px_-8px_rgba(0,0,0,0.15)] z-50 py-1.5 overflow-hidden">
                  <div className="px-3 py-1.5 text-[9px] font-bold text-gray-400 uppercase tracking-widest mb-1">选择模型引擎</div>
                  {models.map(option => (
                    <button
                      key={option.key}
                      onClick={() => { setSelectedModelKey(option.key); setShowModelMenu(false); }}
                      className={`flex w-full items-center justify-between gap-3 px-4 py-2 text-left text-[12px] font-medium leading-none transition-colors hover:bg-gray-50 ${option.key === selectedModelKey ? 'bg-blue-50/30 font-bold text-blue-600' : 'text-gray-700'}`}
                    >
                      <span className="block leading-none">{option.label}</span>
                      {option.key === selectedModelKey && <div className="ml-auto w-1.5 h-1.5 rounded-full bg-blue-600"></div>}
                    </button>
                  ))}
                </div>
              )}

              {isGenerating && activeStreamingMessage && (
                <div className="ml-auto flex items-center gap-2 pr-1 text-[11px] font-medium text-gray-400">
                  <span>生成中 {formatStreamElapsed(streamElapsedMs)}</span>
                  <span>回答 {activeReplyLength} 字</span>
                  {activeThinkingLength > 0 && <span>思考 {activeThinkingLength} 字</span>}
                </div>
              )}
            </div>

            {(attachments.length > 0 || attachmentMessage) && (
              <div className="mx-3 mb-1 flex items-center gap-2 border-b border-gray-50 pb-2 text-[11px] text-gray-500">
                {attachments.map((file) => (
                  <span
                    key={file.id}
                    className="inline-flex max-w-[260px] items-center gap-1.5 rounded-md bg-blue-50 px-2 py-1 font-medium text-blue-700"
                    title={file.name}
                  >
                    <span className="truncate">{file.name}</span>
                    <span className="shrink-0 text-blue-400">{formatFileSize(file.size)}</span>
                  </span>
                ))}
                {attachmentMessage && (
                  <span className={isUploadingAttachment ? "text-blue-500" : "text-gray-400"}>
                    {attachmentMessage}
                  </span>
                )}
                {attachments.length > 0 && (
                  <button
                    type="button"
                    onClick={() => {
                      setAttachments([]);
                      setAttachmentMessage("");
                    }}
                    className="ml-auto rounded px-1.5 py-1 text-gray-400 transition-colors hover:bg-gray-50 hover:text-gray-700"
                    title="移除附件"
                  >
                    移除
                  </button>
                )}
              </div>
            )}

            {/* Main Input Row */}
            <div className="flex items-end gap-2 px-3 pb-3 pt-1">
              <input
                ref={fileInputRef}
                type="file"
                accept=".xlsx,.xlsm"
                className="hidden"
                onChange={handleAttachmentPick}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={isGenerating || isUploadingAttachment}
                className="p-2 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-colors shrink-0 mb-0.5 disabled:cursor-not-allowed disabled:opacity-40"
                title="上传表格"
              >
                <Plus className="w-5 h-5" strokeWidth={2} />
              </button>
              
              <textarea 
                ref={textareaRef}
                value={inputValue}
                onChange={(e) => {
                  setInputValue(e.target.value);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    handleSend();
                  }
                }}
                placeholder={
                  composerMode === "brand_task"
                    ? "直接写一段品牌任务需求，我会拆解后创建到品牌任务里..."
                    : composerMode === "quick_todos"
                      ? "直接写一段任务安排，我会拆解成快速待办..."
                      : "直接聊天提问，或让我帮你处理应用里的任务..."
                }
                className="flex-1 bg-transparent resize-none outline-none text-[14px] text-gray-900 placeholder-gray-400 py-2.5 min-h-[44px] max-h-[160px] scrollbar-thin scrollbar-thumb-gray-200 leading-relaxed"
                rows={1}
              />

              <div className="flex items-center gap-1 mb-0.5 shrink-0">
                <button className="p-2 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-colors">
                  <Mic className="w-5 h-5" strokeWidth={2} />
                </button>
                {isGenerating ? (
                  <button
                    onClick={stopStreaming}
                    className="p-2.5 rounded-lg transition-all flex items-center justify-center ml-1 bg-rose-50 text-rose-600 hover:bg-rose-100"
                    title="停止生成"
                  >
                    <Square className="w-4 h-4 fill-current" strokeWidth={2} />
                  </button>
                ) : (
                  <button 
                    onClick={() => handleSend()}
                    disabled={!inputValue.trim()}
                    className={`p-2.5 rounded-lg transition-all flex items-center justify-center ml-1 ${
                      inputValue.trim()
                        ? 'bg-blue-600 text-white shadow-md shadow-blue-600/20 hover:bg-blue-700' 
                        : 'bg-gray-50 text-gray-300'
                    }`}
                  >
                    <Send className="w-4 h-4" strokeWidth={2} />
                  </button>
                )}
              </div>
            </div>
          </div>
          <div className="text-center mt-4">
            <p className="text-[11px] text-gray-400 font-medium tracking-wide">Surfaced 引擎可能会输出不准确的内容，请以最终导出的数据报表为准。</p>
          </div>
        </div>
      </div>
      
    </div>
  );
}
