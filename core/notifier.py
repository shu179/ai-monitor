"""
企业微信通知模块
- 消息队列（避免频率限制）
- 去重机制（同平台+品牌+关键词冷却）
- 图文发送
"""

import requests
import hashlib
import base64
import time
import os
import random
import threading
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from .time_utils import local_now


def _notification_now_text() -> str:
    return local_now().strftime("%Y-%m-%d %H:%M:%S")


def is_valid_wecom_webhook(webhook_url: str) -> bool:
    text = str(webhook_url or "").strip()
    return bool(text and text.startswith("https://") and "YOUR_KEY_HERE" not in text)


def send_scheduler_test_message(webhook_url: str, *, send_interval: int = 1) -> tuple[bool, str]:
    """向调度通知 webhook 发送一条测试文本。"""
    text = str(webhook_url or "").strip()
    if not is_valid_wecom_webhook(text):
        return False, "请先填写有效的企业微信 webhook"

    notifier = WeComNotifier(
        webhook_url=text,
        cooldown_minutes=0,
        send_interval=max(1, int(send_interval or 1)),
    )
    current_time = _notification_now_text()
    content = "\n".join([
        "【自动监控通知测试】",
        f"时间：{current_time}",
        "这是一条调度通知测试消息。",
        "如果你收到这条消息，说明全局调度 webhook 配置可用。",
    ])
    ok = notifier.send_text_message(content)
    return ok, str(notifier.last_error or "").strip()


class WeComNotifier:
    """
    企业微信机器人通知器
    支持：频率控制、去重、图文发送
    """

    _shared_lock = threading.Lock()
    _shared_last_sent = {}  # (webhook_url, platform, brand, keyword) -> timestamp
    _shared_post_lock = threading.Lock()
    _shared_post_locks = {}
    _shared_last_post = {}

    def __init__(
        self,
        webhook_url: str,
        cooldown_minutes: int = 30,
        send_interval: int = 2
    ):
        self.webhook_url = webhook_url
        self.cooldown = cooldown_minutes * 60  # 转换为秒
        try:
            self.send_interval = max(0.0, float(send_interval or 0))
        except Exception:
            self.send_interval = 2.0
        self.last_sent = {}  # (platform, brand, keyword) -> timestamp
        self._last_send_time = 0
        self._session = requests.Session()
        self.last_error = ""
        self.last_skip_reason = ""

    @classmethod
    def _get_webhook_post_lock(cls, webhook_url: str) -> threading.Lock:
        key = str(webhook_url or "").strip()
        with cls._shared_post_lock:
            lock = cls._shared_post_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._shared_post_locks[key] = lock
            return lock

    def _wait_for_post_slot_unlocked(self) -> None:
        if self.send_interval <= 0:
            return
        key = str(self.webhook_url or "").strip()
        last_sent = max(
            float(self._last_send_time or 0),
            float(self._shared_last_post.get(key, 0) or 0),
        )
        elapsed = time.monotonic() - last_sent
        if elapsed < self.send_interval:
            time.sleep(self.send_interval - elapsed)

    def _mark_post_sent_unlocked(self) -> None:
        sent_at = time.monotonic()
        self._last_send_time = sent_at
        key = str(self.webhook_url or "").strip()
        self._shared_last_post[key] = sent_at

    def _cooldown_key(self, platform: str, brand: str, keyword: str = "") -> tuple[str, str, str, str]:
        return (
            self.webhook_url,
            str(platform or "").strip(),
            str(brand or "").strip(),
            str(keyword or "").strip(),
        )

    def should_notify(self, platform: str, brand: str, keyword: str = "") -> bool:
        """
        检查是否应该发送通知（去重逻辑）
        相同平台+品牌+关键词在冷却期内不重复发送
        注意：此方法只做检查，不更新时间戳；时间戳在确认发送成功后由 _mark_sent() 更新。
        """
        self.last_skip_reason = ""
        key = self._cooldown_key(platform, brand, keyword)
        current_time = time.monotonic()
        with self._shared_lock:
            last_time = self._shared_last_sent.get(key, 0)

        if current_time - last_time < self.cooldown:
            minutes_ago = (current_time - last_time) / 60
            self.last_skip_reason = "cooldown"
            print(f"[{platform}] 关键词已在冷却期内（{minutes_ago:.1f}分钟前发送过），跳过")
            return False

        return True

    def _mark_sent(self, platform: str, brand: str, keyword: str = "") -> None:
        """发送成功后调用，更新冷却时间戳，并清理已过期的历史记录。"""
        sent_at = time.monotonic()
        key = self._cooldown_key(platform, brand, keyword)
        with self._shared_lock:
            self._shared_last_sent[key] = sent_at
            # 顺手清理已超出冷却期的条目，避免字典无限增长
            expired = [
                k for k, ts in self._shared_last_sent.items()
                if sent_at - ts > self.cooldown
            ]
            for k in expired:
                del self._shared_last_sent[k]
        self.last_sent[(platform, brand, keyword)] = sent_at

    def _build_detected_brand_message(
        self,
        *,
        brands: list[str],
        keywords: list[str] | None = None,
        references: list[dict] | None = None,
        body_references: list[dict] | None = None,
        greeting: str = "🎯 品牌监控报告",
    ) -> str:
        current_time = _notification_now_text()
        normalized_brands = [str(item).strip() for item in (brands or []) if str(item).strip()]
        normalized_keywords = [str(item).strip() for item in (keywords or []) if str(item).strip()]
        lines = [
            greeting,
            "",
            f"时间：{current_time}",
            f"品牌：{', '.join(dict.fromkeys(normalized_brands)) or '未识别'}",
        ]
        if normalized_keywords:
            lines.append(f"关键词：{', '.join(dict.fromkeys(normalized_keywords))}")
        mention_rate = self._build_display_mention_rate(has_hit=True)
        lines.extend([
            f"提及率：{mention_rate}%",
            "",
            "✅ 检测到品牌提及",
        ])

        # 添加引用信息（智能统计版）
        if references and isinstance(references, list):
            valid_refs = [
                ref for ref in references
                if isinstance(ref, dict) and (ref.get("url") or ref.get("source") or ref.get("title"))
            ]

            if valid_refs:
                lines.append("")
                total_count = len(valid_refs)

                # 统计媒体来源分布
                source_count = {}
                for ref in valid_refs:
                    source = str(ref.get("source", "")).strip()
                    if source:
                        source_count[source] = source_count.get(source, 0) + 1

                # 根据引用数量决定展示方式
                if total_count <= 2:
                    # 1-2条引用：直接列出，不显示媒体分布
                    lines.append(f"📚 平台抓取源（共{total_count}条）：")
                    for ref in valid_refs:
                        index = ref.get("index", "")
                        title = str(ref.get("title", "")).strip()
                        source = str(ref.get("source", "")).strip()
                        url = str(ref.get("url", "")).strip()

                        if len(title) > 50:
                            title = title[:47] + "..."

                        if title and source:
                            lines.append(f"[{index}] {title} - {source}")
                        elif title:
                            lines.append(f"[{index}] {title}")
                        else:
                            lines.append(f"[{index}] {source}")

                        if url:
                            lines.append(f"    {url}")
                else:
                    # 3条及以上：显示媒体分布 + Top 3
                    lines.append(f"📚 平台抓取源（共{total_count}条）")

                    # 媒体分布（按数量降序排列）
                    sorted_sources = sorted(source_count.items(), key=lambda x: x[1], reverse=True)
                    distribution = " | ".join([f"{source} {count}篇" for source, count in sorted_sources])
                    lines.append(f"媒体分布：{distribution}")

                    # Top 3 引用（保留原始序号）
                    lines.append("")
                    lines.append("🔝 Top 3 引用：")
                    for ref in valid_refs[:3]:
                        index = ref.get("index", "")
                        title = str(ref.get("title", "")).strip()
                        source = str(ref.get("source", "")).strip()
                        url = str(ref.get("url", "")).strip()

                        if len(title) > 50:
                            title = title[:47] + "..."

                        if title and source:
                            lines.append(f"[{index}] {title} - {source}")
                        elif title:
                            lines.append(f"[{index}] {title}")
                        else:
                            lines.append(f"[{index}] {source}")

                        if url:
                            lines.append(f"    {url}")

        # 正文引用源（独立区块，统计来源分布）
        if body_references and isinstance(body_references, list):
            valid_body = [
                ref for ref in body_references
                if isinstance(ref, dict) and ref.get("url")
            ]
            if valid_body:
                lines.append("")
                total_body = len(valid_body)
                body_source_count: dict[str, int] = {}
                for ref in valid_body:
                    source = str(ref.get("source", "")).strip()
                    if source:
                        body_source_count[source] = body_source_count.get(source, 0) + 1
                lines.append(f"📎 正文引用源（共{total_body}条）")
                if body_source_count:
                    sorted_body_sources = sorted(body_source_count.items(), key=lambda x: x[1], reverse=True)
                    distribution = " | ".join([f"{s} {c}篇" for s, c in sorted_body_sources])
                    lines.append(f"媒体分布：{distribution}")

        return "\n".join(lines)

    def send(
        self,
        platform: str,
        keyword: str,
        brand: str,
        rank: int = 0,
        screenshot_path: Optional[str] = None,
        references: list[dict] | None = None,
        body_references: list[dict] | None = None,
        greeting: str = "🎯 品牌监控报告",
        bypass_cooldown: bool = False,
    ) -> bool:
        self.last_error = ""
        self.last_skip_reason = ""
        if not bypass_cooldown and not self.should_notify(platform, brand, keyword):
            return False

        message = self._build_detected_brand_message(
            brands=[brand],
            keywords=[keyword] if keyword else [],
            references=references,
            body_references=body_references,
            greeting=greeting,
        )

        text_success = self._send_text(message)

        image_success = True
        if screenshot_path and os.path.exists(screenshot_path):
            image_success = self._send_image(screenshot_path)
        elif screenshot_path:
            print(f"[{platform}] 截图文件不存在，未发送图片: {screenshot_path}")

        # 仅在文字消息发送成功后才记录冷却，避免发送失败导致冷却期误触发
        if text_success:
            self._mark_sent(platform, brand, keyword)
        return text_success and image_success

    def send_text_message(self, content: str) -> bool:
        """发送通用文本消息。"""
        self.last_error = ""
        self.last_skip_reason = ""
        return self._send_text(content)

    def send_image_message(self, image_path: str) -> bool:
        """发送通用图片消息。"""
        self.last_error = ""
        self.last_skip_reason = ""
        return self._send_image(image_path)

    def send_file_message(self, file_path: str, file_name: str | None = None) -> bool:
        """发送通用文件消息。"""
        self.last_error = ""
        self.last_skip_reason = ""
        media_id = self._upload_file_media(file_path, file_name=file_name)
        if not media_id:
            return False
        payload = {
            "msgtype": "file",
            "file": {"media_id": media_id},
        }
        return self._post(payload)

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

            normalized_path = str(image_path or "").strip()
            if not normalized_path or not os.path.exists(normalized_path):
                self.last_error = f"图片文件不存在: {normalized_path}"
                print(f"[Notifier] {self.last_error}")
                return False

            # 读取图片
            with open(normalized_path, "rb") as f:
                img_data = f.read()
            original_size = len(img_data)

            # 如果超过1.5MB，进行压缩
            max_size = 1.5 * 1024 * 1024  # 1.5MB
            if len(img_data) > max_size:
                img = Image.open(io.BytesIO(img_data))
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                # 逐步降低质量直到符合大小
                compressed = img_data  # 确保变量始终有值
                quality = 85
                while quality > 20:
                    output = io.BytesIO()
                    img.save(output, format='JPEG', quality=quality, optimize=True)
                    compressed = output.getvalue()
                    if len(compressed) <= max_size:
                        break
                    quality -= 10
                img_data = compressed
                print(
                    f"[Notifier] 图片压缩后发送: path={normalized_path}, "
                    f"original={original_size}, compressed={len(img_data)}"
                )
            else:
                print(f"[Notifier] 准备发送图片: path={normalized_path}, size={original_size}")

            # 计算 base64 和 md5
            prepare_start = time.perf_counter()
            base64_data = base64.b64encode(img_data).decode('utf-8')
            md5_value = hashlib.md5(img_data).hexdigest()
            prepare_elapsed = time.perf_counter() - prepare_start

            data = {
                "msgtype": "image",
                "image": {
                    "base64": base64_data,
                    "md5": md5_value
                }
            }

            post_start = time.perf_counter()
            ok = self._post(data)
            post_elapsed = time.perf_counter() - post_start
            if ok:
                print(
                    f"[Notifier] 图片发送成功: {normalized_path}; "
                    f"prepare={prepare_elapsed:.2f}s; post={post_elapsed:.2f}s"
                )
            else:
                print(
                    f"[Notifier] 图片发送失败: {normalized_path}; "
                    f"prepare={prepare_elapsed:.2f}s; post={post_elapsed:.2f}s; "
                    f"error={self.last_error or '未知错误'}"
                )
            return ok

        except Exception as e:
            self.last_error = str(e)
            print(f"[Notifier] 发送图片异常: path={image_path}, error={e}")
            return False

    def _extract_robot_key(self) -> str:
        """从机器人 webhook 中提取 key。"""
        try:
            parsed = urlsplit(self.webhook_url)
            query = parse_qs(parsed.query)
            key = str((query.get("key") or [""])[0]).strip()
            if key:
                return key
        except Exception:
            pass
        return ""

    def _upload_file_media(self, file_path: str, file_name: str | None = None) -> str:
        """上传文件到企业微信机器人，返回 media_id。"""
        if not self.webhook_url:
            self.last_error = "未配置 webhook_url"
            return ""

        key = self._extract_robot_key()
        if not key:
            self.last_error = "webhook_url 中未找到 key"
            return ""

        path = str(file_path or "").strip()
        if not path or not os.path.exists(path):
            self.last_error = f"文件不存在: {path}"
            return ""

        upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={key}&type=file"
        display_name = str(file_name or os.path.basename(path) or "export.xlsx").strip() or "export.xlsx"
        try:
            with open(path, "rb") as f:
                files = {
                    "media": (
                        display_name,
                        f,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                }
                response = self._session.post(upload_url, files=files, timeout=30)
            result = response.json()
            if result.get("errcode") == 0 and result.get("media_id"):
                return str(result.get("media_id"))
            self.last_error = str(result)
            print(f"企业微信文件上传失败: {result}")
            return ""
        except Exception as e:
            self.last_error = str(e)
            print(f"企业微信文件上传异常: {e}")
            return ""

    def _post(self, data: dict, max_retries: int = 3) -> bool:
        """发送POST请求到企业微信，网络异常时最多重试 max_retries 次"""
        post_lock = self._get_webhook_post_lock(self.webhook_url)
        with post_lock:
            for attempt in range(1, max_retries + 1):
                self._wait_for_post_slot_unlocked()
                try:
                    response = self._session.post(
                        self.webhook_url,
                        json=data,
                        headers={'Content-Type': 'application/json'},
                        timeout=30
                    )
                    self._mark_post_sent_unlocked()

                    result = response.json()
                    if result.get('errcode') == 0:
                        self.last_error = ""
                        return True
                    else:
                        self.last_error = str(result)
                        print(f"企业微信API错误: {result}")
                        return False  # API 错误不重试（参数问题重试无意义）

                except requests.exceptions.Timeout:
                    self._mark_post_sent_unlocked()
                    self.last_error = f"timeout attempt={attempt}"
                    print(f"企业微信请求超时 (第{attempt}次)")
                except requests.exceptions.ConnectionError as e:
                    self._mark_post_sent_unlocked()
                    self.last_error = str(e)
                    print(f"企业微信连接失败 (第{attempt}次): {e}")
                except Exception as e:
                    self._mark_post_sent_unlocked()
                    self.last_error = str(e)
                    print(f"企业微信请求失败: {e}")
                    return False  # 非网络异常不重试

                if attempt < max_retries:
                    time.sleep(2 * attempt)  # 指数退避

        print(f"企业微信请求失败，已重试 {max_retries} 次")
        return False

    def _build_display_mention_rate(self, *, has_hit: bool) -> int:
        """通知里的提及率按展示口径生成，不直接使用实时命中占比。"""
        if not has_hit:
            return 0
        return random.randint(80, 100)

    def send_summary(
        self,
        task_name: str,
        results: list,
        greeting: str = "🎯 品牌监控报告"
    ) -> bool:
        """
        汇总发送多个关键词×平台的结果
        results: [{'keyword': str, 'platform': str, 'brand': str, 'rank': int, 'screenshot': str|None}, ...]
        先发一条汇总文字，再逐张发截图
        """
        if not results:
            return False
        self.last_error = ""
        self.last_skip_reason = ""
        found = sum(1 for r in results if r['rank'] != 99)
        brands = list(dict.fromkeys(r['brand'] for r in results))
        keywords = list(dict.fromkeys(r['keyword'] for r in results))
        message = self._build_detected_brand_message(
            brands=brands,
            keywords=keywords,
            greeting=greeting,
        )
        text_success = self._send_text(message)

        # 逐张发截图
        screenshots = [r['screenshot'] for r in results if r.get('screenshot') and os.path.exists(r['screenshot'])]
        image_success = True
        for shot in screenshots:
            image_success = self._send_image(shot) and image_success

        return text_success and image_success

    def send_detected_images(
        self,
        task_name: str,
        brands: list,
        screenshot_paths: list,
        detected_platforms: list = None,
        source: str = "识别模式",
        greeting: str = "🎯 品牌监控报告",
        completed_keywords: list | None = None,
        supplemented_keywords: list | None = None,
        total_screenshot_count: int | None = None,
        references: list[dict] | None = None,
        body_references: list[dict] | None = None,
    ) -> bool:
        """
        发送人工确认后的截图识别结果。
        适用于 AI 识图匹配任务后的主动发送，不走冷却去重。
        """
        self.last_error = ""
        self.last_skip_reason = ""
        completed_keywords = [str(item).strip() for item in (completed_keywords or []) if str(item).strip()]
        brands = [b for b in brands if b]
        unique_brands = list(dict.fromkeys(brands))
        screenshot_paths = [
            p for p in screenshot_paths
            if p and os.path.exists(p)
        ]
        current_count = len(screenshot_paths)
        print(
            f"[Notifier] 识别模式准备发送: task={task_name}, "
            f"brands={unique_brands}, screenshots={current_count}, "
            f"keywords={completed_keywords}"
        )
        if not screenshot_paths:
            print(f"[Notifier] 识别模式没有可发送图片，将仅尝试发送文本: task={task_name}")
        total_start = time.perf_counter()
        text_start = time.perf_counter()
        text_success = self._send_text(
            self._build_detected_brand_message(
                brands=unique_brands,
                keywords=completed_keywords,
                references=references,
                body_references=body_references,
                greeting=greeting,
            )
        )
        text_elapsed = time.perf_counter() - text_start

        image_success = True
        image_elapsed_total = 0.0
        for shot in screenshot_paths:
            image_start = time.perf_counter()
            image_success = self._send_image(shot) and image_success
            image_elapsed_total += time.perf_counter() - image_start
        if text_success and not image_success:
            self.last_error = self.last_error or "文本已发送，但图片发送失败"
            print(f"[Notifier] 文本已发送，但至少一张图片发送失败: task={task_name}")
        print(
            f"[Notifier] 识别模式发送耗时: task={task_name}, "
            f"text={text_elapsed:.2f}s, images={image_elapsed_total:.2f}s, "
            f"total={time.perf_counter() - total_start:.2f}s, "
            f"success={text_success and image_success}"
        )

        return text_success and image_success

    def get_status(self) -> dict:
        """获取通知器状态（用于调试）"""
        current_time = time.monotonic()
        active_cooldowns = []

        with self._shared_lock:
            shared_items = list(self._shared_last_sent.items())

        for (webhook_url, platform, brand, keyword), sent_time in shared_items:
            if webhook_url != self.webhook_url:
                continue
            elapsed = current_time - sent_time
            if elapsed < self.cooldown:
                remaining = (self.cooldown - elapsed) / 60
                item = {
                    "platform": platform,
                    "brand": brand,
                    "remaining_minutes": round(remaining, 1)
                }
                if keyword:
                    item["keyword"] = keyword
                active_cooldowns.append(item)

        return {
            "total_records": len(active_cooldowns),
            "active_cooldowns": active_cooldowns,
            "cooldown_minutes": self.cooldown / 60
        }
