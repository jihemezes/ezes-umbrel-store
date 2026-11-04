FROM python:3.13-slim

LABEL org.opencontainers.image.title="GitHub Downloads" \
      org.opencontainers.image.description="Statistiques de téléchargement des assets de releases GitHub" \
      org.opencontainers.image.source="https://github.com/jihemezes/ezes-umbrel-store"

ENV PYTHONUNBUFFERED=1 \
    GHDL_DATA=/data \
    PORT=8080

WORKDIR /app
COPY ghdl.py index.html ./

RUN mkdir -p /data
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)" || exit 1

CMD ["python", "ghdl.py", "serve"]
