"""FastAPI minimo para que docker compose levante el servicio app.

Solo expone /health. Las rutas reales viven en backend/api/ y se
montan en una tarjeta posterior de Fase 2.
"""

from fastapi import FastAPI

app = FastAPI(title="crypto-futures-gpt55-bot", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
