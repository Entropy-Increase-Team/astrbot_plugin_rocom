import os
import sys
import subprocess
import time
import base64
import tempfile
import asyncio
import re
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

# 确保插件目录在 Python 路径中
plugin_dir = os.path.dirname(os.path.abspath(__file__))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, Image

from .wiki.src.db_service import WikiDBService
from .wiki.src.color_extractor_vision import ColorExtractor
from .core.client import RocomClient
from .core.user import UserManager, MerchantSubscriptionManager
from .core.render import Renderer
from .core.egg_service import EggService, SearchResult

# 数据来源声明（CC BY-NC-SA 4.0 协议）
DATA_SOURCE_NOTICE = "\n\n---\n📚 数据来源: [BiliGame 洛克王国 WIKI](https://wiki.biligame.com/rocom/) | CC BY-NC-SA 4.0"

# Wiki功能导入 - 使用包装器
try:
    from .wiki.wiki_wrapper import WikiWrapper
except ImportError as e:
    logger.warning(f"⚠️ Wiki模块导入失败: {e}")
    WikiWrapper = None

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
        
        # 初始化Wiki功能（整合自 InMain 的 astrbot_plugin_roco_world_wiki_search）
        if WIKI_MODULES_LOADED:
            try:
                self.wiki_mixin = RocoWorldWikiMixin.__new__(RocoWorldWikiMixin)
                self.wiki_mixin.context = self.context
                self.wiki_mixin.config = self.config
                self.wiki_mixin.__init__(self.context, self.config)
                logger.info("✅ Wiki功能初始化成功")
            except Exception as e:
                logger.error(f"❌ Wiki功能初始化失败: {e}")
                self.wiki_mixin = None
        else:
            self.wiki_mixin = None
            logger.warning("⚠️ Wiki模块未加载，Wiki功能不可用")

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
        # Wiki功能无需单独清理（数据库连接由 db_service 管理）

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


# ==================== Wiki 百科查询功能（整合自 InMain 的 astrbot_plugin_roco_world_wiki_search）====================
# 数据来源声明（CC BY-NC-SA 4.0 协议）
DATA_SOURCE_NOTICE = "\n\n---\n📚 数据来源: [BiliGame 洛克王国 WIKI](https://wiki.biligame.com/rocom/) | CC BY-NC-SA 4.0"

# 确保 wiki/src 在 Python 路径中
wiki_src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki", "src")
if wiki_src_path not in sys.path:
    sys.path.insert(0, wiki_src_path)

try:
    from db_service import WikiDBService
    from color_extractor_vision import ColorExtractor
    WIKI_MODULES_LOADED = True
except ImportError as e:
    logger.warning(f"⚠️ Wiki模块导入失败: {e}")
    WIKI_MODULES_LOADED = False
    WikiDBService = None
    ColorExtractor = None


class RocoWorldWikiMixin:
    """洛克王国 Wiki 插件功能混入类"""

    def _resolve_wiki_path(self, relative_path: str) -> str:
        """
        解析Wiki资源路径（跨平台兼容）
        
        Args:
            relative_path: 相对路径（如 output/images/pets/xxx.png）
            
        Returns:
            绝对路径，统一使用 / 分隔符
        """
        if not relative_path:
            return ''
        
        # 清理路径前缀
        if relative_path.startswith('./') or relative_path.startswith('.\\'):
            relative_path = relative_path[2:]
        
        # 如果是绝对路径，直接返回
        if os.path.isabs(relative_path):
            return relative_path.replace('\\', '/')
        
        # 获取wiki目录
        wiki_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wiki')
        
        # 使用 / 拼接，确保跨平台兼容
        full_path = wiki_dir.rstrip('/\\') + '/' + relative_path.replace('\\', '/')
        return full_path

    def _init_color_extractor(self):
        """
        初始化颜色提取器（从 AstrBot provider 配置中获取或手动填写）

        Returns:
            ColorExtractor 实例或 None
        """
        # 检查是否使用手动配置
        manual_api_key = self.config.get("manual_vision_api_key", "").strip()
        manual_base_url = self.config.get("manual_vision_base_url", "").strip()
        manual_model_id = self.config.get("manual_vision_model_id", "").strip()

        # 如果手动配置完整，直接使用
        if manual_api_key and manual_base_url and manual_model_id:
            logger.info("✅ 使用手动配置的视觉模型")
            logger.info(f"   - base_url: {manual_base_url}")
            logger.info(f"   - model: {manual_model_id}")

            # 创建适配器，使用手动配置
            class ManualProviderAdapter:
                """手动配置 Provider 适配器，用于颜色提取"""
                def __init__(self, context, api_key, base_url, model_id):
                    self.context = context
                    self.api_key = api_key
                    self.base_url = base_url
                    self.model_id = model_id

                async def extract_main_colors_async(self, image_path: str, top_n: int = 2):
                    """使用手动配置的 API 进行颜色识别（异步版本）"""
                    import base64
                    import aiohttp

                    try:
                        if not os.path.exists(image_path):
                            logger.error(f"❌ 图片不存在: {image_path}")
                            return None

                        # 读取图片并转为base64
                        with open(image_path, 'rb') as f:
                            image_data = base64.b64encode(f.read()).decode('utf-8')

                        # 构建提示词
                        if top_n == 1:
                            prompt = (
                                "请分析这张图片的主色调是什么颜色？\n"
                                "要求：\n"
                                "1. 只输出一个中文颜色名称（如：红、橙、黄、绿、蓝、紫、粉、白、黑、棕、灰）\n"
                                "2. 不要有任何其他文字、标点或解释\n"
                                "3. 如果图片有多种颜色，选择占比最大的那个"
                            )
                        else:
                            prompt = (
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

                        # 调用 OpenAI 兼容 API
                        headers = {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.api_key}"
                        }

                        payload = {
                            "model": self.model_id,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": prompt},
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": f"data:image/png;base64,{image_data}"
                                            }
                                        }
                                    ]
                                }
                            ],
                            "max_tokens": 100,
                            "temperature": 0.1
                        }

                        # 发送请求
                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                f"{self.base_url}/chat/completions",
                                headers=headers,
                                json=payload,
                                timeout=aiohttp.ClientTimeout(total=30)
                            ) as response:
                                if response.status != 200:
                                    error_text = await response.text()
                                    logger.error(f"❌ API 请求失败: {response.status} - {error_text}")
                                    return None

                                result = await response.json()
                                response_text = result['choices'][0]['message']['content'].strip()

                        # 解析响应
                        lines = response_text.split('\n')

                        valid_colors = []
                        for line in lines:
                            color = line.strip()
                            if len(color) == 1 and color in ['红', '橙', '黄', '绿', '蓝', '紫', '粉', '白', '黑', '棕', '灰']:
                                if color not in valid_colors:
                                    valid_colors.append(color)

                        if not valid_colors:
                            logger.warning(f"⚠️ 无法解析颜色: {response_text}")
                            return None

                        valid_colors = valid_colors[:top_n]

                        return {
                            'main_color': valid_colors[0] if len(valid_colors) > 0 else None,
                            'secondary_color': valid_colors[1] if len(valid_colors) > 1 else None,
                            'colors': valid_colors,
                            'rgb_values': [],
                            'color_ratios': []
                        }

                    except Exception as e:
                        logger.error(f"❌ 颜色提取错误: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                        return None

            return ManualProviderAdapter(self.context, manual_api_key, manual_base_url, manual_model_id)

        # 如果没有手动配置，尝试从 AstrBot provider 配置中获取
        vision_model_config = self.config.get("vision_model_config", "")

        if not vision_model_config or not vision_model_config.strip():
            logger.warning("⚠️ 未配置视觉模型，颜色识别功能不可用")
            logger.warning("💡 请在 WebUI 的插件配置中选择视觉模型")
            return None

        try:
            provider_manager = getattr(self.context, 'provider_manager', None)
            if not provider_manager:
                logger.error("❌ 无法访问 provider_manager")
                return None

            # 获取所有可用的 providers
            providers = getattr(provider_manager, 'get_insts', lambda: [])()

            # 调试：列出所有可用的 providers
            if providers:
                available_ids = []
                for p in providers:
                    # 尝试多种可能的属性名
                    pid = (getattr(p, 'id', None) or
                           getattr(p, 'provider_id', None) or
                           getattr(p, 'name', None) or
                           getattr(p, 'model_name', None) or
                           getattr(p, 'model', None) or
                           str(type(p).__name__))
                    pname = getattr(p, 'name', '') or getattr(p, 'model_name', '') or pid
                    available_ids.append(f"{pid} ({pname})")

                    # 详细调试：打印 provider 的所有属性
                    logger.debug(f"Provider 对象类型: {type(p).__name__}")
                    logger.debug(f"Provider 属性: id={getattr(p, 'id', 'N/A')}, provider_id={getattr(p, 'provider_id', 'N/A')}, name={getattr(p, 'name', 'N/A')}")

                logger.info(f"📋 当前可用的 Providers: {', '.join(available_ids)}")
            else:
                logger.warning("⚠️ 没有找到任何已配置的 Provider")

            # 查找匹配的 provider
            selected_provider = None
            for provider in providers:
                # 尝试多种可能的属性名
                provider_id = (getattr(provider, 'id', None) or
                              getattr(provider, 'provider_id', None) or
                              getattr(provider, 'name', None) or
                              getattr(provider, 'model_name', None) or
                              getattr(provider, 'model', None))

                if not provider_id:
                    continue

                # 精确匹配
                if provider_id == vision_model_config:
                    selected_provider = provider
                    break

                # 模糊匹配：去除前缀后匹配（处理 ollama_amd/ 等前缀）
                if '/' in vision_model_config:
                    config_model_name = vision_model_config.split('/')[-1]
                    if provider_id == config_model_name or provider_id.endswith('/' + config_model_name):
                        logger.info(f"💡 通过模糊匹配找到 Provider: {provider_id}")
                        selected_provider = provider
                        break

            if not selected_provider:
                logger.error(f"❌ 未找到 provider '{vision_model_config}'")
                logger.error("💡 请检查该模型是否已在 AstrBot 中正确配置")
                # 使用与前面相同的逻辑获取 IDs
                available_ids = []
                for p in providers:
                    pid = (getattr(p, 'id', None) or
                           getattr(p, 'provider_id', None) or
                           getattr(p, 'name', None) or
                           getattr(p, 'model_name', None) or
                           getattr(p, 'model', None) or
                           str(type(p).__name__))
                    available_ids.append(pid)
                logger.error(f"💡 可用的 provider IDs: {available_ids}")
                return None

            # 从 provider 配置中提取 API 信息
            # AstrBot v4.x 的 Provider 对象有 provider_config 属性（字典）
            provider_config_dict = getattr(selected_provider, 'provider_config', {})

            if isinstance(provider_config_dict, dict):
                # 从 provider_config 字典中获取
                raw_api_key = provider_config_dict.get('key')
                if isinstance(raw_api_key, list):
                    vision_api_key = raw_api_key[0] if raw_api_key else ''
                else:
                    vision_api_key = raw_api_key or ''

                vision_base_url = (provider_config_dict.get('base_url') or
                                  provider_config_dict.get('api_base') or
                                  provider_config_dict.get('endpoint') or
                                  '')

                vision_model = (provider_config_dict.get('model') or
                               provider_config_dict.get('model_name') or
                               provider_config_dict.get('model_id') or
                               provider_id or
                               '')
            else:
                # 备用方案：尝试直接访问属性
                vision_api_key = (getattr(selected_provider, 'api_key', None) or
                                 getattr(selected_provider, 'token', None) or
                                 getattr(selected_provider, 'key', None) or
                                 '')

                vision_base_url = (getattr(selected_provider, 'base_url', None) or
                                  getattr(selected_provider, 'api_base', None) or
                                  getattr(selected_provider, 'endpoint', None) or
                                  '')

                vision_model = (getattr(selected_provider, 'model_name', None) or
                               getattr(selected_provider, 'model', None) or
                               getattr(selected_provider, 'model_id', None) or
                               provider_id or
                               '')

            # 调试日志：打印所有可能的属性
            logger.debug(f"Provider 对象类型: {type(selected_provider).__name__}")
            logger.debug(f"尝试获取的属性:")
            logger.debug(f"  - api_key/token/key: {vision_api_key or '✗'}")
            logger.debug(f"  - base_url/api_base/endpoint: {vision_base_url or '✗'}")
            logger.debug(f"  - model_name/model/model_id: {vision_model or '✗'}")

            # 如果还是为空，尝试直接访问 __dict__ 或 config
            if not vision_api_key or not vision_base_url or not vision_model:
                logger.debug("尝试从 provider.__dict__ 或 provider.config 获取...")
                provider_dict = getattr(selected_provider, '__dict__', {})
                provider_config = getattr(selected_provider, 'config', {})

                if not vision_api_key:
                    vision_api_key = (provider_dict.get('api_key') or
                                     provider_dict.get('token') or
                                     provider_config.get('api_key') or
                                     provider_config.get('token') or
                                     '')

                if not vision_base_url:
                    vision_base_url = (provider_dict.get('base_url') or
                                      provider_dict.get('api_base') or
                                      provider_config.get('base_url') or
                                      provider_config.get('api_base') or
                                      '')

                if not vision_model:
                    vision_model = (provider_dict.get('model_name') or
                                   provider_dict.get('model') or
                                   provider_config.get('model_name') or
                                   provider_config.get('model') or
                                   provider_id or
                                   '')

            # 最后的备用方案：如果是 Ollama 模型，使用默认配置
            if not vision_base_url and ('ollama' in provider_id.lower() or 'qwen' in provider_id.lower()):
                logger.info(f"💡 检测到可能是 Ollama 模型，使用默认配置")
                vision_base_url = "http://192.168.31.15:11436/v1"
                vision_api_key = vision_api_key or "ollama"  # Ollama 不需要 API Key，但需要一个占位符
                vision_model = vision_model or provider_id
                logger.info(f"   - base_url: {vision_base_url}")
                logger.info(f"   - model: {vision_model}")

            # 如果还是获取不到，尝试从 AstrBot 配置文件中读取
            if not vision_base_url:
                logger.warning("⚠️ 无法从 Provider 对象获取配置，尝试从配置文件读取...")
                try:
                    import json
                    config_file = os.path.join(plugin_dir, '..', 'config', 'abconf_412865ab-2550-4266-89be-e9c00a76752b.json')
                    if os.path.exists(config_file):
                        with open(config_file, 'r', encoding='utf-8-sig') as f:
                            astrbot_config = json.load(f)

                        # 查找 providers 配置
                        providers_config = astrbot_config.get('provider_settings', {}).get('providers', [])
                        for prov_cfg in providers_config:
                            if prov_cfg.get('id') == provider_id or prov_cfg.get('name') == provider_id:
                                vision_api_key = prov_cfg.get('key', [''])[0] if isinstance(prov_cfg.get('key'), list) else prov_cfg.get('key', '')
                                vision_base_url = prov_cfg.get('base_url', '')
                                vision_model = prov_cfg.get('model', '') or provider_id
                                logger.info(f"✅ 从配置文件读取成功")
                                logger.info(f"   - base_url: {vision_base_url}")
                                logger.info(f"   - model: {vision_model}")
                                break
                except Exception as e:
                    logger.debug(f"从配置文件读取失败: {e}")

            if not vision_api_key or not vision_base_url or not vision_model:
                logger.warning(f"⚠️ Provider '{vision_model_config}' 配置不完整")
                logger.warning(f"   - api_key: {'✓' if vision_api_key else '✗'}")
                logger.warning(f"   - base_url: {'✓' if vision_base_url else '✗'}")
                logger.warning(f"   - model: {'✓' if vision_model else '✗'}")
                return None

            logger.info(f"✅ 使用 AstrBot 配置的视觉模型: {vision_model_config}")

            # 调试：打印 provider_manager.inst_map 的所有 key
            if hasattr(self.context.provider_manager, 'inst_map'):
                available_keys = list(self.context.provider_manager.inst_map.keys())
                logger.info(f"📋 provider_manager.inst_map keys: {available_keys}")

            # 创建一个适配器，使用 context.llm_generate() 调用视觉模型
            class AstrBotProviderAdapter:
                """AstrBot Provider 适配器，用于颜色提取"""
                def __init__(self, context, provider_id, model_name):
                    self.context = context
                    self.provider_id = provider_id
                    self.model_name = model_name  # 保存实际的模型名称

                async def extract_main_colors_async(self, image_path: str, top_n: int = 2):
                    """使用 AstrBot Provider 进行颜色识别（异步版本）"""
                    import base64

                    try:
                        if not os.path.exists(image_path):
                            logger.error(f"❌ 图片不存在: {image_path}")
                            return None

                        # 读取图片并转为base64
                        with open(image_path, 'rb') as f:
                            image_data = base64.b64encode(f.read()).decode('utf-8')

                        # 构建提示词
                        if top_n == 1:
                            prompt = (
                                "请分析这张图片的主色调是什么颜色？\n"
                                "要求：\n"
                                "1. 只输出一个中文颜色名称（如：红、橙、黄、绿、蓝、紫、粉、白、黑、棕、灰）\n"
                                "2. 不要有任何其他文字、标点或解释\n"
                                "3. 如果图片有多种颜色，选择占比最大的那个"
                            )
                        else:
                            prompt = (
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

                        # 调用 AstrBot context.llm_generate()
                        response = await self.context.llm_generate(
                            chat_provider_id=self.provider_id,
                            prompt=prompt,
                            image_urls=[f"data:image/png;base64,{image_data}"],
                            model=self.model_name  # 使用实际的模型名称
                        )

                        if not response or not response.completion_text:
                            logger.warning("⚠️ Provider 返回空响应")
                            return None

                        # 解析响应
                        response_text = response.completion_text.strip()
                        lines = response_text.split('\n')

                        valid_colors = []
                        for line in lines:
                            color = line.strip()
                            if len(color) == 1 and color in ['红', '橙', '黄', '绿', '蓝', '紫', '粉', '白', '黑', '棕', '灰']:
                                if color not in valid_colors:
                                    valid_colors.append(color)

                        if not valid_colors:
                            logger.warning(f"⚠️ 无法解析颜色: {response_text}")
                            return None

                        valid_colors = valid_colors[:top_n]

                        return {
                            'main_color': valid_colors[0] if len(valid_colors) > 0 else None,
                            'secondary_color': valid_colors[1] if len(valid_colors) > 1 else None,
                            'colors': valid_colors,
                            'rgb_values': [],
                            'color_ratios': []
                        }

                    except Exception as e:
                        logger.error(f"❌ 颜色提取错误: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                        return None

            return AstrBotProviderAdapter(self.context, provider_id, vision_model)
        except Exception as e:
            logger.error(f"❌ 初始化颜色提取器失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _get_db_path(self):
        """
        获取数据库的绝对路径

        Returns:
            str: 数据库的绝对路径
        """
        db_path = self.config.get("db_path", "./wiki-local.db")
        if not os.path.isabs(db_path):
            db_path = os.path.join(plugin_dir, db_path)
        return db_path

    def _extract_image_query(self, query_content: str) -> tuple:
        """
        检测并提取图片检索请求

        Args:
            query_content: 查询内容

        Returns:
            (is_image_query, clean_query): 是否是图片检索，清理后的查询词
        """
        # 检查是否包含图片关键词
        for keyword in self.image_keywords:
            if keyword in query_content:
                # 移除图片关键词，得到实际要查询的内容
                clean_query = query_content.replace(keyword, '').strip()
                if clean_query:
                    return True, clean_query

        return False, query_content

    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context, config or {})
        self.config = config or {}

        # 初始化数据库服务
        db_path = self.config.get("wiki_db_path", "./wiki/wiki-local.db")
        try:
            self.db_service = WikiDBService(db_path)
            logger.info(f"✅ 数据库服务初始化成功: {db_path}")
        except Exception as e:
            logger.error(f"❌ 数据库服务初始化失败: {e}")
            logger.error("💡 请检查数据库路径是否正确，或运行 setup_db.py 初始化数据库")
            self.db_service = None

        # 获取配置项
        self.search_limit = max(self.config.get("wiki_search_limit", 5), 1) or 5
        self.enable_fuzzy_search = self.config.get("wiki_enable_fuzzy_search", True)
        self.response_style = self.config.get("wiki_response_style", "简洁")
        self.trigger_keywords = self.config.get("wiki_trigger_keywords", ["洛克王国", "查询", "百科"])
        self.query_command = self.config.get("wiki_query_command", "查询")
        self.image_keywords = self.config.get("wiki_image_keywords", ["图片", "图", "头像", "立绘"])

        # 分页配置
        self.page_size = max(5, min(30, self.config.get("wiki_page_size", 10)))

        # 颜色提取器（视觉模型）- 懒加载
        self._color_extractor = None

        # 会话状态管理（用于翻页功能）
        self.session_states = {}
        self.session_timeout = 300  # 5分钟

        logger.info(f"🔥 洛克王国 Wiki 插件已加载")
        logger.info(f"   - 响应风格: {self.response_style}")
        logger.info(f"   - 模糊搜索: {'开启' if self.enable_fuzzy_search else '关闭'}")
        logger.info(f"   - 搜索限制: {self.search_limit}")
        logger.info(f"   - 触发关键词: {', '.join(self.trigger_keywords)}")
        logger.info(f"   - 查询指令: /{self.query_command}")
        logger.info(f"   - 分页大小: {self.page_size} 条/页")
        logger.info(f"   - 图片检索词: {', '.join(self.image_keywords)}")

        # 检查视觉模型配置方式
        manual_api_key = self.config.get("wiki_manual_vision_api_key", "").strip()
        manual_base_url = self.config.get("wiki_manual_vision_base_url", "").strip()
        manual_model_id = self.config.get("wiki_manual_vision_model_id", "").strip()
        vision_model_config = self.config.get("wiki_vision_model_config", "")

        if manual_api_key and manual_base_url and manual_model_id:
            logger.info(f"   - 视觉模型: 手动配置 ({manual_model_id})")
        elif vision_model_config:
            logger.info(f"   - 视觉模型: AstrBot Provider ({vision_model_config})")
        else:
            logger.warning(f"   - 视觉模型: 未配置（颜色识别功能不可用）")

    @property
    def color_extractor(self):
        """颜色提取器（懒加载）"""
        if self._color_extractor is None:
            self._color_extractor = self._init_color_extractor()
        return self._color_extractor

    async def _on_config_update(self, config: Dict[str, Any]):
        """
        配置更新时的回调函数（支持热重载）

        Args:
            config: 新的配置字典
        """
        try:
            logger.info("🔄 检测到配置更新，正在应用...")

            # 更新配置
            self.config = config or {}

            # 检查数据库路径是否变化
            new_db_path = self.config.get("db_path", "./wiki-local.db")
            old_db_path = getattr(self, '_current_db_path', None)

            if new_db_path != old_db_path:
                logger.info(f"📁 数据库路径变更: {old_db_path} -> {new_db_path}")
                try:
                    self.db_service = WikiDBService(new_db_path)
                    logger.info(f"✅ 数据库服务重新初始化成功")
                except Exception as e:
                    logger.error(f"❌ 数据库服务重新初始化失败: {e}")
                    # 保持旧的服务

            self._current_db_path = new_db_path

            # 更新其他配置项
            self.search_limit = max(self.config.get("search_limit", 5), 1) or 5
            self.enable_fuzzy_search = self.config.get("enable_fuzzy_search", True)
            self.response_style = self.config.get("response_style", "简洁")
            self.trigger_keywords = self.config.get("trigger_keywords", ["洛克王国", "查询", "百科"])
            self.query_command = self.config.get("query_command", "查询")
            self.image_keywords = self.config.get("image_keywords", ["图片", "图", "头像", "立绘"])

            # 分页配置
            self.page_size = max(5, min(30, self.config.get("page_size", 10)))

            # 重置颜色提取器（下次使用时会重新初始化）
            self._color_extractor = None

            # 检查视觉模型配置方式
            manual_api_key = self.config.get("manual_vision_api_key", "").strip()
            manual_base_url = self.config.get("manual_vision_base_url", "").strip()
            manual_model_id = self.config.get("manual_vision_model_id", "").strip()
            vision_model_config = self.config.get("vision_model_config", "")

            if manual_api_key and manual_base_url and manual_model_id:
                logger.info(f"   - 视觉模型: 手动配置 ({manual_model_id})")
            elif vision_model_config:
                logger.info(f"   - 视觉模型: AstrBot Provider ({vision_model_config})")
            else:
                logger.warning(f"   - 视觉模型: 未配置（颜色识别功能不可用）")

            logger.info("✅ 配置更新成功")
            logger.info(f"   - 响应风格: {self.response_style}")
            logger.info(f"   - 模糊搜索: {'开启' if self.enable_fuzzy_search else '关闭'}")
            logger.info(f"   - 搜索限制: {self.search_limit}")
            logger.info(f"   - 触发关键词: {', '.join(self.trigger_keywords)}")
            logger.info(f"   - 查询指令: /{self.query_command}")
            logger.info(f"   - 分页大小: {self.page_size} 条/页")
            logger.info(f"   - 图片检索词: {', '.join(self.image_keywords)}")

        except Exception as e:
            logger.error(f"❌ 配置更新失败: {e}", exc_info=True)

    def _parse_list_field(self, field_value: str) -> list:
        """
        解析数据库中的列表字段（支持JSON和Python列表格式）

        Args:
            field_value: 数据库中的字符串值

        Returns:
            解析后的列表，失败返回空列表
        """
        import json
        import ast

        if not field_value or field_value == '[]':
            return []

        try:
            # 先尝试用JSON解析
            return json.loads(field_value)
        except:
            pass

        try:
            # 如果JSON失败，尝试用ast.literal_eval解析Python列表格式
            result = ast.literal_eval(field_value)
            if isinstance(result, list):
                return result
        except:
            pass

        # 最后尝试分号分隔的格式
        if ';' in field_value:
            return [s.strip() for s in field_value.split(';') if s.strip()]

        return []

    def _format_pet_response(self, pet_data: Dict[str, Any]) -> str:
        """
        根据配置风格格式化宠物信息

        Args:
            pet_data: 宠物数据字典

        Returns:
            格式化后的文本
        """
        name = pet_data.get('name', '未知')
        element = pet_data.get('element', '未知')
        hp = pet_data.get('hp', 0)
        ability = pet_data.get('ability', '无')
        ability_desc = pet_data.get('ability_desc', '')
        skills = pet_data.get('skills', '')
        description = pet_data.get('description', '')
        size = pet_data.get('size', '')
        weight = pet_data.get('weight', '')
        distribution = pet_data.get('distribution', '')
        stage = pet_data.get('stage', '')
        pet_type = pet_data.get('type', '')
        form = pet_data.get('form', '')
        initial_stage_name = pet_data.get('initial_stage_name', '')
        has_alt_color = pet_data.get('has_alt_color', '')
        update_version = pet_data.get('update_version', '')
        quest_tasks = pet_data.get('quest_tasks', '')
        quest_skill_stones = pet_data.get('quest_skill_stones', '')
        bloodline_skills = pet_data.get('bloodline_skills', '')
        learnable_skill_stones = pet_data.get('learnable_skill_stones', '')
        physical_attack = pet_data.get('physical_attack', 0)
        magic_attack = pet_data.get('magic_attack', 0)
        physical_defense = pet_data.get('physical_defense', 0)
        magic_defense = pet_data.get('magic_defense', 0)
        speed = pet_data.get('speed', 0)

        # 检查是否数据缺失（六维全为0）
        is_data_missing = (hp == 0 and physical_attack == 0 and magic_attack == 0
                          and physical_defense == 0 and magic_defense == 0 and speed == 0)

        if self.response_style == "详细":
            # 详细模式：显示所有信息
            import json

            response = f"🐾 **{name}**\n"
            response += "━━━━━━━━━━━━━━\n"

            # 基本信息
            info_parts = []
            if form and form != '原始形态':
                info_parts.append(f"形态: {form}")
            if stage:
                info_parts.append(f"阶段: {stage}")
            if pet_type:
                info_parts.append(f"类型: {pet_type}")
            if initial_stage_name:
                info_parts.append(f"初始: {initial_stage_name}")
            if has_alt_color and has_alt_color == '是':
                info_parts.append("✨ 有异色")
            if update_version:
                info_parts.append(f"版本: {update_version}")

            if info_parts:
                response += f"📋 {' | '.join(info_parts)}\n"

            response += f"📊 属性: {element}\n"

            # 六维属性
            if is_data_missing:
                response += f"⚠️ **注意:** 该宠物为特殊形态，暂无战斗数据\n"
            else:
                response += f"⚔️ 六维:\n"
                response += f"  ❤️ HP: {hp}\n"
                response += f"  💪 物攻: {physical_attack}\n"
                response += f"  🔮 魔攻: {magic_attack}\n"
                response += f"  🛡️ 物防: {physical_defense}\n"
                response += f"  ✨ 魔防: {magic_defense}\n"
                response += f"  ⚡ 速度: {speed}\n"

            # 特性
            if ability and ability != '无':
                response += f"✨ 特性: {ability}\n"
                if ability_desc:
                    response += f"  > {ability_desc}\n"

            # 体型信息
            body_info = []
            if size:
                body_info.append(f"体型: {size}m")
            if weight:
                body_info.append(f"体重: {weight}kg")
            if distribution:
                body_info.append(f"分布: {distribution}")
            if body_info:
                response += f"📏 {' | '.join(body_info)}\n"

            # 简介
            if description:
                response += f"\n📝 **简介:**\n{description}\n"

            # 图鉴课题
            if quest_tasks:
                tasks = self._parse_list_field(quest_tasks)
                if tasks and len(tasks) > 0:
                    response += f"\n📚 **图鉴课题** ({len(tasks)}个):\n"
                    for i, task in enumerate(tasks, 1):
                        response += f"  {i}. {task}\n"

            # 课题技能石
            if quest_skill_stones:
                stones = self._parse_list_field(quest_skill_stones)
                if stones and len(stones) > 0:
                    response += f"\n💎 **课题技能石** ({len(stones)}个):\n"
                    response += "  • " + " | ".join(stones) + "\n"

            # 技能列表
            if skills:
                skills_list = self._parse_list_field(skills)
                if skills_list and len(skills_list) > 0:
                    response += f"\n🎯 **技能列表** ({len(skills_list)}个):\n"
                    # 每行显示3个技能
                    for i in range(0, len(skills_list), 3):
                        chunk = skills_list[i:i+3]
                        response += "  • " + " | ".join(chunk) + "\n"

            # 血脉技能
            if bloodline_skills:
                bloodline_list = self._parse_list_field(bloodline_skills)
                if bloodline_list and len(bloodline_list) > 0:
                    response += f"\n🧬 **血脉技能** ({len(bloodline_list)}个):\n"
                    for i in range(0, len(bloodline_list), 3):
                        chunk = bloodline_list[i:i+3]
                        response += "  • " + " | ".join(chunk) + "\n"

            # 可学技能石
            if learnable_skill_stones:
                stone_list = self._parse_list_field(learnable_skill_stones)
                if stone_list and len(stone_list) > 0:
                    response += f"\n💎 **可学技能石** ({len(stone_list)}个):\n"
                    for i in range(0, len(stone_list), 4):
                        chunk = stone_list[i:i+4]
                        response += "  • " + " | ".join(chunk) + "\n"

            return response

        elif self.response_style == "卡片式":
            # 卡片式：中等信息量
            element_emoji = {
                '火': '🔥', '水': '💧', '草': '🌿', '电': '⚡',
                '冰': '❄️', '龙': '🐉', '光': '✨', '暗': '🌑'
            }.get(element.split('+')[0] if '+' in element else element, '⭐')

            response = f"{element_emoji} **{name}**\n"
            response += f"> 属性: {element}\n"

            # 基本信息
            info_parts = []
            if stage:
                info_parts.append(stage)
            if pet_type:
                info_parts.append(pet_type)
            if initial_stage_name:
                info_parts.append(f"初始:{initial_stage_name}")
            if info_parts:
                response += f"> 📋 {' | '.join(info_parts)}\n"

            # 六维属性
            if is_data_missing:
                response += f"> ⚠️ 特殊形态，暂无战斗数据\n"
            else:
                response += f"> ❤️ {hp} | 💪 {physical_attack} | 🔮 {magic_attack}\n"
                response += f"> 🛡️ {physical_defense} | ✨ {magic_defense} | ⚡ {speed}\n"

            if ability and ability != '无':
                response += f"> ✨ {ability}\n"

            if description:
                # 截断简介，最多50字
                short_desc = description[:50] + "..." if len(description) > 50 else description
                response += f"> 📝 {short_desc}\n"

            return response

        else:
            # 简洁模式（默认）：基本信息 + 关键属性
            response = f"`{name}` | {element}系\n"

            # 显示阶段和类型
            if stage or pet_type:
                info_parts = []
                if stage:
                    info_parts.append(stage)
                if pet_type:
                    info_parts.append(pet_type)
                response += f"📋 {' | '.join(info_parts)}\n"

            # 六维属性
            if is_data_missing:
                response += f"⚠️ 特殊形态，暂无战斗数据\n"
            else:
                response += f"❤️ {hp} | 💪 {physical_attack} | 🔮 {magic_attack} | ⚡ {speed}\n"

            if ability and ability != '无':
                response += f"✨ {ability}\n"

            if description:
                short_desc = description[:40] + "..." if len(description) > 40 else description
                response += f"📝 {short_desc}"

            return response

    def _format_skill_response(self, skill_data: Dict[str, Any]) -> str:
        """
        根据配置风格格式化技能信息

        Args:
            skill_data: 技能数据字典

        Returns:
            格式化后的文本
        """
        name = skill_data.get('name', '未知')
        element = skill_data.get('element', '未知')
        power = skill_data.get('power', '0')
        effect = skill_data.get('effect', '无特殊效果')
        cost = skill_data.get('cost', '0')
        category = skill_data.get('category', '魔法')

        if self.response_style == "详细":
            return f"""🎯 **{name}**
━━━━━━━━━━━━━━
📊 属性: {element}
⚔️ 类型: {category}
💪 威力: {power}
🔮 魔力消耗: {cost}
📝 效果: {effect}"""

        elif self.response_style == "卡片式":
            return f"""🎯 **{name}**
> 属性: {element} | 类型: {category}
> 威力: {power} | 消耗: {cost}"""

        else:
            return f"`{name}` | {element}系 | 威力: {power} | 消耗: {cost}"

    def _analyze_query_intent(self, query: str) -> Dict[str, Any]:
        """
        分析用户查询意图（分级检索）

        Args:
            query: 用户原始查询

        Returns:
            意图字典，包含: type, pet_name, detail_type
            例如: {'type': 'pet_detail', 'pet_name': '迪莫', 'detail_type': 'bloodline_skills'}
        """
        import re

        # 先清理常见的语气词、助词和无意义前缀/后缀
        cleaned_query = query.strip()

        logger.info(f"🔍 原始查询: '{query}'")

        # 移除游戏名称前缀（按长度排序，优先匹配长的）
        for prefix in sorted(['洛克王国', '洛克'], key=len, reverse=True):
            if cleaned_query.startswith(prefix):
                cleaned_query = cleaned_query[len(prefix):].strip()

        # 移除常见的前缀词（按长度排序，优先匹配长的）
        for prefix in sorted(['怎么获得', '如何获得', '怎么', '如何', '怎样'], key=len, reverse=True):
            if cleaned_query.startswith(prefix):
                cleaned_query = cleaned_query[len(prefix):].strip()

        logger.info(f"🔧 清理前缀后: '{cleaned_query}'")

        # 移除常见的后缀词（按长度排序，优先匹配长的）
        for suffix in sorted(['怎么获得', '如何获得', '是什么', '的介绍', '的资料', '的信息'], key=len, reverse=True):
            if cleaned_query.endswith(suffix):
                cleaned_query = cleaned_query[:-len(suffix)].strip()

        # 特殊处理：移除"有哪些"、"有什么"等中间词
        for word in ['有哪些', '有什么']:
            cleaned_query = cleaned_query.replace(word, ' ').strip()

        logger.info(f"🔧 清理后最终: '{cleaned_query}'")

        # ========== 第一优先级：检测宠物详细查询（宠物名 + 详细信息类型）==========
        # 支持多种格式：
        # 1. "XX的YYY" - 使用"的"连接
        # 2. "XX YYY" - 使用空格分隔
        # 3. "XXYYY" - 直接拼接
        # 4. "XX会/有/是什么YYY" - 自然语言
        detail_patterns = [
            # ====== 技能相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:所有技能|全部技能|完整技能|技能列表|配招|推荐技能)$', 'all_skills'),
            (r'^(.+?)\s*的\s*技能$', 'skills'),
            (r'^(.+?)\s*的\s*血脉技能$', 'bloodline_skills'),
            (r'^(.+?)\s*的\s*可学技能石$', 'learnable_stones'),
            (r'^(.+?)\s*的\s*课题技能石$', 'quest_stones'),
            # 不带"的"的格式（空格分隔）
            (r'^(.+?)\s+(?:所有技能|全部技能|完整技能|技能列表|配招|推荐技能)$', 'all_skills'),
            (r'^(.+?)\s+技能$', 'skills'),
            (r'^(.+?)\s+血脉技能$', 'bloodline_skills'),
            (r'^(.+?)\s+可学技能石$', 'learnable_stones'),
            (r'^(.+?)\s+课题技能石$', 'quest_stones'),
            # 自然语言格式
            (r'^(.+?)(?:会|能|可以)(?:学|用|使)(?:什么|哪些)?(?:技能)?$', 'skills'),
            (r'^(.+?)(?:有|会)(?:哪些|什么)?技能$', 'skills'),
            (r'^(.+?)的?(?:配招|推荐技能|技能搭配)$', 'all_skills'),
            # 无空格拼接
            (r'^(.+?)(?:所有技能|全部技能|完整技能|技能列表|配招)$', 'all_skills'),
            (r'^(.+?)技能$', 'skills'),

            # ====== 特性相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*特性$', 'ability'),
            (r'^(.+?)\s*的\s*天赋$', 'talent'),
            # 不带"的"的格式
            (r'^(.+?)\s+特性$', 'ability'),
            (r'^(.+?)\s+天赋$', 'talent'),
            # 自然语言格式
            (r'^(.+?)(?:有|是)(?:什么|哪些)?特性$', 'ability'),
            (r'^(.+?)(?:有|是)(?:什么|哪些)?天赋$', 'talent'),
            (r'^(.+?)的特性是什么$', 'ability'),
            (r'^(.+?)的天赋是什么$', 'talent'),
            # 无空格拼接
            (r'^(.+?)特性$', 'ability'),
            (r'^(.+?)天赋$', 'talent'),

            # ====== 属性相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*属性$', 'element'),
            (r'^(.+?)\s*的\s*系别$', 'element'),
            # 不带"的"的格式
            (r'^(.+?)\s+属性$', 'element'),
            (r'^(.+?)\s+系别$', 'element'),
            # 自然语言格式
            (r'^(.+?)是(?:什么|几)系$', 'element'),
            (r'^(.+?)是(?:什么|哪些)?属性$', 'element'),
            (r'^(.+?)的属性是什么$', 'element'),
            # 无空格拼接
            (r'^(.+?)属性$', 'element'),
            (r'^(.+?)系别$', 'element'),

            # ====== HP/生命相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:HP|hp|Hp|hP|生命|生命值|体力|血量)$', 'hp'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:HP|hp|Hp|hP|生命|生命值|体力|血量)$', 'hp'),
            # 无空格拼接
            (r'^(.+?)(?:HP|hp|生命|生命值|体力|血量)$', 'hp'),

            # ====== 物攻相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:物攻|物理攻击|攻击|atk|ATK|Attack|attack)$', 'physical_attack'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:物攻|物理攻击|攻击|atk|ATK|Attack|attack)$', 'physical_attack'),
            # 无空格拼接
            (r'^(.+?)(?:物攻|物理攻击|atk|ATK)$', 'physical_attack'),

            # ====== 魔攻相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:魔攻|魔法攻击|法攻|特攻|spatk|SPATK|SpAtk|Magic Attack|magic attack)$', 'magic_attack'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:魔攻|魔法攻击|法攻|特攻|spatk|SPATK|SpAtk|Magic Attack|magic attack)$', 'magic_attack'),
            # 无空格拼接
            (r'^(.+?)(?:魔攻|魔法攻击|法攻|特攻|spatk|SPATK)$', 'magic_attack'),

            # ====== 物防相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:物防|物理防御|防御|def|DEF|Defense|defense)$', 'physical_defense'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:物防|物理防御|防御|def|DEF|Defense|defense)$', 'physical_defense'),
            # 无空格拼接
            (r'^(.+?)(?:物防|物理防御|def|DEF)$', 'physical_defense'),

            # ====== 魔防相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:魔防|魔法防御|法防|特防|spdef|SPDEF|SpDef|Magic Defense|magic defense)$', 'magic_defense'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:魔防|魔法防御|法防|特防|spdef|SPDEF|SpDef|Magic Defense|magic defense)$', 'magic_defense'),
            # 无空格拼接
            (r'^(.+?)(?:魔防|魔法防御|法防|特防|spdef|SPDEF)$', 'magic_defense'),

            # ====== 速度相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:速度|速|spd|SPD|Speed|speed|先手)$', 'speed'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:速度|速|spd|SPD|Speed|speed|先手)$', 'speed'),
            # 无空格拼接
            (r'^(.+?)(?:速度|速|spd|SPD)$', 'speed'),

            # ====== 种族值/六维/面板 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:种族值|六维|面板|基础属性|能力值)$', 'stats'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:种族值|六维|面板|基础属性|能力值)$', 'stats'),
            # 无空格拼接
            (r'^(.+?)(?:种族值|六维|面板)$', 'stats'),

            # ====== 任务/课题相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:任务|课题|课题任务)$', 'quest_tasks'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:任务|课题|课题任务)$', 'quest_tasks'),
            # 自然语言格式
            (r'^(.+?)(?:要|需要)(?:做|完成)(?:什么|哪些)?任务$', 'quest_tasks'),
            (r'^(.+?)的任务是什么$', 'quest_tasks'),
            # 无空格拼接
            (r'^(.+?)(?:任务|课题)$', 'quest_tasks'),

            # ====== 进化相关 ======
            # 带"的"的格式
            (r'^(.+?)\s*的\s*(?:进化|进化条件|进化方式)$', 'evolution'),
            # 不带"的"的格式
            (r'^(.+?)\s+(?:进化|进化条件|进化方式)$', 'evolution'),
            # 自然语言格式
            (r'^(.+?)怎么进化$', 'evolution'),
            (r'^(.+?)进化成什么$', 'evolution'),
            (r'^(.+?)的进化条件是什么$', 'evolution'),
            # 无空格拼接
            (r'^(.+?)进化$', 'evolution'),

            # ====== 技能石相关 ======
            (r'^(.+?)技能石$', 'skill_stones'),
        ]

        for pattern, detail_type in detail_patterns:
            match = re.search(pattern, cleaned_query)
            if match:
                name = match.group(1).strip()
                # 清理宠物名末尾的"的"字（防止"迪莫的"被当作宠物名）
                if name.endswith('的'):
                    name = name[:-1].strip()

                logger.info(f"🎯 匹配到详细查询: pattern='{pattern}', name='{name}', type='{detail_type}'")

                # 过滤掉常见的非宠物名
                if name and len(name) >= 1 and name not in ['有', '的', '是', '怎么', '如何', '获得']:
                    return {
                        'type': 'pet_detail',
                        'pet_name': name,
                        'detail_type': detail_type
                    }

        # 检测技能石获取方式查询：“技能石 乘风”（反向语法）
        stone_pattern = r'^技能石\s*(.+)$'
        match = re.search(stone_pattern, cleaned_query)
        if match:
            stone_name = match.group(1).strip()
            if stone_name and len(stone_name) >= 1:
                return {
                    'type': 'skill_stone_info',
                    'stone_name': stone_name,
                    'only_source': False  # 显示完整信息
                }

        # 检测“怎么获得XX技能石”的查询
        source_pattern = r'(?:怎么|如何)(?:获得|获取|得到)(\S+?)(?:技能石|配方)'
        match = re.search(source_pattern, query)
        if match:
            stone_name = match.group(1).strip()
            if stone_name and len(stone_name) >= 1:
                return {
                    'type': 'skill_stone_info',
                    'stone_name': stone_name,
                    'only_source': True  # 只显示获取方式
                }

        # 检测属性筛选查询：“火系宠物有哪些”、“水系宠物列表”
        attr_pattern = r'(\S+?)(?:系|属性)?(?:宠物|精灵|有哪些|列表|推荐)'
        match = re.search(attr_pattern, cleaned_query)
        if match:
            attr_name = match.group(1).strip()
            # 移除“系”、“属性”等后缀
            attr_name = attr_name.replace('系', '').replace('属性', '').strip()
            # 常见属性列表
            valid_attrs = ['火', '水', '草', '电', '冰', '土', '风', '光', '暗', '毒', '龙', '机械', '武', '萌', '幽灵', '虫', '石', '普通']
            if attr_name in valid_attrs:
                return {
                    'type': 'attribute_filter',
                    'attribute': attr_name,
                    'entity_type': 'pet'
                }

        # 检测颜色宠物/精灵查询：“红色宠物”、“蓝色精灵”、“绿色宠物有哪些”
        color_pet_patterns = [
            (r'(\S+?)(?:的)?(?:宠物|精灵|魔灵|怪兽|伙伴)(?:有哪些|列表|推荐)?$', 'pet'),
            (r'(\S+?)(?:的)?(?:精灵蛋|蛋|宠物蛋)(?:有哪些|列表|推荐)?$', 'egg'),
        ]

        for pattern, entity_type in color_pet_patterns:
            match = re.search(pattern, cleaned_query)
            if match:
                keyword = match.group(1).strip()

                # 检测是否明确指定了“颜色”关键词
                is_explicit_color = '颜色' in keyword or '色彩' in keyword

                # 如果包含“颜色”关键词，提取实际颜色词
                if is_explicit_color:
                    keyword = re.sub(r'(?:颜色|色彩)', '', keyword).strip()

                # 标准化颜色关键词
                color_normalization = {
                    '紫色': '紫', '蓝色': '蓝', '红色': '红', '绿色': '绿',
                    '黄色': '黄', '白色': '白', '黑色': '黑', '粉色': '粉', '橙色': '橙'
                }
                normalized_keyword = color_normalization.get(keyword, keyword)

                # 检查是否是颜色关键词
                all_colors = ['红', '橙', '黄', '绿', '蓝', '紫', '粉', '白', '黑', '棕', '灰']
                if normalized_keyword in all_colors:
                    logger.info(f"🎨 检测到颜色{entity_type}查询: {normalized_keyword}")
                    return {
                        'type': 'color_filter',
                        'color': normalized_keyword,
                        'entity_type': entity_type
                    }

        # 检测稀有度宠物查询：“稀有宠物”、“史诗精灵”、“传说宠物有哪些”
        rarity_pet_patterns = [
            (r'(稀有|史诗|传说|绝版|限定)(?:的)?(?:宠物|精灵)(?:有哪些|列表|推荐)?$', 'pet'),
        ]

        for pattern, entity_type in rarity_pet_patterns:
            match = re.search(pattern, cleaned_query)
            if match:
                rarity = match.group(1).strip()
                logger.info(f"⭐ 检测到稀有度{entity_type}查询: {rarity}")
                return {
                    'type': 'rarity_filter',
                    'rarity': rarity,
                    'entity_type': entity_type
                }

        # 检测来源宠物查询：“家园宠物”、“活动精灵”、“限时宠物有哪些”
        source_pet_patterns = [
            (r'(家园|活动|限时|副本|挑战)(?:的)?(?:宠物|精灵)(?:有哪些|列表|推荐)?$', 'pet'),
        ]

        for pattern, entity_type in source_pet_patterns:
            match = re.search(pattern, cleaned_query)
            if match:
                source = match.group(1).strip()
                logger.info(f"📍 检测到来源{entity_type}查询: {source}")
                return {
                    'type': 'source_filter',
                    'source': source,
                    'entity_type': entity_type
                }

        # 检测阶段宠物查询：“初始形态宠物”、“最终形态精灵”、“幼年期宠物”
        stage_pet_patterns = [
            (r'(初始|最终|第一|第二|第三|第四|第五|幼年|成年|完全|终极)(?:形态|期|阶段)?(?:的)?(?:宠物|精灵)(?:有哪些|列表|推荐)?$', 'pet'),
        ]

        for pattern, entity_type in stage_pet_patterns:
            match = re.search(pattern, cleaned_query)
            if match:
                stage = match.group(1).strip()
                logger.info(f"🔄 检测到阶段{entity_type}查询: {stage}")
                return {
                    'type': 'stage_filter',
                    'stage': stage,
                    'entity_type': entity_type
                }

        # 检测道具类型/分类筛选：“家园家具”、“蓝色家具”、“紫色道具”、“紫色的家具”、“蓝颜色家具”
        item_patterns = [
            (r'(\S+?)(?:的)?(?:家具|装饰|摆件)', 'furniture'),  # 支持“紫色的家具”
            (r'(\S+?)(?:的)?(?:道具|物品|装备|材料)', 'item'),  # 支持“紫色的道具”
            (r'(\S+?)(?:的)?(?:技能石|配方|石头)', 'skill_stone'),  # 支持“紫色的技能石”
            (r'(\S+?)(?:的)?(?:咕噜球|球)', 'gumball'),  # 支持“蓝色的咕噜球”
            (r'(\S+?)(?:的)?(?:果实|果子)', 'fruit'),  # 支持“红色的果实”
        ]

        for pattern, category in item_patterns:
            match = re.search(pattern, cleaned_query)
            if match:
                keyword = match.group(1).strip()  # 去除前后空格
                logger.info(f"🔧 检测到分类模式: pattern='{pattern}', keyword='{keyword}', category='{category}'")

                # 检测是否明确指定了"颜色"关键词（如"蓝颜色家具"）
                is_explicit_color = '颜色' in keyword or '色彩' in keyword

                # 如果包含"颜色"关键词，提取实际颜色词
                if is_explicit_color:
                    # 移除"颜色"、"色彩"等词
                    keyword = re.sub(r'(?:颜色|色彩)', '', keyword).strip()
                    logger.info(f"🎨 检测到明确颜色查询: 原始keyword='{match.group(1)}', 提取后='{keyword}'")

                # 标准化颜色关键词：将“紫色”→“紫”，“蓝色”→“蓝”等
                color_normalization = {
                    '紫色': '紫', '蓝色': '蓝', '红色': '红', '绿色': '绿',
                    '黄色': '黄', '白色': '白', '黑色': '黑', '粉色': '粉', '橙色': '橙'
                }
                normalized_keyword = color_normalization.get(keyword, keyword)

                # 检查是否是颜色或稀有度关键词
                # 稀有度专用颜色：蓝、紫、橙（这些通常表示稀有度）
                rarity_colors = ['蓝', '紫', '橙']
                # 纯颜色关键词：红、绿、黄、白、黑、粉（这些通常是实际颜色）
                pure_colors = ['红', '绿', '黄', '白', '黑', '粉']
                # 稀有度文本关键词
                rarity_keywords = ['稀有', '史诗', '传说', '绝版', '限定']

                # 判断逻辑：
                # 1. 如果用户明确说"颜色" → filter_type='actual_color'（只查main_color）
                # 2. 如果是稀有度文本关键词 → filter_type='rarity'（只查rarity）
                # 3. 如果是稀有度颜色（蓝/紫/橙）且未明确说"颜色" → filter_type='rarity_color'（优先查rarity，回退main_color）
                # 4. 如果是纯颜色 → filter_type='actual_color'（查main_color）
                is_rarity_text = any(r in normalized_keyword for r in rarity_keywords)
                is_rarity_color = normalized_keyword in rarity_colors
                is_pure_color = normalized_keyword in pure_colors

                # 确定filter_type
                if is_explicit_color:
                    # 用户明确说了"颜色"，查询实际颜色
                    filter_type_detected = 'actual_color'
                elif is_rarity_text:
                    # 稀有度文本关键词
                    filter_type_detected = 'rarity'
                elif is_rarity_color:
                    # 稀有度颜色（蓝/紫/橙），默认当作稀有度查询
                    filter_type_detected = 'rarity_color'
                elif is_pure_color:
                    # 纯颜色，查询实际颜色
                    filter_type_detected = 'actual_color'
                else:
                    filter_type_detected = None

                logger.info(f"🔧 颜色判断: normalized_keyword='{normalized_keyword}', is_explicit_color={is_explicit_color}, is_pure_color={is_pure_color}, is_rarity_color={is_rarity_color}, is_rarity_text={is_rarity_text}, filter_type={filter_type_detected}")

                if filter_type_detected or normalized_keyword in ['家园', '活动', '限时']:
                    final_filter_type = filter_type_detected if filter_type_detected else 'source'
                    logger.info(f"🎯 返回分类筛选意图: keyword='{normalized_keyword}', filter_type='{final_filter_type}'")
                    return {
                        'type': 'category_filter',
                        'keyword': normalized_keyword,
                        'category': category,
                        'filter_type': final_filter_type
                    }

        # 默认意图：普通查询
        return {'type': 'normal'}

    def _format_pet_detail_info(self, pet: Dict, detail_type: str) -> str:
        """
        格式化宠物的详细信息（血脉技能、技能石等）

        Args:
            pet: 宠物数据字典
            detail_type: 详细信息类型

        Returns:
            格式化的文本
        """
        import json

        pet_name = pet.get('name', '未知')

        if detail_type == 'bloodline_skills':
            bloodline_skills = pet.get('bloodline_skills', '')
            skills_list = self._parse_list_field(bloodline_skills)

            response = f"💫 **{pet_name} - 血脉技能**\n"
            response += "━━━━━━━━━━━━━━\n"

            if skills_list:
                for i, skill in enumerate(skills_list, 1):
                    response += f"  {i}. {skill}\n"
            else:
                response += "  (暂无血脉技能信息)"

            return response

        elif detail_type == 'skill_stones' or detail_type == 'learnable_stones':
            learnable_stones = pet.get('learnable_skill_stones', '')
            stones_list = self._parse_list_field(learnable_stones)

            response = f"📖 **{pet_name} - 可学技能石**\n"
            response += "━━━━━━━━━━━━━━\n"

            if stones_list:
                for i, stone in enumerate(stones_list, 1):
                    response += f"  {i}. {stone}\n"
            else:
                response += "  (暂无可学技能石信息)"

            return response

        elif detail_type == 'quest_stones':
            quest_stones = pet.get('quest_skill_stones', '')
            stones_list = self._parse_list_field(quest_stones)

            response = f"🎯 **{pet_name} - 课题技能石**\n"
            response += "━━━━━━━━━━━━━━\n"

            if stones_list:
                for i, stone in enumerate(stones_list, 1):
                    response += f"  {i}. {stone}\n"
            else:
                response += "  (暂无课题技能石信息)"

            return response

        elif detail_type == 'all_skills':
            skills = pet.get('skills', '')
            skills_list = self._parse_list_field(skills)

            response = f"📚 **{pet_name} - 完整技能列表**\n"
            response += "━━━━━━━━━━━━━━\n"

            if skills_list:
                # 显示所有技能，每行5个
                for i in range(0, len(skills_list), 5):
                    batch = skills_list[i:i+5]
                    response += "  " + ", ".join(batch) + "\n"
                response += f"\n总计: {len(skills_list)} 个技能"
            else:
                response += "  (暂无技能信息)"

            return response

        elif detail_type == 'skills':
            # 与 all_skills 相同
            skills = pet.get('skills', '')
            skills_list = self._parse_list_field(skills)

            response = f"⚔️ **{pet_name} - 技能列表**\n"
            response += "━━━━━━━━━━━━━━\n"

            if skills_list:
                for i in range(0, len(skills_list), 5):
                    batch = skills_list[i:i+5]
                    response += "  " + ", ".join(batch) + "\n"
                response += f"\n总计: {len(skills_list)} 个技能"
            else:
                response += "  (暂无技能信息)"

            return response

        elif detail_type == 'ability' or detail_type == 'talent':
            ability = pet.get('ability', '')
            ability_desc = pet.get('ability_desc', '')

            response = f"✨ **{pet_name} - 特性**\n"
            response += "━━━━━━━━━━━━━━\n"

            if ability:
                response += f"🔮 **{ability}**\n"
                if ability_desc:
                    response += f"\n📝 {ability_desc}\n"
            else:
                response += "  (暂无特性信息)"

            return response

        elif detail_type == 'element':
            element = pet.get('element', '')
            element2 = pet.get('element2', '')

            response = f"🎯 **{pet_name} - 属性**\n"
            response += "━━━━━━━━━━━━━━\n"

            if element:
                if element2:
                    response += f"  主属性: {element}\n"
                    response += f"  副属性: {element2}\n"
                else:
                    response += f"  属性: {element}\n"
            else:
                response += "  (暂无属性信息)"

            return response

        elif detail_type == 'hp' or detail_type == 'stats':
            hp = pet.get('hp', 0)
            physical_attack = pet.get('physical_attack', 0)
            magic_attack = pet.get('magic_attack', 0)
            physical_defense = pet.get('physical_defense', 0)
            magic_defense = pet.get('magic_defense', 0)
            speed = pet.get('speed', 0)

            response = f"💪 **{pet_name} - 种族值**\n"
            response += "━━━━━━━━━━━━━━\n"
            response += f"  ❤️ HP: {hp}\n"
            response += f"  ⚔️ 物攻: {physical_attack}\n"
            response += f"  🔮 魔攻: {magic_attack}\n"
            response += f"  🛡️ 物防: {physical_defense}\n"
            response += f"  ✨ 魔防: {magic_defense}\n"
            response += f"  💨 速度: {speed}\n"
            total = hp + physical_attack + magic_attack + physical_defense + magic_defense + speed
            response += f"\n  📊 总和: {total}\n"

            return response

        elif detail_type == 'physical_attack':
            value = pet.get('physical_attack', 0)
            response = f"⚔️ **{pet_name} - 物理攻击**\n"
            response += "━━━━━━━━━━━━━━\n"
            response += f"  物攻: {value}\n"
            return response

        elif detail_type == 'magic_attack':
            value = pet.get('magic_attack', 0)
            response = f"🔮 **{pet_name} - 魔法攻击**\n"
            response += "━━━━━━━━━━━━━━\n"
            response += f"  魔攻: {value}\n"
            return response

        elif detail_type == 'physical_defense':
            value = pet.get('physical_defense', 0)
            response = f"🛡️ **{pet_name} - 物理防御**\n"
            response += "━━━━━━━━━━━━━━\n"
            response += f"  物防: {value}\n"
            return response

        elif detail_type == 'magic_defense':
            value = pet.get('magic_defense', 0)
            response = f"✨ **{pet_name} - 魔法防御**\n"
            response += "━━━━━━━━━━━━━━\n"
            response += f"  魔防: {value}\n"
            return response

        elif detail_type == 'speed':
            value = pet.get('speed', 0)
            response = f"💨 **{pet_name} - 速度**\n"
            response += "━━━━━━━━━━━━━━\n"
            response += f"  速度: {value}\n"
            return response

        elif detail_type == 'quest_tasks':
            quest_tasks = pet.get('quest_tasks', '')
            tasks_list = self._parse_list_field(quest_tasks)

            response = f"📋 **{pet_name} - 课题任务**\n"
            response += "━━━━━━━━━━━━━━\n"

            if tasks_list:
                for i, task in enumerate(tasks_list, 1):
                    response += f"  {i}. {task}\n"
            else:
                response += "  (暂无课题任务信息)"

            return response

        elif detail_type == 'evolution':
            evolution_condition = pet.get('evolution_condition', '')

            response = f"🔄 **{pet_name} - 进化条件**\n"
            response += "━━━━━━━━━━━━━━\n"

            if evolution_condition:
                response += f"  {evolution_condition}\n"
            else:
                response += "  (暂无进化信息)"

            return response

        else:
            return ""

    def _format_skill_stone_info(self, stone_name: str, only_source: bool = False) -> str:
        """
        格式化技能石的获取信息

        Args:
            stone_name: 技能石名称（如“乘风”）
            only_source: 是否只显示获取方式

        Returns:
            格式化的文本
        """
        import json

        # 1. 查询道具表中的技能石
        items = self.db_service.get_item_info(f"技能石/{stone_name}", fuzzy=False, limit=10)

        if not items:
            # 尝试直接匹配名称
            items = self.db_service.get_item_info(stone_name, fuzzy=True, limit=10)
            # 过滤出分类为“技能石”的
            items = [item for item in items if item.get('category') == '技能石']

        if not items:
            return f"❌ 未找到技能石 \"{stone_name}\""

        # 如果只需要获取方式，直接返回
        if only_source:
            item = items[0]
            source = item.get('source', '')
            if source and source.strip():
                response = f"💎 **技能石 - {stone_name}**\n"
                response += "━━━━━━━━━━━━━━\n\n"
                response += f"🛒 **获取方式:**\n{source}\n"
                return response
            else:
                return f"💎 **技能石 - {stone_name}**\n━━━━━━━━━━━━━━\n\n⚠️ 暂无获取方式信息\n"

        response = f"💎 **技能石 - {stone_name}**\n"
        response += "━━━━━━━━━━━━━━\n"

        for item in items[:3]:  # 最多显示3个
            response += f"\n📦 **{item['name']}**\n"
            if item.get('rarity'):
                response += f"⭐ 稀有度: {item['rarity']}\n"
            if item.get('subcategory'):
                response += f"🔹 类型: {item['subcategory']}\n"

            # 显示来源信息（关键修复）
            source = item.get('source', '')
            if source and source.strip():
                response += f"\n🛒 **获取方式:**\n{source}\n"
            else:
                response += f"\n⚠️ 暂无获取方式信息\n"

        # 2. 查询哪些宠物可以学习这个技能石
        cursor = self.db_service.conn.cursor()
        cursor.execute("""
            SELECT name FROM pets
            WHERE learnable_skill_stones LIKE ?
            LIMIT 10
        """, (f'%{stone_name}%',))

        pets_with_stone = [row[0] for row in cursor.fetchall()]

        if pets_with_stone:
            response += f"\n🐾 **可学习此技能石的宠物** ({len(pets_with_stone)}个):\n"
            response += "  " + ", ".join(pets_with_stone[:10])
            if len(pets_with_stone) > 10:
                response += f"...等共{len(pets_with_stone)}个"
            response += "\n"

        # 3. 如果同时有同名技能，也显示一下
        skills = self.db_service.get_skill_info(stone_name, fuzzy=False, limit=1)
        if skills:
            skill = skills[0]
            response += f"\n📚 **相关技能:** {skill['name']} ({skill['element']}系)\n"
            if skill.get('power'):
                response += f"  威力: {skill['power']}"
            if skill.get('cost'):
                response += f" | PP: {skill['cost']}"
            if skill.get('category'):
                response += f" | 类型: {skill['category']}"
            response += "\n"

        return response

    def _handle_color_filter(self, color: str, entity_type: str) -> str:
        """
        处理颜色宠物/精灵蛋查询：“红色宠物”、“蓝色精灵蛋”

        Args:
            color: 颜色关键词（如“红”、“蓝”）
            entity_type: 实体类型（pet=宠物, egg=精灵蛋）

        Returns:
            格式化的文本
        """
        if entity_type not in ['pet', 'egg']:
            return f"❌ 不支持的实体类型: {entity_type}"

        # 查询 pets 表中 main_color 字段匹配的宠物
        cursor = self.db_service.conn.cursor()

        # 构建查询条件
        query = "SELECT name, element, element2, stage, main_color FROM pets WHERE main_color = ?"

        # 如果是精灵蛋，过滤出包含“蛋”字的宠物名
        if entity_type == 'egg':
            query += " AND (name LIKE '%蛋%' OR name LIKE '%卵%')"

        query += " ORDER BY name LIMIT 50"

        cursor.execute(query, (color,))
        pets = [dict(zip(['name', 'element', 'element2', 'stage', 'main_color'], row)) for row in cursor.fetchall()]

        logger.info(f"🎨 颜色{entity_type}筛选: color='{color}', 找到 {len(pets)} 个结果")

        if not pets:
            entity_name = "宠物" if entity_type == 'pet' else "精灵蛋"
            return f"❌ 未找到{color}色的{entity_name}"

        entity_name = "宠物" if entity_type == 'pet' else "精灵蛋"
        response = f"🎨 **{color}色{entity_name}列表** (共{len(pets)}个):\n"
        response += "━━━━━━━━━━━━━━\n\n"

        # 使用分页配置
        page_size = self.page_size
        display_pets = pets[:page_size]

        for i, pet in enumerate(display_pets, 1):
            element = pet.get('element', '未知')
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            stage = pet.get('stage', '')
            stage_str = f" [{stage}]" if stage else ""
            response += f"{i}. {pet['name']} ({element}{extra}系){stage_str}\n"

        if len(pets) > page_size:
            response += f"\n...还有 {len(pets) - page_size} 个"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    def _handle_rarity_filter(self, rarity: str, entity_type: str) -> str:
        """
        处理稀有度宠物查询：“稀有宠物”、“史诗精灵”

        Args:
            rarity: 稀有度关键词（如“稀有”、“史诗”）
            entity_type: 实体类型（pet=宠物）

        Returns:
            格式化的文本
        """
        if entity_type != 'pet':
            return f"❌ 不支持的实体类型: {entity_type}"

        # 查询 pets 表中 description 或 ability 字段包含稀有度关键词的宠物
        cursor = self.db_service.conn.cursor()

        query = "SELECT name, element, element2, stage, description, ability FROM pets WHERE (description LIKE ? OR ability LIKE ?) ORDER BY name LIMIT 50"

        cursor.execute(query, (f'%{rarity}%', f'%{rarity}%'))
        pets = [dict(zip(['name', 'element', 'element2', 'stage', 'description', 'ability'], row)) for row in cursor.fetchall()]

        logger.info(f"⭐ 稀有度{entity_type}筛选: rarity='{rarity}', 找到 {len(pets)} 个结果")

        if not pets:
            return f"❌ 未找到{rarity}稀有度的宠物"

        response = f"⭐ **{rarity}稀有度宠物列表** (共{len(pets)}个):\n"
        response += "━━━━━━━━━━━━━━\n\n"

        # 使用分页配置
        page_size = self.page_size
        display_pets = pets[:page_size]

        for i, pet in enumerate(display_pets, 1):
            element = pet.get('element', '未知')
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            stage = pet.get('stage', '')
            stage_str = f" [{stage}]" if stage else ""
            response += f"{i}. {pet['name']} ({element}{extra}系){stage_str}\n"

        if len(pets) > page_size:
            response += f"\n...还有 {len(pets) - page_size} 个"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    def _handle_source_filter(self, source: str, entity_type: str) -> str:
        """
        处理来源宠物查询：“家园宠物”、“活动精灵”

        Args:
            source: 来源关键词（如“家园”、“活动”）
            entity_type: 实体类型（pet=宠物）

        Returns:
            格式化的文本
        """
        if entity_type != 'pet':
            return f"❌ 不支持的实体类型: {entity_type}"

        # 查询 pets 表中 description 字段包含来源关键词的宠物
        cursor = self.db_service.conn.cursor()

        # 根据来源类型扩展关键词
        source_map = {
            '家园': ['家园', '家具店', '商店'],
            '活动': ['活动', '限时', '节日'],
            '限时': ['限时', '活动', '节日'],
            '副本': ['副本', '挑战', '关卡'],
            '挑战': ['挑战', '副本', '关卡'],
        }

        source_keywords = source_map.get(source, [source])
        like_conditions = ' OR '.join([f"description LIKE '%{s}%" for s in source_keywords])

        query = f"SELECT name, element, element2, stage, description FROM pets WHERE ({like_conditions}) ORDER BY name LIMIT 50"

        cursor.execute(query)
        pets = [dict(zip(['name', 'element', 'element2', 'stage', 'description'], row)) for row in cursor.fetchall()]

        logger.info(f"📍 来源{entity_type}筛选: source='{source}', 找到 {len(pets)} 个结果")

        if not pets:
            return f"❌ 未找到{source}相关的宠物"

        response = f"📍 **{source}相关宠物列表** (共{len(pets)}个):\n"
        response += "━━━━━━━━━━━━━━\n\n"

        # 使用分页配置
        page_size = self.page_size
        display_pets = pets[:page_size]

        for i, pet in enumerate(display_pets, 1):
            element = pet.get('element', '未知')
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            stage = pet.get('stage', '')
            stage_str = f" [{stage}]" if stage else ""
            response += f"{i}. {pet['name']} ({element}{extra}系){stage_str}\n"

        if len(pets) > page_size:
            response += f"\n...还有 {len(pets) - page_size} 个"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    def _handle_stage_filter(self, stage: str, entity_type: str) -> str:
        """
        处理阶段宠物查询：“初始形态宠物”、“最终形态精灵”

        Args:
            stage: 阶段关键词（如“初始”、“最终”）
            entity_type: 实体类型（pet=宠物）

        Returns:
            格式化的文本
        """
        if entity_type != 'pet':
            return f"❌ 不支持的实体类型: {entity_type}"

        # 映射阶段关键词到数据库中的 stage 值
        stage_map = {
            '初始': ['初始形态', '初级'],
            '第一': ['初始形态', '初级'],
            '幼年': ['幼年', '幼年期'],
            '成年': ['成年', '成长期'],
            '完全': ['完全体', '成熟期'],
            '终极': ['终极形态', '完全体'],
            '最终': ['最终形态', '究极体'],
        }

        stage_keywords = stage_map.get(stage, [stage])

        # 查询 pets 表中 stage 字段匹配阶段关键词的宠物
        cursor = self.db_service.conn.cursor()

        like_conditions = ' OR '.join([f"stage LIKE '%{s}%" for s in stage_keywords])
        query = f"SELECT name, element, element2, stage FROM pets WHERE ({like_conditions}) ORDER BY name LIMIT 50"

        cursor.execute(query)
        pets = [dict(zip(['name', 'element', 'element2', 'stage'], row)) for row in cursor.fetchall()]

        logger.info(f"🔄 阶段{entity_type}筛选: stage='{stage}', 找到 {len(pets)} 个结果")

        if not pets:
            return f"❌ 未找到{stage}阶段的宠物"

        response = f"🔄 **{stage}阶段宠物列表** (共{len(pets)}个):\n"
        response += "━━━━━━━━━━━━━━\n\n"

        # 使用分页配置
        page_size = self.page_size
        display_pets = pets[:page_size]

        for i, pet in enumerate(display_pets, 1):
            element = pet.get('element', '未知')
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            pet_stage = pet.get('stage', '')
            stage_str = f" [{pet_stage}]" if pet_stage else ""
            response += f"{i}. {pet['name']} ({element}{extra}系){stage_str}\n"

        if len(pets) > page_size:
            response += f"\n...还有 {len(pets) - page_size} 个"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    def _handle_attribute_filter(self, attribute: str, entity_type: str) -> str:
        """
        处理属性筛选查询：“火系宠物有哪些”

        Args:
            attribute: 属性名称（如“火”）
            entity_type: 实体类型（如“pet”）

        Returns:
            格式化的文本
        """
        if entity_type == 'pet':
            # 查询该属性的宠物
            pets = self.db_service.get_pets_by_element(attribute, limit=50)

            if not pets:
                return f"❌ 未找到{attribute}系宠物"

            response = f"🔥 **{attribute}系宠物列表** (共{len(pets)}个):\n"
            response += "━━━━━━━━━━━━━━\n\n"

            # 使用分页配置
            page_size = self.page_size
            display_pets = pets[:page_size]

            for i, pet in enumerate(display_pets, 1):
                element2 = pet.get('element2', '')
                extra = f"/{element2}" if element2 else ""
                response += f"{i}. {pet['name']} ({pet['element']}{extra}系)\n"

            if len(pets) > page_size:
                response += f"\n...还有 {len(pets) - page_size} 个"

            response += f"\n💡 提示：输入完整名称可查看详细信息"
            return response

        return f"❌ 不支持的实体类型: {entity_type}"

    def _handle_category_filter(self, keyword: str, category: str, filter_type: str) -> str:
        """
        处理分类/颜色/稀有度筛选：“蓝色家具”、“紫色道具”

        Args:
            keyword: 关键词（如“蓝”、“家园”）
            category: 类别（如“furniture”、“item”）
            filter_type: 筛选类型（color/rarity/source）

        Returns:
            格式化的文本
        """
        import json

        # 映射类别到数据库表
        db_category_map = {
            'furniture': '家具',
            'item': '',  # 所有道具
            'skill_stone': '技能石',
            'gumball': '咕噜球',
            'fruit': '精灵果实',
        }

        db_category = db_category_map.get(category, '')

        # 构建查询条件
        cursor = self.db_service.conn.cursor()

        if filter_type == 'actual_color':
            # 实际颜色筛选：只查询 main_color 字段（大模型识别的实际颜色）
            color_map = {
                '蓝': ['蓝'],
                '红': ['红'],
                '绿': ['绿'],
                '黄': ['黄'],
                '紫': ['紫'],
                '白': ['白'],
                '黑': ['黑'],
                '粉': ['粉'],
                '橙': ['橙'],
            }

            color_keywords = color_map.get(keyword, [keyword])
            like_conditions = ' OR '.join([f"main_color = '{c}'" for c in color_keywords])

            query = f"SELECT name, category, rarity, main_color, description FROM items WHERE ({like_conditions})"
            if db_category:
                query += f" AND category = '{db_category}'"
            query += " LIMIT 15"

            cursor.execute(query)
            items = [dict(zip(['name', 'category', 'rarity', 'main_color', 'description'], row)) for row in cursor.fetchall()]
            logger.info(f"🎨 实际颜色筛选: keyword='{keyword}', 找到 {len(items)} 个结果")

        elif filter_type == 'rarity_color':
            # 稀有度颜色筛选：优先查询 rarity 字段，回退到 main_color
            color_map = {
                '蓝': ['蓝'],
                '紫': ['紫'],
                '橙': ['橙'],
            }

            color_keywords = color_map.get(keyword, [keyword])

            # 先查询 rarity 字段
            like_conditions_rarity = ' OR '.join([f"rarity LIKE '%{c}%'" for c in color_keywords])
            query = f"SELECT name, category, rarity, main_color, description FROM items WHERE ({like_conditions_rarity})"
            if db_category:
                query += f" AND category = '{db_category}'"
            query += " LIMIT 15"

            cursor.execute(query)
            items = [dict(zip(['name', 'category', 'rarity', 'main_color', 'description'], row)) for row in cursor.fetchall()]

            # 如果 rarity 没有结果，回退到 main_color
            if not items:
                logger.info(f"🔄 rarity未找到结果，回退到main_color字段")
                like_conditions_main = ' OR '.join([f"main_color = '{c}'" for c in color_keywords])
                query = f"SELECT name, category, rarity, main_color, description FROM items WHERE ({like_conditions_main})"
                if db_category:
                    query += f" AND category = '{db_category}'"
                query += " LIMIT 15"

                cursor.execute(query)
                items = [dict(zip(['name', 'category', 'rarity', 'main_color', 'description'], row)) for row in cursor.fetchall()]

            logger.info(f"⭐ 稀有度颜色筛选: keyword='{keyword}', 找到 {len(items)} 个结果")

        elif filter_type == 'color':
            # 颜色筛选：同时从 main_color 和 rarity 字段匹配
            # main_color: 大模型识别的实际颜色
            # rarity: 稀有度颜色（蓝/紫/橙等）
            color_map = {
                '蓝': ['蓝'],
                '红': ['红'],
                '绿': ['绿'],
                '黄': ['黄'],
                '紫': ['紫'],
                '白': ['白'],
                '黑': ['黑'],
                '粉': ['粉'],
                '橙': ['橙'],
            }

            color_keywords = color_map.get(keyword, [keyword])

            # 同时查询 main_color 和 rarity 字段
            like_conditions_main = ' OR '.join([f"main_color = '{c}'" for c in color_keywords])
            like_conditions_rarity = ' OR '.join([f"rarity LIKE '%{c}%'" for c in color_keywords])

            query = f"SELECT name, category, rarity, main_color, description FROM items WHERE ({like_conditions_main}) OR ({like_conditions_rarity})"
            if db_category:
                query += f" AND category = '{db_category}'"
            query += " LIMIT 15"

            cursor.execute(query)
            items = [dict(zip(['name', 'category', 'rarity', 'main_color', 'description'], row)) for row in cursor.fetchall()]
            logger.info(f"🎨 颜色筛选: keyword='{keyword}', 找到 {len(items)} 个结果")

        elif filter_type == 'rarity':
            # 稀有度筛选
            rarity_map = {
                '稀有': '稀有',
                '史诗': '史诗',
                '传说': '传说',
                '绝版': '绝版',
                '限定': '限定',
            }

            rarity_value = rarity_map.get(keyword, keyword)

            query = "SELECT name, category, rarity, description FROM items WHERE (rarity LIKE ? OR description LIKE ?)"
            if db_category:
                query += f" AND category = '{db_category}'"
            query += " LIMIT 15"

            cursor.execute(query, (f'%{rarity_value}%', f'%{rarity_value}%'))
            items = [dict(zip(['name', 'category', 'rarity', 'description'], row)) for row in cursor.fetchall()]

        else:  # source
            # 来源筛选（如“家园”）
            source_map = {
                '家园': ['家园', '家具店'],
                '活动': ['活动', '限时'],
                '限时': ['限时', '活动'],
            }

            source_keywords = source_map.get(keyword, [keyword])
            like_conditions = ' OR '.join([f"source LIKE '%{s}%' OR description LIKE '%{s}%'" for s in source_keywords])

            query = f"SELECT name, category, rarity, description FROM items WHERE ({like_conditions})"
            if db_category:
                query += f" AND category = '{db_category}'"
            query += " LIMIT 15"

            cursor.execute(query)
            items = [dict(zip(['name', 'category', 'rarity', 'description'], row)) for row in cursor.fetchall()]

        if not items:
            filter_desc = {
                'actual_color': '颜色',
                'rarity_color': '稀有度',
                'color': '颜色',
                'rarity': '稀有度',
                'source': '来源'
            }.get(filter_type, '')
            return f"❌ 未找到{keyword}{filter_desc}的{db_category or '道具'}"

        # 格式化输出
        type_names = {
            'actual_color': '颜色',
            'rarity_color': '稀有度',
            'color': '颜色',
            'rarity': '稀有度',
            'source': '来源',
        }
        type_name = type_names.get(filter_type, '')

        response = f"🎨 **{keyword}{type_name}的{db_category or '道具'}** (共{len(items)}个):\n"
        response += "━━━━━━━━━━━━━━\n\n"

        # 使用分页配置
        page_size = self.page_size
        display_items = items[:page_size]

        for i, item in enumerate(display_items, 1):
            response += f"{i}. **{item['name']}**"
            # 根据筛选类型显示对应字段
            if filter_type == 'actual_color':
                # 实际颜色筛选：显示 main_color
                if item.get('main_color'):
                    response += f" [{item['main_color']}]"
            elif filter_type == 'rarity_color':
                # 稀有度颜色筛选：优先显示 rarity
                if item.get('rarity'):
                    response += f" [{item['rarity']}]"
                elif item.get('main_color'):
                    response += f" [颜色:{item['main_color']}]"
            else:
                # 其他类型：优先显示 main_color，没有则显示 rarity
                if item.get('main_color'):
                    response += f" [{item['main_color']}]"
                elif item.get('rarity'):
                    response += f" [{item['rarity']}]"
            response += "\n"

        if len(items) > page_size:
            response += f"\n...还有 {len(items) - page_size} 个"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    def _parse_type_query(self, query: str) -> Optional[Dict[str, Any]]:
        """
        解析智能查询（包括属性克制、拼音、编号等）

        Args:
            query: 用户输入

        Returns:
            查询类型和参数的字典，如果不是特殊查询返回 None
        """
        import re

        # 0. 宠物编号查询："82"、"#82"、"No.82"、"第82号"
        id_patterns = [
            r'^#?(\d+)$',  # "82"、"#82"
            r'(?:no|NO|No)\.?\s*(\d+)',  # "No.82"、"no82"
            r'第(\d+)号',  # "第82号"
            r'(\d+)号宠物',  # "82号宠物"
        ]

        for pattern in id_patterns:
            match = re.search(pattern, query)
            if match:
                return {
                    'type': 'pet_id',
                    'pet_id': int(match.group(1))
                }

        # 1. 属性克制查询：“火克草”、“水系被电系克”、“水vs电”
        type_patterns = [
            (r'(\w+)[系]?克(\w+)[系]?', 'type_advantage', False),
            (r'(\w+)[系]?被(\w+)[系]?克', 'type_advantage_reverse', False),
            (r'(\w+)[系]?vs(\w+)[系]?', 'type_advantage', False),
            (r'(\w+)[系]?对(\w+)[系]?', 'type_advantage', False),
            (r'(\w+)[系]?打(\w+)[系]?', 'type_advantage', False),  # "火打水"
            (r'(\w+)[系]?抗(\w+)[系]?', 'type_resistance', False),  # "火抗草"
        ]

        for pattern, qtype, is_reverse in type_patterns:
            match = re.search(pattern, query)
            if match:
                attack_type = match.group(1).replace('系', '')
                defense_type = match.group(2).replace('系', '')

                if is_reverse or '被' in pattern:
                    return {
                        'type': 'type_advantage',
                        'attack_type': defense_type,
                        'defense_type': attack_type
                    }
                elif qtype == 'type_resistance':
                    # “火抗草” = 火抵抗草 = 草打火效果不好
                    return {
                        'type': 'type_advantage',
                        'attack_type': defense_type,
                        'defense_type': attack_type
                    }
                else:
                    return {
                        'type': 'type_advantage',
                        'attack_type': attack_type,
                        'defense_type': defense_type
                    }

        # 2. 单属性完整克制关系：“火系”、“火的克制”、“火属性”
        single_type_patterns = [
            r'^(\w+)[系](?:的克制|克制关系)?$',  # "火系"、"火系的克制"
            r'^(\w+)(?:的克制|克制关系)$',  # "火的克制"
            r'^(\w+)[系]?属性$',  # "火属性"
            r'^(\w+)[系]?(?:被什么克|克什么|克制谁)$',  # "火被什么克"、"火克什么"
            r'^(\w+)[系]?(?:弱点|优势|劣势)$',  # "火弱点"、"火优势"
        ]

        for pattern in single_type_patterns:
            match = re.search(pattern, query)
            if match:
                element = match.group(1).replace('系', '')  # 去掉“系”字
                # 验证是否是有效的属性名（避免误匹配）
                valid_elements = ['火', '水', '草', '电', '冰', '龙', '光', '暗', '普通', '机械', '武', '毒', '翼', '萌', '虫', '幽', '幻', '地', '恶']
                if element in valid_elements or len(element) <= 2:  # 允许常见属性或短词
                    return {
                        'type': 'type_summary',
                        'element': element
                    }

        # 2.5 单属性宠物查询：“火系宠物”、“火系精灵”、“水系宝可梦”、“洛克 火系宠物”、“火属性的精灵”
        pet_element_patterns = [
            r'(?:洛克|帮我查|查询|查找)?\s*(\w+?)[系的](?:宠物|精灵|怪兽|魔灵|伙伴)$',  # "火系宠物"、"火属性的精灵"、"洛克 火系宠物"
            r'(?:洛克|帮我查|查询|查找)?\s*(\w+?)[系的]$',  # "火系"、"火属性的" (单独的属性也可能是在查宠物)
        ]

        # 4. 属性组合查询（必须在单属性之前，避免“光+地的精灵”被误匹配为单属性）
        combo_patterns = [
            r'(\w+?)[+和与](\w+?)[系的]?(?:宠物|精灵|怪兽|魔灵|伙伴)',  # "草+毒宠物"、"光+地的精灵"
            r'(\w+?)[+和与](\w+?)系',  # "草+毒系"
            r'(\w+)(\w+)双系',  # "草毒双系"
            r'(\w+)(\w+)系宠物',  # "草毒系宠物" (连续两个属性)
        ]

        for pattern in combo_patterns:
            match = re.search(pattern, query)
            if match:
                elem1, elem2 = match.group(1), match.group(2)
                # 去掉“系”、“属性”、“的”等后缀
                elem1 = elem1.replace('系', '').replace('属性', '').replace('的', '')
                elem2 = elem2.replace('系', '').replace('属性', '').replace('的', '')
                # 验证是否是有效的属性组合
                valid_elements = ['火', '水', '草', '电', '冰', '龙', '光', '暗', '普通', '机械', '武', '毒', '翼', '萌', '虫', '幽', '幻', '地', '恶']
                if elem1 in valid_elements and elem2 in valid_elements:
                    return {
                        'type': 'pet_elements',
                        'elements': [elem1, elem2]
                    }

        # 单属性查询（在双属性之后）
        for pattern in pet_element_patterns:
            match = re.search(pattern, query)
            if match:
                element = match.group(1).replace('系', '').replace('属性', '')
                valid_elements = ['火', '水', '草', '电', '冰', '龙', '光', '暗', '普通', '机械', '武', '毒', '翼', '萌', '虫', '幽', '幻', '地', '恶']
                if element in valid_elements:
                    return {
                        'type': 'pet_by_element',
                        'element': element
                    }

        # 3. 技能威力排行：“最强草系技能”、“威力最大的火系技能”
        # 优先匹配带属性的模式
        top_skill_patterns = [
            r'(\w+)[系]?(?:最强|威力最大|最高)技能',  # "草系最强技能"
            r'(?:最强|威力最大|最高)(\w+)[系]?技能',  # "最强草系技能"
            r'(\w+)[系]?.*?(?:最强|威力最大|最高).*?技能',  # "火系最强的技能"
            r'(\w+)[系]?(?:最好用|最实用|推荐)技能',  # "草系最好用技能"
        ]

        for pattern in top_skill_patterns:
            match = re.search(pattern, query)
            if match:
                element = match.group(1).replace('系', '')  # 去掉“系”字
                # 过滤掉无意义的单字（如"的"、"是"等）
                if element and len(element) >= 1 and element not in ['的', '是', '有', '什么']:
                    return {
                        'type': 'top_skills',
                        'element': element
                    }

        # 无属性限定的技能排行
        if re.search(r'(?:最强|威力最大|最高|最好用|最实用)技能', query):
            return {
                'type': 'top_skills',
                'element': None
            }

        # 3.5 特定技能查询：“迪莫的技能”、“喵喵有什么招式”
        pet_skill_query = re.search(r'(\S+?)(?:的技能|有什么技能|有哪些技能|的招式)', query)
        if pet_skill_query:
            return {
                'type': 'pet_skills_query',
                'pet_name': pet_skill_query.group(1).strip()
            }

        # 3.6 宠物特性查询：“迪莫的特性”、“幻灵菇是什么特性”
        pet_ability_query = re.search(r'(\S+?)(?:的特性|是什么特性|有什么特性)', query)
        if pet_ability_query:
            return {
                'type': 'pet_ability_query',
                'pet_name': pet_ability_query.group(1).strip()
            }

        # 3.7 宠物分布/位置查询：“在哪里抓迪莫”、“幻灵菇在哪出现”、“幻灵菇分布”
        pet_location_query = re.search(r'(?:在哪里|在哪|哪里|什么地方|什么位置|分布|出没)(?:抓|捕捉|遇到|找|有)?(\S+?)|(\S+?)(?:在哪里|在哪|哪里|出没|分布)', query)
        if pet_location_query:
            pet_name = pet_location_query.group(1) or pet_location_query.group(2)
            if pet_name:
                return {
                    'type': 'pet_location_query',
                    'pet_name': pet_name.strip()
                }

        # 3.8 宠物进化查询：支持多种表达方式
        # "迪莫怎么进化"、"小灵菇进化条件"、"幻灵菇进化成什么"
        # "喵喵进化至下一阶段的条件是什么"、"喵呜进化需要什么条件"
        # "墨鱿士进化" （简单格式）

        # 优先匹配最终形态查询（必须在普通进化查询之前）
        # "某精灵的最终形态"、"最终进化是什么"
        # 注意：需要正确处理"XX的最终形态"、"XX的最后进化"、"XX的终极形态"等格式
        pet_final_form = re.search(r'([^\s]+?)(?:的)?(?:最终形态|最后进化|终极形态|最终进化)', query)
        if pet_final_form:
            return {
                'type': 'pet_final_form_query',
                'pet_name': pet_final_form.group(1).strip()
            }

        # 再尝试匹配详细的进化查询（不包括“下一阶段”）
        pet_evolution_query = re.search(r'([^\s]+?)(?:的)?(?:怎么进化|如何进化|进化条件|进化成什么|进化形态|进化至下一阶段|进化需要什么|下一阶段的条件|会进化成|会变成|变成什么|进化成啥)', query)
        if pet_evolution_query:
            return {
                'type': 'pet_evolution_query',
                'pet_name': pet_evolution_query.group(1).strip()
            }

        # “某精灵的下一阶段” - 单独处理，只返回下一个进化形态
        pet_next_stage = re.search(r'([^\s]+?)(?:的)?(?:下一阶段|下一个是什么|后面是什么|下一步)', query)
        if pet_next_stage:
            return {
                'type': 'pet_next_stage_query',
                'pet_name': pet_next_stage.group(1).strip()
            }

        # "某精灵的进化"（简单进化查询）- 必须在 simple_evolution 之前
        simple_pet_evolution = re.search(r'^([^\s的]+?)的进化$', query)
        if simple_pet_evolution:
            return {
                'type': 'pet_evolution_query',
                'pet_name': simple_pet_evolution.group(1).strip()
            }

        # 再尝试匹配简单的“XX进化”格式（确保不是其他类型）
        simple_evolution = re.search(r'^([^\s]+?)进化$', query)
        if simple_evolution:
            pet_name = simple_evolution.group(1).strip()
            # 排除已经匹配的其他类型（如“最终形态”、“第二阶段”等）
            if not re.search(r'(最终|第二|第三|第一|第四|第五|阶段|阶)', query):
                return {
                    'type': 'pet_evolution_query',
                    'pet_name': pet_name
                }

        # "某精灵进化后的样子"、"进化后变成什么"
        pet_after_evolution = re.search(r'([^\s的]+?)(?:进化后的样子|进化后变成|进化后会变成|进化后是什么)', query)
        if pet_after_evolution:
            return {
                'type': 'pet_after_evolution_query',
                'pet_name': pet_after_evolution.group(1).strip()
            }

        # "某精灵以前是什么样子"、"进化前是什么"
        pet_before_evolution = re.search(r'([^\s的]+?)(?:以前是什么样子|进化前是什么|进化前的样子|之前是什么)', query)
        if pet_before_evolution:
            return {
                'type': 'pet_before_evolution_query',
                'pet_name': pet_before_evolution.group(1).strip()
            }

        # "某精灵完整进化链"、"进化路线"
        pet_full_evolution = re.search(r'([^\s]+?)(?:的)?(?:完整进化链|进化链|进化路线|全部进化)', query)
        if pet_full_evolution:
            pet_name = pet_full_evolution.group(1).strip()
            # 排除“所有”、“全部”等修饰词
            pet_name = re.sub(r'(所有|全部)$', '', pet_name).strip()
            # 去除末尾的“的”字
            pet_name = pet_name.rstrip('的').strip()
            if pet_name:  # 确保宠物名不为空
                return {
                    'type': 'pet_full_evolution_query',
                    'pet_name': pet_name
                }

        # 3.8.5 特定阶段形态查询：“墨鱿士的第二阶段”、“秩序鱿墨的第三阶段”、“迪莫的第2阶”
        # 支持有“的”和无“的”两种格式
        pet_stage_query = re.search(r'([^\s]+?)(?:的)?(?:第([一二三四五]|[1-5])阶段|第([一二三四五]|[1-5])阶|([一二三四五]|[1-5])阶)', query)
        if pet_stage_query:
            pet_name = pet_stage_query.group(1).strip()
            # 获取阶段数字（中文或阿拉伯数字）- 支持3种捕获组
            stage_num_str = pet_stage_query.group(2) or pet_stage_query.group(3) or pet_stage_query.group(4)
            # 转换中文数字为阿拉伯数字
            chinese_to_num = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5}
            stage_num = chinese_to_num.get(stage_num_str, int(stage_num_str) if stage_num_str and stage_num_str.isdigit() else None)

            if stage_num:
                return {
                    'type': 'pet_stage_query',
                    'pet_name': pet_name,
                    'stage_number': stage_num
                }

        # 3.9 宠物六维/种族值查询：“迪莫的种族值”、“幻灵菇的六维”、“迪莫各项属性”
        pet_stats_query = re.search(r'(\S+?)(?:的种族值|的六维|各项属性|详细属性|面板数据)', query)
        if pet_stats_query:
            return {
                'type': 'pet_stats_query',
                'pet_name': pet_stats_query.group(1).strip()
            }

        # 5. 属性筛选：“HP大于100的宠物”、“攻击力大于80”
        stat_patterns = [
            (r'HP(?:大于|>|超过|高于)\s*(\d+)', 'hp'),
            (r'(?:攻击|物攻|攻击力)(?:大于|>|超过|高于)\s*(\d+)', 'physical_attack'),
            (r'(?:魔攻|特攻|魔法攻击)(?:大于|>|超过|高于)\s*(\d+)', 'magic_attack'),
            (r'(?:防御|物防|物理防御)(?:大于|>|超过|高于)\s*(\d+)', 'physical_defense'),
            (r'(?:魔防|特防|魔法防御)(?:大于|>|超过|高于)\s*(\d+)', 'magic_defense'),
            (r'(?:速度|速)(?:大于|>|超过|高于)\s*(\d+)', 'speed'),
            # 小于筛选
            (r'HP(?:小于|<|低于)\s*(\d+)', 'hp_lt'),
            (r'(?:攻击|物攻|攻击力)(?:小于|<|低于)\s*(\d+)', 'physical_attack_lt'),
            (r'(?:魔攻|特攻|魔法攻击)(?:小于|<|低于)\s*(\d+)', 'magic_attack_lt'),
            (r'(?:防御|物防|物理防御)(?:小于|<|低于)\s*(\d+)', 'physical_defense_lt'),
            (r'(?:魔防|特防|魔法防御)(?:小于|<|低于)\s*(\d+)', 'magic_defense_lt'),
            (r'(?:速度|速)(?:小于|<|低于)\s*(\d+)', 'speed_lt'),
        ]

        for pattern, stat_key in stat_patterns:
            match = re.search(pattern, query)
            if match:
                is_less_than = stat_key.endswith('_lt')
                stat_name = stat_key.replace('_lt', '')
                return {
                    'type': 'pet_stat',
                    'stat_name': stat_name,
                    'min_value': int(match.group(1)),
                    'is_less_than': is_less_than
                }

        # 6. 更新日志查询：“最近的平衡调整”、“最近有什么更新”、“迪莫被削弱了吗”
        update_log_patterns = [
            r'(?:最近|最新).*(?:平衡|调整|更新|改动)',
            r'(?:平衡|调整|更新|改动).*(?:最近|最新|有哪些|是什么)',
            r'.*(?:被削|被强|削弱|加强|增强|nerf|buff).*',
        ]

        for pattern in update_log_patterns:
            if re.search(pattern, query):
                # 提取可能提到的宠物/技能名称
                mentioned_name = None
                name_match = re.search(r'(.+?)(?:被削|被强|削弱|加强|增强|nerf|buff)', query)
                if name_match:
                    mentioned_name = name_match.group(1).strip()

                return {
                    'type': 'update_log_query',
                    'mentioned_name': mentioned_name
                }

        return None

    def _handle_type_query(self, type_match: Dict[str, Any]) -> str:
        """
        处理智能查询

        Args:
            type_match: 解析后的查询信息

        Returns:
            格式化后的回复
        """
        query_type = type_match.get('type')

        # 0. 宠物编号查询
        if query_type == 'pet_id':
            pet_id = type_match['pet_id']
            pets = self.db_service.get_pet_info(str(pet_id), fuzzy=False, limit=1)

            if pets:
                return self._format_pet_response(pets[0])
            else:
                return f"❌ 未找到编号为 {pet_id} 的宠物"

        # 3.5 特定宠物技能查询
        elif query_type == 'pet_skills_query':
            pet_name = type_match['pet_name']
            pets = self.db_service.get_pet_info(pet_name, fuzzy=True, limit=1)

            if pets:
                pet = pets[0]
                name = pet.get('name', '未知')
                pet_skills = pet.get('skills', '')
                bloodline_skills = pet.get('bloodline_skills', '')
                learnable_skill_stones = pet.get('learnable_skill_stones', '')

                response = f"🎯 **{name}** 的技能:\n\n"

                # 普通技能
                if pet_skills:
                    skills_list = self._parse_list_field(pet_skills)
                    if skills_list:
                        response += f"📚 **技能列表** ({len(skills_list)}个):\n"
                        for i in range(0, len(skills_list), 3):
                            chunk = skills_list[i:i+3]
                            response += "  • " + " | ".join(chunk) + "\n"
                else:
                    response += "⚠️ 暂无普通技能信息\n"

                # 血脉技能
                if bloodline_skills:
                    bloodline_list = self._parse_list_field(bloodline_skills)
                    if bloodline_list and len(bloodline_list) > 0:
                        response += f"\n🧬 **血脉技能** ({len(bloodline_list)}个):\n"
                        for i in range(0, len(bloodline_list), 3):
                            chunk = bloodline_list[i:i+3]
                            response += "  • " + " | ".join(chunk) + "\n"

                # 可学技能石
                if learnable_skill_stones:
                    stone_list = self._parse_list_field(learnable_skill_stones)
                    if stone_list and len(stone_list) > 0:
                        response += f"\n💎 **可学技能石** ({len(stone_list)}个):\n"
                        for i in range(0, len(stone_list), 4):
                            chunk = stone_list[i:i+4]
                            response += "  • " + " | ".join(chunk) + "\n"

                return response
            else:
                return f"❌ 未找到宠物 '{pet_name}'"

        # 3.6 宠物特性查询
        elif query_type == 'pet_ability_query':
            pet_name = type_match['pet_name']
            pets = self.db_service.get_pet_info(pet_name, fuzzy=True, limit=1)

            if pets:
                pet = pets[0]
                ability = pet.get('ability', '无')
                ability_desc = pet.get('ability_desc', '')

                response = f"✨ **{pet['name']}** 的特性:\n\n"
                response += f"🎯 **{ability}**\n"
                if ability_desc:
                    response += f"> {ability_desc}\n"
                else:
                    response += "> 暂无详细描述\n"
                return response
            else:
                return f"❌ 未找到宠物 '{pet_name}'"

        # 3.7 宠物分布/位置查询
        elif query_type == 'pet_location_query':
            pet_name = type_match['pet_name']
            pets = self.db_service.get_pet_info(pet_name, fuzzy=True, limit=1)

            if pets:
                pet = pets[0]
                distribution = pet.get('distribution', '')

                response = f"📍 **{pet['name']}** 的分布信息:\n\n"
                if distribution:
                    response += f"🌍 **出现地点:** {distribution}\n"
                else:
                    response += "⚠️ 暂无分布信息\n"

                # 如果有体型信息，也显示
                size = pet.get('size', '')
                weight = pet.get('weight', '')
                if size or weight:
                    response += f"\n📏 **体型信息:**\n"
                    if size:
                        response += f"  • 身高: {size}m\n"
                    if weight:
                        response += f"  • 体重: {weight}kg\n"

                return response
            else:
                return f"❌ 未找到宠物 '{pet_name}'"

        # 3.8 宠物进化查询
        elif query_type == 'pet_evolution_query':
            pet_name = type_match['pet_name']

            # 尝试获取所有进化分支
            all_chains = self.db_service.get_pet_all_evolution_chains(pet_name)

            if not all_chains:
                return f"❌ 未找到宠物 '{pet_name}'"

            response = f"🔄 **{pet_name}** 的进化信息:\n\n"

            # 检查是否有进化信息
            has_evolution = False
            for chain in all_chains:
                if len(chain.get('stages', [])) > 1:
                    has_evolution = True
                    break

            if not has_evolution:
                return f"ℹ️ {pet_name} 暂无进化信息"

            # 如果有多个分支，显示树状分支
            if len(all_chains) > 1:
                response += f"⚠️ {pet_name} 有 {len(all_chains)} 个进化分支:\n\n"

                # 找到分岔点（共同的前缀）
                common_stages = []
                first_chain_stages = all_chains[0]['stages']

                for i, stage in enumerate(first_chain_stages):
                    is_common = True
                    for chain in all_chains[1:]:
                        if i >= len(chain['stages']) or chain['stages'][i].get('name') != stage.get('name'):
                            is_common = False
                            break
                    if is_common:
                        common_stages.append(stage)
                    else:
                        break

                # 显示共同路径
                if common_stages:
                    response += "**共同进化路径:**\n"
                    for i, stage in enumerate(common_stages):
                        stage_name = stage.get('name', '')

                        response += f"  {i+1}. {stage_name}\n"

                        # 显示从这个阶段进化到下一个阶段的条件
                        if i < len(common_stages) - 1:
                            # level存储在当前阶段，表示从当前阶段进化到下一阶段需要的等级
                            if stage.get('level'):
                                response += f"     🎯 等级: {stage.get('level')}\n"
                            if stage.get('condition'):
                                response += f"     🔮 条件: {stage.get('condition')}\n"
                    response += "\n"

                # 显示各个分支
                for idx, chain in enumerate(all_chains, 1):
                    # 找到分岔后的阶段
                    diverged_stages = chain['stages'][len(common_stages):]

                    if diverged_stages:
                        response += f"**分支{idx}:**\n"
                        for i, stage in enumerate(diverged_stages):
                            stage_name = stage.get('name', '')

                            indent = "  " if i == 0 else "    "
                            response += f"{indent}{len(common_stages)+i+1}. {stage_name}\n"

                            # 显示进化条件（level存储在当前阶段）
                            if stage.get('level'):
                                response += f"{indent}   🎯 等级: {stage.get('level')}\n"
                            if stage.get('condition'):
                                response += f"{indent}   🔮 条件: {stage.get('condition')}\n"
                        response += "\n"
            else:
                # 单条进化链，显示完整路线
                chain = all_chains[0]
                stages = chain.get('stages', [])

                if stages:
                    response += "**进化路线:**\n"
                    for i, stage in enumerate(stages):
                        stage_name = stage.get('name', '')

                        response += f"{i+1}. {stage_name}\n"

                        # 显示进化到下一个阶段的条件
                        if i < len(stages) - 1:
                            # level存储在当前阶段，表示从当前阶段进化到下一阶段需要的等级
                            if stage.get('level'):
                                response += f"   🎯 等级: {stage.get('level')}\n"
                            if stage.get('condition'):
                                response += f"   🔮 条件: {stage.get('condition')}\n"

            return response

        # 3.8.05 下一阶段查询 - 只返回下一个进化形态
        elif query_type == 'pet_next_stage_query':
            pet_name = type_match['pet_name']

            # 尝试获取所有进化分支
            all_chains = self.db_service.get_pet_all_evolution_chains(pet_name)

            if not all_chains:
                return f"❌ 未找到宠物 '{pet_name}'"

            # 收集所有下一个阶段
            next_stages = []
            for chain in all_chains:
                next_stage = chain.get('next_stage')
                if next_stage:
                    next_stages.append({
                        'stage': next_stage,
                        'chain': chain
                    })

            if not next_stages:
                return f"✅ {pet_name} 已经是最终形态，没有后续进化了"

            response = f"➡️ **{pet_name}** 的下一阶段:\n\n"

            # 如果有多个分支，显示所有分支
            if len(next_stages) > 1:
                response += f"✨ {pet_name} 有 {len(next_stages)} 个进化方向:\n\n"

                for idx, next_info in enumerate(next_stages, 1):
                    next_stage = next_info['stage']
                    chain = next_info['chain']
                    stages = chain.get('stages', [])

                    response += f"**分支{idx}:** ✨ **{next_stage.get('name', '')}** (第{next_stage.get('stage', '?')}阶)\n"

                    if next_stage.get('level'):
                        response += f"   🎯 进化等级: {next_stage.get('level')}\n"
                    if next_stage.get('condition'):
                        response += f"   🔮 进化条件: {next_stage.get('condition')}\n"

                    # 显示完整进化路线
                    if len(stages) > 1:
                        stage_names = [s.get('name', '') for s in stages]
                        response += f"   📈 路线: {' → '.join(stage_names)}\n"

                    response += "\n"
            else:
                # 单条进化链
                next_stage = next_stages[0]['stage']
                chain = next_stages[0]['chain']
                stages = chain.get('stages', [])

                response += f"✨ **{next_stage.get('name', '')}** (第{next_stage.get('stage', '?')}阶)\n"

                if next_stage.get('level'):
                    response += f"🎯 **进化等级:** {next_stage.get('level')}\n"
                if next_stage.get('condition'):
                    response += f"🔮 **进化条件:** {next_stage.get('condition')}\n"

                # 显示完整进化路线
                if len(stages) > 1:
                    response += f"\n📈 **完整进化路线:**\n"
                    stage_names = [s.get('name', '') for s in stages]
                    response += " → ".join(stage_names) + "\n"

            return response

        # 3.8.1 最终形态查询
        elif query_type == 'pet_final_form_query':
            pet_name = type_match['pet_name']

            # 获取插件目录
            plugin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wiki')

            # 使用多分支查询方法
            all_chains = self.db_service.get_pet_all_evolution_chains(pet_name)

            if not all_chains:
                return f"❌ 未找到宠物 '{pet_name}'"

            # 收集所有最终形态
            final_forms = []
            for chain in all_chains:
                stages = chain.get('stages', [])
                if stages:
                    final_stage = stages[-1]  # 最后一个阶段是最终形态
                    final_forms.append({
                        'stage': final_stage,
                        'chain': chain
                    })

            if not final_forms:
                return f"⚠️ {pet_name} 暂无进化链信息"

            response = f"🏆 **{pet_name}** 的最终形态:\n\n"

            # 如果有多个最终形态（多分支）
            if len(final_forms) > 1:
                response += f"✨ {pet_name} 有 {len(final_forms)} 个最终形态:\n\n"

                # 收集所有最终形态的图片
                image_paths = []

                for idx, form_info in enumerate(final_forms, 1):
                    final_stage = form_info['stage']
                    chain = form_info['chain']
                    stages = chain.get('stages', [])

                    response += f"**分支{idx}:** ✨ **{final_stage.get('name', '')}** (第{final_stage.get('stage', '?')}阶)\n"

                    if final_stage.get('level'):
                        response += f"   🎯 达到等级: {final_stage.get('level')}\n"
                    if final_stage.get('condition'):
                        response += f"   🔮 进化条件: {final_stage.get('condition')}\n"

                    # 显示完整进化路线
                    if len(stages) > 1:
                        stage_names = [s.get('name', '') for s in stages]
                        response += f"   📈 路线: {' → '.join(stage_names)}\n"

                    # 尝试获取该分支最终形态的图片
                    final_pet_name = final_stage.get('name', '')
                    if final_pet_name:
                        # 先尝试精确搜索，如果找不到再尝试模糊搜索
                        pets = self.db_service.get_pet_info(final_pet_name, fuzzy=False, limit=1)
                        if not pets:
                            pets = self.db_service.get_pet_info(final_pet_name, fuzzy=True, limit=1)

                        if pets:
                            img_path = pets[0].get('sprite_image_local')
                            if img_path:
                                # 清理路径前缀
                                if img_path.startswith('./') or img_path.startswith('.\\'):
                                    img_path = img_path[2:]
                                # 如果是相对路径，基于插件目录解析
                                if not os.path.isabs(img_path):
                                    img_path = self._resolve_wiki_path(img_path)
                                if os.path.exists(img_path):
                                    image_paths.append(img_path)
                                    logger.info(f"🖼️ 分支{idx} '{final_pet_name}' 的图片: {img_path}")

                    response += "\n"

                # 添加数据来源声明
                response += DATA_SOURCE_NOTICE

                # 如果有图片，返回所有图片（最多2张）
                if image_paths:
                    return {
                        'text': response,
                        'image_paths': image_paths[:2]  # 最多返回前2个分支的图片
                    }
                else:
                    return response
            else:
                # 单个最终形态
                final_stage = final_forms[0]['stage']
                chain = final_forms[0]['chain']
                stages = chain.get('stages', [])

                response += f"✨ **{final_stage.get('name', '')}** (第{final_stage.get('stage', '?')}阶)\n"

                if final_stage.get('level'):
                    response += f"🎯 **达到等级:** {final_stage.get('level')}\n"
                if final_stage.get('condition'):
                    response += f"🔮 **进化条件:** {final_stage.get('condition')}\n"

                # 显示完整进化链
                if len(stages) > 1:
                    response += f"\n📈 **完整进化路线:**\n"
                    stage_names = [s.get('name', '') for s in stages]
                    response += " → ".join(stage_names) + "\n"

                # 添加数据来源声明
                response += DATA_SOURCE_NOTICE

                # 尝试获取最终形态宠物的图片
                final_pet_name = final_stage.get('name', '')
                image_path = None

                if final_pet_name:
                    # 先尝试精确搜索，如果找不到再尝试模糊搜索
                    pets = self.db_service.get_pet_info(final_pet_name, fuzzy=False, limit=1)
                    if not pets:
                        pets = self.db_service.get_pet_info(final_pet_name, fuzzy=True, limit=1)

                    if pets:
                        image_path = pets[0].get('sprite_image_local')

                # 处理图片路径
                if image_path:
                    # 清理路径前缀
                    if image_path.startswith('./') or image_path.startswith('.\\'):
                        image_path = image_path[2:]
                    # 如果是相对路径，基于插件目录解析
                    if not os.path.isabs(image_path):
                        image_path = self._resolve_wiki_path(image_path)
                    logger.info(f"🖼️ 最终形态 '{final_pet_name}' 的图片路径: {image_path}")
                    logger.info(f"🖼️ 文件是否存在: {os.path.exists(image_path)}")

                # 如果有图片，返回字典；否则返回纯文本
                if image_path and os.path.exists(image_path):
                    return {
                        'text': response,
                        'image_path': image_path
                    }
                else:
                    return response

        # 3.8.2 进化后形态查询
        elif query_type == 'pet_after_evolution_query':
            pet_name = type_match['pet_name']

            # 尝试获取所有进化分支
            all_chains = self.db_service.get_pet_all_evolution_chains(pet_name)

            if not all_chains:
                return f"❌ 未找到宠物 '{pet_name}'"

            # 如果有多个分支，显示所有分支
            if len(all_chains) > 1:
                response = f"➡️ **{pet_name}** 可以进化为以下形态:\n\n"

                for idx, chain in enumerate(all_chains, 1):
                    next_stage = chain.get('next_stage')
                    if next_stage:
                        stage_name = next_stage.get('name', '')

                        response += f"**分支{idx}:** ✨ {stage_name} (第{next_stage.get('stage', '?')}阶)\n"
                        if next_stage.get('level'):
                            response += f"   🎯 进化等级: {next_stage.get('level')}\n"
                        if next_stage.get('condition'):
                            response += f"   🔮 进化条件: {next_stage.get('condition')}\n"
                        response += "\n"

                return response

            # 单条进化链
            chain = all_chains[0]
            next_stage = chain.get('next_stage')

            if not next_stage:
                return f"✅ {pet_name} 已经是最终形态，没有后续进化了"

            stage_name = next_stage.get('name', '')

            response = f"➡️ **{pet_name}** 进化后的形态:\n\n"
            response += f"✨ **{stage_name}** (第{next_stage.get('stage', '?')}阶)\n"

            if next_stage.get('level'):
                response += f"🎯 **进化等级:** {next_stage.get('level')}\n"
            if next_stage.get('condition'):
                response += f"🔮 **进化条件:** {next_stage.get('condition')}\n"

            return response

        # 3.8.3 进化前形态查询
        elif query_type == 'pet_before_evolution_query':
            pet_name = type_match['pet_name']
            evolution_info = self.db_service.get_pet_evolution_chain(pet_name)

            if not evolution_info:
                return f"❌ 未找到宠物 '{pet_name}'"

            if not evolution_info.get('stages'):
                return f"⚠️ {pet_name} 暂无进化链信息"

            previous_stage = evolution_info.get('previous_stage')
            if not previous_stage:
                return f"ℹ️ {pet_name} 是初始形态，没有进化前的形态"

            response = f"⬅️ **{pet_name}** 进化前的形态:\n\n"
            response += f"✨ **{previous_stage.get('name', '')}** (第{previous_stage.get('stage', '?')}阶)\n"

            if previous_stage.get('level'):
                response += f"🎯 **进化等级:** {previous_stage.get('level')}\n"
            if previous_stage.get('condition'):
                response += f"🔮 **进化条件:** {previous_stage.get('condition')}\n"

            return response

        # 3.8.4 完整进化链查询
        elif query_type == 'pet_full_evolution_query':
            pet_name = type_match['pet_name']

            # 使用新的多分支查询方法
            all_chains = self.db_service.get_pet_all_evolution_chains(pet_name)

            if not all_chains:
                return f"❌ 未找到宠物 '{pet_name}'"

            response = f"🔄 **{pet_name}** 的完整进化链:\n\n"

            # 如果有多个分支，显示树状结构
            if len(all_chains) > 1:
                # 找到共同路径
                common_stages = []
                first_chain_stages = all_chains[0]['stages']

                for i, stage in enumerate(first_chain_stages):
                    is_common = True
                    for chain in all_chains[1:]:
                        if i >= len(chain['stages']) or chain['stages'][i].get('name') != stage.get('name'):
                            is_common = False
                            break
                    if is_common:
                        common_stages.append(stage)
                    else:
                        break

                # 显示共同路径
                if common_stages:
                    response += "**共同进化路径:**\n"
                    for i, stage in enumerate(common_stages):
                        stage_name = stage.get('name', '')
                        response += f"  {i+1}. {stage_name}\n"

                        if i < len(common_stages) - 1:
                            next_stage = common_stages[i+1]
                            if next_stage.get('level'):
                                response += f"     🎯 等级: {next_stage.get('level')}\n"
                            if next_stage.get('condition'):
                                response += f"     🔮 条件: {next_stage.get('condition')}\n"
                    response += "\n"

                # 显示各个分支
                for idx, chain in enumerate(all_chains, 1):
                    diverged_stages = chain['stages'][len(common_stages):]

                    if diverged_stages:
                        response += f"**分支{idx}:**\n"
                        for i, stage in enumerate(diverged_stages):
                            stage_name = stage.get('name', '')
                            indent = "  " if i == 0 else "    "
                            response += f"{indent}{len(common_stages)+i+1}. {stage_name}\n"

                            if stage.get('level'):
                                response += f"{indent}   🎯 等级: {stage.get('level')}\n"
                            if stage.get('condition'):
                                response += f"{indent}   🔮 条件: {stage.get('condition')}\n"
                        response += "\n"
            else:
                # 单条进化链
                chain = all_chains[0]
                stages = chain.get('stages', [])

                if stages:
                    response += "**进化路线:**\n"
                    for i, stage in enumerate(stages):
                        stage_name = stage.get('name', '')
                        response += f"{i+1}. {stage_name}\n"

                        if i < len(stages) - 1:
                            next_stage = stages[i+1]
                            if next_stage.get('level'):
                                response += f"   🎯 等级: {next_stage.get('level')}\n"
                            if next_stage.get('condition'):
                                response += f"   🔮 条件: {next_stage.get('condition')}\n"
                        elif i == len(stages) - 1 and i > 0:
                            if stage.get('level'):
                                response += f"   🎯 等级: {stage.get('level')}\n"
                            if stage.get('condition'):
                                response += f"   🔮 条件: {stage.get('condition')}\n"

            return response

        # 3.9 宠物六维/种族值查询
        elif query_type == 'pet_stats_query':
            pet_name = type_match['pet_name']
            pets = self.db_service.get_pet_info(pet_name, fuzzy=True, limit=1)

            if pets:
                pet = pets[0]
                hp = pet.get('hp', 0)
                pa = pet.get('physical_attack', 0)
                ma = pet.get('magic_attack', 0)
                pd = pet.get('physical_defense', 0)
                md = pet.get('magic_defense', 0)
                spd = pet.get('speed', 0)
                total = hp + pa + ma + pd + md + spd

                response = f"📊 **{pet['name']}** 的种族值:\n\n"
                response += f"❤️ HP: {hp}\n"
                response += f"💪 物攻: {pa}\n"
                response += f"🔮 魔攻: {ma}\n"
                response += f"🛡️ 物防: {pd}\n"
                response += f"✨ 魔防: {md}\n"
                response += f"⚡ 速度: {spd}\n"
                response += f"\n📈 **总和:** {total}\n"

                # 计算平均值和最高属性
                stats = {'HP': hp, '物攻': pa, '魔攻': ma, '物防': pd, '魔防': md, '速度': spd}
                max_stat = max(stats, key=stats.get)
                min_stat = min(stats, key=stats.get)
                avg_stat = total / 6

                response += f"\n📊 **分析:**\n"
                response += f"  • 最高: {max_stat} ({stats[max_stat]})\n"
                response += f"  • 最低: {min_stat} ({stats[min_stat]})\n"
                response += f"  • 平均: {avg_stat:.1f}\n"

                return response
            else:
                return f"❌ 未找到宠物 '{pet_name}'"

        # 3.8.5 特定阶段形态查询
        elif query_type == 'pet_stage_query':
            pet_name = type_match['pet_name']
            stage_number = type_match.get('stage_number')  # 可能为None（如果是模糊查询）

            # 获取插件目录
            plugin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wiki')

            # 如果有明确的阶段数字，尝试从进化链中查找
            if stage_number:
                all_chains = self.db_service.get_pet_all_evolution_chains(pet_name)

                if not all_chains:
                    return f"❌ 未找到宠物 '{pet_name}'"

                # 在所有进化分支中查找指定阶段
                target_stages = []  # 可能有多个分支的同一阶段

                for chain in all_chains:
                    stages = chain.get('stages', [])
                    for stage in stages:
                        if stage.get('stage') == stage_number:
                            target_stages.append({
                                'stage': stage,
                                'chain': chain
                            })
                            break

                if not target_stages:
                    return f"❌ {pet_name} 没有第{stage_number}阶段"

                # 如果只有一个阶段，直接显示
                if len(target_stages) == 1:
                    target_stage = target_stages[0]['stage']
                    target_chain = target_stages[0]['chain']

                    # 获取该阶段宠物的详细信息
                    stage_pet_name = target_stage.get('name', '')
                    if not stage_pet_name:
                        return f"❌ 无法获取第{stage_number}阶段的宠物名称"

                    # 先尝试精确搜索，如果找不到再尝试模糊搜索
                    pets = self.db_service.get_pet_info(stage_pet_name, fuzzy=False, limit=1)
                    if not pets:
                        logger.info(f"⚠️ 精确搜索未找到 '{stage_pet_name}'，尝试模糊搜索")
                        pets = self.db_service.get_pet_info(stage_pet_name, fuzzy=True, limit=1)

                    # 即使找不到宠物记录，也显示进化链中的信息
                    response = f"🐾 **{stage_pet_name}** ({pet_name}的第{stage_number}阶段)\n"
                    response += "━━━━━━━━━━━━━━\n"

                    if pets:
                        # 有宠物记录，显示详细信息
                        pet = pets[0]

                        # 显示基本信息
                        if pet.get('element'):
                            response += f"🔮 属性: {pet['element']}系\n"
                        if pet.get('stage'):
                            response += f"📊 阶段: {pet['stage']}\n"
                        if pet.get('form') and pet['form'] != '原始形态':
                            response += f"✨ 形态: {pet['form']}\n"

                        # 显示描述
                        if pet.get('description'):
                            response += f"\n📝 **介绍:**\n{pet['description']}\n"

                        # 尝试获取图片路径
                        image_path = pet.get('sprite_image_local')
                    else:
                        # 没有宠物记录，仅显示提示信息
                        response += f"\n⚠️ 暂无该宠物的详细信息\n"
                        image_path = None

                    # 显示进化条件
                    if target_stage.get('level'):
                        response += f"🎯 进化等级: {target_stage.get('level')}\n"
                    if target_stage.get('condition'):
                        response += f"🔮 进化条件: {target_stage.get('condition')}\n"

                    # 添加数据来源声明
                    response += DATA_SOURCE_NOTICE

                    # 处理图片路径
                    if image_path:
                        logger.info(f"🖼️ 宠物 '{stage_pet_name}' (第{stage_number}阶段) 的图片路径: {image_path}")
                        # 清理路径前缀（移除 ./ 或 .\）
                        if image_path.startswith('./') or image_path.startswith('.\\'):
                            image_path = image_path[2:]
                        # 如果是相对路径，基于插件目录解析
                        if not os.path.isabs(image_path):
                            image_path = self._resolve_wiki_path(image_path)
                        # 规范化路径分隔符
                        image_path = image_path.replace('\\', '/')
                        logger.info(f"🖼️ 解析后的完整路径: {image_path}")
                        logger.info(f"🖼️ 文件是否存在: {os.path.exists(image_path)}")

                    if image_path and os.path.exists(image_path):
                        # 返回包含图片的字典
                        return {
                            'text': response,
                            'image_path': image_path
                        }
                    else:
                        # 没有图片，只返回文本
                        return response
                else:
                    # 有多个分支的同一阶段，显示所有分支
                    response = f"🐾 **{pet_name}** 的第{stage_number}阶段有 {len(target_stages)} 个分支:\n\n"

                    # 收集所有分支的图片
                    image_paths = []

                    for idx, target_info in enumerate(target_stages, 1):
                        target_stage = target_info['stage']
                        target_chain = target_info['chain']
                        stage_pet_name = target_stage.get('name', '')

                        response += f"**分支{idx}:** ✨ **{stage_pet_name}**\n"

                        if target_stage.get('level'):
                            response += f"   🎯 进化等级: {target_stage.get('level')}\n"
                        if target_stage.get('condition'):
                            response += f"   🔮 进化条件: {target_stage.get('condition')}\n"

                        # 显示该分支的完整路线
                        stages = target_chain.get('stages', [])
                        if len(stages) > 1:
                            stage_names = [s.get('name', '') for s in stages]
                            response += f"   📈 路线: {' → '.join(stage_names)}\n"

                        # 尝试获取该分支宠物的图片
                        if stage_pet_name:
                            # 先尝试精确搜索，如果找不到再尝试模糊搜索
                            pets = self.db_service.get_pet_info(stage_pet_name, fuzzy=False, limit=1)
                            if not pets:
                                pets = self.db_service.get_pet_info(stage_pet_name, fuzzy=True, limit=1)

                            if pets:
                                img_path = pets[0].get('sprite_image_local')
                                if img_path:
                                    # 清理路径前缀
                                    if img_path.startswith('./') or img_path.startswith('.\\'):
                                        img_path = img_path[2:]
                                    # 如果是相对路径，基于插件目录解析
                                    if not os.path.isabs(img_path):
                                        img_path = self._resolve_wiki_path(img_path)
                                    if os.path.exists(img_path):
                                        image_paths.append(img_path)
                                        logger.info(f"🖼️ 分支{idx} '{stage_pet_name}' 的图片: {img_path}")
                            else:
                                logger.info(f"⚠️ 分支{idx} '{stage_pet_name}' 无宠物记录")

                        response += "\n"

                    # 添加数据来源声明
                    response += DATA_SOURCE_NOTICE

                    # 如果有图片，返回所有图片（最多2张）
                    if image_paths:
                        return {
                            'text': response,
                            'image_paths': image_paths[:2]  # 最多返回前2个分支的图片
                        }
                    else:
                        return response

            # 如果没有明确的阶段数字，使用原有的模糊查询逻辑
            pets = self.db_service.get_pet_info(pet_name, fuzzy=True, limit=1)

            if not pets:
                return f"❌ 未找到宠物 '{pet_name}'"

            pet = pets[0]
            response = f"🐾 **{pet['name']}**\n"
            response += "━━━━━━━━━━━━━━\n"

            # 显示基本信息
            if pet.get('element'):
                response += f"🔮 属性: {pet['element']}系\n"
            if pet.get('stage'):
                response += f"📊 阶段: {pet['stage']}\n"
            if pet.get('form') and pet['form'] != '原始形态':
                response += f"✨ 形态: {pet['form']}\n"
            if pet.get('rarity'):
                response += f"💎 稀有度: {pet['rarity']}\n"

            # 显示描述
            if pet.get('description'):
                response += f"\n📝 **介绍:**\n{pet['description']}\n"

            # 尝试获取图片路径
            image_path = None
            if pet.get('sprite_image_local'):
                image_path = pet['sprite_image_local']
                # 清理路径前缀
                if image_path.startswith('./') or image_path.startswith('.\\'):
                    image_path = image_path[2:]
                # 如果是相对路径，转换为绝对路径
                if not os.path.isabs(image_path):
                    image_path = self._resolve_wiki_path(image_path)

            # 如果有图片，返回字典；否则返回纯文本
            if image_path and os.path.exists(image_path):
                return {
                    'text': response,
                    'image_path': image_path
                }
            else:
                return response

        # 1. 属性克制查询
        if query_type == 'type_advantage':
            attack = type_match['attack_type']
            defense = type_match['defense_type']

            multiplier = self.db_service.get_type_advantage(attack, defense)

            if multiplier is not None:
                if multiplier > 1:
                    return f"⚔️ **{attack}系** 克制 **{defense}系**\n伤害倍率: **{multiplier}x**"
                elif multiplier == 1:
                    return f"⚖️ **{attack}系** 对 **{defense}系** 无克制关系\n伤害倍率: **1.0x**"
                elif multiplier == 0:
                    return f"🛡️ **{defense}系** 免疫 **{attack}系**\n伤害倍率: **0x**"
                else:
                    return f"🛡️ **{defense}系** 抵抗 **{attack}系**\n伤害倍率: **{multiplier}x**"
            else:
                return f"❌ 未找到 **{attack}系** 和 **{defense}系** 的克制关系"

        # 2. 单属性完整克制关系
        elif query_type == 'type_summary':
            element = type_match['element']
            summary = self.db_service.get_type_chart_summary(element)

            response = f"📊 **{summary['element']}系** 克制关系:\n\n"

            if summary['strong_against']:
                response += f"✅ **克制:** {', '.join(summary['strong_against'])}\n"

            if summary['weak_against']:
                response += f"❌ **被克:** {', '.join(summary['weak_against'])}\n"

            if summary['immune_to']:
                response += f"🛡️ **免疫:** {', '.join(summary['immune_to'])}\n"

            if summary['no_effect']:
                response += f"⚠️ **抵抗:** {', '.join(summary['no_effect'])}\n"

            if not any([summary['strong_against'], summary['weak_against'],
                       summary['immune_to'], summary['no_effect']]):
                response += "暂无克制关系数据"

            return response

        # 3. 技能威力排行
        elif query_type == 'top_skills':
            element = type_match.get('element')
            skills = self.db_service.get_top_skills_by_power(element, limit=5)

            if not skills:
                return f"❌ 未找到{' ' + element + '系' if element else ''}技能数据"

            response = f"🏆 **{'最强' + element + '系' if element else '最高威力'}技能 TOP 5:**\n\n"
            for i, skill in enumerate(skills, 1):
                medal = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣'][i-1]
                response += f"{medal} **{skill['name']}** ({skill['element']}系)\n"
                response += f"   威力: {skill['power']} | 消耗: {skill['cost']} | 类型: {skill['category']}\n"

            return response

        # 2.5 单属性宠物查询
        elif query_type == 'pet_by_element':
            element = type_match['element']
            pets = self.db_service.search_pets_by_elements([element], limit=10)

            if not pets:
                return f"❌ 未找到 {element} 系的宠物"

            response = f"🔍 **{element}系宠物** (共{len(pets)}个):\n\n"
            page_size = self.page_size
            for i, pet in enumerate(pets[:page_size], 1):
                response += f"{i}. **{pet['name']}** ({pet['element']}系) HP:{pet['hp']}\n"

            if len(pets) > page_size:
                response += f"\n...还有 {len(pets) - page_size} 个\n"
                response += f"💡 提示：每页显示 {page_size} 个"

            return response

        # 4. 属性组合查询
        elif query_type == 'pet_elements':
            elements = type_match['elements']
            pets = self.db_service.search_pets_by_elements(elements, limit=10)

            if not pets:
                return f"❌ 未找到 {'+'.join(elements)} 系的宠物"

            response = f"🔍 **{'/'.join(elements)}系宠物** (共{len(pets)}个):\n\n"
            page_size = self.page_size
            for i, pet in enumerate(pets[:page_size], 1):
                response += f"{i}. **{pet['name']}** ({pet['element']}系) HP:{pet['hp']}\n"

            if len(pets) > page_size:
                response += f"\n...还有 {len(pets) - page_size} 个\n"
                response += f"💡 提示：每页显示 {page_size} 个"

            return response

        # 5. 属性筛选
        elif query_type == 'pet_stat':
            stat_name = type_match['stat_name']
            min_value = type_match['min_value']
            is_less_than = type_match.get('is_less_than', False)

            stat_names_cn = {
                'hp': 'HP',
                'physical_attack': '物攻',
                'magic_attack': '魔攻',
                'physical_defense': '物防',
                'magic_defense': '魔防',
                'speed': '速度'
            }

            pets = self.db_service.search_pets_by_stat(stat_name, min_value, limit=10)

            if not pets:
                operator = '<' if is_less_than else '>='
                return f"❌ 未找到 {stat_names_cn.get(stat_name, stat_name)}{operator}{min_value} 的宠物"

            # 如果是小于筛选，需要过滤结果
            if is_less_than:
                pets = [p for p in pets if p.get(stat_name, 0) < min_value]

            if not pets:
                operator = '<' if is_less_than else '>='
                return f"❌ 未找到 {stat_names_cn.get(stat_name, stat_name)}{operator}{min_value} 的宠物"

            operator = '<' if is_less_than else '>='
            response = f"📊 **{stat_names_cn.get(stat_name, stat_name)}{operator}{min_value} 的宠物** (共{len(pets)}个):\n\n"
            page_size = self.page_size
            for i, pet in enumerate(pets[:page_size], 1):
                stat_value = pet.get(stat_name, 0)
                response += f"{i}. **{pet['name']}** ({pet['element']}系) {stat_names_cn.get(stat_name, stat_name)}:{stat_value}\n"

            if len(pets) > page_size:
                response += f"\n...还有 {len(pets) - page_size} 个\n"
                response += f"💡 提示：每页显示 {page_size} 个"

            return response

        # 6. 更新日志查询
        elif query_type == 'update_log_query':
            mentioned_name = type_match.get('mentioned_name')

            if mentioned_name:
                # 搜索特定宠物/技能的改动
                logs = self.db_service.search_update_logs(mentioned_name, limit=5)
                if logs:
                    response = f"📝 **关于 '{mentioned_name}' 的平衡调整:**\n\n"
                    for log in logs[:3]:
                        response += f"📅 **{log['date']} - {log['title']}**\n"
                        response += f"> {log['content'][:200]}...\n\n"
                    return response
                else:
                    return f"❌ 未找到关于 '{mentioned_name}' 的平衡调整记录"
            else:
                # 获取最近的更新日志
                logs = self.db_service.get_latest_updates(limit=5)
                if logs:
                    response = f"📋 **最近的平衡调整:**\n\n"
                    for log in logs:
                        response += f"📅 **{log['date']} - {log['title']}**\n"

                        # 显示改动统计
                        pet_count = len(log.get('pet_changes', []))
                        skill_count = len(log.get('skill_changes', []))
                        other_count = len(log.get('other_changes', []))

                        if pet_count > 0:
                            response += f"  🐾 宠物改动: {pet_count}条\n"
                        if skill_count > 0:
                            response += f"  ⚔️ 技能改动: {skill_count}条\n"
                        if other_count > 0:
                            response += f"  🔧 其他改动: {other_count}条\n"

                        # 显示具体改动（前几个）
                        pet_changes = log.get('pet_changes', [])
                        if pet_changes:
                            names = [c.get('name', '') for c in pet_changes[:5]]
                            response += f"  👉 {'、'.join(names)}{'等' if len(pet_changes) > 5 else ''}\n"

                        skill_changes = log.get('skill_changes', [])
                        if skill_changes:
                            names = [c.get('name', '') for c in skill_changes[:5]]
                            response += f"  👉 {'、'.join(names)}{'等' if len(skill_changes) > 5 else ''}\n"

                        response += "\n"

                    return response
                else:
                    return "❌ 暂无更新日志记录"

        return "❌ 无法解析查询"

    async def _handle_admin_command_impl(self, event: AstrMessageEvent, command: str):
        """
        处理管理员命令（通过关键词触发，由 AstrBot 权限系统控制）

        Args:
            event: 事件对象
            command: 命令参数（去除前缀后的部分）
        """
        # 停止事件传播，防止被 Agent/LLM 拦截
        event.stop_event()

        # 解析命令
        parts = command.strip().split()
        if len(parts) < 1:
            yield event.plain_result(f"❌ 请提供命令\n用法: 洛克管理 <command>\n示例: 洛克管理 update")
            return

        cmd = parts[0].lower()

        # 执行命令
        if cmd == "update":
            async for msg in self._handle_update_db(event):
                yield event.plain_result(msg)
        elif cmd == "status":
            async for msg in self._handle_db_status(event):
                yield event.plain_result(msg)
        elif cmd == "tag-colors":
            async for msg in self._handle_tag_colors(event):
                yield event.plain_result(msg)
        elif cmd == "tag-pet-colors":
            async for msg in self._handle_tag_pet_colors(event):
                yield event.plain_result(msg)
        elif cmd == "force-tag-colors":
            async for msg in self._handle_force_tag_colors(event):
                yield event.plain_result(msg)
        elif cmd == "force-tag-pet-colors":
            async for msg in self._handle_force_tag_pet_colors(event):
                yield event.plain_result(msg)
        elif cmd == "fix-missing":
            async for msg in self._handle_fix_missing_data(event):
                yield event.plain_result(msg)
        elif cmd == "check-vision":
            async for msg in self._handle_check_vision_model(event):
                yield event.plain_result(msg)
        else:
            yield event.plain_result(f"❌ 未知命令: {cmd}\n\n📋 可用命令:\n  • update - 增量更新数据库\n  • status - 查看数据库状态\n  • tag-colors - 为道具标记颜色\n  • tag-pet-colors - 为宠物标记颜色\n  • force-tag-colors - 强制重新识别所有道具颜色\n  • force-tag-pet-colors - 强制重新识别所有宠物颜色\n  • fix-missing - 补全缺失的宠物数据\n  • check-vision - 检查视觉模型配置\n\n示例: 洛克管理 check-vision")

    @filter.command("查询", ["query", "wiki"])
    async def handle_query(self, event: AstrMessageEvent, content: str):
        """
        处理查询命令
        用法: /查询 <宠物/技能名称>
              /查询 <宠物/技能名称> 图片 (只返回图片)
        """

        # 参数验证
        if not content or len(content.strip()) < 1:
            yield "❌ 请输入要查询的宠物或技能名称！\n示例: /查询 喵喵\n示例: /查询 喵喵 图片"
            return

        content = content.strip()

        # 检查数据库服务是否可用
        if not self.db_service:
            yield "❌ 数据库服务不可用，请联系管理员检查配置"
            return

        # 检测是否是图片检索请求
        is_image_query, clean_content = self._extract_image_query(content)

        if is_image_query:
            logger.info(f"🖼️ 图片检索模式: {clean_content}")
            async for msg in self._handle_image_only_query(event, clean_content):
                yield msg
            return

        # 先尝试查询宠物
        pets = self.db_service.get_pet_info(
            content,
            fuzzy=self.enable_fuzzy_search,
            limit=self.search_limit
        )

        if pets:
            # 找到宠物，格式化返回
            if len(pets) == 1:
                # 精确匹配，返回详细信息 + 图片
                pet = pets[0]
                response = self._format_pet_response(pet)

                # 尝试获取宠物图片
                image_path = pet.get('sprite_image_local')
                if image_path and os.path.exists(image_path):
                    # 有本地图片，发送文字+图片
                    response_with_source = response + DATA_SOURCE_NOTICE
                    yield event.plain_result(response_with_source)
                    yield event.image_result(image_path)
                else:
                    # 没有图片，只发送文字
                    response_with_source = response + DATA_SOURCE_NOTICE
                    yield event.plain_result(response_with_source)
            else:
                # 多个结果，返回列表
                response = f"🔍 找到 {len(pets)} 个相关宠物:\n\n"
                for i, pet in enumerate(pets[:self.search_limit], 1):
                    response += f"{i}. {pet['name']} ({pet['element']}系)\n"

                response += DATA_SOURCE_NOTICE
                yield event.plain_result(response)
            return

        # 再尝试查询技能
        skills = self.db_service.get_skill_info(
            content,
            fuzzy=self.enable_fuzzy_search,
            limit=self.search_limit
        )

        if skills:
            # 找到技能，格式化返回
            if len(skills) == 1:
                response = self._format_skill_response(skills[0])
            else:
                response = f"🔍 找到 {len(skills)} 个相关技能:\n\n"
                for i, skill in enumerate(skills[:self.search_limit], 1):
                    response += f"{i}. {skill['name']} ({skill['element']}系, 威力:{skill['power']})\n"

            response += DATA_SOURCE_NOTICE
            yield event.plain_result(response)
            return

        # 最后尝试搜索 Wiki 页面
        pages = self.db_service.search_wiki_page(
            content,
            fuzzy=self.enable_fuzzy_search,
            limit=self.search_limit
        )

        if pages:
            response = f"📄 找到 {len(pages)} 个相关页面:\n\n"
            for i, page in enumerate(pages[:self.search_limit], 1):
                response += f"{i}. **{page['title']}** ({page['page_type']})\n"
                if page['preview']:
                    response += f"   _{page['preview'][:50]}..._\n"
                response += "\n"

            response += DATA_SOURCE_NOTICE
            yield event.plain_result(response)
            return

        # 未找到任何结果
        yield f"❌ 未找到与 \"{content}\" 相关的信息\n💡 提示: 可以尝试其他关键词或检查拼写"

    async def _handle_image_only_query(self, event: AstrMessageEvent, query: str):
        """
        处理纯图片检索请求（只返回图片，不返回文字）

        Args:
            event: 事件对象
            query: 查询关键词
        """
        # 获取插件目录（用于解析相对路径）
        plugin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wiki')

        # 先尝试查询宠物
        pets = self.db_service.get_pet_info(
            query,
            fuzzy=self.enable_fuzzy_search,
            limit=1
        )

        if pets:
            pet = pets[0]
            image_path = pet.get('sprite_image_local')

            # 如果是相对路径，转换为绝对路径
            if image_path and not os.path.isabs(image_path):
                image_path = self._resolve_wiki_path(image_path)

            if image_path and os.path.exists(image_path):
                # 发送简单的提示和图片
                yield event.plain_result(f"🖼️ {pet['name']}")
                yield event.image_result(image_path)
            else:
                logger.warning(f"⚠️ 宠物 '{pet['name']}' 的图片不存在: {image_path}")
                yield event.plain_result(f"❌ {pet['name']} 没有可用的图片")
            return

        # 再尝试查询技能
        skills = self.db_service.get_skill_info(
            query,
            fuzzy=self.enable_fuzzy_search,
            limit=1
        )

        if skills:
            skill = skills[0]
            image_path = skill.get('icon_image_local')

            # 如果是相对路径，转换为绝对路径
            if image_path and not os.path.isabs(image_path):
                image_path = self._resolve_wiki_path(image_path)

            if image_path and os.path.exists(image_path):
                yield event.plain_result(f"🖼️ {skill['name']}")
                yield event.image_result(image_path)
            else:
                logger.warning(f"⚠️ 技能 '{skill['name']}' 的图片不存在: {image_path}")
                yield event.plain_result(f"❌ {skill['name']} 没有可用的图片")
            return

        # 最后尝试搜索道具
        items = self.db_service.search_item(
            query,
            fuzzy=self.enable_fuzzy_search,
            limit=1
        )

        if items:
            item = items[0]
            image_path = item.get('image_local')

            # 如果是相对路径，转换为绝对路径
            if image_path and not os.path.isabs(image_path):
                image_path = self._resolve_wiki_path(image_path)

            if image_path and os.path.exists(image_path):
                yield event.plain_result(f"🖼️ {item['name']}")
                yield event.image_result(image_path)
            else:
                logger.warning(f"⚠️ 道具 '{item['name']}' 的图片不存在: {image_path}")
                yield event.plain_result(f"❌ {item['name']} 没有可用的图片")
            return

        # 未找到
        yield event.plain_result(f"❌ 未找到与 \"{query}\" 相关的图片\n💡 提示: 请检查名称是否正确")

    @filter.llm_tool(name="roco_wiki_lookup", description="查询洛克王国宠物或技能信息")
    async def wiki_lookup(self, event: AstrMessageEvent, pet_name: str = "") -> str:
        """
        洛克 Wiki 工具 - LLM 可调用

        Args:
            pet_name (str): 宠物或技能名称
        """

        logger.info(f"🔍 LLM 调用查询: {pet_name}")

        # 检查数据库服务
        if not self.db_service:
            return "❌ 数据库服务不可用"

        # 查询宠物
        pets = self.db_service.get_pet_info(
            pet_name,
            fuzzy=self.enable_fuzzy_search,
            limit=1
        )

        if pets:
            return self._format_pet_response(pets[0])

        # 查询技能
        skills = self.db_service.get_skill_info(
            pet_name,
            fuzzy=self.enable_fuzzy_search,
            limit=1
        )

        if skills:
            return self._format_skill_response(skills[0])

        return f"❌ 未找到 \"{pet_name}\" 的相关信息"

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """
        监听所有消息，检测触发关键词
        """
        if not self.db_service:
            return

        # 获取插件目录（用于解析相对路径）
        plugin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wiki')

        message_str = event.message_str.strip()

        # 优先检查翻页命令（避免被当作普通查询）
        if message_str in ['洛克下页', 'wiki-next', 'wiki下一页', '下页']:
            event.stop_event()
            async for msg in self._handle_page_navigation(event, 'next'):
                yield msg
            return
        elif message_str in ['洛克上页', 'wiki-prev', 'wiki上一页', '上页']:
            event.stop_event()
            async for msg in self._handle_page_navigation(event, 'prev'):
                yield msg
            return

        # 检查是否是管理员命令（关键词触发）
        admin_cmd_prefixes = ["洛克管理", "wiki-admin", "wiki_admin"]
        for prefix in admin_cmd_prefixes:
            if message_str.startswith(prefix):
                # 提取命令参数
                command_arg = message_str[len(prefix):].strip()
                # 停止事件传播
                event.stop_event()
                # 调用 _handle_admin_command_impl 处理（避免与 @filter.command 装饰器冲突）
                async for response in self._handle_admin_command_impl(event, command_arg):
                    yield response
                return  # 阻止后续处理

        # 检查是否包含触发关键词
        triggered = any(keyword in message_str for keyword in self.trigger_keywords)

        if not triggered:
            return

        # 提取查询内容（去除触发关键词）
        query_content = message_str
        for keyword in self.trigger_keywords:
            query_content = query_content.replace(keyword, '').strip()

        if not query_content:
            yield "❌ 请提供要查询的宠物或技能名称\n示例：洛克王国 迪莫\n示例：洛克王国 暗突袭\n示例：洛克王国 82 (编号查询)\n示例：洛克王国 火克草 (属性克制)"
            return

        # 检测是否是图片检索请求
        is_image_query, clean_query = self._extract_image_query(query_content)

        if is_image_query:
            logger.info(f"🖼️ 图片检索模式: {clean_query}")
            async for msg in self._handle_image_only_query(event, clean_query):
                yield msg
            return

        # 执行查询
        logger.info(f"🔍 触发关键词查询: {query_content}")

        # 1. 优先使用规则匹配（快速响应）
        smart_query = self._parse_type_query(query_content)
        if smart_query:
            logger.info(f"🧠 规则匹配成功: {smart_query.get('type')}")
            result = self._handle_type_query(smart_query)

            # 检查是否返回字典（包含图片）
            if isinstance(result, dict):
                # 先发送文本
                yield event.plain_result(result['text'])

                # 检查是多张图片还是单张图片
                if 'image_paths' in result and result['image_paths']:
                    # 发送多张图片
                    for img_path in result['image_paths']:
                        yield event.image_result(img_path)
                elif 'image_path' in result and result['image_path']:
                    # 发送单张图片
                    yield event.image_result(result['image_path'])
            else:
                # 纯文本响应
                yield event.plain_result(result)
            return

        # 2. 基础查询：宠物和技能搜索（大多数情况在这里处理）
        # 检测用户的详细查询意图
        query_intent = self._analyze_query_intent(query_content)

        # 如果是详细查询意图，直接提取宠物名进行查询
        if query_intent.get('type') == 'pet_detail':
            pet_name = query_intent.get('pet_name', '')
            detail_type = query_intent.get('detail_type', '')
            logger.info(f"🎯 检测到详细查询意图: 宠物='{pet_name}', 类型='{detail_type}'")

            # 使用宠物名查询
            pets = self.db_service.get_pet_info(
                pet_name,
                fuzzy=self.enable_fuzzy_search,
                limit=1
            )

            if pets:
                pet = pets[0]
                response = self._format_pet_detail_info(pet, detail_type)
                response += DATA_SOURCE_NOTICE
                yield event.plain_result(response)
                return
            else:
                # 未找到宠物，尝试作为技能石查询（例如“乘风 技能石”）
                if detail_type == 'skill_stones':
                    logger.info(f"🔄 未找到宠物 '{pet_name}'，尝试作为技能石查询")
                    response = self._format_skill_stone_info(pet_name)
                    response += DATA_SOURCE_NOTICE
                    yield event.plain_result(response)
                    return
                else:
                    # 未找到宠物
                    yield event.plain_result(f"❌ 未找到宠物 \"{pet_name}\"")
                    return

        # 如果是技能石查询意图
        if query_intent.get('type') == 'skill_stone_info':
            stone_name = query_intent.get('stone_name', '')
            only_source = query_intent.get('only_source', False)
            logger.info(f"🎯 检测到技能石查询意图: '{stone_name}', only_source={only_source}")

            response = self._format_skill_stone_info(stone_name, only_source=only_source)
            response += DATA_SOURCE_NOTICE
            yield event.plain_result(response)
            return

        # 如果是属性筛选查询：“火系宠物有哪些”
        if query_intent.get('type') == 'attribute_filter':
            attribute = query_intent.get('attribute', '')
            entity_type = query_intent.get('entity_type', 'pet')
            logger.info(f"🎯 检测到属性筛选查询: {attribute}系{entity_type}")

            if entity_type == 'pet':
                response = self._handle_attribute_filter(attribute, 'pet')

                # 保存会话状态（用于翻页）
                user_id = event.get_sender_id()
                pets = self.db_service.get_pets_by_element(attribute, limit=1000)
                total_count = len(pets)
                if total_count > self.page_size:
                    self._save_query_state(user_id, 'element_pets', {'element': attribute}, total_count)
                    response += f"\n\n📄 第 1/{(total_count + self.page_size - 1) // self.page_size} 页 | 回复“洛克下页”或“洛克上页”翻⻚"

                response += DATA_SOURCE_NOTICE
                yield event.plain_result(response)
                return

        # 如果是颜色宠物/精灵蛋查询：“红色宠物”、“蓝色精灵蛋”
        if query_intent.get('type') == 'color_filter':
            color = query_intent.get('color', '')
            entity_type = query_intent.get('entity_type', 'pet')
            logger.info(f"🎯 检测到颜色{entity_type}查询: {color}色")

            response = self._handle_color_filter(color, entity_type)

            # 保存会话状态（用于翻页）
            user_id = event.get_sender_id()
            # 估算总数：从响应文本中提取
            import re
            count_match = re.search(r'共(\d+)个', response)
            total_count = int(count_match.group(1)) if count_match else 0

            if total_count > self.page_size:
                self._save_query_state(user_id, 'color_pets', {'color': color, 'entity_type': entity_type}, total_count)
                response += f"\n\n📄 第 1/{(total_count + self.page_size - 1) // self.page_size} 页 | 回复“洛克下页”或“洛克上页”翻⻚"

            response += DATA_SOURCE_NOTICE
            yield event.plain_result(response)
            return

        # 如果是稀有度宠物查询：“稀有宠物”、“史诗精灵”
        if query_intent.get('type') == 'rarity_filter':
            rarity = query_intent.get('rarity', '')
            entity_type = query_intent.get('entity_type', 'pet')
            logger.info(f"🎯 检测到稀有度{entity_type}查询: {rarity}")

            response = self._handle_rarity_filter(rarity, entity_type)

            # 保存会话状态（用于翻页）
            user_id = event.get_sender_id()
            cursor = self.db_service.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM pets WHERE (description LIKE ? OR ability LIKE ?)", (f'%{rarity}%', f'%{rarity}%'))
            total_count = cursor.fetchone()[0]
            if total_count > self.page_size:
                self._save_query_state(user_id, 'rarity_pets', {'rarity': rarity}, total_count)
                response += f"\n\n📄 第 1/{(total_count + self.page_size - 1) // self.page_size} 页 | 回复“洛克下页”或“洛克上页”翻⻚"

            response += DATA_SOURCE_NOTICE
            yield event.plain_result(response)
            return

        # 如果是来源宠物查询：“家园宠物”、“活动精灵”
        if query_intent.get('type') == 'source_filter':
            source = query_intent.get('source', '')
            entity_type = query_intent.get('entity_type', 'pet')
            logger.info(f"🎯 检测到来源{entity_type}查询: {source}")

            response = self._handle_source_filter(source, entity_type)

            # 保存会话状态（用于翻页）
            user_id = event.get_sender_id()
            source_map = {
                '家园': ['家园', '家具店', '商店'],
                '活动': ['活动', '限时', '节日'],
            }
            source_keywords = source_map.get(source, [source])
            like_conditions = ' OR '.join([f"description LIKE '%{s}%" for s in source_keywords])
            cursor = self.db_service.conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM pets WHERE ({like_conditions})")
            total_count = cursor.fetchone()[0]
            if total_count > self.page_size:
                self._save_query_state(user_id, 'source_pets', {'source': source}, total_count)
                response += f"\n\n📄 第 1/{(total_count + self.page_size - 1) // self.page_size} 页 | 回复“洛克下页”或“洛克上页”翻⻚"

            response += DATA_SOURCE_NOTICE
            yield event.plain_result(response)
            return

        # 如果是阶段宠物查询：“初始形态宠物”、“最终形态精灵”
        if query_intent.get('type') == 'stage_filter':
            stage = query_intent.get('stage', '')
            entity_type = query_intent.get('entity_type', 'pet')
            logger.info(f"🎯 检测到阶段{entity_type}查询: {stage}")

            response = self._handle_stage_filter(stage, entity_type)

            # 保存会话状态（用于翻页）
            user_id = event.get_sender_id()
            stage_map = {
                '初始': ['初始形态', '初级'],
                '最终': ['最终形态', '究极体'],
            }
            stage_keywords = stage_map.get(stage, [stage])
            like_conditions = ' OR '.join([f"stage LIKE '%{s}%" for s in stage_keywords])
            cursor = self.db_service.conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM pets WHERE ({like_conditions})")
            total_count = cursor.fetchone()[0]
            if total_count > self.page_size:
                self._save_query_state(user_id, 'stage_pets', {'stage': stage}, total_count)
                response += f"\n\n📄 第 1/{(total_count + self.page_size - 1) // self.page_size} 页 | 回复“洛克下页”或“洛克上页”翻⻚"

            response += DATA_SOURCE_NOTICE
            yield event.plain_result(response)
            return

        # 如果是分类/颜色/稀有度筛选：“蓝色家具”、“紫色道具”
        if query_intent.get('type') == 'category_filter':
            keyword = query_intent.get('keyword', '')
            category = query_intent.get('category', '')
            filter_type = query_intent.get('filter_type', '')
            logger.info(f"🎯 检测到分类筛选: {keyword}{category}, 类型={filter_type}")

            response = self._handle_category_filter(keyword, category, filter_type)

            # 保存会话状态（用于翻页）
            user_id = event.get_sender_id()
            db_category_map = {
                'furniture': '家具',
                'item': '',
                'skill_stone': '技能石',
                'gumball': '咕噜球',
                'fruit': '精灵果实',
            }
            db_category = db_category_map.get(category, '')

            # 估算总数（简化处理，实际应该根据filter_type构建不同的COUNT查询）
            cursor = self.db_service.conn.cursor()
            if db_category:
                cursor.execute(f"SELECT COUNT(*) FROM items WHERE category = '{db_category}'")
            else:
                cursor.execute("SELECT COUNT(*) FROM items")
            total_count = cursor.fetchone()[0]

            if total_count > self.page_size:
                self._save_query_state(user_id, 'category_items', {
                    'keyword': keyword,
                    'category': category,
                    'filter_type': filter_type
                }, total_count)
                response += f"\n\n📄 第 1/{(total_count + self.page_size - 1) // self.page_size} 页 | 回复“洛克下页”或“洛克上页”翻⻚"

            response += DATA_SOURCE_NOTICE
            yield event.plain_result(response)
            return

        # 普通查询：清理查询词
        clean_query = query_content
        for suffix in ['技能', 'skill', 'skills', '招式', 'move', '血脉', 'bloodline', '技能石', 'stone', 'stones', '课题', 'quest']:
            clean_query = clean_query.replace(suffix, '').strip()

        # 额外清理常见语气词、助词和无意义后缀（但不要移除"图片"等关键词）
        # 注意：只清理末尾的后缀，避免误删宠物名称中的部分
        import re
        for word in ['有什么', '有哪些', '是什么', '是多少', '的资料', '的介绍', '的信息', '的详情', '长什么样', '的图片', '的照片', '的立绘', '的头像', '的图标']:
            # 使用正则确保只匹配末尾
            clean_query = re.sub(re.escape(word) + r'$', '', clean_query).strip()

        # 对于"的样子"、"的"等短词，需要更谨慎：只有当它们不在括号内时才清理
        # 例如："丢丢（火山附近的样子）" 不应该清理 "的样子"
        # 但 "喵喵的样子" 应该清理为 "喵喵"
        if '(' not in clean_query and '（' not in clean_query:
            # 没有括号，可以安全清理
            for word in ['的样子', '的', '吗', '呢', '吧', '精灵', '宠物', '怪兽', '魔灵', '伙伴']:
                clean_query = re.sub(re.escape(word) + r'$', '', clean_query).strip()

        logger.info(f"🔧 清理后查询词: '{clean_query}' (原始: '{query_content}')")

        # 先尝试查询宠物（使用清理后的查询词）
        pets = self.db_service.get_pet_info(
            clean_query if clean_query else query_content,
            fuzzy=self.enable_fuzzy_search,
            limit=self.search_limit
        )

        if pets:
            # 检查是否有更多相似的宠物（用于判断是否显示变体列表）
            all_similar_pets = self.db_service.get_pet_info(
                clean_query if clean_query else query_content,
                fuzzy=True,
                limit=50  # 获取更多结果用于判断
            )
            has_variants = len(all_similar_pets) > 1

            # 显示第一个匹配项的详细信息
            pet = pets[0]
            response = self._format_pet_response(pet)

            # 如果有多个变体，附加变体列表
            if has_variants and len(all_similar_pets) > 1:
                response += f"\n\n🐾 **相关形态/变体** ({len(all_similar_pets)}个):\n"
                page_size = self.page_size
                for i, variant in enumerate(all_similar_pets[:page_size], 1):
                    variant_element = variant.get('element', '未知')
                    variant_stage = variant.get('stage', '')
                    variant_form = variant.get('form', '')

                    extra = ""
                    if variant_stage:
                        extra += f" [{variant_stage}]"
                    if variant_form and variant_form != '原始形态':
                        extra += f" {variant_form}"

                    # 标记当前显示的宠物
                    if variant['name'] == pet['name']:
                        response += f"  {i}. **{variant['name']}** ({variant_element}系){extra} ← 当前\n"
                    else:
                        response += f"  {i}. {variant['name']} ({variant_element}系){extra}\n"

                if len(all_similar_pets) > page_size:
                    response += f"  ... 还有 {len(all_similar_pets) - page_size} 个形态\n"
                    response += f"💡 提示：每页显示 {page_size} 个\n"

                response += f"\n💡 提示：输入完整名称（包含形态）可查看其他形态的详细信息"

            # 添加数据来源声明
            response += DATA_SOURCE_NOTICE

            # 检查是否有图片
            image_path = pet.get('sprite_image_local')
            logger.info(f"🖼️ 宠物 '{pet['name']}' 的图片路径: {image_path}")
            if image_path:
                # 清理路径前缀（移除 ./ 或 .\）
                if image_path.startswith('./') or image_path.startswith('.\\'):
                    image_path = image_path[2:]
                # 如果是相对路径，基于插件目录解析
                if not os.path.isabs(image_path):
                    image_path = self._resolve_wiki_path(image_path)
                # 规范化路径分隔符（跨平台兼容）
                image_path = image_path.replace('\\', '/')
                logger.info(f"🖼️ 解析后的完整路径: {image_path}")
                logger.info(f"🖼️ 文件是否存在: {os.path.exists(image_path)}")

            # 如果有图片，使用 MessageChain 组合文本和图片
            if image_path and os.path.exists(image_path):
                try:
                    import astrbot.api.message_components as Comp
                    chain = [
                        Comp.Plain(response),
                        Comp.Image.fromFileSystem(image_path)
                    ]
                    logger.info(f"🖼️ 准备发送宠物图文消息: {image_path}")
                    yield event.chain_result(chain)
                    logger.info(f"✅ 已发送宠物图文消息")
                except Exception as e:
                    logger.warning(f"⚠️ 发送宠物图文消息失败: {e}", exc_info=True)
                    # 降级：分别发送
                    yield event.plain_result(response)
                    yield event.image_result(image_path)
            else:
                # 没有图片，只发送文本
                yield event.plain_result(response)
            return

        # 再尝试查询技能（使用原始查询词）
        skills = self.db_service.get_skill_info(
            query_content,
            fuzzy=self.enable_fuzzy_search,
            limit=self.search_limit
        )

        if skills:
            if len(skills) == 1:
                skill = skills[0]
                response = self._format_skill_response(skill)

                # 检查是否有图片
                image_path = skill.get('icon_image_local')
                logger.info(f"🖼️ 技能 '{skill['name']}' 的图片路径: {image_path}")
                if image_path:
                    # 清理路径前缀（移除 ./ 或 .\）
                    if image_path.startswith('./') or image_path.startswith('.\\'):
                        image_path = image_path[2:]
                    # 如果是相对路径，基于插件目录解析
                    if not os.path.isabs(image_path):
                        image_path = self._resolve_wiki_path(image_path)
                    # 规范化路径分隔符（跨平台兼容）
                    image_path = image_path.replace('\\', '/')
                    logger.info(f"🖼️ 解析后的完整路径: {image_path}")
                    logger.info(f"🖼️ 文件是否存在: {os.path.exists(image_path)}")

                # 如果有图片，使用 MessageChain 组合文本和图片
                if image_path and os.path.exists(image_path):
                    try:
                        import astrbot.api.message_components as Comp
                        chain = [
                            Comp.Plain(response),
                            Comp.Image.fromFileSystem(image_path)
                        ]
                        logger.info(f"🖼️ 准备发送技能图文消息: {image_path}")
                        yield event.chain_result(chain)
                        logger.info(f"✅ 已发送技能图文消息")
                    except Exception as e:
                        logger.warning(f"⚠️ 发送技能图文消息失败: {e}", exc_info=True)
                        # 降级：分别发送
                        yield event.plain_result(response)
                        yield event.image_result(image_path)
                else:
                    # 没有图片，只发送文本
                    yield event.plain_result(response)
            else:
                response = f"🔍 找到 {len(skills)} 个相关技能:\n\n"
                for i, skill in enumerate(skills[:self.search_limit], 1):
                    response += f"{i}. {skill['name']} ({skill['element']}系)\n"

                response += DATA_SOURCE_NOTICE
                yield event.plain_result(response)
            return

        # 尝试查询道具
        items = self.db_service.get_item_info(
            query_content,
            fuzzy=self.enable_fuzzy_search,
            limit=self.search_limit
        )

        if items:
            # 检查是否有更多相似的道具（用于判断是否显示变体列表）
            all_similar_items = self.db_service.get_item_info(
                query_content,
                fuzzy=True,
                limit=50  # 获取更多结果用于判断
            )
            has_variants = len(all_similar_items) > 1

            # 显示第一个匹配项的详细信息
            item = items[0]
            response = f"🎒 **{item['name']}**\n"
            response += "━━━━━━━━━━━━━━\n"

            if item.get('category'):
                response += f"📦 分类: {item['category']}\n"
                if item.get('subcategory'):
                    response += f"🔹 子类: {item['subcategory']}\n"
            if item.get('rarity'):
                response += f"⭐ 稀有度: {item['rarity']}\n"
            if item.get('version'):
                response += f"🎮 版本: {item['version']}\n"

            if item.get('source'):
                response += f"\n🛒 获取方式:\n{item['source']}\n"

            if item.get('description'):
                response += f"\n📝 **描述:**\n{item['description']}\n"

            # 如果有多个变体，附加变体列表
            if has_variants and len(all_similar_items) > 1:
                response += f"\n📚 **相关变体** ({len(all_similar_items)}个):\n"
                page_size = self.page_size
                for i, variant in enumerate(all_similar_items[:page_size], 1):
                    variant_category = variant.get('category', '')
                    variant_rarity = variant.get('rarity', '')
                    extra = ""
                    if variant_category:
                        extra += f" [{variant_category}]"
                    if variant_rarity:
                        extra += f" ⭐{variant_rarity}"
                    # 标记当前显示的物品
                    if variant['name'] == item['name']:
                        response += f"  {i}. **{variant['name']}**{extra} ← 当前\n"
                    else:
                        response += f"  {i}. {variant['name']}{extra}\n"

                if len(all_similar_items) > page_size:
                    response += f"  ... 还有 {len(all_similar_items) - page_size} 个变体\n"
                    response += f"💡 提示：每页显示 {page_size} 个\n"

                response += f"\n💡 提示：输入完整名称可查看其他变体的详细信息"

            # 添加数据来源声明
            response += DATA_SOURCE_NOTICE

            # 检查是否有图片
            image_path = item.get('image_local')
            logger.info(f"🖼️ 道具 '{item['name']}' 的图片路径: {image_path}")
            if image_path:
                # 清理路径前缀（移除 ./ 或 .\）
                if image_path.startswith('./') or image_path.startswith('.\\'):
                    image_path = image_path[2:]
                # 如果是相对路径，基于插件目录解析
                if not os.path.isabs(image_path):
                    image_path = self._resolve_wiki_path(image_path)
                logger.info(f"🖼️ 解析后的完整路径: {image_path}")
                logger.info(f"🖼️ 文件是否存在: {os.path.exists(image_path)}")

            # 如果有图片，发送图文消息
            if image_path and os.path.exists(image_path):
                try:
                    import astrbot.api.message_components as Comp
                    chain = [
                        Comp.Plain(response),
                        Comp.Image.fromFileSystem(image_path)
                    ]
                    logger.info(f"🖼️ 准备发送道具图文消息: {image_path}")
                    yield event.chain_result(chain)
                    logger.info(f"✅ 已发送道具图文消息")
                except Exception as e:
                    logger.warning(f"⚠️ 发送道具图文消息失败: {e}", exc_info=True)
                    # 降级：分别发送
                    yield event.plain_result(response)
                    yield event.image_result(image_path)
            else:
                # 没有图片，只发送文本
                yield event.plain_result(response)
            return

        yield event.plain_result(f"❌ 未找到与 \"{query_content}\" 相关的信息\n💡 提示：可以尝试只输入宠物名、技能名、编号或属性克制关系\n{DATA_SOURCE_NOTICE}")

    def _cleanup_expired_sessions(self):
        """
        清理超时的会话状态
        """
        import time
        current_time = time.time()
        expired_users = [
            user_id for user_id, state in self.session_states.items()
            if current_time - state['timestamp'] > self.session_timeout
        ]
        for user_id in expired_users:
            del self.session_states[user_id]
        if expired_users:
            logger.debug(f"🧹 清理了 {len(expired_users)} 个超时会话")

    def _save_query_state(self, user_id: str, query_type: str, params: dict, total: int):
        """
        保存查询状态到会话

        Args:
            user_id: 用户ID
            query_type: 查询类型
            params: 查询参数
            total: 总结果数
        """
        import time
        self.session_states[user_id] = {
            'query_type': query_type,
            'params': params,
            'page': 1,
            'total': total,
            'timestamp': time.time()
        }
        logger.debug(f"💾 保存会话状态: user={user_id}, type={query_type}, total={total}")

    def _get_query_state(self, user_id: str) -> Optional[dict]:
        """
        获取用户的查询状态

        Args:
            user_id: 用户ID

        Returns:
            会话状态字典，如果不存在或已超时则返回 None
        """
        import time
        self._cleanup_expired_sessions()

        if user_id not in self.session_states:
            return None

        state = self.session_states[user_id]
        # 再次检查是否超时
        if time.time() - state['timestamp'] > self.session_timeout:
            del self.session_states[user_id]
            return None

        return state

    async def _handle_page_navigation(self, event: AstrMessageEvent, action: str):
        """
        处理翻页操作

        Args:
            event: 事件对象
            action: 'next' 或 'prev'
        """
        # 停止事件传播
        event.stop_event()

        # 获取用户ID
        user_id = event.get_sender_id()

        # 获取会话状态
        state = self._get_query_state(user_id)
        if not state:
            yield event.plain_result("❌ 没有可翻⻚的查询记录\n💡 提示：先进行一次列表查询（如“火系宠物”、“红色家具”）")
            return

        # 计算新页码
        current_page = state['page']
        total = state['total']
        page_size = self.page_size
        total_pages = (total + page_size - 1) // page_size

        if action == 'next':
            new_page = current_page + 1
            if new_page > total_pages:
                yield event.plain_result(f"⚠️ 已经是最后一页了\n当前: 第 {current_page}/{total_pages} 页")
                return
        else:  # prev
            new_page = current_page - 1
            if new_page < 1:
                yield event.plain_result(f"⚠️ 已经是第一⻚了\n当前: 第 {current_page}/{total_pages} 页")
                return

        # 更新页码
        state['page'] = new_page
        state['timestamp'] = __import__('time').time()

        # 根据查询类型执行相应的查询
        query_type = state['query_type']
        params = state['params']

        try:
            if query_type == 'color_pets':
                response = await self._execute_color_pets_query(params, new_page)
            elif query_type == 'rarity_pets':
                response = await self._execute_rarity_pets_query(params, new_page)
            elif query_type == 'source_pets':
                response = await self._execute_source_pets_query(params, new_page)
            elif query_type == 'stage_pets':
                response = await self._execute_stage_pets_query(params, new_page)
            elif query_type == 'element_pets':
                response = await self._execute_element_pets_query(params, new_page)
            elif query_type == 'category_items':
                response = await self._execute_category_items_query(params, new_page)
            else:
                yield event.plain_result(f"❌ 不支持的查询类型: {query_type}")
                return

            # 添加分页提示
            response += f"\n\n📄 第 {new_page}/{total_pages} 页 | 回复“洛克下页”或“洛克上页”翻⻚"

            yield event.plain_result(response)

        except Exception as e:
            logger.error(f"❌ 翻⻚查询失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 翻⻚失败: {str(e)}")

    async def _execute_color_pets_query(self, params: dict, page: int) -> str:
        """执行颜色宠物查询"""
        color = params['color']
        offset = (page - 1) * self.page_size

        pets = self.db_service.search_pets_by_color(color, limit=self.page_size, offset=offset)

        if not pets:
            return f"❌ 未找到{color}色宠物"

        response = f"🎨 **{color}色宠物列表**:\n"
        response += "━━━━━━━━━━━━━━\n\n"

        for i, pet in enumerate(pets, offset + 1):
            element = pet.get('element', '未知')
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            stage = pet.get('stage', '')
            stage_str = f" [{stage}]" if stage else ""
            response += f"{i}. {pet['name']} ({element}{extra}系){stage_str}\n"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    async def _execute_rarity_pets_query(self, params: dict, page: int) -> str:
        """执行稀有度宠物查询"""
        rarity = params['rarity']
        offset = (page - 1) * self.page_size

        cursor = self.db_service.conn.cursor()
        query = "SELECT name, element, element2, stage FROM pets WHERE (description LIKE ? OR ability LIKE ?) ORDER BY name LIMIT ? OFFSET ?"
        cursor.execute(query, (f'%{rarity}%', f'%{rarity}%', self.page_size, offset))
        pets = [dict(zip(['name', 'element', 'element2', 'stage'], row)) for row in cursor.fetchall()]

        if not pets:
            return f"❌ 未找到{rarity}稀有度的宠物"

        response = f"⭐ **{rarity}稀有度宠物列表**:\n"
        response += "━━━━━━━━━━━━━━\n\n"

        for i, pet in enumerate(pets, offset + 1):
            element = pet.get('element', '未知')
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            stage = pet.get('stage', '')
            stage_str = f" [{stage}]" if stage else ""
            response += f"{i}. {pet['name']} ({element}{extra}系){stage_str}\n"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    async def _execute_source_pets_query(self, params: dict, page: int) -> str:
        """执行来源宠物查询"""
        source = params['source']
        offset = (page - 1) * self.page_size

        source_map = {
            '家园': ['家园', '家具店', '商店'],
            '活动': ['活动', '限时', '节日'],
        }
        source_keywords = source_map.get(source, [source])
        like_conditions = ' OR '.join([f"description LIKE '%{s}%" for s in source_keywords])

        cursor = self.db_service.conn.cursor()
        query = f"SELECT name, element, element2, stage FROM pets WHERE ({like_conditions}) ORDER BY name LIMIT ? OFFSET ?"
        cursor.execute(query, (self.page_size, offset))
        pets = [dict(zip(['name', 'element', 'element2', 'stage'], row)) for row in cursor.fetchall()]

        if not pets:
            return f"❌ 未找到{source}相关的宠物"

        response = f"📍 **{source}相关宠物列表**:\n"
        response += "━━━━━━━━━━━━━━\n\n"

        for i, pet in enumerate(pets, offset + 1):
            element = pet.get('element', '未知')
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            stage = pet.get('stage', '')
            stage_str = f" [{stage}]" if stage else ""
            response += f"{i}. {pet['name']} ({element}{extra}系){stage_str}\n"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    async def _execute_stage_pets_query(self, params: dict, page: int) -> str:
        """执行阶段宠物查询"""
        stage_keyword = params['stage']
        offset = (page - 1) * self.page_size

        stage_map = {
            '初始': ['初始形态', '初级'],
            '最终': ['最终形态', '究极体'],
        }
        stage_keywords = stage_map.get(stage_keyword, [stage_keyword])
        like_conditions = ' OR '.join([f"stage LIKE '%{s}%" for s in stage_keywords])

        cursor = self.db_service.conn.cursor()
        query = f"SELECT name, element, element2, stage FROM pets WHERE ({like_conditions}) ORDER BY name LIMIT ? OFFSET ?"
        cursor.execute(query, (self.page_size, offset))
        pets = [dict(zip(['name', 'element', 'element2', 'stage'], row)) for row in cursor.fetchall()]

        if not pets:
            return f"❌ 未找到{stage_keyword}阶段的宠物"

        response = f"🔄 **{stage_keyword}阶段宠物列表**:\n"
        response += "━━━━━━━━━━━━━━\n\n"

        for i, pet in enumerate(pets, offset + 1):
            element = pet.get('element', '未知')
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            pet_stage = pet.get('stage', '')
            stage_str = f" [{pet_stage}]" if pet_stage else ""
            response += f"{i}. {pet['name']} ({element}{extra}系){stage_str}\n"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    async def _execute_element_pets_query(self, params: dict, page: int) -> str:
        """执行属性宠物查询"""
        element = params['element']
        offset = (page - 1) * self.page_size

        pets = self.db_service.get_pets_by_element(element, limit=self.page_size, offset=offset)

        if not pets:
            return f"❌ 未找到{element}系宠物"

        response = f"🔥 **{element}系宠物列表**:\n"
        response += "━━━━━━━━━━━━━━\n\n"

        for i, pet in enumerate(pets, offset + 1):
            element2 = pet.get('element2', '')
            extra = f"/{element2}" if element2 else ""
            response += f"{i}. {pet['name']} ({pet['element']}{extra}系)\n"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    async def _execute_category_items_query(self, params: dict, page: int) -> str:
        """执行分类/颜色道具查询"""
        keyword = params['keyword']
        category = params['category']
        filter_type = params['filter_type']
        offset = (page - 1) * self.page_size

        db_category_map = {
            'furniture': '家具',
            'item': '',
            'skill_stone': '技能石',
            'gumball': '咕噜球',
            'fruit': '精灵果实',
        }
        db_category = db_category_map.get(category, '')

        cursor = self.db_service.conn.cursor()

        if filter_type == 'color':
            color_map = {
                '蓝': ['蓝'], '红': ['红'], '绿': ['绿'], '黄': ['黄'],
                '紫': ['紫'], '白': ['白'], '黑': ['黑'], '粉': ['粉'], '橙': ['橙'],
            }
            color_keywords = color_map.get(keyword, [keyword])
            like_conditions_main = ' OR '.join([f"main_color = '{c}'" for c in color_keywords])
            like_conditions_rarity = ' OR '.join([f"rarity LIKE '%{c}%'" for c in color_keywords])

            query = f"SELECT name, category, rarity, main_color FROM items WHERE ({like_conditions_main}) OR ({like_conditions_rarity})"
            if db_category:
                query += f" AND category = '{db_category}'"
            query += " ORDER BY name LIMIT ? OFFSET ?"

            cursor.execute(query, (self.page_size, offset))
            items = [dict(zip(['name', 'category', 'rarity', 'main_color'], row)) for row in cursor.fetchall()]
        else:
            # 其他筛选类型简化处理
            query = f"SELECT name, category, rarity, main_color FROM items WHERE category = '{db_category}' ORDER BY name LIMIT ? OFFSET ?"
            cursor.execute(query, (self.page_size, offset))
            items = [dict(zip(['name', 'category', 'rarity', 'main_color'], row)) for row in cursor.fetchall()]

        if not items:
            return f"❌ 未找到相关{db_category or '道具'}"

        type_names = {'color': '颜色', 'rarity': '稀有度', 'source': '来源'}
        type_name = type_names.get(filter_type, '')

        response = f"🎨 **{keyword}{type_name}的{db_category or '道具'}**:\n"
        response += "━━━━━━━━━━━━━━\n\n"

        for i, item in enumerate(items, offset + 1):
            response += f"{i}. **{item['name']}**"
            if item.get('main_color'):
                response += f" [{item['main_color']}]"
            elif item.get('rarity'):
                response += f" [{item['rarity']}]"
            response += "\n"

        response += f"\n💡 提示：输入完整名称可查看详细信息"
        return response

    @filter.command("洛克管理", ["wiki_admin", "wiki-admin"])
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def handle_admin(self, event: AstrMessageEvent, command: str):
        """
        管理员命令（通过 AstrBot 权限系统控制）
        用法: /洛克管理 <command>
        可用命令:
          - update: 更新数据库（从 Wiki 爬取最新数据）
          - status: 查看数据库状态
          - tag-colors: 为无颜色的家具/道具识别颜色（大模型视觉识别）
          - tag-pet-colors: 为无颜色的宠物/精灵蛋识别颜色（大模型视觉识别）
          - force-tag-colors: 强制重新识别所有家具/道具颜色（覆盖已有颜色）
          - force-tag-pet-colors: 强制重新识别所有宠物/精灵蛋颜色（覆盖已有颜色）
          - fix-missing: 补全缺失的宠物数据
        """
        # 停止事件传播，防止被 Agent/LLM 拦截
        event.stop_event()

        # 解析命令
        parts = command.strip().split()
        if len(parts) < 1:
            yield f"❌ 请提供命令\n用法: /洛克管理 <command>\n示例: /洛克管理 update"
            return

        cmd = parts[0].lower()

        # 执行命令
        if cmd == "update":
            async for msg in self._handle_update_db(event):
                yield msg
        elif cmd == "status":
            async for msg in self._handle_db_status(event):
                yield msg
        elif cmd == "tag-colors":
            async for msg in self._handle_tag_colors(event):
                yield msg

        elif cmd == "tag-pet-colors":
            async for msg in self._handle_tag_pet_colors(event):
                yield msg
        elif cmd == "force-tag-colors":
            async for msg in self._handle_force_tag_colors(event):
                yield msg
        elif cmd == "force-tag-pet-colors":
            async for msg in self._handle_force_tag_pet_colors(event):
                yield msg
        elif cmd == "fix-missing":
            async for msg in self._handle_fix_missing_data(event):
                yield msg
        elif cmd == "check-vision":
            async for msg in self._handle_check_vision_model(event):
                yield msg
        else:
            yield f"❌ 未知命令: {cmd}\n\n📋 可用命令:\n  • update - 增量更新数据库\n  • status - 查看数据库状态\n  • tag-colors - 为道具标记颜色\n  • tag-pet-colors - 为宠物标记颜色\n  • force-tag-colors - 强制重新识别所有道具颜色\n  • force-tag-pet-colors - 强制重新识别所有宠物颜色\n  • fix-missing - 补全缺失的宠物数据\n  • check-vision - 检查视觉模型配置\n\n示例: 洛克管理 check-vision"

    async def _handle_update_db(self, event: AstrMessageEvent):
        """处理数据库更新命令"""
        yield "🔄 开始更新数据库，这可能需要几分钟时间..."

        try:
            # 获取插件目录
            plugin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wiki')
            build_script = os.path.join(plugin_dir, "src", "build_wiki_db.py")

            if not os.path.exists(build_script):
                yield "❌ 找不到爬虫脚本"
                return

            # 运行爬虫脚本
            process = subprocess.Popen(
                [sys.executable, build_script, "build", "--full"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=plugin_dir
            )

            # 等待完成
            stdout, stderr = process.communicate()

            if process.returncode == 0:
                yield "✅ 数据库更新成功！\n可以开始使用新数据进行查询。"
                logger.info("✅ 数据库更新成功")
            else:
                error_msg = stderr[:500] if stderr else "未知错误"
                yield f"❌ 数据库更新失败:\n{error_msg}"
                logger.error(f"❌ 数据库更新失败: {stderr}")

        except Exception as e:
            yield f"❌ 更新过程出错: {str(e)}"
            logger.error(f"❌ 更新过程出错: {e}", exc_info=True)

    async def _handle_db_status(self, event: AstrMessageEvent):
        """处理数据库状态查询命令"""
        if not self.db_service:
            yield "❌ 数据库服务不可用"
            return

        try:
            stats = self.db_service.get_database_stats()

            response = f"📊 **数据库状态**\n\n"
            response += f"宠物数量: {stats['pets']}\n"
            response += f"技能数量: {stats['skills']}\n"
            response += f"Wiki页面: {stats['pages']}\n"
            response += f"属性克制: {stats['type_chart']}\n"

            # 检查是否有图片
            has_images = False
            if stats['pets'] > 0:
                sample_pets = self.db_service.get_pet_info("", fuzzy=True, limit=5)
                for pet in sample_pets:
                    img_path = pet.get('sprite_image_local')
                    if img_path and os.path.exists(img_path):
                        has_images = True
                        break

            if has_images:
                response += "\n✅ 已包含宠物图片"
            else:
                response += "\n⚠️ 暂无宠物图片"

            yield response

        except Exception as e:
            yield f"❌ 获取数据库状态失败: {str(e)}"
            logger.error(f"❌ 获取数据库状态失败: {e}", exc_info=True)

    async def _handle_check_vision_model(self, event: AstrMessageEvent):
        """处理视觉模型诊断命令"""
        yield "🔍 开始检查视觉模型配置..."

        vision_model_config = self.config.get("vision_model_config", "")

        if not vision_model_config or not vision_model_config.strip():
            yield "❌ 未配置视觉模型\n💡 请在 WebUI 的插件配置中选择视觉模型"
            return

        yield f"📋 配置的视觉模型: {vision_model_config}"

        try:
            provider_manager = getattr(self.context, 'provider_manager', None)
            if not provider_manager:
                yield "❌ 无法访问 provider_manager"
                return

            providers = getattr(provider_manager, 'get_insts', lambda: [])()

            if not providers:
                yield "❌ 没有找到任何已配置的 Provider\n💡 请先在 AstrBot 中配置至少一个 Provider"
                return

            yield f"📊 找到 {len(providers)} 个 Provider"

            # 列出所有 provider 的详细信息
            response = "\n📋 Provider 列表:\n"
            selected_found = False

            for i, p in enumerate(providers, 1):
                pid = (getattr(p, 'id', None) or
                       getattr(p, 'provider_id', None) or
                       getattr(p, 'name', None) or
                       getattr(p, 'model_name', None) or
                       getattr(p, 'model', None) or
                       str(type(p).__name__))

                pname = getattr(p, 'name', '') or getattr(p, 'model_name', '') or pid
                api_key = getattr(p, 'api_key', '') or ''
                base_url = getattr(p, 'base_url', '') or ''
                model = getattr(p, 'model_name', '') or getattr(p, 'model', '') or ''

                response += f"\n{i}. ID: {pid}\n"
                response += f"   名称: {pname}\n"
                response += f"   API Key: {'✓' if api_key else '✗'}\n"
                response += f"   Base URL: {base_url[:50] + '...' if len(base_url) > 50 else base_url}\n"
                response += f"   Model: {model}\n"

                # 精确匹配
                if pid == vision_model_config:
                    selected_found = True
                    response += "   ✅ 这是当前选中的视觉模型（精确匹配）\n"
                # 模糊匹配：去除前缀后匹配
                elif '/' in vision_model_config:
                    config_model_name = vision_model_config.split('/')[-1]
                    if pid == config_model_name or pid.endswith('/' + config_model_name):
                        selected_found = True
                        response += f"   ✅ 这是当前选中的视觉模型（模糊匹配）\n"

            yield response

            if not selected_found:
                yield f"\n❌ 未找到匹配 '{vision_model_config}' 的 Provider\n💡 请检查配置是否正确"
            else:
                yield f"\n✅ 视觉模型配置正确！"

                # 测试颜色提取器
                if self.color_extractor:
                    yield "✅ 颜色提取器已初始化成功"
                else:
                    yield "❌ 颜色提取器初始化失败，请检查日志"

        except Exception as e:
            yield f"❌ 检查过程出错: {str(e)}"
            logger.error(f"❌ 检查视觉模型配置失败: {e}", exc_info=True)

    async def _handle_tag_colors(self, event: AstrMessageEvent):
        """处理家具/道具颜色识别命令（大模型视觉识别，只标记无颜色的）"""
        yield "🎨 开始为无颜色的家具/道具识别颜色...（使用大模型视觉识别）"

        # 检查颜色提取器是否可用
        if not self.color_extractor:
            yield "❌ 颜色提取器不可用，请先在 WebUI 中配置视觉模型"
            return

        try:
            import sqlite3

            # 连接数据库
            db_path = self.config.get("wiki_db_path", "./wiki/wiki-local.db")
            if not os.path.isabs(db_path):
                db_path = os.path.join(plugin_dir, db_path)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            try:
                # 检查并添加 main_color 字段（如果不存在）
                cursor.execute("PRAGMA table_info(items)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'main_color' not in columns:
                    yield "📝 添加 main_color 字段到 items 表"
                    cursor.execute("ALTER TABLE items ADD COLUMN main_color TEXT")
                    conn.commit()

                # 查询所有未设置颜色的家具和道具
                cursor.execute("""
                    SELECT name, image_local, category, subcategory
                    FROM items
                    WHERE (main_color IS NULL OR main_color = '')
                    AND image_local IS NOT NULL
                    AND image_local != ''
                """)

                items_list = cursor.fetchall()

                if not items_list:
                    yield "✅ 所有家具和道具都已设置颜色"
                    return

                total_count = len(items_list)
                yield f"📋 找到 {total_count} 个需要识别颜色的项目"
                yield "💡 提示：使用大模型视觉识别，速度较慢但更准确"

                success_count = 0
                fail_count = 0
                no_color_count = 0

                for i, (name, image_local, category, subcategory) in enumerate(items_list, 1):
                    # 构建完整图片路径
                    if image_local and not os.path.isabs(image_local):
                        full_path = os.path.join(plugin_dir, image_local)
                    else:
                        full_path = image_local

                    # 检查图片是否存在
                    if not full_path or not os.path.exists(full_path):
                        logger.warning(f"图片不存在: {full_path}")
                        fail_count += 1
                        continue

                    # 使用大模型视觉识别提取颜色
                    result = self.color_extractor.extract_main_colors(full_path)

                    if result and result['main_color']:
                        cursor.execute(
                            "UPDATE items SET main_color = ? WHERE name = ?",
                            (result['main_color'], name)
                        )
                        conn.commit()
                        success_count += 1
                        colors_str = ', '.join(result['colors'])
                        logger.info(f"[{i}/{total_count}] {name}: {colors_str}")
                    elif result and not result['main_color']:
                        no_color_count += 1
                    else:
                        fail_count += 1

                    # 每处理10个发送一次进度（大模型速度慢，降低频率）
                    if i % 10 == 0:
                        yield f"⏳ 进度: {i}/{total_count} (成功: {success_count}, 无颜色: {no_color_count}, 失败: {fail_count})"

                yield f"✅ 颜色识别完成！\n总计: {total_count}\n成功: {success_count}\n无颜色: {no_color_count}\n失败: {fail_count}"
                logger.info(f"颜色识别完成: 成功{success_count}, 无颜色{no_color_count}, 失败{fail_count}")

            finally:
                conn.close()

        except Exception as e:
            yield f"❌ 颜色识别过程出错: {str(e)}"
            logger.error(f"❌ 颜色识别过程出错: {e}", exc_info=True)

    async def _handle_tag_pet_colors(self, event: AstrMessageEvent):
        """处理宠物/精灵蛋颜色识别命令（大模型视觉识别，只标记无颜色的）"""
        yield "🎨 开始为无颜色的宠物/精灵蛋识别颜色...（使用大模型视觉识别）"

        # 检查颜色提取器是否可用
        if not self.color_extractor:
            yield "❌ 颜色提取器不可用，请先在 WebUI 中配置视觉模型"
            return

        try:
            import sqlite3

            # 连接数据库
            db_path = self.config.get("wiki_db_path", "./wiki/wiki-local.db")
            if not os.path.isabs(db_path):
                db_path = os.path.join(plugin_dir, db_path)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            try:
                # 检查并添加 main_color 字段（如果不存在）
                cursor.execute("PRAGMA table_info(pets)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'main_color' not in columns:
                    yield "📝 添加 main_color 字段到 pets 表"
                    cursor.execute("ALTER TABLE pets ADD COLUMN main_color TEXT")
                    conn.commit()

                # 查询所有未设置颜色的宠物
                cursor.execute("""
                    SELECT name, sprite_image_local
                    FROM pets
                    WHERE (main_color IS NULL OR main_color = '')
                    AND sprite_image_local IS NOT NULL
                    AND sprite_image_local != ''
                """)

                pets_list = cursor.fetchall()

                if not pets_list:
                    yield "✅ 所有宠物都已设置颜色"
                    return

                total_count = len(pets_list)
                yield f"📋 找到 {total_count} 个需要识别颜色的宠物"
                yield "💡 提示：使用大模型视觉识别，速度较慢但更准确"

                success_count = 0
                fail_count = 0
                no_color_count = 0

                for i, (name, image_local) in enumerate(pets_list, 1):
                    # 构建完整图片路径
                    if image_local and not os.path.isabs(image_local):
                        full_path = os.path.join(plugin_dir, image_local)
                    else:
                        full_path = image_local

                    # 检查图片是否存在
                    if not full_path or not os.path.exists(full_path):
                        logger.warning(f"图片不存在: {full_path}")
                        fail_count += 1
                        continue

                    # 使用大模型视觉识别提取颜色
                    result = self.color_extractor.extract_main_colors(full_path)

                    if result and result['main_color']:
                        cursor.execute(
                            "UPDATE pets SET main_color = ? WHERE name = ?",
                            (result['main_color'], name)
                        )
                        conn.commit()
                        success_count += 1
                        colors_str = ', '.join(result['colors'])
                        logger.info(f"[{i}/{total_count}] {name}: {colors_str}")
                    elif result and not result['main_color']:
                        no_color_count += 1
                    else:
                        fail_count += 1

                    # 每处理10个发送一次进度（大模型速度慢，降低频率）
                    if i % 10 == 0:
                        yield f"⏳ 进度: {i}/{total_count} (成功: {success_count}, 无颜色: {no_color_count}, 失败: {fail_count})"

                yield f"✅ 颜色识别完成！\n总计: {total_count}\n成功: {success_count}\n无颜色: {no_color_count}\n失败: {fail_count}"
                logger.info(f"颜色识别完成: 成功{success_count}, 无颜色{no_color_count}, 失败{fail_count}")

            finally:
                conn.close()

        except Exception as e:
            yield f"❌ 颜色识别过程出错: {str(e)}"
            logger.error(f"❌ 颜色识别过程出错: {e}", exc_info=True)

    async def _handle_fix_missing_data(self, event: AstrMessageEvent):
        """处理补全缺失宠物数据命令"""
        yield "🔍 开始检查数据库中缺失数据的宠物..."

        try:
            import sys
            import subprocess

            # 获取插件目录
            plugin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wiki')
            script_path = os.path.join(plugin_dir, "tools", "fix_missing_pet_data.py")

            # 检查脚本是否存在
            if not os.path.exists(script_path):
                yield f"❌ 找不到维护脚本: {script_path}"
                return

            yield f"📋 运行数据补全脚本...\n这可能需要几分钟时间，请耐心等待"

            # 执行脚本（非交互模式，自动执行）
            result = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=600,  # 10分钟超时
                cwd=plugin_dir,
                input='y\n'  # 自动确认执行
            )

            if result.returncode == 0:
                output = result.stdout
                # 提取关键信息
                lines = output.split('\n')
                summary_lines = [line for line in lines if '✅' in line or '成功:' in line or '失败:' in line]

                if summary_lines:
                    yield "✅ 数据补全完成！\n\n" + '\n'.join(summary_lines[-5:])
                else:
                    yield "✅ 数据补全完成！请查看日志获取详细信息"

                logger.info(f"数据补全完成:\n{output}")
            else:
                error_msg = result.stderr if result.stderr else result.stdout
                yield f"❌ 数据补全失败:\n{error_msg[:500]}"
                logger.error(f"数据补全失败: {error_msg}")

        except subprocess.TimeoutExpired:
            yield "⏰ 数据补全超时（超过10分钟），请稍后重试或手动运行脚本"
            logger.error("数据补全超时")
        except Exception as e:
            yield f"❌ 执行出错: {str(e)}"
            logger.error(f"❌ 数据补全执行出错: {e}", exc_info=True)

    async def _handle_force_tag_colors(self, event: AstrMessageEvent):
        """处理强制重新识别家具/道具颜色命令（大模型视觉识别，覆盖已有颜色）"""
        yield "🎨 开始强制重新识别所有家具/道具颜色...（使用大模型视觉识别，将覆盖已有颜色）"

        # 检查颜色提取器是否可用
        if not self.color_extractor:
            yield "❌ 颜色提取器不可用，请先在 WebUI 中配置视觉模型"
            return

        try:
            import sqlite3

            # 连接数据库
            db_path = self.config.get("wiki_db_path", "./wiki/wiki-local.db")
            if not os.path.isabs(db_path):
                db_path = os.path.join(plugin_dir, db_path)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            try:
                # 检查并添加 main_color 字段（如果不存在）
                cursor.execute("PRAGMA table_info(items)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'main_color' not in columns:
                    yield "📝 添加 main_color 字段到 items 表"
                    cursor.execute("ALTER TABLE items ADD COLUMN main_color TEXT")
                    conn.commit()

                # 查询所有有图片的家具和道具（不论是否已有颜色）
                cursor.execute("""
                    SELECT name, image_local, category, subcategory, main_color
                    FROM items
                    WHERE image_local IS NOT NULL
                    AND image_local != ''
                """)

                items_list = cursor.fetchall()

                if not items_list:
                    yield "✅ 没有找到需要识别的项目"
                    return

                total_count = len(items_list)
                has_color_count = sum(1 for item in items_list if item[4])
                no_color_count_initial = total_count - has_color_count

                yield f"📋 找到 {total_count} 个项目（已有颜色: {has_color_count}, 无颜色: {no_color_count_initial}）"
                yield "⚠️ 警告：这将覆盖所有已有的颜色数据！"
                yield "💡 提示：使用大模型视觉识别，速度较慢但更准确"

                success_count = 0
                fail_count = 0
                no_color_count = 0
                updated_count = 0

                for i, (name, image_local, category, subcategory, old_color) in enumerate(items_list, 1):
                    # 构建完整图片路径
                    if image_local and not os.path.isabs(image_local):
                        full_path = os.path.join(plugin_dir, image_local)
                    else:
                        full_path = image_local

                    # 检查图片是否存在
                    if not full_path or not os.path.exists(full_path):
                        logger.warning(f"图片不存在: {full_path}")
                        fail_count += 1
                        continue

                    # 使用大模型视觉识别提取颜色
                    result = self.color_extractor.extract_main_colors(full_path)

                    if result and result['main_color']:
                        new_color = result['main_color']
                        cursor.execute(
                            "UPDATE items SET main_color = ? WHERE name = ?",
                            (new_color, name)
                        )
                        conn.commit()
                        success_count += 1

                        # 统计覆盖情况
                        if old_color and old_color != new_color:
                            updated_count += 1
                            logger.info(f"[{i}/{total_count}] {name}: {old_color} → {new_color}")
                        elif not old_color:
                            logger.info(f"[{i}/{total_count}] {name}: 新增 {new_color}")
                        else:
                            logger.info(f"[{i}/{total_count}] {name}: {new_color} (未变化)")
                    elif result and not result['main_color']:
                        no_color_count += 1
                    else:
                        fail_count += 1

                    # 每处理10个发送一次进度（大模型速度慢，降低频率）
                    if i % 10 == 0:
                        yield f"⏳ 进度: {i}/{total_count} (成功: {success_count}, 覆盖: {updated_count}, 无颜色: {no_color_count}, 失败: {fail_count})"

                yield f"✅ 强制颜色识别完成！\n总计: {total_count}\n成功: {success_count}\n覆盖旧值: {updated_count}\n无颜色: {no_color_count}\n失败: {fail_count}"
                logger.info(f"强制颜色识别完成: 成功{success_count}, 覆盖{updated_count}, 无颜色{no_color_count}, 失败{fail_count}")

            finally:
                conn.close()

        except Exception as e:
            yield f"❌ 颜色识别过程出错: {str(e)}"
            logger.error(f"❌ 颜色识别过程出错: {e}", exc_info=True)

    async def _handle_force_tag_pet_colors(self, event: AstrMessageEvent):
        """处理强制重新识别宠物/精灵蛋颜色命令（大模型视觉识别，覆盖已有颜色）"""
        yield "🎨 开始强制重新识别所有宠物/精灵蛋颜色...（使用大模型视觉识别，将覆盖已有颜色）"

        # 检查颜色提取器是否可用
        if not self.color_extractor:
            yield "❌ 颜色提取器不可用，请先在 WebUI 中配置视觉模型"
            return

        try:
            import sqlite3

            # 连接数据库
            db_path = self.config.get("wiki_db_path", "./wiki/wiki-local.db")
            if not os.path.isabs(db_path):
                db_path = os.path.join(plugin_dir, db_path)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            try:
                # 检查并添加 main_color 字段（如果不存在）
                cursor.execute("PRAGMA table_info(pets)")
                columns = [row[1] for row in cursor.fetchall()]
                if 'main_color' not in columns:
                    yield "📝 添加 main_color 字段到 pets 表"
                    cursor.execute("ALTER TABLE pets ADD COLUMN main_color TEXT")
                    conn.commit()

                # 查询所有有图片的宠物（不论是否已有颜色）
                cursor.execute("""
                    SELECT name, sprite_image_local, main_color
                    FROM pets
                    WHERE sprite_image_local IS NOT NULL
                    AND sprite_image_local != ''
                """)

                pets_list = cursor.fetchall()

                if not pets_list:
                    yield "✅ 没有找到需要识别的宠物"
                    return

                total_count = len(pets_list)
                has_color_count = sum(1 for pet in pets_list if pet[2])
                no_color_count_initial = total_count - has_color_count

                yield f"📋 找到 {total_count} 个宠物（已有颜色: {has_color_count}, 无颜色: {no_color_count_initial}）"
                yield "⚠️ 警告：这将覆盖所有已有的颜色数据！"
                yield "💡 提示：使用大模型视觉识别，速度较慢但更准确"

                success_count = 0
                fail_count = 0
                no_color_count = 0
                updated_count = 0

                for i, (name, image_local, old_color) in enumerate(pets_list, 1):
                    # 构建完整图片路径
                    if image_local and not os.path.isabs(image_local):
                        full_path = os.path.join(plugin_dir, image_local)
                    else:
                        full_path = image_local

                    # 检查图片是否存在
                    if not full_path or not os.path.exists(full_path):
                        logger.warning(f"图片不存在: {full_path}")
                        fail_count += 1
                        continue

                    # 使用大模型视觉识别提取颜色
                    result = await self.color_extractor.extract_main_colors_async(full_path)
                    # 清洗非法UTF-8字符，解决MALFORMED错误
                    import re
                    result = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', result)  # 删掉控制符
                    result = result.encode('utf-8', 'ignore').decode('utf-8')  # 强制清理畸形编码
                    result = result.strip()  # 去掉空字符/空格

                    if result and result['main_color']:
                        new_color = result['main_color']
                        cursor.execute(
                            "UPDATE pets SET main_color = ? WHERE name = ?",
                            (new_color, name)
                        )
                        conn.commit()
                        success_count += 1

                        # 统计覆盖情况
                        if old_color and old_color != new_color:
                            updated_count += 1
                            logger.info(f"[{i}/{total_count}] {name}: {old_color} → {new_color}")
                        elif not old_color:
                            logger.info(f"[{i}/{total_count}] {name}: 新增 {new_color}")
                        else:
                            logger.info(f"[{i}/{total_count}] {name}: {new_color} (未变化)")
                    elif result and not result['main_color']:
                        no_color_count += 1
                    else:
                        fail_count += 1

                    # 每处理10个发送一次进度（大模型速度慢，降低频率）
                    if i % 10 == 0:
                        yield f"⏳ 进度: {i}/{total_count} (成功: {success_count}, 覆盖: {updated_count}, 无颜色: {no_color_count}, 失败: {fail_count})"

                yield f"✅ 强制颜色识别完成！\n总计: {total_count}\n成功: {success_count}\n覆盖旧值: {updated_count}\n无颜色: {no_color_count}\n失败: {fail_count}"
                logger.info(f"强制颜色识别完成: 成功{success_count}, 覆盖{updated_count}, 无颜色{no_color_count}, 失败{fail_count}")

            finally:
                conn.close()

        except Exception as e:
            yield f"❌ 颜色识别过程出错: {str(e)}"
            logger.error(f"❌ 颜色识别过程出错: {e}", exc_info=True)
        """插件卸载/停用时清理资源"""
        if self.db_service:
            self.db_service.close()
            logger.info("🔒 数据库连接已关闭")
