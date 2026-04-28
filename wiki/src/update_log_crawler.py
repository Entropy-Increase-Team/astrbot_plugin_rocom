"""
洛克王国Wiki更新日志爬虫
爬取平衡调整说明、版本更新等内容
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
from typing import Optional, List, Dict
from datetime import datetime
from src.config import BASE_URL, fetch_with_retry


def crawl_update_log(title: str) -> Optional[Dict]:
    """
    爬取单个更新日志页面
    
    Args:
        title: 页面标题，如 "9月5日平衡调整说明"
    
    Returns:
        更新日志数据字典，失败返回None
    """
    try:
        # 获取原始wikitext
        url = f"{BASE_URL}/index.php"
        params = {"title": title, "action": "raw"}
        response = fetch_with_retry(url, params=params)
        
        if not response or response.status_code != 200:
            print(f"❌ 无法获取页面: {title}")
            return None
        
        wikitext = response.text
        
        # 提取日期（从标题中提取）
        date_match = re.search(r'(\d+月\d+日)', title)
        date_str = date_match.group(1) if date_match else ""
        
        # 提取主要内容（去除模板标记）
        content = _extract_content(wikitext)
        
        # 提取关键改动（宠物、技能等）
        changes = _extract_changes(wikitext)
        
        # 构建数据
        update_data = {
            'title': title,
            'date': date_str,
            'content': content[:2000] if content else '',  # 限制长度
            'changes': changes,
            'pet_changes': [c for c in changes if c.get('type') == 'pet'],
            'skill_changes': [c for c in changes if c.get('type') == 'skill'],
            'other_changes': [c for c in changes if c.get('type') == 'other'],
            'created_at': datetime.now().isoformat(),
        }
        
        return update_data
    
    except Exception as e:
        print(f"❌ 爬取更新日志 '{title}' 失败: {e}")
        return None


def _extract_content(wikitext: str) -> str:
    """提取纯文本内容"""
    # 移除模板标记
    content = re.sub(r'\{\{[^}]+\}\}', '', wikitext)
    # 移除文件链接
    content = re.sub(r'\[\[File:[^\]]+\]\]', '', content)
    content = re.sub(r'\[\[文件:[^\]]+\]\]', '', content)
    # 移除分类链接
    content = re.sub(r'\[\[Category:[^\]]+\]\]', '', content)
    content = re.sub(r'\[\[分类:[^\]]+\]\]', '', content)
    # 移除HTML div标签（但保留内部文本）
    content = re.sub(r'<div[^>]*>', '\n', content)
    content = re.sub(r'</div>', '', content)
    # 移除<br>标签
    content = re.sub(r'<br\s*/?>', '\n', content)
    # 清理多余空白
    content = re.sub(r'\n{3,}', '\n\n', content)
    content = content.strip()
    
    return content


def _extract_changes(wikitext: str) -> List[Dict]:
    """提取具体的改动项"""
    changes = []
    
    lines = wikitext.split('\n')
    current_type = 'other'
    
    for line in lines:
        line = line.strip()
        
        # 检测宠物/技能名称（=== 【XXX】 ===）- 必须在段落标题之前检测
        name_match = re.search(r'^===\s*【(.+?)】\s*===$', line)
        if name_match:
            pet_name = name_match.group(1)
            changes.append({
                'type': current_type,
                'name': pet_name,
                'content': f'{pet_name} 进行了调整',
            })
            continue
        
        # 检测段落标题（== XXX ==，但不是 === 【XXX】 ===）
        if line.startswith('==') and line.endswith('==') and not line.startswith('==='):
            heading = line.strip('=')
            if '宠物' in heading or '精灵' in heading:
                current_type = 'pet'
            elif '技能' in heading:
                current_type = 'skill'
            else:
                current_type = 'other'
            continue
    
    return changes


def get_all_update_logs() -> List[str]:
    """
    获取所有更新日志页面标题
    
    Returns:
        页面标题列表
    """
    try:
        # 方法1：通过分类获取
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": "Category:平衡调整",
            "cmlimit": "100",
            "format": "json"
        }
        
        response = fetch_with_retry(f"{BASE_URL}/api.php", params=params)
        if response:
            data = response.json()
            members = data.get("query", {}).get("categorymembers", [])
            titles = [m["title"] for m in members]
            if titles:
                return titles
        
        # 方法2：通过搜索获取
        params = {
            "action": "query",
            "list": "search",
            "srsearch": "平衡调整说明",
            "srlimit": "50",
            "format": "json"
        }
        
        response = fetch_with_retry(f"{BASE_URL}/api.php", params=params)
        if response:
            data = response.json()
            results = data.get("query", {}).get("search", [])
            titles = [r["title"] for r in results]
            # 过滤掉不包含日期或“平衡调整”的页面
            filtered_titles = [t for t in titles if '平衡调整' in t or re.search(r'\d+月\d+日', t)]
            return filtered_titles
        
        return []
    
    except Exception as e:
        print(f"❌ 获取更新日志列表失败: {e}")
        return []


if __name__ == "__main__":
    import json
    
    # 测试
    print("=== 测试爬取更新日志 ===\n")
    
    # 先获取列表
    titles = get_all_update_logs()
    print(f"找到 {len(titles)} 个更新日志:\n")
    for title in titles[:5]:
        print(f"  - {title}")
    
    if titles:
        # 爬取第一个
        print(f"\n爬取: {titles[0]}")
        log = crawl_update_log(titles[0])
        if log:
            print(json.dumps(log, ensure_ascii=False, indent=2))
