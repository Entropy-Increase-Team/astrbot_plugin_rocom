#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试Wiki数据库图片路径是否正确（跨平台兼容）"""

import sqlite3
import os

# 数据库路径
db_path = "H:/code/astrbot_plugin_rocom/wiki/wiki-local.db"
plugin_dir = "H:/code/astrbot_plugin_rocom/wiki"

def resolve_wiki_path(relative_path):
    """模拟代码中的路径解析逻辑"""
    if not relative_path:
        return ''
    
    # 清理路径前缀
    if relative_path.startswith('./') or relative_path.startswith('.\\'):
        relative_path = relative_path[2:]
    
    # 如果是绝对路径，直接返回
    if os.path.isabs(relative_path):
        return relative_path.replace('\\', '/')
    
    # 获取wiki目录
    wiki_dir = plugin_dir
    
    # 使用 / 拼接，确保跨平台兼容
    full_path = wiki_dir.rstrip('/\\') + '/' + relative_path.replace('\\', '/')
    return full_path

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("=" * 80)
print("测试Wiki数据库图片路径（跨平台兼容）")
print("=" * 80)

# 测试宠物图片
print("\n【宠物图片测试】")
cursor.execute("SELECT name, sprite_image_local FROM pets WHERE sprite_image_local IS NOT NULL LIMIT 3")
for name, img_path in cursor.fetchall():
    full_path = resolve_wiki_path(img_path)
    exists = os.path.exists(full_path)
    
    status = "✅" if exists else "❌"
    print(f"{status} {name}")
    print(f"   DB路径: {img_path}")
    print(f"   完整路径: {full_path}")
    print(f"   存在: {exists}")
    print(f"   分隔符: {'/' if '/' in full_path else '\\'}")

# 测试道具图片
print("\n【道具图片测试】")
cursor.execute("SELECT name, image_local FROM items WHERE image_local IS NOT NULL LIMIT 3")
for name, img_path in cursor.fetchall():
    full_path = resolve_wiki_path(img_path)
    exists = os.path.exists(full_path)
    
    status = "✅" if exists else "❌"
    print(f"{status} {name}")
    print(f"   DB路径: {img_path}")
    print(f"   完整路径: {full_path}")
    print(f"   存在: {exists}")
    print(f"   分隔符: {'/' if '/' in full_path else '\\'}")

# 测试技能图标
print("\n【技能图标测试】")
cursor.execute("SELECT name, icon_image_local FROM skills WHERE icon_image_local IS NOT NULL LIMIT 3")
for name, img_path in cursor.fetchall():
    full_path = resolve_wiki_path(img_path)
    exists = os.path.exists(full_path)
    
    status = "✅" if exists else "❌"
    print(f"{status} {name}")
    print(f"   DB路径: {img_path}")
    print(f"   完整路径: {full_path}")
    print(f"   存在: {exists}")
    print(f"   分隔符: {'/' if '/' in full_path else '\\'}")

conn.close()
print("\n" + "=" * 80)
print("测试完成！所有路径统一使用 / 分隔符，跨平台兼容")
print("=" * 80)
