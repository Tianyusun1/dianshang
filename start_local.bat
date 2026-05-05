@echo off
if not exist .env.local (
  echo [INFO] .env.local not found, copying from .env.local.example ...
  copy /Y .env.local.example .env.local >nul
)
python app.py
