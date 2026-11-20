import asyncio
import html
import logging
import re
import uuid
from collections import OrderedDict
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)

# Вставьте сюда токен от @BotFather
TOKEN = "9054045234:AAGRXS1KsFt4F1sGrk5l_EjHA6_NIluCNTQ"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Регулярное выражение: пропускает только ссылки на подарки Telegram
GIFT_RE = re.compile(r"^https://t\.me/(nft|gift)/[\w-]+$")

# Кэш на 10 000 последних офферов (защита от двойного клика без утечки памяти)
processed_offers: OrderedDict[str, bool] = OrderedDict()
MAX_CACHE = 10_000


def mark_processed(uid: str):
    processed_offers[uid] = True
    if len(processed_offers) > MAX_CACHE:
        processed_offers.popitem(last=False)


def get_offer_kb(sender_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"acc_{sender_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"dec_{sender_id}")
        ]
    ])


def format_offer_text(stars: str, gift_link: str) -> str:
    safe_link = html.escape(gift_link, quote=True)
    return (
        f"🎁 <b>Предложение об обмене</b>\n\n"
        f"Готовы ли вы продать <a href='{safe_link}'>этот подарок</a> за <b>{stars} ⭐️</b>?"
    )


def is_valid_offer(stars: str, link: str) -> bool:
    return (
        stars.isdigit()
        and (1 <= int(stars) <= 1_000_000)
        and bool(GIFT_RE.match(link))
    )


# --- ПРИВЕТСТВИЕ В ЛС ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    me = await bot.get_me()
    await message.answer(
        f"👋 <b>Бот для создания офферов готов к работе!</b>\n\n"
        f"Как создать оффер в любом чате:\n"
        f"Напишите <code>@{me.username} 500 https://t.me...</code>\n\n"
        f"<i>Теперь я смогу присылать вам уведомления, когда кто-то ответит на ваше предложение.</i>",
        parse_mode="HTML"
    )


# --- СПОСОБ 1: В ЛЮБОМ ЧАТЕ ЧЕРЕЗ @ИМЯ_БОТА ---
@dp.inline_query()
async def inline_offer_handler(inline_query: InlineQuery):
    parts = inline_query.query.strip().split(maxsplit=1)

    if len(parts) < 2 or not is_valid_offer(parts[0], parts[1]):
        me = await bot.get_me()
        hint = InlineQueryResultArticle(
            id="hint",
            title="Создать оффер",
            description="Формат: 500 https://t.me...",
            input_message_content=InputTextMessageContent(
                message_text=f"⚠️ Используйте формат: `@{me.username} 500 https://t.me...`",
                parse_mode="Markdown"
            )
        )
        await inline_query.answer([hint], cache_time=1)
        return

    stars = parts[0]
    gift_link = parts[1]
    sender_id = inline_query.from_user.id

    article = InlineQueryResultArticle(
        id=uuid.uuid4().hex,
        title=f"Отправить оффер за {stars} ⭐️",
        description=gift_link,
        input_message_content=InputTextMessageContent(
            message_text=format_offer_text(stars, gift_link),
            parse_mode="HTML"
        ),
        reply_markup=get_offer_kb(sender_id)
    )
    await inline_query.answer([article], cache_time=1)


# --- СПОСОБ 2: В ГРУППЕ ЧЕРЕЗ /offer ---
@dp.message(Command("offer"))
async def group_offer_handler(message: types.Message):
    parts = (message.text or "").split(maxsplit=2)

    if len(parts) < 3 or not is_valid_offer(parts[1], parts[2]):
        warn = await message.answer("⚠️ Формат: `/offer 500 https://t.me...`", parse_mode="Markdown")
        await asyncio.sleep(5)
        for msg in (warn, message):
            try:
                await msg.delete()
            except Exception:
                pass
        return

    try:
        await message.delete()
    except Exception:
        pass

    stars = parts[1]
    gift_link = parts[2]
    await message.answer(
        text=format_offer_text(stars, gift_link),
        reply_markup=get_offer_kb(message.from_user.id),
        parse_mode="HTML"
    )


# --- ОБРАБОТКА КНОПОК ---
@dp.callback_query(F.data.startswith(("acc_", "dec_")))
async def process_offer_buttons(callback: CallbackQuery):
    action, _, sender_id_str = callback.data.partition("_")
    sender_id = int(sender_id_str)

    # 1. Защита: автор не может нажать кнопку сам за себя
    if callback.from_user.id == sender_id:
        await callback.answer("Вы не можете ответить на свой оффер!", show_alert=True)
        return

    # 2. Безопасное получение ID сообщения
    if callback.inline_message_id:
        msg_uid = callback.inline_message_id
    elif callback.message:
        msg_uid = f"{callback.message.chat.id}_{callback.message.message_id}"
    else:
        await callback.answer("Оффер больше неактуален.", show_alert=True)
        return

    # 3. Защита от двойного нажатия
    if msg_uid in processed_offers:
        await callback.answer("На этот оффер уже ответили!", show_alert=True)
        return
    mark_processed(msg_uid)

    buyer = callback.from_user.mention_html()
    logging.info("Offer %s: %s by %s", msg_uid, action, callback.from_user.id)

    if action == "dec":
        new_text = "❌ <b>Предложение отклонено.</b>"
        alert_text = "Оффер отклонён"
        notify_text = f"❌ Пользователь {buyer} <b>отклонил</b> ваш оффер."
    else:
        new_text = (
            f"✅ <b>Оффер принят пользователем {buyer}!</b>\n\n"
            f"⏳ <i>Ждём отправки подарка...</i>"
        )
        alert_text = "Вы приняли предложение!"
        notify_text = f"🔔 Пользователь {buyer} <b>принял</b> ваш оффер на обмен!"

    # Отправляем уведомление автору оффера в ЛС
    try:
        await bot.send_message(
            chat_id=sender_id,
            text=notify_text,
            parse_mode="HTML"
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass

    # 4. Обновление карточки в чате
    try:
        if callback.inline_message_id:
            await bot.edit_message_text(
                inline_message_id=callback.inline_message_id,
                text=new_text,
                parse_mode="HTML"
            )
        elif callback.message:
            await callback.message.edit_text(
                text=new_text,
                parse_mode="HTML"
            )
    except TelegramBadRequest:
        pass

    await callback.answer(alert_text)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
