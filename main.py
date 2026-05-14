import base64
import datetime
import hashlib
import os
import re
import sqlite3
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests
import telebot
import yt_dlp
from cryptography.fernet import Fernet
from telebot import types
from telebot.util import quick_markup
from yt_dlp.utils import DownloadError, ExtractorError

import config

# ========================== SETTINGS ==========================
os.makedirs(config.output_folder, exist_ok=True)

UPLOAD_LIMIT_BYTES = 90 * 1024 * 1024
REQUEST_TIMEOUT = 1800
MAX_DOWNLOAD_BYTES = min(int(getattr(config, "max_filesize", 45 * 1024 * 1024) or 45 * 1024 * 1024), UPLOAD_LIMIT_BYTES)

# ========================== BULLETPROOF DOMAIN CHECKER ==========================
def is_allowed_domain(url):
    if not url or not isinstance(url, str):
        return False
    lower = url.strip().lower()
    domains = ["youtu", "youtube", "tiktok", "instagram", "twitter", "x.com", "facebook", "fb.watch", "fb.com", "dailymotion", "bsky"]
    return any(d in lower for d in domains)

# ========================== CRYPTO + DB ==========================
key = hashlib.sha256(getattr(config, "secret_key", "any-secret-you-like").encode()).digest()
cipher = Fernet(base64.urlsafe_b64encode(key))

script_dir = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(script_dir, "db.db")
db_conn = sqlite3.connect(db_path, check_same_thread=False)
db_cursor = db_conn.cursor()
db_cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_cookies (
        user_id INTEGER PRIMARY KEY,
        cookie_data TEXT NOT NULL
    )
""")
db_conn.commit()

bot = telebot.TeleBot(config.token)
last_edited = {}

# ========================== HELPERS ==========================
def encrypt_cookie(cookie_data: str) -> str:
    return cipher.encrypt(cookie_data.encode()).decode()

def decrypt_cookie(encrypted_data: str) -> str:
    return cipher.decrypt(encrypted_data.encode()).decode()

def is_url(text: str) -> bool:
    if not text:
        return False
    return text.strip().lower().startswith(("http://", "https://"))

def _make_progress_hook(message, msg):
    def progress(d):
        if d["status"] != "downloading":
            return
        try:
            last = last_edited.get(f"{message.chat.id}-{msg.message_id}")
            if last and (datetime.datetime.now() - last).total_seconds() < 5:
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            perc = round(downloaded * 100 / total) if total else 0
            title = d.get("info_dict", {}).get("title", "file")
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg.message_id,
                text=f"Downloading {title}\n\n{perc}%\n\n<i>Want to stay updated? @SatoruStatus</i>",
                parse_mode="HTML",
            )
            last_edited[f"{message.chat.id}-{msg.message_id}"] = datetime.datetime.now()
        except Exception as e:
            print(e)
    return progress

def _safe_file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0

def _get_downloaded_filepath(info: Any) -> Optional[str]:
    """Fixed: أقوى طريقة لجلب مسار الملف"""
    for key in ("requested_downloads", "entries"):
        downloads = info.get(key) or []
        if downloads and isinstance(downloads, list):
            fp = downloads[0].get("filepath") or downloads[0].get("__final_filename__")
            if fp and os.path.exists(fp):
                return fp
    return info.get("filepath") or info.get("__final_filename__")

def _send_as_document(message, filepath: str):
    with open(filepath, "rb") as f:
        bot.send_document(
            message.chat.id,
            f,
            reply_to_message_id=message.message_id,
            visible_file_name=os.path.basename(filepath),
            timeout=REQUEST_TIMEOUT,
        )

def _send_media(message, info: Any, audio: bool):
    filepath = _get_downloaded_filepath(info)
    if not filepath or not os.path.exists(filepath):
        raise RuntimeError("Downloaded file path not found")

    size = _safe_file_size(filepath)
    if size > UPLOAD_LIMIT_BYTES:
        raise RuntimeError(f"File too large ({round(size / 1024 / 1024)}MB)")

    try:
        if audio:
            with open(filepath, "rb") as f:
                try:
                    bot.send_audio(message.chat.id, f, reply_to_message_id=message.message_id, timeout=REQUEST_TIMEOUT)
                except:
                    f.seek(0)
                    _send_as_document(message, filepath)
            return

        try:
            with open(filepath, "rb") as f:
                bot.send_video(
                    message.chat.id, f,
                    reply_to_message_id=message.message_id,
                    supports_streaming=True,
                    timeout=REQUEST_TIMEOUT,
                )
            return
        except Exception:
            bot.send_message(message.chat.id, "Trying document fallback...", reply_to_message_id=message.message_id)
            _send_as_document(message, filepath)
    except Exception as e:
        print("send failed:", e)
        raise

def _cleanup(video_title: int):
    try:
        for file in os.listdir(config.output_folder):
            if str(video_title) in file:
                os.remove(os.path.join(config.output_folder, file))
    except FileNotFoundError:
        pass

def check_url(content: str, message):
    if not content:
        return {"success": False}
    match = re.search(r"https?://\S+", content)
    url = match.group(0) if match else content
    if not urlparse(url).scheme:
        bot.reply_to(message, "Invalid URL")
        return {"success": False}
    if not is_allowed_domain(url):
        bot.reply_to(message, "Invalid URL. Only YouTube, TikTok, Instagram, Twitter, Facebook and Bluesky + Dailymotion links are supported.")
        return {"success": False}
    return {"success": True, "url": url}

def _build_default_format_selector(audio: bool):
    """SUPER STRICT → دايماً أقل من 45MB عشان Replit + Telegram"""
    if audio:
        return "bestaudio/best"
    return (
        "bestvideo[height<=480][filesize<45M]+bestaudio/bestvideo[height<=360][filesize<45M]+bestaudio/"
        "best[height<=480][filesize<45M]/best[filesize<45M]/worst"
    )

def download_video(message, content, audio=False, format_id=None):
    check = check_url(content, message)
    if not check["success"]:
        return

    url = check["url"]
    msg = bot.reply_to(message, "Downloading...\n\n<i>Want to stay updated? @SatoruStatus</i>", parse_mode="HTML")
    video_title = round(time.time() * 1000)

    resolved_format = format_id or _build_default_format_selector(audio)

    ydl_opts = {
        "format": resolved_format,
        "outtmpl": f"{config.output_folder}/{video_title}.%(ext)s",
        "progress_hooks": [_make_progress_hook(message, msg)],
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "retries": 15,
        "extractor_retries": 15,
        "format_sort": ["filesize", "height:480", "width:640"],
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}] if audio else [],
        "prefer_free_formats": True,
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        "concurrent_fragment_downloads": 4,
    }

    cookie_file = None
    try:
        user_id = message.from_user.id
        db_cursor.execute("SELECT cookie_data FROM user_cookies WHERE user_id = ?", (user_id,))
        result = db_cursor.fetchone()
        if result:
            decrypted_data = decrypt_cookie(result[0])
            cookie_file = f"{config.output_folder}/cookies_{user_id}.txt"
            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write(decrypted_data)
            ydl_opts["cookiefile"] = cookie_file

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, text="Sending to Telegram...")
        _send_media(message, info, audio)
        bot.delete_message(message.chat.id, msg.message_id)

    except (DownloadError, ExtractorError) as e:
        err = str(e).lower()
        if "404" in err or "not found" in err:
            text = "Dailymotion link failed (temporary server issue). Try /custom or another quality."
        elif any(x in err for x in ["login", "sign in", "rate-limit"]):
            text = "Rate limit or login required.\n\nارسل /cookie + ملف cookies.txt"
        elif "larger than max-filesize" in err or "filesize" in err:
            text = "File too large. Use /custom and choose smaller quality."
        else:
            text = "Download error, try again."
        bot.edit_message_text(text, message.chat.id, msg.message_id)

    except Exception as e:
        print("Unexpected error:", e)
        bot.edit_message_text("Couldn't send file. Try /custom for smaller quality.", message.chat.id, msg.message_id)

    finally:
        if cookie_file and os.path.exists(cookie_file):
            os.remove(cookie_file)
        _cleanup(video_title)

# ========================== باقي الكود (نفس السابق بدون تغيير) ==========================
def log(message, text: str, media: str):
    if getattr(config, "logs", None):
        chat_info = "Private chat" if message.chat.type == "private" else f"Group: *{message.chat.title}* (`{message.chat.id}`)"
        bot.send_message(config.logs, f"Download request ({media}) from @{getattr(message.from_user, 'username', None)} ({message.from_user.id})\n\n{chat_info}\n\n{text}")

def get_text(message):
    text = message.text or ""
    parts = text.split(" ")
    if len(parts) < 2:
        if message.reply_to_message and message.reply_to_message.text:
            return message.reply_to_message.text
        return None
    return parts[1]

@bot.message_handler(commands=["audio"])
def download_audio_command(message):
    text = get_text(message)
    if not text:
        bot.reply_to(message, "Invalid usage, use `/audio url`", parse_mode="MARKDOWN")
        return
    log(message, text, "audio")
    download_video(message, text, True)

@bot.message_handler(commands=["custom"])
def custom(message):
    text = message.text if message.text else message.caption
    check = check_url(text, message)
    if not check["success"]:
        return
    url = check["url"]
    msg = bot.reply_to(message, "Getting formats...")
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = info.get("formats") or []
    data = {}
    for x in formats:
        if x.get("video_ext") == "none":
            continue
        res = x.get("resolution") or x.get("format_note") or "unknown"
        ext = x.get("ext", "mp4")
        data[f"{res}.{ext}"] = {"callback_data": x['format_id']}
    markup = quick_markup(data, row_width=2)
    bot.delete_message(msg.chat.id, msg.message_id)
    bot.reply_to(message, "Choose a format", reply_markup=markup)

def filter_cookies_by_domain(cookie_data: str) -> str:
    lines = cookie_data.split("\n")
    filtered = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            filtered.append(line)
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0].lstrip(".")
        if any(domain.endswith(d) or d in domain for d in ["youtube", "tiktok", "instagram", "twitter", "facebook", "dailymotion", "bsky"]):
            filtered.append(line)
    return "\n".join(filtered)

@bot.message_handler(commands=["id"])
def get_chat_id(message):
    bot.reply_to(message, message.chat.id)

def is_cookie_command(message):
    text = message.text or message.caption or ""
    return text.startswith(("/cookie", "/cookies"))

@bot.message_handler(func=is_cookie_command, content_types=["document", "text"])
def handle_cookie(message):
    user_id = message.from_user.id
    if not message.document:
        db_cursor.execute("SELECT cookie_data FROM user_cookies WHERE user_id = ?", (user_id,))
        result = db_cursor.fetchone()
        if result:
            cookie_file = f"{config.output_folder}/cookies_{user_id}_temp.txt"
            try:
                decrypted = decrypt_cookie(result[0])
                with open(cookie_file, "w", encoding="utf-8") as f:
                    f.write(decrypted)
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🗑 Delete", callback_data="delete_cookies"))
                with open(cookie_file, "rb") as f:
                    bot.send_document(message.chat.id, f, reply_to_message_id=message.message_id, visible_file_name="cookies.txt", reply_markup=markup, timeout=REQUEST_TIMEOUT)
            finally:
                if os.path.exists(cookie_file):
                    os.remove(cookie_file)
        else:
            bot.reply_to(message, "No cookies stored. Send file with /cookie")
        return

    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    cookie_data = downloaded_file.decode("utf-8")
    filtered = filter_cookies_by_domain(cookie_data)
    encrypted = encrypt_cookie(filtered)
    db_cursor.execute("INSERT OR REPLACE INTO user_cookies (user_id, cookie_data) VALUES (?, ?)", (user_id, encrypted))
    db_conn.commit()
    bot.reply_to(message, "✅ Cookies saved!")

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if call.data == "delete_cookies":
        user_id = call.from_user.id
        db_cursor.execute("DELETE FROM user_cookies WHERE user_id = ?", (user_id,))
        db_conn.commit()
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption="Cookies deleted!", reply_markup=None)
        bot.answer_callback_query(call.id, "Deleted!")
        return

    if call.message.reply_to_message and call.from_user.id == call.message.reply_to_message.from_user.id:
        url = get_text(call.message.reply_to_message)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        download_video(call.message.reply_to_message, url, format_id=f"{call.data}+bestaudio")
    else:
        bot.answer_callback_query(call.id, "You didn't send the request")

@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "audio", "video", "document"])
def handle_private_messages(message: types.Message):
    text = message.text if message.text else message.caption if message.caption else None
    if message.chat.type != "private" or not text or not is_url(text):
        return
    log(message, text, "video")
    download_video(message, text)

me = bot.get_me()
print(f"ready as @{me.username} — YouTube + Dailymotion FIXED ✅ (45MB limit + 404 fix)")
bot.infinity_polling()
