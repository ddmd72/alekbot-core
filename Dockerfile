# ================================
# Application
# ================================
FROM python:3.11-slim

# Disable output buffering so logs stream to the console immediately
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg — required by pydub for audio format conversion (mp3, m4a, ogg)
# nodejs — required by DocGeneratorAgent to execute LLM-generated docx npm scripts
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Layer order: everything that does not depend on the Python packages comes first (apt, the two npm installs), so a
# requirements.txt change rebuilds only pip and Playwright Chromium, not Node and puppeteer's Chromium.

# Install docx npm library for LLM-generated DOCX scripts
COPY docx_generator/package.json docx_generator/
RUN cd docx_generator && npm install --omit=dev

# Install puppeteer for PDF generation.
# Downloads bundled Chromium (~170MB) — required for PDF rendering in Cloud Run.
COPY pdf_generator/package.json pdf_generator/
RUN cd pdf_generator && npm install --omit=dev

# Copy the dependency list first so this layer caches across code changes
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium — required for widget rendering (ENABLE_HTML_RENDERER=true)
# Adds ~300MB. Only Chromium is installed (not Firefox/WebKit). Stays right after pip: it needs the playwright package,
# whose version is whatever requirements.txt resolves (one source of truth, no second pin in this file).
RUN python -m playwright install chromium --with-deps

# The drive indexer's parser child runs as this unprivileged user (R3 F3-1, USER_DRIVE_INDEXING_RFC §9): another
# uid cannot read the indexer's /proc/<pid>/environ (its API keys) or rewrite the app source it loads. Only the
# indexer reads DRIVE_PARSER_USER (indexer_main.py); a named user missing from the image fails its start. No USER
# directive: the services stay root, which switching the child to this user needs.
ENV DRIVE_PARSER_USER=driveparser
RUN useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin driveparser

COPY . .

# Nothing the indexer loads may be writable by the parser user: fail the build on a world-writable file or directory
# under /app (node_modules aside: never loaded by the indexer) or in site-packages.
RUN if find /app /usr/local/lib/python3.11/site-packages -path '*/node_modules' -prune -o \
        -perm -o+w ! -type l -print | grep -q .; then \
      echo "world-writable files under /app or site-packages: the drive parser user could rewrite them" >&2; \
      exit 1; \
    fi

CMD ["python", "main.py"]
