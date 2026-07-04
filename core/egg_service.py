from __future__ import annotations

import re
from typing import Any

from ..render.searcheggs.eggs import (
    EggSearcher,
    SearchResult,
    format_egg_groups,
    get_egg_group_label,
)


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
        self,
        height: float | None,
        weight: float | None,
        results: dict[str, list[dict]],
        height_display: str | None = None,
    ) -> dict[str, Any]:
        conditions = []
        if height is not None:
            conditions.append(f"身高 {height_display or self._fmt_height_query(height)}")
        if weight is not None:
            conditions.append(f"体重 {weight} kg")
        perfect, ranged = self._merge_cards_by_name(
            [
                self._format_pet_card(p, query_height=height, query_weight=weight)
                for p in (results or {}).get("perfect", [])
            ],
            [
                self._format_pet_card(p, query_height=height, query_weight=weight)
                for p in (results or {}).get("range", [])
            ],
        )
        return {
            "query_label": " / ".join(conditions) if conditions else "尺寸反查",
            "perfect_matches": perfect,
            "range_matches": ranged,
            "total_count": len(perfect) + len(ranged),
            "has_results": bool(perfect or ranged),
            "commandHint": "💡 /洛克查蛋 <精灵名> | /洛克查蛋 0.18m 1.5kg | /洛克查蛋 0.18",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def build_size_search_data_from_api(
        self,
        height: float | None,
        weight: float | None,
        results: dict[str, Any] | None,
        height_display: str | None = None,
    ) -> dict[str, Any]:
        conditions = []
        if height is not None:
            conditions.append(f"身高 {height_display or self._fmt_height_query(height)}")
        if weight is not None:
            conditions.append(f"体重 {weight} kg")
        if isinstance((results or {}).get("items"), list):
            perfect_raw, ranged_raw = self._format_new_size_api_cards(
                (results or {}).get("items") or []
            )
        else:
            perfect_raw = [
                self._format_size_api_card(item)
                for item in (results or {}).get("exactResults", [])
            ]
            ranged_raw = [
                self._format_size_api_card(item)
                for item in (results or {}).get("candidates", [])
            ]
        perfect, ranged = self._merge_cards_by_name(perfect_raw, ranged_raw)
        query_meta = (results or {}).get("query") or {}
        pool = query_meta.get("pool") if isinstance(query_meta, dict) else {}
        pool_name = pool.get("name") if isinstance(pool, dict) else ""
        search_mode = (results or {}).get("searchMode") or pool_name or ""
        subtitle = " / ".join(conditions) if conditions else "尺寸反查"
        if search_mode:
            subtitle = f"{subtitle} · 模式 {search_mode}"
        return {
            "query_label": subtitle,
            "perfect_matches": perfect,
            "range_matches": ranged,
            "total_count": len(perfect) + len(ranged),
            "has_results": bool(perfect or ranged),
            "commandHint": "💡 /洛克查蛋 <精灵名> | /洛克查蛋 0.18m 1.5kg | /洛克查蛋 0.18",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def build_size_search_text_from_api(
        self,
        height: float | None,
        weight: float | None,
        results: dict[str, Any] | None,
        height_display: str | None = None,
    ) -> str:
        cond = []
        if height is not None:
            cond.append(f"身高={height_display or self._fmt_height_query(height)}")
        if weight is not None:
            cond.append(f"体重={weight}kg")
        cond_str = " + ".join(cond) if cond else "当前条件"

        if isinstance((results or {}).get("items"), list):
            perfect_raw, ranged_raw = self._format_new_size_api_cards(
                (results or {}).get("items") or []
            )
        else:
            perfect_raw = [
                self._format_size_api_card(item)
                for item in (results or {}).get("exactResults") or []
            ]
            ranged_raw = [
                self._format_size_api_card(item)
                for item in (results or {}).get("candidates") or []
            ]
        exact_results, candidates = self._merge_cards_by_name(perfect_raw, ranged_raw)
        if not exact_results and not candidates:
            return f"❌ 未找到符合 {cond_str} 的精灵。"

        lines = []
        if exact_results:
            lines.append(f"✅ 完美匹配 {cond_str} 的精灵（共 {len(exact_results)} 只）：")
            for i, item in enumerate(exact_results[:10], 1):
                lines.append(f"  {i}. {self._format_size_card_text_line(item)}")
            if len(exact_results) > 10:
                lines.append(f"  ... 还有 {len(exact_results) - 10} 个结果")

        if candidates:
            if lines:
                lines.append("")
            lines.append(f"🔍 范围匹配 {cond_str} 的精灵（共 {len(candidates)} 只）：")
            for i, item in enumerate(candidates[:10], 1):
                lines.append(f"  {i}. {self._format_size_card_text_line(item)}")
            if len(candidates) > 10:
                lines.append(f"  ... 还有 {len(candidates) - 10} 个结果")

        lines.append("\n💡 /洛克查蛋 <精灵名> 查看详细蛋组信息")
        return "\n".join(lines)

    def build_search_data_from_api(
        self,
        pet: dict[str, Any],
        compatible_by_group: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Adapt backend pet/detail data to the existing egg search template."""
        compatible_by_group = compatible_by_group or {}
        egg_groups = self._api_egg_groups(pet.get("egg_group"))
        egg_group_labels = {
            gid: (get_egg_group_label(gid) if isinstance(gid, int) else str(gid))
            for gid in egg_groups
        }
        sections = []
        compatible_seen = set()
        total_compatible = 0
        for gid in egg_groups:
            if gid == 1:
                sections.append(
                    {
                        "id": gid,
                        "label": "未发现",
                        "desc": "不能和任何精灵生蛋，多用于传说中的精灵",
                        "count": 0,
                        "members": [],
                        "has_more": False,
                        "total": 0,
                    }
                )
                continue
            group_name = str(gid)
            raw_members = compatible_by_group.get(group_name) or []
            members = []
            for item in raw_members:
                item_id = item.get("id")
                if item_id == pet.get("id"):
                    continue
                key = str(item_id or item.get("name") or "")
                if key and key not in compatible_seen:
                    compatible_seen.add(key)
                    total_compatible += 1
                members.append(self._format_api_member(item))
            sections.append(
                {
                    "id": gid,
                    "label": group_name,
                    "desc": "",
                    "count": len(members),
                    "members": members[:30],
                    "has_more": len(members) > 30,
                    "total": len(members),
                }
            )

        attr = pet.get("attribute") or {}
        breeding = pet.get("breeding") or {}
        bp = pet.get("breeding_profile") or {}
        male_rate, female_rate = self._api_gender_rates(bp, breeding)
        return {
            "pet_name": pet.get("name") or "未知精灵",
            "pet_id": pet.get("id") or "-",
            "pet_icon": pet.get("pet_icon_url") or pet.get("icon_url") or self._pet_icon_url(pet.get("id")),
            "pet_image": pet.get("pet_img_url") or pet.get("image_url") or self._pet_image_url(pet.get("id")),
            "type_label": self._api_type_label(pet),
            "egg_groups_label": " / ".join(str(egg_group_labels.get(gid, gid)) for gid in egg_groups) or "暂无蛋组数据",
            "egg_groups": egg_groups,
            "egg_group_labels": egg_group_labels,
            "male_rate": male_rate,
            "female_rate": female_rate,
            "hatch_label": self._fmt_dur(breeding.get("hatch_data")),
            "weight_label": self._fmt_range(
                self._wt(pet.get("weight_low") or breeding.get("weight_low")),
                self._wt(pet.get("weight_high") or breeding.get("weight_high")),
                "kg",
            ),
            "height_label": self._fmt_range(
                self._ht(pet.get("height_low") or breeding.get("height_low")),
                self._ht(pet.get("height_high") or breeding.get("height_high")),
                "m",
            ),
            "total_compatible": total_compatible,
            "is_undiscovered": 1 in egg_groups,
            "egg_group_sections": sections,
            "total_stats": sum(
                self._num(attr.get(key)) or 0
                for key in (
                    "attr_hp",
                    "attr_atk",
                    "attr_spatk",
                    "attr_def",
                    "attr_spdef",
                    "attr_spd",
                )
            ),
            "egg_details": self._build_egg_details(breeding),
        }

    def build_size_search_text(
        self,
        height: float = None,
        weight: float = None,
        results: dict = None,
        height_display: str | None = None,
    ) -> str:
        cond = []
        if height is not None:
            cond.append(f"身高={height_display or self._fmt_height_query(height)}")
        if weight is not None:
            cond.append(f"体重={weight}kg")
        cond_str = " + ".join(cond)

        perfect, ranged = self._merge_cards_by_name(
            [
                self._format_pet_card(p, query_height=height, query_weight=weight)
                for p in (results or {}).get("perfect", [])
            ],
            [
                self._format_pet_card(p, query_height=height, query_weight=weight)
                for p in (results or {}).get("range", [])
            ],
        )
        if not perfect and not ranged:
            return f"❌ 未找到符合 {cond_str} 的精灵。"

        lines = []
        if perfect:
            lines.append(f"✅ 完美匹配 {cond_str} 的精灵（共 {len(perfect)} 只）：")
            for i, item in enumerate(perfect[:10], 1):
                lines.append(
                    f"  {i}. {item['name']} (#{item['id']}) — {item['height_label']} / {item['weight_label']} · {item['egg_groups_label']}"
                )
            if len(perfect) > 10:
                lines.append(f"  ... 还有 {len(perfect) - 10} 个结果")

        if ranged:
            if lines:
                lines.append("")
            lines.append(f"🔍 范围匹配 {cond_str} 的精灵（共 {len(ranged)} 只，容差±15%）：")
            for i, item in enumerate(ranged[:10], 1):
                lines.append(
                    f"  {i}. {item['name']} (#{item['id']}) — {item['height_label']} / {item['weight_label']} · {item['egg_groups_label']}"
                )
            if len(ranged) > 10:
                lines.append(f"  ... 还有 {len(ranged) - 10} 个结果")

        lines.append("\n💡 /洛克查蛋 <精灵名> 查看详细蛋组信息")
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

    def _api_egg_groups(self, value: Any) -> list[Any]:
        if not value:
            return []
        if not isinstance(value, list):
            value = [value]
        groups = []
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            if "未发现" in text:
                groups.append(1)
            else:
                groups.append(text)
        return groups

    def _api_type_label(self, pet: dict[str, Any]) -> str:
        types = pet.get("unit_type")
        if isinstance(types, list) and types:
            return " / ".join(str(item) for item in types if item) or "未知"
        return str(pet.get("type") or pet.get("attribute_name") or "未知")

    def _format_api_member(self, item: dict[str, Any]) -> dict[str, Any]:
        egg_groups = self._api_egg_groups(item.get("egg_group"))
        return {
            "name": item.get("name") or "未知精灵",
            "id": item.get("id") or "-",
            "type_label": self._api_type_label(item),
            "egg_groups_label": " / ".join(
                get_egg_group_label(gid) if isinstance(gid, int) else str(gid)
                for gid in egg_groups
            ) or "暂无蛋组数据",
        }

    def _api_gender_rates(
        self, breeding_profile: dict[str, Any], breeding: dict[str, Any]
    ) -> tuple[Any, Any]:
        male_rate = breeding_profile.get("male_rate")
        female_rate = breeding_profile.get("female_rate")
        if male_rate is not None or female_rate is not None:
            return male_rate, female_rate
        voice_percent = breeding.get("voice_percent")
        if isinstance(voice_percent, list) and len(voice_percent) >= 2:
            return voice_percent[0], voice_percent[1]
        return None, None

    def build_want_pet_data(self, pet: dict) -> dict[str, Any]:
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
            "commandHint": "💡 /洛克配种 <父体> <母体> 查看详细结果",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _format_pet_card(
        self,
        pet: dict,
        query_height: float | None = None,
        query_weight: float | None = None,
    ) -> dict[str, Any]:
        breeding = pet.get("breeding") or {}
        egg_groups = self.get_egg_groups(pet)
        height_min = self._height_data_to_m(breeding.get("height_low"))
        height_max = self._height_data_to_m(breeding.get("height_high"))
        weight_min = self._wt(breeding.get("weight_low"))
        weight_max = self._wt(breeding.get("weight_high"))
        probability, match_count = self._calc_local_match_info(
            query_height=self._height_data_to_m(query_height),
            query_weight=query_weight,
            height_min=height_min,
            height_max=height_max,
            weight_min=weight_min,
            weight_max=weight_max,
        )
        return {
            "id": pet["id"],
            "name": self._name(pet),
            "icon": self._pet_icon_url(pet["id"]),
            "image": self._pet_image_url(pet["id"]),
            "type_label": self._type(pet),
            "egg_group_ids": egg_groups,
            "egg_groups_label": format_egg_groups(egg_groups),
            "height_min": height_min,
            "height_max": height_max,
            "height_label": self._fmt_range(height_min, height_max, "m"),
            "weight_min": weight_min,
            "weight_max": weight_max,
            "weight_label": self._fmt_range(weight_min, weight_max, "kg"),
            "probability": probability,
            "match_count": match_count,
            "match_info_label": self._format_match_summary(probability, match_count),
        }

    def _format_size_api_card(self, item: dict[str, Any]) -> dict[str, Any]:
        pet_info = item.get("pet") if isinstance(item.get("pet"), dict) else {}
        pet_name = pet_info.get("name") or item.get("pet") or item.get("name") or "未知精灵"
        pet_id = pet_info.get("pet_id") or item.get("petId") or item.get("pet_id") or "-"
        egg_size = item.get("egg_size") or {}
        height = egg_size.get("height") if isinstance(egg_size, dict) else {}
        weight = egg_size.get("weight") if isinstance(egg_size, dict) else {}
        match = item.get("match") or {}
        match_text = match.get("match_percent_text") if isinstance(match, dict) else ""
        match_percent = match.get("percent") if isinstance(match, dict) else None
        egg_groups = pet_info.get("egg_group_names") or item.get("egg_group_names") or []
        type_names = pet_info.get("type_names") or item.get("type_names") or []
        probability = self._num(item.get("probability"))
        if probability is None:
            probability = self._num(match_percent)
        match_count = self._num(item.get("matchCount"))
        return {
            "id": pet_id,
            "name": pet_name,
            "icon": pet_info.get("icon") or item.get("petIcon") or self._pet_icon_url(pet_id),
            "image": pet_info.get("small_icon") or item.get("petImage") or self._pet_image_url(pet_id),
            "type_label": " / ".join(str(x) for x in type_names if x) or "后端未提供",
            "egg_group_ids": [],
            "probability": probability,
            "match_count": match_count,
            "egg_groups_label": " / ".join(str(x) for x in egg_groups if x) or "后端未提供",
            "match_info_label": match_text or self._format_match_summary(probability, match_count),
            "height_min": self._num(height.get("min_m") if isinstance(height, dict) else item.get("diameterMin")),
            "height_max": self._num(height.get("max_m") if isinstance(height, dict) else item.get("diameterMax")),
            "height_label": self._fmt_range(
                height.get("min_m") if isinstance(height, dict) else item.get("diameterMin"),
                height.get("max_m") if isinstance(height, dict) else item.get("diameterMax"),
                "m",
            ),
            "weight_min": self._num(weight.get("min_kg") if isinstance(weight, dict) else item.get("weightMin")),
            "weight_max": self._num(weight.get("max_kg") if isinstance(weight, dict) else item.get("weightMax")),
            "weight_label": self._fmt_range(
                weight.get("min_kg") if isinstance(weight, dict) else item.get("weightMin"),
                weight.get("max_kg") if isinstance(weight, dict) else item.get("weightMax"),
                "kg",
            ),
        }

    def _format_new_size_api_cards(
        self, items: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        perfect: list[dict[str, Any]] = []
        ranged: list[dict[str, Any]] = []
        for item in items:
            card = self._format_size_api_card(item)
            match = item.get("match") or {}
            layer = str(match.get("layer") or "").lower() if isinstance(match, dict) else ""
            display_only = bool(match.get("display_only")) if isinstance(match, dict) else False
            if layer in {"strict", "exact"} and not display_only:
                perfect.append(card)
            else:
                ranged.append(card)
        return perfect, ranged

    def _format_size_card_text_line(self, item: dict[str, Any]) -> str:
        return f"{item.get('name') or '未知精灵'} (#{item.get('id') or '-'}) — {item.get('height_label') or '暂无数据'} / {item.get('weight_label') or '暂无数据'} · {item.get('egg_groups_label') or '暂无数据'}"

    def _base_pet_name(self, name: Any) -> str:
        text = str(name or "").strip()
        if not text:
            return ""
        text = re.sub(r"\s+", "", text)
        return text

    def _merge_cards_by_name(
        self, perfect: list[dict[str, Any]], ranged: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        perfect_map: dict[str, dict[str, Any]] = {}
        ranged_map: dict[str, dict[str, Any]] = {}

        def add_item(target: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
            key = self._base_pet_name(item.get("name")) or str(item.get("id", ""))
            if key in target:
                target[key] = self._merge_size_card(target[key], item)
            else:
                target[key] = item

        for item in perfect:
            add_item(perfect_map, item)
        for item in ranged:
            key = self._base_pet_name(item.get("name")) or str(item.get("id", ""))
            if key in perfect_map:
                perfect_map[key] = self._merge_size_card(perfect_map[key], item)
            else:
                add_item(ranged_map, item)

        return list(perfect_map.values()), list(ranged_map.values())

    def _merge_size_card(self, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        merged = dict(left)
        merged["id"] = self._join_unique_ids(left.get("id"), right.get("id"))

        egg_group_ids = self._unique_values((left.get("egg_group_ids") or []) + (right.get("egg_group_ids") or []))
        merged["egg_group_ids"] = egg_group_ids
        if egg_group_ids:
            merged["egg_groups_label"] = format_egg_groups(egg_group_ids)
        else:
            labels = self._unique_values([left.get("egg_groups_label"), right.get("egg_groups_label")])
            merged["egg_groups_label"] = " / ".join(labels) if labels else left.get("egg_groups_label")

        probability = self._sum_values(left.get("probability"), right.get("probability"))
        match_count = self._sum_values(left.get("match_count"), right.get("match_count"))
        merged["probability"] = probability
        merged["match_count"] = match_count
        merged["match_info_label"] = self._format_match_summary(probability, match_count)

        height_min = self._min_value(left.get("height_min"), right.get("height_min"))
        height_max = self._max_value(left.get("height_max"), right.get("height_max"))
        weight_min = self._min_value(left.get("weight_min"), right.get("weight_min"))
        weight_max = self._max_value(left.get("weight_max"), right.get("weight_max"))
        merged.update({
            "height_min": height_min,
            "height_max": height_max,
            "height_label": self._fmt_range(height_min, height_max, "m"),
            "weight_min": weight_min,
            "weight_max": weight_max,
            "weight_label": self._fmt_range(weight_min, weight_max, "kg"),
        })
        return merged

    @staticmethod
    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _sum_values(cls, *values: Any) -> float | None:
        numbers = [cls._num(value) for value in values if cls._num(value) is not None]
        if not numbers:
            return None
        return sum(numbers)

    @classmethod
    def _format_number(cls, value: Any, digits: int = 2) -> str:
        number = cls._num(value)
        if number is None:
            return ""
        rounded = round(number, digits)
        return f"{rounded:g}"

    @classmethod
    def _format_match_summary(cls, probability: Any = None, match_count: Any = None) -> str:
        parts = []
        if probability is not None:
            parts.append(f"匹配率 {cls._format_number(probability)}%")
        if match_count is not None:
            parts.append(f"命中次数 {cls._format_number(match_count, 0)}")
        return " / ".join(parts) if parts else "后端未提供"

    @classmethod
    def _calc_local_match_info(
        cls,
        query_height: float | None,
        query_weight: float | None,
        height_min: float | None,
        height_max: float | None,
        weight_min: float | None,
        weight_max: float | None,
    ) -> tuple[float | None, float | None]:
        scores = []
        if query_height is not None:
            score = cls._range_match_score(query_height, height_min, height_max)
            if score is not None:
                scores.append(score)
        if query_weight is not None:
            score = cls._range_match_score(query_weight, weight_min, weight_max)
            if score is not None:
                scores.append(score)
        if not scores:
            return None, None
        return sum(scores) / len(scores), float(len(scores))

    @classmethod
    def _range_match_score(cls, value: Any, low: Any, high: Any) -> float | None:
        value_num = cls._num(value)
        low_num = cls._num(low)
        high_num = cls._num(high)
        if value_num is None or low_num is None or high_num is None:
            return None
        if low_num <= value_num <= high_num:
            return 100.0
        if value_num < low_num:
            tolerance = max(low_num * 0.15, 0.0001)
            distance = low_num - value_num
        else:
            tolerance = max(high_num * 0.15, 0.0001)
            distance = value_num - high_num
        if distance > tolerance:
            return 0.0
        return max(0.0, 100.0 * (1.0 - distance / tolerance))

    @classmethod
    def _height_data_to_m(cls, value: Any) -> float | None:
        number = cls._num(value)
        return round(number / 100, 2) if number is not None else None

    @classmethod
    def _fmt_height_query(cls, height_value: Any) -> str:
        height_m = cls._height_data_to_m(height_value)
        return cls._fmt_range(height_m, height_m, "m")

    @classmethod
    def _min_value(cls, *values: Any) -> float | None:
        numbers = [cls._num(value) for value in values if cls._num(value) is not None]
        return min(numbers) if numbers else None

    @classmethod
    def _max_value(cls, *values: Any) -> float | None:
        numbers = [cls._num(value) for value in values if cls._num(value) is not None]
        return max(numbers) if numbers else None

    @staticmethod
    def _unique_values(values: list[Any]) -> list[Any]:
        output = []
        seen = set()
        for value in values:
            if value in (None, ""):
                continue
            key = str(value)
            if key in seen:
                continue
            seen.add(key)
            output.append(value)
        return output

    @classmethod
    def _join_unique_ids(cls, *values: Any) -> str:
        ids: list[str] = []
        for value in values:
            for part in str(value or "").split("/"):
                part = part.strip().lstrip("#")
                if part:
                    ids.append(part)
        return "/".join(str(item) for item in cls._unique_values(ids))

__all__ = ["EggService", "SearchResult"]
