# Зависимость: pip install "aiogram>=3.13,<4"
# Запуск: BOT_TOKEN=123:abc ADMIN_IDS=111,222 SUPERADMIN_IDS=111 python pubg_bot.py
# Если SUPERADMIN_IDS не задан, супер-админами считаются ADMIN_IDS.
# Снятие админа из ADMIN_IDS делается в панели супер-админа и хранится в базе.

import asyncio
import html
import os
import re
import shutil
import sqlite3
import struct
import time
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_NAME = "pubg_bot.sqlite3"


def resolve_db_file() -> str:
    """Bothost хранит базу только в /app/data (переменная DATA_DIR).

    Файл рядом с кодом панель «База данных бота» не видит и затирает
    при обновлении. Явный путь: DATABASE_PATH, DB_PATH или DB_FILE.
    """
    explicit = (
        os.getenv("DATABASE_PATH", "").strip()
        or os.getenv("DB_PATH", "").strip()
        or os.getenv("DB_FILE", "").strip()
    )
    if explicit:
        return os.path.abspath(explicit)

    data_dir = os.getenv("DATA_DIR", "").strip()
    if not data_dir and os.path.isdir("/app/data"):
        data_dir = "/app/data"

    if data_dir:
        return os.path.abspath(os.path.join(data_dir, DB_NAME))

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_NAME)


def adopt_existing_database(target: str) -> None:
    """Если в data базы ещё нет, забираем уже залитый файл рядом с ботом."""
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, DB_NAME),
        os.path.join(os.getcwd(), DB_NAME),
        os.path.join("/app", DB_NAME),
    ]
    target_abs = os.path.abspath(target)
    seen: set[str] = set()

    for source in candidates:
        source_abs = os.path.abspath(source)
        if source_abs in seen or source_abs == target_abs:
            continue
        seen.add(source_abs)
        if not os.path.isfile(source_abs) or os.path.getsize(source_abs) <= 0:
            continue

        os.makedirs(os.path.dirname(target_abs), exist_ok=True)
        shutil.copy2(source_abs, target_abs)
        for suffix in ("-wal", "-shm"):
            extra = source_abs + suffix
            if os.path.isfile(extra):
                shutil.copy2(extra, target_abs + suffix)
        print(
            f"База перенесена в постоянную папку: {source_abs} -> {target_abs}",
            flush=True,
        )
        return


DB_FILE = resolve_db_file()
adopt_existing_database(DB_FILE)

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

# Пустой SUPERADMIN_IDS = все ADMIN_IDS являются супер-админами.
SUPERADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("SUPERADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

router = Router()

COLLECTION_MAX = 100
BLACK_SQUARE_PHOTO = "black_square"
_BLACK_SQUARE_PNG: bytes | None = None


def black_square_png(size: int = 512) -> bytes:
    """Чёрный квадрат 1:1. Кэш, чтобы не собирать PNG на каждую анкету."""
    global _BLACK_SQUARE_PNG

    if _BLACK_SQUARE_PNG is not None and size == 512:
        return _BLACK_SQUARE_PNG

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + (b"\x00\x00\x00" * size) for _ in range(size))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )

    if size == 512:
        _BLACK_SQUARE_PNG = png

    return png


def profile_photo_source(photo_id: str | None):
    photo_id = (photo_id or "").strip()

    if not photo_id:
        return None

    if photo_id == BLACK_SQUARE_PHOTO:
        return BufferedInputFile(
            black_square_png(),
            filename="black.png",
        )

    return photo_id


# ============================================================
# СОСТОЯНИЯ
# ============================================================

class ProfileForm(StatesGroup):
    nickname = State()
    gender = State()
    age = State()
    pubg_id = State()
    server = State()
    mode = State()
    rank = State()
    collection = State()
    voice = State()
    schedule = State()
    language = State()
    bio = State()
    photo_file_id = State()


class AdminProfileForm(StatesGroup):
    user_id = State()
    nickname = State()
    gender = State()
    age = State()
    pubg_id = State()
    server = State()
    mode = State()
    rank = State()
    collection = State()
    voice = State()
    schedule = State()
    language = State()
    bio = State()
    photo_file_id = State()


class AdminMessageForm(StatesGroup):
    text = State()
    photo = State()


class BroadcastForm(StatesGroup):
    content = State()


class ChannelForm(StatesGroup):
    links = State()


class PremiumAdminForm(StatesGroup):
    query = State()


class SuperAdminForm(StatesGroup):
    admin_query = State()


class TikTokForm(StatesGroup):
    link = State()


class ProblemForm(StatesGroup):
    description = State()


PROFILE_FIELDS = [
    "nickname",
    "gender",
    "age",
    "pubg_id",
    "server",
    "mode",
    "rank",
    "collection",
    "voice",
    "schedule",
    "language",
    "bio",
    "photo_file_id",
]

PROFILE_STATES = {
    "nickname": ProfileForm.nickname,
    "gender": ProfileForm.gender,
    "age": ProfileForm.age,
    "pubg_id": ProfileForm.pubg_id,
    "server": ProfileForm.server,
    "mode": ProfileForm.mode,
    "rank": ProfileForm.rank,
    "collection": ProfileForm.collection,
    "voice": ProfileForm.voice,
    "schedule": ProfileForm.schedule,
    "language": ProfileForm.language,
    "bio": ProfileForm.bio,
    "photo_file_id": ProfileForm.photo_file_id,
}

ADMIN_PROFILE_STATES = {
    "nickname": AdminProfileForm.nickname,
    "gender": AdminProfileForm.gender,
    "age": AdminProfileForm.age,
    "pubg_id": AdminProfileForm.pubg_id,
    "server": AdminProfileForm.server,
    "mode": AdminProfileForm.mode,
    "rank": AdminProfileForm.rank,
    "collection": AdminProfileForm.collection,
    "voice": AdminProfileForm.voice,
    "schedule": AdminProfileForm.schedule,
    "language": AdminProfileForm.language,
    "bio": AdminProfileForm.bio,
    "photo_file_id": AdminProfileForm.photo_file_id,
}

MULTI_SELECT_FIELDS = {
    "mode",
    "schedule",
}


FIELD_NAMES = {
    "nickname": "Никнейм",
    "gender": "Пол",
    "age": "Возраст",
    "pubg_id": "PUBG ID",
    "server": "Сервер",
    "mode": "Игровой режим",
    "rank": "Ранг",
    "collection": "Уровень коллекции",
    "voice": "Микрофон",
    "schedule": "Время игры",
    "language": "Язык общения",
    "bio": "О себе",
    "photo_file_id": "Фото",
}


AGE_MIN = 6
AGE_MAX = 66
COLLECTION_MIN = 0


def normalize_collection(value: str) -> str | None:
    raw = str(value).strip()

    if not raw.isdigit():
        return None

    number = int(raw)

    if number < COLLECTION_MIN or number > COLLECTION_MAX:
        return None

    return str(number)

GENDER_VALUES = {
    "мужской": "Мужской",
    "женский": "Женский",
}


def normalize_gender(value: str) -> str | None:
    return GENDER_VALUES.get(str(value).strip().casefold())


def normalize_age(value: str) -> str | None:
    raw = str(value).strip()

    if not raw.isdigit():
        return None

    number = int(raw)

    if number < AGE_MIN or number > AGE_MAX:
        return None

    return str(number)


def gender_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="мужской"),
                KeyboardButton(text="женский"),
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


FIELD_OPTIONS = {
    "gender": [
        "Мужской",
        "Женский",
    ],
    "server": [
        "Европа",
        "Азия",
        "Северная Корея",
    ],
    "mode": [
        "Дуо",
        "Сквады",
        "Метро",
        "ТДМ",
        "Танцы",
    ],
    "voice": [
        "Да",
        "Нет",
    ],
    "schedule": [
        "Утро",
        "День",
        "Вечер",
        "Ночь",
        "Круглосуточно",
    ],
}


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
DB = sqlite3.connect(DB_FILE)
DB.row_factory = sqlite3.Row


def current_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_database() -> None:
    DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            nickname TEXT NOT NULL,
            gender TEXT,
            age TEXT,
            pubg_id TEXT NOT NULL,
            server TEXT NOT NULL,
            mode TEXT NOT NULL,
            rank TEXT NOT NULL,
            collection TEXT,
            voice TEXT NOT NULL,
            schedule TEXT NOT NULL,
            language TEXT NOT NULL,
            bio TEXT NOT NULL,
            photo_file_id TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            updated TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS swipes (
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            action TEXT NOT NULL,
            created TEXT NOT NULL,
            PRIMARY KEY (from_user, to_user)
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter INTEGER NOT NULL,
            reported INTEGER NOT NULL,
            created TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS blocks (
            blocker INTEGER NOT NULL,
            blocked INTEGER NOT NULL,
            created TEXT NOT NULL,
            PRIMARY KEY (blocker, blocked)
        );

        CREATE TABLE IF NOT EXISTS bot_messages (
            message_key TEXT PRIMARY KEY,
            message_text TEXT NOT NULL,
            photo_file_id TEXT
        );

        CREATE TABLE IF NOT EXISTS required_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            title TEXT,
            url TEXT NOT NULL UNIQUE,
            created TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS referrals (
            referred_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            created TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_referrals_referrer
            ON referrals(referrer_id);

        CREATE TABLE IF NOT EXISTS premium_status (
            user_id INTEGER PRIMARY KEY,
            override INTEGER NOT NULL,
            updated TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS premium_referral (
            user_id INTEGER PRIMARY KEY,
            premium_until TEXT,
            referrals_used INTEGER NOT NULL DEFAULT 0,
            updated TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reengage_log (
            user_id INTEGER PRIMARY KEY,
            sent TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            appointed_by INTEGER,
            created TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS revoked_env_admins (
            user_id INTEGER PRIMARY KEY,
            revoked_by INTEGER,
            created TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS username_view_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            admin_username TEXT,
            target_id INTEGER NOT NULL,
            target_username TEXT,
            profile_nickname TEXT,
            created TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tiktok_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            link TEXT NOT NULL,
            created TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tiktok_user
            ON tiktok_submissions(user_id);

        CREATE TABLE IF NOT EXISTS pending_referrals (
            referred_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            created TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_views (
            user_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            PRIMARY KEY (user_id, target_id, day)
        );

        CREATE TABLE IF NOT EXISTS problems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            text TEXT NOT NULL,
            created TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_problems_created
            ON problems(created);

        CREATE TABLE IF NOT EXISTS problem_view_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            admin_username TEXT,
            problem_id INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            reporter_username TEXT,
            problem_text TEXT,
            created TEXT NOT NULL
        );
        """
    )

    columns = {
        row["name"]
        for row in DB.execute(
            "PRAGMA table_info(profiles)"
        ).fetchall()
    }

    if "photo_file_id" not in columns:
        DB.execute(
            "ALTER TABLE profiles ADD COLUMN photo_file_id TEXT"
        )

    if "gender" not in columns:
        DB.execute(
            "ALTER TABLE profiles ADD COLUMN gender TEXT"
        )

    if "age" not in columns:
        DB.execute(
            "ALTER TABLE profiles ADD COLUMN age TEXT"
        )

    if "boosted" not in columns:
        DB.execute(
            "ALTER TABLE profiles ADD COLUMN boosted INTEGER NOT NULL DEFAULT 0"
        )

    if "collection" not in columns:
        DB.execute(
            "ALTER TABLE profiles ADD COLUMN collection TEXT"
        )

    message_columns = {
        row["name"]
        for row in DB.execute(
            "PRAGMA table_info(bot_messages)"
        ).fetchall()
    }

    # Старые базы создавались без колонки фото у шаблонов.
    # INSERT с photo_file_id тогда падал, и картинка не сохранялась.
    if message_columns and "photo_file_id" not in message_columns:
        DB.execute(
            "ALTER TABLE bot_messages ADD COLUMN photo_file_id TEXT"
        )

    DB.commit()


def get_profile(user_id: int):
    return DB.execute(
        """
        SELECT *
        FROM profiles
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()


def is_env_admin_revoked(user_id: int) -> bool:
    try:
        row = DB.execute(
            "SELECT 1 FROM revoked_env_admins WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False

    return row is not None


def clear_env_admin_revocation(user_id: int) -> None:
    try:
        DB.execute(
            "DELETE FROM revoked_env_admins WHERE user_id = ?",
            (user_id,),
        )
    except sqlite3.OperationalError:
        return


def revoke_env_admin(user_id: int, revoked_by: int) -> None:
    DB.execute(
        """
        INSERT INTO revoked_env_admins (
            user_id,
            revoked_by,
            created
        )
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            revoked_by = excluded.revoked_by,
            created = excluded.created
        """,
        (user_id, revoked_by, current_time()),
    )
    DB.commit()


def is_superadmin(user_id: int) -> bool:
    if user_id in SUPERADMIN_IDS:
        return True

    if SUPERADMIN_IDS:
        return False

    return user_id in ADMIN_IDS and not is_env_admin_revoked(user_id)


def is_db_admin(user_id: int) -> bool:
    try:
        row = DB.execute(
            "SELECT 1 FROM admins WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False

    return row is not None


def is_admin(user_id: int) -> bool:
    if is_superadmin(user_id):
        return True

    if user_id in ADMIN_IDS and not is_env_admin_revoked(user_id):
        return True

    return is_db_admin(user_id)


def get_user_row(user_id: int):
    return DB.execute(
        """
        SELECT *
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()


def stored_username(user_id: int) -> str:
    row = get_user_row(user_id)

    if not row:
        return ""

    return str(row["username"] or "").strip()


def grant_admin(user_id: int, appointed_by: int) -> None:
    clear_env_admin_revocation(user_id)
    DB.execute(
        """
        INSERT INTO admins (
            user_id,
            appointed_by,
            created
        )
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            appointed_by = excluded.appointed_by,
            created = excluded.created
        """,
        (user_id, appointed_by, current_time()),
    )
    DB.commit()


def revoke_admin(user_id: int) -> None:
    DB.execute(
        "DELETE FROM admins WHERE user_id = ?",
        (user_id,),
    )
    DB.commit()


def users_blocked(first_id: int, second_id: int) -> bool:
    row = DB.execute(
        """
        SELECT 1
        FROM blocks
        WHERE
            (blocker = ? AND blocked = ?)
            OR
            (blocker = ? AND blocked = ?)
        """,
        (first_id, second_id, second_id, first_id),
    ).fetchone()

    return row is not None


def record_swipe(from_user: int, to_user: int, action: str) -> None:
    DB.execute(
        """
        INSERT INTO swipes (
            from_user,
            to_user,
            action,
            created
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(from_user, to_user) DO UPDATE SET
            action = excluded.action,
            created = excluded.created
        """,
        (from_user, to_user, action, current_time()),
    )
    DB.commit()


_LINK_RE = re.compile(
    r"^(?:https?://\S+|www\.\S+|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/\S*)?)$",
    re.IGNORECASE,
)


def extract_submission_links(text: str) -> list[str] | None:
    raw = (text or "").strip()

    if not raw or raw.startswith("/"):
        return None

    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    if not lines:
        return None

    links = []

    for line in lines:
        if not _LINK_RE.fullmatch(line):
            return None

        if not line.lower().startswith(("http://", "https://")):
            line = "https://" + line

        links.append(line)

    return links


def add_tiktok_submission(user_id: int, link: str) -> bool:
    existing = DB.execute(
        """
        SELECT 1
        FROM tiktok_submissions
        WHERE user_id = ? AND link = ?
        """,
        (user_id, link),
    ).fetchone()

    if existing:
        return False

    DB.execute(
        """
        INSERT INTO tiktok_submissions (
            user_id,
            link,
            created
        )
        VALUES (?, ?, ?)
        """,
        (user_id, link, current_time()),
    )
    DB.commit()
    return True


def log_username_view(
    admin_id: int,
    admin_username: str,
    target_id: int,
    target_username: str,
    nickname: str,
) -> None:
    DB.execute(
        """
        INSERT INTO username_view_logs (
            admin_id,
            admin_username,
            target_id,
            target_username,
            profile_nickname,
            created
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            admin_id,
            admin_username or None,
            target_id,
            target_username or None,
            nickname or None,
            current_time(),
        ),
    )
    DB.commit()


def add_problem(user_id: int, username: str, text: str) -> int:
    cursor = DB.execute(
        """
        INSERT INTO problems (
            user_id,
            username,
            text,
            created
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            (username or "").strip() or None,
            text,
            current_time(),
        ),
    )
    DB.commit()
    return int(cursor.lastrowid)


def get_problem(problem_id: int):
    return DB.execute(
        """
        SELECT *
        FROM problems
        WHERE id = ?
        """,
        (problem_id,),
    ).fetchone()


def problem_sender_tag(user_id: int, snapshot: str | None) -> str:
    username = stored_username(user_id) or str(snapshot or "").strip()

    if username:
        return "@" + username

    return f"id {user_id}"


def problem_list_label(row) -> str:
    tag = problem_sender_tag(
        int(row["user_id"]),
        row["username"],
    )
    body = " ".join(str(row["text"] or "").split())
    return f"{tag}: {body}"[:64]


def log_problem_view(
    admin_id: int,
    admin_username: str,
    problem,
) -> None:
    reporter_id = int(problem["user_id"])
    reporter_username = stored_username(reporter_id) or str(
        problem["username"] or ""
    ).strip()
    DB.execute(
        """
        INSERT INTO problem_view_logs (
            admin_id,
            admin_username,
            problem_id,
            reporter_id,
            reporter_username,
            problem_text,
            created
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            admin_id,
            admin_username or None,
            int(problem["id"]),
            reporter_id,
            reporter_username or None,
            str(problem["text"] or ""),
            current_time(),
        ),
    )
    DB.commit()


def delete_profile_completely(user_id: int) -> None:
    """
    Общее удаление анкеты.
    Используется и пользователем, и администратором.
    """
    try:
        DB.execute(
            "DELETE FROM profiles WHERE user_id = ?",
            (user_id,),
        )

        DB.execute(
            """
            DELETE FROM swipes
            WHERE from_user = ? OR to_user = ?
            """,
            (user_id, user_id),
        )

        DB.execute(
            """
            DELETE FROM blocks
            WHERE blocker = ? OR blocked = ?
            """,
            (user_id, user_id),
        )

        DB.execute(
            """
            DELETE FROM reports
            WHERE reporter = ? OR reported = ?
            """,
            (user_id, user_id),
        )

        DB.commit()
    except Exception:
        DB.rollback()
        raise

    if get_profile(user_id):
        raise RuntimeError(
            "Анкета осталась в базе после удаления."
        )


# ============================================================
# ПОЛЬЗОВАТЕЛИ, РАССЫЛКА, ОБЯЗАТЕЛЬНАЯ ПОДПИСКА
# ============================================================

_BOT_ID: int | None = None

_INVITE_RE = re.compile(
    r"((?:https?://)?(?:t|telegram)\.me/(?:\+|joinchat/)[A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)
_PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?([A-Za-z0-9_]{4,})",
    re.IGNORECASE,
)
_AT_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_]{4,})")
_PUBLIC_SKIP = {
    "joinchat",
    "share",
    "addstickers",
    "addtheme",
    "proxy",
    "socks",
    "login",
    "boost",
}


def remember_user(user) -> bool:
    if user is None or getattr(user, "is_bot", False):
        return False

    now = current_time()

    try:
        existing = DB.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user.id,),
        ).fetchone()
        DB.execute(
            """
            INSERT INTO users (
                user_id,
                username,
                first_name,
                created,
                last_seen
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen = excluded.last_seen
            """,
            (
                user.id,
                user.username,
                user.first_name,
                now,
                now,
            ),
        )
        DB.commit()
        return existing is None
    except Exception as error:
        DB.rollback()
        print(f"Не удалось запомнить пользователя: {error}")
        return False


REFERRAL_GOAL = 3
PREMIUM_DAYS = 30
MSK = timezone(timedelta(hours=3))
SEARCH_GENDERS = {
    "m": "Мужской",
    "f": "Женский",
}
_SEARCH_GENDER: dict[int, str] = {}
_BOT_USERNAME: str | None = None


def referrer_id_from_start(message) -> int | None:
    text = (getattr(message, "text", None) or "").strip()
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        return None

    command = parts[0].split("@", 1)[0]

    if command != "/start":
        return None

    payload = parts[1].strip().split()[0]

    if not payload.startswith("ref_"):
        return None

    raw = payload[4:]

    if not raw.isdigit():
        return None

    referrer_id = int(raw)

    if referrer_id <= 0:
        return None

    return referrer_id


def referral_count(user_id: int) -> int:
    row = DB.execute(
        """
        SELECT COUNT(*) AS count
        FROM referrals
        WHERE referrer_id = ?
        """,
        (user_id,),
    ).fetchone()
    return int(row["count"])


def premium_override(user_id: int) -> int | None:
    row = DB.execute(
        """
        SELECT override
        FROM premium_status
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    if not row:
        return None

    return int(row["override"])


def has_premium(user_id: int) -> bool:
    return premium_state(user_id)["active"]


def parse_db_time(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value))

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc).replace(microsecond=0)


def format_db_time(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )


def format_premium_until(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m.%Y, %H:%M МСК")


def format_msk(value: str) -> str:
    try:
        return format_premium_until(parse_db_time(value))
    except Exception:
        return str(value)


def premium_db_now() -> str:
    return format_db_time(datetime.now(timezone.utc))


def compute_referral_premium_until(user_id: int) -> datetime | None:
    rows = DB.execute(
        """
        SELECT created
        FROM referrals
        WHERE referrer_id = ?
        ORDER BY created, referred_id
        """,
        (user_id,),
    ).fetchall()

    until = None

    for index, row in enumerate(rows, start=1):
        if index % REFERRAL_GOAL != 0:
            continue

        try:
            granted_at = parse_db_time(row["created"])
        except Exception:
            granted_at = datetime.now(timezone.utc).replace(microsecond=0)

        base = granted_at

        if until is not None and until > granted_at:
            base = until

        until = base + timedelta(days=PREMIUM_DAYS)

    return until


def referral_progress(user_id: int) -> int:
    return referral_count(user_id) % REFERRAL_GOAL


def sync_referral_premium(user_id: int) -> tuple[datetime | None, str]:
    """
    Каждые 3 приглашения дают месяц.
    Месяцы суммируются, если приглашения пришли раньше окончания срока.
    После конца срока старые приглашения больше не держат Premium.
    """
    until = compute_referral_premium_until(user_id)
    used = (referral_count(user_id) // REFERRAL_GOAL) * REFERRAL_GOAL
    until_text = format_db_time(until) if until is not None else None
    now = datetime.now(timezone.utc).replace(microsecond=0)

    stored = DB.execute(
        """
        SELECT premium_until, referrals_used
        FROM premium_referral
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    old_until = None

    if stored and stored["premium_until"]:
        try:
            old_until = parse_db_time(stored["premium_until"])
        except Exception:
            old_until = None

    if until is not None and until > now and (
        old_until is None or old_until <= now
    ):
        change = "gained"
    elif (
        until is not None
        and old_until is not None
        and until > old_until
        and old_until > now
    ):
        change = "extended"
    else:
        change = "none"

    stored_until = stored["premium_until"] if stored else None
    stored_used = int(stored["referrals_used"]) if stored else None
    same = (
        stored is not None
        and stored_until == until_text
        and stored_used == used
    )

    if same or (stored is None and until_text is None and used == 0):
        return until, change

    try:
        DB.execute(
            """
            INSERT INTO premium_referral (
                user_id,
                premium_until,
                referrals_used,
                updated
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                premium_until = excluded.premium_until,
                referrals_used = excluded.referrals_used,
                updated = excluded.updated
            """,
            (
                user_id,
                until_text,
                used,
                current_time(),
            ),
        )
        DB.commit()
    except Exception:
        DB.rollback()
        raise

    return until, change


def drop_boost(user_id: int) -> None:
    profile = get_profile(user_id)

    if not profile or not int(profile["boosted"] or 0):
        return

    try:
        DB.execute(
            """
            UPDATE profiles
            SET boosted = 0
            WHERE user_id = ?
            """,
            (user_id,),
        )
        DB.commit()
    except Exception:
        DB.rollback()
        raise


def premium_state(user_id: int) -> dict:
    override = premium_override(user_id)
    until, _change = sync_referral_premium(user_id)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    referral_active = until is not None and until > now

    if override is None:
        kind = "referral" if referral_active else "none"
    elif override == 1:
        kind = "admin"
    else:
        kind = "revoked"

    active = kind in {"admin", "referral"}

    if not active:
        drop_boost(user_id)
        _SEARCH_GENDER.pop(user_id, None)

    progress = referral_progress(user_id)

    return {
        "active": active,
        "kind": kind,
        "until": until if referral_active else None,
        "progress": progress,
        "left": REFERRAL_GOAL - progress,
    }


def sync_all_referral_premium() -> None:
    rows = DB.execute(
        """
        SELECT DISTINCT referrer_id AS user_id
        FROM referrals
        UNION
        SELECT user_id
        FROM profiles
        WHERE IFNULL(boosted, 0) = 1
        """
    ).fetchall()

    for row in rows:
        premium_state(int(row["user_id"]))


def set_premium_override(user_id: int, enabled: bool) -> None:
    try:
        DB.execute(
            """
            INSERT INTO premium_status (
                user_id,
                override,
                updated
            )
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                override = excluded.override,
                updated = excluded.updated
            """,
            (
                user_id,
                1 if enabled else 0,
                current_time(),
            ),
        )

        if not enabled:
            DB.execute(
                """
                UPDATE profiles
                SET boosted = 0
                WHERE user_id = ?
                """,
                (user_id,),
            )

        DB.commit()
    except Exception:
        DB.rollback()
        raise

    if not enabled:
        _SEARCH_GENDER.pop(user_id, None)


def add_referral(referred_id: int, referrer_id: int) -> bool:
    if (
        referred_id <= 0
        or referrer_id <= 0
        or referred_id == referrer_id
    ):
        return False

    referrer = DB.execute(
        """
        SELECT 1
        FROM users
        WHERE user_id = ?
        """,
        (referrer_id,),
    ).fetchone()

    if not referrer:
        return False

    try:
        DB.execute(
            """
            INSERT INTO referrals (
                referred_id,
                referrer_id,
                created
            )
            VALUES (?, ?, ?)
            """,
            (
                referred_id,
                referrer_id,
                current_time(),
            ),
        )
        DB.commit()
    except sqlite3.IntegrityError:
        DB.rollback()
        return False
    except Exception:
        DB.rollback()
        raise

    return True


def add_pending_referral(referred_id: int, referrer_id: int) -> bool:
    """Реферал ждёт подписки и ещё не идёт в зачёт Premium."""
    if (
        referred_id <= 0
        or referrer_id <= 0
        or referred_id == referrer_id
    ):
        return False

    already = DB.execute(
        """
        SELECT 1
        FROM referrals
        WHERE referred_id = ?
        """,
        (referred_id,),
    ).fetchone()

    if already:
        return False

    referrer = DB.execute(
        """
        SELECT 1
        FROM users
        WHERE user_id = ?
        """,
        (referrer_id,),
    ).fetchone()

    if not referrer:
        return False

    try:
        DB.execute(
            """
            INSERT INTO pending_referrals (
                referred_id,
                referrer_id,
                created
            )
            VALUES (?, ?, ?)
            ON CONFLICT(referred_id) DO NOTHING
            """,
            (
                referred_id,
                referrer_id,
                current_time(),
            ),
        )
        DB.commit()
    except Exception:
        DB.rollback()
        raise

    return True


async def maybe_activate_referral(
    bot: Bot,
    user_id: int,
    subscription_ok: bool | None = None,
) -> None:
    """
    Активный реферал — тот, кто подписался на каналы
    обязательной подписки. Без каналов зачёт сразу.
    """
    row = DB.execute(
        """
        SELECT referrer_id
        FROM pending_referrals
        WHERE referred_id = ?
        """,
        (user_id,),
    ).fetchone()

    if not row:
        return

    if list_required_channels():
        if subscription_ok is not True:
            try:
                missing = await missing_channels(bot, user_id)
            except Exception as error:
                print(
                    f"Реферал {user_id}: "
                    f"не удалось проверить подписку: {error}"
                )
                return

            if missing:
                return

    referrer_id = int(row["referrer_id"])

    try:
        counted = add_referral(user_id, referrer_id)
    except Exception as error:
        print(f"Реферал не записан: {error}")
        return

    try:
        DB.execute(
            """
            DELETE FROM pending_referrals
            WHERE referred_id = ?
            """,
            (user_id,),
        )
        DB.commit()
    except Exception as error:
        DB.rollback()
        print(
            f"Не удалось закрыть ожидание реферала "
            f"{user_id}: {error}"
        )
        return

    if counted:
        await notify_referrer(bot, referrer_id)


async def notify_referrer(bot: Bot, referrer_id: int) -> None:
    until, change = sync_referral_premium(referrer_id)
    progress = referral_progress(referrer_id)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    opened = (
        change in {"gained", "extended"}
        and until is not None
        and until > now
        and premium_override(referrer_id) is None
    )

    if opened:
        verb = "продлён" if change == "extended" else "открыт"
        text = (
            f"Premium {verb} до {format_premium_until(until)}.\n"
            "На месяц в главном меню доступны поиск по полу "
            "и поднятие анкеты в топ.\n"
            "Команда: /prem"
        )
    else:
        if list_required_channels():
            text = (
                "Приглашённый подписался на каналы "
                "обязательной подписки.\n"
                f"Прогресс: {progress} из {REFERRAL_GOAL}."
            )
        else:
            text = (
                "По вашей ссылке в бота зашёл новый человек.\n"
                f"Прогресс: {progress} из {REFERRAL_GOAL}."
            )

    try:
        await bot.send_message(
            referrer_id,
            text,
            reply_markup=keyboard_for(referrer_id),
        )
    except Exception as error:
        print(f"Не удалось сообщить рефереру {referrer_id}: {error}")


async def notify_premium_changed(
    bot: Bot,
    user_id: int,
    enabled: bool,
) -> None:
    if enabled:
        text = (
            "Вам выдан Premium без ограничения срока.\n"
            "В главном меню доступны поиск по полу "
            "и поднятие анкеты в топ.\n"
            "Команда: /prem"
        )
    else:
        text = (
            "Premium отключён.\n"
            "Поиск по полу и поднятие анкеты снова недоступны."
        )

    try:
        await bot.send_message(
            user_id,
            text,
            reply_markup=keyboard_for(user_id),
        )
    except Exception as error:
        print(f"Не удалось сообщить о Premium {user_id}: {error}")


async def attribute_start_referral(event, data: dict) -> None:
    if not data.get("new_user") or not isinstance(event, Message):
        return

    user = getattr(event, "from_user", None)

    if user is None:
        return

    referrer_id = referrer_id_from_start(event)

    if not referrer_id:
        return

    try:
        add_pending_referral(user.id, referrer_id)
    except Exception as error:
        print(f"Реферал не записан: {error}")


async def bot_username(bot: Bot) -> str:
    global _BOT_USERNAME

    if not _BOT_USERNAME:
        me = await bot.get_me()
        _BOT_USERNAME = me.username or ""

    return _BOT_USERNAME


async def referral_link(bot: Bot, user_id: int) -> str:
    username = await bot_username(bot)
    return f"https://t.me/{username}?start=ref_{user_id}"


def share_link(link: str) -> str:
    text = "Заходи в бота, ищем напарника в PUBG"
    return (
        "https://t.me/share/url?url="
        + quote(link, safe="")
        + "&text="
        + quote(text, safe="")
    )


def boost_order_sql() -> str:
    return """
        ORDER BY (
            CASE
                WHEN IFNULL(p.boosted, 0) = 0 THEN 0
                WHEN EXISTS (
                    SELECT 1
                    FROM premium_status ps
                    WHERE ps.user_id = p.user_id
                      AND ps.override = 0
                ) THEN 0
                WHEN EXISTS (
                    SELECT 1
                    FROM premium_status ps
                    WHERE ps.user_id = p.user_id
                      AND ps.override = 1
                ) THEN 1
                WHEN EXISTS (
                    SELECT 1
                    FROM premium_referral pr
                    WHERE pr.user_id = p.user_id
                      AND pr.premium_until > ?
                ) THEN 1
                ELSE 0
            END
        ) DESC,
        RANDOM()
    """


def user_plain_label(user_id: int) -> str:
    bits = []
    profile = get_profile(user_id)

    if profile and profile["nickname"]:
        bits.append(str(profile["nickname"]))

    user = DB.execute(
        """
        SELECT username
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    if user and user["username"]:
        bits.append("@" + str(user["username"]))

    bits.append(str(user_id))
    return " · ".join(bits)


def find_premium_targets(text: str) -> list[int]:
    raw = text.strip()

    if not raw:
        return []

    if raw.isdigit():
        user_id = int(raw)
        return [user_id] if user_id > 0 else []

    username = raw[1:] if raw.startswith("@") else raw
    found: list[int] = []
    seen: set[int] = set()

    def add(user_id: int) -> None:
        user_id = int(user_id)

        if user_id > 0 and user_id not in seen:
            seen.add(user_id)
            found.append(user_id)

    if username:
        rows = DB.execute(
            """
            SELECT user_id
            FROM users
            WHERE username = ? COLLATE NOCASE
            """,
            (username,),
        ).fetchall()

        for row in rows:
            add(row["user_id"])

    profiles = DB.execute(
        """
        SELECT user_id, nickname
        FROM profiles
        """
    ).fetchall()
    needle = raw.casefold()
    name_needle = username.casefold()
    exact = []
    partial = []

    for row in profiles:
        name = str(row["nickname"] or "")
        folded = name.casefold()

        if folded == needle or folded == name_needle:
            exact.append(int(row["user_id"]))
        elif name_needle and name_needle in folded:
            partial.append(int(row["user_id"]))

    for user_id in exact or partial:
        add(user_id)

    return found


def broadcast_user_ids() -> list[int]:
    rows = DB.execute(
        """
        SELECT user_id FROM users
        UNION
        SELECT user_id FROM profiles
        UNION
        SELECT from_user AS user_id FROM swipes
        UNION
        SELECT to_user AS user_id FROM swipes
        UNION
        SELECT reporter AS user_id FROM reports
        UNION
        SELECT reported AS user_id FROM reports
        UNION
        SELECT blocker AS user_id FROM blocks
        UNION
        SELECT blocked AS user_id FROM blocks
        """
    ).fetchall()

    found = []
    seen = set()

    for row in rows:
        user_id = row["user_id"]

        if user_id is None:
            continue

        user_id = int(user_id)

        if user_id <= 0 or user_id in seen:
            continue

        seen.add(user_id)
        found.append(user_id)

    return found


def list_required_channels():
    return DB.execute(
        """
        SELECT *
        FROM required_channels
        ORDER BY id
        """
    ).fetchall()


def save_required_channel(
    chat_id: int,
    title: str | None,
    url: str,
) -> None:
    url = url.strip()
    now = current_time()

    try:
        existing = DB.execute(
            """
            SELECT id
            FROM required_channels
            WHERE chat_id = ? OR url = ?
            """,
            (chat_id, url),
        ).fetchone()

        if existing:
            DB.execute(
                """
                UPDATE required_channels
                SET chat_id = ?, title = ?, url = ?
                WHERE id = ?
                """,
                (chat_id, title, url, existing["id"]),
            )
        else:
            DB.execute(
                """
                INSERT INTO required_channels (
                    chat_id,
                    title,
                    url,
                    created
                )
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, title, url, now),
            )

        DB.commit()
    except Exception:
        DB.rollback()
        raise

def delete_required_channel(channel_id: int) -> None:
    DB.execute(
        "DELETE FROM required_channels WHERE id = ?",
        (channel_id,),
    )
    DB.commit()


def normalize_telegram_url(url: str) -> str:
    url = url.strip().split("?")[0].rstrip("/")

    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    elif not url.startswith("https://"):
        url = "https://" + url

    return url


def extract_channel_links(text: str) -> list[dict]:
    result = []
    seen = set()

    def add(url: str, username: str | None, kind: str) -> None:
        url = normalize_telegram_url(url)
        key = url.lower()

        if key in seen:
            return

        seen.add(key)
        result.append(
            {
                "url": url,
                "username": username,
                "kind": kind,
            }
        )

    for match in _INVITE_RE.finditer(text or ""):
        add(match.group(1), None, "invite")

    for match in _PUBLIC_RE.finditer(text or ""):
        username = match.group(1)

        if username.lower() in _PUBLIC_SKIP:
            continue

        add(f"https://t.me/{username}", username, "public")

    for match in _AT_RE.finditer(text or ""):
        username = match.group(1)
        add(f"https://t.me/{username}", username, "public")

    return result


def member_status(member) -> str:
    status = getattr(member, "status", "")
    return str(getattr(status, "value", status))


async def current_bot_id(bot: Bot) -> int:
    global _BOT_ID

    if _BOT_ID is None:
        me = await bot.get_me()
        _BOT_ID = me.id

    return _BOT_ID


async def bot_can_check(bot: Bot, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id,
            await current_bot_id(bot),
        )
    except Exception as error:
        print(f"Бот не может проверить канал {chat_id}: {error}")
        return False

    return member_status(member) in {"administrator", "creator"}


async def is_channel_member(
    bot: Bot,
    chat_id: int,
    user_id: int,
) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as error:
        print(
            f"Проверка подписки {user_id} в {chat_id}: {error}"
        )
        return False

    status = member_status(member)

    if status in {"creator", "administrator", "member"}:
        return True

    if status == "restricted":
        return bool(getattr(member, "is_member", False))

    return False


def chat_type_name(chat) -> str:
    value = getattr(chat, "type", "")
    return str(getattr(value, "value", value))


def forwarded_channel_chat(message: Message):
    origin = getattr(message, "forward_origin", None)

    if origin is not None:
        origin_type = getattr(origin, "type", None)
        origin_type = getattr(origin_type, "value", origin_type)

        if origin_type == "channel":
            return getattr(origin, "chat", None)

    chat = getattr(message, "forward_from_chat", None)

    if chat is not None and chat_type_name(chat) == "channel":
        return chat

    return None


async def missing_channels(
    bot: Bot,
    user_id: int,
):
    rows = list_required_channels()

    if not rows:
        return []

    # Каждый апдейт спрашивает Telegram заново.
    # Кэш «уже подписан» оставлял доступ после отписки.
    missing = []

    for row in rows:
        chat_id = row["chat_id"]

        if not chat_id or not await is_channel_member(
            bot,
            int(chat_id),
            user_id,
        ):
            missing.append(row)

    return missing


def subscription_required_text(rows) -> str:
    links = []

    for row in rows:
        url = html.escape(row["url"], quote=True)
        links.append(f'<a href="{url}">{url}</a>')

    if len(links) == 1:
        return f"Для использования бота подпишись на {links[0]}"

    return "Для использования бота подпишись на:\n" + "\n".join(links)


def subscription_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []

    for index, row in enumerate(rows, start=1):
        title = (row["title"] or "").strip()

        if len(rows) == 1:
            label = "Подписаться"
        elif title:
            label = f"Подписаться: {title}"
        else:
            label = f"Подписаться {index}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    url=row["url"],
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="✅ Проверить подписку",
                callback_data="sub:check",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def answer_subscription(message: Message, rows) -> None:
    text = subscription_required_text(rows)
    markup = subscription_keyboard(rows)

    try:
        await message.answer(text, reply_markup=markup)
    except Exception as error:
        print(f"Не удалось отправить требование подписки: {error}")
        plain = re.sub(r"<[^>]+>", "", text)
        await message.answer(
            plain,
            reply_markup=markup,
            parse_mode=None,
        )


def channels_admin_text(rows) -> str:
    if not rows:
        return (
            "<b>Обязательная подписка</b>\n\n"
            "Каналы не добавлены, бот открывается без подписки.\n"
            "Пришлите одну или несколько ссылок — "
            "без подписки на них бот работать не будет.\n\n"
            "Бот должен быть администратором канала, "
            "иначе Telegram не сообщит, подписан человек или нет."
        )

    lines = [
        "<b>Обязательная подписка</b>",
        "Для использования бота нужна подписка на:",
        "",
    ]

    for row in rows:
        title = html.escape(row["title"] or "Канал")
        url = html.escape(row["url"])
        mark = "🟢" if row["chat_id"] else "⚠️"
        lines.append(f"{mark} {title}\n{url}")

    lines.append("")
    lines.append("Нажмите на канал в списке, чтобы убрать его.")

    return "\n".join(lines)


def channels_admin_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []

    for row in rows:
        label = row["title"] or row["url"]
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {label}"[:64],
                    callback_data=f"admin:channel_del:{row['id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить каналы",
                callback_data="admin:channel_add",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ В админ-панель",
                callback_data="admin:menu",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def try_add_public_channel(
    bot: Bot,
    username: str,
    url: str,
) -> str:
    try:
        chat = await bot.get_chat(f"@{username}")
    except Exception as error:
        return (
            f"❌ {url}\n"
            "Не удалось открыть канал. Проверьте ссылку и "
            "что бот добавлен в администраторы.\n"
            f"{error}"
        )

    if chat_type_name(chat) != "channel":
        return (
            f"❌ {url}\n"
            "Нужна ссылка на канал, а не на человека или чат."
        )

    if not await bot_can_check(bot, chat.id):
        title = chat.title or username
        return (
            f"❌ {title}\n"
            "Бот не администратор этого канала. "
            "Добавьте его в админы и пришлите ссылку снова."
        )

    final_url = (
        f"https://t.me/{chat.username}"
        if chat.username
        else url
    )
    save_required_channel(chat.id, chat.title, final_url)
    return f"✅ Канал «{chat.title}» сохранён."


async def save_from_forward(
    bot: Bot,
    state: FSMContext,
    chat,
    links: list[dict],
) -> tuple[str, str | None]:
    if chat_type_name(chat) != "channel":
        return "Пересланное сообщение не из канала.", None

    if not await bot_can_check(bot, chat.id):
        title = chat.title or "канал"
        return (
            f"Вижу «{title}», но бот там не администратор. "
            "Добавьте бота в админы и перешлите пост ещё раз.",
            None,
        )

    data = await state.get_data()
    username = getattr(chat, "username", None)
    consumed = None

    if username:
        url = f"https://t.me/{username}"
    else:
        invite = next(
            (item["url"] for item in links if item["kind"] == "invite"),
            None,
        )
        url = invite or data.get("pending_invite_url")
        consumed = invite

    if not url:
        await state.update_data(
            pending_chat_id=chat.id,
            pending_title=chat.title,
        )
        return (
            f"Канал «{chat.title}» вижу, он приватный. "
            "Пришлите ссылку-приглашение вида https://t.me/+...",
            None,
        )

    save_required_channel(chat.id, chat.title, url)
    await state.update_data(
        pending_chat_id=None,
        pending_title=None,
        pending_invite_url=None,
    )
    return f"✅ Канал «{chat.title}» сохранён.", consumed


ADMIN_STATE_PREFIXES = (
    "AdminProfileForm:",
    "AdminMessageForm:",
    "BroadcastForm:",
    "ChannelForm:",
    "PremiumAdminForm:",
    "SuperAdminForm:",
)


async def admin_configuration_event(event, data: dict) -> bool:
    """Админ может настраивать бота, даже если сам ещё не подписан."""
    user = getattr(event, "from_user", None)

    if user is None or not is_admin(user.id):
        return False

    if isinstance(event, CallbackQuery):
        payload = event.data or ""

        if payload.startswith("super:"):
            return is_superadmin(user.id)

        return payload.startswith((
            "admin:",
            "admin_message:",
            "broadcast:",
        ))

    if not isinstance(event, Message):
        return False

    text = (event.text or "").strip()

    if text.startswith("/"):
        command = text.split(maxsplit=1)[0].split("@", 1)[0]

        if command == "/superadmin":
            return is_superadmin(user.id)

        if command in {"/admin", "/cancel"}:
            return True

    raw_state = data.get("raw_state")
    state = data.get("state")

    if raw_state is None and state is not None:
        raw_state = await state.get_state()

    return bool(raw_state) and str(raw_state).startswith(
        ADMIN_STATE_PREFIXES
    )


SPAM_INTERVAL = 0.7
SPAM_WARN_INTERVAL = 3.0
_LAST_USER_ACTION: dict[int, float] = {}
_LAST_SPAM_WARN: dict[int, float] = {}


def spam_limited(user_id: int) -> bool:
    """True, если с прошлого сообщения или команды прошло меньше 0.7 с."""
    now = time.monotonic()
    previous = _LAST_USER_ACTION.get(user_id)
    _LAST_USER_ACTION[user_id] = now

    if previous is None:
        return False

    return (now - previous) < SPAM_INTERVAL


async def answer_spam(message: Message, user_id: int) -> None:
    now = time.monotonic()
    last_warn = _LAST_SPAM_WARN.get(user_id, 0.0)

    if now - last_warn < SPAM_WARN_INTERVAL:
        return

    _LAST_SPAM_WARN[user_id] = now

    try:
        await message.answer(
            "Слишком часто. Подождите немного.",
            parse_mode=None,
        )
    except Exception as error:
        print(f"Антиспам {user_id}: {error}")


class GateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler,
        event: TelegramObject,
        data: dict,
    ):
        user = getattr(event, "from_user", None)
        user_id = None

        if user is not None and not getattr(user, "is_bot", False):
            user_id = user.id

        if (
            isinstance(event, Message)
            and user_id is not None
            and spam_limited(user_id)
        ):
            await answer_spam(event, user_id)
            return None

        new_user = False

        if user_id is not None:
            new_user = remember_user(user)

        data["new_user"] = new_user
        await attribute_start_referral(event, data)

        user_id = user.id if user is not None else None

        async def activate_referral(
            subscription_ok: bool | None = None,
        ) -> None:
            if user_id is None:
                return

            bot = data.get("bot") or getattr(event, "bot", None)

            if bot is None:
                return

            try:
                await maybe_activate_referral(
                    bot,
                    user_id,
                    subscription_ok=subscription_ok,
                )
            except Exception as error:
                print(f"Реферал не активирован {user_id}: {error}")

        if user_id is None or await admin_configuration_event(event, data):
            await activate_referral()
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and (
            event.data or ""
        ).startswith("sub:"):
            await activate_referral()
            return await handler(event, data)

        bot = data.get("bot") or getattr(event, "bot", None)

        if bot is None:
            await activate_referral()
            return await handler(event, data)

        try:
            missing = await missing_channels(bot, user_id)
        except Exception as error:
            print(f"Ошибка проверки подписки: {error}")
            await activate_referral()
            return await handler(event, data)

        if not missing:
            await activate_referral(subscription_ok=True)
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer(
                "Вы не подписались",
                show_alert=True,
            )

            if event.message:
                await answer_subscription(event.message, missing)

            return None

        if isinstance(event, Message):
            await answer_subscription(event, missing)
            return None

        return await handler(event, data)


# ============================================================
# ШАБЛОННЫЕ СООБЩЕНИЯ
# ============================================================

DEFAULT_MESSAGES = {
    "start": (
        "Привет! Это бот для поиска напарников в PUBG.\n\n"
        "Создайте анкету и начинайте поиск."
    ),
    "help": (
        "Создайте анкету, после чего нажмите "
        "«🔍 Найти игрока».\n\n"
        "Если вы оба поставите лайк, бот сообщит "
        "о совпадении.\n\n"
        "Premium на месяц за 3 приглашения: /prem"
    ),
    "no_profiles": (
        "Подходящих анкет пока нет. Попробуйте позже."
    ),
    "profile_saved": (
        "✅ Анкета сохранена."
    ),
    "premium": (
        "За 3 приглашения на месяц открывается:\n"
        "• Поиск по полу\n"
        "• Поднятие анкеты в топ (ваша анкета будет "
        "отображаться первее остальных)\n\n"
        "В зачёт идут только новые люди, которые впервые "
        "открыли бота по вашей ссылке и подписались "
        "на каналы обязательной подписки."
    ),
    "tiktok": "тест",
    "reengage": (
        "Вы давно не заходили.\n\n"
        "Анкеты напарников в PUBG уже ждут — "
        "откройте бота и нажмите «🔍 Найти игрока»."
    ),
}


def get_message_row(key: str):
    return DB.execute(
        """
        SELECT *
        FROM bot_messages
        WHERE message_key = ?
        """,
        (key,),
    ).fetchone()


def get_message_text(key: str) -> str:
    row = get_message_row(key)

    if row:
        return row["message_text"]

    return DEFAULT_MESSAGES.get(key, "")


def save_message(
    key: str,
    text: str,
    photo_file_id: str | None,
) -> None:
    photo_file_id = (photo_file_id or "").strip() or None

    try:
        DB.execute(
            """
            INSERT INTO bot_messages (
                message_key,
                message_text,
                photo_file_id
            )
            VALUES (?, ?, ?)
            ON CONFLICT(message_key) DO UPDATE SET
                message_text = excluded.message_text,
                photo_file_id = excluded.photo_file_id
            """,
            (key, text, photo_file_id),
        )
        DB.commit()
    except Exception:
        DB.rollback()
        raise

    stored = get_message_row(key)
    if not stored or stored["message_text"] != text:
        raise RuntimeError("Текст шаблона не сохранился.")

    stored_photo = (stored["photo_file_id"] or "").strip() or None
    if stored_photo != photo_file_id:
        raise RuntimeError(
            "Фото шаблона не сохранилось. "
            "Перезапустите бота и повторите загрузку."
        )


def stored_media(value) -> tuple[str | None, str | None]:
    if value is None:
        return None, None

    raw = str(value).strip()
    if not raw:
        return None, None

    # Файл, отправленный «как документ», нельзя показать через sendPhoto.
    if raw.startswith("document:"):
        file_id = raw[len("document:"):]
        if not file_id:
            return None, None
        return "document", file_id

    return "photo", raw


async def answer_text(
    message: Message,
    text: str,
    reply_markup=None,
) -> None:
    try:
        await message.answer(
            text,
            reply_markup=reply_markup,
        )
    except Exception as error:
        print(f"Не удалось отправить HTML-текст: {error}")
        await message.answer(
            text,
            reply_markup=reply_markup,
            parse_mode=None,
        )


async def send_saved_media(
    message: Message,
    media_value,
    text: str,
    reply_markup=None,
) -> bool:
    """
    Отправляет шаблон вместе с фото.
    True — фото дошло (или его не было).
    False — фото было, но Telegram его отклонил, ушёл только текст.
    """
    kind, file_id = stored_media(media_value)

    if not file_id:
        await answer_text(message, text, reply_markup)
        return True

    caption = text if text and len(text) <= 1024 else None
    media_markup = reply_markup if caption is not None else None

    for parse_mode in (ParseMode.HTML, None):
        try:
            if kind == "document":
                await message.answer_document(
                    document=file_id,
                    caption=caption,
                    reply_markup=media_markup,
                    parse_mode=parse_mode,
                )
            else:
                await message.answer_photo(
                    photo=file_id,
                    caption=caption,
                    reply_markup=media_markup,
                    parse_mode=parse_mode,
                )

            if caption is None:
                await answer_text(message, text, reply_markup)

            return True
        except Exception as error:
            print(f"Не удалось отправить медиа шаблона: {error}")

    await answer_text(message, text, reply_markup)
    return False


async def send_template(
    message: Message,
    key: str,
    reply_markup=None,
) -> bool:
    row = get_message_row(key)

    if row:
        text = row["message_text"]
        media = row["photo_file_id"]
    else:
        text = DEFAULT_MESSAGES.get(key, "")
        media = None

    if media:
        return await send_saved_media(
            message,
            media,
            text,
            reply_markup,
        )

    await answer_text(message, text, reply_markup)
    return True


async def deliver_template(bot: Bot, chat_id: int, key: str) -> None:
    row = get_message_row(key)

    if row:
        text = row["message_text"] or ""
        media = row["photo_file_id"]
    else:
        text = DEFAULT_MESSAGES.get(key, "")
        media = None

    kind, file_id = stored_media(media)

    async def send_text(value: str) -> None:
        last_error = None

        for parse_mode in (ParseMode.HTML, None):
            try:
                await bot.send_message(
                    chat_id,
                    value,
                    parse_mode=parse_mode,
                )
                return
            except Exception as error:
                if getattr(error, "retry_after", None):
                    raise
                last_error = error

        raise RuntimeError(last_error or "Не удалось отправить текст")

    if not file_id:
        await send_text(text)
        return

    caption = text if text and len(text) <= 1024 else None
    sent = False

    for parse_mode in (ParseMode.HTML, None):
        try:
            if kind == "document":
                await bot.send_document(
                    chat_id,
                    document=file_id,
                    caption=caption,
                    parse_mode=parse_mode,
                )
            else:
                await bot.send_photo(
                    chat_id,
                    photo=file_id,
                    caption=caption,
                    parse_mode=parse_mode,
                )
            sent = True
            break
        except Exception as error:
            if getattr(error, "retry_after", None):
                raise
            print(f"Авторассылка медиа {chat_id}: {error}")

    if not sent:
        await send_text(text)
        return

    if caption is None and text:
        await send_text(text)


async def finish_template_save(
    message: Message,
    state: FSMContext,
    key: str,
    text: str,
    photo_file_id: str | None,
    notice: str,
) -> None:
    try:
        save_message(key, text, photo_file_id)
    except Exception as error:
        print(f"Ошибка сохранения шаблона {key}: {error}")
        await message.answer(
            "Не удалось сохранить шаблон:\n"
            f"{error}\n\n"
            "Отправьте данные ещё раз или нажмите /cancel.",
            parse_mode=None,
        )
        return

    await state.clear()
    await message.answer(notice)

    delivered = await send_template(message, key)

    if photo_file_id and not delivered:
        await message.answer(
            "⚠️ Текст записан, но Telegram не принял это фото. "
            "Откройте шаблон ещё раз и отправьте другое изображение."
        )

    await message.answer(
        "Админ-панель:",
        reply_markup=admin_keyboard(),
    )


async def edit_or_send(
    message: Message,
    text: str,
    reply_markup=None,
) -> None:
    """
    edit_text работает только у текстовых сообщений.
    Карточка анкеты с фото из-за этого молча роняла удаление.
    """
    has_media = bool(
        getattr(message, "photo", None)
        or getattr(message, "video", None)
        or getattr(message, "document", None)
        or getattr(message, "animation", None)
        or getattr(message, "caption", None)
    )

    if getattr(message, "text", None) and not has_media:
        try:
            await message.edit_text(
                text,
                reply_markup=reply_markup,
            )
            return
        except Exception as error:
            print(f"edit_text не удался: {error}")

    if has_media:
        try:
            await message.delete()
        except Exception as error:
            print(f"Не удалось удалить медиа-сообщение: {error}")

    await answer_text(message, text, reply_markup)


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_keyboard(
    has_profile: bool = False,
    premium: bool = False,
) -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton(text="🔍 Найти игрока"),
            KeyboardButton(text="📝 Моя анкета"),
        ],
        [
            KeyboardButton(text="✏️ Создать / изменить анкету"),
        ],
    ]

    if has_profile:
        rows.append(
            [KeyboardButton(text="🗑 Удалить мою анкету")]
        )

    if premium:
        rows.append(
            [KeyboardButton(text="🚻 Поиск по полу")]
        )
        rows.append(
            [KeyboardButton(text="⬆️ Поднять в топ")]
        )

    rows.append(
        [KeyboardButton(text="Premium за приглашения")]
    )
    rows.append(
        [KeyboardButton(text="Заработай на тик-токах")]
    )
    rows.append(
        [KeyboardButton(text="ℹ️ Помощь")]
    )

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def keyboard_for(user_id: int) -> ReplyKeyboardMarkup:
    return main_keyboard(
        has_profile=get_profile(user_id) is not None,
        premium=has_premium(user_id),
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Анкеты",
                    callback_data="admin:profiles:0",
                ),
                InlineKeyboardButton(
                    text="➕ Добавить анкету",
                    callback_data="admin:add",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Жалобы",
                    callback_data="admin:reports",
                ),
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="admin:stats",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Шаблоны",
                    callback_data="admin:messages",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📣 Рассылка",
                    callback_data="admin:broadcast",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔔 Обязательная подписка",
                    callback_data="admin:channels",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Premium",
                    callback_data="admin:premium",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Проблемы",
                    callback_data="admin:problems:0",
                ),
            ],
        ]
    )


def premium_menu_keyboard(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Поделиться ссылкой",
                    url=share_link(link),
                )
            ]
        ]
    )


def gender_search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="мужской",
                    callback_data="search_gender:m",
                ),
                InlineKeyboardButton(
                    text="женский",
                    callback_data="search_gender:f",
                ),
            ]
        ]
    )


def premium_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выдать Premium",
                    callback_data="admin:prem_list:grant:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Забрать Premium",
                    callback_data="admin:prem_list:revoke:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ В админ-панель",
                    callback_data="admin:menu",
                )
            ],
        ]
    )


def premium_target_keyboard(
    mode: str,
    page: int,
    rows,
    has_next: bool,
) -> InlineKeyboardMarkup:
    buttons = []

    for row in rows:
        user_id = int(row["user_id"])
        nickname = str(row["nickname"] or "без имени")
        mark = "⭐ " if has_premium(user_id) else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{nickname} · {user_id}"[:64],
                    callback_data=f"admin:prem_set:{user_id}:{mode}",
                )
            ]
        )

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"admin:prem_list:{mode}:{page - 1}",
            )
        )

    if has_next:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"admin:prem_list:{mode}:{page + 1}",
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                text="🔎 ID или @username",
                callback_data=f"admin:prem_query:{mode}",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="admin:premium",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_premium_menu(
    message: Message,
    bot: Bot,
    user_id: int,
) -> None:
    link = await referral_link(bot, user_id)
    state = premium_state(user_id)
    lines = [
        "<b>Premium за приглашения</b>",
        "",
    ]

    if state["active"]:
        if state["kind"] == "admin":
            lines.append("Premium активен без ограничения срока.")
        else:
            lines.append(
                "Premium активен до: "
                f"{format_premium_until(state['until'])}."
            )
            lines.append("")
            lines.append(
                "Для следующего месяца: "
                f"{state['progress']} из {REFERRAL_GOAL}."
            )
            lines.append(
                "После этой даты нужно снова пригласить "
                f"{REFERRAL_GOAL} человек."
            )

        lines.append("")
        lines.append("Ваша ссылка:")
        lines.append(f"<code>{html.escape(link)}</code>")
    else:
        if state["kind"] == "revoked":
            lines.append("Premium отключён администратором.")
            lines.append("")

        lines.append("Ваша ссылка:")
        lines.append(f"<code>{html.escape(link)}</code>")
        lines.append("")
        lines.append(
            f"Приглашено: {state['progress']} из {REFERRAL_GOAL}."
        )
        lines.append(
            f"Осталось пригласить: {state['left']}."
        )
        lines.append("")
        lines.append(get_message_text("premium"))

    if list_required_channels():
        lines.append("")
        lines.append(
            "Активный реферал — человек, который впервые открыл "
            "бота по вашей ссылке и подписался на каналы "
            "обязательной подписки."
        )
    else:
        lines.append("")
        lines.append(
            "Активный реферал — человек, который впервые открыл "
            "бота по вашей ссылке. Если включена обязательная "
            "подписка, он засчитается только после неё."
        )

    text = "\n".join(lines)
    markup = premium_menu_keyboard(link)

    if not state["active"]:
        row = get_message_row("premium")
        media = row["photo_file_id"] if row else None

        if media:
            await send_saved_media(message, media, text, markup)
            return

    await message.answer(text, reply_markup=markup)


async def render_premium_targets(
    message: Message,
    mode: str,
    page: int,
) -> bool:
    limit = 8
    rows = DB.execute(
        """
        SELECT user_id, nickname
        FROM profiles
        ORDER BY nickname COLLATE NOCASE, user_id
        LIMIT ? OFFSET ?
        """,
        (limit + 1, page * limit),
    ).fetchall()
    has_next = len(rows) > limit
    rows = rows[:limit]

    if not rows:
        if page > 0:
            return False

        await edit_or_send(
            message,
            "<b>Анкет пока нет.</b>\n\n"
            "Найдите человека по Telegram ID или @username.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔎 ID или @username",
                            callback_data=f"admin:prem_query:{mode}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ Назад",
                            callback_data="admin:premium",
                        )
                    ],
                ]
            ),
        )
        return True

    title = "Выдать Premium" if mode == "grant" else "Забрать Premium"
    await edit_or_send(
        message,
        f"<b>{title}</b>\n\n"
        "Имена анкет. Нажмите, чтобы выбрать.\n"
        "⭐ — Premium уже включён.\n"
        "Либо найдите по Telegram ID или @username.",
        premium_target_keyboard(mode, page, rows, has_next),
    )
    return True


def delete_confirmation_keyboard(
    user_id: int,
    admin: bool = False,
) -> InlineKeyboardMarkup:
    prefix = "admin" if admin else "profile"

    yes_callback = (
        f"{prefix}:delete:{user_id}"
        if admin
        else "profile_delete_yes"
    )

    no_callback = (
        f"admin:user:{user_id}"
        if admin
        else "profile_delete_no"
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить",
                    callback_data=yes_callback,
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=no_callback,
                ),
            ]
        ]
    )


def profile_actions_keyboard(
    user_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❤️ Нравится",
                    callback_data=f"swipe:like:{user_id}",
                ),
                InlineKeyboardButton(
                    text="➡️ Пропустить",
                    callback_data=f"swipe:skip:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Пожаловаться",
                    callback_data=f"swipe:report:{user_id}",
                ),
            ],
        ]
    )


def options_keyboard(
    field_name: str,
    current_value: str | None = None,
) -> InlineKeyboardMarkup:
    selected = set()

    if current_value:
        selected = {
            item.strip()
            for item in str(current_value).split(",")
            if item.strip()
        }

    buttons = []

    for index, option in enumerate(
        FIELD_OPTIONS[field_name]
    ):
        prefix = "✅ " if option in selected else ""

        buttons.append(
            [
                InlineKeyboardButton(
                    text=prefix + option,
                    callback_data=(
                        f"form_option:{field_name}:{index}"
                    ),
                )
            ]
        )

    if field_name in MULTI_SELECT_FIELDS:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ Готово",
                    callback_data=f"form_done:{field_name}",
                )
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )


def photo_keyboard(
    has_photo: bool,
) -> InlineKeyboardMarkup:
    buttons = []

    if has_photo:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ Оставить прежнее фото",
callback_data="form_keep:photo_file_id",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🖼 Загрузить новое фото",
                callback_data="form_custom:photo_file_id",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="⏭️ Пропустить фото",
                callback_data="form_skip_photo",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )


def bio_keyboard(has_text: bool) -> InlineKeyboardMarkup:
    buttons = []

    if has_text:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ Оставить прежнее",
                    callback_data="form_keep:bio",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="Пропустить",
                callback_data="form_skip_bio",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )


_ADMIN_LIST_FILTER: dict[int, tuple[str, str]] = {}


def remembered_profile_filter(admin_id: int) -> tuple[str, str]:
    photo_filter, sort = _ADMIN_LIST_FILTER.get(
        admin_id,
        ("all", "updated"),
    )

    if photo_filter not in {"all", "yes", "no"}:
        photo_filter = "all"

    if sort not in {"updated", "collection"}:
        sort = "updated"

    return photo_filter, sort


def fetch_admin_profiles(
    photo_filter: str,
    sort: str,
    limit: int,
    offset: int,
):
    clauses = []
    params: list = []

    if photo_filter == "yes":
        clauses.append(
            """
            photo_file_id IS NOT NULL
            AND TRIM(photo_file_id) != ''
            AND photo_file_id != ?
            """
        )
        params.append(BLACK_SQUARE_PHOTO)
    elif photo_filter == "no":
        clauses.append(
            """
            (
                photo_file_id IS NULL
                OR TRIM(photo_file_id) = ''
                OR photo_file_id = ?
            )
            """
        )
        params.append(BLACK_SQUARE_PHOTO)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    if sort == "collection":
        order_sql = """
            ORDER BY
                CASE
                    WHEN collection GLOB '[0-9]*'
                    THEN CAST(collection AS INTEGER)
                    ELSE -1
                END DESC,
                updated DESC
        """
    else:
        order_sql = "ORDER BY updated DESC"

    params.extend([limit, offset])

    return DB.execute(
        f"""
        SELECT user_id, nickname, active, collection
        FROM profiles
        {where_sql}
        {order_sql}
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def admin_profiles_markup(
    profiles,
    page: int,
    photo_filter: str,
    sort: str,
    has_next: bool,
) -> InlineKeyboardMarkup:
    def marked(active: bool, label: str) -> str:
        return f"✅ {label}" if active else label

    def profiles_callback(next_page: int, photo: str, next_sort: str) -> str:
        return f"admin:profiles:{next_page}:{photo}:{next_sort}"

    buttons = [
        [
            InlineKeyboardButton(
                text=marked(photo_filter == "all", "Все"),
                callback_data=profiles_callback(0, "all", sort),
            ),
            InlineKeyboardButton(
                text=marked(photo_filter == "yes", "С фото"),
                callback_data=profiles_callback(0, "yes", sort),
            ),
            InlineKeyboardButton(
                text=marked(photo_filter == "no", "Без фото"),
                callback_data=profiles_callback(0, "no", sort),
            ),
        ],
        [
            InlineKeyboardButton(
                text=marked(sort == "updated", "Сначала новые"),
                callback_data=profiles_callback(0, photo_filter, "updated"),
            ),
            InlineKeyboardButton(
                text=marked(sort == "collection", "Коллекция ↓"),
                callback_data=profiles_callback(0, photo_filter, "collection"),
            ),
        ],
    ]

    for profile in profiles:
        status = "🟢" if profile["active"] else "⚪️"
        collection = str(profile["collection"] or "").strip()
        collection_bit = f" · {collection}" if collection else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"{status} "
                        f"{profile['nickname']}"
                        f"{collection_bit}"
                    )[:64],
                    callback_data=f"admin:user:{profile['user_id']}",
                )
            ]
        )

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=profiles_callback(
                    page - 1,
                    photo_filter,
                    sort,
                ),
            )
        )

    if has_next:
        navigation.append(
            InlineKeyboardButton(
                text="➡️ Далее",
                callback_data=profiles_callback(
                    page + 1,
                    photo_filter,
                    sort,
                ),
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                text="🏠 В меню",
                callback_data="admin:menu",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_profile_keyboard(
    user_id: int,
    active: bool,
    viewer_id: int | None = None,
) -> InlineKeyboardMarkup:
    photo_filter, sort = (
        remembered_profile_filter(viewer_id)
        if viewer_id is not None
        else ("all", "updated")
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Узнать юзернейм",
                    callback_data=f"admin:username:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "⏸️ Отключить показ"
                        if active
                        else "▶️ Включить показ"
                    ),
                    callback_data=f"admin:toggle:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить анкету",
                    callback_data=(
                        f"admin:delete_confirm:{user_id}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К списку",
                    callback_data=(
                        f"admin:profiles:0:{photo_filter}:{sort}"
                    ),
                ),
            ],
        ]
    )


def admin_messages_keyboard() -> InlineKeyboardMarkup:
    names = {
        "start": "Старт",
        "help": "Помощь",
        "no_profiles": "Нет анкет",
        "profile_saved": "Анкета сохранена",
        "premium": "Premium за приглашения",
        "tiktok": "Заработай на тик-токах",
        "reengage": "Авторассылка неактивным",
    }

    buttons = [
        [
            InlineKeyboardButton(
                text=title,
                callback_data=f"admin_message:{key}",
            )
        ]
        for key, title in names.items()
    ]

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ В админ-панель",
                callback_data="admin:menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )


# ============================================================
# ФОРМАТИРОВАНИЕ АНКЕТЫ
# ============================================================

CONTACT_LOCKED_NOTE = "ID и TG будут доступны после взаимного лайка"
ADMIN_HIDDEN_NOTE = (
    "PUBG ID, юзернейм и Telegram ID скрыты. "
    "Их открывает кнопка «Узнать юзернейм»."
)


def has_mutual_like(first_id: int, second_id: int) -> bool:
    if first_id == second_id:
        return False

    rows = DB.execute(
        """
        SELECT from_user
        FROM swipes
        WHERE action = 'like'
          AND (
              (from_user = ? AND to_user = ?)
              OR (from_user = ? AND to_user = ?)
          )
        """,
        (first_id, second_id, second_id, first_id),
    ).fetchall()
    sides = {int(row["from_user"]) for row in rows}
    return first_id in sides and second_id in sides


def profile_text(
    profile,
    reveal_contacts: bool = True,
    contact_note: str | None = None,
) -> str:
    gender = profile["gender"] or "Не указан"
    age = profile["age"] or "Не указан"

    try:
        collection = profile["collection"]
    except (KeyError, IndexError):
        collection = None

    collection_text = (
        str(collection).strip() if collection else ""
    ) or "Не указан"

    try:
        bio = str(profile["bio"] or "").strip()
    except (KeyError, IndexError):
        bio = ""

    if reveal_contacts:
        pubg_id_text = f"<code>{profile['pubg_id']}</code>"
        note = ""
    else:
        pubg_id_text = "скрыто"
        if contact_note is None:
            note = f"\n\n{CONTACT_LOCKED_NOTE}"
        elif contact_note:
            note = f"\n\n{contact_note}"
        else:
            note = ""

    limit = 4000 - len(note)
    text = (
        f"🎮 <b>{profile['nickname']}</b>\n\n"
        f"🚻 Пол: {gender}\n"
        f"🎂 Возраст: {age}\n"
        f"🆔 PUBG ID: {pubg_id_text}\n"
        f"🌍 Сервер: {profile['server']}\n"
        f"🎯 Режим: {profile['mode']}\n"
        f"🏆 Ранг: {profile['rank']}\n"
        f"📦 Уровень коллекции: {collection_text}\n"
        f"🎙 Микрофон: {profile['voice']}\n"
        f"🕒 Время игры: {profile['schedule']}\n"
        f"💬 Язык: {profile['language']}"
    )

    if bio:
        suffix = f"\n\n📝 {bio}"

        if len(text) + len(suffix) <= limit:
            text += suffix
        else:
            room = max(0, limit - len(text) - 2)

            if room:
                text += f"\n\n📝 {bio[:room]}"

    return text + note


async def send_profile(
    message: Message,
    profile,
    reply_markup=None,
    reveal_contacts: bool = True,
) -> None:
    text = profile_text(profile, reveal_contacts=reveal_contacts)
    photo = profile_photo_source(profile["photo_file_id"])

    if photo and len(text) <= 1024:
        try:
            await message.answer_photo(
                photo=photo,
                caption=text,
                reply_markup=reply_markup,
            )
            return
        except Exception as error:
            print(
                f"Не удалось отправить фото анкеты "
                f"{profile['user_id']}: {error}"
            )

    if photo:
        try:
            await message.answer_photo(
                photo=profile_photo_source(profile["photo_file_id"])
            )
        except Exception as error:
            print(
                f"Фото анкеты {profile['user_id']} "
                f"недоступно: {error}"
            )
            text += "\n\n⚠️ Фото анкеты недоступно."

    await answer_text(
        message,
        text,
        reply_markup,
    )


async def deliver_profile(
    bot: Bot,
    chat_id: int,
    profile,
    reply_markup=None,
    prefix: str = "",
    reveal_contacts: bool = True,
    force_photo: bool = False,
) -> None:
    body = profile_text(profile, reveal_contacts=reveal_contacts)
    text = f"{prefix}\n\n{body}" if prefix else body
    photo_id = (profile["photo_file_id"] or "").strip()

    if force_photo and not photo_id:
        photo_id = BLACK_SQUARE_PHOTO

    photo = profile_photo_source(photo_id)

    if photo and len(text) <= 1024:
        try:
            await bot.send_photo(
                chat_id,
                photo=photo,
                caption=text,
                reply_markup=reply_markup,
            )
            return
        except Exception as error:
            print(
                f"Не удалось отправить фото анкеты "
                f"{profile['user_id']} в {chat_id}: {error}"
            )

            if force_photo and photo_id != BLACK_SQUARE_PHOTO:
                try:
                    await bot.send_photo(
                        chat_id,
                        photo=profile_photo_source(BLACK_SQUARE_PHOTO),
                        caption=text,
                        reply_markup=reply_markup,
                    )
                    return
                except Exception as fallback_error:
                    print(
                        f"Чёрный квадрат для {chat_id}: {fallback_error}"
                    )

    if photo:
        try:
            await bot.send_photo(
                chat_id,
                photo=profile_photo_source(photo_id),
            )
        except Exception as error:
            print(
                f"Фото анкеты {profile['user_id']} "
                f"недоступно для {chat_id}: {error}"
            )

            if force_photo and photo_id != BLACK_SQUARE_PHOTO:
                try:
                    await bot.send_photo(
                        chat_id,
                        photo=profile_photo_source(BLACK_SQUARE_PHOTO),
                    )
                except Exception as fallback_error:
                    print(
                        f"Чёрный квадрат для {chat_id}: {fallback_error}"
                    )
                    text += "\n\n⚠️ Фото анкеты недоступно."
            else:
                text += "\n\n⚠️ Фото анкеты недоступно."

    if len(text) > 4096:
        text = text[:4095] + "…"

    await bot.send_message(
        chat_id,
        text,
        reply_markup=reply_markup,
    )


def likeback_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❤️ Лайкнуть в ответ",
                    callback_data=f"likeback:like:{user_id}",
                ),
                InlineKeyboardButton(
                    text="Пропустить",
                    callback_data=f"likeback:skip:{user_id}",
                ),
            ]
        ]
    )


# ============================================================
# ФОРМА АНКЕТЫ
# ============================================================

def parse_selected(value) -> list[str]:
    if not value:
        return []

    return [
        item.strip()
        for item in str(value).split(",")
        if item.strip()
    ]


def form_prompt(
    field_name: str,
    old_value: str | None = None,
) -> str:
    title = FIELD_NAMES[field_name]

    if field_name == "photo_file_id":
        return (
            "Отправьте фото профиля или выберите действие "
            "с помощью кнопок ниже."
        )

    if field_name == "pubg_id":
        return (
            "Введите PUBG ID.\n"
            "ID должен содержать минимум 5 цифр."
        )

    if field_name == "gender":
        text = (
            "Выберите пол кнопками «мужской» или «женский»."
        )

        if old_value:
            text += f"\n\nТекущее значение: {old_value}"

        return text

    if field_name == "age":
        text = (
            "Введите возраст.\n"
            f"Только число от {AGE_MIN} до {AGE_MAX}."
        )

        if old_value:
            text += (
                f"\n\nТекущее значение: {old_value}\n"
                "Если хотите оставить прежнее значение, "
                "используйте соответствующую кнопку."
            )

        return text

    if field_name == "collection":
        text = (
            "Введите уровень коллекции.\n"
            f"Число от {COLLECTION_MIN} до {COLLECTION_MAX}."
        )

        if old_value:
            text += (
                f"\n\nТекущее значение: {old_value}\n"
                "Если хотите оставить прежнее значение, "
                "используйте соответствующую кнопку."
            )

        return text

    if field_name == "bio":
        text = (
            "Расскажите о себе.\n"
            "Можно написать текст любой длины "
            "или нажать «Пропустить»."
        )

        if old_value:
            text += f"\n\nТекущее значение: {old_value}"

        return text

    if field_name in MULTI_SELECT_FIELDS:
        text = (
            f"Выберите варианты для поля «{title}».\n"
"Можно выбрать несколько вариантов, затем нажмите "
            "«✅ Готово»."
        )

        if old_value:
            text += f"\n\nТекущий выбор: {old_value}"

        return text

    if field_name in FIELD_OPTIONS:
        text = f"Выберите значение поля «{title}»."

        if old_value:
            text += f"\n\nТекущее значение: {old_value}"

        return text

    if old_value:
        return (
            f"Введите новое значение поля «{title}».\n\n"
            f"Текущее значение: {old_value}\n"
            "Если хотите оставить прежнее значение, "
            "используйте соответствующую кнопку."
        )

    return f"Введите значение поля «{title}»."


async def ask_field(
    message: Message,
    field_name: str,
    values: dict,
) -> None:
    old_value = values.get(field_name)

    if field_name == "photo_file_id":
        await message.answer(
            form_prompt(field_name, old_value),
            reply_markup=photo_keyboard(
                bool(old_value)
            ),
        )
        return

    if field_name == "gender":
        await message.answer(
            form_prompt(field_name, old_value),
            reply_markup=gender_keyboard(),
        )
        return

    if field_name == "bio":
        await message.answer(
            form_prompt(field_name, old_value),
            reply_markup=bio_keyboard(
                bool(str(old_value or "").strip())
            ),
        )
        return

    if field_name in FIELD_OPTIONS:
        await message.answer(
            form_prompt(field_name, old_value),
            reply_markup=options_keyboard(
                field_name,
                old_value,
            ),
        )
        return

    if old_value:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Оставить прежнее",
                        callback_data=(
                            f"form_keep:{field_name}"
                        ),
                    ),
                ],
            ],
        )

        await message.answer(
            form_prompt(field_name, old_value),
            reply_markup=keyboard,
        )
        return

    await message.answer(
        form_prompt(field_name, old_value)
    )


async def start_profile_form(
    message: Message,
    state: FSMContext,
    admin: bool = False,
    target_user_id: int | None = None,
) -> None:
    await state.clear()

    user_id = (
        target_user_id
        if target_user_id is not None
        else message.from_user.id
    )

    old = get_profile(user_id)

    values = {}

    if old:
        for field in PROFILE_FIELDS:
            if old[field] is not None:
                values[field] = old[field]

    await state.update_data(
        admin_form=admin,
        target_user_id=user_id,
        values=values,
    )

    next_state = (
        AdminProfileForm.nickname
        if admin
        else ProfileForm.nickname
    )

    await state.set_state(next_state)
    await ask_field(message, "nickname", values)


async def next_field(
    message: Message,
    state: FSMContext,
    current_field: str,
) -> None:
    data = await state.get_data()
    values = data.get("values", {})

    current_index = PROFILE_FIELDS.index(current_field)

    if current_index == len(PROFILE_FIELDS) - 1:
        target_user_id = int(data["target_user_id"])

        try:
            save_profile(target_user_id, values)
        except Exception as error:
            print(f"Ошибка сохранения анкеты: {error}")

            await state.clear()
            await message.answer(
                f"Не удалось сохранить анкету:\n{error}",
                reply_markup=keyboard_for(message.chat.id),
            )
            return

        await state.clear()

        if data.get("admin_form"):
            await message.answer(
                f"✅ Анкета пользователя "
                f"<code>{target_user_id}</code> сохранена.",
                reply_markup=admin_keyboard(),
            )
        else:
            await send_template(
                message,
                "profile_saved",
                keyboard_for(message.chat.id),
            )

        return

    next_name = PROFILE_FIELDS[current_index + 1]

    next_state = (
        ADMIN_PROFILE_STATES[next_name]
        if data.get("admin_form")
        else PROFILE_STATES[next_name]
    )

    await state.set_state(next_state)
    await ask_field(message, next_name, values)


def save_profile(
    user_id: int,
    values: dict,
) -> None:
    required = [
        "nickname",
        "gender",
        "age",
        "pubg_id",
        "server",
        "mode",
        "rank",
        "collection",
        "voice",
        "schedule",
        "language",
    ]

    for field in required:
        if not str(values.get(field, "")).strip():
            raise ValueError(
                f"Поле «{FIELD_NAMES[field]}» не заполнено."
            )

    pubg_id = str(values["pubg_id"]).strip()

    if not pubg_id.isdigit() or len(pubg_id) < 5:
        raise ValueError(
            "PUBG ID должен содержать минимум 5 цифр."
        )

    bio = str(values.get("bio") or "").strip()

    gender = normalize_gender(str(values["gender"]))

    if gender is None:
        raise ValueError(
            "Выберите пол кнопками «мужской» или «женский»."
        )

    age = normalize_age(str(values["age"]))

    if age is None:
        raise ValueError(
            f"Возраст нужно указать числом от {AGE_MIN} до {AGE_MAX}."
        )

    collection = normalize_collection(str(values.get("collection", "")))

    if collection is None:
        raise ValueError(
            "Уровень коллекции нужно указать числом "
            f"от {COLLECTION_MIN} до {COLLECTION_MAX}."
        )

    DB.execute(
        """
        INSERT INTO profiles (
            user_id,
            nickname,
            gender,
            age,
            pubg_id,
            server,
            mode,
            rank,
            collection,
            voice,
            schedule,
            language,
            bio,
            photo_file_id,
            active,
            updated
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            nickname = excluded.nickname,
            gender = excluded.gender,
            age = excluded.age,
            pubg_id = excluded.pubg_id,
            server = excluded.server,
            mode = excluded.mode,
            rank = excluded.rank,
            collection = excluded.collection,
            voice = excluded.voice,
            schedule = excluded.schedule,
            language = excluded.language,
            bio = excluded.bio,
            photo_file_id = excluded.photo_file_id,
            active = 1,
            updated = excluded.updated
        """,
        (
            user_id,
            str(values["nickname"]).strip(),
            gender,
            age,
            pubg_id,
            str(values["server"]).strip(),
            str(values["mode"]).strip(),
            str(values["rank"]).strip(),
            collection,
            str(values["voice"]).strip(),
            str(values["schedule"]).strip(),
            str(values["language"]).strip(),
            bio,
            values.get("photo_file_id"),
            current_time(),
        ),
    )

    DB.commit()

    if not get_profile(user_id):
        raise RuntimeError(
            "Анкета не появилась в базе после сохранения."
        )


# ============================================================
# КОМАНДЫ И ГЛАВНОЕ МЕНЮ
# ============================================================

@router.message(Command("start"))
async def command_start(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    await send_template(
        message,
        "start",
        keyboard_for(message.from_user.id),
    )


def help_report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сообщить о проблеме",
                    callback_data="problem:report",
                )
            ]
        ]
    )


@router.message(Command("help"))
async def command_help(message: Message):
    await send_template(
        message,
        "help",
        help_report_keyboard(),
    )


@router.message(Command("cancel"))
async def command_cancel(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    await message.answer(
        "Действие отменено.",
        reply_markup=keyboard_for(message.from_user.id),
    )


@router.message(Command("profile"))
async def command_profile(message: Message):
    profile = get_profile(message.from_user.id)

    if not profile:
        await message.answer(
            "У вас пока нет анкеты.",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    await send_profile(
        message,
        profile,
        keyboard_for(message.from_user.id),
    )


@router.message(Command("search"))
async def command_search(message: Message):
    _SEARCH_GENDER.pop(message.from_user.id, None)
    await search_next(
        message,
        message.from_user.id,
    )


@router.message(Command("prem"))
async def command_prem(message: Message, bot: Bot):
    await send_premium_menu(
        message,
        bot,
        message.from_user.id,
    )


@router.message(Command("admin"))
async def command_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Нет доступа.")
        return

    await message.answer(
        "Админ-панель:",
        reply_markup=admin_keyboard(),
    )


@router.message(Command("superadmin"))
async def command_superadmin(
    message: Message,
    state: FSMContext,
):
    if not is_superadmin(message.from_user.id):
        await message.answer("⛔️ Нет доступа.")
        return

    await state.clear()
    await message.answer(
        "Панель супер-админа:",
        reply_markup=superadmin_keyboard(),
    )


@router.callback_query(F.data == "problem:report")
async def problem_report_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await state.set_state(ProblemForm.description)
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Опишите проблему: что произошло и как это повторить.\n\n"
            "Отмена — /cancel"
        )


async def problem_menu_instead(
    message: Message,
    state: FSMContext,
    text: str,
) -> bool:
    """Кнопка меню во время описания проблемы не должна стать текстом жалобы."""
    if text == "📝 Моя анкета":
        await state.clear()
        await command_profile(message)
        return True

    if text == "ℹ️ Помощь":
        await state.clear()
        await command_help(message)
        return True

    if text == "🔍 Найти игрока":
        await state.clear()
        await command_search(message)
        return True

    if text == "Premium за приглашения":
        await state.clear()
        await command_prem(message, message.bot)
        return True

    if text == "Заработай на тик-токах":
        await state.clear()
        await menu_tiktok(message)
        return True

    if text == "🚻 Поиск по полу":
        await state.clear()
        await menu_search_gender(message)
        return True

    if text == "⬆️ Поднять в топ":
        await state.clear()
        await menu_boost(message)
        return True

    if text == "✏️ Создать / изменить анкету":
        await state.clear()
        await start_profile_form(message, state)
        return True

    if text == "🗑 Удалить мою анкету":
        await state.clear()
        await delete_my_profile_request(message)
        return True

    return False


@router.message(
    StateFilter(ProblemForm.description),
    F.text,
)
async def problem_report_text(
    message: Message,
    state: FSMContext,
):
    text = (message.text or "").strip()

    if await problem_menu_instead(message, state, text):
        return

    if not text or text.startswith("/"):
        await message.answer(
            "Нужно описание проблемы обычным текстом.\n"
            "Отмена — /cancel."
        )
        return

    if len(text) > 3500:
        await message.answer(
            "Слишком длинное описание. Сократите его и отправьте ещё раз."
        )
        return

    username = (message.from_user.username or "").strip()

    try:
        add_problem(message.from_user.id, username, text)
    except Exception as error:
        DB.rollback()
        print(f"Проблема {message.from_user.id}: {error}")
        await message.answer(
            "Не удалось отправить сообщение. Попробуйте ещё раз.",
            parse_mode=None,
        )
        return

    await state.clear()
    await message.answer(
        "Сообщение о проблеме отправлено.",
        reply_markup=keyboard_for(message.from_user.id),
    )


@router.message(StateFilter(ProblemForm.description))
async def problem_report_invalid(message: Message):
    await message.answer(
        "Опишите проблему текстом или нажмите /cancel."
    )


@router.message(F.text == "📝 Моя анкета")
async def menu_profile(message: Message):
    await command_profile(message)


@router.message(F.text == "ℹ️ Помощь")
async def menu_help(message: Message):
    await command_help(message)
@router.message(F.text == "🔍 Найти игрока")
async def menu_search(message: Message):
    await command_search(message)


@router.message(F.text == "Premium за приглашения")
async def menu_prem(message: Message, bot: Bot):
    await command_prem(message, bot)


def tiktok_submit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отправить тикток на выплату",
                    callback_data="tiktok:submit",
                )
            ]
        ]
    )


@router.message(F.text == "Заработай на тик-токах")
async def menu_tiktok(message: Message):
    await send_template(
        message,
        "tiktok",
        tiktok_submit_keyboard(),
    )


@router.message(F.text == "🚻 Поиск по полу")
async def menu_search_gender(message: Message):
    user_id = message.from_user.id

    if not has_premium(user_id):
        await message.answer(
            "Поиск по полу доступен только с Premium.",
            reply_markup=keyboard_for(user_id),
        )
        return

    if not get_profile(user_id):
        await message.answer(
            "Сначала создайте свою анкету.",
            reply_markup=keyboard_for(user_id),
        )
        return

    await message.answer(
        "Кого искать?\n"
        "«🔍 Найти игрока» снова показывает всех.",
        reply_markup=gender_search_keyboard(),
    )


@router.message(F.text == "⬆️ Поднять в топ")
async def menu_boost(message: Message):
    user_id = message.from_user.id
    ok, text = try_boost_profile(user_id)

    await message.answer(
        text,
        reply_markup=keyboard_for(user_id),
    )


@router.message(F.text == "✏️ Создать / изменить анкету")
async def menu_edit_profile(
    message: Message,
    state: FSMContext,
):
    await start_profile_form(message, state)


# ============================================================
# УДАЛЕНИЕ СОБСТВЕННОЙ АНКЕТЫ
# ============================================================

@router.message(F.text == "🗑 Удалить мою анкету")
async def delete_my_profile_request(message: Message):
    if not get_profile(message.from_user.id):
        await message.answer(
            "У вас пока нет анкеты.",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    await message.answer(
        "Удалить вашу анкету?\n"
        "Связанные лайки, жалобы и пропуски также будут удалены.",
        reply_markup=delete_confirmation_keyboard(
            message.from_user.id
        ),
    )


@router.callback_query(F.data == "profile_delete_no")
async def delete_my_profile_no(callback: CallbackQuery):
    await callback.answer("Удаление отменено.")

    if callback.message:
        await callback.message.edit_text(
            "Удаление отменено."
        )


@router.callback_query(F.data == "profile_delete_yes")
async def delete_my_profile_yes(
    callback: CallbackQuery,
):
    user_id = callback.from_user.id

    if not get_profile(user_id):
        await callback.answer(
            "Анкета уже удалена.",
            show_alert=True,
        )
        return

    delete_profile_completely(user_id)

    await callback.answer("Анкета удалена.")

    if callback.message:
        await callback.message.edit_text(
            "✅ Ваша анкета удалена."
        )
        await callback.message.answer(
            "Главное меню:",
            reply_markup=keyboard_for(user_id),
        )


# ============================================================
# ОБРАБОТКА ПОЛЕЙ АНКЕТЫ
# ============================================================

@router.callback_query(F.data.startswith("form_keep:"))
async def form_keep(
    callback: CallbackQuery,
    state: FSMContext,
):
    field_name = callback.data.split(":", 1)[1]
    current_state = await state.get_state()

    if not current_state or not current_state.endswith(
        field_name
    ):
        await callback.answer(
            "Это поле уже обработано.",
            show_alert=True,
        )
        return

    if field_name == "collection":
        data = await state.get_data()
        collection = str(
            data.get("values", {}).get("collection") or ""
        )

        if normalize_collection(collection) is None:
            await callback.answer(
                "Уровень коллекции — число "
                f"от {COLLECTION_MIN} до {COLLECTION_MAX}.",
                show_alert=True,
            )
            return

    if field_name == "age":
        data = await state.get_data()
        age = str(
            data.get("values", {}).get("age") or ""
        )

        if normalize_age(age) is None:
            await callback.answer(
                f"Введите возраст числом от {AGE_MIN} до {AGE_MAX}.",
                show_alert=True,
            )
            return

    await callback.answer("Сохранено.")

    if callback.message:
        await next_field(
            callback.message,
            state,
            field_name,
        )


@router.callback_query(F.data == "form_skip_photo")
async def form_skip_photo(
    callback: CallbackQuery,
    state: FSMContext,
):
    current_state = await state.get_state()

    if not current_state or not current_state.endswith(
        "photo_file_id"
    ):
        await callback.answer(
            "Это поле уже обработано.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    values = data.get("values", {})

    # Пропуск фото: в анкете показываем чёрный квадрат 1:1,
    # а не пустое место и не чужой file_id.
    values["photo_file_id"] = BLACK_SQUARE_PHOTO

    await state.update_data(values=values)
    await callback.answer("В анкете будет чёрный квадрат.")

    if callback.message:
        await next_field(
            callback.message,
            state,
            "photo_file_id",
        )


@router.callback_query(F.data == "form_skip_bio")
async def form_skip_bio(
    callback: CallbackQuery,
    state: FSMContext,
):
    current_state = await state.get_state()

    if not current_state or not current_state.endswith("bio"):
        await callback.answer(
            "Это поле уже обработано.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    values = data.get("values", {})
    values["bio"] = ""

    await state.update_data(values=values)
    await callback.answer("Поле «О себе» пропущено.")

    if callback.message:
        await next_field(
            callback.message,
            state,
            "bio",
        )


@router.callback_query(F.data.startswith("form_custom:"))
async def form_custom(
    callback: CallbackQuery,
    state: FSMContext,
):
    field_name = callback.data.split(":", 1)[1]
    current_state = await state.get_state()

    if not current_state or not current_state.endswith(
        field_name
    ):
        await callback.answer(
            "Это поле уже обработано.",
            show_alert=True,
        )
        return

    await callback.answer()

    if callback.message:
        if field_name == "photo_file_id":
            await callback.message.answer(
                "Отправьте новое фото."
)
        else:
            await callback.message.answer(
                f"Введите новое значение поля "
                f"«{FIELD_NAMES[field_name]}»."
            )


@router.callback_query(
    F.data.startswith("form_option:")
)
async def form_option(
    callback: CallbackQuery,
    state: FSMContext,
):
    _, field_name, index_text = callback.data.split(
        ":",
        2,
    )

    try:
        index = int(index_text)
        option = FIELD_OPTIONS[field_name][index]
    except (KeyError, ValueError, IndexError):
        await callback.answer(
            "Вариант не найден.",
            show_alert=True,
        )
        return

    current_state = await state.get_state()

    if not current_state or not current_state.endswith(
        field_name
    ):
        await callback.answer(
            "Это поле уже обработано.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    values = data.get("values", {})

    if field_name in MULTI_SELECT_FIELDS:
        selected = parse_selected(values.get(field_name))

        if option in selected:
            selected.remove(option)
        else:
            selected.append(option)

        values[field_name] = ", ".join(selected)

        await state.update_data(values=values)
        await callback.answer("Выбор обновлён.")

        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=options_keyboard(
                    field_name,
                    values[field_name],
                )
            )

        return

    values[field_name] = option

    await state.update_data(values=values)
    await callback.answer(f"Выбрано: {option}")

    if callback.message:
        await next_field(
            callback.message,
            state,
            field_name,
        )


@router.callback_query(F.data.startswith("form_done:"))
async def form_done(
    callback: CallbackQuery,
    state: FSMContext,
):
    field_name = callback.data.split(":", 1)[1]

    if field_name not in MULTI_SELECT_FIELDS:
        await callback.answer(
            "Недопустимое поле.",
            show_alert=True,
        )
        return

    current_state = await state.get_state()

    if not current_state or not current_state.endswith(
        field_name
    ):
        await callback.answer(
            "Это поле уже обработано.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    values = data.get("values", {})

    if not values.get(field_name):
        await callback.answer(
            "Выберите хотя бы один вариант.",
            show_alert=True,
        )
        return

    await callback.answer("Выбор сохранён.")

    if callback.message:
        await next_field(
            callback.message,
            state,
            field_name,
        )


@router.message(
    StateFilter(
        ProfileForm.nickname,
        AdminProfileForm.nickname,
    ),
    F.text,
)
async def profile_nickname(
    message: Message,
    state: FSMContext,
):
    value = message.text.strip()

    if not value:
        await message.answer(
            "Никнейм не должен быть пустым."
        )
        return

    data = await state.get_data()
    values = data.get("values", {})
    values["nickname"] = value[:100]

    await state.update_data(values=values)

    await next_field(
        message,
        state,
        "nickname",
    )


@router.message(
    StateFilter(
        ProfileForm.gender,
        AdminProfileForm.gender,
    ),
    F.text,
)
async def profile_gender(
    message: Message,
    state: FSMContext,
):
    gender = normalize_gender(message.text)

    if gender is None:
        await message.answer(
            "Выберите пол кнопками «мужской» или «женский».",
            reply_markup=gender_keyboard(),
        )
        return

    data = await state.get_data()
    values = data.get("values", {})
    values["gender"] = gender

    await state.update_data(values=values)

    await next_field(
        message,
        state,
        "gender",
    )


@router.message(
    StateFilter(
        ProfileForm.gender,
        AdminProfileForm.gender,
    ),
)
async def profile_gender_invalid(message: Message):
    await message.answer(
        "Выберите пол кнопками «мужской» или «женский».",
        reply_markup=gender_keyboard(),
    )


@router.message(
    StateFilter(
        ProfileForm.age,
        AdminProfileForm.age,
    ),
    F.text,
)
async def profile_age(
    message: Message,
    state: FSMContext,
):
    age = normalize_age(message.text)

    if age is None:
        await message.answer(
            f"Возраст нужно указать числом от {AGE_MIN} до {AGE_MAX}."
        )
        return

    data = await state.get_data()
    values = data.get("values", {})
    values["age"] = age

    await state.update_data(values=values)

    await next_field(
        message,
        state,
        "age",
    )


@router.message(
    StateFilter(
        ProfileForm.age,
        AdminProfileForm.age,
    ),
)
async def profile_age_invalid(message: Message):
    await message.answer(
        f"Возраст нужно указать числом от {AGE_MIN} до {AGE_MAX}."
    )


@router.message(
    StateFilter(
        ProfileForm.collection,
        AdminProfileForm.collection,
    ),
    F.text,
)
async def profile_collection(
    message: Message,
    state: FSMContext,
):
    collection = normalize_collection(message.text)

    if collection is None:
        await message.answer(
            "Уровень коллекции нужно указать числом "
            f"от {COLLECTION_MIN} до {COLLECTION_MAX}."
        )
        return

    data = await state.get_data()
    values = data.get("values", {})
    values["collection"] = collection

    await state.update_data(values=values)

    await next_field(
        message,
        state,
        "collection",
    )


@router.message(
    StateFilter(
        ProfileForm.collection,
        AdminProfileForm.collection,
    ),
)
async def profile_collection_invalid(message: Message):
    await message.answer(
        "Уровень коллекции нужно указать числом "
        f"от {COLLECTION_MIN} до {COLLECTION_MAX}."
    )


@router.message(
    StateFilter(
        ProfileForm.pubg_id,
        AdminProfileForm.pubg_id,
    ),
    F.text,
)
async def profile_pubg_id(
    message: Message,
    state: FSMContext,
):
    value = message.text.strip()

    if not value.isdigit() or len(value) < 5:
        await message.answer(
            "PUBG ID должен содержать минимум 5 цифр."
        )
        return

    data = await state.get_data()
    values = data.get("values", {})
    values["pubg_id"] = value

    await state.update_data(values=values)

    await next_field(
        message,
        state,
        "pubg_id",
    )


@router.message(
StateFilter(
        ProfileForm.rank,
        AdminProfileForm.rank,
        ProfileForm.language,
        AdminProfileForm.language,
        ProfileForm.bio,
        AdminProfileForm.bio,
    ),
    F.text,
)
async def profile_text_field(
    message: Message,
    state: FSMContext,
):
    current_state = await state.get_state()

    if not current_state:
        return

    field_name = current_state.split(":")[-1]
    value = message.text.strip()

    if not value:
        if field_name == "bio":
            await message.answer(
                "Напишите текст или нажмите «Пропустить».",
                reply_markup=bio_keyboard(False),
            )
            return

        await message.answer(
            "Значение не должно быть пустым."
        )
        return

    data = await state.get_data()
    values = data.get("values", {})
    limit = 4000 if field_name == "bio" else 2000
    values[field_name] = value[:limit]

    await state.update_data(values=values)

    await next_field(
        message,
        state,
        field_name,
    )


@router.message(
    StateFilter(
        ProfileForm.photo_file_id,
        AdminProfileForm.photo_file_id,
    ),
    F.photo,
)
async def profile_photo(
    message: Message,
    state: FSMContext,
):
    data = await state.get_data()
    values = data.get("values", {})

    values["photo_file_id"] = message.photo[-1].file_id

    await state.update_data(values=values)

    await next_field(
        message,
        state,
        "photo_file_id",
    )


@router.message(
    StateFilter(
        ProfileForm.photo_file_id,
        AdminProfileForm.photo_file_id,
    ),
)
async def profile_photo_invalid(message: Message):
    await message.answer(
        "Отправьте фото или нажмите "
        "«⏭️ Пропустить фото»."
    )


# ============================================================
# ПОИСК
# ============================================================

VIEW_DAILY_LIMIT = 25
VIEW_LIMIT_TEXT = (
    "Вы превысили суточный лимит просмотра анкет 25/25. "
    "Чтобы увеличить лимит нужен Premium, его можно получить бесплатно "
    "за приглашение людей в бота. Также в Premium открываются функции "
    "\"Поиск по полу\" и \"Поднятие анкеты в топ\". "
    "Получить премиум - /prem"
)


def msk_day() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def views_used_today(user_id: int) -> int:
    row = DB.execute(
        """
        SELECT COUNT(*) AS count
        FROM daily_views
        WHERE user_id = ? AND day = ?
        """,
        (user_id, msk_day()),
    ).fetchone()
    return int(row["count"]) if row else 0


def profile_seen_today(user_id: int, target_id: int) -> bool:
    row = DB.execute(
        """
        SELECT 1
        FROM daily_views
        WHERE user_id = ? AND target_id = ? AND day = ?
        """,
        (user_id, target_id, msk_day()),
    ).fetchone()
    return row is not None


def consume_profile_view(user_id: int, target_id: int) -> None:
    if has_premium(user_id):
        return

    try:
        DB.execute(
            """
            INSERT INTO daily_views (
                user_id,
                target_id,
                day
            )
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, target_id, day) DO NOTHING
            """,
            (user_id, target_id, msk_day()),
        )
        DB.commit()
    except Exception:
        DB.rollback()
        raise


async def answer_view_limit(message: Message, user_id: int) -> None:
    await message.answer(
        VIEW_LIMIT_TEXT,
        reply_markup=keyboard_for(user_id),
    )


async def search_next(
    message: Message,
    user_id: int,
) -> None:
    if not get_profile(user_id):
        await message.answer(
            "Сначала создайте свою анкету.",
            reply_markup=keyboard_for(user_id),
        )
        return

    gender = None

    if has_premium(user_id):
        gender = _SEARCH_GENDER.get(user_id)

        if gender not in SEARCH_GENDERS.values():
            gender = None
    else:
        _SEARCH_GENDER.pop(user_id, None)

    params: list = [
        user_id,
        user_id,
        user_id,
        user_id,
    ]
    gender_sql = ""

    if gender:
        gender_sql = "AND p.gender = ?"
        params.append(gender)

    params.append(premium_db_now())

    profile = DB.execute(
        f"""
        SELECT p.*
        FROM profiles p
        WHERE p.active = 1
          AND p.user_id != ?
          AND NOT EXISTS (
              SELECT 1
              FROM swipes s
              WHERE s.from_user = ?
                AND s.to_user = p.user_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM blocks b
              WHERE
                (b.blocker = ? AND b.blocked = p.user_id)
                OR
                (b.blocker = p.user_id AND b.blocked = ?)
          )
          {gender_sql}
        {boost_order_sql()}
        LIMIT 1
        """,
        params,
    ).fetchone()

    if not profile:
        if gender:
            await message.answer(
                f"Анкет с полом «{gender.lower()}» больше нет.",
                reply_markup=gender_search_keyboard(),
            )
            return

        await send_template(
            message,
            "no_profiles",
            keyboard_for(user_id),
        )
        return

    target_id = int(profile["user_id"])

    if not has_premium(user_id) and not profile_seen_today(user_id, target_id):
        if views_used_today(user_id) >= VIEW_DAILY_LIMIT:
            await answer_view_limit(message, user_id)
            return

        consume_profile_view(user_id, target_id)

    await send_profile(
        message,
        profile,
        profile_actions_keyboard(target_id),
        reveal_contacts=has_mutual_like(user_id, target_id),
    )


@router.callback_query(F.data.startswith("search_gender:"))
async def search_gender_chosen(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    gender = SEARCH_GENDERS.get(code)
    user_id = callback.from_user.id

    if not gender:
        await callback.answer(
            "Неизвестный пол.",
            show_alert=True,
        )
        return

    if not has_premium(user_id):
        await callback.answer(
            "Поиск по полу доступен только с Premium.",
            show_alert=True,
        )
        return

    if not get_profile(user_id):
        await callback.answer(
            "Сначала создайте анкету.",
            show_alert=True,
        )
        return

    _SEARCH_GENDER[user_id] = gender
    await callback.answer(f"Ищем: {gender.lower()}")

    if callback.message:
        await search_next(callback.message, user_id)


def try_boost_profile(user_id: int) -> tuple[bool, str]:
    if not has_premium(user_id):
        return False, "Поднятие в топ доступно только с Premium."

    profile = get_profile(user_id)

    if not profile:
        return False, "Сначала создайте анкету."

    if int(profile["boosted"] or 0):
        return False, "Анкета уже в топе."

    try:
        DB.execute(
            """
            UPDATE profiles
            SET boosted = 1, updated = ?
            WHERE user_id = ?
            """,
            (current_time(), user_id),
        )
        DB.commit()
    except Exception as error:
        DB.rollback()
        print(f"Не удалось поднять анкету {user_id}: {error}")
        return False, "Не удалось поднять анкету."

    return True, (
        "Анкета поднята в топ. "
        "Она будет показываться раньше остальных, "
        "пока действует Premium."
    )


@router.callback_query(F.data == "prem:boost")
async def premium_boost(callback: CallbackQuery):
    user_id = callback.from_user.id
    ok, text = try_boost_profile(user_id)

    await callback.answer(
        text if len(text) <= 180 else text[:180],
        show_alert=not ok,
    )

    if ok and callback.message:
        await callback.message.answer(text)


async def notify_incoming_like(
    bot: Bot,
    liker_id: int,
    liked_id: int,
) -> None:
    if liker_id == liked_id or users_blocked(liker_id, liked_id):
        return

    liker = get_profile(liker_id)

    if not liker or not int(liker["active"] or 0):
        return

    try:
        await deliver_profile(
            bot,
            liked_id,
            liker,
            reply_markup=likeback_keyboard(liker_id),
            prefix=(
                "❤️ Вашу анкету лайкнули.\n"
                "Ниже анкета человека, которому вы понравились. "
                "Можно лайкнуть в ответ или пропустить."
            ),
            reveal_contacts=has_mutual_like(liked_id, liker_id),
        )
    except Exception as error:
        print(f"Не удалось сообщить о лайке {liked_id}: {error}")


async def notify_match(
    bot: Bot,
    first_id: int,
    second_id: int,
) -> None:
    first = get_profile(first_id)
    second = get_profile(second_id)

    if not first or not second:
        return

    for user_id, other, other_id in (
        (first_id, second, second_id),
        (second_id, first, first_id),
    ):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть Telegram",
                        url=f"tg://user?id={other_id}",
                    )
                ]
            ]
        )

        try:
            await deliver_profile(
                bot,
                user_id,
                other,
                reply_markup=keyboard,
                prefix="🎉 У вас взаимный лайк!",
                force_photo=True,
            )
        except Exception as error:
            print(
                f"Не удалось отправить уведомление "
                f"{user_id}: {error}"
            )


@router.callback_query(F.data.startswith("likeback:"))
async def process_likeback(
    callback: CallbackQuery,
    bot: Bot,
):
    parts = (callback.data or "").split(":")

    if len(parts) != 3 or parts[1] not in {"like", "skip"}:
        await callback.answer(
            "Неизвестное действие.",
            show_alert=True,
        )
        return

    if not parts[2].isdigit():
        await callback.answer(
            "Анкета не найдена.",
            show_alert=True,
        )
        return

    action = parts[1]
    liker_id = int(parts[2])
    user_id = callback.from_user.id

    if liker_id == user_id:
        await callback.answer(
            "Нельзя лайкнуть свою анкету.",
            show_alert=True,
        )
        return

    existing = DB.execute(
        """
        SELECT action
        FROM swipes
        WHERE from_user = ? AND to_user = ?
        """,
        (user_id, liker_id),
    ).fetchone()

    if existing and existing["action"] == (
        "like" if action == "like" else "skip"
    ):
        await callback.answer("Этот ответ уже выбран.")
        return

    if action == "like":
        if not get_profile(user_id):
            await callback.answer(
                "Сначала создайте свою анкету, чтобы ответить на лайк.",
                show_alert=True,
            )
            return

        liker = get_profile(liker_id)

        if not liker or not int(liker["active"] or 0):
            await callback.answer(
                "Анкета уже недоступна.",
                show_alert=True,
            )
            return

        record_swipe(user_id, liker_id, "like")
        mutual = DB.execute(
            """
            SELECT 1
            FROM swipes
            WHERE from_user = ?
              AND to_user = ?
              AND action = 'like'
            """,
            (liker_id, user_id),
        ).fetchone()

        if mutual:
            await callback.answer(
                "🎉 Взаимный лайк!",
                show_alert=True,
            )
            await notify_match(bot, user_id, liker_id)
        else:
            await callback.answer("Лайк отправлен.")
            await notify_incoming_like(bot, user_id, liker_id)
    else:
        record_swipe(user_id, liker_id, "skip")
        await callback.answer("Анкета пропущена.")

    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as error:
            print(f"Не удалось убрать кнопки ответа на лайк: {error}")

        if action == "skip":
            await callback.message.answer("Вы пропустили эту анкету.")
        elif not existing or existing["action"] != "like":
            await callback.message.answer(
                "Ответ сохранён."
                if action == "skip"
                else "Вы лайкнули в ответ."
            )


@router.callback_query(F.data.startswith("swipe:"))
async def process_swipe(
    callback: CallbackQuery,
    bot: Bot,
):
    _, action, target_text = callback.data.split(":")
    target_id = int(target_text)
    user_id = callback.from_user.id

    target = get_profile(target_id)

    if not target or not target["active"]:
        await callback.answer(
            "Анкета недоступна.",
            show_alert=True,
        )

        if callback.message:
            await search_next(
                callback.message,
                user_id,
            )

        return

    if action == "report":
        DB.execute(
            """
            INSERT INTO reports (
                reporter,
                reported,
                created,
                resolved
            )
            VALUES (?, ?, ?, 0)
            """,
            (
                user_id,
                target_id,
                current_time(),
            ),
        )

        DB.commit()

        await callback.answer(
            "Жалоба отправлена.",
            show_alert=True,
        )

        if callback.message:
            await search_next(
                callback.message,
                user_id,
            )

        return

    DB.execute(
        """
        INSERT INTO swipes (
            from_user,
            to_user,
            action,
            created
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(from_user, to_user) DO UPDATE SET
            action = excluded.action,
            created = excluded.created
        """,
        (
            user_id,
            target_id,
            action,
            current_time(),
        ),
    )

    DB.commit()

    if action == "like":
        mutual = DB.execute(
            """
            SELECT 1
            FROM swipes
            WHERE from_user = ?
              AND to_user = ?
              AND action = 'like'
            """,
            (
                target_id,
                user_id,
            ),
        ).fetchone()

        if mutual:
            await callback.answer(
                "🎉 Взаимный лайк!",
                show_alert=True,
            )
            await notify_match(
                bot,
                user_id,
                target_id,
            )
        else:
            await callback.answer(
                "Лайк отправлен."
            )
            await notify_incoming_like(
                bot,
                user_id,
                target_id,
            )
    else:
        await callback.answer(
            "Анкета пропущена."
        )

    if callback.message:
        await search_next(
            callback.message,
            user_id,
        )


# ============================================================
# АДМИНКА: ОСНОВНЫЕ CALLBACK
# ============================================================

@router.callback_query(F.data.startswith("admin:"))
async def admin_callback(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    parts = callback.data.split(":")
    action = parts[1]

    if action == "menu":
        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                "Админ-панель:",
                reply_markup=admin_keyboard(),
            )

        return

    if action == "add":
        await state.clear()
        await state.set_state(
            AdminProfileForm.user_id
        )

        await callback.answer()

        if callback.message:
            await callback.message.answer(
                "Введите Telegram ID пользователя."
            )

        return

    if action == "profiles":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        photo_filter = (
            parts[3]
            if len(parts) > 3 and parts[3] in {"all", "yes", "no"}
            else "all"
        )
        sort = (
            parts[4]
            if len(parts) > 4 and parts[4] in {"updated", "collection"}
            else "updated"
        )
        limit = 10
        _ADMIN_LIST_FILTER[callback.from_user.id] = (photo_filter, sort)
        profiles = fetch_admin_profiles(
            photo_filter,
            sort,
            limit + 1,
            page * limit,
        )
        has_next = len(profiles) > limit
        profiles = profiles[:limit]

        if not profiles and page > 0:
            await callback.answer(
                "Анкет больше нет.",
                show_alert=True,
            )
            return

        if sort == "collection":
            sort_line = "Сортировка: уровень коллекции, от большего к меньшему."
        else:
            sort_line = "Сортировка: сначала новые."

        if photo_filter == "yes":
            photo_line = "Фото: только с фото."
        elif photo_filter == "no":
            photo_line = "Фото: только без фото."
        else:
            photo_line = "Фото: все анкеты."

        text = (
            f"<b>Анкеты</b> — страница {page + 1}\n"
            f"{photo_line}\n"
            f"{sort_line}"
        )

        if not profiles:
            text += "\n\nПо этому фильтру анкет нет."

        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                text,
                reply_markup=admin_profiles_markup(
                    profiles,
                    page,
                    photo_filter,
                    sort,
                    has_next,
                ),
            )

        return

    if action == "user":
        user_id = int(parts[2])
        profile = get_profile(user_id)

        if not profile:
            await callback.answer(
                "Анкета не найдена.",
                show_alert=True,
            )
            return

        text = (
            f"{profile_text(profile, reveal_contacts=False, contact_note=ADMIN_HIDDEN_NOTE)}\n\n"
            f"Показ: "
            f"{'включён' if profile['active'] else 'выключен'}\n"
            f"Premium: {'да' if has_premium(user_id) else 'нет'}\n"
            f"Приглашено: {referral_count(user_id)}"
        )

        await callback.answer()

        if callback.message:
            photo = profile_photo_source(profile["photo_file_id"])
            keyboard = admin_profile_keyboard(
                user_id,
                bool(profile["active"]),
                callback.from_user.id,
            )

            # Карточка как у игрока: фото и все поля, кроме скрытых.
            # Кнопки на фото; удаление идёт через edit_or_send, он умеет медиа.
            if photo and len(text) <= 1024:
                try:
                    await callback.message.answer_photo(
                        photo=photo,
                        caption=text,
                        reply_markup=keyboard,
                    )
                    return
                except Exception as error:
                    print(f"Фото анкеты {user_id}: {error}")
                    text += "\n\n⚠️ Фото анкеты недоступно."
            elif photo:
                try:
                    await callback.message.answer_photo(
                        photo=profile_photo_source(profile["photo_file_id"]),
                    )
                except Exception as error:
                    print(f"Фото анкеты {user_id}: {error}")
                    text += "\n\n⚠️ Фото анкеты недоступно."

            await callback.message.answer(
                text,
                reply_markup=keyboard,
            )

        return

    if action == "username":
        if len(parts) < 3 or not parts[2].isdigit():
            await callback.answer(
                "Анкета не найдена.",
                show_alert=True,
            )
            return

        target_id = int(parts[2])
        profile = get_profile(target_id)

        if not profile:
            await callback.answer(
                "Анкета не найдена.",
                show_alert=True,
            )
            return

        admin_id = callback.from_user.id
        admin_username = (
            stored_username(admin_id)
            or (callback.from_user.username or "")
        )
        target_username = stored_username(target_id)
        nickname = str(profile["nickname"] or "")

        try:
            log_username_view(
                admin_id,
                admin_username,
                target_id,
                target_username,
                nickname,
            )
        except Exception as error:
            DB.rollback()
            print(f"Лог юзернейма {target_id}: {error}")
            await callback.answer(
                "Не удалось открыть юзернейм.",
                show_alert=True,
            )
            return

        await callback.answer()

        if target_username:
            shown = "@" + html.escape(target_username)
        else:
            shown = "не задан"

        pubg_raw = str(profile["pubg_id"] or "").strip()
        pubg_shown = (
            f"<code>{html.escape(pubg_raw)}</code>"
            if pubg_raw
            else "не указан"
        )

        if callback.message:
            await callback.message.answer(
                "<b>Скрытые данные анкеты</b>\n"
                f"Анкета: {html.escape(nickname)}\n"
                f"Username: {shown}\n"
                f"Telegram ID: <code>{target_id}</code>\n"
                f"PUBG ID: {pubg_shown}"
            )

        return

    if action == "toggle":
        user_id = int(parts[2])
        profile = get_profile(user_id)

        if not profile:
            await callback.answer(
                "Анкета не найдена.",
                show_alert=True,
            )
            return

        new_active = 0 if profile["active"] else 1

        DB.execute(
            """
            UPDATE profiles
            SET active = ?
            WHERE user_id = ?
            """,
            (
                new_active,
                user_id,
            ),
        )

        DB.commit()

        await callback.answer(
            "Статус изменён."
        )

        if callback.message:
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=admin_profile_keyboard(
                        user_id,
                        bool(new_active),
                        callback.from_user.id,
                    )
                )
            except Exception as error:
                print(f"Не удалось обновить кнопки анкеты: {error}")
                await callback.message.answer(
                    "Статус показа изменён.",
                    reply_markup=admin_profile_keyboard(
                        user_id,
                        bool(new_active),
                        callback.from_user.id,
                    ),
                )

        return

    if action == "delete_confirm":
        user_id = int(parts[2])
        profile = get_profile(user_id)

        if not profile:
            await callback.answer(
                "Анкета не найдена.",
                show_alert=True,
            )
            return

        await callback.answer()

        if callback.message:
            nickname = html.escape(str(profile["nickname"] or "анкета"))
            await edit_or_send(
                callback.message,
                f"Удалить анкету «{nickname}» "
                f"(<code>{user_id}</code>)?",
                reply_markup=delete_confirmation_keyboard(
                    user_id,
                    admin=True,
                ),
            )

        return

    if action == "delete":
        user_id = int(parts[2])
        profile = get_profile(user_id)

        if not profile:
            await callback.answer(
                "Анкета уже удалена.",
                show_alert=True,
            )
            return

        nickname = html.escape(str(profile["nickname"] or "анкета"))

        try:
            delete_profile_completely(user_id)
        except Exception as error:
            print(f"Ошибка удаления анкеты {user_id}: {error}")
            await callback.answer(
                "Не удалось удалить анкету.",
                show_alert=True,
            )
            if callback.message:
                await callback.message.answer(
                    f"Не удалось удалить анкету:\n{error}",
                    parse_mode=None,
                )
            return

        await callback.answer(
            "Анкета удалена."
        )

        if callback.message:
            await edit_or_send(
                callback.message,
                f"✅ Анкета «{nickname}» "
                f"(<code>{user_id}</code>) удалена.",
                reply_markup=admin_keyboard(),
            )

        return

    if action == "stats":
        total = DB.execute(
            "SELECT COUNT(*) AS count FROM profiles"
        ).fetchone()["count"]

        active = DB.execute(
            """
            SELECT COUNT(*) AS count
            FROM profiles
            WHERE active = 1
            """
        ).fetchone()["count"]

        reports = DB.execute(
            """
            SELECT COUNT(*) AS count
            FROM reports
            WHERE resolved = 0
            """
        ).fetchone()["count"]

        seen_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).isoformat()
        active_users = DB.execute(
            """
            SELECT COUNT(*) AS count
            FROM users
            WHERE last_seen > ?
            """,
            (seen_cutoff,),
        ).fetchone()["count"]
        inactive_users = DB.execute(
            """
            SELECT COUNT(*) AS count
            FROM users
            WHERE last_seen <= ?
            """,
            (seen_cutoff,),
        ).fetchone()["count"]

        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                "📊 <b>Статистика</b>\n\n"
                f"Всего анкет: {total}\n"
                f"Активных анкет: {active}\n"
                f"Открытых жалоб: {reports}\n\n"
                f"Активных пользователей: {active_users}\n"
                "действовали менее суток назад\n"
                f"Неактивных пользователей: {inactive_users}\n"
                "не взаимодействовали более суток",
                reply_markup=admin_keyboard(),
            )

        return

    if action == "reports":
        reports = DB.execute(
            """
            SELECT
                r.id,
                r.reporter,
                r.reported,
                p.nickname
            FROM reports r
            LEFT JOIN profiles p
                ON p.user_id = r.reported
            WHERE r.resolved = 0
            ORDER BY r.created DESC
            LIMIT 20
            """
        ).fetchall()

        if not reports:
            await callback.answer()

            if callback.message:
                await edit_or_send(
                callback.message,
                    "Открытых жалоб нет.",
                    reply_markup=admin_keyboard(),
                )

            return

        buttons = []

        for report in reports:
            nickname = report["nickname"] or "анкета удалена"

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=(
                            f"Жалоба #{report['id']} — "
                            f"{nickname}"
                        )[:64],
                        callback_data=(
                            f"admin:report:{report['id']}"
                        ),
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    text="🏠 В меню",
                    callback_data="admin:menu",
                )
            ]
        )

        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                "Открытые жалобы:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=buttons,
                ),
            )

        return

    if action == "report":
        report_id = int(parts[2])

        report = DB.execute(
            """
            SELECT *
            FROM reports
            WHERE id = ?
              AND resolved = 0
            """,
            (report_id,),
        ).fetchone()

        if not report:
            await callback.answer(
"Жалоба не найдена.",
                show_alert=True,
            )
            return

        profile = get_profile(report["reported"])

        text = (
            f"Жалоба №{report_id}\n"
            f"Отправитель: "
            f"<code>{report['reporter']}</code>\n"
            f"На пользователя: "
            f"<code>{report['reported']}</code>"
        )

        if profile:
            text += (
                "\n\n"
                + profile_text(
                    profile,
                    reveal_contacts=False,
                    contact_note="",
                )
            )

        buttons = [
            [
                InlineKeyboardButton(
                    text="Узнать юзернейм",
                    callback_data=f"admin:username:{report['reported']}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить анкету",
                    callback_data=(
                        f"admin:delete_confirm:"
                        f"{report['reported']}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Закрыть жалобу",
                    callback_data=(
                        f"admin:resolve:{report_id}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К жалобам",
                    callback_data="admin:reports",
                ),
            ],
        ]

        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=buttons,
                ),
            )

        return

    if action == "resolve":
        report_id = int(parts[2])

        DB.execute(
            """
            UPDATE reports
            SET resolved = 1
            WHERE id = ?
            """,
            (report_id,),
        )

        DB.commit()

        await callback.answer(
            "Жалоба закрыта."
        )

        if callback.message:
            await edit_or_send(
                callback.message,
                "✅ Жалоба закрыта.",
                reply_markup=admin_keyboard(),
            )

        return

    if action == "broadcast":
        await state.set_state(BroadcastForm.content)
        await callback.answer()

        if callback.message:
            total = len(broadcast_user_ids())
            await callback.message.answer(
                "Отправьте сообщение для рассылки.\n"
                "Подойдут текст, фото, видео или файл — "
                "подписчики получат его как есть.\n\n"
                f"Сейчас в базе <b>{total}</b> пользователей.\n"
                "Отмена — /cancel"
            )

        return

    if action == "channels":
        await state.clear()
        await callback.answer()

        if callback.message:
            rows = list_required_channels()
            await edit_or_send(
                callback.message,
                channels_admin_text(rows),
                channels_admin_keyboard(rows),
            )

        return

    if action == "channel_add":
        await state.set_state(ChannelForm.links)
        await callback.answer()

        if callback.message:
            await callback.message.answer(
                "Пришлите одну или несколько ссылок на каналы.\n"
                "Каждая ссылка — с новой строки:\n"
                "• https://t.me/channel\n"
                "• @channel\n"
                "• приглашение https://t.me/+...\n\n"
                "Бот должен быть администратором канала.\n"
                "Для приватного канала после ссылки "
                "перешлите любой пост из него.\n\n"
                "Отмена — /cancel"
            )

        return

    if action == "channel_del":
        if len(parts) < 3 or not parts[2].isdigit():
            await callback.answer(
                "Канал не найден.",
                show_alert=True,
            )
            return

        delete_required_channel(int(parts[2]))
        await callback.answer("Канал удалён.")

        if callback.message:
            rows = list_required_channels()
            await edit_or_send(
                callback.message,
                channels_admin_text(rows),
                channels_admin_keyboard(rows),
            )

        return

    if action == "premium":
        await state.clear()
        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                "<b>Premium</b>\n\n"
                "Выдайте или заберите Premium.\n"
                "Можно выбрать анкету по имени "
                "или найти человека по Telegram ID / @username.",
                premium_admin_keyboard(),
            )

        return

    if action == "prem_list":
        if (
            len(parts) < 4
            or parts[2] not in {"grant", "revoke"}
            or not parts[3].isdigit()
        ):
            await callback.answer(
                "Некорректный запрос.",
                show_alert=True,
            )
            return

        mode = parts[2]
        page = int(parts[3])
        await state.clear()

        if not callback.message:
            await callback.answer()
            return

        shown = await render_premium_targets(
            callback.message,
            mode,
            page,
        )

        if shown:
            await callback.answer()
        else:
            await callback.answer(
                "Дальше анкет нет.",
                show_alert=True,
            )

        return

    if action == "prem_query":
        if len(parts) < 3 or parts[2] not in {"grant", "revoke"}:
            await callback.answer(
                "Некорректный запрос.",
                show_alert=True,
            )
            return

        mode = parts[2]
        await state.set_state(PremiumAdminForm.query)
        await state.update_data(premium_mode=mode)
        await callback.answer()

        if callback.message:
            verb = "выдать" if mode == "grant" else "забрать"
            await callback.message.answer(
                f"Кому {verb} Premium?\n"
                "Пришлите Telegram ID, @username "
                "или имя из анкеты.\n\n"
                "Отмена — /cancel"
            )

        return

    if action == "prem_set":
        if (
            len(parts) < 4
            or not parts[2].isdigit()
            or parts[3] not in {"grant", "revoke"}
        ):
            await callback.answer(
                "Некорректный запрос.",
                show_alert=True,
            )
            return

        target_id = int(parts[2])
        grant = parts[3] == "grant"

        try:
            set_premium_override(target_id, grant)
        except Exception as error:
            print(f"Premium {target_id}: {error}")
            await callback.answer(
                "Не удалось изменить Premium.",
                show_alert=True,
            )
            return

        await notify_premium_changed(bot, target_id, grant)
        await callback.answer("Готово.")

        if callback.message:
            verb = "выдан" if grant else "снят"
            await edit_or_send(
                callback.message,
                f"Premium {verb}: "
                f"{html.escape(user_plain_label(target_id))}",
                premium_admin_keyboard(),
            )

        return

    if action == "problems":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        limit = 8
        rows = DB.execute(
            """
            SELECT id, user_id, username, text, created
            FROM problems
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit + 1, page * limit),
        ).fetchall()
        has_next = len(rows) > limit
        rows = rows[:limit]

        if not rows and page > 0:
            await callback.answer(
                "Дальше проблем нет.",
                show_alert=True,
            )
            return

        await callback.answer()

        if not callback.message:
            return

        buttons = []

        for row in rows:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=problem_list_label(row),
                        callback_data=f"admin:problem:{int(row['id'])}",
                    )
                ]
            )

        navigation = []

        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="⬅️",
                    callback_data=f"admin:problems:{page - 1}",
                )
            )

        if has_next:
            navigation.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=f"admin:problems:{page + 1}",
                )
            )

        if navigation:
            buttons.append(navigation)

        buttons.append(
            [
                InlineKeyboardButton(
                    text="⬅️ В админ-панель",
                    callback_data="admin:menu",
                )
            ]
        )

        if rows:
            text = (
                f"<b>Проблемы</b> — страница {page + 1}\n\n"
                "Каждая кнопка — отдельное сообщение: "
                "тег отправителя и текст проблемы.\n"
                "Нажмите, чтобы открыть полностью."
            )
        else:
            text = (
                "<b>Проблемы</b>\n\n"
                "Пока никто не сообщал о проблемах."
            )

        await edit_or_send(
            callback.message,
            text,
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return

    if action == "problem":
        if len(parts) < 3 or not parts[2].isdigit():
            await callback.answer(
                "Проблема не найдена.",
                show_alert=True,
            )
            return

        problem_id = int(parts[2])
        problem = get_problem(problem_id)

        if not problem:
            await callback.answer(
                "Проблема не найдена.",
                show_alert=True,
            )
            return

        admin_id = callback.from_user.id
        admin_username = (
            stored_username(admin_id)
            or (callback.from_user.username or "")
        )

        try:
            log_problem_view(admin_id, admin_username, problem)
        except Exception as error:
            DB.rollback()
            print(f"Лог проблемы {problem_id}: {error}")
            await callback.answer(
                "Не удалось открыть проблему.",
                show_alert=True,
            )
            return

        await callback.answer()

        if not callback.message:
            return

        reporter_id = int(problem["user_id"])
        tag = html.escape(
            problem_sender_tag(reporter_id, problem["username"])
        )
        body = html.escape(str(problem["text"] or ""))
        text = (
            f"<b>Проблема #{problem_id}</b>\n"
            f"От: {tag}\n"
            f"Telegram ID: <code>{reporter_id}</code>\n"
            f"Когда: {html.escape(format_msk(problem['created']))}\n\n"
            f"{body}"
        )

        if len(text) > 4000:
            text = text[:3999] + "…"

        await edit_or_send(
            callback.message,
            text,
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ К проблемам",
                            callback_data="admin:problems:0",
                        )
                    ]
                ]
            ),
        )
        return

    if action == "messages":
        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                "Выберите шаблон:",
                reply_markup=admin_messages_keyboard(),
            )

        return


# ============================================================
# АДМИНКА: ДОБАВЛЕНИЕ ИЗМЕНЕНИЕ ЧУЖОЙ АНКЕТЫ
# ============================================================

@router.message(
    StateFilter(PremiumAdminForm.query),
    F.text,
)
async def admin_premium_query(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    mode = data.get("premium_mode")

    if mode not in {"grant", "revoke"}:
        await state.clear()
        return

    text = (message.text or "").strip()

    if text.startswith("/"):
        await message.answer(
            "Пришлите ID, @username или имя анкеты, либо /cancel."
        )
        return

    matches = find_premium_targets(text)

    if not matches:
        await message.answer(
            "Никого не нашёл. Проверьте Telegram ID, "
            "@username или имя анкеты."
        )
        return

    if len(matches) == 1:
        target_id = matches[0]
        grant = mode == "grant"

        try:
            set_premium_override(target_id, grant)
        except Exception as error:
            print(f"Premium {target_id}: {error}")
            await message.answer(
                "Не удалось изменить Premium. Попробуйте ещё раз."
            )
            return

        await state.clear()
        await notify_premium_changed(bot, target_id, grant)
        verb = "выдан" if grant else "снят"
        await message.answer(
            f"Premium {verb}: "
            f"{html.escape(user_plain_label(target_id))}",
            reply_markup=premium_admin_keyboard(),
        )
        return

    await state.clear()
    buttons = [
        [
            InlineKeyboardButton(
                text=user_plain_label(user_id)[:64],
                callback_data=f"admin:prem_set:{user_id}:{mode}",
            )
        ]
        for user_id in matches[:20]
    ]
    await message.answer(
        "Нашлось несколько человек. Выберите:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(
    StateFilter(AdminProfileForm.user_id),
    F.text,
)
async def admin_profile_user_id(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    value = message.text.strip()

    if not value.isdigit() or int(value) <= 0:
        await message.answer(
            "Введите корректный числовой Telegram ID."
        )
        return

    await start_profile_form(
        message,
        state,
        admin=True,
        target_user_id=int(value),
    )


# ============================================================
# АДМИНКА: ШАБЛОННЫЕ СООБЩЕНИЯ
# ============================================================

def template_media_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id

    document = message.document
    mime = (document.mime_type or "") if document else ""

    if document and mime.startswith("image/"):
        return f"document:{document.file_id}"

    return None


async def template_text_for_media(
    message: Message,
    state: FSMContext,
) -> str | None:
    data = await state.get_data()
    key = data.get("message_key")

    if not key:
        await message.answer(
            "Сессия редактирования сброшена. Откройте шаблон заново."
        )
        await state.clear()
        return None

    current = await state.get_state()
    caption = (message.caption or "").strip()

    # На шаге текста подпись к фото становится новым текстом.
    # На шаге фото текст уже введён отдельно — подпись не затирает его.
    if current == AdminMessageForm.text.state and caption:
        text = caption
    else:
        text = (data.get("message_text") or "").strip()
        if not text:
            text = get_message_text(key).strip()

    if not text:
        await message.answer(
            "Сначала отправьте текст шаблона "
            "или фото с подписью."
        )
        return None

    return text


@router.callback_query(
    F.data.startswith("admin_message:")
)
async def admin_message_select(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    key = callback.data.split(":", 1)[1]

    if key not in DEFAULT_MESSAGES:
        await callback.answer(
            "Неизвестный шаблон.",
            show_alert=True,
        )
        return

    row = get_message_row(key)

    if row:
        text = row["message_text"]
        photo_file_id = row["photo_file_id"]
    else:
        text = DEFAULT_MESSAGES.get(key, "")
        photo_file_id = None

    await state.clear()

    await state.update_data(
        message_key=key,
        message_text=text,
        old_photo_file_id=photo_file_id,
    )

    await state.set_state(
        AdminMessageForm.text
    )

    await callback.answer()

    if not callback.message:
        return

    hint = (
        "Редактирование шаблона.\n\n"
        "• Отправьте новый текст, затем фото.\n"
        "• Или сразу пришлите фото: подпись станет текстом. "
        "Без подписи останется текущий текст, сменится только фото.\n"
        "• Фото можно прислать и файлом-картинкой.\n\n"
        "Как шаблон выглядит сейчас:"
    )

    if key == "premium":
        hint += (
            "\n\nЭто текст блока «что откроется» в "
            "«Premium за приглашения». Он виден, пока Premium "
            "не активен. Ссылку, счётчик и строку "
            "«Premium активен до …» бот добавляет сам."
        )
    elif key == "tiktok":
        hint += (
            "\n\nЭтот текст показывается по кнопке "
            "«Заработай на тик-токах»."
        )
    elif key == "reengage":
        hint += (
            "\n\nЭто сообщение уходит каждые 24 часа тем, "
            "кто сутки ничего не писал боту и не нажимал кнопки."
        )

    await callback.message.answer(hint)
    await send_template(callback.message, key)


@router.message(
    StateFilter(AdminMessageForm.text),
    F.text,
)
async def admin_message_text(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = message.text.strip()

    if not text or text.startswith("/"):
        await message.answer(
            "Отправьте текст шаблона обычным сообщением.\n"
            "Отмена — /cancel."
        )
        return

    await state.update_data(
        message_text=text,
    )

    await state.set_state(
        AdminMessageForm.photo
    )

    await message.answer(
        "Текст принят. Теперь отправьте фото.\n\n"
        "/skip — оставить старое фото.\n"
        "/delete_photo — убрать фото."
    )


@router.message(
    StateFilter(
        AdminMessageForm.text,
        AdminMessageForm.photo,
    ),
    F.photo,
)
@router.message(
    StateFilter(
        AdminMessageForm.text,
        AdminMessageForm.photo,
    ),
    F.document,
)
async def admin_message_photo(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    photo_file_id = template_media_id(message)

    if not photo_file_id:
        await message.answer(
            "Нужно изображение: обычное фото или файл-картинка."
        )
        return

    data = await state.get_data()
    text = await template_text_for_media(message, state)

    if not text:
        return

    await finish_template_save(
        message,
        state,
        data["message_key"],
        text,
        photo_file_id,
        "✅ Текст и фото сохранены. "
        "Так шаблон выглядит у пользователей:",
    )


@router.message(
    StateFilter(AdminMessageForm.photo),
    Command("skip"),
)
async def admin_message_skip_photo(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    old_photo = data.get("old_photo_file_id")
    notice = (
        "✅ Текст сохранён, прежнее фото оставлено. "
        "Так шаблон выглядит у пользователей:"
        if old_photo
        else "✅ Текст сохранён, фото по-прежнему нет. "
        "Так шаблон выглядит у пользователей:"
    )

    await finish_template_save(
        message,
        state,
        data["message_key"],
        data["message_text"],
        old_photo,
        notice,
    )


@router.message(
    StateFilter(AdminMessageForm.photo),
    Command("delete_photo"),
)
async def admin_message_delete_photo(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()

    await finish_template_save(
        message,
        state,
        data["message_key"],
        data["message_text"],
        None,
        "✅ Текст сохранён без фото. "
        "Так шаблон выглядит у пользователей:",
    )


@router.message(
    StateFilter(AdminMessageForm.text),
)
async def admin_message_text_invalid(message: Message):
    await message.answer(
        "Отправьте текст шаблона или фото.\n"
        "Подпись к фото станет новым текстом."
    )


@router.message(
    StateFilter(AdminMessageForm.photo),
)
async def admin_message_invalid_photo(message: Message):
    await message.answer(
        "Отправьте фото (или файл-картинку), "
        "/skip или /delete_photo."
    )


# ============================================================
# ПОДПИСКА НА КАНАЛЫ
# ============================================================

@router.callback_query(F.data == "sub:check")
async def subscription_check(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
):
    missing = await missing_channels(
        bot,
        callback.from_user.id,
    )

    if missing:
        await callback.answer(
            "Вы не подписались",
            show_alert=True,
        )

        if callback.message:
            try:
                await callback.message.edit_text(
                    subscription_required_text(missing),
                    reply_markup=subscription_keyboard(missing),
                )
            except Exception as error:
                print(f"Не удалось обновить экран подписки: {error}")

        return

    await callback.answer("Подписка подтверждена.")
    await state.clear()

    if callback.message:
        await callback.message.answer(
            "Подписка есть, бот открыт."
        )
        await send_template(
            callback.message,
            "start",
            keyboard_for(callback.from_user.id),
        )


# ============================================================
# РАССЫЛКА
# ============================================================

@router.message(StateFilter(BroadcastForm.content))
async def broadcast_content(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    if message.text and message.text.startswith("/"):
        await message.answer(
            "Пришлите текст или медиа для рассылки, либо /cancel."
        )
        return

    await state.update_data(
        broadcast_chat_id=message.chat.id,
        broadcast_message_id=message.message_id,
    )

    total = len(broadcast_user_ids())

    await message.answer(
        "Отправить это сообщение "
        f"<b>{total}</b> пользователям?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📣 Отправить",
                        callback_data="broadcast:send",
                    ),
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data="broadcast:cancel",
                    ),
                ]
            ]
        ),
    )


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    await state.clear()
    await callback.answer("Рассылка отменена.")

    if callback.message:
        await edit_or_send(
            callback.message,
            "Рассылка отменена.",
            reply_markup=admin_keyboard(),
        )


@router.callback_query(F.data == "broadcast:send")
async def broadcast_send(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    from_chat_id = data.get("broadcast_chat_id")
    message_id = data.get("broadcast_message_id")

    if not from_chat_id or not message_id:
        await state.clear()
        await callback.answer(
            "Сообщение для рассылки не найдено. Начните заново.",
            show_alert=True,
        )
        return

    user_ids = broadcast_user_ids()

    if not user_ids:
        await callback.answer(
            "Пока некому отправлять: бота ещё никто не запускал.",
            show_alert=True,
        )
        return

    await state.clear()
    await callback.answer("Рассылка запущена.")

    if callback.message:
        await edit_or_send(
            callback.message,
            f"Отправляю рассылку: {len(user_ids)} получателей...",
        )

    sent = 0
    failed = 0

    for user_id in user_ids:
        delivered = False

        for attempt in range(2):
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
                sent += 1
                delivered = True
                break
            except Exception as error:
                retry_after = getattr(error, "retry_after", None)

                if retry_after and attempt == 0:
                    await asyncio.sleep(float(retry_after) + 0.1)
                    continue

                print(f"Рассылка {user_id}: {error}")
                break

        if not delivered:
            failed += 1

        await asyncio.sleep(0.05)

    if callback.message:
        await callback.message.answer(
            "✅ Рассылка завершена.\n"
            f"Доставлено: {sent}\n"
            f"Не доставлено: {failed}",
            reply_markup=admin_keyboard(),
        )


# ============================================================
# АДМИНКА: КАНАЛЫ ДЛЯ ПОДПИСКИ
# ============================================================

@router.message(StateFilter(ChannelForm.links))
async def admin_channel_links(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = (message.text or "").strip()

    if text.startswith("/"):
        await message.answer(
            "Пришлите ссылки на каналы или /cancel."
        )
        return

    blob = "\n".join(
        part
        for part in (text, (message.caption or "").strip())
        if part
    )
    links = extract_channel_links(blob)
    forwarded = forwarded_channel_chat(message)
    data = await state.get_data()
    notes = []
    saved_username = None
    consumed_invite = None

    if forwarded is not None:
        note, consumed_invite = await save_from_forward(
            bot,
            state,
            forwarded,
            links,
        )
        notes.append(note)

        if note.startswith("✅"):
            saved_username = (
                getattr(forwarded, "username", None) or ""
            ).lower()

    pending_chat_id = data.get("pending_chat_id")
    pending_title = data.get("pending_title")

    if forwarded is not None:
        fresh = await state.get_data()
        pending_chat_id = fresh.get("pending_chat_id")
        pending_title = fresh.get("pending_title")

    for item in links:
        if (
            consumed_invite
            and item["kind"] == "invite"
            and item["url"] == consumed_invite
        ):
            continue

        if saved_username and (item.get("username") or "").lower() == saved_username:
            continue

        if item["kind"] == "invite" and pending_chat_id:
            if not await bot_can_check(bot, int(pending_chat_id)):
                notes.append(
                    "Бот не администратор этого канала. "
                    "Добавьте его и перешлите пост снова."
                )
                continue

            save_required_channel(
                int(pending_chat_id),
                pending_title,
                item["url"],
            )
            await state.update_data(
                pending_chat_id=None,
                pending_title=None,
                pending_invite_url=None,
            )
            pending_chat_id = None
            notes.append(f"✅ Приватный канал сохранён: {item['url']}")
            continue

        if item["kind"] == "invite":
            await state.update_data(pending_invite_url=item["url"])
            notes.append(
                "Ссылку-приглашение запомнил. Теперь перешлите "
                "любой пост из этого канала. Бот должен быть "
                "его администратором."
            )
            continue

        notes.append(
            await try_add_public_channel(
                bot,
                item["username"],
                item["url"],
            )
        )

    if not notes:
        await message.answer(
            "Не вижу ссылок. Пришлите https://t.me/channel, "
            "@channel или перешлите пост из канала."
        )
        return

    await message.answer(
        "\n\n".join(notes),
        parse_mode=None,
    )

    waiting = any(
        "перешлите" in note.lower() or "запомнил" in note.lower()
        for note in notes
    )

    if not waiting:
        await state.clear()

    rows = list_required_channels()
    await message.answer(
        channels_admin_text(rows),
        reply_markup=channels_admin_keyboard(rows),
    )


# ============================================================
# ЗАПУСК
# ============================================================

def superadmin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назначить админа",
                    callback_data="super:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Снять админа",
                    callback_data="super:remove",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Логи",
                    callback_data="super:logs:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Тиктоки",
                    callback_data="super:tiktoks:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Админ-панель",
                    callback_data="admin:menu",
                )
            ],
        ]
    )


def person_label(user_id: int) -> str:
    username = stored_username(user_id)
    profile = get_profile(user_id)
    nickname = ""

    if profile and profile["nickname"]:
        nickname = str(profile["nickname"])

    if username and nickname:
        return f"{nickname} · @{username}"

    if username:
        return f"@{username}"

    if nickname:
        return f"{nickname} · id {user_id}"

    return f"id {user_id}"


def resolve_admin_query(text: str) -> list[int] | None:
    raw = (text or "").strip()

    if not raw or raw.startswith("/"):
        return None

    if raw.isdigit():
        user_id = int(raw)
        return [user_id] if user_id > 0 else []

    username = raw[1:] if raw.startswith("@") else raw

    if not re.fullmatch(r"[A-Za-z0-9_]{4,}", username):
        return []

    rows = DB.execute(
        """
        SELECT user_id
        FROM users
        WHERE username = ? COLLATE NOCASE
        """,
        (username,),
    ).fetchall()

    return [int(row["user_id"]) for row in rows]


def removable_admin_ids() -> list[int]:
    found: list[int] = []
    seen: set[int] = set()

    def add(user_id: int) -> None:
        user_id = int(user_id)

        if user_id <= 0 or user_id in seen or user_id in SUPERADMIN_IDS:
            return

        seen.add(user_id)
        found.append(user_id)

    for user_id in sorted(ADMIN_IDS):
        if is_env_admin_revoked(user_id):
            continue

        add(user_id)

    rows = DB.execute(
        """
        SELECT user_id
        FROM admins
        ORDER BY created DESC, user_id
        """
    ).fetchall()

    for row in rows:
        add(int(row["user_id"]))

    return found


def admin_remove_label(user_id: int) -> str:
    label = person_label(user_id)
    marks = []

    if user_id in ADMIN_IDS and not is_env_admin_revoked(user_id):
        marks.append("ADMIN_IDS")

    if is_db_admin(user_id):
        marks.append("назначен")

    if marks:
        label = f"{label} · {', '.join(marks)}"

    return label[:64]


async def notify_admin_role(
    bot: Bot,
    user_id: int,
    enabled: bool,
) -> None:
    if enabled:
        text = (
            "Вам выдали права администратора.\n"
            "Команда: /admin"
        )
    else:
        text = "Права администратора сняты."

    try:
        await bot.send_message(user_id, text)
    except Exception as error:
        print(f"Не удалось сообщить о роли {user_id}: {error}")


async def appoint_admin_user(
    bot: Bot,
    actor_id: int,
    target_id: int,
) -> str:
    if target_id <= 0:
        return "Некорректный Telegram ID."

    if (
        is_superadmin(target_id)
        or (
            target_id in ADMIN_IDS
            and not is_env_admin_revoked(target_id)
        )
    ):
        return "Этот человек уже администратор из настроек запуска."

    if is_db_admin(target_id):
        return "Этот человек уже назначен админом."

    grant_admin(target_id, actor_id)
    await notify_admin_role(bot, target_id, True)
    known = get_user_row(target_id)
    extra = ""

    if not known:
        extra = (
            "\nЧеловек ещё не открывал бота. "
            "Права заработают, когда он запустит его."
        )

    return f"Админ назначен: {html.escape(person_label(target_id))}.{extra}"


def fetch_admin_logs(limit: int, offset: int):
    return DB.execute(
        """
        SELECT
            kind,
            id,
            created,
            admin_id,
            admin_username,
            title,
            extra
        FROM (
            SELECT
                'username' AS kind,
                id,
                created,
                admin_id,
                admin_username,
                profile_nickname AS title,
                target_username AS extra
            FROM username_view_logs
            UNION ALL
            SELECT
                'problem' AS kind,
                id,
                created,
                admin_id,
                admin_username,
                problem_text AS title,
                reporter_username AS extra
            FROM problem_view_logs
        )
        ORDER BY created DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()


def admin_log_label(row) -> str:
    when = format_msk(row["created"])
    admin_name = (
        f"@{row['admin_username']}"
        if row["admin_username"]
        else f"id {row['admin_id']}"
    )

    if row["kind"] == "problem":
        reporter = str(row["extra"] or "").strip()
        tag = f"@{reporter}" if reporter else "без тега"
        snippet = " ".join(str(row["title"] or "").split())
        label = f"{when} · {admin_name} · {tag}: {snippet}"
    else:
        nickname = str(row["title"] or "анкета")
        label = f"{when} · {admin_name} · {nickname}"

    return label[:64]


def username_logs_view(page: int) -> tuple[str, InlineKeyboardMarkup, bool]:
    limit = 8
    rows = fetch_admin_logs(limit + 1, page * limit)
    has_next = len(rows) > limit
    rows = rows[:limit]
    has_prev = page > 0

    if not rows:
        text = (
            "<b>Логи</b>\n\n"
            "Пока нет записей: админы не открывали "
            "юзернеймы и проблемы."
            if page == 0
            else "<b>Логи</b>\n\nДальше записей нет."
        )
        return text, logs_keyboard(page, [], has_prev, False), has_next

    text = (
        "<b>Логи</b>\n\n"
        "Каждая кнопка — просмотр юзернейма или проблемы.\n"
        "Нажмите, чтобы открыть запись."
    )
    return text, logs_keyboard(page, rows, has_prev, has_next), has_next


def logs_keyboard(
    page: int,
    rows,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    buttons = []

    for row in rows:
        if row["kind"] == "problem":
            callback_data = f"super:plog:{int(row['id'])}"
        else:
            callback_data = f"super:log:{int(row['id'])}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=admin_log_label(row),
                    callback_data=callback_data,
                )
            ]
        )

    navigation = []

    if has_prev:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"super:logs:{page - 1}",
            )
        )

    if has_next:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"super:logs:{page + 1}",
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ В панель",
                callback_data="super:menu",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def tiktok_people(limit: int, offset: int):
    return DB.execute(
        """
        SELECT
            user_id,
            COUNT(*) AS count,
            MAX(created) AS last_created
        FROM tiktok_submissions
        GROUP BY user_id
        ORDER BY last_created DESC, user_id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()


def tiktok_people_keyboard(page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    limit = 8
    rows = tiktok_people(limit + 1, page * limit)
    has_next = len(rows) > limit
    rows = rows[:limit]

    if not rows and page == 0:
        return (
            "<b>Тиктоки</b>\n\nПока никто не отправлял ссылки на выплату.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ В панель",
                            callback_data="super:menu",
                        )
                    ]
                ]
            ),
        )

    if not rows:
        return "", None

    buttons = []

    for row in rows:
        user_id = int(row["user_id"])
        username = stored_username(user_id)
        count = int(row["count"])

        if username:
            label = f"@{username} · {count}"
        else:
            label = f"{person_label(user_id)} · {count}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"super:tiktok_user:{user_id}",
                )
            ]
        )

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"super:tiktoks:{page - 1}",
            )
        )

    if has_next:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"super:tiktoks:{page + 1}",
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ В панель",
                callback_data="super:menu",
            )
        ]
    )

    return (
        "<b>Тиктоки</b>\n\n"
        "Каждая кнопка — отдельный человек. "
        "На ней юзернейм и число ссылок.",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def tiktok_user_text(user_id: int) -> str:
    rows = DB.execute(
        """
        SELECT link, created
        FROM tiktok_submissions
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    ).fetchall()
    username = stored_username(user_id)
    shown = f"@{html.escape(username)}" if username else "не задан"
    profile = get_profile(user_id)
    nickname = (
        html.escape(str(profile["nickname"]))
        if profile and profile["nickname"]
        else "нет анкеты"
    )
    lines = [
        "<b>Тиктоки на выплату</b>",
        f"Юзернейм: {shown}",
        f"Telegram ID: <code>{user_id}</code>",
        f"Анкета: {nickname}",
        "",
    ]

    if not rows:
        lines.append("Ссылок нет.")
        return "\n".join(lines)

    for index, row in enumerate(rows[:30], start=1):
        lines.append(
            f"{index}. {html.escape(format_msk(row['created']))}\n"
            f"{html.escape(row['link'])}"
        )

    if len(rows) > 30:
        lines.append("")
        lines.append(f"И ещё {len(rows) - 30} более ранних ссылок.")

    return "\n".join(lines)


@router.callback_query(F.data.startswith("super:"))
async def superadmin_callback(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
):
    if not is_superadmin(callback.from_user.id):
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        await state.clear()
        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                "Панель супер-админа:",
                reply_markup=superadmin_keyboard(),
            )

        return

    if action == "add":
        await state.set_state(SuperAdminForm.admin_query)
        await callback.answer()

        if callback.message:
            await callback.message.answer(
                "Кого назначить админом?\n"
                "Пришлите Telegram ID или @username.\n\n"
                "Отмена — /cancel"
            )

        return

    if action == "remove":
        await state.clear()
        ids = [
            user_id
            for user_id in removable_admin_ids()
            if user_id != callback.from_user.id
        ]
        await callback.answer()

        if not callback.message:
            return

        if not ids:
            await edit_or_send(
                callback.message,
                "<b>Снять админа</b>\n\n"
                "Снимать некого.\n"
                "Админов из ADMIN_IDS можно снять здесь.\n"
                "Себя и супер-админов из SUPERADMIN_IDS — нет.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ В панель",
                                callback_data="super:menu",
                            )
                        ]
                    ]
                ),
            )
            return

        buttons = [
            [
                InlineKeyboardButton(
                    text=admin_remove_label(user_id),
                    callback_data=f"super:revoke:{user_id}",
                )
            ]
            for user_id in ids[:30]
        ]
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⬅️ В панель",
                    callback_data="super:menu",
                )
            ]
        )
        await edit_or_send(
            callback.message,
            "<b>Снять админа</b>\n\n"
            "Выберите человека.\n"
            "Можно снять админа из ADMIN_IDS "
            "и назначенного через эту панель.\n"
            "Супер-админа из SUPERADMIN_IDS снять нельзя.",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return

    if action == "revoke" and len(parts) > 2 and parts[2].isdigit():
        target_id = int(parts[2])
        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                "Снять права администратора у "
                f"{html.escape(person_label(target_id))}?",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Да, снять",
                                callback_data=f"super:revoke_yes:{target_id}",
                            ),
                            InlineKeyboardButton(
                                text="Отмена",
                                callback_data="super:remove",
                            ),
                        ]
                    ]
                ),
            )

        return

    if action == "revoke_yes" and len(parts) > 2 and parts[2].isdigit():
        target_id = int(parts[2])

        if target_id == callback.from_user.id:
            await callback.answer(
                "Себя снять нельзя.",
                show_alert=True,
            )
            return

        if target_id in SUPERADMIN_IDS:
            await callback.answer(
                "Супер-админа из SUPERADMIN_IDS нельзя снять отсюда.",
                show_alert=True,
            )
            return

        removed_env = False
        removed_db = False

        if target_id in ADMIN_IDS and not is_env_admin_revoked(target_id):
            revoke_env_admin(target_id, callback.from_user.id)
            removed_env = True

        if is_db_admin(target_id):
            revoke_admin(target_id)
            removed_db = True

        if not removed_env and not removed_db:
            await callback.answer(
                "Этот админ уже снят.",
                show_alert=True,
            )
            return

        await notify_admin_role(bot, target_id, False)
        await callback.answer("Права сняты.")

        if callback.message:
            await edit_or_send(
                callback.message,
                "Права сняты: "
                f"{html.escape(person_label(target_id))}.",
                superadmin_keyboard(),
            )

        return

    if action == "grant" and len(parts) > 2 and parts[2].isdigit():
        await state.clear()
        text = await appoint_admin_user(
            bot,
            callback.from_user.id,
            int(parts[2]),
        )
        await callback.answer("Готово.")

        if callback.message:
            await callback.message.answer(
                text,
                reply_markup=superadmin_keyboard(),
            )

        return

    if action == "logs" and len(parts) > 2 and parts[2].isdigit():
        page = int(parts[2])
        text, markup, has_next = username_logs_view(page)

        if page > 0 and not has_next and "Дальше записей нет" in text:
            await callback.answer(
                "Дальше записей нет.",
                show_alert=True,
            )
            return

        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                text,
                markup,
            )

        return

    if action == "log" and len(parts) > 2 and parts[2].isdigit():
        log_id = int(parts[2])
        row = DB.execute(
            """
            SELECT *
            FROM username_view_logs
            WHERE id = ?
            """,
            (log_id,),
        ).fetchone()

        if not row:
            await callback.answer(
                "Запись не найдена.",
                show_alert=True,
            )
            return

        await callback.answer()

        if not callback.message:
            return

        target_id = int(row["target_id"])
        username = str(row["target_username"] or "").strip()
        shown = f"@{html.escape(username)}" if username else "не задан"
        admin_name = (
            f"@{row['admin_username']}"
            if row["admin_username"]
            else f"id {row['admin_id']}"
        )
        nickname = html.escape(str(row["profile_nickname"] or "без имени"))
        header = (
            "<b>Просмотр юзернейма</b>\n"
            f"Когда: {html.escape(format_msk(row['created']))}\n"
            f"Кто смотрел: {html.escape(admin_name)} "
            f"(<code>{row['admin_id']}</code>)\n"
            f"Анкета: {nickname}\n"
            f"Юзернейм: {shown}\n"
            f"Telegram ID: <code>{target_id}</code>"
        )
        back = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К логам",
                        callback_data="super:logs:0",
                    )
                ]
            ]
        )
        profile = get_profile(target_id)

        if not profile:
            await edit_or_send(
                callback.message,
                header + "\n\nАнкета уже удалена.",
                back,
            )
            return

        await edit_or_send(
            callback.message,
            header,
            back,
        )
        await send_profile(
            callback.message,
            profile,
            reveal_contacts=True,
        )
        return

    if action == "plog" and len(parts) > 2 and parts[2].isdigit():
        log_id = int(parts[2])
        row = DB.execute(
            """
            SELECT *
            FROM problem_view_logs
            WHERE id = ?
            """,
            (log_id,),
        ).fetchone()

        if not row:
            await callback.answer(
                "Запись не найдена.",
                show_alert=True,
            )
            return

        await callback.answer()

        if not callback.message:
            return

        reporter = str(row["reporter_username"] or "").strip()
        shown = f"@{html.escape(reporter)}" if reporter else "не задан"
        admin_name = (
            f"@{row['admin_username']}"
            if row["admin_username"]
            else f"id {row['admin_id']}"
        )
        body = html.escape(str(row["problem_text"] or ""))
        text = (
            "<b>Просмотр проблемы</b>\n"
            f"Когда: {html.escape(format_msk(row['created']))}\n"
            f"Кто смотрел: {html.escape(admin_name)} "
            f"(<code>{row['admin_id']}</code>)\n"
            f"Проблема №{int(row['problem_id'])}\n"
            f"Отправитель: {shown}\n"
            f"Telegram ID: <code>{row['reporter_id']}</code>\n\n"
            f"{body}"
        )

        if len(text) > 4000:
            text = text[:3999] + "…"

        await edit_or_send(
            callback.message,
            text,
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ К логам",
                            callback_data="super:logs:0",
                        )
                    ]
                ]
            ),
        )
        return

    if action == "tiktoks" and len(parts) > 2 and parts[2].isdigit():
        page = int(parts[2])
        text, markup = tiktok_people_keyboard(page)

        if not text:
            await callback.answer(
                "Дальше никого нет.",
                show_alert=True,
            )
            return

        await callback.answer()

        if callback.message and markup is not None:
            await edit_or_send(
                callback.message,
                text,
                markup,
            )

        return

    if action == "tiktok_user" and len(parts) > 2 and parts[2].isdigit():
        target_id = int(parts[2])
        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                tiktok_user_text(target_id),
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ К людям",
                                callback_data="super:tiktoks:0",
                            )
                        ]
                    ]
                ),
            )

        return

    await callback.answer(
        "Некорректный запрос.",
        show_alert=True,
    )


@router.message(
    StateFilter(SuperAdminForm.admin_query),
    F.text,
)
async def superadmin_appoint_query(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if not is_superadmin(message.from_user.id):
        await state.clear()
        return

    matches = resolve_admin_query(message.text or "")

    if matches is None:
        await message.answer(
            "Пришлите Telegram ID или @username, либо /cancel."
        )
        return

    if not matches:
        await message.answer(
            "Никого не нашёл. Нужен Telegram ID или @username "
            "человека, который уже открывал бота."
        )
        return

    if len(matches) == 1:
        await state.clear()
        text = await appoint_admin_user(
            bot,
            message.from_user.id,
            matches[0],
        )
        await message.answer(
            text,
            reply_markup=superadmin_keyboard(),
        )
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=person_label(user_id)[:64],
                callback_data=f"super:grant:{user_id}",
            )
        ]
        for user_id in matches[:20]
    ]
    await message.answer(
        "Нашлось несколько человек. Выберите:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "tiktok:submit")
async def tiktok_submit_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await state.set_state(TikTokForm.link)
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "Пришлите ссылку на тикток.\n"
            "Можно несколько ссылок, каждую с новой строки.\n"
            "Обычный текст бот не примет.\n\n"
            "Отмена — /cancel"
        )


@router.message(
    StateFilter(TikTokForm.link),
    F.text,
)
async def tiktok_submit_link(
    message: Message,
    state: FSMContext,
):
    links = extract_submission_links(message.text or "")

    if not links:
        await message.answer(
            "Нужна только ссылка, например https://www.tiktok.com/...\n"
            "Отмена — /cancel."
        )
        return

    saved = 0
    duplicates = 0

    for link in links:
        try:
            if add_tiktok_submission(message.from_user.id, link):
                saved += 1
            else:
                duplicates += 1
        except Exception as error:
            DB.rollback()
            print(f"Тикток {message.from_user.id}: {error}")
            await message.answer(
                "Не удалось сохранить ссылку. Отправьте её ещё раз.",
                parse_mode=None,
            )
            return

    lines = []

    if saved:
        lines.append(f"Принято ссылок: {saved}.")

    if duplicates:
        lines.append(f"Уже были отправлены: {duplicates}.")

    lines.append("Можно прислать ещё одну ссылку или нажать /cancel.")
    await message.answer("\n".join(lines))


@router.message(StateFilter(TikTokForm.link))
async def tiktok_submit_invalid(message: Message):
    await message.answer(
        "Можно отправить только ссылку текстом или нажать /cancel."
    )


router.message.outer_middleware(GateMiddleware())
router.callback_query.outer_middleware(GateMiddleware())

INACTIVE_AFTER = timedelta(hours=24)
REENGAGE_CHECK_SECONDS = 15 * 60


def mark_reengage_sent(user_id: int) -> None:
    try:
        DB.execute(
            """
            INSERT INTO reengage_log (
                user_id,
                sent
            )
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                sent = excluded.sent
            """,
            (user_id, current_time()),
        )
        DB.commit()
    except Exception:
        DB.rollback()
        raise


def inactive_user_ids() -> list[int]:
    cutoff = (datetime.now(timezone.utc) - INACTIVE_AFTER).isoformat()
    rows = DB.execute(
        """
        SELECT user_id
        FROM users
        WHERE last_seen <= ?
          AND NOT EXISTS (
              SELECT 1
              FROM reengage_log
              WHERE reengage_log.user_id = users.user_id
                AND reengage_log.sent > ?
          )
        """,
        (cutoff, cutoff),
    ).fetchall()

    return [int(row["user_id"]) for row in rows if int(row["user_id"]) > 0]


async def reengage_once(bot: Bot) -> None:
    user_ids = inactive_user_ids()

    if not user_ids:
        return

    sent = 0
    failed = 0

    for user_id in user_ids:
        delivered = False
        permanent = False

        for attempt in range(2):
            try:
                await deliver_template(bot, user_id, "reengage")
                delivered = True
                break
            except Exception as error:
                retry_after = getattr(error, "retry_after", None)

                if retry_after and attempt == 0:
                    await asyncio.sleep(float(retry_after) + 0.1)
                    continue

                permanent = isinstance(
                    error,
                    (TelegramForbiddenError, TelegramBadRequest),
                )
                print(f"Авторассылка {user_id}: {error}")
                break

        if delivered or permanent:
            try:
                mark_reengage_sent(user_id)
            except Exception as error:
                print(f"Не записана авторассылка {user_id}: {error}")

        if delivered:
            sent += 1
        else:
            failed += 1

        await asyncio.sleep(0.05)

    print(
        f"Авторассылка неактивным: отправлено {sent}, "
        f"не доставлено {failed}"
    )


async def reengage_loop(bot: Bot) -> None:
    await asyncio.sleep(20)

    while True:
        try:
            await reengage_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Авторассылка: {error}")

        await asyncio.sleep(REENGAGE_CHECK_SECONDS)


async def main():
    if not TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN."
        )

    init_database()
    print(f"База данных: {DB_FILE}", flush=True)
    sync_all_referral_premium()

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    reengage_task = asyncio.create_task(reengage_loop(bot))

    try:
        await dispatcher.start_polling(bot)
    finally:
        reengage_task.cancel()

        try:
            await reengage_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()
        DB.close()


if __name__ == "__main__":
    asyncio.run(main())
