# Diagnosticador de automatización (LangGraph + FastAPI)

Bot que entrevista a un cliente, releva 13 datos clave del proceso a automatizar y genera:
- un diagnóstico preliminar para el cliente (se muestra en el chat)
- un informe interno + score de lead (se envía por POST a un webhook de n8n)

## Local
    python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    cp .env.example .env   # completar claves
    export $(grep -v '^#' .env | xargs)   # Windows: cargar variables a mano o usar python-dotenv
    uvicorn app.main:app --reload
    # abrir http://localhost:8000

## Deploy en Render (free)
1. Subir esta carpeta a un repo de GitHub.
2. Render → New → Blueprint (usa render.yaml) o Web Service manual:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Cargar variables de entorno (ver .env.example): como mínimo la API key y MODEL.
4. Deploy y abrir la URL pública.

## Payload que recibe n8n (POST JSON)
thread_id, datos{...13 campos}, resumen_cliente, informe_interno, score_viabilidad,
complejidad, herramienta_recomendada, ahorro_horas_mes, calidad_lead

## Personalización
- Preguntas/campos: lista FIELDS en app/graph.py
- Criterios de recomendación y formato del informe: REPORT_SYSTEM en app/graph.py
