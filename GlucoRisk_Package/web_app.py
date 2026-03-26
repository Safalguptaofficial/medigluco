import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, session, flash, g, Response
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from glucorisk_app import GlucoRiskApp, FIELD_HINTS, ACTIVITY_LABELS

app = Flask(__name__)
# Securely fallback to a persistent default if no env var
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "glucorisk-static-fallback-secret-9942a")
DB_PATH = "glucorisk.db"

app_logic = GlucoRiskApp()


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
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    ''')
    conn.commit()


def setup():
    init_db()


@app.route("/")
def index():
    # redirect root to login or dashboard depending on auth state
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


def query_user(username):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    return user


def login_required(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        if not username or not password:
            flash("Username and password required", "warning")
            return redirect(url_for("register"))
        if query_user(username):
            flash("Username already taken", "warning")
            return redirect(url_for("register"))
        hashed = generate_password_hash(password)
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
        conn.commit()
        flash("Registration successful, please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = query_user(username)
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid credentials", "danger")
            return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out", "info")
    return redirect(url_for("login"))


@app.route("/stream")
@login_required
def stream():
    """Server-Sent Events stream for high-speed live telemetry (Phase 5)."""
    session_id = session.get("user_id")
    username = session.get("username", "Safal Gupta")
    
    return Response(
        app_logic.yield_live_data(session_id, username),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )


@app.route("/api/status")
@login_required
def api_status():
    """Health check endpoint for hardware detection status."""
    import time
    hw_active = (time.time() - getattr(app_logic, 'last_hardware_time', 0)) < 5
    session_id = session.get("user_id")
    session_exists = session_id in app_logic.sessions
    return {
        "hardware_connected": hw_active,
        "serial_port": app_logic.ser.port if app_logic.ser and app_logic.ser.is_open else None,
        "session_active": session_exists,
        "model_loaded": app_logic.model is not None
    }


@app.route("/administer_treatment", methods=["POST"])
@login_required
def administer_treatment():
    data = request.get_json()
    if not data or "treatment" not in data:
        return {"status": "error", "message": "Invalid request"}, 400
    
    treatment = data["treatment"]
    session_id = session.get("user_id")
    if session_id in app_logic.sessions:
        app_logic.sessions[session_id]["intervention_queue"].append(treatment)
        return {"status": "success", "message": f"Administered {treatment}"}
    return {"status": "error", "message": "Simulation not ready"}, 500


@app.route("/inject_telemetry", methods=["POST"])
@login_required
def inject_telemetry():
    """Manual override of live telemetry data (Phase 8)."""
    data = request.get_json()
    if not data:
        return {"status": "error", "message": "Invalid request"}, 400
        
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
            
            return {"status": "success", "message": f"Telemetry overridden, mode set to {mode}"}
        except ValueError:
            return {"status": "error", "message": "Invalid telemetry values"}, 400
    return {"status": "error", "message": "Simulation not running"}, 500


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    if request.method == "POST":
        # collect form values
        data = {}
        for key in FIELD_HINTS.keys():
            raw = request.form.get(key)
            try:
                # convert to float (activity is integer but stored as float)
                data[key] = float(raw)
            except (TypeError, ValueError):
                flash(f"Invalid numeric input provided for {key}.", "danger")
                return redirect(url_for("dashboard"))
        # run inference
        result = app_logic.local_inference(data)
        # persist entry
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO entries (user_id, timestamp, glucose, heart_rate, gsr, spo2, stress, age, bmi, activity, risk, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session["user_id"],
                datetime.now().isoformat(),
                data.get("glucose"),
                data.get("heart_rate"),
                data.get("gsr"),
                data.get("spo2"),
                data.get("stress"),
                data.get("age"),
                data.get("bmi"),
                data.get("activity"),
                result.get("risk"),
                result.get("score"),
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

    # prepare chart data
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


if __name__ == "__main__":
    from werkzeug.serving import run_simple
    # Emulate app context to initialize the database
    with app.app_context():
        setup()
    
    # Threaded mode allows SSE streaming alongside normal dashboard requests
    print("Starting GlucoRisk on http://localhost:5001 ...")
    run_simple("0.0.0.0", 5001, app, use_reloader=False, use_debugger=False, threaded=True)
