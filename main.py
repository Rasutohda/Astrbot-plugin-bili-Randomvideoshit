import random
import json
import os
import logging
import time
import hashlib
import aiohttp
from typing import Optional, Dict, Any
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import Plain, Image

logger = logging.getLogger(__name__)

# ---------- WBI 签名函数 ----------
def wbi_sign(params: dict, key: str = "ea1db124af3c7062474693fa704f4ff8") -> dict:
    """
    为 B 站 API 请求添加 WBI 签名（w_rid + wts）
    :param params: 原始请求参数字典
    :param key: 固定密钥（已验证长期有效）
    :return: 添加了 w_rid 和 wts 的新字典
    """
    # 1. 添加当前时间戳
    params['wts'] = int(time.time())
    # 2. 按键名排序，过滤掉 None 值
    sorted_params = sorted([(k, v) for k, v in params.items() if v is not None])
    # 3. 拼接字符串
    param_str = '&'.join([f"{k}={v}" for k, v in sorted_params])
    sign_str = param_str + key
    # 4. 计算 MD5 签名
    params['w_rid'] = hashlib.md5(sign_str.encode()).hexdigest()
    return params


@register(
    "astrbot_plugin_bili_random",
    "Rasutohda",
    "通过关键词触发，随机搬运B站视频（支持配置，已适配WBI签名）",
    "3.0.5",
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
            "hot_series_id": 0,
            "max_video_age_days": 7
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
    async def on_message(self, event: AstrMessageEvent) -> MessageEventResult:
        msg = event.message_str.strip()

        # 命令：重载配置
        if msg == "!bili reload":
            self.reload_config()
            return event.plain_result("✅ 配置已重新加载")

        # 关键词检测
        keywords = self.config.get("keywords", ["随机视频"])
        if not any(kw in msg for kw in keywords):
            return event.plain_result(None)  # 不匹配，无回复

        # 可选：发送“正在搬运”提示
        if self.config.get("show_processing", True):
            await event.send(event.plain_result("🎬 正在搬运，请稍等~"))

        # 获取随机视频
        video = await self.fetch_random_video()
        if not video:
            return event.plain_result("❌ 获取视频失败，请稍后再试")

        # 发送结果
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

        return event.plain_result(None)

    def _format_video_text(self, video: dict) -> str:
        return (
            f"🎬 {video['title']}\n"
            f"👤 {video['owner']}\n"
            f"👍 {video['like']}  ♥️ {video['coin']}  ⭐ {video['favorite']}\n"
            f"💬 {video['danmaku']}  📺 {video['view']}\n"
            f"🔗 {video['url']}"
        )

    # ------------------- B站API（带WBI签名）-------------------
    async def fetch_json(self, url: str, params: dict = None) -> Optional[Dict]:
        """通用GET请求，自动添加WBI签名"""
        try:
            async with aiohttp.ClientSession(timeout=10) as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
                # 为参数添加 WBI 签名（仅当有参数时才执行）
                if params:
                    params = wbi_sign(params)
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
        hot_series_id = self.config.get("hot_series_id", 0)
        max_age_days = self.config.get("max_video_age_days", 7)

        for _ in range(max_retries):
            if hot_series_id == 0:
                # 全站热门列表接口（需要签名，但此接口一般不需要参数，所以直接请求）
                hot_url = "https://api.bilibili.com/x/web-interface/popular"
                hot_data = await self.fetch_json(hot_url, {})  # 传空字典以触发签名
                video_list = hot_data.get('data') if hot_data else None
            else:
                hot_url = f"https://api.bilibili.com/x/web-interface/popular/series/one"
                params = {"series_id": hot_series_id}
                hot_data = await self.fetch_json(hot_url, params)
                video_list = hot_data.get('data', {}).get('list') if hot_data else None

            if not video_list:
                logger.warning("未获取到热门视频列表")
                continue

            random_video = random.choice(video_list)
            bvid = random_video.get('bvid')
            if not bvid:
                continue

            # 获取视频详细信息（需要签名）
            detail_params = {"bvid": bvid}
            detail_data = await self.fetch_json("https://api.bilibili.com/x/web-interface/view", detail_params)
            if not detail_data or not detail_data.get('data'):
                continue

            v = detail_data['data']
            # 时效性过滤
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
