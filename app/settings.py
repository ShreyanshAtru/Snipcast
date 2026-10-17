import os
from pathlib import Path
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
DATA = Path(os.getenv('APP_DATA', str(ROOT / 'data'))).expanduser().resolve()
DATA.mkdir(parents=True, exist_ok=True)
ALLOWED_ROOT = Path(os.getenv('MEDIA_ROOT', str(Path.home()))).expanduser().resolve()
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6381/0')
MODE = os.getenv('POSTIZ_MODE', 'demo')
if MODE not in ('demo','live'): raise ValueError('POSTIZ_MODE must be demo or live')
BASE = os.getenv('POSTIZ_BASE_URL', 'http://127.0.0.1:4007/api/public/v1').rstrip('/')
KEY = os.getenv('POSTIZ_API_KEY', '')
ORIGIN = os.getenv('APP_ORIGIN', 'http://127.0.0.1:8787')
MAX_CLIPS = int(os.getenv('MAX_CLIPS', '500'))
