"""
爬取并保存所有更新日志到数据库
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.update_log_crawler import get_all_update_logs, crawl_update_log
from src.wiki_local_db import WikiLocalDB


def main():
    print("=" * 60)
    print("开始爬取更新日志...")
    print("=" * 60)
    
    # 初始化数据库
    db = WikiLocalDB()
    
    # 获取所有更新日志标题
    print("\n正在获取更新日志列表...")
    titles = get_all_update_logs()
    
    if not titles:
        print("❌ 未找到任何更新日志")
        return
    
    print(f"✅ 找到 {len(titles)} 个更新日志\n")
    
    # 爬取每个更新日志
    success_count = 0
    failed_count = 0
    
    for i, title in enumerate(titles, 1):
        print(f"[{i}/{len(titles)}] 爬取: {title}")
        
        log_data = crawl_update_log(title)
        
        if log_data:
            # 保存到数据库
            db.save_update_log(log_data)
            success_count += 1
            
            # 显示简要信息
            pet_count = len(log_data.get('pet_changes', []))
            skill_count = len(log_data.get('skill_changes', []))
            other_count = len(log_data.get('other_changes', []))
            
            print(f"  ✅ 成功 - 宠物改动:{pet_count}, 技能改动:{skill_count}, 其他:{other_count}")
        else:
            failed_count += 1
            print(f"  ❌ 失败")
        
        # 每5个延迟一下，避免请求过快
        if i % 5 == 0:
            print("  (暂停2秒...)")
            import time
            time.sleep(2)
        else:
            import time
            time.sleep(0.3)
    
    print("\n" + "=" * 60)
    print(f"爬取完成！成功: {success_count}, 失败: {failed_count}")
    print("=" * 60)
    
    # 显示最近的更新日志
    print("\n最近的5条更新日志:")
    latest = db.get_latest_updates(5)
    for log in latest:
        print(f"\n  📅 {log['date']} - {log['title']}")
        if log['pet_changes']:
            print(f"     宠物改动: {len(log['pet_changes'])} 条")
        if log['skill_changes']:
            print(f"     技能改动: {len(log['skill_changes'])} 条")
    
    # 关闭数据库
    db.close()


if __name__ == "__main__":
    main()
