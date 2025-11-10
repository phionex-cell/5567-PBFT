# -*- coding: utf-8 -*-
import os, sys, threading, time, json, glob
from pbft_utils import json_server, json_send, short_uuid

HOST = "127.0.0.1"
DEFAULT_PRIMARY_HOST, DEFAULT_PRIMARY_PORT = "127.0.0.1", 5000

id_ = None
port = None
crashed = False

members = {"P0": (DEFAULT_PRIMARY_HOST, DEFAULT_PRIMARY_PORT)}
current_primary = "P0"
primary_host, primary_port = DEFAULT_PRIMARY_HOST, DEFAULT_PRIMARY_PORT
view = 0

state_data = {}
tx_log = {}
current_tx = None

prepare_votes = {}
commit_votes = {}

pending_prepare_tx = None
clients = set()

# view change
vc_votes = {}
vc_done_for_view = set()

# checkpoint (collector side when this node is leader)
checkpoint_reports = {}
checkpoint_expected = {}
last_final_checkpoint_path = None

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def compute_f_and_quorum():
    N = len(members)
    f = max(0, (N - 1)//3)
    quorum_2f = 2*f
    return f, quorum_2f

def ids_sorted():
    others = sorted([k for k in members.keys() if k != "P0"])
    return ["P0"] + others

def next_primary_id_from(leader):
    ids = ids_sorted()
    if leader not in ids:
        return "P0"
    return ids[(ids.index(leader)+1) % len(ids)]

def next_primary_id():
    return next_primary_id_from(current_primary)

def banner():
    print(f"✓ Node '{id_}' started at localhost:{port}")
    try:
        json_send(DEFAULT_PRIMARY_HOST, DEFAULT_PRIMARY_PORT, {"type":"REGISTER","id":id_,"host":HOST,"port":port})
        print(f"✓ Registered to P0 {DEFAULT_PRIMARY_HOST}:{DEFAULT_PRIMARY_PORT}")
    except Exception:
        print("× Cannot register to P0; please make sure P0 is running")
    print("="*60)
    print("\nCommands:")
    print("  status               - show node/view/ledger/tx history")
    print("  data                 - show committed app data")
    print("  prepare yes/no       - (manual) broadcast PREPARE vote for the pending tx")
    print("  ack commit/abort     - (manual) broadcast COMMIT_VOTE")
    print("  crash / recover")
    print("  view change          - broadcast VIEW_CHANGE (next primary issues NEW_VIEW on 2f+1)")
    print("  checkpoint           - print local snapshot; if I am leader, coordinate distributed checkpoint")
    print("  # when I become primary: tx / progress")
    print("  quit")
    print(f"\n{id_}> ", end="", flush=True)

def announce_primary_capabilities():
    print(f"\n✓ current view={view}, primary={current_primary}")
    if current_primary == id_:
        print("→ I am the leader now: commands available: tx / progress / checkpoint")
        reexec_unfinished()

def reexec_unfinished():
    # restart any tx that is not COMMITTED (including ABORTED/STARTED), rebroadcast PRE_PREPARE
    pending = [tid for tid,info in tx_log.items() if info.get("status")!="COMMITTED"]
    if not pending:
        return
    tid = pending[-1]
    info = tx_log[tid]
    if not info.get("data") or info.get("rebroadcasted"):
        return
    print(f"→ Found unfinished tx {tid} (status={info.get('status')}), rebroadcasting PRE-PREPARE as the new leader")
    for pid,(h,p) in members.items():
        if pid == id_:
            continue
        json_send(h,p,{"type":"PRE_PREPARE","txid":tid,"data":info["data"],"from":current_primary,
                       "primary_host":primary_host,"primary_port":primary_port})
    info["rebroadcasted"] = True
    print("✓ PRE-PREPARE rebroadcasted")

def parse_kv(s):
    out = {}
    for pair in s.split(","):
        if not pair.strip(): continue
        if "=" in pair:
            k,v = pair.split("=",1)
            out[k.strip()] = v.strip()
    return out

def start_tx(data_str):
    global current_tx
    if current_primary != id_:
        print("× I am not the leader; cannot start a transaction"); return
    if len(members) <= 1:
        print("× No participants yet; cannot start a transaction"); return
    txid = short_uuid()
    current_tx = txid
    tx_log[txid] = {"status":"STARTED","data":parse_kv(data_str),"commit_started":False}
    print("="*60)
    print(f"New tx: {txid}")
    print(f"Data: {tx_log[txid]['data']}")
    print(f"Total members: {len(members)}")
    print("="*60)
    print("\n[Phase 1/4] Pre-prepare")
    print("-"*60)
    for pid,(h,p) in members.items():
        if pid == id_:
            continue
        print(f"← send PRE-PREPARE to {pid}... ")
        json_send(h,p,{"type":"PRE_PREPARE","txid":txid,"data":tx_log[txid]["data"],"from":current_primary,
                       "primary_host":primary_host,"primary_port":primary_port})
    print("\n[Phase 2/4] Prepare (manual)")
    print("-"*60)
    print("Hint: on each replica console, run 'prepare yes' or 'prepare no'.")

def evaluate_prepare(txid):
    votes = prepare_votes.get(txid, {})
    yes = sum(1 for v in votes.values() if v.upper()=="VOTE_YES")
    _, q = compute_f_and_quorum()
    # Display adjustment ONLY (logic unchanged): include primary's implicit vote
    yes_disp = yes + 1
    total_nodes = len(members)
    threshold_disp = q + 1
    print(f"→ Prepare votes: {yes_disp}/{total_nodes} (threshold ≥ {threshold_disp})")
    return yes, q

def do_commit_phase(txid):
    if tx_log.get(txid,{}).get("commit_started"):
        print("\n[Phase 3/4] COMMIT in progress, waiting for COMMIT_VOTE from replicas ...")
        return
    tx_log[txid]["commit_started"] = True
    print("\n[Phase 3/4] COMMIT")
    print("-"*60)
    print("→ Leader announces COMMIT (replicas please run 'ack commit' or 'ack abort' to broadcast COMMIT_VOTE)")

def evaluate_commit(txid):
    acks = commit_votes.get(txid, {})
    yes = sum(1 for v in acks.values() if v.upper()=="ACK_COMMIT")
    _, q = compute_f_and_quorum()
    # Display adjustment ONLY (logic unchanged): include primary's implicit vote
    yes_disp = yes + 1
    total_nodes = len(members)
    threshold_disp = q + 1
    print(f"→ Commit acks: {yes_disp}/{total_nodes} (threshold ≥ {threshold_disp})")
    return yes, q

def finalize(txid, commit=True):
    tx = tx_log.get(txid)
    if not tx: return
    if commit:
        tx["status"] = "COMMITTED"
        print("\n[Phase 4/4] Reply")
        print("-"*60)
        print("→ Broadcast REPLY to all members and forward to clients")
        msg = {"type":"REPLY","txid":txid,"result":"COMMITTED","data":tx["data"],"from":current_primary}
        for pid,(h,p) in members.items():
            if pid == id_:
                continue
            json_send(h,p,msg)
        for (h,p) in list(clients):
            json_send(h,p,msg)
        print("\n"+"="*60); print(f"✓ Tx {txid} committed!"); print("="*60)
    else:
        tx["status"] = "ABORTED"
        print("\n"+"="*60); print(f"✗ Tx {txid} aborted"); print("="*60)
        for pid,(h,p) in members.items():
            if pid == id_:
                continue
            json_send(h,p,{"type":"ABORT","txid":txid,"from":current_primary})

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
            bal[acct] = bal.get(acct, 0) + val
    return bal

def status_print():
    print("="*60)
    print(f"Node: {id_}    View: {view}")
    print(f"Current leader: {current_primary}")
    roles = []
    for nid in ids_sorted():
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
    bal = balances_from_committed()
    if not bal:
        print("Balances: (empty)")
    else:
        print("Balances:")
        for k,v in bal.items():
            print(f"  {k}: {v}")
    print("="*60)

def snapshot_text():
    lines = []
    lines.append(f"# Node {id_} snapshot @ {time.strftime('%Y-%m-%d %H:%M:%S')} (view={view}, leader={current_primary})")
    if not tx_log:
        lines.append("Transactions: (empty)")
    else:
        lines.append("Transactions:")
        for tid, info in tx_log.items():
            lines.append(f"  - {tid}: {info.get('status','UNKNOWN')} {json.dumps(info.get('data'))}")
    bal = balances_from_committed()
    if not bal:
        lines.append("Balances: (empty)")
    else:
        lines.append("Balances:")
        for k,v in bal.items():
            lines.append(f"  - {k}: {v}")
    return "\n".join(lines) + "\n"

def write_local_checkpoint_file(text):
    ensure_dir("checkpoints")
    path = os.path.join("checkpoints", f"{id_}_checkpoints.log")
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

def handle_checkpoint_sync_update_from_payload(msg):
    global members, current_primary, primary_host, primary_port, view, tx_log, state_data
    text = msg.get("text", "")
    # Persist unified text
    ensure_dir("checkpoints")
    path = os.path.join("checkpoints", f"{id_}_recovered_from_checkpoint.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    # Load in-memory state if provided
    view = msg.get("view", view)
    current_primary = msg.get("current_primary", current_primary)
    mh = msg.get("members")
    if mh and isinstance(mh, dict):
        members.clear(); members.update(mh)
    primary_host = msg.get("primary_host", primary_host)
    primary_port = msg.get("primary_port", primary_port)
    incoming_tx_log = msg.get("tx_log")
    incoming_state_data = msg.get("state_data")
    if isinstance(incoming_tx_log, dict):
        tx_log.clear(); tx_log.update(incoming_tx_log)
    if isinstance(incoming_state_data, dict):
        state_data.clear(); state_data.update(incoming_state_data)
    else:
        # Derive state_data from tx_log if not provided
        state_data.clear()
        for tid, info in tx_log.items():
            if info.get("status") == "COMMITTED":
                state_data[tid] = info.get("data")
    print(f"\n✓ Checkpoint/state synced from leader. View={view}, leader={current_primary}")

def on_msg(msg, addr):
    global crashed, members, current_primary, primary_host, primary_port, view, current_tx, pending_prepare_tx
    if crashed: return
    t = msg.get("type")

    if t == "MEMBERS":
        new_members = msg.get("members", {})
        members.clear()
        members.update(new_members)
        print(f"\n✓ Membership updated: {list(members.keys())}")
        print(f"\n{id_}> ", end="", flush=True)

    elif t == "CLIENT_JOIN":
        h = msg.get("host"); p = msg.get("port")
        if h and p:
            clients.add((h,p))
            print(f"\n✓ Client seen: {h}:{p}")
            print(f"\n{id_}> ", end="", flush=True)

    elif t == "PRE_PREPARE":
        txid = msg["txid"]; data = msg["data"]
        if current_primary != id_:
            print(f"\n→ Received PRE-PREPARE (tx {txid}) from {msg.get('from')}")
            print("  ✓ Waiting for manual vote: run 'prepare yes' or 'prepare no'")
            primary_host = msg.get("primary_host", primary_host); primary_port = msg.get("primary_port", primary_port)
            pending_prepare_tx = txid
            tx_log.setdefault(txid, {"status":"STARTED","data":data,"commit_started":False})
            print(f"\n{id_}> ", end="", flush=True)

    elif t == "PREPARE":
        txid = msg["txid"]; pid = msg["from"]; vote = msg["vote"]
        if pid != id_:
            prepare_votes.setdefault(txid, {})[pid] = vote
            print(f"\n→ PREPARE from {pid}: {vote}")
            print(f"\n{id_}> ", end="", flush=True)

    elif t == "COMMIT_VOTE":
        txid = msg["txid"]; pid = msg["from"]; ack = msg["ack"]
        if pid != id_:
            commit_votes.setdefault(txid, {})[pid]=ack
            print(f"\n→ COMMIT_VOTE from {pid}: {ack} (tx {txid})")
            print(f"\n{id_}> ", end="", flush=True)

    elif t == "ABORT":
        txid = msg["txid"]
        print(f"\n→ ABORT received (tx {txid})")
        tx_log.setdefault(txid, {"status":"ABORTED","data":state_data.get(txid)})
        tx_log[txid]["status"]="ABORTED"
        state_data.pop(txid, None)
        print(f"\n{id_}> ", end="", flush=True)

    elif t == "REPLY":
        txid = msg["txid"]
        print(f"\n→ REPLY received (tx {txid})")
        state_data[txid] = msg.get("data")
        tx_log.setdefault(txid, {"status":"COMMITTED","data":state_data[txid]})
        tx_log[txid]["status"]="COMMITTED"
        for (h,p) in list(clients):
            json_send(h,p,msg)
        print(f"\n  Data: {state_data.get(txid)}")
        print(f"\n{id_}> ", end="", flush=True)

    elif t == "VIEW_CHANGE":
        sender = msg.get("from")
        req_view = view + 1
        print(f"\n→ VIEW_CHANGE from {sender} (request view={req_view})")
        target = next_primary_id()
        if target == id_ and view not in vc_done_for_view:
            vc_votes.setdefault(view, set()).add(sender)
            vc_votes[view].add(id_)
            f,_ = compute_f_and_quorum()
            need = 2*f+1
            if len(vc_votes[view]) >= need:
                newv = view + 1
                vc_done_for_view.add(view)
                # broadcast NEW_VIEW exactly once
                for pid,(h,p) in members.items():
                    if pid == id_:
                        continue
                    json_send(h,p,{"type":"NEW_VIEW","new_view":newv,"from":id_,
                                   "primary_host":HOST,"primary_port":port,
                                   "members": members})
                # also inform P0
                json_send(DEFAULT_PRIMARY_HOST, DEFAULT_PRIMARY_PORT, {"type":"NEW_VIEW","new_view":newv,"from":id_,
                                                                       "primary_host":HOST,"primary_port":port,
                                                                       "members": members})
                view = newv; current_primary = id_
                primary_host, primary_port = HOST, port
                print(f"✓ Reached {need} votes; I ({id_}) broadcast NEW_VIEW, view={view}")
                # Re-exec aborted/unfinished after view change
                announce_primary_capabilities()
        print(f"\n{id_}> ", end="", flush=True)

    elif t == "NEW_VIEW":
        nv = msg.get("new_view", view+1)
        leader = msg.get("from")
        primary_host = msg.get("primary_host", primary_host)
        primary_port = msg.get("primary_port", primary_port)
        new_members = msg.get("members")
        if new_members:
            members.clear(); members.update(new_members)
        view = nv; current_primary = leader
        print(f"\n✓ NEW_VIEW received: view={view}, new leader={current_primary}")
        announce_primary_capabilities()
        print(f"\n{id_}> ", end="", flush=True)

    elif t == "CHECKPOINT_REQUEST":
        cid = msg.get("checkpoint_id")
        collector_host = msg.get("collector_host", DEFAULT_PRIMARY_HOST)
        collector_port = msg.get("collector_port", DEFAULT_PRIMARY_PORT)
        text = snapshot_text()
        print("\n" + text)
        write_local_checkpoint_file(text)
        json_send(collector_host, collector_port, {
            "type":"CHECKPOINT_REPORT",
            "checkpoint_id": cid,
            "node_id": id_,
            "text": text
        })
        print(f"→ Checkpoint report sent to {collector_host}:{collector_port} (id={cid})")
        print(f"\n{id_}> ", end="", flush=True)

    elif t == "CHECKPOINT_REPORT":
        # Only relevant when I am the collector (leader coordinating the checkpoint)
        cid = msg.get("checkpoint_id")
        node_id = msg.get("node_id", "UNKNOWN")
        text = msg.get("text", "")
        checkpoint_reports.setdefault(cid, {})[node_id] = text
        expected = checkpoint_expected.get(cid, len(members))
        got = len(checkpoint_reports[cid])
        print(f"\n→ Received checkpoint report from {node_id} ({got}/{expected})")
        if got >= expected:
            ensure_dir("checkpoints")
            final_path = os.path.join("checkpoints", f"final_checkpoint_{cid}.log")
            with open(final_path, "w", encoding="utf-8") as f:
                # write in member order for readability
                for nid in ids_sorted():
                    txt = checkpoint_reports[cid].get(nid)
                    if txt:
                        f.write(txt + ("\n" if not txt.endswith("\n") else ""))
            print(f"✓ Final checkpoint assembled: {final_path}")
        print(f"\n{id_}> ", end="", flush=True)

    elif t == "CHECKPOINT_SYNC":
        handle_checkpoint_sync_update_from_payload(msg)
        print(f"\n{id_}> ", end="", flush=True)

    elif t == "RECOVER_HELLO":
        # If I'm the leader, respond with latest checkpoint/state
        if current_primary == id_:
            text = load_latest_final_checkpoint()
            dest_h, dest_p = msg.get("host"), msg.get("port")
            payload = {
                "type":"CHECKPOINT_SYNC",
                "text": text,
                "view": view,
                "current_primary": current_primary,
                "members": members,
                "primary_host": HOST,
                "primary_port": port,
                "tx_log": tx_log,
                "state_data": state_data,
            }
            if dest_h and dest_p:
                json_send(dest_h, dest_p, payload)
                print(f"\n→ Sent latest checkpoint/state to recovering node {dest_h}:{dest_p}")

def server():
    json_server(HOST, port, on_msg, on_ready=banner)

def repl():
    global crashed, current_tx, pending_prepare_tx
    while True:
        try:
            cmd = input(f"{id_}> ").strip()
        except (KeyboardInterrupt, EOFError):
            cmd = "quit"
        if not cmd: continue

        if cmd == "status":
            status_print()

        elif cmd == "data":
            if state_data:
                print("Committed app data:")
                for k,v in state_data.items():
                    print(f"  {k}: {v}")
            else:
                print("(empty)")

        elif cmd.startswith("prepare "):
            if not pending_prepare_tx:
                print("× No pending tx to vote on (PRE_PREPARE not received yet)")
            else:
                choice = cmd.split()[-1].lower()
                vote = "VOTE_YES" if choice in ("yes","y") else "VOTE_NO"
                # Broadcast PREPARE to all members (including primary)
                for pid,(h,p) in members.items():
                    if pid == id_:
                        continue
                    json_send(h,p,{"type":"PREPARE","from":id_,"txid":pending_prepare_tx,"vote":vote})
                json_send(DEFAULT_PRIMARY_HOST, DEFAULT_PRIMARY_PORT, {"type":"PREPARE","from":id_,"txid":pending_prepare_tx,"vote":vote})
                print(f"✓ PREPARE broadcast: {vote} (tx={pending_prepare_tx})")
                pending_prepare_tx = None

        elif cmd.startswith("ack "):
            choice = cmd.split()[-1].lower()
            ack = "ACK_COMMIT" if choice == "commit" else "ACK_ABORT"
            txid = None
            if tx_log: txid = list(tx_log.keys())[-1]
            if not txid:
                print("× No tx to ack")
            else:
                # Broadcast COMMIT_VOTE to all members (including primary)
                for pid,(h,p) in members.items():
                    if pid == id_:
                        continue
                    json_send(h,p,{"type":"COMMIT_VOTE","from":id_,"txid":txid,"ack":ack})
                json_send(DEFAULT_PRIMARY_HOST, DEFAULT_PRIMARY_PORT, {"type":"COMMIT_VOTE","from":id_,"txid":txid,"ack":ack})
                if ack == "ACK_ABORT":
                    state_data.pop(txid, None)
                print("✓ Broadcast", ack)

        elif cmd == "crash":
            crashed = True; print("! Node crashes (will ignore incoming messages)")

        elif cmd == "recover":
            crashed = False; print("✓ Node recovered; requesting latest checkpoint from the current leader")
            # ask current leader (not always P0) for latest checkpoint/state
            json_send(primary_host, primary_port, {"type":"RECOVER_HELLO","host":HOST,"port":port})

        elif cmd == "view change":
            print(f"→ Broadcast VIEW_CHANGE (current view={view}, next leader={next_primary_id()})")
            for pid,(h,p) in members.items():
                if pid == id_:
                    continue
                json_send(h,p,{"type":"VIEW_CHANGE","from":id_})
            # also tell P0
            json_send(DEFAULT_PRIMARY_HOST, DEFAULT_PRIMARY_PORT, {"type":"VIEW_CHANGE","from":id_})

        elif cmd == "checkpoint":
            # If I am the leader, coordinate a distributed checkpoint; otherwise just local print
            if current_primary == id_:
                cid = time.strftime("%Y%m%d_%H%M%S")
                expected = len(members)
                checkpoint_expected[cid] = expected
                checkpoint_reports[cid] = {}
                text = snapshot_text()
                print("\n" + text)
                write_local_checkpoint_file(text)
                checkpoint_reports[cid][id_] = text
                for pid,(h,p) in members.items():
                    if pid == id_:
                        continue
                    json_send(h,p,{"type":"CHECKPOINT_REQUEST","checkpoint_id": cid,
                                   "collector_host": HOST, "collector_port": port})
                print(f"→ Started distributed checkpoint (expecting {expected} reports)")
            else:
                # still allow local snapshot for convenience
                text = snapshot_text()
                print("\n" + text)
                write_local_checkpoint_file(text)

        elif cmd == "tx":
            data = input("Enter tx data (key=value, e.g., account=alice,amount=100,operation=deposit):\ndata> ").strip()
            start_tx(data)

        elif cmd == "progress":
            if current_primary != id_:
                print("× I am not the leader; cannot run progress")
                continue
            if not current_tx:
                pending = [tid for tid,info in tx_log.items() if info.get("status")!="COMMITTED"]
                current_tx = pending[-1] if pending else None
                if not current_tx:
                    print("× No ongoing tx"); continue
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
            # B: Commit decision
            cy, cq = evaluate_commit(current_tx)
            if cy >= cq:
                finalize(current_tx, commit=True)
            else:
                print("Commit threshold not satisfied; aborting the tx.")
                finalize(current_tx, commit=False)
            current_tx = None

        elif cmd == "quit":
            print("Bye!"); time.sleep(0.2); break

        else:
            print("Unknown command")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python pbft_node.py <ID> <PORT>")
        sys.exit(1)
    id_ = sys.argv[1]
    port = int(sys.argv[2])
    threading.Thread(target=server, daemon=True).start()
    repl()
