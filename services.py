from sqlalchemy import select
from models import Notification, OrderLog, User, WorkOrder, utcnow

STATUS_TEXT={0:'待派单',1:'已派单',2:'维修中',3:'待验收',4:'已关闭',5:'已取消'}
ROLE_TEXT={0:'管理员',1:'维修者',2:'业主'}
ORDER_TYPES={'水电故障','公共设施','家政','其他'}
ALLOWED_TRANSITIONS={0:{1,5},1:{2,5},2:{3},3:{2,4},4:set(),5:set()}

class InvalidTransition(ValueError):pass

def scope_orders(query,user):
    from permissions import policy_for
    p=policy_for(user)
    return query.where(p.condition(WorkOrder)) if p else query.where(False)

def can_access_order(user,order):
    from permissions import policy_for
    p=policy_for(user)
    return bool(p and p.db.scalar(p.query(WorkOrder).where(WorkOrder.id==order.id)))

def log_order(db,order,operator_id,remark,before=None):
    db.add(OrderLog(order_id=order.id,operator_id=operator_id,before_status=order.status if before is None else before,after_status=order.status,remark=remark))

def notify(db,user_ids,content,order_id=None):
    for uid in set(x for x in user_ids if x is not None):
        db.add(Notification(user_id=uid,order_id=order_id,content=content[:255]))

def notify_admins(db,content,order_id=None):
    notify(db,list(db.scalars(select(User.id).where(User.role==0,User.active.is_(True)))),content,order_id)

def transition_status(db,order,target,operator_id,remark=''):
    before=order.status
    if target not in ALLOWED_TRANSITIONS.get(before,set()):
        raise InvalidTransition(f'{STATUS_TEXT.get(before,"未知")}不能变更为{STATUS_TEXT.get(target,"未知")}')
    order.status=target;order.updated_at=utcnow()
    if target==2:
        if before==3:order.finish_time=None;order.resolution=''
        else:order.accept_time=utcnow()
    if target==3:order.finish_time=utcnow()
    log_order(db,order,operator_id,remark,before)
    return order
