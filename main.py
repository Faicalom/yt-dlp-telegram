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

# ----------------------------
# Settings
# ----------------------------
os.makedirs(config.output_folder, exist_ok=True)

# Maximum file we allow ourselves to attempt uploading to Telegram.
# Telegram may still refuse some files depending on type/codec/network.
UPLOAD_LIMIT_BYTES = int(getattr(config, "upload_limit_bytes", 90 * 1024 * 1024))

# Hard timeout for Telegram upload/download requests.
REQUEST_TIMEOUT = int(getattr(config, "request_timeout", 1200))

# Use the smaller of config.max_filesize and our upload limit to avoid wasting time.
MAX_DOWNLOAD_BYTES = int(getattr(config, "max_filesize", UPLOAD_LIMIT_BYTES) or UPLOAD_LIMIT_BYTES)
MAX_DOWNLOAD_BYTES = min(MAX_DOWNLOAD_BYTES, UPLOAD_LIMIT_BYTES)

ALLOWED_DOMAINS = getattr(
    config,
    "allowed_domains",
    [
        "youtube.com",
        "youtu.be",
        "m.youtube.com",
        "youtube-nocookie.com",
        "tiktok.com",
        "vt.tiktok.com",
        "instagram.com",
        "instagr.am",
        "twitter.com",
        "x.com",
        "facebook.com",
        "fb.watch",
        "m.facebook.com",
        "web.facebook.com",
        "dailymotion.com",
        "bsky.app",
        "bluesky",
    ],
)

SECRET_KEY = getattr(config, "secret_key", "any-secret-you-like")

# ----------------------------
# Crypto / DB
# ----------------------------
key = hashlib.sha256(SECRET_KEY.encode()).digest()
cipher = Fernet(base64.urlsafe_b64encode(key))

script_dir = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(script_dir, "db.db")
db_conn = sqlite3.connect(db_path, check_same_thread=False)
db_cursor = db_conn.cursor()
db_cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS user_cookies (
        user_id INTEGER PRIMARY KEY,
        cookie_data TEXT NOT NULL
    )
"""
)
db_conn.commit()

ses = requests.Session()
bot = telebot.TeleBot(config.token)
last_edited = {}


# ----------------------------
# Helpers
# ----------------------------
def encrypt_cookie(cookie_data: str) -> str:
    return cipher.encrypt(cookie_data.encode()).decode()


def decrypt_cookie(encrypted_data: str) -> str:
    return cipher.decrypt(encrypted_data.encode()).decode()


def youtube_url_validation(url):
    youtube_regex = (
        r"(https?://)?(www\.|m\.)?"
        r"(youtube|youtu|youtube-nocookie)\.(com|be)/"
        r"(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})"
    )
    youtube_regex_match = re.match(youtube_regex, url)
    if youtube_regex_match:
        return youtube_regex_match
    return youtube_regex_match


def is_allowed_domain(url):
    """Check if URL belongs to allowed domains."""
    if not url or not isinstance(url, str):
        return False

    parsed = urlparse(url.strip().lower())
    host = parsed.netloc.split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    for domain in ALLOWED_DOMAINS:
        domain = domain.lower().strip()
        if host == domain or host.endswith("." + domain):
            return True

    return False


def is_url(text: str) -> bool:
    if not text:
        return False
    text = text.strip().lower()
    return text.startswith(("http://", "https://"))


@bot.message_handler(commands=["start", "help"])
def test(message):
    bot.reply_to(
        message,
        "*Send me a video link* and I'll download it for you, works with *YouTube*, *TikTok*, *Instagram*, *Twitter*, *Facebook* and *Bluesky*.\n\n_Powered by_ [yt-dlp](https://github.com/yt-dlp/yt-dlp/)",
        parse_mode="MARKDOWN",
        disable_web_page_preview=True,
    )


def _validate_url(message, url: str) -> bool:
    """Validate URL domain and YouTube-specific rules."""
    if not is_allowed_domain(url):
        bot.reply_to(
            message,
            "Invalid URL. Only YouTube, TikTok, Instagram, Twitter, Facebook and Bluesky links are supported.",
        )
        return False

    if urlparse(url).netloc in {
        "www.youtube.com",
        "youtube.com",
        "youtu.be",
        "m.youtube.com",
        "youtube-nocookie.com",
    }:
        if not youtube_url_validation(url):
            bot.reply_to(message, "Invalid URL")
            return False

    return True


def _make_progress_hook(message, msg) -> Callable:
    """Return a yt-dlp progress hook that throttles Telegram edits to once per 5s."""

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
                text=(
                    f"Downloading {title}\n\n{perc}%\n\n"
                    f"<i>Want to stay updated? @SatoruStatus</i>"
                ),
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
    downloads = info.get("requested_downloads") or []
    if downloads:
        fp = downloads[0].get("filepath")
        if fp:
            return fp

    fp = info.get("filepath")
    if fp:
        return fp

    return None


def _send_as_document(message, filepath: str) -> None:
    with open(filepath, "rb") as f:
        bot.send_document(
            message.chat.id,
            f,
            reply_to_message_id=message.message_id,
            visible_file_name=os.path.basename(filepath),
            timeout=REQUEST_TIMEOUT,
        )


def _send_media(message, info: Any, audio: bool) -> None:
    """Send the downloaded file back to the user via Telegram."""
    filepath = _get_downloaded_filepath(info)
    if not filepath or not os.path.exists(filepath):
        raise RuntimeError("Downloaded file path not found")

    size = _safe_file_size(filepath)
    if size and size > UPLOAD_LIMIT_BYTES:
        raise RuntimeError(
            f"File too large for Telegram upload ({round(size / 1024 / 1024)}MB)"
        )

    try:
        if audio:
            with open(filepath, "rb") as f:
                try:
                    bot.send_audio(
                        message.chat.id,
                        f,
                        reply_to_message_id=message.message_id,
                        timeout=REQUEST_TIMEOUT,
                    )
                except Exception:
                    f.seek(0)
                    bot.send_document(
                        message.chat.id,
                        f,
                        reply_to_message_id=message.message_id,
                        visible_file_name=os.path.basename(filepath),
                        timeout=REQUEST_TIMEOUT,
                    )
        else:
            # Sending as document is more reliable than send_video for many sources.
            _send_as_document(message, filepath)
    except Exception as e:
        print("send failed:", e)
        raise


def _cleanup(video_title: int) -> None:
    """Remove all files in the output folder that belong to this download."""
    try:
        for file in os.listdir(config.output_folder):
            if file.startswith(str(video_title)):
                try:
                    os.remove(os.path.join(config.output_folder, file))
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass


def check_url(content: str, message) -> dict:
    if not content:
        return {"success": False}

    match = re.search(r"https?://\S+", content)
    url = match.group(0) if match else content

    if not urlparse(url).scheme:
        bot.reply_to(message, "Invalid URL")
        return {"success": False}

    if not _validate_url(message, url):
        return {"success": False}

    return {"success": True, "url": url}


def _build_default_format_selector(audio: bool) -> str:
    if audio:
        return "bestaudio/best"

    limit_mb = max(1, UPLOAD_LIMIT_BYTES // (1024 * 1024))
    return (
        f"best[filesize<{limit_mb}M]/"
        f"best[filesize_approx<{limit_mb}M]/"
        f"worst"
    )


def download_video(message, content, audio=False, format_id=None) -> None:
    check = check_url(content, message)
    if not check["success"]:
        return

    url = check["url"]

    msg = bot.reply_to(
        message,
        "Downloading...\n\n<i>Want to stay updated? @SatoruStatus</i>",
        parse_mode="HTML",
    )
    video_title = round(time.time() * 1000)

    resolved_format = format_id
    if not resolved_format or resolved_format == "mp4":
        resolved_format = _build_default_format_selector(audio)

    ydl_opts: yt_dlp._Params = {
        "format": resolved_format,
        "outtmpl": f"{config.output_folder}/{video_title}.%(ext)s",
        "progress_hooks": [_make_progress_hook(message, msg)],
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
        if audio
        else [],
        "js_runtimes": {"bun": {"path": "bun"}},
        "remote_components": {"ejs:github"},
    }

    if getattr(config, "js_runtime", None):
        ydl_opts["js_runtimes"] = config.js_runtime
        ydl_opts["remote_components"] = {"ejs:github"}

    cookie_file = None
    try:
        user_id = message.from_user.id
        db_cursor.execute(
            "SELECT cookie_data FROM user_cookies WHERE user_id = ?", (user_id,)
        )
        result = db_cursor.fetchone()

        if result:
            decrypted_data = decrypt_cookie(result[0])
            cookie_file = f"{config.output_folder}/cookies_{user_id}.txt"
            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write(decrypted_data)
            ydl_opts["cookiefile"] = cookie_file

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        filepath = _get_downloaded_filepath(info)
        if filepath and os.path.exists(filepath):
            size = _safe_file_size(filepath)
            if size > UPLOAD_LIMIT_BYTES:
                bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=msg.message_id,
                    text=(
                        f"File is too large to upload here ({round(size / 1024 / 1024)}MB).\n"
                        f"Use /custom and pick a smaller format."
                    ),
                )
                return

        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=msg.message_id,
            text="Sending file to Telegram...",
        )

        _send_media(message, info, audio)
        bot.delete_message(message.chat.id, msg.message_id)

    except (DownloadError, ExtractorError) as e:
        err = str(e).lower()

        if "[youtube]" in err and "sign in" in err:
            text = "We're sorry, YouTube is ratelimiting third party downloaders right now, try again later."
        elif "login required" in err or "rate-limit reached" in err:
            text = "Content not available (Rate limit or login required)."
        elif "no video formats" in err or "format" in err:
            text = "No suitable small format was found. Try /custom and choose a lower quality."
        else:
            text = "There was an error downloading the video, please try again later."

        bot.edit_message_text(text, message.chat.id, msg.message_id)

    except Exception as e:
        print("Unexpected error:", e)
        bot.edit_message_text(
            "Couldn't send file. Try a smaller quality or another source.",
            message.chat.id,
            msg.message_id,
        )

    finally:
        if cookie_file and os.path.exists(cookie_file):
            try:
                os.remove(cookie_file)
            except FileNotFoundError:
                pass
        _cleanup(video_title)


def log(message, text: str, media: str):
    if getattr(config, "logs", None):
        if message.chat.type == "private":
            chat_info = "Private chat"
        else:
            chat_info = f"Group: *{message.chat.title}* (`{message.chat.id}`)"

        bot.send_message(
            config.logs,
            f"Download request ({media}) from @{getattr(message.from_user, 'username', None)} ({message.from_user.id})\n\n{chat_info}\n\n{text}",
        )


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
        label = f"{res}.{ext}"
        data[label] = {"callback_data": f"{x['format_id']}"}

    markup = quick_markup(data, row_width=2)

    bot.delete_message(msg.chat.id, msg.message_id)
    bot.reply_to(message, "Choose a format", reply_markup=markup)


def filter_cookies_by_domain(cookie_data: str) -> str:
    lines = cookie_data.split("\n")
    filtered_lines = []

    for line in lines:
        if line.startswith("#") or not line.strip():
            filtered_lines.append(line)
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue

        domain = parts[0].lstrip(".")

        is_allowed = False
        for allowed_domain in ALLOWED_DOMAINS:
            allowed_domain = allowed_domain.lower().strip()
            if domain == allowed_domain or domain.endswith("." + allowed_domain):
                is_allowed = True
                break

        if is_allowed:
            filtered_lines.append(line)

    return "\n".join(filtered_lines)


@bot.message_handler(commands=["id"])
def get_chat_id(message):
    bot.reply_to(message, message.chat.id)


def is_cookie_command(message):
    text = message.text or message.caption or ""
    return text.startswith("/cookie") or text.startswith("/cookies")


@bot.message_handler(func=is_cookie_command, content_types=["document", "text"])
def handle_cookie(message):
    user_id = message.from_user.id

    if not message.document:
        db_cursor.execute(
            "SELECT cookie_data FROM user_cookies WHERE user_id = ?", (user_id,)
        )
        result = db_cursor.fetchone()

        if result:
            cookie_file = f"{config.output_folder}/cookies_{user_id}_temp.txt"
            try:
                decrypted_data = decrypt_cookie(result[0])
                with open(cookie_file, "w", encoding="utf-8") as f:
                    f.write(decrypted_data)

                markup = types.InlineKeyboardMarkup()
                delete_btn = types.InlineKeyboardButton(
                    "🗑 Delete", callback_data="delete_cookies"
                )
                markup.add(delete_btn)

                with open(cookie_file, "rb") as f:
                    bot.send_document(
                        message.chat.id,
                        f,
                        reply_to_message_id=message.message_id,
                        visible_file_name="cookies.txt",
                        reply_markup=markup,
                        timeout=REQUEST_TIMEOUT,
                    )
            finally:
                if os.path.exists(cookie_file):
                    os.remove(cookie_file)
        else:
            bot.reply_to(
                message,
                "No cookies stored. Send a file with this command to store cookies.",
            )
        return

    file_info = bot.get_file(message.document.file_id)
    if not file_info.file_path:
        bot.reply_to(message, "Failed to get file information.")
        return

    downloaded_file = bot.download_file(file_info.file_path)
    cookie_data = downloaded_file.decode("utf-8")

    filtered_cookie_data = filter_cookies_by_domain(cookie_data)
    encrypted_data = encrypt_cookie(filtered_cookie_data)

    db_cursor.execute(
        "INSERT OR REPLACE INTO user_cookies (user_id, cookie_data) VALUES (?, ?)",
        (user_id, encrypted_data),
    )
    db_conn.commit()
    bot.reply_to(message, "Cookies saved successfully!")


@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if call.data == "delete_cookies":
        user_id = call.from_user.id
        db_cursor.execute("DELETE FROM user_cookies WHERE user_id = ?", (user_id,))
        db_conn.commit()

        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption="Cookies deleted successfully!",
            reply_markup=None,
        )
        bot.answer_callback_query(call.id, "Cookies deleted!")
        return

    if call.message.reply_to_message:
        if call.from_user.id == call.message.reply_to_message.from_user.id:
            url = get_text(call.message.reply_to_message)
            if not url:
                bot.answer_callback_query(call.id, "No URL found")
                return

            bot.delete_message(call.message.chat.id, call.message.message_id)
            download_video(
                call.message.reply_to_message,
                url,
                format_id=f"{call.data}+bestaudio",
            )
        else:
            bot.answer_callback_query(call.id, "You didn't send the request")


@bot.message_handler(
    func=lambda m: True,
    content_types=["text", "photo", "audio", "video", "document"],
)
def handle_private_messages(message: types.Message):
    text = message.text if message.text else message.caption if message.caption else None

    if message.chat.type != "private" or not text:
        return

    if not is_url(text):
        bot.reply_to(message, "أرسل رابط صحيح يبدأ بـ http أو https")
        return

    log(message, text, "video")
    download_video(message, text)


me = bot.get_me()
print(f"ready as @{me.username}")
bot.infinity_polling()
