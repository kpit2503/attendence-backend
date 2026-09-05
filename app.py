from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from datetime import datetime
import json
import os
from flask import Flask, render_template, send_from_directory



app = Flask(__name__)
CORS(app)

base_dir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(base_dir, 'attendance.db')

database_url = os.getenv('DATABASE_URL')

if database_url:
    if database_url.startswith('postgres://'):
        database_url = database_url.replace(
            'postgres://',
            'postgresql://',
            1
        )
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- STATIC & FRONTEND ROUTES ---

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False, default='123456')
    monthly_salary = db.Column(db.Float, default=0)
    working_days = db.Column(db.Integer, default=26)
    tiers = db.Column(db.Text, default=json.dumps({"onTime": "10:00", "t1": "11:00", "t2": "13:00", "t3": "16:00"}))
    records = db.relationship('Record', backref='user', cascade="all, delete-orphan", lazy=True)
    reminders = db.relationship('Reminder', backref='user', cascade="all, delete-orphan", lazy=True)

class Record(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date_key = db.Column(db.String(10), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    pct = db.Column(db.Integer, default=0)
    label = db.Column(db.String(30))

class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    note = db.Column(db.Text, nullable=True)
    date_str = db.Column(db.String(20), nullable=True)

# Ensure tables exist and patch missing columns automatically
with app.app_context():
    db.create_all()
    # Check if 'tiers' column exists in existing sqlite DB, add it if missing
    try:
        connection = db.engine.raw_connection()
        cursor = connection.cursor()
        cursor.execute("PRAGMA table_info(user)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'tiers' not in columns:
            cursor.execute("ALTER TABLE user ADD COLUMN tiers TEXT DEFAULT '{\"onTime\": \"10:00\", \"t1\": \"11:00\", \"t2\": \"13:00\", \"t3\": \"16:00\"}'")
            connection.commit()
        connection.close()
    except Exception as e:
        print(f"Database sync warning: {e}")

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    username = data.get('username', '').strip()
    if not username:
        return jsonify({'status': 'fail', 'message': 'Username is required'}), 400

    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(username=username)
        db.session.add(user)
        db.session.commit()

    tiers_data = {"onTime": "10:00", "t1": "11:00", "t2": "13:00", "t3": "16:00"}
    if user.tiers:
        try:
            tiers_data = json.loads(user.tiers)
        except Exception:
            pass

    return jsonify({
        'status': 'ok',
        'user_id': user.id,
        'username': user.username,
        'monthly_salary': user.monthly_salary,
        'working_days': user.working_days,
        'tiers': tiers_data
    })

@app.route('/api/user/settings', methods=['POST'])
def update_settings():
    data = request.json or {}
    user = User.query.get(data.get('user_id'))
    if user:
        user.monthly_salary = float(data.get('monthly_salary', 0))
        user.working_days = int(data.get('working_days', 26))
        if 'tiers' in data:
            user.tiers = json.dumps(data['tiers'])
        db.session.commit()
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'User not found'}), 404

@app.route('/api/punch', methods=['POST'])
def punch():
    data = request.json or {}
    rec = Record.query.filter_by(user_id=data['user_id'], date_key=data['date_key']).first()
    ts = datetime.fromisoformat(data['timestamp']) if 'timestamp' in data else datetime.utcnow()
    if not rec:
        rec = Record(user_id=data['user_id'], date_key=data['date_key'], pct=data['pct'], label=data['label'], timestamp=ts)
        db.session.add(rec)
    else:
        rec.pct = data['pct']
        rec.label = data['label']
        rec.timestamp = ts
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/records/<int:user_id>')
def get_records(user_id):
    recs = Record.query.filter_by(user_id=user_id).all()
    return jsonify([{
        'id': r.id,
        'date': r.date_key,
        'pct': r.pct,
        'label': r.label,
        'timestamp': r.timestamp.isoformat()
    } for r in recs])

@app.route('/api/records/update', methods=['POST'])
def update_record():
    data = request.json or {}
    rec = Record.query.filter_by(user_id=data['user_id'], date_key=data['date_key']).first()
    if rec:
        rec.pct = data['pct']
        rec.label = data['label']
        rec.timestamp = datetime.fromisoformat(data['timestamp'])
        db.session.commit()
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'not found'}), 404

@app.route('/api/records/delete', methods=['POST'])
def delete_record():
    data = request.json or {}
    rec = Record.query.filter_by(user_id=data['user_id'], date_key=data['date_key']).first()
    if rec:
        db.session.delete(rec)
        db.session.commit()
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'not found'}), 404

@app.route('/api/reminders/<int:user_id>', methods=['GET'])
def get_reminders(user_id):
    items = Reminder.query.filter_by(user_id=user_id).all()
    return jsonify([{'id': i.id, 'title': i.title, 'note': i.note, 'date': i.date_str} for i in items])

@app.route('/api/reminders/add', methods=['POST'])
def add_reminder():
    data = request.json or {}
    rem = Reminder(user_id=data['user_id'], title=data['title'], note=data.get('note', ''), date_str=data.get('date', ''))
    db.session.add(rem)
    db.session.commit()
    return jsonify({'status': 'ok', 'id': rem.id})

@app.route('/api/reminders/delete', methods=['POST'])
def delete_reminder():
    data = request.json or {}
    rem = Reminder.query.get(data['id'])
    if rem:
        db.session.delete(rem)
        db.session.commit()
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'not found'}), 404

@app.route('/')
def serve_html():
    return send_from_directory('.', 'attendance.html')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
