"""Authored smoke fixture; no production data or credentials."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg
from pymongo import MongoClient

mongo = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=5000)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                if self.path == "/write":
                    cursor.execute("INSERT INTO iris_smoke VALUES (1, 'durable') ON CONFLICT DO NOTHING")
                    mongo.fixture.iris_smoke.update_one({"_id": 1}, {"$set": {"marker": "durable"}}, upsert=True)
                cursor.execute("SELECT marker FROM iris_smoke WHERE id = 1")
                row = cursor.fetchone()
        document = mongo.fixture.iris_smoke.find_one({"_id": 1})
        payload = json.dumps({"postgres": row[0] if row else None, "mongodb": document["marker"] if document else None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


HTTPServer(("0.0.0.0", 3000), Handler).serve_forever()
