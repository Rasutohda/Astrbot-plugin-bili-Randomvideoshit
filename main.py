import os
import random
import asyncio
import json
import logging
import aiohttp
from typing import Optional, Dict, Any, List
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import *
from astrbot.api import logger

# 配置日志
logger = logging.getLogger(__name__)

@register("astrbot_plugin_bili_random", "Rasutohda",
          "有人@机器人时随机搬运B站视频（支持分区/图片/敏感词）", "2.0.6",
          "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 默认配置
        self.config = {
            "region": None,          # 分区名称或rid
            "send_type": "link",     # link 或 image
            "block_keywords": [],    # 敏感词列表
            "show_processing": False # 是否显示“正在搬运”
        }
        self.history_ids: List[str] = []   # 已发送的视频bvid（去重）
        self.bot_id = None                 # 机器人自身ID
        self.load_config()
        self._init_bot_id()

    def _init_bot_id(self):
        """获取机器人自身ID（用于@检测）"""
        try:
            # 尝试多种方式获取 bot_id
            if hasattr(self.context, 'get_bot_id'):
                self.bot_id = str(self.context.get_bot_id())
            elif hasattr(self.context, 'get_bot_self_id'):
                self.bot_id = str(self.context.get_bot_self_id())
            else:
                # 从全局配置读取 bot_qq
                global_config = self.context.get_config()
                if global_config and 'bot_qq' in global_config:
                    self.bot_id = str(global_config['bot_qq'])
        except Exception as e:
            logger.warning(f"获取 bot_id 失败: {e}")
        if not self.bot_id:
            logger.warning("未获取到机器人ID，将使用宽松@检测（只要有@就响应）")

    def load_config(self):
        """加载插件配置文件（如果存在）"""
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    # 更新配置
                    if "region" in user_config:
                        self.config["region"] = user_config["region"]
                    if "send_type" in user_config:
                        self.config["send_type"] = user_config["send_type"]
                    if "block_keywords" in user_config:
                        self.config["block_keywords"] = user_config["block_keywords"]
                    if "show_processing" in user_config:
                        self.config["show_processing"] = user_config["show_processing"]
                logger.info("配置文件加载成功")
            except Exception as e:
                logger.warning(f"配置文件加载失败: {e}")
        else:
            logger.info("未找到配置文件，使用默认配置")

    # ------------------- B站API相关 -------------------
    async def fetch_json(self, url: str, params: dict = None) -> Optional[Dict]:
        """异步GET请求，返回JSON或None"""
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
                    else:
                        logger.warning(f"API返回错误: {data.get('message')}")
                        return None
        except Exception as e:
            logger.error(f"请求失败 {url}: {e}")
            return None

    async def get_video_details(self, bvid: str) -> Optional[Dict[str, Any]]:
        """根据bvid获取视频详细信息"""
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
        """将分区名称或数字rid转换为整数rid"""
        region_map = {
            "动画":1, "动漫":1, "国创":168, "音乐":3, "舞蹈":129,
            "游戏":4, "知识":36, "科技":188, "数码":147, "生活":160,
            "美食":211, "动物圈":217, "鬼畜":119, "时尚":155,
            "娱乐":5, "影视":181, "放映厅":23
        }
        try:
            return int(region_input)
        except (ValueError, TypeError):
            pass
        if isinstance(region_input, str):
            return region_map.get(region_input.strip())
        return None

    async def get_random_video_from_region(self) -> Optional[Dict]:
        """从指定分区随机获取视频"""
        rid = self._parse_region(self.config["region"])
        if rid is None:
            return None
        # 随机翻页（1~50页）
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
        """从全站最新视频中随机获取"""
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
        """主入口：获取随机视频（带重试、去重、敏感词过滤）"""
        max_retries = 5
        for _ in range(max_retries):
            if self.config["region"]:
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
            if self.config["block_keywords"]:
                title_lower = video["title"].lower()
                owner_lower = video["owner"].lower()
                blocked = any(kw.lower() in title_lower or kw.lower() in owner_lower
                             for kw in self.config["block_keywords"])
                if blocked:
                    continue
            # 通过检查
            self.history_ids.append(video["bvid"])
            if len(self.history_ids) > 50:
                self.history_ids.pop(0)
            return video
        return None

    # ------------------- 消息发送 -------------------
    async def send_video_message(self, event: AstrMessageEvent, video: Dict):
        """根据配置发送视频信息（链接或图片）"""
        stats = video.get("stat", {})
        text = (f"🎬 随机搬运一个B站视频\n"
                f"📺 标题：{video['title']}\n"
                f"👤 UP主：{video['owner']}\n"
                f"🎉 播放量：{stats.get('view', 'N/A')} | 👍 点赞：{stats.get('like', 'N/A')}\n"
                f"🔗 链接：{video['url']}")
        
        if self.config["send_type"] == "image" and video.get("pic"):
            try:
                # 发送图片 + 文本（使用消息链）
                chain = [Image.fromURL(video["pic"]), Plain(f"\n{text}")]
                message = event.make_result().message(chain)
                await event.send(message)
                return
            except Exception as e:
                logger.error(f"图片发送失败: {e}，降级为纯文本")
        # 默认发送纯文本
        await event.send(event.plain_result(text))

    # ------------------- 核心事件处理 -------------------
    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> MessageEventResult:
        """处理所有消息事件"""
        try:
            msg_text = event.message_str.strip()
            
            # 命令：重载配置
            if msg_text == "/bili reload":
                self.load_config()
                return event.plain_result("✅ 配置已重新加载")
            
            # 检测是否@了机器人
            is_at_me = await self._is_at_bot(event)
            if not is_at_me:
                # 不是@我的消息，放行（不处理）
                return event.plain_result(None)  # 返回 None 表示不回复但继续传播？
                # 注意：返回 None 可能不会阻止后续插件，但参考原插件直接 return event.plain_result 会回复空消息
                # 为了不发送多余消息，我们直接返回 event.plain_result("") 或者不回复。
                # 但原插件在不需要回复时使用了 return event.plain_result(None) 不发送任何消息。
                # 我们直接 return None 或 event.plain_result("") 更合适。
                # 然而必须返回 MessageEventResult 类型，故返回 event.plain_result("") 不会发送消息内容但会生成一个空回复。
                # 实际上，原 lolicon 插件在没有匹配关键词时直接返回 None 不产生任何回复。我们模仿：返回 None。
                # 但是函数签名要求返回 MessageEventResult，返回 None 可能不行。稳妥起见，返回 event.plain_result("") 不显示内容。
                return event.plain_result("")
            
            # 处理@消息
            if self.config["show_processing"]:
                await event.send(event.plain_result("🎥 正在随机搬运B站视频，请稍候..."))
            
            video = await self.get_random_video()
            if video:
                await self.send_video_message(event, video)
            else:
                await event.send(event.plain_result("❌ 获取视频失败，请稍后再试"))
            
            # 返回空消息（表示已经处理，但不需要额外回复）
            return event.plain_result("")
        
        except Exception as e:
            logger.error(f"消息处理异常: {e}")
            return event.plain_result(f"❌ 插件出错: {str(e)}")

    async def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """检测消息中是否@了本机器人"""
        # 方法1：尝试 event.is_at_me()
        if hasattr(event, 'is_at_me'):
            try:
                return event.is_at_me()
            except:
                pass
        # 方法2：手动解析消息链中的 At 组件
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
                        # 宽松模式：只要有 @ 就响应（避免漏掉）
                        return True
        return False
