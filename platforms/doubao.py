"""
豆包平台适配器
基于 doubao.json 迁移，继承 BasePlatform
"""

import time
from typing import Tuple, Optional
from .base import BasePlatform


class DoubaoPlatform(BasePlatform):
    """豆包 AI 平台监控"""

    target_url = "https://www.doubao.com/chat/"
    input_selector = "textarea"

    def search(self, keyword: str, brand: str, max_retries: int = 5) -> Tuple[int, Optional[str]]:
        """
        执行搜索，返回 (rank, screenshot_path)
        复用 doubao.json 的核心流程
        """

        # 确保已登录
        self.ensure_logged_in(timeout=15)

        best_rank = 99
        best_screenshot = None

        for attempt in range(1, max_retries + 1):
            print(f"\n[{self.name}] 尝试 {attempt}/{max_retries}: '{keyword}' -> 查找 '{brand}'")

            try:
                # 1. 输入关键词（模拟人工）
                self.type_like_human(keyword)
                self.page.keyboard.press("Enter")

                # 2. 等待生成结果
                start_time = time.time()
                found = False

                while time.time() - start_time < 50:  # 最多等待50秒
                    # 滚动到底部查看最新内容
                    self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1)

                    # 获取页面文本
                    page_text = self.page.evaluate("() => document.body.innerText") or ""

                    # 解析排名
                    rank = self.parse_ranking(page_text, brand)

                    if rank <= 3:
                        print(f"[{self.name}] ✅ 命中目标！排名: {rank}")
                        time.sleep(1)  # 稍微等待页面稳定

                        # 截图
                        screenshot_path = self.take_screenshot(rank)

                        return rank, screenshot_path

                    # 如果排名比之前的更好，记录下来
                    if rank < best_rank:
                        best_rank = rank
                        best_screenshot = self.take_screenshot(rank)
                        print(f"[{self.name}] 发现更好排名: {rank}，已截图")

                    time.sleep(2)  # 等待继续生成

                # 3. 未在前3，准备重试
                print(f"[{self.name}] 本次未进入前3，准备重试...")

                if attempt < max_retries:
                    self.page.reload()
                    time.sleep(5)
                    # 重新等待输入框
                    self.page.wait_for_selector(self.input_selector, timeout=10000)

            except Exception as e:
                print(f"[{self.name}] 搜索出错: {e}")
                if attempt < max_retries:
                    self.page.reload()
                    time.sleep(5)

        # 返回最佳结果（即使不在前3）
        print(f"[{self.name}] 重试结束，最佳排名: {best_rank}")
        return best_rank, best_screenshot


# 兼容性：保留原有的函数接口
def run_doubao_monitor():
    """
    兼容原有 doubao.json 的调用方式
    """
    import os

    USER_DATA_DIR = os.path.join(os.getcwd(), "user_data", "doubao")

    with DoubaoPlatform(USER_DATA_DIR) as platform:
        # 这里需要配置实际的关键词和品牌
        # 实际使用时应从配置文件读取
        KEYWORD = "武汉geo优化公司"
        BRAND = "即搜AI"

        rank, screenshot = platform.search(KEYWORD, BRAND)

        if rank <= 3 and screenshot:
            # 发送企业微信通知
            from core.notifier import WeComNotifier

            notifier = WeComNotifier("YOUR_WEBHOOK_URL")
            notifier.send(platform.name, KEYWORD, BRAND, rank, screenshot)


if __name__ == "__main__":
    run_doubao_monitor()
