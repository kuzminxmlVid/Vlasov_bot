from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


LESSON_TITLES = {
    "acoustic": "Акустика 1",
    "studio": "Студия 1",
    "base": "База 1",
}


def start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Посмотреть видео сейчас", callback_data="menu:open")
    builder.button(text="Подключиться к каналу", callback_data="purchase:open")
    builder.adjust(1)
    return builder.as_markup()


def menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for slug, title in LESSON_TITLES.items():
        builder.button(text=title, callback_data=f"lesson:{slug}")
    builder.button(text="Подключиться к каналу", callback_data="purchase:open")
    builder.adjust(1)
    return builder.as_markup()


def lesson_keyboard(current_slug: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="menu:open")
    for slug, title in LESSON_TITLES.items():
        if slug != current_slug:
            builder.button(text=f"Смотреть {title}", callback_data=f"lesson:{slug}")
    builder.button(text="Подключиться к каналу", callback_data="purchase:open")
    builder.adjust(1)
    return builder.as_markup()


def purchase_keyboard(boosty_url: str, tribute_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оплатить в Telegram через Tribute",
                    url=tribute_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Оформить подписку на Boosty",
                    url=boosty_url,
                )
            ],
            [InlineKeyboardButton(text="Проверить оплату Tribute", callback_data="access:check")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:open")],
        ]
    )


def trial_finished_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Получить полный доступ", callback_data="purchase:open")
    builder.adjust(1)
    return builder.as_markup()
