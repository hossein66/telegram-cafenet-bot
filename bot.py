# bot.py
import json
import logging
import os
import sys
import traceback
import re
import asyncio
import time
from typing import List, Dict, Optional, Any, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, CopyTextButton
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
import base64
import httpx
from api_client import API_BASE, COFENET_Server_URL, data_store
import random
# try:
#     from PIL import Image
#     import io
#     PIL_AVAILABLE = True
# except ImportError:
#     PIL_AVAILABLE = False
#     logging.warning("Pillow not installed, image compression disabled")
# Import config
try:
    from config import BOT_TOKEN
except ImportError:
    BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "No bot token found. Create a config.py with BOT_TOKEN = '...' "
        "(and keep it out of version control), or set the BOT_TOKEN "
        "environment variable."
    )

# How often the bot refreshes its cached data from the API, in seconds.
# Each resource (categories/services/doc-types) has its own TTL inside
# api_client.py, so calling refresh_all() this often is cheap - it's a
# no-op for anything that isn't due yet.
REFRESH_INTERVAL_SECONDS = int(os.environ.get("COFENET_REFRESH_INTERVAL", "86400"))  # default: 24 hours

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────
#  LIVE DATA (fetched from the Cafenet Online API, cached client-side)
# ─────────────────────────────────────────────────────────────
# These names are kept so the rest of the bot's logic doesn't need to
# change - they're bound to the SAME list/dict objects that data_store
# mutates in place on every refresh, so they always reflect the latest
# cached data without needing to be reassigned.
CATEGORIES = data_store.categories
SERVICES = data_store.services
DOC_TYPES = data_store.doc_types
CATEGORY_NAMES = data_store.category_names
CATEGORY_SORT = data_store.category_sort
DOC_TYPE_MAP = data_store.doc_type_map

# Helper functions
def format_price(price: int) -> str:
    """Format price with Persian commas"""
    try:
        return f"{price:,}" #.replace(",", "٬")
    except:
        return str(price)

def get_category_name(category_id: int) -> str:
    """Get category name from ID"""
    return CATEGORY_NAMES.get(category_id, f"دسته {category_id}")

def get_category_icon(category_id: int) -> str:
    """Get category icon from ID, fallback to default if not available"""
    try:
        for cat in CATEGORIES:
            if cat.get("id") == category_id:
                icon = cat.get("icon")
                if icon:
                    return icon
        return "📂"  # fallback icon
    except:
        return "📂"

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

async def get_documents_with_types(service: Dict) -> List[Dict]:
    """Get documents with their types for a service.

    The lightweight service list from /api/services/all doesn't include the
    per-service form/documents definition, so this fetches it lazily (once,
    then cached with a TTL) the first time a service is opened - mirroring
    how index.html calls buildDocsFromDetail() only when a service card is
    clicked.
    """
    try:
        docs = await data_store.get_service_documents(service)
        return docs if docs else [{"title": "نیازی به مدارک نیست", "typeId": 1, "type": "Text", "regex": ".*"}]
    except Exception as e:
        logger.error(f"Error getting documents for service {service.get('Id')}: {e}")
        return [{"title": "نیازی به مدارک نیست", "typeId": 1, "type": "Text", "regex": ".*"}]

async def get_documents_list(service: Dict) -> List[str]:
    """Parse documents from service (backward compatibility)"""
    docs = await get_documents_with_types(service)
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
    return doc_info.get("type", "") in ("Image", "file")

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

# ─────────────────────────────────────────────────────────────
#  AUTHENTICATION WITH SERVER
# ─────────────────────────────────────────────────────────────
async def authenticateUserWithServer(
    telegram_user: Any, 
    init_data: Optional[str] = None,
    context: Optional[ContextTypes.DEFAULT_TYPE] = None
) -> Optional[Dict[str, Any]]:
    """
    Authenticate user with the server using Telegram data.
    
    Called when a user interacts with the bot to validate/store their
    credentials. Returns the authenticated user object and token if successful.
    
    Args:
        telegram_user: Telegram User object or dict with id, first_name, last_name, username, etc.
        init_data: Optional initData string from Telegram Mini App
        context: Optional ContextTypes for storing in user_data
    
    Returns:
        Dict with 'user' and 'token' if successful, None if failed
    """
    try:
        # Convert Telegram User object to dict if needed
        user_dict = telegram_user.to_dict() if hasattr(telegram_user, 'to_dict') else telegram_user
        
        logger.info(f"Authenticating Telegram user: {user_dict.get('username', user_dict.get('id'))}")
        
        # Prepare the authentication payload
        auth_payload = {
            "initData": init_data or "",
            "user": user_dict
        }
        
        # Call the authentication API
        try:
            response = await data_store._get_json(
                "/api/auth/telegram-login",
                params=None  # POST request, so we'll handle it manually
            )
        except:
            # Fallback: make the request directly
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                try:
                    res = await client.post(
                        f"{API_BASE.rstrip('/')}/api/auth/telegram-login",
                        json=auth_payload,
                        headers={"Content-Type": "application/json"}
                    )
                    res.raise_for_status()
                    response = res.json()
                except Exception as e:
                    logger.error(f"Error calling telegram-login API: {e}")
                    return None
        
        # Validate response
        if not response or not response.get("success"):
            logger.warning(f"Authentication failed: {response}")
            return None
        
        auth_data = response.get("user")
        token = response.get("token")
        
        if not auth_data or not token:
            logger.warning("Missing user or token in auth response")
            return None
        
        # Store in context if provided
        if context:
            context.user_data["auth_token"] = token
            context.user_data["authenticated_user"] = auth_data
            context.user_data["telegram_id"] = user_dict.get("id")
            logger.info(f"User {auth_data.get('id')} stored in context")
        
        logger.info(f"✅ User authentication successful: {auth_data.get('username')} (ID: {auth_data.get('id')})")
        
        return {
            "user": auth_data,
            "token": token
        }
        
    except Exception as e:
        logger.error(f"Error in authenticateUserWithServer: {e}")
        logger.error(traceback.format_exc())
        return None

def get_user_token(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Get the stored authentication token for the current user"""
    return context.user_data.get("auth_token")

def get_authenticated_user(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict[str, Any]]:
    """Get the stored authenticated user info for the current user"""
    return context.user_data.get("authenticated_user")

def is_user_authenticated(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the current user is authenticated"""
    return bool(context.user_data.get("auth_token")) and bool(context.user_data.get("authenticated_user"))

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
        # Authenticate user with server and store token/user info in context
        await authenticateUserWithServer(user, context=context)
        
        welcome_text = (
            f"به ربات کافی‌نت آنلاین نت خوش آمدید {user.first_name}! 👋\n"
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
            cat_icon = get_category_icon(cat_id)
            row.append(InlineKeyboardButton(
                f"{cat_icon} {cat_name}", 
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
            InlineKeyboardButton("👨🏻 پروفایل من", callback_data="my_profile"),
            InlineKeyboardButton("👨‍💻 پشتیبانی", callback_data="support"),
            InlineKeyboardButton("📊 همه خدمات", callback_data="all_services"),
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
        elif data == "retry_submit":
            await retry_submit(query, context)    
        elif data == "my_requests":
            await show_my_requests(query, context)
        elif data == "my_profile":
            await show_my_profile(query, context)    
        elif data == "support":
            await show_support(query, context)
        elif data.startswith("categories_page_"):
            page = int(data.split("_")[2])
            await show_categories_page(query, page)
        elif data.startswith("cat_"):
            category_id = int(data[4:])
            await show_category_services(query, category_id)
        elif data.startswith("service_"):
            service_id = data[8:]
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
            service_id = data[8:]
            await handle_request(query, service_id, context)
        elif data == "copy_card":
            await copy_card_number(query, context)
        elif data == "copy_amount":
            await copy_amount(query, context)    
        elif data == "back_to_menu":
            await back_to_menu(query, context)
        elif data == "pay_now":
            await handle_payment(query, context)
        elif data == "payment_done":
            await handle_payment_done(query, context)
        elif data.startswith("prev_doc_"):
            doc_index = int(data[9:])
            await go_to_previous_document(query, doc_index, context)
        elif data.startswith("opti_doc_"):
           parts = data[len("opti_doc_"):].split('|', 1)
           if len(parts) == 2:
               doc_index = int(parts[0])
               selected_option = parts[1]
               doc_label = context.user_data.get('doc_label')
               if doc_label:
                  context.user_data['documents_collected'][doc_label] = selected_option
                  context.user_data['awaiting_option'] = False
                  service = context.user_data.get('current_service')
                  docs = await get_documents_with_types(service)
                  next_index = doc_index + 1
                  if next_index >= len(docs):
                   await show_document_summary_and_payment(query, context)
                  else:
                   await collect_next_document(query, next_index, context)
               else:
                 await query.edit_message_text("❌ خطا در شناسایی مدرک.")
           else:
              await query.edit_message_text("❌ خطا در پردازش انتخاب.")        
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

def get_request_status_label(status: str) -> str:
    """Get Persian label for request status"""
    status_labels = {
        "pending": "در انتظار بررسی",
        "processing": "در حال انجام",
        "done": "انجام شده",
        "rejected": "رد شده"
    }
    return status_labels.get(status, status)

def get_request_status_emoji(status: str) -> str:
    """Get emoji for request status"""
    status_emojis = {
        "pending": "⏳",
        "processing": "⚙️",
        "done": "✅",
        "rejected": "❌"
    }
    return status_emojis.get(status, "📋")

async def ensure_authenticated(update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ensure the user is authenticated. If not, try to re-authenticate.
    
    Supports both Update and CallbackQuery objects.
    """
    if is_user_authenticated(context):
        return True

    # Extract user from different possible objects
    user = None
    if hasattr(update, 'effective_user'):
        user = update.effective_user
    elif hasattr(update, 'from_user'):
        user = update.from_user
    elif hasattr(update, 'message') and hasattr(update.message, 'from_user'):
        user = update.message.from_user
    else:
        logger.error("Cannot extract user from update object")
        return False

    if not user:
        return False

    result = await authenticateUserWithServer(user, context=context)
    if result:
        logger.info(f"Re-authenticated user {user.id}")
        return True

    return False
@handle_errors
async def show_my_requests(query, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    try:
        await query.answer()
        
        # Ensure user is authenticated
        if context and not await ensure_authenticated(query, context):
            await query.edit_message_text(
                "❌ *خطا در احراز هویت*\n\n"
                "لطفا مجددا /start را بزنید.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        await query.edit_message_text(
            "📋 *درخواست‌های من*\n\n⏳ در حال بارگذاری...",
            parse_mode=ParseMode.MARKDOWN
        )
        
        token = get_user_token(context) if context else None
        # logger.info(f"Fetching requests for user with token: {token}")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(
                    f"{API_BASE.rstrip('/')}/api/requests",
                    headers=headers
                )
                logger.info(f"API response status: {res.status_code}, content: {res.text}")
                # If 401, try to re-authenticate once and retry
                if res.status_code == 401 and context:
                    logger.info("Token expired, re-authenticating...")
                    user = query.from_user
                    if await authenticateUserWithServer(user, context=context):
                        new_token = get_user_token(context)
                        if new_token:
                            headers["Authorization"] = f"Bearer {new_token}"
                            res = await client.get(
                                f"{API_BASE.rstrip('/')}/api/requests",
                                headers=headers
                            )
                
                res.raise_for_status()
                requests = res.json()
                requests = requests if isinstance(requests, list) else [requests] if requests else []
                logger.info(f"Fetched {len(requests)} requests for user {query.from_user.id}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    await query.edit_message_text(
                        "❌ *نشست شما منقضی شده است*\n\n"
                        "لطفا /start را بزنید تا دوباره وارد شوید.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    raise
                return
            except Exception as e:
                logger.error(f"Error fetching requests: {e}")
                raise

        # If no requests
        if not requests:
            keyboard = [
                [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "📋 *درخواست‌های من*\n\n"
                "هنوز درخواستی ندارید.\n"
                "پس از ثبت درخواست، اینجا نمایش داده می‌شود.",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Build the message
        text = "📋 *درخواست‌های من*\n\n"
        for idx, req in enumerate(requests[:10], 1):
            status = req.get("status", "pending")
            status_label = get_request_status_label(status)
            status_emoji = get_request_status_emoji(status)

            service_title = req.get("serviceTitle", "سرویس نامشناس")
            price = format_price(req.get("price", 0))

            # Format date
            submitted_date = "—"
            if req.get("submittedAt"):
                try:
                    from datetime import datetime
                    date_obj = datetime.fromisoformat(req["submittedAt"].replace("Z", "+00:00"))
                    submitted_date = date_obj.strftime("%Y-%m-%d")
                except:
                    submitted_date = "—"

            text += f"{idx}. {service_title}\n"
            text += f"   {status_emoji} وضعیت: {status_label}\n"
            text += f"   💰 مبلغ: {price} تومان\n"
            text += f"   📅 تاریخ: {submitted_date}\n\n"

        total_requests = len(requests)
        if total_requests > 10:
            text += f"\n📌 نمایش ۱ تا ۱۰ از {total_requests} درخواست"

        keyboard = [
            [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Error in show_my_requests: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text(
            "❌ خطا در نمایش درخواست‌ها. لطفا مجددا تلاش کنید."
        )


@handle_errors
async def show_support(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show support information with contact button"""
    try:
        await query.answer()
        
        text = (
            "📱 *پشتیبانی کافی نت*\n\n"
            "سلام! 👋\n"
            "ما اینجا هستیم تا کمک کنیم.\n\n"
            "برای تماس با تیم پشتیبانی:\n"
            "💬 @Cofenet_online_support\n\n"
            "لطفا سؤالات یا مشکلاتتان را بنویسید تا در کمترین زمان پاسخ داده شود."
        )
        
        keyboard = [
            [InlineKeyboardButton("💬 تماس با پشتیبانی", url="https://t.me/Cofenet_online_support")],
            [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=None
        )
    except Exception as e:
        logger.error(f"Error in show_support: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش پشتیبانی. لطفا مجددا تلاش کنید.")


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
            cat_icon = get_category_icon(cat_id)
            row.append(InlineKeyboardButton(
                f"{cat_icon} {cat_name}", 
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
            button_text = f"📋 {service['Title'][:25]}"
            if len(service['Title']) > 25:
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
            button_text = f"📋 {service['Title'][:25]}"
            if len(service['Title']) > 25:
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
            button_text = f"📋 {service['Title'][:25]}"
            if len(service['Title']) > 25:
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
            button_text = f"📋 {service['Title'][:25]}"
            if len(service['Title']) > 25:
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
            button_text = f"📋 {service['Title'][:25]}"
            if len(service['Title']) > 25:
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
async def show_service_detail(query, service_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed information about a service"""
    try:
        service = next((s for s in SERVICES if s.get("Id") == service_id), None)
        
        if not service:
            await query.edit_message_text("❌ سرویس مورد نظر یافت نشد.")
            return
        
        price = format_price(service.get("Price", 0))
        discount = service.get("Discount", 0)
        noticeText = service.get("Description", "")
        duration = service.get("Duration", "")
        discount_price = format_price(discount)
        categories = "، ".join(get_service_categories(service))
        
        docs = await get_documents_with_types(service)
        
        text = f"📋 *{service['Title']}*\n\n"
        if noticeText:
            text += f"📢  {noticeText}\n\n"
        if discount > 0:
            text += f"🎯 تخفیف: {discount_price} تومان\n"
        text += f"📂 دسته‌بندی: {categories}\n"
        text += f"💰 قیمت: {price} تومان\n"
        if discount > 0:
            text += f"🎯 تخفیف: {discount_price} تومان\n"
        text += f"📂 دسته‌بندی: {categories}\n"
        text += f"📄 تعداد مدارک: {len(docs)}\n"
        text += f"⏱️ زمان لازم: {duration} \n\n"

        text += f"🔗 [مشاهده در سایت]({COFENET_Server_URL +'?service=' + service['Id'] })\n\n"
        
        text += "*مدارک مورد نیاز:*\n"
        for idx, doc in enumerate(docs, 1):
            doc_type = doc.get('type', 'Text')
            text += f"{idx}. {doc['title']} \n"
        
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
async def show_my_profile(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user profile information and actions."""
    try:
        await query.answer()
        
        # Ensure user is authenticated
        if not await ensure_authenticated(query, context):
            await query.edit_message_text(
                "❌ *خطا در احراز هویت*\n\n"
                "لطفا مجددا /start را بزنید.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        user_data = get_authenticated_user(context)
        if not user_data:
            await query.edit_message_text(
                "❌ اطلاعات کاربر یافت نشد. لطفا مجددا /start را بزنید.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Extract and clean fields
        user_id = user_data.get('id', 'نامشخص')
        if isinstance(user_id, str) and user_id.startswith("tg_"):
            user_id = user_id[3:]  # remove "tg_" prefix
        
        first_name = user_data.get('first_name', '')
        last_name = user_data.get('last_name', '')
        username = user_data.get('username')
        phone = user_data.get('phone')
        
        # Build profile text (using plain text, no Markdown backticks)
        text = "👤 *پروفایل من*\n\n"
        text += f"🆔 شناسه: {user_id}\n"
        # text += f"👤 نام: {first_name} {last_name}\n"
        if username:
            text += f"👤 نام کاربری: @{username}\n"
        # if phone:
        #     text += f"📞 تلفن: {phone}\n"
        # else:
        # text += "📞 تلفن: تکمیل نشده\n"
        text += "\n🔹"
        
        keyboard = [
            [InlineKeyboardButton("📋 درخواست‌های من", callback_data="my_requests")],
            [InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=None
        )
    except Exception as e:
        logger.error(f"Error in show_my_profile: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش پروفایل.")    
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
            cat_icon = get_category_icon(cat_id)
            row.append(InlineKeyboardButton(f"{cat_icon} {cat_name}", callback_data=f"cat_{cat_id}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        if total_categories > 10:
            keyboard.append([
                InlineKeyboardButton("📂 بیشتر...", callback_data="categories_page_1")
            ])
        
        keyboard.append([
            InlineKeyboardButton("👨🏻 پروفایل من", callback_data="my_profile"),
            InlineKeyboardButton("👨‍💻 پشتیبانی", callback_data="support"),
            InlineKeyboardButton("📊 همه خدمات", callback_data="all_services"),
        ])
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
async def handle_request(query, service_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        
        docs = await get_documents_with_types(service)
        final_price = context.user_data.get('final_price', service.get('Price', 0))
        amount = format_price(final_price)
        if len(docs) == 1 and docs[0]['title'] == "نیازی به مدارک نیست":
            await query.edit_message_text(
                f"✅ این سرویس نیازی به مدارک ندارد.\n"
                f"💰 مبلغ قابل پرداخت: {amount} تومان\n\n"
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
def persian_to_english(s: str) -> str:
    persian_digits = '۰۱۲۳۴۵۶۷۸۹'
    english_digits = '0123456789'
    arabic_digits = '٠١٢٣٤٥٦٧٨٩'
    trans = str.maketrans(persian_digits, english_digits)
    trans_arabic = str.maketrans(arabic_digits, english_digits)
    return s.translate(trans).translate(trans_arabic)

@handle_errors
async def collect_next_document(query, doc_index: int, context: ContextTypes.DEFAULT_TYPE):
    """Collect documents one by one"""
    try:
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        docs = await get_documents_with_types(service)
        
        if doc_index >= len(docs):
            await show_document_summary_and_payment(query, context)
            return
        
        current_doc = docs[doc_index]
        total = len(docs)
        doc_type = current_doc.get('type', 'Text')
        options = current_doc.get('options', [])

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
        # text += f"📌 نوع: {doc_type}\n\n"
        # text += f"{instruction}\n\n"
        if previous_value:
            text += f"📌 مقدار قبلی: {previous_value}\n\n"
        if doc_type != "Image":
            text += f"🔹 {current_doc['placeholder']}"
        
        options = current_doc.get('options', [])

# Build keyboard
        keyboard = []
        for option in options:
           keyboard.append([InlineKeyboardButton(option, callback_data=f"opti_doc_{doc_index}|{option}")])

        if doc_index > 0:
           keyboard.append([InlineKeyboardButton("🔙 مرحله قبل", callback_data=f"prev_doc_{doc_index}")])
           keyboard.append([InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")])

# Set state flags
        if options:
             context.user_data['awaiting_option'] = True
             context.user_data['collecting_docs'] = False
             context.user_data['awaiting_image'] = False
        else:
             context.user_data['awaiting_option'] = False
        if is_image_document(current_doc):
            context.user_data['awaiting_image'] = True
            context.user_data['collecting_docs'] = False
        else:
           context.user_data['awaiting_image'] = False
           context.user_data['collecting_docs'] = True 
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
        
        docs = await get_documents_with_types(service)
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
    """Show document summary and proceed to payment, with loan-specific calculations and random fee."""
    try:
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return

        docs_collected = context.user_data.get('documents_collected', {})
        service_id = service.get('Id', '')

        # Generate or retrieve random fee
        if 'random_fee' not in context.user_data:
            context.user_data['random_fee'] = 0 #random.randint(100, 500)
        random_fee = context.user_data['random_fee']

        # Detect loan services
        is_dastadas = service_id == "svc_1784058536818"
        is_resalat  = service_id == "svc_1784058536819"
        is_mehr     = service_id == "svc_1784058536820"

        def fmt(num):
            return format_price(int(round(num)))

        # If it's a loan service, compute loan-specific summary
        if is_dastadas or is_resalat or is_mehr:
            # Extract loan amount and duration
            loan_amount_str = docs_collected.get('مقدار وام', '0')
            duration_str    = docs_collected.get('مدت وام', '0')

            try:
                loan_amount = int(loan_amount_str)
            except ValueError:
                loan_amount = 0
            try:
                refund_duration = int(duration_str)
            except ValueError:
                refund_duration = 0

            national_code = docs_collected.get('کد ملی ', '—')
            mobile = docs_collected.get('شماره موبایل', '—')
            loan_amount_rials = loan_amount * 1_000_000

            if is_dastadas:
                if refund_duration == 12:
                    ratio = 32
                elif refund_duration == 24:
                    ratio = 16
                elif refund_duration == 36:
                    ratio = 11
                elif refund_duration == 48:
                    ratio = 8
                else:
                    ratio = 320

                score = loan_amount_rials / ratio
                score_price = score * 30 / 10
                fee = score * 2.1 / 10
                instruction = f"شما می‌بایستی مبلغ {fmt(score_price)} را در حساب دستادس خود داشته باشد"

            elif is_resalat:
                score = loan_amount * refund_duration / 10
                score_price = score * 135000
                fee = score * 3000
                instruction = f"شما می‌بایستی مبلغ {fmt(score_price)} را در زمان انتقال امتیاز به حساب فروشنده واریز نمایید"

            else:  # is_mehr
                score = loan_amount
                score_price = score * 350000
                fee = score * 10000
                instruction = f"شما می‌بایستی مبلغ {fmt(score_price)} در زمان انتقال امتیاز به حساب فروشنده واریز نمایید"

            # Add random fee to total price
            total_price = int(round(fee)) + random_fee
            context.user_data['final_price'] = total_price

            summary_lines = [
                "📋 *خلاصه مدارک و محاسبات وام*",
                "",
                f"🔹 **مبلغ وام**: {fmt(loan_amount_rials)} تومان",
                f"🔹 **مدت وام (ماه)**: {refund_duration}",
                f"🔹 **مقدار امتیاز مورد نیاز**: {fmt(score)}",
                f"🔹 **قیمت امتیاز**: {fmt(score_price)} تومان",
                f"🔹 **کارمزد**: {fmt(fee)} تومان",
                f"🔹 **کد ملی**: {national_code}",
                f"🔹 **شماره موبایل**: {mobile}",
                "",
                f"💰 *مبلغ قابل پرداخت : {fmt(total_price)} تومان*",
                "",
                instruction,
                "",
                "✅ آیا اطلاعات وارد شده صحیح است؟"
            ]
            summary = "\n".join(summary_lines)

        else:
            # Generic summary for non‑loan services
            base_price = service.get('Price', 0)
            total_price = base_price + random_fee
            context.user_data['final_price'] = total_price
            summary = "📋 *خلاصه مدارک*\n\n"
            for key, value in docs_collected.items():
                if "File" in value:
                    value = " فایل یا تصویر دریافت شد. "
                summary += f"🔹 {key}\n   {value}\n\n"

            if not docs_collected:
                summary += "⚠️ هیچ مدرکی وارد نشده است.\n\n"

            summary += f"💰 *مبلغ قابل پرداخت: {fmt(total_price)} تومان*\n\n"
            summary += "✅ آیا اطلاعات وارد شده صحیح است؟"

        # Get the list of documents for navigation
        docs = await get_documents_with_types(service)
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
        await query.edit_message_text("❌ خطا در نمایش خلاصه مدارک. لطفا مجددا تلاش کنید.")
             
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
                # 1. Authenticate the user to get internal user_id
        if not await ensure_authenticated(update, context):
            await update.message.reply_text("❌ لطفاً /start را بزنید.")
            return
        
        user_data = get_authenticated_user(context)
        internal_user_id = user_data.get("id") if user_data else None
        if not internal_user_id:
            await update.message.reply_text("❌ خطا در شناسایی کاربر.")
            return

        # 2. Check if this is an SMS code (5 digits and not in document collection)
        if (not context.user_data.get('collecting_docs', False) and 
            update.message.text and update.message.text.isdigit() and 
            len(update.message.text) == 5):
            await handle_cache_sms_code(update, context, internal_user_id)
            return
                # Check if we are waiting for an option selection
        if context.user_data.get('awaiting_option', False):
            await update.message.reply_text(
                "❌ لطفاً از دکمه‌های زیر برای انتخاب گزینه استفاده کنید."
            )
            return
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
        doc_value = persian_to_english(doc_value) 
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
        docs = await get_documents_with_types(service)
        
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

async def handle_cache_sms_code(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str) -> None:
    """Send the 5‑digit code to the API for caching."""
    code = update.message.text.strip()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{API_BASE.rstrip('/')}/api/sms/set-code",
                json={"user_id": user_id, "code": code}
            )
            resp.raise_for_status()
        await update.message.reply_text("✅ کد با موفقیت دریافت شد.")
    except Exception as e:
        logger.error(f"Error submitting SMS code: {e}")
        await update.message.reply_text("❌ خطا در ثبت کد. لطفاً مجدداً تلاش کنید.")

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

# Download and encode
        try:
         bot = context.bot
         base64_image = await download_and_encode_photo(photo_file_id, bot)
         context.user_data['documents_collected'][doc_label] = base64_image
        except Exception as e:
         logger.error(f"Failed to encode document image: {e}")
         await update.message.reply_text("❌ خطا در دریافت تصویر. لطفا مجددا تلاش کنید.")
         return
        
        # Store the image as file_id
        if 'documents_collected' not in context.user_data:
            context.user_data['documents_collected'] = {}
        context.user_data['documents_collected'][doc_label] = f"File ID:{photo_file_id}"
        
        # Clear awaiting_image flag
        context.user_data['awaiting_image'] = False
        context.user_data['collecting_docs'] = True
        
        # Confirm receipt
        await update.message.reply_text(
            f"✅ تصویر برای مدرک '{doc_label}' دریافت شد.\n"
            "📸 تصویر با موفقیت ثبت شد."
        )
        
        service = context.user_data.get('current_service')
        docs = await get_documents_with_types(service)
        
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
async def copy_card_number(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send card number in a copyable message."""
    await query.answer()
    payment_info = data_store.get_payment_info()
    card_number = payment_info.get("cardNumber", "5041-7210-0916-7876")
    # Send only the card number in a code block for easy copy
    await query.message.reply_text(f"`{card_number}`", parse_mode=ParseMode.MARKDOWN)

@handle_errors
async def copy_amount(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the amount in a copyable message."""
    await query.answer()
    service = context.user_data.get('current_service')
    if not service:
        await query.message.reply_text("❌ خطا: سرویس یافت نشد.")
        return
    final_price = context.user_data.get('final_price', service.get('Price', 0))
    amount = format_price(final_price)
    # Send only the amount in a code block for easy copy
    await query.message.reply_text(f"`{amount}`", parse_mode=ParseMode.MARKDOWN)  
@handle_errors
async def handle_payment(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle payment processing"""
    try:
        await query.answer()
        
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        payment_info = data_store.get_payment_info()
        card_number = payment_info.get("cardNumber", "5041721009167876")
        logger.info('card_number',card_number);
        account_holder = payment_info.get("accountHolder", "محمد حسین نوابی")
        bank_name = payment_info.get("bankName", "بانک رسالت")
        final_price = context.user_data.get('final_price', service.get('Price', 0))
        amount = format_price(final_price)        
        text = f"💰 *پرداخت*\n\n"
        text += f"سرویس: {service.get('Title', '')}\n"
        text += f"مبلغ: {amount} تومان\n\n"
        text += "✅ لطفا مبلغ را به شماره کارت زیر واریز کنید:\n"
        text += f"🔹 شماره کارت: {card_number}\n"
        text += f"🔹 به نام: {account_holder}\n"
        # text += f"🔹 بانک: {bank_name}\n\n"
        text += "❗️ پس از پرداخت، تصویر رسید را ارسال کنید."
        
        keyboard = [
            [InlineKeyboardButton(" کپی شماره کارت", copy_text=CopyTextButton(card_number)),
             InlineKeyboardButton(" کپی مبلغ", copy_text=CopyTextButton(final_price))],
            [],
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
async def retry_submit(query, context: ContextTypes.DEFAULT_TYPE):
    """Retry submitting the request, re-authenticating if necessary."""
    await query.answer()
    await query.edit_message_text("⏳ در حال ثبت مجدد درخواست...")

    # Ensure we have a valid token; re‑authenticate if needed
    if not is_user_authenticated(context):
        user = query.from_user
        if not await authenticateUserWithServer(user, context=context):
            await query.edit_message_text(
                "❌ *خطا در احراز هویت*\n\nلطفا /start را بزنید و دوباره تلاش کنید.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    success = await sendRequestDataToServer(context)
    if success:
        await query.edit_message_text(
            "✅ *درخواست شما با موفقیت ثبت شد!*\n\n"
            "📞 به زودی کارشناسان ما با شما تماس خواهند گرفت.\n\n"
            "🙏 از اعتماد شما سپاسگزاریم!",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.clear()
        keyboard = [[InlineKeyboardButton("🏠 بازگشت به صفحه اصلی", callback_data="back_to_menu")]]
        await query.message.reply_text(
            "🔹 برای مشاهده سایر خدمات، روی دکمه زیر کلیک کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Still failed – show error again with retry option
        keyboard = [
            [InlineKeyboardButton("🔄 تلاش مجدد", callback_data="retry_submit")],
            [InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")]
        ]
        await query.edit_message_text(
            "❌ *خطا در ثبت درخواست*\n\n"
            "متاسفانه درخواست شما ثبت نشد.\n"
            "لطفا مجددا تلاش کنید یا با پشتیبانی تماس بگیرید.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        
@handle_errors
async def handle_payment_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle payment receipt image and submit the request."""
    try:
        if not context.user_data.get('awaiting_payment_receipt', False):
            return

        photo = update.message.photo
        if not photo:
            await update.message.reply_text("❌ لطفا یک تصویر از رسید پرداخت ارسال کنید.")
            return

        # Download and encode receipt image
        try:
            bot = context.bot
            photo_file_id = photo[-1].file_id
            receipt_base64 = await download_and_encode_photo(photo_file_id, bot)
            context.user_data['receipt_image'] = receipt_base64
        except Exception as e:
            logger.error(f"Failed to encode receipt image: {e}")
            await update.message.reply_text("❌ خطا در دریافت تصویر رسید. لطفا مجددا تلاش کنید.")
            return

        # Ensure we have a valid token; re‑authenticate if needed
        if not is_user_authenticated(context):
            user = update.effective_user
            if not await authenticateUserWithServer(user, context=context):
                await update.message.reply_text(
                    "❌ *خطا در احراز هویت*\n\nلطفا /start را بزنید و دوباره تلاش کنید.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

        status_msg = await update.message.reply_text("⏳ در حال ثبت درخواست...")
        success = await sendRequestDataToServer(context)

        if success:
            await status_msg.edit_text(
                "✅ *درخواست شما با موفقیت ثبت شد!*\n\n"
                "📞 به زودی کارشناسان ما با شما تماس خواهند گرفت.\n\n"
                "🙏 از اعتماد شما سپاسگزاریم!",
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data.clear()
            keyboard = [[InlineKeyboardButton("🏠 بازگشت به صفحه اصلی", callback_data="back_to_menu")]]
            await update.message.reply_text(
                "🔹 برای مشاهده سایر خدمات، روی دکمه زیر کلیک کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [
                [InlineKeyboardButton("🔄 تلاش مجدد", callback_data="retry_submit")],
                [InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")]
            ]
            await status_msg.edit_text(
                "❌ *خطا در ثبت درخواست*\n\n"
                "متاسفانه درخواست شما ثبت نشد.\n"
                "لطفا مجددا تلاش کنید یا با پشتیبانی تماس بگیرید.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            # Do NOT clear context – keep data for retry
    except Exception as e:
        logger.error(f"Error in handle_payment_receipt: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ خطا در دریافت رسید. لطفا مجددا تلاش کنید.")

async def sendRequestDataToServer(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Send the collected request data to the /api/requests endpoint."""
    try:
        service = context.user_data.get('current_service')
        if not service:
            logger.error("No service in context")
            return False

        token = get_user_token(context)
        if not token:
            logger.error("No auth token found")
            return False

        # Build documents list from collected data
        docs_collected = context.user_data.get('documents_collected', {})
        documents = [{"title": title, "value": value} for title, value in docs_collected.items()]

        receipt_image = context.user_data.get('receipt_image', '')
        final_price = context.user_data.get('final_price', service.get('Price', 0))

        payload = {
            "serviceId": service['Id'],
            "serviceTitle": service['Title'],
            "price": final_price,
            "documents": documents,
            "receiptImage": receipt_image
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
            resp = await client.post(
                f"{API_BASE.rstrip('/')}/api/requests",
                json=payload,
                headers=headers
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"Request submitted successfully: {result}")
            return True
    except Exception as e:
        logger.error(f"Error submitting request: {e}")
        logger.error(traceback.format_exc())
        return False

async def download_and_encode_photo(file_id: str, bot, max_size: int = 1024, quality: int = 85) -> str:
    """Download a photo from Telegram, compress/resize, and return as base64 data URL."""
    try:
        file = await bot.get_file(file_id)
        file_bytes = await file.download_as_bytearray()

            # Fallback: encode original file
        encoded = base64.b64encode(file_bytes).decode('utf-8')
        ext = file.file_path.split('.')[-1].lower() if file.file_path else 'png'
        mime = f"image/{ext}" if ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'] else "image/png"
        return f"data:{mime};base64,{encoded}"
    except Exception as e:
        logger.error(f"Error downloading/encoding photo: {e}")
        raise

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

_refresh_task: Optional[asyncio.Task] = None

async def _background_refresh_loop() -> None:
    """Periodically refresh CATEGORIES/SERVICES/DOC_TYPES from the API.

    Each resource has its own TTL in api_client.py, so this just needs to
    run more often than the shortest TTL - the refresh calls are cheap
    no-ops when nothing is due yet.
    """
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            await data_store.refresh_all(force=False)
            logger.info(
                f"Data refreshed: {len(SERVICES)} services, "
                f"{len(CATEGORIES)} categories, {len(DOC_TYPES)} doc types"
            )
        except Exception as e:
            logger.error(f"Background refresh failed: {e}")
            logger.error(traceback.format_exc())

async def post_init(application: Application) -> None:
    """Runs once, inside the bot's own event loop, before polling starts."""
    logger.info("Loading initial data from the Cafenet Online API...")
    await data_store.refresh_all(force=True)
    logger.info(
        f"Loaded {len(SERVICES)} services, {len(CATEGORIES)} categories, "
        f"{len(DOC_TYPES)} document types"
    )

    global _refresh_task
    _refresh_task = asyncio.create_task(_background_refresh_loop())

    await set_menu_button(application)

async def post_shutdown(application: Application) -> None:
    if _refresh_task:
        _refresh_task.cancel()
    await data_store.close()

def run_bot():
    """Run the bot with infinite retry on connection errors"""
    max_retries = 5
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Starting bot (attempt {attempt + 1}/{max_retries})...")
            
            application = (
                Application.builder()
                .token(BOT_TOKEN)
                .post_init(post_init)
                .post_shutdown(post_shutdown)
                .build()
            )
            
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