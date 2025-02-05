Certainly! Below is a more organized and user-friendly version of the bugs and solutions you encountered during the installation of **0xBuilder**. Each issue is clearly labeled, and the solutions are easy to identify and copy-paste.

---

# **0xBuilder Installation Guide: Common Errors & Solutions**

This document provides a structured list of common errors encountered during the installation of **0xBuilder** and their corresponding solutions. The solutions are formatted for easy identification and quick application.

---

## **1. ERROR: "user '_apt'. - pkgAcquire::Run (13: Permission denied)"**

### **Issue:**
When running the following commands:
```bash
sudo add-apt-repository -y ppa:ethereum/ethereum
sudo apt-get update
```
You may encounter the error:
```
file '/var/cache/apt/archives/partial/' couldn't be accessed by user '_apt'. - pkgAcquire::Run (13: Permission denied)
```

### **Solution:**
1. Open the APT sandbox configuration file:
   ```bash
   sudo nano /etc/apt/apt.conf.d/10sandbox
   ```
2. Add the following line to the file:
   ```bash
   APT::Sandbox::User "root";
   ```

---

## **2. ERROR: "Timeout when adding Ethereum PPA"**

### **Issue:**
The command `sudo add-apt-repository -y ppa:ethereum/ethereum` fails with a timeout error.

### **Solution:**
Manually add the Ethereum PPA to your `sources.list` file:
1. Open the `sources.list` file:
   ```bash
   sudo nano /etc/apt/sources.list
   ```
2. Add the following line:
   ```bash
   deb https://ppa.launchpadcontent.net/ethereum/ethereum/ubuntu/ jammy main
   ```

---

## **3. Geth.service Configuration**

### **Service File:**
Here’s a tested **Geth.service** configuration file:

```bash
sudo nano /etc/systemd/system/geth.service
```

#### **Content:**
```ini
[Unit]
Description=Geth Full Node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=5s
User=geth
WorkingDirectory=/home/geth
ExecStart=/usr/bin/geth --mainnet \
  --syncmode snap \
  --http \
  --http.api eth,net,admin,web3,txpool \
  --http.addr "0.0.0.0" \
  --http.port 8545 \
  --ws \
  --ws.api eth,net,admin,web3,txpool \
  --ws.addr "0.0.0.0" \
  --ws.port 8546 \
  --maxpeers 200 \
  --cache 16000 \
  --cache.trie=4096 \
  --cache.gc=256 \
  --cache.snapshot=8192 \
  --allow-insecure-unlock \
  --metrics \
  --metrics.addr "0.0.0.0" \
  --metrics.port 6060 \
  --gcmode full \
  --txlookuplimit 0 \
  --db.engine pebble \
  --authrpc.jwtsecret /var/lib/secrets/jwt.hex \
  --verbosity 3 \
  --pprof \
  --pprof.addr "127.0.0.1" \
  --pprof.port 6060

[Install]
WantedBy=multi-user.target
```

---

## **4. Prysm-Beacon.service Configuration**

### **Service File:**
Here’s a tested **Prysm-Beacon.service** configuration file:

```bash
sudo nano /etc/systemd/system/prysm-beacon.service
```

#### **Content:**
```ini
[Unit]
Description=Prysm Beacon Chain
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=5s
User=prysm-beacon
ExecStart=/home/prysm-beacon/bin/prysm.sh beacon-chain \
  --mainnet \
  --execution-endpoint=http://127.0.0.1:8551 \
  --jwt-secret=/var/lib/secrets/jwt.hex \
  --suggested-fee-recipient=0x \
  --checkpoint-sync-url=https://beaconstate.info \
  --genesis-beacon-api-url=https://beaconstate.info \
  --accept-terms-of-use

[Install]
WantedBy=multi-user.target
```

---

## **5. Monitor Sync Progress**

### **Commands:**
To monitor the sync progress of your node, use the following commands:

1. **Check Sync Status via RPC:**
   ```bash
   curl -X POST \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"eth_syncing","params":[],"id":1}' \
        http://127.0.0.1:8545
   ```

2. **Check Peer Count:**
   ```bash
   curl -X POST \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"net_peerCount","params":[],"id":1}' \
        http://127.0.0.1:8545
   ```

3. **Check Chain ID:**
   ```bash
   curl -X POST \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}' \
        http://127.0.0.1:8545
   ```

---

## **6. Python Version Error: SyntaxError in `except* asyncio.CancelledError`**

### **Issue:**
If you encounter the following error:
```python
SyntaxError: invalid syntax
```
in the line:
```python
except* asyncio.CancelledError:
```

### **Solution:**
1. **Check Python Version:**
   ```bash
   python --version
   ```
   Ensure that you are using **Python 3.11** or later.

2. **Upgrade Python:**
   If your version is older than 3.11, upgrade Python:
   ```bash
   sudo add-apt-repository ppa:deadsnakes/ppa
   sudo apt update
   sudo apt install python3.11
   sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
   sudo update-alternatives --config python3
   ```

3. **Verify Installation:**
   ```bash
   python3 --version
   ```

4. **Install Python 3.11 Virtual Environment:**
   ```bash
   sudo apt install python3.11-venv
   ```

---

## **7. Circular Import Error**

### **Issue:**
Circular import error between `main_core.py` and `abi_registry.py`.

### **Solution:**
1. **Create a New Module:**
   Create a new file called `logging_utils.py` and move the `setup_logging` function there.

2. **Update Imports:**
   In all files where `setup_logging` is used, replace the old import with:
   ```python
   from logging_utils import setup_logging
   setup_logging()
   logger = logging.getLogger(__name__)
   ```

---

## **8. BASE_PATH Not Found**

### **Issue:**
Error related to `BASE_PATH` not being found.

### **Solution:**
1. **Add BASE_PATH to `.env`:**
   ```bash
   BASE_PATH=/root/0xBuilder
   ```

2. **Update `main_core.py`:**
   Replace:
   ```python
   await abi_registry.initialize(self.configuration.BASE_PATH)
   ```
   With:
   ```python
   await abi_registry.initialize(base_path=self.configuration.BASE_PATH)
   ```

3. **Ensure `configuration.py` Loads BASE_PATH:**
   Add the following line to `_initialize_defaults`:
   ```python
   self.BASE_PATH = Path(os.getenv("BASE_PATH", str(Path(__file__).parent.parent)))
   ```

---

## **9. 'Main_Core' Object Has No Attribute 'WEB3_MAX_RETRIES'**

### **Solution:**
In `main_core.py`, ensure the following lines are added in the `__init__` method:
```python
self.WEB3_MAX_RETRIES = WEB3_MAX_RETRIES
self.WEB3_RETRY_DELAY = WEB3_RETRY_DELAY
self.PROVIDER_TIMEOUT = PROVIDER_TIMEOUT
```

---

## **10. 'Configuration' Object Has No Attribute '_load_json'**

### **Solution:**
In `api_config.py`, replace `_load_json` with `_load_json_safe`:
```python
token_addresses = await self.configuration._load_json_safe(
    self.configuration.TOKEN_ADDRESSES, 
    "token addresses"
)
```

---

## **11. Incorrect Coingecko API Header**

### **Issue:**
Incorrect header name for Coingecko API key.

### **Solution:**
Replace `x-cg-pro-api-key` with `x-cg-demo-api-key` if you are using a free-tier API key:
```python
headers = {"x-cg-demo-api-key": config['api_key']} if config['api_key'] else None
```

---

## **12. Automate Virtual Environment Activation**

### **Solution:**
To avoid typing `source venv/bin/activate` every time, create an alias:

1. Open `.bashrc`:
   ```bash
   nano ~/.bashrc
   ```

2. Add the following line:
   ```bash
   alias activate_venv="source venv/bin/activate"
   ```

3. Reload the configuration:
   ```bash
   source ~/.bashrc
   ```

Now, you can activate the virtual environment with:
```bash
activate_venv
```

---

This organized guide should help you quickly identify and resolve issues during the installation of **0xBuilder**.