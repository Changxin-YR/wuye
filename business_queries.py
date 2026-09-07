"""Read-only scoped business queries exposed to Agent; never accept SQL."""
from datetime import datetime,timedelta
from flask import abort
from sqlalchemy import select,func
from models import *
from permissions import Policy
from property_service import snapshot
from agent_security import safe_record
QUERIES={'house.search':'property.read','person.search':'person.read','person.properties':'person.read','order.search':'order.read','order.pending':'order.read','billing.unpaid':'billing.read','complaint.stats':'complaint.handle','notice.read':'notice.read','whoami':'notice.read'}
def query(db,actor,command,args):
    if command not in QUERIES or not isinstance(args,dict):abort(400,description='无效查询')
    args=dict(args)
    # Models commonly use these read-only aliases; normalize them before the
    # strict whitelist so scope checks still run on the canonical fields.
    if command in {'person.search','person.properties'}:
        if 'person_name' not in args:
            for alias in ('name','q','keywords'):
                if alias in args:
                    args['person_name']=args.pop(alias);break
        if command=='person.properties' and 'person_id' not in args and 'id' in args:
            args['person_id']=args.pop('id')
    if command.startswith('order.') and 'order_no' not in args and 'q' in args:
        args['order_no']=args.pop('q')
    allowed={'community_id','building_id','building_name','unit','room_no','id','person_id','person_name','phone','month','status','q','order_no'}
    if set(args)-allowed:abort(400,description='查询包含未知参数')
    if any(not isinstance(v,(str,int)) or isinstance(v,bool) for v in args.values()):abort(400)
    p=Policy(db,actor);p.require(QUERIES[command])
    if command=='whoami':return p.identity()
    if command=='notice.read':
        q=p.query(Notice).order_by(Notice.id.desc())
        for key in ('community_id','building_id'):
            if args.get(key):q=q.where(getattr(Notice,key)==args[key])
        rows=list(db.scalars(q.limit(101)))
        return {'items':[safe_record(snapshot(x)) for x in rows[:100]],'truncated':len(rows)>100,'message':'仅返回当前账号授权范围内的最小必要数据'}
    if command.startswith('house.') or command in {'person.properties','billing.unpaid'}:
        hq=p.query(House)
        if args.get('id'):hq=hq.where(House.id==args['id'])
        if args.get('building_id'):hq=hq.where(House.building_id==args['building_id'])
        for key in ['community_id','building_name','unit','room_no']:
            if args.get(key):hq=hq.where(getattr(House,key)==args[key])
        if command=='person.properties':
            person_id=args.get('person_id')
            name=str(args.get('person_name','')).strip()
            if person_id:
                pq=p.query(Person).where(Person.id==person_id)
            else:
                if not name:abort(400,description='请提供人员姓名')
                pq=p.query(Person).where(Person.name==name)
            if args.get('phone'):pq=pq.where(Person.phone==args['phone'])
            people=list(db.scalars(pq.limit(2)))
            if not people:abort(404,description='未找到该人员')
            if len(people)>1:abort(409,description='存在同名人员，请提供联系电话')
            from permissions import effective
            hq=hq.where(House.id.in_(select(HousePerson.house_id).where(HousePerson.person_id==people[0].id,HousePerson.kind=='owner',effective())))
        if command=='billing.unpaid':
            q=p.query(Bill).where(Bill.house_id.in_(hq.with_only_columns(House.id)),Bill.status.in_(['unpaid','partial']))
        else:q=hq
    elif command=='person.search':
        q=p.query(Person)
        if args.get('id'):q=q.where(Person.id==args['id'])
        if args.get('person_name'):q=q.where(Person.name==args['person_name'])
        if args.get('phone'):q=q.where(Person.phone==args['phone'])
    elif command.startswith('order.'):
        q=p.query(WorkOrder)
        if args.get('id'):q=q.where(WorkOrder.id==args['id'])
        if args.get('order_no'):q=q.where(WorkOrder.order_no==args['order_no'])
        if command=='order.pending':q=q.where(WorkOrder.status.in_([0,1,2,3]))
        if args.get('q'):q=q.where(WorkOrder.title.contains(str(args['q'])[:100],autoescape=True))
    else:
        month=str(args.get('month') or (utcnow()+timedelta(hours=8)).strftime('%Y-%m'))
        try:start=datetime.strptime(month,'%Y-%m');end=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
        except ValueError:abort(400,description='账期应为年-月')
        sub=p.query(Complaint).where(Complaint.created_at>=start-timedelta(hours=8),Complaint.created_at<end-timedelta(hours=8)).subquery()
        return {'month':month,'items':[{'building_id':bid,'count':n} for bid,n in db.execute(select(sub.c.building_id,func.count()).group_by(sub.c.building_id).order_by(func.count().desc()))]}
    rows=list(db.scalars(q.limit(101)))
    return {'items':[safe_record(snapshot(x)) for x in rows[:100]],'truncated':len(rows)>100,'message':'仅返回当前账号授权范围内的最小必要数据'}
