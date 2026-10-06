"""Telegram control is restricted to the configured owner chat."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiogram.types import Message, CallbackQuery
from tg.handlers import is_owner_event


def message(chat, text):
    return Message.model_construct(chat=SimpleNamespace(id=chat), text=text)


def callback(chat):
    return CallbackQuery.model_construct(message=SimpleNamespace(chat=SimpleNamespace(id=chat)), data="toggle_bot")


class AuthTests(unittest.TestCase):
    def test_owner_allowed_strangers_rejected(self):
        self.assertTrue(is_owner_event(message(42, "/start"), "42"))
        self.assertTrue(is_owner_event(callback(42), "42"))
        self.assertFalse(is_owner_event(message(7, "/start"), "42"))
        self.assertFalse(is_owner_event(callback(7), "42"))

    def test_first_start_claims_unconfigured_bot_only(self):
        self.assertTrue(is_owner_event(message(7, "/start"), ""))
        self.assertTrue(is_owner_event(message(7, "/start@FvgBot"), ""))
        self.assertFalse(is_owner_event(message(7, "/starting"), ""))
        self.assertFalse(is_owner_event(message(7, "/balance 1"), ""))
        self.assertFalse(is_owner_event(callback(7), ""))


if __name__ == "__main__":
    unittest.main()
