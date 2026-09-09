import os
from waitress import serve
from wsgi import create_production_app

if __name__=='__main__':
    serve(create_production_app(),host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','5000')),threads=max(2,min(int(os.getenv('WAITRESS_THREADS','8')),64)),channel_timeout=max(10,min(int(os.getenv('WAITRESS_CHANNEL_TIMEOUT','120')),600)))
