from abc import ABC, abstractmethod


class BaseAiAdapter(ABC):
    @abstractmethod
    def get_similar_books(self, author: str, book_name: str, summary: str) -> list[str]:
        pass
