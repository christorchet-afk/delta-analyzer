from flask import Flask, Response
import threading, time, os

app = Flask(__name__)
_html = "<html><body style='background:#080b0f;color:#00d4ff;font-family:monospace;padding:40px'><h2>⚡ Démarrage en cours...</h2><p>Patiente 60 secondes puis rafraîchis.</p></body></html>"

def set_html(h):
    global _html
    _html = h

@app.route('/')
def index():
    return Response(_html, mimetype='text/html; charset=utf-8')

@app.route('/health')
def health():
    return 'OK'

def start_main():
    """Lance main.py en arrière-plan après 2s."""
    time.sleep(2)
    try:
        import main
        main.main()
    except Exception as e:
        print(f"[MAIN] Erreur: {e}")

# Lancer main.py automatiquement au démarrage de Flask
threading.Thread(target=start_main, daemon=True).start()
