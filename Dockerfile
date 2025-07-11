FROM python:3.13-bullseye

# Arguments for environment variables
ARG DJANGO_DEBUG
ARG DJANGO_SECRET_KEY
ARG DJANGO_ALLOWED_HOSTS
ARG DJANGO_ALLOWED_CIDR_NETS

# Set environment variables from args
ENV DJANGO_DEBUG=${DJANGO_DEBUG}
ENV DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
ENV DJANGO_ALLOWED_HOSTS=${DJANGO_ALLOWED_HOSTS}
ENV DJANGO_ALLOWED_CIDR_NETS=${DJANGO_ALLOWED_CIDR_NETS}

# Set working directory
WORKDIR /opt/steppingstones

# Copy application files
COPY . /opt/steppingstones/

# Create and activate virtual environment
RUN python3.13 -m venv .venv && \
    . .venv/bin/activate

# Install system dependencies including Java (openjdk-17-jre)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libldap2-dev \
    libsasl2-dev \
    libssl-dev \
    ldap-utils \
    build-essential \
    libpq-dev \
    netcat-traditional \
    openjdk-17-jdk \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME="/usr/lib/jvm/java-17-openjdk-amd64"
ENV PATH="$JAVA_HOME/bin:$PATH"

# Copy in the requirements first (for better Docker caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    # Install Gunicorn for production WSGI serving
    && pip install --no-cache-dir gunicorn

# Best practices for Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Expose port 8123 for Gunicorn
EXPOSE 8000

# Default command - run Gunicorn on 0.0.0.0:8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "stepping_stones.wsgi:application"]