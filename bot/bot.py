import os
import logging
import traceback
import html
import json
import tempfile
import pydub
from pathlib import Path
from datetime import datetime
from yoomoney import Quickpay
import asyncio
import telegram
from telegram import Update, User, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
from telegram.constants import ParseMode, ChatAction

import config
import database
import openai_utils


# setup
db = database.Database()
logger = logging.getLogger(__name__)

HELP_MESSAGE = """Команды:
⚪ /retry – Повторить последний ответ бота
⚪ /new – Начать новый диалог
⚪ /mode – Выбор вида собеседника
⚪ /balance – Показать баланс
⚪ /help – Помощь
"""


def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name= user.last_name
        )
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)


async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)
    
    reply_text = "Привет! Я <b>ChatGPT</b> бот\n\n"
    reply_text += HELP_MESSAGE

    reply_text += "\nСпрашивай меня о чём угодно!"
    
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def retry_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("No message to retry 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context

    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    # check if message is edited
    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return
    user_id = update.message.from_user.id
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_channel_status = await context.bot.get_chat_member(chat_id='@AllNewsAI', user_id=user_id)
    if user_channel_status["status"] != 'left':
        is_subscribe = db.get_user_attribute(user_id, "is_subscribe")

        chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
        subscribe_date = db.get_user_attribute(user_id, "subscribe_date")
        last_update_tokens = db.get_user_attribute(user_id, "last_update_tokens")
        delta = datetime.now() - last_update_tokens
        if delta.days >= 1:
            db.set_user_attribute(user_id, "last_update_tokens", datetime.now())
            db.set_user_attribute(user_id, "n_used_tokens", 0)
        avaible_tokens = db.get_user_attribute(user_id, "n_used_tokens")
        if avaible_tokens < 5000 or (is_subscribe and datetime.timestamp(datetime.now()) - datetime.timestamp(subscribe_date) < 0):
            # new dialog timeout
            if use_new_dialog_timeout:
                if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
                    db.start_new_dialog(user_id)
                    await update.message.reply_text(f"Начинаю новый диалог из-за таймаута (<b>{openai_utils.CHAT_MODES[chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)
            db.set_user_attribute(user_id, "last_interaction", datetime.now())

            # send typing action
            await update.message.chat.send_action(action="typing")

            try:
                message = message or update.message.text

                dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
                chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

                chatgpt_instance = openai_utils.ChatGPT(use_chatgpt_api=config.use_chatgpt_api)
                answer, n_used_tokens, n_first_dialog_messages_removed = await chatgpt_instance.send_message(
                    message,
                    dialog_messages=dialog_messages,
                    chat_mode=chat_mode
                )

                # update user data
                new_dialog_message = {"user": message, "bot": answer, "date": datetime.now()}
                db.set_dialog_messages(
                    user_id,
                    db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
                    dialog_id=None
                )

                db.set_user_attribute(user_id, "n_used_tokens", n_used_tokens + db.get_user_attribute(user_id, "n_used_tokens"))

            except Exception as e:
                error_text = f"Произошла ошибка. Причина: {e}"
                logger.error(error_text)
                await update.message.reply_text(error_text)
                return

            # send message if some messages were removed from the context
            if n_first_dialog_messages_removed > 0:
                if n_first_dialog_messages_removed == 1:
                    text = "✍️ <i>Пометка:</i> Твой текущий диалог с ботм слишком длинный, твоё <b>первое сообщение</b> удалено.\n Отправь команду /new чтобы начать новый диалог"
                else:
                    text = f"✍️ <i>Пометка:</i> Твой текущий диалог с ботм слишком длинный, <b>{n_first_dialog_messages_removed} первое сообщение</b> было удалено из чата.\n Отправь команду /new чтобы начать новый диалог"
                await update.message.reply_text(text, parse_mode=ParseMode.HTML)

            # split answer into multiple messages due to 4096 character limit
            for answer_chunk in split_text_into_chunks(answer, 4000):
                try:
                    parse_mode = {
                        "html": ParseMode.HTML,
                        "markdown": ParseMode.MARKDOWN
                    }[openai_utils.CHAT_MODES[chat_mode]["parse_mode"]]

                    await update.message.reply_text(answer_chunk, parse_mode=parse_mode)
                except telegram.error.BadRequest:
                    # answer has invalid characters, so we send it without parse_mode
                    await update.message.reply_text(answer_chunk)
        else:
            keyboard = []
            keyboard.append([InlineKeyboardButton('Купить подписку❤', callback_data=f"buy_subscribe")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text('🔴У тебя не осталось токенов :(\n Ты можешь купить подписку за 249 рублей, либо дождаться следующего дня.', reply_markup=reply_markup)

    else:
        keyboard = []
        keyboard.append([InlineKeyboardButton('Подписаться❤', url='https://t.me/AllNewsAI')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(user_id, 'Вы не подписаны на наш канал. Чтобы начать работу с ботом, подпишись!', reply_markup=reply_markup)   

async def voice_message_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    is_subscribe = db.get_user_attribute(user_id, "is_subscribe")

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    subscribe_date = db.get_user_attribute(user_id, "subscribe_date")
    last_update_tokens = db.get_user_attribute(user_id, "last_update_tokens")
    delta = datetime.now() - last_update_tokens
    if delta.days >= 1:
        db.set_user_attribute(user_id, "last_update_tokens", datetime.now())
        db.set_user_attribute(user_id, "n_used_tokens", 0)
    avaible_tokens = db.get_user_attribute(user_id, "n_used_tokens")
    if avaible_tokens < 5000 or (is_subscribe and datetime.timestamp(datetime.now()) - datetime.timestamp(subscribe_date) < 0):
        voice = update.message.voice
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir = Path(tmp_dir)
            voice_ogg_path = tmp_dir / "voice.ogg"

            # download
            voice_file = await context.bot.get_file(voice.file_id)
            await voice_file.download_to_drive(voice_ogg_path)

            # convert to mp3
            voice_mp3_path = tmp_dir / "voice.mp3"
            pydub.AudioSegment.from_file(voice_ogg_path).export(voice_mp3_path, format="mp3")

            # transcribe
            with open(voice_mp3_path, "rb") as f:
                transcribed_text = await openai_utils.transcribe_audio(f)

        text = f"🎤: <i>{transcribed_text}</i>"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

        await message_handle(update, context, message=transcribed_text)

        # calculate spent dollars
        n_spent_dollars = voice.duration * (config.whisper_price_per_1_min / 60)

        # normalize dollars to tokens (it's very convenient to measure everything in a single unit)
        price_per_1000_tokens = config.chatgpt_price_per_1000_tokens if config.use_chatgpt_api else config.gpt_price_per_1000_tokens
        n_used_tokens = int(n_spent_dollars / (price_per_1000_tokens / 1000))
        db.set_user_attribute(user_id, "n_used_tokens", n_used_tokens + db.get_user_attribute(user_id, "n_used_tokens"))
    else:
        keyboard = []
        keyboard.append([InlineKeyboardButton('Купить подписку❤', callback_data=f"buy_subscribe")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('🔴У тебя не осталось токенов :(\n Ты можешь купить подписку за 249 рублей, либо дождаться следующего дня.', reply_markup=reply_markup)


async def new_dialog_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    db.start_new_dialog(user_id)
    await update.message.reply_text("Начинаю новый диалог ✅")

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    await update.message.reply_text(f"{openai_utils.CHAT_MODES[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    keyboard = []
    for chat_mode, chat_mode_dict in openai_utils.CHAT_MODES.items():
        keyboard.append([InlineKeyboardButton(chat_mode_dict["name"], callback_data=f"set_chat_mode|{chat_mode}")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Выберите собеседника:", reply_markup=reply_markup)


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    chat_mode = query.data.split("|")[1]

    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    await query.edit_message_text(
        f"<b>{openai_utils.CHAT_MODES[chat_mode]['name']}</b> собеседник выбран",
        parse_mode=ParseMode.HTML
    )

    await query.edit_message_text(f"{openai_utils.CHAT_MODES[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def show_balance_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    reply_markup = False
    n_used_tokens = db.get_user_attribute(user_id, "n_used_tokens")
    is_subscribe = db.get_user_attribute(user_id, "is_subscribe")
    subscribe_date = db.get_user_attribute(user_id, "subscribe_date")
    if (is_subscribe and datetime.timestamp(datetime.now()) - datetime.timestamp(subscribe_date) < 0):
        subscribe_date = db.get_user_attribute(user_id, "subscribe_date")
        text = f"Поздравляю, у тебя активирована подписка!\nОна действует до {subscribe_date}"
    else:
        keyboard = []
        keyboard.append([InlineKeyboardButton('Купить подписку❤', callback_data=f"buy_subscribe")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = f"Ты потратил {n_used_tokens}\nТы можешь использовать ещё {5000 - int(n_used_tokens)}\nЛибо купить месячную подписку за 249 рублей для безлимитного доступа к боту."
    if reply_markup:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def edited_message_handle(update: Update, context: CallbackContext):
    text = "🥲 К сожалению, изменение <b>сообщений</b> не поддерживается"
    await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # split text into multiple messages due to 4096 character limit
        for message_chunk in split_text_into_chunks(message, 4000):
            try:
                await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
            except telegram.error.BadRequest:
                # answer has invalid characters, so we send it without parse_mode
                await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")

async def buy_tokens(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()
    reply_markup = None
    n_used_tokens = db.get_user_attribute(user_id, "n_used_tokens")
    is_subscribe = db.get_user_attribute(user_id, "is_subscribe")
    subscribe_date = db.get_user_attribute(user_id, "subscribe_date")
    if (is_subscribe and datetime.timestamp(datetime.now()) - datetime.timestamp(subscribe_date) < 0):
        subscribe_date = db.get_user_attribute(user_id, "subscribe_date")
        text = f"Поздравляю, у тебя активирована подписка!\nОна действует до {subscribe_date}"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)

    else:
        import uuid
        label = uuid.uuid4()
        from yoomoney import Client, Quickpay
        token = "4100110907708107.31EE93047D8B24E8DF1955059192812E151E6DB4244478975BE312BD0014788E0807C25975BF0F34FF52D0912D8B23B813F9FA782739351684DA6DD704878C807C3741D9F43610BD027026CA3684AFB672EE08606BA2FA171BF7475320F74C72ED1BC3323B1DB669FAC4552F4990291CC18C8E3CB5FDD9355AB9244E5B29E2BB"
        client = Client(token)

        quickpay = Quickpay(
            receiver="4100110907708107",
            quickpay_form="shop",
            targets="Подписка GPT-Bot",
            paymentType="SB",
            sum=2,
            label=label
        )
        text = f"Стоимость подписки 249 рублей. Нажми на кнопочку, чтобы приобрести её. Ссылка на оплату действует 10 минут."
        keyboard = []
        keyboard.append([InlineKeyboardButton('Купить подписку❤', url=quickpay.base_url)])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        status = None
        c = 0
        while c < 720 and status != 'success':
            try:
                history = client.operation_history(label=label)
                if history.operations:
                    if history.operations[0].status == 'success':
                        status = 'success'

                        await query.edit_message_text('Поздравляю с приобретённой подпиской!', parse_mode=ParseMode.HTML)
                        db.set_user_attribute(user_id, "is_subscribe", True)
                        from datetime import timedelta
                        db.set_user_attribute(user_id, "subscribe_date", datetime.now() + timedelta(weeks=4))

                        
                        
            except Exception as e:
                print(e)
            await asyncio.sleep(5)
            c+=1



def run_bot() -> None:
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .build()
    )

    # add handlers
    if len(config.allowed_telegram_usernames) == 0:
        user_filter = filters.ALL
    else:
        user_filter = filters.User(username=config.allowed_telegram_usernames)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))
    
    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))
    application.add_handler(CallbackQueryHandler(buy_tokens, pattern="^buy_subscribe"))

    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))
    
    application.add_error_handler(error_handle)
    
    # start the bot
    application.run_polling()


if __name__ == "__main__":
    run_bot()