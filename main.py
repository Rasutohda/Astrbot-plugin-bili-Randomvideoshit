# -*- coding: utf-8 -*-
import asyncio
import random
import re
import json
import time
import hashlib
import aiohttp
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from enum import IntEnum
from contextlib import suppress
from http.cookies import SimpleCookie

import qrcode
from cryptography.fernet import Fernet

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Star, Context, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image

# ---------- B站API端点 ----------
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# ---------- 扫码状态常量 ----------
QR_CODE_UNSCANNED = 86101
QR_CODE_SCANNED = 86090
QR_CODE_EXPIRED = 86038
QR_CODE_SUCCESS = 0

QR_CODE_EXPIRE_TIME = 180
POLL_INTERVAL = 5

# ---------- 其他常量 ----------
MAX_RETRIES = 2
SENT_VIDEOS_RETENTION_DAYS = 7
WBI_KEY_CACHE_TTL = 3600
MANUAL_COOLDOWN_SECONDS = 60
BATCH_SEND_SIZE = 5
BATCH_SEND_INTERVAL = 1


@register(
    "Astrbot_plugin_bili_Randomvideoshit",  # 插件ID
    "Rasutohda",                            # 作者
    "B站随机视频搬运插件（支持扫码登录）",      # 描述
    "2.0.0",                                 # 版本
    "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit"
)
class BiliRandomVideo(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 配置项
        self.auto_start = self.config.get("auto_start", True)
        self.scan_interval = max(60, int(self.config.get("scan_interval", 3600)))
        self.keyword_cooldown_seconds = int(self.config.get("keyword_cooldown_seconds", 600))
        self.use_whitelist_mode = self.config.get("use_whitelist_mode", False)
        self.whitelist_groups = self.config.get("whitelist_groups", [])
        self.blacklist_groups = self.config.get("blacklist_groups", [])
        self.keywords = self.config.get("keywords", ["随机视频", "来点视频", "B站视频"])

        # 数据目录
        self._data_dir = StarTools.get_data_dir("Astrbot_plugin_bili_Randomvideoshit")
        self._cookie_file = self._data_dir / "cookie.enc"
        self._bound_groups_file = self._data_dir / "bound_groups.json"
        self._sent_videos_file = self._data_dir / "sent_videos.json"
        self._group_cooldown_file = self._data_dir / "group_cooldown.json"
        self._key_file = self._data_dir / ".cookie_key"

        # 运行时状态
        self.cookie: str = ""
        self.bound_groups: Dict[str, str] = {}
        self.sent_videos: Dict[str, dict] = {}
        self.group_cooldown: Dict[str, float] = {}
        self.manual_cooldown: Dict[str, float] = {}
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._login_tasks: Dict[str, asyncio.Task] = {}

        self._data_lock = asyncio.Lock()
        self._cookie_lock = asyncio.Lock()

    # ==================== 初始化与销毁 ====================
    async def initialize(self):
        await self._load_all_data()
        # 自动添加白名单群组到 bound_groups（即使从未触发关键词）
        if self.use_whitelist_mode and self.whitelist_groups:
            for gid in self.whitelist_groups:
                gid_str = str(gid)
                if gid_str not in self.bound_groups:
                    # 构造 unified_msg_origin
                    origin = f"default:GroupMessage:{gid_str}"
                    self.bound_groups[gid_str] = origin
            await self._save_bound_groups()
            logger.info(f"已自动添加白名单群组: {self.whitelist_groups}")
        
        if self.cookie:
            logger.info("B站Cookie已加载")
            if self.auto_start and not self._running:
                self._start_monitor()
        else:
            logger.info("未找到B站Cookie，请使用 /bili login 扫码登录")

    async def terminate(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for task in self._login_tasks.values():
            if not task.done():
                task.cancel()
        self._login_tasks.clear()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        logger.info("B站随机视频搬运插件已停止")

    # ==================== 持久化 ====================
    def _get_fernet(self) -> Fernet:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        if self._key_file.exists():
            key = self._key_file.read_bytes()
            return Fernet(key)
        key = Fernet.generate_key()
        self._key_file.write_bytes(key)
        try:
            self._key_file.chmod(0o600)
        except:
            pass
        return Fernet(key)

    def _encrypt(self, text: str) -> str:
        if not text:
            return ""
        return self._get_fernet().encrypt(text.encode()).decode()

    def _decrypt(self, encrypted: str) -> str:
        if not encrypted:
            return ""
        try:
            return self._get_fernet().decrypt(encrypted.encode()).decode()
        except:
            return ""

    async def _load_all_data(self):
        async with self._data_lock:
            if self._cookie_file.exists():
                enc = self._cookie_file.read_text(encoding='utf-8').strip()
                self.cookie = self._decrypt(enc)
            if self._bound_groups_file.exists():
                try:
                    self.bound_groups = json.loads(self._bound_groups_file.read_text(encoding='utf-8'))
                except:
                    self.bound_groups = {}
            if self._sent_videos_file.exists():
                try:
                    self.sent_videos = json.loads(self._sent_videos_file.read_text(encoding='utf-8'))
                except:
                    self.sent_videos = {}
            if self._group_cooldown_file.exists():
                try:
                    self.group_cooldown = json.loads(self._group_cooldown_file.read_text(encoding='utf-8'))
                except:
                    self.group_cooldown = {}

    async def _save_cookie(self):
        async with self._data_lock:
            enc = self._encrypt(self.cookie)
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._cookie_file.write_text(enc, encoding='utf-8')

    async def _save_bound_groups(self):
        async with self._data_lock:
            self._bound_groups_file.write_text(json.dumps(self.bound_groups, ensure_ascii=False, indent=2), encoding='utf-8')

    async def _save_sent_videos(self):
        async with self._data_lock:
            self._sent_videos_file.write_text(json.dumps(self.sent_videos, ensure_ascii=False, indent=2), encoding='utf-8')

    async def _save_group_cooldown(self):
        async with self._data_lock:
            self._group_cooldown_file.write_text(json.dumps(self.group_cooldown, ensure_ascii=False, indent=2), encoding='utf-8')

    # ==================== 网络与WBI ====================
    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._http_session

    def _get_bili_headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    class WbiHelper:
        _cache = {"keys": None, "expire_at": 0}
        _lock = asyncio.Lock()

        @classmethod
        async def get_keys(cls, session: aiohttp.ClientSession, cookie: str = "") -> Tuple[str, str]:
            async with cls._lock:
                now = time.time()
                if cls._cache["keys"] and now < cls._cache["expire_at"]:
                    return cls._cache["keys"]
                headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
                if cookie:
                    headers['Cookie'] = cookie
                try:
                    async with session.get('https://api.bilibili.com/x/web-interface/nav', headers=headers) as resp:
                        data = await resp.json()
                        if data.get('code') != 0:
                            raise Exception(data.get('message', '获取wbi密钥失败'))
                        img_url = data['data']['wbi_img']['img_url']
                        sub_url = data['data']['wbi_img']['sub_url']
                        img_key = re.search(r'/([^/]+)\.png', img_url).group(1)
                        sub_key = re.search(r'/([^/]+)\.png', sub_url).group(1)
                        cls._cache["keys"] = (img_key, sub_key)
                        cls._cache["expire_at"] = now + WBI_KEY_CACHE_TTL
                        return img_key, sub_key
                except Exception as e:
                    cls._cache["keys"] = None
                    cls._cache["expire_at"] = 0
                    raise e

        @staticmethod
        def sign(params: dict, img_key: str, sub_key: str) -> dict:
            mixin_key = img_key + sub_key
            sorted_params = sorted(params.items())
            query = '&'.join([f"{k}={v}" for k, v in sorted_params])
            params['w_rid'] = hashlib.md5((query + mixin_key).encode()).hexdigest()
            params['wts'] = int(time.time())
            return params

    async def _fetch_json(self, url: str, params: dict, need_sign: bool = False, retry: int = MAX_RETRIES) -> Optional[dict]:
        if need_sign and not self.cookie:
            return None
        session = await self._get_http_session()
        for attempt in range(retry):
            try:
                if need_sign:
                    try:
                        img_key, sub_key = await self.WbiHelper.get_keys(session, self.cookie)
                        params = self.WbiHelper.sign(params, img_key, sub_key)
                    except Exception as e:
                        logger.warning(f"WBI签名失败: {e}")
                        return None
                headers = self._get_bili_headers()
                if self.cookie:
                    headers['Cookie'] = self.cookie
                async with session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
                    if data.get('code') == 0:
                        return data
                    elif data.get('code') == -509:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        logger.warning(f"API错误 {url}: {data.get('message')} (code={data.get('code')})")
                        return None
            except Exception as e:
                logger.warning(f"请求失败 {attempt+1}/{retry}: {e}")
                await asyncio.sleep(2 ** attempt)
        return None

    async def _fetch_random_video(self) -> Optional[dict]:
        if not self.cookie:
            return None
        data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular/series/one", {"series_id": 0}, need_sign=True)
        video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular", {"pn": 1, "ps": 50}, need_sign=True)
            video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            return None

        candidates = [v for v in video_list if v.get('bvid') not in self.sent_videos]
        if not candidates:
            logger.warning("所有热门视频都已推送过，等待新视频")
            return None

        for _ in range(5):
            video = random.choice(candidates)
            detail = await self._fetch_json("https://api.bilibili.com/x/web-interface/view", {"bvid": video['bvid']}, need_sign=True)
            if not detail:
                continue
            info = detail['data']
            stats = info.get('stat', {})
            return {
                'bvid': info['bvid'],
                'title': info['title'],
                'author': info.get('owner', {}).get('name'),
                'pic': info.get('pic'),
                'url': f"https://www.bilibili.com/video/{info['bvid']}",
                'play': stats.get('view', 0),
                'like': stats.get('like', 0),
                'coin': stats.get('coin', 0),
                'favorite': stats.get('favorite', 0),
                'danmaku': stats.get('danmaku', 0),
            }
        return None

    def _format_number(self, num: int) -> str:
        if num >= 100000000:
            return f"{num/100000000:.1f}亿"
        elif num >= 10000:
            return f"{num/10000:.1f}万"
        return str(num)

    def _build_message_chain(self, video: dict) -> MessageChain:
        chain = MessageChain()
        if video.get('pic'):
            chain.chain.append(Image.fromURL(video['pic']))
        chain.chain.append(Plain(f"🎬 {video['title']}"))
        chain.chain.append(Plain(f"👤 {video['author']}"))
        chain.chain.append(Plain(f"👍 {self._format_number(video['like'])}  ♥️ {self._format_number(video['coin'])}  ⭐ {self._format_number(video['favorite'])}"))
        chain.chain.append(Plain(f"💬 {self._format_number(video['danmaku'])}  📺 {self._format_number(video['play'])}"))
        chain.chain.append(Plain(f"🔗 {video['url']}"))
        return chain

    async def _send_video(self, target, video: dict):
        chain = self._build_message_chain(video)
        if isinstance(target, AstrMessageEvent):
            await target.send(target.chain_result(chain.chain))
        else:
            await self.context.send_message(target, chain)

    async def _push_to_current_event(self, event: AstrMessageEvent) -> bool:
        if not self.cookie:
            await event.send(event.chain_result([Plain("❌ 未登录B站账号，请先使用 /bili login 扫码登录")]))
            return False
        video = await self._fetch_random_video()
        if not video:
            await event.send(event.chain_result([Plain("❌ 暂时没有找到合适的视频，请稍后重试")]))
            return False
        await self._send_video(event, video)
        self.sent_videos[video['bvid']] = {'sent_at': datetime.now().isoformat(), 'title': video['title']}
        await self._save_sent_videos()
        return True

    # ==================== 定时推送 ====================
    def _start_monitor(self):
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("定时推送已启动")

    async def _monitor_loop(self):
        while self._running:
            try:
                await self._push_to_all_allowed_groups()
            except Exception as e:
                logger.exception("定时推送异常")
            await asyncio.sleep(self.scan_interval)

    async def _push_to_all_allowed_groups(self):
        if not self.cookie:
            return
        if not self.bound_groups:
            logger.debug("没有已绑定的群组，跳过定时推送")
            return
        video = await self._fetch_random_video()
        if not video:
            return
        chain = self._build_message_chain(video)
        allowed = [(gid, umo) for gid, umo in self.bound_groups.items() if self._is_group_allowed(gid)]
        if not allowed:
            return
        for i in range(0, len(allowed), BATCH_SEND_SIZE):
            batch = allowed[i:i+BATCH_SEND_SIZE]
            tasks = [self.context.send_message(umo, chain) for _, umo in batch]
            await asyncio.gather(*tasks)
            await asyncio.sleep(BATCH_SEND_INTERVAL)
        self.sent_videos[video['bvid']] = {'sent_at': datetime.now().isoformat(), 'title': video['title']}
        await self._save_sent_videos()

    def _is_group_allowed(self, group_id: str) -> bool:
        if self.use_whitelist_mode:
            return group_id in self.whitelist_groups
        else:
            return group_id not in self.blacklist_groups

    # ==================== 命令处理 ====================
    @filter.command("bili")
    async def bili_command(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            await self._cmd_help(event)
            return
        cmd = parts[1].lower()
        args = parts[2:]

        if cmd == "now":
            await self._cmd_now(event)
        elif cmd == "login":
            await self._cmd_login(event)
        elif cmd == "status":
            await self._cmd_status(event)
        elif cmd == "on":
            await self._cmd_on(event)
        elif cmd == "off":
            await self._cmd_off(event)
        elif cmd == "mode" and args:
            await self._cmd_mode(event, args[0])
        elif cmd == "interval" and args:
            await self._cmd_interval(event, args[0])
        elif cmd == "clear":
            await self._cmd_clear(event)
        elif cmd == "help":
            await self._cmd_help(event)
        else:
            await event.send(event.chain_result([Plain(f"未知子命令: {cmd}，请使用 bili help 查看帮助")]))

    # ==================== 关键词处理（无装饰器，自动接收所有非命令消息） ====================
    async def handle_event(self, event: AstrMessageEvent):
        """处理所有未被命令匹配的消息，用于关键词触发"""
        msg = event.message_str.strip()
        if not msg:
            return
        
        # 调试日志
        logger.info(f"[handle_event] 收到消息: {msg}")
        
        # 跳过命令消息（避免重复处理）
        if msg.lower().startswith(('bili', '/bili')):
            return

        # 获取群组ID（仅在群聊中处理关键词）
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else None
        if not group_id:
            logger.debug(f"私聊消息，忽略关键词触发: {msg}")
            return

        # 关键词匹配（支持部分匹配）
        matched = any(kw in msg for kw in self.keywords)
        if not matched:
            return

        logger.info(f"检测到关键词，群组: {group_id}, 消息: {msg}")

        # 自动记录群组（如果尚未记录）
        if group_id not in self.bound_groups:
            self.bound_groups[group_id] = event.unified_msg_origin
            await self._save_bound_groups()
            logger.info(f"自动记录群组: {group_id}")

        # 冷却检查
        now = time.time()
        last = self.group_cooldown.get(group_id, 0)
        if now - last < self.keyword_cooldown_seconds:
            remain = int(self.keyword_cooldown_seconds - (now - last))
            await event.send(event.chain_result([Plain(f"⏳ 冷却中，请 {remain} 秒后再试")]))
            return

        await event.send(event.chain_result([Plain("🎬 检测到关键词，正在搬运视频...")]))
        success = await self._push_to_current_event(event)
        if success:
            self.group_cooldown[group_id] = now
            await self._save_group_cooldown()

    # ---------- 子命令实现（普通协程，使用 event.send） ----------
    async def _cmd_help(self, event: AstrMessageEvent):
        help_text = (
            "📖 B站随机视频搬运插件使用帮助\n"
            "• bili now - 立即推送一个随机视频\n"
            "• bili on - 开启定时推送\n"
            "• bili off - 关闭定时推送\n"
            "• bili login - 扫码登录B站账号\n"
            "• bili status - 查看当前状态\n"
            "• bili mode whitelist/blacklist - 切换群聊模式\n"
            "• bili interval <秒> - 设置定时推送间隔(>=60)\n"
            "• bili clear - 清除当前群的关键词冷却\n"
            "• bili help - 显示本帮助\n"
            "关键词触发: 在群内发送“随机视频”、“来点视频”、“B站视频”即可触发推送（有冷却）"
        )
        await event.send(event.chain_result([Plain(help_text)]))

    async def _cmd_now(self, event: AstrMessageEvent):
        cooldown_key = str(event.message_obj.group_id) if event.message_obj.group_id else event.get_sender_id()
        now = time.time()
        last = self.manual_cooldown.get(cooldown_key, 0)
        if now - last < MANUAL_COOLDOWN_SECONDS:
            remain = int(MANUAL_COOLDOWN_SECONDS - (now - last))
            await event.send(event.chain_result([Plain(f"⏳ 手动调用冷却中，请 {remain} 秒后再试")]))
            return
        self.manual_cooldown[cooldown_key] = now
        await event.send(event.chain_result([Plain("🎬 正在搬运，请稍候...")]))
        await self._push_to_current_event(event)

    async def _cmd_login(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        if sender_id in self._login_tasks and not self._login_tasks[sender_id].done():
            await event.send(event.chain_result([Plain("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时")]))
            return

        await event.send(event.chain_result([Plain("🔄 正在生成B站登录二维码...")]))
        session = await self._get_http_session()
        try:
            async with session.get(BILI_QR_GENERATE_URL, headers=self._get_bili_headers()) as resp:
                data = await resp.json()
                if data.get("code") != 0:
                    await event.send(event.chain_result([Plain(f"❌ 获取二维码失败: {data.get('message')}")]))
                    return
                qrcode_url = data["data"]["url"]
                qrcode_key = data["data"]["qrcode_key"]
        except Exception as e:
            await event.send(event.chain_result([Plain(f"❌ 网络错误: {e}")]))
            return

        try:
            qr = qrcode.QRCode(box_size=10, border=4)
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            qr_path = self._data_dir / f"qrcode_{sender_id}.png"
            self._data_dir.mkdir(parents=True, exist_ok=True)
            img.save(str(qr_path), "PNG")
            await event.send(event.chain_result([Image.fromFileSystem(str(qr_path))]))
            await event.send(event.chain_result([Plain("📱 请使用B站App扫码登录，有效期3分钟")]))
        except Exception as e:
            await event.send(event.chain_result([Plain(f"❌ 生成二维码失败: {e}")]))
            return

        task = asyncio.create_task(self._poll_qr_login(sender_id, qrcode_key, qr_path, event))
        self._login_tasks[sender_id] = task

    async def _poll_qr_login(self, sender_id: str, qrcode_key: str, qr_path: Path, event: AstrMessageEvent):
        try:
            start = datetime.now()
            last_notified = None
            session = await self._get_http_session()
            while True:
                if (datetime.now() - start).total_seconds() > QR_CODE_EXPIRE_TIME:
                    await self._notify_user(event, "⏱️ 二维码已过期，请重新 /bili login")
                    break
                try:
                    async with session.get(BILI_QR_POLL_URL, params={"qrcode_key": qrcode_key}, headers=self._get_bili_headers()) as resp:
                        poll = await resp.json()
                        set_cookies = resp.headers.getall("Set-Cookie", [])
                        code = poll.get("data", {}).get("code", -1)
                        if code == QR_CODE_UNSCANNED:
                            pass
                        elif code == QR_CODE_SCANNED:
                            if last_notified != QR_CODE_SCANNED:
                                await self._notify_user(event, "✅ 已扫码，请在手机上确认登录")
                                last_notified = QR_CODE_SCANNED
                        elif code == QR_CODE_EXPIRED:
                            await self._notify_user(event, "⏱️ 二维码已过期，请重新 /bili login")
                            break
                        elif code == QR_CODE_SUCCESS:
                            cookie_dict = {}
                            for header in set_cookies:
                                if "=" in header:
                                    part = header.split(";")[0].strip()
                                    if "=" in part:
                                        k, v = part.split("=", 1)
                                        cookie_dict[k] = v
                            if not cookie_dict:
                                await self._notify_user(event, "❌ 登录成功但未获取到Cookie，请重试")
                                break
                            cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
                            async with self._cookie_lock:
                                self.cookie = cookie_str
                                await self._save_cookie()
                            nav = await self._fetch_json("https://api.bilibili.com/x/web-interface/nav", {}, need_sign=False)
                            if nav and nav.get('data', {}).get('isLogin'):
                                uname = nav['data'].get('uname', '未知')
                                await self._notify_user(event, f"✅ 登录成功！\n用户: {uname}\nCookie已自动保存")
                                if self.auto_start and not self._running:
                                    self._start_monitor()
                            else:
                                await self._notify_user(event, "⚠️ Cookie保存成功但验证失败，请稍后手动检查")
                            break
                        else:
                            logger.warning(f"未知扫码状态: {code}")
                except Exception as e:
                    logger.warning(f"轮询出错: {e}")
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("扫码轮询异常")
        finally:
            self._login_tasks.pop(sender_id, None)
            try:
                qr_path.unlink()
            except:
                pass

    async def _notify_user(self, event: AstrMessageEvent, msg: str):
        try:
            await event.send(event.chain_result([Plain(msg)]))
        except Exception as e:
            logger.error(f"通知用户失败: {e}")

    async def _cmd_status(self, event: AstrMessageEvent):
        lines = [
            "=== B站随机视频搬运状态 ===",
            f"定时推送: {'✅ 运行中' if self._running else '❌ 已停止'}",
            f"推送间隔: {self.scan_interval} 秒",
            f"关键词冷却: {self.keyword_cooldown_seconds} 秒",
            f"已发送视频: {len(self.sent_videos)} 个",
            f"已绑定群: {len(self.bound_groups)} 个",
            f"Cookie状态: {'✅ 已配置' if self.cookie else '❌ 未配置'}",
            f"群模式: {'白名单' if self.use_whitelist_mode else '黑名单'}",
            f"关键词列表: {', '.join(self.keywords)}"
        ]
        await event.send(event.chain_result([Plain("\n".join(lines))]))

    async def _cmd_on(self, event: AstrMessageEvent):
        if self._running:
            await event.send(event.chain_result([Plain("定时推送已在运行中")]))
        else:
            self._start_monitor()
            await event.send(event.chain_result([Plain("✅ 已开启定时推送")]))

    async def _cmd_off(self, event: AstrMessageEvent):
        if not self._running:
            await event.send(event.chain_result([Plain("定时推送已关闭")]))
        else:
            self._running = False
            if self._task:
                self._task.cancel()
                self._task = None
            await event.send(event.chain_result([Plain("✅ 已关闭定时推送")]))

    async def _cmd_mode(self, event: AstrMessageEvent, mode: str):
        if mode == "whitelist":
            self.use_whitelist_mode = True
            await event.send(event.chain_result([Plain("✅ 已切换到白名单模式")]))
        elif mode == "blacklist":
            self.use_whitelist_mode = False
            await event.send(event.chain_result([Plain("✅ 已切换到黑名单模式")]))
        else:
            await event.send(event.chain_result([Plain("模式必须是 whitelist 或 blacklist")]))

    async def _cmd_interval(self, event: AstrMessageEvent, sec_str: str):
        try:
            sec = max(60, int(sec_str))
            self.scan_interval = sec
            await event.send(event.chain_result([Plain(f"✅ 定时推送间隔已设置为 {sec} 秒")]))
        except:
            await event.send(event.chain_result([Plain("请输入有效的数字（秒）")]))

    async def _cmd_clear(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else None
        if not group_id:
            await event.send(event.chain_result([Plain("该命令仅支持群聊")]))
            return
        if group_id in self.group_cooldown:
            del self.group_cooldown[group_id]
            await self._save_group_cooldown()
            await event.send(event.chain_result([Plain("✅ 已清除本群的关键词冷却")]))
        else:
            await event.send(event.chain_result([Plain("本群当前没有冷却记录")]))
