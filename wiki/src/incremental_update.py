#!/usr/bin/env python3
"""
增量更新工具 - 支持单独更新特定数据类型
"""
import sys
import os
import time
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.wiki_local_db import WikiLocalDB
from src.skill_index import crawl_skill_index
from src.skill_detail import crawl_skill
from src.pet_index import crawl_all_pets
from src.pet_detail import crawl_pet
from src.generic_index import crawl_category
from src.item_detail import crawl_item
from src.egg_detail import crawl_egg
from src.furniture_detail import crawl_furniture
from src.region_detail import crawl_region
from src.dungeon_detail import crawl_dungeon
from src.config import API_URL, BASE_URL, fetch_with_retry
from src.image_downloader import download_pet_sprite, download_skill_icon, download_item_image, download_image


class IncrementalUpdater:
    """增量更新管理器"""
    
    def __init__(self, db_path="./wiki-local.db"):
        # 如果是相对路径，转换为绝对路径
        if not os.path.isabs(db_path):
            # 获取插件根目录（当前文件的父目录的父目录）
            current_dir = os.path.dirname(os.path.abspath(__file__))
            plugin_root = os.path.dirname(current_dir)
            
            # 如果路径以 ./ 开头，相对于插件根目录
            if db_path.startswith('./') or db_path.startswith('../'):
                db_path = os.path.join(plugin_root, db_path)
            else:
                db_path = os.path.join(current_dir, db_path)
            
            # 规范化路径
            db_path = os.path.normpath(db_path)
        
        self.db = WikiLocalDB(db_path)
        # 缓存文件与数据库在同一目录
        self.cache_file = os.path.join(os.path.dirname(db_path), '.crawl_cache.json')
        self.cache = self._load_cache()
    
    def _load_cache(self):
        """加载爬取缓存"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {
            'skills': [],
            'pets': [],
            'items': [],
            'eggs': [],
            'furniture': [],
            'regions': [],
            'dungeons': [],
            'last_update': None
        }
    
    def _save_cache(self):
        """保存爬取缓存"""
        self.cache['last_update'] = datetime.now().isoformat()
        with open(self.cache_file, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)
    
    def update_skills(self, force=False):
        """更新技能数据"""
        print("=" * 60)
        print("开始更新技能数据...")
        print("=" * 60)
        
        # 获取所有技能列表
        skill_names = crawl_skill_index()
        print(f"找到 {len(skill_names)} 个技能\n")
        
        # 过滤需要更新的技能
        if not force:
            new_skills = [name for name in skill_names if name not in self.cache['skills']]
            print(f"新技能: {len(new_skills)}, 已存在: {len(skill_names) - len(new_skills)}\n")
            skill_names = new_skills
        
        if not skill_names:
            print("没有需要更新的技能")
            return
        
        success = 0
        failed = 0
        image_success = 0
        
        for i, name in enumerate(skill_names, 1):
            skill = crawl_skill(name)
            if skill:
                # 将 SkillDetail 对象转换为字典
                skill_dict = {
                    'name': skill.name,
                    'element': skill.element,
                    'category': skill.category,
                    'cost': skill.cost,
                    'power': skill.power,
                    'effect': skill.effect,
                    'icon_url': skill.icon_image,
                    'icon_image_local': skill.icon_image_local,
                }
                
                # 下载技能图标
                if skill_dict.get('icon_url'):
                    image_local = download_skill_icon(name, skill_dict['icon_url'])
                    if image_local:
                        skill_dict['icon_image_local'] = image_local
                        image_success += 1
                
                self.db.save_skill(skill_dict)
                self.cache['skills'].append(name)
                success += 1
                
                if i % 50 == 0 or i == len(skill_names):
                    print(f"  [{i}/{len(skill_names)}] 成功: {success}, 失败: {failed}, 图片: {image_success}")
            else:
                failed += 1
            
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)
        
        self._save_cache()
        print(f"\n技能更新完成: 成功 {success}, 失败 {failed}, 图片 {image_success}\n")
    
    def update_pets(self, force=False, only_images=False):
        """更新宠物数据
        
        Args:
            force: 强制重新爬取
            only_images: 只下载缺失的图片，不重新爬取文本数据
        """
        print("=" * 60)
        if only_images:
            print("开始下载缺失的宠物图片...")
        else:
            print("开始更新宠物数据...")
        print("=" * 60)
        
        if only_images:
            # 从数据库中获取所有缺少图片的宠物
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT name, sprite_image FROM pets WHERE (sprite_image_local IS NULL OR sprite_image_local = '') AND sprite_image IS NOT NULL")
            missing_image_pets = cursor.fetchall()
            print(f"找到 {len(missing_image_pets)} 个缺少图片的宠物\n")
            
            if not missing_image_pets:
                print("所有宠物图片已完整")
                return
            
            success = 0
            failed = 0
            
            for i, (name, sprite_url) in enumerate(missing_image_pets, 1):
                image_local = download_pet_sprite(name, sprite_url)
                if image_local:
                    # 更新数据库中的图片路径
                    cursor.execute("UPDATE pets SET sprite_image_local = ? WHERE name = ?", (image_local, name))
                    self.db.conn.commit()
                    success += 1
                else:
                    failed += 1
                
                if i % 50 == 0 or i == len(missing_image_pets):
                    print(f"  [{i}/{len(missing_image_pets)}] 成功: {success}, 失败: {failed}")
                
                time.sleep(0.05)
            
            print(f"\n宠物图片下载完成: 成功 {success}, 失败 {failed}\n")
            return
        
        # 原有的完整更新逻辑
        pet_list = crawl_all_pets()
        print(f"找到 {len(pet_list)} 个宠物\n")
        
        # 过滤需要更新的宠物
        if not force:
            new_pets = [name for name in pet_list if name not in self.cache['pets']]
            print(f"新宠物: {len(new_pets)}, 已存在: {len(pet_list) - len(new_pets)}\n")
            pet_list = new_pets
        
        if not pet_list:
            print("没有需要更新的宠物")
            return
        
        success = 0
        failed = 0
        image_success = 0
        save_errors = []  # 记录保存失败的宠物
        
        for i, name in enumerate(pet_list, 1):
            try:
                pet = crawl_pet(name)
                if pet:
                    # 将 PetDetail 对象转换为字典
                    if hasattr(pet, '__dict__'):
                        # 如果是对象，转换为字典（使用数据库期望的字段名）
                        pet_dict = {
                            'id': getattr(pet, 'id', None),
                            'name': getattr(pet, 'name', ''),
                            'form': getattr(pet, 'form', None),
                            'regionalFormName': getattr(pet, 'regional_form_name', None),
                            'initialStageName': getattr(pet, 'initial_stage_name', None),
                            'hasAltColor': getattr(pet, 'has_alt_color', None),
                            'stage': getattr(pet, 'stage', None),
                            'type': getattr(pet, 'type', None),
                            'description': getattr(pet, 'description', ''),
                            'element': getattr(pet, 'element', ''),
                            'element2': getattr(pet, 'element2', None),
                            'ability': getattr(pet, 'ability', ''),
                            'abilityDesc': getattr(pet, 'ability_desc', ''),
                            'hp': getattr(pet, 'hp', 0),
                            'physicalAttack': getattr(pet, 'physical_attack', 0),
                            'magicAttack': getattr(pet, 'magic_attack', 0),
                            'physicalDefense': getattr(pet, 'physical_defense', 0),
                            'magicDefense': getattr(pet, 'magic_defense', 0),
                            'speed': getattr(pet, 'speed', 0),
                            'size': getattr(pet, 'size', ''),
                            'weight': getattr(pet, 'weight', ''),
                            'distribution': getattr(pet, 'distribution', ''),
                            'questTasks': getattr(pet, 'quest_tasks', None),
                            'questSkillStones': getattr(pet, 'quest_skill_stones', None),
                            'skills': getattr(pet, 'skills', None),
                            'skillUnlockLevels': getattr(pet, 'skill_unlock_levels', None),
                            'bloodlineSkills': getattr(pet, 'bloodline_skills', None),
                            'learnableSkillStones': getattr(pet, 'learnable_skill_stones', None),
                            'evolutionCondition': getattr(pet, 'evolution_condition', ''),
                            'evolutionStages': getattr(pet, 'evolution_stages', []),
                            'updateVersion': getattr(pet, 'update_version', ''),
                            'spriteImage': getattr(pet, 'sprite_image', None),
                            'spriteImageLocal': getattr(pet, 'sprite_image_local', None),
                        }
                    else:
                        pet_dict = pet
                    
                    # 下载宠物立绘
                    if pet_dict.get('spriteImage'):
                        image_local = download_pet_sprite(pet_dict['name'], pet_dict['spriteImage'])
                        if image_local:
                            pet_dict['spriteImageLocal'] = image_local
                            image_success += 1
                    
                    # 保存宠物到数据库，添加异常处理
                    try:
                        self.db.save_pet(pet_dict)
                        self.cache['pets'].append(pet_dict['name'])
                        success += 1
                    except Exception as save_error:
                        failed += 1
                        error_msg = f"保存失败 [{name}]: {str(save_error)}"
                        save_errors.append(error_msg)
                        print(f"  [错误] {error_msg}")
                    
                    if i % 50 == 0 or i == len(pet_list):
                        print(f"  [{i}/{len(pet_list)}] 成功: {success}, 失败: {failed}, 图片: {image_success}")
                else:
                    failed += 1
                    print(f"  [警告] 爬取失败: {name}")
                
                if i % 100 == 0:
                    time.sleep(2)
                else:
                    time.sleep(0.05)
            except Exception as e:
                failed += 1
                error_msg = f"处理异常 [{name}]: {str(e)}"
                save_errors.append(error_msg)
                print(f"  [错误] {error_msg}")
                import traceback
                traceback.print_exc()
        
        self._save_cache()
        print(f"\n宠物更新完成: 成功 {success}, 失败 {failed}, 图片 {image_success}")
        
        # 输出保存失败的详细信息
        if save_errors:
            print(f"\n{'='*60}")
            print(f"保存失败的宠物列表 (共{len(save_errors)}个):")
            print(f"{'='*60}")
            for error_msg in save_errors:
                print(f"  {error_msg}")
            print(f"{'='*60}\n")
    
    def update_items(self, force=False, only_images=False):
        """更新道具数据
        
        Args:
            force: 强制重新爬取
            only_images: 只下载缺失的图片
        """
        print("=" * 60)
        if only_images:
            print("开始下载缺失的道具图片...")
        else:
            print("开始更新道具数据...")
        print("=" * 60)
        
        if only_images:
            # 从数据库中获取所有缺少图片的道具
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT name, image_url FROM items WHERE (image_local IS NULL OR image_local = '') AND image_url IS NOT NULL")
            missing_image_items = cursor.fetchall()
            print(f"找到 {len(missing_image_items)} 个缺少图片的道具\n")
            
            if not missing_image_items:
                print("所有道具图片已完整")
                return
            
            success = 0
            failed = 0
            
            for i, (name, image_url) in enumerate(missing_image_items, 1):
                image_local = download_image(image_url, f"items/{name}")
                if image_local:
                    cursor.execute("UPDATE items SET image_local = ? WHERE name = ?", (image_local, name))
                    self.db.conn.commit()
                    success += 1
                else:
                    failed += 1
                
                if i % 100 == 0 or i == len(missing_image_items):
                    print(f"  [{i}/{len(missing_image_items)}] 成功: {success}, 失败: {failed}")
                
                time.sleep(0.05)
            
            print(f"\n道具图片下载完成: 成功 {success}, 失败 {failed}\n")
            return
        
        # 原有的完整更新逻辑
        item_names = crawl_category("道具", "道具")
        print(f"找到 {len(item_names)} 个道具\n")
        
        # 过滤需要更新的道具
        if not force:
            new_items = [name for name in item_names if name not in self.cache['items']]
            print(f"新道具: {len(new_items)}, 已存在: {len(item_names) - len(new_items)}\n")
            item_names = new_items
        
        if not item_names:
            print("没有需要更新的道具")
            return
        
        success = 0
        failed = 0
        image_success = 0
        
        for i, name in enumerate(item_names, 1):
            item = crawl_item(name)
            if item:
                # 下载道具图片
                if item.get('image_url'):
                    category = item.get('category', '其他')
                    image_local = download_item_image(name, item['image_url'], category)
                    if image_local:
                        item['image_local'] = image_local
                        image_success += 1
                
                self.db.save_item(item)
                self.cache['items'].append(name)
                success += 1
                
                if i % 100 == 0 or i == len(item_names):
                    print(f"  [{i}/{len(item_names)}] 成功: {success}, 失败: {failed}, 图片: {image_success}")
            else:
                failed += 1
            
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)
        
        self._save_cache()
        print(f"\n道具更新完成: 成功 {success}, 失败 {failed}, 图片 {image_success}\n")
    
    def update_eggs(self, force=False, only_images=False):
        """更新精灵蛋数据（从数据库中筛选）
        
        Args:
            force: 强制重新爬取
            only_images: 只下载缺失的图片
        """
        print("=" * 60)
        if only_images:
            print("开始下载缺失的精灵蛋图片...")
        else:
            print("开始更新精灵蛋数据...")
        print("=" * 60)
        
        if only_images:
            # 从数据库中获取所有缺少图片的精灵蛋
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT name, image_url FROM items WHERE category = '精灵蛋' AND (image_local IS NULL OR image_local = '') AND image_url IS NOT NULL")
            missing_image_eggs = cursor.fetchall()
            print(f"找到 {len(missing_image_eggs)} 个缺少图片的精灵蛋\n")
            
            if not missing_image_eggs:
                print("所有精灵蛋图片已完整")
                return
            
            success = 0
            failed = 0
            
            for i, (name, image_url) in enumerate(missing_image_eggs, 1):
                image_local = download_item_image(name, image_url, '精灵蛋')
                if image_local:
                    cursor.execute("UPDATE items SET image_local = ? WHERE name = ?", (image_local, name))
                    self.db.conn.commit()
                    success += 1
                else:
                    failed += 1
                
                if i % 50 == 0 or i == len(missing_image_eggs):
                    print(f"  [{i}/{len(missing_image_eggs)}] 成功: {success}, 失败: {failed}")
                
                time.sleep(0.05)
            
            print(f"\n精灵蛋图片下载完成: 成功 {success}, 失败 {failed}\n")
            return
        
        # 原有的完整更新逻辑
        # 从数据库中获取所有分类为"精灵蛋"的道具
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT name FROM items WHERE category = '精灵蛋'")
        egg_names = [row[0] for row in cursor.fetchall()]
        
        print(f"从数据库中找到 {len(egg_names)} 个精灵蛋\n")
        
        if not force:
            new_eggs = [name for name in egg_names if name not in self.cache['eggs']]
            print(f"新精灵蛋: {len(new_eggs)}, 已存在: {len(egg_names) - len(new_eggs)}\n")
            egg_names = new_eggs
        
        if not egg_names:
            print("没有需要更新的精灵蛋")
            return
        
        success = 0
        failed = 0
        image_success = 0
        
        for i, name in enumerate(egg_names, 1):
            egg = crawl_egg(name)
            if egg:
                if egg.get('image_url'):
                    image_local = download_image(egg['image_url'], f"eggs/{name}")
                    if image_local:
                        egg['image_local'] = image_local
                        image_success += 1
                
                self.db.save_egg(egg)
                self.cache['eggs'].append(name)
                success += 1
                
                if i % 50 == 0 or i == len(egg_names):
                    print(f"  [{i}/{len(egg_names)}] 成功: {success}, 失败: {failed}, 图片: {image_success}")
            else:
                failed += 1
            
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)
        
        self._save_cache()
        print(f"\n精灵蛋更新完成: 成功 {success}, 失败 {failed}, 图片 {image_success}\n")
    
    def update_furniture(self, force=False, only_images=False):
        """更新家具数据（从数据库中筛选）
        
        Args:
            force: 强制重新爬取
            only_images: 只下载缺失的图片
        """
        print("=" * 60)
        if only_images:
            print("开始下载缺失的家具图片...")
        else:
            print("开始更新家具数据...")
        print("=" * 60)
        
        if only_images:
            # 从数据库中获取所有缺少图片的家具
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT name, image_url FROM items WHERE category = '家具' AND (image_local IS NULL OR image_local = '') AND image_url IS NOT NULL")
            missing_image_furniture = cursor.fetchall()
            print(f"找到 {len(missing_image_furniture)} 个缺少图片的家具\n")
            
            if not missing_image_furniture:
                print("所有家具图片已完整")
                return
            
            success = 0
            failed = 0
            
            for i, (name, image_url) in enumerate(missing_image_furniture, 1):
                image_local = download_item_image(name, image_url, '家具')
                if image_local:
                    cursor.execute("UPDATE items SET image_local = ? WHERE name = ?", (image_local, name))
                    self.db.conn.commit()
                    success += 1
                else:
                    failed += 1
                
                if i % 50 == 0 or i == len(missing_image_furniture):
                    print(f"  [{i}/{len(missing_image_furniture)}] 成功: {success}, 失败: {failed}")
                
                time.sleep(0.05)
            
            print(f"\n家具图片下载完成: 成功 {success}, 失败 {failed}\n")
            return
        
        # 原有的完整更新逻辑
        # 从数据库中获取所有分类为"家具"的道具
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT name FROM items WHERE category = '家具'")
        furniture_names = [row[0] for row in cursor.fetchall()]
        
        print(f"从数据库中找到 {len(furniture_names)} 个家具\n")
        
        if not force:
            new_furniture = [name for name in furniture_names if name not in self.cache['furniture']]
            print(f"新家具: {len(new_furniture)}, 已存在: {len(furniture_names) - len(new_furniture)}\n")
            furniture_names = new_furniture
        
        if not furniture_names:
            print("没有需要更新的家具")
            return
        
        success = 0
        failed = 0
        image_success = 0
        
        for i, name in enumerate(furniture_names, 1):
            furniture = crawl_furniture(name)
            if furniture:
                if furniture.get('image_url'):
                    image_local = download_image(furniture['image_url'], f"furniture/{name}")
                    if image_local:
                        furniture['image_local'] = image_local
                        image_success += 1
                
                self.db.save_furniture(furniture)
                self.cache['furniture'].append(name)
                success += 1
                
                if i % 50 == 0 or i == len(furniture_names):
                    print(f"  [{i}/{len(furniture_names)}] 成功: {success}, 失败: {failed}, 图片: {image_success}")
            else:
                failed += 1
            
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)
        
        self._save_cache()
        print(f"\n家具更新完成: 成功 {success}, 失败 {failed}, 图片 {image_success}\n")
    
    def update_regions(self, force=False):
        """更新地区数据"""
        print("=" * 60)
        print("开始更新地区数据...")
        print("=" * 60)
        
        region_names = crawl_category("地区图鉴", "地区")
        print(f"找到 {len(region_names)} 个地区\n")
        
        if not force:
            new_regions = [name for name in region_names if name not in self.cache['regions']]
            print(f"新地区: {len(new_regions)}, 已存在: {len(region_names) - len(new_regions)}\n")
            region_names = new_regions
        
        if not region_names:
            print("没有需要更新的地区")
            return
        
        success = 0
        failed = 0
        image_success = 0
        
        for i, name in enumerate(region_names, 1):
            region = crawl_region(name)
            if region:
                if region.get('image_url'):
                    image_local = download_image(region['image_url'], f"regions/{name}")
                    if image_local:
                        region['image_local'] = image_local
                        image_success += 1
                
                self.db.save_region(region)
                self.cache['regions'].append(name)
                success += 1
                
                if i % 50 == 0 or i == len(region_names):
                    print(f"  [{i}/{len(region_names)}] 成功: {success}, 失败: {failed}, 图片: {image_success}")
            else:
                failed += 1
            
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)
        
        self._save_cache()
        print(f"\n地区更新完成: 成功 {success}, 失败 {failed}, 图片 {image_success}\n")
    
    def update_dungeons(self, force=False):
        """更新副本数据"""
        print("=" * 60)
        print("开始更新副本数据...")
        print("=" * 60)
        
        dungeon_names = crawl_category("副本图鉴", "副本")
        print(f"找到 {len(dungeon_names)} 个副本\n")
        
        if not force:
            new_dungeons = [name for name in dungeon_names if name not in self.cache['dungeons']]
            print(f"新副本: {len(new_dungeons)}, 已存在: {len(dungeon_names) - len(new_dungeons)}\n")
            dungeon_names = new_dungeons
        
        if not dungeon_names:
            print("没有需要更新的副本")
            return
        
        success = 0
        failed = 0
        image_success = 0
        
        for i, name in enumerate(dungeon_names, 1):
            dungeon = crawl_dungeon(name)
            if dungeon:
                if dungeon.get('image_url'):
                    image_local = download_image(dungeon['image_url'], f"dungeons/{name}")
                    if image_local:
                        dungeon['image_local'] = image_local
                        image_success += 1
                
                self.db.save_dungeon(dungeon)
                self.cache['dungeons'].append(name)
                success += 1
                
                if i % 50 == 0 or i == len(dungeon_names):
                    print(f"  [{i}/{len(dungeon_names)}] 成功: {success}, 失败: {failed}, 图片: {image_success}")
            else:
                failed += 1
            
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)
        
        self._save_cache()
        print(f"\n副本更新完成: 成功 {success}, 失败 {failed}, 图片 {image_success}\n")
    
    def show_stats(self):
        """显示统计信息"""
        stats = self.db.get_stats()
        print("=" * 60)
        print("数据库统计信息")
        print("=" * 60)
        print(f"  宠物数量: {stats['pets']}")
        print(f"  技能数量: {stats['skills']}")
        print(f"  道具数量: {stats['items']}")
        print(f"  精灵蛋数量: {stats['eggs']}")
        print(f"  家具数量: {stats['furniture']}")
        print(f"  地区数量: {stats['regions']}")
        print(f"  副本数量: {stats['dungeons']}")
        print(f"  页面数量: {stats['pages']}")
        print(f"  数据库文件: {stats['db_path']}")
        print("=" * 60)
        
        # 显示缓存信息
        print("\n爬取缓存信息:")
        print(f"  最后更新时间: {self.cache.get('last_update', '从未')}")
        print(f"  已爬取技能: {len(self.cache['skills'])}")
        print(f"  已爬取宠物: {len(self.cache['pets'])}")
        print(f"  已爬取道具: {len(self.cache['items'])}")
        print(f"  已爬取精灵蛋: {len(self.cache['eggs'])}")
        print(f"  已爬取家具: {len(self.cache['furniture'])}")
        print(f"  已爬取地区: {len(self.cache['regions'])}")
        print(f"  已爬取副本: {len(self.cache['dungeons'])}")
        print("=" * 60)
    
    def clear_cache(self):
        """清除缓存"""
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
            print("缓存已清除")
        else:
            print("缓存文件不存在")


def main():
    parser = argparse.ArgumentParser(description='洛克王国 Wiki 增量更新工具')
    parser.add_argument('action', choices=['update', 'stats', 'clear-cache'],
                       help='操作类型')
    parser.add_argument('--type', choices=['all', 'skills', 'pets', 'items', 'eggs', 'furniture'],
                       default='all', help='要更新的数据类型')
    parser.add_argument('--force', action='store_true', help='强制重新爬取（忽略缓存）')
    parser.add_argument('--only-images', action='store_true', help='只下载缺失的图片（仅适用于 pets）')
    parser.add_argument('--db', default='./wiki-local.db', help='数据库路径')
    
    args = parser.parse_args()
    
    updater = IncrementalUpdater(args.db)
    
    if args.action == 'stats':
        updater.show_stats()
    elif args.action == 'clear-cache':
        updater.clear_cache()
    elif args.action == 'update':
        if args.type == 'all' or args.type == 'skills':
            updater.update_skills(force=args.force)
        
        if args.type == 'all' or args.type == 'pets':
            updater.update_pets(force=args.force, only_images=args.only_images)
        
        if args.type == 'all' or args.type == 'items':
            updater.update_items(force=args.force, only_images=args.only_images)
        
        if args.type == 'all' or args.type == 'eggs':
            updater.update_eggs(force=args.force, only_images=args.only_images)
        
        if args.type == 'all' or args.type == 'furniture':
            updater.update_furniture(force=args.force, only_images=args.only_images)
        
        # 注意：地区和副本需要特殊处理，暂未实现
        # if args.type == 'all' or args.type == 'regions':
        #     updater.update_regions(force=args.force)
        
        # if args.type == 'all' or args.type == 'dungeons':
        #     updater.update_dungeons(force=args.force)
        
        print("\n✅ 更新完成！")
        updater.show_stats()


if __name__ == "__main__":
    from datetime import datetime
    main()
