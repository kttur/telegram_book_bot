import base64
import hashlib
import io
import json
import logging
import os
import sqlite3
import time

import feedparser
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel
from requests.auth import HTTPBasicAuth
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from typing import Optional

base_url = "https://flibusta.is/opds"
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


OPDS_USER = os.getenv("OPDS_USER")
OPDS_PASS = os.getenv("OPDS_PASS")

cookies = []


def _get_basic_auth_header() -> Optional[dict[str, str]]:
    if OPDS_USER and OPDS_PASS:
        token = base64.b64encode(f"{OPDS_USER}:{OPDS_PASS}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return None


def cookies_are_expired(cookies) -> bool:
    expires = [cookie.expires for cookie in cookies if cookie.expires]
    min_expires = min(expires) if expires else 0
    return min_expires < time.time()


def get_cookies():
    global cookies

    if cookies_are_expired(cookies):
        try:
            resp = requests.get(f'{base_url}/polka/', headers=_get_basic_auth_header())
            cookies = resp.cookies
        except Exception as e:
            logger.error(e)
            cookies = []

    return cookies


class Action(BaseModel):
    action_type: str
    url: str
    value: str | None = None

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


def get_entries(link: str) -> list[Entry]:
    cookies = get_cookies()
    if cookies:
        resp = requests.get(link, cookies=cookies, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    else:
        feed = feedparser.parse(link)
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
    if url.startswith('http://flibusta.is/opds/search?'):
        url = f"{url}&pageNumber={page}" if page > 0 else url
    else:
        url = f"{url}/{page}" if page > 0 else url
    return url


async def handle_search(update: Update, context, action: Action):
    logger.debug(update.callback_query.data)
    if action.action_type == "search_authors":
        search_url = f'{base_url}/search?searchType=authors&searchTerm="{action.value}"'
    elif action.action_type == "search_books":
        search_url = f'{base_url}/search?searchType=books&searchTerm="{action.value}"'
    else:
        search_url = f'{base_url}//search?searchTerm="{action.value}"'

    url = await get_page_url(search_url, 0)
    entries = get_entries(url)
    keyboard = []
    for i, entry in enumerate(entries):
        action = Action(action_type="entry", url=search_url, value=str(i))
        action_repository.add(action)
        keyboard.append([InlineKeyboardButton(entry.text, callback_data=hash(action))])
    next_page_action = Action(action_type="page", url=search_url, value="1")
    action_repository.add(next_page_action)
    control_keyboard = [
        InlineKeyboardButton("Вперед", callback_data=hash(next_page_action)),
    ]
    keyboard.append(control_keyboard)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Результаты поиска",
                                   reply_markup=reply_markup)
    await update.callback_query.answer()


def get_book_name(entry: Entry) -> str:
    if entry.authors:
        return f"{', '.join(entry.authors)} - {entry.text}"
    else:
        return entry.text


async def handle_callback(update: Update, context):
    logger.debug(update.callback_query.data)
    action = action_repository.get(update.callback_query.data)
    logger.debug(action)

    match action.action_type:
        case "search_books" | "search_authors":
            await handle_search(update, context, action)
        case "entry":
            entries = get_entries(action.url)
            entry = entries[int(action.value)]
            keyboard = []
            images = {}
            for link in entry.links:
                match link.type:
                    case "application/atom+xml" | "application/atom+xml;profile=opds-catalog":
                        action = Action(action_type="page", url=link.href, value="0")
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
                    case "image/jpeg":
                        images[link.rel] = link.href
                    case _:
                        pass
            image = images.get("x-stanza-cover-image") \
                    or images.get('http://opds-spec.org/image') \
                    or images.get("http://opds-spec.org/image/thumbnail") \
                    or images.get("x-stanza-cover-image-thumbnail")

            reply_markup = InlineKeyboardMarkup(keyboard)
            authors = ", ".join(author for author in entry.authors) if entry.authors else None
            text = f"{entry.text}\n{f'{authors}' if authors else ''}\n\n{entry.summary or ''}"
            if image:
                if len(text) < 1024:
                    try:
                        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image, caption=text,
                                                     reply_markup=reply_markup)
                    except BadRequest:
                        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image)
                        max_length = 4096
                        messages = [text[i:i + max_length] for i in range(0, len(text), max_length)]
                        for text in messages[:-1]:
                            await context.bot.send_message(chat_id=update.effective_chat.id, text=text or "...")
                        await context.bot.send_message(chat_id=update.effective_chat.id, text=messages[-1] or "...",
                                                       reply_markup=reply_markup)
                else:
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image)
                    max_length = 4096
                    messages = [text[i:i + max_length] for i in range(0, len(text), max_length)]
                    for text in messages[:-1]:
                        await context.bot.send_message(chat_id=update.effective_chat.id, text=text or "...")
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=messages[-1] or "...",
                                                   reply_markup=reply_markup)
            else:
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=text or "...",
                        reply_markup=reply_markup
                    )
                except BadRequest:
                    max_length = 4096
                    messages = [text[i:i + max_length] for i in range(0, len(text), max_length)]
                    for text in messages[:-1]:
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=text or "..."
                        )
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=messages[-1] or "...",
                        reply_markup=reply_markup
                    )
        case "page":
            page_url = await get_page_url(action.url, int(action.value))
            entries = get_entries(page_url)
            keyboard = []
            for i, entry in enumerate(entries):
                entry_action = Action(action_type="entry", url=page_url, value=str(i))
                action_repository.add(entry_action)
                keyboard.append([InlineKeyboardButton(entry.text, callback_data=hash(entry_action))])
            control_keyboard = []
            if int(action.value) > 0:
                prev_page_action = Action(action_type="page", url=action.url, value=str(int(action.value) - 1))
                action_repository.add(prev_page_action)
                control_keyboard.append(InlineKeyboardButton("Назад", callback_data=hash(prev_page_action)))
            if entries:
                next_page_action = Action(action_type="page", url=action.url, value=str(int(action.value) + 1))
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
            file_resp = requests.get(action.url, cookies=get_cookies() or None)
            file_resp.raise_for_status()
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=io.BytesIO(file_resp.content),
                filename=action.value or "book"
            )
    await update.callback_query.answer()


async def handle_start(update: Update, context):
    entries = get_entries("http://flibusta.is/opds")
    keyboard = []
    for i, entry in enumerate(entries):
        action = Action(action_type="entry", url="http://flibusta.is/opds", value=str(i))
        action_repository.add(action)
        keyboard.append([InlineKeyboardButton(entry.text, callback_data=hash(action))])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Выберите действие или отправьте текст для поиска по авторам и книгам",
        reply_markup=reply_markup
    )


def main():
    logger.info("Starting bot")
    app = ApplicationBuilder().token(os.environ.get("TOKEN")).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
