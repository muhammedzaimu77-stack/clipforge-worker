FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl supervisor && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY worker/ ./
RUN mkdir -p /data
EXPOSE 8000
COPY deploy/supervisord.conf /etc/supervisor/conf.d/clipforge.conf
CMD ["/usr/bin/supervisord","-n","-c","/etc/supervisor/supervisord.conf"]
