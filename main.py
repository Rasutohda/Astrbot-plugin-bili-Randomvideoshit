# -*- coding: utf-8 -*-
import asyncio
import random
import re
import json
import os
import time
import hashlib
import aiohttp
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta
from enum import IntEnum
from contextlib import suppress
from http.cookies import SimpleCookie

import qrcode

from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image

# ---------- 数据目录 ----------
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"

# ---------- B站API端点 ----------
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# ---------- 二维码状态码枚举 ----------
class QRCodeStatus(IntEnum):
    SUCCESS = 0
    UNSCANNED = 86101
    SCANNED = 86090
    EXPIRED = 86038

QR_CODE_EXPIRE_TIME = 180
POLL_INTERVAL = 5
MAX_RETRIES = 3
SENT_VIDEOS_RETENTION_DAYS = 7
WBI_KEY_CACHE_TTL = 3600
MANUAL_COOLDOWN_SECONDS = 60
BATCH_SEND_SIZE = 5
BATCH_SEND_INTERVAL = 1

# ---------- WBI签名 ----------
class WbiHelper:
    _cache = {"keys": None, "expire_at": 0}
    _lock = asyncio.Lock()

    @classmethod
    async def get_keys(cls, session: aiohttp.ClientSession) -> Tuple[str, str]:
        async with cls._lock:
            now = time.time()
            if cls._cache["keys"] and now < cls._cache["expire_at"]:
                return cls._cache["keys"]
            
            headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
            try:
                async with session.get('https://api.bilibili.com/x/web-interface/nav', headers=headers) as resp:
                    data = await resp.json()
                    if data.get('code') != 0:
                        raise Exception(f"获取WBI密钥失败: {data.get('message')}")
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

# ---------- 辅助函数 ----------
def format_number(num: int) -> str:
    if num >= 1_0000_0000:
        return f"{num/1_0000_0000:.1f}亿"
    elif num >= 1_0000:
        return f"{num/1_0000:.1f}万"
    return str(num)

def atomic_write_json(path: Path, data: dict):
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp_path.replace(path)

# ---------- 插件主类 ----------
class BiliRandomVideo(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = self._merge_config(config or {})
        self.running = False
        self.task = None
        self.session = None
        self.cookie = self._load_cookie()
        self.bound_groups: Dict[str, str] = self._load_json("bound_groups.json", {})
        self.sent_videos: Dict[str, dict] = self._load_json("sent_videos.json", {})
        self.group_cooldown: Dict[str, float] = self._load_json("group_cooldown.json", {})
        self.manual_cooldown: Dict[str, float] = {}
        self._login_tasks: Dict[str, asyncio.Task] = {}

    # ---------- 配置管理 ----------
    def _merge_config(self, user_config: dict) -> dict:
        default = {
            'auto_start': True,
            'scan_interval': 3600,
            'keyword_cooldown_seconds': 600,
            'use_whitelist_mode': False,
            'whitelist_groups': [],
            'blacklist_groups': [],
            'keywords': ["随机视频", "来点视频", "B站视频"]
        }
        merged = {**default, **user_config}
        if not CONFIG_FILE.exists():
            atomic_write_json(CONFIG_FILE, merged)
        return merged

    def _save_config(self):
        atomic_write_json(CONFIG_FILE, self.config)

    # ---------- 持久化 ----------
    def _load_json(self, filename: str, default: dict) -> dict:
        path = DATA_DIR / filename
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except:
            return default

    def _save_json(self, filename: str, data: dict):
        atomic_write_json(DATA_DIR / filename, data)

    def _load_cookie(self) -> str:
        try:
            return (DATA_DIR / "cookie.txt").read_text(encoding='utf-8').strip()
        except:
            return ""

    def _save_cookie(self, cookie: str):
        (DATA_DIR / "cookie.txt").write_text(cookie, encoding='utf-8')

    # ---------- 初始化/卸载 ----------
    async def initialize(self):
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self._clean_expired_cooldown()
        self._clean_old_sent_videos()
        self._clean_qr_images()
        if self.config.get('auto_start', True):
            self.running = True
            self.task = asyncio.create_task(self._timer_loop())
            logger.info("✅ B站随机视频搬运：定时任务已启动")

    async def terminate(self):
        self.running = False
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.session:
            await self.session.close()
        for task in self._login_tasks.values():
            task.cancel()
        self._clean_qr_images()
        logger.info("B站随机视频搬运插件已卸载")

    # ---------- 后台定时循环 ----------
    async def _timer_loop(self):
        interval = max(60, self.config.get('scan_interval', 3600))
        while self.running:
            try:
                await self._push_to_all_allowed_groups()
            except Exception as e:
                logger.exception("定时推送过程中发生异常")
            await asyncio.sleep(interval)

    # ---------- 网络请求 ----------
    async def _fetch_json(self, url: str, params: dict, need_sign: bool = False, retry: int = MAX_RETRIES) -> Optional[dict]:
        for attempt in range(retry):
            try:
                if need_sign:
                    img_key, sub_key = await WbiHelper.get_keys(self.session)
                    params = WbiHelper.sign(params, img_key, sub_key)
                headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
                if self.cookie:
                    headers['Cookie'] = self.cookie
                async with self.session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
                    if data.get('code') == 0:
                        return data
                    elif data.get('code') == -509:
                        wait = 2 ** attempt + random.uniform(0, 1)
                        logger.warning(f"API限流(-509)，等待 {wait:.1f} 秒后重试 ({attempt+1}/{retry})")
                        await asyncio.sleep(wait)
                        continue
                    else:
                        logger.warning(f"API返回非0: {url}, code={data.get('code')}, msg={data.get('message')}")
            except asyncio.TimeoutError:
                logger.warning(f"请求超时 ({attempt+1}/{retry}): {url}")
            except Exception as e:
                logger.warning(f"请求失败 ({attempt+1}/{retry}): {url}, 错误: {e}")
            if attempt < retry - 1:
                await asyncio.sleep(2 ** attempt + random.uniform(0, 0.5))
        return None

    # ---------- 核心视频获取 ----------
    async def _fetch_random_video(self) -> Optional[dict]:
        params = {"series_id": 0}
        data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular/series/one", params, need_sign=True)
        video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular", {"pn": 1, "ps": 50}, need_sign=True)
            video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            return None

        candidate_videos = [v for v in video_list if v.get('bvid') not in self.sent_videos]
        if not candidate_videos:
            logger.warning("所有热门视频都已推送过，等待新视频")
            return None

        for _ in range(5):
            video = random.choice(candidate_videos)
            detail = await self._fetch_json("https://api.bilibili.com/x/web-interface/view",
                                            {"bvid": video['bvid']}, need_sign=True)
            if not detail:
                continue
            info = detail['data']
            stats = info.get('stat', {})
            return {
                'bvid': info['bvid'],
                'title': info.get('title'),
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

    def _clean_old_sent_videos(self):
        cutoff = datetime.now() - timedelta(days=SENT_VIDEOS_RETENTION_DAYS)
        to_delete = []
        for bvid, info in self.sent_videos.items():
            try:
                sent_at = datetime.fromisoformat(info.get('sent_at', '2000-01-01'))
                if sent_at < cutoff:
                    to_delete.append(bvid)
            except:
                to_delete.append(bvid)
        for bvid in to_delete:
            del self.sent_videos[bvid]
        if to_delete:
            self._save_json("sent_videos.json", self.sent_videos)
            logger.info(f"清理了 {len(to_delete)} 条过期的视频发送记录")

    def _clean_expired_cooldown(self):
        now = time.time()
        expired = [gid for gid, ts in self.group_cooldown.items() if now - ts > 86400]
        if expired:
            for gid in expired:
                del self.group_cooldown[gid]
            self._save_json("group_cooldown.json", self.group_cooldown)
            logger.info(f"清理了 {len(expired)} 个过期的冷却记录")

    def _clean_qr_images(self):
        for p in DATA_DIR.glob("qrcode_*.png"):
            p.unlink()

    # ---------- 消息构建与发送 ----------
    def _build_message_chain(self, video: dict) -> MessageChain:
        chain = MessageChain()
        if video.get('pic'):
            chain.chain.append(Image.fromURL(video['pic']))
        chain.chain.append(Plain(f"🎬 {video['title']}"))
        chain.chain.append(Plain(f"👤 {video['author']}"))
        chain.chain.append(Plain(f"👍 {format_number(video['like'])}  ♥️ {format_number(video['coin'])}  ⭐ {format_number(video['favorite'])}"))
        chain.chain.append(Plain(f"💬 {format_number(video['danmaku'])}  📺 {format_number(video['play'])}"))
        chain.chain.append(Plain(f"🔗 {video['url']}"))
        return chain

    async def _send_to_target(self, target, video: dict) -> bool:
        """发送视频到目标，返回是否成功"""
        chain = self._build_message_chain(video)
        try:
            if isinstance(target, AstrMessageEvent):
                await target.send(target.chain_result(chain.chain))
            else:
                await self.context.send_message(target, chain)
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False

    async def _push_to_target_group(self, event: AstrMessageEvent) -> bool:
        """通过事件推送视频到当前群/私聊，返回是否成功"""
        video = await self._fetch_random_video()
        if not video:
            await event.send(event.chain_result([Plain("❌ 没找到合适的视频")]))
            return False
        success = await self._send_to_target(event, video)
        if success:
            self._record_sent_video(video)
        return success

    async def _push_to_all_allowed_groups(self):
        video = await self._fetch_random_video()
        if not video:
            return
        chain = self._build_message_chain(video)
        allowed_groups = [(gid, umo) for gid, umo in self.bound_groups.items() if self._is_allowed(gid)]
        if not allowed_groups:
            return

        for i in range(0, len(allowed_groups), BATCH_SEND_SIZE):
            batch = allowed_groups[i:i+BATCH_SEND_SIZE]
            tasks = [self.context.send_message(umo, chain) for _, umo in batch]
            await asyncio.gather(*tasks)
            if i + BATCH_SEND_SIZE < len(allowed_groups):
                await asyncio.sleep(BATCH_SEND_INTERVAL)
        self._record_sent_video(video)

    def _record_sent_video(self, video: dict):
        bvid = video['bvid']
        self.sent_videos[bvid] = {
            'sent_at': datetime.now().isoformat(),
            'title': video['title']
        }
        self._save_json("sent_videos.json", self.sent_videos)

    def _is_allowed(self, group_id: str) -> bool:
        mode = self.config.get('use_whitelist_mode', False)
        whitelist = self.config.get('whitelist_groups', [])
        blacklist = self.config.get('blacklist_groups', [])
        return group_id in whitelist if mode else group_id not in blacklist

    async def _record_group(self, group_id: str, umo: str):
        if group_id not in self.bound_groups:
            self.bound_groups[group_id] = umo
            self._save_json("bound_groups.json", self.bound_groups)
            logger.info(f"📌 自动记录新群: {group_id}")

    # ---------- 扫码登录 ----------
    async def _generate_qrcode_image(self, url: str, unique_id: str) -> Optional[Path]:
        try:
            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            qr_path = DATA_DIR / f"qrcode_{unique_id}.png"
            img.save(str(qr_path), "PNG")
            return qr_path
        except Exception as e:
            logger.error(f"生成二维码失败: {e}")
            return None

    async def _poll_qr_login(self, sender_id: str, qrcode_key: str, qr_path: Path, origin: str):
        start_time = datetime.now()
        last_notified_status = None
        try:
            while True:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= QR_CODE_EXPIRE_TIME:
                    break

                try:
                    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
                    async with self.session.get(BILI_QR_POLL_URL, params={"qrcode_key": qrcode_key}, headers=headers) as resp:
                        poll_data = await resp.json()
                        set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                except Exception:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                code = poll_data.get("data", {}).get("code", -1)

                if code == QRCodeStatus.SUCCESS:
                    cookie_jar = SimpleCookie()
                    for header in set_cookie_headers:
                        cookie_jar.load(header)
                    sessdata = cookie_jar.get('SESSDATA', '').value if 'SESSDATA' in cookie_jar else ''
                    bili_jct = cookie_jar.get('bili_jct', '').value if 'bili_jct' in cookie_jar else ''
                    buvid3 = cookie_jar.get('buvid3', '').value if 'buvid3' in cookie_jar else ''
                    if sessdata and bili_jct:
                        cookie_str = f"SESSDATA={sessdata}; bili_jct={bili_jct}; buvid3={buvid3}"
                        self.cookie = cookie_str
                        self._save_cookie(cookie_str)
                        await self._notify_user(origin, "✅ 登录成功！Cookie已自动保存")
                        break
                    else:
                        logger.warning("登录成功但Cookie提取失败")
                        await self._notify_user(origin, "⚠️ 登录成功但无法提取完整Cookie，请手动检查")
                        break

                elif code == QRCodeStatus.SCANNED:
                    if last_notified_status != QRCodeStatus.SCANNED:
                        await self._notify_user(origin, "✅ 已扫码\n请在手机上点击「确认登录」完成授权")
                        last_notified_status = QRCodeStatus.SCANNED

                elif code == QRCodeStatus.EXPIRED:
                    await self._notify_user(origin, "⏱️ 二维码已过期\n请重新发送 bili login 获取新二维码")
                    break

                await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("扫码轮询出错")
            await self._notify_user(origin, "❌ 扫码登录过程出错，请重新尝试 bili login")
        finally:
            if qr_path and qr_path.exists():
                qr_path.unlink()
            if sender_id in self._login_tasks:
                del self._login_tasks[sender_id]

    async def _notify_user(self, origin: str, message: str):
        try:
            await self.context.send_message(origin, MessageChain([Plain(message)]))
        except Exception as e:
            logger.error(f"发送消息给 {origin} 失败: {e}")

    # ---------- 命令处理（使用装饰器）----------
    @filter.command("bili")
    async def bili_command(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        if not msg.startswith(('bili', '/bili')):
            return
        if msg.startswith('/bili'):
            msg = msg[1:]
        parts = msg.split()
        cmd = parts[1] if len(parts) > 1 else ''
        args = parts[2:] if len(parts) > 2 else []
        
        if cmd == 'now':
            await self._cmd_now(event)
        elif cmd == 'on':
            await self._cmd_on(event)
        elif cmd == 'off':
            await self._cmd_off(event)
        elif cmd == 'login':
            await self._cmd_login(event)
        elif cmd == 'status':
            await self._cmd_status(event)
        elif cmd == 'mode' and args:
            await self._cmd_mode(event, args[0])
        elif cmd == 'interval' and args:
            await self._cmd_interval(event, args[0])
        elif cmd == 'clear':
            await self._cmd_clear(event)
        elif cmd == 'help':
            await self._cmd_help(event)
        else:
            yield event.chain_result([Plain("可用命令: bili now, on, off, login, status, mode, interval, clear, help")])

    # ---------- 关键词触发 ----------
    @filter.on_message(priority=10)
    async def on_keyword(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        if not msg:
            return
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else None
        if group_id is None:
            return
        if group_id not in self.bound_groups:
            await self._record_group(group_id, event.unified_msg_origin)
        keywords = self.config.get('keywords', [])
        triggered = any(kw in msg for kw in keywords)
        if triggered:
            now = time.time()
            cooldown = self.config.get('keyword_cooldown_seconds', 600)
            last = self.group_cooldown.get(group_id, 0)
            if now - last >= cooldown:
                yield event.chain_result([Plain("🎬 检测到关键词，正在搬运视频...")])
                success = await self._push_to_target_group(event)
                if success:
                    self.group_cooldown[group_id] = now
                    self._save_json("group_cooldown.json", self.group_cooldown)
            else:
                remain = int(cooldown - (now - last))
                yield event.chain_result([Plain(f"⏳ 冷却中，请 {remain} 秒后再试")])

    # ---------- 命令具体实现（混合生成器与普通异步函数）----------
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
        yield event.chain_result([Plain(help_text)])

    async def _cmd_now(self, event: AstrMessageEvent):
        cooldown_key = str(event.message_obj.group_id) if event.message_obj.group_id else event.get_sender_id()
        now = time.time()
        last = self.manual_cooldown.get(cooldown_key, 0)
        if now - last < MANUAL_COOLDOWN_SECONDS:
            remain = int(MANUAL_COOLDOWN_SECONDS - (now - last))
            yield event.chain_result([Plain(f"⏳ 手动调用冷却中，请 {remain} 秒后再试")])
            return
        self.manual_cooldown[cooldown_key] = now
        yield event.chain_result([Plain("🎬 正在搬石，请稍候...")])
        await self._push_to_target_group(event)

    async def _cmd_on(self, event: AstrMessageEvent):
        if self.running:
            yield event.chain_result([Plain("定时推送已在运行中")])
            return
        self.running = True
        self.task = asyncio.create_task(self._timer_loop())
        yield event.chain_result([Plain("✅ 已开启定时推送")])

    async def _cmd_off(self, event: AstrMessageEvent):
        if not self.running:
            yield event.chain_result([Plain("定时推送已关闭")])
            return
        self.running = False
        if self.task:
            self.task.cancel()
            self.task = None
        yield event.chain_result([Plain("✅ 已关闭定时推送")])

    async def _cmd_login(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        origin = event.unified_msg_origin

        if sender_id in self._login_tasks and not self._login_tasks[sender_id].done():
            yield event.chain_result([Plain("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时")])
            return

        yield event.chain_result([Plain("🔐 正在生成登录二维码...")])

        try:
            async with self.session.post(BILI_QR_GENERATE_URL, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}) as resp:
                data = await resp.json()
                if data.get('code') != 0:
                    yield event.chain_result([Plain(f"❌ 获取二维码失败: {data.get('message', '未知错误')}")])
                    return
                qrcode_url = data["data"]["url"]
                qrcode_key = data["data"]["qrcode_key"]

            if not qrcode_key:
                yield event.chain_result([Plain("❌ 获取qrcode_key失败")])
                return

            unique_id = f"{sender_id}_{uuid.uuid4().hex[:8]}"
            qr_path = await self._generate_qrcode_image(qrcode_url, unique_id)
            if not qr_path:
                yield event.chain_result([Plain("❌ 二维码生成失败，请检查是否已安装 qrcode 库")])
                return

            yield event.chain_result([Image.fromFileSystem(str(qr_path))])
            yield event.chain_result([Plain("🔗 请使用B站手机App扫码登录，有效期3分钟")])

            task = asyncio.create_task(self._poll_qr_login(sender_id, qrcode_key, qr_path, origin))
            self._login_tasks[sender_id] = task
            task.add_done_callback(lambda t: self._login_tasks.pop(sender_id, None))

        except Exception as e:
            logger.exception("扫码登录出错")
            yield event.chain_result([Plain(f"❌ 生成二维码失败: {e}")])

    async def _cmd_status(self, event: AstrMessageEvent):
        lines = [
            "=== B站随机视频搬运状态 ===",
            f"定时任务: {'✅ 运行中' if self.running else '❌ 已停止'}",
            f"推送间隔: {self.config.get('scan_interval', 3600)} 秒",
            f"关键词冷却: {self.config.get('keyword_cooldown_seconds', 600)} 秒",
            f"已发送视频: {len(self.sent_videos)} 个",
            f"已绑定群: {len(self.bound_groups)} 个",
            f"Cookie状态: {'✅ 已配置' if self.cookie else '❌ 未配置'}",
            f"群模式: {'白名单' if self.config.get('use_whitelist_mode', False) else '黑名单'}",
            f"关键词列表: {', '.join(self.config.get('keywords', []))}"
        ]
        yield event.chain_result([Plain("\n".join(lines))])

    async def _cmd_mode(self, event: AstrMessageEvent, mode: str):
        if mode == 'whitelist':
            self.config['use_whitelist_mode'] = True
        elif mode == 'blacklist':
            self.config['use_whitelist_mode'] = False
        else:
            yield event.chain_result([Plain("模式只能是 whitelist 或 blacklist")])
            return
        self._save_config()
        yield event.chain_result([Plain(f"✅ 已切换到 {'白名单' if self.config['use_whitelist_mode'] else '黑名单'} 模式")])

    async def _cmd_interval(self, event: AstrMessageEvent, sec_str: str):
        try:
            sec = max(60, int(sec_str))
            self.config['scan_interval'] = sec
            self._save_config()
            yield event.chain_result([Plain(f"✅ 已设置定时推送间隔为 {sec} 秒")])
        except ValueError:
            yield event.chain_result([Plain("请输入有效的数字")])

    async def _cmd_clear(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else None
        if not group_id:
            yield event.chain_result([Plain("❌ 该命令仅支持群聊")])
            return
        if group_id in self.group_cooldown:
            del self.group_cooldown[group_id]
            self._save_json("group_cooldown.json", self.group_cooldown)
            yield event.chain_result([Plain("✅ 已清除本群的关键词冷却")])
        else:
            yield event.chain_result([Plain("本群当前没有冷却记录")])
