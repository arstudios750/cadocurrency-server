server.py: from flask import Flask, request, jsonify
import hashlib
import json
import os
import time
from threading import Lock

app = Flask(__name__)

CHAIN_FILE = "chain.json"

# Blockchain / emission params
START_DIFFICULTY = 2**130      # tune up/down
HALVING_INTERVAL = 1000        # blocks per halving
INITIAL_REWARD = 4.0           # CADO per block
MIN_REWARD = 0.25              # floor reward

chain_lock = Lock()
mempool = []  # pending transactions


def sha1_hex(data: str) -> str:
    return hashlib.sha1(data.encode()).hexdigest()


def load_chain():
    if not os.path.exists(CHAIN_FILE):
        # create genesis if missing
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
    if reward < MIN_REWARD:
        reward = MIN_REWARD
    return reward


def get_difficulty(height):
    # Simple fixed difficulty for now; later you can adjust by height
    return START_DIFFICULTY


@app.route("/")
def home():
    return "Cadocurrency blockchain node is running!"


@app.route("/get_job", methods=["POST"])
def get_job():
    """
    Miner asks for work.
    We give:
      - seed: last block hash
      - difficulty
      - height
      - reward
    Miner will search for nonce s.t. sha1(seed + nonce) < difficulty
    """
    with chain_lock:
        chain = load_chain()
        last_block = chain[-1]
        height = get_height(chain)
        difficulty = get_difficulty(height + 1)
        reward = get_block_reward(height + 1)
        seed = last_block["hash"]

    return jsonify({
        "seed": seed,
        "difficulty": str(difficulty),
        "height": height + 1,
        "reward": reward
    })


@app.route("/submit_share", methods=["POST"])
def submit_share():
    """
    Miner submits:
      - miner_id
      - seed (last hash they used)
      - nonce
    Server checks PoW and, if valid, builds a block with:
      - coinbase tx (network -> miner)
      - any valid mempool txs
    """
    data = request.json
    miner_id = data.get("miner_id")
    seed = data.get("seed")
    nonce = int(data.get("nonce"))

    if not miner_id or seed is None:
        return jsonify({"accepted": False, "error": "Missing miner_id or seed"}), 400

    with chain_lock:
        chain = load_chain()
        last_block = chain[-1]

        # ensure they mined on the current tip
        if seed != last_block["hash"]:
            return jsonify({"accepted": False, "error": "Stale job"}), 400

        height = get_height(chain) + 1
        difficulty = get_difficulty(height)

        # check PoW
        h = sha1_hex(seed + str(nonce))
        if int(h, 16) >= difficulty:
            return jsonify({"accepted": False, "error": "Invalid share"}), 400

        # build coinbase tx
        reward = get_block_reward(height)
        coinbase_tx = {
            "from": "network",
            "to": miner_id,
            "amount": reward
        }

        # load balances to filter mempool txs
        balances = compute_balances(chain)

        valid_txs = []
        # include coinbase at top
        valid_txs.append(coinbase_tx)

        # try to include mempool txs (very naive)
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
                # keep tx in mempool if not enough balance yet
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

        # recompute miner balance to return
        balances = compute_balances(chain)
        miner_balance = balances.get(miner_id, 0.0)

    return jsonify({
        "accepted": True,
        "hash": h,
        "height": height,
        "reward": reward,
        "balance": miner_balance
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
    """
    Create a normal transaction to be mined later.
    This just adds to mempool; actual balance change happens when a block is mined.
    Body:
    {
      "from": "alice",
      "to": "bob",
      "amount": 5
    }
    """
    data = request.json
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
    # dev mode; on Render you use gunicorn with Procfile
    app.run(host="0.0.0.0", port=8080)
