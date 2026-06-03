import random
import json
import time
import asyncio
import httpx
from typing import Optional, Dict, Any, List
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Video, Plain, At, Image

@register("Astrbot_plugin_bili_Randomvideoshit", "Rasutohda",
          "有人@机器人时随机从B站搬运一个视频（支持分区、切换发送形式）", "1.2.0",
          "https://github.com/your_username/Astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 配置项
        self.region_id = None
        self.send_type = "link"   # 可选 link / image
        self.use_single_random = False
        self.history_ids: List[str] = []
        self.load_config()

    def load_config(self):
        """加载用户配置（分区 + 发送形式）"""
        config = self.context.get_config()
        if config:
            # 分区配置
            region_conf = config.get("region", None)
            if region_conf:
                rid = self.parse_region(region_conf)
                if rid is not None:
                    self.region_id = rid
                    logger.info(f"已启用自定义分区: {region_conf} (rid={rid})")
                else:
                    logger.warning(f"无效的分区配置: {region_conf}，将使用全站排行榜")
            else:
                logger.info("未配置分区，使用全站排行榜")

            # 发送形式配置
            send_type_conf = config.get("send_type", "link")
            if send_type_conf in ("link", "image"):
                self.send_type = send_type_conf
                logger.info(f"发送形式设置为: {self.send_type}")
            else:
                logger.warning(f"未知的 send_type: {send_type_conf}，使用默认 link")

    def parse_region(self, region_input) -> Optional[int]:
        """将分区名称或 rid 转换为整数的 rid"""
        region_map = {
            "动画": 1, "动漫": 1,
            "国创": 168,
            "音乐": 3,
            "舞蹈": 129,
            "游戏": 4,
            "知识": 36,
            "科技": 188,
            "数码": 147,
            "生活": 160,
            "美食": 211,
            "动物圈": 217,
            "鬼畜": 119,
            "时尚": 155,
            "娱乐": 5,
            "影视": 181,
            "放映厅": 23
        }
        try:
            rid = int(region_input)
            return rid
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
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
                resp = await client.get(api_url, headers=headers, params=params)
                data = resp.json()
                if data.get("code") != 0:
                    logger.error(f"分区API错误: {data.get('message')}")
                    return None
                archives = data.get("data", {}).get("archives", [])
                if not archives:
                    logger.warning(f"分区 {self.region_id} 没有视频数据")
                    return None
                video = random.choice(archives)
                return await self.get_video_details(video.get("bvid"))
        except Exception as e:
            logger.error(f"获取分区视频失败: {str(e)}")
            return None

    async def get_random_video_from_rank(self) -> Optional[Dict[str, Any]]:
        rank_api_url = "https://api.bilibili.com/x/web-interface/ranking/v2"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
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
            logger.error(f"获取排行榜失败: {str(e)}")
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
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/"
                }
                resp = await client.get(api_url, headers=headers)
                data = resp.json()
                if data.get("code") != 0:
                    return None
                video_data = data.get("data")
                if not video_data:
                    return None
                return {
                    "bvid": video_data.get("bvid"),
                    "aid": video_data.get("aid"),
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
        """方式1：纯文本 + 链接"""
        stats = video_info.get("stat", {})
        play_count = stats.get("view", "N/A")
        like_count = stats.get("like", "N/A")
        message_chain = [
            Plain(f"🎬 随机搬运一个B站视频\n"),
            Plain(f"📺 标题：{video_info['title']}\n"),
            Plain(f"👤 UP主：{video_info['owner']}\n"),
            Plain(f"🎉 播放量：{play_count} | 👍 点赞：{like_count}\n"),
            Plain(f"🔗 链接：{video_info['url']}")
        ]
        yield event.chain_result(message_chain)
        logger.info(f"已发送视频(链接形式): {video_info['title']}")

    async def send_video_as_image(self, event: AstrMessageEvent, video_info: Dict[str, Any]):
        """方式2：封面图片 + 文字信息（降级为链接）"""
        stats = video_info.get("stat", {})
        play_count = stats.get("view", "N/A")
        like_count = stats.get("like", "N/A")
        text_info = (
            f"🎬 随机搬运一个B站视频\n"
            f"📺 标题：{video_info['title']}\n"
            f"👤 UP主：{video_info['owner']}\n"
            f"🎉 播放量：{play_count} | 👍 点赞：{like_count}\n"
            f"🔗 链接：{video_info['url']}"
        )

        # 尝试发送图片
        pic_url = video_info.get("pic")
        if pic_url:
            try:
                # 直接使用 Image 组件发送图片 URL
                yield event.chain_result([Image.fromURL(pic_url), Plain(f"\n{text_info}")])
                logger.info(f"已发送视频(图片形式): {video_info['title']}")
                return
            except Exception as e:
                logger.error(f"发送封面图片失败，降级为链接形式: {e}")
        else:
            logger.warning("视频无封面图，降级为链接形式")

        # 降级：发送纯文本链接
        yield event.chain_result([Plain(text_info)])

    async def on_at_message(self, event: AstrMessageEvent):
        """处理@消息"""
        message_chain = event.message_obj.message
        is_at_me = False
        bot_id = self.context.get_bot_id() if hasattr(self.context, 'get_bot_id') else None
        for segment in message_chain:
            if isinstance(segment, At):
                target_id = str(segment.data.get("qq", "")) if hasattr(segment, 'data') else str(segment)
                if bot_id and target_id == bot_id:
                    is_at_me = True
                    break
                elif not bot_id:
                    is_at_me = True
                    break
        if not is_at_me:
            return

        yield event.plain_result("🎥 正在随机搬运B站视频，请稍候...")

        video_info = await self.get_random_video()
        if not video_info:
            yield event.plain_result("❌ 获取视频失败，请稍后再试")
            return

        # 简单去重
        if video_info["bvid"] in self.history_ids:
            logger.info(f"重复视频 {video_info['bvid']}，重新获取")
            video_info = await self.get_random_video()
            if not video_info:
                yield event.plain_result("❌ 获取视频失败，请稍后再试")
                return

        self.history_ids.append(video_info["bvid"])
        if len(self.history_ids) > 50:
            self.history_ids.pop(0)

        # 根据用户配置选择发送形式
        if self.send_type == "image":
            await self.send_video_as_image(event, video_info)
        else:
            await self.send_video_as_link(event, video_info)

    async def on_message(self, event: AstrMessageEvent):
        if any(isinstance(seg, At) for seg in event.message_obj.message):
            async for result in self.on_at_message(event):
                yield result
