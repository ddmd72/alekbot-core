import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

def run_dummy_server():
    """Starts a simple server in a background thread."""
    port = int(os.environ.get("PORT", 8080))
    print(f"🏥 Dummy Health Check Server listening on {port}...")
    
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"OK")
        
        def log_message(self, format, *args):
            return

    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    
    # Run the server in a separate thread
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("✅ Dummy server started in background.")
