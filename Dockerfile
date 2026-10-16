# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app
ENV PANEL_OPEN_BROWSER=false PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ARG INSTALL_BROWSER=true

COPY pyproject.toml ./
COPY glmweb2api/ ./glmweb2api/
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
      pip install --no-cache-dir ".[browser]" && python -m playwright install --with-deps chromium; \
    else pip install --no-cache-dir .; fi \
    && useradd --create-home --uid 10001 glm \
    && mkdir -p /app/data && chown -R glm:glm /app/data

USER glm
EXPOSE 8788
CMD ["python", "-m", "glmweb2api"]
