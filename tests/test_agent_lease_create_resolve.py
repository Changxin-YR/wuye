import json
import unittest
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g

from agent_planner import clear_pending_plan, plan_request
from dify_client import BailianClient, _PLANNER_HINT


def lease_facts():
    local_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    start = local_today - timedelta(days=1)
    end = local_today + timedelta(days=365)
    move_in = datetime.combine(local_today, time.min).isoformat(timespec='minutes')
    return start.isoformat(), end.isoformat(), move_in


def request_text():
    start, end, move_in = lease_facts()
    return (
        f'给王五登记租户入住，A栋101室，租期{start}到{end}，'
        f'实际入住时间{move_in}'
    )


def tool_call(call_id, operation, command, arguments):
    return {
        'id': call_id,
        'type': 'function',
        'function': {
            'name': 'property_agent_tool',
            'arguments': json.dumps({
                'operation': operation,
                'command': command,
                'arguments_json': json.dumps(arguments, ensure_ascii=False),
            }, ensure_ascii=False),
        },
    }


class LeaseCreatePlannerTests(unittest.TestCase):
    AUTHORIZED = {'house.search', 'person.search', 'lease.create'}

    def test_complete_single_tenant_request_uses_multi_resolution(self):
        start, end, move_in = lease_facts()
        plan = plan_request(request_text(), self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('lease.create', 'TOOL'))
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(plan['candidates'], ['house.search', 'person.search', 'lease.create'])
        self.assertEqual(plan['arguments']['person_name'], '王五')
        self.assertEqual(plan['arguments']['building_name'], 'A栋')
        self.assertEqual(plan['arguments']['room_no'], 101)
        self.assertEqual(plan['arguments']['start_date'], start)
        self.assertEqual(plan['arguments']['end_date'], end)
        self.assertEqual(plan['arguments']['move_in'], move_in)
        self.assertNotIn('house_id', plan['arguments'])
        self.assertNotIn('person_id', plan['arguments'])
        self.assertNotIn('person_ids', plan['arguments'])

    def test_missing_lease_dates_are_clarified_without_guessing(self):
        plan = plan_request(
            '给王五登记租户入住，A栋101室',
            self.AUTHORIZED,
            {},
        )
        self.assertEqual(plan['intent'], 'lease.create')
        self.assertEqual(plan['action'], 'CLARIFY')
        self.assertIn('start_date', plan['missing_fields'])
        self.assertIn('end_date', plan['missing_fields'])
        self.assertIn('move_in', plan['missing_fields'])
        self.assertNotIn('house_id', plan['arguments'])
        self.assertNotIn('person_ids', plan['arguments'])
        self.assertIn('租期', plan['clarification_text'])

    def test_existing_ambiguous_tenant_decision_is_not_weakened(self):
        plan = plan_request(
            '给王五登记租户入住',
            {'lease.create'},
            {'person_candidates': 2},
        )
        self.assertEqual(plan['intent'], 'lease.create')
        self.assertEqual(plan['action'], 'DISAMBIGUATE')


class LeaseCreateConversationTests(unittest.TestCase):
    AUTHORIZED = {'house.search', 'person.search', 'lease.create'}

    def setUp(self):
        self.app = Flask(__name__)
        self.user = SimpleNamespace(id=901, auth_version=1)

    def plan(self, text, conversation_id=None):
        payload = {'message': text}
        if conversation_id:
            payload['conversation_id'] = conversation_id
        with self.app.test_request_context('/ai/chat', method='POST', json=payload):
            g.user = self.user
            return plan_request(text, self.AUTHORIZED, {})

    def tearDown(self):
        for cid in (None, 'lease-conv'):
            payload = {'message': 'clear'}
            if cid:
                payload['conversation_id'] = cid
            with self.app.test_request_context('/ai/chat', method='POST', json=payload):
                g.user = self.user
                clear_pending_plan()

    def test_date_followup_keeps_tenant_and_house_context(self):
        start, end, move_in = lease_facts()
        first = self.plan('给王五登记租户入住，A栋101室')
        self.assertEqual(first['action'], 'CLARIFY')

        second = self.plan(
            f'租期{start}到{end}，实际入住时间{move_in}',
            'lease-conv',
        )
        self.assertEqual(second['action'], 'TOOL')
        self.assertEqual(second['entity_status'], 'RESOLVE_MULTI')
        self.assertEqual(second['arguments']['person_name'], '王五')
        self.assertEqual(second['arguments']['building_name'], 'A栋')
        self.assertEqual(second['arguments']['room_no'], 101)
        self.assertEqual(second['arguments']['start_date'], start)
        self.assertEqual(second['arguments']['end_date'], end)
        self.assertEqual(second['arguments']['move_in'], move_in)


class LeaseCreateProviderTests(unittest.TestCase):
    AUTHORIZED = {'house.search', 'person.search', 'lease.create'}

    def install_patch(self):
        plan = plan_request(request_text(), self.AUTHORIZED, {})
        self.assertEqual((plan['intent'], plan['action']), ('lease.create', 'TOOL'))
        self.assertEqual(plan['entity_status'], 'RESOLVE_MULTI')
        return plan

    def test_model_cannot_guess_ids_or_switch_dates(self):
        plan = self.install_patch()
        start, end, move_in = lease_facts()
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'l-1', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-l1', 'execute', 'lease.create', {
                    'house_id': 999, 'person_ids': [998],
                    'start_date': '2099-01-01', 'end_date': '2099-12-31',
                    'move_in': '2099-01-01T00:00',
                })],
            }}]},
            {'id': 'l-2', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-l2', 'execute', 'lease.create', {
                    'house_id': 997, 'person_ids': [996],
                    'start_date': '2000-01-01', 'end_date': '2000-01-02',
                    'move_in': '2000-01-01T00:00',
                })],
            }}]},
            {'id': 'l-3', 'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [tool_call('bad-l3', 'execute', 'lease.create', {
                    'house_id': 995, 'person_ids': [994],
                })],
            }}]},
            {'id': 'l-4', 'choices': [{'message': {'role': 'assistant', 'content': '租户入住已登记。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'house.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {'building_name': 'A栋', 'room_no': 101})
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 31, 'community_id': 1, 'building_id': 7,
                        'building_name': 'A栋', 'room_no': 101,
                    }]},
                    'terminal': False,
                }
            if args['command'] == 'person.search':
                self.assertEqual(args['operation'], 'lookup')
                self.assertEqual(params, {
                    'person_name': '王五', 'community_id': 1, 'building_id': 7,
                })
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{'id': 41, 'community_id': 1, 'name': '王五'}]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'lease.create')
            self.assertEqual(args['operation'], 'execute')
            self.assertEqual(params, {
                'house_id': 31,
                'person_ids': [41],
                'start_date': start,
                'end_date': end,
                'move_in': move_in,
            })
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'status': 'executed', 'id': 61},
                'terminal': True,
            }

        hint = dict(plan)
        hint['arguments'] = {
            **plan['arguments'],
            'house_id': 123456,
            'person_ids': [654321],
        }
        hint['tool_call'] = {
            'operation': 'execute', 'command': 'lease.create',
            'arguments_json': json.dumps({
                'house_id': 123456, 'person_ids': [654321],
                'start_date': '2099-01-01', 'end_date': '2099-12-31',
                'move_in': '2099-01-01T00:00',
            }, ensure_ascii=False),
        }
        token = _PLANNER_HINT.set(hint)
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(request_text(), 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual(
            [item['command'] for item in callbacks],
            ['house.search', 'person.search', 'lease.create'],
        )
        dumped = json.dumps(callbacks, ensure_ascii=False)
        for guessed in ('999', '998', '997', '996', '995', '994', '123456', '654321', '2099-01-01'):
            self.assertNotIn(guessed, dumped)
        self.assertEqual(result['execution_state'], 'EXECUTED')

    def test_ambiguous_tenant_stops_before_write(self):
        self.install_patch()
        client = BailianClient('http://agent.invalid', 'key', 'qwen-plus')
        callbacks = []
        responses = iter([
            {'id': 'la-1', 'choices': [{'message': {'role': 'assistant', 'content': '先核对房屋。'}}]},
            {'id': 'la-2', 'choices': [{'message': {'role': 'assistant', 'content': '发现同名租户，正在停止写入。'}}]},
            {'id': 'la-3', 'choices': [{'message': {'role': 'assistant', 'content': '发现同名租户，请补充联系电话。'}}]},
        ])

        def tool(args):
            callbacks.append(dict(args))
            params = json.loads(args['arguments_json'])
            if args['command'] == 'house.search':
                return {
                    'ok': True, 'code': 'SUCCESS',
                    'data': {'items': [{
                        'id': 31, 'community_id': 1, 'building_id': 7,
                        'building_name': 'A栋', 'room_no': 101,
                    }]},
                    'terminal': False,
                }
            self.assertEqual(args['command'], 'person.search')
            self.assertEqual(params['person_name'], '王五')
            return {
                'ok': True, 'code': 'SUCCESS',
                'data': {'items': [
                    {'id': 41, 'community_id': 1, 'name': '王五'},
                    {'id': 42, 'community_id': 1, 'name': '王五'},
                ]},
                'terminal': False,
            }

        plan = plan_request(request_text(), self.AUTHORIZED, {})
        plan['tool_call'] = None
        token = _PLANNER_HINT.set(plan)
        try:
            with patch.object(client, '_request', side_effect=lambda *a, **k: next(responses)):
                result = client.chat(request_text(), 'property:1:v1', tool_callback=tool)
        finally:
            _PLANNER_HINT.reset(token)

        self.assertEqual([item['command'] for item in callbacks], ['house.search', 'person.search'])
        self.assertEqual(result['execution_state'], 'LOOKUP_ONLY')


if __name__ == '__main__':
    unittest.main()
