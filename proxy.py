"""
云英 AI 前端代理服务器
将云英聊天页面代理到 3000 端口，可通过沙箱公开域名访问
"""
import http.server
import urllib.request
import urllib.error
import json
import os
import threading
from socketserver import ThreadingMixIn

YUNYING_API = "http://127.0.0.1:8901"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 简化日志
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            # 返回聊天页面
            self._serve_static("index.html", "text/html; charset=utf-8")
        elif self.path == "/health":
            self._proxy("GET", "/health")
        elif self.path.endswith(".css"):
            self._serve_static(self.path.lstrip("/"), "text/css")
        elif self.path.endswith(".js"):
            self._serve_static(self.path.lstrip("/"), "application/javascript")
        else:
            self._proxy("GET", self.path)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        self._proxy("POST", self.path, body)

    def do_OPTIONS(self):
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def _serve_static(self, filename, content_type):
        filepath = os.path.join(STATIC_DIR, filename)
        if os.path.exists(filepath):
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404, f"File not found: {filename}")

    def _proxy(self, method, path, body=b""):
        try:
            url = f"{YUNYING_API}{path}"
            req = urllib.request.Request(
                url,
                data=body if body else None,
                method=method,
                headers={"Content-Type": "application/json"} if body else {}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                response_body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(response_body)
        except urllib.error.HTTPError as e:
            error_body = e.read() if e.fp else b""
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(error_body)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _set_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


if __name__ == "__main__":
    port = 3000
    server = ThreadedHTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"云英 AI 前端代理已启动: http://0.0.0.0:{port}")
    print(f"聊天页面: http://0.0.0.0:{port}/")
    print(f"API 代理: http://0.0.0.0:{port}/api/v1/chat")
    server.serve_forever()
