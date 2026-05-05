import os
from pathlib import Path


def _load_file(path: Path):
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_local_env(env_file='.env.local'):
    """Load KEY=VALUE pairs from .env.local. If absent, fallback to .env.local.example."""
    root = Path(__file__).resolve().parent
    primary = root / env_file
    fallback = root / '.env.local.example'

    if primary.exists():
        _load_file(primary)
        return

    if fallback.exists():
        _load_file(fallback)
        print('⚠️ 未找到 .env.local，已使用 .env.local.example 作为临时配置。建议复制为 .env.local 后再运行。')
