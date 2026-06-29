import os
import hashlib
import secrets
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, session
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")

# ---------- DB connection ----------
def get_db():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "minigpay"),
        user=os.environ.get("DB_USER", "gpayuser"),
        password=os.environ.get("DB_PASS", "gpaypass"),
        cursor_factory=RealDictCursor,
    )

# ---------- Auth helper ----------
def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return salt, hashed

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not logged in"}), 401
        return f(*args, **kwargs)
    return wrapper

# ---------- Routes ----------
@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json()
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    password = data.get("password", "")

    if not name or not phone or not password:
        return jsonify({"error": "name, phone and password required"}), 400

    salt, hashed = hash_password(password)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (name, phone, password_hash, salt) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, phone, hashed, salt),
        )
        user_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (%s, 0)", (user_id,)
        )
        conn.commit()
        return jsonify({"message": "Registered successfully", "user_id": user_id}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Phone number already registered"}), 409
    finally:
        conn.close()

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    phone = data.get("phone", "").strip()
    password = data.get("password", "")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, salt, password_hash FROM users WHERE phone = %s", (phone,))
        user = cur.fetchone()
        if not user:
            return jsonify({"error": "Invalid credentials"}), 401

        _, hashed = hash_password(password, user["salt"])
        if hashed != user["password_hash"]:
            return jsonify({"error": "Invalid credentials"}), 401

        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        return jsonify({"message": "Login successful", "name": user["name"]})
    finally:
        conn.close()

@app.route("/api/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})

@app.route("/api/balance")
@login_required
def balance():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (session["user_id"],))
        wallet = cur.fetchone()
        return jsonify({"balance": float(wallet["balance"]), "name": session["user_name"]})
    finally:
        conn.close()

@app.route("/api/add-money", methods=["POST"])
@login_required
def add_money():
    data = request.get_json()
    amount = data.get("amount", 0)

    if not isinstance(amount, (int, float)) or amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400
    if amount > 100000:
        return jsonify({"error": "Max single deposit is 1,00,000"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE wallets SET balance = balance + %s WHERE user_id = %s RETURNING balance",
            (amount, session["user_id"]),
        )
        new_balance = cur.fetchone()["balance"]
        cur.execute(
            "INSERT INTO transactions (sender_id, receiver_id, amount, type, note) VALUES (%s, %s, %s, 'credit', 'Added to wallet')",
            (session["user_id"], session["user_id"], amount),
        )
        conn.commit()
        return jsonify({"message": f"Added ₹{amount}", "balance": float(new_balance)})
    finally:
        conn.close()

@app.route("/api/send-money", methods=["POST"])
@login_required
def send_money():
    data = request.get_json()
    receiver_phone = data.get("phone", "").strip()
    amount = data.get("amount", 0)
    note = data.get("note", "")

    if not receiver_phone:
        return jsonify({"error": "Receiver phone required"}), 400
    if not isinstance(amount, (int, float)) or amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT id, name FROM users WHERE phone = %s", (receiver_phone,))
        receiver = cur.fetchone()
        if not receiver:
            return jsonify({"error": "Receiver not found"}), 404
        if receiver["id"] == session["user_id"]:
            return jsonify({"error": "Cannot send money to yourself"}), 400

        cur.execute("SELECT balance FROM wallets WHERE user_id = %s FOR UPDATE", (session["user_id"],))
        sender_wallet = cur.fetchone()
        if float(sender_wallet["balance"]) < amount:
            return jsonify({"error": "Insufficient balance"}), 400

        cur.execute("UPDATE wallets SET balance = balance - %s WHERE user_id = %s", (amount, session["user_id"]))
        cur.execute("UPDATE wallets SET balance = balance + %s WHERE user_id = %s", (amount, receiver["id"]))

        cur.execute(
            "INSERT INTO transactions (sender_id, receiver_id, amount, type, note) VALUES (%s, %s, %s, 'transfer', %s)",
            (session["user_id"], receiver["id"], amount, note or f"Sent to {receiver['name']}"),
        )
        conn.commit()
        return jsonify({"message": f"Sent ₹{amount} to {receiver['name']}"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/transactions")
@login_required
def transactions():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.id, t.sender_id, t.amount, t.type, t.note, t.created_at,
                   s.name as sender_name, r.name as receiver_name
            FROM transactions t
            JOIN users s ON t.sender_id = s.id
            JOIN users r ON t.receiver_id = r.id
            WHERE t.sender_id = %s OR t.receiver_id = %s
            ORDER BY t.created_at DESC
            LIMIT 50
        """, (session["user_id"], session["user_id"]))
        txns = cur.fetchall()

        result = []
        for t in txns:
            if t["type"] == "credit" and t["sender_id"] == session["user_id"]:
                direction = "credit"
            elif t["sender_id"] == session["user_id"]:
                direction = "debit"
            else:
                direction = "credit"
            result.append({
                "id": t["id"],
                "amount": float(t["amount"]),
                "direction": direction,
                "note": t["note"],
                "sender": t["sender_name"],
                "receiver": t["receiver_name"],
                "date": t["created_at"].isoformat(),
            })
        return jsonify({"transactions": result})
    finally:
        conn.close()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
