import asyncio
import sys
from netaudio import DanteBrowser

async def main():
    # Initialize with the required timeout
    browser = DanteBrowser(mdns_timeout=3.0)
    
    print("Searching for Dante devices and syncing status...")
    
    # Wait for mDNS discovery
    await asyncio.sleep(2.5)
    
    devices_data = await browser.get_devices()
    
    if not devices_data:
        print("No devices found. Check your network connection.")
        return

    # Convert to a list of device objects
    device_list = list(devices_data.values()) if isinstance(devices_data, dict) else list(devices_data)

    # Sync each device to populate names and clock data
    print("Fetching detailed clock data...")
    sync_tasks = [device.sync() for device in device_list if hasattr(device, "sync")]
    if sync_tasks:
        await asyncio.gather(*sync_tasks)

    # Display Header
    print(f"\n{'ID':<3} {'Device Name':<25} {'Role':<12} {'Pref. Leader'}")
    print("-" * 65)
    
    for idx, device in enumerate(device_list):
        # Using mac_address as a fallback since ip_address failed
        identifier = getattr(device, 'mac_address', 'Unknown ID')
        name = device.name if device.name else f"Unresolved ({identifier})"
        
        # Check clock status
        # Note: some versions use .is_leader, others use .clock_role
        is_leader = getattr(device, 'is_leader', False)
        role = "Leader" if is_leader else "Follower"
        
        is_pref = getattr(device, 'preferred_leader', False)
        pref = "YES" if is_pref else "no"
        
        print(f"{idx:<3} {name:<25} {role:<12} {pref}")

    # Interaction
    try:
        user_input = input("\nEnter ID to toggle 'Preferred Leader' (or 'q' to quit): ").strip().lower()
        if user_input == 'q':
            return
        
        idx = int(user_input)
        target = device_list[idx]
        
        current_pref = getattr(target, 'preferred_leader', False)
        new_val = not current_pref
        
        print(f"Updating {target.name} Preferred Leader to {new_val}...")
        
        # In the latest netaudio, configuration changes are usually methods
        if hasattr(target, 'set_preferred_leader'):
            await target.set_preferred_leader(new_val)
        else:
            # Fallback for property-based versions
            target.preferred_leader = new_val
        
        print("Change requested. It may take a few seconds for the network to update.")
        
    except (ValueError, IndexError):
        print("Invalid selection.")
    except Exception as e:
        print(f"An error occurred during update: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
