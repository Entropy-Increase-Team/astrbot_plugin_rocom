import os
import shutil
import urllib.request
from typing import Dict, List

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


GITHUB_FONT_COMMIT = "ede870913af3e19270dbe6aabe60ee7e058f68a2"
GITCODE_RAW_BASE = "https://api.gitcode.com/api/v5/repos/edvffsb/astrbot_plugin_rocom/raw"
GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/Entropy-Increase-Team/"
    f"astrbot_plugin_rocom/{GITHUB_FONT_COMMIT}/ttf"
)

FONT_FILES: Dict[str, List[str]] = {
    "HYWenHei-85W-1.ttf": [
        f"{GITCODE_RAW_BASE}/HYWenHei-85W-1.ttf?ref=main",
        f"{GITHUB_RAW_BASE}/HYWenHei-85W-1.ttf",
    ],
    "fzlant.D5FI9Et0.ttf": [
        f"{GITCODE_RAW_BASE}/fzlant.D5FI9Et0.ttf?ref=main",
        f"{GITHUB_RAW_BASE}/fzlant.D5FI9Et0.ttf",
    ],
    "dundun.xHd_Ee5-.woff2": [
        f"{GITCODE_RAW_BASE}/dundun.xHd_Ee5-.woff2?ref=main",
        f"{GITHUB_RAW_BASE}/dundun.xHd_Ee5-.woff2",
    ],
}


class FontAssetManager:
    def __init__(self, res_path: str, data_dir: str, timeout: int = 20):
        self.res_path = os.path.abspath(res_path)
        self.cache_dir = os.path.abspath(os.path.join(data_dir, "rocom_fonts"))
        self.timeout = timeout

    def ensure_fonts(self) -> Dict[str, str]:
        os.makedirs(self.cache_dir, exist_ok=True)
        resolved: Dict[str, str] = {}

        for filename, urls in FONT_FILES.items():
            cache_path = os.path.join(self.cache_dir, filename)

            if self._valid_file(cache_path):
                resolved[filename] = cache_path
                continue

            if self._download_first_available(filename, urls, cache_path):
                resolved[filename] = cache_path
                continue

            logger.warning(f"[Rocom Fonts] {filename} 不可用，将使用系统字体兜底")

        return resolved

    @staticmethod
    def _valid_file(path: str) -> bool:
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            return False
        try:
            with open(path, "rb") as f:
                head = f.read(4)
            return head in (b"\x00\x01\x00\x00", b"OTTO", b"wOFF", b"wOF2")
        except Exception:
            return False

    def _download_first_available(self, filename: str, urls: List[str], dst: str) -> bool:
        for url in urls:
            if self._download(url, dst):
                return True
            logger.warning(f"[Rocom Fonts] 字体源不可用 {filename}: {url}")
        return False

    def _download(self, url: str, dst: str) -> bool:
        tmp = f"{dst}.tmp"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(response, f)
            if not self._valid_file(tmp):
                raise RuntimeError("downloaded file is not a valid font")
            os.replace(tmp, dst)
            logger.info(f"[Rocom Fonts] 已下载字体 {os.path.basename(dst)}")
            return True
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            logger.warning(f"[Rocom Fonts] 下载字体失败 {os.path.basename(dst)}: {e}")
            return False
