FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

COPY --from=ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /usr/local/bin/uv
COPY dist/*.whl /tmp/wheels/
COPY dist/requirements.txt /tmp/wheels/
RUN uv pip install --system --no-cache --constraint /tmp/wheels/requirements.txt /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && install -d -o 10001 -g 10001 /data /home/matrix-mcp

LABEL org.opencontainers.image.source="https://github.com/mindroom-ai/matrix-mcp"
ENV HOME=/home/matrix-mcp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /data
EXPOSE 8000
ENTRYPOINT ["matrix-mcp"]
CMD ["--help"]
