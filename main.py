import os
import random
import asyncio
import json
import logging
import re
import aiohttp
from typing import Optional, Dict, Any, List
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import *

logger = logging.getLogger(__name__)

@register("astrbot_plugin_bili_random", "Rasutohda",
          "有人@机器人时随机搬运B站视频", "2.0.9",
          "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.bot_id = None
        self.bot_name = None
        self.config = {
            "region": None,
            "send_type": "link",
            "block_keywords": [],
            "show_processing": False
        }
        self.history_ids = []  # 去重用
        self._init_bot_info()
        self.load_config()

    def _init_bot_info(self):
        """获取机器人ID和昵称"""
        try:
            if hasattr(self.context, 'get_bot_id'):
                self.bot_id = str(self.context.get_bot_id())
            elif hasattr(self.context, 'get_bot_self_id'):
                self.bot_id = str(self.context.get_bot_self_id())
            else:
                global_config = self.context.get_config()
                if global_config and 'bot_qq' in global_config:
                    self.bot_id = str(global_config['bot_qq'])
            if hasattr(self.context, 'get_bot_nickname'):
                self.bot_name = self.context.get_bot_nickname()
        except Exception as e:
            logger.warning(f"获取机器人信息失败: {e}")
        logger.info(f"机器人 ID: {self.bot_id}, 昵称: {self.bot_name}")

    def load_config(self):
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    self.config.update({
                        "region": user_config.get("region"),
                        "send_type": user_config.get("send_type", "link"),
                        "block_keywords": user_config.get("block_keywords", []),
                        "show_processing": user_config.get("show_processing", False)
                    })
                logger.info("配置文件加载成功")
            except Exception as e:
                logger.warning(f"配置文件加载失败: {e}")

    # ---------- B站 API ----------
    async def fetch_json(self, url: str, params: dict = None):
        try:
            async with aiohttp.ClientSession(timeout=10) as session:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
                async with session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data
                    logger.warning(f"API错误: {data.get('message')}")
                    return None
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return None

    async def get_video_details(self, bvid: str):
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

    def _parse_region(self, region_input):
        region_map = {
            "动画":1,"动漫":1,"国创":168,"音乐":3,"舞蹈":129,"游戏":4,
            "知识":36,"科技":188,"数码":147,"生活":160,"美食":211,
            "动物圈":217,"鬼畜":119,"时尚":155,"娱乐":5,"影视":181,"放映厅":23
        }
        try:
            return int(region_input)
        except:
            return region_map.get(region_input.strip()) if isinstance(region_input, str) else None

    async def get_random_video_from_region(self):
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

    async def get_random_video_from_all(self):
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

    async def get_random_video(self):
        for attempt in range(8):
            if self.config["region"]:
                video = await self.get_random_video_from_region()
            else:
                video = await self.get_random_video_from_all()
            if not video:
                await asyncio.sleep(0.5)
                continue
            # 去重（最后一次尝试接受重复）
            if video["bvid"] in self.history_ids and attempt < 7:
                continue
            # 敏感词
            if self.config["block_keywords"]:
                text = (video["title"] + video["owner"]).lower()
                if any(kw.lower() in text for kw in self.config["block_keywords"]):
                    continue
            self.history_ids.append(video["bvid"])
            if len(self.history_ids) > 50:
                self.history_ids.pop(0)
            return video
        return None

    async def send_video_message(self, event: AstrMessageEvent, video: dict):
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
                logger.error(f"图片发送失败: {e}")
        await event.send(event.plain_result(text))

    # ---------- 核心处理 ----------
    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> Optional[MessageEventResult]:
        try:
            msg = event.message_str.strip()
            logger.info(f"收到消息: {msg}")

            # 命令：!bili 或 /bili reload（不区分大小写）
            if msg.lower() in ["!bili", "/bili reload"]:
                logger.info("执行命令，重新加载配置并返回随机视频")
                self.load_config()  # 重载配置
                # 发送处理中提示（如果开启）
                if self.config["show_processing"]:
                    await event.send(event.plain_result("🎥 正在获取视频..."))
                video = await self.get_random_video()
                if video:
                    await self.send_video_message(event, video)
                else:
                    await event.send(event.plain_result("❌ 获取视频失败，请稍后重试"))
                return None

            # 检测是否@了机器人
            if await self._is_at_bot(event):
                logger.info("检测到@机器人，开始处理")
                if self.config["show_processing"]:
                    await event.send(event.plain_result("🎥 正在获取视频..."))
                video = await self.get_random_video()
                if video:
                    await self.send_video_message(event, video)
                else:
                    await event.send(event.plain_result("❌ 获取视频失败，请稍后重试"))
            else:
                logger.debug("未检测到@机器人，忽略")
        except Exception as e:
            logger.error(f"处理异常: {e}", exc_info=True)
            try:
                await event.send(event.plain_result(f"❌ 出错: {str(e)}"))
            except:
                pass
        return None

    async def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """检测是否@了机器人（多重检测）"""
        # 方法1：框架内置方法
        if hasattr(event, 'is_at_me'):
            try:
                if event.is_at_me():
                    logger.debug("is_at_me() 返回 True")
                    return True
            except:
                pass
        # 方法2：遍历消息组件中的 At
        for seg in event.message_obj.message:
            if isinstance(seg, At):
                target = None
                if hasattr(seg, 'data') and seg.data:
                    target = seg.data.get("qq") or seg.data.get("user_id") or seg.data.get("target")
                if not target and hasattr(seg, 'qq'):
                    target = seg.qq
                if not target and hasattr(seg, 'user_id'):
                    target = seg.user_id
                if target:
                    logger.debug(f"找到At目标: {target}")
                    if self.bot_id and str(target) == self.bot_id:
                        return True
                    if not self.bot_id:
                        return True  # 宽松模式
        # 方法3：消息文本中包含 @机器人ID 或 @机器人昵称
        msg = event.message_str
        if self.bot_id and f"@{self.bot_id}" in msg:
            logger.debug("文本匹配 @机器人ID")
            return True
        if self.bot_name and f"@{self.bot_name}" in msg:
            logger.debug("文本匹配 @机器人昵称")
            return True
        # 方法4：如果消息开头就是机器人昵称（没有@符号，可能某些平台）
        if self.bot_name and msg.startswith(self.bot_name):
            logger.debug("消息开头是机器人昵称")
            return True
        return False
