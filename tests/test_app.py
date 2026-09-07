import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image
from sqlalchemy import select,func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.security import generate_password_hash
from app import create_app
from dify_client import BailianClient
from models import AiConversation,AuditLog,Evaluation,House,Notice,Notification,OrderLog,User,WorkOrder
from services import InvalidTransition,transition_status

PW='Demo-pass-123'
HASH=generate_password_hash(PW)

class PropertyAppContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.app=create_app({'TESTING':True,'DATABASE_URL':'sqlite+pysqlite:///:memory:','SECRET_KEY':'test','UPLOAD_FOLDER':self.temp.name,'DIFY_API_KEY':''})
        self.db=self.app.extensions['db_session'];self.client=self.app.test_client()
        with self.db() as db:
            db.add_all([User(id=1,username='admin',password_hash=HASH,role=0),User(id=2,username='alice',password_hash=HASH,role=2),User(id=3,username='bob',password_hash=HASH,role=2),User(id=4,username='worker',password_hash=HASH,role=1),User(id=5,username='worker2',password_hash=HASH,role=1)])
            db.flush();db.add_all([House(id=1,building_name='A',unit='1',room_no=101,owner_id=2),House(id=2,building_name='A',unit='1',room_no=102,owner_id=3),House(id=3,building_name='B',unit='2',room_no=201)])
            db.commit()
    def tearDown(self):self.app.extensions['db_engine'].dispose();self.temp.cleanup()
    def csrf(self,client=None):
        client=client or self.client
        client.get('/auth/login')
        with client.session_transaction() as s:return s['csrf_token']
    def post(self,path,data=None,client=None):
        client=client or self.client;fields={'csrf_token':self.csrf(client)};fields.update(data or {})
        return client.post(path,data=fields)
    def login(self,name='alice',client=None):
        client=client or self.client
        r=self.post('/auth/login',{'username':name,'password':PW},client)
        self.assertEqual(r.status_code,302);return client
    def count(self,model):
        with self.db() as db:return db.scalar(select(func.count()).select_from(model))
    def order(self,client=None,**kw):
        fields={'title':'厨房漏水','content':'水龙头接口漏水','type':'水电故障','house_id':'1','location':'A栋101厨房','contact_name':'测试住户','contact_phone':'13800000000'};fields.update(kw)
        r=self.post('/orders/new',fields,client);self.assertEqual(r.status_code,302,r.get_data(as_text=True))
        return r.headers['Location'].rsplit('/',1)[-1]
    def get_order(self,no):
        with self.db() as db:return db.scalar(select(WorkOrder).where(WorkOrder.order_no==no))
    def action(self,no,action,**kw):
        fields={'action':action,'version':str(self.get_order(no).version)};fields.update(kw)
        return self.post('/orders/'+no,fields)
    def progress(self,no):
        self.login('admin');self.assertEqual(self.action(no,'assign',repairer_id='4').status_code,302)
        self.login('worker');self.assertEqual(self.action(no,'accept').status_code,302)
    def json_post(self,path,payload,client=None):
        c=client or self.client;return c.post(path,json=payload,headers={'X-CSRF-Token':self.csrf(c)})
    @staticmethod
    def image():
        b=io.BytesIO();Image.new('RGB',(20,20),(30,180,140)).save(b,'PNG');b.seek(0);return b

    def test_owner_cannot_read_another_owners_order(self):
        self.login();no=self.order();self.login('bob')
        self.assertNotIn('厨房漏水',self.client.get('/orders').get_data(as_text=True))
        self.assertEqual(self.client.get('/orders/'+no).status_code,403)
    def test_order_transition_rejects_invalid_jump(self):
        with self.assertRaises(InvalidTransition):transition_status(None,SimpleNamespace(status=0),4,1)
    def test_owner_cannot_open_admin_house_page(self):
        self.login();self.assertEqual(self.client.get('/houses').status_code,403)
    def test_dify_without_key_is_explicitly_unavailable(self):
        self.login();r=self.json_post('/ai/chat',{'message':'公告'})
        self.assertEqual(r.status_code,503);self.assertEqual(r.json['code'],'not_configured')
    def test_registration_with_invalid_role_returns_validation_error(self):
        r=self.post('/auth/register',{'username':'bad-role','password':PW,'role':'not-a-number'});self.assertEqual(r.status_code,400)
    def test_login_does_not_redirect_to_external_next_url(self):
        for target in ['https://evil.example','//evil.example','/\\evil.example']:
            with self.subTest(target=target):
                token=self.csrf();r=self.client.post('/auth/login',query_string={'next':target},data={'csrf_token':token,'username':'alice','password':PW});self.assertEqual(r.headers['Location'],'/dashboard')
    def test_order_with_unknown_house_id_is_rejected(self):
        self.login();r=self.post('/orders/new',{'title':'t','content':'c','type':'水电故障','house_id':'999','location':'x','contact_name':'t','contact_phone':'13800000000'})
        self.assertEqual(r.status_code,400);self.assertEqual(self.count(WorkOrder),0)
    def test_profile_renders_uploaded_avatar(self):
        self.login();r=self.post('/profile',{'avatar':(self.image(),'avatar.png')});self.assertEqual(r.status_code,302)
        self.assertIn('avatar-image',self.client.get('/profile').get_data(as_text=True))
    def test_authenticated_forms_render_csrf_token(self):
        self.login()
        for path in ['/orders/new','/profile','/ai']:
            self.assertIn('name="csrf_token"',self.client.get(path).get_data(as_text=True))

    def test_static_javascript_has_executable_mime_type(self):
        response=self.client.get('/static/js/app.js')
        self.assertTrue(response.content_type.startswith('application/javascript'))

    def test_forwarded_prefix_is_used_for_subpath_deployment(self):
        response=self.client.get('/ai',headers={'X-Forwarded-Prefix':'/property'})
        self.assertTrue(response.headers['Location'].startswith('/property/auth/login'))

    def test_login_preserves_forwarded_prefix_for_local_next_target(self):
        token=self.csrf()
        response=self.client.post('/auth/login?next=/ai',headers={'X-Forwarded-Prefix':'/property'},data={'csrf_token':token,'username':'alice','password':PW})
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.headers['Location'],'/property/ai')

    def test_ai_page_exposes_deployment_prefix_to_frontend(self):
        self.login()
        response=self.client.get('/ai',headers={'X-Forwarded-Prefix':'/property'})
        self.assertIn('<meta name="app-root" content="/property">',response.get_data(as_text=True))

    def test_ai_frontend_prefixes_api_requests(self):
        script=self.client.get('/static/js/app.js').get_data(as_text=True)
        self.assertIn("meta[name=\"app-root\"]",script)
        self.assertIn("path('/ai/chat')",script)
        self.assertIn("stream: true",script)
        self.assertIn("getReader()",script)
    def test_logout_requires_csrf_protected_post(self):
        self.login();self.assertEqual(self.client.post('/auth/logout').status_code,400);self.assertEqual(self.post('/auth/logout').status_code,302)
    def test_notice_delete_with_invalid_id_returns_validation_error(self):
        self.login('admin');r=self.post('/notices',{'action':'delete','id':'not-a-number'});self.assertEqual(r.status_code,400)
    def test_profile_rejects_short_password_without_success_message(self):
        self.login();r=self.post('/profile',{'new_password':'123'});self.assertEqual(r.status_code,400);self.assertNotIn('资料已更新',r.get_data(as_text=True))
    def test_public_registration_cannot_choose_staff_or_admin(self):
        for role in ['0','1']:
            r=self.post('/auth/register',{'username':'newuser','password':PW,'role':role});self.assertEqual(r.status_code,400)
        self.assertEqual(self.count(User),5)
    def test_public_owner_registration(self):
        r=self.post('/auth/register',{'username':'newowner','password':PW,'role':'2'});self.assertEqual(r.status_code,302)
        with self.db() as db:self.assertEqual(db.scalar(select(User).where(User.username=='newowner')).role,2)
    def test_login_and_registration_require_csrf(self):
        for path in ['/auth/login','/auth/register']:
            self.assertEqual(self.client.post(path,data={'username':'newowner','password':PW}).status_code,400)
    def test_owner_cannot_claim_unbound_house(self):
        self.login();r=self.post('/orders/new',{'title':'t','content':'c','type':'水电故障','house_id':'3','location':'x','contact_name':'t','contact_phone':'13800000000'})
        self.assertEqual(r.status_code,403)
        with self.db() as db:self.assertIsNone(db.get(House,3).owner_id)
    def test_owner_cannot_use_other_house(self):
        self.login();r=self.post('/orders/new',{'title':'t','content':'c','type':'水电故障','house_id':'2','location':'x','contact_name':'t','contact_phone':'13800000000'})
        self.assertEqual(r.status_code,403)
    def test_public_area_order_without_house(self):
        self.login();no=self.order(type='公共设施',house_id='',location='南门路灯');self.assertIsNone(self.get_order(no).house_id)
    def test_private_order_requires_house(self):
        self.login();r=self.post('/orders/new',{'title':'t','content':'c','type':'水电故障','location':'x','contact_name':'t','contact_phone':'13800000000'});self.assertEqual(r.status_code,400)
    def test_full_repair_rework_evaluation_flow(self):
        self.login();no=self.order();self.progress(no)
        self.assertEqual(self.action(no,'progress',remark='已排查管线').status_code,302)
        self.assertEqual(self.action(no,'finish',remark='已更换密封圈').status_code,302)
        self.login();self.assertEqual(self.action(no,'reopen',remark='仍有少量漏水').status_code,302)
        self.assertEqual(self.get_order(no).status,2);self.assertIsNone(self.get_order(no).finish_time)
        self.login('worker');self.assertEqual(self.action(no,'finish',remark='紧固接口并试水').status_code,302)
        self.login();self.assertEqual(self.action(no,'close').status_code,302)
        self.assertEqual(self.action(no,'evaluate',score='5',comment='维修及时').status_code,302)
        self.assertIn('维修及时',self.client.get('/orders/'+no).get_data(as_text=True))
        self.assertGreaterEqual(self.count(OrderLog),9);self.assertGreater(self.count(Notification),3)
        self.assertEqual(self.action(no,'evaluate',score='4').status_code,409)
    def test_worker_cannot_access_unassigned_order(self):
        self.login();no=self.order();self.login('worker');self.assertEqual(self.client.get('/orders/'+no).status_code,403)
    def test_reassignment_revokes_previous_worker(self):
        self.login();no=self.order();self.login('admin');self.action(no,'assign',repairer_id='4');self.assertEqual(self.action(no,'reassign',repairer_id='5').status_code,302)
        self.login('worker');self.assertEqual(self.client.get('/orders/'+no).status_code,403)
        self.login('worker2');self.assertEqual(self.client.get('/orders/'+no).status_code,200)
    def test_cannot_reassign_in_progress(self):
        self.login();no=self.order();self.progress(no);self.login('admin');self.assertEqual(self.action(no,'reassign',repairer_id='5').status_code,409)
    def test_cannot_cancel_in_progress(self):
        self.login();no=self.order();self.progress(no);self.login();self.assertEqual(self.action(no,'cancel',remark='不需要了').status_code,409)
    def test_finish_requires_resolution(self):
        self.login();no=self.order();self.progress(no);self.assertEqual(self.action(no,'finish').status_code,400);self.assertEqual(self.get_order(no).status,2)
    def test_cancel_requires_reason(self):
        self.login();no=self.order();self.assertEqual(self.action(no,'cancel').status_code,400);self.assertEqual(self.get_order(no).status,0)
    def test_stale_form_rejected(self):
        self.login();no=self.order();v=self.get_order(no).version;self.login('admin');self.action(no,'assign',repairer_id='4')
        r=self.post('/orders/'+no,{'action':'reassign','repairer_id':'5','version':str(v)});self.assertEqual(r.status_code,409);self.assertEqual(self.get_order(no).repairer_id,4)
    def test_orm_concurrent_update_detected(self):
        self.login();no=self.order();a=self.db();b=self.db()
        try:
            x=a.scalar(select(WorkOrder).where(WorkOrder.order_no==no));y=b.get(WorkOrder,x.id)
            x.title='并发修改一';a.commit();y.title='旧版本修改'
            with self.assertRaises(StaleDataError):b.commit()
        finally:a.close();b.close()
    def test_admin_house_bind_and_unbind(self):
        self.login('admin');self.assertEqual(self.post('/houses',{'action':'bind','house_id':'3','owner_id':'2','version':'1'}).status_code,302)
        self.assertEqual(self.post('/houses',{'action':'unbind','house_id':'3','version':'2'}).status_code,302)
        with self.db() as db:self.assertIsNone(db.get(House,3).owner_id)
    def test_house_unbind_blocks_open_order(self):
        self.login();self.order();self.login('admin');self.assertEqual(self.post('/houses',{'action':'unbind','house_id':'1','version':'1'}).status_code,409)
    def test_house_cannot_bind_over_existing_owner(self):
        self.login('admin');self.assertEqual(self.post('/houses',{'action':'bind','house_id':'1','owner_id':'3','version':'1'}).status_code,409)
    def test_invalid_house_action_not_success(self):
        self.login('admin');r=self.post('/houses',{'action':'wrong'});self.assertEqual(r.status_code,400);self.assertNotIn('房屋操作成功',r.get_data(as_text=True))
    def test_invalid_room_rejected(self):
        self.login('admin');self.assertEqual(self.post('/houses',{'action':'add','building_name':'A','unit':'1','room_no':'-1'}).status_code,400)
    def test_admin_creates_worker(self):
        self.login('admin');self.assertEqual(self.post('/users',{'action':'create','username':'staff3','password':PW,'role':'1','real_name':'测试维修员'}).status_code,302)
        self.assertGreater(self.count(AuditLog),0)
    def test_disabled_user_session_invalidated(self):
        other=self.app.test_client();self.login('bob',other);self.login('admin');self.post('/users',{'action':'disable','user_id':'3'})
        self.assertEqual(other.get('/orders').status_code,302);self.assertEqual(other.get('/ai/status').status_code,401)
    def test_disable_busy_repairer_rejected(self):
        self.login();no=self.order();self.progress(no);self.login('admin');self.assertEqual(self.post('/users',{'action':'disable','user_id':'4'}).status_code,409)
    def test_admin_cannot_disable_self(self):
        self.login('admin');self.assertEqual(self.post('/users',{'action':'disable','user_id':'1'}).status_code,400)
    def test_password_change_requires_current_password(self):
        self.login();r=self.post('/profile',{'new_password':'Another-pass-123','current_password':'wrong','real_name':'不应保存'});self.assertEqual(r.status_code,400)
        with self.db() as db:self.assertEqual(db.get(User,2).real_name,'')
    def test_password_change_revokes_other_sessions(self):
        self.login();other=self.app.test_client();self.login('alice',other)
        r=self.post('/profile',{'new_password':'Another-pass-123','current_password':PW});self.assertEqual(r.status_code,302)
        self.assertEqual(other.get('/orders').status_code,302);self.assertEqual(self.client.get('/orders').status_code,200)
    def test_account_locks_after_five_bad_passwords(self):
        for _ in range(5):self.post('/auth/login',{'username':'alice','password':'wrong'})
        r=self.post('/auth/login',{'username':'alice','password':PW});self.assertEqual(r.status_code,200)
        with self.db() as db:self.assertIsNotNone(db.get(User,2).locked_until)
    def test_non_image_rejected(self):
        self.login();r=self.post('/profile',{'avatar':(io.BytesIO(b'not-a-real-image'),'avatar.png')});self.assertEqual(r.status_code,400);self.assertEqual(list(Path(self.temp.name).iterdir()),[])
    def test_order_image_is_private(self):
        self.login();no=self.order(image=(self.image(),'fault.png'));filename=self.get_order(no).img_url
        response=self.client.get('/uploads/'+filename)
        self.assertEqual(response.status_code,200);response.close()
        self.login('bob');self.assertEqual(self.client.get('/uploads/'+filename).status_code,403)
        self.assertEqual(self.app.test_client().get('/uploads/'+filename).status_code,302)
    def test_error_rolls_back_upload_and_order(self):
        self.login()
        with patch('sqlalchemy.orm.Session.commit',side_effect=SQLAlchemyError('synthetic failure')):
            r=self.post('/orders/new',{'title':'t','content':'c','type':'公共设施','location':'x','contact_name':'t','contact_phone':'13800000000','image':(self.image(),'x.png')})
        self.assertEqual(r.status_code,503);self.assertEqual(self.count(WorkOrder),0);self.assertEqual(list(Path(self.temp.name).iterdir()),[])
    def test_profile_avatar_reencoded(self):
        self.login();self.post('/profile',{'avatar':(self.image(),'renamed.webp')})
        files=list(Path(self.temp.name).iterdir());self.assertEqual(files[0].suffix,'.jpg')
        with Image.open(files[0]) as image:self.assertEqual(image.format,'JPEG')
    def test_notice_blank_does_not_report_success(self):
        self.login('admin');r=self.post('/notices',{'action':'add','title':'','content':'c'});self.assertEqual(r.status_code,400);self.assertEqual(self.count(Notice),0)
    def test_notice_edit_and_owner_read(self):
        self.login('admin');self.post('/notices',{'action':'add','title':'供水通知','content':'今晚检修'})
        self.assertEqual(self.post('/notices',{'action':'edit','id':'1','version':'1','title':'供水恢复','content':'已经恢复'}).status_code,302)
        self.login();self.assertIn('已经恢复',self.client.get('/notices').get_data(as_text=True));self.assertEqual(self.post('/notices',{'action':'delete','id':'1','version':'2'}).status_code,403)
    def test_notification_ownership(self):
        self.login();self.order()
        with self.db() as db:item=db.scalar(select(Notification));nid=item.id
        self.assertEqual(self.post('/notifications',{'id':str(nid)}).status_code,403)
        self.login('admin');self.assertEqual(self.post('/notifications',{'id':str(nid)}).status_code,302)
    def test_pagination_and_search_preserve_scope(self):
        with self.db() as db:
            for i in range(25):db.add(WorkOrder(order_no=f'page-{i}',owner_id=2,title=f'测试单{i}',content='c'))
            db.add(WorkOrder(order_no='bob-secret',owner_id=3,title='别人的机密',content='secret'));db.commit()
        self.login();r=self.client.get('/orders');self.assertEqual(r.status_code,200);self.assertIn('下一页',r.get_data(as_text=True))
        self.assertNotIn('别人的机密',self.client.get('/orders?q=机密').get_data(as_text=True));self.assertEqual(self.client.get('/orders?page=-1').status_code,400)
    def test_all_role_pages_render(self):
        for role,paths in [('alice',['/dashboard','/orders','/my-houses','/notifications','/profile','/ai']),('worker',['/dashboard','/orders','/notifications','/ai']),('admin',['/dashboard','/users','/houses','/notices','/audit','/notifications','/ai'])]:
            self.login(role)
            for path in paths:
                with self.subTest(role=role,path=path):self.assertEqual(self.client.get(path).status_code,200)
    def test_ai_numeric_and_non_object_inputs_rejected(self):
        self.login()
        for payload in [{'message':123},{'message':[]},['message'],'text',{'message':'x'*2001}]:
            self.assertEqual(self.json_post('/ai/chat',payload).status_code,400)
    def test_ai_connection_not_claimed(self):
        self.login();r=self.client.get('/ai');self.assertNotIn('已连接 Dify',r.get_data(as_text=True));self.assertFalse(self.client.get('/ai/status').json['connection_verified'])

    def test_ai_chat_stream_returns_sse_and_persists_conversation(self):
        self.login()
        client=BailianClient('http://127.0.0.1:1','fixture-key')
        self.app.extensions['dify']=client
        events=iter([{'type':'delta','content':'流式'}, {'type':'delta','content':'回答'}, {'type':'done','answer':'流式回答','conversation_id':'upstream-stream'}])
        with patch.object(client,'chat_stream',return_value=events):
            response=self.json_post('/ai/chat',{'message':'测试','stream':True})
        self.assertEqual(response.status_code,200)
        self.assertIn('text/event-stream',response.headers.get('Content-Type',''))
        body=response.get_data(as_text=True)
        self.assertIn('流式',body);self.assertIn('回答',body)
        with self.db() as db:self.assertEqual(db.scalar(select(func.count(AiConversation.id))),1)

    def test_ai_blocks_repeated_mutation_command_in_one_turn(self):
        class DuplicateClient(BailianClient):
            def chat(self, query, user, conversation_id='', tool_callback=None, system_prompt=''):
                base={'operation':'execute','command':'order.create','arguments_json':'{"house_id":1,"title":"重复报修","content":"水管漏水","type":"水电故障","location":"厨房","contact_name":"住户","contact_phone":"13800000000"}'}
                tool_callback(base)
                tool_callback({**base,'arguments_json':'{"house_id":1,"title":"重复报修","content":"水管漏水","type":"水电故障","location":"厨房水槽","contact_name":"住户","contact_phone":"13800000000"}'})
                return {'answer':'已处理','conversation_id':'duplicate-check'}

        self.login()
        self.app.extensions['dify']=DuplicateClient('http://127.0.0.1:1','fixture-key')
        response=self.json_post('/ai/chat',{'message':'再提交一次刚才的报修'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.count(WorkOrder),1)
    def test_owner_cannot_run_ai_admin_probe(self):
        self.login();self.assertEqual(self.json_post('/ai/check',{}).status_code,403)
    def test_ai_filters_context_and_keeps_conversation_private(self):
        self.login();self.order()
        with self.db() as db:db.add(WorkOrder(order_no='secret-bob',owner_id=3,title='隐私暗号',content='不可泄露'));db.commit()
        with patch.object(self.app.extensions['dify'],'chat',return_value={'answer':'已收到','conversation_id':'upstream-1'}) as call:
            r=self.json_post('/ai/chat',{'message':'我的工单','user_id':3,'role':'管理员'});self.assertEqual(r.status_code,200)
            query,user,conv=call.call_args.args;self.assertNotIn('隐私暗号',query);self.assertIn('厨房漏水',query);self.assertIn('property:2:',user)
            cid=r.json['conversation_id'];self.login('bob');r=self.json_post('/ai/chat',{'message':'继续','conversation_id':cid});self.assertEqual(r.status_code,403)
    def test_ai_scope_change_resets_upstream_context(self):
        self.login();no=self.order()
        with patch.object(self.app.extensions['dify'],'chat',return_value={'answer':'正常','conversation_id':'upstream-1'}) as call:
            cid=self.json_post('/ai/chat',{'message':'状态'}).json['conversation_id']
            self.json_post('/ai/chat',{'message':'继续','conversation_id':cid});self.assertEqual(call.call_args.args[2],'upstream-1')
            with self.db() as db:o=db.scalar(select(WorkOrder).where(WorkOrder.order_no==no));o.title='更新了';db.commit()
            self.json_post('/ai/chat',{'message':'继续','conversation_id':cid});self.assertEqual(call.call_args.args[2],'')
    def test_ai_upstream_id_not_accepted_as_local_id(self):
        self.login();self.assertEqual(self.json_post('/ai/chat',{'message':'继续','conversation_id':'upstream-guessed'}).status_code,403)
    def test_health_is_database_only(self):
        r=self.client.get('/health');self.assertEqual(r.json,{'status':'ok','database':'ok'})
    def test_security_headers(self):
        r=self.client.get('/auth/login');self.assertEqual(r.headers['X-Content-Type-Options'],'nosniff');self.assertIn('no-store',r.headers['Cache-Control'])
    def test_sql_injection_search_literal(self):
        self.login();self.assertEqual(self.client.get('/orders?q=%27%20OR%201%3D1--').status_code,200)

if __name__=='__main__':unittest.main()
