from database_fixture import test_database
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import Boolean, inspect,select,text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable,CreateIndex
from sqlalchemy.dialects import mysql
from app import create_app
from database import make_engine,initialize,missing_schema,upgrade,schema_contract_drift,_type_signature
from models import Base,User,House,WorkOrder
from fixtures import legacy_models as old

class MigrationTests(unittest.TestCase):
    def test_mysql_boolean_reflection_matches_boolean_contract(self):
        class ReflectedTinyInt:
            __visit_name__='tinyint'
            display_width=1
        self.assertEqual(_type_signature(ReflectedTinyInt()),_type_signature(Boolean()))

    def setUp(self):
        self.temp=TemporaryDirectory();self.url='sqlite+pysqlite:///'+str(Path(self.temp.name)/'migration.db');self.url=test_database(self,self.url);self.engine=make_engine(self.url)
    def tearDown(self):self.engine.dispose();self.temp.cleanup()
    def legacy(self):
        old.Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            db.add_all([old.User(id=1,username='owner',password_hash='preserved-owner-hash',role=2),old.User(id=2,username='worker',password_hash='preserved-worker-hash',role=1)])
            db.flush();db.add(old.House(id=1,building_name='旧楼',unit='1',room_no=101,owner_id=1));db.flush()
            db.add(old.WorkOrder(order_no='legacy-order',owner_id=1,repairer_id=2,house_id=1,title='历史报修',content='保留原内容',status=2,img_url='legacy.jpg'));db.commit()
    def test_fresh_init_and_refuse_overwrite(self):
        initialize(self.engine);self.assertEqual(missing_schema(self.engine),[])
        with self.assertRaises(ValueError):initialize(self.engine)
    def test_legacy_upgrade_preserves_data_and_requires_worker_review(self):
        self.legacy();self.assertTrue(missing_schema(self.engine));changes=upgrade(self.engine);self.assertTrue(changes)
        self.assertEqual(missing_schema(self.engine),[])
        with Session(self.engine) as db:
            self.assertEqual(db.get(User,1).password_hash,'preserved-owner-hash');self.assertTrue(db.get(User,1).active)
            self.assertFalse(db.get(User,2).active);self.assertEqual(db.get(User,2).auth_version,2)
            self.assertEqual(db.get(House,1).owner_id,1)
            o=db.scalar(select(WorkOrder));self.assertEqual((o.title,o.content,o.status,o.img_url),('历史报修','保留原内容',2,'legacy.jpg'));self.assertEqual(o.version,1)
        self.assertIn('idx_order_owner_status',{i['name'] for i in inspect(self.engine).get_indexes('work_order')})
    def test_upgrade_is_idempotent_and_does_not_disable_verified_staff(self):
        self.legacy();upgrade(self.engine)
        with Session(self.engine) as db:db.get(User,2).active=True;db.commit()
        self.assertEqual(upgrade(self.engine),[])
        with Session(self.engine) as db:self.assertTrue(db.get(User,2).active)
    def test_unmigrated_production_start_refused(self):
        self.legacy()
        with self.assertRaisesRegex(RuntimeError,'升级'):
            create_app({'DATABASE_URL':self.url,'SECRET_KEY':'a'*40,'UPLOAD_FOLDER':self.temp.name})
    def test_migrated_production_start_works(self):
        self.legacy();upgrade(self.engine)
        self.assertEqual(missing_schema(self.engine),[], 'upgrade must satisfy the full schema contract before production startup')
        app=create_app({'DATABASE_URL':self.url,'SECRET_KEY':'a'*40,'UPLOAD_FOLDER':self.temp.name})
        self.assertEqual(app.test_client().get('/health').status_code,200);app.extensions['db_engine'].dispose()
    def test_mysql_ddl_compiles(self):
        for table in Base.metadata.sorted_tables:
            self.assertIn('CREATE TABLE',str(CreateTable(table).compile(dialect=mysql.dialect())))
            for index in table.indexes:self.assertIn('CREATE INDEX',str(CreateIndex(index).compile(dialect=mysql.dialect())))

    def test_schema_contract_detects_removed_index(self):
        initialize(self.engine)
        with self.engine.begin() as conn:
            conn.exec_driver_sql('DROP INDEX idx_order_owner_status')
        drift=schema_contract_drift(self.engine)
        self.assertIn('work_order:index:idx_order_owner_status',drift)
        self.assertTrue(missing_schema(self.engine))
