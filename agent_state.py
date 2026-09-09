"""Durable Agent conversation state shared by all workers."""
import json

from sqlalchemy import select, delete

from models import AiConversation, ConversationMessage, ConversationState, utcnow

MAX_MESSAGES = 24
SELECTOR_KEYS = {
    'resolved_order', 'resolved_house', 'resolved_person', 'resolved_notice',
    'resident_current_house', 'writable_communities', 'person_candidates',
    'vehicle_candidates', 'parking_candidates', 'notice_building_candidates',
}


def _json(value, fallback):
    try:
        result = json.loads(value or '')
        return result
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def selector_state(value):
    """Keep only selector data; versions and arbitrary provider fields never persist."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in SELECTOR_KEYS:
        item = value.get(key)
        if key == 'resident_current_house' or key.endswith('_candidates'):
            if isinstance(item, (bool, int)):
                result[key] = item
        elif isinstance(item, list):
            result[key] = [x for x in item[:101] if isinstance(x, dict)]
        elif isinstance(item, dict):
            clean = {k: v for k, v in item.items() if k in {'id', 'name', 'order_no', 'building_name', 'room_no', 'title', 'community_id', 'building_id'} and isinstance(v, (str, int, type(None)))}
            clean.pop('version', None)
            if clean:
                result[key] = clean
    return result


def load_state(db, conversation, user):
    if not conversation or conversation.user_id != user.id:
        return {}
    if conversation.auth_version not in (None, 0, user.auth_version):
        return {}
    row = db.scalar(select(ConversationState).where(ConversationState.conversation_id == conversation.id))
    raw = row.state_json if row and row.user_id == user.id and row.auth_version == user.auth_version else conversation.state_json
    return selector_state(_json(raw, {}))


def save_state(db, conversation, user, state, messages=None):
    """Persist state and optional provider messages atomically with the request."""
    if not conversation or conversation.user_id != user.id:
        return
    conversation.auth_version = user.auth_version
    clean = selector_state(state)
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True)
    conversation.state_json = encoded
    row = db.scalar(select(ConversationState).where(ConversationState.conversation_id == conversation.id).with_for_update())
    if row is None:
        row = ConversationState(conversation_id=conversation.id, user_id=user.id, auth_version=user.auth_version, state_json=encoded)
        db.add(row)
    else:
        row.user_id = user.id
        row.auth_version = user.auth_version
        row.state_json = encoded
        row.updated_at = utcnow()
    if messages is not None:
        safe_messages = []
        for message in list(messages)[-MAX_MESSAGES:]:
            if not isinstance(message, dict) or message.get('role') not in {'system', 'user', 'assistant', 'tool'}:
                continue
            content = message.get('content')
            if content is None:
                content = ''
            if not isinstance(content, str) or len(content) > 12000:
                continue
            item = {'role': message['role'], 'content': content}
            if isinstance(message.get('tool_call_id'), str):
                item['tool_call_id'] = message['tool_call_id'][:128]
            if isinstance(message.get('tool_calls'), list):
                calls = []
                for call in message['tool_calls'][:4]:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get('function') if isinstance(call.get('function'), dict) else {}
                    calls.append({'id': str(call.get('id', ''))[:128], 'type': 'function', 'function': {'name': str(fn.get('name', ''))[:128], 'arguments': str(fn.get('arguments', ''))[:10000]}})
                if calls:
                    item['tool_calls'] = calls
            safe_messages.append(item)
        conversation.messages_json = json.dumps(safe_messages, ensure_ascii=False)
        db.execute(delete(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id))
        for sequence, message in enumerate(safe_messages):
            db.add(ConversationMessage(conversation_id=conversation.id, user_id=user.id, sequence=sequence, role=message['role'], content=message['content']))
    conversation.updated_at = utcnow()


def load_messages(db, conversation, user):
    if not conversation or conversation.user_id != user.id:
        return []
    value = _json(conversation.messages_json, [])
    if isinstance(value, list) and value:
        return value
    rows = db.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id, ConversationMessage.user_id == user.id).order_by(ConversationMessage.sequence)).all()
    return [{'role': row.role, 'content': row.content} for row in rows]
