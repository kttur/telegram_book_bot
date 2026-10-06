import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from book_card import cover_wait_timeout_from_env, send_book_card, split_text
from catalogs import CatalogProgress, CatalogUnavailableError


def units(text):
    return len(text.encode("utf-16-le")) // 2


class BookCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = []
        self.releases = []
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=124)),
            edit_message_media=AsyncMock(),
        )
        self.update = SimpleNamespace(effective_chat=SimpleNamespace(id=42))
        self.context = SimpleNamespace(bot=self.bot, application=SimpleNamespace(create_task=self.create_task))
        self.progress = SimpleNamespace(set_stage=Mock(), on_catalog_progress=Mock())
        self.markup = InlineKeyboardMarkup([[InlineKeyboardButton("epub", callback_data="123")]])

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), timeout=2)

    def create_task(self, coroutine, update=None):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def client(self, *, slow=False, error=None):
        release = threading.Event()
        self.releases.append(release)
        if not slow:
            release.set()
        child = SimpleNamespace(close=Mock())

        def get(url, *, on_progress, operation):
            on_progress(CatalogProgress(2, 3, operation, True))
            if not release.wait(2):
                raise RuntimeError("Test failed to release cover")
            if error:
                raise error
            return SimpleNamespace(content=b"cover bytes")

        child.get = Mock(side_effect=get)
        parent = SimpleNamespace(fork=Mock(return_value=child))
        return parent, child, release

    async def send(self, client, **kwargs):
        params = dict(text="Дюна\nФрэнк Герберт\n\nОписание", reply_markup=self.markup,
                      image_url="http://library.onion/cover.jpg", wait_timeout=0.01)
        params.update(kwargs)
        return await send_book_card(self.update, self.context, self.progress, client, **params)

    async def test_fast_cover_is_sent_with_card_and_no_background_edit(self):
        client, child, _ = self.client()
        card = await self.send(client, wait_timeout=1)
        self.assertEqual(card.message_id, 124)
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_media.assert_not_awaited()
        self.assertEqual(self.bot.send_photo.call_args.kwargs["photo"], b"cover bytes")
        self.assertIs(self.bot.send_photo.call_args.kwargs["reply_markup"], self.markup)
        child.close.assert_called_once()
        self.assertEqual(self.tasks, [])

    async def test_slow_cover_does_not_block_details_and_edits_same_message_later(self):
        client, child, release = self.client(slow=True)
        card = await self.send(client)
        self.assertFalse(release.is_set())
        self.assertEqual(card.message_id, 123)
        self.bot.send_message.assert_awaited_once()
        self.bot.send_photo.assert_not_awaited()
        self.bot.edit_message_media.assert_not_awaited()
        release.set()
        await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=1)
        self.bot.edit_message_media.assert_awaited_once()
        kwargs = self.bot.edit_message_media.call_args.kwargs
        self.assertEqual(kwargs["message_id"], 123)
        self.assertEqual(kwargs["chat_id"], 42)
        self.assertEqual(kwargs["media"].caption, self.bot.send_message.call_args.kwargs["text"])
        self.assertEqual(kwargs["media"].media.input_file_content, b"cover bytes")
        self.assertIs(kwargs["reply_markup"], self.markup)
        child.close.assert_called_once()

    async def test_zero_wait_sends_details_without_waiting_for_cover(self):
        client, _, release = self.client(slow=True)
        await self.send(client, wait_timeout=0)
        self.bot.send_message.assert_awaited_once()
        self.assertFalse(release.is_set())
        self.assertEqual(len(self.tasks), 1)

    async def test_missing_cover_does_not_create_worker_or_background_task(self):
        client, _, _ = self.client()
        await self.send(client, image_url=None)
        client.fork.assert_not_called()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.tasks, [])

    async def test_cover_failure_before_timeout_leaves_text_and_buttons(self):
        client, child, _ = self.client(error=CatalogUnavailableError("down"))
        await self.send(client, wait_timeout=1)
        self.bot.send_message.assert_awaited_once()
        self.bot.send_photo.assert_not_awaited()
        self.assertIs(self.bot.send_message.call_args.kwargs["reply_markup"], self.markup)
        child.close.assert_called_once()
        self.assertEqual(self.tasks, [])

    async def test_cover_failure_after_timeout_does_not_edit_or_send_extra_message(self):
        client, child, release = self.client(slow=True, error=CatalogUnavailableError("down"))
        await self.send(client)
        release.set()
        await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=1)
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_media.assert_not_awaited()
        child.close.assert_called_once()

    async def test_rejected_fast_cover_falls_back_to_text(self):
        client, _, _ = self.client()
        self.bot.send_photo.side_effect = BadRequest("invalid image")
        await self.send(client, wait_timeout=1)
        self.bot.send_message.assert_awaited_once()
        self.assertIs(self.bot.send_message.call_args.kwargs["reply_markup"], self.markup)

    async def test_deleted_card_or_rejected_edit_does_not_break_background_task(self):
        client, _, release = self.client(slow=True)
        self.bot.edit_message_media.side_effect = BadRequest("message to edit not found")
        await self.send(client)
        release.set()
        await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=1)
        self.bot.send_message.assert_awaited_once()
        self.bot.send_photo.assert_not_awaited()

    async def test_long_description_is_preserved_and_main_card_is_caption_safe(self):
        client, _, release = self.client(slow=True)
        text = "Дюна\nАвтор\n\n" + ("Большое описание 📚\n" * 600)
        await self.send(client, text=text)
        sent = self.bot.send_message.call_args_list
        self.assertEqual("".join(call.kwargs["text"] for call in sent), text)
        self.assertLessEqual(units(sent[0].kwargs["text"]), 1024)
        self.assertTrue(all(units(call.kwargs["text"]) <= 4096 for call in sent[1:]))
        self.assertIs(sent[0].kwargs["reply_markup"], self.markup)
        self.assertTrue(all("reply_markup" not in call.kwargs for call in sent[1:]))
        release.set()
        await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=1)
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["media"].caption, sent[0].kwargs["text"])

    async def test_slow_covers_for_two_books_edit_their_own_card(self):
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=123), SimpleNamespace(message_id=456)]
        first, _, release_first = self.client(slow=True)
        second, _, release_second = self.client(slow=True)
        await self.send(first, text="Первая книга")
        await self.send(second, text="Вторая книга")
        release_second.set()
        await asyncio.wait_for(self.tasks[1], timeout=1)
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["message_id"], 456)
        release_first.set()
        await asyncio.wait_for(self.tasks[0], timeout=1)
        pairs = {(call.kwargs["message_id"], call.kwargs["media"].caption)
                 for call in self.bot.edit_message_media.call_args_list}
        self.assertEqual(pairs, {(123, "Первая книга"), (456, "Вторая книга")})

    async def test_failed_card_send_cancels_async_download_but_worker_closes_client(self):
        client, child, release = self.client(slow=True)
        closed = asyncio.Event()
        loop = asyncio.get_running_loop()
        child.close.side_effect = lambda: loop.call_soon_threadsafe(closed.set)
        self.bot.send_message.side_effect = NetworkError("offline")
        with self.assertRaises(NetworkError):
            await self.send(client)
        self.assertEqual(self.tasks, [])
        release.set()
        await asyncio.wait_for(closed.wait(), timeout=1)
        child.close.assert_called_once()


class CoverSettingsTests(unittest.TestCase):
    def test_wait_setting_default_zero_fractional_and_invalid(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cover_wait_timeout_from_env(), 2)
        for value in ["0", "0.5", "3"]:
            with patch.dict(os.environ, {"COVER_WAIT_TIMEOUT": value}):
                self.assertEqual(cover_wait_timeout_from_env(), float(value))
        for value in ["-1", "nan", "inf", "bad"]:
            with patch.dict(os.environ, {"COVER_WAIT_TIMEOUT": value}), self.assertRaises(ValueError):
                cover_wait_timeout_from_env()

    def test_split_preserves_text_and_emoji_limits(self):
        for text in ["", "📚" * 1024, "слово " * 1024, "x" * 1025, "строка\n" * 1000]:
            with self.subTest(text=text[:30]):
                chunks = split_text(text, 1024)
                self.assertEqual("".join(chunks), text)
                self.assertTrue(all(units(chunk) <= 1024 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
