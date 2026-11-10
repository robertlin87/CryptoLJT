"""开发者锁定的固定配置。

信号群由开发者锁定为「加密林教頭 VIP群」：软件只监听该群，
不允许用户在配置里自定义（防止用本软件监听其他群）。所有入口
（run / listen / GUI 向导）都从这里取群信息，忽略配置文件里的
chat_title/chat_id。
"""

GROUP_CHAT_ID = -1003875376369
GROUP_CHAT_TITLE = "加密林教頭 VIP群"

# 未加入信號群（私密群無權訪問）時顯示的提示；同時作為使用授權閘門。
GROUP_NOT_JOINED_MSG = (
    "你現在還沒有加入 加密林教頭 VIP 社群，無法收到報單訊息！"
    "請加入免費的 Telegram 頻道了解如何成為 VIP："
    "https://t.me/+aC9GoBeIdvU5YzFl"
)

VERSION = "1.0"
