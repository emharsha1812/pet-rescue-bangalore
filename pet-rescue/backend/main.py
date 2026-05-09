import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from backend.agent import run_agent
from backend.es_client import get_es

_SCRAPER_INTERVAL_HOURS = 6


async def _scraper_loop() -> None:
    """Scrape all sites then structure raw listings every 6 hours.
    First run fires immediately at startup."""
    from ingest.scraper import run_all_sites
    from ingest.structurer import run as run_structurer

    while True:
        try:
            logger.info("periodic pipeline: scraping all sites")
            totals = await asyncio.to_thread(run_all_sites)
            logger.info("periodic pipeline: scrape done — %s", totals)
        except Exception:
            logger.error("periodic pipeline: scrape error\n%s", traceback.format_exc())

        try:
            logger.info("periodic pipeline: structuring raw listings")
            await asyncio.to_thread(run_structurer, 50)
            logger.info("periodic pipeline: structuring done")
        except Exception:
            logger.error("periodic pipeline: structurer error\n%s", traceback.format_exc())

        await asyncio.sleep(_SCRAPER_INTERVAL_HOURS * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_scraper_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Pet Rescue Coordinator", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class UserLocation(BaseModel):
    lat: float
    lon: float
    place_name: str
    source: str = "manual"


class ChatRequest(BaseModel):
    message: str
    session_id: str
    user_location: UserLocation | None = None


class ChatResponse(BaseModel):
    reply: str
    structured_results: dict


@app.get("/health")
def health():
    es = get_es()
    counts = {}
    for index in ("animals", "vets", "rescuers", "protocols", "raw_listings"):
        try:
            counts[index] = es.count(index=index)["count"]
        except Exception:
            counts[index] = None
    return {"status": "ok", "indices": counts}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    try:
        ul = req.user_location.model_dump() if req.user_location else None
        if ul:
            logger.info("user_location in /chat: %s", ul)
        reply, structured_results = run_agent(
            message=req.message,
            session_id=req.session_id,
            channel="web",
            user_location=ul,
        )
        return ChatResponse(reply=reply, structured_results=structured_results)
    except Exception:
        logger.error("Unhandled error in /chat:\n%s", traceback.format_exc())
        return ChatResponse(
            reply="Something went wrong — please try again.",
            structured_results={"animals": [], "vets": [], "rescuers": [], "protocols": []},
        )


@app.post("/whatsapp")
async def whatsapp(
    From: str = Form(...),
    Body: str = Form(...),
    WaId: str = Form(...),
):
    try:
        reply, _ = run_agent(message=Body, session_id=WaId, channel="whatsapp")
    except Exception:
        logger.error("Unhandled error in /whatsapp:\n%s", traceback.format_exc())
        reply = "Something went wrong — please try again."

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Message>{escape(reply)}</Message>"
        "</Response>"
    )
    return Response(content=twiml, media_type="application/xml")
