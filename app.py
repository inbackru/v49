import os
import json
import logging
import requests

# Configure logging for debugging PDF generation
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session, abort, Blueprint, send_from_directory, send_file, make_response
from sqlalchemy import text, func
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect, validate_csrf
from werkzeug.exceptions import BadRequest

# Configure CSRF protection - ENABLED for security

# Import smart search
from smart_search import smart_search
from urllib.parse import unquote, quote
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
import secrets
import re
from email_service import send_notification, send_email
from flask_caching import Cache
import qrcode
import io
import base64
from PIL import Image

# Models and repositories will be imported after db initialization to avoid circular imports

def parse_address_components(address_display_name):
    """
    ИСПРАВЛЕННАЯ ФУНКЦИЯ: Парсит адрес в формате: Россия, Краснодарский край, Сочи, Кудепста м-н, Искры, 88 лит7
    Возвращает словарь с компонентами адреса
    """
    # ПОЛНАЯ ИНИЦИАЛИЗАЦИЯ РЕЗУЛЬТАТА
    result = {
        'country': None,
        'region': None, 
        'city': None,
        'district': None,
        'street': None,
        'house_number': None
    }
    
    if not address_display_name:
        return result
    
    # РАЗБИВАЕМ АДРЕС ПО ЗАПЯТЫМ
    parts = [part.strip() for part in address_display_name.split(',')]
    
    # ПРЯМОЕ ЗАПОЛНЕНИЕ ОСНОВНЫХ ЧАСТЕЙ
    if len(parts) >= 1:
        result['country'] = parts[0]  # Россия
        
    if len(parts) >= 2:
        result['region'] = parts[1]   # Краснодарский край
        
    if len(parts) >= 3:
        result['city'] = parts[2]     # Сочи
        
    # ОБРАБАТЫВАЕМ ОСТАВШИЕСЯ ЧАСТИ (район, улица, дом)
    if len(parts) >= 4:
        remaining_parts = parts[3:]  # ['Дагомыс', 'Российская', '26г стр']
        
        if len(remaining_parts) == 1:
            # Одна часть: может быть район или улица
            part = remaining_parts[0]
            if any(marker in part for marker in ['м-н', 'микрорайон', 'ЖК', 'жилой комплекс']):
                result['district'] = part
            else:
                result['street'] = part
                
        elif len(remaining_parts) == 2:
            # Две части: район+улица или улица+дом
            first_part, second_part = remaining_parts[0], remaining_parts[1]
            
            if any(marker in first_part for marker in ['м-н', 'микрорайон']):
                result['district'] = first_part
                result['street'] = second_part
            else:
                result['street'] = first_part
                result['house_number'] = second_part
                
        elif len(remaining_parts) == 3:
            # Три части: район, улица, дом
            result['district'] = remaining_parts[0]
            result['street'] = remaining_parts[1]
            result['house_number'] = remaining_parts[2]
            
        elif len(remaining_parts) >= 4:
            # Больше трех частей: район, улица, дом (остальное объединяем в дом)
            result['district'] = remaining_parts[0]
            result['street'] = remaining_parts[1]
            result['house_number'] = ', '.join(remaining_parts[2:])
    
    return result

# DEPRECATED: Legacy function for excel_properties table - not used with normalized Property model
# def update_parsed_addresses():
#     """
#     DEPRECATED: Обновляет ВСЕ поля parsed_* для всех записей в базе данных
#     на основе address_display_name
#     
#     NOTE: This function is no longer used with the normalized Property model.
#     Address parsing is handled directly in Property model fields.
#     """
#     pass

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)

# Create the app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Initialize CSRF protection after app creation - ENABLED FOR SECURITY
csrf = CSRFProtect(app)

# CSRF configuration (object defined at top of file)
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # 1 hour
app.config['WTF_CSRF_SSL_STRICT'] = False  # Allow non-HTTPS for development

# ИСПРАВЛЕНО (22.10.2025): Добавляем hasattr в Jinja2 для проверки типов пользователей
app.jinja_env.globals['hasattr'] = hasattr

# Add CSRF token to template context - ENABLED
@app.context_processor
def inject_csrf_token():
    from flask_wtf.csrf import generate_csrf
    return dict(csrf_token=generate_csrf)

def validate_json_csrf():
    """Validate CSRF token for JSON requests"""
    try:
        # For JSON requests, expect CSRF token in X-CSRFToken header
        token = request.headers.get('X-CSRFToken')
        if not token:
            return False
        validate_csrf(token)
        return True
    except Exception as e:
        return False

def require_json_csrf(f):
    """Decorator to require CSRF protection for JSON endpoints"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check CSRF for all dangerous HTTP methods with JSON content
        if request.method in ['POST', 'PUT', 'PATCH', 'DELETE'] and request.content_type == 'application/json':
            if not validate_json_csrf():
                return jsonify({'success': False, 'error': 'CSRF token missing or invalid'}), 400
        return f(*args, **kwargs)
    return decorated_function

# Русские названия месяцев для локализации
RUSSIAN_MONTHS = {
    1: 'января', 2: 'февраля', 3: 'марта', 4: 'апреля',
    5: 'мая', 6: 'июня', 7: 'июля', 8: 'августа',
    9: 'сентября', 10: 'октября', 11: 'ноября', 12: 'декабря'
}

@app.template_filter('russian_date')
def russian_date_filter(date_value):
    """Форматирует дату на русском языке"""
    if not date_value:
        return 'Недавно'
    
    if isinstance(date_value, str):
        return date_value
    
    day = date_value.day
    month = RUSSIAN_MONTHS.get(date_value.month, date_value.strftime('%B'))
    year = date_value.year
    
    return f"{day} {month} {year}"

@app.template_filter('msk_time')
def msk_time_filter(utc_datetime, format='%d.%m.%Y в %H:%M'):
    """Конвертирует UTC время в московское (MSK = UTC+3) и форматирует"""
    if not utc_datetime:
        return 'Недавно'
    
    if isinstance(utc_datetime, str):
        return utc_datetime
    
    from datetime import timedelta
    # Конвертируем UTC в MSK (UTC+3)
    msk_datetime = utc_datetime + timedelta(hours=3)
    return msk_datetime.strftime(format)

# Настройка супер-производительного кэширования
app.config['CACHE_TYPE'] = 'simple'
app.config['CACHE_DEFAULT_TIMEOUT'] = 300  # 5 минут
cache = Cache(app)

# Session configuration for Replit iframe environment
app.config['SESSION_COOKIE_HTTPONLY'] = True
# Session configuration for development
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Changed from None for better compatibility
app.config['SESSION_COOKIE_SECURE'] = False  # Changed to False for HTTP development
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24  # 24 hours
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Security: prevent JS access to cookies
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # 1 hour CSRF token timeout
app.config['WTF_CSRF_SSL_STRICT'] = False  # Allow development over HTTP

# Enable permanent sessions by default
from datetime import timedelta
app.permanent_session_lifetime = timedelta(hours=24)

# Configure the database
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///properties.db")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

# Configure file uploads
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Add route for uploaded files
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve uploaded files"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# Initialize the app with the extension
db.init_app(app)

# Import and register all models with SQLAlchemy after db initialization
with app.app_context():
    # Import all models explicitly to ensure they are registered with SQLAlchemy
    from models import (User, Manager, SavedSearch, SentSearch, BlogPost, BlogArticle, Category, 
                       ExcelProperty, Developer, ResidentialComplex, CashbackRecord, 
                       Application, Favorite, Notification, District, Street, RoomType, 
                       Admin, City, Region)
    db.create_all()
    print("Database tables created successfully!")

# Import repositories after db initialization to avoid circular imports
from repositories.property_repository import PropertyRepository, ResidentialComplexRepository, DeveloperRepository

# Import Manager for isinstance checks throughout the file
from models import Manager

# Add Jinja2 helper for creating slugs
@app.template_filter('slug')
def create_slug_filter(name):
    """Jinja2 filter for creating SEO-friendly slug from complex name"""
    return create_slug(name)

# Create API blueprint without login requirement
api_bp = Blueprint('api', __name__, url_prefix='/api')

# Debug endpoint removed for security - exposed session data

@api_bp.route('/properties/filter')
def api_properties_filter():
    """
    ✅ MIGRATED TO NORMALIZED TABLES (Property → ResidentialComplex → Developer)
    Unified API endpoint for filtering properties.
    Supports pagination, sorting, and returns properties with coordinates for map.
    
    Query parameters:
        - All filter parameters from build_property_filters()
        - page (int): Page number (default: 1)
        - per_page (int): Results per page (default: 20, max: 100)
        - sort (str): Sort type (price_asc, price_desc, area_asc, area_desc, date_desc)
    
    Returns:
        JSON with:
        - success (bool)
        - properties (list): Filtered properties with coordinates
        - total (int): Total count of filtered properties
        - page (int): Current page
        - per_page (int): Results per page
        - total_pages (int): Total pages
    """
    try:
        # Use unified filter function to parse request arguments
        where_conditions, params, filters_parsed = build_property_filters(request.args)
        
        # Convert build_property_filters() output to PropertyRepository filter format
        repo_filters = {
            'min_price': params.get('price_min'),
            'max_price': params.get('price_max'),
            'min_area': params.get('area_min'),
            'max_area': params.get('area_max'),
            'floor_min': params.get('floor_min'),
            'floor_max': params.get('floor_max'),
            'building_floors_min': params.get('building_floors_min'),
            'building_floors_max': params.get('building_floors_max'),
            'rooms': filters_parsed.get('rooms', []),
            'developer': filters_parsed.get('developer'),
            'developers': filters_parsed.get('developers', []),
            'district': filters_parsed.get('district'),
            'districts': filters_parsed.get('districts', []),
            'residential_complex': filters_parsed.get('residential_complex'),
            'building': filters_parsed.get('building'),
            'cashback_only': filters_parsed.get('cashback_only', False),
            'renovation': filters_parsed.get('renovation', []),
            'object_classes': filters_parsed.get('object_classes', []),
            'building_types': filters_parsed.get('building_types', []),
            'floor_options': filters_parsed.get('floor_options', []),
            'deal_type': filters_parsed.get('deal_type'),
            'search': filters_parsed.get('search')
        }
        
        # Remove None values from filters
        repo_filters = {k: v for k, v in repo_filters.items() if v is not None and v != [] and v != ''}
        
        # Pagination parameters
        page = request.args.get('page', default=1, type=int)
        page = max(1, page)
        per_page = request.args.get('per_page', default=20, type=int)
        per_page = min(max(1, per_page), 10000)  # Limit to 10000 max (for map view)
        offset = (page - 1) * per_page
        
        # Sorting
        sort_type = request.args.get('sort', 'price_asc').replace('_', '-').replace('-', '_')
        sort_by = 'price'
        sort_order = 'asc'
        
        if sort_type == 'price_desc':
            sort_by, sort_order = 'price', 'desc'
        elif sort_type == 'price_asc':
            sort_by, sort_order = 'price', 'asc'
        elif sort_type == 'area_asc':
            sort_by, sort_order = 'area', 'asc'
        elif sort_type == 'area_desc':
            sort_by, sort_order = 'area', 'desc'
        elif sort_type == 'date_desc':
            sort_by, sort_order = 'date', 'desc'
        
        # Get total count using PropertyRepository
        total = PropertyRepository.count_active(filters=repo_filters)
        
        # Get properties using PropertyRepository
        properties_orm = PropertyRepository.get_all_active(
            limit=per_page,
            offset=offset,
            filters=repo_filters,
            sort_by=sort_by,
            sort_order=sort_order
        )
        
        # Format properties for JSON response (maintain backward compatibility)
        properties = []
        for prop in properties_orm:
            # Parse gallery images
            main_image = 'https://via.placeholder.com/400x300?text=No+Photo'
            gallery = [main_image]
            
            if prop.gallery_images:
                try:
                    photos_raw = json.loads(prop.gallery_images)
                    if photos_raw and isinstance(photos_raw, list) and len(photos_raw) > 0:
                        main_image = photos_raw[0]
                        gallery = photos_raw[:5]
                except:
                    pass
            elif prop.main_image:
                main_image = prop.main_image
                gallery = [main_image]
            
            # Format room type
            rooms = int(prop.rooms or 0)
            room_type = "Студия" if rooms == 0 else f"{rooms}-комн"
            
            # Format floor info
            floor = int(prop.floor or 1)
            total_floors = int(prop.total_floors or floor)
            floor_text = f"{floor}/{total_floors}"
            
            # Get cashback rate from residential complex
            cashback_rate = None
            if prop.residential_complex:
                cashback_rate = float(prop.residential_complex.cashback_rate) if prop.residential_complex.cashback_rate else None
            
            # Build property object
            prop_dict = {
                'id': prop.inner_id or str(prop.id),
                'price': int(prop.price or 0),
                'area': float(prop.area or 0),
                'rooms': rooms,
                'room_type': room_type,
                'floor': floor_text,
                'floor_min': floor,
                'floor_max': total_floors,
                'title': prop.title or f"{room_type}, {prop.area} м², {floor_text} эт.",
                'address': prop.address or '',
                'complex_name': prop.residential_complex.name if prop.residential_complex else '',
                'residential_complex': prop.residential_complex.name if prop.residential_complex else '',
                'developer': prop.developer.name if prop.developer else '',
                'district': prop.district.name if prop.district else '',
                'image': main_image,
                'gallery': gallery,
                'object_class': '',  # Can add if needed
                'renovation': PropertyRepository.get_renovation_display_name(prop.renovation_type),
                'cashback_rate': cashback_rate,
                'cashback_available': bool(cashback_rate and cashback_rate > 0),
                'url': f'/object/{prop.inner_id or prop.id}',
                # Coordinates for map
                'lat': float(prop.latitude) if prop.latitude else None,
                'lon': float(prop.longitude) if prop.longitude else None,
                'coordinates': {
                    'lat': float(prop.latitude) if prop.latitude else None,
                    'lng': float(prop.longitude) if prop.longitude else None
                } if prop.latitude and prop.longitude else None
            }
            
            properties.append(prop_dict)
        
        total_pages = (total + per_page - 1) // per_page if total > 0 else 0
        
        return jsonify({
            'success': True,
            'properties': properties,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'filters_applied': filters_parsed
        })
        
    except Exception as e:
        print(f"❌ Error in /api/properties/filter: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e),
            'properties': [],
            'total': 0
        }), 500

@api_bp.route('/property/<int:property_id>/cashback')
def api_property_cashback(property_id):
    """✅ MIGRATED - Get cashback information using PropertyRepository"""
    try:
        # Get property using PropertyRepository
        prop = PropertyRepository.get_by_inner_id(property_id)
        
        if not prop:
            return jsonify({'success': False, 'error': 'Property not found'})
        
        # Extract data from Property model
        price = int(prop.price or 0)
        rooms = int(prop.rooms or 0)
        complex_name = prop.residential_complex.name if prop.residential_complex else "Не указан"
        
        # Get cashback rate from ResidentialComplex
        cashback_percent = 0
        if prop.residential_complex and prop.residential_complex.cashback_rate:
            cashback_percent = float(prop.residential_complex.cashback_rate)
        
        # Calculate cashback amount
        cashback_amount = price * (cashback_percent / 100)
        
        # Format property name
        room_text = f"{rooms}-комнатная квартира" if rooms > 0 else "Студия"
        property_name = f"{room_text} в ЖК «{complex_name}»"
        
        return jsonify({
            'success': True,
            'property_id': property_id,
            'property_name': property_name,
            'property_price': price,
            'cashback_percent': cashback_percent,
            'cashback_amount': int(cashback_amount),
            'complex_name': complex_name,
            'rooms': rooms
        })
        
    except Exception as e:
        print(f"Error getting property cashback: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Server error'})

# Custom Jinja2 filters
def street_slug(street_name):
    """Convert street name to URL slug with transliteration"""
    import re
    
    # Transliteration mapping for Russian to Latin
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
    }
    
    # Clean the name
    name = str(street_name).strip().lower()
    # Remove extra characters
    name = re.sub(r'[«»"\(\)\.,:;]', '', name)
    
    # Transliterate
    result = ''
    for char in name:
        result += translit_map.get(char, char)
    
    # Replace spaces with hyphens and clean up
    result = re.sub(r'\s+', '-', result)
    result = re.sub(r'-+', '-', result)
    result = result.strip('-')
    
    return result

def number_format(value):
    """Format number with space separators"""
    try:
        if isinstance(value, str):
            value = int(value)
        return f"{value:,}".replace(',', ' ')
    except (ValueError, TypeError):
        return str(value)

@app.template_filter('developer_slug')
def developer_slug(developer_name):
    """Convert developer name to URL slug with transliteration"""
    import re
    if not developer_name:
        return ""
    
    # Transliteration mapping for Russian to Latin
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'YO',
        'Ж': 'ZH', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
        'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
        'Ф': 'F', 'Х': 'KH', 'Ц': 'TS', 'Ч': 'CH', 'Ш': 'SH', 'Щ': 'SCH',
        'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'YU', 'Я': 'YA'
    }
    
    # Remove extra spaces and clean
    name = str(developer_name).strip()
    # Remove quotes, parentheses, dots, commas
    name = re.sub(r'[«»"\(\)\.,:;]', '', name)  
    
    # Transliterate cyrillic to latin
    result = ''
    for char in name:
        result += translit_map.get(char, char)
    
    # Replace spaces with hyphens and clean up
    result = re.sub(r'\s+', '-', result)  # Replace spaces with hyphens
    result = re.sub(r'-+', '-', result)   # Replace multiple hyphens with single
    result = result.strip('-')  # Remove leading/trailing hyphens
    return result.lower()

@app.template_filter('from_json')
def from_json_filter(json_string):
    """Парсит JSON строку в объект Python"""
    if not json_string:
        return []
    try:
        if isinstance(json_string, str):
            return json.loads(json_string)
        return json_string
    except (json.JSONDecodeError, TypeError):
        return []

@app.template_filter('crop_watermark')
def crop_watermark_filter(image_url, crop_percent=8):
    """
    Convert image URL to proxied URL with watermark cropped
    Usage in template: {{ image_url | crop_watermark(10) }}
    """
    if not image_url:
        return image_url
    
    from urllib.parse import quote
    encoded_url = quote(image_url, safe='')
    return f"/api/image-proxy?url={encoded_url}&crop={crop_percent}"

def format_room_display(rooms):
    """Format room count for display"""
    if rooms == 0:
        return "Студия"
    else:
        return f"{rooms}-комнатная квартира"

app.jinja_env.filters['street_slug'] = street_slug
app.jinja_env.filters['number_format'] = number_format
app.jinja_env.filters['developer_slug'] = developer_slug

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'  # type: ignore
login_manager.login_message = 'Войдите в аккаунт для доступа к этой странице.'
login_manager.login_message_category = 'info'



# Property data loading functions with cache
_properties_cache = None
_cache_timestamp = None
CACHE_TIMEOUT = 300  # 5 minutes

def load_properties():
    """✅ MIGRATED TO NORMALIZED TABLES: Load properties from Property → ResidentialComplex → Developer"""
    global _properties_cache, _cache_timestamp
    import time
    
    # Check if we have valid cached data
    if (_properties_cache is not None and _cache_timestamp is not None and 
        time.time() - _cache_timestamp < CACHE_TIMEOUT):
        # Cache hit - fast path
        return _properties_cache
    
    # Ensure we have app context
    from flask import has_app_context
    if not has_app_context():
        with app.app_context():
            return load_properties()
    
    try:
        # ✅ MIGRATED: Load from normalized tables using PropertyRepository
        properties = PropertyRepository.get_all_active(
            limit=10000,  # Get all properties
            filters={'min_price': 1},  # price > 0
            sort_by='price',
            sort_order='asc'
        )
        
        excel_properties = properties
        
        if excel_properties and len(excel_properties) > 0:
            # ✅ MIGRATED: No need for max_floors query, data is in normalized tables
            
            # Convert Property ORM objects to dictionary format (backward compatibility)
            db_properties = []
            for prop in excel_properties:
                # Convert ORM object to dict-like structure
                # Property has relationships: residential_complex, developer, district
                complex_obj = prop.residential_complex
                developer_obj = prop.developer
                district_obj = prop.district
                
                prop_dict = {
                    'inner_id': prop.inner_id,
                    'complex_name': complex_obj.name if complex_obj else 'ЖК Без названия',
                    'developer_name': developer_obj.name if developer_obj else 'Не указан',
                    'object_rooms': prop.rooms,
                    'object_area': prop.area,
                    'price': prop.price,
                    'object_min_floor': prop.floor,
                    'object_max_floor': prop.total_floors,
                    'address_display_name': prop.address,
                    'address_position_lat': prop.latitude,
                    'address_position_lon': prop.longitude,
                    'address_locality_display_name': district_obj.name if district_obj else '',
                    'photos': prop.gallery_images or '[]',
                    'complex_object_class_display_name': complex_obj.object_class_display_name if complex_obj else '',
                    'renovation_type': prop.renovation_type,
                    'renovation_display_name': PropertyRepository.get_renovation_display_name(prop.renovation_type),
                    'complex_with_renovation': bool(prop.renovation_type),
                    'complex_building_end_build_year': complex_obj.end_build_year if complex_obj else None,
                    'complex_building_end_build_quarter': complex_obj.end_build_quarter if complex_obj else None,
                    'complex_building_name': prop.complex_building_name,
                    'address_subways': None,
                    'trade_in': False,  # Not in Property model yet
                    'deal_type': prop.deal_type,
                    'square_price': prop.price_per_sqm,
                    'mortgage_price': None,  # Not in Property model yet
                    'object_is_apartment': True,
                    'max_price': prop.price,
                    'min_price': prop.price,
                    'complex_has_green_mortgage': False,  # Not in Property model yet
                    'placement_type': '',  # Not in Property model yet
                    'description': prop.description,
                    'parsed_district': district_obj.name if district_obj else '',
                    'parsed_city': 'Краснодар',  # Default city
                    'complex_id': prop.complex_id,
                    'complex_min_rate': complex_obj.cashback_rate if complex_obj else 0
                }
                
                # Parse photos field (JSON array format)
                photos_raw = prop_dict.get('photos', '')
                main_image = '/static/images/no-photo.jpg'
                
                if photos_raw and photos_raw.strip():
                    try:
                        # Try to parse as JSON array first (current database format)
                        if photos_raw.startswith('[') and photos_raw.endswith(']'):
                            images = json.loads(photos_raw)
                            if images and isinstance(images, list) and len(images) > 0:
                                main_image = images[0].strip() if images[0] else '/static/images/no-photo.jpg'
                        # Fallback: PostgreSQL array format {url1,url2,url3}
                        elif photos_raw.startswith('{') and photos_raw.endswith('}'):
                            images_str = photos_raw[1:-1]  # Remove braces
                            if images_str:
                                images = [img.strip().strip('"') for img in images_str.split(',') if img.strip()]
                                main_image = images[0] if images else '/static/images/no-photo.jpg'
                        # Single image URL
                        else:
                            main_image = photos_raw.strip()
                    except (json.JSONDecodeError, ValueError, IndexError) as e:
                        print(f"Error parsing photos for property {prop_dict.get('inner_id')}: {e}")
                        main_image = '/static/images/no-photo.jpg'
                
                # ✅ MIGRATED: Get total floors directly from Property object (already in normalized tables)
                complex_total_floors = prop_dict.get('object_max_floor', 1)
                
                # Format property data  
                rooms = prop_dict.get('object_rooms', 0)
                area = prop_dict.get('object_area', 0) 
                floor = prop_dict.get('object_min_floor') or '—'
                
                # Format floor display (handle None values)
                if floor != '—' and complex_total_floors and complex_total_floors != 1:
                    floor_display = f"{floor}/{complex_total_floors} эт."
                elif floor != '—':
                    floor_display = f"{floor} эт."
                else:
                    floor_display = "Этаж не указан"
                
                # Create title with proper format: "Студия, 23.40 м², 1/12 эт."
                if rooms == 0:
                    title = f"Студия, {area} м², {floor_display}"
                else:
                    title = f"{rooms}-комн, {area} м², {floor_display}"
                
                # Enhanced completion date from building data
                completion_date = 'Не указана'
                if prop_dict.get('complex_building_end_build_year') and prop_dict.get('complex_building_end_build_quarter'):
                    year = prop_dict.get('complex_building_end_build_year')
                    quarter = prop_dict.get('complex_building_end_build_quarter')
                    completion_date = f"{quarter} кв. {year} г."
                elif prop_dict.get('complex_building_end_build_year'):
                    year = prop_dict.get('complex_building_end_build_year')  
                    completion_date = f"{year} г."
                
                # Enhanced finishing information
                finishing = prop_dict.get('renovation_display_name') or prop_dict.get('renovation_type', 'Не указана')
                if prop_dict.get('complex_with_renovation'):
                    finishing = finishing if finishing != 'Не указана' else 'С отделкой'
                
                formatted_prop = {
                    'id': prop_dict.get('inner_id'),
                    'title': title,
                    'rooms': prop_dict.get('object_rooms', 0),
                    'area': prop_dict.get('object_area', 0),
                    'price': prop_dict.get('price', 0),
                    # Use database square_price if available, fallback to calculation
                    'price_per_sqm': prop_dict.get('square_price') or (int(prop_dict.get('price', 0) / prop_dict.get('object_area', 1)) if prop_dict.get('object_area', 0) > 0 else 0),
                    'floor': prop_dict.get('object_min_floor', 1),
                    'total_floors': complex_total_floors,
                    'address': prop_dict.get('address_display_name', ''),
                    'coordinates': {
                        'lat': float(prop_dict.get('address_position_lat', 45.0448)),
                        'lng': float(prop_dict.get('address_position_lon', 38.9728))
                    },
                    'cashback': calculate_cashback(
                        prop_dict.get('price', 0),
                        complex_id=prop_dict.get('complex_id'),
                        complex_name=prop_dict.get('complex_name')
                    ),
                    'cashback_rate': float(prop_dict.get('complex_min_rate', 0)) if prop_dict.get('complex_min_rate') else 0,
                    'cashback_available': True,
                    'status': 'available',
                    'property_type': 'Квартира' if prop_dict.get('object_is_apartment', True) else 'Недвижимость',
                    'developer': prop_dict.get('developer_name', 'Не указан'),
                    'residential_complex': prop_dict.get('complex_name', 'ЖК Без названия'),
                    'district': prop_dict.get('parsed_district') or prop_dict.get('parsed_city') or prop_dict.get('address_locality_display_name', 'Район не указан'),
                    'main_image': main_image,
                    'url': f"/object/{prop_dict.get('inner_id')}",
                    'complex_name': prop_dict.get('complex_name', 'ЖК Без названия'),
                    'type': 'property',
                    # NEW ENHANCED FIELDS FROM DATABASE:
                    'finishing': finishing,
                    'renovation_type': prop_dict.get('renovation_type'),
                    'completion_date': completion_date,
                    'complex_class': prop_dict.get('complex_object_class_display_name', ''),
                    'building_name': prop_dict.get('complex_building_name', ''),
                    'nearest_metro': prop_dict.get('address_subways', ''),
                    'trade_in_available': bool(prop_dict.get('trade_in', False)),
                    'deal_type': prop_dict.get('deal_type', ''),
                    'mortgage_price': prop_dict.get('mortgage_price'),
                    'max_price': prop_dict.get('max_price'),
                    'min_price': prop_dict.get('min_price'),
                    'green_mortgage_available': bool(prop_dict.get('complex_has_green_mortgage', False)),
                    'placement_type': prop_dict.get('placement_type', ''),
                    'description': prop_dict.get('description', ''),
                    'complex_with_renovation': bool(prop_dict.get('complex_with_renovation', False))
                }
                db_properties.append(formatted_prop)
            
            # Successfully loaded properties from database
            # Cache the data
            _properties_cache = db_properties  
            _cache_timestamp = time.time()
            return db_properties
            
    except Exception as e:
        # Database error logged  
        print(f"CRITICAL: load_properties() database error: {e}")
        import traceback
        traceback.print_exc()
        pass
        
    # No fallback - only database data from now on
    # No properties found
    return []

def load_residential_complexes():
    """Load residential complexes from database enriched with statistics from excel_properties"""
    try:
        # First try to load from database
        from models import ResidentialComplex, Developer, District
        
        complexes = ResidentialComplex.query.all()
        
        if complexes and len(complexes) > 0:
            # Convert database complexes to dictionary format
            db_complexes = []
            for complex in complexes:
                # ✅ MIGRATED: Enrich with statistics from normalized Property table
                try:
                    from models import Property
                    stats_query = (
                        db.session.query(
                            func.min(Property.price).label('min_price'),
                            func.max(Property.price).label('max_price'),
                            func.count(Property.id).label('apartments_count')
                        )
                        .filter(Property.complex_id == complex.id, Property.is_active == True)
                        .first()
                    )
                    
                    min_price = stats_query.min_price if stats_query else None
                    max_price = stats_query.max_price if stats_query else None
                    apartments_count = int(stats_query.apartments_count) if stats_query and stats_query.apartments_count else 0
                except Exception as e:
                    print(f"Warning: Could not load stats for complex {complex.id}: {e}")
                    db.session.rollback()  # Rollback failed transaction
                    min_price = None
                    max_price = None
                    apartments_count = 0
                
                complex_dict = {
                    'id': complex.id,
                    'name': complex.name,
                    'slug': complex.slug,
                    'district': complex.district.name if complex.district else 'Не указан',
                    'developer': complex.developer.name if complex.developer else 'Не указан',
                    'cashback_rate': complex.cashback_rate or 5.0,
                    'cashback_percent': complex.cashback_rate or 5.0,
                    'class': complex.object_class_display_name or 'Комфорт',
                    'description': f'ЖК от застройщика {complex.developer.name if complex.developer else "Не указан"}',
                    'start_year': complex.start_build_year,
                    'completion_year': complex.end_build_year,
                    'quarter': complex.end_build_quarter,
                    'features': {
                        'accreditation': complex.has_accreditation,
                        'green_mortgage': complex.has_green_mortgage,
                        'big_check': complex.has_big_check,
                        'with_renovation': complex.with_renovation,
                        'financing_sber': complex.financing_sber,
                    },
                    'phones': {
                        'complex': complex.complex_phone,
                        'sales': complex.sales_phone,
                    },
                    'sales_address': complex.sales_address,
                    'image': 'https://via.placeholder.com/800x600/0088CC/FFFFFF?text=' + complex.name.replace(' ', '+'),  # Placeholder for now
                    'address': complex.sales_address or 'Адрес уточняется',
                    'location': complex.sales_address or 'Краснодар',  # Add missing location field
                    # Add statistics from excel_properties
                    'min_price': min_price,
                    'price_from': min_price,
                    'max_price': max_price,
                    'price_to': max_price,
                    'apartments_count': apartments_count,
                    'properties_count': apartments_count,
                }
                db_complexes.append(complex_dict)
            
            # Complexes loaded successfully
            return db_complexes
            
    except Exception as e:
        # Error loading complexes
        print(f"Error in load_residential_complexes: {e}")
        import traceback
        traceback.print_exc()
    
    # No fallback - only database data from now on
    # No complexes found
    return []

def load_blog_articles():
    """Load blog articles from JSON file"""
    try:
        with open('data/blog_articles.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def load_blog_categories():
    """Load blog categories from JSON file"""
    try:
        with open('data/blog_categories.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def load_search_data():
    """Load search data from JSON file"""
    try:
        with open('data/search_data.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def load_streets():
    """Load streets from JSON file"""
    try:
        with open('data/streets.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def load_developers():
    """Load developers from residential complexes data"""
    try:
        complexes = load_residential_complexes()
        developers = {}
        
        for complex in complexes:
            dev_name = complex.get('developer', 'Неизвестный застройщик')
            if dev_name not in developers:
                developers[dev_name] = {
                    'name': dev_name,
                    'projects_count': 0,
                    'complexes': []
                }
            developers[dev_name]['projects_count'] += 1
            developers[dev_name]['complexes'].append(complex['name'])
        
        return list(developers.values())
    except Exception:
        return []

def search_global(query):
    """Global search across all types: ЖК, districts, developers, streets"""
    if not query or len(query.strip()) < 2:
        return []
    
    search_data = load_search_data()
    results = []
    query_lower = query.lower().strip()
    
    # Search through all categories
    for category in ['residential_complexes', 'districts', 'developers', 'streets']:
        items = search_data.get(category, [])
        for item in items:
            # Search in name and keywords
            name_match = query_lower in item['name'].lower()
            keyword_match = any(query_lower in keyword.lower() for keyword in item.get('keywords', []))
            
            if name_match or keyword_match:
                # Calculate relevance score
                score = 0
                if query_lower in item['name'].lower():
                    score += 10  # Higher score for name matches
                if query_lower == item['name'].lower():
                    score += 20  # Even higher for exact matches
                    
                result = {
                    'id': item['id'],
                    'name': item['name'],
                    'type': item['type'],
                    'url': item['url'],
                    'score': score
                }
                
                # Add additional context based on type
                if item['type'] == 'residential_complex':
                    result['district'] = item.get('district', '')
                    result['developer'] = item.get('developer', '')
                elif item['type'] == 'street':
                    result['district'] = item.get('district', '')
                    
                results.append(result)
    
    # Sort by relevance score (highest first)
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:10]  # Return top 10 results

def get_article_by_slug(slug):
    """Get a single article by slug"""
    articles = load_blog_articles()
    for article in articles:
        if article['slug'] == slug:
            return article
    return None

def search_articles(query, category=None):
    """Search articles by title, excerpt, content, and tags"""
    articles = load_blog_articles()
    if not query and not category:
        return articles
    
    filtered_articles = []
    for article in articles:
        # Filter by category if specified
        if category and article['category'].lower() != category.lower():
            continue
        
        # If no search query, return all articles in category
        if not query:
            filtered_articles.append(article)
            continue
        
        # Search in title, excerpt, content, and tags
        query_lower = query.lower()
        if (query_lower in article['title'].lower() or 
            query_lower in article['excerpt'].lower() or 
            query_lower in article['content'].lower() or 
            any(query_lower in tag.lower() for tag in article['tags'])):
            filtered_articles.append(article)
    
    return filtered_articles

def _extract_first_photo(photos_json):
    """Extract first photo from photos JSON string"""
    if not photos_json:
        return None
    
    try:
        import json
        if isinstance(photos_json, str):
            photos_list = json.loads(photos_json)
        else:
            photos_list = photos_json
            
        return photos_list[0] if photos_list and len(photos_list) > 0 else None
    except:
        return None

def calculate_cashback(price, complex_id=None, complex_name=None):
    """Calculate cashback amount based on property price and complex cashback rate"""
    if not price or price == 0:
        return 0
    
    try:
        from models import ResidentialComplex
        
        # If complex_id provided, use its cashback rate from database (backwards compatibility)
        if complex_id:
            complex_obj = ResidentialComplex.query.filter_by(id=str(complex_id)).first()
            if complex_obj and complex_obj.cashback_rate:
                rate = float(complex_obj.cashback_rate) / 100  # Convert percentage to decimal
                return int(price * rate)
        
        # If complex_name provided, look up by name
        if complex_name:
            complex_obj = ResidentialComplex.query.filter_by(name=complex_name).first()
            if complex_obj and complex_obj.cashback_rate:
                rate = float(complex_obj.cashback_rate) / 100  # Convert percentage to decimal
                return int(price * rate)
    except Exception as e:
        print(f"Error getting complex cashback rate: {e}")
    
    # Fallback to default 5% calculation if no complex found or error
    return int(price * 0.05)  # 5% default cashback

def get_property_by_id(property_id):
    """✅ MIGRATED TO NORMALIZED TABLES: Get property from Property → ResidentialComplex → Developer"""
    try:
        # ✅ MIGRATED: Use PropertyRepository to get property with all relationships
        prop = PropertyRepository.get_by_inner_id(property_id)
        
        if not prop:
            return None
        
        # Extract related objects
        complex_obj = prop.residential_complex
        developer_obj = prop.developer
        district_obj = prop.district
        
        # Map to old variable names for backward compatibility
        inner_id = prop.inner_id
        price = prop.price
        area = prop.area
        rooms = prop.rooms
        min_floor = prop.floor
        max_floor = prop.total_floors
        address = prop.address
        renovation = PropertyRepository.get_renovation_display_name(prop.renovation_type)
        cashback_rate = complex_obj.cashback_rate if complex_obj else 0
        square_price = prop.price_per_sqm
        mortgage_price = None  # Not in Property model yet
        class_type = complex_obj.object_class_display_name if complex_obj else None
        photos = prop.gallery_images
        developer_name = developer_obj.name if developer_obj else None
        complex_name = complex_obj.name if complex_obj else None
        complex_end_year = complex_obj.end_build_year if complex_obj else None
        complex_end_quarter = complex_obj.end_build_quarter if complex_obj else None
        building_end_year = complex_obj.end_build_year if complex_obj else None
        building_end_quarter = complex_obj.end_build_quarter if complex_obj else None
        lat = prop.latitude
        lon = prop.longitude
        description = prop.description
        district_name = district_obj.name if district_obj else None
        
        # Parse photos JSON
        images = []
        floor_plan = None
        complex_photos = []
        
        try:
            if photos:
                photos_data = json.loads(photos)
                if isinstance(photos_data, list):
                    # If it's a simple list of photo URLs (like in this case)
                    images = photos_data
                    # First photo is always the floor plan
                    if len(images) > 0:
                        floor_plan = images[0]  # First image is floor plan
                elif isinstance(photos_data, dict):
                    # Get apartment gallery photos from dict structure
                    images = photos_data.get('apartment_gallery', [])
                    # Get floor plan 
                    floor_plans = photos_data.get('floor_plans', [])
                    if floor_plans and len(floor_plans) > 0:
                        floor_plan = floor_plans[0]  # Take first floor plan
                    # Get complex photos
                    complex_photos = photos_data.get('complex_gallery', [])
        except Exception as e:
            print(f"Error parsing photos for property {property_id}: {e}")
            pass
        
        # Build completion date
        completion_date = 'Уточняется'
        if building_end_year and building_end_quarter:
            completion_date = f"{building_end_year} г., {building_end_quarter} кв."
        elif building_end_year:
            completion_date = f"{building_end_year} г."
        elif complex_end_year:
            completion_date = f"{complex_end_year} г."
        
        # Create property data structure matching PDF template expectations
        property_data = {
            'id': inner_id,
            'title': f"{'Студия' if rooms == 0 else f'{rooms}-к. квартира'}, {area} м²",
            'price': price or 0,
            'area': area or 0,
            'rooms': rooms or 0,
            'floor': min_floor or 1,
            'total_floors': max_floor or min_floor or 1,
            'address': address or f"{district_name}, Краснодар" if district_name else 'Краснодар',
            'developer': developer_name or 'Не указан',
            'residential_complex': complex_name or 'Не указан',
            'district': district_name or 'Краснодар',
            'status': 'Свободна',
            'property_type': 'Студия' if rooms == 0 else 'Квартира',
            'renovation_type': renovation or 'Уточняется',
            'finishing': renovation or 'Предчистовая',
            'completion_date': completion_date,
            'cashback_rate': cashback_rate or 0,
            'mortgage_rate': f"{cashback_rate}%" if cashback_rate else '3.5%',
            'square_price': square_price,
            'mortgage_payment': mortgage_price,
            'class_type': class_type or 'Не указан',
            'description': description or '',
            'residential_complex_description': f"Современный жилой комплекс от застройщика {developer_name}" if developer_name else None,
            'mortgage_available': True,
            'installment_available': False,
            'cashback_available': True,
            # Photos for PDF
            'image': images[0] if images else None,  # Main photo
            'gallery': images,  # All apartment photos
            'floor_plan': floor_plan,  # Floor plan photo
            'complex_photos': complex_photos,  # Complex photos
            # Additional fields expected by PDF template
            'bathroom_type': 'Совмещенный',
            'has_balcony': True,
            'windows_type': 'Пластиковые', 
            'elevators': '2 пассажирских',
            'parking_type': 'Наземная',
            'developer_inn': 'ИНН не указан',
            'complex_name': complex_name  # Add complex_name for cashback calculation
        }
        
        return property_data
        
    except Exception as e:
        print(f"Error getting property {property_id}: {e}")
        return None

def get_filtered_properties(filters):
    """Filter properties based on criteria including regional filters"""
    properties = load_properties()
    filtered = []
    
    for prop in properties:
        # Keywords filter (для типов недвижимости, классов, материалов)
        if filters.get('keywords') and len(filters['keywords']) > 0:
            keywords_matched = False
            for keyword in filters['keywords']:
                keyword_lower = keyword.lower()
                
                # Check property type
                prop_type_lower = prop.get('property_type', 'Квартира').lower()
                if keyword_lower == 'дом' and prop_type_lower == 'дом':
                    keywords_matched = True
                    break
                elif keyword_lower == 'таунхаус' and prop_type_lower == 'таунхаус':
                    keywords_matched = True
                    break
                elif keyword_lower == 'пентхаус' and prop_type_lower == 'пентхаус':
                    keywords_matched = True
                    break
                elif keyword_lower == 'апартаменты' and prop_type_lower == 'апартаменты':
                    keywords_matched = True
                    break
                elif keyword_lower == 'студия' and (prop_type_lower == 'студия' or prop.get('rooms') == 0):
                    keywords_matched = True
                    break
                elif keyword_lower == 'квартира' and prop_type_lower == 'квартира':
                    keywords_matched = True
                    break
                
                # Check property class
                elif keyword_lower == prop.get('property_class', '').lower():
                    keywords_matched = True
                    break
                
                # Check wall material
                elif keyword_lower in prop.get('wall_material', '').lower():
                    keywords_matched = True
                    break
                
                # Check features
                elif any(keyword_lower in feature.lower() for feature in prop.get('features', [])):
                    keywords_matched = True
                    break
                
                # Check in property type as fallback  
                elif keyword_lower in (f"{prop.get('rooms', 0)}-комн" if prop.get('rooms', 0) > 0 else "студия").lower():
                    keywords_matched = True
                    break
                    
            if not keywords_matched:
                continue
        
        # Text search with improved room number matching and word-based search
        if filters.get('search'):
            search_term = filters['search'].lower()
            
            # Create multiple variations for room descriptions
            rooms = prop.get('rooms', 0)
            if rooms == 0:
                room_variations = ["студия", "studio"]
            else:
                room_variations = [
                    f"{rooms}-комн",
                    f"{rooms}-комнатная",
                    f"{rooms} комн",
                    f"{rooms} комнатная"
                ]
                
                # Add spelled out numbers for 1-3 rooms
                if rooms == 1:
                    room_variations.extend(["однокомнатная", "1-комнатная", "одна комната"])
                elif rooms == 2:
                    room_variations.extend(["двухкомнатная", "2-комнатная", "две комнаты"])
                elif rooms == 3:
                    room_variations.extend(["трехкомнатная", "3-комнатная", "три комнаты"])
            
            # Create searchable text with all variations
            property_title = f"{prop.get('rooms', 0)}-комн" if prop.get('rooms', 0) > 0 else "студия"
            searchable_text = f"{property_title} {' '.join(room_variations)} {prop.get('developer_name', prop.get('developer', ''))} {prop.get('address_locality_name', prop.get('district', ''))} {prop.get('complex_name', prop.get('residential_complex', ''))} {prop.get('location', '')} квартира".lower()
            
            # Split search term into words and check if all words are found
            search_words = search_term.split()
            match_found = True
            
            for word in search_words:
                if word not in searchable_text:
                    match_found = False
                    break
            
            if not match_found:
                continue
        
        # Rooms filter - handle both single value and array
        if filters.get('rooms'):
            rooms_filter = filters['rooms']
            # ✅ ИСПРАВЛЕНО: используем object_rooms вместо rooms
            property_rooms = prop.get('object_rooms', prop.get('rooms', 0))
            
            # Helper function to parse room filter value
            def parse_room_filter(room_value):
                """Convert room filter to integer: '2-комн' -> 2, 'студия' -> 0, '2' -> 2"""
                if not room_value:
                    return None
                room_str = str(room_value).lower().strip()
                
                # Handle special cases
                if room_str in ['студия', 'studio']:
                    return 0
                if room_str in ['4+-комн', '4+', '4+ комнат']:
                    return 4  # Will be handled as >= 4
                
                # Handle "X-комн" format
                if '-комн' in room_str:
                    try:
                        return int(room_str.split('-')[0])
                    except (ValueError, IndexError):
                        return None
                
                # Handle numeric string
                try:
                    return int(room_str)
                except (ValueError, TypeError):
                    return None
            
            # Handle array of rooms from saved searches
            if isinstance(rooms_filter, list):
                rooms_match = False
                for room_filter in rooms_filter:
                    room_num = parse_room_filter(room_filter)
                    if room_num is None:
                        continue
                    
                    # Special case for 4+ rooms
                    if str(room_filter).lower() in ['4+-комн', '4+', '4+ комнат']:
                        if property_rooms >= 4:
                            rooms_match = True
                            break
                    # Exact match
                    elif property_rooms == room_num:
                        rooms_match = True
                        break
                
                if not rooms_match:
                    continue
            else:
                # Handle single room value
                room_num = parse_room_filter(rooms_filter)
                if room_num is None:
                    continue
                    
                # Special case for 4+ rooms
                if str(rooms_filter).lower() in ['4+-комн', '4+', '4+ комнат']:
                    if property_rooms < 4:
                        continue
                # Exact match
                elif property_rooms != room_num:
                    continue
        
        # Price filter - handle both raw rubles and millions
        if filters.get('price_min') and filters['price_min']:
            try:
                min_price = int(filters['price_min'])
                # If value is small, assume it's in millions
                if min_price < 1000:
                    min_price = min_price * 1000000
                if prop['price'] < min_price:
                    continue
            except (ValueError, TypeError):
                pass
        if filters.get('price_max') and filters['price_max']:
            try:
                max_price = int(filters['price_max'])
                # If value is small, assume it's in millions
                if max_price < 1000:
                    max_price = max_price * 1000000
                if prop['price'] > max_price:
                    continue
            except (ValueError, TypeError):
                pass
        
        # District filter
        if filters.get('district') and prop['district'] != filters['district']:
            continue
        
        # Developer filter
        if filters.get('developer') and prop['developer'] != filters['developer']:
            continue
        
        # Residential complex filter
        if filters.get('residential_complex'):
            residential_complex = filters['residential_complex'].lower()
            prop_complex = prop.get('complex_name', '').lower()
            if residential_complex not in prop_complex:
                continue
        
        # Street filter
        if filters.get('street'):
            street = filters['street'].lower()
            prop_location = prop.get('location', '').lower()
            prop_address = prop.get('full_address', '').lower()
            if street not in prop_location and street not in prop_address:
                continue
        
        # Mortgage filter
        if filters.get('mortgage') and not prop.get('mortgage_available', False):
            continue
        
        filtered.append(prop)
    
    return filtered

def build_property_filters(request_args):
    """
    Unified property filtering function for /properties and /map routes.
    
    Args:
        request_args: Flask request.args object
        
    Returns:
        tuple: (where_conditions, params, filters_dict)
            - where_conditions: list of SQL WHERE clause strings
            - params: dict of parameterized values for SQL query
            - filters_dict: dict of parsed filter values for template/debugging
    """
    from datetime import datetime
    
    # Parse all filter parameters (унифицируем названия)
    filters = {}
    
    # Price filters (поддержка всех форматов: price_min, priceFrom, price_from)
    filters['price_min'] = request_args.get('price_min', request_args.get('priceFrom', request_args.get('price_from', '')))
    filters['price_max'] = request_args.get('price_max', request_args.get('priceTo', request_args.get('price_to', '')))
    
    # Area filters (поддержка всех форматов)
    filters['area_min'] = request_args.get('area_min', request_args.get('areaFrom', request_args.get('area_from', '')))
    filters['area_max'] = request_args.get('area_max', request_args.get('areaTo', request_args.get('area_to', '')))
    
    # Floor filters (поддержка всех форматов)
    filters['floor_min'] = request_args.get('floor_min', request_args.get('floorFrom', request_args.get('floor_from', '')))
    filters['floor_max'] = request_args.get('floor_max', request_args.get('floorTo', request_args.get('floor_to', '')))
    
    # Rooms filter (может прийти как "1,2,3" или как список)
    rooms_param = request_args.get('rooms', '')
    if rooms_param:
        filters['rooms'] = rooms_param.split(',') if ',' in rooms_param else [rooms_param]
    else:
        filters['rooms'] = request_args.getlist('rooms') or []
    
    # Multi-select filters
    filters['districts'] = request_args.getlist('districts') or []
    filters['developers'] = request_args.getlist('developers') or []
    filters['completion'] = request_args.getlist('completion') or []
    filters['building_types'] = request_args.getlist('building_types') or []
    filters['delivery_years'] = request_args.getlist('delivery_years') or []
    filters['features'] = request_args.getlist('features') or []
    filters['object_classes'] = request_args.getlist('object_classes') or request_args.getlist('object_class') or []
    filters['renovation'] = request_args.getlist('renovation') or []
    filters['building_released'] = request_args.getlist('building_released') or []
    filters['floor_options'] = request_args.getlist('floor_options') or []  # not_first, not_last
    
    # Boolean filters
    filters['cashback_only'] = request_args.get('cashback_only', '').lower() in ['true', '1', 'yes']
    
    # Single value filters
    filters['developer'] = request_args.get('developer', '')
    filters['district'] = request_args.get('district', '')
    filters['residential_complex'] = request_args.get('residential_complex', '')
    filters['building'] = request_args.get('building', '')
    
    # Building floors filters
    filters['building_floors_min'] = request_args.get('building_floors_min', request_args.get('maxFloorFrom', ''))
    filters['building_floors_max'] = request_args.get('building_floors_max', request_args.get('maxFloorTo', ''))
    
    # Build year filters
    filters['build_year_min'] = request_args.get('build_year_min', request_args.get('buildYearFrom', ''))
    filters['build_year_max'] = request_args.get('build_year_max', request_args.get('buildYearTo', ''))
    
    # Regional filters
    filters['regions'] = request_args.getlist('regions') or []
    filters['cities'] = request_args.getlist('cities') or []
    filters['region'] = request_args.get('region', '')
    filters['city'] = request_args.get('city', '')
    
    # Search query
    filters['search'] = request_args.get('search', '')
    
    # Build SQL WHERE conditions and parameters
    where_conditions = []
    params = {}
    
    # Price filters (пользователь вводит в миллионах)
    if filters.get('price_min'):
        try:
            params['price_min'] = float(filters['price_min']) * 1000000
            where_conditions.append('price >= :price_min')
        except:
            pass
    
    if filters.get('price_max'):
        try:
            params['price_max'] = float(filters['price_max']) * 1000000
            where_conditions.append('price <= :price_max')
        except:
            pass
    
    # Area filters
    if filters.get('area_min'):
        try:
            params['area_min'] = float(filters['area_min'])
            where_conditions.append('object_area >= :area_min')
        except:
            pass
    
    if filters.get('area_max'):
        try:
            params['area_max'] = float(filters['area_max'])
            where_conditions.append('object_area <= :area_max')
        except:
            pass
    
    # Floor filters
    if filters.get('floor_min'):
        try:
            params['floor_min'] = int(filters['floor_min'])
            where_conditions.append('object_min_floor >= :floor_min')
        except:
            pass
    
    if filters.get('floor_max'):
        try:
            params['floor_max'] = int(filters['floor_max'])
            where_conditions.append('object_min_floor <= :floor_max')
        except:
            pass
    
    # Floor options (не первый/не последний этаж)
    if filters.get('floor_options'):
        floor_option_conditions = []
        for option in filters['floor_options']:
            if option == 'not_first':
                floor_option_conditions.append('object_min_floor > 1')
            elif option == 'not_last':
                # Упрощенное условие: квартира не на последнем этаже, если этаж ≤ 15
                # Работает для большинства домов (нет данных об этажности здания)
                floor_option_conditions.append('object_min_floor <= 15')
        
        if floor_option_conditions:
            where_conditions.append(f"({' AND '.join(floor_option_conditions)})")
    
    # Cashback only filter (только объекты с кешбеком)
    if filters.get('cashback_only'):
        where_conditions.append('(min_rate > 0 AND min_rate IS NOT NULL)')
    
    # Rooms filter - support various formats
    if filters.get('rooms'):
        room_conditions = []
        for room_filter in filters['rooms']:
            if isinstance(room_filter, str):
                normalized = room_filter.lower().strip()
                if normalized in ['студия', '0', 'studio']:
                    room_conditions.append('object_rooms = 0')
                elif normalized.endswith('-комн'):
                    try:
                        room_num = int(normalized.split('-')[0])
                        room_conditions.append(f'object_rooms = {room_num}')
                    except:
                        pass
                elif normalized in ['4+', '4+-комн']:
                    room_conditions.append('object_rooms >= 4')
                elif normalized.isdigit():
                    room_conditions.append(f'object_rooms = {int(normalized)}')
        
        if room_conditions:
            where_conditions.append(f"({' OR '.join(room_conditions)})")
    
    # Building types filter (этажность дома)
    if filters.get('building_types'):
        building_conditions = []
        for building_type in filters['building_types']:
            if building_type == 'малоэтажный':
                building_conditions.append('object_max_floor <= 5')
            elif building_type == 'среднеэтажный':
                building_conditions.append('(object_max_floor >= 6 AND object_max_floor <= 12)')
            elif building_type == 'многоэтажный':
                building_conditions.append('object_max_floor >= 13')
        
        if building_conditions:
            where_conditions.append(f"({' OR '.join(building_conditions)})")
    
    # Districts filter (array)
    if filters.get('districts'):
        district_conditions = []
        for idx, district in enumerate(filters['districts']):
            param_name = f'district_{idx}'
            params[param_name] = f'%{district.lower()}%'
            district_conditions.append(f'LOWER(address_locality_name) LIKE :{param_name}')
        
        if district_conditions:
            where_conditions.append(f"({' OR '.join(district_conditions)})")
    
    # Single district filter
    if filters.get('district'):
        params['district'] = f'%{filters["district"].lower()}%'
        where_conditions.append('LOWER(address_locality_name) LIKE :district')
    
    # Developers filter (array)
    if filters.get('developers'):
        developer_conditions = []
        for idx, developer in enumerate(filters['developers']):
            param_name = f'developer_{idx}'
            params[param_name] = f'%{developer.lower()}%'
            developer_conditions.append(f'LOWER(developer_name) LIKE :{param_name}')
        
        if developer_conditions:
            where_conditions.append(f"({' OR '.join(developer_conditions)})")
    
    # Single developer filter
    if filters.get('developer'):
        params['developer'] = f'%{filters["developer"].lower()}%'
        where_conditions.append('LOWER(developer_name) LIKE :developer')
    
    # Residential complex filter
    if filters.get('residential_complex'):
        params['complex'] = f'%{filters["residential_complex"].lower()}%'
        where_conditions.append('LOWER(complex_name) LIKE :complex')
    
    # Building filter
    if filters.get('building'):
        params['building'] = f'%{filters["building"].lower()}%'
        where_conditions.append('LOWER(complex_building_name) LIKE :building')
    
    # Completion year filters
    if filters.get('completion') or filters.get('delivery_years'):
        years = filters.get('completion') or filters.get('delivery_years')
        year_conditions = []
        for year in years:
            if year != 'Сдан':
                year_conditions.append(f'complex_building_end_build_year = {year}')
        
        if year_conditions:
            where_conditions.append(f"({' OR '.join(year_conditions)})")
    
    # Object classes filter
    if filters.get('object_classes'):
        class_conditions = []
        for idx, obj_class in enumerate(filters['object_classes']):
            param_name = f'class_{idx}'
            params[param_name] = f'%{obj_class.lower()}%'
            class_conditions.append(f'LOWER(complex_object_class_display_name) LIKE :{param_name}')
        
        if class_conditions:
            where_conditions.append(f"({' OR '.join(class_conditions)})")
    
    # Renovation filter
    if filters.get('renovation'):
        renovation_conditions = []
        for idx, renovation in enumerate(filters['renovation']):
            param_name = f'renovation_{idx}'
            params[param_name] = f'%{renovation.lower()}%'
            renovation_conditions.append(f'LOWER(renovation_display_name) LIKE :{param_name}')
        
        if renovation_conditions:
            where_conditions.append(f"({' OR '.join(renovation_conditions)})")
    
    # Features filter
    if filters.get('features'):
        feature_conditions = []
        for idx, feature in enumerate(filters['features']):
            param_name = f'feature_{idx}'
            params[param_name] = f'%{feature.lower()}%'
            feature_conditions.append(f'LOWER(description) LIKE :{param_name}')
        
        if feature_conditions:
            where_conditions.append(f"({' OR '.join(feature_conditions)})")
    
    # Building released filter (сдан/не сдан)
    if filters.get('building_released'):
        released_conditions = []
        now = datetime.now()
        current_year = now.year
        current_quarter = (now.month - 1) // 3 + 1
        
        for status in filters['building_released']:
            if status.lower() in ['сдан', 'delivered', 'ready']:
                released_conditions.append(f'''(
                    complex_end_build_year < {current_year} OR 
                    (complex_end_build_year = {current_year} AND 
                     (complex_end_build_quarter IS NULL OR complex_end_build_quarter < {current_quarter})) OR
                    complex_building_end_build_year < {current_year} OR
                    (complex_building_end_build_year = {current_year} AND 
                     (complex_building_end_build_quarter IS NULL OR complex_building_end_build_quarter < {current_quarter}))
                )''')
            elif status.lower() in ['строится', 'under_construction', 'building']:
                released_conditions.append(f'''(
                    complex_end_build_year > {current_year} OR
                    (complex_end_build_year = {current_year} AND 
                     (complex_end_build_quarter IS NULL OR complex_end_build_quarter >= {current_quarter})) OR
                    complex_building_end_build_year > {current_year} OR
                    (complex_building_end_build_year = {current_year} AND 
                     (complex_building_end_build_quarter IS NULL OR complex_building_end_build_quarter >= {current_quarter}))
                )''')
        
        if released_conditions:
            where_conditions.append(f"({' OR '.join(released_conditions)})")
    
    # Building floors filters
    if filters.get('building_floors_min'):
        try:
            params['building_floors_min'] = int(filters['building_floors_min'])
            where_conditions.append('object_max_floor >= :building_floors_min')
        except:
            pass
    
    if filters.get('building_floors_max'):
        try:
            params['building_floors_max'] = int(filters['building_floors_max'])
            where_conditions.append('object_max_floor <= :building_floors_max')
        except:
            pass
    
    # Build year filters
    if filters.get('build_year_min'):
        try:
            params['build_year_min'] = int(filters['build_year_min'])
            where_conditions.append('(complex_end_build_year >= :build_year_min OR complex_building_end_build_year >= :build_year_min)')
        except:
            pass
    
    if filters.get('build_year_max'):
        try:
            params['build_year_max'] = int(filters['build_year_max'])
            where_conditions.append('(complex_end_build_year <= :build_year_max OR complex_building_end_build_year <= :build_year_max)')
        except:
            pass
    
    # Regional filters
    regional_conditions = []
    
    if filters.get('regions'):
        for idx, region in enumerate(filters['regions']):
            param_name = f'region_{idx}'
            params[param_name] = f'%{region.lower()}%'
            regional_conditions.append(f'LOWER(address_display_name) LIKE :{param_name}')
    
    if filters.get('region'):
        params['region'] = f'%{filters["region"].lower()}%'
        regional_conditions.append('LOWER(address_display_name) LIKE :region')
    
    if filters.get('cities'):
        for idx, city in enumerate(filters['cities']):
            param_name = f'city_{idx}'
            params[param_name] = f'%{city.lower()}%'
            regional_conditions.append(f'LOWER(address_display_name) LIKE :{param_name}')
    
    if filters.get('city'):
        params['city'] = f'%{filters["city"].lower()}%'
        regional_conditions.append('LOWER(address_display_name) LIKE :city')
    
    if regional_conditions:
        where_conditions.append(f"({' OR '.join(regional_conditions)})")
    
    # Search filter (multiple fields)
    if filters.get('search'):
        params['search'] = f'%{filters["search"].lower()}%'
        where_conditions.append('''(
            LOWER(address_display_name) LIKE :search OR
            LOWER(developer_name) LIKE :search OR
            LOWER(complex_name) LIKE :search OR
            LOWER(address_locality_name) LIKE :search OR
            LOWER(complex_building_name) LIKE :search
        )''')
    
    return where_conditions, params, filters

def get_developers_list():
    """Get list of unique developers"""
    properties = load_properties()
    developers = set()
    for prop in properties:
        if 'developer' in prop and prop['developer']:
            developers.add(prop['developer'])
    return sorted(list(developers))

def get_districts_list():
    """Get list of unique districts"""
    properties = load_properties()
    districts = set()
    for prop in properties:
        districts.add(prop['district'])
    return sorted(list(districts))

def sort_properties(properties, sort_type):
    """Sort properties by specified criteria with None safety"""
    if sort_type == 'price_asc':
        return sorted(properties, key=lambda x: x.get('price') or 0)
    elif sort_type == 'price_desc':
        return sorted(properties, key=lambda x: x.get('price') or 0, reverse=True)
    elif sort_type == 'cashback_desc':
        return sorted(properties, key=lambda x: calculate_cashback(x.get('price') or 0), reverse=True)
    elif sort_type == 'area_asc':
        return sorted(properties, key=lambda x: x.get('area') or 0)
    elif sort_type == 'area_desc':
        return sorted(properties, key=lambda x: x.get('area') or 0, reverse=True)
    else:
        return properties

def get_similar_properties(property_id, district, limit=3):
    """Get similar properties in the same district"""
    properties = load_properties()
    similar = []
    
    for prop in properties:
        if str(prop['id']) != str(property_id) and prop['district'] == district:
            similar.append(prop)
            if len(similar) >= limit:
                break
    
    return similar

# Routes
@app.route('/')
def index():
    """Home page with featured content"""
    # Админы и менеджеры МОГУТ видеть основной сайт (убрали редирект)
    properties = load_properties()
    
    # ✅ MIGRATED: Load residential complexes using normalized tables
    exclusive_complexes = []
    try:
        # Get active residential complexes with property stats
        complexes_orm = ResidentialComplexRepository.get_all_active(limit=20)
        
        # Get aggregated property stats for all complexes (single query, no N+1)
        property_stats = PropertyRepository.get_all_property_stats()
        
        # Build list of complexes with stats
        complexes_with_stats = []
        for complex_obj in complexes_orm:
            stats = property_stats.get(complex_obj.id, {})
            
            # Skip complexes without properties
            if not stats or stats.get('total_count', 0) == 0:
                continue
            
            # Calculate sorting keys
            end_year = complex_obj.end_build_year or 9999
            end_quarter = complex_obj.end_build_quarter or 4
            is_future = (end_year == 2025 and end_quarter == 4)
            price_from = stats.get('min_price', 0)
            
            complexes_with_stats.append({
                'complex': complex_obj,
                'stats': stats,
                'sort_future': 1 if is_future else 0,
                'sort_price': price_from
            })
        
        # Sort: ready complexes first, then by price
        complexes_with_stats.sort(key=lambda x: (x['sort_future'], x['sort_price']))
        
        # Take top 6 complexes
        for item in complexes_with_stats[:6]:
            complex_obj = item['complex']
            stats = item['stats']
            # ✅ MIGRATED: Format complex data from ORM objects
            # Photo loading strategy: complex.main_image → complex.gallery_images → properties.sample_photos → no-photo.jpg
            photos_list = []
            main_photo = None
            
            # Try complex main_image first
            if complex_obj.main_image:
                main_photo = complex_obj.main_image
                photos_list = [main_photo]
            
            # Try complex gallery_images
            if complex_obj.gallery_images:
                try:
                    photos_raw = json.loads(complex_obj.gallery_images) if isinstance(complex_obj.gallery_images, str) else complex_obj.gallery_images
                    if photos_raw and isinstance(photos_raw, list) and len(photos_raw) > 0:
                        photos_list = photos_raw[:3]
                        main_photo = photos_list[0]
                except:
                    pass
            
            # Fallback: use photos from properties
            if not photos_list or len(photos_list) == 0:
                sample_photos = stats.get('sample_photos', [])
                if sample_photos and len(sample_photos) > 0:
                    photos_list = sample_photos[:3]
                    main_photo = photos_list[0]
            
            # Final fallback: no-photo.jpg
            if not main_photo:
                main_photo = '/static/images/no-photo.jpg'
            if not photos_list or len(photos_list) == 0:
                photos_list = [main_photo]
            
            # Determine completion status
            current_year = 2025
            current_quarter = 4
            
            is_completed = False
            completion_date = 'Не указан'
            
            if complex_obj.end_build_year and complex_obj.end_build_quarter:
                build_year = int(complex_obj.end_build_year)
                build_quarter = int(complex_obj.end_build_quarter)
                
                if build_year < current_year:
                    is_completed = True
                elif build_year == current_year and build_quarter < current_quarter:
                    is_completed = True
                
                quarter_names = {1: 'I', 2: 'II', 3: 'III', 4: 'IV'}
                quarter = quarter_names.get(build_quarter, build_quarter)
                completion_date = f"{quarter} кв. {build_year} г."
            
            # Determine room types based on area range
            room_types = []
            area_from = stats.get('min_area', 0)
            area_to = stats.get('max_area', 0)
            
            if area_from and area_to:
                if area_from < 35:
                    room_types.append("Студии")
                if area_from <= 45 and area_to >= 35:
                    room_types.append("1К")
                if area_to >= 55:
                    room_types.append("2-3К")
            else:
                room_types = ["Студии", "1-3К"]
            
            room_type_display = " - ".join(room_types) if room_types else "Различные"
            
            # Safe image handling
            safe_images = photos_list if photos_list and len(photos_list) > 0 else ['/static/images/no-photo.jpg']
            safe_main_image = safe_images[0] if safe_images else '/static/images/no-photo.jpg'
            
            # Clean address - remove "Сочи" prefix
            clean_address = complex_obj.sales_address or stats.get('sample_address', '')
            if clean_address.startswith('Сочи, '):
                clean_address = clean_address[6:]
            elif clean_address.startswith('г. Сочи, '):
                clean_address = clean_address[9:]
            
            # Cashback from admin panel
            cashback_percent = float(complex_obj.cashback_rate) if complex_obj.cashback_rate else 5.0
            
            # Calculate cashback amount based on max price
            max_price = stats.get('max_price', 0)
            cashback_amount = int(max_price * (cashback_percent / 100)) if max_price else 0
            
            complex_dict = {
                'id': complex_obj.id,
                'name': complex_obj.name or 'Без названия',
                'price_from': int(stats.get('min_price', 0)),
                'price_to': int(stats.get('max_price', 0)),
                'area_from': int(stats.get('min_area', 0)),
                'area_to': int(stats.get('max_area', 0)),
                'room_type': room_type_display,
                'address': clean_address,
                'developer': complex_obj.developer.name if complex_obj.developer else 'Не указан',
                'photos': safe_images,
                'images': safe_images,
                'image': safe_main_image,
                'main_photo': safe_main_image,
                'main_image': safe_main_image,
                'apartments_count': stats.get('total_count', 0),
                'completion_date': completion_date,
                'is_completed': is_completed,
                'cashback_amount': int(cashback_amount),
                'cashback_percent': float(cashback_percent),
                'cashback_max': int(cashback_amount),
                'buildings_count': stats.get('buildings_count', 1)
            }
            exclusive_complexes.append(complex_dict)
            
        print(f"✅ Загружено {len(exclusive_complexes)} эксклюзивных ЖК для главной страницы")
        
    except Exception as e:
        print(f"❌ Error loading exclusive complexes: {e}")
        import traceback
        traceback.print_exc()
        exclusive_complexes = []
    
    complexes = load_residential_complexes()  # Для совместимости со старым кодом
    developers_file = os.path.join('data', 'developers.json')
    with open(developers_file, 'r', encoding='utf-8') as f:
        developers = json.load(f)
    
    # Загружаем статьи блога из базы данных для главной страницы
    blog_articles = []
    try:
        from models import BlogPost
        from sqlalchemy import desc
        
        # Получаем опубликованные статьи только из BlogPost с категорией
        blog_posts = BlogPost.query.filter_by(status='published').filter(BlogPost.category_id.isnot(None)).order_by(desc(BlogPost.published_at)).limit(4).all()
        
        # Преобразуем в единый формат для шаблона
        for post in blog_posts:
            blog_articles.append({
                'title': post.title,
                'slug': post.slug,
                'excerpt': post.excerpt or 'Интересная статья о недвижимости',
                'featured_image': post.featured_image or 'https://images.unsplash.com/photo-1560518883-ce09059eeffa?ixlib=rb-4.0.3&auto=format&fit=crop&w=1000&q=80',
                'published_at': post.published_at or post.created_at,
                'reading_time': getattr(post, 'reading_time', 5),
                'category': post.category or 'Недвижимость',
                'url': f'/blog/{post.slug}'
            })
        
    except Exception as e:
        print(f"Error loading blog articles for index: {e}")
        # Fallback статьи если база недоступна
        blog_articles = [
            {
                'title': 'Ипотека мурабаха: что это и как оформить',
                'slug': 'ipoteka-murabaha',
                'excerpt': 'Ипотека мурабаха — это исламская ипотека без процентов, где банк покупает недвижимость и продает ее клиенту с наценкой.',
                'featured_image': 'https://images.unsplash.com/photo-1560518883-ce09059eeffa?ixlib=rb-4.0.3&auto=format&fit=crop&w=1000&q=80',
                'published_at': datetime.now(),
                'reading_time': 5,
                'category': 'Ипотека',
                'url': '/blog/ipoteka-murabaha'
            }
        ]
    
    # ✅ MIGRATED: Получаем 6 случайных объектов из normalized Property table
    try:
        import random
        from models import Property
        
        # Get random featured properties using PropertyRepository
        all_properties = PropertyRepository.get_all_active(
            limit=100,
            filters={
                'min_price': 1  # Only properties with price > 0
            }
        )
        
        # Filter properties with photos and coordinates
        valid_properties = [
            p for p in all_properties
            if p.gallery_images and p.latitude and p.longitude and p.area and p.residential_complex
        ]
        
        # Shuffle and limit to 8 properties
        random.shuffle(valid_properties)
        result = valid_properties[:8]
        
        featured_properties = []
        for prop_orm in result:
            try:
                # Parse gallery images
                main_image = 'https://via.placeholder.com/400x300?text=Фото+скоро'
                gallery = [main_image]
                
                if prop_orm.gallery_images:
                    try:
                        photos_raw = json.loads(prop_orm.gallery_images)
                        if photos_raw and isinstance(photos_raw, list) and len(photos_raw) > 0:
                            main_image = photos_raw[0]
                            gallery = photos_raw[:5]
                    except:
                        pass
                
                # Property details
                rooms = int(prop_orm.rooms or 0)
                area = float(prop_orm.area or 0)
                price = int(prop_orm.price or 0)
                
                # Floor info
                floor_min = int(prop_orm.floor or 1)
                floor_max = int(prop_orm.total_floors or floor_min)
                floor_text = f"{floor_min}/{floor_max} эт."
                
                # Room type
                room_type = "Студия" if rooms == 0 else f"{rooms}-комн"
                
                # Completion date and status
                current_year = 2025
                build_quarter = prop_orm.residential_complex.end_build_quarter if prop_orm.residential_complex else None
                build_year = prop_orm.residential_complex.end_build_year if prop_orm.residential_complex else None
                
                if build_year and build_quarter:
                    quarter_text = f"{build_quarter}кв. {build_year}г."
                    status_text = "Сдан" if build_year < current_year or (build_year == current_year and build_quarter <= 1) else "Строится"
                else:
                    quarter_text = "Уточняется"
                    status_text = "Строится"
                
                # Build property dict
                prop = {
                    'id': prop_orm.inner_id or str(prop_orm.id),
                    'price': price,
                    'area': area,
                    'rooms': rooms,
                    'title': f"{room_type}, {area} м², {floor_text}",
                    'complex': prop_orm.residential_complex.name if prop_orm.residential_complex else 'ЖК не указан',
                    'developer': prop_orm.developer.name if prop_orm.developer else '',
                    'address': prop_orm.address or '',
                    'image': main_image,
                    'gallery': gallery,
                    'cashback': int(price * 0.02),
                    'cashback_amount': int(price * 0.02),
                    'completion_date': quarter_text,
                    'status': status_text,
                    'floor_info': floor_text
                }
                featured_properties.append(prop)
                if len(featured_properties) >= 6:
                    break
                
            except Exception as e:
                print(f"Error processing property {prop_orm.id}: {e}")
                continue
        
        if featured_properties:
            print(f"✅ Загружено {len(featured_properties)} реальных объектов из базы")
        else:
            raise Exception("No properties loaded")
            
    except Exception as e:
        print(f"❌ Ошибка загрузки реальных объектов: {e}")
        # Fallback к старым данным
        featured_properties = sorted(properties, key=lambda x: x.get('cashback_amount', 0), reverse=True)[:6]
        for prop in featured_properties:
            prop['cashback'] = calculate_cashback(
                prop['price'],
                complex_id=prop.get('complex_id'),
                complex_name=prop.get('residential_complex')
            )
    
    # Get districts with statistics
    districts_data = {}
    for complex in complexes:
        district = complex['district']
        if district not in districts_data:
            districts_data[district] = {
                'name': district,
                'complexes_count': 0,
                'price_from': float('inf'),
                'apartments_count': 0
            }
        districts_data[district]['complexes_count'] += 1
        complex_price = complex.get('price_from') or 0
        if complex_price > 0:  # Only update if we have a valid price
            districts_data[district]['price_from'] = min(districts_data[district]['price_from'], complex_price)
        districts_data[district]['apartments_count'] += complex.get('apartments_count', 0) or 0
    
    districts = sorted(districts_data.values(), key=lambda x: x['complexes_count'], reverse=True)[:8]
    
    # Get featured developers (top 3 with most complexes)
    featured_developers = []
    for developer in developers[:3]:
        developer_complexes = [c for c in complexes if c.get('developer_id') == developer['id']]
        developer_properties = [p for p in properties if any(c['id'] == p.get('complex_id') for c in developer_complexes)]
        
        developer_info = {
            'id': developer['id'],
            'name': developer['name'],
            'complexes_count': len(developer_complexes),
            'apartments_count': len(developer_properties),
            'price_from': min([p['price'] for p in developer_properties]) if developer_properties else 0,
            'max_cashback': max([c.get('cashback_percent', 5) for c in developer_complexes]) if developer_complexes else 5
        }
        featured_developers.append(developer_info)
    
    # Загружаем категории блога для главной страницы
    blog_categories = []
    try:
        from models import Category
        blog_categories = Category.query.filter_by(is_active=True).order_by(Category.sort_order, Category.name).all()
    except Exception as e:
        print(f"Error loading blog categories for index: {e}")
    
    return render_template('index.html',
                         featured_properties=featured_properties,
                         districts=districts,
                         featured_developers=featured_developers,
                         residential_complexes=complexes[:3],
                         exclusive_complexes=exclusive_complexes,
                         blog_articles=blog_articles,
                         blog_categories=blog_categories)

@app.route('/properties')
def properties():
    """Properties listing page - NORMALIZED TABLES (Developer → ResidentialComplex → Property)"""
    try:
        import json
        from repositories.property_repository import PropertyRepository, ResidentialComplexRepository, DeveloperRepository
        from models import Developer, ResidentialComplex, Property
        
        # Parse filters using existing function
        _, _, filters = build_property_filters(request.args)
        
        # Pagination
        page = request.args.get('page', default=1, type=int)
        page = max(1, page)
        per_page = 20
        offset = (page - 1) * per_page
        
        # Sorting
        sort_type = request.args.get('sort', 'price-asc').replace('_', '-')
        
        # Convert filters to Repository format
        repo_filters = {}
        if filters.get('price_min'):
            try:
                repo_filters['min_price'] = int(float(filters['price_min']) * 1000000)
            except:
                pass
        if filters.get('price_max'):
            try:
                repo_filters['max_price'] = int(float(filters['price_max']) * 1000000)
            except:
                pass
        if filters.get('area_min'):
            try:
                repo_filters['min_area'] = float(filters['area_min'])
            except:
                pass
        if filters.get('area_max'):
            try:
                repo_filters['max_area'] = float(filters['area_max'])
            except:
                pass
        if filters.get('rooms'):
            try:
                repo_filters['rooms'] = [int(r) for r in filters['rooms'] if r.strip()]
            except:
                pass
        
        # Get properties with Repository
        properties_list = PropertyRepository.get_all_active(
            limit=per_page,
            offset=offset,
            filters=repo_filters
        )
        
        total_properties = PropertyRepository.count_active(filters=repo_filters)
        
        # Convert to template format
        properties_data = []
        for prop in properties_list:
            try:
                # Get complex and developer data through relationships
                complex_obj = prop.residential_complex
                developer_obj = prop.developer
                
                # Parse photos safely
                photos_list = []
                main_image = 'https://via.placeholder.com/400x300'
                
                if prop.main_image:
                    main_image = prop.main_image
                
                if prop.gallery_images:
                    try:
                        # Check if already a list (SQLAlchemy may auto-parse JSON)
                        if isinstance(prop.gallery_images, list):
                            photos_list = prop.gallery_images
                        elif isinstance(prop.gallery_images, str):
                            photos_list = json.loads(prop.gallery_images)
                        
                        # Use first photo as main_image if not set
                        if photos_list and not prop.main_image:
                            main_image = photos_list[0]
                    except Exception as e:
                        print(f"Error parsing photos for property {prop.id}: {e}")
                
                # Calculate cashback for this property
                cashback_rate = complex_obj.cashback_rate if complex_obj and complex_obj.cashback_rate else 3.5
                cashback_amount = int(prop.price * (cashback_rate / 100)) if prop.price else 0
                
                property_dict = {
                    'id': prop.inner_id or prop.id,
                    'price': prop.price or 0,
                    'price_formatted': prop.formatted_price,
                    'area': prop.area or 0,
                    'rooms': prop.rooms or 0,
                    'room_description': prop.room_description,
                    'floor': prop.floor if prop.floor is not None else 1,
                    'total_floors': prop.total_floors if prop.total_floors is not None else 1,
                    'address': prop.address or (complex_obj.address if complex_obj else ''),
                    'renovation': prop.renovation_type or 'no_renovation',
                    'renovation_display_name': PropertyRepository.get_renovation_display_name(prop.renovation_type),
                    'price_per_sqm': prop.price_per_sqm or (int(prop.price / prop.area) if prop.price and prop.area else 0),
                    'gallery': photos_list,  # Template expects 'gallery'
                    'image': main_image,  # Template expects 'image'
                    'latitude': prop.latitude or (complex_obj.latitude if complex_obj else None),
                    'longitude': prop.longitude or (complex_obj.longitude if complex_obj else None),
                    'complex_name': complex_obj.name if complex_obj else '',
                    'residential_complex': complex_obj.name if complex_obj else '',
                    'developer': developer_obj.name if developer_obj else '',
                    'developer_name': developer_obj.name if developer_obj else '',
                    'cashback_rate': cashback_rate,
                    'cashback': cashback_amount,
                    'cashback_available': True,
                    'complex_object_class_display_name': complex_obj.object_class_display_name if complex_obj else 'Комфорт',
                    'deal_type': prop.deal_type or 'Первичка',
                    'description': prop.description or '',
                    'type': 'apartment',
                    'district': complex_obj.district if complex_obj else 'Краснодар',
                    'mortgage_available': True,
                    'completion_date': f"{complex_obj.end_build_quarter} кв. {complex_obj.end_build_year}" if complex_obj and complex_obj.end_build_year else 'Уточняется'
                }
                properties_data.append(property_dict)
            except Exception as e:
                print(f"Error processing property {prop.id}: {e}")
                continue
        
        # Get all developers and complexes for filters
        developers = DeveloperRepository.get_all_active()
        residential_complexes = ResidentialComplexRepository.get_all_active(limit=100)
        
        # Pagination object
        total_pages = (total_properties + per_page - 1) // per_page
        pagination = {
            'page': page,
            'per_page': per_page,
            'total': total_properties,
            'pages': total_pages,
            'total_pages': total_pages,  # Template expects this name
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'prev_num': page - 1 if page > 1 else None,
            'next_num': page + 1 if page < total_pages else None
        }
        
        # Get manager info
        manager_data = None
        if current_user.is_authenticated:
            user = User.query.get(current_user.id)
            if user and user.assigned_manager:
                manager = user.assigned_manager
                manager_data = {
                    'name': f"{manager.first_name} {manager.last_name}",
                    'phone': manager.phone,
                    'email': manager.email,
                    'photo': manager.profile_image if manager.profile_image and 'randomuser.me' not in manager.profile_image else None
                }
        
        # Calculate max cashback from residential complexes using property stats
        max_cashback = 0
        if residential_complexes:
            all_stats = PropertyRepository.get_all_property_stats()
            for rc in residential_complexes:
                stats = all_stats.get(rc.id, {})
                if rc.cashback_rate and stats.get('max_price'):
                    cashback_amount = int(stats['max_price'] * (rc.cashback_rate / 100))
                    if cashback_amount > max_cashback:
                        max_cashback = cashback_amount
        
        # If no max found, use default 500,000
        if max_cashback == 0:
            max_cashback = 500000
        
        return render_template('properties.html',
                             properties=properties_data,
                             pagination=pagination,
                             filters=filters,
                             developers=developers,
                             residential_complexes=residential_complexes,
                             manager=manager_data,
                             total_pages=total_pages,
                             total_properties=total_properties,
                             max_cashback=max_cashback,
                             user_authenticated=current_user.is_authenticated,
                             manager_authenticated=current_user.is_authenticated and hasattr(current_user, 'role') and current_user.role == 'manager')
        
    except Exception as e:
        print(f"ERROR in /properties: {e}")
        import traceback
        traceback.print_exc()
        return render_template('error.html', 
                             error_message="Ошибка загрузки объектов",
                             error_details=str(e)), 500


@app.route('/object/<int:property_id>')
def property_detail(property_id):
    """Individual property page - NORMALIZED TABLES (Developer → ResidentialComplex → Property)"""
    try:
        import json
        from sqlalchemy import func
        from repositories.property_repository import PropertyRepository
        from models import Property, ResidentialComplex
        
        # Get property from normalized tables
        prop = PropertyRepository.get_by_inner_id(property_id)
        
        if not prop:
            print(f"Property {property_id} not found in normalized tables")
            return redirect(url_for('properties'))
        
        # Get related objects through relationships
        complex_obj = prop.residential_complex
        developer_obj = prop.developer
        
        # Parse photos from gallery_images JSON
        images = []
        main_image = 'https://via.placeholder.com/400x300/f3f4f6/9ca3af?text=Фото+недоступно'
        
        if prop.main_image:
            main_image = prop.main_image
        
        if prop.gallery_images:
            try:
                if isinstance(prop.gallery_images, list):
                    images = prop.gallery_images
                elif isinstance(prop.gallery_images, str):
                    images = json.loads(prop.gallery_images)
                
                if images and not prop.main_image:
                    main_image = images[0]
            except Exception as e:
                print(f"Error parsing photos for property {prop.id}: {e}")
        
        # Create completion date from residential_complex
        completion_date = 'Уточняется'
        if complex_obj:
            if complex_obj.end_build_year and complex_obj.end_build_quarter:
                completion_date = f"{complex_obj.end_build_year} г., {complex_obj.end_build_quarter} кв."
            elif complex_obj.end_build_year:
                completion_date = f"{complex_obj.end_build_year} г."
        
        # Calculate cashback
        cashback_rate = complex_obj.cashback_rate if complex_obj and complex_obj.cashback_rate else 3.5
        cashback_amount = int(prop.price * (cashback_rate / 100)) if prop.price else 0
        
        # Build property data for template using normalized tables
        property_data = {
            'id': prop.inner_id or prop.id,
            'complex_id': complex_obj.id if complex_obj else None,
            'price': prop.price or 0,
            'area': prop.area or 0,
            'rooms': prop.rooms or 0,
            'floor': prop.floor if prop.floor is not None else 1,
            'total_floors': prop.total_floors if prop.total_floors is not None else 1,
            'address': prop.address or (complex_obj.address if complex_obj else 'Адрес уточняется'),
            'short_address': prop.address or (complex_obj.address if complex_obj else 'Адрес уточняется'),
            'locality_name': complex_obj.district if complex_obj else 'Краснодар',
            'developer': developer_obj.name if developer_obj else 'Не указан',
            'complex_name': complex_obj.name if complex_obj else 'Не указан',
            'building_name': 'Корпус 1',  # TODO: Add building support
            'building_released': True,
            'renovation_type': PropertyRepository.get_renovation_display_name(prop.renovation_type),
            'completion_date': completion_date,
            'mortgage_rate': '3.5%',  # Default mortgage rate
            'square_price': prop.price_per_sqm or (int(prop.price / prop.area) if prop.price and prop.area else 0),
            'mortgage_payment': prop.mortgage_price or 0,
            'class_type': complex_obj.object_class_display_name if complex_obj else 'Комфорт',
            'cashback': cashback_amount,
            'images': images,
            'image': main_image,
            'address_position_lat': prop.latitude or (complex_obj.latitude if complex_obj else None),
            'address_position_lon': prop.longitude or (complex_obj.longitude if complex_obj else None),
            'description': prop.description or f"Продается квартира в {complex_obj.name if complex_obj else 'жилом комплексе'}. Отличная планировка, качественная отделка.",
            'gallery': images,
            # Additional fields from residential_complex
            'sales_address': complex_obj.sales_address if complex_obj and complex_obj.sales_address else (complex_obj.address if complex_obj else 'Уточняется'),
            'sales_phone': complex_obj.sales_phone if complex_obj and complex_obj.sales_phone else (complex_obj.complex_phone if complex_obj and complex_obj.complex_phone else 'Уточняется'),
            'with_renovation': complex_obj.with_renovation if complex_obj else False,
            'has_accreditation': complex_obj.has_accreditation if complex_obj else False,
            'has_green_mortgage': complex_obj.has_green_mortgage if complex_obj else False,
            'has_mortgage_subsidy': False,  # Not available in normalized tables
            'trade_in_available': prop.deal_type and 'trade' in str(prop.deal_type).lower(),
            'deal_type': prop.deal_type or 'Продажа',
            'is_apartment': prop.is_apartment if hasattr(prop, 'is_apartment') else True,
            'published_date': None,
            'placement_type': 'Новостройка'
        }
        
        # Ensure all required fields exist for template
        property_data['cashback_amount'] = property_data['cashback']
        property_data['cashback_percent'] = cashback_rate
        
        # Generate full title format for property detail page
        rooms = property_data.get('rooms', 0)
        area = property_data.get('area', 0)
        floor = property_data.get('floor', 1)
        total_floors = property_data.get('total_floors', 1)
        
        # Generate room type text
        if rooms > 0:
            room_text = f"{rooms}-комнатная квартира"
        else:
            room_text = "Студия"
            
        # Create full detailed title for property page
        title_parts = [room_text]
        if area:
            title_parts.append(f"{area} м²")
        title_parts.append(f"{floor}/{total_floors} эт.")
        
        property_data['title'] = ", ".join(title_parts)
        property_data['property_type'] = f"{rooms}-комн" if rooms > 0 else "Студия"
        property_data['apartment_number'] = str(property_data['id'])
        property_data['building'] = 'Корпус 1'
        
        # Build complex_info from complex_obj (already loaded via relationships)
        complex_info = None
        if complex_obj:
            complex_info = {
                'name': complex_obj.name,
                'district_id': complex_obj.district_id if hasattr(complex_obj, 'district_id') else None,
                'developer_id': complex_obj.developer_id,
                'class_display_name': complex_obj.object_class_display_name,
                'start_year': complex_obj.start_build_year if hasattr(complex_obj, 'start_build_year') else None,
                'end_year': complex_obj.end_build_year,
                'has_accreditation': complex_obj.has_accreditation,
                'has_green_mortgage': complex_obj.has_green_mortgage,
                'with_renovation': complex_obj.with_renovation,
                'cashback_rate': complex_obj.cashback_rate if complex_obj.cashback_rate is not None else 5.0
            }
        
        # Calculate statistics for residential complex using normalized tables
        similar_apartments = []
        if complex_obj:
            # 1. Total apartments in this complex
            from sqlalchemy import func
            total_apartments = db.session.query(func.count(Property.id)).filter(
                Property.complex_id == complex_obj.id,
                Property.is_active == True
            ).scalar() or 0
            property_data['complex_total_apartments'] = total_apartments
            
            # 2. Apartments with same room count
            same_type_apartments = db.session.query(func.count(Property.id)).filter(
                Property.complex_id == complex_obj.id,
                Property.rooms == prop.rooms,
                Property.is_active == True
            ).scalar() or 0
            property_data['same_type_apartments'] = same_type_apartments
            
            # 3. Number of buildings (default to 1 for now, can be enhanced later)
            property_data['complex_buildings_count'] = 1
            
            # 4. Get similar apartments from same complex (excluding current)
            similar_props = PropertyRepository.get_base_query().filter(
                Property.complex_id == complex_obj.id,
                Property.inner_id != str(property_id),
                Property.is_active == True
            ).order_by(Property.rooms.asc(), Property.price.asc()).limit(8).all()
            
            for similar_prop in similar_props:
                # Parse photos
                similar_images = []
                if similar_prop.gallery_images:
                    try:
                        if isinstance(similar_prop.gallery_images, list):
                            similar_images = similar_prop.gallery_images
                        elif isinstance(similar_prop.gallery_images, str):
                            similar_images = json.loads(similar_prop.gallery_images)
                    except:
                        pass
                
                room_type = f"{similar_prop.rooms}-комн" if similar_prop.rooms > 0 else "Студия"
                
                similar_apartments.append({
                    'id': similar_prop.inner_id or similar_prop.id,
                    'price': similar_prop.price or 0,
                    'area': similar_prop.area or 0,
                    'rooms': similar_prop.rooms or 0,
                    'room_type': room_type,
                    'floor': similar_prop.floor or 1,
                    'total_floors': similar_prop.total_floors or 1,
                    'building': 'Корпус 1',
                    'cashback': int(similar_prop.price * (cashback_rate / 100)) if similar_prop.price else 0,
                    'image': similar_images[0] if similar_images else (similar_prop.main_image or 'https://via.placeholder.com/300x200/f3f4f6/9ca3af?text=Фото+недоступно'),
                    'title': f"{room_type}, {similar_prop.area} м², {similar_prop.floor}/{similar_prop.total_floors} эт." if similar_prop.area else f"{room_type}"
                })
        else:
            property_data['complex_total_apartments'] = 0
            property_data['same_type_apartments'] = 0
            property_data['complex_buildings_count'] = 0
            
        print(f"Rendering property {property_id}: {property_data.get('title', 'Unknown')}")
        # Get manager info for current user (if logged in)
        manager_data = None
        if current_user.is_authenticated:
            user = User.query.get(current_user.id)
            if user and user.assigned_manager:
                manager = user.assigned_manager
                manager_data = {
                    'name': f"{manager.first_name} {manager.last_name}",
                    'phone': manager.phone,
                    'email': manager.email,
                    'photo': manager.profile_image if manager.profile_image and not 'randomuser.me' in manager.profile_image else None
                }
        
        return render_template('property_detail.html', property=property_data, complex_info=complex_info, similar_apartments=similar_apartments, manager=manager_data)
        
    except Exception as e:
        print(f"ERROR in property detail route: {e}")
        import traceback
        traceback.print_exc()
        return f"Error 500: {str(e)}", 500

def create_slug(name):
    """Create SEO-friendly slug from complex name with transliteration"""
    if not name:
        return "unknown"
    
    # Transliteration table for Russian to Latin
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '',
        'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        # Uppercase variants
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
        'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
        'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
        'Ф': 'F', 'Х': 'H', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sch', 'Ъ': '',
        'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya'
    }
    
    # Remove ЖК prefix and quotes
    name = re.sub(r'^ЖК\s*["\']?', '', name, flags=re.IGNORECASE)
    name = re.sub(r'["\']', '', name)  # Remove remaining quotes
    
    # Transliterate Cyrillic to Latin
    slug = ''
    for char in name:
        if char in translit_map:
            slug += translit_map[char]
        else:
            slug += char
    
    # Clean up: remove special characters except spaces and hyphens
    slug = re.sub(r'[^\w\s-]', '', slug)
    # Replace spaces/multiple hyphens with single hyphen
    slug = re.sub(r'[-\s]+', '-', slug)
    
    return slug.lower().strip('-')

@app.route('/residential_complex/<int:complex_id>')
@app.route('/residential-complex/<int:complex_id>')  # Support both formats
@app.route('/residential-complex/<complex_name>')  # Support name-based routing
@app.route('/zk/<slug>')  # New SEO-friendly format: /zk/zhk-kislorod
def residential_complex_detail(complex_id=None, complex_name=None, slug=None):
    """Individual residential complex page - MIGRATED to normalized tables"""
    try:
        from repositories.property_repository import PropertyRepository, ResidentialComplexRepository
        
        # === STEP 1: Get ResidentialComplex using Repository ===
        complex = None
        
        if slug:
            # Try exact slug match first
            complex = ResidentialComplexRepository.get_by_slug(slug)
            
            # If not found, try matching with create_slug() for transliteration support
            if not complex:
                all_complexes = ResidentialComplexRepository.get_all_active(limit=100)
                for c in all_complexes:
                    if create_slug(c.name) == slug:
                        complex = c
                        break
        elif complex_id:
            # Search by ID in normalized table
            complex = ResidentialComplexRepository.get_by_id(complex_id)
        elif complex_name:
            # Search by name - fallback to database query
            complex = db.session.query(ResidentialComplex).filter_by(name=complex_name).first()
        
        # If not found, redirect to properties page
        if not complex:
            print(f"Complex {complex_id or complex_name or slug} not found in residential_complexes table")
            return redirect(url_for('properties'))
        
        # Convert ORM object to dict for template compatibility
        complex_data = {
            'id': complex.id,
            'name': complex.name,
            'slug': complex.slug,
            'description': complex.description,
            'cashback_rate': complex.cashback_rate,
            'cashback_percent': complex.cashback_rate,  # Alias for template
            'developer_id': complex.developer_id,
            'developer_name': complex.developer.name if complex.developer else 'Не указан',
            'developer': complex.developer.name if complex.developer else 'Не указан',
            'developer_description': complex.developer.description if complex.developer else 'Застройщик с многолетним опытом работы на рынке недвижимости.',
            'developer_website': complex.developer.website if complex.developer else None,
            'object_class_display_name': complex.object_class_display_name or 'Комфорт',
            'sales_address': complex.sales_address,
            'district': complex.district.name if complex.district else 'Краснодар',
            'district_name': complex.district.name if complex.district else 'Краснодар',
            'start_build_year': complex.start_build_year,
            'start_build_quarter': complex.start_build_quarter,
            'end_build_year': complex.end_build_year,
            'end_build_quarter': complex.end_build_quarter,
            'latitude': complex.latitude,
            'longitude': complex.longitude,
            'main_image': complex.main_image,
            'gallery_images': complex.gallery_images
        }
        
        # === STEP 2: Get all properties for this complex using Repository ===
        properties = PropertyRepository.get_by_complex_id(complex.id, limit=500, sort_by='price', sort_order='asc')
        
        if not properties:
            print(f"No properties found for complex {complex.name}")
            complex_data['apartments_count'] = 0
            complex_data['price_from'] = 0
            complex_data['price_to'] = 0
            complex_data['buildings_count'] = 0
            complex_data['images'] = []
            complex_data['coordinates'] = [complex.latitude or 45.0355, complex.longitude or 38.9753]
        else:
            # Calculate aggregated stats from properties
            prices = [p.price for p in properties if p.price]
            areas = [p.area for p in properties if p.area]
            floors = [p.floor for p in properties if p.floor]
            total_floors_list = [p.total_floors for p in properties if p.total_floors]
            
            complex_data['apartments_count'] = len(properties)
            complex_data['total_apartments'] = len(properties)
            complex_data['price_from'] = int(min(prices)) if prices else 0
            complex_data['price_to'] = int(max(prices)) if prices else 0
            complex_data['min_price'] = complex_data['price_from']
            complex_data['max_price'] = complex_data['price_to']
            complex_data['real_price_from'] = complex_data['price_from']
            complex_data['real_price_to'] = complex_data['price_to']
            complex_data['real_area_from'] = float(min(areas)) if areas else 0
            complex_data['real_area_to'] = float(max(areas)) if areas else 0
            complex_data['real_floors_min'] = int(min(floors)) if floors else 1
            complex_data['real_floors_max'] = int(max(total_floors_list)) if total_floors_list else 25
            complex_data['total_floors_in_complex'] = complex_data['real_floors_max']
            
            # Count unique buildings by complex_building_name
            unique_buildings = set(p.complex_building_name for p in properties if p.complex_building_name)
            complex_data['buildings_count'] = max(len(unique_buildings), 1)
            
            # Set full address from sales_address or first property address
            complex_data['full_address'] = complex.sales_address or (properties[0].address if properties else '')
            
            # Set coordinates from complex or first property
            if complex.latitude and complex.longitude:
                complex_data['coordinates'] = [float(complex.latitude), float(complex.longitude)]
            elif properties[0].latitude and properties[0].longitude:
                complex_data['coordinates'] = [float(properties[0].latitude), float(properties[0].longitude)]
            else:
                complex_data['coordinates'] = [45.0355, 38.9753]  # Default Krasnodar
            
            # Get images from complex or first property
            if complex.gallery_images:
                try:
                    import json
                    images_list = json.loads(complex.gallery_images) if isinstance(complex.gallery_images, str) else complex.gallery_images
                    complex_data['images'] = images_list[:10] if images_list else []
                    complex_data['image'] = images_list[0] if images_list else complex.main_image
                except:
                    complex_data['images'] = []
                    complex_data['image'] = complex.main_image
            elif properties[0].gallery_images:
                try:
                    import json
                    images_list = json.loads(properties[0].gallery_images) if isinstance(properties[0].gallery_images, str) else []
                    complex_data['images'] = images_list[:10] if images_list else []
                    complex_data['image'] = images_list[0] if images_list else properties[0].main_image
                except:
                    complex_data['images'] = []
                    complex_data['image'] = properties[0].main_image
            else:
                complex_data['images'] = []
                complex_data['image'] = complex.main_image
                
            print(f"Loaded {len(properties)} properties for {complex.name}: price from {complex_data['price_from']}, {complex_data['buildings_count']} buildings")
        
        # === STEP 3: Convert properties to template-compatible format ===
        complex_properties = []
        cashback_rate_decimal = complex_data.get('cashback_percent', 5.0) / 100.0
        
        for prop in properties:
            # Convert ORM Property object to dict for template
            prop_dict = {
                'id': prop.inner_id or prop.id,  # Use inner_id for compatibility
                'inner_id': prop.inner_id or prop.id,
                'price': prop.price or 0,
                'cashback_amount': int(prop.price * cashback_rate_decimal) if prop.price else 0,
                'residential_complex_id': complex.id,
                'residential_complex': complex.name,
                'object_rooms': prop.rooms or 0,
                'object_area': prop.area or 0,
                'object_min_floor': prop.floor or 1,
                'floor': prop.floor or 1,
                'total_floors': prop.total_floors or complex_data['total_floors_in_complex'],
                'address': prop.address or '',
                'address_short_display_name': prop.address or '',
                'complex_building_name': prop.complex_building_name or 'Основной корпус',  # NEW: Building name for grouping
                'property_type': 'Квартира',
                'deal_type': prop.deal_type or 'sale',
                'renovation_type': prop.renovation_type
            }
            
            # Format title
            rooms = prop.rooms or 0
            if rooms == 0:
                room_type = "Студия"
            else:
                room_type = f"{rooms}-комнатная квартира"
            
            apartment_floor = prop.floor or 1
            total_floors = prop.total_floors or complex_data['total_floors_in_complex']
            prop_dict['title'] = f"{room_type}, {prop.area or 0} м², {apartment_floor}/{total_floors} эт."
            prop_dict['type'] = f"{rooms}-комн" if rooms > 0 else "Студия"
            prop_dict['apartment_floor'] = apartment_floor
            prop_dict['total_floors_in_complex'] = total_floors
            
            # Parse gallery images
            try:
                import json
                if prop.gallery_images:
                    if isinstance(prop.gallery_images, str):
                        photos_list = json.loads(prop.gallery_images)
                    else:
                        photos_list = prop.gallery_images
                else:
                    photos_list = []
                
                prop_dict['image'] = photos_list[0] if photos_list else (prop.main_image or 'https://via.placeholder.com/400x300/0088CC/FFFFFF?text=Квартира')
                prop_dict['photos_list'] = photos_list
            except Exception as e:
                print(f"Error parsing photos for property {prop.id}: {e}")
                prop_dict['image'] = prop.main_image or 'https://via.placeholder.com/400x300/0088CC/FFFFFF?text=Квартира'
                prop_dict['photos_list'] = []
            
            complex_properties.append(prop_dict)
        
        # === STEP 4: Calculate room statistics ===
        properties_by_rooms = {}
        room_stats = {}
        for prop in complex_properties:
            rooms = prop.get('object_rooms', 0)
            room_key = 'Студия' if rooms == 0 else f'{rooms}-комн'
            room_type = room_key
            
            if room_key not in properties_by_rooms:
                properties_by_rooms[room_key] = []
                room_stats[room_key] = {
                    'count': 0,
                    'prices': [],
                    'areas': [],
                    'name': room_type
                }
            
            properties_by_rooms[room_key].append(prop)
            room_stats[room_key]['count'] += 1
            if prop.get('price'):
                room_stats[room_key]['prices'].append(prop['price'])
            if prop.get('object_area'):
                room_stats[room_key]['areas'].append(prop['object_area'])
        
        # Calculate min/max for each room type
        for room_key in room_stats:
            stats = room_stats[room_key]
            if stats['prices']:
                stats['price_from'] = min(stats['prices'])
                stats['price_to'] = max(stats['prices'])
            else:
                stats['price_from'] = 0
                stats['price_to'] = 0
            
            if stats['areas']:
                stats['area_from'] = min(stats['areas'])
                stats['area_to'] = max(stats['areas'])
            else:
                stats['area_from'] = 0
                stats['area_to'] = 0
        
        complex_data['room_stats'] = room_stats
        
        # === STEP 5: Group properties by complex_building_name (e.g. "Корпус 1", "Литер 7") ===
        properties_by_building_unsorted = {}
        for prop in complex_properties:
            # Use complex_building_name from database (imported from backup)
            building_name = prop.get('complex_building_name') or 'Основной корпус'
            if not building_name or building_name.strip() == '':
                building_name = 'Основной корпус'
            
            if building_name not in properties_by_building_unsorted:
                properties_by_building_unsorted[building_name] = []
            properties_by_building_unsorted[building_name].append(prop)
        
        # Sort buildings (try to extract numbers for sorting)
        def sort_buildings(building_name):
            import re
            if not building_name or building_name == 'Основной корпус':
                return (999, building_name)  # Put "Основной корпус" last
            # Try to find number in building name for sorting (e.g. "Корпус 1" -> 1)
            match = re.search(r'(\d+)', building_name)
            if match:
                return (int(match.group(1)), building_name)
            return (999, building_name)
        
        # Create sorted dict of buildings
        properties_by_building = {}
        sorted_building_names = sorted(properties_by_building_unsorted.keys(), key=sort_buildings)
        for building_name in sorted_building_names:
            properties_by_building[building_name] = properties_by_building_unsorted[building_name]
        
        # === STEP 6: Calculate per-building statistics ===
        buildings_data = {}
        from datetime import datetime
        current_date = datetime.now()
        current_year = current_date.year
        current_quarter = (current_date.month - 1) // 3 + 1
        
        for building_name, building_props in properties_by_building.items():
            # Calculate stats for this building
            building_prices = [p['price'] for p in building_props if p.get('price')]
            building_areas = [p['object_area'] for p in building_props if p.get('object_area')]
            building_floors = [p['total_floors'] for p in building_props if p.get('total_floors')]
            
            # Determine building status from complex or default
            end_year = complex.end_build_year
            end_quarter = complex.end_build_quarter
            
            if end_year and end_quarter:
                if (end_year < current_year or (end_year == current_year and end_quarter <= current_quarter)):
                    building_status = 'Сдан'
                else:
                    building_status = 'Строится'
            else:
                building_status = 'Не указан'
            
            buildings_data[building_name] = {
                'building_name': building_name,
                'total_apartments': len(building_props),
                'building_status': building_status,
                'end_build_year': end_year,
                'end_build_quarter': end_quarter,
                'start_build_year': complex.start_build_year,
                'start_build_quarter': complex.start_build_quarter,
                'object_class': complex.object_class_display_name or 'Комфорт',
                'total_floors': max(building_floors) if building_floors else complex_data['total_floors_in_complex'],
                'price_from': min(building_prices) if building_prices else 0,
                'price_to': max(building_prices) if building_prices else 0,
                'area_from': min(building_areas) if building_areas else 0,
                'area_to': max(building_areas) if building_areas else 0
            }
        
        complex_data['buildings'] = buildings_data
        print(f"Grouped {len(properties)} properties into {len(buildings_data)} buildings by address")
        
        # Add current date for building status determination
        complex_data['current_year'] = current_year
        complex_data['current_quarter'] = current_quarter
        
        # === STEP 7: Find similar complexes using normalized tables ===
        similar_complexes = []
        try:
            # Get 3 other complexes sorted by price
            other_complexes = (
                db.session.query(ResidentialComplex)
                .filter(ResidentialComplex.id != complex.id, ResidentialComplex.is_active == True)
                .limit(10)
                .all()
            )
            
            # Get property stats for each complex
            from repositories.property_repository import PropertyRepository
            stats_by_complex = PropertyRepository.get_all_property_stats()
            
            for other_complex in other_complexes[:3]:
                stats = stats_by_complex.get(other_complex.id, {})
                
                # Get image: complex.main_image → complex.gallery_images → property photo
                image_url = other_complex.main_image
                if not image_url and other_complex.gallery_images:
                    try:
                        import json
                        images = json.loads(other_complex.gallery_images) if isinstance(other_complex.gallery_images, str) else other_complex.gallery_images
                        if images:
                            image_url = images[0]
                    except:
                        pass
                
                # Fallback: get photo from first property of this complex
                if not image_url:
                    try:
                        first_property = PropertyRepository.get_by_complex_id(other_complex.id, limit=1)
                        if first_property and first_property[0].gallery_images:
                            import json
                            prop_images = json.loads(first_property[0].gallery_images) if isinstance(first_property[0].gallery_images, str) else first_property[0].gallery_images
                            # Skip first photo (usually floor plan), take second photo
                            if prop_images and len(prop_images) > 1:
                                image_url = prop_images[1]
                            elif prop_images:
                                image_url = prop_images[0]
                    except Exception as e:
                        print(f"Error getting property image for complex {other_complex.id}: {e}")
                
                if not image_url:
                    image_url = f"https://via.placeholder.com/300x200/0088CC/FFFFFF?text={other_complex.name.replace(' ', '+')}"
                
                # Get address: complex.address → sales_address → first property address → district
                complex_address = other_complex.address
                if not complex_address:
                    complex_address = other_complex.sales_address
                if not complex_address:
                    try:
                        first_property = PropertyRepository.get_by_complex_id(other_complex.id, limit=1)
                        if first_property and first_property[0].address:
                            complex_address = first_property[0].address
                    except:
                        pass
                if not complex_address:
                    complex_address = other_complex.district.name if other_complex.district else 'Адрес не указан'
                
                similar_complex = {
                    'id': other_complex.id,
                    'name': other_complex.name,
                    'slug': other_complex.slug,
                    'price_from': stats.get('min_price', 0),
                    'price_to': stats.get('max_price', 0),
                    'apartments_count': stats.get('total_count', 0),
                    'developer': other_complex.developer.name if other_complex.developer else 'Не указан',
                    'location': complex_address,
                    'image': image_url,
                    'completion_date': f"{other_complex.end_build_quarter or '3'} кв. {other_complex.end_build_year or '2025'}",
                    'cashback_percent': other_complex.cashback_rate or 5.0,
                    'url': f'/zk/{other_complex.slug}'
                }
                similar_complexes.append(similar_complex)
                
        except Exception as e:
            print(f"Error finding similar complexes: {e}")
            import traceback
            traceback.print_exc()
            similar_complexes = []
        
        print(f"Found {len(similar_complexes)} similar complexes for {complex.name}")
        print(f"Rendering complex {complex.id}: {complex.name}")
        return render_template('residential_complex_detail.html', 
                             complex=complex_data,
                             properties=complex_properties,
                             properties_by_rooms=properties_by_rooms,
                             properties_by_building=properties_by_building,
                             similar_complexes=similar_complexes)
                             
    except Exception as e:
        print(f"ERROR in complex detail route: {e}")
        import traceback
        traceback.print_exc()
        return f"Error 500: {str(e)}", 500

@app.route('/developer/<int:developer_id>')
def developer_detail(developer_id):
    """Individual developer page"""
    try:
        # Load developer data from JSON file instead of DB to avoid conflicts
        with open('data/developers.json', 'r', encoding='utf-8') as f:
            developers_data = json.load(f)
        
        # Find developer by ID
        developer = None
        for dev in developers_data:
            if dev['id'] == developer_id:
                developer = dev
                break
        
        if not developer:
            return "Застройщик не найден", 404
        
        # Add missing template fields for new developers
        if 'total_apartments_sold' not in developer:
            developer['total_apartments_sold'] = 150
        if 'projects_completed' not in developer:
            developer['projects_completed'] = 8
        if 'years_experience' not in developer:
            developer['years_experience'] = 10
        if 'rating' not in developer:
            developer['rating'] = 4.5
        if 'construction_technology' not in developer:
            developer['construction_technology'] = 'Монолитно-каркасная'
        if 'warranty_years' not in developer:
            developer['warranty_years'] = 5
        if 'advantages' not in developer:
            developer['advantages'] = [
                'Качественное строительство',
                'Соблюдение сроков сдачи',
                'Развитая инфраструктура',
                'Выгодные условия покупки'
            ]
        
        # Get all complexes by this developer
        complexes = load_residential_complexes()
        developer_complexes = [c for c in complexes if c.get('developer_id') == developer_id or c.get('developer') == developer['name']]
        
        # Get all properties by this developer
        properties = load_properties()
        developer_properties = [p for p in properties if p.get('developer') == developer['name']]
        
        return render_template('developer_detail.html',
                             developer=developer,
                             complexes=developer_complexes,
                             properties=developer_properties)
    except Exception as e:
        print(f"ERROR in developer_detail route: {e}")
        import traceback
        traceback.print_exc()
        return f"Error 500: {str(e)}", 500

def crop_watermark(image_url, crop_bottom_percent=8):
    """
    Crop watermark from image by removing bottom portion
    
    Args:
        image_url: URL of the image to crop
        crop_bottom_percent: Percentage of image height to crop from bottom (default 8%)
    
    Returns:
        PIL Image object with watermark cropped
    """
    try:
        # Download image
        response = requests.get(image_url, timeout=10)
        response.raise_for_status()
        
        # Open image
        img = Image.open(io.BytesIO(response.content))
        
        # Calculate crop dimensions
        width, height = img.size
        crop_height = int(height * (crop_bottom_percent / 100))
        new_height = height - crop_height
        
        # Crop image (remove bottom portion with watermark)
        cropped_img = img.crop((0, 0, width, new_height))
        
        return cropped_img
    except Exception as e:
        print(f"Error cropping watermark from {image_url}: {e}")
        return None

def generate_qr_code(url):
    """Generate QR code for given URL and return as base64 string"""
    try:
        # Create QR code instance
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        
        # Add data to QR code
        qr.add_data(url)
        qr.make(fit=True)
        
        # Create image
        qr_image = qr.make_image(fill_color="black", back_color="white")
        
        # Convert to base64
        buffer = io.BytesIO()
        qr_image.save(buffer, format='PNG')
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        return qr_base64
    except Exception as e:
        print(f"Error generating QR code: {e}")
        return None

@app.route('/object/<int:property_id>/pdf')
def property_pdf(property_id):
    """Property PDF card page with QR code"""
    property_data = get_property_by_id(property_id)
    if not property_data:
        return redirect(url_for('properties'))
    
    # Calculate cashback for this property using complex_name
    cashback = calculate_cashback(property_data['price'], complex_name=property_data.get('complex_name'))
    
    # Add cashback data to property_data for PDF template
    property_data['cashback_amount'] = cashback
    
    # Get cashback percent from residential complex
    try:
        if property_data.get('complex_name'):
            from models import ResidentialComplex
            complex = ResidentialComplex.query.filter_by(name=property_data['complex_name']).first()
            if complex and complex.cashback_rate:
                property_data['cashback_percent'] = float(complex.cashback_rate)
            else:
                property_data['cashback_percent'] = 5.0  # Default 5%
        else:
            property_data['cashback_percent'] = 5.0  # Default 5%
    except:
        property_data['cashback_percent'] = 5.0  # Default 5%
    
    # Get current date for PDF generation
    current_date = datetime.now().strftime('%d.%m.%Y')
    
    # Generate QR code with link to object page
    # Use custom domain from environment variable or fall back to current request domain
    custom_domain = os.environ.get('QR_DOMAIN')
    if custom_domain:
        # Remove trailing slash and ensure it starts with http:// or https://
        custom_domain = custom_domain.rstrip('/')
        if not custom_domain.startswith(('http://', 'https://')):
            custom_domain = 'https://' + custom_domain
        object_url = custom_domain + url_for('property_detail', property_id=property_id)
    else:
        # Default behavior - use current request domain
        object_url = request.url_root.rstrip('/') + url_for('property_detail', property_id=property_id)
    
    qr_code_base64 = generate_qr_code(object_url)
    
    # Add missing fields for PDF template
    property_data['name'] = property_data.get('title', 'Объект недвижимости')
    
    # Get layout image from residential complex (first photo from gallery)
    complex_layout_image = None
    try:
        if property_data.get('complex_name'):
            from models import ResidentialComplex
            complex = ResidentialComplex.query.filter_by(name=property_data['complex_name']).first()
            if complex and complex.gallery_images:
                try:
                    import json
                    gallery = json.loads(complex.gallery_images)
                    if gallery and isinstance(gallery, list) and len(gallery) > 0:
                        complex_layout_image = gallery[0]
                except (json.JSONDecodeError, TypeError, IndexError) as e:
                    print(f"Error parsing gallery images for complex {property_data.get('complex_name')}: {e}")
    except Exception as e:
        print(f"Error loading complex layout image: {e}")
    
    # Create presentation object for PDF template
    presentation = {
        'title': 'InBack.ru - Кэшбек при покупке недвижимости'
    }
    
    # Create property_images structure expected by PDF template
    property_images = {
        'photos': property_data.get('gallery', []),  # All apartment photos
        'floor_plan': property_data.get('floor_plan')  # Floor plan photo
    }
    
    return render_template('property_pdf.html', 
                         property=property_data,
                         property_images=property_images,
                         presentation=presentation,
                         cashback=cashback,
                         current_date=current_date,
                         qr_code=qr_code_base64,
                         object_url=object_url,
                         complex_layout_image=complex_layout_image)

@app.route('/about')
def about():
    """About page"""
    return render_template('about.html')

@app.route('/how-it-works')
def how_it_works():
    """How it works page"""
    return render_template('how-it-works.html')

@app.route('/reviews')
def reviews():
    """Reviews page"""
    return render_template('reviews.html')

@app.route('/contacts')
def contacts():
    """Contacts page"""
    return render_template('contacts.html')

@app.route('/blog')
def blog():
    """Blog main page with articles listing, search, and categories"""
    from models import BlogPost, Category
    from sqlalchemy import text
    
    # Get search parameters  
    search_query = request.args.get('search', '')
    category_filter = request.args.get('category', '')
    page = int(request.args.get('page', 1))
    per_page = 6
    
    # Загрузка статей только из BlogPost с категорией
    all_articles = []
    
    try:
        # Получаем статьи только из BlogPost с категорией
        blog_posts = BlogPost.query.filter_by(status='published').filter(BlogPost.category_id.isnot(None)).all()
        for post in blog_posts:
            # Получаем название категории
            category_name = None
            if post.category_id:
                category_obj = Category.query.get(post.category_id)
                if category_obj:
                    category_name = category_obj.name
            
            all_articles.append({
                'id': post.id,
                'title': post.title,
                'slug': post.slug,
                'excerpt': post.excerpt or 'Интересная статья о недвижимости',
                'content': post.content,
                'featured_image': post.featured_image or 'https://images.unsplash.com/photo-1560518883-ce09059eeffa?ixlib=rb-4.0.3&auto=format&fit=crop&w=1000&q=80',
                'published_at': post.published_at or post.created_at,
                'created_at': post.created_at,
                'reading_time': getattr(post, 'reading_time', 5),
                'category_id': post.category_id,
                'category_name': category_name,
                'url': f'/blog/{post.slug}',
                'source': 'BlogPost'
            })
        
    except Exception as e:
        print(f"Error loading unified articles for blog: {e}")
    
    # Применяем фильтры
    filtered_articles = all_articles.copy()
    
    # Поиск по тексту
    if search_query:
        filtered_articles = [
            article for article in filtered_articles
            if search_query.lower() in article['title'].lower() or 
               search_query.lower() in (article['excerpt'] or '').lower() or
               search_query.lower() in (article['content'] or '').lower()
        ]
    
    # Фильтр по категории
    if category_filter:
        category = Category.query.filter_by(name=category_filter, is_active=True).first()
        if category:
            filtered_articles = [
                article for article in filtered_articles
                if article['category_id'] == category.id
            ]
    
    # Сортировка по дате
    filtered_articles.sort(key=lambda x: x['published_at'] or x['created_at'], reverse=True)
    
    # Ручная пагинация
    total_articles = len(filtered_articles)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    articles_page = filtered_articles[start_idx:end_idx]
    
    # Создаем объект похожий на paginate для совместимости
    class PaginationMock:
        def __init__(self, items, page, per_page, total):
            self.items = items
            self.page = page
            self.per_page = per_page
            self.total = total
    
    articles = PaginationMock(articles_page, page, per_page, total_articles)
    
    # Если нет фильтрации, показываем все статьи просто списком
    if not search_query and not category_filter:
        # Get all active categories for navigation
        all_categories = Category.query.filter_by(is_active=True).order_by(Category.sort_order, Category.name).all()
        
        # Показываем все статьи простым списком
        articles_to_show = all_articles[:per_page]
        return render_template('blog.html',
                             articles=articles_to_show,  # Первые статьи для отображения
                             all_categories=all_categories,  # Категории для навигации
                             search_query=search_query,
                             category_filter=category_filter,
                             current_page=page,
                             total_articles=len(all_articles),
                             show_category_sections=False)  # Показываем простым списком
    else:
        # Если есть фильтрация - показываем обычным списком
        # Get all active categories for dynamic navigation
        all_categories = Category.query.filter_by(is_active=True).order_by(Category.sort_order, Category.name).all()
        
        return render_template('blog.html',
                             articles=articles_page,
                             all_categories=all_categories,
                             search_query=search_query,
                             category_filter=category_filter,
                             current_page=page,
                             total_articles=total_articles,
                             show_category_sections=False)

# Removed duplicate blog route - using blog_post function at line 7515

@app.route('/blog/category/<category_slug>')
def blog_category(category_slug):
    """Blog category page with search functionality"""
    try:
        from models import BlogPost, Category
        
        # Сначала проверяем, не является ли это slug статьи
        article = BlogPost.query.filter_by(slug=category_slug, status='published').first()
        if article:
            # Это статья, а не категория - перенаправляем на правильный маршрут
            return redirect(url_for('blog_post', slug=category_slug))
        
        # Поиск категории по slug или по имени
        category = Category.query.filter(
            (Category.slug == category_slug) | 
            (Category.name.ilike(f'%{category_slug}%'))
        ).first()
        
        if not category:
            return redirect(url_for('blog'))
        
        # Get search query from URL parameters
        search_query = request.args.get('q', '').strip()
        
        # Get articles in this category
        page = int(request.args.get('page', 1))
        per_page = 6
        
        # Base query - articles in this category using foreign key
        articles_query = BlogPost.query.filter_by(status='published', category_id=category.id)
        
        # Add search filter if query provided
        if search_query:
            from sqlalchemy import or_, func
            search_filter = f"%{search_query.lower()}%"
            articles_query = articles_query.filter(
                or_(
                    func.lower(BlogPost.title).like(search_filter),
                    func.lower(BlogPost.excerpt).like(search_filter),
                    func.lower(BlogPost.content).like(search_filter)
                )
            )
        
        # Order by date descending
        articles_query = articles_query.order_by(BlogPost.created_at.desc())
        
        articles = articles_query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        # Get all categories for navigation
        all_categories = Category.query.filter_by(is_active=True).order_by(Category.sort_order, Category.name).all()
        
        return render_template('blog.html',
                             articles=articles.items,
                             all_categories=all_categories,
                             current_category=category,
                             featured_articles=[],
                             search_query=search_query,
                             category_filter=category_slug,
                             current_page=page,
                             total_pages=articles.pages,
                             total_articles=articles.total)
                             
    except Exception as e:
        # Log error for debugging
        import traceback
        print(f"[ERROR] Exception in blog_category ({category_slug}): {str(e)}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        # Graceful fallback - redirect to main blog page
        flash('Произошла ошибка при загрузке категории. Попробуйте позже.', 'error')
        return redirect(url_for('blog'))

@app.route('/news')
def news():
    """News article page"""
    return render_template('news.html')

@app.route('/streets')
def streets():
    """Streets page"""
    # Load streets from database with slugs
    streets_db = db.session.execute(text("""
        SELECT name, slug, district_id 
        FROM streets 
        ORDER BY name
    """)).fetchall()
    
    # Get district names
    districts_db = db.session.execute(text("""
        SELECT id, name FROM districts
    """)).fetchall()
    districts_map = {d.id: d.name for d in districts_db}
    
    # Format streets data
    streets_data = []
    for street in streets_db:
        # Get first letter for grouping
        first_char = street.name[0].upper() if street.name else 'А'
        
        streets_data.append({
            'name': street.name,
            'slug': street.slug,  # Use slug from database
            'district': districts_map.get(street.district_id, ''),
            'letter': first_char,
            'properties_count': 0,  # Can be calculated if needed
            'new_buildings': 0  # Can be calculated if needed
        })
    
    return render_template('streets.html', 
                         streets=streets_data)

# Redirect from /streets/<street_name> to /street/<slug>
@app.route('/streets/<path:street_name>')
def streets_redirect(street_name):
    """Редирект с /streets/ на /street/ для обратной совместимости"""
    import urllib.parse
    
    # Декодируем URL-encoded имя
    decoded_name = urllib.parse.unquote(street_name)
    
    # Используем ту же логику что и street_slug() для создания slug
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
    }
    
    # Очищаем имя
    name = decoded_name.strip().lower()
    name = re.sub(r'[«»"\(\)\.,:;]', '', name)
    
    # Транслитерация
    result = ''
    for char in name:
        result += translit_map.get(char, char)
    
    # Заменяем пробелы на дефисы и чистим
    result = re.sub(r'\s+', '-', result)
    result = re.sub(r'-+', '-', result)
    slug = result.strip('-')
    
    # Редирект на правильный URL
    return redirect(url_for('street_detail', street_name=slug), code=301)

@app.route('/street/<path:street_name>')
def street_detail(street_name):
    """Страница конкретной улицы с описанием и картой"""
    try:
        # Сначала ищем улицу в базе данных по slug
        street_db = db.session.execute(text("""
            SELECT name, slug, latitude, longitude, zoom_level, geometry, geometry_source
            FROM streets 
            WHERE slug = :street_slug
        """), {'street_slug': street_name}).fetchone()
        
        if street_db:
            # Вычисление динамических переменных для SEO
            import math
            
            # Определяем тип улицы
            street_type = 'улице'
            street_type_nominative = 'улица'
            name_lower = street_db.name.lower()
            if 'проезд' in name_lower:
                street_type = 'проезде'
                street_type_nominative = 'проезд'
            elif 'переулок' in name_lower or 'пер.' in name_lower:
                street_type = 'переулке'
                street_type_nominative = 'переулок'
            elif 'бульвар' in name_lower:
                street_type = 'бульваре'
                street_type_nominative = 'бульвар'
            elif 'площадь' in name_lower:
                street_type = 'площади'
                street_type_nominative = 'площадь'
            elif 'шоссе' in name_lower:
                street_type = 'шоссе'
                street_type_nominative = 'шоссе'
            
            # Вычисляем расстояние до центра Краснодара
            distance_to_center = 0
            if street_db.latitude and street_db.longitude:
                center_lat, center_lng = 45.0448, 38.9760
                lat_diff = street_db.latitude - center_lat
                lng_diff = street_db.longitude - center_lng
                distance_to_center = round(math.sqrt(lat_diff**2 + lng_diff**2) * 111, 1)  # примерно в км
                
                coordinates = {
                    'lat': float(street_db.latitude),
                    'lng': float(street_db.longitude)
                }
            else:
                coordinates = {
                    'lat': 45.0448,
                    'lng': 38.9760
                }
            
            # Определяем зону города по расстоянию
            location_zone = 'центре города'
            if distance_to_center < 3:
                location_zone = 'центре города'
            elif distance_to_center < 7:
                location_zone = 'средней зоне города'
            else:
                location_zone = 'отдалённой зоне города'
            
            # Вариации описаний на основе первой буквы (для уникальности)
            first_letter_ord = ord(street_db.name[0].upper()) if street_db.name else 0
            variation = first_letter_ord % 4
            
            advantages = [
                ['современными ЖК', 'развитой инфраструктурой', 'отличной транспортной доступностью'],
                ['проверенными застройщиками', 'высоким инвестиционным потенциалом', 'удобным расположением'],
                ['качественными новостройками', 'перспективным районом', 'близостью к центру'],
                ['надёжными девелоперами', 'комфортной средой', 'активным развитием района']
            ][variation]
            
            why_buy_variants = [
                'выгодное расположение и хорошая транспортная доступность',
                'развитая инфраструктура и близость ко всем необходимым объектам',
                'перспективный район с высоким инвестиционным потенциалом',
                'комфортные условия для жизни и активное развитие территории'
            ][variation]
            
            # Используем данные из базы данных
            street = {
                'name': street_db.name,
                'slug': street_db.slug,
                'district': '',
                'description': f'{street_type_nominative.capitalize()} {street_db.name} в Краснодаре',
                'geometry': street_db.geometry if street_db.geometry else None,
                'geometry_source': street_db.geometry_source if street_db.geometry_source else None,
                'street_type': street_type,
                'street_type_nominative': street_type_nominative,
                'distance_to_center': distance_to_center,
                'location_zone': location_zone,
                'advantages': advantages,
                'why_buy': why_buy_variants
            }
                
            app.logger.debug(f"Found street in database: {street['name']} with coordinates: {coordinates}")
            
            # Загружаем данные о свойствах для этой улицы (если есть)
            properties_on_street = []
            try:
                with open('data/properties_new.json', 'r', encoding='utf-8') as f:
                    properties_data = json.load(f)
                
                # Фильтруем свойства по улице
                for prop in properties_data:
                    if (street['name'].lower() in prop.get('location', '').lower() or
                        street['name'].lower() in prop.get('full_address', '').lower()):
                        properties_on_street.append(prop)
            except:
                pass
            
            return render_template('street_detail.html',
                                 street=street,
                                 coordinates=coordinates,
                                 properties=properties_on_street,
                                 title=f'{street["name"]} - новостройки с кэшбеком | InBack',
                                 yandex_api_key=os.environ.get('YANDEX_MAPS_API_KEY', ''))
        else:
            # Ищем в JSON файле как резервный вариант
            streets_data = load_streets()
            
            # Ищем улицу по имени (учитываем URL-кодирование)
            street_name_decoded = street_name.replace('-', ' ').replace('_', ' ')
            street = None
            
            # Логируем для отладки
            app.logger.debug(f"Looking for street: {street_name} -> {street_name_decoded}")
        
        # Функция транслитерации для поиска старых URL
        def translit_to_latin(text):
            translit_map = {
                'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
                'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm',
                'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
                'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
                'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
            }
            result = ''
            for char in text.lower():
                result += translit_map.get(char, char)
            return result
        
        for s in streets_data:
            # Создаем URL-slug точно так же, как в фильтре (с кириллицей)
            street_slug_generated = s['name'].lower().replace(' ', '-').replace('.', '').replace('(', '').replace(')', '').replace(',', '')
            
            # Создаем полную транслитерацию для обратной совместимости
            translit_name = translit_to_latin(s['name'])
            translit_slug = translit_name.replace(' ', '-').replace('.', '').replace('(', '').replace(')', '').replace(',', '')
            
            # Простая замена символов (как было раньше)
            simple_translit = s['name'].lower().replace(' ', '-').replace('.', '').replace('ё', 'e').replace('й', 'i').replace('а', 'a').replace('г', 'g').replace('р', 'r').replace('и', 'i').replace('н', 'n').replace('(', '').replace(')', '').replace(',', '')
            
            # Множественные варианты поиска
            if (street_slug_generated == street_name.lower() or
                translit_slug == street_name.lower() or
                simple_translit == street_name.lower() or
                s['name'].lower() == street_name_decoded.lower() or
                s['name'].lower().replace(' ул.', '').replace(' ул', '') == street_name_decoded.lower().replace(' ул.', '').replace(' ул', '')):
                street = s
                app.logger.debug(f"Found street: {s['name']} with slug: {street_slug_generated}, translit: {translit_slug}")
                break
        
        if not street:
            # Пробуем найти частичное совпадение
            for s in streets_data:
                street_name_clean = street_name_decoded.lower().replace('ул', '').replace('.', '').strip()
                street_db_clean = s['name'].lower().replace('ул.', '').replace('ул', '').replace('.', '').strip()
                
                if (street_name_clean in street_db_clean or 
                    street_db_clean in street_name_clean or
                    street_name_decoded.lower() in s['name'].lower()):
                    street = s
                    app.logger.debug(f"Found street by partial match: {s['name']}")
                    break
        
        if not street:
            app.logger.error(f"Street not found: {street_name} ({street_name_decoded})")
            abort(404)
        
        # Получаем координаты из базы данных
        from models import Street
        
        # Ищем улицу в базе данных по названию
        street_db = Street.query.filter_by(name=street['name']).first()
        
        if street_db and street_db.latitude and street_db.longitude:
            coordinates = {
                'lat': float(street_db.latitude),
                'lng': float(street_db.longitude)
            }
        else:
            # Если координат нет, используем центр Краснодара
            coordinates = {
                'lat': 45.035470,
                'lng': 38.975313
            }
        
        # Загружаем данные о свойствах для этой улицы (если есть)
        properties_on_street = []
        try:
            with open('data/properties_new.json', 'r', encoding='utf-8') as f:
                properties_data = json.load(f)
            
            # Фильтруем свойства по улице
            for prop in properties_data:
                if (street['name'].lower() in prop.get('location', '').lower() or
                    street['name'].lower() in prop.get('full_address', '').lower()):
                    properties_on_street.append(prop)
        except:
            pass
        
        return render_template('street_detail.html',
                             street=street,
                             coordinates=coordinates,
                             properties=properties_on_street,
                             title=f'{street["name"]} - новостройки с кэшбеком | InBack',
                             yandex_api_key=os.environ.get('YANDEX_MAPS_API_KEY', ''))
    
    except Exception as e:
        app.logger.error(f"Error loading street detail: {e}")
        abort(404)

@app.route('/sitemap.xml')
def sitemap():
    """Serve static sitemap.xml file"""
    try:
        # Читаем статический sitemap файл
        sitemap_path = os.path.join(app.static_folder, 'sitemap.xml')
        
        if os.path.exists(sitemap_path):
            with open(sitemap_path, 'r', encoding='utf-8') as f:
                xml_content = f.read()
            
            response = app.response_class(
                response=xml_content,
                status=200,
                mimetype='application/xml'
            )
            return response
        else:
            # Если файла нет, создаем базовый sitemap
            xml_content = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://inback.ru/</loc>
    <lastmod>2025-09-06</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://inback.ru/properties</loc>
    <lastmod>2025-09-06</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
</urlset>'''
            
            response = app.response_class(
                response=xml_content,
                status=200,
                mimetype='application/xml'
            )
            return response
        
    except Exception as e:
        app.logger.error(f"Error serving sitemap: {e}")
        abort(500)

@app.route('/google873bf5c5df6b6710.html')
def google_verification():
    """Google Search Console verification file"""
    return 'google-site-verification: google873bf5c5df6b6710.html'

@app.route('/comparison')
@login_required
def comparison():
    """Unified comparison page for properties and complexes"""
    return render_template('comparison.html')

@app.route('/comparison-new')
@login_required
def comparison_new():
    """New improved comparison page for properties and complexes"""
    return render_template('comparison_new.html')

@app.route('/thank-you')
def thank_you():
    """Thank you page after form submission"""
    return render_template('thank_you.html')

@app.route('/api/property/<int:property_id>')
def api_property_detail(property_id):
    """API endpoint to get property data for comparison"""
    property_data = get_property_by_id(property_id)
    
    if not property_data:
        return jsonify({'error': 'Property not found'}), 404
    
    
    # Use cashback_rate from excel_properties (already loaded in property_data)
    cashback_rate = property_data.get('cashback_rate', 5.0)
    
    # Calculate cashback amount using the actual cashback_rate from database
    if property_data.get('price'):
        property_data['cashback'] = round(property_data['price'] * cashback_rate / 100)
    else:
        property_data['cashback'] = 0
    
    # Add fields expected by comparison interface
    property_data['object_min_floor'] = property_data.get('floor')
    property_data['object_max_floor'] = property_data.get('total_floors')
    property_data['complex_name'] = property_data.get('residential_complex')
    
    return jsonify(property_data)

@app.route('/complex-comparison')
def complex_comparison():
    """Complex comparison page"""
    return render_template('complex_comparison.html')


@app.route('/favorites')
def favorites():
    """Favorites page with animated heart pulse effects"""
    return render_template('favorites.html')



@app.route('/robots.txt')
def robots_txt():
    """Robots.txt for search engine crawlers"""
    robots_content = """User-agent: *
Allow: /
Disallow: /admin/
Disallow: /auth/
Disallow: /api/
Disallow: /manager/
Disallow: /dashboard

Sitemap: https://inback.ru/sitemap.xml

# Crawl-delay for better server performance
Crawl-delay: 1

# Specific rules for major search engines
User-agent: Googlebot
Allow: /
Disallow: /admin/
Disallow: /auth/
Disallow: /api/
Disallow: /manager/

User-agent: Yandex
Allow: /
Disallow: /admin/
Disallow: /auth/
Disallow: /api/
Disallow: /manager/

User-agent: Bingbot
Allow: /
Disallow: /admin/
Disallow: /auth/
Disallow: /api/
Disallow: /manager/"""
    
    return app.response_class(
        response=robots_content,
        status=200,
        mimetype='text/plain'
    )

# Old blog search function removed - using updated version at bottom of file


@app.route('/api/residential-complexes')
def api_residential_complexes():
    """API endpoint for getting residential complexes for cashback calculator using normalized tables"""
    from repositories.property_repository import ResidentialComplexRepository, PropertyRepository
    
    try:
        complexes = ResidentialComplexRepository.get_all_active()
        all_stats = PropertyRepository.get_all_property_stats()
        
        api_complexes = []
        for complex in complexes:
            cashback_rate = complex.cashback_rate if complex.cashback_rate else 5.0
            stats = all_stats.get(complex.id, {'min_price': 0})
            
            api_complexes.append({
                'id': complex.id,
                'name': complex.name,
                'cashback_rate': cashback_rate,
                'price_from': stats['min_price'],
                'district': complex.district or 'Краснодар'
            })
        
        print(f"API residential-complexes loaded from database: {len(api_complexes)} complexes")
        return jsonify({'complexes': api_complexes})
    
    except Exception as e:
        # Load all residential complexes from JSON data
        try:
            complexes = load_residential_complexes()
            api_complexes = []
            
            for complex in complexes:
                # Extract unique name and calculate cashback rate
                complex_name = complex.get('name', 'Неизвестный ЖК')
                cashback_rate = 5.0  # Default rate
                
                # Try to get rate from complex data or calculate based on price
                if 'cashback_rate' in complex:
                    cashback_rate = float(complex['cashback_rate'])
                elif complex.get('real_price_from'):
                    # Calculate rate based on price range (higher price = lower rate)
                    price = complex.get('real_price_from', 5000000)
                    if price < 3000000:
                        cashback_rate = 5.0
                    elif price < 8000000:
                        cashback_rate = 4.5
                    else:
                        cashback_rate = 4.0
                
                api_complexes.append({
                    'id': complex.get('id', len(api_complexes) + 1),
                    'name': complex_name,
                    'cashback_rate': cashback_rate,
                    'price_from': complex.get('real_price_from'),
                    'district': complex.get('district', 'Краснодар')
                })
            
            # Remove duplicates by name
            unique_complexes = {}
            for complex in api_complexes:
                name = complex['name']
                if name not in unique_complexes:
                    unique_complexes[name] = complex
            
            return jsonify({'complexes': list(unique_complexes.values())})
        
        except Exception as json_error:
            print(f"Error loading JSON complexes: {json_error}")
            # Final fallback to simple list
            return jsonify({
                'complexes': [
                    {'id': 1, 'name': 'ЖК «Летний»', 'cashback_rate': 5.0},
                    {'id': 2, 'name': 'ЖК «Чайные холмы»', 'cashback_rate': 4.5},
                    {'id': 3, 'name': 'ЖК «Кислород»', 'cashback_rate': 5.0},
                    {'id': 4, 'name': 'ЖК «Гранд Каскад»', 'cashback_rate': 4.0}
                ]
            })

@app.route('/api/residential-complexes-full')
def api_residential_complexes_full():
    """API endpoint for getting all residential complexes using normalized tables"""
    from repositories.property_repository import ResidentialComplexRepository
    
    try:
        complexes = ResidentialComplexRepository.get_all_active()
        all_stats = PropertyRepository.get_all_property_stats()
        
        complexes_data = []
        for complex in complexes:
            stats = all_stats.get(complex.id, {
                'min_price': 0,
                'max_price': 0,
                'total_properties': 0
            })
            
            complex_dict = {
                'id': complex.id,
                'name': complex.name,
                'cashback_rate': complex.cashback_rate if complex.cashback_rate else 5.0,
                'district': complex.district or 'Краснодар',
                'address': complex.address or 'Адрес не указан',
                'developer': complex.developer.name if complex.developer else 'Не указан',
                'min_price': stats['min_price'],
                'max_price': stats['max_price'],
                'real_price_from': stats['min_price'],
                'real_price_to': stats['max_price'],
                'available_apartments': stats['total_properties']
            }
            complexes_data.append(complex_dict)
        
        return jsonify({'complexes': complexes_data})
    except Exception as e:
        print(f"Error loading residential complexes: {e}")
        return jsonify({'complexes': []}), 500

@app.route('/api/cashback/calculate', methods=['POST'])
# @csrf.exempt  # CSRF disabled  # Disable CSRF for API endpoint
def api_calculate_cashback():
    """API endpoint for calculating cashback"""
    try:
        data = request.get_json()
        price = float(data.get('price', 0))
        complex_id = data.get('complex_id')
        
        if not price or price <= 0:
            return jsonify({'error': 'Invalid price'}), 400
        
        # Get cashback rate from database
        cashback_rate = 5.0  # default
        
        if complex_id:
            try:
                # Ищем комплекс в JSON данных
                import json
                import os
                
                residential_complexes_file = 'static/data/residential_complexes.json'
                if os.path.exists(residential_complexes_file):
                    with open(residential_complexes_file, 'r', encoding='utf-8') as file:
                        json_complexes = json.load(file)
                    
                    for complex in json_complexes:
                        if str(complex.get('id')) == str(complex_id):
                            if 'cashback_rate' in complex:
                                cashback_rate = float(complex['cashback_rate'])
                            elif complex.get('real_price_from'):
                                # Calculate rate based on price range
                                complex_price = complex.get('real_price_from', 5000000)
                                if complex_price < 3000000:
                                    cashback_rate = 5.0
                                elif complex_price < 8000000:
                                    cashback_rate = 4.5
                                else:
                                    cashback_rate = 4.0
                            break
                            
                # Если не нашли в JSON, используем fallback ставки по ID
            except:
                # Fallback rates
                complex_rates = {
                    1: 5.5, 2: 6.0, 3: 7.0, 4: 5.0,
                    5: 6.5, 6: 5.5, 7: 7.5, 8: 8.0
                }
                cashback_rate = complex_rates.get(int(complex_id), 5.0)
        
        cashback_amount = price * (cashback_rate / 100)
        
        # Cap at maximum
        max_cashback = 500000
        if cashback_amount > max_cashback:
            cashback_amount = max_cashback
        
        return jsonify({
            'cashback_amount': int(cashback_amount),
            'cashback_rate': cashback_rate,
            'price': int(price),
            'formatted_amount': f"{int(cashback_amount):,}".replace(',', ' ')
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/cashback/apply', methods=['POST'])
@login_required
# @csrf.exempt  # CSRF disabled  # Disable CSRF for API endpoint
def api_apply_cashback():
    """API endpoint for submitting cashback application"""
    try:
        from models import CashbackApplication, UserActivity, CallbackRequest
        
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Неверный формат данных'}), 400
            
        price = data.get('price')
        complex_id = data.get('complex_id')  # Может быть null для калькулятора
        complex_name = data.get('complex_name', 'Не указан')
        cashback_amount = data.get('cashback_amount')
        cashback_rate = data.get('cashback_rate', 2.5)
        user_phone = data.get('phone', '')
        user_name = data.get('name', '')
        
        # Validate required fields (complex_id опционален)
        if not all([price, cashback_amount, user_phone, user_name]):
            return jsonify({'error': 'Заполните все обязательные поля'}), 400
        
        # Validate data types
        try:
            price = float(price)
            cashback_amount = float(cashback_amount)
            cashback_rate = float(cashback_rate)
        except (ValueError, TypeError):
            return jsonify({'error': 'Неверный формат числовых данных'}), 400
        
        # Create cashback application
        cashback_app = CashbackApplication(
            user_id=current_user.id,
            property_name=f"Квартира в {complex_name}",
            property_type="Квартира",
            property_size=50.0,  # Default size, can be improved later
            property_price=int(price),
            complex_name=complex_name,
            developer_name=data.get('developer_name', 'Не указан'),
            cashback_amount=int(cashback_amount),
            cashback_percent=cashback_rate,
            status='В обработке'
        )
        
        db.session.add(cashback_app)
        
        # Record user activity
        UserActivity.log_activity(
            user_id=current_user.id,
            activity_type='cashback_application',
            description=f'Подана заявка на кешбек {int(cashback_amount):,} ₽ по объекту в {complex_name}'.replace(',', ' '),
            complex_id=complex_id
        )
        
        # Create callback request for manager
        callback = CallbackRequest(
            name=user_name,
            phone=user_phone,
            notes=f"Заявка на кешбек {int(cashback_amount):,} ₽ при покупке квартиры в {complex_name} стоимостью {int(price):,} ₽".replace(',', ' ')
        )
        
        db.session.add(callback)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Заявка успешно отправлена! Менеджер свяжется с вами в ближайшее время.'
        })
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Ошибка при отправке заявки: {str(e)}'}), 500

@app.route('/api/image-proxy')
def image_proxy():
    """
    Proxy endpoint to serve images with watermark cropped out
    Usage: /api/image-proxy?url=<image_url>&crop=<percent>
    """
    image_url = request.args.get('url')
    crop_percent = request.args.get('crop', '8')  # Default 8%
    
    if not image_url:
        return jsonify({'error': 'No image URL provided'}), 400
    
    try:
        crop_percent = int(crop_percent)
        
        # Crop watermark from image
        cropped_img = crop_watermark(image_url, crop_bottom_percent=crop_percent)
        
        if cropped_img is None:
            # If cropping failed, redirect to original image
            return redirect(image_url)
        
        # Convert to bytes
        img_io = io.BytesIO()
        cropped_img.save(img_io, format='JPEG', quality=90, optimize=True)
        img_io.seek(0)
        
        # Return image with caching headers
        response = make_response(send_file(img_io, mimetype='image/jpeg'))
        response.headers['Cache-Control'] = 'public, max-age=2592000'  # Cache for 30 days
        response.headers['X-Content-Type-Options'] = 'nosniff'
        
        return response
        
    except Exception as e:
        print(f"Error in image proxy: {e}")
        # Fallback to original image on error
        return redirect(image_url)

@app.route('/api/search/suggestions')
def search_suggestions():
    """API endpoint for search suggestions (autocomplete) - REAL DATABASE VERSION"""
    query = request.args.get('query', request.args.get('q', '')).lower().strip()
    if not query or len(query) < 2:
        return jsonify([])
    
    suggestions = []
    
    try:
        # 1. Search by room types (PRIORITY - user's main request)
        room_suggestions = {
            'студ': 'Студия',
            '1-к': '1-комнатная',
            '1-ком': '1-комнатная', 
            '1 к': '1-комнатная',
            'одн': '1-комнатная',
            '2-к': '2-комнатная',
            '2-ком': '2-комнатная',
            '2 к': '2-комнатная', 
            'двух': '2-комнатная',
            '3-к': '3-комнатная',
            '3-ком': '3-комнатная',
            '3 к': '3-комнатная',
            'трех': '3-комнатная',
            'трёх': '3-комнатная',
            '4-к': '4-комнатная',
            '4-ком': '4-комнатная',
            'четыр': '4-комнатная'
        }
        
        for pattern, room_type in room_suggestions.items():
            if pattern in query:
                # ✅ MIGRATED: Count properties by room type using PropertyRepository
                if 'студ' in pattern:
                    count = PropertyRepository.count_active(filters={'rooms': [0]})
                else:
                    room_num = room_type.split('-')[0] if '-' in room_type else '1'
                    count = PropertyRepository.count_active(filters={'rooms': [int(room_num)]})
                
                # Создаем URL с тем же параметром что быстрые фильтры
                if 'студ' in pattern:
                    room_param = '0'
                else:
                    room_param = room_type.split('-')[0] if '-' in room_type else '1'
                
                suggestions.append({
                    'type': 'room_type', 
                    'text': room_type,
                    'title': room_type,  # Добавляем title для совместимости
                    'subtitle': f'Найдено {count} квартир',
                    'url': url_for('properties', rooms=room_param)  # rooms=1 как быстрые фильтры
                })
        
        # ✅ MIGRATED: Search in residential complexes using ORM
        from models import ResidentialComplex, Property
        complexes_query = (
            db.session.query(ResidentialComplex.name, func.count(Property.id).label('count'))
            .join(Property, Property.complex_id == ResidentialComplex.id, isouter=True)
            .filter(
                ResidentialComplex.name.ilike(f'%{query}%'),
                Property.is_active == True
            )
            .group_by(ResidentialComplex.name)
            .order_by(func.count(Property.id).desc())
            .limit(5)
            .all()
        )
        
        for row in complexes_query:
            if row[0] and len(row[0]) > 2:  # Skip empty/short names
                suggestions.append({
                    'type': 'complex',
                    'text': row[0],
                    'subtitle': f'{row[1]} квартир доступно',
                    'url': url_for('properties', complex=row[0])
                })
        
        # ✅ MIGRATED: Search in developers using ORM
        from models import Developer
        developers_query = (
            db.session.query(Developer.name, func.count(Property.id).label('count'))
            .join(Property, Property.developer_id == Developer.id, isouter=True)
            .filter(
                Developer.name.ilike(f'%{query}%'),
                Property.is_active == True
            )
            .group_by(Developer.name)
            .order_by(func.count(Property.id).desc())
            .limit(3)
            .all()
        )
        
        for row in developers_query:
            if row[0] and len(row[0]) > 2:
                suggestions.append({
                    'type': 'developer',
                    'text': row[0],
                    'subtitle': f'Застройщик • {row[1]} проектов',
                    'url': url_for('properties', developer=row[0])
                })
        
        # ✅ MIGRATED: Search in districts using ORM
        from models import District
        districts_query = (
            db.session.query(District.name, func.count(Property.id).label('count'))
            .join(Property, Property.district_id == District.id, isouter=True)
            .filter(
                District.name.ilike(f'%{query}%'),
                Property.is_active == True
            )
            .group_by(District.name)
            .order_by(func.count(Property.id).desc())
            .limit(3)
            .all()
        )
        
        for row in districts_query:
            if row[0] and 'Краснодарский' not in row[0]:  # Skip generic region name
                clean_district = row[0].replace('Россия, ', '').replace('Краснодарский край, ', '')
                suggestions.append({
                    'type': 'district',
                    'text': clean_district,
                    'subtitle': f'{row[1]} квартир в районе',
                    'url': url_for('properties', district=clean_district)
                })
        
        # Sort by relevance (room types first, then exact matches)
        suggestions.sort(key=lambda x: (
            0 if x['type'] == 'room_type' else 1,
            0 if x['text'].lower().startswith(query) else 1,
            len(x['text'])
        ))
        
        return jsonify(suggestions[:8])  # Return top 8 suggestions
        
    except Exception as e:
        app.logger.error(f"Error in search suggestions: {e}")
        return jsonify([])

# Mortgage routes
@app.route('/ipoteka')
def ipoteka():
    """Main mortgage page"""
    return render_template('ipoteka.html')

@app.route('/family-mortgage')
def family_mortgage():
    """Family mortgage page"""
    return render_template('family_mortgage.html')

@app.route('/it-mortgage')
def it_mortgage():
    """IT mortgage page"""
    return render_template('it_mortgage.html')

@app.route('/insurance')
def insurance():
    """Insurance page"""
    return render_template('insurance.html')

@app.route('/submit-insurance-application', methods=['POST'])
def submit_insurance_application():
    """Submit insurance application with CSRF protection and enhanced validation"""
    try:
        # Validate CSRF token for form submissions
        try:
            validate_csrf(request.form.get('csrf_token'))
        except Exception:
            return jsonify({'success': False, 'error': 'CSRF token missing or invalid'}), 400
        
        # Get form data
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        bank = request.form.get('bank', '').strip()
        credit_amount = request.form.get('credit_amount', '').strip()
        birth_date = request.form.get('birth_date', '').strip()
        gender = request.form.get('gender', '').strip()
        comment = request.form.get('comment', '').strip()
        
        # Enhanced validation for required fields
        if not all([name, phone, bank, credit_amount, birth_date, gender]):
            return jsonify({'success': False, 'error': 'Заполните все обязательные поля'}), 400
        
        # Validate name (2-50 characters, only letters and spaces)
        if not re.match(r'^[а-яА-ЯёЁa-zA-Z\s]{2,50}$', name):
            return jsonify({'success': False, 'error': 'Некорректное имя'}), 400
        
        # Validate phone (Russian phone number format)
        phone_clean = re.sub(r'[^\d]', '', phone)
        if not re.match(r'^[78]\d{10}$', phone_clean):
            return jsonify({'success': False, 'error': 'Некорректный номер телефона'}), 400
        
        # Validate credit amount (numeric, reasonable range)
        try:
            credit_amount_num = float(re.sub(r'[^\d.]', '', credit_amount))
            if credit_amount_num < 100000 or credit_amount_num > 50000000:
                return jsonify({'success': False, 'error': 'Сумма кредита должна быть от 100 000 до 50 000 000 рублей'}), 400
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Некорректная сумма кредита'}), 400
        
        # Validate birth date
        try:
            from datetime import datetime
            birth_dt = datetime.strptime(birth_date, '%Y-%m-%d')
            age = (datetime.now() - birth_dt).days / 365.25
            if age < 18 or age > 100:
                return jsonify({'success': False, 'error': 'Возраст должен быть от 18 до 100 лет'}), 400
        except ValueError:
            return jsonify({'success': False, 'error': 'Некорректная дата рождения'}), 400
        
        # Validate gender
        if gender not in ['Мужчина', 'Женщина']:
            return jsonify({'success': False, 'error': 'Некорректный пол'}), 400
        
        # Format credit amount for display
        try:
            credit_amount_num = int(credit_amount)
            credit_amount_formatted = f"{credit_amount_num:,}".replace(",", " ") + " ₽"
        except:
            credit_amount_formatted = credit_amount + " ₽"
        
        # Dual notification: send to both email and Telegram
        current_time = datetime.now().strftime('%d.%m.%Y %H:%M')
        
        # Send email notification
        email_success = False
        try:
            email_success = send_email(
                'bithome@mail.ru', 
                f'Новая заявка на страхование от {name}', 
                'emails/insurance_application.html', 
                name=name, 
                phone=phone, 
                bank=bank, 
                credit_amount=credit_amount_formatted, 
                birth_date=birth_date, 
                gender=gender, 
                comment=comment, 
                submitted_at=datetime.now(),
                current_time=current_time
            )
        except Exception as email_error:
            app.logger.error(f"Error sending insurance application email: {email_error}")
        
        # Send Telegram notification
        telegram_success = False
        try:
            from email_service import send_telegram_insurance_notification
            telegram_success = send_telegram_insurance_notification(
                name=name,
                phone=phone,
                bank=bank,
                credit_amount=credit_amount_formatted,
                birth_date=birth_date,
                gender=gender,
                comment=comment,
                current_time=current_time
            )
        except Exception as telegram_error:
            app.logger.error(f"Error sending insurance application Telegram: {telegram_error}")
        
        # Determine response based on both results
        if email_success and telegram_success:
            return jsonify({'success': True, 'message': 'Заявка успешно отправлена на email и в Telegram'})
        elif email_success and not telegram_success:
            return jsonify({'success': True, 'message': 'Заявка отправлена на email, но не удалось отправить в Telegram', 'warning': True})
        elif not email_success and telegram_success:
            return jsonify({'success': True, 'message': 'Заявка отправлена в Telegram, но не удалось отправить на email', 'warning': True})
        else:
            return jsonify({'success': False, 'error': 'Ошибка отправки заявки и на email, и в Telegram'}), 500
            
    except Exception as e:
        app.logger.error(f"Error in insurance application: {e}")
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/api/check-it-company', methods=['POST'])
@csrf.exempt
def check_it_company():
    """Check if company is in IT companies list by INN or company name"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Нет данных для проверки'}), 400
            
        inn = data.get('inn', '').strip()
        company_name = data.get('company_name', '').strip()
        
        if not inn and not company_name:
            return jsonify({'error': 'Необходимо указать ИНН или название компании'}), 400
        
        # Поиск по ИНН
        if inn:
            try:
                inn_int = int(inn)
                company = db.session.execute(text("""
                    SELECT inn, name FROM it_companies 
                    WHERE inn = :inn LIMIT 1
                """), {'inn': inn_int}).fetchone()
                
                if company:
                    return jsonify({
                        'found': True,
                        'inn': company[0],
                        'company_name': company[1],
                        'message': 'Компания найдена в реестре ИТ-организаций'
                    })
            except ValueError:
                pass
        
        # Поиск по названию компании (частичное совпадение)
        if company_name:
            company = db.session.execute(text("""
                SELECT inn, name FROM it_companies 
                WHERE LOWER(name) LIKE LOWER(:company_name) 
                LIMIT 1
            """), {'company_name': f'%{company_name}%'}).fetchone()
            
            if company:
                return jsonify({
                    'found': True,
                    'inn': company[0],
                    'company_name': company[1],
                    'message': 'Компания найдена в реестре ИТ-организаций'
                })
        
        return jsonify({
            'found': False,
            'message': 'Компания не найдена в реестре ИТ-организаций. Проверьте правильность ИНН или названия компании.'
        })
        
    except Exception as e:
        print(f"Error checking IT company: {e}")
        return jsonify({'error': 'Ошибка при проверке компании'}), 500

@app.route('/api/suggest-it-companies', methods=['POST'])
@csrf.exempt
def suggest_it_companies():
    """Get IT company suggestions for autocomplete"""
    try:
        data = request.get_json()
        query = data.get('query', '').strip().lower()
        
        if len(query) < 2:
            return jsonify({'suggestions': []})
            
        # Search for companies matching the query
        suggestions = db.session.execute(text("""
            SELECT DISTINCT name FROM it_companies 
            WHERE LOWER(name) LIKE :query 
            ORDER BY name 
            LIMIT 10
        """), {'query': f'%{query}%'}).fetchall()
        
        return jsonify({
            'suggestions': [suggestion[0] for suggestion in suggestions]
        })
        
    except Exception as e:
        print(f"Error in suggest_it_companies: {str(e)}")
        return jsonify({'suggestions': []})

@app.route('/api/detect-city', methods=['GET'])
@csrf.exempt
def detect_city():
    """Detect user's city by IP address using ipwhois.io API"""
    
    # Словарь для перевода транслита городов в русские названия
    city_translations = {
        'Krasnodar': 'Краснодар',
        'Moscow': 'Москва',
        'Saint Petersburg': 'Санкт-Петербург',
        'Novosibirsk': 'Новосибирск',
        'Yekaterinburg': 'Екатеринбург',
        'Nizhny Novgorod': 'Нижний Новгород',
        'Kazan': 'Казань',
        'Chelyabinsk': 'Челябинск',
        'Omsk': 'Омск',
        'Samara': 'Самара',
        'Rostov-on-Don': 'Ростов-на-Дону',
        'Ufa': 'Уфа',
        'Krasnoyarsk': 'Красноярск',
        'Voronezh': 'Воронеж',
        'Perm': 'Пермь',
        'Volgograd': 'Волгоград',
        'Saratov': 'Саратов',
        'Tyumen': 'Тюмень',
        'Tolyatti': 'Тольятти',
        'Izhevsk': 'Ижевск',
        'Barnaul': 'Барнаул',
        'Ulyanovsk': 'Ульяновск',
        'Irkutsk': 'Иркутск',
        'Khabarovsk': 'Хабаровск',
        'Yaroslavl': 'Ярославль',
        'Vladivostok': 'Владивосток',
        'Makhachkala': 'Махачкала',
        'Tomsk': 'Томск',
        'Orenburg': 'Оренбург',
        'Kemerovo': 'Кемерово',
        'Novokuznetsk': 'Новокузнецк',
        'Ryazan': 'Рязань',
        'Astrakhan': 'Астрахань',
        'Naberezhnye Chelny': 'Набережные Челны',
        'Penza': 'Пенза',
        'Lipetsk': 'Липецк',
        'Kirov': 'Киров',
        'Cheboksary': 'Чебоксары',
        'Kaliningrad': 'Калининград',
        'Tula': 'Тула',
        'Kursk': 'Курск',
        'Sochi': 'Сочи',
        'Stavropol': 'Ставрополь',
        'Ulan-Ude': 'Улан-Удэ',
        'Tver': 'Тверь',
        'Magnitogorsk': 'Магнитогорск',
        'Bryansk': 'Брянск',
        'Ivanovo': 'Иваново',
        'Belgorod': 'Белгород'
    }
    
    # Словарь для перевода регионов
    region_translations = {
        'Krasnodar Krai': 'Краснодарский край',
        'Krasnodar Territory': 'Краснодарский край',
        'Moscow': 'Москва',
        'Saint Petersburg': 'Санкт-Петербург',
        'Moscow Oblast': 'Московская область',
        'Sverdlovsk Oblast': 'Свердловская область',
        'Novosibirsk Oblast': 'Новосибирская область',
        'Rostov Oblast': 'Ростовская область',
        'Tatarstan': 'Республика Татарстан',
        'Bashkortostan': 'Республика Башкортостан',
        'Chelyabinsk Oblast': 'Челябинская область'
    }
    
    try:
        # Get user's IP address from request
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ',' in user_ip:
            user_ip = user_ip.split(',')[0].strip()
        
        # Skip localhost/private IPs
        if user_ip in ['127.0.0.1', 'localhost', '::1'] or user_ip.startswith('192.168.') or user_ip.startswith('10.'):
            return jsonify({
                'success': True,
                'city': 'Краснодар',
                'region': 'Краснодарский край',
                'country': 'Россия',
                'detected': False,
                'message': 'Локальный IP, используется город по умолчанию'
            })
        
        # Use ipwhois.io API (free, no API key required, 10,000 requests/month)
        api_url = f'http://ipwho.is/{user_ip}'
        response = requests.get(api_url, timeout=3)
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get('success', False):
                city_en = data.get('city', 'Krasnodar')
                region_en = data.get('region', 'Krasnodar Krai')
                
                # Переводим город и регион на русский
                city = city_translations.get(city_en, city_en if not city_en else 'Краснодар')
                region = region_translations.get(region_en, region_en if not region_en else 'Краснодарский край')
                
                return jsonify({
                    'success': True,
                    'city': city,
                    'region': region,
                    'country': 'Россия',
                    'detected': True,
                    'ip': user_ip
                })
        
        # Fallback to default city
        return jsonify({
            'success': True,
            'city': 'Краснодар',
            'region': 'Краснодарский край',
            'country': 'Россия',
            'detected': False,
            'message': 'Не удалось определить город, используется город по умолчанию'
        })
        
    except Exception as e:
        print(f"Error detecting city: {e}")
        return jsonify({
            'success': True,
            'city': 'Краснодар',
            'region': 'Краснодарский край',
            'country': 'Россия',
            'detected': False,
            'error': str(e)
        })

@app.route('/military-mortgage')
def military_mortgage():
    """Military mortgage page"""
    return render_template('military_mortgage.html')

@app.route('/developer-mortgage')
def developer_mortgage():
    """Developer mortgage page"""
    return render_template('developer_mortgage.html')

@app.route('/maternal-capital')
def maternal_capital():
    """Maternal capital page"""
    return render_template('maternal_capital.html')

@app.route('/tax-deduction')
def tax_deduction():
    """Tax deduction page"""
    return render_template('tax_deduction.html')

@app.route('/residential')
def residential():
    """Residential complexes page"""
    return render_template('residential.html')

@app.route('/residential-complexes')
def residential_complexes():
    """Residential complexes page - NORMALIZED TABLES"""
    from repositories.property_repository import ResidentialComplexRepository, PropertyRepository
    import json
    from datetime import datetime
    
    try:
        # Загружаем все активные ЖК из normalized таблицы
        all_complexes = ResidentialComplexRepository.get_all_active(limit=100)
        
        # Получаем статистику по всем квартирам одним батч-запросом
        property_stats_by_complex = PropertyRepository.get_all_property_stats()
        
        # Текущая дата для определения статуса
        current_year = 2025
        current_quarter = 4
        
        complexes = []
        
        for complex_obj in all_complexes:
            # Получаем статистику по квартирам для этого ЖК
            stats = property_stats_by_complex.get(complex_obj.id, {})
            
            # Форматируем срок сдачи и определяем статус
            completion_date = 'Не указан'
            is_completed = False
            build_year = None
            
            if complex_obj.end_build_year and complex_obj.end_build_quarter:
                build_year = int(complex_obj.end_build_year)
                build_quarter = int(complex_obj.end_build_quarter)
                
                # Определяем сдан ли комплекс
                if build_year < current_year:
                    is_completed = True
                elif build_year == current_year and build_quarter < current_quarter:
                    is_completed = True
                else:
                    is_completed = False
                    
                quarter_names = {1: 'I', 2: 'II', 3: 'III', 4: 'IV'}
                quarter = quarter_names.get(build_quarter, build_quarter)
                completion_date = f"{quarter} кв. {build_year} г."
            elif complex_obj.end_build_year:
                build_year = int(complex_obj.end_build_year)
                is_completed = build_year < current_year
                completion_date = f"{build_year} г."
            
            # Получаем адрес и район из stats (уже загружены батч-запросом), если в ЖК не указаны
            district_name = 'Краснодар'
            full_address = complex_obj.address or 'Адрес не указан'
            
            if (not complex_obj.address or complex_obj.address == 'Адрес не указан') and stats.get('sample_address'):
                # Берем адрес из stats (загружен батч-запросом)
                full_address = stats['sample_address']
                # Извлекаем город/район из адреса (формат: "Россия, Краснодарский край, Сочи, ...")
                address_parts = full_address.split(',')
                if len(address_parts) >= 3:
                    district_name = address_parts[2].strip()  # "Сочи"
            
            # Базовая информация ЖК
            complex_dict = {
                'id': complex_obj.id,
                'name': complex_obj.name,
                'available_apartments': stats.get('total_count', 0),
                'price_from': stats.get('min_price', 0) or 0,
                'price_to': stats.get('max_price', 0) or 0,
                'real_price_from': stats.get('min_price', 0) or 0,
                'real_price_to': stats.get('max_price', 0) or 0,
                'area_from': stats.get('min_area', 0) or 0,
                'area_to': stats.get('max_area', 0) or 0,
                'real_area_from': stats.get('min_area', 0) or 0,
                'real_area_to': stats.get('max_area', 0) or 0,
                'floors_min': 1,  # Default value
                'floors_max': 25,  # Default value
                'district': district_name,
                'developer': complex_obj.developer.name if complex_obj.developer else 'Не указан',
                'address': full_address,
                'full_address': full_address,
                'location': full_address,
                'completion_date': completion_date,
                'buildings_count': stats.get('buildings_count', 1),  # From property stats
                'is_completed': is_completed,
                'status': 'Сдан' if is_completed else 'Строится',
                'object_class': complex_obj.object_class_display_name or 'Комфорт',
                'housing_class': complex_obj.object_class_display_name or 'Комфорт',
                'max_floors': 25,  # Default value
                'floors': 25,  # Default value
                'completion_year': build_year or 2025,
                'cashback_rate': complex_obj.cashback_rate if complex_obj.cashback_rate else 5.0
            }
            
            # Загружаем фотографии из главного фото ЖК или из дорогой квартиры
            try:
                if complex_obj.main_image:
                    complex_dict['image'] = complex_obj.main_image
                    # Если есть gallery_images, используем их для слайдера
                    if complex_obj.gallery_images:
                        try:
                            gallery_list = json.loads(complex_obj.gallery_images) if isinstance(complex_obj.gallery_images, str) else complex_obj.gallery_images
                            complex_dict['images'] = gallery_list
                        except:
                            complex_dict['images'] = [complex_obj.main_image]
                    else:
                        complex_dict['images'] = [complex_obj.main_image]
                else:
                    # Fallback: берем фото из самой дорогой квартиры
                    properties_in_complex = PropertyRepository.get_by_complex_id(complex_obj.id, limit=1, sort_by='price', sort_order='desc')
                    if properties_in_complex and properties_in_complex[0].gallery_images:
                        try:
                            gallery_list = json.loads(properties_in_complex[0].gallery_images) if isinstance(properties_in_complex[0].gallery_images, str) else []
                            if gallery_list and len(gallery_list) > 0:
                                start_index = min(len(gallery_list) // 4, 5) if len(gallery_list) > 8 else 1
                                complex_dict['image'] = gallery_list[start_index] if len(gallery_list) > start_index else gallery_list[0]
                                complex_dict['images'] = gallery_list[start_index:] if len(gallery_list) > start_index else gallery_list
                            else:
                                complex_dict['image'] = properties_in_complex[0].image or 'https://via.placeholder.com/400x300/0088CC/FFFFFF?text=' + complex_obj.name.replace(' ', '+')
                                complex_dict['images'] = []
                        except Exception as e:
                            print(f"Error parsing gallery for complex {complex_obj.name}: {e}")
                            complex_dict['image'] = 'https://via.placeholder.com/400x300/0088CC/FFFFFF?text=' + complex_obj.name.replace(' ', '+')
                            complex_dict['images'] = []
                    else:
                        complex_dict['image'] = 'https://via.placeholder.com/400x300/0088CC/FFFFFF?text=' + complex_obj.name.replace(' ', '+')
                        complex_dict['images'] = []
            except Exception as e:
                print(f"Error loading photos for complex {complex_obj.name}: {e}")
                complex_dict['image'] = 'https://via.placeholder.com/400x300/0088CC/FFFFFF?text=' + complex_obj.name.replace(' ', '+')
                complex_dict['images'] = []
                
            # Статистика по комнатам с детальными данными
            complex_dict['real_room_distribution'] = stats.get('room_distribution', {})  # {'1-комн': 10, '2-комн': 8}
            complex_dict['room_details'] = stats.get('room_details', {})  # {'1-комн': {'count': 10, 'price_from': 10852800, ...}}
            
            complexes.append(complex_dict)
        
        # Сортировка: "IV кв. 2025 г. Строится" в конце
        complexes.sort(key=lambda x: (
            1 if x['completion_year'] == 2025 and x.get('completion_date', '').startswith('IV кв. 2025') else 0,
            x['name']
        ))
        
        # Database complexes loaded
        
        # Get unique districts and developers (with safe extraction)
        districts = sorted(list(set(complex.get('district', 'Не указан') for complex in complexes if complex.get('district'))))
        developers = sorted(list(set(complex.get('developer', 'Не указан') for complex in complexes if complex.get('developer'))))
        
        # Pagination
        page = int(request.args.get('page', 1))
        per_page = 35  # Show all complexes on one page
        total_complexes = len(complexes)
        total_pages = (total_complexes + per_page - 1) // per_page
        offset = (page - 1) * per_page
        complexes_page = complexes[offset:offset + per_page]
        
        # Prepare pagination info
        pagination = {
            'page': page,
            'per_page': per_page,
            'total': total_complexes,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'prev_page': page - 1 if page > 1 else None,
            'next_page': page + 1 if page < total_pages else None
        }
        
        # Проверяем статус менеджера для передачи в JavaScript
        is_manager = 'is_manager' in session and session.get('is_manager', False)
        manager_id = session.get('manager_id')
        
        return render_template('residential_complexes.html',
                             residential_complexes=complexes_page,
                             all_complexes=complexes,  # For JavaScript filtering
                             districts=districts,
                             developers=developers,
                             pagination=pagination,
                             is_manager=is_manager,
                             manager_id=manager_id,
                             yandex_maps_api_key=os.environ.get('YANDEX_MAPS_API_KEY', ''))
                             
    except Exception as e:
        print(f"ERROR in residential_complexes: {e}")
        import traceback
        traceback.print_exc()
        return f"Error loading residential complexes: {str(e)}", 500





@app.route('/map')
def map_view():
    """Enhanced interactive map view page using real Excel data"""
    # Map route processing
    
    try:
        # ✅ MIGRATED: Load properties with coordinates using repository
        properties_data = PropertyRepository.get_properties_with_coordinates()
        
        properties = []
        for prop_row in properties_data:
            # Unpack RowProxy data
            prop_id = prop_row.id
            inner_id = prop_row.inner_id
            title = prop_row.title
            price = prop_row.price or 0
            rooms = prop_row.rooms or 0
            area = prop_row.area or 0
            lat = prop_row.latitude
            lng = prop_row.longitude
            complex_name = prop_row.complex_name or ''
            cashback_rate = prop_row.cashback_rate or 0
            
            # Calculate cashback
            cashback_amount = int(price * (cashback_rate / 100)) if cashback_rate > 0 else 0
            
            # Format title
            room_label = 'Студия' if rooms == 0 else f'{rooms}-комн'
            formatted_title = f"{room_label}, {area} м²" if title else title
            
            # Format data for map
            property_data = {
                'id': inner_id or prop_id,
                'price': price,
                'area': area,
                'rooms': rooms,
                'title': formatted_title,
                'address': '',  # Will be filled from Property model if available
                'residential_complex': complex_name,
                'complex_name': complex_name,
                'developer': '',  # Will be filled from relationships
                'district': 'Краснодарский край',
                'coordinates': {
                    'lat': float(lat),
                    'lng': float(lng)
                },
                'url': f"/object/{inner_id or prop_id}",
                'type': 'property',
                'cashback': cashback_amount,
                'cashback_rate': cashback_rate,
                'cashback_available': cashback_rate > 0,
                'status': 'available',
                'property_type': 'Квартира',
                'main_image': 'https://via.placeholder.com/400x300'
            }
            
            properties.append(property_data)
        
        # ✅ MIGRATED: Load residential complexes with coordinates
        complexes_data = ResidentialComplexRepository.get_with_coordinates()
        
        residential_complexes = []
        for idx, row in enumerate(complexes_data):
            complex_data = {
                'id': row.id,
                'name': row.name or '',
                'developer': row.developer_name or '',
                'address': '',  # Not in the query result
                'district': 'Краснодарский край',
                'apartments_count': 0,  # Will be calculated if needed
                'price_from': 0,  # Will be calculated if needed
                'coordinates': {
                    'lat': float(row.latitude) if row.latitude else 45.0448,
                    'lng': float(row.longitude) if row.longitude else 38.9760
                },
                'url': f'/zk/{row.slug}' if hasattr(row, 'slug') and row.slug else f'/residential-complex/{row.id}',
                'type': 'complex'
            }
            residential_complexes.append(complex_data)
        
        # Фильтры для интерфейса
        all_districts = sorted(list(set(prop.get('district', 'Не указан') for prop in properties)))
        all_developers = sorted(list(set(prop.get('developer', 'Не указан') for prop in properties)))
        all_complexes = sorted(list(set(prop.get('residential_complex', 'Не указан') for prop in properties)))
        
        filters = {
            'rooms': request.args.getlist('rooms'),
            'price_min': request.args.get('price_min', ''),
            'price_max': request.args.get('price_max', ''),
            'district': request.args.get('district', ''),
            'developer': request.args.get('developer', ''),
            'residential_complex': request.args.get('residential_complex', ''),
        }
        
        # Map data loaded
        
        return render_template('map.html', 
                             properties=properties, 
                             residential_complexes=residential_complexes,
                             all_districts=all_districts,
                             all_developers=all_developers,
                             all_complexes=all_complexes,
                             filters=filters)
                             
    except Exception as e:
        print(f"ERROR in map route: {e}")
        import traceback
        traceback.print_exc()
        return f"Error 500: {str(e)}", 500

def extract_main_image_from_photos(photos_raw):
    """Извлекает основное изображение из поля photos, предпочитая внешние виды зданий"""
    if not photos_raw or not photos_raw.strip():
        return '/static/images/no-photo.jpg'
    
    try:
        import json
        # Попробуем парсить как JSON массив
        if photos_raw.startswith('[') and photos_raw.endswith(']'):
            images = json.loads(photos_raw)
            if not images:
                return '/static/images/no-photo.jpg'
            
            # Фильтруем изображения, предпочитая внешние виды
            # Берем последние изображения, так как первые часто планировки
            if len(images) > 5:
                # Берем из середины/конца массива, где обычно фото зданий
                return images[len(images)//2]
            elif len(images) > 2:
                return images[-1]  # Последнее фото
            else:
                return images[0]
        
        # PostgreSQL array format: {url1,url2,url3}
        elif photos_raw.startswith('{') and photos_raw.endswith('}'):
            images_str = photos_raw[1:-1]  # Remove braces
            if images_str:
                images = [img.strip().strip('"') for img in images_str.split(',') if img.strip()]
                return images[0] if images else '/static/images/no-photo.jpg'
            else:
                return '/static/images/no-photo.jpg'
        
        # Одиночная ссылка
        else:
            return photos_raw
            
    except (json.JSONDecodeError, IndexError) as e:
        print(f"Error parsing photos: {e}, raw data: {photos_raw[:100]}")
        return '/static/images/no-photo.jpg'

@app.route('/complexes-map')
def complexes_map():
    """Карта жилых комплексов"""
    try:
        # ✅ MIGRATED: Load residential complexes with coordinates using repository
        complexes_data = ResidentialComplexRepository.get_with_coordinates()
        property_stats = PropertyRepository.get_all_property_stats()
        
        residential_complexes = []
        current_year = 2025
        
        for row in complexes_data:
            complex_id = row.id
            stats = property_stats.get(complex_id, {})
            
            # Skip complexes without properties
            if not stats or stats.get('total_count', 0) == 0:
                continue
            
            # Determine status based on completion year
            end_build_year = None
            end_build_quarter = None
            status = 'Не указан'
            completion_date = ''
            
            # Get from row if available (from ResidentialComplex ORM)
            # Note: The NamedTuple from get_with_coordinates() may not have these fields
            # We'll use fallback values
            
            if end_build_year:
                if end_build_year <= current_year:
                    status = 'Сдан'
                else:
                    status = 'Строится'
                
                if end_build_quarter:
                    completion_date = f"{end_build_quarter} кв. {end_build_year}"
                else:
                    completion_date = f"{end_build_year} год"
            
            complex_data = {
                'id': complex_id,
                'name': row.name or '',
                'developer': row.developer_name or 'Не указан',
                'address': '',  # Not in get_with_coordinates()
                'district': 'Краснодарский край',
                'apartments_count': stats.get('total_count', 0),
                'price_from': int(stats.get('min_price', 0)),
                'coordinates': {
                    'lat': float(row.latitude) if row.latitude else 45.0448,
                    'lng': float(row.longitude) if row.longitude else 38.9760
                },
                'completion_date': completion_date,
                'status': status,
                'cashback_percent': float(row.cashback_rate) if row.cashback_rate else 0,
                'main_image': row.main_image or '/static/images/no-photo.jpg',
                'description': f"Жилой комплекс {row.name or ''}",
                'object_class': '',
                'housing_class': '',
                'max_floors': 0,
                'url': f'/zk/{row.slug}' if row.slug else f'/residential-complex/{complex_id}',
                'type': 'complex'
            }
            residential_complexes.append(complex_data)
        
        # Фильтры для интерфейса
        all_districts = sorted(list(set(complex.get('district', 'Не указан') for complex in residential_complexes)))
        all_developers = sorted(list(set(complex.get('developer', 'Не указан') for complex in residential_complexes)))
        all_statuses = ['Все', 'Сдан', 'Строится']
        
        print(f"DEBUG: Found {len(residential_complexes)} complexes for map")
        if residential_complexes:
            print(f"DEBUG: First complex: {residential_complexes[0]}")
        
        return render_template('complexes_map.html', 
                             residential_complexes=residential_complexes,
                             all_districts=all_districts,
                             all_developers=all_developers,
                             all_statuses=all_statuses)
                             
    except Exception as e:
        print(f"ERROR in complexes-map route: {e}")
        import traceback
        traceback.print_exc()
        return f"Error 500: {str(e)}", 500

# API Routes
@app.route('/api/properties')
@cache.cached(timeout=300, query_string=True)
def api_properties():
    """API endpoint for properties - NORMALIZED TABLES"""
    try:
        import json
        from sqlalchemy.orm import joinedload
        from repositories.property_repository import PropertyRepository
        from models import Property, ResidentialComplex
        
        # Pagination
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        offset = (page - 1) * per_page
        
        # Get properties with coordinates
        properties_list = Property.query.join(
            ResidentialComplex, Property.complex_id == ResidentialComplex.id, isouter=True
        ).filter(
            Property.is_active == True,
            Property.latitude.isnot(None),
            Property.longitude.isnot(None)
        ).options(
            joinedload(Property.residential_complex),
            joinedload(Property.developer)
        ).offset(offset).limit(per_page).all()
        
        # Count total
        total_count = Property.query.filter(
            Property.is_active == True,
            Property.latitude.isnot(None),
            Property.longitude.isnot(None)
        ).count()
        
        # Format response
        properties = []
        for prop in properties_list:
            complex_obj = prop.residential_complex
            developer_obj = prop.developer
            
            property_data = {
                'id': prop.inner_id or prop.id,
                'price': prop.price or 0,
                'area': prop.area or 0,
                'rooms': prop.rooms or 0,
                'title': prop.room_description + f", {prop.area} м²",
                'subtitle': f"{complex_obj.name if complex_obj else ''} • Краснодар",
                'address': prop.address or (complex_obj.address if complex_obj else ''),
                'residential_complex': complex_obj.name if complex_obj else '',
                'developer': developer_obj.name if developer_obj else '',
                'developer_name': developer_obj.name if developer_obj else '',
                'district': 'Краснодар',
                'complex_object_class_display_name': '',
                'renovation_display_name': prop.renovation_type or 'Без отделки',
                'object_min_floor': prop.floor,
                'object_max_floor': prop.total_floors,
                'coordinates': {
                    'lat': float(prop.latitude) if prop.latitude else 45.0448,
                    'lng': float(prop.longitude) if prop.longitude else 38.9760
                },
                'url': f"/object/{prop.inner_id or prop.id}",
                'type': 'property',
                'cashback': int((prop.price or 0) * (complex_obj.cashback_rate / 100 if complex_obj else 0.035)),
                'cashback_available': True,
                'status': 'available',
                'property_type': 'Квартира'
            }
            
            # Main image
            if prop.main_image:
                property_data['main_image'] = prop.main_image
            elif prop.gallery_images:
                try:
                    photos = json.loads(prop.gallery_images)
                    property_data['main_image'] = photos[0] if photos else 'https://via.placeholder.com/400x300'
                except:
                    property_data['main_image'] = 'https://via.placeholder.com/400x300'
            else:
                property_data['main_image'] = 'https://via.placeholder.com/400x300'
            
            properties.append(property_data)
        
        print(f"DEBUG: API returned {len(properties)} properties (page {page}, total {total_count})")
        return jsonify({
            'properties': properties,
            'total': total_count,
            'page': page,
            'per_page': per_page,
            'total_pages': (total_count + per_page - 1) // per_page,
            'success': True
        })
        
    except Exception as e:
        print(f"ERROR in api_properties: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500



@app.route('/api/search-suggestions-OLD-DISABLED')
def api_search_suggestions_old_disabled():
    """❌ СТАРЫЙ API endpoint - ОТКЛЮЧЁН, чтобы не мешал новому"""
    return jsonify([])  # ВСЕГДА ПУСТОЙ


# ===== СТАРЫЙ КОД ПОЛНОСТЬЮ УДАЛЁН =====

@app.route('/api/residential-complexes-map')
def api_residential_complexes_map():
    """API endpoint for residential complexes with enhanced data for map"""
    complexes = load_residential_complexes()
    
    # Enhance complexes data for map
    for i, complex in enumerate(complexes):
        # Add coordinates if missing
        if 'coordinates' not in complex:
            base_lat = 45.0448
            base_lng = 38.9760
            lat_offset = (hash(str(i) + complex.get('name', '')) % 1000) / 8000 - 0.0625
            lng_offset = (hash(str(i) + complex.get('district', '')) % 1000) / 8000 - 0.0625
            complex['coordinates'] = {
                'lat': base_lat + lat_offset,
                'lng': base_lng + lng_offset
            }
        
        # ✅ ИСПРАВЛЕНО: Правильный подсчет корпусов из normalized tables
        if 'buildings_count' not in complex:
            # Получаем реальное количество корпусов из normalized tables
            try:
                result = db.session.execute(text("""
                    SELECT COUNT(DISTINCT p.complex_building_name) as buildings_count
                    FROM properties p
                    JOIN residential_complexes rc ON p.complex_id = rc.id
                    WHERE rc.name = :complex_name 
                      AND p.is_active = true 
                      AND p.complex_building_name IS NOT NULL
                """), {'complex_name': complex.get('name', '')})
                row = result.fetchone()
                complex['buildings_count'] = row[0] if row and row[0] else 1
            except:
                complex['buildings_count'] = 1  # По умолчанию 1 корпус
        if 'apartments_count' not in complex:
            complex['apartments_count'] = 100 + (i % 300)
            
    return jsonify(complexes)

# Removed duplicate route - using api_property_detail instead

def _get_complex_data_normalized(complex_id):
    """
    Helper function to query complex data from normalized tables.
    Handles both residential_complexes.id and complex_id (legacy external ID).
    Returns tuple with result row and success boolean.
    """
    result = db.session.execute(text("""
        SELECT 
            rc.id,
            rc.name,
            MIN(p.price) as min_price,
            MAX(p.price) as max_price,
            COUNT(DISTINCT p.id) as apartments_count,
            COUNT(DISTINCT NULLIF(p.complex_building_name,'')) as buildings_count,
            d.name as developer_name,
            MIN(p.floor) as floors_min,
            MAX(p.total_floors) as floors_max,
            rc.end_build_year as completion_year,
            rc.end_build_quarter as completion_quarter,
            dis.name as district,
            COALESCE(MAX(p.address), rc.address) as address,
            rc.cashback_rate,
            COALESCE(rc.main_image, rc.gallery_images, MIN(p.gallery_images)) as photos,
            rc.object_class_display_name AS complex_object_class_display_name
        FROM properties p
        JOIN residential_complexes rc ON p.complex_id = rc.id
        JOIN developers d ON p.developer_id = d.id
        LEFT JOIN districts dis ON rc.district_id = dis.id
        WHERE (rc.id = :complex_id OR rc.complex_id = :complex_id_str)
            AND p.is_active = true
        GROUP BY rc.id, rc.name, d.name, rc.end_build_year, rc.end_build_quarter, 
                 dis.name, rc.address, rc.cashback_rate, rc.gallery_images, 
                 rc.main_image, rc.object_class_display_name
        LIMIT 1
    """), {'complex_id': complex_id, 'complex_id_str': str(complex_id)}).fetchone()
    
    return result


@app.route('/api/complex/<int:complex_id>')
def api_complex(complex_id):
    """API endpoint for single residential complex - MIGRATED TO NORMALIZED TABLES"""
    print(f"🔍 API /api/complex/{complex_id} called")
    
    try:
        # Query normalized tables (handles both rc.id and rc.complex_id)
        result = _get_complex_data_normalized(complex_id)
        
        if result:
            print(f"✅ Found complex {complex_id} in normalized tables: {result[1]}, apartments: {result[4]}, price: {result[2]}-{result[3]}")
            
            # Build completion date from year and quarter
            completion_date = 'Не указано'
            if result[9] and result[10]:  # year and quarter
                completion_date = f"{result[9]} г., {result[10]} кв."
            elif result[9]:  # only year
                completion_date = f"{result[9]} г."
            
            # Extract first photo from photos array/string
            image_url = '/static/images/no-image.jpg'
            if result[14]:  # photos field
                try:
                    # Try parsing as JSON array
                    photos_data = json.loads(result[14])
                    if photos_data and isinstance(photos_data, list) and len(photos_data) > 0:
                        image_url = photos_data[0]
                except (json.JSONDecodeError, TypeError):
                    # If not JSON, treat as single image URL
                    if isinstance(result[14], str) and result[14].strip():
                        image_url = result[14]
            
            response_data = {
                'id': result[0],
                'name': result[1],
                'min_price': result[2],
                'price_from': result[2],
                'max_price': result[3],
                'price_to': result[3],
                'apartments_count': result[4],
                'properties_count': result[4],
                'buildings_count': result[5],
                'developer': result[6],
                'developer_name': result[6],
                'floors_min': result[7] if result[7] else 'Не указано',
                'floors_max': result[8],
                'completion_date': completion_date,
                'district': result[11] or 'Не указано',
                'address': result[12] or 'Не указано',
                'cashback_rate': result[13] or 0,
                'cashback_percent': result[13] or 0,
                'object_class': result[15] or 'Не указано',
                'status': 'В продаже',
                'image': image_url
            }
            print(f"📤 Returning from normalized tables: {response_data}")
            return jsonify(response_data)
        else:
            print(f"⚠️ Complex {complex_id} not found in normalized tables, trying JSON fallback")
            
    except Exception as e:
        print(f"❌ Error loading complex {complex_id} from normalized tables: {e}")
        import traceback
        traceback.print_exc()
    
    # Fallback to residential_complexes (JSON file) if not found in database
    complexes = load_residential_complexes()
    print(f"📊 load_residential_complexes() returned {len(complexes)} complexes")
    for complex in complexes:
        if complex.get('id') == complex_id:
            print(f"✅ Found complex {complex_id} in residential_complexes: {complex.get('name')}")
            print(f"📤 Returning from residential_complexes: apartments_count={complex.get('apartments_count')}, min_price={complex.get('min_price')}, price_from={complex.get('price_from')}, cashback_rate={complex.get('cashback_rate')}")
            return jsonify(complex)
    
    print(f"❌ Complex {complex_id} not found anywhere, returning 404")
    return jsonify({'error': 'Complex not found'}), 404



@app.route('/api/mini-map/properties')
def api_mini_map_properties():
    """API endpoint for mini-map: return property coordinates - NORMALIZED TABLES"""
    from repositories.property_repository import PropertyRepository
    
    try:
        # Get coordinates from properties table using Repository
        properties = PropertyRepository.get_all_active(limit=500)
        
        coordinates = []
        for prop in properties:
            # Check if property has coordinates (either direct or from complex)
            lat = prop.latitude
            lng = prop.longitude
            
            # If property doesn't have coordinates, try to get from residential complex
            if (not lat or not lng or lat == 0 or lng == 0) and prop.residential_complex:
                lat = prop.residential_complex.latitude
                lng = prop.residential_complex.longitude
            
            # Only add if we have valid coordinates
            if lat and lng and lat != 0 and lng != 0:
                coordinates.append({
                    'lat': float(lat),
                    'lng': float(lng)
                })
        
        print(f"✅ Mini-map: Loaded {len(coordinates)} property coordinates from normalized tables")
        return jsonify({'success': True, 'coordinates': coordinates, 'count': len(coordinates)})
    except Exception as e:
        print(f"❌ Error in mini-map properties: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mini-map/complexes')
def api_mini_map_complexes():
    """API endpoint for mini-map: return complex coordinates - NORMALIZED TABLES"""
    from repositories.property_repository import ResidentialComplexRepository
    
    try:
        # Get coordinates from residential_complexes table using Repository
        complexes = ResidentialComplexRepository.get_all_active(limit=100)
        
        coordinates = []
        for complex_obj in complexes:
            # Only add if complex has valid coordinates
            if complex_obj.latitude and complex_obj.longitude and complex_obj.latitude != 0 and complex_obj.longitude != 0:
                coordinates.append({
                    'name': complex_obj.name,
                    'lat': float(complex_obj.latitude),
                    'lng': float(complex_obj.longitude)
                })
        
        print(f"✅ Mini-map: Loaded {len(coordinates)} complex coordinates from normalized tables")
        return jsonify({'success': True, 'coordinates': coordinates, 'count': len(coordinates)})
    except Exception as e:
        print(f"❌ Error in mini-map complexes: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/property/<property_id>/pdf')
def download_property_pdf(property_id):
    """Generate and download PDF for property"""
    try:
        property_data = get_property_by_id(property_id)
        if not property_data:
            return jsonify({'error': 'Property not found'}), 404
        
        # Create simple HTML for PDF generation
        html_content = f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .header {{ text-align: center; margin-bottom: 30px; }}
                .property-details {{ margin-bottom: 20px; }}
                .detail-row {{ margin-bottom: 10px; }}
                .label {{ font-weight: bold; }}
                .price {{ color: #0088CC; font-size: 24px; font-weight: bold; }}
                .cashback {{ color: #FF5722; font-size: 18px; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>InBack - Информация о квартире</h1>
                <p>Квартира #{property_id}</p>
            </div>
            
            <div class="property-details">
                <div class="detail-row">
                    <span class="label">Тип:</span> {property_data.get('rooms', 'Не указано')}
                </div>
                <div class="detail-row">
                    <span class="label">Площадь:</span> {property_data.get('area', 'Не указана')} м²
                </div>
                <div class="detail-row">
                    <span class="label">Этаж:</span> {property_data.get('floor', 'Не указан')}
                </div>
                <div class="detail-row">
                    <span class="label">Застройщик:</span> {property_data.get('developer', 'Не указан')}
                </div>
                <div class="detail-row">
                    <span class="label">ЖК:</span> {property_data.get('residential_complex', 'Не указан')}
                </div>
                <div class="detail-row">
                    <span class="label">Район:</span> {property_data.get('district', 'Не указан')}
                </div>
                <div class="detail-row">
                    <span class="label">Адрес:</span> {property_data.get('location', 'Не указан')}
                </div>
                <div class="detail-row">
                    <span class="label">Статус:</span> {property_data.get('status', 'Не указан')}
                </div>
                
                <div class="detail-row" style="margin-top: 30px;">
                    <div class="price">Цена: {property_data.get('price', 0):,} ₽</div>
                </div>
                <div class="detail-row">
                    <div class="cashback">Кешбек: до {calculate_cashback(property_data.get('price', 0)):,} ₽ (5%)</div>
                </div>
            </div>
            
            <div style="margin-top: 50px; text-align: center; color: #666;">
                <p>InBack.ru - ваш кешбек за новостройки</p>
                <p>Телефон: +7 (800) 123-12-12</p>
            </div>
        </body>
        </html>
        """
        
        # Return HTML for PDF conversion (browser will handle PDF generation)
        # Create ASCII-safe filename
        ascii_filename = f'property-{property_id}.html'
        
        response = app.response_class(
            response=html_content,
            status=200,
            mimetype='text/html'
        )
        response.headers['Content-Disposition'] = f'attachment; filename="{ascii_filename}"'
        return response
        
    except Exception as e:
        print(f"Error generating PDF for property {property_id}: {e}")
        return jsonify({'error': 'Failed to generate PDF'}), 500

@app.route('/developers')
@cache.cached(timeout=3600)  # Кэш на 1 час
def developers():
    """Developers listing page with real database data"""
    try:
        print("Loading developers from database...")
        
        from models import Developer, ResidentialComplex, Property
        from sqlalchemy import func
        
        # Получаем застройщиков из базы данных с статистикой
        developers_list = (
            db.session.query(Developer, 
                            func.count(ResidentialComplex.id).label('complexes_count'),
                            func.count(Property.id).label('properties_count'))
            .outerjoin(ResidentialComplex, Developer.id == ResidentialComplex.developer_id)
            .outerjoin(Property, Developer.id == Property.developer_id)
            .group_by(Developer.id)
            .order_by(func.count(Property.id).desc())
            .all()
        )
        
        # Формируем список застройщиков с данными
        developers_data = []
        for developer, complexes_count, properties_count in developers_list:
            developer_dict = {
                'id': developer.id,
                'name': developer.name,
                'slug': developer.slug,
                'description': developer.description or f"Застройщик {developer.name}",
                'logo_url': developer.logo_url or f"https://via.placeholder.com/200x100/3B82F6/FFFFFF?text={developer.name.replace(' ', '+')}",
                'website': developer.website,
                'phone': developer.phone,
                'email': developer.email,
                'address': developer.address,
                'complexes_count': complexes_count,
                'properties_count': properties_count,
                'established_year': developer.established_year,
                # Нужные поля для шаблона
                'max_cashback': 10,  # По умолчанию 10%
                'max_cashback_percent': 10,
                # Статистика для отображения
                'stats': {
                    'total_projects': complexes_count,
                    'total_apartments': properties_count,
                    'avg_price': None  # Добавим позже
                }
            }
            
            # ✅ MIGRATED: Get statistics using ORM Property model
            from sqlalchemy import func
            stats_result = db.session.query(
                func.count(Property.id).label('total_properties'),
                func.avg(Property.price).label('avg_price'),
                func.min(Property.price).label('min_price'),
                func.max(Property.price).label('max_price'),
                func.count(func.distinct(Property.complex_id)).label('total_complexes')
            ).filter(
                Property.developer_id == developer.id,
                Property.is_active == True
            ).first()
            
            if stats_result and stats_result.total_properties:
                total_props, avg_price, min_price, max_price, total_complexes = stats_result
                developer_dict['properties_count'] = total_props or properties_count
                developer_dict['complexes_count'] = total_complexes or complexes_count
                developer_dict['stats'] = {
                    'total_projects': total_complexes or complexes_count,
                    'total_apartments': total_props or properties_count,
                    'avg_price': int(avg_price) if avg_price else None,
                    'min_price': int(min_price) if min_price else None,
                    'max_price': int(max_price) if max_price else None
                }
            else:
                # Fallback to basic database stats
                developer_dict['stats'] = {
                    'total_projects': complexes_count,
                    'total_apartments': properties_count,
                    'avg_price': None
                }
            
            developers_data.append(developer_dict)
        
        print(f"Found {len(developers_data)} developers in database")
        
        return render_template('developers.html', developers=developers_data)
        
    except Exception as e:
        print(f"Error loading developers: {e}")
        return render_template('developers.html', developers=[])

@app.route('/developer/<developer_slug>')  
def developer_page(developer_slug):
    """Individual developer page by slug"""
    try:
        # Transliteration mapping for finding Cyrillic slugs from Latin input
        translit_map = str.maketrans('abcdefghijklmnopqrstuvwxyz', 'абцдефгхийклмнопкрствухызгк-неометрия'[:26])
        
        # Create variations of the developer name to search for
        developer_name_from_slug = developer_slug.replace('-', ' ')
        
        # Try to find developer in database using multiple search strategies
        # First try exact match with original slug
        developer = db.session.execute(
            text("""
            SELECT * FROM developers WHERE 
            LOWER(slug) = LOWER(:slug)
            OR LOWER(name) LIKE LOWER(:name_pattern)
            OR LOWER(REPLACE(name, ' ', '-')) = LOWER(:slug)
            LIMIT 1
            """),
            {
                "slug": developer_slug, 
                "name_pattern": f"%{developer_name_from_slug}%"
            }
        ).fetchone()
        
        # If not found, try searching by name matching (case insensitive)
        if not developer:
            # Try all developers and match by similarity
            all_devs = db.session.execute(text("SELECT * FROM developers")).fetchall()
            for dev in all_devs:
                dev_slug = dev.slug.lower() if dev.slug else ''
                dev_name = dev.name.lower().replace(' ', '-').replace('гк', 'gk')
                dev_name = dev_name.replace('неометрия', 'neometriya')
                
                if developer_slug.lower() in [dev_slug, dev_name]:
                    developer = dev
                    break
        
        if not developer:
            print(f"Developer not found in database: {developer_slug}")
            return redirect(url_for('developers'))
        
        # Convert row to dict-like object for template
        developer_dict = dict(developer._mapping)
        
        # ✅ MIGRATED: Get developer's complexes from normalized tables
        from models import ResidentialComplex
        developer_complexes_orm = (
            db.session.query(ResidentialComplex)
            .filter(ResidentialComplex.developer_id == developer.id)
            .all()
        )
        
        # Get property statistics for each complex using PropertyRepository
        developer_complexes_query = []
        for complex_orm in developer_complexes_orm:
            # Get properties for this complex
            complex_properties = PropertyRepository.get_by_complex_id(complex_orm.id, limit=10000)
            
            # Calculate statistics
            if complex_properties:
                min_price = min(p.price for p in complex_properties if p.price)
                max_price = max(p.price for p in complex_properties if p.price)
                avg_price = sum(p.price for p in complex_properties if p.price) / len(complex_properties)
                
                # Get unique buildings count
                buildings = set(p.complex_building_name for p in complex_properties if p.complex_building_name)
                buildings_count = len(buildings)
                
                # Get photos from first property
                main_image = 'https://images.unsplash.com/photo-1545324418-cc1a3fa10c00?w=800'
                images = [main_image]
                if complex_properties[0].gallery_images:
                    try:
                        photos_list = json.loads(complex_properties[0].gallery_images)
                        if photos_list and isinstance(photos_list, list):
                            images = photos_list
                            main_image = photos_list[0]
                    except:
                        pass
                
                # Get address from properties (fallback to complex address)
                property_address = complex_properties[0].address if complex_properties and complex_properties[0].address else None
                
                # Create complex data structure
                complex_data = type('obj', (object,), {
                    'name': complex_orm.name,
                    'id': complex_orm.name,
                    'location': property_address or complex_orm.address or 'Адрес не указан',
                    'apartments_count': len(complex_properties),
                    'buildings_count': buildings_count or 1,
                    'min_price': int(min_price),
                    'max_price': int(max_price),
                    'avg_price': int(avg_price),
                    'lat': complex_properties[0].latitude if complex_properties[0].latitude else None,
                    'lng': complex_properties[0].longitude if complex_properties[0].longitude else None,
                    'sales_address': property_address or complex_orm.address,
                    'images': images,
                    'image': main_image,
                    'completion_date': f"{complex_orm.end_build_quarter} кв. {complex_orm.end_build_year}" if complex_orm.end_build_quarter and complex_orm.end_build_year else 'Сдан',
                    'real_price_from': int(min_price),
                    'room_types_count': len(set(p.rooms for p in complex_properties)),
                    '_mapping': {
                        'name': complex_orm.name,
                        'id': complex_orm.name,
                        'location': property_address or complex_orm.address or 'Адрес не указан',
                        'apartments_count': len(complex_properties),
                        'buildings_count': buildings_count or 1,
                        'min_price': int(min_price),
                        'max_price': int(max_price),
                        'avg_price': int(avg_price),
                        'lat': complex_properties[0].latitude if complex_properties[0].latitude else None,
                        'lng': complex_properties[0].longitude if complex_properties[0].longitude else None,
                        'sales_address': property_address or complex_orm.address,
                        'images': images,
                        'image': main_image,
                        'completion_date': f"{complex_orm.end_build_quarter} кв. {complex_orm.end_build_year}" if complex_orm.end_build_quarter and complex_orm.end_build_year else 'Сдан',
                        'real_price_from': int(min_price),
                        'room_types_count': len(set(p.rooms for p in complex_properties))
                    }
                })()
                developer_complexes_query.append(complex_data)
        
        # Sort by apartments_count descending
        developer_complexes_query = sorted(developer_complexes_query, key=lambda x: x.apartments_count, reverse=True)
        
        developer_complexes = []
        for complex_row in developer_complexes_query:
            complex_dict = dict(complex_row._mapping)
            
            # ✅ MIGRATED: Get room distribution from PropertyRepository
            # Get all properties for this complex from the developer_complexes_query data
            complex_name = complex_dict['name']
            matching_complex = next((c for c in developer_complexes_orm if c.name == complex_name), None)
            
            room_distribution_query = []
            if matching_complex:
                complex_props = PropertyRepository.get_by_complex_id(matching_complex.id, limit=10000)
                
                # Group by rooms
                from collections import defaultdict
                room_groups = defaultdict(list)
                for p in complex_props:
                    room_groups[p.rooms].append(p)
                
                # Create room distribution data
                for rooms, props in sorted(room_groups.items()):
                    room_type = 'Студия' if rooms == 0 else f'{rooms}-комн.'
                    room_data = type('obj', (object,), {
                        'room_type': room_type,
                        'count': len(props),
                        'price_from': min(p.price for p in props if p.price),
                        'price_to': max(p.price for p in props if p.price),
                        'area_from': min(p.area for p in props if p.area),
                        'area_to': max(p.area for p in props if p.area),
                        '_mapping': {
                            'room_type': room_type,
                            'count': len(props),
                            'price_from': min(p.price for p in props if p.price),
                            'price_to': max(p.price for p in props if p.price),
                            'area_from': min(p.area for p in props if p.area),
                            'area_to': max(p.area for p in props if p.area)
                        }
                    })()
                    room_distribution_query.append(room_data)
            
            # Формируем данные о комнатности
            real_room_distribution = {}
            room_details = {}
            
            for room_row in room_distribution_query:
                room_data = dict(room_row._mapping)
                room_type = room_data['room_type']
                real_room_distribution[room_type] = room_data['count']
                room_details[room_type] = {
                    'price_from': room_data['price_from'],
                    'price_to': room_data['price_to'],
                    'area_from': room_data['area_from'],
                    'area_to': room_data['area_to']
                }
            
            complex_dict['real_room_distribution'] = real_room_distribution
            complex_dict['room_details'] = room_details
            developer_complexes.append(complex_dict)
        
        # ✅ MIGRATED: Get developer's properties from normalized tables
        developer_properties_orm = PropertyRepository.get_all_active(
            limit=10000,
            filters={'developer_id': developer.id},
            sort_by='price',
            sort_order='asc'
        )
        
        # Convert to old format for backward compatibility
        developer_properties = []
        for prop in developer_properties_orm:
            complex_obj = prop.residential_complex
            district_obj = prop.district
            
            # Format floor display properly: "X этаж из Y" or "Этаж не указан"
            if prop.floor and prop.total_floors:
                floor_display = f"{prop.floor} этаж из {prop.total_floors}"
            elif prop.total_floors:
                floor_display = f"Этаж не указан из {prop.total_floors}"
            else:
                floor_display = "Этаж не указан"
            
            prop_dict = {
                'inner_id': prop.inner_id,
                'price': prop.price,
                'object_area': prop.area,
                'object_rooms': prop.rooms,
                'object_min_floor': prop.floor,
                'object_max_floor': prop.total_floors,
                'floor_display': floor_display,  # New formatted field
                'complex_name': complex_obj.name if complex_obj else '',
                'developer_name': developer.name,
                'address_display_name': prop.address,
                'parsed_district': district_obj.name if district_obj else '',
                'photos': prop.gallery_images or '[]'
            }
            developer_properties.append(prop_dict)
        
        properties_count = len(developer_properties)
        min_price = min([p['price'] for p in developer_properties]) if developer_properties else 0
        
        # Parse features, infrastructure, and advantages if they exist
        import json as json_lib
        features = []
        infrastructure = []
        advantages = []
        
        if developer_dict.get('features'):
            try:
                features = json_lib.loads(developer_dict['features'])
            except:
                features = []
        
        if developer_dict.get('infrastructure'):
            try:
                infrastructure = json_lib.loads(developer_dict['infrastructure'])
            except:
                infrastructure = []
        
        if developer_dict.get('advantages'):
            try:
                advantages = json_lib.loads(developer_dict['advantages'])
            except:
                advantages = []
        
        # ✅ MIGRATED: Get statistics from normalized tables (properties + residential_complexes)
        from models import Property
        from sqlalchemy import func
        
        developer_stats = db.session.query(
            func.count(Property.id).label('total_properties'),
            func.avg(Property.price).label('avg_price'),
            func.min(Property.price).label('min_price'),
            func.max(Property.price).label('max_price'),
            func.count(func.distinct(Property.complex_id)).label('total_complexes')
        ).filter(
            Property.developer_id == developer.id,
            Property.is_active == True
        ).first()
        
        # Update statistics with real data from properties table
        if developer_stats and developer_stats.total_properties:
            developer_dict['properties_count'] = developer_stats.total_properties
            developer_dict['complexes_count'] = developer_stats.total_complexes
            developer_dict['min_price'] = int(developer_stats.min_price) if developer_stats.min_price else 12000000
            developer_dict['max_price'] = int(developer_stats.max_price) if developer_stats.max_price else 0
            developer_dict['avg_price'] = int(developer_stats.avg_price) if developer_stats.avg_price else 0
            print(f"DEBUG: Normalized stats for {developer.name}: min_price={developer_stats.min_price}, total_props={developer_stats.total_properties}")
        else:
            print(f"DEBUG: No properties found for {developer.name}")
        
        # Добавляем дефолтные значения для полей, которые могут отсутствовать
        developer_dict['total_projects'] = developer_dict.get('completed_projects', 0) or developer_dict.get('complexes_count', 0)
        developer_dict['rating'] = developer_dict.get('rating') or 4.2
        developer_dict['founded_year'] = developer_dict.get('founded_year') or developer_dict.get('established_year') or 2015
        developer_dict['detailed_description'] = developer_dict.get('description') or 'Надёжный застройщик с многолетним опытом строительства качественного жилья в регионе.'
        developer_dict['description'] = developer_dict.get('description') or developer_dict['detailed_description']
        developer_dict['logo'] = developer_dict.get('logo_url')  # Add logo field
        developer_dict['short_name'] = developer_dict.get('name', '')[:2].upper()  # First 2 letters for fallback logo
        
        # Use advantages from DB if exists, otherwise use defaults
        if not advantages:
            advantages = [
                'Собственное строительство без субподряда',
                'Сдача объектов точно в срок', 
                'Качественные материалы и технологии',
                'Полный пакет документов и сервисов'
            ]
        developer_dict['advantages'] = advantages
        
        return render_template('developer_detail.html', 
                             developer=developer_dict,
                             developer_name=developer_dict['name'],
                             complexes=developer_complexes,
                             apartments=developer_properties,
                             total_properties=properties_count,
                             min_price=min_price,
                             features=features,
                             infrastructure=infrastructure)
        
    except Exception as e:
        print(f"Error loading developer page for {developer_slug}: {e}")
        import traceback
        traceback.print_exc()
        return redirect(url_for('developers'))

# Districts routes
@app.route('/districts')
def districts():
    """Districts listing page"""
    # Импорт модели
    from models import District
    import json
    
    # Получаем все районы из базы данных
    districts_query = District.query.order_by(District.name).all()
    
    # Подготавливаем данные с парсингом JSON
    districts_list = []
    for district in districts_query:
        district_data = {
            'id': district.id,
            'name': district.name,
            'slug': district.slug,
            'description': district.description,
            'latitude': district.latitude,
            'longitude': district.longitude,
            'distance_to_center': district.distance_to_center,
            'infrastructure_data': {}
        }
        
        # Парсим JSON данные инфраструктуры
        if district.infrastructure_data:
            try:
                district_data['infrastructure_data'] = json.loads(district.infrastructure_data)
            except:
                district_data['infrastructure_data'] = {}
        
        districts_list.append(district_data)
    
    return render_template('districts.html', 
                         districts=districts_list,
                         yandex_api_key=os.environ.get('YANDEX_MAPS_API_KEY'))

# ========================================
# АДМИНИСТРАТИВНАЯ ПАНЕЛЬ ДЛЯ КООРДИНАТ
# ========================================

@app.route('/admin/coordinates')
def admin_coordinates():
    """Административная панель для редактирования координат районов"""
    from models import District
    
    # Получаем все районы
    districts = District.query.order_by(District.name).all()
    
    return render_template('admin/coordinates.html', 
                         districts=districts,
                         yandex_api_key=os.environ.get('YANDEX_MAPS_API_KEY'))

@app.route('/admin/update-coordinates', methods=['POST'])
def admin_update_coordinates():
    """API для обновления координат района"""
    from models import District
    import math
    
    try:
        district_id = request.form.get('district_id')
        latitude = float(request.form.get('latitude'))
        longitude = float(request.form.get('longitude'))
        
        # Вычисляем расстояние до центра
        theater_lat, theater_lon = 45.035180, 38.977414
        
        def haversine_distance(lat1, lon1, lat2, lon2):
            R = 6371
            dlat = math.radians(lat2 - lat1)
            dlon = math.radians(lon2 - lon1)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
            c = 2 * math.asin(math.sqrt(a))
            return R * c
        
        distance = haversine_distance(latitude, longitude, theater_lat, theater_lon)
        
        # Обновляем координаты
        district = District.query.get(district_id)
        if district:
            district.latitude = latitude
            district.longitude = longitude
            district.distance_to_center = distance
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': f'Координаты района {district.name} обновлены',
                'distance': round(distance, 1)
            })
        else:
            return jsonify({'success': False, 'message': 'Район не найден'})
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# ========================================
# АЛИАСЫ ДЛЯ СТАРЫХ URL РАЙОНОВ
# ========================================

@app.route('/district/tec')
def district_tec_redirect():
    """Редирект со старого URL ТЭЦ на новый"""
    from flask import redirect, url_for
    return redirect(url_for('district_detail', district='tets'), code=301)

@app.route('/district/mkg')
def district_mkg_redirect():
    """Редирект со старого URL МКГ (МХГ) на новый"""
    from flask import redirect, url_for
    return redirect(url_for('district_detail', district='mhg'), code=301)

@app.route('/district/skhi')
def district_skhi_redirect():
    """Редирект для СХИ (Сельскохозяйственный институт)"""
    from flask import redirect, url_for
    return redirect(url_for('district_detail', district='shi'), code=301)

@app.route('/district/<district>')
def district_detail(district):
    """Individual district page"""
    try:
        # Import District model
        from models import District
        
        # Get properties and complexes in this district
        properties = load_properties()
        complexes = load_residential_complexes()
        
        # Filter by district (simplified district matching)
        district_properties = [p for p in properties if district.replace('-', ' ').lower() in p.get('address', '').lower()]
        district_complexes = [c for c in complexes if district.replace('-', ' ').lower() in c.get('district', '').lower()]
        
        # Add cashback calculations
        for prop in district_properties:
            prop['cashback'] = calculate_cashback(
                prop['price'],
                complex_id=prop.get('complex_id'),
                complex_name=prop.get('residential_complex')
            )
        
        # District info mapping - all 54 districts
        district_names = {
            '40-let-pobedy': '40 лет Победы',
            '9i-kilometr': '9-й километр', 
            'aviagorodok': 'Авиагородок',
            'avrora': 'Аврора',
            'basket-hall': 'Баскет-холл',
            'berezovy': 'Березовый',
            'cheremushki': 'Черемушки',
            'dubinka': 'Дубинка',
            'enka': 'Энка',
            'festivalny': 'Фестивальный',
            'gidrostroitelei': 'Гидростроителей',
            'gorkhutor': 'Горхутор',
            'hbk': 'ХБК',
            'kalinino': 'Калинино',
            'karasunsky': 'Карасунский',
            'kolosisty': 'Колосистый',
            'komsomolsky': 'Комсомольский',
            'kozhzavod': 'Кожзавод',
            'krasnaya-ploshchad': 'Красная площадь',
            'krasnodarskiy': 'Краснодарский',
            'kubansky': 'Кубанский',
            'mkg': 'МКГ',
            'molodezhny': 'Молодежный',
            'muzykalny-mkr': 'Музыкальный микрорайон',
            'nemetskaya-derevnya': 'Немецкая деревня',
            'novoznamenskiy': 'Новознаменский',
            'panorama': 'Панорама',
            'pashkovskiy': 'Пашковский',
            'pashkovsky': 'Пашковский-2',
            'pokrovka': 'Покровка',
            'prikubansky': 'Прикубанский',
            'rayon-aeroporta': 'Район аэропорта',
            'repino': 'Репино',
            'rip': 'РИП',
            'severny': 'Северный',
            'shkolny': 'Школьный',
            'slavyansky': 'Славянский',
            'slavyansky2': 'Славянский-2',
            'solnechny': 'Солнечный',
            'tabachnaya-fabrika': 'Табачная фабрика',
            'tec': 'ТЭЦ',
            'tsentralnyy': 'Центральный',
            'uchhoz-kuban': 'Учхоз Кубань',
            'vavilova': 'Вавилова',
            'votochno-kruglikovskii': 'Восточно-Кругликовский',
            'yablonovskiy': 'Яблоновский',
            'zapadny': 'Западный',
            'zapadny-obhod': 'Западный обход',
            'zapadny-okrug': 'Западный округ',
            'zip-zhukova': 'ЗИП Жукова'
        }
        
        # Get district data from database with coordinates
        district_db = District.query.filter_by(slug=district).first()
        
        # Use district name from database if available, otherwise fallback to mapping
        if district_db and district_db.name:
            district_name = district_db.name
        else:
            district_name = district_names.get(district, district.replace('-', ' ').title())
        
        # Prepare district data for template
        infrastructure_data = None
        if district_db and district_db.infrastructure_data:
            try:
                import json
                if isinstance(district_db.infrastructure_data, str):
                    infrastructure_data = json.loads(district_db.infrastructure_data)
                else:
                    infrastructure_data = district_db.infrastructure_data
            except Exception as e:
                print(f"Infrastructure parsing error: {e}")
                infrastructure_data = None
        
        district_data = {
            'name': district_name,
            'slug': district,
            'latitude': district_db.latitude if district_db and district_db.latitude else None,
            'longitude': district_db.longitude if district_db and district_db.longitude else None,
            'zoom_level': district_db.zoom_level if district_db and district_db.zoom_level else 13,
            'description': district_db.description if district_db else None,
            'distance_to_center': getattr(district_db, 'distance_to_center', None) if district_db else None,
            'infrastructure_data': infrastructure_data,
            'geometry': getattr(district_db, 'geometry', None) if district_db else None,
            'geometry_source': getattr(district_db, 'geometry_source', None) if district_db else None
        }
        
        return render_template('district_detail.html', 
                             district=district,
                             district_name=district_name,
                             district_data=district_data,
                             properties=district_properties,
                             complexes=district_complexes,
                             yandex_api_key=os.environ.get('YANDEX_MAPS_API_KEY', ''))
    except Exception as e:
        # Log detailed error for debugging
        import traceback
        print(f"ERROR in district_detail route: {e}")
        print("Full traceback:")
        traceback.print_exc()
        
        # Return error page
        from flask import render_template_string
        error_template = """
        <html>
        <head><title>Ошибка - InBack.ru</title></head>
        <body>
            <h1>Произошла ошибка</h1>
            <p>К сожалению, не удалось загрузить страницу района {{ district }}.</p>
            <p>Ошибка: {{ error }}</p>
            <a href="/">Вернуться на главную</a>
        </body>
        </html>
        """
        return render_template_string(error_template, district=district, error=str(e)), 500


# Content pages routes are already defined above

# API endpoint for infrastructure data
@app.route('/api/infrastructure')
def get_infrastructure():
    """API endpoint to get infrastructure data for coordinates"""
    try:
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius = request.args.get('radius', 2000, type=int)
        
        if not lat or not lng:
            return jsonify({'error': 'Coordinates required'}), 400
        
        # Import infrastructure functions
        from infrastructure_api import get_poi_around_coordinates
        
        # Get POI data
        poi_data = get_poi_around_coordinates(lat, lng, radius)
        
        return jsonify(poi_data)
        
    except Exception as e:
        print(f"Error getting infrastructure data: {e}")
        return jsonify({'error': 'Failed to get infrastructure data'}), 500

# API endpoint for district streets
@app.route('/api/streets/district/<district_slug>')
def get_district_streets(district_slug):
    """API endpoint to get streets for a specific district"""
    try:
        from models import Street, District
        
        # Получаем район по slug
        district = District.query.filter_by(slug=district_slug).first()
        if not district:
            return jsonify({'error': 'District not found'}), 404
        
        # Получаем улицы района с координатами
        streets = Street.query.filter_by(district_id=district.id).filter(
            Street.latitude.isnot(None),
            Street.longitude.isnot(None)
        ).all()
        
        streets_data = []
        for street in streets:
            streets_data.append({
                'id': street.id,
                'name': street.name,
                'slug': street.slug,
                'latitude': float(street.latitude) if street.latitude else None,
                'longitude': float(street.longitude) if street.longitude else None,
                'description': street.description
            })
        
        return jsonify(streets_data)
        
    except Exception as e:
        print(f"Error getting district streets: {e}")
        return jsonify({'error': 'Failed to get district streets'}), 500

# Privacy and legal pages
@app.route('/privacy-policy')
def privacy_policy():
    """Privacy policy page"""
    return render_template('privacy_policy.html')

def parse_user_agent(user_agent):
    """Простой парсинг User-Agent строки"""
    info = {
        'raw': user_agent,
        'browser': 'Неизвестно',
        'version': 'Неизвестно',
        'os': 'Неизвестно',
        'device': 'Неизвестно'
    }
    
    # Определяем браузер
    if 'Chrome' in user_agent and 'Edg' not in user_agent:
        info['browser'] = 'Chrome'
        if 'Chrome/' in user_agent:
            version = user_agent.split('Chrome/')[1].split()[0]
            info['version'] = version
    elif 'Firefox' in user_agent:
        info['browser'] = 'Firefox'
        if 'Firefox/' in user_agent:
            version = user_agent.split('Firefox/')[1].split()[0]
            info['version'] = version
    elif 'Edg' in user_agent:
        info['browser'] = 'Microsoft Edge'
        if 'Edg/' in user_agent:
            version = user_agent.split('Edg/')[1].split()[0]
            info['version'] = version
    elif 'Safari' in user_agent and 'Chrome' not in user_agent:
        info['browser'] = 'Safari'
        if 'Version/' in user_agent:
            version = user_agent.split('Version/')[1].split()[0]
            info['version'] = version
    
    # Определяем ОС
    if 'Windows NT' in user_agent:
        info['os'] = 'Windows'
        if 'Windows NT 10.0' in user_agent:
            info['os'] = 'Windows 10/11'
    elif 'Mac OS X' in user_agent:
        info['os'] = 'macOS'
    elif 'Linux' in user_agent:
        info['os'] = 'Linux'
    elif 'Android' in user_agent:
        info['os'] = 'Android'
    elif 'iPhone' in user_agent:
        info['os'] = 'iOS'
    
    # Определяем тип устройства
    if 'Mobile' in user_agent or 'Android' in user_agent or 'iPhone' in user_agent:
        info['device'] = 'Мобильное устройство'
    elif 'Tablet' in user_agent or 'iPad' in user_agent:
        info['device'] = 'Планшет'
    else:
        info['device'] = 'Десктоп'
    
    return info

@app.route('/technical-info')
def technical_info():
    """Страница технической информации с данными о сессии и устройстве"""
    import platform
    import socket
    import uuid
    import secrets
    from datetime import datetime
    from flask_login import current_user
    
    # Генерируем session_id если его нет
    if 'session_id' not in session:
        session['session_id'] = secrets.token_hex(16)
    
    # Парсим User-Agent для более детальной информации
    user_agent = request.headers.get('User-Agent', '')
    browser_info = parse_user_agent(user_agent)
    
    # Собираем техническую информацию
    tech_info = {
        'server_info': {
            'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'platform': platform.platform(),
            'python_version': platform.python_version(),
            'hostname': socket.gethostname(),
            'flask_version': '2.3.3',  # или получить динамически
            'environment': 'development'
        },
        'session_info': {
            'session_id': session.get('session_id'),
            'user_id': current_user.id if current_user.is_authenticated else 'Не авторизован',
            'username': current_user.full_name if current_user.is_authenticated and hasattr(current_user, 'full_name') else 'Гость',
            'is_authenticated': current_user.is_authenticated,
            'session_permanent': session.permanent
        },
        'request_info': {
            'user_agent': user_agent,
            'ip_address': request.environ.get('HTTP_X_FORWARDED_FOR', request.remote_addr),
            'method': request.method,
            'url': request.url,
            'referrer': request.headers.get('Referer', 'Прямой переход'),
            'accept_language': request.headers.get('Accept-Language', 'Неизвестно'),
            'accept_encoding': request.headers.get('Accept-Encoding', 'Неизвестно'),
            'content_type': request.headers.get('Content-Type', 'Неизвестно'),
            'host': request.headers.get('Host', 'Неизвестно')
        },
        'browser_info': browser_info
    }
    
    return render_template('technical_info.html', tech_info=tech_info)

@app.route('/data-processing-consent')
def data_processing_consent():
    """Data processing consent page"""
    return render_template('data_processing_consent.html')

# Override Flask-Login unauthorized handler for API routes
@login_manager.unauthorized_handler  
def handle_unauthorized():
    # Check if this is an API route
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401
    # Regular redirect for web routes
    return redirect(url_for('login', next=request.url))

# User loader for Flask-Login
@login_manager.user_loader
def load_user(user_id):
    """ИСПРАВЛЕНО: Унифицированная загрузка пользователей через префиксы"""
    from models import User, Manager, Admin
    
    # Check prefix to determine user type
    if user_id.startswith('m_'):
        # Manager (prefix 'm_')
        manager_id = int(user_id[2:])
        manager = Manager.query.get(manager_id)
        if manager:
            return manager
    elif user_id.startswith('a_'):
        # Admin (prefix 'a_')
        admin_id = int(user_id[2:])
        admin = Admin.query.get(admin_id)
        if admin:
            return admin
    else:
        # Regular user (no prefix)
        try:
            user = User.query.get(int(user_id))
            if user:
                return user
        except ValueError:
            pass
    
    return None

def manager_required(f):
    """Decorator to require manager authentication - ИСПРАВЛЕНО: использует Flask-Login"""
    from functools import wraps
    from models import Manager
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        print(f"DEBUG: manager_required - checking Flask-Login authentication")
        print(f"DEBUG: manager_required - request.path: {request.path}")
        print(f"DEBUG: manager_required - request.method: {request.method}")
        print(f"DEBUG: manager_required - current_user.is_authenticated: {current_user.is_authenticated}")
        
        # Check if this is an AJAX or JSON request
        is_ajax = (request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 
                   request.content_type == 'application/json' or
                   request.path.startswith('/api/'))
        
        # Check if manager is authenticated via Flask-Login
        if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
            print(f"DEBUG: manager_required - Manager not authenticated via Flask-Login")
            if is_ajax:
                return jsonify({'success': False, 'error': 'Authentication required'}), 401
            return redirect(url_for('manager_login'))
        
        print(f"DEBUG: manager_required - Success! Manager {current_user.email} authenticated via Flask-Login")
        return f(*args, **kwargs)
    return decorated_function

# Authentication routes
@app.route('/set-demo-password')
def set_demo_password():
    """Временный роут для установки правильного пароля демо пользователю"""
    from models import User
    from werkzeug.security import generate_password_hash
    
    # Найти демо пользователя
    demo_user = User.query.filter_by(email='demo@inback.ru').first()
    if demo_user:
        # Установить простой пароль "demo123"
        demo_user.password_hash = generate_password_hash('demo123')
        db.session.commit()
        return f"Пароль установлен для пользователя {demo_user.email}. Хэш: {demo_user.password_hash[:50]}..."
    else:
        return "Демо пользователь не найден"

@app.route('/set-managers-passwords')
def set_managers_passwords():
    """Временный роут для установки паролей всем менеджерам"""
    from models import Manager
    from werkzeug.security import generate_password_hash
    
    results = []
    
    # Найти всех менеджеров и установить простые пароли
    managers = Manager.query.all()
    for manager in managers:
        if 'anna' in manager.email.lower():
            password = 'anna123'
        elif 'sergey' in manager.email.lower():
            password = 'sergey123'  
        elif 'maria' in manager.email.lower():
            password = 'maria123'
        else:
            password = 'manager123'  # Для остальных менеджеров
            
        manager.password_hash = generate_password_hash(password)
        results.append(f"{manager.email} -> {password}")
    
    db.session.commit()
    return f"Пароли установлены для {len(managers)} менеджеров:<br>" + "<br>".join(results)

@app.route('/login', methods=['GET', 'POST'])
@csrf.exempt
def login():
    """Login page"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        from models import User
        email = request.form.get('email')
        password = request.form.get('password')
        remember = request.form.get('remember') == 'on'
        
        if not email or not password:
            flash('Заполните все поля', 'error')
            return render_template('auth/login.html')
        
        # Check if email or phone
        user = User.query.filter(
            (User.email == email) | (User.phone == email)
        ).first()
        
        if user:
            # Check if user needs to set password
            if user.needs_password_setup():
                session['temp_user_id'] = user.id
                flash('Необходимо установить пароль для входа', 'info')
                return redirect(url_for('setup_password'))
            
            # Check if email is verified
            if not user.is_verified:
                flash('Ваш аккаунт не подтвержден. Проверьте email или запросите новое письмо.', 'warning')
                # Pass user email to template for resend functionality
                return render_template('auth/login.html', unverified_email=user.email)
            
            # Normal password check
            password_valid = user.check_password(password)
            if password_valid:
                # Clear manager session data if exists
                session.pop('manager_id', None)
                session.pop('is_manager', None)
                
                login_user(user, remember=remember)
                user.last_login = datetime.utcnow()
                db.session.commit()
                
                # Redirect to next page or dashboard
                next_page = request.args.get('next')
                return redirect(next_page) if next_page else redirect(url_for('dashboard'))
            else:
                flash('Неверный email или пароль', 'error')
        else:
            flash('Пользователь не найден', 'error')
    
    return render_template('auth/login.html')

@app.route('/setup-password', methods=['GET', 'POST'])
def setup_password():
    """Setup password for users created by managers"""
    temp_user_id = session.get('temp_user_id')
    if not temp_user_id:
        flash('Сессия истекла', 'error')
        return redirect(url_for('login'))
    
    from models import User
    user = User.query.get(temp_user_id)
    if not user or not user.needs_password_setup():
        flash('Пользователь не найден или пароль уже установлен', 'error')
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if not password or not confirm_password:
            flash('Заполните все поля', 'error')
            return render_template('auth/setup_password.html', user=user)
        
        if len(password) < 8:
            flash('Пароль должен содержать минимум 8 символов', 'error')
            return render_template('auth/setup_password.html', user=user)
        
        if password != confirm_password:
            flash('Пароли не совпадают', 'error')
            return render_template('auth/setup_password.html', user=user)
        
        # Set password
        user.set_password(password)
        user.is_verified = True
        db.session.commit()
        
        # Clear temp session and manager data
        session.pop('temp_user_id', None)
        session.pop('manager_id', None)
        session.pop('is_manager', None)
        
        # Login user
        login_user(user)
        user.last_login = datetime.utcnow()
        db.session.commit()
        
        flash('Пароль успешно установлен!', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('auth/setup_password.html', user=user)

@app.route('/register', methods=['POST'])
def register():
    """User registration"""
    from models import User
    
    full_name = request.form.get('full_name')
    email = request.form.get('email')
    phone = request.form.get('phone')
    password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')
    terms = request.form.get('terms')
    
    # Validation
    if not all([full_name, email, password, confirm_password, terms]):
        flash('Заполните все обязательные поля', 'error')
        return redirect(url_for('login'))
    
    if password != confirm_password:
        flash('Пароли не совпадают', 'error')
        return redirect(url_for('login'))
    
    if not password or len(password) < 8:
        flash('Пароль должен содержать минимум 8 символов', 'error')
        return redirect(url_for('login'))
    
    # Check if user exists
    if User.query.filter_by(email=email).first():
        flash('Пользователь с таким email уже существует', 'error')
        return redirect(url_for('login'))
    
    # Create new user
    user = User(
        full_name=full_name,
        email=email,
        phone=phone
    )
    user.set_password(password)
    
    try:
        db.session.add(user)
        db.session.commit()
        
        # Send welcome notification with verification link
        try:
            from email_service import send_verification_email
            send_verification_email(user, base_url=request.url_root.rstrip('/'))
        except Exception as e:
            print(f"Error sending verification email: {e}")
        
        # Clear manager session data if exists
        session.pop('manager_id', None)
        session.pop('is_manager', None)
        
        # DON'T login user immediately - require email verification first
        # login_user(user)  # REMOVED - user must verify email first
        
        flash('Регистрация успешна! Проверьте email и перейдите по ссылке для подтверждения аккаунта.', 'success')
        return redirect(url_for('login'))
        
    except Exception as e:
        db.session.rollback()
        print(f"Registration error: {e}")
        flash(f'Ошибка при регистрации: {str(e)}', 'error')
        return redirect(url_for('login'))

@app.route('/confirm/<token>')
def confirm_email(token):
    """Email confirmation endpoint"""
    from models import User
    
    try:
        # Find user by verification token
        user = User.query.filter_by(verification_token=token).first()
        
        if not user:
            flash('Неверная или просроченная ссылка подтверждения', 'error')
            return redirect(url_for('login'))
        
        if user.is_verified:
            flash('Ваш аккаунт уже подтвержден', 'info')
            return redirect(url_for('login'))
        
        # Confirm user
        user.is_verified = True
        user.verification_token = None  # Clear the token
        db.session.commit()
        
        # Send welcome email after verification
        try:
            from email_service import send_welcome_email
            send_welcome_email(user, base_url=request.url_root.rstrip('/'))
        except Exception as e:
            print(f"Error sending welcome email: {e}")
        
        flash('Email успешно подтвержден! Теперь вы можете войти в аккаунт.', 'success')
        return redirect(url_for('login'))
        
    except Exception as e:
        print(f"Email confirmation error: {e}")
        flash('Ошибка при подтверждении email', 'error')
        return redirect(url_for('login'))

@app.route('/resend-verification', methods=['POST'])
@require_json_csrf
def resend_verification():
    """Resend verification email with rate limiting and enhanced security"""
    import re
    from models import User, EmailVerificationAttempt
    
    # Get request data - support both form and JSON
    if request.content_type == 'application/json':
        data = request.get_json() or {}
        email = data.get('email', '').strip().lower()
    else:
        email = request.form.get('email', '').strip().lower()
    
    # Get client info for logging
    ip_address = request.environ.get('HTTP_X_FORWARDED_FOR', request.environ.get('REMOTE_ADDR'))
    user_agent = request.headers.get('User-Agent', '')[:500]
    
    # Validate email format
    if not email:
        error_msg = 'Введите email для повторной отправки'
        EmailVerificationAttempt.log_attempt(email or 'empty', ip_address, user_agent, False, error_msg)
        if request.content_type == 'application/json':
            return jsonify({'success': False, 'error': error_msg}), 400
        flash(error_msg, 'error')
        return redirect(url_for('login'))
    
    # Basic email format validation
    email_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(email_regex, email):
        error_msg = 'Введите корректный email адрес'
        EmailVerificationAttempt.log_attempt(email, ip_address, user_agent, False, error_msg)
        if request.content_type == 'application/json':
            return jsonify({'success': False, 'error': error_msg}), 400
        flash(error_msg, 'error')
        return redirect(url_for('login'))
    
    # Check rate limiting (5 minutes between successful attempts)
    if not EmailVerificationAttempt.can_resend_verification(email, rate_limit_minutes=5):
        error_msg = 'Слишком частые запросы. Подождите 5 минут перед повторной отправкой.'
        EmailVerificationAttempt.log_attempt(email, ip_address, user_agent, False, error_msg)
        if request.content_type == 'application/json':
            return jsonify({'success': False, 'error': error_msg}), 429
        flash(error_msg, 'warning')
        return redirect(url_for('login'))
    
    # Check for suspicious activity (more than 10 attempts in 1 hour)
    recent_attempts = EmailVerificationAttempt.get_recent_attempts_count(email, hours=1)
    if recent_attempts >= 10:
        error_msg = 'Превышен лимит попыток. Попробуйте позже или обратитесь в поддержку.'
        EmailVerificationAttempt.log_attempt(email, ip_address, user_agent, False, error_msg)
        if request.content_type == 'application/json':
            return jsonify({'success': False, 'error': error_msg}), 429
        flash(error_msg, 'error')
        return redirect(url_for('login'))
    
    # Find user
    user = User.query.filter_by(email=email).first()
    
    if not user:
        # Don't reveal whether user exists for security
        success_msg = 'Если аккаунт с таким email существует, письмо с подтверждением будет отправлено.'
        EmailVerificationAttempt.log_attempt(email, ip_address, user_agent, False, 'User not found')
        if request.content_type == 'application/json':
            return jsonify({'success': True, 'message': success_msg})
        flash(success_msg, 'info')
        return redirect(url_for('login'))
    
    if user.is_verified:
        error_msg = 'Ваш аккаунт уже подтвержден'
        EmailVerificationAttempt.log_attempt(email, ip_address, user_agent, False, error_msg)
        if request.content_type == 'application/json':
            return jsonify({'success': False, 'error': error_msg}), 400
        flash(error_msg, 'info')
        return redirect(url_for('login'))
    
    # Generate new verification token
    user.verification_token = secrets.token_urlsafe(32)
    db.session.commit()
    
    # Send new verification email
    try:
        from email_service import send_verification_email
        send_verification_email(user, base_url=request.url_root.rstrip('/'))
        
        # Log successful attempt
        EmailVerificationAttempt.log_attempt(email, ip_address, user_agent, True, None)
        
        success_msg = 'Письмо с подтверждением отправлено повторно. Проверьте ваш email.'
        print(f"✅ VERIFICATION RESEND SUCCESS: Email sent to {email} from IP {ip_address}")
        
        if request.content_type == 'application/json':
            return jsonify({'success': True, 'message': success_msg})
        flash(success_msg, 'success')
        
    except Exception as e:
        error_msg = 'Ошибка при отправке письма. Попробуйте позже.'
        EmailVerificationAttempt.log_attempt(email, ip_address, user_agent, False, str(e)[:200])
        print(f"❌ VERIFICATION RESEND ERROR: {e} for email {email}")
        
        if request.content_type == 'application/json':
            return jsonify({'success': False, 'error': error_msg}), 500
        flash(error_msg, 'error')
    
    return redirect(url_for('login'))

@app.route('/quiz-registration')
def quiz_registration():
    """Show quiz registration page"""
    return render_template('quiz_registration.html')

@app.route('/callback-request')
def callback_request_page():
    """Show callback request page"""
    return render_template('callback_request.html')

@app.route('/api/property-selection', methods=['POST'])
def property_selection():
    """Property selection application"""
    from models import Application, User
    data = request.get_json()
    
    try:
        # Extract data
        email = data.get('email', '').strip().lower()
        name = data.get('name', '').strip()
        phone = data.get('phone', '').strip()
        
        # Application preferences
        preferred_district = data.get('preferred_district', '')
        property_type = data.get('property_type', '')
        room_count = data.get('room_count', '')
        budget_range = data.get('budget_range', '')
        
        # Property context information
        property_id = data.get('property_id')
        property_title = data.get('property_title', '')
        property_complex = data.get('property_complex', '')
        property_price = data.get('property_price')
        property_area = data.get('property_area')
        property_rooms = data.get('property_rooms')
        property_floor = data.get('property_floor')
        property_total_floors = data.get('property_total_floors')
        property_district = data.get('property_district', '')
        property_url = data.get('property_url', '')
        property_type_context = data.get('property_type_context', '')
        
        # Validation
        if not email or not name or not phone:
            return jsonify({'success': False, 'error': 'Все обязательные поля должны быть заполнены'})
        
        # Determine application type and build message
        is_specific_property = property_id and property_type_context == 'property'
        is_specific_complex = property_id and property_type_context == 'complex'
        
        if is_specific_property:
            # Specific property interest
            application_title = f"Интерес к квартире: {property_title}"
            complex_name = property_complex or 'Не указан'
            message = f"Заявка по конкретной квартире:\n"
            message += f"Имя: {name}\n"
            message += f"Email: {email}\n"
            message += f"Телефон: {phone}\n\n"
            message += f"=== ОБЪЕКТ ИНТЕРЕСА ===\n"
            message += f"Квартира: {property_title}\n"
            message += f"ЖК: {property_complex}\n"
            if property_price:
                try:
                    formatted_price = f"{int(property_price):,}".replace(',', ' ')
                    message += f"Цена: {formatted_price} ₽\n"
                except (ValueError, TypeError):
                    message += f"Цена: {property_price} ₽\n"
            if property_area:
                message += f"Площадь: {property_area} м²\n"
            if property_floor and property_total_floors:
                message += f"Этаж: {property_floor}/{property_total_floors}\n"
            if property_district:
                message += f"Район: {property_district}\n"
            if property_url:
                message += f"Ссылка: {property_url}\n"
            message += f"\n=== ДОПОЛНИТЕЛЬНЫЕ ПРЕДПОЧТЕНИЯ ===\n"
            message += f"Предпочитаемый район: {preferred_district or 'Не указан'}\n"
            message += f"Тип недвижимости: {property_type or 'Не указан'}\n"
            message += f"Комнат: {room_count or 'Не указано'}\n"
            message += f"Бюджет: {budget_range or 'Не указан'}"
        elif is_specific_complex:
            # Specific complex interest
            application_title = f"Интерес к ЖК: {property_title}"
            complex_name = property_title
            message = f"Заявка по жилому комплексу:\n"
            message += f"Имя: {name}\n"
            message += f"Email: {email}\n"
            message += f"Телефон: {phone}\n\n"
            message += f"=== ОБЪЕКТ ИНТЕРЕСА ===\n"
            message += f"ЖК: {property_title}\n"
            if property_district:
                message += f"Район: {property_district}\n"
            if property_url:
                message += f"Ссылка: {property_url}\n"
            message += f"\n=== ДОПОЛНИТЕЛЬНЫЕ ПРЕДПОЧТЕНИЯ ===\n"
            message += f"Предпочитаемый район: {preferred_district or 'Не указан'}\n"
            message += f"Тип недвижимости: {property_type or 'Не указан'}\n"
            message += f"Комнат: {room_count or 'Не указано'}\n"
            message += f"Бюджет: {budget_range or 'Не указан'}"
        else:
            # General property selection
            application_title = "Подбор квартиры"
            complex_name = "По предпочтениям"
            message = f"Заявка на подбор квартиры:\n"
            message += f"Имя: {name}\n"
            message += f"Email: {email}\n"
            message += f"Телефон: {phone}\n"
            message += f"Район: {preferred_district or 'Любой'}\n"
            message += f"Тип: {property_type or 'Не указан'}\n"
            message += f"Комнат: {room_count or 'Не указано'}\n"
            message += f"Бюджет: {budget_range or 'Не указан'}"
        
        # Create application
        application = Application(
            user_id=None,  # No user account needed for applications
            property_id=property_id,  # Store specific property ID if available
            property_name=application_title,
            complex_name=complex_name,
            message=message,
            status='new',
            contact_name=name,
            contact_email=email,
            contact_phone=phone
        )
        
        db.session.add(application)
        
        # Application submitted successfully
        db.session.commit()
        
        # Send Telegram notification
        try:
            from telegram_bot import send_telegram_message
            from datetime import datetime
            
            # Calculate potential cashback (2% of average budget)
            potential_cashback = ""
            if budget_range:
                if "млн" in budget_range:
                    # Extract average from range like "3-5 млн"
                    numbers = [float(x) for x in budget_range.replace(" млн", "").split("-") if x.strip().replace(".", "").replace(",", "").isdigit()]
                    if numbers:
                        avg_price = sum(numbers) / len(numbers) * 1000000
                        cashback = int(avg_price * 0.02)
                        formatted_cashback = f"{cashback:,}".replace(',', ' ')
                        potential_cashback = f"💰 *Потенциальный кэшбек:* {formatted_cashback} руб. (2%)\n"
            
            # Build telegram message based on application type
            if is_specific_property:
                telegram_message = f"""🏠 *ЗАЯВКА ПО КОНКРЕТНОЙ КВАРТИРЕ*

👤 *КОНТАКТНАЯ ИНФОРМАЦИЯ:*
• Имя: {name}
• Телефон: {phone}
• Email: {email}

🏡 *ОБЪЕКТ ИНТЕРЕСА:*
• Квартира: {property_title}
• ЖК: {property_complex}
{f"• Цена: {int(property_price):,} ₽".replace(',', ' ') if property_price else ''}
{f"• Площадь: {property_area} м²" if property_area else ''}
{f"• Этаж: {property_floor}/{property_total_floors}" if property_floor and property_total_floors else ''}
{f"• Ссылка: {property_url}" if property_url else ''}

🔍 *ДОПОЛНИТЕЛЬНЫЕ ПРЕДПОЧТЕНИЯ:*
• Район: {preferred_district or 'Не указан'}
• Тип недвижимости: {property_type or 'Не указан'}
• Количество комнат: {room_count or 'Не указано'}
• Бюджет: {budget_range or 'Не указан'}

{potential_cashback}📅 *ВРЕМЯ ЗАЯВКИ:* {datetime.now().strftime('%d.%m.%Y в %H:%M')}
🌐 *ИСТОЧНИК:* Страница квартиры на InBack.ru

📋 *СЛЕДУЮЩИЕ ШАГИ:*
1️⃣ Связаться с клиентом в течение 15 минут
2️⃣ Обсудить интересующую квартиру
3️⃣ Рассчитать кэшбек и условия покупки
4️⃣ Назначить встречу для просмотра

⚡ *ВАЖНО:* Клиент уже выбрал конкретную квартиру!"""
            elif is_specific_complex:
                telegram_message = f"""🏢 *ЗАЯВКА ПО ЖИЛОМУ КОМПЛЕКСУ*

👤 *КОНТАКТНАЯ ИНФОРМАЦИЯ:*
• Имя: {name}
• Телефон: {phone}
• Email: {email}

🏗️ *ОБЪЕКТ ИНТЕРЕСА:*
• ЖК: {property_title}
{f"• Район: {property_district}" if property_district else ''}
{f"• Ссылка: {property_url}" if property_url else ''}

🔍 *ДОПОЛНИТЕЛЬНЫЕ ПРЕДПОЧТЕНИЯ:*
• Район: {preferred_district or 'Не указан'}
• Тип недвижимости: {property_type or 'Не указан'}
• Количество комнат: {room_count or 'Не указано'}
• Бюджет: {budget_range or 'Не указан'}

{potential_cashback}📅 *ВРЕМЯ ЗАЯВКИ:* {datetime.now().strftime('%d.%m.%Y в %H:%M')}
🌐 *ИСТОЧНИК:* Страница ЖК на InBack.ru

📋 *СЛЕДУЮЩИЕ ШАГИ:*
1️⃣ Связаться с клиентом в течение 15 минут
2️⃣ Показать доступные квартиры в ЖК
3️⃣ Рассчитать кэшбек и условия покупки
4️⃣ Назначить встречу для просмотра

⚡ *ВАЖНО:* Клиент интересуется конкретным ЖК!"""
            else:
                telegram_message = f"""🏠 *НОВАЯ ЗАЯВКА НА ПОДБОР КВАРТИРЫ*

👤 *КОНТАКТНАЯ ИНФОРМАЦИЯ:*
• Имя: {name}
• Телефон: {phone}
• Email: {email}

🔍 *КРИТЕРИИ ПОИСКА:*
• Район: {preferred_district or 'Любой'}
• Тип недвижимости: {property_type or 'Не указан'}
• Количество комнат: {room_count or 'Не указано'}
• Бюджет: {budget_range or 'Не указан'}

{potential_cashback}📅 *ВРЕМЯ ЗАЯВКИ:* {datetime.now().strftime('%d.%m.%Y в %H:%M')}
🌐 *ИСТОЧНИК:* Форма на сайте InBack.ru

📋 *СЛЕДУЮЩИЕ ШАГИ:*
1️⃣ Связаться с клиентом в течение 15 минут
2️⃣ Уточнить дополнительные предпочтения
3️⃣ Подготовить подборку объектов
4️⃣ Назначить встречу для просмотра

⚡ *ВАЖНО:* Быстрая реакция повышает конверсию!"""
            
            send_telegram_message('730764738', telegram_message)
            
        except Exception as notify_error:
            print(f"Notification error: {notify_error}")
        
        return jsonify({
            'success': True,
            'message': 'Заявка отправлена! Менеджер свяжется с вами.'
        })
    except Exception as e:
        db.session.rollback()
        print(f"Application error: {e}")
        return jsonify({'success': False, 'error': 'Ошибка при отправке заявки'})

@app.route('/api/callback-request', methods=['POST'])
def api_callback_request():
    """Submit callback request"""
    from models import CallbackRequest, Manager
    data = request.get_json()
    
    try:
        # Extract data
        name = data.get('name', '').strip()
        phone = data.get('phone', '').strip()
        email = data.get('email', '').strip()
        preferred_time = data.get('preferred_time', '')
        notes = data.get('notes', '').strip()
        
        # Quiz responses
        district = data.get('district', '').strip()
        interest = data.get('interest', '')
        budget = data.get('budget', '')
        timing = data.get('timing', '')
        
        # Validation
        if not name or not phone:
            return jsonify({'success': False, 'error': 'Имя и телефон обязательны для заполнения'})
        
        # ИСПРАВЛЕНО: Убираем строгую проверку района, делаем optional
        if not district:
            district = 'Не указан'
        
        # Create callback request
        callback_req = CallbackRequest(
            name=name,
            phone=phone,
            email=email or None,
            preferred_time=preferred_time,
            notes=notes,
            interest=interest,
            budget=budget,
            timing=timing
        )
        
        # Auto-assign to first available manager
        available_manager = Manager.query.filter_by(is_active=True).first()
        if available_manager:
            callback_req.assigned_manager_id = available_manager.id
        
        db.session.add(callback_req)
        db.session.commit()
        
        # Send notifications
        try:
            send_callback_notification_email(callback_req, available_manager)
            send_callback_notification_telegram(callback_req, available_manager)
        except Exception as e:
            print(f"Failed to send callback notifications: {e}")
        
        return jsonify({
            'success': True,
            'message': 'Заявка отправлена! Наш менеджер свяжется с вами в ближайшее время.'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Callback request error: {e}")
        return jsonify({'success': False, 'error': 'Ошибка при отправке заявки. Попробуйте еще раз.'})

@app.route('/api/booking', methods=['POST'])
def api_booking_request():
    """✅ MIGRATED TO NORMALIZED TABLES: Submit booking request for property"""
    from models import BookingRequest, Manager
    
    try:
        data = request.get_json()
        
        # Validate required fields
        property_id = data.get('property_id')
        client_name = data.get('client_name')
        client_phone = data.get('client_phone')
        presentation_id = data.get('presentation_id')
        
        if not all([property_id, client_name, client_phone]):
            return jsonify({'success': False, 'error': 'Не все обязательные поля заполнены'}), 400
        
        # ✅ MIGRATED: Find property details using PropertyRepository
        property_detail = PropertyRepository.get_by_inner_id(property_id)
        if not property_detail:
            return jsonify({'success': False, 'error': 'Объект не найден'}), 404
        
        # Create booking request
        booking = BookingRequest()
        booking.property_id = property_id
        booking.client_name = client_name
        booking.client_phone = client_phone
        booking.client_email = data.get('client_email')
        booking.comment = data.get('comment')
        booking.presentation_id = presentation_id
        booking.property_price = property_detail.price
        booking.property_address = property_detail.address
        booking.complex_name = property_detail.residential_complex.name if property_detail.residential_complex else 'Не указан'
        booking.rooms_count = property_detail.rooms
        booking.area = property_detail.area
        booking.status = 'new'
        
        db.session.add(booking)
        db.session.commit()
        
        # Notify managers
        try:
            send_booking_notifications(booking, property_detail)
        except Exception as notification_error:
            print(f"Notification error: {notification_error}")
            # Don't fail the booking if notifications fail
        
        return jsonify({
            'success': True, 
            'message': 'Заявка на бронирование успешно отправлена!',
            'booking_id': booking.id
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Booking request error: {e}")
        return jsonify({'success': False, 'error': 'Ошибка при отправке заявки. Попробуйте еще раз.'}), 500

def send_booking_notifications(booking, property_detail):
    """Send notifications to managers about new booking request"""
    from models import Manager
    
    # Get all active managers
    managers = Manager.query.filter_by(is_active=True).all()
    
    # Email notification (if configured)
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        subject = f"🏠 Новая заявка на бронирование - {property_detail.complex_name}"
        
        # Prepare property details
        rooms_text = "Студия" if property_detail.object_rooms == 0 else f"{property_detail.object_rooms}-комнатная"
        
        body = f"""
🏠 Новая заявка на бронирование квартиры

📋 ИНФОРМАЦИЯ О КЛИЕНТЕ:
👤 Имя: {booking.client_name}
📱 Телефон: {booking.client_phone}
📧 Email: {booking.client_email or 'не указан'}
💬 Комментарий: {booking.comment or 'нет'}

🏢 ИНФОРМАЦИЯ О КВАРТИРЕ:
🏠 ЖК: {property_detail.complex_name}
🏠 Тип: {rooms_text} квартира
📐 Площадь: {property_detail.area} м²
🏢 Этаж: {property_detail.floor}/{property_detail.total_floors}
💰 Цена: {'{:,}'.format(int(property_detail.price)).replace(',', ' ')} ₽
📍 Адрес: {property_detail.address_display_name}
🏗️ Застройщик: {property_detail.developer_name}
🔗 ID объекта: {booking.property_id}

📅 Дата заявки: {booking.created_at.strftime('%d.%m.%Y %H:%M')}
🆔 ID заявки: {booking.id}

⏰ Рекомендуем связаться с клиентом в течение 15 минут!
        """.strip()
        
        for manager in managers:
            if manager.email:
                try:
                    send_email_notification(manager.email, subject, body)
                except Exception as email_error:
                    print(f"Failed to send email to {manager.email}: {email_error}")
                    
    except Exception as e:
        print(f"Email notification error: {e}")
    
    # Telegram notification (if bot configured)
    try:
        send_telegram_booking_notification(booking, property_detail, managers)
    except Exception as e:
        print(f"Telegram notification error: {e}")

def send_email_notification(email, subject, body):
    """Send email notification"""
    import smtplib
    from email.mime.text import MIMEText
    
    # Simple email sending (would need proper SMTP configuration in production)
    print(f"📧 Would send email to {email}: {subject}")
    print(f"Body: {body[:100]}...")

def send_telegram_booking_notification(booking, property_detail, managers):
    """Send Telegram notification to managers"""
    try:
        import requests
        
        bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        if not bot_token:
            print("Telegram bot token not configured")
            return
        
        rooms_text = "Студия" if property_detail.object_rooms == 0 else f"{property_detail.object_rooms}-комнатная"
        
        message = f"""
🏠 <b>НОВАЯ ЗАЯВКА НА БРОНИРОВАНИЕ</b>

👤 <b>Клиент:</b> {booking.client_name}
📱 <b>Телефон:</b> {booking.client_phone}
📧 <b>Email:</b> {booking.client_email or 'не указан'}

🏢 <b>Квартира:</b> {rooms_text}, {property_detail.area} м²
🏠 <b>ЖК:</b> {property_detail.complex_name}
💰 <b>Цена:</b> {'{:,}'.format(int(property_detail.price)).replace(',', ' ')} ₽
📍 <b>Адрес:</b> {property_detail.address_display_name}

💬 <b>Комментарий:</b> {booking.comment or 'нет'}
🆔 <b>ID заявки:</b> {booking.id}

⏰ <b>Рекомендуем связаться в течение 15 минут!</b>
        """.strip()
        
        # Send to managers with telegram_chat_id
        for manager in managers:
            if hasattr(manager, 'telegram_chat_id') and manager.telegram_chat_id:
                try:
                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    payload = {
                        'chat_id': manager.telegram_chat_id,
                        'text': message,
                        'parse_mode': 'HTML'
                    }
                    requests.post(url, data=payload, timeout=5)
                    print(f"📱 Telegram notification sent to manager {manager.name}")
                except Exception as telegram_error:
                    print(f"Failed to send Telegram to manager {manager.name}: {telegram_error}")
                    
    except Exception as e:
        print(f"Telegram notification setup error: {e}")

@app.route('/forgot-password', methods=['POST'])
def forgot_password():
    """Password reset request"""
    email = request.form.get('email')
    
    if not email:
        flash('Введите email адрес', 'error')
        return redirect(url_for('login'))
    
    from models import User
    user = User.query.filter_by(email=email).first()
    
    if user:
        # Generate reset token and send email
        token = user.generate_verification_token()
        db.session.commit()
        
        try:
            from email_service import send_password_reset_email
            send_password_reset_email(user, token)
        except Exception as e:
            print(f"Error sending password reset email: {e}")
        
        flash('Инструкции по восстановлению пароля отправлены на ваш email', 'success')
    else:
        # Don't reveal that user doesn't exist
        flash('Инструкции по восстановлению пароля отправлены на ваш email', 'success')
    
    return redirect(url_for('login'))

# API endpoints for dashboard functionality
@app.route('/api/cashback-application', methods=['POST'])
@login_required
def create_cashback_application():
    """Create new cashback application"""
    from models import CashbackApplication
    data = request.get_json()
    
    try:
        app = CashbackApplication(
            user_id=current_user.id,
            property_name=data['property_name'],
            property_type=data['property_type'],
            property_size=float(data['property_size']),
            property_price=int(data['property_price']),
            complex_name=data['complex_name'],
            developer_name=data['developer_name'],
            cashback_amount=int(data['cashback_amount']),
            cashback_percent=float(data['cashback_percent'])
        )
        db.session.add(app)
        db.session.commit()
        
        return jsonify({'success': True, 'application_id': app.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/contact-manager', methods=['POST'])
@csrf.exempt  # CSRF disabled - отключено для простоты отправки заявок
def contact_manager():
    """API endpoint for contacting manager"""
    try:
        from models import Application
        
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        # Validate required fields
        required_fields = ['name', 'phone']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'error': f'Field {field} is required'}), 400
        
        # Get current user if logged in
        user_id = session.get('user_id')
        
        # Create application with required fields
        application = Application(
            user_id=user_id,
            contact_name=data.get('name'),
            contact_email=data.get('email'),
            contact_phone=data.get('phone'),
            property_name=data.get('property_name', 'Заявка на подбор жилья'),
            complex_name=data.get('complex_name', 'По предпочтениям клиента'),
            status='new',
            message=data.get('message', f"Район: {data.get('district', '')}, Комнат: {data.get('rooms', '')}, Заселение: {data.get('completion', '')}, Оплата: {data.get('payment', '')}"),
            preferred_contact=data.get('preferred_contact', 'phone')
        )
        
        db.session.add(application)
        db.session.commit()
        
        # Send notification to manager (email and Telegram)
        try:
            from email_service import send_manager_notification
            send_manager_notification(
                name=data.get('name'),
                phone=data.get('phone'),
                email=data.get('email'),
                message=data.get('message', ''),
                application_id=application.id
            )
        except Exception as e:
            print(f"Failed to send manager notification email: {e}")
            
        # Send Telegram notification
        try:
            from telegram_bot import send_telegram_message
            from datetime import datetime
            
            # Check if this is for a specific property
            is_specific_property = data.get('property_id') and data.get('property_name')
            
            # Prepare Telegram message with quiz data or property info
            if is_specific_property:
                message_parts = [
                    "🏠 *ЗАЯВКА НА ПРОСМОТР КОНКРЕТНОЙ КВАРТИРЫ*",
                    "",
                    "👤 *КОНТАКТНАЯ ИНФОРМАЦИЯ:*",
                    f"• Имя: {data.get('name')}",
                    f"• Телефон: {data.get('phone')}",
                ]
                
                if data.get('email'):
                    message_parts.append(f"• Email: {data.get('email')}")
                    
                message_parts.extend([
                    "",
                    "🏢 *ИНТЕРЕСУЮЩАЯ КВАРТИРА:*",
                    f"• Объект: {data.get('property_name')}",
                ])
                
                if data.get('complex_name'):
                    message_parts.append(f"• ЖК: {data.get('complex_name')}")
                if data.get('property_price'):
                    price_formatted = f"{int(float(data.get('property_price'))):,}".replace(',', ' ')
                    message_parts.append(f"• Цена: {price_formatted} руб.")
                if data.get('property_area'):
                    message_parts.append(f"• Площадь: {data.get('property_area')} м²")
                if data.get('property_floor'):
                    message_parts.append(f"• Этаж: {data.get('property_floor')}")
                if data.get('property_district'):
                    message_parts.append(f"• Район: {data.get('property_district')}")
                if data.get('property_address'):
                    message_parts.append(f"• Адрес: {data.get('property_address')}")
                    
                # Calculate potential cashback
                if data.get('property_price'):
                    try:
                        price = float(data.get('property_price'))
                        cashback = price * 0.03  # 3% cashback
                        cashback_formatted = f"{int(cashback):,}".replace(',', ' ')
                        message_parts.append(f"💰 Потенциальный кэшбек: {cashback_formatted} руб. (3%)")
                    except:
                        pass
                        
                # Add property URL if available
                if data.get('property_url'):
                    message_parts.extend([
                        "",
                        f"🔗 *ССЫЛКА НА КВАРТИРУ:*",
                        f"{data.get('property_url')}"
                    ])
            else:
                message_parts = [
                    "🏠 *НОВАЯ ЗАЯВКА НА ПОДБОР ЖИЛЬЯ*",
                    "",
                    "👤 *КОНТАКТНАЯ ИНФОРМАЦИЯ:*",
                    f"• Имя: {data.get('name')}",
                    f"• Телефон: {data.get('phone')}",
                ]
                
                if data.get('email'):
                    message_parts.append(f"• Email: {data.get('email')}")
                    
                # Add quiz preferences if available
                if data.get('district'):
                    message_parts.extend([
                        "",
                        "🏘️ *ПРЕДПОЧТЕНИЯ КЛИЕНТА:*",
                        f"• Район: {data.get('district')}"
                    ])
                    
                if data.get('rooms'):
                    message_parts.append(f"• Комнат: {data.get('rooms')}")
                    
                if data.get('completion'):
                    message_parts.append(f"• Срок заселения: {data.get('completion')}")
                    
                if data.get('payment'):
                    message_parts.append(f"• Способ оплаты: {data.get('payment')}")
                
            message_parts.extend([
                "",
                f"📝 *ID заявки:* #{application.id}",
                f"📅 *Время:* {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                "",
                "⚡ *ВАЖНО:* Быстрая реакция повышает конверсию!"
            ])
            
            telegram_message = "\n".join(message_parts)
            
            # Send to all manager telegram IDs from environment variable
            manager_telegram_ids = os.environ.get('MANAGER_TELEGRAM_IDS', '').split(',')
            for manager_id in manager_telegram_ids:
                manager_id = manager_id.strip()
                if manager_id:
                    send_telegram_message(manager_id, telegram_message)
            
        except Exception as notify_error:
            print(f"Telegram notification error: {notify_error}")
        
        return jsonify({
            'success': True,
            'message': 'Заявка отправлена! Менеджер свяжется с вами в ближайшее время.',
            'application_id': application.id
        })
        
    except Exception as e:
        print(f"Error creating manager contact application: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@app.route('/api/favorites', methods=['POST'])
@login_required  
# @csrf.exempt  # CSRF disabled  # Disable CSRF for API endpoint
def add_to_favorites():
    """Add property to favorites"""
    from models import FavoriteProperty
    data = request.get_json()
    
    # Check if already in favorites
    existing = FavoriteProperty.query.filter_by(
        user_id=current_user.id,
        property_name=data['property_name']
    ).first()
    
    if existing:
        return jsonify({'success': False, 'error': 'Уже в избранном'})
    
    try:
        favorite = FavoriteProperty(
            user_id=current_user.id,
            property_name=data['property_name'],
            property_type=data['property_type'],
            property_size=float(data['property_size']),
            property_price=int(data['property_price']),
            complex_name=data['complex_name'],
            developer_name=data['developer_name'],
            property_image=data.get('property_image'),
            cashback_amount=int(data.get('cashback_amount', 0)),
            cashback_percent=float(data.get('cashback_percent', 0))
        )
        db.session.add(favorite)
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/favorites/<property_id>', methods=['DELETE'])
@login_required
@csrf.exempt  # ✅ FIXED: CSRF protection removed for delete action as requested
def remove_from_favorites(property_id):
    """Remove property from favorites"""
    from models import FavoriteProperty
    
    favorite = FavoriteProperty.query.filter_by(
        user_id=current_user.id,
        property_id=property_id
    ).first()
    
    if favorite:
        try:
            db.session.delete(favorite)
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
    else:
        return jsonify({'success': False, 'error': 'Favorite not found'}), 404

def send_view_notification_to_manager(presentation, view):
    """Отправляет уведомление менеджеру о новом просмотре презентации"""
    try:
        manager = presentation.created_by
        if not manager:
            print(f"Manager not found for presentation {presentation.id}")
            return
            
        # Получаем информацию о просмотре
        client_info = "Неизвестный клиент"
        if presentation.client_name:
            client_info = presentation.client_name
        elif presentation.client_phone:
            client_info = presentation.client_phone
            
        # Формируем сообщение уведомления
        notification_text = f"""📊 Новый просмотр презентации!

📋 "{presentation.title}"
👤 Клиент: {client_info}
🔢 Всего просмотров: {presentation.view_count}
⏰ Время просмотра: {view.viewed_at.strftime('%d.%m.%Y %H:%M')}
🌐 IP: {view.view_ip}
📱 Устройство: {view.user_agent[:50] + '...' if view.user_agent and len(view.user_agent) > 50 else view.user_agent or 'Неизвестно'}

👀 Ссылка на презентацию: {request.url_root}presentation/modern/{presentation.unique_url}
🎯 Панель менеджера: {request.url_root}manager/dashboard"""

        # TODO: Интеграция с Telegram Bot API
        # if hasattr(manager, 'telegram_chat_id') and manager.telegram_chat_id:
        #     send_telegram_notification(manager.telegram_chat_id, notification_text)
        
        # TODO: Интеграция с Email
        # if manager.email:
        #     send_email_notification(manager.email, f"Новый просмотр: {presentation.title}", notification_text)
        
        # Пока просто логируем уведомление
        print(f"📧 NOTIFICATION TO MANAGER {manager.email}:")
        print(notification_text)
        print("-" * 50)
        
        # Отмечаем что уведомление отправлено
        view.notification_sent = True
        db.session.commit()
        
    except Exception as e:
        print(f"Error in send_view_notification_to_manager: {e}")

@app.route('/presentation/<string:unique_url>')
def redirect_old_presentation_url(unique_url):
    """Редирект со старого формата URL на новый для обратной совместимости"""
    return redirect(url_for('view_presentation', unique_id=unique_url), code=301)

@app.route('/presentation/view/<string:unique_id>')
def view_presentation(unique_id):
    """Публичная страница просмотра презентации по уникальной ссылке"""
    print(f"🔥 ROUTE HIT: /presentation/view/{unique_id}")
    print(f"🔥 CLIENT IP: {request.remote_addr}")
    print(f"🔥 USER AGENT: {request.headers.get('User-Agent', 'Unknown')}")
    from models import Collection, CollectionProperty, PresentationView
    
    # Находим презентацию по уникальной ссылке
    presentation = Collection.query.filter_by(
        unique_url=unique_id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        return render_template('error.html', 
                             error="Презентация не найдена", 
                             message="Возможно, ссылка устарела или была удалена"), 404
    
    # Записываем просмотр
    try:
        view = PresentationView(
            collection_id=presentation.id,
            view_ip=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
            referer=request.headers.get('Referer')
        )
        db.session.add(view)
        
        # Увеличиваем счетчик просмотров (без автокоммита)
        presentation.increment_view_count()
        db.session.commit()  # Контролируем транзакцию на уровне view
        
        # Отправляем уведомление менеджеру о новом просмотре
        try:
            send_view_notification_to_manager(presentation, view)
        except Exception as e:
            print(f"Error sending view notification: {e}")
        
    except Exception as e:
        db.session.rollback()
        print(f"Error recording presentation view: {e}")
    
    print("DEBUG: Starting data loading phase...")
    
    # Load properties data to enrich the collection properties
    try:
        print("DEBUG: Loading properties data...")
        properties_data = load_properties()
        print(f"DEBUG: Loaded {len(properties_data)} properties")
        
        print("DEBUG: Loading complexes data...")
        complexes_data = load_residential_complexes()
        print(f"DEBUG: Loaded {len(complexes_data) if complexes_data else 0} complexes")
    except Exception as e:
        print(f"ERROR: Failed to load data: {e}")
        import traceback
        traceback.print_exc()
        # Instead of returning 500, use empty data to allow page to render
        print("FALLBACK: Using empty data to allow page rendering")
        properties_data = []
        complexes_data = []
    
    # Enrich collection properties with full property data (same logic as manager version)
    enriched_properties = []
    for prop in presentation.properties:
        # Find the property in the main properties data
        property_data = None
        for p in properties_data:
            if str(p.get('id')) == str(prop.property_id):
                property_data = p
                break
        
        if property_data:
            # Get complex name from property_data
            complex_name = property_data.get('complex_name', prop.complex_name or 'Не указан')
            
            # Extract images
            images = []
            main_image = '/static/images/no-photo.jpg'
            
            # Parse photos/gallery field (check multiple possible field names)
            photos_raw = property_data.get('photos') or property_data.get('gallery') or property_data.get('images')
            if photos_raw:
                try:
                    import json
                    if isinstance(photos_raw, str) and photos_raw.startswith('['):
                        images = json.loads(photos_raw)
                    elif isinstance(photos_raw, list):
                        images = photos_raw
                    elif isinstance(photos_raw, str):
                        # Single image URL
                        images = [photos_raw]
                except:
                    pass
            
            # If no photos found, try the main image field
            if not images:
                main_img = property_data.get('image') or property_data.get('main_image')
                if main_img:
                    images = [main_img]
            
            if images and len(images) > 0:
                main_image = images[0]
            
            # Parse coordinates
            latitude = property_data.get('address_position_lat') or property_data.get('lat') or 0
            longitude = property_data.get('address_position_lon') or property_data.get('lon') or 0
            
            # ✅ MIGRATED: Get full property details from normalized tables
            try:
                property_detail = PropertyRepository.get_by_inner_id(prop.property_id)
                
                # Enhanced property data with full details
                enhanced_images = images
                enhanced_main_image = main_image
                enhanced_layout = property_data.get('layout_image')
                enhanced_address = property_data.get('address_display_name', '')
                enhanced_description = property_data.get('description', '')
                enhanced_features = property_data.get('features', [])
                enhanced_developer = property_data.get('developer_name', 'Не указан')
                
                # Use property_detail database data if available for richer content
                if property_detail:
                    enhanced_address = property_detail.address or enhanced_address
                    enhanced_developer = property_detail.developer.name if property_detail.developer else enhanced_developer
                    
                    # Get photos from database
                    if property_detail.gallery_images:
                        try:
                            import json
                            db_images = json.loads(property_detail.gallery_images) if isinstance(property_detail.gallery_images, str) else property_detail.gallery_images
                            if db_images and isinstance(db_images, list):
                                enhanced_images = db_images
                                enhanced_main_image = db_images[0] if db_images else enhanced_main_image
                        except Exception as photo_error:
                            print(f"DEBUG: Error parsing photos for property {prop.property_id}: {photo_error}")
                
            except Exception as e:
                print(f"Warning: Could not load enhanced property data for {prop.property_id}: {e}")
            
            # Find complex data for this property
            complex_info = None
            for complex_data in complexes_data:
                if complex_data.get('name') == complex_name or complex_data.get('id') == property_data.get('complex_id'):
                    complex_info = complex_data
                    break
            
            # If no complex found, create basic complex info
            if not complex_info:
                complex_info = {
                    'id': property_data.get('complex_id', 0),
                    'name': complex_name,
                    'developer': enhanced_developer,
                    'address': enhanced_address,
                    'description': f'Жилой комплекс {complex_name}',
                    'photos': [],
                    'amenities': [],
                    'completion_date': 'Не указана'
                }
            
            # Enhanced property data with all details from property page
            enriched_property = {
                'id': prop.property_id,
                'name': prop.property_name,
                'complex_name': complex_name,
                'price': prop.property_price,
                'size': prop.property_size,
                'type': prop.property_type,
                'manager_note': prop.manager_note,
                'order_index': prop.order_index,
                'images': enhanced_images,
                'main_image': enhanced_main_image,
                'layout_image': enhanced_layout,
                'address': enhanced_address,
                'latitude': latitude,
                'longitude': longitude,
                'description': enhanced_description,
                'features': enhanced_features,
                'developer': enhanced_developer,
                'district': property_data.get('parsed_district', 'Не указан'),
                'cashback': property_data.get('cashback', 0),
                'cashback_available': property_data.get('cashback_available', True),
                'price_per_sqm': property_data.get('price_per_sqm', 0),
                'status': property_data.get('status', 'available'),
                'title': property_data.get('title', f"{property_data.get('object_rooms', 0)}-комнатная квартира"),
                'url': property_data.get('url', f"/object/{property_data.get('id')}"),
                # Enhanced with complex data and full property details
                'property_info': property_data,
                'complex_info': complex_info,
                # Additional detailed fields for presentation
                'floor': property_data.get('floor', 0),
                'total_floors': property_data.get('total_floors', 0),
                'apartment_number': property_data.get('apartment_number', ''),
                'balcony': property_data.get('balcony', False),
                'finishing': property_data.get('finishing', 'Не указана'),
                'completion_date': property_data.get('completion_date', 'Не указана'),
                'building': property_data.get('building', ''),
                'windows_direction': property_data.get('windows_direction', ''),
                'view': property_data.get('view', ''),
                'ceiling_height': property_data.get('ceiling_height', 0),
                'kitchen_area': property_data.get('kitchen_area', 0),
                'living_area': property_data.get('living_area', 0),
                'rooms': property_data.get('rooms', 0)
            }
            enriched_properties.append(enriched_property)
    
    # Sort by order_index (handle None values)
    enriched_properties.sort(key=lambda x: x['order_index'] if x['order_index'] is not None else 999)
    
    print(f"DEBUG: About to render template with {len(enriched_properties)} enriched properties")
    print(f"DEBUG: Presentation object: {presentation}")
    print(f"DEBUG: First property sample: {enriched_properties[0] if enriched_properties else 'No properties'}")
    
    # Format presentation data for template (same structure as manager version)
    presentation_data = {
        'id': presentation.id,
        'title': presentation.title,
        'description': presentation.description,
        'client_name': presentation.client_name,
        'client_phone': presentation.client_phone,
        'status': presentation.status,
        'created_at': presentation.created_at,
        'view_count': presentation.view_count,
        'last_viewed_at': presentation.last_viewed_at,
        'properties_count': len(enriched_properties),
        'properties': enriched_properties,
        'unique_url': presentation.unique_url
    }
    
    try:
        print(f"🔥 RENDERING: presentation_view.html with {len(enriched_properties)} properties")
        print(f"🔥 PRESENTATION: {presentation_data['title']}")
        print(f"🔥 VIEW COUNT: {presentation_data['view_count']}")
        
        template_result = render_template('presentation_view.html', 
                                        presentation=presentation_data,
                                        properties=enriched_properties,
                                        manager=presentation.created_by)
        print("🔥 TEMPLATE RENDERED: presentation_view.html success!")
        return template_result
    except Exception as e:
        print(f"ERROR in view_presentation template rendering: {e}")
        import traceback
        traceback.print_exc()
        return f"Template rendering error: {str(e)}", 500

@app.route('/presentation/modern/<string:unique_id>')
def view_modern_presentation(unique_id):
    """Современная версия публичной страницы просмотра презентации"""
    from models import Collection, CollectionProperty, PresentationView, ExcelProperty, ManagerNotification
    
    # Находим презентацию по уникальной ссылке
    presentation = Collection.query.filter_by(
        unique_url=unique_id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        return render_template('error.html', 
                             error="Презентация не найдена", 
                             message="Возможно, ссылка устарела или была удалена"), 404
    
    # Записываем просмотр
    try:
        view = PresentationView(
            collection_id=presentation.id,
            view_ip=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
            referer=request.headers.get('Referer')
        )
        db.session.add(view)
        presentation.increment_view_count()
        
        # Создаем уведомление для менеджера
        manager_id = presentation.created_by_manager_id
        client_name = presentation.client_name or 'Неизвестный клиент'
        presentation_title = presentation.title or 'Презентация'
        view_ip = request.remote_addr or 'Неизвестный IP'
        
        # Формируем текст уведомления
        notification_title = f"Просмотр презентации: {presentation_title}"
        notification_message = f"Клиент {client_name} просмотрел презентацию \"{presentation_title}\". IP адрес: {view_ip}"
        
        # Дополнительная информация в JSON
        extra_data = {
            'client_name': client_name,
            'presentation_title': presentation_title,
            'view_ip': view_ip,
            'user_agent': request.headers.get('User-Agent', ''),
            'referer': request.headers.get('Referer', ''),
            'presentation_url': f"/presentation/modern/{presentation.unique_url}",
            'view_count': presentation.view_count + 1  # +1 так как еще не сохранили
        }
        
        # Создаем уведомление
        notification = ManagerNotification(
            manager_id=manager_id,
            title=notification_title,
            message=notification_message,
            notification_type='presentation_view',
            presentation_id=presentation.id
        )
        notification.set_extra_data(extra_data)
        
        db.session.add(notification)
        db.session.commit()
        
        print(f"✅ Created notification for manager {manager_id}: {notification_title}")
        
    except Exception as e:
        db.session.rollback()
        print(f"Error recording presentation view or creating notification: {e}")
        import traceback
        traceback.print_exc()
    
    # ✅ MIGRATED: Get property data using repository
    enriched_properties = []
    all_complexes = {}  # Словарь для хранения всех уникальных ЖК
    
    for prop in presentation.properties:
        # Load property using repository
        property_obj_orm = PropertyRepository.get_by_inner_id(int(prop.property_id))
        
        if property_obj_orm:
            # Get cashback rate from complex
            cashback_rate = property_obj_orm.complex.cashback_rate if property_obj_orm.complex else 5.0
            
            # Parse photos
            photos = []
            if property_obj_orm.photos:
                try:
                    import json
                    if isinstance(property_obj_orm.photos, str):
                        if property_obj_orm.photos.startswith('['):
                            photos = json.loads(property_obj_orm.photos)
                        elif property_obj_orm.photos.startswith('http'):
                            photos = [url.strip() for url in property_obj_orm.photos.split(',') if url.strip()]
                    elif isinstance(property_obj_orm.photos, list):
                        photos = property_obj_orm.photos
                except Exception as e:
                    photos = []
            
            # Format title
            rooms_text = ""
            if property_obj_orm.rooms == 0:
                rooms_text = "Студия"
            elif property_obj_orm.rooms:
                rooms_text = f"{property_obj_orm.rooms}-комнатная квартира"
            else:
                rooms_text = "Квартира"
            
            # Calculate cashback
            cashback_amount = int((property_obj_orm.price or 0) * cashback_rate / 100)
            
            # Format property object for template
            property_obj = {
                'property_id': property_obj_orm.inner_id,
                'title': rooms_text,
                'rooms': property_obj_orm.rooms or 0,
                'area': property_obj_orm.area or 0,
                'price': property_obj_orm.price or 0,
                'floor': property_obj_orm.floor or 1,
                'total_floors': property_obj_orm.total_floors or property_obj_orm.floor or 1,
                'address': property_obj_orm.address or '',
                'images': photos,
                'complex_name': property_obj_orm.complex.name if property_obj_orm.complex else '',
                'developer_name': property_obj_orm.developer.name if property_obj_orm.developer else '',
                'deadline': '',
                'renovation_type': property_obj_orm.renovation_type or 'Не указано',
                'housing_class': property_obj_orm.complex.object_class_display_name if property_obj_orm.complex else 'Комфорт',
                'cashback_percent': cashback_rate,
                'cashback_amount': cashback_amount,
                'manager_note': prop.manager_note if hasattr(prop, 'manager_note') else None
            }
            
            # Format deadline
            if property_obj_orm.complex and property_obj_orm.complex.end_build_year and property_obj_orm.complex.end_build_quarter:
                quarters = ['I', 'II', 'III', 'IV']
                quarter_text = quarters[property_obj_orm.complex.end_build_quarter - 1] if property_obj_orm.complex.end_build_quarter <= 4 else 'IV'
                property_obj['deadline'] = f"{quarter_text} кв. {property_obj_orm.complex.end_build_year} г."
            
            enriched_properties.append(property_obj)
            
            # Collect unique complexes
            if property_obj_orm.complex:
                complex_key = property_obj_orm.complex.name
                if complex_key not in all_complexes:
                    all_complexes[complex_key] = {
                        'name': property_obj_orm.complex.name,
                        'developer': property_obj_orm.developer.name if property_obj_orm.developer else '',
                        'address': property_obj_orm.address or '',
                        'end_year': property_obj_orm.complex.end_build_year,
                        'end_quarter': property_obj_orm.complex.end_build_quarter,
                        'photos': [],
                        'lat': float(property_obj_orm.latitude) if property_obj_orm.latitude else None,
                        'lon': float(property_obj_orm.longitude) if property_obj_orm.longitude else None,
                        'cashback_rate': cashback_rate
                    }
    
    # ✅ MIGRATED: Загружаем фотографии для каждого комплекса из normalized tables
    for complex_name in all_complexes.keys():
        # Находим объекты этого комплекса с фотографиями
        complex_photos = []
        complex_properties = PropertyRepository.get_all_active(
            filters={'residential_complex': complex_name},
            limit=10
        )
        
        for prop in complex_properties:
            if prop.gallery_images:
                try:
                    import json
                    prop_photos = []
                    if isinstance(prop.gallery_images, str) and prop.gallery_images.startswith('['):
                        prop_photos = json.loads(prop.gallery_images)
                    elif isinstance(prop.gallery_images, list):
                        prop_photos = prop.gallery_images
                    elif isinstance(prop.gallery_images, str) and prop.gallery_images.startswith('http'):
                        prop_photos = [url.strip() for url in prop.gallery_images.split(',') if url.strip()]
                    
                    # Добавляем уникальные фотографии
                    for photo in prop_photos:
                        if photo not in complex_photos:
                            complex_photos.append(photo)
                            if len(complex_photos) >= 10:  # Максимум 10 фотографий на комплекс
                                break
                    
                    if len(complex_photos) >= 10:
                        break
                        
                except Exception as e:
                    continue
        
        # Обновляем фотографии комплекса
        all_complexes[complex_name]['photos'] = complex_photos
    
    # Подготавливаем сводную информацию
    total_complexes = len(all_complexes)
    complex_names = list(all_complexes.keys())
    
    # Подготавливаем данные для шаблона
    presentation_data = {
        'title': presentation.title,
        'client_name': presentation.client_name,
        'description': presentation.description,
        'created_at': presentation.created_at,
        'properties': enriched_properties,
        'total_objects': len(enriched_properties),
        'total_complexes': total_complexes,
        'complex_names': complex_names,
        'all_complexes': all_complexes
    }
    
    return render_template('modern_presentation_view.html', presentation=presentation_data)

@app.route('/api/manager/presentation/<int:presentation_id>/share', methods=['POST'])
@manager_required
def share_presentation(presentation_id):
    """Получить данные для отправки презентации в мессенджеры (безопасная версия)"""
    from models import Collection
    from flask_login import current_user
    import urllib.parse
    
    current_manager = current_user
    
    print(f"DEBUG: share_presentation - presentation_id: {presentation_id}")
    print(f"DEBUG: share_presentation - current_user: {current_user}")
    print(f"DEBUG: share_presentation - current_manager.id: {current_manager.id}")
    print(f"DEBUG: share_presentation - request.method: {request.method}")
    print(f"DEBUG: share_presentation - request.content_type: {request.content_type}")
    
    try:
        data = request.get_json() or {}  # Пустой JSON валиден
        print(f"DEBUG: share_presentation - request data: {data}")
    except Exception as e:
        print(f"DEBUG: share_presentation - JSON parsing error: {e}")
        return jsonify({'success': False, 'error': f'Invalid JSON: {str(e)}'}), 400
    
    # Безопасное логирование после проверки аутентификации
    print(f"DEBUG: share_presentation - current_user.email: {getattr(current_user, 'email', 'Not authenticated')}")
    
    print(f"DEBUG: share_presentation - Looking for presentation {presentation_id} by manager {current_manager.id}")
    
    presentation = Collection.query.filter_by(
        id=presentation_id,
        created_by_manager_id=current_manager.id,
        collection_type='presentation'
    ).first()
    
    print(f"DEBUG: share_presentation - Found presentation: {presentation}")
    
    if not presentation:
        # Try to find presentation regardless of owner for debugging
        any_presentation = Collection.query.filter_by(
            id=presentation_id,
            collection_type='presentation'
        ).first()
        print(f"DEBUG: share_presentation - Any presentation with this ID: {any_presentation}")
        if any_presentation:
            print(f"DEBUG: share_presentation - Presentation exists but belongs to manager {any_presentation.created_by_manager_id}")
        return jsonify({'success': False, 'error': 'Презентация не найдена или у вас нет прав доступа'}), 404
    
    client_name = data.get('client_name', presentation.client_name)
    print(f"DEBUG: share_presentation - Client name: {client_name}")
    
    # Обновляем имя клиента если передано
    if client_name and client_name != presentation.client_name:
        print(f"DEBUG: share_presentation - Updating client name from '{presentation.client_name}' to '{client_name}'")
        presentation.client_name = client_name
        db.session.commit()
    
    # Формируем ссылку
    base_url = request.url_root.rstrip('/')
    presentation_url = f"{base_url}/presentation/modern/{presentation.unique_url}"
    print(f"DEBUG: share_presentation - Presentation URL: {presentation_url}")
    
    # Формируем сообщение для отправки
    properties_count = len(presentation.properties) if presentation.properties else 0
    print(f"DEBUG: share_presentation - Properties count: {properties_count}")
    
    message_text = f"""🏠 Презентация недвижимости от InBack

📋 {presentation.title}
{f'👤 Для: {client_name}' if client_name else ''}

🔢 Подобрано объектов: {properties_count}
📅 Создано: {presentation.created_at.strftime('%d.%m.%Y')}

👀 Смотреть презентацию:
{presentation_url}

💬 Есть вопросы? Свяжитесь с нами!
📞 +7 (XXX) XXX-XX-XX"""
    
    response_data = {
        'success': True,
        'share_data': {
            'presentation_url': presentation_url,
            'message_text': message_text,
            'whatsapp_url': f"https://wa.me/?text={urllib.parse.quote(message_text)}",
            'telegram_url': f"https://t.me/share/url?url={presentation_url}&text={urllib.parse.quote(presentation.title)}",
            'client_name': client_name or 'Клиент',
            'properties_count': properties_count
        }
    }
    
    print(f"DEBUG: share_presentation - Returning response: {response_data}")
    return jsonify(response_data)

@app.route('/api/favorites/toggle', methods=['POST'])
@login_required
# @csrf.exempt  # CSRF disabled  # Disable CSRF for API endpoint
def toggle_favorite():
    """Toggle favorite status for property"""
    from models import FavoriteProperty
    data = request.get_json()
    property_id = data.get('property_id')
    
    if not property_id:
        return jsonify({'success': False, 'error': 'property_id required'}), 400
    
    print(f"DEBUG: Favorites toggle called by user {getattr(current_user, 'id', 'not_authenticated')} for property {property_id}")
    print(f"DEBUG: Request data: {data}")
    
    # Check if already in favorites
    existing = FavoriteProperty.query.filter_by(
        user_id=current_user.id,
        property_id=property_id
    ).first()
    
    try:
        if existing:
            # Remove from favorites
            db.session.delete(existing)
            db.session.commit()
            return jsonify({'success': True, 'action': 'removed', 'is_favorite': False})
        else:
            # Add to favorites
            favorite = FavoriteProperty(
                user_id=current_user.id,
                property_id=property_id,
                property_name=data.get('property_name', ''),
                property_type=data.get('property_type', ''),
                property_size=float(data.get('property_size', 0)),
                property_price=int(data.get('property_price', 0)),
                complex_name=data.get('complex_name', ''),
                developer_name=data.get('developer_name', ''),
                property_image=data.get('property_image'),
                cashback_amount=int(data.get('cashback_amount', 0)),
                cashback_percent=float(data.get('cashback_percent', 0))
            )
            db.session.add(favorite)
            db.session.commit()
            return jsonify({'success': True, 'action': 'added', 'is_favorite': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400



@app.route('/api/collections', methods=['POST'])
@login_required
def create_collection():
    """Create new property collection"""
    from models import Collection
    data = request.get_json()
    
    try:
        collection = Collection(
            user_id=current_user.id,
            title=data['name'],
            description=data.get('description'),
            image_url=data.get('image_url'),
            category=data.get('category')
        )
        db.session.add(collection)
        db.session.commit()
        
        return jsonify({'success': True, 'collection_id': collection.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/collections/<int:collection_id>', methods=['DELETE'])
@login_required
def delete_collection(collection_id):
    """Delete a collection"""
    from models import Collection
    collection = Collection.query.filter_by(
        id=collection_id,
        user_id=current_user.id
    ).first()
    
    if not collection:
        return jsonify({'success': False, 'error': 'Подборка не найдена'}), 404
    
    try:
        db.session.delete(collection)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/documents/upload', methods=['POST'])
@login_required
def upload_documents():
    """Upload documents"""
    from models import Document
    import os
    from werkzeug.utils import secure_filename
    from datetime import datetime
    
    if 'files' not in request.files:
        return jsonify({'success': False, 'error': 'Нет файлов для загрузки'}), 400
    
    files = request.files.getlist('files')
    uploaded_files = []
    
    # Create uploads directory if it doesn't exist
    upload_dir = 'instance/uploads'
    os.makedirs(upload_dir, exist_ok=True)
    
    for file in files:
        if file.filename == '':
            continue
        
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            # Add timestamp to avoid conflicts
            timestamp = str(int(datetime.utcnow().timestamp()))
            filename = f"{timestamp}_{filename}"
            file_path = os.path.join(upload_dir, filename)
            
            try:
                file.save(file_path)
                file_size = os.path.getsize(file_path)
                file_ext = filename.rsplit('.', 1)[1].lower()
                
                # Create document record
                document = Document(
                    user_id=current_user.id,
                    original_filename=secure_filename(file.filename) if file.filename else 'unknown',
                    stored_filename=filename,
                    file_path=file_path,
                    file_size=file_size,
                    file_type=file_ext,
                    document_type=determine_document_type(file.filename),
                    status='На проверке'
                )
                db.session.add(document)
                uploaded_files.append({
                    'filename': file.filename,
                    'size': file_size
                })
            except Exception as e:
                return jsonify({'success': False, 'error': f'Ошибка загрузки файла {file.filename}: {str(e)}'}), 400
    
    try:
        db.session.commit()
        return jsonify({'success': True, 'uploaded_files': uploaded_files})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/documents/<int:document_id>', methods=['DELETE'])
@login_required
def delete_document(document_id):
    """Delete a document"""
    from models import Document
    import os
    
    document = Document.query.filter_by(
        id=document_id,
        user_id=current_user.id
    ).first()
    
    if not document:
        return jsonify({'success': False, 'error': 'Документ не найден'}), 404
    
    try:
        # Delete physical file
        if os.path.exists(document.file_path):
            os.remove(document.file_path)
        
        # Delete database record
        db.session.delete(document)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'jpg', 'jpeg', 'png'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def determine_document_type(filename):
    """Determine document type from filename"""
    filename_lower = filename.lower()
    if any(word in filename_lower for word in ['паспорт', 'passport']):
        return 'Паспорт'
    elif any(word in filename_lower for word in ['справка', 'доходы', 'income']):
        return 'Справка о доходах'
    elif any(word in filename_lower for word in ['договор', 'contract']):
        return 'Договор'
    elif any(word in filename_lower for word in ['снилс', 'снилс']):
        return 'СНИЛС'
    elif any(word in filename_lower for word in ['инн', 'inn']):
        return 'ИНН'
    else:
        return 'Другое'

# Manager authentication and dashboard routes
@app.route('/manager/logout')
def manager_logout():
    """Manager logout"""
    session.pop('manager_id', None)
    session.pop('is_manager', None)
    flash('Вы успешно вышли из системы', 'success')
    return redirect(url_for('manager_login'))

@app.route('/switch-to-client')
def switch_to_client():
    """Switch from manager to client mode"""
    session.pop('manager_id', None)
    session.pop('is_manager', None)
    flash('Переключились в режим клиента', 'info')
    return redirect(url_for('index'))

@app.route('/manager/login', methods=['GET', 'POST'])
@csrf.exempt  # CSRF disabled  # Temporarily disable CSRF for login
def manager_login():
    """Simplified manager login with step-by-step error isolation"""
    if request.method == 'POST':
        # Step 1: Import and basic validation
        try:
            print("STEP 1: Starting manager login process")
            email = request.form.get('email')
            password = request.form.get('password')
            print(f"STEP 1: Got email={email}, password={'*' * len(password) if password else 'None'}")
            
            if not email or not password:
                print("STEP 1: Missing credentials")
                flash('Заполните все поля', 'error')
                return render_template('auth/manager_login.html')
            print("STEP 1: Basic validation passed")
            
        except Exception as e:
            print(f"ERROR IN STEP 1: {e}")
            flash('Ошибка обработки данных', 'error')
            return render_template('auth/manager_login.html')
        
        # Step 2: Database query
        try:
            print("STEP 2: Importing Manager model")
            from models import Manager
            print("STEP 2: Manager model imported successfully")
            
            print("STEP 2: Querying database for manager")
            manager = Manager.query.filter_by(email=email, is_active=True).first()
            print(f"STEP 2: Database query result: {manager is not None}")
            
            if not manager:
                print("STEP 2: Manager not found")
                flash('Неверные данные для входа', 'error')
                return render_template('auth/manager_login.html')
            
            print(f"STEP 2: Manager found - ID: {manager.id}, Email: {manager.email}")
            
        except Exception as e:
            print(f"ERROR IN STEP 2: {e}")
            import traceback
            traceback.print_exc()
            flash('Ошибка подключения к базе данных', 'error')
            return render_template('auth/manager_login.html')
        
        # Step 3: Password verification
        try:
            print("STEP 3: Checking password")
            password_valid = manager.check_password(password)
            print(f"STEP 3: Password check result: {password_valid}")
            
            if not password_valid:
                print("STEP 3: Password invalid")
                flash('Неверные данные для входа', 'error')
                return render_template('auth/manager_login.html')
            
            print("STEP 3: Password verification passed")
            
        except Exception as e:
            print(f"ERROR IN STEP 3: {e}")
            import traceback
            traceback.print_exc()
            flash('Ошибка проверки пароля', 'error')
            return render_template('auth/manager_login.html')
        
        # Step 4: Flask-Login авторизация (ИСПРАВЛЕНО)
        try:
            print("STEP 4: Using Flask-Login")
            login_user(manager, remember=True)  # Используем Flask-Login вместо ручных сессий
            print(f"STEP 4: Flask-Login successful, manager.get_id()={manager.get_id()}")
            
        except Exception as e:
            print(f"ERROR IN STEP 4: {e}")
            import traceback
            traceback.print_exc()
            flash('Ошибка авторизации', 'error')
            return render_template('auth/manager_login.html')
        
        # Step 5: Database update
        try:
            print("STEP 5: Updating last login time")
            from datetime import datetime
            manager.last_login = datetime.utcnow()
            db.session.commit()
            print("STEP 5: Database commit successful")
            
        except Exception as e:
            print(f"ERROR IN STEP 5: {e}")
            import traceback
            traceback.print_exc()
            flash('Ошибка обновления базы данных', 'error')
            return render_template('auth/manager_login.html')
        
        # Step 6: Success redirect
        try:
            print("STEP 6: Preparing success response")
            flash('Добро пожаловать в систему!', 'success')
            print(f"STEP 6: Login successful for manager {manager.email}")
            return redirect(url_for('manager_dashboard'))
            
        except Exception as e:
            print(f"ERROR IN STEP 6: {e}")
            import traceback
            traceback.print_exc()
            flash('Ошибка перенаправления', 'error')
            return render_template('auth/manager_login.html')
    
    # GET request - show login form
    return render_template('auth/manager_login.html')



# Manager Comparison Routes
@app.route('/manager/property-comparison')
@manager_required
def manager_property_comparison():
    """Manager property comparison page"""
    from models import Manager
    
    current_manager = current_user
    
    return render_template('auth/manager_property_comparison.html', current_manager=current_manager)

@app.route('/manager/complex-comparison')
@manager_required
def manager_complex_comparison():
    """Manager complex comparison page"""
    from models import Manager
    
    current_manager = current_user
    
    return render_template('auth/manager_complex_comparison.html', current_manager=current_manager)

@app.route('/api/manager/favorites-properties')
@manager_required
def get_manager_favorite_properties():
    """Get properties with full characteristics for comparison"""
    try:
        # Check if specific IDs are requested for server-side filtering
        from flask import request
        requested_ids = request.args.get('ids', '')
        filter_ids_str = [id.strip() for id in requested_ids.split(',') if id.strip()] if requested_ids else []
        
        # Convert string IDs to integers for PostgreSQL filtering
        filter_ids = []
        for id_str in filter_ids_str:
            try:
                filter_ids.append(int(id_str))
            except (ValueError, TypeError):
                continue  # Skip invalid IDs
        
        # ✅ MIGRATED: Load properties using repository (batch loading)
        import json
        
        try:
            from models import Property, ResidentialComplex, Developer, District
            from sqlalchemy.orm import joinedload
            
            properties_data = []
            
            # Load properties with eager loading of relationships
            query = Property.query.options(
                joinedload(Property.complex),
                joinedload(Property.developer),
                joinedload(Property.district)
            )
            
            if filter_ids:
                # Filter by inner_ids
                query = query.filter(Property.inner_id.in_(filter_ids[:10]))
            else:
                # Get all active properties (limited)
                query = query.filter(Property.status == 'available').limit(100)
            
            properties_orm = query.all()
            
            for prop in properties_orm:
                # Get photos
                photos_data = []
                if hasattr(prop, 'photos') and prop.photos:
                    try:
                        photos_data = json.loads(prop.photos) if isinstance(prop.photos, str) else prop.photos
                    except:
                        photos_data = []
                
                # Fallback to main_image or gallery_images
                if not photos_data:
                    if hasattr(prop, 'main_image') and prop.main_image:
                        photos_data = [prop.main_image]
                    elif hasattr(prop, 'gallery_images') and prop.gallery_images:
                        try:
                            gallery = json.loads(prop.gallery_images) if isinstance(prop.gallery_images, str) else prop.gallery_images
                            photos_data = gallery if isinstance(gallery, list) else []
                        except:
                            pass
                
                first_image = photos_data[0] if photos_data else '/static/images/no-photo.jpg'
                
                # Format room text
                rooms_count = prop.rooms or 0
                area_value = prop.area or 0
                rooms_text = "Студия" if rooms_count == 0 else f"{rooms_count}"
                
                # Format property name
                if rooms_count == 0:
                    property_name = f"Студия, {area_value} м²"
                else:
                    property_name = f"{rooms_count} комн, {area_value} м²"
                
                # Calculate cashback
                complex_name = prop.complex.name if prop.complex else ''
                cashback_value = calculate_cashback(prop.price or 0, complex_name=complex_name)
                
                property_data = {
                    'property_id': str(prop.inner_id or ''),
                    'property_name': property_name,
                    'property_type': prop.complex.object_class_display_name if prop.complex else 'Квартира',
                    'property_size': float(prop.area or 0),
                    'property_price': int(prop.price or 0),
                    'complex_name': complex_name or '',
                    'developer_name': prop.developer.name if prop.developer else 'Не указан',
                    'property_image': first_image,
                    'property_url': f'/object/{prop.inner_id}' if prop.inner_id else None,
                    'district': prop.district.name if prop.district else '',
                    'address': prop.address or '',
                    'floor': str(prop.floor or ''),
                    'total_floors': str(prop.total_floors or ''),
                    'floors_total': str(prop.total_floors or ''),
                    'rooms': str(rooms_count),
                    'living_area': '',
                    'kitchen_area': '',
                    'price_per_sqm': int(prop.price_per_sqm or 0) if prop.price_per_sqm else 0,
                    'condition': prop.renovation_type or '',
                    'ceiling_height': '',
                    'furniture': '',
                    'balcony': '',
                    'view_from_windows': '',
                    'parking': '',
                    'metro_distance': '',
                    'year_built': str(prop.complex.end_build_year or '') if prop.complex else '',
                    'building_type': prop.complex_building_name or '',
                    'decoration': prop.renovation_type or 'no_renovation',
                    'deal_type': prop.deal_type or 'sale',
                    'mortgage_available': 'Нет',
                    'added_at': 'Загружено из PostgreSQL',
                    'cashback_amount': cashback_value,
                    'cashback': cashback_value
                }
                properties_data.append(property_data)
            
            return jsonify({
                'success': True,
                'properties': properties_data,
                'count': len(properties_data)
            })
            
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/favorites-complexes')
@manager_required
def get_manager_favorite_complexes():
    """Get manager's favorite complexes for comparison"""
    from models import ManagerFavoriteComplex
    
    try:
        current_manager = current_user
        
        # Get all favorite complexes for this manager
        favorites = ManagerFavoriteComplex.query.filter_by(
            manager_id=current_manager.id
        ).order_by(ManagerFavoriteComplex.created_at.desc()).all()
        
        complexes_data = []
        for fav in favorites:
            complexes_data.append({
                'id': fav.id,
                'complex_id': fav.complex_id,
                'complex_name': fav.complex_name,
                'developer_name': fav.developer_name,
                'complex_address': fav.complex_address,
                'district': fav.district,
                'min_price': fav.min_price,
                'max_price': fav.max_price,
                'complex_image': fav.complex_image,
                'complex_url': fav.complex_url,
                'added_at': fav.created_at.strftime('%d.%m.%Y %H:%M')
            })
        
        return jsonify({
            'success': True,
            'complexes': complexes_data,
            'count': len(complexes_data)
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/manager/dashboard')
@manager_required
def manager_dashboard():
    from models import Manager, User, CashbackApplication, Document
    
    # ИСПРАВЛЕНО: используем Flask-Login вместо session
    current_manager = current_user
    print(f"DEBUG: Manager dashboard - current_manager: {current_manager.email if current_manager else None}")
    
    # Get statistics (используем current_manager.id)
    total_clients = User.query.filter_by(assigned_manager_id=current_manager.id).count()
    new_clients_count = User.query.filter_by(
        assigned_manager_id=current_manager.id, 
        client_status='Новый'
    ).count()
    
    pending_applications_count = CashbackApplication.query.join(User).filter(
        User.assigned_manager_id == current_manager.id,
        CashbackApplication.status == 'На рассмотрении'
    ).count()
    
    pending_documents_count = Document.query.join(User).filter(
        User.assigned_manager_id == current_manager.id,
        Document.status == 'На проверке'
    ).count()
    
    # Calculate total approved cashback (оптимизировано - используем SQL SUM вместо загрузки в память)
    total_approved_cashback = 0
    try:
        from sqlalchemy import func
        from models import CashbackApplication, User
        total_approved_cashback = db.session.query(
            func.sum(CashbackApplication.cashback_amount)
        ).join(User).filter(
            User.assigned_manager_id == current_manager.id,
            CashbackApplication.status == 'Одобрена'
        ).scalar() or 0
    except Exception as e:
        print(f"Error calculating cashback: {e}")
        total_approved_cashback = 0
    
    # Recent activities (mock data for now)
    recent_activities = [
        {
            'message': 'Новый клиент Иван Петров зарегистрировался',
            'time_ago': '5 минут назад',
            'color': 'blue',
            'icon': 'user-plus'
        },
        {
            'message': 'Заявка на кешбек от Анны Сидоровой требует проверки',
            'time_ago': '1 час назад',
            'color': 'yellow',
            'icon': 'file-alt'
        }
    ]
    
    # Get collections statistics  
    from models import Collection
    collections_count = Collection.query.filter_by(created_by_manager_id=current_manager.id).count()
    recent_collections = Collection.query.filter_by(created_by_manager_id=current_manager.id).order_by(Collection.created_at.desc()).limit(5).all()
    
    # Get presentations statistics
    presentations_count = Collection.query.filter_by(
        created_by_manager_id=current_manager.id, 
        collection_type='presentation'
    ).count()
    
    # Get deals statistics
    from models import Deal
    deals_count = Deal.query.filter_by(manager_id=current_manager.id).count()
    
    # Load data for manager filters
    districts = get_districts_list()
    developers = get_developers_list()
    
    print(f"DEBUG: Rendering dashboard with manager: {current_manager.full_name}")
    try:
        response = make_response(render_template('auth/manager_dashboard.html',
                             current_manager=current_manager,
                             total_clients=total_clients,
                             new_clients_count=new_clients_count,
                             pending_applications_count=pending_applications_count,
                             pending_documents_count=pending_documents_count,
                             total_approved_cashback=total_approved_cashback,
                             recent_activities=recent_activities,
                             pending_notifications=pending_applications_count + pending_documents_count,
                             collections_count=collections_count,
                             presentations_count=presentations_count,
                             deals_count=deals_count,
                             recent_collections=recent_collections,
                             districts=districts,
                             developers=developers))
        # Add anti-cache headers
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        print(f"DEBUG: Error rendering dashboard: {e}")
        import traceback
        traceback.print_exc()
        return f"Error rendering dashboard: {e}", 500


@app.route('/manager/favorites')
@manager_required
def manager_favorites():
    """Manager favorites page - separate page like user favorites"""
    from models import Manager
    
    current_manager = current_user
    
    return render_template('manager/favorites.html', current_manager=current_manager)


@app.route('/manager/presentation/<int:presentation_id>')
@manager_required
def manager_presentation_view(presentation_id):
    """View presentation page inside manager dashboard"""
    from models import Collection, CollectionProperty, Manager
    
    current_manager = current_user
    print(f"DEBUG: Presentation view - manager_id: {current_manager.id}, presentation_id: {presentation_id}")
    
    # Get presentation data
    presentation = Collection.query.filter_by(
        id=presentation_id,
        created_by_manager_id=current_manager.id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        print(f"DEBUG: Presentation {presentation_id} not found or access denied")
        flash('Презентация не найдена или у вас нет доступа к ней', 'error')
        return redirect(url_for('manager_dashboard'))
    
    # Get presentation properties
    collection_properties = CollectionProperty.query.filter_by(
        collection_id=presentation_id
    ).order_by(CollectionProperty.order_index).all()
    
    print(f"DEBUG: Found {len(collection_properties)} properties in presentation")
    
    # Load properties data to enrich the collection properties
    properties_data = load_properties()  # This function already exists in the app
    complexes_data = load_residential_complexes()  # This function already exists in the app
    
    # Enrich collection properties with full property data
    enriched_properties = []
    for cp in collection_properties:
        # Find the property in the main properties data
        property_data = None
        for prop in properties_data:
            if str(prop.get('id')) == str(cp.property_id):
                property_data = prop
                break
        
        if property_data:
            # Get complex name directly from property_data (load_properties already includes it)
            complex_name = property_data.get('residential_complex', property_data.get('complex_name', 'Не указан'))
            
            # Get coordinates from the coordinates object
            coordinates = property_data.get('coordinates', {})
            latitude = coordinates.get('lat') if coordinates else None
            longitude = coordinates.get('lng') if coordinates else None
            
            # Prepare main image - load_properties returns 'main_image', not 'images'
            main_image = property_data.get('main_image', '/static/images/no-photo.jpg')
            images = [main_image] if main_image and main_image != '/static/images/no-photo.jpg' else []
            
            enriched_property = {
                'id': property_data.get('id'),
                'property_id': cp.property_id,
                'manager_note': cp.manager_note,
                'order_index': cp.order_index,
                'rooms': property_data.get('rooms', 0),
                'price': property_data.get('price', 0),
                'area': property_data.get('area', 0),
                'floor': property_data.get('floor', 0),
                'total_floors': property_data.get('total_floors', 0),
                'complex_name': complex_name,
                'property_type': property_data.get('property_type', 'Квартира'),
                'images': images,
                'main_image': main_image,
                'layout_image': property_data.get('layout_image'),  # May not exist in load_properties
                'address': property_data.get('address', ''),
                'latitude': latitude,
                'longitude': longitude,
                'description': property_data.get('description', ''),
                'features': property_data.get('features', []),
                # Add additional fields from load_properties for full compatibility
                'developer': property_data.get('developer', 'Не указан'),
                'district': property_data.get('district', 'Не указан'),
                'cashback': property_data.get('cashback', 0),
                'cashback_available': property_data.get('cashback_available', True),
                'price_per_sqm': property_data.get('price_per_sqm', 0),
                'status': property_data.get('status', 'available'),
                'title': property_data.get('title', f"{property_data.get('rooms', 0)}-комнатная квартира"),
                'url': property_data.get('url', f"/object/{property_data.get('id')}")
            }
            enriched_properties.append(enriched_property)
        else:
            print(f"DEBUG: Property {cp.property_id} not found in main data")
    
    print(f"DEBUG: Enriched {len(enriched_properties)} properties")
    
    # Format presentation data for template
    presentation_data = {
        'id': presentation.id,
        'title': presentation.title,
        'description': presentation.description,
        'client_name': presentation.client_name,
        'client_phone': presentation.client_phone,
        'status': presentation.status,
        'created_at': presentation.created_at,
        'view_count': presentation.view_count,
        'last_viewed_at': presentation.last_viewed_at,
        'properties_count': len(enriched_properties),
        'properties': enriched_properties,
        'unique_url': presentation.unique_url
    }
    
    try:
        return render_template('manager/presentation_view.html',
                             manager=current_manager,
                             presentation=presentation_data)
    except Exception as e:
        print(f"DEBUG: Error rendering presentation view: {e}")
        import traceback
        traceback.print_exc()
        flash('Ошибка при загрузке презентации', 'error')
        return redirect(url_for('manager_dashboard'))


# API routes for manager actions
@app.route('/api/manager/clients')
@manager_required
def get_manager_clients_unified():
    """Get ONLY assigned clients for this manager"""
    current_manager = current_user
    
    try:
        print(f"DEBUG: Getting clients for manager {current_manager.id}")
        # Get ALL users assigned to this manager (regardless of role)
        clients = User.query.filter_by(assigned_manager_id=current_manager.id).all()
        print(f"DEBUG: Found {len(clients)} assigned clients for manager {current_manager.id}")
        clients_data = []
        
        for client in clients:
            # Get latest search as preference indicator
            latest_search = SavedSearch.query.filter_by(user_id=client.id).order_by(SavedSearch.last_used.desc()).first()
            
            client_data = {
                'id': client.id,
                'full_name': client.full_name,
                'email': client.email,
                'phone': client.phone or '',
                'created_at': client.created_at.isoformat() if client.created_at else None,
                'search_preferences': None,
                'status': 'active'  # Default status
            }
            
            if latest_search:
                # Create readable search description
                prefs = []
                if latest_search.property_type:
                    prefs.append(latest_search.property_type)
                if latest_search.location:
                    prefs.append(f"район {latest_search.location}")
                if latest_search.price_min or latest_search.price_max:
                    price_range = []
                    if latest_search.price_min:
                        price_range.append(f"от {latest_search.price_min:,} ₽")
                    if latest_search.price_max:
                        price_range.append(f"до {latest_search.price_max:,} ₽")
                    prefs.append(" ".join(price_range))
                
                client_data['search_preferences'] = ", ".join(prefs) if prefs else "Поиск сохранен"
            
            clients_data.append(client_data)
        
        print(f"DEBUG: Returning {len(clients_data)} clients data")
        return jsonify({
            'success': True,
            'clients': clients_data
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/update_client_status', methods=['POST'])
@manager_required
def update_client_status():
    from models import User
    
    current_manager = current_user
    
    data = request.get_json()
    client_id = data.get('client_id')
    new_status = data.get('status')
    notes = data.get('notes', '')
    
    client = User.query.get(client_id)
    if not client or client.assigned_manager_id != current_manager.id:
        return jsonify({'success': False, 'error': 'Клиент не найден'}), 404
    
    try:
        client.client_status = new_status
        if notes:
            client.client_notes = notes
        client.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/approve_cashback', methods=['POST'])
@manager_required
def approve_cashback():
    from models import CashbackApplication, Manager
    
    current_manager = current_user
    
    data = request.get_json()
    application_id = data.get('application_id')
    action = data.get('action')  # approve, reject
    manager_notes = data.get('manager_notes', '')
    
    application = CashbackApplication.query.get(application_id)
    if not application:
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    
    # Check if client is assigned to this manager
    if application.user.assigned_manager_id != current_manager.id:
        return jsonify({'success': False, 'error': 'У вас нет доступа к этой заявке'}), 403
    
    try:
        if action == 'approve':
            # Check approval limits
            if current_manager and current_manager.max_cashback_approval and application.cashback_amount > current_manager.max_cashback_approval:
                return jsonify({
                    'success': False, 
                    'error': f'Сумма превышает ваш лимит на одобрение ({current_manager.max_cashback_approval:,} ₽)'
                }), 400
            
            application.status = 'Одобрена'
            application.approved_date = datetime.utcnow()
            application.approved_by_manager_id = current_manager.id
            
        elif action == 'reject':
            application.status = 'Отклонена'
        
        if manager_notes:
            application.manager_notes = manager_notes
        
        application.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/contact-requests')
@manager_required  
def get_manager_contact_requests():
    """Get contact manager applications for current manager"""
    try:
        from models import Application
        
        # Get all manager contact applications
        applications = Application.query.filter_by(
            application_type='manager_contact'
        ).order_by(Application.created_at.desc()).all()
        
        result = []
        for app in applications:
            result.append({
                'id': app.id,
                'contact_name': app.contact_name,
                'contact_email': app.contact_email,
                'contact_phone': app.contact_phone,
                'message': app.message,
                'preferred_contact_time': app.preferred_contact_time,
                'status': app.status,
                'created_at': app.created_at.isoformat() if app.created_at else None,
                'updated_at': app.updated_at.isoformat() if app.updated_at else None,
                # Property context if available
                'property_id': app.property_id,
                'property_type': app.property_type,
                'budget_min': app.budget_min,
                'budget_max': app.budget_max
            })
        
        return jsonify({
            'success': True,
            'applications': result,
            'total': len(result)
        })
        
    except Exception as e:
        print(f"Error getting manager contact requests: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/applications')
@manager_required
def get_manager_applications():
    from models import CashbackApplication, User
    
    current_manager = current_user
    
    applications = CashbackApplication.query.join(User).filter(
        User.assigned_manager_id == current_manager.id,
        CashbackApplication.status == 'На рассмотрении'
    ).all()
    
    applications_data = []
    for app in applications:
        applications_data.append({
            'id': app.id,
            'client_name': app.user.full_name,
            'client_email': app.user.email,
            'property_name': app.property_name,
            'complex_name': app.complex_name,
            'cashback_amount': app.cashback_amount,
            'cashback_percent': app.cashback_percent,
            'application_date': app.application_date.strftime('%d.%m.%Y'),
            'status': app.status
        })
    
    return jsonify({'applications': applications_data})

@app.route('/api/manager/documents')
@manager_required
def get_manager_documents():
    from models import Document, User
    
    current_manager = current_user
    
    documents = Document.query.join(User).filter(
        User.assigned_manager_id == current_manager.id,
        Document.status == 'На проверке'
    ).all()
    
    documents_data = []
    for doc in documents:
        documents_data.append({
            'id': doc.id,
            'client_name': doc.user.full_name,
            'client_email': doc.user.email,
            'document_type': doc.document_type or 'Не определен',
            'original_filename': doc.original_filename,
            'file_size': doc.file_size,
            'created_at': doc.created_at.strftime('%d.%m.%Y %H:%M'),
            'status': doc.status
        })
    
    return jsonify({'documents': documents_data})

@app.route('/api/manager/document_action', methods=['POST'])
@manager_required
def manager_document_action():
    from models import Document, Manager
    
    current_manager = current_user
    
    data = request.get_json()
    document_id = data.get('document_id')
    action = data.get('action')  # approve, reject
    notes = data.get('notes', '')
    
    document = Document.query.get(document_id)
    
    if not document:
        return jsonify({'success': False, 'error': 'Документ не найден'}), 404
    
    # Check if client is assigned to this manager
    if document.user.assigned_manager_id != current_manager.id:
        return jsonify({'success': False, 'error': 'У вас нет доступа к этому документу'}), 403
    
    try:
        if action == 'approve':
            document.status = 'Проверен'
        elif action == 'reject':
            document.status = 'Отклонен'
        
        document.reviewed_by_manager_id = current_manager.id
        document.reviewed_at = datetime.utcnow()
        if notes:
            document.reviewer_notes = notes
        
        document.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/application_action', methods=['POST'])
@manager_required
def manager_application_action():
    from models import CashbackApplication, Manager, User
    
    current_manager = current_user
    
    data = request.get_json()
    application_id = data.get('application_id')
    action = data.get('action')  # approve, reject
    notes = data.get('notes', '')
    
    application = CashbackApplication.query.get(application_id)
    
    if not application:
        return jsonify({'success': False, 'error': 'Заявка не найдена'}), 404
    
    # Check if client is assigned to this manager
    if application.user.assigned_manager_id != current_manager.id:
        return jsonify({'success': False, 'error': 'У вас нет доступа к этой заявке'}), 403
    
    try:
        if action == 'approve':
            application.status = 'Одобрена'
            # Add cashback to user's balance
            user = application.user
            user.total_cashback = (user.total_cashback or 0) + application.cashback_amount
        elif action == 'reject':
            application.status = 'Отклонена'
        
        application.reviewed_by_manager_id = current_manager.id
        application.reviewed_at = datetime.utcnow()
        if notes:
            application.manager_notes = notes
        
        application.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/collections')
@manager_required
def get_manager_collections():
    from models import Collection, User
    
    current_manager = current_user
    
    collections = Collection.query.filter_by(created_by_manager_id=current_manager.id).all()
    
    collections_data = []
    for collection in collections:
        collections_data.append({
            'id': collection.id,
            'title': collection.title,
            'description': collection.description,
            'status': collection.status,
            'assigned_to_name': collection.assigned_to.full_name if collection.assigned_to else 'Не назначено',
            'assigned_to_id': collection.assigned_to_user_id,
            'properties_count': len(collection.properties),
            'created_at': collection.created_at.strftime('%d.%m.%Y'),
            'tags': collection.tags
        })
    
    return jsonify({'collections': collections_data})

@app.route('/api/manager/collection/create', methods=['POST'])
@manager_required
def api_create_collection():
    from models import Collection, User
    
    current_manager = current_user
    
    data = request.get_json()
    title = data.get('title')
    description = data.get('description', '')
    assigned_to_user_id = data.get('assigned_to_user_id')
    tags = data.get('tags', '')
    
    if not title:
        return jsonify({'success': False, 'error': 'Название подборки обязательно'}), 400
    
    try:
        collection = Collection()
        collection.title = title
        collection.description = description
        collection.created_by_manager_id = current_manager.id
        collection.assigned_to_user_id = assigned_to_user_id if assigned_to_user_id else None
        collection.tags = tags
        collection.status = 'Черновик'
        
        db.session.add(collection)
        db.session.commit()
        
        return jsonify({'success': True, 'collection_id': collection.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/collection/<int:collection_id>/properties')
@manager_required
def get_collection_properties(collection_id):
    from models import Collection, CollectionProperty
    
    current_manager = current_user
    
    collection = Collection.query.filter_by(
        id=collection_id,
        created_by_manager_id=current_manager.id
    ).first()
    
    if not collection:
        return jsonify({'success': False, 'error': 'Подборка не найдена'}), 404
    
    properties_data = []
    for prop in collection.properties:
        properties_data.append({
            'id': prop.id,
            'property_id': prop.property_id,
            'property_name': prop.property_name,
            'property_price': prop.property_price,
            'complex_name': prop.complex_name,
            'property_type': prop.property_type,
            'property_size': prop.property_size,
            'manager_note': prop.manager_note,
            'order_index': prop.order_index
        })
    
    # Sort by order_index
    properties_data.sort(key=lambda x: x['order_index'])
    
    return jsonify({
        'collection': {
            'id': collection.id,
            'title': collection.title,
            'description': collection.description,
            'status': collection.status
        },
        'properties': properties_data
    })



@app.route('/api/searches/save', methods=['POST'])
@login_required
def api_save_search():
    """Save a search with filters"""
    from models import SavedSearch
    
    data = request.get_json()
    name = data.get('name')
    filters = data.get('filters', {})
    
    if not name:
        return jsonify({'success': False, 'error': 'Название поиска обязательно'}), 400
    
    try:
        search = SavedSearch()
        search.name = name
        search.filters = json.dumps(filters)
        search.user_id = current_user.id
        search.created_at = datetime.utcnow()
        
        db.session.add(search)
        db.session.commit()
        
        return jsonify({'success': True, 'search_id': search.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/searches', methods=['POST'])
@manager_required
def api_manager_save_search():
    """Save a search for a manager"""
    from models import ManagerSavedSearch, Manager, SentSearch
    
    current_manager = current_user
    
    data = request.get_json()
    name = data.get('name')
    filters = data.get('filters', {})
    client_email = data.get('client_email', '')
    
    if not name:
        return jsonify({'success': False, 'error': 'Название поиска обязательно'}), 400
    
    try:
        # Create saved search
        search = ManagerSavedSearch()
        search.name = name
        search.filters = json.dumps(filters)
        search.manager_id = current_manager.id
        search.created_at = datetime.utcnow()
        
        db.session.add(search)
        db.session.commit()
        
        # If client email provided, also create sent search record and send notification
        if client_email:
            sent_search = SentSearch()
            sent_search.saved_search_id = search.id
            sent_search.recipient_email = client_email
            sent_search.sent_at = datetime.utcnow()
            sent_search.manager_id = current_manager.id
            
            db.session.add(sent_search)
            db.session.commit()
            
            # Send notification to client
            manager_name = current_manager.name if current_manager else "Менеджер"
            
            try:
                send_notification(
                    recipient_email=client_email,
                    subject=f"Новый подбор недвижимости от {manager_name}",
                    message=f"Менеджер {manager_name} подготовил для вас персональный подбор недвижимости '{name}'. Посмотрите варианты на сайте InBack.ru",
                    notification_type='saved_search',
                    user_id=None,
                    manager_id=current_manager.id
                )
                return jsonify({'success': True, 'search_id': search.id, 'sent_to_client': True})
            except Exception as email_error:
                print(f"Failed to send email notification: {email_error}")
                return jsonify({'success': True, 'search_id': search.id, 'sent_to_client': False, 'email_error': str(email_error)})
        
        return jsonify({'success': True, 'search_id': search.id, 'sent_to_client': False})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/send_recommendation', methods=['POST'])
@manager_required
def api_manager_send_recommendation():
    """Send a recommendation (property or complex) to a client"""
    from models import Recommendation, Manager, User, RecommendationCategory
    from datetime import datetime
    
    current_manager = current_user
    
    data = request.get_json()
    title = data.get('title', '').strip()
    client_id = data.get('client_id')  # Now using client_id instead of email
    client_email = data.get('client_email', '').strip()
    recommendation_type = data.get('recommendation_type')  # 'property' or 'complex'
    item_id = data.get('item_id')
    item_name = data.get('item_name', '').strip()
    description = data.get('description', '').strip()
    manager_notes = data.get('manager_notes', '').strip()
    highlighted_features = data.get('highlighted_features', [])
    priority_level = data.get('priority_level', 'normal')
    category_id = data.get('category_id')  # New field for category
    category_name = data.get('category_name', '').strip()  # For creating new category
    
    # Debug logging (removing verbose logs for production)
    print(f"DEBUG: Recommendation sent - type={recommendation_type}, item_id={item_id}, client_id={client_id}")
    
    # Validation
    missing_fields = []
    if not title:
        missing_fields.append('заголовок')
    if not client_id:
        missing_fields.append('клиент')
    if not recommendation_type:
        missing_fields.append('тип рекомендации')
    if not item_id:
        missing_fields.append('ID объекта')
    if not item_name:
        missing_fields.append('название объекта')
    
    if missing_fields:
        return jsonify({'success': False, 'error': f'Заполните обязательные поля: {", ".join(missing_fields)}'}), 400
    
    if recommendation_type not in ['property', 'complex']:
        return jsonify({'success': False, 'error': 'Неверный тип рекомендации'}), 400
    
    try:
        # Find client by ID
        client = User.query.get(client_id)
        if not client:
            return jsonify({'success': False, 'error': 'Клиент не найден'}), 400
        
        # Handle category
        category = None
        if category_id == 'new' and category_name:
            # Create new category
            category = RecommendationCategory(
                name=category_name,
                manager_id=current_manager.id,
                client_id=client_id
            )
            db.session.add(category)
            db.session.flush()  # To get the ID
        elif category_id and category_id != 'new':
            # Use existing category
            category = RecommendationCategory.query.filter_by(
                id=category_id,
                manager_id=current_manager.id,
                client_id=client_id,
                is_active=True
            ).first()
        
        # Create recommendation
        recommendation = Recommendation()
        recommendation.manager_id = current_manager.id
        recommendation.client_id = client.id
        recommendation.title = title
        recommendation.description = description
        recommendation.recommendation_type = recommendation_type
        recommendation.item_id = item_id
        recommendation.item_name = item_name
        recommendation.manager_notes = manager_notes
        recommendation.highlighted_features = json.dumps(highlighted_features) if highlighted_features else None
        recommendation.priority_level = priority_level
        recommendation.item_data = json.dumps(data.get('item_data', {}))  # Store full item details
        recommendation.category_id = category.id if category else None
        
        db.session.add(recommendation)
        
        # Update category statistics
        if category:
            category.recommendations_count += 1
            category.last_used = datetime.utcnow()
        
        db.session.commit()
        
        # Send notification to client
        manager = Manager.query.get(manager_id)
        manager_name = manager.name if manager else "Менеджер"
        
        try:
            # Get priority text for notifications
            priority_texts = {
                'urgent': 'Срочно',
                'high': 'Высокий', 
                'normal': 'Обычный'
            }
            priority_text = priority_texts.get(priority_level, 'Обычный')
            
            send_notification(
                recipient_email=client_email,
                subject=f"Новая рекомендация от {manager_name}",
                message=f"Менеджер {manager_name} рекомендует вам: {title}",
                notification_type='recommendation',
                user_id=client.id,
                manager_id=current_manager.id,
                title=title,
                item_id=item_id,
                item_name=item_name,
                description=description,
                manager_name=manager_name,
                priority_text=priority_text,
                recommendation_type=recommendation_type
            )
            return jsonify({'success': True, 'recommendation_id': recommendation.id, 'sent_to_client': True})
        except Exception as email_error:
            print(f"Failed to send email notification: {email_error}")
            return jsonify({'success': True, 'recommendation_id': recommendation.id, 'sent_to_client': False, 'email_error': str(email_error)})
        
    except Exception as e:
        db.session.rollback()
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error creating recommendation: {str(e)}")
        print(f"Full traceback: {error_trace}")
        return jsonify({'success': False, 'error': str(e), 'traceback': error_trace}), 400

@app.route('/api/manager/recommendations', methods=['GET'])
@manager_required
def api_manager_get_recommendations():
    """Get manager's sent recommendations with filters"""
    from models import Recommendation
    
    current_manager = current_user
    
    try:
        # Start with base query
        query = Recommendation.query.filter_by(manager_id=current_manager.id)
        
        # Apply filters from request params
        client_id = request.args.get('client_id')
        status = request.args.get('status')
        rec_type = request.args.get('type')
        priority = request.args.get('priority')
        
        if client_id:
            query = query.filter(Recommendation.client_id == client_id)
        if status:
            query = query.filter(Recommendation.status == status)
        if rec_type:
            query = query.filter(Recommendation.item_type == rec_type)
        if priority:
            query = query.filter(Recommendation.priority == priority)
        
        recommendations = query.order_by(Recommendation.sent_at.desc()).all()
        
        recommendations_data = []
        stats = {'sent': 0, 'viewed': 0, 'interested': 0, 'scheduled': 0}
        
        for rec in recommendations:
            rec_dict = rec.to_dict()
            rec_dict['client_email'] = rec.client.email
            rec_dict['client_name'] = rec.client.full_name
            recommendations_data.append(rec_dict)
            
            # Update stats
            stats['sent'] += 1
            if rec.status == 'viewed':
                stats['viewed'] += 1
            elif rec.status == 'interested':
                stats['interested'] += 1
            elif rec.status == 'scheduled_viewing':
                stats['scheduled'] += 1
        
        return jsonify({
            'success': True, 
            'recommendations': recommendations_data,
            'stats': stats
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/recommendations/<int:recommendation_id>', methods=['DELETE'])
@manager_required  
def api_manager_delete_recommendation(recommendation_id):
    """Delete a recommendation"""
    from models import Recommendation
    
    current_manager = current_user
    
    try:
        # Find recommendation that belongs to this manager
        recommendation = Recommendation.query.filter_by(
            id=recommendation_id, 
            manager_id=current_manager.id
        ).first()
        
        if not recommendation:
            return jsonify({'success': False, 'error': 'Рекомендация не найдена'}), 404
        
        db.session.delete(recommendation)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Рекомендация успешно удалена'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/clients-list', methods=['GET'])
@manager_required
def api_manager_get_clients_list():
    """Get manager's clients for filters"""
    from models import User
    
    current_manager = current_user
    
    try:
        # Get clients assigned to this manager or all buyers
        clients = User.query.filter_by(role='buyer').order_by(User.full_name).all()
        
        clients_data = []
        for client in clients:
            clients_data.append({
                'id': client.id,
                'full_name': client.full_name or 'Без имени',
                'email': client.email
            })
        
        return jsonify({
            'success': True,
            'clients': clients_data
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/properties/search', methods=['POST'])
@login_required
def api_search_properties():
    """Search properties with filters from dashboard"""
    data = request.get_json()
    filters = data.get('filters', {})
    
    try:
        # Convert collection filters to property filters
        property_filters = {}
        
        if filters.get('priceFrom'):
            property_filters['price_min'] = filters['priceFrom']
        if filters.get('priceTo'):
            property_filters['price_max'] = filters['priceTo']
        if filters.get('rooms'):
            property_filters['rooms'] = filters['rooms']
        if filters.get('districts') and filters['districts']:
            property_filters['district'] = filters['districts'][0]
        if filters.get('developers') and filters['developers']:
            property_filters['developer'] = filters['developers'][0]
        if filters.get('areaFrom'):
            property_filters['area_min'] = filters['areaFrom']
        if filters.get('areaTo'):
            property_filters['area_max'] = filters['areaTo']
        
        # Get filtered properties
        filtered_properties = get_filtered_properties(property_filters)
        
        # Add cashback to each property
        for prop in filtered_properties:
            prop['cashback'] = calculate_cashback(
                prop['price'],
                complex_id=prop.get('complex_id'),
                complex_name=prop.get('residential_complex')
            )
        
        # Sort by price ascending
        filtered_properties = sort_properties(filtered_properties, 'price_asc')
        
        return jsonify({
            'success': True,
            'properties': filtered_properties[:50],  # Limit to 50 results
            'total_count': len(filtered_properties)
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/send-property', methods=['POST'])
@manager_required
def api_send_property_to_client():
    """Send saved search results to client via email"""
    from models import SavedSearch, User, ClientPropertyRecommendation
    
    data = request.get_json()
    client_id = data.get('client_id')
    search_id = data.get('search_id')
    message = data.get('message', '')
    
    if not client_id or not search_id:
        return jsonify({'success': False, 'error': 'Клиент и поиск обязательны'}), 400
    
    try:
        # Get the search
        search = SavedSearch.query.get(search_id)
        if not search:
            return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
        
        # Get the client
        client = User.query.get(client_id)
        if not client:
            return jsonify({'success': False, 'error': 'Клиент не найден'}), 404
        
        # Get search filters
        filters = json.loads(search.filters) if search.filters else {}
        
        # Filter properties based on search criteria
        properties = load_properties()
        filtered_properties = filter_properties(properties, filters)
        
        # Create recommendation record
        recommendation = ClientPropertyRecommendation()
        recommendation.client_id = client_id
        recommendation.manager_id = current_user.id
        recommendation.search_name = search.name
        recommendation.search_filters = search.filters
        recommendation.message = message
        recommendation.properties_count = len(filtered_properties)
        recommendation.sent_at = datetime.utcnow()
        
        db.session.add(recommendation)
        db.session.commit()
        
        # Send email with property recommendations
        send_property_email(client, search.name, filtered_properties, message)
        
        return jsonify({'success': True, 'properties_sent': len(filtered_properties)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

def filter_properties(properties, filters):
    """Filter properties based on search criteria"""
    filtered = []
    
    for prop in properties:
        # Price filter
        if filters.get('priceFrom'):
            try:
                if prop.get('price', 0) < int(filters['priceFrom']):
                    continue
            except (ValueError, TypeError):
                pass
        
        if filters.get('priceTo'):
            try:
                if prop.get('price', 0) > int(filters['priceTo']):
                    continue
            except (ValueError, TypeError):
                pass
        
        # Rooms filter
        if filters.get('rooms'):
            prop_rooms = str(prop.get('rooms', ''))
            if filters['rooms'] == 'studio' and prop_rooms != 'studio':
                continue
            elif filters['rooms'] != 'studio' and prop_rooms != str(filters['rooms']):
                continue
        
        # District filter
        if filters.get('districts') and len(filters['districts']) > 0:
            prop_district = prop.get('district', '')
            if prop_district not in filters['districts']:
                continue
        
        # Area filter
        if filters.get('areaFrom'):
            try:
                if prop.get('area', 0) < int(filters['areaFrom']):
                    continue
            except (ValueError, TypeError):
                pass
        
        if filters.get('areaTo'):
            try:
                if prop.get('area', 0) > int(filters['areaTo']):
                    continue
            except (ValueError, TypeError):
                pass
        
        # Developer filter
        if filters.get('developers') and len(filters['developers']) > 0:
            prop_developer = prop.get('developer', '')
            if prop_developer not in filters['developers']:
                continue
        
        filtered.append(prop)
    
    return filtered

def send_property_email(client, search_name, properties, message):
    """Send email with property recommendations"""
    try:
        subject = f"Новая подборка недвижимости: {search_name}"
        
        properties_html = ""
        for prop in properties[:10]:  # Limit to first 10 properties
            properties_html += f"""
            <div style="border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                <h3 style="margin: 0 0 8px 0; color: #1f2937;">{prop.get('name', 'Без названия')}</h3>
                <p style="margin: 0 0 4px 0; color: #6b7280;">ЖК: {prop.get('complex_name', 'Не указан')}</p>
                <p style="margin: 0 0 4px 0; color: #6b7280;">Цена: {prop.get('price', 0):,} ₽</p>
                <p style="margin: 0 0 4px 0; color: #6b7280;">Площадь: {prop.get('area', 0)} м²</p>
                <p style="margin: 0 0 8px 0; color: #6b7280;">Комнат: {prop.get('rooms', 'Не указано')}</p>
                <a href="https://inback.ru/properties/{prop.get('id', '')}" style="color: #0088cc; text-decoration: none;">Подробнее →</a>
            </div>
            """
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #0088cc;">Персональная подборка недвижимости</h2>
                
                <p>Здравствуйте, {client.full_name}!</p>
                
                <p>Ваш менеджер подготовил для вас подборку недвижимости: <strong>{search_name}</strong></p>
                
                {f'<div style="background: #f3f4f6; padding: 16px; border-radius: 8px; margin: 16px 0;"><p style="margin: 0; font-style: italic;">"{message}"</p></div>' if message else ''}
                
                <h3>Найденные варианты ({len(properties)} объектов):</h3>
                
                {properties_html}
                
                {f'<p style="color: #6b7280;">И еще {len(properties) - 10} объектов в полном каталоге...</p>' if len(properties) > 10 else ''}
                
                <div style="margin-top: 32px; padding: 20px; background: #f9fafb; border-radius: 8px; text-align: center;">
                    <h3 style="margin: 0 0 8px 0;">Нужна консультация?</h3>
                    <p style="margin: 0 0 16px 0;">Свяжитесь с вашим персональным менеджером</p>
                    <a href="mailto:manager@inback.ru" style="background: #0088cc; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block;">Написать менеджеру</a>
                </div>
                
                <div style="margin-top: 20px; text-align: center; color: #6b7280; font-size: 14px;">
                    <p>С уважением,<br>Команда InBack.ru</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return send_notification(
            client.email,
            subject,
            html_content,
            notification_type="property_recommendation",
            user_id=client.id
        )
    except Exception as e:
        print(f"Error sending property email: {e}")
        return False

@app.route('/api/manager/collection/<int:collection_id>/add_property', methods=['POST'])
@manager_required
def add_property_to_collection(collection_id):
    from models import Collection, CollectionProperty
    import json
    
    current_manager = current_user
    
    data = request.get_json()
    property_id = data.get('property_id')
    manager_note = data.get('manager_note', '')
    
    collection = Collection.query.filter_by(
        id=collection_id,
        created_by_manager_id=current_manager.id
    ).first()
    
    if not collection:
        return jsonify({'success': False, 'error': 'Подборка не найдена'}), 404
    
    # Load property data from JSON
    try:
        with open('data/properties.json', 'r', encoding='utf-8') as f:
            properties_data = json.load(f)
        
        property_info = None
        for prop in properties_data:
            if str(prop['id']) == str(property_id):
                property_info = prop
                break
        
        if not property_info:
            return jsonify({'success': False, 'error': 'Квартира не найдена'}), 404
        
        # Check if property already in collection
        existing = CollectionProperty.query.filter_by(
            collection_id=collection_id,
            property_id=str(property_id)
        ).first()
        
        if existing:
            return jsonify({'success': False, 'error': 'Квартира уже добавлена в подборку'}), 400
        
        # Get max order_index
        max_order = db.session.query(db.func.max(CollectionProperty.order_index)).filter_by(
            collection_id=collection_id
        ).scalar() or 0
        
        collection_property = CollectionProperty()
        collection_property.collection_id = collection_id
        collection_property.property_id = str(property_id)
        collection_property.property_name = property_info['title']
        collection_property.property_price = property_info['price']
        collection_property.complex_name = property_info.get('residential_complex', 'ЖК не указан')
        collection_property.property_type = f"{property_info['rooms']}-комн"
        collection_property.property_size = property_info['area']
        collection_property.manager_note = manager_note
        collection_property.order_index = max_order + 1
        
        db.session.add(collection_property)
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/collection/<int:collection_id>/send', methods=['POST'])
@manager_required
def send_collection(collection_id):
    from models import Collection
    
    current_manager = current_user
    
    collection = Collection.query.filter_by(
        id=collection_id,
        created_by_manager_id=current_manager.id
    ).first()
    
    if not collection:
        return jsonify({'success': False, 'error': 'Подборка не найдена'}), 404
    
    if not collection.assigned_to_user_id:
        return jsonify({'success': False, 'error': 'Клиент не назначен'}), 400
    
    if len(collection.properties) == 0:
        return jsonify({'success': False, 'error': 'В подборке нет квартир'}), 400
    
    try:
        collection.status = 'Отправлена'
        collection.sent_at = datetime.utcnow()
        collection.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/properties/search')
@manager_required
def search_properties():
    """Search properties using normalized tables"""
    from repositories.property_repository import PropertyRepository
    
    query = request.args.get('q', '').lower()
    limit = int(request.args.get('limit', 20))
    
    try:
        properties = PropertyRepository.get_all_active(limit=limit * 3)
        
        filtered_properties = []
        for prop in properties:
            prop_type = f"{prop.rooms}-комн" if prop.rooms > 0 else "Студия"
            complex_name = prop.residential_complex.name if prop.residential_complex else 'ЖК не указан'
            developer_name = prop.developer.name if prop.developer else ''
            district_name = prop.residential_complex.district if prop.residential_complex else ''
            
            property_title = f"{prop.rooms}-комн {prop.area} м²" if prop.rooms > 0 else f"Студия {prop.area} м²"
            
            if (query in property_title.lower() or 
                query in complex_name.lower() or 
                query in prop_type.lower() or
                query in developer_name.lower() or
                query in district_name.lower()):
                
                photos_list = []
                if prop.gallery_images:
                    try:
                        if isinstance(prop.gallery_images, list):
                            photos_list = prop.gallery_images
                        elif isinstance(prop.gallery_images, str):
                            photos_list = json.loads(prop.gallery_images)
                    except:
                        pass
                
                main_image = prop.main_image or (photos_list[0] if photos_list else '/static/images/property-placeholder.jpg')
                
                filtered_properties.append({
                    'id': prop.inner_id or prop.id,
                    'title': property_title,
                    'price': prop.price or 0,
                    'complex': complex_name,
                    'type': prop_type,
                    'size': prop.area or 0,
                    'image': main_image
                })
            
            if len(filtered_properties) >= limit:
                break
        
        return jsonify({'properties': filtered_properties})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/client/collections')
@login_required
def get_client_collections():
    """Get collections assigned to current user"""
    from models import Collection, CollectionProperty
    from datetime import datetime
    
    user_id = current_user.id
    
    collections = Collection.query.filter_by(assigned_to_user_id=user_id).all()
    
    collections_data = []
    for collection in collections:
        properties_count = len(collection.properties)
        
        # Mark as viewed if not already
        if collection.status == 'Отправлена':
            collection.status = 'Просмотрена'
            collection.viewed_at = datetime.utcnow()
            db.session.commit()
        
        collections_data.append({
            'id': collection.id,
            'title': collection.title,
            'description': collection.description,
            'status': collection.status,
            'created_by_manager_name': collection.created_by.full_name,
            'properties_count': properties_count,
            'created_at': collection.created_at.strftime('%d.%m.%Y'),
            'sent_at': collection.sent_at.strftime('%d.%m.%Y %H:%M') if collection.sent_at else None,
            'tags': collection.tags
        })
    
    return jsonify({'collections': collections_data})

@app.route('/api/client/collection/<int:collection_id>/properties')
@login_required
def get_client_collection_properties(collection_id):
    """Get properties in a collection for client view"""
    from models import Collection, CollectionProperty
    
    user_id = current_user.id
    
    collection = Collection.query.filter_by(
        id=collection_id,
        assigned_to_user_id=user_id
    ).first()
    
    if not collection:
        return jsonify({'success': False, 'error': 'Подборка не найдена'}), 404
    
    properties_data = []
    for prop in collection.properties:
        # Calculate potential cashback (example: 2% of price)
        cashback_percent = 2.0
        cashback_amount = int(prop.property_price * cashback_percent / 100)
        
        properties_data.append({
            'id': prop.id,
            'property_id': prop.property_id,
            'property_name': prop.property_name,
            'property_price': prop.property_price,
            'complex_name': prop.complex_name,
            'property_type': prop.property_type,
            'property_size': prop.property_size,
            'manager_note': prop.manager_note,
            'cashback_amount': cashback_amount,
            'cashback_percent': cashback_percent
        })
    
    # Sort by order_index
    properties_data.sort(key=lambda x: collection.properties[0].order_index if collection.properties else 0)
    
    return jsonify({
        'collection': {
            'id': collection.id,
            'title': collection.title,
            'description': collection.description,
            'status': collection.status,
            'manager_name': collection.created_by.full_name,
            'sent_at': collection.sent_at.strftime('%d.%m.%Y %H:%M') if collection.sent_at else None
        },
        'properties': properties_data
    })

@app.route('/dashboard')
@login_required
def dashboard():
    """User dashboard - ИСПРАВЛЕНО: редиректит админов и менеджеров"""
    from models import Admin, Manager
    
    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Проверяем тип пользователя
    # Админы и менеджеры НЕ должны попадать в пользовательский dashboard
    if isinstance(current_user._get_current_object(), Admin):
        return redirect(url_for('admin_dashboard'))
    elif isinstance(current_user._get_current_object(), Manager):
        return redirect(url_for('manager_dashboard'))
    
    try:
        from models import CashbackApplication, FavoriteProperty, Document, Collection, Recommendation, SentSearch, SavedSearch, UserActivity, Deal
        
        # Get user's data for dashboard
        cashback_apps = CashbackApplication.query.filter_by(user_id=current_user.id).all()
        favorites = FavoriteProperty.query.filter_by(user_id=current_user.id).all()
        documents = Document.query.filter_by(user_id=current_user.id).all()
        collections = Collection.query.filter_by(assigned_to_user_id=current_user.id).order_by(Collection.created_at.desc()).all()
        
        # Get user's deals (сделки созданные менеджером для этого клиента)
        deals = Deal.query.filter_by(client_id=current_user.id).order_by(Deal.created_at.desc()).all()
        
        # Get recommendations from managers (exclude dismissed) with categories
        recommendations = Recommendation.query.filter(
            Recommendation.client_id == current_user.id,
            Recommendation.status != 'dismissed'
        ).options(db.joinedload(Recommendation.category)).order_by(Recommendation.created_at.desc()).all()
        
        # Get unique categories for the client (import here to avoid circular imports)
        from models import RecommendationCategory
        categories = RecommendationCategory.query.filter_by(client_id=current_user.id, is_active=True).all()
        
        # ОПТИМИЗАЦИЯ: Загружаем только нужные properties для recommendations из БД
        from models import Property, ResidentialComplex
        
        # Получаем ID нужных объектов
        property_ids = [rec.item_id for rec in recommendations if rec.recommendation_type == 'property' and rec.item_id]
        
        # Загружаем только нужные properties с JOIN (одним запросом!)
        properties_dict = {}
        if property_ids:
            properties_query = db.session.query(
                Property,
                ResidentialComplex.name.label('complex_name')
            ).outerjoin(
                ResidentialComplex, Property.complex_id == ResidentialComplex.id
            ).filter(
                Property.inner_id.in_(property_ids),
                Property.is_active == True
            ).all()
            
            for prop, complex_name in properties_query:
                properties_dict[prop.inner_id] = {
                    'id': prop.inner_id,
                    'rooms': prop.rooms,
                    'area': prop.area,
                    'floor': prop.floor,
                    'total_floors': prop.total_floors,
                    'price': prop.price,
                    'main_image': prop.main_image,
                    'complex_name': complex_name,
                    'property_type': 'apartment',
                    'property_type_ru': 'Квартира'
                }
        
        # Enrich recommendations with property details
        for rec in recommendations:
            if rec.recommendation_type == 'property' and rec.item_id:
                try:
                    property_data = properties_dict.get(rec.item_id)
                    if property_data:
                        # Create a simple object to store property details
                        class PropertyDetails:
                            def __init__(self, data):
                                for key, value in data.items():
                                    setattr(self, key, value)
                                self.residential_complex = data.get('complex_name', 'Не указан')
                        
                        rec.property_details = PropertyDetails(property_data)
                        print(f"✅ Loaded property {rec.item_id}: {property_data.get('rooms')} комн, ЖК {property_data.get('complex_name')}")
                    else:
                        print(f"Property {rec.item_id} not found in database")
                        rec.property_details = None
                except Exception as e:
                    print(f"Error loading property details for recommendation {rec.id}: {e}")
                    rec.property_details = None
        
        # Get sent searches from managers
        sent_searches = SentSearch.query.filter_by(client_id=current_user.id).order_by(SentSearch.sent_at.desc()).all()
        
        # Get user's saved searches
        saved_searches = SavedSearch.query.filter_by(user_id=current_user.id).order_by(SavedSearch.created_at.desc()).all()
        
        # Calculate totals
        total_cashback = sum(app.cashback_amount for app in cashback_apps if app.status == 'Выплачена')
        pending_cashback = sum(app.cashback_amount for app in cashback_apps if app.status == 'Одобрена')
        active_apps = len([app for app in cashback_apps if app.status in ['На рассмотрении', 'Требуются документы']])
        
        # Get developer appointments
        from models import DeveloperAppointment
        appointments = DeveloperAppointment.query.filter_by(user_id=current_user.id).order_by(DeveloperAppointment.appointment_date.desc()).limit(3).all()
        
        # Load data for manager filters
        districts = get_districts_list()
        developers = get_developers_list()
        
        # Get recent user activities
        recent_activities = UserActivity.get_recent_activities(current_user.id, limit=5)
        
        return render_template('auth/dashboard.html', 
                             cashback_applications=cashback_apps,
                             favorites=favorites,
                             documents=documents,
                             collections=collections,
                             appointments=appointments,
                             recommendations=recommendations,
                             categories=categories,
                             sent_searches=sent_searches,
                             saved_searches=saved_searches,
                             deals=deals,
                             total_cashback=total_cashback,
                             pending_cashback=pending_cashback,
                             active_apps=active_apps,
                             districts=districts,
                             developers=developers,
                             recent_activities=recent_activities)
    except Exception as e:
        print(f"Dashboard error: {str(e)}")
        import traceback
        traceback.print_exc()
        # Return basic dashboard on error
        districts = get_districts_list()
        developers = get_developers_list()
        
        return render_template('auth/dashboard.html', 
                             cashback_applications=[],
                             favorites=[],
                             documents=[],
                             collections=[],
                             appointments=[],
                             recommendations=[],
                             sent_searches=[],
                             saved_searches=[],
                             deals=[],
                             total_cashback=0,
                             pending_cashback=0,
                             active_apps=0,
                             districts=districts,
                             developers=developers,
                             recent_activities=[])

@app.route('/logout')
@login_required
def logout():
    """Logout user - ИСПРАВЛЕНО: очищает ВСЕ сессии"""
    logout_user()  # Flask-Login logout
    
    # Очищаем все ручные сессии (legacy код)
    session.pop('manager_id', None)
    session.pop('admin_id', None)
    session.pop('is_manager', None)
    session.pop('is_admin', None)
    session.pop('user_id', None)
    session.pop('temp_user_id', None)
    
    flash('Вы успешно вышли из системы', 'success')
    return redirect(url_for('index'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    """User profile page"""
    from models import Manager
    
    if request.method == 'POST':
        try:
            # Update profile information
            current_user.full_name = request.form.get('full_name', current_user.full_name)
            current_user.phone = request.form.get('phone', current_user.phone)
            current_user.email = request.form.get('email', current_user.email)
            
            # Update password if provided
            new_password = request.form.get('new_password')
            if new_password:
                confirm_password = request.form.get('confirm_password')
                if new_password == confirm_password:
                    current_user.password_hash = generate_password_hash(new_password)
                    flash('Пароль успешно изменен', 'success')
                else:
                    flash('Пароли не совпадают', 'error')
                    return redirect(url_for('profile'))
            
            db.session.commit()
            flash('Профиль успешно обновлен', 'success')
            return redirect(url_for('profile'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при обновлении профиля: {str(e)}', 'error')
            return redirect(url_for('profile'))
    
    # Get assigned manager info
    assigned_manager = None
    if current_user.assigned_manager_id:
        assigned_manager = Manager.query.get(current_user.assigned_manager_id)
    
    return render_template('auth/profile.html', 
                         user=current_user,
                         assigned_manager=assigned_manager)

@app.route('/manager/profile', methods=['GET', 'POST'])
@manager_required
def manager_profile():
    """Manager profile page"""
    from models import Manager
    
    current_manager = current_user
    
    if request.method == 'POST':
        try:
            # Update profile information
            full_name = request.form.get('full_name', '')
            if full_name:
                name_parts = full_name.strip().split(maxsplit=1)
                current_manager.first_name = name_parts[0] if len(name_parts) > 0 else current_manager.first_name
                current_manager.last_name = name_parts[1] if len(name_parts) > 1 else current_manager.last_name
            
            current_manager.phone = request.form.get('phone', current_manager.phone)
            current_manager.email = request.form.get('email', current_manager.email)
            
            # Update password if provided
            new_password = request.form.get('new_password')
            if new_password:
                confirm_password = request.form.get('confirm_password')
                if new_password == confirm_password:
                    current_manager.password_hash = generate_password_hash(new_password)
                    flash('Пароль успешно изменен', 'success')
                else:
                    flash('Пароли не совпадают', 'error')
                    return redirect(url_for('manager_profile'))
            
            db.session.commit()
            flash('Профиль успешно обновлен', 'success')
            return redirect(url_for('manager_profile'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при обновлении профиля: {str(e)}', 'error')
            return redirect(url_for('manager_profile'))
    
    return render_template('auth/manager_profile.html', 
                         manager=current_manager)

@app.route('/profile/upload-avatar', methods=['POST'])
@login_required
def upload_user_avatar():
    """Upload user avatar"""
    import os
    from werkzeug.utils import secure_filename
    
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'error': 'Файл не выбран'}), 400
    
    file = request.files['avatar']
    
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Файл не выбран'}), 400
    
    # Check file extension
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    filename = secure_filename(file.filename)
    if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
        return jsonify({'success': False, 'error': 'Недопустимый формат файла. Разрешены: PNG, JPG, JPEG, GIF, WEBP'}), 400
    
    # Check file size (max 5MB)
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    if file_size > 5 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'Размер файла превышает 5MB'}), 400
    
    try:
        # Generate unique filename
        import uuid
        ext = filename.rsplit('.', 1)[1].lower()
        new_filename = f"user_{current_user.id}_{uuid.uuid4().hex[:8]}.{ext}"
        
        # Save file
        upload_folder = os.path.join('static', 'uploads', 'avatars')
        os.makedirs(upload_folder, exist_ok=True)
        filepath = os.path.join(upload_folder, new_filename)
        file.save(filepath)
        
        # Update user profile_image in database
        avatar_url = f"/static/uploads/avatars/{new_filename}"
        current_user.profile_image = avatar_url
        db.session.commit()
        
        return jsonify({'success': True, 'avatar_url': avatar_url})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'Ошибка при загрузке файла: {str(e)}'}), 500

@app.route('/manager/profile/upload-avatar', methods=['POST'])
@manager_required
def upload_manager_avatar():
    """Upload manager avatar"""
    import os
    from werkzeug.utils import secure_filename
    from models import Manager
    
    current_manager = current_user
    
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'error': 'Файл не выбран'}), 400
    
    file = request.files['avatar']
    
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Файл не выбран'}), 400
    
    # Check file extension
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    filename = secure_filename(file.filename)
    if '.' not in filename or filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
        return jsonify({'success': False, 'error': 'Недопустимый формат файла. Разрешены: PNG, JPG, JPEG, GIF, WEBP'}), 400
    
    # Check file size (max 5MB)
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    if file_size > 5 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'Размер файла превышает 5MB'}), 400
    
    try:
        # Generate unique filename
        import uuid
        ext = filename.rsplit('.', 1)[1].lower()
        new_filename = f"manager_{current_manager.id}_{uuid.uuid4().hex[:8]}.{ext}"
        
        # Save file
        upload_folder = os.path.join('static', 'uploads', 'avatars')
        os.makedirs(upload_folder, exist_ok=True)
        filepath = os.path.join(upload_folder, new_filename)
        file.save(filepath)
        
        # Update manager profile_image in database
        avatar_url = f"/static/uploads/avatars/{new_filename}"
        current_manager.profile_image = avatar_url
        db.session.commit()
        
        return jsonify({'success': True, 'avatar_url': avatar_url})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'Ошибка при загрузке файла: {str(e)}'}), 500

@app.route('/api/search')
def api_search():
    """API endpoint for global search"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    
    results = search_global(query)
    return jsonify(results)

@app.route('/search')
def search_results():
    """Search results page"""
    query = request.args.get('q', '').strip()
    search_type = request.args.get('type', 'all')  # all, residential_complex, district, developer, street
    
    results = []
    if query:
        results = search_global(query)
        
        # Filter by type if specified
        if search_type != 'all':
            results = [r for r in results if r['type'] == search_type]
    
    return render_template('search_results.html', 
                         query=query, 
                         results=results,
                         search_type=search_type)


@app.route('/api/smart-search-suggestions')
def smart_search_suggestions():
    """API endpoint for search suggestions with intelligent keyword matching"""
    query = request.args.get('q', '').strip().lower()
    if not query or len(query) < 1:
        return jsonify({'suggestions': []})
    
    suggestions = []
    
    try:
        # Intelligent room type matching patterns
        room_patterns = {
            # Single room patterns
            ('1', '1-', '1-к', '1-ко', '1-ком', '1 к', '1 ко', '1 ком', 'одн', 'одно', 'однок', 'однокомн', 'однокомнат', 'однокомнатн', 'один', 'одной'): ('1-комнатная квартира', 'rooms', '1'),
            # Two room patterns  
            ('2', '2-', '2-к', '2-ко', '2-ком', '2 к', '2 ко', '2 ком', 'двух', 'двухк', 'двухком', 'двухкомн', 'двухкомнат', 'два', 'двой', 'двойн'): ('2-комнатная квартира', 'rooms', '2'),
            # Three room patterns
            ('3', '3-', '3-к', '3-ко', '3-ком', '3 к', '3 ко', '3 ком', 'трех', 'трёх', 'трехк', 'трёхк', 'трехком', 'трёхком', 'три', 'трой'): ('3-комнатная квартира', 'rooms', '3'),
            # Four room patterns
            ('4', '4-', '4-к', '4-ко', '4-ком', '4 к', '4 ко', '4 ком', 'четыр', 'четырех', 'четырёх', 'четырехк', 'четырёхк', 'четыре'): ('4-комнатная квартира', 'rooms', '4'),
            # Studio patterns
            ('студ', 'studio', 'студий', 'студия'): ('Студия', 'rooms', 'studio'),
        }
        
        # Check room type patterns first
        for patterns, (room_text, type_val, value) in room_patterns.items():
            for pattern in patterns:
                if query.startswith(pattern) or pattern in query:
                    suggestions.append({
                        'text': room_text,
                        'type': type_val,
                        'value': value,
                        'category': 'Тип квартиры'
                    })
                    break
        
        # Search in regional data first (regions and cities)
        from models import Region, City
        
        # Search regions
        regions = Region.query.filter(Region.name.ilike(f'%{query}%')).limit(5).all()
        for region in regions:
            suggestions.append({
                'text': region.name,
                'type': 'region',
                'value': region.slug,
                'category': 'Регион'
            })
        
        # Search cities
        cities = City.query.filter(City.name.ilike(f'%{query}%')).limit(5).all()
        for city in cities:
            suggestions.append({
                'text': f"{city.name} ({city.region.name if city.region else 'Неизвестный регион'})",
                'type': 'city',
                'value': city.slug,
                'category': 'Город'
            })

        # Search in database categories (districts, developers, complexes)
        cursor = db.session.execute(text("""
            SELECT name, category_type, slug 
            FROM search_categories 
            WHERE LOWER(name) LIKE :query 
            ORDER BY 
                CASE 
                    WHEN LOWER(name) LIKE :exact_start THEN 1
                    WHEN LOWER(name) LIKE :word_start THEN 2
                    ELSE 3
                END,
                LENGTH(name)
            LIMIT 10
        """), {
            'query': f'%{query}%',
            'exact_start': f'{query}%',
            'word_start': f'% {query}%'
        })
        
        category_names = {
            'district': 'Район',
            'developer': 'Застройщик', 
            'complex': 'ЖК',
            'rooms': 'Тип квартиры',
            'region': 'Регион',
            'city': 'Город'
        }
        
        for row in cursor:
            name, category_type, slug = row
            suggestions.append({
                'text': name,
                'type': category_type,
                'value': slug,
                'category': category_names.get(category_type, category_type.title())
            })
        
        # Remove duplicates while preserving order
        seen = set()
        unique_suggestions = []
        for s in suggestions:
            key = (s['text'], s['type'])
            if key not in seen:
                seen.add(key)
                unique_suggestions.append(s)
        
        return jsonify({'suggestions': unique_suggestions[:12]})
        
    except Exception as e:
        app.logger.error(f"Smart search error: {e}")
        return jsonify({'suggestions': []})

def init_search_data():
    """Initialize search data in database"""
    from models import District, Developer, ResidentialComplex, Street, RoomType
    
    # Districts
    districts_data = [
        ('Центральный', 'tsentralnyy'), ('Западный', 'zapadny'), 
        ('Карасунский', 'karasunsky'), ('Прикубанский', 'prikubansky'),
        ('Фестивальный', 'festivalny'), ('Юбилейный', 'yubileynyy'),
        ('Гидростроителей', 'gidrostroitelei'), ('Солнечный', 'solnechny'),
        ('Панорама', 'panorama'), ('Музыкальный', 'muzykalnyy')
    ]
    
    for name, slug in districts_data:
        if not District.query.filter_by(slug=slug).first():
            district = District(name=name, slug=slug)
            db.session.add(district)
    
    # Room types
    room_types_data = [
        ('Студия', 0), ('1-комнатная квартира', 1), 
        ('2-комнатная квартира', 2), ('3-комнатная квартира', 3), 
        ('4-комнатная квартира', 4), ('Пентхаус', 5)
    ]
    
    for name, rooms_count in room_types_data:
        if not RoomType.query.filter_by(name=name).first():
            room_type = RoomType(name=name, rooms_count=rooms_count)
            db.session.add(room_type)
    
    # Developers
    developers_data = [
        ('Краснодар Инвест', 'krasnodar-invest'),
        ('ЮгСтройИнвест', 'yugstroyinvest'),
        ('Флагман', 'flagman'),
        ('Солнечный город', 'solnechny-gorod'),
        ('Премьер', 'premier')
    ]
    
    for name, slug in developers_data:
        if not Developer.query.filter_by(slug=slug).first():
            developer = Developer(name=name, slug=slug)
            db.session.add(developer)
    
    # Residential complexes
    complexes_data = [
        ('Солнечный', 'solnechny', 1, 1),
        ('Панорама', 'panorama', 1, 2),
        ('Гармония', 'garmoniya', 2, 3),
        ('Европейский квартал', 'evropeyskiy-kvartal', 3, 1),
        ('Флагман', 'flagman', 4, 4)
    ]
    
    for name, slug, district_id, developer_id in complexes_data:
        if not ResidentialComplex.query.filter_by(slug=slug).first():
            complex = ResidentialComplex(name=name, slug=slug, district_id=district_id, developer_id=developer_id)
            db.session.add(complex)
    
    db.session.commit()


# ==================== ADMIN ROUTES ====================

@app.route('/admin/login', methods=['GET', 'POST'])
@csrf.exempt  # Exempt admin login from CSRF protection
def admin_login():
    """Admin login page - ИСПРАВЛЕНО: использует Flask-Login"""
    if request.method == 'POST':
        from models import Admin
        email = request.form.get('email')
        password = request.form.get('password')
        
        admin = Admin.query.filter_by(email=email, is_active=True).first()
        
        if admin and admin.check_password(password):
            # Используем Flask-Login вместо ручных сессий
            login_user(admin, remember=True)
            admin.last_login = datetime.utcnow()
            db.session.commit()
            flash('Добро пожаловать в панель администратора!', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Неверный email или пароль', 'error')
    
    return render_template('admin/admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    """Admin logout - ИСПРАВЛЕНО: использует Flask-Login"""
    logout_user()  # Используем Flask-Login
    flash('Вы вышли из панели администратора', 'info')
    return redirect(url_for('admin_login'))

def admin_required(f):
    """Decorator to require admin authentication - ИСПРАВЛЕНО: проверяет тип модели"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from models import Admin
        # Проверяем что пользователь авторизован и это Admin
        if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Admin):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/admin')
def admin_base():
    """Base admin route - redirects to dashboard or login - ИСПРАВЛЕНО: проверяет тип модели"""
    from models import Admin
    # Проверяем что пользователь авторизован и это Admin
    if current_user.is_authenticated and isinstance(current_user._get_current_object(), Admin):
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('admin_login'))

@app.route('/admin/client-management')
@admin_required
def admin_client_management():
    """Separate page for client-manager assignment"""
    try:
        from models import Admin
        
        # ИСПРАВЛЕНО: Используем Flask-Login current_user
        current_admin = current_user
        if not current_admin:
            flash('Админ не найден', 'error')
            return redirect(url_for('admin_login'))
        
        return render_template('admin/client_management.html', admin=current_admin)
        
    except Exception as e:
        print(f"ERROR in admin_client_management: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Ошибка загрузки страницы: {str(e)}', 'error')
        return redirect(url_for('admin_dashboard'))

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    """Admin dashboard with analytics - ИСПРАВЛЕНО: использует Flask-Login"""
    from models import Admin, User, Manager, CashbackApplication, CallbackRequest
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    if not current_admin:
        return redirect(url_for('admin_login'))
    
    # Analytics data
    stats = {
        'total_users': User.query.count(),
        'total_managers': Manager.query.count(),
        'total_applications': CashbackApplication.query.count(),
        'pending_applications': CashbackApplication.query.filter_by(status='На рассмотрении').count(),
        'approved_applications': CashbackApplication.query.filter_by(status='Одобрена').count(),
        'paid_applications': CashbackApplication.query.filter_by(status='Выплачена').count(),
        'total_cashback_approved': sum(app.cashback_amount for app in CashbackApplication.query.filter_by(status='Одобрена').all()),
        'total_cashback_paid': sum(app.cashback_amount for app in CashbackApplication.query.filter_by(status='Выплачена').all()),
        'active_users': User.query.filter_by(is_active=True).count(),
        'active_managers': Manager.query.filter_by(is_active=True).count(),
        'cashback_requests': CallbackRequest.query.filter(CallbackRequest.notes.contains('кешбек')).count(),
        'new_requests': CallbackRequest.query.filter_by(status='Новая').count(),
    }
    
    # Recent activity
    recent_applications = CashbackApplication.query.order_by(CashbackApplication.created_at.desc()).limit(10).all()
    recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()
    recent_cashback_requests = CallbackRequest.query.filter(
        CallbackRequest.notes.contains('кешбек')
    ).order_by(CallbackRequest.created_at.desc()).limit(5).all()
    
    return render_template('admin/dashboard.html',
                         admin=current_admin,
                         stats=stats,
                         recent_applications=recent_applications,
                         recent_users=recent_users,
                         recent_cashback_requests=recent_cashback_requests,
                         current_date=datetime.now())

@app.route('/admin/cashback-requests')
@admin_required
def admin_cashback_requests():
    """View all cashback requests"""
    from models import CallbackRequest
    
    # Get page number
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    # Filter cashback requests
    cashback_requests = CallbackRequest.query.filter(
        CallbackRequest.notes.contains('кешбек')
    ).order_by(CallbackRequest.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return render_template('admin/cashback_requests.html',
                         requests=cashback_requests)

@app.route('/admin/callback-request/<int:request_id>/status', methods=['POST'])
@admin_required
def update_callback_request_status(request_id):
    """Update callback request status"""
    from models import CallbackRequest
    
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        callback_request = CallbackRequest.query.get_or_404(request_id)
        callback_request.status = new_status
        
        if new_status == 'Обработана':
            callback_request.processed_at = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Статус обновлен'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/favorites/count', methods=['GET'])
@login_required  
def get_favorites_count():
    """Get count of user's favorites"""
    from models import FavoriteProperty, FavoriteComplex
    
    try:
        properties_count = FavoriteProperty.query.filter_by(user_id=current_user.id).count()
        complexes_count = FavoriteComplex.query.filter_by(user_id=current_user.id).count()
        
        return jsonify({
            'success': True,
            'properties_count': properties_count,
            'complexes_count': complexes_count,
            'total_count': properties_count + complexes_count
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/favorites/list', methods=['GET'])
@login_required  
def get_favorites_list():
    """Get user's favorite properties with full details (OPTIMIZED - loads only needed properties)"""
    from models import FavoriteProperty, Property, ResidentialComplex, Developer
    
    try:
        favorites = db.session.query(FavoriteProperty).filter_by(user_id=current_user.id).order_by(FavoriteProperty.created_at.desc()).all()
        
        if not favorites:
            return jsonify({
                'success': True,
                'favorites': []
            })
        
        # ОПТИМИЗАЦИЯ: Получаем только нужные property_id
        property_ids = [fav.property_id for fav in favorites if fav.property_id]
        
        if not property_ids:
            return jsonify({
                'success': True,
                'favorites': []
            })
        
        # ОПТИМИЗАЦИЯ: Загружаем только нужные объекты с JOIN (одним запросом!)
        properties_query = db.session.query(
            Property,
            ResidentialComplex.name.label('complex_name'),
            ResidentialComplex.cashback_rate,
            ResidentialComplex.main_image.label('complex_image'),
            Developer.name.label('developer_name')
        ).outerjoin(
            ResidentialComplex, Property.complex_id == ResidentialComplex.id
        ).outerjoin(
            Developer, Property.developer_id == Developer.id
        ).filter(
            Property.inner_id.in_(property_ids),
            Property.is_active == True
        ).all()
        
        # Создаем словарь для быстрого поиска
        properties_dict = {}
        for prop, complex_name, cashback_rate, complex_image, developer_name in properties_query:
            # Определяем rooms текст
            rooms_text = f"{prop.rooms}-комн" if prop.rooms and prop.rooms > 0 else "Студия"
            
            properties_dict[prop.inner_id] = {
                'id': prop.inner_id,
                'title': f"{rooms_text}, {prop.area} м², {prop.floor}/{prop.total_floors} эт.",
                'complex': complex_name or 'ЖК не указан',
                'district': prop.address or 'Адрес не указан',  # Полный адрес из БД
                'price': prop.price or 0,
                'image': complex_image or prop.main_image or '/static/images/no-photo.jpg',
                'cashback_rate': cashback_rate or 3.5,
                'cashback_amount': int((prop.price or 0) * (cashback_rate or 3.5) / 100),
                'developer': developer_name or 'Застройщик не указан'
            }
        
        # Формируем финальный список избранного
        favorites_list = []
        for fav in favorites:
            if not fav.property_id:
                continue
                
            property_data = properties_dict.get(fav.property_id)
            
            if property_data:
                # Добавляем временную метку из избранного
                property_data['created_at'] = fav.created_at.strftime('%d.%m.%Y в %H:%M') if fav.created_at else 'Недавно'
                property_data['viewed'] = fav.viewed if hasattr(fav, 'viewed') else False
                favorites_list.append(property_data)
            else:
                # Fallback если объект не найден
                favorites_list.append({
                    'id': fav.property_id,
                    'title': f'Объект #{fav.property_id}',
                    'complex': 'ЖК не найден',
                    'district': 'Данные обновляются...',
                    'price': 0,
                    'image': '/static/images/no-photo.jpg',
                    'cashback_amount': 0,
                    'created_at': fav.created_at.strftime('%d.%m.%Y в %H:%M') if fav.created_at else 'Недавно',
                    'viewed': False
                })
        
        return jsonify({
            'success': True,
            'favorites': favorites_list
        })
    
    except Exception as e:
        print(f"Error in get_favorites_list: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/favorites/mark-viewed/<property_id>', methods=['POST'])
@login_required
@csrf.exempt
def mark_favorite_viewed(property_id):
    """Mark favorite property as viewed"""
    from models import FavoriteProperty
    
    try:
        favorite = FavoriteProperty.query.filter_by(
            user_id=current_user.id,
            property_id=str(property_id)
        ).first()
        
        if favorite:
            favorite.viewed = True
            db.session.commit()
            return jsonify({'success': True})
        
        return jsonify({'success': False, 'error': 'Favorite not found'}), 404
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# Complex Favorites API
@app.route('/api/complexes/favorites', methods=['POST'])
@login_required  
@csrf.exempt  # Disable CSRF for API endpoint
def add_complex_to_favorites():
    """Add residential complex to favorites"""
    from models import FavoriteComplex
    data = request.get_json()
    
    complex_id = data.get('complex_id')
    complex_name = data.get('complex_name', 'ЖК')
    
    if not complex_id:
        return jsonify({'success': False, 'error': 'complex_id is required'}), 400
    
    # Check if already in favorites
    existing = FavoriteComplex.query.filter_by(
        user_id=current_user.id,
        complex_id=str(complex_id)
    ).first()
    
    if existing:
        return jsonify({'success': False, 'error': 'Complex already in favorites'}), 400
    
    try:
        # Create favorite complex record
        favorite = FavoriteComplex(
            user_id=current_user.id,
            complex_id=str(complex_id),
            complex_name=complex_name,
            developer_name=data.get('developer_name', ''),
            complex_address=data.get('address', ''),
            district=data.get('district', ''),
            min_price=data.get('min_price'),
            max_price=data.get('max_price'),
            complex_image=data.get('image', ''),
            complex_url=data.get('url', ''),
            status=data.get('status', 'В продаже')
        )
        
        db.session.add(favorite)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'ЖК добавлен в избранное'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/complexes/favorites/<complex_id>', methods=['DELETE'])
@login_required
@csrf.exempt  # Disable CSRF for API endpoint
def remove_complex_from_favorites(complex_id):
    """Remove residential complex from favorites"""
    from models import FavoriteComplex
    
    favorite = FavoriteComplex.query.filter_by(
        user_id=current_user.id,
        complex_id=str(complex_id)
    ).first()
    
    if not favorite:
        return jsonify({'success': False, 'error': 'Complex not in favorites'}), 404
    
    try:
        db.session.delete(favorite)
        db.session.commit()
        return jsonify({'success': True, 'message': 'ЖК удален из избранного'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/complexes/favorites/toggle', methods=['POST'])
@login_required
@csrf.exempt  # Disable CSRF for API endpoint
def toggle_complex_favorite():
    """Toggle favorite status for residential complex"""
    from models import FavoriteComplex
    data = request.get_json()
    complex_id = data.get('complex_id')
    
    if not complex_id:
        return jsonify({'success': False, 'error': 'complex_id is required'}), 400
    
    try:
        existing = FavoriteComplex.query.filter_by(
            user_id=current_user.id,
            complex_id=str(complex_id)
        ).first()
        
        if existing:
            # Remove from favorites
            db.session.delete(existing)
            db.session.commit()
            return jsonify({'success': True, 'favorited': False, 'message': 'ЖК удален из избранного'})
        else:
            # Add to favorites
            favorite = FavoriteComplex(
                user_id=current_user.id,
                complex_id=str(complex_id),
                complex_name=data.get('complex_name', 'ЖК'),
                developer_name=data.get('developer_name', ''),
                complex_address=data.get('address', ''),  # ✅ ИСПРАВЛЕНО: complex_address вместо address_display_name
                district=data.get('district', ''),
                min_price=data.get('min_price'),
                max_price=data.get('max_price'),
                complex_image=data.get('image', ''),
                complex_url=data.get('url', ''),
                status=data.get('status', 'В продаже')
            )
            
            db.session.add(favorite)
            db.session.commit()
            return jsonify({'success': True, 'favorited': True, 'message': 'ЖК добавлен в избранное'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/complexes/favorites/list', methods=['GET'])
@login_required
def get_user_complex_favorites_list():
    """Get user's favorite complexes with full details from residential_complexes table"""
    from models import FavoriteComplex, ResidentialComplex, Developer, District
    
    try:
        # Получаем избранное пользователя
        favorites = FavoriteComplex.query.filter_by(user_id=current_user.id).order_by(FavoriteComplex.created_at.desc()).all()
        
        # Собираем ID комплексов
        complex_ids_int = []
        for fav in favorites:
            try:
                complex_ids_int.append(int(fav.complex_id))
            except:
                continue
        
        # Batch-загрузка реальных данных из residential_complexes
        complexes_dict = {}
        if complex_ids_int:
            real_complexes = db.session.query(
                ResidentialComplex.id,
                ResidentialComplex.name,
                ResidentialComplex.address,
                ResidentialComplex.main_image,
                ResidentialComplex.cashback_rate,
                District.name.label('district_name'),
                Developer.name.label('developer_name')
            ).outerjoin(District, ResidentialComplex.district_id == District.id)\
             .outerjoin(Developer, ResidentialComplex.developer_id == Developer.id)\
             .filter(ResidentialComplex.id.in_(complex_ids_int)).all()
            
            for rc in real_complexes:
                complexes_dict[rc.id] = rc
        
        # Batch-загрузка цен и фото из properties для каждого ЖК
        prices_dict = {}
        if complex_ids_int:
            # Получаем min/max цены и фото для каждого ЖК
            prices_query = text("""
                SELECT 
                    rc.id,
                    MIN(p.price) as min_price,
                    MAX(p.price) as max_price,
                    COALESCE(rc.main_image, rc.gallery_images, MIN(p.gallery_images)) as photos
                FROM residential_complexes rc
                LEFT JOIN properties p ON p.complex_id = rc.id AND p.is_active = true
                WHERE rc.id = ANY(:complex_ids)
                GROUP BY rc.id, rc.main_image, rc.gallery_images
            """)
            
            prices_result = db.session.execute(prices_query, {'complex_ids': complex_ids_int})
            prices_dict = {row[0]: {'min_price': row[1] or 0, 'max_price': row[2] or 0, 'photos': row[3]} for row in prices_result}
        
        # Собираем данные для ответа
        complexes_data = []
        for fav in favorites:
            try:
                complex_id_int = int(fav.complex_id)
                rc = complexes_dict.get(complex_id_int)
                
                if rc:
                    # Получаем цены и фото из prices_dict
                    complex_prices = prices_dict.get(complex_id_int, {})
                    photos_data = complex_prices.get('photos', '')
                    
                    # Извлекаем первое фото из JSON массива
                    image_url = '/static/images/no-image.jpg'
                    if photos_data:
                        try:
                            photos_list = json.loads(photos_data) if isinstance(photos_data, str) else photos_data
                            if photos_list and isinstance(photos_list, list) and len(photos_list) > 0:
                                image_url = photos_list[0]
                        except (json.JSONDecodeError, TypeError):
                            if isinstance(photos_data, str) and photos_data.strip():
                                image_url = photos_data
                    
                    # Используем реальные данные из БД
                    complexes_data.append({
                        'id': str(complex_id_int),
                        'name': rc.name or 'ЖК',
                        'address': rc.address or 'Не указано',
                        'district': rc.district_name or 'Не указано',
                        'developer': rc.developer_name or 'Не указано',
                        'image': image_url,
                        'cashback_rate': rc.cashback_rate or 5,
                        'min_price': complex_prices.get('min_price', 0),
                        'max_price': complex_prices.get('max_price', 0),
                        'url': f'/residential-complex/{complex_id_int}',
                        'status': 'В продаже',
                        'created_at': fav.created_at.strftime('%d.%m.%Y в %H:%M')
                    })
                else:
                    # Используем сохраненные данные если ЖК не найден
                    complexes_data.append({
                        'id': fav.complex_id,
                        'name': fav.complex_name or 'ЖК',
                        'address': fav.complex_address or 'Не указано',
                        'district': fav.district or 'Не указано',
                        'developer': fav.developer_name or 'Не указано',
                        'image': fav.complex_image or '',
                        'cashback_rate': 5,
                        'min_price': fav.min_price or 0,
                        'max_price': fav.max_price or 0,
                        'url': fav.complex_url or '',
                        'status': fav.status or 'В продаже',
                        'created_at': fav.created_at.strftime('%d.%m.%Y в %H:%M')
                    })
            except Exception as e:
                print(f"Error processing complex {fav.complex_id}: {e}")
                continue
        
        return jsonify({
            'success': True,
            'complexes': complexes_data
        })
    
    except Exception as e:
        print(f"Error in get_user_complex_favorites_list: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/favorites/clear-all', methods=['POST'])
@login_required  
@csrf.exempt  # Disable CSRF for API endpoint
def clear_all_favorites():
    """Clear all user's favorite properties"""
    from models import FavoriteProperty
    
    try:
        # Delete all favorites for current user
        deleted_count = db.session.query(FavoriteProperty).filter_by(user_id=current_user.id).delete()
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'message': f'Удалено {deleted_count} избранных квартир',
            'deleted_count': deleted_count
        })
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/complexes/favorites/clear-all', methods=['POST'])
@login_required  
@csrf.exempt  # Disable CSRF for API endpoint
def clear_all_complex_favorites():
    """Clear all user's favorite complexes"""
    from models import FavoriteComplex
    
    try:
        # Delete all complex favorites for current user
        deleted_count = db.session.query(FavoriteComplex).filter_by(user_id=current_user.id).delete()
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'message': f'Удалено {deleted_count} избранных ЖК',
            'deleted_count': deleted_count
        })
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/dashboard/data', methods=['GET'])
@login_required
def get_dashboard_data():
    """ОПТИМИЗИРОВАННЫЙ ENDPOINT: Получить все данные дашборда одним запросом"""
    from models import FavoriteProperty, FavoriteComplex, Comparison, ComparisonComplex, Recommendation, Collection
    
    try:
        # Все счетчики одним запросом к БД
        favorites_properties_count = FavoriteProperty.query.filter_by(user_id=current_user.id).count()
        favorites_complexes_count = FavoriteComplex.query.filter_by(user_id=current_user.id).count()
        comparison_properties_count = Comparison.query.filter_by(user_id=current_user.id).count()
        comparison_complexes_count = ComparisonComplex.query.filter_by(user_id=current_user.id).count()
        recommendations_count = Recommendation.query.filter(
            Recommendation.client_id == current_user.id,
            Recommendation.status != 'dismissed'
        ).count()
        collections_count = Collection.query.filter_by(assigned_to_user_id=current_user.id).count()
        
        return jsonify({
            'success': True,
            'favorites': {
                'properties': favorites_properties_count,
                'complexes': favorites_complexes_count,
                'total': favorites_properties_count + favorites_complexes_count
            },
            'comparison': {
                'properties': comparison_properties_count,
                'complexes': comparison_complexes_count,
                'total': comparison_properties_count + comparison_complexes_count
            },
            'recommendations': recommendations_count,
            'collections': collections_count
        })
    
    except Exception as e:
        print(f"Error in get_dashboard_data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/searches', methods=['GET'])
@login_required
def get_saved_searches():
    """ЗАГЛУШКА: Сохраненные поиски (пока не реализовано)"""
    return jsonify({
        'success': True,
        'searches': []
    })

@app.route('/api/user/stats', methods=['GET'])
@login_required
def get_user_stats():
    """ЗАГЛУШКА: Статистика пользователя (пока не реализовано)"""
    return jsonify({
        'success': True,
        'views': 0,
        'favorites': 0,
        'applications': 0
    })

@app.route('/api/favorites/all', methods=['GET'])
@login_required
def get_all_favorites():
    """ОПТИМИЗИРОВАННЫЙ: Получить ВСЕ избранное (квартиры + ЖК) одним запросом"""
    from models import FavoriteProperty, FavoriteComplex, Property, ResidentialComplex, Developer, District
    
    try:
        # Получаем избранные квартиры
        favorites = db.session.query(FavoriteProperty).filter_by(user_id=current_user.id).order_by(FavoriteProperty.created_at.desc()).all()
        
        properties_list = []
        if favorites:
            property_ids = [fav.property_id for fav in favorites if fav.property_id]
            
            if property_ids:
                properties_query = db.session.query(
                    Property,
                    ResidentialComplex.name.label('complex_name'),
                    ResidentialComplex.cashback_rate,
                    ResidentialComplex.main_image.label('complex_image'),
                    Developer.name.label('developer_name')
                ).outerjoin(
                    ResidentialComplex, Property.complex_id == ResidentialComplex.id
                ).outerjoin(
                    Developer, Property.developer_id == Developer.id
                ).filter(
                    Property.inner_id.in_(property_ids),
                    Property.is_active == True
                ).all()
                
                properties_dict = {}
                for prop, complex_name, cashback_rate, complex_image, developer_name in properties_query:
                    rooms_text = f"{prop.rooms}-комн" if prop.rooms and prop.rooms > 0 else "Студия"
                    properties_dict[prop.inner_id] = {
                        'id': prop.inner_id,
                        'title': f"{rooms_text}, {prop.area} м², {prop.floor}/{prop.total_floors} эт.",
                        'complex': complex_name or 'ЖК не указан',
                        'district': prop.address or 'Адрес не указан',
                        'price': prop.price or 0,
                        'image': complex_image or prop.main_image or '/static/images/no-photo.jpg',
                        'cashback_rate': cashback_rate or 3.5,
                        'cashback_amount': int((prop.price or 0) * (cashback_rate or 3.5) / 100),
                        'developer': developer_name or 'Застройщик не указан',
                        'created_at': None,
                        'viewed': False
                    }
                
                for fav in favorites:
                    if fav.property_id in properties_dict:
                        prop_data = properties_dict[fav.property_id]
                        prop_data['created_at'] = fav.created_at.strftime('%d.%m.%Y в %H:%M') if fav.created_at else 'Недавно'
                        prop_data['viewed'] = fav.viewed if hasattr(fav, 'viewed') else False
                        properties_list.append(prop_data)
        
        # Получаем избранные ЖК
        complex_favorites = FavoriteComplex.query.filter_by(user_id=current_user.id).order_by(FavoriteComplex.created_at.desc()).all()
        
        complexes_list = []
        if complex_favorites:
            complex_ids_int = []
            for fav in complex_favorites:
                try:
                    complex_ids_int.append(int(fav.complex_id))
                except:
                    continue
            
            if complex_ids_int:
                complexes_query = db.session.query(
                    ResidentialComplex.id,
                    ResidentialComplex.name,
                    ResidentialComplex.address,
                    ResidentialComplex.main_image,
                    ResidentialComplex.cashback_rate,
                    District.name.label('district_name'),
                    Developer.name.label('developer_name')
                ).outerjoin(District, ResidentialComplex.district_id == District.id)\
                 .outerjoin(Developer, ResidentialComplex.developer_id == Developer.id)\
                 .filter(ResidentialComplex.id.in_(complex_ids_int)).all()
                
                complexes_dict = {}
                for rc in complexes_query:
                    complexes_dict[rc.id] = rc
                
                # Получаем цены
                from sqlalchemy import text, func
                prices_query = text("""
                    SELECT 
                        complex_id,
                        MIN(price) as min_price,
                        MAX(price) as max_price,
                        MIN(main_image) as first_image
                    FROM properties 
                    WHERE complex_id = ANY(:complex_ids) AND is_active = true
                    GROUP BY complex_id
                """)
                prices_result = db.session.execute(prices_query, {"complex_ids": complex_ids_int})
                prices_dict = {row[0]: {'min_price': row[1], 'max_price': row[2], 'first_image': row[3]} for row in prices_result}
                
                for fav in complex_favorites:
                    try:
                        complex_id_int = int(fav.complex_id)
                        rc = complexes_dict.get(complex_id_int)
                        
                        if rc:
                            complex_prices = prices_dict.get(complex_id_int, {})
                            image_url = rc.main_image or complex_prices.get('first_image') or fav.complex_image or ''
                            
                            complexes_list.append({
                                'id': str(complex_id_int),
                                'name': rc.name or fav.complex_name or 'ЖК',
                                'address': rc.address or fav.complex_address or 'Не указано',
                                'district': rc.district_name or fav.district or 'Не указано',
                                'developer': rc.developer_name or fav.developer_name or 'Не указано',
                                'image': image_url,
                                'cashback_rate': rc.cashback_rate or 5,
                                'min_price': complex_prices.get('min_price', fav.min_price or 0),
                                'max_price': complex_prices.get('max_price', fav.max_price or 0),
                                'url': f'/residential-complex/{complex_id_int}',
                                'status': 'В продаже',
                                'created_at': fav.created_at.strftime('%d.%m.%Y в %H:%M')
                            })
                    except Exception as e:
                        print(f"Error processing complex {fav.complex_id}: {e}")
                        continue
        
        return jsonify({
            'success': True,
            'properties': properties_list,
            'complexes': complexes_list,
            'total': len(properties_list) + len(complexes_list)
        })
    
    except Exception as e:
        print(f"Error in get_all_favorites: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# Manager Favorites API - Properties
@app.route('/api/manager/favorites', methods=['POST'])
@manager_required  
def manager_add_to_favorites():
    """Add property to manager's favorites"""
    from models import ManagerFavoriteProperty
    
    current_manager = current_user
    data = request.get_json()
    
    # Check if already in favorites
    existing = ManagerFavoriteProperty.query.filter_by(
        manager_id=current_manager.id,
        property_id=data.get('property_id')
    ).first()
    
    if existing:
        return jsonify({'success': False, 'error': 'Уже в избранном'})
    
    try:
        favorite = ManagerFavoriteProperty(
            manager_id=current_manager.id,
            property_id=data.get('property_id'),
            property_name=data.get('property_name', ''),
            property_type=data.get('property_type', ''),
            property_size=float(data.get('property_size', 0)),
            property_price=int(data.get('property_price', 0)),
            complex_name=data.get('complex_name', ''),
            developer_name=data.get('developer_name', ''),
            property_image=data.get('property_image'),
            property_url=data.get('property_url'),
            cashback_amount=int(data.get('cashback_amount', 0)),
            cashback_percent=float(data.get('cashback_percent', 0)),
            notes=data.get('notes', ''),
            recommended_for=data.get('recommended_for', '')
        )
        db.session.add(favorite)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Добавлено в избранное'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/favorites/<property_id>', methods=['DELETE'])
@manager_required
def manager_remove_from_favorites(property_id):
    """Remove property from manager's favorites"""
    from models import ManagerFavoriteProperty
    
    current_manager = current_user
    
    favorite = ManagerFavoriteProperty.query.filter_by(
        manager_id=current_manager.id,
        property_id=property_id
    ).first()
    
    if favorite:
        try:
            db.session.delete(favorite)
            db.session.commit()
            return jsonify({'success': True, 'message': 'Удалено из избранного'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
    
    return jsonify({'success': False, 'error': 'Объект не найден в избранном'}), 404

@app.route('/api/manager/favorites/toggle', methods=['POST'])
@manager_required
def manager_toggle_favorite():
    """Toggle favorite status for property"""
    from models import ManagerFavoriteProperty
    
    current_manager = current_user
    
    data = request.get_json()
    property_id = data.get('property_id')
    
    if not property_id:
        return jsonify({'success': False, 'error': 'property_id required'}), 400
    
    print(f"DEBUG: Manager favorites toggle called by manager {current_manager.id} for property {property_id}")
    print(f"DEBUG: Request data: {data}")
    
    # Check if already in favorites
    existing = ManagerFavoriteProperty.query.filter_by(
        manager_id=current_manager.id,
        property_id=property_id
    ).first()
    
    try:
        if existing:
            # Remove from favorites
            db.session.delete(existing)
            db.session.commit()
            return jsonify({'success': True, 'action': 'removed', 'is_favorite': False, 'message': 'Удалено из избранного'})
        else:
            # Add to favorites
            favorite = ManagerFavoriteProperty(
                manager_id=current_manager.id,
                property_id=property_id,
                property_name=data.get('property_name', ''),
                property_type=data.get('property_type', ''),
                property_size=float(data.get('property_size', 0)),
                property_price=int(data.get('property_price', 0)),
                complex_name=data.get('complex_name', ''),
                developer_name=data.get('developer_name', ''),
                property_image=data.get('property_image'),
                property_url=data.get('property_url'),
                cashback_amount=int(data.get('cashback_amount', 0)),
                cashback_percent=float(data.get('cashback_percent', 0)),
                notes=data.get('notes', ''),
                recommended_for=data.get('recommended_for', '')
            )
            db.session.add(favorite)
            db.session.commit()
            return jsonify({'success': True, 'action': 'added', 'is_favorite': True, 'message': 'Добавлено в избранное'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/favorites/count', methods=['GET'])
@manager_required  
def manager_get_favorites_count():
    """Get count of manager's favorites"""
    from models import ManagerFavoriteProperty, ManagerFavoriteComplex
    
    current_manager = current_user
    
    try:
        # DEBUG: Log which tables we're querying
        print(f"🔍 DEBUG: /api/manager/favorites/count called - querying MANAGER tables for manager {current_manager.id}")
        
        properties_count = ManagerFavoriteProperty.query.filter_by(manager_id=current_manager.id).count()
        complexes_count = ManagerFavoriteComplex.query.filter_by(manager_id=current_manager.id).count()
        
        print(f"✅ Manager favorites count: {properties_count} properties, {complexes_count} complexes from MANAGER tables")
        
        return jsonify({
            'success': True,
            'properties_count': properties_count,
            'complexes_count': complexes_count,
            'total_count': properties_count + complexes_count
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Note: Manager Complex Favorites endpoints already exist below - no duplicates needed

@app.route('/api/manager/favorites/list', methods=['GET'])
@manager_required  
def manager_get_favorites_list():
    """Get manager's favorite properties with full details - MIGRATED TO NORMALIZED SCHEMA"""
    from models import ManagerFavoriteProperty
    
    current_manager = current_user
    
    try:
        # DEBUG: Log which table we're querying
        print(f"🔍 DEBUG: /api/manager/favorites/list called - querying MANAGER_FAVORITE_PROPERTIES table for manager {current_manager.id}")
        
        favorites = db.session.query(ManagerFavoriteProperty).filter_by(manager_id=current_manager.id).order_by(ManagerFavoriteProperty.created_at.desc()).all()
        print(f"✅ Found {len(favorites)} favorites in MANAGER_FAVORITE_PROPERTIES for manager {current_manager.id}")
        
        if not favorites:
            return jsonify({'success': True, 'favorites': []})
        
        # ✅ ОПТИМИЗАЦИЯ: Загружаем только нужные объекты по ID из базы данных
        favorite_ids = [fav.property_id for fav in favorites if fav.property_id]
        if not favorite_ids:
            return jsonify({'success': True, 'favorites': []})
        
        # ✅ MIGRATED TO NORMALIZED SCHEMA: Используем SQL для получения только нужных объектов
        from sqlalchemy import text
        placeholders = ','.join([':id_{}'.format(i) for i in range(len(favorite_ids))])
        params = {'id_{}'.format(i): favorite_ids[i] for i in range(len(favorite_ids))}
        
        query = text(f"""
            SELECT p.inner_id, p.price, 
                   p.area AS object_area, 
                   p.rooms AS object_rooms, 
                   p.floor AS object_min_floor, 
                   p.total_floors AS object_max_floor,
                   p.address AS address_display_name, 
                   p.renovation_type AS renovation_display_name, 
                   p.gallery_images AS photos, 
                   d.name as developer_name, 
                   rc.name as complex_name,
                   p.latitude AS address_position_lat, 
                   p.longitude AS address_position_lon, 
                   p.description, 
                   dist.name AS address_locality_name,
                   rc.object_class_display_name AS complex_object_class_display_name, 
                   COALESCE(rc.cashback_rate, 5.0) as cashback_rate
            FROM properties p
            LEFT JOIN residential_complexes rc ON p.complex_id = rc.id
            LEFT JOIN developers d ON p.developer_id = d.id
            LEFT JOIN districts dist ON p.district_id = dist.id
            WHERE p.inner_id IN ({placeholders})
        """)
        
        result = db.session.execute(query, params)
        excel_properties = result.fetchall()
        
        # Создаем быстрый словарь для поиска
        properties_dict = {}
        for row in excel_properties:
            inner_id, price, area, rooms, min_floor, max_floor, address, renovation, photos, developer_name, complex_name, lat, lon, description, district_name, housing_class, cashback_rate = row
            properties_dict[str(inner_id)] = {
                'id': inner_id,
                'price': price or 0,
                'area': area or 0,
                'rooms': rooms or 0,
                'min_floor': min_floor or 1,
                'max_floor': max_floor or min_floor,
                'address': address,
                'renovation': renovation,
                'photos': photos,
                'developer_name': developer_name,
                'complex_name': complex_name,
                'lat': lat,
                'lon': lon,
                'description': description,
                'district': district_name,
                'housing_class': housing_class or 'Комфорт',
                'cashback_percent': cashback_rate or 5.0  # Реальный кэшбек от ЖК из админ панели
            }
        
        favorites_list = []
        for fav in favorites:
            property_data = properties_dict.get(str(fav.property_id))
            if not property_data:
                continue
            
            if property_data:
                print(f"Found property data for ID {fav.property_id}")
                # Add to favorites list with complete data including timestamp and manager fields
                # ✅ ИСПРАВЛЕННЫЙ MAPPING: используем правильные поля из SQL query
                rooms_text = ""
                if property_data.get('rooms') == 0:
                    rooms_text = "Студия"
                elif property_data.get('rooms'):
                    rooms_text = f"{property_data.get('rooms')}-комн."
                
                area_text = f"{property_data.get('area')} м²" if property_data.get('area') else ""
                floor_text = f"{property_data.get('min_floor')}-{property_data.get('max_floor')} эт." if property_data.get('min_floor') and property_data.get('max_floor') else ""
                
                # Формируем title как в других частях системы
                title_parts = [rooms_text, area_text, floor_text]
                title = ", ".join([part for part in title_parts if part]) or 'Квартира'
                
                # Парсим фото из JSON строки
                photos_list = []
                if property_data.get('photos'):
                    try:
                        import json
                        photos_list = json.loads(property_data.get('photos')) if isinstance(property_data.get('photos'), str) else property_data.get('photos')
                    except:
                        photos_list = []
                
                main_image = photos_list[0] if photos_list and isinstance(photos_list, list) else '/static/images/no-photo.jpg'
                
                # Рассчитываем реальный кэшбек
                cashback_percent = property_data.get('cashback_percent', 5.0)
                cashback_amount = int(property_data.get('price', 0) * cashback_percent / 100)
                
                favorites_list.append({
                    'id': property_data.get('id'),
                    'title': title,
                    'complex': property_data.get('complex_name') or 'ЖК не указан',  # ✅ complex_name из SQL
                    'district': property_data.get('address') or 'Адрес не указан',  # ✅ Показываем адрес квартиры
                    'price': property_data.get('price', 0),
                    'image': main_image,  # ✅ первое фото из photos array
                    'cashback_amount': cashback_amount,  # ✅ Реальный кэшбек из базы данных
                    'cashback_percent': cashback_percent,  # ✅ Процент кэшбека
                    'notes': fav.notes,
                    'recommended_for': fav.recommended_for,
                    'created_at': fav.created_at.strftime('%d.%m.%Y в %H:%M') if fav.created_at else 'Недавно',
                    # Дополнительные поля для полноты
                    'area': property_data.get('area'),
                    'rooms': property_data.get('rooms'),
                    'floor': property_data.get('min_floor'),
                    'address': property_data.get('address'),
                    'developer': property_data.get('developer_name'),
                    'housing_class': property_data.get('housing_class')  # ✅ Класс жилья
                })
            else:
                print(f"No property data found for ID {fav.property_id}")
                # Create fallback entry with minimal data
                favorites_list.append({
                    'id': fav.property_id,
                    'title': f'Объект #{fav.property_id}',
                    'complex': 'ЖК не найден',
                    'district': 'Данные обновляются...',
                    'price': 0,
                    'image': '/static/images/no-photo.jpg',
                    'cashback_amount': 0,
                    'notes': fav.notes,
                    'recommended_for': fav.recommended_for,
                    'created_at': fav.created_at.strftime('%d.%m.%Y в %H:%M') if fav.created_at else 'Недавно'
                })
        
        return jsonify({
            'success': True,
            'favorites': favorites_list
        })
    
    except Exception as e:
        import traceback
        print(f"❌ ERROR in manager_get_favorites_list: {str(e)}")
        print(f"❌ Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Manager Favorites API - Complexes
@app.route('/api/manager/complexes/favorites', methods=['POST'])
@manager_required  
def manager_add_complex_to_favorites():
    """Add residential complex to manager's favorites"""
    from models import ManagerFavoriteComplex
    
    current_manager = current_user
    data = request.get_json()
    
    complex_id = data.get('complex_id')
    complex_name = data.get('complex_name', 'ЖК')
    
    if not complex_id:
        return jsonify({'success': False, 'error': 'complex_id is required'}), 400
    
    # Check if already in favorites
    existing = ManagerFavoriteComplex.query.filter_by(
        manager_id=current_manager.id,
        complex_id=str(complex_id)
    ).first()
    
    if existing:
        return jsonify({'success': False, 'error': 'Complex already in favorites'}), 400
    
    try:
        # Create favorite complex record
        favorite = ManagerFavoriteComplex(
            manager_id=current_manager.id,
            complex_id=str(complex_id),
            complex_name=complex_name,
            developer_name=data.get('developer_name', ''),
            complex_address=data.get('address', ''),
            district=data.get('district', ''),
            min_price=data.get('min_price'),
            max_price=data.get('max_price'),
            complex_image=data.get('image', ''),
            complex_url=data.get('url', ''),
            status=data.get('status', 'В продаже'),
            notes=data.get('notes', ''),
            recommended_for=data.get('recommended_for', '')
        )
        
        db.session.add(favorite)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'ЖК добавлен в избранное'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/complexes/favorites/<complex_id>', methods=['DELETE'])
@manager_required
def manager_remove_complex_from_favorites(complex_id):
    """Remove residential complex from manager's favorites"""
    from models import ManagerFavoriteComplex
    
    current_manager = current_user
    
    favorite = ManagerFavoriteComplex.query.filter_by(
        manager_id=current_manager.id,
        complex_id=str(complex_id)
    ).first()
    
    if not favorite:
        return jsonify({'success': False, 'error': 'Complex not in favorites'}), 404
    
    try:
        db.session.delete(favorite)
        db.session.commit()
        return jsonify({'success': True, 'message': 'ЖК удален из избранного'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/complexes/favorites/toggle', methods=['POST'])
@manager_required
def manager_toggle_complex_favorite():
    """Toggle favorite status for residential complex"""
    from models import ManagerFavoriteComplex
    
    current_manager = current_user
    
    data = request.get_json()
    complex_id = data.get('complex_id')
    
    if not complex_id:
        return jsonify({'success': False, 'error': 'complex_id is required'}), 400
    
    try:
        existing = ManagerFavoriteComplex.query.filter_by(
            manager_id=current_manager.id,
            complex_id=str(complex_id)
        ).first()
        
        if existing:
            # Remove from favorites
            db.session.delete(existing)
            db.session.commit()
            return jsonify({'success': True, 'favorited': False, 'message': 'ЖК удален из избранного'})
        else:
            # ✅ ИСПРАВЛЕНИЕ: Загружаем реальные данные ЖК из базы данных
            real_complex_name = 'ЖК без названия'
            real_developer_name = 'Застройщик не указан'
            real_address = 'Адрес не указан'
            real_district = 'Район не указан'
            real_min_price = 0
            real_max_price = 0
            real_image = '/static/images/no-photo.jpg'
            real_status = 'В продаже'
            
            try:
                # ✅ MIGRATED: Загружаем данные из normalized schema по complex_id
                from sqlalchemy import text
                complex_query = text("""
                    SELECT 
                        rc.name as complex_name,
                        d.name as developer_name,
                        rc.address AS address_display_name,
                        dist.name AS address_locality_name,
                        MIN(p.price) as min_price,
                        MAX(p.price) as max_price,
                        (SELECT p2.gallery_images FROM properties p2 
                         WHERE p2.complex_id = rc.id 
                         AND p2.gallery_images IS NOT NULL 
                         ORDER BY p2.price DESC LIMIT 1) AS photos
                    FROM residential_complexes rc
                    LEFT JOIN developers d ON rc.developer_id = d.id
                    LEFT JOIN districts dist ON rc.district_id = dist.id
                    LEFT JOIN properties p ON p.complex_id = rc.id
                    WHERE rc.id = :complex_id
                    GROUP BY rc.name, d.name, rc.address, dist.name, rc.id
                    LIMIT 1
                """)
                
                result = db.session.execute(complex_query, {'complex_id': str(complex_id)})
                row = result.fetchone()
                
                if row:
                    real_complex_name = row[0] or real_complex_name
                    real_developer_name = row[1] or real_developer_name  
                    real_address = row[2] or real_address
                    real_district = row[3] or real_district
                    real_min_price = int(row[4]) if row[4] else 0
                    real_max_price = int(row[5]) if row[5] else 0
                    
                    # Парсим фото из JSON
                    if row[6]:
                        try:
                            import json
                            photos = json.loads(row[6]) if isinstance(row[6], str) else row[6]
                            if photos and isinstance(photos, list) and len(photos) > 0:
                                real_image = photos[0]  # Первое фото как основное
                        except Exception as photo_error:
                            print(f"DEBUG: Error parsing photos for property {prop.property_id}: {photo_error}")
                    
                    # Определяем статус по году сдачи
                    from datetime import datetime
                    current_year = datetime.now().year
                    
                    try:
                        # ✅ MIGRATED: Запрос статуса из normalized schema
                        status_query = text("""
                            SELECT end_build_year AS complex_building_end_build_year
                            FROM residential_complexes 
                            WHERE id = :complex_id 
                            AND end_build_year IS NOT NULL
                            LIMIT 1
                        """)
                        status_result = db.session.execute(status_query, {'complex_id': str(complex_id)})
                        status_row = status_result.fetchone()
                        
                        if status_row and status_row[0]:
                            build_year = int(status_row[0])
                            real_status = 'Сдан' if build_year <= current_year else 'Строится'
                    except:
                        pass
                        
            except Exception as e:
                print(f"Error loading real complex data for {complex_id}: {e}")
                # Продолжаем с fallback значениями
                pass
            
            # Add to favorites with REAL DATA
            favorite = ManagerFavoriteComplex(
                manager_id=current_manager.id,
                complex_id=str(complex_id),
                complex_name=real_complex_name,
                developer_name=real_developer_name,
                complex_address=real_address,
                district=real_district,
                min_price=real_min_price,
                max_price=real_max_price,
                complex_image=real_image,
                complex_url=data.get('url', f'/zk/{complex_id}'),
                status=real_status,
                notes=data.get('notes', ''),
                recommended_for=data.get('recommended_for', '')
            )
            
            db.session.add(favorite)
            db.session.commit()
            return jsonify({'success': True, 'favorited': True, 'message': 'ЖК добавлен в избранное'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/complexes/favorites/list', methods=['GET'])
@manager_required
def manager_get_complex_favorites_list():
    """Get manager's favorite complexes with full details"""
    from models import ManagerFavoriteComplex, ResidentialComplex, Developer, District
    from sqlalchemy.orm import joinedload, selectinload
    from sqlalchemy import or_
    
    current_manager = current_user
    
    try:
        # Загружаем избранное без broken relationship
        favorites = db.session.query(ManagerFavoriteComplex)\
            .filter_by(manager_id=current_manager.id)\
            .order_by(ManagerFavoriteComplex.created_at.desc()).all()
        
        # Собираем ID комплексов для batch-загрузки
        complex_ids_str = [fav.complex_id for fav in favorites if fav.complex_id]
        complex_ids_int = []
        for cid in complex_ids_str:
            try:
                complex_ids_int.append(int(cid))
            except (ValueError, TypeError):
                pass
        
        # BYPASSING broken favorites data - use direct ResidentialComplex lookup since FK is broken
        # Get complex names from ResidentialComplex table using favorites complex_id (if exists)
        complex_names = []
        for fav in favorites:
            if fav.complex_id:
                try:
                    complex_int_id = int(fav.complex_id)
                    rc = ResidentialComplex.query.get(complex_int_id)
                    if rc and rc.name:
                        complex_names.append(rc.name)
                except (ValueError, TypeError):
                    pass
        
        # Fallback: If no matches found, use all residential complexes for demo
        if not complex_names:
            all_complexes = ResidentialComplex.query.limit(10).all()
            complex_names = [rc.name for rc in all_complexes if rc.name]
        
        # ENSURE we include ALL favorite complex names even if not in excel_data
        # This prevents missing complexes in comparison
        for fav in favorites:
            if fav.complex_id:
                try:
                    complex_int_id = int(fav.complex_id)
                    rc = ResidentialComplex.query.get(complex_int_id)
                    if rc and rc.name and rc.name not in complex_names:
                        complex_names.append(rc.name)
                        print(f"DEBUG: Added missing favorite complex to search: {rc.name}")
                except (ValueError, TypeError):
                    pass
        excel_data = {}
        
        if complex_names:
            # SQL aggregation with proper expanding bind and name normalization
            from sqlalchemy import text, bindparam
            
            # Normalize names for matching
            normalized_names = tuple({n.strip().lower().replace('«','"').replace('»','"') 
                                    for n in complex_names if n})
            
            stmt = text("""
            SELECT 
                rc.name as complex_name,
                MIN(p.price) as min_price,
                MAX(p.price) as max_price,
                COUNT(p.id) as apartments_count,
                rc.address AS address_display_name,
                (SELECT p2.gallery_images FROM properties p2 
                 WHERE p2.complex_id = rc.id AND p2.gallery_images IS NOT NULL 
                 LIMIT 1) AS photos
            FROM residential_complexes rc
            LEFT JOIN properties p ON p.complex_id = rc.id
            WHERE lower(rc.name) IN :names
            GROUP BY rc.id, rc.name, rc.address
            """).bindparams(bindparam('names', expanding=True))
            
            result = db.session.execute(stmt, {'names': normalized_names})
            for row in result:
                # Store with original complex name for mapping
                for original_name in complex_names:
                    if original_name and original_name.strip().lower().replace('«','"').replace('»','"') == row.complex_name.lower():
                        excel_data[original_name] = {
                            'min_price': int(row.min_price) if row.min_price else 0,
                            'max_price': int(row.max_price) if row.max_price else 0,
                            'apartments_count': int(row.apartments_count) if row.apartments_count else 0,
                            'sample_address': row.address_display_name or '',
                            'photos': row.photos
                        }
                        break
            
            print(f"DEBUG: Searched {len(normalized_names)} names, found {len(excel_data)} matches")
            print(f"DEBUG: excel_data keys: {list(excel_data.keys())[:2]}")  # First 2 keys
        
        
        # Загружаем все комплексы сразу с joined данными  
        complexes_data = {}
        if complex_ids_str:
            complexes_query = db.session.query(ResidentialComplex)\
                .options(
                    joinedload(ResidentialComplex.developer), 
                    joinedload(ResidentialComplex.district),
                    selectinload(ResidentialComplex.buildings)
                )\
                .filter(or_(
                    ResidentialComplex.id.in_(complex_ids_int),
                    ResidentialComplex.complex_id.in_(complex_ids_str)
                ))
            
            for complex_data in complexes_query:
                complexes_data[str(complex_data.id)] = complex_data
                if complex_data.complex_id:
                    complexes_data[str(complex_data.complex_id)] = complex_data
        
        favorites_list = []
        for fav in favorites:
            # ✅ ИСПРАВЛЕНИЕ: Ищем данные по ResidentialComplex таблице и excel_properties
            real_complex_name = 'ЖК без названия'
            real_developer_name = 'Застройщик не указан'
            real_address = 'Адрес не указан'
            real_district = 'Район не указан'
            real_min_price = 0
            real_max_price = 0
            real_image = '/static/images/no-photo.jpg'
            real_status = 'В продаже'
            real_apartments_count = 0
            real_buildings_count = 1
            real_delivery_date = 'Не указано'
            
                # ✅ ИСПРАВЛЕНИЕ: Используем тот же SQL что и /residential-complexes для поиска по динамическим ID
            try:
                complex_db = None
                print(f"DEBUG: Searching for complex with fav.complex_id: {fav.complex_id}")
                
                if fav.complex_id:
                    # Сначала пробуем найти в residential_complexes (для старых записей)
                    try:
                        complex_int_id = int(fav.complex_id)
                        complex_db = ResidentialComplex.query.get(complex_int_id)
                        print(f"DEBUG: Found by id {complex_int_id}: {complex_db.name if complex_db else 'None'}")
                    except (ValueError, TypeError):
                        pass
                
                if complex_db and complex_db.name:
                    # Найдено в residential_complexes - используем эти данные
                    real_complex_name = complex_db.name
                    real_developer_name = complex_db.developer.name if complex_db.developer else real_developer_name
                    real_district = complex_db.district.name if complex_db.district else real_district
                    # Адрес берем из sales_address или оставляем placeholder для последующей загрузки из excel_properties
                    real_address = complex_db.sales_address if hasattr(complex_db, 'sales_address') and complex_db.sales_address else real_address
                    real_image = complex_db.main_image if hasattr(complex_db, 'main_image') and complex_db.main_image else real_image
                    print(f"DEBUG: ✅ Using residential_complexes data: {real_complex_name}")
                else:
                    # ✅ MIGRATED: Complex not found in residential_complexes table
                    # With normalized schema, all complexes should exist in residential_complexes
                    # If not found, will use default values set above
                    print(f"DEBUG: ⚠️ Complex with ID {fav.complex_id} not found in residential_complexes")
                
                # ✅ MIGRATED: Дополнительный поиск данных в normalized schema по названию ЖК (всегда выполняется)
                if real_complex_name != 'ЖК без названия':
                    from sqlalchemy import text
                    excel_query = text("""
                        SELECT 
                            MIN(p.price) as min_price,
                            MAX(p.price) as max_price,
                            COUNT(p.id) as apartments_count,
                            COUNT(DISTINCT b.id) as buildings_count,
                            rc.end_build_year AS complex_building_end_build_year,
                            rc.end_build_quarter AS complex_building_end_build_quarter,
                            (SELECT p2.gallery_images FROM properties p2 
                             WHERE p2.complex_id = rc.id AND p2.gallery_images IS NOT NULL 
                             LIMIT 1) AS photos,
                            rc.address AS address_display_name,
                            dist.name AS address_locality_name,
                            d.name as developer_name
                        FROM residential_complexes rc
                        LEFT JOIN properties p ON p.complex_id = rc.id
                        LEFT JOIN buildings b ON b.complex_id = rc.id
                        LEFT JOIN developers d ON rc.developer_id = d.id
                        LEFT JOIN districts dist ON rc.district_id = dist.id
                        WHERE rc.name = :complex_name
                        GROUP BY rc.id, rc.end_build_year, rc.end_build_quarter, rc.address, dist.name, d.name
                        LIMIT 1
                    """)
                    
                    # Use exact match on complex name (no LIKE pattern needed)
                    print(f"DEBUG: Searching normalized schema for complex: {real_complex_name}")
                    result = db.session.execute(excel_query, {'complex_name': real_complex_name})
                    row = result.fetchone()
                    
                    print(f"DEBUG: Normalized schema query result - found: {row is not None}, has price: {row[0] if row else 'N/A'}")
                    if row:
                        print(f"DEBUG: Normalized data - address: {row[7]}, district: {row[8]}, developer: {row[9]}")
                    
                    if row and row[0]:  # Если найдены данные
                        real_min_price = int(row[0]) if row[0] else 0
                        real_max_price = int(row[1]) if row[1] else 0
                        real_apartments_count = int(row[2]) if row[2] else 0
                        real_buildings_count = max(int(row[3]) if row[3] else 1, 1)
                        
                        # Парсим фото из JSON
                        if row[6]:
                            try:
                                import json
                                photos = json.loads(row[6]) if isinstance(row[6], str) else row[6]
                                if photos and isinstance(photos, list) and len(photos) > 0:
                                    # Берем фото ЖК, пропуская интерьеры квартир
                                    start_index = min(len(photos) // 4, 5) if len(photos) > 8 else 1
                                    real_image = photos[start_index] if len(photos) > start_index else photos[0]
                            except Exception as photo_error:
                                print(f"DEBUG: Error parsing photos for property {prop.property_id}: {photo_error}")
                        
                        # Обновляем адрес, район и застройщика из normalized schema если они не были заполнены
                        if row[7] and (not real_address or real_address == 'Адрес не указан'):
                            real_address = row[7]
                            print(f"DEBUG: Updated address from normalized schema: {real_address}")
                        if row[8] and (not real_district or real_district == 'Район не указан'):
                            real_district = row[8]
                            print(f"DEBUG: Updated district from normalized schema: {real_district}")
                        if row[9] and (not real_developer_name or real_developer_name == 'Застройщик не указан'):
                            real_developer_name = row[9]
                            print(f"DEBUG: Updated developer from normalized schema: {real_developer_name}")
                        
                        # Определяем статус и дату сдачи
                        if row[4] and row[5]:  # end_build_year и end_build_quarter
                            build_year = int(row[4])
                            build_quarter = int(row[5])
                            quarter_names = {1: 'I', 2: 'II', 3: 'III', 4: 'IV'}
                            quarter = quarter_names.get(build_quarter, build_quarter)
                            real_delivery_date = f"{quarter} кв. {build_year} г."
                            
                            from datetime import datetime
                            current_year = datetime.now().year
                            real_status = 'Сдан' if build_year <= current_year else 'Строится'
                        elif row[4]:  # только год
                            build_year = int(row[4])
                            real_delivery_date = f"{build_year} г."
                            from datetime import datetime
                            real_status = 'Сдан' if build_year <= datetime.now().year else 'Строится'
                                
            except Exception as e:
                print(f"Error loading complex data for {fav.complex_id}: {e}")
                pass
            
            # Ищем полные данные ЖК (ResidentialComplex)
            complex_data = complexes_data.get(str(fav.complex_id))
            
            # Безопасный способ создания slug с fallback
            try:
                url = f"/zk/{create_slug(real_complex_name)}" if real_complex_name and real_complex_name != 'ЖК без названия' else '#'
            except:
                url = '#'
            
            # ✅ Подтягиваем реальный кэшбек из ResidentialComplex или используем значение по умолчанию
            real_cashback_rate = 5.0
            real_housing_class = 'Комфорт'
            if complex_data:
                real_cashback_rate = complex_data.cashback_rate if complex_data.cashback_rate else 5.0
                real_housing_class = complex_data.object_class_display_name if complex_data.object_class_display_name else 'Комфорт'
            
            # ✅ ИСПОЛЬЗУЕМ ТОЛЬКО РЕАЛЬНЫЕ ДАННЫЕ - игнорируем старые placeholder
            favorites_list.append({
                'id': str(fav.complex_id),
                'name': real_complex_name,
                'developer': real_developer_name,
                'address': real_address,
                'district': real_housing_class,  # ✅ Показываем класс жилья вместо района
                'housing_class': real_housing_class,  # ✅ Класс жилья
                'min_price': real_min_price,
                'max_price': real_max_price,
                'apartments_count': real_apartments_count,
                'buildings_count': real_buildings_count,
                'image': real_image,
                'url': url,
                'status': real_status,
                'delivery_date': real_delivery_date,
                'notes': fav.notes or '',
                'recommended_for': fav.recommended_for or '',
                'created_at': fav.created_at.strftime('%d.%m.%Y в %H:%M') if fav.created_at else 'Недавно',
                'cashback_rate': real_cashback_rate  # ✅ Реальный кэшбек из базы данных
            })
        
        print(f"Found {len(favorites)} favorite complexes for manager {current_manager.id}")
        if favorites:
            print(f"First complex: {favorites_list[0]}")
        
        return jsonify({
            'success': True,
            'complexes': favorites_list,
            'favorite_complexes': favorites_list,  # добавить alias
            'favorites': favorites_list  # добавить alias
        })
    
    except Exception as e:
        print(f"Error loading favorite complexes: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# User Management Routes
@app.route('/admin/users')
@admin_required
def admin_users():
    """User management page"""
    try:
        from models import Admin, User
        
        # ИСПРАВЛЕНО: Используем Flask-Login current_user
        current_admin = current_user
        
        page = request.args.get('page', 1, type=int)
        search = request.args.get('search', '', type=str)
        status = request.args.get('status', '', type=str)
        
        query = User.query
        
        if search:
            query = query.filter(User.email.contains(search) | User.full_name.contains(search))
        
        if status == 'active':
            query = query.filter_by(is_active=True)
        elif status == 'inactive':
            query = query.filter_by(is_active=False)
        elif status == 'verified':
            query = query.filter_by(is_verified=True)
        elif status == 'unverified':
            query = query.filter_by(is_verified=False)
        
        users = query.order_by(User.created_at.desc()).paginate(
            page=page, per_page=20, error_out=False
        )
        
        # Обработка пользователей для безопасного отображения дат
        from datetime import datetime
        for user in users.items:
            if user.created_at is None:
                # Устанавливаем текущую дату для пользователей без даты создания
                user.created_at = datetime.now()
        
        print(f"DEBUG: Loading admin_users page - Found {users.total} users")
        
        return render_template('admin/users.html', 
                             admin=current_admin, 
                             users=users,
                             search=search,
                             status=status)
                             
    except Exception as e:
        print(f"ERROR in admin_users: {str(e)}")
        import traceback
        traceback.print_exc()
        flash(f'Ошибка загрузки страницы пользователей: {str(e)}', 'error')
        return redirect(url_for('admin_dashboard'))

@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_user(user_id):
    """Edit user details"""
    from models import Admin, User, Manager
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    user = User.query.get_or_404(user_id)
    managers = Manager.query.filter_by(is_active=True).all()
    
    if request.method == 'POST':
        user.email = request.form.get('email')
        user.full_name = request.form.get('full_name')
        user.phone = request.form.get('phone')
        user.client_status = request.form.get('client_status')
        user.client_notes = request.form.get('client_notes')
        user.is_active = 'is_active' in request.form
        user.is_verified = 'is_verified' in request.form
        
        assigned_manager_id = request.form.get('assigned_manager_id')
        if assigned_manager_id and assigned_manager_id.isdigit():
            user.assigned_manager_id = int(assigned_manager_id)
        else:
            user.assigned_manager_id = None
        
        try:
            db.session.commit()
            flash('Пользователь успешно обновлен', 'success')
            return redirect(url_for('admin_users'))
        except Exception as e:
            db.session.rollback()
            flash('Ошибка при обновлении пользователя', 'error')
    
    return render_template('admin/edit_user.html', 
                         admin=current_admin, 
                         user=user,
                         managers=managers)

@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    """Delete user"""
    from models import User
    
    user = User.query.get_or_404(user_id)
    
    try:
        db.session.delete(user)
        db.session.commit()
        flash('Пользователь успешно удален', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при удалении пользователя', 'error')
    
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def admin_reset_user_password(user_id):
    """Admin reset user password - sends reset email"""
    from models import User
    
    user = User.query.get_or_404(user_id)
    
    try:
        # Generate reset token and send email
        token = user.generate_verification_token()
        db.session.commit()
        
        try:
            from email_service import send_password_reset_email
            send_password_reset_email(user, token)
            flash(f'Письмо сброса пароля отправлено на {user.email}', 'success')
        except Exception as e:
            print(f"Error sending password reset email: {e}")
            flash(f'Ошибка отправки письма: {str(e)}', 'error')
            
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при генерации токена: {str(e)}', 'error')
        
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/toggle-role', methods=['POST'])
@admin_required 
def admin_toggle_user_role(user_id):
    """Admin change user role"""
    from models import User
    
    user = User.query.get_or_404(user_id)
    new_role = request.form.get('role')
    
    # Validate role
    valid_roles = ['buyer', 'manager', 'admin', None]
    if new_role == '':
        new_role = None
    
    if new_role not in valid_roles:
        flash('Неверная роль пользователя', 'error')
        return redirect(url_for('admin_users'))
    
    try:
        old_role = user.role or 'Не назначена'
        user.role = new_role
        db.session.commit()
        
        new_role_display = new_role or 'Не назначена'
        flash(f'Роль пользователя {user.email} изменена с "{old_role}" на "{new_role_display}"', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при изменении роли: {str(e)}', 'error')
        
    return redirect(url_for('admin_users'))

@app.route('/admin/users/bulk-role', methods=['POST'])
@admin_required
def admin_bulk_assign_role():
    """Bulk assign role to users"""
    from models import User
    
    user_ids = request.form.getlist('user_ids')
    new_role = request.form.get('role')
    
    if new_role == '':
        new_role = None
    
    valid_roles = ['buyer', 'manager', 'admin', None]
    if new_role not in valid_roles:
        flash('Неверная роль пользователя', 'error')
        return redirect(url_for('admin_users'))
    
    if not user_ids:
        flash('Не выбраны пользователи', 'error')
        return redirect(url_for('admin_users'))
    
    try:
        users = User.query.filter(User.id.in_(user_ids)).all()
        role_display = new_role or 'Не назначена'
        updated_count = 0
        
        for user in users:
            user.role = new_role
            updated_count += 1
        
        db.session.commit()
        flash(f'Роль "{role_display}" назначена для {updated_count} пользователей', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при массовом назначении роли: {str(e)}', 'error')
        
    return redirect(url_for('admin_users'))

@app.route('/admin/users/bulk-status', methods=['POST'])
@admin_required
def admin_bulk_toggle_status():
    """Bulk toggle user status"""
    from models import User
    
    user_ids = request.form.getlist('user_ids')
    
    if not user_ids:
        flash('Не выбраны пользователи', 'error')
        return redirect(url_for('admin_users'))
    
    try:
        users = User.query.filter(User.id.in_(user_ids)).all()
        activated_count = 0
        deactivated_count = 0
        
        for user in users:
            if user.is_active:
                user.is_active = False
                deactivated_count += 1
            else:
                user.is_active = True
                activated_count += 1
        
        db.session.commit()
        
        if activated_count > 0 and deactivated_count > 0:
            flash(f'Активировано: {activated_count}, деактивировано: {deactivated_count} пользователей', 'success')
        elif activated_count > 0:
            flash(f'Активировано {activated_count} пользователей', 'success')
        else:
            flash(f'Деактивировано {deactivated_count} пользователей', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при массовом изменении статуса: {str(e)}', 'error')
        
    return redirect(url_for('admin_users'))

@app.route('/admin/users/bulk-delete', methods=['POST'])
@admin_required
def admin_bulk_delete_users():
    """Bulk delete users"""
    from models import User
    
    user_ids = request.form.getlist('user_ids')
    
    if not user_ids:
        flash('Не выбраны пользователи', 'error')
        return redirect(url_for('admin_users'))
    
    try:
        users = User.query.filter(User.id.in_(user_ids)).all()
        deleted_count = len(users)
        
        for user in users:
            db.session.delete(user)
        
        db.session.commit()
        flash(f'Удалено {deleted_count} пользователей', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при массовом удалении: {str(e)}', 'error')
        
    return redirect(url_for('admin_users'))

@app.route('/admin/users/create', methods=['GET', 'POST'])
@admin_required
def admin_create_user():
    """Create new user by admin"""
    from models import Admin, User, Manager
    import re
    import secrets
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    managers = Manager.query.filter_by(is_active=True).all()
    
    if request.method == 'POST':
        try:
            # Validate required fields
            full_name = request.form.get('full_name', '').strip()
            email = request.form.get('email', '').strip().lower()
            phone = request.form.get('phone', '').strip()
            
            if not all([full_name, email, phone]):
                flash('Заполните все обязательные поля', 'error')
                return render_template('admin/create_user.html', 
                                     admin=current_admin, 
                                     managers=managers)
            
            # Validate email format
            if not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email):
                flash('Некорректный формат email', 'error')
                return render_template('admin/create_user.html', 
                                     admin=current_admin, 
                                     managers=managers)
            
            # Check if user already exists
            existing_user = User.query.filter(
                (User.email == email) | (User.phone == phone)
            ).first()
            
            if existing_user:
                flash('Пользователь с таким email или телефоном уже существует', 'error')
                return render_template('admin/create_user.html', 
                                     admin=current_admin, 
                                     managers=managers)
            
            # Clean phone number
            phone_clean = re.sub(r'[^\d]', '', phone)
            if len(phone_clean) == 11 and phone_clean.startswith('8'):
                phone_clean = '7' + phone_clean[1:]
            elif len(phone_clean) == 10:
                phone_clean = '7' + phone_clean
            
            if len(phone_clean) != 11 or not phone_clean.startswith('7'):
                flash('Некорректный формат телефона', 'error')
                return render_template('admin/create_user.html', 
                                     admin=current_admin, 
                                     managers=managers)
            
            # Generate temporary password
            temp_password = secrets.token_urlsafe(12)
            
            # Create user
            user = User(
                email=email,
                full_name=full_name,
                phone=phone_clean,
                client_status=request.form.get('client_status', 'Новый'),
                client_notes=request.form.get('client_notes', ''),
                is_active='is_active' in request.form,
                is_verified='is_verified' in request.form,
                temp_password_hash=temp_password,  # Store temp password for sending
                created_by_admin=True
            )
            
            # Set assigned manager
            assigned_manager_id = request.form.get('assigned_manager_id')
            if assigned_manager_id and assigned_manager_id.isdigit():
                user.assigned_manager_id = int(assigned_manager_id)
            
            # Set temporary password
            user.set_password(temp_password)
            
            db.session.add(user)
            db.session.commit()
            
            print(f"DEBUG: Successfully created user {user.id}: {user.full_name} by admin")
            
            # Send credentials if requested
            if 'send_credentials' in request.form:
                try:
                    from email_service import send_email_smtp
                    from sms_service import send_sms
                    
                    # Prepare email content
                    subject = "Ваш аккаунт создан в InBack.ru - Данные для входа"
                    email_content = f"""Здравствуйте, {full_name}!

Для вас создан аккаунт в системе InBack.ru.

Данные для входа:
• Email: {email}
• Временный пароль: {temp_password}

Войдите в систему по адресу: https://inback.ru/login
При первом входе вам будет предложено установить собственный пароль.

С уважением,
Команда InBack.ru"""
                    
                    # Send email using HTML template
                    send_email_smtp(
                        to_email=email,
                        subject=subject,
                        template_name='emails/user_credentials.html',
                        user_name=full_name,
                        email=email,
                        temp_password=temp_password,
                        login_url='https://inback.ru/login'
                    )
                    
                    # Send SMS
                    sms_message = f"InBack.ru: Ваш аккаунт создан. Логин: {email}, Пароль: {temp_password}. Войти: https://inback.ru/login"
                    send_sms(phone_clean, sms_message)
                    
                    flash(f'Пользователь {full_name} успешно создан. Данные для входа отправлены на email и SMS.', 'success')
                    
                except Exception as e:
                    print(f"Error sending credentials: {str(e)}")
                    flash(f'Пользователь создан, но не удалось отправить данные для входа: {str(e)}', 'warning')
            else:
                flash(f'Пользователь {full_name} успешно создан.', 'success')
            
            return redirect(url_for('admin_users'))
            
        except Exception as e:
            db.session.rollback()
            print(f"Error creating user: {str(e)}")
            flash(f'Ошибка при создании пользователя: {str(e)}', 'error')
            return render_template('admin/create_user.html', 
                                 admin=current_admin, 
                                 managers=managers)
    
    return render_template('admin/create_user.html', 
                         admin=current_admin, 
                         managers=managers)

@app.route('/admin/users/<int:user_id>/toggle-status', methods=['POST'])
@admin_required
def admin_toggle_user_status(user_id):
    """Toggle user active status"""
    from models import User
    
    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    
    try:
        db.session.commit()
        status = 'активирован' if user.is_active else 'заблокирован'
        flash(f'Пользователь {status}', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при изменении статуса пользователя', 'error')
    
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/verify', methods=['POST'])
@admin_required
def admin_verify_user(user_id):
    """Verify user account manually"""
    from models import User, Admin
    
    try:
        user = User.query.get_or_404(user_id)
        
        # Проверяем, что пользователь не верифицирован
        if user.is_verified:
            flash(f'Пользователь {user.full_name} уже подтвержден', 'info')
            return redirect(url_for('admin_users'))
        
        # Подтверждаем пользователя
        user.is_verified = True
        
        # ИСПРАВЛЕНО: Используем Flask-Login current_user
        # Логирование действия админа
        current_admin = current_user
        admin_name = current_admin.full_name if current_admin else 'Неизвестный админ'
        
        print(f"ADMIN ACTION: {admin_name} (ID: {current_admin.id}) verified user {user.full_name} (ID: {user.id}, Email: {user.email})")
        
        db.session.commit()
        flash(f'Аккаунт пользователя {user.full_name} успешно подтвержден', 'success')
        
        # Поддержка AJAX запросов
        if request.headers.get('Content-Type') == 'application/json' or request.is_json:
            return jsonify({
                'success': True,
                'message': f'Аккаунт пользователя {user.full_name} подтвержден',
                'user_id': user.id,
                'verified': True
            })
            
    except Exception as e:
        db.session.rollback()
        error_message = f'Ошибка при подтверждении пользователя: {str(e)}'
        print(f"Error verifying user {user_id}: {str(e)}")
        flash(error_message, 'error')
        
        if request.headers.get('Content-Type') == 'application/json' or request.is_json:
            return jsonify({
                'success': False,
                'error': error_message
            }), 500
    
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/unverify', methods=['POST'])
@admin_required 
def admin_unverify_user(user_id):
    """Unverify user account manually"""
    from models import User, Admin
    
    try:
        user = User.query.get_or_404(user_id)
        
        # Проверяем, что пользователь верифицирован
        if not user.is_verified:
            flash(f'Пользователь {user.full_name} уже не подтвержден', 'info')
            return redirect(url_for('admin_users'))
        
        # Отменяем подтверждение
        user.is_verified = False
        
        # ИСПРАВЛЕНО: Используем Flask-Login current_user
        # Логирование действия админа
        current_admin = current_user
        admin_name = current_admin.full_name if current_admin else 'Неизвестный админ'
        
        print(f"ADMIN ACTION: {admin_name} (ID: {current_admin.id}) unverified user {user.full_name} (ID: {user.id}, Email: {user.email})")
        
        db.session.commit()
        flash(f'Подтверждение аккаунта пользователя {user.full_name} отменено', 'warning')
        
        # Поддержка AJAX запросов
        if request.headers.get('Content-Type') == 'application/json' or request.is_json:
            return jsonify({
                'success': True,
                'message': f'Подтверждение аккаунта {user.full_name} отменено',
                'user_id': user.id,
                'verified': False
            })
            
    except Exception as e:
        db.session.rollback()
        error_message = f'Ошибка при отмене подтверждения: {str(e)}'
        print(f"Error unverifying user {user_id}: {str(e)}")
        flash(error_message, 'error')
        
        if request.headers.get('Content-Type') == 'application/json' or request.is_json:
            return jsonify({
                'success': False,
                'error': error_message
            }), 500
    
    return redirect(url_for('admin_users'))

# Manager Management Routes
@app.route('/admin/managers')
@admin_required
def admin_managers():
    """Manager management page"""
    from models import Admin, Manager
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '', type=str)
    status = request.args.get('status', '', type=str)
    
    query = Manager.query
    
    if search:
        query = query.filter(Manager.email.contains(search) | Manager.first_name.contains(search) | Manager.last_name.contains(search))
    
    if status == 'active':
        query = query.filter_by(is_active=True)
    elif status == 'inactive':
        query = query.filter_by(is_active=False)
    
    managers = query.order_by(Manager.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    return render_template('admin/managers.html', 
                         admin=current_admin, 
                         managers=managers,
                         search=search,
                         status=status)

@app.route('/admin/managers/<int:manager_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_manager(manager_id):
    """Edit manager details"""
    from models import Admin, Manager
    
    try:
        # ИСПРАВЛЕНО: Используем Flask-Login current_user
        current_admin = current_user
        manager = Manager.query.get(manager_id)
        
        if not manager:
            flash(f'Менеджер с ID {manager_id} не найден', 'error')
            return redirect(url_for('admin_managers'))
            
        print(f"DEBUG: Found manager {manager_id}: {manager.email}")
    except Exception as e:
        print(f"ERROR in admin_edit_manager: {e}")
        flash('Ошибка при загрузке менеджера', 'error')
        return redirect(url_for('admin_managers'))
    
    if request.method == 'POST':
        manager.email = request.form.get('email')
        manager.first_name = request.form.get('first_name')
        manager.last_name = request.form.get('last_name')
        manager.phone = request.form.get('phone')
        manager.position = request.form.get('position')
        manager.is_active = 'is_active' in request.form
        
        new_password = request.form.get('new_password')
        if new_password:
            manager.set_password(new_password)
        
        try:
            db.session.commit()
            flash('Менеджер успешно обновлен', 'success')
            return redirect(url_for('admin_managers'))
        except Exception as e:
            db.session.rollback()
            flash('Ошибка при обновлении менеджера', 'error')
    
    from datetime import datetime
    
    return render_template('admin/edit_manager.html', 
                         admin=current_admin, 
                         manager=manager,
                         current_date=datetime.utcnow())



# Blog Management Routes
@app.route('/admin/blog')
@admin_required
def admin_blog():
    """Blog management page"""
    from models import Admin, BlogPost
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '', type=str)
    status = request.args.get('status', '', type=str)
    category_id = request.args.get('category_id', '', type=str)
    
    query = BlogPost.query
    
    if search:
        query = query.filter(BlogPost.title.contains(search) | BlogPost.content.contains(search))
    
    if status:
        query = query.filter_by(status=status)
    
    if category_id:
        query = query.filter_by(category_id=int(category_id))
    
    posts = query.order_by(BlogPost.created_at.desc()).paginate(
        page=page, per_page=10, error_out=False
    )
    
    # Get categories for filter from Category table
    from models import Category
    categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
    
    return render_template('admin/blog.html', 
                         admin=current_admin, 
                         posts=posts,
                         search=search,
                         status=status,
                         category_id=category_id,
                         categories=categories)

@app.route('/admin/blog/create', methods=['GET', 'POST'])
@admin_required
# @csrf.exempt  # CSRF disabled  # Временно отключаем CSRF для отладки
def admin_create_post():
    """Create new blog post with full TinyMCE integration"""
    from models import Admin, BlogPost, Category
    from datetime import datetime
    import re
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    if request.method == 'GET':
        # Load categories for the form
        categories = Category.query.order_by(Category.name).all()
        return render_template('admin/create_article.html', admin=current_admin, categories=categories)
    
    if request.method == 'POST':
        try:
            title = request.form.get('title')
            content = request.form.get('content')
            excerpt = request.form.get('excerpt')
            category_id = request.form.get('category_id')
            
            # Handle featured image upload
            featured_image_url = request.form.get('featured_image', '')
            uploaded_file = request.files.get('featured_image_file')
            
            if uploaded_file and uploaded_file.filename:
                # Secure filename and save
                from werkzeug.utils import secure_filename
                import os
                filename = secure_filename(uploaded_file.filename)
                
                # Create upload directory if it doesn't exist
                upload_dir = 'static/uploads/blog'
                os.makedirs(upload_dir, exist_ok=True)
                
                # Save file with unique name
                import uuid
                unique_filename = f"{uuid.uuid4()}_{filename}"
                file_path = os.path.join(upload_dir, unique_filename)
                uploaded_file.save(file_path)
                
                # Set the URL for the database
                featured_image_url = f"/{file_path}"
            
            if not title or not content or not category_id:
                flash('Заголовок, содержание и категория обязательны', 'error')
                categories = Category.query.order_by(Category.name).all()
                return render_template('admin/create_article.html', admin=current_admin, categories=categories)
            
            # Get category name from category_id
            category = Category.query.get(int(category_id))
            if not category:
                flash('Выбранная категория не найдена', 'error')
                categories = Category.query.order_by(Category.name).all()
                return render_template('admin/create_article.html', admin=current_admin, categories=categories)
            
            # Generate slug from title
            slug = request.form.get('slug', '')
            if not slug:
                # Auto-generate slug from title
                def transliterate(text):
                    rus_to_eng = {
                        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'zh', 'з': 'z',
                        'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r',
                        'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
                        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
                    }
                    return ''.join(rus_to_eng.get(char.lower(), char) for char in text)
                
                slug = transliterate(title.lower())
                slug = re.sub(r'[^\w\s-]', '', slug)
                slug = re.sub(r'[-\s]+', '-', slug).strip('-')
            
            # Ensure unique slug
            original_slug = slug
            counter = 1
            while BlogPost.query.filter_by(slug=slug).first():
                slug = f"{original_slug}-{counter}"
                counter += 1
            
            post = BlogPost(
                title=title,
                slug=slug,
                content=content,
                excerpt=excerpt,
                meta_title=request.form.get('meta_title'),
                meta_description=request.form.get('meta_description'),
                meta_keywords=request.form.get('meta_keywords'),
                category_id=category.id,  # Store category ID for proper relation
                category=category.name,  # Store category name for compatibility
                tags=request.form.get('tags'),
                featured_image=featured_image_url,
                status=request.form.get('status', 'draft'),
                author_id=current_admin.id,
                created_at=datetime.utcnow()
            )
            
            if post.status == 'published':
                post.published_at = datetime.utcnow()
            
            db.session.add(post)
            db.session.commit()
            
            # Update category article count
            if post.status == 'published':
                category.articles_count = BlogPost.query.filter_by(category=category.name, status='published').count()
                db.session.commit()
            
            flash('Статья успешно создана!', 'success')
            return redirect(url_for('admin_blog'))
            
        except Exception as e:
            db.session.rollback()
            print(f'ERROR creating blog post: {str(e)}')
            flash(f'Ошибка при создании статьи: {str(e)}', 'error')
            categories = Category.query.order_by(Category.name).all()
            return render_template('admin/create_article.html', admin=current_admin, categories=categories)


@app.route('/admin/blog/<int:post_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_post(post_id):
    """Edit blog post"""
    from models import Admin, BlogPost, Category
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    try:
        post = BlogPost.query.get_or_404(post_id)
    except Exception as e:
        flash(f'Статья не найдена: {str(e)}', 'error')
        return redirect(url_for('admin_blog'))
    
    if request.method == 'POST':
        post.title = request.form.get('title')
        post.content = request.form.get('content')
        post.excerpt = request.form.get('excerpt')
        post.meta_title = request.form.get('meta_title')
        post.meta_description = request.form.get('meta_description')
        post.meta_keywords = request.form.get('meta_keywords')
        post.category = request.form.get('category')
        post.tags = request.form.get('tags')
        post.featured_image = request.form.get('featured_image')
        
        old_status = post.status
        post.status = request.form.get('status', 'draft')
        
        # Handle publishing
        if post.status == 'published' and old_status != 'published':
            post.published_at = datetime.utcnow()
        elif post.status != 'published':
            post.published_at = None
        
        try:
            db.session.commit()
            flash('Статья успешно обновлена', 'success')
            return redirect(url_for('admin_blog'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при обновлении статьи: {str(e)}', 'error')
    
    # Get categories for dropdown
    try:
        categories = Category.query.order_by(Category.name).all()
    except Exception as e:
        print(f'Error loading categories: {e}')
        categories = []
    
    return render_template('admin/blog_post_create.html', 
                         admin=current_admin, 
                         post=post, 
                         categories=categories)

@app.route('/admin/blog/<int:post_id>/delete', methods=['POST'])
@admin_required
def admin_delete_post(post_id):
    """Delete blog post"""
    from models import BlogPost
    
    post = BlogPost.query.get_or_404(post_id)
    
    try:
        db.session.delete(post)
        db.session.commit()
        flash('Статья успешно удалена', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при удалении статьи', 'error')
    
    return redirect(url_for('admin_blog'))

# Analytics Routes
@app.route('/admin/analytics/cashback')
@admin_required
def admin_cashback_analytics():
    """Cashback analytics page"""
    from models import Admin, CashbackApplication
    from sqlalchemy import func
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    # Monthly cashback stats
    monthly_stats = db.session.query(
        func.date_trunc('month', CashbackApplication.created_at).label('month'),
        func.count(CashbackApplication.id).label('count'),
        func.sum(CashbackApplication.cashback_amount).label('total_amount')
    ).group_by(func.date_trunc('month', CashbackApplication.created_at)).order_by('month').all()
    
    # Status breakdown
    status_stats = db.session.query(
        CashbackApplication.status,
        func.count(CashbackApplication.id).label('count'),
        func.sum(CashbackApplication.cashback_amount).label('total_amount')
    ).group_by(CashbackApplication.status).all()
    
    # Recent large cashbacks
    large_cashbacks = CashbackApplication.query.filter(
        CashbackApplication.cashback_amount >= 100000
    ).order_by(CashbackApplication.created_at.desc()).limit(10).all()
    
    return render_template('admin/cashback_analytics.html',
                         admin=current_admin,
                         monthly_stats=monthly_stats,
                         status_stats=status_stats,
                         large_cashbacks=large_cashbacks)

# Admin Blog Management Routes

@app.route('/admin/blog/<int:article_id>/edit', methods=['GET', 'POST'])
@admin_required  
def admin_edit_article(article_id):
    """Edit blog article"""
    from models import Admin, BlogPost
    import re
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    article = BlogPost.query.get_or_404(article_id)
    
    if request.method == 'POST':
        article.title = request.form.get('title')
        article.slug = request.form.get('slug')
        article.content = request.form.get('content')
        article.excerpt = request.form.get('excerpt')
        article.category = request.form.get('category')
        article.tags = request.form.get('tags')
        article.featured_image = request.form.get('featured_image')
        article.meta_title = request.form.get('meta_title')
        article.meta_description = request.form.get('meta_description')
        article.meta_keywords = request.form.get('meta_keywords')
        action = request.form.get('action', 'save')
        
        # Auto-generate slug if empty
        if not article.slug:
            slug = re.sub(r'[^\w\s-]', '', article.title.lower())
            slug = re.sub(r'[\s_-]+', '-', slug)
            article.slug = slug.strip('-')
        
        # Set status based on action
        if action == 'publish':
            article.status = 'published'
            if not article.published_at:
                article.published_at = datetime.now()
        else:
            article.status = request.form.get('status', 'draft')
        
        # Handle scheduled posts
        if article.status == 'scheduled':
            scheduled_str = request.form.get('scheduled_for')
            if scheduled_str:
                try:
                    article.scheduled_for = datetime.fromisoformat(scheduled_str)
                except:
                    pass
        else:
            article.scheduled_for = None
            
        article.updated_at = datetime.now()
        
        try:
            db.session.commit()
            flash('Статья успешно обновлена', 'success')
            return redirect(url_for('admin_blog'))
        except Exception as e:
            db.session.rollback()
            flash('Ошибка при обновлении статьи', 'error')
    
    return render_template('admin/create_article.html', admin=current_admin, article=article)

@app.route('/admin/blog/<int:article_id>/delete', methods=['POST'])
@admin_required
def admin_delete_article(article_id):
    """Delete blog article"""
    from models import BlogPost
    
    article = BlogPost.query.get_or_404(article_id)
    
    try:
        db.session.delete(article)
        db.session.commit()
        flash('Статья успешно удалена', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при удалении статьи', 'error')
    
    return redirect(url_for('admin_blog'))

@app.route('/admin/blog/<int:article_id>/publish', methods=['POST'])
@admin_required
def admin_publish_article(article_id):
    """Publish blog article"""
    from models import BlogPost
    
    article = BlogPost.query.get_or_404(article_id)
    article.status = 'published'
    article.published_at = datetime.now()
    article.updated_at = datetime.now()
    
    try:
        db.session.commit()
        flash('Статья успешно опубликована', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при публикации статьи', 'error')
    
    return redirect(url_for('admin_blog'))

# Admin Complex Cashback Management Routes
@app.route('/admin/complexes/cashback')
@csrf.exempt  # CSRF disabled for admin routes
@admin_required
def admin_complex_cashback():
    """Complex cashback management page"""
    from models import Admin, ResidentialComplex
    from flask import request
    from sqlalchemy import and_, or_
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    # DEPRECATED: Auto-sync removed - all data now in normalized schema (Developers → ResidentialComplexes → Properties)
    # The auto-sync code that previously inserted residential complexes from excel_properties table
    # has been removed because we now use a fully normalized database schema where all data
    # is properly stored in: Developers, ResidentialComplexes, and Properties tables.
    
    # Get filter parameters
    search = request.args.get('search', '')
    district = request.args.get('district', '')
    status = request.args.get('status', '')
    page = int(request.args.get('page', 1))
    per_page = 20
    
    # Build query
    query = ResidentialComplex.query
    
    # Apply filters
    if search:
        query = query.filter(or_(
            ResidentialComplex.name.contains(search),
            ResidentialComplex.developer.has(name=search)
        ))
    
    if district:
        query = query.filter(ResidentialComplex.district.has(name=district))
    
    if status == 'active':
        query = query.filter(ResidentialComplex.is_active == True)
    elif status == 'inactive':
        query = query.filter(ResidentialComplex.is_active == False)
    
    # Paginate results
    complexes = query.order_by(ResidentialComplex.name).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return render_template('admin/complex_cashback.html',
                         admin=current_admin,
                         complexes=complexes,
                         search=search,
                         district=district,
                         status=status)

@app.route('/admin/complex-cashback/<int:complex_id>/update-cashback', methods=['POST'])
@csrf.exempt  # CSRF disabled for admin routes
@admin_required
def update_complex_cashback(complex_id):
    """API endpoint to update complex cashback rate"""
    from models import ResidentialComplex
    
    try:
        complex = ResidentialComplex.query.get_or_404(complex_id)
        
        data = request.get_json()
        cashback_percent = data.get('cashback_percent')
        
        if cashback_percent is None:
            return jsonify({'success': False, 'message': 'Не указан процент кешбека'})
        
        # Validate percentage
        try:
            cashback_percent = float(cashback_percent)
            if cashback_percent < 0 or cashback_percent > 15:
                return jsonify({'success': False, 'message': 'Процент должен быть от 0% до 15%'})
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Неверный формат процента'})
        
        # Update cashback rate
        complex.cashback_rate = cashback_percent
        complex.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'message': 'Кешбек успешно обновлен',
            'cashback_percent': cashback_percent,
            'complex_name': complex.name
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error updating complex cashback: {e}")
        return jsonify({'success': False, 'message': 'Ошибка сервера'})

@app.route('/admin/complexes/cashback/create', methods=['GET', 'POST'])
@admin_required
def admin_create_complex_cashback():
    """Create new complex cashback settings"""
    from models import Admin, ResidentialComplex, District, Developer
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    if request.method == 'POST':
        name = request.form.get('name')
        developer_id = request.form.get('developer_id')
        district_id = request.form.get('district_id') 
        cashback_rate = request.form.get('cashback_rate', 5.0)
        
        try:
            # Create new complex
            complex = ResidentialComplex(
                name=name,
                slug=name.lower().replace(' ', '-'),
                developer_id=int(developer_id) if developer_id else None,
                district_id=int(district_id) if district_id else None,
                cashback_rate=float(cashback_rate),
                is_active=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            
            db.session.add(complex)
            db.session.commit()
            
            flash('ЖК успешно создан', 'success')
            return redirect(url_for('admin_complex_cashback'))
            
        except Exception as e:
            db.session.rollback()
            flash('Ошибка при создании ЖК', 'error')
    
    # Load data for form
    developers = Developer.query.filter_by(is_active=True).order_by(Developer.name).all()
    districts = District.query.filter_by(is_active=True).order_by(District.name).all()
    
    return render_template('admin/create_complex_cashback.html',
                         admin=current_admin,
                         developers=developers,
                         districts=districts)

@app.route('/admin/complexes/cashback/<int:complex_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_complex_cashback(complex_id):
    """Edit complex cashback settings"""
    from models import Admin, ResidentialComplex, District, Developer
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    complex = ResidentialComplex.query.get_or_404(complex_id)
    
    if request.method == 'POST':
        complex.name = request.form.get('name')
        complex.developer_id = int(request.form.get('developer_id')) if request.form.get('developer_id') else None
        complex.district_id = int(request.form.get('district_id')) if request.form.get('district_id') else None
        complex.cashback_rate = float(request.form.get('cashback_rate', 5.0))
        complex.is_active = bool(request.form.get('is_active'))
        complex.updated_at = datetime.utcnow()
        
        try:
            db.session.commit()
            flash('ЖК успешно обновлен', 'success')
            return redirect(url_for('admin_complex_cashback'))
        except Exception as e:
            db.session.rollback()
            flash('Ошибка при обновлении ЖК', 'error')
    
    # Load data for form
    developers = Developer.query.filter_by(is_active=True).order_by(Developer.name).all()
    districts = District.query.filter_by(is_active=True).order_by(District.name).all()
    
    return render_template('admin/edit_complex_cashback.html',
                         admin=current_admin,
                         complex=complex,
                         developers=developers,
                         districts=districts)

@app.route('/admin/complexes/cashback/<int:complex_id>/delete', methods=['POST'])
@admin_required
def admin_delete_complex_cashback(complex_id):
    """Delete complex cashback settings"""
    from models import ResidentialComplex
    
    complex = ResidentialComplex.query.get_or_404(complex_id)
    
    try:
        db.session.delete(complex)
        db.session.commit()
        flash('ЖК успешно удален', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при удалении ЖК', 'error')
    
    return redirect(url_for('admin_complex_cashback'))

# Admin Manager Management Routes  
@app.route('/admin/managers/create', methods=['GET', 'POST'])
@admin_required
def admin_create_manager():
    """Create new manager"""
    from models import Admin, Manager
    from werkzeug.security import generate_password_hash
    import json
    import random
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    if request.method == 'POST':
        full_name = request.form.get('full_name', '')
        email = request.form.get('email')
        phone = request.form.get('phone')
        position = request.form.get('position', 'Менеджер')
        profile_image = request.form.get('profile_image')
        password = request.form.get('password', 'demo123')  # Default password
        password_confirm = request.form.get('password_confirm', 'demo123')
        is_active = request.form.get('is_active') != 'False'  # Default True
        
        # Split full name into first and last name
        name_parts = full_name.split(' ', 1)
        first_name = name_parts[0] if name_parts else 'Имя'
        last_name = name_parts[1] if len(name_parts) > 1 else 'Фамилия'
        
        # Validate passwords
        if password != password_confirm:
            flash('Пароли не совпадают', 'error')
            return render_template('admin/create_manager.html', admin=current_admin)
        
        if not password:
            password = 'demo123'  # Default password
        
        # Check if email already exists
        if email:
            existing_manager = Manager.query.filter_by(email=email).first()
            if existing_manager:
                flash('Менеджер с таким email уже существует', 'error')
                return render_template('admin/create_manager.html', admin=current_admin)
        
        # Create manager
        manager = Manager()
        manager.email = email or f'manager{random.randint(1000,9999)}@inback.ru'
        manager.first_name = first_name
        manager.last_name = last_name
        manager.phone = phone
        manager.position = position
        manager.profile_image = profile_image or 'https://randomuser.me/api/portraits/men/1.jpg'
        manager.set_password(password)
        manager.is_active = is_active
        
        try:
            db.session.add(manager)
            db.session.commit()
            flash('Менеджер успешно создан', 'success')
            return redirect(url_for('admin_managers'))
        except Exception as e:
            db.session.rollback()
            flash('Ошибка при создании менеджера', 'error')
    
    return render_template('admin/create_manager.html', admin=current_admin)

@app.route('/admin/managers/<int:manager_id>/delete', methods=['POST'])
@admin_required
def admin_delete_manager(manager_id):
    """Delete manager"""
    from models import Manager
    
    manager = Manager.query.get_or_404(manager_id)
    
    try:
        db.session.delete(manager)
        db.session.commit()
        flash('Менеджер успешно удален', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при удалении менеджера', 'error')
    
    return redirect(url_for('admin_managers'))

@app.route('/admin/managers/<int:manager_id>/toggle-status', methods=['POST'])
@admin_required
def admin_toggle_manager_status(manager_id):
    """Toggle manager active status"""
    from models import Manager
    
    manager = Manager.query.get_or_404(manager_id)
    manager.is_active = not manager.is_active
    
    try:
        db.session.commit()
        status = 'активирован' if manager.is_active else 'заблокирован'
        flash(f'Менеджер {status}', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при изменении статуса менеджера', 'error')
    
    return redirect(url_for('admin_managers'))

# Additional Pages Routes
@app.route('/careers')
def careers():
    """Careers page with dynamic data"""
    from models import Job, JobCategory, Admin
    
    try:
        # Check if current user is admin
        is_admin = False
        current_admin = None
        if 'admin_id' in session:
            admin_id = session.get('admin_id')
            current_admin = Admin.query.get(admin_id)
            is_admin = current_admin is not None
        
        # Get all active job categories
        categories = JobCategory.query.filter_by(is_active=True).order_by(JobCategory.sort_order).all()
        
        # Get all active jobs with their categories (excluding paused jobs)
        jobs = Job.query.filter(Job.is_active == True, Job.status == 'active').order_by(Job.is_featured.desc(), Job.created_at.desc()).all()
        
        return render_template('careers.html', 
                             categories=categories, 
                             jobs=jobs,
                             is_admin=is_admin,
                             admin=current_admin)
        
    except Exception as e:
        print(f"Error loading careers page: {e}")
        # Fallback to static page if database fails
        return render_template('careers.html', 
                             categories=[], 
                             jobs=[],
                             is_admin=False,
                             admin=None)

@app.route('/security')
def security():
    """Security page"""
    return render_template('security.html')


if __name__ == '__main__':
    with app.app_context():
        from models import User, Manager, SavedSearch, SentSearch, CashbackRecord, Application, Favorite, Notification, District, Developer, ResidentialComplex, Street, RoomType, Admin, BlogPost, City
        db.create_all()
        
        # Initialize cities
        try:
            init_cities()
            print("Cities initialized successfully")
        except Exception as e:
            print(f"Error initializing cities: {e}")
            db.session.rollback()
        
        # Initialize search data
        try:
            init_search_data()
            print("Search data initialized successfully")
        except Exception as e:
            print(f"Error initializing search data: {e}")
            db.session.rollback()

# Collection routes for clients
@app.route('/collections')
@login_required
def client_collections():
    """Show all collections assigned to current user"""
    from models import Collection
    collections = Collection.query.filter_by(assigned_to_user_id=current_user.id).order_by(Collection.created_at.desc()).all()
    return render_template('auth/client_collections.html', collections=collections)

@app.route('/collection/<int:collection_id>')
@login_required
def view_collection(collection_id):
    """View specific collection details"""
    from models import Collection
    collection = Collection.query.filter_by(id=collection_id, assigned_to_user_id=current_user.id).first()
    if not collection:
        flash('Подборка не найдена', 'error')
        return redirect(url_for('client_collections'))
    
    # Mark as viewed
    if collection.status == 'Отправлена':
        collection.status = 'Просмотрена'
        collection.viewed_at = datetime.utcnow()
        db.session.commit()
    
    return render_template('auth/view_collection.html', collection=collection)

@app.route('/collection/<int:collection_id>/mark-viewed', methods=['POST'])
@login_required
def mark_collection_viewed(collection_id):
    """Mark collection as viewed"""
    from models import Collection
    collection = Collection.query.filter_by(id=collection_id, assigned_to_user_id=current_user.id).first()
    if collection and collection.status == 'Отправлена':
        collection.status = 'Просмотрена'
        collection.viewed_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'success': True})

# Manager collection routes
@app.route('/manager/collections')
@manager_required
def manager_collections():
    """Manager collections list"""
    from models import Collection, Manager
    current_manager = current_user
    collections = Collection.query.filter_by(created_by_manager_id=current_manager.id).order_by(Collection.created_at.desc()).all()
    return render_template('manager/collections.html', collections=collections, manager=current_manager)

@app.route('/manager/collections/new')
@manager_required
def manager_create_collection():
    """Create new collection"""
    from models import Manager, User
    current_manager = current_user
    # Get all clients assigned to this manager
    clients = User.query.filter_by(assigned_manager_id=current_manager.id).all()
    return render_template('manager/create_collection.html', manager=current_manager, clients=clients)

@app.route('/manager/collections/new', methods=['POST'])
@manager_required
def save_collection():
    """Save new collection"""
    from models import Collection, CollectionProperty, Manager
    
    current_manager = current_user
    
    title = request.form.get('title')
    description = request.form.get('description', '')
    assigned_to_user_id = request.form.get('assigned_to_user_id')
    tags = request.form.get('tags', '')
    action = request.form.get('action')
    property_ids = request.form.getlist('property_ids[]')
    property_notes = request.form.getlist('property_notes[]')
    
    if not title or not assigned_to_user_id:
        flash('Заполните обязательные поля', 'error')
        return render_template('manager/create_collection.html', manager=current_manager)
    
    try:
        # Create collection
        collection = Collection(
            title=title,
            description=description,
            created_by_manager_id=current_manager.id,
            assigned_to_user_id=int(assigned_to_user_id),
            tags=tags,
            status='Отправлена' if action == 'send' else 'Черновик',
            sent_at=datetime.utcnow() if action == 'send' else None
        )
        
        db.session.add(collection)
        db.session.flush()  # Get collection ID
        
        # Add properties to collection
        import json
        with open('data/properties.json', 'r', encoding='utf-8') as f:
            properties_data = json.load(f)
        
        properties_dict = {prop['id']: prop for prop in properties_data}
        
        for i, prop_id in enumerate(property_ids):
            if prop_id in properties_dict:
                prop_data = properties_dict[prop_id]
                note = property_notes[i] if i < len(property_notes) else ''
                
                collection_property = CollectionProperty(
                    collection_id=collection.id,
                    property_id=prop_id,
                    property_name=prop_data['title'],
                    property_price=prop_data['price'],
                    complex_name=prop_data.get('residential_complex', ''),
                    property_type=f"{prop_data['rooms']}-комн",
                    property_size=prop_data.get('area'),
                    manager_note=note,
                    order_index=i
                )
                db.session.add(collection_property)
        
        db.session.commit()
        
        action_text = 'отправлена клиенту' if action == 'send' else 'сохранена как черновик'
        flash(f'Подборка "{title}" успешно {action_text}', 'success')
        return redirect(url_for('manager_collections'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при сохранении подборки: {str(e)}', 'error')
        return render_template('manager/create_collection.html', manager=current_manager)

@app.route('/manager/analytics')
@manager_required
def manager_analytics():
    """Manager analytics page"""
    from models import Manager, User, Collection, CashbackApplication
    from sqlalchemy import func
    
    current_manager = current_user
    
    # Manager stats
    clients_count = User.query.filter_by(assigned_manager_id=current_manager.id).count()
    collections_count = Collection.query.filter_by(created_by_manager_id=current_manager.id).count()
    sent_collections = Collection.query.filter_by(created_by_manager_id=current_manager.id, status='Отправлена').count()
    
    # Monthly collection stats
    monthly_collections = db.session.query(
        func.date_trunc('month', Collection.created_at).label('month'),
        func.count(Collection.id).label('count')
    ).filter_by(created_by_manager_id=current_manager.id).group_by(
        func.date_trunc('month', Collection.created_at)
    ).order_by('month').all()
    
    # Client activity stats
    client_stats = db.session.query(
        User.client_status,
        func.count(User.id).label('count')
    ).filter_by(assigned_manager_id=current_manager.id).group_by(User.client_status).all()
    
    # Recent activity
    recent_collections = Collection.query.filter_by(
        created_by_manager_id=current_manager.id
    ).order_by(Collection.created_at.desc()).limit(5).all()
    
    return render_template('manager/analytics.html',
                         manager=current_manager,
                         clients_count=clients_count,
                         collections_count=collections_count,
                         sent_collections=sent_collections,
                         monthly_collections=monthly_collections,
                         client_stats=client_stats,
                         recent_collections=recent_collections)

@app.route('/manager/search-properties', methods=['POST'])
@manager_required
def manager_search_properties():
    """Search properties for collection"""
    import json
    
    data = request.get_json()
    min_price = data.get('min_price')
    max_price = data.get('max_price')
    rooms = data.get('rooms')
    
    try:
        with open('data/properties.json', 'r', encoding='utf-8') as f:
            properties_data = json.load(f)
        
        filtered_properties = []
        for prop in properties_data:
            # Apply filters
            if min_price and prop['price'] < int(min_price):
                continue
            if max_price and prop['price'] > int(max_price):
                continue
            if rooms and str(prop['rooms']) != str(rooms):
                continue
                
            filtered_properties.append({
                'id': prop['id'],
                'title': f"{prop.get('rooms', 0)}-комн {prop.get('area', 0)} м²" if prop.get('rooms', 0) > 0 else f"Студия {prop.get('area', 0)} м²",
                'price': prop['price'],
                'complex_name': prop.get('residential_complex', 'ЖК не указан'),
                'rooms': prop['rooms'],
                'size': prop.get('area', 0)
            })
        
        return jsonify({'properties': filtered_properties[:50]})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# Additional API routes for collection management
@app.route('/api/manager/collection/<int:collection_id>/send', methods=['POST'])
@manager_required
def api_send_collection(collection_id):
    """Send collection to client"""
    from models import Collection
    
    current_manager = current_user
    collection = Collection.query.filter_by(id=collection_id, created_by_manager_id=current_manager.id).first()
    
    if not collection:
        return jsonify({'success': False, 'error': 'Подборка не найдена'}), 404
    
    if not collection.assigned_to_user_id:
        return jsonify({'success': False, 'error': 'Клиент не назначен'}), 400
    
    try:
        collection.status = 'Отправлена'
        collection.sent_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/collection/<int:collection_id>/delete', methods=['DELETE'])
@manager_required 
def api_delete_collection(collection_id):
    """Delete collection"""
    from models import Collection
    
    current_manager = current_user
    collection = Collection.query.filter_by(id=collection_id, created_by_manager_id=current_manager.id).first()
    
    if not collection:
        return jsonify({'success': False, 'error': 'Подборка не найдена'}), 404
    
    try:
        db.session.delete(collection)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

# Manager Saved Searches API routes
@app.route('/api/manager/saved-searches')
@manager_required
def get_manager_saved_searches():
    """Get manager's saved searches"""
    from models import ManagerSavedSearch
    
    current_manager = current_user
    try:
        searches = ManagerSavedSearch.query.filter_by(manager_id=current_manager.id).order_by(ManagerSavedSearch.last_used.desc()).all()
        searches_list = [search.to_dict() for search in searches]
        
        return jsonify({
            'success': True,
            'searches': searches_list,
            'count': len(searches_list)
        })
    except Exception as e:
        print(f"Error loading manager saved searches: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/saved-searches', methods=['POST'])
@manager_required
@csrf.exempt  # Temporarily disabled for debugging
def create_manager_saved_search():
    """Create a new saved search for manager"""
    from models import ManagerSavedSearch
    import json
    
    print(f"DEBUG: ===== create_manager_saved_search API CALLED =====")
    print(f"DEBUG: Method: {request.method}")
    print(f"DEBUG: Path: {request.path}")
    # Log safe headers only (no cookies/tokens)
    safe_headers = {k: v for k, v in request.headers.items() if k.lower() not in ['cookie', 'authorization']}
    print(f"DEBUG: Headers: {safe_headers}")
    
    current_manager = current_user
    print(f"DEBUG: Manager ID: {current_manager.id}")
    
    data = request.get_json()
    print(f"DEBUG: Raw request JSON: {data}")
    print(f"DEBUG: JSON type: {type(data)}")
    
    try:
        # Extract filters from the request
        filters = data.get('filters', {})
        print(f"DEBUG: Creating manager search with filters: {filters}")
        print(f"DEBUG: Full request data: {data}")
        print(f"DEBUG: Filters type: {type(filters)}")
        print(f"DEBUG: Filters empty check: {bool(filters)}")
        
        # Test if filters is actually empty - force some test data if needed
        if not filters or not any(filters.values()):
            print("DEBUG: Filters are empty, checking raw JSON...")
            raw_json = request.get_data(as_text=True)
            print(f"DEBUG: Raw request body: {raw_json}")
        
        filters_json = json.dumps(filters) if filters else None
        print(f"DEBUG: Filters JSON: {filters_json}")
        
        # Create new search
        search = ManagerSavedSearch(
            manager_id=current_manager.id,
            name=data.get('name'),
            description=data.get('description'),
            search_type=data.get('search_type', 'properties'),
            additional_filters=filters_json,
            is_template=data.get('is_template', False)
        )
        
        db.session.add(search)
        db.session.commit()
        print(f"DEBUG: Saved search with ID: {search.id}, additional_filters: {search.additional_filters}")
        
        # Verify the saved data
        db.session.refresh(search)
        print(f"DEBUG: Refreshed search additional_filters: {search.additional_filters}")
        
        return jsonify({
            'success': True,
            'search': search.to_dict(),
            'message': 'Поиск успешно сохранён'
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error creating manager saved search: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/send-search', methods=['POST'])
@manager_required
@csrf.exempt  # Temporarily disabled for debugging
def send_search_to_client():
    """Send manager's saved search to a client"""
    from models import ManagerSavedSearch, SentSearch, User, SavedSearch, UserNotification
    from email_service import send_notification
    import json
    
    current_manager = current_user
    data = request.get_json()
    
    try:
        search_id = data.get('search_id')
        client_id = data.get('client_id')
        message = data.get('message', '')
        
        # Get manager search
        manager_search = ManagerSavedSearch.query.filter_by(id=search_id, manager_id=current_manager.id).first()
        if not manager_search:
            return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
            
        # Get client
        client = User.query.get(client_id)
        if not client:
            return jsonify({'success': False, 'error': 'Клиент не найден'}), 404
            
        # Create SavedSearch for client (copy manager search to client)
        client_search = SavedSearch(
            user_id=client_id,
            name=f"От менеджера: {manager_search.name}",
            description=f"{manager_search.description or ''}\n\n{message}".strip(),
            search_type=manager_search.search_type,
            additional_filters=manager_search.additional_filters,
            notify_new_matches=True
        )
        
        db.session.add(client_search)
        db.session.flush()  # Get the ID before final commit
        
        # Create sent search record
        sent_search = SentSearch(
            manager_id=current_manager.id,
            client_id=client_id,
            manager_search_id=search_id,
            name=manager_search.name,
            description=manager_search.description,
            additional_filters=manager_search.additional_filters,
            status='sent'
        )
        
        db.session.add(sent_search)
        db.session.flush()  # Get sent_search ID
        
        # Note: client_search is now created and linked via sent_search record
        
        # Update usage count
        manager_search.usage_count = (manager_search.usage_count or 0) + 1
        manager_search.last_used = datetime.utcnow()
        
        # Create notification for client
        notification = UserNotification(
            user_id=client_id,
            title="Новый поиск от менеджера",
            message=f"Ваш менеджер отправил вам поиск: {manager_search.name}",
            notification_type='info',
            icon='fas fa-search',
            action_url='/dashboard'
        )
        
        db.session.add(notification)
        
        # Логируем отправку поиска
        from models import UserActivity
        UserActivity.log_activity(
            user_id=client_id,
            activity_type='search_received',
            description=f'Получен новый поиск от менеджера: {manager_search.name}'
        )
        
        db.session.commit()
        
        # Send email notification
        try:
            send_notification(
                client.email,
                f"Новый поиск от менеджера: {manager_search.name}",
                f"Ваш менеджер отправил вам новый поиск недвижимости.\n\n"
                f"Название: {manager_search.name}\n"
                f"Описание: {manager_search.description or 'Без описания'}\n\n"
                f"{message}\n\n"
                f"Войдите в личный кабинет для просмотра: https://{request.host}/dashboard",
                user_id=client_id,
                notification_type='search_received'
            )
        except Exception as e:
            print(f"Error sending email notification: {e}")
        
        return jsonify({
            'success': True,
            'message': 'Поиск успешно отправлен клиенту'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error sending search to client: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/saved-search/<int:search_id>', methods=['DELETE'])
@manager_required
def delete_manager_saved_search(search_id):
    """Delete manager's saved search"""
    from models import ManagerSavedSearch
    
    current_manager = current_user
    
    try:
        search = ManagerSavedSearch.query.filter_by(id=search_id, manager_id=current_manager.id).first()
        if not search:
            return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
            
        db.session.delete(search)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Поиск удалён'})
        
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting manager saved search: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

# Developer appointment routes
@app.route('/book-appointment', methods=['GET', 'POST'])
@login_required
def book_appointment():
    """Book appointment with developer"""
    if request.method == 'POST':
        from models import DeveloperAppointment
        from datetime import datetime
        
        property_id = request.form.get('property_id')
        developer_name = request.form.get('developer_name')
        complex_name = request.form.get('complex_name')
        appointment_date = request.form.get('appointment_date')
        appointment_time = request.form.get('appointment_time')
        client_name = request.form.get('client_name')
        client_phone = request.form.get('client_phone')
        notes = request.form.get('notes', '')
        
        try:
            appointment = DeveloperAppointment(
                user_id=current_user.id,
                property_id=property_id,
                developer_name=developer_name,
                complex_name=complex_name,
                appointment_date=datetime.strptime(appointment_date, '%Y-%m-%d'),
                appointment_time=appointment_time,
                client_name=client_name,
                client_phone=client_phone,
                notes=notes
            )
            
            db.session.add(appointment)
            db.session.commit()
            
            flash('Запись к застройщику успешно создана! Менеджер свяжется с вами для подтверждения.', 'success')
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            db.session.rollback()
            flash('Ошибка при создании записи. Попробуйте еще раз.', 'error')
    
    # Get property data if property_id provided
    property_data = None
    property_id = request.args.get('property_id')
    if property_id:
        properties = load_properties()
        for prop in properties:
            if str(prop.get('id')) == property_id:
                property_data = prop
                break
    
    return render_template('book_appointment.html', property_data=property_data)

@app.route('/api/manager/add-client-old', methods=['POST'])
@manager_required
def add_client():
    """Add new client (old version - deprecated)"""
    from models import User
    from werkzeug.security import generate_password_hash
    import secrets
    
    data = request.get_json()
    first_name = data.get('first_name')
    last_name = data.get('last_name') 
    email = data.get('email')
    phone = data.get('phone')
    
    if not all([first_name, last_name, email]):
        return jsonify({'success': False, 'error': 'Заполните все обязательные поля'}), 400
    
    # Check if user exists
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({'success': False, 'error': 'Пользователь с таким email уже существует'}), 400
    
    try:
        # Generate user ID and password
        user_id = secrets.token_hex(4).upper()
        password = 'demo123'  # Default password
        password_hash = generate_password_hash(password)
        
        current_manager = current_user
        
        user = User(
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            password_hash=password_hash,
            user_id=user_id,
            assigned_manager_id=current_manager.id,
            client_status='Новый'
        )
        
        db.session.add(user)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'user': {
                'id': user.id,
                'full_name': user.full_name,
                'email': user.email,
                'phone': user.phone,
                'user_id': user.user_id,
                'password': password,
                'client_status': user.client_status
            }
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/request-payout', methods=['POST'])
@login_required
def api_request_payout():
    """Request cashback payout"""
    from models import User, CashbackPayout
    from datetime import datetime
    
    try:
        user_id = current_user.id
        
        # Check if user has available cashback
        user = User.query.get(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'Пользователь не найден'})
        
        # For demo purposes, assume available cashback of 125,000
        available_cashback = 125000
        
        if available_cashback <= 0:
            return jsonify({'success': False, 'error': 'Нет доступного кешбека для выплаты'})
        
        # Create payout request
        payout = CashbackPayout(
            user_id=user_id,
            amount=available_cashback,
            status='Запрошена',
            requested_at=datetime.utcnow()
        )
        
        db.session.add(payout)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Запрос на выплату успешно отправлен',
            'amount': available_cashback
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})



# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    db.session.rollback()
    return render_template('errors/500.html', error_details=str(error) if app.debug else None), 500

@app.errorhandler(Exception)
def handle_exception(e):
    """Handle all other exceptions"""
    db.session.rollback()
    if app.debug:
        return render_template('errors/500.html', error_details=str(e)), 500
    else:
        return render_template('errors/500.html'), 500

# City management API endpoints
@app.route('/api/change-city', methods=['POST'])
def change_city():
    """API endpoint to change current city"""
    try:
        data = request.get_json()
        city_slug = data.get('city_slug')
        city_name = data.get('city_name')
        
        if not city_slug or not city_name:
            return jsonify({'success': False, 'message': 'Missing city data'})
        
        # For now, only Krasnodar is available
        if city_slug != 'krasnodar':
            return jsonify({'success': False, 'message': 'City not available yet'})
        
        # Store in session
        session['current_city'] = city_name
        session['current_city_slug'] = city_slug
        
        return jsonify({'success': True, 'message': f'City changed to {city_name}'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': 'Error changing city'})

@app.route('/api/cities')
def get_cities():
    """Get available cities"""
    try:
        from models import City
        cities = City.query.filter_by(is_active=True).all()
        
        cities_data = []
        for city in cities:
            cities_data.append({
                'id': city.id,
                'name': city.name,
                'slug': city.slug,
                'is_default': city.is_default,
                'address_position_lat': city.address_position_lat,
                'address_position_lon': city.address_position_lon,
                'zoom_level': city.zoom_level
            })
            
        return jsonify({'cities': cities_data})
        
    except Exception as e:
        # Fallback data if database not set up yet
        return jsonify({
            'cities': [
                {
                    'id': 1,
                    'name': 'Краснодар',
                    'slug': 'krasnodar',
                    'is_default': True,
                    'address_position_lat': 45.0355,
                    'address_position_lon': 38.9753,
                    'zoom_level': 12
                }
            ]
        })

def init_cities():
    """Initialize default cities in database"""
    try:
        from models import City
        
        # Check if cities already exist
        if City.query.count() == 0:
            cities_data = [
                {
                    'name': 'Краснодар',
                    'slug': 'krasnodar',
                    'is_active': True,
                    'is_default': True,
                    'phone': '8 (862) 266-62-16',
                    'email': 'krasnodar@inback.ru',
                    'address': 'г. Краснодар, ул. Красная, 32',
                    'address_position_lat': 45.0355,
                    'address_position_lon': 38.9753,
                    'zoom_level': 12,
                    'description': 'Кэшбек за новостройки в Краснодаре',
                    'meta_title': 'Кэшбек за новостройки в Краснодаре | InBack.ru',
                    'meta_description': 'Получите до 10% кэшбека при покупке новостройки в Краснодаре. Проверенные застройщики, юридическое сопровождение.'
                },
                {
                    'name': 'Москва',
                    'slug': 'moscow',
                    'is_active': False,
                    'is_default': False,
                    'phone': '8 (862) 266-62-16',
                    'email': 'moscow@inback.ru',
                    'address': 'г. Москва, ул. Тверская, 1',
                    'address_position_lat': 55.7558,
                    'address_position_lon': 37.6176,
                    'zoom_level': 11,
                    'description': 'Кэшбек за новостройки в Москве (скоро)',
                    'meta_title': 'Кэшбек за новостройки в Москве | InBack.ru',
                    'meta_description': 'Скоро: кэшбек сервис для покупки новостроек в Москве.'
                },
                {
                    'name': 'Санкт-Петербург',
                    'slug': 'spb',
                    'is_active': False,
                    'is_default': False,
                    'phone': '8 (862) 266-62-16',
                    'email': 'spb@inback.ru',
                    'address': 'г. Санкт-Петербург, Невский пр., 1',
                    'address_position_lat': 59.9311,
                    'address_position_lon': 30.3609,
                    'zoom_level': 11,
                    'description': 'Кэшбек за новостройки в Санкт-Петербурге (скоро)',
                    'meta_title': 'Кэшбек за новостройки в СПб | InBack.ru',
                    'meta_description': 'Скоро: кэшбек сервис для покупки новостроек в Санкт-Петербурге.'
                },
                {
                    'name': 'Сочи',
                    'slug': 'sochi',
                    'is_active': False,
                    'is_default': False,
                    'phone': '8 (862) 266-62-16',
                    'email': 'sochi@inback.ru',
                    'address': 'г. Сочи, ул. Курортный пр., 1',
                    'address_position_lat': 43.6028,
                    'address_position_lon': 39.7342,
                    'zoom_level': 12,
                    'description': 'Кэшбек за новостройки в Сочи (скоро)',
                    'meta_title': 'Кэшбек за новостройки в Сочи | InBack.ru',
                    'meta_description': 'Скоро: кэшбек сервис для покупки новостроек в Сочи.'
                }
            ]
            
            for city_data in cities_data:
                city = City(**city_data)
                db.session.add(city)
            
            db.session.commit()
            print("Cities initialized successfully")
            
    except Exception as e:
        print(f"Error initializing cities: {e}")

# Legacy API route removed - using Blueprint version instead

@api_bp.route('/searches', methods=['POST'])
def save_search():
    """Save user search parameters with manager-to-client sharing functionality"""
    from models import SavedSearch, User
    data = request.get_json()
    
    # Check authentication using helper function
    auth_info = check_api_authentication()
    if not auth_info:
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401
    
    user_id = auth_info['user_id']
    user_role = auth_info['type']
    current_logged_user = auth_info['user']
    
    try:
        client_email = data.get('client_email')  # For managers
        
        print(f"DEBUG: Saving search with raw data: {data}")
        
        # Create filter object from submitted data
        filters = {}
        
        # Check if filters are nested in 'filters' object
        filter_data = data.get('filters', {}) if 'filters' in data else data
        
        # Extract filters from the data (new format)
        if 'rooms' in filter_data and filter_data['rooms']:
            if isinstance(filter_data['rooms'], list):
                room_list = [r for r in filter_data['rooms'] if r]  # Remove empty strings
                if room_list:
                    filters['rooms'] = room_list
            elif filter_data['rooms']:
                filters['rooms'] = [filter_data['rooms']]
                
        if 'districts' in filter_data and filter_data['districts']:
            if isinstance(filter_data['districts'], list):
                district_list = [d for d in filter_data['districts'] if d]  # Remove empty strings
                if district_list:
                    filters['districts'] = district_list
            elif filter_data['districts']:
                filters['districts'] = [filter_data['districts']]
                
        if 'developers' in filter_data and filter_data['developers']:
            if isinstance(filter_data['developers'], list):
                developer_list = [d for d in filter_data['developers'] if d]  # Remove empty strings
                if developer_list:
                    filters['developers'] = developer_list
            elif filter_data['developers']:
                filters['developers'] = [filter_data['developers']]
                
        if 'completion' in filter_data and filter_data['completion']:
            if isinstance(filter_data['completion'], list):
                completion_list = [c for c in filter_data['completion'] if c]  # Remove empty strings
                if completion_list:
                    filters['completion'] = completion_list
            elif filter_data['completion']:
                filters['completion'] = [filter_data['completion']]
                
        if 'priceFrom' in filter_data and filter_data['priceFrom'] and str(filter_data['priceFrom']) not in ['0', '']:
            filters['priceFrom'] = str(filter_data['priceFrom'])
        if 'priceTo' in filter_data and filter_data['priceTo'] and str(filter_data['priceTo']) not in ['0', '']:
            filters['priceTo'] = str(filter_data['priceTo'])
        if 'areaFrom' in filter_data and filter_data['areaFrom'] and str(filter_data['areaFrom']) not in ['0', '']:
            filters['areaFrom'] = str(filter_data['areaFrom'])
        if 'areaTo' in filter_data and filter_data['areaTo'] and str(filter_data['areaTo']) not in ['0', '']:
            filters['areaTo'] = str(filter_data['areaTo'])
            
        print(f"DEBUG: Extracted filters from {filter_data}: {filters}")

        # Create search with new format
        search = SavedSearch(
            user_id=user_id,
            name=data['name'],
            description=data.get('description'),
            search_type='properties',
            additional_filters=json.dumps(filters),
            notify_new_matches=data.get('notify_new_matches', True)
        )

        # Also save in legacy format for backwards compatibility
        if 'rooms' in data and data['rooms']:
            if isinstance(data['rooms'], list) and len(data['rooms']) > 0:
                search.property_type = data['rooms'][0]  # Use first room type
            else:
                search.property_type = data['rooms']
        if 'priceTo' in data and data['priceTo']:
            try:
                search.price_max = int(float(data['priceTo']) * 1000000)  # Convert millions to rubles
            except (ValueError, TypeError):
                pass
        if 'priceFrom' in data and data['priceFrom']:
            try:
                search.price_min = int(float(data['priceFrom']) * 1000000)  # Convert millions to rubles
            except (ValueError, TypeError):
                pass
        
        db.session.add(search)
        db.session.commit()
        
        # If manager specified client email, send search to client  
        if user_role == 'manager' and client_email:
            try:
                # Check if client exists
                client = User.query.filter_by(email=client_email).first()
                
                # If client exists, also save search to their account
                if client:
                    client_search = SavedSearch(
                        user_id=client.id,
                        name=data['name'] + ' (от менеджера)',
                        description=data.get('description'),
                        search_type='properties',
                        location=data.get('location'),
                        property_type=data.get('property_type'),
                        price_min=data.get('price_min'),
                        price_max=data.get('price_max'),
                        size_min=data.get('size_min'),
                        size_max=data.get('size_max'),
                        developer=data.get('developer'),
                        complex_name=data.get('complex_name'),
                        floor_min=data.get('floor_min'),
                        floor_max=data.get('floor_max'),
                        additional_filters=json.dumps(filters),
                        notify_new_matches=True
                    )
                    db.session.add(client_search)
                    db.session.commit()
                
                # Prepare search URL for client properties page  
                search_params = []
                
                # Convert manager filter format to client filter format
                if data.get('location'):
                    search_params.append(f"district={data['location']}")
                if data.get('developer'):
                    search_params.append(f"developer={data['developer']}")
                if data.get('property_type'):
                    search_params.append(f"rooms={data['property_type']}")
                if data.get('complex_name'):
                    search_params.append(f"complex={data['complex_name']}")
                if data.get('price_min'):
                    search_params.append(f"priceFrom={data['price_min'] / 1000000}")
                if data.get('price_max'):
                    search_params.append(f"priceTo={data['price_max'] / 1000000}")
                if data.get('size_min'):
                    search_params.append(f"areaFrom={data['size_min']}")
                if data.get('size_max'):
                    search_params.append(f"areaTo={data['size_max']}")
                
                search_url = f"{request.url_root}properties"
                if search_params:
                    search_url += "?" + "&".join(search_params)
                
                # Email content for client
                subject = f"Подборка недвижимости: {data['name']}"
                
                # Generate filter description for email
                filter_descriptions = []
                if data.get('property_type'):
                    filter_descriptions.append(f"Тип: {data['property_type']}")
                if data.get('location'):
                    filter_descriptions.append(f"Район: {data['location']}")
                if data.get('developer'):
                    filter_descriptions.append(f"Застройщик: {data['developer']}")
                if data.get('price_min') or data.get('price_max'):
                    price_min = f"{(data.get('price_min', 0) / 1000000):.1f}" if data.get('price_min') else "0"
                    price_max = f"{(data.get('price_max', 0) / 1000000):.1f}" if data.get('price_max') else "∞"
                    filter_descriptions.append(f"Цена: {price_min}-{price_max} млн ₽")
                if data.get('size_min') or data.get('size_max'):
                    area_min = str(data.get('size_min', 0)) if data.get('size_min') else "0"
                    area_max = str(data.get('size_max', 0)) if data.get('size_max') else "∞"
                    filter_descriptions.append(f"Площадь: {area_min}-{area_max} м²")
                
                filter_text = "<br>".join([f"• {desc}" for desc in filter_descriptions])
                
                html_content = f"""
                <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                    <h2 style="color: #0088CC;">Подборка недвижимости от InBack</h2>
                    
                    <p>Здравствуйте!</p>
                    
                    <p>Менеджер <strong>{current_user.full_name or current_user.username}</strong> подготовил для вас персональную подборку недвижимости.</p>
                    
                    <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
                        <h3 style="margin: 0 0 15px 0; color: #333;">Параметры поиска: {data['name']}</h3>
                        <div style="color: #666; line-height: 1.6;">
                            {filter_text}
                        </div>
                    </div>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{search_url}" style="display: inline-block; background: #0088CC; color: white; padding: 15px 30px; text-decoration: none; border-radius: 8px; font-weight: bold;">
                            Посмотреть подборку
                        </a>
                    </div>
                    
                    <p style="color: #666; font-size: 14px;">
                        Если у вас есть вопросы, свяжитесь с вашим менеджером:<br>
                        <strong>{current_logged_user.full_name if hasattr(current_logged_user, 'full_name') else current_logged_user.email}</strong><br>
                        Email: {current_logged_user.email}
                    </p>
                    
                    <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                    <p style="color: #999; font-size: 12px; text-align: center;">
                        InBack - ваш надежный партнер в поиске недвижимости
                    </p>
                </div>
                """
                
                # Send email using existing email service
                from email_service import send_email
                email_sent = send_email(
                    to_email=client_email,
                    subject=subject,
                    html_content=html_content,
                    template_name='collection'
                )
                
                if email_sent:
                    return jsonify({
                        'success': True, 
                        'search_id': search.id, 
                        'search': search.to_dict(),
                        'message': f'Поиск сохранен и отправлен клиенту на {client_email}',
                        'email_sent': True
                    })
                else:
                    return jsonify({
                        'success': True, 
                        'search_id': search.id, 
                        'search': search.to_dict(),
                        'message': 'Поиск сохранен, но не удалось отправить email клиенту',
                        'email_sent': False
                    })
                    
            except Exception as email_error:
                # Still return success for saved search even if email fails
                print(f"Email sending error: {email_error}")
                return jsonify({
                    'success': True, 
                    'search_id': search.id, 
                    'search': search.to_dict(),
                    'message': 'Поиск сохранен, но произошла ошибка при отправке email',
                    'email_sent': False,
                    'email_error': str(email_error)
                })
        
        return jsonify({'success': True, 'search_id': search.id, 'search': search.to_dict()})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

def check_api_authentication():
    """Helper function to check API authentication for both users and managers"""
    # Check if manager is logged in
    if 'manager_id' in session:
        from models import Manager
        manager = Manager.query.get(session['manager_id'])
        if manager:
            return {'type': 'manager', 'user_id': manager.id, 'user': manager}
    
    # Check if regular user is logged in  
    if current_user and hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
        return {'type': 'user', 'user_id': current_user.id, 'user': current_user}
    
    # Also check session for user_id (alternative authentication method)
    if 'user_id' in session:
        from models import User
        user = User.query.get(session['user_id'])
        if user:
            return {'type': 'user', 'user_id': user.id, 'user': user}
    
    return None

@app.route('/api/searches', methods=['GET', 'POST'])
@csrf.exempt
def saved_searches_endpoint():
    """Get or create user's saved searches"""
    from models import SavedSearch
    
    # Check authentication using helper function
    auth_info = check_api_authentication()
    if not auth_info:
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401
    
    if request.method == 'GET':
        # Get saved searches for the authenticated user (manager or regular user) 
        searches = SavedSearch.query.filter_by(user_id=auth_info['user_id']).order_by(SavedSearch.created_at.desc()).all()
        
        return jsonify({
            'success': True,
            'searches': [search.to_dict() for search in searches]
        })
    
    elif request.method == 'POST':
        # Create new saved search
        data = request.get_json()
        name = data.get('name')
        description = data.get('description', '')
        notify_new_matches = data.get('notify_new_matches', False)
        search_type = data.get('search_type', 'properties')
        
        if not name:
            return jsonify({'success': False, 'error': 'Название поиска обязательно'}), 400
        
        try:
            # Extract search parameters (exclude metadata fields)
            exclude_fields = {'name', 'description', 'notify_new_matches', 'search_type'}
            search_params = {k: v for k, v in data.items() if k not in exclude_fields and v}
            
            search = SavedSearch()
            search.name = name
            search.description = description
            search.notify_new_matches = notify_new_matches
            search.search_type = search_type
            search.user_id = auth_info['user_id']
            search.created_at = datetime.utcnow()
            
            # Store search parameters as additional_filters JSON
            search.additional_filters = json.dumps(search_params)
            
            # Also set individual fields if they exist in the model
            for key, value in search_params.items():
                if hasattr(search, key):
                    setattr(search, key, value)
            
            db.session.add(search)
            db.session.commit()
            
            return jsonify({'success': True, 'search_id': search.id, 'message': 'Поиск сохранен'})
        except Exception as e:
            db.session.rollback()
            print(f"Error saving search: {e}")
            return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/user/saved-searches/count')
@login_required
def get_user_saved_searches_count():
    """Get count of user's saved searches"""
    from models import SavedSearch
    
    try:
        count = SavedSearch.query.filter_by(user_id=current_user.id).count()
        return jsonify({
            'success': True,
            'count': count
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/saved-searches/<int:search_id>')
@login_required 
def get_saved_search(search_id):
    """Get saved search by ID - supports both user searches and manager shared searches"""
    try:
        from models import SavedSearch, SentSearch
        
        # First try user's own saved search
        search = SavedSearch.query.filter_by(id=search_id, user_id=current_user.id).first()
        
        # If not found, try manager shared search via SentSearch table
        if not search:
            sent_search = SentSearch.query.filter_by(
                client_id=current_user.id
            ).join(SavedSearch, SentSearch.manager_search_id == SavedSearch.id).filter(
                SavedSearch.id == search_id
            ).first()
            
            if sent_search:
                search = SavedSearch.query.get(search_id)
                # Use the additional_filters from sent_search if available
                if sent_search.additional_filters:
                    search._temp_filters = sent_search.additional_filters
        
        # If still not found, check if it's a global search available to all users
        if not search:
            search = SavedSearch.query.get(search_id)
            if search and not search.user_id:  # Global searches have no user_id
                pass  # Allow access
            else:
                search = None
        
        if not search:
            return jsonify({'success': False, 'error': 'Поиск не найден'})
        
        # Parse filters - check for temp filters from sent search first
        filters = {}
        if hasattr(search, '_temp_filters') and search._temp_filters:
            try:
                filters = json.loads(search._temp_filters)
            except:
                filters = {}
        elif search.additional_filters:
            try:
                filters = json.loads(search.additional_filters)
            except:
                filters = {}
        
        return jsonify({
            'success': True,
            'id': search.id,
            'name': search.name,
            'description': search.description,
            'search_filters': filters,
            'created_at': search.created_at.isoformat() if search.created_at else None
        })
        
    except Exception as e:
        print(f"Error getting saved search: {e}")
        return jsonify({'success': False, 'error': 'Ошибка сервера'})

@app.route('/api/searches/<int:search_id>', methods=['DELETE'])
@csrf.exempt
def delete_saved_search(search_id):
    """Delete saved search"""
    from models import SavedSearch
    
    # Check authentication using helper function
    auth_info = check_api_authentication()
    if not auth_info:
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401
    
    user_id = auth_info['user_id']
    
    search = SavedSearch.query.filter_by(id=search_id, user_id=user_id).first()
    
    if not search:
        return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
    
    try:
        db.session.delete(search)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/searches/<int:search_id>/apply', methods=['POST'])
@csrf.exempt
def apply_saved_search(search_id):
    """Apply saved search and update last_used"""
    from models import SavedSearch
    from datetime import datetime
    from urllib.parse import urlencode
    
    # Check authentication using helper function
    auth_info = check_api_authentication()
    if not auth_info:
        return jsonify({'success': False, 'error': 'Не авторизован'}), 401
    
    user_id = auth_info['user_id']
    
    search = SavedSearch.query.filter_by(id=search_id, user_id=user_id).first()
    
    if not search:
        return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
    
    try:
        search.last_used = datetime.utcnow()
        db.session.commit()
        
        # Parse filters from saved search
        filters = {}
        if search.additional_filters:
            try:
                loaded = json.loads(search.additional_filters)
                print(f"DEBUG: Loaded filters from additional_filters: {loaded}")
                
                # Check if filters are nested under 'filters' key
                if isinstance(loaded, dict) and 'filters' in loaded:
                    filters = loaded['filters']
                    print(f"DEBUG: Extracted nested filters: {filters}")
                else:
                    filters = loaded
            except json.JSONDecodeError as e:
                print(f"DEBUG: Error parsing additional_filters: {e}")
                pass
        
        # Include legacy fields as filters if not already in additional_filters
        if search.location and 'districts' not in filters:
            filters['districts'] = [search.location]
        if search.property_type and 'rooms' not in filters:
            # Keep the original property type format for proper filtering
            filters['rooms'] = [search.property_type]
        if search.developer and 'developers' not in filters:
            filters['developers'] = [search.developer]
        if search.price_min and 'priceFrom' not in filters:
            # Convert rubles to millions for client
            filters['priceFrom'] = str(search.price_min / 1000000)
        if search.price_max and 'priceTo' not in filters:
            # Convert rubles to millions for client
            filters['priceTo'] = str(search.price_max / 1000000)
        if search.size_min and 'areaFrom' not in filters:
            filters['areaFrom'] = str(search.size_min)
        if search.size_max and 'areaTo' not in filters:
            filters['areaTo'] = str(search.size_max)
        
        print(f"DEBUG: Применяем поиск '{search.name}' с фильтрами: {filters}")
        
        try:
            search_dict = search.to_dict()
        except Exception as e:
            print(f"DEBUG: Error in search.to_dict(): {e}")
            search_dict = {
                'id': search.id,
                'name': search.name,
                'description': search.description,
                'created_at': search.created_at.isoformat() if search.created_at else None
            }
        
        return jsonify({
            'success': True, 
            'search': search_dict,
            'filters': filters
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/send-property', methods=['POST'])
@login_required
def send_property_to_client_endpoint():
    """Send property search to client"""
    if current_user.role != 'manager':
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    
    try:
        data = request.get_json()
        client_id = data.get('client_id')
        search_id = data.get('search_id')
        message = data.get('message', '')
        
        if not client_id or not search_id:
            return jsonify({'success': False, 'error': 'Client ID and Search ID are required'}), 400
        
        # Verify client exists and is a buyer
        client = User.query.filter_by(id=client_id, role='buyer').first()
        if not client:
            return jsonify({'success': False, 'error': 'Client not found'}), 404
        
        # Verify search exists and belongs to manager
        search = SavedSearch.query.filter_by(id=search_id, user_id=current_user.id).first()
        if not search:
            return jsonify({'success': False, 'error': 'Search not found'}), 404
        
        # Create recommendation record
        from models import ClientPropertyRecommendation
        recommendation = ClientPropertyRecommendation(
            manager_id=current_user.id,
            client_id=client_id,
            search_id=search_id,
            message=message
        )
        
        db.session.add(recommendation)
        db.session.commit()
        
        # Send notification to client (email)
        try:
            subject = f"Подборка квартир от {current_user.full_name}"
            text_message = f"""
Здравствуйте, {client.full_name}!

Ваш менеджер {current_user.full_name} подготовил для вас подборку квартир: {search.name}

{message if message else ''}

Перейдите в личный кабинет на сайте InBack.ru, чтобы посмотреть подборку.

С уважением,
Команда InBack.ru
            """
            
            from email_service import send_email
            send_email(
                to_email=client.email,
                subject=subject,
                text_content=text_message.strip(),
                template_name='recommendation'
            )
        except Exception as e:
            app.logger.warning(f"Failed to send email notification: {str(e)}")
        
        return jsonify({
            'success': True,
            'message': 'Property recommendation sent successfully'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

# Property API routes for manager search
@app.route('/api/search/properties')
def search_properties_api():
    """Search properties for manager collection creation using normalized tables"""
    from repositories.property_repository import PropertyRepository
    
    try:
        district = request.args.get('district')
        developer = request.args.get('developer') 
        rooms = request.args.get('rooms')
        prop_type = request.args.get('type')
        price_min = request.args.get('price_min')
        price_max = request.args.get('price_max')
        area_min = request.args.get('area_min')
        
        filters = {}
        if price_min:
            filters['min_price'] = int(price_min)
        if price_max:
            filters['max_price'] = int(price_max)
        if area_min:
            filters['min_area'] = float(area_min)
        if rooms and rooms.isdigit():
            filters['rooms'] = [int(rooms)]
        if prop_type:
            filters['deal_type'] = prop_type
        
        properties = PropertyRepository.get_all_active(filters=filters, limit=100)
        
        filtered_properties = []
        for prop in properties:
            complex_obj = prop.residential_complex
            developer_obj = prop.developer
            
            district_name = complex_obj.district if complex_obj else ''
            developer_name = developer_obj.name if developer_obj else ''
            
            if district and district_name.lower() != district.lower():
                continue
            if developer and developer_name.lower() != developer.lower():
                continue
            if prop_type and prop.deal_type and prop.deal_type.lower() != prop_type.lower():
                continue
            
            price = prop.price or 0
            cashback_rate = complex_obj.cashback_rate if complex_obj and complex_obj.cashback_rate else 5.0
            cashback = int(price * cashback_rate / 100)
            
            filtered_properties.append({
                'id': prop.inner_id or prop.id,
                'complex_name': complex_obj.name if complex_obj else '',
                'district': district_name,
                'developer': developer_name,
                'rooms': prop.rooms or 0,
                'price': price,
                'cashback': cashback,
                'area': prop.area or 0,
                'floor': f"{prop.floor}/{prop.total_floors}" if prop.floor and prop.total_floors else '',
                'type': prop.deal_type or 'Первичка'
            })
        
        filtered_properties = filtered_properties[:20]
        
        return jsonify({
            'success': True,
            'properties': filtered_properties
        })
    except Exception as e:
        print(f"Error searching properties: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/search/apartments')
def search_apartments_api():
    """Search apartments with full filtering using normalized tables"""
    from repositories.property_repository import PropertyRepository, ResidentialComplexRepository
    
    try:
        district = request.args.get('district')
        developer = request.args.get('developer') 
        rooms = request.args.get('rooms')
        complex_id = request.args.get('complex')
        price_min = request.args.get('price_min')
        price_max = request.args.get('price_max')
        area_min = request.args.get('area_min')
        area_max = request.args.get('area_max')
        floor_min = request.args.get('floor_min')
        floor_max = request.args.get('floor_max')
        status = request.args.get('status')
        finishing = request.args.get('finishing')
        
        filters = {}
        if price_min:
            filters['min_price'] = int(price_min)
        if price_max:
            filters['max_price'] = int(price_max)
        if area_min:
            filters['min_area'] = float(area_min)
        if area_max:
            filters['max_area'] = float(area_max)
        if complex_id:
            filters['complex_id'] = int(complex_id)
        if rooms and rooms != 'студия':
            if rooms.isdigit():
                filters['rooms'] = [int(rooms)]
        
        properties = PropertyRepository.get_all_active(filters=filters, limit=200)
        
        filtered_apartments = []
        for prop in properties:
            complex_obj = prop.residential_complex
            developer_obj = prop.developer
            
            district_name = complex_obj.district if complex_obj else ''
            developer_name = developer_obj.name if developer_obj else ''
            
            if district and district_name.lower() != district.lower():
                continue
            if developer and developer_name.lower() != developer.lower():
                continue
            
            if rooms == 'студия' and prop.rooms != 0:
                continue
            
            prop_floor = prop.floor if prop.floor else 0
            if floor_min and prop_floor < int(floor_min):
                continue
            if floor_max and prop_floor > int(floor_max):
                continue
            
            price = prop.price or 0
            cashback_rate = complex_obj.cashback_rate if complex_obj and complex_obj.cashback_rate else 5.0
            cashback = int(price * cashback_rate / 100)
            
            photos_list = []
            if prop.gallery_images:
                try:
                    if isinstance(prop.gallery_images, list):
                        photos_list = prop.gallery_images
                    elif isinstance(prop.gallery_images, str):
                        photos_list = json.loads(prop.gallery_images)
                except:
                    pass
            
            filtered_apartments.append({
                'id': prop.inner_id or prop.id,
                'complex_name': complex_obj.name if complex_obj else '',
                'complex_id': prop.complex_id,
                'district': district_name,
                'developer': developer_name,
                'rooms': 'студия' if prop.rooms == 0 else prop.rooms,
                'price': price,
                'cashback': cashback,
                'area': prop.area or 0,
                'floor': prop.floor if prop.floor else '',
                'max_floor': prop.total_floors if prop.total_floors else '',
                'type': 'студия' if prop.rooms == 0 else f'{prop.rooms}-комн',
                'status': 'сдан',
                'finishing': prop.renovation_type or '',
                'images': photos_list,
                'description': prop.description or '',
                'features': []
            })
        
        filtered_apartments.sort(key=lambda x: x['price'])
        filtered_apartments = filtered_apartments[:50]
        
        complexes = ResidentialComplexRepository.get_all_active()
        complexes_data = {c.id: {'name': c.name, 'district': c.district} for c in complexes}
        
        return jsonify({
            'success': True,
            'apartments': filtered_apartments,
            'complexes': complexes_data
        })
    except Exception as e:
        print(f"Error searching apartments: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/complexes')
def get_complexes_api():
    """Get list of residential complexes for filter"""
    try:
        with open('data/residential_complexes.json', 'r', encoding='utf-8') as f:
            complexes_data = json.load(f)
        
        complexes_list = [
            {'id': complex_item.get('id'), 'name': complex_item.get('name', '')}
            for complex_item in complexes_data
        ]
        
        return jsonify({
            'success': True,
            'complexes': complexes_list
        })
    except Exception as e:
        print(f"Error loading complexes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/properties/<property_id>')
def get_property_details(property_id):
    """Get detailed property information using normalized tables"""
    from repositories.property_repository import PropertyRepository
    
    try:
        prop = PropertyRepository.get_by_inner_id(property_id)
        
        if not prop:
            return jsonify({'success': False, 'error': 'Property not found'}), 404
        
        complex_obj = prop.residential_complex
        developer_obj = prop.developer
        
        price = prop.price or 0
        cashback_rate = complex_obj.cashback_rate if complex_obj and complex_obj.cashback_rate else 5.0
        cashback = int(price * cashback_rate / 100)
        
        property_info = {
            'id': prop.inner_id or prop.id,
            'complex_name': complex_obj.name if complex_obj else '',
            'district': complex_obj.district if complex_obj else '',
            'developer': developer_obj.name if developer_obj else '',
            'rooms': prop.rooms or 0,
            'price': price,
            'cashback': cashback,
            'area': prop.area or 0,
            'floor': f"{prop.floor}/{prop.total_floors}" if prop.floor and prop.total_floors else '',
            'type': prop.deal_type or 'Первичка',
            'description': prop.description or '',
            'features': []
        }
        
        return jsonify({
            'success': True,
            'property': property_info
        })
    except Exception as e:
        print(f"Error getting property details: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/collections', methods=['POST'])
@manager_required
def create_collection_api():
    """Create a new property collection"""
    try:
        current_manager = current_user
            
        from models import Collection, CollectionProperty
        
        data = request.get_json()
        name = data.get('name')
        client_id = data.get('client_id')
        property_ids = data.get('property_ids', [])
        
        if not name or not client_id or not property_ids:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        # Create collection
        collection = Collection(
            title=name,
            assigned_to_user_id=client_id,
            created_by_manager_id=current_manager.id,
            status='Создана',
            description=f'Подборка из {len(property_ids)} объектов'
        )
        
        db.session.add(collection)
        db.session.flush()  # Get collection ID
        
        # Add properties to collection
        for prop_id in property_ids:
            collection_property = CollectionProperty(
                collection_id=collection.id,
                property_id=str(prop_id)
            )
            db.session.add(collection_property)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'collection_id': collection.id,
            'message': 'Подборка успешно создана'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error creating collection: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/send-collection', methods=['POST'])
@manager_required
def send_collection_to_client():
    """Send property collection to client via email"""
    try:
        current_manager = current_user
            
        from models import User, Manager
        
        data = request.get_json()
        
        # TODO: Implement collection sending logic
        return jsonify({'success': True, 'message': 'Функция в разработке'})
        
    except Exception as e:
        print(f"Error sending collection: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

# ========== ПРЕЗЕНТАЦИИ API ==========

@app.route('/api/manager/presentations', methods=['GET'])
@manager_required
def get_manager_presentations():
    """Получить все презентации менеджера"""
    try:
        from models import Collection
        
        current_manager = current_user
        
        presentations = Collection.query.filter_by(
            created_by_manager_id=current_manager.id,
            collection_type='presentation'
        ).order_by(Collection.created_at.desc()).all()
        
        presentations_data = []
        for presentation in presentations:
            presentations_data.append(presentation.to_dict())
        
        return jsonify({
            'success': True,
            'presentations': presentations_data
        })
        
    except Exception as e:
        print(f"Error loading presentations: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/presentation/create', methods=['POST'])
@manager_required
# @require_json_csrf  # CSRF disabled
def create_presentation():
    """Создать новую презентацию"""
    from models import Collection
    from flask_login import current_user
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Данные не предоставлены'}), 400
        
    title = data.get('title')
    description = data.get('description', '')
    client_name = data.get('client_name', '')
    client_phone = data.get('client_phone', '')
    
    if not title:
        return jsonify({'success': False, 'error': 'Название презентации обязательно'}), 400
    
    try:
        current_manager = current_user
            
        presentation = Collection(
            title=title,
            description=description,
            created_by_manager_id=current_manager.id,
            collection_type='presentation',
            client_name=client_name,
            client_phone=client_phone,
            status='Черновик'
        )
        
        # Генерируем уникальную ссылку
        presentation.generate_unique_url()
        
        db.session.add(presentation)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'presentation': presentation.to_dict(),
            'message': 'Презентация создана успешно'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/presentation/<int:presentation_id>', methods=['GET'])
@manager_required
def get_presentation_data(presentation_id):
    """Получить данные презентации в JSON формате для дашборда"""
    from models import Collection, CollectionProperty, Manager
    
    current_manager = current_user
    print(f"DEBUG: Get presentation data - manager_id: {current_manager.id}, presentation_id: {presentation_id}")
    
    # Get presentation data
    presentation = Collection.query.filter_by(
        id=presentation_id,
        created_by_manager_id=current_manager.id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        return jsonify({'success': False, 'error': 'Презентация не найдена или доступ запрещен'}), 404
    
    # Get presentation properties
    collection_properties = CollectionProperty.query.filter_by(
        collection_id=presentation_id
    ).order_by(CollectionProperty.order_index).all()
    
    print(f"DEBUG: Found {len(collection_properties)} properties in presentation")
    
    # Load properties data to enrich the collection properties
    properties_data = load_properties()
    
    # Enrich collection properties with full property data
    enriched_properties = []
    for cp in collection_properties:
        # Find the property in the main properties data
        property_data = None
        for prop in properties_data:
            if str(prop.get('id')) == str(cp.property_id):
                property_data = prop
                break
        
        if property_data:
            # Get complex name directly from property_data
            complex_name = property_data.get('residential_complex', property_data.get('complex_name', 'Не указан'))
            
            # Get coordinates from the coordinates object
            coordinates = property_data.get('coordinates', {})
            latitude = coordinates.get('lat') if coordinates else None
            longitude = coordinates.get('lng') if coordinates else None
            
            # Prepare main image
            main_image = property_data.get('main_image', '/static/images/no-photo.jpg')
            images = [main_image] if main_image and main_image != '/static/images/no-photo.jpg' else []
            
            enriched_property = {
                'id': property_data.get('id'),
                'property_id': cp.property_id,
                'manager_note': cp.manager_note,
                'order_index': cp.order_index,
                'rooms': property_data.get('rooms', 0),
                'price': property_data.get('price', 0),
                'area': property_data.get('area', 0),
                'floor': property_data.get('floor', 0),
                'total_floors': property_data.get('total_floors', 0),
                'complex_name': complex_name,
                'property_type': property_data.get('property_type', 'Квартира'),
                'images': images,
                'main_image': main_image,
                'layout_image': property_data.get('layout_image'),
                'address': property_data.get('address', ''),
                'latitude': latitude,
                'longitude': longitude,
                'description': property_data.get('description', ''),
                'features': property_data.get('features', []),
                'developer': property_data.get('developer', 'Не указан'),
                'district': property_data.get('district', 'Не указан'),
                'cashback': property_data.get('cashback', 0),
                'cashback_available': property_data.get('cashback_available', True),
                'price_per_sqm': property_data.get('price_per_sqm', 0),
                'status': property_data.get('status', 'available'),
                'title': property_data.get('title', f"{property_data.get('rooms', 0)}-комнатная квартира"),
                'url': property_data.get('url', f"/object/{property_data.get('id')}")
            }
            enriched_properties.append(enriched_property)
        else:
            print(f"DEBUG: Property {cp.property_id} not found in main data")
    
    print(f"DEBUG: Enriched {len(enriched_properties)} properties")
    
    # Format presentation data for JSON response
    presentation_data = {
        'id': presentation.id,
        'title': presentation.title,
        'description': presentation.description,
        'client_name': presentation.client_name,
        'client_phone': presentation.client_phone,
        'status': presentation.status,
        'created_at': presentation.created_at.isoformat() if presentation.created_at else None,
        'view_count': presentation.view_count,
        'last_viewed_at': presentation.last_viewed_at.isoformat() if presentation.last_viewed_at else None,
        'properties_count': len(enriched_properties),
        'properties': enriched_properties,
        'unique_url': presentation.unique_url,
        'assigned_to_user_id': presentation.assigned_to_user_id  # Add this for "Send to Client Dashboard" button
    }
    
    return jsonify({
        'success': True,
        'presentation': presentation_data
    })

@app.route('/api/manager/presentation/<int:presentation_id>/add_property', methods=['POST'])
@manager_required
# # @require_json_csrf  # CSRF disabled  # CSRF disabled
def add_property_to_presentation(presentation_id):
    """Добавить квартиру в презентацию"""
    from models import Collection, CollectionProperty
    from flask_login import current_user
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Данные не предоставлены'}), 400
        
    property_id = data.get('property_id')
    manager_note = data.get('manager_note', '')
    
    if not property_id:
        return jsonify({'success': False, 'error': 'ID объекта не указан'}), 400
    
    presentation = Collection.query.filter_by(
        id=presentation_id,
        created_by_manager_id=current_user.id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        return jsonify({'success': False, 'error': 'Презентация не найдена или у вас нет прав доступа'}), 404
    
    # Проверяем, не добавлена ли уже эта квартира
    existing = CollectionProperty.query.filter_by(
        collection_id=presentation_id,
        property_id=property_id
    ).first()
    
    if existing:
        return jsonify({'success': False, 'error': 'Квартира уже добавлена в презентацию'}), 400
    
    try:
        # Получаем информацию о квартире из JSON
        properties = load_properties()
        property_info = None
        
        for prop in properties:
            if str(prop.get('id')) == str(property_id):
                property_info = prop
                break
        
        if not property_info:
            return jsonify({'success': False, 'error': 'Квартира не найдена'}), 404
        
        collection_property = CollectionProperty(
            collection_id=presentation_id,
            property_id=property_id,
            property_name=property_info.get('title', 'Квартира'),
            property_price=int(property_info.get('price', 0)) if property_info.get('price') else None,
            complex_name=property_info.get('residential_complex', ''),
            property_type=f"{property_info.get('rooms', 0)}-комнатная" if property_info.get('rooms', 0) > 0 else 'Студия',
            property_size=float(property_info.get('area', 0)) if property_info.get('area') else None,
            manager_note=manager_note,
            order_index=len(presentation.properties) + 1
        )
        
        db.session.add(collection_property)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Квартира добавлена в презентацию',
            'property': {
                'id': collection_property.id,
                'property_name': collection_property.property_name,
                'complex_name': collection_property.complex_name,
                'property_price': collection_property.property_price,
                'manager_note': collection_property.manager_note
            }
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

# НОВЫЕ API ЭНДПОИНТЫ ДЛЯ ПРЕЗЕНТАЦИЙ

@app.route('/api/manager/presentation/<int:presentation_id>/add-property', methods=['POST'])
@manager_required
# # @require_json_csrf  # CSRF disabled  # CSRF disabled
def add_property_to_presentation_fixed(presentation_id):
    """Добавить объект в презентацию (безопасная версия)"""
    from models import Collection, CollectionProperty
    from flask_login import current_user
    
    # Валидация входных данных
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Данные не предоставлены'}), 400
        
    property_id = data.get('property_id')
    if not property_id:
        return jsonify({'success': False, 'error': 'ID объекта не указан'}), 400
    
    current_manager = current_user
        
    # Строгая проверка владения презентацией
    presentation = Collection.query.filter_by(
        id=presentation_id,
        created_by_manager_id=current_manager.id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        return jsonify({'success': False, 'error': 'Презентация не найдена или у вас нет прав доступа'}), 404
    
    # Проверяем, не добавлен ли уже этот объект
    existing = CollectionProperty.query.filter_by(
        collection_id=presentation_id,
        property_id=property_id
    ).first()
    
    if existing:
        return jsonify({'success': False, 'error': 'Объект уже добавлен в презентацию'}), 400
    
    try:
        # Получаем информацию об объекте
        properties = load_properties()
        property_info = None
        
        for prop in properties:
            if str(prop.get('id')) == str(property_id):
                property_info = prop
                break
        
        if not property_info:
            return jsonify({'success': False, 'error': 'Объект не найден'}), 404
        
        collection_property = CollectionProperty(
            collection_id=presentation_id,
            property_id=property_id,
            property_name=property_info.get('title', 'Квартира'),
            property_price=int(property_info.get('price', 0)) if property_info.get('price') else None,
            complex_name=property_info.get('residential_complex', ''),
            property_type=f"{property_info.get('rooms', 0)}-комнатная" if property_info.get('rooms', 0) > 0 else 'Студия',
            property_size=float(property_info.get('area', 0)) if property_info.get('area') else None,
            order_index=len(presentation.properties) + 1
        )
        
        db.session.add(collection_property)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Объект добавлен в презентацию',
            'property': {
                'id': collection_property.id,
                'property_name': collection_property.property_name,
                'complex_name': collection_property.complex_name,
                'property_price': collection_property.property_price
            }
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/presentation/<int:presentation_id>/property/<int:property_id>/comment', methods=['PUT'])
@manager_required
def update_property_comment_in_presentation(presentation_id, property_id):
    """Обновить комментарий менеджера к объекту в презентации"""
    from models import Collection, CollectionProperty
    from flask_login import current_user
    
    # Валидация входных данных
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Данные не предоставлены'}), 400
        
    manager_note = data.get('manager_note', '').strip()
    
    current_manager = current_user
        
    # Строгая проверка владения презентацией
    presentation = Collection.query.filter_by(
        id=presentation_id,
        created_by_manager_id=current_manager.id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        return jsonify({'success': False, 'error': 'Презентация не найдена или у вас нет прав доступа'}), 404
    
    # Найти объект в презентации
    collection_property = CollectionProperty.query.filter_by(
        collection_id=presentation_id,
        property_id=str(property_id)
    ).first()
    
    if not collection_property:
        return jsonify({'success': False, 'error': 'Объект не найден в презентации'}), 404
    
    try:
        # Обновляем комментарий
        collection_property.manager_note = manager_note
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Комментарий обновлен',
            'property': {
                'id': collection_property.property_id,
                'manager_note': collection_property.manager_note
            }
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/presentation/<int:presentation_id>/status', methods=['PUT'])
@manager_required
# @require_json_csrf  # CSRF disabled
def update_presentation_status(presentation_id):
    """Переключить статус презентации между Черновик и Опубликовано"""
    from models import Collection
    from flask_login import current_user
    
    # Валидация входных данных
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Данные не предоставлены'}), 400
        
    new_status = data.get('status', '').strip()
    
    # Валидация статуса
    if new_status not in ['Черновик', 'Опубликовано']:
        return jsonify({'success': False, 'error': 'Недопустимый статус'}), 400
    
    # Найти презентацию
    presentation = Collection.query.filter_by(
        id=presentation_id,
        created_by_manager_id=current_user.id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        return jsonify({'success': False, 'error': 'Презентация не найдена или у вас нет прав доступа'}), 404
    
    try:
        # Обновляем статус и флаг публичности
        presentation.status = new_status
        presentation.is_public = (new_status == 'Опубликовано')
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Статус изменен на "{new_status}"',
            'status': presentation.status,
            'is_public': presentation.is_public
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

# ===== PDF AND PRINT ENDPOINTS =====

def fetch_pdf_context(property_id, presentation_id=None):
    """
    ✅ MIGRATED TO NORMALIZED SCHEMA (Property → ResidentialComplex → Developer)
    
    Fetch comprehensive context for PDF generation including:
    - Property details and images from properties table
    - Residential complex details and characteristics
    - Manager contact information
    
    FIXED: Uses SQLAlchemy text() with bindparams for SQLite compatibility
    FIXED: Added safe resource handling and division by zero protection
    MIGRATED: Uses normalized tables (properties, residential_complexes, developers)
    """
    import json
    from models import Collection, CollectionProperty, ResidentialComplex, Manager
    from sqlalchemy import text
    
    try:
        # Get property data using normalized schema with JOINs
        property_query = text("""
        SELECT p.inner_id, p.gallery_images as photos, rc.name as complex_name, p.complex_id,
               rc.object_class_display_name as complex_object_class_display_name, 
               rc.end_build_year as complex_building_end_build_year,
               rc.end_build_quarter as complex_building_end_build_quarter, 
               rc.has_big_check as complex_has_big_check,
               rc.financing_sber as complex_financing_sber, 
               rc.has_green_mortgage as complex_has_green_mortgage,
               p.rooms as object_rooms, p.area as object_area, 
               p.floor as object_min_floor, p.total_floors as object_max_floor,
               p.price, p.address as address_display_name, 
               p.latitude as address_position_lat, p.longitude as address_position_lon,
               d.name as developer_name, p.renovation_type as renovation_display_name
        FROM properties p
        LEFT JOIN residential_complexes rc ON p.complex_id = rc.id
        LEFT JOIN developers d ON p.developer_id = d.id
        WHERE p.inner_id = :property_id
        """)
        
        # Use SQLAlchemy session with proper error handling
        result = db.session.execute(property_query, {'property_id': property_id})
        property_row = result.fetchone()
        
        if not property_row:
            return None
            
        # Convert to dictionary using _mapping for SQLAlchemy compatibility
        property_data = dict(property_row._mapping)
        
        # Parse photos JSON with safe error handling
        property_images = {'photos': [], 'plans': []}
        if property_data.get('photos'):
            try:
                photos_list = json.loads(property_data['photos'])
                if photos_list and isinstance(photos_list, list):
                    # First 6 images as main photos, 6-8 as plans (fixed logic)
                    property_images['photos'] = photos_list[:6]
                    property_images['plans'] = photos_list[6:8] if len(photos_list) > 6 else []
            except (json.JSONDecodeError, TypeError, ValueError):
                property_images['photos'] = []
                property_images['plans'] = []
        
        # Get residential complex data if available
        complex_data = {}
        complex_images = {'facade': [], 'territory': [], 'infrastructure': [], 'construction': []}
        complex_photos = []
        
        if property_data.get('complex_name'):
            try:
                # Load basic complex data from residential_complexes table
                complex_query = text("""
                SELECT name, slug, district_id, developer_id, cashback_rate,
                       object_class_display_name, start_build_year, start_build_quarter,
                       end_build_year, end_build_quarter, has_accreditation,
                       has_green_mortgage, has_big_check, with_renovation, financing_sber
                FROM residential_complexes 
                WHERE name = :complex_name
                """)
                complex_result = db.session.execute(complex_query, {'complex_name': property_data['complex_name']})
                complex_row = complex_result.fetchone()
                
                if complex_row:
                    complex_data = dict(complex_row._mapping)
                
                # Load complex photos from properties table using complex_id
                photos_query = text("""
                SELECT gallery_images AS photos FROM properties 
                WHERE complex_id = (SELECT id FROM residential_complexes WHERE name = :complex_name LIMIT 1) 
                AND gallery_images IS NOT NULL
                LIMIT 1
                """)
                photos_result = db.session.execute(photos_query, {'complex_name': property_data['complex_name']})
                photos_row = photos_result.fetchone()
                
                if photos_row and photos_row[0]:
                    try:
                        photos_data = json.loads(photos_row[0])
                        if isinstance(photos_data, list):
                            complex_photos = photos_data[:9]  # Take first 9 photos for 3x3 grid
                        elif isinstance(photos_data, dict):
                            # If photos are organized by categories  
                            all_photos = []
                            for category, photos_list in photos_data.items():
                                if isinstance(photos_list, list):
                                    all_photos.extend(photos_list)
                            complex_photos = all_photos[:9]  # Take first 9 photos for 3x3 grid
                    except (json.JSONDecodeError, TypeError):
                        complex_photos = []
                        
            except Exception as e:
                print(f"Error loading complex data: {e}")
        
        # Get manager information if presentation_id provided
        manager_data = {}
        if presentation_id:
            try:
                presentation = Collection.query.get(presentation_id)
                if presentation and presentation.created_by_manager_id:
                    manager = Manager.query.get(presentation.created_by_manager_id)
                    if manager:
                        manager_data = {
                            'name': manager.full_name or 'Менеджер',
                            'email': manager.email or '',
                            'phone': manager.phone or '+7 (XXX) XXX-XX-XX',
                            'photo_url': None  # Add if available
                        }
            except Exception as e:
                print(f"Error loading manager data: {e}")
        
        # Safe type conversion with defaults (using corrected column names)
        area = float(property_data.get('object_area') or 0)
        price = int(property_data.get('price') or 0)  # Fixed column name
        rooms = int(property_data.get('object_rooms') or 0)
        floor = int(property_data.get('object_min_floor') or 0)  # Fixed column name
        total_floors = int(property_data.get('object_max_floor') or 0)
        
        # Calculate price per sqm with division by zero protection
        price_per_sqm = 0
        if area > 0 and price > 0:
            try:
                price_per_sqm = int(price / area)
            except (ZeroDivisionError, ValueError):
                price_per_sqm = 0
        
        # Get cashback from complex data (loaded from residential_complexes table)
        cashback_rate = complex_data.get('cashback_rate', 5.0) if complex_data else 5.0
        cashback_amount = int(price * cashback_rate / 100) if price > 0 else 0
        
        # Construct full context with safe data types
        context = {
            'property': {
                'id': property_data.get('inner_id'),
                'rooms': rooms,
                'area': area,
                'floor': floor,
                'total_floors': total_floors,
                'price': price,
                'price_per_sqm': price_per_sqm,
                'finishing': property_data.get('renovation_display_name') or 'Не указан',
                'status': 'Активен',  # Default since column doesn't exist
                'address': property_data.get('address_display_name') or 'Адрес уточняется',
                'cashback_percent': cashback_rate,
                'cashback_amount': cashback_amount,
                'latitude': property_data.get('address_position_lat'),
                'longitude': property_data.get('address_position_lon'),
                'object_type': 'Квартира',  # Default since column doesn't exist
                'developer_name': property_data.get('developer_name') or '',
                'jk_name': property_data.get('complex_name') or '',
                'property_type': 'Квартира',  # Default since column doesn't exist
                'completion_date': None  # Will be set from complex data if available
            },
            'property_images': property_images,
            'complex': {
                'id': property_data.get('complex_id'),
                'name': property_data.get('complex_name') or '',
                'class': property_data.get('complex_object_class_display_name') or '',
                'completion_year': property_data.get('complex_building_end_build_year'),
                'completion_quarter': property_data.get('complex_building_end_build_quarter'),
                'has_big_check': bool(property_data.get('complex_has_big_check')),
                'financing_sber': bool(property_data.get('complex_financing_sber')),
                'has_green_mortgage': bool(property_data.get('complex_has_green_mortgage')),
                'developer': property_data.get('developer_name') or '',
                'photos': complex_photos,  # Added complex photos from database
                'features': []
            },
            'complex_images': complex_images,
            'manager': manager_data,
            'generated_at': property_data  # Full raw data for backwards compatibility
        }
        
        # Add completion date to property if available
        if context['complex']['completion_quarter'] and context['complex']['completion_year']:
            context['property']['completion_date'] = f"{context['complex']['completion_quarter']} кв. {context['complex']['completion_year']} г."
        
        # Add complex features list with safe checks
        features = []
        if complex_data.get('has_accreditation'):
            features.append('Аккредитован банками')
        if complex_data.get('has_green_mortgage') or property_data.get('complex_has_green_mortgage'):
            features.append('Льготная ипотека')  
        if complex_data.get('with_renovation'):
            features.append('С отделкой')
        if complex_data.get('financing_sber') or property_data.get('complex_financing_sber'):
            features.append('Финансирование Сбербанк')
        if complex_data.get('has_big_check') or property_data.get('complex_has_big_check'):
            features.append('Большой чек')
        
        context['complex']['features'] = features
        
        return context
        
    except Exception as e:
        print(f"Error in fetch_pdf_context: {e}")
        import traceback
        traceback.print_exc()
        return None

@app.route('/api/presentation/<int:presentation_id>/property/<string:property_id>/print')
@manager_required
def print_property(presentation_id, property_id):
    """Открыть версию объекта для печати с полными данными из базы"""
    from models import Collection, CollectionProperty
    
    # Find presentation and property
    presentation = Collection.query.get_or_404(presentation_id)
    property_obj = CollectionProperty.query.filter_by(
        collection_id=presentation_id,
        property_id=property_id
    ).first()
    
    if not property_obj:
        return "Property not found in presentation", 404
    
    # Get comprehensive context using new function
    context = fetch_pdf_context(property_id, presentation_id)
    
    if not context:
        return "Property data not found", 404
    
    # Render print template with full context
    return render_template('print_property.html', 
                         property=context['property'],
                         property_images=context['property_images'],
                         complex=context['complex'],
                         complex_images=context['complex_images'],
                         manager=context['manager'],
                         presentation=presentation,
                         manager_note=getattr(property_obj, 'manager_note', None),
                         context=context)  # Full context for backwards compatibility

@app.route('/api/presentation/<int:presentation_id>/property/<string:property_id>/download')
@manager_required
def download_presentation_property_pdf(presentation_id, property_id):
    """Скачать объект в PDF формате"""
    from models import Collection, CollectionProperty
    from weasyprint import HTML, CSS
    from io import BytesIO
    import tempfile
    import os
    
    try:
        # Find presentation and property
        presentation = Collection.query.get_or_404(presentation_id)
        property_obj = CollectionProperty.query.filter_by(
            collection_id=presentation_id,
            property_id=property_id
        ).first()
        
        if not property_obj:
            return "Property not found in presentation", 404
        
        # Get comprehensive context using new function
        context = fetch_pdf_context(property_id, presentation_id)
        
        if not context:
            return "Property data not found", 404
        
        # Render HTML for PDF with full context
        html_content = render_template('print_property.html', 
                                     property=context['property'],
                                     property_images=context['property_images'],
                                     complex=context['complex'],
                                     complex_images=context['complex_images'],
                                     manager=context['manager'],
                                     presentation=presentation,
                                     manager_note=property_obj.manager_note,
                                     context=context,
                                     for_pdf=True)
        
        # Generate PDF
        pdf_buffer = BytesIO()
        HTML(string=html_content, base_url=request.host_url).write_pdf(pdf_buffer)
        pdf_buffer.seek(0)
        
        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=f'property_{property_id}.pdf',
            mimetype='application/pdf'
        )
        
    except Exception as e:
        print(f"Error generating PDF: {e}")
        return f"Error generating PDF: {str(e)}", 500

@app.route('/api/manager/presentation/<int:presentation_id>/download-all')
@manager_required
def download_all_properties(presentation_id):
    """Скачать все объекты презентации в ZIP архиве"""
    from models import Collection, CollectionProperty
    from weasyprint import HTML, CSS
    import zipfile
    from io import BytesIO
    import tempfile
    
    try:
        # Find presentation
        presentation = Collection.query.get_or_404(presentation_id)
        
        # Check ownership
        current_manager = current_user
        if presentation.created_by_manager_id != current_manager.id:
            return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        # Get all properties in presentation
        properties = CollectionProperty.query.filter_by(
            collection_id=presentation_id
        ).all()
        
        print(f"DEBUG: Found {len(properties)} properties in presentation {presentation_id}")
        for prop in properties:
            print(f"DEBUG: Property ID: {prop.property_id}")
        
        if not properties:
            return jsonify({'success': False, 'error': 'No properties in presentation'}), 400
        
        # Create ZIP archive
        zip_buffer = BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for prop in properties:
                # Get property details from database using raw SQL for compatibility
                try:
                    query = text("""
                        SELECT 
                            p.inner_id AS id,
                            CAST(p.area AS FLOAT) AS object_area,
                            CAST(p.price AS INTEGER) AS price,
                            CAST(p.floor AS INTEGER) AS object_min_floor,
                            CAST(p.total_floors AS INTEGER) AS object_max_floor,
                            CAST(p.rooms AS INTEGER) AS object_rooms,
                            'Квартира' AS property_type,
                            p.address AS address_display_name,
                            rc.name AS jk_name,
                            d.name AS developer_name,
                            NULL AS completion_date
                        FROM properties p
                        LEFT JOIN residential_complexes rc ON p.complex_id = rc.id
                        LEFT JOIN developers d ON p.developer_id = d.id
                        WHERE p.inner_id = :prop_id
                    """)
                    
                    # Convert property_id to int for inner_id lookup
                    try:
                        prop_id_int = int(prop.property_id)
                    except ValueError:
                        print(f"Invalid property ID format: {prop.property_id}")
                        continue
                        
                    result = db.session.execute(query, {'prop_id': prop_id_int}).fetchone()
                    
                    if not result:
                        print(f"Property {prop.property_id} not found in database")
                        continue  # Skip if property not found
                        
                    # Convert row to object and compute safe values
                    from types import SimpleNamespace
                    row_dict = dict(result._mapping)
                    
                    # Ensure safe types and compute price per sqm
                    row_dict['price'] = int(row_dict['price'] or 0)
                    row_dict['area'] = float(row_dict['area'] or 0)
                    row_dict['rooms'] = int(row_dict['rooms'] or 0)
                    
                    # Compute price per sqm safely
                    if row_dict['area'] and row_dict['area'] > 0:
                        row_dict['price_per_sqm'] = int(row_dict['price'] / row_dict['area'])
                    else:
                        row_dict['price_per_sqm'] = 0
                        
                    property_item = SimpleNamespace(**row_dict)
                    property_data = [property_item]
                except Exception as e:
                    print(f"Database error for property {prop.property_id}: {e}")
                    continue
                
                if property_data:
                    property_item = property_data[0]
                    
                    # Use fetch_pdf_context to get all necessary data
                    context = fetch_pdf_context(prop.property_id, presentation_id)
                    if not context:
                        print(f"Failed to get context for property {prop.property_id}")
                        continue
                    
                    # Generate HTML for PDF with complete context
                    html_content = render_template('print_property.html', 
                                                 property=context['property'],
                                                 property_images=context['property_images'],
                                                 complex=context['complex'],
                                                 complex_images=context['complex_images'],
                                                 manager=context['manager'],
                                                 presentation=presentation,
                                                 manager_note=getattr(prop, 'manager_note', None),
                                                 context=context,
                                                 for_pdf=True)
                    
                    # Generate PDF
                    pdf_buffer = BytesIO()
                    HTML(string=html_content, base_url=request.host_url).write_pdf(pdf_buffer)
                    
                    # Add to ZIP
                    filename = f'property_{prop.property_id}.pdf'
                    zip_file.writestr(filename, pdf_buffer.getvalue())
                    print(f"DEBUG: Added {filename} to ZIP (size: {len(pdf_buffer.getvalue())} bytes)")
        
        zip_buffer.seek(0)
        print(f"DEBUG: ZIP created with total size: {len(zip_buffer.getvalue())} bytes")
        
        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name=f'presentation_{presentation_id}_all_properties.zip',
            mimetype='application/zip'
        )
        
    except Exception as e:
        print(f"Error creating ZIP: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/presentation/view/<string:unique_id>/download-all')
def download_all_properties_public(unique_id):
    """Публичное скачивание всех объектов презентации в ZIP архиве"""
    from models import Collection, CollectionProperty
    from weasyprint import HTML, CSS
    import zipfile
    from io import BytesIO
    import tempfile
    
    try:
        # Find presentation by unique_id instead of presentation_id
        presentation = Collection.query.filter_by(
            unique_url=unique_id,
            collection_type='presentation'
        ).first()
        
        if not presentation:
            return "Презентация не найдена", 404
        
        # Get all properties in presentation
        properties = CollectionProperty.query.filter_by(
            collection_id=presentation.id
        ).all()
        
        print(f"DEBUG: Found {len(properties)} properties in presentation {presentation.id}")
        for prop in properties:
            print(f"DEBUG: Property ID: {prop.property_id}")
        
        if not properties:
            return "Нет объектов в презентации", 400
        
        # Initialize progress tracking
        progress_key = f"presentation_{unique_id}"
        total_properties = len(properties)
        
        # Create ZIP archive with real progress tracking
        zip_buffer = BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for current_index, prop in enumerate(properties, 1):
                # Update progress
                progress = int((current_index / total_properties) * 85)  # Leave 15% for ZIP creation
                progress_storage[progress_key] = {
                    'stage': 'processing',
                    'progress': progress,
                    'current': current_index,
                    'total': total_properties,
                    'message': f'Создаю PDF для квартиры {current_index} из {total_properties}...'
                }
                
                # Get property details from database using raw SQL for compatibility
                try:
                    query = text("""
                        SELECT 
                            p.inner_id AS id,
                            CAST(p.area AS FLOAT) AS object_area,
                            CAST(p.price AS INTEGER) AS price,
                            CAST(p.floor AS INTEGER) AS object_min_floor,
                            CAST(p.total_floors AS INTEGER) AS object_max_floor,
                            CAST(p.rooms AS INTEGER) AS object_rooms,
                            'Квартира' AS property_type,
                            p.address AS address_display_name,
                            rc.name AS jk_name,
                            d.name AS developer_name,
                            NULL AS completion_date
                        FROM properties p
                        LEFT JOIN residential_complexes rc ON p.complex_id = rc.id
                        LEFT JOIN developers d ON p.developer_id = d.id
                        WHERE p.inner_id = :prop_id
                    """)
                    
                    # Convert property_id to int for inner_id lookup
                    try:
                        prop_id_int = int(prop.property_id)
                    except ValueError:
                        print(f"Invalid property ID format: {prop.property_id}")
                        continue
                        
                    result = db.session.execute(query, {'prop_id': prop_id_int}).fetchone()
                    
                    if not result:
                        print(f"Property {prop.property_id} not found in database")
                        continue  # Skip if property not found
                        
                    # Convert row to object and compute safe values
                    from types import SimpleNamespace
                    row_dict = dict(result._mapping)
                    
                    # Ensure safe types and compute price per sqm
                    row_dict['price'] = int(row_dict['price'] or 0)
                    row_dict['area'] = float(row_dict['area'] or 0)
                    row_dict['rooms'] = int(row_dict['rooms'] or 0)
                    
                    # Compute price per sqm safely
                    if row_dict['area'] and row_dict['area'] > 0:
                        row_dict['price_per_sqm'] = int(row_dict['price'] / row_dict['area'])
                    else:
                        row_dict['price_per_sqm'] = 0
                        
                    property_item = SimpleNamespace(**row_dict)
                    property_data = [property_item]
                except Exception as e:
                    print(f"Database error for property {prop.property_id}: {e}")
                    continue
                
                if property_data:
                    property_item = property_data[0]
                    
                    # Use fetch_pdf_context to get all necessary data
                    context = fetch_pdf_context(prop.property_id, presentation.id)
                    if not context:
                        print(f"Failed to get context for property {prop.property_id}")
                        continue
                    
                    # Generate HTML for PDF with complete context
                    html_content = render_template('print_property.html', 
                                                 property=context['property'],
                                                 property_images=context['property_images'],
                                                 complex=context['complex'],
                                                 complex_images=context['complex_images'],
                                                 manager=context['manager'],
                                                 presentation=presentation,
                                                 manager_note=getattr(prop, 'manager_note', None),
                                                 context=context,
                                                 for_pdf=True)
                    
                    # Generate PDF
                    pdf_buffer = BytesIO()
                    HTML(string=html_content, base_url=request.host_url).write_pdf(pdf_buffer)
                    
                    # Add to ZIP
                    filename = f'property_{prop.property_id}.pdf'
                    zip_file.writestr(filename, pdf_buffer.getvalue())
                    print(f"DEBUG: Added {filename} to ZIP (size: {len(pdf_buffer.getvalue())} bytes)")
        
        # Final progress - creating archive
        progress_storage[progress_key] = {
            'stage': 'completing',
            'progress': 95,
            'message': 'Создаю архив...'
        }
        
        zip_buffer.seek(0)
        print(f"DEBUG: ZIP created with total size: {len(zip_buffer.getvalue())} bytes")
        
        # Mark as complete
        progress_storage[progress_key] = {
            'stage': 'complete',
            'progress': 100,
            'message': 'Готово! Скачивание началось.'
        }
        
        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name=f'presentation_{presentation.title.replace(" ", "_")}_all_properties.zip',
            mimetype='application/zip'
        )
        
    except Exception as e:
        print(f"Error creating ZIP: {e}")
        
        # Update progress with error
        progress_key = f"presentation_{unique_id}"
        progress_storage[progress_key] = {
            'stage': 'error',
            'progress': 0,
            'message': f'Ошибка при создании архива: {str(e)}'
        }
        
        return f"Ошибка при создании архива: {str(e)}", 500

# Global progress storage for tracking real-time PDF generation
progress_storage = {}

@app.route('/presentation/view/<string:unique_id>/progress')
def download_progress_stream(unique_id):
    """SSE endpoint для отслеживания реального прогресса создания PDF файлов"""
    from flask import Response
    import json
    import time
    from models import Collection, CollectionProperty
    
    try:
        # Find presentation by unique_id
        presentation = Collection.query.filter_by(
            unique_url=unique_id,
            collection_type='presentation'
        ).first()
        
        if not presentation:
            def error_stream():
                yield f"data: {json.dumps({'error': 'Презентация не найдена'})}\n\n"
            return Response(
                error_stream(),
                content_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no'
                }
            )
        
        # Get properties count
        properties = CollectionProperty.query.filter_by(
            collection_id=presentation.id
        ).all()
        
        if not properties:
            def error_stream():
                yield f"data: {json.dumps({'error': 'Нет объектов в презентации'})}\n\n"
            return Response(
                error_stream(),
                content_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no'
                }
            )
            
        def progress_generator():
            try:
                total = len(properties)
                progress_key = f"presentation_{unique_id}"
                
                # Initialize progress
                progress_storage[progress_key] = {
                    'stage': 'starting',
                    'progress': 0,
                    'current': 0,
                    'total': total,
                    'message': 'Начинаю создание PDF файлов...'
                }
                
                # Начальное сообщение
                yield f"data: {json.dumps(progress_storage[progress_key])}\n\n"
                time.sleep(1)
                
                # Wait for real progress updates from download endpoint
                last_progress = 0
                timeout_counter = 0
                
                while True:
                    if progress_key in progress_storage:
                        current_progress = progress_storage[progress_key]
                        
                        # Only send updates when progress changes
                        if current_progress['progress'] != last_progress or current_progress['stage'] != 'processing':
                            yield f"data: {json.dumps(current_progress)}\n\n"
                            last_progress = current_progress['progress']
                        
                        # Check if completed
                        if current_progress['stage'] == 'complete':
                            break
                            
                        # Check if error occurred
                        if current_progress['stage'] == 'error':
                            break
                    
                    time.sleep(0.5)
                    timeout_counter += 1
                    
                    # Timeout after 2 minutes
                    if timeout_counter > 240:
                        progress_storage[progress_key] = {
                            'stage': 'error',
                            'message': 'Превышено время ожидания',
                            'progress': 0
                        }
                        yield f"data: {json.dumps(progress_storage[progress_key])}\n\n"
                        break
                
                # Cleanup
                if progress_key in progress_storage:
                    del progress_storage[progress_key]
                    
            except Exception as e:
                yield f"data: {json.dumps({'error': f'Ошибка: {str(e)}'})}\n\n"
        
        return Response(
            progress_generator(),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',  # Disable nginx buffering
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Cache-Control',
                'Content-Type': 'text/event-stream; charset=utf-8'
            }
        )
        
    except Exception as e:
        def error_stream():
            yield f"data: {json.dumps({'error': f'Ошибка сервера: {str(e)}'})}\n\n"
        return Response(error_stream(), content_type='text/plain')

@app.route('/api/manager/presentation/<int:presentation_id>/send-email', methods=['POST'])
@manager_required
# @require_json_csrf  # CSRF disabled
def send_presentation_email(presentation_id):
    """Отправить презентацию на email"""
    from models import Collection, CollectionProperty
    from flask_login import current_user
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    
    try:
        data = request.get_json()
        if not data or not data.get('email'):
            return jsonify({'success': False, 'error': 'Email не указан'}), 400
        
        email = data['email']
        
        # Find presentation
        presentation = Collection.query.get_or_404(presentation_id)
        
        # Check ownership
        current_manager = current_user
        if presentation.created_by_manager_id != current_manager.id:
            return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        # Get properties count
        properties_count = CollectionProperty.query.filter_by(
            collection_id=presentation_id
        ).count()
        
        # Create simple email (without attachments for now)
        msg = MIMEMultipart()
        msg['From'] = "noreply@inback.ru"
        msg['To'] = email
        msg['Subject'] = f"Презентация недвижимости от InBack - {presentation.name}"
        
        # Email body
        body = f"""
        Здравствуйте!
        
        Высылаем вам подобранную презентацию недвижимости "{presentation.name}".
        
        Количество объектов: {properties_count}
        
        Чтобы посмотреть презентацию, перейдите по ссылке:
        {request.host_url}manager/presentation/{presentation_id}
        
        С уважением,
        Команда InBack
        """
        
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        # Send email (simplified version - would need real SMTP config)
        print(f"EMAIL SENT TO: {email}")
        print(f"EMAIL BODY: {body}")
        
        return jsonify({
            'success': True,
            'message': 'Презентация отправлена на email'
        })
        
    except Exception as e:
        print(f"Error sending email: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/presentation/<int:presentation_id>/add-complex', methods=['POST'])
@manager_required
# # @require_json_csrf  # CSRF disabled  # CSRF disabled
def add_complex_to_presentation(presentation_id):
    """Добавить ЖК в презентацию (безопасная версия)"""
    from models import Collection, CollectionProperty
    from flask_login import current_user
    
    # Валидация входных данных
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Данные не предоставлены'}), 400
        
    complex_id = data.get('complex_id')
    if not complex_id:
        return jsonify({'success': False, 'error': 'ID ЖК не указан'}), 400
    
    # Get current manager
    current_manager = current_user
        
    # Строгая проверка владения презентацией
    presentation = Collection.query.filter_by(
        id=presentation_id,
        created_by_manager_id=current_manager.id,
        collection_type='presentation'
    ).first()
    
    if not presentation:
        return jsonify({'success': False, 'error': 'Презентация не найдена или у вас нет прав доступа'}), 404
    
    try:
        # Получаем все объекты из ЖК
        properties = load_properties()
        complex_properties = []
        
        for prop in properties:
            if str(prop.get('complex_id')) == str(complex_id):
                complex_properties.append(prop)
        
        if not complex_properties:
            return jsonify({'success': False, 'error': 'ЖК не найден или в нем нет объектов'}), 404
        
        added_count = 0
        for prop in complex_properties[:5]:  # Добавляем максимум 5 объектов из ЖК
            property_id = prop.get('ID')
            
            # Проверяем, не добавлен ли уже этот объект
            existing = CollectionProperty.query.filter_by(
                collection_id=presentation_id,
                property_id=property_id
            ).first()
            
            if not existing:
                collection_property = CollectionProperty(
                    collection_id=presentation_id,
                    property_id=property_id,
                    property_name=f"{prop.get('Type', '')} в {prop.get('Complex', '')}",
                    property_price=int(prop.get('Price', 0)) if prop.get('Price') else None,
                    complex_name=prop.get('Complex', ''),
                    property_type=prop.get('Type', ''),
                    property_size=float(prop.get('Size', 0)) if prop.get('Size') else None,
                    order_index=len(presentation.properties) + added_count + 1
                )
                
                db.session.add(collection_property)
                added_count += 1
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Добавлено {added_count} объектов из ЖК в презентацию',
            'added_count': added_count
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/presentation/create-with-property', methods=['POST'])
@manager_required
# # @require_json_csrf  # CSRF disabled  # CSRF disabled
def create_presentation_with_property():
    """Создать презентацию и сразу добавить объект (безопасная версия)"""
    from models import Collection, CollectionProperty
    from flask_login import current_user
    
    # Валидация входных данных
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Данные не предоставлены'}), 400
        
    title = data.get('title', '').strip()
    client_name = data.get('client_name', '').strip()
    property_id = data.get('property_id')
    
    # Строгая валидация обязательных полей
    if not title:
        return jsonify({'success': False, 'error': 'Название презентации обязательно'}), 400
    if not property_id:
        return jsonify({'success': False, 'error': 'ID объекта обязателен'}), 400
    
    try:
        # Get current manager
        current_manager = current_user
            
        # Создаем презентацию
        presentation = Collection(
            title=title,
            created_by_manager_id=current_manager.id,
            collection_type='presentation',
            client_name=client_name,
            status='Черновик'
        )
        
        presentation.generate_unique_url()
        db.session.add(presentation)
        db.session.flush()  # Получаем ID презентации
        
        # Добавляем объект
        properties = load_properties()
        property_info = None
        
        for prop in properties:
            if str(prop.get('ID')) == str(property_id):
                property_info = prop
                break
        
        if not property_info:
            return jsonify({'success': False, 'error': 'Объект не найден'}), 404
        
        collection_property = CollectionProperty(
            collection_id=presentation.id,
            property_id=property_id,
            property_name=f"{property_info.get('Type', '')} в {property_info.get('Complex', '')}",
            property_price=int(property_info.get('Price', 0)) if property_info.get('Price') else None,
            complex_name=property_info.get('Complex', ''),
            property_type=property_info.get('Type', ''),
            property_size=float(property_info.get('Size', 0)) if property_info.get('Size') else None,
            order_index=1
        )
        
        db.session.add(collection_property)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'presentation': presentation.to_dict(),
            'message': 'Презентация создана и объект добавлен'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/presentation/create-with-complex', methods=['POST'])
@manager_required
# # @require_json_csrf  # CSRF disabled  # CSRF disabled
def create_presentation_with_complex():
    """Создать презентацию и сразу добавить ЖК (безопасная версия)"""
    from models import Collection, CollectionProperty
    from flask_login import current_user
    
    # Валидация входных данных
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Данные не предоставлены'}), 400
        
    title = data.get('title', '').strip()
    client_name = data.get('client_name', '').strip()
    complex_id = data.get('complex_id')
    
    # Строгая валидация обязательных полей
    if not title:
        return jsonify({'success': False, 'error': 'Название презентации обязательно'}), 400
    if not complex_id:
        return jsonify({'success': False, 'error': 'ID ЖК обязателен'}), 400
    
    try:
        # Get current manager
        current_manager = current_user
            
        # Создаем презентацию
        presentation = Collection(
            title=title,
            created_by_manager_id=current_manager.id,
            collection_type='presentation', 
            client_name=client_name,
            status='Черновик'
        )
        
        presentation.generate_unique_url()
        db.session.add(presentation)
        db.session.flush()  # Получаем ID презентации
        
        # Добавляем объекты из ЖК
        properties = load_properties()
        complex_properties = []
        
        for prop in properties:
            if str(prop.get('complex_id')) == str(complex_id):
                complex_properties.append(prop)
        
        if not complex_properties:
            return jsonify({'success': False, 'error': 'ЖК не найден или в нем нет объектов'}), 404
        
        added_count = 0
        for prop in complex_properties[:5]:  # Добавляем максимум 5 объектов из ЖК
            property_id = prop.get('ID')
            
            collection_property = CollectionProperty(
                collection_id=presentation.id,
                property_id=property_id,
                property_name=f"{prop.get('Type', '')} в {prop.get('Complex', '')}",
                property_price=int(prop.get('Price', 0)) if prop.get('Price') else None,
                complex_name=prop.get('Complex', ''),
                property_type=prop.get('Type', ''),
                property_size=float(prop.get('Size', 0)) if prop.get('Size') else None,
                order_index=added_count + 1
            )
            
            db.session.add(collection_property)
            added_count += 1
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'presentation': presentation.to_dict(),
            'message': f'Презентация создана с {added_count} объектами из ЖК'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400


# ===== НОВЫЕ API ENDPOINT'Ы ДЛЯ ДЕЙСТВИЙ С ОБЪЕКТАМИ В ПРЕЗЕНТАЦИИ =====

# REMOVED DUPLICATE PRINT ENDPOINT - security fix
    from models import Collection, CollectionProperty
    
    try:
        # Найти презентацию
        presentation = Collection.query.filter_by(
            id=presentation_id, 
            collection_type='presentation'
        ).first()
        
        if not presentation:
            abort(404)
        
        # Найти объект в презентации
        collection_property = CollectionProperty.query.filter_by(
            collection_id=presentation_id,
            property_id=property_id
        ).first()
        
        if not collection_property:
            abort(404)
        
        # Загрузить полные данные объекта
        properties = load_properties()
        property_data = None
        
        for prop in properties:
            if str(prop.get('id')) == str(property_id) or str(prop.get('ID')) == str(property_id) or str(prop.get('inner_id')) == str(property_id):
                property_data = prop
                break
        
        if not property_data:
            abort(404)
        
        # Рендерить print-friendly версию
        return render_template('property_print.html', 
                             property=property_data,
                             presentation=presentation,
                             collection_property=collection_property)
        
    except Exception as e:
        print(f"Error in property_print_view: {e}")
        abort(500)


@app.route('/api/presentation/<int:presentation_id>/property/<string:property_id>/download')
def property_download_pdf(presentation_id, property_id):
    """Скачать PDF объекта из презентации"""
    from models import Collection, CollectionProperty
    from flask import make_response
    import io
    
    # Try to import weasyprint with fallback
    try:
        import weasyprint
        pdf_available = True
    except ImportError:
        pdf_available = False
    
    try:
        # Найти презентацию
        presentation = Collection.query.filter_by(
            id=presentation_id, 
            collection_type='presentation'
        ).first()
        
        if not presentation:
            abort(404)
        
        # Найти объект в презентации
        collection_property = CollectionProperty.query.filter_by(
            collection_id=presentation_id,
            property_id=property_id
        ).first()
        
        if not collection_property:
            abort(404)
        
        # Загрузить полные данные объекта используя fetch_pdf_context для получения фото и кэшбека
        context = fetch_pdf_context(property_id, presentation_id)
        
        if not context:
            abort(404)
        
        # Check if PDF generation is available
        if not pdf_available:
            # Fallback: redirect to print view
            return redirect(url_for('property_print_view', 
                                  presentation_id=presentation_id, 
                                  property_id=property_id))
        
        # Рендерить HTML для PDF с полным контекстом
        html_content = render_template('property_pdf.html', 
                                     property=context['property'],
                                     property_images=context['property_images'],
                                     complex=context['complex'],
                                     complex_images=context['complex_images'],
                                     manager=context['manager'],
                                     presentation=presentation,
                                     collection_property=collection_property)
        
        # Генерировать PDF
        try:
            pdf_buffer = io.BytesIO()
            weasyprint.HTML(string=html_content, base_url=request.url_root).write_pdf(pdf_buffer)
            pdf_buffer.seek(0)
            
            # Создать ASCII-safe имя файла для headers
            property_name = property_data.get('name', f"Объект_{property_id}")
            original_filename = f"{property_name}_{presentation.title}.pdf"
            
            # Create ASCII-safe filename for HTTP headers
            ascii_filename = f"property_{property_id}_{presentation_id}.pdf"
            
            # Clean original filename for RFC 5987 encoding if needed
            clean_filename = "".join(c for c in original_filename if c.isalnum() or c in (' ', '.', '_', '-')).rstrip()
        except Exception as pdf_error:
            print(f"PDF generation failed: {pdf_error}")
            # Fallback: redirect to print view
            return redirect(url_for('property_print_view', 
                                  presentation_id=presentation_id, 
                                  property_id=property_id))
        
        # Вернуть PDF как файл для скачивания с правильной кодировкой
        from urllib.parse import quote
        
        response = make_response(pdf_buffer.read())
        response.headers['Content-Type'] = 'application/pdf'
        
        # Use ASCII-safe filename in Content-Disposition with RFC 5987 fallback for Unicode
        try:
            # Try to encode original filename for browsers that support RFC 5987
            encoded_filename = quote(clean_filename.encode('utf-8'))
            response.headers['Content-Disposition'] = (
                f'attachment; '
                f'filename="{ascii_filename}"; '
                f'filename*=UTF-8\'\'{encoded_filename}'
            )
        except:
            # Fallback to ASCII-only filename
            response.headers['Content-Disposition'] = f'attachment; filename="{ascii_filename}"'
        
        return response
        
    except Exception as e:
        print(f"Error in property_download_pdf: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/presentation/<int:presentation_id>/property/<string:property_id>/view')
def property_view_redirect(presentation_id, property_id):
    """Перенаправить на полную страницу объекта на сайте"""
    from models import Collection, CollectionProperty
    
    try:
        # Найти презентацию
        presentation = Collection.query.filter_by(
            id=presentation_id, 
            collection_type='presentation'
        ).first()
        
        if not presentation:
            abort(404)
        
        # Найти объект в презентации
        collection_property = CollectionProperty.query.filter_by(
            collection_id=presentation_id,
            property_id=property_id
        ).first()
        
        if not collection_property:
            abort(404)
        
        # Перенаправить на страницу объекта
        return redirect(url_for('property_detail', property_id=property_id))
        
    except Exception as e:
        print(f"Error in property_view_redirect: {e}")
        abort(500)


@app.route('/api/manager/presentation/<int:presentation_id>/property/<string:property_id>/delete', methods=['DELETE'])
@manager_required
# # @require_json_csrf  # CSRF disabled  # CSRF disabled
def delete_property_from_presentation(presentation_id, property_id):
    """Удалить объект из презентации (только для менеджера-владельца)"""
    from models import Collection, CollectionProperty
    from flask_login import current_user
    
    try:
        # Найти презентацию
        presentation = Collection.query.filter_by(
            id=presentation_id, 
            collection_type='presentation'
        ).first()
        
        if not presentation:
            return jsonify({'success': False, 'error': 'Презентация не найдена'}), 404
        
        # Проверить права доступа - только создатель презентации может удалять объекты
        if presentation.created_by_manager_id != current_user.id:
            return jsonify({'success': False, 'error': 'Нет прав для удаления объектов из этой презентации'}), 403
        
        # Найти объект в презентации
        collection_property = CollectionProperty.query.filter_by(
            collection_id=presentation_id,
            property_id=property_id
        ).first()
        
        if not collection_property:
            return jsonify({'success': False, 'error': 'Объект не найден в презентации'}), 404
        
        # Удалить объект из презентации
        db.session.delete(collection_property)
        db.session.commit()
        
        # Обновить количество объектов в презентации
        remaining_properties = CollectionProperty.query.filter_by(collection_id=presentation_id).count()
        
        return jsonify({
            'success': True,
            'message': 'Объект успешно удален из презентации',
            'remaining_properties': remaining_properties
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error in delete_property_from_presentation: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def send_collection_to_user():
    """Send collection to user - legacy function"""
    if request.method != 'POST':
        return jsonify({'success': False, 'error': 'Only POST method allowed'}), 405
    
    data = request.get_json()
    current_manager = current_user
    
    try:
        name = data.get('name')
        client_id = data.get('client_id')
        property_ids = data.get('property_ids', [])
        
        if not name or not client_id or not property_ids:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        # Get client and manager info
        client = User.query.get(client_id)
        manager = current_manager
        
        if not client or not manager:
            return jsonify({'success': False, 'error': 'Client or manager not found'}), 404
        
        # Load property details
        with open('data/properties_expanded.json', 'r', encoding='utf-8') as f:
            properties_data = json.load(f)
        
        selected_properties = []
        total_cashback = 0
        
        for prop_id in property_ids:
            for prop in properties_data:
                if str(prop.get('id')) == str(prop_id):
                    price = prop.get('price', 0)
                    cashback = int(price * 0.05)
                    total_cashback += cashback
                    
                    selected_properties.append({
                        'complex_name': prop.get('complex_name', ''),
                        'district': prop.get('district', ''),
                        'developer': prop.get('developer', ''),
                        'rooms': prop.get('rooms', 0),
                        'area': prop.get('area', 0),
                        'price': price,
                        'cashback': cashback,
                        'type': prop.get('type', ''),
                        'description': prop.get('description', '')
                    })
                    break
        
        # Create email content
        properties_list = '\n'.join([
            f"• {prop['complex_name']} ({prop['district']})\n"
            f"  {prop['rooms']}-комн., {prop['area']} м²\n"
            f"  Цена: {prop['price']:,} ₽\n"
            f"  Кешбек: {prop['cashback']:,} ₽\n"
            for prop in selected_properties
        ])
        
        subject = f"Подборка недвижимости: {name}"
        text_message = f"""
Здравствуйте, {client.full_name}!

Ваш менеджер {manager.full_name} подготовил для вас персональную подборку недвижимости "{name}".

ПОДОБРАННЫЕ ОБЪЕКТЫ ({len(selected_properties)} шт.):

{properties_list}

ОБЩИЙ КЕШБЕК: {total_cashback:,} ₽

Для получения подробной информации и записи на просмотр свяжитесь с вашим менеджером:
{manager.full_name}
Email: {manager.email}
Телефон: {manager.phone or 'не указан'}

Или перейдите в личный кабинет на сайте InBack.ru

С уважением,
Команда InBack.ru
        """.strip()
        
        # Send email
        try:
            from email_service import send_email
            send_email(
                to_email=client.email,
                subject=subject,
                text_content=text_message,
                template_name='collection'
            )
            
            return jsonify({
                'success': True,
                'message': f'Подборка отправлена на email {client.email}'
            })
            
        except Exception as e:
            print(f"Error sending email: {e}")
            return jsonify({'success': False, 'error': 'Ошибка отправки email'}), 500
        
    except Exception as e:
        print(f"Error sending collection: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/data/properties_expanded.json')
def properties_json():
    """Serve properties JSON data"""
    try:
        properties = load_properties()
        return jsonify(properties)
    except Exception as e:
        print(f"Error serving properties JSON: {e}")
        return jsonify([]), 500

# Database initialization will be done after all imports

# Client Recommendations API endpoints
@app.route('/api/user/collections', methods=['GET'])
@login_required
def api_user_get_collections():
    """Get collections assigned to current user"""
    from models import Collection, ExcelProperty
    import json
    
    try:
        collections = Collection.query.filter_by(
            assigned_to_user_id=current_user.id
        ).order_by(Collection.created_at.desc()).all()
        
        collections_data = []
        for collection in collections:
            # Get manager info
            manager = collection.created_by
            manager_name = manager.full_name if manager else 'Менеджер'
            # Use manager's profile image if available, otherwise use first letter
            # Use manager's profile image if available and not randomuser.me, otherwise use first letter
            if manager and manager.profile_image and 'randomuser.me' not in manager.profile_image:
                # Convert relative path to absolute URL
                if manager.profile_image.startswith('http'):
                    manager_avatar = manager.profile_image
                else:
                    base_url = request.host_url.rstrip('/')
                    manager_avatar = f"{base_url}{manager.profile_image}"
            else:
                manager_avatar = manager_name[0].upper() if manager_name else 'М'
            
            # ✅ MIGRATED: Get properties with images from normalized tables
            properties_data = []
            for prop in collection.properties[:4]:
                # Load property from database using PropertyRepository
                property_obj = PropertyRepository.get_by_inner_id(prop.property_id)
                if property_obj:
                    # Get first photo from photos array
                    image_url = ''
                    if property_obj.gallery_images:
                        try:
                            photos_list = json.loads(property_obj.gallery_images) if isinstance(property_obj.gallery_images, str) else property_obj.gallery_images
                            image_url = photos_list[0] if photos_list and len(photos_list) > 0 else ''
                        except Exception as photo_error:
                            print(f"DEBUG: Error parsing photos for property {prop.property_id}: {photo_error}")
                    
                    # Build title from property data
                    rooms_text = f"{property_obj.rooms}-комн" if property_obj.rooms and property_obj.rooms > 0 else "Студия"
                    area_text = f"{property_obj.area} м²" if property_obj.area else ""
                    title = f"{rooms_text}, {area_text}".strip(', ')
                    
                    properties_data.append({
                        'id': prop.property_id,
                        'image': image_url,
                        'title': title
                    })
            
            collections_data.append({
                'id': collection.id,
                'title': collection.title,
                'description': collection.description,
                'status': collection.status,
                'created_at': collection.created_at.strftime('%d.%m.%Y'),
                'manager_name': manager_name,
                'manager_avatar': manager_avatar,
                'properties_count': len(collection.properties),
                'properties': properties_data,
                'unique_url': collection.unique_url
            })
        
        print(f"🎯 Collections API: returning {len(collections_data)} collections")
        
        return jsonify({
            'success': True,
            'collections': collections_data
        })
        
    except Exception as e:
        print(f"❌ Error loading collections: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 400

@login_required
def api_user_get_saved_searches():
    """Get saved searches for current user"""
    from models import SavedSearch
    
    try:
        # Get regular saved searches
        saved_searches = SavedSearch.query.filter_by(
            user_id=current_user.id
        ).order_by(SavedSearch.created_at.desc()).all()
        
        # Get sent searches from managers
        from models import SentSearch
        sent_searches = SentSearch.query.filter_by(
            client_id=current_user.id
        ).order_by(SentSearch.sent_at.desc()).all()
        
        searches_data = []
        
        # Add regular saved searches
        for search in saved_searches:
            filters = {}
            if search.filters:
                import json
                filters = json.loads(search.filters) if isinstance(search.filters, str) else search.filters
            
            searches_data.append({
                'id': search.id,
                'name': search.name,
                'filters': filters,
                'created_at': search.created_at.strftime('%d.%m.%Y'),
                'last_used': search.last_used.strftime('%d.%m.%Y') if search.last_used else None,
                'type': 'saved'
            })
        
        # Add sent searches from managers
        for search in sent_searches:
            filters = {}
            if search.additional_filters:
                import json
                filters = json.loads(search.additional_filters) if isinstance(search.additional_filters, str) else search.additional_filters
            
            searches_data.append({
                'id': search.id,
                'name': search.name,
                'filters': filters,
                'created_at': search.sent_at.strftime('%d.%m.%Y') if search.sent_at else 'Не указано',
                'last_used': search.applied_at.strftime('%d.%m.%Y') if search.applied_at else None,
                'type': 'sent',
                'from_manager': True
            })
        
        return jsonify({
            'success': True,
            'searches': searches_data
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/user/recommendations', methods=['GET'])
@login_required
def api_user_get_recommendations():
    """Get recommendations for current user"""
    from models import Recommendation, SentSearch
    from datetime import datetime
    
    try:
        print(f"DEBUG: Loading recommendations for user ID: {current_user.id}")
        
        # Get traditional recommendations
        recommendations = Recommendation.query.filter_by(
            client_id=current_user.id
        ).order_by(Recommendation.sent_at.desc()).all()
        
        print(f"DEBUG: Found {len(recommendations)} recommendations for user {current_user.id}")
        
        recommendations_data = []
        for rec in recommendations:
            rec_data = rec.to_dict()
            rec_data['manager_name'] = f"{rec.manager.first_name} {rec.manager.last_name}" if rec.manager else 'Менеджер'
            recommendations_data.append(rec_data)
        
        # Get sent searches from managers as recommendations  
        sent_searches = SentSearch.query.filter_by(client_id=current_user.id).order_by(SentSearch.sent_at.desc()).all()
        
        # Convert sent searches to recommendation format
        for search in sent_searches:
            search_rec = {
                'id': f'search_{search.id}',
                'title': f'Подбор недвижимости: {search.name}',
                'description': search.description or 'Персональный подбор от вашего менеджера',
                'recommendation_type': 'search',
                'item_id': str(search.id),
                'item_name': search.name,
                'manager_notes': f'Ваш менеджер {search.manager.name} подготовил персональный подбор недвижимости',
                'priority_level': 'high',
                'status': search.status,
                'viewed_at': search.viewed_at.isoformat() if search.viewed_at else None,
                'created_at': search.sent_at.isoformat() if search.sent_at else None,
                'sent_at': search.sent_at.isoformat() if search.sent_at else None,
                'manager_name': search.manager.name,
                'search_filters': search.additional_filters,
                'search_id': search.id
            }
            recommendations_data.append(search_rec)
        
        # Sort by creation date 
        recommendations_data.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        return jsonify({
            'success': True, 
            'recommendations': recommendations_data
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/saved-searches/<int:search_id>')
@login_required
def get_saved_search_details(search_id):
    """Get saved search details for applying filters"""
    from models import SavedSearch
    
    try:
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'User not authenticated'}), 401
        
        # Get the saved search
        saved_search = SavedSearch.query.filter_by(id=search_id, user_id=user_id).first()
        if not saved_search:
            return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
        
        return jsonify({
            'success': True,
            'id': saved_search.id,
            'name': saved_search.name,
            'description': saved_search.description,
            'search_filters': saved_search.additional_filters,
            'created_at': saved_search.created_at.isoformat() if saved_search.created_at else None
        })
        
    except Exception as e:
        print(f"Error getting saved search details: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/sent-searches')
@login_required
def get_sent_searches():
    """Get sent searches from managers as recommendations"""
    from models import SentSearch
    
    try:
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'User not authenticated'}), 401
        
        # Get sent searches
        sent_searches = SentSearch.query.filter_by(client_id=user_id).order_by(SentSearch.sent_at.desc()).all()
        
        # Format as recommendation-like objects
        search_list = []
        
        for search in sent_searches:
            search_list.append({
                'id': search.id,
                'name': search.name or 'Поиск от менеджера',
                'title': search.name or 'Поиск от менеджера',
                'description': search.description,
                'status': search.status or 'sent',
                'sent_at': search.sent_at.isoformat() if search.sent_at else None,
                'created_at': search.sent_at.isoformat() if search.sent_at else None,
                'search_filters': search.additional_filters,
                'manager_id': search.manager_id,
                'recommendation_type': 'search'
            })
        
        return jsonify({
            'success': True,
            'sent_searches': search_list
        })
        
    except Exception as e:
        print(f"Error getting sent searches: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/sent-search/<int:search_id>')
@login_required
def get_sent_search_detail(search_id):
    """Get details of a specific sent search from manager"""
    from models import SentSearch
    import json
    
    try:
        # Get the specific sent search for this client using current_user from Flask-Login
        sent_search = SentSearch.query.filter_by(id=search_id, client_id=current_user.id).first()
        
        if not sent_search:
            return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
        
        # Parse additional_filters if it's a JSON string
        filters = sent_search.additional_filters
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except json.JSONDecodeError:
                filters = {}
        elif filters is None:
            filters = {}
        
        return jsonify({
            'success': True,
            'search': {
                'id': sent_search.id,
                'name': sent_search.name or 'Поиск от менеджера',
                'description': sent_search.description,
                'additional_filters': filters,
                'status': sent_search.status or 'sent',
                'sent_at': sent_search.sent_at.isoformat() if sent_search.sent_at else None,
                'manager_id': sent_search.manager_id
            }
        })
        
    except Exception as e:
        print(f"Error getting sent search detail: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/recommendations/<rec_id>/viewed', methods=['POST'])
@login_required  
def api_mark_recommendation_viewed(rec_id):
    """Mark recommendation as viewed"""
    from models import Recommendation, SentSearch
    from datetime import datetime
    
    try:
        # Handle search recommendations
        if str(rec_id).startswith('search_'):
            search_id = int(rec_id.replace('search_', ''))
            sent_search = SentSearch.query.filter_by(
                id=search_id, 
                client_id=current_user.id
            ).first()
            
            if not sent_search:
                return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
                
            if sent_search.status == 'sent':
                sent_search.status = 'viewed'
                sent_search.viewed_at = datetime.utcnow()
                db.session.commit()
            
            return jsonify({'success': True})
        
        # Handle traditional recommendations
        recommendation = Recommendation.query.filter_by(
            id=int(rec_id), 
            client_id=current_user.id
        ).first()
        
        if not recommendation:
            return jsonify({'success': False, 'error': 'Рекомендация не найдена'}), 404
            
        if recommendation.status == 'sent':
            recommendation.status = 'viewed'
            recommendation.viewed_at = datetime.utcnow()
            db.session.commit()
        
        return jsonify({'success': True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/recommendations/<int:rec_id>/dismiss', methods=['POST'])
@login_required
def api_dismiss_recommendation(rec_id):
    """Dismiss/hide recommendation"""
    from models import Recommendation
    from datetime import datetime
    
    try:
        recommendation = Recommendation.query.filter_by(
            id=rec_id, 
            client_id=current_user.id
        ).first()
        
        if not recommendation:
            return jsonify({'success': False, 'error': 'Рекомендация не найдена'}), 404
            
        # Mark as dismissed
        recommendation.status = 'dismissed'
        recommendation.viewed_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({'success': True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/recommendations/<rec_id>/apply', methods=['POST'])
@login_required  
def api_apply_search_recommendation(rec_id):
    """Apply search recommendation - redirect to properties with filters"""
    from models import SentSearch
    from datetime import datetime
    import json
    
    try:
        # Handle search recommendations only
        if not str(rec_id).startswith('search_'):
            return jsonify({'success': False, 'error': 'Только поиски можно применить'}), 400
            
        search_id = int(rec_id.replace('search_', ''))
        sent_search = SentSearch.query.filter_by(
            id=search_id, 
            client_id=current_user.id
        ).first()
        
        if not sent_search:
            return jsonify({'success': False, 'error': 'Поиск не найден'}), 404
        
        # Update search status
        sent_search.applied_at = datetime.utcnow()
        if sent_search.status == 'sent':
            sent_search.status = 'applied'
        db.session.commit()
        
        # Parse filters from the search
        filters = {}
        if sent_search.additional_filters:
            try:
                filters = json.loads(sent_search.additional_filters)
            except json.JSONDecodeError:
                pass
        
        return jsonify({
            'success': True, 
            'filters': filters
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/user/recommendation-categories', methods=['GET'])
@login_required
def api_user_get_categories():
    """Get all categories that have recommendations for current user"""
    from models import RecommendationCategory
    
    try:
        categories = RecommendationCategory.query.filter_by(
            client_id=current_user.id
        ).filter(RecommendationCategory.recommendations_count > 0).all()
        
        categories_data = []
        for category in categories:
            categories_data.append({
                'id': category.id,
                'name': category.name,
                'description': category.description,
                'color': category.color,
                'recommendations_count': category.recommendations_count
            })
        
        return jsonify({
            'success': True,
            'categories': categories_data
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

# User Comparison Count API Endpoints
@app.route('/api/user/comparison/properties/count')
@login_required
def api_user_comparison_properties_count():
    """Get count of properties in comparison for current user"""
    from models import ComparisonProperty, UserComparison
    
    try:
        # Find active user comparison
        user_comparison = UserComparison.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).first()
        
        if not user_comparison:
            return jsonify({
                'success': True,
                'count': 0
            })
        
        # Count properties in this comparison
        count = ComparisonProperty.query.filter_by(
            user_comparison_id=user_comparison.id
        ).count()
        
        return jsonify({
            'success': True,
            'count': count
        })
        
    except Exception as e:
        print(f"Error getting user comparison properties count: {e}")
        return jsonify({'success': False, 'error': str(e), 'count': 0}), 500

@app.route('/api/user/comparison/complexes/count')
@login_required
def api_user_comparison_complexes_count():
    """Get count of complexes in comparison for current user"""
    from models import ComparisonComplex, UserComparison
    
    try:
        # Find active user comparison
        user_comparison = UserComparison.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).first()
        
        if not user_comparison:
            return jsonify({
                'success': True,
                'count': 0
            })
        
        # Count complexes in this comparison
        count = ComparisonComplex.query.filter_by(
            user_comparison_id=user_comparison.id
        ).count()
        
        return jsonify({
            'success': True,
            'count': count
        })
        
    except Exception as e:
        print(f"Error getting user comparison complexes count: {e}")
        return jsonify({'success': False, 'error': str(e), 'count': 0}), 500

# ========================================
# USER COMPARISON ENDPOINTS
# ========================================

@app.route('/api/user/comparison/property/add', methods=['POST'])
@login_required
def api_user_comparison_property_add():
    """Add property to user's comparison"""
    from models import UserComparison, ComparisonProperty
    
    try:
        data = request.get_json()
        property_id = data.get('property_id')
        
        if not property_id:
            return jsonify({'success': False, 'message': 'Property ID is required'}), 400
        
        # Find or create active user comparison
        user_comparison = UserComparison.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).first()
        
        if not user_comparison:
            user_comparison = UserComparison(
                user_id=current_user.id,
                name='Мое сравнение',
                is_active=True
            )
            db.session.add(user_comparison)
            db.session.flush()
        
        # Check if property already in comparison
        existing = ComparisonProperty.query.filter_by(
            user_comparison_id=user_comparison.id,
            property_id=property_id
        ).first()
        
        if existing:
            count = ComparisonProperty.query.filter_by(
                user_comparison_id=user_comparison.id
            ).count()
            return jsonify({
                'success': True,
                'message': 'Объект уже в сравнении',
                'count': count
            })
        
        # Check maximum limit
        current_count = ComparisonProperty.query.filter_by(
            user_comparison_id=user_comparison.id
        ).count()
        
        if current_count >= 4:
            return jsonify({
                'success': False,
                'message': 'Максимум 4 объекта в сравнении',
                'count': current_count
            }), 400
        
        # Add property to comparison
        comparison_property = ComparisonProperty(
            user_comparison_id=user_comparison.id,
            property_id=property_id,
            property_name=data.get('property_name'),
            property_price=data.get('property_price'),
            complex_name=data.get('complex_name'),
            cashback=data.get('cashback', 0),
            area=data.get('area'),
            rooms=data.get('rooms'),
            order_index=current_count
        )
        db.session.add(comparison_property)
        db.session.commit()
        
        new_count = current_count + 1
        return jsonify({
            'success': True,
            'message': 'Объект добавлен в сравнение',
            'count': new_count
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error adding property to user comparison: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/user/comparison/property/remove', methods=['POST'])
@login_required
def api_user_comparison_property_remove():
    """Remove property from user's comparison"""
    from models import UserComparison, ComparisonProperty
    
    try:
        data = request.get_json()
        property_id = data.get('property_id')
        
        if not property_id:
            return jsonify({'success': False, 'message': 'Property ID is required'}), 400
        
        # Find active user comparison
        user_comparison = UserComparison.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).first()
        
        if not user_comparison:
            return jsonify({
                'success': True,
                'message': 'Сравнение пусто',
                'count': 0
            })
        
        # Find and delete property
        comparison_property = ComparisonProperty.query.filter_by(
            user_comparison_id=user_comparison.id,
            property_id=property_id
        ).first()
        
        if comparison_property:
            db.session.delete(comparison_property)
            db.session.commit()
        
        # Get updated count
        count = ComparisonProperty.query.filter_by(
            user_comparison_id=user_comparison.id
        ).count()
        
        return jsonify({
            'success': True,
            'message': 'Объект удален из сравнения',
            'count': count
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error removing property from user comparison: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/user/comparison/complex/add', methods=['POST'])
@login_required
def api_user_comparison_complex_add():
    """Add residential complex to user's comparison"""
    from models import UserComparison, ComparisonComplex
    
    try:
        data = request.get_json()
        complex_id = data.get('complex_id')
        
        if not complex_id:
            return jsonify({'success': False, 'message': 'Complex ID is required'}), 400
        
        # Find or create active user comparison
        user_comparison = UserComparison.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).first()
        
        if not user_comparison:
            user_comparison = UserComparison(
                user_id=current_user.id,
                name='Мое сравнение',
                is_active=True
            )
            db.session.add(user_comparison)
            db.session.flush()
        
        # Check if complex already in comparison
        existing = ComparisonComplex.query.filter_by(
            user_comparison_id=user_comparison.id,
            complex_id=complex_id
        ).first()
        
        if existing:
            count = ComparisonComplex.query.filter_by(
                user_comparison_id=user_comparison.id
            ).count()
            return jsonify({
                'success': True,
                'message': 'ЖК уже в сравнении',
                'count': count
            })
        
        # Check maximum limit
        current_count = ComparisonComplex.query.filter_by(
            user_comparison_id=user_comparison.id
        ).count()
        
        if current_count >= 4:
            return jsonify({
                'success': False,
                'message': 'Максимум 4 ЖК в сравнении',
                'count': current_count
            }), 400
        
        # Add complex to comparison
        comparison_complex = ComparisonComplex(
            user_comparison_id=user_comparison.id,
            complex_id=complex_id,
            complex_name=data.get('complex_name'),
            developer_name=data.get('developer_name'),
            min_price=data.get('min_price'),
            max_price=data.get('max_price'),
            district=data.get('district'),
            photo=data.get('photo'),
            buildings_count=data.get('buildings_count'),
            apartments_count=data.get('apartments_count'),
            completion_date=data.get('completion_date'),
            status=data.get('status'),
            complex_class=data.get('complex_class'),
            cashback_rate=data.get('cashback_rate', 5.0),
            order_index=current_count
        )
        db.session.add(comparison_complex)
        db.session.commit()
        
        new_count = current_count + 1
        return jsonify({
            'success': True,
            'message': 'ЖК добавлен в сравнение',
            'count': new_count
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error adding complex to user comparison: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/user/comparison/complex/remove', methods=['POST'])
@login_required
def api_user_comparison_complex_remove():
    """Remove residential complex from user's comparison"""
    from models import UserComparison, ComparisonComplex
    
    try:
        data = request.get_json()
        complex_id = data.get('complex_id')
        
        if not complex_id:
            return jsonify({'success': False, 'message': 'Complex ID is required'}), 400
        
        # Find active user comparison
        user_comparison = UserComparison.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).first()
        
        if not user_comparison:
            return jsonify({
                'success': True,
                'message': 'Сравнение пусто',
                'count': 0
            })
        
        # Find and delete complex
        comparison_complex = ComparisonComplex.query.filter_by(
            user_comparison_id=user_comparison.id,
            complex_id=complex_id
        ).first()
        
        if comparison_complex:
            db.session.delete(comparison_complex)
            db.session.commit()
        
        # Get updated count
        count = ComparisonComplex.query.filter_by(
            user_comparison_id=user_comparison.id
        ).count()
        
        return jsonify({
            'success': True,
            'message': 'ЖК удален из сравнения',
            'count': count
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error removing complex from user comparison: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/user/comparison/load')
def api_user_comparison_load():
    """Load all comparisons for current user from database"""
    from models import UserComparison, ComparisonProperty, ComparisonComplex
    
    try:
        # Handle unauthenticated users - return empty arrays instead of redirecting
        if not current_user.is_authenticated:
            return jsonify({
                'success': True,
                'properties': [],
                'complexes': [],
                'properties_count': 0,
                'complexes_count': 0
            })
        
        # Find active user comparison
        user_comparison = UserComparison.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).first()
        
        if not user_comparison:
            return jsonify({
                'success': True,
                'properties': [],
                'complexes': [],
                'properties_count': 0,
                'complexes_count': 0
            })
        
        # Get all property IDs
        properties = ComparisonProperty.query.filter_by(
            user_comparison_id=user_comparison.id
        ).all()
        property_ids = [prop.property_id for prop in properties]
        
        # Get all complex IDs
        complexes = ComparisonComplex.query.filter_by(
            user_comparison_id=user_comparison.id
        ).all()
        complex_ids = [comp.complex_id for comp in complexes]
        
        return jsonify({
            'success': True,
            'properties': property_ids,
            'complexes': complex_ids,
            'properties_count': len(property_ids),
            'complexes_count': len(complex_ids)
        })
        
    except Exception as e:
        print(f"Error loading user comparison: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/comparison/clear', methods=['POST'])
def api_comparison_clear():
    """Clear all comparisons for current user"""
    from models import UserComparison, ComparisonProperty, ComparisonComplex
    
    try:
        # Handle unauthenticated users
        if not current_user.is_authenticated:
            return jsonify({
                'success': True,
                'message': 'Сравнения очищены (локально)'
            })
        
        # Find active user comparison
        user_comparison = UserComparison.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).first()
        
        if user_comparison:
            # Delete all properties
            ComparisonProperty.query.filter_by(
                user_comparison_id=user_comparison.id
            ).delete()
            
            # Delete all complexes
            ComparisonComplex.query.filter_by(
                user_comparison_id=user_comparison.id
            ).delete()
            
            db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Все сравнения очищены'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error clearing comparisons: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ========================================
# PUBLIC COMPARISON DELETE ENDPOINTS (No Auth Required)
# ========================================

@app.route('/api/comparison/remove-property', methods=['POST', 'DELETE'])
@csrf.exempt
def api_comparison_remove_property():
    """Remove property from comparison (works for both authenticated and unauthenticated users)"""
    from models import UserComparison, ManagerComparison, ComparisonProperty
    
    try:
        # Get property_id from request
        if request.method == 'DELETE':
            data = request.get_json() or {}
        else:
            data = request.get_json() or {}
        
        property_id = data.get('property_id')
        
        if not property_id:
            return jsonify({'success': False, 'error': 'Property ID is required'}), 400
        
        # Try to remove from database if user is authenticated
        deleted = False
        
        # Check if regular user is authenticated
        if current_user.is_authenticated:
            user_comparison = UserComparison.query.filter_by(
                user_id=current_user.id,
                is_active=True
            ).first()
            
            if user_comparison:
                comparison_property = ComparisonProperty.query.filter_by(
                    user_comparison_id=user_comparison.id,
                    property_id=property_id
                ).first()
                
                if comparison_property:
                    db.session.delete(comparison_property)
                    db.session.commit()
                    deleted = True
        
        # Check if manager is authenticated
        if isinstance(current_user._get_current_object(), Manager) and not deleted:
            current_manager = current_user
            manager_comparison = ManagerComparison.query.filter_by(
                manager_id=current_manager.id,
                is_active=True
            ).first()
            
            if manager_comparison:
                comparison_property = ComparisonProperty.query.filter_by(
                    manager_comparison_id=manager_comparison.id,
                    property_id=property_id
                ).first()
                
                if comparison_property:
                    db.session.delete(comparison_property)
                    db.session.commit()
                    deleted = True
        
        # Return success even if not in database (frontend will handle localStorage)
        return jsonify({
            'success': True,
            'message': 'Квартира удалена из сравнения',
            'deleted_from_db': deleted
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"❌ Error removing property from comparison: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/comparison/remove-complex', methods=['POST', 'DELETE'])
@csrf.exempt
def api_comparison_remove_complex():
    """Remove complex from comparison (works for both authenticated and unauthenticated users)"""
    from models import UserComparison, ManagerComparison, ComparisonComplex
    
    try:
        # Get complex_id from request
        if request.method == 'DELETE':
            data = request.get_json() or {}
        else:
            data = request.get_json() or {}
        
        complex_id = data.get('complex_id')
        
        if not complex_id:
            return jsonify({'success': False, 'error': 'Complex ID is required'}), 400
        
        # Try to remove from database if user is authenticated
        deleted = False
        
        # Check if regular user is authenticated
        if current_user.is_authenticated:
            user_comparison = UserComparison.query.filter_by(
                user_id=current_user.id,
                is_active=True
            ).first()
            
            if user_comparison:
                comparison_complex = ComparisonComplex.query.filter_by(
                    user_comparison_id=user_comparison.id,
                    complex_id=complex_id
                ).first()
                
                if comparison_complex:
                    db.session.delete(comparison_complex)
                    db.session.commit()
                    deleted = True
        
        # Check if manager is authenticated
        if isinstance(current_user._get_current_object(), Manager) and not deleted:
            current_manager = current_user
            manager_comparison = ManagerComparison.query.filter_by(
                manager_id=current_manager.id,
                is_active=True
            ).first()
            
            if manager_comparison:
                comparison_complex = ComparisonComplex.query.filter_by(
                    manager_comparison_id=manager_comparison.id,
                    complex_id=complex_id
                ).first()
                
                if comparison_complex:
                    db.session.delete(comparison_complex)
                    db.session.commit()
                    deleted = True
        
        # Return success even if not in database (frontend will handle localStorage)
        return jsonify({
            'success': True,
            'message': 'ЖК удален из сравнения',
            'deleted_from_db': deleted
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"❌ Error removing complex from comparison: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ========================================
# MANAGER COMPARISON ENDPOINTS
# ========================================

@app.route('/api/manager/comparison/property/add', methods=['POST'])
@manager_required
def api_manager_comparison_property_add():
    """Add property to manager's comparison"""
    from models import ManagerComparison, ComparisonProperty
    
    try:
        data = request.get_json()
        property_id = data.get('property_id')
        
        if not property_id:
            return jsonify({'success': False, 'message': 'Property ID is required'}), 400
        
        current_manager = current_user
        
        # Find or create active manager comparison
        manager_comparison = ManagerComparison.query.filter_by(
            manager_id=current_manager.id,
            is_active=True
        ).first()
        
        if not manager_comparison:
            manager_comparison = ManagerComparison(
                manager_id=current_manager.id,
                name='Сравнение для клиента',
                is_active=True
            )
            db.session.add(manager_comparison)
            db.session.flush()
        
        # Check if property already in comparison
        existing = ComparisonProperty.query.filter_by(
            manager_comparison_id=manager_comparison.id,
            property_id=property_id
        ).first()
        
        if existing:
            count = ComparisonProperty.query.filter_by(
                manager_comparison_id=manager_comparison.id
            ).count()
            return jsonify({
                'success': True,
                'message': 'Объект уже в сравнении',
                'count': count
            })
        
        # Check maximum limit
        current_count = ComparisonProperty.query.filter_by(
            manager_comparison_id=manager_comparison.id
        ).count()
        
        if current_count >= 4:
            return jsonify({
                'success': False,
                'message': 'Максимум 4 объекта в сравнении',
                'count': current_count
            }), 400
        
        # Add property to comparison
        comparison_property = ComparisonProperty(
            manager_comparison_id=manager_comparison.id,
            property_id=property_id,
            property_name=data.get('property_name'),
            property_price=data.get('property_price'),
            complex_name=data.get('complex_name'),
            cashback=data.get('cashback', 0),
            area=data.get('area'),
            rooms=data.get('rooms'),
            order_index=current_count
        )
        db.session.add(comparison_property)
        db.session.commit()
        
        new_count = current_count + 1
        return jsonify({
            'success': True,
            'message': 'Объект добавлен в сравнение',
            'count': new_count
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error adding property to manager comparison: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/manager/comparison/property/remove', methods=['POST'])
@manager_required
def api_manager_comparison_property_remove():
    """Remove property from manager's comparison"""
    from models import ManagerComparison, ComparisonProperty
    
    try:
        data = request.get_json()
        property_id = data.get('property_id')
        
        if not property_id:
            return jsonify({'success': False, 'message': 'Property ID is required'}), 400
        
        current_manager = current_user
        
        # Find active manager comparison
        manager_comparison = ManagerComparison.query.filter_by(
            manager_id=current_manager.id,
            is_active=True
        ).first()
        
        if not manager_comparison:
            return jsonify({
                'success': True,
                'message': 'Сравнение пусто',
                'count': 0
            })
        
        # Find and delete property
        comparison_property = ComparisonProperty.query.filter_by(
            manager_comparison_id=manager_comparison.id,
            property_id=property_id
        ).first()
        
        if comparison_property:
            db.session.delete(comparison_property)
            db.session.commit()
        
        # Get updated count
        count = ComparisonProperty.query.filter_by(
            manager_comparison_id=manager_comparison.id
        ).count()
        
        return jsonify({
            'success': True,
            'message': 'Объект удален из сравнения',
            'count': count
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error removing property from manager comparison: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/manager/comparison/complex/add', methods=['POST'])
@manager_required
def api_manager_comparison_complex_add():
    """Add residential complex to manager's comparison"""
    from models import ManagerComparison, ComparisonComplex
    
    try:
        data = request.get_json()
        complex_id = data.get('complex_id')
        
        if not complex_id:
            return jsonify({'success': False, 'message': 'Complex ID is required'}), 400
        
        current_manager = current_user
        
        # Find or create active manager comparison
        manager_comparison = ManagerComparison.query.filter_by(
            manager_id=current_manager.id,
            is_active=True
        ).first()
        
        if not manager_comparison:
            manager_comparison = ManagerComparison(
                manager_id=current_manager.id,
                name='Сравнение для клиента',
                is_active=True
            )
            db.session.add(manager_comparison)
            db.session.flush()
        
        # Check if complex already in comparison
        existing = ComparisonComplex.query.filter_by(
            manager_comparison_id=manager_comparison.id,
            complex_id=complex_id
        ).first()
        
        if existing:
            count = ComparisonComplex.query.filter_by(
                manager_comparison_id=manager_comparison.id
            ).count()
            return jsonify({
                'success': True,
                'message': 'ЖК уже в сравнении',
                'count': count
            })
        
        # Check maximum limit
        current_count = ComparisonComplex.query.filter_by(
            manager_comparison_id=manager_comparison.id
        ).count()
        
        if current_count >= 4:
            return jsonify({
                'success': False,
                'message': 'Максимум 4 ЖК в сравнении',
                'count': current_count
            }), 400
        
        # Add complex to comparison
        comparison_complex = ComparisonComplex(
            manager_comparison_id=manager_comparison.id,
            complex_id=complex_id,
            complex_name=data.get('complex_name'),
            developer_name=data.get('developer_name'),
            min_price=data.get('min_price'),
            max_price=data.get('max_price'),
            district=data.get('district'),
            photo=data.get('photo'),
            buildings_count=data.get('buildings_count'),
            apartments_count=data.get('apartments_count'),
            completion_date=data.get('completion_date'),
            status=data.get('status'),
            complex_class=data.get('complex_class'),
            cashback_rate=data.get('cashback_rate', 5.0),
            order_index=current_count
        )
        db.session.add(comparison_complex)
        db.session.commit()
        
        new_count = current_count + 1
        return jsonify({
            'success': True,
            'message': 'ЖК добавлен в сравнение',
            'count': new_count
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error adding complex to manager comparison: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/manager/comparison/complex/remove', methods=['POST'])
@manager_required
def api_manager_comparison_complex_remove():
    """Remove residential complex from manager's comparison"""
    from models import ManagerComparison, ComparisonComplex
    
    try:
        data = request.get_json()
        complex_id = data.get('complex_id')
        
        if not complex_id:
            return jsonify({'success': False, 'message': 'Complex ID is required'}), 400
        
        current_manager = current_user
        
        # Find active manager comparison
        manager_comparison = ManagerComparison.query.filter_by(
            manager_id=current_manager.id,
            is_active=True
        ).first()
        
        if not manager_comparison:
            return jsonify({
                'success': True,
                'message': 'Сравнение пусто',
                'count': 0
            })
        
        # Find and delete complex
        comparison_complex = ComparisonComplex.query.filter_by(
            manager_comparison_id=manager_comparison.id,
            complex_id=complex_id
        ).first()
        
        if comparison_complex:
            db.session.delete(comparison_complex)
            db.session.commit()
        
        # Get updated count
        count = ComparisonComplex.query.filter_by(
            manager_comparison_id=manager_comparison.id
        ).count()
        
        return jsonify({
            'success': True,
            'message': 'ЖК удален из сравнения',
            'count': count
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error removing complex from manager comparison: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/manager/comparison/load')
def api_manager_comparison_load():
    print(f"🔍 DEBUG: /api/manager/comparison/load called")
    print(f"🔍 DEBUG: current_user.is_authenticated = {current_user.is_authenticated}")
    """Load all comparisons for current manager from database (no auth required to prevent redirects)"""
    from models import ManagerComparison, ComparisonProperty, ComparisonComplex, Manager
    
    try:
        # ИСПРАВЛЕНО: правильная проверка типа менеджера
        if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
            print(f"⚠️ DEBUG: User not authenticated or not a manager - returning empty comparison")
            return jsonify({
                'success': True,
                'properties': [],
                'complexes': [],
                'properties_count': 0,
                'complexes_count': 0
            })
        
        current_manager = current_user
        print(f"🔍 DEBUG: Loading comparisons from MANAGER_COMPARISONS table for manager {current_manager.id}")
        
        # Find active manager comparison
        manager_comparison = ManagerComparison.query.filter_by(
            manager_id=current_manager.id,
            is_active=True
        ).first()
        
        if not manager_comparison:
            return jsonify({
                'success': True,
                'properties': [],
                'complexes': [],
                'properties_count': 0,
                'complexes_count': 0
            })
        
        # Get all property IDs
        properties = ComparisonProperty.query.filter_by(
            manager_comparison_id=manager_comparison.id
        ).all()
        property_ids = [prop.property_id for prop in properties]
        
        # Get all complex IDs
        complexes = ComparisonComplex.query.filter_by(
            manager_comparison_id=manager_comparison.id
        ).all()
        complex_ids = [comp.complex_id for comp in complexes]
        
        return jsonify({
            'success': True,
            'properties': property_ids,
            'complexes': complex_ids,
            'properties_count': len(property_ids),
            'complexes_count': len(complex_ids)
        })
        
    except Exception as e:
        print(f"Error loading manager comparison: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ========================================
# PHONE VERIFICATION
# ========================================

@app.route('/api/user/phone/send-verification', methods=['POST'])
@login_required
def send_phone_verification():
    """Send verification code to user's phone"""
    from models import User
    import random
    from datetime import datetime, timedelta
    from scripts.sms_service import send_verification_code_sms
    
    try:
        data = request.get_json()
        phone = data.get('phone')
        
        if not phone:
            return jsonify({'success': False, 'message': 'Номер телефона обязателен'}), 400
        
        # Generate 6-digit verification code
        code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
        
        # Save code and expiration to user
        current_user.phone = phone
        current_user.phone_verification_code = code
        current_user.phone_verification_expires = datetime.utcnow() + timedelta(minutes=10)
        db.session.commit()
        
        # Send SMS
        sms_sent = send_verification_code_sms(phone, code)
        
        if sms_sent:
            return jsonify({
                'success': True,
                'message': 'Код отправлен на ваш номер'
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Ошибка отправки SMS. Попробуйте позже.'
            }), 500
            
    except Exception as e:
        db.session.rollback()
        print(f"Error sending verification code: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/user/phone/verify-code', methods=['POST'])
@login_required
def verify_phone_code():
    """Verify phone number with code"""
    from models import User
    from datetime import datetime
    
    try:
        data = request.get_json()
        code = data.get('code')
        
        if not code:
            return jsonify({'success': False, 'message': 'Код обязателен'}), 400
        
        # Check if code matches and not expired
        if not current_user.phone_verification_code:
            return jsonify({'success': False, 'message': 'Код не был отправлен'}), 400
        
        if current_user.phone_verification_expires < datetime.utcnow():
            return jsonify({'success': False, 'message': 'Код истек. Запросите новый.'}), 400
        
        if current_user.phone_verification_code != code:
            return jsonify({'success': False, 'message': 'Неверный код'}), 400
        
        # Mark phone as verified
        current_user.phone_verified = True
        current_user.phone_verification_code = None
        current_user.phone_verification_expires = None
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Телефон успешно подтвержден!'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error verifying phone code: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# ========================================
# PRESENTATION SHARING (Manager to Client)
# ========================================

@app.route('/api/manager/collection/<int:collection_id>/assign-client', methods=['POST'])
@manager_required
def assign_client_to_presentation(collection_id):
    """Assign client to presentation"""
    from models import Collection, User
    
    try:
        current_manager = current_user
        data = request.get_json()
        
        if not data or 'client_id' not in data:
            return jsonify({'success': False, 'error': 'ID клиента не указан'}), 400
        
        client_id = data['client_id']
        
        # Get collection
        collection = Collection.query.filter_by(
            id=collection_id,
            created_by_manager_id=current_manager.id
        ).first()
        
        if not collection:
            return jsonify({'success': False, 'error': 'Презентация не найдена'}), 404
        
        # Verify client exists and is assigned to this manager
        client = User.query.filter_by(id=client_id, assigned_manager_id=current_manager.id).first()
        
        if not client:
            return jsonify({'success': False, 'error': 'Клиент не найден или не закреплен за вами'}), 404
        
        # Assign client to presentation
        collection.assigned_to_user_id = client_id
        collection.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Клиент {client.full_name} назначен презентации'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error assigning client to presentation: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/collection/<int:collection_id>/send-to-client', methods=['POST'])
@manager_required
def send_presentation_to_client(collection_id):
    """Send presentation from manager to client's personal dashboard"""
    from models import Collection, User, UserNotification
    
    try:
        current_manager = current_user
        
        # Get collection
        collection = Collection.query.filter_by(
            id=collection_id,
            created_by_manager_id=current_manager.id
        ).first()
        
        if not collection:
            return jsonify({'success': False, 'error': 'Презентация не найдена'}), 404
        
        if not collection.assigned_to_user_id:
            return jsonify({'success': False, 'error': 'Клиент не назначен'}), 400
        
        if len(collection.properties) == 0:
            return jsonify({'success': False, 'error': 'В презентации нет объектов'}), 400
        
        # Update collection status
        collection.status = 'Отправлена'
        collection.sent_at = datetime.utcnow()
        collection.updated_at = datetime.utcnow()
        
        # Create notification for client
        notification = UserNotification(
            user_id=collection.assigned_to_user_id,
            title='📦 Новая презентация от менеджера',
            message=f'Менеджер отправил вам презентацию "{collection.title}" с {len(collection.properties)} объектами',
            notification_type='success',
            icon='fas fa-gift',
            action_url=f'/dashboard#presentations'
        )
        db.session.add(notification)
        
        # Логируем отправку презентации
        from models import UserActivity
        UserActivity.log_activity(
            user_id=collection.assigned_to_user_id,
            activity_type='presentation_received',
            description=f'Получена новая презентация от менеджера: {collection.title} ({len(collection.properties)} объектов)'
        )
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Презентация отправлена клиенту'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error sending presentation to client: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/presentation/<int:presentation_id>/delete', methods=['DELETE'])
@login_required
def delete_user_presentation(presentation_id):
    """Delete presentation from user's dashboard"""
    from models import Collection
    
    try:
        # Get collection
        collection = Collection.query.filter_by(
            id=presentation_id,
            assigned_to_user_id=current_user.id
        ).first()
        
        if not collection:
            return jsonify({'success': False, 'error': 'Презентация не найдена'}), 404
        
        # Delete the collection
        db.session.delete(collection)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Презентация удалена'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting presentation: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/deals')
def api_manager_deals_count():
    """Get deals data for manager with count - used for preloading deals tab counter"""
    from models import Deal
    
    try:
        # Check if current user is a manager
        if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
            return jsonify({
                'success': True,
                'deals': [],
                'total': 0,
                'is_manager': False
            })
        
        current_manager = current_user
        
        # Get all deals for this manager
        deals = Deal.query.filter_by(manager_id=current_manager.id).all()
        
        deals_data = []
        for deal in deals:
            deals_data.append({
                'id': deal.id,
                'deal_number': deal.deal_number,
                'client_name': deal.client.full_name if deal.client else 'Unknown',
                'property_description': deal.property_description,
                'property_price': deal.property_price,
                'cashback_amount': deal.cashback_amount,
                'status': deal.status,
                'created_at': deal.created_at.isoformat() if deal.created_at else None
            })
        
        return jsonify({
            'success': True,
            'deals': deals_data,
            'total': len(deals_data),
            'is_manager': True
        })
        
    except Exception as e:
        print(f"Error getting deals count: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/comparison/count')
def api_manager_comparison_count():
    """Get count of items in manager comparison for navigation counter"""
    from models import ManagerComparison, ComparisonProperty, ComparisonComplex
    
    try:
        # Check if current user is a manager
        if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
            return jsonify({
                'success': True,
                'properties_count': 0,
                'complexes_count': 0,
                'total_count': 0
            })
        
        current_manager = current_user
        
        # Find active manager comparison
        manager_comparison = ManagerComparison.query.filter_by(
            manager_id=current_manager.id,
            is_active=True
        ).first()
        
        if not manager_comparison:
            return jsonify({
                'success': True,
                'properties_count': 0,
                'complexes_count': 0,
                'total_count': 0
            })
        
        # Count properties and complexes
        properties_count = ComparisonProperty.query.filter_by(
            manager_comparison_id=manager_comparison.id
        ).count()
        
        complexes_count = ComparisonComplex.query.filter_by(
            manager_comparison_id=manager_comparison.id
        ).count()
        
        total_count = properties_count + complexes_count
        
        return jsonify({
            'success': True,
            'properties_count': properties_count,
            'complexes_count': complexes_count,
            'total_count': total_count
        })
        
    except Exception as e:
        print(f"Error getting manager comparison count: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/manager/generate-comparison-pdf', methods=['POST'])
@csrf.exempt
def api_manager_generate_comparison_pdf():
    """Generate HTML comparison document for manager to send to client"""
    try:
        from models import Manager
        from datetime import datetime
        
        data = request.get_json()
        
        recipient_name = data.get('recipient_name', 'Клиент')
        message_notes = data.get('message_notes', '')
        hide_complex_names = data.get('hide_complex_names', False)
        hide_developer_names = data.get('hide_developer_names', False)
        hide_addresses = data.get('hide_addresses', False)
        properties = data.get('properties', [])
        complexes = data.get('complexes', [])
        
        # Get current manager info
        manager = current_user if current_user.is_authenticated and isinstance(current_user._get_current_object(), Manager) else None
        manager_name = manager.full_name if manager else "InBack Менеджер"
        manager_phone = manager.phone if manager else "8 (862) 266-62-16"
        manager_email = manager.email if manager else "info@inback.ru"
        
        # Get manager avatar with full URL for downloadable HTML
        if manager and manager.profile_image:
            # If it's already a full URL (http/https), use it
            if manager.profile_image.startswith('http'):
                manager_avatar = manager.profile_image
            else:
                # Convert relative path to absolute URL
                base_url = request.host_url.rstrip('/')
                manager_avatar = f"{base_url}{manager.profile_image}"
        else:
            manager_avatar = "https://randomuser.me/api/portraits/men/45.jpg"
        
        # Get current date and time
        now = datetime.now()
        date_str = now.strftime('%d.%m.%Y')
        time_str = now.strftime('%H:%M')
        
        print(f"📄 Generating comparison PDF: {len(properties)} properties, {len(complexes)} complexes")
        
        # Build HTML document for comparison
        html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Сравнение недвижимости - {recipient_name}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Arial', 'Helvetica', sans-serif;
            line-height: 1.6;
            color: #000;
            background: #fff;
            padding: 40px 20px;
            max-width: 1200px;
            margin: 0 auto;
        }}
        
        /* Print styles */
        @media print {{
            body {{
                padding: 20px;
            }}
            .no-print {{
                display: none !important;
            }}
            .page-break {{
                page-break-after: always;
            }}
        }}
        
        /* Header */
        .header {{
            text-align: center;
            margin-bottom: 40px;
            padding-bottom: 20px;
            border-bottom: 3px solid #000;
        }}
        
        .header h1 {{
            font-size: 28px;
            font-weight: bold;
            margin-bottom: 5px;
        }}
        
        .header .subtitle {{
            font-size: 18px;
            color: #555;
            margin-bottom: 20px;
        }}
        
        /* Manager info */
        .manager-info {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 20px;
            margin: 30px 0;
            padding: 20px;
            background: #f8f8f8;
            border-radius: 8px;
        }}
        
        .manager-avatar {{
            width: 80px;
            height: 80px;
            border-radius: 50%;
            object-fit: cover;
            border: 3px solid #333;
        }}
        
        .manager-details {{
            text-align: left;
        }}
        
        .manager-details p {{
            margin: 5px 0;
            font-size: 14px;
        }}
        
        .manager-details strong {{
            font-weight: bold;
        }}
        
        /* Recipient info */
        .recipient-info {{
            text-align: center;
            margin: 20px 0;
            padding: 15px;
            font-size: 16px;
        }}
        
        .recipient-info strong {{
            font-weight: bold;
        }}
        
        /* Notes */
        .notes {{
            margin: 20px 0;
            padding: 15px;
            background: #f0f0f0;
            border-left: 4px solid #333;
        }}
        
        .notes strong {{
            display: block;
            margin-bottom: 10px;
            font-weight: bold;
        }}
        
        /* Section title */
        .section-title {{
            font-size: 20px;
            font-weight: bold;
            margin: 30px 0 15px 0;
            padding: 10px 0;
            border-bottom: 2px solid #000;
        }}
        
        /* Table */
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            font-size: 12px;
        }}
        
        table th {{
            background: #f0f0f0;
            border: 1px solid #ddd;
            padding: 10px;
            text-align: left;
            font-weight: bold;
        }}
        
        table td {{
            border: 1px solid #ddd;
            padding: 10px;
            vertical-align: top;
        }}
        
        table tr:nth-child(even) {{
            background: #f9f9f9;
        }}
        
        table .label-col {{
            font-weight: bold;
            background: #f5f5f5;
            width: 180px;
        }}
        
        /* Footer */
        .footer {{
            text-align: center;
            margin-top: 50px;
            padding-top: 20px;
            border-top: 2px solid #000;
            font-size: 14px;
        }}
        
        .footer p {{
            margin: 5px 0;
        }}
        
        /* Print button */
        .print-btn {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: #000;
            color: #fff;
            border: none;
            padding: 12px 24px;
            font-size: 16px;
            cursor: pointer;
            border-radius: 5px;
            z-index: 1000;
        }}
        
        .print-btn:hover {{
            background: #333;
        }}
    </style>
</head>
<body>
    <button onclick="window.print()" class="print-btn no-print">🖨️ Печать</button>
    
    <div class="header">
        <h1>InBack Недвижимость</h1>
        <div class="subtitle">Сравнение объектов недвижимости</div>
    </div>
    
    <div class="manager-info">
        <img src="{manager_avatar}" alt="Фото менеджера" class="manager-avatar">
        <div class="manager-details">
            <p><strong>Ваш персональный менеджер: {manager_name}</strong></p>
            <p>Телефон: {manager_phone}</p>
            <p>Email: {manager_email}</p>
        </div>
    </div>
    
    <div class="recipient-info">
        <p><strong>Подготовлено для: {recipient_name}</strong></p>
        <p>Дата: {date_str} в {time_str}</p>
    </div>
"""
        
        # Add notes if provided
        if message_notes:
            html_content += f"""
    <div class="notes">
        <strong>Заметки:</strong>
        <p>{message_notes}</p>
    </div>
"""
        
        # Add properties comparison table (vertical layout)
        if properties and len(properties) > 0:
            html_content += """
    <h2 class="section-title">Сравнение квартир</h2>
    <table>
        <thead>
            <tr>
                <th class="label-col">Характеристика</th>"""
            
            # Add column headers for each property
            for i, prop in enumerate(properties, 1):
                html_content += f"\n                <th>Объект {i}</th>"
            
            html_content += """
            </tr>
        </thead>
        <tbody>
"""
            
            # Row: Название
            html_content += """
            <tr>
                <td class="label-col">Название</td>"""
            for prop in properties:
                html_content += f"\n                <td>{prop.get('property_name', 'Не указано')}</td>"
            html_content += "\n            </tr>"
            
            # Row: ЖК (if not hidden)
            if not hide_complex_names:
                html_content += """
            <tr>
                <td class="label-col">ЖК</td>"""
                for prop in properties:
                    html_content += f"\n                <td>{prop.get('complex_name', 'Не указано')}</td>"
                html_content += "\n            </tr>"
            
            # Row: Цена
            html_content += """
            <tr>
                <td class="label-col">Цена</td>"""
            for prop in properties:
                price = prop.get('property_price', 0)
                html_content += f"\n                <td>{price:,.0f} ₽</td>"
            html_content += "\n            </tr>"
            
            # Row: Площадь
            html_content += """
            <tr>
                <td class="label-col">Площадь</td>"""
            for prop in properties:
                size = prop.get('property_size', 'Не указано')
                html_content += f"\n                <td>{size} м²</td>"
            html_content += "\n            </tr>"
            
            # Row: Комнаты
            html_content += """
            <tr>
                <td class="label-col">Комнаты</td>"""
            for prop in properties:
                rooms = prop.get('rooms', 'Не указано')
                # Show "Студия" instead of "0"
                if rooms == 0 or rooms == '0':
                    rooms_display = 'Студия'
                else:
                    rooms_display = rooms
                html_content += f"\n                <td>{rooms_display}</td>"
            html_content += "\n            </tr>"
            
            # Row: Этаж
            html_content += """
            <tr>
                <td class="label-col">Этаж</td>"""
            for prop in properties:
                floor = prop.get('floor', 'Не указано')
                html_content += f"\n                <td>{floor}</td>"
            html_content += "\n            </tr>"
            
            # Row: Всего этажей
            html_content += """
            <tr>
                <td class="label-col">Всего этажей</td>"""
            for prop in properties:
                total_floors = prop.get('total_floors', 'Не указано')
                html_content += f"\n                <td>{total_floors}</td>"
            html_content += "\n            </tr>"
            
            # Row: Район
            html_content += """
            <tr>
                <td class="label-col">Район</td>"""
            for prop in properties:
                district = prop.get('district', 'Не указано')
                html_content += f"\n                <td>{district}</td>"
            html_content += "\n            </tr>"
            
            # Row: Застройщик (if not hidden)
            if not hide_developer_names:
                html_content += """
            <tr>
                <td class="label-col">Застройщик</td>"""
                for prop in properties:
                    developer = prop.get('developer_name', 'Не указано')
                    html_content += f"\n                <td>{developer}</td>"
                html_content += "\n            </tr>"
            
            # Row: Адрес (if not hidden)
            if not hide_addresses:
                html_content += """
            <tr>
                <td class="label-col">Адрес</td>"""
                for prop in properties:
                    address = prop.get('address', 'Не указано')
                    html_content += f"\n                <td>{address}</td>"
                html_content += "\n            </tr>"
            
            html_content += """
        </tbody>
    </table>
"""
        
        # Add complexes comparison table (vertical layout)
        if complexes and len(complexes) > 0:
            html_content += """
    <div class="page-break"></div>
    <h2 class="section-title">Сравнение жилых комплексов</h2>
    <table>
        <thead>
            <tr>
                <th class="label-col">Характеристика</th>"""
            
            # Add column headers for each complex
            for i, complex_data in enumerate(complexes, 1):
                html_content += f"\n                <th>ЖК {i}</th>"
            
            html_content += """
            </tr>
        </thead>
        <tbody>
"""
            
            # Row: Название ЖК
            if not hide_complex_names:
                html_content += """
            <tr>
                <td class="label-col">Название ЖК</td>"""
                for complex_data in complexes:
                    name = complex_data.get('name', 'Не указано')
                    html_content += f"\n                <td><strong>{name}</strong></td>"
                html_content += "\n            </tr>"
            
            # Row: Класс жилья
            html_content += """
            <tr>
                <td class="label-col">Класс жилья</td>"""
            for complex_data in complexes:
                obj_class = complex_data.get('object_class', 'Не указано')
                html_content += f"\n                <td>{obj_class}</td>"
            html_content += "\n            </tr>"
            
            # Row: Цена от
            html_content += """
            <tr>
                <td class="label-col">Цена от</td>"""
            for complex_data in complexes:
                min_price = complex_data.get('min_price', 0)
                html_content += f"\n                <td>{min_price:,.0f} ₽</td>"
            html_content += "\n            </tr>"
            
            # Row: Цена до
            html_content += """
            <tr>
                <td class="label-col">Цена до</td>"""
            for complex_data in complexes:
                max_price = complex_data.get('max_price', 0)
                html_content += f"\n                <td>{max_price:,.0f} ₽</td>"
            html_content += "\n            </tr>"
            
            # Row: Срок сдачи
            html_content += """
            <tr>
                <td class="label-col">Срок сдачи</td>"""
            for complex_data in complexes:
                completion = complex_data.get('completion_date', 'Не указано')
                html_content += f"\n                <td>{completion}</td>"
            html_content += "\n            </tr>"
            
            # Row: Кешбек
            html_content += """
            <tr>
                <td class="label-col">Кешбек</td>"""
            for complex_data in complexes:
                cashback_rate = complex_data.get('cashback_rate', 0)
                html_content += f"\n                <td>{cashback_rate}%</td>"
            html_content += "\n            </tr>"
            
            # Row: Застройщик (if not hidden)
            if not hide_developer_names:
                html_content += """
            <tr>
                <td class="label-col">Застройщик</td>"""
                for complex_data in complexes:
                    developer = complex_data.get('developer', 'Не указано')
                    html_content += f"\n                <td>{developer}</td>"
                html_content += "\n            </tr>"
            
            # Row: Адрес (if not hidden)
            if not hide_addresses:
                html_content += """
            <tr>
                <td class="label-col">Адрес</td>"""
                for complex_data in complexes:
                    address = complex_data.get('address', 'Не указано')
                    html_content += f"\n                <td>{address}</td>"
                html_content += "\n            </tr>"
            
            html_content += """
        </tbody>
    </table>
"""
        
        # Add footer
        html_content += f"""
    <div class="footer">
        <p><strong>InBack.ru</strong> - ваш кешбек за новостройки</p>
        <p>Документ создан {date_str} в {time_str}</p>
    </div>
</body>
</html>
"""
        
        # Return HTML for download
        response = app.response_class(
            response=html_content,
            status=200,
            mimetype='text/html; charset=utf-8'
        )
        
        # Set download filename with safe ASCII characters
        from datetime import datetime
        from unidecode import unidecode
        timestamp = datetime.now().strftime('%Y-%m-%d')
        # Convert recipient name to ASCII to avoid encoding issues
        safe_name = unidecode(recipient_name).replace(' ', '_')
        ascii_filename = f'Sravnenie_{safe_name}_{timestamp}.html'
        
        response.headers['Content-Disposition'] = f'attachment; filename="{ascii_filename}"'
        
        print(f"✅ Comparison HTML generated successfully")
        return response
        
    except Exception as e:
        print(f"❌ Error generating comparison PDF: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to generate comparison document', 'details': str(e)}), 500

@app.route('/api/recommendations/<int:rec_id>/respond', methods=['POST'])
@login_required
def api_respond_to_recommendation(rec_id):
    """Client responds to recommendation with interest/not interested"""
    from models import Recommendation
    from datetime import datetime
    
    try:
        data = request.get_json()
        response_type = data.get('response')  # 'interested' or 'not_interested'
        
        if response_type not in ['interested', 'not_interested']:
            return jsonify({'success': False, 'error': 'Неверный тип ответа'}), 400
            
        recommendation = Recommendation.query.filter_by(
            id=rec_id,
            client_id=current_user.id
        ).first()
        
        if not recommendation:
            return jsonify({'success': False, 'error': 'Рекомендация не найдена'}), 404
            
        recommendation.status = response_type
        recommendation.client_response = response_type
        recommendation.responded_at = datetime.utcnow()
        
        db.session.commit()
        
        # Notify manager about client response
        if recommendation.manager:
            try:
                from email_service import send_notification
                subject = f"Ответ клиента на рекомендацию: {recommendation.title}"
                message = f"""
Клиент {current_user.full_name} ответил на вашу рекомендацию:

Рекомендация: {recommendation.title}
Объект: {recommendation.item_name}
Ответ: {'Интересно' if response_type == 'interested' else 'Не интересно'}

Время ответа: {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
                send_notification(
                    recommendation.manager.email,
                    subject,
                    message,
                    notification_type="client_response"
                )
            except Exception as e:
                print(f"Error sending notification to manager: {e}")
        
        return jsonify({'success': True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/manager/recommendation-categories/<int:client_id>', methods=['GET'])
def api_get_recommendation_categories(client_id):
    """Get recommendation categories for a specific client"""
    from models import RecommendationCategory
    
    # Check if user is authenticated as manager
    if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
        return jsonify({'success': False, 'error': 'Требуется авторизация менеджера'}), 401
    
    current_manager = current_user
    
    try:
        categories = RecommendationCategory.query.filter_by(
            manager_id=current_manager.id,
            client_id=client_id,
            is_active=True
        ).order_by(RecommendationCategory.last_used.desc()).all()
        
        categories_data = []
        for category in categories:
            categories_data.append({
                'id': category.id,
                'name': category.name,
                'description': category.description,
                'color': category.color,
                'recommendations_count': category.recommendations_count,
                'last_used': category.last_used.strftime('%d.%m.%Y') if category.last_used else '',
                'created_at': category.created_at.strftime('%d.%m.%Y') if category.created_at else ''
            })
        
        return jsonify({
            'success': True,
            'categories': categories_data
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/recommendation-categories', methods=['POST'])
def api_create_recommendation_category():
    """Create new recommendation category"""
    from models import RecommendationCategory
    
    # Check if user is authenticated as manager
    if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
        return jsonify({'success': False, 'error': 'Требуется авторизация менеджера'}), 401
    
    current_manager = current_user
    
    try:
        data = request.get_json()
        category_name = data.get('name', '').strip()
        client_id = data.get('client_id')
        description = data.get('description', '').strip()
        color = data.get('color', 'blue')
        
        if not category_name or not client_id:
            return jsonify({'success': False, 'error': 'Название категории и клиент обязательны'}), 400
        
        # Check if category with this name already exists for this client
        existing = RecommendationCategory.query.filter_by(
            manager_id=current_manager.id,
            client_id=client_id,
            name=category_name,
            is_active=True
        ).first()
        
        if existing:
            return jsonify({'success': False, 'error': 'Категория с таким названием уже существует'}), 400
        
        # Create new category
        category = RecommendationCategory(
            name=category_name,
            description=description,
            manager_id=current_manager.id,
            client_id=client_id,
            color=color
        )
        
        db.session.add(category)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'category': {
                'id': category.id,
                'name': category.name,
                'description': category.description,
                'color': category.color,
                'recommendations_count': 0,
                'created_at': category.created_at.strftime('%d.%m.%Y')
            }
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/all-categories', methods=['GET'])
def api_manager_all_categories():
    """Get all categories created by this manager"""
    from models import RecommendationCategory, User
    
    # Check if user is authenticated as manager
    if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
        return jsonify({'success': False, 'error': 'Требуется авторизация менеджера'}), 401
    
    current_manager = current_user
    
    try:
        categories = db.session.query(
            RecommendationCategory, 
            User.email.label('client_email')
        ).outerjoin(
            User, RecommendationCategory.client_id == User.id
        ).filter(
            RecommendationCategory.manager_id == current_manager.id
        ).order_by(
            RecommendationCategory.last_used.desc().nulls_last(),
            RecommendationCategory.created_at.desc()
        ).all()
        
        category_data = []
        for category, client_email in categories:
            category_data.append({
                'id': category.id,
                'name': category.name,
                'description': category.description,
                'client_email': client_email or 'Общая категория',
                'recommendations_count': category.recommendations_count,
                'is_active': category.is_active,
                'last_used': category.last_used.isoformat() if category.last_used else None,
                'created_at': category.created_at.isoformat()
            })
        
        return jsonify({
            'success': True,
            'categories': category_data
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/categories/global', methods=['POST'])
def api_manager_create_global_category():
    """Create a new global category template"""
    from models import RecommendationCategory
    
    # Check if user is authenticated as manager
    if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
        return jsonify({'success': False, 'error': 'Требуется авторизация менеджера'}), 401
    
    current_manager = current_user
    
    data = request.get_json()
    name = data.get('name', '').strip()
    description = data.get('description', '').strip()
    
    if not name:
        return jsonify({'success': False, 'error': 'Укажите название категории'}), 400
    
    try:
        # Create a template category without specific client
        category = RecommendationCategory(
            name=name,
            description=description,
            manager_id=current_manager.id,
            client_id=None,  # Global template
            is_template=True,
            recommendations_count=0
        )
        
        db.session.add(category)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'category': {
                'id': category.id,
                'name': category.name,
                'description': category.description
            }
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/categories/<int:category_id>/toggle', methods=['POST'])
def api_manager_toggle_category(category_id):
    """Toggle category active status"""
    from models import RecommendationCategory
    
    # Check if user is authenticated as manager
    if not current_user.is_authenticated or not isinstance(current_user._get_current_object(), Manager):
        return jsonify({'success': False, 'error': 'Требуется авторизация менеджера'}), 401
    
    current_manager = current_user
    
    data = request.get_json()
    is_active = data.get('is_active', True)
    
    try:
        category = RecommendationCategory.query.filter_by(
            id=category_id,
            manager_id=current_manager.id
        ).first()
        
        if not category:
            return jsonify({'success': False, 'error': 'Категория не найдена'}), 404
        
        category.is_active = is_active
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# Manager Dashboard API endpoints
@app.route('/api/manager/welcome-message', methods=['GET'])
@manager_required
def api_manager_welcome_message():
    """Get adaptive welcome message based on recent activity"""
    from models import User, Recommendation, Collection, SavedSearch, Manager
    from sqlalchemy import func, desc
    from datetime import datetime, timedelta
    
    current_manager = current_user
    
    if not current_manager:
        return jsonify({'success': False, 'error': 'Менеджер не найден'}), 404
    
    try:
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=7)
        
        # Get recent activity counts
        recent_recommendations = Recommendation.query.filter(
            Recommendation.manager_id == current_manager.id,
            Recommendation.created_at >= week_start
        ).count()
        
        today_recommendations = Recommendation.query.filter(
            Recommendation.manager_id == current_manager.id,
            Recommendation.created_at >= today_start
        ).count()
        
        recent_collections = Collection.query.filter(
            Collection.created_by_manager_id == current_manager.id,
            Collection.created_at >= week_start
        ).count()
        
        total_clients = User.query.filter_by(assigned_manager_id=current_manager.id).count()
        
        new_clients_today = User.query.filter(
            User.assigned_manager_id == current_manager.id,
            User.created_at >= today_start
        ).count()
        
        # Get last activity time (use created_at if last_login_at doesn't exist)
        last_activity = getattr(current_manager, 'last_login_at', None) or current_manager.created_at
        hours_since_last_login = (now - last_activity).total_seconds() / 3600 if last_activity else 0
        
        # Get most recent activity
        latest_recommendation = Recommendation.query.filter_by(manager_id=current_manager.id).order_by(desc(Recommendation.created_at)).first()
        latest_collection = Collection.query.filter_by(created_by_manager_id=current_manager.id).order_by(desc(Collection.created_at)).first()
        
        # Generate adaptive message based on activity patterns
        messages = []
        
        # Time-based greeting
        hour = now.hour
        if 5 <= hour < 12:
            time_greeting = "Доброе утро"
        elif 12 <= hour < 18:
            time_greeting = "Добрый день"
        elif 18 <= hour < 23:
            time_greeting = "Добрый вечер"
        else:
            time_greeting = "Доброй ночи"
        
        first_name = current_manager.full_name.split()[0] if current_manager.full_name else 'Коллега'
        
        # Activity-based messages
        if hours_since_last_login >= 24:
            messages.append(f"{time_greeting}, {first_name}! Рады видеть вас снова.")
            if recent_recommendations > 0:
                messages.append(f"За время вашего отсутствия было отправлено {recent_recommendations} рекомендаций.")
        elif hours_since_last_login >= 8:
            messages.append(f"{time_greeting}, {first_name}! Добро пожаловать обратно.")
        else:
            messages.append(f"{time_greeting}, {first_name}!")
        
        # Recent activity highlights
        if today_recommendations > 0:
            messages.append(f"Сегодня вы уже отправили {today_recommendations} рекомендаций - отличная работа!")
        elif recent_recommendations > 0:
            messages.append(f"На этой неделе вы отправили {recent_recommendations} рекомендаций клиентам.")
        
        if new_clients_today > 0:
            messages.append(f"У вас {new_clients_today} новых клиентов сегодня.")
        
        if recent_collections > 0:
            messages.append(f"Создано {recent_collections} новых подборок на этой неделе.")
        
        # Motivational suggestions based on activity
        if recent_recommendations == 0 and recent_collections == 0:
            messages.append("Готовы создать новую подборку для клиентов?")
        elif total_clients > 0 and recent_recommendations < 3:
            messages.append("Возможно, стоит отправить рекомендации активным клиентам?")
        
        # Default fallback
        if len(messages) == 1:  # Only greeting
            messages.append("Панель управления менеджера недвижимости готова к работе.")
        
        # Activity context for additional UI hints
        activity_context = {
            'has_recent_activity': recent_recommendations > 0 or recent_collections > 0,
            'needs_attention': total_clients > 0 and recent_recommendations == 0,
            'high_activity': recent_recommendations >= 5 or recent_collections >= 3,
            'new_day': hours_since_last_login >= 8,
            'latest_recommendation_date': latest_recommendation.created_at.strftime('%d.%m.%Y') if latest_recommendation else None,
            'latest_collection_date': latest_collection.created_at.strftime('%d.%m.%Y') if latest_collection else None
        }
        
        return jsonify({
            'success': True,
            'messages': messages,
            'context': activity_context,
            'stats': {
                'recent_recommendations': recent_recommendations,
                'today_recommendations': today_recommendations,
                'recent_collections': recent_collections,
                'total_clients': total_clients,
                'new_clients_today': new_clients_today
            }
        })
        
    except Exception as e:
        print(f"Error generating welcome message: {e}")
        return jsonify({
            'success': True,
            'messages': [f"{time_greeting}, {first_name}!", "Панель управления менеджера недвижимости"],
            'context': {'has_recent_activity': False},
            'stats': {}
        })

@app.route('/api/manager/dashboard-stats', methods=['GET'])
@login_required
@manager_required
def api_manager_dashboard_stats():
    """Get manager dashboard statistics"""
    from models import User, Recommendation
    from sqlalchemy import func
    
    current_manager = current_user
    
    try:
        # Count clients assigned to this manager
        clients_count = User.query.filter_by(assigned_manager_id=current_manager.id).count()
        
        # Count recommendations sent by this manager
        recommendations_count = Recommendation.query.filter_by(manager_id=current_manager.id).count()
        
        # Count recommendations sent this month
        from datetime import datetime
        month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_recommendations = Recommendation.query.filter(
            Recommendation.manager_id == current_manager.id,
            Recommendation.sent_at >= month_start
        ).count()
        
        # Collections count (placeholder for now)
        collections_count = 5
        
        return jsonify({
            'success': True,
            'clients_count': clients_count,
            'recommendations_count': monthly_recommendations,
            'total_recommendations': recommendations_count,
            'collections_count': collections_count
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/activity-feed', methods=['GET'])
@manager_required
def api_manager_activity_feed():
    """Get manager activity feed"""
    from models import Recommendation, User, ManagerNotification
    from datetime import datetime, timedelta
    
    current_manager = current_user
    
    try:
        # Get recent activities (recommendations sent)
        from sqlalchemy.orm import joinedload
        recent_recommendations = Recommendation.query.filter_by(
            manager_id=current_manager.id
        ).options(joinedload(Recommendation.client)).order_by(Recommendation.sent_at.desc()).limit(10).all()
        
        # Получаем последние уведомления менеджера
        recent_notifications = ManagerNotification.query.filter_by(
            manager_id=current_manager.id
        ).order_by(ManagerNotification.created_at.desc()).limit(15).all()
        
        # Функция для форматирования времени
        def format_time_ago(timestamp):
            time_diff = datetime.utcnow() - timestamp
            if time_diff.days > 0:
                return f"{time_diff.days} дн. назад"
            elif time_diff.seconds > 3600:
                return f"{time_diff.seconds // 3600} ч. назад"
            else:
                return f"{time_diff.seconds // 60} мин. назад"
        
        # Создаем общий список с временными метками для сортировки
        all_activities = []
        
        # Добавляем уведомления
        for notification in recent_notifications:
            all_activities.append({
                'timestamp': notification.created_at,
                'activity': {
                    'title': notification.title,
                    'description': notification.message,
                    'time_ago': format_time_ago(notification.created_at),
                    'icon': 'eye' if notification.notification_type == 'presentation_view' else 'bell',
                    'color': 'purple' if notification.notification_type == 'presentation_view' else 'gray',
                    'is_read': notification.is_read,
                    'notification_id': notification.id,
                    'type': 'notification'
                }
            })
        
        # Добавляем рекомендации
        for rec in recent_recommendations:
            client_name = rec.client.full_name if rec.client and hasattr(rec.client, 'full_name') else 'Клиент'
            all_activities.append({
                'timestamp': rec.sent_at,
                'activity': {
                    'title': f'Отправлена рекомендация',
                    'description': f'{rec.title} для {client_name}',
                    'time_ago': format_time_ago(rec.sent_at),
                    'icon': 'paper-plane',
                    'color': 'blue',
                    'type': 'recommendation'
                }
            })
        
        # Сортируем по времени и берем только активности
        all_activities.sort(key=lambda x: x['timestamp'], reverse=True)
        activities = [item['activity'] for item in all_activities[:10]]  # Берем топ 10
        
        # Добавляем демо активности только если реальных активностей мало
        if len(activities) < 2:
            activities.extend([
                {
                    'title': 'Новый клиент добавлен',
                    'description': 'Демо Клиентов зарегистрировался в системе',
                    'time_ago': '2 ч. назад',
                    'icon': 'user-plus',
                    'color': 'green'
                },
                {
                    'title': 'Клиент просмотрел рекомендацию',
                    'description': 'Демо Клиентов открыл рекомендацию по ЖК "Солнечный"',
                    'time_ago': '4 ч. назад',
                    'icon': 'eye',
                    'color': 'purple'
                }
            ])
        
        return jsonify({
            'success': True,
            'activities': activities
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/manager/dashboard/all', methods=['GET'])
@manager_required
def api_manager_dashboard_all():
    """Агрегированный endpoint для быстрой загрузки панели менеджера - все данные за один запрос"""
    from models import (User, Recommendation, SavedSearch, Deal, ManagerComparison, 
                       ComparisonProperty, ComparisonComplex, ManagerFavoriteProperty, ManagerFavoriteComplex,
                       ManagerNotification, Manager)
    from sqlalchemy import func
    from datetime import datetime, timedelta
    
    current_manager = current_user
    
    try:
        # ===== 1. CLIENTS =====
        clients = User.query.filter_by(assigned_manager_id=current_manager.id).all()
        clients_data = [{
            'id': client.id,
            'full_name': client.full_name,
            'email': client.email,
            'phone': client.phone or '',
            'status': client.status if hasattr(client, 'status') else 'active',
            'search_preferences': client.search_preferences if hasattr(client, 'search_preferences') else None,
            'created_at': client.created_at.isoformat() if client.created_at else None
        } for client in clients]
        
        # ===== 2. WELCOME MESSAGE =====
        import pytz
        moscow_tz = pytz.timezone('Europe/Moscow')
        now_utc = datetime.utcnow()
        now_moscow = now_utc.replace(tzinfo=pytz.UTC).astimezone(moscow_tz)
        hour = now_moscow.hour
        
        if 5 <= hour < 12:
            time_greeting = "Доброе утро"
        elif 12 <= hour < 18:
            time_greeting = "Добрый день"
        else:
            time_greeting = "Добрый вечер"
        
        first_name = current_manager.full_name.split()[0] if current_manager.full_name else 'Менеджер'
        
        today = datetime.utcnow().date()
        week_ago = datetime.utcnow() - timedelta(days=7)
        
        recent_recommendations = Recommendation.query.filter(
            Recommendation.manager_id == current_manager.id,
            Recommendation.sent_at >= week_ago
        ).count()
        
        today_recommendations = Recommendation.query.filter(
            Recommendation.manager_id == current_manager.id,
            func.date(Recommendation.sent_at) == today
        ).count()
        
        total_clients = len(clients_data)
        new_clients_today = User.query.filter(
            User.assigned_manager_id == current_manager.id,
            func.date(User.created_at) == today
        ).count()
        
        messages = [f"{time_greeting}, {first_name}! Рады видеть вас снова."]
        
        if new_clients_today > 0:
            messages.append(f"У вас {new_clients_today} новых клиентов сегодня!")
        elif recent_recommendations == 0:
            messages.append("Готовы создать новую подборку для клиентов?")
        elif today_recommendations > 0:
            messages.append(f"Отлично! Сегодня отправлено {today_recommendations} рекомендаций.")
        else:
            messages.append(f"На этой неделе отправлено {recent_recommendations} рекомендаций.")
        
        # ===== 3. ACTIVITY FEED =====
        from sqlalchemy.orm import joinedload
        recent_recs = Recommendation.query.filter_by(
            manager_id=current_manager.id
        ).options(joinedload(Recommendation.client)).order_by(Recommendation.sent_at.desc()).limit(10).all()
        
        recent_notifications = ManagerNotification.query.filter_by(
            manager_id=current_manager.id
        ).order_by(ManagerNotification.created_at.desc()).limit(15).all()
        
        def format_time_ago(timestamp):
            time_diff = datetime.utcnow() - timestamp
            if time_diff.days > 0:
                return f"{time_diff.days} дн. назад"
            elif time_diff.seconds > 3600:
                return f"{time_diff.seconds // 3600} ч. назад"
            else:
                return f"{time_diff.seconds // 60} мин. назад"
        
        all_activities = []
        
        for notification in recent_notifications:
            all_activities.append({
                'timestamp': notification.created_at,
                'activity': {
                    'title': notification.title,
                    'description': notification.message,
                    'time_ago': format_time_ago(notification.created_at),
                    'icon': 'eye' if notification.notification_type == 'presentation_view' else 'bell',
                    'color': 'purple' if notification.notification_type == 'presentation_view' else 'gray',
                    'is_read': notification.is_read,
                    'notification_id': notification.id,
                    'type': 'notification'
                }
            })
        
        for rec in recent_recs:
            client_name = rec.client.full_name if rec.client and hasattr(rec.client, 'full_name') else 'Клиент'
            all_activities.append({
                'timestamp': rec.sent_at,
                'activity': {
                    'title': 'Отправлена рекомендация',
                    'description': f'{rec.title} для {client_name}',
                    'time_ago': format_time_ago(rec.sent_at),
                    'icon': 'paper-plane',
                    'color': 'blue',
                    'type': 'recommendation'
                }
            })
        
        all_activities.sort(key=lambda x: x['timestamp'], reverse=True)
        activities = [item['activity'] for item in all_activities[:10]]
        
        if len(activities) < 2:
            activities.extend([
                {
                    'title': 'Новый клиент добавлен',
                    'description': 'Демо Клиентов зарегистрировался в системе',
                    'time_ago': '2 ч. назад',
                    'icon': 'user-plus',
                    'color': 'green'
                },
                {
                    'title': 'Клиент просмотрел рекомендацию',
                    'description': 'Демо Клиентов открыл рекомендацию по ЖК "Солнечный"',
                    'time_ago': '4 ч. назад',
                    'icon': 'eye',
                    'color': 'purple'
                }
            ])
        
        # ===== 4. FAVORITES COUNT =====
        properties_count = ManagerFavoriteProperty.query.filter_by(manager_id=current_manager.id).count()
        complexes_count = ManagerFavoriteComplex.query.filter_by(manager_id=current_manager.id).count()
        
        # ===== 5. SAVED SEARCHES =====
        saved_searches = SavedSearch.query.filter_by(manager_id=current_manager.id).order_by(SavedSearch.created_at.desc()).all()
        searches_data = [{
            'id': search.id,
            'name': search.name,
            'filters': search.filters,
            'created_at': search.created_at.strftime('%d.%m.%Y')
        } for search in saved_searches]
        
        # ===== 6. RECOMMENDATIONS =====
        recommendations = Recommendation.query.filter_by(manager_id=current_manager.id).order_by(Recommendation.sent_at.desc()).limit(10).all()
        recs_data = []
        for rec in recommendations:
            client = User.query.get(rec.client_id) if rec.client_id else None
            recs_data.append({
                'id': rec.id,
                'title': rec.title,
                'client_name': client.full_name if client else 'Клиент удален',
                'sent_at': rec.sent_at.strftime('%d.%m.%Y в %H:%M'),
                'status': rec.status,
                'properties_count': len(rec.properties) if hasattr(rec, 'properties') else 0
            })
        
        # ===== 7. DEALS COUNT =====
        deals_count = Deal.query.filter_by(manager_id=current_manager.id).count()
        
        # ===== 8. COMPARISON COUNT =====
        manager_comparison = ManagerComparison.query.filter_by(
            manager_id=current_manager.id,
            is_active=True
        ).first()
        
        comparison_props = 0
        comparison_complexes = 0
        if manager_comparison:
            comparison_props = ComparisonProperty.query.filter_by(manager_comparison_id=manager_comparison.id).count()
            comparison_complexes = ComparisonComplex.query.filter_by(manager_comparison_id=manager_comparison.id).count()
        
        return jsonify({
            'success': True,
            'clients': clients_data,
            'welcome': {
                'messages': messages,
                'stats': {
                    'recent_recommendations': recent_recommendations,
                    'today_recommendations': today_recommendations,
                    'total_clients': total_clients,
                    'new_clients_today': new_clients_today
                }
            },
            'activities': activities,
            'favorites': {
                'properties_count': properties_count,
                'complexes_count': complexes_count,
                'total_count': properties_count + complexes_count
            },
            'saved_searches': {
                'count': len(searches_data),
                'searches': searches_data
            },
            'recommendations': recs_data,
            'deals_count': deals_count,
            'comparison': {
                'properties_count': comparison_props,
                'complexes_count': comparison_complexes,
                'total_count': comparison_props + comparison_complexes
            }
        })
        
    except Exception as e:
        import traceback
        print(f"Error loading manager dashboard data: {e}")
        print(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/manager/top-clients', methods=['GET'])
@login_required
@manager_required
def api_manager_top_clients():
    """Get top clients by interactions"""
    from models import User, Recommendation
    from sqlalchemy import func
    
    current_manager = current_user
    
    try:
        # Get clients with most interactions (recommendations received)
        top_clients = db.session.query(
            User,
            func.count(Recommendation.id).label('interactions_count')
        ).join(
            Recommendation, User.id == Recommendation.client_id
        ).filter(
            Recommendation.manager_id == current_manager.id
        ).group_by(User.id).order_by(
            func.count(Recommendation.id).desc()
        ).limit(5).all()
        
        clients_data = []
        for user, count in top_clients:
            clients_data.append({
                'id': user.id,
                'full_name': user.full_name,
                'email': user.email,
                'interactions_count': count
            })
        
        # Add demo clients if not enough data
        if len(clients_data) < 3:
            demo_clients = [
                {'id': 999, 'full_name': 'Демо Клиентов', 'email': 'demo@inback.ru', 'interactions_count': 8},
                {'id': 998, 'full_name': 'Анна Покупателева', 'email': 'buyer@test.ru', 'interactions_count': 5},
                {'id': 997, 'full_name': 'Петр Инвесторов', 'email': 'investor@test.ru', 'interactions_count': 3}
            ]
            clients_data.extend(demo_clients[:3-len(clients_data)])
        
        return jsonify({
            'success': True,
            'clients': clients_data
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

# Blog Management Routes for Managers
@app.route('/admin/blog-manager')
@manager_required
def admin_blog_manager():
    """Manager blog management page"""
    from models import BlogArticle, Category
    
    try:
        # Get filter parameters
        search = request.args.get('search', '')
        status = request.args.get('status', '')
        category_id = request.args.get('category_id', '')
        
        # Build query
        query = BlogArticle.query
        
        if search:
            query = query.filter(BlogArticle.title.contains(search) | 
                               BlogArticle.content.contains(search))
        
        if status:
            query = query.filter(BlogArticle.status == status)
            
        if category_id:
            query = query.filter(BlogArticle.category_id == int(category_id))
        
        # Order by creation date
        articles = query.order_by(BlogArticle.created_at.desc()).all()
        
        # Get categories for filter dropdown
        categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
        
        return render_template('admin/blog_manager.html',
                             articles=articles,
                             categories=categories,
                             search=search,
                             status=status,
                             category_id=category_id)
        
    except Exception as e:
        flash(f'Ошибка загрузки блога: {str(e)}', 'error')
        return redirect(url_for('manager_dashboard'))


@app.route('/admin/blog/create-new', methods=['GET', 'POST'])
@manager_required
def admin_create_new_article():
    """Create new blog article"""
    from models import Category, BlogArticle
    import re
    from datetime import datetime
    
    if request.method == 'GET':
        categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
        return render_template('admin/blog_create_new.html', categories=categories)
    
    try:
        # Get form data
        title = request.form.get('title')
        excerpt = request.form.get('excerpt')
        content = request.form.get('content')
        category_id = request.form.get('category_id')
        status = request.form.get('status', 'draft')
        is_featured = 'is_featured' in request.form
        
        # Generate slug from title
        slug = re.sub(r'[^\w\s-]', '', (title or '').lower())
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        
        # Ensure slug is unique
        original_slug = slug
        counter = 1
        while BlogArticle.query.filter_by(slug=slug).first():
            slug = f"{original_slug}-{counter}"
            counter += 1
        
        # Create article
        article = BlogArticle(
            title=title,
            slug=slug,
            excerpt=excerpt,
            content=content,
            category_id=int(category_id),
            author_id=current_user.id,
            status=status,
            is_featured=is_featured
        )
        
        # Set publish date if status is published
        if status == 'published':
            article.published_at = datetime.utcnow()
        
        # Calculate reading time (approx 200 words per minute)
        word_count = len(content.split()) if content else 0
        article.reading_time = max(1, word_count // 200)
        
        db.session.add(article)
        db.session.commit()
        
        flash('Статья успешно создана!', 'success')
        return redirect(url_for('admin_blog_manager'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка создания статьи: {str(e)}', 'error')
        return redirect(url_for('admin_create_new_article'))


@app.route('/admin/blog/<int:article_id>/edit-article', methods=['GET', 'POST'])
@manager_required 
def admin_edit_new_article(article_id):
    """Edit existing blog article"""
    from models import BlogArticle, Category
    import re
    from datetime import datetime
    
    article = BlogArticle.query.get_or_404(article_id)
    
    if request.method == 'GET':
        categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
        return render_template('admin/blog_edit_new.html', article=article, categories=categories)
    
    try:
        # Get form data
        title = request.form.get('title')
        excerpt = request.form.get('excerpt') 
        content = request.form.get('content')
        category_id = request.form.get('category_id')
        status = request.form.get('status')
        is_featured = 'is_featured' in request.form
        
        # Update slug if title changed
        if title != article.title:
            slug = re.sub(r'[^\w\s-]', '', (title or '').lower())
            slug = re.sub(r'[-\s]+', '-', slug).strip('-')
            
            # Ensure slug is unique (exclude current article)
            original_slug = slug
            counter = 1
            while BlogArticle.query.filter_by(slug=slug).filter(BlogArticle.id != article_id).first():
                slug = f"{original_slug}-{counter}"
                counter += 1
            
            article.slug = slug
        
        # Update article
        article.title = title
        article.excerpt = excerpt
        article.content = content
        article.category_id = int(category_id)
        article.status = status
        article.is_featured = is_featured
        article.updated_at = datetime.utcnow()
        
        # Set/update publish date if status changed to published
        if status == 'published' and not article.published_at:
            article.published_at = datetime.utcnow()
        
        # Recalculate reading time
        word_count = len(content.split()) if content else 0
        article.reading_time = max(1, word_count // 200)
        
        db.session.commit()
        
        flash('Статья успешно обновлена!', 'success')
        return redirect(url_for('admin_blog_manager'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка обновления статьи: {str(e)}', 'error')
        return redirect(url_for('admin_edit_new_article', article_id=article_id))


@app.route('/admin/blog/<int:article_id>/delete-article', methods=['POST'])
@manager_required
def admin_delete_new_article(article_id):
    """Delete blog article"""
    from models import BlogArticle
    
    try:
        article = BlogArticle.query.get_or_404(article_id)
        db.session.delete(article)
        db.session.commit()
        
        flash('Статья успешно удалена!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка удаления статьи: {str(e)}', 'error')
    
    return redirect(url_for('admin_blog_manager'))


@app.route('/admin/blog/categories')
@admin_required
def admin_blog_categories():
    """Manage blog categories"""
    from models import Admin, Category, BlogPost, BlogArticle
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    categories = Category.query.order_by(Category.sort_order, Category.name).all()
    
    # Добавляем подсчет статей для каждой категории
    for category in categories:
        # Считаем статьи из BlogPost (по названию категории)
        blog_post_count = BlogPost.query.filter_by(
            category=category.name, 
            status='published'
        ).count()
        
        # Считаем статьи из BlogArticle (по category_id)
        blog_article_count = BlogArticle.query.filter_by(
            category_id=category.id,
            status='published'
        ).count()
        
        # Общее количество статей
        category.articles_count = blog_post_count + blog_article_count
    
    return render_template('admin/blog_categories.html', admin=current_admin, categories=categories)


@app.route('/admin/blog/categories/create', methods=['GET', 'POST'])
@admin_required
# @csrf.exempt  # CSRF disabled  # Отключаем CSRF для админ панели
def admin_create_category():
    """Create new blog category - both form and JSON API"""
    from models import Admin, Category
    import re
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    # Handle JSON requests (from inline category creation)
    if request.is_json:
        try:
            data = request.get_json()
            name = data.get('name')
            description = data.get('description', '')
            
            if not name:
                return jsonify({'success': False, 'error': 'Название категории обязательно'})
            
            # Generate slug from Russian name
            def transliterate(text):
                rus_to_eng = {
                    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'zh', 'з': 'z',
                    'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r',
                    'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
                    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
                }
                return ''.join(rus_to_eng.get(char.lower(), char) for char in text)
            
            slug = transliterate(name.lower())
            slug = re.sub(r'[^\w\s-]', '', slug)
            slug = re.sub(r'[-\s]+', '-', slug).strip('-')
            
            # Ensure unique slug
            original_slug = slug
            counter = 1
            while Category.query.filter_by(slug=slug).first():
                slug = f"{original_slug}-{counter}"
                counter += 1
            
            category = Category(
                name=name,
                slug=slug,
                description=description,
                is_active=True
            )
            
            db.session.add(category)
            db.session.commit()
            
            return jsonify({
                'success': True,
                'category': {
                    'id': category.id,
                    'name': category.name,
                    'slug': category.slug
                }
            })
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})
    
    # Handle form requests (standard category creation page)
    if request.method == 'GET':
        return render_template('admin/blog_category_create.html', admin=current_admin)
    
    try:
        name = request.form.get('name')
        if not name:
            flash('Название категории обязательно', 'error')
            return render_template('admin/blog_category_create.html', admin=current_admin)
            
        description = request.form.get('description', '')
        
        # Generate slug
        slug = re.sub(r'[^\w\s-]', '', name.lower())
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        
        # Ensure unique slug
        original_slug = slug
        counter = 1
        while Category.query.filter_by(slug=slug).first():
            slug = f"{original_slug}-{counter}"
            counter += 1
        
        category = Category(
            name=name,
            slug=slug,
            description=description
        )
        
        db.session.add(category)
        db.session.commit()
        
        flash(f'Категория "{name}" успешно создана!', 'success')
        return redirect(url_for('admin_blog'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка создания категории: {str(e)}', 'error')
        return render_template('admin/blog_category_create.html', admin=current_admin)


# Blog Public Routes  
@app.route('/blog-new')
def blog_new():
    """Public blog page"""
    from models import BlogArticle, Category
    
    try:
        # Get published articles
        articles = BlogArticle.query.filter_by(status='published').order_by(BlogArticle.published_at.desc()).all()
        categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
        
        # Add pagination variables that template expects
        return render_template('blog.html', 
                             articles=articles, 
                             categories=categories,
                             total_pages=1,
                             current_page=1,
                             has_prev=False,
                             has_next=False,
                             prev_num=None,
                             next_num=None,
                             search_query='',
                             category_filter=None)
        
    except Exception as e:
        print(f"Blog error: {str(e)}")
        import traceback
        traceback.print_exc()
        # Fallback for when there's an error
        try:
            return render_template('blog.html', articles=[], categories=[])
        except:
            return "Временные проблемы с блогом. Попробуйте позже.", 500


@app.route('/blog-new/<slug>')
def blog_article_new(slug):
    """View single blog article"""
    from models import BlogArticle
    
    try:
        article = BlogArticle.query.filter_by(slug=slug, status='published').first_or_404()
        
        # Increment view count
        article.views_count += 1
        db.session.commit()
        
        # Get related articles from same category
        related_articles = BlogArticle.query.filter_by(
            category_id=article.category_id,
            status='published'
        ).filter(
            BlogArticle.id != article.id
        ).order_by(
            BlogArticle.published_at.desc()
        ).limit(3).all()
        
        return render_template('blog_article.html', 
                             article=article,
                             related_articles=related_articles)
        
    except Exception as e:
        flash('Статья не найдена', 'error')
        return redirect(url_for('blog_new'))


@app.route('/blog-new/category/<slug>')
def blog_category_new(slug):
    """View articles by category"""
    from models import Category, BlogArticle
    
    try:
        category = Category.query.filter_by(slug=slug, is_active=True).first_or_404()
        
        articles = BlogArticle.query.filter_by(
            category_id=category.id,
            status='published'
        ).order_by(
            BlogArticle.published_at.desc()
        ).all()
        
        return render_template('blog_category.html', 
                             category=category,
                             articles=articles)
        
    except Exception as e:
        flash('Категория не найдена', 'error')
        return redirect(url_for('blog_new'))


@app.route('/blog/<slug>')
def blog_post(slug):
    """Display single blog post by slug"""
    try:
        # Find post by slug - using direct SQL query
        from sqlalchemy import text
        result = db.session.execute(text("""
            SELECT id, title, slug, content, excerpt, category, featured_image, 
                   views_count, created_at, '' as author_name
            FROM blog_posts 
            WHERE slug = :slug AND status = 'published'
        """), {'slug': slug}).fetchone()
        
        if not result:
            flash('Статья не найдена', 'error')
            return redirect(url_for('blog'))
        
        # Convert to dict for template
        post = {
            'id': result[0],
            'title': result[1],
            'slug': result[2],
            'content': result[3],
            'excerpt': result[4],
            'category': result[5],
            'featured_image': result[6],
            'views_count': result[7] or 0,
            'created_at': result[8],
            'author_name': result[9] or 'InBack'
        }
        
        # Increment view count
        try:
            db.session.execute(text("""
                UPDATE blog_posts 
                SET views_count = COALESCE(views_count, 0) + 1 
                WHERE id = :id
            """), {'id': post['id']})
            db.session.commit()
            post['views_count'] += 1
        except Exception as e:
            db.session.rollback()
        
        # Get related posts from same category
        related_results = db.session.execute(text("""
            SELECT id, title, slug, excerpt, featured_image, created_at
            FROM blog_posts 
            WHERE category = :category AND status = 'published' AND id != :id
            ORDER BY created_at DESC
            LIMIT 3
        """), {'category': post['category'], 'id': post['id']}).fetchall()
        
        related_posts = []
        for r in related_results:
            related_posts.append({
                'id': r[0],
                'title': r[1], 
                'slug': r[2],
                'excerpt': r[3],
                'featured_image': r[4],
                'created_at': r[5]
            })
        
        return render_template('blog_post.html', 
                             post=post,
                             related_posts=related_posts)
        
    except Exception as e:
        flash('Ошибка загрузки статьи', 'error')
        return redirect(url_for('blog'))


# Admin Blog Management Routes
@app.route('/admin/blog-management')
@admin_required
def admin_blog_management():
    """Admin blog management page"""
    from models import BlogPost, Category
    
    try:
        # Get filter parameters
        search = request.args.get('search', '')
        status = request.args.get('status', '')
        category_name = request.args.get('category', '')
        page = request.args.get('page', 1, type=int)
        
        # Build query
        query = BlogPost.query
        
        if search:
            query = query.filter(BlogPost.title.contains(search) | 
                               BlogPost.content.contains(search))
        
        if status:
            query = query.filter(BlogPost.status == status)
            
        if category_name:
            query = query.filter(BlogPost.category == category_name)
        
        # Order by creation date and paginate
        posts = query.order_by(BlogPost.created_at.desc()).paginate(
            page=page, per_page=10, error_out=False
        )
        
        # Get categories for filter dropdown
        categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
        
        # Get admin user for template
        from flask_login import current_user
        admin = current_user if current_user.is_authenticated else None
        
        return render_template('admin/blog_management.html',
                             posts=posts,
                             categories=categories,
                             search=search,
                             status=status,
                             category_name=category_name,
                             admin=admin)
        
    except Exception as e:
        flash(f'Ошибка загрузки блога: {str(e)}', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/blog-management/create', methods=['GET', 'POST'])
@admin_required
def admin_create_blog_post():
    """Create new blog post"""
    from models import BlogPost, Category, Admin
    import re
    from datetime import datetime
    
    if request.method == 'GET':
        # ИСПРАВЛЕНО: Используем Flask-Login current_user
        # Get current admin
        current_admin = current_user
        
        categories = Category.query.order_by(Category.name).all()
        return render_template('admin/blog_post_create.html', categories=categories, admin=current_admin)
    
    try:
        # Get form data
        title = request.form.get('title')
        excerpt = request.form.get('excerpt')
        content = request.form.get('content')
        category_id = request.form.get('category_id')
        status = request.form.get('status', 'draft')
        is_featured = 'is_featured' in request.form
        featured_image = request.form.get('featured_image', '')
        meta_title = request.form.get('meta_title', '')
        meta_description = request.form.get('meta_description', '')
        keywords = request.form.get('keywords', '')
        
        # Get category name from category_id
        category = Category.query.get(int(category_id))
        if not category:
            flash('Выбранная категория не найдена', 'error')
            return redirect(url_for('admin_create_blog_post'))
        
        # Generate slug from title
        slug = re.sub(r'[^\w\s-]', '', (title or '').lower())
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        
        # Ensure slug is unique
        original_slug = slug
        counter = 1
        while BlogPost.query.filter_by(slug=slug).first():
            slug = f"{original_slug}-{counter}"
            counter += 1
        
        # Calculate reading time (approx 200 words per minute)
        word_count = len(content.split()) if content else 0
        reading_time = max(1, word_count // 200)
        
        # Create blog post using BlogPost model
        post = BlogPost(
            title=title,
            slug=slug,
            excerpt=excerpt,
            content=content,
            category=category.name,  # Use category name, not ID
            author_id=1,  # Default author
            status=status,
            featured_image=featured_image,
            tags=keywords
        )
        
        if status == 'published':
            post.published_at = datetime.utcnow()
        
        db.session.add(post)
        db.session.commit()
        
        # Обновим счетчик статей в категории
        category.articles_count = BlogPost.query.filter_by(category=category.name, status='published').count()
        db.session.commit()
        
        print(f'DEBUG: Created article "{title}" in category "{category.name}" with status "{status}"')
        print(f'DEBUG: Updated category "{category.name}" article count to {category.articles_count}')
        
        flash('Статья успешно создана!', 'success')
        return redirect(url_for('admin_blog_management'))
        
    except Exception as e:
        db.session.rollback()
        print(f'ERROR creating blog post: {str(e)}')
        flash(f'Ошибка создания статьи: {str(e)}', 'error')
        return redirect(url_for('admin_create_blog_post'))

@app.route('/admin/upload-image', methods=['POST'])
@admin_required
@csrf.exempt
def admin_upload_image():
    """Upload image for TinyMCE editor and blog posts"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Файл не выбран'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Файл не выбран'}), 400
    
    # Check if file is an image
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    if not (file.filename and '.' in file.filename and file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return jsonify({'success': False, 'error': 'Разрешены только изображения (PNG, JPG, JPEG, GIF, WebP)'}), 400
    
    try:
        # Generate secure filename
        from werkzeug.utils import secure_filename
        import os, uuid
        
        filename = secure_filename(file.filename) if file.filename else 'unnamed_file'
        
        # Create upload directory if it doesn't exist
        upload_dir = 'static/uploads/blog/content'
        os.makedirs(upload_dir, exist_ok=True)
        
        # Save file with unique name to avoid conflicts
        unique_filename = f"{uuid.uuid4()}_{filename}"
        file_path = os.path.join(upload_dir, unique_filename)
        file.save(file_path)
        
        # Return URL - TinyMCE expects 'location' field
        file_url = f"/{file_path}"
        
        return jsonify({
            'success': True,
            'location': file_url,  # TinyMCE expects 'location' field
            'url': file_url,       # Для совместимости с другими частями кода
            'filename': unique_filename
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': f'Ошибка загрузки файла: {str(e)}'}), 500

# Duplicate route removed - already defined earlier


@app.route('/admin/blog-management/<int:post_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_blog_post(post_id):
    """Edit blog post"""
    from models import BlogPost, Category, Admin
    import re
    from datetime import datetime
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    # Get current admin
    current_admin = current_user
    
    post = BlogPost.query.get_or_404(post_id)
    
    if request.method == 'GET':
        categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
        return render_template('admin/blog_post_create.html', post=post, categories=categories, admin=current_admin)
    
    try:
        # Get form data
        title = request.form.get('title')
        excerpt = request.form.get('excerpt')
        content = request.form.get('content')
        category_id = request.form.get('category_id')
        status = request.form.get('status')
        is_featured = 'is_featured' in request.form
        featured_image = request.form.get('featured_image', '')
        meta_title = request.form.get('meta_title', '')
        meta_description = request.form.get('meta_description', '')
        keywords = request.form.get('keywords', '')
        
        # Validation
        if not title or title.strip() == '':
            flash('Заголовок статьи обязателен', 'error')
            return redirect(url_for('admin_edit_blog_post', post_id=post_id))
        
        if not content or content.strip() == '':
            flash('Содержание статьи обязательно', 'error')
            return redirect(url_for('admin_edit_blog_post', post_id=post_id))
        
        if not category_id or category_id == '':
            flash('Выберите категорию статьи', 'error')
            return redirect(url_for('admin_edit_blog_post', post_id=post_id))

        # Get category name from category_id
        category = Category.query.get(int(category_id))
        if not category:
            flash('Выбранная категория не найдена', 'error')
            return redirect(url_for('admin_edit_blog_post', post_id=post_id))
        
        # Update slug if title changed
        if title != post.title:
            slug = re.sub(r'[^\w\s-]', '', (title or '').lower())
            slug = re.sub(r'[-\s]+', '-', slug).strip('-')
            
            original_slug = slug
            counter = 1
            while BlogPost.query.filter_by(slug=slug).filter(BlogPost.id != post_id).first():
                slug = f"{original_slug}-{counter}"
                counter += 1
            
            post.slug = slug
        
        # Calculate reading time
        word_count = len(content.split()) if content else 0
        reading_time = max(1, word_count // 200)
        
        # Update post
        old_category = post.category
        post.title = title
        post.excerpt = excerpt
        post.content = content
        post.category = category.name  # BlogPost uses category name as string
        post.status = status
        post.is_featured = is_featured
        post.featured_image = featured_image
        post.meta_title = meta_title or title
        post.meta_description = meta_description or excerpt  
        post.tags = keywords  # BlogPost uses tags field
        post.reading_time = reading_time
        post.updated_at = datetime.utcnow()
        
        if status == 'published' and not post.published_at:
            post.published_at = datetime.utcnow()
        
        db.session.commit()
        
        # Update category article counts for both old and new categories
        for cat_name in [old_category, category.name]:
            if cat_name:
                cat = Category.query.filter_by(name=cat_name).first()
                if cat:
                    cat.articles_count = BlogPost.query.filter_by(category=cat_name, status='published').count()
        
        db.session.commit()
        
        flash('Статья успешно обновлена!', 'success')
        return redirect(url_for('admin_blog_management'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка обновления статьи: {str(e)}', 'error')
        return redirect(url_for('admin_edit_blog_post', post_id=post_id))


@app.route('/admin/blog-management/<int:post_id>/delete', methods=['POST'])
@admin_required
def admin_delete_blog_post(post_id):
    """Delete blog post"""
    from models import BlogPost, Category
    
    try:
        post = BlogPost.query.get_or_404(post_id)
        category_name = post.category
        
        db.session.delete(post)
        db.session.commit()
        
        # Update category article count
        if category_name:
            category = Category.query.filter_by(name=category_name).first()
            if category:
                category.articles_count = BlogPost.query.filter_by(category=category_name, status='published').count()
                db.session.commit()
        
        flash('Статья успешно удалена!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка удаления статьи: {str(e)}', 'error')
    
    return redirect(url_for('admin_blog_management'))


@app.route('/admin/blog-categories-management')
@admin_required
def admin_blog_categories_management():
    """Admin blog categories management"""
    from models import Category
    
    try:
        categories = Category.query.order_by(Category.sort_order).all()
        return render_template('admin/blog_categories.html', categories=categories)
        
    except Exception as e:
        flash(f'Ошибка загрузки категорий: {str(e)}', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/blog-categories-management/create', methods=['GET', 'POST'])
@admin_required
def admin_create_blog_category_new():
    """Create blog category"""
    from models import Category
    import re
    
    def transliterate_russian_to_latin(text):
        """Convert Russian text to Latin characters for URL slugs"""
        translit_map = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
            'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
            'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
            'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
            'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
            ' ': '-', '_': '-'
        }
        
        result = ''
        for char in text.lower():
            result += translit_map.get(char, char)
        
        return result
    
    if request.method == 'GET':
        return render_template('admin/blog_category_create.html')
    
    try:
        # Get form data
        name = request.form.get('name')
        description = request.form.get('description', '')
        color = request.form.get('color', 'blue')
        icon = request.form.get('icon', 'fas fa-folder')
        sort_order = request.form.get('sort_order', 0, type=int)
        
        # Generate slug with proper Russian transliteration
        slug = transliterate_russian_to_latin(name)
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)  # Keep only safe characters
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        
        # Ensure slug is unique
        original_slug = slug
        counter = 1
        while Category.query.filter_by(slug=slug).first():
            slug = f"{original_slug}-{counter}"
            counter += 1
        
        category = Category(
            name=name,
            slug=slug,
            description=description,
            color=color,
            icon=icon,
            sort_order=sort_order,
            is_active=True,
            articles_count=0
        )
        
        db.session.add(category)
        db.session.commit()
        
        flash('Категория успешно создана!', 'success')
        return redirect(url_for('admin_blog_categories_management'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка создания категории: {str(e)}', 'error')
        return redirect(url_for('admin_create_blog_category_new'))


@app.route('/admin/blog-categories-management/<int:category_id>/edit', methods=['GET', 'POST'])
@admin_required  
def admin_edit_blog_category_new(category_id):
    """Edit blog category"""
    from models import Category
    import re
    
    category = Category.query.get_or_404(category_id)
    
    if request.method == 'GET':
        return render_template('admin/blog_category_edit.html', category=category)
    
    try:
        # Get form data
        name = request.form.get('name')
        description = request.form.get('description', '')
        color = request.form.get('color', 'blue')
        icon = request.form.get('icon', 'fas fa-folder')
        sort_order = request.form.get('sort_order', 0, type=int)
        is_active = 'is_active' in request.form
        
        # Update slug if name changed
        if name != category.name:
            def transliterate_russian_to_latin(text):
                """Convert Russian text to Latin characters for URL slugs"""
                translit_map = {
                    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
                    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
                    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
                    'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
                    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
                    ' ': '-', '_': '-'
                }
                
                result = ''
                for char in text.lower():
                    result += translit_map.get(char, char)
                
                return result
                
            slug = transliterate_russian_to_latin(name)
            slug = re.sub(r'[^a-z0-9\s-]', '', slug)  # Keep only safe characters
            slug = re.sub(r'[-\s]+', '-', slug).strip('-')
            
            original_slug = slug
            counter = 1
            while Category.query.filter_by(slug=slug).filter(Category.id != category_id).first():
                slug = f"{original_slug}-{counter}"
                counter += 1
            
            category.slug = slug
        
        category.name = name
        category.description = description
        category.color = color
        category.icon = icon
        category.sort_order = sort_order
        category.is_active = is_active
        
        db.session.commit()
        
        flash('Категория успешно обновлена!', 'success')
        return redirect(url_for('admin_blog_categories_management'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка обновления категории: {str(e)}', 'error')
        return redirect(url_for('admin_edit_blog_category_new', category_id=category_id))


@app.route('/admin/blog-categories-management/<int:category_id>/delete', methods=['POST'])
@admin_required
def admin_delete_blog_category_new(category_id):
    """Delete blog category"""
    from models import Category, BlogArticle
    
    try:
        category = Category.query.get_or_404(category_id)
        
        # Check if category has posts
        posts_count = BlogArticle.query.filter_by(category_id=category_id).count()
        if posts_count > 0:
            flash(f'Нельзя удалить категорию с {posts_count} статьями. Сначала переместите статьи в другие категории.', 'error')
            return redirect(url_for('admin_blog_categories_management'))
        
        db.session.delete(category)
        db.session.commit()
        
        flash('Категория успешно удалена!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка удаления категории: {str(e)}', 'error')
    
    return redirect(url_for('admin_blog_categories_management'))


# === JOB MANAGEMENT ADMIN ROUTES ===

@app.route('/admin/jobs')
@admin_required
def admin_jobs_management():
    """Admin jobs management"""
    from models import Job, JobCategory, Admin
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    try:
        jobs = Job.query.order_by(Job.created_at.desc()).all()
        categories = JobCategory.query.filter_by(is_active=True).order_by(JobCategory.sort_order).all()
        
        # Calculate statistics
        stats = {
            'total': len(jobs),
            'active': len([job for job in jobs if job.status == 'active']),
            'paused': len([job for job in jobs if job.status == 'paused']),
            'closed': len([job for job in jobs if job.status == 'closed']),
            'featured': len([job for job in jobs if job.is_featured])
        }
        
        return render_template('admin/careers_panel.html', vacancies=jobs, categories=categories, admin=current_admin, stats=stats)
        
    except Exception as e:
        flash(f'Ошибка загрузки вакансий: {str(e)}', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/jobs/create', methods=['GET', 'POST'])
@admin_required
def admin_create_job():
    """Create new job"""
    from models import Job, JobCategory, Admin
    import re
    
    def transliterate_russian_to_latin(text):
        """Convert Russian text to Latin characters for URL slugs"""
        translit_map = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
            'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
            'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
            'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
            'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
            ' ': '-', '_': '-'
        }
        
        result = ''
        for char in text.lower():
            result += translit_map.get(char, char)
        
        return result
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    if request.method == 'GET':
        categories = JobCategory.query.filter_by(is_active=True).order_by(JobCategory.sort_order).all()
        return render_template('admin/create_vacancy.html', categories=categories, admin=current_admin)
    
    try:
        # Get form data
        title = request.form.get('title')
        category_id = request.form.get('category_id', type=int)
        description = request.form.get('description')
        
        # Validate required fields
        if not title or not category_id or not description:
            flash('Заполните все обязательные поля', 'error')
            categories = JobCategory.query.filter_by(is_active=True).order_by(JobCategory.sort_order).all()
            return render_template('admin/create_vacancy.html', categories=categories, admin=current_admin)
        requirements = request.form.get('requirements', '')
        benefits = request.form.get('benefits', '')
        responsibilities = request.form.get('responsibilities', '')
        location = request.form.get('location')
        salary_min = request.form.get('salary_min', type=int)
        salary_max = request.form.get('salary_max', type=int)
        employment_type = request.form.get('employment_type', 'full_time')
        experience_level = request.form.get('experience_level', '')
        is_remote = 'is_remote' in request.form
        is_featured = 'is_featured' in request.form
        
        # Additional fields
        department = request.form.get('department', '')
        is_urgent = 'is_urgent' in request.form
        status = request.form.get('status', 'active')
        contact_email = request.form.get('contact_email', '')
        contact_phone = request.form.get('contact_phone', '')
        meta_title = request.form.get('meta_title', '')
        meta_description = request.form.get('meta_description', '')
        
        # Generate slug
        slug = transliterate_russian_to_latin(title)
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        
        # Ensure slug is unique
        original_slug = slug
        counter = 1
        while Job.query.filter_by(slug=slug).first():
            slug = f"{original_slug}-{counter}"
            counter += 1
        
        job = Job(
            title=title,
            slug=slug,
            category_id=category_id,
            description=description,
            requirements=requirements,
            benefits=benefits,
            responsibilities=responsibilities,
            location=location,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency='RUB',
            salary_period='month',
            employment_type=employment_type,
            experience_level=experience_level,
            is_remote=is_remote,
            is_featured=is_featured,
            is_urgent=is_urgent,
            status=status,
            department=department,
            is_active=True,
            contact_email=contact_email,
            contact_phone=contact_phone,
            meta_title=meta_title,
            meta_description=meta_description,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        db.session.add(job)
        db.session.commit()
        
        flash('Вакансия успешно создана!', 'success')
        return redirect(url_for('admin_jobs_management'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка создания вакансии: {str(e)}', 'error')
        return redirect(url_for('admin_create_job'))


@app.route('/admin/jobs/<int:job_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_job(job_id):
    """Edit job"""
    from models import Job, JobCategory, Admin
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    job = Job.query.get_or_404(job_id)
    
    if request.method == 'GET':
        categories = JobCategory.query.filter_by(is_active=True).order_by(JobCategory.sort_order).all()
        return render_template('admin/edit_vacancy.html', job=job, categories=categories, admin=current_admin)
    
    try:
        # Update job data
        job.title = request.form.get('title')
        job.category_id = request.form.get('category_id', type=int)
        job.description = request.form.get('description')
        job.requirements = request.form.get('requirements', '')
        job.benefits = request.form.get('benefits', '')
        job.responsibilities = request.form.get('responsibilities', '')
        job.location = request.form.get('location')
        job.salary_min = request.form.get('salary_min', type=int)
        job.salary_max = request.form.get('salary_max', type=int)
        job.employment_type = request.form.get('employment_type', 'full_time')
        job.experience_level = request.form.get('experience_level', '')
        job.is_remote = 'is_remote' in request.form
        job.is_featured = 'is_featured' in request.form
        job.is_urgent = 'is_urgent' in request.form
        job.status = request.form.get('status', 'active')
        job.department = request.form.get('department', '')
        job.contact_email = request.form.get('contact_email', '')
        job.contact_phone = request.form.get('contact_phone', '')
        job.meta_title = request.form.get('meta_title', '')
        job.meta_description = request.form.get('meta_description', '')
        job.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        flash('Вакансия успешно обновлена!', 'success')
        return redirect(url_for('admin_jobs_management'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка обновления вакансии: {str(e)}', 'error')
        return redirect(url_for('admin_edit_job', job_id=job_id))


@app.route('/admin/jobs/<int:job_id>/delete', methods=['POST'])
@admin_required
def admin_delete_job(job_id):
    """Delete job"""
    from models import Job
    
    try:
        job = Job.query.get_or_404(job_id)
        db.session.delete(job)
        db.session.commit()
        
        flash('Вакансия успешно удалена!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка удаления вакансии: {str(e)}', 'error')
    
    return redirect(url_for('admin_jobs_management'))


@app.route('/job/<job_slug>')
def job_detail(job_slug):
    """Job detail page"""
    from models import Job
    
    try:
        job = Job.query.filter(Job.slug == job_slug, Job.is_active == True, Job.status == 'active').first_or_404()
        
        # Increment views count
        job.views_count = (job.views_count or 0) + 1
        db.session.commit()
        
        return render_template('vacancy_details.html', vacancy=job)
        
    except Exception as e:
        print(f"Job detail error: {e}")
        flash('Вакансия не найдена', 'error')
        return redirect(url_for('careers'))


@app.route('/job/<job_slug>/apply', methods=['POST'])
# @csrf.exempt  # Временно исключаем из CSRF защиты - TODO: добавить CSRF в форму
def submit_job_application(job_slug):
    """Submit job application with resume"""
    from models import Job
    import os
    import uuid
    from datetime import datetime
    
    try:
        # Find the job
        job = Job.query.filter(Job.slug == job_slug, Job.is_active == True, Job.status == 'active').first_or_404()
        
        # Get form data
        candidate_name = request.form.get('candidate_name', '').strip()
        candidate_phone = request.form.get('candidate_phone', '').strip()
        candidate_email = request.form.get('candidate_email', '').strip()
        cover_letter = request.form.get('cover_letter', '').strip()
        
        # Validate required fields
        if not candidate_name or not candidate_phone:
            flash('Имя и телефон обязательны для заполнения', 'error')
            return redirect(url_for('job_detail', job_slug=job_slug))
        
        # Validate phone format
        import re
        phone_pattern = r'^[\+]?[0-9\s\-\(\)]{10,18}$'
        if not re.match(phone_pattern, candidate_phone):
            flash('Неверный формат номера телефона', 'error')
            return redirect(url_for('job_detail', job_slug=job_slug))
        
        # Handle file upload
        resume_file = request.files.get('resume_file')
        resume_filename = None
        
        if resume_file and resume_file.filename:
            # Validate file
            allowed_extensions = {'pdf', 'doc', 'docx', 'txt', 'rtf'}
            filename = resume_file.filename.lower()
            
            if not any(filename.endswith('.' + ext) for ext in allowed_extensions):
                flash('Неподдерживаемый формат файла. Используйте PDF, DOC, DOCX, TXT или RTF', 'error')
                return redirect(url_for('job_detail', job_slug=job_slug))
            
            # Check file size (5MB max)
            resume_file.seek(0, 2)  # Seek to end
            file_size = resume_file.tell()
            resume_file.seek(0)  # Reset to beginning
            
            if file_size > 5 * 1024 * 1024:  # 5MB
                flash('Размер файла не должен превышать 5 МБ', 'error')
                return redirect(url_for('job_detail', job_slug=job_slug))
            
            # Save file with unique name
            file_extension = filename.split('.')[-1]
            unique_filename = f"{uuid.uuid4()}_{candidate_name.replace(' ', '_')}_{job.slug}.{file_extension}"
            resume_filename = secure_filename(unique_filename)
            
            # Ensure upload directory exists
            upload_dir = os.path.join(app.root_path, 'static', 'uploads', 'resumes')
            os.makedirs(upload_dir, exist_ok=True)
            
            # Save file
            resume_path = os.path.join(upload_dir, resume_filename)
            resume_file.save(resume_path)
        else:
            flash('Резюме обязательно для отправки', 'error')
            return redirect(url_for('job_detail', job_slug=job_slug))
        
        # Prepare email notification
        try:
            # Email to HR/Admin
            admin_subject = f"Новый отклик на вакансию: {job.title}"
            admin_message = f"""Поступил новый отклик на вакансию "{job.title}":

Кандидат: {candidate_name}
Телефон: {candidate_phone}
Email: {candidate_email if candidate_email else 'Не указан'}

Сопроводительное письмо:
{cover_letter if cover_letter else 'Не указано'}

Резюме сохранено: {resume_filename}

Вакансия: {job.title}
Отдел: {job.department}
Дата подачи: {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
            
            # Send notification using existing email service
            send_notification(
                subject=admin_subject,
                message=admin_message,
                to_email="hr@inback.ru",  # You can configure this
                notification_type="job_application"
            )
            
            # Optional: Send confirmation to candidate if email provided
            if candidate_email:
                candidate_subject = f"Спасибо за отклик на вакансию: {job.title}"
                candidate_message = f"""Здравствуйте, {candidate_name}!

Спасибо за ваш отклик на вакансию "{job.title}" в компании InBack.

Мы получили ваше резюме и рассмотрим его в ближайшее время. 
Если ваша кандидатура подойдет, мы свяжемся с вами по телефону {candidate_phone}.

С уважением,
Команда InBack
"""
                
                send_notification(
                    subject=candidate_subject,
                    message=candidate_message,
                    to_email=candidate_email,
                    notification_type="application_confirmation"
                )
            
        except Exception as e:
            print(f"Email sending error: {e}")
            # Don't fail the whole process if email fails
            pass
        
        flash('Спасибо! Ваше резюме отправлено. Мы свяжемся с вами в ближайшее время.', 'success')
        return redirect(url_for('job_detail', job_slug=job_slug))
        
    except Exception as e:
        print(f"Job application error: {e}")
        flash('Произошла ошибка при отправке резюме. Попробуйте еще раз.', 'error')
        return redirect(url_for('job_detail', job_slug=job_slug))


@app.route('/admin/jobs/<int:vacancy_id>/toggle-status', methods=['POST'])
@admin_required
def admin_toggle_vacancy_status(vacancy_id):
    """Toggle vacancy status between active and paused"""
    from models import Job
    
    job = Job.query.get_or_404(vacancy_id)
    
    # Toggle between 'active' and 'paused' status
    if job.status == 'active':
        job.status = 'paused'
        status_text = 'приостановлена'
    else:
        job.status = 'active'
        status_text = 'активна'
    
    try:
        db.session.commit()
        flash(f'Вакансия "{job.title}" {status_text}', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Ошибка при изменении статуса вакансии', 'error')
    
    return redirect(url_for('admin_jobs_management'))


@app.route('/admin/job-categories')
@admin_required
def admin_job_categories_management():
    """Admin job categories management"""
    from models import JobCategory, Admin
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    try:
        categories = JobCategory.query.order_by(JobCategory.sort_order).all()
        return render_template('admin/job_categories_management.html', categories=categories, admin=current_admin)
        
    except Exception as e:
        flash(f'Ошибка загрузки категорий вакансий: {str(e)}', 'error')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/job-categories/create', methods=['GET', 'POST'])
@admin_required
@csrf.exempt
def admin_create_job_category():
    """Create new job category"""
    from models import JobCategory, Admin
    import re
    
    def transliterate_russian_to_latin(text):
        """Convert Russian text to Latin characters for URL slugs"""
        translit_map = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
            'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
            'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
            'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
            'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
            ' ': '-', '_': '-'
        }
        
        result = ''
        for char in text.lower():
            result += translit_map.get(char, char)
        
        return result
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    if request.method == 'GET':
        return render_template('admin/create_job_category.html', admin=current_admin)
    
    try:
        # Get form data
        name = request.form.get('name')
        description = request.form.get('description', '')
        color = request.form.get('color', 'blue')
        icon = request.form.get('icon', 'fas fa-briefcase')
        sort_order = request.form.get('sort_order', 0, type=int)
        
        # Generate slug
        slug = transliterate_russian_to_latin(name)
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        
        # Ensure slug is unique
        original_slug = slug
        counter = 1
        while JobCategory.query.filter_by(slug=slug).first():
            slug = f"{original_slug}-{counter}"
            counter += 1
        
        category = JobCategory(
            name=name,
            slug=slug,
            description=description,
            color=color,
            icon=icon,
            sort_order=sort_order,
            is_active=True
        )
        
        db.session.add(category)
        db.session.commit()
        
        flash('Категория вакансий успешно создана!', 'success')
        return redirect(url_for('admin_job_categories_management'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка создания категории вакансий: {str(e)}', 'error')
        return redirect(url_for('admin_create_job_category'))


@app.route('/admin/job-categories/<int:category_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_job_category(category_id):
    """Edit job category"""
    from models import JobCategory, Admin
    import re
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    current_admin = current_user
    
    category = JobCategory.query.get_or_404(category_id)
    
    def transliterate_russian_to_latin(text):
        """Convert Russian text to Latin characters for URL slugs"""
        translit_map = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
            'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
            'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
            'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
            'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
            ' ': '-', '_': '-'
        }
        
        result = ''
        for char in text.lower():
            result += translit_map.get(char, char)
        
        return result
    
    if request.method == 'GET':
        return render_template('admin/edit_job_category.html', category=category, admin=current_admin)
    
    try:
        # Update category data
        category.name = request.form.get('name')
        category.description = request.form.get('description', '')
        category.color = request.form.get('color', 'blue')
        category.icon = request.form.get('icon', 'fas fa-briefcase')
        category.sort_order = request.form.get('sort_order', 0, type=int)
        category.is_active = 'is_active' in request.form
        
        # Update slug only if name changed
        old_slug = category.slug
        name = request.form.get('name')
        if name and category.name != name:
            slug = transliterate_russian_to_latin(name)
            slug = re.sub(r'[^a-z0-9\s-]', '', slug)
            slug = re.sub(r'[-\s]+', '-', slug).strip('-')
            
            # Ensure slug is unique (excluding current category)
            original_slug = slug
            counter = 1
            while JobCategory.query.filter(JobCategory.slug == slug, JobCategory.id != category_id).first():
                slug = f"{original_slug}-{counter}"
                counter += 1
            
            category.slug = slug
        
        db.session.commit()
        
        flash('Категория вакансий успешно обновлена!', 'success')
        return redirect(url_for('admin_job_categories_management'))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка обновления категории вакансий: {str(e)}', 'error')
        return redirect(url_for('admin_edit_job_category', category_id=category_id))


@app.route('/admin/job-categories/<int:category_id>/delete', methods=['POST'])
@admin_required
def admin_delete_job_category(category_id):
    """Delete job category"""
    from models import JobCategory, Job
    
    try:
        category = JobCategory.query.get_or_404(category_id)
        
        # Check if category has jobs
        jobs_count = Job.query.filter_by(category_id=category_id).count()
        if jobs_count > 0:
            flash(f'Нельзя удалить категорию с {jobs_count} вакансиями. Сначала переместите вакансии в другие категории.', 'error')
            return redirect(url_for('admin_job_categories_management'))
        
        db.session.delete(category)
        db.session.commit()
        
        flash('Категория вакансий успешно удалена!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка удаления категории вакансий: {str(e)}', 'error')
    
    return redirect(url_for('admin_job_categories_management'))


# Register API blueprint
app.register_blueprint(api_bp)

# Register notification settings blueprint
try:
    from notification_settings import notification_settings_bp
    app.register_blueprint(notification_settings_bp)
except Exception as e:
    print(f"Warning: Could not register notification settings blueprint: {e}")

# Smart Search API Endpoints
@app.route('/api/smart-search')
def smart_search_api():
    """Умный поиск с OpenAI анализом"""
    query = request.args.get('q', '').strip()
    
    if not query:
        return jsonify({'results': [], 'criteria': {}, 'suggestions': []})
    
    try:
        # Анализируем запрос с помощью OpenAI
        criteria = smart_search.analyze_search_query(query)
        # Search criteria processed
        
        # Получаем свойства и применяем фильтры
        properties = load_properties()
        # Применяем базовые фильтры на основе критериев
        filtered_properties = apply_smart_filters(properties, criteria)
        
        # Применяем семантический поиск если нужно
        if criteria.get('semantic_search') or criteria.get('features'):
            filtered_properties = smart_search.semantic_property_search(
                filtered_properties, query, criteria
            )
        
        # Подготавливаем результаты
        results = []
        for prop in filtered_properties[:20]:
            results.append({
                'type': 'property',
                'id': prop['id'],
                'title': f"{prop.get('rooms', 0)}-комн {prop.get('area', 0)} м²" if prop.get('rooms', 0) > 0 else f"Студия {prop.get('area', 0)} м²",
                'subtitle': f"{prop.get('complex_name', '')} • {prop['district']}",
                'price': prop['price'],
                'rooms': prop.get('rooms', 1),
                'area': prop.get('area', 0),
                'url': f"/object/{prop['id']}"
            })
        
        # Генерируем подсказки
        suggestions = smart_search.generate_search_suggestions(query)
        
        return jsonify({
            'results': results,
            'criteria': criteria,
            'suggestions': suggestions[:5],
            'total': len(filtered_properties)
        })
        
    except Exception as e:
        print(f"ERROR: Smart search failed: {e}")
        # Fallback к обычному поиску
        return jsonify({'results': [], 'error': str(e)})

@app.route('/api/search-suggestions')
# ❌ КЭШ ОТКЛЮЧЁН для отладки типов квартир
# @cache.memoize(timeout=300)
def search_suggestions_api():
    """Супер-быстрый API для автодополнения поиска с типами квартир"""
    query = request.args.get('q', '').strip()
    if not query or len(query) < 1:  # ✅ Уменьшили минимальную длину с 2 до 1
        return jsonify([])
    
    try:
        # ✅ Используем ИСПРАВЛЕННЫЙ super_search с поддержкой типов квартир
        from smart_search import smart_search
        suggestions = smart_search.search_suggestions(query, limit=50)
        # ✅ Возвращаем прямо список, как ожидает фронтенд
        return jsonify(suggestions)
        
    except Exception as e:
        # Fallback к старому поиску в случае ошибки
        print(f"❌ Super search failed, using fallback: {e}")
        import traceback
        traceback.print_exc()
        return search_suggestions_fallback(query)

def search_suggestions_fallback(query):
    """✅ MIGRATED TO NORMALIZED TABLES: Fallback search using ResidentialComplexRepository"""
    suggestions = []
    query_lower = query.lower()
    
    try:
        # ✅ MIGRATED: Search complexes using ResidentialComplexRepository
        from models import ResidentialComplex
        complexes = (
            db.session.query(
                ResidentialComplex.name,
                func.count(Property.id).label('count')
            )
            .join(Property, ResidentialComplex.id == Property.complex_id)
            .filter(
                ResidentialComplex.name.ilike(f'%{query_lower}%'),
                Property.is_active == True
            )
            .group_by(ResidentialComplex.name)
            .order_by(func.count(Property.id).desc())
            .limit(4)
            .all()
        )
        
        for row in complexes:
            suggestions.append({
                'type': 'complex',
                'title': row[0],
                'subtitle': f'{row[1]} квартир',
                'icon': 'building',
                'url': f'/properties?residential_complex={row[0]}'
            })
        
        return jsonify({'suggestions': suggestions[:6]})
    except Exception as e:
        return jsonify({'suggestions': [], 'error': str(e)})

@app.route('/api/super-search')
@cache.memoize(timeout=180)  # Кэш на 3 минуты
def super_search_api():
    """Новый супер-быстрый поиск недвижимости"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'results': [], 'total': 0})
    
    try:
        from smart_search import smart_search
        results = smart_search.search_properties(query, limit=50)
        return jsonify(results)
        
    except Exception as e:
        print(f"Super search error: {e}")
        return jsonify({'results': [], 'total': 0, 'error': str(e)})

@app.route('/api/metrics', methods=['POST'])
def collect_metrics():
    """Сбор метрик производительности для анализа"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'message': 'No data provided'}), 400
        
        # Логируем важные метрики
        metric_type = data.get('type', 'unknown')
        
        if metric_type == 'page_load':
            duration = data.get('duration', 0)
            url = data.get('url', 'unknown')
            print(f"⚡ Page Load: {url} in {round(duration)}ms")
        
        elif metric_type == 'search_performance':
            query = data.get('query', '')
            response_time = data.get('response_time', 0)
            results_count = data.get('results_count', 0)
            print(f"🔍 Search: '{query}' - {round(response_time)}ms, {results_count} results")
        
        # В реальном приложении здесь бы была запись в базу данных
        # для аналитики производительности
        
        return jsonify({'status': 'success'})
        
    except Exception as e:
        print(f"Metrics collection error: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/api/smart-suggestions')
def smart_suggestions_api():
    """API для получения умных подсказок поиска"""
    query = request.args.get('q', '').strip()
    
    if len(query) < 2:
        return jsonify({'suggestions': []})
    
    try:
        suggestions = smart_search.generate_search_suggestions(query)
        return jsonify({'suggestions': suggestions})
    except Exception as e:
        print(f"ERROR: Smart suggestions failed: {e}")
        return jsonify({'suggestions': []})

def apply_smart_filters(properties, criteria):
    """Применяет умные фильтры на основе критериев OpenAI"""
    filtered = properties.copy()
    
    # Фильтр по комнатам
    if criteria.get('rooms'):
        rooms_list = criteria['rooms']
        filtered = [p for p in filtered if str(p.get('rooms', '')) in rooms_list]
    
    # Фильтр по району
    if criteria.get('district'):
        district = criteria['district']
        filtered = [p for p in filtered if p.get('district', '') == district]
    
    # Фильтр по ключевым словам (типы недвижимости, классы, материалы)
    if criteria.get('keywords'):
        keywords_filtered = []
        for prop in filtered:
            prop_matches = False
            for keyword in criteria['keywords']:
                keyword_lower = keyword.lower()
                
                # Тип недвижимости
                prop_type_lower = prop.get('property_type', 'Квартира').lower()
                if keyword_lower == prop_type_lower:
                    prop_matches = True
                    break
                
                # Класс недвижимости (точное совпадение)
                prop_class_lower = prop.get('property_class', '').lower()
                if keyword_lower == prop_class_lower:
                    prop_matches = True
                    break
                
                # Материал стен
                wall_material_lower = prop.get('wall_material', '').lower()
                if keyword_lower in wall_material_lower:
                    prop_matches = True
                    break
                
                # Особенности
                features = prop.get('features', [])
                if any(keyword_lower in feature.lower() for feature in features):
                    prop_matches = True
                    break
                
                # Особая логика для ценовых категорий
                if keyword_lower == 'дорого' or keyword_lower == 'недорого':
                    # Эти ключевые слова обрабатываются отдельно после фильтрации
                    continue
                
                # Поиск в заголовке как fallback
                property_title = f"{prop.get('rooms', 0)}-комн {prop.get('area', 0)} м²" if prop.get('rooms', 0) > 0 else f"Студия {prop.get('area', 0)} м²"
                title_lower = property_title.lower()
                if keyword_lower in title_lower:
                    prop_matches = True
                    break
            
            if prop_matches:
                keywords_filtered.append(prop)
        
        filtered = keywords_filtered
        
        # Обработка ценовых ключевых слов после основной фильтрации
        if 'дорого' in criteria.get('keywords', []):
            # Сортируем по цене и берем верхние 50%
            filtered = sorted(filtered, key=lambda x: x.get('price', 0), reverse=True)
            filtered = filtered[:max(1, len(filtered)//2)]
        elif 'недорого' in criteria.get('keywords', []):
            # Сортируем по цене и берем нижние 50%
            filtered = sorted(filtered, key=lambda x: x.get('price', 0))
            filtered = filtered[:max(1, len(filtered)//2)]
    
    # Фильтр по особенностям
    if criteria.get('features'):
        features_list = criteria['features']
        features_filtered = []
        for prop in filtered:
            prop_features = [f.lower() for f in prop.get('features', [])]
            if any(feature.lower() in prop_features for feature in features_list):
                features_filtered.append(prop)
        filtered = features_filtered
    
    # Фильтр по цене
    if criteria.get('price_range'):
        price_range = criteria['price_range']
        if len(price_range) >= 1 and price_range[0]:
            min_price = price_range[0]
            filtered = [p for p in filtered if p.get('price', 0) >= min_price]
        if len(price_range) >= 2 and price_range[1]:
            max_price = price_range[1]
            filtered = [p for p in filtered if p.get('price', 0) <= max_price]
    
    return filtered

# Manager Client Management Routes
@app.route('/manager/clients')
@manager_required
def manager_clients():
    """Manager clients page"""
    from models import User, Manager
    
    current_manager = current_user
    
    if not current_manager:
        return redirect(url_for('manager_login'))
    
    # Get clients assigned to this manager
    clients = User.query.filter_by(assigned_manager_id=current_manager.id).order_by(User.created_at.desc()).all()
    
    return render_template('manager/clients.html', 
                         manager=current_manager,
                         clients=clients)

# Manager Deals Management Routes  
@app.route('/manager/deals')
@manager_required
def manager_deals():
    """Manager deals page"""
    from models import User, Manager, Deal, ResidentialComplex
    from sqlalchemy import func
    
    current_manager = current_user
    
    if not current_manager:
        return redirect(url_for('manager_login'))
    
    # Get deals for this manager
    deals = Deal.query.filter_by(manager_id=current_manager.id).order_by(Deal.created_at.desc()).all()
    
    # Get clients available for this manager (assigned OR unassigned)
    # Менеджер может создавать сделки для своих клиентов и для неназначенных клиентов
    assigned_clients = User.query.filter(
        db.or_(
            User.assigned_manager_id == current_manager.id,
            User.assigned_manager_id == None
        )
    ).filter_by(role='buyer').order_by(User.full_name).all()
    
    residential_complexes = ResidentialComplex.query.order_by(ResidentialComplex.name).all()
    
    # Calculate stats
    active_deals_count = Deal.query.filter(
        Deal.manager_id == current_manager.id,
        Deal.status.in_(['new', 'reserved', 'mortgage'])
    ).count()
    
    completed_deals_count = Deal.query.filter(
        Deal.manager_id == current_manager.id,
        Deal.status == 'completed'
    ).count()
    
    in_progress_deals_count = Deal.query.filter(
        Deal.manager_id == current_manager.id,
        Deal.status.in_(['reserved', 'mortgage'])
    ).count()
    
    # Calculate total cashback
    total_cashback = db.session.query(func.sum(Deal.cashback_amount)).filter(
        Deal.manager_id == current_manager.id,
        Deal.status == 'completed'
    ).scalar() or 0
    
    return render_template('manager/deals.html',
                         manager=current_manager,
                         deals=deals,
                         assigned_clients=assigned_clients,
                         residential_complexes=residential_complexes,
                         active_deals_count=active_deals_count,
                         completed_deals_count=completed_deals_count,
                         in_progress_deals_count=in_progress_deals_count,
                         total_cashback=int(total_cashback))


@app.route('/api/manager/add-client', methods=['POST'])
@manager_required
def manager_add_client():
    """Add new client"""
    from models import User, Manager
    import re
    
    current_manager = current_user
    print(f"DEBUG: Add client endpoint called by manager {current_manager.id}")
    print(f"DEBUG: Request method: {request.method}, Content-Type: {request.content_type}")
    print(f"DEBUG: Request is_json: {request.is_json}")
    
    try:
        # Accept both JSON and form data
        if request.is_json:
            data = request.get_json()
            print(f"DEBUG: Received JSON data: {data}")
            full_name = data.get('full_name', '').strip()
            email = data.get('email', '').strip().lower()
            phone = data.get('phone', '').strip() if data.get('phone') else None
            is_active = data.get('is_active', True)
        else:
            print(f"DEBUG: Received form data: {dict(request.form)}")
            full_name = request.form.get('full_name', '').strip()
            email = request.form.get('email', '').strip().lower()
            phone = request.form.get('phone', '').strip() if request.form.get('phone') else None
            is_active = 'is_active' in request.form
        
        print(f"DEBUG: Parsed data - name: {full_name}, email: {email}, phone: {phone}, active: {is_active}")
        
        # Validation
        if not full_name or len(full_name) < 2:
            return jsonify({'success': False, 'error': 'Полное имя должно содержать минимум 2 символа'}), 400
        
        # Email validation
        email_regex = r'^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$'
        if not email or not re.match(email_regex, email):
            return jsonify({'success': False, 'error': 'Введите корректный email адрес'}), 400
        
        # Phone validation (optional but must be correct format if provided)
        if phone:
            phone_regex = r'^\+7-\d{3}-\d{3}-\d{2}-\d{2}$'
            if not re.match(phone_regex, phone):
                return jsonify({'success': False, 'error': 'Телефон должен быть в формате +7-918-123-45-67'}), 400
        
        # Check if email already exists
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            return jsonify({'success': False, 'error': 'Пользователь с таким email уже существует'}), 400
        
        # Generate temporary password
        import secrets
        import string
        temp_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
        
        # Create new user with temporary password
        user = User(
            full_name=full_name,
            email=email,
            phone=phone,
            is_active=is_active,
            role='buyer',
            assigned_manager_id=current_manager.id,
            registration_source='Manager',
            client_status='Новый'
        )
        user.set_password(temp_password)  # Set temporary password
        
        db.session.add(user)
        db.session.commit()
        
        print(f"DEBUG: Successfully created client {user.id}: {user.full_name}")
        
        # Send welcome email and SMS with credentials
        try:
            from email_service import send_email
            manager = Manager.query.get(manager_id)
            manager_name = manager.full_name if manager else 'Ваш менеджер'
            
            # Email with login credentials
            subject = "Ваш аккаунт создан в InBack.ru - Данные для входа"
            email_content = f"""Здравствуйте, {full_name}!

Для вас создан аккаунт на платформе InBack.ru

📧 Email для входа: {email}
🔑 Временный пароль: {temp_password}

🌐 Ссылка для входа: {request.url_root.rstrip('/')}/login

ВАЖНО: Рекомендуем сменить пароль после первого входа в разделе "Настройки профиля"

Ваш персональный менеджер: {manager_name}

По всем вопросам обращайтесь к своему менеджеру.

С уважением,
Команда InBack.ru"""
            
            send_email(
                to_email=email,
                subject=subject,
                content=email_content,
                template_name='notification'
            )
            print(f"DEBUG: Welcome email with credentials sent to {email}")
            
            # Send SMS if phone number provided
            if phone:
                try:
                    from sms_service import send_login_credentials_sms
                    
                    sms_sent = send_login_credentials_sms(
                        phone=phone,
                        email=email,
                        password=temp_password,
                        manager_name=manager_name,
                        login_url=f"{request.url_root.rstrip('/')}/login"
                    )
                    
                    if sms_sent:
                        print(f"DEBUG: SMS sent successfully to {phone}")
                    else:
                        print(f"DEBUG: SMS sending failed for {phone}")
                    
                except Exception as sms_e:
                    print(f"DEBUG: Failed to send SMS: {sms_e}")
                    
        except Exception as e:
            print(f"DEBUG: Failed to send welcome email: {e}")
        
        return jsonify({
            'success': True, 
            'client_id': user.id,
            'message': f'Клиент {full_name} успешно добавлен. Данные для входа отправлены на email {email}' + (f' и SMS на {phone}' if phone else '') + '.',
            'client_data': {
                'id': user.id,
                'full_name': user.full_name,
                'email': user.email,
                'phone': user.phone,
                'user_id': user.user_id,
                'login_url': f"{request.url_root.rstrip('/')}/login",
                'temp_password': temp_password  # Include for manager reference
            }
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error adding client: {str(e)}")
        return jsonify({'success': False, 'error': f'Ошибка сервера: {str(e)}'}), 500

@app.route('/manager/get-client/<int:client_id>')
@manager_required
def manager_get_client(client_id):
    """Get client data for editing"""
    from models import User

# ================================
# DEAL MANAGEMENT API ENDPOINTS
# ================================

@app.route('/api/deals', methods=['POST'])
@manager_required
@require_json_csrf
def api_create_deal():
    """Create new deal (managers only)"""
    from models import Deal, Manager, User, ResidentialComplex
    from decimal import Decimal
    
    try:
        current_manager = current_user
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'error': 'Нет данных для обработки'}), 400
        
        # Validation - client_id and price are required
        if 'client_id' not in data or not data['client_id']:
            return jsonify({'success': False, 'error': 'Поле client_id обязательно'}), 400
        if 'property_price' not in data or not data['property_price']:
            return jsonify({'success': False, 'error': 'Поле property_price обязательно'}), 400
        
        # Get complex name from request
        complex_name = data.get('residential_complex_name', '').strip()
        if not complex_name:
            return jsonify({'success': False, 'error': 'Необходимо указать название ЖК'}), 400
        
        # Validate client exists and belongs to this manager
        client_id = int(data['client_id'])
        client = User.query.get(client_id)
        if not client:
            return jsonify({'success': False, 'error': 'Клиент не найден'}), 404
        
        # Менеджер может создавать сделки для:
        # 1. Своих назначенных клиентов (assigned_manager_id == current_manager.id)
        # 2. Неназначенных клиентов (assigned_manager_id is None)
        if client.assigned_manager_id is not None and client.assigned_manager_id != current_manager.id:
            return jsonify({'success': False, 'error': 'Этот клиент уже назначен другому менеджеру'}), 403
        
        # Validate price and cashback amounts
        try:
            property_price = Decimal(str(data['property_price']))
            cashback_amount = Decimal(str(data['cashback_amount']))
            
            if property_price <= 0:
                return jsonify({'success': False, 'error': 'Стоимость объекта должна быть больше 0'}), 400
            
            if cashback_amount < 0:
                return jsonify({'success': False, 'error': 'Сумма кешбека не может быть отрицательной'}), 400
                
            # Get complex cashback rate for validation
            max_rate = Decimal('0.15')  # Default max 15% cashback
            if cashback_amount > property_price * max_rate:  # Max cashback validation
                return jsonify({'success': False, 'error': f'Сумма кешбека не может превышать {max_rate * 100}% от стоимости объекта'}), 400
                
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Некорректные значения цены или кешбека'}), 400
        
        # Create new deal
        deal = Deal(
            manager_id=current_manager.id,
            client_id=data['client_id'],
            residential_complex_name=complex_name,  # Save complex name as text
            property_price=property_price,
            cashback_amount=cashback_amount,
            property_description=data.get('property_description', ''),
            property_floor=data.get('property_floor'),
            property_area=data.get('property_area'),
            property_rooms=data.get('property_rooms', ''),
            status=data.get('status', 'new'),
            notes=data.get('notes', ''),
            client_notes=data.get('client_notes', '')
        )
        
        db.session.add(deal)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Сделка успешно создана',
            'deal': {
                'id': deal.id,
                'deal_number': deal.deal_number,
                'status': deal.status,
                'status_display': deal.status_display,
                'property_price': float(deal.property_price),
                'cashback_amount': float(deal.cashback_amount),
                'client_name': client.full_name,
                'complex_name': complex_name,
                'created_at': deal.created_at.isoformat()
            }
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error creating deal: {e}")
        return jsonify({'success': False, 'error': 'Ошибка сервера при создании сделки'}), 500


@app.route('/api/deals', methods=['GET'])
def api_get_deals():
    """Get list of deals with filtering"""
    from models import Deal, Manager, User, ResidentialComplex
    from flask_login import current_user
    
    try:
        # Check if user is manager or client
        is_manager = current_user.is_authenticated and isinstance(current_user._get_current_object(), Manager)
        
        if is_manager:
            # Manager can see all their deals
            current_manager = current_user
            deals_query = Deal.query.filter_by(manager_id=current_manager.id)
        elif current_user.is_authenticated:
            # Client can only see their own deals
            user_id = current_user.id
            deals_query = Deal.query.filter_by(client_id=user_id)
        else:
            # No authentication
            return jsonify({'success': False, 'error': 'Не авторизован'}), 401
        
        # Apply status filtering if provided
        status_filter = request.args.get('status')
        if status_filter:
            status_list = [s.strip() for s in status_filter.split(',') if s.strip()]
            if status_list:
                deals_query = deals_query.filter(Deal.status.in_(status_list))
        
        # Order by creation date (newest first)
        deals = deals_query.order_by(Deal.created_at.desc()).all()
        
        # Format response
        deals_data = []
        for deal in deals:
            deals_data.append({
                'id': deal.id,
                'deal_number': deal.deal_number,
                'status': deal.status,
                'status_display': deal.status_display,
                'status_color': deal.status_color,
                'property_price': float(deal.property_price),
                'cashback_amount': float(deal.cashback_amount),
                'cashback_percentage': deal.get_cashback_percentage(),
                'property_description': deal.property_description,
                'property_floor': deal.property_floor,
                'property_area': deal.property_area,
                'property_rooms': deal.property_rooms,
                'notes': deal.notes,
                'client_notes': deal.client_notes,
                'client_name': deal.client.full_name,
                'manager_name': deal.manager.full_name,
                'complex_name': deal.residential_complex_name or (deal.residential_complex.name if deal.residential_complex else ''),
                'contract_date': deal.contract_date.isoformat() if deal.contract_date else None,
                'completion_date': deal.completion_date.isoformat() if deal.completion_date else None,
                'created_at': deal.created_at.isoformat(),
                'updated_at': deal.updated_at.isoformat(),
                'can_edit': deal.can_edit(current_manager.id if is_manager else current_user.id, is_manager)
            })
        
        return jsonify({
            'success': True,
            'deals': deals_data,
            'total': len(deals_data),
            'is_manager': is_manager
        })
        
    except Exception as e:
        print(f"Error getting deals: {e}")
        return jsonify({'success': False, 'error': 'Ошибка сервера при получении сделок'}), 500


@app.route('/api/deals/<int:deal_id>', methods=['GET'])
@login_required
def api_get_deal(deal_id):
    """Get specific deal with access control"""
    from models import Deal
    
    try:
        deal = Deal.query.get(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Сделка не найдена'}), 404
        
        # Check access rights
        is_manager = current_user.is_authenticated and isinstance(current_user._get_current_object(), Manager)
        
        if is_manager:
            # Manager can only see their own deals
            current_manager = current_user
            if deal.manager_id != current_manager.id:
                return jsonify({'success': False, 'error': 'Недостаточно прав для просмотра этой сделки'}), 403
        else:
            # Client can only see their own deals
            if deal.client_id != current_user.id:
                return jsonify({'success': False, 'error': 'Недостаточно прав для просмотра этой сделки'}), 403
        
        # Return deal data
        deal_data = {
            'id': deal.id,
            'deal_number': deal.deal_number,
            'status': deal.status,
            'status_display': deal.status_display,
            'status_color': deal.status_color,
            'property_price': float(deal.property_price),
            'cashback_amount': float(deal.cashback_amount),
            'cashback_percentage': deal.get_cashback_percentage(),
            'property_description': deal.property_description,
            'property_floor': deal.property_floor,
            'property_area': deal.property_area,
            'property_rooms': deal.property_rooms,
            'notes': deal.notes,
            'client_notes': deal.client_notes,
            'client_name': deal.client.full_name,
            'client_email': deal.client.email,
            'client_phone': deal.client.phone,
            'manager_name': deal.manager.full_name,
            'manager_email': deal.manager.email,
            'manager_phone': deal.manager.phone,
            'complex_name': deal.residential_complex_name or (deal.residential_complex.name if deal.residential_complex else ''),
            'complex_id': deal.residential_complex.id if deal.residential_complex else None,
            'contract_date': deal.contract_date.isoformat() if deal.contract_date else None,
            'completion_date': deal.completion_date.isoformat() if deal.completion_date else None,
            'created_at': deal.created_at.isoformat(),
            'updated_at': deal.updated_at.isoformat(),
            'can_edit': deal.can_edit(current_manager.id if is_manager else current_user.id, is_manager)
        }
        
        return jsonify({
            'success': True,
            'deal': deal_data
        })
        
    except Exception as e:
        print(f"Error getting deal {deal_id}: {e}")
        return jsonify({'success': False, 'error': 'Ошибка сервера при получении сделки'}), 500


@app.route('/api/deals/<int:deal_id>', methods=['PUT'])
@require_json_csrf
def api_update_deal(deal_id):
    """Update deal (status, notes)"""
    from models import Deal
    from datetime import datetime, date
    from flask_login import current_user
    
    try:
        # Check authentication
        is_manager = current_user.is_authenticated and isinstance(current_user._get_current_object(), Manager)
        
        if not is_manager and not current_user.is_authenticated:
            return jsonify({'success': False, 'error': 'Не авторизован'}), 401
        
        deal = Deal.query.get(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Сделка не найдена'}), 404
        
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Нет данных для обновления'}), 400
        
        if is_manager:
            # Manager can only update their own deals
            current_manager = current_user
            if deal.manager_id != current_manager.id:
                return jsonify({'success': False, 'error': 'Недостаточно прав для редактирования этой сделки'}), 403
            
            # Manager can update all fields
            allowed_fields = ['status', 'notes', 'client_notes', 'property_description', 
                            'property_floor', 'property_area', 'property_rooms', 
                            'contract_date', 'completion_date']
        else:
            # Client can only update their own deals and limited fields
            if deal.client_id != current_user.id:
                return jsonify({'success': False, 'error': 'Недостаточно прав для редактирования этой сделки'}), 403
            
            # Client can only update notes and only if deal is in editable status
            if deal.status not in ['new', 'object_reserved']:
                return jsonify({'success': False, 'error': 'Сделка больше не может быть отредактирована'}), 403
            
            allowed_fields = ['client_notes']
        
        # Update allowed fields
        updated_fields = []
        old_status = deal.status  # Запоминаем старый статус
        
        for field in allowed_fields:
            if field in data:
                if field == 'status':
                    # Validate status
                    valid_statuses = ['new', 'object_reserved', 'mortgage', 'successful', 'rejected']
                    if data[field] not in valid_statuses:
                        return jsonify({'success': False, 'error': f'Недопустимый статус: {data[field]}'}), 400
                    # Save validated status
                    setattr(deal, field, data[field])
                    updated_fields.append(field)
                
                elif field in ['contract_date', 'completion_date']:
                    # Handle date fields
                    if data[field]:
                        try:
                            date_value = datetime.strptime(data[field], '%Y-%m-%d').date()
                            setattr(deal, field, date_value)
                            updated_fields.append(field)
                        except ValueError:
                            return jsonify({'success': False, 'error': f'Некорректный формат даты для {field}. Используйте YYYY-MM-DD'}), 400
                    else:
                        setattr(deal, field, None)
                        updated_fields.append(field)
                else:
                    # Handle text fields
                    setattr(deal, field, data[field])
                    updated_fields.append(field)
        
        if not updated_fields:
            return jsonify({'success': False, 'error': 'Нет полей для обновления'}), 400
        
        # Update timestamp
        deal.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        # Логируем изменение статуса
        if 'status' in updated_fields and old_status != deal.status:
            from models import UserActivity
            status_display_map = {
                'new': 'Новая',
                'object_reserved': 'Объект зарезервирован',
                'mortgage': 'Ипотека',
                'successful': 'Успешно завершена',
                'rejected': 'Отклонена'
            }
            old_status_display = status_display_map.get(old_status, old_status)
            new_status_display = status_display_map.get(deal.status, deal.status)
            
            UserActivity.log_activity(
                user_id=deal.client_id,
                activity_type='deal_status_update',
                description=f'Статус сделки {deal.deal_number} изменен с "{old_status_display}" на "{new_status_display}"'
            )
        
        return jsonify({
            'success': True,
            'message': 'Сделка успешно обновлена',
            'updated_fields': updated_fields,
            'deal': {
                'id': deal.id,
                'deal_number': deal.deal_number,
                'status': deal.status,
                'status_display': deal.status_display,
                'status_color': deal.status_color,
                'property_price': float(deal.property_price),
                'cashback_amount': float(deal.cashback_amount),
                'cashback_percentage': deal.get_cashback_percentage(),
                'property_description': deal.property_description,
                'property_floor': deal.property_floor,
                'property_area': deal.property_area,
                'property_rooms': deal.property_rooms,
                'notes': deal.notes,
                'client_notes': deal.client_notes,
                'client_name': deal.client.full_name,
                'complex_name': deal.residential_complex_name or (deal.residential_complex.name if deal.residential_complex else ''),
                'contract_date': deal.contract_date.isoformat() if deal.contract_date else None,
                'completion_date': deal.completion_date.isoformat() if deal.completion_date else None,
                'updated_at': deal.updated_at.isoformat()
            }
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error updating deal {deal_id}: {e}")
        return jsonify({'success': False, 'error': 'Ошибка сервера при обновлении сделки'}), 500


@app.route('/api/deals/<int:deal_id>', methods=['DELETE'])
@manager_required
@require_json_csrf
def api_delete_deal(deal_id):
    """Delete deal (managers only)"""
    from models import Deal
    
    try:
        deal = Deal.query.get(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Сделка не найдена'}), 404
        
        current_manager = current_user
        
        # Check if manager owns this deal
        if deal.manager_id != current_manager.id:
            return jsonify({'success': False, 'error': 'Недостаточно прав для удаления этой сделки'}), 403
        
        # Check if deal can be deleted (only new or rejected deals)
        if deal.status not in ['new', 'rejected']:
            return jsonify({'success': False, 'error': 'Нельзя удалить сделку со статусом "' + deal.status_display + '"'}), 400
        
        deal_number = deal.deal_number
        db.session.delete(deal)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Сделка {deal_number} успешно удалена'
        })
        
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting deal {deal_id}: {e}")
        return jsonify({'success': False, 'error': 'Ошибка сервера при удалении сделки'}), 500

@app.route('/manager/edit-client', methods=['POST'])
@manager_required
def manager_edit_client():
    """Edit existing client"""
    from models import User
    
    current_manager = current_user
    
    try:
        client_id = request.form.get('client_id')
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        is_active = 'is_active' in request.form
        
        if not client_id:
            return jsonify({'success': False, 'error': 'ID клиента не указан'}), 400
        
        # Try to find client assigned to this manager first, then any buyer
        client = User.query.filter_by(id=client_id, assigned_manager_id=current_manager.id).first()
        if not client:
            client = User.query.filter_by(id=client_id, role='buyer').first()
        
        if not client:
            return jsonify({'success': False, 'error': 'Клиент не найден'}), 404
        
        if not all([full_name, email]):
            return jsonify({'success': False, 'error': 'Заполните обязательные поля'}), 400
        
        # Check if email already exists (excluding current client)
        existing_user = User.query.filter(User.email == email, User.id != client_id).first()
        if existing_user:
            return jsonify({'success': False, 'error': 'Пользователь с таким email уже существует'}), 400
        
        # Update client data
        client.full_name = full_name
        client.email = email
        client.phone = phone
        client.is_active = is_active
        client.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify({'success': True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/manager/delete-client', methods=['POST'])
@manager_required
def manager_delete_client():
    """Delete client"""
    from models import User
    
    current_manager = current_user
    
    try:
        # Handle both JSON and form data
        if request.content_type == 'application/json':
            data = request.get_json()
            client_id = data.get('client_id')
        else:
            client_id = request.form.get('client_id')
        
        if not client_id:
            return jsonify({'success': False, 'error': 'ID клиента не указан'}), 400
        
        # Try to find client assigned to this manager first, then any buyer
        client = User.query.filter_by(id=client_id, assigned_manager_id=current_manager.id).first()
        if not client:
            client = User.query.filter_by(id=client_id, role='buyer').first()
        
        if not client:
            return jsonify({'success': False, 'error': 'Клиент не найден'}), 404
        
        # Instead of deleting, mark as inactive
        client.is_active = False
        client.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify({'success': True})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

def send_callback_notification_email(callback_req, manager):
    """Send email notification about callback request"""
    try:
        from email_service import send_email
        
        # Email content
        subject = f"Новая заявка на обратный звонок - {callback_req.name}"
        
        # Build message content
        content = f"""
        Получена новая заявка на обратный звонок:
        
        Клиент: {callback_req.name}
        Телефон: {callback_req.phone}
        Email: {callback_req.email or 'Не указан'}
        Удобное время: {callback_req.preferred_time}
        
        Интересует: {callback_req.interest}
        Бюджет: {callback_req.budget}
        Планирует покупку: {callback_req.timing}
        
        Дополнительно: {callback_req.notes or 'Нет дополнительной информации'}
        
        Назначенный менеджер: {manager.full_name if manager else 'Не назначен'}
        Дата заявки: {callback_req.created_at.strftime('%d.%m.%Y %H:%M')}
        """
        
        # Try to send to manager first, then to admin email
        recipient_email = manager.email if manager else 'admin@inback.ru'
        
        success = send_email(
            to_email=recipient_email,
            subject=subject,
            content=content,
            template_name='notification'
        )
        
        if success:
            print(f"✓ Callback notification email sent to {recipient_email}")
        else:
            print(f"✗ Failed to send callback notification email to {recipient_email}")
            
    except Exception as e:
        print(f"Error sending callback notification email: {e}")


def send_callback_notification_telegram(callback_req, manager):
    """Send Telegram notification about callback request"""
    try:
        # Check if telegram_bot module can be imported
        try:
            from telegram_bot import send_telegram_message
        except ImportError as e:
            print(f"Telegram bot not available: {e}")
            return False
        
        # Calculate potential cashback
        potential_cashback = ""
        if callback_req.budget:
            if "млн" in callback_req.budget:
                # Extract average from range like "3-5 млн"
                numbers = [float(x) for x in callback_req.budget.replace(" млн", "").replace("руб", "").split("-") if x.strip().replace(".", "").replace(",", "").isdigit()]
                if numbers:
                    avg_price = sum(numbers) / len(numbers) * 1000000
                    cashback = int(avg_price * 0.02)
                    potential_cashback = f"💰 *Потенциальный кэшбек:* {cashback:,} руб. (2%)\n"
        
        # Enhanced Telegram message
        message = f"""📞 *НОВАЯ ЗАЯВКА НА ОБРАТНЫЙ ЗВОНОК*

👤 *КОНТАКТНАЯ ИНФОРМАЦИЯ:*
• Имя: {callback_req.name}
• Телефон: {callback_req.phone}
• Email: {callback_req.email or 'Не указан'}
• Удобное время звонка: {callback_req.preferred_time}

🔍 *КРИТЕРИИ ПОИСКА:*
• Интересует: {callback_req.interest or 'Не указано'}
• Бюджет: {callback_req.budget or 'Не указан'}
• Планы на покупку: {callback_req.timing or 'Не указано'}

{potential_cashback}📝 *ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ:*
{callback_req.notes or 'Нет дополнительной информации'}

📅 *ВРЕМЯ ЗАЯВКИ:* {callback_req.created_at.strftime('%d.%m.%Y в %H:%M')}
🌐 *ИСТОЧНИК:* Форма обратного звонка на сайте InBack.ru
👨‍💼 *НАЗНАЧЕННЫЙ МЕНЕДЖЕР:* {manager.full_name if manager else 'Не назначен'}

📋 *СЛЕДУЮЩИЕ ШАГИ:*
1️⃣ Перезвонить клиенту в указанное время
2️⃣ Провести консультацию по критериям
3️⃣ Подготовить персональную подборку
4️⃣ Запланировать показы объектов

⚡ *ВАЖНО:* Соблюдайте время, удобное для клиента!"""
        
        # Always send to admin chat for now
        chat_id = "730764738"  # Admin chat
        
        success = send_telegram_message(chat_id, message)
        
        if success:
            print(f"✓ Callback notification sent to Telegram chat {chat_id}")
        else:
            print(f"✗ Failed to send callback notification to Telegram")
            
    except Exception as e:
        print(f"Error sending callback notification to Telegram: {e}")


# Database initialization happens in the app context below

@app.route('/api/blog/search')
def blog_search_api():
    """API endpoint for instant blog search and suggestions"""
    from models import BlogPost, Category
    from sqlalchemy import or_, func
    
    try:
        query = request.args.get('q', '').strip()
        category = request.args.get('category', '').strip()
        suggestions_only = request.args.get('suggestions', '').lower() == 'true'
        
        # Start with base query - use BlogPost (where data actually is)
        search_query = BlogPost.query.filter(BlogPost.status == 'published')
        
        # Apply search filter
        if query:
            search_query = search_query.filter(
                or_(
                    BlogPost.title.ilike(f'%{query}%'),
                    BlogPost.content.ilike(f'%{query}%'),
                    BlogPost.excerpt.ilike(f'%{query}%')
                )
            )
        
        # Apply category filter
        if category:
            search_query = search_query.filter(BlogPost.category == category)
        
        # For suggestions, limit to title matches only
        if suggestions_only:
            if query:
                suggestions = search_query.filter(
                    BlogPost.title.ilike(f'%{query}%')
                ).limit(5).all()
                
                return jsonify({
                    'suggestions': [{
                        'title': post.title,
                        'slug': post.slug,
                        'category': post.category or 'Общее'
                    } for post in suggestions]
                })
            else:
                return jsonify({'suggestions': []})
        
        # For full search, return formatted articles
        articles = search_query.order_by(BlogPost.created_at.desc()).limit(20).all()
        
        formatted_articles = []
        for article in articles:
            formatted_articles.append({
                'title': article.title,
                'slug': article.slug,
                'excerpt': article.excerpt or '',
                'featured_image': article.featured_image or '',
                'category': article.category or 'Общее',
                'date': article.created_at.strftime('%d.%m.%Y'),
                'reading_time': getattr(article, 'reading_time', 5),
                'views': getattr(article, 'views', 0)
            })
        return jsonify({
            'articles': formatted_articles,
            'total': len(formatted_articles)
        })
        
    except Exception as e:
        print(f"ERROR in blog search API: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Search failed', 'articles': [], 'suggestions': []}), 500

# Developer Scraper Management Endpoints
@app.route('/admin/scraper')
@admin_required
def admin_scraper():
    """Admin panel for developer scraper management"""
    from models import Admin
    
    # ИСПРАВЛЕНО: Используем Flask-Login current_user
    admin = current_user
    
    return render_template('admin/scraper.html', admin=admin)

@app.route('/admin/scraper/run', methods=['POST'])
@admin_required
def run_scraper():
    """Run the AI-powered developer scraper"""
    try:
        from developer_parser_integration import DeveloperParserService
        
        # Получаем параметр лимита (по умолчанию 10)
        limit = 10
        try:
            data = request.get_json(force=True) if request.data else {}
        except:
            data = {}
        
        if data:
            limit = data.get('limit', 10)
        
        service = DeveloperParserService()
        result = service.parse_and_save_developers(limit=limit)
        
        return jsonify({
            'success': True,
            'stats': {
                'developers_created': result.get('created', 0),
                'developers_updated': result.get('updated', 0),
                'total_processed': result.get('total_processed', 0),
                'errors': result.get('errors', 0)
            },
            'message': f'ИИ-парсинг завершен! Обработано {result["total_processed"]} застройщиков. Создано: {result["created"]}, обновлено: {result["updated"]}',
            'errors_list': result.get('errors_list', [])
        })
        
    except Exception as e:
        print(f"AI Scraper error: {e}")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            'success': False,
            'message': f'Ошибка при ИИ-парсинге: {str(e)}'
        }), 500

@app.route('/admin/scraper/test', methods=['POST'])
@admin_required
def test_scraper():
    """Test AI scraper with sample data"""
    try:
        # Простые тестовые данные
        test_data = {
            'name': 'Тестовый застройщик',
            'description': 'Описание тестового застройщика',
            'website': 'https://example.com',
            'phone': '+7-918-000-00-00',
            'email': 'test@example.com'
        }
        
        return jsonify({
            'success': True,
            'data': test_data,
            'stats': {
                'developers_tested': 1,
                'complexes_found': 0,
                'ai_extraction': True,
                'mock_data': True
            },
            'message': 'ИИ-тест завершен! Застройщик: Тестовый застройщик'
        })
        
    except Exception as e:
        print(f"AI Scraper test error: {e}")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            'success': False,
            'message': f'Ошибка при тестировании ИИ-парсера: {str(e)}'
        }), 500

@app.route('/admin/scraper/statistics')
@admin_required
def scraper_statistics():
    """Get AI parser statistics"""
    try:
        from developer_parser_integration import DeveloperParserService
        
        service = DeveloperParserService()
        stats = service.get_parsing_statistics()
        
        return jsonify({
            'success': True,
            'data': stats
        })
        
    except Exception as e:
        print(f"Statistics error: {e}")
        return jsonify({
            'success': False,
            'message': f'Ошибка получения статистики: {str(e)}'
        }), 500

@app.route('/admin/scraper/files')
@admin_required
def scraper_files():
    """List scraped data files"""
    try:
        import glob
        import os
        from datetime import datetime
        
        files = glob.glob('scraped_developers_*.json')
        file_info = []
        
        for file in files:
            stat = os.stat(file)
            file_info.append({
                'name': file,
                'size': stat.st_size,
                'created': datetime.fromtimestamp(stat.st_ctime).strftime('%d.%m.%Y %H:%M'),
                'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%d.%m.%Y %H:%M')
            })
        
        # Sort by creation time, newest first
        file_info.sort(key=lambda x: x['modified'], reverse=True)
        
        return jsonify({
            'success': True,
            'files': file_info
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Ошибка при получении списка файлов: {str(e)}'
        }), 500

@app.route('/admin/scraper/view-file/<filename>')
@admin_required
def view_scraped_file(filename):
    """View scraped data file content"""
    try:
        import json
        import os
        
        # Security check - only allow scraped files
        if not filename.startswith('scraped_developers_') or not filename.endswith('.json'):
            return jsonify({'success': False, 'message': 'Недопустимое имя файла'}), 400
        
        if not os.path.exists(filename):
            return jsonify({'success': False, 'message': 'Файл не найден'}), 404
        
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        return jsonify({
            'success': True,
            'data': data,
            'filename': filename
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Ошибка при чтении файла: {str(e)}'
        }), 500

@app.route('/admin/upload-excel', methods=['POST'])
def admin_upload_excel():
    """Handle Excel file upload from admin panel"""
    try:
        if 'excel_file' not in request.files:
            return jsonify({'success': False, 'error': 'Файл не выбран'})
        
        file = request.files['excel_file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Файл не выбран'})
        
        if not file.filename.endswith(('.xlsx', '.xls')):
            return jsonify({'success': False, 'error': 'Поддерживаются только файлы Excel (.xlsx, .xls)'})
        
        # Save file to attached_assets directory
        import os
        import uuid
        
        # Ensure attached_assets directory exists
        os.makedirs('attached_assets', exist_ok=True)
        
        # Generate unique filename
        file_extension = os.path.splitext(file.filename)[1]
        unique_filename = f"upload_{uuid.uuid4().hex[:8]}{file_extension}"
        file_path = os.path.join('attached_assets', unique_filename)
        
        # Save the file
        file.save(file_path)
        
        # Запуск импорта в фоновом процессе для больших файлов
        try:
            import threading
            import time
            
            # Создаем уникальный ID задачи
            task_id = unique_filename.replace('.', '_')
            
            # Статус импорта (будем хранить в глобальной переменной)
            global import_status
            if 'import_status' not in globals():
                import_status = {}
            
            import_status[task_id] = {
                'status': 'processing',
                'progress': 0,
                'message': 'Обработка файла...',
                'started_at': time.time()
            }
            
            def background_import():
                try:
                    with app.app_context():
                        result = import_excel_to_database(file_path)
                    
                    # Обновляем статус при успехе
                    import_status[task_id] = {
                        'status': 'completed',
                        'progress': 100,
                        'message': f'✅ {result["message"]} Импортировано: {result["imported"]} записей.',
                        'result': result,
                        'completed_at': time.time()
                    }
                    
                    # Очищаем кеш
                    global _properties_cache, _cache_timestamp
                    _properties_cache = None
                    _cache_timestamp = None
                    
                except Exception as import_error:
                    # Обновляем статус при ошибке
                    import_status[task_id] = {
                        'status': 'error',
                        'progress': 0,
                        'message': f'❌ Ошибка импорта: {str(import_error)}',
                        'error': str(import_error),
                        'failed_at': time.time()
                    }
            
            # Запускаем импорт в отдельном потоке
            thread = threading.Thread(target=background_import, daemon=True)
            thread.start()
            
            # Сразу возвращаем ответ о начале обработки
            return jsonify({
                'success': True,
                'message': f'📤 Файл загружен! Обработка запущена в фоне. Проверьте статус через несколько минут.',
                'task_id': task_id,
                'background': True
            })
            
        except Exception as import_error:
            return jsonify({
                'success': False, 
                'error': f'Ошибка запуска импорта: {str(import_error)}'
            })
            
    except Exception as e:
        return jsonify({'success': False, 'error': f'Ошибка обработки файла: {str(e)}'})

@app.route('/admin/check-import-status/<task_id>')
def admin_check_import_status(task_id):
    """Проверка статуса фонового импорта"""
    try:
        global import_status
        if 'import_status' not in globals():
            import_status = {}
        
        if task_id not in import_status:
            return jsonify({
                'success': False,
                'error': 'Задача не найдена'
            })
        
        status_info = import_status[task_id]
        
        # Добавляем время обработки
        import time
        if 'started_at' in status_info:
            elapsed = time.time() - status_info['started_at']
            status_info['elapsed_time'] = f"{elapsed:.1f} сек"
        
        return jsonify({
            'success': True,
            'status': status_info
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Ошибка получения статуса: {str(e)}'
        })

# ================== REGIONAL FUNCTIONS ==================

def parse_address_components(address_display_name):
    """
    Парсит полный адрес и извлекает регион, город и район
    Пример: "Россия, Краснодарский край, Сочи, Кудепста м-н, Искры, 88 лит7"
    Возвращает: {'region': 'Краснодарский край', 'city': 'Сочи', 'district': 'Кудепста м-н'}
    """
    if not address_display_name:
        return {'region': None, 'city': None, 'district': None}
    
    # Разделяем адрес по запятым
    parts = [part.strip() for part in address_display_name.split(',')]
    
    result = {'region': None, 'city': None, 'district': None}
    
    # Ищем регион (обычно содержит "край", "область", "республика")
    for part in parts:
        if any(keyword in part.lower() for keyword in ['край', 'область', 'республика', 'федерация']):
            result['region'] = part
            break
    
    # Ищем город (после региона, обычно не содержит специальных суффиксов)
    region_found = False
    for part in parts:
        if result['region'] and part == result['region']:
            region_found = True
            continue
        
        if region_found and part != 'Россия':
            # Проверяем что это не улица или дом
            if not any(keyword in part.lower() for keyword in ['ул', 'улица', 'проспект', 'пр-т', 'переулок', 'пер', 'м-н', 'лит', 'стр', 'корп', 'д.']):
                # Проверяем что это не номер и не псевдо-город типа "Краснодар 6"
                if not part.replace(' ', '').replace('а', '').replace('б', '').replace('в', '').replace('г', '').isdigit():
                    # Дополнительная проверка на псевдо-города (название + пробел + число)
                    import re
                    if not re.match(r'^[а-яё]+\s+\d+$', part.lower()):
                        result['city'] = part
                        break
    
    # Ищем район/микрорайон (обычно содержит "м-н", "р-н" или идет после города)
    city_found = False
    for part in parts:
        if result['city'] and part == result['city']:
            city_found = True
            continue
            
        if city_found:
            # Если это район/микрорайон
            if any(keyword in part.lower() for keyword in ['м-н', 'р-н', 'район', 'микрорайон', 'мкр']):
                result['district'] = part
                break
            # Или если это название района без суффиксов (первое после города)
            elif not any(keyword in part.lower() for keyword in ['ул', 'улица', 'проспект', 'пр-т', 'лит', 'стр', 'корп', 'дом', 'д.']):
                # Проверяем что это не номер дома (содержит только цифры и буквы типа 2А, 10, 36 и т.д.)
                if not (part.replace('/', '').replace('к', '').replace('стр', '').replace('а', '').replace('б', '').replace('в', '').replace('г', '').replace(' ', '').isdigit() or len(part) <= 5):
                    result['district'] = part
                    break
    
    return result

def get_or_create_region(region_name):
    """Получить или создать регион в базе данных"""
    if not region_name:
        return None
        
    from models import Region
    
    # Ищем существующий регион
    region = Region.query.filter_by(name=region_name).first()
    
    if not region:
        # Создаем новый регион
        slug = region_name.lower().replace(' ', '-').replace('ский', '').replace('край', 'krai')
        region = Region(
            name=region_name,
            slug=slug,
            is_active=True,
            is_default=(region_name == 'Краснодарский край')  # Краснодарский край по умолчанию
        )
        db.session.add(region)
        try:
            db.session.commit()
            print(f"Created new region: {region_name}")
        except Exception as e:
            db.session.rollback()
            print(f"Error creating region {region_name}: {e}")
            return None
    
    return region

def get_or_create_city(city_name, region):
    """Получить или создать город в регионе"""
    if not city_name or not region:
        return None
        
    from models import City
    
    # Ищем существующий город в этом регионе
    city = City.query.filter_by(name=city_name, region_id=region.id).first()
    
    if not city:
        # Создаем новый город
        slug = city_name.lower().replace(' ', '-')
        city = City(
            name=city_name,
            slug=slug,
            region_id=region.id,
            is_active=True,
            is_default=(city_name == 'Краснодар')  # Краснодар по умолчанию
        )
        db.session.add(city)
        try:
            db.session.commit()
            print(f"Created new city: {city_name} in {region.name}")
        except Exception as e:
            db.session.rollback()
            print(f"Error creating city {city_name}: {e}")
            return None
    
    return city

def update_properties_with_regions():
    """Обновить все объекты недвижимости с региональной привязкой"""
    from models import ExcelProperty
    
    # ✅ MIGRATED: Use normalized Property model
    properties = Property.query.all()
    updated_count = 0
    
    print(f"Updating {len(properties)} properties with regional data...")
    
    for prop in properties:
        if prop.address_display_name:
            # Парсим адрес
            address_parts = parse_address_components(prop.address_display_name)
            
            # Обновляем парсеные поля
            prop.parsed_region = address_parts['region']
            prop.parsed_city = address_parts['city'] 
            prop.parsed_district = address_parts['district']
            
            # Создаем/находим регион и город
            if address_parts['region']:
                region = get_or_create_region(address_parts['region'])
                if region:
                    prop.region_id = region.id
                    
                    if address_parts['city']:
                        city = get_or_create_city(address_parts['city'], region)
                        if city:
                            prop.city_id = city.id
            
            updated_count += 1
            
            # Сохраняем по частям для избежания таймаутов
            if updated_count % 50 == 0:
                try:
                    db.session.commit()
                    print(f"Updated {updated_count} properties...")
                except Exception as e:
                    print(f"Error committing batch: {str(e)}")
                    db.session.rollback()
    
    # Финальный коммит
    try:
        db.session.commit()
        print(f"✅ Successfully updated {updated_count} properties with regional data")
    except Exception as e:
        print(f"Error in final commit: {str(e)}")
        db.session.rollback()
    
    return updated_count
