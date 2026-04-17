from flask import Flask, Response
import threading, time

app = Flask(__name__)
_html = "<html><body style='background:#080b0f;color:#00d4ff;font-family:monospace;padding:40px'><h2>⚡ Démarrage en cours...</h2><p>Patiente 30 secondes puis rafraîchis.</p></body></html>"

def set_html(h): 
    global _html
    _html = h

@app.route('/')
def index():
    return Response(_html, mimetype='text/html; charset=utf-8')

@app.route('/health')
def health():
    return 'OK'
