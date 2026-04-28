import os
import time
import base64
import tempfile
import asyncio
import re
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, Image

from .core.client import RocomClient
from .core.user import UserManager, MerchantSubscriptionManager
from .core.render import Renderer
from .core.egg_service import EggService, SearchResult

# Wiki功能导入
try:
    from .wiki.src.db_service import WikiDBService
    from .wiki.src.color_extractor_vision import ColorExtractor
except ImportError as e:
    logger.warning(f"⚠️ Wiki模块导入失败: {e}")
    WikiDBService = None
    ColorExtractor = None

@register("astrbot_plugin_rocom", "bvzrays & 熵增项目组", "洛克王国插件", "v2.7.0", "https://github.com/Entropy-Increase-Team/astrbot_plugin_rocom")
class RocomPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        base_url = self.config.get("api_base_url", "https://wegame.shallow.ink")
        wegame_api_key = self.config.get("wegame_api_key", "")
        
        self.client = RocomClient(
            base_url=base_url,
            wegame_api_key=wegame_api_key,
        )
        
        data_dir = str(StarTools.get_data_dir())
        self.user_mgr = UserManager(data_dir)
        self.merchant_sub_mgr = MerchantSubscriptionManager(data_dir)
        
        render_timeout = self.config.get("render_timeout", 30000)
        self.help_prefix_display = str(self.config.get("help_prefix_display", "") or "")
        # res_path point to astrbot_plugin_rocom directory
        res_path = os.path.abspath(os.path.dirname(__file__))
        self.renderer = Renderer(res_path=res_path, render_timeout=render_timeout)
        
        # 自动刷新配置
        self.auto_refresh_enabled = self.config.get("auto_refresh_enabled", False)
        self.auto_refresh_time = self.config.get("auto_refresh_time", ["00:00", "12:00"])
        self.auto_refresh_notify_group = self.config.get("auto_refresh_notify_group", "")
        self._auto_refresh_task = None
        
        # 初始化查蛋模块（数据自包含在 render/searcheggs/ 下）
        searcheggs_dir = os.path.join(res_path, "render", "searcheggs")
        self.egg_searcher = EggService(searcheggs_dir)
        self.merchant_subscription_enabled = self.config.get(
            "merchant_subscription_enabled", True
        )
        self.merchant_subscription_items = self.config.get(
            "merchant_subscription_items", ["国王球", "棱镜球", "炫彩精灵蛋"]
        )
        self.merchant_private_subscription_enabled = self.config.get(
            "merchant_private_subscription_enabled", True
        )
        self._merchant_subscription_task = None
        self._merchant_retry_delay_seconds = 240
        self._merchant_retry_times = 3
        
        # 启动时检查是否需要开启自动刷新
        logger.info(f"[Rocom] 插件初始化完成，自动刷新启用状态：{self.auto_refresh_enabled}, 刷新时间：{self.auto_refresh_time}, 通知群：{self.auto_refresh_notify_group}")
        if self.auto_refresh_enabled:
            self._auto_refresh_task = asyncio.create_task(self._auto_refresh_loop())
            logger.info("[Rocom] 自动刷新任务已启动")
        else:
            logger.info("[Rocom] 自动刷新功能未启用")
        
        if self.merchant_subscription_enabled:
            self._merchant_subscription_task = asyncio.create_task(
                self._merchant_subscription_loop()
            )
        
        # 初始化Wiki数据库服务
        db_path = self.config.get("wiki_db_path", "./wiki/wiki-local.db")
        try:
            self.db_service = WikiDBService(db_path) if WikiDBService else None
            logger.info(f"✅ Wiki数据库服务初始化成功: {db_path}")
        except Exception as e:
            logger.error(f"❌ Wiki数据库服务初始化失败: {e}")
            self.db_service = None
        
        # 初始化颜色提取器
        self.color_extractor = None
        if ColorExtractor:
            try:
                self.color_extractor = self._init_wiki_color_extractor()
            except Exception as e:
                logger.error(f"❌ Wiki颜色提取器初始化失败: {e}")

    def _init_wiki_color_extractor(self):
        """
        初始化Wiki颜色提取器（从 AstrBot provider 配置中获取或手动填写）
        
        Returns:
            ColorExtractor 实例或 None
        """
        # 检查是否使用手动配置
        manual_api_key = self.config.get("wiki_manual_vision_api_key", "").strip()
        manual_base_url = self.config.get("wiki_manual_vision_base_url", "").strip()
        manual_model_id = self.config.get("wiki_manual_vision_model_id", "").strip()
        
        # 如果手动配置完整，直接使用
        if manual_api_key and manual_base_url and manual_model_id:
            logger.info("✅ Wiki使用手动配置的视觉模型")
            logger.info(f"   - base_url: {manual_base_url}")
            logger.info(f"   - model: {manual_model_id}")
            return ColorExtractor(self.context, api_key=manual_api_key, base_url=manual_base_url, model_id=manual_model_id)
        
        # 如果没有手动配置，尝试从 AstrBot provider 配置中获取
        vision_model_config = self.config.get("wiki_vision_model_config", "")
        
        if not vision_model_config or not vision_model_config.strip():
            logger.warning("⚠️ Wiki未配置视觉模型，颜色识别功能不可用")
            return None
        
        try:
            provider_manager = getattr(self.context, 'provider_manager', None)
            if not provider_manager:
                logger.error("❌ Wiki无法访问 provider_manager")
                return None
            
            providers = getattr(provider_manager, 'get_insts', lambda: [])()
            
            selected_provider = None
            for provider in providers:
                provider_id = (getattr(provider, 'id', None) or 
                              getattr(provider, 'provider_id', None) or 
                              getattr(provider, 'name', None))
                
                if provider_id == vision_model_config:
                    selected_provider = provider
                    break
            
            if not selected_provider:
                logger.error(f"❌ Wiki未找到 provider '{vision_model_config}'")
                return None
            
            return ColorExtractor(self.context, provider=selected_provider)
        except Exception as e:
            logger.error(f"❌ Wiki颜色提取器初始化异常: {e}")
            return None

    async def terminate(self):
        # 防御性检查：确保属性存在（处理 __init__ 中途失败的情况）
        if hasattr(self, '_merchant_subscription_task') and self._merchant_subscription_task and not self._merchant_subscription_task.done():
            self._merchant_subscription_task.cancel()
            try:
                await self._merchant_subscription_task
            except asyncio.CancelledError:
                pass
        if hasattr(self, '_auto_refresh_task') and self._auto_refresh_task and not self._auto_refresh_task.done():
            self._auto_refresh_task.cancel()
            try:
                await self._auto_refresh_task
            except asyncio.CancelledError:
                pass
        if hasattr(self, 'client'):
            await self.client.close()
        if hasattr(self, 'renderer'):
            await self.renderer.close()

    async def _send_and_get_msg_id(self, event: AstrMessageEvent, obmsg: list):
        """发送消息并获取 ID 以支持撤回"""
        try:
            if event.get_platform_name() == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    group_id = event.get_group_id()
                    if group_id:
                        res = await client.send_group_msg(group_id=int(group_id), message=obmsg)
                    else:
                        res = await client.send_private_msg(user_id=int(event.get_sender_id()), message=obmsg)
                    if res:
                        return client, int(res.get("message_id"))
        except Exception as e:
            logger.warning(f"获取消息 ID 失败: {e}")
        return None, None

    def _schedule_recall(self, client, message_id: int, delay: float):
        async def _do_recall():
            await asyncio.sleep(delay)
            try:
                await client.delete_msg(message_id=message_id)
            except Exception:
                pass
        return asyncio.create_task(_do_recall())

    async def _get_primary_token(self, event: AstrMessageEvent) -> str:
        user_id = event.get_sender_id()
        logger.debug(f"[Rocom] 获取主账号 Token，user_id: {user_id}")
        binding = await self.user_mgr.get_primary_binding(user_id)
        if not binding:
            logger.warning(f"[Rocom] 用户 {user_id} 未绑定账号")
            return ""
        
        fw_token = binding.get("framework_token", "")
        logger.debug(f"[Rocom] 用户 {user_id} 的主账号 Token: {fw_token[:8]}...")
        return fw_token

    async def _auto_refresh_loop(self):
        """自动刷新循环任务（非必要不要使用）"""
        logger.info("[自动刷新] 任务已启动")
        
        # 记录上次刷新的时间点，避免同一分钟内重复刷新
        last_refresh_minute = None
        
        while True:
            try:
                now = datetime.now()
                current_time = f"{now.hour:02d}:{now.minute:02d}"
                current_minute_ts = int(now.timestamp()) // 60  # 当前分钟的 timestamp
                
                # 调试：每分钟记录一次当前时间和配置时间
                logger.debug(f"[自动刷新] 当前时间：{current_time}, 配置的刷新时间：{self.auto_refresh_time}, 类型：{type(self.auto_refresh_time)}")
                
                # 检查是否到达刷新时间
                # 确保 auto_refresh_time 是列表
                refresh_times = self.auto_refresh_time if isinstance(self.auto_refresh_time, list) else [self.auto_refresh_time]
                
                # 如果当前时间在刷新时间列表中，并且这一分钟内还没有刷新过
                if current_time in refresh_times and last_refresh_minute != current_minute_ts:
                    logger.info(f"[自动刷新] 检测到刷新时间 {current_time}，开始执行...")
                    await self._do_auto_refresh()
                    last_refresh_minute = current_minute_ts
                    logger.info(f"[自动刷新] 刷新任务完成，下次刷新时间：{refresh_times}")
                
                # 每分钟检查一次
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                logger.info("[自动刷新] 任务已取消")
                break
            except Exception as e:
                logger.error(f"[自动刷新] 任务异常：{e}")
                await asyncio.sleep(60)

    async def _do_auto_refresh(self):
        """执行自动刷新"""
        all_users_data = await self.user_mgr.get_all_users_bindings()
        
        total_users = len(all_users_data)
        success_count = 0
        fail_count = 0
        results = []
        
        for user_id, bindings in all_users_data.items():
            if not bindings:
                continue
            
            for binding in bindings:
                binding_id = binding.get("binding_id", "")
                if not binding_id:
                    continue
                
                # 只刷新 QQ 登录的凭证（只有 QQ 扫码支持刷新）
                if binding.get("login_type") != "qq":
                    continue
                
                try:
                    res = await self.client.refresh_binding(binding_id, user_id)
                    if res and res.get("framework_token"):
                        new_token = res["framework_token"]
                        binding["framework_token"] = new_token
                        
                        # 更新本地存储
                        user_bindings = await self.user_mgr.get_user_bindings(user_id)
                        for i, b in enumerate(user_bindings):
                            if b.get("binding_id") == binding_id:
                                user_bindings[i] = binding
                                break
                        await self.user_mgr.save_user_bindings(user_id, user_bindings)
                        
                        success_count += 1
                        results.append(f"✅ 用户 {user_id} ({binding.get('nickname', '未知')}) 刷新成功")
                        logger.info(f"[自动刷新] 用户 {user_id} 凭证刷新成功")
                    else:
                        fail_count += 1
                        results.append(f"❌ 用户 {user_id} ({binding.get('nickname', '未知')}) 刷新失败")
                        logger.warning(f"[自动刷新] 用户 {user_id} 凭证刷新失败")
                except Exception as e:
                    fail_count += 1
                    results.append(f"❌ 用户 {user_id} ({binding.get('nickname', '未知')}) 异常：{e}")
                    logger.error(f"[自动刷新] 用户 {user_id} 凭证刷新异常：{e}")
        
        # 发送通知
        msg = f"【自动刷新结果】\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        msg += f"总用户数：{total_users}\n"
        msg += f"成功：{success_count} | 失败：{fail_count}\n\n"
        if results:
            msg += "\n".join(results[:10])  # 最多显示 10 条
            if len(results) > 10:
                msg += f"\n... 还有 {len(results) - 10} 条结果"
        
        # 发送到指定群
        if self.auto_refresh_notify_group and success_count > 0 or fail_count > 0:
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                # 创建一个假 event 用于发送消息
                await self._send_notify_to_group(msg)
            except Exception as e:
                logger.error(f"[自动刷新] 发送通知失败：{e}")
        
        logger.info(f"[自动刷新] 执行完成：成功{success_count}，失败{fail_count}")

    @filter.command("洛克刷新所有凭证")
    async def rocom_refresh_all(self, event: AstrMessageEvent):
        """刷新所有用户的凭证（需要 bot 管理员权限，同时非必要不要使用）"""
        # 检查 bot 管理员权限
        if not event.is_admin():
            uid = str(event.get_sender_id())
            allowed = [u.strip() for u in self.config.get("allowed_users", "").split(",") if u.strip()]
            if uid not in allowed:
                yield event.plain_result("⚠️ 此指令仅限 bot 管理员使用。")
                return

        yield event.plain_result("⚠️ 非必要不要手动刷新凭证，服务端会自动刷新。本指令仅用于调试或强制兜底。\n\n正在刷新所有用户的凭证...")

        all_users_data = await self.user_mgr.get_all_users_bindings()
        
        total_users = len(all_users_data)
        success_count = 0
        fail_count = 0
        skipped_count = 0
        results = []
        
        for user_id, bindings in all_users_data.items():
            if not bindings:
                continue
            
            for binding in bindings:
                binding_id = binding.get("binding_id", "")
                if not binding_id:
                    continue
                
                # 只刷新 QQ 登录的凭证（只有 QQ 扫码支持刷新）
                login_type = binding.get("login_type", "")
                if login_type != "qq":
                    skipped_count += 1
                    continue
                
                try:
                    res = await self.client.refresh_binding(binding_id, user_id)
                    if res and res.get("framework_token"):
                        new_token = res["framework_token"]
                        binding["framework_token"] = new_token
                        
                        # 更新本地存储
                        user_bindings = await self.user_mgr.get_user_bindings(user_id)
                        for i, b in enumerate(user_bindings):
                            if b.get("binding_id") == binding_id:
                                user_bindings[i] = binding
                                break
                        await self.user_mgr.save_user_bindings(user_id, user_bindings)
                        
                        success_count += 1
                        results.append(f"✅ 用户 {user_id} ({binding.get('nickname', '未知')}) 刷新成功")
                        logger.info(f"[手动刷新所有] 用户 {user_id} 凭证刷新成功")
                    else:
                        fail_count += 1
                        results.append(f"❌ 用户 {user_id} ({binding.get('nickname', '未知')}) 刷新失败")
                        logger.warning(f"[手动刷新所有] 用户 {user_id} 凭证刷新失败")
                except Exception as e:
                    fail_count += 1
                    results.append(f"❌ 用户 {user_id} ({binding.get('nickname', '未知')}) 异常：{e}")
                    logger.error(f"[手动刷新所有] 用户 {user_id} 凭证刷新异常：{e}")
        
        msg = f"【刷新所有凭证完成】\n"
        msg += f"总用户数：{total_users}\n"
        msg += f"成功：{success_count} | 失败：{fail_count} | 跳过（非 QQ）: {skipped_count}\n\n"
        if results:
            msg += "\n".join(results[:20])  # 最多显示 20 条
            if len(results) > 20:
                msg += f"\n... 还有 {len(results) - 20} 条结果"
        
        yield event.plain_result(msg)

    async def _send_notify_to_group(self, message: str):
        """发送通知到指定群"""
        try:
            if self.auto_refresh_notify_group:
                session_id = self.auto_refresh_notify_group.strip()
                # 创建 MessageChain 对象
                chain = MessageChain()
                chain.chain.append(Plain(message))
                # 直接使用用户填写的完整 UMO
                await self.context.send_message(
                    session_id,
                    chain
                )
                logger.info(f"[自动刷新] 通知已发送到 {session_id}")
        except Exception as e:
            logger.error(f"[自动刷新] 发送群消息失败：{e}")

    def _merchant_check_times(self, base: datetime | None = None) -> List[datetime]:
        now = base or datetime.now(self._cn_tz())
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._cn_tz())
        return [
            now.replace(hour=8, minute=1, second=0, microsecond=0),
            now.replace(hour=12, minute=1, second=0, microsecond=0),
            now.replace(hour=16, minute=1, second=0, microsecond=0),
            now.replace(hour=20, minute=1, second=0, microsecond=0),
        ]

    def _next_merchant_check_time(self, now: datetime | None = None) -> datetime:
        current = now or datetime.now(self._cn_tz())
        if current.tzinfo is None:
            current = current.replace(tzinfo=self._cn_tz())
        for check_time in self._merchant_check_times(current):
            if check_time > current:
                return check_time
        next_day = current + timedelta(days=1)
        return self._merchant_check_times(next_day)[0]

    async def _merchant_subscription_loop(self):
        logger.info("[Rocom] 远行商人订阅循环任务已启动")
        while True:
            try:
                now = datetime.now(self._cn_tz())
                next_check = self._next_merchant_check_time(now)
                sleep_seconds = max(1, (next_check - now).total_seconds())
                logger.info(
                    f"[Rocom] 下次远行商人订阅检查时间：{next_check.strftime('%Y-%m-%d %H:%M:%S CST')}"
                )
                await asyncio.sleep(sleep_seconds)
                await self._run_merchant_subscription_window()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Rocom] 远行商人订阅循环异常: {e}")
                await asyncio.sleep(60)

    def _cn_tz(self):
        return timezone(timedelta(hours=8))

    def _current_merchant_round(self, now: datetime | None = None):
        now = now or datetime.now(self._cn_tz())
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._cn_tz())
        start = now.replace(hour=8, minute=0, second=0, microsecond=0)
        round_index = None
        round_start = None
        round_end = None
        if start <= now < start + timedelta(hours=16):
            delta_seconds = int((now - start).total_seconds())
            round_index = delta_seconds // int(timedelta(hours=4).total_seconds()) + 1
            round_start = start + timedelta(hours=4 * (round_index - 1))
            round_end = round_start + timedelta(hours=4)
        return {
            "date": now.strftime("%Y-%m-%d"),
            "current": round_index,
            "total": 4,
            "round_id": f"{now.strftime('%Y-%m-%d')}-{round_index}" if round_index else f"{now.strftime('%Y-%m-%d')}-closed",
            "is_open": round_index is not None,
            "countdown": self._format_countdown(round_end - now) if round_end else "未开市",
            "start_time": round_start,
            "end_time": round_end,
        }

    def _format_countdown(self, delta: timedelta | None):
        if not delta:
            return "--"
        total = max(0, int(delta.total_seconds()))
        hours, remainder = divmod(total, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0 and minutes > 0:
            return f"{hours}小时{minutes}分钟"
        if hours > 0:
            return f"{hours}小时"
        return f"{minutes}分钟"

    def _format_merchant_time(self, timestamp_ms: Any) -> str:
        try:
            dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=self._cn_tz())
            return dt.strftime("%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return "--"

    def _format_merchant_window(self, item: Dict[str, Any]) -> str:
        start_time = item.get("start_time")
        end_time = item.get("end_time")
        if start_time is None or end_time is None:
            return "褰撳墠杞"
        start_label = self._format_merchant_time(start_time)
        end_label = self._format_merchant_time(end_time)
        if start_label == "--" or end_label == "--":
            return "褰撳墠杞"
        if start_label[:5] == end_label[:5]:
            return f"{start_label} - {end_label[6:]}"
        return f"{start_label} - {end_label}"

    async def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat():
            return False
        sender_id = str(event.get_sender_id())
        role = str(getattr(event, "role", "") or "").lower()
        try:
            group = await event.get_group()
            if group:
                owner_candidates = [
                    getattr(group, "group_owner", None),
                    getattr(group, "owner_id", None),
                    getattr(group, "group_owner_id", None),
                ]
                if any(str(owner) == sender_id for owner in owner_candidates if owner is not None):
                    return True

                admins = [str(x) for x in getattr(group, "group_admins", [])]
                if sender_id in admins:
                    return True

                # 允许 bot 管理员通过；群信息优先，事件角色作为补充
                if role in {"admin", "owner"}:
                    return True
        except Exception:
            if role in {"admin", "owner"}:
                return True
        return False


    def _merchant_products_from_response(self, res: Dict[str, Any] | None):
        payload = res or {}
        activities = payload.get("merchantActivities")
        if activities is None:
            activities = payload.get("merchant_activities")
        activities = activities or []
        activity = activities[0] if activities else {}
        props = activity.get("get_props") or []
        pets = activity.get("get_pets") or []
        products = []
        fallback_icon = "{{_res_path}}img/logo.cVSpb3sL.png"
        now_ms = int(datetime.now(self._cn_tz()).timestamp() * 1000)

        def is_active(item: Dict[str, Any]) -> bool:
            start_time = item.get("start_time")
            end_time = item.get("end_time")
            if start_time is None or end_time is None:
                return True
            try:
                return int(start_time) <= now_ms < int(end_time)
            except (TypeError, ValueError):
                return True

        for item in props:
            if not is_active(item):
                continue
            products.append(
                {
                    "name": item.get("name", "未知商品"),
                    "image": item.get("icon_url") or fallback_icon,
                    "time_label": self._format_merchant_window(item),
                }
            )
        for item in pets:
            if not is_active(item):
                continue
            products.append(
                {
                    "name": item.get("name", "未知精灵"),
                    "image": item.get("icon_url") or fallback_icon,
                    "time_label": self._format_merchant_window(item),
                }
            )
        return activity, products


    async def _render_merchant_image(self, refresh: bool = False):
        res = await self.client.get_merchant_info(refresh=refresh)
        activity, products = self._merchant_products_from_response(res)
        round_info = self._current_merchant_round()
        return await self._render_merchant_image_from_data(activity, products, round_info), res, products, round_info

    async def _render_merchant_image_from_data(
        self,
        activity: Dict[str, Any] | None,
        products: List[Dict[str, Any]] | None,
        round_info: Dict[str, Any] | None,
    ):
        data = {
            "background": "{{_res_path}}img/bg.C8CUoi7I.jpg",
            "titleIcon": True,
            "title": (activity or {}).get("name", "远行商人"),
            "subtitle": (activity or {}).get("start_date", "每日 08:00 / 12:00 / 16:00 / 20:00 刷新"),
            "product_count": len(products or []),
            "round_info": round_info or self._current_merchant_round(),
            "products": products or [],
        }
        img_url = await self.renderer.render_html(
            "render/yuanxing-shangren/index.html",
            data,
            {
                "device_scale_factor": 3,
                "viewport_width": 1600,
                "viewport_height": 1200,
            },
        )
        return img_url

    async def _run_merchant_subscription_window(self):
        for retry_index in range(self._merchant_retry_times + 1):
            status = await self._check_merchant_subscriptions()
            if status != "empty":
                return
            if retry_index >= self._merchant_retry_times:
                logger.warning("[Rocom] 远行商人订阅检查连续为空，已暂停本轮重试")
                return
            logger.warning(
                f"[Rocom] 远行商人返回为空，{self._merchant_retry_delay_seconds // 60} 分钟后进行第 {retry_index + 1} 次重试"
            )
            await asyncio.sleep(self._merchant_retry_delay_seconds)

    async def _check_merchant_subscriptions(self) -> str:
        all_subs = await self.merchant_sub_mgr.get_all_subscriptions()
        if not all_subs:
            return "no_subscriptions"
        try:
            res = await self.client.get_merchant_info(refresh=True)
            activity, products = self._merchant_products_from_response(res)
        except Exception as e:
            logger.warning(f"[Rocom] 远行商人订阅查询失败，视为空结果等待重试: {e}")
            return "empty"
        round_info = self._current_merchant_round()
        if not round_info["is_open"]:
            return "closed"
        if not products:
            return "empty"
        product_names = {p.get("name", "") for p in products}
        pending_pushes = []
        for key, sub in all_subs.items():
            items = sub.get("items") or self.merchant_subscription_items
            matched = [name for name in items if name in product_names]
            if not matched or sub.get("last_push_round") == round_info["round_id"]:
                continue
            pending_pushes.append((key, sub, matched))
        if not pending_pushes:
            return "done"
        img_url = None
        try:
            img_url = await self._render_merchant_image_from_data(activity, products, round_info)
        except Exception as e:
            logger.warning(f"[Rocom] 远行商人订阅图片预渲染失败，将仅发送文本: {e}")
        for key, sub, matched in pending_pushes:
            text_chain = MessageChain()
            if sub.get("mention_all"):
                text_chain.at_all()
            text_chain.message(
                f"远行商人本轮命中订阅商品：{'、'.join(matched)}\n轮次：第{round_info['current']}轮\n剩余：{round_info['countdown']}"
            )
            try:
                await self.context.send_message(sub["umo"], text_chain)
            except Exception as e:
                logger.warning(f"[Rocom] 远行商人订阅文本推送失败: {e}")
                fallback = MessageChain().message(
                    f"远行商人本轮命中订阅商品：{'、'.join(matched)}"
                )
                try:
                    await self.context.send_message(sub["umo"], fallback)
                except Exception as fallback_e:
                    logger.warning(f"[Rocom] 远行商人订阅降级文本推送失败: {fallback_e}")
                    continue
            if img_url:
                try:
                    image_chain = MessageChain().file_image(img_url)
                    await self.context.send_message(sub["umo"], image_chain)
                except Exception as image_e:
                    logger.warning(f"[Rocom] 远行商人订阅图片推送失败: {image_e}")
            sub["last_push_round"] = round_info["round_id"]
            sub["last_matched_items"] = matched
            await self.merchant_sub_mgr.upsert_subscription(key, sub)
            await asyncio.sleep(5)
        return "done"

    def _split_merchant_subscription_items(self, raw_text: str) -> List[str]:
        parts = re.split(r"[\s,，、/|；;]+", raw_text.strip())
        items = []
        seen = set()
        for part in parts:
            name = str(part or "").strip()
            if not name or name in seen:
                continue
            items.append(name)
            seen.add(name)
        return items

    def _parse_merchant_subscription_args(self, raw_text: str) -> tuple[bool, List[str] | None]:
        """解析远行商人订阅参数
        返回：(是否@全体，自定义商品列表)
        商品列表为 None 表示使用默认配置
        """
        text = str(raw_text or "").strip()
        if not text:
            return False, None
        tokens = text.split(maxsplit=1)
        mention = False
        items_text = text
        if tokens and tokens[0] in {"0", "1"}:
            mention = tokens[0] == "1"
            items_text = tokens[1] if len(tokens) > 1 else ""
        items = self._split_merchant_subscription_items(items_text) if items_text.strip() else None
        # 只有当 items 非空时才返回，否则返回 None 表示使用默认配置
        return mention, items if items else None

    def _wiki_asset_id(self, number: Any) -> int | None:
        try:
            numeric_id = int(number)
        except (TypeError, ValueError):
            return None
        return numeric_id if numeric_id >= 3000 else numeric_id + 3000

    def _wiki_pet_icon(self, item: Dict[str, Any]) -> str:
        icon_url = item.get("icon_url") or item.get("pet_icon") or item.get("petIcon")
        if icon_url:
            return icon_url
        asset_id = self._wiki_asset_id(item.get("no") or item.get("pet_id"))
        if asset_id is None:
            return "{{_res_path}}img/roco_icon.png"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{asset_id}/icon.png"

    def _wiki_pet_image(self, item: Dict[str, Any]) -> str:
        image_url = item.get("image_url") or item.get("pet_image") or item.get("petImage")
        if image_url:
            return image_url
        asset_id = self._wiki_asset_id(item.get("no") or item.get("pet_id"))
        if asset_id is None:
            return "{{_res_path}}img/roco_icon.png"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{asset_id}/image.png"

    def _normalize_wiki_type_values(self, values: Any) -> List[str]:
        normalized = []
        for value in values or []:
            if isinstance(value, dict):
                text = value.get("name") or value.get("label") or value.get("value")
            else:
                text = value
            if text:
                normalized.append(str(text))
        return normalized

    def _build_wiki_evolution_data(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_chain = (
            item.get("evolution_chain")
            or item.get("evolutionChain")
            or item.get("evolutions")
            or item.get("evolution")
            or []
        )
        chain = []
        for evo in raw_chain:
            evo_name = evo.get("name") or evo.get("pet_name") or "未知形态"
            evo_number = evo.get("no") or evo.get("pet_id") or item.get("no")
            evo_asset_id = self._wiki_asset_id(evo_number)
            evo_image = (
                evo.get("image")
                or evo.get("image_url")
                or evo.get("petImage")
                or (
                    f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{evo_asset_id}/image.png"
                    if evo_asset_id is not None
                    else self._wiki_pet_image(item)
                )
            )
            evo_icon = (
                evo.get("icon")
                or evo.get("icon_url")
                or evo.get("petIcon")
                or (
                    f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{evo_asset_id}/icon.png"
                    if evo_asset_id is not None
                    else self._wiki_pet_icon(item)
                )
            )
            chain.append(
                {
                    "name": evo_name,
                    "number": evo_number or "?",
                    "image": evo_image,
                    "icon": evo_icon,
                    "condition": evo.get("condition") or evo.get("how") or evo.get("requirement") or "",
                    "is_current": bool(
                        evo.get("is_current")
                        or evo_name == item.get("name")
                        or evo_number == item.get("no")
                    ),
                }
            )
        if chain:
            return chain
        return [
            {
                "name": item.get("name", "未知精灵"),
                "number": item.get("no", "?"),
                "image": self._wiki_pet_image(item),
                "icon": self._wiki_pet_icon(item),
                "condition": "",
                "is_current": True,
            }
        ]

    def _build_wiki_render_data(self, item: Dict[str, Any], query: str):
        stats = item.get("stats") or {}
        stat_defs = [
            ("HP", "hp", "#4bc074"),
            ("攻击", "atk", "#e95f5f"),
            ("魔攻", "sp_atk", "#6f85ff"),
            ("防御", "def", "#da9c37"),
            ("魔抗", "sp_def", "#18a1a1"),
            ("速度", "spd", "#9b61ff"),
        ]
        pet_stats = [
            {"label": label, "value": int(stats.get(key, 0) or 0), "color": color}
            for label, key, color in stat_defs
        ]
        ability_name = item.get("ability_name") or item.get("ability") or "暂无"
        ability_desc = item.get("ability_desc") or item.get("ability_description") or "暂无特性描述"
        pet_types = [{"name": attr} for attr in self._normalize_wiki_type_values(item.get("attributes") or item.get("types"))]
        sprite_skills = []
        skills = item.get("skills") or item.get("skill_list") or []
        for skill in skills[:24]:
            sprite_skills.append(
                {
                    "name": skill.get("name", "未知技能"),
                    "type": skill.get("attribute", "未知"),
                    "category": skill.get("category", "未知"),
                    "power": skill.get("power", "?"),
                    "pp": skill.get("cost", "?"),
                    "effect": skill.get("description", "暂无描述"),
                    "level": skill.get("level", "-"),
                }
            )
        matchup = item.get("type_matchup") or {}
        traits = [
            {"name": ability_name, "type": "特性", "effect": ability_desc, "type_class": "ability"}
        ]
        matchup_defs = [
            ("克制", "strong_against"),
            ("被克制", "weak_to"),
            ("抗性", "resists"),
            ("被抗", "resisted_by"),
        ]
        for label, key in matchup_defs:
            values = self._normalize_wiki_type_values(matchup.get(key))
            traits.append(
                {
                    "name": label,
                    "type": "属性",
                    "effect": "、".join(values) if values else "暂无",
                    "type_class": "matchup",
                }
            )
        description = (
            item.get("description")
            or item.get("summary")
            or item.get("intro")
            or item.get("profile")
            or ability_desc
            or "暂无图鉴描述"
        )
        return {
            "name": item.get("name", query),
            "number": item.get("no", "???"),
            "query": query,
            "form": item.get("form", ""),
            "pet_types": pet_types,
            "pet_icon": self._wiki_pet_icon(item),
            "main_image": self._wiki_pet_image(item),
            "total_stats": int(stats.get("total", 0) or sum(x["value"] for x in pet_stats)),
            "pet_stats": pet_stats,
            "description": description,
            "pet_traits": traits,
            "pet_evolution": self._build_wiki_evolution_data(item),
            "sprite_skills": sprite_skills,
            "updated_at": item.get("updated_at", ""),
            "wiki_url": item.get("url", ""),
            "commandHint": "💡 /洛克wiki <精灵名> | /洛克技能 <技能名>",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }


    def _build_skill_render_data(self, item: Dict[str, Any], query: str):
        power = item.get("power")
        cost = item.get("cost")
        return {
            "name": item.get("name", query),
            "query": query,
            "attribute": item.get("attribute", "unknown"),
            "category": item.get("category", "unknown"),
            "cost": cost if cost not in (None, "") else "?",
            "power": power if power not in (None, "") else "?",
            "description": item.get("description", "No description"),
            "updated_at": item.get("updated_at", ""),
            "commandHint": "/洛克技能 <技能名>",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _normalize_query_text(self, text: str) -> str:
        return re.sub(r"\s+", "", str(text or "")).strip().lower()

    def _find_exact_skill_match(self, results: List[Dict[str, Any]], query: str) -> Dict[str, Any] | None:
        normalized_query = self._normalize_query_text(query)
        if not normalized_query:
            return None
        for item in results:
            name = item.get("name", "")
            form = item.get("form", "")
            candidates = [
                self._normalize_query_text(name),
                self._normalize_query_text(f"{name}{form}"),
                self._normalize_query_text(f"{name} {form}"),
            ]
            if normalized_query in candidates:
                return item
        return None

    def _normalize_lineup_lookup_id(self, raw_value: str) -> str:
        text = str(raw_value or "").strip()
        match = re.search(r"\d+", text)
        if match:
            return match.group(0)
        return text

    def _is_target_lineup(self, lineup: Dict[str, Any], lineup_id: str) -> bool:
        target = self._normalize_lineup_lookup_id(lineup_id)
        if not target:
            return False
        lineup_candidates = {
            self._normalize_lineup_lookup_id(lineup.get("id", "")),
            self._normalize_lineup_lookup_id(lineup.get("code", "")),
            self._normalize_lineup_lookup_id(lineup.get("lineup_code", "")),
        }
        lineup_candidates.discard("")
        return target in lineup_candidates

    def _build_inspect_render_data(
        self,
        title: str,
        subtitle: str,
        rows: List[Dict[str, Any]] | None = None,
        notes: List[str] | None = None,
        payload: Dict[str, Any] | None = None,
        show_payload: bool = False,
        command_hint: str = "",
    ) -> Dict[str, Any]:
        return {
            "title": title,
            "subtitle": subtitle,
            "rows": rows or [],
            "notes": notes or [],
            "payload_text": json.dumps(payload or {}, ensure_ascii=False, indent=2)
            if show_payload and payload
            else "",
            "commandHint": command_hint,
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _format_json_payload(self, payload: Any) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            return str(payload)

    def _get_user_identifier(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "")

    def _stringify_inspect_value(self, value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, list):
            if not value:
                return "-"
            if all(not isinstance(item, (dict, list)) for item in value):
                return "、".join(str(item) for item in value)
            return f"共 {len(value)} 项"
        if isinstance(value, dict):
            if not value:
                return "-"
            pairs = []
            for k, v in list(value.items())[:4]:
                pairs.append(f"{k}: {self._stringify_inspect_value(v)}")
            text = " | ".join(pairs)
            if len(value) > 4:
                text += " | ..."
            return text
        return str(value)

    def _flatten_payload_rows(
        self,
        payload: Any,
        prefix: str = "",
        level: int = 0,
        max_depth: int = 3,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if level > max_depth:
            return rows

        if isinstance(payload, dict):
            for key, value in payload.items():
                label = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(value, dict):
                    if value:
                        rows.extend(
                            self._flatten_payload_rows(
                                value, prefix=label, level=level + 1, max_depth=max_depth
                            )
                        )
                    else:
                        rows.append({"label": label, "value": "-", "level": level})
                elif isinstance(value, list):
                    if not value:
                        rows.append({"label": label, "value": "-", "level": level})
                        continue
                    if all(not isinstance(item, (dict, list)) for item in value):
                        rows.append(
                            {
                                "label": label,
                                "value": self._stringify_inspect_value(value),
                                "level": level,
                            }
                        )
                        continue
                    for index, item in enumerate(value[:8], start=1):
                        item_label = f"{label}[{index}]"
                        if isinstance(item, (dict, list)):
                            rows.extend(
                                self._flatten_payload_rows(
                                    item,
                                    prefix=item_label,
                                    level=level + 1,
                                    max_depth=max_depth,
                                )
                            )
                        else:
                            rows.append(
                                {
                                    "label": item_label,
                                    "value": self._stringify_inspect_value(item),
                                    "level": level,
                                }
                            )
                    if len(value) > 8:
                        rows.append(
                            {
                                "label": label,
                                "value": f"其余 {len(value) - 8} 项已省略",
                                "level": level,
                            }
                        )
                else:
                    rows.append(
                        {
                            "label": label,
                            "value": self._stringify_inspect_value(value),
                            "level": level,
                        }
                    )
            return rows

        if isinstance(payload, list):
            return self._flatten_payload_rows(
                {"items": payload}, prefix=prefix, level=level, max_depth=max_depth
            )

        if prefix:
            rows.append(
                {
                    "label": prefix,
                    "value": self._stringify_inspect_value(payload),
                    "level": level,
                }
            )
        return rows

    def _rows_from_response_payload(self, payload: Dict[str, Any] | None) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        if payload.get("rows"):
            return payload.get("rows") or []
        return self._flatten_payload_rows(payload)

    def _account_type_text(self, account_type: int) -> str:
        return {0: "自动", 1: "QQ", 2: "微信"}.get(account_type, str(account_type))

    def _friendship_status_text(self, status: Any) -> str:
        status_map = {
            0: "查询成功",
            1: "状态码 1",
            2: "状态码 2",
            3: "状态码 3",
        }
        try:
            status_int = int(status)
        except Exception:
            return str(status or "-")
        return status_map.get(status_int, f"状态码 {status_int}")

    def _student_perk_state_text(self, state: Any) -> str:
        try:
            state_int = int(state)
        except Exception:
            return str(state or "-")
        return f"状态码 {state_int}"

    def _student_state_code_text(self, state: Any) -> str:
        state_map = {
            0: "未认证",
            1: "已认证",
            2: "审核中",
        }
        try:
            state_int = int(state)
        except Exception:
            return str(state or "-")
        return state_map.get(state_int, f"状态码 {state_int}")

    def _extract_scalar_items(
        self,
        payload: Dict[str, Any],
        exclude_keys: set[str] | None = None,
        label_map: Dict[str, str] | None = None,
    ) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        exclude_keys = exclude_keys or set()
        label_map = label_map or {}
        for key, value in payload.items():
            if key in exclude_keys or isinstance(value, (dict, list)):
                continue
            items.append(
                {
                    "label": label_map.get(key, key.replace("_", " ").title()),
                    "value": self._stringify_inspect_value(value),
                }
            )
        return items

    def _build_friendship_render_data(
        self, payload: Dict[str, Any], user_ids: str
    ) -> Dict[str, Any]:
        result = payload.get("result") or {}
        users = payload.get("user_list") or payload.get("userList") or []
        user_cards = []
        for index, user in enumerate(users, start=1):
            status_code = user.get("status")
            user_cards.append(
                {
                    "title": f"用户 {index}",
                    "userId": str(user.get("user_id") or user.get("userId") or "-"),
                    "statusCode": self._stringify_inspect_value(status_code),
                    "statusText": "状态正常" if str(status_code) == "0" else self._friendship_status_text(status_code),
                    "statusDesc": "接口已返回该用户状态，但后端当前没有提供更具体的关系类型说明。",
                }
            )

        summary_cards = [
            {"label": "查询对象", "value": str(len(user_cards) or len(user_ids.split(",")))},
            {
                "label": "接口状态",
                "value": "成功" if result.get("error_code", 0) == 0 else "异常",
            },
            {
                "label": "上游返回",
                "value": result.get("error_message") or "OK",
            },
        ]
        return {
            "title": "好友关系",
            "subtitle": f"查询 ID：{user_ids}",
            "summaryCards": summary_cards,
            "userCards": user_cards,
            "resultCode": self._stringify_inspect_value(result.get("error_code", 0)),
            "resultDesc": "当前接口只返回 status 字段，尚未提供“好友/非好友/黑名单”等可读关系类型。",
            "commandHint": "💡 /洛克好友关系 <id1,id2>",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _build_shop_render_data(self, payload: Dict[str, Any], shop_id: str) -> Dict[str, Any]:
        if payload.get("rows"):
            return self._build_shop_render_data_from_rows(payload, shop_id)
        summary_cards = []
        detail_items = []
        sections = []

        scalar_label_map = {
            "shop_id": "商店 ID",
            "id": "ID",
            "name": "名称",
            "title": "标题",
            "desc": "说明",
            "description": "说明",
            "refresh_time": "刷新时间",
            "open_time": "开放时间",
            "close_time": "关闭时间",
            "currency": "货币",
        }

        for key, value in payload.items():
            if isinstance(value, list):
                if not value:
                    continue
                cards = []
                for idx, item in enumerate(value[:24], start=1):
                    if isinstance(item, dict):
                        title = (
                            item.get("name")
                            or item.get("title")
                            or item.get("item_name")
                            or f"{key} #{idx}"
                        )
                        image = (
                            item.get("icon")
                            or item.get("icon_url")
                            or item.get("image")
                            or item.get("image_url")
                            or ""
                        )
                        metas = []
                        for mk, mv in item.items():
                            if mk in {"name", "title", "item_name", "icon", "icon_url", "image", "image_url"}:
                                continue
                            if isinstance(mv, (dict, list)):
                                continue
                            metas.append(
                                {
                                    "label": scalar_label_map.get(mk, mk.replace("_", " ").title()),
                                    "value": self._stringify_inspect_value(mv),
                                }
                            )
                        cards.append(
                            {
                                "title": title,
                                "image": image,
                                "meta": metas[:6],
                            }
                        )
                    else:
                        cards.append(
                            {
                                "title": self._stringify_inspect_value(item),
                                "image": "",
                                "meta": [],
                            }
                        )
                sections.append(
                    {
                        "title": key.replace("_", " ").title(),
                        "cards": cards,
                    }
                )
                summary_cards.append({"label": key.replace("_", " ").title(), "value": str(len(value))})
            elif isinstance(value, dict):
                for subk, subv in value.items():
                    if isinstance(subv, (dict, list)):
                        continue
                    detail_items.append(
                        {
                            "label": scalar_label_map.get(subk, subk.replace("_", " ").title()),
                            "value": self._stringify_inspect_value(subv),
                        }
                    )
            else:
                detail_items.append(
                    {
                        "label": scalar_label_map.get(key, key.replace("_", " ").title()),
                        "value": self._stringify_inspect_value(value),
                    }
                )

        if not summary_cards:
            summary_cards = [
                {"label": "数据字段", "value": str(len(payload))},
                {"label": "商店 ID", "value": shop_id},
                {"label": "列表分组", "value": str(len(sections))},
            ]
        else:
            summary_cards = ([{"label": "商店 ID", "value": shop_id}] + summary_cards)[:3]

        hero_title = "商店信息"
        hero_value = next((item["value"] for item in detail_items if item["label"] in {"名称", "标题"}), shop_id)
        hero_subvalue = f"shop_id = {shop_id}"

        return {
            "title": "洛克商店",
            "subtitle": f"shop_id = {shop_id}",
            "heroTitle": hero_title,
            "heroValue": hero_value,
            "heroSubvalue": hero_subvalue,
            "summaryCards": summary_cards,
            "sections": sections,
            "detailItems": detail_items[:18],
            "commandHint": "💡 /洛克商店 <shop_id>",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _build_shop_render_data_from_rows(self, payload: Dict[str, Any], shop_id: str) -> Dict[str, Any]:
        rows = payload.get("rows") or []
        notes = payload.get("notes") or []
        top_level = [row for row in rows if int(row.get("level", 0) or 0) == 0]
        nested = [row for row in rows if int(row.get("level", 0) or 0) > 0]

        top_map = {str(row.get("field", "")): str(row.get("value", "")) for row in top_level}
        summary_cards = [
            {"label": "商店 ID", "value": top_map.get("shop_id", shop_id)},
            {"label": "返回码", "value": top_map.get("ret_code", "-")},
            {"label": "商品数量", "value": top_map.get("goods_count", str(len(nested) > 0))},
        ]

        current_card = {"title": f"商品 #{1}", "image": "", "meta": []}
        cards = []
        goods_index = 0
        for row in nested:
            field = str(row.get("field", ""))
            label = row.get("label") or field
            value = str(row.get("value", ""))
            if field == "goods_id":
                if current_card["meta"]:
                    cards.append(current_card)
                goods_index += 1
                current_card = {
                    "title": f"商品 #{goods_index}",
                    "image": "",
                    "meta": [{"label": label, "value": value}],
                }
            else:
                current_card["meta"].append({"label": label, "value": value})
        if current_card["meta"]:
            cards.append(current_card)

        detail_items = [
            {
                "label": row.get("label") or row.get("field") or "-",
                "value": str(row.get("value", "")),
            }
            for row in top_level
        ]
        if notes:
            detail_items.extend([{"label": "附加说明", "value": str(note)} for note in notes[:6]])

        return {
            "title": "洛克商店",
            "subtitle": payload.get("title") or f"shop_id = {shop_id}",
            "heroTitle": "商店查询",
            "heroValue": top_map.get("shop_id", shop_id),
            "heroSubvalue": f"商品数量 {top_map.get('goods_count', '0')}",
            "summaryCards": summary_cards,
            "sections": [{"title": "商品列表", "cards": cards}] if cards else [],
            "detailItems": detail_items,
            "commandHint": "💡 /洛克商店 <shop_id>",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _clean_player_field_value(self, field: str, value: str) -> str:
        text = str(value or "").strip().strip("'")
        if text in {"<0B>", "<0b>", "<0B >", "<0b >", ""}:
            return "未设置"
        if field in {"is_online", "online", "chat_top_unlock", "is_friend", "is_black", "is_black_role", "is_chat_node_unlock"}:
            return "是" if text in {"1", "true", "True"} else "否"
        if field in {"sex", "gender"}:
            return {"0": "未知", "1": "男", "2": "女"}.get(text, text)
        if field in {"friend_type"}:
            return {"0": "默认", "1": "特殊"}.get(text, text)
        if field == "battle_state":
            return {"0": "空闲", "1": "对战中"}.get(text, text)
        return text

    def _parse_ingame_player_payload(self, payload: Dict[str, Any], uid: str) -> Dict[str, Any]:
        rows = payload.get("rows") or []
        notes = payload.get("notes") or []
        row_map: Dict[str, str] = {}
        label_map: Dict[str, str] = {}
        for row in rows:
            field = str(row.get("field", ""))
            row_map[field] = str(row.get("value", ""))
            label_map[field] = str(row.get("label") or row.get("field") or "")

        title = payload.get("title") or "玩家搜索"
        nickname = self._clean_player_field_value("name", row_map.get("name", "-"))
        player_uid = self._clean_player_field_value("uin", row_map.get("uin", uid))
        level = self._clean_player_field_value("level", row_map.get("level", "-"))
        signature = self._clean_player_field_value("signature", row_map.get("signature", ""))
        if signature == "未设置":
            signature = "这个玩家还没有设置个性签名"
        ret_code = self._clean_player_field_value("ret_code", row_map.get("ret_code", "0"))

        section_defs = [
            (
                "基础信息",
                [
                    "uin",
                    "name",
                    "level",
                    "gender",
                    "online",
                    "signature",
                    "note",
                    "openid",
                    "regist_date",
                    "last_logout_time",
                    "world_level",
                    "card_handbook_collect_num",
                ],
            ),
            (
                "社交关系",
                [
                    "is_friend",
                    "is_black_role",
                    "friend_type",
                    "add_friend_time",
                    "pinned_time",
                    "bp_gift_grade",
                    "cli_login_channel",
                    "is_chat_node_unlock",
                    "plat_nick_name",
                ],
            ),
            (
                "家园信息",
                [
                    "home_name",
                    "home_experience",
                    "home_level",
                    "room_level",
                    "home_comfort_level",
                    "visitor_num",
                ],
            ),
            (
                "战斗信息",
                [
                    "battle_conf_id",
                    "battle_state",
                    "card_skin_selected",
                    "card_icon_selected",
                    "card_label_first_selected",
                    "card_label_last_selected",
                    "display_type",
                    "scene_res_cfg_id",
                    "camp_id",
                ],
            ),
        ]

        used_fields = set()
        sections = []
        for section_title, fields in section_defs:
            items = []
            for field in fields:
                if field not in row_map:
                    continue
                items.append(
                    {
                        "label": label_map.get(field, field),
                        "value": self._clean_player_field_value(field, row_map.get(field, "")),
                    }
                )
                used_fields.add(field)
            if items:
                sections.append({"title": section_title, "items": items})

        extra_items = []
        skip_fields = {
            "ret_info",
            "player_info",
            "battle_brief_info",
            "home_info",
            "start_up_privilege_info",
            "pos_info",
            "visit_info",
            "ban_info",
        }
        for row in rows:
            field = str(row.get("field", ""))
            if field in used_fields or field in skip_fields:
                continue
            raw_value = str(row.get("value", ""))
            if raw_value.startswith("(") and raw_value.endswith(")"):
                continue
            extra_items.append(
                {
                    "label": row.get("label") or field,
                    "value": self._clean_player_field_value(field, raw_value),
                }
            )
        if extra_items:
            sections.append({"title": "其他信息", "items": extra_items[:12]})

        note_items = [{"label": "附加说明", "value": str(note)} for note in notes[:6]]
        return {
            "title": title,
            "nickname": nickname if nickname and nickname != "-" else player_uid,
            "uid": player_uid,
            "level": level,
            "signature": signature,
            "retCode": ret_code,
            "online": self._clean_player_field_value("online", row_map.get("online", row_map.get("is_online", "0"))),
            "sections": sections,
            "noteItems": note_items,
            "labelMap": label_map,
            "rowMap": {k: self._clean_player_field_value(k, v) for k, v in row_map.items()},
        }

    def _player_field(self, parsed: Dict[str, Any] | None, field: str, default: str = "-") -> str:
        if not parsed:
            return default
        row_map = parsed.get("rowMap") or {}
        value = str(row_map.get(field, default) or default).strip()
        return value if value else default

    def _player_signature_text(self, parsed: Dict[str, Any] | None) -> str:
        if not parsed:
            return ""
        text = str(parsed.get("signature") or "").strip()
        if not text or text == "未设置":
            return ""
        return text

    def _build_player_curated_sections(
        self, parsed: Dict[str, Any], include_card: bool = True
    ) -> List[Dict[str, Any]]:
        def pack(title: str, pairs: List[tuple[str, str]]) -> Dict[str, Any] | None:
            items = [{"label": label, "value": value} for label, value in pairs if value and value != "-" and value != "未设置"]
            return {"title": title, "items": items} if items else None

        sections = [
            pack(
                "核心档案",
                [
                    ("等级", parsed.get("level", "-")),
                    ("在线状态", self._player_field(parsed, "online")),
                    ("性别", self._player_field(parsed, "gender", self._player_field(parsed, "sex"))),
                    ("世界等级", self._player_field(parsed, "world_level")),
                    ("图鉴收集", self._player_field(parsed, "card_handbook_collect_num")),
                    ("最后离线", self._player_field(parsed, "last_logout_time")),
                ],
            ),
            pack(
                "家园信息",
                [
                    ("家园名称", self._player_field(parsed, "home_name")),
                    ("家园等级", self._player_field(parsed, "home_level")),
                    ("家园经验", self._player_field(parsed, "home_experience")),
                    ("舒适度", self._player_field(parsed, "home_comfort_level")),
                    ("访客数量", self._player_field(parsed, "visitor_num")),
                ],
            ),
        ]
        if include_card:
            sections.append(
                pack(
                    "名片信息",
                    [
                        ("名片皮肤", self._player_field(parsed, "card_skin_selected")),
                        ("名片头像", self._player_field(parsed, "card_icon_selected")),
                        ("首标签", self._player_field(parsed, "card_label_first_selected")),
                        ("尾标签", self._player_field(parsed, "card_label_last_selected")),
                    ],
                )
            )
        return [section for section in sections if section]

    def _build_player_search_render_data(self, payload: Dict[str, Any], uid: str) -> Dict[str, Any]:
        parsed = self._parse_ingame_player_payload(payload, uid)
        curated_sections = self._build_player_curated_sections(parsed, include_card=True)
        signature = self._player_signature_text(parsed)
        summary_cards = [
            {"label": "等级", "value": parsed["level"]},
            {"label": "在线状态", "value": parsed["online"]},
            {"label": "世界等级", "value": self._player_field(parsed, "world_level")},
            {"label": "图鉴收集", "value": self._player_field(parsed, "card_handbook_collect_num")},
            {"label": "家园等级", "value": self._player_field(parsed, "home_level")},
            {"label": "舒适度", "value": self._player_field(parsed, "home_comfort_level")},
        ]
        summary_cards = [item for item in summary_cards if item["value"] and item["value"] != "-"]

        return {
            "title": "洛克玩家",
            "subtitle": parsed["title"],
            "heroTitle": "玩家信息",
            "heroValue": parsed["nickname"],
            "heroSubvalue": f"UID {parsed['uid']} · 返回码 {parsed['retCode']}",
            "summaryCards": summary_cards[:6],
            "signature": signature,
            "showSignature": bool(signature),
            "sections": curated_sections,
            "commandHint": "💡 /洛克玩家 <UID>",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _build_student_state_render_data(
        self, payload: Dict[str, Any], account_type: int
    ) -> Dict[str, Any]:
        result = payload.get("result") or {}
        certified = payload.get("certified")
        game_certified = payload.get("game_certified")
        school = payload.get("school") or payload.get("school_name") or "未返回"
        summary_cards = [
            {"label": "账号来源", "value": self._account_type_text(account_type)},
            {
                "label": "认证状态",
                "value": "已认证" if str(certified) == "1" else "未认证",
            },
            {
                "label": "学校信息",
                "value": school,
            },
        ]
        detail_items = [
            {"label": "学生认证", "value": "是" if str(certified) == "1" else "否"},
            {
                "label": "游戏内认证",
                "value": "是" if str(game_certified) == "1" else "否",
            },
            {"label": "学校", "value": school},
            {"label": "上游状态", "value": result.get("error_message") or "WG_COMM_SUCC"},
            {
                "label": "上游错误码",
                "value": self._stringify_inspect_value(result.get("error_code", 0)),
            },
        ]
        return {
            "title": "学生认证状态",
            "subtitle": f"账号类型：{self._account_type_text(account_type)}",
            "summaryCards": summary_cards,
            "detailItems": detail_items,
            "heroTitle": "学生认证",
            "heroValue": "已通过" if str(certified) == "1" else "未认证",
            "heroSubvalue": school,
            "commandHint": "💡 /洛克学生 [area] [account_type]",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _build_student_perks_render_data(
        self, payload: Dict[str, Any], area: int, account_type: int
    ) -> Dict[str, Any]:
        result = payload.get("result") or {}
        cards = payload.get("cards") or []
        perk_cards = []
        for card in cards:
            state_code = card.get("state")
            perk_cards.append(
                {
                    "name": card.get("name") or f"奖励 #{card.get('id', '-')}",
                    "count": card.get("count", 0),
                    "desc": card.get("desc") or "暂无说明",
                    "icon": card.get("icon") or "",
                    "id": self._stringify_inspect_value(card.get("id")),
                    "stateCode": self._stringify_inspect_value(state_code),
                    "stateText": self._student_perk_state_text(state_code),
                }
            )
        detail_items = self._extract_scalar_items(
            payload,
            exclude_keys={"cards", "result"},
            label_map={
                "area": "大区",
                "account_type": "账号类型",
                "activity_name": "活动名称",
                "activity_desc": "活动说明",
                "desc": "活动说明",
            },
        )
        return {
            "title": "学生活动福利",
            "subtitle": f"大区：{area}  账号类型：{self._account_type_text(account_type)}",
            "summaryCards": [
                {"label": "奖励数量", "value": str(len(perk_cards))},
                {"label": "账号来源", "value": self._account_type_text(account_type)},
                {"label": "上游状态", "value": result.get("error_message") or "WG_COMM_SUCC"},
            ],
            "perkCards": perk_cards,
            "detailItems": detail_items,
            "heroTitle": "学生活动奖励",
            "heroValue": str(len(perk_cards)),
            "heroSubvalue": "当前返回奖励项",
            "commandHint": "💡 /洛克学生 [area] [account_type]",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _build_student_render_data(
        self,
        state_payload: Dict[str, Any],
        perks_payload: Dict[str, Any],
        area: int,
        account_type: int,
    ) -> Dict[str, Any]:
        state_data = self._build_student_state_render_data(state_payload, account_type)
        perks_data = self._build_student_perks_render_data(
            perks_payload, area, account_type
        )
        state_result = state_payload.get("result") or {}
        perks_result = perks_payload.get("result") or {}
        return {
            "title": "洛克学生",
            "subtitle": f"大区：{area}  账号类型：{self._account_type_text(account_type)}",
            "heroTitle": "学生信息总览",
            "heroValue": state_data.get("heroValue", "未认证"),
            "heroSubvalue": state_data.get("heroSubvalue", "未返回"),
            "summaryCards": [
                {
                    "label": "认证状态",
                    "value": state_data.get("heroValue", "未认证"),
                },
                {
                    "label": "学校",
                    "value": state_data.get("heroSubvalue", "未返回"),
                },
                {
                    "label": "奖励数量",
                    "value": str(len(perks_data.get("perkCards") or [])),
                },
            ],
            "stateItems": state_data.get("detailItems") or [],
            "perkCards": perks_data.get("perkCards") or [],
            "detailItems": perks_data.get("detailItems") or [],
            "stateResult": state_result.get("error_message") or "WG_COMM_SUCC",
            "perksResult": perks_result.get("error_message") or "WG_COMM_SUCC",
            "commandHint": "💡 /洛克学生 [area] [account_type]",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    @filter.command("洛克")
    async def rocom_help(self, event: AstrMessageEvent):
        """洛克王国帮助菜单"""
        menu_groups = [
                {
                    "groupTitle": "账号管理与登录",
                    "groupSubtitle": "绑定用户信息",
                    "menuItems": [
                        {"cmd": "洛克 QQ 登录", "desc": "使用 QQ 扫码快捷登录及绑定"},
                        {"cmd": "洛克微信登录", "desc": "使用微信扫码快捷登录及绑定"},
                        {"cmd": "洛克导入 <ID> <Ticket>", "desc": "通过客户端凭证手动登录"},
                        {"cmd": "洛克刷新", "desc": "刷新当前主账号 QQ 凭证，非必要不要使用，直接重绑"},
                        {"cmd": "洛克刷新所有凭证", "desc": "刷新所有用户的凭证 (管理员，仅作调试或强制兜底，非必要不要使用)"},
                        {"cmd": "洛克删除无效绑定", "desc": "清理失效的绑定记录 (管理员)"}
                    ]
                },
                {
                    "groupTitle": "数据查询",
                    "groupSubtitle": "查询推送服务（含实验性/暂不可用功能）",
                    "menuItems": [
                        {"cmd": "洛克档案", "desc": "生成个人数据名片"},
                        {"cmd": "洛克战绩 <页码>", "desc": "查询并展示近期的对战场次记录"},
                        {"cmd": "洛克背包 <筛选> <页码>", "desc": "查看精灵收集 (筛选:全部/异色/了不起/炫彩，参数可交换)"},
                        {"cmd": "洛克阵容 <分类> <页码>", "desc": "查看阵容助手推荐阵容 (参数可交换)"},
                        {"cmd": "洛克交换大厅 <页码>", "desc": "查看交换大厅海报 (支持别名：洛克大厅/交换大厅)"},
                        {"cmd": "远行商人", "desc": "查看当前轮次远行商人商品"},
                        {"cmd": "洛克商店 <shop_id>", "desc": "实验性：查询商店信息，接口返回暂不稳定"},
                        {"cmd": "洛克玩家 <UID>", "desc": "通过 ingame 接口查询玩家基础信息，当前推荐优先使用"},
                        {"cmd": "订阅远行商人 1/0 [商品 商品]", "desc": "群主/群管/bot管理可配置本群订阅商品，不填商品则用默认配置"},
                        {"cmd": "取消订阅远行商人", "desc": "关闭当前群远行商人订阅"},
                        {"cmd": "洛克好友关系 <id1,id2>", "desc": "实验性：仅返回有限状态字段，关系说明暂不稳定（需登录）"},
                        {"cmd": "洛克学生", "desc": "实验性：接口信息量有限，当前仅供测试查看（需登录）"},
                        {"cmd": "洛克wiki <精灵名>", "desc": "暂不可用：接口暂时关闭，当前仅返回提示"},
                        {"cmd": "洛克技能 <技能名>", "desc": "暂不可用：接口暂时关闭，当前仅返回提示"},
                        {"cmd": "洛克查蛋 <精灵名>", "desc": "查询精灵蛋组及可配种精灵 (支持别名：查蛋)"},
                        {"cmd": "洛克查蛋 0.18m 1.5kg", "desc": "按身高和体重反查精灵，身高统一使用游戏原生 m"},
                        {"cmd": "洛克配种 <精灵A> <精灵B>", "desc": "判断两只精灵能否配种 (支持别名：配种)"}
                    ]
                },
                {
                    "groupTitle": "多账号操作",
                    "groupSubtitle": "账号切换与管理",
                    "menuItems": [
                        {"cmd": "洛克绑定列表", "desc": "查看所有已扫码绑定的账号"},
                        {"cmd": "洛克切换 <序号>", "desc": "一键切换活跃的数据查询主账号"},
                        {"cmd": "洛克登录", "desc": "扫码登录及绑定"},
                        {"cmd": "洛克解绑 <序号>", "desc": "移除账号绑定记录"}
                    ]
                }
            ]
        if self.help_prefix_display:
            for group in menu_groups:
                for item in group.get("menuItems", []):
                    item["cmd"] = f"{self.help_prefix_display}{item['cmd']}"

        data = {
            "pageTitle": "洛克王国插件",
            "pageSubtitle": "AstrBot Roco Kingdom Data Plugin",
            "menuGroups": menu_groups
        }
        img_url = await self.renderer.render_html("render/menu/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result("菜单生成失败。")

    async def _save_binding_with_role_info(self, event: AstrMessageEvent, fw_token: str, login_type: str, user_id: str):
        yield event.plain_result("登录成功，正在调用绑定接口...")
        bind_res = await self.client.create_binding(fw_token, user_id)
        binding_data = (bind_res or {}).get("binding") or {}
        if not binding_data:
            bindings_res = await self.client.get_bindings(user_id)
            bindings = (bindings_res or {}).get("bindings") or []
            binding_data = next(
                (
                    item for item in bindings
                    if (item.get("framework_token") or "") == fw_token
                ),
                {},
            )
        if not binding_data:
            err = self.client.get_last_error("绑定接口调用失败")
            yield event.plain_result(f"绑定接口调用失败：{err}")
            return
        
        yield event.plain_result("绑定成功，正在获取角色信息...")
        role_res = await self.client.get_role(fw_token, user_identifier=self._get_user_identifier(event))
        
        # 检查角色信息获取是否成功
        if not role_res or not role_res.get("role"):
            err = self.client.get_last_error("获取角色信息失败")
            logger.warning(f"[Rocom] 获取角色信息失败：{err}")

            binding_id = binding_data.get("id", fw_token)
            fallback_role_id = binding_data.get("tgp_id") or "未知"
            fallback_login_type = binding_data.get("login_type") or login_type
            fallback_nickname = "未初始化角色"
            binding = {
                "framework_token": fw_token,
                "binding_id": binding_id,
                "login_type": fallback_login_type,
                "role_id": str(fallback_role_id),
                "nickname": fallback_nickname,
                "bind_time": int(time.time() * 1000),
                "is_primary": True
            }
            await self.user_mgr.add_binding(user_id, binding)

            if "8258601" in err:
                yield event.plain_result(
                    "⚠️ 绑定已保存，但当前账号暂时查不到洛克角色资料（上游错误 8258601）。"
                    "这通常表示该账号尚未完成洛克角色初始化，或上游暂未返回角色数据。"
                    "你之后可直接重试 /洛克档案，无需重新登录。"
                )
            else:
                yield event.plain_result(
                    f"⚠️ 绑定已保存，但获取角色信息失败：{err}。"
                    "你之后可直接重试 /洛克档案，无需重新登录。"
                )
            return
        
        role = role_res.get("role", {})
        binding_id = binding_data.get("id", fw_token)
        
        binding = {
            "framework_token": fw_token,
            "binding_id": binding_id,
            "login_type": login_type,
            "role_id": role.get("id", "未知"),
            "nickname": role.get("name", "洛克"),
            "bind_time": int(time.time() * 1000),
            "is_primary": True
        }
        replace_result = await self.user_mgr.replace_binding_for_role(user_id, binding)
        removed_count = int(replace_result.get("removed_count", 0))
        if removed_count > 0:
            logger.info(
                f"[Rocom] 重新登录检测到相同 UID={binding['role_id']} 的旧绑定，已清理 {removed_count} 条旧记录后写入新凭证"
            )
        yield event.plain_result(f"✅ 绑定成功！当前账号：{binding['nickname']} (ID: {binding['role_id']})")

    async def _not_logged_in_hint(self, event: AstrMessageEvent):
        """统一的未登录引导"""
        yield event.plain_result("💡 [未登录] 你尚未绑定洛克王国账号。请参考下方菜单，发送 /洛克QQ登录 或 /洛克微信登录 进行绑定。")
        async for res in self.rocom_help(event):
            yield res

    @filter.command("洛克QQ登录")
    async def rocom_qq_login(self, event: AstrMessageEvent):
        """QQ 扫码登录"""
        user_id = event.get_sender_id()
        qr_data = await self.client.qq_qr_login(user_id)
        if not qr_data or "qr_image" not in qr_data:
            yield event.plain_result(f"获取 QQ 二维码失败：{self.client.get_last_error()}")
            return
            
        fw_token = qr_data["frameworkToken"]
        qr_b64 = qr_data["qr_image"]
        
        img_data = base64.b64decode(qr_b64.split(",")[-1])
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(img_data)
            tmp_path = tmp.name
            
        client, msg_id = await self._send_and_get_msg_id(event, [
            {"type": "at", "data": {"qq": str(event.get_sender_id())}},
            {"type": "text", "data": {"text": "\n请使用 QQ 扫描二维码登录 (有效时间 2 分钟)\n⚠️ 注意需要双设备扫码！"}},
            {"type": "image", "data": {"file": "base64://" + qr_b64.split(",")[-1]}}
        ])

        if msg_id is None:
            yield event.chain_result([
                Plain(f"@{event.get_sender_id()}\n请使用 QQ 扫描二维码登录 (有效时间 2 分钟)\n⚠️ 注意需要双设备扫码！"),
                Image.fromFileSystem(tmp_path)
            ])
            
        recall_task = self._schedule_recall(client, msg_id, 110) if client and msg_id else None
        
        start_time = time.time()
        success = False
        while time.time() - start_time < 115:
            await asyncio.sleep(3)
            status = await self.client.qq_qr_status(fw_token, user_id)
            if not status:
                continue
                
            state = status.get("status")
            if state == "done":
                success = True
                if recall_task and not recall_task.done():
                    recall_task.cancel()
                if client and msg_id:
                    try:
                        await client.delete_msg(message_id=msg_id)
                        logger.info(f"[Rocom] 登录成功，已撤回二维码消息 {msg_id}")
                    except Exception:
                        pass
                break
            elif state in ["expired", "failed", "canceled"]:
                if recall_task and not recall_task.done():
                    recall_task.cancel()
                if client and msg_id:
                    try:
                        await client.delete_msg(message_id=msg_id)
                    except Exception:
                        pass
                break
                
        if success:
            async for res in self._save_binding_with_role_info(event, fw_token, "qq", user_id):
                yield res
        else:
            yield event.plain_result("登录超时或失败，请重试。")

    @filter.command("洛克微信登录")
    async def rocom_wechat_login(self, event: AstrMessageEvent):
        """微信扫码登录"""
        user_id = event.get_sender_id()
        qr_data = await self.client.wechat_qr_login(user_id)
        if not qr_data or "qr_image" not in qr_data:
            yield event.plain_result(f"获取微信登录链接失败：{self.client.get_last_error()}")
            return
            
        fw_token = qr_data["frameworkToken"]
        qr_url = qr_data["qr_image"]
        
        client, msg_id = await self._send_and_get_msg_id(event, [
            {"type": "at", "data": {"qq": str(event.get_sender_id())}},
            {"type": "text", "data": {"text": f"\n请使用微信打开以下链接扫码登录 (有效时间 2 分钟)\n⚠️ 注意需要双设备扫码！\n{qr_url}"}}
        ])

        if msg_id is None:
            yield event.plain_result(f"@{event.get_sender_id()}\n请使用微信打开以下链接扫码登录 (有效时间 2 分钟)\n⚠️ 注意需要双设备扫码！\n{qr_url}")
            
        recall_task = self._schedule_recall(client, msg_id, 110) if client and msg_id else None
        
        start_time = time.time()
        success = False
        while time.time() - start_time < 115:
            await asyncio.sleep(3)
            status = await self.client.wechat_qr_status(fw_token, user_id)
            if not status:
                continue
                
            state = status.get("status")
            if state == "done":
                success = True
                if recall_task and not recall_task.done():
                    recall_task.cancel()
                if client and msg_id:
                    try:
                        await client.delete_msg(message_id=msg_id)
                        logger.info(f"[Rocom] 登录成功，已撤回链接消息 {msg_id}")
                    except Exception:
                        pass
                break
            elif state in ["expired", "failed"]:
                if recall_task and not recall_task.done():
                    recall_task.cancel()
                if client and msg_id:
                    try:
                        await client.delete_msg(message_id=msg_id)
                    except Exception:
                        pass
                break
                
        if success:
            async for res in self._save_binding_with_role_info(event, fw_token, "wechat", user_id):
                yield res
        else:
            yield event.plain_result("登录超时或失败，请重试。")

    @filter.command("洛克导入")
    async def rocom_import(self, event: AstrMessageEvent, tgp_id: str, tgp_ticket: str):
        """导入 WeGame 凭证"""
        user_id = event.get_sender_id()
        res = await self.client.import_token(tgp_id, tgp_ticket, user_id)
        if not res or not res.get("frameworkToken"):
            err_msg = self.client.get_last_error("凭证导入失败")
            yield event.plain_result(f"{err_msg}。")
            return
        fw_token = res["frameworkToken"]
        async for r in self._save_binding_with_role_info(event, fw_token, "manual", user_id):
            yield r

    @filter.command("洛克绑定列表", alias={"绑定列表"})
    async def rocom_bind_list(self, event: AstrMessageEvent):
        """查看已绑定账号列表"""
        bindings = await self.user_mgr.get_user_bindings(event.get_sender_id())
        if not bindings:
            yield event.plain_result("暂无绑定账号。")
            return
            
        bind_items = []
        for i, b in enumerate(bindings):
            create_ts = b.get("bind_time", 0)
            if create_ts > 0:
                dt = datetime.fromtimestamp(create_ts / 1000)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            else:
                time_str = "未知"
                
            bind_items.append({
                "index": i + 1,
                "nickname": b.get("nickname", "未知"),
                "isPrimary": b.get("is_primary", False),
                "role_id": b.get("role_id", "未知"),
                "type_label": b.get("login_type", "未知"),
                "created_at": time_str
            })
            
        data = {
            "title": "绑定账号列表",
            "subtitle": f"共找到 {len(bindings)} 个有效绑定账号",
            "bindings": bind_items,
            "commandHint": "💡 /洛克切换 <序号> 切换主账号 | /洛克解绑 <序号> 移除绑定",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin"
        }
        
        img_url = await self.renderer.render_html("render/bind-list/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            msg = "【绑定账号列表】\n"
            for item in bind_items:
                mark = " ⭐(主账号)" if item["isPrimary"] else ""
                msg += f"[{item['index']}] {item['nickname']} (ID: {item['role_id']}) {item['type_label']}{mark}\n"
            yield event.plain_result(msg)

    @filter.command("洛克切换")
    async def rocom_switch(self, event: AstrMessageEvent, index: int):
        """切换活跃主账号"""
        ok = await self.user_mgr.switch_primary(event.get_sender_id(), index)
        if ok:
            yield event.plain_result(f"成功切换到序号 {index} 账号。")
        else:
            yield event.plain_result("序号无效。")

    @filter.command("洛克解绑")
    async def rocom_unbind(self, event: AstrMessageEvent, index: int):
        """解绑并在本地移除账号"""
        removed = await self.user_mgr.delete_user_binding(event.get_sender_id(), index)
        if removed:
            await self.client.delete_binding(removed.get("binding_id", ""), event.get_sender_id())
            yield event.plain_result(f"已解绑账号：{removed.get('nickname')}")
        else:
            yield event.plain_result("序号无效。")
            
    @filter.command("洛克刷新")
    async def rocom_refresh(self, event: AstrMessageEvent):
        """刷新当前主账号凭证（非必要不要使用）"""
        user_id = event.get_sender_id()
        binding = await self.user_mgr.get_primary_binding(user_id)
        if not binding:
            async for res in self._not_logged_in_hint(event):
                yield res
            return

        binding_id = binding.get("binding_id", "")
        if not binding_id:
            yield event.plain_result("绑定 ID 无效，请重新绑定账号。")
            return

        yield event.plain_result("⚠️ 非必要不要手动刷新凭证，服务端会自动刷新。仅在凭证异常且你确认需要兜底时再使用此指令。")

        res = await self.client.refresh_binding(binding_id, user_id)
        if res and res.get("framework_token"):
            new_token = res["framework_token"]
            binding["framework_token"] = new_token
            bindings = await self.user_mgr.get_user_bindings(user_id)
            for i, b in enumerate(bindings):
                if b.get("binding_id") == binding_id:
                    bindings[i] = binding
                    break
            await self.user_mgr.save_user_bindings(user_id, bindings)
            yield event.plain_result("当前账号凭证刷新成功。非必要情况下仍建议直接重绑，不要频繁手动刷新。")
        else:
            yield event.plain_result("凭证刷新失败，可能已过期或不支持刷新（仅 QQ 扫码支持）。非必要不要手动刷新，服务端会自动刷新。")

    @filter.command("洛克删除无效绑定")
    async def rocom_cleanup_bindings(self, event: AstrMessageEvent):
        """删除所有人的无效绑定（需要 bot 管理员权限）"""
        # 检查 bot 管理员权限
        if not event.is_admin():
            uid = str(event.get_sender_id())
            allowed = [u.strip() for u in self.config.get("allowed_users", "").split(",") if u.strip()]
            if uid not in allowed:
                yield event.plain_result("⚠️ 此指令仅限 bot 管理员使用。")
                return

        yield event.plain_result("正在检查所有用户的绑定有效性...")

        # 获取所有用户的绑定数据
        all_users_data = await self.user_mgr.get_all_users_bindings()
        total_users = len(all_users_data)
        total_invalid = 0
        total_valid = 0

        for user_id, bindings in all_users_data.items():
            if not bindings:
                continue

            valid_bindings = []
            invalid_count = 0

            for binding in bindings:
                fw_token = binding.get("framework_token", "")
                binding_id = binding.get("binding_id", "")

                if not fw_token and not binding_id:
                    invalid_count += 1
                    # 删除本地无效绑定
                    if binding_id:
                        await self.user_mgr.remove_binding_by_id(user_id, binding_id)
                    continue

                role_res = await self.client.get_role(fw_token, user_identifier=str(user_id))
                if role_res and isinstance(role_res, dict) and role_res.get("role"):
                    valid_bindings.append(binding)
                else:
                    # 无效绑定：删除服务端 + 本地
                    if binding_id:
                        try:
                            # 调用 API 删除服务端绑定
                            await self.client.delete_binding(binding_id, str(user_id))
                            logger.info(f"已删除用户 {user_id} 的服务端绑定 {binding_id}")
                        except Exception as e:
                            logger.warning(f"删除用户 {user_id} 服务端绑定 {binding_id} 失败：{e}")
                        
                        # 删除本地绑定
                        await self.user_mgr.remove_binding_by_id(user_id, binding_id)
                        logger.info(f"已删除用户 {user_id} 本地绑定 {binding_id}")
                    
                    invalid_count += 1

            # 保存该用户的有效绑定
            if valid_bindings or invalid_count > 0:
                await self.user_mgr.save_user_bindings(user_id, valid_bindings)
            
            total_invalid += invalid_count
            total_valid += len(valid_bindings)

        if total_invalid > 0:
            yield event.plain_result(f"✅ 清理完成！共检查 {total_users} 位用户，移除 {total_invalid} 个无效绑定，当前剩余 {total_valid} 个有效绑定。")
        else:
            yield event.plain_result(f"✅ 所有绑定均有效，无需清理。共检查 {total_users} 位用户，{total_valid} 个有效绑定。")

    @filter.command("洛克档案", alias={"档案"})
    async def rocom_profile(self, event: AstrMessageEvent):
        """查看个人档案"""
        fw_token = await self._get_primary_token(event)
        if not fw_token:
            async for res in self._not_logged_in_hint(event):
                yield res
            return

        yield event.plain_result("正在获取洛克王国数据...")
        
        user_identifier = self._get_user_identifier(event)
        role_task = self.client.get_role(fw_token, user_identifier=user_identifier)
        eval_task = self.client.get_evaluation(fw_token, user_identifier=user_identifier)
        sum_task = self.client.get_pet_summary(fw_token, user_identifier=user_identifier)
        coll_task = self.client.get_collection(fw_token, user_identifier=user_identifier)
        battle_overview_task = self.client.get_battle_overview(fw_token, user_identifier=user_identifier)
        battle_list_task = self.client.get_battle_list(fw_token, page_size=1, user_identifier=user_identifier)
        
        results = await asyncio.gather(role_task, eval_task, sum_task, coll_task, battle_overview_task, battle_list_task, return_exceptions=True)
        role_res, eval_res, sum_res, coll_res, bo_res, bl_res = results
        
        if isinstance(role_res, Exception) or not role_res or not role_res.get("role"):
            err_msg = str(role_res) if isinstance(role_res, Exception) else (role_res.get("message") if isinstance(role_res, dict) else "未知错误")
            if "401" in err_msg or "403" in err_msg:
                err_hint = "【凭据过期】请尝试重新通过 QQ/微信 登录绑定。"
            else:
                err_hint = f"接口返回错误: {err_msg}"
            yield event.plain_result(f"获取角色档案失败。\n{err_hint}")
            return
            
        role = role_res["role"]
        ev = eval_res if isinstance(eval_res, dict) else {}
        sm = sum_res if isinstance(sum_res, dict) else {}
        cl = coll_res if isinstance(coll_res, dict) else {}
        bo = bo_res if isinstance(bo_res, dict) else {}
        if not sm:
            logger.warning("[Rocom] 洛克档案：pet-summary 接口不可用，已降级为基础档案渲染")
        if not ev:
            logger.warning("[Rocom] 洛克档案：evaluation 接口不可用，已降级为基础档案渲染")
        if not cl:
            logger.warning("[Rocom] 洛克档案：collection 接口不可用，已降级为基础档案渲染")
        if not bo:
            logger.warning("[Rocom] 洛克档案：battle-overview 接口不可用，已降级为基础档案渲染")
        player_search_res = await self.client.ingame_player_search(role.get("id", "")) if role.get("id") else None
        player_search_data = (
            self._parse_ingame_player_payload(player_search_res, str(role.get("id", "")))
            if player_search_res
            else None
        )
        profile_signature = self._player_signature_text(player_search_data) if player_search_data else ""
        profile_head_tags = []
        profile_home_items = []
        profile_card_items = []
        profile_card_image = ""
        if player_search_data:
            tag_pairs = [
                ("在线", self._player_field(player_search_data, "online")),
                ("性别", self._player_field(player_search_data, "gender", self._player_field(player_search_data, "sex"))),
                ("世界等级", self._player_field(player_search_data, "world_level")),
                ("家园等级", self._player_field(player_search_data, "home_level")),
            ]
            profile_head_tags = [
                {"label": label, "value": value}
                for label, value in tag_pairs
                if value and value != "-" and value != "未设置"
            ][:4]
            profile_home_items = [
                {"label": label, "value": value}
                for label, value in [
                    ("家园名称", self._player_field(player_search_data, "home_name")),
                    ("家园等级", self._player_field(player_search_data, "home_level")),
                    ("家园经验", self._player_field(player_search_data, "home_experience")),
                    ("舒适度", self._player_field(player_search_data, "home_comfort_level")),
                    ("访客数量", self._player_field(player_search_data, "visitor_num")),
                ]
                if value and value != "-" and value != "未设置"
            ]
            profile_card_items = [
                {"label": label, "value": value}
                for label, value in [
                    ("名片皮肤", self._player_field(player_search_data, "card_skin_selected")),
                    ("名片头像", self._player_field(player_search_data, "card_icon_selected")),
                ]
                if value and value != "-" and value != "未设置"
            ]
            profile_card_image = self._player_field(player_search_data, "card_bussiness_card_url", "")
        
        # 组装数据
        data = {
            "userName": role.get("name", "洛克"),
            "userAvatarDisplay": role.get("avatar_url", ""),
            "backgroundUrl": role.get("background_url", ""),
            "userLevel": role.get("level", 1),
            "userUid": role.get("id", ""),
            "enrollDays": role.get("enroll_days", 0),
            "starName": role.get("star_name", "魔法学徒"),
            
            "hasAiProfileData": "best_pet_id" in sm,
            "bestPetName": sm.get("best_pet_name", ""),
            "summaryTitleParts": sm.get("summary_title", "未 知").split(" "),
            "bestPetImageDisplay": sm.get("best_pet_img_url", ""),
            "fallbackPetImage": f"{{{{_res_path}}}}img/roco_icon.png",
            "scoreText": ev.get("score", "0.0"),
            "commandHint": "💡 /洛克背包 <筛选> <页码> | /洛克战绩 <页码> | /洛克 查看菜单",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
            
            "radarPolygons": [
                "130,30 230,130 130,230 30,130",
                "130,55 205,130 130,205 55,130",
                "130,80 180,130 130,180 80,130"
            ],
            "radarAxes": [{"x": 130, "y": 30}, {"x": 230, "y": 130}, {"x": 130, "y": 230}, {"x": 30, "y": 130}],
            "centerX": 130, "centerY": 130,
            
            "aiCommentText": sm.get("summary_content", "暂无点评"),
            
            "currentCollectionCount": cl.get("current_collection_count", 0),
            "totalCollectionCount": f"/{cl.get('total_collection_count', 0)}",
            "amazingSpriteCount": cl.get("amazing_sprite_count", 0),
            "shinySpriteCount": cl.get("shiny_sprite_count", 0),
            "colorfulSpriteCount": cl.get("colorful_sprite_count", 0),
            "collectionHint": "查看精灵收集详情",
            "fashionCollectionCount": cl.get("fashion_collection_count", 0),
            "itemCount": cl.get("item_count", 0),
            "hasExtraProfileData": bool(profile_signature or profile_home_items or profile_card_items or profile_card_image),
            "profileSignature": profile_signature,
            "showProfileSignature": bool(profile_signature),
            "profileHeadTags": profile_head_tags,
            "profileHomeItems": profile_home_items,
            "profileCardItems": profile_card_items,
            "profileCardImage": profile_card_image,
            "profileStatusText": self._player_field(player_search_data, "online", "未知"),
            "profileStatusClass": "online" if self._player_field(player_search_data, "online", "未知") == "是" else "offline",
            
            "hasBattleData": bo.get("total_match", 0) > 0,
            "tierBadgeUrl": bo.get("tier_icon_url", ""),
            "winRate": f"{bo.get('win_rate', 0)}%",
            "totalMatch": bo.get("total_match", 0),
            
            "opponentName": "",
            "opponentAvatarDisplay": "",
            "matchResult": "",
            "leftTeamPets": [],
            "rightTeamPets": [],
            "commandHint": "💡 /洛克背包 <筛选> <页码> | /洛克战绩 <页码> | /洛克 查看菜单",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin"
        }
        
        # Radar area scaling (mock base max values)
        max_str, max_coll, max_capt, max_prog = 100, 100, 100, 100
        str_val = min(ev.get("strength", 0), max_str)
        coll_val = min(ev.get("collection", 0), max_coll)
        capt_val = min(ev.get("capture", 0), max_capt)
        prog_val = min(ev.get("progression", 0), max_prog)
        
        def scalePt(value, max_v, dx, dy):
            r = value / max_v if max_v else 0
            return int(130 + dx * r), int(130 + dy * r)
            
        p1 = scalePt(str_val, max_str, 0, -100) # top
        p2 = scalePt(coll_val, max_coll, 100, 0) # right
        p3 = scalePt(capt_val, max_capt, 0, 100) # bot
        p4 = scalePt(prog_val, max_prog, -100, 0) # left
        
        data["radarAreaPoints"] = f"{p1[0]},{p1[1]} {p2[0]},{p2[1]} {p3[0]},{p3[1]} {p4[0]},{p4[1]}"
        
        data["radarAxisLabels"] = [
            {"x": 130, "y": 18, "anchor": "middle", "name": "战力"},
            {"x": 246, "y": 136, "anchor": "start", "name": "收藏"},
            {"x": 130, "y": 246, "anchor": "middle", "name": "捕捉" if "capture" in ev else "未知"},
            {"x": 14, "y": 136, "anchor": "end", "name": "推进"}
        ]
        
        data["radarValueBadges"] = [
            {"x": 105, "y": 38, "width": 50, "value": ev.get("strength", 0)},
            {"x": 190, "y": 116, "width": 50, "value": ev.get("collection", 0)},
            {"x": 105, "y": 186, "width": 50, "value": ev.get("capture", 0)},
            {"x": 20, "y": 116, "width": 50, "value": ev.get("progression", 0)}
        ]
        
        data["radarDots"] = [
            {"x": p1[0], "y": p1[1]}, {"x": p2[0], "y": p2[1]}, {"x": p3[0], "y": p3[1]}, {"x": p4[0], "y": p4[1]}
        ]
        
        # Recent battle
        if bl_res and bl_res.get("battles") and len(bl_res["battles"]) > 0:
            recent_battle = bl_res["battles"][0]
            data["hasBattleData"] = True
            res_class = "fail" if recent_battle.get("result") == 1 else "win"
            data["matchResult"] = res_class
            data["opponentName"] = recent_battle.get("enemy_nickname", "")
            data["opponentAvatarDisplay"] = recent_battle.get("enemy_avatar_url", "")
            data["leftTeamPets"] = [{"icon": p["pet_img_url"].replace("/image.png", "/icon.png")} for p in recent_battle.get("pet_base_info", [])]
            data["rightTeamPets"] = [{"icon": p["pet_img_url"].replace("/image.png", "/icon.png")} for p in recent_battle.get("enemy_pet_base_info", [])]

        img_url = await self.renderer.render_html("render/personal-card/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result("档案图像生成失败。")

    @filter.command("洛克战绩")
    async def rocom_battle_record(self, event: AstrMessageEvent, page: str = "1"):
        """查看对战战绩"""
        fw_token = await self._get_primary_token(event)
        if not fw_token:
            async for res in self._not_logged_in_hint(event):
                yield res
            return
            
        try:
            page_no = int(page)
        except ValueError:
            page_no = 1
        
        # 简易实现分页，因为没有 after_time 无法随机跳转，只能支持当前只拉一页或者固定N条
        # 此处按原文档只作为战绩展示，我们就展示最近一页
        user_identifier = self._get_user_identifier(event)
        results = await asyncio.gather(
            self.client.get_role(fw_token, user_identifier=user_identifier),
            self.client.get_battle_overview(fw_token, user_identifier=user_identifier),
            self.client.get_battle_list(fw_token, page_size=4, user_identifier=user_identifier),
            return_exceptions=True
        )
        role_res, bo_res, bl_res = results
        
        if isinstance(role_res, Exception) or not role_res or "role" not in role_res:
             err_msg = str(role_res) if isinstance(role_res, Exception) else (role_res.get("message") if isinstance(role_res, dict) else "未知错误")
             yield event.plain_result(f"获取战绩数据失败：{err_msg}")
             return
        
        role = role_res.get("role", {}) if role_res else {}
        bo = bo_res if isinstance(bo_res, dict) else {}
        
        parsed_battles = []
        if bl_res and bl_res.get("battles"):
            for b in bl_res["battles"]:
                bt_str = b.get("battle_time", "")
                try:
                    bt = datetime.fromisoformat(bt_str)
                    t_str = bt.strftime("%H:%M")
                    d_str = bt.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    t_str = "未知"
                    d_str = "未知"
                    
                res_class = "fail" if b.get("result") == 1 else "win"
                
                parsed_battles.append({
                    "time": t_str,
                    "date": d_str,
                    "result": res_class,
                    "leftName": b.get("nickname", ""),
                    "leftAvatar": b.get("avatar_url", ""),
                    "leftBadge": b.get("tier_url", ""),
                    "leftPets": [{"icon": p["pet_img_url"].replace("/image.png", "/icon.png")} for p in b.get("pet_base_info", [])],
                    "rightName": b.get("enemy_nickname", ""),
                    "rightAvatar": b.get("enemy_avatar_url", ""),
                    "rightBadge": b.get("enemy_tier_url", ""),
                    "rightPets": [{"icon": p["pet_img_url"].replace("/image.png", "/icon.png")} for p in b.get("enemy_pet_base_info", [])]
                })

        data = {
            "userName": role.get("name", "洛克"),
            "userAvatarDisplay": role.get("avatar_url", ""),
            "userLevel": role.get("level", 1),
            "userUid": role.get("id", ""),
            "tierBadgeUrl": bo.get("tier_icon_url", ""),
            "winRate": f"{bo.get('win_rate', 0)}%",
            "totalMatch": bo.get("total_match", 0),
            "currentPage": page_no,
            "totalPages": 1,
            "battles": parsed_battles,
            "commandHint": "💡 /洛克战绩 <页码> | 默认第1页",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin"
        }

        img_url = await self.renderer.render_html("render/record/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result("战绩图生成失败。")

    @filter.command("洛克背包", alias={"背包"})
    async def rocom_package(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        """查看个人洛克王国精灵背包"""
        fw_token = await self._get_primary_token(event)
        if not fw_token:
            async for res in self._not_logged_in_hint(event):
                yield res
            return
            
        # 智能解析参数
        category = "全部"
        page_no = 1
        
        cat_map = {
            "全部": 0, "了不起": 1, "异色": 2, "炫彩": 3,
            "全部精灵": 0, "了不起精灵": 1, "异色精灵": 2, "炫彩精灵": 3
        }

        # 参数乱序识别
        for arg in [arg1, arg2]:
            if not arg: continue
            # 处理数字（页码）
            if isinstance(arg, int) or (isinstance(arg, str) and arg.isdigit()):
                page_no = int(arg)
            # 处理分类
            elif isinstance(arg, str) and arg in cat_map:
                category = arg.replace("精灵", "")
        
        pet_subset = cat_map.get(category, cat_map.get(category+"精灵", 0))
        cat_name = f"{category}精灵"
        
        # 统一生成指令提示 (支持参数乱序)
        hint_str = "💡 /洛克背包 <全部/异色/了不起/炫彩> <页码> | 参数可交换位置，默认：全部第1页"
        
        user_identifier = self._get_user_identifier(event)
        role_res = await self.client.get_role(fw_token, user_identifier=user_identifier)
        pet_res = await self.client.get_pets(
            fw_token, pet_subset=pet_subset, page_no=page_no, page_size=10, user_identifier=user_identifier
        )
        
        if not role_res or "role" not in role_res or not pet_res or "pets" not in pet_res:
            err_msg = role_res.get("message") if isinstance(role_res, dict) and role_res.get("message") else (pet_res.get("message") if isinstance(pet_res, dict) else "接口异常")
            yield event.plain_result(f"获取背包数据失败：{err_msg}")
            return
        
        role = role_res.get("role", {})
        total_count = pet_res.get("total", 0)
        total_pages = max(1, (total_count + 9) // 10)
        
        pets_list = []
        for pet in pet_res.get("pets", []):
            element_icons = []
            for t in pet.get("pet_types_info", []):
                if t.get("name"):
                    element_icons.append({
                        "src": t.get("icon", ""),
                        "name": t.get("name", "")
                    })
            full_name = pet.get("pet_name", "")
            if "&" in full_name:
                name_parts = full_name.split("&", 1)
                p_name = name_parts[0]
                c_name = name_parts[1]
            else:
                p_name = full_name
                c_name = None
            
            pets_list.append({
                "name": p_name,
                "custom_name": c_name,
                "level": pet.get("pet_level", 1),
                "pet_img_url": pet.get("pet_img_url", ""),
                "elementIcons": element_icons,
                "badgeImage": ""
            })
            
        empty_count = max(0, 10 - len(pets_list))

        data = {
            "pageTitle": f"背包 - {cat_name}",
            "currentTab": cat_name,
            "totalCount": total_count,
            "accountLabel": role.get("id", ""),
            "userAvatar": role.get("avatar_url", ""),
            "defaultAvatar": "",
            "userName": role.get("name", "洛克"),
            "userLevel": role.get("level", 1),
            "userUid": role.get("id", ""),
            "tabs": [
                {"text": "全部精灵", "active": pet_subset == 0},
                {"text": "了不起精灵", "active": pet_subset == 1},
                {"text": "异色精灵", "active": pet_subset == 2},
                {"text": "炫彩精灵", "active": pet_subset == 3}
            ],
            "currentPage": page_no,
            "totalPages": total_pages,
            "pageSize": 10,
            "commandHint": hint_str,
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
            "fallbackPetImage": f"{{{{_res_path}}}}img/roco_icon.png",
            "pets": pets_list,
            "emptySlots": list(range(empty_count))
        }

        img_url = await self.renderer.render_html("render/package/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result("背包图生成失败。")
    @filter.command("洛克wiki")
    async def rocom_wiki(self, event: AstrMessageEvent, name: str = "焰火"):
        """查询精灵 wiki"""
        yield event.plain_result(
            f"洛克 wiki 接口当前已在新版后端文档中暂时关闭，插件侧已暂停调用。\n"
            f"你查询的是：{name}\n"
            f"待后端重新开放后会恢复该功能。"
        )

    @filter.command("洛克技能", alias={"技能 wiki"})
    async def rocom_skill(self, event: AstrMessageEvent, name: str = "圣光斩"):
        """查询技能 wiki"""
        yield event.plain_result(
            f"技能 wiki 接口当前已在新版后端文档中暂时关闭，插件侧已暂停调用。\n"
            f"你查询的是：{name}\n"
            f"待后端重新开放后会恢复该功能。"
        )

    @filter.command("远行商人")
    async def rocom_merchant(self, event: AstrMessageEvent):
        """查询远行商人"""
        img_url, _, products, round_info = await self._render_merchant_image(refresh=True)
        if img_url:
            yield event.image_result(img_url)
            return
        if not products:
            yield event.plain_result("当前远行商人暂无商品。")
            return
        names = "、".join([p["name"] for p in products])
        yield event.plain_result(
            f"远行商人当前商品：{names}\n当前轮次：{round_info['current'] or '未开放'}\n剩余：{round_info['countdown']}"
        )

    @filter.command("洛克玩家")
    async def rocom_player_search(self, event: AstrMessageEvent, uid: str = ""):
        """通过 ingame 接口搜索玩家"""
        uid = str(uid or "").strip()
        if not uid:
            yield event.plain_result("请提供玩家 UID。用法：/洛克玩家 <UID>")
            return
        res = await self.client.ingame_player_search(uid)
        if not res:
            yield event.plain_result(f"玩家搜索失败：{self.client.get_last_error()}")
            return
        data = self._build_player_search_render_data(res, uid)
        img_url = await self.renderer.render_html("render/player-search/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(self._format_json_payload(res))

    @filter.command("洛克商店")
    async def rocom_ingame_shop(self, event: AstrMessageEvent, shop_id: str = "3019"):
        """通过 ingame 接口查询商店信息"""
        shop_id = str(shop_id or "").strip()
        if not shop_id:
            yield event.plain_result("请提供商店 ID。用法：/洛克商店 <shop_id>")
            return
        res = await self.client.ingame_merchant_info(shop_id)
        if not res:
            yield event.plain_result(f"商店查询失败：{self.client.get_last_error()}")
            return
        data = self._build_shop_render_data(res, shop_id)
        img_url = await self.renderer.render_html("render/ingame-shop/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(self._format_json_payload(res))

    @filter.command("洛克好友关系")
    async def rocom_friendship(self, event: AstrMessageEvent, user_ids: str = ""):
        """查询好友关系"""
        user_ids = str(user_ids or "").strip()
        if not user_ids:
            yield event.plain_result("请提供要查询的用户 ID 列表。用法：/洛克好友关系 <id1,id2>")
            return
        fw_token = await self._get_primary_token(event)
        if not fw_token:
            async for res in self._not_logged_in_hint(event):
                yield res
            return
        res = await self.client.get_friendship(
            fw_token, user_ids, user_identifier=self._get_user_identifier(event)
        )
        if not res:
            yield event.plain_result(f"好友关系查询失败：{self.client.get_last_error()}")
            return
        data = self._build_friendship_render_data(res, user_ids)
        img_url = await self.renderer.render_html("render/friendship/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(self._format_json_payload(res))

    @filter.command("洛克学生")
    async def rocom_student(self, event: AstrMessageEvent, arg1: str = "101", arg2: str = "0"):
        """查询学生认证状态与学生活动福利"""
        fw_token = await self._get_primary_token(event)
        if not fw_token:
            async for res in self._not_logged_in_hint(event):
                yield res
            return
        try:
            area = int(arg1)
        except ValueError:
            area = 101
        try:
            account_type = int(arg2)
        except ValueError:
            account_type = 0
        user_identifier = self._get_user_identifier(event)
        state_res, perks_res = await asyncio.gather(
            self.client.get_student_state(
                fw_token,
                account_type=account_type,
                user_identifier=user_identifier,
            ),
            self.client.get_student_perks(
                fw_token,
                area=area,
                account_type=account_type,
                user_identifier=user_identifier,
            ),
        )
        if not state_res:
            yield event.plain_result(f"学生认证状态查询失败：{self.client.get_last_error()}")
            return
        if not perks_res:
            yield event.plain_result(f"学生活动福利查询失败：{self.client.get_last_error()}")
            return
        data = self._build_student_render_data(state_res, perks_res, area, account_type)
        img_url = await self.renderer.render_html("render/student/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(
                self._format_json_payload(
                    {"student_state": state_res, "student_perks": perks_res}
                )
            )

    @filter.command("订阅远行商人")
    async def subscribe_merchant(self, event: AstrMessageEvent, args: str = ""):
        """订阅远行商人商品提醒"""
        # 检查私聊订阅是否启用
        if event.is_private_chat() and not self.merchant_private_subscription_enabled:
            yield event.plain_result("个人私聊订阅功能已被禁用，请联系机器人管理员。")
            return
        
        # 检查权限：群聊需要管理员，私聊无权限限制
        if not event.is_private_chat() and not await self._is_group_admin(event):
            yield event.plain_result("仅当前群管理员可以配置远行商人订阅。")
            return
        
        # 从 event.message_str 中提取完整参数，避免 AstrBot 按空格拆分
        full_command = event.message_str or ""
        if "订阅远行商人" in full_command:
            args_text = full_command.split("订阅远行商人", 1)[1].strip()
        else:
            args_text = args.strip()
        
        mention, custom_items = self._parse_merchant_subscription_args(args_text)
        # custom_items 为 None 时使用默认配置，否则使用自定义商品
        selected_items = list(custom_items) if custom_items is not None else list(self.merchant_subscription_items)
        
        # 生成唯一订阅键：私聊用 user_id，群聊用 group_id
        if event.is_private_chat():
            subscription_key = f"private_{event.get_sender_id()}"
            subscription_type = "个人订阅"
        else:
            subscription_key = str(event.get_group_id())
            subscription_type = "群订阅"
        
        await self.merchant_sub_mgr.upsert_subscription(
            subscription_key,
            {
                "key": subscription_key,
                "type": subscription_type,
                "umo": event.unified_msg_origin,
                "mention_all": mention,
                "items": selected_items,
                "last_push_round": "",
                "last_matched_items": [],
                "updated_by": str(event.get_sender_id()),
            },
        )
        source_hint = "自定义商品" if custom_items is not None else "WebUI 默认商品"
        mention_hint = f"命中后{'会' if mention else '不会'}@全体" if not event.is_private_chat() else ""
        yield event.plain_result(
            f"已订阅远行商人，监听商品：{'、'.join(selected_items)}（{source_hint}）；{mention_hint}\n"
            f"订阅方式：/订阅远行商人 1 为 @全体（仅群聊），/订阅远行商人 0 为不@全体，"
            f"/订阅远行商人 1 国王球 棱镜球 为自定义商品，"
            f"/取消订阅远行商人 可关闭订阅。"
        )

    @filter.command("取消订阅远行商人")
    async def unsubscribe_merchant(self, event: AstrMessageEvent):
        """取消远行商人商品提醒"""
        # 检查私聊订阅是否启用（即使禁用，也应该允许取消已有的订阅）
        if event.is_private_chat() and not self.merchant_private_subscription_enabled:
            yield event.plain_result("个人私聊订阅功能已被禁用，但仍可取消已有订阅。")
        
        # 检查权限：群聊需要管理员，私聊无权限限制
        if not event.is_private_chat() and not await self._is_group_admin(event):
            yield event.plain_result("仅当前群管理员可以取消远行商人订阅。")
            return
        
        # 确定订阅键
        if event.is_private_chat():
            subscription_key = f"private_{event.get_sender_id()}"
            subscription_name = "你的个人"
        else:
            subscription_key = str(event.get_group_id())
            subscription_name = "本群"
        
        deleted = await self.merchant_sub_mgr.delete_subscription(subscription_key)
        if deleted:
            yield event.plain_result(f"已取消{subscription_name}远行商人订阅。")
        else:
            yield event.plain_result(f"{subscription_name}当前没有远行商人订阅。")
    @filter.command("洛克交换大厅", alias={"洛克大厅", "交换大厅"})
    async def rocom_exchange_hall(self, event: AstrMessageEvent, page: str = "1"):
        """查看交换大厅"""
        logger.info(f"收到交换大厅请求: page={page}")
        fw_token = await self._get_primary_token(event)
        if not fw_token:
            async for res in self._not_logged_in_hint(event):
                yield res
            return
        try:
            page_no = int(page)
        except:
            page_no = 1
        page_no = max(page_no, 1)
            
        try:
            res = await self.client.get_exchange_posters(
                fw_token, page_no=page_no, user_identifier=self._get_user_identifier(event)
            )
            if not res or "posters" not in res:
                err_msg = res.get("message") if isinstance(res, dict) else "数据结构异常"
                yield event.plain_result(f"获取交换大厅数据失败：{err_msg}")
                return
        except Exception as e:
            yield event.plain_result(f"获取交换大厅数据发生异常：{str(e)}")
            return
            
        posts = []
        for p in res.get("posters", []):
            u = p.get("user_info", {})
            posts.append({
                "userName": u.get("nickname", "未知"),
                "userLevel": u.get("level", 0),
                "isOnline": u.get("online_status") == 1,
                "avatarUrl": u.get("avatar_url", ""),
                "userId": u.get("role_id", "未知"),
                "wantText": p.get("want_item_name", "交友"),
                "provideItems": p.get("offer_items", []),
                "timeLabel": datetime.fromtimestamp(int(p.get("create_time", 0))).strftime("%m-%d %H:%M") if p.get("create_time") else "未知"
            })
            
        
        data = {
            "filterLabel": "全部",
            "posts": posts,
            "currentPage": page_no,
            "totalPages": res.get("total_pages", 1),
            "commandHint": "💡 /洛克交换大厅 <页码> | 默认第1页，支持别名：/洛克大厅 / /交换大厅",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin"
        }
        
        img_url = await self.renderer.render_html("render/exchange-hall/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result("交换大厅渲染失败。")

    @filter.command("查看阵容", alias={"阵容详情"})
    async def rocom_lineup_detail(self, event: AstrMessageEvent, lineup_id: str = None):
        """查看阵容详情"""
        if not lineup_id:
            yield event.plain_result("请提供阵容码。用法：/查看阵容 <阵容码>")
            return
        lineup_id = self._normalize_lineup_lookup_id(lineup_id)
        if not lineup_id:
            yield event.plain_result("请提供有效的阵容码。用法：/查看阵容 <阵容码>")
            return
            
        fw_token = await self._get_primary_token(event)
        if not fw_token:
            async for res in self._not_logged_in_hint(event):
                yield res
            return
        
        # 先获取阵容列表，找到对应 ID 的阵容
        user_identifier = self._get_user_identifier(event)
        res = await self.client.get_lineup_list(fw_token, page_no=1, user_identifier=user_identifier)
        if not res or "lineups" not in res:
            yield event.plain_result("获取阵容数据失败。")
            return
        
        # 查找匹配的阵容
        target_lineup = None
        for lineup in res.get("lineups", []):
            if self._is_target_lineup(lineup, lineup_id):
                target_lineup = lineup
                break
        
        # 如果当前页没有，尝试获取更多页
        if not target_lineup:
            total_pages = res.get("total_pages", 1)
            for page in range(2, min(total_pages + 1, 10)):  # 最多查找前 10 页
                res = await self.client.get_lineup_list(
                    fw_token, page_no=page, user_identifier=user_identifier
                )
                if res and "lineups" in res:
                    for lineup in res.get("lineups", []):
                        if self._is_target_lineup(lineup, lineup_id):
                            target_lineup = lineup
                            break
                if target_lineup:
                    break
        
        if not target_lineup:
            yield event.plain_result(f"未找到阵容码为 {lineup_id} 的阵容。")
            return
        
        # 处理阵容数据
        lineup_data = target_lineup.get("lineup", {})
        processed_pets = []
        for pet in lineup_data.get("pets", []):
            pet_data = {
                "pet_name": pet.get("pet_name", ""),
                "pet_img_url": pet.get("pet_img_url", ""),
                "skills": [
                    {
                        "icon": skill.get("skill_img_url", ""),
                        "name": skill.get("skill_name", ""),
                    }
                    for skill in pet.get("skills_info", [])
                ],
                "bloodline": pet.get("bloodline_info") is not None,
                "bloodline_icon": pet.get("bloodline_info", {}).get("icon", "") if pet.get("bloodline_info") else ""
            }
            processed_pets.append(pet_data)
        
        data = {
            "lineup": {
                "name": target_lineup.get("name", ""),
                "tags": target_lineup.get("tags", []),
                "pets": processed_pets,
                "author_name": target_lineup.get("author_name", ""),
                "author_avatar": target_lineup.get("author_avatar", ""),
                "likes": target_lineup.get("likes", 0),
                "lineup_code": lineup_id
            },
            "fallbackPetImage": f"{{{{_res_path}}}}img/roco_icon.png"
        }
        
        img_url = await self.renderer.render_html("render/lineup-detail/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result("阵容详情渲染失败。")

    @filter.command("洛克阵容", alias={"阵容"})
    async def rocom_lineup(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        """查看阵容推荐"""
        fw_token = await self._get_primary_token(event)
        if not fw_token:
            async for res in self._not_logged_in_hint(event):
                yield res
            return

        category = ""
        page_no = 1

        for arg in [arg1, arg2]:
            if not arg: continue
            if isinstance(arg, int) or (isinstance(arg, str) and arg.isdigit()):
                page_no = int(arg)
            else:
                category = arg

        hint_str = "💡 /洛克阵容 <分类> <页码> | 参数可交换位置，默认：热门推荐第1页"
        if category:
            hint_str = f"💡 当前分类：{category} | /洛克阵容 {category} 2 查看下一页"

        try:
            res = await self.client.get_lineup_list(
                fw_token, page_no=page_no, category=category, user_identifier=self._get_user_identifier(event)
            )
        except Exception as e:
            yield event.plain_result(f"获取阵容数据异常：{str(e)}")
            return

        if not res or "lineups" not in res:
            err_msg = res.get("message") if isinstance(res, dict) and res.get("message") else ""
            if "frameworkToken" in str(err_msg) or "无效" in str(err_msg):
                yield event.plain_result("【凭据过期】你的登录已过期，请重新使用 /洛克QQ登录 或 /洛克微信登录 绑定账号。")
            else:
                yield event.plain_result("获取阵容数据失败。")
            return
            
        # 处理阵容数据
        processed_lineups = []
        for lineup in res.get("lineups", []):
            processed_lineup = {
                "name": lineup.get("name", ""),
                "tags": lineup.get("tags", []),
                "pets": [],
                "author_name": lineup.get("author_name", ""),
                "author_avatar": lineup.get("author_avatar", ""),
                "likes": lineup.get("likes", 0),
                "lineup_code": str(lineup.get("id", ""))
            }
            
            # 处理每个精灵的数据
            lineup_data = lineup.get("lineup", {})
            for pet in lineup_data.get("pets", []):
                pet_data = {
                    "pet_name": pet.get("pet_name", ""),
                    "pet_img_url": pet.get("pet_img_url", ""),
                    "skills": [skill.get("skill_img_url", "") for skill in pet.get("skills_info", [])]
                }
                processed_lineup["pets"].append(pet_data)
            
            processed_lineups.append(processed_lineup)
            
        data = {
            "category": category or "热门推荐",
            "lineups": processed_lineups,
            "page_no": res.get("page_no", 1),
            "total_pages": res.get("total_pages", 1),
            "commandHint": hint_str,
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
            "fallbackPetImage": f"{{{{_res_path}}}}img/roco_icon.png"
        }
        
        img_url = await self.renderer.render_html("render/lineup/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result("阵容图生成失败。")

    @filter.command("洛克查蛋", alias={"查蛋"})
    async def rocom_search_eggs(self, event: AstrMessageEvent, arg1: str = None, arg2: str = None):
        """查询精灵蛋组（支持名称/身高/体重反查）"""
        if not arg1:
            yield event.plain_result(
                "🥚 查蛋用法：\n"
                "  /洛克查蛋 <精灵名>     — 查询蛋组及可配种精灵\n"
                "  /洛克查蛋 0.18 1.5     — 按身高(m)+体重(kg)反查（游戏原生单位）\n"
                "  /洛克查蛋 0.18m 1.5kg  — 带单位反查，身高统一使用 m\n"
                "  /洛克查蛋 0.18         — 仅按身高(m)反查\n"
                "  /洛克查蛋 身高0.18m 体重1.5kg — 带前缀和单位也行"
            )
            return

        # 解析：两个数字 = 前身高后体重；身高统一使用游戏原生 m，体重使用 kg。
        height, weight = None, None
        height_m, height_display = None, None
        name_parts = []

        def try_parse_num(s):
            try:
                return float(s)
            except (TypeError, ValueError):
                return None

        def parse_height_value(raw: str):
            text = str(raw or "").strip().lower()
            text = re.sub(r"^(身高|高度|h)", "", text, flags=re.IGNORECASE).strip()
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(m|米)?", text)
            if not match:
                return None
            value = float(match.group(1))
            unit = match.group(2) or ""
            if unit in {"m", "米"}:
                return value * 100, value, f"{value:g} m"
            return value * 100, value, f"{value:g} m"

        def parse_weight_value(raw: str):
            text = str(raw or "").strip().lower()
            text = re.sub(r"^(体重|重量|w)", "", text, flags=re.IGNORECASE).strip()
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(kg|千克|公斤)?", text)
            if not match:
                return None
            return float(match.group(1))

        nums_parsed = []
        for raw_arg in [arg1, arg2]:
            if raw_arg is None:
                continue
            arg = str(raw_arg)
            # 带前缀的显式写法
            if arg.startswith("身高") or arg.startswith("h") or arg.startswith("H"):
                parsed = parse_height_value(arg)
                if parsed is not None:
                    height, height_m, height_display = parsed
                    continue
            if arg.startswith("体重") or arg.startswith("w") or arg.startswith("W"):
                v = parse_weight_value(arg)
                if v is not None:
                    weight = v
                    continue
            # 纯数字/带单位：按顺序 前身高后体重
            height_candidate = parse_height_value(arg)
            weight_candidate = parse_weight_value(arg)
            if height_candidate is not None or weight_candidate is not None:
                nums_parsed.append((arg, height_candidate, weight_candidate))
            else:
                name_parts.append(arg)

        # 纯数字按位置分配
        if nums_parsed:
            if height is None and len(nums_parsed) >= 1:
                parsed = nums_parsed[0][1]
                if parsed is not None:
                    height, height_m, height_display = parsed
            if weight is None and len(nums_parsed) >= 2:
                parsed_weight = nums_parsed[1][2]
                if parsed_weight is not None:
                    weight = parsed_weight

        # 身高/体重反查模式
        if height is not None or weight is not None:
            use_backend_size_query = height is not None and weight is not None
            results = None
            data = None
            text_result = None

            if use_backend_size_query:
                results = await self.client.query_pet_size(height_m if height_m is not None else height / 100, weight)
                if results is not None:
                    data = self.egg_searcher.build_size_search_data_from_api(
                        height, weight, results
                    )
                    text_result = self.egg_searcher.build_size_search_text_from_api(
                        height, weight, results
                    )

            if data is None:
                results = self.egg_searcher.search_by_size(height=height, weight=weight)
                data = self.egg_searcher.build_size_search_data(
                    height, weight, results
                )
                text_result = self.egg_searcher.build_size_search_text(
                    height, weight, results
                )

            img_url = await self.renderer.render_html("render/searcheggs/size.html", data)
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(text_result)
            return

        # 名称查蛋模式
        name = " ".join(name_parts)
        if not name:
            yield event.plain_result("请输入精灵名称。用法：/洛克查蛋 <精灵名>")
            return

        sr = self.egg_searcher.search(name)

        if sr.match_type == SearchResult.MULTI:
            data = self.egg_searcher.build_candidates_render_data(name, sr.candidates)
            img_url = await self.renderer.render_html("render/searcheggs/candidates.html", data)
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(
                    self.egg_searcher.build_candidates_text(name, sr.candidates)
                )
            return
        if sr.match_type == SearchResult.NOT_FOUND:
            yield event.plain_result(f"❌ 未找到名为「{name}」的精灵，请检查名称后重试。")
            return

        pet = sr.pet
        hint_prefix = ""
        if sr.match_type == SearchResult.FUZZY:
            zh = pet.get("localized", {}).get("zh", {}).get("name", "")
            hint_prefix = f"🔍 模糊匹配到「{zh}」\n"

        try:
            data = self.egg_searcher.build_search_data(pet)
            data["commandHint"] = "💡 /洛克查蛋 <名称> | /洛克查蛋 身高0.25 体重1.5 | /洛克配种 <父> <母>"
            data["copyright"] = "AstrBot & WeGame Locke Kingdom Plugin"
            img_url = await self.renderer.render_html("render/searcheggs/index.html", data)
            if img_url:
                if hint_prefix:
                    yield event.plain_result(hint_prefix)
                yield event.image_result(img_url)
            else:
                msg = hint_prefix
                msg += f"🥚 {data['pet_name']} (#{data['pet_id']})\n"
                msg += f"属性：{data['type_label']}\n"
                msg += f"蛋组：{data['egg_groups_label']}\n"
                msg += f"可配种精灵数：{data['total_compatible']}\n"
                if data['is_undiscovered']:
                    msg += "⚠️ 该精灵属于「未发现」蛋组，无法配种。"
                yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"[Rocom] 查蛋渲染异常: {e}")
            yield event.plain_result(f"查蛋功能异常：{e}")

    @filter.command("洛克配种", alias={"配种"})
    async def rocom_breeding_check(self, event: AstrMessageEvent, name_a: str = None, name_b: str = None):
        """配种查询：双参数判断兼容性，单参数查询如何孵出目标精灵"""
        if not name_a:
            yield event.plain_result(
                "🥚 配种用法：\n"
                "  /洛克配种 <父体> <母体>  — 判断能否配种，孵蛋结果跟随母体\n"
                "  /洛克配种 <精灵名>       — 查询想要该精灵需要哪些父母组合"
            )
            return

        # 单参数模式：想要某精灵，查询怎么配
        if not name_b:
            sr = self.egg_searcher.search(name_a)
            if sr.match_type == SearchResult.MULTI:
                data = self.egg_searcher.build_candidates_render_data(name_a, sr.candidates)
                img_url = await self.renderer.render_html("render/searcheggs/candidates.html", data)
                if img_url:
                    yield event.image_result(img_url)
                else:
                    yield event.plain_result(
                        self.egg_searcher.build_candidates_text(name_a, sr.candidates)
                    )
                return
            if sr.match_type == SearchResult.NOT_FOUND:
                yield event.plain_result(f"❌ 未找到名为「{name_a}」的精灵。")
                return
            data = self.egg_searcher.build_want_pet_data(sr.pet)
            img_url = await self.renderer.render_html("render/searcheggs/want.html", data)
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(self.egg_searcher.build_want_pet_text(sr.pet))
            return

        # 双参数模式：父体 + 母体配种判定
        sr_a = self.egg_searcher.search(name_a)
        if sr_a.match_type == SearchResult.MULTI:
            data = self.egg_searcher.build_candidates_render_data(name_a, sr_a.candidates)
            img_url = await self.renderer.render_html("render/searcheggs/candidates.html", data)
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(
                    self.egg_searcher.build_candidates_text(name_a, sr_a.candidates)
                )
            return
        if sr_a.match_type == SearchResult.NOT_FOUND:
            yield event.plain_result(f"❌ 未找到名为「{name_a}」的精灵。")
            return

        sr_b = self.egg_searcher.search(name_b)
        if sr_b.match_type == SearchResult.MULTI:
            data = self.egg_searcher.build_candidates_render_data(name_b, sr_b.candidates)
            img_url = await self.renderer.render_html("render/searcheggs/candidates.html", data)
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(
                    self.egg_searcher.build_candidates_text(name_b, sr_b.candidates)
                )
            return
        if sr_b.match_type == SearchResult.NOT_FOUND:
            yield event.plain_result(f"❌ 未找到名为「{name_b}」的精灵。")
            return

        # 默认前父后母：father=a, mother=b，孵蛋结果跟随母体(b)
        father, mother = sr_a.pet, sr_b.pet
        try:
            data = self.egg_searcher.build_pair_data(mother, father)
            # 交换显示顺序：模板中 mother=母体(结果跟随), father=父体
            data["commandHint"] = "💡 默认前父后母，孵蛋结果跟随母体 | /洛克配种 <精灵名> 查怎么孵"
            data["copyright"] = "AstrBot & WeGame Locke Kingdom Plugin"
            img_url = await self.renderer.render_html("render/searcheggs/pair.html", data)
            if img_url:
                yield event.image_result(img_url)
            else:
                ma, fa = data["mother"]["name"], data["father"]["name"]
                if data["compatible"]:
                    shared = " / ".join(data["shared_egg_group_labels"])
                    yield event.plain_result(
                        f"✅ 父体 {fa} × 母体 {ma} 可以配种！\n"
                        f"共享蛋组：{shared}\n"
                        f"孵出结果：{ma}（跟随母体）\n"
                        f"孵化时长：{data['hatch_label']}"
                    )
                else:
                    yield event.plain_result(f"❌ {fa} × {ma} 无法配种。\n原因：{'；'.join(data['reasons'])}")
        except Exception as e:
            logger.error(f"[Rocom] 配种判定渲染异常: {e}")
            yield event.plain_result(f"配种判定功能异常：{e}")
