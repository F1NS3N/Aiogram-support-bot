import os
import re
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
from aiogram import Bot, F, Router
from aiogram.dispatcher.filters import Command
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, Chat
from dotenv import load_dotenv

from filter_media import SupportedMediaFilter

load_dotenv()
GROUP_ID = os.getenv("GROUP_ID")
GROUP_TYPE = os.getenv('GROUP_TYPE', default='group')

router = Router()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Хранилище для замученных пользователей: {user_id: (время_размута, причина)}
muted_users: Dict[int, Tuple[datetime, str]] = {}

# Хранилище для забаненных пользователей: {user_id: причина}
banned_users: Dict[int, str] = {}

# Фоновая задача для проверки истечения мутов
async def check_mute_expirations(bot: Bot):
    """Фоновая задача для автоматического размута пользователей"""
    while True:
        try:
            current_time = datetime.now()
            expired_users = []
            
            # Проверяем, чьи муты истекли
            for user_id, (unmute_time, reason) in muted_users.items():
                if current_time >= unmute_time:
                    expired_users.append(user_id)
            
            # Размучиваем пользователей
            for user_id in expired_users:
                del muted_users[user_id]
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text="✅ С вас автоматически сняты ограничения. Вы можете снова писать в бота."
                    )
                    logger.info(f"User {user_id} automatically unmuted")
                except Exception as e:
                    logger.warning(f"Failed to notify user {user_id} about unmute: {e}")
            
            await asyncio.sleep(60)  # Проверяем каждую минуту
        except Exception as e:
            logger.error(f"Error in mute expiration checker: {e}")
            await asyncio.sleep(60)

def extract_user_id(message: Message) -> int:
    """Извлекает ID пользователя из сообщения"""
    text_to_search = ""
    if message.text:
        text_to_search = message.text
    elif message.caption:
        text_to_search = message.caption
    
    if text_to_search:
        match = re.search(r'tg://user\?id=(\d+)', text_to_search)
        if match:
            return int(match.group(1))
    raise ValueError("Не могу извлечь Id")

def get_name(chat: Chat) -> str:
    """Получает полное имя пользователя"""
    if not chat.first_name:
        return ""
    if not chat.last_name:
        return chat.first_name
    return f"{chat.first_name} {chat.last_name}"

def parse_duration(duration_str: str) -> Tuple[int, str]:
    """
    Парсит строку с длительностью мута
    Например: "1ч", "30м", "2ч30м"
    Возвращает: (минуты, строковое представление)
    """
    if not duration_str:
        return 60, "1 час"  # по умолчанию 1 час
    
    total_minutes = 0
    time_parts = []
    
    # Ищем часы
    hours_match = re.search(r'(\d+)ч', duration_str)
    if hours_match:
        hours = int(hours_match.group(1))
        total_minutes += hours * 60
        time_parts.append(f"{hours}ч")
    
    # Ищем минуты
    minutes_match = re.search(r'(\d+)м', duration_str)
    if minutes_match:
        minutes = int(minutes_match.group(1))
        total_minutes += minutes
        time_parts.append(f"{minutes}м")
    
    if total_minutes == 0:
        # Если не найдено, используем значение как часы
        try:
            hours = int(duration_str)
            total_minutes = hours * 60
            time_parts.append(f"{hours}ч")
        except ValueError:
            total_minutes = 60  # по умолчанию 1 час
            time_parts.append("1ч")
    
    # Ограничиваем максимальное время (24 часа = 1440 минут)
    total_minutes = min(total_minutes, 1440)
    
    return total_minutes, ' '.join(time_parts)

def is_user_muted(user_id: int) -> bool:
    """Проверяет, замучен ли пользователь"""
    if user_id in muted_users:
        unmute_time, _ = muted_users[user_id]
        if datetime.now() < unmute_time:
            return True
        else:
            # Мут истёк, удаляем из списка
            del muted_users[user_id]
            return False
    return False

def is_user_banned(user_id: int) -> bool:
    """Проверяет, забанен ли пользователь"""
    return user_id in banned_users

@router.message(Command(commands=["start"]))
async def command_start(message: Message) -> None:
    """Обработчик команды /start"""
    # Проверяем, забанен ли пользователь
    if is_user_banned(message.from_user.id):
        reason = banned_users[message.from_user.id]
        await message.answer(
            f"❌ Вы были заблокированы администратором.\n"
            f"Причина: {reason}\n"
            f"Ваши сообщения не будут обработаны."
        )
        return
    
    # Проверяем, замучен ли пользователь
    if is_user_muted(message.from_user.id):
        unmute_time, reason = muted_users[message.from_user.id]
        remaining = unmute_time - datetime.now()
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{hours}ч {minutes}мин" if hours > 0 else f"{minutes}мин"
        
        await message.answer(
            f"❌ Вы были замучены администратором.\n"
            f"Причина: {reason}\n"
            f"Оставшееся время: {time_str}\n"
            f"Ваши сообщения не будут обработаны до размута."
        )
        return
    
    await message.answer(
        "Привет! Мы - команда поддержки. Если у вас есть вопрос, "
        "напишите нам, мы с радостью на него ответим.",
    )

@router.message(F.chat.type == 'private', F.text)
async def send_message_to_group(message: Message, bot: Bot):
    """Пересылает текстовые сообщения от пользователя в группу"""
    # Проверяем, забанен ли пользователь
    if is_user_banned(message.from_user.id):
        reason = banned_users[message.from_user.id]
        await message.reply(
            f"❌ Вы были заблокированы администратором.\n"
            f"Причина: {reason}\n"
            f"Ваши сообщения не будут обработаны."
        )
        return
    
    # Проверяем, замучен ли пользователь
    if is_user_muted(message.from_user.id):
        unmute_time, reason = muted_users[message.from_user.id]
        remaining = unmute_time - datetime.now()
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{hours}ч {minutes}мин" if hours > 0 else f"{minutes}мин"
        
        await message.reply(
            f"❌ Вы были замучены администратором.\n"
            f"Причина: {reason}\n"
            f"Оставшееся время: {time_str}\n"
            f"Ваши сообщения не будут обработаны до размута."
        )
        return
    
    if len(message.text) > 4000:
        return await message.reply(text='Сообщение слишком длинное (максимум 4000 символов)')
    
    await bot.send_message(
        chat_id=GROUP_ID,
        text=(
            f'Имя: {message.from_user.full_name}\n'
            f'Профиль: tg://user?id={message.from_user.id}\n\n'
            f'{message.text}'
        ),
        parse_mode='HTML'
    )
    logger.info(f"User {message.from_user.id} sent message to group")

@router.message(Command(commands="info"),
                F.chat.type == GROUP_TYPE,
                F.reply_to_message)
async def get_user_info(message: Message, bot: Bot):
    """Получает информацию о пользователе по команде /info"""
    try:
        user_id = extract_user_id(message.reply_to_message)
    except ValueError as err:
        return await message.reply(str(err))

    try:
        user = await bot.get_chat(user_id)
    except TelegramAPIError as err:
        await message.reply(
            text=(f'Невозможно найти пользователя с таким Id. Текст ошибки:\n'
                  f'{err.message}')
        )
        return

    username = f"@{user.username}" if user.username else "отсутствует"
    
    # Проверяем статус пользователя
    status_info = "Нет"
    if user_id in banned_users:
        status_info = f"Забанен: {banned_users[user_id]}"
    elif user_id in muted_users:
        unmute_time, reason = muted_users[user_id]
        if datetime.now() < unmute_time:
            remaining = unmute_time - datetime.now()
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            time_str = f"{hours}ч {minutes}мин" if hours > 0 else f"{minutes}мин"
            status_info = f"Замучен (до {unmute_time.strftime('%d.%m.%Y %H:%M')}, осталось {time_str})"
        else:
            # Мут истёк, удаляем
            del muted_users[user_id]
    
    await message.reply(text=f'Имя: {get_name(user)}\n'
                             f'Id: {user.id}\n'
                             f'username: {username}\n'
                             f'Статус: {status_info}')
    logger.info(f"Admin {message.from_user.id} requested info for user {user_id}")

@router.message(F.chat.type == GROUP_TYPE, F.reply_to_message, ~F.text.startswith('/'))
async def send_message_answer(message: Message, bot: Bot):
    """Пересылает ответ администратора пользователю"""
    try:
        chat_id = extract_user_id(message.reply_to_message)
        await message.copy_to(chat_id)
        logger.info(f"Admin {message.from_user.id} sent answer to user {chat_id}")
    except ValueError as err:
        await message.reply(text=f'Не могу извлечь Id. Возможно он некорректный. Текст ошибки:\n{str(err)}')
    except TelegramAPIError as err:
        await message.reply(text=f'Ошибка при отправке сообщения пользователю: {err.message}')

@router.message(
    F.chat.type.in_({'group', 'supergroup'}),
    F.reply_to_message,
    F.text.startswith('/')
)
async def handle_admin_commands(message: Message, bot: Bot):
    """Обработчик команд администратора"""
    try:
        user_id = extract_user_id(message.reply_to_message)
    except ValueError as err:
        await message.reply(f"Ошибка: {err}")
        return

    command_parts = message.text.strip().split(maxsplit=1)
    command = command_parts[0].lower()
    logger.info(f"Admin {message.from_user.id} executed command {command} on user {user_id}")

    if command == '/ban':
        try:
            # Парсим причину бана
            reason = "Заблокирован администратором"
            if len(command_parts) > 1:
                reason = command_parts[1].strip()
            
            # Добавляем в список забаненных
            banned_users[user_id] = reason
            
            # Удаляем из списка замученных, если был замучен
            if user_id in muted_users:
                del muted_users[user_id]
            
            await bot.send_message(
                chat_id=user_id,
                text=f"⚠️ Вы были заблокированы администратором.\nПричина: {reason}"
            )
            await message.reply("✅ Пользователь заблокирован.")
        except TelegramAPIError as e:
            await message.reply(f"❌ Ошибка при блокировке: {e.message}")

    elif command == '/mute':
        try:
            # Проверяем, не забанен ли пользователь
            if user_id in banned_users:
                await message.reply("❌ Пользователь забанен. Сначала разбаньте его.")
                return
            
            # Парсим время мута (по умолчанию 1 час)
            reason = "Нарушение правил"
            
            if len(command_parts) > 1:
                args = command_parts[1].strip().split(maxsplit=1)
                duration_str = args[0]
                if len(args) > 1:
                    reason = args[1]
            else:
                duration_str = "1ч"
            
            # Парсим длительность
            total_minutes, time_display = parse_duration(duration_str)
            
            if total_minutes <= 0:
                total_minutes = 60  # по умолчанию 1 час
                time_display = "1ч"
            
            # Добавляем в список замученных
            unmute_time = datetime.now() + timedelta(minutes=total_minutes)
            muted_users[user_id] = (unmute_time, reason)
            
            # Форматируем время окончания
            end_time_str = unmute_time.strftime('%d.%m.%Y %H:%M')
            
            await bot.send_message(
                chat_id=user_id,
                text=f"🔇 Вы были замучены администратором на {time_display}.\n"
                     f"Причина: {reason}\n"
                     f"Ваши сообщения больше не будут обработаны до {end_time_str}."
            )
            await message.reply(f"✅ Пользователь замучен на {time_display}.")
        except TelegramAPIError as e:
            await message.reply(f"❌ Ошибка при муте: {e.message}")

    elif command == '/unmute':
        try:
            # Убираем из списка замученных
            if user_id in muted_users:
                del muted_users[user_id]
                await bot.send_message(
                    chat_id=user_id,
                    text="✅ С вас сняты ограничения. Вы можете снова писать в бота."
                )
                await message.reply("✅ С пользователя сняты ограничения.")
            else:
                await message.reply("❌ Пользователь не замучен.")
        except TelegramAPIError as e:
            await message.reply(f"❌ Ошибка при размуте: {e.message}")

    elif command == '/unban':
        try:
            # Убираем из списка забаненных
            if user_id in banned_users:
                del banned_users[user_id]
                await bot.send_message(
                    chat_id=user_id,
                    text="✅ Вы были разблокированы администратором."
                )
                await message.reply("✅ Пользователь разблокирован.")
            else:
                await message.reply("❌ Пользователь не забанен.")
        except TelegramAPIError as e:
            await message.reply(f"❌ Ошибка при разблокировке: {e.message}")

    else:
        await message.reply("❓ Неизвестная команда. Доступные команды:\n"
                           "/ban [причина] - заблокировать пользователя\n"
                           "/mute [время] [причина] - замутить пользователя (пример: 1ч, 30м, 1ч30м)\n"
                           "/unmute - размутить пользователя\n"
                           "/unban - разблокировать пользователя")

@router.message(SupportedMediaFilter(), F.chat.type == 'private')
async def supported_media(message: Message, bot: Bot):
    """Обработка медиафайлов от пользователя"""
    # Проверяем, забанен ли пользователь
    if is_user_banned(message.from_user.id):
        reason = banned_users[message.from_user.id]
        await message.reply(
            f"❌ Вы были заблокированы администратором.\n"
            f"Причина: {reason}\n"
            f"Ваши сообщения не будут обработаны."
        )
        return
    
    # Проверяем, замучен ли пользователь
    if is_user_muted(message.from_user.id):
        unmute_time, reason = muted_users[message.from_user.id]
        remaining = unmute_time - datetime.now()
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{hours}ч {minutes}мин" if hours > 0 else f"{minutes}мин"
        
        await message.reply(
            f"❌ Вы были замучены администратором.\n"
            f"Причина: {reason}\n"
            f"Оставшееся время: {time_str}\n"
            f"Ваши сообщения не будут обработаны до размута."
        )
        return
        
    if message.caption and len(message.caption) > 1000:
        return await message.reply(text='Слишком длинное описание. Описание не может быть больше 1000 символов')
    
    try:
        await message.copy_to(
            chat_id=GROUP_ID,
            caption=((message.caption or "") +
                     f"\n\nИмя: {message.from_user.full_name}\ntg://user?id={message.from_user.id}"),
            parse_mode="HTML"
        )
        logger.info(f"User {message.from_user.id} sent media to group")
    except TelegramAPIError as e:
        await message.reply(f"❌ Ошибка при отправке медиа: {e.message}")