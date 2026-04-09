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

    return f"""
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <title>Armadietto IoT Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; background: #f4f6f8; color: #222; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }}
    .card {{ background: white; border-radius: 12px; padding: 16px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #ddd; }}
    .mono {{ font-family: monospace; font-size: 13px; }}
    .ok {{ color: green; font-weight: bold; }}
    .bad {{ color: #b00020; font-weight: bold; }}
  </style>
</head>
<body>
  <h1>Dashboard Armadietto IoT</h1>
  <p>Device: <strong id="device-id">{device_id}</strong></p>

  <div class="grid">
    <div class="card">
      <h2>Stato corrente</h2>
      <div id="status">Caricamento...</div>
    </div>

    <div class="card">
      <h2>Ultimi sensori</h2>
      <div id="latest-sensors">Caricamento...</div>
    </div>
  </div>

  <div class="grid" style="margin-top:16px;">
    <div class="card">
      <h2>Ultimi eventi</h2>
      <table id="events-table">
        <thead><tr><th>Ora</th><th>Tipo</th><th>Messaggio</th><th>UID</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Ultimi RFID</h2>
      <table id="rfid-table">
        <thead><tr><th>Ora</th><th>UID</th><th>Esito</th><th>Porta</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="card" style="margin-top:16px;">
    <h2>Storico sensori</h2>
    <table id="sensor-table">
      <thead><tr><th>Ora</th><th>Temperatura</th><th>Umidità</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

<script>
const deviceId = document.getElementById("device-id").textContent;

function fmtDate(s) {{
  try {{ return new Date(s).toLocaleString(); }}
  catch {{ return s; }}
}}

async function loadStatus() {{
  const res = await fetch(`/api/status/${{deviceId}}`);
  const data = await res.json();

  document.getElementById("status").innerHTML = `
    <p>Online: <span class="${{data.online ? 'ok' : 'bad'}}">${{data.online ? 'SI' : 'NO'}}</span></p>
    <p>Porta: <strong>${{data.door_state || '-'}}</strong></p>
    <p>Temperatura: <strong>${{data.last_temperature_c ?? '-'}} °C</strong></p>
    <p>Umidità: <strong>${{data.last_humidity_pct ?? '-'}} %</strong></p>
    <p>Ultimo UID: <span class="mono">${{data.last_rfid_uid || '-'}}</span></p>
    <p>Ultimo esito RFID: <strong>${{data.last_rfid_authorized === true ? 'AUTORIZZATO' : data.last_rfid_authorized === false ? 'NEGATO' : '-'}}</strong></p>
    <p>RSSI WiFi: <strong>${{data.wifi_rssi ?? '-'}} dBm</strong></p>
    <p>Aggiornato: <strong>${{data.updated_at ? fmtDate(data.updated_at) : '-'}}</strong></p>
  `;

  document.getElementById("latest-sensors").innerHTML = `
    <p>Ultima temperatura: <strong>${{data.last_temperature_c ?? '-'}} °C</strong></p>
    <p>Ultima umidità: <strong>${{data.last_humidity_pct ?? '-'}} %</strong></p>
  `;
}}

async function loadEvents() {{
  const res = await fetch(`/api/events/${{deviceId}}?limit=20`);
  const data = await res.json();
  const tbody = document.querySelector("#events-table tbody");
  tbody.innerHTML = data.map(row => `
    <tr>
      <td>${{fmtDate(row.timestamp)}}</td>
      <td>${{row.type || ''}}</td>
      <td>${{row.message || ''}}</td>
      <td class="mono">${{row.uid || ''}}</td>
    </tr>
  `).join("");
}}

async function loadRFID() {{
  const res = await fetch(`/api/rfid/${{deviceId}}?limit=20`);
  const data = await res.json();
  const tbody = document.querySelector("#rfid-table tbody");
  tbody.innerHTML = data.map(row => `
    <tr>
      <td>${{fmtDate(row.timestamp)}}</td>
      <td class="mono">${{row.uid || ''}}</td>
      <td>${{row.authorized ? 'AUTORIZZATO' : 'NEGATO'}}</td>
      <td>${{row.door_state || ''}}</td>
    </tr>
  `).join("");
}}

async function loadSensors() {{
  const res = await fetch(`/api/sensors/${{deviceId}}?limit=50`);
  const data = await res.json();
  const tbody = document.querySelector("#sensor-table tbody");
  tbody.innerHTML = data.map(row => `
    <tr>
      <td>${{fmtDate(row.timestamp)}}</td>
      <td>${{row.temperature_c ?? '-'}} °C</td>
      <td>${{row.humidity_pct ?? '-'}} %</td>
    </tr>
  `).join("");
}}

async function refreshAll() {{
  await Promise.all([loadStatus(), loadEvents(), loadRFID(), loadSensors()]);
}}

refreshAll();
setInterval(refreshAll, 5000);
</script>
</body>
</html>
"""
