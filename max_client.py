import asyncio
from datetime import datetime, timedelta, timezone
from enum import IntEnum
import html
import json
import logging
import time
from typing import Callable, Optional
from collections import OrderedDict

import websockets
import xxhash
from curl_cffi import requests

from config import (
    WS_URL,
    HEADERS,
    MAX_DEVICE_AUTH_PAYLOAD,
    MAX_TOKEN_PAYLOAD,
    MAX_TG_PAIRS,
    USER_AGENT,
)
from database import add_message_pair, get_tg_info_by_max_id

logger = logging.getLogger("MaxClient")


class OpCode(IntEnum):
    HEARTBEAT = 1
    DEVICE_AUTH = 6
    TOKEN_AUTH = 19
    CONTACTS_INFO = 32
    SEND_MESSAGE = 64
    DELETE_MESSAGE = 66
    GET_FILE_URL = 88


class MaxClient:
    def __init__(
        self,
        on_message_callback: Callable,
        on_edit_callback: Callable,
        on_self_message_sent: Callable,
    ):
        self.on_message = on_message_callback
        self.on_edit = on_edit_callback
        self.on_self_message_sent = on_self_message_sent

        self.ws: Optional[websockets.ClientConnection] = None
        self._seq = 0
        self.contacts_cache: dict[int, str] = {}
        self.pending_file_requests: dict[int, asyncio.Future] = {}

        # Очередь для дедупликации своих исходящих сообщений (hash -> tg_msg_id)
        self.sent_messages_queue: OrderedDict[int, int] = OrderedDict()

    def _get_seq(self) -> int:
        self._seq += 1
        return self._seq

    @property
    def is_connected(self) -> bool:
        return self.ws is not None and self.ws.state is websockets.State.OPEN

    def make_anti_ban_request(self):
        """Фейковый запрос для прогрева сессии."""
        try:
            requests.get(
                "https://web.max.ru/-72180932047545",
                impersonate="chrome110",
                headers={"User-Agent": USER_AGENT, "Origin": "https://web.max.ru"},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Anti-ban fake request error: {e}")

    async def send_text(
        self,
        chat_id: int,
        text: str,
        display_name: str,
        tg_msg_id: int,
        notify: bool = True,
        reply_msg_id: int | None = None
    ) -> bool:
        if not self.is_connected:
            logger.error("Невозможно отправить сообщение: сокет не подключен.")
            return False

        cid = int(-time.time() * 1000)
        title = f"{display_name} из Telegram\n"
        content = f"{title}\n\n{text}"

        title_utf16_len = len(title.encode("utf-16-le")) // 2

        payload = {
            "ver": 11,
            "cmd": 0,
            "seq": self._get_seq(),
            "opcode": OpCode.SEND_MESSAGE,
            "payload": {
                "chatId": int(chat_id),
                "notify": notify,
                "message": {
                    "text": content,
                    "cid": cid,
                    "type": "User",
                    "elements": [
                        {"type": "STRONG", "from": 0, "length": title_utf16_len}
                    ],
                    "attaches": []
                },
            },
        }

        if reply_msg_id:
            payload["payload"]["message"]["link"] = {
                "type": "REPLY",
                "messageId": reply_msg_id
            }

        # Добавляем в очередь хэш контента
        content_hash = xxhash.xxh32_intdigest(content.encode())
        self.sent_messages_queue[content_hash] = tg_msg_id

        # Ограничиваем размер очереди (FIFO)
        if len(self.sent_messages_queue) > 500:
            self.sent_messages_queue.popitem(last=False)

        try:
            assert self.ws

            await self.ws.send(json.dumps(payload))
            logger.info(f"Сообщение отправлено в Max чат {chat_id}")
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения в Max: {e}")
            return False

    async def delete_message(self, max_msg_id: str | int, max_chat_id: int) -> bool:
        if not self.is_connected:
            return False

        payload = {
            "ver": 11,
            "cmd": 0,
            "seq": self._get_seq(),
            "opcode": OpCode.DELETE_MESSAGE,
            "payload": {
                "chatId": int(max_chat_id),
                "messageIds": [int(max_msg_id)],
            },
        }
        try:
            assert self.ws

            await self.ws.send(json.dumps(payload))
            return True
        except Exception as e:
            logger.error(f"Ошибка удаления сообщения в Max: {e}")
            return False

    def _update_contacts(self, contacts_list: list[dict]):
        for contact in contacts_list:
            user_id = int(contact.get("id")) # type: ignore
            names = contact.get("names", [{}])
            if names:
                name_obj = names[0]
                full_name = f"{name_obj.get('firstName', '')} {name_obj.get('lastName', '')}".strip()
                if not full_name:
                    full_name = name_obj.get("name", f"User_{user_id}")
                self.contacts_cache[user_id] = full_name

    async def _heartbeat_loop(self):
        while self.is_connected:
            try:
                await asyncio.sleep(30)
                assert self.ws
                await self.ws.send(
                    json.dumps(
                        {
                            "ver": 11,
                            "cmd": 0,
                            "seq": self._get_seq(),
                            "opcode": OpCode.HEARTBEAT,
                            "payload": {"interactive": True},
                        }
                    )
                )
            except Exception:
                break

    async def _request_contact_name(self, user_id: int):
        if not self.is_connected or not user_id:
            return

        assert self.ws
        await self.ws.send(
            json.dumps(
                {
                    "ver": 11,
                    "cmd": 0,
                    "seq": self._get_seq(),
                    "opcode": OpCode.CONTACTS_INFO,
                    "payload": {"contactIds": [user_id]},
                }
            )
        )

    async def _parse_linked_messages(self, msg: dict) -> tuple[str, str, list, Optional[int]]:
        """Обрабатывает вложенные ответы (REPLY) и пересылки (FORWARD)."""
        links = []
        link_obj = msg.get("link")
        
        # Ищем ID сообщения, на которое отвечают, чтобы попытаться ответить нативно в ТГ
        reply_to_max_id = None
        reply_to_tg_id = None
        
        if link_obj and link_obj.get("type") == "REPLY":
            reply_to_max_id = link_obj.get("message", {}).get("id")
            
        if reply_to_max_id:
            mapping = await get_tg_info_by_max_id(reply_to_max_id)
            if mapping:
                reply_to_tg_id, _ = mapping

        while link_obj:
            l_msg = link_obj.get("message", {})
            if not l_msg:
                break
            links.append(
                {
                    "type": link_obj.get("type"),
                    "sender": l_msg.get("sender"),
                    "text": l_msg.get("text", ""),
                    "raw_attaches": l_msg.get("attaches", []) or [],
                    "messageId": l_msg.get("id"),
                }
            )
            link_obj = l_msg.get("link")

        reply_blocks = []
        forward_blocks = []
        extra_attaches = []

        for l in links:
            s_id = l["sender"]
            s_name = self.contacts_cache.get(s_id)

            if not s_name:
                s_name = f"ID:{s_id}"
                asyncio.create_task(self._request_contact_name(s_id))

            l_text = html.escape(l["text"]) if l["text"] else ""
            prefix = "Переслано от" if l["type"] == "FORWARD" else "Ответ"
            quote = f"<blockquote><b>{prefix} {html.escape(s_name)}:</b>\n{l_text}"
            if l["raw_attaches"]:
                quote += "\n<i>[Вложение]</i>"
            quote += "</blockquote>"

            if l["type"] == "FORWARD":
                forward_blocks.append(quote)
            else:
                # Если это тот самый ответ, который мы нативно привяжем в ТГ, 
                # скрываем для него блок-цитату, чтобы не дублировать
                if reply_to_tg_id and l["messageId"] == reply_to_max_id:
                    pass
                else:
                    reply_blocks.append(quote)

            extra_attaches.extend(l["raw_attaches"])

        return (
            "\n\n".join(reply_blocks),
            "\n\n".join(forward_blocks),
            extra_attaches,
            reply_to_tg_id
        )
    
    async def _handle_message_payload(self, msg: dict, chat_id: int):
        print("MSG")
        print(msg)
        msg_id = msg.get("id")
        status = msg.get("status")
        sender_id = msg.get("sender")

        assert sender_id
        assert msg_id

        sender_name = self.contacts_cache.get(sender_id)
        if not sender_name:
            sender_name = f"ID:{sender_id}"
            await self._request_contact_name(sender_id)

        raw_text = msg.get("text", "") or ""

        # Проверяем, не является ли это сообщением, отправленным нами из TG
        if (
            sender_name == "Aleksey K"
            and raw_text.split("\n\n")[0].endswith(" из Telegram")
        ):
            content_hash = xxhash.xxh32_intdigest(raw_text.encode())
            tg_id = self.sent_messages_queue.pop(content_hash, None)
            if tg_id:
                await add_message_pair(msg_id, tg_id, is_media=False)
                await self.on_self_message_sent(chat_id, tg_id)
            return

        reply_str, forward_str, extra_attaches, reply_to_tg_id = await self._parse_linked_messages(msg)
        main_text = html.escape(raw_text)

        text_parts = [p for p in [reply_str, main_text, forward_str] if p.strip()]
        full_text = "\n\n".join(text_parts)

        raw_attaches = (msg.get("attaches", []) or []) + extra_attaches
        parsed_attaches = []

        for a in raw_attaches:
            atype = a.get("_type")
            if atype == "PHOTO":
                parsed_attaches.append({"type": "photo", "url": a.get("baseUrl")})
            elif atype == "VIDEO":
                parsed_attaches.append(
                    {"type": "video", "thumbnail": a.get("thumbnail")}
                )
            elif atype == "AUDIO":
                parsed_attaches.append(
                    {
                        "type": "audio",
                        "url": a.get("url"),
                        "duration": int(a.get("duration") or 0),
                    }
                )
            elif atype == "FILE":
                file_id = a.get("fileId")
                filename = a.get("name", f"file_{file_id}")
                seq = self._get_seq()

                loop = asyncio.get_running_loop()
                future = loop.create_future()
                self.pending_file_requests[seq] = future

                assert self.ws
                
                await self.ws.send(
                    json.dumps(
                        {
                            "ver": 11,
                            "cmd": 0,
                            "seq": seq,
                            "opcode": OpCode.GET_FILE_URL,
                            "payload": {
                                "fileId": file_id,
                                "chatId": chat_id,
                                "messageId": msg_id,
                            },
                        }
                    )
                )

                try:
                    url = await asyncio.wait_for(future, timeout=15.0)
                    if url:
                        parsed_attaches.append(
                            {"type": "file", "url": url, "name": filename}
                        )
                except asyncio.TimeoutError:
                    logger.error(f"Таймаут получения URL файла {filename}")
                finally:
                    self.pending_file_requests.pop(seq, None)

        now_str = datetime.now(timezone(offset=timedelta(hours=3))).strftime(
            "%d.%m.%Y в %H:%M"
        )

        if status == "REMOVED":
            mapping = await get_tg_info_by_max_id(msg_id)
            if mapping:
                tg_id, is_media = mapping
                del_text = f"{full_text}\n\n<i>[Сообщение удалено в Max: {now_str}]</i>"
                await self.on_edit(chat_id, tg_id, sender_name, del_text, is_media)

        elif status == "EDITED":
            mapping = await get_tg_info_by_max_id(msg_id)
            if mapping:
                tg_id, is_media = mapping
                edit_text = f"{full_text}\n\n<i>[Изменено в Max: {now_str}]</i>"
                await self.on_edit(chat_id, tg_id, sender_name, edit_text, is_media)

        else:
            tg_msg_id = await self.on_message(
                chat_id=chat_id,
                name=sender_name,
                text=full_text,
                attaches=parsed_attaches,
                max_msg_id=msg_id,
                reply_to_tg_id=reply_to_tg_id,
            )
            if tg_msg_id:
                await add_message_pair(
                    msg_id, tg_msg_id, is_media=bool(parsed_attaches)
                )

    async def run(self):
        while True:
            try:
                logger.info("Подключение к WebSocket Max...")
                async with websockets.connect(
                    WS_URL, additional_headers=HEADERS
                ) as ws:
                    self.ws = ws

                    # 1. Device Auth
                    await ws.send(
                        json.dumps(
                            {
                                "ver": 11,
                                "cmd": 0,
                                "seq": self._get_seq(),
                                "opcode": OpCode.DEVICE_AUTH,
                                "payload": MAX_DEVICE_AUTH_PAYLOAD,
                            }
                        )
                    )

                    # 2. Token Auth
                    await ws.send(
                        json.dumps(
                            {
                                "ver": 11,
                                "cmd": 0,
                                "seq": self._get_seq(),
                                "opcode": OpCode.TOKEN_AUTH,
                                "payload": MAX_TOKEN_PAYLOAD,
                            }
                        )
                    )

                    asyncio.create_task(self._heartbeat_loop())
                    logger.info("Успешно подключено к Max WS.")

                    while True:
                        raw = await ws.recv()
                        data = json.loads(raw)
                        opcode = data.get("opcode")
                        cmd = data.get("cmd")

                        # Ответ на запрос URL файла
                        if opcode == OpCode.GET_FILE_URL and cmd == 1:
                            seq = data.get("seq")
                            if seq in self.pending_file_requests:
                                url = data.get("payload", {}).get("url")
                                if not self.pending_file_requests[seq].done():
                                    self.pending_file_requests[seq].set_result(url)
                            continue

                        payload = data.get("payload")
                        if not payload:
                            continue

                        if "contacts" in payload:
                            self._update_contacts(payload["contacts"])

                        if "chats" in payload:
                            for c in payload["chats"]:
                                if c.get("id") in MAX_TG_PAIRS:
                                    p_ids = [
                                        int(uid)
                                        for uid in c.get("participants", {}).keys()
                                    ]
                                    if p_ids:
                                        await ws.send(
                                            json.dumps(
                                                {
                                                    "ver": 11,
                                                    "cmd": 0,
                                                    "seq": self._get_seq(),
                                                    "opcode": OpCode.CONTACTS_INFO,
                                                    "payload": {"contactIds": p_ids},
                                                }
                                            )
                                        )

                        if "messages" in payload or "message" in payload:
                            cid = payload.get("chatId") or payload.get("cid")
                            msgs = payload.get("messages", [])
                            if isinstance(msgs, dict):
                                msgs = list(msgs.values())
                            if not msgs and "message" in payload:
                                msgs = [payload["message"]]

                            for m in msgs:
                                if m and (cid in MAX_TG_PAIRS or not cid):
                                    asyncio.create_task(
                                        self._handle_message_payload(m, cid)
                                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"Ошибка сокета Max: {e}. Переподключение через 5 секунд..."
                )
                self.ws = None
                await asyncio.sleep(5)