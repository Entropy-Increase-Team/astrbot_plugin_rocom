# -*- coding: utf-8 -*-
"""
Wiki 数据库查询服务
提供宠物、技能、属性克制等数据的查询功能
"""

import sqlite3
import os
from typing import Optional, List, Dict, Any, Tuple

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


class WikiDBService:
    """洛克王国 Wiki 数据库服务（单例模式）"""
    
    _instance = None
    
    def __new__(cls, db_path: str = "./wiki-local.db"):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_path: str = "./wiki-local.db"):
        if self._initialized:
            return
        
        self._initialized = True
        self.db_path = db_path
        self.conn = None
        self._connect_db()
    
    def _connect_db(self):
        """连接数据库"""
        try:
            # 如果是相对路径，转换为绝对路径
            if not os.path.isabs(self.db_path):
                self.db_path = os.path.join(os.path.dirname(__file__), '..', self.db_path)
            
            if not os.path.exists(self.db_path):
                raise FileNotFoundError(f"数据库文件不存在: {self.db_path}")
            
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            logger.info(f"✅ 数据库连接成功: {self.db_path}")
        except Exception as e:
            logger.error(f"❌ 数据库连接失败: {e}")
            raise
    
    def _ensure_connection(self):
        """确保数据库连接有效"""
        if self.conn is None:
            self._connect_db()
    
    def _normalize_path(self, path: Optional[str]) -> Optional[str]:
        """
        规范化文件路径（跨平台兼容）
        
        Args:
            path: 原始路径
            
        Returns:
            规范化后的路径（统一使用正斜杠）
        """
        if not path:
            return None
        # 统一将反斜杠转换为正斜杠，兼容 Windows 和 Linux
        return path.replace('\\', '/')
    
    def get_pet_info(self, name: str, fuzzy: bool = True, limit: int = 5) -> List[Dict[str, Any]]:
        """
        查询宠物信息
        
        Args:
            name: 宠物名称或ID
            fuzzy: 是否使用模糊匹配
            limit: 返回结果数量限制
            
        Returns:
            宠物信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            # 智能匹配：如果输入包含关键词（如"沙地"、"雪山"），尝试匹配特殊形态
            keywords_map = {
                '沙地': ['沙地', '沙漠'],
                '雪山': ['雪山', '雪地', '冰雪'],
                '火山': ['火山', '熔岩'],
                '草地': ['草地', '草原', '森林'],
                '悲鸣': ['悲鸣'],
                '睡衣': ['睡衣', '星星'],
                '竹林': ['竹林'],
                '本命年': ['本命年', '新年'],
                '噩梦': ['噩梦'],
                # 新增：鸭吉吉变体关键词
                '紧实': ['紧实', '紧凑'],
                '蓬松': ['蓬松', '松散'],
                '起来': ['起来', '起床'],
                '急了': ['急了', '着急', '急急急'],
                '燃了': ['燃了', '燃烧'],
                '等了': ['等了', '等待', '等一等'],
            }
            
            matched_suffix = None
            for keyword, aliases in keywords_map.items():
                if any(alias in name for alias in aliases):
                    matched_suffix = keyword
                    break
            
            # 检测是否是数字ID查询
            clean_name = name.strip().lstrip('#')
            if clean_name.isdigit():
                query = """
                    SELECT id, name, element, element2, hp, physical_attack, magic_attack, 
                           physical_defense, magic_defense, speed, ability, ability_desc,
                           size, weight, distribution, description, stage, type,
                           form, initial_stage_name, has_alt_color, update_version,
                           quest_tasks, quest_skill_stones, bloodline_skills, learnable_skill_stones,
                           skills, sprite_image_local, evolution_stages
                    FROM pets
                    WHERE id = ?
                    LIMIT ?
                """
                cursor.execute(query, (int(clean_name), limit))
            elif fuzzy:
                # 先尝试精确匹配
                query_exact = """
                    SELECT id, name, element, element2, hp, physical_attack, magic_attack,
                           physical_defense, magic_defense, speed, ability, ability_desc,
                           size, weight, distribution, description, stage, type,
                           form, initial_stage_name, has_alt_color, update_version,
                           quest_tasks, quest_skill_stones, bloodline_skills, learnable_skill_stones,
                           skills, sprite_image_local, evolution_stages
                    FROM pets
                    WHERE name = ?
                    LIMIT ?
                """
                cursor.execute(query_exact, (name, limit))
                rows = cursor.fetchall()
                
                # 如果精确匹配有结果，直接返回
                if not rows:
                    # 如果没有精确匹配，再尝试前缀匹配
                    query_prefix = """
                        SELECT id, name, element, element2, hp, physical_attack, magic_attack,
                               physical_defense, magic_defense, speed, ability, ability_desc,
                               size, weight, distribution, description, stage, type,
                               form, initial_stage_name, has_alt_color, update_version,
                               quest_tasks, quest_skill_stones, bloodline_skills, learnable_skill_stones,
                               skills, sprite_image_local, evolution_stages
                        FROM pets
                        WHERE name LIKE ?
                        LIMIT ?
                    """
                    cursor.execute(query_prefix, (f"{name}%", limit))
                    rows = cursor.fetchall()
                
                if not rows:
                    # 如果还没有结果，尝试关键词匹配
                    if matched_suffix:
                        query = """
                            SELECT id, name, element, element2, hp, physical_attack, magic_attack,
                                   physical_defense, magic_defense, speed, ability, ability_desc,
                                   size, weight, distribution, description, stage, type,
                                   form, initial_stage_name, has_alt_color, update_version,
                                   quest_tasks, quest_skill_stones, bloodline_skills, learnable_skill_stones,
                                   skills, sprite_image_local, evolution_stages
                            FROM pets
                            WHERE name LIKE ?
                            ORDER BY 
                                CASE 
                                    WHEN name LIKE '%(' || ? || '%)' THEN 1
                                    ELSE 2
                                END,
                                name
                            LIMIT ?
                        """
                        cursor.execute(query, (f"%{matched_suffix}%", matched_suffix, limit))
                        rows = cursor.fetchall()
                    else:
                        # 最后才使用模糊匹配
                        query = """
                            SELECT id, name, element, element2, hp, physical_attack, magic_attack,
                                   physical_defense, magic_defense, speed, ability, ability_desc,
                                   size, weight, distribution, description, stage, type,
                                   form, initial_stage_name, has_alt_color, update_version,
                                   quest_tasks, quest_skill_stones, bloodline_skills, learnable_skill_stones,
                                   skills, sprite_image_local, evolution_stages
                            FROM pets
                            WHERE name LIKE ?
                            LIMIT ?
                        """
                        cursor.execute(query, (f"%{name}%", limit))
                        rows = cursor.fetchall()
            else:
                query = """
                    SELECT id, name, element, element2, hp, physical_attack, magic_attack,
                           physical_defense, magic_defense, speed, ability, ability_desc,
                           size, weight, distribution, description, stage, type,
                           form, initial_stage_name, has_alt_color, update_version,
                           quest_tasks, quest_skill_stones, bloodline_skills, learnable_skill_stones,
                           skills, sprite_image_local, evolution_stages
                    FROM pets
                    WHERE name = ?
                    LIMIT ?
                """
                cursor.execute(query, (name, limit))
                rows = cursor.fetchall()
            
            # rows已经在上面的if-else分支中赋值
            results = []
            import json
            for row in rows:
                # 将 sqlite3.Row 转换为字典
                row_dict = dict(row)
                
                # 合并主属性和第二属性
                element = row_dict.get('element') or '未知'
                element2 = row_dict.get('element2', '')
                if element2 and element2 != '无' and element2 != 'None':
                    full_element = f"{element}+{element2}"
                else:
                    full_element = element
                
                # 解析进化阶段数据
                evolution_stages_raw = row_dict.get('evolution_stages', '')
                evolution_stages = []
                if evolution_stages_raw:
                    try:
                        evolution_stages = json.loads(evolution_stages_raw)
                    except:
                        evolution_stages = []
                
                results.append({
                    'id': row_dict.get('id'),
                    'name': row_dict.get('name'),
                    'element': full_element,
                    'hp': row_dict.get('hp') or 0,
                    'physical_attack': row_dict.get('physical_attack') or 0,
                    'magic_attack': row_dict.get('magic_attack') or 0,
                    'physical_defense': row_dict.get('physical_defense') or 0,
                    'magic_defense': row_dict.get('magic_defense') or 0,
                    'speed': row_dict.get('speed') or 0,
                    'ability': row_dict.get('ability') or '无',
                    'ability_desc': row_dict.get('ability_desc', ''),
                    'size': row_dict.get('size', ''),
                    'weight': row_dict.get('weight', ''),
                    'distribution': row_dict.get('distribution', ''),
                    'description': row_dict.get('description', ''),
                    'stage': row_dict.get('stage', ''),
                    'type': row_dict.get('type', ''),
                    'form': row_dict.get('form', ''),
                    'initial_stage_name': row_dict.get('initial_stage_name', ''),
                    'has_alt_color': row_dict.get('has_alt_color', ''),
                    'update_version': row_dict.get('update_version', ''),
                    'quest_tasks': row_dict.get('quest_tasks', ''),
                    'quest_skill_stones': row_dict.get('quest_skill_stones', ''),
                    'bloodline_skills': row_dict.get('bloodline_skills', ''),
                    'learnable_skill_stones': row_dict.get('learnable_skill_stones', ''),
                    'skills': row_dict.get('skills') or '',
                    'sprite_image_local': self._normalize_path(row_dict.get('sprite_image_local')),
                    'evolution_stages': evolution_stages
                })
            
            # 对结果进行排序：优先返回有六维数据的宠物
            def has_stats(pet):
                """检查宠物是否有完整的六维数据"""
                return (pet.get('hp', 0) > 0 or 
                        pet.get('physical_attack', 0) > 0 or
                        pet.get('magic_attack', 0) > 0)
            
            results.sort(key=lambda x: (not has_stats(x), x['name']))
            
            logger.info(f"🔍 查询宠物 '{name}'，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 查询宠物失败: {e}")
            return []
    
    def get_pet_evolution_chain(self, name: str) -> Optional[Dict[str, Any]]:
        """
        获取宠物的完整进化链（单条）
        
        Args:
            name: 宠物名称
            
        Returns:
            包含进化链信息的字典，包括：
            - stages: 进化阶段列表
            - current_stage: 当前阶段序号
            - previous_stage: 前一阶段信息
            - next_stage: 下一阶段信息
            - final_stage: 最终阶段信息
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            import json
            
            # 查询宠物的进化阶段数据
            query = """
                SELECT name, stage, evolution_stages
                FROM pets
                WHERE name = ?
            """
            cursor.execute(query, (name,))
            row = cursor.fetchone()
            
            if not row:
                logger.warning(f"⚠️ 未找到宠物: {name}")
                return None
            
            row_dict = dict(row)
            evolution_stages_raw = row_dict.get('evolution_stages', '')
            
            if not evolution_stages_raw:
                logger.info(f"ℹ️ 宠物 '{name}' 没有进化链数据")
                return {
                    'name': name,
                    'stages': [],
                    'current_stage': None,
                    'previous_stage': None,
                    'next_stage': None,
                    'final_stage': None,
                    'message': '暂无进化链信息'
                }
            
            try:
                evolution_stages = json.loads(evolution_stages_raw)
            except Exception as e:
                logger.error(f"❌ 解析进化链数据失败: {e}")
                return None
            
            if not evolution_stages:
                return {
                    'name': name,
                    'stages': [],
                    'current_stage': None,
                    'previous_stage': None,
                    'next_stage': None,
                    'final_stage': None,
                    'message': '暂无进化链信息'
                }
            
            # 确定当前阶段
            current_stage_num = None
            stage_str = row_dict.get('stage', '')
            if '一阶' in stage_str or '初阶' in stage_str:
                current_stage_num = 1
            elif '二阶' in stage_str:
                current_stage_num = 2
            elif '三阶' in stage_str:
                current_stage_num = 3
            elif '四阶' in stage_str:
                current_stage_num = 4
            elif '五阶' in stage_str:
                current_stage_num = 5
            
            # 如果没有明确的阶段信息，尝试通过名称匹配
            if current_stage_num is None:
                for stage in evolution_stages:
                    if stage.get('name') == name:
                        current_stage_num = stage.get('stage')
                        break
            
            # 获取各阶段信息
            current_stage = None
            previous_stage = None
            next_stage = None
            final_stage = evolution_stages[-1] if evolution_stages else None
            
            for stage in evolution_stages:
                stage_num = stage.get('stage')
                if stage_num == current_stage_num:
                    current_stage = stage
                    # 查找前一个阶段
                    if stage_num > 1:
                        for prev in evolution_stages:
                            if prev.get('stage') == stage_num - 1:
                                previous_stage = prev
                                break
                    # 查找下一个阶段
                    if stage_num < len(evolution_stages):
                        for nxt in evolution_stages:
                            if nxt.get('stage') == stage_num + 1:
                                next_stage = nxt
                                break
                    break
            
            result = {
                'name': name,
                'stages': evolution_stages,
                'current_stage': current_stage,
                'previous_stage': previous_stage,
                'next_stage': next_stage,
                'final_stage': final_stage,
                'total_stages': len(evolution_stages)
            }
            
            logger.info(f"🔍 获取宠物 '{name}' 的进化链，共 {len(evolution_stages)} 个阶段")
            return result
            
        except Exception as e:
            logger.error(f"❌ 获取进化链失败: {e}")
            return None
    
    def get_pet_all_evolution_chains(self, name: str) -> List[Dict[str, Any]]:
        """
        获取宠物的所有进化分支（支持多分支进化）
        
        Args:
            name: 宠物名称
            
        Returns:
            进化链列表，每个元素包含：
            - stages: 进化阶段列表（包含name、level、condition、image等字段）
            - current_stage: 当前阶段序号
            - next_stage: 下一阶段信息
            - final_stage: 最终阶段信息
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            import json
            
            # 查找所有initial_stage_name为该宠物的记录
            query = """
                SELECT name, stage, evolution_stages, sprite_image_local, evolution_condition
                FROM pets
                WHERE initial_stage_name = ? AND evolution_stages != '' AND evolution_stages IS NOT NULL
            """
            cursor.execute(query, (name,))
            rows = cursor.fetchall()
            
            if not rows:
                return []
            
            all_chains = []
            seen_chains = set()  # 用于去重
            
            for row in rows:
                row_dict = dict(row)
                pet_name = row_dict.get('name')
                evolution_stages_raw = row_dict.get('evolution_stages', '')
                pet_sprite_image = row_dict.get('sprite_image_local')
                pet_evolution_condition = row_dict.get('evolution_condition')
                
                if not evolution_stages_raw:
                    continue
                
                try:
                    evolution_stages = json.loads(evolution_stages_raw)
                except:
                    continue
                
                if not evolution_stages:
                    continue
                
                # 生成唯一标识用于去重（基于进化链路径）
                chain_key = tuple(s.get('name', '') for s in evolution_stages)
                if chain_key in seen_chains:
                    continue  # 跳过重复的进化链
                seen_chains.add(chain_key)
                
                # 为每个stage补充图片路径和进化条件
                enriched_stages = []
                for i, stage in enumerate(evolution_stages):
                    enriched_stage = stage.copy()
                    stage_name = stage.get('name')
                    
                    # 为每个阶段查询图片和条件
                    if stage_name:
                        cursor.execute(
                            "SELECT sprite_image_local, evolution_condition FROM pets WHERE name = ?",
                            (stage_name,)
                        )
                        stage_row = cursor.fetchone()
                        if stage_row:
                            if stage_row[0]:
                                enriched_stage['image'] = self._normalize_path(stage_row[0])
                            # 如果是最后一个阶段，使用它的evolution_condition
                            if i == len(evolution_stages) - 1 and stage_row[1]:
                                enriched_stage['condition'] = stage_row[1]
                    
                    enriched_stages.append(enriched_stage)
                
                # 确定当前阶段（如果是初始形态，总是第1阶）
                current_stage = enriched_stages[0] if enriched_stages else None
                next_stage = enriched_stages[1] if len(enriched_stages) > 1 else None
                final_stage = enriched_stages[-1] if enriched_stages else None
                
                chain_info = {
                    'name': name,
                    'stages': enriched_stages,
                    'current_stage': current_stage,
                    'next_stage': next_stage,
                    'final_stage': final_stage,
                    'total_stages': len(enriched_stages)
                }
                
                all_chains.append(chain_info)
            
            logger.info(f"🔍 获取宠物 '{name}' 的所有进化链，共 {len(all_chains)} 个分支")
            return all_chains
            
        except Exception as e:
            logger.error(f"❌ 获取所有进化链失败: {e}")
            return []
    
    def get_skill_info(self, name: str, fuzzy: bool = True, limit: int = 5) -> List[Dict[str, Any]]:
        """
        查询技能信息
        
        Args:
            name: 技能名称
            fuzzy: 是否使用模糊匹配
            limit: 返回结果数量限制
            
        Returns:
            技能信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            if fuzzy:
                query = """
                    SELECT name, element, power, effect, cost, category, icon_image_local
                    FROM skills
                    WHERE name LIKE ?
                    LIMIT ?
                """
                cursor.execute(query, (f"%{name}%", limit))
            else:
                query = """
                    SELECT name, element, power, effect, cost, category, icon_image_local
                    FROM skills
                    WHERE name = ?
                    LIMIT ?
                """
                cursor.execute(query, (name, limit))
            
            rows = cursor.fetchall()
            results = []
            for row in rows:
                # 将 sqlite3.Row 转换为字典
                row_dict = dict(row)
                
                results.append({
                    'name': row_dict.get('name'),
                    'element': row_dict.get('element') or '未知',
                    'power': row_dict.get('power') or '0',
                    'effect': row_dict.get('effect') or '无特殊效果',
                    'cost': row_dict.get('cost') or '0',
                    'category': row_dict.get('category') or '魔法',
                    'icon_image_local': self._normalize_path(row_dict.get('icon_image_local'))
                })
            
            logger.info(f"🔍 查询技能 '{name}'，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 查询技能失败: {e}")
            return []
    
    def get_type_advantage(self, attack_type: str, defense_type: str) -> Optional[float]:
        """
        查询属性克制倍率
        
        Args:
            attack_type: 攻击方属性
            defense_type: 防御方属性
            
        Returns:
            克制倍率，未找到返回 None
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            query = """
                SELECT multiplier
                FROM type_chart
                WHERE attack_type = ? AND defense_type = ?
            """
            cursor.execute(query, (attack_type, defense_type))
            row = cursor.fetchone()
            
            if row:
                multiplier = dict(row).get('multiplier')
                logger.info(f"⚔️ 属性克制: {attack_type} vs {defense_type} = {multiplier}x")
                return multiplier
            else:
                logger.warning(f"⚠️ 未找到属性克制关系: {attack_type} vs {defense_type}")
                return None
                
        except Exception as e:
            logger.error(f"❌ 查询属性克制失败: {e}")
            return None
    
    def get_type_chart_summary(self, element: str) -> Dict[str, Any]:
        """
        获取某属性的完整克制关系
        
        Args:
            element: 属性名称
            
        Returns:
            包含克制、被克、免疫等信息的字典
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            result = {
                'element': element,
                'strong_against': [],  # 克制的属性
                'weak_against': [],    # 被克制的属性
                'immune_to': [],       # 免疫的属性
                'no_effect': []        # 无效的属性
            }
            
            # 查询该属性克制的属性
            cursor.execute("""
                SELECT defense_type, multiplier
                FROM type_chart
                WHERE attack_type = ? AND multiplier > 1.0
                ORDER BY multiplier DESC
            """, (element,))
            result['strong_against'] = [
                f"{dict(row).get('defense_type')}({dict(row).get('multiplier')}x)" 
                for row in cursor.fetchall()
            ]
            
            # 查询克制该属性的属性
            cursor.execute("""
                SELECT attack_type, multiplier
                FROM type_chart
                WHERE defense_type = ? AND multiplier > 1.0
                ORDER BY multiplier DESC
            """, (element,))
            result['weak_against'] = [
                f"{dict(row).get('attack_type')}({dict(row).get('multiplier')}x)" 
                for row in cursor.fetchall()
            ]
            
            # 查询免疫的属性
            cursor.execute("""
                SELECT attack_type
                FROM type_chart
                WHERE defense_type = ? AND multiplier = 0.0
            """, (element,))
            result['immune_to'] = [dict(row).get('attack_type') for row in cursor.fetchall()]
            
            # 查询无效的属性
            cursor.execute("""
                SELECT attack_type
                FROM type_chart
                WHERE defense_type = ? AND multiplier = 0.5
            """, (element,))
            result['no_effect'] = [dict(row).get('attack_type') for row in cursor.fetchall()]
            
            logger.info(f"📊 获取属性 '{element}' 的克制关系")
            return result
            
        except Exception as e:
            logger.error(f"❌ 获取属性克制关系失败: {e}")
            return {'element': element, 'strong_against': [], 'weak_against': [], 'immune_to': [], 'no_effect': []}
    
    def search_wiki_page(self, title: str, fuzzy: bool = True, limit: int = 5) -> List[Dict[str, Any]]:
        """
        搜索 Wiki 页面
        
        Args:
            title: 页面标题
            fuzzy: 是否使用模糊匹配
            limit: 返回结果数量限制
            
        Returns:
            页面信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            if fuzzy:
                query = """
                    SELECT id, title, page_type, substr(wikitext, 1, 200) as preview
                    FROM pages
                    WHERE title LIKE ?
                    LIMIT ?
                """
                cursor.execute(query, (f"%{title}%", limit))
            else:
                query = """
                    SELECT id, title, page_type, substr(wikitext, 1, 200) as preview
                    FROM pages
                    WHERE title = ?
                    LIMIT ?
                """
                cursor.execute(query, (title, limit))
            
            rows = cursor.fetchall()
            results = []
            for row in rows:
                # 将 sqlite3.Row 转换为字典
                row_dict = dict(row)
                
                results.append({
                    'id': row_dict.get('id'),
                    'title': row_dict.get('title'),
                    'page_type': row_dict.get('page_type') or '未知',
                    'preview': row_dict.get('preview') or ''
                })
            
            logger.info(f"📄 搜索页面 '{title}'，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 搜索页面失败: {e}")
            return []
    
    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            self.conn = None
            logger.info("🔒 数据库连接已关闭")
    
    def get_database_stats(self) -> Dict[str, int]:
        """
        获取数据库统计信息
        
        Returns:
            包含各表记录数的字典
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            stats = {}
            
            # 宠物数量
            cursor.execute("SELECT COUNT(*) as count FROM pets")
            stats['pets'] = dict(cursor.fetchone()).get('count', 0)
            
            # 技能数量
            cursor.execute("SELECT COUNT(*) as count FROM skills")
            stats['skills'] = dict(cursor.fetchone()).get('count', 0)
            
            # Wiki页面数量
            cursor.execute("SELECT COUNT(*) as count FROM pages")
            stats['pages'] = dict(cursor.fetchone()).get('count', 0)
            
            # 属性克制关系数量
            cursor.execute("SELECT COUNT(*) as count FROM type_chart")
            stats['type_chart'] = dict(cursor.fetchone()).get('count', 0)
            
            return stats
        
        except Exception as e:
            logger.error(f"❌ 获取数据库统计失败: {e}")
            return {'pets': 0, 'skills': 0, 'pages': 0, 'type_chart': 0}
    
    def search_pets_by_elements(self, elements: List[str], limit: int = 10) -> List[Dict[str, Any]]:
        """
        按属性组合搜索宠物
        
        Args:
            elements: 属性列表，如 ['草', '毒']
            limit: 返回结果数量限制
            
        Returns:
            宠物信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            if len(elements) == 1:
                # 单属性查询：主属性或副属性匹配
                query = """
                    SELECT id, name, element, element2, hp, ability, skills, sprite_image_local
                    FROM pets
                    WHERE element = ? OR element2 = ?
                    LIMIT ?
                """
                cursor.execute(query, (elements[0], elements[0], limit))
            elif len(elements) == 2:
                # 双属性组合查询：宠物的两个属性必须正好匹配这两个属性（顺序不限）
                query = """
                    SELECT id, name, element, element2, hp, ability, skills, sprite_image_local
                    FROM pets
                    WHERE (
                        (element = ? AND element2 = ?) OR
                        (element = ? AND element2 = ?)
                    )
                    LIMIT ?
                """
                cursor.execute(query, (elements[0], elements[1], elements[1], elements[0], limit))
            else:
                # 3个及以上属性：几乎不存在，返回空
                logger.warning(f"⚠️ 不支持 {len(elements)} 个属性的组合查询")
                return []
            
            rows = cursor.fetchall()
            results = []
            for row in rows:
                # 将 sqlite3.Row 转换为字典
                row_dict = dict(row)
                
                # 合并主属性和第二属性
                element = row_dict.get('element') or '未知'
                element2 = row_dict.get('element2', '')
                if element2 and element2 != '无' and element2 != 'None':
                    full_element = f"{element}+{element2}"
                else:
                    full_element = element
                
                results.append({
                    'id': row_dict.get('id'),
                    'name': row_dict.get('name'),
                    'element': full_element,
                    'hp': row_dict.get('hp') or 0,
                    'ability': row_dict.get('ability') or '无',
                    'skills': row_dict.get('skills') or '',
                    'sprite_image_local': self._normalize_path(row_dict.get('sprite_image_local'))
                })
            
            logger.info(f"🔍 按属性 {elements} 搜索宠物，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 属性搜索失败: {e}")
            return []
    
    def search_pets_by_stat(self, stat_name: str, min_value: int, limit: int = 10) -> List[Dict[str, Any]]:
        """
        按属性值筛选宠物
        
        Args:
            stat_name: 属性名 (hp, physical_attack, magic_attack, physical_defense, magic_defense, speed)
            min_value: 最小值
            limit: 返回结果数量限制
            
        Returns:
            宠物信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        # 验证属性名
        valid_stats = ['hp', 'physical_attack', 'magic_attack', 'physical_defense', 'magic_defense', 'speed']
        if stat_name not in valid_stats:
            logger.error(f"❌ 无效的属性名: {stat_name}")
            return []
        
        try:
            query = f"""
                SELECT id, name, element, element2, {stat_name}, ability, skills, sprite_image_local
                FROM pets
                WHERE {stat_name} >= ?
                ORDER BY {stat_name} DESC
                LIMIT ?
            """
            cursor.execute(query, (min_value, limit))
            
            rows = cursor.fetchall()
            results = []
            for row in rows:
                # 将 sqlite3.Row 转换为字典
                row_dict = dict(row)
                
                # 合并主属性和第二属性
                element = row_dict.get('element') or '未知'
                element2 = row_dict.get('element2', '')
                if element2 and element2 != '无' and element2 != 'None':
                    full_element = f"{element}+{element2}"
                else:
                    full_element = element
                
                results.append({
                    'id': row_dict.get('id'),
                    'name': row_dict.get('name'),
                    'element': full_element,
                    stat_name: row_dict.get(stat_name) or 0,
                    'hp': row_dict.get('hp') or 0,
                    'ability': row_dict.get('ability') or '无',
                    'skills': row_dict.get('skills') or '',
                    'sprite_image_local': self._normalize_path(row_dict.get('sprite_image_local'))
                })
            
            logger.info(f"🔍 按 {stat_name}>={min_value} 搜索宠物，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 属性筛选失败: {e}")
            return []
    
    def get_pets_by_element(self, element: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        根据单个属性获取宠物列表
        
        Args:
            element: 属性名称（如“火”、“水”）
            limit: 返回结果数量限制
            
        Returns:
            宠物信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            query = """
                SELECT id, name, element, element2, hp, physical_attack, magic_attack,
                       physical_defense, magic_defense, speed, ability, skills, sprite_image_local
                FROM pets
                WHERE element = ? OR element2 = ?
                ORDER BY name
                LIMIT ?
            """
            cursor.execute(query, (element, element, limit))
            
            rows = cursor.fetchall()
            results = []
            for row in rows:
                row_dict = dict(row)
                
                # 合并主属性和第二属性
                elem = row_dict.get('element') or '未知'
                element2 = row_dict.get('element2', '')
                if element2 and element2 != '无' and element2 != 'None':
                    full_element = f"{elem}+{element2}"
                else:
                    full_element = elem
                
                results.append({
                    'id': row_dict.get('id'),
                    'name': row_dict.get('name'),
                    'element': full_element,
                    'hp': row_dict.get('hp') or 0,
                    'physical_attack': row_dict.get('physical_attack') or 0,
                    'magic_attack': row_dict.get('magic_attack') or 0,
                    'physical_defense': row_dict.get('physical_defense') or 0,
                    'magic_defense': row_dict.get('magic_defense') or 0,
                    'speed': row_dict.get('speed') or 0,
                    'ability': row_dict.get('ability') or '无',
                    'skills': row_dict.get('skills') or '',
                    'sprite_image_local': self._normalize_path(row_dict.get('sprite_image_local'))
                })
            
            logger.info(f"🔍 按属性 '{element}' 搜索宠物，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 属性筛选失败: {e}")
            return []
    
    def get_pets_by_dual_element(self, element1: str, element2: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        根据双属性获取宠物列表
        
        Args:
            element1: 第一个属性
            element2: 第二个属性
            limit: 返回结果数量限制
            
        Returns:
            宠物信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            query = """
                SELECT id, name, element, element2, hp, physical_attack, magic_attack,
                       physical_defense, magic_defense, speed, ability, skills, sprite_image_local
                FROM pets
                WHERE (element = ? AND element2 = ?) OR (element = ? AND element2 = ?)
                ORDER BY name
                LIMIT ?
            """
            cursor.execute(query, (element1, element2, element2, element1, limit))
            
            rows = cursor.fetchall()
            results = []
            for row in rows:
                row_dict = dict(row)
                
                # 合并主属性和第二属性
                elem = row_dict.get('element') or '未知'
                elem2 = row_dict.get('element2', '')
                if elem2 and elem2 != '无' and elem2 != 'None':
                    full_element = f"{elem}+{elem2}"
                else:
                    full_element = elem
                
                results.append({
                    'id': row_dict.get('id'),
                    'name': row_dict.get('name'),
                    'element': full_element,
                    'hp': row_dict.get('hp') or 0,
                    'physical_attack': row_dict.get('physical_attack') or 0,
                    'magic_attack': row_dict.get('magic_attack') or 0,
                    'physical_defense': row_dict.get('physical_defense') or 0,
                    'magic_defense': row_dict.get('magic_defense') or 0,
                    'speed': row_dict.get('speed') or 0,
                    'ability': row_dict.get('ability') or '无',
                    'skills': row_dict.get('skills') or '',
                    'sprite_image_local': self._normalize_path(row_dict.get('sprite_image_local'))
                })
            
            logger.info(f"🔍 按双属性 '{element1}+{element2}' 搜索宠物，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 双属性筛选失败: {e}")
            return []
    
    def get_top_skills_by_power(self, element: str = None, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取威力最高的技能
        
        Args:
            element: 可选，限定属性
            limit: 返回结果数量限制
            
        Returns:
            技能信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            if element:
                query = """
                    SELECT name, element, power, effect, cost, category
                    FROM skills
                    WHERE element = ?
                    ORDER BY CAST(power AS INTEGER) DESC
                    LIMIT ?
                """
                cursor.execute(query, (element, limit))
            else:
                query = """
                    SELECT name, element, power, effect, cost, category
                    FROM skills
                    ORDER BY CAST(power AS INTEGER) DESC
                    LIMIT ?
                """
                cursor.execute(query, (limit,))
            
            rows = cursor.fetchall()
            results = []
            for row in rows:
                results.append({
                    'name': row['name'],
                    'element': row['element'] or '未知',
                    'power': row['power'] or '0',
                    'effect': row['effect'] or '无特殊效果',
                    'cost': row['cost'] or '0',
                    'category': row['category'] or '魔法'
                })
            
            logger.info(f"🔍 获取最高威力技能{'(' + element + ')' if element else ''}，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 获取技能排行失败: {e}")
            return []
    
    def get_latest_updates(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        获取最近的更新日志
        
        Args:
            limit: 返回数量限制
            
        Returns:
            更新日志列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            query = """
                SELECT title, date, content, pet_changes, skill_changes, other_changes
                FROM update_logs
                ORDER BY created_at DESC
                LIMIT ?
            """
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()
            
            import json
            results = []
            for row in rows:
                results.append({
                    'title': row['title'],
                    'date': row['date'] or '',
                    'content': row['content'][:500] if row['content'] else '',
                    'pet_changes': json.loads(row['pet_changes']) if row['pet_changes'] else [],
                    'skill_changes': json.loads(row['skill_changes']) if row['skill_changes'] else [],
                    'other_changes': json.loads(row['other_changes']) if row['other_changes'] else [],
                })
            
            logger.info(f"🔍 获取最近 {len(results)} 条更新日志")
            return results
            
        except Exception as e:
            logger.error(f"❌ 获取更新日志失败: {e}")
            return []
    
    def search_update_logs(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        搜索更新日志
        
        Args:
            keyword: 搜索关键词
            limit: 返回数量限制
            
        Returns:
            更新日志列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            query = """
                SELECT title, date, content
                FROM update_logs
                WHERE title LIKE ? OR content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            cursor.execute(query, (f'%{keyword}%', f'%{keyword}%', limit))
            rows = cursor.fetchall()
            
            results = []
            for row in rows:
                results.append({
                    'title': row['title'],
                    'date': row['date'] or '',
                    'content': row['content'][:500] if row['content'] else '',
                })
            
            logger.info(f"🔍 搜索更新日志 '{keyword}'，找到 {len(results)} 条结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 搜索更新日志失败: {e}")
            return []
    
    def get_item_info(self, name: str, fuzzy: bool = True, limit: int = 5) -> List[Dict[str, Any]]:
        """
        查询道具信息
        
        Args:
            name: 道具名称
            fuzzy: 是否使用模糊匹配
            limit: 返回结果数量限制
            
        Returns:
            道具信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            if fuzzy:
                query = """
                    SELECT name, description, category, image_local, rarity,
                           subcategory, source, version
                    FROM items
                    WHERE name LIKE ?
                    ORDER BY 
                        CASE 
                            WHEN name = ? THEN 1
                            WHEN name LIKE ? THEN 2
                            ELSE 3
                        END,
                        name
                    LIMIT ?
                """
                cursor.execute(query, (f'%{name}%', name, f'{name}%', limit))
            else:
                query = """
                    SELECT name, description, category, image_local, rarity,
                           subcategory, source, version
                    FROM items
                    WHERE name = ?
                    LIMIT ?
                """
                cursor.execute(query, (name, limit))
            
            rows = cursor.fetchall()
            results = []
            for row in rows:
                row_dict = dict(row)
                results.append({
                    'name': row_dict.get('name'),
                    'description': row_dict.get('description') or '',
                    'category': row_dict.get('category') or '',
                    'subcategory': row_dict.get('subcategory') or '',
                    'image_local': self._normalize_path(row_dict.get('image_local')),
                    'rarity': row_dict.get('rarity') or '',
                    'source': row_dict.get('source') or '',
                    'version': row_dict.get('version') or '',
                })
            
            logger.info(f"🔍 查询道具 '{name}'，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"❌ 查询道具失败: {e}")
            return []
    
    def search_pets_by_color(self, color: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """
        根据颜色搜索宠物
        
        Args:
            color: 颜色名称（如"红"、"蓝"）
            limit: 返回数量限制
            offset: 偏移量
            
        Returns:
            宠物信息列表
        """
        self._ensure_connection()
        cursor = self.conn.cursor()
        
        try:
            query = """
                SELECT name, element, element2, stage, main_color
                FROM pets
                WHERE main_color = ?
                ORDER BY name
                LIMIT ? OFFSET ?
            """
            cursor.execute(query, (color, limit, offset))
            rows = cursor.fetchall()
            
            results = []
            for row in rows:
                results.append({
                    'name': row['name'],
                    'element': row['element'] or '',
                    'element2': row['element2'] or '',
                    'stage': row['stage'] or '',
                    'main_color': row['main_color'] or '',
                })
            
            logger.info(f"🔍 按颜色 '{color}' 搜索宠物，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"❌ 按颜色搜索宠物失败: {e}")
            return []
    
    def __del__(self):
        """析构时关闭连接"""
        self.close()
