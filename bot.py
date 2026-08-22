import asyncio
import csv
import logging
import os
import sys
import traceback
from datetime import datetime, date

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, TelegramObject
)

from database import Database

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

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Создайте файл .env с токеном бота.")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
db = Database()

ADMIN_IDS = [1173990828]


class ErrorHandlerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        try:
            return await handler(event, data)
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            logger.error(traceback.format_exc())

            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
                except Exception:
                    pass

                try:
                    if event.message:
                        await event.message.answer(
                            f"🚨 <b>Произошла ошибка</b>\n\n"
                            f"Действие: <code>{event.data}</code>\n"
                            f"Ошибка: <code>{str(e)[:200]}</code>",
                            parse_mode="HTML"
                        )
                except Exception:
                    pass
            return None


dp.update.outer_middleware(ErrorHandlerMiddleware())


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_senior_or_admin(user):
    return bool(user and (user[3] in ('senior', 'admin') or is_admin(user[0])))


def is_operator_or_higher(user):
    return bool(user and user[3] in ("operator", "senior", "admin"))


def has_permission(user, permission):
    if not user:
        return False
    if is_admin(user[0]):
        return True
    return db.has_permission(user, permission)


class AuthStates(StatesGroup):
    waiting_auth_method = State()
    waiting_username = State()
    waiting_fullname = State()


class BoxAddStates(StatesGroup):
    waiting_sector = State()
    waiting_shift = State()
    waiting_part = State()
    waiting_type = State()
    waiting_quantity = State()
    waiting_confirmation = State()


class BoxEditStates(StatesGroup):
    waiting_sector = State()
    waiting_shift = State()
    waiting_task = State()
    waiting_box = State()
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


def format_sector_report(sector_id, shift_date):
    tasks = db.get_all_tasks_for_sector(sector_id, shift_date)
    sector_name = db.get_sector_name(sector_id)

    if not tasks:
        return f"📋 <b>{sector_name}</b>\\nНет заданий на {shift_date}."

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    report = f"📋 <b>{sector_name}</b>\\n🕒 {now}\\n📅 {shift_date}\\n\\n"

    for task in tasks:
        task_id = task[0]
        part_number = task[2]
        shift_time = task[3]
        printers = task[4]
        launches = task[5]
        plan = task[7]
        status = task[9] if len(task) > 9 else ("🔒" if task[8] else "🟢")

        stats = db.get_task_statistics(task_id)
        total_good, total_defect, box_count = stats
        total_printed = total_good + total_defect
        percentage = (total_good / plan * 100) if plan > 0 else 0

        remaining = plan - total_good
        if remaining > 0:
            remaining_text = f"⬇️ не хватает: <b>{remaining}</b>"
        elif remaining == 0:
            remaining_text = "✅ <b>план выполнен!</b>"
        else:
            remaining_text = f"⬆️ перевыполнение: <b>{abs(remaining)}</b>"

        status_text = {
            "open": "🟡 открыто",
            "active": "🟢 в работе",
            "locked": "🔒 заблокировано",
            "closed": "⛔ закрыто",
        }.get(status, "❔ неизвестно")

        report += (
            f"📦 <b>Деталь {part_number}</b> — {shift_time} | {status_text}\\n"
            f"🆔 ПЗ: <code>{task_id}</code>\\n"
            f"🖨️ {printers} принтеров | съемов {box_count}/{launches}\\n"
            f"📊 факт/план: <b>{total_good}/{plan}</b> ({percentage:.0f}%)\\n"
            f"{remaining_text}\\n"
            f"❌ брак: <b>{total_defect}</b>\\n"
            f"📦 всего напечатано: {total_printed}\\n\\n"
        )

    return report


def task_fields(task):
    return {
        "id": task[0],
        "sector_id": task[1],
        "part_number": task[2],
        "shift_time": task[3],
        "printers_count": task[4],
        "launches_count": task[5],
        "parts_per_table": task[6],
        "total_plan": task[7],
        "shift_date": task[8],
        "status": task[9] if len(task) > 9 else None,
        "is_locked": task[10] if len(task) > 10 else 0,
    }


async def notify_seniors(message_text):
    for senior_id in db.get_senior_operators():
        try:
            await bot.send_message(senior_id, message_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка уведомления senior {senior_id}: {e}")


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
            if not active_shift and db.sector_exists(user[4]):
                db.open_shift(message.from_user.id, user[4])
                await message.answer(
                    f"✅ Смена автоматически открыта!\n"
                    f"Участок: {db.get_sector_name(user[4])}"
                )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Ввести ФИО", callback_data="auth_name")],
            [InlineKeyboardButton(text="🔑 Ввести никнейм", callback_data="auth_username")]
        ])
        await state.set_state(AuthStates.waiting_auth_method)
        await message.answer(
            "🔐 <b>Вы не найдены в системе</b>\n\nВыберите способ авторизации:",
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
    username = (message.text or "").strip().lstrip("@")
    user = db.get_user_by_username(username)

    if not user:
        await message.answer("❌ Никнейм не найден. Попробуйте через ФИО (/start).")
        return

    tg_username = message.from_user.username or f"id_{message.from_user.id}"
    db.bind_user_to_telegram(username, message.from_user.id, tg_username)
    user = db.get_user_by_telegram_id(message.from_user.id)
    await state.clear()

    if user[3] == 'operator' and user[4] and db.sector_exists(user[4]):
        db.open_shift(message.from_user.id, user[4])
        await message.answer(
            f"✅ Авторизация успешна!\nРоль: {user[3]}\n"
            f"Участок: {db.get_sector_name(user[4])}\n\nИспользуйте /menu"
        )
    else:
        await message.answer(
            f"✅ Авторизация успешна!\nРоль: {user[3]}\n\nИспользуйте /menu"
        )


@dp.message(AuthStates.waiting_fullname)
async def process_auth_by_name(message: types.Message, state: FSMContext):
    full_name = (message.text or "").strip()

    if not db.bind_user_by_fullname(
        full_name,
        message.from_user.id,
        message.from_user.username or f"id_{message.from_user.id}"
    ):
        await message.answer(
            "❌ ФИО не найдено в системе.\n\n"
            "Проверьте правильность написания или обратитесь к старшему оператору."
        )
        return

    user = db.get_user_by_telegram_id(message.from_user.id)
    await state.clear()

    if user[3] == 'operator' and user[4] and db.sector_exists(user[4]):
        db.open_shift(message.from_user.id, user[4])
        await message.answer(
            f"✅ Авторизация успешна!\nРоль: {user[3]}\n"
            f"Участок: {db.get_sector_name(user[4])}\n\nИспользуйте /menu"
        )
    else:
        await message.answer(
            f"✅ Авторизация успешна!\nРоль: {user[3]}\n\nИспользуйте /menu"
        )


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext = None):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала авторизуйтесь: /start")
        return

    if is_admin(message.from_user.id) or user[3] == "admin":
        rows = [
            ["📋 Задание", "📊 Отчет по участку"],
            ["➕ Добавить коробку", "✏️ Редактировать"],
            ["➕ Создать ПЗ", "📍 Все участки"],
            ["📄 Сформировать отчет"],
            ["👥 Пользователи", "📊 Статистика"]
        ]
    elif user[3] == "senior":
        rows = [
            ["📋 Задание", "📊 Отчет по участку"],
            ["➕ Добавить коробку", "✏️ Редактировать"],
            ["➕ Создать ПЗ", "📍 Все участки"],
            ["📄 Сформировать отчет"]
        ]
    else:
        rows = [
            ["📋 Задание", "📊 Отчет по участку"],
            ["➕ Добавить коробку", "✏️ Редактировать"],
            ["🔄 Закрыть смену"]
        ]

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=x) for x in row] for row in rows],
        resize_keyboard=True
    )
    await message.answer("Главное меню:", reply_markup=keyboard)


@dp.message(F.text.in_(["📋 Задание", "📊 Отчет по участку"]))
async def show_report(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала авторизуйтесь: /start")
        return

    if user[3] == "operator":
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
    sector_id = int(callback.data.split("_", 1)[1])
    await callback.message.edit_text(
        format_sector_report(sector_id, date.today().isoformat()),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(F.text == "➕ Добавить коробку")
async def start_box_add(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not has_permission(user, "box_add"):
        await message.answer("🚫 Недостаточно прав.")
        return

    if user[3] == "operator":
        shift = db.get_active_shift_for_operator(message.from_user.id)
        if not shift:
            await message.answer("У вас нет активной смены. Используйте /openshift")
            return
        sector_id = shift[2]
        await state.update_data(sector_id=sector_id)
        await state.set_state(BoxAddStates.waiting_shift)
        await message.answer(
            "Выберите смену ПЗ:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="☀️ День", callback_data="ba_shift_день"),
                 InlineKeyboardButton(text="🌙 Ночь", callback_data="ba_shift_ночь")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]
            ])
        )
        return

    sectors = db.get_all_sectors()
    await state.set_state(BoxAddStates.waiting_sector)
    await message.answer(
        "Выберите участок:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=s[1], callback_data=f"ba_sector_{s[0]}")]
            for s in sectors
        ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]])
    )


@dp.callback_query(BoxAddStates.waiting_sector, F.data.startswith("ba_sector_"))
async def box_add_sector(callback: types.CallbackQuery, state: FSMContext):
    sector_id = int(callback.data.split("_")[-1])
    await state.update_data(sector_id=sector_id)
    await state.set_state(BoxAddStates.waiting_shift)
    await callback.message.edit_text(
        f"Участок: <b>{db.get_sector_name(sector_id)}</b>\\nВыберите смену ПЗ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="☀️ День", callback_data="ba_shift_день"),
             InlineKeyboardButton(text="🌙 Ночь", callback_data="ba_shift_ночь")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(BoxAddStates.waiting_shift, F.data.startswith("ba_shift_"))
async def box_add_shift(callback: types.CallbackQuery, state: FSMContext):
    shift_time = callback.data.split("ba_shift_", 1)[1]
    data = await state.get_data()
    sector_id = data["sector_id"]
    tasks = db.get_tasks_for_sector_and_shift(
        sector_id, date.today().isoformat(), shift_time
    )
    if not tasks:
        await callback.answer("❌ Для этой смены ПЗ нет.", show_alert=True)
        return

    await state.update_data(shift_time=shift_time)
    await state.set_state(BoxAddStates.waiting_part)

    buttons = [
        [InlineKeyboardButton(
            text=f"Деталь {t[2]} | ПЗ #{t[0]}",
            callback_data=f"ba_task_{t[0]}"
        )]
        for t in tasks
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")])
    await callback.message.edit_text(
        "Выберите ПЗ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@dp.callback_query(BoxAddStates.waiting_part, F.data.startswith("ba_task_"))
async def box_add_task(callback: types.CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    task = db.get_task(task_id)
    if not task:
        await callback.answer("❌ ПЗ не найдено", show_alert=True)
        return

    await state.update_data(
        task_id=task_id,
        part_number=task[2],
        shift_time=task[3]
    )
    await state.set_state(BoxAddStates.waiting_type)
    await callback.message.edit_text(
        f"ПЗ #{task_id} | Деталь <b>{task[2]}</b> ({task[3]})\\nВыберите тип:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Годен", callback_data="bt_good"),
             InlineKeyboardButton(text="❌ Брак", callback_data="bt_defect")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(BoxAddStates.waiting_part)
async def process_part(message: types.Message, state: FSMContext):
    await message.answer("Выберите ПЗ кнопкой выше.")


@dp.callback_query(BoxAddStates.waiting_type, F.data.in_(["bt_good", "bt_defect"]))
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    box_type = "good" if callback.data == "bt_good" else "defect"
    await state.update_data(box_type=box_type, quantity=0)
    await state.set_state(BoxAddStates.waiting_quantity)
    await callback.answer()
    await show_qty_keyboard(callback.message, state, 0)


async def show_qty_keyboard(msg_obj, state: FSMContext, quantity):
    data = await state.get_data()
    type_text = "годен" if data.get("box_type") == "good" else "брак"
    part_number = data.get("part_number", "?")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="+1", callback_data="q_+1"),
         InlineKeyboardButton(text="-1", callback_data="q_-1"),
         InlineKeyboardButton(text="+5", callback_data="q_+5")],
        [InlineKeyboardButton(text="+10", callback_data="q_+10"),
         InlineKeyboardButton(text="+50", callback_data="q_+50"),
         InlineKeyboardButton(text="+100", callback_data="q_+100")],
        [InlineKeyboardButton(text="📋 Стол +", callback_data="t_+1"),
         InlineKeyboardButton(text="📋 Стол -", callback_data="t_-1")],
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="q_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_box")]
    ])

    text = (
        f"ПЗ #{data.get('task_id', '?')} | Деталь <b>{part_number}</b> ({type_text})\\n"
        f"Количество: <b>{quantity}</b>\\n\\nВведите число или используйте кнопки:"
    )

    try:
        if isinstance(msg_obj, types.Message):
            await msg_obj.answer(text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg_obj.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Ошибка show_qty_keyboard: {e}")


@dp.callback_query(BoxAddStates.waiting_quantity, F.data.startswith("q_"))
async def process_qty_button(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quantity = int(data.get("quantity", 0))
    action = callback.data.split("_", 1)[1]

    if action == "confirm":
        if quantity <= 0:
            await callback.answer("Количество должно быть > 0", show_alert=True)
            return
        await callback.answer()
        await show_confirmation(callback.message, state)
        return

    if action.startswith("+"):
        quantity += int(action[1:])
    elif action.startswith("-"):
        quantity = max(0, quantity - int(action[1:]))

    await state.update_data(quantity=quantity)
    await callback.answer()
    await show_qty_keyboard(callback.message, state, quantity)


@dp.callback_query(BoxAddStates.waiting_quantity, F.data.startswith("t_"))
async def process_table_button(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quantity = int(data.get("quantity", 0))
    part_name = data.get("part_number")
    part_info = db.get_part_by_name(part_name)

    if not part_info:
        await callback.answer("❌ Деталь не найдена в базе", show_alert=True)
        return

    parts_per_table = part_info[4]
    action = callback.data.split("_", 1)[1]

    if action.startswith("+"):
        quantity += parts_per_table * int(action[1:])
    elif action.startswith("-"):
        quantity = max(0, quantity - parts_per_table * int(action[1:]))

    await state.update_data(quantity=quantity)
    await callback.answer()
    await show_qty_keyboard(callback.message, state, quantity)


@dp.message(BoxAddStates.waiting_quantity)
async def process_qty_text(message: types.Message, state: FSMContext):
    try:
        qty = int((message.text or "").strip())
        if qty <= 0:
            await message.answer("Количество должно быть > 0.")
            return
        await state.update_data(quantity=qty)
        await show_confirmation(message, state)
    except ValueError:
        await message.answer("Введите целое число.")


async def show_confirmation(msg_obj, state: FSMContext):
    data = await state.get_data()
    type_text = "годен" if data.get("box_type") == "good" else "брак"
    await state.set_state(BoxAddStates.waiting_confirmation)

    text = (
        f"<b>Подтвердите создание коробки</b>\\n"
        f"ПЗ: #{data.get('task_id')}\\n"
        f"Деталь: {data.get('part_number')} ({data.get('shift_time')})\\n"
        f"Тип: {type_text}\\n"
        f"Количество: {data.get('quantity')}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data="cf_yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="cf_no")
    ]])
    if isinstance(msg_obj, types.Message):
        await msg_obj.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg_obj.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(BoxAddStates.waiting_confirmation, F.data == "cf_yes")
async def confirm_box_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = db.get_user_by_telegram_id(callback.from_user.id)
    task = db.get_task(data.get("task_id"))

    if not task:
        await callback.message.edit_text("❌ ПЗ не найдено")
        await state.clear()
        await callback.answer()
        return

    try:
        good = data["quantity"] if data["box_type"] == "good" else 0
        defect = data["quantity"] if data["box_type"] == "defect" else 0

        # IMPORTANT: no lock_task() here.
        # First box activates the task; locking happens on close.
        box_id = db.add_box(
            task[0], task[2], None, good, defect,
            user_id=callback.from_user.id,
            details=f"Добавление через Telegram, смена ПЗ: {task[3]}"
        )
        box = db.get_box(box_id)
        box_number = box[3]

        stats = db.get_task_statistics(task[0])
        total_fact, total_defect, _ = stats
        plan = task[7]
        pct = (total_fact / plan * 100) if plan > 0 else 0
        type_text = "годен" if data["box_type"] == "good" else "брак"

        report_text = (
            f"✅ <b>Коробка #{box_number} добавлена!</b>\\n"
            f"ПЗ #{task[0]} | Деталь {task[2]} ({task[3]})\\n"
            f"{type_text}: {data['quantity']}\\n\\n"
            f"📊 <b>Факт/План:</b> {total_fact}/{plan} ({pct:.0f}%)\\n"
            f"❌ Брак: {total_defect}"
        )
        await callback.message.edit_text(report_text, parse_mode="HTML")

        await notify_seniors(
            f"🔔 <b>{'Старший' if user[3] != 'operator' else 'Оператор'} {user[2]}</b> "
            f"добавил коробку #{box_number}\\n"
            f"Участок: {db.get_sector_name(task[1])}\\n"
            f"ПЗ: #{task[0]} | Деталь {task[2]} ({task[3]})\\n"
            f"{type_text}: {data['quantity']}\\n"
            f"Факт/План: {total_fact}/{plan} ({pct:.0f}%)"
        )

    except Exception as e:
        await callback.message.edit_text(f"❌ Не удалось сохранить коробку: {e}")
        logger.exception("Ошибка сохранения коробки")
    finally:
        await state.clear()
        await callback.answer()


@dp.callback_query(BoxAddStates.waiting_confirmation, F.data == "cf_no")
async def confirm_box_no(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BoxAddStates.waiting_part)
    data = await state.get_data()
    tasks = db.get_tasks_for_sector_and_shift(
        data["sector_id"], date.today().isoformat(), data.get("shift_time")
    )
    buttons = [
        [InlineKeyboardButton(text=f"Деталь {t[2]} | ПЗ #{t[0]}", callback_data=f"ba_task_{t[0]}")]
        for t in tasks
    ]
    await callback.message.edit_text(
        "Выберите ПЗ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel_box")
async def cancel_box(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Операция отменена.")
    await callback.answer()


# ===================== EDIT BOXES =====================

@dp.message(F.text == "✏️ Редактировать")
@dp.message(Command("box_edit"))
async def box_edit(message: types.Message, state: FSMContext):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not has_permission(user, "box_edit"):
        await message.answer("🚫 Недостаточно прав.")
        return

    if user[3] == "operator":
        shift = db.get_active_shift_for_operator(message.from_user.id)
        if not shift:
            await message.answer("У вас нет активной смены.")
            return
        await state.update_data(sector_id=shift[2])
    else:
        sectors = db.get_all_sectors()
        await state.set_state(BoxEditStates.waiting_sector)
        await message.answer(
            "Выберите участок:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=s[1], callback_data=f"be_sector_{s[0]}")]
                for s in sectors
            ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]])
        )
        return

    await state.set_state(BoxEditStates.waiting_shift)
    await message.answer(
        "Выберите смену ПЗ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="☀️ День", callback_data="be_shift_день"),
             InlineKeyboardButton(text="🌙 Ночь", callback_data="be_shift_ночь")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]
        ])
    )


@dp.callback_query(BoxEditStates.waiting_sector, F.data.startswith("be_sector_"))
async def edit_sector_select(callback: types.CallbackQuery, state: FSMContext):
    sector_id = int(callback.data.split("_")[-1])
    await state.update_data(sector_id=sector_id)
    await state.set_state(BoxEditStates.waiting_shift)
    await callback.message.edit_text(
        "Выберите смену ПЗ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="☀️ День", callback_data="be_shift_день"),
             InlineKeyboardButton(text="🌙 Ночь", callback_data="be_shift_ночь")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]
        ])
    )
    await callback.answer()


@dp.callback_query(BoxEditStates.waiting_shift, F.data.startswith("be_shift_"))
async def edit_shift_select(callback: types.CallbackQuery, state: FSMContext):
    shift_time = callback.data.split("be_shift_", 1)[1]
    data = await state.get_data()
    tasks = db.get_tasks_for_sector_and_shift(
        data["sector_id"], date.today().isoformat(), shift_time
    )
    if not tasks:
        await callback.answer("❌ ПЗ на эту смену не найдены", show_alert=True)
        return

    await state.update_data(shift_time=shift_time)
    await state.set_state(BoxEditStates.waiting_task)
    buttons = [
        [InlineKeyboardButton(
            text=f"Деталь {t[2]} | ПЗ #{t[0]}",
            callback_data=f"be_task_{t[0]}"
        )]
        for t in tasks
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")])
    await callback.message.edit_text(
        "Выберите ПЗ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@dp.callback_query(BoxEditStates.waiting_task, F.data.startswith("be_task_"))
async def edit_task_select(callback: types.CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    task = db.get_task(task_id)
    if not task:
        await callback.answer("❌ ПЗ не найдено", show_alert=True)
        return

    boxes = db.get_boxes_for_task(task_id)
    buttons = [
        [InlineKeyboardButton(
            text=f"📦 #{b[3]} | годен {b[4]} | брак {b[5]}",
            callback_data=f"be_box_{b[0]}"
        )]
        for b in boxes
    ]
    buttons.append([
        InlineKeyboardButton(text="➕ Новая коробка", callback_data=f"be_new_{task_id}")
    ])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")])

    await state.update_data(task_id=task_id)
    await state.set_state(BoxEditStates.waiting_box)
    await callback.message.edit_text(
        f"ПЗ #{task_id} | Деталь {task[2]} ({task[3]})\\nВыберите коробку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@dp.callback_query(BoxEditStates.waiting_box, F.data.startswith("be_box_"))
async def edit_box_select(callback: types.CallbackQuery, state: FSMContext):
    box_id = int(callback.data.split("_")[-1])
    box = db.get_box(box_id)
    if not box:
        await callback.answer("❌ Коробка не найдена", show_alert=True)
        return

    user = db.get_user_by_telegram_id(callback.from_user.id)
    if user[3] == "operator":
        task = db.get_task(box[1])
        if not task or task[1] != user[4] or task[8] != date.today().isoformat():
            await callback.answer("🚫 Эта коробка не принадлежит вашему участку/дню.", show_alert=True)
            return

    await state.update_data(
        box_id=box_id,
        task_id=box[1],
        part_number=box[2],
        box_type="good",
        original_good=box[4],
        original_defect=box[5],
        is_new=False
    )
    await state.set_state(BoxEditStates.waiting_type)
    await callback.message.edit_text(
        f"📦 Коробка #{box[3]}\\n"
        f"Сейчас: годен {box[4]}, брак {box[5]}\\n\\n"
        f"Что изменить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Годен", callback_data="be_type_good"),
             InlineKeyboardButton(text="❌ Брак", callback_data="be_type_defect")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="be_delete")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]
        ])
    )
    await callback.answer()


@dp.callback_query(BoxEditStates.waiting_box, F.data.startswith("be_new_"))
async def edit_new_box(callback: types.CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[-1])
    task = db.get_task(task_id)
    if not task:
        await callback.answer("❌ ПЗ не найдено", show_alert=True)
        return

    await state.update_data(
        task_id=task_id,
        box_id=None,
        part_number=task[2],
        box_type="defect",
        original_good=0,
        original_defect=0,
        is_new=True
    )
    await state.set_state(BoxEditStates.waiting_type)
    await callback.message.edit_text(
        f"➕ Новая коробка для ПЗ #{task_id}\\n"
        f"Деталь: {task[2]} ({task[3]})\\n"
        f"Выберите тип:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Годен", callback_data="be_type_good"),
             InlineKeyboardButton(text="❌ Брак", callback_data="be_type_defect")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]
        ])
    )
    await callback.answer()


@dp.callback_query(BoxEditStates.waiting_type, F.data.in_(["be_type_good", "be_type_defect"]))
async def edit_type_select(callback: types.CallbackQuery, state: FSMContext):
    box_type = "good" if callback.data == "be_type_good" else "defect"
    await state.update_data(box_type=box_type, quantity=0)
    await state.set_state(BoxEditStates.waiting_quantity)
    await callback.answer()
    await show_edit_qty_keyboard(callback.message, state, 0)


async def show_edit_qty_keyboard(msg_obj, state: FSMContext, quantity):
    data = await state.get_data()
    text = (
        f"ПЗ #{data.get('task_id')} | Деталь <b>{data.get('part_number')}</b>\\n"
        f"Новое значение ({'годен' if data.get('box_type') == 'good' else 'брак'}): "
        f"<b>{quantity}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="+1", callback_data="eq_+1"),
         InlineKeyboardButton(text="-1", callback_data="eq_-1"),
         InlineKeyboardButton(text="+10", callback_data="eq_+10")],
        [InlineKeyboardButton(text="+50", callback_data="eq_+50"),
         InlineKeyboardButton(text="+100", callback_data="eq_+100")],
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="eq_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]
    ])
    if isinstance(msg_obj, types.Message):
        await msg_obj.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg_obj.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(BoxEditStates.waiting_quantity, F.data.startswith("eq_"))
async def edit_qty_button(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quantity = int(data.get("quantity", 0))
    action = callback.data.split("_", 1)[1]

    if action == "confirm":
        if quantity < 0:
            await callback.answer("Количество не может быть отрицательным", show_alert=True)
            return
        await state.set_state(BoxEditStates.waiting_confirmation)
        await callback.message.edit_text(
            f"<b>Подтвердить изменение?</b>\\n"
            f"ПЗ #{data.get('task_id')} | Деталь {data.get('part_number')}\\n"
            f"{'Годен' if data.get('box_type') == 'good' else 'Брак'}: {quantity}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="be_confirm"),
                InlineKeyboardButton(text="❌ Нет", callback_data="cancel_edit")
            ]]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if action.startswith("+"):
        quantity += int(action[1:])
    elif action.startswith("-"):
        quantity = max(0, quantity - int(action[1:]))

    await state.update_data(quantity=quantity)
    await callback.answer()
    await show_edit_qty_keyboard(callback.message, state, quantity)


@dp.message(BoxEditStates.waiting_quantity)
async def edit_qty_text(message: types.Message, state: FSMContext):
    try:
        qty = int((message.text or "").strip())
        if qty < 0:
            await message.answer("Количество не может быть отрицательным.")
            return
        await state.update_data(quantity=qty)
        await state.set_state(BoxEditStates.waiting_confirmation)
        data = await state.get_data()
        await message.answer(
            f"<b>Подтвердить изменение?</b>\\n"
            f"ПЗ #{data.get('task_id')} | {data.get('part_number')}\\n"
            f"{'Годен' if data.get('box_type') == 'good' else 'Брак'}: {qty}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="be_confirm"),
                InlineKeyboardButton(text="❌ Нет", callback_data="cancel_edit")
            ]]),
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("Введите целое число.")


@dp.callback_query(BoxEditStates.waiting_confirmation, F.data == "be_confirm")
async def edit_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = db.get_user_by_telegram_id(callback.from_user.id)
    task = db.get_task(data.get("task_id"))
    if not task:
        await callback.message.edit_text("❌ ПЗ не найдено")
        await state.clear()
        await callback.answer()
        return

    new_good = data["quantity"] if data["box_type"] == "good" else 0
    new_defect = data["quantity"] if data["box_type"] == "defect" else 0
    allow_locked = user[3] in ("senior", "admin") or is_admin(user[0])

    try:
        if data.get("is_new"):
            box_id = db.add_box(
                task[0], task[2], None, new_good, new_defect,
                user_id=callback.from_user.id,
                details="Создание коробки через режим редактирования",
                allow_locked=allow_locked
            )
        else:
            box_id = data["box_id"]
            db.update_box(
                box_id,
                good_qty=new_good,
                defect_qty=new_defect,
                user_id=callback.from_user.id,
                details="Редактирование коробки через Telegram",
                allow_locked=allow_locked
            )

        stats = db.get_task_statistics(task[0])
        await callback.message.edit_text(
            f"✅ Сохранено.\\n"
            f"ПЗ #{task[0]} | Деталь {task[2]} ({task[3]})\\n"
            f"Годен: {stats[0]}\\nБрак: {stats[1]}\\nКоробок: {stats[2]}",
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Не удалось сохранить: {e}")
        logger.exception("Ошибка редактирования коробки")
    finally:
        await state.clear()
        await callback.answer()


@dp.callback_query(BoxEditStates.waiting_box, F.data == "be_delete")
async def edit_delete_box(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = db.get_user_by_telegram_id(callback.from_user.id)
    try:
        allow_locked = user[3] in ("senior", "admin") or is_admin(user[0])
        db.delete_box(
            data["box_id"],
            user_id=callback.from_user.id,
            details="Удаление коробки через Telegram",
            allow_locked=allow_locked
        )
        await callback.message.edit_text("🗑 Коробка удалена. История изменения сохранена.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Не удалось удалить: {e}")
    finally:
        await state.clear()
        await callback.answer()


@dp.callback_query(F.data == "cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Редактирование отменено.")
    await callback.answer()


@dp.message(F.text == "➕ Создать ПЗ")
async def start_create_task(message: types.Message, state: FSMContext):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return

    sectors = db.get_all_sectors()
    buttons = [
        [InlineKeyboardButton(text=s[1], callback_data=f"ts_{s[0]}")]
        for s in sectors
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_task")])

    await state.set_state(TaskCreateStates.waiting_sector)
    await message.answer(
        "Выберите участок:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(TaskCreateStates.waiting_sector, F.data.startswith("ts_"))
async def create_task_sector(callback: types.CallbackQuery, state: FSMContext):
    sector_id = int(callback.data.split("_", 1)[1])
    await state.update_data(sector_id=sector_id)
    await state.set_state(TaskCreateStates.waiting_part)
    await callback.message.edit_text("Введите номер детали:")
    await callback.answer()


@dp.message(TaskCreateStates.waiting_part)
async def create_task_part(message: types.Message, state: FSMContext):
    await state.update_data(part_number=(message.text or "").strip())
    await state.set_state(TaskCreateStates.waiting_shift_time)
    await message.answer(
        "Выберите смену:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="☀️ День", callback_data="st_день"),
            InlineKeyboardButton(text="🌙 Ночь", callback_data="st_ночь")
        ]])
    )


@dp.callback_query(TaskCreateStates.waiting_shift_time, F.data.startswith("st_"))
async def create_task_shift_time(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(shift_time=callback.data.split("_", 1)[1])
    await state.set_state(TaskCreateStates.waiting_printers)
    await callback.message.edit_text("Введите количество принтеров:")
    await callback.answer()


@dp.message(TaskCreateStates.waiting_printers)
async def create_task_printers(message: types.Message, state: FSMContext):
    try:
        n = int((message.text or "").strip())
        if n <= 0:
            raise ValueError
        await state.update_data(printers_count=n)
        await state.set_state(TaskCreateStates.waiting_launches)
        await message.answer("Введите количество запусков:")
    except ValueError:
        await message.answer("Введите положительное целое число.")


@dp.message(TaskCreateStates.waiting_launches)
async def create_task_launches(message: types.Message, state: FSMContext):
    try:
        n = int((message.text or "").strip())
        if n <= 0:
            raise ValueError
        await state.update_data(launches_count=n)
        await state.set_state(TaskCreateStates.waiting_parts_per_table)
        await message.answer("Введите количество деталей на столе:")
    except ValueError:
        await message.answer("Введите положительное целое число.")


@dp.message(TaskCreateStates.waiting_parts_per_table)
async def create_task_parts(message: types.Message, state: FSMContext):
    try:
        n = int((message.text or "").strip())
        if n <= 0:
            raise ValueError
        await state.update_data(parts_per_table=n)
        data = await state.get_data()
        total = data['printers_count'] * data['launches_count'] * data['parts_per_table']

        await state.set_state(TaskCreateStates.waiting_confirmation)
        await message.answer(
            f"<b>Подтвердите ПЗ:</b>\n"
            f"Участок: {db.get_sector_name(data['sector_id'])}\n"
            f"Деталь: {data['part_number']} ({data['shift_time']})\n"
            f"Принтеры: {data['printers_count']}\n"
            f"Запуски: {data['launches_count']}\n"
            f"Деталей/стол: {data['parts_per_table']}\n"
            f"План: <b>{total}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="tc_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="tc_no")
            ]]),
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("Введите положительное целое число.")


@dp.callback_query(TaskCreateStates.waiting_confirmation, F.data == "tc_yes")
async def create_task_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    existing = db.get_tasks_for_sector_and_shift(
        data['sector_id'],
        date.today().isoformat(),
        data['shift_time']
    )
    duplicate = next(
        (t for t in existing if str(t[2]) == str(data['part_number'])),
        None
    )
    if duplicate:
        await callback.message.edit_text(
            f"⚠️ ПЗ для детали <b>{data['part_number']}</b> "
            f"на смену <b>{data['shift_time']}</b> уже существует: "
            f"#{duplicate[0]}",
            parse_mode="HTML"
        )
        await state.clear()
        await callback.answer()
        return

    task_id = db.create_task(
        data['sector_id'], data['part_number'], data['shift_time'],
        data['printers_count'], data['launches_count'],
        data['parts_per_table'], date.today().isoformat()
    )
    total = data['printers_count'] * data['launches_count'] * data['parts_per_table']
    await callback.message.edit_text(f"✅ ПЗ создано! ID: {task_id}, план: {total}")
    await state.clear()
    await callback.answer()


@dp.callback_query(TaskCreateStates.waiting_confirmation, F.data == "tc_no")
async def create_task_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()


@dp.callback_query(F.data == "cancel_task")
async def cancel_task(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Создание ПЗ отменено.")
    await callback.answer()


@dp.message(F.text == "📍 Все участки")
async def show_all_sectors(message: types.Message, state: FSMContext):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return

    sectors = db.get_all_sectors()
    await message.answer(
        "Выберите участок:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=s[1], callback_data=f"si_{s[0]}")]
            for s in sectors
        ])
    )


@dp.callback_query(F.data.startswith("si_"))
async def sector_info(callback: types.CallbackQuery):
    sector_id = int(callback.data.split("_", 1)[1])
    await callback.message.edit_text(
        format_sector_report(sector_id, date.today().isoformat()),
        parse_mode="HTML"
    )
    await callback.answer()


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

    await message.answer(
        "Выберите период:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📅 Сегодня", callback_data="rp_today"),
            InlineKeyboardButton(text="📊 Все данные", callback_data="rp_all")
        ]])
    )


@dp.callback_query(F.data.in_(["rp_today", "rp_all"]))
async def generate_report_callback(callback: types.CallbackQuery):
    report_date = date.today().isoformat() if callback.data == "rp_today" else None
    filename = db.export_report_to_csv(report_date, f"report_{report_date or 'all'}.csv")

    try:
        with open(filename, 'rb') as f:
            await bot.send_document(
                callback.from_user.id,
                types.BufferedInputFile(f.read(), filename=os.path.basename(filename))
            )
    finally:
        if os.path.exists(filename):
            os.remove(filename)

    await callback.message.edit_text("✅ Отчет отправлен!")
    await callback.answer()


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

    await message.answer(
        "Закрыть смену?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да", callback_data="cs_yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="cs_no")
        ]])
    )


@dp.callback_query(F.data == "cs_yes")
async def confirm_close_shift(callback: types.CallbackQuery):
    user = db.get_user_by_telegram_id(callback.from_user.id)
    shift = db.get_active_shift_for_operator(callback.from_user.id)

    if not shift:
        await callback.message.edit_text("❌ Нет активной смены")
        await callback.answer()
        return

    db.close_tasks_for_sector_date(shift[2], date.today().isoformat())
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
        text += f"\n🏭 <b>Участок:</b> {sector_name}\n📋 <b>Роль в БД:</b> {user[3]}"

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
            "<b>Без Telegram ID:</b>\n"
            "<code>/adduser operator 16 Иванов Иван</code>",
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

        if role not in ('operator', 'senior', 'admin'):
            await message.answer("❌ Роль: 'operator', 'senior' или 'admin'")
            return

        if sector_id is not None and not db.sector_exists(sector_id):
            sectors = db.get_all_sectors()
            sector_list = "\n".join(f"  ID {s[0]}: {s[1]}" for s in sectors)
            await message.answer(
                f"❌ Участок с ID {sector_id} не существует!\n\n"
                f"Доступные участки:\n{sector_list}"
            )
            return

        if tg_id:
            db.add_user(tg_id, username, full_name, role, sector_id)
        else:
            db.add_user_without_tg(username, full_name, role, sector_id)

        await message.answer(
            f"✅ <b>Пользователь добавлен!</b>\n\n"
            f"📋 Имя: {full_name}\n"
            f"🎭 Роль: {role}\n"
            f"🏭 Участок: {db.get_sector_name(sector_id) if sector_id else 'не указан'}\n"
            f"🔑 {'ID: ' + str(tg_id) if tg_id else '⚠️ Без TG ID'}",
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
            "📋 Формат:\n<code>/add_sector Участок 16</code>\n"
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
                c.execute("INSERT INTO sectors (id, name) VALUES (?, ?)", (sector_id, sector_name))
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
            c.execute(
                "SELECT COUNT(*) FROM users WHERE sector_id = ? AND role = 'operator'",
                (s[0],)
            )
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
        await message.answer("Формат: <code>/change_sector 123456789 16</code>", parse_mode="HTML")
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
            c.execute(
                "UPDATE users SET sector_id = ? WHERE telegram_id = ?",
                (new_sector_id, telegram_id)
            )
            conn.commit()

        await message.answer(
            f"✅ Участок изменен!\nПользователь: {target_user[2]}\n"
            f"Новый участок: {db.get_sector_name(new_sector_id)}"
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
        await message.answer(f"⚠️ Уже есть активная смена: {db.get_sector_name(active_shift[2])}")
        return

    if not user[4] or not db.sector_exists(user[4]):
        await message.answer("❌ У вас нет участка.")
        return

    db.open_shift(message.from_user.id, user[4])
    await message.answer(f"✅ Смена открыта!\nУчасток: {db.get_sector_name(user[4])}")


@dp.message(Command("checkshift"))
async def check_shift_cmd(message: types.Message):
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала авторизуйтесь: /start")
        return

    active_shift = db.get_active_shift_for_operator(message.from_user.id)
    if active_shift:
        await message.answer(
            f"✅ Активная смена\nУчасток: {db.get_sector_name(active_shift[2])}\n"
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

    for tg_id, name, sector_id in operators:
        if not tg_id or not sector_id or not db.sector_exists(sector_id):
            continue

        if not db.get_active_shift_for_operator(tg_id):
            db.open_shift(tg_id, sector_id)
            results.append(f"✅ {name} → {db.get_sector_name(sector_id)}")
            fixed_count += 1
        else:
            results.append(f"➡️ {name} — уже есть смена")

    report = (
        f"📋 <b>Обработка завершена</b>\n\n"
        f"Всего операторов: {len(operators)}\n"
        f"Открыто смен: {fixed_count}\n\n"
        + ("\n".join(results) if results else "Нет операторов")
    )
    await message.answer(report, parse_mode="HTML")


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
        db.add_user(tg_id, f"admin_{tg_id}", full_name, 'admin', None)
        await message.answer(
            f"✅ <b>Назначен новый администратор!</b>\n"
            f"ID: <code>{tg_id}</code>\nИмя: {full_name}",
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
        text += f"• {name} (<code>{tg_id}</code>)" + (" ⭐" if tg_id in ADMIN_IDS else "") + "\n"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("allusers"))
async def list_all_users(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return

    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT telegram_id, full_name, role, sector_id "
            "FROM users WHERE is_active = 1 ORDER BY role, full_name"
        )
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
        text += f"• {name} (<code>{tg_id if tg_id else '—'}</code>){sector}\n"

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
        if tg_id in ADMIN_IDS or tg_id == message.from_user.id:
            await message.answer("❌ Нельзя удалить супер-администратора или самого себя")
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
        queries = {
            "total_users": "SELECT COUNT(*) FROM users WHERE is_active = 1",
            "operators": "SELECT COUNT(*) FROM users WHERE role = 'operator' AND is_active = 1",
            "seniors": "SELECT COUNT(*) FROM users WHERE role = 'senior' AND is_active = 1",
            "admins": "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1",
            "pending": "SELECT COUNT(*) FROM users WHERE telegram_id IS NULL AND is_active = 1",
            "sectors": "SELECT COUNT(*) FROM sectors",
            "tasks": "SELECT COUNT(*) FROM tasks",
            "boxes": "SELECT COUNT(*) FROM boxes",
            "open_shifts": "SELECT COUNT(*) FROM shifts WHERE status = 'open'",
        }
        values = {}
        for key, query in queries.items():
            c.execute(query)
            values[key] = c.fetchone()[0]

    db_path = getattr(db, "db_path", "production.db")
    db_size = os.path.getsize(db_path) / 1024 if os.path.exists(db_path) else 0

    stats_text = (
        f"📊 <b>Статистика системы</b>\n\n"
        f"👥 <b>Пользователи:</b> {values['total_users']}\n"
        f"   👑 Админов: {values['admins']}\n"
        f"   ⭐ Senior: {values['seniors']}\n"
        f"   👷 Операторов: {values['operators']}\n"
        f"   ⏳ Ждут входа: {values['pending']}\n\n"
        f"🏭 <b>Участков:</b> {values['sectors']}\n"
        f"📋 <b>Заданий:</b> {values['tasks']}\n"
        f"📦 <b>Коробок:</b> {values['boxes']}\n"
        f"🔄 <b>Открытых смен:</b> {values['open_shifts']}\n\n"
        f"💾 <b>Размер БД:</b> {db_size:.1f} KB\n"
        f"🔑 <b>Супер-админов:</b> {len(ADMIN_IDS)}"
    )
    await message.answer(stats_text, parse_mode="HTML")


@dp.message(Command("import"))
async def import_csv(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return

    await message.answer(
        "📥 <b>Массовый импорт из CSV</b>\n\n"
        "Отправьте CSV файл следующим сообщением.\n\n"
        "<b>Формат:</b>\n"
        "<code>telegram_id,username,full_name,role,sector_id</code>",
        parse_mode="HTML"
    )


@dp.message(F.document)
async def handle_csv_file(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return

    document = message.document
    filename = document.file_name or ""

    if not filename.lower().endswith(".csv"):
        await message.answer("❌ Нужен файл с расширением .csv")
        return

    file_path = f"import_{message.from_user.id}.csv"

    try:
        file = await bot.get_file(document.file_id)
        await bot.download_file(file.file_path, file_path)

        users_list = []
        with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)

            if not header:
                await message.answer("❌ Файл пустой")
                return

            for row in reader:
                row += [""] * max(0, 5 - len(row))
                if len(row) < 3:
                    continue

                tg_id = row[0].strip() or None
                username = row[1].strip() or None
                full_name = row[2].strip()
                role = row[3].strip() or "operator"
                sector_id = row[4].strip() or None

                if full_name:
                    users_list.append((tg_id, username, full_name, role, sector_id))

        if not users_list:
            await message.answer("❌ В файле нет валидных строк")
            return

        success, errors = db.bulk_import_users(users_list)

        report = (
            f"📊 <b>Результаты импорта</b>\n\n"
            f"✅ Успешно: <b>{success}</b>\n"
            f"❌ Ошибок: <b>{len(errors)}</b>\n"
        )
        if errors:
            report += "\n<b>Ошибки:</b>\n" + "\n".join(f"• {e}" for e in errors[:10])

        await message.answer(report, parse_mode="HTML")

    except Exception as e:
        logger.exception("Ошибка CSV импорта")
        await message.answer(f"❌ Ошибка при импорте: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


@dp.message(F.text == "👥 Пользователи")
async def show_users_menu(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Только для супер-администратора.")
        return

    await message.answer(
        "Управление пользователями:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📥 Импорт CSV"), KeyboardButton(text="📋 Все пользователи")],
                [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔙 Назад")]
            ],
            resize_keyboard=True
        )
    )


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
    await cmd_menu(message)


@dp.message(Command("addpart"))
async def add_part_cmd(message: types.Message):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return

    parts = message.text.split(maxsplit=5)
    if len(parts) < 5:
        await message.answer(
            "📋 <b>Формат:</b>\n"
            "<code>/addpart имя пластик время_печати деталей_на_столе [описание]</code>\n\n"
            "Пример: <code>/addpart 7 PLA 45 5 Корпус основной</code>",
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
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("listparts"))
async def list_parts_cmd(message: types.Message):
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
        text += f"<b>{p[1]}</b> | {p[2] or '?'} | ⏱️{p[3]}мин | 📋{p[4]}шт\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(Command("updatepart"))
async def update_part_cmd(message: types.Message):
    if not is_senior_or_admin(db.get_user_by_telegram_id(message.from_user.id)):
        await message.answer("🚫 Только для старших операторов.")
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Формат: <code>/updatepart имя_детали новое_кол-во</code>",
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


async def main():
    try:
        logger.info("Бот запущен...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")
        for senior_id in db.get_senior_operators():
            try:
                await bot.send_message(senior_id, f"🚨 Бот упал: {e}")
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
