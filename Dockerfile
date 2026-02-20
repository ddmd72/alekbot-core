# ================================
# Application
# ================================
FROM python:3.11-slim

# Вимикаємо буферизацію (щоб логи лилися в консоль одразу)
ENV PYTHONUNBUFFERED=1

# Робоча папка всередині контейнера
WORKDIR /app

# ffmpeg — required by pydub for audio format conversion (mp3, m4a, ogg)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Спочатку копіюємо тільки список бібліотек (для кешування)
COPY requirements.txt .

# Встановлюємо бібліотеки
RUN pip install --no-cache-dir -r requirements.txt

# Копіюємо решту коду
COPY . .

# Запускаємо бота
CMD ["python", "main.py"]
