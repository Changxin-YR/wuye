"""Server-side Agent capability gateway.

The LLM is a planner only.  Every tool call resolves the current persisted IAM
state, applies RBAC + row scope, evaluates risk, executes the same business
service used by the UI, verifies the flushed result, and records an audit event.
A short-lived grant can never confirm a CONFIRM operation; confirmation belongs
to the authenticated browser session and is revalidated at execution time.
"""
import hashlib
import json
import uuid
from datetime import timedelta

from flask import abort
from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from agent_security import (
    authorization_fingerprint,
    execution_mode,
    requires_confirmation,
    risk_for,
    safe_record,
    validate_agent_token_fingerprint,
)
from business import BusinessService
from models import AiAction, AiGrant, AuditLog, House, Notice, User, WorkOrder, utcnow
from permissions import Policy
from property_service import COMMANDS as DOMAIN_COMMANDS, PropertyService, encode, snapshot
from services import ROLE_TEXT, STATUS_TEXT, can_access_order

# Legacy compatibility commands. New Agent discovery exposes PropertyService
# commands, but older deployed Dify applications can continue to use these.
# Each legacy command is still permission/scope checked on the server.
COMMANDS = {
    'order.create': ({2}, 'create_order', '', '提交报修', 'title content type house_id location contact_name contact_phone'),
    'order.assign': ({0}, 'order_action', 'assign', '派单', 'order_no version repairer_id'),
    'order.reassign': ({0}, 'order_action', 'reassign', '改派维修人员', 'order_no version repairer_id'),
    'order.accept': ({1}, 'order_action', 'accept', '接受工单', 'order_no version'),
    'order.progress': ({1}, 'order_action', 'progress', '记录维修进度', 'order_no version remark'),
    'order.finish': ({1}, 'order_action', 'finish', '提交完工待验收', 'order_no version remark'),
    'order.cancel': ({0, 2}, 'order_action', 'cancel', '取消工单', 'order_no version remark'),
    'order.close': ({2}, 'order_action', 'close', '确认验收关闭', 'order_no version'),
    'order.reopen': ({2}, 'order_action', 'reopen', '要求返修', 'order_no version remark'),
    'order.evaluate': ({2}, 'order_action', 'evaluate', '评价维修', 'order_no version score comment'),
    'house.add': ({0}, 'house_action', 'add', '登记房屋', 'building_name unit room_no'),
    'house.bind': ({0}, 'house_action', 'bind', '绑定房屋业主', 'house_id version owner_id'),
    'house.unbind': ({0}, 'house_action', 'unbind', '解除房屋绑定', 'house_id version'),
    'house.delete': ({0}, 'house_action', 'delete', '删除空置房屋记录', 'house_id version'),
    'notice.add': ({0}, 'notice_action', 'add', '发布公告', 'title content'),
    'notice.edit': ({0}, 'notice_action', 'edit', '修改公告', 'id version title content'),
    'notice.delete': ({0}, 'notice_action', 'delete', '删除公告', 'id version'),
    'user.disable': ({0}, 'user_action', 'disable', '停用账号', 'user_id auth_version'),
    'user.enable': ({0}, 'user_action', 'enable', '恢复账号', 'user_id auth_version'),
}
LABELS = {
    'order_no': '工单号', 'version': '记录版本', 'repairer_id': '维修人员ID', 'house_id': '房屋ID',
    'owner_id': '业主ID', 'user_id': '用户ID', 'auth_version': '账号版本', 'id': '公告ID', 'title': '标题',
    'content': '内容', 'type': '分类', 'location': '具体位置', 'contact_name': '联系人', 'contact_phone': '联系电话',
    'remark': '处理说明/原因', 'score': '评分', 'comment': '评价', 'building_name': '楼栋', 'unit': '单元', 'room_no': '房号',
}
HIGH_IMPACT = {'house.unbind', 'house.delete', 'notice.delete', 'user.disable', 'user.enable'}
AGENT_EXCLUDED = {'staff.create', 'role.save', 'relation.bind'}
CAPABILITY_CATALOG = {
    'relation.bind_by_name': {
        'when_to_use': '将唯一明确的人员绑定到唯一明确的房屋',
        'when_not_to_use': '房屋或人员缺少关键字段、人员重名或关系已存在时不要调用',
        'required_parameters': ['building_name', 'room_no', 'person_name'],
        'entity_requirements': ['house:RESOLVED', 'person:RESOLVED'],
        'ambiguity_policy': '多个结果必须返回 AMBIGUOUS_ENTITY 并要求消歧',
        'examples': ['把23栋311绑定王五'],
        'negative_examples': ['给23栋绑定业主'],
    },
    'order.create': {
        'when_to_use': '用户明确要求新建报修或维修工单',
        'when_not_to_use': '用户只查询、重复提交或未明确房屋时不要猜测创建',
        'required_parameters': ['title', 'content', 'type'],
        'entity_requirements': ['house:RESOLVED for private repair'],
        'ambiguity_policy': '房屋不唯一时要求补充楼栋、单元和房号',
        'examples': ['登记厨房漏水报修'],
        'negative_examples': ['再提交一次刚才的报修'],
    },
    'order.assign': {
        'when_to_use': '给明确工单分配明确维修人员',
        'when_not_to_use': '缺少工单 id/version 或维修人未解析时不要调用',
        'required_parameters': ['id', 'version', 'repairer_id'],
        'entity_requirements': ['order:RESOLVED', 'repairer:RESOLVED'],
        'ambiguity_policy': '查询结果多于一个时要求消歧',
        'examples': ['把指定工单派给张三'],
        'negative_examples': ['把工单派给某个人'],
    },
    'inspection.complete': {
        'when_to_use': '提交当前登录人员负责的巡检结果',
        'when_not_to_use': '只有设备编号但没有巡检记录 id/version 时不要猜测',
        'required_parameters': ['id', 'version', 'findings'],
        'entity_requirements': ['inspection:RESOLVED'],
        'ambiguity_policy': '设备对应多条巡检时必须要求选择',
        'examples': ['提交 P-01 巡检结果'],
        'negative_examples': ['把设备直接标记为完成巡检'],
    },
    'notice.save': {
        'when_to_use': '发布一个小区或楼栋公告',
        'when_not_to_use': '没有明确发布范围且当前用户拥有多个小区时禁止猜测；不得创建小区或楼栋',
        'required_parameters': ['title', 'content', 'community_id'],
        'optional_parameters': ['building_id'],
        'scope_rules': 'building_id=null 表示整个小区；building_id!=null 表示指定楼栋',
        'examples': ['发布全小区停水公告', '给3栋发布电梯检修公告'],
        'negative_examples': ['发布公告（多小区时先澄清）'],
    },
    'notice.batch_publish': {
        'when_to_use': '用户明确要求向当前负责的所有小区发布同一条公告',
        'when_not_to_use': '用户未明确“所有负责小区”或没有可写小区时不要调用',
        'required_parameters': ['community_ids', 'title', 'content'],
        'optional_parameters': [],
        'scope_rules': '服务端重新核验每个小区的 notice.write 和 DataScope，事务中全部成功或全部回滚',
        'examples': ['给我负责的所有小区发布公告'],
        'negative_examples': ['发布公告（范围不明确）'],
    },
}
LEGACY_PERMISSION = {
    'house.add': 'property.write', 'house.bind': 'relation.write', 'house.unbind': 'relation.end',
    'house.delete': 'property.write', 'notice.add': 'notice.write', 'notice.edit': 'notice.write',
    'notice.delete': 'notice.write', 'user.disable': 'staff.manage', 'user.enable': 'staff.manage',
}


def agent_request_key(user_id, conversation_id, request_id, command):
    """Create a bounded, deterministic domain idempotency key."""
    raw = f'{user_id}:{conversation_id or "new"}:{request_id}:{command}'
    return 'agent:' + str(user_id) + ':' + hashlib.sha256(raw.encode()).hexdigest()[:64]


def _domain_meta(command):
    permission, high_impact, parameters = DOMAIN_COMMANDS[command]
    risk = risk_for(command, high_impact=high_impact)
    return permission, high_impact, parameters, risk, execution_mode(command, high_impact=high_impact)


def available_commands(actor):
    """Capability discovery is derived from current persisted permissions."""
    p = Policy(object_session(actor), actor)
    from management_ui import ACTION_NAMES
    result = []
    for command, values in DOMAIN_COMMANDS.items():
        if command in AGENT_EXCLUDED or not p.has(values[0]):
            continue
        permission, high_impact, parameters, risk, mode = _domain_meta(command)
        item = {
            'command': command,
            'name': ACTION_NAMES.get(command, command),
            'permission': permission,
            'parameters': parameters,
            'risk_level': risk,
            'execution_mode': mode,
            'requires_confirmation': mode == 'CONFIRM',
        }
        item.update(CAPABILITY_CATALOG.get(command, {
            'when_to_use': ACTION_NAMES.get(command, command),
            'when_not_to_use': '缺少必要参数或实体不明确时不要调用',
            'required_parameters': [x for x in parameters.split() if x in {'id', 'version'}],
            'entity_requirements': [],
            'ambiguity_policy': '多个结果必须要求用户消歧',
            'examples': [],
            'negative_examples': [],
        }))
        result.append(item)
    return result


def grant_actor(db, token):
    if not isinstance(token, str) or not 32 <= len(token) <= 100:
        abort(401)
    grant = db.scalar(select(AiGrant).where(
        AiGrant.token_hash == hashlib.sha256(token.encode()).hexdigest()
    ).with_for_update())
    if not grant or grant.expires_at <= utcnow():
        abort(401, description='Agent本次授权已过期')
    actor = db.get(User, grant.user_id)
    if not actor or not actor.active or actor.auth_version != grant.auth_version:
        abort(403, description='当前账号授权已变化')
    policy = Policy(db, actor)
    if not validate_agent_token_fingerprint(token, policy):
        abort(403, description='当前角色或数据范围已变化，请重新发起Agent请求')
    return grant, actor


def normalize(actor, command, params):
    if not isinstance(command, str) or command not in set(COMMANDS) | set(DOMAIN_COMMANDS) or command in AGENT_EXCLUDED:
        abort(400, description='未开放的Agent操作')
    p = Policy(object_session(actor), actor)
    permission = DOMAIN_COMMANDS[command][0] if command in DOMAIN_COMMANDS else LEGACY_PERMISSION.get(command)
    if not permission:
        # Legacy order permissions are resolved by BusinessService/PropertyService;
        # exposing one without a known permission mapping would be unsafe.
        permission = {
            'order.create': 'order.create', 'order.assign': 'order.dispatch', 'order.reassign': 'order.dispatch',
            'order.accept': 'order.work', 'order.progress': 'order.work', 'order.finish': 'order.work',
            'order.cancel': 'order.cancel', 'order.close': 'order.verify', 'order.reopen': 'order.verify',
            'order.evaluate': 'order.verify',
        }.get(command)
    if not permission:
        abort(400, description='未配置Agent权限映射')
    p.require(permission)
    allowed = set(DOMAIN_COMMANDS[command][2].split()) if command in DOMAIN_COMMANDS else set()
    if command in COMMANDS:
        allowed.update(COMMANDS[command][4].split())
    if not isinstance(params, dict) or set(params) - allowed:
        abort(400, description='操作包含未允许的参数')
    if any(v is not None and not isinstance(v, (str, int, bool, list)) for v in params.values()):
        abort(400, description='操作参数类型错误')
    required = {
        'relation.bind_by_name': ('room_no', 'person_name'),
    }.get(command, ())
    missing = [key for key in required if key not in params or params[key] in (None, '')]
    if missing:
        abort(400, description='缺少必要参数：' + '、'.join(missing))
    if len(json.dumps(params, ensure_ascii=False)) > 10000:
        abort(400, description='操作参数过长')
    return params


def _verification(db, actor, command, params, result):
    """Read the flushed row back before reporting success to the model."""
    from models import (
        Bill, Building, Community, Complaint, Device, FeeItem, HousePerson, Inspection,
        Lease, ParkingSpace, ParkingUse, Payment, Person, PropertyUnit, Vehicle,
    )
    model_by_root = {
        'community': Community, 'building': Building, 'unit': PropertyUnit, 'house': House,
        'person': Person, 'relation': HousePerson, 'lease': Lease, 'staff': User,
        'order': WorkOrder, 'complaint': Complaint, 'notice': Notice, 'visitor': __import__('models').Visitor,
        'vehicle': Vehicle, 'parking': ParkingUse if command in {'parking.assign','parking.release'} else ParkingSpace,
        'device': Device, 'inspection': Inspection, 'fee': FeeItem, 'bill': Bill, 'payment': Payment,
    }
    rid = result.get('id') if isinstance(result, dict) else None
    if command == 'notice.batch_publish' and isinstance(result, dict):
        ids = result.get('ids') or []
        if not ids or any(not db.get(Notice, int(rid)) for rid in ids):
            abort(503, description='批量公告服务返回成功但未能回读全部公告')
        return {'status': 'verified', 'resource': 'notice', 'ids': [int(item) for item in ids]}
    if command == 'payment.record':
        rid = result.get('id')
    if rid is not None and command in DOMAIN_COMMANDS:
        cls = model_by_root.get(command.split('.')[0])
        if cls:
            obj = db.get(cls, rid)
            if not obj:
                abort(503, description='业务服务返回成功但未能回读目标记录')
            return {'status': 'verified', 'resource': obj.__tablename__, 'id': rid}
    if command.startswith('order.') and isinstance(result, dict) and result.get('url', '').startswith('/orders/'):
        no = result['url'].rsplit('/', 1)[-1]
        obj = db.scalar(Policy(db, actor).query(WorkOrder).where(WorkOrder.order_no == no))
        if not obj:
            abort(503, description='工单操作返回成功但未能回读工单')
        return {'status': 'verified', 'resource': 'work_order', 'id': obj.id, 'order_no': no}
    if params.get('house_id'):
        obj = db.scalar(Policy(db, actor).query(House).where(House.id == int(params['house_id'])))
        if obj:
            return {'status': 'verified', 'resource': 'house', 'id': obj.id}
    return {'status': 'service_validated'}


def execute(db, actor, command, params, request_key=None):
    params = normalize(actor, command, params)
    if command in DOMAIN_COMMANDS and not params.get('order_no'):
        result = PropertyService(db, actor, source='agent').run(command, params, request_key=request_key)
        if command == 'order.create':
            result['url'] = '/orders/' + result['record']['order_no']
        result['verification'] = _verification(db, actor, command, params, result)
        return result
    _, method, action, _, _ = COMMANDS[command]
    if method == 'user_action':
        checker = BusinessService(db, actor, params)
        target = db.scalar(select(User).where(User.id == checker.number('user_id')).with_for_update())
        if not target:
            abort(404)
        if target.auth_version != checker.number('auth_version'):
            abort(409, description='目标账号已变化，请重新生成操作')
    service = BusinessService(db, actor, {**params, 'action': action})
    if method == 'order_action':
        no = service.field('order_no', 64, True)
        service.order_action(no)
        result = {'message': '工单操作已保存', 'url': '/orders/' + no}
    else:
        obj = getattr(service, method)()
        if method == 'create_order':
            result = {'message': '报修已提交', 'url': '/orders/' + obj.order_no}
        else:
            result = {'message': '操作已保存', 'url': {'house_action': '/houses', 'notice_action': '/notices', 'user_action': '/users'}[method]}
    result['verification'] = _verification(db, actor, command, params, result)
    return result


def describe(db, actor, command, params):
    if command in DOMAIN_COMMANDS:
        from management_ui import ACTION_NAMES, LABELS as DOMAIN_LABELS
        _, high_impact, _, risk, mode = _domain_meta(command)
        return {
            'title': ACTION_NAMES.get(command, command),
            'high_impact': high_impact or mode == 'CONFIRM',
            'risk_level': risk,
            'execution_mode': mode,
            'current': domain_current(db, actor, command, params),
            'changes': [{'label': DOMAIN_LABELS.get(k, k), 'value': str(v)} for k, v in params.items()],
        }
    p = Policy(db, actor)
    facts = []
    if params.get('order_no'):
        order = db.scalar(select(WorkOrder).where(WorkOrder.order_no == params['order_no']))
        if not order:
            abort(404)
        if not can_access_order(actor, order):
            abort(403)
        facts.append('工单：' + order.title + '；当前状态：' + STATUS_TEXT[order.status])
    if params.get('house_id'):
        house = p.get(House, int(params['house_id']))
        facts.append(f'房屋：{house.building_name} {house.unit} {house.room_no}；当前业主ID：{house.owner_id or "未绑定"}')
    for key in ['repairer_id', 'owner_id', 'user_id']:
        if params.get(key):
            target = p.get(User, int(params[key]))
            facts.append(LABELS[key] + '：' + target.username + '（' + ROLE_TEXT.get(target.role, '账号') + '）')
    if command.startswith('notice.') and params.get('id'):
        notice = p.get(Notice, int(params['id']))
        facts.append('原公告：' + notice.title + '\n' + notice.content)
    risk = risk_for(command, high_impact=command in HIGH_IMPACT)
    mode = execution_mode(command, high_impact=command in HIGH_IMPACT)
    return {
        'title': COMMANDS[command][3],
        'high_impact': command in HIGH_IMPACT or mode == 'CONFIRM',
        'risk_level': risk,
        'execution_mode': mode,
        'current': facts,
        'changes': [{'label': LABELS[k], 'value': v} for k, v in params.items()],
    }


def _agent_event(db, actor, action, item, detail=''):
    p = Policy(db, actor)
    preview = json.loads(item.preview) if item.preview else {}
    db.add(AuditLog(
        operator_id=actor.id,
        actor_name=actor.username,
        roles=','.join(sorted(p.roles)),
        source='agent',
        resource='ai_action',
        action=action,
        target=item.id,
        detail=(detail or item.command)[:500],
        status='success',
        trace_id=item.grant_id,
    ))


def propose(db, actor, grant, command, params, request_key=None):
    params = normalize(actor, command, params)
    encoded = json.dumps(params, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256((command + encoded).encode()).hexdigest()
    existing = db.scalar(select(AiAction).where(AiAction.grant_id == grant.id, AiAction.payload_hash == digest))
    if existing:
        return existing
    if db.scalar(select(func.count(AiAction.id)).where(AiAction.grant_id == grant.id)) >= 5:
        abort(429, description='每轮最多生成5项变更，请分步处理')
    # Validate the real command against current state, then undo *all* effects,
    # including domain audit rows and notifications created during validation.
    savepoint = db.begin_nested()
    try:
        validated = execute(db, actor, command, params)
        db.flush()
    finally:
        savepoint.rollback()
    preview = describe(db, actor, command, params)
    preview['current'].append('预检结果（尚未执行）：' + validated.get('message', '校验通过'))
    item = AiAction(
        id=str(uuid.uuid4()), grant_id=grant.id, user_id=actor.id, auth_version=actor.auth_version,
        command=command, payload=encoded, payload_hash=digest, request_key=request_key or '', preview=json.dumps(preview, ensure_ascii=False),
        expires_at=utcnow() + timedelta(minutes=10),
    )
    db.add(item)
    db.flush()
    _agent_event(db, actor, 'ai_propose', item, f"{command} risk={preview.get('risk_level')}")
    return item


def action_view(item, model_safe=False):
    """Serialize an action for the authenticated UI or for an upstream model.

    The browser is already inside the user's authorized session and can render
    the exact confirmation payload. The model does not need raw phone numbers,
    receipt references or full records to continue planning, so its view is
    intentionally narrower.
    """
    preview = json.loads(item.preview)
    result = json.loads(item.result) if item.result else None
    if model_safe:
        preview = {
            'title': preview.get('title'),
            'high_impact': bool(preview.get('high_impact')),
            'risk_level': preview.get('risk_level', risk_for(item.command)),
            'execution_mode': preview.get('execution_mode'),
            'current': preview.get('current', [])[:5],
            'change_fields': [str(x.get('label', ''))[:80] for x in preview.get('changes', []) if isinstance(x, dict)],
        }
        if isinstance(result, dict):
            result = {
                key: result.get(key) for key in ('message', 'id', 'url', 'verification', 'traceId')
                if result.get(key) is not None
            }
    return {
        'id': item.id,
        'command': item.command,
        'risk_level': preview.get('risk_level', risk_for(item.command)),
        'execution_mode': preview.get('execution_mode', 'CONFIRM' if preview.get('high_impact') else 'AUTO'),
        'status': item.status,
        'preview': preview,
        'expires_at': item.expires_at.isoformat() + 'Z',
        'result': result,
    }


def structured_result(value, operation='lookup'):
    """Return a stable, model-facing tool envelope without changing business data."""
    if isinstance(value, dict) and value.get('ok') is False:
        return {**value, 'ok': False, 'code': value.get('code') or 'SYSTEM_ERROR', 'terminal': False}
    status = value.get('status') if isinstance(value, dict) else None
    if operation == 'propose' or status == 'pending':
        code, ok, terminal = 'CONFIRMATION_REQUIRED', True, False
    elif operation == 'execute':
        code, ok, terminal = 'SUCCESS', True, True
    elif operation == 'lookup':
        code, ok, terminal = 'SUCCESS', True, False
    else:
        code, ok, terminal = 'SUCCESS', True, False
    envelope = dict(value) if isinstance(value, dict) else {'value': value}
    envelope.update({
        'ok': ok,
        'code': code,
        'message': (value.get('message') if isinstance(value, dict) else None) or code,
        'data': value,
        'terminal': terminal,
    })
    return envelope


def confirm(db, actor, action_id):
    item = db.scalar(select(AiAction).where(AiAction.id == action_id).with_for_update())
    if not item or item.user_id != actor.id:
        abort(403)
    if actor.auth_version != item.auth_version:
        abort(409, description='账号授权已变化，请重新生成操作')
    if item.status == 'executed':
        return action_view(item)
    if item.status != 'pending':
        abort(409, description='操作已取消')
    if item.expires_at <= utcnow():
        abort(410, description='操作已过期，请重新生成')
    # Re-run normalize + policy + row scope + optimistic version checks now.
    result = execute(db, actor, item.command, json.loads(item.payload), request_key=item.request_key or None)
    item.status = 'executed'
    item.result = json.dumps(result, ensure_ascii=False, default=str)
    _agent_event(db, actor, 'ai_execute', item, f"{item.command} confirmed_by_browser")
    db.flush()
    return action_view(item)


def perform(db, actor, grant, command, params, request_key=None):
    """Auto-execute only R1 / explicitly safe R2; all other writes are pending."""
    params = normalize(actor, command, params)
    high = DOMAIN_COMMANDS[command][1] if command in DOMAIN_COMMANDS else command in HIGH_IMPACT
    if requires_confirmation(command, high_impact=high):
        return propose(db, actor, grant, command, params, request_key=request_key)
    encoded = json.dumps(params, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256((command + encoded).encode()).hexdigest()
    old = db.scalar(select(AiAction).where(AiAction.grant_id == grant.id, AiAction.payload_hash == digest))
    if old:
        return old
    if db.scalar(select(func.count(AiAction.id)).where(AiAction.grant_id == grant.id)) >= 5:
        abort(429, description='每轮最多执行5项，请分步处理')
    result = execute(db, actor, command, params, request_key=request_key)
    preview = describe(db, actor, command, params)
    item = AiAction(
        id=str(uuid.uuid4()), grant_id=grant.id, user_id=actor.id, auth_version=actor.auth_version,
        command=command, payload=encoded, payload_hash=digest, request_key=request_key or '', preview=json.dumps(preview, ensure_ascii=False),
        status='executed', result=json.dumps(result, ensure_ascii=False, default=str),
        expires_at=utcnow() + timedelta(minutes=10),
    )
    db.add(item)
    db.flush()
    _agent_event(db, actor, 'ai_execute', item, f"{command} auto risk={preview.get('risk_level')}")
    return item


def domain_current(db, actor, command, params):
    from models import Bill, Device, HousePerson, Lease, ParkingUse, Payment, Person
    root = command.split('.')[0]
    cls = {
        'house': House, 'person': Person, 'relation': HousePerson, 'lease': Lease, 'staff': User,
        'notice': Notice, 'parking': ParkingUse, 'payment': Payment, 'bill': Bill, 'device': Device,
    }.get(root)
    if command == 'payment.record':
        cls = Bill
    rid = params.get('id') or (params.get('bill_id') if command == 'payment.record' else None)
    if not cls or not rid:
        return []
    obj = Policy(db, actor).get(cls, int(rid))
    return ['当前已保存记录：' + encode(safe_record(snapshot(obj)))]
