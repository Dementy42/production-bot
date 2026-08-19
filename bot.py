import asyncio
import logging
import os
import sys
from datetime import datetime, date

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

from database import Database

# ====== Настройка логирования ======
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ====== Загрузка токена ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Создайте файл .env с токеном бота.")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
db = Database()

# ====== ГЛОБАЛЬНАЯ ЗАЩИТА CALLBACK'ОВ ======
from aiogram.types import CallbackQuery
import traceback

# ====== ПРАВИЛЬНАЯ РЕГИСТРАЦИЯ MIDDLEWARE ======
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery
import traceback


class ErrorHandlerMiddleware(BaseMiddleware):
    """Middleware для отлова ошибок во всех callback'ах"""
    
    async def __call__(self, handler, event: TelegramObject, data):
        try:
            return await handler(event, data)
        except Exception as e:
            # Логируем ошибку
            logger.error(f"❌ Ошибка: {e}")
            logger.error(traceback.format_exc())
            
            # Если это callback — отвечаем и показываем ошибку
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer(f"⚠️ Ошибка: {str(e)[:50]}", show_alert=True)
                except:
                    pass
                
                try:
                    await event.message.answer(
                        f"🚨 <b>Произошла ошибка</b>\n\n"
                        f"Действие: <code>{event.data}</code>\n"
                        f"Ошибка: <code>{str(e)[:200]}</code>\n\n"
                        f"Попробуйте ещё раз.",
                        parse_mode="HTML"
                    )
                except:
                    pass
            
            # Помечаем как обработанное
            return None


# Регистрируем middleware на ВСЕ обновления (включая callback_query)
dp.update.outer_middleware(ErrorHandlerMiddleware())


# ====== СПИСОК ГЛАВНЫХ АДМИНИСТРАТОРОВ ======
ADMIN_IDS = [
    1173990828,  # ← ВАШ TELEGRAM ID
]

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_senior_or_admin(user):
    return user and (user[3] in ['senior', 'admin'] or is_admin(user[0]))

def is_operator_or_higher(user):
    return user is not None


# ====== Состояния FSM ======
class AuthStates(StatesGroup):
    waiting_auth_method = State()
    waiting_username = State()
    waiting_fullname = State()

class BoxAddStates(StatesGroup):
    waiting_part = State()
    waiting_type = State()
    waiting_quantity = State()
    waiting_confirmation = State()

class TaskCreateStates(StatesGroup):
    waiting_sector = State()
    waiting_part = State()
    waiting_shift_time = State()
    waiting_printers = State()
    waiting_launches = State()
    waiting_parts_per_table = State()
    waiting_confirmation = State()


# ====== Вспомогательные функции ======
def format_sector_report(sector_id, shift_date):
    tasks = db.get_all_tasks_for_sector(sector_id, shift_date)
    sector_name = db.get_sector_name(sector_id)
    
    if not tasks:
        return f"📋 <b>{sector_name}</b>\nНет заданий на текущую смену."
    
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    report = f"📋 <b>{sector_name}</b>\n🕒 {now}\n\n"
    
    for task in tasks:
        stats = db.get_task_statistics(task[0])
        
        # 🔥 ИСПРАВЛЕНИЕ: факт = только годные детали
        total_good = stats[0]      # годные
        total_defect = stats[1]    # брак
        total_printed = total_good + total_defect  # всего напечатано
        plan = task[7]
        
        # Процент выполнения (от годных к плану)
        percentage = (total_good / plan * 100) if plan > 0 else 0
        
        # 🔥 НОВОЕ: сколько деталей не хватает
        remaining = plan - total_good
        if remaining > 0:
            remaining_text = f"⬇️ не хватает: <b>{remaining}</b>"
        elif remaining == 0:
            remaining_text = "✅ <b>план выполнен!</b>"
        else:
            remaining_text = f"⬆️ перевыполнение: <b>{abs(remaining)}</b>"
        
        report += (
            f"📦 <b>Деталь {task[2]}</b> ({task[3]})\n"
            f"🖨️ {task[4]} принтеров | "
            f"съемов {stats[2]}/{task[5]}\n"
            f"📊 факт/план: <b>{total_good}/{plan}</b> "
            f"({percentage:.0f}%)\n"
            f"{remaining_text}\n"
            f"❌ брак: <b>{total_defect}</b>\n"
            f"📦 всего напечатано: {total_printed}\n\n"
        )
    
    return report

async def notify_seniors(message_text):
    for senior_id in db.get_senior_operators():
        try:
            await bot.send_message(senior_id, message_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка уведомления senior {senior_id}: {e}")


# ====== /start ======
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    
    if user:
        await message.answer(
            f"👋 Добро пожаловать, {user[2]}!\n"
            f"Роль: {user[3]}\n\nИспользуйте /menu для навигации."
        )
        
        if user[3] == 'operator' and user[4]:
            active_shift = db.get_active_shift_for_operator(message.from_user.id)
            if not active_shift:
                if db.sector_exists(user[4]):
                    shift_id = db.open_shift(message.from_user.id, user[4])
                    sector_name = db.get_sector_name(user[4])
                    await message.answer(
                        f"✅ Смена автоматически открыта!\n"
                        f"Участок: {sector_name}"
                    )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Ввести ФИО", callback_data="auth_name")],
            [InlineKeyboardButton(text="🔑 Ввести никнейм", callback_data="auth_username")]
        ])
        await state.set_state(AuthStates.waiting_auth_method)
        await message.answer(
            "🔐 <b>Вы не найдены в системе</b>\n\n"
            "Выберите способ авторизации:",
            reply_markup=kb, parse_mode="HTML"
        )


@dp.callback_query(AuthStates.waiting_auth_method, F.data == "auth_username")
async def auth_choose_username(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AuthStates.waiting_username)
    await callback.message.edit_text("🔑 Введите ваш никнейм (как в базе сотрудников):")
    await callback.answer()


@dp.callback_query(AuthStates.waiting_auth_method, F.data == "auth_name")
async def auth_choose_name(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AuthStates.waiting_fullname)
    await callback.message.edit_text(
        "📝 Введите ваше ФИО (как в базе сотрудников):\n\n"
        "Пример: <code>Иванов Иван Иванович</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(AuthStates.waiting_username)
async def process_auth(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip("@")
    user = db.get_user_by_username(username)
    
    if user:
        tg_username = message.from_user.username or f"id_{message.from_user.id}"
        db.bind_user_to_telegram(username, message.from_user.id, tg_username)
        user = db.get_user_by_telegram_id(message.from_user.id)
        
        await state.clear()
        
        if user[3] == 'operator' and user[4]:
            if db.sector_exists(user[4]):
                db.open_shift(message.from_user.id, user[4])
                sector_name = db.get_sector_name(user[4])
                await message.answer(
                    f"✅ Авторизация успешна!\n"
                    f"Роль: {user[3]}\n"
                    f"Участок: {sector_name}\n\nИспользуйте /menu"
                )
            else:
                await message.answer(
                    f"✅ Авторизация успешна!\n"
                    f"⚠️ Ваш участок не существует. Обратитесь к старшему оператору."
                )
        else:
            await message.answer(
                f"✅ Авторизация успешна!\n"
                f"Роль: {user[3]}\n\nИспользуйте /menu"
            )
    else:
        await message.answer("❌ Никнейм не найден. Попробуйте через ФИО (/start).")


@dp.message(AuthStates.waiting_fullname)
async def process_auth_by_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    
    if db.bind_user_by_fullname(full_name, message.from_user.id, 
                                 message.from_user.username or f"id_{message.from_user.id}"):
        user = db.get_user_by_telegram_id(message.from_user.id)
        await state.clear()
        
        if user[3] == 'operator' and user[4]:
            if db.sector_exists(user[4]):
                db.open_shift(message.from_user.id, user[4])
                sector_name = db.get_sector_name(user[4])
                await message.answer(
                    f"✅ Авторизация успешна!\n"
                    f"Роль: {user[3]}\n"
                    f"Участок: {sector_name}\n\nИспользуйте /menu"
                )
        else:
            await message.answer(
                f"✅ Авторизация успешна!\n"
                f"Роль: {user[3]}\n\nИспользуйте /menu"
            )
    else:
        await message.answer(
            "❌ ФИО не найдено в системе.\n\n"
            "Проверьте правильность написания или обратитесь к старшему оператору.\n"
            "Команда /start — вернуться к выбору."
        )


# ====== /menu ======
@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала авторизуйтесь: /start")
        return
    
    if is_admin(message.from_user.id):
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📋 Задание"), KeyboardButton(text="📊 Отчет по участку")],
                [KeyboardButton(text="➕ Создать ПЗ"), KeyboardButton(text="📍 Все участки")],
                [KeyboardButton(text="📄 Сформировать отчет"), KeyboardButton(text="🔄 Закрыть смену")],
                [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="📊 Статистика")]
            ],
            resize_keyboard=True
        )
    elif user[3] == 'senior':
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📋 Задание"), KeyboardButton(text="📊 Отчет по участку")],
                [KeyboardButton(text="➕ Создать ПЗ"), KeyboardButton(text="📍 Все участки")],
                [KeyboardButton(text="📄 Сформировать отчет"), KeyboardButton(text="🔄 Закрыть смену")]
            ],
            resize_keyboard=True
        )
    else:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📋 Задание"), KeyboardButton(text="📊 Отчет по участку")],
                [KeyboardButton(text="➕ Добавить коробку"), KeyboardButton(text="✏️ Редактировать")],
                [KeyboardButton(text="🔄 Закрыть смену")]
            ],
            resize_keyboard=True
        )
    await message.answer("Главное меню:", reply_markup=keyboard)


# ====== Отчет по участку ======
@dp.message(F.text.in_(["📋 Задание", "📊 Отчет по участку"]))
async def show_report(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала авторизуйтесь: /start")
        return
    
    if user[3] == 'operator':
        shift = db.get_active_shift_for_operator(message.from_user.id)
        if not shift:
            await message.answer("У вас нет активной смены. Используйте /openshift")
            return
        await message.answer(
            format_sector_report(shift[2], date.today().isoformat()),
            parse_mode="HTML"
        )
    else:
        sectors = db.get_all_sectors()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=s[1], callback_data=f"sr_{s[0]}")] 
            for s in sectors
        ])
        await message.answer("Выберите участок:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("sr_"))
async def cb_sector_report(callback: types.CallbackQuery):
    sector_id = int(callback.data.split("_")[1])
    await callback.message.edit_text(
        format_sector_report(sector_id, date.today().isoformat()),
        parse_mode="HTML"
    )
    await callback.answer()


# ====== Добавление коробки ======
@dp.message(F.text == "➕ Добавить коробку")
async def start_box_add(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user or user[3] != 'operator':
        await message.answer("Доступ запрещен.")
        return
    
    shift = db.get_active_shift_for_operator(message.from_user.id)
    if not shift:
        await message.answer("У вас нет активной смены. Используйте /openshift")
        return
    
    await state.set_state(BoxAddStates.waiting_part)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]
    ])
    await message.answer("Введите номер детали:", reply_markup=kb)


@dp.message(BoxAddStates.waiting_part)
async def process_part(message: types.Message, state: FSMContext):
    part = message.text.strip()
    await state.update_data(part_number=part)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Годен", callback_data="bt_good"),
            InlineKeyboardButton(text="❌ Брак", callback_data="bt_defect")
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]
    ])
    await state.set_state(BoxAddStates.waiting_type)
    await message.answer(f"Деталь <b>{part}</b>. Выберите тип:", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(BoxAddStates.waiting_type, F.data.in_(["bt_good", "bt_defect"]))
@dp.callback_query(BoxAddStates.waiting_type, F.data.in_(["bt_good", "bt_defect"]))
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    try:
        box_type = "good" if callback.data == "bt_good" else "defect"
        await state.update_data(box_type=box_type, quantity=0)
        await state.set_state(BoxAddStates.waiting_quantity)
        
        # Отвечаем СРАЗУ, чтобы снять часики
        await callback.answer()
        
        # Теперь обновляем клавиатуру
        await show_qty_keyboard(callback.message, state, 0)
        
    except Exception as e:
        logger.error(f"Ошибка в process_type: {e}")
        try:
            await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
        except:
            pass

async def show_qty_keyboard(msg_obj, state: FSMContext, quantity):
    data = await state.get_data()
    type_text = "годен" if data.get('box_type') == 'good' else "брак"
    part_number = data.get('part_number', '?')
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+1", callback_data="q_+1"),
            InlineKeyboardButton(text="-1", callback_data="q_-1"),
            InlineKeyboardButton(text="+5", callback_data="q_+5")
        ],
        [
            InlineKeyboardButton(text="+10", callback_data="q_+10"),
            InlineKeyboardButton(text="+50", callback_data="q_+50"),
            InlineKeyboardButton(text="+100", callback_data="q_+100")
        ],
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="q_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]
    ])
    
    text = (
        f"Деталь <b>{part_number}</b> ({type_text})\n"
        f"Количество: <b>{quantity}</b>\n\n"
        f"Или введите число текстом:"
    )
    
    # 🔥 ЗАЩИТА: оборачиваем edit_text в try/except
    try:
        if isinstance(msg_obj, types.Message):
            await msg_obj.answer(text, reply_markup=kb, parse_mode="HTML")
        else:
            try:
                await msg_obj.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                # Если edit_text упал (например, "message is not modified")
                # отправляем новым сообщением
                if "message is not modified" in str(e).lower():
                    pass  # игнорируем — текст такой же
                else:
                    # fallback: отправляем новое сообщение
                    await msg_obj.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в show_qty_keyboard: {e}")


@dp.callback_query(BoxAddStates.waiting_quantity, F.data.startswith("q_"))
@dp.callback_query(BoxAddStates.waiting_quantity, F.data.startswith("q_"))
async def process_qty_button(callback: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        quantity = data.get('quantity', 0)
        action = callback.data.split("_")[1]
        
        if action == "confirm":
            if quantity > 0:
                await callback.answer()  # снять часики
                await show_confirmation(callback.message, state)
            else:
                await callback.answer("Количество должно быть > 0", show_alert=True)
            return
        
        if action.startswith("+"):
            quantity += int(action[1:])
        elif action.startswith("-"):
            quantity = max(0, quantity - int(action[1:]))
        
        await state.update_data(quantity=quantity)
        await callback.answer()  # 🔥 СНАЧАЛА answer
        await show_qty_keyboard(callback.message, state, quantity)
        
    except Exception as e:
        logger.error(f"Ошибка в process_qty_button: {e}")
        try:
            await callback.answer(f"Ошибка: {e}", show_alert=True)
        except:
            pass

@dp.callback_query(BoxAddStates.waiting_quantity, F.data.startswith("t_"))
async def process_table_button(callback: types.CallbackQuery, state: FSMContext):
    """Обработка кнопок +1/-1 стол"""
    data = await state.get_data()
    quantity = data.get('quantity', 0)
    part_name = data.get('part_number')
    
    # Получаем parts_per_table из БД
    part_info = db.get_part_by_name(part_name)
    if not part_info:
        await callback.answer("❌ Деталь не найдена в базе", show_alert=True)
        return
    
    parts_per_table = part_info[4]
    
    # Парсим действие: t_+1, t_-1, t_+5, t_+10
    action = callback.data.split("_")[1]
    
    if action.startswith("+"):
        multiplier = int(action[1:])
        quantity += parts_per_table * multiplier
    elif action.startswith("-"):
        multiplier = int(action[1:])
        quantity = max(0, quantity - parts_per_table * multiplier)
    
    await state.update_data(quantity=quantity)
    await show_qty_keyboard(callback.message, state, quantity)
    await callback.answer(f"±{parts_per_table * (multiplier if 'multiplier' in locals() else 1)} шт.")

@dp.message(BoxAddStates.waiting_quantity)
async def process_qty_text(message: types.Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty < 0:
            await message.answer("Не может быть отрицательным.")
            return
        await state.update_data(quantity=qty)
        await show_confirmation(message, state)
    except ValueError:
        await message.answer("Введите целое число.")


async def show_confirmation(msg_obj, state: FSMContext):
    data = await state.get_data()
    type_text = "годен" if data.get('box_type') == 'good' else "брак"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data="cf_yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="cf_no")
        ]
    ])
    await state.set_state(BoxAddStates.waiting_confirmation)
    
    text = (
        f"<b>Подтвердите:</b>\n"
        f"Деталь: {data.get('part_number')}\n"
        f"Тип: {type_text}\n"
        f"Количество: {data.get('quantity')}"
    )
    
    if isinstance(msg_obj, types.Message):
        await msg_obj.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg_obj.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(BoxAddStates.waiting_confirmation, F.data == "cf_yes")
async def confirm_box_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = db.get_user_by_telegram_id(callback.from_user.id)
    shift = db.get_active_shift_for_operator(callback.from_user.id)
    
    if not shift:
        await callback.message.edit_text("❌ Смена не активна")
        await state.clear()
        await callback.answer()
        return
    
    tasks = db.get_all_tasks_for_sector(shift[2], date.today().isoformat())
    task = next((t for t in tasks if str(t[2]) == str(data.get('part_number'))), None)
    
    if not task:
        await callback.message.edit_text(
            f"❌ Деталь {data.get('part_number')} не найдена в ПЗ.",
            parse_mode="HTML"
        )
        await state.clear()
        await callback.answer()
        return
    
    if not db.is_task_locked(task[0]):
        db.lock_task(task[0])
    
    boxes = db.get_boxes_for_task(task[0])
    box_number = len(boxes) + 1
    
    good = data.get('quantity') if data.get('box_type') == 'good' else 0
    defect = data.get('quantity') if data.get('box_type') == 'defect' else 0
    
    db.add_box(task[0], data.get('part_number'), box_number, good, defect)
    
    stats = db.get_task_statistics(task[0])
    total_fact = stats[0]
    pct = (total_fact / task[7] * 100) if task[7] > 0 else 0
    type_text = "годен" if data.get('box_type') == 'good' else "брак"
    
    report_text = (
        f"✅ <b>Коробка #{box_number} добавлена!</b>\n"
        f"Деталь: {data.get('part_number')} ({type_text}): {data.get('quantity')}\n\n"
        f"📊 <b>Обновленный отчет:</b>\n{format_sector_report(shift[2], date.today().isoformat())}"
    )
    await callback.message.edit_text(report_text, parse_mode="HTML")
    
    await notify_seniors(
        f"🔔 <b>Оператор {user[2]}</b> добавил коробку #{box_number}\n"
        f"Участок: {db.get_sector_name(shift[2])}\n"
        f"{data.get('part_number')} ({type_text}): {data.get('quantity')}\n"
        f"Факт/План: {total_fact}/{task[7]} ({pct:.0f}%)"
    )
    
    await state.clear()
    await callback.answer()


@dp.callback_query(BoxAddStates.waiting_confirmation, F.data == "cf_no")
async def confirm_box_no(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BoxAddStates.waiting_part)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]
    ])
    await callback.message.edit_text("Введите номер детали:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "cancel_box")
async def cancel_box(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Добавление отменено.")
    await callback.answer()


# ====== Редактирование коробок ======
@dp.message(F.text == "✏️ Редактировать")
@dp.message(Command("box_edit"))
async def box_edit(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user or user[3] != 'operator':
        await message.answer("Доступ запрещен.")
        return
    
    shift = db.get_active_shift_for_operator(message.from_user.id)
    if not shift:
        await message.answer("У вас нет активной смены. Используйте /openshift")
        return
    
    tasks = db.get_all_tasks_for_sector(shift[2], date.today().isoformat())
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t in tasks:
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"Деталь {t[2]} ({t[3]})", 
                callback_data=f"edit_part_{t[0]}"
            )
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")])
    
    await message.answer("Выберите деталь для редактирования:", reply_markup=kb)


@dp.callback_query(F.data.startswith("edit_part_"))
async def edit_part_select(callback: types.CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[2])
    task = db.get_task(task_id)
    boxes = db.get_boxes_for_task(task_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for box in boxes:
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"Коробка #{box[3]} (годен:{box[4]}, брак:{box[5]})",
                callback_data=f"edit_box_{box[0]}"
            )
        ])
    
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="➕ Добавить брак", callback_data=f"edit_add_defect_{task_id}")
    ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")])
    
    await state.update_data(edit_task_id=task_id)
    await callback.message.edit_text(
        f"Деталь {task[2]} ({task[3]}). Выберите коробку:",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_box_"))
@dp.callback_query(F.data.startswith("edit_add_defect_"))
@dp.callback_query(F.data.startswith("edit_box_"))
@dp.callback_query(F.data.startswith("edit_add_defect_"))
async def edit_box_select(callback: types.CallbackQuery, state: FSMContext):
    is_new_defect = callback.data.startswith("edit_add_defect_")
    
    try:
        if is_new_defect:
            task_id = int(callback.data.split("_")[3])
            task = db.get_task(task_id)
            if not task:
                await callback.answer("❌ Задание не найдено", show_alert=True)
                return
            
            boxes = db.get_boxes_for_task(task_id)
            box_number = len(boxes) + 1
            
            db.add_box(task_id, task[2], box_number, 0, 0)
            box_id = db.get_boxes_for_task(task_id)[-1][0]
            part_number = task[2]  # 🔥 сохраняем номер детали
            
            await state.update_data(
                edit_box_id=box_id, 
                is_new_defect=True,
                part_number=part_number,  # 🔥 сохраняем в state
                box_type='defect'  # для добавления брака
            )
        else:
            box_id = int(callback.data.split("_")[2])
            
            # Получаем part_number из существующей коробки
            with db.get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT part_number FROM boxes WHERE id = ?", (box_id,))
                row = c.fetchone()
                part_number = row[0] if row else "?"
            
            await state.update_data(
                edit_box_id=box_id, 
                is_new_defect=False,
                part_number=part_number,  # 🔥 сохраняем в state
                box_type='good'
            )
        
        await state.update_data(quantity=0)
        await state.set_state(BoxAddStates.waiting_quantity)
        
        # Отвечаем СРАЗУ, чтобы часики не крутились
        await callback.answer()
        
        # Теперь обновляем сообщение
        await show_qty_keyboard(callback.message, state, 0)
        
    except Exception as e:
        logger.error(f"Ошибка в edit_box_select: {e}")
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

# ====== Создание ПЗ (senior/admin) ======
@dp.message(F.text == "➕ Создать ПЗ")
async def start_create_task(message: types.Message, state: FSMContext):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    sectors = db.get_all_sectors()
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for s in sectors:
        locked = db.has_locked_tasks_today(s[0], date.today().isoformat())
        text = f"{s[1]} 🔒" if locked else s[1]
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=text, 
                callback_data=f"ts_{s[0]}" if not locked else "disabled"
            )
        ])
    
    await state.set_state(TaskCreateStates.waiting_sector)
    await message.answer("Выберите участок (🔒 - уже есть съем):", reply_markup=kb)


@dp.callback_query(TaskCreateStates.waiting_sector, F.data.startswith("ts_"))
async def create_task_sector(callback: types.CallbackQuery, state: FSMContext):
    sector_id = int(callback.data.split("_")[1])
    await state.update_data(sector_id=sector_id)
    await state.set_state(TaskCreateStates.waiting_part)
    await callback.message.edit_text("Введите номер детали:")
    await callback.answer()


@dp.message(TaskCreateStates.waiting_part)
async def create_task_part(message: types.Message, state: FSMContext):
    await state.update_data(part_number=message.text.strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="☀️ День", callback_data="st_день"),
            InlineKeyboardButton(text="🌙 Ночь", callback_data="st_ночь")
        ]
    ])
    await state.set_state(TaskCreateStates.waiting_shift_time)
    await message.answer("Выберите смену:", reply_markup=kb)


@dp.callback_query(TaskCreateStates.waiting_shift_time, F.data.startswith("st_"))
async def create_task_shift_time(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(shift_time=callback.data.split("_")[1])
    await state.set_state(TaskCreateStates.waiting_printers)
    await callback.message.edit_text("Введите количество принтеров:")
    await callback.answer()


@dp.message(TaskCreateStates.waiting_printers)
async def create_task_printers(message: types.Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n <= 0: raise ValueError
        await state.update_data(printers_count=n)
        await state.set_state(TaskCreateStates.waiting_launches)
        await message.answer("Введите количество запусков:")
    except ValueError:
        await message.answer("Введите положительное целое число.")


@dp.message(TaskCreateStates.waiting_launches)
async def create_task_launches(message: types.Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n <= 0: raise ValueError
        await state.update_data(launches_count=n)
        await state.set_state(TaskCreateStates.waiting_parts_per_table)
        await message.answer("Введите количество деталей на столе:")
    except ValueError:
        await message.answer("Введите положительное целое число.")


@dp.message(TaskCreateStates.waiting_parts_per_table)
async def create_task_parts(message: types.Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n <= 0: raise ValueError
        await state.update_data(parts_per_table=n)
        
        data = await state.get_data()
        total = data['printers_count'] * data['launches_count'] * data['parts_per_table']
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="tc_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="tc_no")
            ]
        ])
        await state.set_state(TaskCreateStates.waiting_confirmation)
        await message.answer(
            f"<b>Подтвердите ПЗ:</b>\n"
            f"Участок: {db.get_sector_name(data['sector_id'])}\n"
            f"Деталь: {data['part_number']} ({data['shift_time']})\n"
            f"Принтеры: {data['printers_count']}\n"
            f"Запуски: {data['launches_count']}\n"
            f"Деталей/стол: {data['parts_per_table']}\n"
            f"План: <b>{total}</b>",
            reply_markup=kb, parse_mode="HTML"
        )
    except ValueError:
        await message.answer("Введите положительное целое число.")


@dp.callback_query(TaskCreateStates.waiting_confirmation, F.data == "tc_yes")
async def create_task_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    task_id = db.create_task(
        data['sector_id'], data['part_number'], data['shift_time'],
        data['printers_count'], data['launches_count'], data['parts_per_table'],
        date.today().isoformat()
    )
    total = data['printers_count'] * data['launches_count'] * data['parts_per_table']
    await callback.message.edit_text(
        f"✅ ПЗ создано! ID: {task_id}, план: {total}",
        parse_mode="HTML"
    )
    await state.clear()
    await callback.answer()


@dp.callback_query(TaskCreateStates.waiting_confirmation, F.data == "tc_no")
async def create_task_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()


# ====== Все участки ======
@dp.message(F.text == "📍 Все участки")
async def show_all_sectors(message: types.Message, state: FSMContext):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    sectors = db.get_all_sectors()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s[1], callback_data=f"si_{s[0]}")]
        for s in sectors
    ])
    await message.answer("Выберите участок:", reply_markup=kb)


@dp.callback_query(F.data.startswith("si_"))
async def sector_info(callback: types.CallbackQuery):
    sector_id = int(callback.data.split("_")[1])
    await callback.message.edit_text(
        format_sector_report(sector_id, date.today().isoformat()),
        parse_mode="HTML"
    )
    await callback.answer()


# ====== Формирование отчета ======
@dp.message(F.text == "📄 Сформировать отчет")
async def generate_report(message: types.Message, state: FSMContext):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    open_shifts = db.get_open_shifts_count()
    if open_shifts > 0:
        await message.answer(
            f"⚠️ Нельзя сформировать отчет.\nОткрытых смен: {open_shifts}\n"
            f"Дождитесь закрытия всех смен операторами."
        )
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Сегодня", callback_data="rp_today"),
            InlineKeyboardButton(text="📊 Все данные", callback_data="rp_all")
        ]
    ])
    await message.answer("Выберите период:", reply_markup=kb)


@dp.callback_query(F.data.in_(["rp_today", "rp_all"]))
async def generate_report_callback(callback: types.CallbackQuery):
    report_date = date.today().isoformat() if callback.data == "rp_today" else None
    filename = db.export_report_to_csv(
        report_date, 
        f"report_{report_date or 'all'}.csv"
    )
    with open(filename, 'rb') as f:
        await bot.send_document(
            callback.from_user.id,
            types.BufferedInputFile(f.read(), filename=filename)
        )
    os.remove(filename)
    await callback.message.edit_text("✅ Отчет отправлен!")
    await callback.answer()


# ====== Закрытие смены ======
@dp.message(F.text == "🔄 Закрыть смену")
async def close_shift(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Авторизуйтесь: /start")
        return
    
    shift = db.get_active_shift_for_operator(message.from_user.id)
    if not shift:
        await message.answer("У вас нет активной смены.")
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data="cs_yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="cs_no")
        ]
    ])
    await message.answer("Закрыть смену?", reply_markup=kb)


@dp.callback_query(F.data == "cs_yes")
async def confirm_close_shift(callback: types.CallbackQuery):
    user = db.get_user_by_telegram_id(callback.from_user.id)
    shift = db.get_active_shift_for_operator(callback.from_user.id)
    
    if not shift:
        await callback.message.edit_text("❌ Нет активной смены")
        await callback.answer()
        return
    
    db.close_shift(shift[0])
    db.add_shift_log(callback.from_user.id, shift[2], shift[0], "shift_closed")
    
    total_sectors = len(db.get_all_sectors())
    closed = total_sectors - db.get_open_shifts_count()
    
    await notify_seniors(
        f"🔒 <b>Смена закрыта</b>\n"
        f"Оператор: {user[2]}\n"
        f"Участок: {db.get_sector_name(shift[2])}\n"
        f"Закрыто: {closed}/{total_sectors}"
    )
    
    await callback.message.edit_text("✅ Смена закрыта!")
    await callback.answer()


@dp.callback_query(F.data == "cs_no")
async def cancel_close_shift(callback: types.CallbackQuery):
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ====== Админские команды ======
@dp.message(Command("myid"))
async def get_my_id(message: types.Message):
    await message.answer(
        f"🔑 <b>Ваш Telegram ID:</b> <code>{message.from_user.id}</code>",
        parse_mode="HTML"
    )


@dp.message(Command("whoami"))
async def who_am_i(message: types.Message):
    user = db.get_user_by_telegram_id(message.from_user.id)
    
    if is_admin(message.from_user.id):
        access_level = "👑 <b>СУПЕР-АДМИНИСТРАТОР</b>"
    elif user and user[3] == 'senior':
        access_level = "⭐ Старший оператор"
    elif user and user[3] == 'operator':
        access_level = "👷 Оператор"
    else:
        access_level = "❌ Не авторизован"
    
    text = (
        f"👤 <b>Пользователь:</b> {message.from_user.full_name}\n"
        f"🔑 <b>ID:</b> <code>{message.from_user.id}</code>\n"
        f"🏷️ <b>Username:</b> @{message.from_user.username or 'нет'}\n"
        f"🎭 <b>Уровень доступа:</b> {access_level}"
    )
    
    if user:
        sector_name = db.get_sector_name(user[4]) if user[4] else "не указан"
        text += f"\n🏭 <b>Участок:</b> {sector_name}"
        text += f"\n📋 <b>Роль в БД:</b> {user[3]}"
    
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("adduser"))
async def admin_add_user(message: types.Message):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    parts = message.text.split()
    
    if len(parts) < 4:
        await message.answer(
            "📋 <b>Формат команды:</b>\n\n"
            "<b>С Telegram ID:</b>\n"
            "<code>/adduser 123456789 operator 16 Иванов Иван</code>\n\n"
            "<b>Без Telegram ID (пользователь введёт ФИО при входе):</b>\n"
            "<code>/adduser operator 16 Иванов Иван</code>\n\n"
            "Доступные участки: /list_sectors",
            parse_mode="HTML"
        )
        return
    
    try:
        try:
            tg_id = int(parts[1])
            role = parts[2]
            
            try:
                sector_id = int(parts[3])
                full_name = " ".join(parts[4:])
                username = f"user_{tg_id}"
            except ValueError:
                sector_id = None
                full_name = " ".join(parts[3:])
                username = f"user_{tg_id}"
                
        except ValueError:
            tg_id = None
            role = parts[1]
            
            try:
                sector_id = int(parts[2])
                full_name = " ".join(parts[3:])
            except ValueError:
                sector_id = None
                full_name = " ".join(parts[2:])
            
            username = f"pending_{full_name.replace(' ', '_')}"
        
        if role not in ['operator', 'senior', 'admin']:
            await message.answer("❌ Роль: 'operator', 'senior' или 'admin'")
            return
        
        if sector_id is not None and not db.sector_exists(sector_id):
            sectors = db.get_all_sectors()
            sector_list = "\n".join([f"  ID {s[0]}: {s[1]}" for s in sectors])
            await message.answer(
                f"❌ Участок с ID {sector_id} не существует!\n\n"
                f"Доступные участки:\n{sector_list}"
            )
            return
        
        if tg_id:
            db.add_user(tg_id, username, full_name, role, sector_id)
        else:
            db.add_user_without_tg(username, full_name, role, sector_id)
        
        sector_name = db.get_sector_name(sector_id) if sector_id else "не указан"
        tg_info = f"ID: {tg_id}" if tg_id else "⚠️ Без TG ID (войдёт по ФИО)"
        
        await message.answer(
            f"✅ <b>Пользователь добавлен!</b>\n\n"
            f"📋 Имя: {full_name}\n"
            f"🎭 Роль: {role}\n"
            f"🏭 Участок: {sector_name}\n"
            f"🔑 {tg_info}",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("add_sector"))
async def add_sector_cmd(message: types.Message):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    parts = message.text.split(maxsplit=2)
    
    if len(parts) < 2:
        await message.answer(
            "📋 Формат:\n"
            "<code>/add_sector Участок 16</code>\n"
            "<code>/add_sector 16 Участок 16</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        try:
            sector_id = int(parts[1])
            sector_name = parts[2] if len(parts) > 2 else f"Участок {sector_id}"
            
            with db.get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT id FROM sectors WHERE id = ?", (sector_id,))
                if c.fetchone():
                    await message.answer(f"❌ Участок с ID {sector_id} уже существует!")
                    return
                
                c.execute("INSERT INTO sectors (id, name) VALUES (?, ?)", 
                         (sector_id, sector_name))
                conn.commit()
                
        except ValueError:
            sector_name = " ".join(parts[1:])
            
            with db.get_connection() as conn:
                c = conn.cursor()
                c.execute("INSERT INTO sectors (name) VALUES (?)", (sector_name,))
                conn.commit()
                sector_id = c.lastrowid
        
        await message.answer(
            f"✅ <b>Участок создан!</b>\nID: {sector_id}\nНазвание: {sector_name}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("list_sectors"))
async def list_sectors_cmd(message: types.Message):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    sectors = db.get_all_sectors()
    
    if not sectors:
        await message.answer("Участков нет")
        return
    
    text = "📋 <b>Все участки:</b>\n\n"
    for s in sectors:
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM users WHERE sector_id = ? AND role = 'operator'", (s[0],))
            operator_count = c.fetchone()[0]
        
        text += f"<b>{s[1]}</b> (ID: {s[0]}) — операторов: {operator_count}\n"
    
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("change_sector"))
async def change_sector_cmd(message: types.Message):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Формат: <code>/change_sector 123456789 16</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        telegram_id = int(parts[1])
        new_sector_id = int(parts[2])
        
        target_user = db.get_user_by_telegram_id(telegram_id)
        if not target_user:
            await message.answer("❌ Пользователь не найден")
            return
        
        if not db.sector_exists(new_sector_id):
            await message.answer(f"❌ Участок {new_sector_id} не существует")
            return
        
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET sector_id = ? WHERE telegram_id = ?",
                     (new_sector_id, telegram_id))
            conn.commit()
        
        sector_name = db.get_sector_name(new_sector_id)
        await message.answer(
            f"✅ Участок изменен!\n"
            f"Пользователь: {target_user[2]}\n"
            f"Новый участок: {sector_name}",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("openshift"))
async def open_shift_cmd(message: types.Message):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user or user[3] != 'operator':
        await message.answer("Только для операторов.")
        return
    
    active_shift = db.get_active_shift_for_operator(message.from_user.id)
    if active_shift:
        sector_name = db.get_sector_name(active_shift[2])
        await message.answer(f"⚠️ Уже есть активная смена: {sector_name}")
        return
    
    if not user[4] or not db.sector_exists(user[4]):
        await message.answer("❌ У вас нет участка.")
        return
    
    shift_id = db.open_shift(message.from_user.id, user[4])
    sector_name = db.get_sector_name(user[4])
    
    await message.answer(
        f"✅ Смена открыта!\n"
        f"Участок: {sector_name}"
    )


@dp.message(Command("checkshift"))
async def check_shift_cmd(message: types.Message):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала авторизуйтесь: /start")
        return
    
    active_shift = db.get_active_shift_for_operator(message.from_user.id)
    
    if active_shift:
        sector_name = db.get_sector_name(active_shift[2])
        await message.answer(
            f"✅ Активная смена\n"
            f"Участок: {sector_name}\n"
            f"Начало: {active_shift[3]}"
        )
    else:
        await message.answer("❌ Нет активной смены.")


@dp.message(Command("fixshifts"))
async def fix_all_shifts(message: types.Message):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    operators = db.get_all_operators()
    
    fixed_count = 0
    results = []
    
    for op in operators:
        tg_id, name, sector_id = op
        if not tg_id or not sector_id or not db.sector_exists(sector_id):
            continue
        
        active_shift = db.get_active_shift_for_operator(tg_id)
        if not active_shift:
            db.open_shift(tg_id, sector_id)
            sector_name = db.get_sector_name(sector_id)
            results.append(f"✅ {name} → {sector_name}")
            fixed_count += 1
        else:
            results.append(f"➡️ {name} — уже есть смена")
    
    report = f"📋 <b>Обработка завершена</b>\n\n"
    report += f"Всего операторов: {len(operators)}\n"
    report += f"Открыто смен: {fixed_count}\n\n"
    report += "\n".join(results) if results else "Нет операторов"
    
    await message.answer(report, parse_mode="HTML")


# ====== Команды только для СУПЕР-АДМИНА ======
@dp.message(Command("addadmin"))
async def add_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/addadmin 123456789 Иванов Иван</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        tg_id = int(parts[1])
        full_name = parts[2] if len(parts) > 2 else f"Admin {tg_id}"
        username = f"admin_{tg_id}"
        
        db.add_user(tg_id, username, full_name, 'admin', None)
        await message.answer(
            f"✅ <b>Назначен новый администратор!</b>\n"
            f"ID: <code>{tg_id}</code>\n"
            f"Имя: {full_name}",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("listadmins"))
async def list_admins(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    admins = db.get_admin_users()
    
    if not admins:
        await message.answer("Администраторов в БД нет")
        return
    
    text = "👑 <b>Администраторы:</b>\n\n"
    for tg_id, name in admins:
        is_super = " ⭐" if tg_id in ADMIN_IDS else ""
        text += f"• {name} (<code>{tg_id}</code>){is_super}\n"
    
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("allusers"))
async def list_all_users(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute("""SELECT telegram_id, full_name, role, sector_id 
                    FROM users WHERE is_active = 1 
                    ORDER BY role, full_name""")
        users = c.fetchall()
    
    if not users:
        await message.answer("Пользователей нет")
        return
    
    text = f"👥 <b>Все пользователи ({len(users)}):</b>\n\n"
    
    current_role = None
    for tg_id, name, role, sector_id in users:
        if role != current_role:
            current_role = role
            role_emoji = {"admin": "👑", "senior": "⭐", "operator": "👷"}.get(role, "❓")
            text += f"\n<b>{role_emoji} {role.upper()}:</b>\n"
        
        sector = f" [{db.get_sector_name(sector_id)}]" if sector_id else ""
        tg = tg_id if tg_id else "—"
        text += f"• {name} (<code>{tg}</code>){sector}\n"
    
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("removeuser"))
async def remove_user(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: <code>/removeuser 123456789</code>", parse_mode="HTML")
        return
    
    try:
        tg_id = int(parts[1])
        
        if tg_id in ADMIN_IDS:
            await message.answer("❌ Нельзя удалить супер-администратора")
            return
        
        if tg_id == message.from_user.id:
            await message.answer("❌ Нельзя удалить самого себя")
            return
        
        target = db.get_user_by_telegram_id(tg_id)
        if not target:
            await message.answer("❌ Пользователь не найден")
            return
        
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (tg_id,))
            conn.commit()
        
        await message.answer(f"✅ {target[2]} деактивирован")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("demote"))
async def demote_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: <code>/demote 123456789</code>", parse_mode="HTML")
        return
    
    try:
        tg_id = int(parts[1])
        
        if tg_id in ADMIN_IDS:
            await message.answer("❌ Нельзя понизить супер-админа")
            return
        
        target = db.get_user_by_telegram_id(tg_id)
        if not target:
            await message.answer("❌ Пользователь не найден")
            return
        
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET role = 'senior' WHERE telegram_id = ?", (tg_id,))
            conn.commit()
        
        await message.answer(f"✅ {target[2]} понижен до senior")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("stats"))
async def system_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    with db.get_connection() as conn:
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
        total_users = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM users WHERE role = 'operator' AND is_active = 1")
        operators = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM users WHERE role = 'senior' AND is_active = 1")
        seniors = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1")
        admins = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM users WHERE telegram_id IS NULL AND is_active = 1")
        pending = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM sectors")
        sectors = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM tasks")
        tasks = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM boxes")
        boxes = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM shifts WHERE status = 'open'")
        open_shifts = c.fetchone()[0]
    
    import os
    db_size = os.path.getsize('production.db') / 1024
    
    stats_text = (
        f"📊 <b>Статистика системы</b>\n\n"
        f"👥 <b>Пользователи:</b> {total_users}\n"
        f"   👑 Админов: {admins}\n"
        f"   ⭐ Senior: {seniors}\n"
        f"   👷 Операторов: {operators}\n"
        f"   ⏳ Ждут входа (без TG ID): {pending}\n\n"
        f"🏭 <b>Участков:</b> {sectors}\n"
        f"📋 <b>Заданий:</b> {tasks}\n"
        f"📦 <b>Коробок:</b> {boxes}\n"
        f"🔄 <b>Открытых смен:</b> {open_shifts}\n\n"
        f"💾 <b>Размер БД:</b> {db_size:.1f} KB\n"
        f"🔑 <b>Супер-админов:</b> {len(ADMIN_IDS)}"
    )
    
    await message.answer(stats_text, parse_mode="HTML")


# ====== МАССОВЫЙ ИМПОРТ ИЗ CSV ======
@dp.message(Command("import"))
async def import_csv(message: types.Message):
    """Импорт пользователей из CSV файла (только супер-админ)"""
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    await message.answer(
        "📥 <b>Массовый импорт из CSV</b>\n\n"
        "Отправьте CSV файл в следующем сообщении.\n\n"
        "<b>Формат CSV (первая строка — заголовок):</b>\n"
        "<code>telegram_id,username,full_name,role,sector_id</code>\n\n"
        "<b>Пример:</b>\n"
        "<code>telegram_id,username,full_name,role,sector_id\n"
        "111111111,ivanov,Иванов Иван Иванович,operator,14\n"
        ",,Петров Петр Петрович,operator,16\n"
        "333333333,sidorov,Сидоров Сидор Сидорович,senior,</code>\n\n"
        "Пустые поля допустимы:\n"
        "• Без telegram_id — пользователь войдёт по ФИО\n"
        "• Без username — будет сгенерирован\n"
        "• Без sector_id — не будет привязан к участку",
        parse_mode="HTML"
    )


@dp.message(F.document)
async def handle_csv_file(message: types.Message):
    """Обработка загруженного CSV файла"""
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    document = message.document
    
    if not document.file_name.endswith('.csv'):
        await message.answer("❌ Нужен файл с расширением .csv")
        return
    
    try:
        # Скачиваем файл
        file = await bot.get_file(document.file_id)
        file_path = f"import_{message.from_user.id}.csv"
        await bot.download_file(file.file_path, file_path)
        
        # Читаем CSV
        users_list = []
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f, delimiter=',')
            header = next(reader, None)  # пропускаем заголовок
            
            if not header:
                await message.answer("❌ Файл пустой")
                os.remove(file_path)
                return
            
            for row in reader:
                if len(row) < 3:
                    continue
                
                # Дополняем строку до 5 элементов
                while len(row) < 5:
                    row.append('')
                
                tg_id = row[0].strip() if row[0] else None
                username = row[1].strip() if row[1] else None
                full_name = row[2].strip()
                role = row[3].strip() if row[3] else 'operator'
                sector_id = row[4].strip() if row[4] else None
                
                if not full_name:
                    continue
                
                users_list.append((tg_id, username, full_name, role, sector_id))
        
        if not users_list:
            await message.answer("❌ В файле нет валидных строк")
            os.remove(file_path)
            return
        
        # Импортируем
        success, errors = db.bulk_import_users(users_list)
        
        # Формируем отчёт
        report = (
            f"📊 <b>Результаты импорта</b>\n\n"
            f"✅ Успешно: <b>{success}</b>\n"
            f"❌ Ошибок: <b>{len(errors)}</b>\n"
        )
        
        if errors:
            report += "\n<b>Ошибки:</b>\n"
            for err in errors[:10]:  # максимум 10
                report += f"• {err}\n"
            if len(errors) > 10:
                report += f"...и ещё {len(errors) - 10}"
        
        await message.answer(report, parse_mode="HTML")
        
        # Удаляем временный файл
        os.remove(file_path)
        
    except Exception as e:
        await message.answer(f"❌ Ошибка при импорте: {e}")
        if os.path.exists(file_path):
            os.remove(file_path)


@dp.message(F.text == "👥 Пользователи")
async def show_users_menu(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return
    
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Импорт CSV"), KeyboardButton(text="📋 Все пользователи")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔙 Назад")]
        ],
        resize_keyboard=True
    )
    await message.answer("Управление пользователями:", reply_markup=kb)


@dp.message(F.text == "📥 Импорт CSV")
async def import_csv_button(message: types.Message):
    await import_csv(message)


@dp.message(F.text == "📋 Все пользователи")
async def all_users_button(message: types.Message):
    await list_all_users(message)


@dp.message(F.text == "📊 Статистика")
async def stats_button(message: types.Message):
    await system_stats(message)


@dp.message(F.text == "🔙 Назад")
async def back_button(message: types.Message):
    await cmd_menu(message, None)

# ====== Управление деталями (senior/admin) ======
@dp.message(Command("addpart"))
async def add_part_cmd(message: types.Message):
    """Добавить новую деталь в каталог"""
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    parts = message.text.split(maxsplit=5)
    
    if len(parts) < 5:
        await message.answer(
            "📋 <b>Формат команды:</b>\n\n"
            "<code>/addpart имя пластик время_печати деталей_на_столе [описание]</code>\n\n"
            "<b>Пример:</b>\n"
            "<code>/addpart 7 PLA 45 5 Корпус основной</code>\n"
            "<code>/addpart 10 ABS 60 4</code>\n\n"
            "Список деталей: /listparts",
            parse_mode="HTML"
        )
        return
    
    try:
        name = parts[1].strip()
        plastic_type = parts[2].strip()
        print_time = int(parts[3].strip())
        parts_per_table = int(parts[4].strip())
        description = parts[5].strip() if len(parts) > 5 else ""
        
        if print_time <= 0 or parts_per_table <= 0:
            await message.answer("❌ Время и количество должны быть > 0")
            return
        
        part_id = db.add_part(name, plastic_type, print_time, parts_per_table, description)
        
        await message.answer(
            f"✅ <b>Деталь добавлена!</b>\n\n"
            f"🆔 ID: {part_id}\n"
            f"📦 Имя: <b>{name}</b>\n"
            f"🧪 Пластик: {plastic_type}\n"
            f"⏱️ Время печати: {print_time} мин\n"
            f"📋 На столе: {parts_per_table} шт\n"
            f"📝 Описание: {description or '—'}",
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Неверный формат чисел")
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            await message.answer(f"❌ Деталь с именем '{name}' уже существует")
        else:
            await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("listparts"))
async def list_parts_cmd(message: types.Message):
    """Показать все детали"""
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала авторизуйтесь: /start")
        return
    
    parts = db.get_all_parts()
    
    if not parts:
        await message.answer("📦 Каталог деталей пуст.\nДобавьте через /addpart")
        return
    
    text = f"📦 <b>Каталог деталей ({len(parts)}):</b>\n\n"
    for p in parts:
        text += (
            f"<b>{p[1]}</b> | {p[2] or '?'} | "
            f"⏱️{p[3]}мин | 📋{p[4]}шт\n"
        )
    
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("updatepart"))
async def update_part_cmd(message: types.Message):
    """Обновить количество деталей на столе"""
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return
    
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Формат: <code>/updatepart имя_детали новое_кол-во</code>\n"
            "Пример: <code>/updatepart 7 6</code>",
            parse_mode="HTML"
        )
        return
    
    try:
        name = parts[1].strip()
        new_qty = int(parts[2].strip())
        
        if new_qty <= 0:
            await message.answer("❌ Количество должно быть > 0")
            return
        
        part = db.get_part_by_name(name)
        if not part:
            await message.answer(f"❌ Деталь '{name}' не найдена")
            return
        
        old_qty = part[4]
        db.update_part_table_qty(name, new_qty)
        
        await message.answer(
            f"✅ Обновлено для детали <b>{name}</b>:\n"
            f"Было: {old_qty} шт на столе\n"
            f"Стало: <b>{new_qty}</b> шт на столе",
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Количество должно быть числом")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ====== Запуск ======
async def main():
    try:
        logger.info("Бот запущен...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")
        for senior_id in db.get_senior_operators():
            try:
                await bot.send_message(senior_id, f"🚨 Бот упал: {e}")
            except:
                pass


if __name__ == "__main__":
    asyncio.run(main())