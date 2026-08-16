FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e ".[web]"
EXPOSE 8000
CMD ["sh", "-c", "mkdir -p .secrets && printf '%s' \"$A2T_USER_PASSWORD\" > .secrets/user-password && printf '%s' \"$A2T_ADMIN_PASSWORD\" > .secrets/admin-password && A2T_WEB_PORT=\"${A2T_WEB_PORT:-$PORT}\" a2t-web"]