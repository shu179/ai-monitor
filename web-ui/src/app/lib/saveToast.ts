export const SAVE_SUCCESS_TOAST_EVENT = "app:save-success-toast";

export function dispatchSaveSuccessToast(message = "保存成功") {
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(
    new CustomEvent(SAVE_SUCCESS_TOAST_EVENT, {
      detail: { message },
    }),
  );
}

export function notifySaveSuccess(
  onSaveSuccess?: (message?: string) => void,
  message = "保存成功",
) {
  onSaveSuccess?.(message);
  dispatchSaveSuccessToast(message);
}
