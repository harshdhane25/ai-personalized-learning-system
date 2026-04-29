#!/usr/bin/env python3
"""
LearnPath — AI Student Learning Generator
Complete Backend — all features: Resume Builder, To-Do, AI Chat, PDF Summarizer
"""

import os
import io
import json
import sqlite3
import hashlib
import secrets
import requests
import threading
import time
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, session, send_file
from functools import wraps

try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    REPORTLAB_OK = True
except ImportError:
    REPORTLAB_OK = False
    print("⚠  reportlab not installed — run: pip install reportlab")

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = secrets.token_hex(32)

# ── API KEYS ──────────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = "sk-or-v1-your-api-key"
YOUTUBE_API_KEY    = "your-youtude-key"

SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = "your-email-@gmail.com"
SMTP_PASSWORD = "ipde-your-password-hhlb"
EMAIL_FROM    = "LearnPath <noreply@learnpath.ai>"

# ── Models (free fallback chain) ──────────────────────────────────────────────
MODEL_FALLBACKS = [
    "google/gemma-4-26b-a4b-it:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "tencent/hy3-preview:free",
    "inclusionai/ling-2.6-1t:free",
]

# ── AI Queue — one request at a time ─────────────────────────────────────────
_ai_queue    = []
_ai_queue_cv = threading.Condition()


def _ai_worker():
    while True:
        with _ai_queue_cv:
            while not _ai_queue:
                _ai_queue_cv.wait()
            task = _ai_queue.pop(0)
        try:
            task()
        except Exception as e:
            print(f"AI worker error: {e}")


threading.Thread(target=_ai_worker, daemon=True).start()


def _call_or(prompt, model, system_prompt=None):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "LearnPath",
    }
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json={"model": model, "messages": messages, "max_tokens": 1200, "temperature": 0.3},
            timeout=90,
        )
        if r.status_code != 200:
            print(f"OpenRouter HTTP {r.status_code} for {model}: {r.text[:500]}")
            return None
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"OpenRouter error ({model}): {e}")
        return None


def call_openrouter(prompt, models=None, system_prompt=None):
    models = models or MODEL_FALLBACKS

    def try_model(model_name):
        ev     = threading.Event()
        holder = {}

        def task():
            holder["result"] = _call_or(prompt, model_name, system_prompt)
            ev.set()

        with _ai_queue_cv:
            _ai_queue.append(task)
            _ai_queue_cv.notify_all()
        ev.wait(timeout=120)
        return holder.get("result")

    for model in models:
        for attempt in range(2):
            result = try_model(model)
            if result:
                return result
            time.sleep(1 + attempt)
    return None


# ── JSON extraction helper ────────────────────────────────────────────────────
def extract_json(raw: str):
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") or p.startswith("["):
                raw = p
                break
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r'\{[\s\S]*\}', raw)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = "learning_path.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c    = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            student_number TEXT,
            parent_number TEXT,
            parent_email TEXT,
            email TEXT UNIQUE NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_parent INTEGER DEFAULT 0,
            linked_student_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS learning_paths (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            path_data TEXT NOT NULL,
            is_completed INTEGER DEFAULT 0,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS day_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            watch_time_seconds REAL DEFAULT 0,
            total_duration_seconds REAL DEFAULT 0,
            completed INTEGER DEFAULT 0,
            last_position_seconds REAL DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(path_id, day_number, user_id),
            FOREIGN KEY (path_id) REFERENCES learning_paths(id)
        );
        CREATE TABLE IF NOT EXISTS test_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            path_id INTEGER NOT NULL,
            path_name TEXT NOT NULL,
            questions TEXT NOT NULL,
            score INTEGER DEFAULT -1,
            total INTEGER DEFAULT 10,
            attempt_number INTEGER DEFAULT 1,
            completed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS study_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            path_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL,
            notes_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, path_id, day_number)
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            logged_at DATE DEFAULT (date('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            email_type TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS resumes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT DEFAULT 'My Resume',
            template TEXT DEFAULT 'modern',
            contact TEXT DEFAULT '{}',
            summary TEXT DEFAULT '',
            experience TEXT DEFAULT '[]',
            education TEXT DEFAULT '[]',
            skills TEXT DEFAULT '[]',
            projects TEXT DEFAULT '[]',
            certifications TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            category TEXT DEFAULT 'general',
            due_date TEXT,
            completed INTEGER DEFAULT 0,
            completed_at TIMESTAMP,
            path_id INTEGER,
            day_number INTEGER,
            auto_created INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS pdf_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_text TEXT,
            summary TEXT,
            key_points TEXT DEFAULT '[]',
            word_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)

    # Safe migrations for existing DBs
    migrations = [
        ("users",           "is_parent INTEGER DEFAULT 0"),
        ("users",           "linked_student_id INTEGER"),
        ("day_progress",    "last_position_seconds REAL DEFAULT 0"),
        ("learning_paths",  "is_completed INTEGER DEFAULT 0"),
        ("learning_paths",  "completed_at TIMESTAMP"),
        ("todos",           "path_id INTEGER"),
        ("todos",           "day_number INTEGER"),
        ("todos",           "auto_created INTEGER DEFAULT 0"),
    ]
    for table, col_def in migrations:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


init_db()


# ── Auth helpers ──────────────────────────────────────────────────────────────
def hash_pw(p):
    return hashlib.sha256(p.encode()).hexdigest()


def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "user_id" not in session:
            return jsonify({"error": "Not authenticated"}), 401
        return f(*a, **kw)
    return dec


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(to, subject, html):
    if not SMTP_USER or not SMTP_PASSWORD or not to:
        print(f"[EMAIL SKIP] To:{to} | {subject}")
        return
    try:
        msg             = MIMEMultipart("alternative")
        msg["Subject"]  = subject
        msg["From"]     = EMAIL_FROM
        msg["To"]       = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, to, msg.as_string())
        print(f"[EMAIL SENT] To:{to} | {subject}")
    except Exception as e:
        print(f"Email error: {e}")


def send_email_async(to, subject, html):
    threading.Thread(target=send_email, args=(to, subject, html), daemon=True).start()


# ── Path completion checker ───────────────────────────────────────────────────
def check_and_mark_path_complete(path_id, user_id, path_data, conn_ext=None):
    own_conn = conn_ext is None
    conn     = get_db() if own_conn else conn_ext
    try:
        days  = path_data.get("days", [])
        total = len(days)
        if total == 0:
            return False

        path_row = conn.execute(
            "SELECT is_completed FROM learning_paths WHERE id=?", (path_id,)
        ).fetchone()
        if path_row and path_row["is_completed"] == 1:
            return True

        done_count = conn.execute(
            "SELECT COUNT(*) c FROM day_progress WHERE path_id=? AND user_id=? AND completed=1",
            (path_id, user_id)
        ).fetchone()["c"]

        if done_count < total:
            return False

        conn.execute(
            "UPDATE learning_paths SET is_completed=1, completed_at=CURRENT_TIMESTAMP WHERE id=?",
            (path_id,)
        )
        if own_conn:
            conn.commit()

        path_row = conn.execute("SELECT * FROM learning_paths WHERE id=?", (path_id,)).fetchone()
        user_row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

        topic        = path_row["topic"] if path_row else "Your course"
        user_name    = user_row["full_name"] if user_row else "Student"
        user_email   = user_row["email"]     if user_row else None
        parent_email = user_row["parent_email"] if user_row else None

        today_str = date.today().strftime("%B %d, %Y")

        if user_email:
            send_email_async(
                user_email,
                f"🏆 Congratulations! You completed '{topic}' — LearnPath",
                f"""
                <div style="font-family:sans-serif;max-width:520px;margin:auto">
                  <h2 style="color:#2563eb">🎓 Path Complete!</h2>
                  <p>Hi <b>{user_name}</b>,</p>
                  <p>You have successfully completed all {total} days of <b>{topic}</b> on LearnPath!</p>
                  <p>Your certificate is now available. Log in to download it.</p>
                  <p style="color:#6b7280;font-size:13px">Completed on {today_str}</p>
                  <p>Keep learning! 🚀</p>
                </div>
                """
            )

        if parent_email:
            send_email_async(
                parent_email,
                f"🎉 {user_name} completed '{topic}' — LearnPath Parent Update",
                f"""
                <div style="font-family:sans-serif;max-width:520px;margin:auto">
                  <h2 style="color:#059669">🏆 Learning Path Completed!</h2>
                  <p>Hi Parent,</p>
                  <p>Great news! <b>{user_name}</b> has successfully completed all {total} days of
                  the <b>{topic}</b> learning path on LearnPath.</p>
                  <p>They completed it on <b>{today_str}</b>.</p>
                  <p>A certificate of completion has been earned.</p>
                </div>
                """
            )

        log_activity(user_id, "path_completed", topic)
        return True

    finally:
        if own_conn:
            conn.close()


# ── YouTube ───────────────────────────────────────────────────────────────────
def search_youtube(query):
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "q": query, "maxResults": 1, "type": "video",
                    "key": YOUTUBE_API_KEY, "videoDuration": "medium"},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if items:
            v   = items[0]
            vid = v["id"]["videoId"]
            sn  = v["snippet"]
            return {
                "video_id":  vid,
                "title":     sn["title"],
                "thumbnail": sn["thumbnails"]["high"]["url"],
                "channel":   sn["channelTitle"],
                "url":       f"https://www.youtube.com/watch?v={vid}",
            }
    except Exception as e:
        print(f"YouTube error: {e}")
    return None


# ── Fallback learning path ────────────────────────────────────────────────────
def fallback_learning_path(topic, duration_days, start_date_str):
    start_dt    = datetime.strptime(start_date_str, "%Y-%m-%d")
    base_titles = [
        f"Introduction to {topic}",
        f"Core concepts of {topic}",
        f"Basic practice of {topic}",
        f"Intermediate ideas in {topic}",
        f"Examples and applications",
        f"Revision and recap",
        f"Final review",
    ]
    days = []
    for i in range(duration_days):
        title = base_titles[i % len(base_titles)]
        days.append({
            "day_number":    i + 1,
            "date":          (start_dt + timedelta(days=i)).strftime("%Y-%m-%d"),
            "topic":         title,
            "explanation":   f"Learn the main ideas of {title}.",
            "youtube_query": f"{topic} {title} tutorial",
            "video":         search_youtube(f"{topic} {title} tutorial"),
        })
    return {"days": days, "topic": topic, "duration_days": duration_days}


# ── Learning path generation ──────────────────────────────────────────────────
def generate_learning_path(topic, duration_days, start_date_str):
    prompt = (
        f"You are a curriculum designer. Create a {duration_days}-day learning path for: {topic}\n"
        f"Start date: {start_date_str}\n"
        "Return ONLY valid JSON, no extra text:\n"
        '{"days":[{"day_number":1,"date":"YYYY-MM-DD","topic":"Title",'
        '"explanation":"90 word explanation","youtube_query":"search query"}]}\n'
        f"Generate exactly {duration_days} days."
    )
    result = call_openrouter(prompt, system_prompt="Return only valid JSON. No markdown.")
    data   = extract_json(result)
    if not data:
        print(f"Path gen failed. Raw: {str(result)[:200]}")
        return fallback_learning_path(topic, duration_days, start_date_str)

    days = data.get("days", [])
    if len(days) < duration_days:
        print(f"Path gen returned too few days: {len(days)} / {duration_days}")
        return fallback_learning_path(topic, duration_days, start_date_str)

    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    enhanced = []
    for i, day in enumerate(days[:duration_days]):
        day["date"]  = (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        day["video"] = search_youtube(
            day.get("youtube_query", f"{topic} {day.get('topic', '')} tutorial")
        )
        enhanced.append(day)

    return {"days": enhanced, "topic": topic, "duration_days": duration_days}


# ── Streak calculator ─────────────────────────────────────────────────────────
def calculate_streak(path_id, user_id, path_data, conn_ext=None):
    own_conn = conn_ext is None
    conn     = get_db() if own_conn else conn_ext
    try:
        rows = {
            r["day_number"]: r["completed"]
            for r in conn.execute(
                "SELECT day_number, completed FROM day_progress WHERE path_id=? AND user_id=?",
                (path_id, user_id)
            ).fetchall()
        }
    finally:
        if own_conn:
            conn.close()

    days  = path_data.get("days", [])
    today = date.today()

    past_days = [d for d in days if datetime.strptime(d["date"], "%Y-%m-%d").date() <= today]
    if not past_days:
        return 0, None

    consec   = 0
    reset_to = None
    missed   = 0

    for day in reversed(past_days):
        dn = day["day_number"]
        if rows.get(dn, 0) == 1:
            consec += 1
            missed  = 0
        else:
            missed += 1
            if missed >= 2:
                done_days = sorted([d["day_number"] for d in days if rows.get(d["day_number"], 0) == 1])
                reset_to  = (done_days[-1] + 1) if done_days else 1
                consec    = 0
                break

    return consec, reset_to


# ── PDF Certificate ───────────────────────────────────────────────────────────
def make_certificate(user_name, topic, completion_date):
    buf = io.BytesIO()
    if not REPORTLAB_OK:
        buf.write(b"%PDF-1.4 placeholder")
        buf.seek(0)
        return buf
    w, h = landscape(A4)
    cv   = canvas.Canvas(buf, pagesize=landscape(A4))
    cv.setFillColorRGB(.98, .97, .94)
    cv.rect(0, 0, w, h, fill=1, stroke=0)
    cv.setStrokeColorRGB(.7, .55, .1)
    cv.setLineWidth(6)
    cv.rect(30, 30, w - 60, h - 60, fill=0)
    cv.setLineWidth(2)
    cv.rect(42, 42, w - 84, h - 84, fill=0)
    cv.setFillColorRGB(.15, .15, .35)
    cv.setFont("Helvetica-Bold", 36)
    cv.drawCentredString(w / 2, h - 110, "CERTIFICATE OF COMPLETION")
    cv.setFillColorRGB(.7, .55, .1)
    cv.setFont("Helvetica", 15)
    cv.drawCentredString(w / 2, h - 140, "LearnPath AI Student Learning Platform")
    cv.setFillColorRGB(.4, .4, .4)
    cv.setFont("Helvetica", 13)
    cv.drawCentredString(w / 2, h - 185, "This certifies that")
    cv.setFillColorRGB(.1, .1, .3)
    cv.setFont("Helvetica-Bold", 32)
    cv.drawCentredString(w / 2, h - 230, user_name)
    cv.setFillColorRGB(.4, .4, .4)
    cv.setFont("Helvetica", 13)
    cv.drawCentredString(w / 2, h - 265, "has successfully completed the learning path")
    cv.setFillColorRGB(.15, .35, .65)
    cv.setFont("Helvetica-Bold", 20)
    cv.drawCentredString(w / 2, h - 305, topic)
    cv.setFillColorRGB(.4, .4, .4)
    cv.setFont("Helvetica", 12)
    cv.drawCentredString(w / 2, h - 340, f"Completed on: {completion_date}")
    cv.setStrokeColorRGB(.7, .55, .1)
    cv.setLineWidth(1.5)
    cv.line(w / 2 - 120, h - 360, w / 2 + 120, h - 360)
    cv.setFillColorRGB(.5, .5, .5)
    cv.setFont("Helvetica", 10)
    cv.drawCentredString(w / 2, h - 380, "LearnPath AI · Empowering Students Worldwide")
    cv.save()
    buf.seek(0)
    return buf


# ── Study Notes PDF ───────────────────────────────────────────────────────────
def make_notes_pdf(topic, day_topic, notes_text, user_name):
    buf = io.BytesIO()
    if not REPORTLAB_OK:
        buf.write(notes_text.encode())
        buf.seek(0)
        return buf
    doc = SimpleDocTemplate(
        buf, pagesize=A4, rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm
    )
    styles = getSampleStyleSheet()
    t_sty  = ParagraphStyle("t", parent=styles["Title"], fontSize=18, spaceAfter=6,
                            textColor=colors.HexColor("#1e40af"))
    s_sty  = ParagraphStyle("s", parent=styles["Normal"], fontSize=11, spaceAfter=14,
                            textColor=colors.HexColor("#6b7280"))
    b_sty  = ParagraphStyle("b", parent=styles["Normal"], fontSize=11, leading=18, spaceAfter=7)
    story  = [
        Paragraph(f"Study Notes: {day_topic}", t_sty),
        Paragraph(f"Path: {topic} · {user_name} · {date.today():%B %d, %Y}", s_sty),
        Spacer(1, .3 * cm),
    ]
    for line in notes_text.split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, .15 * cm))
        else:
            safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(safe, b_sty))
    doc.build(story)
    buf.seek(0)
    return buf


# ── Background scheduler ──────────────────────────────────────────────────────
def _scheduler():
    while True:
        try:
            now = datetime.now()
            if now.hour == 23 and now.minute < 30:
                _send_11pm_reminders()
            if now.minute < 30:
                _send_streak_alerts()
                _send_todo_overdue_reminders()
        except Exception as e:
            print(f"Scheduler: {e}")
        time.sleep(1800)


def _send_11pm_reminders():
    conn  = get_db()
    today = date.today().isoformat()
    for user in conn.execute("SELECT * FROM users WHERE is_parent=0").fetchall():
        for path in conn.execute(
            "SELECT * FROM learning_paths WHERE user_id=? AND is_completed=0", (user["id"],)
        ).fetchall():
            pd    = json.loads(path["path_data"])
            tday  = next((d for d in pd["days"] if d["date"] == today), None)
            if not tday:
                continue
            prog = conn.execute(
                "SELECT completed FROM day_progress WHERE path_id=? AND user_id=? AND day_number=?",
                (path["id"], user["id"], tday["day_number"])
            ).fetchone()
            if prog and prog["completed"] == 1:
                continue
            already = conn.execute(
                "SELECT id FROM email_log WHERE user_id=? AND email_type='daily_reminder' AND date(sent_at)=?",
                (user["id"], today)
            ).fetchone()
            if already:
                continue
            send_email_async(
                user["email"],
                "⏰ Don't forget today's lesson! — LearnPath",
                f"<h2>Hi {user['full_name']}!</h2>"
                f"<p>You haven't completed today's video for <b>{path['topic']}</b>.</p>"
                f"<p>Topic: <b>{tday['topic']}</b></p>"
                f"<p>Complete it before midnight to keep your streak! 🔥</p>"
            )
            conn.execute("INSERT INTO email_log (user_id, email_type) VALUES (?,?)",
                         (user["id"], "daily_reminder"))
            conn.commit()
    conn.close()


def _send_streak_alerts():
    conn  = get_db()
    today = date.today()
    for user in conn.execute("SELECT * FROM users WHERE is_parent=0").fetchall():
        for path in conn.execute(
            "SELECT * FROM learning_paths WHERE user_id=? AND is_completed=0", (user["id"],)
        ).fetchall():
            pd     = json.loads(path["path_data"])
            streak, _ = calculate_streak(path["id"], user["id"], pd)
            if streak > 0:
                continue
            already = conn.execute(
                "SELECT id FROM email_log WHERE user_id=? AND email_type='streak_alert' AND date(sent_at)=?",
                (user["id"], today.isoformat())
            ).fetchone()
            if already:
                continue
            send_email_async(
                user["email"],
                "⚡ Your streak is about to reset! — LearnPath",
                f"<h2>Hi {user['full_name']}!</h2>"
                f"<p>You've missed 2 consecutive days in <b>{path['topic']}</b>. "
                f"Complete today's video to keep your streak alive! 🔥</p>"
            )
            conn.execute("INSERT INTO email_log (user_id, email_type) VALUES (?,?)",
                         (user["id"], "streak_alert"))
            conn.commit()
    conn.close()


def _send_todo_overdue_reminders():
    """Send email reminders for overdue todos once per day."""
    conn  = get_db()
    today = date.today().isoformat()
    for user in conn.execute("SELECT * FROM users WHERE is_parent=0").fetchall():
        overdue = conn.execute(
            "SELECT * FROM todos WHERE user_id=? AND completed=0 AND due_date IS NOT NULL AND due_date < ?",
            (user["id"], today)
        ).fetchall()
        if not overdue:
            continue
        already = conn.execute(
            "SELECT id FROM email_log WHERE user_id=? AND email_type='todo_overdue' AND date(sent_at)=?",
            (user["id"], today)
        ).fetchone()
        if already:
            continue
        items_html = "".join(
            f"<li><b>{t['title']}</b> — due {t['due_date']} ({t['priority']} priority)</li>"
            for t in overdue
        )
        send_email_async(
            user["email"],
            f"📋 {len(overdue)} overdue task(s) need your attention — LearnPath",
            f"""
            <div style="font-family:sans-serif;max-width:520px;margin:auto">
              <h2 style="color:#dc2626">⚠️ Overdue Tasks</h2>
              <p>Hi <b>{user['full_name']}</b>,</p>
              <p>You have <b>{len(overdue)}</b> overdue task(s):</p>
              <ul>{items_html}</ul>
              <p>Log in to LearnPath to complete or reschedule them.</p>
            </div>
            """
        )
        conn.execute("INSERT INTO email_log (user_id, email_type) VALUES (?,?)",
                     (user["id"], "todo_overdue"))
        conn.commit()
    conn.close()


threading.Thread(target=_scheduler, daemon=True).start()


# ── Activity logger ───────────────────────────────────────────────────────────
def log_activity(user_id, action, detail=None):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO activity_log (user_id, action, detail) VALUES (?,?,?)",
            (user_id, action, detail)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Auto-create todos for learning path ──────────────────────────────────────
def auto_create_path_todos(user_id, path_id, topic, days):
    """Create a todo for each day in a new learning path."""
    try:
        conn = get_db()
        for day in days:
            conn.execute(
                "INSERT OR IGNORE INTO todos (user_id, title, description, priority, category, due_date, path_id, day_number, auto_created)"
                " VALUES (?,?,?,?,?,?,?,?,1)",
                (
                    user_id,
                    f"Complete Day {day['day_number']}: {day['topic']}",
                    f"Watch the learning video for Day {day['day_number']} of {topic}",
                    "medium",
                    "study",
                    day["date"],
                    path_id,
                    day["day_number"]
                )
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Auto-create todos error: {e}")


def auto_complete_path_todo(user_id, path_id, day_number):
    """Mark the corresponding auto-created todo as complete when a day is completed."""
    try:
        conn = get_db()
        conn.execute(
            "UPDATE todos SET completed=1, completed_at=CURRENT_TIMESTAMP"
            " WHERE user_id=? AND path_id=? AND day_number=? AND auto_created=1 AND completed=0",
            (user_id, path_id, day_number)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Auto-complete todo error: {e}")


# =============================================================================
# AUTH
# =============================================================================
@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.json or {}
    for f in ["full_name", "email", "username", "password"]:
        if not d.get(f):
            return jsonify({"error": f"{f} is required"}), 400
    if d["password"] != d.get("confirm_password"):
        return jsonify({"error": "Passwords do not match"}), 400
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (full_name,student_number,parent_number,parent_email,"
            "email,username,password_hash,is_parent) VALUES (?,?,?,?,?,?,?,0)",
            (d["full_name"], d.get("student_number"), d.get("parent_number"),
             d.get("parent_email"), d["email"], d["username"], hash_pw(d["password"]))
        )
        conn.commit()
        return jsonify({"success": True})
    except sqlite3.IntegrityError as e:
        msg = "Email already registered" if "email" in str(e).lower() else "Username already taken"
        return jsonify({"error": msg}), 400
    finally:
        conn.close()


@app.route("/api/auth/login", methods=["POST"])
def login():
    d    = request.json or {}
    un   = d.get("username", "").strip()
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE (username=? OR email=?) AND password_hash=?",
        (un, un, hash_pw(d.get("password", "")))
    ).fetchone()
    conn.close()
    if not user:
        return jsonify({"error": "Invalid username or password"}), 401
    session.update({
        "user_id":   user["id"],
        "username":  user["username"],
        "full_name": user["full_name"],
        "is_parent": user["is_parent"],
    })
    return jsonify({"success": True, "user": {
        "id":        user["id"],
        "username":  user["username"],
        "full_name": user["full_name"],
        "is_parent": user["is_parent"],
    }})


@app.route("/api/auth/logout", methods=["POST"])
def logout_route():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/auth/me")
def me():
    if "user_id" not in session:
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "user": {
        "id":        session["user_id"],
        "username":  session["username"],
        "full_name": session["full_name"],
        "is_parent": session.get("is_parent", 0),
    }})


# ── Profile ───────────────────────────────────────────────────────────────────
@app.route("/api/profile", methods=["GET"])
@login_required
def get_profile():
    conn = get_db()
    u    = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    if not u:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"user": {
        "id":            u["id"],
        "full_name":     u["full_name"],
        "email":         u["email"],
        "username":      u["username"],
        "student_number":u["student_number"],
        "parent_number": u["parent_number"],
        "parent_email":  u["parent_email"],
    }})


@app.route("/api/profile", methods=["PUT"])
@login_required
def update_profile():
    d    = request.json or {}
    conn = get_db()
    try:
        if d.get("new_password"):
            if not d.get("current_password"):
                return jsonify({"error": "Current password required"}), 400
            u = conn.execute("SELECT password_hash FROM users WHERE id=?",
                             (session["user_id"],)).fetchone()
            if u["password_hash"] != hash_pw(d["current_password"]):
                return jsonify({"error": "Current password incorrect"}), 400
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_pw(d["new_password"]), session["user_id"]))
        if d.get("full_name"):
            conn.execute(
                "UPDATE users SET full_name=?,email=?,parent_number=?,parent_email=? WHERE id=?",
                (d.get("full_name"), d.get("email"), d.get("parent_number"),
                 d.get("parent_email"), session["user_id"])
            )
            session["full_name"] = d["full_name"]
        conn.commit()
        return jsonify({"success": True})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already in use"}), 400
    finally:
        conn.close()


# =============================================================================
# PARENT PORTAL
# =============================================================================
@app.route("/api/parent/register", methods=["POST"])
def parent_register():
    d       = request.json or {}
    su      = d.get("student_username", "").strip()
    conn    = get_db()
    student = conn.execute(
        "SELECT * FROM users WHERE username=? AND is_parent=0", (su,)
    ).fetchone()
    if not student:
        conn.close()
        return jsonify({"error": "Student not found"}), 404
    try:
        conn.execute(
            "INSERT INTO users (full_name,email,username,password_hash,is_parent,"
            "linked_student_id,parent_number,parent_email) VALUES (?,?,?,?,1,?,?,?)",
            (d["full_name"], d["email"], d["username"], hash_pw(d["password"]),
             student["id"], d.get("phone", ""), d.get("email", ""))
        )
        conn.commit()
        return jsonify({"success": True})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username or email already taken"}), 400
    finally:
        conn.close()


@app.route("/api/parent/dashboard")
@login_required
def parent_dashboard():
    if not session.get("is_parent"):
        return jsonify({"error": "Not a parent account"}), 403
    conn   = get_db()
    parent = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    sid    = parent["linked_student_id"]
    if not sid:
        conn.close()
        return jsonify({"error": "No linked student"}), 404
    student = conn.execute("SELECT * FROM users WHERE id=?", (sid,)).fetchone()
    paths   = conn.execute(
        "SELECT * FROM learning_paths WHERE user_id=? ORDER BY created_at DESC", (sid,)
    ).fetchall()
    info = []
    for p in paths:
        pd    = json.loads(p["path_data"])
        total = len(pd["days"])
        done  = conn.execute(
            "SELECT COUNT(*) c FROM day_progress WHERE path_id=? AND user_id=? AND completed=1",
            (p["id"], sid)
        ).fetchone()["c"]
        streak, _ = calculate_streak(p["id"], sid, pd)
        tests = conn.execute(
            "SELECT * FROM test_attempts WHERE user_id=? AND path_id=? AND completed=1 ORDER BY created_at DESC",
            (sid, p["id"])
        ).fetchall()
        info.append({
            "id":           p["id"],
            "topic":        p["topic"],
            "duration_days":p["duration_days"],
            "start_date":   p["start_date"],
            "total_days":   total,
            "completed_days": done,
            "progress_pct": round(done / total * 100) if total else 0,
            "streak":       streak,
            "is_completed": p["is_completed"],
            "completed_at": p["completed_at"],
            "tests":        [dict(t) for t in tests],
        })
    conn.close()
    return jsonify({
        "student": {
            "id":        student["id"],
            "full_name": student["full_name"],
            "email":     student["email"],
        },
        "paths": info,
    })


# =============================================================================
# LEARNING PATHS
# =============================================================================
@app.route("/api/generate", methods=["POST"])
@login_required
def generate():
    d     = request.json or {}
    topic = d.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "Topic is required"}), 400
    dur_map  = {"7 days": 7, "2 weeks": 14, "1 month": 30, "2 months": 60, "6 months": 180}
    dur_days = dur_map.get(d.get("duration", "7 days"), 7)
    start    = d.get("start_date", date.today().isoformat())
    pd       = generate_learning_path(topic, dur_days, start)
    if not pd:
        return jsonify({"error": "Failed to generate path."}), 500
    conn   = get_db()
    cursor = conn.execute(
        "INSERT INTO learning_paths (user_id,topic,duration_days,start_date,path_data,is_completed)"
        " VALUES (?,?,?,?,?,0)",
        (session["user_id"], topic, dur_days, start, json.dumps(pd))
    )
    path_id = cursor.lastrowid
    conn.commit()
    conn.close()
    log_activity(session["user_id"], "path_created", topic)
    # Auto-create todos for each day
    threading.Thread(
        target=auto_create_path_todos,
        args=(session["user_id"], path_id, topic, pd["days"]),
        daemon=True
    ).start()
    return jsonify({"success": True, "path_id": path_id, "path_data": pd})


@app.route("/api/paths")
@login_required
def get_paths():
    conn  = get_db()
    paths = conn.execute(
        "SELECT id,topic,duration_days,start_date,created_at,is_completed,completed_at"
        " FROM learning_paths WHERE user_id=? ORDER BY created_at DESC",
        (session["user_id"],)
    ).fetchall()
    result = []
    for p in paths:
        pd    = json.loads(conn.execute(
            "SELECT path_data FROM learning_paths WHERE id=?", (p["id"],)
        ).fetchone()["path_data"])
        total = len(pd.get("days", []))
        done  = conn.execute(
            "SELECT COUNT(*) c FROM day_progress WHERE path_id=? AND user_id=? AND completed=1",
            (p["id"], session["user_id"])
        ).fetchone()["c"]
        row = dict(p)
        row.update({
            "total_days":    total,
            "completed_days":done,
            "progress_pct":  round(done / total * 100) if total else 0,
            "is_completed":  p["is_completed"],
            "completed_at":  p["completed_at"],
        })
        result.append(row)
    conn.close()
    return jsonify({"paths": result})


@app.route("/api/paths/<int:path_id>")
@login_required
def get_path(path_id):
    conn = get_db()
    path = conn.execute(
        "SELECT * FROM learning_paths WHERE id=? AND user_id=?",
        (path_id, session["user_id"])
    ).fetchone()
    if not path:
        conn.close()
        return jsonify({"error": "Path not found"}), 404
    pd   = json.loads(path["path_data"])
    rows = conn.execute(
        "SELECT day_number,watch_time_seconds,total_duration_seconds,completed,last_position_seconds"
        " FROM day_progress WHERE path_id=? AND user_id=?",
        (path_id, session["user_id"])
    ).fetchall()
    conn.close()
    progress = {r["day_number"]: dict(r) for r in rows}
    streak, rt = calculate_streak(path_id, session["user_id"], pd)
    total = len(pd.get("days", []))
    done  = sum(1 for v in progress.values() if v.get("completed") == 1)
    return jsonify({
        "path":         dict(path),
        "path_data":    pd,
        "progress":     progress,
        "streak":       streak,
        "reset_to":     rt,
        "progress_pct": round(done / total * 100) if total else 0,
        "completed_days": done,
        "total_days":   total,
        "is_completed": path["is_completed"],
        "completed_at": path["completed_at"],
    })


@app.route("/api/paths/<int:path_id>/reset", methods=["POST"])
@login_required
def reset_path(path_id):
    conn = get_db()
    if not conn.execute(
        "SELECT id FROM learning_paths WHERE id=? AND user_id=?",
        (path_id, session["user_id"])
    ).fetchone():
        conn.close()
        return jsonify({"error": "Path not found"}), 404
    conn.execute("DELETE FROM day_progress WHERE path_id=? AND user_id=?",
                 (path_id, session["user_id"]))
    conn.execute(
        "UPDATE learning_paths SET is_completed=0, completed_at=NULL WHERE id=?",
        (path_id,)
    )
    # Reset auto-created todos too
    conn.execute(
        "UPDATE todos SET completed=0, completed_at=NULL WHERE path_id=? AND user_id=? AND auto_created=1",
        (path_id, session["user_id"])
    )
    conn.commit()
    conn.close()
    log_activity(session["user_id"], "path_reset", str(path_id))
    return jsonify({"success": True})


@app.route("/api/paths/<int:path_id>", methods=["DELETE"])
@login_required
def delete_path(path_id):
    conn = get_db()
    if not conn.execute(
        "SELECT id FROM learning_paths WHERE id=? AND user_id=?",
        (path_id, session["user_id"])
    ).fetchone():
        conn.close()
        return jsonify({"error": "Path not found"}), 404
    conn.execute("DELETE FROM day_progress WHERE path_id=?", (path_id,))
    conn.execute("DELETE FROM study_notes WHERE path_id=?", (path_id,))
    conn.execute("DELETE FROM test_attempts WHERE path_id=?", (path_id,))
    conn.execute("DELETE FROM todos WHERE path_id=? AND auto_created=1", (path_id,))
    conn.execute("DELETE FROM learning_paths WHERE id=?", (path_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/progress", methods=["POST"])
@login_required
def save_progress():
    d    = request.json or {}
    conn = get_db()
    conn.execute(
        "INSERT INTO day_progress (path_id,day_number,user_id,watch_time_seconds,"
        "total_duration_seconds,completed,last_position_seconds,last_updated)"
        " VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)"
        " ON CONFLICT(path_id,day_number,user_id) DO UPDATE SET"
        " watch_time_seconds=excluded.watch_time_seconds,"
        " total_duration_seconds=excluded.total_duration_seconds,"
        " completed=MAX(completed, excluded.completed),"
        " last_position_seconds=excluded.last_position_seconds,"
        " last_updated=CURRENT_TIMESTAMP",
        (d["path_id"], d["day_number"], session["user_id"],
         d.get("watch_time_seconds", 0), d.get("total_duration_seconds", 0),
         d.get("completed", 0), d.get("last_position_seconds", 0))
    )
    conn.commit()

    just_completed = d.get("completed", 0) == 1

    if just_completed:
        log_activity(session["user_id"], "day_completed",
                     f"path:{d['path_id']} day:{d['day_number']}")
        # Auto-complete the corresponding todo
        auto_complete_path_todo(session["user_id"], d["path_id"], d["day_number"])
        path_row = conn.execute(
            "SELECT path_data FROM learning_paths WHERE id=?", (d["path_id"],)
        ).fetchone()
        if path_row:
            pd = json.loads(path_row["path_data"])
            check_and_mark_path_complete(d["path_id"], session["user_id"], pd, conn_ext=conn)
            conn.commit()

    conn.close()
    return jsonify({"success": True})


@app.route("/api/activity")
@login_required
def get_activity():
    conn = get_db()
    rows = conn.execute(
        "SELECT logged_at, COUNT(*) as count FROM activity_log WHERE user_id=?"
        " GROUP BY logged_at ORDER BY logged_at DESC LIMIT 90",
        (session["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify({"activity": [dict(r) for r in rows]})


# =============================================================================
# TEST SECTION
# =============================================================================
@app.route("/api/test/create", methods=["POST"])
@login_required
def create_test():
    d         = request.json or {}
    path_id   = d.get("path_id")
    path_name = d.get("path_name", "")
    conn      = get_db()
    attempts  = conn.execute(
        "SELECT * FROM test_attempts WHERE user_id=? AND path_id=? ORDER BY attempt_number",
        (session["user_id"], path_id)
    ).fetchall()

    if len(attempts) >= 3:
        conn.close()
        return jsonify({"error": "Maximum 3 attempts reached", "attempts_used": 3}), 400

    incomplete = next((a for a in attempts if a["completed"] == 0), None)
    if incomplete:
        conn.close()
        return jsonify({
            "success":        True,
            "test_id":        incomplete["id"],
            "questions":      json.loads(incomplete["questions"]),
            "attempt_number": incomplete["attempt_number"],
            "is_existing":    True,
        })
    conn.close()

    attempt_num = len(attempts) + 1
    prompt = (
        f'Generate exactly 10 MCQ questions about: {path_name}\n'
        'Return ONLY this JSON (no extra text, no markdown):\n'
        '{"questions":[{"id":1,"question":"Q?","options":["A) opt1","B) opt2","C) opt3","D) opt4"],'
        '"correct":"A) opt1","explanation":"brief"}]}'
    )
    raw  = call_openrouter(prompt, system_prompt="Return only valid JSON. No markdown fences.")
    if not raw:
        return jsonify({"error": "AI unavailable. Please try again."}), 500

    data      = extract_json(raw)
    if not data:
        return jsonify({"error": "Failed to parse test. Please try again."}), 500

    questions = data.get("questions", [])
    for i, q in enumerate(questions):
        q["id"] = i + 1
        if "options" not in q or len(q["options"]) < 2:
            q["options"] = ["A) True", "B) False", "C) Maybe", "D) None of the above"]
        if q.get("correct", "") not in q["options"]:
            q["correct"] = q["options"][0]
        if "explanation" not in q:
            q["explanation"] = ""

    if len(questions) < 5:
        return jsonify({"error": "Not enough questions generated. Please try again."}), 500

    questions = questions[:10]
    conn      = get_db()
    cursor    = conn.execute(
        "INSERT INTO test_attempts (user_id,path_id,path_name,questions,attempt_number)"
        " VALUES (?,?,?,?,?)",
        (session["user_id"], path_id, path_name, json.dumps(questions), attempt_num)
    )
    test_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({
        "success":        True,
        "test_id":        test_id,
        "questions":      questions,
        "attempt_number": attempt_num,
        "is_existing":    False,
    })


@app.route("/api/test/<int:test_id>/submit", methods=["POST"])
@login_required
def submit_test(test_id):
    d       = request.json or {}
    answers = d.get("answers", {})
    conn    = get_db()
    test    = conn.execute(
        "SELECT * FROM test_attempts WHERE id=? AND user_id=?",
        (test_id, session["user_id"])
    ).fetchone()
    if not test:
        conn.close()
        return jsonify({"error": "Test not found"}), 404
    if test["completed"] == 1:
        conn.close()
        return jsonify({"error": "Test already submitted"}), 400

    questions = json.loads(test["questions"])
    score     = 0
    results   = []
    for q in questions:
        qid      = q["id"]
        selected = answers.get(str(qid), answers.get(qid, answers.get(int(qid) if str(qid).isdigit() else qid, "")))
        correct  = q["correct"]
        is_right = (str(selected).strip() == str(correct).strip()) if selected else False
        if is_right:
            score += 1
        results.append({
            "id":          qid,
            "question":    q["question"],
            "options":     q["options"],
            "correct":     correct,
            "selected":    selected,
            "is_correct":  is_right,
            "explanation": q.get("explanation", ""),
        })

    conn.execute("UPDATE test_attempts SET score=?,completed=1 WHERE id=?", (score, test_id))
    conn.commit()

    user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if user:
        pct = round(score / 10 * 100)
        if user["parent_email"]:
            send_email_async(
                user["parent_email"],
                f"📊 Test Score Update — LearnPath",
                f"""
                <div style="font-family:sans-serif;max-width:520px;margin:auto">
                  <h2 style="color:#2563eb">📝 Test Result</h2>
                  <p>Hi Parent,</p>
                  <p><b>{user['full_name']}</b> scored <b>{score}/10 ({pct}%)</b> on the test for
                  <b>{test['path_name']}</b> (Attempt {test['attempt_number']}).</p>
                  {'<p style="color:#059669">✅ Passed!</p>' if score >= 7 else '<p style="color:#dc2626">❌ Needs more practice.</p>'}
                </div>
                """
            )

    conn.close()
    log_activity(session["user_id"], "test_completed", f"score:{score}/10")
    return jsonify({"success": True, "score": score, "total": 10, "results": results})


@app.route("/api/test/history/<int:path_id>")
@login_required
def test_history(path_id):
    conn  = get_db()
    tests = conn.execute(
        "SELECT id,attempt_number,score,total,completed,created_at,path_name"
        " FROM test_attempts WHERE user_id=? AND path_id=? ORDER BY attempt_number",
        (session["user_id"], path_id)
    ).fetchall()
    conn.close()
    return jsonify({"tests": [dict(t) for t in tests], "attempts_used": len(tests)})


# =============================================================================
# STUDY NOTES
# =============================================================================
@app.route("/api/notes/generate", methods=["POST"])
@login_required
def generate_notes():
    d          = request.json or {}
    path_id    = d.get("path_id")
    day_number = d.get("day_number")
    day_topic  = d.get("day_topic", "")
    path_topic = d.get("path_topic", "")
    conn       = get_db()
    existing   = conn.execute(
        "SELECT notes_text FROM study_notes WHERE user_id=? AND path_id=? AND day_number=?",
        (session["user_id"], path_id, day_number)
    ).fetchone()
    conn.close()
    if existing:
        return jsonify({"success": True, "notes": existing["notes_text"], "cached": True})

    prompt = (
        f'Create study notes for "{day_topic}" (from {path_topic} course).\n'
        "Use bullet points with emoji: key concepts, important terms, examples, quick summary.\n"
        "Keep it concise and student-friendly. Use emoji section headers."
    )
    notes = call_openrouter(prompt, system_prompt="Return plain text notes only.")
    if not notes:
        notes = (
            f"📌 Key Concepts\n- Main ideas of {day_topic}\n\n"
            f"🧠 Important Terms\n- Review the essential terms related to {path_topic}\n\n"
            f"✅ Quick Summary\n- Study the topic, practice examples, and revise once more."
        )

    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO study_notes (user_id,path_id,day_number,notes_text) VALUES (?,?,?,?)",
        (session["user_id"], path_id, day_number, notes)
    )
    conn.commit()
    conn.close()
    log_activity(session["user_id"], "notes_generated", f"path:{path_id} day:{day_number}")
    return jsonify({"success": True, "notes": notes, "cached": False})


@app.route("/api/notes/pdf", methods=["POST"])
@login_required
def notes_pdf():
    d    = request.json or {}
    conn = get_db()
    user  = conn.execute("SELECT full_name FROM users WHERE id=?", (session["user_id"],)).fetchone()
    notes = conn.execute(
        "SELECT notes_text FROM study_notes WHERE user_id=? AND path_id=? AND day_number=?",
        (session["user_id"], d.get("path_id"), d.get("day_number"))
    ).fetchone()
    conn.close()
    if not notes:
        return jsonify({"error": "Notes not found. Generate them first."}), 404
    buf = make_notes_pdf(
        d.get("path_topic", ""), d.get("day_topic", "Notes"),
        notes["notes_text"], user["full_name"]
    )
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"notes_{d.get('day_topic','notes')[:25].replace(' ','_')}.pdf")


# =============================================================================
# CERTIFICATE
# =============================================================================
@app.route("/api/certificate/<int:path_id>")
@login_required
def certificate(path_id):
    conn = get_db()
    path = conn.execute(
        "SELECT * FROM learning_paths WHERE id=? AND user_id=?",
        (path_id, session["user_id"])
    ).fetchone()
    if not path:
        conn.close()
        return jsonify({"error": "Path not found"}), 404
    pd    = json.loads(path["path_data"])
    total = len(pd.get("days", []))
    done  = conn.execute(
        "SELECT COUNT(*) c FROM day_progress WHERE path_id=? AND user_id=? AND completed=1",
        (path_id, session["user_id"])
    ).fetchone()["c"]
    user = conn.execute("SELECT full_name FROM users WHERE id=?",
                        (session["user_id"],)).fetchone()
    conn.close()

    if done < total:
        return jsonify({"error": f"Path not complete. {done}/{total} days done."}), 400

    if path["is_completed"] == 0:
        check_and_mark_path_complete(path_id, session["user_id"], pd)

    comp_date = path["completed_at"] or date.today().isoformat()
    try:
        formatted = datetime.strptime(comp_date[:10], "%Y-%m-%d").strftime("%B %d, %Y")
    except Exception:
        formatted = date.today().strftime("%B %d, %Y")

    buf = make_certificate(user["full_name"], path["topic"], formatted)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"certificate_{path['topic'][:25].replace(' ','_')}.pdf")


# =============================================================================
# RESUME BUILDER
# =============================================================================

def make_resume_pdf_modern(data):
    """Modern template — blue accent, clean layout."""
    buf = io.BytesIO()
    if not REPORTLAB_OK:
        buf.write(b"%PDF placeholder")
        buf.seek(0)
        return buf

    doc    = SimpleDocTemplate(buf, pagesize=A4,
                               rightMargin=1.8*cm, leftMargin=1.8*cm,
                               topMargin=1.5*cm, bottomMargin=1.5*cm)
    accent = colors.HexColor("#2563eb")
    dark   = colors.HexColor("#1f2937")
    muted  = colors.HexColor("#6b7280")

    name_sty    = ParagraphStyle("name", fontSize=26, fontName="Helvetica-Bold",
                                  textColor=dark, spaceAfter=2)
    title_sty   = ParagraphStyle("title", fontSize=12, fontName="Helvetica",
                                  textColor=accent, spaceAfter=4)
    contact_sty = ParagraphStyle("contact", fontSize=9, fontName="Helvetica",
                                  textColor=muted, spaceAfter=2)
    sec_sty     = ParagraphStyle("sec", fontSize=11, fontName="Helvetica-Bold",
                                  textColor=accent, spaceBefore=10, spaceAfter=3)
    body_sty    = ParagraphStyle("body", fontSize=9.5, fontName="Helvetica",
                                  textColor=dark, spaceAfter=3, leading=14)
    bullet_sty  = ParagraphStyle("bul", fontSize=9, fontName="Helvetica",
                                  textColor=dark, spaceAfter=2, leading=13,
                                  leftIndent=12, bulletIndent=4)
    sub_sty     = ParagraphStyle("sub", fontSize=9, fontName="Helvetica-Oblique",
                                  textColor=muted, spaceAfter=2)

    contact = data.get("contact", {})
    story   = []

    story.append(Paragraph(contact.get("name", "Your Name"), name_sty))
    if contact.get("job_title"):
        story.append(Paragraph(contact["job_title"], title_sty))
    parts = []
    for k in ["email", "phone", "location", "linkedin", "website"]:
        if contact.get(k):
            parts.append(contact[k])
    if parts:
        story.append(Paragraph(" · ".join(parts), contact_sty))
    story.append(HRFlowable(width="100%", thickness=2, color=accent, spaceAfter=6))

    if data.get("summary"):
        story.append(Paragraph("SUMMARY", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d1d5db"), spaceAfter=4))
        story.append(Paragraph(data["summary"], body_sty))

    exp_list = data.get("experience", [])
    if exp_list:
        story.append(Paragraph("EXPERIENCE", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d1d5db"), spaceAfter=4))
        for exp in exp_list:
            row = f"<b>{exp.get('role','')}</b> — {exp.get('company','')}"
            story.append(Paragraph(row, body_sty))
            date_loc = " | ".join(filter(None, [exp.get("duration",""), exp.get("location","")]))
            if date_loc:
                story.append(Paragraph(date_loc, sub_sty))
            for bp in (exp.get("bullets") or []):
                if bp.strip():
                    story.append(Paragraph(f"• {bp}", bullet_sty))
            story.append(Spacer(1, 4))

    edu_list = data.get("education", [])
    if edu_list:
        story.append(Paragraph("EDUCATION", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d1d5db"), spaceAfter=4))
        for edu in edu_list:
            row = f"<b>{edu.get('degree','')}</b> — {edu.get('school','')}"
            story.append(Paragraph(row, body_sty))
            meta = " | ".join(filter(None, [edu.get("year",""), edu.get("gpa","")]))
            if meta:
                story.append(Paragraph(meta, sub_sty))

    skills = data.get("skills", [])
    if skills:
        story.append(Paragraph("SKILLS", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d1d5db"), spaceAfter=4))
        skill_text = " · ".join([s.get("name", s) if isinstance(s, dict) else s for s in skills])
        story.append(Paragraph(skill_text, body_sty))

    proj_list = data.get("projects", [])
    if proj_list:
        story.append(Paragraph("PROJECTS", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d1d5db"), spaceAfter=4))
        for proj in proj_list:
            story.append(Paragraph(f"<b>{proj.get('name','')}</b>", body_sty))
            if proj.get("description"):
                story.append(Paragraph(proj["description"], bullet_sty))
            if proj.get("tech"):
                story.append(Paragraph(f"Tech: {proj['tech']}", sub_sty))

    certs = data.get("certifications", [])
    if certs:
        story.append(Paragraph("CERTIFICATIONS", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d1d5db"), spaceAfter=4))
        for cert in certs:
            story.append(Paragraph(f"• {cert.get('name','')} — {cert.get('issuer','')} ({cert.get('year','')})", bullet_sty))

    doc.build(story)
    buf.seek(0)
    return buf


def make_resume_pdf_classic(data):
    """Classic template — black/gray, traditional look."""
    buf = io.BytesIO()
    if not REPORTLAB_OK:
        buf.write(b"%PDF placeholder")
        buf.seek(0)
        return buf

    doc    = SimpleDocTemplate(buf, pagesize=A4,
                               rightMargin=2*cm, leftMargin=2*cm,
                               topMargin=2*cm, bottomMargin=2*cm)
    dark   = colors.HexColor("#111111")
    muted  = colors.HexColor("#555555")

    name_sty    = ParagraphStyle("name", fontSize=22, fontName="Helvetica-Bold",
                                  textColor=dark, spaceAfter=2, alignment=1)
    contact_sty = ParagraphStyle("contact", fontSize=9, fontName="Helvetica",
                                  textColor=muted, spaceAfter=6, alignment=1)
    sec_sty     = ParagraphStyle("sec", fontSize=10, fontName="Helvetica-Bold",
                                  textColor=dark, spaceBefore=10, spaceAfter=3)
    body_sty    = ParagraphStyle("body", fontSize=9.5, fontName="Helvetica",
                                  textColor=dark, spaceAfter=3, leading=14)
    bullet_sty  = ParagraphStyle("bul", fontSize=9, fontName="Helvetica",
                                  textColor=dark, spaceAfter=2, leading=13, leftIndent=12)
    sub_sty     = ParagraphStyle("sub", fontSize=9, fontName="Helvetica-Oblique",
                                  textColor=muted, spaceAfter=2)

    contact = data.get("contact", {})
    story   = []

    story.append(Paragraph(contact.get("name", "Your Name"), name_sty))
    parts = []
    for k in ["email", "phone", "location", "linkedin"]:
        if contact.get(k):
            parts.append(contact[k])
    if parts:
        story.append(Paragraph(" | ".join(parts), contact_sty))
    story.append(HRFlowable(width="100%", thickness=1.5, color=dark, spaceAfter=6))

    if data.get("summary"):
        story.append(Paragraph("OBJECTIVE", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=muted, spaceAfter=4))
        story.append(Paragraph(data["summary"], body_sty))

    exp_list = data.get("experience", [])
    if exp_list:
        story.append(Paragraph("WORK EXPERIENCE", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=muted, spaceAfter=4))
        for exp in exp_list:
            story.append(Paragraph(f"<b>{exp.get('role','')}</b>, {exp.get('company','')}", body_sty))
            meta = " | ".join(filter(None,[exp.get("duration",""),exp.get("location","")]))
            if meta:
                story.append(Paragraph(meta, sub_sty))
            for bp in (exp.get("bullets") or []):
                if bp.strip():
                    story.append(Paragraph(f"• {bp}", bullet_sty))
            story.append(Spacer(1, 4))

    edu_list = data.get("education", [])
    if edu_list:
        story.append(Paragraph("EDUCATION", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=muted, spaceAfter=4))
        for edu in edu_list:
            story.append(Paragraph(f"<b>{edu.get('degree','')}</b>, {edu.get('school','')}", body_sty))
            meta = " | ".join(filter(None,[edu.get("year",""),edu.get("gpa","")]))
            if meta:
                story.append(Paragraph(meta, sub_sty))

    skills = data.get("skills", [])
    if skills:
        story.append(Paragraph("SKILLS", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=muted, spaceAfter=4))
        skill_text = ", ".join([s.get("name", s) if isinstance(s, dict) else s for s in skills])
        story.append(Paragraph(skill_text, body_sty))

    certs = data.get("certifications", [])
    if certs:
        story.append(Paragraph("CERTIFICATIONS", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=muted, spaceAfter=4))
        for cert in certs:
            story.append(Paragraph(f"• {cert.get('name','')} — {cert.get('issuer','')} ({cert.get('year','')})", bullet_sty))

    doc.build(story)
    buf.seek(0)
    return buf


def make_resume_pdf_academic(data):
    """Academic template — purple accent."""
    buf = io.BytesIO()
    if not REPORTLAB_OK:
        buf.write(b"%PDF placeholder")
        buf.seek(0)
        return buf

    doc    = SimpleDocTemplate(buf, pagesize=A4,
                               rightMargin=2*cm, leftMargin=2*cm,
                               topMargin=1.8*cm, bottomMargin=1.8*cm)
    accent = colors.HexColor("#7c3aed")
    dark   = colors.HexColor("#1f2937")
    muted  = colors.HexColor("#6b7280")

    name_sty     = ParagraphStyle("name", fontSize=24, fontName="Helvetica-Bold",
                                   textColor=dark, spaceAfter=2)
    subtitle_sty = ParagraphStyle("sub2", fontSize=11, fontName="Helvetica",
                                   textColor=accent, spaceAfter=4)
    contact_sty  = ParagraphStyle("contact", fontSize=8.5, fontName="Helvetica",
                                   textColor=muted, spaceAfter=6)
    sec_sty      = ParagraphStyle("sec", fontSize=10, fontName="Helvetica-Bold",
                                   textColor=accent, spaceBefore=12, spaceAfter=2)
    body_sty     = ParagraphStyle("body", fontSize=9.5, fontName="Helvetica",
                                   textColor=dark, spaceAfter=3, leading=14)
    bullet_sty   = ParagraphStyle("bul", fontSize=9, fontName="Helvetica",
                                   textColor=dark, spaceAfter=2, leading=13, leftIndent=14)
    sub_sty      = ParagraphStyle("sub3", fontSize=8.5, fontName="Helvetica-Oblique",
                                   textColor=muted, spaceAfter=2)

    contact = data.get("contact", {})
    story   = []

    story.append(Paragraph(contact.get("name", "Your Name"), name_sty))
    if contact.get("job_title"):
        story.append(Paragraph(contact["job_title"], subtitle_sty))
    parts = []
    for k in ["email", "phone", "location", "linkedin", "website"]:
        if contact.get(k):
            parts.append(contact[k])
    if parts:
        story.append(Paragraph("  ·  ".join(parts), contact_sty))
    story.append(HRFlowable(width="100%", thickness=2.5, color=accent, spaceAfter=6))

    if data.get("summary"):
        story.append(Paragraph("Research Interests / Summary", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e9d5ff"), spaceAfter=3))
        story.append(Paragraph(data["summary"], body_sty))

    edu_list = data.get("education", [])
    if edu_list:
        story.append(Paragraph("Education", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e9d5ff"), spaceAfter=3))
        for edu in edu_list:
            story.append(Paragraph(f"<b>{edu.get('degree','')}</b> — {edu.get('school','')}", body_sty))
            meta = " | ".join(filter(None,[edu.get("year",""),edu.get("gpa",""),edu.get("field","")]))
            if meta:
                story.append(Paragraph(meta, sub_sty))

    exp_list = data.get("experience", [])
    if exp_list:
        story.append(Paragraph("Research / Work Experience", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e9d5ff"), spaceAfter=3))
        for exp in exp_list:
            story.append(Paragraph(f"<b>{exp.get('role','')}</b> — {exp.get('company','')}", body_sty))
            meta = " | ".join(filter(None,[exp.get("duration",""),exp.get("location","")]))
            if meta:
                story.append(Paragraph(meta, sub_sty))
            for bp in (exp.get("bullets") or []):
                if bp.strip():
                    story.append(Paragraph(f"• {bp}", bullet_sty))
            story.append(Spacer(1, 4))

    proj_list = data.get("projects", [])
    if proj_list:
        story.append(Paragraph("Publications / Projects", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e9d5ff"), spaceAfter=3))
        for proj in proj_list:
            story.append(Paragraph(f"<b>{proj.get('name','')}</b>", body_sty))
            if proj.get("description"):
                story.append(Paragraph(proj["description"], bullet_sty))

    skills = data.get("skills", [])
    if skills:
        story.append(Paragraph("Technical Skills", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e9d5ff"), spaceAfter=3))
        skill_text = " · ".join([s.get("name", s) if isinstance(s, dict) else s for s in skills])
        story.append(Paragraph(skill_text, body_sty))

    certs = data.get("certifications", [])
    if certs:
        story.append(Paragraph("Awards & Certifications", sec_sty))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e9d5ff"), spaceAfter=3))
        for cert in certs:
            story.append(Paragraph(f"• {cert.get('name','')} — {cert.get('issuer','')} ({cert.get('year','')})", bullet_sty))

    doc.build(story)
    buf.seek(0)
    return buf


@app.route("/api/resume", methods=["GET"])
@login_required
def get_resumes():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,title,template,created_at,updated_at FROM resumes WHERE user_id=? ORDER BY updated_at DESC",
        (session["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify({"resumes": [dict(r) for r in rows]})


@app.route("/api/resume/<int:resume_id>", methods=["GET"])
@login_required
def get_resume(resume_id):
    conn = get_db()
    r    = conn.execute("SELECT * FROM resumes WHERE id=? AND user_id=?",
                        (resume_id, session["user_id"])).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Resume not found"}), 404
    row = dict(r)
    for field in ["contact","experience","education","skills","projects","certifications"]:
        try:
            row[field] = json.loads(row[field]) if isinstance(row[field], str) else row[field]
        except Exception:
            row[field] = {} if field == "contact" else []
    return jsonify({"resume": row})


@app.route("/api/resume", methods=["POST"])
@login_required
def create_resume():
    d    = request.json or {}
    conn = get_db()
    cur  = conn.execute(
        "INSERT INTO resumes (user_id,title,template,contact,summary,experience,education,skills,projects,certifications)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session["user_id"], d.get("title","My Resume"), d.get("template","modern"),
         json.dumps(d.get("contact",{})), d.get("summary",""),
         json.dumps(d.get("experience",[])), json.dumps(d.get("education",[])),
         json.dumps(d.get("skills",[])), json.dumps(d.get("projects",[])),
         json.dumps(d.get("certifications",[])))
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    log_activity(session["user_id"], "resume_created", d.get("title","Resume"))
    return jsonify({"success": True, "resume_id": rid})


@app.route("/api/resume/<int:resume_id>", methods=["PUT"])
@login_required
def update_resume(resume_id):
    d    = request.json or {}
    conn = get_db()
    if not conn.execute("SELECT id FROM resumes WHERE id=? AND user_id=?",
                        (resume_id, session["user_id"])).fetchone():
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.execute(
        "UPDATE resumes SET title=?,template=?,contact=?,summary=?,experience=?,education=?,"
        "skills=?,projects=?,certifications=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (d.get("title","My Resume"), d.get("template","modern"),
         json.dumps(d.get("contact",{})), d.get("summary",""),
         json.dumps(d.get("experience",[])), json.dumps(d.get("education",[])),
         json.dumps(d.get("skills",[])), json.dumps(d.get("projects",[])),
         json.dumps(d.get("certifications",[])), resume_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/resume/<int:resume_id>", methods=["DELETE"])
@login_required
def delete_resume(resume_id):
    conn = get_db()
    if not conn.execute("SELECT id FROM resumes WHERE id=? AND user_id=?",
                        (resume_id, session["user_id"])).fetchone():
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.execute("DELETE FROM resumes WHERE id=?", (resume_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/resume/<int:resume_id>/pdf")
@login_required
def download_resume_pdf(resume_id):
    conn = get_db()
    r    = conn.execute("SELECT * FROM resumes WHERE id=? AND user_id=?",
                        (resume_id, session["user_id"])).fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Not found"}), 404

    row = dict(r)
    for field in ["contact","experience","education","skills","projects","certifications"]:
        try:
            row[field] = json.loads(row[field]) if isinstance(row[field], str) else row[field]
        except Exception:
            row[field] = {} if field == "contact" else []

    template = row.get("template","modern")
    if template == "classic":
        buf = make_resume_pdf_classic(row)
    elif template == "academic":
        buf = make_resume_pdf_academic(row)
    else:
        buf = make_resume_pdf_modern(row)

    fname = f"resume_{row['contact'].get('name','resume')[:20].replace(' ','_')}.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)


@app.route("/api/resume/ai-enhance", methods=["POST"])
@login_required
def ai_enhance_resume():
    d       = request.json or {}
    section = d.get("section","")
    content = d.get("content","")
    context = d.get("context","")

    if section == "summary":
        prompt = (
            f"Rewrite this professional summary to be more impactful and concise (3-4 sentences):\n\n{content}\n\n"
            f"Context: {context}\nReturn only the improved summary text, no explanation."
        )
    elif section == "bullets":
        prompt = (
            f"Improve these job description bullet points to be more achievement-focused "
            f"using strong action verbs and quantified results where possible:\n\n{content}\n\n"
            f"Role/Company context: {context}\nReturn the improved bullets, one per line starting with •. No extra explanation."
        )
    elif section == "skills":
        prompt = (
            f"Given this role/context: {context}\nSuggest 10-15 relevant technical and soft skills "
            f"as a comma-separated list:\n{content}\n"
            "Return only a comma-separated list of skill names. No explanation."
        )
    else:
        prompt = f"Improve this resume section:\n\n{content}\n\nContext: {context}\nReturn only the improved text."

    result = call_openrouter(prompt, system_prompt="You are a professional resume writer. Return only the improved content.")
    if not result:
        return jsonify({"error": "AI unavailable. Please try again."}), 500
    return jsonify({"success": True, "enhanced": result.strip()})


# =============================================================================
# TO-DO LIST
# =============================================================================
@app.route("/api/todos", methods=["GET"])
@login_required
def get_todos():
    category  = request.args.get("category","")
    priority  = request.args.get("priority","")
    completed = request.args.get("completed","")
    conn = get_db()
    q    = "SELECT * FROM todos WHERE user_id=?"
    params = [session["user_id"]]
    if category:
        q += " AND category=?"; params.append(category)
    if priority:
        q += " AND priority=?"; params.append(priority)
    if completed != "":
        q += " AND completed=?"; params.append(int(completed))
    q += " ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify({"todos": [dict(r) for r in rows]})


@app.route("/api/todos", methods=["POST"])
@login_required
def create_todo():
    d    = request.json or {}
    if not d.get("title","").strip():
        return jsonify({"error": "Title required"}), 400
    conn = get_db()
    cur  = conn.execute(
        "INSERT INTO todos (user_id,title,description,priority,category,due_date) VALUES (?,?,?,?,?,?)",
        (session["user_id"], d["title"].strip(), d.get("description",""),
         d.get("priority","medium"), d.get("category","general"), d.get("due_date"))
    )
    tid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
    conn.close()
    log_activity(session["user_id"], "todo_created", d["title"])
    return jsonify({"success": True, "todo": dict(row)})


@app.route("/api/todos/<int:todo_id>", methods=["PUT"])
@login_required
def update_todo(todo_id):
    d    = request.json or {}
    conn = get_db()
    if not conn.execute("SELECT id FROM todos WHERE id=? AND user_id=?",
                        (todo_id, session["user_id"])).fetchone():
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.execute(
        "UPDATE todos SET title=?,description=?,priority=?,category=?,due_date=? WHERE id=?",
        (d.get("title"), d.get("description",""), d.get("priority","medium"),
         d.get("category","general"), d.get("due_date"), todo_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/todos/<int:todo_id>/toggle", methods=["POST"])
@login_required
def toggle_todo(todo_id):
    conn = get_db()
    row  = conn.execute("SELECT * FROM todos WHERE id=? AND user_id=?",
                        (todo_id, session["user_id"])).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    new_state = 0 if row["completed"] else 1
    if new_state:
        conn.execute("UPDATE todos SET completed=1, completed_at=CURRENT_TIMESTAMP WHERE id=?", (todo_id,))
    else:
        conn.execute("UPDATE todos SET completed=0, completed_at=NULL WHERE id=?", (todo_id,))
    conn.commit()
    conn.close()
    if new_state:
        log_activity(session["user_id"], "todo_completed", row["title"])
    return jsonify({"success": True, "completed": new_state})


@app.route("/api/todos/<int:todo_id>", methods=["DELETE"])
@login_required
def delete_todo(todo_id):
    conn = get_db()
    conn.execute("DELETE FROM todos WHERE id=? AND user_id=?", (todo_id, session["user_id"]))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/todos/stats", methods=["GET"])
@login_required
def todo_stats():
    conn  = get_db()
    today = date.today().isoformat()
    total = conn.execute("SELECT COUNT(*) c FROM todos WHERE user_id=?", (session["user_id"],)).fetchone()["c"]
    done  = conn.execute("SELECT COUNT(*) c FROM todos WHERE user_id=? AND completed=1", (session["user_id"],)).fetchone()["c"]
    high  = conn.execute("SELECT COUNT(*) c FROM todos WHERE user_id=? AND priority='high' AND completed=0", (session["user_id"],)).fetchone()["c"]
    overdue = conn.execute(
        "SELECT COUNT(*) c FROM todos WHERE user_id=? AND completed=0 AND due_date IS NOT NULL AND due_date < ?",
        (session["user_id"], today)
    ).fetchone()["c"]
    conn.close()
    return jsonify({"total": total, "done": done, "pending": total-done, "high_priority": high, "overdue": overdue})


# =============================================================================
# AI CHATBOT — Page-Context Aware
# =============================================================================
@app.route("/api/chat/send", methods=["POST"])
@login_required
def chat_send():
    import uuid as _uuid
    d          = request.json or {}
    message    = d.get("message","").strip()
    session_id = d.get("session_id", str(_uuid.uuid4()))
    context    = d.get("context", {})   # {page, path_id, day_number, topic, day_topic}
    if not message:
        return jsonify({"error": "Message required"}), 400

    conn = get_db()
    # Get conversation history (last 12 messages)
    history = conn.execute(
        "SELECT role, content FROM chat_messages WHERE user_id=? AND session_id=? ORDER BY created_at DESC LIMIT 12",
        (session["user_id"], session_id)
    ).fetchall()
    history = list(reversed(history))

    # Build context-aware system message
    page      = context.get("page", "")
    topic     = context.get("topic", "")
    day_topic = context.get("day_topic", "")
    day_num   = context.get("day_number", "")

    if page == "path" and topic:
        if day_topic and day_num:
            system_msg = (
                f"You are LearnPath AI Assistant — a friendly, expert tutor. "
                f"The student is currently on the Learning Path page for '{topic}', "
                f"Day {day_num} about '{day_topic}'. "
                f"Answer questions concisely, explain concepts clearly, and suggest related topics. "
                f"Be encouraging and use examples. Keep responses under 300 words unless asked for more."
            )
        else:
            system_msg = (
                f"You are LearnPath AI Assistant — a friendly tutor. "
                f"The student is studying '{topic}'. Help them understand concepts, "
                f"suggest study strategies, and answer questions about the subject."
            )
    elif page == "test":
        system_msg = (
            f"You are LearnPath AI Assistant helping a student prepare for their test"
            f"{' on ' + topic if topic else ''}. "
            f"Help them understand concepts, practice questions, and review material. "
            f"Do NOT give direct answers to test questions, but explain underlying concepts."
        )
    elif page == "resume":
        system_msg = (
            "You are LearnPath AI Resume Assistant. Help students write professional resumes. "
            "Give actionable advice on bullet points, summaries, skills, and formatting. "
            "Be specific and concise."
        )
    elif page == "pdf":
        system_msg = (
            "You are LearnPath AI Document Assistant. Help students understand documents, "
            "extract key information, and answer questions about uploaded PDFs. Be precise and helpful."
        )
    else:
        system_msg = (
            "You are LearnPath AI Assistant — a helpful tutor and study companion for students. "
            "Help with learning questions, explain concepts clearly, suggest study strategies, "
            "help debug code, explain math, and provide academic support. "
            "Be encouraging, clear, and concise. Use examples when helpful."
        )

    messages_for_ai = []
    for h in history:
        messages_for_ai.append({"role": h["role"], "content": h["content"]})
    messages_for_ai.append({"role": "user", "content": message})

    # Save user message
    conn.execute(
        "INSERT INTO chat_messages (user_id, session_id, role, content) VALUES (?,?,?,?)",
        (session["user_id"], session_id, "user", message)
    )
    conn.commit()

    def _chat_call():
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5000",
            "X-Title": "LearnPath",
        }
        sys_messages = [{"role": "system", "content": system_msg}] + messages_for_ai
        for model in MODEL_FALLBACKS:
            try:
                r = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json={"model": model, "messages": sys_messages, "max_tokens": 800, "temperature": 0.7},
                    timeout=60,
                )
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"]
            except Exception:
                continue
        return None

    ev     = threading.Event()
    holder = {}

    def task():
        holder["result"] = _chat_call()
        ev.set()

    with _ai_queue_cv:
        _ai_queue.append(task)
        _ai_queue_cv.notify_all()
    ev.wait(timeout=90)

    reply = holder.get("result") or "I'm sorry, I'm having trouble connecting right now. Please try again in a moment."

    conn.execute(
        "INSERT INTO chat_messages (user_id, session_id, role, content) VALUES (?,?,?,?)",
        (session["user_id"], session_id, "assistant", reply)
    )
    conn.commit()
    conn.close()
    log_activity(session["user_id"], "chat_message", session_id)
    return jsonify({"success": True, "reply": reply, "session_id": session_id})


@app.route("/api/chat/suggestions", methods=["GET"])
@login_required
def chat_suggestions():
    """Generate 3 context-aware quick question suggestions."""
    page      = request.args.get("page","")
    topic     = request.args.get("topic","")
    day_topic = request.args.get("day_topic","")
    day_num   = request.args.get("day_number","")

    if page == "path" and topic and day_topic:
        prompt = (
            f"Generate exactly 3 short, engaging questions a student might ask about "
            f"'{day_topic}' (Day {day_num} of a '{topic}' course). "
            f"Return ONLY a JSON array of 3 strings. Example: [\"What is X?\", \"How does Y work?\", \"Give me an example of Z\"]"
        )
    elif page == "test" and topic:
        prompt = (
            f"Generate exactly 3 study help questions for a student preparing a test on '{topic}'. "
            f"Return ONLY a JSON array of 3 strings."
        )
    elif page == "resume":
        prompt = (
            "Generate exactly 3 questions a student might ask when building their resume. "
            "Return ONLY a JSON array of 3 strings."
        )
    else:
        prompt = (
            "Generate exactly 3 general study help questions a student might ask an AI tutor. "
            "Return ONLY a JSON array of 3 strings."
        )

    raw = call_openrouter(prompt, system_prompt="Return only a valid JSON array of 3 strings. No extra text.")
    suggestions = []
    if raw:
        try:
            parsed = extract_json(raw)
            if isinstance(parsed, list):
                suggestions = [str(s) for s in parsed[:3]]
        except Exception:
            pass

    if not suggestions:
        defaults = {
            "path": [
                f"Explain {day_topic or topic} in simple terms",
                f"Give me a quiz question about {day_topic or topic}",
                f"What are the key takeaways from today's lesson?"
            ],
            "test": ["Help me understand this concept", "What should I focus on?", "Give me a practice question"],
            "resume": ["How do I write a good summary?", "What skills should I list?", "Improve my bullet points"],
        }
        suggestions = defaults.get(page, ["How can I study better?", "Explain a concept to me", "Give me study tips"])

    return jsonify({"suggestions": suggestions})


@app.route("/api/chat/history", methods=["GET"])
@login_required
def chat_history():
    session_id = request.args.get("session_id","")
    conn       = get_db()
    if session_id:
        rows = conn.execute(
            "SELECT role,content,created_at FROM chat_messages WHERE user_id=? AND session_id=? ORDER BY created_at",
            (session["user_id"], session_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role,content,created_at FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
            (session["user_id"],)
        ).fetchall()
    conn.close()
    return jsonify({"messages": [dict(r) for r in rows]})


@app.route("/api/chat/sessions", methods=["GET"])
@login_required
def chat_sessions():
    conn = get_db()
    rows = conn.execute(
        "SELECT session_id, MIN(content) first_msg, MAX(created_at) last_at, COUNT(*) msg_count"
        " FROM chat_messages WHERE user_id=? AND role='user'"
        " GROUP BY session_id ORDER BY last_at DESC LIMIT 20",
        (session["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify({"sessions": [dict(r) for r in rows]})


@app.route("/api/chat/clear", methods=["POST"])
@login_required
def chat_clear():
    import uuid as _uuid
    d          = request.json or {}
    session_id = d.get("session_id","")
    conn       = get_db()
    if session_id:
        conn.execute("DELETE FROM chat_messages WHERE user_id=? AND session_id=?",
                     (session["user_id"], session_id))
    else:
        conn.execute("DELETE FROM chat_messages WHERE user_id=?", (session["user_id"],))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


# =============================================================================
# PDF SUMMARIZER — with chunked map-reduce summarization
# =============================================================================

try:
    import PyPDF2
    PYPDF2_OK = True
except ImportError:
    try:
        import pypdf as PyPDF2
        PYPDF2_OK = True
    except ImportError:
        PYPDF2_OK = False
        print("⚠  PyPDF2/pypdf not installed — run: pip install pypdf2")


def extract_text_from_pdf_bytes(pdf_bytes):
    """Extract text from PDF bytes using PyPDF2/pypdf."""
    if not PYPDF2_OK:
        return ""
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        texts  = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                texts.append(t)
        return "\n".join(texts)
    except Exception as e:
        print(f"PDF extract error: {e}")
        return ""


def chunk_text(text, max_words=2500):
    """Split text into chunks of max_words words."""
    words  = text.split()
    chunks = []
    for i in range(0, len(words), max_words):
        chunks.append(" ".join(words[i:i+max_words]))
    return chunks


def summarize_pdf_text(raw_text):
    """Map-reduce summarization for long documents."""
    word_count = len(raw_text.split())

    if word_count <= 3000:
        # Short document — single pass
        prompt = (
            f"Summarize this document in 3-5 clear paragraphs. Capture the main ideas:\n\n{raw_text}"
        )
        summary = call_openrouter(prompt, system_prompt="Summarize documents clearly and concisely.")
        return summary or "Summary unavailable — please try again."

    # Long document — map-reduce
    chunks = chunk_text(raw_text, max_words=2500)
    chunk_summaries = []
    for i, chunk in enumerate(chunks[:8]):   # max 8 chunks
        prompt = f"Summarize this section of a document (part {i+1}):\n\n{chunk}"
        s = call_openrouter(prompt, system_prompt="Summarize concisely in 2-3 paragraphs.")
        if s:
            chunk_summaries.append(s)

    if not chunk_summaries:
        return "Summary unavailable — please try again."

    combined = "\n\n---\n\n".join(chunk_summaries)
    final_prompt = (
        f"The following are summaries of different sections of a document. "
        f"Combine them into a single coherent 4-6 paragraph summary:\n\n{combined}"
    )
    final = call_openrouter(final_prompt, system_prompt="Write a clear, cohesive document summary.")
    return final or combined[:2000]


def extract_key_points_from_text(text):
    """Extract 8-10 key points from document text."""
    truncated = " ".join(text.split()[:4000])
    prompt = (
        f"Extract exactly 8-10 key points from this document as a JSON array of strings:\n\n{truncated}\n\n"
        "Return ONLY a JSON array like: [\"Point 1\", \"Point 2\", ...]"
    )
    raw = call_openrouter(prompt, system_prompt="Return only a JSON array of key points.")
    if raw:
        try:
            parsed = extract_json(raw)
            if isinstance(parsed, list):
                return [str(p) for p in parsed[:10]]
        except Exception:
            pass
    return ["Key points unavailable — please regenerate."]


@app.route("/api/pdf-summarize/upload", methods=["POST"])
@login_required
def pdf_summarize_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    pdf_bytes = f.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        return jsonify({"error": "File too large. Max 10MB."}), 400

    raw_text = extract_text_from_pdf_bytes(pdf_bytes)
    if not raw_text.strip():
        return jsonify({"error": "Could not extract text from PDF. It may be image-based or scanned."}), 400

    word_count = len(raw_text.split())
    summary    = summarize_pdf_text(raw_text)
    key_points = extract_key_points_from_text(raw_text)

    conn = get_db()
    cur  = conn.execute(
        "INSERT INTO pdf_summaries (user_id,filename,original_text,summary,key_points,word_count) VALUES (?,?,?,?,?,?)",
        (session["user_id"], f.filename, raw_text[:60000], summary,
         json.dumps(key_points), word_count)
    )
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    log_activity(session["user_id"], "pdf_summarized", f.filename)
    return jsonify({
        "success":    True,
        "summary_id": sid,
        "filename":   f.filename,
        "summary":    summary,
        "key_points": key_points,
        "word_count": word_count,
    })


@app.route("/api/pdf-summarize/ask", methods=["POST"])
@login_required
def pdf_summarize_ask():
    d          = request.json or {}
    summary_id = d.get("summary_id")
    question   = d.get("question","").strip()
    if not question:
        return jsonify({"error": "Question required"}), 400

    conn = get_db()
    row  = conn.execute("SELECT * FROM pdf_summaries WHERE id=? AND user_id=?",
                        (summary_id, session["user_id"])).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Summary not found"}), 404

    context = row["original_text"][:8000] if row["original_text"] else row["summary"]
    prompt  = (
        f"Based on this document, answer the question:\n\nQuestion: {question}\n\nDocument content:\n{context}\n\n"
        "Answer clearly and concisely, citing relevant parts of the document."
    )
    answer = call_openrouter(prompt, system_prompt="Answer questions about documents accurately.")
    if not answer:
        return jsonify({"error": "AI unavailable. Please try again."}), 500
    return jsonify({"success": True, "answer": answer.strip()})


@app.route("/api/pdf-summarize/history", methods=["GET"])
@login_required
def pdf_summarize_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT id,filename,word_count,created_at FROM pdf_summaries WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
        (session["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify({"summaries": [dict(r) for r in rows]})


@app.route("/api/pdf-summarize/<int:summary_id>", methods=["GET"])
@login_required
def get_pdf_summary(summary_id):
    conn = get_db()
    row  = conn.execute("SELECT * FROM pdf_summaries WHERE id=? AND user_id=?",
                        (summary_id, session["user_id"])).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    r = dict(row)
    try:
        r["key_points"] = json.loads(r["key_points"]) if r["key_points"] else []
    except Exception:
        r["key_points"] = []
    r.pop("original_text", None)
    return jsonify({"summary": r})


@app.route("/api/pdf-summarize/<int:summary_id>", methods=["DELETE"])
@login_required
def delete_pdf_summary(summary_id):
    conn = get_db()
    conn.execute("DELETE FROM pdf_summaries WHERE id=? AND user_id=?",
                 (summary_id, session["user_id"]))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/pdf-summarize/<int:summary_id>/download")
@login_required
def download_pdf_summary(summary_id):
    """Download summary as a text file."""
    conn = get_db()
    row  = conn.execute("SELECT * FROM pdf_summaries WHERE id=? AND user_id=?",
                        (summary_id, session["user_id"])).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404

    try:
        key_points = json.loads(row["key_points"]) if row["key_points"] else []
    except Exception:
        key_points = []

    kp_text = "\n".join(f"  {i+1}. {kp}" for i, kp in enumerate(key_points))
    content = (
        f"DOCUMENT SUMMARY — LearnPath AI\n"
        f"{'='*50}\n"
        f"File: {row['filename']}\n"
        f"Words: {row['word_count']}\n"
        f"Summarized: {row['created_at']}\n\n"
        f"SUMMARY\n{'-'*30}\n{row['summary']}\n\n"
        f"KEY POINTS\n{'-'*30}\n{kp_text}\n"
    )

    buf = io.BytesIO(content.encode("utf-8"))
    buf.seek(0)
    fname = f"summary_{row['filename'][:30].replace(' ','_').replace('.pdf','')}.txt"
    return send_file(buf, mimetype="text/plain", as_attachment=True, download_name=fname)


# =============================================================================
# STATIC
# =============================================================================
@app.route("/")
def index():
    return send_file("index.html")


@app.route("/<path:filename>")
def serve_file(filename):
    return send_file(filename)


if __name__ == "__main__":
    print("=" * 60)
    print("🎓 LearnPath — AI Student Learning Generator")
    print("=" * 60)
    print(f"  → http://localhost:5000")
    print(f"  → DB: {DB_PATH}")
    print(f"  → ReportLab: {'✓' if REPORTLAB_OK else '✗  pip install reportlab'}")
    print(f"  → PyPDF2:    {'✓' if PYPDF2_OK else '✗  pip install pypdf2'}")
    print("=" * 60)
    app.run(debug=True, port=5000)