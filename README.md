# PBFT Demo (Manual Voting) — README

This repository is a **teaching/demo** implementation of a simplified PBFT-style protocol with **manual** PREPARE/COMMIT voting. It includes:

- `primary_node.py` — node `P0` (starts as the initial leader but can become a replica after a view change)
- `pbft_node.py` — replica nodes `P1`, `P2`, `P3`, … (any node can become leader after a view change)
- `pbft_client.py` — a simple client that connects to the leader and displays `REPLY` messages
- `pbft_utils.py` — lightweight TCP + newline-delimited JSON (NDJSON) utilities

The code demonstrates:
- Transaction flow with **Pre-prepare → Prepare → Commit → Reply**
- **Manual** voting by replicas (`prepare yes/no`, `ack commit/abort`)
- **View change** to rotate the leader role across nodes
- **Distributed checkpoints** coordinated by the **current leader**
- **Crash/Recover** behavior where a recovering node syncs its view/membership/tx-log/state from the **current leader**

> ⚠️ This is a **didactic** system. Many production PBFT details are simplified. It’s intentionally interactive to visualize consensus phases.

---

## 1) Requirements

- Python 3.8+
- OS: Linux / macOS / Windows (multiple terminals needed)
- All processes run on `127.0.0.1` by default
- Default ports:
  - Leader (initial): `P0` on **5000**
  - Client (default): **7000**
  - Replicas: choose distinct ports (e.g., `5001`, `5002`, `5003`)

---

## 2) File Layout

```
pbft_utils.py       # socket helpers (json_server/json_send)
pbft_client.py      # client CLI
pbft_node.py        # replica node implementation (can become leader)
primary_node.py     # P0 node (starts as leader, can become replica)
```

---

## 3) Quick Start (Recommended 4-node cluster)

To get meaningful quorums (f = 1), run **4 nodes total**:

- `P0` (initial leader) + `P1` + `P2` + `P3`  
- Client connects to the leader (whoever it is for the current view)

Open **five** terminals.

### Terminal A — Start the initial leader `P0`
```bash
python primary_node.py
```

### Terminals B–D — Start replicas `P1`, `P2`, `P3`
```bash
python pbft_node.py P1 5001
python pbft_node.py P2 5002
python pbft_node.py P3 5003
```

### Terminal E — Start client on port 7000
```bash
python pbft_client.py 7000
```

You should see each replica register with `P0`, and the client connect to the current leader.

---

## 4) Command Reference

### 4.1 `primary_node.py` (P0)

- `list` — list participants
- `status` — show **View**, **Current leader**, **Members**, tx history, balances
- `tx` — start a new transaction (only when **P0 is the leader**)
- `progress` — drive consensus (leader only): evaluate **Prepare**, then **Commit**
- `prepare yes|no` — as a **replica** only (when P0 is **not** leader), cast PREPARE
- `ack commit|abort` — as a **replica** only (when P0 is **not** leader), cast COMMIT_VOTE
- `view change` — broadcast a view-change request
- `checkpoint` — coordinate a distributed checkpoint (**leader only**)
- `crash` / `recover` — simulate failure and recovery
- `quit` — exit

### 4.2 `pbft_node.py` (P1/P2/P3/…)

- `status` — show **Node**, **View**, **Current leader**, **Members**, tx history, balances
- `data` — show locally committed app data
- `tx` — start a new transaction (only when **this node is the leader**)
- `progress` — leader only: evaluate **Prepare**, then **Commit**
- `prepare yes|no` — **replica** casts PREPARE for the pending tx
- `ack commit|abort` — **replica** casts COMMIT_VOTE
- `view change` — broadcast a view-change request
- `checkpoint` — if **leader**, coordinate distributed checkpoint; otherwise print local snapshot only
- `crash` / `recover` — simulate failure and recovery
- `quit` — exit

### 4.3 `pbft_client.py`

- `send key=value,...` — send a request payload to the leader
- `list` — show number of `REPLY` messages received
- `quit` — exit

---

## 5) End-to-End Transaction Flow (Manual Voting)

This is the flow when **P0 is leader** (initial view), with `P1`, `P2`, `P3` replicas.

1) **Start a transaction** on `P0`:
   ```
   tx
   Enter tx data (key=value, e.g., account=alice,amount=100,operation=deposit):
   data> account=alice,amount=100,operation=deposit
   ```
   P0 broadcasts `PRE_PREPARE{txid,data}` to replicas.

2) **Each replica** (`P1`, `P2`, `P3`) receives `PRE_PREPARE` and must **manually vote**:
   ```
   prepare yes
   ```
   Each replica broadcasts `PREPARE{txid, VOTE_YES|VOTE_NO}`.

3) **Leader** (`P0`) checks prepare votes and enters COMMIT when threshold met:
   ```
   progress
   ```
   - On the first `progress`, P0 prints **Prepare votes** and if satisfied, starts **COMMIT** phase.

4) **Each replica** casts **commit ack**:
   ```
   ack commit
   ```
   Each replica broadcasts `COMMIT_VOTE{txid, ACK_COMMIT|ACK_ABORT}`.

5) **Leader** finalizes:
   ```
   progress
   ```
   - On the second `progress`, if commit threshold is satisfied, P0 **commits** and broadcasts `REPLY`.  
   - The client displays received `REPLY` messages.

> You can perform the exact same flow when a **replica becomes leader** after a view change (see next section). The leader is the only node that can run `tx`, `progress`, and coordinate `checkpoint`.

---

## 6) View Change (Leader Rotation)

Any node can request a view change:

- On a replica (e.g., `P1`) **or** on `P0`:
  ```
  view change
  ```

- The **next leader** is determined by round-robin: `["P0", "P1", "P2", "P3", ...]`.
- The **next leader** gathers `VIEW_CHANGE` votes until it has **2f+1** (including itself), then broadcasts:
  ```
  NEW_VIEW { new_view, from=<new leader>, primary_host/port, members }
  ```

After `NEW_VIEW`:
- All nodes update `view`, `current_primary`, and, if they are now the leader, they can:
  - `tx` / `progress`
  - coordinate `checkpoint`
  - optionally re-broadcast unfinished `PRE_PREPARE` for pending txs

**P0 as replica:** When `P0` is no longer leader, it behaves like any replica:
- `prepare yes|no`
- `ack commit|abort`
- It cannot `tx`, `progress`, or coordinate `checkpoint` (unless it becomes leader again).

---

## 7) Checkpoints

Only the **current leader** should coordinate a **distributed checkpoint**.

- On the leader (whoever currently leads):
  ```
  checkpoint
  ```
  The leader:
  - prints & appends a local snapshot,
  - sends `CHECKPOINT_REQUEST{checkpoint_id, collector_host, collector_port}` to all replicas,
  - collects `CHECKPOINT_REPORT` from each node,
  - assembles `checkpoints/final_checkpoint_<id>.log` when all reports arrive.

- On **replicas**, `checkpoint` prints and appends a **local** snapshot only (no coordination).

---

## 8) Crash & Recover (State Sync)

- On any node:
  ```
  crash
  ```
  The node ignores all incoming messages.

- To recover:
  ```
  recover
  ```
  The recovering node sends `RECOVER_HELLO{host, port}` to the **current leader** (not always P0).  
  The leader replies with `CHECKPOINT_SYNC` carrying:
  - latest checkpoint text,
  - `view`, `current_primary`, `members`, `primary_host/port`,
  - `tx_log`, `state_data`.

The recovering node applies this **in-memory**:
- updates `view`, `current_primary`, `members`, `primary_host/port`,
- replaces its `tx_log` and `state_data` (or derives `state_data` from committed txs).

After recovery, `status` on the recovering node **matches the leader**.

---

## 9) Quorum & Display Rules (Important)

- Let **N** be the **total number of nodes** in the view (leader + replicas).
- We compute `f = floor((N-1)/3)`. In a **4-node** setup, `f = 1`.
- **Decision logic (unchanged)**:
  - Prepare phase threshold uses **2f** **replica** votes (not counting the leader’s own implicit vote).
  - Commit phase threshold uses **2f** **replica** acks (not counting the leader’s own implicit vote).
- **Display-only adjustment** (what you see printed):
  - Prepare shows: **(yes_from_replicas + 1) / N** with threshold **≥ (2f + 1)**
  - Commit shows: **(acks_from_replicas + 1) / N** with threshold **≥ (2f + 1)**
  - This presentation **includes the leader’s implicit vote** to match the expected **“x/N (threshold ≥ 2f+1)”** format, **without changing the decision logic**.

**Example:** For N = 4 (P0 + P1 + P2 + P3), `f = 1`  
- Display threshold: **≥ 3** (i.e., `2f + 1 = 3`)  
- Decision logic still uses **2f = 2 replica** votes/acks internally.

---

## 10) Client Usage

Start the client (default port 7000; you can pass your own):
```bash
python pbft_client.py 7000
```

Commands:
- `send account=alice,amount=100,operation=deposit`
- `list`
- `quit`

When the leader commits a transaction, you’ll see `REPLY` messages in the client.

---

## 11) Message Types (Protocol Sketch)

- **Membership / Client**
  - `REGISTER{id,host,port}` → sent by replicas to `P0`
  - `MEMBERS{members}` → distributed membership map
  - `CLIENT_HELLO{host,port}` → client announces itself to leader (initially P0)
  - `CLIENT_JOIN{host,port}` → leader informs nodes about client endpoint
- **Consensus**
  - `PRE_PREPARE{txid,data,from,primary_host,primary_port}`
  - `PREPARE{txid,vote}` with `VOTE_YES|VOTE_NO`
  - `COMMIT_VOTE{txid,ack}` with `ACK_COMMIT|ACK_ABORT`
  - `REPLY{txid,result,data,from}`
  - `ABORT{txid,from}`
- **View Change**
  - `VIEW_CHANGE{from}`
  - `NEW_VIEW{new_view,from,primary_host,primary_port,members}`
- **Checkpoint & Recovery**
  - `CHECKPOINT_REQUEST{checkpoint_id,collector_host,collector_port}`
  - `CHECKPOINT_REPORT{checkpoint_id,node_id,text}`
  - `CHECKPOINT_SYNC{text,view,current_primary,members,primary_host,primary_port,tx_log,state_data}`
  - `RECOVER_HELLO{host,port}`

---

## 12) Typical Walkthrough (4 Nodes)

1) Start `P0`, `P1`, `P2`, `P3`, and `client`.
2) On `P0`:
   ```
   tx
   data> account=alice,amount=100,operation=deposit
   ```
3) On **each** of `P1`, `P2`, `P3`:
   ```
   prepare yes
   ```
4) On `P0`:
   ```
   progress
   ```
   (Should enter COMMIT phase.)
5) On **each** of `P1`, `P2`, `P3`:
   ```
   ack commit
   ```
6) On `P0`:
   ```
   progress
   ```
   (Should commit, broadcasting `REPLY`.)

7) In `client`:
   ```
   list
   ```
   (See number of replies increase.)

---

## 13) View Change Example

Assume we want **P1** to become leader.

- On `P1` (or any node), request:
  ```
  view change
  ```
- Once **P1** collects enough votes (2f+1), it will broadcast `NEW_VIEW`.  
- All nodes print the new view and leader.  
- Now you can start a transaction on **P1**:
  ```
  tx
  data> account=bob,amount=50,operation=deposit
  progress
  ```
- Replicas vote as usual (`prepare yes`, `ack commit`), and **P1** calls `progress` again to finalize.

> **P0 as replica:** After `NEW_VIEW` makes `P1` the leader, `P0` can now run `prepare yes/no` and `ack commit/abort`.

---

## 14) Checkpoint Example

- On the **current leader** (e.g., `P1` after view change):
  ```
  checkpoint
  ```
  The leader prints its snapshot, requests reports from replicas, and assembles a file:
  ```
  checkpoints/final_checkpoint_<timestamp>.log
  ```

- On a **replica**, running `checkpoint` prints/writes only a local snapshot (no coordination).

---

## 15) Crash & Recover Example

- On `P2`:
  ```
  crash
  ```
- After some commits by the cluster, bring `P2` back:
  ```
  recover
  ```
  `P2` will request `CHECKPOINT_SYNC` from the **current leader**.  
  After applying it:
  ```
  status
  ```
  should match the leader’s view/leader/members and reflect committed history.

---

## 16) Notes & Limitations

- **Manual** votes are intentional to visualize PBFT phases.
- Only the **leader** can:
  - start a transaction (`tx`),
  - drive the phases (`progress`),
  - coordinate checkpoint (`checkpoint`).
- Displayed quorum counters show `x/N (threshold ≥ 2f+1)` including the leader’s implicit vote.  
  **Decision logic is unchanged** and uses the original “replicas-only 2f” rule internally.
- `ack` targets the most recent tx known locally; avoid running multiple overlapping txs in demo sessions.
- This demo uses one TCP connection per message and a thread-per-connection model for simplicity.

---

## 17) Troubleshooting

- **Client cannot connect**: ensure the leader is running and listening on `127.0.0.1:5000`.
- **PRE_PREPARE not received** on replicas: verify `P0` is leader and that you’ve started replicas with distinct ports.
- **`progress` does nothing**: only the leader should run it; ensure replicas have already cast votes/acks.
- **View change didn’t complete**: you need enough nodes (prefer **4 nodes** total) to reach `2f+1` view-change votes.
- **Checkpoint didn’t assemble**: only the leader coordinates; check that all replicas are up and reported.

---

## 18) License & Attribution

This code is for learning purposes and demonstrations of consensus concepts. Use at your own risk.
