"""ai_backend.py — reply-model wrapper.

`ask_grok` sends the prompt to the backend chosen in bot_config.json
(`backend`, then any `fallback_backends`) and returns the text reply:

  * `cursor` (default): `cursor-agent -p --force` with the persisted fast
    model (default grok-4.7-low-fast).
  * `antigravity`: the Antigravity CLI over ACP (JSON-RPC on stdio), e.g.
    `gemini-4-hydrogen-xhigh`. Its per-session files are removed afterwards.
  * `openai`: an OpenAI-compatible `/v1/chat/completions` endpoint
    (`openai_url`, default http://127.0.0.1:8080/...; e.g. llama-server).

Every backend runs in an empty temporary cwd so a coding agent never
treats this repository as its workspace. On Windows, CLIs are started
without cmd.exe (see platform_compat.resolve_cli).
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request

import bot_config
import platform_compat as pc

MODEL = "grok-4.7-low-fast"
CONFIG_PATH = bot_config.DEFAULT_PATH
DEFAULT_OPENAI_URL = "http://127.0.0.1:8080/v1/chat/completions"


class GrokError(RuntimeError):
    """The reply backend failed, timed out, or is missing."""


def _config():
    try:
        return bot_config.BotConfig(CONFIG_PATH)
    except bot_config.ConfigError as exc:
        raise GrokError(str(exc)) from exc


def _ask_cursor(prompt, model, timeout, on_started):
    argv = pc.resolve_cli("cursor-agent")
    if not argv:
        raise GrokError("cannot run cursor-agent: not found on PATH")
    cmd = pc.safe_argv(argv + ["-p", "--force", "--model", model,
                               "--output-format", "text", prompt])
    # Empty cwd so the coding agent does not treat this repo as its
    # workspace and edit it. The prompt is the whole input.
    try:
        work = tempfile.mkdtemp(prefix="line-ai-")
    except OSError as e:
        raise GrokError(f"cannot create cursor-agent working directory: {e}") from e
    try:
        try:
            kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                          errors="replace", cwd=work, creationflags=pc.no_window_flags())
            if on_started is None:
                r = subprocess.run(cmd, timeout=timeout, **kwargs)
            else:
                with subprocess.Popen(cmd, **kwargs) as proc:
                    try:
                        # The model process is running before reaction work is
                        # queued. The callback only enqueues; it never does UI.
                        on_started()
                        stdout, stderr = proc.communicate(timeout=timeout)
                    except BaseException:
                        proc.kill()
                        proc.communicate()
                        raise
                    r = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as e:
            raise GrokError(f"cursor-agent timed out after {timeout}s (model {model})") from e
        except (OSError, ValueError) as e:
            raise GrokError(f"cannot run cursor-agent: {e}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise GrokError(f"cursor-agent exited {r.returncode} (model {model}): {err}")
    return (r.stdout or "").strip()


def find_antigravity(config=None):
    """Antigravity CLI: config `agy_bin`, LINE_AGY_BIN, then PATH."""
    for raw in ((config.get("agy_bin") if config else None), os.environ.get("LINE_AGY_BIN")):
        if raw and os.path.isfile(os.path.expanduser(raw)):
            return os.path.expanduser(raw)
    for name in ("agy", "antigravity"):
        argv = pc.resolve_cli(name)
        # A Windows .cmd shim would re-parse the prompt through cmd.exe.
        if argv and (not pc.IS_WINDOWS or argv[0].lower().endswith(".exe")):
            return argv[0]
    if pc.IS_WINDOWS:
        local = os.path.expanduser(os.path.join("~", "AppData", "Local", "agy", "bin", "agy.exe"))
        if os.path.isfile(local):
            return local
    return None


class _AcpSession:
    """One short-lived `agy acp` process speaking JSON-RPC over stdio."""

    def __init__(self, exe, timeout, gemini_dir):
        self.work = tempfile.mkdtemp(prefix="line-ai-")
        self.gemini_dir = gemini_dir
        self.sid = None
        self.timed_out = threading.Event()
        self.chunks = []
        try:
            self.proc = subprocess.Popen(
                [exe, "acp", f"--gemini-dir={gemini_dir}",
                 "--app-data-dir=antigravity-acp",
                 "--include-default-customizations=false"],
                cwd=self.work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                creationflags=pc.no_window_flags())
        except OSError as e:
            shutil.rmtree(self.work, ignore_errors=True)
            raise GrokError(f"failed to start antigravity ACP: {e}") from e
        self.timer = threading.Timer(timeout, self._expire)
        self.timer.daemon = True
        self.timer.start()
        self.timeout = timeout

    def _expire(self):
        self.timed_out.set()
        try:
            self.proc.kill()
        except OSError:
            pass

    def rpc(self, req_id, method, params):
        msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method,
                          "params": params}, ensure_ascii=False)
        try:
            self.proc.stdin.write(msg + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise GrokError(f"antigravity ACP write failed ({method}): {e}") from e
        while True:
            line = self.proc.stdout.readline()
            if not line:
                if self.timed_out.is_set():
                    raise GrokError(f"antigravity ACP timed out after {self.timeout}s")
                raise GrokError(f"antigravity ACP closed unexpectedly during {method}")
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("method") == "session/update":
                upd = (obj.get("params") or {}).get("update") or {}
                if upd.get("sessionUpdate") == "agent_message_chunk":
                    text = (upd.get("content") or {}).get("text") or ""
                    if text:
                        self.chunks.append(text)
            if obj.get("id") == req_id:
                if obj.get("error") is not None:
                    raise GrokError(f"antigravity ACP {method} failed: {obj['error']}")
                return obj.get("result") or {}

    def new_session(self):
        self.rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        result = self.rpc(2, "session/new", {"cwd": self.work, "mcpServers": []})
        self.sid = result.get("sessionId")
        if not self.sid:
            raise GrokError("antigravity ACP session/new returned no sessionId")
        return result

    def close(self):
        self.timer.cancel()
        try:
            if self.sid and self.proc.poll() is None:
                self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 99,
                                                  "method": "session/close",
                                                  "params": {"sessionId": self.sid}}) + "\n")
                self.proc.stdin.flush()
        except OSError:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.work, ignore_errors=True)
        if self.sid:
            # Do not keep chat transcripts in the agent's history.
            root = os.path.join(self.gemini_dir, "antigravity-acp")
            shutil.rmtree(os.path.join(root, "brain", self.sid), ignore_errors=True)
            try:
                os.unlink(os.path.join(root, "conversations", f"{self.sid}.pb"))
            except OSError:
                pass


def _acp_open(config, timeout):
    exe = find_antigravity(config)
    if not exe:
        raise GrokError("antigravity CLI not found (set agy_bin or LINE_AGY_BIN)")
    gemini_dir = os.environ.get("LINE_GEMINI_DIR",
                                os.path.join(os.path.expanduser("~"), ".gemini"))
    return _AcpSession(exe, timeout, gemini_dir)


def antigravity_models(timeout=30):
    """Model ids the Antigravity account offers (from `session/new`)."""
    session = _acp_open(_config(), timeout)
    try:
        result = session.new_session()
    finally:
        session.close()
    models = (result.get("models") or {}).get("availableModels") or []
    return sorted({m.get("modelId") for m in models if isinstance(m, dict) and m.get("modelId")})


def _ask_antigravity(prompt, model, timeout, on_started, config):
    session = _acp_open(config, timeout)
    try:
        session.new_session()
        session.rpc(3, "session/set_model", {"sessionId": session.sid, "modelId": model})
        if on_started is not None:
            on_started()
        session.rpc(4, "session/prompt", {"sessionId": session.sid,
                                          "prompt": [{"type": "text", "text": prompt}]})
    finally:
        session.close()
    reply = "".join(session.chunks).strip()
    if not reply:
        raise GrokError(f"antigravity ACP returned an empty reply (model {model})")
    return reply


def _ask_openai(prompt, model, timeout, on_started, config):
    url = config.get("openai_url") or os.environ.get("LINE_LLM_URL") or DEFAULT_OPENAI_URL
    payload = json.dumps({"model": model, "temperature": 0.3, "max_tokens": 600,
                          "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    if on_started is not None:
        on_started()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        raise GrokError(f"OpenAI-compatible request failed (model {model}): {e}") from e
    choices = data.get("choices") or []
    if not choices:
        raise GrokError(f"OpenAI-compatible endpoint returned no choices (model {model})")
    return ((choices[0].get("message") or {}).get("content") or "").strip()


def ask_grok(prompt: str, timeout=120, on_started=None) -> str:
    """Send `prompt` to the configured backend(s) and return the reply.

    Reads the selected backend/model on each request. A fallback backend
    is tried only when the previous one failed before producing a reply;
    `on_started` is called at most once.

    Raises GrokError when every backend fails.
    """
    config = _config()
    try:
        backends = config.backends
        primary_model = config.model
    except bot_config.ConfigError as exc:
        raise GrokError(str(exc)) from exc
    started = []

    def once():
        if on_started is not None and not started:
            started.append(True)
            on_started()

    errors = []
    for i, backend in enumerate(backends):
        model = primary_model if i == 0 else bot_config.DEFAULT_MODELS[backend]
        try:
            if backend == "cursor":
                return _ask_cursor(prompt, model, timeout, once if on_started else None)
            if backend == "antigravity":
                return _ask_antigravity(prompt, model, timeout, once if on_started else None, config)
            return _ask_openai(prompt, model, timeout, once if on_started else None, config)
        except GrokError as exc:
            errors.append(str(exc))
    raise GrokError("; ".join(errors))
