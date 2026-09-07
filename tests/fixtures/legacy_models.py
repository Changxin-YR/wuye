from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "sys_user"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    real_name = Column(String(50), nullable=False, default="")
    phone = Column(String(20), nullable=False, default="")
    role = Column(Integer, nullable=False, default=2)
    avatar = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class House(Base):
    __tablename__ = "house"
    __table_args__ = (UniqueConstraint("building_name", "unit", "room_no", name="uk_house_room"),)
    id = Column(Integer, primary_key=True)
    building_name = Column(String(50), nullable=False)
    unit = Column(String(20), nullable=False)
    room_no = Column(Integer, nullable=False)
    owner_id = Column(Integer, ForeignKey("sys_user.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class WorkOrder(Base):
    __tablename__ = "work_order"
    id = Column(Integer, primary_key=True)
    order_no = Column(String(64), unique=True, nullable=False)
    owner_id = Column(Integer, ForeignKey("sys_user.id"), nullable=False)
    repairer_id = Column(Integer, ForeignKey("sys_user.id"))
    house_id = Column(Integer, ForeignKey("house.id"))
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False, default="")
    img_url = Column(String(500), nullable=False, default="")
    type = Column(String(30), nullable=False, default="其他")
    status = Column(Integer, nullable=False, default=0)
    accept_time = Column(DateTime)
    finish_time = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Notice(Base):
    __tablename__ = "notice"
    id = Column(Integer, primary_key=True)
    title = Column(String(100), nullable=False)
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Evaluation(Base):
    __tablename__ = "order_evaluate"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("work_order.id", ondelete="CASCADE"), unique=True, nullable=False)
    score = Column(Integer, nullable=False)
    comment = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime, default=utcnow, nullable=False)


class OrderLog(Base):
    __tablename__ = "order_log"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("work_order.id", ondelete="CASCADE"), nullable=False)
    operator_id = Column(Integer, ForeignKey("sys_user.id", ondelete="SET NULL"))
    before_status = Column(Integer)
    after_status = Column(Integer)
    remark = Column(String(255), nullable=False, default="")
    operate_time = Column(DateTime, default=utcnow, nullable=False)
