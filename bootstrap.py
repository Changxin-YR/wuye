"""Catalog initialization and one-time compatibility backfill; no demo accounts."""
from sqlalchemy import select,event
from models import *
from permissions import ROLES

def seed_catalog(conn):
    if not conn.execute(select(Community.id).where(Community.id==1)).first():
        conn.execute(Community.__table__.insert().values(id=1,name='待完善小区',address='',phone='',created_at=utcnow(),updated_at=utcnow()))
    for code,(name,permissions) in ROLES.items():
        if not conn.execute(select(RbacRole.code).where(RbacRole.code==code)).first():
            conn.execute(RbacRole.__table__.insert().values(code=code,name=name,builtin=True))
            conn.execute(RolePermission.__table__.insert(),[{'role_code':code,'permission':x} for x in sorted(permissions)])

def assign_legacy(conn,uid,role):
    if conn.execute(select(UserRole.user_id).where(UserRole.user_id==uid)).first():return
    code={0:'superadmin',1:'engineer',2:'resident'}[role]
    conn.execute(UserRole.__table__.insert().values(user_id=uid,role_code=code))
    conn.execute(UserScope.__table__.insert().values(user_id=uid,kind={0:'all',1:'assigned',2:'self'}[role],community_id=None if role==0 else 1))

def normalize_house(conn,h):
    cid=h['community_id'] or 1
    bid=h['building_id']
    if not bid:
        bid=conn.execute(select(Building.id).where(Building.community_id==cid,Building.name==h['building_name'])).scalar()
        if not bid:bid=conn.execute(Building.__table__.insert().values(community_id=cid,name=h['building_name'],floors=1,created_at=utcnow(),updated_at=utcnow())).inserted_primary_key[0]
    unid=h['unit_id']
    if not unid:
        unid=conn.execute(select(PropertyUnit.id).where(PropertyUnit.building_id==bid,PropertyUnit.name==h['unit'])).scalar()
        if not unid:unid=conn.execute(PropertyUnit.__table__.insert().values(community_id=cid,building_id=bid,name=h['unit'],created_at=utcnow(),updated_at=utcnow())).inserted_primary_key[0]
    conn.execute(House.__table__.update().where(House.id==h['id']).values(community_id=cid,building_id=bid,unit_id=unid))
    if h['owner_id']:
        pid=conn.execute(select(Person.id).where(Person.user_id==h['owner_id'])).scalar()
        if not pid:
            u=conn.execute(select(User.__table__).where(User.id==h['owner_id'])).mappings().one()
            pid=conn.execute(Person.__table__.insert().values(community_id=cid,user_id=u['id'],name=u['real_name'] or u['username'],phone=u['phone'] or '',created_at=utcnow(),updated_at=utcnow())).inserted_primary_key[0]
        key=f"{h['id']}:{pid}:owner"
        if not conn.execute(select(HousePerson.id).where(HousePerson.active_key==key)).first():
            conn.execute(HousePerson.__table__.insert().values(community_id=cid,building_id=bid,house_id=h['id'],person_id=pid,kind='owner',active_key=key,start_at=utcnow(),status='active',created_at=utcnow(),updated_at=utcnow()))

@event.listens_for(User,'after_insert')
def user_created(mapper,conn,target):assign_legacy(conn,target.id,target.role)
@event.listens_for(House,'after_insert')
def house_created(mapper,conn,target):
    if not target.building_id or not target.unit_id or target.owner_id:
        normalize_house(conn,{c.name:getattr(target,c.name) for c in House.__table__.columns})
