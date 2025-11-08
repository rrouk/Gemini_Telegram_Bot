import telebot
import requests
import re
import json
import threading
import time
import os
from collections import deque # Импорт, который вы уже используете в sanitize_html, но стоит убедиться, что он сверху
import logging
# Настройка логирования в начале файла (должна быть)
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s')

# === Настройки ===
# API_TOKEN должен быть обязательным. Если его нет, скрипт завершится.
API_TOKEN = os.environ.get('TG_BOT_TOKEN')
if not API_TOKEN:
    logging.info("Ошибка: Переменная окружения 'TG_BOT_TOKEN' не установлена.")
    exit(1)

MAX_HISTORY_MESSAGES = int(os.environ.get('MAX_HISTORY_MESSAGES', 5))
MAX_MESSAGE_LENGTH = int(os.environ.get('MAX_MESSAGE_LENGTH', 3000))
ADMIN_ID = int(os.environ.get('ADMIN_ID', 6887512338))
Free_Chat = int(os.environ.get('Free_Chat', 1))

allowed_users_str = os.environ.get('ALLOWED_USER_IDS', str(ADMIN_ID))
# Преобразуем строку в список целых чисел (ID)
try:
    # Разделяем строку по запятой, очищаем от пробелов и преобразуем в int
    ALLOWED_USERS = [
        int(user_id.strip()) 
        for user_id in allowed_users_str.split(',') 
        if user_id.strip()
    ]
except ValueError:
    logging.error("Ошибка при разборе 'ALLOWED_USER_IDS': Убедитесь, что все значения - числа.")
    # Если ошибка, используем только ID администратора
    ALLOWED_USERS = [ADMIN_ID]

logging.info(f"Разрешенные пользователи (ID): {ALLOWED_USERS}")

roles_json = "roles_gemini.json"
config_json = "config_gemini.json"
USERS_ID_FILE = 'gemini_user_ids.json'


DEFAULT_ROLE_SUFFIX = "\n\nТы отвечаешь в чате телеграмм, используй стиль форматирования HTML, Используй ТОЛЬКО ПОДДЕРЖИВАЕМЫЕ теги HTML: <b>, <i>, <s>, <code>, <pre>. "

# === URL нового API ===
# Базовый URL для Gemini API. Имя модели и метод будут добавлены позже.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# === Создаем бота ===
bot = telebot.TeleBot(API_TOKEN)
bot_info = bot.get_me()
bot_username = bot_info.username.lower() #получаем юзарнейм бота


# === Хранилища ===
# Структура dialogues изменится для соответствия Gemini API:
# dialogues = {user_id: [{"role": "user/model", "parts": [{"text": "..."}]}, ...]}
dialogues = {}

waiting_for_bot_name = {} # {user_id: chat_id}
waiting_for_role = {} # {user_id: {"chat_id": ..., "name_bot": ...}}
waiting_for_api_key = {} # {user_id: chat_id}

waiting_for_name_ai_neyro = {} # {chat_id: True}
waiting_for_name_bot = {} # {chat_id: True}


# === проверка на админа ===
def admin_only(handler):
    def wrapper(message):
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "У вас нет прав для использования этой команды.")
            return
        return handler(message)
    return wrapper




def save_id_user(user_id):
    """
    Сохраняет ID пользователя в JSON-файл.

    :param user_id: ID пользователя Telegram (int или str).
    """
    user_id_str = str(user_id) # Преобразуем ID в строку для ключей JSON

    # Проверяем, существует ли файл и является ли он корректным JSON
    if os.path.exists(USERS_ID_FILE) and os.path.getsize(USERS_ID_FILE) > 0:
        try:
            with open(USERS_ID_FILE, 'r', encoding='utf-8') as f:
                user_ids_data = json.load(f)
        except json.JSONDecodeError:
            # Если файл поврежден или пуст, начинаем с нового словаря
            user_ids_data = {}
    else:
        user_ids_data = {}

    # Добавляем ID пользователя, если его ещё нет
    if user_id_str not in user_ids_data:
        user_ids_data[user_id_str] = True # Можно хранить просто True или дополнительную информацию

        # Сохраняем обновленные данные обратно в файл
        with open(USERS_ID_FILE, 'w', encoding='utf-8') as f:
            json.dump(user_ids_data, f, ensure_ascii=False, indent=4)
        logging.info(f"Пользователь с ID {user_id_str} добавлен в список.")
    else:
        logging.info(f"Пользователь с ID {user_id_str} уже существует в списке.")




# === очистка ответа нейронки от ненужных символов
def sanitize_html(text):
    # Разрешённые теги
    allowed_tags = r'<(\/?(b|i|s|code|pre))\b[^>]*>'

    # Шаблон для поиска всех тегов
    tag_pattern = re.compile(r'<(/?)(\w+)([^>]*?)>', re.DOTALL)

    # 1. Удаляем все теги, кроме разрешённых
    def keep_allowed(match):
        full_tag = match.group(0)
        if re.match(allowed_tags, full_tag):
            return full_tag
        else:
            return ''  # Удаляем запрещённые теги

    cleaned = tag_pattern.sub(keep_allowed, text)

    # 2. Балансируем теги — делаем так, чтобы все теги были правильно открыты/закрыты
    # from collections import deque # Уже импортировано сверху

    stack = {}
    for tag in ['b', 'i', 's', 'code', 'pre']:
        stack[tag] = deque()

    open_close_pattern = re.compile(r'<(/?)(b|i|s|code|pre)([^>]*)>', re.DOTALL)

    # Список позиций тегов
    tag_positions = []

    def record_tags(match):
        is_close = match.group(1) == '/'
        tag_name = match.group(2)
        full_tag = match.group(0)
        pos = match.start()

        tag_positions.append((pos, tag_name, is_close))
        return full_tag

    # Пробегаем по всем тегам и записываем их в список
    open_close_pattern.sub(record_tags, cleaned)

    # Теперь корректируем порядок тегов
    to_insert = []

    # Стек для открытых тегов
    open_stack = []

    for pos, tag, is_close in tag_positions:
        if not is_close:
            open_stack.append((pos, tag))
        else:
            if open_stack and open_stack[-1][1] == tag:
                open_stack.pop()
            else:
                # Лишний закрывающий тег — помечаем на удаление
                to_insert.append((pos, '', 'remove'))

    # Добавляем недостающие закрывающие теги
    for pos, tag in reversed(open_stack):
        to_insert.append((len(cleaned), f"</{tag}>", 'add'))

    # Сортируем изменения по позиции
    to_insert.sort(key=lambda x: x[0])

    # Применяем изменения к строке
    offset = 0
    for pos, repl, action in to_insert:
        pos += offset
        if action == 'remove':
            # Нужно найти и удалить сам тег, а не просто пустую строку
            # Этот участок кода сложнее, чем кажется, так как pos и offset будут меняться
            # Простейшее решение - пересобрать строку без удаляемых тегов после определения всех изменений
            pass # Пока оставляем, так как focus на API вызове.
        elif action == 'add':
            cleaned = cleaned[:pos] + repl + cleaned[pos:]
            offset += len(repl)

    # Пересобираем строку, чтобы учесть удаления, если они были сложными.
    # Для простоты, если вам нужно удалить лишние закрывающие теги,
    # проще переделать логику так, чтобы они просто не попадали в cleaned изначально,
    # или использовать более robust HTML парсер.
    # В текущей реализации 'remove' не применяется.

    # 3. Экранируем & если они не часть правильного HTML-сущности
    # Оставляем существующие &amp;, но исправляем & → &amp; где нужно
    # (например, если ИИ написал просто "Символ & — это важно")
    cleaned = re.sub(r'&(?!(?:amp|lt|gt|quot|apos);)', '&amp;', cleaned)

    return cleaned.strip()



# === декоратор обращения к боту ===
def is_allowed_message(message):
    chat_type = message.chat.type

    if chat_type == 'private':
        return True # В личке всегда отвечаем

    elif chat_type in ['group', 'supergroup']:
        user_id = str(message.from_user.id)
        text = message.text or ""
        text_lower = text.lower()

        # Получаем данные о боте
        bot_info = bot.get_me()
        bot_username = bot_info.username.lower() # Например: laurabot

        # Подгружаем данные пользователя (его роль и имя бота)
        roles = load_roles()
        user_data = roles.get(user_id, {})
        user_custom_name_bot = user_data.get("name_bot")

        # Если пользователь задал своё имя бота — используем его
        if user_custom_name_bot:
            bot_first_name = user_custom_name_bot.lower()
        else:
            # Иначе используем глобальное имя из конфига
            config = load_config()
            bot_first_name = config.get("NAME_BOT", "Бот").lower()

        # Проверяем упоминание бота или ответ на его сообщение
        mentioned = f'@{bot_username}' in text_lower or bot_first_name in text_lower
        replied_to_bot = message.reply_to_message and message.reply_to_message.from_user.id == bot_info.id

        return mentioned or replied_to_bot

    else:
        return False






# === функция отправки длинного сообщения ===
def send_long_message(bot, chat_id, text, parse_mode=None, message_thread_id=None, delete_after=False):
    kwargs = {"parse_mode": parse_mode}

    # Добавляем thread_id только если он реально нужен
    if isinstance(message_thread_id, int) and message_thread_id > 0:
        kwargs["message_thread_id"] = message_thread_id

    if len(text) <= MAX_MESSAGE_LENGTH:
        sent_msg = bot.send_message(chat_id, text, **kwargs)
        if delete_after:
            threading.Thread(target=delete_message_after_delay, args=(chat_id, sent_msg.message_id, 10)).start()
        return

    parts = []
    while len(text) > 0:
        if len(text) > MAX_MESSAGE_LENGTH:
            part = text[:MAX_MESSAGE_LENGTH]
            last_space = part.rfind(' ')
            if last_space != -1:
                part = part[:last_space]
                text = text[last_space+1:]
            else:
                text = text[MAX_MESSAGE_LENGTH:]
        else:
            part = text
            text = ""
        parts.append(part)

    for part in parts:
        sent_msg = bot.send_message(chat_id, part, **kwargs)
        if delete_after:
            threading.Thread(target=delete_message_after_delay, args=(chat_id, sent_msg.message_id, 10)).start()



# === работа с json ===
def load_config():
    if not os.path.exists(config_json):
        # Если файла нет, создаём с базовой конфигурацией
        default_config = {
            "IO_API_KEY": "XXXX",
            "NAME_BOT": "лаура",
            "NAME_AI_NEYRO": "gemini-pro" # Изменено на более общий "gemini-pro"
        }
        save_config(default_config)
        return default_config

    with open(config_json, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(config):
    with open(config_json, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)









# === Команда: установить NAME_AI_NEYRO ===
@bot.message_handler(commands=['set_name_ai_neyro'])
@admin_only
def cmd_set_name_ai_neyro(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "Введите новое имя ИИ-модели (например: gemini-pro или gemini-1.5-flash-latest):")
    waiting_for_name_ai_neyro[chat_id] = True


# === Команда: установить NAME_BOT ===
@bot.message_handler(commands=['set_name_bot'])
@admin_only
def cmd_set_name_bot(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "Введите новое имя бота (по умолчанию):")
    waiting_for_name_bot[chat_id] = True

# === Обработка ввода имени ИИ и имени бота ===
@bot.message_handler(func=lambda m: m.chat.id in waiting_for_name_ai_neyro or m.chat.id in waiting_for_name_bot)
@admin_only
def handle_admin_inputs(message):
    chat_id = message.chat.id
    text = message.text.strip()

    if chat_id in waiting_for_name_ai_neyro:
        del waiting_for_name_ai_neyro[chat_id]

        config = load_config()
        config["NAME_AI_NEYRO"] = text
        save_config(config)

        bot.reply_to(message, f"Имя ИИ-модели изменено на: `{text}`", parse_mode='Markdown', reply_markup=get_main_keyboard_admin(message.from_user.id))

    elif chat_id in waiting_for_name_bot:
        del waiting_for_name_bot[chat_id]

        config = load_config()
        config["NAME_BOT"] = text
        save_config(config)

        bot.reply_to(message, f"Имя бота по умолчанию изменено на: `{text}`", parse_mode='Markdown', reply_markup=get_main_keyboard_admin(message.from_user.id))






# === Команда: показать NAME_AI_NEYRO ===
@bot.message_handler(commands=['show_name_ai_neyro'])
@admin_only
def cmd_show_name_ai_neyro(message):
    config = load_config()
    name_ai_neyro = config.get("NAME_AI_NEYRO", "Не задано")
    bot.reply_to(message, f"Текущая модель ИИ: `{name_ai_neyro}`", parse_mode='Markdown', reply_markup=get_main_keyboard_admin(message.from_user.id))


# === Команда: показать NAME_BOT ===
@bot.message_handler(commands=['show_name_bot'])
@admin_only
def cmd_show_name_bot(message):
    config = load_config()
    name_bot = config.get("NAME_BOT", "Не задано")
    bot.reply_to(message, f"Текущее имя бота по умолчанию: `{name_bot}`", parse_mode='Markdown', reply_markup=get_main_keyboard_admin(message.from_user.id))









# === Показать\Внести API ключ
@bot.message_handler(commands=['key_show'])
@admin_only
def cmd_key_show(message):
    config = load_config()
    key = config.get("IO_API_KEY", "")

    if key:
        masked_key = key[:300] + "..." if len(key) > 300 else key
        bot.reply_to(message, f"Текущий API-ключ:\n`{masked_key}`", parse_mode='Markdown')
    else:
        bot.reply_to(message, "API-ключ не установлен.")

@bot.message_handler(commands=['key_set'])
@admin_only
def cmd_key_set(message):
    user_id = str(message.from_user.id)
    chat_id = message.chat.id

    waiting_for_api_key[user_id] = chat_id
    bot.send_message(chat_id, "Введите новый IO_API_KEY:")


# === Обработка нового ключа
@bot.message_handler(func=lambda m: str(m.from_user.id) in waiting_for_api_key and m.text)
@admin_only
def handle_new_api_key(message):
    user_id = str(message.from_user.id)
    chat_id = message.chat.id
    new_key = message.text.strip()

    if user_id in waiting_for_api_key:
        del waiting_for_api_key[user_id]

    config = load_config()
    config["IO_API_KEY"] = new_key
    save_config(config)

    bot.reply_to(message, "API-ключ успешно обновлён!", reply_markup=get_main_keyboard_admin(message.from_user.id))





# === Функции работы с ролями ===
def load_roles():
    try:
        with open(roles_json, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_roles(roles):
    with open(roles_json, "w", encoding="utf-8") as f:
        json.dump(roles, f, ensure_ascii=False, indent=4)

def delete_message_after_delay(chat_id, message_id, delay=10):
    time.sleep(delay)
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        pass









# === Команда /role_save ===
@bot.message_handler(commands=['role_save'])
def cmd_role_save(message):
    user_id = str(message.from_user.id)
    chat_id = message.chat.id

    # Переходим к вводу имени бота
    waiting_for_bot_name[user_id] = chat_id

    bot.send_message(
        chat_id,
        "Введите имя, которое будет использовать бот в вашем чате:",
        reply_markup=get_main_keyboard(user_id)
    )

@bot.message_handler(func=lambda m: str(m.from_user.id) in waiting_for_bot_name and m.text)
def handle_bot_name(message):
    user_id = str(message.from_user.id)
    chat_id = message.chat.id
    name_bot = message.text.strip()

    # Убираем пользователя из ожидания имени бота
    if user_id in waiting_for_bot_name:
        del waiting_for_bot_name[user_id]

    # Переводим к вводу роли, сохраняем имя бота временно
    waiting_for_role[user_id] = {
        "chat_id": chat_id,
        "name_bot": name_bot
    }

    bot.send_message(chat_id, f"Хорошо! Теперь введите роль для бота *{name_bot}*", parse_mode='Markdown')

@bot.message_handler(func=lambda m: str(m.from_user.id) in waiting_for_role and m.text)
def handle_new_role(message):
    user_id = str(message.from_user.id)
    chat_id = message.chat.id
    new_role = message.text.strip()

    # Получаем сохранённое имя бота
    name_bot = waiting_for_role[user_id]["name_bot"]

    # Убираем пользователя из ожидания
    if user_id in waiting_for_role:
        del waiting_for_role[user_id]

    # Добавляем суффикс к пользовательской роли
    full_role = new_role + DEFAULT_ROLE_SUFFIX

    # Загружаем текущие роли и сохраняем новую информацию
    roles = load_roles()
    roles[user_id] = {
        "name_bot": name_bot,
        "role": full_role
    }
    save_roles(roles)

    sent_msg = bot.reply_to(
        message,
        f"Роль и имя бота ({name_bot}) успешно сохранены!",
        reply_markup=get_main_keyboard(message.from_user.id)
    )
    threading.Thread(target=delete_message_after_delay, args=(chat_id, sent_msg.message_id, 5)).start()







# === Команда /role_load ===
@bot.message_handler(commands=['role_load'])
def cmd_role_load(message):
    user_id = str(message.from_user.id)
    roles = load_roles()

    if user_id in roles:
        user_data = roles[user_id]
        role = user_data.get("role", "Роль не задана")
        name_bot = user_data.get("name_bot", "Бот")

        bot.reply_to(
            message,
            f"Ваша текущая роль:\n\n{role}\n\nИмя бота для вас: *{name_bot}*",
            parse_mode='Markdown',
            reply_markup=get_main_keyboard(message.from_user.id)
        )
    else:
        bot.reply_to(
            message,
            "У вас ещё нет сохранённой роли или имени бота.",
            reply_markup=get_main_keyboard(message.from_user.id)
        )



@bot.message_handler(commands=['kb'])
def keyboard_create(message):
    user_id = message.from_user.id
    bot.reply_to(message, "Управление", reply_markup=get_main_keyboard(message.from_user.id))



# === Команда /reset для сброса контекста (по желанию) ===
@bot.message_handler(commands=['reset'])
def reset_context(message):
    user_id = message.from_user.id
    if user_id in dialogues:
        del dialogues[user_id]
    bot.reply_to(message, "Контекст успешно сброшен. Начнём с чистого листа!", reply_markup=get_main_keyboard(message.from_user.id))

# === Команда сброса роли и контекста
@bot.message_handler(commands=['role_reset'])
def cmd_role_reset(message):
    user_id = str(message.from_user.id)
    roles = load_roles()
    reset_context(message)

    if user_id in roles:
        del roles[user_id]
        save_roles(roles)
        bot.reply_to(message, "Ваша роль успешно сброшена. Теперь будет использоваться роль по умолчанию.", reply_markup=get_main_keyboard(message.from_user.id))
    else:
        bot.reply_to(message, "У вас и так используется роль по умолчанию.", reply_markup=get_main_keyboard(message.from_user.id))






# === Клавиатура с командами ===
def get_main_keyboard(user_id=None):
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)

    # Основные кнопки
    keyboard.row(
        telebot.types.KeyboardButton("Сохранить роль"),
        telebot.types.KeyboardButton("Показать роль")
    )
    keyboard.row(
        telebot.types.KeyboardButton("Сбросить роль"),
        telebot.types.KeyboardButton("Сбросить историю")
    )

    # Админские кнопки (только для админа)
    if user_id == ADMIN_ID:
        keyboard.row(
            telebot.types.KeyboardButton("Админ"))

    return keyboard


# === Клавиатура с командами админа ===
def get_main_keyboard_admin(user_id=None):
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    if user_id == ADMIN_ID:
        keyboard.row(
            telebot.types.KeyboardButton("Показать ключ API"),
            telebot.types.KeyboardButton("Установить ключ API")
        )
        keyboard.row(
            telebot.types.KeyboardButton("Показать модель ИИ"),
            telebot.types.KeyboardButton("Установить модель ИИ")
        )
        keyboard.row(
            telebot.types.KeyboardButton("Показать имя бота"),
            telebot.types.KeyboardButton("Установить имя бота")
        )
        keyboard.row(
            telebot.types.KeyboardButton("Назад"))


    return keyboard



@bot.message_handler(commands=['kb_admin'])
@admin_only
def keyboard_create_admin(message):
    user_id = message.from_user.id
    bot.reply_to(message, "Админка", reply_markup=get_main_keyboard_admin(message.from_user.id))




# === Словарь: текст кнопки -> команда ===
BUTTON_TO_COMMAND = {
    "Сохранить роль": cmd_role_save,
    "Показать роль": cmd_role_load,
    "Сбросить роль": cmd_role_reset,
    "Сбросить историю": reset_context,
    "Админ": keyboard_create_admin
}

# === Словарь: текст кнопки -> команда (только для админа) ===
ADMIN_BUTTONS = {
    "Показать ключ API": cmd_key_show,
    "Установить ключ API": cmd_key_set,
    "Установить модель ИИ": cmd_set_name_ai_neyro,
    "Установить имя бота": cmd_set_name_bot,
    "Показать модель ИИ": cmd_show_name_ai_neyro,    # ← новая кнопка
    "Показать имя бота": cmd_show_name_bot,
    "Назад": keyboard_create
}




# === Обработчик нажатий на кнопки ===
@bot.message_handler(func=lambda m: m.text in BUTTON_TO_COMMAND or m.text in ADMIN_BUTTONS)
def handle_custom_button(message):
    user_id = message.from_user.id

    if message.text in ADMIN_BUTTONS:
        if user_id != ADMIN_ID:
            bot.reply_to(message, "У вас нет доступа к этой кнопке.")
            return
        handler = ADMIN_BUTTONS[message.text]
    else:
        handler = BUTTON_TO_COMMAND.get(message.text)

    if handler:
        handler(message)





# === обрезка истории без роли ==
def trim_history(history, max_length):
    if len(history) <= max_length:
        return history
    return [history[0]] + history[-(max_length - 1):]


# === Функция для получения ответа от нейросети с историей ===
def get_ai_response(user_id, user_message):
    global dialogues

    user_id_str = str(user_id)

    # Загружаем данные пользователя и конфига
    roles = load_roles()
    config = load_config()
    io_api_key = config.get("IO_API_KEY", "")
    default_name_bot = config.get("NAME_BOT", "Бот")
    name_ai_neyro = config.get("NAME_AI_NEYRO", "gemini-pro") # Убедитесь, что здесь есть значение по умолчанию
    default_role = (
        "Ты отвечаешь в чате телеграмм, используй стиль форматирования HTML "
        "Используй ТОЛЬКО ПОДДЕРЖИВАЕМЫЕ теги HTML: <b>, <i>, <s>, <code>, <pre>. "
        "Ты серьезная молодая девушка, не любишь болтовни и немногословная, "
        "твоя задача отвечать человеку на его вопросы. Тебя зовут {name_bot}, и ты должна притворяться человеком. "
    ).format(name_bot=default_name_bot)

    if not io_api_key or io_api_key == "XXXX":
        return "Ошибка: IO_API_KEY не установлен или является значением по умолчанию 'XXXX'. Используйте команду /key_set, чтобы установить его."

    # Получаем данные пользователя
    user_data = roles.get(user_id_str, {})
    user_custom_name_bot = user_data.get("name_bot")
    user_custom_role = user_data.get("role")

    # Формируем системный промпт
    if user_custom_name_bot and user_custom_role:
        system_prompt_text = f"Тебя зовут {user_custom_name_bot}\n\n{user_custom_role}"
    elif user_custom_name_bot:
        system_prompt_text = (
            "{role}\n\n"
            "Тебя зовут {name_bot}, и ты должна притворяться человеком. "
            "Ты отвечаешь в чате телеграмм, используй стиль форматирования HTML "
            "Используй ТОЛЬКО ПОДДЕРЖИВАЕМЫЕ теги HTML: <b>, <i>, <s>, <code>, <pre>. "
        ).format(role=default_role, name_bot=user_custom_name_bot)
    elif user_custom_role:
        system_prompt_text = user_custom_role
    else:
        system_prompt_text = default_role

    # Gemini API использует "role": "user" и "role": "model", а не "system" и "assistant"
    # Для системного промпта Gemini API рекомендует использовать его как первое сообщение пользователя
    # или как часть первого "user" turn, если модель поддерживает настройки системы.
    # В данном случае, мы будем рассматривать "системный промпт" как часть первого сообщения.
    # Или, что более правильно для Gemini: использовать его как часть контекста, передаваемого в первом user turn.
    # Но для сохранения логики истории, будем добавлять его как первое "user" сообщение с "role": "user".

    # Инициализация или обновление истории для Gemini API
    # Gemini API ожидает структуру: {"role": "user/model", "parts": [{"text": "..."}]}
    if user_id not in dialogues or (
        dialogues[user_id] and
        (dialogues[user_id][0].get("role") != "user" or dialogues[user_id][0].get("parts")[0].get("text") != system_prompt_text)
    ):
        dialogues[user_id] = [{"role": "user", "parts": [{"text": system_prompt_text}]}]
        # Важно: Gemini не имеет отдельной "system" роли в чате.
        # Системный промпт должен быть частью первого запроса пользователя или настройки модели.
        # Для простоты, мы добавляем его как первое сообщение от пользователя.
        # Возможно, вам придется настроить это поведение, если модель начнет отвечать на "системное" сообщение.
        # Более корректный способ - использовать `system_instruction` в `generation_config` или как отдельное поле,
        # если API его предоставляет. Но ваш текущий API_URL предполагает `generateContent` без `system_instruction` в теле запроса.

    # Добавляем текущее сообщение пользователя
    # Проверяем, является ли последнее сообщение в истории пользовательским.
    # Если да, объединяем с ним. Иначе добавляем новое.
    if dialogues[user_id] and dialogues[user_id][-1]["role"] == "user":
        # Если последнее сообщение от пользователя, добавляем к нему новые части
        dialogues[user_id][-1]["parts"].append({"text": user_message})
    else:
        # Иначе добавляем новое сообщение от пользователя
        dialogues[user_id].append({"role": "user", "parts": [{"text": user_message}]})


    try:
        # Формируем полный URL для запроса к Gemini
        # Например: https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent
        full_api_url = f"{GEMINI_BASE_URL}/{name_ai_neyro}:generateContent"

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": io_api_key # Использование правильного заголовка для API ключа Google
        }

        request_data = {
            "contents": dialogues[user_id] # Используем "contents" вместо "messages"
        }

        response = requests.post(full_api_url, headers=headers, json=request_data, timeout=60)

        if response.status_code == 200:
            response_json = response.json()
            if 'candidates' in response_json and response_json['candidates']:
                # Получаем ответ из первого кандидата, из первой части (текст)
                ai_reply = response_json['candidates'][0]['content']['parts'][0]['text']

                # Добавляем ответ ИИ в историю
                dialogues[user_id].append({
                    "role": "model", # Роль "model" для Gemini API
                    "parts": [{"text": ai_reply}]
                })

                # Ограничиваем длину истории (учитывая системный промпт как первое сообщение)
                # Если системный промпт всегда на первом месте и его не нужно обрезать,
                # то обрезка начинается со второго элемента.
                if len(dialogues[user_id]) > MAX_HISTORY_MESSAGES:
                    dialogues[user_id] = [dialogues[user_id][0]] + dialogues[user_id][-(MAX_HISTORY_MESSAGES - 1):]


                return ai_reply
            else:
                return f"Ошибка: Ответ Gemini не содержит 'candidates' или он пуст. Ответ: {response.text}"
        else:
            return f"Ошибка при получении ответа от Gemini API: {response.status_code}, {response.text}"

    except requests.exceptions.Timeout:
        return "Произошла ошибка: Истекло время ожидания ответа от AI-модели."
    except requests.exceptions.ConnectionError:
        return "Произошла ошибка: Не удалось подключиться к AI-модели. Проверьте ваше интернет-соединение."
    except Exception as e:
        return f"Произошла непредвиденная ошибка: {e}"


# === Обработчик всех текстовых сообщений ===
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if not is_allowed_message(message):
        return

    chat_id = message.chat.id
    user_input = message.text
    user_id = message.from_user.id
    save_id_user(user_id)
    bot.send_chat_action(chat_id, 'typing')
    ai_reply = get_ai_response(user_id, user_input)

    #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
    # НОВАЯ ПРОВЕРКА: ЕСЛИ ПОЛЬЗОВАТЕЛЬ НЕ РАЗРЕШЕН, ОСТАНОВИТЬ
    if Free_Chat == 0:
        if user_id not in ALLOWED_USERS:
            bot.send_message(user_id, "Извините, сейчас бот работает только для разрешенных пользователей.")
            return
    #+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++    
    try:
        # Убираем все до </think>, если оно есть.
        # Это специфично для вашей логики и не относится напрямую к работе Gemini API.
        pattern = r'</think>[\r\n]*'
        parts = re.split(pattern, ai_reply)
        escaped_text_h = parts[-1].strip()
        escaped_text = sanitize_html(escaped_text_h)
    except IndexError:
        escaped_text_h = ai_reply.strip()
        escaped_text = sanitize_html(escaped_text_h)

    # Пытаемся получить thread_id
    thread_id = getattr(message, 'message_thread_id', None)
    if thread_id is None and getattr(message, 'reply_to_message', None):
        thread_id = getattr(message.reply_to_message, 'message_thread_id', None)

    logging.info(f"Thread ID: {thread_id}, Chat Type: {message.chat.type}, Is Forum: {getattr(message.chat, 'is_forum', False)}")

    # Добавляем thread_id ТОЛЬКО если это супергруппа и включены темы (форум)
    if message.chat.type == 'supergroup' and getattr(message.chat, 'is_forum', False) and isinstance(thread_id, int):
        send_long_message(bot, chat_id, escaped_text, parse_mode='HTML', message_thread_id=thread_id)
    else:
        send_long_message(bot, chat_id, escaped_text, parse_mode='HTML')


# === Запуск бота ===
logging.info("Бот запущен...")

while True:
    try:
        # none_stop=True - уже обеспечивает перезапуск для большинства ошибок
        # но обертывание в try/except защищает от фатальных ошибок
        bot.polling(none_stop=True, interval=0, timeout=40)
    
    except Exception as e:
        # Логирование критической ошибки
        logging.info(f"*** КРИТИЧЕСКАЯ ОШИБКА ВНЕ ПОЛЛИНГА: {e} ***")
        # Ждём перед попыткой перезапуска
        time.sleep(15)


