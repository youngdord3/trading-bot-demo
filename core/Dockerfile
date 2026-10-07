FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app

COPY --chown=app:app app.py common.py binance_public.py binance_private.py strategy.py ./
COPY --chown=app:app check_binance_public.py check_binance_account.py check_reconciler_db.py reconciler.py ./

USER app

EXPOSE 8000

CMD ["python", "app.py"]
