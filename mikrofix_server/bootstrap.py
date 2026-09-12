"""One-time interactive bootstrap for the first central administrator."""

from __future__ import annotations

import argparse
import getpass

from sqlalchemy import func, select

from .auth import create_central_user, register_workstation
from .config import ServerSettings
from .database import create_database_engine
from .models import central_users


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create Daniel and the first registered Office workstation."
    )
    parser.add_argument("--workstation-code", default="OFFICE-PC-1")
    parser.add_argument("--workstation-name", default="Glavni Office računalnik")
    args = parser.parse_args()
    pin = getpass.getpass("Začetni PIN za uporabnika daniel: ")
    repeated = getpass.getpass("Ponovi PIN: ")
    if pin != repeated:
        raise SystemExit("PIN-a se ne ujemata.")

    settings = ServerSettings.from_environment()
    engine = create_database_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            count = connection.execute(
                select(func.count()).select_from(central_users)
            ).scalar_one()
            if count:
                raise SystemExit(
                    "Centralni uporabniki že obstajajo; bootstrap je zato zavrnjen."
                )
            admin_uuid = create_central_user(
                connection, username="daniel", display_name="Daniel", role="ADMIN",
                pin=pin, must_change_pin=False,
            )
            workstation_uuid, secret = register_workstation(
                connection, workstation_code=args.workstation_code,
                display_name=args.workstation_name, application_type="OFFICE",
                approved_by=admin_uuid,
            )
    finally:
        engine.dispose()
    print(f"Ustvarjena delovna postaja: {workstation_uuid}")
    print("Skrivnost delovne postaje (prikazana samo zdaj):")
    print(secret)


if __name__ == "__main__":
    main()
