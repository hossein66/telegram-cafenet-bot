# bot.py
import json
import logging
import sys
import traceback
import re
import asyncio
import time
from typing import List, Dict, Optional, Any, Tuple
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
from telegram.error import TelegramError, NetworkError, TimedOut, RetryAfter

# Import config
try:
    from config import BOT_TOKEN
except ImportError:
    BOT_TOKEN = "8824483780:AAH7CES3hG69Kf0q_wA6D0oe1-tE0Lxz7pI"

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Load category data
def load_categories():
    try:
        with open("categoury.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("categoury.json file not found!")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing categories JSON: {e}")
        return []

# Load service data
def load_services():
    try:
        with open("cofenet-items.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("cofenet-items.json file not found!")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing services JSON: {e}")
        sys.exit(1)

# Load document types
def load_document_types():
    try:
        with open("docType.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("docType.json file not found!")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing document types JSON: {e}")
        return []

# Load data
CATEGORIES = load_categories()
SERVICES = load_services()
DOC_TYPES = load_document_types()

# Build category mapping from categories file
CATEGORY_NAMES = {cat["id"]: cat["name"] for cat in CATEGORIES}
CATEGORY_SORT = {cat["id"]: cat.get("sort", 999) for cat in CATEGORIES}

# Build document type mapping
DOC_TYPE_MAP = {doc["Id"]: doc for doc in DOC_TYPES}

# Helper functions
def format_price(price: int) -> str:
    """Format price with Persian commas"""
    try:
        return f"{price:,}".replace(",", "٬")
    except:
        return str(price)

def get_category_name(category_id: int) -> str:
    """Get category name from ID"""
    return CATEGORY_NAMES.get(category_id, f"دسته {category_id}")

def get_service_categories(service: Dict) -> List[str]:
    """Get category names for a service"""
    cat_ids = service.get("Categories", [])
    return [get_category_name(cid) for cid in cat_ids if cid in CATEGORY_NAMES]

def get_top_services(services: List[Dict]) -> List[Dict]:
    """Get services with IsTop10 = true"""
    return [s for s in services if s.get("IsTop10", False) and s.get("IsActive", False)]

def get_category_services(category_id: int, services: List[Dict]) -> List[Dict]:
    """Get services for a specific category ID"""
    result = []
    for service in services:
        if not service.get("IsActive", False):
            continue
        if category_id in service.get("Categories", []):
            result.append(service)
    result.sort(key=lambda x: x.get("Sort", 999))
    return result

def paginate_items(items: List[Dict], page: int = 0, page_size: int = 10) -> List[Dict]:
    """Paginate items"""
    try:
        start = page * page_size
        end = start + page_size
        return items[start:end]
    except:
        return []

def get_documents_with_types(service: Dict) -> List[Dict]:
    """Get documents with their types from service"""
    try:
        docs = service.get("Documents", [])
        if not docs:
            return [{"title": "نیازی به مدارک نیست", "typeId": 1, "type": "Text", "regex": ".*"}]
        
        result = []
        for doc in docs:
            doc_title = doc.get("title", "").strip()
            type_id = doc.get("typeId", 1)
            doc_type = DOC_TYPE_MAP.get(type_id, {"title": "Text", "REx": ".*"})
            result.append({
                "title": doc_title,
                "typeId": type_id,
                "type": doc_type.get("title", "Text"),
                "regex": doc_type.get("REx", ".*")
            })
        return result
    except:
        return [{"title": "نیازی به مدارک نیست", "typeId": 1, "type": "Text", "regex": ".*"}]

def get_documents_list(service: Dict) -> List[str]:
    """Parse documents from service (backward compatibility)"""
    docs = get_documents_with_types(service)
    return [doc["title"] for doc in docs]

def validate_document(value: str, doc_info: Dict) -> Tuple[bool, str]:
    """Validate document value against its regex pattern"""
    try:
        regex_pattern = doc_info.get("regex", ".*")
        if regex_pattern == ".*":
            return True, value
        
        # Compile and test regex
        pattern = re.compile(regex_pattern)
        if pattern.match(value):
            return True, value
        else:
            # Get user-friendly error message
            doc_type = doc_info.get("type", "Text")
            error_messages = {
                "Number": "❌ لطفا فقط عدد وارد کنید.",
                "Sheba": "❌ شماره شبا باید با IR شروع شود و ۲۴ رقم داشته باشد.\nمثال: IR123456789012345678901234",
                "Mobile": "❌ شماره موبایل باید ۱۱ رقم و با ۰۹ شروع شود.\nمثال: 09123456789",
                "NationalCode": "❌ کد ملی باید ۱۰ رقم باشد.\nمثال: 1234567890",
                "PostalCode": "❌ کد پستی باید ۱۰ رقم باشد.\nمثال: 1234567890",
                "Date": "❌ تاریخ باید به فرمت YYYY-MM-DD باشد.\nمثال: 1403-01-15",
                "Image": "❌ لطفا یک تصویر ارسال کنید.",
                "Text": "❌ فرمت وارد شده صحیح نیست."
            }
            return False, error_messages.get(doc_type, "❌ فرمت وارد شده صحیح نیست.")
    except Exception as e:
        logger.error(f"Error validating document: {e}")
        return False, "❌ خطا در اعتبارسنجی. لطفا مجددا تلاش کنید."

def get_active_categories(services: List[Dict]) -> List[int]:
    """Get all unique category IDs from active services"""
    category_ids = set()
    for service in services:
        if service.get("IsActive", False):
            for cat_id in service.get("Categories", []):
                category_ids.add(cat_id)
    # Sort by category sort order
    return sorted(category_ids, key=lambda x: CATEGORY_SORT.get(x, 999))

def paginate_categories(category_ids: List[int], page: int = 0, page_size: int = 10) -> List[int]:
    """Paginate categories"""
    try:
        start = page * page_size
        end = start + page_size
        return category_ids[start:end]
    except:
        return []

def is_image_document(doc_info: Dict) -> bool:
    """Check if document type is Image"""
    return doc_info.get("type", "") == "Image"

# Error handler decorator with retry
def handle_errors(func):
    """Decorator to handle errors in async functions with retry"""
    async def wrapper(*args, **kwargs):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return await func(*args, **kwargs)
            except (TelegramError, NetworkError, TimedOut) as e:
                logger.warning(f"Error in {func.__name__} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    logger.info(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Failed after {max_retries} attempts: {e}")
                    try:
                        for arg in args:
                            if hasattr(arg, 'message') and hasattr(arg.message, 'reply_text'):
                                await arg.message.reply_text("❌ خطا در ارتباط با سرور. لطفا مجددا تلاش کنید.")
                                break
                            elif hasattr(arg, 'edit_message_text'):
                                await arg.edit_message_text("❌ خطا در ارتباط با سرور. لطفا مجددا تلاش کنید.")
                                break
                    except:
                        pass
            except Exception as e:
                logger.error(f"Unexpected error in {func.__name__}: {e}")
                logger.error(traceback.format_exc())
                try:
                    for arg in args:
                        if hasattr(arg, 'message') and hasattr(arg.message, 'reply_text'):
                            await arg.message.reply_text("❌ خطای غیرمنتظره. لطفا مجددا تلاش کنید.")
                            break
                        elif hasattr(arg, 'edit_message_text'):
                            await arg.edit_message_text("❌ خطای غیرمنتظره. لطفا مجددا تلاش کنید.")
                            break
                except:
                    pass
                break
    return wrapper

async def set_menu_button(application: Application) -> None:
    """Set the menu button for the bot - only show start and menu"""
    try:
        commands = [
            BotCommand("start", "🏠 صفحه اصلی"),
            BotCommand("top", "⭐ خدمات پر کاربرد"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Menu button commands set successfully")
    except Exception as e:
        logger.error(f"Error setting menu button: {e}")

# Start command
@handle_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with main menu"""
    try:
        context.user_data.clear()
        
        user = update.effective_user
        welcome_text = (
            f"به ربات کافی نت خوش آمدید {user.first_name}! 👋\n"
            "چه کمکی می توان به شما بکنم؟\n\n"
            "🔹 با کلیک روی هر دکمه می‌توانید خدمات مربوطه را مشاهده کنید.\n"
            "🔹 همچنین می‌توانید عبارت مورد نظر را تایپ کرده و جستجو کنید.\n"
            "🔹 از منوی زیر نیز می‌توانید استفاده کنید."
        )
        
        # Get all unique category IDs from active services
        sorted_cats = get_active_categories(SERVICES)
        total_categories = len(sorted_cats)
        
        keyboard = []
        
        # Add "خدمات پر کاربرد" button at top
        keyboard.append([
            InlineKeyboardButton("⭐ خدمات پر کاربرد", callback_data="top_services")
        ])
        
        # Add category buttons in 2 columns (first page)
        row = []
        for cat_id in sorted_cats[:10]:
            cat_name = get_category_name(cat_id)
            row.append(InlineKeyboardButton(
                f"📂 {cat_name}", 
                callback_data=f"cat_{cat_id}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        # Add "Show more categories" if there are more than 10
        if total_categories > 10:
            keyboard.append([
                InlineKeyboardButton("📂 بیشتر...", callback_data="categories_page_1")
            ])
        
        keyboard.append([
            InlineKeyboardButton("📊 همه خدمات", callback_data="all_services")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            welcome_text, 
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in start: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ خطا در ارتباط با سرور. لطفا مجددا تلاش کنید.")

@handle_errors
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show main menu"""
    await start(update, context)

@handle_errors
async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show top services directly"""
    try:
        class MockQuery:
            def __init__(self, message, from_user):
                self.message = message
                self.from_user = from_user
                self.data = None
            
            async def edit_message_text(self, text, reply_markup=None, parse_mode=None, disable_web_page_preview=None):
                await self.message.reply_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview
                )
            
            async def answer(self):
                pass
        
        mock_query = MockQuery(update.message, update.effective_user)
        await show_top_services(mock_query)
    except Exception as e:
        logger.error(f"Error in top_command: {e}")
        await update.message.reply_text("❌ خطا در نمایش خدمات پر کاربرد.")

# Callback query handler
@handle_errors
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks"""
    try:
        query = update.callback_query
        await query.answer()
        
        data = query.data
        logger.info(f"Callback data: {data}")
        
        if data == "top_services":
            await show_top_services(query)
        elif data == "all_services":
            await show_all_services(query)
        elif data.startswith("categories_page_"):
            page = int(data.split("_")[2])
            await show_categories_page(query, page)
        elif data.startswith("cat_"):
            category_id = int(data[4:])
            await show_category_services(query, category_id)
        elif data.startswith("service_"):
            service_id = int(data[8:])
            await show_service_detail(query, service_id, context)
        elif data.startswith("page_top_"):
            page = int(data[9:])
            await show_top_services(query, page)
        elif data.startswith("page_all_"):
            page = int(data[9:])
            await show_all_services(query, page)
        elif data.startswith("page_cat_"):
            parts = data.split("_")
            page = int(parts[2])
            category_id = int(parts[3])
            await show_category_services(query, category_id, page)
        elif data.startswith("request_"):
            service_id = int(data[8:])
            await handle_request(query, service_id, context)
        elif data == "back_to_menu":
            await back_to_menu(query, context)
        elif data == "pay_now":
            await handle_payment(query, context)
        elif data == "payment_done":
            await handle_payment_done(query, context)
        elif data.startswith("prev_doc_"):
            doc_index = int(data[9:])
            await go_to_previous_document(query, doc_index, context)
        elif data == "start_over":
            await start_over(query, context)
        elif data == "dummy":
            pass
        elif data.startswith("search_more"):
            await show_search_more(query, context)
        elif data == "retry_document":
            await retry_document(query, context)
    except Exception as e:
        logger.error(f"Error in button_callback: {e}")
        logger.error(traceback.format_exc())
        try:
            await update.callback_query.edit_message_text("❌ خطا در پردازش درخواست. لطفا مجددا تلاش کنید.")
        except:
            pass

@handle_errors
async def show_categories_page(query, page: int = 0):
    """Show a specific page of categories"""
    try:
        sorted_cats = get_active_categories(SERVICES)
        total_categories = len(sorted_cats)
        page_size = 10
        total_pages = (total_categories + page_size - 1) // page_size
        
        if page >= total_pages:
            page = total_pages - 1
        
        categories_page = paginate_categories(sorted_cats, page, page_size)
        
        text = f"📂 *دسته‌بندی خدمات* (صفحه {page + 1} از {total_pages})\n\n"
        text += "لطفا یک دسته را انتخاب کنید:\n\n"
        
        keyboard = []
        row = []
        for cat_id in categories_page:
            cat_name = get_category_name(cat_id)
            row.append(InlineKeyboardButton(
                f"📂 {cat_name}", 
                callback_data=f"cat_{cat_id}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        # Pagination buttons
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"categories_page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("صفحه بعد ▶️", callback_data=f"categories_page_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        if total_pages > 1:
            keyboard.append([InlineKeyboardButton(f"📄 صفحه {page+1} از {total_pages}", callback_data="dummy")])
        
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in show_categories_page: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش دسته‌بندی‌ها.")

@handle_errors
async def show_top_services(query, page: int = 0):
    """Show top 10 services with pagination"""
    try:
        top_services = get_top_services(SERVICES)
        total_items = len(top_services)
        page_size = 10
        
        if total_items == 0:
            await query.edit_message_text("هیچ سرویس پرکاربردی یافت نشد.")
            return
        
        services_page = paginate_items(top_services, page, page_size)
        total_pages = (total_items + page_size - 1) // page_size
        
        text = "⭐ *خدمات پر کاربرد*\n\n"
        for service in services_page:
            price = format_price(service.get("Price", 0))
            categories = "، ".join(get_service_categories(service))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n"
            text += f"  📂 {categories}\n\n"
        
        keyboard = []
        for service in services_page:
            price = format_price(service.get("Price", 0))
            button_text = f"📋 {service['Title'][:35]}"
            if len(service['Title']) > 35:
                button_text += "..."
            button_text += f" - {price}ت"
            keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"service_{service['Id']}")
            ])
        
        # Pagination buttons
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"page_top_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("صفحه بعد ▶️", callback_data=f"page_top_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        if total_pages > 1:
            keyboard.append([InlineKeyboardButton(f"📄 صفحه {page+1} از {total_pages}", callback_data="dummy")])
        
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Error in show_top_services: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش خدمات پر کاربرد.")

@handle_errors
async def show_all_services(query, page: int = 0):
    """Show all active services with pagination"""
    try:
        active_services = [s for s in SERVICES if s.get("IsActive", False)]
        active_services.sort(key=lambda x: x.get("Sort", 999))
        
        total_items = len(active_services)
        page_size = 10
        
        if total_items == 0:
            await query.edit_message_text("هیچ سرویسی یافت نشد.")
            return
        
        services_page = paginate_items(active_services, page, page_size)
        total_pages = (total_items + page_size - 1) // page_size
        
        text = f"📊 *همه خدمات* (صفحه {page+1} از {total_pages})\n\n"
        for service in services_page:
            price = format_price(service.get("Price", 0))
            categories = "، ".join(get_service_categories(service))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n"
            text += f"  📂 {categories}\n\n"
        
        keyboard = []
        for service in services_page:
            price = format_price(service.get("Price", 0))
            button_text = f"📋 {service['Title'][:35]}"
            if len(service['Title']) > 35:
                button_text += "..."
            button_text += f" - {price}ت"
            keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"service_{service['Id']}")
            ])
        
        # Pagination buttons
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"page_all_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("صفحه بعد ▶️", callback_data=f"page_all_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        if total_pages > 1:
            keyboard.append([InlineKeyboardButton(f"📄 صفحه {page+1} از {total_pages}", callback_data="dummy")])
        
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in show_all_services: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش همه خدمات.")

@handle_errors
async def show_category_services(query, category_id: int, page: int = 0):
    """Show services for a specific category with pagination"""
    try:
        category_services = get_category_services(category_id, SERVICES)
        category_name = get_category_name(category_id)
        
        if not category_services:
            await query.edit_message_text(f"هیچ سرویسی در دسته '{category_name}' یافت نشد.")
            return
        
        total_items = len(category_services)
        page_size = 10
        services_page = paginate_items(category_services, page, page_size)
        total_pages = (total_items + page_size - 1) // page_size
        
        text = f"📂 *{category_name}* (صفحه {page+1} از {total_pages})\n\n"
        for service in services_page:
            price = format_price(service.get("Price", 0))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n\n"
        
        keyboard = []
        for service in services_page:
            price = format_price(service.get("Price", 0))
            button_text = f"📋 {service['Title'][:35]}"
            if len(service['Title']) > 35:
                button_text += "..."
            button_text += f" - {price}ت"
            keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"service_{service['Id']}")
            ])
        
        # Pagination buttons
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"page_cat_{page-1}_{category_id}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("صفحه بعد ▶️", callback_data=f"page_cat_{page+1}_{category_id}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        if total_pages > 1:
            keyboard.append([InlineKeyboardButton(f"📄 صفحه {page+1} از {total_pages}", callback_data="dummy")])
        
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به دسته‌بندی‌ها", callback_data="categories_page_0")])
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in show_category_services: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش خدمات دسته.")

@handle_errors
async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle search input from user"""
    try:
        if context.user_data.get('collecting_docs', False):
            return
            
        search_term = update.message.text.strip()
        
        if not search_term:
            return
        
        results = []
        for service in SERVICES:
            if not service.get("IsActive", False):
                continue
            if search_term.lower() in service.get("Title", "").lower():
                results.append(service)
            else:
                # Search in category names
                categories = get_service_categories(service)
                for cat in categories:
                    if search_term.lower() in cat.lower():
                        results.append(service)
                        break
        
        if not results:
            await update.message.reply_text(
                f"❌ هیچ نتیجه‌ای برای '{search_term}' یافت نشد.\n"
                "لطفا عبارت دیگری را جستجو کنید یا از دکمه‌ها استفاده کنید."
            )
            return
        
        context.user_data['search_results'] = results
        context.user_data['search_term'] = search_term
        
        text = f"🔍 *نتایج جستجو برای '{search_term}':*\n\n"
        for service in results[:10]:
            price = format_price(service.get("Price", 0))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n\n"
        
        keyboard = []
        for service in results[:10]:
            price = format_price(service.get("Price", 0))
            button_text = f"📋 {service['Title'][:35]}"
            if len(service['Title']) > 35:
                button_text += "..."
            button_text += f" - {price}ت"
            keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"service_{service['Id']}")
            ])
        
        if len(results) > 10:
            keyboard.append([
                InlineKeyboardButton("▶️ مشاهده بیشتر", callback_data="search_more")
            ])
        
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in handle_search: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ خطا در جستجو. لطفا مجددا تلاش کنید.")

@handle_errors
async def show_search_more(query, context: ContextTypes.DEFAULT_TYPE):
    """Show more search results"""
    try:
        results = context.user_data.get('search_results', [])
        search_term = context.user_data.get('search_term', '')
        
        if not results:
            await query.edit_message_text("❌ نتیجه‌ای برای نمایش وجود ندارد.")
            return
        
        # Get current page from callback data
        data = query.data
        if data.startswith("search_more"):
            parts = data.split("_")
            page = int(parts[2]) if len(parts) > 2 else 1
        else:
            page = 1
        
        start_idx = page * 10
        end_idx = start_idx + 10
        
        text = f"🔍 *نتایج بیشتر برای '{search_term}':*\n\n"
        for service in results[start_idx:end_idx]:
            price = format_price(service.get("Price", 0))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n\n"
        
        keyboard = []
        for service in results[start_idx:end_idx]:
            price = format_price(service.get("Price", 0))
            button_text = f"📋 {service['Title'][:35]}"
            if len(service['Title']) > 35:
                button_text += "..."
            button_text += f" - {price}ت"
            keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"service_{service['Id']}")
            ])
        
        if len(results) > end_idx:
            keyboard.append([
                InlineKeyboardButton("▶️ مشاهده بیشتر", callback_data=f"search_more_{page+1}")
            ])
        
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in show_search_more: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش نتایج بیشتر.")

@handle_errors
async def show_service_detail(query, service_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed information about a service"""
    try:
        service = next((s for s in SERVICES if s.get("Id") == service_id), None)
        
        if not service:
            await query.edit_message_text("❌ سرویس مورد نظر یافت نشد.")
            return
        
        price = format_price(service.get("Price", 0))
        discount = service.get("Discount", 0)
        discount_price = format_price(discount)
        categories = "، ".join(get_service_categories(service))
        
        text = f"📋 *{service['Title']}*\n\n"
        text += f"💰 قیمت: {price} تومان\n"
        if discount > 0:
            text += f"🎯 تخفیف: {discount_price} تومان\n"
        text += f"📂 دسته‌بندی: {categories}\n"
        text += f"📄 تعداد مدارک: {len(service.get('Documents', []))}\n"
        text += f"🔗 [مشاهده در سایت]({service.get('URL', '#')})\n\n"
        
        docs = get_documents_with_types(service)
        text += "*مدارک مورد نیاز:*\n"
        for idx, doc in enumerate(docs, 1):
            doc_type = doc.get('type', 'Text')
            text += f"{idx}. {doc['title']} ({doc_type})\n"
        
        keyboard = [
            [InlineKeyboardButton("📝 ثبت درخواست این سرویس", callback_data=f"request_{service_id}")],
            [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Error in show_service_detail: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش جزئیات سرویس. لطفا مجددا تلاش کنید.")

@handle_errors
async def back_to_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to main menu"""
    try:
        context.user_data.clear()
        
        user = query.from_user
        welcome_text = (
            f"به ربات کافی نت خوش آمدید {user.first_name}! 👋\n"
            "چه کمکی می توان به شما بکنم؟\n\n"
            "🔹 با کلیک روی هر دکمه می‌توانید خدمات مربوطه را مشاهده کنید.\n"
            "🔹 همچنین می‌توانید عبارت مورد نظر را تایپ کرده و جستجو کنید.\n"
            "🔹 از منوی زیر نیز می‌توانید استفاده کنید."
        )
        
        sorted_cats = get_active_categories(SERVICES)
        total_categories = len(sorted_cats)
        
        keyboard = [
            [InlineKeyboardButton("⭐ خدمات پر کاربرد", callback_data="top_services")]
        ]
        
        row = []
        for cat_id in sorted_cats[:10]:
            cat_name = get_category_name(cat_id)
            row.append(InlineKeyboardButton(f"📂 {cat_name}", callback_data=f"cat_{cat_id}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        if total_categories > 10:
            keyboard.append([
                InlineKeyboardButton("📂 بیشتر...", callback_data="categories_page_1")
            ])
        
        keyboard.append([InlineKeyboardButton("📊 همه خدمات", callback_data="all_services")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            welcome_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in back_to_menu: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در بازگشت به منو. لطفا /start را بزنید.")

@handle_errors
async def handle_request(query, service_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle service request initiation"""
    try:
        await query.answer()
        
        service = next((s for s in SERVICES if s.get("Id") == service_id), None)
        
        if not service:
            await query.edit_message_text("❌ سرویس مورد نظر یافت نشد.")
            return
        
        context.user_data['current_service'] = service
        context.user_data['current_service_id'] = service_id
        context.user_data['documents_collected'] = {}
        context.user_data['doc_index'] = 0
        context.user_data['collecting_docs'] = True
        context.user_data['awaiting_image'] = False
        
        docs = get_documents_with_types(service)
        if len(docs) == 1 and docs[0]['title'] == "نیازی به مدارک نیست":
            await query.edit_message_text(
                f"✅ این سرویس نیازی به مدارک ندارد.\n"
                f"💰 مبلغ قابل پرداخت: {format_price(service.get('Price', 0))} تومان\n\n"
                "لطفا تصویر رسید پرداخت را ارسال کنید."
            )
            context.user_data['awaiting_payment'] = True
            context.user_data['collecting_docs'] = False
            return
        
        await collect_next_document(query, 0, context)
    except Exception as e:
        logger.error(f"Error in handle_request: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در ثبت درخواست. لطفا مجددا تلاش کنید.")

@handle_errors
async def collect_next_document(query, doc_index: int, context: ContextTypes.DEFAULT_TYPE):
    """Collect documents one by one"""
    try:
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        docs = get_documents_with_types(service)
        
        if doc_index >= len(docs):
            await show_document_summary_and_payment(query, context)
            return
        
        current_doc = docs[doc_index]
        total = len(docs)
        doc_type = current_doc.get('type', 'Text')
        
        # Store document info for validation
        context.user_data['current_doc_info'] = current_doc
        context.user_data['doc_index'] = doc_index
        context.user_data['doc_label'] = current_doc['title']
        
        # Check if this is an image document
        if is_image_document(current_doc):
            context.user_data['awaiting_image'] = True
            context.user_data['collecting_docs'] = False  # Temporarily disable text input
        else:
            context.user_data['awaiting_image'] = False
            context.user_data['collecting_docs'] = True
        
        previous_value = context.user_data['documents_collected'].get(current_doc['title'], "")
        
        # Create type-specific instructions
        type_instructions = {
            "Text": "📝 لطفا متن مورد نظر را وارد کنید.",
            "Number": "🔢 لطفا فقط عدد وارد کنید.",
            "Sheba": "🏦 شماره شبا باید با IR شروع شود و ۲۴ رقم داشته باشد.\nمثال: IR123456789012345678901234",
            "Mobile": "📱 شماره موبایل باید ۱۱ رقم و با ۰۹ شروع شود.\nمثال: 09123456789",
            "NationalCode": "🆔 کد ملی باید ۱۰ رقم باشد.\nمثال: 1234567890",
            "PostalCode": "📮 کد پستی باید ۱۰ رقم باشد.\nمثال: 1234567890",
            "Date": "📅 تاریخ باید به فرمت YYYY-MM-DD باشد.\nمثال: 1403-01-15",
            "Image": "🖼️ لطفا یک تصویر ارسال کنید.\n📸 می‌توانید از گالری یا دوربین عکس ارسال کنید."
        }
        
        instruction = type_instructions.get(doc_type, "📝 لطفا اطلاعات مورد نظر را وارد کنید.")
        
        text = f"📄 *مدرک {doc_index + 1} از {total}*\n\n"
        text += f"🔹 {current_doc['title']}\n"
        text += f"📌 نوع: {doc_type}\n\n"
        text += f"{instruction}\n\n"
        if previous_value:
            text += f"📌 مقدار قبلی: {previous_value}\n\n"
        if doc_type != "Image":
            text += "❗️ لطفا دقیق و کامل وارد کنید."
        
        keyboard = []
        if doc_index > 0:
            keyboard.append([InlineKeyboardButton("🔙 مرحله قبل", callback_data=f"prev_doc_{doc_index}")])
        keyboard.append([InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in collect_next_document: {e}")
        logger.error(traceback.format_exc())

@handle_errors
async def go_to_previous_document(query, doc_index: int, context: ContextTypes.DEFAULT_TYPE):
    """Go to previous document to edit"""
    try:
        await query.answer()
        
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        # Go to previous document (doc_index - 1)
        prev_index = doc_index - 1
        if prev_index < 0:
            await query.edit_message_text("❌ شما در اولین مرحله هستید.")
            return
        
        docs = get_documents_with_types(service)
        if prev_index >= len(docs):
            await query.edit_message_text("❌ خطا در بازگشت به مرحله قبل.")
            return
        
        # Remove the current document's value from collected docs
        current_label = docs[doc_index]['title']
        if current_label in context.user_data['documents_collected']:
            del context.user_data['documents_collected'][current_label]
        
        # Clear validation error state
        context.user_data.pop('validation_error', None)
        context.user_data['awaiting_image'] = False
        
        # Go to the previous document
        await collect_next_document(query, prev_index, context)
    except Exception as e:
        logger.error(f"Error in go_to_previous_document: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در بازگشت به مرحله قبل.")

@handle_errors
async def retry_document(query, context: ContextTypes.DEFAULT_TYPE):
    """Retry the current document after validation error"""
    try:
        await query.answer()
        
        doc_index = context.user_data.get('doc_index', 0)
        service = context.user_data.get('current_service')
        
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        # Clear the validation error state
        context.user_data.pop('validation_error', None)
        
        # Go back to the same document
        await collect_next_document(query, doc_index, context)
    except Exception as e:
        logger.error(f"Error in retry_document: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در بازگشت به مرحله قبل.")

@handle_errors
async def show_document_summary_and_payment(query, context: ContextTypes.DEFAULT_TYPE):
    """Show document summary and proceed to payment"""
    try:
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        summary = "📋 *خلاصه مدارک*\n\n"
        for key, value in context.user_data['documents_collected'].items():
            summary += f"🔹 {key}\n   {value}\n\n"
        
        if not context.user_data['documents_collected']:
            summary += "⚠️ هیچ مدرکی وارد نشده است.\n\n"
        
        summary += f"💰 *مبلغ قابل پرداخت: {format_price(service.get('Price', 0))} تومان*\n\n"
        summary += "✅ آیا اطلاعات وارد شده صحیح است؟"
        
        docs = get_documents_with_types(service)
        last_index = len(docs) - 1
        
        keyboard = [
            [InlineKeyboardButton("✅ بله، صحیح است - پرداخت", callback_data="pay_now")],
            [InlineKeyboardButton("🔙 بازگشت به مرحله قبل", callback_data=f"prev_doc_{last_index}")],
            [InlineKeyboardButton("🔄 شروع مجدد", callback_data="start_over")],
            [InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            summary,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        context.user_data['collecting_docs'] = False
        context.user_data['awaiting_payment'] = True
        context.user_data['awaiting_image'] = False
    except Exception as e:
        logger.error(f"Error in show_document_summary_and_payment: {e}")
        logger.error(traceback.format_exc())

@handle_errors
async def start_over(query, context: ContextTypes.DEFAULT_TYPE):
    """Start over the document collection process"""
    try:
        await query.answer()
        
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        context.user_data['documents_collected'] = {}
        context.user_data['doc_index'] = 0
        context.user_data['collecting_docs'] = True
        context.user_data['awaiting_image'] = False
        context.user_data.pop('validation_error', None)
        
        await collect_next_document(query, 0, context)
    except Exception as e:
        logger.error(f"Error in start_over: {e}")
        logger.error(traceback.format_exc())

@handle_errors
async def handle_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document input from user with validation"""
    try:
        # Check if we're waiting for an image
        if context.user_data.get('awaiting_image', False):
            # User sent text but we're expecting an image
            doc_info = context.user_data.get('current_doc_info', {})
            doc_label = context.user_data.get('doc_label', '')
            doc_index = context.user_data.get('doc_index', 0)
            
            error_text = f"❌ *خطا در نوع مدرک*\n\n"
            error_text += f"این مدرک از نوع **تصویر** است.\n"
            error_text += f"🔹 *{doc_label}*\n\n"
            error_text += "📸 لطفا یک تصویر ارسال کنید.\n"
            error_text += "🔹 از دکمه ضمیمه (📎) برای ارسال عکس استفاده کنید.\n"
            error_text += "🔹 می‌توانید از گالری یا دوربین عکس ارسال کنید."
            
            keyboard = [
                [InlineKeyboardButton("🔙 مرحله قبل", callback_data=f"prev_doc_{doc_index}")],
                [InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                error_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        doc_value = update.message.text.strip()
        doc_label = context.user_data.get('doc_label', '')
        doc_index = context.user_data.get('doc_index', 0)
        doc_info = context.user_data.get('current_doc_info', {})
        
        # If not collecting docs, treat as search
        if not context.user_data.get('collecting_docs', False) and doc_value:
            await handle_search(update, context)
            return
        
        # If we're not collecting docs or no doc_label, ignore
        if not context.user_data.get('collecting_docs', False) or not doc_label:
            return
        
        # Validate the document
        is_valid, result = validate_document(doc_value, doc_info)
        
        if not is_valid:
            # Store the error and show it to the user
            context.user_data['validation_error'] = result
            
            # Show error and let user try again
            error_text = f"❌ *خطا در اعتبارسنجی*\n\n"
            error_text += result
            error_text += f"\n\n🔹 *{doc_label}*\n"
            error_text += f"📌 مقدار وارد شده: `{doc_value}`\n\n"
            error_text += "لطفا مجددا تلاش کنید و اطلاعات را به درستی وارد کنید."
            
            keyboard = [
                [InlineKeyboardButton("🔄 تلاش مجدد", callback_data="retry_document")],
                [InlineKeyboardButton("🔙 مرحله قبل", callback_data=f"prev_doc_{doc_index}")],
                [InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                error_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Valid input - store it
        if 'documents_collected' not in context.user_data:
            context.user_data['documents_collected'] = {}
        context.user_data['documents_collected'][doc_label] = result
        
        # Clear validation error state
        context.user_data.pop('validation_error', None)
        
        service = context.user_data.get('current_service')
        docs = get_documents_with_types(service)
        
        next_index = doc_index + 1
        
        class MockQuery:
            def __init__(self, message, from_user):
                self.message = message
                self.from_user = from_user
                self.data = None
            
            async def edit_message_text(self, text, reply_markup=None, parse_mode=None, disable_web_page_preview=None):
                await self.message.reply_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview
                )
            
            async def answer(self):
                pass
        
        mock_query = MockQuery(update.message, update.effective_user)
        
        if next_index >= len(docs):
            await show_document_summary_and_payment(mock_query, context)
        else:
            await collect_next_document(mock_query, next_index, context)
    except Exception as e:
        logger.error(f"Error in handle_document_input: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ خطا در دریافت اطلاعات. لطفا مجددا تلاش کنید.")

@handle_errors
async def handle_image_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image upload for document collection"""
    try:
        # Check if we're waiting for an image
        if not context.user_data.get('awaiting_image', False):
            # User sent an image but we're not expecting one
            if context.user_data.get('collecting_docs', False):
                # If we're collecting docs but not waiting for image, inform the user
                doc_info = context.user_data.get('current_doc_info', {})
                doc_label = context.user_data.get('doc_label', '')
                doc_type = doc_info.get('type', 'Text')
                
                if doc_type != "Image":
                    await update.message.reply_text(
                        f"❌ *نوع مدرک اشتباه است*\n\n"
                        f"🔹 *{doc_label}*\n"
                        f"📌 این مدرک از نوع **{doc_type}** است.\n\n"
                        f"📝 لطفا متن مورد نظر را وارد کنید، نه تصویر."
                    )
                return
            return
        
        # Get the photo
        photo = update.message.photo
        if not photo:
            await update.message.reply_text(
                "❌ لطفا یک تصویر ارسال کنید.\n"
                "📸 از دکمه ضمیمه (📎) برای ارسال عکس استفاده کنید."
            )
            return
        
        doc_label = context.user_data.get('doc_label', '')
        doc_index = context.user_data.get('doc_index', 0)
        
        # Get the photo file ID
        photo_file_id = photo[-1].file_id
        
        # Store the image as file_id
        if 'documents_collected' not in context.user_data:
            context.user_data['documents_collected'] = {}
        context.user_data['documents_collected'][doc_label] = f"🖼️ تصویر (File ID: {photo_file_id})"
        
        # Clear awaiting_image flag
        context.user_data['awaiting_image'] = False
        context.user_data['collecting_docs'] = True
        
        # Confirm receipt
        await update.message.reply_text(
            f"✅ تصویر برای مدرک '{doc_label}' دریافت شد.\n"
            "📸 تصویر با موفقیت ثبت شد."
        )
        
        service = context.user_data.get('current_service')
        docs = get_documents_with_types(service)
        
        next_index = doc_index + 1
        
        class MockQuery:
            def __init__(self, message, from_user):
                self.message = message
                self.from_user = from_user
                self.data = None
            
            async def edit_message_text(self, text, reply_markup=None, parse_mode=None, disable_web_page_preview=None):
                await self.message.reply_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview
                )
            
            async def answer(self):
                pass
        
        mock_query = MockQuery(update.message, update.effective_user)
        
        if next_index >= len(docs):
            await show_document_summary_and_payment(mock_query, context)
        else:
            await collect_next_document(mock_query, next_index, context)
    except Exception as e:
        logger.error(f"Error in handle_image_input: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ خطا در دریافت تصویر. لطفا مجددا تلاش کنید.")

@handle_errors
async def handle_payment(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle payment processing"""
    try:
        await query.answer()
        
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        text = f"💰 *پرداخت*\n\n"
        text += f"سرویس: {service.get('Title', '')}\n"
        text += f"مبلغ: {format_price(service.get('Price', 0))} تومان\n\n"
        text += "✅ لطفا مبلغ را به شماره کارت زیر واریز کنید:\n"
        text += "🔹 شماره کارت: `6037-9912-3456-7890`\n"
        text += "🔹 به نام: کافی نت آنلاین\n\n"
        text += "❗️ پس از پرداخت، تصویر رسید را ارسال کنید."
        
        keyboard = [
            [InlineKeyboardButton("✅ پرداخت انجام شد", callback_data="payment_done")],
            [InlineKeyboardButton("🔙 بازگشت به خلاصه مدارک", callback_data="start_over")],
            [InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in handle_payment: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در پردازش پرداخت. لطفا مجددا تلاش کنید.")

@handle_errors
async def handle_payment_done(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle payment confirmation"""
    try:
        await query.answer()
        
        await query.edit_message_text(
            "✅ لطفا تصویر رسید پرداخت را ارسال کنید.\n"
            "📸 عکس یا اسکرین‌شات از رسید پرداخت را بفرستید."
        )
        context.user_data['awaiting_payment_receipt'] = True
        context.user_data['collecting_docs'] = False
    except Exception as e:
        logger.error(f"Error in handle_payment_done: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در تایید پرداخت. لطفا مجددا تلاش کنید.")

@handle_errors
async def handle_payment_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle payment receipt image"""
    try:
        if not context.user_data.get('awaiting_payment_receipt', False):
            return
        
        photo = update.message.photo
        if not photo:
            await update.message.reply_text(
                "❌ لطفا یک تصویر از رسید پرداخت ارسال کنید."
            )
            return
        
        await update.message.reply_text(
            "✅ *رسید پرداخت شما دریافت شد!*\n\n"
            "🎉 درخواست شما با موفقیت ثبت شد.\n"
            "📞 به زودی کارشناسان ما با شما تماس خواهند گرفت.\n\n"
            "🙏 از اعتماد شما سپاسگزاریم!"
        )
        
        # Show button to return to main page
        keyboard = [
            [InlineKeyboardButton("🏠 بازگشت به صفحه اصلی", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🔹 برای مشاهده سایر خدمات، روی دکمه زیر کلیک کنید:",
            reply_markup=reply_markup
        )
        
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Error in handle_payment_receipt: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ خطا در دریافت رسید. لطفا مجددا تلاش کنید.")

@handle_errors
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo upload"""
    try:
        # Check if we're waiting for a payment receipt
        if context.user_data.get('awaiting_payment_receipt', False):
            await handle_payment_receipt(update, context)
            return
        
        # Check if we're waiting for an image document
        if context.user_data.get('awaiting_image', False):
            await handle_image_input(update, context)
            return
        
        # If we're collecting documents but not waiting for an image
        if context.user_data.get('collecting_docs', False):
            doc_info = context.user_data.get('current_doc_info', {})
            doc_label = context.user_data.get('doc_label', '')
            doc_type = doc_info.get('type', 'Text')
            
            if doc_type != "Image":
                await update.message.reply_text(
                    f"❌ *نوع مدرک اشتباه است*\n\n"
                    f"🔹 *{doc_label}*\n"
                    f"📌 این مدرک از نوع **{doc_type}** است.\n\n"
                    f"📝 لطفا متن مورد نظر را وارد کنید، نه تصویر."
                )
    except Exception as e:
        logger.error(f"Error in handle_photo: {e}")
        logger.error(traceback.format_exc())

@handle_errors
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel current operation"""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ عملیات لغو شد.\n"
        "برای شروع مجدد /start را بزنید."
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log and handle errors - catches errors from the polling loop"""
    logger.error(f"Unhandled error: {context.error}")
    logger.error(traceback.format_exc())
    
    # Don't try to send message if it's a network error
    if isinstance(context.error, (NetworkError, TimedOut, ConnectionError)):
        logger.warning("Network error detected, will retry connection")
        return
    
    # Don't try to send message if update is None or doesn't have effective_message
    if update is None or not hasattr(update, 'effective_message') or update.effective_message is None:
        logger.warning("Cannot send error message - update is None or missing effective_message")
        return
    
    try:
        await update.effective_message.reply_text(
            "❌ خطایی رخ داده است. لطفا مجددا تلاش کنید یا /start را بزنید."
        )
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")

def run_bot():
    """Run the bot with infinite retry on connection errors"""
    max_retries = 5
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Starting bot (attempt {attempt + 1}/{max_retries})...")
            
            application = Application.builder().token(BOT_TOKEN).build()
            
            # Set menu button
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(set_menu_button(application))
            
            # Add error handler
            application.add_error_handler(error_handler)
            
            # Add handlers
            application.add_handler(CommandHandler("start", start))
            # application.add_handler(CommandHandler("menu", menu_command))
            application.add_handler(CommandHandler("top", top_command))
            # application.add_handler(CommandHandler("cancel", cancel))
            application.add_handler(CallbackQueryHandler(button_callback))
            
            application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_document_input))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
            
            # Start polling with error handling
            logger.info("Bot is running...")
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                stop_signals=None,
            )
            
            # If we get here, polling stopped normally
            logger.info("Bot stopped normally.")
            break
            
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as e:
            logger.error(f"Error in run_bot (attempt {attempt + 1}): {e}")
            logger.error(traceback.format_exc())
            
            if attempt < max_retries - 1:
                wait_time = retry_delay * (attempt + 1)
                logger.info(f"Restarting bot in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                logger.error("Max retries reached. Bot will exit.")
                sys.exit(1)

def main():
    """Main entry point with infinite loop"""
    while True:
        try:
            run_bot()
            # If run_bot exits normally, break
            break
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as e:
            logger.error(f"Fatal error in main: {e}")
            logger.error(traceback.format_exc())
            logger.info("Restarting in 10 seconds...")
            time.sleep(10)

if __name__ == "__main__":
    main()