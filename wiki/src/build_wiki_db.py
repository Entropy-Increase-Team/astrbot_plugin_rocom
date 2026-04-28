"""
Wiki 数据爬取和数据库构建脚本
从 Wiki 爬取所有数据并存储到本地 SQLite 数据库
"""
import os
import sys

# 添加项目根目录到 Python 路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import time
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


def get_all_wiki_pages():
    """
    获取 Wiki 中所有页面列表
    
    Returns:
        页面标题列表
    """
    print("正在获取所有页面列表...")
    pages = []
    apcontinue = None
    
    while True:
        params = {
            "action": "query",
            "list": "allpages",
            "aplimit": "500",
            "format": "json",
        }
        
        if apcontinue:
            params["apcontinue"] = apcontinue
        
        response = fetch_with_retry(API_URL, params=params)
        if not response:
            print("  请求失败，跳过剩余页面")
            break
        
        data = response.json()
        page_list = data.get("query", {}).get("allpages", [])
        pages.extend([p["title"] for p in page_list])
        
        print(f"  已获取 {len(pages)} 个页面...")
        
        # 检查是否有更多页面
        if "continue" not in data:
            break
        apcontinue = data["continue"].get("apcontinue")
        
        time.sleep(0.5)  # 避免请求过快
    
    print(f"总共找到 {len(pages)} 个页面")
    return pages


def fetch_page_wikitext(title: str):
    """
    获取页面的 Wiki 文本
    
    Args:
        title: 页面标题
    
    Returns:
        Wiki 文本内容，失败返回 None
    """
    url = f"{BASE_URL}/index.php"
    params = {"title": title, "action": "raw"}
    
    response = fetch_with_retry(url, params=params)
    if response:
        return response.text
    return None


def build_database(full_wiki=False):
    """
    爬取数据并构建本地数据库
    
    Args:
        full_wiki: 是否爬取整个 Wiki（包括所有页面）
    """
    db = WikiLocalDB("./wiki-local.db")
    
    try:
        print("=" * 60)
        print("洛克王国 Wiki 本地数据库构建工具")
        if full_wiki:
            print("模式: 全量 Wiki 爬取")
        else:
            print("模式: 仅宠物和技能数据")
        print("=" * 60)
        
        # 1. 爬取技能数据
        if full_wiki:
            print("\n【步骤 1/5】爬取技能索引...")
        else:
            print("\n【步骤 1/4】爬取技能索引...")
        skill_names = crawl_skill_index()
        print(f"找到 {len(skill_names)} 个技能\n")
        
        print("【步骤 2/5】爬取技能详情并下载图标..." if full_wiki else "【步骤 2/4】爬取技能详情并下载图标...")
        success_count = 0
        failed_count = 0
        image_success = 0
        
        for i, name in enumerate(skill_names, 1):
            skill = crawl_skill(name)
            if skill:
                # 下载技能图标
                icon_local_path = None
                if skill.icon_image:
                    icon_local_path = download_skill_icon(name, skill.icon_image)
                    if icon_local_path:
                        image_success += 1
                
                db.save_skill({
                    'name': skill.name,
                    'element': skill.element,
                    'category': skill.category,
                    'cost': skill.cost,
                    'power': skill.power,
                    'effect': skill.effect,
                    'iconImage': skill.icon_image,
                    'iconImageLocal': icon_local_path,
                })
                success_count += 1
                if i % 100 == 0 or i == len(skill_names):
                    print(f"  [{i}/{len(skill_names)}] 进度: {success_count} 成功, {failed_count} 失败, {image_success} 图片")
            else:
                failed_count += 1
            
            # 每 50 个延迟一下，避免被封
            if i % 50 == 0:
                time.sleep(2)
            else:
                time.sleep(0.1)
        
        print(f"技能爬取完成: 成功 {success_count}, 失败 {failed_count}, 图片 {image_success}\n")
        
        # 2. 爬取宠物数据
        print("【步骤 3/5】爬取宠物索引..." if full_wiki else "【步骤 3/4】爬取宠物索引...")
        pet_names = crawl_all_pets()
        print(f"找到 {len(pet_names)} 个宠物\n")
        
        print("【步骤 4/5】爬取宠物详情并下载立绘..." if full_wiki else "【步骤 4/4】爬取宠物详情并下载立绘...")
        pet_success = 0
        pet_failed = 0
        pet_image_success = 0
        
        for i, name in enumerate(pet_names, 1):
            pet = crawl_pet(name)
            if pet:
                # 下载宠物立绘
                sprite_local_path = None
                if pet.sprite_image:
                    sprite_local_path = download_pet_sprite(name, pet.sprite_image)
                    if sprite_local_path:
                        pet_image_success += 1
                
                # 转换为字典格式
                pet_dict = {
                    'id': pet.id,
                    'name': pet.name,
                    'form': pet.form,
                    'regionalFormName': pet.regional_form_name,
                    'initialStageName': pet.initial_stage_name,
                    'hasAltColor': pet.has_alt_color,
                    'stage': pet.stage,
                    'type': pet.type,
                    'description': pet.description,
                    'element': pet.element,
                    'element2': pet.element2,
                    'ability': pet.ability,
                    'abilityDesc': pet.ability_desc,
                    'hp': pet.hp,
                    'physicalAttack': pet.physical_attack,
                    'magicAttack': pet.magic_attack,
                    'physicalDefense': pet.physical_defense,
                    'magicDefense': pet.magic_defense,
                    'speed': pet.speed,
                    'size': pet.size,
                    'weight': pet.weight,
                    'distribution': pet.distribution,
                    'questTasks': pet.quest_tasks,
                    'questSkillStones': pet.quest_skill_stones,
                    'skills': pet.skills,
                    'skillUnlockLevels': pet.skill_unlock_levels,
                    'bloodlineSkills': pet.bloodline_skills,
                    'learnableSkillStones': pet.learnable_skill_stones,
                    'evolutionCondition': pet.evolution_condition,
                    'updateVersion': pet.update_version,
                    'spriteImage': pet.sprite_image,
                    'spriteImageLocal': sprite_local_path,
                }
                
                db.save_pet(pet_dict)
                pet_success += 1
                if i % 100 == 0 or i == len(pet_names):
                    print(f"  [{i}/{len(pet_names)}] 进度: {pet_success} 成功, {pet_failed} 失败, {pet_image_success} 图片")
            else:
                pet_failed += 1
            
            # 每 20 个延迟一下
            if i % 20 == 0:
                time.sleep(2)
            else:
                time.sleep(0.1)
        
        print(f"宠物爬取完成: 成功 {pet_success}, 失败 {pet_failed}, 图片 {pet_image_success}\n")
        
        # 3. 爬取道具数据
        if full_wiki:
            print("【步骤 5/6】爬取道具索引...")
        else:
            print("【步骤 5/5】爬取道具索引...")
        item_names = crawl_category("道具", "道具")
        print(f"找到 {len(item_names)} 个道具\n")
        
        if full_wiki:
            print("【步骤 6/6】爬取道具详情...")
        else:
            print("【步骤 6/5】爬取道具详情...")
        item_success = 0
        item_failed = 0
        
        for i, name in enumerate(item_names, 1):
            item = crawl_item(name)
            if item:
                db.save_item(item)
                item_success += 1
                if i % 200 == 0 or i == len(item_names):
                    print(f"  [{i}/{len(item_names)}] 进度: {item_success} 成功, {item_failed} 失败")
            else:
                item_failed += 1
            
            # 每 100 个延迟一下
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)  # 道具页面简单，可以更快
        
        print(f"道具爬取完成: 成功 {item_success}, 失败 {item_failed}\n")
        
        # 4. 爬取精灵蛋数据（从已爬取的道具中筛选）
        if full_wiki:
            print("【步骤 7/10】处理精灵蛋数据...")
        else:
            print("【步骤 7/9】处理精灵蛋数据...")
        
        # 从数据库中获取所有分类为"精灵蛋"的道具
        cursor = db.conn.cursor()
        cursor.execute("SELECT name FROM items WHERE category = '精灵蛋'")
        egg_names = [row[0] for row in cursor.fetchall()]
        
        print(f"从数据库中找到 {len(egg_names)} 个精灵蛋\n")
        
        if full_wiki:
            print("【步骤 8/10】爬取精灵蛋详情并下载图片...")
        else:
            print("【步骤 8/9】爬取精灵蛋详情并下载图片...")
        egg_success = 0
        egg_failed = 0
        egg_image_success = 0
        
        for i, name in enumerate(egg_names, 1):
            egg = crawl_egg(name)
            if egg:
                # 下载精灵蛋图片
                if egg.get('image_url'):
                    category = egg.get('category', '精灵蛋')
                    image_local = download_item_image(name, egg['image_url'], category)
                    if image_local:
                        egg['image_local'] = image_local
                        egg_image_success += 1
                
                db.save_egg(egg)
                egg_success += 1
                if i % 100 == 0 or i == len(egg_names):
                    print(f"  [{i}/{len(egg_names)}] 进度: {egg_success} 成功, {egg_failed} 失败, {egg_image_success} 图片")
            else:
                egg_failed += 1
            
            # 每 100 个延迟一下
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)
        
        print(f"精灵蛋爬取完成: 成功 {egg_success}, 失败 {egg_failed}, 图片 {egg_image_success}\n")
        
        # 5. 爬取家具数据（从已爬取的道具中筛选）
        if full_wiki:
            print("【步骤 9/10】处理家具数据...")
        else:
            print("【步骤 9/9】处理家具数据...")
        
        # 从数据库中获取所有分类为"家具"的道具
        cursor = db.conn.cursor()
        cursor.execute("SELECT name FROM items WHERE category = '家具'")
        furniture_names = [row[0] for row in cursor.fetchall()]
        
        print(f"从数据库中找到 {len(furniture_names)} 个家具\n")
        
        if full_wiki:
            print("【步骤 10/10】爬取家具详情并下载图片...")
        else:
            print("【步骤 10/9】爬取家具详情并下载图片...")
        furniture_success = 0
        furniture_failed = 0
        furniture_image_success = 0
        
        for i, name in enumerate(furniture_names, 1):
            furniture = crawl_furniture(name)
            if furniture:
                # 下载家具图片
                if furniture.get('image_url'):
                    category = furniture.get('category', '家具')
                    image_local = download_item_image(name, furniture['image_url'], category)
                    if image_local:
                        furniture['image_local'] = image_local
                        furniture_image_success += 1
                
                db.save_furniture(furniture)
                furniture_success += 1
                if i % 100 == 0 or i == len(furniture_names):
                    print(f"  [{i}/{len(furniture_names)}] 进度: {furniture_success} 成功, {furniture_failed} 失败, {furniture_image_success} 图片")
            else:
                furniture_failed += 1
            
            # 每 100 个延迟一下
            if i % 100 == 0:
                time.sleep(2)
            else:
                time.sleep(0.05)
        
        print(f"家具爬取完成: 成功 {furniture_success}, 失败 {furniture_failed}, 图片 {furniture_image_success}\n")
        
        # 6. 地区和副本数据
        # 注意：地区和副本不是独立的Wiki页面，而是嵌入在图鉴页面中的模板数据
        # 需要特殊的解析逻辑，暂时跳过
        print("【提示】地区和副本数据需要特殊处理，暂未实现")
        print("       它们嵌入在图鉴页面中，不是独立的Wiki页面\n")
        
        # 4. 可选：爬取整个 Wiki
        if full_wiki:
            print("\n【步骤 7/7】爬取整个 Wiki 所有页面...")
            all_pages = get_all_wiki_pages()
            
            # 过滤掉已经爬取的宠物和技能页面
            crawled_titles = set(skill_names + pet_names)
            remaining_pages = [p for p in all_pages if p not in crawled_titles]
            
            print(f"需要爬取 {len(remaining_pages)} 个额外页面\n")
            
            page_success = 0
            page_failed = 0
            
            for i, title in enumerate(remaining_pages, 1):
                wikitext = fetch_page_wikitext(title)
                if wikitext is not None:
                    # 判断页面类型
                    page_type = 'other'
                    if title.startswith("道具:") or "道具" in title:
                        page_type = 'item'
                    elif title.startswith("地图:") or "地图" in title:
                        page_type = 'map'
                    elif title.startswith("任务:") or "任务" in title:
                        page_type = 'quest'
                    
                    db.save_page(title, wikitext, page_type=page_type)
                    page_success += 1
                    
                    if i % 100 == 0:
                        print(f"  [{i}/{len(remaining_pages)}] 进度: {page_success} 成功, {page_failed} 失败")
                else:
                    page_failed += 1
                
                # 每 50 个延迟
                if i % 50 == 0:
                    time.sleep(2)
                else:
                    time.sleep(0.2)
            
            print(f"\n额外页面爬取完成: 成功 {page_success}, 失败 {page_failed}\n")
        
        # 8. 显示统计信息
        stats = db.get_stats()
        print("=" * 60)
        print("数据库构建完成！")
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
        
    except KeyboardInterrupt:
        print("\n\n用户中断操作")
        stats = db.get_stats()
        print(f"\n当前进度: 宠物 {stats['pets']}, 技能 {stats['skills']}")
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


def test_query():
    """测试数据库查询功能"""
    if not os.path.exists("./wiki-local.db"):
        print("错误: 数据库文件不存在，请先运行 build 命令构建数据库")
        return
    
    db = WikiLocalDB("./wiki-local.db")
    
    try:
        print("=" * 60)
        print("数据库查询测试")
        print("=" * 60)
        
        # 测试 1: 查询宠物
        print("\n【测试 1】查询宠物 '炽心勇狮'...")
        pet = db.query_pet("炽心勇狮")
        if pet:
            print(f"  名称: {pet['name']}")
            print(f"  ID: {pet['id']}")
            print(f"  属性: {pet['element']}/{pet.get('element2', '无')}")
            print(f"  HP: {pet['hp']}, 物攻: {pet['physicalAttack']}, 魔攻: {pet['magicAttack']}")
            print(f"  特性: {pet['ability']}")
        else:
            print("  未找到该宠物")
        
        # 测试 2: 查询技能
        print("\n【测试 2】查询技能 '暗突袭'...")
        skill = db.query_skill("暗突袭")
        if skill:
            print(f"  名称: {skill['name']}")
            print(f"  属性: {skill['element']}")
            print(f"  类别: {skill['category']}")
            print(f"  威力: {skill['power']}, 耗能: {skill['cost']}")
        else:
            print("  未找到该技能")
        
        # 测试 3: 搜索宠物
        print("\n【测试 3】搜索包含 '喵' 的宠物...")
        results = db.search_pets("喵", limit=5)
        if results:
            for r in results:
                print(f"  - {r['name']} (ID:{r['id']}, {r['element']})")
        else:
            print("  未找到相关宠物")
        
        # 测试 4: 属性克制
        print("\n【测试 4】查询 '火' 属性的克制关系...")
        advantage = db.get_type_advantage("火")
        print(f"  攻击方: {advantage['attack_type']}")
        print(f"  克制: {advantage['strong_against']}")
        print(f"  被抵抗: {advantage['weak_against']}")
        print(f"  无效: {advantage['immune_against']}")
        
        # 测试 5: 统计信息
        print("\n【测试 5】数据库统计...")
        stats = db.get_stats()
        print(f"  宠物总数: {stats['pets']}")
        print(f"  技能总数: {stats['skills']}")
        print(f"  页面总数: {stats['pages']}")
        
        print("\n" + "=" * 60)
        print("测试完成！")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == "build":
            # 检查是否有 --full 参数
            full_wiki = "--full" in sys.argv
            build_database(full_wiki=full_wiki)
        elif command == "test":
            test_query()
        else:
            print(f"未知命令: {command}")
            print("可用命令: build [--full], test")
    else:
        print("用法:")
        print("  python -m src.build_wiki_db build       - 仅爬取宠物和技能")
        print("  python -m src.build_wiki_db build --full - 爬取整个 Wiki")
        print("  python -m src.build_wiki_db test        - 测试查询功能")
        print()
        print("示例:")
        print("  python -m src.build_wiki_db build       # 快速模式（推荐）")
        print("  python -m src.build_wiki_db build --full # 全量模式（耗时较长）")
