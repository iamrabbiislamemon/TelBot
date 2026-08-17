import logging
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
# CONFIGURATION & STORAGE
# ---------------------------------------------------------
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
ADMIN_CHAT_ID = 123456789  # Replace with your numerical Telegram User ID

# Conversation States
WAIT_WITHDRAW_INFO = 1

# Database Structures (In-Memory)
USER_BALANCES = {123456789: 500.0}  # Example initial balance
PENDING_WITHDRAWALS = {}  # {withdraw_id: {"user_id": int, "method": str, "account": str, "amount": float, "status": str}}

# Menu items for filter check
MENU_TEXTS = [
    "📧 NEW GMAIL SELL", "📧 OLD GMAIL SELL", "🎧 SUPPORT", 
    "👤 PROFILE", "💳 WITHDRAW", "👥 REFER", "💲 GMAIL PRICES"
]
MENU_FILTER = ~filters.Regex(f"^({'|'.join(MENU_TEXTS)})$")

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID

def get_balance(user_id: int) -> float:
    return USER_BALANCES.get(user_id, 0.0)

# ---------------------------------------------------------
# MANUAL WITHDRAWAL FLOW (USER SIDE)
# ---------------------------------------------------------
async def start_withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when user picks a payment method inline button (e.g. Bkash, Nagad)."""
    query = update.callback_query
    await query.answer()

    method = query.data.replace("withdraw_", "").upper()
    user_id = query.from_user.id
    balance = get_balance(user_id)

    if balance <= 0:
        await query.message.reply_text(
            f"❌ <b>Insufficient Balance!</b>\n"
            f"Your current balance is <b>{balance} Tk</b>. You cannot request a withdrawal.",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    context.user_data["withdraw_method"] = method

    await query.message.reply_text(
        f"💳 <b>Method Selected:</b> {method}\n"
        f"💰 <b>Available Balance:</b> {balance} Tk\n\n"
        f"📱 Please reply with your <b>{method} Account Number</b> and <b>Amount</b> separated by space.\n"
        "<i>Example: 01700000000 100</i>",
        parse_mode="HTML"
    )
    return WAIT_WITHDRAW_INFO


async def receive_withdraw_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    method = context.user_data.get("withdraw_method", "UNKNOWN")
    current_balance = get_balance(user.id)

    # Parse inputs: split by space to get number and amount
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "⚠️ <b>Invalid Format!</b>\n"
            "Please provide both account number and amount.\n"
            "<i>Example: 01700000000 100</i>",
            parse_mode="HTML"
        )
        return WAIT_WITHDRAW_INFO

    account_no = parts[0]
    try:
        amount = float(parts[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a valid number for the amount:")
        return WAIT_WITHDRAW_INFO

    # Validate against balance
    if amount <= 0:
        await update.message.reply_text("❌ Withdrawal amount must be greater than 0 Tk.")
        return WAIT_WITHDRAW_INFO

    if amount > current_balance:
        await update.message.reply_text(
            f"❌ <b>Insufficient Funds!</b>\n"
            f"Requested: {amount} Tk | Available: {current_balance} Tk\n"
            "Please enter a valid amount:",
            parse_mode="HTML"
        )
        return WAIT_WITHDRAW_INFO

    # Deduct balance immediately (reserve funds)
    USER_BALANCES[user.id] -= amount
    remaining_bal = USER_BALANCES[user.id]

    wdr_id = f"WDR_{user.id}_{len(PENDING_WITHDRAWALS) + 1}"
    PENDING_WITHDRAWALS[wdr_id] = {
        "user_id": user.id,
        "method": method,
        "account": account_no,
        "amount": amount,
        "status": "PENDING"
    }

    # 1. Notify User
    await update.message.reply_text(
        f"✅ <b>Withdrawal Request Received!</b>\n\n"
        f"💳 <b>Method:</b> {method}\n"
        f"📞 <b>Account:</b> {account_no}\n"
        f"💰 <b>Requested Amount:</b> {amount} Tk\n"
        f"💼 <b>Remaining Balance:</b> {remaining_bal} Tk\n\n"
        "⏳ An admin will review and send your money shortly.",
        parse_mode="HTML"
    )

    # 2. Forward Request to Admin Inbox
    admin_card = (
        "💸 <b>NEW MANUAL WITHDRAWAL REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User Name:</b> {user.first_name}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"💳 <b>Payment Method:</b> {method}\n"
        f"📱 <b>Payee Account:</b> <code>{account_no}</code>\n"
        f"💵 <b>Payout Amount:</b> {amount} Tk\n"
        f"💰 <b>Current Profile Balance:</b> {remaining_bal} Tk\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    admin_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Mark Paid & Complete", callback_data=f"adm_app_wdr_{wdr_id}"),
            InlineKeyboardButton("❌ Reject & Refund", callback_data=f"adm_rej_wdr_{wdr_id}")
        ]
    ])

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_card,
        parse_mode="HTML",
        reply_markup=admin_keyboard
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Withdrawal request canceled.")
    return ConversationHandler.END

# ---------------------------------------------------------
# ADMIN ACTIONS FOR WITHDRAWALS
# ---------------------------------------------------------
async def handle_admin_withdrawal_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Unauthorized.", show_alert=True)
        return

    data = query.data

    # Approve & Mark Paid
    if data.startswith("adm_app_wdr_"):
        wdr_id = data.replace("adm_app_wdr_", "")
        wdr = PENDING_WITHDRAWALS.get(wdr_id)

        if not wdr or wdr["status"] != "PENDING":
            await query.answer("This request has already been processed.")
            return

        wdr["status"] = "APPROVED"

        await query.edit_message_text(
            f"{query.message.text}\n\n✅ <b>STATUS: PAID & SENT ({wdr['amount']} Tk via {wdr['method']})</b>",
            parse_mode="HTML"
        )

        # Notify User
        await context.bot.send_message(
            chat_id=wdr["user_id"],
            text=(
                f"🎉 <b>Withdrawal Successful!</b>\n\n"
                f"💰 <b>Amount:</b> {wdr['amount']} Tk\n"
                f"💳 <b>Method:</b> {wdr['method']}\n"
                f"📱 <b>Account:</b> {wdr['account']}\n\n"
                f"Money has been sent by the admin. Thank you!"
            ),
            parse_mode="HTML"
        )
        await query.answer("Marked as paid!")

    # Reject & Refund Balance
    elif data.startswith("adm_rej_wdr_"):
        wdr_id = data.replace("adm_rej_wdr_", "")
        wdr = PENDING_WITHDRAWALS.get(wdr_id)

        if not wdr or wdr["status"] != "PENDING":
            await query.answer("This request has already been processed.")
            return

        wdr["status"] = "REJECTED"

        # Refund user's balance
        USER_BALANCES[wdr["user_id"]] += wdr["amount"]
        refunded_bal = USER_BALANCES[wdr["user_id"]]

        await query.edit_message_text(
            f"{query.message.text}\n\n❌ <b>STATUS: REJECTED & REFUNDED ({wdr['amount']} Tk)</b>",
            parse_mode="HTML"
        )

        # Notify User
        await context.bot.send_message(
            chat_id=wdr["user_id"],
            text=(
                f"❌ <b>Withdrawal Request Rejected</b>\n\n"
                f"Your request to withdraw <b>{wdr['amount']} Tk</b> via {wdr['method']} was rejected.\n"
                f"💰 The funds have been refunded to your profile balance.\n"
                f"👤 <b>Updated Balance:</b> {refunded_bal} Tk"
            ),
            parse_mode="HTML"
        )
        await query.answer("Rejected and funds refunded.")

# ---------------------------------------------------------
# BOT SETUP
# ---------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Withdrawal Handler
    withdraw_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_withdraw_callback, pattern="^withdraw_")
        ],
        states={
            WAIT_WITHDRAW_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & MENU_FILTER, receive_withdraw_details)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_withdraw),
            MessageHandler(~MENU_FILTER, cancel_withdraw)
        ],
    )

    app.add_handler(withdraw_conv)
    app.add_handler(CallbackQueryHandler(handle_admin_withdrawal_callbacks, pattern="^adm_(app|rej)_wdr_"))

    print("Bot startup completed...")
    app.run_polling()

if __name__ == "__main__":
    main()