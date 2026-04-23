# ============================================================
# Dockerfile — Consórcio Sorteado
# Imagem de produção para Railway
# ============================================================

FROM python:3.12-slim

# Metadados
LABEL maintainer="Guará Lab"
LABEL description="Consórcio Sorteado — Servidor de Automação"

# Variáveis de build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependências do sistema para o Chromium headless (Playwright)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Instala dependências Python primeiro (camada cacheada — só rebuilda se requirements.txt mudar)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

# Copia o código-fonte
COPY . .

# Remove arquivos desnecessários em produção
RUN find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
RUN find . -name "*.pyc" -delete 2>/dev/null || true
RUN rm -rf tests/

# Railway injeta $PORT automaticamente
EXPOSE 8000

# Usa sh -c para expandir $PORT em runtime
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level info"]
