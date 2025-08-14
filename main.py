import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List
import os
import random

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.types import ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, PollAnswer
from aiohttp import web

# Настройки
WEBHOOK_PATH = "/webhook"
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = 5000
VERIFICATION_TIMEOUT = 300  # 5 минут на верификацию
MESSAGE_CLEANUP_TIME = 600  # 10 минут хранения сообщений
BAN_NOTIFICATION_TIME = 180  # 3 минуты для уведомления о бане

# Механизмы проверки
HTEST_ENABLED = True
FASTOUT_ENABLED = True

# Формат URL для Replit
REPL_SLUG = os.getenv('REPL_SLUG', 'workspace')
REPL_OWNER = os.getenv('REPL_OWNER', 'user')
# WEBHOOK_URL = f"https://{REPL_SLUG}-{REPL_OWNER}.replit.app{WEBHOOK_PATH}"
# URL для вебхука (мы будем получать его из переменной окружения Railway)
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Класс бота
class VerificationBot:
    def __init__(self, token: str):
        self.bot = Bot(token=token)
        self.dp = Dispatcher()
        self.pending_verifications: Dict[int, Dict] = {}  # user_id -> verification_data
        self.user_messages: Dict[int, List[int]] = {}    # user_id -> [message_ids]
        self.ban_notifications: Dict[int, int] = {}      # chat_id -> message_id
        self.htest_enabled = HTEST_ENABLED
        self.fastout_enabled = FASTOUT_ENABLED
        self.setup_handlers()

    def setup_handlers(self):
        """Настройка обработчиков событий"""
        self.dp.chat_member(ChatMemberUpdatedFilter(
            member_status_changed=(IS_NOT_MEMBER, IS_MEMBER)
        ))(self.handle_new_member)
        self.dp.chat_member(ChatMemberUpdatedFilter(
            member_status_changed=(IS_MEMBER, IS_NOT_MEMBER)
        ))(self.handle_member_left)
        self.dp.poll_answer()(self.handle_poll_answer)
        self.dp.callback_query(lambda c: c.data.startswith("reaction_"))(self.handle_reaction)
        self.dp.message(lambda m: m.chat.type in ["group", "supergroup"])(self.handle_message_from_new_member)
        self.dp.message(Command("htest"))(self.toggle_htest)
        self.dp.message(Command("fastout"))(self.toggle_fastout)
        self.dp.message(Command("status"))(self.show_status)
        self.dp.message(CommandStart())(self.start_command)

    async def start_command(self, message: types.Message):
        """Обработчик команды /start"""
        logger.info(f"Получена команда /start от {message.from_user.id} в чате {message.chat.id}")
        try:
            await message.reply("Привет! Бот работает через вебхук на Replit.")
            logger.info(f"Ответ на /start отправлен в чат {message.chat.id}")
        except Exception as e:
            logger.error(f"Ошибка при отправке ответа на /start в чат {message.chat.id}: {e}")

    async def handle_new_member(self, event: ChatMemberUpdated):
        """Обработка нового участника"""
        if not self.htest_enabled:
            return

        user = event.new_chat_member.user
        chat = event.chat
        logger.info(f"Новый участник {user.id} в чате {chat.id}")

        restricted_permissions = ChatPermissions(
            can_send_messages=False,
            can_send_media_messages=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False
        )

        try:
            await self.bot.restrict_chat_member(chat_id=chat.id, user_id=user.id, permissions=restricted_permissions)
            await self.create_verification_poll(chat.id, user)
        except Exception as e:
            logger.error(f"Ошибка при ограничении прав пользователя {user.id}: {e}")

    async def create_verification_poll(self, chat_id: int, user):
        """Создание опроса для верификации"""
        username = user.username or user.first_name or "Новый участник"
        poll_question = f"Приветствуем тебя, {username}\nОтветь на вопрос или покинь группу"

        poll_options_all = [
            "Я спам-бот и горжусь этим",
            "Отправляю спам 24/7",
            "Спам - это моя профессия",
            "Реклама казино - мое призвание",
            "Продаю крипто-курсы",
            "Млм-маркетолог со стажем",
            "Фейковые новости - мой хлеб",
            "Накрутчик подписчиков",
            "Бот для рассылки рекламы"
        ]
        correct_answer = "Я не спамер"
        wrong_answers = random.sample(poll_options_all, 2)
        poll_options = wrong_answers + [correct_answer]
        random.shuffle(poll_options)
        correct_option_id = poll_options.index(correct_answer)

        try:
            poll_message = await self.bot.send_poll(
                chat_id=chat_id,
                question=poll_question,
                options=poll_options,
                is_anonymous=False,
                allows_multiple_answers=False
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="👍", callback_data=f"reaction_approve_{user.id}"),
                    InlineKeyboardButton(text="👎", callback_data=f"reaction_reject_{user.id}")
                ]
            ])
            await self.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=poll_message.message_id,
                reply_markup=keyboard
            )

            self.pending_verifications[user.id] = {
                "chat_id": chat_id,
                "poll_id": poll_message.poll.id,
                "message_id": poll_message.message_id,
                "correct_option_id": correct_option_id,
                "deadline": datetime.now() + timedelta(seconds=VERIFICATION_TIMEOUT),
                "user": user
            }
            asyncio.create_task(self.verification_timeout(user.id))
            logger.info(f"Создан опрос для пользователя {user.id} в чате {chat_id}")
        except Exception as e:
            logger.error(f"Ошибка при создании опроса для пользователя {user.id}: {e}")

    async def handle_poll_answer(self, poll_answer: PollAnswer):
        """Обработка ответа на опрос"""
        user = poll_answer.user
        if user.id not in self.pending_verifications:
            return

        verification_data = self.pending_verifications[user.id]
        if poll_answer.poll_id != verification_data["poll_id"]:
            return

        selected_option_id = poll_answer.option_ids[0]
        logger.info(f"Пользователь {user.id} ответил на опрос, выбрав опцию {selected_option_id}")

        if selected_option_id == verification_data["correct_option_id"]:
            await self.approve_user(user.id)
        else:
            await self.reject_user(user.id, "Неправильный ответ на опрос")

    async def handle_reaction(self, callback: types.CallbackQuery):
        """Обработка реакций админов"""
        if not await self.is_admin(callback.from_user.id, callback.message.chat.id):
            await callback.answer("Только администраторы могут использовать эти кнопки")
            return

        data_parts = callback.data.split("_")
        action, user_id = data_parts[1], int(data_parts[2])

        if user_id not in self.pending_verifications:
            await callback.answer("Этот пользователь уже прошел верификацию или был исключен.", show_alert=True)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception as e:
                logger.warning(f"Не удалось убрать кнопки у сообщения {callback.message.message_id}: {e}")
            return

        logger.info(f"Админ {callback.from_user.id} выполнил действие {action} для пользователя {user_id}")

        try:
            if action == "approve":
                await self.approve_user(user_id)
                await callback.answer("Пользователь одобрен")
            elif action == "reject":
                await self.reject_user(user_id, "Отклонен администратором")
                await callback.answer("Пользователь отклонен")
        except Exception as e:
            logger.error(f"Ошибка при обработке реакции {action} для пользователя {user_id}: {e}")

    async def approve_user(self, user_id: int):
        """Одобрение пользователя"""
        verification_data = self.pending_verifications.pop(user_id, None)
        if not verification_data:
            return

        chat_id = verification_data["chat_id"]

        default_permissions = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False
        )

        try:
            await self.bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=default_permissions)
            await self.bot.delete_message(chat_id=chat_id, message_id=verification_data["message_id"])
            await self.bot.send_message(
                chat_id=chat_id,
                text=f"✅ {verification_data['user'].first_name} успешно прошел верификацию!",
                disable_notification=True
            )
            logger.info(f"Пользователь {user_id} одобрен в чате {chat_id}")
        except Exception as e:
            logger.error(f"Ошибка при одобрении пользователя {user_id}: {e}")

    async def reject_user(self, user_id: int, reason: str):
        """Отклонение пользователя"""
        verification_data = self.pending_verifications.pop(user_id, None)
        if not verification_data:
            return

        chat_id = verification_data["chat_id"]
        user = verification_data["user"]

        try:
            await self.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await self.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await self.bot.delete_message(chat_id=chat_id, message_id=verification_data["message_id"])
            ban_message = await self.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 {user.first_name} исключен из группы. Причина: {reason}",
                disable_notification=True
            )
            self.ban_notifications[chat_id] = ban_message.message_id
            asyncio.create_task(self.remove_ban_notification(chat_id, ban_message.message_id))
            logger.info(f"Пользователь {user_id} исключен из чата {chat_id}: {reason}")
        except Exception as e:
            logger.error(f"Ошибка при исключении пользователя {user_id}: {e}")

    async def verification_timeout(self, user_id: int):
        """Таймер верификации"""
        await asyncio.sleep(VERIFICATION_TIMEOUT)
        if user_id in self.pending_verifications:
            await self.reject_user(user_id, "Превышено время верификации")

    async def remove_ban_notification(self, chat_id: int, message_id: int):
        """Удаление уведомления о бане"""
        await asyncio.sleep(BAN_NOTIFICATION_TIME)
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
            self.ban_notifications.pop(chat_id, None)
            logger.info(f"Уведомление о бане удалено в чате {chat_id}")
        except Exception as e:
            logger.error(f"Ошибка при удалении уведомления о бане: {e}")

    async def handle_message_from_new_member(self, message: types.Message):
        """Обработка сообщений от участников"""
        if not self.fastout_enabled or not message.from_user:
            return

        user_id = message.from_user.id
        if user_id in self.pending_verifications:
            return

        if user_id not in self.user_messages:
            self.user_messages[user_id] = []
        self.user_messages[user_id].append(message.message_id)
        logger.info(f"Сообщение от пользователя {user_id} в чате {message.chat.id}: {message.text}")

    async def handle_member_left(self, event: ChatMemberUpdated):
        """Обработка выхода участника"""
        if not self.fastout_enabled:
            return

        user_id = event.new_chat_member.user.id
        chat_id = event.chat.id

        if user_id in self.user_messages:
            for message_id in self.user_messages[user_id]:
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
                except Exception as e:
                    logger.error(f"Ошибка при удалении сообщения {message_id}: {e}")
            del self.user_messages[user_id]
            logger.info(f"Сообщения пользователя {user_id} удалены из чата {chat_id}")

    async def toggle_htest(self, message: types.Message):
        """Переключение механизма HTest"""
        await self.toggle_mechanism(message, "htest")

    async def toggle_fastout(self, message: types.Message):
        """Переключение механизма FastOut"""
        await self.toggle_mechanism(message, "fastout")

    async def toggle_mechanism(self, message: types.Message, mechanism: str):
        """Переключение механизмов проверки"""
        try:
            if not await self.is_admin(message.from_user.id, message.chat.id):
                await message.reply("Только администраторы могут управлять настройками бота")
                return

            args = message.text.split()
            if len(args) < 2:
                status = self.htest_enabled if mechanism == "htest" else self.fastout_enabled
                await message.reply(f"Механизм {mechanism}: {'включен' if status else 'выключен'}")
                return

            action = args[1].lower()
            if action not in ["on", "off"]:
                await message.reply(f"Используйте: /{mechanism} on|off")
                return

            new_state = (action == "on")
            if mechanism == "htest":
                self.htest_enabled = new_state
            else:
                self.fastout_enabled = new_state

            await message.reply(f"Механизм {mechanism} {'включен' if new_state else 'выключен'}")
            logger.info(f"Механизм {mechanism} переключен на {action} пользователем {message.from_user.id}")

        except Exception as e:
            logger.error(f"Ошибка при обработке команды /{mechanism} от {message.from_user.id}: {e}")

    async def show_status(self, message: types.Message):
        """Показ статуса бота"""
        status_text = f"""
🤖 Статус бота верификации
📊 Механизмы:
• HTest (опросы): {'✅ включен' if self.htest_enabled else '❌ выключен'}
• FastOut (отслеживание): {'✅ включен' if self.fastout_enabled else '❌ выключен'}
⏱ Настройки времени:
• Время верификации: {VERIFICATION_TIMEOUT // 60} мин
• Хранение сообщений: {MESSAGE_CLEANUP_TIME // 60} мин
• Уведомления о бане: {BAN_NOTIFICATION_TIME // 60} мин
👥 Активность:
• Ожидают верификации: {len(self.pending_verifications)}
• Отслеживаемые пользователи: {len(self.user_messages)}
        """
        logger.info(f"Статус бота запрошен пользователем {message.from_user.id} в чате {message.chat.id}")
        try:
            await message.reply(status_text)
            logger.info(f"Статус бота отправлен в чат {message.chat.id}")
        except Exception as e:
            logger.error(f"Ошибка при отправке статуса в чат {message.chat.id}: {e}")

    async def is_admin(self, user_id: int, chat_id: int) -> bool:
        """Проверка прав администратора"""
        try:
            chat_member = await self.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            return chat_member.status in ["creator", "administrator"]
        except Exception as e:
            logger.error(f"Ошибка при проверке прав админа для {user_id} в чате {chat_id}: {e}")
            return False

# Настройка вебхука
async def on_startup(app):
    """Установка вебхука при запуске"""
    try:
        await bot.bot.delete_webhook(drop_pending_updates=True)
        await bot.bot.set_webhook(WEBHOOK_URL, allowed_updates=["message", "callback_query", "poll_answer", "chat_member"])
        logger.info(f"Вебхук установлен: {WEBHOOK_URL}")
        webhook_info = await bot.bot.get_webhook_info()
        logger.info(f"Информация о вебхуке: {webhook_info}")
    except Exception as e:
        logger.error(f"Ошибка установки вебхука: {e}")

async def on_shutdown(app):
    """Очистка при остановке"""
    try:
        await bot.bot.delete_webhook(drop_pending_updates=True)
        await bot.bot.session.close()
        logger.info("Бот остановлен, вебхук удалён")
    except Exception as e:
        logger.error(f"Ошибка при остановке: {e}")

# Обработчик вебхука
async def webhook_handler(request):
    """Обработка входящих запросов вебхука"""
    logger.debug(f"Получен запрос: {request.method} {request.url} from {request.remote}")
    if request.method == "POST":
        try:
            data = await request.json()
            logger.debug(f"Данные вебхука: {data}")
            update = types.Update(**data)
            await bot.dp.feed_update(bot.bot, update)
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"Ошибка при обработке вебхука: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)
    logger.warning(f"Неподдерживаемый метод: {request.method}")
    return web.json_response({"status": "method not allowed"}, status=405)

# Запуск приложения
app = web.Application()
bot = None

async def main():
    """Основная функция запуска"""
    global bot
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не установлен!")
        return

    logger.info(f"REPL_SLUG: {REPL_SLUG}, REPL_OWNER: {REPL_OWNER}, WEBHOOK_URL: {WEBHOOK_URL}")

    bot = VerificationBot(token)
    app.router.add_post(WEBHOOK_PATH, webhook_handler)
    app.router.add_get("/", lambda _: web.Response(text="Бот работает!"))
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    try:
        await site.start()
        logger.info(f"Сервер запущен на {WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
        await asyncio.Event().wait()  # Ждём бесконечно
    except Exception as e:
        logger.error(f"Ошибка сервера: {e}")
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())