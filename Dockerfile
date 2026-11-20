FROM python:3.13-slim

# tzdata: zoneinfo needs it for the rate plan's time zone; slim images ship without system zone files
RUN pip install --no-cache-dir tzdata==2025.2 && useradd --create-home app && mkdir /data && chown app /data

WORKDIR /app
COPY ecoflow_api.py planner.py db.py server.py index.html demo_quota.json ./
COPY rates ./rates

USER app
ENV HOST=0.0.0.0 PORT=8787 DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"

CMD ["python", "server.py"]
