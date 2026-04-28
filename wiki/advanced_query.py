# -*- coding: utf-8 -*-
"""
Wiki 高级查询模块
提供颜色筛选、属性筛选、稀有度筛选、进化链等高级查询功能
"""
import re
from typing import Dict, Any, List, Optional, Tuple

# Mock astrbot logger if not available (for standalone testing)
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        logger.addHandler(handler)


class AdvancedQueryHandler:
    """高级查询处理器"""
    
    def __init__(self, db_service):
        """
        初始化高级查询处理器
        
        Args:
            db_service: WikiDBService 实例
        """
        self.db = db_service
    
    def _parse_list_field(self, field_value: str) -> list:
        """
        解析数据库中的列表字段（支持JSON和Python列表格式）
        
        Args:
            field_value: 数据库中的字符串值
            
        Returns:
            解析后的列表，失败返回空列表
        """
        import json
        import ast
        
        if not field_value or field_value == '[]':
            return []
        
        try:
            # 先尝试用JSON解析
            return json.loads(field_value)
        except:
            pass
        
        try:
            # 如果JSON失败，尝试用ast.literal_eval解析Python列表格式
            result = ast.literal_eval(field_value)
            if isinstance(result, list):
                return result
        except:
            pass
        
        # 最后尝试分号分隔的格式
        if ';' in field_value:
            return [s.strip() for s in field_value.split(';') if s.strip()]
        
        return []
    
    def parse_query_intent(self, query: str) -> Dict[str, Any]:
        """
        解析查询意图,识别是否为高级查询
        支持自然语言表达
        
        Args:
            query: 用户查询文本
            
        Returns:
            意图字典,包含 query_type 和 params
        """
        import re
        
        query = query.strip()
        cleaned_query = query
        
        # 清理前缀词(洛克王国、洛克等)
        for prefix in ['洛克王国', '洛克']:
            if cleaned_query.startswith(prefix):
                cleaned_query = cleaned_query[len(prefix):].strip()
        
        # 清理常见的前缀词和后缀词
        for word in ['怎么获得', '如何获得', '是什么', '的介绍', '的资料', '的信息']:
            if cleaned_query.endswith(word):
                cleaned_query = cleaned_query[:-len(word)].strip()
        
        logger.info(f"🔍 原始查询: '{query}'")
        logger.info(f"🔧 清理后: '{cleaned_query}'")
        
        # 0. 检查图片检索请求
        is_image_query, clean_query = self._extract_image_query(cleaned_query)
        if is_image_query:
            cleaned_query = clean_query
        
        # ========== 第一优先级：检测宠物详细查询（宠物名 + 详细信息类型）==========
        detail_patterns = [
            # ====== 技能相关 ======
            (r'^(.+?)\s*的\s*(?:所有技能|全部技能|完整技能|技能列表|配招|推荐技能)$', 'all_skills'),
            (r'^(.+?)\s*的\s*技能$', 'skills'),
            (r'^(.+?)\s*的\s*血脉技能$', 'bloodline_skills'),
            (r'^(.+?)\s*的\s*可学技能石$', 'learnable_stones'),
            (r'^(.+?)\s*的\s*课题技能石$', 'quest_stones'),
            (r'^(.+?)\s+(?:所有技能|全部技能|完整技能|技能列表|配招|推荐技能)$', 'all_skills'),
            (r'^(.+?)\s+技能$', 'skills'),
            (r'^(.+?)\s+血脉技能$', 'bloodline_skills'),
            (r'^(.+?)\s+可学技能石$', 'learnable_stones'),
            (r'^(.+?)\s+课题技能石$', 'quest_stones'),
            (r'^(.+?)(?:会|能|可以)(?:学|用|使)(?:什么|哪些)?(?:技能)?$', 'skills'),
            (r'^(.+?)(?:有|会)(?:哪些|什么)?技能$', 'skills'),
            (r'^(.+?)的?(?:配招|推荐技能|技能搭配)$', 'all_skills'),
            (r'^(.+?)(?:所有技能|全部技能|完整技能|技能列表|配招)$', 'all_skills'),
            (r'^(.+?)技能$', 'skills'),
            
            # ====== 特性相关 ======
            (r'^(.+?)\s*的\s*特性$', 'ability'),
            (r'^(.+?)\s*的\s*天赋$', 'talent'),
            (r'^(.+?)\s+特性$', 'ability'),
            (r'^(.+?)\s+天赋$', 'talent'),
            (r'^(.+?)(?:有|是)(?:什么|哪些)?特性$', 'ability'),
            (r'^(.+?)的特性是什么$', 'ability'),
            (r'^(.+?)特性$', 'ability'),
            
            # ====== 属性相关 ======
            (r'^(.+?)\s*的\s*属性$', 'element'),
            (r'^(.+?)\s*的\s*系别$', 'element'),
            (r'^(.+?)\s+属性$', 'element'),
            (r'^(.+?)是(?:什么|几)系$', 'element'),
            (r'^(.+?)的属性是什么$', 'element'),
            (r'^(.+?)属性$', 'element'),
            
            # ====== HP/生命相关 ======
            (r'^(.+?)\s*的\s*(?:HP|hp|Hp|hP|生命|生命值|体力|血量)$', 'hp'),
            (r'^(.+?)\s+(?:HP|hp|Hp|hP|生命|生命值|体力|血量)$', 'hp'),
            (r'^(.+?)(?:HP|hp|生命|生命值|体力|血量)$', 'hp'),
            
            # ====== 物攻相关 ======
            (r'^(.+?)\s*的\s*(?:物攻|物理攻击|攻击|atk|ATK)$', 'physical_attack'),
            (r'^(.+?)\s+(?:物攻|物理攻击|攻击|atk|ATK)$', 'physical_attack'),
            (r'^(.+?)(?:物攻|物理攻击|atk|ATK)$', 'physical_attack'),
            
            # ====== 魔攻相关 ======
            (r'^(.+?)\s*的\s*(?:魔攻|魔法攻击|法攻|特攻|spatk|SPATK)$', 'magic_attack'),
            (r'^(.+?)\s+(?:魔攻|魔法攻击|法攻|特攻|spatk|SPATK)$', 'magic_attack'),
            (r'^(.+?)(?:魔攻|魔法攻击|法攻|特攻|spatk|SPATK)$', 'magic_attack'),
            
            # ====== 物防相关 ======
            (r'^(.+?)\s*的\s*(?:物防|物理防御|防御|def|DEF)$', 'physical_defense'),
            (r'^(.+?)\s+(?:物防|物理防御|防御|def|DEF)$', 'physical_defense'),
            (r'^(.+?)(?:物防|物理防御|def|DEF)$', 'physical_defense'),
            
            # ====== 魔防相关 ======
            (r'^(.+?)\s*的\s*(?:魔防|魔法防御|法防|特防|spdef|SPDEF)$', 'magic_defense'),
            (r'^(.+?)\s+(?:魔防|魔法防御|法防|特防|spdef|SPDEF)$', 'magic_defense'),
            (r'^(.+?)(?:魔防|魔法防御|法防|特防|spdef|SPDEF)$', 'magic_defense'),
            
            # ====== 速度相关 ======
            (r'^(.+?)\s*的\s*(?:速度|速|spd|SPD|先手)$', 'speed'),
            (r'^(.+?)\s+(?:速度|速|spd|SPD|先手)$', 'speed'),
            (r'^(.+?)(?:速度|速|spd|SPD)$', 'speed'),
            
            # ====== 种族值/六维/面板 ======
            (r'^(.+?)\s*的\s*(?:种族值|六维|面板|基础属性|能力值)$', 'stats'),
            (r'^(.+?)\s+(?:种族值|六维|面板|基础属性|能力值)$', 'stats'),
            (r'^(.+?)(?:种族值|六维|面板)$', 'stats'),
            
            # ====== 任务/课题相关 ======
            (r'^(.+?)\s*的\s*(?:任务|课题|课题任务)$', 'quest_tasks'),
            (r'^(.+?)\s+(?:任务|课题|课题任务)$', 'quest_tasks'),
            (r'^(.+?)(?:要|需要)(?:做|完成)(?:什么|哪些)?任务$', 'quest_tasks'),
            (r'^(.+?)的任务是什么$', 'quest_tasks'),
            (r'^(.+?)(?:任务|课题)$', 'quest_tasks'),
            
            # ====== 进化相关 ======
            (r'^(.+?)\s*的\s*(?:进化|进化条件|进化方式)$', 'evolution'),
            (r'^(.+?)\s+(?:进化|进化条件|进化方式)$', 'evolution'),
            (r'^(.+?)怎么进化$', 'evolution'),
            (r'^(.+?)进化成什么$', 'evolution'),
            (r'^(.+?)的进化条件是什么$', 'evolution'),
            (r'^(.+?)进化$', 'evolution'),
            
            # ====== 技能石相关 ======
            (r'^(.+?)技能石$', 'skill_stones'),
        ]
        
        for pattern, detail_type in detail_patterns:
            match = re.search(pattern, cleaned_query)
            if match:
                name = match.group(1).strip()
                # 清理宠物名末尾的“的”字
                if name.endswith('的'):
                    name = name[:-1].strip()
                
                logger.info(f"🎯 匹配到详细查询: pattern='{pattern}', name='{name}', type='{detail_type}'")
                
                # 过滤掉常见的非宠物名
                if name and len(name) >= 1 and name not in ['有', '的', '是', '怎么', '如何', '获得']:
                    return {
                        'type': 'natural_language',
                        'params': {'type': detail_type, 'pet_name': name}
                    }
        
        # 1. 检查颜色筛选
        color_match = self._check_color_filter(query)
        if color_match:
            return {
                'type': 'color_filter',
                'params': {**color_match, 'is_image_query': is_image_query}
            }
        
        # 2. 检查属性筛选
        element_match = self._check_element_filter(query)
        if element_match:
            return {
                'type': 'element_filter',
                'params': element_match
            }
        
        # 3. 检查稀有度筛选
        rarity_match = self._check_rarity_filter(query)
        if rarity_match:
            return {
                'type': 'rarity_filter',
                'params': rarity_match
            }
        
        # 4. 检查阶段筛选
        stage_match = self._check_stage_filter(query)
        if stage_match:
            return {
                'type': 'stage_filter',
                'params': stage_match
            }
        
        # 5. 检查来源筛选
        source_match = self._check_source_filter(query)
        if source_match:
            return {
                'type': 'source_filter',
                'params': source_match
            }
        
        # 6. 检查进化链查询
        evolution_match = self._check_evolution_query(query)
        if evolution_match:
            return {
                'type': 'evolution_chain',
                'params': evolution_match
            }
        
        # 7. 检查课题任务查询
        task_match = self._check_task_query(query)
        if task_match:
            return {
                'type': 'task_query',
                'params': task_match
            }
        
        # 8. 检查技能石来源查询
        skill_stone_match = self._check_skill_stone_source(query)
        if skill_stone_match:
            return {
                'type': 'skill_stone_source',
                'params': skill_stone_match
            }
        
        # 9. 检查属性克制查询
        type_effectiveness_match = self._check_type_effectiveness(query)
        if type_effectiveness_match:
            return {
                'type': 'type_effectiveness',
                'params': type_effectiveness_match
            }
        
        # 10. 检查自然语言查询(技能/特性/种族值等)
        natural_lang_match = self._check_natural_language_query(query)
        if natural_lang_match:
            return {
                'type': 'natural_language',
                'params': natural_lang_match
            }
        
        # 默认:普通查询
        return {
            'type': 'normal',
            'params': {'keyword': query, 'is_image_query': is_image_query}
        }
    
    def _check_color_filter(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为颜色筛选查询"""
        colors = ['红', '橙', '黄', '绿', '蓝', '紫', '粉', '白', '黑', '棕', '灰']
        entities = ['宠物', '精灵', '蛋', '家具', '道具', '物品']
        
        for color in colors:
            for entity in entities:
                if f'{color}色{entity}' in query or f'{color}{entity}' in query:
                    return {
                        'color': color,
                        'entity_type': self._map_entity_type(entity)
                    }
        
        return None
    
    def _check_element_filter(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为属性筛选查询"""
        elements = ['火', '水', '草', '电', '冰', '龙', '光', '暗', '武', '毒', '土', '翼', '萌', '幽灵', '机械', '石', '虫', '魔']
        
        for element in elements:
            if f'{element}系宠物' in query or f'{element}系精灵' in query or f'{element}系有哪些' in query:
                return {'element': element, 'entity_type': 'pet'}
            elif f'{element}系' in query:
                return {'element': element, 'entity_type': 'pet'}
        
        return None
    
    def _check_rarity_filter(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为稀有度筛选查询"""
        rarities = ['普通', '稀有', '史诗', '传说', '神话']
        entities = ['宠物', '精灵']
        
        for rarity in rarities:
            for entity in entities:
                if f'{rarity}{entity}' in query or f'{rarity}的{entity}' in query:
                    return {'rarity': rarity, 'entity_type': 'pet'}
        
        return None
    
    def _check_stage_filter(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为阶段筛选查询"""
        stages = [
            ('初始', 'initial'),
            ('初级', 'initial'),
            ('第一阶', 'stage1'),
            ('一阶', 'stage1'),
            ('第二阶段', 'stage2'),
            ('二阶', 'stage2'),
            ('第三阶', 'stage3'),
            ('三阶', 'stage3'),
            ('最终', 'final'),
            ('完全体', 'final')
        ]
        
        for stage_text, stage_code in stages:
            if stage_text in query and ('宠物' in query or '精灵' in query):
                return {'stage': stage_code, 'entity_type': 'pet'}
        
        return None
    
    def _check_source_filter(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为来源筛选查询"""
        sources = ['家园', '活动', '商城', '签到', '任务', 'BOSS', 'VIP']
        
        for source in sources:
            if f'{source}宠物' in query or f'{source}精灵' in query:
                return {'source': source, 'entity_type': 'pet'}
        
        return None
    
    def _check_evolution_query(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为进化链查询"""
        evolution_keywords = [
            '进化', '形态', '阶段', '会变成', '变成什么', 
            '下一阶段', '下一个是什么', '进化路线', '所有进化'
        ]
        
        # 提取宠物名称(去除进化相关关键词)
        pet_name = query
        for keyword in evolution_keywords:
            pet_name = pet_name.replace(keyword, '').replace('的', '').strip()
        
        if not pet_name:
            return None
        
        # 判断查询类型
        if '下一阶段' in query or '下一个' in query or '会变成' in query:
            query_subtype = 'next_stage'
        elif '所有进化' in query:
            query_subtype = 'all_branches'
        elif any(f'第{i}阶' in query or f'{i}阶' in query for i in range(1, 10)):
            query_subtype = 'specific_stage'
            # 提取具体阶段
            match = re.search(r'第?(\d+)阶|([一二三四五六七八九十])阶', query)
            if match:
                stage_num = match.group(1) or match.group(2)
                if stage_num.isdigit():
                    query_subtype = f'stage_{stage_num}'
                else:
                    num_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, 
                              '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
                    query_subtype = f'stage_{num_map.get(stage_num, 1)}'
        elif '最终' in query or '完全体' in query:
            query_subtype = 'final_stage'
        else:
            query_subtype = 'full_chain'
        
        return {
            'pet_name': pet_name,
            'query_subtype': query_subtype
        }
    
    def _check_task_query(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为课题任务查询"""
        task_keywords = ['任务', '课题', '要做什么']
        
        for keyword in task_keywords:
            if keyword in query:
                pet_name = query.replace(keyword, '').replace('的', '').strip()
                if pet_name:
                    return {'pet_name': pet_name}
        
        return None
    
    def _check_skill_stone_source(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为技能石来源查询"""
        if '技能石' in query or '的来源' in query or '哪些宠物可以学' in query:
            skill_name = query.replace('技能石', '').replace('的来源', '').replace('哪些宠物可以学', '').strip()
            if skill_name:
                return {'skill_name': skill_name}
        
        return None
    
    def _check_type_effectiveness(self, query: str) -> Optional[Dict[str, Any]]:
        """检查是否为属性克制查询"""
        elements = ['火', '水', '草', '电', '冰', '龙', '光', '暗', '武', '毒', '土', '翼', '萌', '幽灵', '机械', '石', '虫', '魔']
        
        match = re.search(r'([\u4e00-\u9fa5])克([\u4e00-\u9fa5])', query)
        if match:
            attacker = match.group(1)
            defender = match.group(2)
            if attacker in elements and defender in elements:
                return {'attacker': attacker, 'defender': defender}
        
        return None
    
    def _map_entity_type(self, entity_keyword: str) -> str:
        """映射实体类型"""
        mapping = {
            '宠物': 'pet',
            '精灵': 'pet',
            '蛋': 'egg',
            '家具': 'furniture',
            '道具': 'item',
            '物品': 'item'
        }
        return mapping.get(entity_keyword, 'item')
    
    def _extract_image_query(self, query_content: str) -> tuple:
        """
        检测并提取图片检索请求
        
        Args:
            query_content: 查询内容
            
        Returns:
            (is_image_query, clean_query): 是否是图片检索，清理后的查询词
        """
        image_keywords = ["图片", "图", "头像", "立绘"]
        
        for keyword in image_keywords:
            if keyword in query_content:
                # 移除图片关键词，得到实际要查询的内容
                clean_query = query_content.replace(keyword, '').strip()
                if clean_query:
                    return True, clean_query
        
        return False, query_content
    
    def _check_natural_language_query(self, query: str) -> Optional[Dict[str, Any]]:
        """检查自然语言查询(技能/特性/种族值等)"""
        # 技能查询
        skill_patterns = [
            r'(.+?)(?:会什么技能|的技能|能学什么|配招)',
        ]
        for pattern in skill_patterns:
            match = re.search(pattern, query)
            if match:
                pet_name = match.group(1).strip()
                return {'type': 'skills', 'pet_name': pet_name}
        
        # 特性查询
        trait_patterns = [
            r'(.+?)(?:的特性|是什么特性)',
        ]
        for pattern in trait_patterns:
            match = re.search(pattern, query)
            if match:
                pet_name = match.group(1).strip()
                return {'type': 'trait', 'pet_name': pet_name}
        
        # 属性查询
        element_patterns = [
            r'(.+?)(?:是什么系|的属性)',
        ]
        for pattern in element_patterns:
            match = re.search(pattern, query)
            if match:
                pet_name = match.group(1).strip()
                return {'type': 'element', 'pet_name': pet_name}
        
        # HP查询
        hp_patterns = [
            r'(.+?)(?:的HP|的生命值|血量)',
        ]
        for pattern in hp_patterns:
            match = re.search(pattern, query)
            if match:
                pet_name = match.group(1).strip()
                return {'type': 'hp', 'pet_name': pet_name}
        
        # 种族值/六维查询
        stats_patterns = [
            r'(.+?)(?:的种族值|的六维|的面板)',
            r'(.+?)\s+(ATK|SPATK|DEF|SPDEF|SPEED|速度|物攻|魔攻|物防|魔防)',
        ]
        for pattern in stats_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                pet_name = match.group(1).strip()
                stat_type = match.group(2) if len(match.groups()) > 1 else 'all'
                return {'type': 'stats', 'pet_name': pet_name, 'stat_type': stat_type}
        
        # 血脉技能查询
        bloodline_patterns = [
            r'(.+?)(?:的血脉技能|血脉技)',
        ]
        for pattern in bloodline_patterns:
            match = re.search(pattern, query)
            if match:
                pet_name = match.group(1).strip()
                return {'type': 'bloodline_skills', 'pet_name': pet_name}
        
        # 可学技能石查询
        learnable_patterns = [
            r'(.+?)(?:的可学技能石|能学的技能石)',
        ]
        for pattern in learnable_patterns:
            match = re.search(pattern, query)
            if match:
                pet_name = match.group(1).strip()
                return {'type': 'learnable_skill_stones', 'pet_name': pet_name}
        
        return None
    
    def execute_color_filter(self, color: str, entity_type: str, page: int = 1, page_size: int = 10) -> str:
        """执行颜色筛选查询"""
        if not self.db:
            return "❌ 数据库服务不可用"
        
        try:
            # 根据实体类型查询
            if entity_type == 'pet':
                results = self.db.search_pets_by_color(color, limit=page_size, offset=(page - 1) * page_size)
                title = f"🎨 {color}色宠物"
            elif entity_type == 'egg':
                # TODO: 实现精灵蛋颜色查询
                return f"⚠️ 精灵蛋颜色筛选功能暂未实现"
            elif entity_type == 'furniture':
                # TODO: 实现家具颜色查询
                return f"⚠️ 家具颜色筛选功能暂未实现"
            else:
                # TODO: 实现道具颜色查询
                return f"⚠️ 道具颜色筛选功能暂未实现"
            
            if not results:
                return f"❌ 未找到{color}色的{entity_type}"
            
            # 格式化结果
            response = f"{title} (第{page}页)\n"
            response += "━━━━━━━━━━━━━━\n"
            
            for item in results:
                name = item.get('name', '未知')
                response += f"• {name}\n"
            
            response += f"\n共找到 {len(results)} 条结果"
            
            return response
        
        except Exception as e:
            return f"❌ 查询失败: {str(e)}"
    
    def execute_element_filter(self, element: str, page: int = 1, page_size: int = 10) -> str:
        """执行属性筛选查询"""
        if not self.db:
            return "❌ 数据库服务不可用"
        
        try:
            results = self.db.get_pets_by_element(element, limit=page_size, offset=(page - 1) * page_size)
            
            if not results:
                return f"❌ 未找到{element}系宠物"
            
            response = f"🔮 {element}系宠物 (第{page}页)\n"
            response += "━━━━━━━━━━━━━━\n"
            
            for pet in results:
                name = pet.get('name', '未知')
                response += f"• {name}\n"
            
            response += f"\n共找到 {len(results)} 条结果"
            
            return response
        
        except Exception as e:
            return f"❌ 查询失败: {str(e)}"
    
    def execute_evolution_chain(self, pet_name: str, query_subtype: str = 'full_chain') -> str:
        """执行进化链查询"""
        if not self.db:
            return "❌ 数据库服务不可用"
        
        try:
            # 查询宠物信息
            pets = self.db.get_pet_info(pet_name, fuzzy=True, limit=1)
            if not pets:
                return f"❌ 未找到宠物 \"{pet_name}\""
            
            pet = pets[0]
            evolution_data = pet.get('evolution_chain', {})
            
            if not evolution_data:
                return f"⚠️ {pet_name} 暂无进化数据"
            
            # 根据查询子类型返回不同结果
            if query_subtype == 'next_stage':
                return self._format_next_stage(pet, evolution_data)
            elif query_subtype.startswith('stage_'):
                stage_num = int(query_subtype.split('_')[1])
                return self._format_specific_stage(pet, evolution_data, stage_num)
            elif query_subtype == 'final_stage':
                return self._format_final_stage(pet, evolution_data)
            else:  # full_chain
                return self._format_full_evolution_chain(pet, evolution_data)
        
        except Exception as e:
            return f"❌ 查询失败: {str(e)}"
    
    def _format_next_stage(self, pet: Dict, evolution_data: Dict) -> str:
        """格式化下一阶段信息"""
        current_stage = pet.get('stage', 1)
        next_stages = evolution_data.get(str(current_stage + 1), [])
        
        if not next_stages:
            return f"✅ {pet['name']} 已是最终形态,无法继续进化"
        
        response = f"🔄 {pet['name']} 的下一阶段:\n"
        response += "━━━━━━━━━━━━━━\n\n"
        
        for next_pet in next_stages:
            name = next_pet.get('name', '未知')
            condition = next_pet.get('condition', '未知条件')
            response += f"✨ {name}\n"
            response += f"   进化条件: {condition}\n\n"
        
        return response
    
    def _format_full_evolution_chain(self, pet: Dict, evolution_data: Dict) -> str:
        """格式化完整进化链"""
        response = f"🧬 {pet['name']} 的完整进化链:\n"
        response += "━━━━━━━━━━━━━━\n\n"
        
        # 按阶段展示
        for stage_num in sorted(evolution_data.keys(), key=int):
            stage_pets = evolution_data[stage_num]
            response += f"【第{stage_num}阶段】\n"
            
            for stage_pet in stage_pets:
                name = stage_pet.get('name', '未知')
                condition = stage_pet.get('condition', '')
                
                response += f"  ✨ {name}"
                if condition:
                    response += f"\n     → {condition}"
                response += "\n"
            
            response += "\n"
        
        return response
    
    def _format_specific_stage(self, pet: Dict, evolution_data: Dict, stage_num: int) -> str:
        """格式化特定阶段"""
        stage_pets = evolution_data.get(str(stage_num), [])
        
        if not stage_pets:
            return f"❌ {pet['name']} 没有第{stage_num}阶段的形态"
        
        response = f"📊 {pet['name']} 的第{stage_num}阶段:\n"
        response += "━━━━━━━━━━━━━━\n\n"
        
        for stage_pet in stage_pets:
            name = stage_pet.get('name', '未知')
            condition = stage_pet.get('condition', '')
            
            response += f"✨ {name}\n"
            if condition:
                response += f"进化条件: {condition}\n"
            response += "\n"
        
        return response
    
    def _format_final_stage(self, pet: Dict, evolution_data: Dict) -> str:
        """格式化最终形态"""
        max_stage = max(int(k) for k in evolution_data.keys()) if evolution_data else 1
        final_pets = evolution_data.get(str(max_stage), [])
        
        if not final_pets:
            return f"❌ 未找到{pet['name']}的最终形态"
        
        response = f"👑 {pet['name']} 的最终形态:\n"
        response += "━━━━━━━━━━━━━━\n\n"
        
        for final_pet in final_pets:
            name = final_pet.get('name', '未知')
            response += f"✨ {name}\n\n"
        
        return response
    
    def execute_task_query(self, pet_name: str) -> str:
        """执行课题任务查询"""
        if not self.db:
            return "❌ 数据库服务不可用"
        
        try:
            pets = self.db.get_pet_info(pet_name, fuzzy=True, limit=1)
            if not pets:
                return f"❌ 未找到宠物 \"{pet_name}\""
            
            pet = pets[0]
            # 使用_parse_list_field解析JSON字符串
            tasks_raw = pet.get('quest_tasks', '')
            tasks = self._parse_list_field(tasks_raw)
            
            if not tasks:
                return f"⚠️ {pet_name} 暂无课题任务数据"
            
            response = f"📋 {pet_name} 的课题任务:\n"
            response += "━━━━━━━━━━━━━━\n\n"
            
            for i, task in enumerate(tasks, 1):
                # task是字符串（根据原始项目，quest_tasks存储的是字符串列表）
                response += f"{i}. {task}\n\n"
            
            return response
        
        except Exception as e:
            return f"❌ 查询失败: {str(e)}"
    
    def execute_skill_stone_source(self, skill_name: str) -> str:
        """执行技能石来源查询"""
        if not self.db:
            return "❌ 数据库服务不可用"
        
        try:
            # 查询哪些宠物可以学习该技能
            pets = self.db.get_pets_can_learn_skill(skill_name)
            
            if not pets:
                return f"❌ 未找到可以学习 \"{skill_name}\" 的宠物"
            
            response = f"✨ 可以学习 \"{skill_name}\" 的宠物:\n"
            response += "━━━━━━━━━━━━━━\n\n"
            
            for pet in pets[:20]:  # 限制显示数量
                name = pet.get('name', '未知')
                response += f"• {name}\n"
            
            if len(pets) > 20:
                response += f"\n... 还有 {len(pets) - 20} 个宠物"
            
            response += f"\n\n共 {len(pets)} 个宠物可以学习此技能"
            
            return response
        
        except Exception as e:
            return f"❌ 查询失败: {str(e)}"
    
    def calculate_type_effectiveness(self, attacker: str, defender: str) -> str:
        """计算属性克制倍率"""
        # 简化的属性克制表(实际应该从数据库读取)
        effectiveness_table = {
            '火': {'草': 2.0, '冰': 2.0, '虫': 2.0, '水': 0.5, '火': 0.5, '龙': 0.5},
            '水': {'火': 2.0, '土': 2.0, '石': 2.0, '草': 0.5, '水': 0.5, '龙': 0.5},
            '草': {'水': 2.0, '土': 2.0, '石': 2.0, '火': 0.5, '草': 0.5, '毒': 0.5, '虫': 0.5, '龙': 0.5},
            '电': {'水': 2.0, '翼': 2.0, '电': 0.5, '草': 0.5, '龙': 0.5, '土': 0.0},
            '冰': {'草': 2.0, '龙': 2.0, '土': 2.0, '翼': 2.0, '火': 0.5, '水': 0.5, '冰': 0.5},
            '龙': {'龙': 2.0, '火': 0.5, '水': 0.5, '电': 0.5, '草': 0.5},
            '光': {'暗': 2.0, '光': 0.5},
            '暗': {'光': 2.0, '暗': 0.5},
        }
        
        if attacker not in effectiveness_table:
            return f"⚠️ 未知的攻击属性: {attacker}"
        
        multiplier = effectiveness_table[attacker].get(defender, 1.0)
        
        if multiplier == 0.0:
            effect = "无效"
        elif multiplier < 1.0:
            effect = "抵抗"
        elif multiplier > 1.0:
            effect = "克制"
        else:
            effect = "正常"
        
        emoji_map = {
            '火': '🔥', '水': '💧', '草': '🌿', '电': '⚡',
            '冰': '❄️', '龙': '🐉', '光': '✨', '暗': '🌑'
        }
        
        attacker_emoji = emoji_map.get(attacker, '⭐')
        defender_emoji = emoji_map.get(defender, '⭐')
        
        response = f"{attacker_emoji} {attacker} → {defender_emoji} {defender}: {multiplier}x ({effect})"
        
        return response
