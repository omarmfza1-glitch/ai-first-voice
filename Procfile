# ==========================================
# Procfile (for Heroku)
# ==========================================
# Save as: Procfile (no extension)
web: gunicorn -k uvicorn.workers.UvicornWorker app:app --timeout 120 --workers 1

# ==========================================
# .gitignore
# ==========================================
# Save as: .gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Environment
.env
.env.local
.env.*.local

# Database
*.db
*.sqlite3
db.sqlite3

# Generated files
public/tts/*
!public/tts/.gitkeep

# Credentials
gcp.json
*.pem
*.key
*.crt

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# Logs
*.log
logs/

# ==========================================
# docker-compose.yml (for local development)
# ==========================================
version: '3.8'

services:
  app:
    build: .
    ports:
      - "5000:5000"
    environment:
      - PORT=5000
      - BASE_URL=http://localhost:5000
    env_file:
      - .env
    volumes:
      - ./public:/app/public
      - ./db.sqlite3:/app/db.sqlite3
    command: uvicorn app:app --host 0.0.0.0 --port 5000 --reload

  # Optional: PostgreSQL for production
  # db:
  #   image: postgres:15
  #   environment:
  #     POSTGRES_DB: smartcc
  #     POSTGRES_USER: smartcc
  #     POSTGRES_PASSWORD: ${DB_PASSWORD}
  #   volumes:
  #     - postgres_data:/var/lib/postgresql/data
  #   ports:
  #     - "5432:5432"

  # Optional: Redis for session management
  # redis:
  #   image: redis:7-alpine
  #   ports:
  #     - "6379:6379"

volumes:
  postgres_data:

# ==========================================
# Dockerfile
# ==========================================
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p public/tts

# Expose port
EXPOSE 5000

# Run the application
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app:app", "--bind", "0.0.0.0:5000", "--timeout", "120"]

# ==========================================
# runtime.txt (for Heroku Python version)
# ==========================================
# Save as: runtime.txt
python-3.11.7

# ==========================================
# app.json (for Heroku Deploy Button)
# ==========================================
{
  "name": "Smart Call Center",
  "description": "AI-powered call center with Arabic support",
  "repository": "https://github.com/yourusername/smart-call-center",
  "keywords": ["python", "fastapi", "twilio", "ai", "arabic"],
  "env": {
    "OPENAI_API_KEY": {
      "description": "OpenAI API key for GPT and TTS",
      "required": true
    },
    "TWILIO_ACCOUNT_SID": {
      "description": "Twilio Account SID",
      "required": true
    },
    "TWILIO_AUTH_TOKEN": {
      "description": "Twilio Auth Token",
      "required": true
    },
    "BASE_URL": {
      "description": "Base URL of your application",
      "required": true
    },
    "GCP_KEY_JSON": {
      "description": "Google Cloud service account JSON (as string)",
      "required": false
    }
  },
  "formation": {
    "web": {
      "quantity": 1,
      "size": "standard-1x"
    }
  },
  "buildpacks": [
    {
      "url": "heroku/python"
    }
  ]
}
