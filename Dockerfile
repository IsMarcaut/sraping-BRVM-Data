FROM python:3.12-slim-bookworm

# ============================================================
# VARIABLES PYTHON
# ============================================================

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Chromium / Selenium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver


# ============================================================
# DOSSIER DE TRAVAIL
# ============================================================

WORKDIR /app


# ============================================================
# INSTALLATION DE CHROMIUM + CHROMEDRIVER
# ============================================================

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        ca-certificates \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*


# ============================================================
# DÉPENDANCES PYTHON
# ============================================================

COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt


# ============================================================
# COPIE DU PROJET
# ============================================================

COPY . /app


# ============================================================
# UTILISATEUR NON ROOT
# ============================================================

RUN groupadd -g 1000 rendersecrets \
    && useradd --create-home --uid 10001 --groups 1000 appuser \
    && chown -R appuser:appuser /app

USER appuser


# ============================================================
# PORT RENDER
# ============================================================

EXPOSE 10000


# ============================================================
# LANCEMENT
# ============================================================

CMD ["python", "scraping_brvm_render.py"]