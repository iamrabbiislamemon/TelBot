import random
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
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
CHANNEL_USERNAME = "FastGmailMarket"
BOT_USERNAME = "fast_gmail_sell_bot"

# REPLACE THIS WITH YOUR NUMERICAL TELEGRAM USER ID (Get it from @userinfobot)
ADMIN_CHAT_ID = 123456789

# Conversation state
WAIT_WITHDRAW_INFO = 1

# Simulated User Balances (In production, connect this to SQLite/MongoDB)
USER_BALANCES = {}


def get_user_balance(user_id: int) -> float:
    """Returns the user's current balance (default 0.0 Tk)."""
    return USER_BALANCES.get(user_id, 0.0)


# ---------------------------------------------------------
# KEYBOARDS
# ---------------------------------------------------------
def get_home_keyboard():
    keyboard = [
        [KeyboardButton("📧 NEW GMAIL SELL"), KeyboardButton("📧 OLD GMAIL SELL")],
        [KeyboardButton("🎧 SUPPORT"), KeyboardButton("👤 PROFILE")],
        [KeyboardButton("💳 WITHDRAW"), KeyboardButton("👥 REFER")],
        [KeyboardButton("💲 GMAIL PRICES")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def check_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(
            chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id
        )
        return member.status in ["creator", "administrator", "member"]
    except Exception:
        return False


# ---------------------------------------------------------
# START & VERIFICATION
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await check_membership(user_id, context):
        await send_home_menu(update.effective_chat.id, context)
    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=f"tg://resolve?domain={CHANNEL_USERNAME}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔑 Verify Account", callback_data="verify_user"
                )
            ],
        ]
        text = (
            "⚠️ 𝐉𝐨𝐢𝐧 𝐅𝐢𝐫𝐬𝐭, 𝐓𝐡𝐞𝐧 𝐕𝐞𝐫𝐢𝐟𝐲\n\n"
            "✅ 𝐉𝐨𝐢𝐧 𝐅𝐢𝐫𝐬𝐭 ➔ 𝐓𝐡𝐞𝐧 𝐂𝐥𝐢𝐜𝐤 𝐕𝐞𝐫𝐢𝐟𝐲"
        )
        await update.message.reply_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def handle_verification(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user_id = query.from_user.id

    if await check_membership(user_id, context):
        await query.answer(" Verification Successful!")
        await query.message.delete()
        await send_home_menu(query.message.chat_id, context)
    else:
        alert_text = (
            "⚠️ 𝐉𝐨𝐢𝐧 𝐅𝐢𝐫𝐬𝐭, 𝐓𝐡𝐞𝐧 𝐕𝐞𝐫𝐢𝐟𝐲.\n"
            "❌ 𝐍𝐨 𝐉𝐨𝐢𝐧 = 𝐍𝐨 𝐕𝐞𝐫𝐢𝐟𝐢𝐜𝐚𝐭𝐢𝐨𝐧."
        )
        await query.answer(text=alert_text, show_alert=True)


async def send_home_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏠 <b>Home Menu</b>\n"
        "━━━━━━━\n"
        "✅ <b>You are now in the home menu.</b>"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=get_home_keyboard(),
    )


# ---------------------------------------------------------
# WITHDRAWAL FLOW
# ---------------------------------------------------------
async def start_withdraw_process(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Triggered when a user clicks a specific withdrawal method button."""
    query = update.callback_query
    await query.answer()

    method = query.data.replace("withdraw_", "").upper()
    user_id = query.from_user.id
    balance = get_user_balance(user_id)

    # Save selection to user context
    context.user_data["withdraw_method"] = method

    prompt_text = (
        f"💳 <b>Selected Method:</b> {method}\n"
        f"💰 <b>Your Available Balance:</b> {balance} Tk\n\n"
        f"📱 <b>Please reply with your {method} account/phone number and withdrawal amount:</b>\n"
        "<i>Example: 017XXXXXXXX 50</i>"
    )
    await query.message.reply_text(prompt_text, parse_mode="HTML")
    return WAIT_WITHDRAW_INFO


async def receive_withdraw_details(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Receives user number/amount and forwards request to Admin inbox."""
    user = update.effective_user
    user_input = update.message.text
    method = context.user_data.get("withdraw_method", "UNKNOWN")
    balance = get_user_balance(user.id)

    # 1. Notify the user
    await update.message.reply_text(
        "✅ <b>Withdrawal request submitted successfully!</b>\n"
        "An admin will review and process your payment shortly.",
        parse_mode="HTML",
    )

    # 2. Format request report for Admin Inbox
    admin_report = (
        "🚨 <b>NEW WITHDRAWAL REQUEST</b> 🚨\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {user.first_name}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"💳 <b>Method:</b> {method}\n"
        f"💰 <b>Profile Balance:</b> {balance} Tk\n"
        f"📞 <b>Submitted Details:</b> <code>{user_input}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    admin_keyboard = [
        [
            InlineKeyboardButton(
                "✅ Approve", callback_data=f"adm_app_{user.id}"
            ),
            InlineKeyboardButton(
                "❌ Reject", callback_data=f"adm_rej_{user.id}"
            ),
        ]
    ]

    # 3. Send report directly to Admin Inbox
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_report,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(admin_keyboard),
    )

    return ConversationHandler.END


async def cancel_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Withdrawal request canceled.")
    return ConversationHandler.END


# ---------------------------------------------------------
# MAIN MENU HANDLERS
# ---------------------------------------------------------
async def handle_menu_options(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text
    user = update.effective_user
    balance = get_user_balance(user.id)

    if text == "📧 NEW GMAIL SELL":
        rand_pass = "".join(
            random.choices(string.ascii_letters + string.digits, k=10)
        )
        msg_text = (
            "📨 <b>Register Gmail account using the specified data</b>\n\n"
            "👤 <b>First name:</b> Shahriar\n"
            "👤 <b>Last Name:</b> ❌\n"
            "✉️ <b>Email:</b> <code>rovenexi121@gmail.com</code>\n"
            f"🔐 <b>Password:</b> <code>{rand_pass}</code>\n\n"
            "🔐 <b>Be sure to use the specified data, otherwise the account will not be paid.</b>"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "✔️ Done", callback_data="new_gmail_done"
                )
            ],
            [
                InlineKeyboardButton(
                    "📖 How To Create Account", url="https://t.me/Telegram"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel Registration",
                    callback_data="cancel_registration",
                )
            ],
        ]
        await update.message.reply_text(
            msg_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "📧 OLD GMAIL SELL":
        await update.message.reply_text(
            "📨 <b>Send Gmail Address</b>\n\n"
            "✅ <b>Please Enter A Valid Gmail Address</b>",
            parse_mode="HTML",
        )

    elif text == "🎧 SUPPORT":
        msg_text = "🔰 <b>Select A Gmail Option Below:</b>"
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
                    "⏰ Report Status", callback_data="sup_report_status"
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 Delay News", callback_data="sup_delay_news"
                )
            ],
            [
                InlineKeyboardButton(
                    "🧑‍💻 Technical Support", url="https://t.me/telegram"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ 1 Week Rejected Orders", callback_data="sup_rejected"
                )
            ],
            [
                InlineKeyboardButton(
                    "📜 1 Week Approved Orders", callback_data="sup_approved"
                )
            ],
            [
                InlineKeyboardButton(
                    "💸 1 Week Withdrawals", callback_data="sup_withdrawals"
                )
            ],
        ]
        await update.message.reply_text(
            msg_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "👤 PROFILE":
        msg_text = (
            f"👤 <b>Name:</b> {user.first_name}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
            f"💰 <b>Balanced:</b> {balance} Tk\n\n"
            "✦─────✦"
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
        msg_text = (
            "🎯 <b>Withdraw</b>\n"
            "━━━━━━━━━━━\n"
            f"💰 <b>Available Balance:</b> {balance} Tk\n"
            "💳 <b>Select Payment Method</b> ⚡"
        )
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
            [
                InlineKeyboardButton(
                    "🔶 USDT (TRC-20)", callback_data="withdraw_usdt_trc20"
                ),
                InlineKeyboardButton(
                    "⬛ LTC (Litecoin)", callback_data="withdraw_ltc"
                ),
            ],
        ]
        await update.message.reply_text(
            msg_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "👥 REFER":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=Bot{user.id}"
        msg_text = (
            "🎯 <b>Referral System</b>\n\n"
            "👥 <b>Total referrals:</b> 0\n"
            "💰 <b>Reward per referral:</b> 2 Tk\n\n"
            "🔗 <b>Your referral link:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            "📢 <b>Invite friends and earn more.</b>"
        )
        share_url = f"https://t.me/share/url?url={ref_link}&text=Join%20this%20bot%20to%20sell%20Gmail%20accounts!"
        keyboard = [
            [
                InlineKeyboardButton(
                    "📋 Copy Referral Link", callback_data="copy_ref_link"
                )
            ],
            [
                InlineKeyboardButton(
                    "↗️ Share Referral Link", url=share_url
                )
            ],
        ]
        await update.message.reply_text(
            msg_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif text == "💲 GMAIL PRICES":
        msg_text = (
            "📊 <b>Current Gmail Prices</b>\n\n"
            "📧 <b>OLD GMAIL SELL:-</b> 25 Tk\n"
            "📧 <b>NEW GMAIL SELL:-</b> 20 Tk\n\n"
            "💸 <b>Start Selling Now To Earn Money.</b>"
        )
        await update.message.reply_text(msg_text, parse_mode="HTML")


async def handle_callbacks(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    data = query.data

    if data.startswith("copy_id_"):
        await query.answer("📋 ID copied to clipboard!", show_alert=True)
    elif data == "copy_ref_link":
        await query.answer(
            "📋 Referral link copied to clipboard!", show_alert=True
        )
    elif data.startswith("adm_app_"):
        target_id = data.replace("adm_app_", "")
        await query.answer("Payment Approved!")
        await query.edit_message_text(
            f"{query.message.text}\n\n✅ <b>STATUS: APPROVED</b>",
            parse_mode="HTML",
        )
        await context.bot.send_message(
            chat_id=target_id,
            text="🎉 <b>Your withdrawal request has been approved and paid!</b>",
            parse_mode="HTML",
        )
    elif data.startswith("adm_rej_"):
        target_id = data.replace("adm_rej_", "")
        await query.answer("Payment Rejected!")
        await query.edit_message_text(
            f"{query.message.text}\n\n❌ <b>STATUS: REJECTED</b>",
            parse_mode="HTML",
        )
        await context.bot.send_message(
            chat_id=target_id,
            text="❌ <b>Your withdrawal request was rejected by admin.</b>",
            parse_mode="HTML",
        )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # 1. Start & Verification Handler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(handle_verification, pattern="^verify_user$")
    )

    # 2. Conversation Handler for Withdrawal Inputs
    withdraw_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                start_withdraw_process, pattern="^withdraw_"
            )
        ],
        states={
            WAIT_WITHDRAW_INFO: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_withdraw_details
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_withdraw)],
    )
    app.add_handler(withdraw_conv)

    # 3. Universal Callback Handler
    app.add_handler(CallbackQueryHandler(handle_callbacks))

    # 4. Main Menu Handler
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_options)
    )

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()