FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e ".[web]" && python -c "import a2t_web.app"
EXPOSE 8000
CMD ["sh", "-c", "A2T_WEB_PORT=\"${A2T_WEB_PORT:-${PORT:-5000}}\" python -m a2t_web.app"]