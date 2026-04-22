from __future__ import annotations

from typing import Any

from ..render.searcheggs.eggs import EggSearcher, SearchResult, format_egg_groups


class EggService(EggSearcher):
    """Wrap the local egg/breeding engine in the plugin core layer."""

    @staticmethod
    def _asset_pet_id(pet_id: Any) -> int | None:
        try:
            numeric_id = int(pet_id)
        except (TypeError, ValueError):
            return None
        return numeric_id if numeric_id >= 3000 else numeric_id + 3000

    def _pet_icon_url(self, pet_id: Any) -> str:
        asset_id = self._asset_pet_id(pet_id)
        if asset_id is None:
            return "{{_res_path}}img/roco_icon.png"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{asset_id}/icon.png"

    def _pet_image_url(self, pet_id: Any) -> str:
        asset_id = self._asset_pet_id(pet_id)
        if asset_id is None:
            return "{{_res_path}}img/roco_icon.png"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{asset_id}/image.png"

    def build_size_search_data(
        self, height: float | None, weight: float | None, results: dict[str, list[dict]], cmd_prefix: str = "/"
    ) -> dict[str, Any]:
        conditions = []
        if height is not None:
            conditions.append(f"身高 {height} cm")
        if weight is not None:
            conditions.append(f"体重 {weight} kg")
        perfect = [self._format_pet_card(p) for p in (results or {}).get("perfect", [])]
        ranged = [self._format_pet_card(p) for p in (results or {}).get("range", [])]
        return {
            "query_label": " / ".join(conditions) if conditions else "尺寸反查",
            "perfect_matches": perfect,
            "range_matches": ranged,
            "total_count": len(perfect) + len(ranged),
            "has_results": bool(perfect or ranged),
            "commandHint": f"💡 {cmd_prefix}洛克查蛋 <精灵名> | {cmd_prefix}洛克查蛋 身高25 体重1.5",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def build_size_search_data_from_api(
        self, height: float | None, weight: float | None, results: dict[str, Any] | None, cmd_prefix: str = "/"
    ) -> dict[str, Any]:
        conditions = []
        if height is not None:
            conditions.append(f"身高 {height} cm")
        if weight is not None:
            conditions.append(f"体重 {weight} kg")
        perfect = [
            self._format_size_api_card(item)
            for item in (results or {}).get("exactResults", [])
        ]
        ranged = [
            self._format_size_api_card(item)
            for item in (results or {}).get("candidates", [])
        ]
        search_mode = (results or {}).get("searchMode") or ""
        subtitle = " / ".join(conditions) if conditions else "尺寸反查"
        if search_mode:
            subtitle = f"{subtitle} · 模式 {search_mode}"
        return {
            "query_label": subtitle,
            "perfect_matches": perfect,
            "range_matches": ranged,
            "total_count": len(perfect) + len(ranged),
            "has_results": bool(perfect or ranged),
            "commandHint": f"💡 {cmd_prefix}洛克查蛋 <精灵名> | {cmd_prefix}洛克查蛋 身高25 体重1.5",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def build_size_search_text_from_api(
        self, height: float | None, weight: float | None, results: dict[str, Any] | None, cmd_prefix: str = "/"
    ) -> str:
        cond = []
        if height is not None:
            cond.append(f"身高={height}cm")
        if weight is not None:
            cond.append(f"体重={weight}kg")
        cond_str = " + ".join(cond) if cond else "当前条件"

        exact_results = (results or {}).get("exactResults") or []
        candidates = (results or {}).get("candidates") or []
        if not exact_results and not candidates:
            return f"❌ 未找到符合 {cond_str} 的精灵。"

        lines = []
        if exact_results:
            lines.append(f"✅ 完美匹配 {cond_str} 的精灵（共 {len(exact_results)} 只）：")
            for i, item in enumerate(exact_results[:10], 1):
                lines.append(f"  {i}. {self._format_size_api_text_line(item)}")
            if len(exact_results) > 10:
                lines.append(f"  ... 还有 {len(exact_results) - 10} 个结果")

        if candidates:
            if lines:
                lines.append("")
            lines.append(f"🔍 范围匹配 {cond_str} 的精灵（共 {len(candidates)} 只）：")
            for i, item in enumerate(candidates[:10], 1):
                lines.append(f"  {i}. {self._format_size_api_text_line(item)}")
            if len(candidates) > 10:
                lines.append(f"  ... 还有 {len(candidates) - 10} 个结果")

        lines.append(f"\n💡 {cmd_prefix}洛克查蛋 <精灵名> 查看详细蛋组信息")
        return "\n".join(lines)

    def build_candidates_render_data(
        self, keyword: str, candidates: list[dict]
    ) -> dict[str, Any]:
        return {
            "keyword": keyword,
            "count": len(candidates),
            "candidates": [self._format_pet_card(p) for p in candidates],
            "commandHint": "💡 请使用更精确的名称重新查询",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def build_want_pet_data(self, pet: dict, cmd_prefix: str = "/") -> dict[str, Any]:
        fathers = self.get_breeding_parents(pet)
        bp = pet.get("breeding_profile") or {}
        egg_groups = self.get_egg_groups(pet)
        return {
            "target": self._format_pet_card(pet),
            "egg_groups_label": format_egg_groups(egg_groups),
            "female_rate": bp.get("female_rate"),
            "male_rate": bp.get("male_rate"),
            "is_undiscovered": 1 in egg_groups,
            "fathers": [self._format_pet_card(p) for p in fathers[:30]],
            "father_count": len(fathers),
            "commandHint": f"💡 {cmd_prefix}洛克配种 <父体> <母体> 查看详细结果",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _format_pet_card(self, pet: dict) -> dict[str, Any]:
        breeding = pet.get("breeding") or {}
        return {
            "id": pet["id"],
            "name": self._name(pet),
            "icon": self._pet_icon_url(pet["id"]),
            "image": self._pet_image_url(pet["id"]),
            "type_label": self._type(pet),
            "egg_groups_label": format_egg_groups(self.get_egg_groups(pet)),
            "height_label": self._fmt_range(
                breeding.get("height_low"), breeding.get("height_high"), "cm"
            ),
            "weight_label": self._fmt_range(
                self._wt(breeding.get("weight_low")),
                self._wt(breeding.get("weight_high")),
                "kg",
            ),
        }

    def _format_size_api_card(self, item: dict[str, Any]) -> dict[str, Any]:
        pet_name = item.get("pet") or "未知精灵"
        pet_id = item.get("petId") or "-"
        probability = item.get("probability")
        match_count = item.get("matchCount")
        extra_parts = []
        if probability is not None:
            extra_parts.append(f"匹配概率 {probability}%")
        if match_count is not None:
            extra_parts.append(f"命中次数 {match_count}")
        egg_group_label = "后端未提供"
        if extra_parts:
            egg_group_label = " / ".join(extra_parts)
        return {
            "id": pet_id,
            "name": pet_name,
            "icon": item.get("petIcon") or self._pet_icon_url(pet_id),
            "image": item.get("petImage") or self._pet_image_url(pet_id),
            "type_label": "后端未提供",
            "egg_groups_label": egg_group_label,
            "height_label": self._fmt_range(
                item.get("diameterMin"),
                item.get("diameterMax"),
                "m",
            ),
            "weight_label": self._fmt_range(
                item.get("weightMin"),
                item.get("weightMax"),
                "kg",
            ),
        }

    def _format_size_api_text_line(self, item: dict[str, Any]) -> str:
        pet_name = item.get("pet") or "未知精灵"
        pet_id = item.get("petId") or "-"
        h_str = self._fmt_range(item.get("diameterMin"), item.get("diameterMax"), "m")
        w_str = self._fmt_range(item.get("weightMin"), item.get("weightMax"), "kg")
        extras = []
        if item.get("probability") is not None:
            extras.append(f"概率 {item['probability']}%")
        if item.get("matchCount") is not None:
            extras.append(f"命中 {item['matchCount']} 次")
        extra_str = f" · {' / '.join(extras)}" if extras else ""
        return f"{pet_name} (#{pet_id}) — {h_str} / {w_str}{extra_str}"


__all__ = ["EggService", "SearchResult"]
