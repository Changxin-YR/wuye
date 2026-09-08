import hashlib
import io
import json
import os
import re
import secrets
import uuid
from datetime import timedelta
from threading import BoundedSemaphore
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, g, jsonify, make_response, redirect, render_template, request, send_from_directory, session, stream_with_context, url_for
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from agent_tools import action_view, available_commands, confirm, grant_actor, propose, structured_result
from agent_planner import building_name_variants, plan_request
from agent_security import error_code_for, issue_agent_token, redact_provider_text, safe_record
from business import BusinessService
from database import make_engine, missing_schema
from bootstrap import seed_catalog
from database import environment
from management import bp as management_bp
from property_service import audit as domain_audit,snapshot as domain_snapshot
from business_queries import query as domain_query
from permissions import Policy
from dify_client import BailianClient, DeepSeekClient, DifyClient, DifyUnavailable, OpenAICompatibleAgentClient, _PLANNER_HINT, _TOOL_COMMANDS
from models import AiAction, AiGrant, AiConversation, AuditLog, Base, Community, Evaluation, House, Notice, Notification, OrderLog, Person, User, WorkOrder, utcnow
from services import InvalidTransition, ORDER_TYPES, ROLE_TEXT, STATUS_TEXT, can_access_order, log_order, notify, notify_admins, scope_orders, transition_status

PROJECT_ROOT=Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT/'.env')
AGENT_SYSTEM_PROMPT=(PROJECT_ROOT/'config'/'agent_system_prompt.md').read_text(encoding='utf-8')


def create_app(test_config=None):
    app=Flask(__name__)
    app.wsgi_app=ProxyFix(app.wsgi_app,x_prefix=1)
    app.config.from_mapping(SECRET_KEY=os.getenv('SECRET_KEY',''),DATABASE_URL=os.getenv('DATABASE_URL',''),
        UPLOAD_FOLDER=os.getenv('UPLOAD_FOLDER','uploads'),MAX_CONTENT_LENGTH=8*1024*1024,
        SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE','0')=='1',PERMANENT_SESSION_LIFETIME=timedelta(hours=4),
        AI_PROVIDER=os.getenv('AI_PROVIDER','bailian'),
        BAILIAN_BASE_URL=os.getenv('BAILIAN_BASE_URL','https://dashscope.aliyuncs.com/compatible-mode/v1'),
        BAILIAN_API_KEY=os.getenv('BAILIAN_API_KEY') or os.getenv('DASHSCOPE_API_KEY',''),BAILIAN_MODEL=os.getenv('BAILIAN_MODEL','qwen-plus'),
        DEEPSEEK_BASE_URL=os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com'),
        DEEPSEEK_API_KEY=os.getenv('DEEPSEEK_API_KEY') or os.getenv('DEEPSEEK_KEY',''),DEEPSEEK_MODEL=os.getenv('DEEPSEEK_MODEL','deepseek-v4-pro'),
        DIFY_BASE_URL=os.getenv('DIFY_BASE_URL','http://127.0.0.1/v1'),DIFY_API_KEY=os.getenv('DIFY_API_KEY',''),
        DIFY_TIMEOUT=int(os.getenv('DIFY_TIMEOUT','60')),APP_ENV=os.getenv('APP_ENV','production'))
    if test_config:app.config.update(test_config)
    if not app.testing and (len(app.config['SECRET_KEY'])<32 or app.config['SECRET_KEY'].startswith('replace-')):
        raise RuntimeError('请配置至少32位随机SECRET_KEY，参见README')
    if not app.config['DATABASE_URL']:raise RuntimeError('请配置DATABASE_URL，参见README')
    engine=make_engine(app.config['DATABASE_URL'])
    if app.testing:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:seed_catalog(conn);environment(conn,'test')
        app.config['APP_ENV']='test'
    elif missing_schema(engine):
        engine.dispose()
        raise RuntimeError('数据库尚未初始化或需要升级，请运行python manage.py init-db或upgrade-db')
    if not app.testing:
        with engine.begin() as conn:environment(conn,app.config['APP_ENV'])
    ai_slots=BoundedSemaphore(2)
    app.register_blueprint(management_bp)
    factory=sessionmaker(bind=engine,expire_on_commit=False,class_=Session)
    legacy_dify = bool(test_config and ('DIFY_BASE_URL' in test_config or 'DIFY_API_KEY' in test_config) and 'AI_PROVIDER' not in test_config)
    if app.config['AI_PROVIDER']=='dify' or legacy_dify:
        ai_client=DifyClient(app.config['DIFY_BASE_URL'],app.config['DIFY_API_KEY'],app.config['DIFY_TIMEOUT'])
    elif str(app.config['AI_PROVIDER']).lower()=='deepseek':
        ai_client=DeepSeekClient(app.config['DEEPSEEK_BASE_URL'],app.config['DEEPSEEK_API_KEY'],app.config['DEEPSEEK_MODEL'],app.config['DIFY_TIMEOUT'])
    else:
        ai_client=BailianClient(app.config['BAILIAN_BASE_URL'],app.config['BAILIAN_API_KEY'],app.config['BAILIAN_MODEL'],app.config['DIFY_TIMEOUT'])
    app.extensions.update(db_engine=engine,db_session=factory,dify=ai_client,ai=ai_client,agent_contexts={})
    folder=Path(app.config['UPLOAD_FOLDER'])
    if not folder.is_absolute():folder=PROJECT_ROOT/folder
    folder.mkdir(parents=True,exist_ok=True);app.config['UPLOAD_FOLDER']=str(folder)

    def wants_json():return request.path.startswith('/ai/') or request.path=='/health' or request.path.startswith('/api/')

    def error_response(message,status,code=None):
        if wants_json():return make_response(jsonify(error=message,code=code or error_code_for(status,str(message))),status)
        return make_response(render_template('error.html',message=message,status=status),status)

    @app.before_request
    def begin():
        g.db=factory();g.new_files=[];g.user=None;g.trace_id=uuid.uuid4().hex
        uid=session.get('user_id')
        if uid:
            user=g.db.get(User,uid)
            if user and user.active and user.role in ROLE_TEXT and session.get('auth_version')==user.auth_version:g.user=user
            else:session.clear()
        if 'csrf_token' not in session:session['csrf_token']=secrets.token_urlsafe(32)
        if (request.path.startswith('/ai/') or (request.path.startswith('/api/') and request.path!='/api/agent/tools')) and not g.user:return jsonify(error='登录已失效，请重新登录。'),401
        if request.method in {'POST','PUT','PATCH','DELETE'} and request.path!='/api/agent/tools':
            token=request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
            if not isinstance(token,str) or not secrets.compare_digest(token,session['csrf_token']):abort(400,description='页面验证已过期，请刷新后重试')

    @app.after_request
    def finalize(response):
        db=g.get('db');failed=response.status_code>=400
        try:
            if db:
                if request.method in {'POST','PUT','PATCH','DELETE'} and not failed:db.commit()
                else:db.rollback()
        except (StaleDataError,IntegrityError):
            db.rollback();failed=True
            session['_flashes']=[x for x in session.get('_flashes',[]) if x[0]!='success']
            response=error_response('数据已更新或存在重复记录，请刷新后重试。',409)
        except SQLAlchemyError:
            db.rollback();failed=True
            session['_flashes']=[x for x in session.get('_flashes',[]) if x[0]!='success']
            app.logger.error('database_transaction_failed')
            response=error_response('数据保存失败，请稍后重试。',503)
        if failed:
            for name in g.get('new_files',[]):(folder/name).unlink(missing_ok=True)
        response.headers['X-Request-ID']=g.get('trace_id','')
        if failed and g.get('user') and request.method in {'POST','PUT','PATCH','DELETE'}:
            try:
                with factory() as log_db:
                    actor=log_db.get(User,g.user.id)
                    if actor:domain_audit(log_db,actor,request.path[:50],request.path[:100],source='agent' if request.path=='/api/agent/tools' else 'manual',trace_id=g.get('trace_id'),status='failure',error=(response.get_json(silent=True) or {}).get('error','操作未成功'))
                    log_db.commit()
            except SQLAlchemyError:app.logger.error('audit_write_failed trace=%s',g.get('trace_id'))
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith('/static/') and request.path.endswith('.js'):
            response.headers['Content-Type']='application/javascript; charset=utf-8'
        if not request.path.startswith('/static/'):response.headers['Cache-Control']='no-store'
        return response

    @app.teardown_request
    def end(error=None):
        db=g.pop('db',None)
        if db:db.rollback();db.close()

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages={401:'请先登录',403:'你没有权限执行此操作',404:'内容不存在或当前不可访问',413:'文件或请求过大'}
        shown=messages.get(exc.code,exc.description)
        # Keep a data-scope distinction for API/Agent clients without exposing rows.
        basis=str(exc.description or shown)
        return error_response(shown,exc.code,error_code_for(exc.code,basis))

    @app.errorhandler(InvalidTransition)
    def transition_error(exc):return error_response(str(exc),409)

    @app.errorhandler(IntegrityError)
    @app.errorhandler(StaleDataError)
    def conflict_error(exc):
        g.db.rollback();return error_response('数据已变化或存在重复记录，请刷新后重试。',409)

    @app.errorhandler(SQLAlchemyError)
    def db_error(exc):
        g.db.rollback();app.logger.error('database_request_failed');return error_response('数据库暂时不可用。',503)

    @app.context_processor
    def helpers():
        unread=0
        if g.get('user'):
            try:unread=g.db.scalar(select(func.count(Notification.id)).where(Notification.user_id==g.user.id,Notification.is_read.is_(False))) or 0
            except SQLAlchemyError:g.db.rollback();unread=0
        return {'current_user':g.get('user'),'status_text':STATUS_TEXT.get,'role_text':ROLE_TEXT.get,
                'csrf_token':session.get('csrf_token',''),'unread_count':unread,'order_types':sorted(ORDER_TYPES)}

    @app.template_filter('cn_time')
    def cn_time(value):return (value+timedelta(hours=8)).strftime('%Y-%m-%d %H:%M') if value else '—'

    def login_required(view):
        @wraps(view)
        def wrapped(*args,**kwargs):
            if not g.user:
                if wants_json():return jsonify(error='请重新登录'),401
                return redirect(url_for('login',next=request.path))
            return view(*args,**kwargs)
        return wrapped

    def role_required(*roles):
        def decorator(view):
            @wraps(view)
            @login_required
            def wrapped(*args,**kwargs):
                if g.user.role not in roles or (roles==(0,) and not Policy(g.db,g.user).super):abort(403)
                return view(*args,**kwargs)
            return wrapped
        return decorator

    def field(name,maxlen,required=False,values=None):
        value=request.form.get(name,'').strip()
        if (required and not value) or len(value)>maxlen:abort(400,description=f'{name}不能为空或超过{maxlen}字')
        if values is not None and value not in values:abort(400,description='请选择有效的'+name)
        return value

    def number(name,minimum=1):
        raw=request.form.get(name,'')
        if not raw.isascii() or not raw.isdigit() or len(raw)>10 or int(raw)<minimum:abort(400,description='数字参数无效: '+name)
        return int(raw)

    def phone(value):
        if not re.fullmatch(r'[0-9+() -]{6,20}',value):abort(400,description='请填写有效联系电话')
        return value

    def password(value):
        if not isinstance(value,str) or not 8<=len(value)<=128:abort(400,description='新密码长度须为8—128位')
        return value

    def versioned(obj):
        if number('version')!=obj.version:abort(409,description='记录已被更新，请刷新后再操作')

    def audit(action,target,detail=''):
        domain_audit(g.db,g.user,action,target,g.get('profile_before'),domain_snapshot(g.user) if action=='profile_update' else {'detail':detail},source='agent' if request.path.startswith('/ai/actions/') else 'manual',trace_id=g.trace_id,obj=g.user if action=='profile_update' else None)

    def paginate(query,per_page=20):
        raw=request.args.get('page','1')
        if not raw.isascii() or not raw.isdigit() or len(raw)>6 or int(raw)<1:abort(400,description='页码无效')
        page=int(raw);total=g.db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
        return list(g.db.scalars(query.limit(per_page).offset((page-1)*per_page))),page,total

    def save_image(upload):
        if not upload or not upload.filename:return ''
        raw=upload.read(5*1024*1024+1)
        if len(raw)>5*1024*1024:abort(413,description='图片不得超过5MB')
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.format not in {'JPEG','PNG','WEBP'} or im.width*im.height>20_000_000:raise ValueError()
                im.verify()
            with Image.open(io.BytesIO(raw)) as im:
                im=im.convert('RGB');im.thumbnail((1800,1800));name=uuid.uuid4().hex+'.jpg';im.save(folder/name,'JPEG',quality=88)
        except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError):abort(400,description='请上传真实的JPEG、PNG或WebP图片')
        g.new_files.append(name);return name

    def get_order(order_no,lock=False):
        q=select(WorkOrder).where(WorkOrder.order_no==order_no)
        if lock:q=q.with_for_update()
        order=g.db.scalar(q)
        if not order:abort(404)
        if not can_access_order(g.user,order):abort(403)
        return order

    @app.get('/')
    def index():return redirect(url_for('dashboard' if g.user else 'login'))

    @app.route('/auth/login',methods=['GET','POST'])
    def login():
        if request.method=='POST':
            username=field('username',50,True);pw=request.form.get('password','')
            if len(pw)>128:abort(400,description='密码过长')
            user=g.db.scalar(select(User).where(User.username==username).with_for_update())
            if user and user.locked_until and user.locked_until<=utcnow():user.failed_logins=0;user.locked_until=None
            invalid=not user or not user.active or (user.locked_until and user.locked_until>utcnow())
            if invalid or not check_password_hash(user.password_hash,pw):
                if user and user.active and not(user.locked_until and user.locked_until>utcnow()):
                    user.failed_logins+=1
                    if user.failed_logins>=5:user.locked_until=utcnow()+timedelta(minutes=10)
                flash('用户名或密码错误、账号停用或暂时锁定。请稍后重试。','error')
            else:
                user.failed_logins=0;user.locked_until=None;session.clear()
                session.update(user_id=user.id,auth_version=user.auth_version,csrf_token=secrets.token_urlsafe(32));session.permanent=True
                target=request.args.get('next','');parsed=urlsplit(target)
                if not target.startswith('/') or target.startswith('//') or parsed.scheme or parsed.netloc or any(c in target for c in ['\\','\r','\n']):target=url_for('dashboard')
                elif request.script_root and target != request.script_root and not target.startswith(request.script_root.rstrip('/')+'/'):
                    target=request.script_root.rstrip('/')+target
                return redirect(target)
        return render_template('login.html')

    @app.route('/auth/register',methods=['GET','POST'])
    def register():
        if request.method=='POST':
            username=field('username',50,True)
            if not re.fullmatch(r'[\w.-]{3,50}',username):abort(400,description='用户名须为3—50位字母、数字、中文或._-')
            if request.form.get('role','2')!='2':abort(400,description='仅开放业主注册，维修人员请联系管理员开通')
            if g.db.scalar(select(User.id).where(User.username==username)):abort(409,description='用户名已存在')
            g.db.add(User(username=username,password_hash=generate_password_hash(password(request.form.get('password',''))),role=2))
            flash('注册成功，请登录。房屋须由物业核验后绑定。','success');return redirect(url_for('login'))
        return render_template('register.html')

    @app.post('/auth/logout')
    @login_required
    def logout():session.clear();return redirect(url_for('login'))

    @app.get('/health')
    def health():
        g.db.execute(select(1));return jsonify(status='ok',database='ok')

    @app.get('/dashboard')
    @login_required
    def dashboard():
        q=scope_orders(select(WorkOrder),g.user)
        counts={s:0 for s in STATUS_TEXT}
        rows=g.db.execute(scope_orders(select(WorkOrder.status,func.count(WorkOrder.id)),g.user).group_by(WorkOrder.status)).all();counts.update(dict(rows))
        return render_template('dashboard.html',orders=list(g.db.scalars(q.order_by(WorkOrder.created_at.desc()).limit(8))),counts=counts,notices=list(g.db.scalars(Policy(g.db,g.user).query(Notice).order_by(Notice.created_at.desc()).limit(5))))

    @app.get('/orders')
    @login_required
    def orders():
        q=scope_orders(select(WorkOrder),g.user).order_by(WorkOrder.created_at.desc(),WorkOrder.id.desc())
        status=request.args.get('status','');search=request.args.get('q','').strip()
        if len(search)>100:abort(400)
        if status:
            if status not in {str(s) for s in STATUS_TEXT}:abort(400,description='工单状态无效')
            q=q.where(WorkOrder.status==int(status))
        if search:q=q.where(or_(WorkOrder.title.contains(search,autoescape=True),WorkOrder.order_no.contains(search,autoescape=True)))
        rows,page,total=paginate(q)
        return render_template('orders.html',orders=rows,selected_status=status,q=search,page=page,total=total)

    @app.route('/orders/new',methods=['GET','POST'])
    @role_required(2)
    def new_order():
        if request.method=='POST':
            service=BusinessService(g.db,g.user,request.form)
            order=service.create_order(save_image(request.files.get('image')))
            flash('报修已提交','success');return redirect(url_for('order_detail',order_no=order.order_no))
        houses=list(g.db.scalars(Policy(g.db,g.user).query(House)))
        return render_template('order_form.html',houses=houses)

    @app.route('/orders/<order_no>',methods=['GET','POST'])
    @login_required
    def order_detail(order_no):
        order=get_order(order_no,request.method=='POST')
        if request.method=='POST':
            BusinessService(g.db,g.user,request.form).order_action(order_no)
            flash('操作成功','success');return redirect(url_for('order_detail',order_no=order_no))
        return render_template('order_detail.html',order=order,
            logs=list(g.db.scalars(select(OrderLog).where(OrderLog.order_id==order.id).order_by(OrderLog.id.desc()))),
            repairers=list(g.db.scalars(select(User).where(User.role==1,User.active.is_(True)))) if g.user.role==0 else [],
            evaluation=g.db.scalar(select(Evaluation).where(Evaluation.order_id==order.id)),
            owner=g.db.get(User,order.owner_id),repairer=g.db.get(User,order.repairer_id) if order.repairer_id else None,
            house=g.db.get(House,order.house_id) if order.house_id else None)

    @app.route('/houses',methods=['GET','POST'])
    @role_required(0)
    def houses():
        if request.method=='POST':
            BusinessService(g.db,g.user,request.form).house_action()
            flash('房屋操作成功','success');return redirect(url_for('houses'))
        rows,page,total=paginate(select(House).order_by(House.building_name,House.unit,House.room_no))
        users={u.id:u for u in g.db.scalars(select(User).where(User.role==2))}
        return render_template('houses.html',houses=rows,owners=users,page=page,total=total)

    @app.get('/my-houses')
    @role_required(2)
    def my_houses():return render_template('my_houses.html',houses=list(g.db.scalars(Policy(g.db,g.user).query(House))))

    @app.route('/users',methods=['GET','POST'])
    @role_required(0)
    def users():
        if request.method=='POST':
            BusinessService(g.db,g.user,request.form).user_action()
            flash('用户操作成功','success');return redirect(url_for('users'))
        q=request.args.get('q','').strip()
        if len(q)>50:abort(400)
        query=select(User).order_by(User.id)
        if q:query=query.where(User.username.contains(q,autoescape=True))
        rows,page,total=paginate(query)
        return render_template('users.html',users=rows,page=page,total=total,q=q)

    @app.route('/notices',methods=['GET','POST'])
    @login_required
    def notices():
        if request.method=='POST':
            if not Policy(g.db,g.user).super:abort(403)
            BusinessService(g.db,g.user,request.form).notice_action()
            flash('公告操作成功','success');return redirect(url_for('notices'))
        rows,page,total=paginate(Policy(g.db,g.user).query(Notice).order_by(Notice.id.desc()))
        return render_template('notices.html',notices=rows,page=page,total=total)

    @app.route('/notifications',methods=['GET','POST'])
    @login_required
    def notifications():
        if request.method=='POST':
            item=g.db.get(Notification,number('id'))
            if not item or item.user_id!=g.user.id:abort(403)
            item.is_read=True;return redirect(url_for('notifications'))
        rows,page,total=paginate(select(Notification).where(Notification.user_id==g.user.id).order_by(Notification.id.desc()))
        accessible={}
        for item in rows:
            order=g.db.get(WorkOrder,item.order_id) if item.order_id else None
            if order and can_access_order(g.user,order):accessible[item.id]=order.order_no
        return render_template('notifications.html',notifications=rows,links=accessible,page=page,total=total)

    @app.get('/audit')
    @role_required(0)
    def audit_page():
        rows,page,total=paginate(select(AuditLog).order_by(AuditLog.id.desc()))
        return render_template('audit.html',logs=rows,page=page,total=total)

    @app.route('/profile',methods=['GET','POST'])
    @login_required
    def profile():
        if request.method=='POST':
            g.profile_before=domain_snapshot(g.user)
            name=field('real_name',50);ph=field('phone',20)
            if ph:phone(ph)
            new=request.form.get('new_password','')
            if new:
                password(new)
                if not check_password_hash(g.user.password_hash,request.form.get('current_password','')):abort(400,description='原密码不正确')
            image=save_image(request.files.get('avatar'))
            g.user.real_name=name;g.user.phone=ph
            if new:
                g.user.password_hash=generate_password_hash(new);g.user.auth_version+=1;session['auth_version']=g.user.auth_version
            if image:g.user.avatar=url_for('uploaded_file',filename=image)
            flash('资料已更新','success');return redirect(url_for('profile'))
        return render_template('profile.html')

    @app.get('/uploads/<filename>')
    @login_required
    def uploaded_file(filename):
        if not re.fullmatch(r'[a-f0-9]{32}\.(?:jpg|jpeg|png|gif|webp)',filename):abort(404)
        url=url_for('uploaded_file',filename=filename)
        order=g.db.scalar(select(WorkOrder).where(WorkOrder.img_url.in_([filename,url])))
        avatar=g.db.scalar(select(User).where(User.avatar.in_([filename,url])))
        if not ((order and can_access_order(g.user,order)) or (avatar and (avatar.id==g.user.id or Policy(g.db,g.user).super))):abort(403)
        return send_from_directory(folder,filename)

    def ai_context():
        p=Policy(g.db,g.user)
        from property_service import snapshot as record_snapshot
        orders=list(g.db.scalars(p.query(WorkOrder).order_by(WorkOrder.updated_at.desc()).limit(20)))
        notices=list(g.db.scalars(p.query(Notice).order_by(Notice.id.desc()).limit(5)))
        houses=list(g.db.scalars(p.query(House).order_by(House.id).limit(50))) if p.has('property.read') else []
        from business_queries import QUERIES
        permitted_queries=[k for k,v in QUERIES.items() if p.has(v)]
        context={'identity':p.identity(),'queries':permitted_queries,
                 'query_capabilities':[{'command':k,'permission':QUERIES[k],'risk_level':'R0','execution_mode':'READ_ONLY'} for k in permitted_queries],
                 'role':'、'.join(sorted(p.roles)),'scope':'当前账号授权范围，最多20条工单、50套房屋、5条公告；其他记录通过lookup查询',
                 'orders':[safe_record(record_snapshot(o)) for o in orders],
                 'houses':[safe_record(record_snapshot(h)) for h in houses],
                 'notices':[safe_record(record_snapshot(n)) for n in notices], 'commands':available_commands(g.user)}
        if p.super:context['users']=[{'id':u.id,'name':u.real_name or u.username,'active':u.active,'auth_version':u.auth_version} for u in g.db.scalars(p.query(User).limit(100))]
        digest=hashlib.sha256(json.dumps(context,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
        return json.loads(json.dumps(context,ensure_ascii=False,default=str)),digest

    @app.get('/ai')
    @login_required
    def ai_page():return render_template('ai.html',ai_configured=app.extensions['dify'].configured)

    @app.get('/ai/status')
    @login_required
    def ai_status():return jsonify(configured=app.extensions['dify'].configured,connection_verified=False,provider=getattr(app.extensions['dify'],'provider','dify'),model=getattr(app.extensions['dify'],'model',None),message='已填写 AI 服务配置，连接状态待检测。')

    @app.post('/ai/check')
    @role_required(0)
    def ai_check():
        try:return jsonify(app.extensions['dify'].check(infer=True))
        except DifyUnavailable as exc:return jsonify(error=str(exc),code=exc.code),503

    @app.post('/ai/chat')
    @login_required
    def ai_chat():
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict):return jsonify(error='请求须为JSON对象'),400
        message=payload.get('message')
        if not isinstance(message,str) or not message.strip() or len(message)>2000:return jsonify(error='请输入1—2000字的问题'),400
        cid=payload.get('conversation_id')
        if cid is not None and (not isinstance(cid,str) or len(cid)>36):return jsonify(error='会话标识无效'),400
        conversation=None
        if cid:
            conversation=g.db.get(AiConversation,cid)
            if not conversation or conversation.user_id!=g.user.id:abort(403)
        context,digest=ai_context()
        authorized={item['command'] for item in context.get('commands', []) if isinstance(item,dict)} | set(context.get('queries', []))
        planner_context=dict(app.extensions['agent_contexts'].get((g.user.id,cid),{})) if cid else {}
        if 'resolved_order' not in planner_context:
            accessible_orders=list(g.db.scalars(Policy(g.db,g.user).query(WorkOrder).limit(2)))
            if len(accessible_orders)==1:
                planner_context['resolved_order']={'id':accessible_orders[0].id,'order_no':accessible_orders[0].order_no}
        if Policy(g.db,g.user).resident and 'resolved_house' not in planner_context:
            own_houses=list(g.db.scalars(Policy(g.db,g.user).query(House).limit(2)))
            if len(own_houses)==1:
                planner_context['resolved_house']={'id':own_houses[0].id,'building_name':own_houses[0].building_name,'room_no':own_houses[0].room_no}
                planner_context['resident_current_house']=True
        if {'notice.save', 'notice.batch_publish'} & authorized:
            writable=list(g.db.scalars(Policy(g.db,g.user).query(Community).limit(101)))
            planner_context['writable_communities']=[{'id':c.id,'name':c.name} for c in writable]
        planner_hint=plan_request(message, authorized, planner_context)
        if planner_hint.get('intent')=='notice.save' and (planner_hint.get('arguments') or {}).get('building_name'):
            from models import Building
            building_name=planner_hint['arguments']['building_name']
            names=building_name_variants(building_name)
            planner_context['notice_building_candidates']=len(list(g.db.scalars(Policy(g.db,g.user).query(Building).where(Building.name.in_(names)).limit(3))))
            planner_hint=plan_request(message, authorized, planner_context)
        person_name=(planner_hint.get('arguments') or {}).get('person_name')
        if person_name:
            person_candidates=len(g.db.scalars(Policy(g.db,g.user).query(Person).where(Person.name==person_name).limit(4)).all())
            planner_context['person_candidates']=person_candidates
            planner_hint=plan_request(message, authorized, planner_context)
        if planner_hint.get('intent')=='parking.assign':
            from models import ParkingSpace, Vehicle
            values=planner_hint.get('arguments') or {}
            policy=Policy(g.db,g.user)
            vehicle_candidates=len(g.db.scalars(policy.query(Vehicle).where(Vehicle.plate==values.get('plate')).limit(3)).all()) if values.get('plate') else 0
            parking_candidates=len(g.db.scalars(policy.query(ParkingSpace).where(ParkingSpace.code==values.get('space_code')).limit(3)).all()) if values.get('space_code') else 0
            planner_context['vehicle_candidates']=vehicle_candidates
            planner_context['parking_candidates']=parking_candidates
            planner_hint=plan_request(message, authorized, planner_context)
        if planner_hint.get('action') in {'CLARIFY','DISAMBIGUATE','DENY'} and planner_hint.get('entity_status') != 'REPEAT':
            local_conversation=conversation
            if not local_conversation:
                local_conversation=AiConversation(id=str(uuid.uuid4()),user_id=g.user.id,scope_hash=digest)
                g.db.add(local_conversation);g.db.flush()
            answers={'CLARIFY':'请补充必要的业务信息后再操作。','DISAMBIGUATE':'找到多个匹配对象，请提供更多信息以确认。','DENY':'该请求不在当前登录身份允许的范围内。'}
            return jsonify(answer=answers[planner_hint['action']],conversation_id=local_conversation.id,source='planner',scope=context['scope'],actions=[])
        upstream=conversation.upstream_id if conversation and conversation.scope_hash==digest else ''
        if g.db.scalar(select(func.count(AiGrant.id)).where(AiGrant.user_id==g.user.id,AiGrant.created_at>utcnow()-timedelta(minutes=1)))>=10:
            return jsonify(error='请求过于频繁，请稍后再试'),429
        token=issue_agent_token(Policy(g.db,g.user));gid=str(uuid.uuid4());uid=g.user.id;auth=g.user.auth_version
        g.db.add(AiGrant(id=gid,token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=uid,auth_version=auth,expires_at=utcnow()+timedelta(minutes=3)))
        # Publish the narrow delegated grant before an external provider callback.
        # The database stores only the token hash and every callback rechecks IAM.
        g.db.commit()
        is_bailian=isinstance(app.extensions['dify'],OpenAICompatibleAgentClient)
        if is_bailian:
            # Local Bailian tool callbacks are bound to this closure. The grant
            # secret never needs to be placed in model-visible text.
            prompt=message.strip()
            system_instruction=(AGENT_SYSTEM_PROMPT+'\n当前授权摘要（仅供规划，最终权限以后端实时校验为准）：'+json.dumps(context,ensure_ascii=False)+
                                '\n确定性规划提示（只用于缩小候选，不代表授权）：'+json.dumps(planner_hint,ensure_ascii=False)+
                                '\n本地工具调用无需提供request_token；服务端自动绑定本轮登录身份。')
        else:
            # Dify invokes the exported OpenAPI endpoint itself, so it receives a
            # three-minute delegated token. Provider output is scrubbed before UI.
            prompt=(AGENT_SYSTEM_PROMPT+'\n本次request_token：'+token+'\n授权数据：'+json.dumps(context,ensure_ascii=False)+'\n用户请求：'+message.strip())
            system_instruction=''
        mutation_commands=set()
        def planner_tool_call():
            """Build one conservative callback request for a clear planner result."""
            command=planner_hint.get('intent')
            values=dict(planner_hint.get('arguments') or {})
            repeat_create=planner_hint.get('entity_status')=='REPEAT' and command=='order.create'
            if (planner_hint.get('action') not in {'TOOL','CONFIRM'} and not repeat_create) or not command:
                return None
            if repeat_create:
                return {'operation':'execute','command':'order.create','arguments_json':'{}'}
            from models import Bill, Building, Complaint, FeeItem, Inspection, ParkingSpace, Person as DomainPerson, Vehicle
            policy=Policy(g.db,g.user)
            def one_person(name=None, phone=None):
                if not name and not phone:return None
                q=policy.query(DomainPerson)
                if name:q=q.where(DomainPerson.name==name)
                if phone:q=q.where(DomainPerson.phone==phone)
                rows=list(g.db.scalars(q.limit(2)))
                return rows[0] if len(rows)==1 else None
            def one_house():
                q=policy.query(House)
                if values.get('building_name'):q=q.where(House.building_name==values['building_name'])
                if values.get('unit'):q=q.where(House.unit==values['unit'])
                if values.get('room_no') is not None:q=q.where(House.room_no==values['room_no'])
                rows=list(g.db.scalars(q.limit(2)))
                return rows[0] if len(rows)==1 else None
            def one_order():
                q=policy.query(WorkOrder)
                if values.get('order_no'):q=q.where(WorkOrder.order_no==values['order_no'])
                elif planner_context.get('resolved_order',{}).get('id'):q=q.where(WorkOrder.id==planner_context['resolved_order']['id'])
                else:q=q.order_by(WorkOrder.updated_at.desc())
                rows=list(g.db.scalars(q.limit(2)))
                return rows[0] if len(rows)==1 else None
            params={}
            operation='lookup' if command in {'house.search','person.search','order.search','billing.unpaid','complaint.stats','whoami','notice.read'} else ('propose' if planner_hint.get('action')=='CONFIRM' else 'execute')
            if command=='house.search':
                params={k:values[k] for k in ('building_name','unit','room_no') if k in values}
            elif command=='person.search':
                params={k:values[k] for k in ('person_name','phone') if k in values}
            elif command=='order.search':
                if values.get('order_no'):params={'order_no':values['order_no']}
                elif planner_context.get('resolved_order',{}).get('id'):params={'id':planner_context['resolved_order']['id']}
            elif command in {'notice.read','whoami','complaint.stats'}:
                params={}
            elif command=='billing.unpaid':
                params={k:values[k] for k in ('building_name','unit','room_no','month') if k in values}
                if values.get('person_name') or values.get('phone'):
                    person=one_person(values.get('person_name'),values.get('phone'))
                    if not person:return None
                    params['person_id']=person.id
            elif command=='person.save':
                person=planner_context.get('resolved_person') or {}
                if not person.get('id'):
                    found=one_person(values.get('person_name'),values.get('phone'))
                    if not found:return None
                    person={'id':found.id,'name':found.name}
                current=g.db.get(DomainPerson,int(person['id']))
                if not current:return None
                params={'id':current.id,'version':current.version,'community_id':current.community_id,'name':current.name,'phone':values['phone']}
            elif command=='notice.archive':
                notice_id=values.get('notice_id') or (planner_context.get('resolved_notice') or {}).get('id')
                if not notice_id:return None
                notice=policy.get(Notice,int(notice_id))
                params={'id':notice.id,'version':notice.version,'reason':message.strip()}
            elif command in {'notice.save','notice.batch_publish'}:
                communities=planner_context.get('writable_communities') or []
                named=values.get('community_name')
                matches=[row for row in communities if not named or row.get('name')==named]
                title=values.get('notice_title') or values.get('title')
                content=values.get('notice_content') or values.get('content')
                if not title or not content or not communities:return None
                if command=='notice.save' and values.get('notice_id'):
                    notice=policy.get(Notice,int(values['notice_id']))
                    params={'id':notice.id,'version':notice.version,'community_id':notice.community_id,'building_id':notice.building_id,'title':title,'content':content}
                elif command=='notice.batch_publish':
                    params={'community_ids':[int(row['id']) for row in communities],'title':title,'content':content}
                else:
                    if len(matches)!=1:return None
                    cid=int(matches[0]['id'])
                    bid=None
                    if values.get('notice_scope')=='building' or values.get('building_name'):
                        building_name=values.get('building_name')
                        names=building_name_variants(building_name)
                        buildings=list(g.db.scalars(policy.query(Building).where(Building.community_id==cid,Building.name.in_(names)).limit(2)))
                        if len(buildings)!=1:return None
                        bid=buildings[0].id
                    params={'community_id':cid,'building_id':bid,'title':title,'content':content}
            elif command=='order.create':
                house=one_house()
                if not house:return None
                content=message.split('，',1)[-1].strip() or message.strip()
                params={'house_id':house.id,'community_id':house.community_id,'building_id':house.building_id,'title':content[:100],'content':content,'type':'其他'}
            elif command=='vehicle.save':
                person=one_person(values.get('person_name'),values.get('phone'))
                house=one_house()
                if not person or not house or not values.get('plate'):return None
                params={'house_id':house.id,'person_id':person.id,'plate':values['plate'],'model':''}
            elif command=='device.save':
                building_name=values.get('building_name')
                code=values.get('space_code')
                building=g.db.scalar(policy.query(Building).where(Building.name==building_name)) if building_name else None
                if not building or not code:return None
                name='水泵' if '水泵' in message else message.split('，',1)[0][:100]
                params={'community_id':building.community_id,'building_id':building.id,'code':code,'name':name,'category':'equipment','location':'','status':'normal'}
            elif command=='parking.assign':
                space_code=values.get('space_code');plate=values.get('plate')
                if not space_code or not plate:return None
                space=g.db.scalar(policy.query(ParkingSpace).where(ParkingSpace.code==space_code))
                vehicle=g.db.scalar(policy.query(Vehicle).where(Vehicle.plate==plate))
                if not space or not vehicle:return None
                params={'space_id':space.id,'vehicle_id':vehicle.id}
            elif command in {'order.assign','order.accept','order.progress','order.finish','order.reopen','order.close'}:
                order=one_order()
                if not order:return None
                params={'id':order.id,'version':order.version}
                if command=='order.assign':
                    person=one_person(values.get('repairer_name'))
                    repairer_id=person.user_id if person else None
                    if not repairer_id:return None
                    params['repairer_id']=repairer_id
                elif command in {'order.progress','order.finish','order.reopen','order.close'}:
                    params['remark']=message.strip()
            elif command=='complaint.resolve':
                complaint=g.db.scalar(policy.query(Complaint).order_by(Complaint.updated_at.desc()))
                if not complaint:return None
                params={'id':complaint.id,'version':complaint.version,'resolution':message.split('，',1)[-1].strip()}
            elif command=='bill.batch':
                building=one_house()
                if not building:return None
                fee=g.db.scalar(policy.query(FeeItem).limit(1))
                if not fee:return None
                month=(utcnow()+timedelta(hours=8)).strftime('%Y-%m')
                params={'building_id':building.building_id,'fee_item_id':fee.id,'period':month,'due_date':(utcnow()+timedelta(days=30)).date().isoformat()}
            elif command=='payment.record':
                bill_id=values.get('bill_id') or values.get('id')
                if not bill_id or 'amount' not in values:return None
                bill=g.db.get(Bill,int(bill_id))
                if not bill:return None
                params={'bill_id':bill.id,'version':bill.version,'amount':values['amount'],'channel':'cash','reference':''}
            elif command=='payment.reverse':
                payment_id=values.get('id')
                if not payment_id:return None
                from models import Payment
                payment=g.db.get(Payment,int(payment_id))
                if not payment:return None
                params={'id':payment.id,'version':payment.version,'reason':message.strip()}
            else:
                return None
            return {'operation':operation,'command':command,'arguments_json':json.dumps(params,ensure_ascii=False)}
        planner_hint['tool_call']=planner_tool_call()
        def bailian_tool(args):
            try:
                grant,actor=grant_actor(g.db,token if is_bailian else args.get('request_token'))
                operation=args.get('operation')
                if operation=='context':return {'ok':True,'code':'SUCCESS','data':ai_context()[0],'terminal':True}
                if planner_hint.get('action') in {'CLARIFY','DISAMBIGUATE','DENY'} and planner_hint.get('entity_status') != 'REPEAT' and operation in {'execute','propose'} and planner_hint.get('intent') != 'relation.bind_by_name':
                    code={'CLARIFY':'MISSING_PARAMETER','DISAMBIGUATE':'AMBIGUOUS_ENTITY','DENY':'PERMISSION_DENIED'}[planner_hint['action']]
                    return {'ok':False,'code':code,'message':'请先完成必要的澄清、身份确认或权限校验。','terminal':False}
                raw=args.get('arguments_json','{}')
                if not isinstance(raw,str) or len(raw)>10000:return {'error':'arguments_json无效'}
                params=json.loads(raw)
                if operation=='lookup':return structured_result(domain_query(g.db,actor,args.get('command'),params),'lookup')
                if operation not in {'execute','propose'}:return {'error':'工具操作类型无效'}
                command=args.get('command')
                if command in mutation_commands:return {'error':'本轮已处理该业务命令，请先查看结果再继续'}
                if planner_hint.get('entity_status') == 'REPEAT' and command == 'order.create':
                    existing=g.db.scalar(Policy(g.db,g.user).query(WorkOrder).order_by(WorkOrder.updated_at.desc()))
                    if existing:
                        mutation_commands.add(command)
                        return {'ok':True,'code':'ALREADY_EXECUTED','message':'已有报修记录，本轮未重复提交。','data':{'id':existing.id},'terminal':True}
                from agent_tools import perform
                item=perform(g.db,actor,grant,command,params) if operation=='execute' else propose(g.db,actor,grant,command,params)
                result_view=action_view(item,model_safe=True)
                if result_view.get('status') in {'executed','pending'}:mutation_commands.add(command)
                g.db.commit();return structured_result(result_view,operation)
            except (HTTPException,ValueError,TypeError,KeyError) as exc:
                g.db.rollback();message=getattr(exc,'description',str(exc))[:300]
                return {'ok':False,'error':message,'message':message,'code':error_code_for(getattr(exc,'code',400) or 400,message),'terminal':False}
        if payload.get('stream') is True:
            stream_db=factory()
            def stream_result():
                nonlocal conversation
                g.db=stream_db;g.user=stream_db.get(User,uid)
                if conversation:conversation=stream_db.get(AiConversation,conversation.id)
                failure=None;result=None
                if not ai_slots.acquire(blocking=False):
                    grant=g.db.get(AiGrant,gid);grant.expires_at=utcnow();g.db.commit()
                    yield 'data: '+json.dumps({'type':'error','error':'AI正在处理其他任务，请稍后再试'},ensure_ascii=False)+'\n\n';return
                try:
                    if isinstance(app.extensions['dify'],OpenAICompatibleAgentClient):
                        command_token=_TOOL_COMMANDS.set(authorized)
                        planner_token=_PLANNER_HINT.set(planner_hint)
                        try:
                            for event in app.extensions['dify'].chat_stream(prompt,f'property:{uid}:v{auth}',upstream,bailian_tool,system_prompt=system_instruction):
                                if event['type']=='delta':
                                    yield 'data: '+json.dumps({'type':'delta','content':redact_provider_text(event['content'],token)},ensure_ascii=False)+'\n\n'
                                elif event['type']=='done':result=event
                        finally:
                            _PLANNER_HINT.reset(planner_token)
                            _TOOL_COMMANDS.reset(command_token)
                    else:
                        result=app.extensions['dify'].chat(prompt,f'property:{uid}:v{auth}',upstream)
                        yield 'data: '+json.dumps({'type':'delta','content':redact_provider_text(result['answer'],token)},ensure_ascii=False)+'\n\n'
                except DifyUnavailable as exc:failure=exc
                finally:ai_slots.release()
                g.db.rollback();g.db.expire_all();g.user=g.db.get(User,uid)
                grant=g.db.get(AiGrant,gid)
                if grant:grant.expires_at=utcnow()
                g.db.commit()
                if not g.user or not g.user.active or g.user.auth_version!=auth:
                    yield 'data: '+json.dumps({'type':'error','error':'账号权限已变化，请重新登录'},ensure_ascii=False)+'\n\n';return
                current,current_digest=ai_context()
                own_execution=g.db.scalar(select(AiAction.id).where(AiAction.grant_id==gid,AiAction.status=='executed').limit(1))
                if current_digest!=digest and not own_execution:
                    yield 'data: '+json.dumps({'type':'error','error':'业务数据在回答期间发生变化，请重新提问以获取当前信息'},ensure_ascii=False)+'\n\n';return
                if failure:
                    yield 'data: '+json.dumps({'type':'error','error':str(failure),'code':failure.code},ensure_ascii=False)+'\n\n';return
                if not result:
                    yield 'data: '+json.dumps({'type':'error','error':'百炼未返回有效文本','code':'bad_response'},ensure_ascii=False)+'\n\n';return
                if not conversation:conversation=AiConversation(id=str(uuid.uuid4()),user_id=uid);g.db.add(conversation)
                conversation.upstream_id=result['conversation_id'];conversation.scope_hash=digest;conversation.updated_at=utcnow()
                remembered=dict(planner_context)
                values=planner_hint.get('arguments') or {}
                if values.get('person_name'):
                    rows=list(g.db.scalars(Policy(g.db,g.user).query(Person).where(Person.name==values['person_name']).limit(2)))
                    if len(rows)==1:remembered['resolved_person']={'id':rows[0].id,'name':rows[0].name}
                if values.get('building_name') and values.get('room_no') is not None:
                    q=Policy(g.db,g.user).query(House).where(House.building_name==values['building_name'],House.room_no==values['room_no'])
                    rows=list(g.db.scalars(q.limit(2)))
                    if len(rows)==1:remembered['resolved_house']={'id':rows[0].id,'building_name':rows[0].building_name,'room_no':rows[0].room_no}
                if planner_hint.get('intent','').startswith('order.'):
                    row=g.db.scalar(Policy(g.db,g.user).query(WorkOrder).order_by(WorkOrder.updated_at.desc()))
                    if row:remembered['resolved_order']={'id':row.id,'order_no':row.order_no}
                if planner_hint.get('intent','').startswith('notice.'):
                    row=g.db.scalar(Policy(g.db,g.user).query(Notice).order_by(Notice.updated_at.desc()))
                    if row:remembered['resolved_notice']={'id':row.id,'title':row.title,'community_id':row.community_id,'building_id':row.building_id}
                app.extensions['agent_contexts'][(uid,conversation.id)]=remembered
                actions=[action_view(x) for x in g.db.scalars(select(AiAction).where(AiAction.grant_id==gid).order_by(AiAction.created_at))]
                g.db.commit()
                yield 'data: '+json.dumps({'type':'done','conversation_id':conversation.id,'source':getattr(app.extensions['dify'],'provider','dify'),'scope':context['scope'],'actions':actions},ensure_ascii=False)+'\n\n'
            return Response(stream_with_context(stream_result()),mimetype='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
        failure=None;result=None
        if not ai_slots.acquire(blocking=False):
            grant=g.db.get(AiGrant,gid);grant.expires_at=utcnow();g.db.commit();return jsonify(error='AI正在处理其他任务，请稍后再试'),429
        try:
            try:
                if isinstance(app.extensions['dify'],OpenAICompatibleAgentClient):
                    command_token=_TOOL_COMMANDS.set(authorized)
                    planner_token=_PLANNER_HINT.set(planner_hint)
                    try:result=app.extensions['dify'].chat(prompt,f'property:{uid}:v{auth}',upstream,bailian_tool,system_prompt=system_instruction)
                    finally:
                        _PLANNER_HINT.reset(planner_token)
                        _TOOL_COMMANDS.reset(command_token)
                else:result=app.extensions['dify'].chat(prompt,f'property:{uid}:v{auth}',upstream)
            except DifyUnavailable as exc:failure=exc
        finally:ai_slots.release()
        # End the old read snapshot, then recheck identity/scope after the upstream wait.
        g.db.rollback();g.db.expire_all();g.user=g.db.get(User,uid)
        grant=g.db.get(AiGrant,gid)
        if grant:grant.expires_at=utcnow()
        g.db.commit()
        if not g.user or not g.user.active or g.user.auth_version!=auth:return jsonify(error='账号权限已变化，请重新登录'),401
        current,current_digest=ai_context()
        own_execution=g.db.scalar(select(AiAction.id).where(AiAction.grant_id==gid,AiAction.status=='executed').limit(1))
        if current_digest!=digest and not own_execution:return jsonify(error='业务数据在回答期间发生变化，请重新提问以获取当前信息'),409
        if failure:return jsonify(error=str(failure),code=failure.code),503
        if not conversation:conversation=AiConversation(id=str(uuid.uuid4()),user_id=uid);g.db.add(conversation)
        conversation.upstream_id=result['conversation_id'];conversation.scope_hash=digest;conversation.updated_at=utcnow()
        remembered=dict(planner_context)
        values=planner_hint.get('arguments') or {}
        if values.get('person_name'):
            rows=list(g.db.scalars(Policy(g.db,g.user).query(Person).where(Person.name==values['person_name']).limit(2)))
            if len(rows)==1:remembered['resolved_person']={'id':rows[0].id,'name':rows[0].name}
        if values.get('building_name') and values.get('room_no') is not None:
            q=Policy(g.db,g.user).query(House).where(House.building_name==values['building_name'],House.room_no==values['room_no'])
            rows=list(g.db.scalars(q.limit(2)))
            if len(rows)==1:remembered['resolved_house']={'id':rows[0].id,'building_name':rows[0].building_name,'room_no':rows[0].room_no}
        if planner_hint.get('intent','').startswith('order.'):
            row=g.db.scalar(Policy(g.db,g.user).query(WorkOrder).order_by(WorkOrder.updated_at.desc()))
            if row:remembered['resolved_order']={'id':row.id,'order_no':row.order_no}
        if planner_hint.get('intent','').startswith('notice.'):
            row=g.db.scalar(Policy(g.db,g.user).query(Notice).order_by(Notice.updated_at.desc()))
            if row:remembered['resolved_notice']={'id':row.id,'title':row.title,'community_id':row.community_id,'building_id':row.building_id}
        app.extensions['agent_contexts'][(uid,conversation.id)]=remembered
        return jsonify(answer=redact_provider_text(result['answer'],token),conversation_id=conversation.id,source=getattr(app.extensions['dify'],'provider','dify'),scope=context['scope'],
                       actions=[action_view(x) for x in g.db.scalars(select(AiAction).where(AiAction.grant_id==gid).order_by(AiAction.created_at))])

    @app.post('/api/agent/tools')
    def agent_tool():
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict):abort(400)
        grant,actor=grant_actor(g.db,payload.get('request_token'));g.user=actor
        operation=payload.get('operation')
        if operation=='context':return jsonify(ai_context()[0])
        if operation not in {'lookup','execute','propose'}:abort(400,description='工具支持context/lookup/execute/propose；高风险确认须由登录用户操作')
        args=payload.get('arguments_json','{}')
        if not isinstance(args,str) or len(args)>10000:abort(400)
        try:params=json.loads(args)
        except ValueError:abort(400,description='arguments_json不是有效JSON')
        if operation=='lookup':return jsonify(domain_query(g.db,actor,payload.get('command'),params))
        from agent_tools import perform
        item=perform(g.db,actor,grant,payload.get('command'),params) if operation=='execute' else propose(g.db,actor,grant,payload.get('command'),params)
        return jsonify(action_view(item,model_safe=True))

    @app.get('/ai/actions')
    @login_required
    def ai_actions():
        rows=g.db.scalars(select(AiAction).where(AiAction.user_id==g.user.id,AiAction.auth_version==g.user.auth_version,AiAction.status.in_(['pending','executed']),AiAction.expires_at>utcnow()).order_by(AiAction.created_at.desc()).limit(20))
        return jsonify(actions=[action_view(x) for x in rows])

    @app.post('/ai/actions/<action_id>/confirm')
    @login_required
    def ai_action_confirm(action_id):return jsonify(confirm(g.db,g.user,action_id))

    @app.post('/ai/actions/<action_id>/cancel')
    @login_required
    def ai_action_cancel(action_id):
        item=g.db.scalar(select(AiAction).where(AiAction.id==action_id).with_for_update())
        if not item or item.user_id!=g.user.id:abort(403)
        if item.status!='pending':abort(409)
        item.status='cancelled';audit('ai_cancel',item.id,item.command)
        return jsonify(action_view(item))

    return app

if __name__=='__main__':
    create_app().run(host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','5000')),debug=False)
