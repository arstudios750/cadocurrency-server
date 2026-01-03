from flask import Flask, request, jsonify, Response
import hashlib
import json
import os
import time
import secrets
from threading import Lock

app = Flask(__name__)

# Where we store the blockchain and user accounts
CHAIN_FILE = "chain.json"
USERS_FILE = "users.json"

# Basic block reward economics
HALVING_INTERVAL = 1000
INITIAL_REWARD = 4.0
MIN_REWARD = 0.25

# Difficulty settings:
#  - 75   = easiest
#  - 100  = medium
#  - 120  = hardest
# Bigger number = harder, smaller = easier.
MIN_DIFFICULTY = 75
DEFAULT_DIFFICULTY = 100
MAX_DIFFICULTY = 120

# We want each miner to find a valid share roughly this often
TARGET_SHARE_TIME = 5  # seconds

# Per-miner difficulty and timing
miner_difficulty = {}   # miner_id -> current difficulty
last_share_time = {}    # miner_id -> last accepted share timestamp

# Pending transactions get collected here
mempool = []

# Simple in-memory session store: token -> username
sessions = {}

# Lock for touching the chain file safely from multiple requests
chain_lock = Lock()


# ---------------------- Helpers: hashing, storage ---------------------- #

def sha1_hex(data: str) -> str:
    return hashlib.sha1(data.encode()).hexdigest()


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def load_chain():
    # If there is no chain yet, start with a tiny genesis chain
    if not os.path.exists(CHAIN_FILE):
        genesis = [{
            "index": 0,
            "timestamp": 0,
            "transactions": [],
            "prev_hash": "0",
            "nonce": 0,
            "hash": "0",
            "miner": "genesis"
        }]
        with open(CHAIN_FILE, "w") as f:
            json.dump(genesis, f, indent=2)
        return genesis
    with open(CHAIN_FILE, "r") as f:
        return json.load(f)


def save_chain(chain):
    with open(CHAIN_FILE, "w") as f:
        json.dump(chain, f, indent=2)


def load_users():
    # Very lightweight "user database" as a JSON dict
    # Structure:
    # {
    #   "email1": {"email": ..., "username": ..., "password_hash": ...},
    #   "email2": {...}
    # }
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w") as f:
            json.dump({}, f)
        return {}
    with open(USERS_FILE, "r") as f:
        return json.load(f)


def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def get_height(chain):
    return len(chain) - 1


def compute_balances(chain):
    # No separate state machine here; balances are derived from transactions
    balances = {}
    for block in chain:
        for tx in block.get("transactions", []):
            sender = tx["from"]
            receiver = tx["to"]
            amount = float(tx["amount"])
            if sender != "network":
                balances[sender] = balances.get(sender, 0.0) - amount
            balances[receiver] = balances.get(receiver, 0.0) + amount
    return balances


def get_block_reward(height):
    # Simple halvings every HALVING_INTERVAL blocks
    halvings = height // HALVING_INTERVAL
    reward = INITIAL_REWARD / (2 ** halvings)
    return max(reward, MIN_REWARD)


def difficulty_to_threshold(diff: int) -> int:
    """
    Convert a difficulty 75–120 into a numeric threshold over the first
    6 hex digits of the SHA-1 hash.

    We look at hash[:6] as a hex number, so the raw space is:
        0 .. 16**6 - 1 = 0 .. 16,777,215

    Rules we want:
        - Smaller difficulty  (e.g. 75)  = easier  = higher threshold
        - Bigger difficulty   (e.g. 120) = harder  = lower threshold
    """
    min_d = MIN_DIFFICULTY
    max_d = MAX_DIFFICULTY
    max_raw = 16**6 - 1  # 16,777,215

    # You can tweak these if you want to change how "spiky" difficulty feels
    easy_threshold = int(max_raw * 0.9)   # very forgiving
    hard_threshold = int(max_raw * 0.2)   # quite strict

    if diff < min_d:
        diff = min_d
    if diff > max_d:
        diff = max_d

    # Linear interpolation:
    # diff = min_d -> x = 0 -> easy_threshold
    # diff = max_d -> x = 1 -> hard_threshold
    x = (diff - min_d) / (max_d - min_d)
    threshold = int(easy_threshold + (hard_threshold - easy_threshold) * x)
    return threshold


def get_user_from_token(request):
    """Extract the username from the Authorization header, if the token is valid."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    return sessions.get(token)


# ---------------------- Web miner (HTML + JS) ---------------------- #

WEB_MINER_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Cadocurrency Web Miner</title>
  <style>
    body { font-family: Arial, sans-serif; background:#111; color:#eee; text-align:center; }
    .box { max-width:700px; margin:40px auto; padding:20px; border-radius:8px; background:#1c1c1c; }
    input { padding:8px; border-radius:4px; border:none; margin:4px; width:100%; max-width:260px; }
    button { padding:8px 16px; border-radius:4px; border:none; background:#3a8fff; color:white; cursor:pointer; margin:4px; }
    button:disabled { background:#555; cursor:default; }
    .stat { margin:6px 0; text-align:left; }
    code { color:#9f9; word-break:break-all; }
    .row { margin:8px 0; }
    .flex { display:flex; flex-wrap:wrap; gap:10px; justify-content:center; }
    label { display:block; text-align:left; margin-bottom:4px; font-size:0.9rem; }
    hr { border:none; border-bottom:1px solid #333; margin:18px 0; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Cadocurrency Web Miner</h1>
    <p>First register, then log in, then mine directly from your browser.</p>

    <div class="flex">
      <div>
        <h2>Register</h2>
        <div class="row">
          <label for="reg_email">Email</label>
          <input id="reg_email" placeholder="you@example.com">
        </div>
        <div class="row">
          <label for="reg_username">Username (your miner ID)</label>
          <input id="reg_username" placeholder="coolminer123">
        </div>
        <div class="row">
          <label for="reg_password">Password</label>
          <input id="reg_password" type="password" placeholder="password">
        </div>
        <button id="registerBtn">Register</button>
        <div id="registerStatus"></div>
      </div>

      <div>
        <h2>Login</h2>
        <div class="row">
          <label for="login_email">Email</label>
          <input id="login_email" placeholder="you@example.com">
        </div>
        <div class="row">
          <label for="login_password">Password</label>
          <input id="login_password" type="password" placeholder="password">
        </div>
        <button id="loginBtn">Login</button>
        <div id="loginStatus"></div>
      </div>
    </div>

    <hr>

    <div>
      <h2>Miner</h2>
      <div class="row">Logged in as: <span id="currentUser">-</span></div>
      <button id="startBtn" disabled>Start Mining</button>
      <button id="stopBtn" disabled>Stop</button>
    </div>

    <div style="margin-top:15px; text-align:left;">
      <div class="stat">Status: <span id="status">Idle</span></div>
      <div class="stat">Hashrate: <span id="hashrate">0</span> H/s</div>
      <div class="stat">Accepted shares: <span id="shares">0</span></div>
      <div class="stat">Block height: <span id="height">-</span></div>
      <div class="stat">Your balance: <span id="balance">0</span> CADO</div>
      <div class="stat">Current difficulty: <span id="diff">-</span></div>
      <div class="stat">Last block hash: <code id="lasthash"></code></div>
    </div>
  </div>

<script>
let mining = false;
let token = null;
let username = null;
let shares = 0;
let hashrate = 0;

function setStatus(id, text, color) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.style.color = color || "#eee";
}

document.getElementById("registerBtn").onclick = async () => {
  const email = document.getElementById("reg_email").value.trim();
  const user = document.getElementById("reg_username").value.trim();
  const pass = document.getElementById("reg_password").value;

  if (!email || !user || !pass) {
    setStatus("registerStatus", "Please fill all fields.", "orange");
    return;
  }

  try {
    const res = await fetch("/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, username: user, password: pass })
    });
    const data = await res.json();
    if (data.success) {
      setStatus("registerStatus", "Registered successfully. You can now log in.", "lightgreen");
    } else {
      setStatus("registerStatus", data.error || "Registration failed.", "red");
    }
  } catch (e) {
    console.log(e);
    setStatus("registerStatus", "Request failed.", "red");
  }
};

document.getElementById("loginBtn").onclick = async () => {
  const email = document.getElementById("login_email").value.trim();
  const pass = document.getElementById("login_password").value;

  if (!email || !pass) {
    setStatus("loginStatus", "Please fill both fields.", "orange");
    return;
  }

  try {
    const res = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password: pass })
    });
    const data = await res.json();
    if (data.success) {
      token = data.token;
      username = data.username;
      document.getElementById("currentUser").textContent = username;
      setStatus("loginStatus", "Logged in.", "lightgreen");
      document.getElementById("startBtn").disabled = false;
    } else {
      setStatus("loginStatus", data.error || "Login failed.", "red");
    }
  } catch (e) {
    console.log(e);
    setStatus("loginStatus", "Request failed.", "red");
  }
};

document.getElementById("startBtn").onclick = () => {
  if (!token || !username) {
    setStatus("status", "You must log in first.", "orange");
    return;
  }
  mining = true;
  shares = 0;
  document.getElementById("shares").textContent = "0";
  setStatus("status", "Starting miner...");
  document.getElementById("startBtn").disabled = true;
  document.getElementById("stopBtn").disabled = false;
  mineLoop();
};

document.getElementById("stopBtn").onclick = () => {
  mining = false;
  setStatus("status", "Stopped");
  document.getElementById("startBtn").disabled = false;
  document.getElementById("stopBtn").disabled = true;
};

async function getJob() {
  const res = await fetch("/get_job", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer " + token
    },
    body: JSON.stringify({})
  });
  return res.json();
}

async function submitShare(seed, nonce) {
  const res = await fetch("/submit_share", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer " + token
    },
    body: JSON.stringify({ seed: seed, nonce: nonce })
  });
  return res.json();
}

async function mineLoop() {
  while (mining) {
    try {
      setStatus("status", "Requesting job...");
      const job = await getJob();
      if (!job || job.error) {
        setStatus("status", job.error || "Failed to get job", "red");
        await new Promise(r => setTimeout(r, 2000));
        continue;
      }

      const seed = job.seed;
      const difficulty = job.difficulty;
      const threshold = job.threshold;
      const height = job.height;

      document.getElementById("height").textContent = height;
      document.getElementById("diff").textContent = difficulty;
      setStatus("status", "Mining...");

      let nonce = 0n;
      let start = performance.now();
      let hashes = 0n;

      while (mining) {
        const msg = new TextEncoder().encode(seed + nonce.toString());
        const digest = await crypto.subtle.digest("SHA-1", msg);
        const hashArray = Array.from(new Uint8Array(digest));
        const hex = hashArray.map(b => b.toString(16).padStart(2, "0")).join("");

        // First 6 hex digits, just like the server
        const shortVal = parseInt(hex.slice(0, 6), 16);
        hashes++;

        // Only bother the server when it looks like a valid share locally
        if (shortVal < threshold) {
          const res = await submitShare(seed, nonce.toString());
          if (res.accepted) {
            shares++;
            document.getElementById("shares").textContent = shares;
            document.getElementById("balance").textContent = res.balance.toFixed(4);
            document.getElementById("lasthash").textContent = res.hash;
            setStatus(
              "status",
              "Share accepted (diff " + res.difficulty + ", " + res.share_time.toFixed(2) + "s)",
              "lightgreen"
            );
          } else {
            setStatus("status", "Share rejected: " + (res.error || "unknown reason"), "orange");
          }
          break;
        }

        nonce++;

        if (hashes % 2000n === 0n) {
          const elapsed = (performance.now() - start) / 1000;
          if (elapsed > 0) {
            hashrate = Number(hashes) / elapsed;
            document.getElementById("hashrate").textContent = Math.floor(hashrate);
          }
        }
      }
    } catch (e) {
      console.log(e);
      setStatus("status", "Error, retrying in 3s...", "red");
      await new Promise(r => setTimeout(r, 3000));
    }
  }
}
</script>
</body>
</html>
"""


@app.route("/")
def webminer():
    return Response(WEB_MINER_HTML, mimetype="text/html")


# ---------------------- Auth: register + login ---------------------- #

@app.route("/register", methods=["POST"])
def register():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    username = (data.get("username") or "").strip()
    password = data.get("password")

    if not email or not username or not password:
        return jsonify({"success": False, "error": "Missing email, username, or password"}), 400

    # Very basic checks to avoid silly spam
    if "@" not in email or "." not in email:
        return jsonify({"success": False, "error": "Invalid email"}), 400
    if len(username) < 3:
        return jsonify({"success": False, "error": "Username too short"}), 400
    if len(password) < 4:
        return jsonify({"success": False, "error": "Password too short"}), 400

    users = load_users()

    # Make sure username and email are unique
    for u in users.values():
        if u["username"].lower() == username.lower():
            return jsonify({"success": False, "error": "Username already taken"}), 400
    if email in users:
        return jsonify({"success": False, "error": "Email already registered"}), 400

    password_hash = sha256_hex(password)

    users[email] = {
        "email": email,
        "username": username,
        "password_hash": password_hash
    }
    save_users(users)

    return jsonify({"success": True})


@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")

    if not email or not password:
        return jsonify({"success": False, "error": "Missing email or password"}), 400

    users = load_users()
    user = users.get(email)
    if not user:
        return jsonify({"success": False, "error": "Invalid email or password"}), 400

    password_hash = sha256_hex(password)
    if password_hash != user["password_hash"]:
        return jsonify({"success": False, "error": "Invalid email or password"}), 400

    # Simple random token. For a demo project this is fine.
    token = secrets.token_hex(32)
    sessions[token] = user["username"]

    return jsonify({"success": True, "token": token, "username": user["username"]})


# ---------------------- Mining + chain API ---------------------- #

@app.route("/get_job", methods=["POST"])
def get_job():
    miner_id = get_user_from_token(request)
    if miner_id is None:
        return jsonify({"error": "Unauthorized"}), 401

    with chain_lock:
        chain = load_chain()
        last_block = chain[-1]
        height = get_height(chain)
        reward = get_block_reward(height + 1)

        current_diff = miner_difficulty.get(miner_id, DEFAULT_DIFFICULTY)
        threshold = difficulty_to_threshold(current_diff)
        seed = last_block["hash"]

    return jsonify({
        "seed": seed,
        "difficulty": current_diff,
        "threshold": threshold,
        "height": height + 1,
        "reward": reward
    })


@app.route("/submit_share", methods=["POST"])
def submit_share():
    miner_id = get_user_from_token(request)
    if miner_id is None:
        return jsonify({"accepted": False, "error": "Unauthorized"}), 401

    data = request.json or {}
    seed = data.get("seed")
    nonce = data.get("nonce")

    if seed is None or nonce is None:
        return jsonify({"accepted": False, "error": "Missing seed or nonce"}), 400

    try:
        nonce = int(nonce)
    except ValueError:
        return jsonify({"accepted": False, "error": "Invalid nonce"}), 400

    with chain_lock:
        chain = load_chain()
        last_block = chain[-1]

        # If the miner worked on an outdated job, we don't accept it
        if seed != last_block["hash"]:
            return jsonify({"accepted": False, "error": "Stale job"}), 400

        height = get_height(chain) + 1
        current_diff = miner_difficulty.get(miner_id, DEFAULT_DIFFICULTY)
        threshold = difficulty_to_threshold(current_diff)

        h = sha1_hex(seed + str(nonce))

        # Use first 6 hex digits for difficulty, DUCO-style but with a 6-digit range
        hash_val = int(h[:6], 16)

        if hash_val >= threshold:
            return jsonify({"accepted": False, "error": "Invalid share"}), 400

        # At this point the share is good enough to become the next block
        reward = get_block_reward(height)
        coinbase_tx = {"from": "network", "to": miner_id, "amount": reward}

        balances = compute_balances(chain)
        valid_txs = [coinbase_tx]

        global mempool
        new_mempool = []
        for tx in mempool:
            sender = tx["from"]
            amount = float(tx["amount"])
            if balances.get(sender, 0.0) >= amount:
                balances[sender] = balances.get(sender, 0.0) - amount
                balances[tx["to"]] = balances.get(tx["to"], 0.0) + amount
                valid_txs.append(tx)
            else:
                # If the sender is broke, leave the tx in the mempool for later
                new_mempool.append(tx)
        mempool = new_mempool

        new_block = {
            "index": height,
            "timestamp": time.time(),
            "transactions": valid_txs,
            "prev_hash": last_block["hash"],
            "nonce": nonce,
            "hash": h,
            "miner": miner_id
        }

        chain.append(new_block)
        save_chain(chain)

        balances = compute_balances(chain)
        miner_balance = balances.get(miner_id, 0.0)

        # Adaptive difficulty: try to keep the miner around TARGET_SHARE_TIME
        now = time.time()
        prev_time = last_share_time.get(miner_id, now)
        elapsed = now - prev_time if prev_time != now else TARGET_SHARE_TIME
        last_share_time[miner_id] = now

        # Faster than target? Increase difficulty (harder).
        # Slower than target? Decrease difficulty (easier).
        if elapsed < TARGET_SHARE_TIME:
            current_diff += 1
        else:
            current_diff -= 1

        if current_diff < MIN_DIFFICULTY:
            current_diff = MIN_DIFFICULTY
        if current_diff > MAX_DIFFICULTY:
            current_diff = MAX_DIFFICULTY

        miner_difficulty[miner_id] = current_diff

    return jsonify({
        "accepted": True,
        "hash": h,
        "height": height,
        "reward": reward,
        "balance": miner_balance,
        "difficulty": current_diff,
        "share_time": elapsed
    })


@app.route("/balance/me")
def my_balance():
    miner_id = get_user_from_token(request)
    if miner_id is None:
        return jsonify({"error": "Unauthorized"}), 401

    with chain_lock:
        chain = load_chain()
        balances = compute_balances(chain)
        bal = balances.get(miner_id, 0.0)
    return jsonify({"miner": miner_id, "balance": bal})


@app.route("/send", methods=["POST"])
def send():
    miner_id = get_user_from_token(request)
    if miner_id is None:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.json or {}
    receiver = data.get("to")
    amount = data.get("amount")

    if not receiver or amount is None:
        return jsonify({"success": False, "error": "Missing fields"}), 400

    try:
        amount = float(amount)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid amount"}), 400

    if amount <= 0:
        return jsonify({"success": False, "error": "Amount must be positive"}), 400

    with chain_lock:
        chain = load_chain()
        balances = compute_balances(chain)
        if balances.get(miner_id, 0.0) < amount:
            return jsonify({"success": False, "error": "Insufficient balance"}), 400

        tx = {"from": miner_id, "to": receiver, "amount": amount}
        mempool.append(tx)

    return jsonify({"success": True})


@app.route("/chain")
def get_chain():
    with chain_lock:
        chain = load_chain()
    return jsonify(chain)


if __name__ == "__main__":
    # For local dev. In production, something like gunicorn will usually wrap this.
    app.run(host="0.0.0.0", port=8080)
