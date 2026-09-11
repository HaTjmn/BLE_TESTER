import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from holter_ble_client import HolterBLEClient, ControllerError
from holter_hlv_protocol import (
    HLV_CONTROL_POINT_UUID, HLV_COMMAND_RESPONSE_UUID, HLV_STATUS_UUID,
    HLV_OPCODE_START_FILE_DOWNLOAD, HLV_RESPONSE_PHASE_ACCEPTED, HLV_RESPONSE_PHASE_REJECTED,
    encode_hlv_message, decode_command_response, decode_status_bytes,
    wait_for_command_response, authenticate_raw,
)

def connect_and_prepare(client):
    client.send("reset_controller", timeout=5)
    client.send("connect", timeout=12, name="NOAMED", timeout_ms=8000)
    client.send("discover_services", timeout=10)
    client.send("discover_characteristics", timeout=10)
    client.send("subscribe", timeout=6, characteristic=HLV_COMMAND_RESPONSE_UUID, mode="notify")
    client.send("subscribe", timeout=6, characteristic="7b8e0046-6a52-4f4d-9a33-71c02e6b4d90", mode="notify")
    client.drain_events()
    base_txn = int(time.time() * 1000) & 0x7FFFFFFF
    status = authenticate_raw(client, base_txn)
    return status, base_txn

client = HolterBLEClient(sys.argv[1], baud=115200, default_timeout=15)
try:
    status, base_txn = connect_and_prepare(client)
    print("post-connect status:", status)

    txn = base_txn + 1
    # file_index=2 is a known 0-byte file (from the earlier LIST_FILES scan) -- verifies
    # admission logic accepts a brand-new transfer without any real data movement.
    msg = encode_hlv_message(HLV_OPCODE_START_FILE_DOWNLOAD, txn, (2).to_bytes(2, "little") + (0).to_bytes(4, "little"))
    client.send("write", timeout=6, characteristic=HLV_CONTROL_POINT_UUID, data_hex=msg.hex())
    resp = wait_for_command_response(client, txn, window_s=5)
    print("START_FILE_DOWNLOAD(file_index=2) response:", resp)
    if resp is None:
        print("VERDICT: NOT YET PROVEN -- no response received")
    elif resp["phase"] == HLV_RESPONSE_PHASE_REJECTED and resp["result_code"] == 15:
        print("VERDICT: STUCK -- device reports TRANSFER_BUSY (result_code=15), a prior transfer is still considered active")
    elif resp["phase"] == HLV_RESPONSE_PHASE_REJECTED:
        print(f"VERDICT: NOT YET PROVEN -- rejected for an unrelated reason (result_code={resp['result_code']}), inconclusive")
    else:
        print("VERDICT: PROVEN CLEAN -- device accepted a new file-transfer request normally (ACCEPTED, not TRANSFER_BUSY)")

finally:
    client.close()
