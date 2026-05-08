"""
批量测试执行器

用于执行品牌展示率批量测试，支持多关键词×多平台×多次查询的组合测试。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .app_paths import resolve_app_path
from .batch_test_storage import merge_batch_json, write_batch_json
from .history import record as record_history
from .notifier import WeComNotifier
from .platform_sessions import PlatformSessionManager, build_query_execution_policy
from .time_utils import local_now


class BatchTestRunner:
    """批量测试执行器"""

    def __init__(self, config: dict, notifier: WeComNotifier):
        self.config = config
        self.notifier = notifier
        self._cancel_requested = False
        self._lock = threading.Lock()
        self._session_managers = {}  # platform -> PlatformSessionManager

    def run_batch_test(
        self,
        batch_config: dict,
        progress_callback: Optional[Callable] = None
    ) -> dict:
        """
        执行批量测试

        Args:
            batch_config: 批量测试配置
            progress_callback: 进度回调函数 callback(completed, total, status_text)

        Returns:
            batch_report: 批量测试报告
        """
        batch_id = batch_config["batch_id"]
        brand = batch_config["brand"]
        inspect = bool(batch_config.get("inspect", False))

        print(f"[BatchTestRunner] 开始批量测试: batch_id={batch_id}, brand={brand}, inspect={inspect}")

        # 更新状态为 running
        self._update_batch_status(batch_id, "running")

        try:
            # 1. 构建查询计划
            query_plan = self._build_query_plan(batch_config)
            total_queries = len(query_plan)

            print(f"[BatchTestRunner] 查询计划已构建: total_queries={total_queries}")
            print(f"[BatchTestRunner] 查询计划详情: {query_plan}")

            # 更新总查询数
            self._update_batch_progress(batch_id, {
                "total_queries": total_queries,
                "completed_queries": 0,
                "current_keyword": "",
                "current_platform": ""
            })

            # 2. 顺序执行每个查询
            for idx, query in enumerate(query_plan):
                # 检查是否取消
                if self._cancel_requested:
                    self._update_batch_status(batch_id, "cancelled")
                    return {"status": "cancelled", "batch_id": batch_id}

                keyword = query["keyword"]
                platform = query["platform"]
                deep_think = query.get("deep_think", False)
                query_index = query["query_index"]
                query_count = query["query_count"]

                # 更新进度
                deep_think_label = " [深度思考]" if deep_think else ""
                status_text = f"正在查询 {keyword} - {platform}{deep_think_label} ({query_index}/{query_count})"
                self._update_batch_progress(batch_id, {
                    "total_queries": total_queries,
                    "completed_queries": idx,
                    "current_keyword": keyword,
                    "current_platform": platform,
                    "status_text": status_text
                })

                if progress_callback:
                    try:
                        progress_callback(idx, total_queries, status_text)
                    except Exception:
                        pass

                # 执行单次查询
                result = self._execute_single_query(
                    keyword=keyword,
                    brand=brand,
                    platform=platform,
                    deep_think=deep_think,
                    inspect=inspect,
                    batch_id=batch_id,
                    query_index=query_index
                )

                # 记录到 history
                self._record_result(result, batch_id, query_index)

                # 短暂延迟，避免请求过快
                time.sleep(1.0)

            # 3. 生成报告
            from .batch_test_report import BatchTestReportGenerator
            report_generator = BatchTestReportGenerator()
            report = report_generator.generate_report(batch_id)

            # 保存报告
            self._save_report(batch_id, report)

            # 更新状态为 completed
            self._update_batch_status(batch_id, "completed")
            self._update_batch_progress(batch_id, {
                "total_queries": total_queries,
                "completed_queries": total_queries,
                "current_keyword": "",
                "current_platform": "",
                "status_text": "测试完成"
            })

            if progress_callback:
                try:
                    progress_callback(total_queries, total_queries, "测试完成")
                except Exception:
                    pass

            return report

        except Exception as e:
            # 更新状态为 failed
            self._update_batch_status(batch_id, "failed", error=str(e))
            raise
        finally:
            self.close()

    def cancel(self):
        """取消批量测试"""
        with self._lock:
            self._cancel_requested = True

    def close(self):
        """关闭批量测试持有的浏览器会话，释放 profile 占用。"""
        for manager in list(self._session_managers.values()):
            try:
                manager.close_all(reason="批量测试结束")
            except Exception:
                pass
        self._session_managers.clear()

    def _build_query_plan(self, batch_config: dict) -> list[dict]:
        """构建查询计划：按关键词顺序，每个关键词内按平台顺序，每个平台重复N次"""
        plan = []
        for kw_config in batch_config["keywords"]:
            keyword = str(kw_config.get("keyword") or "").strip()
            platforms = kw_config.get("platforms") or []
            deep_think_platforms = kw_config.get("deep_think_platforms") or []
            query_count = int(kw_config.get("query_count") or 1)

            if not keyword or not platforms:
                continue

            for platform in platforms:
                platform = str(platform or "").strip()
                if not platform:
                    continue

                # 判断该平台是否启用深度思考
                deep_think = platform in deep_think_platforms

                for i in range(1, query_count + 1):
                    plan.append({
                        "keyword": keyword,
                        "platform": platform,
                        "deep_think": deep_think,
                        "query_index": i,
                        "query_count": query_count
                    })

        return plan

    def _execute_single_query(
        self,
        keyword: str,
        brand: str,
        platform: str,
        deep_think: bool,
        inspect: bool,
        batch_id: str,
        query_index: int
    ) -> dict:
        """
        执行单次查询

        Returns:
            {
                "keyword": str,
                "brand": str,
                "platform": str,
                "deep_think": bool,
                "rank": int,
                "success": bool,
                "answer_text": str,
                "screenshot": str,
                "media_sources": list[dict],
                "error_message": str
            }
        """
        try:
            # 获取或创建该平台的 session manager
            if platform not in self._session_managers:
                policy = build_query_execution_policy(
                    config=self.config,
                    mode="browser"  # 批量测试默认使用 browser 模式
                )
                self._session_managers[platform] = PlatformSessionManager(
                    mode="browser",
                    policy=policy
                )

            session_manager = self._session_managers[platform]

            # 执行查询 - 使用 get_or_create 获取平台实例
            from core.browser_platform_factory import create_browser_platform

            def _create_platform_for_batch():
                browser_platform = create_browser_platform(
                    platform_name=platform,
                    config=self.config,
                    inspect=inspect,
                    stop_checker=None
                )
                if inspect:
                    setattr(browser_platform, "force_reclaim_profile_processes_on_start", True)
                return browser_platform

            platform_instance = session_manager.get_or_create(
                platform,
                _create_platform_for_batch
            )

            if not platform_instance:
                return {
                    "keyword": keyword,
                    "brand": brand,
                    "platform": platform,
                    "deep_think": deep_think,
                    "inspect": inspect,
                    "success": False,
                    "media_sources": [],
                    "error_message": f"无法创建 {platform} 平台实例"
                }

            # 设置深度思考和引用提取，禁用截图
            platform_instance.deep_think = deep_think
            platform_instance.extract_references_enabled = True
            platform_instance.screenshot_on_mention = False  # 批量测试不需要截图

            # 执行搜索，传递 max_retries=1（批量测试每次只查询1次）
            rank, _ = platform_instance.search(keyword, brand, max_retries=1, deep_think=deep_think)
            success = rank != 99

            # 提取媒体来源
            media_sources = self._extract_media_sources(platform_instance)

            return {
                "keyword": keyword,
                "brand": brand,
                "platform": platform,
                "deep_think": deep_think,
                "inspect": inspect,
                "success": success,
                "media_sources": media_sources,
                "error_message": platform_instance.last_error if not success else ""
            }

        except Exception as e:
            import traceback
            traceback.print_exc()
            return {
                "keyword": keyword,
                "brand": brand,
                "platform": platform,
                "deep_think": deep_think,
                "inspect": inspect,
                "success": False,
                "media_sources": [],
                "error_message": str(e)
            }

    def _extract_media_sources(self, platform) -> list[dict]:
        """
        从平台实例提取媒体来源

        复用现有的 extract_answer_references() 和 extract_body_references()
        """
        media_sources = []

        # 合并 last_references 和 last_body_references
        references = getattr(platform, "last_references", []) or []
        body_references = getattr(platform, "last_body_references", []) or []

        all_references = list(references) + list(body_references)

        # 去重并转换格式
        seen_urls = set()
        for ref in all_references:
            if not isinstance(ref, dict):
                continue

            url = str(ref.get("url") or "").strip()
            if not url or url in seen_urls:
                continue

            seen_urls.add(url)

            media_sources.append({
                "name": str(ref.get("source") or ref.get("title") or "").strip(),
                "url": url,
                "rank": len(media_sources) + 1
            })

        return media_sources

    def _record_result(self, result: dict, batch_id: str, query_index: int):
        """记录查询结果到 history"""
        extra = {
            "batch_id": batch_id,
            "query_index": query_index,
            "inspect": bool(result.get("inspect", False)),
            "media_sources": result.get("media_sources", [])
        }

        record_history(
            task_name=f"batch_test_{batch_id}",
            platform=result["platform"],
            keyword=result["keyword"],
            brand=result["brand"],
            rank=1 if result["success"] else 99,  # 简化：只记录是否出现
            success=result["success"],
            details={
                "error_message": result.get("error_message", ""),
                "mode": "browser",
                "execution_source": "batch_test",
                "extra": extra
            },
            task_id=batch_id
        )

    def _update_batch_status(self, batch_id: str, status: str, error: str = ""):
        """更新批量测试状态"""
        batch_file = resolve_app_path(f"user_data/batch_tests/{batch_id}.json")

        try:
            updates = {
                "status": status,
                "updated_at": local_now().isoformat(),
            }
            if error:
                updates["error"] = error

            merge_batch_json(batch_file, updates)
        except Exception:
            pass

    def _update_batch_progress(self, batch_id: str, progress: dict):
        """更新批量测试进度"""
        batch_file = resolve_app_path(f"user_data/batch_tests/{batch_id}.json")

        try:
            merge_batch_json(
                batch_file,
                {
                    "progress": progress,
                    "updated_at": local_now().isoformat(),
                },
            )
        except Exception:
            pass

    def _save_report(self, batch_id: str, report: dict):
        """保存批量测试报告"""
        report_file = resolve_app_path(f"user_data/batch_tests/{batch_id}_report.json")
        write_batch_json(report_file, report)
