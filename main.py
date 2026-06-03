import random
import json
import os
import logging
from typing import Optional, Dict, Any, List
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import Plain, Image
from bilibili_api import sync, Credential
from bilibili_api import popular_series, video, user as bilibili_user

logger = logging.getLogger(__name__)

@register(
    "astrbot_plugin_bili_random",
    "Rasutohda",
    "通过关键词触发，随机搬运B站视频（基于bilibili-api-python）",
    "3.1.0",
    "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit"
)
class BiliRandomVideo(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = self._load_config()
        if config:
            self.config.update(config)
        self.credential = None
        self._init_credential()

    def _init_credential(self):
        """初始化Bilibili API认证信息"""
        try:
            cred_config = self.config.get("credential", {})
            sessdata = cred_config.get("sessdata", os.getenv("BILI_SESSDATA"))
            bili_jct = cred_config.get("bili_jct", os.getenv("BILI_JCT"))
            buvid3 = cred_config.get("buvid3", os.getenv("BILI_BUVID3"))
            
            if sessdata and bili_jct and buvid3:
                self.credential = Credential(
                    sessdata=sessdata,
                    bili_jct=bili_jct,
                    buvid3=buvid3
                )
                logger.info("B站API认证信息已初始化")
            else:
                logger.warning("B站API认证信息不完整，部分功能可能受限")
        except Exception as e:
            logger.error(f"初始化B站API认证失败: {e}")

    def _load_config(self) -> dict:
        default_config = {
            "keywords": ["随机视频", "来点视频", "随机B站"],
            "send_type": "image",
            "show_processing": True,
            "max_retries": 3,
            "max_video_age_days": 7,
            "credential": {  # 新增：用户凭证配置
                "sessdata": "",
                "bili_jct": "",
                "buvid3": ""
            }
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

    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> Optional[MessageEventResult]:
        msg = event.message_str.strip()
        logger.debug(f"收到消息: {msg}")
        
        if msg == "!bili reload":
            self.config = self._load_config()
            self._init_credential()
            return event.plain_result("✅ 配置已重新加载")
        
        keywords = self.config.get("keywords", ["随机视频"])
        if not any(kw in msg for kw in keywords):
            return None
        
        if self.config.get("show_processing", True):
            await event.send(event.plain_result("🎬 正在搬运，请稍等~"))
        
        video = await self.fetch_random_video()
        if not video:
            return event.plain_result("❌ 获取视频失败，请稍后再试")
        
        if self.config.get("send_type", "image") == "image" and video.get("pic"):
            try:
                chain = [Image.fromURL(video["pic"]), Plain(self._format_video_text(video))]
                await event.send(event.make_result().message(chain))
            except Exception as e:
                logger.error(f"图片发送失败: {e}")
                await event.send(event.plain_result(self._format_video_text(video)))
        else:
            await event.send(event.plain_result(self._format_video_text(video)))
        return None

    async def fetch_random_video(self) -> Optional[Dict[str, Any]]:
        for attempt in range(self.config.get("max_retries", 3)):
            try:
                # 获取随机页的热门视频
                page = random.randint(1, 10)
                # 通过api获取热门视频列表
                try:
                    popular_list = await popular_series.get_popular_series()
                    series_id = popular_list['list'][0]['series_id']
                    # 根据certified检测是否需要异步处理
                    hot_videos = await popular_series.get_popular_series_video(series_id, self.credential)
                    video_list = hot_videos.get('list', [])
                except:
                    # 备用接口：尝试获取全站热门
                    video_list = await popular_series.get_popular_videos(ps=30, pn=page, credential=self.credential)
                    video_list = video_list.get('list', [])
                
                if not video_list:
                    logger.warning("未获取到热门视频列表")
                    continue
                
                random_video = random.choice(video_list)
                bvid = random_video.get('bvid')
                if not bvid:
                    continue
                
                # 获取详细信息
                video_info = await video.Video(bvid=bvid, credential=self.credential).get_info()
                
                # 时效性检查
                if self.config.get("max_video_age_days", 0) > 0:
                    import time
                    pub_ts = video_info.get('pubdate', 0)
                    if pub_ts and (time.time() - pub_ts) > self.config['max_video_age_days'] * 86400:
                        logger.debug(f"视频过旧，跳过: {bvid}")
                        continue
                
                stats = video_info.get('stat', {})
                return {
                    'bvid': bvid,
                    'title': video_info.get('title'),
                    'owner': video_info.get('owner', {}).get('name'),
                    'pic': video_info.get('pic'),
                    'url': f"https://www.bilibili.com/video/{bvid}",
                    'view': self._format_number(stats.get('view', 0)),
                    'like': self._format_number(stats.get('like', 0)),
                    'coin': self._format_number(stats.get('coin', 0)),
                    'favorite': self._format_number(stats.get('favorite', 0)),
                    'danmaku': self._format_number(stats.get('danmaku', 0))
                }
            except Exception as e:
                logger.error(f"获取视频失败 (尝试 {attempt+1}): {e}")
                continue
        return None

    @staticmethod
    def _format_number(num: int) -> str:
        """格式化数字显示"""
        if num >= 100000000:
            return f"{num/100000000:.1f}亿"
        if num >= 10000:
            return f"{num/10000:.1f}万"
        return str(num)

    @staticmethod
    def _format_video_text(video: dict) -> str:
        return (
            f"🎬 {video['title']}\n"
            f"👤 {video['owner']}\n"
            f"👍 {video['like']}  ♥️ {video['coin']}  ⭐ {video['favorite']}\n"
            f"💬 {video['danmaku']}  📺 {video['view']}\n"
            f"🔗 {video['url']}"
        )
