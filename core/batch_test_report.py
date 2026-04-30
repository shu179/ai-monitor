"""
批量测试报告生成器

生成包含品牌展示率和媒体抓取统计的完整报告。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .history import get_records


class BatchTestReportGenerator:
    """批量测试报告生成器"""

    def generate_report(self, batch_id: str) -> dict:
        """
        生成批量测试报告

        Returns:
            完整的报告数据结构，包含：
            - summary: 总览统计
            - keyword_stats: 关键词维度统计（含跨平台同抓取媒体）
            - platform_stats: 平台维度统计（含跨关键词相同媒体）
            - global_media_matrix: 全局媒体矩阵
        """
        # 从 history 读取该 batch_id 的所有记录
        records = self._get_batch_records(batch_id)

        if not records:
            return {
                "batch_id": batch_id,
                "brand": "",
                "summary": {"total_queries": 0, "success_queries": 0, "overall_display_rate": 0},
                "keyword_stats": [],
                "platform_stats": [],
                "global_media_matrix": []
            }

        brand = self._get_brand_from_records(records)

        return {
            "batch_id": batch_id,
            "brand": brand,
            "summary": self._calc_summary(records),
            "keyword_stats": self._calc_keyword_stats(records),
            "platform_stats": self._calc_platform_stats(records),
            "global_media_matrix": self._calc_global_media_matrix(records)
        }

    def _get_batch_records(self, batch_id: str) -> list[dict]:
        """获取批量测试的所有记录"""
        all_records = get_records(task_id=batch_id)

        # 过滤出属于该 batch_id 的记录
        batch_records = []
        for record in all_records:
            extra = record.get("extra") or {}
            if isinstance(extra, dict) and extra.get("batch_id") == batch_id:
                batch_records.append(record)

        return batch_records

    def _get_brand_from_records(self, records: list[dict]) -> str:
        """从记录中提取品牌名"""
        for record in records:
            brand = str(record.get("brand") or "").strip()
            if brand:
                return brand
        return ""

    def _calc_summary(self, records: list[dict]) -> dict:
        """计算总览统计"""
        total_queries = len(records)
        success_queries = sum(1 for r in records if r.get("success"))
        overall_display_rate = success_queries / total_queries if total_queries > 0 else 0

        return {
            "total_queries": total_queries,
            "success_queries": success_queries,
            "overall_display_rate": overall_display_rate
        }

    def _calc_keyword_stats(self, records: list[dict]) -> list[dict]:
        """计算关键词维度统计"""
        keyword_map = {}

        for record in records:
            keyword = str(record.get("keyword") or "").strip()
            platform = str(record.get("platform") or "").strip()
            success = bool(record.get("success"))
            extra = record.get("extra") or {}
            media_sources = extra.get("media_sources") if isinstance(extra, dict) else []
            media_sources = media_sources if isinstance(media_sources, list) else []

            if not keyword:
                continue

            if keyword not in keyword_map:
                keyword_map[keyword] = {
                    "keyword": keyword,
                    "total_queries": 0,
                    "brand_displays": 0,
                    "platforms": {}
                }

            kw_stat = keyword_map[keyword]
            kw_stat["total_queries"] += 1
            if success:
                kw_stat["brand_displays"] += 1

            # 平台统计
            if platform not in kw_stat["platforms"]:
                kw_stat["platforms"][platform] = {
                    "platform": platform,
                    "queries": 0,
                    "brand_displays": 0,
                    "media_counter": {}
                }

            plat_stat = kw_stat["platforms"][platform]
            plat_stat["queries"] += 1
            if success:
                plat_stat["brand_displays"] += 1

            # 媒体统计
            for media in media_sources:
                if not isinstance(media, dict):
                    continue
                media_name = str(media.get("name") or "").strip()
                if media_name:
                    plat_stat["media_counter"][media_name] = plat_stat["media_counter"].get(media_name, 0) + 1

        # 计算展示率和媒体比例
        result = []
        for kw_stat in keyword_map.values():
            kw_stat["display_rate"] = kw_stat["brand_displays"] / kw_stat["total_queries"] if kw_stat["total_queries"] > 0 else 0

            # 平台统计
            platform_stats = []
            for plat_stat in kw_stat["platforms"].values():
                plat_stat["display_rate"] = plat_stat["brand_displays"] / plat_stat["queries"] if plat_stat["queries"] > 0 else 0

                # 媒体统计
                total_media_count = sum(plat_stat["media_counter"].values())
                media_stats = {}
                for media_name, count in plat_stat["media_counter"].items():
                    media_stats[media_name] = {
                        "count": count,
                        "ratio": count / total_media_count if total_media_count > 0 else 0
                    }
                plat_stat["media_stats"] = media_stats
                del plat_stat["media_counter"]

                platform_stats.append(plat_stat)

            kw_stat["platform_stats"] = platform_stats

            # 计算跨平台同抓取媒体
            kw_stat["cross_platform_media"] = self._calc_cross_platform_media_for_keyword(kw_stat)
            del kw_stat["platforms"]

            result.append(kw_stat)

        return result

    def _calc_cross_platform_media_for_keyword(self, kw_stat: dict) -> list[dict]:
        """计算某个关键词的跨平台同抓取媒体"""
        media_map = {}

        for plat_stat in kw_stat["platform_stats"]:
            platform = plat_stat["platform"]
            for media_name, media_data in plat_stat["media_stats"].items():
                if media_name not in media_map:
                    media_map[media_name] = {
                        "media": media_name,
                        "platforms": [],
                        "total_count": 0
                    }

                media_map[media_name]["platforms"].append({
                    "platform": platform,
                    "count": media_data["count"],
                    "ratio": media_data["ratio"]
                })
                media_map[media_name]["total_count"] += media_data["count"]

        # 只保留出现在多个平台的媒体
        result = []
        for media_data in media_map.values():
            media_data["platform_count"] = len(media_data["platforms"])
            if media_data["platform_count"] >= 2:
                result.append(media_data)

        # 按出现平台数降序排序
        result.sort(key=lambda x: (-x["platform_count"], -x["total_count"]))
        return result

    def _calc_platform_stats(self, records: list[dict]) -> list[dict]:
        """计算平台维度统计（跨关键词）"""
        platform_map = {}

        for record in records:
            keyword = str(record.get("keyword") or "").strip()
            platform = str(record.get("platform") or "").strip()
            success = bool(record.get("success"))
            extra = record.get("extra") or {}
            media_sources = extra.get("media_sources") if isinstance(extra, dict) else []
            media_sources = media_sources if isinstance(media_sources, list) else []

            if not platform:
                continue

            if platform not in platform_map:
                platform_map[platform] = {
                    "platform": platform,
                    "total_queries": 0,
                    "brand_displays": 0,
                    "keywords": {}
                }

            plat_stat = platform_map[platform]
            plat_stat["total_queries"] += 1
            if success:
                plat_stat["brand_displays"] += 1

            # 关键词维度的媒体统计
            if keyword not in plat_stat["keywords"]:
                plat_stat["keywords"][keyword] = {
                    "keyword": keyword,
                    "queries": 0,
                    "media_counter": {}
                }

            kw_data = plat_stat["keywords"][keyword]
            kw_data["queries"] += 1

            # 媒体统计
            for media in media_sources:
                if not isinstance(media, dict):
                    continue
                media_name = str(media.get("name") or "").strip()
                if media_name:
                    kw_data["media_counter"][media_name] = kw_data["media_counter"].get(media_name, 0) + 1

        # 计算展示率和跨关键词相同媒体
        result = []
        for plat_stat in platform_map.values():
            plat_stat["display_rate"] = plat_stat["brand_displays"] / plat_stat["total_queries"] if plat_stat["total_queries"] > 0 else 0

            # 计算跨关键词相同媒体
            plat_stat["cross_keyword_media"] = self._calc_cross_keyword_media_for_platform(plat_stat)
            del plat_stat["keywords"]

            result.append(plat_stat)

        return result

    def _calc_cross_keyword_media_for_platform(self, plat_stat: dict) -> list[dict]:
        """计算某个平台的跨关键词相同媒体"""
        media_map = {}

        for kw_data in plat_stat["keywords"].values():
            keyword = kw_data["keyword"]
            queries = kw_data["queries"]

            for media_name, count in kw_data["media_counter"].items():
                if media_name not in media_map:
                    media_map[media_name] = {
                        "media": media_name,
                        "keywords": [],
                        "total_count": 0
                    }

                media_map[media_name]["keywords"].append({
                    "keyword": keyword,
                    "count": count,
                    "ratio": count / queries if queries > 0 else 0,
                    "queries": queries
                })
                media_map[media_name]["total_count"] += count

        # 只保留出现在多个关键词的媒体
        result = []
        for media_data in media_map.values():
            media_data["keyword_count"] = len(media_data["keywords"])
            if media_data["keyword_count"] >= 2:
                result.append(media_data)

        # 按出现关键词数降序排序
        result.sort(key=lambda x: (-x["keyword_count"], -x["total_count"]))
        return result

    def _calc_global_media_matrix(self, records: list[dict]) -> list[dict]:
        """计算全局跨关键词跨平台媒体矩阵"""
        media_map = {}

        for record in records:
            keyword = str(record.get("keyword") or "").strip()
            platform = str(record.get("platform") or "").strip()
            extra = record.get("extra") or {}
            media_sources = extra.get("media_sources") if isinstance(extra, dict) else []
            media_sources = media_sources if isinstance(media_sources, list) else []

            if not keyword or not platform:
                continue

            # 统计每个媒体在每个关键词×平台组合中的出现情况
            for media in media_sources:
                if not isinstance(media, dict):
                    continue
                media_name = str(media.get("name") or "").strip()
                if not media_name:
                    continue

                if media_name not in media_map:
                    media_map[media_name] = {
                        "media": media_name,
                        "cells": {},  # (keyword, platform) -> count
                        "keywords": set(),
                        "platforms": set()
                    }

                cell_key = (keyword, platform)
                if cell_key not in media_map[media_name]["cells"]:
                    media_map[media_name]["cells"][cell_key] = {"count": 0, "queries": 0}

                media_map[media_name]["cells"][cell_key]["count"] += 1
                media_map[media_name]["keywords"].add(keyword)
                media_map[media_name]["platforms"].add(platform)

        # 统计每个关键词×平台组合的总查询次数
        query_counts = {}
        for record in records:
            keyword = str(record.get("keyword") or "").strip()
            platform = str(record.get("platform") or "").strip()
            if keyword and platform:
                cell_key = (keyword, platform)
                query_counts[cell_key] = query_counts.get(cell_key, 0) + 1

        # 构建矩阵
        result = []
        for media_name, media_data in media_map.items():
            # 只保留出现在多个关键词或多个平台的媒体
            keyword_count = len(media_data["keywords"])
            platform_count = len(media_data["platforms"])

            if keyword_count >= 2 or platform_count >= 2:
                matrix = []
                total_count = 0

                for (keyword, platform), cell_data in media_data["cells"].items():
                    count = cell_data["count"]
                    queries = query_counts.get((keyword, platform), 0)
                    ratio = count / queries if queries > 0 else 0

                    matrix.append({
                        "keyword": keyword,
                        "platform": platform,
                        "count": count,
                        "queries": queries,
                        "ratio": ratio
                    })
                    total_count += count

                # 按关键词、平台排序
                matrix.sort(key=lambda x: (x["keyword"], x["platform"]))

                result.append({
                    "media": media_name,
                    "total_count": total_count,
                    "keyword_count": keyword_count,
                    "platform_count": platform_count,
                    "matrix": matrix
                })

        # 按总出现次数降序排序
        result.sort(key=lambda x: -x["total_count"])
        return result
