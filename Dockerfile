# zcode-hub (dengyie/zcode2api) — account-operations console + gateway
# Python(FastAPI) gateway/admin + Node captcha solver (puppeteer-core + system Chromium)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ZCODE_HOST=0.0.0.0 \
    ZCODE_PORT=3000 \
    ZCODE_DATA_DIR=/data \
    ZCODE_NODE_PATH=node

WORKDIR /app

# Node.js (captcha solver) + Chromium (solver_pw.js launches a real browser;
# the happy-dom path in solver.js is rejected by upstream risk control)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl ca-certificates gnupg chromium \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

# package-lock.json is out of sync with package.json upstream -> use install, not ci
COPY captcha_node/package.json ./captcha_node/
RUN cd captcha_node && npm install --omit=dev

COPY . .

VOLUME ["/data"]
EXPOSE 3000
CMD ["python", "cli.py", "serve"]
