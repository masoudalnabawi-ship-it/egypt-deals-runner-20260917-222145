import json, re, time
from pathlib import Path

DEV = Path("/root/telegram_deals_bot_v7_dev")
WATCH = Path("/root/telegram_deals_bot_v1/.amazon_manual_watch.txt")
STATE = DEV / ".amazon_competitor_bridge_state.json"

ASIN_RE = re.compile(r'(?<![A-Z0-9])(B0[A-Z0-9]{8})(?![A-Z0-9])', re.I)

def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}

def save_state(s):
    STATE.write_text(json.dumps(s))

def existing():
    WATCH.parent.mkdir(parents=True, exist_ok=True)
    WATCH.touch(exist_ok=True)
    return set(ASIN_RE.findall(WATCH.read_text(errors="ignore").upper()))

def candidate_files():
    out=[]
    for p in DEV.glob("*competitor*"):
        if p.is_file() and p.suffix.lower() in {".jsonl",".json",".log",".txt"}:
            out.append(p)
    return out

state=load_state()
seen_asins=existing()

print("✅ AMAZON COMPETITOR RECOVERY ON", flush=True)

while True:
    try:
        for p in candidate_files():
            pos=int(state.get(str(p),0))
            size=p.stat().st_size
            if pos > size:
                pos=0

            with p.open("r",encoding="utf-8",errors="ignore") as f:
                f.seek(pos)
                chunk=f.read()
                state[str(p)]=f.tell()

            for asin in ASIN_RE.findall(chunk.upper()):
                if asin in seen_asins:
                    continue

                # Only accept ASIN-like IDs appearing near Amazon context.
                idx=chunk.upper().find(asin)
                around=chunk[max(0,idx-250):idx+250].lower()
                if "amazon" not in around and "amzn" not in around:
                    continue

                with WATCH.open("a",encoding="utf-8") as w:
                    w.write("\n"+asin)

                seen_asins.add(asin)
                print("🎯 COMPETITOR -> AMAZON PRIORITY", asin, flush=True)

        save_state(state)

    except Exception as e:
        print("⚠️ BRIDGE ERROR",repr(e),flush=True)

    time.sleep(10)
