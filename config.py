import json
import os
import sys
from dotenv import load_dotenv

load_dotenv()


def get_env_or_exit(key: str) -> str:
    val = os.getenv(key)
    if not val:
        print(f"[ERROR] Переменная окружения '{key}' не задана!")
        sys.exit(1)
    return val


WS_URL: str = "wss://ws-api.oneme.ru/websocket"

USER_AGENT: str = get_env_or_exit("USER_AGENT")

HEADERS: dict = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Origin": "https://web.max.ru",
}

TG_BOT_TOKEN: str = get_env_or_exit("TG_BOT_TOKEN")
OWNER_TG_ID: int = int(get_env_or_exit("OWNER_TG_ID"))
DB_PATH: str = os.getenv("DB_PATH", "database.db")

MAX_TOKEN_PAYLOAD: dict = json.loads(get_env_or_exit("MAX_TOKEN_PAYLOAD"))
MAX_DEVICE_AUTH_PAYLOAD: dict = json.loads(get_env_or_exit("MAX_DEVICE_AUTH_PAYLOAD"))

# Словарь {int(max_chat_id): int(tg_chat_id)}
MAX_TG_PAIRS: dict[int, int] = {
    int(k): int(v) for k, v in json.loads(get_env_or_exit("MAX_TG_PAIRS")).items()
}

# Обратный словарь {int(tg_chat_id): int(max_chat_id)}
TG_MAX_PAIRS: dict[int, int] = {v: k for k, v in MAX_TG_PAIRS.items()}