from flask import Flask, request, jsonify
import hashlib
import time
import random

app = Flask(__name__)

difficulty = 2**240
block_reward = 4.0
balances = {}

@app.route("/get_job", methods=["POST"])
def get_job():
    miner = request.json.get("miner_id", "unknown")
    seed = str(random.getrandbits(128))
    return jsonify({
        "job_id": seed,
        "seed": seed,
        "difficulty": str(difficulty)
    })

@app.route("/submit_share", methods=["POST"])
def submit_share():
    miner = request.json["miner_id"]
    seed = request.json["seed"]
    nonce = request.json["nonce"]

    h = hashlib.sha1((seed + str(nonce)).encode()).hexdigest()
    h_int = int(h, 16)

    if h_int < difficulty:
        balances[miner] = balances.get(miner, 0) + block_reward
        return jsonify({"accepted": True, "reward": block_reward})
    else:
        return jsonify({"accepted": False})

@app.route("/balance/<miner>")
def balance(miner):
    return jsonify({"miner": miner, "balance": balances.get(miner, 0)})

@app.route("/")
def home():
    return "Cadocurrency mining server is running!"