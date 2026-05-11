# =====================================================================
# Imagen compartida entre el servicio app (FastAPI) y el worker.
# El comando concreto se define por servicio en docker-compose.yml.
# =====================================================================
FROM python:3.12-slim

# curl es necesario para los healthchecks definidos en docker-compose.yml.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Usuario no-root para correr la aplicacion (defense in depth).
RUN groupadd --system --gid 1001 botgroup \
    && useradd --system --uid 1001 --gid botgroup --no-create-home --shell /bin/false botuser

WORKDIR /app

# README.md va incluido porque pyproject.toml lo referencia como readme.
COPY pyproject.toml README.md config.yaml ./
COPY backend ./backend
COPY worker ./worker

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER botuser

EXPOSE 8000

# Default: app. El servicio worker lo override en docker-compose.yml.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
