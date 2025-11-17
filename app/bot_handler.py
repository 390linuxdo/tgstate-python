import asyncio
import json
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from . import database
from .core.config import get_settings, get_telegram_bots
from .events import file_update_queue
from .services.telegram_service import get_telegram_service


async def handle_new_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理新增的文件或照片，将其元数据存入数据库，并通过队列发送通知。
    仅处理来自允许频道且非 bot 用户的消息，避免与 API 上传产生重复。
    """
    message = update.message or update.channel_post

    if not message:
        return

    # 忽略 bot 发送的内容（API 上传和并行分片均由 bot 推送到频道）
    if message.from_user and getattr(message.from_user, "is_bot", False):
        return

    chat = message.chat
    bots = get_telegram_bots()
    allowed_usernames = {
        bot.channel_name.lstrip("@").lower() for bot in bots if bot.channel_name.startswith("@")
    }
    allowed_ids = {bot.channel_name for bot in bots if not bot.channel_name.startswith("@")}

    chat_username = (chat.username or "").lower()
    chat_id_str = str(chat.id)

    is_allowed = False
    if allowed_usernames and chat_username in allowed_usernames:
        is_allowed = True
    if allowed_ids and chat_id_str in allowed_ids:
        is_allowed = True

    if not is_allowed:
        return

    file_obj = None
    file_name = None

    if message.document:
        file_obj = message.document
        file_name = file_obj.file_name
    elif message.photo:
        file_obj = message.photo[-1]
        file_name = f"photo_{message.message_id}.jpg"

    if file_obj and file_name:
        if file_obj.file_size < (20 * 1024 * 1024) and not file_name.endswith(".manifest"):
            composite_id = f"{message.message_id}:{file_obj.file_id}"

            database.add_file_metadata(
                filename=file_name,
                file_id=composite_id,
                filesize=file_obj.file_size,
            )

            file_info = {
                "action": "add",
                "filename": file_name,
                "file_id": composite_id,
                "filesize": file_obj.file_size,
                "upload_date": message.date.isoformat(),
            }
            await file_update_queue.put(json.dumps(file_info))


async def handle_get_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理对文件消息回复 "get" 的情况。
    """
    if not (
        update.message
        and update.message.reply_to_message
        and (update.message.reply_to_message.document or update.message.reply_to_message.photo)
    ):
        return

    if update.message.text.lower().strip() != "get":
        return

    document = update.message.reply_to_message.document or update.message.reply_to_message.photo[-1]
    file_id = document.file_id
    file_name = getattr(document, "file_name", f"photo_{update.message.reply_to_message.message_id}.jpg")
    settings = get_settings()

    final_file_id = file_id
    final_file_name = file_name

    if file_name.endswith(".manifest"):
        telegram_service = get_telegram_service()
        download_url = await telegram_service.get_download_url(file_id)
        if download_url:
            import httpx

            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.get(download_url)
                    resp.raise_for_status()
                    content = resp.content
                    if content.startswith(b"tgstate-blob\n"):
                        lines = content.decode("utf-8").strip().split("\n")
                        final_file_name = lines[1]
                except httpx.RequestError as e:
                    print(f"下载清单文件时出错 {e}")
                    await update.message.reply_text("错误：无法获取清单文件内容。")
                    return

    file_path = f"/d/{final_file_id}"

    if settings.BASE_URL:
        download_link = f"{settings.BASE_URL.strip('/')}{file_path}"
        reply_text = f"这是 '{final_file_name}' 的下载链接:\n{download_link}"
    else:
        reply_text = f"这是 '{final_file_name}' 的下载路径(请自行拼接域名):\n`{file_path}`"

    await update.message.reply_text(reply_text)


async def handle_deleted_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    处理消息删除事件，同步删除数据库中的文件记录。
    """
    if update.edited_message and not update.edited_message.text:
        message_id = update.edited_message.message_id
        deleted_file_id = database.delete_file_by_message_id(message_id)
        if deleted_file_id:
            delete_info = {
                "action": "delete",
                "file_id": deleted_file_id,
            }
            await file_update_queue.put(json.dumps(delete_info))


def create_bot_app() -> Application:
    """
    创建并配置 Telegram Bot 应用实例。
    """
    settings = get_settings()
    if not settings.BOT_TOKEN:
        print("错误: .env 文件中未设置 BOT_TOKEN。机器人无法创建。")
        raise ValueError("BOT_TOKEN not configured.")

    application = Application.builder().token(settings.BOT_TOKEN).build()

    get_handler = MessageHandler(
        filters.TEXT & (~filters.COMMAND) & filters.REPLY,
        handle_get_reply,
    )
    application.add_handler(get_handler)

    new_file_handler = MessageHandler(filters.ALL, handle_new_file)
    application.add_handler(new_file_handler, group=0)

    delete_handler = MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_deleted_message)
    application.add_handler(delete_handler, group=1)

    return application
