import { useEffect, useMemo, useRef, useState } from "react";
import { loadAmapJsApi } from "../lib/amap";
import { fallbackRegionNode, getRegionNodes, normalizeRegionName, splitRegionTags, type RegionMapType, type RegionMapNode } from "../lib/regionMap";

const AMAP_JS_KEY = import.meta.env.VITE_AMAP_JS_KEY || "";
const AMAP_SECURITY_CODE = import.meta.env.VITE_AMAP_SECURITY_CODE || "";
const AMAP_SERVICE_HOST = import.meta.env.VITE_AMAP_SERVICE_HOST || "";
const CHINA_CENTER: [number, number] = [104.5, 36.2];
const CHINA_OVERVIEW_ZOOM = 3.05;
const AMAP_MIN_ZOOM = 2.85;
const AMAP_MIN_ZOOM_EPSILON = 0.03;
const WHEEL_ZOOM_THRESHOLD = 40;
const WHEEL_ZOOM_STEP = 0.35;
const GEOCODE_CACHE_KEY = "dashboard.amap.regionGeocodeCache.v1";
const AMAP_BRANDING_SELECTOR = ".amap-logo, .amap-copyright";

type AmapRegionMapProps = {
  mapType: RegionMapType;
  activeRegions?: string[];
  className?: string;
  accentColor?: string;
  fallbackBorderClassName?: string;
};

type RenderNode = RegionMapNode & {
  active: boolean;
  x: string;
  y: string;
};

const DOMESTIC_FALLBACK_BOUNDS = {
  minLng: 73,
  maxLng: 135,
  minLat: 18,
  maxLat: 54,
};

const INTERNATIONAL_FALLBACK_BOUNDS = {
  minLng: -170,
  maxLng: 170,
  minLat: -48,
  maxLat: 72,
};

function getFallbackPosition(node: RegionMapNode, mapType: RegionMapType) {
  const bounds = mapType === "domestic" ? DOMESTIC_FALLBACK_BOUNDS : INTERNATIONAL_FALLBACK_BOUNDS;
  const x = ((node.lng - bounds.minLng) / (bounds.maxLng - bounds.minLng)) * 100;
  const y = (1 - (node.lat - bounds.minLat) / (bounds.maxLat - bounds.minLat)) * 100;
  return {
    x: `${Math.min(94, Math.max(6, x))}%`,
    y: `${Math.min(92, Math.max(8, y))}%`,
  };
}

function buildMarkerHtml(node: RegionMapNode, active: boolean, accentColor: string) {
  const dotSize = active ? 9 : 6;
  const glow = active
    ? `<span style="position:absolute;width:22px;height:22px;border-radius:999px;background:${accentColor};opacity:.18;transform:translate(-50%,-50%);left:50%;top:50%;"></span>`
    : "";

  return `
    <div style="position:relative;display:flex;align-items:center;gap:5px;white-space:nowrap;">
      <span style="position:relative;display:inline-flex;width:22px;height:22px;align-items:center;justify-content:center;">
        ${glow}
        <span style="position:relative;width:${dotSize}px;height:${dotSize}px;border-radius:999px;background:${active ? accentColor : "#cbd5e1"};border:1.5px solid #fff;box-shadow:0 4px 10px rgba(15,23,42,.16);"></span>
      </span>
      <span style="font-size:10px;font-weight:700;letter-spacing:.02em;color:${active ? "#1f2937" : "#94a3b8"};text-shadow:0 1px 0 rgba(255,255,255,.85);">${node.label}</span>
    </div>
  `;
}

function readGeocodeCache(): Record<string, { lng: number; lat: number }> {
  try {
    const raw = window.localStorage.getItem(GEOCODE_CACHE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeGeocodeCache(cache: Record<string, { lng: number; lat: number }>) {
  try {
    window.localStorage.setItem(GEOCODE_CACHE_KEY, JSON.stringify(cache));
  } catch {
    // localStorage may be unavailable in restricted WebViews.
  }
}

function hideAmapBranding(root: ParentNode = document) {
  root.querySelectorAll<HTMLElement>(AMAP_BRANDING_SELECTOR).forEach((element) => {
    element.style.setProperty("display", "none", "important");
    element.style.setProperty("opacity", "0", "important");
    element.style.setProperty("pointer-events", "none", "important");
  });
}

function buildFallbackNodes(mapType: RegionMapType, labels: string[]) {
  const sourceNodes = getRegionNodes(mapType);
  if (!labels.length) {
    return sourceNodes.map((node) => ({ ...node, active: false }));
  }

  const fallbackNodes = labels.map((label, index) => {
    const fallback = fallbackRegionNode(label, mapType);
    return fallback
      ? { ...fallback, id: `${fallback.id}-${index}`, label, active: true }
      : null;
  }).filter(Boolean) as Array<RegionMapNode & { active: boolean }>;

  return fallbackNodes.length ? fallbackNodes : sourceNodes.map((node) => ({ ...node, active: false }));
}

export function AmapRegionMap({
  mapType,
  activeRegions = [],
  className = "",
  accentColor = "#2FB8E6",
  fallbackBorderClassName = "border border-gray-100/60",
}: AmapRegionMapProps) {
  const mapRef = useRef<any>(null);
  const markerRef = useRef<any[]>([]);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewInitializedRef = useRef(false);
  const wheelDeltaRef = useRef(0);
  const [loadState, setLoadState] = useState<"idle" | "ready" | "fallback">("idle");
  const [geocodedNodes, setGeocodedNodes] = useState<RegionMapNode[] | null>(null);
  const normalizedActiveRegions = useMemo(() => splitRegionTags(activeRegions), [activeRegions]);
  const nodes = useMemo(() => {
    const sourceNodes = geocodedNodes || buildFallbackNodes(mapType, normalizedActiveRegions);
    return sourceNodes.map((node) => ({
      ...node,
      active: normalizedActiveRegions.length ? normalizedActiveRegions.some((label) => normalizeRegionName(label) === normalizeRegionName(node.label)) : false,
    }));
  }, [geocodedNodes, mapType, normalizedActiveRegions]);
  const activeNodes = nodes.filter((node) => node.active);
  const fallbackNodes: RenderNode[] = nodes.map((node) => ({
    ...node,
    ...getFallbackPosition(node, mapType),
  }));

  useEffect(() => {
    if (!AMAP_JS_KEY.trim()) {
      setLoadState("fallback");
      return;
    }

    let cancelled = false;

    loadAmapJsApi({
      key: AMAP_JS_KEY,
      securityCode: AMAP_SECURITY_CODE,
      serviceHost: AMAP_SERVICE_HOST,
    })
      .then((AMap) => {
        if (cancelled || !containerRef.current) {
          return;
        }

        if (!mapRef.current) {
          mapRef.current = new AMap.Map(containerRef.current, {
            zoom: mapType === "domestic" ? CHINA_OVERVIEW_ZOOM : 1.45,
            center: mapType === "domestic" ? CHINA_CENTER : [42, 24],
            viewMode: "2D",
            mapStyle: "amap://styles/whitesmoke",
            showOversea: true,
            vectorMapForeign: "Chinese_Simplified",
            showLabel: false,
            zooms: [AMAP_MIN_ZOOM, 18],
            resizeEnable: true,
            dragEnable: true,
            zoomEnable: true,
            scrollWheel: false,
            doubleClickZoom: false,
            keyboardEnable: false,
            jogEnable: false,
            animateEnable: false,
          });
        }

        setLoadState("ready");
        window.requestAnimationFrame(() => hideAmapBranding());
      })
      .catch(() => {
        if (!cancelled) {
          setLoadState("fallback");
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const AMap = window.AMap;
    const map = mapRef.current;
    if (loadState !== "ready" || !AMap?.Marker || !map) {
      return;
    }

    markerRef.current.forEach((marker) => marker.setMap?.(null));
    markerRef.current = [];

    const markers = nodes.map((node) => {
      const marker = new AMap.Marker({
        position: [node.lng, node.lat],
        content: buildMarkerHtml(node, node.active, accentColor),
        offset: new AMap.Pixel(-11, -11),
        zIndex: node.active ? 30 : 10,
      });
      marker.setMap(map);
      return marker;
    });

    markerRef.current = markers;

    if (mapType !== "domestic") {
      const fitMarkers = activeNodes.length ? markers.filter((_marker, index) => nodes[index].active) : markers;
      if (fitMarkers.length > 1) {
        map.setFitView(fitMarkers, false, [20, 20, 20, 20], 3.2);
      } else if (fitMarkers.length === 1) {
        map.setZoomAndCenter(3.4, fitMarkers[0].getPosition());
      } else {
        map.setZoomAndCenter(2, [42, 24]);
      }
    }
  }, [accentColor, activeNodes.length, loadState, mapType, nodes]);

  useEffect(() => {
    const AMap = window.AMap;
    if (loadState !== "ready" || !AMap?.Geocoder || !normalizedActiveRegions.length) {
      setGeocodedNodes(null);
      return;
    }

    let cancelled = false;
    const cache = readGeocodeCache();
    const geocoder = new AMap.Geocoder({
      city: mapType === "domestic" ? "全国" : "",
    });

    const resolveRegion = (label: string, index: number) => new Promise<RegionMapNode | null>((resolve) => {
      const normalizedLabel = normalizeRegionName(label);
      const cacheKey = `${mapType}:${normalizedLabel}`;
      const cached = cache[cacheKey];
      if (cached && Number.isFinite(cached.lng) && Number.isFinite(cached.lat)) {
        resolve({ id: `geo-${index}-${normalizedLabel}`, label, lng: cached.lng, lat: cached.lat, geocoded: true });
        return;
      }

      geocoder.getLocation(label, (status: string, result: any) => {
        if (status === "complete" && result?.geocodes?.[0]?.location) {
          const location = result.geocodes[0].location;
          const lng = Number(location.lng);
          const lat = Number(location.lat);
          if (Number.isFinite(lng) && Number.isFinite(lat)) {
            cache[cacheKey] = { lng, lat };
            resolve({ id: `geo-${index}-${normalizedLabel}`, label, lng, lat, geocoded: true });
            return;
          }
        }

        const fallback = fallbackRegionNode(label, mapType);
        resolve(fallback ? { ...fallback, id: `${fallback.id}-${index}`, label } : null);
      });
    });

    void Promise.all(normalizedActiveRegions.map(resolveRegion)).then((items) => {
      if (cancelled) {
        return;
      }
      writeGeocodeCache(cache);
      const resolved = items.filter(Boolean) as RegionMapNode[];
      setGeocodedNodes(resolved.length ? resolved : null);
    });

    return () => {
      cancelled = true;
    };
  }, [loadState, mapType, normalizedActiveRegions]);

  useEffect(() => {
    const map = mapRef.current;
    if (loadState !== "ready" || mapType !== "domestic" || !map || viewInitializedRef.current) {
      return;
    }

    viewInitializedRef.current = true;
    map.setZoomAndCenter(CHINA_OVERVIEW_ZOOM, CHINA_CENTER);
  }, [loadState, mapType]);

  useEffect(() => {
    const container = containerRef.current;
    const map = mapRef.current;
    if (loadState !== "ready" || !container || !map) {
      return;
    }

    const handleWheel = (event: WheelEvent) => {
      const currentZoom = typeof map.getZoom === "function" ? Number(map.getZoom()) : CHINA_OVERVIEW_ZOOM;
      const deltaScale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 240 : 1;
      const normalizedDelta = event.deltaY * deltaScale;
      if (!Number.isFinite(normalizedDelta) || normalizedDelta === 0) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();

      wheelDeltaRef.current += normalizedDelta;
      const steps = Math.trunc(wheelDeltaRef.current / WHEEL_ZOOM_THRESHOLD);
      if (steps === 0) return;
      wheelDeltaRef.current -= steps * WHEEL_ZOOM_THRESHOLD;

      const nextZoom = Math.min(18, Math.max(AMAP_MIN_ZOOM, currentZoom - steps * WHEEL_ZOOM_STEP));
      if (nextZoom <= AMAP_MIN_ZOOM + AMAP_MIN_ZOOM_EPSILON && steps > 0) {
        wheelDeltaRef.current = 0;
      }
      if (Math.abs(nextZoom - currentZoom) < 0.01) return;

      const center = typeof map.getCenter === "function" ? map.getCenter() : null;
      if (center && typeof map.setZoomAndCenter === "function") {
        map.setZoomAndCenter(nextZoom, center);
      } else {
        map.setZoom(nextZoom);
      }
    };

    container.addEventListener("wheel", handleWheel, { passive: false, capture: true });
    return () => {
      container.removeEventListener("wheel", handleWheel, { capture: true });
    };
  }, [loadState]);

  useEffect(() => {
    if (loadState !== "ready") {
      return;
    }

    hideAmapBranding();
    const container = containerRef.current;
    if (!container) {
      return;
    }

    const observer = new MutationObserver(() => hideAmapBranding());
    observer.observe(container, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
    };
  }, [loadState]);

  useEffect(() => {
    return () => {
      markerRef.current.forEach((marker) => marker.setMap?.(null));
      markerRef.current = [];
      mapRef.current?.destroy?.();
      mapRef.current = null;
      viewInitializedRef.current = false;
      hideAmapBranding();
    };
  }, []);

  if (loadState === "fallback") {
    return (
      <div className={`relative overflow-hidden bg-[#f8fafc] ${fallbackBorderClassName} ${className}`}>
        <div
          className="absolute inset-0 opacity-[0.45]"
          style={{
            backgroundImage:
              "linear-gradient(rgba(15,23,42,.055) 1px, transparent 1px), linear-gradient(90deg, rgba(15,23,42,.055) 1px, transparent 1px)",
            backgroundSize: "18px 18px",
          }}
        />
        <div className="absolute inset-x-0 top-0 h-10 bg-gradient-to-b from-white/80 to-transparent" />
        <svg className="absolute inset-0 h-full w-full opacity-20" aria-hidden="true">
          {fallbackNodes.filter((node) => node.active).map((node, index, items) => {
            if (index === items.length - 1) return null;
            const next = items[index + 1];
            return (
              <line
                key={`${node.id}-${next.id}`}
                x1={node.x}
                y1={node.y}
                x2={next.x}
                y2={next.y}
                stroke={accentColor}
                strokeWidth="1"
                strokeDasharray="3 4"
              />
            );
          })}
        </svg>
        {fallbackNodes.map((node) => (
          <div
            key={node.id}
            className="absolute flex -translate-x-1/2 -translate-y-1/2 items-center gap-1"
            style={{ left: node.x, top: node.y }}
          >
            <span className="relative flex h-5 w-5 items-center justify-center">
              {node.active && <span className="absolute h-5 w-5 rounded-full opacity-20" style={{ backgroundColor: accentColor }} />}
              <span
                className="relative rounded-full border-[1.5px] border-white shadow-sm"
                style={{
                  width: node.active ? 9 : 6,
                  height: node.active ? 9 : 6,
                  backgroundColor: node.active ? accentColor : "#cbd5e1",
                }}
              />
            </span>
            <span className={`text-[9px] font-bold tracking-wide ${node.active ? "text-gray-700" : "text-gray-400"}`}>
              {node.label}
            </span>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className={`relative overflow-hidden bg-[#f8fafc] ${fallbackBorderClassName} ${className}`}>
      <div ref={containerRef} className="h-full w-full [&_.amap-copyright]:hidden [&_.amap-logo]:hidden" />
      {loadState === "idle" && (
        <div className="absolute inset-0 bg-[#f8fafc]">
          <div
            className="h-full w-full opacity-[0.45]"
            style={{
              backgroundImage:
                "linear-gradient(rgba(15,23,42,.055) 1px, transparent 1px), linear-gradient(90deg, rgba(15,23,42,.055) 1px, transparent 1px)",
              backgroundSize: "18px 18px",
            }}
          />
        </div>
      )}
      <div className="pointer-events-none absolute inset-x-0 top-0 h-10 bg-gradient-to-b from-white/65 to-transparent" />
    </div>
  );
}
