"""
爬虫配置文件
包含基础URL、请求头生成等配置
"""
import time
import requests
from fake_useragent import UserAgent

# 批处理配置
BATCH_SIZE = 20
BATCH_DELAY = 2  # 秒
MAX_DURATION = 10 * 60  # 10分钟（秒）

# API配置
BASE_URL = "https://wiki.biligame.com/rocom"
API_URL = f"{BASE_URL}/api.php"

# User-Agent生成器
ua = UserAgent()


def make_headers():
    """生成随机请求头"""
    return {
        "User-Agent": ua.random,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"{BASE_URL}/",
    }


def fetch_with_retry(url, params=None, retries=3, timeout=10):
    """
    带重试机制的HTTP请求
    
    Args:
        url: 请求URL
        params: 查询参数
        retries: 重试次数
        timeout: 超时时间（秒）
    
    Returns:
        requests.Response对象，失败返回None
    """
    for i in range(retries):
        try:
            response = requests.get(
                url, 
                params=params, 
                headers=make_headers(),
                timeout=timeout
            )
            
            # 567状态码表示需要重试
            if response.status_code == 567:
                wait_time = 3 * (i + 1)
                print(f"  收到567状态码，{wait_time}秒后重试...")
                time.sleep(wait_time)
                continue
            
            response.raise_for_status()
            return response
            
        except requests.exceptions.RequestException as e:
            print(f"  请求失败: {e}")
            if i < retries - 1:
                wait_time = 3 * (i + 1)
                time.sleep(wait_time)
            else:
                return None
    
    return None
