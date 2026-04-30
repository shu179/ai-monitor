import React from 'react';

function getGreeting() {
  const h = new Date().getHours();
  if (h < 6)  return '凌晨好';
  if (h < 12) return '早上好';
  if (h < 18) return '下午好';
  return '晚上好';
}

function buildCalendar(year: number, month: number): (number | null)[] {
  const dow    = new Date(year, month, 1).getDay();
  const offset = (dow + 6) % 7;
  const total  = new Date(year, month + 1, 0).getDate();
  const cells: (number | null)[] = Array(offset).fill(null);
  for (let d = 1; d <= total; d++) cells.push(d);
  while (cells.length % 7 !== 0) cells.push(null);
  return cells;
}

export function UserProfile() {
  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth();
  const today = now.getDate();
  const cells = buildCalendar(year, month);
  const weeks: (number | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  const WD = ['一', '二', '三', '四', '五', '六', '日'];
  const dateText = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    weekday: "long",
  }).format(now).replace(/\//g, " · ");

  return (
    <div className="rounded-[28px] h-[240px] flex shrink-0 w-full bg-white border border-slate-100 shadow-[0_4px_24px_rgba(0,0,0,0.04)] overflow-hidden">

      {/* ── 左侧：问候区 ── */}
      <div className="flex-1 flex flex-col justify-center px-12 border-r border-slate-100">
        <p className="text-[12px] font-semibold text-slate-400 tracking-widest mb-3 uppercase">
          {dateText}
        </p>
        <h1 className="text-[2rem] font-black text-slate-900 tracking-tight leading-snug mb-3">
          {getGreeting()}，陈晓雯 👋
        </h1>
        <p className="text-[13.5px] text-slate-400 font-medium leading-relaxed">
          今天有 <span className="text-slate-800 font-bold">3 项</span> 任务待处理，整体进度已达 80%
        </p>
        <div className="mt-5 flex items-center gap-3">
          <div className="w-40 h-1.5 bg-slate-100 rounded-full overflow-hidden">
            <div className="h-full w-[80%] bg-slate-900 rounded-full" />
          </div>
          <span className="text-[12px] font-bold text-slate-400">80%</span>
        </div>
      </div>

      {/* ── 右侧：日历 ── */}
      <div className="w-[280px] flex flex-col justify-center px-7 py-5">

        <div className="flex justify-between items-baseline mb-3">
          <span className="text-[14px] font-extrabold text-slate-700">
            {new Intl.DateTimeFormat("zh-CN", { month: "long" }).format(now)}
          </span>
          <span className="text-[12px] text-slate-400 font-medium">{year}</span>
        </div>

        <div className="grid grid-cols-7 mb-1.5">
          {WD.map(d => (
            <div key={d} className="text-center text-[11px] font-semibold text-slate-300">{d}</div>
          ))}
        </div>

        {weeks.map((week, wi) => (
          <div key={wi} className="grid grid-cols-7">
            {week.map((day, di) => (
              <div
                key={di}
                className={`text-center text-[12px] leading-[22px] rounded-lg font-medium
                  ${day === today
                    ? 'bg-slate-900 text-white font-bold'
                    : day === null
                    ? ''
                    : 'text-slate-500'
                  }`}
              >
                {day ?? ''}
              </div>
            ))}
          </div>
        ))}

      </div>
    </div>
  );
}
