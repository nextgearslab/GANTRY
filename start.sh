#!/bin/bash
set -e

# Ensure we are in the script's directory
cd "$(dirname "$0")"

echo "🚀 Starting Gantry via Docker Compose..."

# This builds the image if needed and starts the container in the background
docker compose up -d --build

echo "✅ Gantry is running!"
echo "📜 Type './logs.sh' to see real-time logs."