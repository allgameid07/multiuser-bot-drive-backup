import os
import sys
import json
import asyncio
from pathlib import Path
from datetime import datetime
from telethon import TelegramClient, functions, errors
from telethon.tl.types import InputPhoneContact
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler, ContextTypes, filters
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# ===== CONFIG =====
API_ID = int(os.getenv("API_ID", "22914296"))
API_HASH = os.getenv("API_HASH", "ce04b81d45eba374618c0bf05b745aad")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

FOLDER_ID = "12GrU-NhyUvXykX2Tejw_jr92oNwjDatZ"

BASE_DIR = Path("/mnt/data/bot_data") if Path("/mnt/data").exists() else Path(__file__).resolve().parent
BASE_DIR.mkdir(parents=True, exist_ok=True)
USERS_DIR = BASE_DIR / "users"
USERS_DIR.mkdir(exist_ok=True)

BATCH_SIZE = 5
SLEEP_TIME = 3
DAILY_LIMIT = 150

clients = {}
recent_results = {}
full_results = {}

PHONE, OTP, PASS = range(3)

# ===== GOOGLE DRIVE AUTH =====
def init_drive():
    gauth = GoogleAuth()
    cred_path = BASE_DIR / "credentials.json"
    if cred_path.exists():
        gauth.LoadCredentialsFile(str(cred_path))
    if not gauth.credentials:
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()
    gauth.SaveCredentialsFile(str(cred_path))
    return GoogleDrive(gauth)

def download_all_user_data(drive):
    file_list = drive.ListFile({"q": f"'{FOLDER_ID}' in parents and trashed=false"}).GetList()
    for file in file_list:
        file.GetContentFile(str(USERS_DIR / file["title"]))
    print("[DRIVE] Data downloaded")

def upload_all_user_data(drive):
    for file_name in os.listdir(USERS_DIR):
        fpath = USERS_DIR / file_name
        gfile = drive.CreateFile({"title": file_name, "parents": [{"id": FOLDER_ID}]})
        gfile.SetContentFile(str(fpath))
        gfile.Upload()
    print("[DRIVE] Data uploaded")

# ===== USER DATA =====
def load_user_data(user_id):
    file_path = USERS_DIR / f"user_{user_id}.json"
    if file_path.exists():
        return json.load(open(file_path, "r", encoding="utf-8"))
    return {"checked_today": 0, "date": str(datetime.now().date())}

def save_user_data(user_id, data):
    json.dump(data, open(USERS_DIR / f"user_{user_id}.json", "w", encoding="utf-8"))

def reset_if_new_day(user_id, data):
    today = str(datetime.now().date())
    if data["date"] != today:
        data["date"] = today
        data["checked_today"] = 0
    return data

# ===== AUTO LOAD SESSIONS =====
async def auto_load_sessions():
    for sess_file in USERS_DIR.glob("session_*.session"):
        try:
            user_id = int(sess_file.stem.replace("session_", ""))
            client = TelegramClient(str(sess_file), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                clients[user_id] = client
                if user_id not in full_results:
                    full_results[user_id] = {"registered": set(), "nonregistered": set()}
                print(f"[AUTOLOAD] User {user_id} loaded")
        except Exception as e:
            print(f"[AUTOLOAD ERROR] {e}")

# ===== LOGIN FLOW =====
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 ফোন নম্বর দিন (+880...)")
    return PHONE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["phone"] = update.message.text.strip()
    user_id = update.effective_user.id
    session_path = USERS_DIR / f"session_{user_id}"
    context.user_data["client"] = TelegramClient(str(session_path), API_ID, API_HASH)
    await context.user_data["client"].connect()
    try:
        await context.user_data["client"].send_code_request(context.user_data["phone"])
    except errors.PhoneNumberBannedError:
        await update.message.reply_text("🚫 এই নম্বর Telegram দ্বারা ব্যান করা হয়েছে!")
        return ConversationHandler.END
    await update.message.reply_text("🔐 OTP দিন")
    return OTP

async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data["client"]
    otp = update.message.text.strip()
    try:
        await client.sign_in(context.user_data["phone"], otp)
    except errors.SessionPasswordNeededError:
        await update.message.reply_text("🔑 2FA পাসওয়ার্ড দিন")
        return PASS
    clients[update.effective_user.id] = client
    if update.effective_user.id not in full_results:
        full_results[update.effective_user.id] = {"registered": set(), "nonregistered": set()}
    await update.message.reply_text("✅ লগইন সফল")
    drive = init_drive()
    upload_all_user_data(drive)
    return ConversationHandler.END

async def login_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data["client"]
    pwd = update.message.text.strip()
    await client.sign_in(password=pwd)
    clients[update.effective_user.id] = client
    if update.effective_user.id not in full_results:
        full_results[update.effective_user.id] = {"registered": set(), "nonregistered": set()}
    await update.message.reply_text("✅ লগইন সফল")
    drive = init_drive()
    upload_all_user_data(drive)
    return ConversationHandler.END

async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ লগইন বাতিল")
    return ConversationHandler.END

# ===== CHECK =====
async def telethon_check(user_id, numbers):
    if user_id not in clients:
        return ["❌ আগে /login করুন"]

    client = clients[user_id]
    registered_nums = []
    nonregistered_nums = []

    for start in range(0, len(numbers), BATCH_SIZE):
        batch = numbers[start:start+BATCH_SIZE]
        contacts = [InputPhoneContact(client_id=i, phone=n, first_name="Test", last_name="User") for i, n in enumerate(batch)]
        res = await client(functions.contacts.ImportContactsRequest(contacts=contacts))
        found = {"+" + u.phone for u in res.users if getattr(u, "phone", None)}
        for n in batch:
            if n in found:
                registered_nums.append(n)
            else:
                nonregistered_nums.append(n)
        await asyncio.sleep(SLEEP_TIME)

    recent_results[user_id] = {"registered": registered_nums, "nonregistered": nonregistered_nums}
    if user_id not in full_results:
        full_results[user_id] = {"registered": set(), "nonregistered": set()}
    full_results[user_id]["registered"].update(registered_nums)
    full_results[user_id]["nonregistered"].update(nonregistered_nums)

    data = reset_if_new_day(user_id, load_user_data(user_id))
    data["checked_today"] += len(numbers)
    save_user_data(user_id, data)

    drive = init_drive()
    upload_all_user_data(drive)

    msg = "✅ Registered Numbers:\n" + ("\n".join(registered_nums) if registered_nums else "❌ None")
    msg += "\n\n🚫 Non-Registered Numbers:\n" + ("\n".join(nonregistered_nums) if nonregistered_nums else "❌ None")
    return [msg]

# ===== COMMANDS =====
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = reset_if_new_day(user_id, load_user_data(user_id))
    remaining = DAILY_LIMIT - data["checked_today"]
    if remaining <= 0:
        await update.message.reply_text("❌ আজকের লিমিট শেষ।")
        return
    await update.message.reply_text(f"📤 আজকের বাকি ক্রেডিট: {remaining}\nপ্রতি লাইনে একটি নাম্বার দিন।")
    context.user_data["awaiting_numbers"] = True

async def handle_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.user_data.get("awaiting_numbers"):
        return
    context.user_data["awaiting_numbers"] = False
    numbers = [n.strip() for n in update.message.text.splitlines() if n.strip()]
    results = await telethon_check(user_id, numbers)
    for line in results:
        await update.message.reply_text(line)

async def recentresult_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in recent_results:
        await update.message.reply_text("❌ কোনো ডাটা নেই")
        return
    reg = recent_results[user_id]["registered"]
    nonreg = recent_results[user_id]["nonregistered"]
    msg = "✅ Registered Numbers:\n" + ("\n".join(reg) if reg else "❌ None")
    msg += "\n\n🚫 Non-Registered Numbers:\n" + ("\n".join(nonreg) if nonreg else "❌ None")
    await update.message.reply_text(msg)

async def fullresult_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in full_results:
        await update.message.reply_text("❌ কোনো ডাটা নেই")
        return
    reg = sorted(full_results[user_id]["registered"])
    nonreg = sorted(full_results[user_id]["nonregistered"])
    msg = "✅ All Registered Numbers:\n" + ("\n".join(reg) if reg else "❌ None")
    msg += "\n\n🚫 All Non-Registered Numbers:\n" + ("\n".join(nonreg) if nonreg else "❌ None")
    await update.message.reply_text(msg)

# ===== MAIN =====
def main():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    drive = init_drive()
    download_all_user_data(drive)
    asyncio.get_event_loop().run_until_complete(auto_load_sessions())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp)],
            PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_pass)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)]
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("recentresult", recentresult_command))
    app.add_handler(CommandHandler("fullresult", fullresult_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_numbers))

    app.run_polling()

if __name__ == "__main__":
    main()