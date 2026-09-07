import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dify_client import BailianClient, DifyUnavailable


class BailianSimulator(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.server.mode == 'auth':
            return self.send_json({'error': 'secret'}, 401)
        if self.path.endswith('/models'):
            return self.send_json({'object': 'list', 'data': [{'id': 'qwen-plus'}]})
        return self.send_json({'error': 'not-found'}, 404)

    def do_POST(self):
        self.server.headers_seen = dict(self.headers)
        if self.server.mode == 'timeout':
            time.sleep(1.2)
        if self.server.mode == 'auth':
            return self.send_json({'error': 'secret'}, 401)
        if self.server.mode == 'rate':
            return self.send_json({'error': 'busy'}, 429)
        length = int(self.headers.get('Content-Length', 0))
        self.server.payload = json.loads(self.rfile.read(length))
        if self.server.mode in {'stream', 'tool_stream'}:
            self.server.post_count = getattr(self.server, 'post_count', 0) + 1
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if self.server.mode == 'tool_stream' and self.server.post_count == 1:
                events = [
                    {'id': 'chatcmpl-tool', 'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_', 'function': {'name': 'property_agent_tool', 'arguments': '{"request_token":"x"'}}]}, 'finish_reason': None}]},
                    {'id': 'chatcmpl-tool', 'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': '1', 'function': {'name': '', 'arguments': ',"operation":"context"}'}}]}, 'finish_reason': 'tool_calls'}]},
                ]
            else:
                events = [
                {'id': 'chatcmpl-stream', 'choices': [{'delta': {'role': 'assistant'}, 'finish_reason': None}]},
                {'id': 'chatcmpl-stream', 'choices': [{'delta': {'content': '百炼'}, 'finish_reason': None}]},
                {'id': 'chatcmpl-stream', 'choices': [{'delta': {'content': '已收到'}, 'finish_reason': 'stop'}]},
                ]
            for event in events:
                self.wfile.write(('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            return
        return self.send_json({'id': 'chatcmpl-1', 'choices': [{'message': {'content': '百炼已收到'}}]})


class BailianClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), BailianSimulator)
        cls.server.mode = 'ok'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}/compatible-mode/v1'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.server.mode = 'ok'
        self.client = BailianClient(self.base, 'bailian-secret', 'qwen-plus', 2)

    def test_chat_uses_openai_compatible_payload(self):
        result = self.client.chat('测试问题', 'property:1:v1')
        self.assertEqual(result['answer'], '百炼已收到')
        self.assertTrue(result['conversation_id'])
        self.assertEqual(self.server.payload['model'], 'qwen-plus')
        self.assertEqual(self.server.payload['messages'][0]['content'], '测试问题')
        self.assertEqual(self.server.headers_seen['Authorization'], 'Bearer bailian-secret')

    def test_check_and_safe_errors(self):
        self.assertEqual(self.client.check()['status'], 'ok')
        for mode, code in [('auth', 'authentication'), ('rate', 'rate_limit')]:
            self.server.mode = mode
            with self.subTest(mode=mode):
                with self.assertRaises(DifyUnavailable) as caught:
                    self.client.chat('测试', 'property:1:v1')
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('secret', str(caught.exception))

    def test_timeout_is_bounded(self):
        self.server.mode = 'timeout'
        with self.assertRaises(DifyUnavailable) as caught:
            BailianClient(self.base, 'bailian-secret', 'qwen-plus', 1).chat('测试', 'property:1:v1')
        self.assertEqual(caught.exception.code, 'timeout')

    def test_chat_stream_emits_text_deltas_and_uses_stream_payload(self):
        self.server.mode = 'stream'
        events = list(self.client.chat_stream('测试问题', 'property:1:v1'))
        self.assertEqual(''.join(e['content'] for e in events if e['type'] == 'delta'), '百炼已收到')
        done = events[-1]
        self.assertEqual(done['type'], 'done')
        self.assertEqual(done['answer'], '百炼已收到')
        self.assertTrue(self.server.payload['stream'])

    def test_chat_stream_merges_tool_call_fragments_before_callback(self):
        self.server.mode = 'tool_stream';self.server.post_count = 0
        called=[]
        events=list(self.client.chat_stream('测试问题','property:1:v1',tool_callback=lambda args: called.append(args) or {'ok': True}))
        self.assertEqual(called,[{'request_token':'x','operation':'context'}])
        self.assertEqual(events[-1]['answer'],'百炼已收到')


if __name__ == '__main__':
    unittest.main()
