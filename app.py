import socket
import threading
import time
import datetime
import re
import logging
from flask import Flask, jsonify, render_template
from threading import Thread
import pymysql
import csv
import requests
import io
from io import StringIO
import atexit
from dotenv import load_dotenv
import os

load_dotenv()

HOST = "aprs.glidernet.org"
PORT = 14580
SKIP_STATS_DATABASE = False

# Dictionary in memory: device_id -> latest packet data
ads_l_devices = {}

app = Flask(__name__)
logging.basicConfig(level=logging.ERROR)
listener_started = False

# Database connection for statistics
conn = None

# Global mapping: device_id -> aircraft type
device_type_map = {}

# URL of the OGN device database
DEVICE_TYPE_URL = "https://ddb.glidernet.org/download/"





# ---  SUPPORT FUNCTIONS ---

def get_aircraft_type_description(symbol1, symbol2):
    # Mappa per symbol1 \
    alternative_map = {
        "\\": "Drop plane",
        "^": "Powered aircraft"
    }

    # Mappa predefinita 
    default_map = {
        "z": "Unknown",
        "'": "Glider",
        "X": "Helicopter",
        "g": "Paraglider or hang-glider",
        "^": "Jet aircraft",
        "z": "UFO",
        "\\": "Drop plane",
        "O": "Balloon"
    }

    if symbol1 == "/":
        current_map = default_map
    elif symbol1 == "\\":
        current_map = alternative_map
    else:
        current_map = default_map

    description = current_map.get(symbol2, "Unknown")

    return description



def get_db_connection(max_retries=30, retry_delay=10):
    if SKIP_STATS_DATABASE == True:
        return None
    retries = 0
    while retries < max_retries:
        print("Attempting to connect to db.")
        try:
            conn = pymysql.connect(
                host="localhost",
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD"),
                database="ads_l",
                autocommit=True
            )
            print("Connection to database established.")
            return conn
        except pymysql.MySQLError as e:
            retries += 1
            print(f"Error connecting database (attempt {retries}/{max_retries}): {e}")
            if retries < max_retries:
                print(f"New attempt in {retry_delay} seconds...")
                time.sleep(retry_delay)
    raise Exception("Cannot connect to database after many attempts.")

def record_monthly_device(device_id, device_type):
    global conn
    max_retries = 3
    retry_delay = 3

    for attempt in range(max_retries):
        try:
            if conn is not None:
                with conn.cursor() as cur:
                    month = datetime.datetime.utcnow().strftime("%Y-%m")
                    now = datetime.datetime.utcnow()
                    cur.execute(
                        """
                        INSERT IGNORE INTO monthly_devices
                        (month, device_id, device_type, first_seen)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (month, device_id, device_type, now)
                    )
                break  # Se tutto va bene, esci dal loop
        except pymysql.MySQLError as e:
            print(f"Error writing to database (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                print(f"Will retry connecting in {retry_delay} second...")
                time.sleep(retry_delay)
                conn = get_db_connection()  # Riconnetti al database
            else:
                print("Cannot write to database after many attempts.")



def connect_ogn():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
    except AttributeError:
        pass  # non Linux
    s.settimeout(10)	
    s.connect((HOST, PORT))
    login_line = f"user ADSLMAP-1 pass -1 vers ADS-L webmap 1.0 filter r/0/0/20000\n"
    s.send(login_line.encode())
    print("OGN connection established")
    return s


def parse_aprs_line(line):
    global device_type_map
    try:
        device_id, rest = line.split('>', 1)
        device_id = device_id.strip()

        # Station and type
        m_station = re.match(r'^\w+,([^,]+),([^:/]+):', rest)
        if m_station:
            routing_info, station = m_station.groups()
        else:
            station = routing_info = None

        # Lat/Lon and timestamp GPS
        m_gps = re.search(r'/(\d{2})(\d{2})(\d{2})h(\d{2,3})(\d{2}\.\d+)([NS])([\\/])(\d{2,3})(\d{2}\.\d+)([EW])(.)', line)
        if m_gps:
            hh, mm, ss, deg_lat, min_lat, ns, symbol1, deg_lon, min_lon, ew, symbol2 = m_gps.groups()
            lat = int(deg_lat) + float(min_lat)/60
            lat = lat if ns=='N' else -lat
            lon = int(deg_lon) + float(min_lon)/60
            lon = lon if ew=='E' else -lon
            aircraft_aprs = get_aircraft_type_description(symbol1, symbol2)
        else:
            lat = lon = None
            aircraft_aprs = "Unknown"

        # Heading and speed
        m_heading = re.search(r'[EW][\^X\'Ozg>\\\!](\d{3})\/(\d{3})\/', line)
        heading = int(m_heading.group(1)) if m_heading else None
        speed = int(m_heading.group(2)) if m_heading else None

        # Altitude in feet
        alt_match = re.search(r'A=(\d+)', line)
        if alt_match:
            altitude = float(alt_match.group(1))
        else:
            fl_match = re.search(r'FL?(\d+\.?\d*)', line)
            altitude = float(fl_match.group(1))*100 if fl_match else None

        # Vertical speed
        vs_match = re.search(r'([+-]?\d+)fpm', line)
        vspeed = int(vs_match.group(1)) if vs_match else None

        # Flight / callsign
        flight_match = re.search(r'A3:([^\s]+)', line)
        flight = flight_match.group(1) if flight_match else None

        # Signal dB
        sig_match = re.search(r'(-?\d+\.?\d*)dB', line)
        signal = float(sig_match.group(1)) if sig_match else None

        # Offset frequency
        freq_match = re.search(r'([+-]?\d+\.\d+)kHz', line)
        freq_offset = float(freq_match.group(1)) if freq_match else None

        # GPS fix / satellites
        gps_match = re.search(r'gps(\d+)x(\d+)', line)
        gps_fix = int(gps_match.group(1)) if gps_match else None
        gps_sats = int(gps_match.group(2)) if gps_match else None

        # packet ID
        id_match = re.search(r'id([A-F0-9]+)', line)
        pkt_id = id_match.group(1) if id_match else None

        # Signal quality
        qual_match = re.search(r'!W(\d+)!', line)
        quality = int(qual_match.group(1)) if qual_match else None

        raw_id = device_id
        lookup_id = raw_id[3:] if raw_id.startswith("OGN") else raw_id
        aircraft_type = device_type_map.get(lookup_id, aircraft_aprs)

        return {
            "device_id": device_id,
			"aircraft_type": aircraft_type,
            "routing_info": routing_info,
            "station": station,
            "lat": lat,
            "lon": lon,
            "heading": heading,
            "speed": speed,
            "altitude": altitude,
            "vspeed": vspeed,
            "flight": flight,
            "signal": signal,
            "freq_offset": freq_offset,
            "gps_fix": gps_fix,
            "gps_sats": gps_sats,
            "pkt_id": pkt_id,
            "quality": quality,
            "timestamp": datetime.datetime.utcnow(),
            "raw": line
        }
    except Exception as e:
        logging.error("parse_aprs_line failed: %s", e)
        return None


def ads_l_listener():
    global ads_l_devices
    while True:
        try:
            s = connect_ogn()
            s.settimeout(30)
            buffer = ""
            last_rx = time.time()

            while True:
                data = s.recv(4096)
                if not data:
                    raise ConnectionError("No data from OGN for 60s")

                last_rx = time.time()
                buffer += data.decode(errors="ignore")

                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if '>OGADSL' in line:
                        pkt = parse_aprs_line(line)
                        logging.warning("[RAW] %s", line)
                        logging.warning("[PARSED] %s", pkt)
                        if pkt and pkt["lat"] is not None and pkt["lon"] is not None:
                            ads_l_devices[pkt["device_id"]] = pkt
                            record_monthly_device(
                                pkt["device_id"],
                                "ADSL"  # ADSL / ADSB / FLARM
                            )
                    else:
                        logging.debug("[RAW] %s", line)
                if time.time() - last_rx > 60:
                    raise ConnectionError("OGN feed stalled")

        except (socket.timeout, ConnectionError) as e:
            print("Connetion error:", e)
            try:
                s.close()
            except:
                pass
            time.sleep(5)
            continue


def update_device_type_map():
    global device_type_map
    try:
        r = requests.get(DEVICE_TYPE_URL)
        r.raise_for_status()
        text = r.text

        reader = csv.DictReader(StringIO(text))
        new_map = {}
        for row in reader:
            device_id = row["DEVICE_ID"].strip().strip("'")  # remove quotes
            aircraft_model = row["AIRCRAFT_MODEL"].strip().strip("'")
            registration = row["REGISTRATION"].strip().strip("'")
            new_map[device_id] = aircraft_model + " (" + registration + ")" if aircraft_model else "Unknown"

        device_type_map = new_map
        print(f"[Device map] Loaded {len(device_type_map)} entries")

    except Exception as e:
        print("Error updating device type map:", e)

def periodic_device_type_update(interval=3600):
    """Update the device type map every interval seconds."""
    while True:
        update_device_type_map()
        time.sleep(interval)




def prune_old_devices():
    global ads_l_devices
    while True:
        time.sleep(60)
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=60)
        ads_l_devices = {k:v for k,v in ads_l_devices.items() if v["timestamp"] > cutoff}

def close_db(exception):
    global conn
    if conn is not None:
        print("Closing DB")
        try:
            # Only close if there's no active transaction
            if not conn.in_transaction():
                conn.close()
        except Exception as e:
            print(f"Error closing database connection: {e}")
        finally:
            conn = None

def start_listener():
    """Start the APRS listener in a background thread."""
    print("Starting APRS listener thread...")
    listener_thread = Thread(target=ads_l_listener, daemon=True)
    listener_thread.start()
    print(f"Listener thread started with ID: {listener_thread.ident}")
    return listener_thread

# --- ROUTES FLASK ---
@app.route("/ads-l-map")
def index():
    return render_template("map.html")

@app.route("/ads-l/")
def get_ads_l():
    out = []
    for v in ads_l_devices.values():
        entry = v.copy()
        entry["timestamp"] = entry["timestamp"].isoformat()
        out.append(entry)
    return jsonify(out)

@app.route("/ads-l/stats")
def ads_l_stats():
    global conn
    if conn is None:
        print("No database connection")
        return '[]'
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        # count unique monthly devices
        cur.execute("""

            SELECT month, COUNT(DISTINCT device_id) AS devices
                FROM monthly_devices
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """)
        results = cur.fetchall()
    return jsonify(results)

@app.route("/device-map")
def show_device_map():
        return jsonify(device_type_map)

def bootstrap():
    global listener_started, conn
    if listener_started:
        return

    print("Bootstrapping ADS-L listener")

    conn = get_db_connection()

    Thread(target=ads_l_listener, daemon=True).start()
    Thread(target=periodic_device_type_update, daemon=True).start()
    Thread(target=prune_old_devices, daemon=True).start()

    listener_started = True

# Register the close_db function to run when the application exits
atexit.register(close_db)

bootstrap()

# --- MAIN ---
if __name__ == "__main__":
    try:
        app.run()
    finally:
        close_db()

