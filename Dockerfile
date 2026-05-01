FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .
# Ensure data directory exists and has permissions
RUN mkdir -p data/PaperBananaBench/diagram data/PaperBananaBench/plot && chmod -R 777 data

# Expose port
EXPOSE 8080

# Run FastAPI (lightweight monolith: FastAPI + Jinja2)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
