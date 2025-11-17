import asyncio
import io
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import telegram
from telegram.request import HTTPXRequest

from .. import database
from ..core.config import Settings, TelegramBotConfig, get_settings, get_telegram_bots

# Telegram Bot API 对通过 getFile 方法下载的文件有 20MB 的限制
# 我们将分块大小设置为 19.5MB 以确保上传和下载都能成功
CHUNK_SIZE_BYTES = int(19.5 * 1024 * 1024)
# 多 bot 并行分片时默认的单分片大小（更小以便并行传输）
MULTI_BOT_CHUNK_SIZE_BYTES = int(8 * 1024 * 1024)
MAX_PARALLEL_CHUNKS = 6


@dataclass
class BotClient:
    name: str
    bot: telegram.Bot
    channel_name: str


class TelegramService:
    """
    用于与 Telegram Bot API 交互的服务，支持多 bot 并行上传。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot_configs: List[TelegramBotConfig] = get_telegram_bots()
        if not self.bot_configs:
            raise ValueError("至少需要配置一个 Telegram bot。")
        self.bot_clients: List[BotClient] = [self._build_bot_client(cfg) for cfg in self.bot_configs]
        self.bot_map = {client.name: client for client in self.bot_clients}
        self.bot = self.bot_clients[0].bot  # 保持向后兼容
        self.channel_name = self.bot_clients[0].channel_name
        self.multibot_threshold_bytes = max(self.settings.MULTIBOT_THRESHOLD_MB, 1) * 1024 * 1024
        self.multi_bot_chunk_size = max(MULTI_BOT_CHUNK_SIZE_BYTES, 2 * 1024 * 1024)
        self.max_parallel_chunks = MAX_PARALLEL_CHUNKS

    @staticmethod
    def _build_bot_client(config: TelegramBotConfig) -> BotClient:
        request = HTTPXRequest(
            connect_timeout=300.0,
            read_timeout=300.0,
            write_timeout=300.0,
        )
        bot = telegram.Bot(token=config.token, request=request)
        return BotClient(name=config.name, bot=bot, channel_name=config.channel_name)

    def _should_use_multibot(self, file_size: int) -> bool:
        return len(self.bot_clients) > 1 and file_size > self.multibot_threshold_bytes

    async def _upload_small_file(self, file_path: str, file_name: str) -> Optional[str]:
        try:
            with open(file_path, "rb") as document_file:
                message = await self.bot.send_document(
                    chat_id=self.channel_name,
                    document=document_file,
                    filename=file_name,
                )
            if message.document:
                composite_id = f"{message.message_id}:{message.document.file_id}"
                file_size = os.path.getsize(file_path)
                database.add_file_metadata(
                    filename=file_name,
                    file_id=composite_id,
                    filesize=file_size,
                )
                return composite_id
        except Exception as e:
            print(f"上传文件到 Telegram 时出错 {e}")
        return None

    async def _upload_as_chunks_single_bot(self, file_path: str, original_filename: str) -> Optional[str]:
        chunk_file_ids = []
        first_message_id = None

        try:
            with open(file_path, "rb") as f:
                chunk_number = 1
                while True:
                    chunk = f.read(CHUNK_SIZE_BYTES)
                    if not chunk:
                        break

                    chunk_name = f"{original_filename}.part{chunk_number}"
                    print(f"正在上传分块: {chunk_name}")

                    with io.BytesIO(chunk) as chunk_io:
                        reply_to_id = first_message_id if first_message_id else None
                        message = await self.bot.send_document(
                            chat_id=self.channel_name,
                            document=chunk_io,
                            filename=chunk_name,
                            reply_to_message_id=reply_to_id,
                        )

                    if not first_message_id:
                        first_message_id = message.message_id

                    chunk_file_ids.append(f"{message.message_id}:{message.document.file_id}")
                    chunk_number += 1
        except IOError as e:
            print(f"读取或上传文件块时出错 {e}")
            return None
        except Exception as e:
            print(f"发送文件块时出错 {e}")
            return None

        manifest_content = f"tgstate-blob\n{original_filename}\n" + "\n".join(chunk_file_ids)
        manifest_name = f"{original_filename}.manifest"

        print("所有分块上传完毕。正在上传清单文件...")
        try:
            with io.BytesIO(manifest_content.encode("utf-8")) as manifest_file:
                message = await self.bot.send_document(
                    chat_id=self.channel_name,
                    document=manifest_file,
                    filename=manifest_name,
                    reply_to_message_id=first_message_id,
                )
            if message.document:
                print("清单文件上传成功。")
                total_size = os.path.getsize(file_path)
                composite_id = f"{message.message_id}:{message.document.file_id}"
                database.add_file_metadata(
                    filename=original_filename,
                    file_id=composite_id,
                    filesize=total_size,
                    is_multipart=True,
                )
                return composite_id
        except Exception as e:
            print(f"上传清单文件时出错 {e}")

        return None

    async def _upload_file_multibot(self, file_path: str, original_filename: str) -> Optional[str]:
        """
        多 bot 并行分片上传，将文件切片后按 bot 轮询分发并上传。
        """
        file_size = os.path.getsize(file_path)
        chunk_size = self.multi_bot_chunk_size
        total_parts = max(1, math.ceil(file_size / chunk_size))

        # 为了限制整体并发量，我们使用单独的信号量，而不是在内部声明
        semaphore = asyncio.Semaphore(self.max_parallel_chunks)

        async def dispatch_chunk(part_index: int, chunk_bytes: bytes) -> Dict[str, Any]:
            bot_client = self.bot_clients[(part_index - 1) % len(self.bot_clients)]
            caption = f"{original_filename} (part {part_index}/{total_parts})"
            async with semaphore:
                with io.BytesIO(chunk_bytes) as chunk_io:
                    message = await bot_client.bot.send_document(
                        chat_id=bot_client.channel_name,
                        document=chunk_io,
                        filename=f"{original_filename}.part{part_index}",
                        caption=caption,
                    )
            if not message.document:
                raise RuntimeError("分片上传失败，未返回 document 信息。")
            return {
                "part_index": part_index,
                "message_id": message.message_id,
                "file_id": message.document.file_id,
                "bot_name": bot_client.name,
                "channel_name": bot_client.channel_name,
                "size": len(chunk_bytes),
            }

        pending: List[asyncio.Task] = []
        results: List[Dict[str, Any]] = []

        try:
            with open(file_path, "rb") as f:
                part_index = 1
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    task = asyncio.create_task(dispatch_chunk(part_index, chunk))
                    pending.append(task)
                    if len(pending) >= self.max_parallel_chunks:
                        done, still_pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        for finished in done:
                            results.append(finished.result())
                        pending = list(still_pending)
                    part_index += 1
        except Exception as e:
            for task in pending:
                task.cancel()
            print(f"并行分片上传时出错: {e}")
            return None

        if pending:
            done, _ = await asyncio.wait(pending)
            for finished in done:
                results.append(finished.result())

        if len(results) != total_parts:
            print("警告：分片上传结果数量与分片总数不符。")

        results.sort(key=lambda item: item["part_index"])
        chunk_file_ids = [f"{item['message_id']}:{item['file_id']}" for item in results]

        manifest_content = f"tgstate-blob\n{original_filename}\n" + "\n".join(chunk_file_ids)
        manifest_name = f"{original_filename}.manifest"

        manifest_metadata = {
            "version": 1,
            "strategy": "multi_bot",
            "original_filename": original_filename,
            "total_parts": total_parts,
            "total_size": file_size,
            "parts": results,
        }

        summary_caption = (
            "[MULTIPART UPLOAD COMPLETED] "
            f"{original_filename}\n"
            f"Total size: {file_size / 1024 / 1024:.2f} MB\n"
            f"Parts: {total_parts}\n"
        )

        print("所有分片上传完毕，正在发送 manifest ...")
        try:
            with io.BytesIO(manifest_content.encode("utf-8")) as manifest_file:
                message = await self.bot.send_document(
                    chat_id=self.channel_name,
                    document=manifest_file,
                    filename=manifest_name,
                    caption=summary_caption + "Download: preparing...",
                )
        except Exception as e:
            print(f"发送 manifest 文件失败: {e}")
            return None

        if not message.document:
            return None

        composite_id = f"{message.message_id}:{message.document.file_id}"
        download_url = f"{self.settings.BASE_URL.rstrip('/')}/d/{composite_id}/{quote(original_filename)}"
        final_caption = summary_caption + f"Download: {download_url}"
        try:
            await self.bot.edit_message_caption(
                chat_id=self.channel_name,
                message_id=message.message_id,
                caption=final_caption,
            )
        except Exception as e:
            print(f"更新 manifest 消息说明失败: {e}")
        manifest_metadata["download_url"] = download_url
        manifest_metadata["manifest_message_id"] = message.message_id

        database.add_file_metadata(
            filename=original_filename,
            file_id=composite_id,
            filesize=file_size,
            is_multipart=True,
            manifest_data=manifest_metadata,
        )
        return composite_id

    async def upload_file(self, file_path: str, file_name: str) -> Optional[str]:
        """
        将文件上传到 Telegram 频道。根据文件大小和 bot 数量选择单 bot 或多 bot 分片。
        """
        if not self.channel_name:
            print("错误：环境变量中未设置 CHANNEL_NAME。")
            return None

        try:
            file_size = os.path.getsize(file_path)
        except OSError as e:
            print(f"无法获取文件大小: {e}")
            return None

        if self._should_use_multibot(file_size):
            return await self._upload_file_multibot(file_path, file_name)

        if file_size >= CHUNK_SIZE_BYTES:
            print(
                f"文件大小 ({file_size / 1024 / 1024:.2f} MB) 超过或等于 "
                f"{CHUNK_SIZE_BYTES / 1024 / 1024:.2f}MB。正在启动单 bot 分块上传..."
            )
            return await self._upload_as_chunks_single_bot(file_path, file_name)

        print(
            f"文件大小 ({file_size / 1024 / 1024:.2f} MB) 小于 "
            f"{CHUNK_SIZE_BYTES / 1024 / 1024:.2f}MB。正在直接上传..."
        )
        return await self._upload_small_file(file_path, file_name)

    async def get_download_url(self, file_id: str) -> Optional[str]:
        """
        为给定的 file_id 获取临时下载链接（默认 bot）。
        """
        try:
            file = await self.bot.get_file(file_id)
            return file.file_path
        except Exception as e:
            print(f"向 Telegram 获取下载链接时出错 {e}")
            return None

    async def get_download_url_for_bot(self, bot_name: str, file_id: str) -> Optional[str]:
        """
        针对指定 bot 获取下载链接，用于多 bot 分片下载。
        """
        bot_client = self.bot_map.get(bot_name)
        if not bot_client:
            print(f"警告：未找到名为 {bot_name} 的 bot，无法获取下载链接。")
            return None
        try:
            file = await bot_client.bot.get_file(file_id)
            return file.file_path
        except Exception as e:
            print(f"Bot {bot_name} 获取下载链接失败: {e}")
            return None

    async def delete_message(self, message_id: int) -> tuple[bool, str]:
        """
        从频道中删除指定 ID 的消息。
        """
        try:
            await self.bot.delete_message(
                chat_id=self.channel_name,
                message_id=message_id,
            )
            return (True, "deleted")
        except telegram.error.BadRequest as e:
            if "not found" in str(e).lower():
                print(f"消息 {message_id} 未找到，视为已删除。")
                return (True, "not_found")
            else:
                print(f"删除消息 {message_id} 失败 (BadRequest): {e}")
                return (False, "error")
        except Exception as e:
            print(f"删除消息 {message_id} 时发生未知错误 {e}")
            return (False, "error")

    async def delete_file_with_chunks(self, file_id: str) -> dict:
        """
        完全删除一个文件，包括其所有可能的分块。
        该函数会处理清单文件，并删除所有引用的分块。
        """
        results = {
            "status": "pending",
            "main_file_id": file_id,
            "deleted_chunks": [],
            "failed_chunks": [],
            "main_message_deleted": False,
            "is_manifest": False,
            "reason": "",
        }

        try:
            main_message_id_str, main_actual_file_id = file_id.split(":", 1)
            main_message_id = int(main_message_id_str)
        except (ValueError, IndexError):
            results["status"] = "error"
            results["reason"] = "Invalid composite file_id format."
            return results

        download_url = await self.get_download_url(main_actual_file_id)
        if not download_url:
            print(f"警告: 无法为文件{main_actual_file_id} 获取下载链接。将只尝试删除主消息。")
            results["reason"] = f"Could not get download URL for {main_actual_file_id}."
        else:
            try:
                import httpx

                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.get(download_url)
                    if response.status_code == 200 and response.content.startswith(b"tgstate-blob\n"):
                        results["is_manifest"] = True
                        print(f"文件 {file_id} 是一个清单文件。正在处理分块删除...")

                        manifest_content = response.content.decode("utf-8")
                        lines = manifest_content.strip().split("\n")
                        chunk_composite_ids = lines[2:]

                        for chunk_id in chunk_composite_ids:
                            try:
                                chunk_message_id_str, _ = chunk_id.split(":", 1)
                                chunk_message_id = int(chunk_message_id_str)
                                success, _ = await self.delete_message(chunk_message_id)
                                if success:
                                    results["deleted_chunks"].append(chunk_id)
                                else:
                                    results["failed_chunks"].append(chunk_id)
                            except Exception as e:
                                print(f"处理或删除分块{chunk_id} 时出错 {e}")
                                results["failed_chunks"].append(chunk_id)
            except Exception as e:
                error_message = f"下载或解析清单文件{file_id} 时出错 {e}"
                print(error_message)
                results["reason"] += " " + error_message

        main_message_deleted, delete_reason = await self.delete_message(main_message_id)
        results["main_message_deleted"] = main_message_deleted

        if main_message_deleted:
            if delete_reason == "deleted":
                print(f"主消息{main_message_id} 已成功删除。")
            elif delete_reason == "not_found":
                print(f"主消息{main_message_id} 在 Telegram 中未找到，视为成功。")
        else:
            print(f"删除主消息{main_message_id} 失败。")

        if results["main_message_deleted"] and (not results["is_manifest"] or not results["failed_chunks"]):
            results["status"] = "success"
        else:
            results["status"] = "partial_failure"
            if not results["main_message_deleted"]:
                results["reason"] += " Failed to delete main message."
            if results["failed_chunks"]:
                results["reason"] += f" Failed to delete {len(results['failed_chunks'])} chunks."

        return results

    async def list_files_in_channel(self) -> List[dict]:
        """
        遍历频道历史记录，智能地列出所有文件。
        - 小于20MB的文件直接显示。
        - 大于20MB但通过清单管理的文件，显示原始文件名。
        """
        files = []
        last_message_id = None
        MAX_ITERATIONS = 100

        print("开始从频道获取历史消息...")

        for i in range(MAX_ITERATIONS):
            try:
                messages = await self.bot.get_chat_history(
                    chat_id=self.channel_name,
                    limit=100,
                    offset_id=last_message_id if last_message_id else 0,
                )
            except Exception as e:
                print(f"获取聊天历史时出错 {e}")
                break

            if not messages:
                print("没有更多历史消息了。")
                break

            for message in messages:
                if message.document:
                    doc = message.document
                    if doc.file_size < 20 * 1024 * 1024 and not doc.file_name.endswith(".manifest"):
                        files.append(
                            {
                                "name": doc.file_name,
                                "file_id": doc.file_id,
                                "size": doc.file_size,
                            }
                        )
                    elif doc.file_name.endswith(".manifest"):
                        manifest_url = await self.get_download_url(doc.file_id)
                        if not manifest_url:
                            continue

                        import httpx

                        async with httpx.AsyncClient() as client:
                            try:
                                resp = await client.get(manifest_url)
                                if resp.status_code == 200 and resp.content.startswith(b"tgstate-blob\n"):
                                    lines = resp.content.decode("utf-8").strip().split("\n")
                                    original_filename = lines[1]
                                    files.append(
                                        {
                                            "name": original_filename,
                                            "file_id": doc.file_id,
                                            "size": None,
                                        }
                                    )
                            except httpx.RequestError:
                                continue

            last_message_id = messages[-1].message_id
            print(f"已处理批次{i+1}，最后的消息 ID: {last_message_id}")

        print(f"文件列表获取完毕，共找到 {len(files)} 个有效文件。")
        return files


@lru_cache()
def get_telegram_service() -> TelegramService:
    """
    TelegramService 的缓存工厂函数。
    """
    return TelegramService(settings=get_settings())
