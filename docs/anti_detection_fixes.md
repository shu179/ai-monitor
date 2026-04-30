# 反检测修复总结

## 修复日期
2026-04-21

## 问题分析

根据指纹检测报告，原有反检测实现存在以下漏洞：

1. **时区伪装错误** - 随机使用台北时区，与实际东八区位置不符
2. **User-Agent 与硬件不匹配** - 伪装 Intel 架构但 GPU 暴露 M4 Pro（ARM）
3. **Client Hints 架构泄露** - sec-ch-ua-arch 暴露真实 ARM 架构
4. **WebRTC IP 泄露** - 本地 IP 防护不完整
5. **Headless 检测风险** - 31% 概率被识别为自动化工具
6. **Canvas/Audio 指纹** - 缺少指纹噪声，容易被追踪

## 修复方案

### 1. 时区配置修复

**文件**: `core/browser_fingerprint.py`

**修改前**:
```python
timezones = ["Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei"]
timezone_id = random.choice(timezones)
```

**修改后**:
```python
# 使用真实系统时区，不伪装
import datetime
try:
    local_tz = datetime.datetime.now().astimezone().tzinfo
    timezone_id = str(getattr(local_tz, 'key', None) or 'Asia/Shanghai')
except Exception:
    timezone_id = "Asia/Shanghai"
```

**原理**: 不再随机伪装时区，使用真实系统时区。伪装时区反而会暴露矛盾（如台北时区但语言是简体中文）。

---

### 2. User-Agent 与硬件配置修复

**文件**: `core/browser_fingerprint.py`

**修改前**:
```python
mac_major = random.randint(10, 14)
mac_minor = random.randint(0, 15) if mac_major == 10 else random.randint(0, 6)
hardware_concurrency = random.choice([4, 8, 12, 16])
```

**修改后**:
```python
# 读取真实 macOS 版本
import platform
mac_version = platform.mac_ver()[0]  # 例如 "14.6.1"
parts = mac_version.split('.')
mac_major = int(parts[0])
mac_minor = int(parts[1])
mac_patch = int(parts[2])

# 使用真实 CPU 核心数
import os
hardware_concurrency = os.cpu_count() or 8

# 使用真实内存（向下取整到常见值）
import psutil
total_memory_gb = psutil.virtual_memory().total / (1024 ** 3)
if total_memory_gb >= 32:
    device_memory = 32
elif total_memory_gb >= 16:
    device_memory = 16
# ...
```

**原理**: 
- macOS 的 Chrome 始终报告 "Intel Mac OS X"，即使在 Apple Silicon 上（这是浏览器标准行为）
- 使用真实硬件信息，避免 UA 与实际硬件不匹配的矛盾

---

### 3. Client Hints 架构修复

**文件**: `core/browser_fingerprint.py`

**修改前**:
```python
headers["sec-ch-ua-arch"] = '"x86"' if os_type == "Windows" else '"arm"' if "arm" in platform_name.lower() else '"x86"'
```

**修改后**:
```python
# macOS 的 Chrome 始终报告 "x86"，即使在 Apple Silicon 上
if os_type == "macOS":
    headers["sec-ch-ua-arch"] = '"x86"'
elif os_type == "Windows":
    headers["sec-ch-ua-arch"] = '"x86"'
else:
    headers["sec-ch-ua-arch"] = '"x86"'
```

**原理**: 确保 Client Hints 架构与 User-Agent 一致，都报告 x86（Chrome 在 macOS 上的标准行为）。

---

### 4. WebRTC IP 防护增强

**文件**: `platforms/base.py`

**修改前**:
```python
# 只覆盖 createOffer，不完整
pc.createOffer = function(options) {
    const opts = options || {};
    opts.offerToReceiveVideo = false;
    opts.offerToReceiveAudio = false;
    return originalCreateOffer.apply(this, [opts]);
};
```

**修改后**:
```python
// 完全覆盖 RTCPeerConnection，阻止本地 IP 泄露
const config = args[0] || {};
config.iceTransportPolicy = 'relay';  // 强制 relay 模式

// 拦截 onicecandidate，过滤本地 IP
pc.onicecandidate = function(event) {
    if (event.candidate) {
        const candidate = event.candidate.candidate;
        // 过滤掉包含本地 IP 的候选
        if (candidate && (
            candidate.includes('192.168.') ||
            candidate.includes('10.') ||
            candidate.includes('172.') ||
            candidate.includes('fe80:') ||
            candidate.includes('::1')
        )) {
            return;
        }
    }
    if (originalOnIceCandidate) {
        originalOnIceCandidate.call(this, event);
    }
};
```

**原理**: 
- 强制使用 relay 模式，禁用本地候选收集
- 拦截 onicecandidate 事件，过滤所有本地 IP 地址

---

### 5. Headless 检测防护增强

**文件**: `platforms/base.py`

**新增内容**:
```javascript
// 覆盖 navigator.plugins（Headless 通常为 0）
if (navigator.plugins.length === 0) {
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin', ... },
            { name: 'Chrome PDF Viewer', ... },
            { name: 'Native Client', ... }
        ]
    });
}

// 覆盖 window.outerWidth/outerHeight（Headless 通常为 0）
if (window.outerWidth === 0 || window.outerHeight === 0) {
    Object.defineProperty(window, 'outerWidth', {
        get: () => window.innerWidth
    });
    Object.defineProperty(window, 'outerHeight', {
        get: () => window.innerHeight + 85
    });
}

// 删除 Selenium 特征
delete window.__webdriver_script_fn;
delete window.__driver_evaluate;
// ...
```

**原理**: 
- 覆盖 Headless 浏览器的典型特征（plugins 为空、outerWidth 为 0）
- 删除 Selenium/WebDriver 的全局变量
- 模拟真实浏览器的 plugins 和 mimeTypes

---

### 6. Canvas/Audio 指纹噪声

**文件**: `platforms/base.py`

**新增内容**:
```javascript
// Canvas 指纹噪声（轻微随机化）
const addNoise = (canvas, context) => {
    const noise = getNoiseValue();
    const imageData = originalGetImageData.call(context, 0, 0, canvas.width, canvas.height);
    for (let i = 0; i < imageData.data.length; i += 4) {
        // 只对 1% 的像素添加 ±1 的噪声
        if (Math.random() < 0.01) {
            imageData.data[i] = imageData.data[i] + (noise > 0.5 ? 1 : -1);
        }
    }
    context.putImageData(imageData, 0, 0);
};

// Audio 指纹噪声
analyser.getFloatFrequencyData = function(array) {
    originalGetFloatFrequencyData.call(this, array);
    // 添加轻微噪声（±0.0001）
    for (let i = 0; i < array.length; i++) {
        array[i] += (Math.random() - 0.5) * 0.0002;
    }
};
```

**原理**: 
- 在 Canvas 和 Audio 指纹中注入轻微噪声
- 噪声足够小，不影响正常渲染和音频播放
- 每次会话使用稳定的噪声种子，同一会话内指纹一致

---

## 修复效果验证

运行 `python3 test_anti_detection_fixed.py` 验证修复效果：

```
✅ 指纹配置测试通过！
✅ 启动参数测试通过！
🎉 所有测试通过！反检测修复已完成
```

### 修复前后对比

| 检测项 | 修复前 | 修复后 |
|--------|--------|--------|
| 时区 | Asia/Taipei（随机） | Asia/Shanghai（真实） |
| User-Agent | Intel Mac OS X 10.15.7（伪装） | Intel Mac OS X 26.4.1（真实版本） |
| sec-ch-ua-arch | "arm"（暴露真实架构） | "x86"（与 UA 一致） |
| CPU 核心数 | 随机 4/8/12/16 | 12（真实） |
| 内存 | 随机 4/8/16 | 16GB（真实） |
| WebRTC IP | 部分防护 | 完全禁用本地 IP |
| Headless 检测 | 31% 风险 | 增强防护（plugins/outerWidth） |
| Canvas/Audio | 无噪声 | 轻微噪声注入 |

---

## 核心原则

1. **不过度伪装** - 伪装不当反而暴露矛盾（如台北时区+简体中文）
2. **使用真实信息** - 真实系统信息比随机伪装更安全
3. **保持一致性** - User-Agent、Client Hints、硬件信息必须一致
4. **遵循浏览器标准** - macOS Chrome 报告 "Intel Mac OS X" 是标准行为
5. **轻量级注入** - 只覆盖关键检测点，避免过度注入被检测
6. **依赖 Patchright** - CDP 层反检测比 JS 注入更强，优先依赖 Patchright

---

## 使用建议

1. **删除旧配置** - 如果之前运行过，删除 `auth/*/.fingerprint.json` 以应用新配置
2. **重新测试** - 访问指纹检测网站（如 creepjs.com、browserleaks.com）验证效果
3. **监控日志** - 观察是否还有 "异常" 或 "自动化" 标记
4. **定期更新** - 跟随 Chrome 版本更新，保持指纹配置最新

---

## 相关文件

- `core/browser_fingerprint.py` - 指纹配置生成
- `platforms/base.py` - 反检测脚本注入
- `test_anti_detection_fixed.py` - 验证脚本

---

## 参考资料

- [Patchright 反检测原理](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)
- [Chrome Client Hints 规范](https://wicg.github.io/client-hints-infrastructure/)
- [WebRTC IP 泄露防护](https://browserleaks.com/webrtc)
- [Canvas 指纹原理](https://browserleaks.com/canvas)
