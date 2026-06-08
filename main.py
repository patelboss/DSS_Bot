"""
main.py — SDSS Telegram Bot Starter (python-telegram-bot v21, async)

Main orchestrator file responsible for handling thread boot routines and
maintaining the Koyeb web-service health container checks cleanly.
"""

import logging
import threading
import traceback
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes
)

from config import cfg
from modules.cmd import (
    cmd_start, 
    cmd_help, 
    cmd_history, 
    cmd_status,
    handle_document, 
    handle_button_click, 
    handle_unknown
)
from modules.ingestion import cmd_upload_master

# ── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Global Error Capture Callback System ──────────────────────────────────────
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs runtime exceptions with tracebacks and reports updates cleanly."""
    log.error("💥 Critical Unhandled Exception caught by SDSS Hooks:", exc_info=context.error)
    
    # Extract traceback text to inspect inside server logs
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    log.error(f"Full traceback summary:\n{tb_string}")

    # Notify user if the crash happened during an active conversation update hook
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "❌ *A critical pipeline extraction error occurred.*\n\n"
            "Please check the server logs for the full traceback.",
            parse_mode=ParseMode.MARKDOWN
        )


# ── Koyeb Infrastructure Port Binder ──────────────────────────────────────────
def run_koyeb_health_server():
    """
    Spins up a lightweight HTTP daemon thread on port 8080 to satisfy
    Koyeb's health infrastructure loops, keeping the server instance alive.
    """
    class HealthHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self.end_headers()

    port = 8080
    try:
        TCPServer.allow_reuse_address = True
        with TCPServer(("", port), HealthHandler) as httpd:
            log.info("🌍 Koyeb Background Health Server active on port %d", port)
            httpd.serve_forever()
    except Exception as e:
        log.error("❌ Failed to start health server: %s", e)


# ── Application Bootstrap Setup ───────────────────────────────────────────────
def build_application() -> Application:
    """Builds and wires up the unified Application environment mapping context."""
    app = Application.builder().token(cfg.TELEGRAM_TOKEN).build()

    # Register the Global Error Handler
    app.add_error_handler(global_error_handler)

    # Core Command Routes
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("status",  cmd_status))
    
    # 🚀 FIXED: Registered the master ingestion command router explicitly
    app.add_handler(CommandHandler("upload_master", cmd_upload_master))

    # Inline Keyboard Interaction Query Route
    app.add_handler(CallbackQueryHandler(handle_button_click))

    # Structural Document Upload & Fallback Streams
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    return app


def main() -> None:
    # 1. Fire daemon health listener thread up immediately
    health_thread = threading.Thread(target=run_koyeb_health_server, daemon=True)
    health_thread.start()

    log.info("Starting SDSS Telegram Bot Orchestrator Engine…")
    
    # 2. Start the unified Telegram client instance polling loop
    app = build_application()
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"], # Capture inline keyboard clicks
    )


if __name__ == "__main__":
    main()
    
