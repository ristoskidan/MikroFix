#!/usr/bin/env python3
"""Majhen trajni zapisovalnik stanja računalnika za iskanje zamrznitev."""

from __future__ import annotations

import csv
import datetime as dt
import glob
import os
from pathlib import Path
import subprocess
import time


INTERVAL_SECONDS = 5
STATE_DIR = Path.home() / ".local" / "state" / "mikrofix-diagnostics"
LOG_PATH = STATE_DIR / "hardware-watch-v3.csv"


def read_text(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return default


def read_number(path: str | Path, divisor: float = 1.0) -> float | None:
    try:
        return float(read_text(path)) / divisor
    except (TypeError, ValueError):
        return None


def thermal_value(kind: str) -> float | None:
    values = []
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        zone_type = read_text(Path(zone) / "type").lower()
        if kind in zone_type:
            value = read_number(Path(zone) / "temp", 1000.0)
            if value is not None:
                values.append(value)
    return max(values) if values else None


def gpu_values() -> tuple[float | None, float | None, str]:
    for hwmon in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*"):
        if read_text(Path(hwmon) / "name").lower() == "radeon":
            return (
                read_number(Path(hwmon) / "temp1_input", 1000.0),
                read_number(Path(hwmon) / "freq1_input", 1_000_000.0),
                read_text(Path(hwmon) / "power/runtime_status", "unknown"),
            )
    return None, None, "missing"


def meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        try:
            result[key] = int(raw.split()[0])
        except (IndexError, ValueError):
            pass
    return result


def pressure_avg10(resource: str) -> float | None:
    line = read_text(f"/proc/pressure/{resource}").splitlines()
    if not line:
        return None
    for field in line[0].split():
        if field.startswith("avg10="):
            try:
                return float(field.split("=", 1)[1])
            except ValueError:
                return None
    return None


def disk_state(device: str = "sda") -> tuple[int | None, int | None, int | None, int | None]:
    for line in read_text("/proc/diskstats").splitlines():
        fields = line.split()
        if len(fields) >= 13 and fields[2] == device:
            try:
                return int(fields[11]), int(fields[12]), int(fields[5]), int(fields[9])
            except ValueError:
                break
    return None, None, None, None


def process_rss_mb(names: set[str]) -> float:
    total_kb = 0
    for status in glob.glob("/proc/[0-9]*/status"):
        lines = read_text(status).splitlines()
        process_name = next((x.split(":", 1)[1].strip() for x in lines if x.startswith("Name:")), "")
        if process_name not in names:
            continue
        raw = next((x.split() for x in lines if x.startswith("VmRSS:")), [])
        if len(raw) >= 2:
            try:
                total_kb += int(raw[1])
            except ValueError:
                pass
    return round(total_kb / 1024.0, 1)


def x_state() -> tuple[int, str]:
    environment = dict(os.environ)
    environment.setdefault("DISPLAY", ":0")
    try:
        completed = subprocess.run(
            ["/usr/bin/xset", "q"], stdout=subprocess.PIPE, text=True,
            stderr=subprocess.DEVNULL, timeout=2, env=environment, check=False,
        )
        state = "unknown"
        for line in completed.stdout.splitlines():
            if "Monitor is " in line:
                state = line.split("Monitor is ", 1)[1].strip()
        return int(completed.returncode == 0), state
    except (OSError, subprocess.TimeoutExpired):
        return 0, "timeout"


def session_idle_hint() -> str:
    try:
        completed = subprocess.run(
            ["/usr/bin/loginctl", "show-user", str(os.getuid()), "-p", "IdleHint", "--value"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return completed.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "timeout"


FIELDS = [
    "timestamp", "uptime_s", "load1", "cpu_temp_c", "gpu_temp_c",
    "gpu_clock_mhz", "gpu_runtime", "mem_available_mb", "swap_used_mb",
    "cpu_pressure10", "memory_pressure10", "io_pressure10", "sda_io_active",
    "sda_io_ms", "x_responsive", "desktop_rss_mb",
    "sda_read_sectors", "sda_write_sectors", "sda_runtime_status",
    "sdb_io_active", "sdb_io_ms", "sdb_read_sectors", "sdb_write_sectors",
    "sdb_runtime_status", "sata_host0_lpm", "sata_host2_lpm",
    "session_idle", "monitor_state",
]


def sample() -> dict[str, object]:
    memory = meminfo()
    gpu_temp, gpu_clock, gpu_runtime = gpu_values()
    disk_active, disk_ms, read_sectors, write_sectors = disk_state()
    sdb_active, sdb_ms, sdb_read_sectors, sdb_write_sectors = disk_state("sdb")
    x_responsive, monitor_state = x_state()
    swap_used = max(0, memory.get("SwapTotal", 0) - memory.get("SwapFree", 0))
    return {
        "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "uptime_s": round(float(read_text("/proc/uptime", "0").split()[0]), 1),
        "load1": round(os.getloadavg()[0], 2),
        "cpu_temp_c": thermal_value("x86_pkg_temp"),
        "gpu_temp_c": gpu_temp,
        "gpu_clock_mhz": gpu_clock,
        "gpu_runtime": gpu_runtime,
        "mem_available_mb": round(memory.get("MemAvailable", 0) / 1024.0, 1),
        "swap_used_mb": round(swap_used / 1024.0, 1),
        "cpu_pressure10": pressure_avg10("cpu"),
        "memory_pressure10": pressure_avg10("memory"),
        "io_pressure10": pressure_avg10("io"),
        "sda_io_active": disk_active,
        "sda_io_ms": disk_ms,
        "x_responsive": x_responsive,
        "desktop_rss_mb": process_rss_mb({"ChatGPT", "cinnamon", "Xorg", "rustdesk"}),
        "sda_read_sectors": read_sectors,
        "sda_write_sectors": write_sectors,
        "sda_runtime_status": read_text("/sys/block/sda/device/power/runtime_status", "unknown"),
        "sdb_io_active": sdb_active,
        "sdb_io_ms": sdb_ms,
        "sdb_read_sectors": sdb_read_sectors,
        "sdb_write_sectors": sdb_write_sectors,
        "sdb_runtime_status": read_text("/sys/block/sdb/device/power/runtime_status", "unknown"),
        "sata_host0_lpm": read_text("/sys/class/scsi_host/host0/link_power_management_policy", "unknown"),
        "sata_host2_lpm": read_text("/sys/class/scsi_host/host2/link_power_management_policy", "unknown"),
        "session_idle": session_idle_hint(),
        "monitor_state": monitor_state,
    }


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0
    with LOG_PATH.open("a", encoding="utf-8", newline="", buffering=1) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        while True:
            started = time.monotonic()
            try:
                writer.writerow(sample())
                handle.flush()
                os.fsync(handle.fileno())
            except Exception as exc:  # logger mora preživeti posamezen slab vzorec
                writer.writerow({"timestamp": dt.datetime.now().astimezone().isoformat(), "gpu_runtime": f"ERROR:{exc!r}"})
                handle.flush()
                os.fsync(handle.fileno())
            time.sleep(max(0.2, INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
