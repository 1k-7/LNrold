# Base image
FROM python:3.10-slim

# 1. Install system build dependencies + Chromium for Selenium
RUN apt-get update && apt-get install -y \
gcc \
g++ \
git \
libxml2-dev \
libxslt-dev \
zlib1g-dev \
libjpeg-dev \
libffi-dev \
libssl-dev \
libjemalloc2 \
chromium \
chromium-driver \
&& rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# 2. Upgrade pip
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# 3. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy project files
COPY . .
# 5. IMPORTANT: Optimization Flags
ENV LNCRAWL_MODE="production"
ENV PYTHONUNBUFFERED=1
ENV LD_PRELOAD="/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"

# Create downloads directory
RUN mkdir -p downloads

# Command to run the bot
CMD ["python", "bot.py"]
