"""
宠物详情爬虫
解析宠物Wiki页面的详细信息
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from src.config import BASE_URL, fetch_with_retry
from src.skill_detail import SkillDetail


@dataclass
class PetDetail:
    """宠物详情数据类"""
    id: int = 0
    name: str = ""
    form: str = ""
    regional_form_name: str = ""
    initial_stage_name: str = ""
    has_alt_color: str = ""
    stage: str = ""
    type: str = ""
    description: str = ""
    element: str = ""
    element2: str = ""
    ability: str = ""
    ability_desc: str = ""
    hp: int = 0
    physical_attack: int = 0
    magic_attack: int = 0
    physical_defense: int = 0
    magic_defense: int = 0
    speed: int = 0
    size: str = ""
    weight: str = ""
    distribution: str = ""
    quest_tasks: List[str] = field(default_factory=list)
    quest_skill_stones: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    skill_unlock_levels: List[int] = field(default_factory=list)
    bloodline_skills: List[str] = field(default_factory=list)
    learnable_skill_stones: List[str] = field(default_factory=list)
    evolution_condition: str = ""
    evolution_chain: Optional[str] = None  # 进化链wikitext原始数据
    evolution_stages: List[Dict] = field(default_factory=list)  # 结构化进化阶段数据
    update_version: str = ""
    sprite_image: Optional[str] = None  # 宠物立绘URL
    sprite_image_local: Optional[str] = None  # 本地图片路径
    skill_details: List[SkillDetail] = field(default_factory=list)
    bloodline_skill_details: List[SkillDetail] = field(default_factory=list)
    learnable_skill_stone_details: List[SkillDetail] = field(default_factory=list)


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
    获取Wiki页面的HTML内容（用于提取精灵编号）
    
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


def extract_pet_id(html: str) -> Optional[int]:
    """
    从 HTML中提取精灵编号
    
    Args:
        html: HTML内容
    
    Returns:
        精灵编号，未找到返回None
    """
    # 匹配 NOxxx.精灵名 或 NO.xxx 等格式
    match = re.search(r'NO\.?(\d+)[\.．]', html)
    if match and match.group(1):
        return int(match.group(1))
    return None


def extract_pet_sprite_image(html: str) -> Optional[str]:
    """
    从 HTML中提取宠物立绘图片URL
    
    Args:
        html: HTML内容
    
    Returns:
        图片URL，未找到返回None
    """
    # 匹配包含“页面 宠物 立绘”的img标签
    match = re.search(r'<img[^>]*alt="[^"]*页面\s*宠物\s*立绘[^"]*"[^>]*src="([^"]+)"', html)
    if match and match.group(1):
        return match.group(1)
    return None


def parse_evolution_chain(wikitext: str) -> List[Dict]:
    """
    解析Wiki文本中的进化链模板
    
    Args:
        wikitext: Wiki原始文本
    
    Returns:
        进化阶段列表，每个阶段包含：name(名称), level(等级), condition(条件), image(图片)
    """
    import re
    
    stages = []
    
    # 匹配 {{进化链|...}} 或 {{EvolutionChain|...}}
    match = re.search(r'\{\{(?:进化链|EvolutionChain)\s*\|([\s\S]*?)\}\}', wikitext)
    if not match:
        return stages
    
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
    
    # 提取各个阶段的信息（支持最多5个阶段）
    stage_keys = [
        ('一阶段形态', '一阶段等级', '一阶段进化条件'),
        ('二阶段形态', '二阶段等级', '二阶段进化条件'),
        ('三阶段形态', '三阶段等级', '三阶段进化条件'),
        ('四阶段形态', '四阶段等级', '四阶段进化条件'),
        ('五阶段形态', '五阶段等级', '五阶段进化条件'),
    ]
    
    for i, (name_key, level_key, condition_key) in enumerate(stage_keys, 1):
        stage_name = data.get(name_key, '')
        if not stage_name:
            continue
        
        stage_info = {
            'stage': i,
            'name': stage_name,
            'level': data.get(level_key, ''),
            'condition': data.get(condition_key, ''),
        }
        stages.append(stage_info)
    
    return stages


def parse_wikitext(wikitext: str) -> Optional[PetDetail]:
    """
    解析Wiki文本中的宠物信息
    
    Args:
        wikitext: Wiki原始文本
    
    Returns:
        PetDetail对象，解析失败返回None
    """
    # 匹配 {{精灵信息|...}}
    match = re.search(r'\{\{精灵信息\s*\|([\s\S]*?)\}\}', wikitext)
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
    
    def split_comma(s: str) -> List[str]:
        """按逗号分割字符串"""
        return [x.strip() for x in s.split(',') if x.strip()]
    
    def split_tasks(s: str) -> List[str]:
        """按换行或逗号分割任务"""
        return [x.strip() for x in re.split(r'[,\n]', s) if x.strip()]
    
    # 解析进化链
    evolution_stages = parse_evolution_chain(wikitext)
    
    return PetDetail(
        id=0,  # 默认值，稍后从HTML中提取
        name="",
        form=data.get("精灵形态", ""),
        regional_form_name=data.get("地区形态名称", ""),
        initial_stage_name=data.get("精灵初阶名称", ""),
        has_alt_color=data.get("是否有异色", ""),
        stage=data.get("精灵阶段", ""),
        type=data.get("精灵类型", ""),
        description=data.get("精灵描述", ""),
        element=data.get("主属性", ""),
        element2=data.get("2属性", ""),
        ability=data.get("特性", ""),
        ability_desc=data.get("特性描述", ""),
        hp=int(data.get("生命", "0") or "0"),
        physical_attack=int(data.get("物攻", "0") or "0"),
        magic_attack=int(data.get("魔攻", "0") or "0"),
        physical_defense=int(data.get("物防", "0") or "0"),
        magic_defense=int(data.get("魔防", "0") or "0"),
        speed=int(data.get("速度", "0") or "0"),
        size=data.get("体型", ""),
        weight=data.get("重量", ""),
        distribution=data.get("分布地区", ""),
        quest_tasks=split_tasks(data.get("图鉴课题", "")),
        quest_skill_stones=split_comma(data.get("课题技能石", "")),
        skills=split_comma(data.get("技能", "")),
        skill_unlock_levels=[
            int(x) for x in split_comma(data.get("技能解锁等级", ""))
            if x.isdigit()
        ],
        bloodline_skills=split_comma(data.get("血脉技能", "")),
        learnable_skill_stones=split_comma(data.get("可学技能石", "")),
        evolution_condition=data.get("进化条件", ""),
        evolution_chain=None,  # 不存储原始wikitext，只存储结构化数据
        evolution_stages=evolution_stages,
        update_version=data.get("更新版本", ""),
        sprite_image=data.get("宠物立绘形态") or None,
    )


def crawl_pet(name: str) -> Optional[PetDetail]:
    """
    爬取宠物详情
    
    Args:
        name: 宠物名称
    
    Returns:
        PetDetail对象，失败返回None
    """
    # 获取Wiki文本并解析
    wikitext = fetch_wikitext(name)
    if not wikitext:
        return None
    
    detail = parse_wikitext(wikitext)
    if not detail:
        return None
    
    detail.name = name
    
    # 从 HTML页面中提取精灵编号和图片URL
    html = fetch_html_page(name)
    if html:
        pet_id = extract_pet_id(html)
        if pet_id is not None:
            detail.id = pet_id
        
        # 提取宠物立绘图片URL
        sprite_image = extract_pet_sprite_image(html)
        if sprite_image:
            detail.sprite_image = sprite_image
    
    return detail


if __name__ == "__main__":
    import json
    
    pet = crawl_pet("炽心勇狮")
    if pet:
        print(json.dumps(pet.__dict__, ensure_ascii=False, indent=2, default=str))
