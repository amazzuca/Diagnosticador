import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from .graph import MODEL, build_graph, progress

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("diagnosticador")

graph = build_graph()

app = FastAPI(title="Diagnosticador de automatización")

# Para embeber el chat en otro sitio, listá los dominios separados por coma.
origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# --- Rate limit simple por IP (en memoria) para proteger el gasto de LLM ---
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "60"))
_hits: dict[str, deque] = defaultdict(deque)


def _limited(ip: str) -> bool:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


class ChatIn(BaseModel):
    thread_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    # Id idempotente: si el cliente reintenta el mismo mensaje, LangGraph lo deduplica.
    message_id: Optional[str] = Field(default=None, max_length=64)


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


@app.post("/chat")
def chat(body: ChatIn, request: Request):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()) or request.client.host
    if _limited(ip):
        raise HTTPException(429, "Demasiadas solicitudes. Probá más tarde.")
    config = {"configurable": {"thread_id": body.thread_id}}
    try:
        out = graph.invoke(
            {"messages": [HumanMessage(content=body.message, id=body.message_id)]},
            config,
        )
    except Exception as exc:
        # El traceback completo queda en los logs de Render.
        logger.exception("Error en /chat thread=%s", body.thread_id)
        raise HTTPException(502, f"Error interno ({type(exc).__name__}). Revisá los logs del servidor.")
    return {
        "reply": _text(out["messages"][-1].content),
        "progress": progress(out),
        "done": bool(out.get("done")),
    }


@app.get("/health")
def health():
    """Chequeo de estado: no expone secretos, solo si están configurados."""
    return {
        "status": "ok",
        "model": MODEL,
        "api_key_set": bool(os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")),
        "checkpointer": "postgres" if os.getenv("DATABASE_URL") else "memory",
        "n8n_webhook_set": bool(os.getenv("N8N_WEBHOOK_URL")),
        "n8n_token_set": bool(os.getenv("N8N_WEBHOOK_TOKEN")),
        "n8n_url_is_test": "/webhook-test/" in os.getenv("N8N_WEBHOOK_URL", ""),
    }


STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
