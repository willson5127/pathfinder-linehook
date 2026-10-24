"""Standalone LINE webhook and authenticated notification relay."""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

load_dotenv(Path(__file__).with_name('.env'))


def split_text(text: str) -> list[str]:
    """Keep every character; prefer whole lines, then split at Unicode code points."""
    # Reject lone surrogates before sending any part to LINE.
    text.encode('utf-16-le')
    parts = []
    start = 0
    while start < len(text):
        end = start
        units = 0
        last_newline = None
        while end < len(text):
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if units + width > 5000:
                break
            units += width
            if text[end] == '\n':
                last_newline = end + 1
            end += 1
        if end < len(text) and last_newline is not None:
            end = last_newline
        parts.append(text[start:end])
        start = end
    return parts


@dataclass(frozen=True)
class Settings:
    channel_secret: str = ''
    access_token: str = ''
    admin_token: str = ''
    publish_token: str = ''
    owner_user_id: str = ''
    target_ids: tuple[str, ...] = ()
    db_path: str = 'data/chats.sqlite3'

    @classmethod
    def from_env(cls):
        return cls(
            channel_secret=os.getenv('LINE_CHANNEL_SECRET', '').strip(),
            access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '').strip(),
            admin_token=os.getenv('ADMIN_API_TOKEN', '').strip(),
            publish_token=os.getenv('PUBLISH_API_TOKEN', '').strip(),
            owner_user_id=os.getenv('LINE_OWNER_USER_ID', '').strip(),
            target_ids=tuple(dict.fromkeys(x.strip() for x in os.getenv('LINE_TARGET_IDS', '').split(',') if x.strip())),
            db_path=os.getenv('CHAT_DB_PATH', 'data/chats.sqlite3'),
        )


def create_app(settings=None, transport=None):
    settings = settings or Settings.from_env()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def database():
        connection = sqlite3.connect(settings.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with database() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY, type TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS handled_events (
                id TEXT PRIMARY KEY, handled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        ''')

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(base_url='https://api.line.me', timeout=15, transport=transport) as client:
            app.state.line_client = client
            yield

    app = FastAPI(title='Pathfinder LINE Webhook', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(request, expected):
        if not expected:
            raise HTTPException(503, 'API token is not configured')
        supplied = request.headers.get('authorization', '')
        if not hmac.compare_digest(supplied.encode(), ('Bearer ' + expected).encode()):
            raise HTTPException(401, 'Invalid API token')

    async def body(request):
        # Bound memory use even when the sender omits Content-Length.
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 1024 * 1024:
                raise HTTPException(413, 'Request is too large')
        return bytes(chunks)

    async def line_api(path, payload=None):
        if not settings.access_token:
            raise HTTPException(503, 'LINE_CHANNEL_ACCESS_TOKEN is not configured')
        headers = {'Authorization': 'Bearer ' + settings.access_token}
        try:
            response = await app.state.line_client.request(
                'GET' if payload is None else 'POST', path, json=payload, headers=headers)
        except httpx.RequestError:
            raise HTTPException(502, 'LINE API connection failed') from None
        if response.status_code >= 400:
            # Never expose upstream response bodies or credentials.
            raise HTTPException(502, f'LINE API returned HTTP {response.status_code}')
        return response.json() if response.content else {}

    @app.get('/healthz')
    async def health():
        return {'status': 'ok'}

    @app.get('/admin/status')
    async def status(request: Request):
        authorize(request, settings.admin_token)
        return {'channel_secret_configured': bool(settings.channel_secret),
                'access_token_configured': bool(settings.access_token),
                'publish_token_configured': bool(settings.publish_token),
                'owner_user_id': settings.owner_user_id,
                'target_ids': settings.target_ids}

    @app.get('/admin/chats')
    async def chats(request: Request):
        authorize(request, settings.admin_token)
        with database() as db:
            return {'chats': [dict(row) for row in db.execute('SELECT * FROM chats ORDER BY last_seen DESC')],
                    'users': [dict(row) for row in db.execute('SELECT * FROM users ORDER BY last_seen DESC')]}

    @app.get('/admin/bot')
    async def bot(request: Request):
        authorize(request, settings.admin_token)
        return await line_api('/v2/bot/info')

    @app.post('/webhook')
    @app.post('/webhook/', include_in_schema=False)
    @app.post('/callback', include_in_schema=False)
    async def webhook(request: Request):
        if not settings.channel_secret:
            raise HTTPException(503, 'LINE_CHANNEL_SECRET is not configured')
        raw = await body(request)
        expected = base64.b64encode(hmac.new(settings.channel_secret.encode(), raw, hashlib.sha256).digest()).decode()
        supplied = request.headers.get('x-line-signature', '')
        if not hmac.compare_digest(expected.encode(), supplied.encode()):
            raise HTTPException(401, 'Invalid LINE signature')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid JSON') from None
        if not isinstance(payload, dict) or not isinstance(payload.get('events'), list):
            raise HTTPException(400, 'events must be an array')
        for event in payload['events']:
            if not isinstance(event, dict):
                raise HTTPException(400, 'Invalid event')
            event_id = event.get('webhookEventId')
            with database() as db:
                if event_id and db.execute('SELECT 1 FROM handled_events WHERE id=?', (event_id,)).fetchone():
                    continue
            source = event.get('source', {})
            if not isinstance(source, dict):
                raise HTTPException(400, 'Invalid event source')
            kind = source.get('type')
            chat_id = source.get({'user': 'userId', 'group': 'groupId', 'room': 'roomId'}.get(kind, ''))
            user_id = source.get('userId')
            with database() as db:
                if chat_id:
                    active = int(event.get('type') not in ('leave', 'unfollow'))
                    db.execute('INSERT INTO chats(id,type,active) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET active=excluded.active,last_seen=CURRENT_TIMESTAMP', (chat_id, kind, active))
                if user_id:
                    db.execute('INSERT INTO users(id) VALUES(?) ON CONFLICT(id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP', (user_id,))
            message = event.get('message', {})
            if (isinstance(message, dict) and event.get('type') == 'message'
                    and message.get('type') == 'text' and message.get('text', '').strip().lower() in ('/id', '/whoami')
                    and event.get('replyToken') and settings.access_token):
                text = f'聊天室類型：{kind}\n聊天室 ID：{chat_id or "未知"}\n你的 userId：{user_id or "LINE 未提供"}'
                await line_api('/v2/bot/message/reply', {'replyToken': event['replyToken'], 'messages': [{'type': 'text', 'text': text}]})
            with database() as db:
                if event_id:
                    db.execute('INSERT OR IGNORE INTO handled_events(id) VALUES(?)', (event_id,))
                db.execute("DELETE FROM handled_events WHERE handled_at < datetime('now', '-7 days')")
        return {'ok': True}

    @app.post('/publishmessage/')
    @app.post('/publishmessage', include_in_schema=False)
    async def publish(request: Request):
        authorize(request, settings.publish_token)
        raw = await body(request)
        try:
            if request.headers.get('content-type', '').split(';')[0] == 'application/json':
                data = json.loads(raw)
                text = data.get('msg') if isinstance(data, dict) else None
            else:
                text = parse_qs(raw.decode('utf-8'), max_num_fields=10).get('msg', [None])[0]
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid message payload') from None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(422, 'msg must be a non-empty string')
        try:
            parts = split_text(text)
        except UnicodeEncodeError:
            raise HTTPException(422, 'msg contains invalid Unicode') from None
        if not settings.target_ids:
            raise HTTPException(503, 'LINE_TARGET_IDS is not configured')
        # The request cannot choose its own recipient. Only configured targets receive pushes.
        with database() as db:
            for target in settings.target_ids:
                row = db.execute('SELECT active FROM chats WHERE id=?', (target,)).fetchone()
                if row is not None and not row['active']:
                    raise HTTPException(409, 'A configured target has left or blocked the bot')
        sent = []
        sent_messages = 0
        for target_index, target in enumerate(settings.target_ids):
            for offset in range(0, len(parts), 5):
                batch = parts[offset:offset + 5]
                try:
                    await line_api('/v2/bot/message/push', {
                        'to': target, 'messages': [{'type': 'text', 'text': part} for part in batch]})
                except HTTPException as error:
                    raise HTTPException(error.status_code, {
                        'error': error.detail, 'sent_count': len(sent),
                        'sent_message_count': sent_messages, 'parts_per_target': len(parts),
                        'failed_target_index': target_index, 'failed_batch_start': offset,
                        'warning': 'Do not blindly retry: earlier batches may already have been accepted; a timed-out batch may also have been accepted',
                    }) from None
                sent_messages += len(batch)
            sent.append(target)
        return {'ok': True, 'sent_count': len(sent), 'sent_message_count': sent_messages,
                'parts_per_target': len(parts)}

    return app


app = create_app()
