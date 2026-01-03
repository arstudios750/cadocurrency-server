from flask import Flask, request, jsonify, Response
import hashlib
import json
import os
import time
from threading import Lock

app = Flask(__name__)

CHAIN_FILE = "chain.json"

# Emission params
HALVING_INTERVAL = 1000
INITIAL_REWARD = 4.0
MIN_REWARD = 0.25

# Per-user difficulty (Pi vs supercomputer fairness)
miner_difficulty = {}
last_share_time = {}

TARGET_SHARE_TIME = 10         # seconds per share per user
MIN_DIFFICULTY = 2**100
MAX_DIFFICULTY = 2**150
DEFAULT_DIFFICULTY = 2**130

chain_lock = Lock()
mempool = []  # pending txs


def sha1_hex(data: str) -> str:
    return hashlib.sha1(data.encode()).hexdigest()


def load_chain():
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


def compute_balances(chain):
    balances = {}
    for block in chain:
        for tx in block.get("transactions", []):
            sender = tx["from"]
            receiver = tx["to"]
            amount = float(tx["amount"])
            if sender != "network":
                balances[sender] = balances.get(sender, 0) - amount
            balances[receiver] = balances.get(receiver, 0) + amount
    return balances


def get_height(chain):
    return len(chain) - 1


def get_block_reward(height):
    halvings = height // HALVING_INTERVAL
    reward = INITIAL_REWARD / (2 ** halvings)
    return max(reward, MIN_REWARD)


# ---------- Web UI (web miner) ----------

WEB_MINER_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Cadocurrency Web Miner</title>
  <style>
    body { font-family: Arial, sans-serif; background:#111; color:#eee; text-align:center; }
    .box { max-width:600px; margin:40px auto; padding:20px; border-radius:8px; background:#1c1c1c; }
    input { padding:8px; border-radius:4px; border:none; margin:4px; }
    button { padding:8px 16px; border-radius:4px; border:none; background:#3a8fff; color:white; cursor:pointer; }
    button:disabled { background:#555; cursor:default; }
    .stat { margin:6px 0; }
    code { color:#9f9; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Cadocurrency Web Miner</h1>
    <p>Mine with just your <b>username</b>, like DUCO.</p>
    <div>
      <input id="username" placeholder="Enter your username">
      <button id="startBtn">Start Mining</button>
      <button id="stopBtn" disabled>Stop</button>
    </div>
    <div style="margin-top:15px; text-align:left;">
      <div class="stat">Status: <span id="status">Idle</span></div>
      <div class="stat">Hashrate: <span id="hashrate">0</span> H/s</div>
      <div class="stat">Accepted shares: <span id="shares">0</span></div>
      <div class="stat">Last block height: <span id="height">-</span></div>
      <div class="stat">Your balance: <span id="balance">0</span> CADO</div>
      <div class="stat">Last block hash: <code id="lasthash"></code></div>
    </div>
  </div>

<script>
let mining = false;
let username = "";
let shares = 0;
let hashrate = 0;

document.getElementById("startBtn").onclick = () => {
  username = document.getElementById("username").value.trim();
  if (!username) {
    alert("Enter a username first.");
    return;
  }
  mining = true;
  shares = 0;
  document.getElementById("shares").textContent = "0";
  document.getElementById("status").textContent = "Starting...";
  document.getElementById("startBtn").disabled = true;
  document.getElementById("stopBtn").disabled = false;
  mineLoop();
};

document.getElementById("stopBtn").onclick = () => {
  mining = false;
  document.getElementById("status").textContent = "Stopped";
  document.getElementById("startBtn").disabled = false;
  document.getElementById("stopBtn").disabled = true;
};

async function getJob() {
  const res = await fetch("/get_job", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ miner_id: username })
  });
  return res.json();
}

async function submitShare(seed, nonce) {
  const res = await fetch("/submit_share", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ miner_id: username, seed: seed, nonce: nonce })
  });
  return res.json();
}

async function mineLoop() {
  while (mining) {
    try {
      document.getElementById("status").textContent = "Requesting job...";
      const job = await getJob();
      let seed = job.seed;
      let difficulty = BigInt(job.difficulty);
      let height = job.height;

      document.getElementById("height").textContent = height;
      document.getElementById("status").textContent = "Mining...";

      let nonce = 0n;
      let start = performance.now();
      let hashes = 0n;

      while (mining) {
        // hash(seed + nonce) using SHA-1 (Web Crypto)
        let msg = new TextEncoder().encode(seed + nonce.toString());
        let digest = await crypto.subtle.digest("SHA-1", msg);
        let hashArray = Array.from(new Uint8Array(digest));
        let hex = hashArray.map(b => b.toString(16).padStart(2, "0")).join("");

        // convert hash to BigInt
        let val = BigInt("0x" + hex);
        hashes++;

        if (val < difficulty) {
          const res = await submitShare(seed, nonce.toString());
          if (res.accepted) {
            shares++;
            document.getElementById("shares").textContent = shares;
            document.getElementById("balance").textContent = res.balance.toFixed(4);
            document.getElementById("lasthash").textContent = res.hash;
            document.getElementById("status").textContent = "Share accepted!";
          } else {
            document.getElementById("status").textContent = "Share rejected: " + (res.error || "");
          }
          break;
        }

        nonce++;

        if (hashes % 2000n === 0n) {
          let elapsed = (performance.now() - start) / 1000;
          if (elapsed > 0) {
            hashrate = Number(hashes) / elapsed;
            document.getElementById("hashrate").textContent = Math.floor(hashrate);
          }
        }
      }
    } catch (e) {
      console.log(e);
      document.getElementById("status").textContent = "Error, retrying in 3s...";
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


# ---------- API / blockchain ----------

@app.route("/get_job", methods=["POST"])
def get_job():
    data = request.json or {}
    miner_id = data.get("miner_id", "unknown")

    with chain_lock:
        chain = load_chain()
        last_block = chain[-1]
        height = get_height(chain)
        reward = get_block_reward(height + 1)
        difficulty = miner_difficulty.get(miner_id, DEFAULT_DIFFICULTY)
        seed = last_block["hash"]

    return jsonify({
        "seed": seed,
        "difficulty": str(difficulty),
        "height": height + 1,
        "reward": reward
    })


@app.route("/submit_share", methods=["POST"])
def submit_share():
    data = request.json or {}
    miner_id = data.get("miner_id")
    seed = data.get("seed")
    nonce = data.get("nonce")

    if miner_id is None or seed is None or nonce is None:
        return jsonify({"accepted": False, "error": "Missing miner_id, seed, or nonce"}), 400

    try:
        nonce = int(nonce)
    except ValueError:
        return jsonify({"accepted": False, "error": "Invalid nonce"}), 400

    with chain_lock:
        chain = load_chain()
        last_block = chain[-1]

        if seed != last_block["hash"]:
            return jsonify({"accepted": False, "error": "Stale job"}), 400

        height = get_height(chain) + 1
        diff = miner_difficulty.get(miner_id, DEFAULT_DIFFICULTY)

        h = sha1_hex(seed + str(nonce))
        if int(h, 16) >= diff:
            return jsonify({"accepted": False, "error": "Invalid share"}), 400

        reward = get_block_reward(height)
        coinbase_tx = {"from": "network", "to": miner_id, "amount": reward}

        balances = compute_balances(chain)
        valid_txs = [coinbase_tx]

        global mempool
        new_mempool = []
        for tx in mempool:
            sender = tx["from"]
            amount = float(tx["amount"])
            if balances.get(sender, 0) >= amount:
                balances[sender] = balances.get(sender, 0) - amount
                balances[tx["to"]] = balances.get(tx["to"], 0) + amount
                valid_txs.append(tx)
            else:
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

        # Adaptive difficulty update
        now = time.time()
        prev_time = last_share_time.get(miner_id, now)
        elapsed = now - prev_time if prev_time != now else TARGET_SHARE_TIME
        last_share_time[miner_id] = now

        current_diff = miner_difficulty.get(miner_id, DEFAULT_DIFFICULTY)
        if elapsed < TARGET_SHARE_TIME:
            current_diff *= 1.2
        else:
            current_diff *= 0.8
        current_diff = max(MIN_DIFFICULTY, min(current_diff, MAX_DIFFICULTY))
        miner_difficulty[miner_id] = current_diff

    return jsonify({
        "accepted": True,
        "hash": h,
        "height": height,
        "reward": reward,
        "balance": miner_balance,
        "difficulty": str(current_diff),
        "share_time": elapsed
    })


@app.route("/balance/<miner_id>")
def balance(miner_id):
    with chain_lock:
        chain = load_chain()
        balances = compute_balances(chain)
        bal = balances.get(miner_id, 0.0)
    return jsonify({"miner": miner_id, "balance": bal})


@app.route("/send", methods=["POST"])
def send():
    data = request.json or {}
    sender = data.get("from")
    receiver = data.get("to")
    amount = data.get("amount")

    if not sender or not receiver or amount is None:
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
        if balances.get(sender, 0) < amount:
            return jsonify({"success": False, "error": "Insufficient balance"}), 400

        tx = {"from": sender, "to": receiver, "amount": amount}
        mempool.append(tx)

    return jsonify({"success": True})


@app.route("/chain")
def get_chain():
    with chain_lock:
        chain = load_chain()
    return jsonify(chain)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
