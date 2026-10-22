import os
import time
import json
import sqlite3
import smtplib
import threading
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, jsonify, session

app = Flask(__name__)

# 安全讀取環境變數 (無硬編碼秘密資訊)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "default_secret_key_change_me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "").replace(" ", "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "school_grades.db")

# 10 分鐘閒置寄信觸發 (600 秒)
IDLE_THRESHOLD_SECONDS = 10 * 60
last_activity_time = datetime.now()
email_sent_for_idle = False

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS students (
                student_id TEXT PRIMARY KEY,
                student_name TEXT NOT NULL,
                pin TEXT NOT NULL,
                is_public INTEGER DEFAULT 1
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS subjects (
                subject_name TEXT PRIMARY KEY,
                weight REAL NOT NULL CHECK(weight > 0)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS grades (
                student_id TEXT NOT NULL,
                subject_name TEXT NOT NULL,
                score REAL NOT NULL CHECK(score >= 0 AND score <= 100),
                PRIMARY KEY (student_id, subject_name),
                FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE,
                FOREIGN KEY (subject_name) REFERENCES subjects(subject_name) ON DELETE CASCADE
            )
        ''')
        c.execute("SELECT COUNT(*) FROM subjects")
        if c.fetchone()[0] == 0:
            default_subs = [("數學", 4.0), ("英文", 4.0), ("國文", 4.0), ("物理", 2.0), ("化學", 2.0)]
            c.executemany("INSERT INTO subjects (subject_name, weight) VALUES (?, ?)", default_subs)
        conn.commit()

init_db()

@app.before_request
def update_last_activity():
    global last_activity_time, email_sent_for_idle
    last_activity_time = datetime.now()
    email_sent_for_idle = False

def export_db_to_json_string():
    with get_db() as conn:
        students = [dict(r) for r in conn.execute("SELECT * FROM students").fetchall()]
        subjects = [dict(r) for r in conn.execute("SELECT * FROM subjects").fetchall()]
        grades = [dict(r) for r in conn.execute("SELECT * FROM grades").fetchall()]
        
        backup_data = {
            "version": "1.0",
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "students": students,
            "subjects": subjects,
            "grades": grades
        }
        return json.dumps(backup_data, ensure_ascii=False, indent=2)

def send_backup_email(json_str):
    if not SENDER_EMAIL or not SENDER_PASSWORD or not RECEIVER_EMAIL:
        print("未完整設定 Email 環境變數，跳過發送自動備份信件")
        return
        
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = f"🎓 [Render 雲端備份] 10 分鐘閒置資料庫備份 - {datetime.now().strftime('%Y/%m/%d %H:%M')}"

        body = (
            "管理者您好：\n\n"
            "系統已偵測到連續 10 分鐘無人存取（即將進入休眠），已為您自動備份最新數據。\n"
            "若伺服器重置，請複製下方 JSON 內容至 /admin 後台貼上還原。\n\n"
            "=================== 備份 JSON 資料開始 ===================\n\n"
            f"{json_str}\n\n"
            "=================== 備份 JSON 資料結束 ==================="
        )
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"[{datetime.now()}] 10 分鐘閒置備份信件已順利寄出至 {RECEIVER_EMAIL}")
    except Exception as e:
        print(f"[{datetime.now()}] 信件發送失敗: {e}")

def idle_checker_thread():
    global last_activity_time, email_sent_for_idle
    while True:
        time.sleep(20)
        idle_seconds = (datetime.now() - last_activity_time).total_seconds()
        if idle_seconds >= IDLE_THRESHOLD_SECONDS and not email_sent_for_idle:
            json_str = export_db_to_json_string()
            send_backup_email(json_str)
            email_sent_for_idle = True

threading.Thread(target=idle_checker_thread, daemon=True).start()

def validate_backup_payload(data):
    if not isinstance(data, dict):
        return False, "JSON 格式錯誤：根節點必須是物件 (Object)"
    
    for key in ["students", "subjects", "grades"]:
        if key not in data or not isinstance(data[key], list):
            return False, f"JSON 缺漏必要資料陣列：[{key}]"

    for sub in data["subjects"]:
        if not isinstance(sub, dict) or "subject_name" not in sub or "weight" not in sub:
            return False, "subjects 科目資料格式不完整"
        if not isinstance(sub["weight"], (int, float)) or sub["weight"] <= 0:
            return False, f"科目 [{sub.get('subject_name')}] 權重必須大於 0"

    for st in data["students"]:
        if not isinstance(st, dict) or not all(k in st for k in ["student_id", "student_name", "pin"]):
            return False, "students 學生資料缺少學號、姓名或 PIN 碼"

    for gd in data["grades"]:
        if not isinstance(gd, dict) or not all(k in gd for k in ["student_id", "subject_name", "score"]):
            return False, "grades 成績紀錄格式不完整"
        if not isinstance(gd["score"], (int, float)) or not (0 <= gd["score"] <= 100):
            return False, f"學號 [{gd.get('student_id')}] 的成績分數超界 (必須為 0~100)"

    return True, "驗證通過"

# --- 頁面與 API 路由 ---
@app.route('/')
@app.route('/input')
def page_input():
    return render_template('input.html')

@app.route('/rankings')
def page_rankings():
    return render_template('rankings.html')

@app.route('/admin')
def page_admin():
    return render_template('admin.html')

@app.route('/api/subjects', methods=['GET'])
def get_subjects():
    with get_db() as conn:
        subs = conn.execute("SELECT subject_name, weight FROM subjects").fetchall()
        return jsonify([dict(s) for s in subs])

@app.route('/api/auth', methods=['POST'])
def auth_student():
    data = request.json or {}
    action = data.get('action')
    student_id = data.get('student_id', '').strip()
    pin = data.get('pin', '').strip()

    if not student_id or not pin:
        return jsonify({"status": "error", "message": "請輸入學號與通行密碼"}), 400

    with get_db() as conn:
        if action == 'register':
            student_name = data.get('student_name', '').strip()
            is_public = 1 if data.get('is_public', True) else 0
            if not student_name:
                return jsonify({"status": "error", "message": "請輸入姓名"}), 400
            try:
                conn.execute(
                    "INSERT INTO students (student_id, student_name, pin, is_public) VALUES (?, ?, ?, ?)",
                    (student_id, student_name, pin, is_public)
                )
                conn.commit()
                return jsonify({"status": "success", "message": "註冊成功！請進行登入"})
            except sqlite3.IntegrityError:
                return jsonify({"status": "error", "message": "該學號已被註冊"}), 400

        elif action == 'login':
            student = conn.execute(
                "SELECT student_id, student_name, is_public FROM students WHERE student_id = ? AND pin = ?",
                (student_id, pin)
            ).fetchone()
            if student:
                session['student_id'] = student['student_id']
                return jsonify({"status": "success", "student": dict(student)})
            return jsonify({"status": "error", "message": "學號或通行密碼錯誤"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('student_id', None)
    return jsonify({"status": "success"})

@app.route('/api/grades/<student_id>', methods=['GET', 'POST'])
def handle_grades(student_id):
    if session.get('student_id') != student_id:
        return jsonify({"status": "error", "message": "無權限修改，請先完成登入！"}), 403

    with get_db() as conn:
        if request.method == 'GET':
            rows = conn.execute('''
                SELECT g.subject_name, g.score, s.weight 
                FROM grades g 
                JOIN subjects s ON g.subject_name = s.subject_name 
                WHERE g.student_id = ?
            ''', (student_id,)).fetchall()
            return jsonify([dict(r) for r in rows])

        if request.method == 'POST':
            scores = request.json.get('scores', {}) if request.json else {}
            required_subs = [s['subject_name'] for s in conn.execute("SELECT subject_name FROM subjects").fetchall()]
            
            if len(scores) != len(required_subs) or not all(sub in scores for sub in required_subs):
                return jsonify({"status": "error", "message": f"必須完整填寫所有 {len(required_subs)} 個科目的成績才能送出！"}), 400

            validated_scores = {}
            for sub_name, score_val in scores.items():
                if score_val is None or str(score_val).strip() == "":
                    return jsonify({"status": "error", "message": f"[{sub_name}] 請勿留空！"}), 400
                try:
                    score_flt = float(score_val)
                except (ValueError, TypeError):
                    return jsonify({"status": "error", "message": f"[{sub_name}] 分數必須是數字！"}), 400

                if score_flt < 0 or score_flt > 100:
                    return jsonify({"status": "error", "message": f"[{sub_name}] 分數必須介於 0 到 100 之間！"}), 400

                validated_scores[sub_name] = score_flt

            for sub_name, score_flt in validated_scores.items():
                conn.execute('''
                    INSERT INTO grades (student_id, subject_name, score)
                    VALUES (?, ?, ?)
                    ON CONFLICT(student_id, subject_name) DO UPDATE SET score = excluded.score
                ''', (student_id, sub_name, score_flt))
            conn.commit()
            return jsonify({"status": "success", "message": "成績更新成功！"})

@app.route('/api/rankings', methods=['GET'])
def get_rankings():
    with get_db() as conn:
        query = '''
            SELECT 
                s.student_id,
                s.student_name,
                s.is_public,
                ROUND(SUM(g.score * sub.weight) / SUM(sub.weight), 2) AS weighted_avg,
                COUNT(g.subject_name) AS subject_count
            FROM students s
            JOIN grades g ON s.student_id = g.student_id
            JOIN subjects sub ON g.subject_name = sub.subject_name
            GROUP BY s.student_id
            HAVING subject_count = (SELECT COUNT(*) FROM subjects)
            ORDER BY weighted_avg DESC
        '''
        students = [dict(r) for r in conn.execute(query).fetchall()]
        
        current_rank = 0
        last_avg = None
        public_rankings = []

        for i, st in enumerate(students):
            if st['weighted_avg'] != last_avg:
                current_rank = i + 1
                last_avg = st['weighted_avg']
            st['rank'] = current_rank

            if st['is_public'] == 1:
                del st['is_public']
                public_rankings.append(st)

        return jsonify(public_rankings)

@app.route('/api/admin/all_data', methods=['POST'])
def admin_get_data():
    password = request.json.get('password', '') if request.json else ''
    if password != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "管理者密碼不正確！"}), 403

    with get_db() as conn:
        query = '''
            SELECT 
                s.student_id, s.student_name, s.is_public,
                g.subject_name, g.score, sub.weight
            FROM students s
            LEFT JOIN grades g ON s.student_id = g.student_id
            LEFT JOIN subjects sub ON g.subject_name = sub.subject_name
            ORDER BY s.student_id, g.subject_name
        '''
        data = [dict(r) for r in conn.execute(query).fetchall()]
        return jsonify({"status": "success", "data": data})

@app.route('/api/admin/restore', methods=['POST'])
def admin_restore_data():
    req_data = request.json or {}
    password = req_data.get('password', '')
    restore_str = req_data.get('restore_data', '')

    if password != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "管理者密碼錯誤！"}), 403

    try:
        data = json.loads(restore_str)
    except Exception as e:
        return jsonify({"status": "error", "message": f"還原失敗：貼上的文字不是標準 JSON 格式 ({str(e)})"}), 400

    is_valid, err_msg = validate_backup_payload(data)
    if not is_valid:
        return jsonify({"status": "error", "message": f"還原失敗：{err_msg}"}), 400

    conn = get_db()
    try:
        with conn:
            conn.execute("DELETE FROM grades")
            conn.execute("DELETE FROM students")
            conn.execute("DELETE FROM subjects")

            for sub in data['subjects']:
                conn.execute("INSERT INTO subjects (subject_name, weight) VALUES (?, ?)",
                             (sub['subject_name'], float(sub['weight'])))
            for st in data['students']:
                conn.execute("INSERT INTO students (student_id, student_name, pin, is_public) VALUES (?, ?, ?, ?)",
                             (st['student_id'], st['student_name'], str(st['pin']), int(st['is_public'])))
            for gd in data['grades']:
                conn.execute("INSERT INTO grades (student_id, subject_name, score) VALUES (?, ?, ?)",
                             (gd['student_id'], gd['subject_name'], float(gd['score'])))

        return jsonify({"status": "success", "message": "資料庫已成功安全覆蓋還原！"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"寫入資料庫失敗，原資料已被保護且自動恢復：{str(e)}"}), 500
    finally:
        conn.close()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)