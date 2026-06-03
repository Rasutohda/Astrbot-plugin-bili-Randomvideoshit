# main.py
import random
import logging
import aiohttp
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Plain, Image

logger = logging.getLogger(__name__)

@register(
    "astrbot_plugin_bili_random",
    "Rasutohda",
    "通过关键词触发，随机搬运B站视频",
    "3.0.0",
    "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit"
)
class BiliRandomVideo(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter
    async def message_filter(self, event: AstrMessageEvent) -> MessageEventResult:
        """处理所有消息"""
        msg = event.message_str.strip()

        # 1. 检测关键词
        keywords = ["随机视频", "来点视频", "随机B站"]
        if not any(kw in msg for kw in keywords):
            return event.plain_result(None)  # 无关键词，不做任何回复

        # 2. 发送处理中提示
        yield event.plain_result("🎬 正在搬运，请稍等~")

        # 3. 获取视频
        video = await self.fetch_random_video()
        if not video:
            yield event.plain_result("❌ 没找到视频，换个时间试试吧~")

        # 4. 发送视频信息
        yield event.chain_result([
            Image.fromURL(video['pic']),
            Plain(
                f"\n🎬 {video['title']}\n"
                f"👤 {video['owner']}\n"
                f"👍 {video['like']}  ♥️ {video['coin']}  ⭐ {video['favorite']}\n"
                f"💬 {video['danmaku']}  📺 {video['view']}\n"
                f"🔗 {video['url']}"
            )
        ])

    # ------------------- API 请求 -------------------
    async def fetch_json(self, url: str, params: dict = None) -> dict | None:
        """通用 fetch 函数"""
        try:
            async with aiohttp.ClientSession(timeout=10) as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
                async with session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data
                    logger.warning(f"API 错误: {data.get('message')}")
                    return None
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return None

    async def fetch_random_video(self) -> dict | None:
        """从B站首页热门中随机获取一个视频"""
        # 获取热门视频列表
        hot_url = "https://api.bilibili.com/x/web-interface/popular/series/one"
        hot_data = await self.fetch_json(hot_url)
        if not hot_data or not hot_data.get('data', {}).get('list'):
            logger.error("获取热门视频列表失败")
            return None
        
        hot_list = hot_data['data']['list']
        if not hot_list:
            return None
        
        # 随机挑选一个视频
        random_video = random.choice(hot_list)
        bvid = random_video.get('bvid')
        if not bvid:
            return None
        
        # 获取详细信息
        detail_url = "https://api.bilibili.com/x/web-interface/view"
        detail_data = await self.fetch_json(detail_url, {'bvid': bvid})
        if not detail_data or not detail_data.get('data'):
            return None
        
        v = detail_data['data']
        stats = v.get('stat', {})
        
        return {
            'bvid': v.get('bvid'),
            'title': v.get('title'),
            'owner': v.get('owner', {}).get('name'),
            'pic': v.get('pic'),
            'url': f"https://www.bilibili.com/video/{v.get('bvid')}",
            'view': self.format_number(stats.get('view', 0)),
            'like': self.format_number(stats.get('like', 0)),
            'coin': self.format_number(stats.get('coin', 0)),
            'favorite': self.format_number(stats.get('favorite', 0)),
            'danmaku': self.format_number(stats.get('danmaku', 0))
        }
    
    def format_number(self, num: int) -> str:
        """格式化数字：万/亿"""
        if num >= 100000000:
            return f"{num/100000000:.1f}亿"
        if num >= 10000:
            return f"{num/10000:.1f}万"
        return str(num)
