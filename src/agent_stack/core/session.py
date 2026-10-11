from langchain_core.messages import BaseMessage


# Base class later replace with real DB for persistence
class SessionStore:
    def __init__(self):
        self.messages = {}

    def get_messages(self, session_id: str):
        return self.messages.get(session_id, [])

    def add_messages(self, session_id: str, messages: list[BaseMessage]):
        self.messages.setdefault(session_id, []).extend(messages)
