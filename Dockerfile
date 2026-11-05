# ================================
# Application
# ================================
FROM python:3.11-slim

# Disable output buffering so logs stream to the console immediately
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy the dependency list first so this layer caches across code changes
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium — required for widget rendering (ENABLE_HTML_RENDERER=true)
# Adds ~300MB. Only Chromium is installed (not Firefox/WebKit). Stays right after pip: it needs the playwright package,
# whose version is whatever requirements.txt resolves (one source of truth, no second pin in this file).
RUN python -m playwright install chromium --with-deps

COPY . .

CMD ["python", "main.py"]
