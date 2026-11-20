"""Local operator configuration and bounded reply-model discovery.

Backends: `cursor` (cursor-agent, fast models only; the original
default), `antigravity` (Antigravity CLI over ACP, e.g. on Windows) and
`openai` (any OpenAI-compatible HTTP endpoint such as llama-server).
"""
import difflib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import line_common as lc
import platform_compat as pc


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
DEFAULT_BACKEND = 'cursor'
BACKENDS = ('cursor', 'antigravity', 'openai')
DEFAULT_MODELS = {'cursor': DEFAULT_MODEL, 'antigravity': 'gemini-4-hydrogen-xhigh',
                  'openai': 'local'}
DEFAULT_PATH = Path(__file__).with_name('bot_config.json')
MODEL_RE = re.compile(r'[a-z0-9][a-z0-9._-]{0,100}\Z')
USER_MID_RE = re.compile(r'u[0-9a-f]{32}\Z')


class ConfigError(ValueError):
    pass


def model_ok(backend, model):
    """Cursor replies stay on fast models; other backends take any model id."""
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        return False
    return backend != 'cursor' or model.endswith('-fast')


def _str_list(data, key):
    raw = data.get(key, [])
    if not isinstance(raw, list) or any(not isinstance(x, str) or not x.strip() for x in raw):
        raise ConfigError(f'{key} must be a list of non-empty strings')
    return [x.strip() for x in raw]


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
            data = json.loads(self.path.read_text(encoding='utf-8'))
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
        backend = data.get('backend', DEFAULT_BACKEND)
        if backend not in BACKENDS:
            raise ConfigError('backend must be one of ' + ', '.join(BACKENDS))
        fallbacks = _str_list(data, 'fallback_backends')
        if any(b not in BACKENDS for b in fallbacks):
            raise ConfigError('fallback_backends must name known backends')
        model = data.get('model', DEFAULT_MODELS[backend])
        if not model_ok(backend, model):
            raise ConfigError('reply model must be a fast model ID' if backend == 'cursor'
                              else 'reply model ID is malformed')
        for key in ('file_roots', 'room_plugins'):
            _str_list(data, key)
        for key in ('agy_bin', 'openai_url'):
            if key in data and (not isinstance(data[key], str) or not data[key].strip()):
                raise ConfigError(f'{key} must be a non-empty string')
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
    def backend(self):
        return self.load().get('backend', DEFAULT_BACKEND)

    @property
    def backends(self):
        """Primary backend, then configured fallbacks (no duplicates)."""
        data = self.load()
        order = [data.get('backend', DEFAULT_BACKEND)] + list(data.get('fallback_backends', []))
        return list(dict.fromkeys(order))

    @property
    def model(self):
        data = self.load()
        return data.get('model', DEFAULT_MODELS[data.get('backend', DEFAULT_BACKEND)])

    def get(self, key, default=None):
        return self.load().get(key, default)

    def is_admin(self, sender_mid, overrides=None):
        admins = overrides if overrides is not None else self.owner_mids()
        return isinstance(sender_mid, str) and bool(USER_MID_RE.fullmatch(sender_mid)) and sender_mid in admins

    def set_model(self, model):
        backend = self.backend
        if not model_ok(backend, model):
            raise ConfigError('invalid fast model ID' if backend == 'cursor' else 'invalid model ID')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with pc.locked_path(str(self.path) + '.lock'):
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
        self._by_backend = {}

    def _list_cursor(self):
        argv = pc.resolve_cli('cursor-agent')
        if not argv:
            raise ConfigError('找不到 cursor-agent')
        try:
            result = subprocess.run(argv + ['--list-models'], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=12, check=True,
                                    creationflags=pc.no_window_flags())
            models = parse_models(result.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError('無法取得 Cursor 模型清單，請稍後再試') from exc
        if not models:
            raise ConfigError('Cursor 模型清單無法解析')
        return models

    def _list_antigravity(self):
        import ai_backend  # local import: ai_backend imports this module
        try:
            models = ai_backend.antigravity_models()
        except ai_backend.GrokError as exc:
            raise ConfigError('無法取得 Antigravity 模型清單，請稍後再試') from exc
        if not models:
            raise ConfigError('Antigravity 模型清單是空的')
        return models

    def get(self, backend='cursor'):
        cached = self._by_backend.get(backend)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        if backend == 'cursor':
            models = self._list_cursor()
        elif backend == 'antigravity':
            models = self._list_antigravity()
        else:
            raise ConfigError('這個後端不支援列出模型')
        self._by_backend[backend] = (models, time.monotonic() + self.ttl)
        if backend == 'cursor':
            self.models, self.expires = models, time.monotonic() + self.ttl
        return models

    def resolve(self, name, backend='cursor'):
        name = name.strip().lower()
        if not MODEL_RE.fullmatch(name):
            raise ConfigError('模型名稱格式不正確')
        if backend == 'openai':
            return name  # the endpoint decides; nothing to list
        models = self.get(backend)
        if backend != 'cursor':
            if name in models:
                return name
            close = difflib.get_close_matches(name, models, n=3, cutoff=0.35)
            raise ConfigError('找不到這個模型；建議：' + '、'.join(close))
        base = name.removesuffix('-fast')
        for candidate in (base + '-fast', 'cursor-' + base + '-fast'):
            if candidate in models:
                return candidate
        fast = [x for x in models if x.endswith('-fast')]
        close = difflib.get_close_matches(base + '-fast', fast, n=3, cutoff=0.35)
        raise ConfigError('找不到可用的 fast 模型；建議：' + '、'.join(close))


CATALOG = ModelCatalog()
