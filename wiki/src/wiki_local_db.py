"""
本地 Wiki 数据库管理
将爬取的 Wiki 数据存储到 SQLite 数据库，支持快速查询
"""
import os
import json
import sqlite3
from typing import Optional, List, Dict
from datetime import datetime


class WikiLocalDB:
    """本地 Wiki 数据库管理类"""
    
    def __init__(self, db_path: str = "./wiki-local.db"):
        self.db_path = db_path
        self.conn = None
        self._init_db()
    
    def _init_db(self):
        """初始化数据库表结构"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        
        cursor = self.conn.cursor()
        
        # 页面表 - 存储所有原始页面
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT UNIQUE NOT NULL,
                wikitext TEXT,
                html_content TEXT,
                page_type TEXT,  -- 'pet', 'skill', 'other'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 宠物信息表 - 结构化存储宠物数据
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pets (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                form TEXT,
                regional_form_name TEXT,
                initial_stage_name TEXT,
                has_alt_color TEXT,
                stage TEXT,
                type TEXT,
                description TEXT,
                element TEXT,
                element2 TEXT,
                ability TEXT,
                ability_desc TEXT,
                hp INTEGER,
                physical_attack INTEGER,
                magic_attack INTEGER,
                physical_defense INTEGER,
                magic_defense INTEGER,
                speed INTEGER,
                size TEXT,
                weight TEXT,
                distribution TEXT,
                quest_tasks TEXT,  -- JSON array
                quest_skill_stones TEXT,  -- JSON array
                skills TEXT,  -- JSON array
                skill_unlock_levels TEXT,  -- JSON array
                bloodline_skills TEXT,  -- JSON array
                learnable_skill_stones TEXT,  -- JSON array
                evolution_condition TEXT,
                evolution_stages TEXT,  -- JSON array of evolution stages
                update_version TEXT,
                sprite_image TEXT,  -- 宠物立绘URL
                sprite_image_local TEXT,  -- 本地图片路径
                raw_data TEXT,  -- 完整原始数据 JSON
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 技能信息表 - 结构化存储技能数据
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS skills (
                name TEXT PRIMARY KEY,
                element TEXT,
                category TEXT,
                cost TEXT,
                power TEXT,
                effect TEXT,
                icon_image TEXT,  -- 技能图标URL
                icon_image_local TEXT,  -- 本地图片路径
                raw_data TEXT,  -- 完整原始数据 JSON
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 属性克制关系表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS type_chart (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attack_type TEXT NOT NULL,
                defense_type TEXT NOT NULL,
                multiplier REAL NOT NULL,  -- 克制倍数: 2.0, 0.5, 0.0
                UNIQUE(attack_type, defense_type)
            )
        ''')
        
        # 道具信息表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS items (
                name TEXT PRIMARY KEY,
                description TEXT,
                category TEXT,  -- 道具分类
                image_url TEXT,
                image_local TEXT,  -- 本地图片路径
                raw_data TEXT,  -- 完整原始数据 JSON
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 精灵蛋信息表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS eggs (
                name TEXT PRIMARY KEY,
                description TEXT,
                image_url TEXT,
                image_local TEXT,  -- 本地图片路径
                raw_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 家具信息表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS furniture (
                name TEXT PRIMARY KEY,
                description TEXT,
                category TEXT,
                image_url TEXT,
                image_local TEXT,  -- 本地图片路径
                raw_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 地区信息表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS regions (
                name TEXT PRIMARY KEY,
                description TEXT,
                image_url TEXT,
                image_local TEXT,  -- 本地图片路径
                raw_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 副本信息表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dungeons (
                name TEXT PRIMARY KEY,
                description TEXT,
                image_url TEXT,
                image_local TEXT,  -- 本地图片路径
                raw_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 更新日志表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS update_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT UNIQUE,
                date TEXT,
                content TEXT,
                changes TEXT,  -- JSON array of all changes
                pet_changes TEXT,  -- JSON array of pet changes
                skill_changes TEXT,  -- JSON array of skill changes
                other_changes TEXT,  -- JSON array of other changes
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 创建索引加速查询
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pets_name ON pets(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pets_element ON pets(element)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_skills_element ON skills(element)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pages_title ON pages(title)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_type_chart_attack ON type_chart(attack_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_type_chart_defense ON type_chart(defense_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_name ON items(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_items_category ON items(category)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_eggs_name ON eggs(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_furniture_name ON furniture(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_furniture_category ON furniture(category)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_regions_name ON regions(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_dungeons_name ON dungeons(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_update_logs_date ON update_logs(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_update_logs_title ON update_logs(title)')
        
        self.conn.commit()
        
        # 初始化属性克制数据
        self._init_type_chart()
    
    def _init_type_chart(self):
        """初始化属性克制关系数据"""
        cursor = self.conn.cursor()
        
        # 检查是否已有数据
        cursor.execute('SELECT COUNT(*) FROM type_chart')
        if cursor.fetchone()[0] > 0:
            return  # 已有数据，不重复插入
        
        # 洛克王国属性克制关系（简化版，可根据实际情况调整）
        type_relations = [
            # (攻击方, 防御方, 倍率)
            ("火", "草", 2.0), ("火", "冰", 2.0), ("火", "虫", 2.0), ("火", "钢", 2.0),
            ("火", "水", 0.5), ("火", "火", 0.5), ("火", "岩石", 0.5), ("火", "龙", 0.5),
            
            ("水", "火", 2.0), ("水", "地面", 2.0), ("水", "岩石", 2.0),
            ("水", "水", 0.5), ("水", "草", 0.5), ("水", "龙", 0.5),
            
            ("草", "水", 2.0), ("草", "地面", 2.0), ("草", "岩石", 2.0),
            ("草", "火", 0.5), ("草", "草", 0.5), ("草", "毒", 0.5), ("草", "飞", 0.5), ("草", "虫", 0.5), ("草", "龙", 0.5), ("草", "钢", 0.5),
            
            ("电", "水", 2.0), ("电", "飞", 2.0),
            ("电", "电", 0.5), ("电", "草", 0.5), ("电", "龙", 0.5), ("电", "地面", 0.0),
            
            ("冰", "草", 2.0), ("冰", "地面", 2.0), ("冰", "飞", 2.0), ("冰", "龙", 2.0),
            ("冰", "火", 0.5), ("冰", "水", 0.5), ("冰", "冰", 0.5), ("冰", "钢", 0.5),
            
            ("格斗", "普通", 2.0), ("格斗", "冰", 2.0), ("格斗", "岩石", 2.0), ("格斗", "恶", 2.0), ("格斗", "钢", 2.0),
            ("格斗", "毒", 0.5), ("格斗", "飞", 0.5), ("格斗", "超能", 0.5), ("格斗", "虫", 0.5), ("格斗", "妖精", 0.5), ("格斗", "幽灵", 0.0),
            
            ("毒", "草", 2.0), ("毒", "妖精", 2.0),
            ("毒", "毒", 0.5), ("毒", "地面", 0.5), ("毒", "岩石", 0.5), ("毒", "幽灵", 0.5), ("毒", "钢", 0.0),
            
            ("地面", "火", 2.0), ("地面", "电", 2.0), ("地面", "毒", 2.0), ("地面", "岩石", 2.0), ("地面", "钢", 2.0),
            ("地面", "草", 0.5), ("地面", "虫", 0.5), ("地面", "飞", 0.0),
            
            ("飞", "草", 2.0), ("飞", "格斗", 2.0), ("飞", "虫", 2.0),
            ("飞", "电", 0.5), ("飞", "岩石", 0.5), ("飞", "钢", 0.5),
            
            ("超能", "格斗", 2.0), ("超能", "毒", 2.0),
            ("超能", "超能", 0.5), ("超能", "钢", 0.5), ("超能", "恶", 0.0),
            
            ("虫", "草", 2.0), ("虫", "超能", 2.0), ("虫", "恶", 2.0),
            ("虫", "火", 0.5), ("虫", "格斗", 0.5), ("虫", "毒", 0.5), ("虫", "飞", 0.5), ("虫", "幽灵", 0.5), ("虫", "钢", 0.5), ("虫", "妖精", 0.5),
            
            ("岩石", "火", 2.0), ("岩石", "冰", 2.0), ("岩石", "飞", 2.0), ("岩石", "虫", 2.0),
            ("岩石", "格斗", 0.5), ("岩石", "地面", 0.5), ("岩石", "钢", 0.5),
            
            ("幽灵", "幽灵", 2.0), ("幽灵", "超能", 2.0),
            ("幽灵", "恶", 0.5), ("幽灵", "普通", 0.0),
            
            ("龙", "龙", 2.0),
            ("龙", "钢", 0.5), ("龙", "妖精", 0.0),
            
            ("恶", "幽灵", 2.0), ("恶", "超能", 2.0),
            ("恶", "格斗", 0.5), ("恶", "恶", 0.5), ("恶", "妖精", 0.5),
            
            ("钢", "冰", 2.0), ("钢", "岩石", 2.0), ("钢", "妖精", 2.0),
            ("钢", "火", 0.5), ("钢", "水", 0.5), ("钢", "电", 0.5), ("钢", "钢", 0.5),
            
            ("妖精", "格斗", 2.0), ("妖精", "龙", 2.0), ("妖精", "恶", 2.0),
            ("妖精", "火", 0.5), ("妖精", "毒", 0.5), ("妖精", "钢", 0.5),
        ]
        
        cursor.executemany(
            'INSERT OR IGNORE INTO type_chart (attack_type, defense_type, multiplier) VALUES (?, ?, ?)',
            type_relations
        )
        self.conn.commit()
    
    def save_page(self, title: str, wikitext: str, html_content: str = None, page_type: str = 'other'):
        """保存页面到数据库"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO pages (title, wikitext, html_content, page_type, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (title, wikitext, html_content, page_type))
        self.conn.commit()
    
    def save_pet(self, pet_data: dict):
        """保存宠物数据"""
        cursor = self.conn.cursor()
        
        # 检查是否已存在同名宠物
        cursor.execute('SELECT id FROM pets WHERE name = ?', (pet_data.get('name', ''),))
        existing = cursor.fetchone()
        
        if existing:
            # 如果存在，更新记录（保持原有ID）
            pet_id = existing['id']
        else:
            # 如果不存在，让数据库自动生成新ID
            pet_id = None
        
        cursor.execute('''
            INSERT OR REPLACE INTO pets 
            (id, name, form, regional_form_name, initial_stage_name, has_alt_color,
             stage, type, description, element, element2, ability, ability_desc,
             hp, physical_attack, magic_attack, physical_defense, magic_defense, speed,
             size, weight, distribution, quest_tasks, quest_skill_stones,
             skills, skill_unlock_levels, bloodline_skills, learnable_skill_stones,
             evolution_condition, evolution_stages, update_version, sprite_image, sprite_image_local, raw_data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            pet_id,  # 使用查询到的ID或None（让数据库自动生成）
            pet_data.get('name', ''),
            pet_data.get('form', ''),
            pet_data.get('regionalFormName', ''),
            pet_data.get('initialStageName', ''),
            pet_data.get('hasAltColor', ''),
            pet_data.get('stage', ''),
            pet_data.get('type', ''),
            pet_data.get('description', ''),
            pet_data.get('element', ''),
            pet_data.get('element2', ''),
            pet_data.get('ability', ''),
            pet_data.get('abilityDesc', ''),
            pet_data.get('hp', 0),
            pet_data.get('physicalAttack', 0),
            pet_data.get('magicAttack', 0),
            pet_data.get('physicalDefense', 0),
            pet_data.get('magicDefense', 0),
            pet_data.get('speed', 0),
            pet_data.get('size', ''),
            pet_data.get('weight', ''),
            pet_data.get('distribution', ''),
            json.dumps(pet_data.get('questTasks', []), ensure_ascii=False),
            json.dumps(pet_data.get('questSkillStones', []), ensure_ascii=False),
            json.dumps(pet_data.get('skills', []), ensure_ascii=False),
            json.dumps(pet_data.get('skillUnlockLevels', []), ensure_ascii=False),
            json.dumps(pet_data.get('bloodlineSkills', []), ensure_ascii=False),
            json.dumps(pet_data.get('learnableSkillStones', []), ensure_ascii=False),
            pet_data.get('evolutionCondition', ''),
            json.dumps(pet_data.get('evolutionStages', []), ensure_ascii=False),
            pet_data.get('updateVersion', ''),
            pet_data.get('spriteImage'),
            pet_data.get('spriteImageLocal'),
            json.dumps(pet_data, ensure_ascii=False)
        ))
        self.conn.commit()
    
    def save_skill(self, skill_data: dict):
        """保存技能数据"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO skills 
            (name, element, category, cost, power, effect, icon_image, icon_image_local, raw_data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            skill_data.get('name', ''),
            skill_data.get('element', ''),
            skill_data.get('category', ''),
            skill_data.get('cost', ''),
            skill_data.get('power', ''),
            skill_data.get('effect', ''),
            skill_data.get('iconImage'),
            skill_data.get('iconImageLocal'),
            json.dumps(skill_data, ensure_ascii=False)
        ))
        self.conn.commit()
    
    def save_item(self, item_data: dict):
        """保存道具数据"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO items 
            (name, description, category, subcategory, rarity, source, version, image_url, image_local, raw_data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            item_data.get('name', ''),
            item_data.get('description', ''),
            item_data.get('category', ''),
            item_data.get('subcategory'),
            item_data.get('rarity'),
            item_data.get('source'),
            item_data.get('version'),
            item_data.get('image_url'),
            item_data.get('image_local'),
            json.dumps(item_data, ensure_ascii=False)
        ))
        self.conn.commit()
    
    def save_egg(self, egg_data: dict):
        """保存精灵蛋数据"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO eggs 
            (name, description, image_url, image_local, raw_data, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            egg_data.get('name', ''),
            egg_data.get('description', ''),
            egg_data.get('image_url'),
            egg_data.get('image_local'),
            json.dumps(egg_data, ensure_ascii=False)
        ))
        self.conn.commit()
    
    def save_furniture(self, furniture_data: dict):
        """保存家具数据"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO furniture 
            (name, description, category, image_url, image_local, raw_data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            furniture_data.get('name', ''),
            furniture_data.get('description', ''),
            furniture_data.get('category', ''),
            furniture_data.get('image_url'),
            furniture_data.get('image_local'),
            json.dumps(furniture_data, ensure_ascii=False)
        ))
        self.conn.commit()
    
    def save_region(self, region_data: dict):
        """保存地区数据"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO regions 
            (name, description, image_url, image_local, raw_data, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            region_data.get('name', ''),
            region_data.get('description', ''),
            region_data.get('image_url'),
            region_data.get('image_local'),
            json.dumps(region_data, ensure_ascii=False)
        ))
        self.conn.commit()
    
    def save_dungeon(self, dungeon_data: dict):
        """保存副本数据"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO dungeons 
            (name, description, image_url, image_local, raw_data, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            dungeon_data.get('name', ''),
            dungeon_data.get('description', ''),
            dungeon_data.get('image_url'),
            dungeon_data.get('image_local'),
            json.dumps(dungeon_data, ensure_ascii=False)
        ))
        self.conn.commit()
    
    def save_update_log(self, log_data: dict):
        """保存更新日志"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO update_logs 
            (title, date, content, changes, pet_changes, skill_changes, other_changes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            log_data.get('title', ''),
            log_data.get('date', ''),
            log_data.get('content', ''),
            json.dumps(log_data.get('changes', []), ensure_ascii=False),
            json.dumps(log_data.get('pet_changes', []), ensure_ascii=False),
            json.dumps(log_data.get('skill_changes', []), ensure_ascii=False),
            json.dumps(log_data.get('other_changes', []), ensure_ascii=False)
        ))
        self.conn.commit()
    
    def get_latest_updates(self, limit: int = 5) -> List[dict]:
        """获取最近的更新日志"""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT * FROM update_logs ORDER BY created_at DESC LIMIT ?',
            (limit,)
        )
        rows = cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                'id': row['id'],
                'title': row['title'],
                'date': row['date'],
                'content': row['content'][:500],  # 限制内容长度
                'pet_changes': json.loads(row['pet_changes']) if row['pet_changes'] else [],
                'skill_changes': json.loads(row['skill_changes']) if row['skill_changes'] else [],
                'other_changes': json.loads(row['other_changes']) if row['other_changes'] else [],
            })
        return results
    
    def search_update_logs(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索更新日志"""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT * FROM update_logs WHERE title LIKE ? OR content LIKE ? ORDER BY created_at DESC LIMIT ?',
            (f'%{keyword}%', f'%{keyword}%', limit)
        )
        rows = cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                'id': row['id'],
                'title': row['title'],
                'date': row['date'],
                'content': row['content'][:500],
            })
        return results
    
    def query_pet(self, name: str) -> Optional[dict]:
        """查询宠物"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM pets WHERE name = ?', (name,))
        row = cursor.fetchone()
        if row:
            return json.loads(row['raw_data'])
        return None
    
    def query_skill(self, name: str) -> Optional[dict]:
        """查询技能"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM skills WHERE name = ?', (name,))
        row = cursor.fetchone()
        if row:
            return json.loads(row['raw_data'])
        return None
    
    def search_pets(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索宠物（模糊匹配）"""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT raw_data FROM pets WHERE name LIKE ? LIMIT ?',
            (f'%{keyword}%', limit)
        )
        return [json.loads(row['raw_data']) for row in cursor.fetchall()]
    
    def search_skills(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索技能（模糊匹配）"""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT raw_data FROM skills WHERE name LIKE ? LIMIT ?',
            (f'%{keyword}%', limit)
        )
        return [json.loads(row['raw_data']) for row in cursor.fetchall()]
    
    def search_items(self, keyword: str, limit: int = 10) -> List[dict]:
        """搜索道具（模糊匹配）"""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT raw_data FROM items WHERE name LIKE ? LIMIT ?',
            (f'%{keyword}%', limit)
        )
        return [json.loads(row['raw_data']) for row in cursor.fetchall()]
    
    def get_pets_by_element(self, element: str, limit: int = 50) -> List[dict]:
        """根据属性获取宠物列表"""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT raw_data FROM pets WHERE element = ? OR element2 = ? LIMIT ?',
            (element, element, limit)
        )
        return [json.loads(row['raw_data']) for row in cursor.fetchall()]
    
    def get_skills_by_element(self, element: str, limit: int = 50) -> List[dict]:
        """根据属性获取技能列表"""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT raw_data FROM skills WHERE element = ? LIMIT ?',
            (element, limit)
        )
        return [json.loads(row['raw_data']) for row in cursor.fetchall()]
    
    def get_type_advantage(self, attack_type: str) -> dict:
        """查询属性克制关系"""
        cursor = self.conn.cursor()
        
        # 查询克制的属性
        cursor.execute(
            'SELECT defense_type, multiplier FROM type_chart WHERE attack_type = ? AND multiplier > 1.0',
            (attack_type,)
        )
        strong_against = {row['defense_type']: row['multiplier'] for row in cursor.fetchall()}
        
        # 查询被克制的属性
        cursor.execute(
            'SELECT defense_type, multiplier FROM type_chart WHERE attack_type = ? AND multiplier < 1.0 AND multiplier > 0.0',
            (attack_type,)
        )
        weak_against = {row['defense_type']: row['multiplier'] for row in cursor.fetchall()}
        
        # 查询无效的属性
        cursor.execute(
            'SELECT defense_type FROM type_chart WHERE attack_type = ? AND multiplier = 0.0',
            (attack_type,)
        )
        immune_against = [row['defense_type'] for row in cursor.fetchall()]
        
        return {
            "attack_type": attack_type,
            "strong_against": strong_against,  # 克制对方
            "weak_against": weak_against,      # 被对方抵抗
            "immune_against": immune_against   # 对对方无效
        }
    
    def get_all_pets(self) -> List[dict]:
        """获取所有宠物"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT raw_data FROM pets')
        return [json.loads(row['raw_data']) for row in cursor.fetchall()]
    
    def get_all_skills(self) -> List[dict]:
        """获取所有技能"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT raw_data FROM skills')
        return [json.loads(row['raw_data']) for row in cursor.fetchall()]
    
    def get_stats(self) -> dict:
        """获取数据库统计信息"""
        cursor = self.conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM pets')
        pet_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM skills')
        skill_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM items')
        item_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM eggs')
        egg_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM furniture')
        furniture_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM regions')
        region_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM dungeons')
        dungeon_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM pages')
        page_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM update_logs')
        update_log_count = cursor.fetchone()[0]
        
        return {
            "pets": pet_count,
            "skills": skill_count,
            "items": item_count,
            "eggs": egg_count,
            "furniture": furniture_count,
            "regions": region_count,
            "dungeons": dungeon_count,
            "pages": page_count,
            "update_logs": update_log_count,
            "db_path": self.db_path
        }
    
    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
    
    def __del__(self):
        """析构函数，确保关闭连接"""
        self.close()
