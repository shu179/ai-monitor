import type { ReactNode } from "react";

export function AccountSection({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="border-b border-gray-200/80 pb-8 last:border-b-0">
      <div className="mb-5 flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <div className="h-3.5 w-1 rounded-full bg-blue-600" />
          <h2 className="text-[14px] font-bold tracking-tight text-gray-900">{title}</h2>
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-[12px] font-bold text-gray-500">{label}</label>
      {children}
    </div>
  );
}

export function ReadOnlyFieldValue({ value }: { value: string }) {
  return (
    <div className="w-full border-b border-gray-200 px-0 py-2 text-[14px] font-semibold text-gray-900">
      {value}
    </div>
  );
}

export function DetailItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-b border-gray-200/70 pb-3">
      <div className="text-[11px] font-bold tracking-wide text-gray-400">{label}</div>
      <div className="mt-2 text-[16px] font-semibold leading-snug text-gray-900">{value}</div>
    </div>
  );
}
