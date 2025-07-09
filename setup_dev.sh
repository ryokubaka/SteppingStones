#!/bin/bash

# Stepping Stones Development Setup Script
# This script sets up the development environment with automatic certificate generation

echo "Setting up Stepping Stones development environment..."

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "Error: Docker is not installed. Please install Docker first."
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "Error: Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

# Create nginx/certs directory if it doesn't exist
mkdir -p nginx/certs

# Generate certificates if they don't exist
if [ ! -f nginx/certs/steppingstones.cer ] || [ ! -f nginx/certs/steppingstones.key ]; then
    echo "Generating SSL certificates for localhost..."
    cd nginx
    ./generate_certs.sh
    cd ..
else
    echo "Certificates already exist, skipping generation."
fi

# Build and start the services
echo "Building and starting services..."
docker-compose build
docker-compose up -d

echo ""
echo "Stepping Stones is now running!"
echo ""
echo "Access the application:"
echo "  - HTTP:  http://localhost:4321"
echo "  - HTTPS: https://localhost:443 (self-signed certificate)"
echo ""
echo "Note: When accessing HTTPS, you may see a browser warning about the self-signed certificate."
echo "This is normal for development environments. You can proceed by accepting the certificate."
echo ""
echo "To stop the services, run: docker-compose down"
echo "To view logs, run: docker-compose logs -f" 