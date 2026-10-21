"""Local operator configuration and bounded Cursor model discovery."""
import difflib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

import line_common as lc


def fake_mid(prefix, n):
    """Syntactically valid LINE mid. Built at runtime so source has no literal."""
    if prefix not in ('u', 'c', 'r'):
        raise ValueError('mid prefix must be u, c, or r')
    number = int(n)
    if number < 0 or number > 0xffffffff:
        raise ValueError('mid counter out of range')
    return prefix + format(number, '032x')


DEFAULT_ADMIN = fake_mid('u', 1)
DEFAULT_BOT_MID = fake_mid('u', 2)
DEFAULT_MENTION = '@mybot'
DEFAULT_MODEL = 'grok-4.7-low-fast'
DEFAULT_PATH = Path(__file__).with_name('bot_config.json')
MODEL_RE = re.compile(r'[a-z0-9][a-z0-9._-]{0,100}\Z')
USER_MID_RE = re.compile(r'u[0-9a-f]{32}\Z')


class ConfigError(ValueError):
    pass


def _env_user_mid(name):
    raw = os.environ.get(name, '').strip()
    if not raw:
        return None
    if not USER_MID_RE.fullmatch(raw):
        raise ConfigError(name + ' must be a user MID')
    return raw


def _check_admins(admins):
    if (not isinstance(admins, list) or not admins or
            any(not isinstance(item, str) or not USER_MID_RE.fullmatch(item) for item in admins)):
        raise ConfigError('admins must contain user MIDs')


class BotConfig:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ConfigError('bot config unreadable; refusing operator actions') from exc
        if not isinstance(data, dict):
            raise ConfigError('bot config must be an object')
        if 'admins' in data:
            _check_admins(data.get('admins'))
        self_mid = data.get('self_mid', data.get('bot_mid'))
        if self_mid not in (None, ''):
            if not isinstance(self_mid, str) or not USER_MID_RE.fullmatch(self_mid.strip()):
                raise ConfigError('self_mid must be a user MID')
        mention = data.get('mention')
        if mention is not None and (not isinstance(mention, str) or not mention.strip()):
            raise ConfigError('mention must be a display token')
        model = data.get('model', DEFAULT_MODEL)
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model) or not model.endswith('-fast'):
            raise ConfigError('reply model must be a fast model ID')
        return data

    def owner_mids(self):
        """Admin user mids: file, else LINE_OWNER_MID, else a placeholder."""
        data = self.load()
        if 'admins' in data:
            return list(data['admins'])
        env = _env_user_mid('LINE_OWNER_MID')
        return [env] if env else [DEFAULT_ADMIN]

    def bot_mid(self):
        """This account's mid: file self_mid/bot_mid, else LINE_BOT_MID, else a placeholder."""
        data = self.load()
        raw = data.get('self_mid') or data.get('bot_mid')
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return _env_user_mid('LINE_BOT_MID') or DEFAULT_BOT_MID

    def mention_token(self):
        """Display-name token: file mention, else LINE_BOT_MENTION, else @mybot."""
        data = self.load()
        raw = data.get('mention')
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        env = os.environ.get('LINE_BOT_MENTION', '').strip()
        return env or DEFAULT_MENTION

    @property
    def model(self):
        return self.load().get('model', DEFAULT_MODEL)

    def is_admin(self, sender_mid, overrides=None):
        admins = overrides if overrides is not None else self.owner_mids()
        return isinstance(sender_mid, str) and bool(USER_MID_RE.fullmatch(sender_mid)) and sender_mid in admins

    def set_model(self, model):
        if not MODEL_RE.fullmatch(model) or not model.endswith('-fast'):
            raise ConfigError('invalid fast model ID')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.load()
            data['model'] = model
            lc.atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def parse_models(output):
    output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
    return sorted(set(re.findall(r'^\s*([a-z0-9][a-z0-9._-]+)\s+[-–]\s+\S', output, re.M)))


class ModelCatalog:
    def __init__(self, ttl=300):
        self.ttl = ttl
        self.models = []
        self.expires = 0

    def get(self):
        if time.monotonic() < self.expires and self.models:
            return self.models
        try:
            result = subprocess.run(['cursor-agent', '--list-models'], capture_output=True,
                                    text=True, timeout=12, check=True)
            models = parse_models(result.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError('無法取得 Cursor 模型清單，請稍後再試') from exc
        if not models:
            raise ConfigError('Cursor 模型清單無法解析')
        self.models, self.expires = models, time.monotonic() + self.ttl
        return models

    def resolve(self, name):
        models = self.get()
        name = name.strip().lower()
        if not MODEL_RE.fullmatch(name):
            raise ConfigError('模型名稱格式不正確')
        base = name.removesuffix('-fast')
        for candidate in (base + '-fast', 'cursor-' + base + '-fast'):
            if candidate in models:
                return candidate
        fast = [x for x in models if x.endswith('-fast')]
        close = difflib.get_close_matches(base + '-fast', fast, n=3, cutoff=0.35)
        raise ConfigError('找不到可用的 fast 模型；建議：' + '、'.join(close))


CATALOG = ModelCatalog()
