"""
洛克王国查蛋模块 (自包含)

数据、逻辑、渲染模板均在 render/searcheggs/ 下，
外部 main.py 仅做指令路由的薄调用层。
"""

import os
import json
from typing import Dict, List, Optional

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

# ── 蛋组元数据 ──────────────────────────────────────────────

EGG_GROUP_META = {
    1:  {"label": "未发现", "desc": "不能和任何精灵生蛋，多用于传说中的精灵"},
    2:  {"label": "怪兽",   "desc": "像怪兽一样，或者比较野性的动物"},
    3:  {"label": "两栖",   "desc": "两栖动物和水边生活的多栖动物"},
    4:  {"label": "虫",     "desc": "看起来像虫子的精灵"},
    5:  {"label": "飞行",   "desc": "会飞的精灵"},
    6:  {"label": "陆上",   "desc": "生活在陆地上的精灵"},
    7:  {"label": "妖精",   "desc": "可爱的小动物，以及神话中的精灵"},
    8:  {"label": "植物",   "desc": "看起来像植物的精灵"},
    9:  {"label": "人型",   "desc": "看起来像人的精灵"},
    10: {"label": "软体",   "desc": "看起来软软的精灵，圆形多为软体动物"},
    11: {"label": "矿物",   "desc": "身体由矿物组成的精灵"},
    12: {"label": "不定形", "desc": "没有固定形态的精灵，包括水、火、灵魂、能量"},
    13: {"label": "鱼",     "desc": "看起来像鱼的精灵"},
    14: {"label": "龙",     "desc": "看起来像龙的精灵"},
    15: {"label": "机械",   "desc": "身体由机械组成的精灵"},
}


def get_egg_group_label(group_id: int) -> str:
    meta = EGG_GROUP_META.get(group_id)
    return meta["label"] if meta else f"蛋组{group_id}"


def format_egg_groups(group_ids: List[int]) -> str:
    if not group_ids:
        return "暂无蛋组数据"
    return " / ".join(get_egg_group_label(gid) for gid in group_ids)


# ── 搜索结果类型 ─────────────────────────────────────────────

class SearchResult:
    """搜索结果封装，区分精确/模糊/多候选"""
    EXACT = "exact"
    FUZZY = "fuzzy"
    MULTI = "multi"
    NOT_FOUND = "not_found"

    def __init__(self, match_type: str, pet=None, candidates=None):
        self.match_type = match_type
        self.pet = pet                        # 单个匹配结果
        self.candidates: List[dict] = candidates or []  # 多候选


# ── 查蛋引擎 ─────────────────────────────────────────────────

class EggSearcher:
    """蛋组查询引擎，数据自包含在 render/searcheggs/ 下"""

    def __init__(self, data_dir: str = None):
        """
        data_dir: render/searcheggs/ 目录路径。
        若不传则自动取本文件所在目录。
        """
        if data_dir is None:
            data_dir = os.path.dirname(os.path.abspath(__file__))
        self._data_dir = data_dir
        self._pets: List[dict] = []
        self._by_id: Dict[int, dict] = {}
        self._by_zh: Dict[str, dict] = {}      # 中文名 → pet (精确)
        self._by_en: Dict[str, dict] = {}      # 英文名小写 → pet
        self._load()

    def _load(self):
        path = os.path.join(self._data_dir, "Pets.json")
        if not os.path.exists(path):
            logger.error(f"[Eggs] Pets.json 不存在: {path}")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._pets = raw if isinstance(raw, list) else []
            for p in self._pets:
                self._by_id[p["id"]] = p
                zh = p.get("localized", {}).get("zh", {}).get("name", "")
                if zh:
                    self._by_zh[zh] = p
                en = p.get("name", "").lower()
                if en:
                    self._by_en[en] = p
            logger.info(f"[Eggs] 加载 {len(self._pets)} 只精灵")
        except Exception as e:
            logger.error(f"[Eggs] 加载失败: {e}")

    # ── 按身高/体重反查 ──

    def search_by_size(self, height: float = None, weight: float = None) -> dict:
        """
        通过身高(cm)或体重(kg)反查精灵。
        返回结构：
        {
            "perfect": [精确匹配的精灵列表],
            "range": [范围匹配的精灵列表(带容差)],
        }
        """
        perfect_results = []
        range_results = []

        for p in self._pets:
            br = p.get("breeding") or {}
            h_lo = br.get("height_low")
            h_hi = br.get("height_high")
            w_lo = br.get("weight_low")
            w_hi = br.get("weight_high")

            height_match_type = None  # None=不查, "perfect"=完美, "range"=范围
            weight_match_type = None

            # 身高匹配判定
            if height is not None:
                if h_lo is not None and h_hi is not None:
                    # 完美匹配：输入值在 [height_low, height_high] 区间内
                    if h_lo <= height <= h_hi:
                        height_match_type = "perfect"
                    else:
                        # 范围匹配：容差 15%
                        h_min = h_lo * 0.85
                        h_max = h_hi * 1.15
                        if h_min <= height <= h_max:
                            height_match_type = "range"
                        else:
                            height_match_type = "none"
                else:
                    height_match_type = "none"

            # 体重匹配判定（Pets.json 中 weight 单位是克，用户输入 kg）
            if weight is not None:
                if w_lo is not None and w_hi is not None:
                    w_kg_lo = w_lo / 1000
                    w_kg_hi = w_hi / 1000
                    # 完美匹配：输入值在 [weight_low, weight_high] 区间内（kg）
                    if w_kg_lo <= weight <= w_kg_hi:
                        weight_match_type = "perfect"
                    else:
                        # 范围匹配：容差 15%
                        w_min = w_kg_lo * 0.85
                        w_max = w_kg_hi * 1.15
                        if w_min <= weight <= w_max:
                            weight_match_type = "range"
                        else:
                            weight_match_type = "none"
                else:
                    weight_match_type = "none"

            # 综合判定
            if height is not None and weight is not None:
                # 双条件：两者都完美才算完美，任一范围就算范围
                if height_match_type == "perfect" and weight_match_type == "perfect":
                    perfect_results.append(p)
                elif height_match_type != "none" and weight_match_type != "none":
                    range_results.append(p)
            elif height is not None:
                if height_match_type == "perfect":
                    perfect_results.append(p)
                elif height_match_type == "range":
                    range_results.append(p)
            elif weight is not None:
                if weight_match_type == "perfect":
                    perfect_results.append(p)
                elif weight_match_type == "range":
                    range_results.append(p)

        return {
            "perfect": perfect_results[:20],
            "range": range_results[:20],
        }

    def build_size_search_text(self, height: float = None, weight: float = None, results: dict = None, cmd_prefix: str = "/") -> str:
        """构建身高/体重反查结果文本（区分完美匹配和范围匹配）"""
        cond = []
        if height is not None:
            cond.append(f"身高={height}cm")
        if weight is not None:
            cond.append(f"体重={weight}kg")
        cond_str = " + ".join(cond)

        if not results or (not results["perfect"] and not results["range"]):
            return f"❌ 未找到符合 {cond_str} 的精灵。"

        lines = []

        # 完美匹配
        if results["perfect"]:
            lines.append(f"✅ 完美匹配 {cond_str} 的精灵（共 {len(results['perfect'])} 只）：")
            for i, p in enumerate(results["perfect"][:10], 1):
                zh = self._name(p)
                br = p.get("breeding") or {}
                h_str = self._fmt_range(br.get("height_low"), br.get("height_high"), "cm")
                w_str = self._fmt_range(self._wt(br.get("weight_low")), self._wt(br.get("weight_high")), "kg")
                egs = format_egg_groups(self.get_egg_groups(p))
                lines.append(f"  {i}. {zh} (#{p['id']}) — {h_str} / {w_str} · {egs}")
            if len(results["perfect"]) > 10:
                lines.append(f"  ... 还有 {len(results['perfect'])-10} 个结果")

        # 范围匹配
        if results["range"]:
            if lines:
                lines.append("")
            lines.append(f"🔍 范围匹配 {cond_str} 的精灵（共 {len(results['range'])} 只，容差±15%）：")
            for i, p in enumerate(results["range"][:10], 1):
                zh = self._name(p)
                br = p.get("breeding") or {}
                h_str = self._fmt_range(br.get("height_low"), br.get("height_high"), "cm")
                w_str = self._fmt_range(self._wt(br.get("weight_low")), self._wt(br.get("weight_high")), "kg")
                egs = format_egg_groups(self.get_egg_groups(p))
                lines.append(f"  {i}. {zh} (#{p['id']}) — {h_str} / {w_str} · {egs}")
            if len(results["range"]) > 10:
                lines.append(f"  ... 还有 {len(results['range'])-10} 个结果")

        lines.append(f"\n💡 {cmd_prefix}洛克查蛋 <精灵名> 查看详细蛋组信息")
        return "\n".join(lines)

    # ── 配种结果查询 ──

    def get_breeding_parents(self, pet: dict) -> List[dict]:
        """
        想要孵出指定精灵，需要哪些父母组合？
        规则：母体决定孵出结果，所以母体必须是目标精灵（或其进化链最低形态）。
        返回所有可作为父体的精灵列表。
        """
        egg_groups = set(self.get_egg_groups(pet))
        if not egg_groups or 1 in egg_groups:
            return []
        fathers = []
        for o in self._pets:
            if o["id"] == pet["id"]:
                continue
            og = set(self.get_egg_groups(o))
            if not og or 1 in og:
                continue
            if egg_groups & og:
                fathers.append(o)
        return fathers

    def build_want_pet_text(self, pet: dict, cmd_prefix: str = "/") -> str:
        """构建「想要某精灵需要怎么配」的文本"""
        zh = self._name(pet)
        egs = self.get_egg_groups(pet)
        bp = pet.get("breeding_profile") or {}
        female_rate = bp.get("female_rate")
        male_rate = bp.get("male_rate")

        lines = [f"🥚 想要孵出「{zh}」："]
        lines.append(f"蛋组：{format_egg_groups(egs)}")

        if 1 in egs:
            lines.append("⚠️ 该精灵属于「未发现」蛋组，无法通过配种获得。")
            return "\n".join(lines)

        lines.append(f"\n📌 母体必须是「{zh}」（孵蛋结果跟随母体）")
        if female_rate is not None:
            lines.append(f"   母体概率：{female_rate}%")
            if female_rate <= 0:
                lines.append(f"   ⚠️ 该精灵雌性概率为 0%，可能无法作为母体！")

        fathers = self.get_breeding_parents(pet)
        if fathers:
            lines.append(f"\n🔗 可选父体（共 {len(fathers)} 只，需雄性）：")
            for i, f in enumerate(fathers[:15], 1):
                f_bp = f.get("breeding_profile") or {}
                f_male = f_bp.get("male_rate")
                male_hint = f" (♂{f_male}%)" if f_male is not None else ""
                lines.append(f"  {i}. {self._name(f)}{male_hint} — {format_egg_groups(self.get_egg_groups(f))}")
            if len(fathers) > 15:
                lines.append(f"  ... 还有 {len(fathers)-15} 只")
        else:
            lines.append("\n❌ 未找到可配种的父体精灵。")

        lines.append(f"\n💡 {cmd_prefix}洛克配种 <父体> <母体> 查看详细配种结果")
        return "\n".join(lines)

    # ── 搜索 ──

    def search(self, keyword: str) -> SearchResult:
        """
        智能搜索：
        1. 精确匹配中文名 / ID / 英文名
        2. 单一模糊命中 → 视为精确
        3. 多个模糊命中 → 返回候选列表
        4. 无命中 → NOT_FOUND
        """
        kw = keyword.strip()
        if not kw:
            return SearchResult(SearchResult.NOT_FOUND)

        # 1) 精确：中文名
        if kw in self._by_zh:
            return SearchResult(SearchResult.EXACT, pet=self._by_zh[kw])

        # 2) 精确：ID
        try:
            pid = int(kw)
            if pid in self._by_id:
                return SearchResult(SearchResult.EXACT, pet=self._by_id[pid])
        except ValueError:
            pass

        # 3) 精确：英文名（不区分大小写）
        if kw.lower() in self._by_en:
            return SearchResult(SearchResult.EXACT, pet=self._by_en[kw.lower()])

        # 4) 模糊匹配
        kw_lower = kw.lower()
        hits = []
        for p in self._pets:
            zh = p.get("localized", {}).get("zh", {}).get("name", "")
            en = p.get("name", "")
            if kw_lower in zh.lower() or kw_lower in en.lower():
                hits.append(p)

        if len(hits) == 1:
            return SearchResult(SearchResult.FUZZY, pet=hits[0])
        if len(hits) > 1:
            return SearchResult(SearchResult.MULTI, candidates=hits[:20])

        return SearchResult(SearchResult.NOT_FOUND)

    # ── 蛋组 / 配种 ──

    def get_egg_groups(self, pet: dict) -> List[int]:
        bp = pet.get("breeding_profile")
        return bp.get("egg_groups", []) if bp else []

    def get_compatible_pets(self, pet: dict) -> List[dict]:
        groups = set(self.get_egg_groups(pet))
        if not groups or 1 in groups:
            return []
        out = []
        for o in self._pets:
            if o["id"] == pet["id"]:
                continue
            og = set(self.get_egg_groups(o))
            if not og or 1 in og:
                continue
            if groups & og:
                out.append(o)
        return out

    def evaluate_pair(self, a: dict, b: dict) -> dict:
        ga, gb = set(self.get_egg_groups(a)), set(self.get_egg_groups(b))
        shared = sorted(ga & gb)
        reasons = []
        if not ga:
            reasons.append(f"{self._name(a)} 暂无蛋组数据")
        if not gb:
            reasons.append(f"{self._name(b)} 暂无蛋组数据")
        if 1 in ga:
            reasons.append(f"{self._name(a)} 属于「未发现」蛋组")
        if 1 in gb:
            reasons.append(f"{self._name(b)} 属于「未发现」蛋组")
        if not shared and not reasons:
            reasons.append("蛋组不相同，无法配种")
        br = a.get("breeding") or {}
        return {
            "compatible": not reasons and len(shared) > 0,
            "reasons": reasons,
            "shared_egg_groups": shared,
            "shared_egg_group_labels": [get_egg_group_label(g) for g in shared],
            "hatch_label": self._fmt_dur(br.get("hatch_data")),
            "weight_label": self._fmt_range(self._wt(br.get("weight_low")), self._wt(br.get("weight_high")), "kg"),
            "height_label": self._fmt_range(br.get("height_low"), br.get("height_high"), "cm"),
        }

    # ── 构建渲染数据 ──

    def build_search_data(self, pet: dict) -> dict:
        egs = self.get_egg_groups(pet)
        compat = self.get_compatible_pets(pet)
        gmap: Dict[int, List[dict]] = {gid: [] for gid in egs if gid != 1}
        for c in compat:
            for gid in egs:
                if gid in self.get_egg_groups(c) and gid != 1:
                    gmap.setdefault(gid, []).append(c)
        sections = []
        for gid in egs:
            meta = EGG_GROUP_META.get(gid, {})
            members = gmap.get(gid, [])
            sections.append({
                "id": gid,
                "label": meta.get("label", f"蛋组{gid}"),
                "desc": meta.get("desc", ""),
                "count": len(members),
                "members": [{"name": self._name(m), "id": m["id"],
                             "type_label": self._type(m),
                             "egg_groups_label": format_egg_groups(self.get_egg_groups(m))}
                            for m in members[:30]],
                "has_more": len(members) > 30,
                "total": len(members),
            })
        br = pet.get("breeding") or {}
        bp = pet.get("breeding_profile") or {}

        # 蛋详细数据
        egg_details = self._build_egg_details(br)

        return {
            "pet_name": self._name(pet), "pet_id": pet["id"],
            "pet_icon": self._pet_icon_url(pet["id"]),
            "pet_image": self._pet_image_url(pet["id"]),
            "type_label": self._type(pet),
            "egg_groups_label": format_egg_groups(egs),
            "egg_groups": egs,
            "egg_group_labels": {gid: get_egg_group_label(gid) for gid in egs},
            "male_rate": bp.get("male_rate"), "female_rate": bp.get("female_rate"),
            "hatch_label": self._fmt_dur(br.get("hatch_data")),
            "weight_label": self._fmt_range(self._wt(br.get("weight_low")), self._wt(br.get("weight_high")), "kg"),
            "height_label": self._fmt_range(br.get("height_low"), br.get("height_high"), "cm"),
            "total_compatible": len(compat),
            "is_undiscovered": 1 in egs,
            "egg_group_sections": sections,
            "total_stats": sum(pet.get(k, 0) for k in
                               ["base_hp","base_phy_atk","base_mag_atk","base_phy_def","base_mag_def","base_spd"]),
            "egg_details": egg_details,
        }

    def _build_egg_details(self, breeding: dict) -> dict:
        """构建蛋详细数据"""
        if not breeding:
            return {"has_data": False}

        # 基础异色概率
        base_prob_array = breeding.get("egg_base_glass_prob_array")
        if base_prob_array and len(base_prob_array) == 2:
            base_prob = base_prob_array[0] / base_prob_array[1]
            base_prob_pct = base_prob * 100
            base_prob_str = f"{base_prob_array[0]}/{base_prob_array[1]}"
        else:
            base_prob = None
            base_prob_pct = None
            base_prob_str = "暂无数据"

        # 额外异色概率
        add_prob_array = breeding.get("egg_add_glass_prob_array")
        if add_prob_array and len(add_prob_array) == 2:
            add_prob = add_prob_array[0] / add_prob_array[1]
            add_prob_pct = add_prob * 100
            add_prob_str = f"{add_prob_array[0]}/{add_prob_array[1]}"
        else:
            add_prob = None
            add_prob_pct = None
            add_prob_str = "暂无数据"

        # 异色概率说明
        is_contact_add_glass = breeding.get("is_contact_add_glass_prob")
        is_contact_add_shining = breeding.get("is_contact_add_shining_prob")

        # 珍稀蛋类型
        precious_egg_type = breeding.get("precious_egg_type")
        precious_egg_label = self._get_precious_egg_label(precious_egg_type)

        # 变体数据
        variants = breeding.get("variants") or []
        variant_list = []
        for v in variants:
            variant_info = {
                "id": v.get("id"),
                "name": v.get("name", ""),
                "hatch_label": self._fmt_dur(v.get("hatch_data")),
                "weight_label": self._fmt_range(self._wt(v.get("weight_low")), self._wt(v.get("weight_high")), "kg"),
                "height_label": self._fmt_range(v.get("height_low"), v.get("height_high"), "cm"),
                "precious_egg_type": v.get("precious_egg_type"),
                "precious_egg_label": self._get_precious_egg_label(v.get("precious_egg_type")),
            }
            # 变体的异色概率
            v_base = v.get("egg_base_glass_prob_array")
            if v_base and len(v_base) == 2:
                variant_info["base_prob_str"] = f"{v_base[0]}/{v_base[1]}"
            else:
                variant_info["base_prob_str"] = "暂无"
            variant_list.append(variant_info)

        return {
            "has_data": True,
            "base_prob_str": base_prob_str,
            "base_prob_pct": base_prob_pct,
            "add_prob_str": add_prob_str,
            "add_prob_pct": add_prob_pct,
            "is_contact_add_glass": is_contact_add_glass,
            "is_contact_add_shining": is_contact_add_shining,
            "precious_egg_type": precious_egg_type,
            "precious_egg_label": precious_egg_label,
            "variants": variant_list,
            "variant_count": len(variant_list),
        }

    def _get_precious_egg_label(self, egg_type) -> str:
        """获取珍稀蛋类型标签"""
        if egg_type is None:
            return "普通蛋"
        precious_map = {
            1: "迪莫蛋",
            2: "星辰蛋",
            3: "彩虹蛋",
            4: "梦幻蛋",
            5: "传说蛋",
            6: "神秘蛋",
            7: "特殊蛋",
        }
        return precious_map.get(egg_type, f"珍稀蛋(类型{egg_type})")

    def build_pair_data(self, a: dict, b: dict) -> dict:
        ev = self.evaluate_pair(a, b)
        return {
            "mother": {"name": self._name(a), "id": a["id"],
                       "type_label": self._type(a),
                       "egg_groups_label": format_egg_groups(self.get_egg_groups(a))},
            "father": {"name": self._name(b), "id": b["id"],
                       "type_label": self._type(b),
                       "egg_groups_label": format_egg_groups(self.get_egg_groups(b))},
            **ev,
        }

    def build_candidates_text(self, keyword: str, candidates: List[dict], cmd_prefix: str = "/") -> str:
        """构建多候选的友好提示文本"""
        lines = [f"🔍 「{keyword}」匹配到 {len(candidates)} 只精灵，请精确输入："]
        for i, p in enumerate(candidates[:10], 1):
            zh = self._name(p)
            egs = format_egg_groups(self.get_egg_groups(p))
            lines.append(f"  {i}. {zh} (#{p['id']}) — {self._type(p)} · {egs}")
        if len(candidates) > 10:
            lines.append(f"  ... 还有 {len(candidates)-10} 个结果")
        lines.append(f"\n💡 请使用精确名称重新查询，如：{cmd_prefix}洛克查蛋 喵喵")
        return "\n".join(lines)

    # ── 工具 ──

    def _name(self, p: dict) -> str:
        return p.get("localized", {}).get("zh", {}).get("name", p.get("name", "???"))

    def _type(self, p: dict) -> str:
        parts = []
        mt = p.get("main_type", {}).get("localized", {}).get("zh", "")
        if mt: parts.append(mt)
        st = (p.get("sub_type") or {}).get("localized", {}).get("zh", "")
        if st: parts.append(st)
        return " / ".join(parts) or "未知"

    @staticmethod
    def _fmt_dur(s) -> str:
        if not s or s <= 0: return "暂无数据"
        if s % 86400 == 0: return f"{s//86400} 天"
        h = s / 3600
        return f"{int(h)} 小时" if h == int(h) else f"{h:.1f} 小时"

    @staticmethod
    def _wt(v) -> Optional[float]:
        return round(v/1000, 1) if v is not None else None

    @staticmethod
    def _fmt_range(lo, hi, u: str) -> str:
        if lo is None and hi is None: return "暂无数据"
        if lo is not None and hi is not None:
            return f"{lo}{u}" if lo == hi else f"{lo}-{hi}{u}"
        return f"{lo or hi}{u}"

    @staticmethod
    def _asset_pet_id(pet_id) -> Optional[int]:
        try:
            numeric_id = int(pet_id)
        except (TypeError, ValueError):
            return None
        return numeric_id if numeric_id >= 3000 else numeric_id + 3000

    def _pet_icon_url(self, pet_id) -> str:
        asset_id = self._asset_pet_id(pet_id)
        if asset_id is None:
            return "{{_res_path}}img/roco_icon.png"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{asset_id}/icon.png"

    def _pet_image_url(self, pet_id) -> str:
        asset_id = self._asset_pet_id(pet_id)
        if asset_id is None:
            return "{{_res_path}}img/roco_icon.png"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{asset_id}/image.png"
