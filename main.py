import os
import random
import asyncio
import json
import logging
import aiohttp
from typing import Optional, Dict, Any, List
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import *

logger = logging.getLogger(__name__)

@register("astrbot_plugin_bili_random", "Rasutohda",
          "有人@机器人时随机搬运B站视频（支持分区/图片/敏感词）", "2.0.8",
          "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = {
            "region": None,
            "send_type": "link",
            "block_keywords": [],
            "show_processing": False
        }
        self.history_ids: List[str] = []
        self.bot_id = None
        self.bot_name = None
        self.load_config()
        self._init_bot_info()

    def _init_bot_info(self):
        """获取机器人自身ID和名称"""
        try:
            if hasattr(self.context, 'get_bot_id'):
                self.bot_id = str(self.context.get_bot_id())
            elif hasattr(self.context, 'get_bot_self_id'):
                self.bot_id = str(self.context.get_bot_self_id())
            else:
                global_config = self.context.get_config()
                if global_config and 'bot_qq' in global_config:
                    self.bot_id = str(global_config['bot_qq'])
            # 尝试获取机器人昵称
            if hasattr(self.context, 'get_bot_nickname'):
                self.bot_name = self.context.get_bot_nickname()
        except Exception as e:
            logger.warning(f"获取机器人信息失败: {e}")
        if not self.bot_id:
            logger.warning("未获取到机器人ID，将使用宽松@检测")

    def load_config(self):
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    self.config.update({
                        "region": user_config.get("region", self.config["region"]),
                        "send_type": user_config.get("send_type", self.config["send_type"]),
                        "block_keywords": user_config.get("block_keywords", self.config["block_keywords"]),
                        "show_processing": user_config.get("show_processing", self.config["show_processing"])
                    })
                logger.info("配置文件加载成功")
            except Exception as e:
                logger.warning(f"配置文件加载失败: {e}")

    # ------------------- B站API -------------------
    async def fetch_json(self, url: str, params: dict = None) -> Optional[Dict]:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
                async with session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data
                    logger.warning(f"API错误: {data.get('message')}")
                    return None
        except Exception as e:
            logger.error(f"请求失败 {url}: {e}")
            return None

    async def get_video_details(self, bvid: str) -> Optional[Dict[str, Any]]:
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        data = await self.fetch_json(url)
        if not data or not data.get("data"):
            return None
        v = data["data"]
        return {
            "bvid": v.get("bvid"),
            "title": v.get("title"),
            "owner": v.get("owner", {}).get("name"),
            "pic": v.get("pic"),
            "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
            "stat": v.get("stat", {})
        }

    def _parse_region(self, region_input) -> Optional[int]:
        region_map = {
            "动画":1, "动漫":1, "国创":168, "音乐":3, "舞蹈":129,
            "游戏":4, "知识":36, "科技":188, "数码":147, "生活":160,
            "美食":211, "动物圈":217, "鬼畜":119, "时尚":155,
            "娱乐":5, "影视":181, "放映厅":23
        }
        try:
            return int(region_input)
        except (ValueError, TypeError):
            return region_map.get(region_input.strip()) if isinstance(region_input, str) else None

    async def get_random_video_from_region(self) -> Optional[Dict]:
        rid = self._parse_region(self.config["region"])
        if rid is None:
            return None
        page = random.randint(1, 50)
        url = "https://api.bilibili.com/x/web-interface/dynamic/region"
        params = {"rid": rid, "pn": page, "ps": 30}
        data = await self.fetch_json(url, params)
        if data and data.get("data", {}).get("archives"):
            archives = data["data"]["archives"]
            if archives:
                video = random.choice(archives)
                return await self.get_video_details(video.get("bvid"))
        return None

    async def get_random_video_from_all(self) -> Optional[Dict]:
        page = random.randint(1, 100)
        url = "https://api.bilibili.com/x/web-interface/archive/index"
        params = {"pn": page, "ps": 30}
        data = await self.fetch_json(url, params)
        if data and data.get("data"):
            archives = data["data"]
            if archives:
                video = random.choice(archives)
                return await self.get_video_details(video.get("bvid"))
        return None

    async def get_random_video(self) -> Optional[Dict]:
        max_attempts = 8
        for attempt in range(max_attempts):
            if self.config["region"]:
                video = await self.get_random_video_from_region()
            else:
                video = await self.get_random_video_from_all()
            if not video:
                logger.warning(f"获取视频失败，尝试 {attempt+1}/{max_attempts}")
                await asyncio.sleep(0.5)
                continue
            # 去重：如果重复，但已经是最后一次尝试，则接受重复
            if video["bvid"] in self.history_ids:
                if attempt < max_attempts - 1:
                    logger.info(f"重复视频 {video['bvid']}，重试")
                    continue
                else:
                    logger.warning(f"重复视频，但已达最大尝试次数，接受重复")
            # 敏感词过滤
            if self.config["block_keywords"]:
                title_lower = video["title"].lower()
                owner_lower = video["owner"].lower()
                if any(kw.lower() in title_lower or kw.lower() in owner_lower
                       for kw in self.config["block_keywords"]):
                    logger.info(f"命中敏感词: {video['title']}，重试")
                    continue
            # 记录并返回
            self.history_ids.append(video["bvid"])
            if len(self.history_ids) > 50:
                self.history_ids.pop(0)
            return video
        return None

    async def send_video_message(self, event: AstrMessageEvent, video: Dict):
        stats = video.get("stat", {})
        text = (f"🎬 随机搬运一个B站视频\n"
                f"📺 标题：{video['title']}\n"
                f"👤 UP主：{video['owner']}\n"
                f"🎉 播放量：{stats.get('view', 'N/A')} | 👍 点赞：{stats.get('like', 'N/A')}\n"
                f"🔗 链接：{video['url']}")
        
        if self.config["send_type"] == "image" and video.get("pic"):
            try:
                chain = [Image.fromURL(video["pic"]), Plain(f"\n{text}")]
                await event.send(event.make_result().message(chain))
                return
            except Exception as e:
                logger.error(f"图片发送失败: {e}，降级为文本")
        await event.send(event.plain_result(text))

    # ------------------- 核心事件处理 -------------------
    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> Optional[MessageEventResult]:
        """处理所有消息，返回 None 表示不额外回复（但已用 send 发送）"""
        try:
            msg_text = event.message_str.strip()
            logger.debug(f"收到消息: {msg_text}")
            
            # 命令：重载配置（不需要@）
            if msg_text == "/bili reload":
                self.load_config()
                await event.send(event.plain_result("✅ 配置已重新加载"))
                return None
            
            # 检测是否@机器人
            if not await self._is_at_bot(event):
                logger.debug("未检测到@机器人，忽略")
                return None
            
            logger.info("检测到@机器人，开始处理")
            
            # 可选：发送处理中提示
            if self.config["show_processing"]:
                await event.send(event.plain_result("🎥 正在随机搬运B站视频，请稍候..."))
            
            video = await self.get_random_video()
            if video:
                await self.send_video_message(event, video)
                logger.info(f"成功发送视频: {video['title']}")
            else:
                error_msg = "❌ 获取视频失败，请稍后再试（可能网络问题或暂无视频）"
                await event.send(event.plain_result(error_msg))
                logger.warning("获取视频失败，已发送错误提示")
            
            return None
        
        except Exception as e:
            logger.error(f"消息处理异常: {e}", exc_info=True)
            try:
                await event.send(event.plain_result(f"❌ 插件出错: {str(e)}"))
            except:
                pass
            return None

    async def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """检测是否@机器人（支持多种方式）"""
        # 方法1：内置API
        if hasattr(event, 'is_at_me'):
            try:
                return event.is_at_me()
            except:
                pass
        
        # 方法2：遍历At组件
        for seg in event.message_obj.message:
            if isinstance(seg, At):
                target = None
                if hasattr(seg, 'data') and seg.data:
                    target = seg.data.get("qq") or seg.data.get("user_id") or seg.data.get("target")
                if not target and hasattr(seg, 'qq'):
                    target = seg.qq
                if not target and hasattr(seg, 'user_id'):
                    target = seg.user_id
                if not target and hasattr(seg, 'target'):
                    target = seg.target
                if target:
                    if self.bot_id and str(target) == self.bot_id:
                        return True
                    if not self.bot_id:
                        return True  # 宽松模式：有任何@就响应
        
        # 方法3：文本匹配（针对某些平台没有At组件的情况）
        msg = event.message_str
        if self.bot_id and f"@{self.bot_id}" in msg:
            return True
        if self.bot_name and f"@{self.bot_name}" in msg:
            return True
        # 常见格式：@机器人昵称
        if self.bot_name and self.bot_name in msg:
            # 避免误判（如果消息中出现了机器人昵称但不是@，可能误触发）
            # 简单处理：如果昵称在消息开头或前面有@符号
            if f"@{self.bot_name}" in msg or msg.startswith(self.bot_name):
                return True
        
        return False
