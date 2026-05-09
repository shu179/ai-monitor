import { Check, Search } from "lucide-react";
import type { CloudAdminTaskSnapshot } from "../../lib/backend";

export function ViewerTaskVisibilityEditor({
  tasks,
  totalTaskCount,
  loaded,
  search,
  onSearchChange,
  viewAllTasks,
  visibleTaskIds,
  onToggleAllTasks,
  onToggleTask,
  compact = false,
}: {
  tasks: CloudAdminTaskSnapshot[];
  totalTaskCount: number;
  loaded: boolean;
  search: string;
  onSearchChange: (value: string) => void;
  viewAllTasks: boolean;
  visibleTaskIds: number[];
  onToggleAllTasks: (checked: boolean) => void;
  onToggleTask: (taskId: number) => void;
  compact?: boolean;
}) {
  const selectedCount = viewAllTasks ? totalTaskCount : visibleTaskIds.length;
  const selectedTaskIds = new Set(visibleTaskIds);
  const modeLabel = viewAllTasks ? "全部可见" : "自定义范围";
  const hasMoreThanThreeRows = tasks.length > 3;

  return (
    <div className={`w-full max-w-full overflow-hidden border-y border-gray-200/80 bg-white ${compact ? "py-4" : "py-5"}`}>
      <div className="flex min-w-0 flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
            <div className="text-[12px] font-black tracking-tight text-gray-900">可见品牌</div>
            <span className="text-[10px] font-bold tracking-[0.16em] text-gray-300 uppercase">
              {modeLabel}
            </span>
          </div>
          <div className="mt-1 text-[12px] font-medium text-gray-400">
            {viewAllTasks ? "当前及未来新增品牌都会自动可见" : `已选 ${selectedCount} / ${totalTaskCount} 个品牌`}
          </div>
        </div>

        <div className="inline-grid w-full max-w-[240px] shrink-0 grid-cols-2 overflow-hidden rounded-full border border-gray-200 bg-gray-50/70 p-0.5 text-[11px] font-black sm:w-auto">
          <button
            type="button"
            onClick={() => onToggleAllTasks(true)}
            className={`min-w-0 rounded-full px-3 py-1.5 transition-colors ${
              viewAllTasks ? "bg-white text-gray-900 shadow-sm" : "text-gray-400 hover:text-gray-700"
            }`}
          >
            全部可见
          </button>
          <button
            type="button"
            onClick={() => onToggleAllTasks(false)}
            className={`min-w-0 rounded-full px-3 py-1.5 transition-colors ${
              !viewAllTasks ? "bg-white text-gray-900 shadow-sm" : "text-gray-400 hover:text-gray-700"
            }`}
          >
            自定义
          </button>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2 border-b border-gray-200/90 pb-2.5 transition-colors focus-within:border-gray-900">
        <Search className="h-3.5 w-3.5 shrink-0 text-gray-300" />
        <input
          type="text"
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="搜索品牌名称或任务标识"
          className="min-w-0 flex-1 border-0 bg-transparent p-0 text-[12px] font-semibold text-gray-900 outline-none placeholder:text-gray-400"
        />
      </div>

      <div className="relative mt-3 min-w-0 overflow-hidden border-b border-gray-200/70">
        <div className="max-h-[138px] min-w-0 overflow-y-auto pr-1 scrollbar-thin scrollbar-thumb-gray-300 scrollbar-track-transparent">
        {tasks.length > 0 ? (
          tasks.map((task) => {
            const checked = viewAllTasks || selectedTaskIds.has(task.id);
            const title = task.brand || task.name || task.task_key || `品牌任务 ${task.id}`;
            return (
              <label
                key={task.id}
                className={`group flex w-full min-w-0 cursor-pointer items-center gap-3 border-t border-gray-100 py-3 text-left transition-colors first:border-t-0 ${
                  checked ? "bg-gray-50/55" : "hover:bg-gray-50/45"
                }`}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => onToggleTask(task.id)}
                  className="sr-only"
                />
                <span
                  className={`grid h-5 w-5 shrink-0 place-items-center rounded-full border transition-colors ${
                    checked
                      ? "border-gray-900 bg-gray-900 text-white"
                      : "border-gray-200 bg-white text-transparent group-hover:border-gray-400"
                  }`}
                >
                  <Check className="h-3 w-3" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-baseline justify-between gap-3">
                    <div className="truncate text-[13px] font-black text-gray-900">{title}</div>
                  </div>
                </div>
              </label>
            );
          })
        ) : (
          <div className="border-t border-gray-100 py-9 text-center text-[12px] font-medium text-gray-400">
            {!loaded ? "品牌列表读取中" : totalTaskCount > 0 ? "没有匹配的品牌" : "暂无品牌任务可选"}
          </div>
        )}
        </div>
        {hasMoreThanThreeRows ? (
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-white to-white/0" />
        ) : null}
      </div>
    </div>
  );
}
