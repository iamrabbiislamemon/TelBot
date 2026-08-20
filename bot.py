import asyncio
import html
import logging
import os
import random
import re
import string

import libsql
from telegram import (
    CopyTextButton,
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

# Turso (remote libSQL) — replaces local SQLite so data survives job restarts
TURSO_DATABASE_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_AUTH_TOKEN = os.environ["TURSO_AUTH_TOKEN"]

# Financial Settings — defaults only; live values are stored in the
# `settings` DB table and editable from the admin panel at runtime.
DEFAULT_SETTINGS = {
    "gmail_price_new": "20.0",
    "gmail_price_old": "25.0",
    "referral_reward": "2.0",
}

# Conversation States
WAIT_OLD_GMAIL_EMAIL, WAIT_OLD_GMAIL_PASS = range(2)
WAIT_WITHDRAW_INFO = 2
ADMIN_MENU, WAIT_ADMIN_VALUE = range(3, 5)

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
# DATABASE MANAGEMENT (Turso / remote libSQL)
# ---------------------------------------------------------
def init_db():
    conn = libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
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

    # Admin-editable settings (prices, referral bonus, ...)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Support contacts shown in the Support menu (admin-managed)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS support_contacts (
            contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            url TEXT
        )
    """)

    conn.commit()
    conn.close()


def get_db_connection():
    return libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)


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
        referral_bonus = 0.0
        if referrer_id:
            setting_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'referral_reward'"
            ).fetchone()
            referral_bonus = float(
                setting_row[0] if setting_row else DEFAULT_SETTINGS["referral_reward"]
            )
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (referral_bonus, referrer_id),
            )
            conn.execute(
                "UPDATE users SET ref_count = ref_count + 1, referred_by = NULL WHERE user_id = ?",
                (sub_user_id,),
            )

        conn.commit()
        return sub_user_id, reward, referrer_id, referral_bonus
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


def db_get_admin_stats():
    conn = get_db_connection()
    try:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pending_subs = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE status = 'PENDING'"
        ).fetchone()[0]
        pending_wdrs = conn.execute(
            "SELECT COUNT(*) FROM withdrawals WHERE status = 'PENDING'"
        ).fetchone()[0]
        total_balance = conn.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM users"
        ).fetchone()[0]
        return {
            "total_users": total_users,
            "pending_subs": pending_subs,
            "pending_wdrs": pending_wdrs,
            "total_balance": total_balance,
        }
    finally:
        conn.close()


def db_list_all_user_ids():
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def db_get_all_settings() -> dict:
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        settings = dict(DEFAULT_SETTINGS)
        settings.update({key: value for key, value in rows})
        return settings
    finally:
        conn.close()


def db_set_setting(key: str, value: str):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def db_list_support_contacts():
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT contact_id, label, url FROM support_contacts ORDER BY contact_id"
        ).fetchall()
    finally:
        conn.close()


def db_add_support_contact(label: str, url: str):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO support_contacts (label, url) VALUES (?, ?)", (label, url)
        )
        conn.commit()
    finally:
        conn.close()


def db_remove_support_contact(contact_id: int) -> bool:
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM support_contacts WHERE contact_id = ?", (contact_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
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
    "⚙️ ADMIN PANEL",
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


def get_home_keyboard(is_admin_user: bool = False):
    keyboard = [
        [KeyboardButton("📧 NEW GMAIL SELL"), KeyboardButton("📧 OLD GMAIL SELL")],
        [KeyboardButton("🎧 SUPPORT"), KeyboardButton("👤 PROFILE")],
        [KeyboardButton("💳 WITHDRAW"), KeyboardButton("👥 REFER")],
        [KeyboardButton("💲 GMAIL PRICES")],
    ]
    if is_admin_user:
        keyboard.append([KeyboardButton("⚙️ ADMIN PANEL")])
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


def build_join_prompt():
    keyboard = [
        [
            InlineKeyboardButton(
                "📢 Join Channel", url=f"https://t.me/{CHANNEL_USERNAME}"
            )
        ],
        [InlineKeyboardButton("🔑 Verify Account", callback_data="verify_user")],
    ]
    text = (
        "⚠️ <b>Join First, Then Verify</b>\n\n"
        "✅ Join Channel ➔ Then Click Verify"
    )
    return text, InlineKeyboardMarkup(keyboard)


async def ensure_active_user(
    chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Guarantees a users row exists and channel membership is still current.
    A stale keyboard/message can let a user tap into the bot without a fresh
    /start — this re-checks both on every menu action, not just at /start, so
    a user who left the channel (or whose row never existed) gets bounced
    back to Join/Verify instead of silently using paid features or having
    actions succeed with no users row to credit. Returns False and sends the
    Join/Verify prompt if the check fails — callers must stop processing."""
    await asyncio.to_thread(get_or_create_user, user_id)

    if await check_membership(user_id, context):
        return True

    text, markup = build_join_prompt()
    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup
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
        text, markup = build_join_prompt()
        await update.message.reply_text(
            text=text, parse_mode="HTML", reply_markup=markup
        )


async def handle_verification(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user_id = query.from_user.id
    # Reachable from an old cached "Verify Account" button — guarantee the
    # users row exists even if this wasn't preceded by /start on this instance.
    await asyncio.to_thread(get_or_create_user, user_id)

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
        reply_markup=get_home_keyboard(is_admin(chat_id)),
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
    # This is its own conversation entry point, separate from
    # handle_menu_options — reachable directly from a stale keyboard, so it
    # needs its own re-check.
    if not await ensure_active_user(
        update.effective_chat.id, update.effective_user.id, context
    ):
        return ConversationHandler.END

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

    settings = await asyncio.to_thread(db_get_all_settings)
    price_old = float(settings["gmail_price_old"])

    sub_id = f"SUB_{user.id}_{random.randint(10000, 99999)}"
    await asyncio.to_thread(
        db_insert_submission, sub_id, user.id, email, password, price_old
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
        f"💰 <b>Reward:</b> {price_old} Tk\n"
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
    # Own conversation entry point, reachable from an old cached inline
    # keyboard — re-check before reading balance.
    if not await ensure_active_user(query.message.chat_id, user_id, context):
        return ConversationHandler.END
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
# ADMIN SETTINGS FLOW
# ---------------------------------------------------------
def build_admin_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💲 New Gmail Price", callback_data="adm_menu_price_new"
                ),
                InlineKeyboardButton(
                    "💲 Old Gmail Price", callback_data="adm_menu_price_old"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎁 Referral Bonus", callback_data="adm_menu_referral"
                )
            ],
            [
                InlineKeyboardButton(
                    "🛠 Support Contacts", callback_data="adm_menu_support"
                )
            ],
            [InlineKeyboardButton("📢 Broadcast", callback_data="adm_menu_broadcast")],
            [InlineKeyboardButton("📊 Stats", callback_data="adm_menu_stats")],
            [InlineKeyboardButton("✖️ Close", callback_data="adm_menu_close")],
        ]
    )


async def build_support_submenu():
    contacts = await asyncio.to_thread(db_list_support_contacts)
    rows = [
        [
            InlineKeyboardButton(
                f"🗑 Remove {label}", callback_data=f"adm_menu_support_del_{contact_id}"
            )
        ]
        for contact_id, label, url in contacts
    ]
    rows.append(
        [InlineKeyboardButton("➕ Add Contact", callback_data="adm_menu_support_add")]
    )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_menu_back")])

    text = "🛠 <b>Support Contacts</b>\n━━━━━━━\n"
    if contacts:
        text += "\n".join(f"• {label} — {url}" for _, label, url in contacts)
    else:
        text += "<i>No contacts added yet.</i>"

    return text, InlineKeyboardMarkup(rows)


async def open_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    await update.message.reply_text(
        "⚙️ <b>Admin Panel</b>\nSelect what you want to manage:",
        parse_mode="HTML",
        reply_markup=build_admin_main_menu(),
    )
    return ADMIN_MENU


ADMIN_EDIT_TARGETS = {
    "adm_menu_price_new": ("gmail_price_new", "New Gmail Price"),
    "adm_menu_price_old": ("gmail_price_old", "Old Gmail Price"),
    "adm_menu_referral": ("referral_reward", "Referral Bonus"),
}


async def run_broadcast(bot, admin_chat_id: int, text: str):
    user_ids = await asyncio.to_thread(db_list_all_user_ids)
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await bot.send_message(
        chat_id=admin_chat_id,
        text=(
            f"✅ <b>Broadcast Complete</b>\n\n"
            f"📤 Sent: {sent}\n"
            f"❌ Failed (blocked/deleted): {failed}"
        ),
        parse_mode="HTML",
    )


async def handle_admin_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return ConversationHandler.END

    data = query.data

    if data == "adm_menu_close":
        await query.answer()
        await query.edit_message_text("⚙️ Admin panel closed.")
        return ConversationHandler.END

    if data == "adm_menu_back":
        await query.answer()
        await query.edit_message_text(
            "⚙️ <b>Admin Panel</b>\nSelect what you want to manage:",
            parse_mode="HTML",
            reply_markup=build_admin_main_menu(),
        )
        return ADMIN_MENU

    if data == "adm_menu_stats":
        await query.answer()
        stats = await asyncio.to_thread(db_get_admin_stats)
        settings = await asyncio.to_thread(db_get_all_settings)
        text = (
            "📊 <b>Bot Stats</b>\n━━━━━━━\n"
            f"👥 <b>Total Users:</b> {stats['total_users']}\n"
            f"💰 <b>Total User Balance:</b> {stats['total_balance']} Tk\n"
            f"📨 <b>Pending Submissions:</b> {stats['pending_subs']}\n"
            f"💳 <b>Pending Withdrawals:</b> {stats['pending_wdrs']}\n\n"
            f"📧 <b>NEW GMAIL Price:</b> {settings['gmail_price_new']} Tk\n"
            f"📧 <b>OLD GMAIL Price:</b> {settings['gmail_price_old']} Tk\n"
            f"👥 <b>Referral Bonus:</b> {settings['referral_reward']} Tk"
        )
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="adm_menu_back")]]
            ),
        )
        return ADMIN_MENU

    if data == "adm_menu_support":
        await query.answer()
        text, markup = await build_support_submenu()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return ADMIN_MENU

    if data.startswith("adm_menu_support_del_"):
        contact_id = int(data.replace("adm_menu_support_del_", ""))
        removed = await asyncio.to_thread(db_remove_support_contact, contact_id)
        await query.answer("Removed." if removed else "Already removed.")
        text, markup = await build_support_submenu()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return ADMIN_MENU

    if data == "adm_menu_support_add":
        await query.answer()
        context.user_data["admin_edit_target"] = "support_add"
        await query.edit_message_text(
            "➕ <b>Add Support Contact</b>\n\n"
            "Reply with: <code>Label | https://t.me/username</code>\n"
            "<i>Example: WhatsApp | https://wa.me/8801700000000</i>",
            parse_mode="HTML",
        )
        return WAIT_ADMIN_VALUE

    if data in ADMIN_EDIT_TARGETS:
        key, label = ADMIN_EDIT_TARGETS[data]
        await query.answer()
        context.user_data["admin_edit_target"] = key
        await query.edit_message_text(
            f"✏️ <b>Edit {label}</b>\n\nReply with the new amount (numbers only, e.g. 22.5):",
            parse_mode="HTML",
        )
        return WAIT_ADMIN_VALUE

    if data == "adm_menu_broadcast":
        await query.answer()
        context.user_data["admin_edit_target"] = "broadcast_message"
        await query.edit_message_text(
            "📢 <b>Broadcast Message</b>\n\n"
            "Reply with the message to send to <b>all users</b>. "
            "Sent as plain text — no formatting.",
            parse_mode="HTML",
        )
        return WAIT_ADMIN_VALUE

    if data == "adm_menu_broadcast_cancel":
        context.user_data.pop("broadcast_text", None)
        await query.answer("Canceled.")
        await query.edit_message_text("❌ Broadcast canceled.")
        return ConversationHandler.END

    if data == "adm_menu_broadcast_confirm":
        broadcast_text = context.user_data.pop("broadcast_text", None)
        if not broadcast_text:
            await query.answer()
            await query.edit_message_text(
                "❌ Nothing to send. Open the Admin Panel again."
            )
            return ConversationHandler.END

        await query.answer("Sending...")
        await query.edit_message_text(
            "📤 Broadcast started — running in the background so the bot "
            "stays responsive to other users. A summary will follow here."
        )
        # Runs as a background task (not awaited) so this handler returns
        # immediately — otherwise PTB processes updates one at a time by
        # default, and a broadcast to many users would freeze the bot for
        # everyone else until every send completed.
        context.application.create_task(
            run_broadcast(context.bot, query.from_user.id, broadcast_text)
        )
        return ConversationHandler.END

    await query.answer()
    return ADMIN_MENU


async def receive_admin_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = context.user_data.get("admin_edit_target")
    text = update.message.text.strip()

    if target in ("gmail_price_new", "gmail_price_old", "referral_reward"):
        try:
            value = float(text)
            if value < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Enter a valid positive number, e.g. 22.5"
            )
            return WAIT_ADMIN_VALUE

        await asyncio.to_thread(db_set_setting, target, str(value))
        context.user_data.pop("admin_edit_target", None)
        await update.message.reply_text(f"✅ Updated. New value: {value} Tk")
        return ConversationHandler.END

    if target == "support_add":
        if "|" not in text:
            await update.message.reply_text(
                "❌ Invalid format. Use: Label | https://t.me/username"
            )
            return WAIT_ADMIN_VALUE

        label, url = (part.strip() for part in text.split("|", 1))
        if not label or not re.match(r"^https?://", url):
            await update.message.reply_text(
                "❌ URL must start with http:// or https://. "
                "Use: Label | https://t.me/username"
            )
            return WAIT_ADMIN_VALUE

        await asyncio.to_thread(db_add_support_contact, label, url)
        context.user_data.pop("admin_edit_target", None)
        await update.message.reply_text(f"✅ Support contact added: {label}")
        return ConversationHandler.END

    if target == "broadcast_message":
        context.user_data.pop("admin_edit_target", None)
        context.user_data["broadcast_text"] = update.message.text
        user_ids = await asyncio.to_thread(db_list_all_user_ids)
        await update.message.reply_text(
            f"📢 <b>Confirm Broadcast</b>\n\n"
            f"This will be sent to <b>{len(user_ids)}</b> users:\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n{html.escape(update.message.text)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Send", callback_data="adm_menu_broadcast_confirm"
                        ),
                        InlineKeyboardButton(
                            "❌ Cancel", callback_data="adm_menu_broadcast_cancel"
                        ),
                    ]
                ]
            ),
        )
        return ADMIN_MENU

    context.user_data.pop("admin_edit_target", None)
    await update.message.reply_text("❌ Nothing to update. Open the Admin Panel again.")
    return ConversationHandler.END


# ---------------------------------------------------------
# MAIN MENU TEXT ROUTER
# ---------------------------------------------------------
async def handle_menu_options(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text
    user = update.effective_user
    # A returning user can tap a menu button from a stale keyboard without
    # ever sending /start (or after leaving the channel) — re-verify before
    # anything here touches balance.
    if not await ensure_active_user(update.effective_chat.id, user.id, context):
        return
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
        ]
        contacts = await asyncio.to_thread(db_list_support_contacts)
        for _contact_id, label, url in contacts:
            keyboard.append([InlineKeyboardButton(f"🧑‍💻 {label}", url=url)])

        msg_text = "🔰 <b>Select Support Option:</b>"
        if not contacts:
            msg_text += "\n\n<i>No direct contact added yet — use the options above.</i>"

        await update.message.reply_text(
            msg_text,
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
                    "📋 Copy ID", copy_text=CopyTextButton(str(user.id))
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
        settings = await asyncio.to_thread(db_get_all_settings)
        referral_reward = float(settings["referral_reward"])
        ref_link = f"https://t.me/{BOT_USERNAME}?start=Bot{user.id}"
        msg_text = (
            f"🎯 <b>Referral System</b>\n\n"
            f"Earn <b>{referral_reward} Tk</b> for every active user you invite!\n\n"
            f"🔗 <b>Your Invite Link:</b>\n<code>{ref_link}</code>"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "📋 Copy Link", copy_text=CopyTextButton(ref_link)
                )
            ]
        ]
        await update.message.reply_text(
            msg_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "💲 GMAIL PRICES":
        settings = await asyncio.to_thread(db_get_all_settings)
        await update.message.reply_text(
            f"📊 <b>Current Gmail Prices</b>\n\n"
            f"📧 <b>OLD GMAIL:</b> {settings['gmail_price_old']} Tk\n"
            f"📧 <b>NEW GMAIL:</b> {settings['gmail_price_new']} Tk",
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

    # Same reasoning as handle_menu_options: a stale message from a previous
    # bot instance, or a channel-leave, can let a user act without a valid
    # users row / current membership. Skip this for admin (adm_*) actions —
    # those are already gated by is_admin() below and must not be blocked by
    # the *submitter's* channel status.
    if not data.startswith("adm_"):
        if not await ensure_active_user(query.message.chat_id, user.id, context):
            await query.answer()
            return

    # Support Menu Interactive Responses
    if data == "sup_buy_gmail":
        await query.answer()
        await query.message.reply_text(
            "🛒 <b>Buy Gmail Accounts</b>\n\nTo purchase bulk Gmail accounts, use one of the support contacts above.",
            parse_mode="HTML",
        )
    elif data == "sup_gmail_cancel":
        await query.answer()
        await query.message.reply_text(
            "❓ <b>Gmail Submission Rules</b>\n\nAccounts rejected by admin are usually invalid or have password mismatches. Double-check details before resubmitting.",
            parse_mode="HTML",
        )

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

        settings = await asyncio.to_thread(db_get_all_settings)
        price_new = float(settings["gmail_price_new"])

        sub_id = f"SUB_{user.id}_{random.randint(10000, 99999)}"
        await asyncio.to_thread(
            db_insert_submission,
            sub_id,
            user.id,
            creds["email"],
            creds["password"],
            price_new,
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
            f"💰 <b>Reward:</b> {price_new} Tk\n"
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
            sub_user_id, reward, referrer_id, referral_bonus = result

            if referrer_id:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 <b>Referral Bonus!</b> You earned <b>+{referral_bonus} Tk</b> for an active referral.",
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

    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^⚙️ ADMIN PANEL$"), open_admin_panel)
        ],
        states={
            ADMIN_MENU: [
                CallbackQueryHandler(
                    handle_admin_menu_callback, pattern="^adm_menu_"
                )
            ],
            WAIT_ADMIN_VALUE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & MENU_FILTER,
                    receive_admin_value,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_flow),
            MessageHandler(~MENU_FILTER, cancel_flow),
        ],
    )

    app.add_handler(old_gmail_conv)
    app.add_handler(withdraw_conv)
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(handle_universal_callbacks))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_options)
    )

    print("Database connected. Bot polling started...")
    app.run_polling()


if __name__ == "__main__":
    main()