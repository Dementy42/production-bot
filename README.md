# Production Bot

Telegram-бот для учёта производства деталей.

## Установка

```bash
# 1. Клонировать репозиторий
git clone <your-repo-url>
cd production-bot

# 2. Создать виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Создать .env файл
cp .env.example .env
nano .env  # вставить токен бота

# 5. Запустить
python bot.py


CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    plastic_type TEXT,
    print_time_minutes INTEGER,
    parts_per_table INTEGER DEFAULT 1,
    description TEXT,
    is_active BOOLEAN DEFAULT 1
);

INSERT OR IGNORE INTO parts (name, plastic_type, print_time_minutes, parts_per_table) VALUES 
    ('1v1', 'PLA AERO', 310, 6),
    ('9', 'PLA AERO', 695, 4),
    ('10', 'PLA AERO', 350, 4),
    ('16v1', 'PLA AERO', 355, 2);

SELECT id, name, plastic_type, parts_per_table FROM parts;
.quit

