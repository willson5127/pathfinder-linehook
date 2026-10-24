import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import Settings, create_app, split_text


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(channel_secret='test-secret', access_token='test-line-token',
                                 admin_token='test-admin', publish_token='test-publish',
                                 target_ids=('Ctest',), db_path=self.directory.name + '/chats.db')
        self.calls = []
        self.fail = False

    def upstream(self, request):
        self.calls.append((request.url.path, json.loads(request.content) if request.content else None))
        self.assertEqual(request.headers['authorization'], 'Bearer test-line-token')
        return httpx.Response(429 if self.fail else 200, json={'message': 'upstream private response'})

    def client(self, **changes):
        return TestClient(create_app(replace(self.settings, **changes), httpx.MockTransport(self.upstream)))

    def event(self, event_id='event-1', kind='message', text='/id'):
        return {'webhookEventId': event_id, 'type': kind, 'replyToken': 'reply-1',
                'source': {'type': 'group', 'groupId': 'Ctest', 'userId': 'Utest'},
                'message': {'type': 'text', 'text': text}}

    def send(self, client, events):
        raw = json.dumps({'events': events}, ensure_ascii=False).encode()
        signature = base64.b64encode(hmac.new(b'test-secret', raw, hashlib.sha256).digest()).decode()
        return client.post('/webhook', content=raw, headers={'x-line-signature': signature})

    def test_signature_required_and_no_data_saved(self):
        with self.client() as client:
            self.assertEqual(client.post('/webhook', json={'events': [self.event()]}).status_code, 401)
            data = client.get('/admin/chats', headers={'Authorization': 'Bearer test-admin'}).json()
            self.assertEqual(data, {'chats': [], 'users': []})
            self.assertEqual(self.calls, [])

    def test_discover_ids_reply_and_deduplicate(self):
        with self.client() as client:
            self.assertEqual(self.send(client, []).status_code, 200)
            self.assertEqual(self.send(client, [self.event()]).status_code, 200)
            self.assertEqual(self.send(client, [self.event()]).status_code, 200)
            data = client.get('/admin/chats', headers={'Authorization': 'Bearer test-admin'}).json()
            self.assertEqual(data['chats'][0]['id'], 'Ctest')
            self.assertEqual(data['users'][0]['id'], 'Utest')
            self.assertEqual(len(self.calls), 1)
            self.assertIn('Utest', self.calls[0][1]['messages'][0]['text'])

    def test_admin_and_publish_require_different_tokens(self):
        with self.client() as client:
            self.assertEqual(client.get('/admin/chats').status_code, 401)
            self.assertEqual(client.get('/admin/chats', headers={'Authorization': 'Bearer test-publish'}).status_code, 401)
            self.assertEqual(client.post('/publishmessage/', data={'msg': 'hello'}).status_code, 401)
            self.assertEqual(self.calls, [])

    def test_legacy_form_and_json_cannot_override_recipient(self):
        with self.client() as client:
            headers = {'Authorization': 'Bearer test-publish'}
            self.assertEqual(client.post('/publishmessage/', data={'msg': '中文訊息', 'to': 'attacker'}, headers=headers).status_code, 200)
            self.assertEqual(client.post('/publishmessage', json={'msg': 'hello', 'to': 'attacker'}, headers=headers).status_code, 200)
            self.assertEqual([call[1]['to'] for call in self.calls], ['Ctest', 'Ctest'])

    def test_missing_settings_fail_closed(self):
        with self.client(channel_secret='', access_token='', admin_token='', publish_token='', target_ids=()) as client:
            self.assertEqual(client.get('/healthz').status_code, 200)
            self.assertEqual(client.get('/admin/status').status_code, 503)
            self.assertEqual(client.post('/webhook', json={'events': []}).status_code, 503)
            self.assertEqual(client.post('/publishmessage/', data={'msg': 'hello'}).status_code, 503)
        with self.client(target_ids=()) as client:
            self.assertEqual(client.post('/publishmessage/', data={'msg': 'hello'}, headers={'Authorization': 'Bearer test-publish'}).status_code, 503)
        self.assertEqual(self.calls, [])

    def test_leave_blocks_push(self):
        with self.client() as client:
            self.assertEqual(self.send(client, [self.event(kind='leave')]).status_code, 200)
            self.assertEqual(client.post('/publishmessage/', data={'msg': 'hello'}, headers={'Authorization': 'Bearer test-publish'}).status_code, 409)
            self.assertEqual(self.calls, [])

    def test_upstream_failure_is_not_reported_as_success(self):
        self.fail = True
        with self.client() as client:
            response = client.post('/publishmessage/', data={'msg': 'hello'}, headers={'Authorization': 'Bearer test-publish'})
            self.assertEqual(response.status_code, 502)
            self.assertNotIn('upstream private response', response.text)

    def test_reply_failure_can_retry_event(self):
        with self.client() as client:
            self.fail = True
            self.assertEqual(self.send(client, [self.event()]).status_code, 502)
            self.fail = False
            self.assertEqual(self.send(client, [self.event()]).status_code, 200)
            self.assertEqual(len(self.calls), 2)

    def test_empty_and_oversize_messages_rejected(self):
        with self.client() as client:
            for text in (' ', '\ud800'):
                self.assertEqual(client.post('/publishmessage/', content=json.dumps({'msg': text}), headers={
                    'Authorization': 'Bearer test-publish', 'Content-Type': 'application/json'}).status_code, 422)
            self.assertEqual(client.post('/publishmessage/', content=b'x' * (1024 * 1024 + 1), headers={'Authorization': 'Bearer test-publish'}).status_code, 413)
            self.assertEqual(self.calls, [])

    def test_split_preserves_text_and_utf16_boundaries(self):
        for text in ('中' * 5000, '中' * 5001, '😀' * 2501,
                     'a' * 4999 + '😀尾巴', ('2330 台積電 😀\r\n' * 1200)):
            with self.subTest(length=len(text)):
                parts = split_text(text)
                self.assertEqual(''.join(parts), text)
                self.assertTrue(all(0 < len(part.encode('utf-16-le')) // 2 <= 5000 for part in parts))
        self.assertEqual(split_text('a' * 5000), ['a' * 5000])
        self.assertEqual(split_text('a' * 3000 + '\n' + 'b' * 3000), ['a' * 3000 + '\n', 'b' * 3000])

    def test_long_form_is_split_and_sent_to_every_target_in_order(self):
        text = '股票清單😀\n' * 1000
        with self.client(target_ids=('Ctest', 'Uother')) as client:
            response = client.post('/publishmessage/', data={'msg': text}, headers={'Authorization': 'Bearer test-publish'})
            self.assertEqual(response.status_code, 200)
            for target in ('Ctest', 'Uother'):
                messages = [message['text'] for _, payload in self.calls if payload['to'] == target for message in payload['messages']]
                self.assertEqual(''.join(messages), text)
            self.assertEqual(response.json()['sent_count'], 2)
            self.assertEqual(response.json()['sent_message_count'], 2 * len(split_text(text)))

    def test_more_than_five_parts_are_batched(self):
        text = '中' * 30001
        with self.client() as client:
            response = client.post('/publishmessage/', json={'msg': text}, headers={'Authorization': 'Bearer test-publish'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([len(payload['messages']) for _, payload in self.calls], [5, 2])
            self.assertEqual(''.join(message['text'] for _, payload in self.calls for message in payload['messages']), text)
            self.assertEqual(response.json()['parts_per_target'], 7)

    def test_partial_batch_failure_reports_progress_and_stops(self):
        def fail_second(request):
            self.fail = len(self.calls) == 1
            return self.upstream(request)

        app = create_app(self.settings, httpx.MockTransport(fail_second))
        with TestClient(app) as client:
            response = client.post('/publishmessage/', json={'msg': '中' * 50001}, headers={'Authorization': 'Bearer test-publish'})
            self.assertEqual(response.status_code, 502)
            detail = response.json()['detail']
            self.assertEqual(detail['sent_count'], 0)
            self.assertEqual(detail['sent_message_count'], 5)
            self.assertEqual(detail['failed_batch_start'], 5)
            self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()
