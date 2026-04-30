const MEDIA_BADGE_STYLES = [
  { container: "bg-emerald-50 text-emerald-600", tag: "bg-emerald-50 text-emerald-600" },
  { container: "bg-blue-50 text-blue-600", tag: "bg-blue-50 text-blue-600" },
  { container: "bg-orange-50 text-orange-600", tag: "bg-orange-50 text-orange-600" },
  { container: "bg-purple-50 text-purple-600", tag: "bg-purple-50 text-purple-600" },
  { container: "bg-rose-50 text-rose-600", tag: "bg-rose-50 text-rose-600" },
  { container: "bg-cyan-50 text-cyan-600", tag: "bg-cyan-50 text-cyan-600" },
  { container: "bg-amber-50 text-amber-600", tag: "bg-amber-50 text-amber-600" },
  { container: "bg-slate-100 text-slate-700", tag: "bg-slate-100 text-slate-600" },
];

function normalizeMediaName(source: string): string {
  return String(source || "").trim().toLowerCase();
}

function hashMediaName(source: string): number {
  const normalized = normalizeMediaName(source);
  let hash = 0;
  for (const ch of normalized) {
    hash = (hash * 33 + ch.charCodeAt(0)) >>> 0;
  }
  return hash;
}

export function getMediaBranding(source: string) {
  const normalized = normalizeMediaName(source);
  const style = MEDIA_BADGE_STYLES[hashMediaName(normalized || "media") % MEDIA_BADGE_STYLES.length];
  const displayText = String(source || "").trim();
  const initial = displayText ? displayText.substring(0, 1).toUpperCase() : "媒";

  return {
    initial,
    containerClassName: style.container,
    tagClassName: style.tag,
  };
}
