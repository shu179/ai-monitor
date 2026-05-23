export type RegionMapType = "domestic" | "international";

export type RegionMapNode = {
  id: string;
  label: string;
  lng: number;
  lat: number;
  geocoded?: boolean;
};

const REGION_SUFFIX_PATTERN = /(维吾尔自治区|壮族自治区|回族自治区|自治区|特别行政区|省|市|地区|盟|州|县|区)$/g;

export function normalizeRegionName(value: string) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, "")
    .replace(REGION_SUFFIX_PATTERN, "");
}

export function splitRegionTags(value: string | string[] | undefined) {
  const source = Array.isArray(value) ? value : String(value || "").split(/[,，、\s]+/);
  return source.map(normalizeRegionName).filter(Boolean);
}

export function isRegionActive(label: string, activeRegions: string[]) {
  const normalizedLabel = normalizeRegionName(label);
  return activeRegions.some((item) => {
    const normalizedItem = normalizeRegionName(item);
    return normalizedItem === normalizedLabel || normalizedItem.includes(normalizedLabel) || normalizedLabel.includes(normalizedItem);
  });
}

export const DOMESTIC_REGION_NODES: RegionMapNode[] = [
  { id: "xj", label: "新疆", lng: 87.62, lat: 43.82 },
  { id: "xz", label: "西藏", lng: 91.13, lat: 29.65 },
  { id: "qh", label: "青海", lng: 101.78, lat: 36.62 },
  { id: "gs", label: "甘肃", lng: 103.83, lat: 36.06 },
  { id: "nm", label: "内蒙古", lng: 111.75, lat: 40.84 },
  { id: "hlj", label: "黑龙江", lng: 126.64, lat: 45.76 },
  { id: "jl", label: "吉林", lng: 125.32, lat: 43.82 },
  { id: "ln", label: "辽宁", lng: 123.43, lat: 41.8 },
  { id: "bj", label: "北京", lng: 116.41, lat: 39.9 },
  { id: "tj", label: "天津", lng: 117.2, lat: 39.09 },
  { id: "he", label: "河北", lng: 114.51, lat: 38.04 },
  { id: "sx", label: "山西", lng: 112.55, lat: 37.87 },
  { id: "sn", label: "陕西", lng: 108.94, lat: 34.34 },
  { id: "nx", label: "宁夏", lng: 106.23, lat: 38.49 },
  { id: "sd", label: "山东", lng: 117.12, lat: 36.65 },
  { id: "ha", label: "河南", lng: 113.62, lat: 34.75 },
  { id: "js", label: "江苏", lng: 118.8, lat: 32.06 },
  { id: "ah", label: "安徽", lng: 117.28, lat: 31.86 },
  { id: "sh", label: "上海", lng: 121.47, lat: 31.23 },
  { id: "zj", label: "浙江", lng: 120.15, lat: 30.27 },
  { id: "jx", label: "江西", lng: 115.86, lat: 28.68 },
  { id: "fj", label: "福建", lng: 119.3, lat: 26.08 },
  { id: "tw", label: "台湾", lng: 121.56, lat: 25.04 },
  { id: "hb", label: "湖北", lng: 114.31, lat: 30.52 },
  { id: "hn", label: "湖南", lng: 112.94, lat: 28.23 },
  { id: "gd", label: "广东", lng: 113.27, lat: 23.13 },
  { id: "hk", label: "香港", lng: 114.17, lat: 22.32 },
  { id: "mc", label: "澳门", lng: 113.55, lat: 22.2 },
  { id: "hi", label: "海南", lng: 110.35, lat: 20.02 },
  { id: "gx", label: "广西", lng: 108.32, lat: 22.82 },
  { id: "gz", label: "贵州", lng: 106.71, lat: 26.58 },
  { id: "sc", label: "四川", lng: 104.06, lat: 30.67 },
  { id: "cq", label: "重庆", lng: 106.55, lat: 29.56 },
  { id: "yn", label: "云南", lng: 102.71, lat: 25.04 },
];

export const INTERNATIONAL_REGION_NODES: RegionMapNode[] = [
  { id: "usa", label: "美国", lng: -95.71, lat: 37.09 },
  { id: "japan", label: "日本", lng: 139.69, lat: 35.69 },
  { id: "uk", label: "英国", lng: -0.13, lat: 51.51 },
  { id: "singapore", label: "新加坡", lng: 103.82, lat: 1.35 },
  { id: "germany", label: "德国", lng: 13.41, lat: 52.52 },
  { id: "australia", label: "澳大利亚", lng: 151.21, lat: -33.87 },
  { id: "france", label: "法国", lng: 2.35, lat: 48.86 },
  { id: "canada", label: "加拿大", lng: -75.7, lat: 45.42 },
  { id: "sk", label: "韩国", lng: 126.98, lat: 37.57 },
  { id: "brazil", label: "巴西", lng: -47.88, lat: -15.79 },
  { id: "india", label: "印度", lng: 77.21, lat: 28.61 },
  { id: "russia", label: "俄罗斯", lng: 37.62, lat: 55.75 },
];

export function getRegionNodes(mapType: RegionMapType) {
  return mapType === "domestic" ? DOMESTIC_REGION_NODES : INTERNATIONAL_REGION_NODES;
}

export function fallbackRegionNode(label: string, mapType: RegionMapType): RegionMapNode | null {
  const normalizedLabel = normalizeRegionName(label);
  return getRegionNodes(mapType).find((node) => isRegionActive(node.label, [normalizedLabel])) || null;
}
