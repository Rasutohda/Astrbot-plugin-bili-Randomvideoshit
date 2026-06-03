# bili_login.py
import asyncio
import json
import time
import qrcode
import aiohttp
from pathlib import Path
from typing import Optional, Tuple

class BiliLogin:
    """B站扫码登录器"""
    
    QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.bilibili.com/'
        }
    
    async def generate_qrcode(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """生成二维码，返回 (qrcode_key, qr_url, qr_image_path)"""
        try:
            async with self.session.post(self.QR_GENERATE_URL, headers=self.headers) as resp:
                data = await resp.json()
                if data.get('code') != 0:
                    return None, None, None
                qrcode_key = data['data']['qrcode_key']
                qr_url = data['data']['url']
                
                # 生成二维码图片
                qr = qrcode.make(qr_url)
                qr_path = Path(__file__).parent / "data" / f"qrcode_{int(time.time())}.png"
                qr_path.parent.mkdir(exist_ok=True)
                qr.save(qr_path)
                return qrcode_key, qr_url, str(qr_path)
        except Exception as e:
            print(f"生成二维码失败: {e}")
            return None, None, None
    
    async def poll_login(self, qrcode_key: str, timeout: int = 180) -> Optional[str]:
        """轮询扫码结果，返回Cookie字符串或None"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            await asyncio.sleep(3)
            try:
                async with self.session.get(self.QR_POLL_URL, params={'qrcode_key': qrcode_key}, headers=self.headers) as resp:
                    poll_data = await resp.json()
                    if poll_data.get('code') == 0:
                        # 扫码成功，从响应头提取Cookie
                        cookie_dict = {}
                        set_cookie_headers = resp.headers.getall('Set-Cookie', [])
                        for header in set_cookie_headers:
                            cookie_part = header.split(';')[0].strip()
                            if '=' in cookie_part:
                                k, v = cookie_part.split('=', 1)
                                cookie_dict[k] = v
                        sessdata = cookie_dict.get('SESSDATA', '')
                        bili_jct = cookie_dict.get('bili_jct', '')
                        buvid3 = cookie_dict.get('buvid3', '')
                        if sessdata and bili_jct:
                            cookie_str = f"SESSDATA={sessdata}; bili_jct={bili_jct}; buvid3={buvid3}"
                            return cookie_str
                    elif poll_data.get('code') == 86038:
                        # 二维码过期
                        return None
            except Exception as e:
                print(f"轮询异常: {e}")
                continue
        return None

    async def login(self, event_callback=None) -> Optional[str]:
        """
        完整登录流程
        event_callback: 异步函数，用于发送二维码图片和消息（适配AstrBot的event）
        如果不提供，则直接返回cookie
        """
        qrcode_key, qr_url, qr_path = await self.generate_qrcode()
        if not qrcode_key:
            return None
        
        if event_callback:
            # 发送二维码图片
            await event_callback(qr_path)
            await event_callback("🔗 请使用B站手机App扫码登录，有效期3分钟")
        
        cookie = await self.poll_login(qrcode_key)
        # 清理二维码文件
        try:
            Path(qr_path).unlink()
        except:
            pass
        return cookie
