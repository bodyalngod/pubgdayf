# Зависимость: pip install "aiogram>=3.13,<4"
# Запуск: BOT_TOKEN=123:abc ADMIN_IDS=111,222 python pubg_bot.py

import asyncio
import html
import os
import re
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
DB_FILE = "pubg_bot.sqlite3"

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

router = Router()

BIO_MIN_LENGTH = 20
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


PROFILE_FIELDS = [
    "nickname",
    "gender",
    "age",
    "pubg_id",
    "server",
    "mode",
    "rank",
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
    "voice": "Микрофон",
    "schedule": "Время игры",
    "language": "Язык общения",
    "bio": "О себе",
    "photo_file_id": "Фото",
}


AGE_MIN = 6
AGE_MAX = 66

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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
        counted = add_referral(user.id, referrer_id)
    except Exception as error:
        print(f"Реферал не записан: {error}")
        return

    if not counted:
        return

    bot = data.get("bot") or getattr(event, "bot", None)

    if bot is not None:
        await notify_referrer(bot, referrer_id)


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
)


async def admin_configuration_event(event, data: dict) -> bool:
    """Админ может настраивать бота, даже если сам ещё не подписан."""
    user = getattr(event, "from_user", None)

    if user is None or not is_admin(user.id):
        return False

    if isinstance(event, CallbackQuery):
        payload = event.data or ""
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

        if user_id is None or await admin_configuration_event(event, data):
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and (
            event.data or ""
        ).startswith("sub:"):
            return await handler(event, data)

        bot = data.get("bot") or getattr(event, "bot", None)

        if bot is None:
            return await handler(event, data)

        try:
            missing = await missing_channels(bot, user_id)
        except Exception as error:
            print(f"Ошибка проверки подписки: {error}")
            return await handler(event, data)

        if not missing:
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
        "открыли бота именно по вашей ссылке."
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


def admin_profile_keyboard(
    user_id: int,
    active: bool,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
                    callback_data="admin:profiles:0",
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

def profile_text(profile) -> str:
    gender = profile["gender"] or "Не указан"
    age = profile["age"] or "Не указан"

    return (
        f"🎮 <b>{profile['nickname']}</b>\n\n"
        f"🚻 Пол: {gender}\n"
        f"🎂 Возраст: {age}\n"
        f"🆔 PUBG ID: <code>{profile['pubg_id']}</code>\n"
        f"🌍 Сервер: {profile['server']}\n"
        f"🎯 Режим: {profile['mode']}\n"
        f"🏆 Ранг: {profile['rank']}\n"
        f"🎙 Микрофон: {profile['voice']}\n"
        f"🕒 Время игры: {profile['schedule']}\n"
        f"💬 Язык: {profile['language']}\n\n"
        f"📝 {profile['bio']}"
    )


async def send_profile(
    message: Message,
    profile,
    reply_markup=None,
) -> None:
    text = profile_text(profile)
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

    if field_name == "bio":
        text = (
            "Расскажите о себе.\n"
            f"Минимум {BIO_MIN_LENGTH} символов."
        )

        if old_value:
            text += (
                f"\n\nТекущее значение: {old_value}\n"
                "Если хотите оставить прежнее значение, "
                "используйте соответствующую кнопку."
            )

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
        "voice",
        "schedule",
        "language",
        "bio",
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

    bio = str(values["bio"]).strip()

    if len(bio) < BIO_MIN_LENGTH:
        raise ValueError(
            f"Поле «О себе» должно содержать минимум "
            f"{BIO_MIN_LENGTH} символов."
        )

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
            voice,
            schedule,
            language,
            bio,
            photo_file_id,
            active,
            updated
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            nickname = excluded.nickname,
            gender = excluded.gender,
            age = excluded.age,
            pubg_id = excluded.pubg_id,
            server = excluded.server,
            mode = excluded.mode,
            rank = excluded.rank,
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


@router.message(Command("help"))
async def command_help(message: Message):
    await send_template(
        message,
        "help",
        keyboard_for(message.from_user.id),
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


@router.message(F.text == "Заработай на тик-токах")
async def menu_tiktok(message: Message):
    await send_template(
        message,
        "tiktok",
        keyboard_for(message.from_user.id),
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

    if field_name == "bio":
        data = await state.get_data()
        bio = str(
            data.get("values", {}).get("bio") or ""
        ).strip()

        if len(bio) < BIO_MIN_LENGTH:
            await callback.answer(
                f"В поле «О себе» нужно минимум "
                f"{BIO_MIN_LENGTH} символов.",
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
        await message.answer(
            "Значение не должно быть пустым."
        )
        return

    if field_name == "bio" and len(value) < BIO_MIN_LENGTH:
        await message.answer(
            f"Поле «О себе» должно содержать минимум "
            f"{BIO_MIN_LENGTH} символов."
        )
        return

    data = await state.get_data()
    values = data.get("values", {})
    values[field_name] = value[:2000]

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

    await send_profile(
        message,
        profile,
        profile_actions_keyboard(profile["user_id"]),
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


async def notify_match(
    bot: Bot,
    first_id: int,
    second_id: int,
) -> None:
    first = get_profile(first_id)
    second = get_profile(second_id)

    if not first or not second:
        return

    text_first = (
        "🎉 У вас взаимный лайк!\n\n"
        f"{profile_text(second)}"
    )

    text_second = (
        "🎉 У вас взаимный лайк!\n\n"
        f"{profile_text(first)}"
    )

    for user_id, text, other_id in [
        (first_id, text_first, second_id),
        (second_id, text_second, first_id),
    ]:
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
            await bot.send_message(
                user_id,
                text,
                reply_markup=keyboard,
            )
        except Exception as error:
            print(
                f"Не удалось отправить уведомление "
                f"{user_id}: {error}"
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
        page = int(parts[2])
        limit = 10
        offset = page * limit

        profiles = DB.execute(
            """
            SELECT user_id, nickname, server, mode, active
            FROM profiles
            ORDER BY updated DESC
            LIMIT ? OFFSET ?
            """,
            (
                limit,
                offset,
            ),
        ).fetchall()
        if not profiles:
            if page == 0 and callback.message:
                await callback.answer()
                await edit_or_send(
                    callback.message,
                    "Анкет пока нет.",
                    reply_markup=admin_keyboard(),
                )
            else:
                await callback.answer(
                    "Анкет больше нет.",
                    show_alert=True,
                )
            return

        buttons = []

        for profile in profiles:
            status = "🟢" if profile["active"] else "⚪️"

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=(
                            f"{status} "
                            f"{profile['nickname']} "
                            f"— {profile['user_id']}"
                        )[:64],
                        callback_data=(
                            f"admin:user:{profile['user_id']}"
                        ),
                    )
                ]
            )

        navigation = []

        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=(
                        f"admin:profiles:{page - 1}"
                    ),
                )
            )

        if len(profiles) == limit:
            navigation.append(
                InlineKeyboardButton(
                    text="➡️ Далее",
                    callback_data=(
                        f"admin:profiles:{page + 1}"
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

        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                f"Анкеты — страница {page + 1}:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=buttons,
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
            f"{profile_text(profile)}\n\n"
            f"Telegram ID: <code>{user_id}</code>\n"
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
            )

            # Кнопки всегда на текстовом сообщении.
            # На фото edit_text недоступен, из-за этого
            # «Удалить анкету» ничего не делало.
            photo_shown = False

            if photo:
                try:
                    await callback.message.answer_photo(
                        photo=photo,
                        caption=text if len(text) <= 1024 else None,
                    )
                    photo_shown = True
                except Exception as error:
                    print(f"Фото анкеты {user_id}: {error}")
                    text += "\n\n⚠️ Фото анкеты недоступно."

            show_full_text = (not photo_shown) or len(text) > 1024

            await callback.message.answer(
                text if show_full_text else "Управление анкетой:",
                reply_markup=keyboard,
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
                    )
                )
            except Exception as error:
                print(f"Не удалось обновить кнопки анкеты: {error}")
                await callback.message.answer(
                    "Статус показа изменён.",
                    reply_markup=admin_profile_keyboard(
                        user_id,
                        bool(new_active),
                    ),
                )

        return

    if action == "delete_confirm":
        user_id = int(parts[2])

        if not get_profile(user_id):
            await callback.answer(
                "Анкета не найдена.",
                show_alert=True,
            )
            return

        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                f"Удалить анкету пользователя "
                f"<code>{user_id}</code>?",
                reply_markup=delete_confirmation_keyboard(
                    user_id,
                    admin=True,
                ),
            )

        return

    if action == "delete":
        user_id = int(parts[2])

        if not get_profile(user_id):
            await callback.answer(
                "Анкета уже удалена.",
                show_alert=True,
            )
            return

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
                f"✅ Анкета пользователя "
                f"<code>{user_id}</code> удалена.",
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

        await callback.answer()

        if callback.message:
            await edit_or_send(
                callback.message,
                "📊 <b>Статистика</b>\n\n"
                f"Всего анкет: {total}\n"
                f"Активных анкет: {active}\n"
                f"Открытых жалоб: {reports}",
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
            text += f"\n\n{profile_text(profile)}"

        buttons = [
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
