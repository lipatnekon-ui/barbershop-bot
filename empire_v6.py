#!/usr/bin/env python3
"""
👑 EMPIRE SAAS V16 — ULTIMATE EDITION (FIXED NAVIGATION)
"""
import asyncio, asyncpg, os, time, logging, secrets, csv, io, aiohttp
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from dataclasses import dataclass
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, F, BaseMiddleware, Router
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import uvicorn, re

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1"))
YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET", "4100119552067165")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@barbershop_owner")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise ValueError("❌ Неверный BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
app = FastAPI(title="👑 EMPIRE SAAS V16")

# Московское время
MOSCOW_TZ = timezone(timedelta(hours=3))

PLAN_PRICES = {"free": 0, "start": 490, "pro": 990, "business": 1490}
PLAN_NAMES = {"free": "FREE", "start": "СТАРТ", "pro": "ПРО", "business": "БИЗНЕС"}
PLAN_LIMITS = {"free": 15, "start": 65, "pro": 1000, "business": 9999}
MASTER_LIMITS = {"free": 1, "start": 3, "pro": 10, "business": 999}

USER_CACHE = {}
REFERRAL_CODES = {}
BOT_USERNAME = None

def generate_ref_code(uid):
    code = secrets.token_urlsafe(6)
    REFERRAL_CODES[code] = uid
    return code

@dataclass
class RequestContext:
    user_id: int
    company_id: int = None
    role: str = "client"
    plan: str = "free"

def back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="main_menu")
    return kb.as_markup()

def main_menu_kb(role):
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Записи", callback_data="bookings_screen")
    kb.button(text="👨‍🔧 Мастера", callback_data="masters_screen")
    kb.button(text="💈 Услуги", callback_data="services_screen")
    kb.button(text="💳 Тариф", callback_data="billing_screen")
    kb.button(text="📊 Аналитика", callback_data="analytics")
    kb.adjust(1)
    return kb.as_markup()

def owner_menu(ctx):
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Записи", callback_data="bookings_screen")
    kb.button(text="👨‍🔧 Мастера", callback_data="masters_screen")
    kb.button(text="💈 Услуги", callback_data="services_screen")
    kb.button(text="💳 Тарифы", callback_data="billing_screen")
    kb.button(text="📊 Аналитика", callback_data="analytics")
    kb.button(text="👥 Клиенты", callback_data="clients_analytics")
    kb.button(text="🏆 Рейтинг мастеров", callback_data="masters_rating")
    kb.button(text="🔑 Код приглашения", callback_data="show_invite_code")
    kb.button(text="✏️ Контакты компании", callback_data="edit_contacts")
    kb.button(text="📎 Экспорт CSV", callback_data="export_csv")
    kb.button(text="📞 Связаться с админом", callback_data="contact")
    kb.adjust(1)
    return kb.as_markup()

def client_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Записаться", callback_data="bookings_screen")
    kb.button(text="📞 Контакты", callback_data="contact")
    kb.adjust(1)
    return kb.as_markup()

def master_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Моё расписание", callback_data="today_schedule")
    return kb.as_markup()

class ContextMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, event, data):
        uid = event.from_user.id if hasattr(event, "from_user") else None
        if not uid:
            return await handler(event, data)
        
        if uid in USER_CACHE:
            data["ctx"] = USER_CACHE[uid]
        else:
            user = await self.db.fetchrow("SELECT * FROM users WHERE id=$1", uid)
            ctx = RequestContext(
                user_id=uid,
                company_id=user["company_id"] if user else None,
                role=user["role"] if user else "client",
                plan=user["plan"] if user else "free"
            )
            USER_CACHE[uid] = ctx
            data["ctx"] = ctx
        return await handler(event, data)

class DB:
    def __init__(self):
        self.pool = None

    async def get_user(self, uid):
        async with self.pool.acquire() as c:
            return await c.fetchrow("SELECT * FROM users WHERE id=$1", uid)

    async def create_user(self, uid):
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO users(id) VALUES($1) ON CONFLICT DO NOTHING", uid)

    async def create_company(self, name, owner_id, owner_username="", telegram="", address="", phone=""):
        async with self.pool.acquire() as c:
            invite_code = secrets.token_urlsafe(8)
            cid = await c.fetchval("""
                INSERT INTO companies(name, owner_id, telegram, address, phone, invite_code)
                VALUES($1, $2, $3, $4, $5, $6) RETURNING id
            """, name, owner_id, telegram, address, phone, invite_code)
            await c.execute("UPDATE users SET company_id=$1, role='owner' WHERE id=$2", cid, owner_id)
            for svc in [("Стрижка", 800, 30), ("Борода", 500, 20), ("Комплекс", 1200, 45)]:
                await c.execute("INSERT INTO services(company_id, name, price, duration) VALUES($1,$2,$3,$4)", cid, svc[0], svc[1], svc[2])
            return cid, invite_code

    async def join_company(self, user_id, invite_code):
        async with self.pool.acquire() as c:
            company = await c.fetchrow("SELECT id FROM companies WHERE invite_code=$1", invite_code)
            if not company:
                return False
            await c.execute("UPDATE users SET company_id=$1, role='client' WHERE id=$2", company["id"], user_id)
            return True

    async def get_company(self, cid):
        async with self.pool.acquire() as c:
            return await c.fetchrow("SELECT * FROM companies WHERE id=$1", cid)

    async def get_masters(self, cid):
        async with self.pool.acquire() as c:
            return await c.fetch("SELECT * FROM masters WHERE company_id=$1", cid)

    async def add_master(self, cid, name, telegram_id=None):
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO masters(company_id, name, telegram_id) VALUES($1,$2,$3)", cid, name, telegram_id)

    async def delete_master(self, mid, cid):
        async with self.pool.acquire() as c:
            await c.execute("DELETE FROM masters WHERE id=$1 AND company_id=$2", mid, cid)

    async def get_services(self, cid):
        async with self.pool.acquire() as c:
            return await c.fetch("SELECT * FROM services WHERE company_id=$1 ORDER BY id", cid)

    async def add_service(self, cid, name, price, duration):
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO services(company_id, name, price, duration) VALUES($1,$2,$3,$4)", cid, name, price, duration)

    async def delete_service(self, sid, cid):
        async with self.pool.acquire() as c:
            await c.execute("DELETE FROM services WHERE id=$1 AND company_id=$2", sid, cid)

    async def get_all_bookings(self, cid):
        async with self.pool.acquire() as c:
            return await c.fetch("""
                SELECT b.*, m.name as master_name, s.name as service_name
                FROM bookings b
                JOIN masters m ON b.master_id = m.id
                JOIN services s ON b.service_id = s.id
                WHERE b.company_id=$1 AND b.status='active'
                ORDER BY b.start_time
            """, cid)

    async def get_bookings_for_export(self, cid, days=30):
        async with self.pool.acquire() as c:
            date_from = datetime.now(MOSCOW_TZ) - timedelta(days=days)
            return await c.fetch("""
                SELECT b.id, b.start_time, b.end_time, b.status,
                       m.name as master_name, s.name as service_name, s.price,
                       u.id as client_id
                FROM bookings b
                JOIN masters m ON b.master_id = m.id
                JOIN services s ON b.service_id = s.id
                JOIN users u ON b.client_id = u.id
                WHERE b.company_id=$1 AND b.start_time >= $2
                ORDER BY b.start_time DESC
            """, cid, date_from)

    async def update_company_contacts(self, cid, telegram, address, phone):
        async with self.pool.acquire() as c:
            await c.execute("UPDATE companies SET telegram=$1, address=$2, phone=$3 WHERE id=$4", telegram, address, phone, cid)

    async def get_revenue_by_company(self, cid):
        async with self.pool.acquire() as c:
            return await c.fetch("SELECT plan, SUM(amount) as revenue FROM revenue WHERE company_id=$1 GROUP BY plan", cid)

    async def get_booking_stats(self, cid):
        async with self.pool.acquire() as c:
            today = datetime.now(MOSCOW_TZ).date()
            week_ago = today - timedelta(days=7)
            today_count = await c.fetchval("SELECT COUNT(*) FROM bookings WHERE company_id=$1 AND start_time::date = $2 AND status='active'", cid, today)
            week_count = await c.fetchval("SELECT COUNT(*) FROM bookings WHERE company_id=$1 AND start_time::date >= $2 AND status='active'", cid, week_ago)
            popular = await c.fetchrow("""
                SELECT s.name, COUNT(*) as cnt
                FROM bookings b
                JOIN services s ON b.service_id = s.id
                WHERE b.company_id=$1 AND b.status='active'
                GROUP BY s.name
                ORDER BY cnt DESC
                LIMIT 1
            """, cid)
            return {"today": today_count, "week": week_count, "popular": popular["name"] if popular else "—", "popular_count": popular["cnt"] if popular else 0}

    async def init(self):
        import os
        
        db_url = os.getenv("DATABASE_URL")
        
        if not db_url:
            raise ValueError("❌ DATABASE_URL не найден!")
        
        if "proxy.rlwy.net" in db_url and "sslmode" not in db_url:
            db_url += "?sslmode=require"
            print("🔒 Добавлен SSL")
        
        print(f"🔌 Подключаюсь к PostgreSQL...")
        self.pool = await asyncpg.create_pool(db_url, timeout=30)
        print("✅ БД подключена!")
        
        # СОЗДАЕМ ТАБЛИЦЫ
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, role TEXT DEFAULT 'client', company_id INT, plan TEXT DEFAULT 'free', paid_until TIMESTAMP);
                CREATE TABLE IF NOT EXISTS companies(id SERIAL PRIMARY KEY, name TEXT, owner_id BIGINT, invite_code TEXT UNIQUE, telegram TEXT DEFAULT '', address TEXT DEFAULT '', phone TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS masters(id SERIAL PRIMARY KEY, company_id INT, name TEXT, telegram_id BIGINT);
                CREATE TABLE IF NOT EXISTS services(id SERIAL PRIMARY KEY, company_id INT, name TEXT, price INT, duration INT);
                CREATE TABLE IF NOT EXISTS bookings(id SERIAL PRIMARY KEY, company_id INT, master_id INT, client_id BIGINT, service_id INT, start_time TIMESTAMP, end_time TIMESTAMP, status TEXT DEFAULT 'active', reminder_24h_sent BOOLEAN DEFAULT FALSE, reminder_2h_sent BOOLEAN DEFAULT FALSE, review_sent BOOLEAN DEFAULT FALSE);
                CREATE TABLE IF NOT EXISTS revenue(id SERIAL PRIMARY KEY, company_id INT, user_id BIGINT, amount INT, plan TEXT, created_at TIMESTAMP DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS reviews(id SERIAL PRIMARY KEY, booking_id INT, client_id BIGINT, master_id INT, rating INT, comment TEXT, created_at TIMESTAMP DEFAULT NOW());
            """)
            
            await conn.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS invite_code TEXT UNIQUE DEFAULT ''")
            await conn.execute("ALTER TABLE masters ADD COLUMN IF NOT EXISTS telegram_id BIGINT DEFAULT NULL")
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminder_24h_sent BOOLEAN DEFAULT FALSE")
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminder_2h_sent BOOLEAN DEFAULT FALSE")
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS review_sent BOOLEAN DEFAULT FALSE")
            
            print("✅ Таблицы созданы!")

db = DB()

class AccessService:
    FEATURES = {
        "free": ["book", "manage_masters"],
        "start": ["book", "manage_masters", "today"],
        "pro": ["book", "manage_masters", "today", "analytics"],
        "business": ["book", "manage_masters", "today", "analytics", "export"]
    }
    MASTER_LIMITS = {"free": 1, "start": 3, "pro": 10, "business": 999}
    def can(self, ctx, feature): return feature in self.FEATURES.get(ctx.plan, [])
    async def can_add_master(self, ctx):
        if not self.can(ctx, "manage_masters"): return False
        async with db.pool.acquire() as c:
            count = await c.fetchval("SELECT COUNT(*) FROM masters WHERE company_id=$1", ctx.company_id)
            return count < self.MASTER_LIMITS.get(ctx.plan, 1)

access = AccessService()

class BookFSM(StatesGroup):
    master = State()
    service = State()
    date = State()
    time = State()

class Onboarding(StatesGroup):
    company_name = State()
    company_telegram = State()
    company_address = State()
    company_phone = State()

class JoinCompanyFSM(StatesGroup):
    invite_code = State()

class AddMasterFSM(StatesGroup):
    name = State()
    telegram_id = State()

class ServiceFSM(StatesGroup):
    name = State()
    price = State()
    duration = State()

class EditContactsFSM(StatesGroup):
    telegram = State()
    address = State()
    phone = State()

def generate_header(company_name, current_plan):
    plan_name = PLAN_NAMES.get(current_plan, "FREE")
    limit = PLAN_LIMITS.get(current_plan, 0)
    return f"👑 EMPIRE SAAS\n🏢 {company_name}\n📊 Тариф: {plan_name}\n📈 Лимит: {limit} записей/мес"

async def get_gpt_advice(stats_text):
    if not OPENAI_API_KEY:
        return "Добавьте API ключ OpenAI в .env для получения советов"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{
                        "role": "user",
                        "content": f"""
Ты консультант барбершопов.

Дай 3 конкретных рекомендации.

Статистика:
{stats_text}

Ответ максимум 120 слов.
"""
                    }],
                    "max_tokens": 200
                }
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"GPT error: {e}")
    return "Добавьте больше услуг и напоминайте клиентам о записи!"

# ==================== КОМАНДЫ И CALLBACK'И ====================
@router.message(Command("today"))
async def today_schedule(msg: Message, ctx: RequestContext):
    if not access.can(ctx, "today"):
        await msg.answer("❌ Команда доступна на тарифах СТАРТ, ПРО, БИЗНЕС")
        return
    async with db.pool.acquire() as c:
        master = await c.fetchrow("SELECT * FROM masters WHERE telegram_id=$1 AND company_id=$2", msg.from_user.id, ctx.company_id)
    if not master:
        await msg.answer("❌ Вы не добавлены как мастер в этой компании")
        return
    today = datetime.now(MOSCOW_TZ).date()
    bookings = await c.fetch("""
        SELECT b.*, s.name as service_name
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        WHERE b.master_id=$1 AND b.start_time::date = $2 AND b.status='active'
        ORDER BY b.start_time
    """, master["id"], today)
    if not bookings:
        await msg.answer(f"📅 Расписание на {today.strftime('%d.%m.%Y')}\n\nНет записей")
        return
    text = f"📅 Расписание на {today.strftime('%d.%m.%Y')}\n\n"
    for b in bookings:
        start_msk = b['start_time'].astimezone(MOSCOW_TZ) if b['start_time'].tzinfo else b['start_time']
        text += f"⏰ {start_msk.strftime('%H:%M')} — {b['service_name']}\n"
    await msg.answer(text)

@router.callback_query(F.data == "today_schedule")
async def today_schedule_callback(cb: CallbackQuery, ctx: RequestContext):
    if not access.can(ctx, "today"):
        await cb.answer("❌ Доступно на тарифах СТАРТ, ПРО, БИЗНЕС", show_alert=True)
        return
    await today_schedule(cb.message, ctx)
    await cb.answer()

@router.callback_query(F.data.startswith("rating_"))
async def handle_rating(cb: CallbackQuery, ctx: RequestContext):
    parts = cb.data.split("_")
    booking_id = int(parts[1])
    rating = int(parts[2])
    async with db.pool.acquire() as c:
        booking = await c.fetchrow("SELECT master_id FROM bookings WHERE id=$1", booking_id)
        if booking:
            await c.execute("INSERT INTO reviews(booking_id, client_id, master_id, rating) VALUES($1,$2,$3,$4)", booking_id, ctx.user_id, booking["master_id"], rating)
    await cb.message.edit_text(f"✅ Спасибо за оценку! ⭐ {rating}/5")
    await cb.answer()

@router.message(CommandStart())
async def start(msg: Message, state: FSMContext, ctx: RequestContext):
    args = msg.text.split()
    if len(args) > 1:
        ref_code = args[1]
        if ref_code in REFERRAL_CODES:
            owner_id = REFERRAL_CODES[ref_code]
            async with db.pool.acquire() as c:
                company = await c.fetchrow("SELECT id, invite_code FROM companies WHERE owner_id=$1", owner_id)
                if company:
                    await db.join_company(ctx.user_id, company["invite_code"])
                    user = await db.get_user(ctx.user_id)
                    USER_CACHE[ctx.user_id] = RequestContext(user_id=ctx.user_id, company_id=user["company_id"], role=user["role"], plan=user["plan"])
                    await msg.answer("✅ Вы автоматически присоединились к компании!")
    await db.create_user(ctx.user_id)
    await state.clear()
    
    if ctx.company_id:
        company = await db.get_company(ctx.company_id)
        header = generate_header(company["name"], ctx.plan)
        async with db.pool.acquire() as c:
            is_master = await c.fetchval("SELECT id FROM masters WHERE telegram_id=$1 AND company_id=$2", msg.from_user.id, ctx.company_id)
        if is_master and ctx.role != "owner":
            await msg.answer(f"{header}\n👇 Панель мастера:", reply_markup=master_menu())
        elif ctx.role == "owner":
            ref_code = generate_ref_code(ctx.user_id)
            ref_link = f"\n\n🔗 Реферальная ссылка: t.me/{BOT_USERNAME}?start={ref_code}"
            await msg.answer(f"{header}{ref_link}\n👇 Панель владельца:", reply_markup=owner_menu(ctx))
        else:
            await msg.answer(f"{header}\n👇 Доступные действия:", reply_markup=client_menu())
        return
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🏪 Создать компанию", callback_data="create_company")
    kb.button(text="🔑 Войти по коду", callback_data="join_company")
    kb.adjust(1)
    await msg.answer(
        "👑 EMPIRE SAAS V16\n\nУ вас ещё нет компании.\n\n• Создать — стать владельцем бизнеса\n• Войти по коду — клиентом к существующей компании\n\n📚 /help — справка\n\n💰 Тарифы: СТАРТ 490₽/мес | ПРО 990₽/мес | БИЗНЕС 1490₽/мес",
        reply_markup=kb.as_markup()
    )

@router.callback_query(F.data == "create_company")
async def create_company_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Onboarding.company_name)
    await cb.message.edit_text("🏪 Создание компании\n\nВведите название компании:", reply_markup=back_kb())
    await cb.answer()

@router.message(Onboarding.company_name)
async def create_company_name(msg: Message, state: FSMContext):
    if not 2 <= len(msg.text.strip()) <= 50:
        await msg.answer("❌ Название от 2 до 50 символов")
        return
    await state.update_data(company_name=msg.text.strip())
    await state.set_state(Onboarding.company_telegram)
    await msg.answer("📱 Telegram компании\n\nВведите ссылку (например @barbershop):\nМожно пропустить, отправив '-'")

@router.message(Onboarding.company_telegram)
async def create_company_telegram(msg: Message, state: FSMContext):
    val = msg.text.strip()
    await state.update_data(company_telegram=val if val != "-" else "")
    await state.set_state(Onboarding.company_address)
    await msg.answer("📍 Адрес компании\n\nВведите адрес:\nМожно пропустить, отправив '-'")

@router.message(Onboarding.company_address)
async def create_company_address(msg: Message, state: FSMContext):
    val = msg.text.strip()
    await state.update_data(company_address=val if val != "-" else "")
    await state.set_state(Onboarding.company_phone)
    await msg.answer("📞 Телефон компании\n\nВведите номер:\nМожно пропустить, отправив '-'")

@router.message(Onboarding.company_phone)
async def create_company_phone(msg: Message, state: FSMContext, ctx: RequestContext):
    val = msg.text.strip()
    data = await state.get_data()
    cid, invite_code = await db.create_company(
        name=data["company_name"],
        owner_id=ctx.user_id,
        owner_username=msg.from_user.username or "",
        telegram=data["company_telegram"],
        address=data["company_address"],
        phone=val if val != "-" else ""
    )
    USER_CACHE[ctx.user_id] = RequestContext(user_id=ctx.user_id, company_id=cid, role="owner", plan="free")
    await state.clear()
    await msg.answer(f"✅ Компания «{data['company_name']}» создана!\n\n🔑 Код приглашения: `{invite_code}`\n\n👇 Панель владельца:", reply_markup=owner_menu(ctx))

@router.callback_query(F.data == "join_company")
async def join_company_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(JoinCompanyFSM.invite_code)
    await cb.message.edit_text("🔑 Вход в компанию\n\nВведите код приглашения:", reply_markup=back_kb())
    await cb.answer()

@router.message(JoinCompanyFSM.invite_code)
async def join_company_done(msg: Message, state: FSMContext, ctx: RequestContext):
    code = msg.text.strip()
    success = await db.join_company(ctx.user_id, code)
    if not success:
        await msg.answer("❌ Неверный код приглашения.")
        return
    user = await db.get_user(ctx.user_id)
    USER_CACHE[ctx.user_id] = RequestContext(user_id=ctx.user_id, company_id=user["company_id"], role=user["role"], plan=user["plan"])
    company = await db.get_company(user["company_id"])
    await state.clear()
    await msg.answer(f"✅ Вы присоединились к компании\n{generate_header(company['name'], user['plan'])}", reply_markup=client_menu())

@router.callback_query(F.data == "show_invite_code")
async def show_invite_code(cb: CallbackQuery, ctx: RequestContext):
    company = await db.get_company(ctx.company_id)
    if not company:
        await cb.answer("❌ Компания не найдена")
        return
    await cb.message.edit_text(f"🔑 Код приглашения:\n\n`{company['invite_code']}`\n\nОтправьте этот код клиентам.", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "edit_contacts")
async def edit_contacts_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(EditContactsFSM.telegram)
    await cb.message.edit_text("✏️ Редактирование контактов\n\nВведите Telegram (можно пропустить, отправив '-'):", reply_markup=back_kb())
    await cb.answer()

@router.message(EditContactsFSM.telegram)
async def edit_contacts_telegram(msg: Message, state: FSMContext):
    val = msg.text.strip()
    await state.update_data(telegram=val if val != "-" else "")
    await state.set_state(EditContactsFSM.address)
    await msg.answer("📍 Введите адрес (можно пропустить, отправив '-'):")

@router.message(EditContactsFSM.address)
async def edit_contacts_address(msg: Message, state: FSMContext):
    val = msg.text.strip()
    await state.update_data(address=val if val != "-" else "")
    await state.set_state(EditContactsFSM.phone)
    await msg.answer("📞 Введите телефон (можно пропустить, отправив '-'):")

@router.message(EditContactsFSM.phone)
async def edit_contacts_phone(msg: Message, state: FSMContext, ctx: RequestContext):
    val = msg.text.strip()
    data = await state.get_data()
    await db.update_company_contacts(ctx.company_id, data["telegram"], data["address"], val if val != "-" else "")
    await state.clear()
    await msg.answer("✅ Контакты обновлены!", reply_markup=owner_menu(ctx))

@router.callback_query(F.data == "clients_analytics")
async def clients_analytics(cb: CallbackQuery, ctx: RequestContext):
    async with db.pool.acquire() as c:
        rows = await c.fetch("""
            SELECT
                b.client_id,
                COUNT(*) as visits,
                COALESCE(SUM(s.price),0) as spent,
                MAX(b.start_time) as last_visit
            FROM bookings b
            JOIN services s ON s.id=b.service_id
            WHERE b.company_id=$1
            AND b.status='active'
            GROUP BY b.client_id
            ORDER BY spent DESC
            LIMIT 20
        """, ctx.company_id)

    if not rows:
        await cb.message.edit_text(
            "👥 Клиенты пока отсутствуют",
            reply_markup=back_kb()
        )
        return

    text = "👥 ТОП КЛИЕНТЫ\n\n"

    for r in rows:
        text += (
            f"🆔 {r['client_id']}\n"
            f"📅 Визитов: {r['visits']}\n"
            f"💰 Потрачено: {r['spent']} ₽\n"
            f"🕒 Последний визит: {r['last_visit'].strftime('%d.%m.%Y')}\n\n"
        )

    await cb.message.edit_text(text, reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "masters_rating")
async def masters_rating(cb: CallbackQuery, ctx: RequestContext):
    async with db.pool.acquire() as c:
        rows = await c.fetch("""
            SELECT
                m.name,
                ROUND(COALESCE(AVG(r.rating),0)::numeric,2) as rating,
                COUNT(r.id) as reviews
            FROM masters m
            LEFT JOIN reviews r ON r.master_id=m.id
            WHERE m.company_id=$1
            GROUP BY m.id,m.name
            ORDER BY rating DESC
        """, ctx.company_id)

    text = "🏆 Рейтинг мастеров\n\n"

    for r in rows:
        text += (
            f"⭐ {r['name']}\n"
            f"Рейтинг: {r['rating']}\n"
            f"Отзывов: {r['reviews']}\n\n"
        )

    await cb.message.edit_text(text, reply_markup=back_kb())
    await cb.answer()

# ==================== ОСНОВНЫЕ ЭКРАНЫ ====================
@router.callback_query(F.data == "main_menu")
async def main_menu_screen(cb: CallbackQuery, state: FSMContext, ctx: RequestContext):
    await state.clear()
    if ctx.role == "owner":
        await cb.message.edit_text(f"🏠 Главное меню\n\n👑 {ctx.role.upper()} | 💳 {PLAN_NAMES.get(ctx.plan, 'FREE')}", reply_markup=owner_menu(ctx))
    elif ctx.role == "client":
        await cb.message.edit_text(f"🏠 Главное меню\n\n👤 КЛИЕНТ | 💳 {PLAN_NAMES.get(ctx.plan, 'FREE')}", reply_markup=client_menu())
    else:
        await cb.message.edit_text(f"🏠 Главное меню\n\n👨‍🔧 МАСТЕР | 💳 {PLAN_NAMES.get(ctx.plan, 'FREE')}", reply_markup=master_menu())
    await cb.answer()

@router.callback_query(F.data == "bookings_screen")
async def bookings_screen(cb: CallbackQuery, ctx: RequestContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Записаться", callback_data="book")
    kb.button(text="📋 Мои записи", callback_data="my_bookings")
    if ctx.role == "owner":
        kb.button(text="📋 Все записи", callback_data="all_bookings")
    kb.button(text="⬅️ Назад", callback_data="main_menu")
    kb.adjust(1)
    await cb.message.edit_text("📅 Управление записями", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "masters_screen")
async def masters_screen(cb: CallbackQuery, ctx: RequestContext):
    if not ctx.company_id:
        await cb.answer("❌ Компания не найдена", show_alert=True)
        return
    masters = await db.get_masters(ctx.company_id)
    kb = InlineKeyboardBuilder()
    for m in masters:
        kb.button(text=f"🗑 {m['name']}", callback_data=f"del_master_{m['id']}")
    if await access.can_add_master(ctx):
        kb.button(text="➕ Добавить мастера", callback_data="add_master_screen")
    kb.button(text="⬅️ Назад", callback_data="main_menu")
    kb.adjust(1)
    text = f"👨‍🔧 Мастера ({len(masters)}/{MASTER_LIMITS.get(ctx.plan, 1)})\n\n"
    for m in masters:
        text += f"• {m['name']}\n"
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "add_master_screen")
async def add_master_screen(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddMasterFSM.name)
    await cb.message.edit_text("👨‍🔧 Введите имя мастера:", reply_markup=back_kb())
    await cb.answer()

@router.message(AddMasterFSM.name)
async def add_master_name(msg: Message, state: FSMContext):
    if not 2 <= len(msg.text.strip()) <= 50:
        await msg.answer("❌ Имя от 2 до 50 символов")
        return
    await state.update_data(name=msg.text.strip())
    await state.set_state(AddMasterFSM.telegram_id)
    await msg.answer("📱 Введите Telegram ID мастера (можно пропустить, отправив '-'):\nКак получить ID: @userinfobot", reply_markup=back_kb())

@router.message(AddMasterFSM.telegram_id)
async def add_master_telegram(msg: Message, state: FSMContext, ctx: RequestContext):
    val = msg.text.strip()
    telegram_id = None
    if val != "-" and val.isdigit():
        telegram_id = int(val)
    data = await state.get_data()
    await db.add_master(ctx.company_id, data["name"], telegram_id)
    await state.clear()
    await msg.answer(f"✅ Мастер {data['name']} добавлен!", reply_markup=back_kb())

@router.callback_query(F.data.startswith("del_master_"))
async def delete_master(cb: CallbackQuery, ctx: RequestContext):
    parts = cb.data.split("_")
    if len(parts) < 3 or not parts[2].isdigit():
        await cb.answer("❌ Ошибка: неверный ID мастера", show_alert=True)
        return
    mid = int(parts[2])
    await db.delete_master(mid, ctx.company_id)
    await cb.answer("✅ Удалён!")
    await masters_screen(cb, ctx)

@router.callback_query(F.data == "services_screen")
async def services_screen(cb: CallbackQuery, ctx: RequestContext):
    services = await db.get_services(ctx.company_id)
    kb = InlineKeyboardBuilder()
    for s in services:
        kb.button(text=f"✏️ {s['name']} — {s['price']}₽", callback_data=f"edit_service_{s['id']}")
        kb.button(text=f"❌", callback_data=f"del_service_{s['id']}")
    kb.button(text="➕ Добавить услугу", callback_data="add_service")
    kb.button(text="⬅️ Назад", callback_data="main_menu")
    kb.adjust(2)
    text = f"💈 Услуги ({len(services)})\n\n"
    for s in services:
        text += f"• {s['name']} — {s['price']}₽ ({s['duration']} мин)\n"
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "add_service")
async def add_service_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ServiceFSM.name)
    await cb.message.edit_text("💈 Введите название услуги:\n\nПример: Окрашивание бороды", reply_markup=back_kb())
    await cb.answer()

@router.message(ServiceFSM.name)
async def add_service_price(msg: Message, state: FSMContext):
    if not 2 <= len(msg.text.strip()) <= 50:
        await msg.answer("❌ Название от 2 до 50 символов")
        return
    await state.update_data(name=msg.text.strip())
    await state.set_state(ServiceFSM.price)
    await msg.answer("💰 Введите стоимость услуги в рублях:\n\nПример: 1500", reply_markup=back_kb())

@router.message(ServiceFSM.price)
async def add_service_duration(msg: Message, state: FSMContext):
    if not msg.text.isdigit() or int(msg.text) <= 0:
        await msg.answer("❌ Введите корректную цену (только цифры)")
        return
    await state.update_data(price=int(msg.text))
    await state.set_state(ServiceFSM.duration)
    await msg.answer("⏱️ Введите длительность услуги в минутах:\n\nПример: 60", reply_markup=back_kb())

@router.message(ServiceFSM.duration)
async def save_service(msg: Message, state: FSMContext, ctx: RequestContext):
    if not msg.text.isdigit() or int(msg.text) <= 0:
        await msg.answer("❌ Введите корректную длительность (только цифры)")
        return
    data = await state.get_data()
    await db.add_service(ctx.company_id, data["name"], data["price"], int(msg.text))
    await state.clear()
    await msg.answer(f"✅ Услуга «{data['name']}» добавлена!", reply_markup=back_kb())

@router.callback_query(F.data.startswith("del_service_"))
async def delete_service(cb: CallbackQuery, ctx: RequestContext):
    sid = int(cb.data.split("_")[2])
    await db.delete_service(sid, ctx.company_id)
    await cb.answer("✅ Услуга удалена!")
    await services_screen(cb, ctx)

@router.callback_query(F.data == "billing_screen")
async def billing_screen(cb: CallbackQuery, ctx: RequestContext):
    if ctx.plan == "business":
        await cb.answer("✅ Максимальный тариф!", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ СТАРТ — 490₽/мес", callback_data="pay_start")
    kb.button(text="🔥 ПРО — 990₽/мес", callback_data="pay_pro")
    kb.button(text="👑 БИЗНЕС — 1490₽/мес", callback_data="pay_business")
    kb.button(text="⬅️ Назад", callback_data="main_menu")
    kb.adjust(1)
    text = "💳 ВЫБЕРИ ТАРИФ:\n\n⭐ СТАРТ (490₽) — 65 записей/мес + мастера\n🔥 ПРО (990₽) — 1000 записей + аналитика\n👑 БИЗНЕС (1490₽) — безлимит + экспорт"
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("pay_"))
async def pay_activate(cb: CallbackQuery, ctx: RequestContext):
    plan = cb.data.split("_")[1]
    limit = PLAN_LIMITS.get(plan, 0)
    price = PLAN_PRICES.get(plan, 0)
    plan_name = PLAN_NAMES.get(plan, plan.upper())
    await cb.message.edit_text(f"💳 ОПЛАТА {plan_name}\n\n💰 {price}₽/мес\n📊 Лимит записей: {limit}\n🏦 `{YOOMONEY_WALLET}`\n\n1. Переведи сумму\n2. Напиши сюда: {ADMIN_USERNAME}\n3. Скажи какой тариф выбрал\n\n⚡ После оплаты активирую за 5 минут", reply_markup=back_kb())

@router.callback_query(F.data == "analytics")
async def analytics(cb: CallbackQuery, ctx: RequestContext):
    if not access.can(ctx, "analytics"):
        await cb.answer("❌ Аналитика доступна на тарифах ПРО и БИЗНЕС", show_alert=True)
        return
    
    async with db.pool.acquire() as c:
        stats = await db.get_booking_stats(ctx.company_id)
        
        top_master = await c.fetchrow("""
            SELECT m.name, COUNT(*) as cnt
            FROM bookings b
            JOIN masters m ON b.master_id = m.id
            WHERE b.company_id=$1 AND b.status='active'
            GROUP BY m.name
            ORDER BY cnt DESC
            LIMIT 1
        """, ctx.company_id)
        
        peak_hour = await c.fetchrow("""
            SELECT EXTRACT(HOUR FROM start_time) as hour, COUNT(*) as cnt
            FROM bookings
            WHERE company_id=$1 AND status='active'
            GROUP BY hour
            ORDER BY cnt DESC
            LIMIT 1
        """, ctx.company_id)
        
        top_revenue = await c.fetchrow("""
            SELECT m.name, SUM(s.price) as total
            FROM bookings b
            JOIN masters m ON b.master_id = m.id
            JOIN services s ON b.service_id = s.id
            WHERE b.company_id=$1 AND b.status='active'
            GROUP BY m.name
            ORDER BY total DESC
            LIMIT 1
        """, ctx.company_id)
        
        week_stats = await c.fetch("""
            SELECT DATE(start_time) as day, COUNT(*) as cnt
            FROM bookings
            WHERE company_id=$1 AND status='active' AND start_time > NOW() - interval '7 days'
            GROUP BY day
            ORDER BY day
        """, ctx.company_id)
        
        rows = await db.get_revenue_by_company(ctx.company_id)
    
    graph = ""
    for day in week_stats:
        bar = "█" * min(day["cnt"], 20)
        graph += f"{day['day'].strftime('%d.%m')}: {bar} {day['cnt']}\n"
    
    text = f"📊 *УМНАЯ АНАЛИТИКА*\n\n"
    text += f"📅 Записей сегодня: {stats['today']}\n"
    text += f"📆 Записей за неделю: {stats['week']}\n"
    text += f"🔥 Популярная услуга: {stats['popular']} ({stats['popular_count']} зап.)\n"
    
    if top_master:
        text += f"💪 Самый загруженный мастер: {top_master['name']} ({top_master['cnt']} зап.)\n"
    
    if peak_hour:
        text += f"⏰ Час-пик: {int(peak_hour['hour'])}:00 ({peak_hour['cnt']} зап.)\n"
    
    if top_revenue:
        text += f"💰 Самый прибыльный мастер: {top_revenue['name']} ({top_revenue['total']}₽)\n"
    
    text += f"\n📈 *Динамика за неделю:*\n{graph}\n"
    text += f"\n💳 *Выручка по тарифам:*\n"
    
    if rows:
        for r in rows:
            text += f"• {PLAN_NAMES.get(r['plan'], r['plan'])}: {r['revenue']}₽\n"
    else:
        text += "Нет данных\n"
    
    stats_text = f"Записей сегодня {stats['today']}, за неделю {stats['week']}, популярная услуга {stats['popular']}"
    advice = await get_gpt_advice(stats_text)
    text += f"\n🧠 *Совет ИИ:* {advice}\n"
    
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "book")
async def book_start(cb: CallbackQuery, state: FSMContext, ctx: RequestContext):
    if not access.can(ctx, "book"):
        await cb.answer("❌ Повысьте тариф", show_alert=True)
        return
    if not ctx.company_id:
        await cb.answer("❌ Компания не найдена", show_alert=True)
        return
    await state.clear()
    await state.set_state(BookFSM.master)
    masters = await db.get_masters(ctx.company_id)
    if not masters:
        await cb.answer("❌ Нет мастеров!", show_alert=True)
        await state.clear()
        return
    kb = InlineKeyboardBuilder()
    for m in masters:
        kb.button(text=f"💈 {m['name']}", callback_data=f"m_{m['id']}")
    kb.button(text="🏠 Главная", callback_data="main_menu")
    kb.adjust(1)
    await cb.message.edit_text("👨‍🔧 ВЫБЕРИТЕ МАСТЕРА:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(BookFSM.master, F.data.startswith("m_"))
async def book_master(cb: CallbackQuery, state: FSMContext, ctx: RequestContext):
    master_id = int(cb.data.split("_")[1])
    await state.update_data(master_id=master_id)
    await state.set_state(BookFSM.service)
    services = await db.get_services(ctx.company_id)
    if not services:
        await cb.answer("❌ Нет услуг! Добавьте в разделе Услуги", show_alert=True)
        await state.clear()
        return
    kb = InlineKeyboardBuilder()
    for s in services:
        kb.button(text=f"💈 {s['name']} — {s['price']}₽", callback_data=f"s_{s['id']}")
    kb.button(text="◀️ Назад", callback_data="book")
    kb.adjust(1)
    await cb.message.edit_text("💈 ВЫБЕРИТЕ УСЛУГУ:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(BookFSM.service, F.data.startswith("s_"))
async def book_date(cb: CallbackQuery, state: FSMContext):
    service_id = int(cb.data.split("_")[1])
    await state.update_data(service_id=service_id)
    await state.set_state(BookFSM.date)
    kb = InlineKeyboardBuilder()
    now_msk = datetime.now(MOSCOW_TZ)
    for i in range(14):
        day = now_msk + timedelta(days=i)
        kb.button(text=day.strftime("%d.%m (%a)"), callback_data=f"date_{day.strftime('%Y-%m-%d')}")
    kb.button(text="◀️ Назад", callback_data="book")
    kb.adjust(2)
    await cb.message.edit_text("📅 ВЫБЕРИТЕ ДАТУ:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(BookFSM.date, F.data.startswith("date_"))
async def book_time(cb: CallbackQuery, state: FSMContext):
    date_str = cb.data[5:]
    await state.update_data(date=date_str)
    await state.set_state(BookFSM.time)
    kb = InlineKeyboardBuilder()
    now_msk = datetime.now(MOSCOW_TZ)
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    for h in range(10, 19):
        if selected_date > now_msk.date() or (selected_date == now_msk.date() and h > now_msk.hour):
            kb.button(text=f"⏰ {h}:00", callback_data=f"t_{h}")
    kb.button(text="◀️ Назад", callback_data="book")
    kb.adjust(3)
    await cb.message.edit_text(f"📅 {date_str}\n\n⏰ Выберите время:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(BookFSM.time, F.data.startswith("t_"))
async def book_done(cb: CallbackQuery, state: FSMContext, ctx: RequestContext):
    try:
        hour = int(cb.data.split("_")[1])
        data = await state.get_data()
        if not all(k in data for k in ['master_id', 'service_id', 'date']):
            await cb.answer("❌ Сессия устарела. Начните заново", show_alert=True)
            await state.clear()
            return
        
        date_obj = datetime.strptime(data["date"], "%Y-%m-%d").date()
        start = datetime.combine(date_obj, datetime.min.time().replace(hour=hour))
        end = start + timedelta(hours=1)
        
        now_msk = datetime.now(MOSCOW_TZ)
        if start.date() < now_msk.date() or (start.date() == now_msk.date() and start.hour <= now_msk.hour):
            await cb.answer("❌ Нельзя записаться в прошлое!", show_alert=True)
            return
        
        async with db.pool.acquire() as c:
            existing = await c.fetchval("SELECT id FROM bookings WHERE master_id=$1 AND start_time=$2 AND status='active'", data["master_id"], start)
            if existing:
                await cb.answer("❌ Это время уже занято!", show_alert=True)
                return
            service = await c.fetchrow("SELECT name, price FROM services WHERE id=$1", data["service_id"])
            master = await c.fetchrow("SELECT name, telegram_id FROM masters WHERE id=$1", data["master_id"])
            bid = await c.fetchval("""
                INSERT INTO bookings(company_id, master_id, client_id, service_id, start_time, end_time, status)
                VALUES($1, $2, $3, $4, $5, $6, 'active') RETURNING id
            """, ctx.company_id, data["master_id"], ctx.user_id, data["service_id"], start, end)
            if master["telegram_id"]:
                try:
                    await bot.send_message(master["telegram_id"], f"📅 НОВАЯ ЗАПИСЬ!\n\n💇 {service['name']}\n📅 {start.strftime('%d.%m.%Y %H:%M')}\n🆔 #{bid}")
                except:
                    pass
        await cb.message.edit_text(f"✅ ЗАПИСЬ ПОДТВЕРЖДЕНА!\n\n🆔 #{bid}\n📅 {start.strftime('%d.%m.%Y')}\n⏰ {start.strftime('%H:%M')}", reply_markup=back_kb())
        await state.clear()
    except Exception as e:
        logger.error(f"Booking error: {e}")
        await cb.answer("❌ Ошибка записи", show_alert=True)

@router.callback_query(F.data == "my_bookings")
async def my_bookings(cb: CallbackQuery, ctx: RequestContext):
    async with db.pool.acquire() as c:
        rows = await c.fetch("""
            SELECT b.*, m.name as master_name, s.name as service_name
            FROM bookings b
            JOIN masters m ON b.master_id = m.id
            JOIN services s ON b.service_id = s.id
            WHERE b.client_id=$1 AND b.company_id=$2
            ORDER BY b.start_time
        """, ctx.user_id, ctx.company_id)
    if not rows:
        await cb.message.edit_text("📋 Нет записей.", reply_markup=back_kb())
    else:
        kb = InlineKeyboardBuilder()
        text = "📋 ВАШИ ЗАПИСИ:\n\n"
        for r in rows:
            start_msk = r['start_time'].astimezone(MOSCOW_TZ) if r['start_time'].tzinfo else r['start_time']
            status = "❌ Отменена" if r["status"] == "cancelled" else "✅ Активна"
            text += f"{status} #{r['id']} — {r['service_name']}\n👨‍🔧 {r['master_name']}\n📅 {start_msk.strftime('%d.%m.%Y %H:%M')}\n\n"
            if r["status"] == "active":
                kb.button(text=f"🔄 Повторить #{r['id']}", callback_data=f"repeat_{r['id']}")
                kb.button(text=f"❌ Отменить #{r['id']}", callback_data=f"cancel_{r['id']}")
        kb.button(text="⬅️ Назад", callback_data="main_menu")
        kb.adjust(1)
        await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "all_bookings")
async def all_bookings(cb: CallbackQuery, ctx: RequestContext):
    if ctx.role != "owner":
        await cb.answer("❌ Только для владельца", show_alert=True)
        return
    rows = await db.get_all_bookings(ctx.company_id)
    if not rows:
        await cb.message.edit_text("📋 Нет активных записей.", reply_markup=back_kb())
    else:
        text = "📋 ВСЕ ЗАПИСИ:\n\n"
        for r in rows:
            start_msk = r['start_time'].astimezone(MOSCOW_TZ) if r['start_time'].tzinfo else r['start_time']
            text += f"✅ #{r['id']} — {r['service_name']}\n👨‍🔧 {r['master_name']}\n📅 {start_msk.strftime('%d.%m.%Y %H:%M')}\n\n"
        await cb.message.edit_text(text, reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data.startswith("repeat_"))
async def repeat_booking(cb: CallbackQuery, state: FSMContext, ctx: RequestContext):
    booking_id = int(cb.data.split("_")[1])
    async with db.pool.acquire() as c:
        booking = await c.fetchrow("SELECT master_id, service_id FROM bookings WHERE id=$1 AND client_id=$2", booking_id, ctx.user_id)
        if booking:
            await state.update_data(master_id=booking["master_id"], service_id=booking["service_id"])
            await state.set_state(BookFSM.date)
            kb = InlineKeyboardBuilder()
            now_msk = datetime.now(MOSCOW_TZ)
            for i in range(14):
                day = now_msk + timedelta(days=i)
                kb.button(text=day.strftime("%d.%m (%a)"), callback_data=f"date_{day.strftime('%Y-%m-%d')}")
            kb.button(text="◀️ Назад", callback_data="my_bookings")
            kb.adjust(2)
            await cb.message.edit_text("📅 ВЫБЕРИТЕ ДАТУ ДЛЯ ПОВТОРА:", reply_markup=kb.as_markup())
        else:
            await cb.answer("❌ Не удалось повторить запись", show_alert=True)
    await cb.answer()

@router.callback_query(F.data.startswith("cancel_"))
async def cancel_booking(cb: CallbackQuery, ctx: RequestContext):
    bid = int(cb.data.split("_")[1])
    async with db.pool.acquire() as c:
        await c.execute("UPDATE bookings SET status='cancelled' WHERE id=$1 AND client_id=$2 AND status='active'", bid, ctx.user_id)
    await cb.answer("✅ Запись отменена!")
    await my_bookings(cb, ctx)

@router.callback_query(F.data == "export_csv")
async def export_csv_handler(cb: CallbackQuery, ctx: RequestContext):
    if not access.can(ctx, "export"):
        await cb.answer("❌ Экспорт доступен на тарифах ПРО и БИЗНЕС", show_alert=True)
        return
    rows = await db.get_bookings_for_export(ctx.company_id, 30)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Дата", "Время", "Услуга", "Цена", "Мастер", "Клиент ID", "Статус"])
    for r in rows:
        start_msk = r["start_time"].astimezone(MOSCOW_TZ) if r["start_time"].tzinfo else r["start_time"]
        writer.writerow([r["id"], start_msk.strftime("%Y-%m-%d"), start_msk.strftime("%H:%M"), r["service_name"], r["price"], r["master_name"], r["client_id"], r["status"]])
    output.seek(0)
    await cb.message.answer_document(BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=f"bookings_{datetime.now(MOSCOW_TZ).strftime('%Y%m%d')}.csv"), caption="📎 Экспорт записей за 30 дней")
    await cb.answer()

@router.callback_query(F.data == "contact")
async def contact(cb: CallbackQuery, ctx: RequestContext):
    company = await db.get_company(ctx.company_id) if ctx.company_id else None
    telegram = company["telegram"] if company and company["telegram"] else "Не указан"
    address = company["address"] if company and company["address"] else "Не указан"
    phone = company["phone"] if company and company["phone"] else "Не указан"
    await cb.message.edit_text(f"📞 КОНТАКТЫ\n\n💬 {telegram}\n📍 {address}\n📞 {phone}", reply_markup=back_kb())
    await cb.answer()

@router.message(Command("help"))
async def help_command(msg: Message, ctx: RequestContext):
    if ctx.role == "owner":
        help_text = "📚 СПРАВКА ВЛАДЕЛЬЦА\n\n/start — Главное меню\n/help — Эта справка\n\n💈 Управление услугами в разделе Услуги\n👨‍🔧 Мастера добавляются там же\n💰 Тарифы меняются в разделе Тариф\n📊 Аналитика показывает статистику"
    elif ctx.role == "client":
        help_text = "📚 СПРАВКА КЛИЕНТА\n\n/start — Главное меню\n/help — Эта справка\n\n📅 Запись в разделе Записи\n📋 Мои записи — просмотр и отмена\n🔄 Повтор записи — в Моих записях"
    else:
        help_text = "📚 СПРАВКА МАСТЕРА\n\n/start — Главное меню\n/help — Эта справка\n📅 /today — расписание на сегодня"
    await msg.answer(help_text)

# ==================== WORKERS ====================
async def reminder_worker():
    while True:
        try:
            now_msk = datetime.now(MOSCOW_TZ)
            now_naive = now_msk.replace(tzinfo=None)
            
            async with db.pool.acquire() as c:
                bookings = await c.fetch("""
                    SELECT b.*, m.name as master_name, s.name as service_name, comp.address
                    FROM bookings b
                    JOIN masters m ON b.master_id = m.id
                    JOIN services s ON b.service_id = s.id
                    JOIN companies comp ON b.company_id = comp.id
                    WHERE b.start_time BETWEEN $1 AND $2
                    AND b.status='active' AND b.reminder_24h_sent = FALSE
                """, now_naive, now_naive + timedelta(hours=24))
                for booking in bookings:
                    start_msk = booking['start_time'].astimezone(MOSCOW_TZ) if booking['start_time'].tzinfo else booking['start_time']
                    await bot.send_message(booking["client_id"], f"⏰ НАПОМИНАНИЕ!\n\nЗавтра в {start_msk.strftime('%H:%M')} у вас запись\n💈 {booking['service_name']} → {booking['master_name']}\n📍 {booking['address'] or 'адрес не указан'}")
                    await c.execute("UPDATE bookings SET reminder_24h_sent = TRUE WHERE id = $1", booking["id"])
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Reminder worker error: {e}")
            await asyncio.sleep(3600)

async def review_worker():
    while True:
        try:
            now_msk = datetime.now(MOSCOW_TZ)
            now_naive = now_msk.replace(tzinfo=None)
            one_hour_ago = (now_msk - timedelta(hours=1)).replace(tzinfo=None)
            
            async with db.pool.acquire() as c:
                bookings = await c.fetch("""
                    SELECT b.*, m.name as master_name, s.name as service_name
                    FROM bookings b
                    JOIN masters m ON b.master_id = m.id
                    JOIN services s ON b.service_id = s.id
                    WHERE b.start_time < $1 AND b.end_time < $2
                    AND b.status='active' AND b.review_sent = FALSE
                """, one_hour_ago, now_naive)
                for booking in bookings:
                    kb = InlineKeyboardBuilder()
                    for i in range(1, 6):
                        kb.button(text=f"⭐ {i}", callback_data=f"rating_{booking['id']}_{i}")
                    kb.adjust(5)
                    await bot.send_message(booking["client_id"], f"⭐ Как прошёл визит?\n\nОцените запись #{booking['id']}\n💇 {booking['service_name']} → {booking['master_name']}", reply_markup=kb.as_markup())
                    await c.execute("UPDATE bookings SET review_sent = TRUE WHERE id = $1", booking["id"])
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Review worker error: {e}")
            await asyncio.sleep(3600)

async def return_clients_worker():
    while True:
        try:
            async with db.pool.acquire() as c:
                rows = await c.fetch("""
                    SELECT
                        client_id,
                        company_id,
                        MAX(start_time) as last_visit
                    FROM bookings
                    WHERE status='active'
                    GROUP BY client_id, company_id
                    HAVING MAX(start_time)
                        < NOW() - interval '30 days'
                """)

            for row in rows:
                try:
                    await bot.send_message(
                        row["client_id"],
                        "👋 Давно не виделись!\n\n"
                        "Прошло больше месяца после последнего визита.\n"
                        "Пора обновить образ ✂️"
                    )
                except:
                    pass

            await asyncio.sleep(86400)

        except Exception as e:
            logger.error(f"return worker error: {e}")
            await asyncio.sleep(3600)

dp.include_router(router)

async def run_api():
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    await uvicorn.Server(config).serve()

async def main():
    global BOT_USERNAME
    await db.init()
    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username
    dp.message.middleware(ContextMiddleware(db.pool))
    dp.callback_query.middleware(ContextMiddleware(db.pool))
    asyncio.create_task(reminder_worker())
    asyncio.create_task(review_worker())
    asyncio.create_task(return_clients_worker())
    logger.info("👑 EMPIRE SAAS V16 ULTIMATE ЗАПУЩЕН!")
    print(f"✅ Bot username: @{BOT_USERNAME}")
    print("💰 Тарифы: СТАРТ 490₽ | ПРО 990₽ | БИЗНЕС 1490₽")
    print("📊 Умная аналитика + графики + GPT-советы включены")
    print("🔄 Повтор записи работает")
    print("💈 Барбер сам добавляет услуги")
    print("🕐 Московское время (UTC+3)")
    await asyncio.gather(run_api(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
