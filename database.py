import asyncio
import logging
from typing import Optional
import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")

        # Таблица соответствия ID сообщений
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS message_map (
                max_id TEXT PRIMARY KEY,
                tg_id INTEGER NOT NULL,
                is_media BOOLEAN NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tg_id ON message_map(tg_id);"
        )

        # Таблица кастомных имен пользователей
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_names (
                tg_user_id INTEGER PRIMARY KEY,
                custom_name TEXT NOT NULL
            )
        """
        )
        await db.commit()
    logger.info("База данных инициализирована.")


# ------------------ Работа с кастомными именами ------------------


async def set_user_custom_name(tg_user_id: int, custom_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_names (tg_user_id, custom_name) VALUES (?, ?) "
            "ON CONFLICT(tg_user_id) DO UPDATE SET custom_name = excluded.custom_name",
            (tg_user_id, custom_name.strip()),
        )
        await db.commit()


async def get_user_custom_name(tg_user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT custom_name FROM user_names WHERE tg_user_id = ?",
            (tg_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def delete_user_custom_name(tg_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM user_names WHERE tg_user_id = ?", (tg_user_id,)
        )
        await db.commit()


# ------------------ Работа с сообщениями ------------------


async def add_message_pair(max_id: str | int, tg_id: int, is_media: bool):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO message_map (max_id, tg_id, is_media) VALUES (?, ?, ?)",
                (str(max_id), tg_id, is_media),
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка при сохранении ID сообщений: {e}")


async def get_max_id_by_tg_id(tg_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT max_id FROM message_map WHERE tg_id = ?", (tg_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_tg_info_by_max_id(max_id: str | int) -> Optional[tuple[int, bool]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT tg_id, is_media FROM message_map WHERE max_id = ?",
            (str(max_id),),
        ) as cursor:
            row = await cursor.fetchone()
            return (int(row[0]), bool(row[1])) if row else None


# ------------------ Фоновая очистка ------------------


async def periodic_db_cleanup(interval_seconds: int = 86400, days_keep: int = 30):
    """Фоновая периодическая очистка старых сообщений."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    f"DELETE FROM message_map WHERE created_at < datetime('now', '-{days_keep} days')"
                )
                await db.commit()
            logger.info("Старые сообщения очищены из БД.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка при периодической очистке БД: {e}")
            