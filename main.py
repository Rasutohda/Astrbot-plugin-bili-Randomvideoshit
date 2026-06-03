import random
import asyncio
import httpx
import traceback
from typing import Optional, Dict, Any, List
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, At, Image

@register("Astrbot_plugin_bili_Randomvideoshit", "Rasutohda",
          "有人@机器人时随机从B站搬运一个视频（支持分区、切换发送形式、敏感词过滤）", "2.0.1",
          "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit",
          priority=0)
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
        """尝试多种方式获取机器人自身ID"""
        try:
            if hasattr(self.context, 'get_bot_id'):
                self.bot_id = str(self.context.get_bot_id())
                logger.info(f"通过 get_bot_id 获取 bot_id: {self.bot_id}")
            if not self.bot_id and hasattr(self.context, 'get_bot_self_id'):
                self.bot_id = str(self.context.get_bot_self_id())
                logger.info(f"通过 get_bot_self_id 获取 bot_id: {self.bot_id}")
            if not self.bot_id:
                config = self.context.get_config()
                if config and 'bot_qq' in config:
                    self.bot_id = str(config['bot_qq'])
                    logger.info(f"从配置 bot_qq 获取 bot_id: {self.bot_id}")
        except Exception as e:
            logger.warning(f"获取 bot_id 失败: {e}")
        if not self.bot_id:
            logger.warning("未获取到机器人自身ID，@检测将采用宽松模式（任意@均视为@机器人）")

    def load_config(self):
        """加载配置文件（分区、发送形式、敏感词、是否显示处理提示）"""
        config = self.context.get_config()
        if not config:
            return
        
        # 分区配置
        region_conf = config.get("region", None)
        if region_conf:
            rid = self._parse_region(region_conf)
            if rid is not None:
                self.region_id = rid
                logger.info(f"已启用自定义分区: {region_conf} (rid={rid})")
            else:
                logger.warning(f"无效分区配置: {region_conf}，使用全站随机视频")
        else:
            logger.info("未配置分区，使用全站随机视频")

        # 发送形式配置
        send_type_conf = config.get("send_type", "link")
        if send_type_conf in ("link", "image"):
            self.send_type = send_type_conf
            logger.info(f"发送形式: {self.send_type}")
        else:
            logger.warning(f"未知 send_type: {send_type_conf}，使用 link")

        # 敏感词过滤（可选）
        keywords = config.get("block_keywords", [])
        if keywords and isinstance(keywords, list):
            self.block_keywords = [kw.lower() for kw in keywords]
            logger.info(f"已加载 {len(self.block_keywords)} 个敏感词")

        # 是否显示“正在搬运”提示
        self.show_processing = config.get("show_processing", False)
        logger.info(f"显示处理提示: {self.show_processing}")

    def _parse_region(self, region_input) -> Optional[int]:
        """分区名称或rid转整数rid"""
        region_map = {
            "动画": 1, "动漫": 1, "国创": 168, "音乐": 3, "舞蹈": 129,
            "游戏": 4, "知识": 36, "科技": 188, "数码": 147, "生活": 160,
            "美食": 211, "动物圈": 217, "鬼畜": 119, "时尚": 155,
            "娱乐": 5, "影视": 181, "放映厅": 23
        }
        try:
            return int(region_input)
        except (ValueError, TypeError):
            pass
        if isinstance(region_input, str):
            key = region_input.strip()
            if key in region_map:
                return region_map[key]
        return None

    # ------------------- B站API相关 -------------------
    async def _fetch_json(self, url: str, params: dict = None) -> Optional[Dict]:
        """通用GET请求，返回JSON或None"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
                resp = await client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    logger.warning(f"API返回错误码 {data.get('code')}: {data.get('message')}")
                    return None
                return data
        except Exception as e:
            logger.error(f"请求失败 {url}: {e}")
            return None

    async def get_video_details(self, bvid: str) -> Optional[Dict[str, Any]]:
        """根据bvid获取视频详细信息"""
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        data = await self._fetch_json(url)
        if not data or not data.get("data"):
            return None
        video_data = data["data"]
        return {
            "bvid": video_data.get("bvid"),
            "title": video_data.get("title"),
            "owner": video_data.get("owner", {}).get("name"),
            "pic": video_data.get("pic"),
            "url": f"https://www.bilibili.com/video/{video_data.get('bvid')}",
            "stat": video_data.get("stat", {})
        }

    async def get_random_video_from_region(self) -> Optional[Dict[str, Any]]:
        """从指定分区随机获取视频（支持随机翻页）"""
        if self.region_id is None:
            return None
        
        first_page_url = "https://api.bilibili.com/x/web-interface/dynamic/region"
        params = {"rid": self.region_id, "pn": 1, "ps": 30}
        data = await self._fetch_json(first_page_url, params)
        if not data:
            return None
        
        page_info = data.get("data", {}).get("page", {})
        total_pages = page_info.get("pages", 1)
        if total_pages > 50:
            total_pages = 50
        
        random_page = random.randint(1, max(1, total_pages))
        params["pn"] = random_page
        data = await self._fetch_json(first_page_url, params)
        if not data:
            return None
        
        archives = data.get("data", {}).get("archives", [])
        if not archives:
            logger.warning(f"分区 {self.region_id} 第{random_page}页无视频")
            return None
        
        video = random.choice(archives)
        return await self.get_video_details(video.get("bvid"))

    async def get_random_video_from_all(self) -> Optional[Dict[str, Any]]:
        """从全站最新视频中随机获取（随机翻页）"""
        max_page = 100
        random_page = random.randint(1, max_page)
        url = "https://api.bilibili.com/x/web-interface/archive/index"
        params = {"pn": random_page, "ps": 30}
        data = await self._fetch_json(url, params)
        if not data:
            return None
        
        archives = data.get("data", [])
        if not archives:
            params["pn"] = 1
            data = await self._fetch_json(url, params)
            if not data:
                return None
            archives = data.get("data", [])
            if not archives:
                return None
        
        video = random.choice(archives)
        return await self.get_video_details(video.get("bvid"))

    async def get_random_video(self) -> Optional[Dict[str, Any]]:
        """主入口：根据配置获取随机视频（带重试和过滤）"""
        for attempt in range(1, self.max_retries + 1):
            if self.region_id is not None:
                video = await self.get_random_video_from_region()
            else:
                video = await self.get_random_video_from_all()
            
            if not video:
                logger.warning(f"获取视频失败，尝试 {attempt}/{self.max_retries}")
                await asyncio.sleep(0.5)
                continue
            
            if video["bvid"] in self.history_ids:
                logger.info(f"重复视频 {video['bvid']}，重新获取")
                continue
            
            if self.block_keywords:
                title_lower = video["title"].lower()
                owner_lower = video["owner"].lower()
                blocked = any(kw in title_lower or kw in owner_lower for kw in self.block_keywords)
                if blocked:
                    logger.info(f"视频命中敏感词，跳过: {video['title']}")
                    continue
            
            self.history_ids.append(video["bvid"])
            if len(self.history_ids) > 50:
                self.history_ids.pop(0)
            return video
        
        logger.error("达到最大重试次数，未能获取有效视频")
        return None

    # ------------------- 发送回复（使用 event.reply） -------------------
    async def send_video_reply(self, event: AstrMessageEvent, video_info: Dict[str, Any]):
        """根据配置发送视频消息（使用event.reply）"""
        stats = video_info.get("stat", {})
        play_count = stats.get("view", "N/A")
        like_count = stats.get("like", "N/A")
        text = (f"🎬 随机搬运一个B站视频\n"
                f"📺 标题：{video_info['title']}\n"
                f"👤 UP主：{video_info['owner']}\n"
                f"🎉 播放量：{play_count} | 👍 点赞：{like_count}\n"
                f"🔗 链接：{video_info['url']}")
        
        if self.send_type == "image" and video_info.get("pic"):
            try:
                await event.reply(Image.fromURL(video_info["pic"]))
                await event.reply(Plain(text))
                logger.info(f"已发送视频(图片形式): {video_info['title']}")
                return
            except Exception as e:
                logger.error(f"发送封面图片失败: {e}，降级为链接")
        await event.reply(Plain(text))
        logger.info(f"已发送视频(链接形式): {video_info['title']}")

    async def handle_at_message(self, event: AstrMessageEvent):
        """处理@机器人的消息，产出视频回复"""
        logger.info("开始处理@消息")
        try:
            if self.show_processing:
                await event.reply("🎥 正在随机搬运B站视频，请稍候...")

            video_info = await self.get_random_video()
            if not video_info:
                await event.reply("❌ 获取视频失败，请稍后再试")
                return

            await self.send_video_reply(event, video_info)

        except Exception as e:
            logger.error(f"处理@消息异常: {e}\n{traceback.format_exc()}")
            await event.reply(f"❌ 处理出错: {str(e)}")

    # ------------------- 指令：重载配置 -------------------
    @filter(commands=["/bili reload"])
    async def reload_config_command(self, event: AstrMessageEvent):
        """手动重载配置文件（无需重启）"""
        try:
            self.load_config()
            yield event.plain_result("✅ 配置已重新加载")
        except Exception as e:
            logger.error(f"重载配置失败: {e}")
            yield event.plain_result(f"❌ 重载配置失败: {str(e)}")

    # ------------------- 全局过滤器（核心拦截） -------------------
    @filter
    async def message_filter(self, event: AstrMessageEvent) -> bool:
        """检测是否@机器人，若是则处理并阻止其他插件继续处理"""
        logger.debug(f"[Filter] 收到消息: {event.message_str}")
        
        is_at_me = await self._is_at_bot(event)
        
        if is_at_me:
            logger.info("[Filter] 检测到@机器人，开始处理并阻止其他插件")
            await self.handle_at_message(event)
            # 返回 False 表示不继续传递给其他插件
            return False
        else:
            logger.debug("[Filter] 未检测到@机器人，放行")
            return True
    
    async def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """跨平台检测是否@了本机器人"""
        # 优先使用框架内置方法
        if hasattr(event, 'is_at_me'):
            try:
                return event.is_at_me()
            except:
                pass
        if hasattr(event, 'get_at_list'):
            try:
                at_list = event.get_at_list()
                if self.bot_id:
                    return any(str(uid) == self.bot_id for uid in at_list)
                else:
                    return len(at_list) > 0
            except:
                pass
        
        # 手动遍历消息链
        message_chain = event.message_obj.message
        for seg in message_chain:
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
                if not target:
                    target = str(seg)
                
                if target:
                    target_str = str(target)
                    if self.bot_id and target_str == self.bot_id:
                        return True
                    elif not self.bot_id:
                        if target_str.lower() != "all":
                            logger.warning(f"无bot_id，但检测到At目标 {target_str}，视为@机器人")
                            return True
        return False
