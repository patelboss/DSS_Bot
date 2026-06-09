"""
main.py — SDSS Telegram Bot Orchestrator Engine (Pyrogram Object Oriented)

Handles dynamic automated folder plugin registration and native 
asynchronous web server tracking initialization on port 8080.
"""

import logging
import sys
from pyrogram import Client, __version__
from aiohttp import web

from config import cfg
from modules.web_server import web_server

# ── Unified Core Logging Configuration ────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout
)
log = logging.getLogger("main.orchestrator")

# Mute verbose networking data noise from dependencies
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("aiohttp.access").setLevel(logging.ERROR)


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="sdss_core_session",
            api_id=cfg.API_ID,
            api_hash=cfg.API_HASH,
            bot_token=cfg.TELEGRAM_TOKEN,
            workers=5,
            plugins={"root": "modules"}, # 🚀 AUTOMATION: Auto-registers all files in modules/
            sleep_threshold=10,
        )

    async def start(self):
        # Boot up the primary Telegram MTProto long-polling driver link
        await super().start()
        
        me = await self.get_me()
        log.info(f"🤖 Connected successfully to MTProto layers! Bot Client Named: {me.first_name} (@{me.username})")

        # 🚀 WEB SERVER APPARATUS: Wires web dashboard natively into the core runtime loop
        log.info("🌍 Initializing asynchronous web engine interface router on port 8080...")
        app_runner = web.AppRunner(await web_server())
        await app_runner.setup()
        
        # Bind to absolute universal listening ports matching Koyeb criteria layouts
        bind_target_site = web.TCPSite(app_runner, "0.0.0.0", 8080)
        await bind_target_site.start()
        
        log.info("🔥 Core systems stabilized. Spatial indexing ingestion layers active.")

    async def stop(self, *args):
        await super().stop()
        log.info("🛑 Bot Engine shut down cleanly. Goodbye.")


if __name__ == "__main__":
    # Create the shared app runtime agent object instance and run the execution stack
    app = Bot()
    app.run()
    
