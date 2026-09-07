"""Optional real-MySQL test backend, each case uses a fresh disposable database."""
import os,uuid
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url

def test_database(testcase,sqlite_url):
    raw=os.getenv('MYSQL_TEST_SERVER_URL')
    if not raw:return sqlite_url
    url=make_url(raw)
    if url.get_backend_name()!='mysql' or url.database:raise ValueError('MYSQL_TEST_SERVER_URL must point to a test server with no database path')
    name='property_test_'+uuid.uuid4().hex
    server=create_engine(url,pool_pre_ping=True)
    with server.begin() as c:c.execute(text('CREATE DATABASE '+name+' CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'))
    def cleanup():
        with server.begin() as c:c.execute(text('DROP DATABASE '+name))
        server.dispose()
    testcase.addCleanup(cleanup)
    return url.set(database=name).render_as_string(hide_password=False)
