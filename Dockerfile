FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 botuser

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser

VOLUME ["/app/data"]
CMD ["python", "-m", "bottom_post_bot"]
