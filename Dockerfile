FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files (including get_end_date.py and bot.py)
COPY . .

# Set up open permissions on /data so SQLite can create state.db regardless of mount ownership
RUN mkdir -p /data && chmod 777 /data

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)"

CMD ["python", "bot.py"]