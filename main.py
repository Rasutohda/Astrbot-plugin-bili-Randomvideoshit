import random
import asyncio
import httpx
import traceback
from typing import Optional, Dict, Any, List
from astrbot.api.event import AstrMessageEvent, event_handler
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, At, Image

@register("astrbot_plugin_bili_random", "Rasutohda",
          "有人@机器人时随机搬运B站视频（分区/图片/敏感词）", "2.0.4",
          "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 配置项
        self.region_id = None
        self.send_type = "link"
        self.history_ids: List[str] = []
        self.bot_id = None
        self.block_keywords: List[str] = []
        self.show_processing = False
        self.max_retries = 5
        
        self._init_bot_id()
        self.load_config()

    def _init_bot_id(self):
        """获取机器人自身ID"""
        try:
            if hasattr(self.context, 'get_bot_id'):
                self.bot_id = str(self.context.get_bot_id())
            elif hasattr(self.context, 'get_bot_self_id'):
                self.bot_id = str(self.context.get_bot_self_id())
            else:
                config = self.context.get_config()
                if config and 'bot_qq' in config:
                    self.bot_id = str(config['bot_qq'])
        except Exception as e:
            logger.warning(f"获取 bot_id 失败: {e}")
        if not self.bot_id:
            logger.warning("未获取到机器人ID，将宽松匹配@")

    def load_config(self):
        """加载配置"""
        config = self.context.get_config()
        if not config:
            return
        # 分区
        region_conf = config.get("region")
        if region_conf:
            rid = self._parse_region(region_conf)
            if rid:
                self.region_id = rid
                logger.info(f"启用分区: {region_conf} (rid={rid})")
        # 发送形式
        send_type_conf = config.get("send_type", "link")
        if send_type_conf in ("link", "image"):
            self.send_type = send_type_conf
        # 敏感词
        keywords = config.get("block_keywords", [])
        if isinstance(keywords, list):
            self.block_keywords = [kw.lower() for kw in keywords]
        # 提示开关
        self.show_processing = config.get("show_processing", False)
        logger.info(f"配置加载完成: send_type={self.send_type}, 敏感词数={len(self.block_keywords)}")

    def _parse_region(self, region_input) -> Optional[int]:
        region_map = {
            "动画":1,"动漫":1,"国创":168,"音乐":3,"舞蹈":129,"游戏":4,
            "知识":36,"科技":188,"数码":147,"生活":160,"美食":211,
            "动物圈":217,"鬼畜":119,"时尚":155,"娱乐":5,"影视":181,"放映厅":23
        }
        try:
            return int(region_input)
        except:
            return region_map.get(region_input.strip())

    # ------------------- B站API -------------------
    async def _fetch_json(self, url: str, params: dict = None) -> Optional[Dict]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
                resp = await client.get(url, headers=headers, params=params)
                data = resp.json()
                if data.get("code") == 0:
                    return data
                logger.warning(f"API错误 {data.get('code')}: {data.get('message')}")
                return None
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return None

    async def get_video_details(self, bvid: str) -> Optional[Dict]:
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        data = await self._fetch_json(url)
        if data and data.get("data"):
            v = data["data"]
            return {
                "bvid": v.get("bvid"),
                "title": v.get("title"),
                "owner": v.get("owner", {}).get("name"),
                "pic": v.get("pic"),
                "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
                "stat": v.get("stat", {})
            }
        return None

    async def get_random_video_from_region(self) -> Optional[Dict]:
        if not self.region_id:
            return None
        url = "https://api.bilibili.com/x/web-interface/dynamic/region"
        params = {"rid": self.region_id, "pn": random.randint(1, 50), "ps": 30}
        data = await self._fetch_json(url, params)
        if data and data.get("data", {}).get("archives"):
            archives = data["data"]["archives"]
            video = random.choice(archives)
            return await self.get_video_details(video.get("bvid"))
        return None

    async def get_random_video_from_all(self) -> Optional[Dict]:
        url = "https://api.bilibili.com/x/web-interface/archive/index"
        params = {"pn": random.randint(1, 100), "ps": 30}
        data = await self._fetch_json(url, params)
        if data and data.get("data"):
            archives = data["data"]
            if archives:
                video = random.choice(archives)
                return await self.get_video_details(video.get("bvid"))
        return None

    async def get_random_video(self) -> Optional[Dict]:
        for attempt in range(self.max_retries):
            if self.region_id:
                video = await self.get_random_video_from_region()
            else:
                video = await self.get_random_video_from_all()
            if not video:
                await asyncio.sleep(0.5)
                continue
            # 去重
            if video["bvid"] in self.history_ids:
                continue
            # 敏感词过滤
            if self.block_keywords:
                title_lower = video["title"].lower()
                owner_lower = video["owner"].lower()
                if any(kw in title_lower or kw in owner_lower for kw in self.block_keywords):
                    continue
            self.history_ids.append(video["bvid"])
            if len(self.history_ids) > 50:
                self.history_ids.pop(0)
            return video
        return None

    # ------------------- 回复 -------------------
    async def send_video(self, event: AstrMessageEvent, video: Dict):
        stats = video.get("stat", {})
        text = (f"🎬 随机B站视频\n"
                f"📺 {video['title']}\n"
                f"👤 {video['owner']}\n"
                f"🎉 播放:{stats.get('view','N/A')} 👍 {stats.get('like','N/A')}\n"
                f"🔗 {video['url']}")
        if self.send_type == "image" and video.get("pic"):
            try:
                await event.reply(Image.fromURL(video["pic"]))
                await event.reply(Plain(text))
                return
            except Exception as e:
                logger.error(f"图片发送失败: {e}")
        await event.reply(Plain(text))

    # ------------------- 事件处理（核心）-------------------
    @event_handler
    async def on_message(self, event: AstrMessageEvent) -> bool:
        """处理所有消息，返回True放行，False拦截"""
        msg = event.message_str.strip()
        # 重载命令
        if msg == "/bili reload":
            self.load_config()
            await event.reply("✅ 配置已重载")
            return False

        # 检查是否@机器人
        if not await self._is_at_me(event):
            return True   # 不是@我，放行

        # 处理@消息
        if self.show_processing:
            await event.reply("🎥 正在随机搬运，请稍等...")
        video = await self.get_random_video()
        if video:
            await self.send_video(event, video)
        else:
            await event.reply("❌ 获取视频失败，请稍后再试")
        return False   # 拦截消息，不再传递给其他插件

    async def _is_at_me(self, event: AstrMessageEvent) -> bool:
        """检测是否被@"""
        # 尝试内置方法
        if hasattr(event, 'is_at_me'):
            try:
                return event.is_at_me()
            except:
                pass
        # 手动解析At段
        for seg in event.message_obj.message:
            if isinstance(seg, At):
                target = None
                if hasattr(seg, 'data') and seg.data:
                    target = seg.data.get("qq") or seg.data.get("user_id")
                if not target and hasattr(seg, 'qq'):
                    target = seg.qq
                if not target and hasattr(seg, 'user_id'):
                    target = seg.user_id
                if target:
                    if self.bot_id and str(target) == self.bot_id:
                        return True
                    if not self.bot_id:
                        return True  # 宽松匹配
        return False
