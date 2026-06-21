# bot.py
import json
import logging
import sys
import traceback
from typing import List, Dict, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, BotCommand, MenuButtonDefault
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, NetworkError

# Import config
try:
    from config import BOT_TOKEN
except ImportError:
    BOT_TOKEN = "8824483780:AAH7CES3hG69Kf0q_wA6D0oe1-tE0Lxz7pI"

# Enable logging with more detail
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress httpx logging
logging.getLogger("httpx").setLevel(logging.WARNING)

# Load data
def load_data():
    try:
        with open("cofenet-items.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("cofenet-items.json file not found!")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON: {e}")
        sys.exit(1)

SERVICES = load_data()

# Helper functions
def parse_price(price_str) -> int:
    """Convert price string to integer"""
    try:
        if isinstance(price_str, str):
            return int(price_str.replace(",", ""))
        return int(price_str)
    except:
        return 0

def format_price(price_str) -> str:
    """Format price with commas"""
    try:
        price = parse_price(price_str)
        return f"{price:,}".replace(",", "٬")
    except:
        return str(price_str)

def get_top_services(services: List[Dict]) -> List[Dict]:
    """Get services with Sequence < 10"""
    try:
        return [s for s in services if s.get("Sequence", 0) < 10 and s.get("Is Active", False)]
    except:
        return []

def get_category_services(category: str, services: List[Dict]) -> List[Dict]:
    """Get services for a specific category"""
    try:
        result = []
        for service in services:
            if not service.get("Is Active", False):
                continue
            if category in service.get("Categories", "").split(", "):
                result.append(service)
        result.sort(key=lambda x: x.get("Sequence", 999))
        return result
    except:
        return []

def paginate_items(items: List[Dict], page: int = 0, page_size: int = 10) -> List[Dict]:
    """Paginate items"""
    try:
        start = page * page_size
        end = start + page_size
        return items[start:end]
    except:
        return []

def get_documents_list(service: Dict) -> List[str]:
    """Parse documents from service"""
    try:
        docs = service.get("Documents", "N/A")
        if docs == "N/A" or not docs:
            return ["نیازی به مدارک نیست"]
        return [d.strip() for d in docs.split("|") if d.strip()]
    except:
        return ["نیازی به مدارک نیست"]

# Error handler decorator
def handle_errors(func):
    """Decorator to handle errors in async functions"""
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except TelegramError as e:
            logger.error(f"Telegram error in {func.__name__}: {e}")
            logger.error(traceback.format_exc())
            try:
                for arg in args:
                    if hasattr(arg, 'message') and hasattr(arg.message, 'reply_text'):
                        await arg.message.reply_text("❌ خطا در ارتباط با تلگرام. لطفا مجددا تلاش کنید.")
                        break
                    elif hasattr(arg, 'edit_message_text'):
                        await arg.edit_message_text("❌ خطا در ارتباط با تلگرام. لطفا مجددا تلاش کنید.")
                        break
            except:
                pass
        except NetworkError as e:
            logger.error(f"Network error in {func.__name__}: {e}")
            logger.error(traceback.format_exc())
            try:
                for arg in args:
                    if hasattr(arg, 'message') and hasattr(arg.message, 'reply_text'):
                        await arg.message.reply_text("❌ خطا در شبکه. لطفا مجددا تلاش کنید.")
                        break
                    elif hasattr(arg, 'edit_message_text'):
                        await arg.edit_message_text("❌ خطا در شبکه. لطفا مجددا تلاش کنید.")
                        break
            except:
                pass
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
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
    return wrapper

async def set_menu_button(application: Application) -> None:
    """Set the menu button for the bot"""
    try:
        # Set commands for the menu button
        commands = [
            BotCommand("start", "🏠 صفحه اصلی"),
            BotCommand("top", "⭐ خدمات پر کاربرد"),
            BotCommand("cancel", "❌ لغو عملیات"),
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
        # Clear any previous data
        context.user_data.clear()
        
        user = update.effective_user
        welcome_text = (
            f"به ربات کافی نت خوش آمدید {user.first_name}! 👋\n"
            "چه کمکی می توان به شما بکنم؟\n\n"
            "🔹 با کلیک روی هر دکمه می‌توانید خدمات مربوطه را مشاهده کنید.\n"
            "🔹 همچنین می‌توانید عبارت مورد نظر را تایپ کرده و جستجو کنید.\n"
            "🔹 از منوی زیر نیز می‌توانید استفاده کنید."
        )
        
        # Get categories with CategourySequence < 10
        categories = {}
        for service in SERVICES:
            if not service.get("Is Active", False):
                continue
            seq = service.get("CategourySequence", 999)
            if seq < 10:
                for cat in service.get("Categories", "").split(", "):
                    if cat not in categories:
                        categories[cat] = []
                    categories[cat].append(service)
        
        # Sort categories by first service's sequence
        sorted_cats = sorted(
            categories.items(),
            key=lambda x: min(s.get("CategourySequence", 999) for s in x[1])
        )
        
        keyboard = []
        
        # Add "خدمات پر کاربرد" button at top (full width)
        keyboard.append([
            InlineKeyboardButton("⭐ خدمات پر کاربرد", callback_data="top_services")
        ])
        
        # Add category buttons in 2 columns
        row = []
        for idx, (cat_name, cat_services) in enumerate(sorted_cats[:10]):
            row.append(InlineKeyboardButton(
                f"📂 {cat_name}", 
                callback_data=f"cat_{cat_name[:50]}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        # Add remaining button if any
        if row:
            keyboard.append(row)
        
        # Add "همه خدمات" button at bottom (full width)
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

# Menu command
@handle_errors
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show main menu"""
    await start(update, context)

# Top services command
@handle_errors
async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show top services directly"""
    try:
        # Create a mock query object
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

# Main menu keyboard for input box
async def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Get the main menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("⭐ خدمات پر کاربرد", callback_data="top_services")],
        [InlineKeyboardButton("📂 همه دسته‌بندی‌ها", callback_data="show_categories")],
        [InlineKeyboardButton("📊 همه خدمات", callback_data="all_services")],
    ]
    return InlineKeyboardMarkup(keyboard)

# Show categories command
@handle_errors
async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all categories"""
    try:
        # Get categories with CategourySequence < 10
        categories = {}
        for service in SERVICES:
            if not service.get("Is Active", False):
                continue
            seq = service.get("CategourySequence", 999)
            if seq < 10:
                for cat in service.get("Categories", "").split(", "):
                    if cat not in categories:
                        categories[cat] = []
                    categories[cat].append(service)
        
        sorted_cats = sorted(
            categories.items(),
            key=lambda x: min(s.get("CategourySequence", 999) for s in x[1])
        )
        
        text = "📂 *دسته‌بندی‌های خدمات*\n\n"
        text += "لطفا یکی از دسته‌بندی‌های زیر را انتخاب کنید:\n\n"
        
        keyboard = []
        row = []
        for idx, (cat_name, cat_services) in enumerate(sorted_cats[:10]):
            row.append(InlineKeyboardButton(
                f"📂 {cat_name}", 
                callback_data=f"cat_{cat_name[:50]}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="back_to_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error(f"Error in show_categories: {e}")

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
        elif data == "show_categories":
            await show_categories(update, context)
        elif data.startswith("cat_"):
            category = data[4:]
            await show_category_services(query, category)
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
            category = parts[3]
            await show_category_services(query, category, page)
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
            # Do nothing for dummy buttons
            logger.info("Dummy callback received")
        else:
            logger.info(f"Unhandled callback data: {data}")

    except Exception as e:
        logger.error(f"Error in button_callback: {e}")
        logger.error(traceback.format_exc())
        try:
            await update.callback_query.edit_message_text("❌ خطا در پردازش درخواست. لطفا مجددا تلاش کنید.")
        except:
            pass

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
            price = format_price(service.get("Price", "0"))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n"
            text += f"  📂 {service.get('Categories', '')}\n\n"
        
        keyboard = []
        for service in services_page:
            # Show full title and price on button
            price = format_price(service.get("Price", "0"))
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
        
        # Add page info
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
        active_services = [s for s in SERVICES if s.get("Is Active", False)]
        active_services.sort(key=lambda x: x.get("Sequence", 999))
        
        total_items = len(active_services)
        page_size = 10
        
        if total_items == 0:
            await query.edit_message_text("هیچ سرویسی یافت نشد.")
            return
        
        services_page = paginate_items(active_services, page, page_size)
        total_pages = (total_items + page_size - 1) // page_size
        
        text = f"📊 *همه خدمات* (صفحه {page+1} از {total_pages})\n\n"
        for service in services_page:
            price = format_price(service.get("Price", "0"))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n"
            text += f"  📂 {service.get('Categories', '')}\n\n"
        
        keyboard = []
        for service in services_page:
            # Show full title and price on button
            price = format_price(service.get("Price", "0"))
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
        
        # Add page info
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
async def show_category_services(query, category: str, page: int = 0):
    """Show services for a specific category with pagination"""
    try:
        category_services = get_category_services(category, SERVICES)
        
        if not category_services:
            await query.edit_message_text(f"هیچ سرویسی در دسته '{category}' یافت نشد.")
            return
        
        total_items = len(category_services)
        page_size = 10
        services_page = paginate_items(category_services, page, page_size)
        total_pages = (total_items + page_size - 1) // page_size
        
        text = f"📂 *{category}* (صفحه {page+1} از {total_pages})\n\n"
        for service in services_page:
            price = format_price(service.get("Price", "0"))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n\n"
        
        keyboard = []
        for service in services_page:
            # Show full title and price on button
            price = format_price(service.get("Price", "0"))
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
            nav_buttons.append(InlineKeyboardButton("◀️ صفحه قبل", callback_data=f"page_cat_{page-1}_{category}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("صفحه بعد ▶️", callback_data=f"page_cat_{page+1}_{category}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        # Add page info
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
        logger.error(f"Error in show_category_services: {e}")
        logger.error(traceback.format_exc())
        await query.edit_message_text("❌ خطا در نمایش خدمات دسته.")

@handle_errors
async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle search input from user"""
    try:
        # Check if we're in document collection mode
        if context.user_data.get('collecting_docs', False):
            # Let the document input handler process this
            return
            
        search_term = update.message.text.strip()
        
        if not search_term:
            return
        
        results = []
        for service in SERVICES:
            if not service.get("Is Active", False):
                continue
            if search_term.lower() in service.get("Title", "").lower():
                results.append(service)
            elif search_term.lower() in service.get("Categories", "").lower():
                results.append(service)
        
        if not results:
            await update.message.reply_text(
                f"❌ هیچ نتیجه‌ای برای '{search_term}' یافت نشد.\n"
                "لطفا عبارت دیگری را جستجو کنید یا از دکمه‌ها استفاده کنید."
            )
            return
        
        # Store results in context for pagination
        context.user_data['search_results'] = results
        context.user_data['search_term'] = search_term
        
        text = f"🔍 *نتایج جستجو برای '{search_term}':*\n\n"
        for service in results[:10]:
            price = format_price(service.get("Price", "0"))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n\n"
        
        keyboard = []
        for service in results[:10]:
            price = format_price(service.get("Price", "0"))
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
        
        text = f"🔍 *نتایج بیشتر برای '{search_term}':*\n\n"
        for service in results[10:20]:
            price = format_price(service.get("Price", "0"))
            text += f"• {service['Title']}\n"
            text += f"  💰 قیمت: {price} تومان\n\n"
        
        keyboard = []
        for service in results[10:20]:
            price = format_price(service.get("Price", "0"))
            button_text = f"📋 {service['Title'][:35]}"
            if len(service['Title']) > 35:
                button_text += "..."
            button_text += f" - {price}ت"
            keyboard.append([
                InlineKeyboardButton(button_text, callback_data=f"service_{service['Id']}")
            ])
        
        if len(results) > 20:
            keyboard.append([
                InlineKeyboardButton("▶️ مشاهده بیشتر", callback_data="search_more_2")
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
        
        price = format_price(service.get("Price", "0"))
        discount = service.get("Discount", "0")
        discount_price = format_price(discount)
        
        text = f"📋 *{service['Title']}*\n\n"
        text += f"💰 قیمت: {price} تومان\n"
        if parse_price(discount) > 0:
            text += f"🎯 تخفیف: {discount_price} تومان\n"
        text += f"📂 دسته‌بندی: {service.get('Categories', '')}\n"
        text += f"📄 تعداد مدارک: {service.get('Documents Count', 0)}\n"
        text += f"🔗 [مشاهده در سایت]({service.get('URL', '#')})\n\n"
        
        docs = get_documents_list(service)
        text += "*مدارک مورد نیاز:*\n"
        for idx, doc in enumerate(docs, 1):
            text += f"{idx}. {doc}\n"
        
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
        # Clear user data
        context.user_data.clear()
        
        # Create a new update object to call start
        user = query.from_user
        welcome_text = (
            f"به ربات کافی نت خوش آمدید {user.first_name}! 👋\n"
            "چه کمکی می توان به شما بکنم؟\n\n"
            "🔹 با کلیک روی هر دکمه می‌توانید خدمات مربوطه را مشاهده کنید.\n"
            "🔹 همچنین می‌توانید عبارت مورد نظر را تایپ کرده و جستجو کنید.\n"
            "🔹 از منوی زیر نیز می‌توانید استفاده کنید."
        )
        
        # Get categories with CategourySequence < 10
        categories = {}
        for service in SERVICES:
            if not service.get("Is Active", False):
                continue
            seq = service.get("CategourySequence", 999)
            if seq < 10:
                for cat in service.get("Categories", "").split(", "):
                    if cat not in categories:
                        categories[cat] = []
                    categories[cat].append(service)
        
        sorted_cats = sorted(
            categories.items(),
            key=lambda x: min(s.get("CategourySequence", 999) for s in x[1])
        )
        
        keyboard = [
            [InlineKeyboardButton("⭐ خدمات پر کاربرد", callback_data="top_services")]
        ]
        
        # Add category buttons in 2 columns
        row = []
        for idx, (cat_name, cat_services) in enumerate(sorted_cats[:10]):
            row.append(InlineKeyboardButton(f"📂 {cat_name}", callback_data=f"cat_{cat_name[:50]}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
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
        
        docs = get_documents_list(service)
        if len(docs) == 1 and docs[0] == "نیازی به مدارک نیست":
            await query.edit_message_text(
                f"✅ این سرویس نیازی به مدارک ندارد.\n"
                f"💰 مبلغ قابل پرداخت: {format_price(service.get('Price', '0'))} تومان\n\n"
                "لطفا تصویر رسید پرداخت را ارسال کنید."
            )
            context.user_data['awaiting_payment'] = True
            context.user_data['collecting_docs'] = False
            return
        
        # Start document collection
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
        
        docs = get_documents_list(service)
        
        if doc_index >= len(docs):
            # All documents collected - show summary and payment
            await show_document_summary_and_payment(query, context)
            return
        
        current_doc = docs[doc_index]
        total = len(docs)
        
        # Get previously entered value if any
        previous_value = context.user_data['documents_collected'].get(current_doc, "")
        
        text = f"📄 *مدرک {doc_index + 1} از {total}*\n\n"
        text += f"لطفا اطلاعات زیر را وارد کنید:\n"
        text += f"🔹 {current_doc}\n\n"
        if previous_value:
            text += f"📌 مقدار قبلی: {previous_value}\n\n"
        text += "❗️ لطفا دقیق و کامل وارد کنید."
        
        keyboard = [
            [InlineKeyboardButton("🔙 مرحله قبل", callback_data=f"prev_doc_{doc_index}")],
            [InlineKeyboardButton("❌ لغو و بازگشت به منو", callback_data="back_to_menu")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
        context.user_data['doc_index'] = doc_index
        context.user_data['doc_label'] = current_doc
        context.user_data['collecting_docs'] = True
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
        
        # Go to previous document
        prev_index = doc_index - 1
        if prev_index < 0:
            await query.edit_message_text("❌ شما در اولین مرحله هستید.")
            return
        
        docs = get_documents_list(service)
        if prev_index >= len(docs):
            await query.edit_message_text("❌ خطا در بازگشت به مرحله قبل.")
            return
        
        # Remove the current document value so user can re-enter
        current_label = docs[doc_index]
        if current_label in context.user_data['documents_collected']:
            del context.user_data['documents_collected'][current_label]
        
        # Go to previous document
        await collect_next_document(query, prev_index, context)
    except Exception as e:
        logger.error(f"Error in go_to_previous_document: {e}")
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
        
        # Show summary
        summary = "📋 *خلاصه مدارک*\n\n"
        for key, value in context.user_data['documents_collected'].items():
            summary += f"🔹 {key}\n   {value}\n\n"
        
        if not context.user_data['documents_collected']:
            summary += "⚠️ هیچ مدرکی وارد نشده است.\n\n"
        
        summary += f"💰 *مبلغ قابل پرداخت: {format_price(service.get('Price', '0'))} تومان*\n\n"
        summary += "✅ آیا اطلاعات وارد شده صحیح است؟"
        
        keyboard = [
            [InlineKeyboardButton("✅ بله، صحیح است - پرداخت", callback_data="pay_now")],
            [InlineKeyboardButton("🔙 بازگشت به مرحله قبل", callback_data=f"prev_doc_{len(get_documents_list(service))-1}")],
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
        
        # Reset document collection
        context.user_data['documents_collected'] = {}
        context.user_data['doc_index'] = 0
        context.user_data['collecting_docs'] = True
        
        await collect_next_document(query, 0, context)
    except Exception as e:
        logger.error(f"Error in start_over: {e}")
        logger.error(traceback.format_exc())

@handle_errors
async def handle_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document input from user"""
    logger.info("handle_document_input")

    try:
        if not context.user_data.get('collecting_docs', False):
           await handle_search(update, context)  # If not collecting docs, treat as search input
           return
        
        doc_value = update.message.text
        doc_label = context.user_data.get('doc_label', '')
        doc_index = context.user_data.get('doc_index', 0)
        logger.info("handle_document_input" + f" - doc_label: {doc_label}, doc_value: {doc_value}, doc_index: {doc_index}")

        # Store the document value
        if 'documents_collected' not in context.user_data:
            context.user_data['documents_collected'] = {}
        context.user_data['documents_collected'][doc_label] = doc_value
        
        # Move to next document
        service = context.user_data.get('current_service')
        docs = get_documents_list(service)
        
        next_index = doc_index + 1
        
        # Create a new query object to use the same functions
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
            # All documents collected - show summary and payment
            await show_document_summary_and_payment(mock_query, context)
        else:
            # Continue with next document
            await collect_next_document(mock_query, next_index, context)
    except Exception as e:
        logger.error(f"Error in handle_document_input: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ خطا در دریافت اطلاعات. لطفا مجددا تلاش کنید.")

@handle_errors
async def handle_payment(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle payment processing"""
    try:
        await query.answer()
        
        service = context.user_data.get('current_service')
        if not service:
            await query.edit_message_text("❌ خطا در دریافت اطلاعات سرویس.")
            return
        
        # Show payment details
        text = f"💰 *پرداخت*\n\n"
        text += f"سرویس: {service.get('Title', '')}\n"
        text += f"مبلغ: {format_price(service.get('Price', '0'))} تومان\n\n"
        text += "✅ لطفا مبلغ را به شماره کارت زیر واریز کنید:\n"
        text += "🔹 شماره کارت: `6037-9912-3456-7890`\n"
        text += "🔹 به نام: کافی نت آنلاین\n\n"
        text += "❗️ پس از پرداخت، تصویر رسید را ارسال کنید."
        
        keyboard = [
            [InlineKeyboardButton("✅ پرداخت انجام شد", callback_data="payment_done")],
            [InlineKeyboardButton("🔙 بازگشت به خلاصه مدارک", callback_data=f"prev_doc_{len(get_documents_list(service))-1}")],
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
        
        # Get the photo
        photo = update.message.photo
        if not photo:
            await update.message.reply_text(
                "❌ لطفا یک تصویر از رسید پرداخت ارسال کنید."
            )
            return
        
        # Here you would save the photo and process the order
        # For now, just confirm
        
        await update.message.reply_text(
            "✅ *رسید پرداخت شما دریافت شد!*\n\n"
            "🎉 درخواست شما با موفقیت ثبت شد.\n"
            "📞 به زودی کارشناسان ما با شما تماس خواهند گرفت.\n\n"
            "🙏 از اعتماد شما سپاسگزاریم!"
        )
        
        # Clear session data
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Error in handle_payment_receipt: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("❌ خطا در دریافت رسید. لطفا مجددا تلاش کنید.")

@handle_errors
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo upload"""
    try:
        if context.user_data.get('awaiting_payment_receipt', False):
            await handle_payment_receipt(update, context)
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

@handle_errors
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming messages"""
        user = update.effective_user
        message_text = update.message.text
        logger.info(f"Received message from {user.id} ({user.first_name}): {message_text}")
        await handle_search(update, context)    


# Global error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log and handle errors"""
    logger.error(f"Unhandled error: {context.error}")
    logger.error(traceback.format_exc())
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ خطایی رخ داده است. لطفا مجددا تلاش کنید یا /start را بزنید."
            )
    except:
        pass

def main() -> None:
    """Start the bot with retry logic"""
    while True:
        try:
            # Create the application
            application = Application.builder().token(BOT_TOKEN).build()
            
            # Set menu button commands
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(set_menu_button(application))
            
            # Add global error handler
            application.add_error_handler(error_handler)
            
            # Add command handlers
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("menu", menu_command))
            application.add_handler(CommandHandler("top", top_command))
            application.add_handler(CommandHandler("cancel", cancel))
            
            # Callback query handler
            application.add_handler(CallbackQueryHandler(button_callback))
            
            # Message handlers - ORDER MATTERS!
            # application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_document_input))
            application.add_handler(MessageHandler(filters.TEXT, handle_search))
 
            # Start the bot
            logger.info("Bot is starting...")
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            
            # If we get here, the bot stopped normally
            break
            
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as e:
            logger.error(f"Error in main: {e}")
            logger.error(traceback.format_exc())
            logger.info("Restarting bot in 5 seconds...")
            import time
            time.sleep(5)
            continue

if __name__ == "__main__":
    main()