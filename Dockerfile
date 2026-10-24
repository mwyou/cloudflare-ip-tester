FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS cfst
ARG TARGETARCH
ENV TARGETARCH=$TARGETARCH
COPY docker/fetch_cfst.py /build/docker/fetch_cfst.py
COPY app/cfst_manager.py /build/cfst_manager.py
RUN python /build/docker/fetch_cfst.py

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data CFST_HOME=/opt/cfst
WORKDIR /app
RUN groupadd --gid 10001 cfip && useradd --uid 10001 --gid 10001 --no-create-home cfip \
    && mkdir /data /opt/cfst && chown 10001:10001 /data /opt/cfst
COPY --from=cfst /out/ /usr/local/lib/cfst/
COPY app/ /app/
COPY LICENSE-CFST THIRD_PARTY.md /app/
USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=3).read()"]
ENTRYPOINT ["python", "/app/cfst_manager.py"]
