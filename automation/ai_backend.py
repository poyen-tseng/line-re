"""ai_backend.py — thin wrapper around the Cursor CLI agent.

`ask_grok` shells out to `cursor-agent` with the persisted fast model
(default grok-4.7-low-fast) and returns the text reply.
"""
import shutil
import subprocess
import tempfile
import bot_config

MODEL = "grok-4.7-low-fast"
CONFIG_PATH = bot_config.DEFAULT_PATH


class GrokError(RuntimeError):
    """cursor-agent failed, timed out, or is missing."""


def ask_grok(prompt: str, timeout=120, on_started=None) -> str:
    """Send `prompt` to cursor-agent and return stdout as the reply.

    Reads the selected model on each request, using an empty temporary cwd.

    Raises GrokError on timeout, missing binary, or non-zero exit.
    """
    try:
        model = bot_config.BotConfig(CONFIG_PATH).model
    except bot_config.ConfigError as exc:
        raise GrokError(str(exc)) from exc
    cmd = [
        "cursor-agent", "-p", "--force",
        "--model", model,
        "--output-format", "text",
        prompt,
    ]
    # Empty cwd so the coding agent does not treat this repo as its
    # workspace and edit it. The prompt is the whole input.
    try:
        work = tempfile.mkdtemp(prefix="line-ai-")
    except OSError as e:
        raise GrokError(f"cannot create cursor-agent working directory: {e}") from e
    try:
        try:
            kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, text=True, cwd=work)
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
            raise GrokError(
                f"cursor-agent timed out after {timeout}s (model {model})"
            ) from e
        except (OSError, ValueError) as e:
            raise GrokError(f"cannot run cursor-agent: {e}") from e
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise GrokError(
            f"cursor-agent exited {r.returncode} (model {model}): {err}"
        )
    return (r.stdout or "").strip()
