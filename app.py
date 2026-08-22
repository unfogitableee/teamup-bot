from __future__ import annotations

import asyncio
import hashlib
import html
import hmac
import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "teamup.sqlite3")))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = (
    os.getenv("WEBAPP_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    or "https://example.com"
)
DEV_MODE = os.getenv("DEV_MODE", "1") == "1"
DEMO_DATA = os.getenv("DEMO_DATA", "0") == "1"
REQUIRE_CHANNEL_SUBSCRIPTION = os.getenv("REQUIRE_CHANNEL_SUBSCRIPTION", "0") == "1"
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
AUTOPOST_CHANNEL = os.getenv("AUTOPOST_CHANNEL", "").strip() or REQUIRED_CHANNEL
CHANNEL_USERNAME = AUTOPOST_CHANNEL or REQUIRED_CHANNEL
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
BOT_USERNAME_FALLBACK = os.getenv("BOT_USERNAME", "tteamup_bot").strip().lstrip("@")

router = Router()
dp = Dispatcher()
dp.include_router(router)
bot: Bot | None = Bot(BOT_TOKEN) if BOT_TOKEN else None
polling_task: asyncio.Task | None = None
BOT_USERNAME = BOT_USERNAME_FALLBACK


# ---------------- database ----------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS users (
              telegram_id INTEGER PRIMARY KEY,
              first_name TEXT NOT NULL DEFAULT '',
              username TEXT NOT NULL DEFAULT '',
              role TEXT NOT NULL DEFAULT '',
              level TEXT NOT NULL DEFAULT '',
              bio TEXT NOT NULL DEFAULT '',
              stack TEXT NOT NULL DEFAULT '',
              availability TEXT NOT NULL DEFAULT '',
              mode_free INTEGER NOT NULL DEFAULT 1,
              mode_paid INTEGER NOT NULL DEFAULT 0,
              mode_share INTEGER NOT NULL DEFAULT 0,
              seeking_status TEXT NOT NULL DEFAULT 'open',
              goal TEXT NOT NULL DEFAULT 'both',
              blocked INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              owner_id INTEGER NOT NULL,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              mode TEXT NOT NULL CHECK(mode IN ('free','paid','share')),
              stack TEXT NOT NULL DEFAULT '',
              roles TEXT NOT NULL DEFAULT '',
              terms TEXT NOT NULL DEFAULT '',
              hours TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'open',
              moderation_status TEXT NOT NULL DEFAULT 'active',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS applications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              message TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','accepted','declined')),
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL DEFAULT 0,
              UNIQUE(project_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS invitations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              sender_id INTEGER NOT NULL,
              recipient_id INTEGER NOT NULL,
              message TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','accepted','declined')),
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              UNIQUE(project_id, recipient_id)
            );

            CREATE TABLE IF NOT EXISTS reports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              reporter_id INTEGER NOT NULL,
              target_type TEXT NOT NULL CHECK(target_type IN ('project','user')),
              target_id INTEGER NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','resolved','dismissed')),
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              UNIQUE(reporter_id, target_type, target_id)
            );

            CREATE TABLE IF NOT EXISTS broadcasts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              admin_id INTEGER NOT NULL,
              text TEXT NOT NULL,
              target_count INTEGER NOT NULL DEFAULT 0,
              sent_count INTEGER NOT NULL DEFAULT 0,
              blocked_count INTEGER NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'running'
                CHECK(status IN ('running','done','failed')),
              created_at INTEGER NOT NULL,
              finished_at INTEGER NOT NULL DEFAULT 0
            );
            """
        )

        # Migrations from older MVPs.
        user_cols = _columns(conn, "users")
        if "seeking_status" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN seeking_status TEXT NOT NULL DEFAULT 'open'")
        if "blocked" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        if "goal" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN goal TEXT NOT NULL DEFAULT 'both'")

        project_cols = _columns(conn, "projects")
        if "status" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
        if "moderation_status" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN moderation_status TEXT NOT NULL DEFAULT 'active'")
        if "updated_at" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE projects SET updated_at=created_at WHERE updated_at=0")

        app_cols = _columns(conn, "applications")
        if "updated_at" not in app_cols:
            conn.execute("ALTER TABLE applications ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE applications SET updated_at=created_at WHERE updated_at=0")

        if DEMO_DATA:
            count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
            if count == 0:
                now = int(time.time())
                demo_users = [
                    (900001, "Артём", "artem_dev", "Frontend Developer", "Junior+", "Хочу реальный командный проект для портфолио.", "React,TypeScript,Next.js", "5–10 ч/нед", 1, 0, 1, "open", now, now),
                    (900002, "Лера", "lera_design", "UI/UX Designer", "Middle", "Делаю интерфейсы для web и mobile.", "Figma,UI/UX,Mobile", "до 10 ч/нед", 1, 1, 1, "open", now, now),
                    (900003, "Миша", "misha_py", "Backend Developer", "Middle", "Python backend, API и базы данных.", "Python,FastAPI,PostgreSQL,Docker", "вечера", 0, 1, 1, "open", now, now),
                ]
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO users
                    (telegram_id,first_name,username,role,level,bio,stack,availability,
                     mode_free,mode_paid,mode_share,seeking_status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    demo_users,
                )
                demo_projects = [
                    (900003, "AI Seller Assistant", "Сервис аналитики для продавцов маркетплейсов. Backend уже начали.", "share", "Python,React,FastAPI", "Frontend,Sales", "10% доля / условия обсуждаются", "5–8 ч/нед", "open", now, now),
                    (900001, "Habit Tracker", "Pet-project для портфолио. Хотим довести до релиза.", "free", "React,TypeScript,Figma", "Designer,Backend", "Без оплаты, общий кейс в портфолио", "4–6 ч/нед", "open", now, now),
                    (900002, "Telegram CRM", "Нужен Python-разработчик на небольшой MVP.", "paid", "Python,Telegram,PostgreSQL", "Backend Developer", "40–60 тыс. ₽", "2–3 недели", "open", now, now),
                ]
                conn.executemany(
                    """
                    INSERT INTO projects
                    (owner_id,title,description,mode,stack,roles,terms,hours,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    demo_projects,
                )


def ensure_user(user: dict) -> None:
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id,first_name,username,created_at,updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              first_name=excluded.first_name,
              username=excluded.username,
              updated_at=excluded.updated_at
            """,
            (
                int(user["id"]),
                user.get("first_name", "") or "",
                user.get("username", "") or "",
                now,
                now,
            ),
        )


# ---------------- Telegram auth ----------------

def validate_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN is not configured")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Telegram hash is missing")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(401, "Invalid Telegram signature")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if not auth_date or abs(int(time.time()) - auth_date) > 24 * 60 * 60:
        raise HTTPException(401, "Telegram initData is too old")

    try:
        user = json.loads(pairs["user"])
    except (KeyError, json.JSONDecodeError):
        raise HTTPException(401, "Telegram user is missing")

    ensure_user(user)
    return user


def _check_user_access(user: dict) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT blocked FROM users WHERE telegram_id=?",
            (int(user["id"]),),
        ).fetchone()
    if row and int(row["blocked"]):
        raise HTTPException(403, "Аккаунт заблокирован администратором Тимап")
    return user


def current_user(
    x_telegram_init_data: str = Header(default=""),
    x_dev_user: str = Header(default=""),
) -> dict:
    if x_telegram_init_data:
        return _check_user_access(validate_init_data(x_telegram_init_data))

    if DEV_MODE:
        dev_id = int(x_dev_user or "777001")
        user = {"id": dev_id, "first_name": "Dev User", "username": "local_dev"}
        ensure_user(user)
        return _check_user_access(user)

    raise HTTPException(401, "Open this Mini App inside Telegram")


# ---------------- schemas ----------------

Mode = Literal["free", "paid", "share"]
DecisionStatus = Literal["accepted", "declined"]
ProjectStatus = Literal["open", "closed"]
SeekingStatus = Literal["open", "busy"]
Goal = Literal["project", "people", "both"]
ModerationStatus = Literal["active", "removed"]
ReportTarget = Literal["project", "user"]
ReportStatus = Literal["resolved", "dismissed"]


class ProfileIn(BaseModel):
    role: str = Field(max_length=80)
    level: str = Field(max_length=40)
    bio: str = Field(max_length=500)
    stack: list[str] = Field(default_factory=list, max_length=20)
    availability: str = Field(max_length=80)
    mode_free: bool = True
    mode_paid: bool = False
    mode_share: bool = False
    seeking_status: SeekingStatus = "open"
    goal: Goal = "both"


class ProjectIn(BaseModel):
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(max_length=1000)
    mode: Mode
    stack: list[str] = Field(default_factory=list, max_length=20)
    roles: str = Field(max_length=180)
    terms: str = Field(max_length=180)
    hours: str = Field(max_length=80)


class ApplyIn(BaseModel):
    message: str = Field(default="", max_length=500)


class DecisionIn(BaseModel):
    status: DecisionStatus


class InviteIn(BaseModel):
    project_id: int
    recipient_id: int
    message: str = Field(default="", max_length=500)


class ReportIn(BaseModel):
    target_type: ReportTarget
    target_id: int
    reason: str = Field(min_length=3, max_length=500)


class BroadcastIn(BaseModel):
    text: str = Field(min_length=3, max_length=3500)
    test_only: bool = False


# ---------------- bot ----------------

def _channel_url() -> str | None:
    channel = CHANNEL_USERNAME
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"
    return None


def app_keyboard(project_id: int | None = None) -> InlineKeyboardMarkup:
    url = WEBAPP_URL
    if project_id:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}project={project_id}"

    rows = [
        [InlineKeyboardButton(text="🚀 Открыть Тимап", web_app=WebAppInfo(url=url))]
    ]
    channel_url = _channel_url()
    if channel_url:
        rows.append([InlineKeyboardButton(text="📣 Канал Тимап", url=channel_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    first_name = "друг"
    if message.from_user:
        first_name = message.from_user.first_name or first_name
        ensure_user(
            {
                "id": message.from_user.id,
                "first_name": message.from_user.first_name,
                "username": message.from_user.username or "",
            }
        )

    safe_name = html.escape(first_name)
    await message.answer(
        f"👋 <b>Привет, {safe_name}! Это Тимап.</b>\n\n"
        "Место, где разработчики, дизайнеры, маркетологи и другие специалисты "
        "находят друг друга и собирают команды.\n\n"
        "<b>Что здесь можно:</b>\n"
        "🚀 найти проект под свой стек\n"
        "👥 найти человека в команду\n"
        "📨 откликаться и приглашать участников\n"
        "✨ получать подборки с лучшим совпадением\n\n"
        "<b>Форматы:</b>\n"
        "🟢 бесплатно — опыт и pet-project\n"
        "💼 оплата — работа за деньги\n"
        "💎 доля — equity / revenue share\n\n"
        "👇 <b>Открой Тимап.</b> При первом входе — быстрая регистрация из 3 пунктов, дальше сразу в проекты.",
        reply_markup=app_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("app"))
async def app_handler(message: Message) -> None:
    await message.answer(
        "🚀 <b>Тимап готов.</b> Открывай Mini App:",
        reply_markup=app_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("myid"))
async def myid_handler(message: Message) -> None:
    if not message.from_user:
        return
    await message.answer(
        f"Твой Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "Он нужен, если хочешь включить себе админку через ADMIN_IDS.",
        parse_mode="HTML",
    )


@router.message(Command("channelcheck"))
async def channelcheck_handler(message: Message) -> None:
    if not message.from_user:
        return

    if not CHANNEL_USERNAME:
        await message.answer(
            "⚠️ Канал ещё не указан.\n\n"
            "Добавь в <code>.env</code> строку:\n"
            "<code>AUTOPOST_CHANNEL=@username_канала</code>\n\n"
            "и перезапусти Uvicorn.",
            parse_mode="HTML",
        )
        return

    if not bot:
        await message.answer("❌ Бот сейчас не запущен.")
        return

    try:
        chat = await bot.get_chat(CHANNEL_USERNAME)
        member = await bot.get_chat_member(CHANNEL_USERNAME, message.from_user.id)
        raw_status = getattr(member, "status", "")
        status = getattr(raw_status, "value", str(raw_status))
        status = str(status).lower().split(".")[-1]

        subscribed = status in {"member", "administrator", "creator", "owner"}
        if status == "restricted":
            subscribed = bool(getattr(member, "is_member", False))

        result = "✅ подписка определяется" if subscribed else "⚠️ ты сейчас не подписан"
        await message.answer(
            "📣 <b>Проверка канала Тимап</b>\n\n"
            f"Канал: <b>{html.escape(getattr(chat, 'title', CHANNEL_USERNAME))}</b>\n"
            f"ID/username: <code>{html.escape(CHANNEL_USERNAME)}</code>\n"
            f"Твой статус: <code>{html.escape(status)}</code>\n"
            f"Результат: <b>{result}</b>\n\n"
            "Если команда видит канал, но подписка других пользователей не определяется — "
            "проверь, что <b>@tteamup_bot добавлен администратором канала</b>.",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.answer(
            "❌ <b>Не удалось проверить канал.</b>\n\n"
            f"<code>{html.escape(str(exc)[:500])}</code>\n\n"
            "Проверь username канала и добавь @tteamup_bot администратором.",
            parse_mode="HTML",
        )


async def configure_bot() -> None:
    global BOT_USERNAME
    if not bot:
        return

    me = await bot.get_me()
    BOT_USERNAME = me.username or BOT_USERNAME_FALLBACK

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главная"),
            BotCommand(command="app", description="Открыть Тимап"),
            BotCommand(command="myid", description="Показать мой Telegram ID"),
            BotCommand(command="channelcheck", description="Проверить канал и подписку"),
        ]
    )
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Тимап", web_app=WebAppInfo(url=WEBAPP_URL))
    )


async def notify_user(user_id: int, text: str, project_id: int | None = None) -> None:
    if not bot:
        return
    try:
        await bot.send_message(user_id, text, reply_markup=app_keyboard(project_id))
    except Exception:
        pass


def project_deep_link(project_id: int) -> str:
    username = BOT_USERNAME or BOT_USERNAME_FALLBACK
    return f"https://t.me/{username}?startapp=project_{project_id}"


async def publish_project_to_channel(project_id: int) -> None:
    if not bot or not AUTOPOST_CHANNEL:
        return

    with db() as conn:
        project = project_row(conn, project_id)
    if not project:
        return

    mode_text = {
        "free": "🟢 Бесплатно",
        "paid": "💼 Оплата",
        "share": "💎 Доля / revenue share",
    }.get(project["mode"], project["mode"])

    text = (
        "🚀 <b>Новый проект в Тимап</b>\n\n"
        f"<b>{html.escape(project['title'])}</b>\n"
        f"{html.escape(project['description'][:500])}\n\n"
        f"{mode_text}\n"
        f"👥 Ищут: <b>{html.escape(project['roles'] or 'участников')}</b>\n"
        f"🧩 Стек: <b>{html.escape(project['stack'] or 'не указан')}</b>\n"
        f"⏱ Занятость: <b>{html.escape(project['hours'] or 'обсудим')}</b>\n"
        f"🤝 Условия: <b>{html.escape(project['terms'] or 'обсудим')}</b>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Открыть проект →",
                url=project_deep_link(project_id),
            )
        ]]
    )
    try:
        await bot.send_message(
            AUTOPOST_CHANNEL,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as exc:
        print(f"[channel autopost] {exc}")


broadcast_lock = asyncio.Lock()


async def send_broadcast(*, admin_id: int, text: str, test_only: bool = False) -> dict:
    if not bot:
        raise HTTPException(503, "Бот сейчас не подключён")

    clean_text = text.strip()
    if len(clean_text) < 3:
        raise HTTPException(400, "Сообщение слишком короткое")

    if broadcast_lock.locked():
        raise HTTPException(409, "Другая рассылка уже выполняется")

    async with broadcast_lock:
        with db() as conn:
            if test_only:
                targets = [admin_id]
            else:
                targets = [
                    int(row["telegram_id"])
                    for row in conn.execute(
                        "SELECT telegram_id FROM users WHERE blocked=0 ORDER BY created_at ASC"
                    ).fetchall()
                ]

            now = int(time.time())
            cur = conn.execute(
                """
                INSERT INTO broadcasts
                  (admin_id,text,target_count,sent_count,blocked_count,error_count,status,created_at,finished_at)
                VALUES (?,?,?,?,?,?, 'running', ?, 0)
                """,
                (admin_id, clean_text, len(targets), 0, 0, 0, now),
            )
            broadcast_id = int(cur.lastrowid)

        sent = 0
        blocked = 0
        errors = 0
        keyboard = app_keyboard()

        for user_id in targets:
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=clean_text,
                    reply_markup=keyboard,
                )
                sent += 1

            except TelegramForbiddenError:
                blocked += 1

            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.25)
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=clean_text,
                        reply_markup=keyboard,
                    )
                    sent += 1
                except TelegramForbiddenError:
                    blocked += 1
                except Exception:
                    errors += 1

            except TelegramBadRequest:
                errors += 1

            except Exception as exc:
                print(f"[broadcast] user={user_id}: {exc}")
                errors += 1

            # Около 10 сообщений в секунду — спокойно для такой небольшой базы.
            await asyncio.sleep(0.09)

        finished = int(time.time())
        with db() as conn:
            conn.execute(
                """
                UPDATE broadcasts
                SET sent_count=?,blocked_count=?,error_count=?,status='done',finished_at=?
                WHERE id=?
                """,
                (sent, blocked, errors, finished, broadcast_id),
            )

        return {
            "ok": True,
            "id": broadcast_id,
            "target_count": len(targets),
            "sent": sent,
            "blocked": blocked,
            "errors": errors,
            "test_only": test_only,
        }


# ---------------- lifecycle ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global polling_task
    init_db()

    if bot:
        await configure_bot()
        polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))

    yield

    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

    if bot:
        await bot.session.close()


app = FastAPI(title="Тимап Mini App", lifespan=lifespan)


# ---------------- helpers ----------------

def project_row(conn: sqlite3.Connection, project_id: int):
    return conn.execute(
        """
        SELECT p.*, u.first_name AS owner_name, u.username AS owner_username,
          (SELECT COUNT(*) FROM applications a WHERE a.project_id=p.id AND a.status='pending') AS pending_applications
        FROM projects p
        JOIN users u ON u.telegram_id=p.owner_id
        WHERE p.id=? AND p.moderation_status='active' AND u.blocked=0
        """,
        (project_id,),
    ).fetchone()


def require_owned_project(conn: sqlite3.Connection, project_id: int, owner_id: int):
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Проект не найден")
    if int(row["owner_id"]) != owner_id:
        raise HTTPException(403, "Это не твой проект")
    return row

def require_admin(user: dict) -> int:
    uid = int(user["id"])
    if uid not in ADMIN_IDS:
        raise HTTPException(403, "Админка доступна только владельцу")
    return uid


def _stack_tokens(value: str) -> set[str]:
    return {
        token.strip().lower()
        for token in (value or "").replace(";", ",").split(",")
        if token.strip()
    }


def _mode_allowed(user_row: sqlite3.Row | dict, mode: str) -> bool:
    key = {"free": "mode_free", "paid": "mode_paid", "share": "mode_share"}[mode]
    return bool(user_row[key])


def _words(value: str) -> set[str]:
    cleaned = re.sub(r"[^a-zA-Zа-яА-Я0-9+#.]+", " ", (value or "").lower())
    return {part for part in cleaned.split() if len(part) > 1}


def project_match(user_row: sqlite3.Row | dict, project: sqlite3.Row | dict) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    user_stack = _stack_tokens(user_row["stack"])
    project_stack = _stack_tokens(project["stack"])
    overlap = sorted(user_stack & project_stack)

    if overlap:
        score += min(60, 30 + 12 * (len(overlap) - 1))
        reasons.append("совпали " + ", ".join(overlap[:3]))

    if _mode_allowed(user_row, project["mode"]):
        score += 25
        reasons.append("подходит формат")

    role_words = _words(user_row["role"])
    needed_words = _words(project["roles"])
    if role_words & needed_words:
        score += 10
        reasons.append("нужна твоя роль")

    if user_row["availability"] and project["hours"]:
        score += 5

    return min(100, score), reasons[:3]


def people_match(user_row: sqlite3.Row | dict, person: sqlite3.Row | dict) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    my_stack = _stack_tokens(user_row["stack"])
    their_stack = _stack_tokens(person["stack"])
    overlap = sorted(my_stack & their_stack)

    if overlap:
        score += min(55, 28 + 10 * (len(overlap) - 1))
        reasons.append("общий стек: " + ", ".join(overlap[:3]))

    common_modes = []
    for mode, label in [("free", "бесплатно"), ("paid", "оплата"), ("share", "доля")]:
        if _mode_allowed(user_row, mode) and _mode_allowed(person, mode):
            common_modes.append(label)
    if common_modes:
        score += min(30, 15 + 5 * (len(common_modes) - 1))
        reasons.append("общий формат: " + ", ".join(common_modes[:2]))

    if user_row["role"] and person["role"] and user_row["role"].lower() != person["role"].lower():
        score += 10
        reasons.append("разные роли в команду")

    if user_row["availability"] and person["availability"]:
        score += 5

    return min(100, score), reasons[:3]


def current_profile(conn: sqlite3.Connection, uid: int):
    return conn.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()


def require_action_profile(conn: sqlite3.Connection, uid: int):
    row = current_profile(conn, uid)
    if not row or not (row["role"] or "").strip() or not (row["stack"] or "").strip():
        raise HTTPException(
            428,
            "Чтобы сделать это действие, укажи роль и стек — это займёт около 15 секунд.",
        )
    return row



# ---------------- API ----------------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "brand": "Тимап",
        "bot_configured": bool(BOT_TOKEN),
        "dev_mode": DEV_MODE,
        "demo_data": DEMO_DATA,
        "required_channel": REQUIRED_CHANNEL or None,
        "autopost_channel": AUTOPOST_CHANNEL or None,
        "channel_username": CHANNEL_USERNAME or None,
        "subscription_gate": REQUIRE_CHANNEL_SUBSCRIPTION,
        "channel_username": CHANNEL_USERNAME or None,
        "subscription_gate": REQUIRE_CHANNEL_SUBSCRIPTION,
        "progressive_onboarding": False,
        "first_visit_registration": True,
        "admins_configured": len(ADMIN_IDS),
    }


@app.get("/api/config")
def config():
    return {
        "brand": "Тимап",
        "bot_username": BOT_USERNAME or BOT_USERNAME_FALLBACK,
        "required_channel": REQUIRED_CHANNEL or None,
        "autopost_channel": AUTOPOST_CHANNEL or None,
    }


@app.get("/api/access")
async def access(user: dict = Depends(current_user)):
    # v0.7: channel subscription is optional by default.
    # A hard gate exists only when explicitly enabled with REQUIRE_CHANNEL_SUBSCRIPTION=1.
    if not REQUIRE_CHANNEL_SUBSCRIPTION or not CHANNEL_USERNAME:
        return {
            "required": False,
            "subscribed": True,
            "channel": CHANNEL_USERNAME or None,
        }

    if DEV_MODE:
        return {"required": True, "subscribed": True, "channel": CHANNEL_USERNAME}

    if not bot:
        raise HTTPException(500, "Bot is not configured")

    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=int(user["id"]))
        raw_status = getattr(member, "status", "")
        status = getattr(raw_status, "value", str(raw_status))
        status = str(status).lower().split(".")[-1]
        subscribed = status in {"member", "administrator", "creator", "owner"}
        if status == "restricted":
            subscribed = bool(getattr(member, "is_member", False))
        return {
            "required": True,
            "subscribed": subscribed,
            "channel": CHANNEL_USERNAME,
            "status": status,
        }
    except Exception as exc:
        return {
            "required": True,
            "subscribed": False,
            "channel": CHANNEL_USERNAME,
            "status": "error",
            "error": str(exc) if DEV_MODE else None,
        }


@app.get("/api/me")
def get_me(user: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (int(user["id"]),)).fetchone()
    result = dict(row)
    result["is_admin"] = int(user["id"]) in ADMIN_IDS
    return result


@app.post("/api/profile")
def save_profile(data: ProfileIn, user: dict = Depends(current_user)):
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """
            UPDATE users SET
              role=?, level=?, bio=?, stack=?, availability=?,
              mode_free=?, mode_paid=?, mode_share=?, seeking_status=?, goal=?, updated_at=?
            WHERE telegram_id=?
            """,
            (
                data.role.strip(),
                data.level.strip(),
                data.bio.strip(),
                ",".join(x.strip() for x in data.stack if x.strip()),
                data.availability.strip(),
                int(data.mode_free),
                int(data.mode_paid),
                int(data.mode_share),
                data.seeking_status,
                data.goal,
                now,
                int(user["id"]),
            ),
        )
    return {"ok": True}


@app.get("/api/projects")
def list_projects(
    mode: Mode | None = Query(default=None),
    stack: str = Query(default=""),
    mine: bool = Query(default=False),
    include_closed: bool = Query(default=False),
    recommended: bool = Query(default=False),
    user: dict = Depends(current_user),
):
    sql = """
      SELECT p.*, u.first_name AS owner_name, u.username AS owner_username,
        (SELECT COUNT(*) FROM applications a WHERE a.project_id=p.id AND a.status='pending') AS pending_applications
      FROM projects p
      JOIN users u ON u.telegram_id=p.owner_id
      WHERE p.moderation_status='active' AND u.blocked=0
    """
    args: list[object] = []

    if mine:
        sql += " AND p.owner_id=?"
        args.append(int(user["id"]))
    elif not include_closed:
        sql += " AND p.status='open'"

    if mine and not include_closed:
        sql += " AND p.status='open'"

    if mode:
        sql += " AND p.mode=?"
        args.append(mode)

    if stack.strip():
        sql += " AND lower(p.stack) LIKE ?"
        args.append(f"%{stack.strip().lower()}%")

    sql += " ORDER BY p.status='open' DESC, p.updated_at DESC, p.id DESC LIMIT 100"

    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
        profile = current_profile(conn, int(user["id"]))

    result = [dict(row) for row in rows]
    if recommended and not mine and profile:
        for item in result:
            item["match_score"], item["match_reasons"] = project_match(profile, item)
        result.sort(key=lambda item: (item.get("match_score", 0), item.get("updated_at", 0)), reverse=True)
    return result


@app.get("/api/projects/{project_id}")
def project_detail(project_id: int, user: dict = Depends(current_user)):
    with db() as conn:
        row = project_row(conn, project_id)
        if not row:
            raise HTTPException(404, "Проект не найден")

        membership = conn.execute(
            """
            SELECT status FROM applications WHERE project_id=? AND user_id=?
            """,
            (project_id, int(user["id"])),
        ).fetchone()

    result = dict(row)
    result["my_application_status"] = membership["status"] if membership else None
    result["is_owner"] = int(row["owner_id"]) == int(user["id"])
    return result


@app.post("/api/projects")
async def create_project(data: ProjectIn, user: dict = Depends(current_user)):
    now = int(time.time())
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects
              (owner_id,title,description,mode,stack,roles,terms,hours,status,moderation_status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?, 'open', 'active', ?, ?)
            """,
            (
                int(user["id"]),
                data.title.strip(),
                data.description.strip(),
                data.mode,
                ",".join(x.strip() for x in data.stack if x.strip()),
                data.roles.strip(),
                data.terms.strip(),
                data.hours.strip(),
                now,
                now,
            ),
        )
        project_id = cur.lastrowid

    await publish_project_to_channel(project_id)
    return {"ok": True, "id": project_id}


@app.put("/api/projects/{project_id}")
def update_project(project_id: int, data: ProjectIn, user: dict = Depends(current_user)):
    now = int(time.time())
    with db() as conn:
        require_owned_project(conn, project_id, int(user["id"]))
        conn.execute(
            """
            UPDATE projects SET
              title=?, description=?, mode=?, stack=?, roles=?, terms=?, hours=?, updated_at=?
            WHERE id=?
            """,
            (
                data.title.strip(),
                data.description.strip(),
                data.mode,
                ",".join(x.strip() for x in data.stack if x.strip()),
                data.roles.strip(),
                data.terms.strip(),
                data.hours.strip(),
                now,
                project_id,
            ),
        )
    return {"ok": True}


@app.post("/api/projects/{project_id}/status")
def set_project_status(
    project_id: int,
    status: ProjectStatus,
    user: dict = Depends(current_user),
):
    with db() as conn:
        require_owned_project(conn, project_id, int(user["id"]))
        conn.execute(
            "UPDATE projects SET status=?, updated_at=? WHERE id=?",
            (status, int(time.time()), project_id),
        )
    return {"ok": True, "status": status}


@app.get("/api/people")
def list_people(
    mode: Mode | None = Query(default=None),
    stack: str = Query(default=""),
    recommended: bool = Query(default=False),
    user: dict = Depends(current_user),
):
    sql = "SELECT * FROM users WHERE telegram_id != ? AND seeking_status='open' AND blocked=0 AND trim(role)<>'' AND trim(stack)<>''"
    args: list[object] = [int(user["id"])]

    if mode == "free":
        sql += " AND mode_free=1"
    elif mode == "paid":
        sql += " AND mode_paid=1"
    elif mode == "share":
        sql += " AND mode_share=1"

    if stack.strip():
        sql += " AND lower(stack) LIKE ?"
        args.append(f"%{stack.strip().lower()}%")

    sql += " ORDER BY updated_at DESC LIMIT 100"

    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
        profile = current_profile(conn, int(user["id"]))

    result = [dict(row) for row in rows]
    if recommended and profile:
        for item in result:
            item["match_score"], item["match_reasons"] = people_match(profile, item)
        result.sort(key=lambda item: (item.get("match_score", 0), item.get("updated_at", 0)), reverse=True)
    return result


@app.post("/api/projects/{project_id}/apply")
async def apply_to_project(project_id: int, data: ApplyIn, user: dict = Depends(current_user)):
    now = int(time.time())
    with db() as conn:
        require_action_profile(conn, int(user["id"]))
        project = conn.execute("SELECT owner_id,title,status,moderation_status FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(404, "Проект не найден")
        if project["moderation_status"] != "active":
            raise HTTPException(409, "Проект снят с публикации")
        if project["status"] != "open":
            raise HTTPException(409, "Проект уже закрыт")
        if int(project["owner_id"]) == int(user["id"]):
            raise HTTPException(400, "Нельзя откликнуться на свой проект")

        applicant = conn.execute(
            "SELECT first_name,role FROM users WHERE telegram_id=?",
            (int(user["id"]),),
        ).fetchone()

        try:
            conn.execute(
                """
                INSERT INTO applications(project_id,user_id,message,status,created_at,updated_at)
                VALUES (?,?,?,'pending',?,?)
                """,
                (project_id, int(user["id"]), data.message.strip(), now, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Ты уже откликался на этот проект")

    applicant_name = applicant["first_name"] if applicant else "Новый участник"
    applicant_role = applicant["role"] if applicant else ""
    await notify_user(
        int(project["owner_id"]),
        f"📨 Новый отклик на «{project['title']}»\n\n{applicant_name}"
        + (f" · {applicant_role}" if applicant_role else "")
        + "\n\nОткрой Тимап → Отклики.",
        project_id,
    )
    return {"ok": True}


@app.post("/api/applications/{application_id}/decision")
async def application_decision(
    application_id: int,
    data: DecisionIn,
    user: dict = Depends(current_user),
):
    owner_id = int(user["id"])
    with db() as conn:
        row = conn.execute(
            """
            SELECT a.id,a.user_id,a.status,p.title,p.owner_id,p.id AS project_id
            FROM applications a
            JOIN projects p ON p.id=a.project_id
            WHERE a.id=?
            """,
            (application_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Отклик не найден")
        if int(row["owner_id"]) != owner_id:
            raise HTTPException(403, "Это не твой проект")
        if row["status"] != "pending":
            raise HTTPException(409, "По этому отклику решение уже принято")

        conn.execute(
            "UPDATE applications SET status=?,updated_at=? WHERE id=?",
            (data.status, int(time.time()), application_id),
        )

    if data.status == "accepted":
        text = f"✅ Твой отклик на «{row['title']}» приняли!\n\nОткрой Тимап → Отклики — там будет контакт автора."
    else:
        text = f"По отклику на «{row['title']}» пока не совпали. Можно найти другие проекты в Тимап."

    await notify_user(int(row["user_id"]), text, int(row["project_id"]))
    return {"ok": True, "status": data.status}


@app.post("/api/invitations")
async def create_invitation(data: InviteIn, user: dict = Depends(current_user)):
    sender_id = int(user["id"])
    now = int(time.time())

    with db() as conn:
        require_action_profile(conn, sender_id)
        project = require_owned_project(conn, data.project_id, sender_id)
        if project["status"] != "open":
            raise HTTPException(409, "Проект закрыт")
        if data.recipient_id == sender_id:
            raise HTTPException(400, "Нельзя пригласить самого себя")

        recipient = conn.execute(
            "SELECT first_name,role FROM users WHERE telegram_id=?",
            (data.recipient_id,),
        ).fetchone()
        if not recipient:
            raise HTTPException(404, "Пользователь не найден")

        try:
            conn.execute(
                """
                INSERT INTO invitations
                  (project_id,sender_id,recipient_id,message,status,created_at,updated_at)
                VALUES (?,?,?,?,'pending',?,?)
                """,
                (data.project_id, sender_id, data.recipient_id, data.message.strip(), now, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Ты уже приглашал этого человека в проект")

    await notify_user(
        data.recipient_id,
        f"🚀 Тебя приглашают в проект «{project['title']}»\n\n"
        + (data.message.strip() or "Автор проекта считает, что твой стек подходит.")
        + "\n\nОткрой Тимап → Отклики.",
        data.project_id,
    )
    return {"ok": True}


@app.post("/api/invitations/{invitation_id}/decision")
async def invitation_decision(
    invitation_id: int,
    data: DecisionIn,
    user: dict = Depends(current_user),
):
    recipient_id = int(user["id"])

    with db() as conn:
        row = conn.execute(
            """
            SELECT i.*,p.title,p.id AS project_id,u.first_name AS recipient_name
            FROM invitations i
            JOIN projects p ON p.id=i.project_id
            JOIN users u ON u.telegram_id=i.recipient_id
            WHERE i.id=?
            """,
            (invitation_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Приглашение не найдено")
        if int(row["recipient_id"]) != recipient_id:
            raise HTTPException(403, "Это приглашение не тебе")
        if row["status"] != "pending":
            raise HTTPException(409, "По приглашению уже принято решение")

        conn.execute(
            "UPDATE invitations SET status=?,updated_at=? WHERE id=?",
            (data.status, int(time.time()), invitation_id),
        )

    if data.status == "accepted":
        text = f"🤝 {row['recipient_name']} принял приглашение в «{row['title']}». Открой Тимап → Отклики, чтобы написать ему."
    else:
        text = f"Приглашение в «{row['title']}» отклонили."

    await notify_user(int(row["sender_id"]), text, int(row["project_id"]))
    return {"ok": True, "status": data.status}


@app.get("/api/inbox")
def inbox(user: dict = Depends(current_user)):
    uid = int(user["id"])
    with db() as conn:
        incoming_apps = conn.execute(
            """
            SELECT a.*,p.title AS project_title,u.first_name,u.username,u.role,u.level,u.stack,u.availability
            FROM applications a
            JOIN projects p ON p.id=a.project_id
            JOIN users u ON u.telegram_id=a.user_id
            WHERE p.owner_id=?
            ORDER BY CASE a.status WHEN 'pending' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END, a.updated_at DESC, a.id DESC
            """,
            (uid,),
        ).fetchall()

        outgoing_apps = conn.execute(
            """
            SELECT a.*,p.title AS project_title,p.mode,p.id AS project_id,
                   u.first_name AS owner_name,u.username AS owner_username
            FROM applications a
            JOIN projects p ON p.id=a.project_id
            JOIN users u ON u.telegram_id=p.owner_id
            WHERE a.user_id=?
            ORDER BY a.updated_at DESC,a.id DESC
            """,
            (uid,),
        ).fetchall()

        incoming_invites = conn.execute(
            """
            SELECT i.*,p.title AS project_title,p.mode,p.stack,p.roles,p.terms,p.hours,p.id AS project_id,
                   u.first_name AS sender_name,u.username AS sender_username
            FROM invitations i
            JOIN projects p ON p.id=i.project_id
            JOIN users u ON u.telegram_id=i.sender_id
            WHERE i.recipient_id=?
            ORDER BY CASE i.status WHEN 'pending' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END, i.updated_at DESC,i.id DESC
            """,
            (uid,),
        ).fetchall()

        outgoing_invites = conn.execute(
            """
            SELECT i.*,p.title AS project_title,p.id AS project_id,
                   u.first_name AS recipient_name,u.username AS recipient_username,u.role,u.stack
            FROM invitations i
            JOIN projects p ON p.id=i.project_id
            JOIN users u ON u.telegram_id=i.recipient_id
            WHERE i.sender_id=?
            ORDER BY i.updated_at DESC,i.id DESC
            """,
            (uid,),
        ).fetchall()

    return {
        "incoming_applications": [dict(r) for r in incoming_apps],
        "outgoing_applications": [dict(r) for r in outgoing_apps],
        "incoming_invitations": [dict(r) for r in incoming_invites],
        "outgoing_invitations": [dict(r) for r in outgoing_invites],
    }


@app.post("/api/reports")
def create_report(data: ReportIn, user: dict = Depends(current_user)):
    uid = int(user["id"])
    reason = data.reason.strip()
    now = int(time.time())

    with db() as conn:
        if data.target_type == "project":
            target = conn.execute(
                "SELECT id,owner_id FROM projects WHERE id=?",
                (data.target_id,),
            ).fetchone()
            if not target:
                raise HTTPException(404, "Проект не найден")
            if int(target["owner_id"]) == uid:
                raise HTTPException(400, "Нельзя пожаловаться на свой проект")
        else:
            target = conn.execute(
                "SELECT telegram_id FROM users WHERE telegram_id=?",
                (data.target_id,),
            ).fetchone()
            if not target:
                raise HTTPException(404, "Пользователь не найден")
            if int(target["telegram_id"]) == uid:
                raise HTTPException(400, "Нельзя пожаловаться на самого себя")

        existing = conn.execute(
            """
            SELECT id,status FROM reports
            WHERE reporter_id=? AND target_type=? AND target_id=?
            """,
            (uid, data.target_type, data.target_id),
        ).fetchone()

        if existing:
            if existing["status"] == "pending":
                raise HTTPException(409, "Жалоба уже отправлена")
            conn.execute(
                """
                UPDATE reports
                SET reason=?,status='pending',updated_at=?
                WHERE id=?
                """,
                (reason, now, existing["id"]),
            )
            report_id = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO reports
                  (reporter_id,target_type,target_id,reason,status,created_at,updated_at)
                VALUES (?,?,?,?, 'pending', ?, ?)
                """,
                (uid, data.target_type, data.target_id, reason, now, now),
            )
            report_id = cur.lastrowid

    return {"ok": True, "id": report_id}


@app.get("/api/admin/stats")
def admin_stats(user: dict = Depends(current_user)):
    require_admin(user)
    since = int(time.time()) - 24 * 60 * 60
    with db() as conn:
        return {
            "users": conn.execute("SELECT COUNT(*) c FROM users WHERE blocked=0").fetchone()["c"],
            "new_users_24h": conn.execute("SELECT COUNT(*) c FROM users WHERE created_at>=?", (since,)).fetchone()["c"],
            "projects": conn.execute("SELECT COUNT(*) c FROM projects WHERE moderation_status='active'").fetchone()["c"],
            "open_projects": conn.execute("SELECT COUNT(*) c FROM projects WHERE moderation_status='active' AND status='open'").fetchone()["c"],
            "applications": conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"],
            "pending_applications": conn.execute("SELECT COUNT(*) c FROM applications WHERE status='pending'").fetchone()["c"],
            "invitations": conn.execute("SELECT COUNT(*) c FROM invitations").fetchone()["c"],
            "pending_reports": conn.execute("SELECT COUNT(*) c FROM reports WHERE status='pending'").fetchone()["c"],
        }


@app.get("/api/admin/projects")
def admin_projects(
    limit: int = Query(default=30, ge=1, le=100),
    user: dict = Depends(current_user),
):
    require_admin(user)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.*,u.first_name AS owner_name,u.username AS owner_username
            FROM projects p
            JOIN users u ON u.telegram_id=p.owner_id
            ORDER BY p.created_at DESC,p.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/admin/users")
def admin_users(
    limit: int = Query(default=30, ge=1, le=100),
    user: dict = Depends(current_user),
):
    require_admin(user)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT telegram_id,first_name,username,role,stack,blocked,created_at,updated_at
            FROM users
            ORDER BY created_at DESC,telegram_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/projects/{project_id}/moderation")
def moderate_project(
    project_id: int,
    status: ModerationStatus,
    user: dict = Depends(current_user),
):
    require_admin(user)
    with db() as conn:
        row = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Проект не найден")
        conn.execute(
            "UPDATE projects SET moderation_status=?,updated_at=? WHERE id=?",
            (status, int(time.time()), project_id),
        )
    return {"ok": True, "status": status}


@app.post("/api/admin/users/{telegram_id}/block")
def block_user(
    telegram_id: int,
    blocked: bool = Query(default=True),
    user: dict = Depends(current_user),
):
    admin_id = require_admin(user)
    if telegram_id == admin_id:
        raise HTTPException(400, "Нельзя заблокировать самого себя")
    with db() as conn:
        row = conn.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Пользователь не найден")
        conn.execute(
            "UPDATE users SET blocked=?,updated_at=? WHERE telegram_id=?",
            (int(blocked), int(time.time()), telegram_id),
        )
    return {"ok": True, "blocked": blocked}


@app.get("/api/admin/reports")
def admin_reports(
    limit: int = Query(default=30, ge=1, le=100),
    user: dict = Depends(current_user),
):
    require_admin(user)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT r.*,u.first_name AS reporter_name,u.username AS reporter_username
            FROM reports r
            LEFT JOIN users u ON u.telegram_id=r.reporter_id
            ORDER BY CASE WHEN r.status='pending' THEN 0 ELSE 1 END,
                     r.created_at DESC,r.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        result = []
        for row in rows:
            item = dict(row)
            if item["target_type"] == "project":
                target = conn.execute(
                    "SELECT title,owner_id,moderation_status FROM projects WHERE id=?",
                    (item["target_id"],),
                ).fetchone()
                item["target_label"] = target["title"] if target else "Удалённый проект"
                item["target_state"] = target["moderation_status"] if target else "missing"
                item["target_owner_id"] = target["owner_id"] if target else None
            else:
                target = conn.execute(
                    "SELECT first_name,username,blocked FROM users WHERE telegram_id=?",
                    (item["target_id"],),
                ).fetchone()
                if target:
                    item["target_label"] = target["first_name"] or (
                        f"@{target['username']}" if target["username"] else f"ID {item['target_id']}"
                    )
                    item["target_state"] = "blocked" if target["blocked"] else "active"
                else:
                    item["target_label"] = "Удалённый пользователь"
                    item["target_state"] = "missing"
            result.append(item)
    return result


@app.post("/api/admin/reports/{report_id}/decision")
def decide_report(
    report_id: int,
    status: ReportStatus,
    user: dict = Depends(current_user),
):
    require_admin(user)
    with db() as conn:
        row = conn.execute("SELECT id FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Жалоба не найдена")
        conn.execute(
            "UPDATE reports SET status=?,updated_at=? WHERE id=?",
            (status, int(time.time()), report_id),
        )
    return {"ok": True, "status": status}


@app.post("/api/admin/broadcast")
async def admin_broadcast(
    data: BroadcastIn,
    user: dict = Depends(current_user),
):
    admin_id = require_admin(user)
    return await send_broadcast(
        admin_id=admin_id,
        text=data.text,
        test_only=data.test_only,
    )


@app.get("/api/admin/broadcasts")
def admin_broadcasts(
    limit: int = Query(default=5, ge=1, le=20),
    user: dict = Depends(current_user),
):
    require_admin(user)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id,text,target_count,sent_count,blocked_count,error_count,status,created_at,finished_at
            FROM broadcasts
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/cleanup-demo")
def cleanup_demo(user: dict = Depends(current_user)):
    require_admin(user)
    demo_ids = (900001, 900002, 900003)

    with db() as conn:
        demo_projects = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM projects WHERE owner_id IN (?,?,?)",
                demo_ids,
            ).fetchall()
        ]

        if demo_projects:
            placeholders = ",".join("?" for _ in demo_projects)
            conn.execute(
                f"DELETE FROM applications WHERE project_id IN ({placeholders})",
                demo_projects,
            )
            conn.execute(
                f"DELETE FROM invitations WHERE project_id IN ({placeholders})",
                demo_projects,
            )
            conn.execute(
                f"DELETE FROM reports WHERE target_type='project' AND target_id IN ({placeholders})",
                demo_projects,
            )
            conn.execute(
                f"DELETE FROM projects WHERE id IN ({placeholders})",
                demo_projects,
            )

        conn.execute(
            "DELETE FROM applications WHERE user_id IN (?,?,?)",
            demo_ids,
        )
        conn.execute(
            "DELETE FROM invitations WHERE sender_id IN (?,?,?) OR recipient_id IN (?,?,?)",
            demo_ids + demo_ids,
        )
        conn.execute(
            "DELETE FROM reports WHERE (target_type='user' AND target_id IN (?,?,?)) OR reporter_id IN (?,?,?)",
            demo_ids + demo_ids,
        )
        deleted_users = conn.execute(
            "DELETE FROM users WHERE telegram_id IN (?,?,?)",
            demo_ids,
        ).rowcount

    return {
        "ok": True,
        "deleted_users": deleted_users,
        "deleted_projects": len(demo_projects),
    }


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
