"""
平台基类 - 提供通用的浏览器操作和排名解析逻辑
基于 doubao.json 的模式抽象
"""

import os
import re
import time
import random
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Tuple


class BasePlatform(ABC):
    """AI平台监控基类"""

    # 子类必须定义
    target_url: str = ""
    input_selector: str = "textarea"
    result_selector: str = "body"

    def __init__(self, user_data_dir: str):
        self.name = self.__class__.__name__.replace("Platform", "").lower()
        self.user_data_dir = user_data_dir
        self.context = None
        self.page = None
        self._playwright = None

    def start(self) -> "BasePlatform":
        """启动浏览器（复用 doubao.json 的 persistent context 模式）"""
        from playwright.sync_api import sync_playwright
        from playwright_stealth import stealth

        # 确保用户数据目录存在
        if not os.path.exists(self.user_data_dir):
            os.makedirs(self.user_data_dir)

        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=False,
            no_viewport=True,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        stealth(self.page)

        # 访问目标页面
        self.page.goto(self.target_url)
        print(f"[{self.name}] 浏览器已启动，访问: {self.target_url}")

        return self

    def ensure_logged_in(self, timeout: int = 15) -> bool:
        """
        检查是否已登录（通过检测输入框是否存在）
        如果未登录，等待用户手动登录
        """
        try:
            self.page.wait_for_selector(self.input_selector, timeout=timeout * 1000)
            print(f"[{self.name}] 已登录，输入框可用")
            return True
        except:
            print(f"[{self.name}] 未检测到输入框，请手动登录...")
            # 等待用户手动登录
            self.page.wait_for_selector(self.input_selector, timeout=0)
            return True

    def type_like_human(self, text: str) -> None:
        """
        模拟人类输入（复用 doubao.json 的模式）
        - 清空现有内容
        - 逐个字符输入，带随机延迟
        """
        chat_input = self.page.locator(self.input_selector).first
        chat_input.click()

        # 清空现有内容（全选+删除）
        self.page.keyboard.press("Meta+A")
        self.page.keyboard.press("Backspace")
        time.sleep(0.2)

        # 逐个输入，模拟人工
        for char in text:
            chat_input.type(char, delay=random.randint(50, 100))

    def parse_ranking(self, text: str, brand: str) -> int:
        """
        解析品牌在搜索结果中的排名（复用 doubao.json 的核心逻辑）

        支持的序号格式：
        - 1. / 2. / 3.
        - 1、 / 2、 / 3、
        - (1) / (2) / (3)
        - ① / ② / ③
        - 一、 / 二、 / 三、

        返回: 排名数字（1-based），99表示未找到
        """
        if not text or not brand:
            return 99

        # 分割关键词后的内容（通常推荐在关键词之后）
        # 但为了通用性，直接分析全文
        clean_text = re.sub(r'[*_#`\-\[\]]', '', text).replace(" ", "")
        lines = re.split(r'\n+', clean_text)

        current_rank = 1
        brand_lower = brand.lower()

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 匹配各种序号格式
            is_numbered = bool(re.match(
                r'^(\d+[\.、\s]|\(\d+\)|[①-⑩]|[一二三四五六七八九十]+[、\s])',
                line
            ))

            if is_numbered:
                if brand_lower in line.lower():
                    return current_rank
                current_rank += 1

        # 如果没有明确的序号，但全文包含品牌，默认给第2名
        # 这是一个启发式策略，表示品牌被提及但不是明确排名
        if brand_lower in clean_text.lower():
            return 2

        return 99

    def wait_for_generation(self, timeout: int = 60) -> str:
        """
        等待AI回答生成完成
        基础实现：简单等待 + 返回页面文本
        子类可以覆盖以适配特定平台的流式输出检测
        """
        time.sleep(2)  # 初始等待

        start_time = time.time()
        last_text = ""
        stable_count = 0

        while time.time() - start_time < timeout:
            current_text = self.page.evaluate("() => document.body.innerText") or ""

            # 检测文本是否稳定（连续3次相同认为生成完成）
            if current_text == last_text and len(current_text) > 50:
                stable_count += 1
                if stable_count >= 3:
                    break
            else:
                stable_count = 0
                last_text = current_text

            time.sleep(1)

        return last_text

    def take_screenshot(self, rank: int, quality: int = 85) -> str:
        """
        截取当前页面，保存为JPG格式（控制文件大小）

        返回: 截图文件路径
        """
        from PIL import Image
        import io

        ts = datetime.now().strftime("%m%d_%H%M%S")
        filename = f"screenshots/{self.name}_Rank{rank}_{ts}.jpg"

        # 确保截图目录存在
        os.makedirs("screenshots", exist_ok=True)

        # Playwright截图为PNG
        png_bytes = self.page.screenshot(type="png")

        # 转换为JPG并压缩
        img = Image.open(io.BytesIO(png_bytes))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')

        # 如果图片太大，调整尺寸
        max_width = 1920
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

        # 保存JPG，控制质量
        img.save(filename, format='JPEG', quality=quality, optimize=True)

        print(f"[{self.name}] 截图已保存: {filename}")
        return filename

    @abstractmethod
    def search(self, keyword: str, brand: str, max_retries: int = 5) -> Tuple[int, Optional[str]]:
        """
        执行搜索并返回排名和截图路径

        参数:
            keyword: 搜索关键词
            brand: 要监控的品牌名称
            max_retries: 最大重试次数

        返回:
            (rank, screenshot_path) - rank为99表示未找到
        """
        pass

    def close(self):
        """关闭浏览器上下文"""
        if self.context:
            self.context.close()
        if self._playwright:
            self._playwright.stop()
        print(f"[{self.name}] 浏览器已关闭")

    def __enter__(self):
        """上下文管理器支持"""
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器支持"""
        self.close()
        return False
