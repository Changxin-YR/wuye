from datetime import datetime, timezone
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Numeric, Date
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime

# Preserve microseconds on MySQL: second rounding can put new relations in the future.
DateTime = DateTime().with_variant(MySQLDateTime(fsp=6), "mysql")

Base = declarative_base()

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class User(Base):
    __tablename__ = 'sys_user'
    __table_args__ = (CheckConstraint('role IN (0,1,2)', name='ck_user_role'),)
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    real_name = Column(String(50), nullable=False, default='')
    phone = Column(String(20), nullable=False, default='')
    role = Column(Integer, nullable=False, default=2)
    avatar = Column(String(255), nullable=False, default='')
    active = Column(Boolean, nullable=False, default=True, server_default='1')
    auth_version = Column(Integer, nullable=False, default=1, server_default='1')
    failed_logins = Column(Integer, nullable=False, default=0, server_default='0')
    locked_until = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

class House(Base):
    __tablename__ = 'house'
    __table_args__ = (UniqueConstraint('unit_id','room_no',name='uk_house_unit_room'),)
    id = Column(Integer, primary_key=True)
    community_id = Column(Integer, ForeignKey('community.id'), nullable=False, default=1, server_default='1')
    building_id = Column(Integer, ForeignKey('building.id'))
    unit_id = Column(Integer, ForeignKey('property_unit.id'))
    area = Column(Numeric(10,2), nullable=False, default=0, server_default='0')
    usage = Column(String(20), nullable=False, default='residential', server_default='residential')
    occupancy = Column(String(20), nullable=False, default='vacant', server_default='vacant')
    ownership = Column(String(20), nullable=False, default='private', server_default='private')
    deleted = Column(Boolean, nullable=False, default=False, server_default='0')
    building_name = Column(String(50), nullable=False)
    unit = Column(String(20), nullable=False)
    room_no = Column(Integer, nullable=False)
    owner_id = Column(Integer, ForeignKey('sys_user.id',ondelete='SET NULL'))
    version = Column(Integer, nullable=False, default=1, server_default='1')
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    __mapper_args__ = {'version_id_col': version}

class WorkOrder(Base):
    __tablename__ = 'work_order'
    __table_args__ = (Index('idx_order_owner_status','owner_id','status'),Index('idx_order_repairer_status','repairer_id','status'),CheckConstraint('status IN (0,1,2,3,4,5)',name='ck_order_status'))
    id = Column(Integer, primary_key=True)
    community_id = Column(Integer, ForeignKey('community.id'), nullable=False, default=1, server_default='1')
    building_id = Column(Integer, ForeignKey('building.id'))
    requester_person_id = Column(Integer, ForeignKey('person.id'))
    order_no = Column(String(64), unique=True, nullable=False)
    owner_id = Column(Integer, ForeignKey('sys_user.id'), nullable=False)
    repairer_id = Column(Integer, ForeignKey('sys_user.id'))
    house_id = Column(Integer, ForeignKey('house.id',ondelete='SET NULL'))
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False, default='')
    img_url = Column(String(500), nullable=False, default='')
    type = Column(String(30), nullable=False, default='其他')
    status = Column(Integer, nullable=False, default=0)
    location = Column(String(200), nullable=False, default='', server_default='')
    contact_name = Column(String(50), nullable=False, default='', server_default='')
    contact_phone = Column(String(20), nullable=False, default='', server_default='')
    resolution = Column(String(1000), nullable=False, default='', server_default='')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    accept_time = Column(DateTime)
    finish_time = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    __mapper_args__ = {'version_id_col': version}

class Notice(Base):
    __tablename__ = 'notice'
    community_id = Column(Integer, ForeignKey('community.id'), nullable=False, default=1, server_default='1')
    building_id = Column(Integer, ForeignKey('building.id'))
    deleted = Column(Boolean, nullable=False, default=False, server_default='0')
    id = Column(Integer, primary_key=True)
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False, default='')
    version = Column(Integer, nullable=False, default=1, server_default='1')
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    __mapper_args__ = {'version_id_col': version}

class Evaluation(Base):
    __tablename__ = 'order_evaluate'
    __table_args__ = (CheckConstraint('score BETWEEN 1 AND 5',name='ck_evaluate_score'),)
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('work_order.id',ondelete='CASCADE'), unique=True, nullable=False)
    score = Column(Integer, nullable=False)
    comment = Column(String(200), nullable=False, default='')
    created_at = Column(DateTime, default=utcnow, nullable=False)

class OrderLog(Base):
    __tablename__ = 'order_log'
    __table_args__ = (Index('idx_log_order_time','order_id','operate_time'),)
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('work_order.id',ondelete='CASCADE'), nullable=False)
    operator_id = Column(Integer, ForeignKey('sys_user.id',ondelete='SET NULL'))
    before_status = Column(Integer)
    after_status = Column(Integer)
    remark = Column(String(255), nullable=False, default='')
    operate_time = Column(DateTime, default=utcnow, nullable=False)

class Notification(Base):
    __tablename__ = 'notification'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('sys_user.id'),nullable=False,index=True)
    order_id = Column(Integer, ForeignKey('work_order.id'))
    content = Column(String(255),nullable=False)
    is_read = Column(Boolean,nullable=False,default=False)
    created_at = Column(DateTime,default=utcnow,nullable=False)

class AuditLog(Base):
    __tablename__ = 'audit_log'
    id = Column(Integer,primary_key=True)
    operator_id = Column(Integer,ForeignKey('sys_user.id'),nullable=False)
    actor_name = Column(String(50), nullable=False, default='', server_default='')
    roles = Column(String(500), nullable=False, default='', server_default='')
    source = Column(String(20), nullable=False, default='manual', server_default='manual')
    resource = Column(String(50), nullable=False, default='', server_default='')
    before_data = Column(Text)
    after_data = Column(Text)
    status = Column(String(20), nullable=False, default='success', server_default='success')
    error = Column(String(500), nullable=False, default='', server_default='')
    trace_id = Column(String(64), index=True)
    community_id = Column(Integer, ForeignKey('community.id'))
    building_id = Column(Integer, ForeignKey('building.id'))
    action = Column(String(50),nullable=False)
    target = Column(String(100),nullable=False)
    detail = Column(String(500),nullable=False,default='')
    created_at = Column(DateTime,default=utcnow,nullable=False)

class AiConversation(Base):
    __tablename__ = 'ai_conversation'
    id = Column(String(36),primary_key=True)
    user_id = Column(Integer,ForeignKey('sys_user.id'),nullable=False,index=True)
    auth_version = Column(Integer,nullable=False,default=1,server_default='1')
    upstream_id = Column(String(128),nullable=False,default='')
    scope_hash = Column(String(64),nullable=False,default='')
    state_json = Column(Text,nullable=True,default='{}')
    messages_json = Column(Text,nullable=True,default='[]')
    created_at = Column(DateTime,default=utcnow,nullable=False)
    updated_at = Column(DateTime,default=utcnow,onupdate=utcnow,nullable=False)

class ConversationState(Base):
    __tablename__ = 'conversation_state'
    id = Column(Integer,primary_key=True)
    conversation_id = Column(String(36),ForeignKey('ai_conversation.id',ondelete='CASCADE'),nullable=False,unique=True)
    user_id = Column(Integer,ForeignKey('sys_user.id'),nullable=False,index=True)
    auth_version = Column(Integer,nullable=False,default=1,server_default='1')
    state_json = Column(Text,nullable=True,default='{}')
    updated_at = Column(DateTime,default=utcnow,onupdate=utcnow,nullable=False)

class ConversationMessage(Base):
    __tablename__ = 'conversation_message'
    id = Column(Integer,primary_key=True)
    conversation_id = Column(String(36),ForeignKey('ai_conversation.id',ondelete='CASCADE'),nullable=False,index=True)
    user_id = Column(Integer,ForeignKey('sys_user.id'),nullable=False,index=True)
    sequence = Column(Integer,nullable=False)
    role = Column(String(20),nullable=False)
    content = Column(Text,nullable=False,default='')
    created_at = Column(DateTime,default=utcnow,nullable=False)
    __table_args__ = (UniqueConstraint('conversation_id','sequence',name='uk_conversation_message_sequence'),)

class SchemaMigration(Base):
    __tablename__ = 'schema_migration'
    revision = Column(String(30), primary_key=True)
    applied_at = Column(DateTime, default=utcnow, nullable=False)

class AiGrant(Base):
    __tablename__ = 'ai_grant'
    id = Column(String(36),primary_key=True)
    token_hash = Column(String(64),unique=True,nullable=False)
    user_id = Column(Integer,ForeignKey('sys_user.id'),nullable=False,index=True)
    auth_version = Column(Integer,nullable=False)
    expires_at = Column(DateTime,nullable=False)
    created_at = Column(DateTime,default=utcnow,nullable=False)

class AiAction(Base):
    __tablename__ = 'ai_action'
    __table_args__ = (UniqueConstraint('grant_id','payload_hash',name='uk_ai_proposal'),)
    id = Column(String(36),primary_key=True)
    grant_id = Column(String(36),ForeignKey('ai_grant.id'),nullable=False)
    user_id = Column(Integer,ForeignKey('sys_user.id'),nullable=False,index=True)
    auth_version = Column(Integer,nullable=False)
    command = Column(String(40),nullable=False)
    payload = Column(Text,nullable=False)
    payload_hash = Column(String(64),nullable=False)
    request_key = Column(String(100),nullable=False,default='',server_default='')
    preview = Column(Text,nullable=False)
    status = Column(String(20),nullable=False,default='pending')
    result = Column(Text,nullable=False,default='')
    expires_at = Column(DateTime,nullable=False)
    created_at = Column(DateTime,default=utcnow,nullable=False)
    version = Column(Integer,nullable=False,default=1)
    __mapper_args__ = {'version_id_col':version}

# Normalized property domain. Legacy account/owner columns are compatibility projections.
class Record:
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey('sys_user.id'))
    updated_by = Column(Integer, ForeignKey('sys_user.id'))
    deleted = Column(Boolean, default=False, nullable=False, server_default='0')
    version = Column(Integer, default=1, nullable=False, server_default='1')
    @classmethod
    def __declare_last__(cls):
        cls.__mapper__.version_id_col=cls.__table__.c.version

class Scoped:
    community_id = Column(Integer, ForeignKey('community.id'), nullable=False, index=True)
    building_id = Column(Integer, ForeignKey('building.id'), index=True)

class Community(Record, Base):
    __tablename__='community'
    name=Column(String(80),unique=True,nullable=False)
    address=Column(String(200),nullable=False,default='')
    phone=Column(String(20),nullable=False,default='')

class Building(Record, Base):
    __tablename__='building'
    __table_args__=(UniqueConstraint('community_id','name',name='uk_building_name'),)
    community_id=Column(Integer,ForeignKey('community.id'),nullable=False,index=True)
    name=Column(String(50),nullable=False)
    floors=Column(Integer,nullable=False,default=1)

class PropertyUnit(Record, Scoped, Base):
    __tablename__='property_unit'
    __table_args__=(UniqueConstraint('building_id','name',name='uk_unit_name'),)
    name=Column(String(20),nullable=False)

class Person(Record, Base):
    __tablename__='person'
    community_id=Column(Integer,ForeignKey('community.id'),nullable=False,index=True)
    user_id=Column(Integer,ForeignKey('sys_user.id'),unique=True)
    name=Column(String(50),nullable=False,index=True)
    phone=Column(String(20),nullable=False,index=True)
    emergency_contact=Column(String(100),nullable=False,default='')
    note=Column(String(500),nullable=False,default='')

class Lease(Record, Scoped, Base):
    __tablename__='lease'
    __table_args__=(CheckConstraint('end_date >= start_date',name='ck_lease_dates'),)
    house_id=Column(Integer,ForeignKey('house.id'),nullable=False,index=True)
    start_date=Column(Date,nullable=False)
    end_date=Column(Date,nullable=False)
    move_in=Column(DateTime,nullable=False)
    move_out=Column(DateTime)
    status=Column(String(20),nullable=False,default='active')
    active_key=Column(Integer,unique=True)
    note=Column(String(500),nullable=False,default='')

class HousePerson(Record, Scoped, Base):
    __tablename__='house_person'
    __table_args__=(CheckConstraint("kind IN ('owner','family','tenant','contact')",name='ck_relation_kind'),CheckConstraint('end_at IS NULL OR end_at >= start_at',name='ck_relation_dates'),Index('idx_relation_house_person','house_id','person_id','status'))
    house_id=Column(Integer,ForeignKey('house.id'),nullable=False)
    person_id=Column(Integer,ForeignKey('person.id'),nullable=False)
    lease_id=Column(Integer,ForeignKey('lease.id'))
    kind=Column(String(20),nullable=False)
    is_resident=Column(Boolean,nullable=False,default=True)
    start_at=Column(DateTime,nullable=False,default=utcnow)
    end_at=Column(DateTime)
    status=Column(String(20),nullable=False,default='active')
    active_key=Column(String(100),unique=True)
    reason=Column(String(300),nullable=False,default='')

class RbacRole(Base):
    __tablename__='rbac_role'
    code=Column(String(40),primary_key=True)
    name=Column(String(50),nullable=False)
    builtin=Column(Boolean,nullable=False,default=False)
class RolePermission(Base):
    __tablename__='role_permission'
    role_code=Column(String(40),ForeignKey('rbac_role.code'),primary_key=True)
    permission=Column(String(60),primary_key=True)
class UserRole(Base):
    __tablename__='user_role'
    user_id=Column(Integer,ForeignKey('sys_user.id'),primary_key=True)
    role_code=Column(String(40),ForeignKey('rbac_role.code'),primary_key=True)
class UserScope(Base):
    __tablename__='user_scope'
    id=Column(Integer,primary_key=True)
    user_id=Column(Integer,ForeignKey('sys_user.id'),nullable=False,index=True)
    kind=Column(String(20),nullable=False)
    community_id=Column(Integer,ForeignKey('community.id'))
    building_id=Column(Integer,ForeignKey('building.id'))
class BusinessRequest(Base):
    __tablename__='business_request'
    __table_args__=(UniqueConstraint('user_id','request_key',name='uk_business_request'),)
    id=Column(Integer,primary_key=True)
    user_id=Column(Integer,ForeignKey('sys_user.id'),nullable=False)
    request_key=Column(String(100),nullable=False)
    digest=Column(String(64),nullable=False)
    response=Column(Text,nullable=False)
    created_at=Column(DateTime,nullable=False,default=utcnow)
class SystemSetting(Base):
    __tablename__='system_setting'
    key=Column(String(50),primary_key=True)
    value=Column(String(200),nullable=False)

class Complaint(Record, Scoped, Base):
    __tablename__='complaint'
    house_id=Column(Integer,ForeignKey('house.id'),nullable=False)
    reporter_id=Column(Integer,ForeignKey('sys_user.id'),nullable=False)
    assignee_id=Column(Integer,ForeignKey('sys_user.id'))
    title=Column(String(100),nullable=False)
    content=Column(Text,nullable=False)
    category=Column(String(30),nullable=False,default='服务投诉')
    status=Column(String(20),nullable=False,default='open')
    resolution=Column(String(1000),nullable=False,default='')
class Visitor(Record, Scoped, Base):
    __tablename__='visitor'
    house_id=Column(Integer,ForeignKey('house.id'),nullable=False)
    host_person_id=Column(Integer,ForeignKey('person.id'),nullable=False)
    name=Column(String(50),nullable=False)
    phone=Column(String(20),nullable=False)
    purpose=Column(String(200),nullable=False)
    expected_at=Column(DateTime,nullable=False)
    check_in=Column(DateTime)
    check_out=Column(DateTime)
    status=Column(String(20),nullable=False,default='registered')
class Vehicle(Record, Scoped, Base):
    __tablename__='vehicle'
    __table_args__=(UniqueConstraint('community_id','plate',name='uk_vehicle_plate'),)
    house_id=Column(Integer,ForeignKey('house.id'),nullable=False)
    person_id=Column(Integer,ForeignKey('person.id'),nullable=False)
    plate=Column(String(20),nullable=False)
    model=Column(String(50),nullable=False,default='')
    status=Column(String(20),nullable=False,default='active')
class ParkingSpace(Record, Scoped, Base):
    __tablename__='parking_space'
    __table_args__=(UniqueConstraint('community_id','code',name='uk_parking_code'),)
    code=Column(String(50),nullable=False)
    location=Column(String(200),nullable=False)
    status=Column(String(20),nullable=False,default='available')
class ParkingUse(Record, Scoped, Base):
    __tablename__='parking_use'
    space_id=Column(Integer,ForeignKey('parking_space.id'),nullable=False)
    vehicle_id=Column(Integer,ForeignKey('vehicle.id'),nullable=False)
    start_at=Column(DateTime,nullable=False,default=utcnow)
    end_at=Column(DateTime)
    status=Column(String(20),nullable=False,default='active')
    active_space=Column(Integer,unique=True)
    active_vehicle=Column(Integer,unique=True)
class Device(Record, Scoped, Base):
    __tablename__='device'
    __table_args__=(UniqueConstraint('community_id','code',name='uk_device_code'),)
    code=Column(String(50),nullable=False)
    name=Column(String(100),nullable=False)
    category=Column(String(30),nullable=False)
    location=Column(String(200),nullable=False)
    status=Column(String(20),nullable=False,default='normal')
class Inspection(Record, Scoped, Base):
    __tablename__='inspection'
    device_id=Column(Integer,ForeignKey('device.id'),nullable=False)
    assignee_id=Column(Integer,ForeignKey('sys_user.id'),nullable=False)
    due_at=Column(DateTime,nullable=False)
    completed_at=Column(DateTime)
    checklist=Column(String(1000),nullable=False)
    findings=Column(String(1000),nullable=False,default='')
    status=Column(String(20),nullable=False,default='pending')
    work_order_id=Column(Integer,ForeignKey('work_order.id'))
class FeeItem(Record, Base):
    __tablename__='fee_item'
    __table_args__=(UniqueConstraint('community_id','name',name='uk_fee_item'),CheckConstraint('rate > 0',name='ck_fee_rate'))
    community_id=Column(Integer,ForeignKey('community.id'),nullable=False)
    name=Column(String(80),nullable=False)
    basis=Column(String(20),nullable=False)
    rate=Column(Numeric(12,4),nullable=False)
class Bill(Record, Scoped, Base):
    __tablename__='bill'
    __table_args__=(UniqueConstraint('house_id','fee_item_id','period',name='uk_monthly_bill'),CheckConstraint('amount_cents > 0 AND paid_cents >= 0 AND paid_cents <= amount_cents',name='ck_bill_amounts'))
    house_id=Column(Integer,ForeignKey('house.id'),nullable=False)
    fee_item_id=Column(Integer,ForeignKey('fee_item.id'),nullable=False)
    title=Column(String(100),nullable=False)
    period=Column(String(7),nullable=False)
    due_date=Column(Date,nullable=False)
    amount_cents=Column(Integer,nullable=False)
    paid_cents=Column(Integer,nullable=False,default=0)
    status=Column(String(20),nullable=False,default='unpaid')
    calculation=Column(String(500),nullable=False)
    void_reason=Column(String(300),nullable=False,default='')
class Payment(Record, Scoped, Base):
    __tablename__='payment'
    __table_args__=(UniqueConstraint('community_id','channel','reference',name='uk_payment_receipt'),CheckConstraint('amount_cents > 0',name='ck_payment_amount'))
    house_id=Column(Integer,ForeignKey('house.id'),nullable=False)
    bill_id=Column(Integer,ForeignKey('bill.id'),nullable=False,index=True)
    amount_cents=Column(Integer,nullable=False)
    channel=Column(String(20),nullable=False)
    reference=Column(String(100),nullable=False)
    environment=Column(String(20),nullable=False)
    status=Column(String(20),nullable=False,default='posted')
    reversed_at=Column(DateTime)
    reason=Column(String(300),nullable=False,default='')

# Explicit database constraints also protect imports and administrative maintenance.
Base.metadata.tables['lease'].append_constraint(CheckConstraint("status IN ('active','ended')",name='ck_lease_state'))
Base.metadata.tables['house_person'].append_constraint(CheckConstraint("status IN ('active','ended')",name='ck_house_person_state'))
Base.metadata.tables['complaint'].append_constraint(CheckConstraint("status IN ('open','assigned','resolved','closed')",name='ck_complaint_state'))
Base.metadata.tables['visitor'].append_constraint(CheckConstraint("status IN ('registered','inside','left','cancelled')",name='ck_visitor_state'))
Base.metadata.tables['vehicle'].append_constraint(CheckConstraint("status IN ('active','archived')",name='ck_vehicle_state'))
Base.metadata.tables['parking_space'].append_constraint(CheckConstraint("status IN ('available','occupied')",name='ck_parking_space_state'))
Base.metadata.tables['parking_use'].append_constraint(CheckConstraint("status IN ('active','ended')",name='ck_parking_use_state'))
Base.metadata.tables['device'].append_constraint(CheckConstraint("status IN ('normal','fault','maintenance','retired')",name='ck_device_state'))
Base.metadata.tables['inspection'].append_constraint(CheckConstraint("status IN ('pending','completed')",name='ck_inspection_state'))
Base.metadata.tables['bill'].append_constraint(CheckConstraint("status IN ('unpaid','partial','paid','void')",name='ck_bill_state'))
Base.metadata.tables['payment'].append_constraint(CheckConstraint("status IN ('posted','reversed')",name='ck_payment_state'))
Base.metadata.tables['user_scope'].append_constraint(CheckConstraint("kind IN ('all','community','building','assigned','self')",name='ck_scope_kind'))
