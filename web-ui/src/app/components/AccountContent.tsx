import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CalendarDays, Clock, Edit2, LogOut, Save, Upload, RotateCcw, User, Link2, Plus, Trash2 } from "lucide-react";
import { ImageWithFallback } from "./figma/ImageWithFallback";
import { ConfirmModal } from "./ConfirmModal";
import { DatePickerField } from "./ui/date-picker-field";
import { fetchSettings, saveProfile, saveSettings, type ArticleAccountSnapshot } from "../lib/backend";
import { notifySaveSuccess } from "../lib/saveToast";

type AvatarCropDraft = {
  src: string;
  fileName: string;
  width: number;
  height: number;
  baseScale: number;
  zoom: number;
  offsetX: number;
  offsetY: number;
};

const DEFAULT_PROFILE_AVATAR =
  "https://images.unsplash.com/photo-1624091844772-554661d10173?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxtaW5pbWFsaXN0JTIwcHJvZmVzc2lvbmFsJTIwcG9ydHJhaXQlMjBhc2lhbiUyMHdvbWFufGVufDF8fHx8MTc3NDU0MTU0MHww&ixlib=rb-4.1.0&q=80&w=1080&utm_source=figma&utm_medium=referral";
const AVATAR_CROP_VIEWPORT = 240;
const AVATAR_OUTPUT_SIZE = 320;
const AVATAR_MAX_ZOOM = 3;
const ARTICLE_ACCOUNT_PLATFORM_LABELS: Record<string, string> = {
  toutiao: "头条号",
  sohu: "搜狐号",
  zhihu: "知乎",
  cnblogs: "博客园",
  rss: "RSS",
  unknown: "未知平台",
};

type ArticleAccountGroup = {
  key: string;
  label: string;
  latestCrawledAt: string;
  accounts: ArticleAccountSnapshot[];
};

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") {
        resolve(reader.result);
        return;
      }
      reject(new Error("读取头像失败"));
    };
    reader.onerror = () => reject(new Error("读取头像失败"));
    reader.readAsDataURL(file);
  });
}

function loadImage(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("头像解析失败"));
    img.src = dataUrl;
  });
}

function getAvatarCropMetrics(draft: AvatarCropDraft, zoom = draft.zoom) {
  const displayWidth = draft.width * draft.baseScale * zoom;
  const displayHeight = draft.height * draft.baseScale * zoom;
  return {
    displayWidth,
    displayHeight,
    maxOffsetX: Math.max(0, (displayWidth - AVATAR_CROP_VIEWPORT) / 2),
    maxOffsetY: Math.max(0, (displayHeight - AVATAR_CROP_VIEWPORT) / 2),
  };
}

function clampAvatarCropOffset(draft: AvatarCropDraft, offsetX: number, offsetY: number, zoom = draft.zoom) {
  const metrics = getAvatarCropMetrics(draft, zoom);
  return {
    offsetX: Math.min(metrics.maxOffsetX, Math.max(-metrics.maxOffsetX, offsetX)),
    offsetY: Math.min(metrics.maxOffsetY, Math.max(-metrics.maxOffsetY, offsetY)),
  };
}

function ensureArticleAccountUrlScheme(url: string) {
  const raw = String(url || "").trim();
  if (!raw) {
    return "";
  }
  return /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(raw) ? raw : `https://${raw}`;
}

function inferArticleAccountPlatform(url: string) {
  const normalized = ensureArticleAccountUrlScheme(url);
  if (!normalized) {
    return "unknown";
  }
  try {
    const parsed = new URL(normalized);
    const host = parsed.hostname.toLowerCase();
    const path = parsed.pathname.toLowerCase();
    if (host.includes("cnblogs.com")) return "cnblogs";
    if (host.includes("sohu.com")) return "sohu";
    if (host.includes("toutiao.com") || host.includes("toutiaohao.com")) return "toutiao";
    if (host.includes("zhihu.com")) return "zhihu";
    if (host.includes("rsshub") || path.endsWith(".xml") || path.endsWith(".rss") || path.includes("/rss")) return "rss";
  } catch {
    return "unknown";
  }
  return "unknown";
}

function getArticleAccountPlatformKey(account: ArticleAccountSnapshot) {
  const savedPlatform = String(account.platform || "").trim().toLowerCase();
  if (savedPlatform && savedPlatform !== "unknown") {
    return savedPlatform;
  }
  return inferArticleAccountPlatform(account.url);
}

function getArticleAccountPlatformLabel(account: ArticleAccountSnapshot) {
  const key = getArticleAccountPlatformKey(account);
  const savedLabel = String(account.platform_label || "").trim();
  if (savedLabel && savedLabel !== ARTICLE_ACCOUNT_PLATFORM_LABELS.unknown) {
    return savedLabel;
  }
  return ARTICLE_ACCOUNT_PLATFORM_LABELS[key] || key || ARTICLE_ACCOUNT_PLATFORM_LABELS.unknown;
}

function getLatestArticleAccountCrawledAt(current: string, next?: string) {
  const value = String(next || "").trim();
  if (!value) {
    return current;
  }
  if (!current) {
    return value;
  }
  return value > current ? value : current;
}

async function buildAvatarCropDraft(file: File): Promise<AvatarCropDraft> {
  const src = await readFileAsDataUrl(file);
  const img = await loadImage(src);
  const width = img.naturalWidth || img.width;
  const height = img.naturalHeight || img.height;
  const baseScale = Math.max(AVATAR_CROP_VIEWPORT / width, AVATAR_CROP_VIEWPORT / height);
  return {
    src,
    fileName: file.name,
    width,
    height,
    baseScale,
    zoom: 1,
    offsetX: 0,
    offsetY: 0,
  };
}

async function renderAvatarFromCrop(draft: AvatarCropDraft): Promise<string> {
  const img = await loadImage(draft.src);
  const canvas = document.createElement("canvas");
  canvas.width = AVATAR_OUTPUT_SIZE;
  canvas.height = AVATAR_OUTPUT_SIZE;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return draft.src;
  }

  const ratio = AVATAR_OUTPUT_SIZE / AVATAR_CROP_VIEWPORT;
  const { displayWidth, displayHeight } = getAvatarCropMetrics(draft);
  const drawWidth = displayWidth * ratio;
  const drawHeight = displayHeight * ratio;
  const drawX = (AVATAR_OUTPUT_SIZE - drawWidth) / 2 + draft.offsetX * ratio;
  const drawY = (AVATAR_OUTPUT_SIZE - drawHeight) / 2 + draft.offsetY * ratio;

  ctx.fillStyle = "#f3f4f6";
  ctx.fillRect(0, 0, AVATAR_OUTPUT_SIZE, AVATAR_OUTPUT_SIZE);
  ctx.drawImage(img, drawX, drawY, drawWidth, drawHeight);
  return canvas.toDataURL("image/jpeg", 0.9);
}

export function AccountContent({
  isAuthenticated,
  onSaveSuccess,
  onProfileSaved,
  onLogout,
}: {
  isAuthenticated?: boolean;
  onSaveSuccess?: (message?: string) => void;
  onProfileSaved?: () => void | Promise<void>;
  onLogout?: () => void;
}) {
  const [profile, setProfile] = useState({
    name: "林见鹿",
    role: "资深 AI 运营",
    birthday: "1996-08-12",
    hireDate: "2022-10-24",
    avatar: DEFAULT_PROFILE_AVATAR,
  });
  const [editProfile, setEditProfile] = useState(profile);
  const [isEditingProfile, setIsEditingProfile] = useState(false);
  const [showLogoutConfirm, setShowLogoutConfirm] = useState(false);
  const [avatarUploadMessage, setAvatarUploadMessage] = useState("");
  const [avatarCropDraft, setAvatarCropDraft] = useState<AvatarCropDraft | null>(null);
  const [articleAccounts, setArticleAccounts] = useState<ArticleAccountSnapshot[]>([]);
  const [articleAccountUrl, setArticleAccountUrl] = useState("");
  const [articleAccountMessage, setArticleAccountMessage] = useState("");
  const [articleAccountsSaving, setArticleAccountsSaving] = useState(false);
  const avatarDragRef = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null);

  useEffect(() => {
    fetchSettings().then((data) => {
      const prof = data.profile as Record<string, string> | undefined;
      if (prof) {
        const nextProfile = {
          name: prof.name || "林见鹿",
          role: prof.role || "资深 AI 运营",
          birthday: prof.birthday || "1996-08-12",
          hireDate: prof.hire_date || "2022-10-24",
          avatar: prof.avatar || DEFAULT_PROFILE_AVATAR,
        };
        setProfile(nextProfile);
        setEditProfile(nextProfile);
      }
      const accountCrawling = data.account_crawling as { accounts?: ArticleAccountSnapshot[] } | undefined;
      if (Array.isArray(accountCrawling?.accounts)) {
        setArticleAccounts(accountCrawling.accounts);
      }
    });
  }, []);

  const hireDays = useMemo(() => {
    const start = new Date(profile.hireDate);
    const now = new Date();
    if (Number.isNaN(start.getTime())) {
      return 0;
    }
    const diffTime = Math.abs(now.getTime() - start.getTime());
    return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
  }, [profile.hireDate]);

  const articleAccountGroups = useMemo<ArticleAccountGroup[]>(() => {
    const groups: ArticleAccountGroup[] = [];
    const groupByKey = new Map<string, ArticleAccountGroup>();

    articleAccounts.forEach((account) => {
      const key = getArticleAccountPlatformKey(account);
      const label = getArticleAccountPlatformLabel(account);
      let group = groupByKey.get(key);
      if (!group) {
        group = {
          key,
          label,
          latestCrawledAt: "",
          accounts: [],
        };
        groupByKey.set(key, group);
        groups.push(group);
      }
      group.accounts.push(account);
      group.latestCrawledAt = getLatestArticleAccountCrawledAt(group.latestCrawledAt, account.last_crawled_at);
    });

    return groups;
  }, [articleAccounts]);

  const handleAvatarFileChange = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) {
      return;
    }
    if (!file.type.startsWith("image/")) {
      setAvatarUploadMessage("请选择图片文件");
      return;
    }
    try {
      const draft = await buildAvatarCropDraft(file);
      setAvatarCropDraft(draft);
      setAvatarUploadMessage(`已载入头像：${file.name}，请拖动并裁切`);
    } catch {
      setAvatarUploadMessage("头像处理失败，请换一张图片再试");
    }
  }, []);

  const handleAvatarCropZoomChange = useCallback((value: string) => {
    setAvatarCropDraft((prev) => {
      if (!prev) {
        return prev;
      }
      const nextZoom = Math.min(AVATAR_MAX_ZOOM, Math.max(1, Number(value) || 1));
      return {
        ...prev,
        zoom: nextZoom,
        ...clampAvatarCropOffset(prev, prev.offsetX, prev.offsetY, nextZoom),
      };
    });
  }, []);

  const handleApplyAvatarCrop = useCallback(async () => {
    if (!avatarCropDraft) {
      return;
    }
    try {
      const avatar = await renderAvatarFromCrop(avatarCropDraft);
      setEditProfile((prev) => ({ ...prev, avatar }));
      setAvatarCropDraft(null);
      setAvatarUploadMessage("头像裁切已应用");
    } catch {
      setAvatarUploadMessage("头像裁切失败，请重新上传后再试");
    }
  }, [avatarCropDraft]);

  const handleAvatarCropPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!avatarCropDraft) {
      return;
    }
    avatarDragRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      originX: avatarCropDraft.offsetX,
      originY: avatarCropDraft.offsetY,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }, [avatarCropDraft]);

  const handleAvatarCropPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = avatarDragRef.current;
    if (!drag) {
      return;
    }
    const deltaX = event.clientX - drag.startX;
    const deltaY = event.clientY - drag.startY;
    setAvatarCropDraft((prev) => {
      if (!prev) {
        return prev;
      }
      const nextOffset = clampAvatarCropOffset(prev, drag.originX + deltaX, drag.originY + deltaY);
      return { ...prev, ...nextOffset };
    });
  }, []);

  const handleAvatarCropPointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    avatarDragRef.current = null;
  }, []);

  const handleSaveProfile = useCallback(async () => {
    let nextProfile = editProfile;
    if (avatarCropDraft) {
      try {
        const avatar = await renderAvatarFromCrop(avatarCropDraft);
        nextProfile = { ...editProfile, avatar };
        setEditProfile(nextProfile);
      } catch {
        setAvatarUploadMessage("头像裁切失败，请重新上传后再试");
        return;
      }
    }

    setProfile(nextProfile);
    setIsEditingProfile(false);
    setAvatarCropDraft(null);
    setAvatarUploadMessage("");

    await saveProfile({
      name: nextProfile.name,
      role: nextProfile.role,
      avatar: nextProfile.avatar,
      birthday: nextProfile.birthday,
      hire_date: nextProfile.hireDate,
    });

    if (onProfileSaved) {
      await onProfileSaved();
    }
    notifySaveSuccess(onSaveSuccess, "保存成功");
  }, [avatarCropDraft, editProfile, onProfileSaved, onSaveSuccess]);

  const handleAddArticleAccount = useCallback(() => {
    const url = ensureArticleAccountUrlScheme(articleAccountUrl);
    if (!url) {
      setArticleAccountMessage("请先填写自媒体账号主页链接");
      return;
    }
    const duplicate = articleAccounts.some((account) => ensureArticleAccountUrlScheme(account.url) === url);
    if (duplicate) {
      setArticleAccountMessage("这个自媒体账号主页已经在列表里了");
      return;
    }
    const platform = inferArticleAccountPlatform(url);
    setArticleAccounts((prev) => [
      ...prev,
      {
        id: `local-${Date.now()}`,
        name: "",
        url,
        platform,
        platform_label: ARTICLE_ACCOUNT_PLATFORM_LABELS[platform] || ARTICLE_ACCOUNT_PLATFORM_LABELS.unknown,
        enabled: true,
      },
    ]);
    setArticleAccountUrl("");
    setArticleAccountMessage("已加入列表，记得保存自媒体账号");
  }, [articleAccountUrl, articleAccounts]);

  const updateArticleAccount = useCallback((id: string, patch: Partial<ArticleAccountSnapshot>) => {
    setArticleAccounts((prev) => prev.map((account) => (
      account.id === id ? { ...account, ...patch } : account
    )));
  }, []);

  const handleSaveArticleAccounts = useCallback(async () => {
    setArticleAccountsSaving(true);
    setArticleAccountMessage("");
    try {
      const accounts = articleAccounts
        .map((account) => ({
          id: String(account.id || "").trim(),
          name: String(account.name || "").trim(),
          url: String(account.url || "").trim(),
          platform: String(account.platform || "").trim(),
          enabled: account.enabled !== false,
        }))
        .filter((account) => account.url);
      const result = await saveSettings({
        account_crawling: {
          accounts,
        },
      });
      if (!result.ok) {
        setArticleAccountMessage("自媒体账号保存失败");
        return;
      }
      const refreshed = await fetchSettings();
      const accountCrawling = refreshed.account_crawling as { accounts?: ArticleAccountSnapshot[] } | undefined;
      if (Array.isArray(accountCrawling?.accounts)) {
        setArticleAccounts(accountCrawling.accounts);
      }
      setArticleAccountMessage(`已保存 ${accounts.length} 个自媒体账号`);
      notifySaveSuccess(onSaveSuccess, "自媒体账号主页链接已保存");
    } finally {
      setArticleAccountsSaving(false);
    }
  }, [articleAccounts, onSaveSuccess]);

  if (!isAuthenticated) {
    return null;
  }

  return (
    <div className="flex-1 h-full overflow-hidden bg-transparent px-8 py-8 xl:px-10 flex flex-col">
      <div className="flex justify-between items-end border-b border-gray-200/70 pb-6 mb-6 shrink-0">
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <User className="w-5 h-5 text-blue-600" />
            <h1 className="app-wordmark-heading text-[20px]">账号信息</h1>
          </div>
          <span className="text-[12px] font-medium text-gray-500 tracking-wide">管理个人资料、头像和基础身份信息</span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto pr-3 -mr-3 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent pb-10">
        <div className="max-w-4xl">
          <div className="flex items-start justify-between gap-4 pb-6 border-b border-gray-200/80">
            <div className="flex items-start gap-4 min-w-0">
              <div className="w-20 h-20 rounded-[22px] overflow-hidden bg-white border border-gray-200/80 shadow-sm shrink-0">
                <ImageWithFallback
                  src={(isEditingProfile ? editProfile.avatar : profile.avatar) || DEFAULT_PROFILE_AVATAR}
                  alt="Avatar"
                  className="w-full h-full object-cover"
                />
              </div>
              <div className="min-w-0 pt-1">
                <h2 className="text-[22px] font-black text-gray-900 tracking-tight leading-none">
                  {isEditingProfile ? (editProfile.name || "未设置姓名") : profile.name}
                </h2>
                <div className="mt-2 text-[13px] font-medium text-gray-500">
                  {isEditingProfile ? (editProfile.role || "未设置职位") : profile.role}
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-[12px] text-gray-500">
                  <span className="inline-flex items-center gap-1.5">
                    <CalendarDays className="w-3.5 h-3.5" />
                    生日 {isEditingProfile ? (editProfile.birthday ? editProfile.birthday.replace(/-/g, ".") : "未设置") : profile.birthday.replace(/-/g, ".")}
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <Clock className="w-3.5 h-3.5" />
                    入职 {isEditingProfile ? (editProfile.hireDate ? editProfile.hireDate.replace(/-/g, ".") : "未设置") : profile.hireDate.replace(/-/g, ".")}
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <User className="w-3.5 h-3.5" />
                    在岗 {hireDays.toLocaleString()} 天
                  </span>
                </div>
              </div>
            </div>

            {!isEditingProfile ? (
              <div className="flex items-center gap-2 shrink-0">
                <button
                  onClick={() => {
                    setEditProfile(profile);
                    setAvatarCropDraft(null);
                    setAvatarUploadMessage("");
                    setIsEditingProfile(true);
                  }}
                  className="inline-flex items-center gap-1.5 px-0 py-1 text-[12px] font-bold text-gray-600 hover:text-gray-900 transition-colors"
                >
                  <Edit2 className="w-3.5 h-3.5" />
                  编辑资料
                </button>
                <span className="text-gray-200">/</span>
                <button
                  onClick={() => setShowLogoutConfirm(true)}
                  className="inline-flex items-center gap-1.5 px-0 py-1 text-[12px] font-bold text-gray-400 hover:text-red-600 transition-colors"
                >
                  <LogOut className="w-3.5 h-3.5" />
                  退出登录
                </button>
              </div>
            ) : null}
          </div>

          {isEditingProfile ? (
            <div className="pt-7 animate-in fade-in duration-200">
              <div className="grid gap-10 xl:grid-cols-[260px_minmax(0,1fr)]">
                <div>
                  <div className="text-[12px] font-bold text-gray-900">头像</div>
                  <div className="mt-3 flex items-center gap-3">
                    <label className="inline-flex items-center gap-1.5 text-[12px] font-bold text-gray-900 hover:text-black transition-colors cursor-pointer">
                      <Upload className="w-3.5 h-3.5" />
                      上传头像
                      <input
                        type="file"
                        accept="image/png,image/jpeg,image/webp,image/gif"
                        className="hidden"
                        onChange={handleAvatarFileChange}
                      />
                    </label>
                    <button
                      type="button"
                      onClick={() => {
                        setAvatarCropDraft(null);
                        setEditProfile((prev) => ({ ...prev, avatar: "" }));
                        setAvatarUploadMessage("已切换为默认头像");
                      }}
                      className="inline-flex items-center gap-1.5 text-[12px] font-bold text-gray-400 hover:text-gray-700 transition-colors"
                    >
                      <RotateCcw className="w-3.5 h-3.5" />
                      恢复默认
                    </button>
                  </div>
                  <div className="mt-2 text-[11px] leading-relaxed text-gray-500">
                    支持 JPG、PNG、WEBP、GIF。头像会同步到左侧账号状态。
                  </div>
                  {avatarUploadMessage ? (
                    <div className="mt-2 text-[11px] font-medium text-blue-600">{avatarUploadMessage}</div>
                  ) : null}

                  {avatarCropDraft ? (
                    <div className="mt-5 space-y-4">
                      <AvatarCropViewport
                        draft={avatarCropDraft}
                        onPointerDown={handleAvatarCropPointerDown}
                        onPointerMove={handleAvatarCropPointerMove}
                        onPointerUp={handleAvatarCropPointerUp}
                      />
                      <div className="flex items-center justify-between text-[11px] font-medium text-gray-500">
                        <span>缩放</span>
                        <span>{Math.round(avatarCropDraft.zoom * 100)}%</span>
                      </div>
                      <input
                        type="range"
                        min="1"
                        max={String(AVATAR_MAX_ZOOM)}
                        step="0.01"
                        value={avatarCropDraft.zoom}
                        onChange={(event) => handleAvatarCropZoomChange(event.target.value)}
                        className="w-full accent-gray-900"
                      />
                      <div className="flex items-center gap-4 text-[12px] font-bold">
                        <button
                          type="button"
                          onClick={() => {
                            setAvatarCropDraft(null);
                            setAvatarUploadMessage("已取消本次头像裁切");
                          }}
                          className="text-gray-400 hover:text-gray-700 transition-colors"
                        >
                          取消裁切
                        </button>
                        <button
                          type="button"
                          onClick={handleApplyAvatarCrop}
                          className="text-gray-900 hover:text-black transition-colors"
                        >
                          应用裁切
                        </button>
                      </div>
                    </div>
                  ) : null}
                </div>

                <div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-5">
                    <Field label="姓名">
                      <input
                        type="text"
                        value={editProfile.name}
                        onChange={(event) => setEditProfile({ ...editProfile, name: event.target.value })}
                        className="w-full border-0 border-b border-gray-200 bg-transparent px-0 py-2 text-[14px] text-gray-900 outline-none focus:border-gray-900 transition-colors"
                      />
                    </Field>
                    <Field label="职位">
                      <input
                        type="text"
                        value={editProfile.role}
                        onChange={(event) => setEditProfile({ ...editProfile, role: event.target.value })}
                        className="w-full border-0 border-b border-gray-200 bg-transparent px-0 py-2 text-[14px] text-gray-900 outline-none focus:border-gray-900 transition-colors"
                      />
                    </Field>
                    <DatePickerField
                      label="生日"
                      value={editProfile.birthday}
                      onChange={(value) => setEditProfile({ ...editProfile, birthday: value })}
                      fromYear={1950}
                      toYear={new Date().getFullYear() + 1}
                      triggerClassName="border-0 border-b border-gray-200 rounded-none px-0 py-2 shadow-none hover:border-gray-300 focus:border-gray-900"
                    />
                    <DatePickerField
                      label="入职时间"
                      value={editProfile.hireDate}
                      onChange={(value) => setEditProfile({ ...editProfile, hireDate: value })}
                      fromYear={1990}
                      toYear={new Date().getFullYear() + 1}
                      triggerClassName="border-0 border-b border-gray-200 rounded-none px-0 py-2 shadow-none hover:border-gray-300 focus:border-gray-900"
                    />
                  </div>

                  <div className="mt-8 grid grid-cols-1 md:grid-cols-3 gap-x-8 gap-y-4 border-t border-gray-200/80 pt-6">
                    <SimpleMeta label="当前姓名" value={editProfile.name || "未设置"} />
                    <SimpleMeta label="当前职位" value={editProfile.role || "未设置"} />
                    <SimpleMeta label="入职天数" value={`${(() => {
                      const start = new Date(editProfile.hireDate);
                      if (Number.isNaN(start.getTime())) return 0;
                      return Math.ceil(Math.abs(Date.now() - start.getTime()) / (1000 * 60 * 60 * 24));
                    })().toLocaleString()} 天`} />
                  </div>

                  <div className="mt-8 flex justify-end gap-5 border-t border-gray-200/80 pt-5 text-[12px] font-bold">
                    <button
                      onClick={() => {
                        setAvatarCropDraft(null);
                        setAvatarUploadMessage("");
                        setEditProfile(profile);
                        setIsEditingProfile(false);
                      }}
                      className="text-gray-400 hover:text-gray-700 transition-colors"
                    >
                      取消
                    </button>
                    <button
                      onClick={handleSaveProfile}
                      className="inline-flex items-center gap-1.5 text-gray-900 hover:text-black transition-colors"
                    >
                      <Save className="w-3.5 h-3.5" />
                      保存信息
                    </button>
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="pt-7 animate-in fade-in duration-200">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-x-10 gap-y-6">
                <DetailItem label="姓名" value={profile.name} />
                <DetailItem label="职位" value={profile.role} />
                <DetailItem label="生日" value={profile.birthday.replace(/-/g, ".")} />
                <DetailItem label="入职时间" value={profile.hireDate.replace(/-/g, ".")} />
              </div>

              <div className="mt-8 pt-6 border-t border-gray-200/80 grid grid-cols-1 md:grid-cols-3 gap-x-10 gap-y-4">
                <SimpleMeta label="在岗时长" value={`${hireDays.toLocaleString()} 天`} />
                <SimpleMeta label="头像状态" value={profile.avatar ? "已设置" : "默认"} />
                <SimpleMeta label="资料同步" value="已生效" />
              </div>
            </div>
          )}
        </div>

        <section className="mt-8 max-w-4xl border-t border-gray-200/80 pt-7">
          <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2">
                <Link2 className="h-4 w-4 text-blue-600" />
                <h2 className="text-[16px] font-black text-gray-900 tracking-tight">自媒体账号主页链接</h2>
              </div>
              <p className="mt-1 text-[12px] font-medium leading-relaxed text-gray-500">
                填搜狐号、头条号、知乎、博客园等自媒体账号主页；系统会按设置频率抓取首页 / 接口最新列表。
              </p>
            </div>
            <button
              type="button"
              onClick={handleSaveArticleAccounts}
              disabled={articleAccountsSaving}
              className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-600 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Save className="h-3.5 w-3.5" />
              {articleAccountsSaving ? "保存中..." : "保存自媒体账号"}
            </button>
          </div>

          <div className="mb-4 flex gap-3">
            <input
              type="text"
              value={articleAccountUrl}
              onChange={(event) => {
                setArticleAccountUrl(event.target.value);
                if (articleAccountMessage) setArticleAccountMessage("");
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  handleAddArticleAccount();
                }
              }}
              placeholder="粘贴自媒体账号主页链接，例如 https://www.cnblogs.com/用户名/"
              className="min-w-0 flex-1 border-0 border-b border-gray-200 bg-transparent px-0 py-2 text-[13px] text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-gray-900"
            />
            <button
              type="button"
              onClick={handleAddArticleAccount}
              className="inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-900 transition-colors hover:text-black"
            >
              <Plus className="h-3.5 w-3.5" />
              添加
            </button>
          </div>

          {articleAccountMessage ? (
            <div className="mb-4 text-[12px] font-medium text-blue-600">{articleAccountMessage}</div>
          ) : null}

          <div className="border-t border-gray-200/80">
            {articleAccounts.length > 0 ? (
              articleAccountGroups.map((group) => (
                <div key={group.key} className="border-b border-gray-200/80 py-4">
                  <div className="mb-3 flex flex-wrap items-start justify-between gap-x-5 gap-y-1">
                    <div className="flex items-center gap-2">
                      <div className="text-[12px] font-black text-gray-900">{group.label}</div>
                      <div className="text-[11px] font-medium text-gray-400">{group.accounts.length} 个账号</div>
                    </div>
                    <div className="text-[10px] font-medium text-gray-400">
                      {group.latestCrawledAt ? `上次抓取 ${group.latestCrawledAt}` : "尚未抓取"}
                    </div>
                  </div>

                  <div className="border-t border-gray-100">
                    {group.accounts.map((account) => (
                      <div
                        key={account.id}
                        className="border-b border-gray-100 py-3 transition-colors last:border-b-0 hover:bg-gray-50/40"
                      >
                        <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
                          <div className="min-w-0">
                            <input
                              type="text"
                              value={account.url}
                              spellCheck={false}
                              title={account.url}
                              onChange={(event) => {
                                const nextUrl = event.target.value;
                                const nextPlatform = inferArticleAccountPlatform(nextUrl);
                                updateArticleAccount(account.id, {
                                  url: nextUrl,
                                  platform: nextPlatform,
                                  platform_label: ARTICLE_ACCOUNT_PLATFORM_LABELS[nextPlatform] || ARTICLE_ACCOUNT_PLATFORM_LABELS.unknown,
                                });
                              }}
                              onKeyDown={(event) => {
                                if (event.key === "Enter") {
                                  event.preventDefault();
                                }
                              }}
                              className="block w-full min-w-0 truncate border-0 border-b border-gray-200 bg-transparent px-0 pb-2 text-[13px] font-semibold text-gray-900 outline-none transition-colors focus:border-gray-900"
                            />
                            <input
                              type="text"
                              value={account.name || ""}
                              onChange={(event) => updateArticleAccount(account.id, { name: event.target.value })}
                              placeholder="账号备注名（可选）"
                              className="mt-2 w-full border-0 bg-transparent px-0 py-1 text-[12px] text-gray-500 outline-none placeholder:text-gray-400"
                            />
                          </div>
                          <div className="flex items-center justify-start gap-4 md:justify-end">
                            <button
                              type="button"
                              onClick={() => updateArticleAccount(account.id, { enabled: !account.enabled })}
                              className={`text-[11px] font-bold transition-colors ${account.enabled ? "text-gray-700 hover:text-gray-900" : "text-gray-400 hover:text-gray-600"}`}
                            >
                              {account.enabled ? "已启用" : "已停用"}
                            </button>
                            <button
                              type="button"
                              onClick={() => setArticleAccounts((prev) => prev.filter((item) => item.id !== account.id))}
                              className="text-gray-300 transition-colors hover:text-red-500"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </div>
                        {account.last_message ? (
                          <div className="mt-2 text-[11px] text-gray-500">
                            最近结果：{account.last_message}
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <div className="border-b border-gray-100 py-8 text-center text-[13px] font-medium text-gray-400">
                暂无自媒体账号主页链接，添加后可以在全部文章汇总页手动抓取。
              </div>
            )}
          </div>
        </section>
      </div>

      <ConfirmModal
        isOpen={showLogoutConfirm}
        onClose={() => setShowLogoutConfirm(false)}
        onConfirm={() => {
          setShowLogoutConfirm(false);
          onLogout?.();
        }}
        title="确认退出登录？"
        message="退出后会切换为访客模式，本地活动仍可查看，但账号信息与高级权限会暂时收起，确认退出吗？"
        confirmText="退出"
        type="danger"
      />
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-[12px] font-bold text-gray-500">{label}</label>
      {children}
    </div>
  );
}

function DetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="pb-3 border-b border-gray-200/70">
      <div className="text-[11px] font-bold tracking-wide text-gray-400">{label}</div>
      <div className="mt-2 text-[16px] font-semibold text-gray-900 leading-snug">{value}</div>
    </div>
  );
}

function SimpleMeta({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div>
      <div className="text-[11px] font-bold tracking-wide text-gray-400">{label}</div>
      <div className="mt-2 text-[14px] font-semibold text-gray-900">{value}</div>
    </div>
  );
}

function AvatarCropViewport({
  draft,
  onPointerDown,
  onPointerMove,
  onPointerUp,
}: {
  draft: AvatarCropDraft;
  onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => void;
  onPointerMove: (event: React.PointerEvent<HTMLDivElement>) => void;
  onPointerUp: (event: React.PointerEvent<HTMLDivElement>) => void;
}) {
  const { displayWidth, displayHeight } = getAvatarCropMetrics(draft);

  return (
    <div
      className="relative rounded-[28px] overflow-hidden border border-gray-200 bg-white shadow-sm shrink-0 select-none touch-none cursor-grab active:cursor-grabbing"
      style={{ width: AVATAR_CROP_VIEWPORT, height: AVATAR_CROP_VIEWPORT }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      <img
        src={draft.src}
        alt="Avatar Crop"
        draggable={false}
        className="absolute max-w-none pointer-events-none"
        style={{
          width: displayWidth,
          height: displayHeight,
          left: `calc(50% + ${draft.offsetX}px)`,
          top: `calc(50% + ${draft.offsetY}px)`,
          transform: "translate(-50%, -50%)",
        }}
      />
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,transparent_56%,rgba(17,24,39,0.12)_100%)] pointer-events-none" />
      <div className="absolute inset-[18px] border border-white/90 rounded-[22px] shadow-[0_0_0_1px_rgba(17,24,39,0.08)] pointer-events-none" />
      <div className="absolute left-1/2 top-1/2 w-10 h-10 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/90 pointer-events-none" />
    </div>
  );
}
