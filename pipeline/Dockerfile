FROM python:3.12-slim

WORKDIR /app/pipeline

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline code
COPY . .

# Create data directory
RUN mkdir -p /app/data

# Default command: run the scheduler
CMD ["python", "main.py", "--schedule"]
