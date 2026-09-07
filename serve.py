import os
from waitress import serve
from app import create_app

if __name__=='__main__':
    serve(create_app(),host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','5000')),threads=8)
