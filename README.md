# Relevador de procesos (LangGraph + FastAPI)

Bot que **detecta** procesos automatizables: entrevista al usuario (modo DEEP o Rápido), lee documentos
que suba, y entrega (1) un resumen breve al usuario y (2) un informe oculto para el consultor.

## Estructura
- `app/prompts/relevador.md`  → tu especificación (editable sin tocar código). Placeholders: `[NOMBRE DEL BOT]`, `[EMPRESA]`, `{{APRENDIZAJES_APROBADOS}}`.
- `app/prompts/aprendizajes.md` → aprendizajes aprobados "a mano" (opcional).
- `app/prompt.py`  → arma el system prompt + formato técnico del sistema.
- `app/graph.py`   → grafo LangGraph, parseo de `<estado>` / botones `[[...]]`, blindaje de salida, informe del consultor.
- `app/store.py`   → Postgres opcional: tablas `relevamientos` y `aprendizajes`.
- `app/extract.py` → lectura de PDF, Word, Excel, CSV y TXT.
- `app/main.py`    → API: `/chat`, `/upload`, `/health`, `/`.

## Variables de entorno
MODEL, OPENAI_API_KEY / ANTHROPIC_API_KEY, AGENCY_NAME, BRAND_LOGO, MAX_TURNS, RATE_LIMIT_PER_HOUR,
N8N_WEBHOOK_URL, N8N_WEBHOOK_TOKEN, DATABASE_URL, ALLOWED_ORIGINS. Opcional: BOT_NAME.

## Eventos que recibe n8n (POST JSON, header X-Auth-Token; clave de upsert: `thread_id`)
- `contacto_capturado`: apenas el usuario deja mail o teléfono (email, telefono, persona, estado).
- `informe_consultor`: al cerrar (version, email, telefono, persona, estado, informe_consultor, mejoras_propuestas[]).
- `informe_fallido`: si no se pudo generar el informe (trae el estado para no perder el relevamiento).

## Automejora (aprobación del consultor)
Con `DATABASE_URL`, las mejoras propuestas se guardan en `aprendizajes` con `status = 'pendiente'`.
Para aprobarlas: Supabase → Table Editor → `aprendizajes` → cambiar `status` a `aprobada`.
En ≤ 60 s el bot las incorpora en `{{APRENDIZAJES_APROBADOS}}`. Para descartarlas: `rechazada`.

## Pruebas (LLM simulado, sin gastar tokens)
    pip install pytest reportlab && pytest -q
