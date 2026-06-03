import random
import asyncio
import httpx
import traceback
from typing import Optional, Dict, Any, List
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, At, Image

@register("Astrbot_plugin_bili_Randomvideoshit", "Rasutohda",
          "有人@机器人时随机从B站搬运一个视频（支持分区、切换发送形式）", "1.2.2",
          "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.region_id = None
        self.send_type = "link"
        self.use_single_random = False
        self.history_ids: List[str] = []
        self.bot_id = None
        self._init_bot_id()
        self.load_config()

    def _init_bot_id(self):
        try:
            if hasattr(self.context, 'get_bot_id'):
                self.bot_id = self.context.get_bot_id()
                logger.info(f"通过 get_bot_id 获取 bot_id: {self.bot_id}")
            if not self.bot_id and hasattr(self.context, 'get_bot_self_id'):
                self.bot_id = self.context.get_bot_self_id()
                logger.info(f"通过 get_bot_self_id 获取 bot_id: {self.bot_id}")
            if not self.bot_id:
                config = self.context.get_config()
                if config and 'bot_qq' in config:
                    self.bot_id = str(config['bot_qq'])
                    logger.info(f"从配置 bot_qq 获取 bot_id: {self.bot_id}")
        except Exception as e:
            logger.warning(f"获取 bot_id 失败: {e}")
        if not self.bot_id:
            logger.warning("未获取到机器人自身ID，将采用宽松AT检测")

    def load_config(self):
        config = self.context.get_config()
        if config:
            region_conf = config.get("region", None)
            if region_conf:
                rid = self.parse_region(region_conf)
                if rid is not None:
                    self.region_id = rid
                    logger.info(f"已启用自定义分区: {region_conf} (rid={rid})")
                else:
                    logger.warning(f"无效分区配置: {region_conf}，使用全站排行榜")
            else:
                logger.info("未配置分区，使用全站排行榜")

            send_type_conf = config.get("send_type", "link")
            if send_type_conf in ("link", "image"):
                self.send_type = send_type_conf
                logger.info(f"发送形式: {self.send_type}")
            else:
                logger.warning(f"未知 send_type: {send_type_conf}，使用 link")

    def parse_region(self, region_input) -> Optional[int]:
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

    async def get_random_video_from_region(self) -> Optional[Dict[str, Any]]:
        if self.region_id is None:
            return await self.get_random_video_from_rank()
        api_url = "https://api.bilibili.com/x/web-interface/dynamic/region"
        params = {"rid": self.region_id, "pn": 1, "ps": 50}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
                resp = await client.get(api_url, headers=headers, params=params)
                data = resp.json()
                if data.get("code") != 0:
                    logger.error(f"分区API错误: {data.get('message')}")
                    return None
                archives = data.get("data", {}).get("archives", [])
                if not archives:
                    logger.warning(f"分区 {self.region_id} 无视频数据")
                    return None
                video = random.choice(archives)
                return await self.get_video_details(video.get("bvid"))
        except Exception as e:
            logger.error(f"获取分区视频失败: {e}")
            return None

    async def get_random_video_from_rank(self) -> Optional[Dict[str, Any]]:
        rank_api_url = "https://api.bilibili.com/x/web-interface/ranking/v2"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
                resp = await client.get(rank_api_url, headers=headers)
                data = resp.json()
                if data.get("code") != 0:
                    logger.error(f"排行榜API错误: {data.get('message')}")
                    return None
                rank_list = data.get("data", {}).get("list", [])
                if not rank_list:
                    logger.error("排行榜数据为空")
                    return None
                video = random.choice(rank_list)
                return await self.get_video_details(video.get("bvid"))
        except Exception as e:
            logger.error(f"获取排行榜失败: {e}")
            return None

    async def get_random_video_by_bvid(self) -> Optional[Dict[str, Any]]:
        max_attempts = 10
        base62_chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        for attempt in range(max_attempts):
            random_suffix = ''.join(random.choices(base62_chars, k=10))
            bvid = f"BV{random_suffix}"
            video_info = await self.get_video_details(bvid)
            if video_info:
                return video_info
            await asyncio.sleep(0.5)
        return None

    async def get_video_details(self, bvid: str) -> Optional[Dict[str, Any]]:
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
                resp = await client.get(api_url, headers=headers)
                data = resp.json()
                if data.get("code") != 0:
                    return None
                video_data = data.get("data")
                if not video_data:
                    return None
                return {
                    "bvid": video_data.get("bvid"),
                    "title": video_data.get("title"),
                    "owner": video_data.get("owner", {}).get("name"),
                    "pic": video_data.get("pic"),
                    "url": f"https://www.bilibili.com/video/{video_data.get('bvid')}",
                    "stat": video_data.get("stat", {})
                }
        except Exception:
            return None

    async def get_random_video(self) -> Optional[Dict[str, Any]]:
        if self.use_single_random:
            return await self.get_random_video_by_bvid()
        else:
            if self.region_id is not None:
                return await self.get_random_video_from_region()
            else:
                return await self.get_random_video_from_rank()

    async def send_video_as_link(self, event: AstrMessageEvent, video_info: Dict[str, Any]):
        stats = video_info.get("stat", {})
        play_count = stats.get("view", "N/A")
        like_count = stats.get("like", "N/A")
        text = (f"🎬 随机搬运一个B站视频\n"
                f"📺 标题：{video_info['title']}\n"
                f"👤 UP主：{video_info['owner']}\n"
                f"🎉 播放量：{play_count} | 👍 点赞：{like_count}\n"
                f"🔗 链接：{video_info['url']}")
        yield event.plain_result(text)
        logger.info(f"已发送视频(链接形式): {video_info['title']}")

    async def send_video_as_image(self, event: AstrMessageEvent, video_info: Dict[str, Any]):
        stats = video_info.get("stat", {})
        play_count = stats.get("view", "N/A")
        like_count = stats.get("like", "N/A")
        text = (f"🎬 随机搬运一个B站视频\n"
                f"📺 标题：{video_info['title']}\n"
                f"👤 UP主：{video_info['owner']}\n"
                f"🎉 播放量：{play_count} | 👍 点赞：{like_count}\n"
                f"🔗 链接：{video_info['url']}")
        pic_url = video_info.get("pic")
        if pic_url:
            try:
                yield event.chain_result([Image.fromURL(pic_url), Plain(f"\n{text}")])
                logger.info(f"已发送视频(图片形式): {video_info['title']}")
                return
            except Exception as e:
                logger.error(f"发送封面图片失败: {e}，降级为链接")
        yield event.plain_result(text)

    async def on_at_message(self, event: AstrMessageEvent):
        """处理@消息 - 带详细日志和异常捕获"""
        logger.info("=== 进入 on_at_message 方法 ===")
        try:
            # 发送处理中状态
            logger.info("尝试发送 '正在获取视频...' 消息")
            yield event.plain_result("🎥 正在随机搬运B站视频，请稍候...")
            logger.info("已发送处理中提示")

            logger.info("开始调用 get_random_video()")
            video_info = await self.get_random_video()
            logger.info(f"get_random_video 返回: {video_info is not None}")

            if not video_info:
                logger.warning("获取视频失败，发送错误提示")
                yield event.plain_result("❌ 获取视频失败，请稍后再试")
                return

            # 去重检查
            if video_info["bvid"] in self.history_ids:
                logger.info(f"重复视频 {video_info['bvid']}，重新获取")
                video_info = await self.get_random_video()
                if not video_info:
                    yield event.plain_result("❌ 获取视频失败，请稍后再试")
                    return

            self.history_ids.append(video_info["bvid"])
            if len(self.history_ids) > 50:
                self.history_ids.pop(0)

            # 根据配置发送
            if self.send_type == "image":
                logger.info("使用图片形式发送")
                await self.send_video_as_image(event, video_info)
            else:
                logger.info("使用链接形式发送")
                await self.send_video_as_link(event, video_info)

            logger.info("=== on_at_message 执行完成 ===")
        except Exception as e:
            logger.error(f"on_at_message 发生异常: {e}\n{traceback.format_exc()}")
            try:
                yield event.plain_result(f"❌ 处理出错: {str(e)}")
            except:
                pass

    async def on_message(self, event: AstrMessageEvent):
        """消息入口：判断是否被AT"""
        logger.debug(f"收到消息: {event.message_str}")
        message_chain = event.message_obj.message
        is_at_me = False
        for segment in message_chain:
            if isinstance(segment, At):
                target = None
                if hasattr(segment, 'data') and segment.data:
                    target = segment.data.get("qq") or segment.data.get("user_id") or segment.data.get("target")
                elif hasattr(segment, 'qq'):
                    target = segment.qq
                elif hasattr(segment, 'user_id'):
                    target = segment.user_id
                else:
                    target = str(segment) if segment else None
                logger.debug(f"发现 At 段, target={target}, bot_id={self.bot_id}")
                if target:
                    target_str = str(target)
                    if self.bot_id and target_str == str(self.bot_id):
                        is_at_me = True
                        break
                    elif not self.bot_id and target_str.lower() != "all":
                        is_at_me = True
                        logger.warning(f"未获取到bot_id，但检测到At目标 {target_str}，将处理该消息")
                        break
        if is_at_me:
            logger.info("检测到@机器人的消息，开始处理")
            async for result in self.on_at_message(event):
                yield result
        else:
            logger.debug("未检测到@机器人的消息，忽略")
