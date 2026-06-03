import random
import json
import os
import logging
import time
import hashlib
import re
import aiohttp
from typing import Optional, Dict, Any
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import Plain, Image

logger = logging.getLogger(__name__)

# ---------- WBI 签名混排映射表 ----------
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
]

async def get_wbi_keys(session: aiohttp.ClientSession) -> tuple:
    """获取 img_key 和 sub_key（原始字符串）"""
    url = 'https://api.bilibili.com/x/web-interface/nav'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.bilibili.com/'
    }
    async with session.get(url, headers=headers) as resp:
        data = await resp.json()
        if data.get('code') != 0:
            raise Exception(f"获取 wbi 密钥失败，状态码：{data.get('code')}")
        wbi_img = data['data']['wbi_img']
        img_url = wbi_img['img_url']
        sub_url = wbi_img['sub_url']
        # 提取文件名中的 key
        img_key = re.search(r'/([^/]+)\.png', img_url).group(1)
        sub_key = re.search(r'/([^/]+)\.png', sub_url).group(1)
        return img_key, sub_key

def get_mixin_key(orig: str) -> str:
    """对 img_key + sub_key 拼接后的字符串进行重排，取前 32 位作为最终密钥"""
    return ''.join([orig[i] for i in MIXIN_KEY_ENC_TAB])[:32]

def wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    """使用正确的混排 mixin_key 对参数进行签名"""
    # ① 计算真正的 mixin_key
    raw_str = img_key + sub_key
    mixin_key = get_mixin_key(raw_str)
    # ② 先添加时间戳
    params['wts'] = int(time.time())
    # ③ 参数排序并拼接
    sorted_params = sorted(params.items())
    query = '&'.join([f"{k}={v}" for k, v in sorted_params])
    # ④ 计算签名
    sign_str = query + mixin_key
    w_rid = hashlib.md5(sign_str.encode()).hexdigest()
    # ⑤ 将签名加入参数
    params['w_rid'] = w_rid
    return params

# ---------- 插件主类 ----------
@register(
    "astrbot_plugin_bili_random",
    "Rasutohda",
    "通过关键词触发，随机搬运B站视频（无外部依赖，支持动态签名）",
    "3.1.3",
    "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit"
)
class BiliRandomVideo(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = self._load_config()
        if config:
            self.config.update(config)

    def _load_config(self) -> dict:
        default_config = {
            "keywords": ["随机视频", "来点视频", "随机B站"],
            "send_type": "image",
            "show_processing": True,
            "max_retries": 3,
            "max_video_age_days": 7,
            "cookie": ""
        }
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    default_config.update(user_config)
                logger.info("配置文件加载成功")
            except Exception as e:
                logger.warning(f"配置文件加载失败: {e}")
        else:
            logger.info("未找到配置文件，使用默认配置")
        return default_config

    def reload_config(self):
        self.config = self._load_config()
        logger.info("配置已重载")

    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> Optional[MessageEventResult]:
        msg = event.message_str.strip()
        if msg == "!bili reload":
            self.reload_config()
            return event.plain_result("✅ 配置已重新加载")
        keywords = self.config.get("keywords", ["随机视频"])
        if not any(kw in msg for kw in keywords):
            return None
        if self.config.get("show_processing", True):
            await event.send(event.plain_result("🎬 正在搬运，请稍等~"))
        video = await self.fetch_random_video()
        if not video:
            return event.plain_result("❌ 获取视频失败，请稍后再试")
        send_type = self.config.get("send_type", "image")
        text = self._format_video_text(video)
        if send_type == "image" and video.get("pic"):
            try:
                chain = [Image.fromURL(video["pic"]), Plain(text)]
                await event.send(event.make_result().message(chain))
            except Exception as e:
                logger.error(f"图片发送失败: {e}，降级为纯文本")
                await event.send(event.plain_result(text))
        else:
            await event.send(event.plain_result(text))
        return None

    def _format_video_text(self, video: dict) -> str:
        return (
            f"🎬 {video['title']}\n"
            f"👤 {video['owner']}\n"
            f"👍 {video['like']}  ♥️ {video['coin']}  ⭐ {video['favorite']}\n"
            f"💬 {video['danmaku']}  📺 {video['view']}\n"
            f"🔗 {video['url']}"
        )

    async def fetch_json(self, url: str, params: dict = None, need_sign: bool = False) -> Optional[Dict]:
        try:
            async with aiohttp.ClientSession(timeout=10) as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
                cookie_str = self.config.get("cookie", "")
                if cookie_str:
                    headers["Cookie"] = cookie_str
                if params is None:
                    params = {}
                if need_sign:
                    img_key, sub_key = await get_wbi_keys(session)
                    params = wbi_sign(params, img_key, sub_key)
                async with session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data
                    logger.warning(f"API错误码 {data.get('code')}: {data.get('message')}")
                    return None
        except Exception as e:
            logger.error(f"请求失败 {url}: {e}")
            return None

    async def fetch_random_video(self) -> Optional[Dict[str, Any]]:
        max_retries = self.config.get("max_retries", 3)
        max_age_days = self.config.get("max_video_age_days", 7)
        for attempt in range(max_retries):
            page = random.randint(1, 5)
            params = {"pn": page, "ps": 30}
            popular_data = await self.fetch_json(
                "https://api.bilibili.com/x/web-interface/popular",
                params,
                need_sign=True
            )
            if not popular_data:
                continue
            video_list = popular_data.get('data', {}).get('list', [])
            if not video_list:
                continue
            selected = random.choice(video_list)
            bvid = selected.get('bvid')
            if not bvid:
                continue
            detail_params = {"bvid": bvid}
            detail_data = await self.fetch_json(
                "https://api.bilibili.com/x/web-interface/view",
                detail_params,
                need_sign=True
            )
            if not detail_data:
                continue
            v = detail_data.get('data')
            if not v:
                continue
            if max_age_days > 0:
                pub_ts = v.get('pubdate', 0)
                if pub_ts and (time.time() - pub_ts) > max_age_days * 86400:
                    logger.debug(f"视频过旧，跳过: {bvid}")
                    continue
            stats = v.get('stat', {})
            return {
                'bvid': bvid,
                'title': v.get('title'),
                'owner': v.get('owner', {}).get('name'),
                'pic': v.get('pic'),
                'url': f"https://www.bilibili.com/video/{bvid}",
                'view': self._format_number(stats.get('view', 0)),
                'like': self._format_number(stats.get('like', 0)),
                'coin': self._format_number(stats.get('coin', 0)),
                'favorite': self._format_number(stats.get('favorite', 0)),
                'danmaku': self._format_number(stats.get('danmaku', 0))
            }
        return None

    @staticmethod
    def _format_number(num: int) -> str:
        if num >= 100000000:
            return f"{num/100000000:.1f}亿"
        if num >= 10000:
            return f"{num/10000:.1f}万"
        return str(num)
