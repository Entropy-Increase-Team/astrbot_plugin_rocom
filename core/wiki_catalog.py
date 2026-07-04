from __future__ import annotations

from typing import Any, Dict, List


def _route(
    key: str,
    title: str,
    aliases: list[str],
    detail_path: str = "",
    id_fields: list[str] | None = None,
    search: bool | None = None,
) -> Dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "aliases": aliases,
        "detail_path": detail_path,
        "id_fields": id_fields or [],
        "search": search,
    }


WIKI_CATALOG_ROUTES: List[Dict[str, Any]] = [
    _route("pets", "精灵", ["精灵", "宠物", "pet", "pets"], "/api/v1/games/rocom/wiki/pets/{pet_id}", ["pet_id"]),
    _route("pet-skills", "精灵技能", ["精灵技能", "宠物技能"], "/api/v1/games/rocom/wiki/pets/{pet_id}/skills", ["pet_id"], False),
    _route("pet-features", "精灵特性", ["精灵特性", "宠物特性"], "/api/v1/games/rocom/wiki/pets/{pet_id}/features", ["pet_id"], False),
    _route("pet-family", "精灵家族", ["精灵家族", "进化链", "形态链"], "/api/v1/games/rocom/wiki/pets/{pet_id}/family", ["pet_id"], False),
    _route("pet-egg", "精灵蛋详情", ["精灵蛋详情", "宠物蛋详情"], "/api/v1/games/rocom/wiki/pets/{pet_id}/egg", ["pet_id"], False),
    _route("pet-handbook", "精灵图鉴课题", ["精灵图鉴课题", "图鉴课题"], "/api/v1/games/rocom/wiki/pets/{pet_id}/handbook", ["pet_id"], False),
    _route("pet-talents", "精灵天赋", ["精灵天赋", "同乘天赋"], "/api/v1/games/rocom/wiki/pets/{pet_id}/talents", ["pet_id"], False),
    _route("pet-natures", "精灵性格", ["精灵性格", "性格"], "/api/v1/games/rocom/wiki/pets/{pet_id}/natures", ["pet_id"], False),
    _route("pet-profile", "精灵资料", ["精灵资料", "资料页"], "/api/v1/games/rocom/wiki/pets/{pet_id}/profile", ["pet_id"], False),
    _route("pet-fruit-section", "精灵关联果实", ["精灵关联果实"], "/api/v1/games/rocom/wiki/pets/{pet_id}/fruit", ["pet_id"], False),
    _route("pet-ride", "精灵骑乘", ["精灵骑乘", "骑乘"], "/api/v1/games/rocom/wiki/pets/{pet_id}/ride", ["pet_id"], False),
    _route("pet-ecology", "精灵生态", ["精灵生态", "生态"], "/api/v1/games/rocom/wiki/pets/{pet_id}/ecology", ["pet_id"], False),
    _route("pet-stories", "精灵故事", ["精灵故事", "故事"], "/api/v1/games/rocom/wiki/pets/{pet_id}/stories", ["pet_id"], False),
    _route("pet-acquisition", "精灵获取方式", ["精灵获取", "获取方式"], "/api/v1/games/rocom/wiki/pets/{pet_id}/acquisition", ["pet_id"], False),
    _route("skills", "技能", ["技能", "skill", "skills"], "/api/v1/games/rocom/wiki/skills/{skill_id}", ["skill_id"]),
    _route("skill-pets", "技能可用精灵", ["技能可用精灵", "技能精灵"], "/api/v1/games/rocom/wiki/skills/{skill_id}/pets", ["skill_id"], False),
    _route("skill-stones", "技能石", ["技能石", "skillstone", "skill-stone"], "/api/v1/games/rocom/wiki/skill-stones/{item_id}", ["item_id"]),
    _route("skill-stone-recipes", "技能石配方", ["技能石配方", "技能配方", "skillstonerecipe"], "/api/v1/games/rocom/wiki/skill-stone-recipes/{item_id}", ["item_id"]),
    _route("items", "物品", ["物品", "道具", "item", "items"], "/api/v1/games/rocom/wiki/items/{item_kind}/{item_id}", ["item_kind", "item_id"]),
    _route("egg-items", "蛋物品", ["蛋物品", "蛋道具", "egg-item"], "/api/v1/games/rocom/wiki/egg-items/{item_id}", ["item_id"]),
    _route("pet-eggs", "精灵蛋", ["精灵蛋", "宠物蛋", "蛋图鉴", "pet-egg"], "/api/v1/games/rocom/wiki/pet-eggs/{egg_conf_id}", ["egg_conf_id"]),
    _route("random-eggs", "随机蛋", ["随机蛋", "随机蛋池", "random-egg"], "/api/v1/games/rocom/wiki/random-eggs/{random_egg_id}", ["random_egg_id"]),
    _route("pet-fruits", "精灵果实", ["精灵果实", "果实", "pet-fruit"], "/api/v1/games/rocom/wiki/pet-fruits/{item_id}", ["item_id"]),
    _route("pet-foods", "精灵食物", ["精灵食物", "食物", "pet-food"], "/api/v1/games/rocom/wiki/pet-foods/{item_id}", ["item_id"]),
    _route("pet-gifts", "精灵礼物", ["精灵礼物", "礼物", "pet-gift"], "/api/v1/games/rocom/wiki/pet-gifts/{item_id}", ["item_id"]),
    _route("pet-close-level-effects", "亲密等级效果", ["亲密等级", "亲密效果"], "", ["level"], False),
    _route("pet-action-close-exp", "行为亲密经验", ["行为亲密", "亲密经验"], "", [], False),
    _route("home-egg-lay-rates", "家园产蛋概率", ["家园产蛋", "产蛋概率"], "", [], False),
    _route("home-progression", "家园成长", ["家园成长", "家园升级"], "", [], False),
    _route("pet-levels", "精灵等级经验", ["精灵等级", "等级经验", "pet-level"], "/api/v1/games/rocom/wiki/pet-levels/{level}", ["level"], False),
    _route("handbook-areas", "图鉴区域", ["图鉴区域", "区域图鉴"], "", ["id"], False),
    _route("fruit-trees", "果实树", ["果实树", "树"], "/api/v1/games/rocom/wiki/fruit-trees/{tree_id}", ["tree_id"]),
    _route("fruit-refresh", "果实刷新精灵", ["果实刷新", "刷新精灵"], "/api/v1/games/rocom/wiki/fruit-refresh/{fruit_item_id}", ["fruit_item_id"], False),
    _route("medals", "奖章", ["奖章", "勋章", "medal"], "/api/v1/games/rocom/wiki/medals/{medal_id}", ["medal_id"]),
    _route("recipes", "食谱", ["食谱", "料理", "recipe"], "/api/v1/games/rocom/wiki/recipes/{item_id}", ["item_id"]),
    _route("balls", "咕噜球", ["咕噜球", "球", "ball"], "/api/v1/games/rocom/wiki/balls/{ball_id}", ["ball_id"]),
    _route("plants", "种植", ["种植", "作物", "植物", "plant"], "/api/v1/games/rocom/wiki/plants/{plant_id}", ["plant_id"]),
    _route("pet-carryons", "精灵携带物", ["精灵携带物", "携带物", "carryon"], "/api/v1/games/rocom/wiki/pet-carryons/{carryon_id}", ["carryon_id"]),
    _route("furniture", "家具", ["家具", "furniture"], "/api/v1/games/rocom/wiki/furniture/{furniture_id}", ["furniture_id"]),
    _route("fashion", "服装套装", ["服装", "套装", "fashion"], "/api/v1/games/rocom/wiki/fashion/{suit_id}", ["suit_id"]),
    _route("regions", "地区", ["地区", "区域", "region"], "/api/v1/games/rocom/wiki/regions/{region_id}", ["region_id"]),
    _route("dungeons", "副本", ["副本", "dungeon"], "/api/v1/games/rocom/wiki/dungeons/{dungeon_id}", ["dungeon_id"]),
    _route("mails", "邮件模板", ["邮件", "邮件模板", "mail"], "/api/v1/games/rocom/wiki/mails/{mail_id}", ["mail_id"]),
    _route("music", "音乐", ["音乐", "music"], "/api/v1/games/rocom/wiki/music/{music_id}", ["music_id"]),
    _route("profile-assets", "个人名片资产", ["名片", "名片资产", "profile-asset"], "/api/v1/games/rocom/wiki/profile-assets/{asset_type}/{asset_id}", ["asset_type", "asset_id"]),
    _route("chat-emojis", "聊天表情", ["表情", "聊天表情", "emoji"], "/api/v1/games/rocom/wiki/chat-emojis/{emoji_id}", ["emoji_id"]),
    _route("photo-actions", "拍照动作", ["拍照动作", "动作", "photo-action"], "/api/v1/games/rocom/wiki/photo-actions/{action_type}/{action_id}", ["action_type", "action_id"]),
    _route("tasks", "任务", ["任务", "task"], "/api/v1/games/rocom/wiki/tasks/{task_id}", ["task_id"]),
    _route("task-summaries", "剧情摘要", ["剧情", "剧情摘要", "task-summary"], "/api/v1/games/rocom/wiki/task-summaries/{summary_id}", ["summary_id"]),
    _route("shops", "商店", ["商店", "shop"], "/api/v1/games/rocom/wiki/shops/{shop_id}", ["shop_id"]),
    _route("exchanges", "兑换", ["兑换", "exchange"], "/api/v1/games/rocom/wiki/exchanges/{exchange_id}", ["exchange_id"]),
]


WIKI_CATALOG_ROUTES_BY_KEY = {item["key"]: item for item in WIKI_CATALOG_ROUTES}
WIKI_CATALOG_ROUTES_BY_ALIAS = {
    alias.lower(): item
    for item in WIKI_CATALOG_ROUTES
    for alias in [item["key"], item["title"], *item["aliases"]]
}
