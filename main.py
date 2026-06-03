# -*- coding: utf-8 -*-
import asyncio
import random
import re
import json
import os
import time
import hashlib
import aiohttp
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import qrcode

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image

# ---------- 数据目录 ----------
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# ---------- B站API端点 ----------
QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# ---------- 扫码登录模块 ----------
class BiliLogin:
    """B站扫码登录器"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.bilibili.com/'
        }

    async def generate_qrcode(self) -> tuple:
        """生成二维码，返回 (qrcode_key, qr_url, qr_image_path)"""
        try:
            async with self.session.post(self.QR_GENERATE_URL, headers=self.headers) as resp:
                data = await resp.json()
                if data.get('code') != 0:
                    return None, None, None
                qrcode_key = data['data']['qrcode_key']
                qr_url = data['data']['url']

                # 生成二维码图片
                qr = qrcode.make(qr_url)
                qr_path = DATA_DIR / f"qrcode_{int(time.time())}.png"
                qr.save(qr_path)
                return qrcode_key, qr_url, str(qr_path)
        except Exception as e:
            logger.error(f"生成二维码失败: {e}")
            return None, None, None

    async def poll_login(self, qrcode_key: str, timeout: int = 180) -> Optional[str]:
        """轮询扫码结果，返回Cookie字符串或None"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            await asyncio.sleep(3)
            try:
                async with self.session.get(self.QR_POLL_URL, params={'qrcode_key': qrcode_key}, headers=self.headers) as resp:
                    poll_data = await resp.json()
                    if poll_data.get('code') == 0:
                        # 扫码成功，从响应头提取Cookie
                        cookie_dict = {}
                        set_cookie_headers = resp.headers.getall('Set-Cookie', [])
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
                            return cookie_str
                    elif poll_data.get('code') == 86038:
                        # 二维码过期
                        logger.warning("登录二维码已过期")
                        return None
            except Exception as e:
                logger.error(f"轮询登录状态异常: {e}")
                continue
        return None

# ---------- WBI签名助手 ----------
class WbiHelper:
    @staticmethod
    async def get_keys(session: aiohttp.ClientSession) -> tuple:
        async with session.get('https://api.bilibili.com/x/web-interface/nav',
                               headers=WbiHelper._headers()) as resp:
            data = await resp.json()
            if data.get('code') != 0:
                raise Exception("获取密钥失败")
            img_url = data['data']['wbi_img']['img_url']
            sub_url = data['data']['wbi_img']['sub_url']
            img_key = re.search(r'/([^/]+)\.png', img_url).group(1)
            sub_key = re.search(r'/([^/]+)\.png', sub_url).group(1)
            return img_key, sub_key

    @staticmethod
    def sign(params: dict, img_key: str, sub_key: str) -> dict:
        mixin_key = img_key + sub_key
        sorted_params = sorted(params.items())
        query = '&'.join([f"{k}={v}" for k, v in sorted_params])
        params['w_rid'] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        params['wts'] = int(time.time())
        return params

    @staticmethod
    def _headers() -> dict:
        return {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.bilibili.com/'
        }

# ---------- 辅助函数 ----------
def format_number(num: int) -> str:
    if num >= 1_0000_0000:
        return f"{num/1_0000_0000:.1f}亿"
    elif num >= 1_0000:
        return f"{num/1_0000:.1f}万"
    return str(num)

def normalize_title(title: str) -> str:
    return re.sub(r'\s+', ' ', title).strip().lower()

# ---------- 主插件类 ----------
@register("astrbot_plugin_bili_Randomvideoshit", "Rasutohda",
          "B站随机视频搬运｜扫码登录｜关键词触发｜定时推送", "3.0.5",
          "https://github.com/Rasutohda/astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.running = False
        self.task = None
        self.session = None
        self.bili_login = None
        self.cookie = self._load_cookie()
        self.bound_groups: Dict[str, str] = self._load_json("bound_groups.json", {})
        self.sent_titles: Dict[str, dict] = self._load_json("sent_titles.json", {})
        self.group_cooldown: Dict[str, float] = self._load_json("group_cooldown.json", {})
        self.manual_cooldown = 60
        self.last_manual_time = 0.0

    # ---------- 持久化 ----------
    def _load_json(self, filename: str, default: dict) -> dict:
        path = DATA_DIR / filename
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except:
            return default

    def _save_json(self, filename: str, data: dict):
        (DATA_DIR / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

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
        self.bili_login = BiliLogin(self.session)
        if self.config.get('auto_start', True):
            self.running = True
            self.task = asyncio.create_task(self._timer_loop())
            logger.info("✅ B站随机视频搬运：定时任务已启动")

    async def terminate(self):
        self.running = False
        if self.task:
            self.task.cancel()
        if self.session:
            await self.session.close()
        logger.info("B站随机视频搬运插件已卸载")

    # ---------- 后台定时循环 ----------
    async def _timer_loop(self):
        interval = max(60, self.config.get('scan_interval', 3600))
        while self.running:
            await self._push_to_all_allowed_groups()
            await asyncio.sleep(interval)

    # ---------- 核心视频获取 ----------
    async def _fetch_random_video(self) -> Optional[dict]:
        params = {"series_id": 0}
        data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular/series/one", params, need_sign=True)
        video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            return None
        for _ in range(10):
            video = random.choice(video_list)
            title = video.get('title', '')
            if not title:
                continue
            if normalize_title(title) in self.sent_titles:
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

    async def _fetch_json(self, url: str, params: dict, need_sign: bool = False) -> Optional[dict]:
        if need_sign:
            try:
                img_key, sub_key = await WbiHelper.get_keys(self.session)
                params = WbiHelper.sign(params, img_key, sub_key)
            except Exception as e:
                logger.error(f"WBI签名失败: {e}")
                return None
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://www.bilibili.com/',
            'Cookie': self.cookie
        }
        try:
            async with self.session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                if data.get('code') == 0:
                    return data
                logger.warning(f"API错误 {data.get('code')}: {data.get('message')}")
                return None
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return None

    # ---------- 消息构建与发送 ----------
    def _build_message_chain(self, video: dict) -> list:
        text = (
            f"🎬 {video['title']}\n"
            f"👤 {video['author']}\n"
            f"👍 {format_number(video['like'])}  ♥️ {format_number(video['coin'])}  ⭐ {format_number(video['favorite'])}\n"
            f"💬 {format_number(video['danmaku'])}  📺 {format_number(video['play'])}\n"
            f"🔗 {video['url']}"
        )
        chain = [Image.fromURL(video['pic']), Plain(text)] if video.get('pic') else [Plain(text)]
        return chain

    async def _send_video(self, target, video: dict, is_command: bool = False):
        """智能发送视频，兼容event和umo
        Args:
            target: AstrMessageEvent 对象 或 unified_msg_origin 字符串
            video: 视频信息字典
            is_command: 是否为命令触发（影响日志输出）
        """
        chain = self._build_message_chain(video)
        try:
            if isinstance(target, AstrMessageEvent):
                # 命令触发：回复到当前会话
                await target.send(target.make_result().message(chain))
            else:
                # 定时任务：使用存储的umo发送
                await self.context.send_message(target, chain)
            return True
        except Exception as e:
            logger.error(f"发送失败: {e}")
            return False

    async def _push_to_target_group(self, group_id: str, umo: str, event: AstrMessageEvent = None) -> bool:
        """推送给指定群，event存在时回复到当前会话"""
        video = await self._fetch_random_video()
        if not video:
            if event:
                await event.send(event.plain_result("❌ 没找到合适的视频，换个时间再试吧~"))
            return False
        # 发送视频
        success = await self._send_video(event if event else umo, video, is_command=bool(event))
        if success:
            self.sent_titles[normalize_title(video['title'])] = {'sent_at': datetime.now().isoformat()}
            self._save_json("sent_titles.json", self.sent_titles)
            logger.info(f"向群 {group_id} 推送成功: {video['title']}")
        return success

    async def _push_to_all_allowed_groups(self):
        """定时推送：向所有允许的群发送视频"""
        video = await self._fetch_random_video()
        if not video:
            logger.warning("定时推送未找到视频")
            return
        chain = self._build_message_chain(video)
        success_count = 0
        for gid, umo in self.bound_groups.items():
            if self._is_allowed(gid):
                try:
                    await self.context.send_message(umo, chain)
                    success_count += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"向群 {gid} 发送失败: {e}")
        if success_count > 0:
            self.sent_titles[normalize_title(video['title'])] = {'sent_at': datetime.now().isoformat()}
            self._save_json("sent_titles.json", self.sent_titles)
            logger.info(f"定时推送完成，成功发送给 {success_count} 个群")

    # ---------- 群权限 ----------
    def _is_allowed(self, group_id: str) -> bool:
        mode = self.config.get('use_whitelist_mode', False)
        whitelist = self.config.get('whitelist_groups', [])
        blacklist = self.config.get('blacklist_groups', [])
        if mode:
            return group_id in whitelist
        return group_id not in blacklist

    # ---------- 自动记录群 ----------
    async def _record_group(self, group_id: str, umo: str):
        """记录群组信息"""
        if group_id not in self.bound_groups:
            self.bound_groups[group_id] = umo
            self._save_json("bound_groups.json", self.bound_groups)
            logger.info(f"📌 自动记录新群: {group_id}")

    # ---------- 关键词触发 ----------
    @filter.command("bili")  # 使用command装饰器监听以"bili"开头的消息
    async def on_bili_command(self, event: AstrMessageEvent):
        """智能处理以 'bili' 开头的命令"""
        msg_text = event.message_str.strip()
        logger.debug(f"收到bili指令: {msg_text}")

        # 记录当前群
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "0"
        await self._record_group(group_id, event.unified_msg_origin)

        # 解析命令
        if msg_text == "bili now":
            await self.cmd_now(event)
        elif msg_text == "bili on":
            await self.cmd_on(event)
        elif msg_text == "bili off":
            await self.cmd_off(event)
        elif msg_text == "bili login":
            await self.cmd_login(event)
        elif msg_text == "bili status":
            await self.cmd_status(event)
        elif msg_text == "bili mode":
            await self.cmd_mode(event)
        elif msg_text.startswith("bili interval"):
            await self.cmd_interval(event)
        # 关键词模式：如果消息中不包含上述完整命令，但包含配置中的关键词，则触发推送
        else:
            keywords = self.config.get('keywords', ["随机视频", "来点视频", "B站视频"])
            if any(kw in msg_text for kw in keywords):
                now = time.time()
                cooldown_sec = self.config.get('keyword_cooldown_seconds', 600)
                last = self.group_cooldown.get(group_id, 0)
                if now - last >= cooldown_sec:
                    logger.info(f"群 {group_id} 触发关键词，开始推送")
                    await event.send(event.plain_result("🎬 检测到关键词，正在搬运视频..."))
                    success = await self._push_to_target_group(group_id, event.unified_msg_origin, event)
                    if success:
                        self.group_cooldown[group_id] = now
                        self._save_json("group_cooldown.json", self.group_cooldown)
                else:
                    await event.send(event.plain_result(f"⏳ 冷却中，请 {int(cooldown_sec - (now - last))} 秒后再试"))

    # ---------- 命令实现 ----------
    async def cmd_now(self, event: AstrMessageEvent):
        now = time.time()
        if now - self.last_manual_time < self.manual_cooldown:
            remain = int(self.manual_cooldown - (now - self.last_manual_time))
            await event.send(event.plain_result(f"⏳ 冷却中，请 {remain} 秒后再试"))
            return
        self.last_manual_time = now
        await event.send(event.plain_result("🎬 正在搬石，请稍候..."))
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "0"
        success = await self._push_to_target_group(group_id, event.unified_msg_origin, event)
        if not success:
            await event.send(event.plain_result("❌ 没找到合适的视频"))

    async def cmd_on(self, event: AstrMessageEvent):
        if self.running:
            await event.send(event.plain_result("定时推送已在运行中"))
            return
        self.running = True
        self.task = asyncio.create_task(self._timer_loop())
        await event.send(event.plain_result("✅ 已开启定时推送"))

    async def cmd_off(self, event: AstrMessageEvent):
        if not self.running:
            await event.send(event.plain_result("定时推送已关闭"))
            return
        self.running = False
        if self.task:
            self.task.cancel()
            self.task = None
        await event.send(event.plain_result("✅ 已关闭定时推送"))

    async def cmd_login(self, event: AstrMessageEvent):
        await event.send(event.plain_result("🔐 正在生成登录二维码..."))
        async def send_callback(data):
            if isinstance(data, str) and data.endswith('.png'):
                await event.send(event.make_result().file_image(data))
            else:
                await event.send(event.plain_result(data))
        cookie = await self.bili_login.login(event_callback=send_callback)
        if cookie:
            self.cookie = cookie
            self._save_cookie(cookie)
            await event.send(event.plain_result("✅ 登录成功！Cookie已保存"))
        else:
            await event.send(event.plain_result("❌ 登录失败或超时，请重新执行 /bili login"))

    async def cmd_status(self, event: AstrMessageEvent):
        status_lines = [
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
        await event.send(event.plain_result("\n".join(status_lines)))

    async def cmd_mode(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            await event.send(event.plain_result(f"当前模式: {'白名单' if self.config.get('use_whitelist_mode', False) else '黑名单'}\n用法: bili mode whitelist 或 bili mode blacklist"))
            return
        mode = parts[2].lower()
        if mode == 'whitelist':
            self.config['use_whitelist_mode'] = True
        elif mode == 'blacklist':
            self.config['use_whitelist_mode'] = False
        else:
            await event.send(event.plain_result("模式只能是 whitelist 或 blacklist"))
            return
        if hasattr(self.config, 'save_config'):
            self.config.save_config()
        await event.send(event.plain_result(f"✅ 已切换到 {'白名单' if self.config['use_whitelist_mode'] else '黑名单'} 模式"))

    async def cmd_interval(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            await event.send(event.plain_result("用法: bili interval <秒数>"))
            return
        try:
            sec = max(60, int(parts[2]))
            self.config['scan_interval'] = sec
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            await event.send(event.plain_result(f"✅ 已设置定时推送间隔为 {sec} 秒"))
        except:
            await event.send(event.plain_result("请输入有效的数字"))

    # 兼容旧命令（非 bili 开头）
    @filter.command("bili now")
    async def legacy_cmd_now(self, event: AstrMessageEvent):
        await self.cmd_now(event)

    @filter.command("bili on")
    async def legacy_cmd_on(self, event: AstrMessageEvent):
        await self.cmd_on(event)

    @filter.command("bili off")
    async def legacy_cmd_off(self, event: AstrMessageEvent):
        await self.cmd_off(event)

    @filter.command("bili login")
    async def legacy_cmd_login(self, event: AstrMessageEvent):
        await self.cmd_login(event)

    @filter.command("bili status")
    async def legacy_cmd_status(self, event: AstrMessageEvent):
        await self.cmd_status(event)

    @filter.command("bili mode")
    async def legacy_cmd_mode(self, event: AstrMessageEvent):
        await self.cmd_mode(event)

    @filter.command("bili interval")
    async def legacy_cmd_interval(self, event: AstrMessageEvent):
        await self.cmd_interval(event)
