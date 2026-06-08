"""
modules/web_server.py — Asynchronous web routing daemon for Koyeb health sweeps.
Handles keeping the application context active across MTProto intervals.
"""

from aiohttp import web

# Create the automated routing table definition layer
routes = web.RouteTableDef()

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    """
    Handles primary root ping sweeps. Returns a compact JSON footprint
    to minimize overhead during high-frequency health checks.
    """
    return web.json_response("SDSS ENGINE ACTIVE")


@routes.get("/health", allow_head=True)
async def health_route_handler(request):
    """
    Explicit fallback target for specialized container lifecycle monitors.
    """
    return web.Response(
        text="✨ SDSS Spatial Decision Support Engine is Natively Active (MTProto) ✨",
        content_type="text/plain"
    )


async def web_server():
    """
    Initializes the unified aiohttp application worker mapping the table routes.
    """
    app = web.Application()
    
    # Register the decorated routes table into the application instance router
    app.add_routes(routes)
    
    return app
    
