from openai import OpenAI
import os

from adapters.ai.base import BaseAiAdapter


class OpenAiAdapter(BaseAiAdapter):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OpenAI API key is not specified and not found in environment variables")
            
        self.model = model
        self.client = OpenAI(api_key=self.api_key)

    def get_similar_books(self, authors: str, book_name: str, summary: str) -> list[str]:
        system_prompt = f"""
                Ты — профессиональный литературный критик и эксперт по книгам. Пользователь предоставляет тебе:

                - автора
                - название книги
                - краткий синопсис

                На основе этих данных:

                1. Укажи, есть ли у книги продолжения или другие части в серии, если они существуют.
                2. Подбери 3–5 книг, максимально похожих по следующим критериям:
                - стиль написания (ирония, философия, лёгкий слог и т.п.)
                - ключевые темы (например: искусственный интеллект, одиночество, колонизация космоса, взросление)
                - темп и формат повествования (например: от первого лица, внутренние монологи, приключенческий ритм)
                - характер главного героя (инженер, гик, одиночка, неохотный герой и т.д.)

                3. Для каждой рекомендуемой книги:
                - Укажи автора и название
                - В 1–2 предложениях расскажи, о чём она
                - Объясни, почему ты её рекомендуешь в данном случае

                Ответ пиши с ясностью и уважением к читателю. Избегай банальных списков. Формат — читаемый, в стиле краткой заметки или литературного совета.
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