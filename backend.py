import json
import os
from datetime import datetime, timezone
from html import escape

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import paho.mqtt.client as mqtt
from pymongo import MongoClient, DESCENDING

# =========================================================
# CONFIG
# =========================================================
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_TOPIC_FILTER = os.getenv("MQTT_TOPIC_FILTER", "Guerzoni/5F/+/#")

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "armadietto_iot")
DEFAULT_DEVICE_ID = os.getenv("DEFAULT_DEVICE_ID", "armadietto01")

print("[DEBUG] MQTT_HOST =", MQTT_HOST)
print("[DEBUG] MQTT_PORT =", MQTT_PORT)
print("[DEBUG] MQTT_USERNAME =", MQTT_USERNAME)
print("[DEBUG] MQTT_PASSWORD_LEN =", len(MQTT_PASSWORD))
print("[DEBUG] MQTT_TOPIC_FILTER =", MQTT_TOPIC_FILTER)

# =========================================================
# MongoDB
# =========================================================
mongo_client = MongoClient(MONGODB_URI, tz_aware=True)
db = mongo_client[MONGODB_DB]

RAW_COLLECTION = "mqtt_raw"
EVENTS_COLLECTION = "events"
RFID_COLLECTION = "rfid_reads"
STATE_COLLECTION = "device_state"
SENSOR_COLLECTION = "sensor_readings"

raw_col = db[RAW_COLLECTION]
events_col = db[EVENTS_COLLECTION]
rfid_col = db[RFID_COLLECTION]
state_col = db[STATE_COLLECTION]
sensor_col = db[SENSOR_COLLECTION]

app = FastAPI(title="Armadietto IoT Dashboard")


def now_utc():
    return datetime.now(timezone.utc)


def ensure_collections():
    existing = db.list_collection_names()

    if SENSOR_COLLECTION not in existing:
        db.create_collection(
            SENSOR_COLLECTION,
            timeseries={
                "timeField": "timestamp",
                "metaField": "meta",
                "granularity": "seconds"
            }
        )

    db[RAW_COLLECTION].create_index([("timestamp", DESCENDING)])
    db[RAW_COLLECTION].create_index("topic")

    db[EVENTS_COLLECTION].create_index([("timestamp", DESCENDING)])
    db[EVENTS_COLLECTION].create_index("device_id")
    db[EVENTS_COLLECTION].create_index("type")

    db[RFID_COLLECTION].create_index([("timestamp", DESCENDING)])
    db[RFID_COLLECTION].create_index("device_id")
    db[RFID_COLLECTION].create_index("uid")

    db[STATE_COLLECTION].create_index("updated_at")
    db[SENSOR_COLLECTION].create_index([("timestamp", DESCENDING)])


def serialize_doc(doc):
    if not doc:
        return None

    out = {}
    for k, v in doc.items():
        if k == "_id":
            out[k] = str(v)
        elif isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = {}
            for kk, vv in v.items():
                if isinstance(vv, datetime):
                    if vv.tzinfo is None:
                        vv = vv.replace(tzinfo=timezone.utc)
                    out[k][kk] = vv.isoformat()
                else:
                    out[k][kk] = vv
        else:
            out[k] = v
    return out


def parse_topic(topic: str):
    parts = topic.split("/")
    if len(parts) != 4:
        return None, None
    return parts[2], parts[3]


def save_raw(topic: str, payload_text: str, payload_json):
    raw_col.insert_one({
        "timestamp": now_utc(),
        "topic": topic,
        "payload_raw": payload_text,
        "payload_json": payload_json
    })


def update_state(device_id: str, fields: dict):
    state_col.update_one(
        {"_id": device_id},
        {"$set": {**fields, "updated_at": now_utc()}},
        upsert=True
    )


def handle_status(device_id: str, payload: dict):
    update_state(device_id, {
        "device_id": device_id,
        "online": payload.get("online", False),
        "door_state": payload.get("door_state"),
        "wifi_rssi": payload.get("wifi_rssi"),
        "last_temperature_c": payload.get("temperature_c"),
        "last_humidity_pct": payload.get("humidity_pct")
    })


def handle_event(device_id: str, payload: dict):
    events_col.insert_one({
        "timestamp": now_utc(),
        "device_id": device_id,
        "type": payload.get("type"),
        "message": payload.get("message"),
        "source": payload.get("source"),
        "uid": payload.get("uid"),
        "door_state": payload.get("door_state")
    })


def handle_rfid(device_id: str, payload: dict):
    rfid_col.insert_one({
        "timestamp": now_utc(),
        "device_id": device_id,
        "uid": payload.get("uid"),
        "authorized": payload.get("authorized"),
        "door_state": payload.get("door_state")
    })

    update_state(device_id, {
        "device_id": device_id,
        "last_rfid_uid": payload.get("uid"),
        "last_rfid_authorized": payload.get("authorized")
    })


def handle_sensors(device_id: str, payload: dict):
    sensor_col.insert_one({
        "timestamp": now_utc(),
        "meta": {
            "device_id": device_id,
            "sensor": payload.get("sensor", "aht20")
        },
        "temperature_c": payload.get("temperature_c"),
        "humidity_pct": payload.get("humidity_pct")
    })

    update_state(device_id, {
        "device_id": device_id,
        "last_temperature_c": payload.get("temperature_c"),
        "last_humidity_pct": payload.get("humidity_pct")
    })


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"[MQTT] Connected: {reason_code}")

    if reason_code == 0:
        client.subscribe(MQTT_TOPIC_FILTER)
        print(f"[MQTT] Subscribed to: {MQTT_TOPIC_FILTER}")
    else:
        print("[MQTT] Connessione NON riuscita")


def on_message(client, userdata, msg):
    topic = msg.topic
    payload_text = msg.payload.decode("utf-8", errors="replace").strip()

    print(f"[MQTT] {topic} -> {payload_text}")

    device_id, channel = parse_topic(topic)
    if not device_id or not channel:
        print(f"[WARN] Topic non riconosciuto: {topic}")
        return

    try:
        payload_json = json.loads(payload_text)
    except json.JSONDecodeError:
        payload_json = {"raw": payload_text}

    try:
        save_raw(topic, payload_text, payload_json)

        if not isinstance(payload_json, dict):
            return

        if channel == "status":
            handle_status(device_id, payload_json)
        elif channel == "events":
            handle_event(device_id, payload_json)
        elif channel == "rfid":
            handle_rfid(device_id, payload_json)
        elif channel == "sensors":
            handle_sensors(device_id, payload_json)

        print("[MongoDB] Salvataggio OK")

    except Exception as e:
        print(f"[MongoDB] ERRORE salvataggio: {e}")


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="backend-armadietto")
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

if MQTT_USERNAME:
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

mqtt_client.tls_set()


@app.on_event("startup")
def startup_event():
    print("[APP] Avvio backend...")

    try:
        mongo_client.admin.command("ping")
        print("[MongoDB] Ping OK")
    except Exception as e:
        print(f"[MongoDB] ERRORE ping: {e}")

    try:
        ensure_collections()
        print("[MongoDB] Collections OK")
    except Exception as e:
        print(f"[MongoDB] ERRORE collections: {e}")

    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.loop_start()
        print("[MQTT] connect() chiamato")
    except Exception as e:
        print(f"[MQTT] ERRORE connect: {e}")

    print("[APP] Backend avviato")


@app.on_event("shutdown")
def shutdown_event():
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    print("[APP] Backend fermato")


@app.get("/api/status/{device_id}")
def api_status(device_id: str):
    doc = state_col.find_one({"_id": device_id})
    return serialize_doc(doc) or {}


@app.get("/api/events/{device_id}")
def api_events(device_id: str, limit: int = Query(50, ge=1, le=500)):
    docs = events_col.find({"device_id": device_id}).sort("timestamp", DESCENDING).limit(limit)
    return [serialize_doc(d) for d in docs]


@app.get("/api/rfid/{device_id}")
def api_rfid(device_id: str, limit: int = Query(50, ge=1, le=500)):
    docs = rfid_col.find({"device_id": device_id}).sort("timestamp", DESCENDING).limit(limit)
    return [serialize_doc(d) for d in docs]


@app.get("/api/sensors/{device_id}")
def api_sensors(device_id: str, limit: int = Query(100, ge=1, le=1000)):
    docs = sensor_col.find({"meta.device_id": device_id}).sort("timestamp", DESCENDING).limit(limit)
    return [serialize_doc(d) for d in docs]


@app.get("/", response_class=HTMLResponse)
def dashboard():
    device_id = escape(DEFAULT_DEVICE_ID)

    return f"""<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <title>Armadietto IoT — Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:        #0b0e14;
      --surface:   #111520;
      --surface2:  #171d2e;
      --border:    #1f2840;
      --border2:   #2a3556;
      --cyan:      #00d4ff;
      --cyan-dim:  #0097b8;
      --green:     #00e5a0;
      --green-dim: #00a36e;
      --amber:     #ffb830;
      --red:       #ff4d6a;
      --text:      #d8e0f0;
      --text-muted:#6b7a99;
      --text-dim:  #3d4d6e;
      --mono:      'JetBrains Mono', monospace;
      --display:   'Syne', sans-serif;
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: var(--mono);
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
    }}

    /* subtle grid background */
    body::before {{
      content: '';
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(var(--border) 1px, transparent 1px),
        linear-gradient(90deg, var(--border) 1px, transparent 1px);
      background-size: 40px 40px;
      opacity: 0.35;
      pointer-events: none;
      z-index: 0;
    }}

    /* ── HEADER ── */
    header {{
      position: relative;
      z-index: 10;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 18px 32px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, #0d1220 0%, transparent 100%);
      backdrop-filter: blur(8px);
    }}

    .header-left {{
      display: flex;
      align-items: center;
      gap: 16px;
    }}

    .logo-icon {{
      width: 36px; height: 36px;
      border: 2px solid var(--cyan);
      border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 0 12px rgba(0,212,255,0.3);
    }}

    .logo-icon svg {{ width: 20px; height: 20px; fill: var(--cyan); }}

    .header-title {{
      font-family: var(--display);
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      color: #fff;
    }}

    .header-title span {{
      color: var(--cyan);
    }}

    .header-sub {{
      font-size: 0.7rem;
      color: var(--text-muted);
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-top: 1px;
    }}

    .header-right {{
      display: flex;
      align-items: center;
      gap: 20px;
    }}

    .device-badge {{
      display: flex;
      align-items: center;
      gap: 8px;
      background: var(--surface);
      border: 1px solid var(--border2);
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 0.72rem;
      color: var(--text-muted);
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }}

    .device-badge strong {{
      color: var(--cyan);
      font-weight: 500;
    }}

    #live-clock {{
      font-size: 0.72rem;
      color: var(--text-muted);
      letter-spacing: 0.08em;
    }}

    #refresh-indicator {{
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.68rem;
      color: var(--text-dim);
      letter-spacing: 0.08em;
    }}

    .pulse-dot {{
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 6px var(--green);
      animation: pulse 2s ease-in-out infinite;
    }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.4; transform: scale(0.7); }}
    }}

    /* ── MAIN LAYOUT ── */
    main {{
      position: relative;
      z-index: 1;
      padding: 28px 32px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }}

    /* ── STATUS ROW ── */
    .status-row {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 14px;
    }}

    .stat-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px 20px;
      position: relative;
      overflow: hidden;
      transition: border-color 0.2s;
    }}

    .stat-card:hover {{
      border-color: var(--border2);
    }}

    .stat-card::before {{
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 2px;
      background: var(--accent, var(--cyan));
      opacity: 0.7;
    }}

    .stat-label {{
      font-size: 0.65rem;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--text-muted);
      margin-bottom: 10px;
    }}

    .stat-value {{
      font-family: var(--display);
      font-size: 1.9rem;
      font-weight: 700;
      color: #fff;
      line-height: 1;
    }}

    .stat-value.unit {{
      font-size: 1.4rem;
    }}

    .stat-unit {{
      font-family: var(--mono);
      font-size: 0.75rem;
      font-weight: 300;
      color: var(--text-muted);
      margin-left: 4px;
    }}

    .stat-sub {{
      font-size: 0.65rem;
      color: var(--text-muted);
      margin-top: 6px;
    }}

    /* online status card */
    .status-online  {{ --accent: var(--green); }}
    .status-offline {{ --accent: var(--red); }}
    .status-door    {{ --accent: var(--amber); }}
    .status-rfid    {{ --accent: var(--cyan); }}
    .status-temp    {{ --accent: #ff7c5c; }}
    .status-humi    {{ --accent: #5ca8ff; }}
    .status-rssi    {{ --accent: var(--cyan-dim); }}

    .badge-online {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(0,229,160,0.1);
      border: 1px solid rgba(0,229,160,0.3);
      border-radius: 20px;
      padding: 4px 12px;
      font-size: 0.7rem;
      font-weight: 500;
      color: var(--green);
      letter-spacing: 0.08em;
    }}

    .badge-offline {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(255,77,106,0.1);
      border: 1px solid rgba(255,77,106,0.3);
      border-radius: 20px;
      padding: 4px 12px;
      font-size: 0.7rem;
      font-weight: 500;
      color: var(--red);
      letter-spacing: 0.08em;
    }}

    /* ── DATA ROW ── */
    .data-row {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
    }}

    /* ── PANEL ── */
    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }}

    .panel-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 20px;
      border-bottom: 1px solid var(--border);
      background: var(--surface2);
    }}

    .panel-title {{
      font-family: var(--display);
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .panel-title .dot {{
      width: 8px; height: 8px;
      border-radius: 50%;
      background: var(--panel-color, var(--cyan));
      box-shadow: 0 0 6px var(--panel-color, var(--cyan));
    }}

    .panel-count {{
      font-size: 0.65rem;
      color: var(--text-dim);
      letter-spacing: 0.08em;
    }}

    /* ── TABLE ── */
    .table-wrap {{
      overflow-x: auto;
      max-height: 340px;
      overflow-y: auto;
    }}

    .table-wrap::-webkit-scrollbar {{ width: 4px; height: 4px; }}
    .table-wrap::-webkit-scrollbar-track {{ background: var(--surface); }}
    .table-wrap::-webkit-scrollbar-thumb {{ background: var(--border2); border-radius: 4px; }}

    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.72rem;
    }}

    thead th {{
      position: sticky;
      top: 0;
      background: var(--surface2);
      padding: 10px 16px;
      text-align: left;
      font-size: 0.62rem;
      font-weight: 500;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }}

    tbody tr {{
      border-bottom: 1px solid rgba(31,40,64,0.6);
      transition: background 0.15s;
    }}

    tbody tr:hover {{
      background: var(--surface2);
    }}

    tbody tr:last-child {{
      border-bottom: none;
    }}

    td {{
      padding: 10px 16px;
      color: var(--text);
      white-space: nowrap;
    }}

    td.mono {{
      font-family: var(--mono);
      color: var(--cyan);
      font-size: 0.7rem;
    }}

    td.time {{
      color: var(--text-muted);
      font-size: 0.68rem;
    }}

    td.door {{
      text-transform: uppercase;
      font-size: 0.66rem;
      letter-spacing: 0.1em;
      color: var(--amber);
    }}

    /* badge pills */
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 10px;
      border-radius: 20px;
      font-size: 0.65rem;
      font-weight: 600;
      letter-spacing: 0.07em;
      text-transform: uppercase;
    }}

    .pill-green {{
      background: rgba(0,229,160,0.12);
      border: 1px solid rgba(0,229,160,0.25);
      color: var(--green);
    }}

    .pill-red {{
      background: rgba(255,77,106,0.12);
      border: 1px solid rgba(255,77,106,0.25);
      color: var(--red);
    }}

    .pill-amber {{
      background: rgba(255,184,48,0.12);
      border: 1px solid rgba(255,184,48,0.25);
      color: var(--amber);
    }}

    .pill-cyan {{
      background: rgba(0,212,255,0.1);
      border: 1px solid rgba(0,212,255,0.2);
      color: var(--cyan);
    }}

    /* ── SENSOR FULL PANEL ── */
    .panel-full {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }}

    /* ── EMPTY STATE ── */
    .empty {{
      padding: 40px 20px;
      text-align: center;
      color: var(--text-dim);
      font-size: 0.72rem;
      letter-spacing: 0.08em;
    }}

    /* ── FOOTER ── */
    footer {{
      position: relative;
      z-index: 1;
      padding: 16px 32px;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 0.65rem;
      color: var(--text-dim);
      letter-spacing: 0.08em;
    }}

    /* ── FADE-IN ANIMATION ── */
    @keyframes fadeUp {{
      from {{ opacity: 0; transform: translateY(10px); }}
      to   {{ opacity: 1; transform: translateY(0); }}
    }}

    .status-row {{ animation: fadeUp 0.4s ease both; }}
    .data-row   {{ animation: fadeUp 0.4s ease 0.1s both; }}
    .panel-full {{ animation: fadeUp 0.4s ease 0.2s both; }}

    /* ── RESPONSIVE ── */
    @media (max-width: 1100px) {{
      .status-row {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    @media (max-width: 720px) {{
      main {{ padding: 16px; }}
      header {{ padding: 12px 16px; flex-wrap: wrap; gap: 10px; }}
      .status-row {{ grid-template-columns: repeat(2, 1fr); }}
      .data-row {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 480px) {{
      .status-row {{ grid-template-columns: 1fr 1fr; }}
    }}
  </style>
</head>
<body>

<!-- ══ HEADER ══════════════════════════════════════════ -->
<header>
  <div class="header-left">
    <div class="logo-icon">
      <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <path d="M17 8C8 10 5.9 16.17 3.82 19.5c2.84-1.76 6.5-2.6 8.18-1.5C14 19.5 15 21 18 21c3 0 5-2 5-5 0-4-3-5-5-5-1.79 0-2.67.67-3.5 1.5C15.5 12 16 9 17 8z"/>
      </svg>
    </div>
    <div>
      <div class="header-title">Armadietto <span>IoT</span></div>
      <div class="header-sub">Monitor di sistema</div>
    </div>
  </div>
  <div class="header-right">
    <div class="device-badge">
      <span>Device</span>
      <strong id="device-id">{device_id}</strong>
    </div>
    <div id="live-clock">—</div>
    <div id="refresh-indicator">
      <div class="pulse-dot"></div>
      <span>LIVE</span>
    </div>
  </div>
</header>

<!-- ══ MAIN ════════════════════════════════════════════ -->
<main>

  <!-- STATUS CARDS -->
  <div class="status-row" id="status-row">
    <div class="stat-card status-online" id="card-online">
      <div class="stat-label">Stato dispositivo</div>
      <div class="stat-value" id="val-online">—</div>
      <div class="stat-sub" id="val-updated">—</div>
    </div>
    <div class="stat-card status-door" id="card-door">
      <div class="stat-label">Porta</div>
      <div class="stat-value" id="val-door">—</div>
    </div>
    <div class="stat-card status-temp">
      <div class="stat-label">Temperatura</div>
      <div class="stat-value unit" id="val-temp">—<span class="stat-unit">°C</span></div>
    </div>
    <div class="stat-card status-humi">
      <div class="stat-label">Umidità relativa</div>
      <div class="stat-value unit" id="val-humi">—<span class="stat-unit">%</span></div>
    </div>
    <div class="stat-card status-rfid">
      <div class="stat-label">Ultimo UID RFID</div>
      <div class="stat-value" style="font-size:1.1rem;font-family:var(--mono);color:var(--cyan)" id="val-uid">—</div>
      <div class="stat-sub" id="val-rfid-result">—</div>
    </div>
    <div class="stat-card status-rssi">
      <div class="stat-label">WiFi RSSI</div>
      <div class="stat-value unit" id="val-rssi">—<span class="stat-unit">dBm</span></div>
    </div>
  </div>

  <!-- DATA TABLES ROW -->
  <div class="data-row">

    <!-- EVENTI -->
    <div class="panel" style="--panel-color: var(--amber)">
      <div class="panel-header">
        <div class="panel-title"><span class="dot"></span> Log eventi</div>
        <div class="panel-count" id="events-count">0 righe</div>
      </div>
      <div class="table-wrap">
        <table id="events-table">
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Tipo</th>
              <th>Messaggio</th>
              <th>UID</th>
            </tr>
          </thead>
          <tbody><tr><td colspan="4" class="empty">In attesa di dati…</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- RFID -->
    <div class="panel" style="--panel-color: var(--cyan)">
      <div class="panel-header">
        <div class="panel-title"><span class="dot"></span> Accessi RFID</div>
        <div class="panel-count" id="rfid-count">0 righe</div>
      </div>
      <div class="table-wrap">
        <table id="rfid-table">
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>UID</th>
              <th>Esito</th>
              <th>Porta</th>
            </tr>
          </thead>
          <tbody><tr><td colspan="4" class="empty">In attesa di dati…</td></tr></tbody>
        </table>
      </div>
    </div>

  </div>

  <!-- SENSOR HISTORY -->
  <div class="panel-full">
    <div class="panel-header">
      <div class="panel-title" style="--panel-color:#ff7c5c"><span class="dot" style="background:#ff7c5c;box-shadow:0 0 6px #ff7c5c"></span> Storico sensori</div>
      <div class="panel-count" id="sensor-count">0 righe</div>
    </div>
    <div class="table-wrap">
      <table id="sensor-table">
        <thead>
          <tr>
            <th>Timestamp</th>
            <th>Temperatura (°C)</th>
            <th>Umidità (%)</th>
          </tr>
        </thead>
        <tbody><tr><td colspan="3" class="empty">In attesa di dati…</td></tr></tbody>
      </table>
    </div>
  </div>

</main>

<!-- ══ FOOTER ══════════════════════════════════════════ -->
<footer>
  <span>Armadietto IoT Dashboard — aggiornamento automatico ogni 5 s</span>
  <span id="last-refresh">—</span>
</footer>


<script>
const deviceId = document.getElementById("device-id").textContent;

/* ── Clock ── */
function tickClock() {{
  document.getElementById("live-clock").textContent =
    new Date().toLocaleTimeString("it-IT");
}}
tickClock();
setInterval(tickClock, 1000);

/* ── Helpers ── */
function fmtDate(s) {{
  if (!s) return "—";
  try {{
    return new Date(s).toLocaleString("it-IT", {{
      day: "2-digit", month: "2-digit", year: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit"
    }});
  }} catch {{ return s; }}
}}

function rssiBar(v) {{
  if (v == null) return "—";
  const pct = Math.max(0, Math.min(100, 2 * (v + 100)));
  const col = v > -65 ? "var(--green)" : v > -80 ? "var(--amber)" : "var(--red)";
  return `<span style="color:${{col}}">${{v}} dBm</span>`;
}}

/* ── Status ── */
async function loadStatus() {{
  try {{
    const res  = await fetch(`/api/status/${{deviceId}}`);
    const data = await res.json();

    /* online */
    const isOnline = data.online === true;
    document.getElementById("val-online").innerHTML = isOnline
      ? `<span class="badge-online"><span class="pulse-dot"></span>ONLINE</span>`
      : `<span class="badge-offline">OFFLINE</span>`;
    document.getElementById("card-online").className =
      `stat-card ${{isOnline ? "status-online" : "status-offline"}}`;
    document.getElementById("val-updated").textContent =
      data.updated_at ? "Agg. " + fmtDate(data.updated_at) : "—";

    /* door */
    const door = (data.door_state || "—").toUpperCase();
    document.getElementById("val-door").textContent = door;

    /* sensors */
    const t = data.last_temperature_c;
    const h = data.last_humidity_pct;
    document.getElementById("val-temp").innerHTML =
      t != null ? `${{t.toFixed(1)}}<span class="stat-unit">°C</span>` : `—<span class="stat-unit">°C</span>`;
    document.getElementById("val-humi").innerHTML =
      h != null ? `${{h.toFixed(1)}}<span class="stat-unit">%</span>` : `—<span class="stat-unit">%</span>`;

    /* rfid */
    document.getElementById("val-uid").textContent = data.last_rfid_uid || "—";
    const auth = data.last_rfid_authorized;
    document.getElementById("val-rfid-result").innerHTML =
      auth === true  ? `<span class="pill pill-green">✓ Autorizzato</span>` :
      auth === false ? `<span class="pill pill-red">✗ Negato</span>` : "—";

    /* rssi */
    const rssiEl = document.getElementById("val-rssi");
    const rssi = data.wifi_rssi;
    if (rssi != null) {{
      const col = rssi > -65 ? "var(--green)" : rssi > -80 ? "var(--amber)" : "var(--red)";
      rssiEl.innerHTML = `<span style="color:${{col}}">${{rssi}}</span><span class="stat-unit">dBm</span>`;
    }} else {{
      rssiEl.innerHTML = `—<span class="stat-unit">dBm</span>`;
    }}

  }} catch(e) {{
    console.warn("loadStatus error", e);
  }}
}}

/* ── Events ── */
async function loadEvents() {{
  try {{
    const res  = await fetch(`/api/events/${{deviceId}}?limit=20`);
    const data = await res.json();
    const tbody = document.querySelector("#events-table tbody");
    document.getElementById("events-count").textContent = data.length + " righe";
    if (!data.length) {{
      tbody.innerHTML = `<tr><td colspan="4" class="empty">Nessun evento registrato</td></tr>`;
      return;
    }}
    tbody.innerHTML = data.map(row => `
      <tr>
        <td class="time">${{fmtDate(row.timestamp)}}</td>
        <td>${{row.type ? `<span class="pill pill-cyan">${{row.type}}</span>` : "—"}}</td>
        <td style="color:var(--text-muted);font-size:0.7rem">${{row.message || "—"}}</td>
        <td class="mono">${{row.uid || "—"}}</td>
      </tr>
    `).join("");
  }} catch(e) {{
    console.warn("loadEvents error", e);
  }}
}}

/* ── RFID ── */
async function loadRFID() {{
  try {{
    const res  = await fetch(`/api/rfid/${{deviceId}}?limit=20`);
    const data = await res.json();
    const tbody = document.querySelector("#rfid-table tbody");
    document.getElementById("rfid-count").textContent = data.length + " righe";
    if (!data.length) {{
      tbody.innerHTML = `<tr><td colspan="4" class="empty">Nessun accesso RFID</td></tr>`;
      return;
    }}
    tbody.innerHTML = data.map(row => `
      <tr>
        <td class="time">${{fmtDate(row.timestamp)}}</td>
        <td class="mono">${{row.uid || "—"}}</td>
        <td>${{row.authorized
          ? `<span class="pill pill-green">✓ Auth</span>`
          : `<span class="pill pill-red">✗ Negato</span>`}}</td>
        <td class="door">${{row.door_state || "—"}}</td>
      </tr>
    `).join("");
  }} catch(e) {{
    console.warn("loadRFID error", e);
  }}
}}

/* ── Sensors ── */
async function loadSensors() {{
  try {{
    const res  = await fetch(`/api/sensors/${{deviceId}}?limit=50`);
    const data = await res.json();
    const tbody = document.querySelector("#sensor-table tbody");
    document.getElementById("sensor-count").textContent = data.length + " righe";
    if (!data.length) {{
      tbody.innerHTML = `<tr><td colspan="3" class="empty">Nessun dato sensore</td></tr>`;
      return;
    }}
    tbody.innerHTML = data.map(row => {{
      const t = row.temperature_c;
      const h = row.humidity_pct;
      const tCol = t != null
        ? (t > 30 ? "var(--red)" : t > 25 ? "var(--amber)" : "var(--text)")
        : "var(--text-dim)";
      const hCol = h != null
        ? (h > 70 ? "#5ca8ff" : h > 50 ? "var(--cyan-dim)" : "var(--text)")
        : "var(--text-dim)";
      return `
        <tr>
          <td class="time">${{fmtDate(row.timestamp)}}</td>
          <td style="color:${{tCol}};font-weight:500">${{t != null ? t.toFixed(2) + " °C" : "—"}}</td>
          <td style="color:${{hCol}};font-weight:500">${{h != null ? h.toFixed(2) + " %" : "—"}}</td>
        </tr>
      `;
    }}).join("");
  }} catch(e) {{
    console.warn("loadSensors error", e);
  }}
}}

/* ── Refresh all ── */
async function refreshAll() {{
  await Promise.all([loadStatus(), loadEvents(), loadRFID(), loadSensors()]);
  document.getElementById("last-refresh").textContent =
    "Ultimo aggiornamento: " + new Date().toLocaleTimeString("it-IT");
}}

refreshAll();
setInterval(refreshAll, 5000);
</script>
</body>
</html>
"""
