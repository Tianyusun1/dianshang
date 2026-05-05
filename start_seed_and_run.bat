@echo off
if not exist .env.local (
  echo [INFO] .env.local not found, copying from .env.local.example ...
  copy /Y .env.local.example .env.local >nul
)
python init_product.py
if errorlevel 1 (
  echo [WARN] init_product.py failed, continue to start app...
)
python app.py
