// Local persistence for the Search chat conversation history.
//
// Kept in its own module so the component only deals with in-memory state
// and so this layer can later be swapped for a backend API without touching
// the UI. Everything is namespaced under versioned localStorage keys so we
// can evolve the shape safely.

export type StoredMessage = {
  id: string;
  role: "user" | "bot";
  content: string;
  thinking?: string;
  rawContent?: string;
  rawThinking?: string;
};

export type StoredConversation = {
  id: string;
  title: string;
  messages: StoredMessage[];
  createdAt: number;
  updatedAt: number;
};

const STORAGE_KEY = "search-chat-conversations:v1";
const CURRENT_ID_KEY = "search-chat-current-id:v1";
// Cap stored history so localStorage usage stays bounded. 50 conversations
// at typical sizes is well under the 5MB per-origin quota.
const MAX_CONVERSATIONS = 50;
const TITLE_MAX_CHARS = 22;

function hasStorage(): boolean {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined";
}

function safeParse<T>(raw: string | null, fallback: T): T {
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object") {
      return parsed as T;
    }
    return fallback;
  } catch {
    return fallback;
  }
}

function sortByUpdatedDesc(list: StoredConversation[]): StoredConversation[] {
  return [...list].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}

export function loadConversations(): StoredConversation[] {
  if (!hasStorage()) return [];
  const list = safeParse<StoredConversation[]>(window.localStorage.getItem(STORAGE_KEY), []);
  if (!Array.isArray(list)) return [];
  return sortByUpdatedDesc(list.filter((item) => item && typeof item.id === "string"));
}

export function saveConversations(list: StoredConversation[]): void {
  if (!hasStorage()) return;
  const trimmed = sortByUpdatedDesc(list).slice(0, MAX_CONVERSATIONS);
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
  } catch {
    // Quota exceeded or storage disabled — silently degrade rather than
    // breaking the chat. The next save attempt will retry.
  }
}

export function loadCurrentConversationId(): string | null {
  if (!hasStorage()) return null;
  return window.localStorage.getItem(CURRENT_ID_KEY);
}

export function saveCurrentConversationId(id: string | null): void {
  if (!hasStorage()) return;
  try {
    if (id) {
      window.localStorage.setItem(CURRENT_ID_KEY, id);
    } else {
      window.localStorage.removeItem(CURRENT_ID_KEY);
    }
  } catch {
    // ignore
  }
}

export function summarizeConversationTitle(
  messages: { role: "user" | "bot"; content: string }[],
): string {
  const firstUser = messages.find((m) => m.role === "user" && m.content.trim().length > 0);
  if (!firstUser) return "新对话";
  const text = firstUser.content.trim().replace(/\s+/g, " ");
  if (!text) return "新对话";
  return text.length > TITLE_MAX_CHARS ? `${text.slice(0, TITLE_MAX_CHARS)}…` : text;
}

export function formatRelativeTime(timestamp: number): string {
  if (!timestamp) return "";
  const delta = Math.max(0, Date.now() - timestamp);
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (delta < minute) return "刚刚";
  if (delta < hour) return `${Math.floor(delta / minute)} 分钟前`;
  if (delta < day) return `${Math.floor(delta / hour)} 小时前`;
  if (delta < 7 * day) return `${Math.floor(delta / day)} 天前`;
  const date = new Date(timestamp);
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

export type ConversationGroup = {
  // Stable key for React reconciliation; never displayed.
  key: "today" | "yesterday" | "week" | "older";
  // Display label shown above the group in the popover.
  label: string;
  items: StoredConversation[];
};

// Bucket conversations into mature-chatbot time groups (今天 / 昨天 /
// 过去 7 天 / 更早). Empty groups are filtered out so the UI never renders
// an orphan header.
export function groupConversationsByTime(list: StoredConversation[]): ConversationGroup[] {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;
  const startOfWeekWindow = startOfToday - 7 * 24 * 60 * 60 * 1000;

  const buckets: Record<ConversationGroup["key"], StoredConversation[]> = {
    today: [],
    yesterday: [],
    week: [],
    older: [],
  };

  for (const conv of list) {
    const ts = conv.updatedAt || 0;
    if (ts >= startOfToday) {
      buckets.today.push(conv);
    } else if (ts >= startOfYesterday) {
      buckets.yesterday.push(conv);
    } else if (ts >= startOfWeekWindow) {
      buckets.week.push(conv);
    } else {
      buckets.older.push(conv);
    }
  }

  const labels: Record<ConversationGroup["key"], string> = {
    today: "今天",
    yesterday: "昨天",
    week: "过去 7 天",
    older: "更早",
  };

  const order: ConversationGroup["key"][] = ["today", "yesterday", "week", "older"];
  return order
    .filter((key) => buckets[key].length > 0)
    .map((key) => ({ key, label: labels[key], items: buckets[key] }));
}

export function generateConversationId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `conv-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
