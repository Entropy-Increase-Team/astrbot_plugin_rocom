"""
技能索引爬虫
从Wiki分类中获取所有技能名称
"""
from src.config import API_URL, fetch_with_retry


def fetch_category_members(cmcontinue=None):
    """
    递归获取分类成员
    
    Args:
        cmcontinue: 继续标记，用于分页
    
    Returns:
        技能名称列表
    """
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:技能",
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


def crawl_skill_index():
    """
    爬取所有技能索引
    
    Returns:
        技能名称列表
    """
    print("正在获取技能列表...")
    names = fetch_category_members()
    print(f"找到 {len(names)} 个技能")
    return names


if __name__ == "__main__":
    skills = crawl_skill_index()
    import json
    print(json.dumps(skills[:10], ensure_ascii=False, indent=2))
