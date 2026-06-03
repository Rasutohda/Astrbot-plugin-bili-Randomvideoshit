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
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 扫码状态码
QR_CODE_UNSCANNED = 86101      # 未扫码
QR_CODE_SCANNED = 86090        # 已扫码待确认
QR_CODE_EXPIRED = 86038        # 二维码已过期
QR_CODE_SUCCESS = 0            # 登录成功

QR_CODE_EXPIRE_TIME = 180      # 二维码有效期（秒）
POLL_INTERVAL = 5              # 轮询间隔（秒）


# ---------- WBI签名 ----------
class WbiHelper:
    """B站WBI签名生成器"""
    
    @staticmethod
    async def get_keys(session: aiohttp.ClientSession) -> tuple:
        """获取最新的 img_key 和 sub_key"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.bilibili.com/'
        }
        async with session.get('https://api.bilibili.com/x/web-interface/nav', headers=headers) as resp:
            data = await resp.json()
            if data.get('code') != 0:
                raise Exception("获取WBI密钥失败")
            img_url = data['data']['wbi_img']['img_url']
            sub_url = data['data']['wbi_img']['sub_url']
            img_key = re.search(r'/([^/]+)\.png', img_url).group(1)
            sub_key = re.search(r'/([^/]+)\.png', sub_url).group(1)
            return img_key, sub_key

    @staticmethod
    def sign(params: dict, img_key: str, sub_key: str) -> dict:
        """为请求参数添加 w_rid 和 wts 签名"""
        mixin_key = img_key + sub_key
        sorted_params = sorted(params.items())
        query = '&'.join([f"{k}={v}" for k, v in sorted_params])
        params['w_rid'] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        params['wts'] = int(time.time())
        return params


# ---------- 辅助函数 ----------
def format_number(num: int) -> str:
    """数字格式化（万/亿）"""
    if num >= 1_0000_0000:
        return f"{num/1_0000_0000:.1f}亿"
    elif num >= 1_0000:
        return f"{num/1_0000:.1f}万"
    return str(num)


def normalize_title(title: str) -> str:
    """标准化标题（去重用）"""
    return re.sub(r'\s+', ' ', title).strip().lower()


# ---------- 主插件 ----------
@register("astrbot_plugin_bili_Randomvideoshit", "Rasutohda",
          "B站随机视频搬运｜扫码登录｜关键词触发｜定时推送", "3.0.8",
          "https://github.com/Rasutohda/astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.running = False
        self.task = None
        self.session = None
        self.cookie = self._load_cookie()
        self.bound_groups: Dict[str, str] = self._load_json("bound_groups.json", {})
        self.sent_titles: Dict[str, dict] = self._load_json("sent_titles.json", {})
        self.group_cooldown: Dict[str, float] = self._load_json("group_cooldown.json", {})
        self.manual_cooldown = 60
        self.last_manual_time = 0.0
        self._login_tasks: Dict[str, asyncio.Task] = {}   # 防止重复扫码

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
        """从B站热门榜单随机获取视频"""
        params = {"series_id": 0}
        data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular/series/one", params, need_sign=True)
        video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular", {"pn": 1, "ps": 30}, need_sign=True)
            video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            return None

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

    async def _fetch_json(self, url: str, params: dict, need_sign: bool = False) -> Optional[dict]:
        """带Cookie和WBI签名的API请求"""
        if need_sign:
            try:
                img_key, sub_key = await WbiHelper.get_keys(self.session)
                params = WbiHelper.sign(params, img_key, sub_key)
            except Exception as e:
                logger.warning(f"WBI签名失败: {e}")
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
        if self.cookie:
            headers['Cookie'] = self.cookie
        try:
            async with self.session.get(url, headers=headers, params=params) as resp:
                data = await resp.json()
                return data if data.get('code') == 0 else None
        except Exception:
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
        return [Image.fromURL(video['pic']), Plain(text)] if video.get('pic') else [Plain(text)]

    async def _send_to_target(self, target, video: dict):
        chain = self._build_message_chain(video)
        if isinstance(target, AstrMessageEvent):
            await target.send(target.make_result().message(chain))
        else:
            await self.context.send_message(target, chain)

    async def _push_to_target_group(self, group_id: str, umo: str, event: AstrMessageEvent = None) -> bool:
        """推送给指定群"""
        video = await self._fetch_random_video()
        if not video:
            if event:
                await event.send(event.plain_result("❌ 没找到合适的视频"))
            return False
        await self._send_to_target(event if event else umo, video)
        self.sent_titles[normalize_title(video['title'])] = {'sent_at': datetime.now().isoformat()}
        self._save_json("sent_titles.json", self.sent_titles)
        return True

    async def _push_to_all_allowed_groups(self):
        """定时推送：向所有允许的群发送视频"""
        video = await self._fetch_random_video()
        if not video:
            return
        chain = self._build_message_chain(video)
        for gid, umo in self.bound_groups.items():
            if self._is_allowed(gid):
                try:
                    await self.context.send_message(umo, chain)
                    await asyncio.sleep(1)
                except Exception:
                    pass
        self.sent_titles[normalize_title(video['title'])] = {'sent_at': datetime.now().isoformat()}
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
    async def _generate_qrcode_image(self, url: str) -> Optional[Path]:
        """生成二维码图片并返回路径"""
        try:
            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            qr_path = DATA_DIR / f"qrcode_{int(time.time())}.png"
            img.save(str(qr_path), "PNG")
            return qr_path
        except Exception as e:
            logger.error(f"生成二维码失败: {e}")
            return None

    async def _poll_qr_login(self, sender_id: str, qrcode_key: str, qr_path: Path):
        """轮询扫码状态"""
        start_time = datetime.now()
        last_notified_status = None
        try:
            while True:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= QR_CODE_EXPIRE_TIME:
                    logger.info(f"用户 {sender_id} 的二维码已过期")
                    break

                try:
                    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
                    async with self.session.get(BILI_QR_POLL_URL, params={"qrcode_key": qrcode_key}, headers=headers) as resp:
                        poll_data = await resp.json()
                        set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                    logger.warning(f"轮询请求失败: {e}")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                code = poll_data.get("data", {}).get("code", -1)

                if code == QR_CODE_SUCCESS:
                    # 登录成功，提取Cookie
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
                        await self._notify_user(sender_id, "✅ 登录成功！Cookie已自动保存")
                        break
                    else:
                        logger.warning("登录成功但Cookie提取失败")

                elif code == QR_CODE_UNSCANNED:
                    if last_notified_status != QR_CODE_UNSCANNED:
                        pass  # 静默等待

                elif code == QR_CODE_SCANNED:
                    if last_notified_status != QR_CODE_SCANNED:
                        await self._notify_user(sender_id, "✅ 已扫码\n请在手机上点击「确认登录」完成授权")
                        last_notified_status = QR_CODE_SCANNED

                elif code == QR_CODE_EXPIRED:
                    logger.info(f"用户 {sender_id} 的二维码已过期")
                    await self._notify_user(sender_id, "⏱️ 二维码已过期\n请重新发送 bili login 获取新二维码")
                    break

                await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info(f"用户 {sender_id} 的扫码轮询被取消")
        except Exception as e:
            logger.exception(f"扫码轮询出错 (用户: {sender_id})")
            await self._notify_user(sender_id, "❌ 扫码登录过程出错，请重新尝试 bili login")
        finally:
            # 清理二维码文件
            if qr_path and qr_path.exists():
                try:
                    qr_path.unlink()
                except:
                    pass
            # 移除登录任务记录
            if sender_id in self._login_tasks:
                del self._login_tasks[sender_id]

    async def _notify_user(self, user_id: str, message: str):
        """向用户发送通知"""
        try:
            # 构造 unify_msg_origin 格式
            umo = user_id if ":" in user_id else f"default:FriendMessage:{user_id}"
            await self.context.send_message(umo, message)
        except Exception as e:
            logger.error(f"发送消息给 {user_id} 失败: {e}")

    # ---------- 核心消息过滤器 ----------
    @filter
    async def message_filter(self, event: AstrMessageEvent):
        """监听所有群消息，处理命令和关键词"""
        msg = event.message_str.strip()
        if not msg:
            return True

        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "0"
        await self._record_group(group_id, event.unified_msg_origin)

        # 处理命令（支持 bili 和 /bili 开头）
        if msg.startswith('bili ') or msg.startswith('/bili ') or msg in ('bili', '/bili'):
            if msg.startswith('/'):
                msg = msg[1:]
            parts = msg.split()
            cmd = parts[1] if len(parts) > 1 else ''

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
            elif cmd == 'mode' and len(parts) > 2:
                await self._cmd_mode(event, parts[2])
            elif cmd == 'interval' and len(parts) > 2:
                await self._cmd_interval(event, parts[2])
            else:
                await event.send(event.plain_result("可用命令: bili now, on, off, login, status, mode, interval"))
            return False

        # 关键词触发
        keywords = self.config.get('keywords', ["随机视频", "来点视频", "B站视频"])
        if any(kw in msg for kw in keywords):
            now = time.time()
            cooldown = self.config.get('keyword_cooldown_seconds', 600)
            last = self.group_cooldown.get(group_id, 0)
            if now - last >= cooldown:
                await event.send(event.plain_result("🎬 检测到关键词，正在搬运视频..."))
                success = await self._push_to_target_group(group_id, event.unified_msg_origin, event)
                if success:
                    self.group_cooldown[group_id] = now
                    self._save_json("group_cooldown.json", self.group_cooldown)
            else:
                remain = int(cooldown - (now - last))
                await event.send(event.plain_result(f"⏳ 冷却中，请 {remain} 秒后再试"))
            return False

        return True

    # ---------- 命令实现 ----------
    async def _cmd_now(self, event: AstrMessageEvent):
        now = time.time()
        if now - self.last_manual_time < self.manual_cooldown:
            remain = int(self.manual_cooldown - (now - self.last_manual_time))
            await event.send(event.plain_result(f"⏳ 冷却中，请 {remain} 秒后再试"))
            return
        self.last_manual_time = now
        await event.send(event.plain_result("🎬 正在搬石，请稍候..."))
        group_id = str(event.message_obj.group_id) if event.message_obj.group_id else "0"
        await self._push_to_target_group(group_id, event.unified_msg_origin, event)

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
        """B站扫码登录"""
        sender_id = event.get_sender_id()

        # 检查是否有正在进行的登录
        if sender_id in self._login_tasks and not self._login_tasks[sender_id].done():
            await event.send(event.plain_result("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时"))
            return

        await event.send(event.plain_result("🔐 正在生成登录二维码..."))

        try:
            # 1. 获取二维码URL和key
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

            # 2. 生成二维码图片
            qr_path = await self._generate_qrcode_image(qrcode_url)
            if not qr_path:
                await event.send(event.plain_result("❌ 二维码生成失败，请检查是否已安装 qrcode 库"))
                return

            # 3. 发送二维码图片
            await event.send(event.make_result().file_image(str(qr_path)))
            await event.send(event.plain_result("🔗 请使用B站手机App扫码登录，有效期3分钟"))

            # 4. 启动异步轮询
            task = asyncio.create_task(self._poll_qr_login(sender_id, qrcode_key, qr_path))
            self._login_tasks[sender_id] = task

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
        if hasattr(self.config, 'save_config'):
            self.config.save_config()
        await event.send(event.plain_result(f"✅ 已切换到 {'白名单' if self.config['use_whitelist_mode'] else '黑名单'} 模式"))

    async def _cmd_interval(self, event: AstrMessageEvent, sec_str: str):
        try:
            sec = max(60, int(sec_str))
            self.config['scan_interval'] = sec
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            await event.send(event.plain_result(f"✅ 已设置定时推送间隔为 {sec} 秒"))
        except:
            await event.send(event.plain_result("请输入有效的数字"))
