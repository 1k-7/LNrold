import os
import sys
import json
import logging
import asyncio
import shutil
import queue
import time
import urllib3
import gc
import uuid
import zipfile
import datetime
import multiprocessing
import requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ProcessPoolExecutor

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from pyrogram import Client as UserBotClient

from lncrawl.core.app import App
from lncrawl.core.sources import load_sources
from lncrawl.core.arguments import get_args

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("lncrawl").setLevel(logging.WARNING)

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_TOKEN")

# [SPEED OPTIMIZATION]
THREADS_PER_NOVEL = 60
MAX_CONCURRENT_NOVELS = 10

# Group Configs (Must be -100xxxx format)
TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID") 
ERROR_GROUP_ID = os.getenv("ERROR_GROUP_ID")   

# --- MANUAL OVERRIDES (Failsafe) ---
FORCE_TARGET_TOPIC_ID = os.getenv("FORCE_TARGET_TOPIC_ID")
FORCE_ERROR_TOPIC_ID = os.getenv("FORCE_ERROR_TOPIC_ID")

# Userbot Config
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
USERBOT_THRESHOLD = 40.0 

DATA_DIR = os.getenv("DATA_DIR", "data")
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")

# Ensure Data Directory Exists
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

pending_uploads = {}

# --- WORKER INITIALIZER ---
def worker_initializer():
    load_sources()
    args = get_args()
    args.suppress = True
    args.ignore_images = False 

# --- WORKER FUNCTION ---
def scrape_logic_worker(url, progress_queue):
    app = App()
    try:
        if progress_queue: progress_queue.put("🔍 Fetching info...")
        
        app.user_input = url
        app.prepare_search()
        app.get_novel_info()
        
        if app.crawler: 
            # 1. Initialize High-Thread Executor
            app.crawler.init_executor(THREADS_PER_NOVEL)

            # 2. Inject Aggressive Connection Pool
            if hasattr(app.crawler, 'scraper') and hasattr(app.crawler.scraper, 'mount'):
                adapter = HTTPAdapter(
                    pool_connections=THREADS_PER_NOVEL, 
                    pool_maxsize=THREADS_PER_NOVEL,
                    max_retries=3,
                    pool_block=False
                )
                app.crawler.scraper.mount("https://", adapter)
                app.crawler.scraper.mount("http://", adapter)

        # [FIXED] Cover Image - Removed hardcoded headers that caused 403
        if app.crawler.novel_cover:
            try:
                # We use the crawler's scraper which HAS the valid cookies/headers
                response = app.crawler.scraper.get(app.crawler.novel_cover, timeout=15)
                if response.status_code == 200:
                    cover_path = os.path.abspath(os.path.join(app.output_path, 'cover.jpg'))
                    with open(cover_path, 'wb') as f: f.write(response.content)
                    app.book_cover = cover_path
            except Exception as e: 
                pass

        app.chapters = app.crawler.chapters[:]
        if not app.chapters:
            raise Exception("No chapters extracted")

        app.pack_by_volume = False
        app.output_formats = {'epub': True}
        
        total = len(app.chapters)
        if progress_queue: progress_queue.put(f"⬇️ Downloading {total} chapters...")
        
        count = 0
        for i, _ in enumerate(app.start_download()):
            count += 1
            if app.novel_status == "HALTED":
                raise Exception(f"HALTED: {app.novel_status}")

            if count % 25 == 0 and progress_queue: 
                progress_queue.put(f"🚀 {int(app.progress)}% ({i}/{total})")
        
        if app.novel_status != "COMPLETED":
            raise Exception(f"Download completed with FAILED status.")
        
        if progress_queue: progress_queue.put("📦 Binding...")
        
        try:
            for fmt, f in app.bind_books(): return f
        except IndexError:
             raise Exception("IndexError during binding")
        return None

    except Exception as e:
        raise e
    finally: 
        app.destroy()
        gc.collect()

class NovelBot:
    def __init__(self):
        self.executor = ProcessPoolExecutor(
            max_workers=MAX_CONCURRENT_NOVELS,
            initializer=worker_initializer
        )
        self.manager = multiprocessing.Manager()
        self.userbot = None
        self.bot_username = None 
        self.processed = set()
        self.errors = {}
        self.nullcon = set()
        self.genfail = set()
        self.nwerror = set()
        self.target_topic_id = int(FORCE_TARGET_TOPIC_ID) if FORCE_TARGET_TOPIC_ID else None
        self.error_topic_id = int(FORCE_ERROR_TOPIC_ID) if FORCE_ERROR_TOPIC_ID else None
        self.backup_topic_id = None
        self.files = {}

    def get_file_path(self, name):
        return os.path.join(DATA_DIR, f"{name}_{self.bot_username}.json")

    def load_data(self):
        if os.path.exists(self.files['processed']):
            try:
                with open(self.files['processed'], 'r') as f: self.processed = set(json.load(f))
            except Exception as e: logger.error(f"⚠️ Load Processed Failed: {e}")

        if os.path.exists(self.files['errors']):
            try:
                with open(self.files['errors'], 'r') as f: self.errors = json.load(f)
            except Exception as e: logger.error(f"⚠️ Load Errors Failed: {e}")

        if os.path.exists(self.files['nullcon']):
            try:
                with open(self.files['nullcon'], 'r') as f: self.nullcon = set(json.load(f))
            except Exception as e: logger.error(f"⚠️ Load Nullcon Failed: {e}")

        if os.path.exists(self.files['genfail']):
            try:
                with open(self.files['genfail'], 'r') as f: self.genfail = set(json.load(f))
            except Exception as e: logger.error(f"⚠️ Load Genfail Failed: {e}")

        if not self.target_topic_id or not self.error_topic_id:
            if os.path.exists(self.files['topics']):
                try:
                    with open(self.files['topics'], 'r') as f:
                        data = json.load(f)
                        if not self.target_topic_id: self.target_topic_id = data.get("target_topic_id")
                        if not self.error_topic_id: self.error_topic_id = data.get("error_topic_id")
                        self.backup_topic_id = data.get("backup_topic_id")
                except Exception as e: 
                    logger.error(f"❌ CRITICAL: Found topics file but could not read it: {e}")

    def save_topics(self):
        try:
            with open(self.files['topics'], 'w') as f:
                json.dump({
                    "target_topic_id": self.target_topic_id,
                    "error_topic_id": self.error_topic_id,
                    "backup_topic_id": self.backup_topic_id
                }, f, indent=2)
        except Exception as e:
            logger.error(f"❌ Could not save topics: {e}")

    def save_success(self, url):
        self.processed.add(url)
        if url in self.errors: del self.errors[url]
        if url in self.nullcon: self.nullcon.remove(url)
        if url in self.genfail: self.genfail.remove(url)
        
        try:
            with open(self.files['processed'], 'w') as f: 
                json.dump(list(self.processed), f, indent=2)
        except Exception as e: logger.error(f"⚠️ Save Processed Failed: {e}")
        
        self.save_nullcon()
        self.save_genfail()

    def save_errors(self):
        try:
            with open(self.files['errors'], 'w') as f: json.dump(self.errors, f, indent=2)
        except Exception as e: logger.error(f"⚠️ Save Errors Failed: {e}")

    def save_nullcon(self):
        try:
            with open(self.files['nullcon'], 'w') as f: json.dump(list(self.nullcon), f, indent=2)
        except Exception as e: logger.error(f"⚠️ Save Nullcon Failed: {e}")

    def save_genfail(self):
        try:
            with open(self.files['genfail'], 'w') as f: json.dump(list(self.genfail), f, indent=2)
        except Exception as e: logger.error(f"⚠️ Save Genfail Failed: {e}")

    def save_error(self, url, error_msg):
        self.errors[url] = str(error_msg)
        self.save_errors()
        
    async def post_init(self, application: Application):
        me = await application.bot.get_me()
        self.bot_username = me.username
        logger.info(f"🤖 Identity Verified: @{self.bot_username}")

        self.files = {
            'processed': self.get_file_path("processed"),
            'errors': self.get_file_path("errors"),
            'queue': self.get_file_path("queue"),
            'topics': self.get_file_path("topics"),
            'nullcon': self.get_file_path("nullcon"),
            'genfail': self.get_file_path("genfail"),
            'nwerror': self.get_file_path("nwerror")
        }

        self.load_data()

        if TARGET_GROUP_ID and ERROR_GROUP_ID:
            try:
                if not self.target_topic_id:
                    topic = await application.bot.create_forum_topic(chat_id=TARGET_GROUP_ID, name=f"📚 {self.bot_username} Novels")
                    self.target_topic_id = topic.message_thread_id
                    self.save_topics()
                
                if not self.error_topic_id:
                    topic = await application.bot.create_forum_topic(chat_id=ERROR_GROUP_ID, name=f"🛠 {self.bot_username} Logs")
                    self.error_topic_id = topic.message_thread_id
                    self.save_topics()

                if not self.backup_topic_id:
                    topic = await application.bot.create_forum_topic(chat_id=ERROR_GROUP_ID, name=f"🗄️ {self.bot_username} Backup")
                    self.backup_topic_id = topic.message_thread_id
                    self.save_topics()
            except Exception as e:
                logger.error(f"❌ Failed to configure topics: {e}")

        if SESSION_STRING and API_ID:
            try:
                self.userbot = UserBotClient(
                    "uploader",
                    api_id=int(API_ID),
                    api_hash=API_HASH,
                    session_string=SESSION_STRING,
                    in_memory=True
                )
                await self.userbot.start()
                logger.info("✅ Userbot Connected!")
            except Exception as e:
                logger.error(f"❌ Userbot Failed: {e}")

        asyncio.create_task(self.backup_loop(application.bot))
        
        if os.path.exists(self.files['queue']):
            try:
                with open(self.files['queue'], 'r') as f: data = json.load(f)
                urls = data.get("urls", [])
                to_process = [u for u in urls if u not in self.processed and u not in self.nullcon and u not in self.genfail]
                
                if to_process:
                    await self.send_log(application.bot, f"🔄 **Restarted**\nResuming {len(to_process)} novels...")
                    asyncio.create_task(self.process_queue(to_process, application.bot))
                elif urls:
                     await self.send_log(application.bot, "ℹ️ Queue exists but all novels processed/skipped. Clearing.")
                     os.remove(self.files['queue'])
            except Exception as e:
                logger.error(f"❌ Error processing queue file: {e}")

    async def send_log(self, bot, text, edit_msg=None):
        if ERROR_GROUP_ID and self.error_topic_id:
            try:
                if edit_msg:
                    return await edit_msg.edit_text(text)
                return await bot.send_message(
                    chat_id=ERROR_GROUP_ID, 
                    message_thread_id=self.error_topic_id, 
                    text=text
                )
            except: pass 
        return None

    async def backup_loop(self, bot):
        await asyncio.sleep(60)
        while True:
            await self.perform_backup(bot)
            await asyncio.sleep(86400)

    async def perform_backup(self, bot):
        if not ERROR_GROUP_ID or not self.backup_topic_id: return
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
            zip_name = f"backup_{self.bot_username}_{timestamp}.zip"
            zip_path = os.path.join(DATA_DIR, zip_name)
            files_to_backup = [f for f in os.listdir(DATA_DIR) if f.endswith('.json') and self.bot_username in f]
            if not files_to_backup: return
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for file_name in files_to_backup:
                    zf.write(os.path.join(DATA_DIR, file_name), file_name)
            with open(zip_path, 'rb') as f:
                await bot.send_document(chat_id=ERROR_GROUP_ID, message_thread_id=self.backup_topic_id, document=f, caption=f"🗄️ Backup {timestamp}")
            if os.path.exists(zip_path): os.remove(zip_path)
            logger.info("✅ Backup uploaded successfully")
        except Exception as e:
            logger.error(f"Backup Failed: {e}")
            await self.send_log(bot, f"⚠️ Backup Failed: {e}")

    def start(self):
        print("🚀 Bot Starting...")
        sys.stdout.flush()
        if not TOKEN:
            print("❌ FATAL ERROR: TELEGRAM_TOKEN missing!")
            sys.exit(1)

        app = Application.builder().token(TOKEN).post_init(self.post_init).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("backup", self.cmd_force_backup))
        app.add_handler(CommandHandler("rmp", self.cmd_rmp)) # NEW COMMAND
        app.add_handler(MessageHandler(filters.Document.MimeType("application/json"), self.handle_json_file))
        app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, self.handle_bot_dm))
        
        load_sources()
        print("🚀 Bot Polling...")
        app.run_polling()

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"⚡ **FanMTL Bot (Turbo)** ⚡\nProcessed: {len(self.processed)}\nUser: {self.bot_username}")

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.processed = set()
        self.nullcon = set()
        self.genfail = set()
        self.nwerror = set()
        for f in [self.files['processed'], self.files['nullcon'], self.files['genfail'], self.files['nwerror'], self.files['queue']]:
            if os.path.exists(f): os.remove(f)
        await update.message.reply_text("🗑️ History Reset.")

    # [NEW] Remove from Processed Command
    async def cmd_rmp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        urls = context.args
        if not urls:
            await update.message.reply_text("⚠️ Usage: /rmp <url1> <url2> ...")
            return
        
        removed_count = 0
        for url in urls:
            if url in self.processed:
                self.processed.remove(url)
                removed_count += 1
        
        if removed_count > 0:
            try:
                with open(self.files['processed'], 'w') as f: 
                    json.dump(list(self.processed), f, indent=2)
                await update.message.reply_text(f"✅ Removed {removed_count} novels from history.")
            except Exception as e: 
                logger.error(f"⚠️ Save Processed Failed: {e}")
                await update.message.reply_text(f"❌ Error saving file: {e}")
        else:
             await update.message.reply_text("⚠️ URLs not found in history.")

    async def cmd_force_backup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏳ Starting manual backup...")
        await self.perform_backup(context.bot)
        await update.message.reply_text("✅ Backup sent to logs group.")

    async def handle_bot_dm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.message.caption
        if uid and uid in pending_uploads:
            pending_uploads[uid].set_result(update.message.document.file_id)
            del pending_uploads[uid]

    async def handle_json_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        file = await update.message.document.get_file()
        temp_path = os.path.join(DATA_DIR, "temp.json")
        await file.download_to_drive(temp_path)
        try:
            with open(temp_path, 'r', encoding='utf-8') as f: urls = json.load(f)
            with open(self.files['queue'], 'w') as f:
                json.dump({"chat_id": update.effective_chat.id, "urls": urls}, f, indent=2)
            await update.message.reply_text(f"✅ Received {len(urls)} novels. Check Log Group for progress.")
            await self.process_queue(urls, context.bot)
        except Exception as e:
            logger.error(f"File Error: {e}")
            await update.message.reply_text("❌ Invalid JSON")
        finally:
            if os.path.exists(temp_path): os.remove(temp_path)

    async def process_queue(self, urls, bot):
        gc.collect()
        to_process = []
        skipped = 0
        
        for u in urls:
            if u in self.processed or u in self.nullcon or u in self.genfail:
                skipped += 1
                continue
            to_process.append(u)

        if not to_process:
            if os.path.exists(self.files['queue']): os.remove(self.files['queue'])
            await self.send_log(bot, f"✅ **Queue Complete**\n(Skipped: {skipped} already processed/failed)")
            return

        await self.send_log(bot, f"📥 **Starting Batch**\nQueue: {len(to_process)}\n(Skipped: {skipped})")
        
        for url in to_process:
            if url in self.processed: continue
            await self.process_novel(url, bot)
            gc.collect()
        
        await self.send_log(bot, "✅ **All Tasks Finished**")
        if os.path.exists(self.files['queue']): os.remove(self.files['queue'])

    async def process_novel(self, url: str, bot):
        status_msg = await self.send_log(bot, f"⏳ **Processing:** {url}")
        
        progress_queue = self.manager.Queue()
        loop = asyncio.get_running_loop()
        start_time = time.time()
        
        future = loop.run_in_executor(self.executor, scrape_logic_worker, url, progress_queue)
        
        last_text = ""
        last_update = 0
        
        while not future.done():
            try:
                try:
                    text = progress_queue.get_nowait()
                    if text != last_text and (time.time() - last_update) > 5:
                        try: 
                            await self.send_log(bot, text, edit_msg=status_msg)
                            last_text = text; last_update = time.time()
                        except: pass
                except queue.Empty: pass
                await asyncio.sleep(0.5)
            except: await asyncio.sleep(0.5)

        try:
            epub_path = await future
            duration = int(time.time() - start_time)
            
            if epub_path and os.path.exists(epub_path):
                file_size_mb = os.path.getsize(epub_path) / (1024 * 1024)
                caption = f"📕 {os.path.basename(epub_path)}\n📦 {file_size_mb:.1f}MB | ⏱️ {duration}s"
                try: await status_msg.delete()
                except: pass
                
                dest_chat_id = TARGET_GROUP_ID if TARGET_GROUP_ID else ERROR_GROUP_ID
                dest_topic_id = self.target_topic_id

                if file_size_mb > USERBOT_THRESHOLD and self.userbot:
                    prog_msg = await self.send_log(bot, f"🚀 Uploading {file_size_mb:.1f}MB via Userbot...")
                    uid = uuid.uuid4().hex
                    upload_future = loop.create_future()
                    pending_uploads[uid] = upload_future
                    try:
                        try: receiver = await self.userbot.get_users(self.bot_username)
                        except: receiver = await self.userbot.get_users(f"@{self.bot_username}")
                        await self.userbot.send_document(chat_id=receiver.id, document=epub_path, caption=uid)
                        file_id = await asyncio.wait_for(upload_future, timeout=600)
                        await bot.send_document(chat_id=dest_chat_id, message_thread_id=dest_topic_id, document=file_id, caption=caption)
                        await prog_msg.delete()
                        self.save_success(url)
                    except Exception as e:
                        await self.send_log(bot, f"❌ Userbot Upload Failed: {e}", edit_msg=prog_msg)
                        self.save_error(url, f"Userbot Upload Failed: {e}")
                else:
                    if file_size_mb >= 50 and file_size_mb > USERBOT_THRESHOLD:
                        await self.send_log(bot, f"❌ File > 50MB & No Userbot.")
                        self.save_error(url, "File > 50MB & No Userbot")
                    else:
                        with open(epub_path, 'rb') as f:
                            await bot.send_document(chat_id=dest_chat_id, message_thread_id=dest_topic_id, document=f, caption=caption)
                        self.save_success(url)
                os.remove(epub_path)
            else:
                self.genfail.add(url)
                self.save_genfail()
                await self.send_log(bot, f"❌ Gen Failed: {url}", edit_msg=status_msg)

        except Exception as e:
            err_msg = str(e)
            if "list index out of range" in err_msg or "IndexError" in err_msg or "No chapters extracted" in err_msg:
                self.nullcon.add(url)
                self.save_nullcon()
                await self.send_log(bot, f"⚠️ **Null Content:** {url}", edit_msg=status_msg)
            else:
                await self.send_log(bot, f"❌ **Error:** {e}\n{url}", edit_msg=status_msg)

if __name__ == "__main__":
    multiprocessing.freeze_support()
    bot = NovelBot()
    bot.start()
