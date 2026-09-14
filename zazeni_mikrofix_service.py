"""Production entry point for the standalone MikroFIX Service process."""

from zazeni_mikrofix import launch


if __name__ == "__main__":
    raise SystemExit(launch("service"))
