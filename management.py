import json,uuid
from datetime import datetime,date,timedelta
from decimal import Decimal
from flask import Blueprint,g,request,render_template,abort,jsonify,redirect,flash,current_app
from sqlalchemy import select,func,or_,String
from models import *
from permissions import Policy,effective,ROLES,ALL_PERMISSIONS
from property_service import PropertyService,COMMANDS,snapshot,MODULES
from management_ui import *
bp=Blueprint('management',__name__)

@bp.before_request
def require_login():
    if not g.user:abort(401)
    g.policy=Policy(g.db,g.user)

def module(name):
    spec=MODULE_CONFIG.get(name)
    if not spec:abort(404)
    g.policy.require(spec[2]);return spec

def display(value,key=''):
    if value is None:return '—'
    if key in REFS and isinstance(value,int):
        cache=g.setdefault('reference_labels',{})
        if (key,value) not in cache:
            cls=MODULE_CONFIG[REFS[key]][0];p=g.get('policy') or Policy(g.db,g.user)
            obj=g.db.scalar(p.query(cls).where(cls.id==value))
            cache[(key,value)]=label_record(obj) if obj else '#'+str(value)
        return cache[(key,value)]
    if isinstance(value,bool):return '是' if value else '否'
    if isinstance(value,datetime):return (value+timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
    if isinstance(value,date):return value.isoformat()
    if key.endswith('_cents'):return f'¥{value/100:,.2f}'
    if key in {'before_data','after_data','calculation'} and value:
        try:return json.dumps(json.loads(value),ensure_ascii=False,indent=2)
        except (ValueError,TypeError):pass
    return VALUES.get(str(value),str(value))

def effective_status(obj):
    if isinstance(obj,HousePerson) and obj.status=='active':
        if obj.start_at>utcnow():return '尚未生效'
        if obj.end_at and obj.end_at<=utcnow():return '已到期'
    if isinstance(obj,Lease) and obj.status=='active' and obj.end_date<(utcnow()+timedelta(hours=8)).date():return '租期已到，待退租核验'
    if isinstance(obj,WorkOrder):
        from services import STATUS_TEXT
        return STATUS_TEXT[obj.status]
    return display(getattr(obj,'status',None),'status')

def label_record(o):
    cid=getattr(o,'community_id',None)
    community=g.db.get(Community,cid) if cid else None
    prefix=(community.name+' / ') if community else ''
    if isinstance(o,House):return f'{prefix}{o.building_name} / {o.unit} / {o.room_no}（#{o.id}）'
    if isinstance(o,Building):return f'{prefix}{o.name}（#{o.id}）'
    if isinstance(o,PropertyUnit):
        building=g.db.get(Building,o.building_id)
        return f'{prefix}{building.name if building else o.building_id} / {o.name}（#{o.id}）'
    if isinstance(o,User):return f'{o.real_name or o.username}（{o.username}，#{o.id}）'
    if isinstance(o,Person):return f'{o.name} / {o.phone}（#{o.id}）'
    return str(next((getattr(o,k) for k in ['title','name','plate','code'] if getattr(o,k,None)),getattr(o,'id','')))+f'（#{getattr(o,"id",getattr(o,"code",""))}）'

@bp.app_context_processor
def context():
    user=g.get('user');p=Policy(g.db,user) if user else None
    return {'business_can':p.has if p else lambda permission:False,'business_status':effective_status,'business_menu':[(key,s[1]) for key,s in MODULE_CONFIG.items() if p and p.has(s[2])], 'business_role_names': '、'.join(ROLES.get(c,(c,))[0] for c in sorted(p.roles)) if p else '', 'business_display':display,'business_labels':LABELS}

def listing(name):
    cls,title,perm,columns,create=module(name);q=g.policy.query(cls)
    search=request.args.get('q','').strip()
    if len(search)>100:abort(400)
    if search:
        cols=[c for c in cls.__table__.columns if isinstance(c.type,String) and c.name not in {'password_hash','avatar'}]
        q=q.where(or_(*[c.contains(search,autoescape=True) for c in cols],cls.id==int(search) if search.isascii() and search.isdigit() and hasattr(cls,'id') else False))
    for key in ['house_id','person_id','building_id','community_id']:
        if request.args.get(key) and hasattr(cls,key):
            try:value=int(request.args[key])
            except ValueError:abort(400)
            q=q.where(getattr(cls,key)==value)
    state=request.args.get('status')
    if state and hasattr(cls,'status'):q=q.where(cls.status==state)
    try:page=int(request.args.get('page','1'))
    except ValueError:abort(400)
    if not 1<=page<=100000:abort(400)
    total=g.db.scalar(select(func.count()).select_from(q.subquery()))
    key=cls.id if hasattr(cls,'id') else cls.code
    rows=list(g.db.scalars(q.order_by(key.desc()).offset((page-1)*25).limit(25)))
    return cls,title,columns.split(),create if create and g.policy.has(COMMANDS[create][0]) else None,rows,page,total

@bp.get('/manage/<name>')
def page(name):
    cls,title,columns,create,rows,p,total=listing(name)
    return render_template('management_list.html',module=name,title=title,columns=columns,create=create,rows=rows,page=p,total=total,q=request.args.get('q',''),action_names=ACTION_NAMES)

@bp.get('/api/manage/<name>')
def api_list(name):
    _,_,_,_,rows,page,total=listing(name)
    return jsonify(items=[snapshot(x) for x in rows],page=page,total=total)

def detail(name,id):
    cls,*_=module(name)
    if cls is RbacRole:
        o=g.db.get(cls,id)
        if not o:abort(404)
    else:
        if not id.isdigit():abort(404)
        o=g.policy.get(cls,int(id))
    extras={}
    if cls is Lease and o.status=='active' and o.end_date<(utcnow()+timedelta(hours=8)).date():extras['到期提醒']=['租期已到，请核验实际入住情况并办理退租或新的租赁登记。']
    if cls is House:
        extras['房屋人员关系']=[snapshot(r) for r in g.db.scalars(g.policy.query(HousePerson).where(HousePerson.house_id==o.id).order_by(HousePerson.id.desc()))]
        extras['租赁历史']=[snapshot(r) for r in g.db.scalars(g.policy.query(Lease).where(Lease.house_id==o.id).order_by(Lease.id.desc()))]
    if cls is Person:extras['房屋关系与历史']=[snapshot(r) for r in g.db.scalars(g.policy.query(HousePerson).where(HousePerson.person_id==o.id))]
    if cls is User:extras['岗位与数据范围']=Policy(g.db,o).identity()
    if cls is RbacRole:extras['权限']=list(g.db.scalars(select(RolePermission.permission).where(RolePermission.role_code==o.code)))
    if cls is WorkOrder:extras['流转记录']=[snapshot(r) for r in g.db.scalars(select(OrderLog).where(OrderLog.order_id==o.id).order_by(OrderLog.id))]
    if cls is Bill:extras['收款记录']=[snapshot(r) for r in g.db.scalars(g.policy.query(Payment).where(Payment.bill_id==o.id))]
    return o,extras

@bp.get('/api/manage/<name>/<id>')
def api_detail(name,id):
    o,extras=detail(name,id);return jsonify(record=snapshot(o),related=extras)

@bp.get('/manage/<name>/<id>')
def detail_page(name,id):
    o,extras=detail(name,id);actions=[]
    for command in ACTIONS[name]:
        if not g.policy.has(COMMANDS[command][0]):continue
        params={'id':id,'version':getattr(o,'version','')}
        if command=='property.archive':params['kind']={Community:'community',Building:'building',PropertyUnit:'unit',House:'house'}[type(o)]
        if name=='houses' and command in {'relation.bind','lease.create','order.create','bill.create'}:params={'house_id':id}
        if name=='parking' and command=='parking.assign':params={'space_id':id}
        if name=='devices' and command=='inspection.create':params={'device_id':id}
        if name=='bills' and command=='payment.record':params={'bill_id':id,'version':o.version}
        if name=='work-orders':
            allowed={0:{'order.assign','order.cancel'},1:{'order.reassign','order.accept','order.cancel'},2:{'order.progress','order.finish'},3:{'order.close','order.reopen'},4:{'order.evaluate'},5:set()}[o.status]
            if command not in allowed:continue
            if command in {'order.accept','order.progress','order.finish'} and o.repairer_id!=g.user.id:continue
        if name=='inspections' and (o.status!='pending' or o.assignee_id!=g.user.id):continue
        actions.append((command,params))
    return render_template('management_detail.html',module=name,title=MODULE_CONFIG[name][1],obj=o,record=snapshot(o),extras=extras,actions=actions,action_names=ACTION_NAMES)

@bp.get('/api/options/<name>')
def options(name):
    spec=MODULE_CONFIG.get(name)
    if not spec:abort(404)
    cls=spec[0]
    if name=='staff':
        if not any(g.policy.has(p) for p in ['staff.manage','order.dispatch','inspection.assign','complaint.handle','person.write']):abort(403)
    else:g.policy.require(spec[2])
    q=g.policy.query(cls);search=request.args.get('q','').strip()
    if len(search)>80:abort(400)
    if search:
        cols=[getattr(cls,k) for k in ['name','username','real_name','phone','title','building_name','plate','code'] if hasattr(cls,k)]
        q=q.where(or_(*[c.contains(search,autoescape=True) for c in cols],cls.id==int(search) if search.isascii() and search.isdigit() and hasattr(cls,'id') else False))
    purpose=request.args.get('purpose','')
    rows=list(g.db.scalars(q.limit(100)))
    if cls is User:
        permission={'repairer_id':'order.work','assignee_id':request.args.get('worker_permission','inspection.write')}.get(purpose)
        rows=[u for u in rows if u.active and (Policy(g.db,u).has(permission) if permission else True)]
    if cls is RbacRole:rows=[r for r in rows if (set(g.db.scalars(select(RolePermission.permission).where(RolePermission.role_code==r.code)))-{'resident.self'})<=g.policy.permissions]
    return jsonify(items=[{'id':getattr(o,'id',getattr(o,'code',None)),'label':label_record(o)} for o in rows],limit=100)

@bp.route('/operations/<command>',methods=['GET','POST'])
def operation(command):
    if command not in COMMANDS:abort(404)
    permission,high,fields=COMMANDS[command];g.policy.require(permission)
    if request.method=='POST':
        if high and request.form.get('confirmed')!='1':abort(400,description='请核对内容并勾选确认')
        data={k:v for k,v in request.form.items() if k in fields.split() and v!=''}
        for key in ['person_ids','role_codes','permissions']:
            if key in fields.split() and key in request.form:data[key]=request.form.getlist(key)
        result=PropertyService(g.db,g.user,trace_id=g.trace_id).run(command,data,request.form.get('request_key'))
        flash(result['message'],'success');return redirect(result.get('url','/dashboard'))
    values={k:v for k,v in request.args.items() if k in fields.split()}
    if values.get('id'):
        root=command.split('.')[0];mapping={'community':Community,'building':Building,'unit':PropertyUnit,'house':House,'person':Person,'relation':HousePerson,'lease':Lease,'staff':User,'order':WorkOrder,'complaint':Complaint,'notice':Notice,'visitor':Visitor,'vehicle':Vehicle,'parking':ParkingUse if command=='parking.release' else ParkingSpace,'device':Device,'inspection':Inspection,'fee':FeeItem,'bill':Bill,'payment':Payment}
        cls=mapping.get(root)
        if root=='property':cls={'community':Community,'building':Building,'unit':PropertyUnit,'house':House}.get(values.get('kind'))
        if cls:
            try:oid=int(values['id'])
            except ValueError:abort(400)
            o=g.policy.get(cls,oid)
            for k in fields.split():
                if hasattr(o,k):
                    v=getattr(o,k)
                    if v is not None:values[k]=v.isoformat() if hasattr(v,'isoformat') else str(int(v)) if isinstance(v,bool) else str(v)
    if command=='staff.roles' and values.get('id'):
        p=Policy(g.db,g.policy.get(User,int(values['id'])))
        values['role_codes']=','.join(sorted(p.roles))
        if p.scopes:
            first=p.scopes[0];values.update(scope_kind=first.kind,community_id=str(first.community_id or ''),building_id=str(first.building_id or ''))
    enums=dict(ENUMS)
    enums['role_codes']={r.code:r.name for r in g.db.scalars(select(RbacRole)) if (set(g.db.scalars(select(RolePermission.permission).where(RolePermission.role_code==r.code)))-{'resident.self'})<=g.policy.permissions or g.policy.super}
    enums['permissions']={p:p for p in sorted(g.policy.permissions)}
    if command=='property.archive':enums['kind']={'community':'小区','building':'楼栋','unit':'单元','house':'房屋'}
    optional={'id','version','community_id','building_id','user_id','requester_person_id','emergency_contact','note','model','comment','is_resident','fault','auth_version','occupancy'}
    required_update=bool(values.get('id'))
    return render_template('management_form.html',command=command,title=ACTION_NAMES[command],fields=fields.split(),values=values,enums=enums,refs=REFS,optional=optional,high=high,request_key=uuid.uuid4().hex,required_update=required_update)

@bp.post('/api/business/<command>')
def execute_api(command):
    if command not in COMMANDS:abort(404)
    data=request.get_json(silent=True)
    if not isinstance(data,dict):abort(400)
    g.policy.require(COMMANDS[command][0])
    if COMMANDS[command][1] and data.get('confirmed') is not True:abort(400,description='此操作需要核验后确认')
    return jsonify(PropertyService(g.db,g.user,trace_id=g.trace_id).run(command,data.get('data',{}),request.headers.get('Idempotency-Key')))

@bp.get('/api/reports/finance')
def finance():
    g.policy.require('billing.read');q=g.policy.query(Bill).where(Bill.status!='void').subquery()
    amount,paid=g.db.execute(select(func.coalesce(func.sum(q.c.amount_cents),0),func.coalesce(func.sum(q.c.paid_cents),0))).one()
    return jsonify(receivable_cents=int(amount),paid_cents=int(paid),unpaid_cents=int(amount-paid))

@bp.get('/api/reports/complaints')
def complaints_report():
    g.policy.require('complaint.handle');month=request.args.get('month',(utcnow()+timedelta(hours=8)).strftime('%Y-%m'))
    try:start=datetime.strptime(month,'%Y-%m');end=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
    except ValueError:abort(400)
    q=g.policy.query(Complaint).where(Complaint.created_at>=start-timedelta(hours=8),Complaint.created_at<end-timedelta(hours=8)).subquery()
    rows=g.db.execute(select(q.c.building_id,func.count()).group_by(q.c.building_id).order_by(func.count().desc())).all()
    return jsonify(month=month,items=[{'building_id':bid,'count':n} for bid,n in rows])
