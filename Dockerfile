FROM python:3.12-slim

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=5000 \
    DATA_DIR=/app/data \
    COOKIE_FILE=/app/data/cookie.txt

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.lock
COPY . .
RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown -R app:app /app/data

USER app
VOLUME ["/app/data"]
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '5000'), timeout=4)"
CMD ["python", "main.py"]
