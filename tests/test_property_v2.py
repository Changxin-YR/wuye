from database_fixture import test_database
"""Business workflow, negative authorization and persistence acceptance tests."""
import json,hashlib,secrets,uuid,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta,date
from sqlalchemy import select,func
from werkzeug.security import generate_password_hash
from app import create_app
from models import *
from permissions import ROLES
from management_ui import MODULE_CONFIG
from property_service import COMMANDS
PW='Fixture-only-292!';HASH=generate_password_hash(PW)
class PropertyV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.url='sqlite+pysqlite:///'+str(Path(self.temp.name)/'property.db')
        self.url=test_database(self,self.url)
        self.app=create_app({'TESTING':True,'DATABASE_URL':self.url,'SECRET_KEY':'fixture','UPLOAD_FOLDER':self.temp.name,'DIFY_API_KEY':''})
        self.factory=self.app.extensions['db_session'];self.client=self.app.test_client()
        with self.factory() as db:
            db.add(User(username='admin',password_hash=HASH,role=0,real_name='管理员',phone='13800000001'));db.commit()
        self.login('admin')
    def tearDown(self):self.app.extensions['db_engine'].dispose();self.temp.cleanup()
    def csrf(self,client=None):
        c=client or self.client;c.get('/auth/login')
        with c.session_transaction() as s:return s['csrf_token']
    def login(self,name,client=None):
        c=client or self.client;r=c.post('/auth/login',data={'csrf_token':self.csrf(c),'username':name,'password':PW});self.assertEqual(r.status_code,302,r.text)
    def call(self,command,data,expected=200,confirmed=True,key=None):
        h={'X-CSRF-Token':self.csrf()}
        if key:h['Idempotency-Key']=key
        r=self.client.post('/api/business/'+command,json={'data':data,'confirmed':confirmed},headers=h)
        self.assertEqual(r.status_code,expected,r.text[:1500]);return r.json
    def record(self,model,id):
        with self.factory() as db:return db.get(model,id)
    def setup_house(self,building='23栋',room=311,cid=1):
        b=self.call('building.save',{'community_id':cid,'name':building,'floors':25})['id']
        u=self.call('unit.save',{'building_id':b,'name':'3单元'})['id']
        h=self.call('house.save',{'unit_id':u,'room_no':room,'area':'100','usage':'residential','occupancy':'vacant'})['id']
        return b,u,h
    def person(self,name='王五',phone='13800000123',cid=1):return self.call('person.save',{'community_id':cid,'name':name,'phone':phone})['id']
    def staff(self,name,role,scope='community',cid=1,bid=None):
        data={'username':name,'password':PW,'real_name':name,'phone':'13800000999','role_codes':[role],'scope_kind':scope,'community_id':cid}
        if bid:data['building_id']=bid
        return self.call('staff.create',data)['id']
    def grant(self,uid=1):
        token=secrets.token_urlsafe(32)
        with self.factory() as db:
            u=db.get(User,uid);db.add(AiGrant(id=str(uuid.uuid4()),user_id=uid,auth_version=u.auth_version,token_hash=hashlib.sha256(token.encode()).hexdigest(),expires_at=utcnow()+timedelta(minutes=3)));db.commit()
        return token
    def tool(self,command,args,uid=1,op='execute',expected=200,token=None):
        r=self.app.test_client().post('/api/agent/tools',json={'request_token':token or self.grant(uid),'operation':op,'command':command,'arguments_json':json.dumps(args,ensure_ascii=False)})
        self.assertEqual(r.status_code,expected,r.text[:1800]);return r.json
    def test_admin_property_owner_relationship_persists_after_reopen(self):
        b,u,h=self.setup_house();pid=self.person();r=self.call('relation.bind',{'house_id':h,'person_id':pid,'kind':'owner'})
        self.assertEqual(r['record']['kind'],'owner')
        data=self.client.get(f'/api/manage/houses/{h}').json;self.assertEqual(len(data['related']['房屋人员关系']),1)
        self.app.extensions['db_engine'].dispose()
        new=create_app({'TESTING':True,'DATABASE_URL':self.url,'SECRET_KEY':'fixture','UPLOAD_FOLDER':self.temp.name})
        with new.extensions['db_session']() as db:self.assertEqual(db.scalar(select(func.count(HousePerson.id))),1)
        new.extensions['db_engine'].dispose()
    def test_agent_direct_bind_repeat_lookup_and_ambiguity(self):
        b,u,h=self.setup_house();p=self.person();args={'building_name':'23栋','room_no':311,'person_name':'王五'}
        receipt=self.tool('relation.bind_by_name',args);self.assertEqual(receipt['status'],'executed')
        self.tool('relation.bind_by_name',args)
        with self.factory() as db:self.assertEqual(db.scalar(select(func.count(HousePerson.id))),1);self.assertTrue(db.scalar(select(AuditLog.id).where(AuditLog.source=='agent',AuditLog.action=='relation.bind_by_name')))
        result=self.tool('person.properties',{'person_name':'王五'},op='lookup');self.assertEqual(result['items'][0]['id'],h)
        self.setup_house('24栋');self.tool('relation.bind_by_name',{'room_no':311,'person_name':'王五'},expected=409)
        self.person('王五','13800000456');self.tool('relation.bind_by_name',args,expected=409)
        self.tool('relation.bind_by_name',{**args,'phone':'13800000123'})
    def test_agent_missing_entities_and_no_permission(self):
        self.setup_house();uid=self.staff('worker','engineer','assigned')
        self.tool('property.archive',{'kind':'building','id':1,'version':1,'reason':'删除23栋'},uid=uid,expected=403)
        self.tool('relation.bind_by_name',{'building_name':'23栋','room_no':311,'person_name':'不存在'},expected=404)
        self.tool('relation.bind_by_name',{'building_name':'不存在','room_no':999,'person_name':'王五'},expected=404)
        self.tool('staff.roles',{'id':uid,'auth_version':2,'role_codes':['superadmin'],'scope_kind':'all','reason':'提权'},uid=uid,expected=403)
    def test_agent_query_common_aliases_are_scoped(self):
        building, _, house = self.setup_house(); self.person()
        houses = self.tool('house.search', {'building_id': building, 'id': house}, op='lookup')
        self.assertEqual([row['id'] for row in houses['items']], [house])
        people = self.tool('person.search', {'name': '王五'}, op='lookup')
        self.assertEqual([row['name'] for row in people['items']], ['王五'])
    def test_customer_dispatch_engineer_complete_and_verify(self):
        b,u,h=self.setup_house();pid=self.person();self.call('relation.bind',{'house_id':h,'person_id':pid,'kind':'owner'})
        cs=self.staff('service','customer_service');worker=self.staff('engineer','engineer','assigned');other=self.staff('other','engineer','assigned')
        self.login('service');o=self.call('order.create',{'house_id':h,'requester_person_id':pid,'title':'厨房漏水','content':'厨房管道接头漏水','type':'水电故障','location':'23栋3单元311厨房','contact_name':'王五','contact_phone':'13800000123'})['id']
        self.call('order.assign',{'id':o,'version':1,'repairer_id':worker})
        self.login('other');self.assertEqual(self.client.get(f'/api/manage/work-orders/{o}').status_code,404)
        self.login('engineer');self.call('order.finish',{'id':o,'version':2,'remark':'尚未接单'},expected=409)
        self.call('order.accept',{'id':o,'version':2});self.call('order.progress',{'id':o,'version':3,'remark':'已更换接头并试压'})
        version=self.record(WorkOrder,o).version;self.call('order.finish',{'id':o,'version':version,'remark':'更换密封垫并试水20分钟，无渗漏'})
        self.login('service');self.call('order.close',{'id':o,'version':self.record(WorkOrder,o).version,'remark':'电话回访王五确认无渗漏，认可完成'})
        self.assertEqual(self.record(WorkOrder,o).status,4)
    def test_coowners_family_tenant_checkout_and_history(self):
        b,u,h=self.setup_house();p1=self.person();p2=self.person('李四','13800000124');tenant=self.person('赵六','13800000125')
        for pid in [p1,p2]:self.call('relation.bind',{'house_id':h,'person_id':pid,'kind':'owner'})
        self.call('relation.bind',{'house_id':h,'person_id':tenant,'kind':'family'})
        today=date.today();lease=self.call('lease.create',{'house_id':h,'person_ids':[tenant],'start_date':today.isoformat(),'end_date':(today+timedelta(days=365)).isoformat(),'move_in':utcnow().isoformat()+'Z'})['id']
        self.assertEqual(self.record(House,h).occupancy,'rented')
        self.call('lease.create',{'house_id':h,'person_ids':[tenant],'start_date':today.isoformat(),'end_date':(today+timedelta(days=365)).isoformat(),'move_in':utcnow().isoformat()+'Z'},expected=409)
        self.call('lease.checkout',{'id':lease,'version':1,'reason':'租期提前结束，双方确认交接'})
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(HousePerson.id)).where(HousePerson.kind=='owner',HousePerson.status=='active')),2)
            self.assertEqual(db.scalar(select(HousePerson.status).where(HousePerson.kind=='tenant')),'ended')
        self.call('lease.checkout',{'id':lease,'version':2,'reason':'重复退租'},expected=409)
    def test_building_data_scope_and_legacy_route_no_bypass(self):
        b,u,h=self.setup_house();b2,u2,h2=self.setup_house('24栋');self.person();uid=self.staff('building_staff','building_manager','building',bid=b)
        self.login('building_staff')
        self.assertEqual([r['id'] for r in self.client.get('/api/manage/houses').json['items']],[h])
        self.assertEqual(self.client.get(f'/api/manage/houses/{h2}').status_code,404)
        self.call('house.save',{'id':h2,'version':1,'room_no':311,'area':100,'usage':'residential'},expected=404)
        self.assertEqual(self.client.get('/houses').status_code,403)
        self.call('notice.save',{'community_id':1,'title':'越范围公告','content':'不应成功'},expected=403)
        self.tool('house.search',{},uid=uid,op='lookup')
    def test_resident_only_current_relations(self):
        b,u,h=self.setup_house();b2,u2,h2=self.setup_house('24栋');pid=self.person();uid=self.staff('resident','resident','self')
        self.call('person.save',{'id':pid,'version':1,'name':'王五','phone':'13800000123','user_id':uid})
        rel=self.call('relation.bind',{'house_id':h,'person_id':pid,'kind':'owner'})['id']
        self.login('resident');self.assertEqual(len(self.client.get('/api/manage/houses').json['items']),1)
        self.assertEqual(self.client.get(f'/api/manage/houses/{h2}').status_code,404)
        self.call('relation.bind',{'house_id':h2,'person_id':pid,'kind':'owner'},expected=403)
        self.login('admin');self.call('relation.end',{'id':rel,'version':1,'reason':'产权核验变更'})
        self.login('resident');self.assertEqual(self.client.get('/api/manage/houses').json['items'],[])
    def test_money_calculation_partial_full_reversal_and_mock_isolation(self):
        b,u,h=self.setup_house();fid=self.call('fee.save',{'community_id':1,'name':'物业费','basis':'area','rate':'2.3456'})['id']
        bill=self.call('bill.create',{'house_id':h,'fee_item_id':fid,'period':'2026-09','due_date':'2026-09-30'})['id']
        self.assertEqual(self.record(Bill,bill).amount_cents,23456)
        self.call('bill.create',{'house_id':h,'fee_item_id':fid,'period':'2026-09','due_date':'2026-09-30'},expected=409)
        pay=self.call('payment.record',{'bill_id':bill,'version':1,'amount':'100','channel':'cash','reference':'receipt-001'})['id']
        self.assertEqual(self.record(Bill,bill).status,'partial')
        self.call('payment.record',{'bill_id':bill,'version':2,'amount':'999','channel':'bank','reference':'bank-001'},expected=409)
        self.call('payment.reverse',{'id':pay,'version':1,'reason':'收据填写错误，现金已核对'})
        self.assertEqual(self.record(Bill,bill).paid_cents,0)
        self.call('payment.record',{'bill_id':bill,'version':self.record(Bill,bill).version,'amount':'234.56','channel':'bank','reference':'bank-002'})
        self.assertEqual(self.record(Bill,bill).status,'paid')
        report=self.client.get('/api/reports/finance').json;self.assertEqual(report['unpaid_cents'],0)
        self.app.config['APP_ENV']='production'
        self.call('payment.record',{'bill_id':bill,'version':self.record(Bill,bill).version,'amount':'1','channel':'mock','reference':'m1'},expected=503)
    def test_finance_cannot_edit_properties_or_roles(self):
        b,u,h=self.setup_house();self.staff('finance','finance');self.login('finance')
        self.call('property.archive',{'kind':'building','id':b,'version':1,'reason':'不允许'},expected=403)
        self.assertEqual(self.client.get('/api/manage/staff').status_code,403)
        self.assertEqual(self.client.get('/api/reports/finance').status_code,200)
    def test_high_risk_agent_pending_confirm_rechecks_version(self):
        b,u,h=self.setup_house();pid=self.person();rel=self.call('relation.bind',{'house_id':h,'person_id':pid,'kind':'owner'})['id']
        pending=self.tool('relation.end',{'id':rel,'version':1,'reason':'已核验产权变更'});self.assertEqual(pending['status'],'pending');self.assertEqual(self.record(HousePerson,rel).status,'active')
        r=self.client.post('/ai/actions/'+pending['id']+'/confirm',json={},headers={'X-CSRF-Token':self.csrf()});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.record(HousePerson,rel).status,'ended')
        r=self.client.post('/ai/actions/'+pending['id']+'/confirm',json={},headers={'X-CSRF-Token':self.csrf()});self.assertEqual(r.status_code,200)
    def test_idempotency_stale_updates_and_failed_attempt_audit(self):
        b,u,h=self.setup_house();data={'community_id':1,'name':'王五','phone':'13800000123'}
        one=self.call('person.save',data,key='test-request-111');two=self.call('person.save',data,key='test-request-111');self.assertEqual(one['id'],two['id'])
        self.call('person.save',{**data,'name':'李四'},key='test-request-111',expected=409)
        update={'id':h,'version':1,'room_no':311,'area':'120','usage':'residential','occupancy':'owner_occupied'}
        self.call('house.save',update);self.call('house.save',update,expected=409)
        self.assertEqual(self.record(House,h).area,120)
        with self.factory() as db:self.assertTrue(db.scalar(select(AuditLog.id).where(AuditLog.status=='failure')))
    def test_complaints_visitors_vehicle_parking_inspection_fault(self):
        b,u,h=self.setup_house();pid=self.person();self.call('relation.bind',{'house_id':h,'person_id':pid,'kind':'owner'})
        cid=self.call('complaint.create',{'house_id':h,'title':'夜间施工噪声','content':'晚上十点仍施工','category':'噪声'})['id']
        self.call('complaint.resolve',{'id':cid,'version':1,'resolution':'已上门告知停止施工并登记装修时间'})
        self.call('complaint.close',{'id':cid,'version':2,'resolution':'住户确认当晚已停止'})
        self.assertEqual(self.client.get('/api/reports/complaints').json['items'][0]['count'],1)
        vid=self.call('visitor.create',{'house_id':h,'host_person_id':pid,'name':'来访亲属','phone':'13800000234','purpose':'探亲','expected_at':utcnow().isoformat()+'Z'})['id']
        self.call('visitor.checkin',{'id':vid,'version':1});self.call('visitor.checkout',{'id':vid,'version':2});self.call('visitor.checkin',{'id':vid,'version':3},expected=409)
        vehicle=self.call('vehicle.save',{'house_id':h,'person_id':pid,'plate':'浙A12345','model':'轿车'})['id']
        space=self.call('parking.save',{'community_id':1,'building_id':b,'code':'B001','location':'23栋地下'})['id']
        use=self.call('parking.assign',{'space_id':space,'vehicle_id':vehicle})['id'];self.call('parking.assign',{'space_id':space,'vehicle_id':vehicle},expected=409)
        self.call('parking.release',{'id':use,'version':1,'reason':'到期解除'})
        device=self.call('device.save',{'community_id':1,'building_id':b,'code':'PUMP01','name':'排水泵','category':'给排水','location':'地库集水井','status':'normal'})['id']
        worker=self.staff('worker','engineer','assigned')
        inspection=self.call('inspection.create',{'device_id':device,'assignee_id':worker,'due_at':(utcnow()+timedelta(hours=4)).isoformat()+'Z','checklist':'检查电源、启停及排水'})['id']
        self.login('worker');self.call('inspection.complete',{'id':inspection,'version':1,'findings':'通电后水泵无法启动','fault':True})
        self.assertIsNotNone(self.record(Inspection,inspection).work_order_id);self.assertEqual(self.record(Device,device).status,'fault')
    def test_all_modules_and_operation_forms_render(self):
        self.setup_house();self.person()
        for name in MODULE_CONFIG:
            with self.subTest(module=name):self.assertEqual(self.client.get('/manage/'+name).status_code,200)
        for command in COMMANDS:
            with self.subTest(command=command):self.assertEqual(self.client.get('/operations/'+command).status_code,200)
    def test_unique_house_per_unit_not_global_address(self):
        self.setup_house();cid=self.call('community.save',{'name':'第二小区','address':'杭州第二路1号','phone':'057188888888'})['id'];self.setup_house(cid=cid)
        self.assertEqual(len(self.client.get('/api/manage/houses').json['items']),2)
    def test_csrf_and_foreign_grant_cannot_impersonate(self):
        self.assertEqual(self.client.post('/api/business/person.save',json={'data':{}}).status_code,400)
        self.assertEqual(self.app.test_client().get('/api/manage/houses').status_code,401)
        self.tool('relation.bind_by_name',{'room_no':311,'person_name':'王五','userId':1},expected=400)
    def test_mock_channel_cannot_run_in_production_even_with_matching_marker(self):
        b,u,h=self.setup_house();f=self.call('fee.save',{'community_id':1,'name':'停车费','basis':'fixed','rate':'300'})['id']
        bill=self.call('bill.create',{'house_id':h,'fee_item_id':f,'period':'2026-09','due_date':'2026-09-30'})['id']
        self.app.config['APP_ENV']='production'
        with self.factory() as db:db.get(SystemSetting,'runtime_environment').value='production';db.commit()
        self.call('payment.record',{'bill_id':bill,'version':1,'amount':'300','channel':'mock','reference':'test'},expected=403)
        self.assertEqual(self.record(Bill,bill).paid_cents,0)
    def test_high_risk_manual_requires_confirmation(self):
        b,u,h=self.setup_house()
        self.call('house.ownership',{'id':h,'version':1,'ownership':'public','reason':'核验'},confirmed=False,expected=400)
        self.assertEqual(self.record(House,h).ownership,'private')
    def test_role_change_invalidates_session_and_records_old_new_scope(self):
        uid=self.staff('service','customer_service');other=self.app.test_client();self.login('service',other)
        auth=self.record(User,uid).auth_version
        self.call('staff.roles',{'id':uid,'auth_version':auth,'role_codes':['finance'],'scope_kind':'community','community_id':1,'reason':'岗位调整，已核验审批'})
        self.assertEqual(other.get('/api/manage/people').status_code,401)
        with self.factory() as db:
            row=db.scalar(select(AuditLog).where(AuditLog.action=='staff.authorization'))
            self.assertIn('customer_service',row.before_data);self.assertIn('finance',row.after_data)
    def test_bill_rows_cannot_leak_across_building_scope(self):
        b,u,h=self.setup_house();b2,u2,h2=self.setup_house('24栋');fid=self.call('fee.save',{'community_id':1,'name':'物业费','basis':'fixed','rate':'100'})['id']
        for house in [h,h2]:self.call('bill.create',{'house_id':house,'fee_item_id':fid,'period':'2026-09','due_date':'2026-09-30'})
        uid=self.staff('scopefinance','finance','building',bid=b);self.login('scopefinance')
        self.assertEqual(self.client.get('/api/reports/finance').json['receivable_cents'],10000)
        self.call('payment.record',{'bill_id':2,'version':1,'amount':'100','channel':'cash','reference':'f1'},expected=404)
        self.call('fee.save',{'id':fid,'version':1,'name':'任意改价','basis':'fixed','rate':'1'},expected=403)
    def test_expired_relation_does_not_authorize_resident(self):
        b,u,h=self.setup_house();p=self.person();uid=self.staff('resident','resident','self')
        self.call('person.save',{'id':p,'version':1,'name':'王五','phone':'13800000123','user_id':uid})
        rel=self.call('relation.bind',{'house_id':h,'person_id':p,'kind':'owner'})['id']
        with self.factory() as db:
            r=db.get(HousePerson,rel);r.start_at=utcnow()-timedelta(days=2);r.end_at=utcnow()-timedelta(days=1);db.commit()
        self.login('resident');self.assertEqual(self.client.get('/api/manage/houses').json['items'],[])
    def test_currency_precision_and_active_equipment_prevent_bad_mutations(self):
        b,u,h=self.setup_house();self.call('house.save',{'id':h,'version':1,'room_no':311,'area':'100.001','usage':'residential'},expected=400)
        free=self.call('building.save',{'community_id':1,'name':'公共设备楼','floors':1})['id']
        self.call('device.save',{'community_id':1,'building_id':free,'code':'ELEC1','name':'总配电柜','category':'供电','location':'一层设备房','status':'normal'})
        self.call('property.archive',{'kind':'building','id':free,'version':1,'reason':'归档'},expected=409)
    def test_batch_monthly_bills_are_atomic_scoped_and_repeat_safe(self):
        b,u,h=self.setup_house();self.call('house.save',{'unit_id':u,'room_no':312,'area':'85','usage':'residential','occupancy':'vacant'})
        fid=self.call('fee.save',{'community_id':1,'name':'批量物业费','basis':'area','rate':'2'})['id']
        data={'fee_item_id':fid,'building_id':b,'period':'2026-09','due_date':'2026-09-30'}
        self.call('bill.batch',data,confirmed=False,expected=400)
        token=self.grant();proposal=self.tool('bill.batch',data,token=token)
        self.assertEqual(proposal['status'],'pending')
        with self.factory() as db:self.assertEqual(db.scalar(select(func.count(Bill.id))),0)
        r=self.client.post('/ai/actions/'+proposal['id']+'/confirm',json={},headers={'X-CSRF-Token':self.csrf()});self.assertEqual(r.status_code,200,r.text)
        self.call('bill.batch',data)
        with self.factory() as db:self.assertEqual(db.scalar(select(func.count(Bill.id))),2);self.assertEqual(db.scalar(select(func.sum(Bill.amount_cents))),37000)
