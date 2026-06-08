"""
main.py — SDSS Telegram Bot Orchestrator Engine (Pyrogram MTProto Edition)

Main orchestrator file responsible for handling thread boot routines and
maintaining the Koyeb web-service health container checks cleanly.
"""

import logging
import threading
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

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

# Reduce verbose session network noise from internal Pyrogram operations
logging.getLogger("pyrogram").setLevel(logging.WARNING)


# ── Initialize Pyrogram App Client ───────────────────────────────────────────
app = Client(
    name="sdss_core_bot",
    api_id=cfg.API_ID,          # 👈 Make sure these are declared in config / env vars
    api_hash=cfg.API_HASH,      # 👈 Generated from my.telegram.org
    bot_token=cfg.TELEGRAM_TOKEN
)


# ── Register Core Pyrogram Command & Message Decoders ────────────────────────
@app.on_message(filters.command("start"))
async def route_start(client: Client, message: Message):
    await cmd_start(client, message)

@app.on_message(filters.command("help"))
async def route_help(client: Client, message: Message):
    await cmd_help(client, message)

@app.on_message(filters.command("history"))
async def route_history(client: Client, message: Message):
    await cmd_history(client, message)

@app.on_message(filters.command("status"))
async def route_status(client: Client, message: Message):
    await cmd_status(client, message)

@app.on_message(filters.command("upload_master"))
async def route_upload_master(client: Client, message: Message):
    await cmd_upload_master(client, message)

# Handle inline keyboard interactive click updates
@app.on_callback_query()
async def route_callback_query(client: Client, callback_query: CallbackQuery):
    await handle_button_click(client, callback_query)

# Route incoming shapefiles, geopackages, or tracking vectors
@app.on_message(filters.document)
async def route_document(client: Client, message: Message):
    await handle_document(client, message)

# Default routing layout for unhandled text prompts
@app.on_message(filters.text & ~filters.command)
async def route_unknown(client: Client, message: Message):
    await handle_unknown(client, message)


# ── Koyeb Infrastructure Port Binder ──────────────────────────────────────────
def run_koyeb_health_server():
    """
    Spins up a lightweight HTTP daemon thread on port 8080 to satisfy
    Koyeb's health infrastructure loops, keeping the server instance alive.
    """
    class HealthHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ('/', '/health'):
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"SDSS Core Engine Active (MTProto)")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            return  # Suppress health check spam lines inside server logs

    port = 8080
    try:
        TCPServer.allow_reuse_address = True
        with TCPServer(("", port), HealthHandler) as httpd:
            log.info("🌍 Koyeb Background Health Server permanently active on port %d", port)
            httpd.serve_forever()
    except Exception as e:
        log.error("❌ Failed to start health server: %s", e)


# ── Application Bootstrap Run ─────────────────────────────────────────────────
def main() -> None:
    # 1. Fire daemon health listener thread up immediately
    health_thread = threading.Thread(target=run_koyeb_health_server, daemon=True)
    health_thread.start()

    log.info("Starting SDSS Telegram Bot Orchestrator Engine via Pyrogram...")
    
    # 2. Boot the native Pyrogram server polling loop environment
    app.run()


if __name__ == "__main__":
    main()

