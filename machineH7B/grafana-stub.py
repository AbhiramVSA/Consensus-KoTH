#!/usr/bin/env python3
"""
Grafana 8.3.0 CVE-2021-43798 Path Traversal Simulation
Vulnerable endpoint: GET /public/plugins/<plugin-id>/../../../../../../../<file>
"""
import http.server
import os
import sys
import urllib.parse

GRAFANA_ROOT = '/usr/share/grafana'
# Simulated plugin directory
PLUGINS_DIR = '/var/lib/grafana/plugins'

class GrafanaHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.unquote(self.path)

        # CVE-2021-43798: Path traversal via plugin endpoint
        if '/public/plugins/' in path:
            # Extract the traversal path after plugin name
            plugin_part = path.split('/public/plugins/', 1)[1]
            parts = plugin_part.split('/', 1)
            if len(parts) > 1:
                traversal = parts[1]
                # Normalize path traversal
                real_path = os.path.normpath('/' + traversal)
                try:
                    if os.path.isfile(real_path):
                        with open(real_path, 'rb') as f:
                            content = f.read()
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.end_headers()
                        self.wfile.write(content)
                        return
                    else:
                        self.send_error(404, f"File not found: {real_path}")
                        return
                except PermissionError:
                    self.send_error(403, "Permission denied")
                    return
                except Exception as e:
                    self.send_error(500, str(e))
                    return

        # Default Grafana login page
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'''
<html><head><title>Grafana</title>
<style>body{background:#111;color:#eee;font-family:sans-serif;padding:2em;}
.panel{background:#1f1f1f;padding:2em;max-width:400px;margin:auto;border-radius:8px;}
input{background:#2c2c2c;border:1px solid #444;color:#eee;padding:8px;width:100%;margin:4px 0;box-sizing:border-box;}
button{background:#f05a28;border:none;color:white;padding:10px;width:100%;cursor:pointer;border-radius:4px;}</style>
</head><body>
<div class="panel">
<h2>Grafana Login</h2>
<form method=POST action=/login>
<input type=text name=user placeholder="Email or username" value="admin"><br>
<input type=password name=password placeholder="Password"><br>
<button>Log in</button>
</form>
</div>
<p style="text-align:center;color:#666">Grafana v8.3.0 | <a href="/public/plugins/text/../../../../../../../etc/passwd" style="color:#666">plugins</a></p>
</body></html>''')

    def log_message(self, fmt, *args):
        pass

if __name__ == '__main__':
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    server = http.server.HTTPServer(('0.0.0.0', 3000), GrafanaHandler)
    print('Grafana 8.3.0 stub running on port 3000 (CVE-2021-43798)')
    sys.stdout.flush()
    server.serve_forever()
