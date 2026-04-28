#!/usr/bin/env python3
"""
通用分类爬虫 - 从 Wiki 分类中获取页面列表
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import API_URL, fetch_with_retry


def fetch_category_members(category: str, cmcontinue=None):
    """
    递归获取分类成员
    
    Args:
        category: 分类名称（如 "Category:道具"）
        cmcontinue: 继续标记，用于分页
    
    Returns:
        页面名称列表
    """
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category,
        "cmlimit": "500",
        "format": "json",
    }
    
    if cmcontinue:
        params["cmcontinue"] = cmcontinue
    
    response = fetch_with_retry(API_URL, params=params)
    if not response:
        print(f"  ⚠️ 警告: 获取分类 '{category}' 失败，无响应")
        return []
    
    # 检查响应状态码
    if response.status_code != 200:
        print(f"  ⚠️ 警告: 获取分类 '{category}' 失败，HTTP {response.status_code}")
        return []
    
    # 检查响应内容是否为空
    if not response.text or len(response.text.strip()) == 0:
        print(f"  ⚠️ 警告: 获取分类 '{category}' 失败，响应为空")
        return []
    
    try:
        data = response.json()
    except Exception as e:
        print(f"  ⚠️ 警告: 解析分类 '{category}' JSON 失败: {e}")
        print(f"  响应内容前200字符: {response.text[:200]}")
        return []
    names = [
        member["title"] 
        for member in data.get("query", {}).get("categorymembers", [])
        if not member["title"].startswith("模板:") and not member["title"].startswith("Data:")
    ]
    
    # 如果有更多数据，递归获取
    if "continue" in data and "cmcontinue" in data["continue"]:
        names.extend(fetch_category_members(category, data["continue"]["cmcontinue"]))
    
    return names


def crawl_category(category_name: str, display_name: str = None):
    """
    爬取指定分类的所有页面
    
    Args:
        category_name: 分类名称（如 "道具"）
        display_name: 显示名称
    
    Returns:
        页面名称列表
    """
    if display_name is None:
        display_name = category_name
    
    print(f"正在获取 {display_name} 列表...")
    category = f"Category:{category_name}"
    names = fetch_category_members(category)
    print(f"找到 {len(names)} 个 {display_name}")
    return names


if __name__ == "__main__":
    import json
    
    # 测试
    categories = ["道具", "精灵蛋", "家具"]
    for cat in categories:
        items = crawl_category(cat)
        print(json.dumps(items[:5], ensure_ascii=False, indent=2))
        print()
