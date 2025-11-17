import json
import os
from functools import lru_cache
from typing import List, Optional

from pydantic import BaseModel
from pydantic_settings import BaseSettings


class TelegramBotConfig(BaseModel):
    """
    Telegram 机器人配置项，用于描述单个可用 bot。
    """
    name: str
    token: str
    channel_name: str


class Settings(BaseSettings):
    """
    应用程序设置入口，兼容历史单 bot 配置并支持扩展。
    """
    BOT_TOKEN: str
    CHANNEL_NAME: str
    EXTRA_BOTS: Optional[str] = None  # JSON 字符串，描述额外 bot 列表
    PASS_WORD: Optional[str] = None
    PICGO_API_KEY: Optional[str] = None  # [可选] PicGo 上传接口 API
    BASE_URL: str = "http://127.0.0.1:8000"
    MODE: str = "p"  # p 代表公开模式, m 代表私有模式
    FILE_ROUTE: str = "/d/"
    MULTIBOT_THRESHOLD_MB: int = 10  # 超过该阈值后启用多 bot 分片


@lru_cache()
def get_settings() -> Settings:
    """
    获取应用程序设置。

    此函数会被缓存，以避免在每个请求中都从环境中重新读取设置。
    """
    return Settings()


def get_active_password() -> Optional[str]:
    """
    获取当前有效的密码。
    优先从 .password 文件读取，如果文件不存在，则回退到环境变量。
    """
    password_file = ".password"
    if os.path.exists(password_file):
        with open(password_file, "r", encoding="utf-8") as f:
            password = f.read().strip()
            if password:
                return password

    # 如果文件不存在或为空，则回退到环境变量
    return get_settings().PASS_WORD


def _parse_extra_bots(raw_extra: Optional[str]) -> List[TelegramBotConfig]:
    """
    将 EXTRA_BOTS 字符串解析为 TelegramBotConfig 列表。

    字符串期望为 JSON 数组，例如:
    [
        {"name": "bot_b", "token": "123:ABC", "channel_name": "@my_channel_b"},
        {"name": "bot_c", "token": "456:DEF", "channel_name": "-100123456"}
    ]
    """
    parsed: List[TelegramBotConfig] = []
    if not raw_extra:
        return parsed

    try:
        entries = json.loads(raw_extra)
    except json.JSONDecodeError:
        return parsed

    if not isinstance(entries, list):
        return parsed

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            parsed.append(TelegramBotConfig(**entry))
        except Exception:
            continue
    return parsed


def get_telegram_bots() -> List[TelegramBotConfig]:
    """
    返回所有可用的 Telegram bot 配置，列表至少包含主 bot。
    """
    settings = get_settings()
    bots: List[TelegramBotConfig] = [
        TelegramBotConfig(
            name="default",
            token=settings.BOT_TOKEN,
            channel_name=settings.CHANNEL_NAME,
        )
    ]
    bots.extend(_parse_extra_bots(settings.EXTRA_BOTS))
    return bots
