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
          "有人@机器人时随机从B站搬运一个视频（支持自定义分区）", "1.1.0",
          "https://github.com/Rasutohda/Astrbot_plugin_bili_Randomvideoshit")
class BiliRandomVideo(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 从配置中读取用户设置的分区 rid（默认 None 表示全站排行榜）
        self.region_id = None
        self.use_single_random = False   # False = 使用排行榜/分区模式；True = 随机BV号（不推荐）
        self.history_ids: List[str] = []
        self.load_config()

    def load_config(self):
        """加载用户配置（分区）"""
        config = self.context.get_config()  # 获取插件配置
        if config:
            # 配置项: region 可以是 rid 数字或分区名称字符串
            region_conf = config.get("region", None)
            if region_conf:
                # 尝试转换为整数 rid，否则通过映射表转换
                rid = self.parse_region(region_conf)
                if rid is not None:
                    self.region_id = rid
                    logger.info(f"已启用自定义分区: {region_conf} (rid={rid})")
                else:
                    logger.warning(f"无效的分区配置: {region_conf}，将使用全站排行榜")
            else:
                logger.info("未配置分区，使用全站排行榜")

    def parse_region(self, region_input) -> Optional[int]:
        """将分区名称或 rid 转换为整数的 rid"""
        # 常见分区映射
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
        # 如果输入是数字字符串或整数
        try:
            rid = int(region_input)
            return rid
        except (ValueError, TypeError):
            pass
        # 如果是字符串名称
        if isinstance(region_input, str):
            key = region_input.strip()
            if key in region_map:
                return region_map[key]
            # 尝试直接转换拼音或英文等（可扩展）
        return None

    async def get_random_video_from_region(self) -> Optional[Dict[str, Any]]:
        """从指定分区获取随机视频（使用分区视频列表接口）"""
        if self.region_id is None:
            # 没有分区 → 使用全站排行榜
            return await self.get_random_video_from_rank()

        api_url = "https://api.bilibili.com/x/web-interface/dynamic/region"
        params = {
            "rid": self.region_id,
            "pn": 1,
            "ps": 50   # 每页50个，足够随机
        }
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
                # 随机选一个
                video = random.choice(archives)
                return await self.get_video_details(video.get("bvid"))
        except Exception as e:
            logger.error(f"获取分区视频失败: {str(e)}")
            return None

    async def get_random_video_from_rank(self) -> Optional[Dict[str, Any]]:
        """从全站排行榜随机抽一个（原方式一）"""
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
        """方式二：随机BV号（备胎，默认关闭）"""
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
        """获取视频详细信息"""
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
        """统一入口：优先使用配置的分区，否则全站排行榜"""
        if self.use_single_random:
            return await self.get_random_video_by_bvid()
        else:
            # 如果有自定义分区，则从分区获取；否则从全站排行榜获取
            if self.region_id is not None:
                return await self.get_random_video_from_region()
            else:
                return await self.get_random_video_from_rank()

    async def send_video_as_link(self, event: AstrMessageEvent, video_info: Dict[str, Any]):
        """以文字+链接形式发送（兼容所有平台）"""
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
        logger.info(f"已发送视频: {video_info['title']}")

    async def on_at_message(self, event: AstrMessageEvent):
        """处理@消息"""
        # 检查是否被@（兼容多平台）
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

        await self.send_video_as_link(event, video_info)

    async def on_message(self, event: AstrMessageEvent):
        """消息入口"""
        if any(isinstance(seg, At) for seg in event.message_obj.message):
            async for result in self.on_at_message(event):
                yield result
