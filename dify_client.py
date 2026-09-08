"""Compatibility wrapper for direct model providers.

The stable provider implementation lives in :mod:`dify_client_core`.  This
wrapper tightens the final-answer guard: a planner-resolved write intent is a
write even when a deterministic fallback tool call cannot yet be built because
an entity must first be looked up.  That prevents lookup-only turns from being
presented to users as successful mutations.
"""
import dify_client_core as _core
from dify_client_core import *

_TOOL_COMMANDS = _core._TOOL_COMMANDS
_PLANNER_HINT = _core._PLANNER_HINT
_planner_calls = _core._planner_calls
_safe_final_answer = _core._safe_final_answer

_READ_ONLY_INTENTS = {
    'house.search', 'building.search', 'unit.search', 'person.search', 'person.properties',
    'order.search', 'order.pending', 'complaint.search', 'complaint.stats', 'visitor.search',
    'vehicle.search', 'parking.search', 'device.search', 'inspection.search', 'fee.search',
    'payment.search', 'billing.unpaid', 'notice.read', 'whoami',
}


def _expected_write():
    hint = _PLANNER_HINT.get() or {}
    action = str(hint.get('action') or '').upper()
    intent = str(hint.get('intent') or '')
    if action == 'CONFIRM':
        return True
    if action == 'TOOL' and intent and intent not in _READ_ONLY_INTENTS and intent not in {'unknown', 'security_boundary'}:
        return True
    fallback = hint.get('tool_call')
    return isinstance(fallback, dict) and fallback.get('operation') in {'execute', 'propose'}


# Core classes resolve this symbol from their own module globals at execution
# time, so patch the narrow predicate without copying the provider loop.
_core._expected_write = _expected_write
