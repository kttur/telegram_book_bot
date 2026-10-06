import asyncio
import hashlib
import io
import json
import logging
import os
import sqlite3

from bs4 import BeautifulSoup
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from adapters.ai.openai import OpenAiAdapter
from book_card import cover_wait_timeout_from_env, send_book_card
from catalogs import CatalogClient, CatalogUnavailableError
from progress import OperationProgress, describe_action, status_delay_from_env


catalog_client = CatalogClient.from_env()
status_delay = status_delay_from_env()
cover_wait_timeout = cover_wait_timeout_from_env()
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ai_adapter = OpenAiAdapter(api_key=os.environ.get("OPENAI_API_KEY"))


class Action(BaseModel):
    action_type: str
    url: str
    value: str | None = None
    label: str | None = None

    def __hash__(self):
        hash_str = f"{self.action_type}{self.url}{self.value}"
        return int(hashlib.sha256(hash_str.encode('utf-8')).hexdigest(), 16)


class SQLiteActionRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self.cursor.execute(
            "CREATE TABLE IF NOT EXISTS actions "
            "(action_hash INT PRIMARY KEY, action_json TEXT UNIQUE)"
        )
        self.conn.commit()

    def add(self, action: Action):
        self.cursor.execute(
            "INSERT OR IGNORE INTO actions VALUES (?, ?)",
            (hash(action), action.json())
        )
        self.conn.commit()

    def get(self, action_hash: int) -> Action:
        self.cursor.execute(
            "SELECT action_json FROM actions WHERE action_hash = ?",
            (action_hash,)
        )
        results = self.cursor.fetchone()
        return Action(**json.loads(results[0])) if results else None


action_repository = SQLiteActionRepository(db_path="books.db")


class Link(BaseModel):
    href: str
    type: str
    title: str | None = None
    rel: str | None = None

    @property
    def content(self) -> dict[str, "Entry"]:
        return get_entries(self.href)


class Entry(BaseModel):
    text: str
    links: list[Link]
    summary: str = ""
    authors: list[str] = None


def get_entries(link: str, *, on_progress=None) -> list[Entry]:
    feed = catalog_client.get_feed(link, on_progress=on_progress)
    return [
        Entry(
            text=entry.title,
            links=[Link(**link) for link in entry.links],
            summary=BeautifulSoup(
                entry.summary, features='html.parser'
            ).get_text(
                '\n', strip=True
            ) if 'summary' in entry else '',
            authors=[author.name for author in entry.authors] if 'authors' in entry else None,
        )
        for entry in feed["entries"]
    ]


async def handle_message(update: Update, context):
    chat_id = update.effective_chat.id

    logger.debug(update.message.to_dict())
    message = update.message.text.replace(" ", "%20")
    action_books = Action(action_type="search_books", url="", value=message)
    action_authors = Action(action_type="search_authors", url="", value=message)
    action_repository.add(action_books)
    action_repository.add(action_authors)

    keyboard = [
        [
            InlineKeyboardButton("Поиск по книгам", callback_data=hash(action_books)),
            InlineKeyboardButton("Поиск по авторам", callback_data=hash(action_authors)),
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f'Поиск "{update.message.text}"',
        reply_markup=reply_markup
    )


async def get_page_url(url: str, page: int = 0) -> str:
    if "/search?" in url:
        url = f"{url}&pageNumber={page}" if page > 0 else url
    else:
        url = f"{url}/{page}" if page > 0 else url
    return url


async def handle_search(update: Update, context, action: Action, progress):
    logger.debug(update.callback_query.data)
    base_url = catalog_client.base_url
    if action.action_type == "search_authors":
        search_url = f'{base_url}/search?searchType=authors&searchTerm="{action.value}"'
    elif action.action_type == "search_books":
        search_url = f'{base_url}/search?searchType=books&searchTerm="{action.value}"'
    else:
        search_url = f'{base_url}//search?searchTerm="{action.value}"'

    url = await get_page_url(search_url, 0)
    entries = await progress.run(get_entries, url)
    progress.set_stage("Отправка результатов поиска…")
    keyboard = []
    for i, entry in enumerate(entries):
        action = Action(action_type="entry", url=search_url, value=str(i), label=entry.text)
        action_repository.add(action)
        keyboard.append([InlineKeyboardButton(entry.text, callback_data=hash(action))])
    next_page_action = Action(action_type="page", url=search_url, value="1", label=progress.label)
    action_repository.add(next_page_action)
    control_keyboard = [
        InlineKeyboardButton("Вперед", callback_data=hash(next_page_action)),
    ]
    keyboard.append(control_keyboard)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Результаты поиска",
                                   reply_markup=reply_markup)


def get_book_name(entry: Entry) -> str:
    if entry.authors:
        return f"{', '.join(entry.authors)} - {entry.text}"
    else:
        return entry.text


async def handle_callback(update: Update, context):
    logger.debug(update.callback_query.data)
    action = action_repository.get(update.callback_query.data)
    logger.debug(action)
    async with OperationProgress(
        update, context.bot, describe_action(action, update.callback_query), delay=status_delay,
    ) as progress:
        if action is None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Эта кнопка устарела. Повторите поиск или отправьте /start.",
            )
            return
        await handle_action(update, context, action, progress)


async def handle_action(update: Update, context, action: Action, progress):
    match action.action_type:
        case "search_books" | "search_authors":
            await handle_search(update, context, action, progress)
        case "suggest_similar_books":
            progress.set_stage("Подбор похожих книг…")
            similar_books = await asyncio.to_thread(
                ai_adapter.get_similar_books, **json.loads(action.value),
            )
            progress.set_stage("Отправка рекомендаций…")
            for i in range(0, len(similar_books), 4000):
                await context.bot.send_message(chat_id=update.effective_chat.id, text=similar_books[i:i + 4000])
        case "entry":
            entries = await progress.run(get_entries, action.url)
            entry = entries[int(action.value)]
            progress.set_stage("Подготовка описания книги…")
            keyboard = []
            images = {}
            for link in entry.links:
                match link.type:
                    case "application/atom+xml" | "application/atom+xml;profile=opds-catalog":
                        action = Action(action_type="page", url=link.href, value="0", label=entry.text)
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "...", callback_data=hash(action))])
                    case "text/html":
                        keyboard.append([InlineKeyboardButton(link.title or "Сайт", url=link.href)])
                    case "application/epub+zip" | "application/epub":
                        action = Action(action_type="download", url=link.href, value=f"{get_book_name(entry)}.epub")
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "epub", callback_data=hash(action))])
                    case "application/fb2+zip":
                        action = Action(action_type="download", url=link.href, value=f"{get_book_name(entry)}.fb2")
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "fb2", callback_data=hash(action))])
                    case "application/pdf":
                        action = Action(action_type="download", url=link.href, value=f"{get_book_name(entry)}.pdf")
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "pdf", callback_data=hash(action))])
                    case "application/rtf+zip":
                        action = Action(action_type="download", url=link.href, value=f"{get_book_name(entry)}.zip")
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "rtf+zip", callback_data=hash(action))])
                    case "application/x-mobipocket-ebook":
                        action = Action(action_type="download", url=link.href, value=f"{get_book_name(entry)}.mobi")
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "mobi", callback_data=hash(action))])
                    case "application/txt+zip":
                        action = Action(action_type="download", url=link.href, value=f"{get_book_name(entry)}.zip")
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "txt+zip", callback_data=hash(action))])
                    case "application/djvu":
                        action = Action(action_type="download", url=link.href, value=f"{get_book_name(entry)}.djvu")
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "djvu", callback_data=hash(action))])
                    case "application/html+zip":
                        action = Action(action_type="download", url=link.href, value=f"{get_book_name(entry)}.zip")
                        action_repository.add(action)
                        keyboard.append([InlineKeyboardButton(link.title or "html+zip", callback_data=hash(action))])
                    case "image/jpeg" | "image/png" | "image/webp":
                        images[link.rel] = link.href
                    case _:
                        pass
            image = images.get("x-stanza-cover-image") \
                    or images.get('http://opds-spec.org/image') \
                    or images.get("http://opds-spec.org/image/thumbnail") \
                    or images.get("x-stanza-cover-image-thumbnail")

            authors = ", ".join(author for author in entry.authors) if entry.authors else None
            text = f"{entry.text}\n{f'{authors}' if authors else ''}\n\n{entry.summary or ''}"

            suggest_similar_books_action = Action(action_type="suggest_similar_books", url='', value=json.dumps({'authors': authors, 'book_name': entry.text, 'summary': entry.summary}))
            action_repository.add(suggest_similar_books_action)
            keyboard.append([InlineKeyboardButton("Посоветуй похожие книги", callback_data=hash(suggest_similar_books_action))])

            reply_markup = InlineKeyboardMarkup(keyboard)

            await send_book_card(
                update, context, progress, catalog_client,
                text=text, reply_markup=reply_markup, image_url=image,
                wait_timeout=cover_wait_timeout,
            )
        case "page":
            page_url = await get_page_url(action.url, int(action.value))
            entries = await progress.run(get_entries, page_url)
            progress.set_stage("Отправка страницы каталога…")
            keyboard = []
            for i, entry in enumerate(entries):
                entry_action = Action(action_type="entry", url=page_url, value=str(i), label=entry.text)
                action_repository.add(entry_action)
                keyboard.append([InlineKeyboardButton(entry.text, callback_data=hash(entry_action))])
            control_keyboard = []
            if int(action.value) > 0:
                prev_page_action = Action(action_type="page", url=action.url, value=str(int(action.value) - 1), label=action.label)
                action_repository.add(prev_page_action)
                control_keyboard.append(InlineKeyboardButton("Назад", callback_data=hash(prev_page_action)))
            if entries:
                next_page_action = Action(action_type="page", url=action.url, value=str(int(action.value) + 1), label=action.label)
                action_repository.add(next_page_action)
                control_keyboard.append(InlineKeyboardButton("Вперед", callback_data=hash(next_page_action)))
            if control_keyboard:
                keyboard.append(control_keyboard)
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Результаты поиска",
                reply_markup=reply_markup
            )
        case "download":
            file_resp = await progress.run(catalog_client.get, action.url, operation="Загрузка книги")
            progress.set_stage("Отправка книги в Telegram…")
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=io.BytesIO(file_resp.content),
                filename=action.value or "book"
            )


async def handle_start(update: Update, context):
    async with OperationProgress(update, context.bot, "Открытие каталога", delay=status_delay) as progress:
        await show_start(update, context, progress)


async def show_start(update: Update, context, progress):
    base_url = catalog_client.base_url
    entries = await progress.run(get_entries, base_url)
    progress.set_stage("Отправка каталога…")
    keyboard = []
    for i, entry in enumerate(entries):
        action = Action(action_type="entry", url=base_url, value=str(i), label=entry.text)
        action_repository.add(action)
        keyboard.append([InlineKeyboardButton(entry.text, callback_data=hash(action))])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Выберите действие или отправьте текст для поиска по авторам и книгам",
        reply_markup=reply_markup
    )


async def handle_error(update: object, context):
    if isinstance(context.error, CatalogUnavailableError) and isinstance(update, Update):
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Каталоги сейчас недоступны. Попробуйте ещё раз немного позже.",
            )
        return
    error = context.error
    logger.error("Unhandled bot error", exc_info=(type(error), error, error.__traceback__))


def main():
    logger.info("Starting bot")
    app = ApplicationBuilder().token(os.environ.get("TOKEN")).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.add_error_handler(handle_error)
    try:
        app.run_polling()
    finally:
        catalog_client.close()


if __name__ == "__main__":
    main()
