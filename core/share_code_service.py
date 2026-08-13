from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
from urllib.parse import unquote


CN_TZ = timezone(timedelta(hours=8))


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def _format_time(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CN_TZ)
        return parsed.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def extract_share_code(raw_value: Any) -> str:
    """接受原始分享码，或从包含 shareData 的页面链接中提取分享码。"""
    raw = _text(raw_value).strip("`\"'")
    if not raw:
        return ""

    match = re.search(r"(?:[?&#]|^)share(?:Data|_data|_code)=([^&#\s]+)", raw, re.I)
    if match:
        value = match.group(1)
        for _ in range(2):
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
        return value.strip().strip("`\"'")
    return raw


def _normalize_url(value: Any, base_url: str) -> str:
    url = _text(value)
    if url.startswith(("http://", "https://", "data:")):
        return url
    if url.startswith("/") and base_url:
        return f"{str(base_url).rstrip('/')}{url}"
    return url


def _normalize_named_icon(
    value: Any,
    empty_name: str,
    base_url: str = "",
) -> Dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    item_id = _to_int(item.get("id"))
    return {
        "id": item_id,
        "name": _text(item.get("name"), empty_name),
        "icon": _normalize_url(item.get("icon"), base_url),
        "configured": bool(item_id or _text(item.get("name")) or _text(item.get("icon"))),
    }


def _wiki_asset_url(base_url: str, path: str) -> str:
    if not base_url:
        return path
    return f"{str(base_url).rstrip('/')}{path}"


def _normalize_team(value: Any, index: int, base_url: str) -> Dict[str, Any]:
    team = value if isinstance(value, dict) else {}
    pet = _normalize_named_icon(team.get("pet"), "未配置精灵", base_url)
    if pet["id"]:
        pet["icon"] = _wiki_asset_url(
            base_url,
            f"/api/v1/resources/wiki/assets/pets/{pet['id']}/icon.png",
        )
    bloodline = _normalize_named_icon(
        team.get("bloodline"), "未配置血脉", base_url
    )
    personality = _normalize_named_icon(
        team.get("personality"), "未配置性格", base_url
    )

    ivs: List[Dict[str, Any]] = []
    for raw in team.get("ivs_detail") or []:
        ivs.append(_normalize_named_icon(raw, "未配置", base_url))
    while len(ivs) < 3:
        ivs.append(_normalize_named_icon(None, "未配置", base_url))

    skills: List[Dict[str, Any]] = []
    for raw in team.get("skills") or []:
        skill = _normalize_named_icon(raw, "未配置技能", base_url)
        if skill["id"]:
            skill["icon"] = _wiki_asset_url(
                base_url,
                f"/api/v1/resources/wiki/assets/skills/{skill['id']}.png",
            )
        skills.append(skill)
    while len(skills) < 4:
        skills.append(_normalize_named_icon(None, "未配置技能", base_url))

    return {
        "slot": _to_int(team.get("slot"), index + 1),
        "pet": pet,
        "bloodline": bloodline,
        "personality": personality,
        "ivs": ivs[:3],
        "skills": skills[:4],
        "empty": not pet["configured"],
    }


def _unwrap_record(record: Dict[str, Any] | None) -> tuple[Dict[str, Any], Dict[str, Any]]:
    record = record if isinstance(record, dict) else {}
    nested = record.get("share_code")
    if isinstance(nested, dict):
        return nested, record
    parsed = record.get("parsed")
    if isinstance(parsed, dict):
        return parsed, record
    return record, {}


def _share_code_preview(code: str) -> str:
    if len(code) <= 36:
        return code
    return f"{code[:20]}...{code[-12:]}"


def build_share_code_render_data(
    payload: Dict[str, Any] | None,
    source: str,
    record: Dict[str, Any] | None = None,
    base_url: str = "",
) -> Dict[str, Any]:
    if record:
        record_payload, record_meta = _unwrap_record(record)
        if not isinstance(payload, dict) or not payload.get("teams"):
            payload = record_payload
    else:
        payload = payload if isinstance(payload, dict) else {}
        record_meta = {}

    teams = [
        _normalize_team(item, index, base_url)
        for index, item in enumerate(payload.get("teams") or [])
    ]
    teams.sort(key=lambda item: item["slot"])
    share_code = _text(payload.get("share_code"))
    mode = _normalize_named_icon(payload.get("mode"), "未知编队模式", base_url)
    magic = _normalize_named_icon(payload.get("magic"), "未配置魔法", base_url)
    if magic["configured"] and magic["name"] == "未配置魔法":
        magic["name"] = f"魔法 {magic['id']}"
    sprite_count = _to_int(payload.get("sprite_count"), len(teams))

    return {
        "source": source,
        "sourceLabel": "历史记录" if source == "record" else "即时解析",
        "shareCode": share_code,
        "shareCodePreview": _share_code_preview(share_code),
        "version": _to_int(payload.get("version"), 1),
        "spriteCount": sprite_count,
        "mode": mode,
        "magic": magic,
        "teams": teams,
        "parseCount": _to_int(record_meta.get("parse_count")),
        "firstSeen": _format_time(record_meta.get("first_seen_at")),
        "lastSeen": _format_time(record_meta.get("last_seen_at")),
        "shareCodeHash": _text(record_meta.get("share_code_hash")),
        "commandHint": "/阵容码 解析 <分享码或链接> · /阵容码 查询 <分享码>",
        "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
    }


def build_share_code_text(data: Dict[str, Any]) -> str:
    lines = [
        f"阵容码{data.get('sourceLabel', '解析')}："
        f"{data.get('mode', {}).get('name', '未知模式')}，"
        f"共 {data.get('spriteCount', 0)} 只精灵"
    ]
    for team in data.get("teams") or []:
        skill_names = [
            item["name"] for item in team.get("skills") or [] if item.get("configured")
        ]
        lines.append(
            f"{team['slot']}. {team['pet']['name']} | "
            f"{team['bloodline']['name']} | {team['personality']['name']} | "
            f"技能：{'、'.join(skill_names) or '未配置'}"
        )
    if data.get("parseCount"):
        lines.append(
            f"解析次数：{data['parseCount']}，最近记录：{data.get('lastSeen') or '未知'}"
        )
    return "\n".join(lines)
