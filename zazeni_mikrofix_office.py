"""Production entry point for the standalone MikroFIX Office process."""

from zazeni_mikrofix import launch


if __name__ == "__main__":
    raise SystemExit(launch("office"))
