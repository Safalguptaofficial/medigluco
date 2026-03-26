import os
import re
import time
import json
import logging
from datetime import datetime
from io import BytesIO
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, Response, jsonify, send_file)
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash

from glucorisk_app import GlucoRiskApp, FIELD_HINTS, ACTIVITY_LABELS

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("glucorisk")

# ── App Setup ─────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "glucorisk-static-fallback-secret-9942a")

# Session hardening
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=3600,  # 1 hour
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,  # 1MB max upload
)

# ── CSRF Protection ──────────────────────────────────────────
from flask_wtf.csrf import CSRFProtect
csrf = CSRFProtect(app)
# Exempt SSE and JSON API endpoints from CSRF (they use session auth)
csrf_exempt_views = []

# ── Rate Limiting ─────────────────────────────────────────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)

DB_PATH = "glucorisk.db"
app_logic = GlucoRiskApp()

# ── Database ──────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
    ''')
    c.execute('''
    CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        glucose REAL,
        heart_rate REAL,
        gsr REAL,
        spo2 REAL,
        stress REAL,
        age REAL,
        bmi REAL,
        activity REAL,
        risk TEXT,
        score INTEGER,
        source TEXT DEFAULT 'form',
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    ''')
    # Add source column if upgrading from old schema
    try:
        c.execute("ALTER TABLE entries ADD COLUMN source TEXT DEFAULT 'form'")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()

def setup():
    init_db()
    logger.info("Database initialized")

# ── Auth ──────────────────────────────────────────────────────
USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{3,30}$')

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def query_user(username):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    return c.fetchone()

# ── Routes ────────────────────────────────────────────────────
@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        if not username or not password:
            flash("Username and password required", "warning")
            return redirect(url_for("register"))
        
        if not USERNAME_RE.match(username):
            flash("Username must be 3-30 characters, alphanumeric and underscores only", "warning")
            return redirect(url_for("register"))
        
        if len(password) < 6:
            flash("Password must be at least 6 characters", "warning")
            return redirect(url_for("register"))
        
        if query_user(username):
            flash("Username already taken", "warning")
            return redirect(url_for("register"))
        
        hashed = generate_password_hash(password)
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
        conn.commit()
        logger.info(f"New user registered: {username}")
        flash("Registration successful, please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query_user(username)
        if user and check_password_hash(user["password"], password):
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            logger.info(f"User logged in: {username}")
            return redirect(url_for("dashboard"))
        else:
            logger.warning(f"Failed login attempt for: {username}")
            flash("Invalid credentials", "danger")
            return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    username = session.get("username", "unknown")
    session.clear()
    logger.info(f"User logged out: {username}")
    flash("You have been logged out", "info")
    return redirect(url_for("login"))

# ── SSE Stream ────────────────────────────────────────────────
@app.route("/stream")
@login_required
@csrf.exempt
def stream():
    """Server-Sent Events stream for real-time telemetry."""
    session_id = session.get("user_id")
    username = session.get("username", "Safal Gupta")
    logger.info(f"SSE stream opened for user {username} (session {session_id})")
    
    return Response(
        app_logic.yield_live_data(session_id, username),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )

# ── API Endpoints ─────────────────────────────────────────────
@app.route("/api/status")
@login_required
@csrf.exempt
def api_status():
    """Health check endpoint for hardware detection status."""
    hw_active = (time.time() - getattr(app_logic, 'last_hardware_time', 0)) < 5
    session_id = session.get("user_id")
    session_exists = session_id in app_logic.sessions
    return jsonify({
        "hardware_connected": hw_active,
        "serial_port": app_logic.ser.port if app_logic.ser and app_logic.ser.is_open else None,
        "session_active": session_exists,
        "model_loaded": app_logic.model is not None,
        "server_uptime": time.time()
    })

@app.route("/api/history")
@login_required
@csrf.exempt
def api_history():
    """Fetch telemetry history for the current user."""
    conn = get_db()
    c = conn.cursor()
    limit = min(int(request.args.get("limit", 100)), 500)
    c.execute(
        "SELECT timestamp, glucose, heart_rate, gsr, spo2, risk, score, source "
        "FROM entries WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (session["user_id"], limit)
    )
    rows = c.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/export_pdf")
@login_required
@csrf.exempt
def export_pdf():
    """Generate a clinical PDF report for the current patient."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT timestamp, glucose, heart_rate, spo2, gsr, risk, score "
        "FROM entries WHERE user_id = ? ORDER BY timestamp DESC LIMIT 50",
        (session["user_id"],)
    )
    rows = c.fetchall()
    username = session.get("username", "Unknown")
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=18, textColor=colors.HexColor("#0f172a"))
    subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=10, textColor=colors.grey)
    
    elements = []
    elements.append(Paragraph("GlucoRisk Clinical Report", title_style))
    elements.append(Paragraph(f"Patient: {username} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", subtitle_style))
    elements.append(Spacer(1, 10*mm))
    
    if rows:
        table_data = [["Timestamp", "Glucose", "HR", "SpO2", "GSR", "Risk", "Score"]]
        for r in rows:
            table_data.append([
                r["timestamp"][:19] if r["timestamp"] else "",
                f"{r['glucose']:.1f}" if r['glucose'] else "—",
                f"{r['heart_rate']:.0f}" if r['heart_rate'] else "—",
                f"{r['spo2']:.1f}" if r['spo2'] else "—",
                f"{r['gsr']:.0f}" if r['gsr'] else "—",
                r["risk"] or "—",
                str(r["score"]) if r["score"] else "—"
            ])
        
        t = Table(table_data, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1e293b")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('FONTSIZE', (0, 1), (-1, -1), 7),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ]))
        elements.append(t)
    else:
        elements.append(Paragraph("No telemetry entries found.", styles['Normal']))
    
    doc.build(elements)
    buffer.seek(0)
    logger.info(f"PDF report generated for user {username}")
    return send_file(buffer, mimetype='application/pdf',
                     download_name=f"glucorisk_report_{username}_{datetime.now().strftime('%Y%m%d')}.pdf",
                     as_attachment=True)

# ── Treatment & Override ──────────────────────────────────────
@app.route("/administer_treatment", methods=["POST"])
@login_required
@csrf.exempt
def administer_treatment():
    data = request.get_json()
    if not data or "treatment" not in data:
        return jsonify({"status": "error", "message": "Invalid request"}), 400
    
    treatment = data["treatment"]
    session_id = session.get("user_id")
    if session_id in app_logic.sessions:
        app_logic.sessions[session_id]["intervention_queue"].append(treatment)
        logger.info(f"Treatment administered: {treatment} for session {session_id}")
        return jsonify({"status": "success", "message": f"Administered {treatment}"})
    return jsonify({"status": "error", "message": "Simulation not ready"}), 500

@app.route("/inject_telemetry", methods=["POST"])
@login_required
@csrf.exempt
def inject_telemetry():
    """Manual override of live telemetry data."""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Invalid request"}), 400
        
    session_id = session.get("user_id")
    if session_id in app_logic.sessions:
        try:
            sd = app_logic.sessions[session_id]
            sd["inputs"]["glucose"] = float(data.get("glucose", 100))
            sd["inputs"]["heart_rate"] = float(data.get("heart_rate", 80))
            sd["inputs"]["spo2"] = float(data.get("spo2", 98))
            sd["inputs"]["gsr"] = float(data.get("gsr", 200))
            
            mode = data.get("mode", "manual")
            sd["simulation_mode"] = mode
            logger.info(f"Telemetry injected: mode={mode}, glucose={sd['inputs']['glucose']}")
            return jsonify({"status": "success", "message": f"Telemetry overridden, mode set to {mode}"})
        except ValueError:
            return jsonify({"status": "error", "message": "Invalid telemetry values"}), 400
    return jsonify({"status": "error", "message": "Simulation not running"}), 500

# ── Dashboard ─────────────────────────────────────────────────
@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    if request.method == "POST":
        data = {}
        for key in FIELD_HINTS.keys():
            raw = request.form.get(key)
            try:
                data[key] = float(raw)
            except (TypeError, ValueError):
                flash(f"Invalid numeric input provided for {key}.", "danger")
                return redirect(url_for("dashboard"))
        
        result = app_logic.local_inference(data)
        
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO entries (user_id, timestamp, glucose, heart_rate, gsr, spo2, stress, age, bmi, activity, risk, score, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session["user_id"],
                datetime.now().isoformat(),
                data.get("glucose"), data.get("heart_rate"), data.get("gsr"),
                data.get("spo2"), data.get("stress"), data.get("age"),
                data.get("bmi"), data.get("activity"),
                result.get("risk"), result.get("score"), "form"
            ),
        )
        conn.commit()
        flash(f"Prediction: {result.get('risk')} ({result.get('score')}%)", "success")
        return redirect(url_for("dashboard"))

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM entries WHERE user_id = ? ORDER BY timestamp",
        (session["user_id"],),
    )
    entries = c.fetchall()

    timestamps = [e["timestamp"] for e in entries]
    glucose_values = [e["glucose"] for e in entries]
    scores = [e["score"] for e in entries]
    risk_labels = [e["risk"] for e in entries]

    return render_template(
        "dashboard.html",
        entries=entries,
        timestamps=timestamps,
        glucose_values=glucose_values,
        scores=scores,
        risk_labels=risk_labels,
        FIELD_HINTS=FIELD_HINTS,
        ACTIVITY_LABELS=ACTIVITY_LABELS,
    )

# ── Error Handlers ────────────────────────────────────────────
@app.errorhandler(429)
def ratelimit_handler(e):
    flash("Too many requests. Please wait a moment.", "warning")
    return redirect(url_for("login")), 429

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Bad request"}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Internal server error: {e}")
    return jsonify({"error": "Internal server error"}), 500

# ── Entry Point ───────────────────────────────────────────────
if __name__ == "__main__":
    from werkzeug.serving import run_simple
    with app.app_context():
        setup()
    
    logger.info("Starting GlucoRisk on http://localhost:5001 (threaded mode)")
    run_simple("0.0.0.0", 5001, app, use_reloader=False, use_debugger=False, threaded=True)
