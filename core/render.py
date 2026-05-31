"""
HTML → 图片渲染器

基于 Playwright 截图，将 art-template 语法模板转为 Jinja2 后渲染。
复用 endfield 插件的 Renderer 架构，适配洛克王国模板。
"""

import os
import re
import asyncio
import base64
import mimetypes
import uuid
import tempfile
import jinja2
from typing import Dict, Any, Optional
from astrbot.api import logger


class Renderer:
    """洛克王国 HTML→图片 渲染器"""

    _jinja_env: Optional[jinja2.Environment] = None

    @classmethod
    def _get_jinja_env(cls) -> jinja2.Environment:
        if cls._jinja_env is None:
            cls._jinja_env = jinja2.Environment(
                autoescape=True,
                keep_trailing_newline=True,
            )
        return cls._jinja_env

    def __init__(self, res_path: str, render_timeout: int = 30000):
        self.res_path = res_path
        self.render_timeout = render_timeout
        self._browser = None
        self._playwright = None
        self._lock = asyncio.Lock()
        self._cache_cleanup_task: Optional[asyncio.Task] = None
        self._browser_launch_mode: Optional[str] = None
        self._output_dir = os.path.abspath(
            os.path.join(self.res_path, "render_cache")
        )
        os.makedirs(self._output_dir, exist_ok=True)

        self._start_cache_cleanup_task()

    def _start_cache_cleanup_task(self):
        """启动后台清理任务"""
        if self._cache_cleanup_task is None or self._cache_cleanup_task.done():
            self._cache_cleanup_task = asyncio.create_task(
                self._cache_cleanup_loop()
            )

    async def _cache_cleanup_loop(self):
        """后台清理超过 5 分钟的渲染缓存"""
        while True:
            try:
                await asyncio.sleep(60)
                import time as _time
                now = _time.time()
                for f in os.listdir(self._output_dir):
                    if not f.startswith("render_"):
                        continue
                    fp = os.path.join(self._output_dir, f)
                    try:
                        if now - os.path.getmtime(fp) > 300:
                            os.remove(fp)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def get_template(self, name: str) -> str:
        path = os.path.join(self.res_path, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    async def render_html(
        self,
        template_name: str,
        data: Dict[str, Any],
        options: Optional[Dict] = None,
    ) -> Optional[str]:
        """渲染 HTML 模板为图片，返回图片路径"""
        tmpl_content = self.get_template(template_name)
        if not tmpl_content:
            logger.error(f"[Rocom Render] 模板不存在: {template_name}")
            return None

        adapted = self._adapt_template(tmpl_content)
        adapted = self._inline_assets(adapted)
        html_content = self._render_jinja(adapted, data)
        if not html_content:
            return None

        return await self._screenshot(html_content, template_name, options)

    def _adapt_template(self, content: str) -> str:
        """将 art-template 语法转换为 Jinja2"""
        # $index / $value
        adapted = content.replace("$index+1", "loop.index").replace(
            "$index", "loop.index0"
        )
        adapted = adapted.replace("$value", "item")

        def fix_condition(match):
            cond = (
                match.group(1)
                .replace("===", "==")
                .replace("!==", "!=")
                .replace("&&", " and ")
                .replace("||", " or ")
                .replace("null", "none")
                .replace(".length", "|length")
            )
            cond = re.sub(r"!\s*([\w\.]+)", r"not \1", cond)
            return f"{{% if {cond} %}}"

        adapted = re.sub(r"\{\{if\s+(.+?)\}\}", fix_condition, adapted)
        adapted = adapted.replace("{{/if}}", "{% endif %}").replace(
            "{{else}}", "{% else %}"
        )
        adapted = re.sub(
            r"\{\{else if\s+(.+?)\}\}",
            lambda m: fix_condition(m).replace("{% if", "{% elif"),
            adapted,
        )

        def replace_each(match):
            inner = match.group(1).strip().split()
            if len(inner) >= 2:
                return f"{{% for {inner[1]} in {inner[0]} %}}"
            return f"{{% for item in {inner[0]} %}}"

        adapted = re.sub(r"\{\{\s*each\s+(.+?)\s*\}\}", replace_each, adapted)
        adapted = adapted.replace("{{/each}}", "{% endfor %}")

        # Raw interpolation {{@ ... }}
        adapted = re.sub(
            r"\{\{@\s*(.+?)\s*\}\}",
            lambda m: (
                "{{"
                + m.group(1)
                .split("||")[0]
                .replace("&&", " and ")
                .replace("null", "none")
                .replace(".length", "|length")
                + "|safe}}"
            ),
            adapted,
        )

        # Standard interpolation {{ ... }}
        def replace_interpolation(match):
            content = (
                match.group(1)
                .split("||")[0]
                .replace("&&", " and ")
                .replace("null", "none")
                .replace(".length", "|length")
            )
            return "{{" + content + "}}"

        adapted = re.sub(r"\{\{([^%\}]+?)\}\}", replace_interpolation, adapted)
        return adapted

    def _inline_assets(self, html: str) -> str:
        """将 CSS 和图片资源内联到 HTML 中"""

        def inline_css(match):
            path = os.path.join(self.res_path, match.group(1))
            if os.path.exists(path):
                css_content = ""
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        css_content = f.read()
                except UnicodeDecodeError:
                    try:
                        with open(path, "r", encoding="gbk", errors="replace") as f:
                            css_content = f.read()
                    except Exception as e:
                        logger.error(f"[Renderer] 无法读取 CSS 文件 {path}: {e}")
                        return ""
                
                css_content = self._adapt_template(css_content)
                return f"<style>\n{css_content}\n</style>"
            return ""

        def inline_image(match):
            path = os.path.join(self.res_path, match.group(1))
            if os.path.exists(path):
                mime = mimetypes.guess_type(path)[0] or "image/png"
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                    if match.group(0).startswith("src"):
                        return f'src="data:{mime};base64,{b64}"'
                    return f"url(data:{mime};base64,{b64})"
            return match.group(0)

        # Inline <link rel="stylesheet" href="{{_res_path}}...">
        html = re.sub(
            r'<link\s+rel="stylesheet"\s+href="\{\{(?:_res_path|pluResPath)\}\}([^"]+\.css)">',
            inline_css,
            html,
        )
        # Inline src="{{_res_path}}...png"
        html = re.sub(
            r'src="\{\{(?:_res_path|pluResPath)\}\}([^"]+\.(?:png|jpg|jpeg|gif|svg|webp))"',
            inline_image,
            html,
        )
        # Inline url({{_res_path}}...
        html = re.sub(
            r"url\(\s*['\"]?\{\{(?:_res_path|pluResPath)\}\}([^)\"']+?)['\"]?\s*\)",
            inline_image,
            html,
        )
        # Inline style bg url
        def inline_style_bg(m):
            path = os.path.join(self.res_path, m.group(1))
            if os.path.exists(path):
                mime = mimetypes.guess_type(path)[0] or "image/png"
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                return f"url(data:{mime};base64,{b64})"
            return m.group(0)

        html = re.sub(
            r"url\(\{\{(?:pluResPath|_res_path)\}\}([^)]+)\)",
            inline_style_bg,
            html,
        )
        return html

    def _render_jinja(
        self, template_str: str, data: Dict[str, Any]
    ) -> Optional[str]:
        """使用 Jinja2 渲染模板"""
        try:
            env = self._get_jinja_env()
            data_copy = data.copy()
            data_copy["_res_path"] = data_copy.get("pluResPath", "X")
            return env.from_string(template_str).render(**data_copy)
        except Exception as e:
            logger.error(f"[Rocom Render] Jinja2 渲染错误: {e}")
            return None

    async def _screenshot(
        self, html: str, name: str, options: Optional[Dict]
    ) -> Optional[str]:
        """Playwright 截图"""
        from playwright.async_api import async_playwright

        output_path = os.path.join(
            self._output_dir, f"render_{uuid.uuid4().hex[:8]}.png"
        )

        try:
            async with self._lock:
                # 确保 playwright 和 browser 可用，处理 stale 实例
                try:
                    if not self._playwright:
                        self._playwright = await async_playwright().start()
                    if not self._browser or not self._browser.is_connected():
                        self._browser = await self._playwright.chromium.launch()
                except Exception:
                    # playwright 或 browser 实例已损坏，重建
                    try:
                        if self._browser:
                            await self._browser.close()
                    except Exception:
                        pass
                    try:
                        if self._playwright:
                            await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch()

            options = options or {}
            device_scale_factor = float(options.get("device_scale_factor", 2.0))
            viewport_width = int(options.get("viewport_width", 1400))
            viewport_height = int(options.get("viewport_height", 900))

            context = await self._browser.new_context(
                device_scale_factor=device_scale_factor,
                viewport={"width": viewport_width, "height": viewport_height},
            )
            page = await context.new_page()

            temp_html = os.path.join(
                os.path.dirname(
                    os.path.abspath(os.path.join(self.res_path, name))
                ),
                f"tmp_{uuid.uuid4().hex[:8]}.html",
            )
            with open(temp_html, "w", encoding="utf-8") as f:
                f.write(html)

            try:
                await page.goto(
                    f"file:///{temp_html.replace(chr(92), '/')}",
                    wait_until="networkidle",
                    timeout=self.render_timeout,
                )
            except Exception:
                pass  # 部分外部资源超时无妨

            # 等待图片加载
            await page.evaluate(
                """
                Promise.all(Array.from(document.images).map(img => {
                    if (img.complete) return Promise.resolve();
                    return new Promise(resolve => {
                        img.onload = resolve;
                        img.onerror = resolve;
                    });
                }))
            """
            )
            await page.wait_for_timeout(500)

            # 优先查找第一个非 body 的块级元素，避免截取到多余的空白
            el = await page.evaluate_handle("""
                () => {
                    // 尝试查找常见的内容容器
                    const selectors = [
                        '.exchange-page',
                        '.record-page', 
                        '.package-cont',
                        '.searcheggs-cont',
                        '.bwiki-shell',
                        '.skill-shell',
                        '.page-section-main',
                        '.lineup-page',
                        '.inspect-page',
                        '.player-search-page',
                        '.ingame-shop-page',
                        '.friendship-page',
                        '.student-state-page',
                        '.student-perks-page',
                        '.student-page',
                        '.announcement-list-page',
                        '.announcement-detail-page',
                        '.activity-calendar-page'
                    ];
                    for (const selector of selectors) {
                        const element = document.querySelector(selector);
                        if (element) return element;
                    }
                    // 如果都没找到，使用 body 的第一个子元素
                    return document.body.firstElementChild || document.body;
                }
            """)
            box = await el.bounding_box() if el else None
            if box and el:
                await page.set_viewport_size(
                    {
                        "width": max(int(box["width"]) + 8, 200),
                        "height": max(int(box["height"]) + 8, 200),
                    }
                )
                await page.wait_for_timeout(100)
                await el.screenshot(path=output_path, type="png")
            else:
                await page.screenshot(path=output_path, full_page=True)

            if el:
                await el.dispose()

            if os.path.exists(temp_html):
                os.remove(temp_html)
            await page.close()
            await context.close()
            return output_path

        except Exception as e:
            logger.error(f"[Rocom Render] Playwright 渲染错误: {e}")
            return None

    async def close(self):
        if self._cache_cleanup_task and not self._cache_cleanup_task.done():
            self._cache_cleanup_task.cancel()
            self._cache_cleanup_task = None
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
