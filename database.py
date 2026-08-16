import sqlite3
import csv
from datetime import datetime


class Database:
    def __init__(self, db_path="production.db"):
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS users
                        (telegram_id INTEGER PRIMARY KEY,
                         telegram_username TEXT,
                         full_name TEXT,
                         role TEXT CHECK(role IN ('operator', 'senior', 'admin')),
                         sector_id INTEGER,
                         is_active BOOLEAN DEFAULT 1)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS sectors
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                         name TEXT UNIQUE)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS tasks
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                         sector_id INTEGER,
                         part_number TEXT,
                         shift_time TEXT CHECK(shift_time IN ('день', 'ночь')),
                         printers_count INTEGER,
                         launches_count INTEGER,
                         parts_per_table INTEGER,
                         total_plan INTEGER,
                         shift_date DATE,
                         is_locked BOOLEAN DEFAULT 0,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         FOREIGN KEY (sector_id) REFERENCES sectors(id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS boxes
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                         task_id INTEGER,
                         part_number TEXT,
                         box_number INTEGER,
                         good_quantity INTEGER DEFAULT 0,
                         defect_quantity INTEGER DEFAULT 0,
                         timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         FOREIGN KEY (task_id) REFERENCES tasks(id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS shifts
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                         operator_id INTEGER,
                         sector_id INTEGER,
                         start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         end_time TIMESTAMP,
                         status TEXT CHECK(status IN ('open', 'closed')) DEFAULT 'open',
                         FOREIGN KEY (operator_id) REFERENCES users(telegram_id),
                         FOREIGN KEY (sector_id) REFERENCES sectors(id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS shift_logs
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                         operator_id INTEGER,
                         sector_id INTEGER,
                         shift_id INTEGER,
                         action TEXT,
                         timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                         details TEXT)''')
            
            conn.commit()
    
    # ===== USERS =====
    def add_user(self, telegram_id, telegram_username, full_name, role, sector_id=None):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""INSERT OR REPLACE INTO users 
                        (telegram_id, telegram_username, full_name, role, sector_id, is_active)
                        VALUES (?, ?, ?, ?, ?, 1)""",
                     (telegram_id, telegram_username, full_name, role, sector_id))
            conn.commit()
    
    def add_user_without_tg(self, telegram_username, full_name, role, sector_id=None):
        """Добавить пользователя без telegram_id (будет привязан при авторизации)"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO users 
                        (telegram_username, full_name, role, sector_id, is_active)
                        VALUES (?, ?, ?, ?, 1)""",
                     (telegram_username, full_name, role, sector_id))
            conn.commit()
            return c.lastrowid
    
    def get_user_by_username(self, username):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""SELECT * FROM users 
                        WHERE telegram_username = ? AND is_active = 1 AND telegram_id IS NULL""", 
                     (username,))
            return c.fetchone()
    
    def get_user_by_fullname(self, full_name):
        """Найти пользователя по ФИО (для авторизации без username)"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""SELECT * FROM users 
                        WHERE full_name = ? AND is_active = 1 AND telegram_id IS NULL""", 
                     (full_name,))
            return c.fetchone()
    
    def get_user_by_telegram_id(self, telegram_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE telegram_id = ? AND is_active = 1", 
                     (telegram_id,))
            return c.fetchone()
    
    def bind_user_to_telegram(self, temp_username, telegram_id, telegram_username):
        """Привязать пользователя к Telegram аккаунту"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""UPDATE users 
                        SET telegram_id = ?, telegram_username = ?
                        WHERE telegram_username = ? AND telegram_id IS NULL""",
                     (telegram_id, telegram_username, temp_username))
            conn.commit()
    
    def bind_user_by_fullname(self, full_name, telegram_id, telegram_username):
        """Привязать пользователя по ФИО к Telegram аккаунту"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""UPDATE users 
                        SET telegram_id = ?, telegram_username = ?
                        WHERE full_name = ? AND telegram_id IS NULL AND is_active = 1""",
                     (telegram_id, telegram_username, full_name))
            conn.commit()
            return c.rowcount > 0
    
    # ===== SECTORS =====
    def get_all_sectors(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM sectors ORDER BY id")
            return c.fetchall()
    
    def get_sector_name(self, sector_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM sectors WHERE id = ?", (sector_id,))
            row = c.fetchone()
            return row[0] if row else "Неизвестный"
    
    def sector_exists(self, sector_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM sectors WHERE id = ?", (sector_id,))
            return c.fetchone() is not None
    
    # ===== TASKS =====
    def create_task(self, sector_id, part_number, shift_time, printers_count, 
                   launches_count, parts_per_table, shift_date):
        total_plan = printers_count * launches_count * parts_per_table
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO tasks 
                        (sector_id, part_number, shift_time, printers_count, 
                         launches_count, parts_per_table, total_plan, shift_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (sector_id, part_number, shift_time, printers_count,
                      launches_count, parts_per_table, total_plan, shift_date))
            conn.commit()
            return c.lastrowid
    
    def get_all_tasks_for_sector(self, sector_id, shift_date):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""SELECT * FROM tasks 
                        WHERE sector_id = ? AND shift_date = ? 
                        ORDER BY id""", (sector_id, shift_date))
            return c.fetchall()
    
    def get_task(self, task_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            return c.fetchone()
    
    def lock_task(self, task_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE tasks SET is_locked = 1 WHERE id = ?", (task_id,))
            conn.commit()
    
    def is_task_locked(self, task_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT is_locked FROM tasks WHERE id = ?", (task_id,))
            row = c.fetchone()
            return row[0] == 1 if row else False
    
    def has_locked_tasks_today(self, sector_id, shift_date):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""SELECT COUNT(*) FROM tasks 
                        WHERE sector_id = ? AND shift_date = ? AND is_locked = 1""",
                     (sector_id, shift_date))
            return c.fetchone()[0] > 0
    
    # ===== BOXES =====
    def add_box(self, task_id, part_number, box_number, good_qty, defect_qty):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO boxes 
                        (task_id, part_number, box_number, good_quantity, defect_quantity)
                        VALUES (?, ?, ?, ?, ?)""",
                     (task_id, part_number, box_number, good_qty, defect_qty))
            conn.commit()
            return c.lastrowid
    
    def get_boxes_for_task(self, task_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM boxes WHERE task_id = ? ORDER BY box_number", (task_id,))
            return c.fetchall()
    
    def get_task_statistics(self, task_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""SELECT COALESCE(SUM(good_quantity), 0) as total_good,
                               COALESCE(SUM(defect_quantity), 0) as total_defect,
                               COUNT(DISTINCT CASE WHEN good_quantity > 0 OR defect_quantity > 0 THEN box_number END) as box_count
                        FROM boxes WHERE task_id = ?""", (task_id,))
            return c.fetchone()
    
    # ===== SHIFTS =====
    def open_shift(self, operator_id, sector_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""UPDATE shifts SET end_time = CURRENT_TIMESTAMP, status = 'closed'
                        WHERE operator_id = ? AND status = 'open'""", (operator_id,))
            c.execute("INSERT INTO shifts (operator_id, sector_id) VALUES (?, ?)",
                     (operator_id, sector_id))
            conn.commit()
            return c.lastrowid
    
    def close_shift(self, shift_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""UPDATE shifts SET end_time = CURRENT_TIMESTAMP, status = 'closed'
                        WHERE id = ?""", (shift_id,))
            conn.commit()
    
    def get_active_shift_for_operator(self, operator_id):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM shifts WHERE operator_id = ? AND status = 'open'",
                     (operator_id,))
            return c.fetchone()
    
    def get_open_shifts_count(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM shifts WHERE status = 'open'")
            return c.fetchone()[0]
    
    def add_shift_log(self, operator_id, sector_id, shift_id, action, details=""):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO shift_logs 
                        (operator_id, sector_id, shift_id, action, details)
                        VALUES (?, ?, ?, ?, ?)""",
                     (operator_id, sector_id, shift_id, action, details))
            conn.commit()
    
    def get_senior_operators(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""SELECT telegram_id FROM users 
                        WHERE role IN ('senior', 'admin') 
                        AND is_active = 1 
                        AND telegram_id IS NOT NULL""")
            return [row[0] for row in c.fetchall()]
    
    def get_admin_users(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT telegram_id, full_name FROM users WHERE role = 'admin'")
            return c.fetchall()
    
    def get_all_operators(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""SELECT telegram_id, full_name, sector_id 
                        FROM users WHERE role = 'operator'""")
            return c.fetchall()
    
    # ===== REPORTS =====
    def generate_shift_report(self, report_date=None):
        with self.get_connection() as conn:
            c = conn.cursor()
            if report_date:
                c.execute("""SELECT s.name, t.part_number, t.shift_time,
                                   t.printers_count, t.launches_count, t.parts_per_table,
                                   t.total_plan, t.is_locked,
                                   COALESCE(SUM(b.good_quantity), 0),
                                   COALESCE(SUM(b.defect_quantity), 0)
                            FROM tasks t
                            JOIN sectors s ON t.sector_id = s.id
                            LEFT JOIN boxes b ON t.id = b.task_id
                            WHERE t.shift_date = ?
                            GROUP BY t.id
                            ORDER BY s.name, t.part_number""", (report_date,))
            else:
                c.execute("""SELECT s.name, t.part_number, t.shift_time,
                                   t.printers_count, t.launches_count, t.parts_per_table,
                                   t.total_plan, t.is_locked,
                                   COALESCE(SUM(b.good_quantity), 0),
                                   COALESCE(SUM(b.defect_quantity), 0)
                            FROM tasks t
                            JOIN sectors s ON t.sector_id = s.id
                            LEFT JOIN boxes b ON t.id = b.task_id
                            GROUP BY t.id
                            ORDER BY s.name, t.part_number""")
            return c.fetchall()
    
    def export_report_to_csv(self, report_date=None, filename="report.csv"):
        data = self.generate_shift_report(report_date)
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(['Участок', 'Деталь', 'Смена', 'Принтеров',
                           'Запусков', 'Деталей/стол', 'План', 'Задание закрыто',
                           'Факт (годен)', 'Брак'])
            for row in data:
                writer.writerow(row)
        return filename
    
    # ===== MASS IMPORT =====
    def bulk_import_users(self, users_list):
        """
        Массовый импорт пользователей из списка
        users_list: список кортежей (telegram_id, username, full_name, role, sector_id)
        Возвращает: (success_count, errors_list)
        """
        success = 0
        errors = []
        
        with self.get_connection() as conn:
            c = conn.cursor()
            
            for i, user in enumerate(users_list, start=2):  # start=2 (1-я строка - заголовок)
                try:
                    tg_id, username, full_name, role, sector_id = user
                    
                    # Проверка роли
                    if role not in ['operator', 'senior', 'admin']:
                        errors.append(f"Строка {i}: Неверная роль '{role}'")
                        continue
                    
                    # Проверка участка
                    if sector_id is not None and sector_id != '':
                        try:
                            sector_id = int(sector_id)
                        except ValueError:
                            errors.append(f"Строка {i}: Неверный sector_id '{sector_id}'")
                            continue
                        
                        c.execute("SELECT 1 FROM sectors WHERE id = ?", (sector_id,))
                        if not c.fetchone():
                            errors.append(f"Строка {i}: Участок {sector_id} не существует")
                            continue
                    else:
                        sector_id = None
                    
                    # Обработка telegram_id
                    if tg_id and str(tg_id).strip():
                        try:
                            tg_id = int(tg_id)
                            username = username if username else f"user_{tg_id}"
                            c.execute("""INSERT OR REPLACE INTO users 
                                        (telegram_id, telegram_username, full_name, role, sector_id, is_active)
                                        VALUES (?, ?, ?, ?, ?, 1)""",
                                     (tg_id, username, full_name, role, sector_id))
                        except ValueError:
                            errors.append(f"Строка {i}: Неверный telegram_id '{tg_id}'")
                            continue
                    else:
                        # Без telegram_id
                        if not username:
                            username = f"pending_{full_name.replace(' ', '_')}_{i}"
                        c.execute("""INSERT INTO users 
                                    (telegram_username, full_name, role, sector_id, is_active)
                                    VALUES (?, ?, ?, ?, 1)""",
                                 (username, full_name, role, sector_id))
                    
                    success += 1
                    
                except Exception as e:
                    errors.append(f"Строка {i}: {str(e)}")
            
            conn.commit()
        
        return success, errors