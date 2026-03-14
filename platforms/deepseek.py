"""
DeepSeek 平台适配器
特点：流式输出，需要检测生成完成状态
"""

import time
from typing import Tuple, Optional
from .base import BasePlatform


class DeepSeekPlatform(BasePlatform):
    """DeepSeek AI 平台监控"""

    target_url = "https://chat.deepseek.com/"
    input_selector = "textarea"
    result_selector = ".ds-markdown-content"

    def wait_for_generation(self, timeout: int = 60) -> str:
        """
        等待DeepSeek生成完成
        通过检测停止按钮是否消失来判断
        """
        start_time = time.time()
        check_count = 0

        while time.time() - start_time < timeout:
            # DeepSeek的流式生成可以通过以下方式检测：
            # 1. 检查是否有"停止生成"按钮
            # 2. 检查内容是否稳定

            try:
                # 查找停止按钮（生成中时存在）
                stop_button = self.page.locator('button:has-text("停止")').first
                is_generating = stop_button.is_visible(timeout=500)

                if not is_generating:
                    check_count += 1
                    # 连续两次检测不到停止按钮，认为生成完成
                    if check_count >= 2:
                        time.sleep(0.5)
                        break
                else:
                    check_count = 0

            except:
                # 找不到停止按钮，可能已经生成完成
                check_count += 1
                if check_count >= 3:
                    break

            time.sleep(0.5)

        # 返回页面文本
        return self.page.evaluate("() => document.body.innerText") or ""

    def search(self, keyword: str, brand: str, max_retries: int = 5) -> Tuple[int, Optional[str]]:
        """执行搜索"""
        self.ensure_logged_in(timeout=15)

        best_rank = 99
        best_screenshot = None

        for attempt in range(1, max_retries + 1):
            print(f"\n[{self.name}] 尝试 {attempt}/{max_retries}: '{keyword}' -> 查找 '{brand}'")

            try:
                # 输入关键词
                self.type_like_human(keyword)
                self.page.keyboard.press("Enter")

                # 等待生成完成
                self.wait_for_generation(timeout=60)

                # 获取结果文本
                page_text = self.page.evaluate("() => document.body.innerText") or ""

                # 解析排名
                rank = self.parse_ranking(page_text, brand)

                if rank <= 3:
                    print(f"[{self.name}] ✅ 命中目标！排名: {rank}")
                    time.sleep(0.5)
                    screenshot_path = self.take_screenshot(rank)
                    return rank, screenshot_path

                # 记录最佳排名
                if rank < best_rank:
                    best_rank = rank
                    best_screenshot = self.take_screenshot(rank)
                    print(f"[{self.name}] 当前排名: {rank}，已截图")

                # 重试
                if attempt < max_retries:
                    print(f"[{self.name}] 准备重试...")
                    self.page.reload()
                    time.sleep(3)
                    self.page.wait_for_selector(self.input_selector, timeout=10000)

            except Exception as e:
                print(f"[{self.name}] 搜索出错: {e}")
                if attempt < max_retries:
                    self.page.reload()
                    time.sleep(3)

        return best_rank, best_screenshot
