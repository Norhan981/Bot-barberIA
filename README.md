# Bot de atención para barberías 💈

Asistente virtual para Telegram que responde preguntas frecuentes
(servicios, precios, horarios) y agenda citas usando IA.

## Tecnologías
- Python + FastAPI
- API de Telegram (webhooks)
- Google Gemini (con modelo de respaldo)

## Cómo ejecutarlo
1. Crear entorno virtual e instalar dependencias: `pip install -r requirements.txt`
2. Crear un archivo `.env` con `TELEGRAM_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL` y `GEMINI_MODEL_RESPALDO`
3. Ejecutar: `uvicorn main:app --reload`