"""FastAPI application entrypoint for the supplement label scanner backend."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as scan_router

app = FastAPI(
    title="Supplement Label Scanner API",
    version="0.1.0",
)

# Local development origins used by Expo / React Native tooling.
# - 8081: Metro bundler (default) and Expo web dev server on newer SDKs
# - 19000-19002: Expo Dev Tools / legacy web preview ports
# - 19006: Expo web preview (older SDKs)
LOCAL_DEV_ORIGINS: list[str] = [
    "http://localhost:8081",
    "http://127.0.0.1:8081",
    "http://localhost:19000",
    "http://localhost:19001",
    "http://localhost:19002",
    "http://localhost:19006",
    "http://127.0.0.1:19006",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_DEV_ORIGINS,
    # Also allow LAN addresses (e.g. http://192.168.x.x:8081) so the app
    # works when previewed via Expo Go / web on a physical device on the
    # same network as the dev machine.
    allow_origin_regex=r"^http://(192\.168|10\.\d{1,3}|172\.(1[6-9]|2\d|3[0-1]))\.\d{1,3}\.\d{1,3}(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scan_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Basic liveness check, useful for confirming the server is reachable."""
    return {"status": "ok"}
