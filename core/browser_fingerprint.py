"""
浏览器指纹管理器

为每个 user_data_dir 生成并持久化一个稳定的浏览器指纹配置，
确保长期使用时指纹一致，模拟真实用户的稳定特征。
"""

import json
import hashlib
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Any

from .file_lock import CrossProcessRLock


class BrowserFingerprint:
    """浏览器指纹配置"""

    def __init__(self, user_data_dir: str):
        self.user_data_dir = user_data_dir
        self.config_file = Path(user_data_dir) / ".fingerprint.json"
        self._lock = CrossProcessRLock(lambda: self._lock_file())
        self.config = self._load_or_generate()

    def _lock_file(self) -> Path:
        return self.config_file.with_name(f"{self.config_file.name}.lock")

    def _load_or_generate(self) -> Dict[str, Any]:
        """加载或生成指纹配置"""
        with self._lock:
            config = self._load_unlocked()
            if config:
                return config

            # 生成新的指纹配置
            config = self._generate_fingerprint()
            self._save_unlocked(config)
            return config

    def _generate_fingerprint(self) -> Dict[str, Any]:
        """生成一个稳定的指纹配置"""
        seed_source = str(Path(self.user_data_dir).expanduser())
        seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:16], 16)
        rng = random.Random(seed)

        # Chrome 版本（2026年4月最新稳定版：147）
        chrome_major = 147
        chrome_minor = 0
        chrome_patch = rng.randint(7720, 7730)
        chrome_build = rng.randint(90, 110)
        chrome_version = f"{chrome_major}.0.{chrome_patch}.{chrome_build}"

        # 操作系统版本（使用合理的版本范围）
        if sys.platform == "darwin":
            os_type = "macOS"
            # 使用更常见的公开 UA 版本段，避免新硬件/新系统组合过于唯一。
            mac_major = rng.choice([13, 14])
            mac_minor = rng.randint(0, 6)
            mac_patch = rng.randint(0, 3)
            os_version = f"{mac_major}_{mac_minor}_{mac_patch}"
            platform_name = "MacIntel"
        elif sys.platform == "win32":
            os_type = "Windows"
            os_version = rng.choice(["10.0", "11.0"])
            platform_name = "Win32"
        else:
            os_type = "Linux"
            os_version = "x86_64"
            platform_name = "Linux x86_64"

        # 使用常见桌面配置，而不是直接暴露本机硬件特征。
        hardware_concurrency, device_memory = rng.choice([
            (4, 8),
            (4, 16),
            (8, 8),
            (8, 16),
            (16, 16),
            (16, 32),
        ])

        # 屏幕分辨率：避免在 HTTP 子线程里触发 Tk/NSWindow 初始化导致 macOS 直接崩溃。
        screen_width, screen_height = self._detect_screen_resolution()

        # 语言偏好
        accept_languages = [
            "zh-CN,zh;q=0.9,en;q=0.8",
            "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "zh-CN,zh-TW;q=0.9,zh;q=0.8,en;q=0.7",
        ]
        accept_language = rng.choice(accept_languages)

        # Accept 格式
        accept_formats = [
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        ]
        accept_format = rng.choice(accept_formats)

        # DNT（Do Not Track）
        dnt = str(rng.choice([0, 1])) if rng.random() < 0.3 else None

        # 保持东八区一致，同时在常见城市间分散，避免所有账号完全同质。
        timezone_id = rng.choice(["Asia/Shanghai", "Asia/Hong_Kong"])

        # WebGL 供应商（移除 - 与启动参数保持一致，不启用 WebGL）
        # 真实场景：部分用户禁用 WebGL 以提升隐私

        # 用户偏好（LocalStorage 数据）
        user_preferences = {
            "theme": rng.choice(["light", "dark"]),
            "language": "zh-CN",
            "visited": True,
            "sessionCount": rng.randint(10, 100),  # 模拟老用户
        }

        return {
            "chrome_version": chrome_version,
            "chrome_major": chrome_major,
            "os_type": os_type,
            "os_version": os_version,
            "platform_name": platform_name,
            "hardware_concurrency": hardware_concurrency,
            "device_memory": device_memory,
            "screen_width": screen_width,
            "screen_height": screen_height,
            "accept_language": accept_language,
            "accept_format": accept_format,
            "dnt": dnt,
            "timezone_id": timezone_id,
            "user_preferences": user_preferences,
        }

    def _detect_screen_resolution(self) -> tuple[int, int]:
        """尽量读取真实分辨率；失败时回退到常见桌面分辨率。"""
        if sys.platform == "darwin":
            resolution = self._detect_macos_resolution()
            if resolution:
                return resolution
        elif sys.platform.startswith("linux"):
            resolution = self._detect_linux_resolution()
            if resolution:
                return resolution

        screen_resolutions = [
            (1920, 1080),
            (2560, 1440),
            (1680, 1050),
        ]
        return random.choice(screen_resolutions)

    def _detect_macos_resolution(self) -> tuple[int, int] | None:
        try:
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            return None

        if result.returncode != 0:
            return None

        matches = re.findall(r"Resolution:\s*(\d+)\s*x\s*(\d+)", result.stdout or "")
        if not matches:
            return None
        try:
            width, height = matches[0]
            return int(width), int(height)
        except Exception:
            return None

    def _detect_linux_resolution(self) -> tuple[int, int] | None:
        try:
            result = subprocess.run(
                ["xrandr", "--current"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception:
            return None

        if result.returncode != 0:
            return None

        match = re.search(r"current\s+(\d+)\s+x\s+(\d+)", result.stdout or "")
        if not match:
            return None
        try:
            return int(match.group(1)), int(match.group(2))
        except Exception:
            return None

    def _save(self, config: Dict[str, Any]) -> None:
        """保存指纹配置"""
        with self._lock:
            self._save_unlocked(config)

    def _load_unlocked(self) -> Dict[str, Any]:
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_unlocked(self, config: Dict[str, Any]) -> None:
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.config_file.parent),
                prefix=f".{self.config_file.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.config_file)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            print(f"[BrowserFingerprint] 保存指纹配置失败: {e}")

    def get_user_agent(self) -> str:
        """
        获取稳定的 User-Agent

        注意：macOS Chrome 的公开 UA 长期使用 "Intel Mac OS X"，
        Client Hints 中也保持 x86；这里保持两者一致，避免架构矛盾。
        """
        chrome_version = self.config["chrome_version"]
        os_type = self.config["os_type"]
        os_version = self.config["os_version"]

        if os_type == "Windows":
            return f"Mozilla/5.0 (Windows NT {os_version}; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36"
        elif os_type == "macOS":
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X {os_version}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36"
        else:
            return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36"

    def get_sec_ch_ua(self) -> str:
        """获取稳定的 Sec-Ch-Ua"""
        chrome_major = self.config["chrome_major"]
        return f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", "Not?A_Brand";v="{random.randint(8, 99)}"'

    def get_sec_ch_ua_platform(self) -> str:
        """获取稳定的 Sec-Ch-Ua-Platform"""
        os_type = self.config["os_type"]
        if os_type == "macOS":
            return '"macOS"'
        elif os_type == "Windows":
            return '"Windows"'
        else:
            return '"Linux"'

    def get_accept_language(self) -> str:
        """获取稳定的 Accept-Language"""
        return self.config["accept_language"]

    def get_accept_format(self) -> str:
        """获取稳定的 Accept 格式"""
        return self.config["accept_format"]

    def get_dnt(self) -> str | None:
        """获取稳定的 DNT"""
        return self.config.get("dnt")

    def get_hardware_concurrency(self) -> int:
        """获取稳定的 CPU 核心数"""
        return self.config["hardware_concurrency"]

    def get_device_memory(self) -> int:
        """获取稳定的设备内存"""
        return self.config["device_memory"]

    def get_screen_resolution(self) -> tuple[int, int]:
        """获取稳定的屏幕分辨率"""
        return self.config["screen_width"], self.config["screen_height"]

    def get_timezone_id(self) -> str:
        """获取稳定的时区配置"""
        return self.config["timezone_id"]

    def get_user_preferences(self) -> Dict[str, Any]:
        """获取稳定的用户偏好"""
        return self.config.get("user_preferences", {})

    def get_client_hints_headers(self) -> Dict[str, str]:
        """获取完整的 Client Hints 头部（Chrome 147 标准）"""
        chrome_version = self.config["chrome_version"]
        chrome_major = self.config["chrome_major"]
        os_type = self.config["os_type"]
        os_version = self.config["os_version"]
        platform_name = self.get_sec_ch_ua_platform().strip('"')

        # 基础 Client Hints
        headers = {
            "sec-ch-ua": self.get_sec_ch_ua(),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.get_sec_ch_ua_platform(),
        }

        # 完整版本列表（Chrome 147 格式）
        not_a_brand_version = random.randint(8, 99)
        headers["sec-ch-ua-full-version-list"] = (
            f'"Chromium";v="{chrome_version}", '
            f'"Google Chrome";v="{chrome_version}", '
            f'"Not?A_Brand";v="{not_a_brand_version}.0.0.0"'
        )

        # 平台版本
        if os_type == "macOS":
            # macOS 版本格式：14_6_1 -> 14.6.1
            platform_version = os_version.replace("_", ".")
        elif os_type == "Windows":
            # Windows 版本格式：10.0 -> 10.0.0
            platform_version = f"{os_version}.0"
        else:
            # Linux 通常不发送具体版本
            platform_version = ""

        if platform_version:
            headers["sec-ch-ua-platform-version"] = f'"{platform_version}"'

        # 架构和位数（桌面设备）
        # 注意：macOS 的 Chrome 在 Client Hints 中始终报告 "x86"，
        # 即使在 Apple Silicon 上也是如此，与 User-Agent 保持一致。
        # 这是 Chrome 的标准行为，确保 UA 和 Client Hints 不矛盾。
        if os_type == "macOS":
            headers["sec-ch-ua-arch"] = '"x86"'
        elif os_type == "Windows":
            headers["sec-ch-ua-arch"] = '"x86"'
        else:
            headers["sec-ch-ua-arch"] = '"x86"'
        headers["sec-ch-ua-bitness"] = '"64"'

        # 模型（桌面设备通常为空）
        headers["sec-ch-ua-model"] = '""'

        return headers

    def get_platform_name(self) -> str:
        """获取稳定的 platform 名称"""
        return self.config["platform_name"]

    def increment_session_count(self) -> int:
        """
        增加访问次数（模拟真实用户行为）
        每次调用 sessionCount++，并保存到配置文件
        """
        self.config["user_preferences"]["sessionCount"] += 1
        self._save(self.config)
        return self.config["user_preferences"]["sessionCount"]

    def update_last_visit(self) -> None:
        """
        更新最后访问时间（模拟真实用户行为）
        记录当前时间戳，下次启动时作为 lastVisit
        """
        import time
        self.config["user_preferences"]["lastVisit"] = int(time.time() * 1000)  # 毫秒时间戳
        self._save(self.config)
