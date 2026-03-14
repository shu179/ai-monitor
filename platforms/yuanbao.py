"""
腾讯元宝 平台适配器
"""

import time
from typing import Tuple, Optional
from .base import BasePlatform


class YuanbaoPlatform(BasePlatform):
    """腾讯元宝 AI 平台监控"""

    target_url = "https://yuanbao.tencent.com/"
    input_selector = "textarea"
    result_selector = ".content"

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

                # 等待生成
                time.sleep(3)
                start_time = time.time()

                while time.time() - start_time < 60:
                    page_text = self.page.evaluate("() => document.body.innerText") or ""
                    rank = self.parse_ranking(page_text, brand)

                    if rank <= 3:
                        print(f"[{self.name}] ✅ 命中目标！排名: {rank}")
                        time.sleep(1)
                        screenshot_path = self.take_screenshot(rank)
                        return rank, screenshot_path

                    if rank < best_rank:
                        best_rank = rank
                        best_screenshot = self.take_screenshot(rank)

                    time.sleep(2)

                if attempt < max_retries:
                    self.page.reload()
                    time.sleep(3)

            except Exception as e:
                print(f"[{self.name}] 搜索出错: {e}")
                if attempt < max_retries:
                    self.page.reload()
                    time.sleep(3)

        return best_rank, best_screenshot
