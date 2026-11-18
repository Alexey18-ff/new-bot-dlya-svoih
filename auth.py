import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def check_init_data(init_data: str, bot_token: str, max_age: int = 24 * 3600):
    """Проверяет подпись Telegram WebApp initData. Возвращает dict пользователя или None."""
    if not init_data or not bot_token:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got_hash = pairs.pop("hash", None)
    if not got_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got_hash):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None
