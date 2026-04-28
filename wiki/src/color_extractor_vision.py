# -*- coding: utf-8 -*-
"""
颜色提取服务 - 基于视觉大模型
使用 Qwen-VL 等多模态模型识别图片主色调
"""
import os
import base64
import requests
from typing import List, Optional, Dict


class ColorExtractor:
    """颜色提取器 - 基于视觉大模型"""
    
    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        """
        初始化颜色提取器
        
        Args:
            api_key: API密钥
            base_url: API基础URL
            model: 模型名称
        """
        # 默认配置（可从环境变量或配置文件读取）
        self.api_key = api_key or os.getenv("VISION_API_KEY", "sk-lm-fWHsHRVg:v8hrxBv4zzTDSkT4CdF4")
        self.base_url = base_url or os.getenv("VISION_BASE_URL", "http://192.168.31.91:1234/v1")
        self.model = model or os.getenv("VISION_MODEL", "qwen/qwen3.5-9b-q6_k.gguf")
    
    def extract_main_colors(self, image_path: str, top_n: int = 2) -> Optional[Dict]:
        """
        从图片中提取主要颜色（主色+副色）
        
        Args:
            image_path: 图片路径
            top_n: 返回前 N 种主要颜色（默认2：主色+副色）
            
        Returns:
            {
                'main_color': str,  # 主要颜色（单字）
                'secondary_color': str,  # 次要颜色（单字，可能为None）
                'colors': List[str],  # 所有检测到的颜色列表
                'rgb_values': List[tuple],  # RGB值（视觉模型不提供，设为空）
                'color_ratios': List[float],  # 颜色占比（视觉模型不提供，设为空）
            }
        """
        try:
            if not os.path.exists(image_path):
                print(f"❌ 图片不存在: {image_path}")
                return None
            
            # 读取图片并转为base64
            with open(image_path, 'rb') as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')
            
            # 构建提示词
            prompt = self._build_prompt(top_n)
            
            # 调用视觉模型API
            response_text = self._call_vision_api(image_data, prompt)
            
            if not response_text:
                return None
            
            # 解析响应
            return self._parse_response(response_text, top_n)
            
        except Exception as e:
            print(f"❌ 颜色提取错误: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _build_prompt(self, top_n: int) -> str:
        """
        构建提示词
        
        Args:
            top_n: 需要识别的颜色数量
            
        Returns:
            提示词字符串
        """
        if top_n == 1:
            return (
                "请分析这张图片的主色调是什么颜色？\n"
                "要求：\n"
                "1. 只输出一个中文颜色名称（如：红、橙、黄、绿、蓝、紫、粉、白、黑、棕、灰）\n"
                "2. 不要有任何其他文字、标点或解释\n"
                "3. 如果图片有多种颜色，选择占比最大的那个"
            )
        else:
            return (
                f"请分析这张图片的主要颜色，按占比从高到低列出前{top_n}种颜色。\n"
                "要求：\n"
                "1. 每行一个颜色，格式：颜色名\n"
                "2. 颜色必须是单个中文字：红、橙、黄、绿、蓝、紫、粉、白、黑、棕、灰\n"
                "3. 按占比从高到低排序\n"
                "4. 不要有任何其他文字、标点或解释\n"
                "5. 如果图片颜色单一，只输出一个颜色即可\n"
                "\n"
                "示例输出：\n"
                "绿\n"
                "白"
            )
    
    def _call_vision_api(self, image_base64: str, prompt: str) -> Optional[str]:
        """
        调用视觉模型API
        
        Args:
            image_base64: Base64编码的图片数据
            prompt: 提示词
            
        Returns:
            模型响应文本
        """
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ],
                "max_tokens": 100,
                "temperature": 0.1
            }
            
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                return content.strip()
            else:
                print(f"❌ API请求失败: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            print(f"❌ API调用错误: {e}")
            return None
    
    def _parse_response(self, response_text: str, top_n: int) -> Optional[Dict]:
        """
        解析模型响应
        
        Args:
            response_text: 模型响应文本
            top_n: 期望的颜色数量
            
        Returns:
            解析后的颜色数据
        """
        try:
            # 清理响应文本
            lines = response_text.strip().split('\n')
            
            # 提取颜色（过滤空行和无效内容）
            valid_colors = []
            for line in lines:
                color = line.strip()
                # 只保留单个中文字的颜色
                if len(color) == 1 and color in ['红', '橙', '黄', '绿', '蓝', '紫', '粉', '白', '黑', '棕', '灰']:
                    if color not in valid_colors:  # 去重
                        valid_colors.append(color)
            
            if not valid_colors:
                print(f"⚠️ 无法解析颜色: {response_text}")
                return None
            
            # 确保不超过top_n个颜色
            valid_colors = valid_colors[:top_n]
            
            # 构建返回结果
            main_color = valid_colors[0] if len(valid_colors) > 0 else None
            secondary_color = valid_colors[1] if len(valid_colors) > 1 else None
            
            return {
                'main_color': main_color,
                'secondary_color': secondary_color,
                'colors': valid_colors,
                'rgb_values': [],  # 视觉模型不提供RGB
                'color_ratios': [],  # 视觉模型不提供占比
            }
            
        except Exception as e:
            print(f"❌ 解析响应错误: {e}")
            return None
    
    def batch_extract_colors(self, image_paths: list, top_n: int = 2) -> dict:
        """
        批量提取图片颜色
        
        Args:
            image_paths: 图片路径列表
            top_n: 每个图片返回前 N 种主要颜色
            
        Returns:
            {
                'success_count': int,  # 成功处理的图片数量
                'failed_count': int,   # 失败的图片数量
                'results': dict,       # {image_path: color_data}
            }
        """
        results = {}
        success_count = 0
        failed_count = 0
        
        total = len(image_paths)
        for i, image_path in enumerate(image_paths, 1):
            print(f"[{i}/{total}] 处理: {os.path.basename(image_path)}")
            color_data = self.extract_main_colors(image_path, top_n)
            if color_data:
                results[image_path] = color_data
                success_count += 1
            else:
                failed_count += 1
        
        return {
            'success_count': success_count,
            'failed_count': failed_count,
            'results': results,
        }


# 全局实例（向后兼容）
_extractor = None

def get_extractor(api_key: str = None, base_url: str = None, model: str = None) -> ColorExtractor:
    """获取颜色提取器单例"""
    global _extractor
    if _extractor is None:
        _extractor = ColorExtractor(api_key, base_url, model)
    return _extractor
