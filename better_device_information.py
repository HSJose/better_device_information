import httpx
from datetime import datetime
from dotenv import load_dotenv
from rich import print
import os
import re
import json
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import sessionmaker, DeclarativeBase

load_dotenv()

# --- Global variables ---
api_key = os.getenv('API_KEY')
header = { 'Authorization': f'Bearer {api_key}' }
api_base = 'api-dev.headspin.io'
api_devices_info = '/v0/devices'
api_devices_team_info = '/v0/teams/devices'
api_teams_info = '/v0/teams'

# --------------------------
# Database Setup
# --------------------------

DB_TYPE = os.getenv("DB_TYPE", "SQLITE").upper()

if DB_TYPE == "REDSHIFT":
    # Build the Redshift connection string from environment variables
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT")
    dbname = os.getenv("DB_NAME")
    
    if not all([user, password, host, port, dbname]):
        raise ValueError("Missing one or more Redshift database environment variables (DB_USER, DB_PASSWORD, etc.)")
        
    connection_string = f"redshift+psycopg2://{user}:{password}@{host}:{port}/{dbname}"
    print(f"[bold blue]Connecting to Redshift database at {host}...[/bold blue]")
else:
    # Default to a persistent SQLite file for local development/testing
    connection_string = 'sqlite:///device_inventory.db'
    print("[bold blue]Using local SQLite database (device_inventory.db)...[/bold blue]")

engine = create_engine(connection_string)

class Base(DeclarativeBase):
    pass

class AVBoxMapping(Base):
    __tablename__ = 'avbox_mapping'
    id = Column(Integer, primary_key=True)
    device_type = Column(String)
    device_notes = Column(String)
    dut = Column(String, unique=True)
    camera_device = Column(String)
    control = Column(String)

    def __repr__(self):
        return (f"<AVBoxMapping(\n"
                f"  Device Type='{self.device_type}',\n"
                f"  device_notes='{self.device_notes}',\n"
                f"  DUT='{self.dut}',\n"
                f"  camera_device='{self.camera_device}',\n"
                f"  control='{self.control}'\n"
                f")>")

class DeviceInventory(Base):
    __tablename__ = 'device_inventory'
    id = Column(Integer, primary_key=True)
    device_type = Column(String)
    model = Column(String)
    device_skus = Column(String)
    udid = Column(String, unique=True, index=True)
    host_name = Column(String)
    os_version = Column(String)
    location = Column(String)
    device_notes = Column(String) 
    teams = Column(String)
    is_avbox = Column(Boolean)

    def __repr__(self):
        return (f"<DeviceInventory(\n"
                f"  Device Type='{self.device_type}',\n"
                f"  Model='{self.model}',\n"
                f"  Device Skus='{self.device_skus}',\n"
                f"  UDID='{self.udid}',\n"
                f"  Host Name='{self.host_name}',\n"
                f"  OS Version='{self.os_version}',\n"
                f"  Location='{self.location}',\n"
                f"  device_notes='{self.device_notes}',\n" 
                f"  Teams='{self.teams}',\n"
                f"  Is AVBox={self.is_avbox}\n"
                f")>")

class DeviceLedger(Base):
    __tablename__ = 'device_ledger'
    id = Column(Integer, primary_key=True)
    udid = Column(String, index=True)
    status = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)
    details = Column(String)

    def __repr__(self):
        return (f"<DeviceLedger(Timestamp='{self.timestamp.strftime('%Y-%m-%d %H:%M')}', "
                f"UDID='{self.udid}', Status='{self.status}', Details='{self.details}')>")

engine = create_engine('sqlite:///:memory:')
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# --------------------------
# Helper Functions
# --------------------------
def fetch_data(api_url):
    timeout = httpx.Timeout(60.0, connect=60.0)
    with httpx.Client(timeout=timeout) as client:
        response = client.get(api_url, headers=header)
        if response.status_code == 200: return response.json()
        else:
            print(f"Failed to fetch data from {api_url}: {response.status_code} - {response.text}")
            return None

def get_effective_device_type(device):
    device_subtype = device.get("device_subtype")
    device_type = device.get("device_type", "")
    if device_subtype: return device_subtype
    if device_type.lower() in ["chrome", "firefox", "safari"]: return "browser"
    return device_type

def get_unique_device_id(device):
    if get_effective_device_type(device) == 'browser':
        return device.get('device_address')
    return device.get('device_id')

def get_device_teams(device_address, teams_list):
    device_teams = []
    if not teams_list or not teams_list.get("devices"): return []
    for device in teams_list["devices"]:
        if device_address == device.get("device_address"):
            for team in device.get("teams", []):
                if team["team_name"] not in device_teams:
                    device_teams.append(team["team_name"])
    return device_teams

def device_details_for_db(device, teams_list):
    device_teams = get_device_teams(device.get("device_address"), teams_list)
    skus = device.get("device_skus")
    return {
        "device_type": get_effective_device_type(device),
        "model": device.get("model"),
        "device_skus": ', '.join(skus) if isinstance(skus, list) else skus,
        "udid": get_unique_device_id(device),
        "host_name": device.get("hostname"),
        "os_version": device.get("os_version"),
        "location": f'{device.get("host_city", "")}, {device.get("host_country", "")}',
        "device_notes": device.get("device_note"), 
        "teams": ', '.join(device_teams) if device_teams else '',
        "is_avbox": True if device.get("avbox_info") else False,
    }

def safe_get_device(device_list, index):
    try: return device_list[index]
    except IndexError: return None

def add_avbox_mapping_entry(device, AVBoxs: dict):
    avbox_info = device.get("avbox_info")
    if not avbox_info or not avbox_info.get("usage") or not avbox_info.get("devices"): return
    usage = avbox_info.get("usage")
    avbox_devices = avbox_info.get("devices")
    dut_address = None
    if "device_under_test" in usage: dut_address = device.get("device_address")
    elif avbox_devices: dut_address = safe_get_device(avbox_devices, 0)
    if not dut_address: return
    if dut_address not in AVBoxs:
        AVBoxs[dut_address] = {"dut": dut_address}
    if "device_under_test" in usage:
        AVBoxs[dut_address].update({
            "device_type": device.get("device_subtype") or device.get("device_type"),
            "device_notes": device.get("device_note") 
        })
    elif "camera_device" in usage: AVBoxs[dut_address]["camera_device"] = device.get("device_address")
    elif "control" in usage: AVBoxs[dut_address]["control"] = device.get("device_address")

# --------------------------
# Main Function
# --------------------------
def main() -> None:
    session = Session()
    try:
        AVBoxs_data = {}
        print("[bold green]Fetching device data from API...[/bold green]")
        devices_list_raw = fetch_data(f'https://{api_key}@{api_base}{api_devices_info}')
        devices_team_list = fetch_data(f'https://{api_key}@{api_base}{api_devices_team_info}')
        if not devices_list_raw or not devices_list_raw.get("devices"):
            print("[bold red]No devices found.[/bold red]")
            return

        for device in devices_list_raw["devices"]:
            if device.get("avbox_info"):
                add_avbox_mapping_entry(device, AVBoxs_data)

        api_unique_ids = {get_unique_device_id(device) for device in devices_list_raw['devices']}
        db_unique_ids = {uid for (uid,) in session.query(DeviceInventory.udid).all()}
        added_ids = api_unique_ids - db_unique_ids
        removed_ids = db_unique_ids - db_unique_ids

        if added_ids: print(f"[cyan]New devices to be added: {len(added_ids)}[/cyan]")
        if removed_ids: print(f"[cyan]Devices to be removed: {len(removed_ids)}[/cyan]")

        for unique_id in removed_ids:
            device_to_remove = session.query(DeviceInventory).filter_by(udid=unique_id).one()
            details = {"model": device_to_remove.model, "type": device_to_remove.device_type}
            avbox_map = session.query(AVBoxMapping).filter_by(dut=unique_id).first()
            if avbox_map:
                details["companions"] = {"camera": avbox_map.camera_device, "control": avbox_map.control}
            session.add(DeviceLedger(udid=unique_id, status='removed', details=json.dumps(details)))
            session.delete(device_to_remove)

        print(f"[bold green]Processing {len(devices_list_raw['devices'])} total devices...[/bold green]")
        for device in devices_list_raw["devices"]:
            avbox_info = device.get("avbox_info")
            if not avbox_info or (avbox_info.get("usage") == "device_under_test"):
                device_inventory_data = device_details_for_db(device, devices_team_list)
                unique_id = device_inventory_data['udid']
                
                existing_device = session.query(DeviceInventory).filter_by(udid=unique_id).first()
                if existing_device:
                    for key, value in device_inventory_data.items(): setattr(existing_device, key, value)
                else:
                    session.add(DeviceInventory(**device_inventory_data))

                if unique_id in added_ids:
                    details = {"model": device.get('model'), "type": get_effective_device_type(device)}
                    avbox_map = AVBoxs_data.get(device['device_address'])
                    if avbox_map:
                        details["companions"] = {"camera": avbox_map.get("camera_device"), "control": avbox_map.get("control")}
                    session.add(DeviceLedger(udid=unique_id, status='added', details=json.dumps(details)))
        
        for dut_udid, avbox_map_data in AVBoxs_data.items():
            existing_map = session.query(AVBoxMapping).filter_by(dut=dut_udid).first()
            if existing_map:
                for key, value in avbox_map_data.items(): setattr(existing_map, key, value)
            else:
                session.add(AVBoxMapping(**avbox_map_data))
                
        session.commit()
        print("[bold green]Database has been synchronized successfully.[/bold green]")
        
        # --- Inspection Section ---
        inventory_count = session.query(DeviceInventory).count()
        avbox_count = session.query(AVBoxMapping).count()
        ledger_count = session.query(DeviceLedger).count()
        print(f"\n[bold yellow]Total records in Device_Inventory: {inventory_count}[/bold yellow]")
        print(f"[bold yellow]Total records in AVBox_mapping: {avbox_count}[/bold yellow]")
        print(f"[bold yellow]Total records in Device_Ledger: {ledger_count}[/bold yellow]")

        browser_count = session.query(DeviceInventory).filter(DeviceInventory.device_type == 'browser').count()
        print(f"[bold yellow]Total browser devices: {browser_count}[/bold yellow]")

        # list all browsers
        browsers = session.query(DeviceInventory).filter(DeviceInventory.device_type == 'browser').all()
        if browsers:
            print("\n[bold magenta]--- Browser Devices ---[/bold magenta]")
            for browser in browsers: print(browser)
        else:
            print("[yellow]No browser devices found.[/yellow]")

        print("\n[bold cyan]--- Inspecting 10 Most Recent DeviceInventory Records ---[/bold cyan]")
        inv_sample = session.query(DeviceInventory).order_by(DeviceInventory.id.desc()).limit(10).all()
        if inv_sample:
            for entry in inv_sample: print(entry)
        else:
            print("[yellow]No entries found in Device_Inventory.[/yellow]")

        print("\n[bold cyan]--- Inspecting 10 Most Recent AVBoxMapping Records ---[/bold cyan]")
        avbox_sample = session.query(AVBoxMapping).order_by(AVBoxMapping.id.desc()).limit(10).all()
        if avbox_sample:
            for entry in avbox_sample: print(entry)
        else:
            print("[yellow]No entries found in AVBox_mapping.[/yellow]")

        print("\n[bold cyan]--- Inspecting 10 Most Recent DeviceLedger Records ---[/bold cyan]")
        ledger_sample = session.query(DeviceLedger).order_by(DeviceLedger.timestamp.desc()).limit(10).all()
        if ledger_sample:
            for entry in ledger_sample: print(entry)
        else:
            print("[yellow]No entries found in Device_Ledger.[/yellow]")
            
    except Exception as e:
        print(f"[bold red]An error occurred: {e}[/bold red]")
        session.rollback()
    finally:
        session.close()

if __name__ == '__main__':
    main()
