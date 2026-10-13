#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then cp .env.example .env; echo "Created .env. Review settings before exposing the API."; fi
docker compose up --build -d
docker compose ps
