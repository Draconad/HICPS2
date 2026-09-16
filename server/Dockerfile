FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/data/monitor.db PORT=8420
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY fetch_player.py .
RUN python fetch_player.py
VOLUME /data
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:8420/api/health'+('?key='+os.environ['API_KEY'] if os.environ.get('API_KEY') else ''))" || exit 1
CMD ["python", "-u", "app/server.py"]
