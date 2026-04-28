#!/usr/bin/env python3
"""
道具详情爬虫
从 Wiki 页面提取道具信息
"""
import sys
import os
import re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import BASE_URL, fetch_with_retry


def extract_fields_from_wikitext(wikitext: str) -> dict:
    """
    从 wikitext 中提取道具字段（单次遍历）
    
    Args:
        wikitext: Wiki 原始文本
    
    Returns:
        包含所有提取字段的字典
    """
    field_mapping = {
        'category': '|主分类=',
        'subcategory': '|次分类=', 
        'rarity': '|稀有度=',
        'source': '|来源=',
        'version': '|道具版本=',
        'icon_id': '|icon=',
    }
    
    result = {field: '' for field in field_mapping.keys()}
    result['description'] = ''
    description_found = False
    
    for line in wikitext.split('\n'):
        line = line.strip()
        
        if not description_found:
            if line.startswith('|用途='):
                result['description'] = line.replace('|用途=', '').strip()
                description_found = True
            elif line.startswith('|描述='):
                desc_value = line.replace('|描述=', '').strip()
                if desc_value:
                    result['description'] = desc_value
        
        for target_field, wiki_field in field_mapping.items():
            if line.startswith(wiki_field):
                result[target_field] = line.replace(wiki_field, '').strip()
    
    return result


def crawl_item(name: str):
    """
    爬取道具详情
    
    Args:
        name: 道具名称
    
    Returns:
        道具数据字典，失败返回 None
    """
    try:
        # 获取页面原始文本
        url = f"{BASE_URL}/index.php"
        params = {"title": name, "action": "raw"}
        
        response = fetch_with_retry(url, params=params)
        if not response:
            return None
        
        wikitext = response.text
        
        fields = extract_fields_from_wikitext(wikitext)
        
        description = fields['description']
        category = fields['category'] or "道具"
        subcategory = fields['subcategory']
        rarity = fields['rarity']
        source = fields['source']
        version = fields['version']
        icon_id = fields['icon_id'] or None
        
        image_url = None
        html_url = f"{BASE_URL}/{name}"
        html_response = fetch_with_retry(html_url)
        if html_response:
            html = html_response.text
            # 查找所有img标签
            img_tags = re.findall(r'<img[^>]+>', html)
            
            # 解析每个img标签的src和alt属性
            matches = []
            for tag in img_tags:
                src_match = re.search(r'src="([^"]+)"', tag)
                alt_match = re.search(r'alt="([^"]*)"', tag)
                if src_match and alt_match:
                    matches.append((src_match.group(1), alt_match.group(1)))
            
            # 第一优先级：查找alt属性包含icon ID的图片
            if icon_id:
                for src, alt in matches:
                    # 检查alt是否以icon ID开头（如 "100003.png"）
                    if alt.startswith(icon_id):
                        image_url = src
                        break
            
            # 第二优先级：在信息框中查找第一个小尺寸缩略图
            if not image_url:
                infobox_pattern = r'<div[^>]*class="[^"]*infobox[^"]*"[^>]*>(.*?)</div>'
                infobox_match = re.search(infobox_pattern, html, re.DOTALL | re.IGNORECASE)
                if infobox_match:
                    infobox_content = infobox_match.group(1)
                    infobox_img_tags = re.findall(r'<img[^>]+>', infobox_content)
                    for tag in infobox_img_tags:
                        src_match = re.search(r'src="([^"]+)"', tag)
                        if src_match:
                            src = src_match.group(1)
                            # 检查是否是缩略图
                            if '/thumb/' in src or re.search(r'/\d+px-', src):
                                # 排除属性和宠物图片
                                if '属性' not in src and '宠物' not in src:
                                    image_url = src
                                    break
            
            # 第三优先级：查找页面上第一个小尺寸缩略图
            if not image_url:
                for src, alt in matches:
                    # 排除大尺寸图片和属性/宠物图片
                    if '属性' in src or '宠物' in src:
                        continue
                    # 检查URL中是否包含缩略图标记
                    if '/thumb/' in src or re.search(r'/\d+px-', src):
                        image_url = src
                        break
            
            # 第四优先级：如果没有缩略图，直接提取第一个patchwiki图片（排除logo）
            if not image_url:
                for src, alt in matches:
                    # 排除logo和通用图标
                    if 'logo' in src.lower() or 'resources/assets' in src:
                        continue
                    # 排除“图标 物品 来源”这种通用图标
                    if '图标' in alt and '来源' in alt:
                        continue
                    # 只接受patchwiki的图片
                    if 'patchwiki.biligame.com/images/rocom' in src:
                        image_url = src
                        break
        
        # 构建道具数据
        item_data = {
            'name': name,
            'description': description[:500] if description else '暂无描述',
            'category': category,
            'subcategory': subcategory if subcategory else None,  # 次分类
            'rarity': rarity if rarity else None,  # 稀有度
            'source': source if source else None,  # 来源信息
            'version': version if version else None,  # 道具版本
            'image_url': image_url,
            'image_local': None,
        }
        
        return item_data
    
    except Exception as e:
        print(f"❌ 爬取道具 '{name}' 失败: {e}")
        return None


if __name__ == "__main__":
    import json
    
    # 测试
    test_items = ["1号药剂", "2号药剂"]
    for item_name in test_items:
        print(f"\n爬取: {item_name}")
        item = crawl_item(item_name)
        if item:
            print(json.dumps(item, ensure_ascii=False, indent=2))
        else:
            print("失败")
