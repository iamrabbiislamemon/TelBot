import asyncio
import logging
import os
import random
import re
import sqlite3
import string
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_USERNAME = "FastGmailMarket"
BOT_USERNAME = "FastMailMarketBot"
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])

# Financial Settings
GMAIL_PRICE_NEW = 20.0
GMAIL_PRICE_OLD = 25.0
REFERRAL_REWARD = 2.0  # Reward in Tk per active referred user

# Conversation States
WAIT_OLD_GMAIL_EMAIL, WAIT_OLD_GMAIL_PASS = range(2)
WAIT_WITHDRAW_INFO = 2

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Data Pools for Option 1: Dynamic Credential Generator
FIRST_NAMES = ["Shahriar", "Tanvir", "Arafat", "Sabbir", "Naim", "Rayhan"]
LAST_NAMES = ["Hossain", "Rahman", "Ahmed", "Khan", "Chowdhury"]
KEYWORDS = ["dev", "tech", "box", "net", "pro", "digital", "hub"]


# ---------------------------------------------------------
# DATABASE MANAGEMENT (SQLite)
# ---------------------------------------------------------
def init_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()

    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            referred_by INTEGER DEFAULT NULL,
            ref_count INTEGER DEFAULT 0
        )
    """)

    # Submissions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            sub_id TEXT PRIMARY KEY,
            user_id INTEGER,
            email TEXT,
            password TEXT,
            reward REAL,
            status TEXT
        )
    """)

    # Withdrawals table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            wdr_id TEXT PRIMARY KEY,
            user_id INTEGER,
            method TEXT,
            account TEXT,
            amount REAL,
            status TEXT
        )
    """)

    conn.commit()
    conn.close()


def get_db_connection():
    conn = sqlite3.connect("bot_data.db")
    # Avoid immediate "database is locked" errors when multiple handler
    # threads hit the file at once; retry internally for up to 5s instead.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_or_create_user(user_id: int, referrer_id: int = None) -> tuple:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT user_id, balance, referred_by, ref_count FROM users WHERE user_id = ?",
            (user_id,),
        )
        user = cursor.fetchone()

        if not user:
            # Validate referrer exists and isn't self
            if referrer_id == user_id:
                referrer_id = None

            cursor.execute(
                "INSERT INTO users (user_id, balance, referred_by, ref_count) VALUES (?, 0.0, ?, 0)",
                (user_id, referrer_id),
            )
            conn.commit()
            cursor.execute(
                "SELECT user_id, balance, referred_by, ref_count FROM users WHERE user_id = ?",
                (user_id,),
            )
            user = cursor.fetchone()

        return user
    finally:
        conn.close()


def get_user_balance(user_id: int) -> float:
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )
        res = cursor.fetchone()
        return res[0] if res else 0.0
    finally:
        conn.close()


# ---------------------------------------------------------
# DB HELPERS THAT NEED TO RUN ATOMICALLY (called via asyncio.to_thread
# from handlers so a slow DB call never blocks the event loop, and each
# state transition is gated by a single UPDATE ... WHERE status='PENDING'
# so a double-tap / concurrent callback can't process the same row twice).
# ---------------------------------------------------------
def db_insert_submission(sub_id: str, user_id: int, email: str, password: str, reward: float):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, 'PENDING')",
            (sub_id, user_id, email, password, reward),
        )
        conn.commit()
    finally:
        conn.close()


def db_get_ref_count(user_id: int) -> int:
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT ref_count FROM users WHERE user_id = ?", (user_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def db_create_withdrawal(user_id: int, method: str, account_no: str, amount: float):
    """Atomically validates balance, deducts it, and inserts the withdrawal row.
    Returns (wdr_id, remaining_balance) on success, or (None, current_balance) if
    the amount is invalid — closing the race where two fast requests could both
    read the same balance and jointly overdraw it."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )
        row = cursor.fetchone()
        balance = row[0] if row else 0.0

        if amount <= 0 or amount > balance:
            return None, balance

        wdr_id = f"WDR_{user_id}_{random.randint(10000, 99999)}"
        conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.execute(
            "INSERT INTO withdrawals VALUES (?, ?, ?, ?, ?, 'PENDING')",
            (wdr_id, user_id, method, account_no, amount),
        )
        conn.commit()
        return wdr_id, balance - amount
    finally:
        conn.close()


def db_approve_submission(sub_id: str):
    """Atomically claims a PENDING submission, credits the seller, and pays out
    any referral bonus. Returns (sub_user_id, reward, referrer_id) on success,
    or None if the submission was already approved/rejected (or doesn't exist)."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE submissions SET status = 'APPROVED' WHERE sub_id = ? AND status = 'PENDING'",
            (sub_id,),
        )
        if cursor.rowcount == 0:
            return None

        cursor = conn.execute(
            "SELECT user_id, reward FROM submissions WHERE sub_id = ?", (sub_id,)
        )
        sub_user_id, reward = cursor.fetchone()

        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (reward, sub_user_id),
        )

        cursor = conn.execute(
            "SELECT referred_by FROM users WHERE user_id = ?", (sub_user_id,)
        )
        ref_row = cursor.fetchone()
        referrer_id = ref_row[0] if ref_row else None
        if referrer_id:
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (REFERRAL_REWARD, referrer_id),
            )
            conn.execute(
                "UPDATE users SET ref_count = ref_count + 1, referred_by = NULL WHERE user_id = ?",
                (sub_user_id,),
            )

        conn.commit()
        return sub_user_id, reward, referrer_id
    finally:
        conn.close()


def db_reject_submission(sub_id: str):
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE submissions SET status = 'REJECTED' WHERE sub_id = ? AND status = 'PENDING'",
            (sub_id,),
        )
        if cursor.rowcount == 0:
            return None

        cursor = conn.execute(
            "SELECT user_id, email FROM submissions WHERE sub_id = ?", (sub_id,)
        )
        row = cursor.fetchone()
        conn.commit()
        return row
    finally:
        conn.close()


def db_approve_withdrawal(wdr_id: str):
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE withdrawals SET status = 'APPROVED' WHERE wdr_id = ? AND status = 'PENDING'",
            (wdr_id,),
        )
        if cursor.rowcount == 0:
            return None

        cursor = conn.execute(
            "SELECT user_id, amount, method, account FROM withdrawals WHERE wdr_id = ?",
            (wdr_id,),
        )
        row = cursor.fetchone()
        conn.commit()
        return row
    finally:
        conn.close()


def db_reject_withdrawal(wdr_id: str):
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE withdrawals SET status = 'REJECTED' WHERE wdr_id = ? AND status = 'PENDING'",
            (wdr_id,),
        )
        if cursor.rowcount == 0:
            return None

        cursor = conn.execute(
            "SELECT user_id, amount FROM withdrawals WHERE wdr_id = ?", (wdr_id,)
        )
        user_id, amount = cursor.fetchone()
        conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.commit()
        return user_id, amount
    finally:
        conn.close()


# ---------------------------------------------------------
# HELPER FUNCTIONS & FILTERS
# ---------------------------------------------------------
MENU_TEXTS = [
    "📧 NEW GMAIL SELL",
    "📧 OLD GMAIL SELL",
    "🎧 SUPPORT",
    "👤 PROFILE",
    "💳 WITHDRAW",
    "👥 REFER",
    "💲 GMAIL PRICES",
]
MENU_FILTER = ~filters.Regex(
    f"^({'|'.join(re.escape(t) for t in MENU_TEXTS)})$"
)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID


def generate_gmail_credentials():
    first_name = random.choice(FIRST_NAMES)
    last_name = random.choice(LAST_NAMES)
    random_num = random.randint(1000, 9999)
    keyword = random.choice(KEYWORDS)

    email = (
        f"{first_name.lower()}{last_name.lower()}{keyword}{random_num}@gmail.com"
    )
    chars = string.ascii_letters + string.digits + "!@#$"
    password = "".join(random.choices(chars, k=12))

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "password": password,
    }


def get_home_keyboard():
    keyboard = [
        [KeyboardButton("📧 NEW GMAIL SELL"), KeyboardButton("📧 OLD GMAIL SELL")],
        [KeyboardButton("🎧 SUPPORT"), KeyboardButton("👤 PROFILE")],
        [KeyboardButton("💳 WITHDRAW"), KeyboardButton("👥 REFER")],
        [KeyboardButton("💲 GMAIL PRICES")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def check_membership(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    try:
        member = await context.bot.get_chat_member(
            chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id
        )
        return member.status in ["creator", "administrator", "member"]
    except Exception:
        logger.warning(
            "Membership check failed for user_id=%s", user_id, exc_info=True
        )
        return False


# ---------------------------------------------------------
# START & REFERRAL PARSING
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Parse referral code e.g., /start Bot123456
    referrer_id = None
    if context.args and context.args[0].startswith("Bot"):
        try:
            referrer_id = int(context.args[0].replace("Bot", ""))
        except ValueError:
            referrer_id = None

    await asyncio.to_thread(get_or_create_user, user_id, referrer_id)

    if await check_membership(user_id, context):
        await send_home_menu(update.effective_chat.id, context)
    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=f"https://t.me/{CHANNEL_USERNAME}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔑 Verify Account", callback_data="verify_user"
                )
            ],
        ]
        text = (
            "⚠️ <b>Join First, Then Verify</b>\n\n"
            "✅ Join Channel ➔ Then Click Verify"
        )
        await update.message.reply_text(
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def handle_verification(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user_id = query.from_user.id

    if await check_membership(user_id, context):
        await query.answer("Verification Successful!")
        await query.message.delete()
        await send_home_menu(query.message.chat_id, context)
    else:
        await query.answer(
            text="⚠️ Join First, Then Verify.\n❌ No Join = No Verification.",
            show_alert=True,
        )


async def send_home_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏠 <b>Home Menu</b>\n━━━━━━━\n✅ <b>You are now in the home menu.</b>"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=get_home_keyboard(),
    )


# ---------------------------------------------------------
# GMAIL SELLING FLOWS
# ---------------------------------------------------------
async def handle_new_gmail_sell(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    creds = generate_gmail_credentials()
    context.user_data["assigned_new_gmail"] = creds

    msg_text = (
        "📨 <b>Register Gmail account using specified data</b>\n\n"
        f"👤 <b>First name:</b> {creds['first_name']}\n"
        f"👤 <b>Last Name:</b> {creds['last_name']}\n"
        f"✉️ <b>Email:</b> <code>{creds['email']}</code>\n"
        f"🔐 <b>Password:</b> <code>{creds['password']}</code>\n\n"
        "🔐 <b>Use specified data strictly to receive balance.</b>"
    )
    keyboard = [
        [InlineKeyboardButton("✔️ Done", callback_data="new_gmail_done")],
        [
            InlineKeyboardButton(
                "❌ Cancel Registration", callback_data="cancel_new_gmail"
            )
        ],
    ]
    await update.message.reply_text(
        msg_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def start_old_gmail_sell(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "📨 <b>Sell Old Gmail Account</b>\n\n"
        "Please reply with the <b>Gmail address</b>:\n"
        "<i>Example: account@gmail.com</i>",
        parse_mode="HTML",
    )
    return WAIT_OLD_GMAIL_EMAIL


async def receive_old_gmail_email(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    email = update.message.text.strip()
    if not re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email):
        await update.message.reply_text(
            "❌ Invalid format! Enter a valid Gmail address:"
        )
        return WAIT_OLD_GMAIL_EMAIL

    context.user_data["temp_old_email"] = email
    await update.message.reply_text(
        f"✅ <b>Email:</b> <code>{email}</code>\n\n"
        "🔐 Enter the <b>Password</b> for this account:",
        parse_mode="HTML",
    )
    return WAIT_OLD_GMAIL_PASS


async def receive_old_gmail_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    password = update.message.text.strip()
    email = context.user_data.get("temp_old_email")

    sub_id = f"SUB_{user.id}_{random.randint(10000, 99999)}"
    await asyncio.to_thread(
        db_insert_submission, sub_id, user.id, email, password, GMAIL_PRICE_OLD
    )

    await update.message.reply_text(
        "🎉 <b>Old Gmail Submitted Successfully!</b>\nSent to Admin for manual review.",
        parse_mode="HTML",
    )

    admin_card = (
        "📥 <b>NEW OLD GMAIL SUBMISSION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Seller:</b> {user.first_name}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"✉️ <b>Email:</b> <code>{email}</code>\n"
        f"🔑 <b>Password:</b> <code>{password}</code>\n"
        f"💰 <b>Reward:</b> {GMAIL_PRICE_OLD} Tk\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve & Pay", callback_data=f"adm_app_sub_{sub_id}"
                ),
                InlineKeyboardButton(
                    "❌ Reject", callback_data=f"adm_rej_sub_{sub_id}"
                ),
            ]
        ]
    )
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_card,
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    context.user_data.pop("temp_old_email", None)
    return ConversationHandler.END


# ---------------------------------------------------------
# MANUAL WITHDRAWAL FLOW
# ---------------------------------------------------------
async def start_withdraw_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    method = query.data.replace("withdraw_", "").upper()
    user_id = query.from_user.id
    balance = await asyncio.to_thread(get_user_balance, user_id)

    if balance <= 0:
        await query.message.reply_text(
            f"❌ <b>Insufficient Balance!</b>\nAvailable: <b>{balance} Tk</b>",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    context.user_data["withdraw_method"] = method
    await query.message.reply_text(
        f"💳 <b>Method:</b> {method}\n"
        f"💰 <b>Available Balance:</b> {balance} Tk\n\n"
        f"📱 Reply with your <b>{method} Account Number</b> and <b>Amount</b> separated by space:\n"
        "<i>Example: 01700000000 100</i>",
        parse_mode="HTML",
    )
    return WAIT_WITHDRAW_INFO


async def receive_withdraw_details(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    text = update.message.text.strip()
    method = context.user_data.get("withdraw_method", "UNKNOWN")

    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "⚠️ Invalid Format! Example: 01700000000 100"
        )
        return WAIT_WITHDRAW_INFO

    account_no = parts[0]
    try:
        amount = float(parts[1])
    except ValueError:
        await update.message.reply_text("❌ Enter a numeric amount.")
        return WAIT_WITHDRAW_INFO

    # Validates the balance, deducts it, and inserts the withdrawal row in a
    # single DB transaction so two fast/concurrent requests can't both pass
    # the balance check and jointly overdraw the account.
    wdr_id, balance_info = await asyncio.to_thread(
        db_create_withdrawal, user.id, method, account_no, amount
    )
    if wdr_id is None:
        await update.message.reply_text(
            f"❌ Invalid Amount! Requested: {amount} Tk | Available: {balance_info} Tk"
        )
        return WAIT_WITHDRAW_INFO

    remaining_bal = balance_info

    await update.message.reply_text(
        f"✅ <b>Withdrawal Request Received!</b>\n\n"
        f"💳 <b>Method:</b> {method}\n"
        f"📱 <b>Account:</b> {account_no}\n"
        f"💰 <b>Amount:</b> {amount} Tk\n"
        f"💼 <b>Remaining Balance:</b> {remaining_bal} Tk\n\n"
        "⏳ Admin will process payment manually.",
        parse_mode="HTML",
    )

    admin_card = (
        "💸 <b>NEW WITHDRAWAL REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user.first_name}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"💳 <b>Method:</b> {method}\n"
        f"📱 <b>Payee Account:</b> <code>{account_no}</code>\n"
        f"💵 <b>Payout Amount:</b> {amount} Tk\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Mark Paid", callback_data=f"adm_app_wdr_{wdr_id}"
                ),
                InlineKeyboardButton(
                    "❌ Reject & Refund", callback_data=f"adm_rej_wdr_{wdr_id}"
                ),
            ]
        ]
    )
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_card,
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Action canceled.")
    return ConversationHandler.END


# ---------------------------------------------------------
# MAIN MENU TEXT ROUTER
# ---------------------------------------------------------
async def handle_menu_options(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text
    user = update.effective_user
    balance = await asyncio.to_thread(get_user_balance, user.id)

    if text == "📧 NEW GMAIL SELL":
        await handle_new_gmail_sell(update, context)

    elif text == "🎧 SUPPORT":
        keyboard = [
            [
                InlineKeyboardButton(
                    "🛒 BUY GMAIL", callback_data="sup_buy_gmail"
                )
            ],
            [
                InlineKeyboardButton(
                    "❓ Gmail Cancel", callback_data="sup_gmail_cancel"
                )
            ],
            [
                InlineKeyboardButton(
                    "🧑‍💻 Technical Support", url="https://t.me/telegram"
                )
            ],
        ]
        await update.message.reply_text(
            "🔰 <b>Select Support Option:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "👤 PROFILE":
        ref_count = await asyncio.to_thread(db_get_ref_count, user.id)

        msg_text = (
            f"👤 <b>Name:</b> {user.first_name}\n"
            f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
            f"💰 <b>Balance:</b> {balance} Tk\n"
            f"👥 <b>Total Referrals:</b> {ref_count}\n\n✦─────✦"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "📋 Copy ID", callback_data=f"copy_id_{user.id}"
                )
            ]
        ]
        await update.message.reply_text(
            msg_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "💳 WITHDRAW":
        keyboard = [
            [
                InlineKeyboardButton(
                    "📱 Bkash", callback_data="withdraw_bkash"
                ),
                InlineKeyboardButton(
                    "📱 Nagad", callback_data="withdraw_nagad"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🏦 Binance", callback_data="withdraw_binance"
                ),
                InlineKeyboardButton(
                    "🪙 USDT (BEP-20)", callback_data="withdraw_usdt_bep20"
                ),
            ],
        ]
        await update.message.reply_text(
            f"🎯 <b>Withdrawal Menu</b>\n💰 Available: <b>{balance} Tk</b>\nSelect Method:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "👥 REFER":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=Bot{user.id}"
        msg_text = (
            f"🎯 <b>Referral System</b>\n\n"
            f"Earn <b>{REFERRAL_REWARD} Tk</b> for every active user you invite!\n\n"
            f"🔗 <b>Your Invite Link:</b>\n<code>{ref_link}</code>"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "📋 Copy Link", callback_data="copy_ref_link"
                )
            ]
        ]
        await update.message.reply_text(
            msg_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "💲 GMAIL PRICES":
        await update.message.reply_text(
            f"📊 <b>Current Gmail Prices</b>\n\n📧 <b>OLD GMAIL:</b> {GMAIL_PRICE_OLD} Tk\n📧 <b>NEW GMAIL:</b> {GMAIL_PRICE_NEW} Tk",
            parse_mode="HTML",
        )


# ---------------------------------------------------------
# CALLBACK HANDLERS & ADMIN ENGINE
# ---------------------------------------------------------
async def handle_universal_callbacks(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    data = query.data
    user = query.from_user

    # Support Menu Interactive Responses
    if data == "sup_buy_gmail":
        await query.answer()
        await query.message.reply_text(
            "🛒 <b>Buy Gmail Accounts</b>\n\nTo purchase bulk Gmail accounts, contact support directly at @telegram.",
            parse_mode="HTML",
        )
    elif data == "sup_gmail_cancel":
        await query.answer()
        await query.message.reply_text(
            "❓ <b>Gmail Submission Rules</b>\n\nAccounts rejected by admin are usually invalid or have password mismatches. Double-check details before resubmitting.",
            parse_mode="HTML",
        )

    elif data.startswith("copy_id_"):
        await query.answer("📋 ID copied!", show_alert=True)
    elif data == "copy_ref_link":
        await query.answer("📋 Referral link copied!", show_alert=True)
    elif data == "cancel_new_gmail":
        context.user_data.pop("assigned_new_gmail", None)
        await query.answer("Task canceled.")
        await query.message.edit_text("❌ Registration task canceled.")

    # New Gmail "Done" Flow
    elif data == "new_gmail_done":
        creds = context.user_data.get("assigned_new_gmail")
        if not creds:
            await query.answer("Task expired.", show_alert=True)
            return

        sub_id = f"SUB_{user.id}_{random.randint(10000, 99999)}"
        await asyncio.to_thread(
            db_insert_submission,
            sub_id,
            user.id,
            creds["email"],
            creds["password"],
            GMAIL_PRICE_NEW,
        )

        await query.answer("Submitted!")
        await query.message.edit_text(
            "🎉 <b>Gmail Submitted Successfully!</b>\nWaiting for admin verification.",
            parse_mode="HTML",
        )

        admin_card = (
            "📥 <b>NEW GMAIL SUBMISSION</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Seller:</b> {user.first_name}\n"
            f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
            f"✉️ <b>Email:</b> <code>{creds['email']}</code>\n"
            f"🔑 <b>Password:</b> <code>{creds['password']}</code>\n"
            f"💰 <b>Reward:</b> {GMAIL_PRICE_NEW} Tk\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve & Pay",
                        callback_data=f"adm_app_sub_{sub_id}",
                    ),
                    InlineKeyboardButton(
                        "❌ Reject", callback_data=f"adm_rej_sub_{sub_id}"
                    ),
                ]
            ]
        )
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_card,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        context.user_data.pop("assigned_new_gmail", None)

    # Admin Submissions Handling
    elif data.startswith("adm_app_sub_"):
        if not is_admin(user.id):
            return
        sub_id = data.replace("adm_app_sub_", "")

        result = await asyncio.to_thread(db_approve_submission, sub_id)

        if result:
            sub_user_id, reward, referrer_id = result

            if referrer_id:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 <b>Referral Bonus!</b> You earned <b>+{REFERRAL_REWARD} Tk</b> for an active referral.",
                    parse_mode="HTML",
                )

            await query.edit_message_text(
                f"{query.message.text}\n\n✅ <b>APPROVED & PAID (+{reward} Tk)</b>",
                parse_mode="HTML",
            )
            await context.bot.send_message(
                chat_id=sub_user_id,
                text=f"🎉 <b>Gmail Approved!</b>\n💰 <b>+{reward} Tk</b> added to profile.",
                parse_mode="HTML",
            )
            await query.answer("Approved!")
        else:
            await query.answer("Already processed.", show_alert=True)

    elif data.startswith("adm_rej_sub_"):
        if not is_admin(user.id):
            return
        sub_id = data.replace("adm_rej_sub_", "")

        sub = await asyncio.to_thread(db_reject_submission, sub_id)

        if sub:
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ <b>REJECTED</b>", parse_mode="HTML"
            )
            await context.bot.send_message(
                chat_id=sub[0],
                text=f"❌ <b>Gmail Submission Rejected.</b> Verification failed for <code>{sub[1]}</code>",
                parse_mode="HTML",
            )
            await query.answer("Rejected.")
        else:
            await query.answer("Already processed.", show_alert=True)

    # Admin Withdrawal Actions
    elif data.startswith("adm_app_wdr_"):
        if not is_admin(user.id):
            return
        wdr_id = data.replace("adm_app_wdr_", "")

        wdr = await asyncio.to_thread(db_approve_withdrawal, wdr_id)

        if wdr:
            await query.edit_message_text(
                f"{query.message.text}\n\n✅ <b>STATUS: PAID & SENT</b>",
                parse_mode="HTML",
            )
            await context.bot.send_message(
                chat_id=wdr[0],
                text=f"🎉 <b>Withdrawal Sent!</b> {wdr[1]} Tk via {wdr[2]} (Acc: {wdr[3]}).",
                parse_mode="HTML",
            )
            await query.answer("Paid!")
        else:
            await query.answer("Already processed.", show_alert=True)

    elif data.startswith("adm_rej_wdr_"):
        if not is_admin(user.id):
            return
        wdr_id = data.replace("adm_rej_wdr_", "")

        wdr = await asyncio.to_thread(db_reject_withdrawal, wdr_id)

        if wdr:
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ <b>REJECTED & REFUNDED</b>",
                parse_mode="HTML",
            )
            await context.bot.send_message(
                chat_id=wdr[0],
                text=f"❌ <b>Withdrawal Rejected.</b> {wdr[1]} Tk refunded to your profile balance.",
                parse_mode="HTML",
            )
            await query.answer("Refunded.")
        else:
            await query.answer("Already processed.", show_alert=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception while processing an update", exc_info=context.error)


# ---------------------------------------------------------
# APPLICATION INIT
# ---------------------------------------------------------
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(handle_verification, pattern="^verify_user$")
    )

    old_gmail_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^📧 OLD GMAIL SELL$"), start_old_gmail_sell
            )
        ],
        states={
            WAIT_OLD_GMAIL_EMAIL: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & MENU_FILTER,
                    receive_old_gmail_email,
                )
            ],
            WAIT_OLD_GMAIL_PASS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & MENU_FILTER,
                    receive_old_gmail_password,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_flow),
            MessageHandler(~MENU_FILTER, cancel_flow),
        ],
    )

    withdraw_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_withdraw_callback, pattern="^withdraw_")
        ],
        states={
            WAIT_WITHDRAW_INFO: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & MENU_FILTER,
                    receive_withdraw_details,
                )
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_flow),
            MessageHandler(~MENU_FILTER, cancel_flow),
        ],
    )

    app.add_handler(old_gmail_conv)
    app.add_handler(withdraw_conv)
    app.add_handler(CallbackQueryHandler(handle_universal_callbacks))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_options)
    )

    print("Database connected. Bot polling started...")
    app.run_polling()


if __name__ == "__main__":
    main()