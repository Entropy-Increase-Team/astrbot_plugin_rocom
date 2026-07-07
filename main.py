import os
import time
import base64
import tempfile
import asyncio
import re
import json
import random
import shutil
import zipfile
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Callable, Awaitable

import httpx
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, Image

from .core.client import RocomClient
from .core.user import (
    UserManager,
    MerchantSubscriptionManager,
    HomeSubscriptionManager,
    AnnouncementSubscriptionManager,
)
from .core.render import Renderer
from .core.egg_service import EggService, SearchResult
from .core.font_assets import FontAssetManager
from .core.wiki_catalog import (
    WIKI_CATALOG_ROUTES_BY_ALIAS,
    WIKI_CATALOG_ROUTES_BY_KEY,
)

@register("astrbot_plugin_rocom", "bvzrays & 熵增项目组", "洛克王国插件", "v3.7.5", "https://github.com/Entropy-Increase-Team/astrbot_plugin_rocom")
class RocomPlugin(Star):
    _BACKGROUND_REGISTRY_KEY = "_astrbot_plugin_rocom_background_tasks"

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._instance_id = f"{id(self):x}"
        self.config = config or {}
        base_url = self.config.get("api_base_url", "https://wegame.shallow.ink")
        wegame_api_key = self.config.get("wegame_api_key", "")
        
        self.client = RocomClient(
            base_url=base_url,
            wegame_api_key=wegame_api_key,
        )
        self._wiki_catalogs_cache: Dict[str, Any] | None = None
        self._wiki_catalogs_cache_ts = 0.0
        self._wiki_options_cache: Dict[str, Any] | None = None
        self._wiki_options_cache_ts = 0.0
        self._wiki_skill_detail_cache: Dict[str, Dict[str, Any]] = {}
        self._wiki_pet_size_cache: Dict[str, Dict[str, Any]] = {}
        
        data_dir = str(StarTools.get_data_dir())
        self.data_dir = data_dir
        self.atlas_dir = os.path.join(data_dir, "rocom_atlas")
        self.user_mgr = UserManager(data_dir)
        self.merchant_sub_mgr = MerchantSubscriptionManager(data_dir)
        self.home_sub_mgr = HomeSubscriptionManager(data_dir)
        self.announcement_sub_mgr = AnnouncementSubscriptionManager(data_dir)
        
        render_timeout = self.config.get("render_timeout", 30000)
        self.low_bandwidth_mode = bool(self.config.get("low_bandwidth_mode", False))
        self.help_prefix_display = str(self.config.get("help_prefix_display", "") or "")
        # res_path point to astrbot_plugin_rocom directory
        res_path = os.path.abspath(os.path.dirname(__file__))
        self.font_paths = FontAssetManager(res_path=res_path, data_dir=data_dir).ensure_fonts()
        self.renderer = Renderer(
            res_path=res_path,
            render_timeout=render_timeout,
            font_paths=self.font_paths,
        )
        self.home_plant_map = self._load_home_plant_map(res_path)
        
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
        self._merchant_jitter_seconds = 30
        self.home_subscription_enabled = self.config.get(
            "home_subscription_enabled", True
        )
        try:
            self.home_subscription_interval_minutes = int(
                self.config.get("home_subscription_interval_minutes", 5) or 5
            )
        except (TypeError, ValueError):
            self.home_subscription_interval_minutes = 5
        self._home_subscription_task = None
        self.announcement_subscription_enabled = self.config.get(
            "announcement_subscription_enabled", True
        )
        try:
            self.announcement_poll_interval_minutes = int(
                self.config.get("announcement_poll_interval_minutes", 10) or 10
            )
        except (TypeError, ValueError):
            self.announcement_poll_interval_minutes = 10
        self._announcement_subscription_task = None
        
        # 启动时检查是否需要开启自动刷新
        logger.info(f"[Rocom] 插件初始化完成，自动刷新启用状态：{self.auto_refresh_enabled}, 刷新时间：{self.auto_refresh_time}, 通知群：{self.auto_refresh_notify_group}")
        self._cancel_stale_background_tasks()
        if self.auto_refresh_enabled:
            self._auto_refresh_task = self._register_background_task(
                "auto_refresh",
                self._auto_refresh_loop(),
            )
            logger.info("[Rocom] 自动刷新任务已启动")
        else:
            logger.info("[Rocom] 自动刷新功能未启用")
        
        if self.merchant_subscription_enabled:
            self._merchant_subscription_task = self._register_background_task(
                "merchant_subscription",
                self._merchant_subscription_loop(),
            )
        if self.home_subscription_enabled:
            self._home_subscription_task = self._register_background_task(
                "home_subscription",
                self._home_subscription_loop(),
            )
        if self.announcement_subscription_enabled:
            self._announcement_subscription_task = self._register_background_task(
                "announcement_subscription",
                self._announcement_subscription_loop(),
            )

    def _background_task_registry(self) -> Dict[str, asyncio.Task]:
        loop = asyncio.get_running_loop()
        registry = getattr(loop, self._BACKGROUND_REGISTRY_KEY, None)
        if not isinstance(registry, dict):
            registry = {}
            setattr(loop, self._BACKGROUND_REGISTRY_KEY, registry)
        return registry

    def _cancel_stale_background_tasks(self):
        registry = self._background_task_registry()
        for name, task in list(registry.items()):
            if task and not task.done():
                logger.warning(f"[Rocom] 取消旧后台任务：{name}")
                task.cancel()
        registry.clear()

    def _register_background_task(self, name: str, coro) -> asyncio.Task:
        task = asyncio.create_task(
            coro,
            name=f"rocom:{name}:{self._instance_id}",
        )
        self._background_task_registry()[name] = task
        return task

    def _unregister_background_task(self, name: str, task: asyncio.Task | None):
        if not task:
            return
        registry = self._background_task_registry()
        if registry.get(name) is task:
            registry.pop(name, None)

    async def terminate(self):
        if self._announcement_subscription_task and not self._announcement_subscription_task.done():
            self._announcement_subscription_task.cancel()
            try:
                await self._announcement_subscription_task
            except asyncio.CancelledError:
                pass
        self._unregister_background_task(
            "announcement_subscription",
            self._announcement_subscription_task,
        )
        if self._home_subscription_task and not self._home_subscription_task.done():
            self._home_subscription_task.cancel()
            try:
                await self._home_subscription_task
            except asyncio.CancelledError:
                pass
        self._unregister_background_task(
            "home_subscription",
            self._home_subscription_task,
        )
        if self._merchant_subscription_task and not self._merchant_subscription_task.done():
            self._merchant_subscription_task.cancel()
            try:
                await self._merchant_subscription_task
            except asyncio.CancelledError:
                pass
        self._unregister_background_task(
            "merchant_subscription",
            self._merchant_subscription_task,
        )
        if self._auto_refresh_task and not self._auto_refresh_task.done():
            self._auto_refresh_task.cancel()
            try:
                await self._auto_refresh_task
            except asyncio.CancelledError:
                pass
        self._unregister_background_task("auto_refresh", self._auto_refresh_task)
        await self.client.close()
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

    async def _resolve_ingame_identity(
        self, event: AstrMessageEvent, uid: str = ""
    ) -> tuple[str, str, str]:
        uid = str(uid or "").strip()
        user_identifier = self._get_user_identifier(event)
        if uid:
            return uid, "", user_identifier

        binding = await self.user_mgr.get_primary_binding(event.get_sender_id())
        if not binding:
            return "", "", user_identifier

        return (
            str(binding.get("role_id", "") or ""),
            str(binding.get("framework_token", "") or ""),
            user_identifier,
        )

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

    async def _resolve_home_uid(self, event: AstrMessageEvent, uid: str = "") -> str:
        uid = str(uid or "").strip()
        if uid:
            return uid
        binding = await self.user_mgr.get_primary_binding(event.get_sender_id())
        return str((binding or {}).get("role_id", "") or "")

    def _home_subscription_key(self, session_id: str, uid: str, kind: str) -> str:
        return f"{session_id}:{uid}:{kind}"

    def _normalize_epoch_seconds(self, value: Any) -> int:
        try:
            ts = int(float(value))
        except (TypeError, ValueError):
            return 0
        if ts > 10_000_000_000_000:
            return ts // 1_000_000
        if ts > 10_000_000_000:
            return ts // 1000
        return ts

    def _normalize_duration_seconds(self, value: Any) -> int:
        try:
            seconds = int(float(value))
        except (TypeError, ValueError):
            return 0
        if seconds > 1_000_000_000:
            return seconds // 1_000_000
        if seconds > 1_000_000:
            return seconds // 1000
        return seconds

    def _format_home_remaining(self, target_ts: int, now_ts: int | None = None) -> str:
        if not target_ts:
            return "未开始"
        now_ts = now_ts or int(time.time())
        remain = max(0, int(target_ts) - now_ts)
        if remain <= 0:
            return "已完成"
        hours, remainder = divmod(remain, 3600)
        minutes, _ = divmod(remainder, 60)
        if hours >= 24:
            days, hours = divmod(hours, 24)
            return f"{days}天{hours}小时"
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        return f"{minutes}分钟"

    def _home_info_payload(self, res: Dict[str, Any] | None) -> Dict[str, Any]:
        payload = res or {}
        if isinstance(payload.get("result"), dict) and isinstance(payload["result"].get("home_info"), dict):
            return payload["result"]["home_info"]
        if isinstance(payload.get("home_info"), dict):
            return payload["home_info"]
        if isinstance(payload.get("data"), dict):
            data = payload["data"]
            if isinstance(data.get("result"), dict) and isinstance(data["result"].get("home_info"), dict):
                return data["result"]["home_info"]
            if isinstance(data.get("home_info"), dict):
                return data["home_info"]
        return payload if isinstance(payload, dict) else {}

    def _home_brief_info(self, home_info: Dict[str, Any]) -> Dict[str, Any]:
        return home_info.get("friend_home_brief_info") or home_info.get("home_brief_info") or home_info or {}

    def _home_cell_info(self, home_info: Dict[str, Any]) -> Dict[str, Any]:
        return home_info.get("friend_cell_home_brief_info") or home_info.get("cell_home_brief_info") or {}

    def _home_pet_icon(self, pet_id: Any, icon_url: str = "") -> str:
        if icon_url:
            return icon_url
        try:
            asset_id = int(str(pet_id))
        except (TypeError, ValueError):
            return ""
        if asset_id <= 0:
            return ""
        if asset_id < 3000:
            asset_id += 3000
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{asset_id}/icon.png"

    def _extract_home_pet(self, raw: Dict[str, Any], index: int, guard: bool = False) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        home_pet = raw.get("home_pet_info") if isinstance(raw.get("home_pet_info"), dict) else raw
        display = raw.get("display_info") if isinstance(raw.get("display_info"), dict) else {}
        pet_id = home_pet.get("pet_cfg_id") or home_pet.get("pet_id") or home_pet.get("pet_base_id") or raw.get("pet_cfg_id") or raw.get("pet_id") or raw.get("id")
        if str(pet_id or "0") in {"", "0"} and not guard:
            return None
        name = home_pet.get("name") or home_pet.get("pet_name") or raw.get("name") or raw.get("pet_name") or f"精灵 {pet_id}"
        feed_info = home_pet.get("feed_info") if isinstance(home_pet.get("feed_info"), dict) else {}
        begin_time = self._normalize_epoch_seconds(feed_info.get("begin_time"))
        time_cost = self._normalize_duration_seconds(feed_info.get("time_cost"))
        rip_time = self._normalize_epoch_seconds(home_pet.get("pet_rip_time") or raw.get("pet_rip_time") or raw.get("rip_time"))
        if not rip_time and begin_time and time_cost:
            rip_time = begin_time + time_cost
        now_ts = int(time.time())
        has_inspiration = bool(rip_time)
        inspire_ready = has_inspiration and now_ts >= rip_time
        egg_time = self._normalize_epoch_seconds(
            raw.get("predicted_egg_time")
            or home_pet.get("predicted_egg_time")
            or raw.get("egg_time")
            or home_pet.get("egg_time")
        )
        egg_ready = bool(egg_time and now_ts >= egg_time)
        speciality_values = []
        for value in (
            home_pet.get("real_speciality_ids"),
            raw.get("real_speciality_ids"),
            home_pet.get("speciality_id"),
            raw.get("speciality_id"),
        ):
            if isinstance(value, list):
                speciality_values.extend(value)
            elif value not in (None, ""):
                speciality_values.append(value)
        speciality_ids = {str(value).strip() for value in speciality_values if str(value).strip()}
        mutation_name = str(display.get("mutation_name") or raw.get("mutation_name") or home_pet.get("mutation_name") or "")
        variant_text = ""
        if "异色" in mutation_name and "炫彩" in mutation_name:
            variant_text = "异色炫彩"
        elif "异色" in mutation_name:
            variant_text = "异色"
        elif "炫彩" in mutation_name:
            variant_text = "炫彩"
        elif "103" in speciality_ids and "502" in speciality_ids:
            variant_text = "异色炫彩"
        elif "103" in speciality_ids:
            variant_text = "异色"
        elif "502" in speciality_ids:
            variant_text = "炫彩"
        is_shiny = variant_text in {"异色", "异色炫彩"}
        status = raw.get("status")
        is_guard = guard or bool(raw.get("is_guard") or raw.get("guard")) or str(status).lower() in {"2", "guard", "守卫"}
        status_text = "守卫中" if is_guard and not has_inspiration else ("灵感已完成" if inspire_ready else ("灵感收集中" if has_inspiration else "未喂食"))
        status_class = "guard" if is_guard and not has_inspiration else ("ready" if inspire_ready else ("progress" if has_inspiration else "idle"))
        return {
            "id": str(pet_id),
            "pos": raw.get("pos") or raw.get("position") or index + 1,
            "name": str(name),
            "level": display.get("level") or raw.get("level") or home_pet.get("level") or "--",
            "iconUrl": self._home_pet_icon(pet_id, raw.get("icon_url") or raw.get("pet_img_url") or raw.get("petIcon") or ""),
            "badge": "守" if is_guard else "",
            "isShiny": is_shiny,
            "variantText": variant_text,
            "isGuard": is_guard,
            "statusText": status_text,
            "statusClass": status_class,
            "note": self._format_home_remaining(rip_time, now_ts) if has_inspiration else ("家园守卫位" if is_guard else "暂无灵感倒计时"),
            "hasEgg": bool(raw.get("have_egg") or home_pet.get("have_egg")),
            "eggReady": egg_ready,
            "eggTime": egg_time,
            "eggText": ("可能已生蛋" if egg_ready else f"预计生蛋 {self._format_home_remaining(egg_time, now_ts)}") if egg_time else "",
            "inspireReady": inspire_ready,
            "readyAt": rip_time,
            "eventId": f"pet:{raw.get('pos') or index + 1}:{pet_id}:{rip_time}",
        }

    def _home_pet_sources(self, home_info: Dict[str, Any]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        cell = self._home_cell_info(home_info)
        indoor_sources = []
        guard_sources = []
        if isinstance(home_info.get("home_pets"), list):
            indoor_sources.extend(home_info.get("home_pets") or [])
        if isinstance(cell.get("home_pets"), list):
            for pet in cell.get("home_pets") or []:
                home_pet = pet.get("home_pet_info") if isinstance(pet, dict) and isinstance(pet.get("home_pet_info"), dict) else {}
                if str(home_pet.get("pet_cfg_id") or "0") == "0" and (home_pet.get("name") or home_pet.get("pet_name")):
                    guard_sources.append(pet)
                else:
                    indoor_sources.append(pet)
        pet_info = cell.get("home_pet_info") if isinstance(cell.get("home_pet_info"), dict) else {}
        if isinstance(pet_info.get("home_pet_list"), list):
            indoor_sources.extend(pet_info.get("home_pet_list") or [])
        for key in ("guard_pets", "home_guard_pets", "guard_pet_list"):
            if isinstance(home_info.get(key), list):
                guard_sources.extend(home_info.get(key) or [])
            if isinstance(cell.get(key), list):
                guard_sources.extend(cell.get(key) or [])
        for key in ("guard_pet", "home_guard_pet", "guard_pet_info", "home_guard_pet_info", "defend_pet", "defend_pet_info", "protect_pet", "protect_pet_info"):
            if isinstance(home_info.get(key), dict):
                guard_sources.append(home_info.get(key))
            if isinstance(cell.get(key), dict):
                guard_sources.append(cell.get(key))
        for key in ("guard_pet_info", "home_guard_pet_info"):
            info = cell.get(key) if isinstance(cell.get(key), dict) else home_info.get(key)
            if isinstance(info, dict):
                for list_key in ("guard_pet_list", "home_guard_pet_list", "pet_list"):
                    if isinstance(info.get(list_key), list):
                        guard_sources.extend(info.get(list_key) or [])
        return indoor_sources, guard_sources

    def _load_home_plant_map(self, res_path: str) -> Dict[str, Any]:
        path = os.path.join(res_path, "render", "home", "data", "home_item_list.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[Rocom] 加载家园作物映射失败: {e}")
            return {}

    def _home_plant_icon(self, icon_id: Any) -> str:
        if not icon_id:
            return ""
        icon_text = str(icon_id)
        if icon_text.startswith(("http://", "https://", "data:")):
            return icon_text
        return f"img/home_icon/{icon_text}_2.png"

    def _extract_home_plants(self, home_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        cell = self._home_cell_info(home_info)
        plant_sources = []
        if isinstance(home_info.get("home_plants"), list):
            plant_sources.extend(home_info.get("home_plants") or [])
        plant_info = cell.get("home_plant_info") if isinstance(cell.get("home_plant_info"), dict) else {}
        land_list = plant_info.get("home_plant_land_list") if isinstance(plant_info.get("home_plant_land_list"), list) else []
        for land in land_list:
            if not isinstance(land, dict):
                continue
            for item in land.get("home_plant_list") or []:
                if isinstance(item, dict):
                    copied = dict(item)
                    copied.setdefault("land_index", land.get("land_index"))
                    plant_sources.append(copied)
        now_ts = int(time.time())
        result = []
        for index, raw in enumerate(plant_sources):
            plant_data = raw.get("plant_info") if isinstance(raw.get("plant_info"), dict) else raw
            plant_id = raw.get("plant_seed_id") or raw.get("plant_cfg_id") or raw.get("plant_id") or plant_data.get("id")
            if str(plant_id or "0") in {"", "0"}:
                continue
            mapped_plant = getattr(self, "home_plant_map", {}).get(str(plant_id), {})
            icon_id = (
                plant_data.get("icon_url")
                or plant_data.get("iconUrl")
                or raw.get("icon_url")
                or raw.get("iconUrl")
                or plant_data.get("iconid")
                or raw.get("iconid")
                or raw.get("icon_id")
                or (mapped_plant.get("iconid") if isinstance(mapped_plant, dict) else "")
            )
            rip_time = self._normalize_epoch_seconds(raw.get("plant_rip_time") or raw.get("rip_time") or raw.get("end_time"))
            left_time = int(raw.get("left_time") or 0)
            if not rip_time and left_time > 0:
                rip_time = now_ts + left_time
            ready = bool(rip_time and now_ts >= rip_time) or (raw.get("status") in {2, "ready", "mature"})
            total = int(raw.get("time_cost") or raw.get("total_time") or 0)
            if not total and raw.get("plant_tab_id"):
                try:
                    total = int(raw.get("plant_tab_id")) * 21600
                except (TypeError, ValueError):
                    total = 0
            progress = int(max(0, min(100, ((total - max(0, rip_time - now_ts)) / total) * 100))) if total and rip_time else (100 if ready else 35)
            land_index = raw.get("slot_index") or raw.get("land_index") or index + 1
            harvest_num = raw.get("plant_harvest_num")
            steal_account = raw.get("plant_steal_account")
            can_steal_account = raw.get("plant_can_steal_account")
            result.append({
                "id": str(plant_id),
                "landIndex": land_index,
                "plantName": plant_data.get("name") or raw.get("name") or (mapped_plant.get("name") if isinstance(mapped_plant, dict) else "") or f"种子 {plant_id}",
                "iconUrl": self._home_plant_icon(icon_id),
                "stateType": "ready" if ready else "warning",
                "statusText": "已成熟" if ready else "成长中",
                "leftTimeText": "可收获" if ready else self._format_home_remaining(rip_time, now_ts),
                "progress": progress,
                "ready": ready,
                "readyAt": rip_time,
                "harvestText": f"产量 {harvest_num}" if harvest_num not in (None, "") else "",
                "stealText": f"可偷 {steal_account}/{can_steal_account}" if steal_account not in (None, "") and can_steal_account not in (None, "") else "",
                "eventId": f"plant:{raw.get('slot_index') or raw.get('land_index') or index}:{plant_id}:{rip_time}",
            })
        return result

    def _build_home_render_data(self, res: Dict[str, Any] | None, uid: str) -> Dict[str, Any]:
        home_info = self._home_info_payload(res)
        brief = self._home_brief_info(home_info)
        indoor_sources, guard_sources = self._home_pet_sources(home_info)
        indoor_pets = []
        guard_pets = []
        for index, raw in enumerate(indoor_sources):
            item = self._extract_home_pet(raw, index)
            if not item:
                continue
            if item["isGuard"]:
                guard_pets.append(item)
            else:
                indoor_pets.append(item)
        for index, raw in enumerate(guard_sources):
            item = self._extract_home_pet(raw, index, guard=True)
            if item:
                guard_pets.append(item)
        garden_plots = self._extract_home_plants(home_info)
        home_name = brief.get("home_name") or brief.get("name") or f"{uid} 的小屋"
        meta = (res or {}).get("meta") or {}
        created_at = self._normalize_epoch_seconds(meta.get("created_at"))
        updated_at = datetime.fromtimestamp(created_at, tz=self._cn_tz()).strftime("%Y-%m-%d %H:%M:%S") if created_at else datetime.now(self._cn_tz()).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "title": "洛克家园",
            "subtitle": "Home Information",
            "homeName": home_name,
            "uid": uid,
            "summaryCards": [
                {"label": "房间等级", "value": brief.get("room_level", "--")},
                {"label": "家园等级", "value": brief.get("home_level", "--")},
                {"label": "家园经验", "value": brief.get("home_experience", "--")},
                {"label": "舒适度", "value": brief.get("home_comfort_level", "--")},
            ],
            "gardenPlots": garden_plots,
            "guardPets": guard_pets,
            "indoorPets": indoor_pets,
            "gardenCount": len(garden_plots),
            "guardCount": len(guard_pets),
            "indoorCount": len(indoor_pets),
            "guardEmptyText": "后端当前返回中没有守卫精灵字段",
            "updatedAt": updated_at,
        }

    def _pet_data_display(self, value: Any, default: str = "--") -> str:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return "是" if value else "否"
        return str(value)

    def _pet_data_image_url(self, pet_id: Any, image_type: str = "image") -> str:
        try:
            asset_id = int(str(pet_id))
        except (TypeError, ValueError):
            return ""
        if asset_id <= 0:
            return ""
        if asset_id < 3000:
            asset_id += 3000
        image_type = "icon" if image_type == "icon" else "image"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{asset_id}/{image_type}.png"

    def _pet_data_time_text(self, value: Any) -> str:
        ts = self._normalize_epoch_seconds(value)
        if not ts:
            return "--"
        return datetime.fromtimestamp(ts, tz=self._cn_tz()).strftime("%Y-%m-%d %H:%M")

    def _pet_data_size_text(self, value: Any, unit: str) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "--"
        if unit == "g":
            return f"{number / 1000:.2f} kg"
        if unit == "cm":
            return f"{number:g} cm"
        return f"{number:g} {unit}".strip()

    def _pet_data_voice_text(self, value: Any) -> str:
        if value in (None, ""):
            return "--"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return self._pet_data_display(value)
        if number.is_integer():
            return f"{int(number)} dB"
        return f"{number:g} dB"

    def _pet_data_voice_hint(self, value: Any) -> str:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return ""
        if 96 <= number <= 100:
            return "婉转声 96~100"
        if -100 <= number <= -96:
            return "粗嗓门 -96~-100"
        return "婉转声 96~100 / 粗嗓门 -96~-100"

    def _pet_data_voice_info(self, value: Any) -> Dict[str, str]:
        text = self._pet_data_voice_text(value)
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return {"value": text, "hint": "", "className": ""}
        if 96 <= number <= 100:
            return {
                "value": f"{text} · 婉转声",
                "hint": "婉转声 96~100",
                "className": "voice-soft",
            }
        if -100 <= number <= -96:
            return {
                "value": f"{text} · 粗嗓门",
                "hint": "粗嗓门 -96~-100",
                "className": "voice-rough",
            }
        return {
            "value": text,
            "hint": "婉转声 96~100 / 粗嗓门 -96~-100",
            "className": "",
        }

    def _pet_data_wiki_pet_id(self, value: Any) -> str:
        try:
            pet_id = int(str(value))
        except (TypeError, ValueError):
            return ""
        if pet_id <= 0:
            return ""
        if pet_id < 3000:
            pet_id += 3000
        return str(pet_id)

    def _pet_data_kg_compact(self, grams: Any) -> str:
        try:
            number = float(grams) / 1000
        except (TypeError, ValueError):
            return "--"
        text = f"{number:.2f}".rstrip("0").rstrip(".")
        return f"{text}kg"

    def _pet_data_weight_size_info(
        self,
        pet: Dict[str, Any],
        wiki_pet: Dict[str, Any] | None,
    ) -> Dict[str, str]:
        body_size = wiki_pet.get("body_size") if isinstance(wiki_pet, dict) else {}
        weight_range = body_size.get("weight") if isinstance(body_size, dict) else {}
        if not isinstance(weight_range, dict):
            return {}
        try:
            current = float(pet.get("weight"))
            low = float(weight_range.get("min_g"))
            high = float(weight_range.get("max_g"))
        except (TypeError, ValueError):
            return {}
        if high <= low:
            return {}

        span = high - low
        small_cut = low + span * 0.05
        large_cut = high - span * 0.05
        range_text = f"{self._pet_data_kg_compact(low)}-{self._pet_data_kg_compact(high)}"
        if current <= small_cut:
            return {
                "label": "小块头",
                "className": "size-small",
                "hint": f"小块头 · ≤{self._pet_data_kg_compact(small_cut)} · 范围 {range_text}",
            }
        if current >= large_cut:
            return {
                "label": "大块头",
                "className": "size-large",
                "hint": f"大块头 · ≥{self._pet_data_kg_compact(large_cut)} · 范围 {range_text}",
            }
        return {
            "label": "",
            "className": "",
            "hint": f"范围 {range_text}",
        }

    def _pet_data_option_maps(self, options: Dict[str, Any] | None) -> Dict[str, Dict[str, str]]:
        pet_options = (options or {}).get("pet") if isinstance(options, dict) else {}
        if not isinstance(pet_options, dict):
            pet_options = {}

        def build_map(key: str, prefer_short: bool = False) -> Dict[str, str]:
            result: Dict[str, str] = {}
            values = pet_options.get(key)
            if not isinstance(values, list):
                return result
            for item in values:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id")
                if item_id in (None, ""):
                    continue
                name = item.get("short_name") if prefer_short and item.get("short_name") else item.get("name")
                if name:
                    result[str(item_id)] = str(name)
            return result

        natures: Dict[str, str] = {}
        for item in pet_options.get("natures") or []:
            if not isinstance(item, dict) or item.get("id") in (None, ""):
                continue
            name = str(item.get("name") or item.get("id"))
            summary = str(item.get("summary") or "").strip()
            natures[str(item.get("id"))] = f"{name}（{summary}）" if summary else name

        return {
            "natures": natures,
            "bloodlines": build_map("bloodlines", prefer_short=True),
            "types": build_map("types"),
            "talent_ratings": build_map("talent_ratings"),
        }

    def _pet_data_lookup(self, mapping: Dict[str, str], value: Any, default: str = "--") -> str:
        key = str(value or "").strip()
        if not key:
            return default
        return mapping.get(key) or f"{key}"

    def _pet_data_variant(self, pet: Dict[str, Any], fallback: Dict[str, Any] | None = None) -> tuple[str, str]:
        fallback = fallback or {}
        mutation_name = str(
            pet.get("mutation_name")
            or fallback.get("mutation_name")
            or fallback.get("pet_mutation_name")
            or ""
        ).strip()
        mutation_type = str(pet.get("mutation_type") or fallback.get("mutation_type") or "").strip()
        speciality_values = []
        for value in (
            pet.get("real_speciality_ids"),
            fallback.get("real_speciality_ids"),
            pet.get("speciality_id"),
            fallback.get("speciality_id"),
        ):
            if isinstance(value, list):
                speciality_values.extend(value)
            elif value not in (None, ""):
                speciality_values.append(value)
        speciality_ids = {str(value).strip() for value in speciality_values if str(value).strip()}

        if mutation_type == "9" or ("异色" in mutation_name and "炫彩" in mutation_name) or ("103" in speciality_ids and "502" in speciality_ids):
            return "异色炫彩", "异色炫彩.png"
        if mutation_type == "1" or "异色" in mutation_name or "103" in speciality_ids:
            return "异色", "异色.png"
        if mutation_type == "8" or "炫彩" in mutation_name or "502" in speciality_ids:
            return "炫彩", "炫彩.png"
        return mutation_name or "普通", ""

    def _pet_data_attributes(self, pet: Dict[str, Any]) -> List[Dict[str, str]]:
        attribute_info = pet.get("attribute_info") if isinstance(pet.get("attribute_info"), dict) else {}
        fields = [
            ("hp", "生命"),
            ("attack", "物攻"),
            ("special_attack", "魔攻"),
            ("defense", "物防"),
            ("special_defense", "魔防"),
            ("speed", "速度"),
        ]
        result = []
        for key, label in fields:
            raw = attribute_info.get(key) if isinstance(attribute_info.get(key), dict) else {}
            try:
                percent = int(max(6, min(100, float(raw.get("base_value") or 0) / 200 * 100)))
            except (TypeError, ValueError):
                percent = 6
            result.append({
                "label": label,
                "value": self._pet_data_display(raw.get("base_value")),
                "race": self._pet_data_display(raw.get("total_race")),
                "talent": self._pet_data_display(raw.get("talent")),
                "percent": percent,
            })
        return result

    def _pet_data_skill_items(self, pet: Dict[str, Any]) -> List[Dict[str, Any]]:
        skill_root = pet.get("skill") if isinstance(pet.get("skill"), dict) else {}
        skills = skill_root.get("skill_data") if isinstance(skill_root.get("skill_data"), list) else []

        def sort_key(item: Dict[str, Any]) -> tuple[int, int, int]:
            equipped_rank = 0 if item.get("is_equipped") else 1
            learned_rank = 0 if item.get("is_learned") else 1
            try:
                pos = int(item.get("pos") or 99)
            except (TypeError, ValueError):
                pos = 99
            return equipped_rank, learned_rank, pos

        return sorted([s for s in skills if isinstance(s, dict)], key=sort_key)

    def _pet_data_skill_ids_from_payload(self, payload: Dict[str, Any]) -> List[str]:
        raw_items: List[Dict[str, Any]] = []
        if isinstance(payload.get("result"), dict):
            payload = payload.get("result") or payload
        if isinstance(payload.get("npc_pets"), list):
            raw_items.extend([item for item in payload.get("npc_pets") or [] if isinstance(item, dict)])
        elif isinstance(payload.get("npc_pet"), dict):
            raw_items.append({"npc_pet": payload.get("npc_pet")})

        ids: List[str] = []
        for raw in raw_items:
            npc_pet = raw.get("npc_pet") if isinstance(raw.get("npc_pet"), dict) else {}
            pet = npc_pet.get("pet") if isinstance(npc_pet.get("pet"), dict) else {}
            for item in self._pet_data_skill_items(pet)[:8]:
                skill_id = str(item.get("id") or "").strip()
                if skill_id and skill_id not in ids:
                    ids.append(skill_id)
        return ids

    def _pet_data_pet_ids_from_payload(self, payload: Dict[str, Any]) -> List[str]:
        raw_items: List[Dict[str, Any]] = []
        if isinstance(payload.get("result"), dict):
            payload = payload.get("result") or payload
        if isinstance(payload.get("npc_pets"), list):
            raw_items.extend([item for item in payload.get("npc_pets") or [] if isinstance(item, dict)])
        elif isinstance(payload.get("npc_pet"), dict):
            raw_items.append({"npc_pet": payload.get("npc_pet")})

        ids: List[str] = []
        for raw in raw_items:
            npc_pet = raw.get("npc_pet") if isinstance(raw.get("npc_pet"), dict) else {}
            pet = npc_pet.get("pet") if isinstance(npc_pet.get("pet"), dict) else {}
            pet_id = self._pet_data_wiki_pet_id(
                pet.get("base_conf_id")
                or raw.get("pet_cfg_id")
                or pet.get("catch_base_id")
                or pet.get("conf_id")
            )
            if pet_id and pet_id not in ids:
                ids.append(pet_id)
        return ids

    async def _pet_data_wiki_skill_lookup(self, res: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]]:
        ids = self._pet_data_skill_ids_from_payload(res or {})
        if not ids:
            return {}

        lookup: Dict[str, Dict[str, Any]] = {}
        missing = []
        for skill_id in ids:
            cached = self._wiki_skill_detail_cache.get(skill_id)
            if isinstance(cached, dict):
                lookup[skill_id] = cached
            else:
                missing.append(skill_id)

        semaphore = asyncio.Semaphore(6)

        async def fetch(skill_id: str):
            async with semaphore:
                detail = await self.client.get_wiki_skill(skill_id)
            if isinstance(detail, dict):
                self._wiki_skill_detail_cache[skill_id] = detail
                lookup[skill_id] = detail

        if missing:
            await asyncio.gather(*(fetch(skill_id) for skill_id in missing), return_exceptions=True)
        return lookup

    async def _pet_data_wiki_size_lookup(self, res: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]]:
        ids = self._pet_data_pet_ids_from_payload(res or {})
        if not ids:
            return {}

        lookup: Dict[str, Dict[str, Any]] = {}
        missing = []
        for pet_id in ids:
            cached = self._wiki_pet_size_cache.get(pet_id)
            if isinstance(cached, dict):
                lookup[pet_id] = cached
            else:
                missing.append(pet_id)

        semaphore = asyncio.Semaphore(6)

        async def fetch(pet_id: str):
            async with semaphore:
                detail = await self.client.get_wiki_pet(pet_id)
            if isinstance(detail, dict):
                self._wiki_pet_size_cache[pet_id] = detail
                lookup[pet_id] = detail

        if missing:
            await asyncio.gather(*(fetch(pet_id) for pet_id in missing), return_exceptions=True)
        return lookup

    def _pet_data_skill_icon_url(self, skill_id: Any, detail: Dict[str, Any] | None = None) -> str:
        detail = detail or {}
        icon = str(detail.get("icon") or "").strip()
        if icon:
            return icon
        skill_text = str(skill_id or "").strip()
        if not skill_text:
            return ""
        return f"{self.client.base_url}/api/v1/resources/wiki/assets/skills/{skill_text}.png"

    def _pet_data_skills(
        self,
        pet: Dict[str, Any],
        skill_lookup: Dict[str, Dict[str, Any]] | None = None,
        load_skill_icons: bool = True,
    ) -> List[Dict[str, str]]:
        skill_lookup = skill_lookup or {}
        result = []
        for item in self._pet_data_skill_items(pet)[:8]:
            skill_id = self._pet_data_display(item.get("id"))
            detail = skill_lookup.get(str(skill_id)) if skill_id != "--" else {}
            if item.get("is_equipped"):
                status = "已装备"
                status_class = "equipped"
            elif item.get("is_learned"):
                status = "已学会"
                status_class = "learned"
            else:
                status = "未学会"
                status_class = "locked"
            element = self._wiki_named_value((detail or {}).get("element_type")) if detail else ""
            skill_type = self._wiki_named_value((detail or {}).get("skill_type") or (detail or {}).get("damage_type")) if detail else ""
            power = (detail or {}).get("power")
            cost = (detail or {}).get("cost")
            result.append({
                "id": skill_id,
                "name": str((detail or {}).get("name") or skill_id),
                "icon": self._pet_data_skill_icon_url(skill_id, detail) if load_skill_icons else "",
                "element": element or "未知",
                "type": skill_type or "技能",
                "power": self._pet_data_display(power),
                "cost": self._pet_data_display(cost),
                "pos": self._pet_data_display(item.get("pos")),
                "status": status,
                "statusClass": status_class,
                "unlock": f"Lv.{item.get('unlock_need_lv')}" if item.get("unlock_need_lv") not in (None, "", 0) else "--",
                "description": str((detail or {}).get("description") or (detail or {}).get("desc") or ""),
            })
        return result

    def _pet_data_card_items(
        self,
        pet: Dict[str, Any],
        option_maps: Dict[str, Dict[str, str]],
        variant_text: str,
        size_info: Dict[str, str] | None = None,
    ) -> List[Dict[str, Any]]:
        gender_map = {"0": "未知", "1": "雄性", "2": "雌性"}
        gender = gender_map.get(str(pet.get("gender") or ""), self._pet_data_display(pet.get("gender")))
        nature = self._pet_data_lookup(option_maps.get("natures", {}), pet.get("nature"))
        blood = self._pet_data_lookup(option_maps.get("bloodlines", {}), pet.get("blood_id"))
        talent_rank = self._pet_data_lookup(option_maps.get("talent_ratings", {}), pet.get("talent_rank"), default=self._pet_data_display(pet.get("talent_rank")))
        voice_info = self._pet_data_voice_info(pet.get("voice"))
        size_info = size_info or {}
        weight_value = self._pet_data_size_text(pet.get("weight"), "g")
        if size_info.get("label"):
            weight_value = f"{weight_value} · {size_info['label']}"
        return [
            {"label": "等级", "value": self._pet_data_display(pet.get("level"))},
            {"label": "性别", "value": gender},
            {"label": "分贝", **voice_info},
            {"label": "性格", "value": nature},
            {"label": "血脉", "value": blood},
            {"label": "天赋评级", "value": talent_rank},
            {"label": "身高", "value": self._pet_data_size_text(pet.get("height"), "cm")},
            {
                "label": "体重",
                "value": weight_value,
                "hint": size_info.get("hint", ""),
                "className": size_info.get("className", ""),
            },
        ]

    def _pet_data_extract_items(
        self,
        payload: Dict[str, Any],
        option_maps: Dict[str, Dict[str, str]],
        skill_lookup: Dict[str, Dict[str, Any]] | None = None,
        size_lookup: Dict[str, Dict[str, Any]] | None = None,
        load_skill_icons: bool = True,
    ) -> List[Dict[str, Any]]:
        raw_items: List[Dict[str, Any]] = []
        if isinstance(payload.get("npc_pets"), list):
            raw_items.extend([item for item in payload.get("npc_pets") or [] if isinstance(item, dict)])
        elif isinstance(payload.get("npc_pet"), dict):
            query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
            raw_items.append({
                "status": "ok",
                "npc_pet": payload.get("npc_pet"),
                "pet_gid": query.get("pet_gid"),
                "furniture_guid": query.get("npc_id") or query.get("furniture_guid"),
                "npc_id_source": "query",
            })

        pets = []
        for index, raw in enumerate(raw_items):
            npc_pet = raw.get("npc_pet") if isinstance(raw.get("npc_pet"), dict) else {}
            pet = npc_pet.get("pet") if isinstance(npc_pet.get("pet"), dict) else {}
            status = str(raw.get("status") or ("ok" if pet else "unknown"))
            variant_text, variant_icon = self._pet_data_variant(pet, raw)
            base_id = pet.get("base_conf_id") or raw.get("pet_cfg_id") or pet.get("catch_base_id") or pet.get("conf_id")
            wiki_pet_id = self._pet_data_wiki_pet_id(base_id)
            size_info = self._pet_data_weight_size_info(pet, (size_lookup or {}).get(wiki_pet_id))
            name = pet.get("name") or pet.get("pet_default_name") or raw.get("pet_default_name") or raw.get("name") or f"精灵 {base_id or index + 1}"
            default_name = pet.get("pet_default_name") or raw.get("pet_default_name") or ""
            display_default = default_name if default_name and default_name != name else ""
            pet_gid = pet.get("gid") or raw.get("pet_gid")
            furniture_guid = raw.get("furniture_guid") or (pet.get("scene_info") or {}).get("npc_id") if isinstance(pet.get("scene_info"), dict) else raw.get("furniture_guid")
            pets.append({
                "index": index + 1,
                "status": status,
                "statusText": "成功" if status == "ok" else status,
                "retCode": self._pet_data_display(npc_pet.get("ret_code")),
                "name": str(name),
                "defaultName": str(display_default),
                "level": self._pet_data_display(pet.get("level")),
                "baseId": self._pet_data_display(base_id),
                "confId": self._pet_data_display(pet.get("conf_id")),
                "petGid": self._pet_data_display(pet_gid),
                "npcId": self._pet_data_display(furniture_guid),
                "npcIdSource": self._pet_data_display(raw.get("npc_id_source")),
                "imageUrl": self._pet_data_image_url(base_id, "image"),
                "iconUrl": self._pet_data_image_url(base_id, "icon"),
                "variantText": variant_text,
                "variantIcon": variant_icon,
                "voiceText": self._pet_data_voice_text(pet.get("voice")),
                "cards": self._pet_data_card_items(pet, option_maps, variant_text, size_info),
                "attributes": self._pet_data_attributes(pet),
                "skills": self._pet_data_skills(pet, skill_lookup, load_skill_icons=load_skill_icons),
                "catchItems": [
                    {"label": "捕捉等级", "value": self._pet_data_display(pet.get("catch_lv"))},
                    {"label": "捕捉方式", "value": self._pet_data_display(pet.get("catch_way"))},
                    {"label": "捕捉营地", "value": self._pet_data_display(pet.get("caught_camp"))},
                    {"label": "获得时间", "value": self._pet_data_time_text(pet.get("add_time"))},
                ],
                "specialityIds": " / ".join(str(x) for x in (pet.get("real_speciality_ids") or [])) or self._pet_data_display(pet.get("speciality_id")),
                "relationshipType": self._pet_data_display(npc_pet.get("relationship_type")),
                "errorText": self._pet_data_display(raw.get("error") or raw.get("message") or npc_pet.get("error_message"), ""),
            })
        return pets

    def _build_pet_data_render_data(
        self,
        res: Dict[str, Any] | None,
        uid: str,
        options: Dict[str, Any] | None = None,
        skill_lookup: Dict[str, Dict[str, Any]] | None = None,
        size_lookup: Dict[str, Dict[str, Any]] | None = None,
        single_query: bool = False,
        low_bandwidth_mode: bool = False,
    ) -> Dict[str, Any]:
        payload = res or {}
        if isinstance(payload.get("result"), dict):
            payload = payload.get("result") or payload
        option_maps = self._pet_data_option_maps(options)
        player_info = payload.get("player_info") if isinstance(payload.get("player_info"), dict) else {}
        if not player_info and isinstance(payload.get("npc_pet"), dict):
            player_info = payload["npc_pet"].get("player_info") if isinstance(payload["npc_pet"].get("player_info"), dict) else {}
        pets = self._pet_data_extract_items(
            payload,
            option_maps,
            skill_lookup,
            size_lookup,
            load_skill_icons=not low_bandwidth_mode,
        )
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        finished_at = self._normalize_epoch_seconds(meta.get("finished_at") or meta.get("created_at"))
        updated_at = datetime.fromtimestamp(finished_at, tz=self._cn_tz()).strftime("%Y-%m-%d %H:%M:%S") if finished_at else datetime.now(self._cn_tz()).strftime("%Y-%m-%d %H:%M:%S")
        online = player_info.get("online")
        online_text = "在线" if online is True else ("离线" if online is False else "未知")
        ok_count = payload.get("npc_pet_ok_count")
        error_count = payload.get("npc_pet_error_count")
        skipped_count = payload.get("npc_pet_skipped_count")
        return {
            "title": "家园详情",
            "subtitle": "Ingame Pet Data",
            "uid": self._pet_data_display(payload.get("uin") or player_info.get("uin") or uid),
            "playerName": self._pet_data_display(player_info.get("name"), "未知玩家"),
            "playerLevel": self._pet_data_display(player_info.get("level")),
            "worldLevel": self._pet_data_display(player_info.get("world_level")),
            "onlineText": online_text,
            "isOnline": online is True,
            "queryMode": "单只精灵" if single_query else "家园批量",
            "lowBandwidthMode": low_bandwidth_mode,
            "summaryCards": [
                {"label": "目标状态", "value": online_text},
                {"label": "返回精灵", "value": str(len(pets))},
                {"label": "成功/失败", "value": f"{self._pet_data_display(ok_count, str(len(pets)))} / {self._pet_data_display(error_count, '0')}"},
                {"label": "跳过", "value": self._pet_data_display(skipped_count, "0")},
            ],
            "pets": pets,
            "updatedAt": updated_at,
            "notice": "该接口依赖目标玩家在线且家园可访问；离线或隐私/上游不可达时可能失败。",
            "emptyText": "未获取到家园精灵完整数据。请确认目标玩家在线、家园可访问，或稍后重试。",
            "commandHint": "💡 /家园详情 <UID> | /家园详情 <UID> <pet_gid> <npc_id>",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    async def _home_subscription_loop(self):
        logger.info("[Rocom] 家园订阅循环任务已启动")
        interval = max(1, int(self.home_subscription_interval_minutes or 5)) * 60
        while True:
            try:
                await asyncio.sleep(interval)
                await self._check_home_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Rocom] 家园订阅循环异常: {e}")
                await asyncio.sleep(60)

    def _home_subscription_state(
        self, data: Dict[str, Any], kind: str
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, List[str]]:
        if kind == "garden":
            items = list(data.get("gardenPlots") or [])
            ready_items = [item for item in items if item.get("ready")]
            unit = "成熟"
            names = [f"田地{item.get('landIndex')} {item.get('plantName')}" for item in ready_items]
            return items, ready_items, unit, names

        if kind == "egg":
            items = [
                item
                for item in list(data.get("indoorPets") or []) + list(data.get("guardPets") or [])
                if item.get("eggTime")
            ]
            ready_items = [item for item in items if item.get("eggReady")]
            unit = "生蛋"
            names = [item.get("name", "未知精灵") for item in ready_items]
            return items, ready_items, unit, names

        items = [
            item
            for item in list(data.get("indoorPets") or []) + list(data.get("guardPets") or [])
            if item.get("readyAt")
        ]
        ready_items = [item for item in items if item.get("inspireReady")]
        unit = "灵感完成"
        names = [item.get("name", "未知精灵") for item in ready_items]
        return items, ready_items, unit, names

    def _home_subscription_level_message(
        self,
        display_name: str,
        kind: str,
        level: str,
        total_count: int,
        ready_items: List[Dict[str, Any]],
        names: List[str],
    ) -> str:
        text_map = {
            "garden": ("菜园作物", "成熟"),
            "inspiration": ("精灵灵感", "完成"),
            "egg": ("精灵生蛋", "可领取"),
        }
        kind_text, action_text = text_map.get(kind, ("家园项目", "完成"))
        level_text = "首个" if level == "first" else "全部"
        title = f"家园{kind_text}{level_text}{action_text}提醒"
        lines = [
            f"{title}：{display_name}",
            f"进度：{len(ready_items)}/{total_count}",
        ]
        if names:
            lines.append("已完成：" + "、".join(names[:8]))
        return "\n".join(lines)

    async def _home_subscription_targets(self, uid: str, data: Dict[str, Any]) -> tuple[str, List[Dict[str, str]]]:
        display_name = str((data or {}).get("homeName") or uid)
        mentions = []
        try:
            all_bindings = await self.user_mgr.get_all_users_bindings()
        except Exception as e:
            logger.warning(f"[Rocom] 读取家园订阅绑定用户失败: {e}")
            return display_name, mentions

        seen_users = set()
        for user_id, bindings in all_bindings.items():
            if str(user_id) in seen_users:
                continue
            for binding in bindings or []:
                if str(binding.get("role_id", "") or "") != str(uid):
                    continue
                nickname = str(binding.get("nickname") or display_name or uid)
                if nickname and display_name == str(uid):
                    display_name = nickname
                if str(user_id).isdigit():
                    mentions.append({"qq": str(user_id), "name": nickname})
                    seen_users.add(str(user_id))
                break
        return display_name, mentions

    async def _check_home_subscriptions(self):
        all_subs = await self.home_sub_mgr.get_all_subscriptions()
        if not all_subs:
            return
        data_cache: Dict[str, Dict[str, Any] | None] = {}
        for key, sub in all_subs.items():
            uid = str(sub.get("uid", "") or "")
            kind = str(sub.get("kind", "") or "")
            if not uid or kind not in {"garden", "inspiration", "egg"}:
                continue
            if uid not in data_cache:
                data_cache[uid] = await self.client.ingame_home_info(uid)
                await asyncio.sleep(1)
            res = data_cache.get(uid)
            if not res:
                continue
            data = self._build_home_render_data(res, uid)
            total_items, ready_items, _unit, names = self._home_subscription_state(data, kind)
            total_count = len(total_items)
            ready_count = len(ready_items)
            if total_count <= 0:
                continue

            notify_state = sub.get("notify_state") if isinstance(sub.get("notify_state"), dict) else {}
            changed = False
            push_levels = []

            if ready_count <= 0:
                if notify_state.get("first") or notify_state.get("all"):
                    notify_state["first"] = False
                    notify_state["all"] = False
                    changed = True
            else:
                if not notify_state.get("first"):
                    push_levels.append("first")
                if ready_count >= total_count and not notify_state.get("all"):
                    push_levels.append("all")
                elif ready_count < total_count and notify_state.get("all"):
                    notify_state["all"] = False
                    changed = True

            if not push_levels:
                if changed:
                    sub["notify_state"] = notify_state
                    await self.home_sub_mgr.upsert_subscription(key, sub)
                continue

            display_name, mentions = await self._home_subscription_targets(uid, data)
            messages = [
                self._home_subscription_level_message(display_name, kind, level, total_count, ready_items, names)
                for level in push_levels
            ]
            try:
                chain = MessageChain()
                for mention in mentions:
                    chain.at(mention.get("name") or display_name, mention.get("qq"))
                if mentions:
                    chain.message("\n")
                chain.message("\n\n".join(messages))
                await self.context.send_message(sub["umo"], chain)
            except Exception as e:
                logger.warning(f"[Rocom] 家园订阅推送失败: {e}")
                continue
            for level in push_levels:
                notify_state[level] = True
            sub["notify_state"] = notify_state
            sub["last_push_time"] = int(time.time())
            await self.home_sub_mgr.upsert_subscription(key, sub)
            await asyncio.sleep(2)

    def _announcement_id(self, item: Dict[str, Any] | None) -> str:
        item = item or {}
        return str(item.get("thread_id") or item.get("id") or "").strip()

    def _announcement_ts(self, item: Dict[str, Any] | None) -> int:
        item = item or {}
        for key in ("published_at_ts", "publish_at_ts", "created_at_ts"):
            try:
                value = int(item.get(key) or 0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        for key in ("publishAt", "published_at", "createdAt"):
            text = str(item.get(key) or "").strip()
            if not text:
                continue
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    return int(datetime.strptime(text, fmt).timestamp())
                except ValueError:
                    continue
        return 0

    def _announcement_images(self, item: Dict[str, Any] | None) -> List[str]:
        images = []
        content = (item or {}).get("content") if isinstance((item or {}).get("content"), dict) else {}
        for index in content.get("indexes") or []:
            if not isinstance(index, dict):
                continue
            for field in ("imageUrl", "imagePreviewUrl"):
                value = index.get(field)
                if isinstance(value, list):
                    images.extend([str(url) for url in value if url])
                elif value:
                    images.append(str(value))
        cover = (item or {}).get("cover")
        if cover:
            images.insert(0, str(cover))
        seen = set()
        result = []
        for url in images:
            if url in seen:
                continue
            seen.add(url)
            result.append(url)
        return result

    def _build_announcement_list_render_data(self, res: Dict[str, Any] | None) -> Dict[str, Any]:
        items = (res or {}).get("list") or (res or {}).get("items") or []
        cards = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            cards.append(
                {
                    "index": index,
                    "id": self._announcement_id(item),
                    "title": item.get("title", "未命名公告"),
                    "summary": item.get("summary") or "",
                    "cover": item.get("cover") or "",
                    "time": item.get("publishAt") or item.get("published_at") or item.get("createdAt") or "",
                    "author": ((item.get("author") or {}).get("nickname") if isinstance(item.get("author"), dict) else "") or "洛克王国：世界",
                    "isStick": bool(item.get("isStick")),
                }
            )
        page = (res or {}).get("page", 1)
        total_text = (res or {}).get("total") or (res or {}).get("count") or "未知"
        return {
            "title": "洛克王国公告",
            "subtitle": f"第 {page} 页 · 本页 {len(cards)} 条",
            "cards": cards,
            "listHeader": "洛克王国公告",
            "listSubtitle": f"共 {total_text} 条公告，本页显示 {len(cards)} 条",
            "list": [
                {
                    "index": item["index"],
                    "id": item["id"],
                    "title": item["title"],
                    "timeStr": item["time"],
                    "coverUrl": item["cover"],
                    "summary": item["summary"],
                    "author": item["author"],
                    "isStick": item["isStick"],
                }
                for item in cards
            ],
            "has_more": bool((res or {}).get("has_more")),
            "next_page": (res or {}).get("next_page"),
            "commandHint": "💡 /洛克公告 <页码> | /洛克公告详情 <公告ID> | /洛克公告最新",
            "footerLine1": "由 AstrBot & WeGame Locke Kingdom Plugin 渲染",
            "pageWidth": 680,
        }

    def _build_announcement_detail_render_data(self, item: Dict[str, Any] | None) -> Dict[str, Any]:
        item = item or {}
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        caption_html = content.get("text") or item.get("summary") or "该公告暂无正文。"
        return {
            "title": item.get("title", "洛克王国公告"),
            "summary": item.get("summary") or "",
            "cover": item.get("cover") or "",
            "coverUrl": item.get("cover") or "",
            "time": item.get("publishAt") or item.get("published_at") or item.get("createdAt") or "",
            "timeLabel": "发布时间：",
            "timeStr": item.get("publishAt") or item.get("published_at") or item.get("createdAt") or "",
            "author": ((item.get("author") or {}).get("nickname") if isinstance(item.get("author"), dict) else "") or "洛克王国：世界",
            "content_html": content.get("text") or "",
            "captionHtml": caption_html,
            "images": self._announcement_images(item),
            "stats": [
                {"label": "浏览", "value": item.get("viewCount", 0)},
                {"label": "收藏", "value": item.get("collectCount", 0)},
                {"label": "分享", "value": item.get("shareCount", 0)},
            ],
            "commandHint": "💡 /订阅洛克公告 可订阅新公告推送",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
            "pageWidth": 760,
        }

    def _activity_ts(self, value: Any, fallback_date: str = "", end_of_day: bool = False) -> int:
        try:
            raw = int(float(value))
            if raw > 10_000_000_000:
                raw = raw // 1000
            if raw > 0:
                return raw
        except (TypeError, ValueError):
            pass

        text = str(value or fallback_date or "").strip()
        if not text:
            return 0
        formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
        )
        for fmt in formats:
            try:
                dt = datetime.strptime(text, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=self._cn_tz())
                if fmt == "%Y-%m-%d" and end_of_day:
                    dt = dt.replace(hour=23, minute=59, second=59)
                return int(dt.timestamp())
            except ValueError:
                continue
        return 0

    def _activity_time_text(self, ts: int, with_time: bool = False) -> str:
        if not ts:
            return "--"
        fmt = "%m.%d %H:%M" if with_time else "%m.%d"
        return datetime.fromtimestamp(ts, tz=self._cn_tz()).strftime(fmt)

    def _activity_rewards_text(self, act: Dict[str, Any]) -> str:
        names: List[str] = []
        for key in ("get_props", "get_extra_props", "get_pets"):
            value = act.get(key)
            if not isinstance(value, list):
                continue
            for item in value[:4]:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("goods_name") or item.get("pet_name") or item.get("title")
                    if name:
                        names.append(str(name))
                elif item:
                    names.append(str(item))
        return "、".join(names[:6]) if names else "暂无奖励信息"

    def _extract_activity_items(self, res: Dict[str, Any] | None) -> List[Dict[str, Any]]:
        payload = res or {}
        source = []
        for key in ("activityCalendar", "calendar", "otherActivities", "activities", "list", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                source = value
                break
        if not source and isinstance(payload.get("data"), dict):
            return self._extract_activity_items(payload.get("data"))

        now_ts = int(time.time())
        result = []
        for act in source:
            if not isinstance(act, dict) or act.get("is_deleted"):
                continue
            start_ts = self._activity_ts(
                act.get("start_time")
                or act.get("startAt")
                or act.get("start_at")
                or act.get("start_ts"),
                act.get("start_date") or "",
            )
            end_ts = self._activity_ts(
                act.get("end_time")
                or act.get("endAt")
                or act.get("end_at")
                or act.get("end_ts"),
                act.get("end_date") or "",
                end_of_day=True,
            )
            is_unlimited = bool(act.get("is_unlimited"))
            if not start_ts and not end_ts and not is_unlimited:
                continue
            if is_unlimited and not end_ts:
                end_ts = start_ts + 365 * 86400 if start_ts else now_ts + 365 * 86400
            if not start_ts:
                start_ts = now_ts
            if not end_ts or end_ts <= start_ts:
                end_ts = start_ts + 86400

            if now_ts < start_ts:
                status_text = "未开始"
                status_class = "upcoming"
            elif now_ts > end_ts and not is_unlimited:
                status_text = "已结束"
                status_class = "ended"
            else:
                status_text = "进行中" if not is_unlimited else "常驻"
                status_class = "active" if not is_unlimited else "permanent"

            result.append(
                {
                    "name": str(act.get("name") or act.get("title") or "未命名活动"),
                    "desc": str(act.get("description") or act.get("desc") or "活动"),
                    "cover": str(act.get("cover_url") or act.get("cover") or act.get("pic") or ""),
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "start": self._activity_time_text(start_ts, with_time=True),
                    "end": self._activity_time_text(end_ts, with_time=True),
                    "statusText": status_text,
                    "statusClass": status_class,
                    "is_perm": is_unlimited or (end_ts - start_ts >= 300 * 86400),
                    "rewards": self._activity_rewards_text(act),
                    "sort": int(act.get("sort") or 999),
                }
            )
        return sorted(result, key=lambda x: (x["is_perm"], x["start_ts"], x["sort"]))

    def _build_activity_calendar_render_data(self, res: Dict[str, Any] | None) -> Dict[str, Any]:
        items = self._extract_activity_items(res)
        now = datetime.now(self._cn_tz())
        now_ts = int(now.timestamp())
        today_midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=self._cn_tz())
        min_ts = int(today_midnight.timestamp()) - 10 * 86400
        max_ts = int(today_midnight.timestamp()) + 50 * 86400
        total_duration = max(max_ts - min_ts, 1)

        normal_items = []
        permanent_items = []
        key_dates = set()
        for item in items:
            left_pct = (item["start_ts"] - min_ts) / total_duration * 100
            right_pct = (item["end_ts"] - min_ts) / total_duration * 100
            if item["is_perm"]:
                right_pct = 100
            left_pct = max(0, min(100, left_pct))
            right_pct = max(0, min(100, right_pct))
            width_pct = max(12.5, right_pct - left_pct)
            if left_pct + width_pct > 100:
                left_pct = max(0, 100 - width_pct)
            item["left_pct"] = round(left_pct, 3)
            item["width_pct"] = round(width_pct, 3)
            item["hide_start"] = item["start_ts"] < min_ts
            if item["is_perm"]:
                permanent_items.append(item)
            else:
                normal_items.append(item)
                if min_ts <= item["start_ts"] <= max_ts:
                    key_dates.add(item["start_ts"])

        def pack_lanes(source: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
            lanes: List[List[Dict[str, Any]]] = []
            for item in source:
                placed = False
                for lane in lanes:
                    if item["start_ts"] >= lane[-1]["end_ts"] + 86400:
                        lane.append(item)
                        placed = True
                        break
                if not placed:
                    lanes.append([item])
            return lanes

        lanes = pack_lanes(normal_items) + pack_lanes(permanent_items)
        axis_dates = []
        last_ts = 0
        for ts in sorted(key_dates):
            if ts - last_ts < 4 * 86400:
                continue
            last_ts = ts
            axis_dates.append(
                {
                    "label": self._activity_time_text(ts),
                    "left_pct": round((ts - min_ts) / total_duration * 100, 3),
                }
            )

        now_pct = (now_ts - min_ts) / total_duration * 100
        now_line = (
            {"label": "TODAY", "left_pct": round(now_pct, 3)}
            if 0 <= now_pct <= 100
            else None
        )

        return {
            "title": "洛克活动日历",
            "subtitle": f"显示 {now.strftime('%m.%d')} 前 10 天至后 50 天活动",
            "lanes": lanes,
            "axis_dates": axis_dates,
            "now_line": now_line,
            "empty": not bool(items),
            "commandHint": "💡 /洛克活动日历",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    async def _announcement_subscription_loop(self):
        logger.info("[Rocom] 公告订阅循环任务已启动")
        interval = max(1, int(self.announcement_poll_interval_minutes or 10)) * 60
        while True:
            try:
                await asyncio.sleep(interval)
                await self._check_announcement_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Rocom] 公告订阅循环异常: {e}")
                await asyncio.sleep(60)

    async def _check_announcement_subscriptions(self):
        all_subs = await self.announcement_sub_mgr.get_all_subscriptions()
        if not all_subs:
            return
        latest = await self.client.get_announcement_latest()
        if not latest:
            return
        latest_id = self._announcement_id(latest)
        latest_ts = self._announcement_ts(latest)
        if not latest_id:
            return
        detail = None
        img_url = None
        for key, sub in all_subs.items():
            last_id = str(sub.get("last_id") or "")
            last_ts = int(sub.get("since_ts") or 0)
            if latest_id == last_id:
                continue
            if latest_ts and last_ts and latest_ts <= last_ts:
                continue
            if detail is None:
                detail = await self.client.get_announcement_detail(latest_id) or latest
                img_url = await self.renderer.render_html(
                    "render/announcement/detail.html",
                    self._build_announcement_detail_render_data(detail),
                    {"device_scale_factor": 1.5, "viewport_width": 1100, "viewport_height": 1200},
                )
            chain = MessageChain().message(
                f"【洛克王国新公告】\n{latest.get('title', '未命名公告')}\n"
            )
            if img_url:
                chain.file_image(img_url)
            elif latest.get("summary"):
                chain.message(str(latest.get("summary")))
            try:
                await self.context.send_message(sub["umo"], chain)
            except Exception as e:
                logger.warning(f"[Rocom] 公告订阅推送失败: {e}")
                continue
            sub["last_id"] = latest_id
            sub["since_ts"] = latest_ts or int(time.time())
            sub["updated_at"] = int(time.time())
            await self.announcement_sub_mgr.upsert_subscription(key, sub)
            await asyncio.sleep(2)

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
        logger.info(f"[Rocom] 远行商人订阅循环任务已启动（instance={self._instance_id}）")
        while True:
            try:
                now = datetime.now(self._cn_tz())
                next_check = self._next_merchant_check_time(now)
                jitter = random.uniform(-self._merchant_jitter_seconds, self._merchant_jitter_seconds)
                target_check = next_check + timedelta(seconds=jitter)
                sleep_seconds = max(1, (target_check - now).total_seconds())
                logger.info(
                    f"[Rocom] 下次远行商人订阅检查时间：{target_check.strftime('%Y-%m-%d %H:%M:%S CST')}（基准 {next_check.strftime('%H:%M:%S')}，随机偏移 {jitter:.1f}s，instance={self._instance_id}）"
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


    def _merchant_payload(self, res: Dict[str, Any] | None) -> Dict[str, Any]:
        payload = res or {}
        if isinstance(payload.get("data"), dict):
            payload = payload.get("data") or {}
        return payload if isinstance(payload, dict) else {}

    def _merchant_timestamp_ms(self, value: Any) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _merchant_product_from_item(
        self,
        item: Dict[str, Any],
        fallback_icon: str,
        activity: Dict[str, Any],
        category: str,
        now_ms: int,
        goods_meta: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        goods_meta = goods_meta or {}
        start_ms = self._merchant_timestamp_ms(item.get("start_time"))
        end_ms = self._merchant_timestamp_ms(item.get("end_time"))
        if start_ms is None:
            start_ms = self._merchant_timestamp_ms(activity.get("start_time"))
        if end_ms is None:
            end_ms = self._merchant_timestamp_ms(activity.get("end_time"))
        is_active = True
        if start_ms is not None and end_ms is not None:
            is_active = start_ms <= now_ms < end_ms
        status_label = "当前轮次"
        if start_ms is not None and now_ms < start_ms:
            status_label = "未开始"
        elif end_ms is not None and now_ms >= end_ms:
            status_label = "已结束"
        return {
            "name": item.get("name", "未知商品"),
            "image": item.get("icon_url") or item.get("iconUrl") or fallback_icon,
            "time_label": self._format_merchant_window({"start_time": start_ms, "end_time": end_ms}),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "is_active": is_active,
            "status_label": status_label,
            "category": category,
            "price": item.get("price") if item.get("price") not in (None, "") else goods_meta.get("price"),
            "buy_limit_num": (
                item.get("buy_limit_num")
                if item.get("buy_limit_num") not in (None, "")
                else goods_meta.get("buy_limit_num")
            ),
        }

    def _merchant_history_groups(
        self,
        products: List[Dict[str, Any]],
        now_ms: int,
    ) -> List[Dict[str, Any]]:
        today = datetime.fromtimestamp(now_ms / 1000, tz=self._cn_tz()).strftime("%Y-%m-%d")
        grouped: Dict[str, Dict[str, Any]] = {}
        for product in products:
            if product.get("is_active"):
                continue
            start_ms = self._merchant_timestamp_ms(product.get("start_ms"))
            if start_ms is None:
                continue
            start_dt = datetime.fromtimestamp(start_ms / 1000, tz=self._cn_tz())
            if start_dt.strftime("%Y-%m-%d") != today:
                continue
            key = f"{start_ms}-{product.get('end_ms') or ''}"
            group = grouped.setdefault(
                key,
                {
                    "time_label": product.get("time_label") or "--",
                    "status_label": product.get("status_label") or "其他时段",
                    "sort": start_ms,
                    "products": [],
                },
            )
            names = {item.get("name") for item in group["products"]}
            if product.get("name") not in names and len(group["products"]) < 5:
                group["products"].append(product)
        return [
            {k: v for k, v in group.items() if k != "sort"}
            for group in sorted(grouped.values(), key=lambda item: item["sort"])
            if group.get("products")
        ]

    def _merchant_products_from_response(self, res: Dict[str, Any] | None):
        payload = self._merchant_payload(res)
        activities = payload.get("merchantActivities")
        if activities is None:
            activities = payload.get("merchant_activities")
        activities = activities or []
        activity = activities[0] if activities else {}
        buckets = [
            ("道具", activity.get("get_props") or []),
            ("额外道具", activity.get("get_extra_props") or []),
            ("精灵", activity.get("get_pets") or []),
        ]
        products = []
        all_products = []
        fallback_icon = "{{_res_path}}img/logo.cVSpb3sL.png"
        now_ms = int(datetime.now(self._cn_tz()).timestamp() * 1000)
        random_goods = payload.get("random_goods") if isinstance(payload.get("random_goods"), list) else []
        goods_meta_by_name = {
            str(item.get("goods_name", "") or item.get("name", "")).strip(): item
            for item in random_goods
            if isinstance(item, dict) and str(item.get("goods_name", "") or item.get("name", "")).strip()
        }

        for category, items in buckets:
            for item in items:
                if not isinstance(item, dict):
                    continue
                goods_meta = goods_meta_by_name.get(str(item.get("name", "") or "").strip(), {})
                product = self._merchant_product_from_item(
                    item, fallback_icon, activity, category, now_ms, goods_meta=goods_meta
                )
                all_products.append(product)
                if product.get("is_active"):
                    products.append(product)
        return activity, products, self._merchant_history_groups(all_products, now_ms)


    async def _render_merchant_image(self, refresh: bool = False):
        res = await self.client.get_merchant_info(refresh=refresh)
        activity, products, history_groups = self._merchant_products_from_response(res)
        round_info = self._current_merchant_round()
        return await self._render_merchant_image_from_data(activity, products, round_info, history_groups), res, products, round_info

    async def _render_merchant_image_from_data(
        self,
        activity: Dict[str, Any] | None,
        products: List[Dict[str, Any]] | None,
        round_info: Dict[str, Any] | None,
        history_groups: List[Dict[str, Any]] | None = None,
    ):
        data = {
            "background": "{{_res_path}}img/bg.C8CUoi7I.jpg",
            "titleIcon": True,
            "title": (activity or {}).get("name", "远行商人"),
            "subtitle": (activity or {}).get("start_date", "每日 08:00 / 12:00 / 16:00 / 20:00 刷新"),
            "product_count": len(products or []),
            "round_info": round_info or self._current_merchant_round(),
            "products": products or [],
            "history_groups": history_groups or [],
        }
        img_url = await self.renderer.render_html(
            "render/yuanxing-shangren/index.html",
            data,
            {
                "device_scale_factor": 2,
                "viewport_width": 1200,
                "viewport_height": 1000,
            },
        )
        return img_url

    async def _run_merchant_subscription_window(self):
        for retry_index in range(self._merchant_retry_times + 1):
            if retry_index > 0:
                delay = max(
                    1,
                    self._merchant_retry_delay_seconds
                    + random.uniform(-self._merchant_jitter_seconds, self._merchant_jitter_seconds),
                )
                logger.warning(
                    f"[Rocom] 远行商人返回为空，{delay:.1f} 秒后进行第 {retry_index} 次重试"
                )
                await asyncio.sleep(delay)
            status = await self._check_merchant_subscriptions()
            if status != "empty":
                return
            if retry_index >= self._merchant_retry_times:
                logger.warning("[Rocom] 远行商人订阅检查连续为空，已暂停本轮重试")
                return

    async def _check_merchant_subscriptions(self) -> str:
        all_subs = await self.merchant_sub_mgr.get_all_subscriptions()
        if not all_subs:
            return "no_subscriptions"
        try:
            res = await self.client.get_merchant_info(refresh=True)
            activity, products, history_groups = self._merchant_products_from_response(res)
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
            img_url = await self._render_merchant_image_from_data(activity, products, round_info, history_groups)
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

    def _normalize_query_text(self, text: str) -> str:
        return re.sub(r"\s+", "", str(text or "")).strip().lower()

    def _wiki_text(self, value: Any, default: str = "") -> str:
        if value in (None, ""):
            return default
        return str(value)

    def _wiki_named_value(self, value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("name") or value.get("label") or value.get("value") or "")
        return str(value or "")

    def _wiki_names(self, values: Any) -> List[str]:
        if not values:
            return []
        if not isinstance(values, list):
            values = [values]
        result = []
        for value in values:
            text = self._wiki_named_value(value).strip()
            if text:
                result.append(text)
        return result

    def _find_exact_wiki_match(self, results: List[Dict[str, Any]], query: str) -> Dict[str, Any] | None:
        normalized_query = self._normalize_query_text(query)
        if not normalized_query:
            return None
        for item in results:
            name = str(item.get("name") or "")
            form = str(item.get("form") or "")
            candidates = [
                self._normalize_query_text(name),
                self._normalize_query_text(f"{name}{form}"),
                self._normalize_query_text(f"{name} {form}"),
                self._normalize_query_text(f"{form}{name}"),
            ]
            if normalized_query in candidates:
                return item
        return None

    def _wiki_candidate_text(self, query: str, items: List[Dict[str, Any]], kind: str) -> str:
        lines = [f"找到多个{kind}候选，请使用更精确名称："]
        for idx, item in enumerate(items[:10], 1):
            name = item.get("name") or "未知"
            form = item.get("form") or ""
            item_id = item.get("pet_id") or item.get("skill_id") or item.get("id") or "-"
            suffix = f"（{form}）" if form and form != "普通" else ""
            lines.append(f"{idx}. {name}{suffix} #{item_id}")
        lines.append(f"\n你查询的是：{query}")
        return "\n".join(lines)

    def _wiki_range_label(self, data: Dict[str, Any] | None, key_min: str, key_max: str, unit: str) -> str:
        if not isinstance(data, dict):
            return "暂无"
        low = data.get(key_min)
        high = data.get(key_max)
        if low in (None, "") and high in (None, ""):
            return "暂无"
        if low in (None, ""):
            return f"{high}{unit}"
        if high in (None, "") or low == high:
            return f"{low}{unit}"
        return f"{low}-{high}{unit}"

    def _wiki_body_size(self, *sources: Dict[str, Any]) -> Dict[str, str]:
        body_size = {}
        for source in sources:
            if isinstance(source, dict) and isinstance(source.get("body_size"), dict):
                body_size = source.get("body_size") or {}
                break
        height = body_size.get("height") if isinstance(body_size, dict) else {}
        weight = body_size.get("weight") if isinstance(body_size, dict) else {}
        return {
            "height": self._wiki_range_label(height, "min_m", "max_m", "m"),
            "weight": self._wiki_range_label(weight, "min_kg", "max_kg", "kg"),
        }

    def _wiki_gender_label(self, value: Any) -> str:
        if isinstance(value, dict):
            male = value.get("male_percent")
            female = value.get("female_percent")
            parts = []
            if male not in (None, ""):
                parts.append(f"雄 {male}%")
            if female not in (None, ""):
                parts.append(f"雌 {female}%")
            return " / ".join(parts) if parts else "暂无"
        return str(value or "暂无")

    def _wiki_ecology_label(self, value: Any, fallback: Any = "") -> str:
        if isinstance(value, dict):
            parts = []
            for key in ("pet_text", "pet_style"):
                text = str(value.get(key) or "").strip()
                if text:
                    parts.append(text)
            habitats = self._wiki_names(value.get("habitats"))
            if habitats:
                parts.append("栖息：" + " / ".join(habitats))
            return "；".join(parts)
        text = str(value or fallback or "").strip()
        return text

    def _wiki_source_label(self, source: Any, fallback: Any = "") -> str:
        mapping = {
            "level": "升级",
            "machine": "技能石",
            "blood": "血脉",
            "field": "特殊来源",
        }
        text = str(source or "").strip()
        return str(fallback or mapping.get(text, text) or "")

    def _wiki_type_effectiveness(self, profile: Dict[str, Any]) -> List[Dict[str, str]]:
        data = profile.get("type_effectiveness") if isinstance(profile, dict) else {}
        if not isinstance(data, dict):
            return []
        rows = []
        definitions = [
            ("攻击克制", ("attack", "strong")),
            ("攻击弱效", ("attack", "weak")),
            ("防御弱点", ("defense", "weak")),
            ("防御抵抗", ("defense", "resist")),
        ]
        for label, (group_key, item_key) in definitions:
            group = data.get(group_key) if isinstance(data.get(group_key), dict) else {}
            names = self._wiki_names(group.get(item_key) if isinstance(group, dict) else [])
            if names:
                rows.append({"label": label, "value": " / ".join(names)})
        return rows

    def _build_pet_stats(self, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        attrs = profile.get("attributes") if isinstance(profile, dict) else {}
        attrs = attrs if isinstance(attrs, dict) else {}
        defs = [
            ("精力", "hp", "#42b883"),
            ("物攻", "physical_attack", "#e86452"),
            ("魔攻", "magic_attack", "#5987f5"),
            ("物防", "physical_defense", "#d69a32"),
            ("魔防", "magic_defense", "#25a6a6"),
            ("速度", "speed", "#8d62d9"),
        ]
        return [
            {
                "label": label,
                "value": int(attrs.get(key) or 0),
                "color": color,
                "percent": min(int(attrs.get(key) or 0) / 160 * 100, 100),
            }
            for label, key, color in defs
        ]

    def _build_pet_skill_groups(self, skills_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        groups = []
        source_defs = [
            ("level", "升级习得"),
            ("machine", "技能机"),
            ("blood", "血脉技能"),
            ("field", "场景习得"),
        ]
        for key, label in source_defs:
            raw_items = []
            if isinstance(skills_data, dict):
                value = skills_data.get(key)
                if isinstance(value, list):
                    raw_items = value
            items = []
            for skill in raw_items[:10]:
                items.append(
                    {
                        "name": skill.get("name") or "未知技能",
                        "icon": skill.get("icon") or "",
                        "level": skill.get("level") or skill.get("source_label") or "",
                        "type": self._wiki_named_value(skill.get("element_type") or skill.get("type")) or "未知",
                        "category": self._wiki_named_value(skill.get("skill_type")) or self._wiki_named_value(skill.get("damage_type")) or "未知",
                        "cost": skill.get("cost") if skill.get("cost") not in (None, "") else "?",
                        "power": skill.get("power") if skill.get("power") not in (None, "") else "?",
                        "desc": skill.get("desc") or skill.get("description") or skill.get("flavor_text") or "暂无说明",
                    }
                )
            if items:
                groups.append({"label": label, "items": items})
        return groups

    def _build_pet_family(self, family_data: Dict[str, Any], current_id: Any) -> List[Dict[str, Any]]:
        items = []
        if isinstance(family_data, dict):
            if isinstance(family_data.get("members"), list):
                items.extend(family_data.get("members") or [])
            for form_group in family_data.get("forms") or []:
                for member in form_group.get("members") or []:
                    merged = dict(member)
                    if form_group.get("name") and not merged.get("form_group"):
                        merged["form_group"] = form_group.get("name")
                    items.append(merged)
            if not items:
                for key in ("items", "family", "evolutions"):
                    value = family_data.get(key)
                    if isinstance(value, list):
                        items.extend(value)
                        break
        elif isinstance(family_data, list):
            items = family_data
        result = []
        seen = set()
        for item in items[:12]:
            pet_id = item.get("pet_id") or item.get("id")
            key = str(pet_id or item.get("name") or "")
            if key and key in seen:
                continue
            seen.add(key)
            condition_texts = item.get("condition_texts") if isinstance(item.get("condition_texts"), list) else []
            result.append(
                {
                    "pet_id": pet_id or "-",
                    "name": item.get("name") or "未知精灵",
                    "form": item.get("form") or item.get("form_group") or "",
                    "icon": item.get("icon") or item.get("small_icon") or "",
                    "condition": item.get("condition_summary")
                    or " / ".join(str(x) for x in condition_texts if x)
                    or item.get("evolution_description")
                    or item.get("condition")
                    or item.get("evolve_condition")
                    or item.get("relation")
                    or "",
                    "is_current": str(pet_id or "") == str(current_id or ""),
                }
            )
        return result

    def _build_handbook_topics(self, handbook_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        topics = handbook_data.get("topics") if isinstance(handbook_data, dict) else []
        result = []
        for item in (topics or [])[:8]:
            rewards = []
            reward_items = item.get("rewards") or item.get("reward_items")
            reward = item.get("reward")
            if not reward_items and isinstance(reward, dict):
                reward_items = reward.get("items")
            for reward in reward_items or []:
                rewards.append(
                    {
                        "name": reward.get("name") or "奖励",
                        "count": reward.get("count") or "",
                        "icon": reward.get("icon") or "",
                    }
                )
            result.append(
                {
                    "name": item.get("name") or item.get("title") or f"课题 {item.get('topic_id') or ''}".strip(),
                    "desc": item.get("description") or item.get("desc") or "",
                    "rewards": rewards[:4],
                }
            )
        return result

    async def _resolve_wiki_pet(self, query: str) -> tuple[Dict[str, Any] | None, List[Dict[str, Any]], str]:
        query = str(query or "").strip()
        if not query:
            return None, [], "请输入精灵名称或 ID。用法：/洛克wiki 精灵 <精灵名或ID>"
        if query.isdigit():
            detail = await self.client.get_wiki_pet(query)
            if detail:
                return detail, [], ""
            return None, [], f"获取 Wiki 精灵详情失败：{self.client.get_last_error()}"
        search_res = await self.client.list_wiki_pets(q=query, page_no=1, page_size=10)
        items = (search_res or {}).get("items") or []
        if not items:
            suggestions = await self._wiki_suggest_catalog_items(await self._get_wiki_catalog_by_key("pets"), query, 10)
            if suggestions:
                return None, suggestions, ""
            return None, [], f"未找到「{query}」的 Wiki 精灵资料：{self.client.get_last_error('无匹配结果')}"
        selected = self._find_exact_wiki_match(items, query)
        if selected is None and len(items) == 1:
            selected = items[0]
        if selected is None:
            return None, items, ""
        detail = await self.client.get_wiki_pet(selected.get("pet_id"))
        if detail:
            return detail, [], ""
        return None, [], f"获取 Wiki 精灵详情失败：{self.client.get_last_error()}"

    async def _resolve_wiki_skill(self, query: str) -> tuple[Dict[str, Any] | None, List[Dict[str, Any]], str]:
        query = str(query or "").strip()
        if not query:
            return None, [], "请输入技能名称或 ID。用法：/洛克wiki 技能 <技能名或ID>"
        if query.isdigit():
            detail = await self.client.get_wiki_skill(query)
            if detail:
                return detail, [], ""
            return None, [], f"获取技能详情失败：{self.client.get_last_error()}"
        search_res = await self.client.list_wiki_skills(q=query, page_no=1, page_size=10)
        items = (search_res or {}).get("items") or []
        if not items:
            suggestions = await self._wiki_suggest_catalog_items(await self._get_wiki_catalog_by_key("skills"), query, 10)
            if suggestions:
                return None, suggestions, ""
            return None, [], f"未找到「{query}」的技能 Wiki 资料：{self.client.get_last_error('无匹配结果')}。用法：/洛克wiki 技能 <技能名或ID>"
        selected = self._find_exact_wiki_match(items, query)
        if selected is None and len(items) == 1:
            selected = items[0]
        if selected is None:
            return None, items, ""
        detail = await self.client.get_wiki_skill(selected.get("skill_id"))
        if detail:
            return detail, [], ""
        return None, [], f"获取技能详情失败：{self.client.get_last_error()}"

    async def _fetch_wiki_pet_sections(self, pet_id: Any) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        profile_res, skills_res, family_res, handbook_res = await asyncio.gather(
            self.client.get_wiki_pet_profile(pet_id),
            self.client.get_wiki_pet_skills(pet_id),
            self.client.get_wiki_pet_family(pet_id),
            self.client.get_wiki_pet_handbook(pet_id),
            return_exceptions=True,
        )
        return (
            profile_res if isinstance(profile_res, dict) else {},
            skills_res if isinstance(skills_res, dict) else {},
            family_res if isinstance(family_res, dict) else {},
            handbook_res if isinstance(handbook_res, dict) else {},
        )

    def _build_wiki_render_data(
        self,
        overview: Dict[str, Any],
        profile: Dict[str, Any] | None,
        skills: Dict[str, Any] | None,
        family: Dict[str, Any] | None,
        handbook: Dict[str, Any] | None,
        query: str,
    ) -> Dict[str, Any]:
        profile = profile or {}
        skills = skills or {}
        family = family or {}
        handbook = handbook or {}
        pet_id = overview.get("pet_id") or profile.get("pet_id")
        type_names = self._wiki_names(overview.get("type_names") or overview.get("types"))
        egg_groups = self._wiki_names(overview.get("egg_group_names") or overview.get("egg_groups"))
        feature = overview.get("feature") or {}
        body_size = self._wiki_body_size(profile, overview)
        stats = self._build_pet_stats(profile)
        total_stats = (profile.get("attributes") or {}).get("sum") or sum(item["value"] for item in stats)
        return {
            "name": overview.get("name") or profile.get("name") or query,
            "query": query,
            "pet_id": pet_id or "-",
            "number": overview.get("handbook_no") or profile.get("handbook_no") or "---",
            "form": overview.get("form") or profile.get("form") or "",
            "quality": overview.get("quality") or profile.get("quality") or "",
            "stage": overview.get("stage") or profile.get("stage") or "",
            "pet_icon": overview.get("icon") or overview.get("small_icon") or "{{_res_path}}img/roco_icon.png",
            "main_image": profile.get("small_icon") or overview.get("small_icon") or profile.get("icon") or overview.get("icon") or "{{_res_path}}img/roco_icon.png",
            "type_names": type_names,
            "egg_groups": egg_groups,
            "description": profile.get("description") or overview.get("description") or "暂无图鉴描述",
            "classis": self._wiki_named_value(overview.get("classis")) or "暂无",
            "feature_name": feature.get("name") or "暂无",
            "feature_desc": feature.get("desc") or "暂无特性说明",
            "ride_talent": "支持" if overview.get("has_ride_talent") else "不支持",
            "height_label": body_size["height"],
            "weight_label": body_size["weight"],
            "gender_ratio": self._wiki_gender_label(profile.get("gender_ratio")),
            "move_type": profile.get("move_type") or "暂无",
            "habitats": self._wiki_names(profile.get("habitats")),
            "ecology": self._wiki_ecology_label(profile.get("ecology"), profile.get("pet_style")),
            "type_effectiveness": self._wiki_type_effectiveness(profile),
            "total_stats": total_stats,
            "pet_stats": stats,
            "skill_groups": self._build_pet_skill_groups(skills),
            "family_members": self._build_pet_family(family, pet_id),
            "handbook_topics": self._build_handbook_topics(handbook),
            "areas": self._wiki_names(handbook.get("areas") if isinstance(handbook, dict) else []),
            "commandHint": "💡 /洛克wiki <类型> <关键词或ID> | 示例：/洛克wiki 技能 圣光斩",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _build_skill_render_data(
        self,
        item: Dict[str, Any],
        pets_data: Dict[str, Any] | None,
        query: str,
    ) -> Dict[str, Any]:
        pets = []
        for pet in ((pets_data or {}).get("items") or [])[:30]:
            pets.append(
                {
                    "name": pet.get("name") or "未知精灵",
                    "pet_id": pet.get("pet_id") or "-",
                    "icon": pet.get("icon") or pet.get("small_icon") or "",
                    "source": self._wiki_source_label(pet.get("source"), pet.get("source_label")),
                    "level": pet.get("level") or "",
                }
            )
        tags = []
        for tag in item.get("tags") or []:
            tags.append({"name": tag.get("name") or "", "icon": tag.get("icon") or ""})
        return {
            "name": item.get("name") or query,
            "skill_id": item.get("skill_id") or "-",
            "query": query,
            "icon": item.get("icon") or "",
            "type": self._wiki_named_value(item.get("type")) or "未知",
            "skill_type": self._wiki_named_value(item.get("skill_type")) or "未知",
            "damage_type": self._wiki_named_value(item.get("damage_type")) or "未知",
            "element_type": self._wiki_named_value(item.get("element_type")) or "未知",
            "cost": item.get("cost") if item.get("cost") not in (None, "") else "?",
            "power": item.get("power") if item.get("power") not in (None, "") else "?",
            "families": item.get("families") or "暂无",
            "description": item.get("description") or "暂无说明",
            "flavor_text": item.get("flavor_text") or "",
            "tags": tags,
            "pets": pets,
            "pet_total": (pets_data or {}).get("total") or len(pets),
            "commandHint": "💡 /洛克wiki 技能 <技能名或ID>",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _extract_command_args_text(self, event: AstrMessageEvent, command_names: List[str]) -> str:
        full_command = str(getattr(event, "message_str", "") or "").strip()
        for command in command_names:
            if command in full_command:
                return full_command.split(command, 1)[1].strip()
        return ""

    def _wiki_catalog_by_token(self, token: str) -> Dict[str, Any] | None:
        text = str(token or "").strip().lower()
        return WIKI_CATALOG_ROUTES_BY_ALIAS.get(text)

    def _split_wiki_command_parts(self, text: str) -> tuple[List[str], int]:
        parts = str(text or "").strip().split()
        page_no = 1
        if parts:
            tail = parts[-1]
            page_match = re.fullmatch(r"(?:p|P|页|第)(\d+)(?:页)?|(\d+)页", tail)
            if page_match and len(parts) > 1:
                page_value = page_match.group(1) or page_match.group(2)
                page_no = max(int(page_value), 1)
                parts = parts[:-1]
        return parts, page_no

    def _wiki_catalog_usage_text(self) -> str:
        catalogs = self._wiki_catalogs_from_payload(self._wiki_catalogs_cache or {})
        names = "、".join((item.get("title") or item.get("key") or "") for item in catalogs[:24])
        return (
            "Wiki 用法：\n"
            "  /洛克wiki <类型> [关键词或ID]\n"
            "  /洛克wiki <关键词或ID>\n"
            "示例：/洛克wiki 水灵、/洛克wiki 技能 圣光斩、/洛克wiki 物品 xx球、/洛克wiki 种植 食谱名\n"
            f"后端目录：{names or '请稍后重试'}"
        )

    async def _get_wiki_catalogs_payload(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and self._wiki_catalogs_cache is not None and now - self._wiki_catalogs_cache_ts < 300:
            return self._wiki_catalogs_cache
        payload = await self.client.get_wiki_catalogs()
        if isinstance(payload, dict):
            self._wiki_catalogs_cache = payload
            self._wiki_catalogs_cache_ts = now
            return payload
        return self._wiki_catalogs_cache or {}

    async def _get_wiki_options_payload(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and self._wiki_options_cache is not None and now - self._wiki_options_cache_ts < 300:
            return self._wiki_options_cache
        payload = await self.client.get_wiki_options()
        if isinstance(payload, dict):
            self._wiki_options_cache = payload
            self._wiki_options_cache_ts = now
            return payload
        return self._wiki_options_cache or {}

    def _wiki_backend_catalog_items(self, payload: Any) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _wiki_backend_catalog_by_key(self, payload: Any) -> Dict[str, Dict[str, Any]]:
        return {
            str(item.get("key") or ""): item
            for item in self._wiki_backend_catalog_items(payload)
            if item.get("key")
        }

    def _wiki_catalog_from_backend_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        key = str(item.get("key") or "").strip()
        base = WIKI_CATALOG_ROUTES_BY_KEY.get(key)
        catalog = dict(base) if base else {
            "key": key,
            "title": str(item.get("name") or key or "Wiki"),
            "aliases": [],
            "list_path": "",
            "detail_path": "",
            "id_fields": [],
            "search": None,
        }
        backend_name = str(item.get("name") or "").strip()
        if backend_name and not base:
            catalog["title"] = backend_name
        aliases = list(catalog.get("aliases") or [])
        for alias in (key, backend_name, backend_name.replace("图鉴", "").strip()):
            if alias and alias not in aliases:
                aliases.append(alias)
        catalog["aliases"] = aliases
        filters = item.get("filters") if isinstance(item.get("filters"), list) else []
        has_q_filter = any(isinstance(f, dict) and f.get("key") == "q" for f in filters)
        route_search = catalog.get("search")
        catalog["search"] = has_q_filter if route_search is None else bool(route_search)
        if item.get("path"):
            catalog["list_path"] = str(item.get("path"))
        catalog["_backend"] = item
        return catalog

    def _wiki_catalog_for_key_from_payload(self, key: str, catalogs_payload: Any) -> Dict[str, Any] | None:
        item = self._wiki_backend_catalog_by_key(catalogs_payload).get(str(key or ""))
        if item:
            return self._wiki_catalog_from_backend_item(item)
        route = WIKI_CATALOG_ROUTES_BY_KEY.get(str(key or ""))
        return dict(route) if route else None

    def _wiki_catalogs_from_payload(self, catalogs_payload: Any) -> List[Dict[str, Any]]:
        return [self._wiki_catalog_from_backend_item(item) for item in self._wiki_backend_catalog_items(catalogs_payload)]

    async def _get_wiki_catalog_by_key(self, key: str) -> Dict[str, Any] | None:
        catalogs_payload = await self._get_wiki_catalogs_payload()
        return self._wiki_catalog_for_key_from_payload(key, catalogs_payload)

    def _wiki_dynamic_catalog_by_token(self, token: str, catalogs_payload: Any) -> Dict[str, Any] | None:
        text = self._normalize_query_text(token)
        if not text:
            return None
        for item in self._wiki_backend_catalog_items(catalogs_payload):
            candidates = [
                item.get("key"),
                item.get("name"),
                str(item.get("name") or "").replace("图鉴", "").strip(),
            ]
            if any(self._normalize_query_text(candidate) == text for candidate in candidates if candidate):
                return self._wiki_catalog_from_backend_item(item)
        return None

    def _parse_wiki_command(self, event: AstrMessageEvent, fallback: str = "") -> tuple[Dict[str, Any] | None, str, int]:
        text = self._extract_command_args_text(event, ["洛克wiki", "洛克百科"]) or str(fallback or "").strip()
        if not text:
            return None, "", 1
        parts, page_no = self._split_wiki_command_parts(text)
        catalog = self._wiki_catalog_by_token(parts[0]) if parts else None
        if catalog:
            return catalog, " ".join(parts[1:]).strip(), page_no
        return None, text, page_no

    def _wiki_global_search_catalogs(self, catalogs_payload: Any = None) -> List[Dict[str, Any]]:
        catalogs = []
        seen = set()
        for catalog in self._wiki_catalogs_from_payload(catalogs_payload):
            key = str(catalog.get("key") or "")
            if key in seen or not catalog.get("list_path") or not catalog.get("search", True):
                continue
            catalogs.append(catalog)
            seen.add(key)
        return catalogs

    def _wiki_global_catalog_priority(self, catalog: Dict[str, Any] | None) -> int:
        key = str((catalog or {}).get("key") or "")
        priorities = {
            "pets": 0,
            "skills": 0,
            "balls": 1,
            "pet-carryons": 1,
            "medals": 1,
            "plants": 1,
            "pet-fruits": 1,
            "recipes": 1,
            "pet-eggs": 1,
            "random-eggs": 1,
            "egg-items": 1,
            "skill-stones": 1,
            "skill-stone-recipes": 1,
            "pet-foods": 1,
            "pet-gifts": 1,
            "furniture": 1,
            "fashion": 1,
            "regions": 1,
            "dungeons": 1,
            "tasks": 1,
            "task-summaries": 1,
            "shops": 1,
            "exchanges": 1,
            "mails": 2,
            "music": 2,
            "chat-emojis": 2,
            "photo-actions": 2,
            "items": 3,
            "profile-assets": 4,
        }
        return priorities.get(key, 2)

    def _wiki_label_for_key(self, key: str) -> str:
        labels = {
            "pet_id": "精灵ID",
            "skill_id": "技能ID",
            "item_id": "物品ID",
            "item_kind": "物品类型",
            "egg_conf_id": "蛋配置ID",
            "random_egg_id": "随机蛋ID",
            "tree_id": "果实树ID",
            "medal_id": "奖章ID",
            "ball_id": "咕噜球ID",
            "plant_id": "种植ID",
            "carryon_id": "携带物ID",
            "furniture_id": "家具ID",
            "suit_id": "套装ID",
            "region_id": "地区ID",
            "dungeon_id": "副本ID",
            "mail_id": "邮件ID",
            "music_id": "音乐ID",
            "asset_type": "资产类型",
            "asset_id": "资产ID",
            "emoji_id": "表情ID",
            "action_type": "动作类型",
            "action_id": "动作ID",
            "task_id": "任务ID",
            "summary_id": "剧情ID",
            "shop_id": "商店ID",
            "exchange_id": "兑换ID",
            "handbook_no": "图鉴编号",
            "stage": "阶段",
            "quality": "品质",
            "cost": "能量",
            "power": "威力",
            "level": "等级",
            "total_exp": "累计经验",
            "next_level_exp": "升级所需",
            "next_level_total_exp": "下级累计",
            "hatch_label": "孵化时间",
            "hatch_seconds": "孵化秒数",
            "egg_type": "蛋类型",
            "egg_size": "蛋尺寸",
            "body_size": "体型",
            "type": "类型",
            "skill_type": "技能类型",
            "damage_type": "伤害类型",
            "element_type": "属性",
            "label_type": "标签",
            "families": "技能族",
            "pet_count": "可用精灵",
            "learnable_pet_count": "可学习精灵",
            "food_close_exp": "亲密经验",
            "close_exp": "亲密经验",
            "home_exp_num": "家园经验",
            "furniture_coin_num": "家具币",
            "need_time_label": "制作时间",
            "interaction_count": "交互次数",
            "comfort": "舒适度",
            "footprint": "占地",
            "gender": "性别",
            "grade": "品级",
            "bond": "羁绊",
            "category": "分类",
            "area": "区域",
            "source": "来源",
            "source_label": "来源",
            "summary": "摘要",
            "description": "说明",
            "flavor_text": "背景说明",
            "enabled": "启用",
            "banned": "禁用",
            "shareable": "可分享",
            "common": "通用",
            "nest_num": "巢穴数",
            "egg_laying_nest_num": "产蛋巢数",
            "pet_lay_egg_rate_percent": "产蛋概率",
            "close_level": "亲密等级",
            "action_label": "行为",
        }
        return labels.get(str(key or ""), str(key or "").replace("_", " "))

    def _wiki_generic_value(self, value: Any, depth: int = 0) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, (int, float)):
            return f"{value:g}" if isinstance(value, float) else str(value)
        if isinstance(value, dict):
            for key in ("name", "label", "title", "summary", "description", "text"):
                text = str(value.get(key) or "").strip()
                if text:
                    return text
            if "min_m" in value or "max_m" in value:
                return self._wiki_range_label(value, "min_m", "max_m", "m")
            if "min_kg" in value or "max_kg" in value:
                return self._wiki_range_label(value, "min_kg", "max_kg", "kg")
            if depth >= 1:
                pairs = []
                for key, item in list(value.items())[:4]:
                    text = self._wiki_generic_value(item, depth + 1)
                    if text:
                        pairs.append(f"{self._wiki_label_for_key(key)}：{text}")
                return "；".join(pairs)
            return ""
        if isinstance(value, list):
            texts = [self._wiki_generic_value(item, depth + 1) for item in value[:6]]
            texts = [item for item in texts if item]
            suffix = " ..." if len(value) > 6 else ""
            return "、".join(texts) + suffix
        return str(value)

    def _wiki_size_label(self, value: Any) -> str:
        if not isinstance(value, dict):
            return self._wiki_generic_value(value)
        parts = []
        height = value.get("height")
        weight = value.get("weight")
        if isinstance(height, dict):
            text = self._wiki_range_label(height, "min_m", "max_m", "m")
            if text and text != "暂无":
                parts.append(f"高 {text}")
        if isinstance(weight, dict):
            text = self._wiki_range_label(weight, "min_kg", "max_kg", "kg")
            if text and text != "暂无":
                parts.append(f"重 {text}")
        return " / ".join(parts)

    def _wiki_footprint_label(self, value: Any) -> str:
        if not isinstance(value, dict):
            return self._wiki_generic_value(value)
        width = value.get("width") or value.get("x") or value.get("cols")
        height = value.get("height") or value.get("y") or value.get("rows")
        if width not in (None, "") and height not in (None, ""):
            return f"{width} x {height}"
        return self._wiki_generic_value(value, 1)

    def _wiki_count_label(self, value: Any, unit: str = "个") -> str:
        if isinstance(value, list):
            return f"{len(value)}{unit}"
        if value in (None, ""):
            return ""
        return f"{value}{unit}" if isinstance(value, int) else str(value)

    def _wiki_pick_image(self, item: Dict[str, Any]) -> str:
        image_keys = [
            "big_icon",
            "image",
            "cover_image",
            "preview_image",
            "checkout_icon",
            "icon",
            "small_icon",
            "background_image",
            "display_image",
            "package_bg",
            "package_cover",
        ]
        for key in image_keys:
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://", "{{")):
                return value
            if isinstance(value, dict):
                url = value.get("url") or value.get("image") or value.get("icon")
                if isinstance(url, str) and url.startswith(("http://", "https://", "{{")):
                    return url
        return ""

    def _wiki_title_for_item(self, item: Dict[str, Any], fallback: str = "Wiki 条目") -> str:
        for key in ("name", "title", "display_name", "summary", "description"):
            value = str(item.get(key) or "").strip()
            if value:
                return value[:48]
        for key in (
            "pet_id",
            "skill_id",
            "item_id",
            "egg_conf_id",
            "random_egg_id",
            "tree_id",
            "medal_id",
            "ball_id",
            "plant_id",
            "furniture_id",
            "suit_id",
            "region_id",
            "dungeon_id",
            "mail_id",
            "music_id",
            "task_id",
            "shop_id",
            "exchange_id",
            "id",
        ):
            if item.get(key) not in (None, ""):
                return f"{fallback} #{item.get(key)}"
        return fallback

    def _wiki_summary_for_item(self, item: Dict[str, Any]) -> str:
        for key in ("summary", "description", "desc", "effect", "flavor_text", "text", "content"):
            value = self._wiki_generic_value(item.get(key))
            if value:
                return value[:180]
        return ""

    def _wiki_badges_for_item(self, item: Dict[str, Any]) -> List[str]:
        badges = []
        for key in ("type", "skill_type", "damage_type", "element_type", "category", "quality", "rarity", "label_type", "egg_type", "grade", "gender"):
            value = self._wiki_generic_value(item.get(key))
            if value:
                badges.append(value)
        for badge in item.get("badges") or []:
            value = self._wiki_generic_value(badge)
            if value:
                badges.append(value)
        for key in ("type_names", "egg_group_names", "tags", "source_counts"):
            values = self._wiki_names(item.get(key))
            badges.extend(values[:4])
        output = []
        seen = set()
        for badge in badges:
            key = str(badge)
            if key and key not in seen:
                output.append(key)
                seen.add(key)
        return output[:8]

    def _wiki_add_fact(self, rows: List[Dict[str, str]], label: str, value: Any) -> None:
        text = self._wiki_generic_value(value)
        if text:
            rows.append({"label": label, "value": text[:96]})

    def _wiki_meta_for_item(self, item: Dict[str, Any], limit: int = 12, catalog_key: str = "") -> List[Dict[str, str]]:
        rows = []
        if catalog_key == "pets":
            self._wiki_add_fact(rows, "图鉴编号", item.get("handbook_no"))
            self._wiki_add_fact(rows, "精灵ID", item.get("pet_id"))
            self._wiki_add_fact(rows, "属性", item.get("type_names") or item.get("types"))
            self._wiki_add_fact(rows, "蛋组", item.get("egg_group_names") or item.get("egg_groups"))
        elif catalog_key == "skills":
            self._wiki_add_fact(rows, "技能ID", item.get("skill_id"))
            self._wiki_add_fact(rows, "属性", item.get("element_type") or item.get("type"))
            self._wiki_add_fact(rows, "类型", item.get("skill_type") or item.get("damage_type"))
            self._wiki_add_fact(rows, "威力 / 能量", f"{item.get('power') or '-'} / {item.get('cost') or '-'}")
            self._wiki_add_fact(rows, "可用精灵", item.get("pet_count"))
        elif catalog_key in {"pet-eggs", "egg-items", "random-eggs", "pet-egg"}:
            self._wiki_add_fact(rows, "蛋ID", item.get("egg_conf_id") or item.get("random_egg_id") or item.get("item_id"))
            self._wiki_add_fact(rows, "蛋类型", item.get("egg_type"))
            self._wiki_add_fact(rows, "孵化时间", item.get("hatch_label"))
            self._wiki_add_fact(rows, "尺寸", self._wiki_size_label(item.get("egg_size")))
            self._wiki_add_fact(rows, "关联精灵", item.get("pet"))
        elif catalog_key in {"pet-foods", "pet-gifts"}:
            self._wiki_add_fact(rows, "物品ID", item.get("item_id"))
            self._wiki_add_fact(rows, "亲密经验", item.get("food_close_exp") or item.get("close_exp"))
            self._wiki_add_fact(rows, "家园经验", item.get("home_exp_num"))
            self._wiki_add_fact(rows, "制作时间", item.get("need_time_label"))
            self._wiki_add_fact(rows, "交互次数", item.get("interaction_count"))
        elif catalog_key == "home-egg-lay-rates":
            self._wiki_add_fact(rows, "巢穴数", item.get("nest_num"))
            self._wiki_add_fact(rows, "产蛋巢", item.get("egg_laying_nest_num"))
            rate = item.get("pet_lay_egg_rate_percent")
            self._wiki_add_fact(rows, "产蛋概率", f"{rate}%" if rate not in (None, "") else "")
        elif catalog_key == "pet-levels":
            self._wiki_add_fact(rows, "等级", item.get("level"))
            self._wiki_add_fact(rows, "累计经验", item.get("total_exp"))
            self._wiki_add_fact(rows, "下级所需", item.get("next_level_exp"))
        elif catalog_key == "furniture":
            self._wiki_add_fact(rows, "家具ID", item.get("furniture_id"))
            self._wiki_add_fact(rows, "分类", item.get("category"))
            self._wiki_add_fact(rows, "舒适度", item.get("comfort"))
            self._wiki_add_fact(rows, "占地", self._wiki_footprint_label(item.get("footprint")))
        elif catalog_key == "fashion":
            self._wiki_add_fact(rows, "套装ID", item.get("suit_id"))
            self._wiki_add_fact(rows, "性别", item.get("gender"))
            self._wiki_add_fact(rows, "品级", item.get("grade"))
            self._wiki_add_fact(rows, "部件", self._wiki_count_label(item.get("parts")))
        elif catalog_key == "exchanges":
            self._wiki_add_fact(rows, "兑换ID", item.get("exchange_id"))
            self._wiki_add_fact(rows, "获得", item.get("get_items"))
            self._wiki_add_fact(rows, "消耗", item.get("cost_items"))
        elif catalog_key == "shops":
            self._wiki_add_fact(rows, "商店ID", item.get("shop_id"))
            self._wiki_add_fact(rows, "页签", item.get("tab_name"))
            self._wiki_add_fact(rows, "商品数", self._wiki_count_label(item.get("goods")))
        elif catalog_key in {"skill-stones", "skill-stone-recipes", "items", "pet-fruits", "recipes", "balls", "plants", "pet-carryons", "medals"}:
            self._wiki_add_fact(rows, "物品ID", item.get("item_id") or item.get("medal_id") or item.get("ball_id") or item.get("plant_id") or item.get("carryon_id"))
            self._wiki_add_fact(rows, "类型", item.get("type") or item.get("label_type"))
            self._wiki_add_fact(rows, "品质", item.get("quality"))
            self._wiki_add_fact(rows, "效果", item.get("summary") or item.get("catch_effect") or item.get("carryon"))
        else:
            for key in (
                "pet_id", "skill_id", "item_id", "tree_id", "level", "close_level", "action_label",
                "region_id", "dungeon_id", "mail_id", "music_id", "emoji_id", "task_id", "summary_id",
                "asset_type", "asset_id", "action_type", "action_id", "quality", "type", "category",
            ):
                if key in item:
                    self._wiki_add_fact(rows, self._wiki_label_for_key(key), item.get(key))
        if rows:
            return rows[:limit]
        skip = {
            "name",
            "title",
            "description",
            "desc",
            "summary",
            "icon",
            "small_icon",
            "big_icon",
            "image",
            "cover_image",
            "preview_image",
            "background_image",
            "items",
            "rewards",
            "reward_items",
            "tags",
            "badges",
            "catalog",
        }
        for key, value in item.items():
            if key in skip:
                continue
            text = self._wiki_generic_value(value)
            if not text:
                continue
            rows.append({"label": self._wiki_label_for_key(key), "value": text[:80]})
            if len(rows) >= limit:
                break
        return rows

    def _wiki_card_for_item(self, item: Dict[str, Any], fallback: str, catalog: Dict[str, Any] | None = None) -> Dict[str, Any]:
        catalog_key = str((catalog or {}).get("key") or "")
        return {
            "title": self._wiki_title_for_item(item, fallback),
            "image": self._wiki_pick_image(item),
            "summary": self._wiki_summary_for_item(item),
            "badges": self._wiki_badges_for_item(item),
            "meta": self._wiki_meta_for_item(item, 5, catalog_key),
        }

    def _wiki_result_command_examples(
        self,
        items: List[Dict[str, Any]],
        catalog: Dict[str, Any] | None = None,
        limit: int = 3,
    ) -> List[str]:
        examples = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            child = self._wiki_catalog_for_key_from_payload(
                str(item.get("_catalog_key") or ""),
                self._wiki_catalogs_cache or {},
            ) or catalog or {}
            category = str(item.get("_catalog_title") or child.get("title") or "").strip()
            title = self._wiki_title_for_item(item, "").strip()
            if not category or not title:
                continue
            command = f"/洛克wiki {category} {title}"
            if command in seen:
                continue
            examples.append(command)
            seen.add(command)
            if len(examples) >= limit:
                break
        return examples

    def _wiki_section_title(self, key: str) -> str:
        labels = {
            "items": "条目",
            "members": "家族成员",
            "probabilities": "概率信息",
            "variants": "蛋型变体",
            "voice_percent": "语音概率",
            "topics": "图鉴课题",
            "rewards": "奖励",
            "reward_items": "奖励",
            "acquire_methods": "获取方式",
            "acquisition": "获取方式",
            "methods": "获取方式",
            "sources": "来源",
            "world_maps": "世界地图",
            "habitats": "栖息地",
            "effects": "效果",
            "rules": "规则",
            "parts": "套装部件",
            "tags": "标签",
            "goods": "商品",
            "mall_items": "商城商品",
            "get_items": "获得物品",
            "cost_items": "消耗物品",
            "comfort_levels": "舒适度收益",
            "levels": "等级成长",
            "pet_bond_counts": "精灵羁绊",
            "pet_home_limits": "入驻上限",
            "settled_bonuses": "入驻加成",
            "enjoy_field_types": "喜欢场景",
            "hate_field_types": "讨厌场景",
            "npc_reactions": "NPC 反应",
            "movements": "骑乘动作",
            "blood": "血脉技能",
            "level": "升级习得",
            "machine": "技能石习得",
            "field": "场景习得",
        }
        return labels.get(str(key or ""), self._wiki_label_for_key(key))

    def _wiki_rows_from_dict(self, value: Dict[str, Any], limit: int = 16) -> List[Dict[str, str]]:
        rows = []
        for key, item in value.items():
            if key in {"icon", "small_icon", "big_icon", "image", "display_image"}:
                continue
            text = self._wiki_generic_value(item)
            if text:
                rows.append({"label": self._wiki_label_for_key(key), "value": text[:120]})
            if len(rows) >= limit:
                break
        return rows

    def _wiki_sections_for_payload(self, payload: Any, catalog: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        sections = []
        def add_list(title: str, value: Any) -> None:
            if not isinstance(value, list) or not value:
                return
            dict_items = [item for item in value if isinstance(item, dict)]
            if dict_items:
                sections.append(
                    {
                        "title": title,
                        "cards": [self._wiki_card_for_item(item, title, catalog) for item in dict_items[:12]],
                        "rows": [],
                        "total": len(dict_items),
                    }
                )
                return
            texts = [self._wiki_generic_value(item) for item in value[:16]]
            texts = [text for text in texts if text]
            if not texts:
                return
            sections.append(
                {
                    "title": title,
                    "cards": [],
                    "rows": [{"label": str(idx), "value": text} for idx, text in enumerate(texts, 1)],
                    "total": len(texts),
                }
            )

        for key, value in payload.items():
            title = self._wiki_section_title(key)
            if isinstance(value, dict) and key in {"probabilities", "npc_reactions", "catch_effect", "carryon", "planting", "limit", "bond", "mall_package"}:
                rows = self._wiki_rows_from_dict(value)
                if rows:
                    sections.append({"title": title, "cards": [], "rows": rows, "total": len(rows)})
                continue
            add_list(title, value)
            if isinstance(value, dict):
                for subkey, subvalue in value.items():
                    add_list(f"{title} / {self._wiki_section_title(subkey)}", subvalue)
        return sections[:8]

    def _wiki_merge_detail_payloads(self, base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base or {})
        for key, value in (extra or {}).items():
            if value in (None, "", [], {}):
                continue
            old_value = merged.get(key)
            if isinstance(old_value, dict) and isinstance(value, dict):
                merged[key] = self._wiki_merge_detail_payloads(old_value, value)
            elif isinstance(old_value, list) and isinstance(value, list):
                seen = set()
                combined = []
                for item in [*old_value, *value]:
                    marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) if isinstance(item, (dict, list)) else str(item)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    combined.append(item)
                merged[key] = combined
            else:
                merged[key] = value
        return merged

    def _wiki_item_detail_companion_keys(self, catalog: Dict[str, Any], detail: Dict[str, Any]) -> List[str]:
        catalog_key = str((catalog or {}).get("key") or "")
        item_id = detail.get("item_id")
        if item_id in (None, ""):
            return []
        if catalog_key in {"skill-stones", "skill-stone-recipes", "balls", "medals", "pet-carryons"}:
            return ["items"] if detail.get("item_kind") else []
        if catalog_key != "items":
            return []

        flags = detail.get("flags") if isinstance(detail.get("flags"), dict) else {}
        type_label = self._wiki_generic_value(detail.get("type") or detail.get("label_type"))
        if flags.get("is_skill_stone") or type_label == "技能石":
            return ["skill-stones"]
        if flags.get("is_skill_stone_recipe") or str(detail.get("name") or "").startswith("配方-"):
            return ["skill-stone-recipes"]
        if type_label == "咕噜球":
            return ["balls"]
        if type_label in {"奖章", "勋章"}:
            return ["medals"]
        if type_label in {"携带物", "精灵携带物"}:
            return ["pet-carryons"]
        return []

    async def _wiki_enrich_item_detail(self, catalog: Dict[str, Any], detail: Any) -> Any:
        if not isinstance(detail, dict):
            return detail
        item_id = detail.get("item_id")
        if item_id in (None, ""):
            return detail

        merged = dict(detail)
        for key in self._wiki_item_detail_companion_keys(catalog, detail):
            companion = self._wiki_catalog_for_key_from_payload(key, self._wiki_catalogs_cache or {})
            if not companion:
                continue
            if key == "items":
                item_kind = detail.get("item_kind")
                if item_kind in (None, ""):
                    continue
                companion_detail = await self.client.get_wiki_path(f"/api/v1/games/rocom/wiki/items/{item_kind}/{item_id}")
                if companion_detail:
                    merged = self._wiki_merge_detail_payloads(companion_detail, merged)
                continue
            companion_detail = await self.client.get_wiki_path(f"/api/v1/games/rocom/wiki/{key}/{item_id}")
            if companion_detail:
                merged = self._wiki_merge_detail_payloads(merged, companion_detail)
        return merged

    def _wiki_path_params_from_item(self, catalog: Dict[str, Any], item: Dict[str, Any], raw_text: str = "") -> Dict[str, str]:
        tokens = str(raw_text or "").split()
        params: Dict[str, str] = {}
        for idx, field in enumerate(catalog.get("id_fields") or []):
            value = item.get(field)
            if value in (None, "") and field == "item_id":
                value = item.get("id")
            if value in (None, "") and field == "asset_id":
                value = item.get("id")
            if value in (None, "") and field == "action_id":
                value = item.get("id")
            if value in (None, "") and field in {"ball_id", "plant_id", "carryon_id"}:
                value = item.get("item_id") or item.get("id")
            if value in (None, "") and idx < len(tokens):
                value = tokens[idx]
            if value not in (None, ""):
                params[field] = str(value)
        return params

    def _wiki_fill_detail_path(self, catalog: Dict[str, Any], params: Dict[str, str]) -> str:
        path = str(catalog.get("detail_path") or "")
        for field in catalog.get("id_fields") or []:
            value = params.get(field)
            if value in (None, ""):
                return ""
            path = path.replace("{" + field + "}", str(value))
        return path

    def _wiki_item_matches_query_exact(self, catalog: Dict[str, Any], item: Dict[str, Any], query: str) -> bool:
        query = str(query or "").strip()
        if not query:
            return False
        normalized_query = self._normalize_query_text(query)
        for field in catalog.get("id_fields") or []:
            value = item.get(field)
            if value in (None, "") and field in {"ball_id", "plant_id", "carryon_id"}:
                value = item.get("item_id") or item.get("id")
            if str(value or "") == query:
                return True
        title = self._wiki_title_for_item(item, "")
        return self._normalize_query_text(title) == normalized_query

    def _wiki_find_catalog_match(
        self,
        catalog: Dict[str, Any],
        items: List[Dict[str, Any]],
        query: str,
        allow_single: bool = True,
    ) -> Dict[str, Any] | None:
        query = str(query or "").strip()
        if not query:
            return None
        for item in items:
            if self._wiki_item_matches_query_exact(catalog, item, query):
                return item
        if allow_single and len(items) == 1:
            return items[0]
        return None

    def _wiki_similarity_score(self, query: str, item: Dict[str, Any]) -> float:
        normalized_query = self._normalize_query_text(query)
        if not normalized_query:
            return 0.0
        candidates = [
            self._wiki_title_for_item(item, ""),
            item.get("name") or "",
            item.get("summary") or "",
            item.get("description") or item.get("desc") or "",
        ]
        best = 0.0
        for text in candidates:
            normalized_text = self._normalize_query_text(text)
            if not normalized_text:
                continue
            if normalized_query == normalized_text:
                best = max(best, 1.0)
            elif normalized_query in normalized_text or normalized_text in normalized_query:
                best = max(best, 0.82)
            else:
                best = max(best, SequenceMatcher(None, normalized_query, normalized_text).ratio())
        return best

    async def _wiki_suggest_catalog_items(
        self,
        catalog: Dict[str, Any] | None,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        if not catalog or not catalog.get("list_path"):
            return []
        query = str(query or "").strip()
        if not query:
            return []
        terms = []
        for term in (query, query[:2], query[:1], query[:-1]):
            term = str(term or "").strip()
            if term and term not in terms:
                terms.append(term)

        collected: Dict[str, Dict[str, Any]] = {}
        for term in terms[:4]:
            res = await self.client.list_wiki_catalog_items(
                catalog["list_path"],
                q=term,
                page_no=1,
                page_size=30,
                search=bool(term and catalog.get("search", True)),
            )
            items = (res or {}).get("items") if isinstance(res, dict) else []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                title = self._wiki_title_for_item(item, "")
                key_parts = [catalog.get("key") or "", title]
                for field in catalog.get("id_fields") or []:
                    if item.get(field) not in (None, ""):
                        key_parts.append(str(item.get(field)))
                dedupe_key = "|".join(key_parts)
                if dedupe_key not in collected:
                    collected[dedupe_key] = item
            if len(collected) >= limit * 3:
                break

        scored = []
        for item in collected.values():
            score = self._wiki_similarity_score(query, item)
            if score >= 0.18:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored:
            return list(collected.values())[:limit]
        return [item for _, item in scored[:limit]]

    def _build_generic_wiki_render_data(
        self,
        catalog: Dict[str, Any],
        payload: Any,
        query: str,
        mode: str,
        page_no: int = 1,
    ) -> Dict[str, Any]:
        payload = payload or {}
        if mode == "global-search":
            items = payload.get("items") if isinstance(payload, dict) else []
            cards = []
            for item in (items or []):
                if not isinstance(item, dict):
                    continue
                child = self._wiki_catalog_for_key_from_payload(
                    str(item.get("_catalog_key") or ""),
                    self._wiki_catalogs_cache or {},
                ) or catalog
                card = self._wiki_card_for_item(item, child.get("title") or "Wiki", child)
                if item.get("_catalog_title"):
                    card["badges"] = [item["_catalog_title"], *card.get("badges", [])]
                cards.append(card)
            return {
                "title": f"全局搜索：{query}",
                "subtitle": "Wiki / 全局搜索",
                "image": "",
                "summary": "未指定分类时，会在所有可搜索的大分类接口中使用后端 q 参数查询。",
                "badges": ["全局搜索", "可继续指定分类"],
                "facts": [
                    {"label": "查询词", "value": query or "-"},
                    {"label": "结果数", "value": str(payload.get("total", len(cards)) if isinstance(payload, dict) else len(cards))},
                    {"label": "搜索接口", "value": str(len(self._wiki_global_search_catalogs(self._wiki_catalogs_cache or {})))},
                ],
                "actionHint": "要查看具体条目，请按卡片绿色分类 tag 加名称继续查询。",
                "actionExamples": self._wiki_result_command_examples(items or [], catalog),
                "cards": cards,
                "sections": [],
                "commandHint": "💡 结果过多时可指定分类，例如 /洛克wiki 技能 水花 或 /洛克wiki 物品 国王球",
                "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
            }
        if mode == "suggestions":
            items = payload.get("items") if isinstance(payload, dict) else []
            cards = []
            for item in (items or []):
                if not isinstance(item, dict):
                    continue
                child = self._wiki_catalog_for_key_from_payload(
                    str(item.get("_catalog_key") or ""),
                    self._wiki_catalogs_cache or {},
                ) or catalog
                card = self._wiki_card_for_item(item, child.get("title") or catalog["title"], child)
                if item.get("_catalog_title"):
                    card["badges"] = [item["_catalog_title"], *card.get("badges", [])]
                cards.append(card)
            return {
                "title": f"没有找到「{query}」",
                "subtitle": f"Wiki / {catalog['title']} / 联想结果",
                "summary": "接口没有返回精确结果，下面是按名称、摘要和相似度整理的候选。",
                "image": "",
                "badges": [catalog["title"], "联想"],
                "facts": [
                    {"label": "查询词", "value": query or "-"},
                    {"label": "候选数", "value": str(len(cards))},
                ],
                "actionHint": "接口没有精确命中时，可按候选卡片的绿色分类 tag 加名称继续查询。",
                "actionExamples": self._wiki_result_command_examples(items or [], catalog),
                "cards": cards,
                "sections": [],
                "commandHint": f"💡 可使用 /洛克wiki {catalog['title']} <候选名称或ID> 继续查询",
                "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
            }
        if mode == "list":
            items = payload.get("items") if isinstance(payload, dict) else []
            cards = [self._wiki_card_for_item(item, catalog["title"], catalog) for item in (items or []) if isinstance(item, dict)]
            catalog_info = payload.get("catalog") if isinstance(payload, dict) and isinstance(payload.get("catalog"), dict) else {}
            title = f"{catalog['title']}列表"
            summary = f"共 {payload.get('total', len(cards)) if isinstance(payload, dict) else len(cards)} 条"
            return {
                "title": title,
                "subtitle": f"Wiki / {catalog['title']} / 第 {page_no} 页",
                "image": "",
                "summary": catalog_info.get("description") or summary,
                "badges": [catalog["title"], "列表"],
                "facts": [
                    {"label": "当前页", "value": str(payload.get("page_no", page_no) if isinstance(payload, dict) else page_no)},
                    {"label": "总页数", "value": str(payload.get("total_pages", "-") if isinstance(payload, dict) else "-")},
                    {"label": "总数", "value": str(payload.get("total", len(cards)) if isinstance(payload, dict) else len(cards))},
                ],
                "actionHint": "搜索结果较多时，可按分类加名称继续查询具体条目。" if query else "",
                "actionExamples": self._wiki_result_command_examples(items or [], catalog) if query else [],
                "cards": cards,
                "sections": [],
                "commandHint": "💡 /洛克wiki <类型> <关键词或ID> | /洛克wiki 查看支持类型",
                "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
            }
        item = payload if isinstance(payload, dict) else {}
        facts = self._wiki_meta_for_item(item, 16, str(catalog.get("key") or ""))
        return {
            "title": self._wiki_title_for_item(item, catalog["title"]),
            "subtitle": f"Wiki / {catalog['title']}",
            "image": self._wiki_pick_image(item),
            "summary": self._wiki_summary_for_item(item),
            "badges": [catalog["title"], *self._wiki_badges_for_item(item)],
            "facts": facts,
            "cards": [],
            "sections": self._wiki_sections_for_payload(item, catalog),
            "commandHint": "💡 /洛克wiki <类型> <关键词或ID> | /洛克wiki 查看支持类型",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    def _build_wiki_catalog_render_data(self, catalogs_payload: Any = None, options_payload: Any = None) -> Dict[str, Any]:
        catalogs = self._wiki_catalogs_from_payload(catalogs_payload)
        options_groups = len(options_payload) if isinstance(options_payload, dict) else 0

        def topic_card(catalog: Dict[str, Any]) -> Dict[str, Any]:
            backend = catalog.get("_backend") if isinstance(catalog.get("_backend"), dict) else {}
            filters = backend.get("filters") if isinstance(backend.get("filters"), list) else []
            filter_text = "、".join(
                str(item.get("label") or item.get("key") or "")
                for item in filters[:6]
                if isinstance(item, dict)
            )
            title = str(catalog.get("title") or backend.get("name") or catalog.get("key") or "Wiki")
            key = str(catalog.get("key") or "")
            count = backend.get("count")
            hint = f"/洛克wiki {title} <关键词或ID>"
            if key:
                hint = f"{hint} / /洛克wiki {key} <关键词或ID>"
            coverage = str(backend.get("description") or "").strip()
            if filter_text:
                coverage = f"{coverage} 筛选：{filter_text}".strip()
            return {
                "title": title,
                "key": key,
                "summary": f"{hint} · 条目 {count}" if count not in (None, "") else hint,
                "desc": key,
                "children": coverage,
            }

        groups = [
            {
                "title": "后端 Wiki 图鉴入口",
                "desc": "以下入口来自 /wiki/catalogs；筛选项来自 /wiki/options 和各入口 filters。",
                "items": [topic_card(item) for item in catalogs],
                "total": len(catalogs),
            }
        ]
        return {
            "title": "洛克 Wiki",
            "subtitle": "统一资料查询入口",
            "summary": "可直接全局搜索，也可指定后端返回的图鉴入口缩小范围；目录和筛选项会从后端接口实时读取。",
            "badges": ["Wiki", "全局搜索", "接口实时"],
            "facts": [
                {"label": "后端目录", "value": str(len(catalogs)) if catalogs else "-"},
                {"label": "筛选模块", "value": str(options_groups) if options_groups else "-"},
                {"label": "搜索方式", "value": "全局搜索 / 指定入口"},
            ],
            "primary": [],
            "groups": groups,
            "commandHint": "💡 示例：/洛克wiki 水灵 | /洛克wiki 技能 圣光斩 | /洛克wiki 物品 国王球",
            "copyright": "AstrBot & WeGame Locke Kingdom Plugin",
        }

    async def _fetch_generic_wiki_catalog(
        self,
        catalog: Dict[str, Any],
        query: str,
        page_no: int,
    ) -> tuple[Any, str, str]:
        query = str(query or "").strip()
        detail_path = str(catalog.get("detail_path") or "")
        list_path = str(catalog.get("list_path") or "")

        if detail_path and query:
            direct_params = self._wiki_path_params_from_item(catalog, {}, query)
            if catalog.get("id_fields") == ["pet_id"] and not str(query).isdigit():
                pet, _, _ = await self._resolve_wiki_pet(query)
                if pet and pet.get("pet_id") not in (None, ""):
                    direct_params["pet_id"] = str(pet.get("pet_id"))
            if catalog.get("id_fields") == ["skill_id"] and not str(query).isdigit():
                skill, _, _ = await self._resolve_wiki_skill(query)
                if skill and skill.get("skill_id") not in (None, ""):
                    direct_params["skill_id"] = str(skill.get("skill_id"))
            direct_path = self._wiki_fill_detail_path(catalog, direct_params)
            type_fields = {"item_kind", "asset_type", "action_type"}
            id_fields = catalog.get("id_fields") or []
            direct_allowed = bool(direct_path) and len(direct_params) >= len(id_fields)
            for field in id_fields:
                if field in type_fields:
                    continue
                if not str(direct_params.get(field) or "").isdigit():
                    direct_allowed = False
                    break
            if direct_allowed:
                detail = await self.client.get_wiki_path(direct_path)
                if detail:
                    return await self._wiki_enrich_item_detail(catalog, detail), "detail", ""

        if not list_path:
            return None, "detail", f"{catalog['title']} 需要提供 ID。用法：/洛克wiki {catalog['title']} <ID>"

        list_res = await self.client.list_wiki_catalog_items(
            list_path,
            q=query,
            page_no=page_no,
            page_size=12,
            search=bool(catalog.get("search", True)),
        )
        if list_res is None:
            return None, "list", f"获取 {catalog['title']} 失败：{self.client.get_last_error()}"
        if not isinstance(list_res, dict) or "items" not in list_res:
            return list_res, "detail", ""

        items = (list_res or {}).get("items") or []
        if query and not items:
            suggestions = await self._wiki_suggest_catalog_items(catalog, query, 10)
            if suggestions:
                return {"items": suggestions, "query": query}, "suggestions", ""
        if detail_path and query and items:
            selected = self._wiki_find_catalog_match(catalog, items, query)
            if selected:
                params = self._wiki_path_params_from_item(catalog, selected, query)
                selected_path = self._wiki_fill_detail_path(catalog, params)
                if selected_path:
                    detail = await self.client.get_wiki_path(selected_path)
                    if detail:
                        return await self._wiki_enrich_item_detail(catalog, detail), "detail", ""
        return list_res, "list", ""

    async def _fetch_wiki_detail_for_catalog_item(
        self,
        catalog: Dict[str, Any],
        item: Dict[str, Any],
        query: str,
    ) -> Dict[str, Any] | None:
        if not catalog.get("detail_path"):
            return None
        params = self._wiki_path_params_from_item(catalog, item, query)
        detail_path = self._wiki_fill_detail_path(catalog, params)
        if not detail_path:
            return None
        detail = await self.client.get_wiki_path(detail_path)
        return await self._wiki_enrich_item_detail(catalog, detail)

    async def _fetch_global_wiki_search(self, query: str, page_no: int) -> tuple[Any, str, str]:
        query = str(query or "").strip()
        if not query:
            return None, "global-search", "请输入 Wiki 关键词。"

        catalogs_payload = await self._get_wiki_catalogs_payload()
        catalogs = self._wiki_global_search_catalogs(catalogs_payload)
        if not catalogs:
            return None, "global-search", "暂无可全局搜索的 Wiki 接口。"

        semaphore = asyncio.Semaphore(8)

        async def fetch_catalog(catalog: Dict[str, Any]) -> tuple[Dict[str, Any], List[Dict[str, Any]], str]:
            async with semaphore:
                res = await self.client.list_wiki_catalog_items(
                    catalog["list_path"],
                    q=query,
                    page_no=page_no,
                    page_size=6,
                    search=True,
                )
            if res is None:
                return catalog, [], self.client.get_last_error("")
            items = (res or {}).get("items") if isinstance(res, dict) else []
            return catalog, [item for item in (items or []) if isinstance(item, dict)], ""

        results = await asyncio.gather(*(fetch_catalog(catalog) for catalog in catalogs), return_exceptions=True)
        collected: Dict[str, Dict[str, Any]] = {}
        exact_keys = set()
        errors = []

        for result in results:
            if isinstance(result, Exception):
                errors.append(str(result))
                continue
            catalog, items, error = result
            if error:
                errors.append(f"{catalog.get('title') or catalog.get('key')}：{error}")
                continue
            for item in items:
                merged = dict(item)
                merged["_catalog_key"] = catalog.get("key")
                merged["_catalog_title"] = catalog.get("title")
                title = self._wiki_title_for_item(item, "")
                key_parts = [str(catalog.get("key") or ""), title]
                for field in catalog.get("id_fields") or []:
                    value = item.get(field)
                    if value in (None, "") and field in {"ball_id", "plant_id", "carryon_id"}:
                        value = item.get("item_id") or item.get("id")
                    if value not in (None, ""):
                        key_parts.append(str(value))
                dedupe_key = "|".join(key_parts)
                if dedupe_key in collected:
                    continue
                score = self._wiki_similarity_score(query, merged)
                if self._wiki_item_matches_query_exact(catalog, item, query):
                    score += 10 - (self._wiki_global_catalog_priority(catalog) * 0.01)
                    exact_keys.add(dedupe_key)
                merged["_match_score"] = score
                collected[dedupe_key] = merged

        items = list(collected.values())
        items.sort(key=lambda item: item.get("_match_score", 0), reverse=True)

        exact_items = [collected[key] for key in exact_keys if key in collected]
        if exact_items:
            exact_items.sort(
                key=lambda item: (
                    self._wiki_global_catalog_priority(self._wiki_catalog_for_key_from_payload(str(item.get("_catalog_key") or ""), catalogs_payload)),
                    -float(item.get("_match_score", 0)),
                )
            )
            best_priority = self._wiki_global_catalog_priority(
                self._wiki_catalog_for_key_from_payload(str(exact_items[0].get("_catalog_key") or ""), catalogs_payload)
            )
            best_items = [
                item
                for item in exact_items
                if self._wiki_global_catalog_priority(self._wiki_catalog_for_key_from_payload(str(item.get("_catalog_key") or ""), catalogs_payload)) == best_priority
            ]
            if len(best_items) == 1:
                exact_item = best_items[0]
                catalog = self._wiki_catalog_for_key_from_payload(str(exact_item.get("_catalog_key") or ""), catalogs_payload)
                if catalog:
                    detail = await self._fetch_wiki_detail_for_catalog_item(catalog, exact_item, query)
                    if detail:
                        return {"item": detail, "_catalog": catalog}, "global-detail", ""

        if items:
            return {
                "items": items[:12],
                "query": query,
                "total": len(items),
                "_global": True,
            }, "global-search", ""

        if errors:
            return None, "global-search", f"全局搜索失败：{errors[0]}"
        return {"items": [], "query": query, "total": 0, "_global": True}, "global-search", ""

    def _atlas_index_path(self) -> str:
        return os.path.join(self.atlas_dir, "path.json")

    def _atlas_pets_dir(self) -> str:
        return os.path.join(self.atlas_dir, "pets")

    def _atlas_pet_alias_path(self) -> str:
        return os.path.join(self.atlas_dir, "othername", "pets.yaml")

    def _atlas_ready(self) -> bool:
        return os.path.isfile(self._atlas_index_path()) and os.path.isdir(self._atlas_pets_dir())

    def _load_atlas_index(self) -> Dict[str, str]:
        try:
            with open(self._atlas_index_path(), "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning(f"[Rocom Atlas] 读取本地图鉴索引失败: {e}")
            return {}
        pets = payload.get("pets") if isinstance(payload, dict) else {}
        return pets if isinstance(pets, dict) else {}

    def _strip_atlas_yaml_value(self, value: str) -> str:
        text = str(value or "").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1].strip()
        return text

    def _load_atlas_pet_aliases(self) -> Dict[str, List[str]]:
        path = self._atlas_pet_alias_path()
        if not os.path.isfile(path):
            return {}
        aliases: Dict[str, List[str]] = {}
        current_name = ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.rstrip("\r\n")
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if not line[:1].isspace() and stripped.endswith(":"):
                        current_name = self._strip_atlas_yaml_value(stripped[:-1])
                        if current_name:
                            key = self._normalize_query_text(current_name)
                            aliases.setdefault(key, [])
                            if current_name not in aliases[key]:
                                aliases[key].append(current_name)
                        continue
                    if current_name and stripped.startswith("-"):
                        alias = self._strip_atlas_yaml_value(stripped[1:])
                        if not alias:
                            continue
                        key = self._normalize_query_text(alias)
                        aliases.setdefault(key, [])
                        if current_name not in aliases[key]:
                            aliases[key].append(current_name)
        except Exception as e:
            logger.warning(f"[Rocom Atlas] 读取本地图鉴别名失败: {e}")
            return {}
        return aliases

    def _atlas_local_image_path(self, atlas_rel_path: str) -> str:
        rel = str(atlas_rel_path or "").replace("\\", "/").lstrip("/")
        if not rel:
            return ""
        candidate = os.path.abspath(os.path.join(self.atlas_dir, *rel.split("/")))
        root = os.path.abspath(self.atlas_dir)
        try:
            if os.path.commonpath([root, candidate]) != root:
                return ""
        except ValueError:
            return ""
        return candidate

    def _atlas_existing_image_path(self, index: Dict[str, str], name: str) -> str:
        path = self._atlas_local_image_path(index.get(name, ""))
        return path if path and os.path.isfile(path) else ""

    def _find_atlas_match(self, query: str) -> tuple[str, str, List[str]]:
        query = str(query or "").strip()
        if not query:
            return "", "", []
        if query.isdigit():
            path = os.path.join(self._atlas_pets_dir(), f"{query}.png")
            if os.path.isfile(path):
                return f"#{query}", path, []

        index = self._load_atlas_index()
        if not index:
            return "", "", []

        normalized_query = self._normalize_query_text(query)
        exact_key = ""
        for name in index.keys():
            if self._normalize_query_text(name) == normalized_query:
                exact_key = name
                break
        if exact_key:
            path = self._atlas_existing_image_path(index, exact_key)
            if path:
                return exact_key, path, []

        alias_map = self._load_atlas_pet_aliases()
        exact_alias_candidates = [
            name
            for name in alias_map.get(normalized_query, [])
            if self._atlas_existing_image_path(index, name)
        ]
        if exact_alias_candidates:
            only = exact_alias_candidates[0]
            return only, self._atlas_existing_image_path(index, only), []

        candidates = []

        def add_candidate(name: str):
            if name not in candidates and self._atlas_existing_image_path(index, name):
                candidates.append(name)

        for name, rel_path in index.items():
            normalized_name = self._normalize_query_text(name)
            if normalized_query and (normalized_query in normalized_name or normalized_name in normalized_query):
                add_candidate(name)
        for alias, names in alias_map.items():
            if normalized_query and (normalized_query in alias or alias in normalized_query):
                for name in names:
                    add_candidate(name)
        if len(candidates) == 1:
            only = candidates[0]
            return only, self._atlas_existing_image_path(index, only), []
        return "", "", candidates[:10]

    def _safe_replace_atlas_dir(self, prepared_dir: str) -> None:
        target = os.path.abspath(self.atlas_dir)
        data_root = os.path.abspath(self.data_dir)
        if os.path.commonpath([data_root, target]) != data_root:
            raise RuntimeError("图鉴目标目录不在插件数据目录内")
        if os.path.isdir(target):
            shutil.rmtree(target)
        shutil.move(prepared_dir, target)

    def _prepare_atlas_dir(self, source_root: str, prepared_dir: str) -> tuple[int, int]:
        os.makedirs(prepared_dir, exist_ok=True)
        for name in ("path.json", "index", "othername", "pets"):
            src = os.path.join(source_root, name)
            dst = os.path.join(prepared_dir, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
        if not os.path.isfile(os.path.join(prepared_dir, "path.json")):
            raise RuntimeError("Atlas 缺少 path.json")
        if not os.path.isfile(os.path.join(prepared_dir, "othername", "pets.yaml")):
            raise RuntimeError("Atlas 缺少 othername/pets.yaml")
        pets_dir = os.path.join(prepared_dir, "pets")
        if not os.path.isdir(pets_dir):
            raise RuntimeError("Atlas 缺少 pets 图片目录")
        image_count = len([name for name in os.listdir(pets_dir) if name.lower().endswith(".png")])
        total_bytes = sum(
            os.path.getsize(os.path.join(root, name))
            for root, _, files in os.walk(prepared_dir)
            for name in files
        )
        return image_count, total_bytes

    async def _emit_atlas_progress(
        self,
        progress_cb: Callable[[int, str], Awaitable[None]] | None,
        percent: int,
        stage: str,
    ) -> None:
        if progress_cb:
            await progress_cb(max(0, min(100, int(percent))), stage)

    async def _download_atlas_zip(
        self,
        client: httpx.AsyncClient,
        url: str,
        zip_path: str,
        progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            downloaded = 0
            with open(zip_path, "wb") as f:
                async for chunk in resp.aiter_bytes(1024 * 256):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            await self._emit_atlas_progress(
                                progress_cb,
                                int(downloaded * 80 / total),
                                "正在下载图鉴压缩包",
                            )
                        elif downloaded == len(chunk):
                            await self._emit_atlas_progress(progress_cb, 20, "正在下载图鉴压缩包")
            await self._emit_atlas_progress(progress_cb, 80, "图鉴压缩包下载完成")

    async def _clone_atlas_repo(
        self,
        dst: str,
        progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        if not shutil.which("git"):
            raise RuntimeError("GitHub 压缩包不可用，且当前环境未安装 git，无法使用 git clone 兜底")
        await self._emit_atlas_progress(progress_cb, 20, "正在连接图鉴仓库")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--progress",
            "https://github.com/Entropy-Increase-Team/Rocom-Atlas.git",
            dst,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        output_parts: List[str] = []

        async def consume_stream(stream: asyncio.StreamReader | None):
            if not stream:
                return
            while True:
                chunk = await stream.read(1024)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="ignore")
                output_parts.append(text)
                for matched in re.findall(r"(\d{1,3})%", text):
                    raw_percent = max(0, min(100, int(matched)))
                    await self._emit_atlas_progress(
                        progress_cb,
                        int(raw_percent * 80 / 100),
                        "正在 git clone 图鉴仓库",
                    )

        consumers = [
            asyncio.create_task(consume_stream(proc.stdout)),
            asyncio.create_task(consume_stream(proc.stderr)),
        ]
        try:
            await asyncio.wait_for(proc.wait(), timeout=180)
            await asyncio.gather(*consumers)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            raise RuntimeError("git clone Rocom-Atlas 超时")
        if proc.returncode != 0:
            message = "".join(output_parts).strip()
            raise RuntimeError(f"git clone Rocom-Atlas 失败：{message or proc.returncode}")
        await self._emit_atlas_progress(progress_cb, 80, "图鉴仓库下载完成")

    async def _download_atlas_archive(
        self,
        progress_cb: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> tuple[int, int]:
        urls = [
            "https://codeload.github.com/Entropy-Increase-Team/Rocom-Atlas/zip/refs/heads/main",
            "https://github.com/Entropy-Increase-Team/Rocom-Atlas/archive/refs/heads/main.zip",
        ]
        tmp_root = tempfile.mkdtemp(prefix="rocom_atlas_")
        zip_path = os.path.join(tmp_root, "atlas.zip")
        extract_dir = os.path.join(tmp_root, "extract")
        prepared_dir = os.path.join(tmp_root, "rocom_atlas")
        clone_dir = os.path.join(tmp_root, "clone")
        try:
            timeout = httpx.Timeout(connect=20.0, read=180.0, write=30.0, pool=20.0)
            zip_errors = []
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                for url in urls:
                    try:
                        await self._download_atlas_zip(client, url, zip_path, progress_cb)
                        break
                    except Exception as e:
                        zip_errors.append(f"{url}: {e}")
                        if os.path.exists(zip_path):
                            os.remove(zip_path)

            if os.path.isfile(zip_path):
                await self._emit_atlas_progress(progress_cb, 80, "正在解压图鉴文件")
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)
                roots = [
                    os.path.join(extract_dir, name)
                    for name in os.listdir(extract_dir)
                    if os.path.isdir(os.path.join(extract_dir, name))
                ]
                if not roots:
                    raise RuntimeError("Atlas 压缩包结构异常")
                image_count, total_bytes = self._prepare_atlas_dir(roots[0], prepared_dir)
            else:
                logger.warning(f"[Rocom Atlas] GitHub zip 下载失败，尝试 git clone：{' | '.join(zip_errors)}")
                await self._clone_atlas_repo(clone_dir, progress_cb)
                await self._emit_atlas_progress(progress_cb, 80, "正在整理图鉴文件")
                image_count, total_bytes = self._prepare_atlas_dir(clone_dir, prepared_dir)
            self._safe_replace_atlas_dir(prepared_dir)
            await self._emit_atlas_progress(progress_cb, 100, "本地图鉴缓存已更新")
            prepared_dir = ""
            return image_count, total_bytes
        finally:
            if prepared_dir and os.path.exists(prepared_dir):
                shutil.rmtree(prepared_dir, ignore_errors=True)
            shutil.rmtree(tmp_root, ignore_errors=True)

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
            "commandHint": "💡 /洛克玩家 [UID]",
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
                    "groupSubtitle": "查询推送服务（含实验性功能）",
                    "menuItems": [
                        {"cmd": "洛克档案", "desc": "生成个人数据名片"},
                        {"cmd": "洛克战绩 <页码>", "desc": "查询并展示近期的对战场次记录"},
                        {"cmd": "洛克背包 <筛选> <页码>", "desc": "查看精灵收集 (筛选:全部/异色/了不起/炫彩，参数可交换)"},
                        {"cmd": "洛克阵容 <分类> <页码>", "desc": "查看阵容助手推荐阵容 (参数可交换)"},
                        {"cmd": "洛克交换大厅 <页码>", "desc": "查看交换大厅海报 (支持别名：洛克大厅/交换大厅)"},
                        {"cmd": "远行商人", "desc": "查看当前轮次远行商人商品"},
                        {"cmd": "洛克公告 [页码]", "desc": "查询洛克王国公告列表"},
                        {"cmd": "洛克公告详情 <公告ID>", "desc": "查看指定公告详情"},
                        {"cmd": "洛克公告最新", "desc": "查看最新一条公告"},
                        {"cmd": "洛克活动日历", "desc": "查询 activities/info 活动日历"},
                        {"cmd": "订阅洛克公告", "desc": "订阅新公告推送（群聊需群主/群管/bot管理员）"},
                        {"cmd": "取消订阅洛克公告", "desc": "关闭当前会话的新公告推送"},
                        {"cmd": "洛克商店 <shop_id>", "desc": "实验性：查询商店信息，接口返回暂不稳定"},
                        {"cmd": "洛克玩家 [UID]", "desc": "通过 ingame 队列接口查询玩家基础信息"},
                        {"cmd": "洛克家园 [UID]", "desc": "通过 UID 查询自己或他人的家园菜园、守卫和室内精灵"},
                        {"cmd": "家园详情 [UID] [pet_gid] [npc_id]", "desc": "查询目标家园摆放精灵完整数据，目标玩家需在线"},
                        {"cmd": "订阅家园菜园 [UID]", "desc": "订阅指定 UID 的菜园提醒：首个成熟/全部成熟"},
                        {"cmd": "订阅家园灵感 [UID]", "desc": "订阅指定 UID 的灵感提醒：首个完成/全部完成"},
                        {"cmd": "订阅家园生蛋 [UID]", "desc": "订阅指定 UID 的生蛋提醒：首个可领取/全部可领取"},
                        {"cmd": "取消订阅家园 [菜园/灵感/生蛋/全部] [UID]", "desc": "取消当前会话的家园订阅"},
                        {"cmd": "订阅远行商人 1/0 [商品 商品]", "desc": "群主/群管/bot管理可配置本群订阅商品，不填商品则用默认配置"},
                        {"cmd": "取消订阅远行商人", "desc": "关闭当前群远行商人订阅"},
                        {"cmd": "洛克好友关系 <id1,id2>", "desc": "实验性：仅返回有限状态字段，关系说明暂不稳定（需登录）"},
                        {"cmd": "洛克学生", "desc": "实验性：接口信息量有限，当前仅供测试查看（需登录）"},
                        {"cmd": "图鉴下载", "desc": "下载 Rocom-Atlas 本地图鉴图片，图库仍在构建中可能缺图"},
                        {"cmd": "精灵图鉴 <精灵名>", "desc": "仅查询本地 Rocom-Atlas 图片集"},
                        {"cmd": "洛克wiki [类型] [关键词/ID]", "desc": "统一 Wiki 查询入口；无参数显示支持的专题类型"},
                        {"cmd": "洛克查蛋 <精灵名>", "desc": "后端图鉴优先查询蛋组及可配种精灵，后端不可用时本地兜底 (别名：查蛋)"},
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
                    "请在wegame登录洛克王国完成初始化。"
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

    @filter.command("洛克QQ登录", alias={"洛克qq登录"})
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
        player_search_res = (
            await self.client.ingame_player_search(
                role.get("id", ""),
                fw_token=fw_token,
                user_identifier=user_identifier,
            )
            if role.get("id")
            else None
        )
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
    @filter.command("图鉴下载")
    async def rocom_atlas_download(self, event: AstrMessageEvent):
        """下载 Rocom-Atlas 本地图鉴图片"""
        try:
            sent_thresholds = set()

            async def notify_progress(percent: int, stage: str):
                for threshold in (20, 40, 60, 80, 100):
                    if percent >= threshold and threshold not in sent_thresholds:
                        sent_thresholds.add(threshold)
                        await event.send(event.plain_result(f"图鉴下载进度：{threshold}%（{stage}）"))

            image_count, total_bytes = await self._download_atlas_archive(notify_progress)
            size_mb = total_bytes / 1024 / 1024
            yield event.plain_result(
                f"图鉴下载完成：已缓存 {image_count} 张图片，约 {size_mb:.1f} MB。\n"
                f"本地目录：{self.atlas_dir}\n"
                f"现在可以使用 /精灵图鉴 <精灵名> 查询。\n"
                f"提示：Rocom-Atlas 图库仍在构建中；若仓库为私有仓库，请确保当前环境具备访问权限。"
            )
        except httpx.HTTPError as e:
            yield event.plain_result(f"图鉴下载失败：网络请求异常 {e}\n请稍后重试 /图鉴下载。")
        except Exception as e:
            logger.error(f"[Rocom Atlas] 图鉴下载失败: {e}")
            yield event.plain_result(f"图鉴下载失败：{e}")

    @filter.command("精灵图鉴")
    async def rocom_atlas(self, event: AstrMessageEvent, name: str = ""):
        """查询本地 Atlas 精灵图鉴图片"""
        name = str(name or "").strip()
        if not name:
            yield event.plain_result("请输入精灵名称。用法：/精灵图鉴 <精灵名>")
            return
        if not self._atlas_ready():
            yield event.plain_result(
                "本地图鉴尚未下载，请先运行 /图鉴下载。\n"
                "图鉴图片会下载到 AstrBot 插件数据目录，不会写入插件目录。\n"
                "提示：Rocom-Atlas 图库仍在构建中，部分精灵可能暂时缺图。"
            )
            return
        match_name, image_path, candidates = self._find_atlas_match(name)
        if image_path and os.path.isfile(image_path):
            yield event.image_result(image_path)
            return
        if candidates:
            lines = [f"找到多个图鉴候选，请使用更精确名称："]
            lines.extend(f"{idx}. {item}" for idx, item in enumerate(candidates, 1))
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result(
            f"未找到「{name}」的本地图鉴图片。可运行 /图鉴下载 更新本地图库。\n"
            f"提示：Rocom-Atlas 图库仍在构建中，缺图不代表 Wiki 中没有该精灵。"
        )

    @filter.command("洛克wiki", alias={"洛克百科"})
    async def rocom_wiki(self, event: AstrMessageEvent, name: str = ""):
        """查询 Wiki"""
        catalog, query, page_no = self._parse_wiki_command(event, name)
        raw_text = self._extract_command_args_text(event, ["洛克wiki", "洛克百科"]) or str(name or "").strip()
        raw_key = raw_text.strip().lower()
        catalogs_meta = await self._get_wiki_catalogs_payload()

        if not raw_text or raw_key in {"帮助", "help", "类型", "目录", "专题"}:
            options_meta = await self._get_wiki_options_payload()
            data = self._build_wiki_catalog_render_data(catalogs_meta, options_meta)
            img_url = await self.renderer.render_html(
                "render/wiki/menu/index.html",
                data,
                {"device_scale_factor": 1.35, "viewport_width": 1280, "viewport_height": 1500},
            )
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(self._wiki_catalog_usage_text())
            return

        if catalog:
            catalog = self._wiki_catalog_for_key_from_payload(str(catalog.get("key") or ""), catalogs_meta) or catalog

        if catalog is None:
            parts, parsed_page_no = self._split_wiki_command_parts(raw_text)
            if parts:
                dynamic_catalog = self._wiki_dynamic_catalog_by_token(parts[0], catalogs_meta)
                if dynamic_catalog:
                    catalog = dynamic_catalog
                    query = " ".join(parts[1:]).strip()
                    page_no = parsed_page_no

        if catalog is None:
            payload, mode, error = await self._fetch_global_wiki_search(query, page_no)
            if error:
                yield event.plain_result(error)
                return

            if mode == "global-detail":
                source_catalog = payload.get("_catalog") if isinstance(payload, dict) else {}
                detail = payload.get("item") if isinstance(payload, dict) and isinstance(payload.get("item"), dict) else {}
                source_key = str((source_catalog or {}).get("key") or "")
                if source_key == "pets":
                    pet_id = detail.get("pet_id")
                    profile, skills, family, handbook = await self._fetch_wiki_pet_sections(pet_id)
                    data = self._build_wiki_render_data(detail, profile, skills, family, handbook, query)
                    img_url = await self.renderer.render_html(
                        "render/wiki/pet/index.html",
                        data,
                        {"device_scale_factor": 1.4, "viewport_width": 1120, "viewport_height": 1500},
                    )
                    if img_url:
                        yield event.image_result(img_url)
                    else:
                        yield event.plain_result(
                            f"{data['name']} #{data['number']}\n"
                            f"属性：{' / '.join(data['type_names']) or '暂无'}\n"
                            f"蛋组：{' / '.join(data['egg_groups']) or '暂无'}\n"
                            f"{data['description']}"
                        )
                    return
                if source_key == "skills":
                    pets = await self.client.get_wiki_skill_pets(detail.get("skill_id"))
                    data = self._build_skill_render_data(detail, pets if isinstance(pets, dict) else {}, query)
                    img_url = await self.renderer.render_html(
                        "render/wiki/skill/index.html",
                        data,
                        {"device_scale_factor": 1.4, "viewport_width": 960, "viewport_height": 1200},
                    )
                    if img_url:
                        yield event.image_result(img_url)
                    else:
                        yield event.plain_result(f"{data['name']} #{data['skill_id']}\n{data['description']}")
                    return
                data = self._build_generic_wiki_render_data(source_catalog or {"title": "全局搜索", "key": "global"}, detail, query, "detail", page_no)
                img_url = await self.renderer.render_html(
                    "render/wiki/detail/index.html",
                    data,
                    {"device_scale_factor": 1.35, "viewport_width": 1280, "viewport_height": 1600},
                )
                if img_url:
                    yield event.image_result(img_url)
                else:
                    yield event.plain_result(f"{data['title']}\n{data.get('summary') or ''}")
                return

            data = self._build_generic_wiki_render_data({"title": "全局搜索", "key": "global"}, payload, query, "global-search", page_no)
            img_url = await self.renderer.render_html(
                "render/wiki/list/index.html",
                data,
                {"device_scale_factor": 1.35, "viewport_width": 1280, "viewport_height": 1600},
            )
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(f"{data['title']}\n{data.get('summary') or ''}")
            return

        if catalog.get("key") in {"pets", "pet"} and query:
            overview, candidates, error = await self._resolve_wiki_pet(query)
            if error:
                yield event.plain_result(error)
                return
            if candidates:
                yield event.plain_result(self._wiki_candidate_text(query, candidates, "精灵"))
                return
            if not overview:
                yield event.plain_result("获取 Wiki 精灵详情失败：接口未返回有效数据。")
                return
            pet_id = overview.get("pet_id")
            profile, skills, family, handbook = await self._fetch_wiki_pet_sections(pet_id)
            data = self._build_wiki_render_data(overview, profile, skills, family, handbook, query)
            img_url = await self.renderer.render_html(
                "render/wiki/pet/index.html",
                data,
                {"device_scale_factor": 1.4, "viewport_width": 1120, "viewport_height": 1500},
            )
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(f"{data['name']} #{data['number']}\n{data['description']}")
            return

        if catalog.get("key") in {"skills", "skill"} and query:
            detail, candidates, error = await self._resolve_wiki_skill(query)
            if error:
                yield event.plain_result(error)
                return
            if candidates:
                yield event.plain_result(self._wiki_candidate_text(query, candidates, "技能"))
                return
            if not detail:
                yield event.plain_result("获取技能详情失败：接口未返回有效数据。")
                return
            pets = await self.client.get_wiki_skill_pets(detail.get("skill_id"))
            data = self._build_skill_render_data(detail, pets if isinstance(pets, dict) else {}, query)
            img_url = await self.renderer.render_html(
                "render/wiki/skill/index.html",
                data,
                {"device_scale_factor": 1.4, "viewport_width": 960, "viewport_height": 1200},
            )
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(f"{data['name']} #{data['skill_id']}\n{data['description']}")
            return

        payload, mode, error = await self._fetch_generic_wiki_catalog(catalog, query, page_no)
        if error:
            yield event.plain_result(error)
            return
        data = self._build_generic_wiki_render_data(catalog, payload, query, mode, page_no)
        template_name = "render/wiki/detail/index.html"
        if mode == "list":
            template_name = "render/wiki/list/index.html"
        elif mode == "suggestions":
            template_name = "render/wiki/suggestions/index.html"
        img_url = await self.renderer.render_html(
            template_name,
            data,
            {"device_scale_factor": 1.35, "viewport_width": 1280, "viewport_height": 1600},
        )
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(f"{data['title']}\n{data.get('summary') or ''}")

    @filter.command("洛克公告")
    async def rocom_announcement_list(self, event: AstrMessageEvent, page: int = 1):
        """查询洛克王国公告列表"""
        try:
            page = max(int(page or 1), 1)
        except (TypeError, ValueError):
            page = 1
        res = await self.client.get_announcement_list(page=page, limit=8)
        if not res:
            yield event.plain_result(f"获取公告列表失败：{self.client.get_last_error()}")
            return
        data = self._build_announcement_list_render_data(res)
        img_url = await self.renderer.render_html(
            "render/announcement/list.html",
            data,
            {"device_scale_factor": 1.5, "viewport_width": 1100, "viewport_height": 1200},
        )
        if img_url:
            yield event.image_result(img_url)
        else:
            titles = [item.get("title", "未命名公告") for item in (res.get("list") or res.get("items") or [])[:8]]
            yield event.plain_result("公告列表：\n" + "\n".join(titles))

    @filter.command("洛克公告详情")
    async def rocom_announcement_detail(self, event: AstrMessageEvent, thread_id: str = ""):
        """查询洛克王国公告详情"""
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            yield event.plain_result("请提供公告 ID。用法：/洛克公告详情 <公告ID>")
            return
        res = await self.client.get_announcement_detail(thread_id)
        if not res:
            yield event.plain_result(
                f"获取公告详情失败：{self.client.get_last_error()}\n请注意公告 ID 是否正确。"
            )
            return
        data = self._build_announcement_detail_render_data(res)
        img_url = await self.renderer.render_html(
            "render/announcement/detail.html",
            data,
            {"device_scale_factor": 1.5, "viewport_width": 1100, "viewport_height": 1200},
        )
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(f"{data['title']}\n{data.get('summary') or '该公告暂无摘要。'}")

    @filter.command("洛克公告最新")
    async def rocom_announcement_latest(self, event: AstrMessageEvent):
        """查询最新洛克王国公告"""
        res = await self.client.get_announcement_latest()
        if not res:
            yield event.plain_result(f"获取最新公告失败：{self.client.get_last_error()}")
            return
        detail = await self.client.get_announcement_detail(self._announcement_id(res)) or res
        data = self._build_announcement_detail_render_data(detail)
        img_url = await self.renderer.render_html(
            "render/announcement/detail.html",
            data,
            {"device_scale_factor": 1.5, "viewport_width": 1100, "viewport_height": 1200},
        )
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(f"{data['title']}\n{data.get('summary') or '该公告暂无摘要。'}")

    @filter.command("洛克活动日历", alias={"洛克活动", "洛克日历"})
    async def rocom_activity_calendar(self, event: AstrMessageEvent):
        """查询洛克王国活动日历"""
        res = await self.client.get_activities_info()
        if not res:
            yield event.plain_result(f"获取活动日历失败：{self.client.get_last_error()}")
            return
        data = self._build_activity_calendar_render_data(res)
        if data.get("empty"):
            yield event.plain_result("当前没有可展示的洛克王国活动。")
            return
        img_url = await self.renderer.render_html(
            "render/activity-calendar/index.html",
            data,
            {"device_scale_factor": 1.0, "viewport_width": 2200, "viewport_height": 900},
        )
        if img_url:
            yield event.image_result(img_url)
        else:
            names = [item["name"] for lane in data.get("lanes", []) for item in lane][:10]
            yield event.plain_result("活动日历：\n" + "\n".join(names))

    @filter.command("订阅洛克公告")
    async def subscribe_announcement(self, event: AstrMessageEvent):
        """订阅洛克王国新公告提醒"""
        if not event.is_private_chat() and not await self._is_group_admin(event):
            yield event.plain_result("仅当前群管理员可以配置洛克公告订阅。")
            return
        key = str(event.unified_msg_origin)
        latest = await self.client.get_announcement_latest()
        latest_id = self._announcement_id(latest) if latest else ""
        latest_ts = self._announcement_ts(latest) if latest else int(time.time())
        await self.announcement_sub_mgr.upsert_subscription(
            key,
            {
                "key": key,
                "umo": event.unified_msg_origin,
                "updated_by": str(event.get_sender_id()),
                "last_id": latest_id,
                "since_ts": latest_ts,
                "updated_at": int(time.time()),
            },
        )
        yield event.plain_result("已订阅洛克公告，新公告发布后会推送到当前会话。")

    @filter.command("取消订阅洛克公告")
    async def unsubscribe_announcement(self, event: AstrMessageEvent):
        """取消洛克王国新公告提醒"""
        if not event.is_private_chat() and not await self._is_group_admin(event):
            yield event.plain_result("仅当前群管理员可以取消洛克公告订阅。")
            return
        key = str(event.unified_msg_origin)
        deleted = await self.announcement_sub_mgr.delete_subscription(key)
        if deleted:
            yield event.plain_result("已取消当前会话的洛克公告订阅。")
        else:
            yield event.plain_result("当前会话没有洛克公告订阅。")

    @filter.command("远行商人", alias={"yxsr"})
    async def rocom_merchant(self, event: AstrMessageEvent):
        """查询远行商人"""
        img_url, _, products, round_info = await self._render_merchant_image()
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
        """通过 ingame 接口搜索玩家，未传 UID 时查询当前绑定账号"""
        uid, fw_token, user_identifier = await self._resolve_ingame_identity(event, uid)
        if not uid and not fw_token:
            yield event.plain_result("请提供玩家 UID，或先完成绑定后使用 /洛克玩家。")
            return
        res = await self.client.ingame_player_search(
            uid,
            fw_token=fw_token,
            user_identifier=user_identifier,
        )
        if not res:
            yield event.plain_result(f"玩家搜索失败：{self.client.get_last_error()}")
            return
        data = self._build_player_search_render_data(res, uid or "当前绑定")
        img_url = await self.renderer.render_html("render/player-search/index.html", data)
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(self._format_json_payload(res))

    @filter.command("洛克家园")
    async def rocom_home(self, event: AstrMessageEvent, uid: str = ""):
        """通过 UID 查询洛克家园菜园、守卫精灵与室内精灵"""
        uid, fw_token, user_identifier = await self._resolve_ingame_identity(event, uid)
        if not uid and not fw_token:
            yield event.plain_result("请提供玩家 UID，或先完成绑定后使用 /洛克家园。")
            return
        res = await self.client.ingame_home_info(
            uid,
            fw_token=fw_token,
            user_identifier=user_identifier,
        )
        if not res:
            yield event.plain_result(f"家园查询失败：{self.client.get_last_error()}")
            return
        data = self._build_home_render_data(res, uid or "当前绑定")
        img_url = await self.renderer.render_html(
            "render/home/index.html",
            data,
            {
                "device_scale_factor": 3,
                "viewport_width": 1500,
                "viewport_height": 1200,
                "image_format": "jpeg",
                "image_quality": 82,
            },
        )
        if img_url:
            yield event.image_result(img_url)
        else:
            yield event.plain_result(self._format_json_payload(res))

    @filter.command("家园详情", alias={"洛克精灵数据", "精灵数据"})
    async def rocom_pet_data(self, event: AstrMessageEvent, uid: str = "", pet_gid: str = "", npc_id: str = ""):
        """查询目标家园摆放精灵的完整 ingame 数据"""
        uid = str(uid or "").strip()
        pet_gid = str(pet_gid or "").strip()
        npc_id = str(npc_id or "").strip()
        if (pet_gid and not npc_id) or (npc_id and not pet_gid):
            yield event.plain_result(
                "请同时提供 pet_gid 和 npc_id。用法：/家园详情 <UID> 或 /家园详情 <UID> <pet_gid> <npc_id>\n"
                "提示：该接口依赖目标玩家在线，npc_id 也可以使用 furniture_guid。"
            )
            return
        uid, fw_token, user_identifier = await self._resolve_ingame_identity(event, uid)
        if not uid and not fw_token:
            yield event.plain_result("请提供玩家 UID，或先完成绑定后使用 /家园详情。目标玩家需要在线。")
            return

        res = await self.client.ingame_pet_data(
            uid,
            pet_gid=pet_gid,
            npc_id=npc_id,
            fw_token=fw_token,
            user_identifier=user_identifier,
        )
        if not res:
            yield event.plain_result(
                f"家园详情查询失败：{self.client.get_last_error()}\n"
                "提示：该接口需要目标玩家在线，且目标家园可访问；离线时通常无法获取完整数据。"
            )
            return

        options_meta, skill_lookup, size_lookup = await asyncio.gather(
            self._get_wiki_options_payload(),
            self._pet_data_wiki_skill_lookup(res),
            self._pet_data_wiki_size_lookup(res),
        )
        data = self._build_pet_data_render_data(
            res,
            uid or "当前绑定",
            options=options_meta,
            skill_lookup=skill_lookup,
            size_lookup=size_lookup,
            single_query=bool(pet_gid and npc_id),
            low_bandwidth_mode=self.low_bandwidth_mode,
        )
        render_options = {
            "device_scale_factor": 2,
            "viewport_width": 1500,
            "viewport_height": 1200,
            "image_format": "jpeg",
            "image_quality": 84,
        }
        if self.low_bandwidth_mode:
            render_options.update(
                {
                    "device_scale_factor": 1,
                    "viewport_width": 1400,
                    "viewport_height": 1000,
                    "image_quality": 72,
                    "image_wait_timeout": 2000,
                    "screenshot_timeout": 20000,
                    "screenshot_scale": "css",
                }
            )
        img_url = await self.renderer.render_html(
            "render/pet-data/index.html",
            data,
            render_options,
        )
        if img_url:
            yield event.image_result(img_url)
            return

        lines = [
            f"家园详情 - UID {data['uid']}（{data['onlineText']}）",
            data["notice"],
        ]
        for pet in data.get("pets", [])[:8]:
            lines.append(
                f"{pet['index']}. {pet['name']} Lv.{pet['level']} #{pet['baseId']} "
                f"{pet['variantText']} 分贝 {pet.get('voiceText', '--')}"
            )
        if not data.get("pets"):
            lines.append(data["emptyText"])
        lines.append("")
        lines.append("若服务器带宽过小导致生成超时，请在配置项打开低带宽模式。")
        lines.append("低带宽模式开启后，家园详情将不再加载技能图标。")
        yield event.plain_result("\n".join(lines))

    @filter.command("订阅家园菜园")
    async def subscribe_home_garden(self, event: AstrMessageEvent, uid: str = ""):
        """订阅家园菜园成熟提醒"""
        if not event.is_private_chat() and not await self._is_group_admin(event):
            yield event.plain_result("仅当前群管理员可以配置家园菜园订阅。")
            return
        uid = await self._resolve_home_uid(event, uid)
        if not uid:
            yield event.plain_result("请提供玩家 UID，或先完成绑定后再订阅家园菜园。")
            return
        key = self._home_subscription_key(event.unified_msg_origin, uid, "garden")
        await self.home_sub_mgr.upsert_subscription(
            key,
            {
                "key": key,
                "kind": "garden",
                "uid": uid,
                "umo": event.unified_msg_origin,
                "updated_by": str(event.get_sender_id()),
                "sent_event_ids": [],
                "notify_state": {"first": False, "all": False},
                "updated_at": int(time.time()),
            },
        )
        yield event.plain_result(f"已订阅 UID {uid} 的家园菜园提醒：首个成熟和全部成熟时各推送一次。")

    @filter.command("订阅家园灵感")
    async def subscribe_home_inspiration(self, event: AstrMessageEvent, uid: str = ""):
        """订阅家园精灵灵感完成提醒"""
        if not event.is_private_chat() and not await self._is_group_admin(event):
            yield event.plain_result("仅当前群管理员可以配置家园灵感订阅。")
            return
        uid = await self._resolve_home_uid(event, uid)
        if not uid:
            yield event.plain_result("请提供玩家 UID，或先完成绑定后再订阅家园灵感。")
            return
        key = self._home_subscription_key(event.unified_msg_origin, uid, "inspiration")
        await self.home_sub_mgr.upsert_subscription(
            key,
            {
                "key": key,
                "kind": "inspiration",
                "uid": uid,
                "umo": event.unified_msg_origin,
                "updated_by": str(event.get_sender_id()),
                "sent_event_ids": [],
                "notify_state": {"first": False, "all": False},
                "updated_at": int(time.time()),
            },
        )
        yield event.plain_result(f"已订阅 UID {uid} 的家园精灵灵感提醒：首个完成和全部完成时各推送一次。")

    @filter.command("订阅家园生蛋")
    async def subscribe_home_egg(self, event: AstrMessageEvent, uid: str = ""):
        """订阅家园精灵生蛋提醒"""
        if not event.is_private_chat() and not await self._is_group_admin(event):
            yield event.plain_result("仅当前群管理员可以配置家园生蛋订阅。")
            return
        uid = await self._resolve_home_uid(event, uid)
        if not uid:
            yield event.plain_result("请提供玩家 UID，或先完成绑定后再订阅家园生蛋。")
            return
        key = self._home_subscription_key(event.unified_msg_origin, uid, "egg")
        await self.home_sub_mgr.upsert_subscription(
            key,
            {
                "key": key,
                "kind": "egg",
                "uid": uid,
                "umo": event.unified_msg_origin,
                "updated_by": str(event.get_sender_id()),
                "sent_event_ids": [],
                "notify_state": {"first": False, "all": False},
                "updated_at": int(time.time()),
            },
        )
        yield event.plain_result(f"已订阅 UID {uid} 的家园精灵生蛋提醒：首个可领取和全部可领取时各推送一次。")

    @filter.command("取消订阅家园")
    async def unsubscribe_home(self, event: AstrMessageEvent, kind: str = "全部", uid: str = ""):
        """取消家园菜园、灵感或生蛋订阅"""
        if not event.is_private_chat() and not await self._is_group_admin(event):
            yield event.plain_result("仅当前群管理员可以取消家园订阅。")
            return
        kind_map = {
            "菜园": "garden",
            "灵感": "inspiration",
            "生蛋": "egg",
            "全部": "",
            "all": "",
            "garden": "garden",
            "inspiration": "inspiration",
            "egg": "egg",
        }
        selected_kind = kind_map.get(str(kind or "全部").strip(), "")
        deleted = await self.home_sub_mgr.delete_matching(
            event.unified_msg_origin,
            kind=selected_kind,
            uid=str(uid or "").strip(),
        )
        if deleted:
            yield event.plain_result(f"已取消 {deleted} 条家园订阅。")
        else:
            yield event.plain_result("当前会话没有匹配的家园订阅。")

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

        backend_detail = None
        backend_list = await self.client.get_pet_list(q=name, page_no=1, page_size=10)
        backend_items = (backend_list or {}).get("items") or []
        if backend_items:
            selected = None
            for item in backend_items:
                item_name = str(item.get("name") or "").strip()
                item_form = str(item.get("form") or "").strip()
                if item_name == name or (item_form and f"{item_name}{item_form}" == name):
                    selected = item
                    break
            if selected is None and len(backend_items) == 1:
                selected = backend_items[0]
            if selected is not None:
                backend_detail = await self.client.get_pet_detail(pet_id=selected.get("id"))
                if not backend_detail:
                    backend_detail = selected
        if not backend_detail:
            backend_detail = await self.client.get_pet_detail(name=name)
        if backend_detail:
            compatible_by_group = {}
            for group in backend_detail.get("egg_group") or []:
                group_name = str(group or "").strip()
                if not group_name:
                    continue
                group_res = await self.client.get_pet_list(
                    egg_group=group_name, page_no=1, page_size=31
                )
                compatible_by_group[group_name] = (group_res or {}).get("items") or []
                await asyncio.sleep(0.2)
            data = self.egg_searcher.build_search_data_from_api(
                backend_detail, compatible_by_group
            )
            data["commandHint"] = "💡 数据来自后端图鉴；后端不可用时自动回退本地查蛋"
            data["copyright"] = "AstrBot & WeGame Locke Kingdom Plugin"
            img_url = await self.renderer.render_html("render/searcheggs/index.html", data)
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(
                    f"🥚 {data['pet_name']} (#{data['pet_id']})\n"
                    f"属性：{data['type_label']}\n"
                    f"蛋组：{data['egg_groups_label']}\n"
                    f"可配种精灵数：{data['total_compatible']}"
                )
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
