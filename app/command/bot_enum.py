from enum import StrEnum

class TGBotCommand(StrEnum):
    """Telegram Bot 指令名稱列舉。

    用集中式 enum 管理指令字串，避免在多處硬編碼導致不一致。
    """
    COMMAND   = "command"
    SEARCH    = "s"
    STRATEGY  = "strategy"
    CONFIG    = "config"
    SETUP     = "setup"
    MY_CONFIG = "myconfig"