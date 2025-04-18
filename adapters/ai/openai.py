from openai import OpenAI
import os

from adapters.ai.base import BaseAiAdapter


class OpenAiAdapter(BaseAiAdapter):
    def __init__(self, api_key: str, model: str = "gpt-4.1-nano"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OpenAI API key is not specified and not found in environment variables")
            
        self.model = model
        self.client = OpenAI(api_key=self.api_key)

    def get_similar_books(self, authors: str, book_name: str, summary: str) -> list[str]:
        system_prompt = f"""
                Ты — эксперт по литературе и книжным рекомендациям. Пользователь указывает авторов, название книги и её синопсис. На основе этих данных подбери 3–5 книг, максимально похожих по духу, жанру, стилю или теме. Учитывай следующие факторы:

                - Стиль написания (ирония, серьёзность, философия и т.д.)
                - Темы (например: ИИ, одиночество, освоение космоса, постапокалипсис, взросление)
                - Жанр (научная фантастика, фэнтези, технотриллер и т.д.)
                - Темп повествования (динамичный, размеренный, напряжённый)
                - Характер главного героя (интеллектуал, циник, герой поневоле и т.п.)

                Выведи результат в виде списка, укажи название книги, автора и 1–2 предложения, объясняющих, почему ты её рекомендуешь.
                Посоветуй похожие книги:
                """
        user_prompt = f"""
                Авторы: {authors}
                Название: {book_name}
                Синопсис: {summary}
                """
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7
            )
            
            # Extracting the answer from the result
            similar_books = response.choices[0].message.content.strip()
            
            return similar_books
            
        except Exception as e:
            # In case of an error, return an error message
            return f"Error while getting similar books: {str(e)}"