"""
技能详情爬虫
解析技能Wiki页面的详细信息
"""
import re
from dataclasses import dataclass, field
from typing import Optional
from src.config import BASE_URL, fetch_with_retry


@dataclass
class SkillDetail:
    """技能详情数据类"""
    name: str = ""
    element: str = ""
    category: str = ""
    cost: str = ""
    power: str = ""
    effect: str = ""
    icon_image: Optional[str] = None  # 技能图标URL
    icon_image_local: Optional[str] = None  # 本地图片路径


def fetch_wikitext(name: str) -> Optional[str]:
    """
    获取Wiki页面的原始文本
    
    Args:
        name: 页面名称
    
    Returns:
        Wiki文本内容，失败返回None
    """
    url = f"{BASE_URL}/index.php"
    params = {
        "title": name,
        "action": "raw"
    }
    
    response = fetch_with_retry(url, params=params)
    if response:
        return response.text
    return None


def fetch_html_page(name: str) -> Optional[str]:
    """
    获取Wiki页面的 HTML内容（用于提取技能图标）
    
    Args:
        name: 页面名称
    
    Returns:
        HTML内容，失败返回None
    """
    url = f"{BASE_URL}/{name}"
    response = fetch_with_retry(url)
    if response:
        return response.text
    return None


def extract_skill_icon_image(html: str, skill_name: str = "") -> Optional[str]:
    """
    从 HTML中提取技能图标图片URL
    
    Args:
        html: HTML内容
        skill_name: 技能名称（用于精确匹配）
    
    Returns:
        图片URL，未找到返回None
    """
    # 查找所有img标签
    img_tags = re.findall(r'<img[^>]+>', html)
    
    # 解析每个img标签的src和alt属性
    matches = []
    for tag in img_tags:
        src_match = re.search(r'src="([^"]+)"', tag)
        alt_match = re.search(r'alt="([^"]*)"', tag)
        if src_match and alt_match:
            matches.append((src_match.group(1), alt_match.group(1)))
    
    # 第一优先级：查找alt等于技能名称的图片（最准确）
    if skill_name:
        for src, alt in matches:
            if alt == skill_name:
                # 排除属性/宠物图片（虽然alt是技能名，但以防万一）
                if '属性' not in src and '宠物' not in src:
                    return src
    
    # 第二优先级：在信息框中查找第一个非属性/宠物的图片
    infobox_pattern = r'<div[^>]*class="[^"]*infobox[^"]*"[^>]*>(.*?)</div>'
    infobox_match = re.search(infobox_pattern, html, re.DOTALL | re.IGNORECASE)
    if infobox_match:
        infobox_content = infobox_match.group(1)
        # 在信息框中查找所有img标签
        infobox_img_tags = re.findall(r'<img[^>]+>', infobox_content)
        for tag in infobox_img_tags:
            src_match = re.search(r'src="([^"]+)"', tag)
            alt_match = re.search(r'alt="([^"]*)"', tag)
            if src_match and alt_match:
                src, alt = src_match.group(1), alt_match.group(1)
                # 排除属性和宠物图片
                if '属性' not in src and '宠物' not in src and '属性' not in alt and '宠物' not in alt:
                    return src
    
    # 第三优先级：查找页面上第一个小尺寸缩略图（排除属性/宠物）
    for src, alt in matches:
        # 排除大尺寸图片和属性/宠物图片
        if '属性' in src or '宠物' in src:
            continue
        # 检查URL中是否包含缩略图标记（如 /thumb/ 或尺寸参数）
        if '/thumb/' in src or re.search(r'/\d+px-', src):
            # 这很可能是一个小图标
            return src
    
    return None


def parse_wikitext(wikitext: str) -> Optional[SkillDetail]:
    """
    解析Wiki文本中的技能信息
    
    Args:
        wikitext: Wiki原始文本
    
    Returns:
        SkillDetail对象，解析失败返回None
    """
    import re
    
    # 匹配 {{技能信息|...}}
    match = re.search(r'\{\{技能信息\s*\|([\s\S]*?)\}\}', wikitext)
    if not match:
        return None
    
    raw = match.group(1)
    
    # 解析 key=value 对
    data = {}
    pairs = raw.split('\n|')
    for pair in pairs:
        if '=' not in pair:
            continue
        eq_index = pair.index('=')
        key = pair[:eq_index].strip()
        value = pair[eq_index + 1:].strip()
        data[key] = value
    
    return SkillDetail(
        name="",  # 稍后设置
        element=data.get("属性", ""),
        category=data.get("技能类别", ""),
        cost=data.get("耗能", ""),
        power=data.get("威力", ""),
        effect=data.get("效果", ""),
    )


def crawl_skill(name: str) -> Optional[SkillDetail]:
    """
    爬取技能详情
    
    Args:
        name: 技能名称
    
    Returns:
        SkillDetail对象，失败返回None
    """
    wikitext = fetch_wikitext(name)
    if not wikitext:
        return None
    
    detail = parse_wikitext(wikitext)
    if detail:
        detail.name = name
        
        # 从 HTML页面中提取技能图标URL（传入技能名称用于精确匹配）
        html = fetch_html_page(name)
        if html:
            icon_image = extract_skill_icon_image(html, skill_name=name)
            if icon_image:
                detail.icon_image = icon_image
    
    return detail


if __name__ == "__main__":
    import json
    
    skill = crawl_skill("暗突袭")
    if skill:
        print(json.dumps(skill.__dict__, ensure_ascii=False, indent=2))
