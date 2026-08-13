from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List


CN_TZ = timezone(timedelta(hours=8))
RANKING_META = {
    "shining": {
        "title": "异色精灵排行榜",
        "subtitle": "按异色精灵收集数排名",
        "label": "异色",
        "other_label": "炫彩",
        "count_field": "collected_shining_pet_count",
        "other_count_field": "collected_glass_pet_count",
        "theme": "shining",
    },
    "glass": {
        "title": "炫彩精灵排行榜",
        "subtitle": "按炫彩精灵收集数排名",
        "label": "炫彩",
        "other_label": "异色",
        "count_field": "collected_glass_pet_count",
        "other_count_field": "collected_shining_pet_count",
        "theme": "glass",
    },
}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "未知"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CN_TZ)
        return parsed.astimezone(CN_TZ).strftime("%m-%d %H:%M")
    except ValueError:
        return text


def _avatar_url(base_url: str, card_icon: Any) -> str:
    icon_id = str(card_icon or "").strip()
    if not icon_id:
        return ""
    return (
        f"{str(base_url or '').rstrip('/')}"
        f"/api/v1/resources/wiki/assets/profile/avatar/{icon_id}.png"
    )


def _normalize_item(
    item: Dict[str, Any],
    meta: Dict[str, str],
    base_url: str,
) -> Dict[str, Any]:
    rank = _to_int(item.get("rank"))
    return {
        "rank": rank,
        "podiumClass": f"podium-{rank}" if 1 <= rank <= 3 else "",
        "playerName": str(item.get("player_name") or "未记录名称"),
        "avatar": _avatar_url(base_url, item.get("card_icon_selected")),
        "signature": str(item.get("card_signature") or "暂无签名"),
        "primaryCount": _to_int(item.get(meta["count_field"])),
        "secondaryCount": _to_int(item.get(meta["other_count_field"])),
        "sampleCount": _to_int(item.get("sample_count")),
        "lastSeen": _format_time(item.get("last_seen_at")),
    }


def build_ranking_render_data(
    payload: Dict[str, Any] | None,
    rank_type: str,
    base_url: str,
    requested_uid: str = "",
) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    meta = RANKING_META.get(rank_type, RANKING_META["shining"])
    items: List[Dict[str, Any]] = []
    for raw in payload.get("items") or []:
        if isinstance(raw, dict):
            items.append(_normalize_item(raw, meta, base_url))

    current_raw = payload.get("current")
    current = None
    if isinstance(current_raw, dict):
        current = _normalize_item(current_raw, meta, base_url)
        current["uid"] = str(current_raw.get("uid") or requested_uid or "")

    return {
        "title": meta["title"],
        "subtitle": meta["subtitle"],
        "label": meta["label"],
        "otherLabel": meta["other_label"],
        "theme": meta["theme"],
        "total": _to_int(payload.get("total")),
        "items": items,
        "current": current,
        "requestedUid": str(requested_uid or ""),
        "shownCount": len(items),
        "updatedAt": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        "commandHint": (
            f"/{meta['label']}排行榜 [UID] [数量] · 数量支持 1-50"
        ),
        "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
    }


def build_ranking_text(data: Dict[str, Any]) -> str:
    lines = [
        f"{data.get('title', '精灵排行榜')}（共 {data.get('total', 0)} 名）"
    ]
    for item in data.get("items") or []:
        lines.append(
            f"#{item['rank']} {item['playerName']}："
            f"{data.get('label')} {item['primaryCount']}，"
            f"{data.get('otherLabel')} {item['secondaryCount']}"
        )
    current = data.get("current")
    if current:
        lines.append(
            f"我的排名：#{current['rank']} {current['playerName']}，"
            f"{data.get('label')} {current['primaryCount']}"
        )
    elif data.get("requestedUid"):
        lines.append(f"UID {data['requestedUid']} 暂无排行榜记录。")
    return "\n".join(lines)
