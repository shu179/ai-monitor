type AMapSecurityConfig = {
  securityJsCode?: string;
  serviceHost?: string;
};

declare global {
  interface Window {
    AMap?: any;
    _AMapSecurityConfig?: AMapSecurityConfig;
  }
}

let amapLoadPromise: Promise<any> | null = null;

export function loadAmapJsApi({
  key,
  securityCode,
  serviceHost,
}: {
  key: string;
  securityCode?: string;
  serviceHost?: string;
}) {
  const safeKey = key.trim();
  if (!safeKey) {
    return Promise.reject(new Error("Missing AMap JS API key"));
  }

  if (typeof window === "undefined") {
    return Promise.reject(new Error("AMap JS API can only load in the browser"));
  }

  if (window.AMap?.Map) {
    return Promise.resolve(window.AMap);
  }

  if (serviceHost?.trim()) {
    window._AMapSecurityConfig = {
      serviceHost: serviceHost.trim().replace(/\/$/, ""),
    };
  } else if (securityCode?.trim()) {
    window._AMapSecurityConfig = {
      securityJsCode: securityCode.trim(),
    };
  }

  if (amapLoadPromise) {
    return amapLoadPromise;
  }

  amapLoadPromise = new Promise((resolve, reject) => {
    const callbackName = `__dashboardAmapReady_${Date.now()}`;
    const cleanup = () => {
      try {
        delete (window as any)[callbackName];
      } catch {
        (window as any)[callbackName] = undefined;
      }
    };

    (window as any)[callbackName] = () => {
      cleanup();
      if (window.AMap?.Map) {
        resolve(window.AMap);
      } else {
        amapLoadPromise = null;
        reject(new Error("AMap JS API loaded without Map runtime"));
      }
    };

    const script = document.createElement("script");
    script.async = true;
    script.defer = true;
    script.dataset.dashboardAmap = "true";
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(safeKey)}&plugin=AMap.Geocoder&callback=${callbackName}`;
    script.onerror = () => {
      cleanup();
      amapLoadPromise = null;
      reject(new Error("Failed to load AMap JS API"));
    };
    script.onload = () => {
      if (window.AMap?.Map) {
        cleanup();
        resolve(window.AMap);
      }
    };

    document.head.appendChild(script);
  });

  return amapLoadPromise;
}
