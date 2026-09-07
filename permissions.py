"""Persisted RBAC and row-level data scope. No authority comes from prompts."""
from flask import abort
from sqlalchemy import select, or_, and_, false
from sqlalchemy.orm import object_session
from models import *

ALL_PERMISSIONS=set('community.manage property.read property.write person.read person.write relation.write relation.end lease.write staff.manage rbac.manage rbac.define order.read order.create order.dispatch order.work order.verify order.cancel complaint.read complaint.create complaint.handle notice.read notice.write visitor.read visitor.write vehicle.read vehicle.write parking.read parking.write device.read device.write inspection.read inspection.write inspection.assign billing.read billing.manage billing.collect billing.reverse audit.read resident.self'.split())
COMMON=set('notice.read order.read order.create complaint.read complaint.create'.split())
ROLES={
 'superadmin':('超级管理员',ALL_PERMISSIONS-{'resident.self'}),
 'manager':('物业经理',ALL_PERMISSIONS-{'rbac.define','resident.self'}),
 'building_manager':('楼栋管理员',ALL_PERMISSIONS-{'rbac.define','rbac.manage','staff.manage','community.manage','billing.collect','billing.reverse','resident.self'}),
 'customer_service':('客服',COMMON|set('property.read person.read person.write relation.write relation.end lease.write order.dispatch order.verify order.cancel complaint.handle visitor.read visitor.write'.split())),
 'frontdesk':('前台',COMMON|set('property.read person.read person.write relation.write lease.write visitor.read visitor.write vehicle.read'.split())),
 'finance':('财务',COMMON|set('property.read person.read billing.read billing.manage billing.collect billing.reverse'.split())),
 'engineer':('工程维修',COMMON|set('order.work device.read inspection.read inspection.write'.split())),
 'security':('保安',COMMON|set('property.read person.read visitor.read visitor.write vehicle.read vehicle.write parking.read parking.write device.read inspection.read inspection.write'.split())),
 'cleaner':('保洁',COMMON|set('inspection.read inspection.write device.read'.split())),
 'staff':('普通工作人员',COMMON),
 'resident':('住户',COMMON|set('resident.self property.read person.read billing.read order.verify order.cancel vehicle.read'.split())),
}

def effective():
    now=utcnow()
    return and_(HousePerson.deleted.is_(False),HousePerson.status=='active',HousePerson.start_at<=now,or_(HousePerson.end_at.is_(None),HousePerson.end_at>now))

class Policy:
    def __init__(self,db,user):
        self.db=db;self.user=user
        self.roles=set(db.scalars(select(UserRole.role_code).where(UserRole.user_id==user.id))) if user and user.active else set()
        self.permissions=set(db.scalars(select(RolePermission.permission).where(RolePermission.role_code.in_(self.roles)))) if self.roles else set()
        self.scopes=list(db.scalars(select(UserScope).where(UserScope.user_id==user.id))) if self.roles else []
        self.super='superadmin' in self.roles
        self.resident='resident.self' in self.permissions
    def has(self,permission):return permission in self.permissions
    def require(self,permission):
        if not self.has(permission):abort(403,description='当前账号没有执行该操作的权限')
    def own_houses(self):
        return select(HousePerson.house_id).join(Person,Person.id==HousePerson.person_id).where(Person.user_id==self.user.id,Person.deleted.is_(False),effective())
    def within(self,cid,bid=None,write=False):
        if self.super:return True
        for s in self.scopes:
            if s.kind=='community' and s.community_id==cid:return True
            if s.kind=='building' and s.community_id==cid and bid==s.building_id:return True
            if not write and s.kind=='assigned' and s.community_id==cid:return True
        return False
    def require_scope(self,cid,bid=None):
        if not self.within(cid,bid,True):abort(403,description='该记录不在当前账号负责的数据范围内')
    def condition(self,model):
        if self.super:return True
        if model is User:
            permitted_ids=select(UserScope.user_id).where(or_(*[and_(UserScope.community_id==s.community_id,True if s.kind=='community' else or_(UserScope.building_id==s.building_id,UserScope.kind.in_(['community','assigned']))) for s in self.scopes if s.kind in {'community','building'}],false()))
            return or_(User.id==self.user.id,User.id.in_(permitted_ids)) if self.has('staff.manage') or self.has('order.dispatch') or self.has('inspection.assign') else User.id==self.user.id
        if model is RbacRole:return True if self.has('rbac.manage') else false()
        if model is HousePerson and self.resident:
            return HousePerson.person_id.in_(select(Person.id).where(Person.user_id==self.user.id))
        if model is Person and self.resident:return Person.user_id==self.user.id
        if self.resident:
            if model is House:return House.id.in_(self.own_houses())
            if model is WorkOrder:return or_(WorkOrder.owner_id==self.user.id,WorkOrder.requester_person_id.in_(select(Person.id).where(Person.user_id==self.user.id)))
            if model is Complaint:return Complaint.reporter_id==self.user.id
            if model is Notice:
                return and_(Notice.community_id.in_(select(House.community_id).where(House.id.in_(self.own_houses()))),or_(Notice.building_id.is_(None),Notice.building_id.in_(select(House.building_id).where(House.id.in_(self.own_houses())))))
            if model in {Bill,Payment,Vehicle,Lease}:return model.house_id.in_(self.own_houses())
            if model is Community:return Community.id.in_(select(House.community_id).where(House.id.in_(self.own_houses())))
            if model is Building:return Building.id.in_(select(House.building_id).where(House.id.in_(self.own_houses())))
            if model is PropertyUnit:return PropertyUnit.id.in_(select(House.unit_id).where(House.id.in_(self.own_houses())))
            return false()
        if model is WorkOrder and not(self.has('order.dispatch') or self.has('order.verify')):
            return or_(WorkOrder.owner_id==self.user.id,WorkOrder.repairer_id==self.user.id if self.has('order.work') else false())
        if model is Complaint and not self.has('complaint.handle'):return Complaint.reporter_id==self.user.id
        if model is Inspection and not self.has('inspection.assign'):return Inspection.assignee_id==self.user.id
        cond=[]
        for s in self.scopes:
            if s.kind not in {'community','building','assigned'}:continue
            if model is Community:cond.append(Community.id==s.community_id);continue
            cid=getattr(model,'community_id',None)
            if cid is None:continue
            if s.kind=='community':cond.append(cid==s.community_id)
            elif s.kind=='assigned':
                if model in {Device,Notice,Building,PropertyUnit}:cond.append(cid==s.community_id)
            elif model is Building:cond.append(and_(cid==s.community_id,Building.id==s.building_id))
            elif model is Person:
                linked=select(HousePerson.person_id).where(HousePerson.building_id==s.building_id,effective())
                cond.append(and_(cid==s.community_id,or_(Person.id.in_(linked),Person.created_by==self.user.id)))
            elif model is FeeItem:cond.append(cid==s.community_id)
            elif hasattr(model,'building_id'):
                b=model.building_id==s.building_id
                if model is Notice:b=or_(b,model.building_id.is_(None))
                cond.append(and_(cid==s.community_id,b))
        return or_(*cond,false())
    def query(self,model):
        q=select(model).where(self.condition(model))
        if hasattr(model,'deleted'):q=q.where(model.deleted.is_(False))
        return q
    def get(self,model,id,lock=False):
        q=self.query(model).where(model.id==id)
        obj=self.db.scalar(q.with_for_update() if lock else q)
        if not obj:abort(404,description='记录不存在或不在您的可访问范围内')
        return obj
    def identity(self):
        scopes=[{'kind':s.kind,'community_id':s.community_id,'building_id':s.building_id} for s in self.scopes]
        scopes.sort(key=lambda x:(x['kind'] or '',x['community_id'] or 0,x['building_id'] or 0))
        return {'userId':self.user.id,'username':self.user.username,'roles':sorted(self.roles),'permissions':sorted(self.permissions),'data_scope':scopes}

def policy_for(user):
    db=object_session(user)
    return Policy(db,user) if db else None
