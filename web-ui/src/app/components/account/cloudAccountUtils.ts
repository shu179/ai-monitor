import type { CloudAdminTaskSnapshot, CloudUserSnapshot } from "../../lib/backend";

export const CLOUD_USER_ROLE_LABELS: Record<string, string> = {
  admin: "管理员",
  operator: "运营账号",
  viewer: "浏览账号",
};

export const CLOUD_PROFILE_ROLE_LABELS: Record<string, string> = {
  admin: "管理",
  operator: "运营",
  viewer: "销售",
};

export function roleToProfileLabel(role: unknown) {
  return CLOUD_PROFILE_ROLE_LABELS[String(role || "").trim()] || "";
}

export function normalizeCloudNumericId(value: unknown) {
  const id = Number(value || 0);
  return Number.isFinite(id) && id > 0 ? id : 0;
}

export function normalizeDateText(value: unknown) {
  return String(value || "").trim().slice(0, 10);
}

export function formatProfileDate(value: string) {
  const normalized = normalizeDateText(value);
  return normalized ? normalized.replace(/-/g, ".") : "未设置";
}

export function getViewerVisibleTaskIds(user: CloudUserSnapshot | undefined | null, tasks: CloudAdminTaskSnapshot[]) {
  const userId = normalizeCloudNumericId(user?.id);
  if (!userId) {
    return [];
  }
  if (user?.view_all_tasks) {
    return tasks.map((task) => task.id);
  }
  if (Array.isArray(user?.visible_task_ids)) {
    return user.visible_task_ids
      .map((item) => normalizeCloudNumericId(item))
      .filter((item) => item > 0);
  }
  return tasks
    .filter((task) => (task.assigned_viewer_user_ids || []).includes(userId))
    .map((task) => task.id);
}

export function areNumberArraysEqual(a: number[], b: number[]) {
  if (a.length !== b.length) {
    return false;
  }
  const left = [...a].sort((x, y) => x - y);
  const right = [...b].sort((x, y) => x - y);
  return left.every((value, index) => value === right[index]);
}
