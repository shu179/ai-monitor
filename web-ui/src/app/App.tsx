import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { Sidebar } from "./components/Sidebar";
import { RightSidebar } from "./components/RightSidebar";
import { SaveSuccessToast } from "./components/SaveSuccessToast";
import { LoginScreen, type LoginPayload } from "./components/LoginScreen";
import { SAVE_SUCCESS_TOAST_EVENT } from "./lib/saveToast";
import {
  ARTICLE_DATA_CHANGED_EVENT,
  FALLBACK_BOOTSTRAP,
  TASK_DATA_CHANGED_EVENT,
  areArticlesEqual,
  areTodosEqual,
  fetchBootstrap,
  invalidateBootstrapCache,
  readTodoCache,
  warmBootstrapCache,
  warmSettingsCache,
  warmTasksFullCache,
  writeTodoCache,
  setMonitoringEnabled,
  type BootstrapPayload,
} from "./lib/backend";

const AUTH_STORAGE_KEY = "surfaced-web-ui-authenticated";
const LOGIN_IDENTITY_STORAGE_KEY = "surfaced-web-ui-login-identity";
const LOGIN_MODAL_WIDTH = 332;
const LOGIN_MODAL_HEIGHT = 430;

const loadCenterContent = () => import("./components/CenterContent").then((module) => ({ default: module.CenterContent }));
const loadBrandsContent = () => import("./components/BrandsContent").then((module) => ({ default: module.BrandsContent }));
const loadOcrFloatingWindow = () => import("./components/OcrFloatingWindow").then((module) => ({ default: module.OcrFloatingWindow }));
const loadSearchContent = () => import("./components/SearchContent").then((module) => ({ default: module.SearchContent }));
const loadAPIContent = () => import("./components/APIContent").then((module) => ({ default: module.APIContent }));
const loadSettingsContent = () => import("./components/SettingsContent").then((module) => ({ default: module.SettingsContent }));
const preloadSettingsContent = () => {
  warmSettingsCache();
  return loadSettingsContent();
};
const loadAccountContent = () => import("./components/AccountContent").then((module) => ({ default: module.AccountContent }));
const loadReleaseContent = () => import("./components/ReleaseContent").then((module) => ({ default: module.ReleaseContent }));

const CenterContent = lazy(loadCenterContent);
const BrandsContent = lazy(loadBrandsContent);
const OcrFloatingWindow = lazy(loadOcrFloatingWindow);
const SearchContent = lazy(loadSearchContent);
const APIContent = lazy(loadAPIContent);
const SettingsContent = lazy(loadSettingsContent);
const AccountContent = lazy(loadAccountContent);
const ReleaseContent = lazy(loadReleaseContent);

const preloadPageByTab: Record<string, () => Promise<unknown>> = {
  看板: loadCenterContent,
  品牌: loadBrandsContent,
  发稿: loadReleaseContent,
  搜搜: loadSearchContent,
  API配置: loadAPIContent,
  账号: loadAccountContent,
  系统设置: preloadSettingsContent,
};

function PageLoadingFallback() {
  return (
    <main className="flex min-w-0 flex-1 items-center justify-center bg-[#fcfdff] text-sm font-medium text-gray-400">
      正在加载页面…
    </main>
  );
}

function readStoredAuthState() {
  if (typeof window === "undefined") {
    return true;
  }
  const value = window.localStorage.getItem(AUTH_STORAGE_KEY);
  if (value === null) {
    return true;
  }
  return value === "true";
}

function readStoredLoginIdentity() {
  if (typeof window === "undefined") {
    return "";
  }
  return window.localStorage.getItem(LOGIN_IDENTITY_STORAGE_KEY) || "";
}

export default function App() {
  const saveToastTimerRef = useRef<number | null>(null);
  const runMessageTimerRef = useRef<number | null>(null);
  const loginModalDragRef = useRef<{
    pointerId: number;
    offsetX: number;
    offsetY: number;
  } | null>(null);
  const [activeTab, setActiveTab] = useState("看板");
  const [localOCR, setLocalOCR] = useState(true);
  const [isDarkMode, setIsDarkMode] = useState(false);
  const [recognitionTestWindow, setRecognitionTestWindow] = useState<{
    open: boolean;
    taskId: string;
    taskName: string;
    key: number;
  }>({
    open: false,
    taskId: "",
    taskName: "",
    key: 0,
  });
  const [bootstrap, setBootstrap] = useState<BootstrapPayload>(() => {
    const cachedTodos = readTodoCache();
    if (!cachedTodos?.todos.length) {
      return FALLBACK_BOOTSTRAP;
    }
    return {
      ...FALLBACK_BOOTSTRAP,
      todos: cachedTodos.todos,
    };
  });
  const [runMessage, setRunMessage] = useState("");
  const [saveToast, setSaveToast] = useState({
    visible: false,
    message: "保存成功",
  });
  const [isAuthenticated, setIsAuthenticated] = useState(() => readStoredAuthState());
  const [loginIdentity, setLoginIdentity] = useState(() => readStoredLoginIdentity());
  const [isLoginModalOpen, setIsLoginModalOpen] = useState(false);
  const [loginModalPosition, setLoginModalPosition] = useState({ x: 0, y: 0 });
  const currentDetectionMode = (() => {
    const activeCard = bootstrap.dashboard?.taskCards?.find((card) => card.active);
    if (!activeCard) return "smart";
    if (activeCard.title === "抓取模式") return "browser";
    if (activeCard.title === "识别模式") return "recognition";
    if (activeCard.title === "接口模式") return "api";
    return "smart";
  })();
  const residentOcrWindowEnabled = Boolean(bootstrap.config?.recognition?.floating_window_resident_enabled);
  const showResidentOcrWindow = residentOcrWindowEnabled && !recognitionTestWindow.open;

  const refreshBootstrap = useCallback(async (options?: { force?: boolean }) => {
    if (options?.force) {
      invalidateBootstrapCache();
    }
    const data = await fetchBootstrap();
    const cachedTodos = readTodoCache();
    if (cachedTodos?.pending) {
      if (areTodosEqual(cachedTodos.todos, data.todos)) {
        writeTodoCache(data.todos, false);
      } else {
        data.todos = cachedTodos.todos;
      }
    }
    setBootstrap((prev) => ({
      ...data,
      todos: areTodosEqual(prev.todos, data.todos) ? prev.todos : data.todos,
      articles: areArticlesEqual(prev.articles, data.articles) ? prev.articles : data.articles,
    }));
  }, []);

  const handleTabChange = useCallback((tab: string) => {
    preloadPageByTab[tab]?.();
    if (tab === "品牌") {
      warmTasksFullCache();
    }
    warmBootstrapCache();
    setActiveTab(tab);
  }, []);

  const handleTabPreload = useCallback((tab: string) => {
    preloadPageByTab[tab]?.();
    if (tab === "品牌") {
      warmTasksFullCache();
      return;
    }
    warmBootstrapCache();
  }, []);

  useEffect(() => {
    const preload = () => {
      void loadBrandsContent();
      warmTasksFullCache();
      void loadReleaseContent();
      void loadSearchContent();
      void loadAPIContent();
      void preloadSettingsContent();
      void loadAccountContent();
    };
    const requestIdleCallback = window.requestIdleCallback;
    if (requestIdleCallback) {
      const id = requestIdleCallback(preload, { timeout: 1800 });
      return () => window.cancelIdleCallback?.(id);
    }
    const timer = window.setTimeout(preload, 400);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    let cancelled = false;
    refreshBootstrap().then(() => {
      if (cancelled) {
        return;
      }
    });
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, refreshBootstrap]);

  useEffect(() => {
    if (activeTab === "搜搜" || activeTab === "API配置" || activeTab === "发稿") {
      refreshBootstrap({ force: true });
    }
  }, [activeTab, isAuthenticated, refreshBootstrap]);

  useEffect(() => {
    const intervalMs = activeTab === "看板" || activeTab === "品牌" ? 5000 : 30000;
    const syncTimer = window.setInterval(() => {
      void refreshBootstrap();
    }, intervalMs);
    return () => window.clearInterval(syncTimer);
  }, [activeTab, isAuthenticated, refreshBootstrap]);

  useEffect(() => {
    const handleTaskDataChanged = () => {
      void refreshBootstrap({ force: true });
    };
    const handleArticleDataChanged = () => {
      void refreshBootstrap({ force: true });
    };
    const handleWindowFocus = () => {
      void refreshBootstrap();
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void refreshBootstrap({ force: true });
      }
    };

    window.addEventListener(ARTICLE_DATA_CHANGED_EVENT, handleArticleDataChanged);
    window.addEventListener(TASK_DATA_CHANGED_EVENT, handleTaskDataChanged);
    window.addEventListener("focus", handleWindowFocus);
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      window.removeEventListener(ARTICLE_DATA_CHANGED_EVENT, handleArticleDataChanged);
      window.removeEventListener(TASK_DATA_CHANGED_EVENT, handleTaskDataChanged);
      window.removeEventListener("focus", handleWindowFocus);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [isAuthenticated, refreshBootstrap]);

  const handleMonitoringToggle = async (enabled: boolean) => {
    setBootstrap((prev) => ({
      ...prev,
      monitoring: {
        ...prev.monitoring,
        enabled,
        running: enabled ? prev.monitoring.running : false,
        statusMessage: enabled ? "正在开启定时任务..." : "正在关闭定时任务...",
      },
      assistant: {
        ...prev.assistant,
        status: enabled ? "定时任务开启中" : "定时任务关闭中",
      },
    }));

    invalidateBootstrapCache();
    const result = await setMonitoringEnabled(enabled);
    const nextMessage = result.message || (result.ok ? (enabled ? "已开启定时任务" : "已关闭定时任务") : "切换失败");
    if (runMessageTimerRef.current !== null) {
      window.clearTimeout(runMessageTimerRef.current);
    }
    setRunMessage(nextMessage);
    await refreshBootstrap({ force: true });
    runMessageTimerRef.current = window.setTimeout(() => {
      setRunMessage("");
      runMessageTimerRef.current = null;
    }, 3000);
  };

  const handleTodosChange = useCallback((todos: BootstrapPayload["todos"]) => {
    setBootstrap((prev) => ({ ...prev, todos }));
  }, []);

  const handleArticlesChange = useCallback((articles: BootstrapPayload["articles"]) => {
    setBootstrap((prev) => ({ ...prev, articles }));
  }, []);

  const showRunMessage = useCallback((message: string, durationMs = 3000) => {
    if (runMessageTimerRef.current !== null) {
      window.clearTimeout(runMessageTimerRef.current);
    }
    setRunMessage(message);
    runMessageTimerRef.current = window.setTimeout(() => {
      setRunMessage("");
      runMessageTimerRef.current = null;
    }, durationMs);
  }, []);

  const showSaveSuccessToast = useCallback((message = "保存成功") => {
    if (saveToastTimerRef.current !== null) {
      window.clearTimeout(saveToastTimerRef.current);
    }
    setSaveToast({
      visible: true,
      message,
    });
    saveToastTimerRef.current = window.setTimeout(() => {
      setSaveToast((prev) => ({ ...prev, visible: false }));
      saveToastTimerRef.current = null;
    }, 1600);
  }, []);

  const handleRecognitionTestStart = useCallback((payload: { taskId: string; taskName: string }) => {
    setRecognitionTestWindow((prev) => ({
      open: true,
      taskId: payload.taskId,
      taskName: payload.taskName,
      key: prev.key + 1,
    }));
  }, []);

  const handleRecognitionTestClose = useCallback(() => {
    setRecognitionTestWindow((prev) => ({
      ...prev,
      open: false,
    }));
    void refreshBootstrap({ force: true });
  }, [refreshBootstrap]);

  const centerLoginModal = useCallback(() => {
    if (typeof window === "undefined") {
      return;
    }
    setLoginModalPosition({
      x: Math.max(24, Math.round((window.innerWidth - LOGIN_MODAL_WIDTH) / 2)),
      y: Math.max(24, Math.round((window.innerHeight - LOGIN_MODAL_HEIGHT) / 2)),
    });
  }, []);

  const handleOpenAccountSettings = useCallback(() => {
    if (!isAuthenticated) {
      centerLoginModal();
      setIsLoginModalOpen(true);
      return;
    }
    setActiveTab("账号");
  }, [centerLoginModal, isAuthenticated]);

  const handleLogout = useCallback(() => {
    setIsAuthenticated(false);
    setActiveTab("看板");
    setIsLoginModalOpen(false);
    window.localStorage.setItem(AUTH_STORAGE_KEY, "false");
  }, []);

  const handleLogin = useCallback(async (payload: LoginPayload) => {
    const nextIdentity = payload.method === "email" ? payload.email.trim() : payload.account.trim();
    setLoginIdentity(nextIdentity);
    setIsAuthenticated(true);
    setIsLoginModalOpen(false);
    setActiveTab("看板");
    window.localStorage.setItem(AUTH_STORAGE_KEY, "true");
    window.localStorage.setItem(LOGIN_IDENTITY_STORAGE_KEY, nextIdentity);
    await refreshBootstrap({ force: true });
  }, [refreshBootstrap]);

  useEffect(() => {
    return () => {
      if (saveToastTimerRef.current !== null) {
        window.clearTimeout(saveToastTimerRef.current);
      }
      if (runMessageTimerRef.current !== null) {
        window.clearTimeout(runMessageTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!isLoginModalOpen) {
      return;
    }

    const handleResize = () => {
      setLoginModalPosition((prev) => ({
        x: Math.min(Math.max(16, prev.x), Math.max(16, window.innerWidth - LOGIN_MODAL_WIDTH - 16)),
        y: Math.min(Math.max(16, prev.y), Math.max(16, window.innerHeight - LOGIN_MODAL_HEIGHT - 16)),
      }));
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsLoginModalOpen(false);
      }
    };

    window.addEventListener("resize", handleResize);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("resize", handleResize);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isLoginModalOpen]);

  const handleLoginModalPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "mouse" && event.button !== 0) {
      return;
    }
    loginModalDragRef.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - loginModalPosition.x,
      offsetY: event.clientY - loginModalPosition.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }, [loginModalPosition.x, loginModalPosition.y]);

  const handleLoginModalPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = loginModalDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) {
      return;
    }
    const maxX = Math.max(16, window.innerWidth - LOGIN_MODAL_WIDTH - 16);
    const maxY = Math.max(16, window.innerHeight - LOGIN_MODAL_HEIGHT - 16);
    setLoginModalPosition({
      x: Math.min(Math.max(16, event.clientX - drag.offsetX), maxX),
      y: Math.min(Math.max(16, event.clientY - drag.offsetY), maxY),
    });
  }, []);

  const handleLoginModalPointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (loginModalDragRef.current?.pointerId !== event.pointerId) {
      return;
    }
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    loginModalDragRef.current = null;
  }, []);

  useEffect(() => {
    const handleSaveToastEvent = (event: Event) => {
      const customEvent = event as CustomEvent<{ message?: string }>;
      showSaveSuccessToast(customEvent.detail?.message || "保存成功");
    };
    window.addEventListener(SAVE_SUCCESS_TOAST_EVENT, handleSaveToastEvent as EventListener);
    return () => {
      window.removeEventListener(SAVE_SUCCESS_TOAST_EVENT, handleSaveToastEvent as EventListener);
    };
  }, [showSaveSuccessToast]);

  // Dynamic CSS block to globally force a refined dark mode
  const darkThemeStyles = `
    .dark-app {
      background-color: #000000 !important;
      color: #fafafa !important;
    }
    .dark-app .bg-\\[\\#f7f8fa\\] {
      background-color: #000000 !important;
    }
    .dark-app .bg-white {
      background-color: #0a0a0a !important;
      border-color: #262626 !important;
      color: #fafafa !important;
    }
    .dark-app .text-gray-900, .dark-app .text-gray-800 {
      color: #fafafa !important;
    }
    .dark-app .text-gray-600 {
      color: #a3a3a3 !important;
    }
    .dark-app .text-gray-500, .dark-app .text-gray-400 {
      color: #737373 !important;
    }
    .dark-app .border-gray-100, .dark-app .border-gray-200, .dark-app .border-gray-200\\/50, .dark-app .border-gray-200\\/80 {
      border-color: #262626 !important;
    }
    .dark-app .bg-gray-50, .dark-app .bg-gray-50\\/80, .dark-app .bg-gray-100 {
      background-color: #1a1a1a !important;
    }
    .dark-app .hover\\:bg-gray-50:hover {
      background-color: #1a1a1a !important;
    }
    .dark-app .hover\\:text-gray-900:hover {
      color: #ffffff !important;
    }
    .dark-app .shadow-sm {
      box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.5) !important;
    }
    .dark-app input, .dark-app select, .dark-app textarea {
      background-color: transparent !important;
      color: #fafafa !important;
      border-color: #333333 !important;
    }
    /* Keep brand accents visible in dark mode */
    .dark-app .text-blue-600 {
      color: var(--brand-cyan) !important;
    }
    .dark-app .bg-blue-600 {
      background-color: var(--brand-navy) !important;
    }
    .dark-app .bg-blue-50, .dark-app .hover\\:bg-blue-50:hover {
      background-color: rgba(var(--brand-cyan-rgb), 0.12) !important;
      border-color: rgba(var(--brand-cyan-rgb), 0.2) !important;
    }
    .dark-app .search-bottom-gradient {
      background: linear-gradient(to top, #000000 0%, rgba(0,0,0,0.95) 30%, transparent 100%) !important;
    }
  `;

  return (
    <>
      {isDarkMode && <style>{darkThemeStyles}</style>}
      <SaveSuccessToast visible={saveToast.visible} message={saveToast.message} />
      <div className={`app-canvas flex h-screen w-full bg-[#fcfdff] text-gray-900 font-sans overflow-hidden selection:bg-blue-50 selection:text-blue-900 ${isDarkMode ? 'dark-app' : ''}`}>
        <Sidebar
          activeTab={activeTab}
          onTabChange={handleTabChange}
          onTabPreload={handleTabPreload}
          onAccountClick={handleOpenAccountSettings}
          isDarkMode={isDarkMode}
          setIsDarkMode={setIsDarkMode}
          branding={bootstrap.branding}
          isAuthenticated={isAuthenticated}
          sidebar={{
            ...bootstrap.sidebar,
            userName: isAuthenticated ? (bootstrap.sidebar?.userName || "shu") : "访客模式",
            role: isAuthenticated ? bootstrap.sidebar?.role : "本地活动可查看，云端权限已收回",
            avatar: isAuthenticated ? bootstrap.sidebar?.avatar : "",
          }}
        />
        <Suspense fallback={<PageLoadingFallback />}>
          {activeTab === "看板" ? (
            <CenterContent
              localOCR={localOCR}
              dashboard={bootstrap.dashboard}
              runMessage={runMessage}
              activeRegions={bootstrap.regionTags}
              onDataChanged={refreshBootstrap}
              suppressOcrWindow={recognitionTestWindow.open || showResidentOcrWindow}
              onRunMessage={showRunMessage}
            />
          ) : activeTab === "品牌" ? (
            <BrandsContent
              currentDetectionMode={currentDetectionMode}
              onSaveSuccess={showSaveSuccessToast}
              onRecognitionTestStart={handleRecognitionTestStart}
            />
          ) : activeTab === "发稿" ? (
            <ReleaseContent />
          ) : activeTab === "搜搜" ? (
            <SearchContent
              availableModels={bootstrap.availableModels}
              bootstrap={bootstrap}
              onDataChanged={refreshBootstrap}
            />
          ) : activeTab === "API配置" ? (
            <APIContent onSaved={refreshBootstrap} onSaveSuccess={showSaveSuccessToast} />
          ) : activeTab === "账号" ? (
            <AccountContent
              isAuthenticated={isAuthenticated}
              onSaveSuccess={showSaveSuccessToast}
              onProfileSaved={refreshBootstrap}
              onLogout={handleLogout}
            />
          ) : activeTab === "系统设置" ? (
            <SettingsContent
              localOCR={localOCR}
              setLocalOCR={setLocalOCR}
              onSaveSuccess={(message) => {
                showSaveSuccessToast(message);
                void refreshBootstrap({ force: true });
              }}
              onLogout={handleLogout}
            />
          ) : (
            <CenterContent
              localOCR={localOCR}
              dashboard={bootstrap.dashboard}
              runMessage={runMessage}
              onDataChanged={refreshBootstrap}
              suppressOcrWindow={recognitionTestWindow.open || showResidentOcrWindow}
              onRunMessage={showRunMessage}
            />
          )}
        </Suspense>
        <RightSidebar
          monitoring={bootstrap.monitoring}
          todos={bootstrap.todos}
          articles={bootstrap.articles}
          stats={bootstrap.stats}
          onMonitoringToggle={handleMonitoringToggle}
          onTodosChange={handleTodosChange}
          onArticlesChange={handleArticlesChange}
        />
      </div>
      {recognitionTestWindow.open && (
        <Suspense fallback={null}>
          <OcrFloatingWindow
            key={recognitionTestWindow.key}
            onClose={handleRecognitionTestClose}
            localOCR={localOCR}
            stopOnClose
          />
        </Suspense>
      )}
      {showResidentOcrWindow && (
        <Suspense fallback={null}>
          <OcrFloatingWindow
            onClose={() => undefined}
            localOCR={localOCR}
            resident
          />
        </Suspense>
      )}
      {isLoginModalOpen && !isAuthenticated && (
        <div
          className="fixed inset-0 z-[120] bg-transparent"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setIsLoginModalOpen(false);
            }
          }}
        >
          <div
            className="absolute"
            style={{
              left: `${loginModalPosition.x}px`,
              top: `${loginModalPosition.y}px`,
              width: `${LOGIN_MODAL_WIDTH}px`,
            }}
          >
            <div className="overflow-hidden rounded-[24px] border border-gray-200 bg-white shadow-[0_28px_90px_-42px_rgba(15,23,42,0.32)]">
              <div
                className="flex items-center justify-between border-b border-gray-100 px-4 py-3"
                onPointerDown={handleLoginModalPointerDown}
                onPointerMove={handleLoginModalPointerMove}
                onPointerUp={handleLoginModalPointerUp}
              >
                <div className="text-[13px] font-semibold tracking-wide text-gray-900">账号登录</div>
                <button
                  type="button"
                  onClick={() => setIsLoginModalOpen(false)}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-full text-gray-400 transition-colors hover:bg-gray-50 hover:text-gray-700"
                  aria-label="关闭登录弹窗"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="px-4 pb-4 pt-3">
                <LoginScreen
                  variant="panel"
                  defaultIdentity={loginIdentity}
                  onLogin={handleLogin}
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
