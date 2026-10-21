"""Deterministic operator commands. No UI, shell or model calls."""
import re

ACTION_TYPES = ('screenshot', 'create_job', 'job_status', 'cancel_job', 'switch_model', 'current_model')


def parse_command(text):
    text = text.strip()
    # Deliberately anchored: quoted history and ordinary discussion are not commands.
    if re.fullmatch(r'(?:請|幫我|麻煩)?\s*(?:截圖|擷取|截取)(?:一下)?\s*(?:桌面|螢幕|畫面)?\s*(?::\d{1,3}(?:\.\d)?)?\s*[。！!？?]?', text):
        display = re.search(r':\d{1,3}(?:\.\d)?', text)
        return [{'type': 'screenshot', 'display': display.group() if display else ':3'}]
    if re.fullmatch(r'(?:目前|當前|現在)(?:的)?模型[？?]?', text):
        return [{'type': 'current_model'}]
    match = re.fullmatch(r'(?:請)?\s*(?:切換|更換|切换)模型\s*[:：]?\s*(\S+)', text)
    if match:
        return [{'type': 'switch_model', 'model': match[1]}]
    match = re.fullmatch(r'(?:查看|查詢|請)?\s*工作狀態(?:\s+#?([a-zA-Z0-9_-]+))?[？?]?', text)
    if match:
        return [{'type': 'job_status', 'id': match[1] or ''}]
    match = re.fullmatch(r'(?:請)?\s*(?:取消|停止)工作\s+#?([a-zA-Z0-9_-]+)', text)
    if match:
        return [{'type': 'cancel_job', 'id': match[1]}]
    match = re.fullmatch(r'(?:請|幫我|麻煩)?\s*(?:建立|新增|開始|创建)(?:一份|一個|一个)?\s*(?:Python\s*程式類的?|程式|編碼|coding)?\s*工作\s*[:：]\s*([\s\S]+)', text, re.I)
    if match:
        prompt = match[1].strip()
        backend, model = 'cursor', ''
        choice = re.match(r'\[(codex|cursor)(?:\s+([a-z0-9._-]+))?\]\s*', prompt, re.I)
        if choice:
            backend, model = choice[1].lower(), choice[2] or ''
            prompt = prompt[choice.end():].strip()
        return [{'type': 'create_job', 'prompt': prompt, 'backend': backend, 'model': model}]
    return None


def normalize(item):
    kind = item.get('type')
    if kind == 'screenshot':
        display = item.get('display', ':3')
        return {'type': kind, 'display': display} if isinstance(display, str) and re.fullmatch(r':\d{1,3}(?:\.\d)?', display) else None
    if kind in ('current_model',):
        return {'type': kind}
    if kind in ('job_status', 'cancel_job'):
        jid = item.get('id', '')
        return {'type': kind, 'id': jid} if isinstance(jid, str) and (not jid or re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', jid)) else None
    if kind == 'switch_model':
        name = item.get('model')
        return {'type': kind, 'model': name} if isinstance(name, str) and 0 < len(name) <= 101 else None
    if kind == 'create_job':
        prompt, backend, model = item.get('prompt'), item.get('backend', 'cursor'), item.get('model', '')
        if isinstance(prompt, str) and prompt.strip() and len(prompt) <= 12000 and backend in ('codex', 'cursor') and isinstance(model, str) and len(model) <= 101:
            return dict(type=kind, prompt=prompt.strip(), backend=backend, model=model)
    return None
