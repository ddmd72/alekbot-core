# ================================
# Application
# ================================
FROM python:3.11-slim

# Вимикаємо буферизацію (щоб логи лилися в консоль одразу)
ENV PYTHONUNBUFFERED=1

# Робоча папка всередині контейнера
WORKDIR /app

# ffmpeg — required by pydub for audio format conversion (mp3, m4a, ogg)
# nodejs — required by DocGeneratorAgent to execute LLM-generated docx npm scripts
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Спочатку копіюємо тільки список бібліотек (для кешування)
COPY requirements.txt .

# Встановлюємо бібліотеки
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium — required for widget rendering (ENABLE_HTML_RENDERER=true)
# Adds ~300MB. Only Chromium is installed (not Firefox/WebKit).
RUN python -m playwright install chromium --with-deps

# Install docx npm library for LLM-generated DOCX scripts
COPY docx_generator/package.json docx_generator/
RUN cd docx_generator && npm install --omit=dev

# Install puppeteer for PDF generation.
# Downloads bundled Chromium (~170MB) — required for PDF rendering in Cloud Run.
COPY pdf_generator/package.json pdf_generator/
RUN cd pdf_generator && npm install --omit=dev

# Копіюємо решту коду
COPY . .

# Запускаємо бота
CMD ["python", "main.py"]
