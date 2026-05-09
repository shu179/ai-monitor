import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { Edit2, KeyRound, Loader2, Plus, Power, PowerOff, Save, Trash2, X } from "lucide-react";
import { ConfirmModal } from "../ConfirmModal";
import { DatePickerField } from "../ui/date-picker-field";
import {
  createCloudAdminUser,
  deleteCloudAdminUser,
  CLOUD_ADMIN_USERS_CHANGED_EVENT,
  updateCloudAdminUser,
  type CloudAdminTaskSnapshot,
  type CloudStatusSnapshot,
  type CloudUserSnapshot,
} from "../../lib/backend";
import {
  ensureCloudAdminTasks,
  ensureCloudAdminUsers,
  readCloudAdminCache,
  subscribeCloudAdminCache,
  updateCloudAdminUsersCache,
} from "../../lib/cloudAdminCache";
import { AccountSection, Field } from "./AccountPrimitives";
import { ViewerTaskVisibilityEditor } from "./ViewerTaskVisibilityEditor";
import {
  areNumberArraysEqual,
  CLOUD_USER_ROLE_LABELS,
  formatProfileDate,
  getViewerVisibleTaskIds,
  normalizeDateText,
  roleToProfileLabel,
} from "./cloudAccountUtils";
import type { CloudUserForm, CloudUserManageForm, CloudUsersNotice } from "./cloudSubAccountTypes";

const CLOUD_USER_ROLE_DESCRIPTIONS: Record<"operator" | "viewer", string> = {
  operator: "品牌任务承接",
  viewer: "品牌数据浏览",
};

const CLOUD_USER_ROLE_EMPTY_TEXT: Record<"operator" | "viewer", string> = {
  operator: "暂无运营账号。创建后可在品牌编辑页分配品牌任务。",
  viewer: "暂无浏览账号。创建后可下放只读展示数据。",
};

function mergeViewerScopeIntoCloudUser(
  user: CloudUserSnapshot,
  scope?: { viewAllTasks: boolean; visibleTaskIds: number[] },
) {
  if (user.role !== "viewer" || !scope) {
    return user;
  }
  return {
    ...user,
    view_all_tasks: scope.viewAllTasks,
    visible_task_ids: scope.viewAllTasks ? [] : scope.visibleTaskIds,
  };
}

export function CloudSubAccountsSection({
  isCloudAdmin,
  onCloudStatusChange,
}: {
  isCloudAdmin: boolean;
  onCloudStatusChange?: (status: CloudStatusSnapshot | null) => void;
}) {
  const [cloudUsers, setCloudUsers] = useState<CloudUserSnapshot[]>([]);
  const [cloudAdminTasks, setCloudAdminTasks] = useState<CloudAdminTaskSnapshot[]>([]);
  const [cloudAdminTasksLoaded, setCloudAdminTasksLoaded] = useState(false);
  const [cloudUsersLoading, setCloudUsersLoading] = useState(false);
  const [cloudUsersNotice, setCloudUsersNotice] = useState<CloudUsersNotice>(null);
  const [cloudUserActionId, setCloudUserActionId] = useState<string>("");
  const [cloudDeleteConfirmUser, setCloudDeleteConfirmUser] = useState<CloudUserSnapshot | null>(null);
  const [managedCloudUser, setManagedCloudUser] = useState<CloudUserManageForm | null>(null);
  const [cloudUserModalRole, setCloudUserModalRole] = useState<"operator" | "viewer" | null>(null);
  const [visibleTaskSearch, setVisibleTaskSearch] = useState("");
  const [newCloudUser, setNewCloudUser] = useState<CloudUserForm>({
    username: "",
    password: "",
    role: "operator",
    birthday: "",
    hireDate: "",
    viewAllTasks: false,
    visibleTaskIds: [],
  });

  const applyCloudSubAccountsCache = useCallback((cache = readCloudAdminCache()) => {
    if (!cache.usersLoaded) {
      return false;
    }
    setCloudUsers(cache.users);
    setCloudAdminTasks(cache.tasks);
    setCloudAdminTasksLoaded(cache.tasksLoaded);
    onCloudStatusChange?.(cache.status);
    return true;
  }, [onCloudStatusChange]);

  const updateCachedCloudUsers = useCallback((updater: (users: CloudUserSnapshot[]) => CloudUserSnapshot[]) => {
    updateCloudAdminUsersCache(updater);
  }, []);

  const refreshCloudAdminTasks = useCallback(async (options?: { force?: boolean }) => {
    const snapshot = await ensureCloudAdminTasks({ force: Boolean(options?.force) });
    applyCloudSubAccountsCache(snapshot);
    return snapshot.tasksLoaded;
  }, [applyCloudSubAccountsCache]);

  const refreshCloudUsers = useCallback(async (options?: { force?: boolean; includeTasks?: boolean; quiet?: boolean }) => {
    const force = Boolean(options?.force);
    const includeTasks = Boolean(options?.includeTasks);
    const quiet = Boolean(options?.quiet);
    if (!quiet) {
      setCloudUsersLoading(true);
    }
    try {
      let snapshot = await ensureCloudAdminUsers({ force });
      if (includeTasks) {
        snapshot = await ensureCloudAdminTasks({ force });
      }
      const applied = applyCloudSubAccountsCache(snapshot);
      if (!applied && isCloudAdmin) {
        setCloudUsersNotice({ tone: "error", message: "云端账号获取失败" });
      }
    } finally {
      if (!quiet) {
        setCloudUsersLoading(false);
      }
    }
  }, [applyCloudSubAccountsCache, isCloudAdmin]);

  useEffect(() => {
    if (!isCloudAdmin) {
      return;
    }
    const unsubscribe = subscribeCloudAdminCache((snapshot) => {
      applyCloudSubAccountsCache(snapshot);
      setManagedCloudUser((current) => (
        current && snapshot.users.some((user) => String(user.id || "") === current.userId)
          ? current
          : null
      ));
    });
    const snapshot = readCloudAdminCache();
    const hasCache = applyCloudSubAccountsCache(snapshot);
    if (!snapshot.usersLoaded) {
      void refreshCloudUsers({ quiet: hasCache });
    }
    return unsubscribe;
  }, [applyCloudSubAccountsCache, isCloudAdmin, refreshCloudUsers]);

  const manageableCloudUsers = useMemo(
    () => cloudUsers.filter((user) => user.role === "operator" || user.role === "viewer"),
    [cloudUsers],
  );
  const cloudUsersByRole = useMemo(() => {
    return {
      operator: manageableCloudUsers.filter((user) => user.role === "operator"),
      viewer: manageableCloudUsers.filter((user) => user.role === "viewer"),
    };
  }, [manageableCloudUsers]);
  const modalCloudUsers = cloudUserModalRole ? cloudUsersByRole[cloudUserModalRole] : [];
  const filteredVisibleTasks = useMemo(() => {
    const query = visibleTaskSearch.trim().toLowerCase();
    if (!query) {
      return cloudAdminTasks;
    }
    return cloudAdminTasks.filter((task) => {
      const haystack = `${task.name} ${task.brand} ${task.task_key}`.toLowerCase();
      return haystack.includes(query);
    });
  }, [cloudAdminTasks, visibleTaskSearch]);
  const managedCloudUserId = managedCloudUser?.userId || "";

  const openCloudUserModal = useCallback((role: "operator" | "viewer") => {
    setCloudUserModalRole(role);
    setManagedCloudUser(null);
    setCloudUsersNotice(null);
    setVisibleTaskSearch("");
    setNewCloudUser((prev) => ({
      ...prev,
      role,
      viewAllTasks: false,
      visibleTaskIds: [],
    }));
    if (role === "viewer") {
      void refreshCloudAdminTasks();
    }
  }, [refreshCloudAdminTasks]);

  const closeCloudUserModal = useCallback(() => {
    setCloudUserModalRole(null);
    setManagedCloudUser(null);
    setCloudUsersNotice(null);
  }, []);

  const toggleNewCloudVisibleTask = useCallback((taskId: number) => {
    setNewCloudUser((current) => {
      if (current.viewAllTasks) {
        return {
          ...current,
          viewAllTasks: false,
          visibleTaskIds: cloudAdminTasks
            .map((task) => task.id)
            .filter((id) => id !== taskId),
          viewerScopeDirty: true,
        };
      }
      const exists = current.visibleTaskIds.includes(taskId);
      return {
        ...current,
        visibleTaskIds: exists
          ? current.visibleTaskIds.filter((item) => item !== taskId)
          : [...current.visibleTaskIds, taskId],
        viewerScopeDirty: true,
      };
    });
  }, [cloudAdminTasks]);

  const toggleManagedCloudVisibleTask = useCallback((taskId: number) => {
    setManagedCloudUser((current) => {
      if (!current) {
        return current;
      }
      if (current.viewAllTasks) {
        return {
          ...current,
          viewAllTasks: false,
          visibleTaskIds: cloudAdminTasks
            .map((task) => task.id)
            .filter((id) => id !== taskId),
        };
      }
      const exists = current.visibleTaskIds.includes(taskId);
      return {
        ...current,
        visibleTaskIds: exists
          ? current.visibleTaskIds.filter((item) => item !== taskId)
          : [...current.visibleTaskIds, taskId],
      };
    });
  }, [cloudAdminTasks]);

  const setNewCloudVisibleAll = useCallback((checked: boolean) => {
    setNewCloudUser((current) => ({
      ...current,
      viewAllTasks: checked,
      visibleTaskIds: [],
    }));
  }, []);

  const setManagedCloudVisibleAll = useCallback((checked: boolean) => {
    setManagedCloudUser((current) => (
      current
        ? {
          ...current,
          viewAllTasks: checked,
          visibleTaskIds: [],
          viewerScopeDirty: true,
        }
        : current
    ));
  }, []);

  useEffect(() => {
    if (!managedCloudUserId || cloudUserModalRole !== "viewer") {
      return;
    }
    const user = cloudUsers.find((item) => String(item.id || "") === managedCloudUserId);
    if (!user) {
      return;
    }
    const nextVisibleTaskIds = getViewerVisibleTaskIds(user, cloudAdminTasks);
    const nextViewAllTasks = Boolean(user.view_all_tasks);
    setManagedCloudUser((current) => {
      if (current?.userId !== managedCloudUserId) {
        return current;
      }
      if (current.viewerScopeDirty) {
        return current;
      }
      // Keep the user's in-progress mode choice intact. This effect only fills
      // task ids after the admin task list refreshes; otherwise switching a
      // viewer from custom scope to all-visible can be overwritten by the old
      // server snapshot before Save is clicked.
      if (current.viewAllTasks !== nextViewAllTasks) {
        return current;
      }
      return !areNumberArraysEqual(current.visibleTaskIds, nextVisibleTaskIds)
        ? { ...current, visibleTaskIds: nextVisibleTaskIds }
        : current;
    });
  }, [cloudAdminTasks, cloudUserModalRole, cloudUsers, managedCloudUserId]);

  const handleCreateCloudUser = useCallback(async () => {
    if (!isCloudAdmin) {
      return;
    }
    const username = newCloudUser.username.trim();
    const password = newCloudUser.password;
    if (!username || !password) {
      setCloudUsersNotice({ tone: "error", message: "请填写账号名和密码" });
      return;
    }
    const role = cloudUserModalRole || newCloudUser.role;
    if (role === "viewer" && !cloudAdminTasksLoaded && !newCloudUser.viewAllTasks) {
      setCloudUsersNotice({ tone: "error", message: "品牌列表还在读取，请稍后再创建浏览账号" });
      return;
    }
    setCloudUserActionId("create");
    try {
      const result = await createCloudAdminUser({
        username,
        password,
        role,
        displayName: username,
        birthday: newCloudUser.birthday,
        hireDate: newCloudUser.hireDate,
        viewAllTasks: role === "viewer" ? newCloudUser.viewAllTasks : false,
        visibleTaskIds: role === "viewer" ? newCloudUser.visibleTaskIds : [],
      });
      if (!result.ok) {
        setCloudUsersNotice({ tone: "error", message: result.message || "云端账号创建失败" });
        return;
      }
      setCloudUsersNotice({ tone: "success", message: result.message || "云端账号已创建" });
      setNewCloudUser((prev) => ({
        username: "",
        password: "",
        role: cloudUserModalRole || role || prev.role,
        birthday: "",
        hireDate: "",
        viewAllTasks: false,
        visibleTaskIds: [],
      }));
      if (result.user) {
        const createdUser = mergeViewerScopeIntoCloudUser(
          result.user as CloudUserSnapshot,
          role === "viewer"
            ? {
              viewAllTasks: newCloudUser.viewAllTasks,
              visibleTaskIds: newCloudUser.visibleTaskIds,
            }
            : undefined,
        );
        updateCachedCloudUsers((users) => [...users.filter((item) => String(item.id || "") !== String(createdUser.id || "")), createdUser]);
      } else {
        await refreshCloudUsers({ force: true, quiet: true });
      }
      window.dispatchEvent(new CustomEvent(CLOUD_ADMIN_USERS_CHANGED_EVENT));
    } finally {
      setCloudUserActionId("");
    }
  }, [cloudAdminTasksLoaded, cloudUserModalRole, isCloudAdmin, newCloudUser, refreshCloudUsers, updateCachedCloudUsers]);

  const handleToggleCloudUserEnabled = useCallback(async (user: CloudUserSnapshot) => {
    if (!isCloudAdmin || !user.id) {
      return;
    }
    const userId = String(user.id);
    const nextEnabled = user.enabled === false;
    setCloudUserActionId(userId);
    try {
      const result = await updateCloudAdminUser({
        userId,
        enabled: nextEnabled,
      });
      if (!result.ok) {
        setCloudUsersNotice({ tone: "error", message: result.message || "云端账号保存失败" });
        return;
      }
      setCloudUsersNotice({ tone: "success", message: nextEnabled ? "账号已启用" : "账号已停用" });
      if (result.user) {
        updateCachedCloudUsers((users) => users.map((item) => (
          String(item.id || "") === String(result.user?.id || "") ? result.user as CloudUserSnapshot : item
        )));
      } else {
        updateCachedCloudUsers((users) => users.map((item) => (
          String(item.id || "") === userId ? { ...item, enabled: nextEnabled } : item
        )));
      }
      window.dispatchEvent(new CustomEvent(CLOUD_ADMIN_USERS_CHANGED_EVENT));
    } finally {
      setCloudUserActionId("");
    }
  }, [isCloudAdmin, updateCachedCloudUsers]);

  const handleOpenCloudUserManager = useCallback((user: CloudUserSnapshot) => {
    const userId = String(user.id || "");
    if (!userId) {
      return;
    }
    setManagedCloudUser((current) => (
      current?.userId === userId
        ? null
        : {
          userId,
          username: String(user.display_name || user.username || ""),
          displayName: String(user.display_name || user.username || ""),
          password: "",
          birthday: normalizeDateText(user.birthday),
          hireDate: normalizeDateText(user.hire_date),
          viewAllTasks: Boolean(user.view_all_tasks),
          visibleTaskIds: getViewerVisibleTaskIds(user, cloudAdminTasks),
          viewerScopeDirty: false,
        }
    ));
    setCloudUsersNotice(null);
  }, [cloudAdminTasks]);

  const handleSaveManagedCloudUser = useCallback(async (user: CloudUserSnapshot) => {
    if (!isCloudAdmin || !user.id || !managedCloudUser) {
      return;
    }
    const userId = String(user.id);
    if (managedCloudUser.userId !== userId) {
      return;
    }
    const username = managedCloudUser.username.trim();
    const displayName = managedCloudUser.displayName.trim();
    const password = managedCloudUser.password;
    if (!username || !displayName) {
      setCloudUsersNotice({ tone: "error", message: "请填写账号名" });
      return;
    }
    if (password && password.length < 8) {
      setCloudUsersNotice({ tone: "error", message: "新密码至少需要 8 位" });
      return;
    }
    const usernameChanged = username !== String(user.username || "");
    const displayNameChanged = displayName !== String(user.display_name || user.username || "");
    const birthdayChanged = managedCloudUser.birthday !== normalizeDateText(user.birthday);
    const hireDateChanged = managedCloudUser.hireDate !== normalizeDateText(user.hire_date);
    const shouldSyncViewerScope = user.role === "viewer" && (managedCloudUser.viewAllTasks || cloudAdminTasksLoaded);
    const viewerScopeChanged = shouldSyncViewerScope && (
      managedCloudUser.viewAllTasks !== Boolean(user.view_all_tasks)
      || areNumberArraysEqual(managedCloudUser.visibleTaskIds, getViewerVisibleTaskIds(user, cloudAdminTasks)) === false
    );
    if (!usernameChanged && !displayNameChanged && !password && !birthdayChanged && !hireDateChanged && !viewerScopeChanged) {
      setCloudUsersNotice({ tone: "success", message: "没有检测到改动" });
      return;
    }
    setCloudUserActionId(userId);
    try {
      const result = await updateCloudAdminUser({
        userId,
        username,
        displayName,
        password: password || undefined,
        birthday: managedCloudUser.birthday,
        hireDate: managedCloudUser.hireDate,
        viewAllTasks: shouldSyncViewerScope ? managedCloudUser.viewAllTasks : undefined,
        visibleTaskIds: shouldSyncViewerScope ? managedCloudUser.visibleTaskIds : undefined,
      });
      if (!result.ok) {
        setCloudUsersNotice({ tone: "error", message: result.message || "云端账号保存失败" });
        return;
      }
      setCloudUsersNotice({
        tone: "success",
        message: password ? "账号已更新，新密码已生效" : "账号资料已更新",
      });
      setManagedCloudUser(null);
      if (result.user) {
        const savedUser = mergeViewerScopeIntoCloudUser(
          result.user as CloudUserSnapshot,
          shouldSyncViewerScope
            ? {
              viewAllTasks: managedCloudUser.viewAllTasks,
              visibleTaskIds: managedCloudUser.visibleTaskIds,
            }
            : undefined,
        );
        updateCachedCloudUsers((users) => users.map((item) => (
          String(item.id || "") === String(savedUser.id || "") ? savedUser : item
        )));
      } else {
        updateCachedCloudUsers((users) => users.map((item) => (
          String(item.id || "") === userId
            ? {
              ...item,
              username,
              display_name: displayName,
              birthday: managedCloudUser.birthday || null,
              hire_date: managedCloudUser.hireDate || null,
              view_all_tasks: shouldSyncViewerScope ? managedCloudUser.viewAllTasks : item.view_all_tasks,
              visible_task_ids: shouldSyncViewerScope ? managedCloudUser.visibleTaskIds : item.visible_task_ids,
            }
            : item
        )));
      }
      window.dispatchEvent(new CustomEvent(CLOUD_ADMIN_USERS_CHANGED_EVENT));
    } finally {
      setCloudUserActionId("");
    }
  }, [cloudAdminTasks, cloudAdminTasksLoaded, isCloudAdmin, managedCloudUser, updateCachedCloudUsers]);

  const handleDeleteCloudUser = useCallback(async (user: CloudUserSnapshot) => {
    if (!isCloudAdmin || !user.id) {
      return;
    }
    setCloudUserActionId(String(user.id));
    try {
      const result = await deleteCloudAdminUser({ userId: user.id });
      if (!result.ok) {
        setCloudUsersNotice({ tone: "error", message: result.message || "云端账号删除失败" });
        return;
      }
      setCloudUsersNotice({ tone: "success", message: result.message || "云端账号已删除" });
      setCloudDeleteConfirmUser(null);
      setManagedCloudUser((current) => (current?.userId === String(user.id) ? null : current));
      updateCachedCloudUsers((users) => users.filter((item) => String(item.id || "") !== String(user.id)));
      window.dispatchEvent(new CustomEvent(CLOUD_ADMIN_USERS_CHANGED_EVENT));
    } finally {
      setCloudUserActionId("");
    }
  }, [isCloudAdmin, updateCachedCloudUsers]);

  if (!isCloudAdmin) {
    return null;
  }

  return (
    <>
      <AccountSection
        title="下属账号"
        action={(
          <button
            type="button"
            onClick={() => { void refreshCloudUsers({ force: true }); }}
            disabled={cloudUsersLoading}
            className="inline-flex items-center gap-1.5 text-[12px] font-bold text-gray-400 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Loader2 className={`h-3.5 w-3.5 ${cloudUsersLoading ? "animate-spin" : ""}`} />
            刷新
          </button>
        )}
      >
        {!cloudUserModalRole && cloudUsersNotice ? (
          <div
            className={`mb-5 border-l-2 px-3 py-2.5 text-[12px] font-medium ${
              cloudUsersNotice.tone === "success"
                ? "border-emerald-300 bg-emerald-50/55 text-emerald-700"
                : "border-rose-300 bg-rose-50/60 text-rose-700"
            }`}
          >
            {cloudUsersNotice.message}
          </div>
        ) : null}

        <div className="border-y border-gray-200/80 py-5">
          <div className="pb-4">
            <div className="text-[11px] font-bold tracking-wide text-gray-400">下属账号总数</div>
            <div className="mt-2 text-[22px] font-black leading-none tracking-tight text-gray-900 tabular-nums">
              {manageableCloudUsers.length}
            </div>
          </div>

          <div className="divide-y divide-gray-100 border-t border-gray-200/80">
            <CloudUserRoleEntry
              role="operator"
              count={cloudUsersByRole.operator.length}
              onOpen={() => openCloudUserModal("operator")}
            />
            <CloudUserRoleEntry
              role="viewer"
              count={cloudUsersByRole.viewer.length}
              onOpen={() => openCloudUserModal("viewer")}
            />
          </div>
        </div>
      </AccountSection>

      {cloudUserModalRole ? (
        <CloudSubAccountModal
          cloudUserModalRole={cloudUserModalRole}
          cloudUsersLoading={cloudUsersLoading}
          cloudUsersNotice={cloudUsersNotice}
          modalCloudUsers={modalCloudUsers}
          cloudUserActionId={cloudUserActionId}
          managedCloudUser={managedCloudUser}
          newCloudUser={newCloudUser}
          cloudAdminTasks={cloudAdminTasks}
          filteredVisibleTasks={filteredVisibleTasks}
          cloudAdminTasksLoaded={cloudAdminTasksLoaded}
          visibleTaskSearch={visibleTaskSearch}
          onClose={closeCloudUserModal}
          onRefresh={() => refreshCloudUsers({ force: true, includeTasks: cloudUserModalRole === "viewer" })}
          onNoticeClose={() => setCloudUsersNotice(null)}
          onCreate={handleCreateCloudUser}
          onNewCloudUserChange={setNewCloudUser}
          onVisibleTaskSearchChange={setVisibleTaskSearch}
          onSetNewCloudVisibleAll={setNewCloudVisibleAll}
          onToggleNewCloudVisibleTask={toggleNewCloudVisibleTask}
          onToggleCloudUserEnabled={handleToggleCloudUserEnabled}
          onOpenCloudUserManager={handleOpenCloudUserManager}
          onDeleteCloudUser={setCloudDeleteConfirmUser}
          onManagedCloudUserChange={setManagedCloudUser}
          onSetManagedCloudVisibleAll={setManagedCloudVisibleAll}
          onToggleManagedCloudVisibleTask={toggleManagedCloudVisibleTask}
          onSaveManagedCloudUser={handleSaveManagedCloudUser}
        />
      ) : null}

      <ConfirmModal
        isOpen={!!cloudDeleteConfirmUser}
        onClose={() => setCloudDeleteConfirmUser(null)}
        onConfirm={() => {
          if (cloudDeleteConfirmUser) {
            void handleDeleteCloudUser(cloudDeleteConfirmUser);
          }
        }}
        title="确认删除普通账号？"
        message={`删除后「${cloudDeleteConfirmUser?.display_name || cloudDeleteConfirmUser?.username || "该账号"}」会立即退出登录并解除品牌归属，历史运行数据会保留在对应品牌任务下。`}
        confirmText="删除账号"
        type="danger"
      />
    </>
  );
}

function CloudSubAccountModal({
  cloudUserModalRole,
  cloudUsersLoading,
  cloudUsersNotice,
  modalCloudUsers,
  cloudUserActionId,
  managedCloudUser,
  newCloudUser,
  cloudAdminTasks,
  filteredVisibleTasks,
  cloudAdminTasksLoaded,
  visibleTaskSearch,
  onClose,
  onRefresh,
  onNoticeClose,
  onCreate,
  onNewCloudUserChange,
  onVisibleTaskSearchChange,
  onSetNewCloudVisibleAll,
  onToggleNewCloudVisibleTask,
  onToggleCloudUserEnabled,
  onOpenCloudUserManager,
  onDeleteCloudUser,
  onManagedCloudUserChange,
  onSetManagedCloudVisibleAll,
  onToggleManagedCloudVisibleTask,
  onSaveManagedCloudUser,
}: {
  cloudUserModalRole: "operator" | "viewer";
  cloudUsersLoading: boolean;
  cloudUsersNotice: CloudUsersNotice;
  modalCloudUsers: CloudUserSnapshot[];
  cloudUserActionId: string;
  managedCloudUser: CloudUserManageForm | null;
  newCloudUser: CloudUserForm;
  cloudAdminTasks: CloudAdminTaskSnapshot[];
  filteredVisibleTasks: CloudAdminTaskSnapshot[];
  cloudAdminTasksLoaded: boolean;
  visibleTaskSearch: string;
  onClose: () => void;
  onRefresh: () => void | Promise<void>;
  onNoticeClose: () => void;
  onCreate: () => void | Promise<void>;
  onNewCloudUserChange: Dispatch<SetStateAction<CloudUserForm>>;
  onVisibleTaskSearchChange: (value: string) => void;
  onSetNewCloudVisibleAll: (checked: boolean) => void;
  onToggleNewCloudVisibleTask: (taskId: number) => void;
  onToggleCloudUserEnabled: (user: CloudUserSnapshot) => void | Promise<void>;
  onOpenCloudUserManager: (user: CloudUserSnapshot) => void;
  onDeleteCloudUser: (user: CloudUserSnapshot) => void;
  onManagedCloudUserChange: Dispatch<SetStateAction<CloudUserManageForm | null>>;
  onSetManagedCloudVisibleAll: (checked: boolean) => void;
  onToggleManagedCloudVisibleTask: (taskId: number) => void;
  onSaveManagedCloudUser: (user: CloudUserSnapshot) => void | Promise<void>;
}) {
  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center overflow-hidden bg-black/18 px-4 py-5 backdrop-blur-[2px] transition-opacity animate-in fade-in duration-200 sm:px-6 sm:py-7">
      <button
        type="button"
        aria-label={`关闭${CLOUD_USER_ROLE_LABELS[cloudUserModalRole]}管理`}
        className="absolute inset-0"
        onClick={onClose}
      />
      <div
        className="relative flex h-full w-full max-w-[780px] min-w-0 flex-col overflow-hidden rounded-2xl border border-gray-200/80 bg-white shadow-[0_20px_60px_-18px_rgba(15,23,42,0.16)] animate-in slide-in-from-bottom-4 duration-300"
        style={{
          maxHeight: "min(720px, calc(100vh - 32px))",
        }}
      >
        <div className="z-10 flex shrink-0 flex-col gap-3 border-b border-gray-100 bg-white px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:px-6">
          <div className="flex min-w-0 flex-col gap-1">
            <div className="flex min-w-0 items-center gap-2">
              <div className="h-4 w-2 shrink-0 rounded-sm bg-[var(--brand-navy)]" />
              <h2 className="text-[18px] font-black leading-none tracking-tight text-gray-900">
                {CLOUD_USER_ROLE_LABELS[cloudUserModalRole]}管理
              </h2>
              <span className="mt-0.5 min-w-0 truncate text-[12px] font-bold tracking-widest text-gray-400 uppercase">
                {modalCloudUsers.length} 个账号
              </span>
            </div>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 sm:justify-end">
            <button
              type="button"
              onClick={() => { void onRefresh(); }}
              disabled={cloudUsersLoading}
              className="inline-flex h-8 items-center gap-1.5 px-0 text-[12px] font-bold text-gray-500 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:text-gray-300"
            >
              <Loader2 className={`h-3.5 w-3.5 ${cloudUsersLoading ? "animate-spin" : ""}`} />
              刷新
            </button>
            <span className="hidden text-gray-200 sm:inline">/</span>
            <button
              type="button"
              onClick={onClose}
              className="inline-flex h-8 items-center gap-1.5 px-0 text-[12px] font-bold text-gray-500 transition-colors hover:text-gray-900"
            >
              <X className="h-4 w-4" />
              关闭
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto bg-white px-5 py-5 scrollbar-thin scrollbar-thumb-gray-200 scrollbar-track-transparent sm:px-6 md:py-6">
          <div className="mx-auto flex w-full max-w-[680px] min-w-0 flex-col">
          {cloudUsersNotice ? (
            <div
              className={`mb-6 flex min-w-0 items-start justify-between gap-3 border-l-2 px-3 py-2.5 animate-in fade-in slide-in-from-top-2 duration-200 ${
                cloudUsersNotice.tone === "success"
                  ? "border-emerald-300 bg-emerald-50/50"
                  : "border-rose-300 bg-rose-50/55"
              }`}
            >
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className={`text-[10px] font-bold tracking-[0.18em] uppercase ${
                  cloudUsersNotice.tone === "success" ? "text-emerald-600" : "text-rose-600"
                }`}>
                  {cloudUsersNotice.tone === "success" ? "操作成功" : "操作失败"}
                </span>
                <span className="text-[12px] font-medium text-gray-700">
                  {cloudUsersNotice.message}
                </span>
              </div>
              <button
                type="button"
                onClick={onNoticeClose}
                className="shrink-0 rounded-full p-1 text-gray-400 transition-colors hover:bg-white/70 hover:text-gray-700"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          ) : null}

          <section className="flex flex-col gap-4 border-b border-gray-100 pb-6">
            <AccountModalSectionTitle title={`新建${CLOUD_USER_ROLE_LABELS[cloudUserModalRole]}`} />
            <div className="flex flex-col gap-5">
              <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-end sm:justify-between sm:gap-4">
                <div className="min-w-0 text-[12px] font-medium leading-5 text-gray-400">
                  {CLOUD_USER_ROLE_DESCRIPTIONS[cloudUserModalRole]}
                </div>
                <button
                  type="button"
                  onClick={() => { void onCreate(); }}
                  disabled={cloudUserActionId === "create" || (cloudUserModalRole === "viewer" && !cloudAdminTasksLoaded && !newCloudUser.viewAllTasks)}
                  className="inline-flex h-8 shrink-0 items-center gap-1.5 border-b border-gray-900 px-0 text-[12px] font-black text-gray-900 transition-colors hover:border-black hover:text-black disabled:cursor-not-allowed disabled:border-gray-200 disabled:text-gray-300"
                >
                  {cloudUserActionId === "create" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                  创建账号
                </button>
              </div>
              <div className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-5 md:grid-cols-2">
                <Field label="姓名/账号名">
                  <input
                    type="text"
                    value={newCloudUser.username}
                    onChange={(event) => onNewCloudUserChange((prev) => ({ ...prev, username: event.target.value }))}
                    placeholder={cloudUserModalRole === "operator" ? "例如 张三" : "例如 林见路"}
                    className="w-full border-0 border-b border-gray-200 bg-transparent px-0 py-2 text-[13px] text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-gray-900"
                  />
                </Field>
                <Field label="初始密码">
                  <div className="flex items-center gap-2 border-b border-gray-200 focus-within:border-gray-900">
                    <KeyRound className="h-3.5 w-3.5 shrink-0 text-gray-400" />
                    <input
                      type="password"
                      value={newCloudUser.password}
                      onChange={(event) => onNewCloudUserChange((prev) => ({ ...prev, password: event.target.value }))}
                      placeholder="至少 8 位"
                      className="min-w-0 flex-1 border-0 bg-transparent px-0 py-2 text-[13px] text-gray-900 outline-none placeholder:text-gray-400"
                    />
                  </div>
                </Field>
                <DatePickerField
                  label="生日"
                  value={newCloudUser.birthday}
                  onChange={(value) => onNewCloudUserChange((prev) => ({ ...prev, birthday: value }))}
                  fromYear={1950}
                  toYear={new Date().getFullYear() + 1}
                  variant="compact"
                  surface="plain"
                  className="min-w-0"
                  triggerClassName="border-0 border-b border-gray-200 rounded-none bg-transparent px-0 py-2 shadow-none hover:border-gray-300 focus:border-gray-900"
                />
                <DatePickerField
                  label="入职时间"
                  value={newCloudUser.hireDate}
                  onChange={(value) => onNewCloudUserChange((prev) => ({ ...prev, hireDate: value }))}
                  fromYear={1990}
                  toYear={new Date().getFullYear() + 1}
                  variant="compact"
                  surface="plain"
                  className="min-w-0"
                  triggerClassName="border-0 border-b border-gray-200 rounded-none bg-transparent px-0 py-2 shadow-none hover:border-gray-300 focus:border-gray-900"
                />
                {cloudUserModalRole === "viewer" ? (
                  <div className="min-w-0 md:col-span-2">
                    <ViewerTaskVisibilityEditor
                      tasks={filteredVisibleTasks}
                      totalTaskCount={cloudAdminTasks.length}
                      loaded={cloudAdminTasksLoaded}
                      search={visibleTaskSearch}
                      onSearchChange={onVisibleTaskSearchChange}
                      viewAllTasks={newCloudUser.viewAllTasks}
                      visibleTaskIds={newCloudUser.visibleTaskIds}
                      onToggleAllTasks={onSetNewCloudVisibleAll}
                      onToggleTask={onToggleNewCloudVisibleTask}
                    />
                  </div>
                ) : null}
              </div>
            </div>
          </section>

          <section className="mt-6 flex flex-col gap-4">
            <div className="flex min-w-0 items-end justify-between gap-4">
              <AccountModalSectionTitle title={`当前${CLOUD_USER_ROLE_LABELS[cloudUserModalRole]}`} />
              <div className="text-[11px] font-bold tracking-[0.16em] text-gray-400">
                {modalCloudUsers.length} 个
              </div>
            </div>

            {cloudUsersLoading && modalCloudUsers.length === 0 ? (
              <div className="flex items-center justify-center gap-2 border-y border-gray-100 py-8 text-[13px] font-medium text-gray-400">
                <Loader2 className="h-4 w-4 animate-spin" />
                正在读取云端账号
              </div>
            ) : modalCloudUsers.length > 0 ? (
              modalCloudUsers.map((user) => {
                const userId = String(user.id || "");
                const enabled = user.enabled !== false;
                const saving = cloudUserActionId === userId;
                const isManaging = managedCloudUser?.userId === userId;
                return (
                  <div key={userId} className="border-t border-gray-100 first:border-t-gray-200/80">
                    <div className="py-4">
                      <div className="flex min-w-0 flex-col gap-4">
                        <div className="min-w-0">
                          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
                            <span className="truncate text-[15px] font-black text-gray-900">{user.display_name || user.username}</span>
                            <span className="text-[10px] font-bold tracking-wide text-gray-300">#{userId}</span>
                            <span className={`inline-flex items-center gap-1.5 text-[11px] font-bold ${
                              enabled ? "text-gray-500" : "text-gray-300"
                            }`}>
                              <span className={`h-1.5 w-1.5 rounded-full ${enabled ? "bg-emerald-500" : "bg-gray-300"}`} />
                              {enabled ? "可登录" : "已停用"}
                            </span>
                          </div>
                          <div
                            className={`mt-3 grid min-w-0 grid-cols-2 gap-x-5 gap-y-3 ${
                              user.role === "viewer" ? "sm:grid-cols-4" : "sm:grid-cols-3"
                            }`}
                          >
                            <SimpleMeta
                              label="职位"
                              value={roleToProfileLabel(user.role) || CLOUD_USER_ROLE_LABELS[String(user.role || "")] || String(user.role || "")}
                            />
                            <SimpleMeta label="生日" value={formatProfileDate(normalizeDateText(user.birthday))} />
                            <SimpleMeta label="入职时间" value={formatProfileDate(normalizeDateText(user.hire_date))} />
                            {user.role === "viewer" ? (
                              <SimpleMeta
                                label="可见品牌"
                                value={user.view_all_tasks ? "全部可见" : `${getViewerVisibleTaskIds(user, cloudAdminTasks).length} 个`}
                              />
                            ) : null}
                          </div>
                        </div>
                        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 border-t border-gray-100 pt-3 text-[12px] font-bold">
                          <button
                            type="button"
                            onClick={() => { void onToggleCloudUserEnabled(user); }}
                            disabled={saving}
                            className={`inline-flex items-center gap-1.5 transition-colors ${
                              enabled ? "text-gray-500 hover:text-gray-900" : "text-gray-400 hover:text-gray-700"
                            }`}
                          >
                            {saving ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : enabled ? (
                              <Power className="h-3.5 w-3.5" />
                            ) : (
                              <PowerOff className="h-3.5 w-3.5" />
                            )}
                            {enabled ? "停用" : "启用"}
                          </button>
                          <button
                            type="button"
                            onClick={() => onOpenCloudUserManager(user)}
                            disabled={saving}
                            className={`inline-flex items-center gap-1.5 transition-colors disabled:cursor-not-allowed disabled:text-gray-300 ${
                              isManaging ? "text-gray-900" : "text-gray-500 hover:text-gray-900"
                            }`}
                          >
                            <Edit2 className="h-3.5 w-3.5" />
                            {isManaging ? "收起" : "管理"}
                          </button>
                          <button
                            type="button"
                            onClick={() => onDeleteCloudUser(user)}
                            disabled={saving}
                            className="inline-flex items-center gap-1.5 text-gray-300 transition-colors hover:text-red-500 disabled:cursor-not-allowed disabled:text-gray-200"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                            删除
                          </button>
                        </div>
                      </div>
                    </div>
                    {isManaging && managedCloudUser ? (
                      <div className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-4 border-t border-gray-100 py-5 md:grid-cols-2">
                        <div className="flex min-w-0 flex-col gap-1.5">
                          <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">姓名/账号名</label>
                          <input
                            type="text"
                            value={managedCloudUser.username}
                            onChange={(event) => onManagedCloudUserChange((current) => (
                              current?.userId === userId
                                ? { ...current, username: event.target.value, displayName: event.target.value }
                                : current
                            ))}
                            className="w-full border-0 border-b border-gray-200 bg-transparent px-0 py-2 text-[13px] text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-gray-900"
                          />
                        </div>
                        <div className="flex min-w-0 flex-col gap-1.5">
                          <label className="text-[11px] font-bold text-gray-500 tracking-widest uppercase">新密码</label>
                          <div className="flex items-center gap-2 border-b border-gray-200 focus-within:border-gray-900">
                            <KeyRound className="h-3.5 w-3.5 shrink-0 text-gray-400" />
                            <input
                              type="password"
                              value={managedCloudUser.password}
                              onChange={(event) => onManagedCloudUserChange((current) => (
                                current?.userId === userId
                                  ? { ...current, password: event.target.value }
                                  : current
                              ))}
                              placeholder="留空则不重设"
                              className="min-w-0 flex-1 border-0 bg-transparent px-0 py-2 text-[13px] text-gray-900 outline-none placeholder:text-gray-400"
                            />
                          </div>
                        </div>
                        <DatePickerField
                          label="生日"
                          value={managedCloudUser.birthday}
                          onChange={(value) => onManagedCloudUserChange((current) => (
                            current?.userId === userId
                              ? { ...current, birthday: value }
                              : current
                          ))}
                          fromYear={1950}
                          toYear={new Date().getFullYear() + 1}
                          variant="compact"
                          surface="plain"
                          className="min-w-0"
                          triggerClassName="border-0 border-b border-gray-200 rounded-none bg-transparent px-0 py-2 shadow-none hover:border-gray-300 focus:border-gray-900"
                        />
                        <DatePickerField
                          label="入职时间"
                          value={managedCloudUser.hireDate}
                          onChange={(value) => onManagedCloudUserChange((current) => (
                            current?.userId === userId
                              ? { ...current, hireDate: value }
                              : current
                          ))}
                          fromYear={1990}
                          toYear={new Date().getFullYear() + 1}
                          variant="compact"
                          surface="plain"
                          className="min-w-0"
                          triggerClassName="border-0 border-b border-gray-200 rounded-none bg-transparent px-0 py-2 shadow-none hover:border-gray-300 focus:border-gray-900"
                        />
                        {user.role === "viewer" ? (
                          <div className="min-w-0 md:col-span-2">
                            <ViewerTaskVisibilityEditor
                              tasks={filteredVisibleTasks}
                              totalTaskCount={cloudAdminTasks.length}
                              loaded={cloudAdminTasksLoaded}
                              search={visibleTaskSearch}
                              onSearchChange={onVisibleTaskSearchChange}
                              viewAllTasks={managedCloudUser.viewAllTasks}
                              visibleTaskIds={managedCloudUser.visibleTaskIds}
                              onToggleAllTasks={onSetManagedCloudVisibleAll}
                              onToggleTask={onToggleManagedCloudVisibleTask}
                              compact
                            />
                          </div>
                        ) : null}
                        <div className="flex min-w-0 items-center justify-end gap-4 text-[12px] font-bold md:col-span-2">
                          <button
                            type="button"
                            onClick={() => onManagedCloudUserChange(null)}
                            disabled={saving}
                            className="text-gray-400 transition-colors hover:text-gray-700 disabled:cursor-not-allowed disabled:text-gray-300"
                          >
                            取消
                          </button>
                          <button
                            type="button"
                            onClick={() => { void onSaveManagedCloudUser(user); }}
                            disabled={saving}
                            className="inline-flex items-center gap-1.5 text-gray-900 transition-colors hover:text-black disabled:cursor-not-allowed disabled:text-gray-300"
                          >
                            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                            保存
                          </button>
                        </div>
                      </div>
                    ) : null}
                  </div>
                );
              })
            ) : (
              <div className="border-y border-gray-100 py-8 text-center text-[13px] font-medium text-gray-400">
                {CLOUD_USER_ROLE_EMPTY_TEXT[cloudUserModalRole]}
              </div>
            )}
          </section>
          </div>
        </div>
      </div>
    </div>
  );
}

function CloudUserRoleEntry({
  role,
  count,
  onOpen,
}: {
  role: "operator" | "viewer";
  count: number;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group flex w-full items-center justify-between gap-5 py-4 text-left transition-colors hover:bg-gray-50/35"
    >
      <div className="min-w-0">
        <div className="text-[14px] font-black tracking-tight text-gray-900">
          {CLOUD_USER_ROLE_LABELS[role]}
        </div>
        <div className="mt-1 text-[11px] font-medium text-gray-400">
          {CLOUD_USER_ROLE_DESCRIPTIONS[role]}
        </div>
      </div>
      <div className="flex shrink-0 items-baseline gap-5">
        <span className="text-[20px] font-black leading-none text-gray-900 tabular-nums">
          {count}
        </span>
        <span className="border-b border-transparent pb-0.5 text-[12px] font-bold text-gray-400 transition-colors group-hover:border-gray-900 group-hover:text-gray-900">
          管理
        </span>
      </div>
    </button>
  );
}

function AccountModalSectionTitle({ title }: { title: string }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-3.5 w-1 rounded-sm bg-[var(--brand-navy)]" />
      <h3 className="text-[13px] font-black tracking-[0.14em] text-gray-900">{title}</h3>
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
    <div className="min-w-0">
      <div className="text-[11px] font-bold tracking-wide text-gray-400">{label}</div>
      <div className="mt-2 truncate text-[14px] font-semibold text-gray-900">{value}</div>
    </div>
  );
}
