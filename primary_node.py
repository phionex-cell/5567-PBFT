# -*- coding: utf-8 -*-
import os, json, threading, time, sys, glob, random
from pbft_utils import json_server, json_send, short_uuid

HOST = "127.0.0.1"
PRIMARY_PORT = 5000

participants = {}      # id -> (host, port)
clients = set()        # (host, port)
tx_log = {}            # txid -> {"status","data","commit_started":bool}
current_tx = None
self_prepare_vote = {}  # txid -> "VOTE_YES" | "VOTE_NO"
self_commit_vote  = {}
prepare_votes = {}     # txid -> {pid: vote}
commit_votes = {}      # txid -> {pid: ack}

pending_prepare_tx = None
state_data = {}

crashed = False
view = 0
current_primary = "P0"
waiting_sync = False
# Byzantine config: chosen once when we have P0..P3 (total 4 nodes)
byzantine_id = None

# View-change
vc_votes = {}          # view -> set(node_ids) collected at next primary only
vc_done_for_view = set()

# checkpoint (store last final)
checkpoint_reports = {}
checkpoint_expected = {}
last_final_checkpoint_path = None

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def compute_f_and_quorum():
    N = 1 + len(participants)    # primary + backups
    f = max(0, (N - 1)//3)
    quorum_2f = 2*f              # count of *other* nodes' votes
    return f, quorum_2f

def all_ids():
    return ["P0"] + sorted(participants.keys())

def next_primary_id_from(leader):
    ids = all_ids()
    if leader not in ids:
        return "P0"
    idx = (ids.index(leader) + 1) % len(ids)
    return ids[idx]

def next_primary_id():
    return next_primary_id_from(current_primary)

def broadcast(obj):
    for pid, (h, p) in participants.items():
        try:
            json_send(h, p, obj)
        except Exception:
            pass

def broadcast_membership():
    msg = {
        "type":"MEMBERS",
        "members": {"P0": (HOST, PRIMARY_PORT), **participants},
        "view": view,
        "leader": current_primary,
        "byzantine_id": byzantine_id,
    }
    broadcast(msg)

def broadcast_clients(obj):
    for (h, p) in list(clients):
        try:
            json_send(h, p, obj)
            print(h, p, obj)
        except Exception:
            pass

def maybe_choose_byzantine():
    global byzantine_id
    # 固定将 P3 设为拜占庭；当 P3 完成注册后设置并广播
    if byzantine_id is None and "P3" in participants:
        byzantine_id = "P3"
        print(f"\n★ Byzantine node selected: {byzantine_id}")
        broadcast_membership()

def banner():
    print("✓ P0 started at localhost:%d" % PRIMARY_PORT)
    print("="*60)
    print("\nCommands:")
    print("  list         - list participants")
    print("  status       - show leader/members/tx history & balances")
    print("  tx           - start a new tx (any node may run progress)")
    print("  progress     - evaluate votes/acks and possibly finalize")
    print("  prepare yes|no                - P0 votes as replica (when not leader)")
    print("  prepare to <PID> yes|no       - Byzantine *targeted* PREPARE to one node")
    print("  ack commit|abort              - P0 votes as replica (when not leader)")
    print("  ack to <PID> commit|abort     - Byzantine *targeted* COMMIT_VOTE to one node")
    print("  view change  - broadcast VIEW_CHANGE")
    print("  checkpoint   - coordinate distributed checkpoint (leader only)")
    print("  crash / recover / quit")
    print("\nP0> ", end="", flush=True)

def parse_kv(s):
    out = {}
    for pair in s.split(","):
        if not pair.strip():
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out

def start_tx(data_str):
    global current_tx
    if not participants:
        print("× No participants; cannot start a tx")
        return

    data = parse_kv(data_str)
    op = str(data.get("operation", "")).lower()
    acct = data.get("account", None)
    amt = data.get("amount", None)
    if op not in ("deposit", "withdraw"):
        print("× Invalid operation. Only 'deposit' or 'withdraw' are allowed.")
        return
    if not acct:
        print("× Missing 'account'.")
        return
    try:
        int(str(amt))
    except Exception:
        print("× 'amount' must be an integer.")
        return
    txid = short_uuid()
    current_tx = txid
    tx_log[txid] = {"status":"STARTED","data":parse_kv(data_str),"commit_started":False}
    print("="*60)
    print(f"New tx: {txid}")
    print(f"Data: {tx_log[txid]['data']}")
    print(f"Total members: {len(participants)+1}")
    print("="*60)
    print("\n[Phase 1/4] Pre-prepare")
    print("-"*60)
    for pid, (h,p) in participants.items():
        print(f"← send PRE-PREPARE to {pid}... ")
        json_send(h,p,{"type":"PRE_PREPARE","txid":txid,"data":tx_log[txid]["data"],
                       "from":current_primary,"primary_host":HOST,"primary_port":PRIMARY_PORT})
    print("\n[Phase 2/4] Prepare (manual)")
    print("-"*60)
    print("Hint: replicas should run 'prepare yes' or 'prepare no'. Byzantine can target specific nodes.")

def evaluate_prepare(txid):
    votes = prepare_votes.get(txid, {})
    yes = sum(1 for v in votes.values() if v.upper() == "VOTE_YES")
    MY_ID = "P0"
    is_leader = (current_primary == MY_ID)

    self_yes = 1 if is_leader else (1 if self_prepare_vote.get(txid, "").upper() == "VOTE_YES" else 0)

    f, _ = compute_f_and_quorum()
    threshold = 2 * f + 1

    yes_total = yes + self_yes
    N = 1 + len(participants)

    print(f"→ Prepare YES(total): {yes_total}/{N}  (threshold ≥ {threshold})")
    return yes_total, threshold

def do_commit_phase(txid):
    if tx_log.get(txid,{}).get("commit_started"):
        print("\n[Phase 3/4] COMMIT in progress, awaiting COMMIT_VOTE ...")
        return
    tx_log[txid]["commit_started"] = True
    tx_log[txid]["status"] = "PREPARED"
    print("\n[Phase 3/4] COMMIT")
    print("-"*60)
    print("→ Entered COMMIT phase (replicas: 'ack commit' or 'ack abort').")

def evaluate_commit(txid):
    acks = commit_votes.get(txid, {})
    yes = sum(1 for v in acks.values() if v.upper()=="ACK_COMMIT")

    MY_ID = "P0"
    is_leader = (current_primary == MY_ID)

    self_yes = 1 if is_leader else (1 if self_commit_vote.get(txid, "").upper() == "ACK_COMMIT" else 0)

    f, _ = compute_f_and_quorum()
    threshold = 2 * f + 1

    yes_total = yes + self_yes
    N = 1 + len(participants)

    print(f"→ Commit ACK_COMMIT(total): {yes_total}/{N}  (threshold ≥ {threshold})")
    return yes_total, threshold

def finalize(txid, commit=True):
    tx = tx_log.get(txid)
    if not tx: return
    if commit:
        tx["status"] = "COMMITTED"
        print("\n[Phase 4/4] Reply")
        print("-"*60)
        print("→ Broadcast REPLY to clients")
        msg = {"type":"REPLY","txid":txid,"result":"COMMITTED","data":tx["data"],"from":current_primary}
        broadcast_clients(msg)
        print("\n"+"="*60); print(f"✓ Tx {txid} committed!"); print("="*60)
    else:
        tx["status"] = "ABORTED"
        print("\n" + "=" * 60);
        print(f"✗ Tx {txid} aborted");
        print("=" * 60)
        fail_reply = {"type": "REPLY", "txid": txid, "result": "ABORTED", "data": tx.get("data"),
                      "from": current_primary}
        broadcast_clients(fail_reply)
        # msg = {"type": "ABORT", "txid": txid, "from": current_primary}
        # broadcast(msg)

def handle_recover_request(msg):
    # Send latest checkpoint text + state to recovering node
    text = load_latest_final_checkpoint()
    dest_h, dest_p = msg.get("host"), msg.get("port")
    sdata = {}
    for tid, info in tx_log.items():
        if info.get("status") == "COMMITTED":
            sdata[tid] = info.get("data")
    payload = {
        "type":"CHECKPOINT_SYNC",
        "text": text,
        "view": view,
        "current_primary": current_primary,
        "members": {"P0": (HOST, PRIMARY_PORT), **participants},
        "byzantine_id": byzantine_id,
        "primary_host": HOST,
        "primary_port": PRIMARY_PORT,
        "tx_log": tx_log,
        "state_data": sdata,
    }
    if dest_h and dest_p:
        json_send(dest_h, dest_p, payload)
        print(f"\n→ Sent latest checkpoint/state to recovering node {dest_h}:{dest_p}")

def on_msg(msg, addr):
    global crashed, view, current_primary, pending_prepare_tx, byzantine_id
    if crashed: return
    t = msg.get("type")
    if t == "REGISTER":
        pid = msg["id"]; h=msg["host"]; p=msg["port"]
        participants[pid]=(h,p)
        print(f"\n✓ Participant registered: {pid} ({h}:{p})")
        maybe_choose_byzantine()
        broadcast_membership()
    elif t == "CLIENT_HELLO":
        clients.add((msg["host"], msg["port"]))
        print(f"\n✓ Client connected: {msg['host']}:{msg['port']}")
        broadcast({"type":"CLIENT_JOIN","host": msg["host"], "port": msg["port"]})
    elif t == "CLIENT_TX":
        raw = msg.get("data")
        src_port = msg.get("from_port")
        print(f"\n→ CLIENT_TX received from {addr[0]}:{src_port}: {raw}")
    elif t == "PREPARE":
        txid = msg["txid"]; pid = msg["from"]; vote = msg["vote"]
        prepare_votes.setdefault(txid, {})[pid]=vote
        print(f"\n→ PREPARE from {pid}: {vote}")
    elif t == "COMMIT_VOTE":
        txid = msg["txid"]; pid=msg["from"]; ack=msg["ack"]
        commit_votes.setdefault(txid, {})[pid]=ack
        print(f"\n← COMMIT_VOTE from {pid}: {ack} (tx {txid})")
    elif t == "PRE_PREPARE":
        # When P0 is not the leader, it can act as a replica
        txid = msg["txid"]; data = msg["data"]
        print(f"\n→ Received PRE-PREPARE (tx {txid}) from {msg.get('from')}")
        print("  ✓ Waiting for manual vote: run 'prepare yes' or 'prepare no'")
        pending_prepare_tx = txid
        tx_log.setdefault(txid, {"status":"STARTED","data":data,"commit_started":False})
    elif t == "ABORT":
        txid = msg["txid"]
        print(f"\n→ ABORT received (tx {txid})")
        tx_log.setdefault(txid, {"status":"ABORTED","data":state_data.get(txid)})
        tx_log[txid]["status"]="ABORTED"
        state_data.pop(txid, None)
    elif t == "REPLY":
        txid = msg["txid"]
        print(f"\n→ REPLY received (tx {txid})")
        state_data[txid] = msg.get("data")
        tx_log.setdefault(txid, {"status":"COMMITTED","data":state_data[txid]})
        tx_log[txid]["status"]="COMMITTED"
        # broadcast_clients(msg)
        print(f"  Data: {state_data.get(txid)}")
    elif t == "VIEW_CHANGE":
        sender = msg.get("from")
        req_view = view + 1
        print(f"\n→ VIEW_CHANGE from {sender} (request view={req_view})")
        target = next_primary_id()
        if target == "P0" and view not in vc_done_for_view:
            vc_votes.setdefault(view, set()).add(sender)
            vc_votes[view].add("P0")
            f,_ = compute_f_and_quorum()
            need = 2*f+1
            if len(vc_votes[view]) >= need:
                newv = view + 1
                vc_done_for_view.add(view)
                broadcast({"type":"NEW_VIEW","new_view": newv, "from":"P0",
                           "primary_host": HOST, "primary_port": PRIMARY_PORT,
                           "members": {"P0": (HOST, PRIMARY_PORT), **participants},
                           "byzantine_id": byzantine_id})
                view = newv
                current_primary = "P0"
                print(f"✓ Reached {need} votes; I am the new leader: view={view}, current_primary=P0")
                reexec_unfinished_as_leader()
    elif t == "NEW_VIEW":
        newv = msg["new_view"]; leader = msg["from"]
        view = newv; current_primary = leader
        if "byzantine_id" in msg and msg["byzantine_id"]:
            byzantine_id = msg["byzantine_id"]
        print(f"\n✓ NEW_VIEW received: view={view}, new leader={current_primary} (Byzantine={byzantine_id})")
    elif t == "CHECKPOINT_REPORT":
        cid = msg.get("checkpoint_id")
        node_id = msg.get("node_id", "UNKNOWN")
        text = msg.get("text", "")
        checkpoint_reports.setdefault(cid, {})[node_id] = text
        expected = checkpoint_expected.get(cid, len(participants)+1)
        got = len(checkpoint_reports[cid])
        print(f"\n→ Received checkpoint report from {node_id} ({got}/{expected})")
        if got >= expected:
            ensure_dir("checkpoints")
            final_path = os.path.join("checkpoints", f"final_checkpoint_{cid}.log")
            with open(final_path, "w", encoding="utf-8") as f:
                for nid in ["P0"] + sorted(participants.keys()):
                    txt = checkpoint_reports[cid].get(nid)
                    if txt:
                        f.write(txt + ("\n" if not txt.endswith("\n") else ""))
            print(f"✓ Final checkpoint assembled: {final_path}")

    elif t == "RECOVER_HELLO":
        handle_recover_request(msg)
    print("P0> ", end="", flush=True)

def reexec_unfinished_as_leader():
    pending = [tid for tid,info in tx_log.items() if info.get("status") in ("STARTED", "PREPARED")]
    if not pending:
        return
    tid = pending[-1]
    info = tx_log[tid]
    if not info.get("data"):
        return
    print(f"→ As the leader, restart unfinished tx {tid} (from Pre-prepare)")
    for pid,(h,p) in participants.items():
        json_send(h,p,{"type":"PRE_PREPARE","txid":tid,"data":info["data"],"from":current_primary,
                       "primary_host":HOST,"primary_port":PRIMARY_PORT})
    print("✓ PRE-PREPARE rebroadcasted")

def load_latest_final_checkpoint():
    ensure_dir("checkpoints")
    files = sorted(glob.glob(os.path.join("checkpoints", "final_checkpoint_*.log")))
    if files:
        try:
            with open(files[-1], "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    return ""

def server():
    json_server(HOST, PRIMARY_PORT, on_msg, on_ready=banner)

def repl():
    global crashed, current_tx, view, pending_prepare_tx
    while True:
        try:
            cmd = input("P0> ").strip()
        except (EOFError, KeyboardInterrupt):
            cmd = "quit"
        if not cmd: continue
        if cmd == "list":
            if participants:
                print(f"Participants ({len(participants)}):")
                for pid, (h,p) in sorted(participants.items()):
                    print(f"  - {pid} ({h}:{p})")
            else:
                print("(empty)")
            if byzantine_id:
                print(f"Byzantine node: {byzantine_id}")
            else:
                print("Byzantine node: (not chosen yet)")
        elif cmd == "status":
            print("="*60)
            print(f"View: {view}")
            print(f"Current leader: {current_primary}")
            print(f"Byzantine node: {byzantine_id}")
            roles = []
            for nid in ["P0"] + sorted(participants.keys()):
                role = "Leader" if nid == current_primary else "Replica"
                roles.append(f"{nid}({role})")
            print("Members: " + ", ".join(roles))
            print("-"*60)
            if not tx_log:
                print("Tx history: (empty)")
            else:
                print("Tx history:")
                for tid, info in tx_log.items():
                    print(f"  {tid}: {info.get('status','UNKNOWN')} - {info.get('data')}")
                balances = {}
                for tid, info in tx_log.items():
                    if info.get("status") == "COMMITTED":
                        data = info.get("data", {})
                        acct = data.get("account")
                        amt = data.get("amount")
                        op = str(data.get("operation", "")).lower()
                        try:
                            val = int(str(amt))
                        except Exception:
                            continue
                        if acct:
                            if op == "withdraw":
                                val = -val
                            balances[acct] = balances.get(acct, 0) + val
                print("-" * 60)
                if balances:
                    print("Account Balances:")
                    for acct, bal in balances.items():
                        print(f"  {acct}: {bal}")
                else:
                    print("Account Balances: (none committed yet)")
            print("="*60)
        elif cmd == "tx":
            data = input("Enter tx data (key=value, e.g., account=alice,amount=100,operation=deposit):\ndata> ").strip()
            start_tx(data)
        elif cmd == "progress":
            if not current_tx:
                pending = [tid for tid,info in tx_log.items() if info.get("status")!="COMMITTED"]
                current_tx = pending[-1] if pending else None
                if not current_tx:
                    print("× No ongoing tx"); continue
            py, pq = evaluate_prepare(current_tx)
            if not tx_log[current_tx].get("commit_started"):
                if py < pq:
                    print("Prepare not satisfied; aborting the tx.")
                    finalize(current_tx, commit=False)
                    current_tx = None
                    continue
                else:
                    print("\nPrepare threshold satisfied; entering COMMIT")
                    do_commit_phase(current_tx)
                    continue
            cy, cq = evaluate_commit(current_tx)
            if cy >= cq:
                finalize(current_tx, commit=True)
            else:
                print("Commit threshold not satisfied; aborting the tx.")
                finalize(current_tx, commit=False)
            current_tx = None
        elif cmd.startswith("prepare to "):
            # Byzantine targeted PREPARE
            parts = cmd.split()
            if len(parts) != 4 or parts[2] not in participants and parts[2] != "P0":
                print("Usage: prepare to <PID> yes|no")
                continue
            if byzantine_id != "P0":
                print("× Only the Byzantine node can send targeted votes"); continue
            if not pending_prepare_tx:
                print("× No pending tx to vote on"); continue
            pid = parts[2]; choice = parts[3].lower()
            vote = "VOTE_YES" if choice in ("yes","y") else "VOTE_NO"
            if pid == "P0":
                h,p = HOST, PRIMARY_PORT
            else:
                h,p = participants[pid]
            json_send(h,p,{"type":"PREPARE","from":"P0","txid":pending_prepare_tx,"vote":vote})
            print(f"✓ Targeted PREPARE sent to {pid}: {vote}")
        elif cmd.startswith("ack to "):
            parts = cmd.split()
            if len(parts) != 4 or parts[2] not in participants and parts[2] != "P0":
                print("Usage: ack to <PID> commit|abort")
                continue
            if byzantine_id != "P0":
                print("× Only the Byzantine node can send targeted acks"); continue
            txid = None
            if tx_log: txid = list(tx_log.keys())[-1]
            if not txid: print("× No tx to ack"); continue
            pid = parts[2]; choice = parts[3].lower()
            ack = "ACK_COMMIT" if choice == "commit" else "ACK_ABORT"
            if pid == "P0":
                h,p = HOST, PRIMARY_PORT
            else:
                h,p = participants[pid]
            json_send(h,p,{"type":"COMMIT_VOTE","from":"P0","txid":txid,"ack":ack})
            print(f"✓ Targeted COMMIT_VOTE sent to {pid}: {ack}")
        elif cmd.startswith("prepare "):
            if not pending_prepare_tx:
                print("× No pending tx to vote on (PRE_PREPARE not received yet)")
            else:
                choice = cmd.split()[-1].lower()
                vote = "VOTE_YES" if choice in ("yes","y") else "VOTE_NO"
                self_prepare_vote[pending_prepare_tx] = vote
                for pid,(h,p) in participants.items():
                    json_send(h,p,{"type":"PREPARE","from":"P0","txid":pending_prepare_tx,"vote":vote})
                print(f"✓ PREPARE broadcast by P0: {vote} (tx={pending_prepare_tx})")
                pending_prepare_tx = None
        elif cmd.startswith("ack "):
            choice = cmd.split()[-1].lower()
            ack = "ACK_COMMIT" if choice == "commit" else "ACK_ABORT"
            txid = None
            if tx_log: txid = list(tx_log.keys())[-1]
            if not txid:
                print("× No tx to ack")
            else:
                self_commit_vote[txid] = ack
                for pid,(h,p) in participants.items():
                    json_send(h,p,{"type":"COMMIT_VOTE","from":"P0","txid":txid,"ack":ack})
                print("✓ Broadcast", ack, "(by P0 as replica)")
        elif cmd == "crash":
            crashed = True; print("! Primary crashes (will ignore incoming messages)")
        elif cmd == "recover":
            crashed = False; print("✓ recovered")
        elif cmd == "view change":
            print(f"→ Broadcast VIEW_CHANGE (current view={view}, next leader={next_primary_id()})")
            for pid,(h,p) in participants.items():
                json_send(h,p,{"type":"VIEW_CHANGE","from":"P0"})
            if next_primary_id() == "P0":
                vc_votes.setdefault(view, set()).add("P0")
        elif cmd == "checkpoint":
            if current_primary != "P0":
                print("× Only the current leader can coordinate a distributed checkpoint")
                continue
            cid = time.strftime("%Y%m%d_%H%M%S")
            expected = len(participants)+1
            checkpoint_expected[cid] = expected
            checkpoint_reports[cid] = {}
            text = snapshot_text("P0")
            print("\n" + text)
            write_local_checkpoint_file(text)
            checkpoint_reports[cid]["P0"] = text
            for pid,(h,p) in participants.items():
                json_send(h,p,{"type":"CHECKPOINT_REQUEST","checkpoint_id": cid,
                               "collector_host": HOST, "collector_port": PRIMARY_PORT})
            print(f"→ Started distributed checkpoint (expecting {expected} reports)")
        elif cmd == "quit":
            print("Bye!"); time.sleep(0.2); break
        else:
            print("Unknown command")
    sys.exit(0)

def snapshot_text(node_id):
    import json, time
    lines = []
    lines.append(f"# Node {node_id} snapshot @ {time.strftime('%Y-%m-%d %H:%M:%S')} (view={view}, leader={current_primary})")
    if not tx_log:
        lines.append("Transactions: (empty)")
    else:
        lines.append("Transactions:")
        for tid, info in tx_log.items():
            lines.append(f"  - {tid}: {info.get('status','UNKNOWN')} {json.dumps(info.get('data'))}")
    return "\n".join(lines) + "\n"

def write_local_checkpoint_file(text):
    ensure_dir("checkpoints")
    path = os.path.join("checkpoints", "P0_checkpoints.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + "\n")

if __name__ == "__main__":
    threading.Thread(target=server, daemon=True).start()
    repl()
