import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from werkzeug.exceptions import BadRequest
from werkzeug.security import generate_password_hash

from app import create_app
from business_queries import query
from database_fixture import test_database
from models import Building, House, ParkingSpace, Person, PropertyUnit, User, Visitor, utcnow


PW = 'Fixture-only-296!'
HASH = generate_password_hash(PW)


def china_today():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


class SemanticReadQueryBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        sqlite_url = 'sqlite+pysqlite:///' + str(Path(self.temp.name) / 'property.db')
        self.url = test_database(self, sqlite_url)
        self.app = create_app({
            'TESTING': True,
            'DATABASE_URL': self.url,
            'SECRET_KEY': 'fixture',
            'UPLOAD_FOLDER': self.temp.name,
            'DIFY_API_KEY': '',
        })
        self.factory = self.app.extensions['db_session']
        with self.factory() as db:
            admin = User(username='admin', password_hash=HASH, role=0, real_name='管理员', phone='13800000001')
            db.add(admin)
            db.flush()
            building = Building(community_id=1, name='23栋', floors=20)
            db.add(building)
            db.flush()
            unit = PropertyUnit(community_id=1, building_id=building.id, name='1单元')
            db.add(unit)
            db.flush()
            house = House(
                community_id=1, building_id=building.id, unit_id=unit.id,
                building_name='23栋', unit='1单元', room_no=101,
                area=90, usage='residential', occupancy='owner_occupied', ownership='private',
            )
            host = Person(community_id=1, name='王五', phone='13800000010')
            db.add_all([house, host])
            db.flush()
            db.add_all([
                Visitor(
                    community_id=1, building_id=building.id, house_id=house.id, host_person_id=host.id,
                    name='今日访客', phone='13800000011', purpose='测试',
                    expected_at=utcnow() + timedelta(hours=1), created_at=utcnow(),
                ),
                Visitor(
                    community_id=1, building_id=building.id, house_id=house.id, host_person_id=host.id,
                    name='历史访客', phone='13800000012', purpose='测试',
                    expected_at=utcnow() - timedelta(days=2), created_at=utcnow() - timedelta(days=2),
                ),
                ParkingSpace(community_id=1, building_id=building.id, code='A-001', location='地库', status='available'),
                ParkingSpace(community_id=1, building_id=building.id, code='A-002', location='地库', status='occupied'),
            ])
            db.commit()

    def tearDown(self):
        self.app.extensions['db_engine'].dispose()
        self.temp.cleanup()

    def actor(self, db):
        return db.query(User).filter_by(username='admin').one()

    def test_created_date_returns_only_that_china_local_registration_day(self):
        with self.factory() as db:
            result = query(db, self.actor(db), 'visitor.search', {'created_date': china_today()})
        self.assertEqual([row['name'] for row in result['items']], ['今日访客'])

    def test_invalid_created_date_is_rejected_instead_of_becoming_broad_query(self):
        with self.factory() as db:
            with self.assertRaises(BadRequest):
                query(db, self.actor(db), 'visitor.search', {'created_date': 'today'})

    def test_available_status_returns_only_free_parking_spaces(self):
        with self.factory() as db:
            result = query(db, self.actor(db), 'parking.search', {'status': 'available'})
        self.assertEqual([row['code'] for row in result['items']], ['A-001'])


if __name__ == '__main__':
    unittest.main()
