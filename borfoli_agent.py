"""
Borfoli OS Agent — runs locally on Windows
Sends live system telemetry to your Borfoli server every 30s.

Install deps:  pip install psutil requests pywin32
Run:           python borfoli_agent.py
"""

import time
import requests
import psutil

try:
    import win32gui
    import win32process
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    print("[warn] pywin32 not installed — active window tracking disabled")
    print("       Run: pip install pywin32")

BORFOLI_URL = "https://jarvis-oouo.onrender.com"
PASSWORD    = "RC05pesaOtpBK0fw"
INTERVAL    = 30  # seconds between pings

def get_active_window():
    if not HAS_WIN32:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        return title if title else None
    except Exception:
        return None

def get_top_processes(n=5):
    procs = []
    for p in psutil.process_iter(["name", "cpu_percent"]):
        try:
            procs.append({"name": p.info["name"], "cpu": p.info["cpu_percent"] or 0})
        except Exception:
            pass
    procs.sort(key=lambda x: x["cpu"], reverse=True)
    return procs[:n]

def collect():
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu": cpu,
        "ram": mem.percent,
        "disk": disk.percent,
        "ram_used_gb": round(mem.used / 1e9, 1),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "active_window": get_active_window(),
        "top_processes": get_top_processes(),
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
    }

def run():
    print(f"Borfoli OS Agent starting — sending to {BORFOLI_URL}")
    print(f"Interval: {INTERVAL}s  |  pywin32: {'yes' if HAS_WIN32 else 'no'}")
    print("-" * 50)
    consecutive_errors = 0
    while True:
        try:
            data = collect()
            r = requests.post(
                f"{BORFOLI_URL}/os-data",
                json=data,
                auth=("x", PASSWORD),
                timeout=12
            )
            if r.status_code == 200:
                consecutive_errors = 0
                win = data.get("active_window") or "unknown"
                print(f"[{time.strftime('%H:%M:%S')}] OK  CPU {data['cpu']:.0f}%  RAM {data['ram']:.0f}%  | {win[:55]}")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] Server returned {r.status_code}")
                consecutive_errors += 1
        except requests.exceptions.ConnectionError:
            print(f"[{time.strftime('%H:%M:%S')}] Connection failed — server may be sleeping (cold start ~50s)")
            consecutive_errors += 1
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Error: {e}")
            consecutive_errors += 1

        if consecutive_errors >= 5:
            print("[warn] 5 consecutive errors — check that borfoli is deployed and password is correct")
            consecutive_errors = 0

        time.sleep(INTERVAL)

if __name__ == "__main__":
    run()
