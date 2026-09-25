"""Glue between the auto mode driver and one install session."""

from __future__ import annotations

import threading
from pathlib import Path

from config.config_manager import config_manager as cm
from handler.database import db_install_session_handler
from handler.install.manifest import read_live_manifest
from logger.logger import log

from .catalog import load_catalog
from .driver import AutoModeDriver, make_x11_actor, make_x11_observer


def _extra_buttons() -> list[dict]:
    try:
        return list(cm.get_config().INSTALL_AUTO_MODE_EXTRA_BUTTONS)
    except Exception as e:  # noqa: BLE001 - a broken config must not stop the built-in list
        log.debug(f"Could not read auto mode extra buttons: {e}")
        return []


def build_driver(
    install_session_id: int, display: str, work_dir: Path
) -> AutoModeDriver:
    def enabled() -> bool:
        session = db_install_session_handler.get_session(install_session_id)
        return bool(session and session.auto_mode)

    def progress() -> int:
        live = read_live_manifest(work_dir)
        return sum(e.size_bytes for e in live.values()) if live else 0

    def report(status: str | None, detail: str | None) -> None:
        db_install_session_handler.update_session(
            install_session_id, {"auto_status": status, "auto_detail": detail}
        )

    catalog = load_catalog(_extra_buttons())
    return AutoModeDriver(
        catalog=catalog,
        observe=make_x11_observer(display, catalog),
        act=make_x11_actor(display),
        enabled=enabled,
        progress=progress,
        report=report,
    )


def start_auto_mode(
    install_session_id: int, display: str, work_dir: Path, stop: threading.Event
) -> threading.Thread:
    driver = build_driver(install_session_id, display, work_dir)
    thread = threading.Thread(target=driver.run, args=(stop,), daemon=True)
    thread.start()
    return thread
