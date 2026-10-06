"""Send book details promptly and attach a slow cover to the same message."""

import asyncio
import logging
import math
import os
import threading

from telegram import InputMediaPhoto
from telegram.error import BadRequest, TelegramError

from catalogs import CatalogUnavailableError


logger = logging.getLogger(__name__)


def cover_wait_timeout_from_env():
    timeout = float(os.getenv("COVER_WAIT_TIMEOUT", "2"))
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("COVER_WAIT_TIMEOUT must be a non-negative finite number")
    return timeout


def split_text(text, limit):
    """Split plain text without exceeding Telegram's UTF-16 length limits."""
    chunks = []
    while text:
        units = 0
        end = 0
        for char in text:
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > limit:
                break
            units += size
            end += 1
        if end < len(text):
            # Prefer a paragraph/word boundary, preserving all characters.
            boundary = text.rfind("\n", 0, end)
            if boundary < end // 2:
                boundary = text.rfind(" ", 0, end)
            if boundary >= end // 2:
                end = boundary + 1
        chunks.append(text[:end])
        text = text[end:]
    return chunks


def _download_cover(client, url, on_progress):
    try:
        return client.get(url, on_progress=on_progress, operation="Загрузка обложки").content
    except CatalogUnavailableError:
        logger.warning("Cover is unavailable: %s", url)
        return None
    finally:
        # This runs in the worker even if its awaiting coroutine is cancelled.
        client.close()


async def _attach_cover(download, bot, chat_id, message_id, caption, reply_markup):
    try:
        cover = await download
        if cover:
            await bot.edit_message_media(
                chat_id=chat_id, message_id=message_id,
                media=InputMediaPhoto(media=cover, caption=caption, parse_mode=None),
                reply_markup=reply_markup,
            )
    except TelegramError:
        # Deleted messages or invalid images must not affect the usable book card.
        logger.warning("Could not attach cover to message %s", message_id, exc_info=True)


async def send_book_card(update, context, progress, client, *, text, reply_markup,
                         image_url=None, wait_timeout=2):
    chunks = split_text(text or "...", 1024)
    caption = chunks[0]
    remainder = "".join(chunks[1:])
    download = None
    deferred = False
    handed_off = False
    foreground = threading.Event()
    foreground.set()

    def report(event):
        if foreground.is_set():
            progress.on_catalog_progress(event)

    try:
        cover = None
        if image_url:
            # Isolate sessions/cookies and mirror selection from subsequent requests.
            cover_client = client.fork()
            download = asyncio.create_task(asyncio.to_thread(
                _download_cover, cover_client, image_url, report,
            ))
            done, _ = await asyncio.wait({download}, timeout=wait_timeout)
            if done:
                cover = download.result()
            else:
                deferred = True
        foreground.clear()
        progress.set_stage("Отправка описания книги…")
        chat_id = update.effective_chat.id
        card = None
        if cover:
            try:
                card = await context.bot.send_photo(
                    chat_id=chat_id, photo=cover, caption=caption,
                    reply_markup=reply_markup, parse_mode=None,
                )
            except BadRequest:
                logger.warning("Telegram rejected the book cover", exc_info=True)
        if card is None:
            card = await context.bot.send_message(
                chat_id=chat_id, text=caption,
                reply_markup=reply_markup, parse_mode=None,
            )
        for chunk in split_text(remainder, 4096):
            await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=None)
        if deferred:
            context.application.create_task(
                _attach_cover(download, context.bot, chat_id, card.message_id, caption, reply_markup),
                update=update,
            )
            handed_off = True
        return card
    finally:
        foreground.clear()
        if download is not None and not handed_off:
            if not download.done():
                download.cancel()
            # Retrieve exceptions and finish the coroutine on early Telegram errors.
            await asyncio.gather(download, return_exceptions=True)
