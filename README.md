# COMP5567 - Lab 4: Practical Byzantine Fault Tolerance (PBFT)

**Lecturer:** Prof. Xiao Bin  
**TA:** Zhao Mingyang (myang.zhao@connect.polyu.hk)  
**Course:** COMP5567 - Distributed Systems  
**Lab 4 Theme:** Implementation and Testing of PBFT Consensus Mechanism  

---

## 📦 1. Environment Setup

### 1.1 Requirements
- **Python** ≥ 3.8  
- **PyCharm** or any Python IDE  
- Operating System: Windows / macOS / Linux  
- Recommended Dependencies (install via pip if needed):  
  ```bash
  pip install socket json threading
  ```

### 1.2 Code Download
1. Visit the repository:  
   👉 [https://github.com/mzhao6320-dot/5567-PBFT](https://github.com/mzhao6320-dot/5567-PBFT)
2. Click **Code → Download ZIP**
3. Extract the ZIP file (e.g., `Extract to ./5567-PBFT-main`)
4. Open the folder in **PyCharm**:
   - File → Open → Select folder `5567-PBFT-main`
   - Confirm with **OK**
5. In the popup dialog, choose **“This Window”**

---

## 🧰 2. System Startup Guide

### 2.1 Terminal Setup
Open a terminal window in the project root path:
```bash
cd 5567-PBFT-main
```

### 2.2 Start the Primary Node
```bash
python primary_node.py
```

### 2.3 Start Replica Nodes
```bash
python pbft_node.py P1 5001
python pbft_node.py P2 5002
python pbft_node.py P3 5003   # Byzantine node
```

### 2.4 Start the Client
```bash
python pbft_client.py 7000
```

### 2.5 Common Commands
In the **Primary Node** terminal:
```bash
list       # show all nodes
status     # show node status
quit       # exit the program
```

---

## 🧪 3. Case Studies (Built-in Demos)

This project contains multiple test cases simulating different PBFT states, failures, and recoveries.

### 🧩 Case 1: Normal Operation (Deposit)
**Command:**
```bash
send account=alice,amount=100,operation=deposit
```
client: send account=alice,amount=l00,operation=deposit
P0 (primary): tx
P0 (primary): account=alice,amount=100,operation=deposit
Pl (replica): prepare yes
P2 (replica): prepare yes
P3 (replica, Byzantine): prepare to PO yes
P3 (replica, Byzantine): prepare to Pl yes
P3 (replica, Byzantine): prepare to P2 yes
P0 (primary), Pl (replica), P2 (replica), P3 (replica, Byzantine): progress
P0 (primary), Pl (replica), P2 (replica): ack commit
P3 (replica, Byzantine): ack to PO commit
P3 (replica, Byzantine): ack to Pl commit
P3 (replica, Byzantine): ack to P2 commit
P0 (primary), Pl (replica), P2 (replica), P3 (replica, Byzantine): progress
PO (primary): status

---

### 💸 Case 2: Normal Operation (Withdraw 20)
**Command:**
```bash
send account=alice,amount=20,operation=withdraw
```
client: send account=alice,amount=20,operation=withdraw
P0 (primary): tx
P0 (primary): account=alice,amount=20,operation=withdraw
Pl (replica): prepare yes
P2 (replica): prepare yes
P3 (replica, Byzantine): prepare to P0 yes
P3 (replica, Byzantine): prepare to Pl no
P3 (replica, Byzantine): prepare to P2 no
P0 (primary), Pl (replica), P2 (replica), P3 (replica, Byzantine): progress
P0 (primary), Pl (replica), P2 (replica): ack commit
P3 (replica, Byzantine): ack to P0 commit
P3 (replica, Byzantine): ack to Pl abort
P3 (replica, Byzantine): ack to P2 abort
P0 (primary), Pl (replica), P2 (replica), P3 (replica, Byzantine): progress
P0(primary): status

---

### 💣 Case 3: Withdrawal Failure (Withdraw 100)
**Command:**
```bash
send account=alice,amount=100,operation=withdraw
```
client: send account=alice,amount=l00,operation=withdraw PO(primary): tx
P0 (primary): account=alice,amount=100,operation=withdraw
Pl(replica): prepare no
P2 (replica): prepare no
P3 (replica, Byzantine): prepare to PO yes
P3 (replica, Byzantine): prepare to Pl yes。P3 (replica, Byzantine): prepare to P2 no
P0 (primary), Pl (replica), P2 (replica), P3 (replica, Byzantine): progressP0 (primary),Pl(replica), P2 (replica):ack commit
P0 (primary): status

---

### 🔄 Case 4: Commit Phase Conflict
- All nodes prepare *yes*, but some replicas abort in commit phase.  
- Simulates inconsistent commit/abort behavior due to Byzantine interference.
client: send account=alice,amount=l00,operation=withdraw
P0 (primary): tx
P0 (primary): account=alice,amount=100,operation=withdraw
PI(replica): prepare yes
P2 (replica): prepare yes
P3 (replica, Byzantine): prepare to PO yes
P3 (replica, Byzantine): prepare to Pl yes
P3 (replica, Byzantine): prepare to P2 yes
P0 (primary),Pl (replica), P2 (replica), P3 (replica, Byzantine): progress
P0 (primary): ack commit
Pl (replica): ack abort
P2 (replica): ack abort
P3 (replica, Byzantine): ack to PO commitP3 (replica, Byzantine): ack to Pl commitP3 (replica, Byzantine): ack to P2 commit
P0 (primary), Pl (replica), P2 (replica), P3 (replica, Byzantine): progress
P0 (primary): status
---

### ⚠️ Case 5: Crash and Recovery
**Scenario:**
- P3 (Byzantine) crashes, then recovers.  
- System continues consensus with remaining nodes.
P3 (replica, Byzantine): crash
client: account=alice,amount=|00,operation=deposit
P0 (primary): tx
P0(primary): account=alice,amount=l00,operation=deposit
Pl (replica): prepare yes
P2 (replica): prepare yes
P0 (primary), Pl (replica), P2 (replica): progress
P0 (primary): ack commit
P2 (replica): ack commit
P3 (replica): ack commit
P0 (primary), Pl (replica), P2 (replica): progress
P0 (primary): status
P3(replica, Byzantine): recover
---

### 🔁 Case 6: View Change
**Scenario:**
- Primary node (P0) crashes.  
- Next replica (P1) automatically becomes the new primary.  
- System continues operation under new view.
P0 (primary): crash
Pl (replica), P2 (replica), P3 (replica, Byzantine): view change
P0 (replica): recover
Pl(primary): status
Pl(primary): crash
P0 (replica), P2 (replica), P3 (replica, Byzantine): view change
Pl(replica): recover
P2 (primary): status
P2 (primary): crash
P0 (replica), Pl (replica), P3 (replica, Byzantine): view change
P2 (replica): recover
P3 (primary): status
P3 (primary): crash
P0 (replica), Pl (replica), P2 (replica): view change
P3 (replica): recover
P0 (primary): status
---

### 📍 Case 7: Checkpointing
**Scenario:**
- Periodically records the transaction state (e.g., after 1000 rounds).  
- Checkpoint logs stored in:
  ```
  checkpoints/final_checkpoint_<timestamp>.log
  ```
P0(primary):checkpoint
View all nodes'logs in checkpointslfinal checkpoint 20251110 231455.log
---

## 👩‍💻 4. Your Practice

You are required to reproduce and analyze the following cases:

### 🧠 Practice Case 1
**Withdraw 50**
```bash
send account=alice,amount=50,operation=withdraw
```
- P1: no  
- P2: no  
- P3 (Byzantine): mixed yes/no responses  

Observe how the system resolves disagreement and whether consensus is achieved.

---

### 🧠 Practice Case 2
**Withdraw 20**
```bash
send account=alice,amount=20,operation=withdraw
```
- P1: yes  
- P2: yes  
- P3: no to P0, yes to P1, no to P2  
Despite mixed signals, all nodes eventually commit.

---

## 🧾 5. Program Termination
To cleanly stop all running processes:
```bash
quit
```
Run this command in each terminal (Primary + all Replicas).

---

## 📁 6. Useful Links
- 📚 Lecture Reference: [Lecture 9, Pages 3–18](https://learn.polyu.edu.hk/ultra/courses/_125335_1/cl/outline)
- 💾 Code Repository: [https://github.com/mzhao6320-dot/5567-PBFT](https://github.com/mzhao6320-dot/5567-PBFT)

---

## ✅ 7. Expected Outcomes
- Understand PBFT system deployment and inter-node communication  
- Observe how consensus is reached or fails under Byzantine behavior  
- Learn fault recovery (crash & view change) mechanisms  
- Generate and analyze system logs and checkpoints  

---

## 🏁 8. Notes
- Ensure all ports (5000–7000) are available.  
- Use separate terminal windows for each node.  
- If a node crashes, simply rerun its command to recover.  
- You can modify client transaction parameters to simulate different scenarios.  

---

**© 2025 Hong Kong Polytechnic University — COMP5567 Distributed Systems Lab**
