import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_security import (
    R0, R1, R2, R3, authorization_fingerprint, execution_mode,
    issue_agent_token, redact_provider_text, risk_for, safe_record,
    validate_agent_token_fingerprint,
)


class DummyPolicy:
    def __init__(self, scopes=None, permissions=None):
        self.user = SimpleNamespace(id=7, auth_version=3, active=True)
        self._scopes = scopes or [{'kind': 'building', 'community_id': 1, 'building_id': 23}]
        self._permissions = permissions or ['property.read', 'relation.write']

    def identity(self):
        return {
            'userId': 7,
            'username': 'tester',
            'roles': ['building_manager'],
            'permissions': sorted(self._permissions),
            'data_scope': self._scopes,
        }


class AgentSecurityPrimitiveTests(unittest.TestCase):
    def test_risk_and_execution_modes(self):
        self.assertEqual(risk_for('house.search', read_only=True), R0)
        self.assertEqual(risk_for('order.create'), R1)
        self.assertEqual(risk_for('relation.bind_by_name'), R2)
        self.assertEqual(risk_for('payment.record'), R3)
        self.assertEqual(execution_mode('relation.bind_by_name'), 'AUTO')
        self.assertEqual(execution_mode('relation.end'), 'CONFIRM')
        self.assertEqual(execution_mode('payment.reverse'), 'CONFIRM')

    def test_r2_auto_policy_can_be_made_stricter_but_not_upgrade_r3(self):
        with patch.dict(os.environ, {'AGENT_R2_AUTO_COMMANDS': ''}):
            self.assertEqual(execution_mode('relation.bind_by_name'), 'CONFIRM')
        with patch.dict(os.environ, {'AGENT_R2_AUTO_COMMANDS': 'payment.reverse,relation.bind_by_name'}):
            self.assertEqual(execution_mode('relation.bind_by_name'), 'AUTO')
            self.assertEqual(execution_mode('payment.reverse'), 'CONFIRM')

    def test_runtime_grant_detects_scope_change(self):
        original = DummyPolicy()
        token = issue_agent_token(original)
        self.assertTrue(validate_agent_token_fingerprint(token, original))
        changed = DummyPolicy(scopes=[{'kind': 'building', 'community_id': 1, 'building_id': 24}])
        self.assertFalse(validate_agent_token_fingerprint(token, changed))
        self.assertNotEqual(authorization_fingerprint(original), authorization_fingerprint(changed))

    def test_legacy_short_lived_token_remains_compatible(self):
        self.assertTrue(validate_agent_token_fingerprint('legacy-random-token-value-1234567890', DummyPolicy()))

    def test_model_record_uses_minimum_disclosure(self):
        row = safe_record({
            'id': 1, 'phone': '13812345678', 'contact_phone': '13987654321',
            'reference': 'BANK-20260907-9988', 'password_hash': 'secret',
            'emergency_contact': '某人 13700001111', 'title': '测试',
        })
        self.assertEqual(row['phone'], '138****5678')
        self.assertEqual(row['contact_phone'], '139****4321')
        self.assertNotIn('password_hash', row)
        self.assertNotIn('emergency_contact', row)
        self.assertNotEqual(row['reference'], 'BANK-20260907-9988')

    def test_provider_output_scrubs_current_grant(self):
        token = issue_agent_token(DummyPolicy())
        text = redact_provider_text('令牌=' + token, token)
        self.assertNotIn(token, text)
        self.assertIn('[临时授权已隐藏]', text)
        legacy = 'x' * 43
        old_text = redact_provider_text('request_token：' + legacy)
        self.assertNotIn(legacy, old_text)


if __name__ == '__main__':
    unittest.main()
