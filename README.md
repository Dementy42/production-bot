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