import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import urlopen


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with urlopen(os.environ["API_URL"] + self.path, timeout=10) as response:
            payload = response.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
