"""
modules/web_server.py — Asynchronous web routing daemon for Koyeb health sweeps.
"""
from aiohttp import web

async def home_route_handler(request):
    # This renders an active message right on the screen if you visit your Koyeb App URL
    return web.Response(
        text="✨ SDSS Spatial Decision Support Engine is Natively Active (MTProto) ✨",
        content_type="text/plain"
    )

async def web_server():
    app = web.Application()
    app.router.add_ready_to_work = True
    app.router.add_get("/", home_route_handler)
    app.router.add_get("/health", home_route_handler)
    return app
