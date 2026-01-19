import os
import json
import time
import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_key")

# Data files
DATA_DIR = "data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")

# Ensure data directory exists
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# Ensure data files exist
def init_db():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'w') as f:
            json.dump([], f)
    if not os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'w') as f:
            json.dump([], f)

init_db()

# Helpers
def load_json(filepath):
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except:
        return []

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

def get_user(username):
    users = load_json(USERS_FILE)
    for user in users:
        if user['username'] == username:
            return user
    return None

# Dummy Captions
DUMMY_CAPTIONS = [
    {"caption": "A person holding a red mug in a bright kitchen.", "objects": ["person", "mug", "kitchen"], "actions": ["holding"]},
    {"caption": "A set of keys lying on a wooden table near a laptop.", "objects": ["keys", "table", "laptop"], "actions": ["lying"]},
    {"caption": "A cat sleeping on a blue sofa.", "objects": ["cat", "sofa"], "actions": ["sleeping"]},
    {"caption": "A white cane leaning against a wall.", "objects": ["cane", "wall"], "actions": ["leaning"]},
    {"caption": "A bottle of water on a desk.", "objects": ["bottle", "desk"], "actions": ["standing"]},
    {"caption": "A pair of glasses on a book.", "objects": ["glasses", "book"], "actions": ["resting"]}
]

last_caption_update = time.time()
current_caption_index = 0

# Routes

@app.route('/')
def index():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        # Check if user exists
        if get_user(username):
            return "User already exists", 400
        
        users = load_json(USERS_FILE)
        new_user = {
            "username": username,
            "password": generate_password_hash(password),
            "vision_assistance_level": request.form['vision_assistance_level'],
            "auto_speak": 'auto_speak' in request.form,
            "speech_rate": int(request.form.get('speech_rate', 165)),
            "important_objects": [x.strip() for x in request.form.get('important_objects', '').split(',')]
        }
        users.append(new_user)
        save_json(USERS_FILE, users)
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = get_user(username)
        if user and check_password_hash(user['password'], password):
            session['user'] = user
            return redirect(url_for('dashboard'))
        return "Invalid credentials", 401
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))
    # Re-fetch user to get latest settings
    user = get_user(session['user']['username'])
    if user:
        session['user'] = user
    return render_template('dashboard.html', user=session['user'])

@app.route('/memory')
def memory():
    if 'user' not in session:
        return redirect(url_for('login'))
    memories = load_json(MEMORY_FILE)
    user_memories = [m for m in memories if m.get('user') == session['user']['username']]
    return render_template('memory.html', memories=reversed(user_memories), user=session['user'])

@app.route('/find', methods=['GET', 'POST'])
def find():
    if 'user' not in session:
        return redirect(url_for('login'))
    result = None
    if request.method == 'POST':
        query = request.form['object'].lower()
        memories = load_json(MEMORY_FILE)
        found = None
        for m in reversed(memories):
            if m.get('user') == session['user']['username'] and query in m['object'].lower():
                found = m
                break
        
        if found:
            result = f"Your {found['object']} were last seen {found['description']} at {found['timestamp']}."
        else:
            result = "Sorry, I have not seen that object recently."
            
    return render_template('find.html', result=result, user=session['user'])

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if 'user' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        users = load_json(USERS_FILE)
        for user in users:
            if user['username'] == session['user']['username']:
                user['vision_assistance_level'] = request.form['vision_assistance_level']
                user['auto_speak'] = 'auto_speak' in request.form
                user['speech_rate'] = int(request.form.get('speech_rate', 165))
                user['important_objects'] = [x.strip() for x in request.form.get('important_objects', '').split(',')]
                session['user'] = user
                break
        save_json(USERS_FILE, users)
        return redirect(url_for('dashboard'))
        
    return render_template('settings.html', user=session['user'])

# API Endpoints

@app.route('/api/caption')
def api_caption():
    global last_caption_update, current_caption_index
    now = time.time()
    
    # Rotate caption every 5 seconds
    if now - last_caption_update > 5:
        current_caption_index = (current_caption_index + 1) % len(DUMMY_CAPTIONS)
        last_caption_update = now
        
    data = DUMMY_CAPTIONS[current_caption_index]
    
    status = "LIVE"
    if now - last_caption_update > 10:
        status = "SLOW"
    
    return jsonify({
        "status": status,
        "caption": data['caption'],
        "objects": data['objects'],
        "actions": data['actions'],
        "updated_at": datetime.datetime.now().strftime("%I:%M %p")
    })

@app.route('/api/memory', methods=['GET'])
def api_memory():
    if 'user' not in session:
        return jsonify([]), 401
    memories = load_json(MEMORY_FILE)
    user_memories = [m for m in memories if m.get('user') == session['user']['username']]
    return jsonify(user_memories)

@app.route('/api/memory/add', methods=['POST'])
def api_memory_add():
    if 'user' not in session:
        return "Unauthorized", 401
    
    data = request.json
    memories = load_json(MEMORY_FILE)
    new_memory = {
        "object": data['object'],
        "description": data['description'],
        "timestamp": data['timestamp'],
        "user": session['user']['username']
    }
    memories.append(new_memory)
    save_json(MEMORY_FILE, memories)
    return jsonify({"status": "success"})

@app.route('/api/memory/clear', methods=['POST'])
def api_memory_clear():
    if 'user' not in session:
        return "Unauthorized", 401
    
    memories = load_json(MEMORY_FILE)
    memories = [m for m in memories if m.get('user') != session['user']['username']]
    save_json(MEMORY_FILE, memories)
    return jsonify({"status": "cleared"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
