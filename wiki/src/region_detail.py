#!/usr/bin/env python3
"""
地区详情爬虫
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import API_URL, BASE_URL, fetch_with_retry


def crawl_region(name: str):
    """
    爬取地区详情
    
    Args:
        name: 地区名称
    
    Returns:
        地区数据字典，失败返回 None
    """
    try:
        # 获取页面原始文本
        url = f"{BASE_URL}/index.php"
        params = {"title": name, "action": "raw"}
        
        response = fetch_with_retry(url, params=params)
        if not response:
            return None
        
        wikitext = response.text
        
        # 解析基本信息
        description = ""
        image_url = None
        
        # 查找描述字段
        for line in wikitext.split('\n'):
            line = line.strip()
            if line.startswith('|描述=') or line.startswith('|说明=') or line.startswith('|简介='):
                desc_value = line.split('=', 1)[1].strip()
                if desc_value and not desc_value.startswith('{{'):
                    description = desc_value
                    break
        
        # 提取图片URL（地图或场景图）
        for line in wikitext.split('\n'):
            if '[[File:' in line or '[[文件:' in line:
                start = line.find('[[') + 2
                end = line.find(']]', start)
                if end > start:
                    file_info = line[start:end]
                    if '|' in file_info:
                        filename = file_info.split('|')[0].strip()
                    else:
                        filename = file_info.strip()
                    
                    if filename and (filename.endswith('.png') or filename.endswith('.jpg')):
                        image_url = f"{BASE_URL}/images/{filename}"
                        break
        
        # 构建地区数据
        region_data = {
            'name': name,
            'description': description[:500] if description else '暂无描述',
            'image_url': image_url,
            'image_local': None,
        }
        
        return region_data
    
    except Exception as e:
        print(f"❌ 爬取地区 '{name}' 失败: {e}")
        return None
