FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS cfst
ARG TARGETARCH
ENV TARGETARCH=$TARGETARCH
COPY docker/fetch_cfst.py /build/docker/fetch_cfst.py
COPY app/cfst_bundle.py /build/cfst_bundle.py
RUN python /build/docker/fetch_cfst.py

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app
RUN groupadd --gid 10001 cfip && useradd --uid 10001 --gid 10001 --no-create-home cfip \
    && mkdir /data && chown 10001:10001 /data
COPY --from=cfst /out/ /usr/local/lib/cfst/
COPY app/ /app/
COPY LICENSE-CFST THIRD_PARTY.md /app/
COPY docker/entrypoint.py /usr/local/bin/cfip-entrypoint.py
EXPOSE 36963
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:36963/health',timeout=3).read()"]
ENTRYPOINT ["python", "/usr/local/bin/cfip-entrypoint.py"]
