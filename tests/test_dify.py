"""Local HTTP protocol tests. This simulator is not a real Dify/model deployment."""
import json
import re
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import requests
from sqlalchemy import select,func
from werkzeug.serving import make_server,WSGIRequestHandler
from werkzeug.security import generate_password_hash
from app import create_app
from dify_client import DifyClient,DifyUnavailable
from models import AiAction,AiGrant,User,House,WorkOrder,Person,HousePerson,UserRole,UserScope

class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self,*args,**kwargs):pass

class DifySimulator(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send_json(self,obj,status=200):
        raw=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        self.server.headers_seen=dict(self.headers)
        if self.server.mode=='authentication':return self.send_json({'error':'private-secret'},401)
        if self.server.mode=='wrong_mode':return self.send_json({'mode':'workflow'})
        return self.send_json({'mode':'agent-chat','name':'Protocol fixture'})
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));self.server.payload=data
        mode=self.server.mode
        if mode=='authentication':return self.send_json({'error':'private-secret'},401)
        if mode=='rate_limit':return self.send_json({},429)
        if mode=='wrong_type':return self.send_json({'answer':'unexpected blocking'})
        if mode=='timeout':time.sleep(1.15)
        if mode in {'tool','scope_change','tool_error','direct_bind'}:
            token=re.search(r'本次request_token：([^\n]+)',data['query']).group(1);self.server.last_token=token
            context=requests.post(self.server.callback,json={'request_token':token,'operation':'context'},timeout=3)
            self.server.tool_context=context.json();self.server.context_status=context.status_code
            if mode=='direct_bind':
                result=requests.post(self.server.callback,json={'request_token':token,'operation':'execute','command':'relation.bind_by_name','arguments_json':json.dumps({'building_name':'23栋','room_no':311,'person_name':'王五'})},timeout=3)
                self.server.proposal=result.json();self.server.proposal_status=result.status_code
            elif mode in {'tool','tool_error'}:
                params={'title':'HTTP Agent 报修','content':'请求检查水龙头','type':'水电故障','house_id':1,'location':'测试楼101厨房','contact_name':'测试住户','contact_phone':'13800000000'}
                result=requests.post(self.server.callback,json={'request_token':token,'operation':'propose','command':'order.create','arguments_json':json.dumps(params)},timeout=3)
                self.server.proposal=result.json();self.server.proposal_status=result.status_code
            else:
                with self.server.db() as db:u=db.get(User,1);u.active=False;u.auth_version+=1;db.commit()
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        events=[{'event':'agent_thought','thought':'private reasoning must not be returned'},
                {'event':'agent_message','answer':'已准备操作，','conversation_id':'fixture-upstream'},
                {'event':'agent_message','answer':'请确认。','conversation_id':'fixture-upstream'},
                {'event':'message_end','conversation_id':'fixture-upstream'}]
        if mode in {'error','tool_error'}:events=[{'event':'error','message':'private-secret'}]
        if mode=='truncated':events=events[:-1]
        if mode=='replace':events.insert(-1,{'event':'message_replace','answer':'最终回答','conversation_id':'fixture-upstream'})
        if mode=='malformed':
            try:self.wfile.write(b'data: not-json\n\n')
            except (BrokenPipeError,ConnectionResetError):pass
            return
        try:
            for item in events:self.wfile.write(('data: '+json.dumps(item,ensure_ascii=False)+'\n\n').encode());self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError):pass

class DifyWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),DifySimulator)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)+'/v1'
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join(2)
    def setUp(self):self.server.mode='normal';self.client=DifyClient(self.base,'fixture-secret',2)
    def test_agent_mode_sse_real_http_and_conversation(self):
        result=self.client.check(infer=True);self.assertEqual(result['app_mode'],'agent-chat');self.assertFalse(result['agent_tools_verified'])
        answer=self.client.chat('测试','property:1:v1','previous')
        self.assertEqual(answer['answer'],'已准备操作，请确认。');self.assertNotIn('reasoning',answer['answer'])
        self.assertEqual(self.server.payload['response_mode'],'streaming');self.assertEqual(self.server.payload['conversation_id'],'previous')
        self.assertEqual(self.server.headers_seen['Authorization'],'Bearer fixture-secret')
    def test_http_and_sse_errors_are_safe(self):
        for mode,code in [('authentication','authentication'),('rate_limit','rate_limit'),('wrong_type','bad_response'),('error','upstream'),('malformed','bad_response'),('truncated','bad_response')]:
            with self.subTest(mode=mode):
                self.server.mode=mode
                with self.assertRaises(DifyUnavailable) as caught:self.client.chat('测试','fixture')
                self.assertEqual(caught.exception.code,code);self.assertNotIn('private-secret',str(caught.exception))
    def test_timeout_is_bounded(self):
        self.server.mode='timeout'
        with self.assertRaises(DifyUnavailable):DifyClient(self.base,'fixture-secret',1).chat('测试','fixture')
    def test_wrong_application_mode_explained(self):
        self.server.mode='wrong_mode'
        with self.assertRaises(DifyUnavailable) as caught:self.client.check()
        self.assertEqual(caught.exception.code,'app_mode')
    def test_message_replacement(self):
        self.server.mode='replace';self.assertEqual(self.client.chat('测试','fixture')['answer'],'最终回答')
    def integration_app(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        app=create_app({'TESTING':True,'DATABASE_URL':'sqlite+pysqlite:///'+str(Path(temp.name)/'wire.db'),'UPLOAD_FOLDER':temp.name,'SECRET_KEY':'fixture-only','DIFY_BASE_URL':self.base,'DIFY_API_KEY':'fixture-secret'})
        self.addCleanup(app.extensions['db_engine'].dispose)
        with app.extensions['db_session']() as db:
            db.add(User(id=1,username='wire-owner',password_hash=generate_password_hash('fixture-pass'),role=2));db.flush()
            db.add(House(id=1,building_name='测试楼',unit='1',room_no=101,owner_id=1));db.commit()
        server=make_server('127.0.0.1',0,app,threaded=True,request_handler=QuietRequestHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(2)))
        self.server.callback='http://127.0.0.1:'+str(server.server_port)+'/api/agent/tools';self.server.db=app.extensions['db_session']
        client=app.test_client();client.get('/auth/login')
        with client.session_transaction() as s:csrf=s['csrf_token']
        response=client.post('/auth/login',data={'csrf_token':csrf,'username':'wire-owner','password':'fixture-pass'});self.assertEqual(response.status_code,302)
        with client.session_transaction() as s:csrf=s['csrf_token']
        return app,client,{'X-CSRF-Token':csrf}
    def test_native_tool_callback_to_confirmation_roundtrip(self):
        app,client,headers=self.integration_app();self.server.mode='tool'
        response=client.post('/ai/chat',json={'message':'帮我提交报修'},headers=headers)
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        self.assertEqual(self.server.context_status,200);self.assertEqual(self.server.proposal_status,200)
        self.assertNotIn(self.server.last_token,response.get_data(as_text=True))
        with self.server.db() as db:self.assertEqual(db.scalar(select(func.count(WorkOrder.id))),0)
        action=response.json['actions'][0]['id'];self.assertEqual(response.json['actions'][0]['status'],'pending')
        confirmed=client.post('/ai/actions/'+action+'/confirm',json={},headers=headers);self.assertEqual(confirmed.status_code,200)
        with self.server.db() as db:self.assertEqual(db.scalar(select(func.count(WorkOrder.id))),1)
        expired=requests.post(self.server.callback,json={'request_token':self.server.last_token,'operation':'context'},timeout=3);self.assertEqual(expired.status_code,401)
    def test_model_failure_preserves_visible_pending_action(self):
        app,client,headers=self.integration_app();self.server.mode='tool_error'
        response=client.post('/ai/chat',json={'message':'帮我报修'},headers=headers)
        self.assertEqual(response.status_code,503)
        self.assertEqual(len(client.get('/ai/actions').json['actions']),1)
        with self.server.db() as db:self.assertEqual(db.scalar(select(func.count(WorkOrder.id))),0)
    def test_permission_revocation_during_inference_drops_answer(self):
        app,client,headers=self.integration_app();self.server.mode='scope_change'
        response=client.post('/ai/chat',json={'message':'查看工单'},headers=headers)
        self.assertEqual(response.status_code,401);self.assertNotIn('answer',response.json)

    def test_native_tool_direct_owner_binding_real_http_persistence(self):
        app,client,headers=self.integration_app()
        with self.server.db() as db:
            user=db.get(User,1);user.role=0
            db.delete(db.get(UserRole,(1,'resident')));db.flush();db.add(UserRole(user_id=1,role_code='superadmin'))
            db.add(UserScope(user_id=1,kind='all'))
            db.add(House(building_name='23栋',unit='3单元',room_no=311));db.add(Person(community_id=1,name='王五',phone='13800000123'));db.commit()
        self.server.mode='direct_bind'
        response=client.post('/ai/chat',json={'message':'为23栋311室绑定业主王五'},headers=headers)
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(self.server.proposal_status,200)
        self.assertEqual(response.json['actions'][0]['status'],'executed')
        with self.server.db() as db:
            person=db.scalar(select(Person).where(Person.name=='王五'))
            self.assertEqual(db.scalar(select(func.count(HousePerson.id)).where(HousePerson.person_id==person.id)),1)
