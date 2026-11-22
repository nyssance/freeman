"""Multiple APIs with Django middleware on the documentation owner."""

from freeman import BoltAPI
from freeman.openapi import OpenAPIConfig, SwaggerRenderPlugin

api = BoltAPI(
    django_middleware=["debug_toolbar.middleware.DebugToolbarMiddleware"],
    openapi_config=OpenAPIConfig(title="Merged docs", version="1", render_plugins=[SwaggerRenderPlugin()]),
)


@api.get("/health")
async def health():
    return {"ok": True}


other_api = BoltAPI()


@other_api.get("/other/health")
async def other_health():
    return {"ok": True}
