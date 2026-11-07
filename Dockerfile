FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GRAVYBOAT_DATA=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY docker-entrypoint.sh ./

# Runs as an unprivileged user. Override with `user:` in compose (e.g. "99:100" on Unraid)
# so files in the data volume get the owner you want.
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin gravyboat \
    && mkdir -p /data && chown 1000:1000 /data
VOLUME /data
EXPOSE 8794
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8794/healthz', timeout=4).status == 200 else 1)"
USER 1000:1000
CMD ["./docker-entrypoint.sh"]
