import {
  fetchCloudAdminTasks,
  fetchCloudAdminUsers,
  type CloudAdminTaskSnapshot,
  type CloudStatusSnapshot,
  type CloudUserSnapshot,
} from "./backend";

const CLOUD_USERS_CACHE_TTL_MS = 3 * 60 * 1000;
const CLOUD_TASKS_CACHE_TTL_MS = 5 * 60 * 1000;

export type CloudAdminCacheSnapshot = {
  identityKey: string;
  status: CloudStatusSnapshot | null;
  users: CloudUserSnapshot[];
  usersLoaded: boolean;
  usersFetchedAt: number;
  tasks: CloudAdminTaskSnapshot[];
  tasksLoaded: boolean;
  tasksFetchedAt: number;
};

const EMPTY_CACHE: CloudAdminCacheSnapshot = {
  identityKey: "",
  status: null,
  users: [],
  usersLoaded: false,
  usersFetchedAt: 0,
  tasks: [],
  tasksLoaded: false,
  tasksFetchedAt: 0,
};

let cache: CloudAdminCacheSnapshot = { ...EMPTY_CACHE };
let usersRequest: Promise<CloudAdminCacheSnapshot> | null = null;
let tasksRequest: Promise<CloudAdminCacheSnapshot> | null = null;
const listeners = new Set<(snapshot: CloudAdminCacheSnapshot) => void>();

export function readCloudAdminCache(): CloudAdminCacheSnapshot {
  return cloneCache(cache);
}

export function subscribeCloudAdminCache(listener: (snapshot: CloudAdminCacheSnapshot) => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export async function ensureCloudAdminUsers(options?: { force?: boolean }) {
  const force = Boolean(options?.force);
  if (!force && cache.usersLoaded && Date.now() - cache.usersFetchedAt < CLOUD_USERS_CACHE_TTL_MS) {
    return readCloudAdminCache();
  }
  if (usersRequest) {
    return usersRequest;
  }

  const request = (async () => {
    const result = await fetchCloudAdminUsers();
    if (!result.ok) {
      if (result.cloud?.loggedIn) {
        commitCloudAdminStatus(result.cloud);
      }
      return readCloudAdminCache();
    }

    commitCloudAdminUsers(result.users || [], result.cloud);
    return readCloudAdminCache();
  })();
  usersRequest = request;
  try {
    return await request;
  } finally {
    if (usersRequest === request) {
      usersRequest = null;
    }
  }
}

export async function ensureCloudAdminTasks(options?: { force?: boolean }) {
  const force = Boolean(options?.force);
  if (!force && cache.tasksLoaded && Date.now() - cache.tasksFetchedAt < CLOUD_TASKS_CACHE_TTL_MS) {
    return readCloudAdminCache();
  }
  if (tasksRequest) {
    return tasksRequest;
  }

  const request = (async () => {
    const result = await fetchCloudAdminTasks();
    if (!result.ok) {
      if (result.cloud?.loggedIn) {
        commitCloudAdminStatus(result.cloud);
      }
      return readCloudAdminCache();
    }

    commitCloudAdminTasks(result.tasks || [], result.cloud);
    return readCloudAdminCache();
  })();
  tasksRequest = request;
  try {
    return await request;
  } finally {
    if (tasksRequest === request) {
      tasksRequest = null;
    }
  }
}

export function commitCloudAdminStatus(status: CloudStatusSnapshot | null | undefined) {
  const previousIdentityKey = cache.identityKey;
  const previousStatusKey = cloudStatusUserSignature(cache.status);
  applyStatusToCache(status ?? null);
  if (cache.identityKey !== previousIdentityKey || cloudStatusUserSignature(cache.status) !== previousStatusKey) {
    emitCloudAdminCache();
  }
}

export function commitCloudAdminUsers(users: CloudUserSnapshot[], status?: CloudStatusSnapshot | null) {
  if (status !== undefined) {
    applyStatusToCache(status);
  }
  cache = {
    ...cache,
    users: sortCloudUsers(users),
    usersLoaded: true,
    usersFetchedAt: Date.now(),
  };
  emitCloudAdminCache();
}

export function updateCloudAdminUsersCache(updater: (users: CloudUserSnapshot[]) => CloudUserSnapshot[]) {
  cache = {
    ...cache,
    users: sortCloudUsers(updater(cache.users)),
    usersLoaded: true,
    usersFetchedAt: Date.now(),
  };
  emitCloudAdminCache();
}

export function commitCloudAdminTasks(tasks: CloudAdminTaskSnapshot[], status?: CloudStatusSnapshot | null) {
  if (status !== undefined) {
    applyStatusToCache(status);
  }
  cache = {
    ...cache,
    tasks: sortCloudTasks(tasks),
    tasksLoaded: true,
    tasksFetchedAt: Date.now(),
  };
  emitCloudAdminCache();
}

export function upsertCloudAdminTaskCache(task: CloudAdminTaskSnapshot) {
  if (!task?.id) {
    return;
  }
  commitCloudAdminTasks([
    ...cache.tasks.filter((item) => item.id !== task.id),
    task,
  ]);
}

export function invalidateCloudAdminTasksCache() {
  cache = {
    ...cache,
    tasksFetchedAt: 0,
    tasksLoaded: false,
  };
  emitCloudAdminCache();
}

function applyStatusToCache(status: CloudStatusSnapshot | null) {
  const nextIdentityKey = cloudStatusIdentityKey(status);
  if (!nextIdentityKey) {
    usersRequest = null;
    tasksRequest = null;
    cache = {
      ...EMPTY_CACHE,
      status,
      identityKey: "",
    };
    return;
  }
  if (cache.identityKey && cache.identityKey !== nextIdentityKey) {
    usersRequest = null;
    tasksRequest = null;
    cache = {
      ...EMPTY_CACHE,
      status,
      identityKey: nextIdentityKey,
    };
    return;
  }
  cache = {
    ...cache,
    status,
    identityKey: nextIdentityKey,
  };
}

function emitCloudAdminCache() {
  const snapshot = readCloudAdminCache();
  listeners.forEach((listener) => listener(snapshot));
}

function cloneCache(source: CloudAdminCacheSnapshot): CloudAdminCacheSnapshot {
  return {
    ...source,
    status: source.status ? { ...source.status, user: { ...source.status.user } } : null,
    users: source.users.map((user) => ({ ...user })),
    tasks: source.tasks.map((task) => ({ ...task })),
  };
}

function cloudStatusIdentityKey(status: CloudStatusSnapshot | null | undefined) {
  if (!isAdminCloudStatus(status)) {
    return "";
  }
  return [
    String(status?.baseUrl || "").trim(),
    String(status?.user.workspace_id || "").trim(),
    String(status?.user.id || "").trim(),
  ].join("|");
}

function isAdminCloudStatus(status: CloudStatusSnapshot | null | undefined) {
  return Boolean(status?.loggedIn && status.user.role === "admin");
}

function cloudStatusUserSignature(status: CloudStatusSnapshot | null | undefined) {
  if (!status?.loggedIn) {
    return "logged-out";
  }
  const user = status.user || {};
  return [
    String(status.baseUrl || "").trim(),
    String(user.workspace_id || "").trim(),
    String(user.id || "").trim(),
    String(user.role || "").trim(),
    String(user.username || "").trim(),
    String(user.display_name || "").trim(),
    String(user.avatar || "").trim(),
    String(user.birthday || "").trim(),
    String(user.hire_date || "").trim(),
    String(user.enabled ?? "").trim(),
    String(user.token_version || "").trim(),
    String(user.deleted_at || "").trim(),
  ].join("|");
}

function sortCloudUsers(users: CloudUserSnapshot[]) {
  return [...users].sort((a, b) => Number(a.id || 0) - Number(b.id || 0));
}

function sortCloudTasks(tasks: CloudAdminTaskSnapshot[]) {
  return [...tasks].sort((a, b) => a.id - b.id);
}
