import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from agent_state import load_messages, load_state, save_state
from app import create_app
from models import AiConversation, ConversationMessage, User


class AgentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'DATABASE_URL': 'sqlite+pysqlite:///:memory:', 'SECRET_KEY': 'test', 'UPLOAD_FOLDER': self.temp.name})
        self.db = self.app.extensions['db_session']
        with self.db() as db:
            self.user = User(id=1, username='agent-user', password_hash=generate_password_hash('pw'), role=0)
            db.add(self.user)
            db.flush()
            self.conversation = AiConversation(id='conversation-1', user_id=1, auth_version=1)
            db.add(self.conversation)
            db.commit()

    def tearDown(self):
        self.app.extensions['db_engine'].dispose()
        self.temp.cleanup()

    def test_state_and_messages_survive_new_session(self):
        with self.db() as db:
            user = db.get(User, 1)
            conversation = db.get(AiConversation, 'conversation-1')
            save_state(db, conversation, user, {'resolved_order': {'id': 7, 'order_no': 'WO-7', 'version': 9}, 'secret': 'discard'}, [{'role': 'user', 'content': '查工单'}, {'role': 'assistant', 'content': '已找到'}])
            db.commit()
        with self.db() as db:
            user = db.get(User, 1)
            conversation = db.get(AiConversation, 'conversation-1')
            state = load_state(db, conversation, user)
            self.assertEqual(state['resolved_order'], {'id': 7, 'order_no': 'WO-7'})
            self.assertNotIn('secret', state)
            self.assertNotIn('version', json.dumps(state))
            self.assertEqual(load_messages(db, conversation, user)[-1]['content'], '已找到')
            self.assertEqual(db.scalar(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id).order_by(ConversationMessage.sequence.desc())).content, '已找到')

    def test_auth_version_change_invalidates_state(self):
        with self.db() as db:
            user = db.get(User, 1)
            conversation = db.get(AiConversation, 'conversation-1')
            save_state(db, conversation, user, {'resolved_order': {'id': 7}}, [])
            user.auth_version = 2
            db.commit()
            self.assertEqual(load_state(db, conversation, user), {})
