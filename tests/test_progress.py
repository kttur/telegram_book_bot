import asyncio
import importlib
import os
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest, NetworkError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from catalogs import CatalogProgress, CatalogUnavailableError
from progress import OperationProgress, describe_action, status_delay_from_env


def make_update(message_id=10):
    source = SimpleNamespace(message_id=message_id, reply_markup=None)
    return SimpleNamespace(
        callback_query=SimpleNamespace(answer=AsyncMock(), message=source, data="123"),
        effective_message=source,
        effective_chat=SimpleNamespace(id=42),
    )


def make_bot(message_id=100):
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=message_id)),
        edit_message_text=AsyncMock(),
    )


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def tracker(self, label="Поиск книг: «Дюна»", **kwargs):
        update, bot = make_update(), make_bot()
        tracker = OperationProgress(update, bot, label, delay=0.01, edit_interval=0.001, **kwargs)
        return tracker, update, bot

    async def wait_sent(self, bot):
        async def wait():
            while not bot.send_message.await_count:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait(), timeout=1)

    async def test_fast_operation_keeps_callback_until_completion_without_status_message(self):
        tracker, update, bot = self.tracker()
        async with tracker:
            update.callback_query.answer.assert_not_awaited()
            bot.send_message.assert_not_awaited()
        update.callback_query.answer.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()
        self.assertTrue(tracker._monitor.done())

    async def test_slow_operation_sends_current_status_then_edits_same_message(self):
        tracker, update, bot = self.tracker()
        async with tracker:
            tracker.on_catalog_progress(CatalogProgress(1, 3, "Получение каталога", False))
            await self.wait_sent(bot)
            update.callback_query.answer.assert_awaited_once()
            text = bot.send_message.call_args.kwargs["text"]
            self.assertIn("Поиск книг: «Дюна»", text)
            self.assertIn("сервером 1 из 3", text)
            self.assertEqual(bot.send_message.call_args.kwargs["reply_to_message_id"], 10)
            tracker.on_catalog_progress(CatalogProgress(2, 3, "Получение cookie", True))
            async def wait_edit():
                while not bot.edit_message_text.await_count:
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_edit(), timeout=1)
            kwargs = bot.edit_message_text.call_args.kwargs
            self.assertEqual(kwargs["message_id"], 100)
            self.assertIn("сервером 2 из 3 через Tor", kwargs["text"])
            self.assertIn("Получение cookie", kwargs["text"])
        self.assertEqual(bot.edit_message_text.call_args.kwargs["text"], "Поиск книг: «Дюна»\nГотово.")
        bot.send_message.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()

    async def test_worker_thread_progress_updates_the_event_loop(self):
        tracker, _, bot = self.tracker()

        def work(*, on_progress):
            on_progress(CatalogProgress(2, 2, "Загрузка книги", True))
            return b"book"

        async with tracker:
            self.assertEqual(await tracker.run(work), b"book")
            await self.wait_sent(bot)
            self.assertIn("сервером 2 из 2", bot.send_message.call_args.kwargs["text"])
            self.assertIn("Загрузка книги", bot.send_message.call_args.kwargs["text"])

    async def test_independent_operations_have_distinct_labels_and_reply_targets(self):
        bot = make_bot()
        updates = [make_update(10), make_update(20)]
        bot.send_message.side_effect = [SimpleNamespace(message_id=100), SimpleNamespace(message_id=200)]

        async def work(index):
            async with OperationProgress(updates[index], bot, f"Поиск {index}", delay=0.01) as tracker:
                tracker.set_stage(f"Сервер {index}")
                async def wait():
                    while tracker.message is None:
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait(), timeout=1)

        await asyncio.gather(work(0), work(1))
        sent = bot.send_message.call_args_list
        self.assertEqual({call.kwargs["reply_to_message_id"] for call in sent}, {10, 20})
        self.assertEqual({call.kwargs["text"] for call in sent},
                         {"Поиск 0\nСервер 0", "Поиск 1\nСервер 1"})
        final = bot.edit_message_text.call_args_list
        self.assertEqual({call.kwargs["text"] for call in final}, {"Поиск 0\nГотово.", "Поиск 1\nГотово."})

    async def test_catalog_failure_reports_operation_even_before_delay(self):
        tracker, update, bot = self.tracker()
        async with tracker:
            raise CatalogUnavailableError("failed")
        self.assertIn("Поиск книг: «Дюна»", bot.send_message.call_args.kwargs["text"])
        self.assertIn("Каталоги сейчас недоступны", bot.send_message.call_args.kwargs["text"])
        update.callback_query.answer.assert_awaited_once()

    async def test_failure_replaces_existing_status_instead_of_sending_duplicate(self):
        tracker, _, bot = self.tracker()
        async with tracker:
            await self.wait_sent(bot)
            raise CatalogUnavailableError("failed")
        bot.send_message.assert_awaited_once()
        self.assertIn("Каталоги сейчас недоступны", bot.edit_message_text.call_args.kwargs["text"])

    async def test_expired_callback_does_not_interrupt_operation_or_status(self):
        tracker, update, bot = self.tracker()
        update.callback_query.answer.side_effect = BadRequest("Query is too old")
        async with tracker:
            await self.wait_sent(bot)
        self.assertIn("Готово", bot.edit_message_text.call_args.kwargs["text"])
        update.callback_query.answer.assert_awaited_once()

    async def test_status_api_error_does_not_interrupt_catalog_operation(self):
        tracker, _, bot = self.tracker()
        bot.send_message.side_effect = NetworkError("offline")
        async with tracker:
            await self.wait_sent(bot)
            tracker.set_stage("Следующее зеркало")
        self.assertTrue(tracker._monitor.done())

    async def test_unexpected_exception_is_reported_and_propagated(self):
        tracker, _, bot = self.tracker()
        with self.assertRaises(ValueError):
            async with tracker:
                raise ValueError("unexpected")
        self.assertIn("Не удалось выполнить", bot.send_message.call_args.kwargs["text"])

    async def test_cancellation_stops_monitor_and_finalizes_status(self):
        tracker, _, bot = self.tracker()
        with self.assertRaises(asyncio.CancelledError):
            async with tracker:
                await self.wait_sent(bot)
                raise asyncio.CancelledError()
        self.assertTrue(tracker._monitor.done())
        self.assertIn("отменена", bot.edit_message_text.call_args.kwargs["text"])

    async def test_command_without_callback_can_report_progress(self):
        tracker, update, bot = self.tracker()
        update.callback_query = None
        async with tracker:
            await self.wait_sent(bot)
        bot.send_message.assert_awaited_once()


class DescriptionTests(unittest.TestCase):
    def action(self, kind, value="", url="", label=None):
        return SimpleNamespace(action_type=kind, value=value, url=url, label=label)

    def test_operation_labels_include_query_filename_and_page(self):
        self.assertEqual(describe_action(self.action("search_books", "Дюна%20книга")),
                         "Поиск книг: «Дюна книга»")
        self.assertEqual(describe_action(self.action("search_authors", "Толстой")),
                         "Поиск авторов: «Толстой»")
        self.assertEqual(describe_action(self.action("download", "Толстой - Война и мир.epub")),
                         "Загрузка книги: «Толстой - Война и мир.epub»")
        self.assertEqual(describe_action(self.action("page", "2", label="Поиск книг: «Дюна»")),
                         "Поиск книг: «Дюна» · страница 3")
        self.assertIn("Дюна", describe_action(self.action("page", "1",
                      url="https://example.org/opds/search?searchTerm=%22Дюна%22")))
        self.assertIn("Дюна", describe_action(self.action("suggest_similar_books", '{"book_name": "Дюна"}')))

    def test_old_entry_without_label_uses_clicked_button(self):
        query = make_update().callback_query
        query.message.reply_markup = SimpleNamespace(inline_keyboard=[
            [SimpleNamespace(callback_data="123", text="Название книги")],
        ])
        self.assertEqual(describe_action(self.action("entry", "0"), query), "Открытие: «Название книги»")

    def test_status_delay_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(status_delay_from_env(), 8)
        for value in ["0", "-1", "15", "nan", "inf"]:
            with patch.dict(os.environ, {"BOT_STATUS_DELAY": value}), self.assertRaises(ValueError):
                status_delay_from_env()


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection = sqlite3.connect(":memory:")
        ai_module = ModuleType("adapters.ai.openai")
        ai_module.OpenAiAdapter = Mock()
        with patch.dict(os.environ, {}, clear=True), \
                patch("sqlite3.connect", return_value=cls.connection), \
                patch.dict(sys.modules, {"adapters.ai.openai": ai_module}):
            cls.main = importlib.import_module("main")

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()
        sys.modules.pop("main", None)

    async def test_download_button_keeps_loading_until_file_is_sent(self):
        update, bot = make_update(), make_bot()
        bot.send_document = AsyncMock()
        action = self.main.Action(action_type="download", url="https://example.org/book", value="Дюна.epub")
        repository = Mock()
        repository.get.return_value = action
        catalog = Mock()
        catalog.get.return_value = SimpleNamespace(content=b"book")

        async def sent_document(**kwargs):
            update.callback_query.answer.assert_not_awaited()

        bot.send_document.side_effect = sent_document
        with patch.object(self.main, "action_repository", repository), \
                patch.object(self.main, "catalog_client", catalog):
            await self.main.handle_callback(update, SimpleNamespace(bot=bot))
        update.callback_query.answer.assert_awaited_once()
        bot.send_document.assert_awaited_once()
        self.assertEqual(bot.send_document.call_args.kwargs["filename"], "Дюна.epub")
        bot.send_message.assert_not_awaited()
        self.assertIn("on_progress", catalog.get.call_args.kwargs)
        self.assertEqual(catalog.get.call_args.kwargs["operation"], "Загрузка книги")

    async def test_slow_download_keeps_one_status_through_upload_and_completion(self):
        update, bot = make_update(), make_bot()
        action = self.main.Action(action_type="download", url="https://example.org/book", value="Дюна.epub")
        repository = Mock()
        repository.get.return_value = action
        release_download = threading.Event()
        release_upload = asyncio.Event()

        def download(url, *, on_progress, operation):
            on_progress(CatalogProgress(2, 3, operation, True))
            if not release_download.wait(2):
                raise RuntimeError("Test did not release the download")
            return SimpleNamespace(content=b"book")

        async def upload(**kwargs):
            await release_upload.wait()

        catalog = SimpleNamespace(get=download)
        bot.send_document = AsyncMock(side_effect=upload)

        def tracker(*args, **kwargs):
            return OperationProgress(*args, **kwargs, edit_interval=0.001)

        with patch.object(self.main, "action_repository", repository), \
                patch.object(self.main, "catalog_client", catalog), \
                patch.object(self.main, "status_delay", 0.01), \
                patch.object(self.main, "OperationProgress", tracker):
            task = asyncio.create_task(self.main.handle_callback(update, SimpleNamespace(bot=bot)))
            try:
                async def wait_sent():
                    while not bot.send_message.await_count:
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait_sent(), timeout=1)
                text = bot.send_message.call_args.kwargs["text"]
                self.assertIn("Дюна.epub", text)
                self.assertIn("сервером 2 из 3 через Tor", text)
                update.callback_query.answer.assert_awaited_once()
                release_download.set()
                async def wait_upload_status():
                    while not any("Отправка книги" in call.kwargs["text"]
                                  for call in bot.edit_message_text.call_args_list):
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait_upload_status(), timeout=1)
                release_upload.set()
                await asyncio.wait_for(task, timeout=1)
            finally:
                release_download.set()
                release_upload.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        bot.send_message.assert_awaited_once()
        self.assertIn("Готово", bot.edit_message_text.call_args.kwargs["text"])

    async def test_short_recommendation_is_sent_and_does_not_block_timer(self):
        update, bot = make_update(), make_bot()
        action = self.main.Action(action_type="suggest_similar_books", url="",
                                 value='{"book_name": "Дюна", "authors": "Герберт", "summary": ""}')
        repository = Mock()
        repository.get.return_value = action
        adapter = Mock()
        adapter.get_similar_books.return_value = "Рекомендация"
        with patch.object(self.main, "action_repository", repository), \
                patch.object(self.main, "ai_adapter", adapter):
            await self.main.handle_callback(update, SimpleNamespace(bot=bot))
        self.assertEqual(bot.send_message.call_args.kwargs["text"], "Рекомендация")

    def test_action_labels_preserve_old_hashes_and_json(self):
        old = self.main.Action(action_type="entry", url="https://example.org/opds", value="0")
        labelled = old.copy(update={"label": "Дюна"})
        self.assertEqual(hash(old), hash(labelled))
        restored = self.main.Action.parse_raw('{"action_type":"entry","url":"https://example.org/opds","value":"0"}')
        self.assertIsNone(restored.label)


if __name__ == "__main__":
    unittest.main()
