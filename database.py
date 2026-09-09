"""Explicit schema upgrade, preserving original records and normalized ownership."""
import os
from sqlalchemy import create_engine,event,inspect,text,select
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn,CreateTable,AddConstraint
from sqlalchemy import CheckConstraint,UniqueConstraint
from models import Base,utcnow,SchemaMigration,House,User,WorkOrder,SystemSetting
from bootstrap import seed_catalog,assign_legacy,normalize_house
REVISION='property_v2'
CONTRACT_REVISION='property_v2_001'

def make_engine(url):
    kwargs={'pool_pre_ping':True}
    if url.endswith(':memory:'):kwargs.update(poolclass=StaticPool,connect_args={'check_same_thread':False})
    engine=create_engine(url,**kwargs)
    if engine.dialect.name=='sqlite':
        @event.listens_for(engine,'connect')
        def configure(conn,record):conn.execute('PRAGMA foreign_keys=ON');conn.execute('PRAGMA busy_timeout=5000')
    return engine

def _type_signature(value):
    typ=getattr(value,'type',value)
    visit=getattr(typ,'__visit_name__',typ.__class__.__name__.lower()).lower()
    # MySQL reflects Boolean columns as TINYINT(1); keep the contract semantic.
    if visit=='tinyint' and getattr(typ,'display_width',None)==1:visit='boolean'
    visit={'varchar':'string','char':'string','nvarchar':'string','bigint':'integer','smallint':'integer','tinyint':'integer','numeric':'decimal'}.get(visit,visit)
    return (visit,getattr(typ,'length',None),getattr(typ,'precision',None),getattr(typ,'scale',None))

def schema_contract_drift(engine):
    """Return catalog contract mismatches instead of treating columns-only as healthy."""
    inspector=inspect(engine);existing=set(inspector.get_table_names());drift=[]
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:continue
        actual={item['name']:item for item in inspector.get_columns(table.name)}
        for column in table.columns:
            item=actual.get(column.name)
            if not item:continue
            if _type_signature(item['type'])!=_type_signature(column.type):drift.append(f'{table.name}.{column.name}:type')
            if bool(item.get('nullable',True))!=bool(column.nullable):drift.append(f'{table.name}.{column.name}:nullable')
        actual_unique={item.get('name') for item in inspector.get_unique_constraints(table.name) if item.get('name')}
        expected_unique={item.name for item in table.constraints if isinstance(item,UniqueConstraint) and item.name}
        drift.extend(f'{table.name}:unique:{name}' for name in sorted(expected_unique-actual_unique))
        actual_indexes={item.get('name') for item in inspector.get_indexes(table.name) if item.get('name')}
        expected_indexes={item.name for item in table.indexes if item.name}
        drift.extend(f'{table.name}:index:{name}' for name in sorted(expected_indexes-actual_indexes))
        actual_checks={item.get('name') for item in inspector.get_check_constraints(table.name) if item.get('name')}
        expected_checks={item.name for item in table.constraints if isinstance(item,CheckConstraint) and item.name}
        drift.extend(f'{table.name}:check:{name}' for name in sorted(expected_checks-actual_checks))
        actual_fks={(tuple(item.get('constrained_columns') or ()),item.get('referred_table'),tuple(item.get('referred_columns') or ())) for item in inspector.get_foreign_keys(table.name)}
        for fk in table.foreign_key_constraints:
            expected=(tuple(column.name for column in fk.columns),fk.elements[0].column.table.name,tuple(element.column.name for element in fk.elements))
            if expected not in actual_fks:drift.append(f'{table.name}:foreign_key:{"-".join(expected[0])}')
    return sorted(set(drift))

def missing_schema(engine):
    inspector=inspect(engine);existing=set(inspector.get_table_names());missing=[]
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:missing.append(table.name);continue
        cols={c['name'] for c in inspector.get_columns(table.name)}
        missing.extend(f'{table.name}.{c.name}' for c in table.columns if c.name not in cols)
    if not missing:
        missing.extend(schema_contract_drift(engine))
    if not missing:
        with engine.connect() as c:
            if not c.execute(select(SchemaMigration.revision).where(SchemaMigration.revision==CONTRACT_REVISION)).first():missing.append('schema_migration.'+CONTRACT_REVISION)
    return missing

def environment(conn,env=None):
    env=env or os.getenv('APP_ENV','production')
    if env not in {'production','dev','test'}:raise ValueError('APP_ENV只允许production/dev/test')
    found=conn.execute(select(SystemSetting.value).where(SystemSetting.key=='runtime_environment')).scalar()
    if found and found!=env:raise ValueError('数据库环境标记不匹配；生产与测试必须使用不同数据库')
    if not found:conn.execute(SystemSetting.__table__.insert().values(key='runtime_environment',value=env))

def initialize(engine):
    if inspect(engine).get_table_names():raise ValueError('数据库已有表，请使用upgrade-db；不会覆盖现有数据')
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        seed_catalog(c);environment(c)
        c.execute(SchemaMigration.__table__.insert(),[{'revision':REVISION,'applied_at':utcnow()},{'revision':CONTRACT_REVISION,'applied_at':utcnow()}])

def rebuild_sqlite(engine,table_names):
    # SQLite requires a table rebuild to add foreign keys and replace old unique keys.
    with engine.connect() as c:
        c.exec_driver_sql('PRAGMA foreign_keys=OFF');c.commit()
        try:
            with c.begin():
                for name in table_names:
                    table=Base.metadata.tables[name]
                    old={col['name'] for col in inspect(c).get_columns(name)}
                    if old-set(table.columns.keys()):raise ValueError('发现非本项目字段，停止迁移以保护数据：'+name)
                    temp='migration_'+name
                    ddl=str(CreateTable(table).compile(dialect=engine.dialect)).replace('CREATE TABLE '+name+' ', 'CREATE TABLE '+temp+' ',1)
                    quote=engine.dialect.identifier_preparer.quote
                    if ddl==str(CreateTable(table).compile(dialect=engine.dialect)):
                        ddl=ddl.replace('CREATE TABLE '+quote(name)+' ', 'CREATE TABLE '+quote(temp)+' ',1)
                    c.exec_driver_sql(ddl)
                    cols=','.join(quote(x.name) for x in table.columns)
                    c.exec_driver_sql(f'INSERT INTO {quote(temp)} ({cols}) SELECT {cols} FROM {quote(name)}')
                    c.exec_driver_sql(f'DROP TABLE {quote(name)}');c.exec_driver_sql(f'ALTER TABLE {quote(temp)} RENAME TO {quote(name)}')
                violations=c.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
                if violations:raise ValueError('存在无效外键，已回滚迁移，请核验原数据')
        finally:c.exec_driver_sql('PRAGMA foreign_keys=ON');c.commit()

def upgrade(engine):
    if not missing_schema(engine):return []
    existing=set(inspect(engine).get_table_names());changes=[]
    # New tables may refer to existing tables whose added columns are not used as keys.
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:table.create(engine);changes.append('新增表 '+table.name);continue
        old={c['name'] for c in inspect(engine).get_columns(table.name)}
        for col in table.columns:
            if col.name in old:continue
            if col.primary_key:raise ValueError('缺失主键，停止自动迁移：'+table.name)
            ddl=str(CreateColumn(col).compile(dialect=engine.dialect))
            with engine.begin() as c:c.exec_driver_sql('ALTER TABLE '+engine.dialect.identifier_preparer.quote(table.name)+' ADD COLUMN '+ddl)
            changes.append('新增列 '+table.name+'.'+col.name)
    with engine.begin() as c:
        seed_catalog(c)
        for u in c.execute(select(User.__table__)).mappings().all():assign_legacy(c,u['id'],u['role'])
        for h in c.execute(select(House.__table__)).mappings().all():normalize_house(c,h)
        for h in c.execute(select(House.id,House.community_id,House.building_id)).mappings():
            c.execute(WorkOrder.__table__.update().where(WorkOrder.house_id==h['id']).values(community_id=h['community_id'],building_id=h['building_id']))
        prior=c.execute(select(SchemaMigration.revision).where(SchemaMigration.revision.in_(['property_v1_1',REVISION]))).first()
        if not prior:
            c.execute(User.__table__.update().where(User.role==1).values(active=False,auth_version=User.auth_version+1));changes.append('旧维修账号待管理员重新核验启用')
        environment(c)
    if engine.dialect.name=='sqlite':rebuild_sqlite(engine,[n for n in existing if n in Base.metadata.tables])
    elif engine.dialect.name=='mysql':
        # MySQL DDL commits implicitly. Run under maintenance with an independently verified backup.
        with engine.begin() as c:
            for u in inspect(c).get_unique_constraints('house'):
                if set(u['column_names'])=={'building_name','unit','room_no'}:
                    c.exec_driver_sql('ALTER TABLE house DROP INDEX '+engine.dialect.identifier_preparer.quote(u['name']))
            present={tuple(u['column_names']) for u in inspect(c).get_unique_constraints('house')}
            if ('unit_id','room_no') not in present:c.exec_driver_sql('ALTER TABLE house ADD CONSTRAINT uk_house_unit_room UNIQUE (unit_id,room_no)')
            for table in Base.metadata.sorted_tables:
                for info in inspect(c).get_columns(table.name):
                    col=table.c.get(info['name'])
                    if col is not None and getattr(col.type,'__visit_name__','')=='datetime' and getattr(info['type'],'fsp',None)!=6:
                        c.exec_driver_sql('ALTER TABLE '+engine.dialect.identifier_preparer.quote(table.name)+' MODIFY COLUMN '+str(CreateColumn(col).compile(dialect=engine.dialect)))
                checks={x['name'] for x in inspect(c).get_check_constraints(table.name)}
                for constraint in table.constraints:
                    if isinstance(constraint,CheckConstraint) and constraint.name not in checks:c.execute(AddConstraint(constraint))
                foreign={tuple(f['constrained_columns']) for f in inspect(c).get_foreign_keys(table.name)}
                for fk in table.foreign_key_constraints:
                    if tuple(col.name for col in fk.columns) not in foreign:c.execute(AddConstraint(fk))
    for table in Base.metadata.sorted_tables:
        present={x['name'] for x in inspect(engine).get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in present:index.create(engine)
    with engine.begin() as c:
        if not c.execute(select(SchemaMigration.revision).where(SchemaMigration.revision==REVISION)).first():c.execute(SchemaMigration.__table__.insert().values(revision=REVISION,applied_at=utcnow()))
        if not c.execute(select(SchemaMigration.revision).where(SchemaMigration.revision==CONTRACT_REVISION)).first():c.execute(SchemaMigration.__table__.insert().values(revision=CONTRACT_REVISION,applied_at=utcnow()))
    return changes
