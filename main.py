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

# ---------- WBI签名助手 ----------
class WbiHelper:
    @staticmethod
    async def get_keys(session: aiohttp.ClientSession) -> tuple:
        """从B站导航页获取最新的 img_key 和 sub_key"""
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
        """为请求参数添加 w_rid 和 wts 签名"""
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
    """格式化数字为万/亿"""
    if num >= 1_0000_0000:
        return f"{num/1_0000_0000:.1f}亿"
    elif num >= 1_0000:
        return f"{num/1_0000:.1f}万"
    return str(num)

def normalize_title(title: str) -> str:
    """规范化标题用于去重"""
    return re.sub(r'\s+', ' ', title).strip().lower()

# ---------- 主插件类 ----------
@register("astrbotplugin_bili_randomvideoshit", "Rasutohda",
          "B站随机视频搬运｜扫码登录｜关键词触发｜定时推送", "3.0.0",
          "https://github.com/Rasutohda/astrbotplugin_bili_randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.running = False          # 定时任务运行标志
        self.task = None              # 定时任务句柄
        self.session = None           # aiohttp会话
        self.cookie = self._load_cookie()
        self.bound_groups: Dict[str, str] = self._load_json("bound_groups.json", {})   # group_id -> umo
        self.sent_titles: Dict[str, dict] = self._load_json("sent_titles.json", {})    # 已发送标题
        self.group_cooldown: Dict[str, float] = self._load_json("group_cooldown.json", {})  # 关键词冷却

        self.manual_cooldown = 60      # 手动触发冷却秒数
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
        """插件被加载时调用"""
        self.session = aiohttp.ClientSession()
        if self.config.get('auto_start', True):
            self.running = True
            self.task = asyncio.create_task(self._timer_loop())
            logger.info("✅ B站随机视频搬运：定时任务已启动")

    async def terminate(self):
        """插件被卸载时调用"""
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
        """从B站热门榜单随机获取一个未发送过的视频"""
        params = {"series_id": 0}
        data = await self._fetch_json("https://api.bilibili.com/x/web-interface/popular/series/one", params, need_sign=True)
        video_list = data.get('data', {}).get('list', []) if data else []
        if not video_list:
            return None
        # 随机抽取，最多尝试10次
        for _ in range(10):
            video = random.choice(video_list)
            title = video.get('title', '')
            if not title:
                continue
            if normalize_title(title) in self.sent_titles:
                continue
            # 获取详细信息
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
        """带Cookie和可选WBI签名的API请求"""
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
        """构建图文消息链"""
        text = (
            f"🎬 {video['title']}\n"
            f"👤 {video['author']}\n"
            f"👍 {format_number(video['like'])}  ♥️ {format_number(video['coin'])}  ⭐ {format_number(video['favorite'])}\n"
            f"💬 {format_number(video['danmaku'])}  📺 {format_number(video['play'])}\n"
            f"🔗 {video['url']}"
        )
        chain = [Image.fromURL(video['pic']), Plain(text)] if video.get('pic') else [Plain(text)]
        return chain

    async def _send_to_target(self, target_umo: str, video: dict) -> bool:
        """发送到指定目标（群或个人）"""
        try:
            chain = self._build_message_chain(video)
            await self.context.send_message(target_umo, chain)
            return True
        except Exception as e:
            logger.error(f"发送失败: {e}")
            return False

    async def _push_to_target_group(self, group_id: str, umo: str, event: AstrMessageEvent = None) -> bool:
        """向指定群推送视频，成功后记录标题去重"""
        video = await self._fetch_random_video()
        if not video:
            if event:
                await event.send(event.plain_result("❌ 没找到合适的视频，换个时间再试吧~"))
            return False
        success = await self._send_to_target(umo, video)
        if success:
            self.sent_titles[normalize_title(video['title'])] = {'sent_at': datetime.now().isoformat()}
            self._save_json("sent_titles.json", self.sent_titles)
            logger.info(f"向群 {group_id} 推送成功: {video['title']}")
        return success

    async def _push_to_all_allowed_groups(self):
        """定时推送：向所有允许的群发送同一视频（避免重复请求）"""
        video = await self._fetch_random_video()
        if not video:
            logger.warning("定时推送未找到视频")
            return
        success_count = 0
        for gid, umo in self.bound_groups.items():
            if self._is_allowed(gid):
                if await self._send_to_target(umo, video):
                    success_count += 1
                await asyncio.sleep(1)  # 避免刷屏
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
    @filter.event_message_type("group_message")
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = str(event.message_obj.group_id)
        # 自动记录群
        if group_id not in self.bound_groups:
            self.bound_groups[group_id] = event.unified_msg_origin
            self._save_json("bound_groups.json", self.bound_groups)
            logger.info(f"📌 自动记录新群: {group_id}")

        # 关键词触发逻辑
        keywords = self.config.get('keywords', ["随机视频", "来点视频", "B站视频"])
        if not keywords:
            return
        msg_text = event.message_str.strip()
        if not any(kw in msg_text for kw in keywords):
            return
        # 冷却检查
        now = time.time()
        cooldown_sec = self.config.get('keyword_cooldown_seconds', 600)
        last = self.group_cooldown.get(group_id, 0)
        if now - last < cooldown_sec:
            logger.debug(f"群 {group_id} 关键词触发冷却中")
            return
        # 触发推送
        logger.info(f"群 {group_id} 触发关键词，开始推送")
        await event.send(event.plain_result("🎬 检测到关键词，正在搬运视频..."))
        success = await self._push_to_target_group(group_id, event.unified_msg_origin, event)
        if success:
            self.group_cooldown[group_id] = now
            self._save_json("group_cooldown.json", self.group_cooldown)
        else:
            await event.send(event.plain_result("❌ 暂时没有合适的视频，请稍后再试"))

    # ---------- 命令 ----------
    @filter.command("bili now")
    async def cmd_now(self, event: AstrMessageEvent):
        """立即推送一条视频到当前群"""
        now = time.time()
        if now - self.last_manual_time < self.manual_cooldown:
            remain = int(self.manual_cooldown - (now - self.last_manual_time))
            yield event.plain_result(f"⏳ 冷却中，请 {remain} 秒后再试")
            return
        self.last_manual_time = now
        yield event.plain_result("🎬 正在搬石，请稍候...")
        group_id = str(event.message_obj.group_id)
        success = await self._push_to_target_group(group_id, event.unified_msg_origin, event)
        if not success:
            yield event.plain_result("❌ 没找到合适的视频，换个时间试试吧~")

    @filter.command("bili on")
    async def cmd_on(self, event: AstrMessageEvent):
        """开启定时自动推送"""
        if self.running:
            yield event.plain_result("定时推送已在运行中")
            return
        self.running = True
        self.task = asyncio.create_task(self._timer_loop())
        yield event.plain_result("✅ 已开启定时推送")

    @filter.command("bili off")
    async def cmd_off(self, event: AstrMessageEvent):
        """关闭定时自动推送"""
        if not self.running:
            yield event.plain_result("定时推送已关闭")
            return
        self.running = False
        if self.task:
            self.task.cancel()
            self.task = None
        yield event.plain_result("✅ 已关闭定时推送")

    @filter.command("bili login")
    async def cmd_login(self, event: AstrMessageEvent):
        """扫码登录B站，自动保存Cookie"""
        # 获取二维码
        async with self.session.post(QR_GENERATE_URL, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}) as resp:
            data = await resp.json()
            if data.get('code') != 0:
                yield event.plain_result("❌ 获取二维码失败，请稍后再试")
                return
            qrcode_key = data['data']['qrcode_key']
            qr_url = data['data']['url']
        # 生成二维码图片
        qr = qrcode.make(qr_url)
        qr_path = DATA_DIR / f"qrcode_{int(time.time())}.png"
        qr.save(qr_path)
        # 发送二维码（注意：AstrBot的file_image需要文件路径字符串）
        await event.send(event.make_result().file_image(str(qr_path)))
        await event.send(event.plain_result("🔗 请使用B站手机App扫码登录，有效期3分钟"))
        # 轮询结果
        for _ in range(60):  # 3分钟 / 3秒 = 60次
            await asyncio.sleep(3)
            async with self.session.get(QR_POLL_URL, params={'qrcode_key': qrcode_key}, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}) as resp:
                poll_data = await resp.json()
                if poll_data.get('code') == 0:
                    # 从响应头中提取Cookie
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
                        self.cookie = cookie_str
                        self._save_cookie(cookie_str)
                        yield event.plain_result(f"✅ 登录成功！Cookie已保存。\n用户名：{poll_data['data']['user_info']['uname']}")
                        break
                elif poll_data.get('code') == 86038:
                    yield event.plain_result("⏱️ 二维码已过期，请重新执行 /bili login")
                    break
        else:
            yield event.plain_result("⏱️ 扫码超时，请重新执行 /bili login")
        # 清理临时二维码文件
        try:
            qr_path.unlink()
        except:
            pass

    @filter.command("bili status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件运行状态"""
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
        yield event.plain_result("\n".join(status_lines))

    @filter.command("bili mode")
    async def cmd_mode(self, event: AstrMessageEvent):
        """切换群推送模式: /bili mode whitelist 或 /bili mode blacklist"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /bili mode <whitelist|blacklist>")
            return
        mode = parts[2].lower()
        if mode == 'whitelist':
            self.config['use_whitelist_mode'] = True
        elif mode == 'blacklist':
            self.config['use_whitelist_mode'] = False
        else:
            yield event.plain_result("模式只能是 whitelist 或 blacklist")
            return
        if hasattr(self.config, 'save_config'):
            self.config.save_config()
        yield event.plain_result(f"✅ 已切换到 {'白名单' if self.config['use_whitelist_mode'] else '黑名单'} 模式")

    @filter.command("bili interval")
    async def cmd_interval(self, event: AstrMessageEvent):
        """设置定时推送间隔（秒）: /bili interval 3600"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /bili interval <秒数>")
            return
        try:
            sec = max(60, int(parts[2]))
            self.config['scan_interval'] = sec
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            yield event.plain_result(f"✅ 已设置定时推送间隔为 {sec} 秒")
        except:
            yield event.plain_result("请输入有效的数字")
