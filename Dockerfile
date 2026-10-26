FROM python:3.12-slim
WORKDIR /app
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.lock requirements.lock
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.lock
COPY . .
RUN chmod +x /app/entrypoint.sh
ENV TZ=Asia/Shanghai
EXPOSE 5000
CMD ["/app/entrypoint.sh"]
