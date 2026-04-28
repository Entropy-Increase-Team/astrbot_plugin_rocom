"""
宠物索引爬虫
从Wiki分类中获取所有宠物名称
"""
from src.config import API_URL, fetch_with_retry


def fetch_category_members(cmcontinue=None):
    """
    递归获取分类成员
    
    Args:
        cmcontinue: 继续标记，用于分页
    
    Returns:
        宠物名称列表
    """
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:精灵",
        "cmlimit": "500",
        "format": "json",
    }
    
    if cmcontinue:
        params["cmcontinue"] = cmcontinue
    
    response = fetch_with_retry(API_URL, params=params)
    if not response:
        return []
    
    data = response.json()
    names = [
        member["title"] 
        for member in data.get("query", {}).get("categorymembers", [])
        if not member["title"].startswith("模板:")
    ]
    
    # 如果有更多数据，递归获取
    if "continue" in data and "cmcontinue" in data["continue"]:
        names.extend(fetch_category_members(data["continue"]["cmcontinue"]))
    
    return names


def crawl_all_pets():
    """
    爬取所有宠物索引
    
    Returns:
        宠物名称列表
    """
    print("正在获取宠物列表...")
    names = fetch_category_members()
    print(f"找到 {len(names)} 个宠物")
    return names


if __name__ == "__main__":
    import json
    pets = crawl_all_pets()
    print(json.dumps(pets[:10], ensure_ascii=False, indent=2))
