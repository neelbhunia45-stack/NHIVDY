# Telegram Bot Hosting Manager

## What it does
- Telegram-based manager bot
- Upload `.py` or `.zip` bot projects
- Start / stop / restart hosted bots
- Per-bot logs
- Admin panel, user list, ban/unban, bot list
- SQLite database
- Each hosted project gets its own Python virtual environment when started

## Requirements
- Python 3.10+
- A host that allows long-running background processes, subprocesses, disk writes, and outbound network access.
- This is not suitable for a serverless function or a sleeping free web service.

## Setup
1. Create a manager bot with `@BotFather`.
2. Get your Telegram numeric user ID (send `/id` to a bot that provides it, or use a Telegram ID lookup bot).
3. Install dependencies:

   `pip install -r requirements.txt`

4. Set environment variables:

   - `MANAGER_BOT_TOKEN` = token of the manager bot
   - `ADMIN_ID` = your numeric Telegram user ID
   - Optional: `MAX_UPLOAD_MB` (default 20)
   - Optional: `MAX_BOTS_PER_USER` (default 3)
   - Optional: `HOSTING_DATA_DIR` (default `hosting_data`)

5. Start:

   `python main.py`

## Hosted bot format
- `.py`: upload a single Python file.
- `.zip`: include `main.py` (or `bot.py`, `app.py`, `run.py`) at the project root and optionally `requirements.txt`.
- The hosted bot can read its token from:
  `os.getenv("BOT_TOKEN")` or `os.getenv("TELEGRAM_BOT_TOKEN")` or `os.getenv("TOKEN")`.

Example:
```python
import os
import telebot

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Your bot is online!")

bot.infinity_polling()
```

## Admin commands
- `/users` - latest users
- `/allbots` - latest hosted bots
- `/ban USER_ID`
- `/unban USER_ID`

## Important security and hosting limitations
1. Uploaded Python files execute as operating-system processes. A malicious upload can read files, environment variables, database contents, and potentially compromise the host. **Do not offer public multi-user hosting on a shared host without real isolation.** Use one isolated container/VM per customer, restricted filesystem/network, CPU/RAM/PID quotas, and a non-root account.
2. This starter project does not provide container isolation, resource quotas, billing, or automatic abuse detection. It is a learning/MVP project, not a production public hosting platform.
3. Tokens are stored in SQLite in plaintext in this starter version. Keep the host private, protect the data directory and backups, and use a secrets manager/encryption before production.
4. Free hosting providers may sleep, restart, limit CPU/RAM, or prohibit background workers. No free provider can be assumed to guarantee 24/7 uptime. Check the provider's current terms.
5. Do not hardcode your manager token in source code. Use environment variables.
6. If a Telegram bot token was ever posted publicly or shared in a chat, revoke/regenerate it with `@BotFather`.

## Troubleshooting
- `Conflict: terminated by other getUpdates request`: only one manager instance should poll with this token.
- Hosted bot exits: open its Logs button and check missing packages, token, or Python errors.
- ZIP upload fails: make sure `main.py` is at the ZIP root (not nested inside another folder).
