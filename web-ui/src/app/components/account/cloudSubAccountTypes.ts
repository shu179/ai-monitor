export type CloudUserForm = {
  username: string;
  password: string;
  role: "operator" | "viewer";
  birthday: string;
  hireDate: string;
  viewAllTasks: boolean;
  visibleTaskIds: number[];
};

export type CloudUserManageForm = {
  userId: string;
  username: string;
  displayName: string;
  password: string;
  birthday: string;
  hireDate: string;
  viewAllTasks: boolean;
  visibleTaskIds: number[];
  viewerScopeDirty?: boolean;
};

export type CloudUsersNotice = { tone: "success" | "error"; message: string } | null;
