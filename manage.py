"""Deployment CLI. Never prints secrets or overwrites existing admin credentials."""
import argparse
from datetime import timedelta
from getpass import getpass
import json
import os
from pathlib import Path
import re
import sys
from dotenv import load_dotenv
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash
from database import initialize, make_engine, missing_schema, upgrade
from dify_client import DifyUnavailable, client_from_env
from models import AiAction, AiConversation, AiGrant, BusinessRequest, ConversationMessage, ConversationState, User, utcnow

load_dotenv(Path(__file__).resolve().parent/'.env')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init-db');sub.add_parser('upgrade-db')
    admin=sub.add_parser('create-admin');admin.add_argument('--username',required=True)
    diag=sub.add_parser('diagnose');diag.add_argument('--ai',action='store_true');diag.add_argument('--infer',action='store_true')
    cleanup=sub.add_parser('cleanup-agent-state');cleanup.add_argument('--dry-run',action='store_true');cleanup.add_argument('--conversation-days',type=int,default=90);cleanup.add_argument('--request-days',type=int,default=30)
    args=parser.parse_args()
    try:
        url=os.getenv('DATABASE_URL','')
        if not url:raise ValueError('请配置DATABASE_URL')
        engine=make_engine(url)
        if args.command=='init-db':initialize(engine);print('数据库初始化完成')
        elif args.command=='upgrade-db':
            for item in upgrade(engine):print(item)
            print('数据库升级完成；请核对原有数据与维修账号')
        elif args.command=='create-admin':
            if missing_schema(engine):raise ValueError('数据库尚未完成初始化或升级')
            if not re.fullmatch(r'[\w.-]{3,50}',args.username):raise ValueError('用户名格式无效')
            pw=getpass('管理员密码（至少8位，不回显）：')
            if not 8<=len(pw)<=128 or pw!=getpass('再次输入密码：'):raise ValueError('密码长度不符合要求或两次不一致')
            with Session(engine) as db:
                if db.scalar(select(User).where(User.username==args.username)):raise ValueError('用户名已存在，不覆盖账号')
                db.add(User(username=args.username,password_hash=generate_password_hash(pw),role=0,real_name='物业管理员',active=True));db.commit()
            print('管理员已创建')
        elif args.command=='cleanup-agent-state':
            now=utcnow()
            grant_cutoff=now-timedelta(days=7)
            conversation_cutoff=now-timedelta(days=max(1,args.conversation_days))
            request_cutoff=now-timedelta(days=max(1,args.request_days))
            with Session(engine) as db:
                counts={
                    'actions': db.scalar(select(func.count()).select_from(AiAction).where(AiAction.expires_at < now)),
                    'grants': db.scalar(select(func.count()).select_from(AiGrant).where(AiGrant.created_at < grant_cutoff)),
                    'conversations': db.scalar(select(func.count()).select_from(AiConversation).where(AiConversation.updated_at < conversation_cutoff)),
                    'business_requests': db.scalar(select(func.count()).select_from(BusinessRequest).where(BusinessRequest.created_at < request_cutoff)),
                }
                if not args.dry_run:
                    db.execute(delete(AiAction).where(AiAction.expires_at < now))
                    db.execute(delete(AiGrant).where(AiGrant.created_at < grant_cutoff))
                    old_ids=select(AiConversation.id).where(AiConversation.updated_at < conversation_cutoff)
                    db.execute(delete(ConversationMessage).where(ConversationMessage.conversation_id.in_(old_ids)))
                    db.execute(delete(ConversationState).where(ConversationState.conversation_id.in_(old_ids)))
                    db.execute(delete(AiConversation).where(AiConversation.id.in_(old_ids)))
                    db.execute(delete(BusinessRequest).where(BusinessRequest.created_at < request_cutoff))
                    db.commit()
            print(json.dumps({'dry_run':args.dry_run,'deleted':counts},ensure_ascii=False))
        else:
            missing=missing_schema(engine);result={'database':'ok','schema':'ok' if not missing else 'upgrade_required','missing':missing}
            if args.ai or args.infer:
                client=client_from_env()
                try:result['ai']=client.check(infer=args.infer)
                except DifyUnavailable as exc:
                    result['ai']={'status':'error','provider':getattr(client,'provider','dify'),'model':getattr(client,'model',None),
                                  'configured':bool(getattr(client,'configured',False)),'inference':'error','tool_call':'not_checked','code':exc.code,'message':str(exc)}
            print(json.dumps(result,ensure_ascii=False,indent=2))
            if missing or result.get('ai',{}).get('status')=='error':return 1
        return 0
    except ValueError as exc:print(str(exc),file=sys.stderr);return 1
    except Exception as exc:
        print('操作失败（'+type(exc).__name__+'）；请检查数据库服务、账号权限和备份，敏感连接信息未输出。',file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())
