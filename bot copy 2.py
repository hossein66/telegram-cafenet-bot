# bot.py – Telegram bot with caching, no authentication (fixed)

import json
import logging
import sys
import traceback
import re
import asyncio
import time
import base64
import io
import hashlib
from typing import List, Dict, Optional, Any, Tuple, Callable
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, NetworkError, TimedOut
import aiohttp

# ─── CONFIG ──────────────────────────────────────────────────────────────
BOT_TOKEN = "8824483780:AAH7CES3hG69Kf0q_wA6D0oe1-tE0Lxz7pI"
API_BASE = "https://cofenet-online.ir"

# ─── STATIC TOKEN FOR BOT REQUESTS ─────────────────────────────────────
# 🔴 REPLACE THIS with a valid JWT token from your backend.
# You can get one by logging in via the web app and copying it.
BOT_API_TOKEN = "your_bot_user_jwt_token_here"   # <-- CHANGE THIS

# ─── LOGGING ────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─── CACHE ──────────────────────────────────────────────────────────────
class TTLCache:
    def __init__(self, default_ttl=300):
        self._cache = {}
        self._ttl = {}
        self._lock = asyncio.Lock()
        self.default_ttl = default_ttl

    async def get(self, key):
        async with self._lock:
            if key in self._cache:
                if key in self._ttl and time.time() > self._ttl[key]:
                    del self._cache[key]
                    del self._ttl[key]
                    return None
                return self._cache[key]
            return None

    async def set(self, key, value, ttl=None):
        async with self._lock:
            self._cache[key] = value
            if ttl is None:
                ttl = self.default_ttl
            self._ttl[key] = time.time() + ttl

    async def clear(self):
        async with self._lock:
            self._cache.clear()
            self._ttl.clear()

cache = TTLCache(default_ttl=300)

def cached(ttl: Optional[int] = None):
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            key_parts = [func.__name__]
            for arg in args:
                if isinstance(arg, (str, int, float, bool)):
                    key_parts.append(str(arg))
                elif arg is not None:
                    key_parts.append(str(id(arg)))
            for k, v in sorted(kwargs.items()):
                if isinstance(v, (str, int, float, bool)):
                    key_parts.append(f"{k}:{v}")
                elif v is not None:
                    key_parts.append(f"{k}:{id(v)}")
            cache_key = hashlib.md5('|'.join(key_parts).encode()).hexdigest()

            cached_result = await cache.get(cache_key)
            if cached_result is not None:
                return cached_result

            result = await func(*args, **kwargs)
            await cache.set(cache_key, result, ttl)
            return result
        return wrapper
    return decorator

# ─── API CLIENT ──────────────────────────────────────────────────────────

class APIClient:
    def __init__(self, base_url: str, bot_token: str):
        self.base_url = base_url.rstrip('/')
        self.bot_token = bot_token
        self.session = aiohttp.ClientSession()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, method: str, path: str, token: Optional[str] = None, **kwargs) -> Any:
        url = self.base_url + path
        headers = {"Content-Type": "application/json"}
        auth_token = token or self.bot_token
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        async with self.session.request(method, url, headers=headers, **kwargs) as resp:
            if resp.status >= 400:
                text = await resp.text()
                logger.error(f"API error {resp.status}: {text}")
                raise Exception(f"خطای سرور: {resp.status} – {text}")
            if resp.status == 204:
                return None
            return await resp.json()

    @cached(ttl=3600)
    async def get_categories(self) -> List[Dict]:
        return await self._request("GET", "/api/categories")

    @cached(ttl=300)
    async def get_services(self, cursor: Optional[str] = None, limit: int = 20,
                          category: Optional[str] = None, search: Optional[str] = None) -> Dict:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if category:
            params["category"] = category
        if search:
            params["search"] = search
        return await self._request("GET", "/api/services", params=params)

    @cached(ttl=300)
    async def get_featured_services(self) -> List[Dict]:
        return await self._request("GET", "/api/services/featured")

    @cached(ttl=600)
    async def get_service_detail(self, service_id: str) -> Dict:
        return await self._request("GET", f"/api/services/{service_id}")

    @cached(ttl=86400)
    async def get_payment_info(self) -> Dict:
        return await self._request("GET", "/api/payment/info")

    @cached(ttl=86400)
    async def get_provinces(self) -> List[str]:
        return await self._request("GET", "/api/provinces")

    @cached(ttl=86400)
    async def get_cities(self, province: str) -> List[str]:
        return await self._request("GET", f"/api/cities/{province}")

    async def create_request(self, token: str, service_id: str, service_title: str,
                            price: int, documents: List[Dict], receipt_image: Optional[str] = None) -> Dict:
        payload = {
            "serviceId": service_id,
            "serviceTitle": service_title,
            "price": price,
            "documents": documents,
            "receiptImage": receipt_image,
        }
        return await self._request("POST", "/api/requests", token=token, json=payload)

# ─── GLOBAL CLIENT ──────────────────────────────────────────────────────

_api_client = None

def get_api_client() -> APIClient:
    global _api_client
    if _api_client is None:
        _api_client = APIClient(API_BASE, BOT_API_TOKEN)
    return _api_client

# ─── HELPERS ─────────────────────────────────────────────────────────────

def format_price(price: int) -> str:
    try:
        return f"{price:,}".replace(",", "٬")
    except:
        return str(price)

def escape_markdown(text: str) -> str:
    if not text:
        return ""
    special = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", text)

def convert_persian_to_english(s: str) -> str:
    mapping = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    return s.translate(mapping)

def validate_field(value: str, field: Dict) -> Tuple[bool, str]:
    rules = field.get("validationRules", {})
    v = value.strip()

    if rules.get("min") and len(v) < rules["min"]:
        return False, f"حداقل {rules['min']} کاراکتر وارد کنید."
    if rules.get("max") and len(v) > rules["max"]:
        return False, f"حداکثر {rules['max']} کاراکتر مجاز است."

    if rules.get("pattern"):
        try:
            if not re.match(rules["pattern"], v):
                return False, "فرمت وارد شده معتبر نیست."
        except:
            pass

    field_type = field.get("type", "text")
    if field_type == "number":
        cleaned = convert_persian_to_english(v)
        if not cleaned.isdigit():
            return False, "لطفاً فقط عدد وارد کنید."
        return True, cleaned
    elif field_type == "nationalCode":
        cleaned = convert_persian_to_english(v)
        if not re.match(r"^[0-9]{10}$", cleaned):
            return False, "کد ملی باید ۱۰ رقم باشد."
        return True, cleaned
    elif field_type == "date":
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            return False, "تاریخ باید به فرمت YYYY-MM-DD باشد."
        return True, v
    return True, v

async def send_or_edit(target, text, reply_markup=None, parse_mode=None):
    """Send a new message if target is a Message, else edit the existing message."""
    if hasattr(target, 'edit_message_text'):
        await target.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await target.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

# ─── ERROR HANDLER DECORATOR ──────────────────────────────────────────

def handle_errors(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            logger.error(traceback.format_exc())
            update = args[0] if args else None
            if update and hasattr(update, 'message') and update.message:
                await update.message.reply_text("❌ خطا در ارتباط با سرور. لطفاً مجددا تلاش کنید.")
            elif update and hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.edit_message_text("❌ خطا در ارتباط با سرور. لطفاً مجددا تلاش کنید.")
    return wrapper

async def set_menu_button(application: Application) -> None:
    try:
        commands = [BotCommand("start", "🏠 صفحه اصلی")]
        await application.bot.set_my_commands(commands)
        logger.info("Menu button set.")
    except Exception as e:
        logger.error(f"Error setting menu: {e}")

# ─── START / MENU ──────────────────────────────────────────────────────

@handle_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user = update.effective_user
    welcome_text = (
        f"به ربات کافی نت خوش آمدید {user.first_name}! 👋\n"
        "چه کمکی می‌توان به شما بکنم؟\n\n"
        "🔹 از دکمه‌های زیر برای مشاهده خدمات استفاده کنید.\n"
        "🔹 همچنین می‌توانید نام سرویس یا دسته را جستجو کنید."
    )

    try:
         categories = await get_categories_local()
    except Exception as e:
        logger.error(f"Failed to fetch categories: {e}")
        categories = []

    keyboard = [
        [InlineKeyboardButton("⭐ خدمات پر کاربرد", callback_data="top_services")],
        [InlineKeyboardButton("📊 همه خدمات", callback_data="all_services")]
    ]

    row = []
    for cat in categories[:10]:
        cat_name = cat.get("name", "بدون نام")
        cat_id = cat.get("id")
        cat_icon = cat.get("icon")
        if cat_id is None:
            continue
        row.append(InlineKeyboardButton(f"{cat_icon} {cat_name[:35]}", callback_data=f"cat_{cat_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    if len(categories) > 10:
        keyboard.append([
            InlineKeyboardButton("📂 بیشتر...", callback_data="categories_page_1")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

# ─── CATEGORIES PAGINATION ────────────────────────────────────────────

@handle_errors
async def show_categories_page(query, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    categories = await get_categories_local()
    total = len(categories)
    page_size = 10
    total_pages = (total + page_size - 1) // page_size
    if page >= total_pages:
        page = total_pages - 1
    start = page * page_size
    end = min(start + page_size, total)
    page_cats = categories[start:end]

    text = f"📂 *دسته‌بندی خدمات* (صفحه {page+1} از {total_pages})\n\nلطفاً یک دسته را انتخاب کنید:\n\n"
    keyboard = []
    row = []
    for cat in page_cats:
        cat_name = cat.get("name", "بدون نام")
        cat_id = cat.get("id")
        cat_icon = cat.get("icon")
        if cat_id is None:
            continue
        row.append(InlineKeyboardButton(f"{cat_icon} {cat_name[:35]}", callback_data=f"cat_{cat_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"categories_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("صفحه بعد ▶️", callback_data=f"categories_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def get_categories_local():
    client = get_api_client()
    categories = await client.get_categories()
    return categories

# ─── SERVICES LISTS ────────────────────────────────────────────────────

@handle_errors
async def show_services(query, context: ContextTypes.DEFAULT_TYPE,
                        category: Optional[str] = None,
                        search: Optional[str] = None,
                        cursor: Optional[str] = None,
                        page: int = 0,
                        callback_prefix: str = "svc") -> None:
    client = get_api_client()
    limit = 10
    try:
        resp = await client.get_services(cursor=cursor, limit=limit, category=category, search=search)
    except Exception as e:
        await query.edit_message_text(f"❌ خطا در دریافت خدمات: {e}")
        return

    items = resp.get("items", [])
    has_more = resp.get("hasMore", False)
    next_cursor = resp.get("nextCursor")

    if not items:
        await query.edit_message_text("❌ هیچ خدماتی یافت نشد.")
        return

    text = f"*نتایج* (نمایش {len(items)} مورد)\n\n"
    for svc in items:
        title = svc.get("serviceTitle", "بدون عنوان")
        price = format_price(svc.get("price", 0))
        text += f"• {escape_markdown(title)}\n  💰 {price} تومان\n\n"

    keyboard = []
    for svc in items:
        title = svc.get("serviceTitle", "بدون عنوان")[:30]
        price = format_price(svc.get("price", 0))
        button_text = f"📋 {escape_markdown(title)} - {price}ت"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"service_{svc.get('serviceId')}")])

    if has_more and next_cursor:
        context.user_data['next_cursor'] = next_cursor
        context.user_data['current_category'] = category
        context.user_data['current_search'] = search
        keyboard.append([InlineKeyboardButton("⬇️ بیشتر", callback_data=f"load_more_{callback_prefix}")])

    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

@handle_errors
async def show_top_services(query, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    client = get_api_client()
    items = await client.get_featured_services()
    if not items:
        await query.edit_message_text("⭐ هیچ سرویس پرکاربردی یافت نشد.")
        return

    limit = 10
    start = page * limit
    end = start + limit
    page_items = items[start:end]
    total = len(items)

    if not page_items:
        await query.edit_message_text("⭐ هیچ سرویس پرکاربردی در این صفحه نیست.")
        return

    text = f"⭐ *خدمات پر کاربرد* (صفحه {page+1} از {(total+limit-1)//limit})\n\n"
    for svc in page_items:
        title = svc.get("serviceTitle", "بدون عنوان")
        price = format_price(svc.get("price", 0))
        text += f"• {escape_markdown(title)}\n  💰 {price} تومان\n\n"

    keyboard = []
    for svc in page_items:
        title = svc.get("serviceTitle", "بدون عنوان")[:30]
        price = format_price(svc.get("price", 0))
        button_text = f"📋 {escape_markdown(title)} - {price}ت"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"service_{svc.get('serviceId')}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"top_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("صفحه بعد ▶️", callback_data=f"top_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

@handle_errors
async def show_all_services(query, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    await show_services(query, context, cursor=None, page=page, callback_prefix="all")

@handle_errors
async def show_category_services(query, context: ContextTypes.DEFAULT_TYPE, category_id: int, page: int = 0):
    categories = await get_categories_local() 
    cat_name = None
    for cat in categories:
        if cat.get("id") == category_id:
            cat_name = cat.get("name")
            break
    if not cat_name:
        await query.edit_message_text("❌ دسته‌بندی یافت نشد.")
        return
    await show_services(query, context, category=cat_name, cursor=None, page=page, callback_prefix=f"cat_{category_id}")

# ─── SERVICE DETAIL ────────────────────────────────────────────────────

@handle_errors
async def show_service_detail(query, service_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    client = get_api_client()
    try:
        detail = await client.get_service_detail(service_id)
    except Exception as e:
        await query.edit_message_text(f"❌ خطا در دریافت اطلاعات سرویس: {e}")
        return

    context.user_data['current_service'] = detail

    data = detail.get("data", {})
    title = detail.get("serviceTitle", "بدون عنوان")
    price = data.get("price", 0)
    description = data.get("description", "")
    duration = data.get("duration", "")
    fields = []
    forms = data.get("forms", [])
    if forms:
        fields = forms[0].get("fields", [])

    text = f"📋 *{escape_markdown(title)}*\n\n"
    if duration:
        text += f"⏱ {escape_markdown(duration)}\n"
    if description:
        text += f"{escape_markdown(description)}\n\n"
    text += f"💰 قیمت: {format_price(price)} تومان\n"
    if fields:
        text += f"📄 تعداد مدارک: {len(fields)}\n"
    else:
        text += "📄 این سرویس نیازی به مدرک ندارد.\n"

    keyboard = [
        [InlineKeyboardButton("📝 ثبت درخواست", callback_data=f"request_{service_id}")],
        [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

# ─── REQUEST FLOW ──────────────────────────────────────────────────────

@handle_errors
async def handle_request_start(query, service_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    service = context.user_data.get('current_service')
    if not service or service.get('serviceId') != service_id:
        client = get_api_client()
        try:
            service = await client.get_service_detail(service_id)
            context.user_data['current_service'] = service
        except Exception as e:
            await query.edit_message_text(f"❌ خطا در دریافت سرویس: {e}")
            return

    await start_field_collection(query, service, context)

@handle_errors
async def start_field_collection(query, service: Dict, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["answers"] = {}
    context.user_data["field_index"] = 0
    fields = []
    forms = service.get("data", {}).get("forms", [])
    if forms:
        fields = forms[0].get("fields", [])

    if not fields:
        await show_payment_step(query, context)
    else:
        context.user_data["fields"] = fields
        await ask_field(query, 0, context)

@handle_errors
async def ask_field(target, index: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    fields = context.user_data.get("fields", [])
    if index >= len(fields):
        await show_summary(target, context)
        return

    field = fields[index]
    label = field.get("label", "بدون عنوان")
    field_type = field.get("type", "text")
    is_required = field.get("isRequired", False)
    placeholder = field.get("placeholder", "")
    options = field.get("options", [])

    context.user_data["current_field_index"] = index
    context.user_data["current_field"] = field

    text = f"📄 *مرحله {index+1} از {len(fields)}*\n\n"
    text += f"🔹 {label}\n"
    # if is_required:
    #     text += "⚠️\n"
    # text += f"📌 نوع: {field_type}\n\n" این فیلد الزامی است.

    if field_type in ("select", "province", "city"):
        text += "لطفاً یکی از گزینه‌ها را انتخاب کنید:"
        if field_type == "province":
            client = get_api_client()
            provinces = await client.get_provinces()
            options = provinces
        elif field_type == "city":
            province = context.user_data.get("selected_province", "")
            if not province:
                await send_or_edit(target, "❌ لطفاً ابتدا استان را انتخاب کنید.\nاز دکمه '🔙 بازگشت به مدارک' استفاده کنید.")
                return
            client = get_api_client()
            cities = await client.get_cities(province)
            options = cities
        else:
            options = field.get("options", [])

        if not options:
            await send_or_edit(target, "❌ گزینه‌ای موجود نیست.")
            return

        keyboard = []
        for opt in options:
            keyboard.append([InlineKeyboardButton(opt, callback_data=f"field_opt_{opt}")])
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await send_or_edit(target, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return

    elif field_type == "file":
        text += "📎 لطفاً یک تصویر یا فایل ارسال کنید.\nاز دکمه ضمیمه (📎) برای ارسال عکس استفاده کنید."
        await send_or_edit(target, text, parse_mode=ParseMode.MARKDOWN)
        context.user_data["awaiting_file"] = True
        return

    else:  # text, number, textarea
        text += f"{placeholder}"
        await send_or_edit(target, text, parse_mode=ParseMode.MARKDOWN)
        context.user_data["awaiting_text"] = True
        return

@handle_errors
async def show_summary(target, context: ContextTypes.DEFAULT_TYPE) -> None:
    answers = context.user_data.get("answers", {})
    service = context.user_data.get("current_service", {})
    data = service.get("data", {})
    price = data.get("price", 0)

    text = "📋 *خلاصه اطلاعات*\n\n"
    for key, value in answers.items():
        val_display = value[:100] + "..." if len(value) > 100 else value
        text += f"🔹 {key}: {val_display}\n"
    text += f"\n💰 مبلغ قابل پرداخت: {format_price(price)} تومان\n\n"
    text += "✅ اطلاعات صحیح است؟"

    keyboard = [
        [InlineKeyboardButton("✅ بله، پرداخت", callback_data="pay_now")],
        [InlineKeyboardButton("🔙 بازگشت به مدارک", callback_data="back_to_fields")],
        [InlineKeyboardButton("🔄 شروع مجدد", callback_data="restart_request")],
        [InlineKeyboardButton("❌ لغو", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_or_edit(target, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

@handle_errors
async def show_payment_step(target, context: ContextTypes.DEFAULT_TYPE) -> None:
    client = get_api_client()
    payment_info = await client.get_payment_info()
    card_number = payment_info.get("cardNumber", "نامشخص")
    holder = payment_info.get("accountHolder", "نامشخص")
    bank = payment_info.get("bankName", "بانک")

    service = context.user_data.get("current_service", {})
    data = service.get("data", {})
    price = data.get("price", 0)

    text = f"💳 *پرداخت*\n\nمبلغ: {format_price(price)} تومان\n\n"
    text += f"🏦 شماره کارت: `{card_number}`\n"
    text += f"👤 به نام: {holder}\n"
    text += f"🏛 بانک: {bank}\n\n"
    text += "❗️ پس از واریز، تصویر رسید را ارسال کنید."

    keyboard = [
        [InlineKeyboardButton("🔙 بازگشت به خلاصه", callback_data="back_to_summary")],
        [InlineKeyboardButton("❌ لغو", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_or_edit(target, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    context.user_data["awaiting_payment_receipt"] = True

# ─── SUBMIT REQUEST (FIXED) ───────────────────────────────────────────

@handle_errors
async def submit_request(target, context: ContextTypes.DEFAULT_TYPE) -> None:
    token = BOT_API_TOKEN
    if not token:
        if hasattr(target, 'message'):
            reply_target = target.message
        else:
            reply_target = target
        await reply_target.reply_text("❌ توکن ربات تنظیم نشده است. لطفاً با پشتیبان تماس بگیرید.")
        return

    service = context.user_data.get("current_service", {})
    data = service.get("data", {})
    service_id = service.get("serviceId")
    title = service.get("serviceTitle")
    price = data.get("price", 0)
    answers = context.user_data.get("answers", {})
    receipt = context.user_data.get("receipt_image")

    documents = [{"title": key, "value": value} for key, value in answers.items()]

    client = get_api_client()
    try:
        result = await client.create_request(
            token=token,
            service_id=service_id,
            service_title=title,
            price=price,
            documents=documents,
            receipt_image=receipt
        )
    except Exception as e:
        if hasattr(target, 'message'):
            reply_target = target.message
        else:
            reply_target = target
        await reply_target.reply_text(f"❌ خطا در ثبت درخواست: {e}")
        return

    if hasattr(target, 'message'):
        reply_target = target.message
    else:
        reply_target = target

    if result.get("success"):
        await reply_target.reply_text(
            "✅ *درخواست شما با موفقیت ثبت شد!*\n\n"
            "📞 کارشناسان ما به زودی با شما تماس خواهند گرفت.\n"
            "🙏 از اعتماد شما سپاسگزاریم.",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.clear()
    else:
        await reply_target.reply_text("❌ ثبت درخواست ناموفق بود. لطفاً مجدداً تلاش کنید.")

# ─── TEXT AND PHOTO HANDLERS ──────────────────────────────────────────

@handle_errors
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    # SEARCH (default behaviour)
    if not context.user_data.get("awaiting_text") and not context.user_data.get("awaiting_file") and not context.user_data.get("awaiting_payment_receipt"):
        client = get_api_client()
        try:
            resp = await client.get_services(search=text)
        except Exception as e:
            await update.message.reply_text(f"❌ خطا در جستجو: {e}")
            return
        items = resp.get("items", [])
        if not items:
            await update.message.reply_text(f"❌ هیچ نتیجه‌ای برای '{text}' یافت نشد.")
            return
        keyboard = []
        for svc in items[:10]:
            title = svc.get("serviceTitle", "بدون عنوان")[:30]
            price = format_price(svc.get("price", 0))
            button_text = f"📋 {escape_markdown(title)} - {price}ت"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"service_{svc.get('serviceId')}")])
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🔍 *نتایج جستجو برای '{escape_markdown(text)}'*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # FIELD TEXT INPUT
    if context.user_data.get("awaiting_text"):
        field_index = context.user_data.get("current_field_index", 0)
        field = context.user_data.get("current_field", {})
        if not field:
            await update.message.reply_text("❌ خطا در دریافت فیلد.")
            return

        is_valid, result = validate_field(text, field)
        if not is_valid:
            await update.message.reply_text(f"❌ {result}\nلطفاً مجدداً وارد کنید:")
            return

        answers = context.user_data.get("answers", {})
        answers[field.get("key", field.get("label", ""))] = result
        context.user_data["answers"] = answers
        context.user_data["awaiting_text"] = False

        next_index = field_index + 1
        fields = context.user_data.get("fields", [])
        if next_index >= len(fields):
            await show_summary(update.message, context)
        else:
            await ask_field(update.message, next_index, context)
        return

@handle_errors
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_file"):
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        base64_data = base64.b64encode(file_bytes.getvalue()).decode('utf-8')
        data_url = f"data:image/jpeg;base64,{base64_data}"

        field_index = context.user_data.get("current_field_index", 0)
        field = context.user_data.get("current_field", {})
        answers = context.user_data.get("answers", {})
        answers[field.get("key", field.get("label", ""))] = data_url
        context.user_data["answers"] = answers
        context.user_data["awaiting_file"] = False

        await update.message.reply_text("✅ فایل دریافت شد.")

        next_index = field_index + 1
        fields = context.user_data.get("fields", [])
        if next_index >= len(fields):
            await show_summary(update.message, context)
        else:
            await ask_field(update.message, next_index, context)

    elif context.user_data.get("awaiting_payment_receipt"):
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        base64_data = base64.b64encode(file_bytes.getvalue()).decode('utf-8')
        data_url = f"data:image/jpeg;base64,{base64_data}"

        context.user_data["receipt_image"] = data_url
        context.user_data["awaiting_payment_receipt"] = False

        await update.message.reply_text("✅ تصویر رسید دریافت شد. در حال ثبت نهایی...")
        await submit_request(update.message, context)   # pass Message object
    else:
        await update.message.reply_text("❌ در حال حاضر نیازی به تصویر نداریم.")

# ─── CALLBACK QUERY HANDLER ───────────────────────────────────────────

@handle_errors
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.info(f"Callback: {data}")

    if data == "back_to_menu":
        await start(update, context)
        return
    if data == "top_services":
        await show_top_services(query, context)
        return
    if data == "all_services":
        await show_all_services(query, context)
        return
    if data.startswith("categories_page_"):
        page = int(data.split("_")[2])
        await show_categories_page(query, context, page)
        return
    if data.startswith("cat_"):
        cat_id = int(data[4:])
        await show_category_services(query, context, cat_id)
        return
    if data.startswith("top_page_"):
        page = int(data.split("_")[2])
        await show_top_services(query, context, page)
        return
    if data.startswith("page_all_"):
        page = int(data.split("_")[2])
        await show_all_services(query, context, page)
        return
    if data.startswith("page_cat_"):
        parts = data.split("_")
        page = int(parts[2])
        cat_id = int(parts[3])
        await show_category_services(query, context, cat_id, page)
        return
    if data.startswith("load_more_"):
        cursor = context.user_data.get("next_cursor")
        category = context.user_data.get("current_category")
        search = context.user_data.get("current_search")
        if not cursor:
            await query.edit_message_text("❌ دیگر خدماتی موجود نیست.")
            return
        await show_services(query, context, category=category, search=search, cursor=cursor, callback_prefix="load")
        return

    if data.startswith("service_"):
        service_id = data[8:]
        await show_service_detail(query, service_id, context)
        return

    if data.startswith("request_"):
        service_id = data[8:]
        await handle_request_start(query, service_id, context)
        return

    if data.startswith("field_opt_"):
        option = data[10:]
        field_index = context.user_data.get("current_field_index", 0)
        field = context.user_data.get("current_field", {})
        if not field:
            await query.edit_message_text("❌ خطا در دریافت فیلد.")
            return

        if field.get("type") == "province":
            context.user_data["selected_province"] = option
            answers = context.user_data.get("answers", {})
            answers[field.get("key", field.get("label", ""))] = option
            context.user_data["answers"] = answers
            next_index = field_index + 1
            fields = context.user_data.get("fields", [])
            if next_index < len(fields) and fields[next_index].get("type") == "city":
                await ask_field(query, next_index, context)
            else:
                await ask_field(query, next_index, context)
            return

        if field.get("type") == "city":
            answers = context.user_data.get("answers", {})
            answers[field.get("key", field.get("label", ""))] = option
            context.user_data["answers"] = answers
            next_index = field_index + 1
            await ask_field(query, next_index, context)
            return

        answers = context.user_data.get("answers", {})
        answers[field.get("key", field.get("label", ""))] = option
        context.user_data["answers"] = answers
        next_index = field_index + 1
        await ask_field(query, next_index, context)
        return

    if data == "pay_now":
        await show_payment_step(query, context)
        return
    if data == "back_to_summary":
        await show_summary(query, context)
        return
    if data == "back_to_fields":
        await ask_field(query, 0, context)
        return
    if data == "restart_request":
        context.user_data["answers"] = {}
        context.user_data["field_index"] = 0
        context.user_data["receipt_image"] = None
        await ask_field(query, 0, context)
        return

    await query.edit_message_text("❌ گزینه نامعتبر.")

# ─── RUN BOT ───────────────────────────────────────────────────────────

def run_bot():
    max_retries = 5
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            logger.info(f"Starting bot (attempt {attempt+1}/{max_retries})...")
            application = Application.builder().token(BOT_TOKEN).build()

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(set_menu_button(application))

            async def shutdown():
                client = get_api_client()
                await client.close()
            application.post_stop = shutdown

            application.add_handler(CommandHandler("start", start))
            application.add_handler(CallbackQueryHandler(button_callback))
            application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                stop_signals=None,
            )
            break

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as e:
            logger.error(f"Error in run_bot: {e}")
            logger.error(traceback.format_exc())
            if attempt < max_retries - 1:
                wait_time = retry_delay * (attempt + 1)
                logger.info(f"Restarting in {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.error("Max retries reached. Exiting.")
                sys.exit(1)

def main():
    while True:
        try:
            run_bot()
            break
        except KeyboardInterrupt:
            logger.info("Bot stopped.")
            break
        except Exception as e:
            logger.error(f"Fatal error in main: {e}")
            logger.error(traceback.format_exc())
            logger.info("Restarting in 10 seconds...")
            time.sleep(10)

if __name__ == "__main__":
    main()