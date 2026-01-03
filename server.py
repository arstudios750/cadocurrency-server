from flask import Flask, request, jsonify, Response
import hashlib
import json
import os
import time
from threading import Lock

app = Flask(__name__)

# Where the blockchain is stored on disk
CHAIN_FILE = "chain.json"

# Very simple block reward schedule
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

# How often (on average) a miner should find a valid share
TARGET_SHARE_TIME = 5  # seconds

# Per-miner state
miner_difficulty = {}   # miner_id -> current difficulty
last_share_time = {}    # miner_id -> last accepted share time

# Pending transactions sit here until they get picked up by a block
mempool = []

# We touch the chain file from multiple requests, so use a lock
chain_lock = Lock()


def sha1_hex(data: str) -> str:
    return hashlib.sha1(data.encode()).hexdigest()


def load_chain():
    # If there's no chain yet, spin up a tiny genesis chain
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


def get_height(chain):
    return len(chain) - 1


def compute_balances(chain):
    # No separate database, just derive balances from the chain itself
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
    # Simple halving schedule: every HALVING_INTERVAL blocks, reward halves
    halvings = height // HALVING_INTERVAL
    reward = INITIAL_REWARD / (2 ** halvings)
    return max(reward, MIN_REWARD)


def difficulty_to_threshold(diff: int) -> int:
    """
    Convert a difficulty 75–120 into a numeric threshold on the first 5 hex
    digits of the SHA-1 hash.

    We look at hash[:6] as a hex number, so the raw range is:
        0 .. 16**6 - 1 = 0 

    Rules we want:
        - Smaller difficulty  (e.g. 75)  = easier  = higher threshold
        - Bigger difficulty   (e.g. 120) = harder  = lower threshold

    So we map:
        diff = 75  -> threshold near the top of the range
        diff = 120 -> threshold much lower, harder to hit
    """
    min_d = MIN_DIFFICULTY
    max_d = MAX_DIFFICULTY
    max_raw = 16**6 - 1  

    # These endpoints are somewhat arbitrary, but behave nicely in practice
    # easy_threshold: pretty forgiving
    easy_threshold = int(max_raw * 0.9)
    # hard_threshold: noticeably stricter
    hard_threshold = int(max_raw * 0.2)

    if diff < min_d:
        diff = min_d
    if diff > max_d:
        diff = max_d

    # Linearly interpolate with diff across [min_d, max_d]
    # diff = min_d -> x = 0 -> easy_threshold
    # diff = max_d -> x = 1 -> hard_threshold
    x = (diff - min_d) / (max_d - min_d)
    threshold = int(easy_threshold + (hard_threshold - easy_threshold) * x)
    return threshold


# ---------------------- Web miner UI ---------------------- #

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
    code { color:#9f9; word-break:break-all; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Cadocurrency Web Miner</h1>
    <p>One username, one browser tab, no wallet files. Just mine.</p>
    <div>
      <input id="username" placeholder="Enter your username">
      <button id="startBtn">Start Mining</button>
      <button id="stopBtn" disabled>Stop</button>
    </div>
    <div style="margin-top:15px; text-align:left;">
      <div class="stat">Status: <span id="status">Idle</span></div>
      <div class="stat">Hashrate: <span id="hashrate">0</span> H/s</div>
      <div class="stat">Accepted shares: <span id="shares">0</span></div>
      <div class="stat">Block height: <span id="height">-</span></div>
      <div class="stat">Your balance: <span id="balance">0</span> CADO</div>
      <div class="stat">Last block hash: <code id="lasthash"></code></div>
      <div class="stat">Current difficulty: <span id="diff">-</span></div>
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
      let difficulty = job.difficulty;
      let threshold = job.threshold;
      let height = job.height;

      document.getElementById("height").textContent = height;
      document.getElementById("diff").textContent = difficulty;
      document.getElementById("status").textContent = "Mining...";

      let nonce = 0n;
      let start = performance.now();
      let hashes = 0n;

      while (mining) {
        const msg = new TextEncoder().encode(seed + nonce.toString());
        const digest = await crypto.subtle.digest("SHA-1", msg);
        const hashArray = Array.from(new Uint8Array(digest));
        const hex = hashArray.map(b => b.toString(16).padStart(2, "0")).join("");

        // First 6 hex digits, same as the server
        const shortVal = parseInt(hex.slice(0, 6), 16);
        hashes++;

        // We only bother the server when it looks like a valid share locally
        if (shortVal < threshold) {
          const res = await submitShare(seed, nonce.toString());
          if (res.accepted) {
            shares++;
            document.getElementById("shares").textContent = shares;
            document.getElementById("balance").textContent = res.balance.toFixed(4);
            document.getElementById("lasthash").textContent = res.hash;
            document.getElementById("status").textContent =
              "Share accepted (diff " + res.difficulty + ", " + res.share_time.toFixed(2) + "s)";
          } else {
            document.getElementById("status").textContent =
              "Share rejected: " + (res.error || "unknown reason");
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


# ---------------------- Mining + chain API ---------------------- #

@app.route("/get_job", methods=["POST"])
def get_job():
    data = request.json or {}
    miner_id = data.get("miner_id", "anonymous")

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

        # If the miner is working on an old seed, reject early
        if seed != last_block["hash"]:
            return jsonify({"accepted": False, "error": "Stale job"}), 400

        height = get_height(chain) + 1
        current_diff = miner_difficulty.get(miner_id, DEFAULT_DIFFICULTY)
        threshold = difficulty_to_threshold(current_diff)

        h = sha1_hex(seed + str(nonce))

        # DUCO-style: only look at the first 5 hex digits for difficulty
        hash_val = int(h[:5], 16)

        if hash_val >= threshold:
            return jsonify({"accepted": False, "error": "Invalid share"}), 400

        # At this point the share is good enough to become a block
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

        # Adaptive difficulty: look at how long this share took
        now = time.time()
        prev_time = last_share_time.get(miner_id, now)
        elapsed = now - prev_time if prev_time != now else TARGET_SHARE_TIME
        last_share_time[miner_id] = now

        # If the miner is faster than our target, crank difficulty up (harder).
        # If they're slower, ease difficulty down.
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
        if balances.get(sender, 0.0) < amount:
            return jsonify({"success": False, "error": "Insufficient balance"}), 400

        # We don't mine the tx into a block immediately, we just queue it
        tx = {"from": sender, "to": receiver, "amount": amount}
        mempool.append(tx)

    return jsonify({"success": True})


@app.route("/chain")
def get_chain():
    with chain_lock:
        chain = load_chain()
    return jsonify(chain)


if __name__ == "__main__":
    # Local dev entrypoint; in production you'll probably use gunicorn
    app.run(host="0.0.0.0", port=8080)

