# -*- coding: utf-8 -*-
import os, json, threading, time, sys, glob
from pbft_utils import json_server, json_send, short_uuid

HOST = "127.0.0.1"
PRIMARY_PORT = 5000

participants = {}      # id -> (host, port)
clients = set()        # (host, port)
tx_log = {}            # txid -> {"status","data","commit_started":bool}
current_tx = None

prepare_votes = {}     # txid -> {pid: vote}
commit_votes = {}      # txid -> {pid: vote}

# When P0 is a replica in a view, we allow it to prepare/ack as well:
pending_prepare_tx = None
state_data = {}

crashed = False
view = 0
current_primary = "P0"

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
    msg = {"type":"MEMBERS","members": {"P0": (HOST, PRIMARY_PORT), **participants}}
    broadcast(msg)

def broadcast_clients(obj):
    for (h, p) in list(clients):
        try:
            json_send(h, p, obj)
        except Exception:
            pass

def banner():
    print("✓ P0 started at localhost:%d" % PRIMARY_PORT)
    print("="*60)
    print("\nCommands:")
    print("  list         - list participants")
    print("  status       - show leader/members/tx history & balances")
    print("  tx           - start a new tx (only if P0 is leader)")
    print("  progress     - drive consensus (two-step decision; abort if thresholds not met)")
    print("  prepare yes/no   - when P0 is a replica, cast PREPARE vote")
    print("  ack commit/abort - when P0 is a replica, cast COMMIT_VOTE")
    print("  view change  - broadcast VIEW_CHANGE (next primary issues NEW_VIEW on 2f+1)")
    print("  checkpoint   - coordinate distributed checkpoint (only if P0 is leader)")
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
    if current_primary != "P0":
        print(f"× Current leader is {current_primary}; this console cannot start a tx")
        return
    if not participants:
        print("× No participants; cannot start a tx")
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
    print("Hint: replicas should run 'prepare yes' or 'prepare no' on their consoles.")

def evaluate_prepare(txid):
    votes = prepare_votes.get(txid, {})
    yes = sum(1 for v in votes.values() if v.upper() == "VOTE_YES")
    _, q = compute_f_and_quorum()
    if votes:
        print("Replica PREPARE votes collected:")
        for pid in sorted(votes.keys()):
            print(f"  - {pid}: {votes[pid]}")
    else:
        print("Replica PREPARE votes: none")
    # Display adjustment ONLY (logic unchanged): include primary's implicit vote
    yes_disp = yes + 1
    total_nodes = len(participants) + 1
    threshold_disp = q + 1
    print(f"→ Prepare votes: {yes_disp}/{total_nodes} (threshold ≥ {threshold_disp})")
    return yes, q

def do_commit_phase(txid):
    if tx_log.get(txid,{}).get("commit_started"):
        print("\n[Phase 3/4] COMMIT in progress, waiting for replicas' COMMIT_VOTE ...")
        return
    tx_log[txid]["commit_started"] = True
    print("\n[Phase 3/4] COMMIT")
    print("-"*60)
    print("→ Leader announces COMMIT (replicas should run 'ack commit/abort' to broadcast COMMIT_VOTE)")

def evaluate_commit(txid):
    acks = commit_votes.get(txid, {})
    yes = sum(1 for v in acks.values() if v.upper()=="ACK_COMMIT")
    _, q = compute_f_and_quorum()
    if acks:
        print("Replica COMMIT votes collected:")
        for pid in sorted(acks.keys()):
            print(f"  - {pid}: {acks[pid]}")
    else:
        print("Replica COMMIT votes: none")
    # Display adjustment ONLY (logic unchanged): include primary's implicit vote
    yes_disp = yes + 1
    total_nodes = len(participants) + 1
    threshold_disp = q + 1
    print(f"→ Commit acks: {yes_disp}/{total_nodes} (threshold ≥ {threshold_disp})")
    return yes, q

def _op_to_signed_amount(data):
    acct = data.get("account")
    amt = data.get("amount")
    op = str(data.get("operation", "deposit")).lower()
    is_deposit = op in ("deposit", "depoist")
    if acct is None or amt is None:
        return None, None
    try:
        v = int(str(amt))
    except Exception:
        return None, None
    return acct, v if is_deposit else -v if op == "withdraw" else None

def balances_from_committed():
    bal = {}
    for tid, info in tx_log.items():
        if info.get("status") == "COMMITTED":
            data = info.get("data") or {}
            acct, val = _op_to_signed_amount(data)
            if acct is None or val is None:
                continue
            bal[acct] = bal.get(bal, 0) + val
    return bal

def status_print():
    print("="*60)
    print(f"View: {view}")
    print(f"Current leader: {current_primary}")
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
    print("-"*60)
    # balance section (optional, derive from tx_log like demo)
    print("Balances: (demo based on COMMITTED tx):")
    bal = {}
    for tid, info in tx_log.items():
        if info.get("status") == "COMMITTED":
            data = info.get("data") or {}
            acct = data.get("account"); amt=data.get("amount")
            try:
                amt = int(str(amt))
            except Exception:
                amt = None
            if acct and amt is not None:
                op = str(data.get("operation","deposit")).lower()
                val = amt if op in ("deposit","depoist") else (-amt if op=="withdraw" else 0)
                bal[acct]=bal.get(acct,0)+val
    if not bal:
        print("  (empty)")
    else:
        for k,v in bal.items():
            print(f"  {k}: {v}")
    print("="*60)

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
    # balances are derived in status_print; omit here
    return "\n".join(lines) + "\n"

def write_local_checkpoint_file(text):
    ensure_dir("checkpoints")
    path = os.path.join("checkpoints", "P0_checkpoints.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + "\n")

def load_latest_final_checkpoint():
    global last_final_checkpoint_path
    ensure_dir("checkpoints")
    files = sorted(glob.glob(os.path.join("checkpoints", "final_checkpoint_*.log")))
    if files:
        last_final_checkpoint_path = files[-1]
        try:
            with open(last_final_checkpoint_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    return ""

def finalize(txid, commit=True):
    tx = tx_log.get(txid)
    if not tx: return
    if commit:
        tx["status"] = "COMMITTED"
        print("\n[Phase 4/4] Reply")
        print("-"*60)
        print("→ Broadcast REPLY to all members (and forward to clients)")
        msg = {"type":"REPLY","txid":txid,"result":"COMMITTED","data":tx["data"],"from":current_primary}
        broadcast(msg)
        broadcast_clients(msg)
        print("\n"+"="*60); print(f"✓ Tx {txid} committed!"); print("="*60)
    else:
        tx["status"] = "ABORTED"
        print("\n"+"="*60); print(f"✗ Tx {txid} aborted"); print("="*60)
        msg = {"type":"ABORT","txid":txid,"from":current_primary}
        broadcast(msg)

def handle_recover_request(msg):
    # Send latest checkpoint text + state to recovering node
    text = load_latest_final_checkpoint()
    dest_h, dest_p = msg.get("host"), msg.get("port")
    # Build a simple state_data (committed tx -> data)
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
        "primary_host": HOST,
        "primary_port": PRIMARY_PORT,
        "tx_log": tx_log,
        "state_data": sdata,
    }
    if dest_h and dest_p:
        json_send(dest_h, dest_p, payload)
        print(f"\n→ Sent latest checkpoint/state to recovering node {dest_h}:{dest_p}")

def on_msg(msg, addr):
    global crashed, view, current_primary, pending_prepare_tx
    if crashed: return
    t = msg.get("type")
    if t == "REGISTER":
        pid = msg["id"]; h=msg["host"]; p=msg["port"]
        participants[pid]=(h,p)
        print(f"\n✓ Participant registered: {pid} ({h}:{p})")
        broadcast_membership()
    elif t == "CLIENT_HELLO":
        clients.add((msg["host"], msg["port"]))
        print(f"\n✓ Client connected: {msg['host']}:{msg['port']}")
        broadcast({"type":"CLIENT_JOIN","host": msg["host"], "port": msg["port"]})
    elif t == "PREPARE":
        txid = msg["txid"]; pid = msg["from"]; vote = msg["vote"]
        prepare_votes.setdefault(txid, {})[pid]=vote
        print(f"\n→ PREPARE from {pid}: {vote}")
    elif t == "COMMIT_VOTE":
        txid = msg["txid"]; pid=msg["from"]; ack=msg["ack"]
        commit_votes.setdefault(txid, {})[pid]=ack
        print(f"\n← COMMIT_VOTE from {pid}: {ack} (tx {txid})")
    elif t == "PRE_PREPARE":
        # When P0 is not the leader, it acts as a replica
        txid = msg["txid"]; data = msg["data"]
        if current_primary != "P0":
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
        broadcast_clients(msg)
        print(f"  Data: {state_data.get(txid)}")
    elif t == "VIEW_CHANGE":
        sender = msg.get("from")
        req_view = view + 1  # requested next
        print(f"\n→ VIEW_CHANGE from {sender} (request view={req_view})")
        target = next_primary_id()  # deterministic next primary
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
                           "members": {"P0": (HOST, PRIMARY_PORT), **participants}})
                view = newv
                current_primary = "P0"
                print(f"✓ Reached {need} votes; I am the new leader: view={view}, current_primary=P0")
                # Re-exec aborted/unfinished tx if any
                reexec_unfinished_as_leader()
    elif t == "NEW_VIEW":
        newv = msg["new_view"]; leader = msg["from"]
        view = newv; current_primary = leader
        print(f"\n✓ NEW_VIEW received: view={view}, new leader={current_primary}")
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
                # write in member order for readability
                for nid in ["P0"] + sorted(participants.keys()):
                    txt = checkpoint_reports[cid].get(nid)
                    if txt:
                        f.write(txt + ("\n" if not txt.endswith("\n") else ""))
            print(f"✓ Final checkpoint assembled: {final_path}")
    elif t == "RECOVER_HELLO":
        handle_recover_request(msg)
    print("P0> ", end="", flush=True)

def reexec_unfinished_as_leader():
    # restart any tx that is not COMMITTED (including ABORTED/STARTED), rebroadcast PRE_PREPARE
    pending = [tid for tid,info in tx_log.items() if info.get("status")!="COMMITTED"]
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
        elif cmd == "status":
            status_print()
        elif cmd == "tx":
            if current_primary != "P0":
                print(f"× Current leader is {current_primary}; this console cannot start a tx")
                continue
            data = input("Enter tx data (key=value, e.g., account=alice,amount=100,operation=deposit):\ndata> ").strip()
            start_tx(data)
        elif cmd == "progress":
            if current_primary != "P0":
                print("× I am not the leader; cannot run progress here")
                continue
            if not current_tx:
                print("× No ongoing tx")
                continue
            # A: Prepare decision
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
            # B: Commit decision (based on COMMIT_VOTE)
            cy, cq = evaluate_commit(current_tx)
            if cy >= cq:
                finalize(current_tx, commit=True)
            else:
                print("Commit threshold not satisfied; aborting the tx.")
                finalize(current_tx, commit=False)
            current_tx = None
        elif cmd.startswith("prepare "):
            # allow P0 to act as a replica in non-leader views
            if current_primary == "P0":
                print("× I am the leader; replicas should vote, not me")
                continue
            if not pending_prepare_tx:
                print("× No pending tx to vote on (PRE_PREPARE not received yet)")
            else:
                choice = cmd.split()[-1].lower()
                vote = "VOTE_YES" if choice in ("yes","y") else "VOTE_NO"
                for pid,(h,p) in participants.items():
                    json_send(h,p,{"type":"PREPARE","from":"P0","txid":pending_prepare_tx,"vote":vote})
                # also inform P0 itself for local logs (optional)
                print(f"✓ PREPARE broadcast by P0 as replica: {vote} (tx={pending_prepare_tx})")
                pending_prepare_tx = None
        elif cmd.startswith("ack "):
            # allow P0 to ack as a replica in non-leader views
            if current_primary == "P0":
                print("× I am the leader; replicas should ack, not me")
                continue
            choice = cmd.split()[-1].lower()
            ack = "ACK_COMMIT" if choice == "commit" else "ACK_ABORT"
            txid = None
            if tx_log: txid = list(tx_log.keys())[-1]
            if not txid:
                print("× No tx to ack")
            else:
                for pid,(h,p) in participants.items():
                    json_send(h,p,{"type":"COMMIT_VOTE","from":"P0","txid":txid,"ack":ack})
                print("✓ Broadcast", ack, "(by P0 as replica)")
        elif cmd == "crash":
            crashed = True; print("! Primary crashes (will ignore incoming messages)")
        elif cmd == "recover":
            crashed = False; print("✓ Primary recovered")
        elif cmd == "view change":
            print(f"→ Broadcast VIEW_CHANGE (current view={view}, next leader={next_primary_id()})")
            for pid,(h,p) in participants.items():
                json_send(h,p,{"type":"VIEW_CHANGE","from":"P0"})
            # also count self vote (only useful if next leader is P0)
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

if __name__ == "__main__":
    threading.Thread(target=server, daemon=True).start()
    repl()

