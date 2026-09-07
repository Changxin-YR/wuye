"""Transactional application service used by web forms, APIs and Dify Tools.
Caller owns commit/rollback. Identifiers, scope and state are rechecked here.
"""
import hashlib,json,re,uuid
from datetime import date,datetime,timedelta,time
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from flask import abort,current_app,has_app_context,has_request_context,request
from sqlalchemy import select,func,or_,event,inspect
from werkzeug.security import generate_password_hash
from models import *
from permissions import Policy,ALL_PERMISSIONS,effective
from services import log_order,notify,transition_status,ORDER_TYPES

# command: required permission, high risk, accepted fields (shared with UI/Agent)
COMMANDS={
 'community.save':('community.manage',False,'id version name address phone'),
 'building.save':('property.write',False,'id version community_id name floors'),
 'unit.save':('property.write',False,'id version building_id name'),
 'house.save':('property.write',False,'id version unit_id room_no area usage occupancy'),
 'house.ownership':('property.write',True,'id version ownership reason'),
 'property.archive':('property.write',True,'kind id version reason'),
 'person.save':('person.write',False,'id version community_id name phone user_id emergency_contact note'),
 'person.archive':('person.write',True,'id version reason'),
 'relation.bind':('relation.write',False,'house_id person_id kind is_resident'),
 'relation.bind_by_name':('relation.write',False,'community_id building_name unit room_no person_name phone'),
 'relation.end':('relation.end',True,'id version reason'),
 'lease.create':('lease.write',False,'house_id person_ids start_date end_date move_in note'),
 'lease.checkout':('lease.write',True,'id version reason'),
 'staff.create':('staff.manage',False,'username password real_name phone role_codes scope_kind community_id building_id'),
 'staff.roles':('rbac.manage',True,'id auth_version role_codes scope_kind community_id building_id reason'),
 'staff.state':('staff.manage',True,'id auth_version active reason'),
 'role.save':('rbac.define',True,'code name permissions reason'),
 'order.create':('order.create',False,'house_id community_id building_id requester_person_id title content type location contact_name contact_phone'),
 'order.assign':('order.dispatch',False,'id version repairer_id'),
 'order.reassign':('order.dispatch',False,'id version repairer_id'),
 'order.accept':('order.work',False,'id version'),
 'order.progress':('order.work',False,'id version remark'),
 'order.finish':('order.work',False,'id version remark'),
 'order.close':('order.verify',False,'id version remark'),
 'order.reopen':('order.verify',False,'id version remark'),
 'order.cancel':('order.cancel',False,'id version remark'),
 'order.evaluate':('order.verify',False,'id version score comment'),
 'complaint.create':('complaint.create',False,'house_id title content category'),
 'complaint.assign':('complaint.handle',False,'id version assignee_id'),
 'complaint.resolve':('complaint.handle',False,'id version resolution'),
 'complaint.close':('complaint.handle',False,'id version resolution'),
 'notice.save':('notice.write',False,'id version community_id building_id title content'),
 'notice.archive':('notice.write',True,'id version reason'),
 'visitor.create':('visitor.write',False,'house_id host_person_id name phone purpose expected_at'),
 'visitor.checkin':('visitor.write',False,'id version'),
 'visitor.checkout':('visitor.write',False,'id version'),
 'visitor.cancel':('visitor.write',False,'id version'),
 'vehicle.save':('vehicle.write',False,'id version house_id person_id plate model'),
 'vehicle.archive':('vehicle.write',True,'id version reason'),
 'parking.save':('parking.write',False,'id version community_id building_id code location'),
 'parking.assign':('parking.write',False,'space_id vehicle_id'),
 'parking.release':('parking.write',True,'id version reason'),
 'device.save':('device.write',False,'id version community_id building_id code name category location status'),
 'device.archive':('device.write',True,'id version reason'),
 'inspection.create':('inspection.assign',False,'device_id assignee_id due_at checklist'),
 'inspection.complete':('inspection.write',False,'id version findings fault'),
 'fee.save':('billing.manage',False,'id version community_id name basis rate'),
 'bill.create':('billing.manage',False,'house_id fee_item_id period due_date'),
 'bill.batch':('billing.manage',True,'fee_item_id building_id period due_date'),
 'bill.void':('billing.manage',True,'id version reason'),
 'payment.record':('billing.collect',True,'bill_id version amount channel reference'),
 'payment.reverse':('billing.reverse',True,'id version reason'),
}
MODULES={Community:'communities',Building:'buildings',PropertyUnit:'units',House:'houses',Person:'people',HousePerson:'relations',Lease:'leases',User:'staff',RbacRole:'roles',WorkOrder:'work-orders',Complaint:'complaints',Notice:'notices',Visitor:'visitors',Vehicle:'vehicles',ParkingSpace:'parking',ParkingUse:'parking-uses',Device:'devices',Inspection:'inspections',FeeItem:'fees',Bill:'bills',Payment:'payments',AuditLog:'audit'}
SECRET={'password','password_hash','token_hash','payload','auth_version','failed_logins','locked_until'}
def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,default=lambda v:v.isoformat() if hasattr(v,'isoformat') else str(v))
def snapshot(obj):return {c.name:getattr(obj,c.name) for c in obj.__table__.columns if c.name not in SECRET}

def audit(db,actor,action,target,before=None,after=None,source='manual',trace_id=None,status='success',error='',obj=None):
    p=Policy(db,actor)
    db.add(AuditLog(operator_id=actor.id,actor_name=actor.username,roles=','.join(sorted(p.roles)),source=source,action=action,target=str(target),resource=obj.__tablename__ if obj is not None else action.split('.')[0],before_data=encode(before) if before is not None else None,after_data=encode(after) if after is not None else None,status=status,error=str(error)[:500],trace_id=trace_id or uuid.uuid4().hex,community_id=getattr(obj,'community_id',None),building_id=getattr(obj,'building_id',None)))

class PropertyService:
    def __init__(self,db,actor,source=None,trace_id=None):
        self.db=db;self.actor=actor;self.policy=Policy(db,actor);self.source=source or ('agent' if has_request_context() and (request.path=='/api/agent/tools' or request.path.startswith('/ai/actions/')) else 'manual');self.trace_id=trace_id or uuid.uuid4().hex
    def run(self,command,data,request_key=None):
        if not isinstance(command,str) or command not in COMMANDS:abort(400,description='未开放的业务操作')
        perm,_,fields=COMMANDS[command];self.policy.require(perm)
        if not isinstance(data,dict) or set(data)-set(fields.split()):abort(400,description='包含未知或不可修改的字段')
        if len(encode(data))>16000:abort(400,description='请求内容过长')
        self.data=data;self.command=command
        digest=hashlib.sha256((command+encode(data)).encode()).hexdigest()
        if request_key:
            if not isinstance(request_key,str) or not re.fullmatch(r'[\w:.-]{8,100}',request_key):abort(400,description='提交编号无效')
            old=self.db.scalar(select(BusinessRequest).where(BusinessRequest.user_id==self.actor.id,BusinessRequest.request_key==request_key))
            if old:
                if old.digest!=digest:abort(409,description='同一提交编号不能用于不同内容，请重新提交')
                return json.loads(old.response)
        captured={}
        def capture(session,flush_context,instances):
            for obj in set(session.new)|set(session.dirty)|set(session.deleted):
                if type(obj) not in MODULES or isinstance(obj,AuditLog):continue
                key=id(obj)
                if key in captured:continue
                state=inspect(obj);before=None
                if obj not in session.new:
                    before=snapshot(obj)
                    for attr in state.attrs:
                        if attr.key in before and attr.history.deleted:before[attr.key]=attr.history.deleted[0]
                captured[key]=(obj,before)
        event.listen(self.db,'before_flush',capture)
        try:
            with self.db.no_autoflush:
                obj,message=getattr(self,'do_'+command.split('.')[0])(command.split('.')[1])
            self.db.flush()
        finally:event.remove(self.db,'before_flush',capture)
        result={'message':message,'traceId':self.trace_id}
        if command=='bill.batch':result['url']='/manage/bills'
        if obj is not None:
            result.update(id=getattr(obj,'id',getattr(obj,'code',None)),record=snapshot(obj),url=f"/manage/{MODULES[type(obj)]}/{getattr(obj,'id',getattr(obj,'code',''))}")
        result=json.loads(encode(result))
        for changed,before in captured.values():audit(self.db,self.actor,command,getattr(changed,'id',getattr(changed,'code','')),before,snapshot(changed),self.source,self.trace_id,obj=changed)
        if not captured:audit(self.db,self.actor,command,result.get('id',''),None,result,self.source,self.trace_id,obj=obj)
        if request_key:self.db.add(BusinessRequest(user_id=self.actor.id,request_key=request_key,digest=digest,response=encode(result)))
        self.db.flush();return result
    def text(self,key,maxlen=100,required=True,choices=None):
        value=self.data.get(key,'')
        if not isinstance(value,str):abort(400,description='文本格式错误：'+key)
        value=value.strip()
        if (required and not value) or len(value)>maxlen or (choices is not None and value not in choices):abort(400,description='请填写有效的'+key)
        return value
    def integer(self,key,required=True):
        v=self.data.get(key)
        if not required and v in (None,''):return None
        if isinstance(v,bool) or not re.fullmatch(r'[1-9][0-9]{0,8}',str(v)):abort(400,description='请选择有效记录或正整数：'+key)
        return int(v)
    def boolean(self,key,default=False):
        v=self.data.get(key,default)
        if v in (True,1,'1','true'):return True
        if v in (False,0,'0','false',''):return False
        abort(400,description='请选择是或否：'+key)
    def decimal(self,key,maxvalue='99999999',positive=True):
        try:v=Decimal(str(self.data.get(key)))
        except InvalidOperation:abort(400,description='金额或面积格式错误')
        precision={'amount':2,'area':2,'rate':4}.get(key,4)
        if v.is_finite() and v.as_tuple().exponent < -precision:abort(400,description=f'{key}最多保留{precision}位小数')
        if not v.is_finite() or v<0 or (positive and v==0) or v>Decimal(maxvalue):abort(400,description='金额或面积超出有效范围')
        return v
    def phone(self,key='phone',required=True):
        v=self.text(key,20,required)
        if v and not re.fullmatch(r'[0-9+() -]{6,20}',v):abort(400,description='请填写有效联系电话')
        return v
    def date(self,key):
        try:return date.fromisoformat(self.text(key,10))
        except ValueError:abort(400,description='日期格式应为年-月-日')
    def moment(self,key,optional=False):
        if optional and not self.data.get(key):return utcnow()
        try:
            v=datetime.fromisoformat(self.text(key,35).replace('Z','+00:00'))
            if v.tzinfo:return v.astimezone(__import__('datetime').timezone.utc).replace(tzinfo=None)
            return v-timedelta(hours=8) # Chinese local forms
        except ValueError:abort(400,description='时间格式无效')
    def ids(self,key):
        v=self.data.get(key)
        if isinstance(v,str):v=[x.strip() for x in v.split(',') if x.strip()]
        if not isinstance(v,list) or not 1<=len(v)<=20 or any(isinstance(x,bool) or not re.fullmatch(r'[1-9][0-9]{0,8}',str(x)) for x in v):abort(400,description='请选择1—20位有效人员')
        return list(dict.fromkeys(map(int,v)))
    def codes(self,key):
        v=self.data.get(key)
        if isinstance(v,str):v=v.split(',')
        if not isinstance(v,list) or not v or any(not isinstance(x,str) for x in v):abort(400,description='请选择有效岗位或权限')
        return set(v)
    def get(self,model,key='id',version=True):
        obj=self.policy.get(model,self.integer(key),True)
        if version and hasattr(obj,'version') and self.integer('version')!=obj.version:abort(409,description='记录已被他人更新，请刷新后重试')
        if hasattr(obj,'updated_by'):obj.updated_by=self.actor.id
        return obj
    def add(self,model,**values):
        if hasattr(model,'created_by'):values.update(created_by=self.actor.id,updated_by=self.actor.id)
        obj=model(**values);self.db.add(obj);return obj
    def scope_values(self):
        cid=self.integer('community_id');self.policy.get(Community,cid)
        bid=self.integer('building_id',False)
        if bid:
            b=self.policy.get(Building,bid)
            if b.community_id!=cid:abort(400,description='楼栋不属于所选小区')
        self.policy.require_scope(cid,bid);return {'community_id':cid,'building_id':bid}
    def house(self,key='house_id'):
        return self.policy.get(House,self.integer(key),True)
    @staticmethod
    def from_house(h):return {'community_id':h.community_id,'building_id':h.building_id,'house_id':h.id}
    def require_person(self,pid,h):
        p=self.policy.get(Person,pid)
        if p.community_id!=h.community_id:abort(400,description='人员不属于该小区，请先核验人员档案')
        return p
    def current_resident(self,pid,h):
        self.require_person(pid,h)
        if not self.db.scalar(select(HousePerson.id).where(HousePerson.house_id==h.id,HousePerson.person_id==pid,effective())):abort(400,description='所选人员目前未与该房屋建立有效关系')
    def worker(self,uid,permission,cid,bid):
        u=self.policy.get(User,uid)
        p=Policy(self.db,u)
        if not u.active or not p.has(permission) or not p.within(cid,bid):abort(400,description='所选工作人员未启用、岗位不符或不负责该区域')
        return u
    def do_community(self,action):
        o=self.get(Community) if self.data.get('id') else self.add(Community)
        if o.id:self.policy.require_scope(o.id)
        elif not self.policy.super:abort(403)
        o.name=self.text('name',80);o.address=self.text('address',200);o.phone=self.phone(required=False)
        return o,'小区资料已保存'
    def do_building(self,action):
        if self.data.get('id'):
            o=self.get(Building);self.policy.require_scope(o.community_id,o.id)
        else:
            cid=self.integer('community_id');self.policy.get(Community,cid);self.policy.require_scope(cid);o=self.add(Building,community_id=cid)
        o.name=self.text('name',50);o.floors=self.integer('floors')
        if o.floors>300:abort(400,description='楼层数量不合理')
        self.db.flush()
        for h in self.db.scalars(select(House).where(House.building_id==o.id)):h.building_name=o.name
        return o,'楼栋已保存'
    def do_unit(self,action):
        if self.data.get('id'):o=self.get(PropertyUnit);self.policy.require_scope(o.community_id,o.building_id)
        else:
            b=self.policy.get(Building,self.integer('building_id'));self.policy.require_scope(b.community_id,b.id);o=self.add(PropertyUnit,community_id=b.community_id,building_id=b.id)
        o.name=self.text('name',20);self.db.flush()
        for h in self.db.scalars(select(House).where(House.unit_id==o.id)):h.unit=o.name
        return o,'单元已保存'
    def do_house(self,action):
        if action=='ownership':
            o=self.get(House);self.policy.require_scope(o.community_id,o.building_id);self.text('reason',300)
            o.ownership=self.text('ownership',20,choices={'private','public','unknown'});return o,'产权状态已核验变更'
        if self.data.get('id'):o=self.get(House)
        else:
            unit=self.policy.get(PropertyUnit,self.integer('unit_id'));b=self.policy.get(Building,unit.building_id)
            o=House(community_id=unit.community_id,building_id=b.id,unit_id=unit.id,building_name=b.name,unit=unit.name);self.db.add(o)
        self.policy.require_scope(o.community_id,o.building_id)
        o.room_no=self.integer('room_no');o.area=self.decimal('area','100000');o.usage=self.text('usage',20,choices={'residential','commercial','office'})
        requested=self.text('occupancy',20,False) or 'vacant'
        if requested not in {'vacant','owner_occupied','renovating'}:abort(400,description='出租状态由有效租赁记录自动确定')
        if not self.db.scalar(select(Lease.id).where(Lease.house_id==o.id,Lease.status=='active',Lease.end_date>=(utcnow()+timedelta(hours=8)).date())):o.occupancy=requested
        return o,'房屋资料已保存'
    def do_property(self,action):
        cls={'community':Community,'building':Building,'unit':PropertyUnit,'house':House}.get(self.text('kind',20))
        if not cls:abort(400)
        if cls is Community:self.policy.require('community.manage')
        o=self.get(cls);self.text('reason',300)
        self.policy.require_scope(o.id if cls is Community else o.community_id,o.id if cls is Building else getattr(o,'building_id',None))
        children={Community:[(Building,Building.community_id),(Person,Person.community_id),(Device,Device.community_id),(ParkingSpace,ParkingSpace.community_id),(FeeItem,FeeItem.community_id),(Notice,Notice.community_id)],Building:[(PropertyUnit,PropertyUnit.building_id),(Device,Device.building_id),(ParkingSpace,ParkingSpace.building_id),(Notice,Notice.building_id)],PropertyUnit:[(House,House.unit_id)],House:[(HousePerson,HousePerson.house_id),(Lease,Lease.house_id),(WorkOrder,WorkOrder.house_id),(Bill,Bill.house_id),(Vehicle,Vehicle.house_id),(Complaint,Complaint.house_id)]}[cls]
        for child,col in children:
            q=select(child.id).where(col==o.id)
            if child is HousePerson:q=q.where(effective())
            elif child is Lease:q=q.where(Lease.status=='active')
            elif child is WorkOrder:q=q.where(WorkOrder.status.in_([0,1,2,3]))
            elif child is Bill:q=q.where(Bill.status.in_(['unpaid','partial']))
            elif child is Complaint:q=q.where(Complaint.status!='closed')
            elif hasattr(child,'deleted'):q=q.where(child.deleted.is_(False))
            if self.db.scalar(q.limit(1)):abort(409,description='仍有关联业务或有效记录，请先处理后归档')
        o.deleted=True;return o,'记录已归档，历史业务保留'
    def do_person(self,action):
        o=self.get(Person) if self.data.get('id') else None
        if action=='archive':
            self.text('reason',300)
            if self.db.scalar(select(HousePerson.id).where(HousePerson.person_id==o.id,effective())):abort(409,description='人员仍有有效房屋关系，请先核验解除')
            o.deleted=True;return o,'人员档案已归档'
        if not o:
            cid=self.integer('community_id');self.policy.get(Community,cid)
            if not self.policy.within(cid) and not any(s.community_id==cid and s.kind=='building' for s in self.policy.scopes):abort(403)
            o=self.add(Person,community_id=cid)
        o.name=self.text('name',50);o.phone=self.phone();o.emergency_contact=self.text('emergency_contact',100,False);o.note=self.text('note',500,False)
        uid=self.integer('user_id',False)
        if uid:
            u=self.db.get(User,uid)
            if not u or not u.active or 'resident' not in Policy(self.db,u).roles:abort(400,description='仅可关联已注册的有效住户账号')
            if self.db.scalar(select(Person.id).where(Person.user_id==uid,Person.id!=(o.id or 0))):abort(409,description='该账号已关联其他人员档案')
            if o.user_id and o.user_id!=uid:abort(409,description='该人员已关联账号，不能通过普通资料编辑更换身份')
            o.user_id=uid;u.real_name=o.name;u.phone=o.phone
        return o,'人员档案已保存'
    def bind(self,h,p,kind,resident=True,lease=None):
        self.policy.require_scope(h.community_id,h.building_id)
        key=f'{h.id}:{p.id}:{kind}'
        old=self.db.scalar(select(HousePerson).where(HousePerson.active_key==key).with_for_update())
        if old and (not old.end_at or old.end_at>utcnow()):return old,'该关系已存在，未重复登记'
        if old:old.active_key=None;old.status='ended';self.db.flush()
        rel=self.add(HousePerson,**self.from_house(h),person_id=p.id,kind=kind,is_resident=resident,active_key=key,lease_id=lease.id if lease else None,start_at=lease.move_in if lease else utcnow(),end_at=datetime.combine(lease.end_date+timedelta(days=1),time.min)-timedelta(hours=8) if lease else None)
        if kind=='owner' and not h.owner_id and p.user_id:h.owner_id=p.user_id
        return rel,'房屋人员关系已登记'
    def do_relation(self,action):
        if action=='end':
            r=self.get(HousePerson);h=self.policy.get(House,r.house_id,True)
            if r.kind=='tenant':abort(409,description='租户关系请通过退租流程结束')
            if r.status!='active':abort(409,description='该关系已经结束')
            r.reason=self.text('reason',300);r.end_at=utcnow();r.status='ended';r.active_key=None;self.db.flush()
            owners=self.db.scalars(select(Person.user_id).join(HousePerson,HousePerson.person_id==Person.id).where(HousePerson.house_id==h.id,HousePerson.kind=='owner',effective(),Person.user_id.is_not(None))).all()
            h.owner_id=owners[0] if owners else None;return r,'关系已结束，历史记录保留'
        if action=='bind_by_name':
            q=self.policy.query(House).where(House.room_no==self.integer('room_no'))
            for key,col in [('building_name',House.building_name),('unit',House.unit)]:
                if self.data.get(key):q=q.where(col==self.text(key,50))
            if self.data.get('community_id'):q=q.where(House.community_id==self.integer('community_id'))
            houses=list(self.db.scalars(q.with_for_update().limit(2)))
            if not houses:abort(404,description='没有找到可访问的房屋，请核对小区、楼栋、单元和房号')
            if len(houses)>1:abort(409,description='存在多套同号房屋，请补充小区、楼栋或单元')
            h=houses[0];q=self.policy.query(Person).where(Person.community_id==h.community_id,Person.name==self.text('person_name',50))
            if self.data.get('phone'):q=q.where(Person.phone==self.phone())
            persons=list(self.db.scalars(q.limit(2)))
            if not persons:abort(404,description='没有找到该人员，请先核验并登记人员档案')
            if len(persons)>1:abort(409,description='存在同名人员，请补充联系电话以确认身份')
            r,msg=self.bind(h,persons[0],'owner');return r,msg+f'：{h.building_name} {h.unit} {h.room_no}，{persons[0].name}'
        h=self.house();p=self.require_person(self.integer('person_id'),h);kind=self.text('kind',20,choices={'owner','family','contact'})
        return self.bind(h,p,kind,self.boolean('is_resident',True))
    def do_lease(self,action):
        if action=='checkout':
            o=self.get(Lease);h=self.policy.get(House,o.house_id,True);reason=self.text('reason',300)
            if o.status!='active':abort(409,description='该租赁已结束')
            o.status='ended';o.move_out=utcnow();o.active_key=None;o.note=(o.note+'；退租：'+reason)[:500]
            for r in self.db.scalars(select(HousePerson).where(HousePerson.lease_id==o.id,HousePerson.status=='active')):r.status='ended';r.end_at=min(r.end_at or utcnow(),utcnow());r.active_key=None;r.reason=reason;r.updated_by=self.actor.id
            h.occupancy='vacant';return o,'退租已登记，产权关系保留'
        h=self.house();self.policy.require_scope(h.community_id,h.building_id)
        start=self.date('start_date');end=self.date('end_date');move=self.moment('move_in')
        if end<start or end<(utcnow()+timedelta(hours=8)).date() or not start<= (move+timedelta(hours=8)).date() <=end or move>utcnow()+timedelta(minutes=5):abort(400,description='租赁日期不合理；入住登记须为已经发生的入住')
        old=self.db.scalar(select(Lease).where(Lease.active_key==h.id).with_for_update())
        if old:abort(409,description='房屋仍有未结束租赁，请先办理退租')
        people=[self.require_person(pid,h) for pid in self.ids('person_ids')]
        o=self.add(Lease,**self.from_house(h),start_date=start,end_date=end,move_in=move,active_key=h.id,note=self.text('note',500,False));self.db.flush()
        for p in people:self.bind(h,p,'tenant',True,o)
        h.occupancy='rented';return o,'租户入住已登记，产权业主保留'
    def staff_scope(self):
        kind=self.text('scope_kind',20,choices={'all','community','building','assigned','self'})
        cid=self.integer('community_id',False);bid=self.integer('building_id',False)
        if kind=='all':
            if not self.policy.super:abort(403)
            return {'kind':kind,'community_id':None,'building_id':None}
        if not cid:abort(400,description='请选择负责的小区')
        self.policy.get(Community,cid)
        if kind=='building':
            b=self.policy.get(Building,bid)
            if b.community_id!=cid:abort(400,description='楼栋与小区不匹配')
        else:bid=None
        self.policy.require_scope(cid,bid)
        return {'kind':kind,'community_id':cid,'building_id':bid}
    def set_roles(self,u,codes,scope):
        if len(codes)>10:abort(400)
        roles=list(self.db.scalars(select(RbacRole).where(RbacRole.code.in_(codes))))
        if len(roles)!=len(codes):abort(400,description='岗位不存在')
        granted=set(self.db.scalars(select(RolePermission.permission).where(RolePermission.role_code.in_(codes))))
        if not self.policy.super and not (granted-{'resident.self'})<=self.policy.permissions:abort(403,description='不能分配超过自身权限的岗位')
        if 'superadmin' in codes and not self.policy.super:abort(403)
        if 'resident' in codes and codes!={'resident'}:abort(400,description='住户身份须与工作人员账号分开')
        if codes=={'resident'} and scope['kind']!='self':abort(400,description='住户只能使用本人数据范围')
        if 'superadmin' in codes and scope['kind']!='all':abort(400,description='超级管理员须明确选择全域范围')
        for row in self.db.scalars(select(UserRole).where(UserRole.user_id==u.id)):self.db.delete(row)
        for row in self.db.scalars(select(UserScope).where(UserScope.user_id==u.id)):self.db.delete(row)
        self.db.flush()
        for code in codes:self.db.add(UserRole(user_id=u.id,role_code=code))
        self.db.add(UserScope(user_id=u.id,**scope))
        # Legacy integer is presentation compatibility only; authorization uses memberships.
        u.role=0 if 'superadmin' in codes else (1 if 'order.work' in granted else 2)
        u.auth_version+=1
    def do_staff(self,action):
        if action=='create':
            username=self.text('username',50)
            if not re.fullmatch(r'[\w.-]{3,50}',username):abort(400,description='账号应为3—50位字母、数字、中文或._-')
            password=self.text('password',128)
            if len(password)<8:abort(400,description='密码至少8位')
            codes=self.codes('role_codes');scope=self.staff_scope()
            u=User(username=username,password_hash=generate_password_hash(password),real_name=self.text('real_name',50),phone=self.phone(),role=2)
            self.db.add(u);self.db.flush();self.set_roles(u,codes,scope)
            return u,'工作人员账号已创建'
        u=self.get(User,version=False)
        if u.id==self.actor.id:abort(409,description='不能通过此流程修改自己的岗位、数据范围或启停状态')
        if self.integer('auth_version')!=u.auth_version:abort(409,description='账号授权已变化，请刷新')
        self.text('reason',300)
        target=Policy(self.db,u)
        old_identity=target.identity()
        if not self.policy.super and not (target.permissions-{'resident.self'})<=self.policy.permissions:abort(403)
        if not self.policy.super and any(s.kind=='all' or not self.policy.within(s.community_id,s.building_id,True) for s in target.scopes):abort(403,description='不能修改负责范围以外的账号')
        if self.db.scalar(select(WorkOrder.id).where(WorkOrder.repairer_id==u.id,WorkOrder.status.in_([1,2,3]))) or self.db.scalar(select(Inspection.id).where(Inspection.assignee_id==u.id,Inspection.status=='pending')):abort(409,description='该账号还有未完成任务，请先处理或改派')
        if action=='roles':self.set_roles(u,self.codes('role_codes'),self.staff_scope())
        else:
            u.active=self.boolean('active');u.auth_version+=1;u.failed_logins=0;u.locked_until=None
        self.db.flush()
        audit(self.db,self.actor,'staff.authorization',u.id,old_identity,Policy(self.db,u).identity(),self.source,self.trace_id,obj=u)
        return u,'账号权限或状态已更新，原会话已失效'
    def do_role(self,action):
        code=self.text('code',40)
        if not re.fullmatch(r'[a-z][a-z0-9_]{2,39}',code):abort(400,description='岗位编码须为小写英文、数字和下划线')
        o=self.db.get(RbacRole,code)
        if o and o.builtin:abort(409,description='内置岗位不可直接改写，请新建自定义岗位')
        permissions=self.codes('permissions')
        if not permissions<=ALL_PERMISSIONS or not permissions<=self.policy.permissions or 'resident.self' in permissions:abort(400,description='包含不可授予的权限')
        self.text('reason',300)
        if not o:o=RbacRole(code=code,builtin=False);self.db.add(o)
        o.name=self.text('name',50);self.db.flush()
        before=list(self.db.scalars(select(RolePermission.permission).where(RolePermission.role_code==code)))
        for row in self.db.scalars(select(RolePermission).where(RolePermission.role_code==code)):self.db.delete(row)
        self.db.flush()
        for p in permissions:self.db.add(RolePermission(role_code=code,permission=p))
        for u in self.db.scalars(select(User).join(UserRole,UserRole.user_id==User.id).where(UserRole.role_code==code)):u.auth_version+=1
        audit(self.db,self.actor,'role.permissions',code,before,sorted(permissions),self.source,self.trace_id)
        return o,'岗位权限已保存，相关账号须重新登录'
    def do_order(self,action):
        if action=='create':
            h=self.house() if self.data.get('house_id') else None
            if h:
                scope=self.from_house(h);scope.pop('house_id');pid=self.integer('requester_person_id',False)
                if pid:self.current_resident(pid,h)
            else:
                if self.policy.resident:
                    cid=self.integer('community_id');self.policy.get(Community,cid);scope={'community_id':cid,'building_id':None}
                else:scope=self.scope_values()
                pid=None
            title=self.text('title',100);content=self.text('content',2000);kind=self.text('type',30,choices=ORDER_TYPES)
            if not h and kind!='公共设施':abort(400,description='室内报修必须选择房屋')
            loc=self.text('location',200);contact=self.text('contact_name',50);phone=self.phone('contact_phone')
            old=self.db.scalar(select(WorkOrder).where(WorkOrder.owner_id==self.actor.id,WorkOrder.house_id==(h.id if h else None),WorkOrder.title==title,WorkOrder.content==content,WorkOrder.status.in_([0,1,2,3]),WorkOrder.created_at>utcnow()-timedelta(minutes=5)).limit(1))
            if old:return old,'相同报修刚刚提交，已返回原工单'
            o=WorkOrder(**scope,house_id=h.id if h else None,requester_person_id=pid,owner_id=self.actor.id,order_no=uuid.uuid4().hex,title=title,content=content,type=kind,location=loc,contact_name=contact,contact_phone=phone)
            self.db.add(o);self.db.flush();log_order(self.db,o,self.actor.id,'提交报修')
            managers=[u.id for u in self.db.scalars(select(User).where(User.active.is_(True))) if Policy(self.db,u).has('order.dispatch') and Policy(self.db,u).within(o.community_id,o.building_id)]
            notify(self.db,managers,'新报修：'+title,o.id);return o,'报修工单已创建'
        o=self.get(WorkOrder);own=o.owner_id==self.actor.id or (o.requester_person_id and self.db.scalar(select(Person.id).where(Person.id==o.requester_person_id,Person.user_id==self.actor.id)))
        if action in {'assign','reassign'}:
            self.policy.require_scope(o.community_id,o.building_id)
            expected=0 if action=='assign' else 1
            if o.status!=expected:abort(409,description='仅待派单可派单，未接单可改派')
            u=self.worker(self.integer('repairer_id'),'order.work',o.community_id,o.building_id)
            if o.repairer_id==u.id:abort(409,description='已指派该人员')
            o.repairer_id=u.id
            if action=='assign':transition_status(self.db,o,1,self.actor.id,'派单给 '+u.real_name)
            else:log_order(self.db,o,self.actor.id,'改派给 '+u.real_name);o.updated_at=utcnow()
        elif action in {'accept','progress','finish'}:
            if o.repairer_id!=self.actor.id:abort(403)
            if action=='accept':transition_status(self.db,o,2,self.actor.id,'维修人员接单')
            else:
                if o.status!=2:abort(409,description='工单不在维修中')
                remark=self.text('remark',240)
                if action=='finish':o.resolution=remark;transition_status(self.db,o,3,self.actor.id,'完工：'+remark)
                else:log_order(self.db,o,self.actor.id,remark);o.updated_at=utcnow()
        elif action in {'close','reopen','evaluate'}:
            if not own and self.policy.resident:abort(403)
            if not own:self.policy.require_scope(o.community_id,o.building_id)
            if action=='evaluate':
                if not own:abort(403)
                score=self.integer('score')
                if o.status!=4 or score>5:abort(409,description='只能对已验收工单评分1—5')
                if self.db.scalar(select(Evaluation.id).where(Evaluation.order_id==o.id)):abort(409,description='已经评价过该工单')
                self.db.add(Evaluation(order_id=o.id,score=score,comment=self.text('comment',200,False)));log_order(self.db,o,self.actor.id,f'评价{score}分')
            else:
                if o.status!=3:abort(409,description='工单尚未进入待验收状态')
                remark=self.text('remark',230,action=='reopen' or not own) or '住户确认验收'
                transition_status(self.db,o,4 if action=='close' else 2,self.actor.id,remark)
        elif action=='cancel':
            if not own:self.policy.require_scope(o.community_id,o.building_id)
            if o.status not in {0,1}:abort(409,description='已开始维修，不能直接取消')
            transition_status(self.db,o,5,self.actor.id,self.text('remark',240))
        notify(self.db,[o.owner_id,o.repairer_id],'工单进展：'+o.title,o.id)
        return o,'工单处理结果已保存'
    def do_complaint(self,action):
        if action=='create':
            h=self.house();o=self.add(Complaint,**self.from_house(h),reporter_id=self.actor.id,title=self.text('title',100),content=self.text('content',5000),category=self.text('category',30))
        else:
            o=self.get(Complaint);self.policy.require_scope(o.community_id,o.building_id)
            if action=='assign':
                if o.status not in {'open','assigned'}:abort(409,description='投诉已处理，不能再派单')
                u=self.worker(self.integer('assignee_id'),'complaint.handle',o.community_id,o.building_id);o.assignee_id=u.id;o.status='assigned'
            elif action=='resolve':
                if o.status not in {'open','assigned'}:abort(409,description='投诉状态不允许重复处理')
                o.resolution=self.text('resolution',1000);o.status='resolved'
            else:
                if o.status!='resolved':abort(409,description='请先填写处理结果，再回访结案')
                o.resolution=(o.resolution+'；回访：'+self.text('resolution',500))[:1000];o.status='closed'
        return o,'投诉处理记录已保存'
    def do_notice(self,action):
        if self.data.get('id'):
            o=self.get(Notice);self.policy.require_scope(o.community_id,o.building_id)
        else:o=Notice(**self.scope_values());self.db.add(o)
        if action=='archive':self.text('reason',300);o.deleted=True
        else:o.title=self.text('title',100);o.content=self.text('content',5000)
        return o,'公告已保存'
    def do_visitor(self,action):
        if action=='create':
            h=self.house();pid=self.integer('host_person_id');self.current_resident(pid,h)
            o=self.add(Visitor,**self.from_house(h),host_person_id=pid,name=self.text('name',50),phone=self.phone(),purpose=self.text('purpose',200),expected_at=self.moment('expected_at'))
        else:
            o=self.get(Visitor)
            expected={'checkin':'registered','checkout':'inside','cancel':'registered'}[action]
            if o.status!=expected:abort(409,description='访客当前状态不允许该操作')
            o.status={'checkin':'inside','checkout':'left','cancel':'cancelled'}[action]
            if action=='checkin':
                h=self.policy.get(House,o.house_id);self.current_resident(o.host_person_id,h);o.check_in=utcnow()
            elif action=='checkout':o.check_out=utcnow()
        return o,'访客记录已保存'
    def do_vehicle(self,action):
        o=self.get(Vehicle) if self.data.get('id') else None
        if action=='archive':
            self.text('reason',300)
            if self.db.scalar(select(ParkingUse.id).where(ParkingUse.active_vehicle==o.id)):abort(409,description='请先结束该车辆的车位使用关系')
            o.deleted=True;o.status='archived';return o,'车辆已归档'
        h=self.house();pid=self.integer('person_id');self.current_resident(pid,h)
        if o and (o.house_id!=h.id or o.person_id!=pid):abort(409,description='请归档原车辆关系后重新核验登记')
        if not o:o=self.add(Vehicle,**self.from_house(h),person_id=pid)
        plate=self.text('plate',20).upper().replace(' ','')
        if not re.fullmatch(r'[\u4e00-\u9fffA-Z0-9]{5,12}',plate):abort(400,description='车牌格式不正确')
        o.plate=plate;o.model=self.text('model',50,False);return o,'车辆资料已保存'
    def do_parking(self,action):
        if action=='save':
            if self.data.get('id'):o=self.get(ParkingSpace)
            else:o=self.add(ParkingSpace,**self.scope_values())
            o.code=self.text('code',50);o.location=self.text('location',200);return o,'车位已保存'
        if action=='assign':
            space=self.policy.get(ParkingSpace,self.integer('space_id'),True);v=self.policy.get(Vehicle,self.integer('vehicle_id'),True)
            if space.community_id!=v.community_id:abort(400,description='车辆与车位不属于同一小区')
            if space.status!='available' or self.db.scalar(select(ParkingUse.id).where(or_(ParkingUse.active_space==space.id,ParkingUse.active_vehicle==v.id))):abort(409,description='车位或车辆已存在有效使用关系')
            o=self.add(ParkingUse,community_id=space.community_id,building_id=space.building_id,space_id=space.id,vehicle_id=v.id,active_space=space.id,active_vehicle=v.id);space.status='occupied'
        else:
            o=self.get(ParkingUse);self.text('reason',300)
            if o.status!='active':abort(409,description='车位使用关系已结束')
            space=self.policy.get(ParkingSpace,o.space_id,True);o.status='ended';o.end_at=utcnow();o.active_space=None;o.active_vehicle=None;space.status='available'
        return o,'车位使用关系已更新'
    def do_device(self,action):
        if self.data.get('id'):o=self.get(Device)
        else:o=self.add(Device,**self.scope_values())
        if action=='archive':
            self.text('reason',300)
            if self.db.scalar(select(Inspection.id).where(Inspection.device_id==o.id,Inspection.status=='pending')):abort(409,description='设施还有未完成巡检')
            o.deleted=True
        else:
            o.code=self.text('code',50);o.name=self.text('name',100);o.category=self.text('category',30);o.location=self.text('location',200);o.status=self.text('status',20,choices={'normal','fault','maintenance','retired'})
        return o,'设备设施记录已保存'
    def do_inspection(self,action):
        if action=='create':
            d=self.policy.get(Device,self.integer('device_id'));u=self.worker(self.integer('assignee_id'),'inspection.write',d.community_id,d.building_id)
            o=self.add(Inspection,community_id=d.community_id,building_id=d.building_id,device_id=d.id,assignee_id=u.id,due_at=self.moment('due_at'),checklist=self.text('checklist',1000))
        else:
            o=self.get(Inspection)
            if o.assignee_id!=self.actor.id:abort(403,description='只能提交本人负责的巡检结果')
            if o.status!='pending':abort(409,description='巡检已经完成')
            o.findings=self.text('findings',1000);o.completed_at=utcnow();o.status='completed'
            if self.boolean('fault'):
                d=self.policy.get(Device,o.device_id,True);d.status='fault'
                order=WorkOrder(community_id=o.community_id,building_id=o.building_id,owner_id=self.actor.id,order_no=uuid.uuid4().hex,title='巡检报修：'+d.name[:80],content=o.findings,type='公共设施',location=d.location,contact_name=self.actor.real_name or self.actor.username,contact_phone=self.actor.phone)
                self.db.add(order);self.db.flush();o.work_order_id=order.id;log_order(self.db,order,self.actor.id,'巡检发现故障自动建单')
        return o,'巡检记录已保存'
    def do_fee(self,action):
        if self.data.get('id'):o=self.get(FeeItem);self.policy.require_scope(o.community_id)
        else:
            cid=self.integer('community_id');self.policy.get(Community,cid);self.policy.require_scope(cid);o=self.add(FeeItem,community_id=cid)
        o.name=self.text('name',80);o.basis=self.text('basis',20,choices={'fixed','area'});o.rate=self.decimal('rate','99999999')
        return o,'收费项目已保存；已生成账单金额保持不变'
    def do_bill(self,action):
        if action=='batch':
            fee=self.policy.get(FeeItem,self.integer('fee_item_id'))
            period=self.text('period',7);due=self.date('due_date')
            if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',period):abort(400,description='账期格式应为年-月')
            q=self.policy.query(House).where(House.community_id==fee.community_id)
            if self.data.get('building_id'):
                b=self.policy.get(Building,self.integer('building_id'))
                if b.community_id!=fee.community_id:abort(400,description='楼栋与收费项目不属于同一小区')
                q=q.where(House.building_id==b.id)
            houses=list(self.db.scalars(q.order_by(House.id).with_for_update().limit(501)))
            if not houses:abort(404,description='当前范围没有可开账单的房屋')
            if len(houses)>500:abort(400,description='每批最多500套房屋，请按楼栋分批生成')
            created=skipped=total=0
            for h in houses:
                if self.db.scalar(select(Bill.id).where(Bill.house_id==h.id,Bill.fee_item_id==fee.id,Bill.period==period)):
                    skipped+=1;continue
                amount=(fee.rate*(h.area if fee.basis=='area' else 1)*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP)
                if amount<=0 or amount>2_000_000_000:abort(400,description=f'房屋{h.id}面积或金额无效，本批未生成，请先完善房屋资料')
                self.add(Bill,**self.from_house(h),fee_item_id=fee.id,title=period+' '+fee.name,period=period,due_date=due,amount_cents=int(amount),calculation=encode({'basis':fee.basis,'rate':str(fee.rate),'area':str(h.area),'rounding':'half_up_cent'}))
                created+=1;total+=int(amount)
            return None,f'新增{created}张账单，共{total/100:.2f}元；跳过{skipped}张已存在账单'
        if action=='void':
            o=self.get(Bill)
            if o.paid_cents or o.status=='void':abort(409,description='已收款账单须先核验收款冲销；已作废不能重复作废')
            o.void_reason=self.text('reason',300);o.status='void';return o,'账单已作废，历史记录保留'
        h=self.house();fee=self.policy.get(FeeItem,self.integer('fee_item_id'))
        if fee.community_id!=h.community_id:abort(400,description='收费项目不属于房屋所在小区')
        period=self.text('period',7)
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',period):abort(400,description='账期格式应为年-月')
        amount=(fee.rate*(h.area if fee.basis=='area' else 1)*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP)
        if amount<=0 or amount>2_000_000_000:abort(400,description='房屋面积或收费金额无效')
        o=self.add(Bill,**self.from_house(h),fee_item_id=fee.id,title=period+' '+fee.name,period=period,due_date=self.date('due_date'),amount_cents=int(amount),calculation=encode({'basis':fee.basis,'rate':str(fee.rate),'area':str(h.area),'rounding':'half_up_cent'}))
        return o,'应收账单已生成'
    def environment(self):
        env=current_app.config.get('APP_ENV','production') if has_app_context() else 'production'
        marker=self.db.get(SystemSetting,'runtime_environment')
        if marker and marker.value!=env:abort(503,description='当前运行环境与数据库标记不一致，已禁止收款')
        return env
    def do_payment(self,action):
        env=self.environment()
        if action=='reverse':
            o=self.get(Payment);b=self.policy.get(Bill,o.bill_id,True)
            if o.status!='posted' or o.environment!=env:abort(409,description='收款已冲销或不属于当前环境')
            o.reason=self.text('reason',300);o.status='reversed';o.reversed_at=utcnow();b.paid_cents-=o.amount_cents
        else:
            b=self.get(Bill,'bill_id');channel=self.text('channel',20,choices={'cash','bank','mock'})
            from payment_adapter import validate_receipt
            reference=validate_receipt(channel,self.text('reference',100),env)
            cents=int((self.decimal('amount','20000000')*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
            if cents<=0 or b.status in {'void','paid'} or cents>b.amount_cents-b.paid_cents:abort(409,description='收款金额必须为正且不能超过账单剩余应收金额')
            o=self.add(Payment,community_id=b.community_id,building_id=b.building_id,house_id=b.house_id,bill_id=b.id,amount_cents=cents,channel=channel,reference=reference,environment=env)
            b.paid_cents+=cents
        b.status='paid' if b.paid_cents==b.amount_cents else ('partial' if b.paid_cents else 'unpaid')
        return o,'收款记录已保存' if action=='record' else '收款已冲销，账单余额已恢复；实际资金退款需按原渠道核验'
