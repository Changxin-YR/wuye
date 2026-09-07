"""Business commands shared by HTML forms and authenticated Agent execution.
No model output, request role, or supplied owner identity grants authority.
The caller owns the transaction; commands never commit.
"""
import re
import uuid
from flask import abort
from sqlalchemy import select
from werkzeug.security import generate_password_hash
from models import AuditLog, Evaluation, House, Notice, User, WorkOrder, Person, HousePerson, utcnow
from property_service import PropertyService, COMMANDS as DOMAIN_COMMANDS, snapshot, audit as domain_audit
from permissions import Policy, effective
from services import InvalidTransition, ORDER_TYPES, can_access_order, log_order, notify, notify_admins, transition_status

class BusinessService:
    def __init__(self,db,actor,data):
        self.db=db;self.actor=actor;self.data=data;self.before={}
    def require(self,*roles):
        if not self.actor.active or self.actor.role not in roles:abort(403)
        if roles==(0,) and not Policy(self.db,self.actor).super:abort(403)
    def field(self,name,maxlen,required=False,values=None):
        value=self.data.get(name,'')
        if not isinstance(value,str):abort(400,description='字段类型错误：'+name)
        value=value.strip()
        if (required and not value) or len(value)>maxlen:abort(400,description=f'{name}不能为空或超过{maxlen}字')
        if values is not None and value not in values:abort(400,description='请选择有效的'+name)
        return value
    def number(self,name,minimum=1):
        raw=self.data.get(name,'')
        if isinstance(raw,int) and not isinstance(raw,bool):raw=str(raw)
        if not isinstance(raw,str) or not raw.isascii() or not raw.isdigit() or len(raw)>10 or int(raw)<minimum:abort(400,description='数字参数无效：'+name)
        return int(raw)
    def phone(self,value):
        if not re.fullmatch(r'[0-9+() -]{6,20}',value):abort(400,description='请填写有效联系电话')
        return value
    def password(self,value):
        if not isinstance(value,str) or not 8<=len(value)<=128:abort(400,description='新密码长度须为8—128位')
        return value
    def versioned(self,obj):
        if self.number('version')!=obj.version:abort(409,description='记录已被更新，请刷新后再操作')
    def audit(self,action,target,detail=''):
        model={'house':House,'user':User,'notice':Notice}.get(action.split('_')[0])
        obj=self.db.get(model,int(target)) if model else None
        domain_audit(self.db,self.actor,action,target,self.before.get((model,target)),snapshot(obj) if obj else {'detail':detail},PropertyService(self.db,self.actor).source,obj=obj)

    def create_order(self,image=''):
        self.require(2)
        fields=DOMAIN_COMMANDS['order.create'][2].split();data={k:v for k,v in self.data.items() if k in fields and v!=''}
        if data.get('house_id'):
            h=self.db.get(House,self.number('house_id'))
            if not h:abort(400,description='房屋不存在')
            if not self.db.scalar(Policy(self.db,self.actor).query(House).where(House.id==h.id)):abort(403)
        else:
            p=Policy(self.db,self.actor)
            cid=self.db.scalar(select(House.community_id).where(House.id.in_(p.own_houses())).limit(1))
            if not cid:abort(403,description='请先核验房屋关系后提交公共区域报修')
            data['community_id']=cid
        result=PropertyService(self.db,self.actor).run('order.create',data)
        order=self.db.get(WorkOrder,result['id']);order.img_url=image
        return order

    def order_action(self,order_no):
        order=self.db.scalar(select(WorkOrder).where(WorkOrder.order_no==order_no).with_for_update())
        if not order:abort(404)
        if not can_access_order(self.actor,order):abort(403)
        action=self.field('action',30,True);command='order.'+action
        if command not in DOMAIN_COMMANDS:abort(403)
        fields=DOMAIN_COMMANDS[command][2].split()
        data={k:v for k,v in self.data.items() if k in fields};data['id']=order.id
        PropertyService(self.db,self.actor).run(command,data)

    def house_action(self):
        self.require(0)
        action=self.field('action',20,True)
        if action=='add':
            h=House(building_name=self.field('building_name',50,True),unit=self.field('unit',20,True),room_no=self.number('room_no'));self.db.add(h);self.db.flush();self.audit('house_add',h.id)
        elif action in {'bind','unbind','delete'}:
            h=self.db.scalar(Policy(self.db,self.actor).query(House).where(House.id==self.number('house_id')).with_for_update())
            if not h:abort(404)
            self.before[(House,h.id)]=snapshot(h)
            self.versioned(h)
            if action=='bind':
                if h.owner_id is not None:abort(409,description='房屋已有业主，请先核验并解除原绑定')
                owner=self.db.get(User,self.number('owner_id'))
                if not owner or owner.role!=2 or not owner.active:abort(400,description='请选择有效业主')
                service=PropertyService(self.db,self.actor)
                person=self.db.scalar(select(Person).where(Person.user_id==owner.id))
                if not person:
                    person=Person(community_id=h.community_id,user_id=owner.id,name=owner.real_name or owner.username,phone=owner.phone,created_by=self.actor.id,updated_by=self.actor.id);self.db.add(person);self.db.flush()
                service.run('relation.bind',{'house_id':h.id,'person_id':person.id,'kind':'owner'})
                h.owner_id=owner.id;notify(self.db,[owner.id],'房屋已绑定：'+h.building_name+' '+h.unit+' '+str(h.room_no));self.audit('house_bind',h.id,f'owner={owner.id}')
            elif action=='unbind':
                if h.owner_id is None:abort(409,description='房屋尚未绑定')
                if self.db.scalar(select(WorkOrder.id).where(WorkOrder.house_id==h.id,WorkOrder.status.in_([0,1,2,3])).with_for_update().limit(1)):abort(409,description='房屋存在未完成工单，暂不能解绑')
                old=h.owner_id
                relations=list(self.db.scalars(select(HousePerson).join(Person,Person.id==HousePerson.person_id).where(HousePerson.house_id==h.id,HousePerson.kind=='owner',Person.user_id==old,effective())))
                for r in relations:PropertyService(self.db,self.actor).run('relation.end',{'id':r.id,'version':r.version,'reason':'管理员在原房屋页面核验解绑'})
                notify(self.db,[old],'房屋绑定已由物业解除，请联系物业核实。');self.audit('house_unbind',h.id,f'previous_owner={old}')
            else:
                if h.owner_id is not None or self.db.scalar(select(WorkOrder.id).where(WorkOrder.house_id==h.id).limit(1)):abort(409,description='已绑定或存在历史工单的房屋不能删除')
                PropertyService(self.db,self.actor).run('property.archive',{'kind':'house','id':h.id,'version':h.version,'reason':'管理员核验归档'})
        else:abort(400,description='无效房屋操作')

    def user_action(self):
        self.require(0)
        action=self.field('action',20,True)
        if action=='create':
            username=self.field('username',50,True)
            if not re.fullmatch(r'[\w.-]{3,50}',username):abort(400,description='用户名格式无效')
            role=self.number('role')
            if role not in {1,2}:abort(400,description='此处仅创建维修者或业主；管理员通过部署命令创建')
            contact_phone=self.field('phone',20)
            if contact_phone:self.phone(contact_phone)
            u=User(username=username,password_hash=generate_password_hash(self.password(self.data.get('password',''))),role=role,real_name=self.field('real_name',50),phone=contact_phone)
            self.db.add(u);self.db.flush();self.audit('user_create',u.id,f'role={role}')
        elif action in {'disable','enable'}:
            u=self.db.scalar(select(User).where(User.id==self.number('user_id')).with_for_update())
            if not u:abort(404)
            self.before[(User,u.id)]=snapshot(u)
            if u.id==self.actor.id:abort(400,description='不能停用自己的管理员账号')
            if action=='disable':
                if u.role==0:
                    admins=list(self.db.scalars(select(User).where(User.role==0,User.active.is_(True)).with_for_update()))
                    if len(admins)<=1:abort(409,description='不能停用最后一个管理员')
                if u.role==1 and self.db.scalar(select(WorkOrder.id).where(WorkOrder.repairer_id==u.id,WorkOrder.status.in_([1,2,3])).with_for_update().limit(1)):abort(409,description='请先处理该维修人员的未完成工单')
                u.active=False
            else:u.active=True;u.failed_logins=0;u.locked_until=None
            u.auth_version+=1;self.audit('user_'+action,u.id)
        else:abort(400,description='无效用户操作')

    def notice_action(self):
        self.require(0)
        action=self.field('action',20,True)
        if action=='add':
            notice=Notice(title=self.field('title',100,True),content=self.field('content',5000,True));self.db.add(notice);self.db.flush();self.audit('notice_add',notice.id)
        elif action in {'edit','delete'}:
            notice=self.db.scalar(Policy(self.db,self.actor).query(Notice).where(Notice.id==self.number('id')).with_for_update())
            if not notice:abort(404)
            self.before[(Notice,notice.id)]=snapshot(notice)
            self.versioned(notice)
            if action=='edit':notice.title=self.field('title',100,True);notice.content=self.field('content',5000,True)
            else:notice.deleted=True
            self.audit('notice_'+action,notice.id)
        else:abort(400,description='无效公告操作')
