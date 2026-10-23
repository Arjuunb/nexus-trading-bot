FROM python:3.12-alpine
# wheels/: pip download --no-deps --only-binary=:all: -d wheels uvicorn h11 click
COPY wheels /wheels
RUN pip install --no-index --no-cache-dir /wheels/*.whl && rm -rf /wheels \
 && addgroup -S -g 101 tradexa && adduser -S -D -H -u 100 -G tradexa -h /nonexistent tradexa \
 && mkdir -p /var/lib/tradexa && chown tradexa:tradexa /var/lib/tradexa
WORKDIR /app
COPY app.py /app/app.py
USER tradexa
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*"]
