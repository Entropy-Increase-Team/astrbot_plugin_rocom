"""
WeGame + Rocom HTTP API 客户端

基于单一 API Key 模型：
- 每个开发者仅维护 1 个 WeGame API Key
- 该 Key 统一用于 WeGame 登录层与具体游戏接口 (如 game:rocom)
- session 管理接口依据 X-API-Key + X-User-Identifier 进行身份校验
"""

import asyncio
import httpx
from typing import Optional, Dict, Any, List
from astrbot.api import logger


class RocomClient:
    """洛克王国 API 客户端"""

    LOGIN_PROVIDER = "rocom"
    CLIENT_TYPE = "bot"
    CLIENT_ID = "astrbot"

    def __init__(
        self,
        base_url: str = "https://wegame.shallow.ink",
        wegame_api_key: str = "",
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.wegame_api_key = wegame_api_key
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self.last_error_message: str = ""

    def _set_last_error(self, message: str) -> None:
        self.last_error_message = message

    def _clear_last_error(self) -> None:
        self.last_error_message = ""

    def get_last_error(self, default: str = "接口异常") -> str:
        return self.last_error_message or default

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _wegame_headers(
        self,
        fw_token: str = "",
        user_identifier: str = "",
        client_type: str = "",
        client_id: str = "",
    ) -> Dict[str, str]:
        """登录/账号管理接口的请求头 (scope=wegame)"""
        headers = {}
        if self.wegame_api_key:
            headers["X-API-Key"] = self.wegame_api_key
        
        if fw_token:
            headers["X-Framework-Token"] = fw_token
        if user_identifier:
            headers["X-User-Identifier"] = self._sanitize_uid(user_identifier)
        if client_type:
            headers["X-Client-Type"] = client_type
        if client_id:
            headers["X-Client-ID"] = client_id
        return headers

    def _sanitize_uid(self, uid: str) -> str:
        """参考 Go 端的 SanitizeStrictInput 逻辑"""
        import re
        if not uid: return ""
        uid = str(uid).strip()
        # 注意：服务器端 Go 逻辑允许字母、数字以及中日韩字符。
        cleaned = re.sub(r'[^a-zA-Z0-9_\- \u4e00-\u9fa5]', '', uid)
        return cleaned.strip()

    def _rocom_headers(
        self, fw_token: str, user_identifier: str = ""
    ) -> Dict[str, str]:
        """游戏数据查询接口的请求头 (scope=game:rocom)"""
        headers = {
            "X-Framework-Token": fw_token
        }
        if self.wegame_api_key:
            headers["X-API-Key"] = self.wegame_api_key
        if user_identifier:
            headers["X-User-Identifier"] = self._sanitize_uid(user_identifier)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> Optional[Dict]:
        try:
            self._clear_last_error()
            client = await self._get_client()

            if method == "GET":
                resp = await client.get(f"{self.base_url}{path}", headers=headers, params=params)
            elif method == "POST":
                resp = await client.post(f"{self.base_url}{path}", headers=headers, json=json_data, params=params)
            elif method == "DELETE":
                resp = await client.delete(f"{self.base_url}{path}", headers=headers)
            else:
                logger.error(f"[Rocom API] 不支持的 HTTP 方法: {method}")
                self._set_last_error(f"不支持的 HTTP 方法: {method}")
                return None

            if resp.status_code != 200:
                body_hint = resp.text[:300] if resp.text else ""
                try:
                    body_json = resp.json()
                    body_hint = body_json.get("message") or body_hint
                except Exception:
                    pass
                logger.warning(f"[Rocom API] {path} HTTP 错误: {resp.status_code} {body_hint}")
                self._set_last_error(f"HTTP {resp.status_code}: {body_hint}".strip(": "))
                return None

            if not resp.text or not resp.text.strip():
                logger.warning(f"[Rocom API] {path} 响应为空")
                self._set_last_error("响应为空")
                return None

            try:
                data = resp.json()
            except Exception as json_err:
                logger.warning(f"[Rocom API] {path} JSON 解析失败: {json_err}, 响应内容: {resp.text[:200]}")
                self._set_last_error("JSON 解析失败")
                return None

            if data.get("code") != 0:
                err_message = data.get("message", "未知")
                logger.warning(f"[Rocom API] {path} 错误: {err_message}")
                self._set_last_error(str(err_message))
                return None
            return data.get("data", {})
        except httpx.TimeoutException:
            logger.error(f"[Rocom API] {method} {path} 请求超时")
            self._set_last_error("请求超时")
            return None
        except httpx.RequestError as e:
            logger.error(f"[Rocom API] {method} {path} 请求失败: {e}")
            self._set_last_error(f"请求失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[Rocom API] {method} {path} 异常: {e}")
            self._set_last_error(f"异常: {e}")
            return None

    async def _request_with_status(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        accepted_statuses: tuple[int, ...] = (200,),
        request_timeout: float | None = None,
    ) -> tuple[Optional[int], Optional[Dict]]:
        try:
            self._clear_last_error()
            client = await self._get_client()
            timeout = request_timeout if request_timeout is not None else self.timeout

            if method == "GET":
                resp = await client.get(
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=params,
                    timeout=timeout,
                )
            elif method == "POST":
                resp = await client.post(
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json_data,
                    params=params,
                    timeout=timeout,
                )
            elif method == "DELETE":
                resp = await client.delete(
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=timeout,
                )
            else:
                logger.error(f"[Rocom API] 不支持的 HTTP 方法: {method}")
                self._set_last_error(f"不支持的 HTTP 方法: {method}")
                return None, None

            if resp.status_code not in accepted_statuses:
                body_hint = resp.text[:300] if resp.text else ""
                try:
                    body_json = resp.json()
                    body_hint = body_json.get("message") or body_hint
                except Exception:
                    pass
                logger.warning(f"[Rocom API] {path} HTTP 错误: {resp.status_code} {body_hint}")
                self._set_last_error(f"HTTP {resp.status_code}: {body_hint}".strip(": "))
                return None, None

            if not resp.text or not resp.text.strip():
                logger.warning(f"[Rocom API] {path} 响应为空")
                self._set_last_error("响应为空")
                return None, None

            try:
                data = resp.json()
            except Exception as json_err:
                logger.warning(
                    f"[Rocom API] {path} JSON 解析失败: {json_err}, 响应内容: {resp.text[:200]}"
                )
                self._set_last_error("JSON 解析失败")
                return None, None

            if data.get("code") != 0:
                err_message = data.get("message", "未知")
                logger.warning(f"[Rocom API] {path} 错误: {err_message}")
                self._set_last_error(str(err_message))
                return None, None

            return resp.status_code, data.get("data", {})
        except httpx.TimeoutException:
            logger.error(f"[Rocom API] {method} {path} 请求超时")
            self._set_last_error("请求超时")
            return None, None
        except httpx.RequestError as e:
            logger.error(f"[Rocom API] {method} {path} 请求失败: {e}")
            self._set_last_error(f"请求失败: {e}")
            return None, None
        except Exception as e:
            logger.error(f"[Rocom API] {method} {path} 异常: {e}")
            self._set_last_error(f"异常: {e}")
            return None, None

    async def _get(
        self, path: str, headers: Dict[str, str], params: Optional[Dict] = None
    ) -> Optional[Dict]:
        return await self._request("GET", path, headers, params=params)

    async def _post(
        self,
        path: str,
        headers: Dict[str, str],
        json_data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Optional[Dict]:
        return await self._request("POST", path, headers, params=params, json_data=json_data)

    async def _delete(
        self, path: str, headers: Dict[str, str]
    ) -> Optional[Dict]:
        return await self._request("DELETE", path, headers)

    # ─── 登录相关 ───

    async def qq_qr_login(
        self, user_identifier: str = ""
    ) -> Optional[Dict]:
        """发起 QQ 扫码登录，返回 frameworkToken + qr_image (base64)"""
        params = {
            "client_type": self.CLIENT_TYPE,
            "client_id": self.CLIENT_ID,
            "provider": self.LOGIN_PROVIDER,
        }
        if user_identifier:
            params["user_identifier"] = self._sanitize_uid(user_identifier)
        return await self._get(
            "/api/v1/login/wegame/qr",
            self._wegame_headers(
                user_identifier=user_identifier,
                client_type=self.CLIENT_TYPE,
                client_id=self.CLIENT_ID,
            ),
            params=params,
        )

    async def qq_qr_status(
        self, fw_token: str, user_identifier: str = ""
    ) -> Optional[Dict]:
        """轮询 QQ 扫码状态"""
        params = {}
        if user_identifier:
            params["user_identifier"] = self._sanitize_uid(user_identifier)
        return await self._get(
            "/api/v1/login/wegame/status",
            self._wegame_headers(
                fw_token, user_identifier=user_identifier
            ),
            params=params,
        )

    async def wechat_qr_login(
        self, user_identifier: str = ""
    ) -> Optional[Dict]:
        """发起微信扫码登录，返回 frameworkToken + qr_image (URL)"""
        params = {
            "client_type": self.CLIENT_TYPE,
            "client_id": self.CLIENT_ID,
            "provider": self.LOGIN_PROVIDER,
        }
        if user_identifier:
            params["user_identifier"] = self._sanitize_uid(user_identifier)
        return await self._get(
            "/api/v1/login/wegame/wechat/qr",
            self._wegame_headers(
                user_identifier=user_identifier,
                client_type=self.CLIENT_TYPE,
                client_id=self.CLIENT_ID,
            ),
            params=params,
        )

    async def wechat_qr_status(
        self, fw_token: str, user_identifier: str = ""
    ) -> Optional[Dict]:
        """轮询微信扫码状态"""
        params = {}
        if user_identifier:
            params["user_identifier"] = self._sanitize_uid(user_identifier)
        return await self._get(
            "/api/v1/login/wegame/wechat/status",
            self._wegame_headers(
                fw_token, user_identifier=user_identifier
            ),
            params=params,
        )

    async def get_qq_token(
        self, fw_token: str, user_identifier: str = ""
    ) -> Optional[Dict]:
        """查询 QQ 扫码凭证"""
        user_identifier = self._sanitize_uid(user_identifier)
        params = {}
        if user_identifier:
            params["user_identifier"] = user_identifier
        return await self._get(
            "/api/v1/login/wegame/token",
            self._wegame_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_wechat_token(
        self, fw_token: str, user_identifier: str = ""
    ) -> Optional[Dict]:
        """查询微信扫码凭证"""
        user_identifier = self._sanitize_uid(user_identifier)
        params = {}
        if user_identifier:
            params["user_identifier"] = user_identifier
        return await self._get(
            "/api/v1/login/wegame/wechat/token",
            self._wegame_headers(fw_token, user_identifier),
            params=params,
        )

    async def import_token(
        self, tgp_id: str, tgp_ticket: str, user_identifier: str = ""
    ) -> Optional[Dict]:
        """导入 tgp_id + tgp_ticket 凭证"""
        user_identifier = self._sanitize_uid(user_identifier)
        body: Dict[str, Any] = {
            "tgp_id": tgp_id,
            "tgp_ticket": tgp_ticket,
            "provider": self.LOGIN_PROVIDER,
            "client_type": self.CLIENT_TYPE,
            "client_id": self.CLIENT_ID,
        }
        if user_identifier:
            body["user_identifier"] = user_identifier
        return await self._post(
            "/api/v1/login/wegame/token",
            self._wegame_headers(
                user_identifier=user_identifier,
                client_type=self.CLIENT_TYPE,
                client_id=self.CLIENT_ID,
            ),
            json_data=body,
        )

    async def create_binding(
        self, fw_token: str, user_identifier: str
    ) -> Optional[Dict]:
        """将匿名创建的 frameworkToken 通过 API Key 绑定给用户，从而获得持久授权"""
        user_identifier = self._sanitize_uid(user_identifier)
        payload = {
            "framework_token": fw_token,
            "user_identifier": user_identifier,
            "client_type": self.CLIENT_TYPE,
            "client_id": self.CLIENT_ID,
        }
        return await self._post(
            "/api/v1/user/bindings",
            # 这里必须带 API Key
            self._wegame_headers(
                user_identifier=user_identifier,
                client_type=self.CLIENT_TYPE,
                client_id=self.CLIENT_ID,
            ),
            json_data=payload,
        )

    async def refresh_binding(
        self, binding_id: str, user_identifier: str
    ) -> Optional[Dict]:
        """刷新绑定凭证"""
        user_identifier = self._sanitize_uid(user_identifier)
        return await self._post(
            f"/api/v1/user/bindings/{binding_id}/refresh",
            self._wegame_headers(user_identifier=user_identifier),
            json_data={},
        )

    async def get_bindings(
        self, user_identifier: str = ""
    ) -> Optional[Dict]:
        """获取用户的绑定列表"""
        user_identifier = self._sanitize_uid(user_identifier)
        params = {}
        if user_identifier:
            params["user_identifier"] = user_identifier
        return await self._get(
            "/api/v1/user/bindings",
            self._wegame_headers(user_identifier=user_identifier),
            params=params,
        )

    async def delete_binding(
        self, binding_id: str, user_identifier: str
    ) -> bool:
        """删除绑定记录"""
        headers = self._wegame_headers(user_identifier=user_identifier)
        res = await self._delete(
            f"/api/v1/user/bindings/{binding_id}",
            headers
        )
        return res is not None

    # ─── 洛克王国游戏数据 ───

    async def get_role(
        self, fw_token: str, account_type: int | None = None, user_identifier: str = ""
    ) -> Optional[Dict]:
        """角色资料"""
        params = {}
        if account_type:
            params["account_type"] = account_type
        return await self._get(
            "/api/v1/games/rocom/profile/role",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_evaluation(
        self, fw_token: str, account_type: int | None = None, user_identifier: str = ""
    ) -> Optional[Dict]:
        """AI 维度评价"""
        params = {}
        if account_type:
            params["account_type"] = account_type
        return await self._get(
            "/api/v1/games/rocom/profile/evaluation",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_pet_summary(
        self, fw_token: str, account_type: int | None = None, user_identifier: str = ""
    ) -> Optional[Dict]:
        """精灵摘要"""
        params = {}
        if account_type:
            params["account_type"] = account_type
        return await self._get(
            "/api/v1/games/rocom/profile/pet-summary",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_collection(
        self, fw_token: str, account_type: int | None = None, user_identifier: str = ""
    ) -> Optional[Dict]:
        """收藏数据"""
        params = {}
        if account_type:
            params["account_type"] = account_type
        return await self._get(
            "/api/v1/games/rocom/profile/collection",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_battle_overview(
        self, fw_token: str, zone: int | None = None, user_identifier: str = ""
    ) -> Optional[Dict]:
        """对战总览"""
        params = {}
        if zone is not None:
            params["zone"] = zone
        return await self._get(
            "/api/v1/games/rocom/profile/battle-overview",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_battle_list(
        self,
        fw_token: str,
        page_size: int = 4,
        after_time: str = "",
        zone: int | None = None,
        user_identifier: str = "",
    ) -> Optional[Dict]:
        """对战记录列表"""
        params: Dict[str, Any] = {"page_size": page_size}
        if after_time:
            params["after_time"] = after_time
        if zone is not None:
            params["zone"] = zone
        return await self._get(
            "/api/v1/games/rocom/battle/list",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_pets(
        self,
        fw_token: str,
        pet_subset: int = 0,
        page_no: int = 1,
        page_size: int = 10,
        zone: int | None = None,
        user_identifier: str = "",
    ) -> Optional[Dict]:
        """精灵列表"""
        params = {
            "pet_subset": pet_subset,
            "page_no": page_no,
            "page_size": page_size,
        }
        if zone is not None:
            params["zone"] = zone
        return await self._get(
            "/api/v1/games/rocom/battle/pets",
            self._rocom_headers(fw_token, user_identifier),
            params,
        )

    async def get_lineup_list(
        self,
        fw_token: str,
        page_no: int = 1,
        category: str = "",
        account_type: int | None = None,
        user_identifier: str = "",
    ) -> Optional[Dict]:
        """查询阵容助手列表"""
        params = {"page_no": page_no}
        if category:
            params["category"] = category
        if account_type:
            params["account_type"] = account_type
        return await self._get(
            "/api/v1/games/rocom/lineup/list",
            self._rocom_headers(fw_token, user_identifier),
            params,
        )

    async def get_exchange_posters(
        self,
        fw_token: str = "",
        page_no: int = 1,
        refresh: bool = False,
        account_type: int | None = None,
        user_identifier: str = "",
    ) -> Optional[Dict]:
        """查询交换大厅海报列表"""
        params = {
            "page_no": max(int(page_no or 1), 1),
            "refresh": "true" if refresh else "false",
        }
        if account_type:
            params["account_type"] = account_type
        return await self._get(
            "/api/v1/games/rocom/exchange/posters",
            self._wegame_headers(fw_token, user_identifier=user_identifier),
            params,
        )

    async def get_merchant_info(self, refresh: bool = False) -> Optional[Dict]:
        """Query merchant activity data."""
        params = {"refresh": "true" if refresh else "false"}
        return await self._get(
            "/api/v1/games/rocom/merchant/info",
            self._wegame_headers(),
            params=params,
        )

    async def query_pet_size(
        self,
        diameter: float,
        weight: float,
        pool: str = "magic",
        page_no: int = 1,
        page_size: int = 30,
    ) -> Optional[Dict]:
        """Query pet candidates by size."""
        params = {
            "diameter": diameter,
            "weight": weight,
            "pool": pool or "magic",
            "include_display_only": "false",
            "page_no": max(int(page_no or 1), 1),
            "page_size": min(max(int(page_size or 30), 1), 100),
        }
        return await self._get(
            "/api/v1/games/rocom/wiki/pet-size/query",
            self._wegame_headers(),
            params=params,
        )

    async def search_egg_by_size(
        self,
        height: float,
        weight: float,
        page_no: int = 1,
        page_size: int = 30,
    ) -> Optional[Dict]:
        """Query egg candidates by displayed height and weight."""
        params = {
            "height": height,
            "weight": weight,
            "page_no": max(int(page_no or 1), 1),
            "page_size": min(max(int(page_size or 30), 1), 100),
        }
        return await self._get(
            "/api/v1/games/rocom/egg/search",
            self._wegame_headers(),
            params=params,
        )

    async def get_egg_groups(self) -> Optional[Dict]:
        """Query egg group dictionary."""
        return await self._get(
            "/api/v1/games/rocom/egg/groups",
            self._wegame_headers(),
        )

    async def get_egg_group_pets(
        self,
        group_ids: list[int] | tuple[int, ...] | str,
        match_mode: str = "all",
        page_no: int = 1,
        page_size: int = 60,
    ) -> Optional[Dict]:
        """Query pets by egg group ids."""
        if isinstance(group_ids, str):
            group_id_text = group_ids
        else:
            group_id_text = ",".join(str(item) for item in group_ids if item not in (None, ""))
        if not group_id_text:
            self._set_last_error("蛋组 ID 不能为空")
            return None
        params = {
            "group_ids": group_id_text,
            "match_mode": match_mode or "all",
            "page_no": max(int(page_no or 1), 1),
            "page_size": min(max(int(page_size or 60), 1), 100),
        }
        return await self._get(
            "/api/v1/games/rocom/egg/group-pets",
            self._wegame_headers(),
            params=params,
        )

    async def get_egg_pet_groups(
        self,
        q: str,
        limit: int = 20,
    ) -> Optional[Dict]:
        """Query egg groups by pet name or form keyword."""
        keyword = str(q or "").strip()
        if not keyword:
            self._set_last_error("精灵名称不能为空")
            return None
        params = {
            "q": keyword,
            "limit": min(max(int(limit or 20), 1), 50),
        }
        return await self._get(
            "/api/v1/games/rocom/egg/pet-groups",
            self._wegame_headers(),
            params=params,
        )

    async def list_wiki_pets(
        self,
        q: str = "",
        page_no: int = 1,
        page_size: int = 10,
        **filters: Any,
    ) -> Optional[Dict]:
        params: Dict[str, Any] = {
            "page_no": max(int(page_no or 1), 1),
            "page_size": min(max(int(page_size or 10), 1), 100),
        }
        if q:
            params["q"] = q
        for key, value in filters.items():
            if value not in (None, ""):
                params[key] = value
        return await self._get(
            "/api/v1/games/rocom/wiki/pets",
            self._wegame_headers(),
            params=params,
        )

    async def get_wiki_pet(self, pet_id: int | str) -> Optional[Dict]:
        return await self._get(
            f"/api/v1/games/rocom/wiki/pets/{pet_id}",
            self._wegame_headers(),
        )

    async def get_wiki_pet_profile(self, pet_id: int | str) -> Optional[Dict]:
        return await self._get(
            f"/api/v1/games/rocom/wiki/pets/{pet_id}/profile",
            self._wegame_headers(),
        )

    async def get_wiki_pet_skills(self, pet_id: int | str) -> Optional[Dict]:
        return await self._get(
            f"/api/v1/games/rocom/wiki/pets/{pet_id}/skills",
            self._wegame_headers(),
        )

    async def get_wiki_pet_family(self, pet_id: int | str) -> Optional[Dict]:
        return await self._get(
            f"/api/v1/games/rocom/wiki/pets/{pet_id}/family",
            self._wegame_headers(),
        )

    async def get_wiki_pet_handbook(self, pet_id: int | str) -> Optional[Dict]:
        return await self._get(
            f"/api/v1/games/rocom/wiki/pets/{pet_id}/handbook",
            self._wegame_headers(),
        )

    async def list_wiki_skills(
        self,
        q: str = "",
        page_no: int = 1,
        page_size: int = 10,
        **filters: Any,
    ) -> Optional[Dict]:
        params: Dict[str, Any] = {
            "page_no": max(int(page_no or 1), 1),
            "page_size": min(max(int(page_size or 10), 1), 100),
        }
        if q:
            params["q"] = q
        for key, value in filters.items():
            if value not in (None, ""):
                params[key] = value
        return await self._get(
            "/api/v1/games/rocom/wiki/skills",
            self._wegame_headers(),
            params=params,
        )

    async def get_wiki_skill(self, skill_id: int | str) -> Optional[Dict]:
        return await self._get(
            f"/api/v1/games/rocom/wiki/skills/{skill_id}",
            self._wegame_headers(),
        )

    async def get_wiki_skill_pets(self, skill_id: int | str) -> Optional[Dict]:
        return await self._get(
            f"/api/v1/games/rocom/wiki/skills/{skill_id}/pets",
            self._wegame_headers(),
        )

    async def get_wiki_catalogs(self) -> Optional[Dict]:
        return await self._get(
            "/api/v1/games/rocom/wiki/catalogs",
            self._wegame_headers(),
        )

    async def get_wiki_options(self) -> Optional[Dict]:
        return await self._get(
            "/api/v1/games/rocom/wiki/options",
            self._wegame_headers(),
        )

    async def get_wiki_path(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        path = str(path or "").strip()
        if not path.startswith("/api/v1/games/rocom/wiki/"):
            self._set_last_error("非法 Wiki 路径")
            return None
        return await self._get(path, self._wegame_headers(), params=params or {})

    async def list_wiki_catalog_items(
        self,
        path: str,
        q: str = "",
        page_no: int = 1,
        page_size: int = 10,
        search: bool = True,
    ) -> Optional[Dict]:
        params: Dict[str, Any] = {
            "page_no": max(int(page_no or 1), 1),
            "page_size": min(max(int(page_size or 10), 1), 100),
        }
        if q and search:
            params["q"] = q
        return await self.get_wiki_path(path, params=params)

    async def get_pet_list(
        self,
        q: str = "",
        egg_group: str = "",
        page_no: int = 1,
        page_size: int = 20,
    ) -> Optional[Dict]:
        params: Dict[str, Any] = {
            "page_no": max(int(page_no or 1), 1),
            "page_size": min(max(int(page_size or 20), 1), 100),
        }
        if q:
            params["q"] = q
        if egg_group:
            params["egg_group"] = egg_group
        return await self._get(
            "/api/v1/games/rocom/pet/list",
            self._wegame_headers(),
            params=params,
        )

    async def get_pet_detail(
        self, pet_id: int | str | None = None, name: str = ""
    ) -> Optional[Dict]:
        params: Dict[str, Any] = {}
        if pet_id not in (None, ""):
            params["id"] = pet_id
        elif name:
            params["name"] = name
        else:
            self._set_last_error("宠物 ID 或名称不能为空")
            return None
        return await self._get(
            "/api/v1/games/rocom/pet/detail",
            self._wegame_headers(),
            params=params,
        )

    async def get_announcement_list(
        self,
        category_id: int = 99,
        page: int = 1,
        limit: int = 10,
        order: str = "ttDesc",
    ) -> Optional[Dict]:
        params = {
            "category_id": category_id,
            "page": max(int(page or 1), 1),
            "limit": min(max(int(limit or 10), 1), 50),
            "order": order or "ttDesc",
        }
        return await self._get(
            "/api/v1/games/rocom/announcement/list",
            self._wegame_headers(),
            params=params,
        )

    async def get_announcement_latest(
        self, category_id: int = 99, order: str = "ttDesc"
    ) -> Optional[Dict]:
        return await self._get(
            "/api/v1/games/rocom/announcement/latest",
            self._wegame_headers(),
            params={"category_id": category_id, "order": order or "ttDesc"},
        )

    async def get_announcement_detail(self, thread_id: int | str) -> Optional[Dict]:
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            self._set_last_error("公告 ID 不能为空")
            return None
        return await self._get(
            "/api/v1/games/rocom/announcement/detail",
            self._wegame_headers(),
            params={"thread_id": thread_id},
        )

    async def get_activities_info(self, refresh: bool = False) -> Optional[Dict]:
        """Query RoCom activities and calendar data."""
        params = {"refresh": "true" if refresh else "false"}
        return await self._get(
            "/api/v1/games/rocom/activities/info",
            self._wegame_headers(),
            params=params,
        )

    async def search_wiki_pet(self, query: str, limit: int = 10) -> Optional[Dict]:
        """Search pet wiki entries."""
        return await self.list_wiki_pets(q=query, page_no=1, page_size=limit)

    async def search_wiki_skill(self, query: str, limit: int = 10) -> Optional[Dict]:
        """Search skill wiki entries."""
        return await self.list_wiki_skills(q=query, page_no=1, page_size=limit)

    async def get_ingame_task(
        self,
        task_id: str,
        fw_token: str = "",
        user_identifier: str = "",
    ) -> tuple[Optional[int], Optional[Dict]]:
        return await self._request_with_status(
            "GET",
            f"/api/v1/games/rocom/ingame/tasks/{task_id}",
            self._wegame_headers(fw_token, user_identifier=user_identifier),
            accepted_statuses=(200, 202),
            request_timeout=10.0,
        )

    def _task_result_payload(self, task_data: Optional[Dict]) -> Optional[Dict]:
        if not isinstance(task_data, dict):
            return task_data
        status = str(task_data.get("status") or "").lower()
        if status in {"queued", "pending", "running", "processing"}:
            return None
        for key in ("result", "data"):
            value = task_data.get(key)
            if isinstance(value, dict):
                return value
        if any(key in task_data for key in ("rows", "home_info", "source", "title", "npc_pet", "npc_pets", "query_status")):
            return task_data
        return task_data

    async def _poll_ingame_task(
        self,
        task_id: str,
        label: str,
        fw_token: str = "",
        user_identifier: str = "",
        max_wait_seconds: int = 180,
        poll_interval: int = 5,
    ) -> Optional[Dict]:
        for _ in range(max(1, max_wait_seconds // poll_interval)):
            await asyncio.sleep(poll_interval)
            task_status, task_data = await self.get_ingame_task(
                task_id,
                fw_token=fw_token,
                user_identifier=user_identifier,
            )
            if task_status is None:
                return None
            result = self._task_result_payload(task_data)
            if result:
                return result
            status = str((task_data or {}).get("status") or "").lower()
            if status in {"failed", "error", "cancelled", "canceled"}:
                self._set_last_error(
                    str((task_data or {}).get("message") or f"{label}任务执行失败")
                )
                return None

        self._set_last_error(f"{label}任务仍在队列中，请稍后重试（task_id: {task_id}）")
        return None

    async def _ingame_queued_query(
        self,
        path: str,
        label: str,
        uid: str = "",
        fw_token: str = "",
        user_identifier: str = "",
        wait_ms: int = 5000,
        max_wait_seconds: int = 180,
        uid_param: str = "uid",
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        uid = self._sanitize_uid(uid)
        user_identifier = self._sanitize_uid(user_identifier)
        if not uid and not fw_token:
            self._set_last_error("UID 不能为空")
            return None

        headers = self._wegame_headers(fw_token, user_identifier=user_identifier)
        body: Dict[str, Any] = {"wait_ms": wait_ms}
        params: Dict[str, Any] = {"wait_ms": wait_ms}
        if uid:
            body[uid_param or "uid"] = uid
            params[uid_param or "uid"] = uid
        for key, value in (extra_payload or {}).items():
            if value not in (None, ""):
                body[key] = value
                params[key] = value

        status_code, data = await self._request_with_status(
            "POST",
            path,
            headers,
            json_data=body,
            accepted_statuses=(200, 202),
            request_timeout=10.0,
        )
        if status_code == 200:
            task_id = (data or {}).get("task_id")
            if task_id:
                return await self._poll_ingame_task(
                    task_id,
                    label,
                    fw_token=fw_token,
                    user_identifier=user_identifier,
                    max_wait_seconds=max_wait_seconds,
                )
            return self._task_result_payload(data) or data

        if status_code is None:
            status_code, data = await self._request_with_status(
                "GET",
                path,
                headers,
                params=params,
                accepted_statuses=(200, 202),
                request_timeout=10.0,
            )
            if status_code == 200:
                task_id = (data or {}).get("task_id")
                if task_id:
                    return await self._poll_ingame_task(
                        task_id,
                        label,
                        fw_token=fw_token,
                        user_identifier=user_identifier,
                        max_wait_seconds=max_wait_seconds,
                    )
                return self._task_result_payload(data) or data

        task_id = (data or {}).get("task_id")
        if not task_id:
            if status_code == 202:
                self._set_last_error(f"{label}任务已入队，但未返回 task_id")
            return None

        return await self._poll_ingame_task(
            task_id,
            label,
            fw_token=fw_token,
            user_identifier=user_identifier,
            max_wait_seconds=max_wait_seconds,
        )

    async def ingame_pet_data(
        self,
        uid: str = "",
        pet_gid: int | str | None = None,
        npc_id: int | str | None = None,
        wait_ms: int = 20000,
        fw_token: str = "",
        user_identifier: str = "",
    ) -> Optional[Dict]:
        payload: Dict[str, Any] = {}
        if pet_gid not in (None, ""):
            payload["pet_gid"] = pet_gid
        if npc_id not in (None, ""):
            payload["npc_id"] = npc_id
        return await self._ingame_queued_query(
            "/api/v1/games/rocom/ingame/pet/data",
            "精灵数据查询",
            uid=uid,
            fw_token=fw_token,
            user_identifier=user_identifier,
            wait_ms=wait_ms,
            max_wait_seconds=180,
            uid_param="target_uin",
            extra_payload=payload,
        )

    async def ingame_player_search(
        self,
        uid: str = "",
        fw_token: str = "",
        user_identifier: str = "",
        wait_ms: int = 5000,
    ) -> Optional[Dict]:
        return await self._ingame_queued_query(
            "/api/v1/games/rocom/ingame/player/search",
            "玩家搜索",
            uid=uid,
            fw_token=fw_token,
            user_identifier=user_identifier,
            wait_ms=wait_ms,
            max_wait_seconds=180,
        )

    async def ingame_home_info(
        self,
        uid: str = "",
        wait_ms: int = 5000,
        fw_token: str = "",
        user_identifier: str = "",
    ) -> Optional[Dict]:
        return await self._ingame_queued_query(
            "/api/v1/games/rocom/ingame/home/info",
            "家园查询",
            uid=uid,
            fw_token=fw_token,
            user_identifier=user_identifier,
            wait_ms=wait_ms,
            max_wait_seconds=180,
        )

    async def ingame_merchant_info(self, shop_id: int | str) -> Optional[Dict]:
        params = {"shop_id": shop_id}
        data = await self._get(
            "/api/v1/games/rocom/ingame/merchant/info",
            self._wegame_headers(),
            params=params,
        )
        if data is not None:
            return data
        return await self._post(
            "/api/v1/games/rocom/ingame/merchant/info",
            self._wegame_headers(),
            json_data={"shop_id": shop_id},
        )

    async def get_friendship(
        self, fw_token: str, user_ids: str, user_identifier: str = ""
    ) -> Optional[Dict]:
        params = {"user_ids": user_ids}
        return await self._get(
            "/api/v1/games/rocom/social/friendship",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_student_state(
        self, fw_token: str, account_type: int | None = None, user_identifier: str = ""
    ) -> Optional[Dict]:
        params: Dict[str, Any] = {}
        if account_type is not None:
            params["account_type"] = account_type
        return await self._get(
            "/api/v1/games/rocom/activity/student-state",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def get_student_perks(
        self,
        fw_token: str,
        area: int | None = None,
        account_type: int | None = None,
        user_identifier: str = "",
    ) -> Optional[Dict]:
        params: Dict[str, Any] = {}
        if area is not None:
            params["area"] = area
        if account_type is not None:
            params["account_type"] = account_type
        return await self._get(
            "/api/v1/games/rocom/activity/perks",
            self._rocom_headers(fw_token, user_identifier),
            params=params,
        )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
