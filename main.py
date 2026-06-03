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
from datetime import datetime
from enum import IntEnum
from contextlib import suppress

import qrcode

from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent
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
SENT_TITLES_MAX_ITEMS = 2000
WBI_KEY_CACHE_TTL = 3600  # 缓存WBI密钥1小时

# ---------- WBI签名（带缓存）----------
class WbiHelper:
    _cache = {"keys": None, "expire_at": 0}

    @classmethod
    async def get_keys(cls, session: aiohttp.ClientSession) -> Tuple[str, str]:
        now = time.time()
        if cls._cache["keys"] and now < cls._cache["expire_at"]:
            return cls._cache["keys"]
        
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
        async with session.get('https://api.bilibili.com/x/web-interface/nav', headers=headers) as resp:
            data = await resp.json()
            if data.get('code') != 0:
                raise Exception("获取WBI密钥失败")
            img_url = data['data']['wbi_img']['img_url']
            sub_url = data['data']['wbi_img']['sub_url']
            img_key = re.search(r'/([^/]+)\.png', img_url).group(1)
            sub_key = re.search(r'/([^/]+)\.png', sub_url).group(1)
            cls._cache["keys"] = (img_key, sub_key)
            cls._cache["expire_at"] = now + WBI_KEY_CACHE_TTL
            return img_key, sub_key

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

def normalize_title(title: str) -> str:
    return re.sub(r'\s+', ' ', title).strip().lower()

def atomic_write_json(path: Path, data: dict):
    """原子写入JSON文件"""
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp_path.replace(path)

# ---------- 插件主类 ----------
@register("astrbot_plugin_bili_Randomvideoshit", "Rasutohda",
          "B站随机视频搬运｜扫码登录｜关键词触发｜定时推送", "3.4.1",
          "https://github.com/Rasutohda/astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = self._merge_config(config or {})
        self.running = False
        self.task = None
        self.session = None
        self.cookie = self._load_cookie()
        self.bound_groups: Dict[str, str] = self._load_json("bound_groups.json", {})
        self.sent_titles: Dict[str, dict] = self._load_json("sent_titles.json", {})
        self.group_cooldown: Dict[str, float] = self._load_json("group_cooldown.json", {})
        self.manual_cooldown = 60
        self.last_manual_time = 0.0
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
        # 保存默认配置到文件
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
        self.session = aiohttp.ClientSession()
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
        logger.info("B站随机视频搬运插件已卸载")

    # ---------- 后台定时循环 ----------
    async def _timer_loop(self):
        interval = max(60, self.config.get('scan_interval', 3600))
        while self.running:
            await self._push_to_all_allowed_groups()
            await asyncio.sleep(interval)

    # ---------- 网络请求（带重试）----------
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
                    else:
                        logger.warning(f"API返回非0: {url}, code={data.get('code')}, msg={data.get('message')}")
            except Exception as e:
                logger.warning(f"请求失败 ({attempt+1}/{retry}): {url}, 错误: {e}")
                if attempt < retry - 1:
                    await asyncio.sleep(2 ** attempt)
        return None

    # ---------- 核心视频获取 ----------
    async def _fetch_random_video(self) -> Optional[dict]:
        # 先尝试获取热门系列
        params = {"series_id": 0}
        data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular/series/one", params, need_sign=True)
        video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular", {"pn": 1, "ps": 30}, need_sign=True)
            video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            return None

        # 清理过期的 sent_titles
        self._clean_sent_titles()

        for _ in range(10):
            video = random.choice(video_list)
            title = video.get('title', '')
            if not title or normalize_title(title) in self.sent_titles:
                continue
            detail = await self._fetch_json("https://api.bilibili.com/x/web-interface/view",
                                            {"bvid": video['bvid']}, need_sign=True)
            if not detail:
                continue
            info = detail['data']
            stats = info.get('stat', {})
            return {
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

    def _clean_sent_titles(self):
        """清理过期的已推送标题记录，保留最近N条"""
        if len(self.sent_titles) > SENT_TITLES_MAX_ITEMS:
            # 按时间排序，删除旧的
            sorted_items = sorted(self.sent_titles.items(), key=lambda x: x[1].get('sent_at', ''))
            to_delete = sorted_items[:len(sorted_items) - SENT_TITLES_MAX_ITEMS]
            for key, _ in to_delete:
                del self.sent_titles[key]
            self._save_json("sent_titles.json", self.sent_titles)

    # ---------- 消息构建与发送 ----------
    def _build_message_chain(self, video: dict) -> list:
        text = (
            f"🎬 {video['title']}\n"
            f"👤 {video['author']}\n"
            f"👍 {format_number(video['like'])}  ♥️ {format_number(video['coin'])}  ⭐ {format_number(video['favorite'])}\n"
            f"💬 {format_number(video['danmaku'])}  📺 {format_number(video['play'])}\n"
            f"🔗 {video['url']}"
        )
        return [Image.fromURL(video['pic']), Plain(text)] if video.get('pic') else [Plain(text)]

    async def _send_to_target(self, target, video: dict):
        """target可以是AstrMessageEvent或unified_msg_origin字符串"""
        chain = self._build_message_chain(video)
        if isinstance(target, AstrMessageEvent):
            await target.send(target.make_result().message(chain))
        else:
            await self.context.send_message(target, chain)

    async def _push_to_target_group(self, event: AstrMessageEvent, group_id: str) -> bool:
        """通过事件推送视频到当前群/私聊"""
        video = await self._fetch_random_video()
        if not video:
            await event.send(event.plain_result("❌ 没找到合适的视频"))
            return False
        await self._send_to_target(event, video)
        self._record_sent_title(video['title'])
        return True

    async def _push_to_all_allowed_groups(self):
        """定时推送到所有允许的群聊"""
        video = await self._fetch_random_video()
        if not video:
            return
        chain = self._build_message_chain(video)
        tasks = []
        for gid, umo in self.bound_groups.items():
            if self._is_allowed(gid):
                tasks.append(self.context.send_message(umo, chain))
        if tasks:
            await asyncio.gather(*tasks)
        self._record_sent_title(video['title'])

    def _record_sent_title(self, title: str):
        norm = normalize_title(title)
        self.sent_titles[norm] = {'sent_at': datetime.now().isoformat()}
        self._save_json("sent_titles.json", self.sent_titles)

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
                    cookie_dict = {}
                    for header in set_cookie_headers:
                        cookie_part = header.split(';')[0].strip()
                        if '=' in cookie_part:
                            k, v = cookie_part.split('=', 1)
                            cookie_dict[k] = v
                    sessdata = cookie_dict.get('SESSDATA', '')
                    bili_jct = cookie_dict.get('bili_jct', '')
                    buvid3 = cookie_dict.get('buvid3', '')
                    if sessdata and bili_jct:
                        cookie_str = f"SESSDATA={sessdata}; bili_jct={bili_jct}; buvid3={buvid3}"
                        self.cookie = cookie_str
                        self._save_cookie(cookie_str)
                        await self._notify_user(origin, "✅ 登录成功！Cookie已自动保存")
                        break
                    else:
                        logger.warning("登录成功但Cookie提取失败")

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
        """origin为unified_msg_origin字符串"""
        try:
            await self.context.send_message(origin, message)
        except Exception as e:
            logger.error(f"发送消息给 {origin} 失败: {e}")

    # ---------- 核心事件处理 ----------
    async def handle_event(self, event) -> bool:
        if not isinstance(event, AstrMessageEvent):
            return True

        msg = event.message_str.strip()
        if not msg:
            return True

        # 区分群聊和私聊
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else None
        is_group = group_id is not None

        # 记录新群
        if is_group and group_id not in self.bound_groups:
            await self._record_group(group_id, event.unified_msg_origin)

        # 命令处理
        if msg.startswith('bili ') or msg.startswith('/bili ') or msg in ('bili', '/bili'):
            if msg.startswith('/'):
                msg = msg[1:]
            parts = msg.split()
            cmd = parts[1] if len(parts) > 1 else ''
            await self._handle_command(event, cmd, parts[2:] if len(parts) > 2 else [])
            return False

        # 关键词触发（仅群聊）
        if is_group:
            keywords = self.config.get('keywords', [])
            if any(kw in msg for kw in keywords):
                now = time.time()
                cooldown = self.config.get('keyword_cooldown_seconds', 600)
                last = self.group_cooldown.get(group_id, 0)
                if now - last >= cooldown:
                    await event.send(event.plain_result("🎬 检测到关键词，正在搬运视频..."))
                    success = await self._push_to_target_group(event, group_id)
                    if success:
                        self.group_cooldown[group_id] = now
                        self._save_json("group_cooldown.json", self.group_cooldown)
                else:
                    remain = int(cooldown - (now - last))
                    await event.send(event.plain_result(f"⏳ 冷却中，请 {remain} 秒后再试"))
                return False

        return True

    async def _handle_command(self, event: AstrMessageEvent, cmd: str, args: List[str]):
        """分发命令"""
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
        elif cmd == 'help':
            await self._cmd_help(event)
        else:
            await event.send(event.plain_result("可用命令: bili now, on, off, login, status, mode, interval, help"))

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
            "• bili help - 显示本帮助\n"
            "关键词触发: 在群内发送“随机视频”、“来点视频”、“B站视频”即可触发推送（有冷却）"
        )
        await event.send(event.plain_result(help_text))

    async def _cmd_now(self, event: AstrMessageEvent):
        now = time.time()
        if now - self.last_manual_time < self.manual_cooldown:
            remain = int(self.manual_cooldown - (now - self.last_manual_time))
            await event.send(event.plain_result(f"⏳ 冷却中，请 {remain} 秒后再试"))
            return
        self.last_manual_time = now
        await event.send(event.plain_result("🎬 正在搬石，请稍候..."))
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "private"
        await self._push_to_target_group(event, group_id)

    async def _cmd_on(self, event: AstrMessageEvent):
        if self.running:
            await event.send(event.plain_result("定时推送已在运行中"))
            return
        self.running = True
        self.task = asyncio.create_task(self._timer_loop())
        await event.send(event.plain_result("✅ 已开启定时推送"))

    async def _cmd_off(self, event: AstrMessageEvent):
        if not self.running:
            await event.send(event.plain_result("定时推送已关闭"))
            return
        self.running = False
        if self.task:
            self.task.cancel()
            self.task = None
        await event.send(event.plain_result("✅ 已关闭定时推送"))

    async def _cmd_login(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        origin = event.unified_msg_origin

        if sender_id in self._login_tasks and not self._login_tasks[sender_id].done():
            await event.send(event.plain_result("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时"))
            return

        await event.send(event.plain_result("🔐 正在生成登录二维码..."))

        try:
            async with self.session.post(BILI_QR_GENERATE_URL, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}) as resp:
                data = await resp.json()
                if data.get('code') != 0:
                    await event.send(event.plain_result(f"❌ 获取二维码失败: {data.get('message', '未知错误')}"))
                    return
                qrcode_url = data["data"]["url"]
                qrcode_key = data["data"]["qrcode_key"]

            if not qrcode_key:
                await event.send(event.plain_result("❌ 获取qrcode_key失败"))
                return

            unique_id = f"{sender_id}_{uuid.uuid4().hex[:8]}"
            qr_path = await self._generate_qrcode_image(qrcode_url, unique_id)
            if not qr_path:
                await event.send(event.plain_result("❌ 二维码生成失败，请检查是否已安装 qrcode 库"))
                return

            await event.send(event.make_result().file_image(str(qr_path)))
            await event.send(event.plain_result("🔗 请使用B站手机App扫码登录，有效期3分钟"))

            task = asyncio.create_task(self._poll_qr_login(sender_id, qrcode_key, qr_path, origin))
            self._login_tasks[sender_id] = task
            # 任务完成后自动清理
            task.add_done_callback(lambda t: self._login_tasks.pop(sender_id, None))

        except Exception as e:
            logger.exception("扫码登录出错")
            await event.send(event.plain_result(f"❌ 生成二维码失败: {e}"))

    async def _cmd_status(self, event: AstrMessageEvent):
        lines = [
            "=== B站随机视频搬运状态 ===",
            f"定时任务: {'✅ 运行中' if self.running else '❌ 已停止'}",
            f"推送间隔: {self.config.get('scan_interval', 3600)} 秒",
            f"关键词冷却: {self.config.get('keyword_cooldown_seconds', 600)} 秒",
            f"已记录标题: {len(self.sent_titles)} 个",
            f"已绑定群: {len(self.bound_groups)} 个",
            f"Cookie状态: {'✅ 已配置' if self.cookie else '❌ 未配置'}",
            f"群模式: {'白名单' if self.config.get('use_whitelist_mode', False) else '黑名单'}",
            f"关键词列表: {', '.join(self.config.get('keywords', []))}"
        ]
        await event.send(event.plain_result("\n".join(lines)))

    async def _cmd_mode(self, event: AstrMessageEvent, mode: str):
        if mode == 'whitelist':
            self.config['use_whitelist_mode'] = True
        elif mode == 'blacklist':
            self.config['use_whitelist_mode'] = False
        else:
            await event.send(event.plain_result("模式只能是 whitelist 或 blacklist"))
            return
        self._save_config()
        await event.send(event.plain_result(f"✅ 已切换到 {'白名单' if self.config['use_whitelist_mode'] else '黑名单'} 模式"))

    async def _cmd_interval(self, event: AstrMessageEvent, sec_str: str):
        try:
            sec = max(60, int(sec_str))
            self.config['scan_interval'] = sec
            self._save_config()
            await event.send(event.plain_result(f"✅ 已设置定时推送间隔为 {sec} 秒"))
        except ValueError:
            await event.send(event.plain_result("请输入有效的数字"))
