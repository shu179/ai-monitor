"""
Kimi (Moonshot) 平台适配器
"""

import time
from typing import Tuple, Optional
from .base import BasePlatform


class KimiPlatform(BasePlatform):
    """Kimi AI 平台监控"""

    target_url = "https://kimi.moonshot.cn/"
    input_selector = "textarea"
    result_selector = ".chat-content"

    def wait_for_generation(self, timeout: int = 60) -> str:
        """等待Kimi生成完成"""
        start_time = time.time()
        last_length = 0
        stable_count = 0

        while time.time() - start_time < timeout:
            # 获取当前内容长度
            content = self.page.evaluate(
                "() => document.querySelector('.chat-content')?.innerText || ''"
            ) or ""

            current_length = len(content)

            if current_length == last_length and current_length > 50:
                stable_count += 1
                if stable_count >= 3:  # 连续3次稳定认为完成
                    break
            else:
                stable_count = 0
                last_length = current_length

            time.sleep(1)

        return self.page.evaluate("() => document.body.innerText") or ""

    def search(self, keyword: str, brand: str, max_retries: int = 5) -> Tuple[int, Optional[str]]:
        """执行搜索"""
        self.ensure_logged_in(timeout=15)

        best_rank = 99
        best_screenshot = None

        for attempt in range(1, max_retries + 1):
            print(f"\n[{self.name}] 尝试 {attempt}/{max_retries}: '{keyword}' -> 查找 '{brand}'")

            try:
                self.type_like_human(keyword)
                self.page.keyboard.press("Enter")

                self.wait_for_generation(timeout=60)

                page_text = self.page.evaluate("() => document.body.innerText") or ""
                rank = self.parse_ranking(page_text, brand)

                if rank <= 3:
                    print(f"[{self.name}] ✅ 命中目标！排名: {rank}")
                    time.sleep(0.5)
                    screenshot_path = self.take_screenshot(rank)
                    return rank, screenshot_path

                if rank < best_rank:
                    best_rank = rank
                    best_screenshot = self.take_screenshot(rank)

                if attempt < max_retries:
                    self.page.reload()
                    time.sleep(3)
                    self.page.wait_for_selector(self.input_selector, timeout=10000)

            except Exception as e:
                print(f"[{self.name}] 搜索出错: {e}")
                if attempt < max_retries:
                    self.page.reload()
                    time.sleep(3)

        return best_rank, best_screenshot
