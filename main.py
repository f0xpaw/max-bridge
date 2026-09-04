import asyncio
import html
import logging
import sys
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, InputMediaPhoto
from curl_cffi.requests import AsyncSession

from config import TG_BOT_TOKEN, OWNER_TG_ID, MAX_TG_PAIRS, TG_MAX_PAIRS
from database import (
    init_db,
    periodic_db_cleanup,
    set_user_custom_name,
    get_user_custom_name,
    delete_user_custom_name,
    get_max_id_by_tg_id,
)
from max_client import MaxClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("BridgeBot")

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher()

max_client: Optional[MaxClient] = None


# ================== Telegram Handlers (Команды и Имена) ==================


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Бот-мост между Telegram и Max запущен.\n\n"
        "Управление отображаемым именем:\n"
        "• <code>/setname Ваше Имя</code> — установить имя для чата Max\n"
        "• <code>/myname</code> — узнать текущее имя\n"
        "• <code>/delname</code> — сбросить имя к стандартному\n"
        "• <code>/delete</code> (в ответ) — удалить свое сообщение в Max",
        parse_mode="HTML",
    )


@dp.message(Command("setname"))
async def cmd_set_name(message: types.Message, command: CommandObject):
    assert message.from_user

    if not command.args or not command.args.strip():
        return await message.reply(
            "Укажите имя после команды. Пример:\n<code>/setname Иван Иванов</code>",
            parse_mode="HTML",
            disable_notification=True
        )

    custom_name = command.args.strip()
    await set_user_custom_name(message.from_user.id, custom_name)
    return await bot.set_message_reaction(
        chat_id=message.chat.id,
        message_id=message.message_id,
        reaction=[types.ReactionTypeEmoji(emoji="🔥")],
    )


@dp.message(Command("myname"))
async def cmd_my_name(message: types.Message):
    assert message.from_user

    custom_name = await get_user_custom_name(message.from_user.id)
    if custom_name:
        await message.reply(
            f"Текущее имя для Max: <b>{html.escape(custom_name)}</b>",
            parse_mode="HTML",
            disable_notification=True
        )
    else:
        tg_default = (
            message.from_user.full_name
            + (f" (@{message.from_user.username})" if message.from_user.username else "")
        )
        await message.reply(
            f"Кастомное имя не задано. Используется стандартное: <b>{html.escape(tg_default)}</b>\n"
            f"Чтобы задать, напишите: <code>/name Ваше Имя</code>",
            parse_mode="HTML", disable_notification=True
        )


@dp.message(Command("delname"))
async def cmd_del_name(message: types.Message):
    assert message.from_user

    await delete_user_custom_name(message.from_user.id)
    await message.reply("🗑 Кастомное имя сброшено. Будет использоваться имя из Telegram.", disable_notification=True)


@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    assert message.from_user

    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[types.ReactionTypeEmoji(emoji="🗿")],
        )

    if message.reply_to_message.from_user.id != message.from_user.id:
        return await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[types.ReactionTypeEmoji(emoji="🗿")],
        )

    max_msg_id = await get_max_id_by_tg_id(message.reply_to_message.message_id)
    max_chat_id = TG_MAX_PAIRS.get(message.chat.id)

    if not max_msg_id or not max_chat_id:
        return await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[types.ReactionTypeEmoji(emoji="👎")],
        )

    assert max_client

    if await max_client.delete_message(max_msg_id, max_chat_id):
        try:
            await message.reply_to_message.delete()
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass


# ================== Отправка из Telegram в Max ==================


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_tg_to_max(message: types.Message):
    assert message.from_user

    if message.from_user.is_bot or not message.text:
        return

    max_chat_id = TG_MAX_PAIRS.get(message.chat.id)
    if not max_chat_id:
        return

    # Получаем кастомное имя либо генерируем дефолтное
    custom_name = await get_user_custom_name(message.from_user.id)
    if not custom_name:
        custom_name = message.from_user.full_name
        if message.from_user.username:
            custom_name += f" @{message.from_user.username}"

    
    assert max_client

    reply_max_msg_id = None

    if message.reply_to_message is not None:
        tg_msg_id = message.reply_to_message.message_id

        reply_max_msg_id = await get_max_id_by_tg_id(tg_msg_id)

        if reply_max_msg_id is not None:
            reply_max_msg_id = int(reply_max_msg_id)


    success = await max_client.send_text(
        chat_id=max_chat_id,
        text=message.text,
        display_name=custom_name,
        tg_msg_id=message.message_id,
        reply_msg_id=reply_max_msg_id
    )

    if not success:
        await message.reply("⚠️ Нет подключения к Max. Сообщение не отправлено.", 
            disable_notification=True)


# ================== Callbacks от Max к Telegram ==================


async def on_self_message_sent(max_chat_id: int, tg_msg_id: int):
    """Ставим реакцию на наше сообщение в TG при успешной доставке в Max."""
    tg_chat_id = MAX_TG_PAIRS.get(max_chat_id)
    if tg_chat_id:
        try:
            await bot.set_message_reaction(
                chat_id=tg_chat_id,
                message_id=tg_msg_id,
                reaction=[types.ReactionTypeEmoji(emoji="👍")],
            )
        except Exception:
            pass


async def send_to_recipients(chat_id: int, send_coroutine_gen, reply_to_tg_id: Optional[int] = None):
    """Универсальная отправка и в связанный чат, и Owner'у."""
    target_chats = set()
    paired_tg = MAX_TG_PAIRS.get(chat_id)
    if paired_tg:
        target_chats.add(paired_tg)
    if OWNER_TG_ID:
        target_chats.add(OWNER_TG_ID)

    last_msg_id = None
    for tg_id in target_chats:
        # Реплай имеет смысл отправлять только в связанную группу (там где сообщение реально существует)
        current_reply_id = reply_to_tg_id if tg_id == paired_tg else None
        
        try:
            res = await send_coroutine_gen(tg_id, current_reply_id)
            if tg_id == paired_tg:
                last_msg_id = res.message_id if hasattr(res, "message_id") else res[0].message_id
        except Exception as e:
            # Если целевое сообщение в ТГ было удалено, отправляем без привязки ответа
            if current_reply_id and "reply message not found" in str(e).lower():
                try:
                    res = await send_coroutine_gen(tg_id, None)
                    if tg_id == paired_tg:
                        last_msg_id = res.message_id if hasattr(res, "message_id") else res[0].message_id
                except Exception as ex:
                    logger.error(f"Ошибка фоллбека отправки в TG чат {tg_id}: {ex}")
            else:
                logger.error(f"Ошибка отправки в TG чат {tg_id}: {e}")

    return last_msg_id


async def on_message_from_max(
    chat_id: int, 
    name: str, 
    text: str, 
    attaches: list, 
    max_msg_id: str,
    reply_to_tg_id: Optional[int] = None
):
    safe_name = html.escape(name)
    caption_text = f"<b>{safe_name}</b>"
    if text:
        caption_text += f"\n\n{text}"

    medias = [a for a in attaches if a.get("type") in ["photo", "video"]]

    # 1. Фото / Видео
    if medias:
        media_group = []
        for i, m in enumerate(medias):
            url = m.get("url") if m.get("type") == "photo" else m.get("thumbnail")
            MAX_CAPTION = 1024
            TRUNC_NOTICE = "\n... [Обрезано]"

            if i == 0 and caption_text:
                if len(caption_text) > MAX_CAPTION:
                    caption = caption_text[: MAX_CAPTION - len(TRUNC_NOTICE)] + TRUNC_NOTICE
                else:
                    caption = caption_text
            else:
                caption = None

            media_group.append(
                InputMediaPhoto(
                    media=url,
                    caption=caption,
                    parse_mode="HTML",
                )
            )
        return await send_to_recipients(
            chat_id, 
            lambda cid, rep_id: bot.send_media_group(chat_id=cid, media=media_group, reply_to_message_id=rep_id),
            reply_to_tg_id
        )

    # 2. Аудио (Voice)
    elif any(a.get("type") == "audio" for a in attaches):
        audio_item = attaches[0]
        async with AsyncSession() as session:
            resp = await session.get(audio_item.get("url"))
            if resp.status_code == 200:
                voice_file = BufferedInputFile(resp.content, filename="voice.ogg")
                duration = audio_item.get("duration", 0) // 1000
                return await send_to_recipients(
                    chat_id,
                    lambda cid, rep_id: bot.send_voice(
                        chat_id=cid,
                        voice=voice_file,
                        duration=duration,
                        caption=f"<b>{safe_name}</b>",
                        parse_mode="HTML",
                        reply_to_message_id=rep_id
                    ),
                    reply_to_tg_id
                )

    # 3. Файлы
    elif any(a.get("type") == "file" for a in attaches):
        file_item = attaches[0]
        async with AsyncSession() as session:
            resp = await session.get(file_item.get("url"))
            if resp.status_code == 200:
                doc_file = BufferedInputFile(
                    resp.content, filename=file_item.get("name", "file")
                )
                return await send_to_recipients(
                    chat_id,
                    lambda cid, rep_id: bot.send_document(
                        chat_id=cid,
                        document=doc_file,
                        caption=caption_text,
                        parse_mode="HTML",
                        reply_to_message_id=rep_id
                    ),
                    reply_to_tg_id
                )

    # 4. Простой текст
    else:
        return await send_to_recipients(
            chat_id,
            lambda cid, rep_id: bot.send_message(
                chat_id=cid, text=caption_text, parse_mode="HTML", reply_to_message_id=rep_id
            ),
            reply_to_tg_id
        )
    

async def on_edit_from_max(chat_id: int, tg_msg_id: int, name: str, text: str, is_media: bool):
    tg_chat_id = MAX_TG_PAIRS.get(chat_id)
    if not tg_chat_id:
        return

    content = f"<b>{html.escape(name)}</b>\n\n{text}"
    try:
        if is_media:
            await bot.edit_message_caption(
                chat_id=tg_chat_id,
                message_id=tg_msg_id,
                caption=content,
                parse_mode="HTML",
            )
        else:
            await bot.edit_message_text(
                chat_id=tg_chat_id,
                message_id=tg_msg_id,
                text=content,
                parse_mode="HTML",
            )
    except Exception as e:
        logger.warning(f"Не удалось отредактировать сообщение в TG: {e}")


# ================== Запуск приложения ==================


async def main():
    global max_client

    await init_db()

    max_client = MaxClient(
        on_message_callback=on_message_from_max,
        on_edit_callback=on_edit_from_max,
        on_self_message_sent=on_self_message_sent,
    )
    max_client.make_anti_ban_request()

    # Запускаем фоновые задачи
    tasks = [
        asyncio.create_task(dp.start_polling(bot)),
        asyncio.create_task(max_client.run()),
        asyncio.create_task(periodic_db_cleanup()),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Программа остановлена.")
        sys.exit(0)