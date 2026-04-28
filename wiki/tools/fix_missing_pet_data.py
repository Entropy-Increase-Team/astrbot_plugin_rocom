# -*- coding: utf-8 -*-
"""
补全宠物缺失数据脚本
检查数据库中六维或图片路径缺失的宠物，重新爬取并更新
"""
import sys
import os
import json
import sqlite3

# 添加项目路径（指向 wiki 目录）
# 当前文件位置: wiki/tools/fix_missing_pet_data.py
# 需要导入: wiki/src/pet_detail 和 wiki/src/image_downloader
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.pet_detail import crawl_pet
from src.image_downloader import download_image


def check_missing_data():
    """检查数据库中缺失数据的宠物"""
    # 数据库在 wiki 目录
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wiki-local.db")
    
    if not os.path.exists(db_path):
        print(f"❌ 数据库不存在: {db_path}")
        return []
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 查询数据不完整的宠物
    # 检查条件：
    # 1. 六维有任意一个为0
    # 2. 图片路径为空
    # 3. 特性描述为空
    # 4. 图鉴课题为空
    # 5. 血脉技能为空
    # 6. 可学技能石为空
    # 7. 课题技能石为空
    cursor.execute("""
        SELECT name, hp, physical_attack, magic_attack, 
               physical_defense, magic_defense, speed,
               sprite_image_local, ability_desc, quest_tasks,
               bloodline_skills, learnable_skill_stones,
               quest_skill_stones
        FROM pets
        WHERE (hp = 0 OR physical_attack = 0 OR magic_attack = 0 
               OR physical_defense = 0 OR magic_defense = 0 OR speed = 0)
           OR sprite_image_local IS NULL
           OR sprite_image_local = ''
           OR ability_desc IS NULL
           OR ability_desc = ''
           OR quest_tasks IS NULL
           OR quest_tasks = '[]'
           OR bloodline_skills IS NULL
           OR bloodline_skills = '[]'
           OR learnable_skill_stones IS NULL
           OR learnable_skill_stones = '[]'
           OR quest_skill_stones IS NULL
           OR quest_skill_stones = ''
           OR quest_skill_stones = '[]'
        ORDER BY name
    """)
    
    missing_pets = cursor.fetchall()
    conn.close()
    
    return missing_pets


def update_pet_data(pet_name: str) -> bool:
    """
    爬取并更新单个宠物的数据
    
    Args:
        pet_name: 宠物名称
    
    Returns:
        是否成功更新
    """
    print(f"\n正在处理: {pet_name}")
    
    try:
        # 爬取宠物详情
        detail = crawl_pet(pet_name)
        
        if not detail:
            print(f"  爬取失败")
            return False
        
        print(f"  爬取成功")
        
        # 下载图片
        image_path = None
        if detail.sprite_image:
            print(f"  下载图片: {detail.sprite_image}")
            # 使用正确的输出目录（wiki 目录的 output/images/pets）
            output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "images", "pets")
            image_path = download_image(detail.sprite_image, output_dir, f"{pet_name}.png")
            if image_path:
                # 转换为相对路径（与数据库其他记录保持一致）
                rel_path = os.path.relpath(image_path, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                rel_path = rel_path.replace('\\', '/')
                image_path = f"./{rel_path}"
                print(f"  图片已保存: {image_path}")
            else:
                print(f"  图片下载失败")
        
        # 更新数据库
        # 数据库在 wiki 目录
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wiki-local.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 规范化图片路径格式（保持 ./ 前缀）
        normalized_image_path = None
        if image_path:
            # 统一使用标准相对路径格式: ./output/images/pets/xxx.png
            # 确保路径以 ./output/images/ 开头
            if not image_path.startswith('./'):
                # 如果路径不以 ./ 开头，添加它
                clean_path = image_path.lstrip('/')
                normalized_image_path = './' + clean_path.replace('\\', '/')
            else:
                normalized_image_path = image_path.replace('\\', '/')
        
        cursor.execute("""
            UPDATE pets 
            SET hp = ?,
                physical_attack = ?,
                magic_attack = ?,
                physical_defense = ?,
                magic_defense = ?,
                speed = ?,
                sprite_image_local = ?,
                element = ?,
                stage = ?,
                form = ?,
                ability_desc = ?,
                quest_tasks = ?,
                bloodline_skills = ?,
                learnable_skill_stones = ?,
                skill_unlock_levels = ?,
                quest_skill_stones = ?
            WHERE name = ?
        """, (
            detail.hp,
            detail.physical_attack,
            detail.magic_attack,
            detail.physical_defense,
            detail.magic_defense,
            detail.speed,
            normalized_image_path,
            detail.element,
            detail.stage,
            detail.form,
            detail.ability_desc or '',
            json.dumps(detail.quest_tasks, ensure_ascii=False) if detail.quest_tasks else '[]',
            json.dumps(detail.bloodline_skills, ensure_ascii=False) if detail.bloodline_skills else '[]',
            json.dumps(detail.learnable_skill_stones, ensure_ascii=False) if detail.learnable_skill_stones else '[]',
            json.dumps(detail.skill_unlock_levels, ensure_ascii=False) if detail.skill_unlock_levels else '[]',
            json.dumps(detail.quest_skill_stones, ensure_ascii=False) if detail.quest_skill_stones else '[]',
            pet_name
        ))
        
        conn.commit()
        conn.close()
        
        print(f"  数据库已更新")
        print(f"     HP={detail.hp}, PA={detail.physical_attack}, MA={detail.magic_attack}")
        print(f"     PD={detail.physical_defense}, MD={detail.magic_defense}, SPD={detail.speed}")
        print(f"     属性={detail.element}, 阶段={detail.stage}, 形态={detail.form}")
        if detail.ability_desc:
            print(f"     特性描述: {detail.ability_desc[:50]}...")
        if detail.quest_tasks:
            print(f"     图鉴课题: {len(detail.quest_tasks)}个")
        if detail.bloodline_skills:
            print(f"     血脉技能: {len(detail.bloodline_skills)}个")
        if detail.learnable_skill_stones:
            print(f"     可学技能石: {len(detail.learnable_skill_stones)}个")
        if detail.quest_skill_stones:
            print(f"     课题技能石: {len(detail.quest_skill_stones)}个")
        
        return True
        
    except Exception as e:
        print(f"  错误: {e}")
        return False


def main():
    """主函数"""
    print("=" * 60)
    print("检查数据库中缺失数据的宠物...")
    print("=" * 60)
    
    missing_pets = check_missing_data()
    
    if not missing_pets:
        print("\n所有宠物数据完整！")
        return
    
    print(f"\n发现 {len(missing_pets)} 个宠物需要补全数据:\n")
    
    for i, pet in enumerate(missing_pets, 1):
        name = pet[0]
        hp, pa, ma, pd, md, spd = pet[1], pet[2], pet[3], pet[4], pet[5], pet[6]
        no_image = (pet[7] is None or pet[7] == '')
        no_ability_desc = (pet[8] is None or pet[8] == '')
        no_quest_tasks = (pet[9] is None or pet[9] == '' or pet[9] == '[]')
        no_bloodline_skills = (pet[10] is None or pet[10] == '' or pet[10] == '[]')
        no_learnable_stones = (pet[11] is None or pet[11] == '' or pet[11] == '[]')
        no_quest_skill_stones = (pet[12] is None or pet[12] == '' or pet[12] == '[]')
        
        # 检查哪些六维是0
        missing_stats = []
        if hp == 0: missing_stats.append("HP")
        if pa == 0: missing_stats.append("PA")
        if ma == 0: missing_stats.append("MA")
        if pd == 0: missing_stats.append("PD")
        if md == 0: missing_stats.append("MD")
        if spd == 0: missing_stats.append("SPD")
        
        issues = []
        if missing_stats:
            if len(missing_stats) == 6:
                issues.append("六维全为0")
            else:
                issues.append(f"缺失{','.join(missing_stats)}")
        if no_image:
            issues.append("无图片")
        if no_ability_desc:
            issues.append("无特性描述")
        if no_quest_tasks:
            issues.append("无图鉴课题")
        if no_bloodline_skills:
            issues.append("无血脉技能")
        if no_learnable_stones:
            issues.append("无可学技能石")
        if no_quest_skill_stones:
            issues.append("无课题技能石")
        
        print(f"{i}. {name} - {', '.join(issues)}")
    
    print("\n" + "=" * 60)
    response = input("\n是否开始补全？(y/n): ").strip().lower()
    
    if response != 'y':
        print("已取消")
        return
    
    print("\n" + "=" * 60)
    print("开始补全数据...")
    print("=" * 60)
    
    success_count = 0
    fail_count = 0
    
    for i, pet in enumerate(missing_pets, 1):
        name = pet[0]
        print(f"\n[{i}/{len(missing_pets)}]", end="")
        
        if update_pet_data(name):
            success_count += 1
        else:
            fail_count += 1
    
    print("\n" + "=" * 60)
    print(f"完成！")
    print(f"   成功: {success_count}")
    print(f"   失败: {fail_count}")
    print("=" * 60)


if __name__ == "__main__":
    main()
