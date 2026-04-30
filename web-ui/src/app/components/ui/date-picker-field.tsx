"use client";

import * as React from "react";
import { CalendarDays, ChevronDown } from "lucide-react";
import { DayPicker } from "react-day-picker";

import { Popover, PopoverContent, PopoverTrigger } from "./popover";
import { cn } from "./utils";

export function parseDateValue(value: string): Date | undefined {
  if (!value) {
    return undefined;
  }
  const [year, month, day] = value.split("-").map((item) => Number(item));
  if (!year || !month || !day) {
    return undefined;
  }
  const parsed = new Date(year, month - 1, day);
  if (
    Number.isNaN(parsed.getTime()) ||
    parsed.getFullYear() !== year ||
    parsed.getMonth() !== month - 1 ||
    parsed.getDate() !== day
  ) {
    return undefined;
  }
  return parsed;
}

export function formatDateValue(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function formatDateDisplay(value: string): string {
  if (!value) {
    return "请选择日期";
  }
  const parsed = parseDateValue(value);
  if (!parsed) {
    return value;
  }
  return `${parsed.getFullYear()}.${String(parsed.getMonth() + 1).padStart(2, "0")}.${String(parsed.getDate()).padStart(2, "0")}`;
}

type DatePickerFieldProps = {
  label?: string;
  value: string;
  onChange: (val: string) => void;
  fromYear?: number;
  toYear?: number;
  placeholder?: string;
  variant?: "default" | "compact";
  icon?: React.ReactNode;
  className?: string;
  triggerClassName?: string;
  contentClassName?: string;
  showTodayShortcut?: boolean;
  surface?: "soft" | "plain";
};

export function DatePickerField({
  label,
  value,
  onChange,
  fromYear = 1950,
  toYear = new Date().getFullYear() + 5,
  placeholder = "请选择日期",
  variant = "default",
  icon,
  className,
  triggerClassName,
  contentClassName,
  showTodayShortcut = true,
  surface = "soft",
}: DatePickerFieldProps) {
  const [open, setOpen] = React.useState(false);
  const selectedDate = React.useMemo(() => parseDateValue(value), [value]);
  const [month, setMonth] = React.useState<Date>(selectedDate || new Date());

  React.useEffect(() => {
    if (open) {
      setMonth(selectedDate || new Date());
    }
  }, [open, selectedDate]);

  const isCompact = variant === "compact";
  const isPlainSurface = surface === "plain";
  const displayText = value ? formatDateDisplay(value) : placeholder;
  const currentMonthLabel = `${month.getFullYear()} 年 ${month.getMonth() + 1} 月`;
  const triggerBaseClassName = isCompact
    ? "w-full flex items-center justify-between gap-2 bg-[linear-gradient(180deg,#ffffff_0%,#fbfbfc_100%)] border border-gray-200/80 rounded-xl px-3 py-2 text-[12px] font-semibold text-gray-800 shadow-[0_8px_20px_-16px_rgba(15,23,42,0.26)] hover:border-gray-300 hover:shadow-[0_12px_24px_-18px_rgba(15,23,42,0.26)] focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 transition-all"
    : "w-full flex items-center justify-between gap-3 bg-[linear-gradient(180deg,#ffffff_0%,#fbfbfc_100%)] border border-gray-200 rounded-[14px] px-3.5 py-2.5 text-[12px] text-gray-800 shadow-[0_10px_24px_-20px_rgba(15,23,42,0.28)] hover:border-gray-300 hover:shadow-[0_14px_28px_-22px_rgba(15,23,42,0.28)] focus:border-blue-500 focus:ring-1 focus:ring-blue-500/20 transition-all";
  const contentBaseClassName = isPlainSurface
    ? "w-[296px] rounded-[18px] border-gray-200/85 bg-white p-2.5 shadow-[0_14px_34px_-28px_rgba(15,23,42,0.26)]"
    : "w-[308px] rounded-[22px] border-gray-200/90 bg-[linear-gradient(180deg,rgba(255,255,255,0.98),rgba(249,250,251,0.96))] p-3 shadow-[0_24px_56px_-28px_rgba(15,23,42,0.28)] backdrop-blur-sm";
  const pickerHeaderClassName = isPlainSurface
    ? "mb-2 rounded-[14px] border border-gray-100 bg-white px-3 py-2"
    : "mb-2.5 rounded-[18px] border border-gray-100/90 bg-[linear-gradient(135deg,rgba(248,250,252,0.96),rgba(255,255,255,1))] px-3 py-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]";
  const pickerIconClassName = isPlainSurface
    ? "w-7 h-7 rounded-xl bg-gray-50 border border-gray-100 flex items-center justify-center shrink-0"
    : "w-8 h-8 rounded-2xl bg-white border border-gray-200/80 shadow-sm flex items-center justify-center shrink-0";
  const pickerCalendarPanelClassName = isPlainSurface
    ? "rounded-[14px] border border-gray-100 bg-white p-1.5"
    : "rounded-[18px] border border-gray-100/90 bg-white/90 p-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]";
  const footerClassName = isPlainSurface
    ? "mt-2 pt-2 border-t border-gray-100 flex items-center justify-between gap-3"
    : "mt-2.5 pt-2.5 border-t border-gray-100/90 flex items-center justify-between gap-3";
  const todayButtonClassName = isPlainSurface
    ? "px-3 py-1.5 rounded-lg text-[11px] font-bold text-gray-600 bg-white hover:bg-gray-50 border border-gray-200 transition-colors"
    : "px-3 py-1.5 rounded-xl text-[11px] font-bold text-gray-700 bg-[linear-gradient(180deg,#ffffff_0%,#f9fafb_100%)] hover:bg-gray-50 border border-gray-200 shadow-sm transition-colors";

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {label ? <label className="text-[12px] font-bold text-gray-900">{label}</label> : null}
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button type="button" className={cn(triggerBaseClassName, triggerClassName)}>
            <div className="flex items-center gap-2 min-w-0">
              {icon ?? <CalendarDays className="w-4 h-4 text-gray-400 shrink-0" />}
              <span className={cn("truncate", value ? "text-gray-900" : "text-gray-400")}>
                {value ? formatDateDisplay(value) : placeholder}
              </span>
            </div>
            <ChevronDown className={cn("shrink-0 text-gray-300", isCompact ? "w-3.5 h-3.5" : "w-4 h-4")} />
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          sideOffset={8}
          className={cn(
            contentBaseClassName,
            contentClassName,
          )}
        >
          <div className={pickerHeaderClassName}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-[10px] font-bold tracking-[0.18em] text-gray-400 uppercase">日期选择</div>
                <div className="mt-1 text-[13px] font-semibold text-gray-900 truncate">
                  {displayText}
                </div>
                <div className="mt-1 text-[11px] font-medium text-gray-400">
                  {currentMonthLabel}
                </div>
              </div>
              <div className={pickerIconClassName}>
                <CalendarDays className="w-3.5 h-3.5 text-gray-500" />
              </div>
            </div>
          </div>

          <div className={pickerCalendarPanelClassName}>
            <DayPicker
              mode="single"
              selected={selectedDate}
              month={month}
              onMonthChange={setMonth}
              onSelect={(date) => {
                if (!date) {
                  return;
                }
                onChange(formatDateValue(date));
                setOpen(false);
              }}
              captionLayout="dropdown"
              fromYear={fromYear}
              toYear={toYear}
              showOutsideDays
              weekStartsOn={1}
              className="w-full"
              classNames={{
                months: "w-full",
                month: "w-full space-y-2.5",
                caption: "pt-1",
                caption_dropdowns: "grid grid-cols-2 gap-1.5 px-1 items-center",
                dropdown_month: "relative",
                dropdown_year: "relative",
                dropdown: "absolute inset-0 z-10 w-full h-full opacity-0 cursor-pointer appearance-none",
                caption_label: isPlainSurface
                  ? "flex h-8 items-center justify-between rounded-lg border border-gray-100 bg-white px-2.5 text-[11px] font-semibold text-gray-700"
                  : "flex h-8 items-center justify-between rounded-xl border border-gray-200 bg-[linear-gradient(180deg,#ffffff_0%,#f9fafb_100%)] px-2.5 text-[11px] font-semibold text-gray-700 shadow-[0_8px_20px_-16px_rgba(15,23,42,0.3)]",
                dropdown_icon: "w-2.5 h-2.5 text-gray-300 shrink-0",
                vhidden: "sr-only",
                table: "w-full border-collapse",
                head_row: "grid grid-cols-7 mb-1",
                head_cell: isPlainSurface
                  ? "mx-auto w-8 h-6 text-[9px] font-bold tracking-[0.1em] text-gray-400 flex items-center justify-center uppercase"
                  : "mx-auto w-8 h-6 rounded-lg bg-gray-50 text-[9px] font-bold tracking-[0.1em] text-gray-400 flex items-center justify-center uppercase",
                row: "grid grid-cols-7 gap-y-1",
                cell: "p-0 text-center",
                day: "w-8 h-8 mx-auto rounded-[14px] text-[11px] font-semibold text-gray-700 hover:bg-gray-100 hover:text-gray-900 transition-all",
                day_selected: isPlainSurface
                  ? "bg-gray-900 text-white hover:bg-gray-900 hover:text-white"
                  : "bg-gray-900 text-white hover:bg-gray-900 hover:text-white shadow-[0_14px_24px_-18px_rgba(17,24,39,0.95)]",
                day_today: isPlainSurface
                  ? "text-[var(--brand-navy)] bg-gray-50 ring-1 ring-gray-100"
                  : "text-blue-600 bg-blue-50 ring-1 ring-blue-100",
                day_outside: "text-gray-300 opacity-70",
                day_disabled: "text-gray-200 opacity-60",
                nav: "hidden",
              }}
            />
          </div>

          {(showTodayShortcut || value) ? (
            <div className={footerClassName}>
              {showTodayShortcut ? (
                <button
                  type="button"
                  onClick={() => {
                    const today = new Date();
                    setMonth(today);
                    onChange(formatDateValue(today));
                    setOpen(false);
                  }}
                  className={todayButtonClassName}
                >
                  今天
                </button>
              ) : (
                <span />
              )}
              <span className="text-[10px] font-medium text-gray-400 text-right">
                {displayText}
              </span>
            </div>
          ) : null}
        </PopoverContent>
      </Popover>
    </div>
  );
}
