⚙️ 配置
插件配置文件位于 data/plugins/Astrbot_plugin_bili_Randomvideoshit/_conf_schema.json，可通过 AstrBot WebUI 的“插件配置”界面修改，或直接编辑 JSON 文件。

配置项	类型	默认值	说明
auto_start	bool	true	插件启动时自动开启定时推送
scan_interval	int	3600	定时推送间隔（秒），最小 60 秒
keyword_cooldown_seconds	int	600	关键词触发冷却时间（秒），0 表示无冷却
use_whitelist_mode	bool	false	true：白名单模式；false：黑名单模式
whitelist_groups	array	[]	白名单群组 ID 列表
blacklist_groups	array	[]	黑名单群组 ID 列表
keywords	array	["随机视频", "来点视频", "B站视频"]	触发关键词列表
注意：群组 ID 可通过机器人收到的消息日志获取，格式通常为纯数字字符串。

📝 命令
所有命令均以 /bili 或 bili 开头，在私聊或群聊中均可使用。

命令	说明
/bili now	立即推送一个随机视频（手动调用有 60 秒冷却）
/bili login	扫码登录 B 站账号（发送二维码图片）
/bili on	开启定时推送
/bili off	关闭定时推送
/bili status	查看插件当前状态（定时任务、Cookie、群组等）
/bili mode whitelist	切换为白名单模式（仅白名单群组接收推送）
/bili mode blacklist	切换为黑名单模式（黑名单群组不接收推送）
/bili interval <秒>	设置定时推送间隔（最小 60 秒）
/bili clear	清除当前群的关键词冷却记录（仅群聊）
/bili help	显示帮助信息
关键词触发
在群聊中发送包含配置中 keywords 关键词的消息（例如“随机视频”），机器人会自动推送一个随机视频，并进入冷却时间（默认 10 分钟）。

🔐 首次使用
登录 B 站账号
在私聊或群聊中发送 /bili login，机器人会生成一个二维码。使用 B 站手机 App 扫码并确认登录，Cookie 会自动保存，之后即可正常使用。

测试推送
发送 /bili now 立即获取一个视频，检查是否正常。

开启定时推送（可选）
默认插件启动时自动开启定时推送，如需关闭可发送 /bili off，重新开启用 /bili on。

🛠️ 常见问题
1. 扫码登录后提示“未获取到 Cookie”？
网络波动可能导致 Cookie 提取失败，请重新执行 /bili login 再试一次。

2. 定时推送没有反应？

检查是否已登录（发送 /bili status 查看 Cookie 状态）

确认定时推送已开启（/bili status 中显示“运行中”）

检查群组是否在白名单/黑名单中

3. 如何让机器人只推送到特定群组？
设置 use_whitelist_mode: true，然后将允许的群组 ID 填入 whitelist_groups 数组。

4. 视频总是重复推送？
插件会自动记录已推送过的视频（保留 7 天），7 天后会重新推送。

5. 提示“异步生成器错误”？
请确保您使用的 AstrBot 版本为 v4.25.1 或更高，并已正确安装所有依赖。

📄 依赖
aiohttp - 异步 HTTP 请求

qrcode - 生成登录二维码

cryptography - Cookie 加密存储

安装命令：

bash
pip install aiohttp qrcode cryptography
🤝 贡献
欢迎提交 Issue 和 Pull Request！

📜 许可证
本项目采用 MIT 许可证。

🔗 相关链接
AstrBot 项目主页

插件仓库

作者: Rasutohda
版本: 2.0.0
最后更新: 2026-06-03
