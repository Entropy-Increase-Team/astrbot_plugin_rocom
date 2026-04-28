"""
图片下载工具
从URL下载图片并保存到本地
"""
import os
import re
from pathlib import Path
from typing import Optional
from src.config import fetch_with_retry


def sanitize_filename(name: str) -> str:
    """
    清理文件名，移除非法字符
    
    Args:
        name: 原始文件名
    
    Returns:
        清理后的文件名
    """
    # 替换Windows和Linux文件系统不允许的字符
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    # 限制长度
    if len(sanitized) > 100:
        sanitized = sanitized[:100]
    return sanitized


def extract_filename_from_url(url: str) -> str:
    """
    从URL中提取文件名
    
    Args:
        url: 图片URL
    
    Returns:
        文件名
    """
    # 获取最后一个斜杠后的部分
    filename = url.split('/')[-1]
    # URL解码并清理
    from urllib.parse import unquote
    filename = unquote(filename)
    return sanitize_filename(filename)


def download_image(url: str, save_dir: str, filename: Optional[str] = None) -> Optional[str]:
    """
    下载图片并保存到指定目录
    
    Args:
        url: 图片URL
        save_dir: 保存目录
        filename: 自定义文件名（可选）
    
    Returns:
        本地文件路径（统一使用 / 分隔符），失败返回None
    """
    try:
        # 创建保存目录
        os.makedirs(save_dir, exist_ok=True)
        
        # 确定文件名
        if not filename:
            filename = extract_filename_from_url(url)
        else:
            filename = sanitize_filename(filename)
        
        # 确保有扩展名
        if '.' not in filename:
            # 尝试从 URL 中推断扩展名
            ext_match = re.search(r'\.(png|jpg|jpeg|gif|webp)', url, re.IGNORECASE)
            if ext_match:
                filename += f".{ext_match.group(1).lower()}"
            else:
                filename += ".png"  # 默认 PNG
        
        file_path = os.path.join(save_dir, filename)
        
        # 如果文件已存在，跳过下载
        if os.path.exists(file_path):
            print(f"  [跳过] 图片已存在: {file_path}")
            # 统一返回 Linux 兼容的路径格式
            return file_path.replace('\\', '/')
        
        # 下载图片
        print(f"  [下载] {url}")
        response = fetch_with_retry(url)
        
        if response and response.status_code == 200:
            with open(file_path, 'wb') as f:
                f.write(response.content)
            print(f"  [成功] 保存到: {file_path}")
            # 统一返回 Linux 兼容的路径格式
            return file_path.replace('\\', '/')
        else:
            print(f"  [失败] HTTP {response.status_code if response else '无响应'}")
            return None
            
    except Exception as e:
        print(f"  [错误] 下载失败: {e}")
        return None


def download_pet_sprite(pet_name: str, image_url: str, output_dir: str = "./output/images/pets") -> Optional[str]:
    """
    下载宠物立绘图片
    
    Args:
        pet_name: 宠物名称
        image_url: 图片URL
        output_dir: 输出目录
    
    Returns:
        本地文件路径，失败返回None
    """
    # 使用宠物名称作为文件名前缀
    filename = f"{sanitize_filename(pet_name)}.png"
    return download_image(image_url, output_dir, filename)


def download_skill_icon(skill_name: str, image_url: str, output_dir: str = "./output/images/skills") -> Optional[str]:
    """
    下载技能图标图片
    
    Args:
        skill_name: 技能名称
        image_url: 图片URL
        output_dir: 输出目录
    
    Returns:
        本地文件路径，失败返回None
    """
    # 使用技能名称作为文件名前缀
    filename = f"{sanitize_filename(skill_name)}.png"
    return download_image(image_url, output_dir, filename)


def download_item_image(item_name: str, image_url: str, category: str = "其他", base_dir: str = "./output/images/items") -> Optional[str]:
    """
    下载道具图片（按分类组织目录）
    
    Args:
        item_name: 道具名称
        image_url: 图片URL
        category: 道具分类（用于组织目录）
        base_dir: 基础目录
    
    Returns:
        本地文件路径，失败返回None
    """
    # 清理分类名称，用作目录名
    safe_category = sanitize_filename(category) if category else "其他"
    # 构建输出目录：base_dir/category/
    output_dir = os.path.join(base_dir, safe_category)
    
    # 使用道具名称作为文件名
    filename = f"{sanitize_filename(item_name)}.png"
    return download_image(image_url, output_dir, filename)


if __name__ == "__main__":
    # 测试下载功能
    test_url = "https://patchwiki.biligame.com/images/rocom/thumb/2/25/o64cvcxq1l6tlur77xjqbwx2s4imabd.png/80px-%E9%A1%B5%E9%9D%A2_%E5%AE%A0%E7%89%A9_%E7%AB%8B%E7%BB%98_%E8%BF%AA%E8%8E%AB_1.png"
    result = download_image(test_url, "./output/images/test")
    print(f"下载结果: {result}")
