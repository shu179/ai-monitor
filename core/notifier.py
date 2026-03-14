"""
企业微信通知模块
- 消息队列（避免频率限制）
- 去重机制（同排名冷却）
- 图文发送
"""

import requests
import hashlib
import base64
import time
import os
from datetime import datetime
from typing import Optional
from pathlib import Path


class WeComNotifier:
    """
    企业微信机器人通知器
    支持：频率控制、去重、图文发送
    """

    def __init__(
        self,
        webhook_url: str,
        cooldown_minutes: int = 30,
        send_interval: int = 2
    ):
        self.webhook_url = webhook_url
        self.cooldown = cooldown_minutes * 60  # 转换为秒
        self.send_interval = send_interval
        self.last_sent = {}  # (platform, brand, rank) -> timestamp
        self._last_send_time = 0

    def should_notify(self, platform: str, brand: str, rank: int) -> bool:
        """
        检查是否应该发送通知（去重逻辑）
        相同平台+品牌+排名在冷却期内不重复发送
        """
        key = (platform, brand, rank)
        last_time = self.last_sent.get(key, 0)
        current_time = time.time()

        if current_time - last_time < self.cooldown:
            minutes_ago = (current_time - last_time) / 60
            print(f"[{platform}] 排名{rank}已在冷却期内（{minutes_ago:.1f}分钟前发送过），跳过")
            return False

        # 更新发送时间
        self.last_sent[key] = current_time
        return True

    def send(
        self,
        platform: str,
        keyword: str,
        brand: str,
        rank: int,
        screenshot_path: Optional[str] = None,
        greeting: str = "🎯 品牌排名监控报告"
    ) -> bool:
        """
        发送完整通知（文字+截图）

        参数:
            platform: 平台名称
            keyword: 搜索关键词
            brand: 监控品牌
            rank: 排名
            screenshot_path: 截图文件路径
            greeting: 问候语

        返回:
            是否发送成功
        """
        if not self.should_notify(platform, brand, rank):
            return False

        # 构建文字消息
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message_lines = [
            greeting,
            "",
            f"📅 检测时间：{current_time}",
            f"🔍 检测平台：{platform}",
            f"🏷️ 监控品牌：{brand}",
        ]

        if keyword:
            message_lines.append(f"🔑 搜索关键词：{keyword}")

        message_lines.append("")

        # 排名信息
        if rank <= 3:
            message_lines.append(f"🎉 排名结果：第 {rank} 名")
            message_lines.append("✅ 品牌已进入推荐前三！")
        else:
            message_lines.append(f"📊 当前排名：第 {rank} 名")

        message = "\n".join(message_lines)

        # 发送文字消息
        text_success = self._send_text(message)

        # 发送截图（如果有）
        image_success = True
        if screenshot_path and os.path.exists(screenshot_path):
            time.sleep(self.send_interval)  # 频率控制
            image_success = self._send_image(screenshot_path)

        return text_success and image_success

    def _send_text(self, content: str) -> bool:
        """发送纯文本消息"""
        data = {
            "msgtype": "text",
            "text": {"content": content}
        }
        return self._post(data)

    def _send_image(self, image_path: str) -> bool:
        """
        发送图片消息
        自动压缩以确保在2MB限制内
        """
        try:
            from PIL import Image
            import io

            # 读取图片
            with open(image_path, "rb") as f:
                img_data = f.read()

            # 如果超过1.5MB，进行压缩
            max_size = 1.5 * 1024 * 1024  # 1.5MB
            if len(img_data) > max_size:
                img = Image.open(io.BytesIO(img_data))
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                # 逐步降低质量直到符合大小
                quality = 85
                while len(img_data) > max_size and quality > 50:
                    output = io.BytesIO()
                    img.save(output, format='JPEG', quality=quality, optimize=True)
                    img_data = output.getvalue()
                    quality -= 10

            # 计算 base64 和 md5
            base64_data = base64.b64encode(img_data).decode('utf-8')
            md5_value = hashlib.md5(img_data).hexdigest()

            data = {
                "msgtype": "image",
                "image": {
                    "base64": base64_data,
                    "md5": md5_value
                }
            }

            return self._post(data)

        except Exception as e:
            print(f"发送图片失败: {e}")
            return False

    def _post(self, data: dict) -> bool:
        """发送POST请求到企业微信"""
        try:
            # 频率控制：确保两次发送间隔
            elapsed = time.time() - self._last_send_time
            if elapsed < self.send_interval:
                time.sleep(self.send_interval - elapsed)

            response = requests.post(
                self.webhook_url,
                json=data,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )

            self._last_send_time = time.time()

            result = response.json()

            if result.get('errcode') == 0:
                return True
            else:
                print(f"企业微信API错误: {result}")
                return False

        except Exception as e:
            print(f"企业微信请求失败: {e}")
            return False

    def get_status(self) -> dict:
        """获取通知器状态（用于调试）"""
        current_time = time.time()
        active_cooldowns = []

        for (platform, brand, rank), sent_time in self.last_sent.items():
            elapsed = current_time - sent_time
            if elapsed < self.cooldown:
                remaining = (self.cooldown - elapsed) / 60
                active_cooldowns.append({
                    "platform": platform,
                    "brand": brand,
                    "rank": rank,
                    "remaining_minutes": round(remaining, 1)
                })

        return {
            "total_records": len(self.last_sent),
            "active_cooldowns": active_cooldowns,
            "cooldown_minutes": self.cooldown / 60
        }
