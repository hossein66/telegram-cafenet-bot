# app.py - Fixed Version with Better Error Handling

from fastapi import FastAPI, HTTPException, Depends, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict, Literal
from datetime import datetime, timedelta
import uuid
import jwt
import hashlib
import sqlite3
import json
import os
import time
import threading
from contextlib import contextmanager
import math
from functools import wraps

# ─────────────────────────────────────────────────────────────
#  SIMPLE THREAD-SAFE CACHE (No Redis required)
# ─────────────────────────────────────────────────────────────
class SimpleCache:
    """Thread-safe in-memory cache with TTL"""
    
    def __init__(self, default_ttl=300):
        self.cache = {}
        self.ttl = {}
        self.lock = threading.Lock()
        self.default_ttl = default_ttl
        
    def get(self, key):
        """Get value from cache if not expired"""
        with self.lock:
            if key in self.cache:
                if key in self.ttl and time.time() > self.ttl[key]:
                    # Expired, remove it
                    del self.cache[key]
                    del self.ttl[key]
                    return None
                return self.cache[key]
            return None
    
    def set(self, key, value, ttl=None):
        """Set cache value with TTL in seconds"""
        with self.lock:
            self.cache[key] = value
            if ttl is None:
                ttl = self.default_ttl
            self.ttl[key] = time.time() + ttl
    
    def delete(self, key):
        """Remove key from cache"""
        with self.lock:
            self.cache.pop(key, None)
            self.ttl.pop(key, None)
    
    def clear(self):
        """Clear entire cache"""
        with self.lock:
            self.cache.clear()
            self.ttl.clear()
    
    def cleanup(self):
        """Remove expired items"""
        with self.lock:
            now = time.time()
            expired_keys = [k for k, exp in self.ttl.items() if exp <= now]
            for k in expired_keys:
                self.cache.pop(k, None)
                self.ttl.pop(k, None)

# Global cache instance
cache = SimpleCache(default_ttl=300)

# Cache decorator - FIXED for sync functions
def cached(ttl=None):
    """Decorator to cache function results - works with sync functions"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Create cache key from function name and arguments
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
            
            # Try to get from cache
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                try:
                    return json.loads(cached_result)
                except (json.JSONDecodeError, TypeError):
                    return cached_result
            
            # Execute function
            result = func(*args, **kwargs)
            
            # Store in cache
            try:
                # Convert to JSON for storage
                serializable = json.dumps(result, default=str, ensure_ascii=False)
                cache.set(cache_key, serializable, ttl)
            except (TypeError, ValueError) as e:
                # If can't serialize, store as-is with shorter TTL
                print(f"Cache serialization warning: {e}")
                cache.set(cache_key, result, 60)
            
            return result
        return wrapper
    return decorator

# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────
SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
DB_PATH = "cafenet.db"

# ─────────────────────────────────────────────────────────────
#  DATABASE HELPERS with Connection Pooling
# ─────────────────────────────────────────────────────────────
import threading
_db_local = threading.local()

def get_db_connection():
    """Get database connection from thread-local storage"""
    try:
        if not hasattr(_db_local, 'conn') or _db_local.conn is None:
            _db_local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _db_local.conn.row_factory = sqlite3.Row
            _db_local.conn.execute("PRAGMA foreign_keys = ON")
            _db_local.conn.execute("PRAGMA journal_mode = WAL")
            _db_local.conn.execute("PRAGMA synchronous = NORMAL")
            _db_local.conn.execute("PRAGMA cache_size = -10000")  # 10MB cache
            _db_local.conn.execute("PRAGMA temp_store = MEMORY")
        return _db_local.conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        # Fallback to a new connection
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = None
    try:
        conn = get_db_connection()
        yield conn
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        # Don't close - keep for pooling
        pass

# ─────────────────────────────────────────────────────────────
#  INIT DATABASE WITH INDEXES
# ─────────────────────────────────────────────────────────────
def init_db():
    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Categories
            cur.execute("""
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    icon TEXT,
                    sort INTEGER DEFAULT 0,
                    is_enable INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)

            # Services
            cur.execute("""
                CREATE TABLE IF NOT EXISTS services (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category_id INTEGER,
                    sort INTEGER DEFAULT 0,
                    price INTEGER DEFAULT 0,
                    cost INTEGER DEFAULT 0,
                    benefit INTEGER DEFAULT 0,
                    duration TEXT,
                    is_special INTEGER DEFAULT 0,
                    is_enabled INTEGER DEFAULT 1,
                    type TEXT DEFAULT 'selectable',
                    description TEXT,
                    is_payment_required INTEGER DEFAULT 1,
                    is_location_based INTEGER DEFAULT 1,
                    auto_invoice_enabled INTEGER DEFAULT 1,
                    is_notice_enabled INTEGER DEFAULT 0,
                    notice_text TEXT,
                    notice_image TEXT,
                    images TEXT,
                    forms TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    FOREIGN KEY (category_id) REFERENCES categories(id)
                )
            """)

            # Users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    phone TEXT UNIQUE,
                    first_name TEXT,
                    last_name TEXT,
                    username TEXT,
                    telegram_id INTEGER UNIQUE,
                    is_premium INTEGER DEFAULT 0,
                    token TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)

            # Requests
            cur.execute("""
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    service_title TEXT,
                    price INTEGER DEFAULT 0,
                    documents TEXT,
                    receipt_image TEXT,
                    status TEXT DEFAULT 'pending',
                    submitted_at TEXT,
                    updated_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)

            # OTP codes
            cur.execute("""
                CREATE TABLE IF NOT EXISTS otp_codes (
                    phone TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0
                )
            """)

            cur.execute("""
    CREATE TABLE IF NOT EXISTS payment_config (
        id INTEGER PRIMARY KEY DEFAULT 1,
        card_number TEXT NOT NULL,
        account_holder TEXT NOT NULL,
        bank_name TEXT,
        updated_at TEXT
    )
""")
            # ─── CREATE INDEXES ─────────────────────────────────────
            print("📊 Creating indexes...")
            
            # Service indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_services_category ON services(category_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_services_enabled ON services(is_enabled)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_services_sort ON services(sort)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_services_title ON services(title)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_services_special ON services(is_special)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_services_price ON services(price)")
            
            # Request indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_submitted ON requests(submitted_at)")
            
            # User indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
            
            # Category indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_categories_enable ON categories(is_enable)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_categories_sort ON categories(sort)")
            
            # OTP indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_otp_expires ON otp_codes(expires_at)")
            
            # Composite indexes for common queries
            cur.execute("CREATE INDEX IF NOT EXISTS idx_services_category_enabled ON services(category_id, is_enabled)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_services_sort_enabled ON services(sort, is_enabled)")
            
            conn.commit()
            print("✅ Database initialized with indexes")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")
        raise

# ─────────────────────────────────────────────────────────────
#  MODELS
# ─────────────────────────────────────────────────────────────
class Category(BaseModel):
    id: Optional[int] = None
    name: str
    icon: str = "📂"
    sort: int = 0
    isEnable: bool = True
    count: int = 0

    class Config:
        from_attributes = True

class FieldOption(BaseModel):
    label: str
    value: str

class Field(BaseModel):
    id: str
    label: str
    key: str
    type: str
    isRequired: bool = False
    placeholder: Optional[str] = ""
    options: List[str] = []
    hasOther: bool = False
    validationRules: Dict[str, Any] = {}

class Form(BaseModel):
    id: str
    title: str
    description: str
    fields: List[Field]

class ServiceCreate(BaseModel):
    serviceId: str
    serviceTitle: str
    category: str
    sort: int = 0
    data: Dict[str, Any]

class ServiceResponse(BaseModel):
    serviceId: str
    serviceTitle: str
    category: str
    sort: int = 0
    price: int = 0
    duration: str = ""
    isSpecial: bool = False
    isEnabled: bool = True
    description: str = ""

class ServiceDetailResponse(BaseModel):
    serviceId: str
    serviceTitle: str
    category: str
    sort: int = 0
    data: Dict[str, Any]

class PaginatedResponse(BaseModel):
    items: List[ServiceResponse]
    nextCursor: Optional[str] = None
    hasMore: bool
    total: int

class User(BaseModel):
    id: str
    phone: str
    firstName: Optional[str] = ""
    lastName: Optional[str] = ""
    username: Optional[str] = ""
    telegramId: Optional[int] = None
    isPremium: bool = False
    token: Optional[str] = None

class RequestCreate(BaseModel):
    serviceId: str
    serviceTitle: str
    price: int
    documents: List[Dict[str, str]]
    receiptImage: Optional[str] = None

class RequestResponse(BaseModel):
    id: str
    userId: str
    serviceId: str
    serviceTitle: str
    price: int
    documents: List[Dict[str, str]]
    receiptImage: Optional[str] = None
    status: str
    submittedAt: str

class OTPRequest(BaseModel):
    phone: str
    app: Optional[str] = None

class OTPVerify(BaseModel):
    phone: str
    otp: str

class TelegramLogin(BaseModel):
    initData: str
    user: Dict[str, Any]

class LoginResponse(BaseModel):
    success: bool
    user: Optional[User] = None
    token: Optional[str] = None
    message: Optional[str] = None

class BulkImportResult(BaseModel):
    imported: int
    updated: int
    failed: int
    errors: List[str]

# ─────────────────────────────────────────────────────────────
#  AUTH HELPERS
# ─────────────────────────────────────────────────────────────
security = HTTPBearer()

def create_token(user_id: str, phone: str) -> str:
    payload = {
        "sub": user_id,
        "phone": phone,
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    payload = decode_token(credentials.credentials)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (payload["sub"],))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="User not found")
        return dict(row)

# ─────────────────────────────────────────────────────────────
#  APP INIT
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="Cafenet Online API", version="2.0.0")

# Add compression middleware
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add performance monitoring middleware
@app.middleware("http")
async def add_process_time_header(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    
    # Log slow requests
    if process_time > 0.5:
        print(f"⚠️ Slow request: {request.url.path} - {process_time:.2f}s")
    
    return response

# ─────────────────────────────────────────────────────────────
#  PAYMENT INFO ENDPOINT
# ─────────────────────────────────────────────────────────────
@app.get("/api/payment/info")
@cached(ttl=3600)  # Cache for 1 hour
def get_payment_info():
    """Get payment information from database"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT card_number, account_holder, bank_name 
                FROM payment_config 
                WHERE id = 1
            """)
            row = cur.fetchone()
            
            if row:
                return {
                    "cardNumber": row["card_number"],
                    "accountHolder": row["account_holder"],
                    "bankName": row["bank_name"] or "بانک ملی"
                }
            else:
                # Return defaults if no config exists
                return {
                    "cardNumber": "5041-7210-0916-7876",
                    "accountHolder": "محمد حسین نوابی",
                    "bankName": "بانک رسالت"
                }
    except Exception as e:
        print(f"❌ Error in get_payment_info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Admin endpoint to update payment info
@app.put("/api/payment/info")
def update_payment_info(
    card_number: str,
    account_holder: str,
    bank_name: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    """Update payment information (admin only)"""
    if user["id"] != 'user_927b32ac7f9ef3ef' :
      raise HTTPException(status_code=500, detail=str(e))

    try:
        # You might want to add admin check here
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO payment_config (id, card_number, account_holder, bank_name, updated_at)
                VALUES (1, ?, ?, ?, ?)
            """, (card_number, account_holder, bank_name or "بانک ملی", datetime.utcnow().isoformat()))
            conn.commit()
            
            # Clear cache if needed
            cache.delete("/api/payment/info")
            
            return {"success": True, "message": "Payment info updated"}
    except Exception as e:
        print(f"❌ Error in update_payment_info: {e}")
        raise HTTPException(status_code=500, detail=str(e))
# ─────────────────────────────────────────────────────────────
#  CATEGORY ENDPOINTS - FIXED
# ─────────────────────────────────────────────────────────────
@app.get("/api/categories", response_model=List[Category])
def get_categories():
    """Get all categories with service counts - NO CACHE to avoid issues"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            # Optimized query with proper joins
            cur.execute("""
                WITH category_counts AS (
                    SELECT category_id, COUNT(*) as cnt
                    FROM services
                    WHERE is_enabled = 1
                    GROUP BY category_id
                )
                SELECT 
                    c.id, 
                    c.name, 
                    c.icon, 
                    c.sort, 
                    c.is_enable as isEnable,
                    COALESCE(cc.cnt, 0) as count
                FROM categories c
                LEFT JOIN category_counts cc ON c.id = cc.category_id
                WHERE c.is_enable = 1
                UNION ALL
                SELECT 
                    -1 as id,
                    'پرکاربرد' as name,
                    '⭐' as icon,
                    -5 as sort,
                    1 as isEnable,
                    COUNT(*) as count
                FROM services
                WHERE is_special = 1 AND is_enabled = 1
                UNION ALL
                SELECT 
                    0 as id,
                    'همه خدمات' as name,
                    '🔍' as icon,
                    -4 as sort,
                    1 as isEnable,
                    COUNT(*) as count
                FROM services
                WHERE is_enabled = 1
                ORDER BY sort ASC, name ASC
            """)
            rows = cur.fetchall()
            result = [dict(row) for row in rows]
            return result
    except Exception as e:
        print(f"❌ Error in get_categories: {e}")
        # Return empty list instead of error
        return []

# Alternative cached version - use this if you want caching
@app.get("/api/categories-cached", response_model=List[Category])
@cached(ttl=3600)  # Cache for 1 hour
def get_categories_cached():
    """Get all categories with service counts - CACHED version"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                WITH category_counts AS (
                    SELECT category_id, COUNT(*) as cnt
                    FROM services
                    WHERE is_enabled = 1
                    GROUP BY category_id
                )
                SELECT 
                    c.id, 
                    c.name, 
                    c.icon, 
                    c.sort, 
                    c.is_enable as isEnable,
                    COALESCE(cc.cnt, 0) as count
                FROM categories c
                LEFT JOIN category_counts cc ON c.id = cc.category_id
                WHERE c.is_enable = 1
                UNION ALL
                SELECT 
                    -1 as id,
                    'پرکاربرد' as name,
                    '⭐' as icon,
                    -5 as sort,
                    1 as isEnable,
                    COUNT(*) as count
                FROM services
                WHERE is_special = 1 AND is_enabled = 1
                UNION ALL
                SELECT 
                    0 as id,
                    'همه خدمات' as name,
                    '🔍' as icon,
                    -4 as sort,
                    1 as isEnable,
                    COUNT(*) as count
                FROM services
                WHERE is_enabled = 1
                ORDER BY sort ASC, name ASC
            """)
            rows = cur.fetchall()
            result = [dict(row) for row in rows]
            return result
    except Exception as e:
        print(f"❌ Error in get_categories_cached: {e}")
        return []

@app.post("/api/categories")
def create_category(cat: Category):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO categories (name, icon, sort, is_enable, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (cat.name, cat.icon, cat.sort, 1 if cat.isEnable else 0,
                  datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
            conn.commit()
            # Clear cache
            cache.delete(hashlib.md5('get_categories_cached'.encode()).hexdigest())
            return {"success": True, "id": cur.lastrowid}
    except Exception as e:
        print(f"❌ Error in create_category: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────
#  SERVICE ENDPOINTS
# ─────────────────────────────────────────────────────────────
@app.get("/api/services", response_model=PaginatedResponse)
def get_services(
    cursor: Optional[str] = Query(None),
    limit: int = Query(12, ge=1, le=100),
    category: Optional[str] = None,
    search: Optional[str] = None,
    sort: Literal["default", "price_asc", "price_desc"] = "default",
    page: Optional[int] = None,
):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            # Use more efficient query - select only needed columns
            query = """
                SELECT s.id as serviceId, s.title as serviceTitle, c.name as category,
                       s.sort, s.price, s.duration, s.is_special as isSpecial,
                       s.is_enabled as isEnabled, s.description
                FROM services s
                LEFT JOIN categories c ON s.category_id = c.id
                WHERE s.is_enabled = 1
            """
            params = []
            where_clauses = []

            if category:
                where_clauses.append("c.name = ?")
                params.append(category)

            if search:
                where_clauses.append("(s.title LIKE ? OR c.name LIKE ?)")
                params.extend([f"%{search}%", f"%{search}%"])

            if where_clauses:
                query += " AND " + " AND ".join(where_clauses)

            # More efficient cursor pagination
            if cursor:
                try:
                    sort_val, last_id = cursor.split("|")
                    if sort == "default":
                        query += " AND (s.sort > ? OR (s.sort = ? AND s.id > ?))"
                        params.extend([int(sort_val), int(sort_val), last_id])
                    elif sort == "price_asc":
                        query += " AND (s.price > ? OR (s.price = ? AND s.id > ?))"
                        params.extend([int(sort_val), int(sort_val), last_id])
                    else:
                        query += " AND (s.price < ? OR (s.price = ? AND s.id > ?))"
                        params.extend([int(sort_val), int(sort_val), last_id])
                except:
                    pass

            # Use indexed order by
            if sort == "price_asc":
                query += " ORDER BY s.price ASC, s.id ASC"
            elif sort == "price_desc":
                query += " ORDER BY s.price DESC, s.id ASC"
            else:
                query += " ORDER BY s.sort ASC, s.id ASC"

            query += f" LIMIT {limit + 1}"
            
            cur.execute(query, params)
            rows = cur.fetchall()
            items = [dict(row) for row in rows]

            # Optimized count query
            count_query = """
                SELECT COUNT(*) as total
                FROM services s
                LEFT JOIN categories c ON s.category_id = c.id
                WHERE s.is_enabled = 1
            """
            count_params = []
            if category:
                count_query += " AND c.name = ?"
                count_params.append(category)
            if search:
                count_query += " AND (s.title LIKE ? OR c.name LIKE ?)"
                count_params.extend([f"%{search}%", f"%{search}%"])
            
            cur.execute(count_query, count_params)
            total = cur.fetchone()["total"]

            has_more = len(items) > limit
            items_page = items[:limit]

            next_cursor = None
            if has_more and items_page:
                last = items_page[-1]
                if sort == "default":
                    next_cursor = f"{last.get('sort', 0)}|{last['serviceId']}"
                elif sort == "price_asc":
                    next_cursor = f"{last.get('price', 0)}|{last['serviceId']}"
                else:
                    next_cursor = f"{last.get('price', 0)}|{last['serviceId']}"

            return {
                "items": items_page,
                "nextCursor": next_cursor,
                "hasMore": has_more,
                "total": total
            }
    except Exception as e:
        print(f"❌ Error in get_services: {e}")
        return {
            "items": [],
            "nextCursor": None,
            "hasMore": False,
            "total": 0
        }

@app.get("/api/services/featured")
def get_featured_services():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT s.id as serviceId, s.title as serviceTitle, c.name as category,
                       s.sort, s.price, s.duration, s.is_special as isSpecial,
                       s.is_enabled as isEnabled, s.description
                FROM services s
                JOIN categories c ON s.category_id = c.id
                WHERE s.is_enabled = 1 AND s.is_special = 1
                ORDER BY s.sort ASC
                LIMIT 48
            """)
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"❌ Error in get_featured_services: {e}")
        return []

@app.get("/api/services/{service_id}", response_model=ServiceDetailResponse)
def get_service(service_id: str):
    try:
        with get_db() as conn:
            cur = conn.cursor()

            cur.execute("""
                SELECT s.*, c.name as category_name
                FROM services s
                JOIN categories c ON s.category_id = c.id
                WHERE s.id = ?
            """, (service_id,))
            row = cur.fetchone()

            if not row:
                raise HTTPException(status_code=404, detail="Service not found")

            row_dict = dict(row)

            # Parse JSON fields
            images = json.loads(row_dict.get("images", "[]"))
            forms = json.loads(row_dict.get("forms", "[]"))

            data = {
                "sort": row_dict.get("sort", 0),
                "price": row_dict.get("price", 0),
                "cost": row_dict.get("cost", 0),
                "benefit": row_dict.get("benefit", 0),
                "duration": row_dict.get("duration", ""),
                "isSpecial": bool(row_dict.get("is_special", 0)),
                "isEnabled": bool(row_dict.get("is_enabled", 1)),
                "type": row_dict.get("type", "selectable"),
                "description": row_dict.get("description", ""),
                "isPaymentRequired": bool(row_dict.get("is_payment_required", 1)),
                "isLocationBased": bool(row_dict.get("is_location_based", 1)),
                "autoInvoiceEnabled": bool(row_dict.get("auto_invoice_enabled", 1)),
                "isNoticeEnabled": bool(row_dict.get("is_notice_enabled", 0)),
                "noticeText": row_dict.get("notice_text"),
                "noticeImage": row_dict.get("notice_image"),
                "images": images,
                "forms": forms,
                "id": row_dict.get("id"),
                "title": row_dict.get("title"),
                "createdAt": row_dict.get("created_at"),
                "updatedAt": row_dict.get("updated_at"),
                "category": {
                    "title": row_dict.get("category_name"),
                    "type": "service",
                    "is_enabled": True
                }
            }

            return {
                "serviceId": row_dict.get("id"),
                "serviceTitle": row_dict.get("title"),
                "category": row_dict.get("category_name"),
                "sort": row_dict.get("sort", 0),
                "data": data
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in get_service: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────
#  BULK DATA IMPORT
# ─────────────────────────────────────────────────────────────
@app.post("/api/services/import")
def import_services(services: List[ServiceCreate]):
    results = {"imported": 0, "updated": 0, "failed": 0, "errors": []}

    try:
        with get_db() as conn:
            cur = conn.cursor()

            for svc in services:
                try:
                    # 1. Get or create category
                    cat_name = svc.category
                    cur.execute("SELECT id FROM categories WHERE name = ?", (cat_name,))
                    cat_row = cur.fetchone()

                    if not cat_row:
                        cur.execute("""
                            INSERT INTO categories (name, sort, is_enable, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (cat_name, 0, 1, datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
                        cat_id = cur.lastrowid
                    else:
                        cat_id = cat_row["id"]

                    # 2. Prepare service data
                    data = svc.data
                    service_id = svc.serviceId
                    title = svc.serviceTitle

                    price = data.get("price", 0)
                    cost = data.get("cost", 0)
                    benefit = data.get("benefit", 0)
                    duration = data.get("duration", "")
                    is_special = 1 if data.get("isSpecial", False) else 0
                    is_enabled = 1 if data.get("isEnabled", True) else 0
                    service_type = data.get("type", "selectable")
                    description = data.get("description", "")
                    is_payment_required = 1 if data.get("isPaymentRequired", True) else 0
                    is_location_based = 1 if data.get("isLocationBased", True) else 0
                    auto_invoice = 1 if data.get("autoInvoiceEnabled", True) else 0
                    is_notice = 1 if data.get("isNoticeEnabled", False) else 0
                    notice_text = data.get("noticeText")
                    notice_image = data.get("noticeImage")
                    images = json.dumps(data.get("images", []))
                    forms = json.dumps(data.get("forms", []))

                    # 3. Insert or update service
                    cur.execute("""
                        INSERT OR REPLACE INTO services (
                            id, title, category_id, sort, price, cost, benefit, duration,
                            is_special, is_enabled, type, description,
                            is_payment_required, is_location_based, auto_invoice_enabled,
                            is_notice_enabled, notice_text, notice_image, images, forms,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        service_id, title, cat_id, svc.sort, price, cost, benefit, duration,
                        is_special, is_enabled, service_type, description,
                        is_payment_required, is_location_based, auto_invoice,
                        is_notice, notice_text, notice_image, images, forms,
                        datetime.utcnow().isoformat(),
                        datetime.utcnow().isoformat()
                    ))

                    if cur.rowcount > 0:
                        results["updated"] += 1
                    else:
                        results["imported"] += 1

                except Exception as e:
                    results["failed"] += 1
                    results["errors"].append(f"Service {svc.serviceId}: {str(e)}")

            conn.commit()
        
        # Clear cache after import
        cache.clear()
        return results
    except Exception as e:
        print(f"❌ Error in import_services: {e}")
        results["errors"].append(f"Import failed: {str(e)}")
        return results

# ─────────────────────────────────────────────────────────────
#  PROVINCE & CITY ENDPOINTS
# ─────────────────────────────────────────────────────────────
PROVINCE_CITY_DATA = {
    "آذربایجان شرقی": ["تبریز", "مراغه", "مرند", "میانه", "اهر", "بناب", "سراب", "شبستر", "عجب‌شیر", "کلیبر", "هریس", "ورزقان", "چاراویماق", "خداآفرین", "جلفا", "هشترود"],
    "آذربایجان غربی": ["ارومیه", "خوی", "میاندوآب", "بوکان", "سردشت", "سلماس", "ماکو", "نقده", "پیرانشهر", "شاهین‌دژ", "تکاب", "چالدران", "چایپاره", "سیاه‌چشمه", "پلدشت", "اشنویه"],
    "اردبیل": ["اردبیل", "پارس‌آباد", "مشگین‌شهر", "خورشید", "بیله‌سوار", "گرمی", "نمین", "نیر", "کوثر", "سرعین"],
    "اصفهان": ["اصفهان", "کاشان", "خمینی‌شهر", "نجف‌آباد", "شاهین‌شهر", "فولادشهر", "مبارکه", "لنجان", "زرین‌شهر", "سپاهان‌شهر", "برخوار", "آران و بیدگل", "گلپایگان", "خوانسار", "فریدن", "فریدون‌شهر", "چادگان", "تیران و کرون", "نائین", "نطنز", "سمیرم", "دهاقان", "شهرضا", "میمه", "اصغرآباد"],
    "البرز": ["کرج", "نظرآباد", "اشتهارد", "طالقان", "ساوجبلاغ"],
    "ایلام": ["ایلام", "دهلران", "دره‌شهر", "آبدانان", "ایوان", "مهران", "چرداول", "ملکشاهی", "سیروان"],
    "بوشهر": ["بوشهر", "برازجان", "کنگان", "گناوه", "جم", "دشتستان", "دشتی", "تنگستان", "دیر", "عسلویه"],
    "تهران": ["تهران", "شهریار", "اسلامشهر", "قدس", "ملارد", "ورامین", "پاکدشت", "ری", "شمیرانات", "رباط‌کریم", "بهارستان", "قرچک", "پردیس", "فیروزکوه", "دماوند"],
    "چهارمحال و بختیاری": ["شهرکرد", "بروجن", "لردگان", "فارسان", "اردل", "کوهرنگ", "کیار", "سامان", "بن"],
    "خراسان جنوبی": ["بیرجند", "قائن", "نهبندان", "سربیشه", "درمیان", "خوسف", "طبس", "فردوس", "بشرویه", "زیرکوه", "سرایان"],
    "خراسان رضوی": ["مشهد", "نیشابور", "سبزوار", "کاشمر", "گناباد", "تربت حیدریه", "تربت جام", "خواف", "تایباد", "فریمان", "چناران", "کلات", "سرخس", "بردسکن", "بجستان", "جغتای", "داورزن", "رشتخوار", "زاوه", "مه‌ولات", "فیروزه", "باخرز"],
    "خراسان شمالی": ["بجنورد", "اسفراین", "شیروان", "قوچان", "مانه و سملقان", "جاجرم", "فاروج", "راز و جرگلان"],
    "خوزستان": ["اهواز", "دزفول", "آبادان", "خرمشهر", "بهبهان", "ایذه", "شوشتر", "دشت آزادگان", "شوش", "اندیمشک", "ماهشهر", "رامهرمز", "شادگان", "هندیجان", "بندر ماهشهر", "کارون", "باوی", "حمیدیه", "گتوند", "لالی", "مسجد سلیمان", "هفتگل", "امیدیه", "رامشیر", "هویزه"],
    "زنجان": ["زنجان", "ابهر", "خرمدره", "ماهنشان", "ایجرود", "طارم", "سلطانیه", "خدابنده"],
    "سمنان": ["سمنان", "شاهرود", "دامغان", "گرمسار", "میامی", "سرخه", "آرادان", "مهدی‌شهر"],
    "سیستان و بلوچستان": ["زاهدان", "چابهار", "زابل", "کنارک", "ایرانشهر", "خاش", "سراوان", "نیکشهر", "سرباز", "دلگان", "میرجاوه", "زهک", "هیرمند", "نیمروز", "بمپور", "راسک", "فنوج", "قصرقند", "سیب و سوران", "محمدآباد"],
    "فارس": ["شیراز", "مرودشت", "کازرون", "لارستان", "جهرم", "فسا", "داراب", "نی‌ریز", "سپیدان", "لامرد", "ممسنی", "زرقان", "پاسارگاد", "خرامه", "فراشبند", "اقلید", "بوانات", "استهبان", "خنج", "قیر و کارزین", "رستم", "سرچهان", "کوار", "مهر", "گراش", "جویم"],
    "قزوین": ["قزوین", "الوند", "آبیک", "بوئین‌زهرا", "تاکستان", "شال", "ضیاءآباد", "اقبالیه", "محمودآباد نمونه"],
    "قم": ["قم", "جعفرآباد", "کهک"],
    "کردستان": ["سنندج", "سقز", "مریوان", "بانه", "قروه", "بیجار", "دیواندره", "کامیاران", "سروآباد", "دهگلان", "زرینه"],
    "کرمان": ["کرمان", "رفسنجان", "سیرجان", "بم", "جیرفت", "کهنوج", "بردسیر", "زرند", "شهربابک", "راور", "عنبرآباد", "فاریاب", "فهرج", "قلعه‌گنج", "منوجان", "نرماشیر", "رودبار جنوب", "ریگان", "بافت", "کوهبنان"],
    "کرمانشاه": ["کرمانشاه", "اسلام‌آباد غرب", "سنقر", "هرسین", "کنگاور", "صحنه", "پاوه", "جوانرود", "سرپل ذهاب", "قصر شیرین", "دالاهو", "گیلانغرب", "روانسر", "ثلاث باباجانی"],
    "کهگیلویه و بویراحمد": ["یاسوج", "گچساران", "دوگنبدان", "کهگیلویه", "لنده", "بویراحمد", "دنا", "باشت", "چرام", "بهمئی"],
    "گلستان": ["گرگان", "گنبد کاووس", "علی‌آباد کتول", "کردکوی", "آق‌قلا", "بندر ترکمن", "مراوه‌تپه", "رامیان", "کلاله", "مینودشت", "گمیشان"],
    "گیلان": ["رشت", "انزلی", "لاهیجان", "فومن", "رودسر", "رودبار", "آستارا", "آستانه اشرفیه", "صومعه‌سرا", "تالش", "شفت", "رضوانشهر", "ماسال", "سیاهکل", "املش"],
    "لرستان": ["خرم‌آباد", "بروجرد", "دورود", "کوهدشت", "الیگودرز", "ازنا", "پلدختر", "دلفان", "سلسله", "چگنی", "رومشکان"],
    "مازندران": ["ساری", "بابل", "آمل", "قائم‌شهر", "بابلسر", "نوشهر", "چالوس", "تنکابن", "رامسر", "نور", "بهشهر", "نکا", "جویبار", "سیمرغ", "سوادکوه", "میاندرود", "فریدونکنار", "محمودآباد", "کلاردشت", "عباس‌آباد"],
    "مرکزی": ["اراک", "ساوه", "خمین", "محلات", "دلیجان", "تفرش", "فراهان", "شازند", "آشتیان", "زرندیه", "خنداب", "کمیجان"],
    "هرمزگان": ["بندرعباس", "میناب", "قشم", "کیش", "جاسک", "رودان", "حاجی‌آباد", "بندر لنگه", "بستک", "پارسیان", "خمیر", "سیریک", "ابوموسی"],
    "همدان": ["همدان", "ملایر", "نهاوند", "تویسرکان", "کبودرآهنگ", "اسدآباد", "رزن", "بهار", "فامنین", "درگزین"],
    "یزد": ["یزد", "میبد", "اردکان", "بافق", "مهریز", "ابرکوه", "تفت", "اشکذر", "خاتم", "بهاباد", "زارچ", "مرودشت", "نیر"]
}

@app.get("/api/provinces")
def get_provinces():
    return list(PROVINCE_CITY_DATA.keys())

@app.get("/api/cities/{province}")
def get_cities(province: str):
    return PROVINCE_CITY_DATA.get(province, [])

# ─────────────────────────────────────────────────────────────
#  DOC TYPES
# ─────────────────────────────────────────────────────────────
DOC_TYPES = [
    {"Id": 1, "title": "متن", "REx": ".*"},
    {"Id": 2, "title": "عدد", "REx": "^[0-9]+$"},
    {"Id": 3, "title": "تصویر", "REx": ".*\\.(jpg|jpeg|png|gif|svg)$"},
    {"Id": 4, "title": "شبا", "REx": "^IR[0-9]{24}$"},
    {"Id": 5, "title": "مبایل", "REx": "^09[0-9]{9}$"},
    {"Id": 6, "title": "کد ملی", "REx": "^[0-9]{10}$"},
    {"Id": 7, "title": "کد پستی", "REx": "^[0-9]{10}$"},
    {"Id": 8, "title": "تاریخ", "REx": "^\\d{4}-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\\d|3[01])$"}
]

@app.get("/api/doc-types")
def get_doc_types():
    return DOC_TYPES

# ─────────────────────────────────────────────────────────────
#  AUTH ENDPOINTS
# ─────────────────────────────────────────────────────────────
@app.post("/api/auth/otp/send")
def send_otp(req: OTPRequest):
    try:
        code = "12345"  # In production, generate random 5-digit code
        expires = (datetime.utcnow() + timedelta(minutes=5)).isoformat()

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO otp_codes (phone, code, expires_at, attempts)
                VALUES (?, ?, ?, 0)
            """, (req.phone, code, expires))
            conn.commit()

        print(f"📱 OTP for {req.phone}: {code}")
        return {"success": True, "message": "کد ارسال شد"}
    except Exception as e:
        print(f"❌ Error in send_otp: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/auth/otp/verify")
def verify_otp(req: OTPVerify):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT code, expires_at, attempts FROM otp_codes WHERE phone = ?", (req.phone,))
            row = cur.fetchone()

            if not row:
                raise HTTPException(status_code=400, detail="کد ارسال نشده است")

            attempts = row["attempts"] + 1
            cur.execute("UPDATE otp_codes SET attempts = ? WHERE phone = ?", (attempts, req.phone))
            conn.commit()

            if attempts > 5:
                cur.execute("DELETE FROM otp_codes WHERE phone = ?", (req.phone,))
                conn.commit()
                raise HTTPException(status_code=400, detail="تعداد تلاش بیش از حد مجاز")

            if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
                cur.execute("DELETE FROM otp_codes WHERE phone = ?", (req.phone,))
                conn.commit()
                raise HTTPException(status_code=400, detail="کد منقضی شده است")

            if row["code"] != req.otp:
                raise HTTPException(status_code=400, detail="کد اشتباه است")

            cur.execute("DELETE FROM otp_codes WHERE phone = ?", (req.phone,))
            conn.commit()

        # Create or get user
        user_id = f"user_{hashlib.md5(req.phone.encode()).hexdigest()[:16]}"
        token = create_token(user_id, req.phone)

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR IGNORE INTO users (id, phone, created_at, updated_at)
                VALUES (?, ?, ?, ?)
            """, (user_id, req.phone, datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
            cur.execute("UPDATE users SET token = ?, updated_at = ? WHERE id = ?",
                        (token, datetime.utcnow().isoformat(), user_id))
            conn.commit()

            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()

        user = dict(row)
        return {
            "success": True,
            "user": {
                "id": user["id"],
                "phone": user["phone"],
                "firstName": user["first_name"] or "",
                "lastName": user["last_name"] or "",
                "username": user["username"] or "",
                "telegramId": user["telegram_id"],
                "isPremium": bool(user["is_premium"]),
                "token": user["token"]
            },
            "token": token
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in verify_otp: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/auth/telegram-login")
def telegram_login(req: TelegramLogin):
    try:
        user_data = req.user
        tg_id = user_data.get("id")
        first_name = user_data.get("first_name", "")
        last_name = user_data.get("last_name", "")
        username = user_data.get("username", "")
        is_premium = user_data.get("is_premium", False)
        phone = user_data.get("phone_number", f"tg_{tg_id}")

        user_id = f"tg_{tg_id}"

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO users
                    (id, phone, first_name, last_name, username, telegram_id, is_premium, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                phone,
                first_name,
                last_name,
                username,
                tg_id,
                1 if is_premium else 0,
                datetime.utcnow().isoformat(),
                datetime.utcnow().isoformat()
            ))
            conn.commit()

            token = create_token(user_id, phone)
            cur.execute("UPDATE users SET token = ? WHERE id = ?", (token, user_id))
            conn.commit()

            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()

        user = dict(row)
        return {
            "success": True,
            "user": {
                "id": user["id"],
                "phone": user["phone"],
                "firstName": user["first_name"] or "",
                "lastName": user["last_name"] or "",
                "username": user["username"] or "",
                "telegramId": user["telegram_id"],
                "isPremium": bool(user["is_premium"]),
                "token": user["token"]
            },
            "token": token
        }
    except Exception as e:
        print(f"❌ Error in telegram_login: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    try:
        return {
            "id": user["id"],
            "phone": user["phone"],
            "firstName": user["first_name"] or "",
            "lastName": user["last_name"] or "",
            "username": user["username"] or "",
            "telegramId": user["telegram_id"],
            "isPremium": bool(user["is_premium"])
        }
    except Exception as e:
        print(f"❌ Error in get_me: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/auth/logout")
def logout(user: dict = Depends(get_current_user)):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE users SET token = NULL WHERE id = ?", (user["id"],))
            conn.commit()
        return {"success": True}
    except Exception as e:
        print(f"❌ Error in logout: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────
#  REQUEST ENDPOINTS
# ─────────────────────────────────────────────────────────────
@app.post("/api/requests")
def create_request(req: RequestCreate, user: dict = Depends(get_current_user)):
    try:
        req_id = f"req_{uuid.uuid4().hex[:16]}"
        now = datetime.utcnow().isoformat()

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO requests
                    (id, user_id, service_id, service_title, price, documents, receipt_image, status, submitted_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                req_id,
                user["id"],
                req.serviceId,
                req.serviceTitle,
                req.price,
                json.dumps(req.documents, ensure_ascii=False),
                req.receiptImage,
                "pending",
                now,
                now
            ))
            conn.commit()

        # Clear user requests cache
        cache.delete(f"user_requests_{user['id']}")
        return {"success": True, "requestId": req_id}
    except Exception as e:
        print(f"❌ Error in create_request: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/requests")
def get_requests(user: dict = Depends(get_current_user)):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM requests WHERE user_id = ? ORDER BY submitted_at DESC
            """, (user["id"],))
            rows = cur.fetchall()

            result = []
            for row in rows:
                r = dict(row)
                result.append({
                    "id": r["id"],
                    "userId": r["user_id"],
                    "serviceId": r["service_id"],
                    "serviceTitle": r["service_title"],
                    "price": r["price"],
                    "documents": json.loads(r["documents"] or "[]"),
                    "receiptImage": r["receipt_image"],
                    "status": r["status"],
                    "submittedAt": r["submitted_at"]
                })
            return result
    except Exception as e:
        print(f"❌ Error in get_requests: {e}")
        return []

@app.put("/api/requests/{request_id}/status")
def update_request_status(
    request_id: str,
    status: Literal["pending", "processing", "done", "rejected"],
    user: dict = Depends(get_current_user)
):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE requests
                SET status = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
            """, (status, datetime.utcnow().isoformat(), request_id, user["id"]))
            conn.commit()

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Request not found")

        # Clear cache
        cache.delete(f"user_requests_{user['id']}")
        return {"success": True, "status": status}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in update_request_status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────────────────────
#  SEED DATA
# ─────────────────────────────────────────────────────────────
def seed_data():
    try:
        with get_db() as conn:
            cur = conn.cursor()

            # Check if data exists
            cur.execute("SELECT COUNT(*) FROM categories")
            if cur.fetchone()[0] > 0:
                print("📦 Data already exists, skipping seed")
                return

            print("🌱 Seeding initial data...")

            # Seed categories
            categories = [
                (1, "اصناف و کسب‌وکارها", "🏢", 1, 1),
                (2, "بازگردانی مدارک گم شده", "📄", 2, 1),
                (3, "خدمات خودرو", "🚗", 3, 1),
                (4, "دانشگاه و دانشجو", "🎓", 4, 1),
                (5, "راهنمایی و رانندگی", "🚦", 5, 1),
                (6, "شرکت و برند", "🏷️", 6, 1),
                (7, "خدمات ملک و املاک", "🏠", 7, 1),
                (8, "کلیه خدمات قبض برق و کنتور برق", "⚡", 8, 1),
                (9, "کلیه خدمات قبض گاز و کنتور گاز", "🔥", 9, 1),
                (10, "کلیه خدمات قبض آب و کنتور آب", "💧", 10, 1),
                (11, "خدمات کمیته", "📋", 0, 1),
                (12, "خدمات بهزیستی", "🤝", 12, 1),
                (13, "امور بانکی و وام", "🏦", 13, 1),
                (14, "امور مالیاتی", "🧾", 14, 1),
                (15, "خدمات عمومی و اینترنتی", "🌐", 15, 1),
                (16, "فیش حقوقی", "💰", 16, 1),
                (17, "دادگاه خدمات قضایی", "⚖️", 17, 1),
                (18, "خدمات مدرسه و دانش اموز", "📚", 18, 1),
                (19, "سهام و عدالت و یارانه", "📊", 19, 1),
                (20, "خدمات پستی", "📮", 20, 1),
                (21, "خدمات پرداخت قبوض", "💳", 21, 1),
                (22, "استخدامی", "👔", 22, 1),
                (23, "پزشک و بیمار", "👨‍⚕️", 23, 1),
                (24, "خدمات فراجا", "👮", 24, 1),
                (25, "خدمات بیمه", "🛡️", 25, 1),
                (26, "خدمات مسافر بری", "🚌", 26, 1),
                (27, "ثبت احوال", "📝", 27, 1),
                (28, "خدمات بیمه روستایی", "🌾", 28, 1),
                (29, "هوش مصنوعی", "🤖", 29, 1),
                (30, "طراحی", "🎨", -1, 1),
                (31, "تولید مهر اصناف و شرکت ها", "🔏", 31, 1),
            ]

            for cat in categories:
                cur.execute("""
                    INSERT INTO categories (id, name, icon, sort, is_enable, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (cat[0], cat[1], cat[2], cat[3], cat[4],
                      datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))

            # Sample services
            sample_services = [
                (
                    "svc_001", "استعلام وضعیت درخواست پلیس اماکن",
                    1, 0, 60000, 0, 60000, "45 دقیقه",
                    0, 1, "selectable", "",
                    1, 1, 1, 0, None, None,
                    json.dumps([]),
                    json.dumps([{
                        "id": "form_001",
                        "title": "استعلام تاییدیه پلیس اماکن",
                        "description": "اگر برای واحد صنفی‌ت اعتراض یا درخواست تأییدیه پلیس اماکن ثبت کردی...",
                        "fields": [
                            {
                                "id": "fld_001",
                                "label": "کد ملی",
                                "key": "text_uvlx",
                                "type": "nationalCode",
                                "isRequired": True,
                                "placeholder": "کد ملی متقاضی رو وارد کنید",
                                "options": [],
                                "hasOther": False,
                                "validationRules": {"min": 10, "max": 10}
                            }
                        ]
                    }]))
            ]

            for svc in sample_services:
                cur.execute("""
                    INSERT INTO services (
                        id, title, category_id, sort, price, cost, benefit, duration,
                        is_special, is_enabled, type, description,
                        is_payment_required, is_location_based, auto_invoice_enabled,
                        is_notice_enabled, notice_text, notice_image, images, forms,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    *svc,
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat()
                ))

            conn.commit()
            print("✅ Seed complete!")
    except Exception as e:
        print(f"❌ Error in seed_data: {e}")

# ─────────────────────────────────────────────────────────────
#  HEALTH CHECK
# ─────────────────────────────────────────────────────────────
@app.get("/api/health")
def health_check():
    """Health check endpoint"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            db_status = "ok"
    except:
        db_status = "error"
    
    return {
        "status": "healthy" if db_status == "ok" else "unhealthy",
        "database": db_status,
        "cache": "ok",
        "timestamp": datetime.utcnow().isoformat()
    }

# ─────────────────────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    print("🚀 Starting Cafenet Online API...")
    init_db()
    seed_data()
    # Start cache cleanup thread
    def clean_cache():
        while True:
            time.sleep(60)
            cache.cleanup()
    threading.Thread(target=clean_cache, daemon=True).start()
    print("✅ Server started with optimized database and caching")
    print(f"📊 Database: {DB_PATH}")

# ─────────────────────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app", 
        host="0.0.0.0", 
        port=8000, 
        reload=True,
        workers=1,  # Use 1 for development, more for production
        limit_concurrency=100,
        backlog=2048
    )