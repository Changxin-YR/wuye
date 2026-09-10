import hashlib
import json
import secrets
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch
from sqlalchemy import select,func
from sqlalchemy.exc import SQLAlchemyError
import test_app as fixtures
from models import AiAction,AiGrant,AuditLog,Evaluation,House,Notice,Notification,OrderLog,User,WorkOrder,utcnow

class AgentTests(unittest.TestCase):
    for _method in ('setUp','tearDown','csrf','post','login','count','order','get_order','action','progress','json_post'):
        locals()[_method]=getattr(fixtures.PropertyAppContractTests,_method)

    def grant(self,uid=2,expired=False):
        token=secrets.token_urlsafe(32)
        with self.db() as db:
            user=db.get(User,uid)
            db.add(AiGrant(id=str(uuid.uuid4()),user_id=uid,auth_version=user.auth_version,token_hash=hashlib.sha256(token.encode()).hexdigest(),expires_at=utcnow()+timedelta(minutes=-1 if expired else 3)));db.commit()
        return token
    def tool(self,token,command=None,args=None,operation='propose'):
        return self.app.test_client().post('/api/agent/tools',json={'request_token':token,'operation':operation,'command':command,'arguments_json':json.dumps(args or {})})
    def proposal(self,token,command,args):
        r=self.tool(token,command,args);self.assertEqual(r.status_code,200,r.get_data(as_text=True));return r.json['id']
    def confirm(self,id):return self.json_post('/ai/actions/'+id+'/confirm',{})
    def create_params(self):return {'title':'Agent报修测试','content':'厨房水龙头漏水','type':'水电故障','house_id':1,'location':'A栋101厨房','contact_name':'测试住户','contact_phone':'13800000000'}

    def test_missing_or_expired_grant(self):
        for token in ('','invalid',self.grant(expired=True)):
            self.assertEqual(self.tool(token,operation='context').status_code,401)
    def test_grant_does_not_bypass_confirmation_login(self):
        self.assertEqual(self.app.test_client().post('/ai/actions/no/confirm',json={'request_token':self.grant()}).status_code,401)
    def test_tool_cannot_confirm(self):
        self.assertEqual(self.tool(self.grant(),operation='confirm').status_code,400)
    def test_grant_revoked_by_password_change(self):
        token=self.grant()
        with self.db() as db:u=db.get(User,2);u.auth_version+=1;db.commit()
        self.assertEqual(self.tool(token,operation='context').status_code,403)
    def test_context_role_scope_and_no_passwords(self):
        self.login();no=self.order()
        for uid in (2,3,4):
            r=self.tool(self.grant(uid),operation='context');self.assertEqual(r.status_code,200)
            self.assertNotIn('password',r.get_data(as_text=True));self.assertNotIn('users',r.json)
            self.assertEqual(bool(r.json['orders']),uid==2)
            self.assertNotIn('order.assign',[c['command'] for c in r.json['commands']])
    def test_owner_cannot_propose_admin_or_impersonate(self):
        token=self.grant()
        self.assertEqual(self.tool(token,'notice.add',{'title':'x','content':'x'}).status_code,403)
        params=self.create_params();params['owner_id']=3
        self.assertEqual(self.tool(token,'order.create',params).status_code,400)
    def test_proposal_has_no_business_side_effects(self):
        self.login();token=self.grant();id=self.proposal(token,'order.create',self.create_params())
        self.assertEqual(self.count(WorkOrder),0);self.assertEqual(self.count(OrderLog),0);self.assertEqual(self.count(Notification),0)
        self.assertEqual(self.count(AiAction),1)
        self.assertEqual(self.proposal(token,'order.create',self.create_params()),id)
        self.assertEqual(self.count(AiAction),1)
        self.assertEqual(self.client.get('/ai/actions').json['actions'][0]['id'],id)
    def test_confirm_creates_once_and_audits(self):
        self.login();id=self.proposal(self.grant(),'order.create',self.create_params())
        r=self.confirm(id);self.assertEqual(r.status_code,200);self.assertEqual(r.json['status'],'executed')
        self.assertEqual(self.count(WorkOrder),1);self.assertEqual(self.count(OrderLog),1);self.assertEqual(self.count(Notification),1)
        repeat=self.confirm(id);self.assertEqual(repeat.status_code,200);self.assertEqual(repeat.json['result'],r.json['result']);self.assertEqual(self.count(WorkOrder),1)

    def test_production_r3_confirm_requires_step_up_password(self):
        self.app.config['APP_ENV']='production';self.login('admin')
        token=self.grant(1);action_id=self.proposal(token,'house.delete',{'house_id':3,'version':1})
        self.assertEqual(self.confirm(action_id).status_code,401)
        confirmed=self.json_post('/ai/actions/'+action_id+'/confirm',{'current_password':'Demo-pass-123'})
        self.assertEqual(confirmed.status_code,200,confirmed.get_data(as_text=True))
        with self.db() as db:self.assertEqual(db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action=='ai_execute')),1)
    def test_confirm_requires_csrf(self):
        self.login();id=self.proposal(self.grant(),'order.create',self.create_params())
        self.assertEqual(self.client.post('/ai/actions/'+id+'/confirm',json={}).status_code,400);self.assertEqual(self.count(WorkOrder),0)
    def test_foreign_action_cannot_read_confirm_cancel(self):
        id=self.proposal(self.grant(),'order.create',self.create_params());self.login('bob')
        self.assertEqual(self.client.get('/ai/actions').json['actions'],[])
        self.assertEqual(self.confirm(id).status_code,403)
        self.assertEqual(self.json_post('/ai/actions/'+id+'/cancel',{}).status_code,403)
    def test_cancel_or_expired_action_cannot_execute(self):
        self.login();id=self.proposal(self.grant(),'order.create',self.create_params())
        self.assertEqual(self.json_post('/ai/actions/'+id+'/cancel',{}).status_code,200)
        self.assertEqual(self.confirm(id).status_code,409)
        id=self.proposal(self.grant(),'order.create',self.create_params())
        with self.db() as db:db.get(AiAction,id).expires_at=utcnow()-timedelta(seconds=1);db.commit()
        self.assertEqual(self.confirm(id).status_code,410);self.assertEqual(self.count(WorkOrder),0)
    def test_failed_commit_rolls_back_execution_and_status(self):
        self.login();id=self.proposal(self.grant(),'order.create',self.create_params());token=self.csrf()
        with patch('sqlalchemy.orm.Session.commit',side_effect=SQLAlchemyError('injected')):
            r=self.client.post('/ai/actions/'+id+'/confirm',json={},headers={'X-CSRF-Token':token})
        self.assertEqual(r.status_code,503);self.assertEqual(self.count(WorkOrder),0)
        with self.db() as db:self.assertEqual(db.get(AiAction,id).status,'pending')
    def test_stale_order_proposal_rejected(self):
        self.login();no=self.order();self.login('admin')
        id=self.proposal(self.grant(1),'order.assign',{'order_no':no,'version':1,'repairer_id':4})
        self.assertEqual(self.get_order(no).status,0)
        self.assertEqual(self.action(no,'assign',repairer_id='5').status_code,302)
        self.assertEqual(self.confirm(id).status_code,409);self.assertEqual(self.get_order(no).repairer_id,5)
    def test_admin_bind_and_notice_proposals_dry_run_and_execute(self):
        self.login('admin');token=self.grant(1)
        id=self.proposal(token,'house.bind',{'house_id':3,'version':1,'owner_id':3})
        with self.db() as db:self.assertIsNone(db.get(House,3).owner_id)
        self.assertEqual(self.confirm(id).status_code,200)
        with self.db() as db:self.assertEqual(db.get(House,3).owner_id,3)
        id=self.proposal(token,'notice.add',{'title':'测试停水','content':'仅测试'})
        self.assertEqual(self.count(Notice),0);self.assertEqual(self.confirm(id).status_code,200);self.assertEqual(self.count(Notice),1)
    def test_disabled_session_cannot_confirm(self):
        self.login();id=self.proposal(self.grant(),'order.create',self.create_params())
        with self.db() as db:db.get(User,2).active=False;db.commit()
        self.assertEqual(self.confirm(id).status_code,401)
    def test_user_disable_proposal_revalidates_target_version(self):
        self.login('admin');id=self.proposal(self.grant(1),'user.disable',{'user_id':3,'auth_version':1})
        with self.db() as db:self.assertTrue(db.get(User,3).active);db.get(User,3).auth_version+=1;db.commit()
        self.assertEqual(self.confirm(id).status_code,409)
    def test_unknown_commands_and_bad_types_rejected(self):
        token=self.grant()
        for command,args in [('sql.execute',{'sql':'DELETE FROM sys_user'}),('order.create',{'title':True}),('order.create',{'title':[]} )]:
            self.assertEqual(self.tool(token,command,args).status_code,400)
    def test_proposal_limit(self):
        token=self.grant(1)
        for i in range(5):self.proposal(token,'notice.add',{'title':str(i),'content':'测试'})
        self.assertEqual(self.tool(token,'notice.add',{'title':'six','content':'测试'}).status_code,429)
        self.assertEqual(self.count(Notice),0)
    def test_agent_whole_order_lifecycle(self):
        self.login();id=self.proposal(self.grant(),'order.create',self.create_params());r=self.confirm(id);no=r.json['result']['url'].rsplit('/',1)[-1]
        steps=[('admin',1,'assign',{'repairer_id':4}),('worker',4,'accept',{}),('worker',4,'progress',{'remark':'已关水检修'}),('worker',4,'finish',{'remark':'更换密封圈'}),('alice',2,'reopen',{'remark':'接口仍渗水'}),('worker',4,'finish',{'remark':'更换接口，测试正常'}),('alice',2,'close',{}),('alice',2,'evaluate',{'score':5,'comment':'修好了'})]
        for username,uid,action,args in steps:
            self.login(username);before=self.get_order(no)
            id=self.proposal(self.grant(uid),'order.'+action,{'order_no':no,'version':before.version,**args})
            self.assertEqual(self.get_order(no).status,before.status)
            r=self.confirm(id);self.assertEqual(r.status_code,200,r.get_data(as_text=True))
        self.assertEqual(self.get_order(no).status,4);self.assertEqual(self.count(Evaluation),1);self.assertEqual(self.count(OrderLog),9)
