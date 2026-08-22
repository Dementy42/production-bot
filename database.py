import sqlite3
import csv
from datetime import datetime


class Database:
    """
    Database layer for the production Telegram bot.

    Main architectural rule:
        task_id is the ONLY parent key for production boxes/statistics.

    A task represents:
        sector + part + shift_time + shift_date

    Boxes belong to exactly one task:
        boxes.task_id -> tasks.id

    This prevents day/night production of the same part from being mixed.
    """

    ROLES = {"operator", "senior", "admin"}
    TASK_STATUSES = {"open", "active", "locked", "closed"}

    def __init__(self, db_path="production.db"):
        self.db_path = db_path
        self.init_db()

    # ============================================================
    # CONNECTION / SCHEMA
    # ============================================================

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            c = conn.cursor()

            # ---------------- USERS ----------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    telegram_username TEXT,
                    full_name TEXT,
                    role TEXT CHECK(role IN ('operator', 'senior', 'admin')),
                    sector_id INTEGER,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (sector_id) REFERENCES sectors(id)
                )
            """)

            # ---------------- SECTORS ----------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS sectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL
                )
            """)

            # ---------------- TASKS ----------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector_id INTEGER NOT NULL,
                    part_number TEXT NOT NULL,
                    shift_time TEXT NOT NULL
                        CHECK(shift_time IN ('день', 'ночь')),
                    printers_count INTEGER NOT NULL DEFAULT 0,
                    launches_count INTEGER NOT NULL DEFAULT 0,
                    parts_per_table INTEGER NOT NULL DEFAULT 0,
                    total_plan INTEGER NOT NULL DEFAULT 0,
                    shift_date DATE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open', 'active', 'locked', 'closed')),
                    is_locked INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    locked_at TIMESTAMP,
                    closed_at TIMESTAMP,
                    FOREIGN KEY (sector_id) REFERENCES sectors(id)
                )
            """)

            # ---------------- BOXES ----------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS boxes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    part_number TEXT NOT NULL,
                    box_number INTEGER NOT NULL,
                    good_quantity INTEGER NOT NULL DEFAULT 0
                        CHECK(good_quantity >= 0),
                    defect_quantity INTEGER NOT NULL DEFAULT 0
                        CHECK(defect_quantity >= 0),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
            """)

            # ---------------- SHIFTS ----------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_id INTEGER,
                    sector_id INTEGER,
                    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    end_time TIMESTAMP,
                    status TEXT CHECK(status IN ('open', 'closed')) DEFAULT 'open',
                    FOREIGN KEY (operator_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (sector_id) REFERENCES sectors(id)
                )
            """)

            # ---------------- SHIFT LOGS ----------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS shift_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_id INTEGER,
                    sector_id INTEGER,
                    shift_id INTEGER,
                    action TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    details TEXT,
                    FOREIGN KEY (operator_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (sector_id) REFERENCES sectors(id),
                    FOREIGN KEY (shift_id) REFERENCES shifts(id)
                )
            """)

            # ---------------- PARTS ----------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS parts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    plastic_type TEXT,
                    print_time_minutes REAL,
                    parts_per_table INTEGER,
                    description TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ---------------- BOX HISTORY ----------------
            c.execute("""
                CREATE TABLE IF NOT EXISTS box_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    box_id INTEGER,
                    task_id INTEGER NOT NULL,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    old_box_number INTEGER,
                    new_box_number INTEGER,
                    old_good INTEGER,
                    old_defect INTEGER,
                    new_good INTEGER,
                    new_defect INTEGER,
                    details TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (box_id) REFERENCES boxes(id) ON DELETE SET NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(telegram_id)
                )
            """)

            # ---------------- MIGRATION FOR OLD DATABASES ----------------
            self._migrate_schema(conn)

            # ---------------- DATA INTEGRITY ----------------
            self._repair_box_numbers(conn)

            # A box number is unique ONLY inside its own task.
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                ux_boxes_task_box_number
                ON boxes(task_id, box_number)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_boxes_task_id
                ON boxes(task_id)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_sector_date
                ON tasks(sector_id, shift_date)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_shift
                ON tasks(sector_id, shift_date, shift_time)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_history_task
                ON box_history(task_id, created_at)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_shifts_operator_status
                ON shifts(operator_id, status)
            """)

            conn.commit()

    def _table_columns(self, conn, table_name):
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row[1] for row in rows}

    def _add_column_if_missing(self, conn, table_name, column_name, definition):
        columns = self._table_columns(conn, table_name)
        if column_name not in columns:
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
            )

    def _migrate_schema(self, conn):
        """
        Non-destructive migration for databases created by the previous
        version of database.py.

        Existing production data is preserved.
        """
        # tasks: old DB has is_locked but not status/timestamps.
        self._add_column_if_missing(
            conn, "tasks", "status", "TEXT DEFAULT 'open'"
        )
        self._add_column_if_missing(
            conn, "tasks", "locked_at", "TIMESTAMP"
        )
        self._add_column_if_missing(
            conn, "tasks", "closed_at", "TIMESTAMP"
        )

        # boxes: updated_at was not present before.
        self._add_column_if_missing(
            conn, "boxes", "updated_at", "TIMESTAMP"
        )

        # users: timestamps were not present before.
        self._add_column_if_missing(
            conn, "users", "created_at", "TIMESTAMP"
        )
        self._add_column_if_missing(
            conn, "users", "updated_at", "TIMESTAMP"
        )

        # Normalize old task status from is_locked.
        conn.execute("""
            UPDATE tasks
            SET status = CASE
                WHEN is_locked = 1 THEN 'locked'
                ELSE 'open'
            END
            WHERE status IS NULL OR status = ''
        """)

        # Fill timestamps for old rows.
        conn.execute("""
            UPDATE boxes
            SET updated_at = COALESCE(updated_at, timestamp)
            WHERE updated_at IS NULL
        """)

        conn.execute("""
            UPDATE users
            SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP),
                updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)
        """)

    def _repair_box_numbers(self, conn):
        """
        Old versions used len(boxes) + 1 and could create duplicate numbers
        after deletion. Normalize numbers inside each task before creating
        the unique index.
        """
        task_ids = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT task_id FROM boxes ORDER BY task_id"
            ).fetchall()
        ]

        for task_id in task_ids:
            rows = conn.execute("""
                SELECT id, box_number
                FROM boxes
                WHERE task_id = ?
                ORDER BY
                    CASE WHEN box_number IS NULL THEN 1 ELSE 0 END,
                    box_number,
                    id
            """, (task_id,)).fetchall()

            used = set()
            next_number = 1

            for box_id, box_number in rows:
                if box_number is not None and box_number not in used:
                    used.add(box_number)
                    continue

                while next_number in used:
                    next_number += 1

                conn.execute(
                    "UPDATE boxes SET box_number = ? WHERE id = ?",
                    (next_number, box_id)
                )
                used.add(next_number)
                next_number += 1

    # ============================================================
    # PERMISSIONS
    # ============================================================

    ROLE_PERMISSIONS = {
        "operator": {
            "box_add",
            "box_edit",
            "report_view",
            "shift_manage",
        },
        "senior": {
            "box_add",
            "box_edit",
            "box_delete",
            "report_view",
            "report_export",
            "task_create",
            "task_edit",
            "sector_view",
            "shift_manage",
            "history_view",
        },
        "admin": {"*"},
    }

    def has_permission(self, user, permission):
        if not user:
            return False

        # user tuple: telegram_id, username, full_name, role, sector_id, ...
        role = user[3] if len(user) > 3 else None

        if role == "admin":
            return True

        return permission in self.ROLE_PERMISSIONS.get(role, set())

    # ============================================================
    # USERS
    # ============================================================

    def add_user(self, telegram_id, telegram_username, full_name,
                 role, sector_id=None):
        if role not in self.ROLES:
            raise ValueError(f"Неверная роль: {role}")

        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO users
                (telegram_id, telegram_username, full_name, role, sector_id,
                 is_active, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            """, (
                telegram_id,
                telegram_username,
                full_name,
                role,
                sector_id
            ))
            conn.commit()

    def add_user_without_tg(self, telegram_username, full_name,
                             role, sector_id=None):
        if role not in self.ROLES:
            raise ValueError(f"Неверная роль: {role}")

        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO users
                (telegram_username, full_name, role, sector_id, is_active)
                VALUES (?, ?, ?, ?, 1)
            """, (
                telegram_username,
                full_name,
                role,
                sector_id
            ))
            conn.commit()
            return c.lastrowid

    def get_user_by_username(self, username):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT * FROM users
                WHERE telegram_username = ?
                  AND is_active = 1
                  AND telegram_id IS NULL
            """, (username,)).fetchone()

    def get_user_by_fullname(self, full_name):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT * FROM users
                WHERE full_name = ?
                  AND is_active = 1
                  AND telegram_id IS NULL
            """, (full_name,)).fetchone()

    def get_user_by_telegram_id(self, telegram_id):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT * FROM users
                WHERE telegram_id = ? AND is_active = 1
            """, (telegram_id,)).fetchone()

    def bind_user_to_telegram(self, temp_username, telegram_id,
                              telegram_username):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE users
                SET telegram_id = ?,
                    telegram_username = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE telegram_username = ?
                  AND telegram_id IS NULL
            """, (
                telegram_id,
                telegram_username,
                temp_username
            ))
            conn.commit()
            return c.rowcount > 0

    def bind_user_by_fullname(self, full_name, telegram_id,
                              telegram_username):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE users
                SET telegram_id = ?,
                    telegram_username = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE full_name = ?
                  AND telegram_id IS NULL
                  AND is_active = 1
            """, (
                telegram_id,
                telegram_username,
                full_name
            ))
            conn.commit()
            return c.rowcount > 0

    # ============================================================
    # SECTORS
    # ============================================================

    def get_all_sectors(self):
        with self.get_connection() as conn:
            return conn.execute(
                "SELECT * FROM sectors ORDER BY id"
            ).fetchall()

    def get_sector_name(self, sector_id):
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT name FROM sectors WHERE id = ?",
                (sector_id,)
            ).fetchone()
            return row[0] if row else "Неизвестный"

    def sector_exists(self, sector_id):
        with self.get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM sectors WHERE id = ?",
                (sector_id,)
            ).fetchone() is not None

    def add_sector(self, name):
        name = str(name).strip()
        if not name:
            raise ValueError("Название участка не может быть пустым")

        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO sectors (name) VALUES (?)",
                (name,)
            )
            conn.commit()
            return c.lastrowid

    # ============================================================
    # TASKS
    # ============================================================

    def create_task(self, sector_id, part_number, shift_time,
                    printers_count, launches_count,
                    parts_per_table, shift_date):
        if shift_time not in ("день", "ночь"):
            raise ValueError("shift_time должен быть 'день' или 'ночь'")

        if not self.sector_exists(sector_id):
            raise ValueError(f"Участок {sector_id} не существует")

        printers_count = int(printers_count)
        launches_count = int(launches_count)
        parts_per_table = int(parts_per_table)

        if printers_count < 0 or launches_count < 0 or parts_per_table < 0:
            raise ValueError("Количество не может быть отрицательным")

        total_plan = (
            printers_count *
            launches_count *
            parts_per_table
        )

        with self.get_connection() as conn:
            c = conn.cursor()

            c.execute("""
                INSERT INTO tasks
                (
                    sector_id,
                    part_number,
                    shift_time,
                    printers_count,
                    launches_count,
                    parts_per_table,
                    total_plan,
                    shift_date,
                    status,
                    is_locked
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 0)
            """, (
                sector_id,
                str(part_number),
                shift_time,
                printers_count,
                launches_count,
                parts_per_table,
                total_plan,
                shift_date
            ))

            task_id = c.lastrowid
            conn.commit()
            return task_id

    def get_all_tasks_for_sector(self, sector_id, shift_date):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT * FROM tasks
                WHERE sector_id = ?
                  AND shift_date = ?
                ORDER BY
                    CASE shift_time
                        WHEN 'день' THEN 1
                        WHEN 'ночь' THEN 2
                        ELSE 3
                    END,
                    id
            """, (sector_id, shift_date)).fetchall()

    def get_tasks_for_sector_and_shift(self, sector_id, shift_date,
                                       shift_time=None):
        with self.get_connection() as conn:
            if shift_time:
                return conn.execute("""
                    SELECT * FROM tasks
                    WHERE sector_id = ?
                      AND shift_date = ?
                      AND shift_time = ?
                    ORDER BY id
                """, (
                    sector_id,
                    shift_date,
                    shift_time
                )).fetchall()

            return conn.execute("""
                SELECT * FROM tasks
                WHERE sector_id = ?
                  AND shift_date = ?
                ORDER BY id
            """, (sector_id, shift_date)).fetchall()

    def get_all_tasks(self, shift_date=None):
        with self.get_connection() as conn:
            if shift_date:
                return conn.execute("""
                    SELECT * FROM tasks
                    WHERE shift_date = ?
                    ORDER BY sector_id, shift_time, id
                """, (shift_date,)).fetchall()

            return conn.execute("""
                SELECT * FROM tasks
                ORDER BY shift_date DESC, sector_id, shift_time, id
            """).fetchall()

    def get_task(self, task_id):
        with self.get_connection() as conn:
            return conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()

    def get_task_dict(self, task_id):
        """
        Named representation for new code.
        Old bot code can continue using get_task(), which returns a tuple.
        """
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT
                    id,
                    sector_id,
                    part_number,
                    shift_time,
                    printers_count,
                    launches_count,
                    parts_per_table,
                    total_plan,
                    shift_date,
                    status,
                    is_locked,
                    created_at,
                    locked_at,
                    closed_at
                FROM tasks
                WHERE id = ?
            """, (task_id,)).fetchone()

        if not row:
            return None

        keys = [
            "id",
            "sector_id",
            "part_number",
            "shift_time",
            "printers_count",
            "launches_count",
            "parts_per_table",
            "total_plan",
            "shift_date",
            "status",
            "is_locked",
            "created_at",
            "locked_at",
            "closed_at",
        ]
        return dict(zip(keys, row))

    def update_task(self, task_id, **fields):
        allowed = {
            "part_number",
            "shift_time",
            "printers_count",
            "launches_count",
            "parts_per_table",
            "shift_date",
            "total_plan",
        }

        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(
                f"Недопустимые поля: {', '.join(sorted(unknown))}"
            )

        if not fields:
            return False

        task = self.get_task(task_id)
        if not task:
            raise ValueError("ПЗ не найдено")

        if self.is_task_closed(task_id):
            raise ValueError("ПЗ уже закрыто")

        if "shift_time" in fields and fields["shift_time"] not in ("день", "ночь"):
            raise ValueError("shift_time должен быть 'день' или 'ночь'")

        # Recalculate plan if its components changed.
        printers = fields.get("printers_count", task[4])
        launches = fields.get("launches_count", task[5])
        parts_per_table = fields.get("parts_per_table", task[6])

        if any(int(x) < 0 for x in (printers, launches, parts_per_table)):
            raise ValueError("Количество не может быть отрицательным")

        fields["total_plan"] = (
            int(printers) *
            int(launches) *
            int(parts_per_table)
        )

        assignments = ", ".join(
            f"{field} = ?" for field in fields
        )
        values = list(fields.values()) + [task_id]

        with self.get_connection() as conn:
            conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                values
            )
            conn.commit()

        return True

    def activate_task(self, task_id):
        task = self.get_task(task_id)
        if not task:
            return False

        if self.is_task_closed(task_id):
            return False

        with self.get_connection() as conn:
            conn.execute("""
                UPDATE tasks
                SET status = 'active',
                    is_locked = 0
                WHERE id = ?
            """, (task_id,))
            conn.commit()
        return True

    def lock_task(self, task_id):
        """
        Legacy-compatible method.

        LOCKED means normal operator editing is stopped, but the task
        is not necessarily permanently closed.
        """
        with self.get_connection() as conn:
            cur = conn.execute("""
                UPDATE tasks
                SET status = 'locked',
                    is_locked = 1,
                    locked_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status != 'closed'
            """, (task_id,))
            conn.commit()
            return cur.rowcount > 0

    def close_task(self, task_id):
        with self.get_connection() as conn:
            cur = conn.execute("""
                UPDATE tasks
                SET status = 'closed',
                    is_locked = 1,
                    locked_at = COALESCE(locked_at, CURRENT_TIMESTAMP),
                    closed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status != 'closed'
            """, (task_id,))
            conn.commit()
            return cur.rowcount > 0

    def reopen_task(self, task_id):
        """
        Administrative operation. Reopening does not erase history.
        """
        with self.get_connection() as conn:
            cur = conn.execute("""
                UPDATE tasks
                SET status = 'active',
                    is_locked = 0,
                    closed_at = NULL
                WHERE id = ?
            """, (task_id,))
            conn.commit()
            return cur.rowcount > 0

    def is_task_locked(self, task_id):
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT is_locked, status
                FROM tasks
                WHERE id = ?
            """, (task_id,)).fetchone()

            if not row:
                return False

            return bool(row[0]) or row[1] in ("locked", "closed")

    def is_task_closed(self, task_id):
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
            return bool(row and row[0] == "closed")

    def get_task_status(self, task_id):
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,)
            ).fetchone()
            return row[0] if row else None

    def has_locked_tasks_today(self, sector_id, shift_date):
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT COUNT(*)
                FROM tasks
                WHERE sector_id = ?
                  AND shift_date = ?
                  AND (
                      is_locked = 1
                      OR status IN ('locked', 'closed')
                  )
            """, (sector_id, shift_date)).fetchone()
            return row[0] > 0

    # ============================================================
    # BOXES
    # ============================================================

    def _validate_box_values(self, good_qty, defect_qty):
        good_qty = int(good_qty)
        defect_qty = int(defect_qty)

        if good_qty < 0 or defect_qty < 0:
            raise ValueError("Количество не может быть отрицательным")

        if good_qty == 0 and defect_qty == 0:
            raise ValueError(
                "Коробка не может содержать 0 годных и 0 брака"
            )

        return good_qty, defect_qty

    def get_next_box_number(self, task_id, conn=None):
        """
        Correct replacement for len(boxes) + 1.

        Number is local to the task, so day/night tasks can both have #1.
        """
        if conn is not None:
            row = conn.execute("""
                SELECT COALESCE(MAX(box_number), 0) + 1
                FROM boxes
                WHERE task_id = ?
            """, (task_id,)).fetchone()
            return int(row[0])

        with self.get_connection() as connection:
            row = connection.execute("""
                SELECT COALESCE(MAX(box_number), 0) + 1
                FROM boxes
                WHERE task_id = ?
            """, (task_id,)).fetchone()
            return int(row[0])

    def add_box(self, task_id, part_number, box_number,
                good_qty, defect_qty, user_id=None, details=""):
        """
        Atomic box creation.

        IMPORTANT:
        No box is inserted before the final confirmation.
        """
        good_qty, defect_qty = self._validate_box_values(
            good_qty,
            defect_qty
        )

        with self.get_connection() as conn:
            # Lock / close check is done inside the transaction.
            task = conn.execute("""
                SELECT id, part_number, status, is_locked
                FROM tasks
                WHERE id = ?
            """, (task_id,)).fetchone()

            if not task:
                raise ValueError("ПЗ не найдено")

            if task[2] == "closed":
                raise ValueError("ПЗ уже закрыто")

            if task[3] == 1 or task[2] == "locked":
                raise ValueError("ПЗ заблокировано")

            # If caller passes None/0, generate the number safely.
            if box_number is None or int(box_number) <= 0:
                box_number = self.get_next_box_number(
                    task_id,
                    conn=conn
                )
            else:
                box_number = int(box_number)

                exists = conn.execute("""
                    SELECT 1
                    FROM boxes
                    WHERE task_id = ?
                      AND box_number = ?
                """, (task_id, box_number)).fetchone()

                if exists:
                    raise ValueError(
                        f"Коробка #{box_number} уже существует в этом ПЗ"
                    )

            # Prefer task's part number as the source of truth.
            if part_number is None:
                part_number = task[1]

            cur = conn.execute("""
                INSERT INTO boxes
                (
                    task_id,
                    part_number,
                    box_number,
                    good_quantity,
                    defect_quantity,
                    timestamp,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (
                task_id,
                str(part_number),
                box_number,
                good_qty,
                defect_qty
            ))

            box_id = cur.lastrowid

            conn.execute("""
                INSERT INTO box_history
                (
                    box_id,
                    task_id,
                    user_id,
                    action,
                    old_box_number,
                    new_box_number,
                    old_good,
                    old_defect,
                    new_good,
                    new_defect,
                    details
                )
                VALUES (?, ?, ?, 'create', NULL, ?, NULL, NULL, ?, ?, ?)
            """, (
                box_id,
                task_id,
                user_id,
                box_number,
                good_qty,
                defect_qty,
                details
            ))

            # First real box activates an OPEN task.
            conn.execute("""
                UPDATE tasks
                SET status = CASE
                    WHEN status = 'open' THEN 'active'
                    ELSE status
                END
                WHERE id = ?
            """, (task_id,))

            conn.commit()
            return box_id

    def update_box(self, box_id, good_qty=None, defect_qty=None,
                   box_number=None, user_id=None, details="",
                   allow_locked=False):
        """
        Update an existing box.

        For normal operators a locked/closed task cannot be changed.
        Senior/admin code can explicitly pass allow_locked=True.
        """
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT
                    b.id,
                    b.task_id,
                    b.box_number,
                    b.good_quantity,
                    b.defect_quantity,
                    t.status,
                    t.is_locked
                FROM boxes b
                JOIN tasks t ON t.id = b.task_id
                WHERE b.id = ?
            """, (box_id,)).fetchone()

            if not row:
                raise ValueError("Коробка не найдена")

            (
                _box_id,
                task_id,
                old_box_number,
                old_good,
                old_defect,
                status,
                is_locked
            ) = row

            if not allow_locked and (
                status in ("locked", "closed") or is_locked
            ):
                raise ValueError("ПЗ заблокировано или закрыто")

            new_good = old_good if good_qty is None else int(good_qty)
            new_defect = (
                old_defect if defect_qty is None
                else int(defect_qty)
            )
            new_box_number = (
                old_box_number
                if box_number is None
                else int(box_number)
            )

            if new_good < 0 or new_defect < 0:
                raise ValueError("Количество не может быть отрицательным")

            if new_good == 0 and new_defect == 0:
                raise ValueError(
                    "Коробка не может содержать 0 годных и 0 брака"
                )

            if new_box_number != old_box_number:
                duplicate = conn.execute("""
                    SELECT 1
                    FROM boxes
                    WHERE task_id = ?
                      AND box_number = ?
                      AND id != ?
                """, (
                    task_id,
                    new_box_number,
                    box_id
                )).fetchone()

                if duplicate:
                    raise ValueError(
                        f"Коробка #{new_box_number} уже существует"
                    )

            conn.execute("""
                UPDATE boxes
                SET box_number = ?,
                    good_quantity = ?,
                    defect_quantity = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                new_box_number,
                new_good,
                new_defect,
                box_id
            ))

            conn.execute("""
                INSERT INTO box_history
                (
                    box_id,
                    task_id,
                    user_id,
                    action,
                    old_box_number,
                    new_box_number,
                    old_good,
                    old_defect,
                    new_good,
                    new_defect,
                    details
                )
                VALUES (?, ?, ?, 'update', ?, ?, ?, ?, ?, ?, ?)
            """, (
                box_id,
                task_id,
                user_id,
                old_box_number,
                new_box_number,
                old_good,
                old_defect,
                new_good,
                new_defect,
                details
            ))

            conn.commit()
            return True

    def delete_box(self, box_id, user_id=None, details="",
                   allow_locked=False):
        """
        Delete a box and write the deletion to history before removing it.

        History keeps box_id nullable because boxes.id is ON DELETE SET NULL.
        """
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT
                    b.id,
                    b.task_id,
                    b.box_number,
                    b.good_quantity,
                    b.defect_quantity,
                    t.status,
                    t.is_locked
                FROM boxes b
                JOIN tasks t ON t.id = b.task_id
                WHERE b.id = ?
            """, (box_id,)).fetchone()

            if not row:
                return False

            (
                _box_id,
                task_id,
                box_number,
                good,
                defect,
                status,
                is_locked
            ) = row

            if not allow_locked and (
                status in ("locked", "closed") or is_locked
            ):
                raise ValueError("ПЗ заблокировано или закрыто")

            # Store history with box_id first; FK is SET NULL after delete.
            conn.execute("""
                INSERT INTO box_history
                (
                    box_id,
                    task_id,
                    user_id,
                    action,
                    old_box_number,
                    new_box_number,
                    old_good,
                    old_defect,
                    new_good,
                    new_defect,
                    details
                )
                VALUES (?, ?, ?, 'delete', ?, NULL, ?, ?, NULL, NULL, ?)
            """, (
                box_id,
                task_id,
                user_id,
                box_number,
                good,
                defect,
                details
            ))

            conn.execute(
                "DELETE FROM boxes WHERE id = ?",
                (box_id,)
            )

            conn.commit()
            return True

    def get_box(self, box_id):
        with self.get_connection() as conn:
            return conn.execute(
                "SELECT * FROM boxes WHERE id = ?",
                (box_id,)
            ).fetchone()

    def get_boxes_for_task(self, task_id):
        """
        IMPORTANT: task_id is the only filter.

        There is intentionally NO part_number filtering here.
        """
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT *
                FROM boxes
                WHERE task_id = ?
                ORDER BY box_number, id
            """, (task_id,)).fetchall()

    def get_boxes_for_sector_shift_part(self, sector_id, shift_date,
                                        shift_time, part_number):
        """
        Senior-facing helper.

        Finds the exact task first, then gets boxes through task_id.
        It never aggregates boxes across day/night tasks.
        """
        with self.get_connection() as conn:
            task_rows = conn.execute("""
                SELECT *
                FROM tasks
                WHERE sector_id = ?
                  AND shift_date = ?
                  AND shift_time = ?
                  AND part_number = ?
                ORDER BY id
            """, (
                sector_id,
                shift_date,
                shift_time,
                str(part_number)
            )).fetchall()

            result = []

            for task in task_rows:
                boxes = conn.execute("""
                    SELECT *
                    FROM boxes
                    WHERE task_id = ?
                    ORDER BY box_number, id
                """, (task[0],)).fetchall()

                result.append({
                    "task": task,
                    "boxes": boxes
                })

            return result

    def get_task_statistics(self, task_id):
        """
        Statistics are calculated ONLY by task_id.

        This is the critical day/night separation.
        """
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT
                    COALESCE(SUM(good_quantity), 0) AS total_good,
                    COALESCE(SUM(defect_quantity), 0) AS total_defect,
                    COUNT(
                        DISTINCT CASE
                            WHEN good_quantity > 0
                              OR defect_quantity > 0
                            THEN box_number
                        END
                    ) AS box_count
                FROM boxes
                WHERE task_id = ?
            """, (task_id,)).fetchone()

    def get_task_statistics_dict(self, task_id):
        good, defect, box_count = self.get_task_statistics(task_id)
        task = self.get_task(task_id)

        plan = task[7] if task else 0
        fact = good + defect
        remaining = max(plan - fact, 0)

        percent = (
            (fact / plan) * 100
            if plan
            else 0
        )

        return {
            "task_id": task_id,
            "plan": plan,
            "good": good,
            "defect": defect,
            "fact": fact,
            "remaining": remaining,
            "percent": round(percent, 2),
            "box_count": box_count,
        }

    # ============================================================
    # BOX HISTORY / AUDIT
    # ============================================================

    def get_box_history(self, box_id):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT *
                FROM box_history
                WHERE box_id = ?
                ORDER BY created_at DESC, id DESC
            """, (box_id,)).fetchall()

    def get_task_history(self, task_id):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT *
                FROM box_history
                WHERE task_id = ?
                ORDER BY created_at DESC, id DESC
            """, (task_id,)).fetchall()

    def get_recent_history(self, limit=100):
        limit = max(1, min(int(limit), 1000))

        with self.get_connection() as conn:
            return conn.execute("""
                SELECT
                    h.*,
                    u.full_name,
                    u.telegram_username
                FROM box_history h
                LEFT JOIN users u ON u.telegram_id = h.user_id
                ORDER BY h.created_at DESC, h.id DESC
                LIMIT ?
            """, (limit,)).fetchall()

    # ============================================================
    # SHIFTS
    # ============================================================

    def open_shift(self, operator_id, sector_id):
        with self.get_connection() as conn:
            c = conn.cursor()

            # One active shift per operator.
            c.execute("""
                UPDATE shifts
                SET end_time = CURRENT_TIMESTAMP,
                    status = 'closed'
                WHERE operator_id = ?
                  AND status = 'open'
            """, (operator_id,))

            c.execute("""
                INSERT INTO shifts
                (operator_id, sector_id, start_time, status)
                VALUES (?, ?, CURRENT_TIMESTAMP, 'open')
            """, (
                operator_id,
                sector_id
            ))

            shift_id = c.lastrowid

            c.execute("""
                INSERT INTO shift_logs
                (operator_id, sector_id, shift_id, action, details)
                VALUES (?, ?, ?, 'shift_open', '')
            """, (
                operator_id,
                sector_id,
                shift_id
            ))

            conn.commit()
            return shift_id

    def close_shift(self, shift_id):
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT operator_id, sector_id, status
                FROM shifts
                WHERE id = ?
            """, (shift_id,)).fetchone()

            if not row:
                return False

            if row[2] == "closed":
                return True

            conn.execute("""
                UPDATE shifts
                SET end_time = CURRENT_TIMESTAMP,
                    status = 'closed'
                WHERE id = ?
            """, (shift_id,))

            conn.execute("""
                INSERT INTO shift_logs
                (operator_id, sector_id, shift_id, action, details)
                VALUES (?, ?, ?, 'shift_close', '')
            """, (
                row[0],
                row[1],
                shift_id
            ))

            conn.commit()
            return True

    def get_active_shift_for_operator(self, operator_id):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT *
                FROM shifts
                WHERE operator_id = ?
                  AND status = 'open'
                ORDER BY id DESC
                LIMIT 1
            """, (operator_id,)).fetchone()

    def get_open_shifts_count(self):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT COUNT(*)
                FROM shifts
                WHERE status = 'open'
            """).fetchone()[0]

    def add_shift_log(self, operator_id, sector_id, shift_id,
                      action, details=""):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO shift_logs
                (operator_id, sector_id, shift_id, action, details)
                VALUES (?, ?, ?, ?, ?)
            """, (
                operator_id,
                sector_id,
                shift_id,
                action,
                details
            ))
            conn.commit()

    # ============================================================
    # USER LISTS
    # ============================================================

    def get_senior_operators(self):
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT telegram_id
                FROM users
                WHERE role IN ('senior', 'admin')
                  AND is_active = 1
                  AND telegram_id IS NOT NULL
            """).fetchall()
            return [row[0] for row in rows]

    def get_admin_users(self):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT telegram_id, full_name
                FROM users
                WHERE role = 'admin'
                  AND is_active = 1
            """).fetchall()

    def get_all_operators(self):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT telegram_id, full_name, sector_id
                FROM users
                WHERE role = 'operator'
                  AND is_active = 1
            """).fetchall()

    # ============================================================
    # REPORTS
    # ============================================================

    def generate_shift_report(self, report_date=None):
        """
        One row = one task.

        Therefore:
            same part + day != same part + night

        LEFT JOIN is performed using task.id = boxes.task_id.
        """
        with self.get_connection() as conn:
            base_sql = """
                SELECT
                    s.name,
                    t.part_number,
                    t.shift_time,
                    t.printers_count,
                    t.launches_count,
                    t.parts_per_table,
                    t.total_plan,
                    t.is_locked,
                    COALESCE(SUM(b.good_quantity), 0),
                    COALESCE(SUM(b.defect_quantity), 0)
                FROM tasks t
                JOIN sectors s ON t.sector_id = s.id
                LEFT JOIN boxes b ON b.task_id = t.id
            """

            if report_date:
                sql = base_sql + """
                    WHERE t.shift_date = ?
                    GROUP BY t.id
                    ORDER BY
                        s.name,
                        t.part_number,
                        CASE t.shift_time
                            WHEN 'день' THEN 1
                            WHEN 'ночь' THEN 2
                            ELSE 3
                        END,
                        t.id
                """
                return conn.execute(sql, (report_date,)).fetchall()

            sql = base_sql + """
                GROUP BY t.id
                ORDER BY
                    t.shift_date DESC,
                    s.name,
                    t.part_number,
                    CASE t.shift_time
                        WHEN 'день' THEN 1
                        WHEN 'ночь' THEN 2
                        ELSE 3
                    END,
                    t.id
            """
            return conn.execute(sql).fetchall()

    def generate_task_report(self, task_id):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT
                    s.name,
                    t.part_number,
                    t.shift_time,
                    t.shift_date,
                    t.printers_count,
                    t.launches_count,
                    t.parts_per_table,
                    t.total_plan,
                    t.status,
                    t.is_locked,
                    COALESCE(SUM(b.good_quantity), 0),
                    COALESCE(SUM(b.defect_quantity), 0),
                    COUNT(b.id)
                FROM tasks t
                JOIN sectors s ON s.id = t.sector_id
                LEFT JOIN boxes b ON b.task_id = t.id
                WHERE t.id = ?
                GROUP BY t.id
            """, (task_id,)).fetchone()

    def export_report_to_csv(self, report_date=None,
                             filename="report.csv"):
        data = self.generate_shift_report(report_date)

        with open(
            filename,
            "w",
            newline="",
            encoding="utf-8-sig"
        ) as f:
            writer = csv.writer(f, delimiter=";")

            writer.writerow([
                "Участок",
                "Деталь",
                "Смена",
                "Принтеров",
                "Запусков",
                "Деталей/стол",
                "План",
                "Задание закрыто",
                "Факт (годен)",
                "Брак"
            ])

            for row in data:
                writer.writerow(row)

        return filename

    # ============================================================
    # PARTS
    # ============================================================

    def get_all_parts(self):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT
                    id,
                    name,
                    plastic_type,
                    print_time_minutes,
                    parts_per_table,
                    description
                FROM parts
                WHERE is_active = 1
                ORDER BY name
            """).fetchall()

    def get_part_by_name(self, part_name):
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT
                    id,
                    name,
                    plastic_type,
                    print_time_minutes,
                    parts_per_table,
                    description
                FROM parts
                WHERE name = ?
                  AND is_active = 1
            """, (str(part_name),)).fetchone()

    def add_part(self, name, plastic_type, print_time,
                 parts_per_table, description=""):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO parts
                (
                    name,
                    plastic_type,
                    print_time_minutes,
                    parts_per_table,
                    description
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                name,
                plastic_type,
                print_time,
                parts_per_table,
                description
            ))
            conn.commit()
            return c.lastrowid

    def update_part_table_qty(self, part_name, new_qty):
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE parts
                SET parts_per_table = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE name = ?
            """, (
                int(new_qty),
                str(part_name)
            ))
            conn.commit()

    # ============================================================
    # MASS IMPORT
    # ============================================================

    def bulk_import_users(self, users_list):
        """
        users_list:
            (telegram_id, username, full_name, role, sector_id)

        Returns:
            (success_count, errors_list)
        """
        success = 0
        errors = []

        with self.get_connection() as conn:
            c = conn.cursor()

            for i, user in enumerate(users_list, start=2):
                try:
                    tg_id, username, full_name, role, sector_id = user

                    if role not in self.ROLES:
                        errors.append(
                            f"Строка {i}: Неверная роль '{role}'"
                        )
                        continue

                    if sector_id is not None and sector_id != "":
                        try:
                            sector_id = int(sector_id)
                        except (ValueError, TypeError):
                            errors.append(
                                f"Строка {i}: Неверный sector_id "
                                f"'{sector_id}'"
                            )
                            continue

                        c.execute(
                            "SELECT 1 FROM sectors WHERE id = ?",
                            (sector_id,)
                        )

                        if not c.fetchone():
                            errors.append(
                                f"Строка {i}: Участок "
                                f"{sector_id} не существует"
                            )
                            continue
                    else:
                        sector_id = None

                    if tg_id and str(tg_id).strip():
                        try:
                            tg_id = int(tg_id)
                        except (ValueError, TypeError):
                            errors.append(
                                f"Строка {i}: Неверный telegram_id "
                                f"'{tg_id}'"
                            )
                            continue

                        username = (
                            username
                            if username
                            else f"user_{tg_id}"
                        )

                        c.execute("""
                            INSERT OR REPLACE INTO users
                            (
                                telegram_id,
                                telegram_username,
                                full_name,
                                role,
                                sector_id,
                                is_active,
                                updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                        """, (
                            tg_id,
                            username,
                            full_name,
                            role,
                            sector_id
                        ))

                    else:
                        if not username:
                            safe_name = str(full_name).replace(" ", "_")
                            username = (
                                f"pending_{safe_name}_{i}"
                            )

                        c.execute("""
                            INSERT INTO users
                            (
                                telegram_username,
                                full_name,
                                role,
                                sector_id,
                                is_active
                            )
                            VALUES (?, ?, ?, ?, 1)
                        """, (
                            username,
                            full_name,
                            role,
                            sector_id
                        ))

                    success += 1

                except Exception as e:
                    errors.append(
                        f"Строка {i}: {str(e)}"
                    )

            conn.commit()

        return success, errors
