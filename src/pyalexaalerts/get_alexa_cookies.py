import argparse
import json
from pathlib import Path

from .auth import SUPPORTED_DOMAINS, default_service_auth_path, exchange_refresh_token


def main(
    service_auth_path: Path | None = None,
    runtime_creds_path: Path | None = None,
    amazon_domain: str = "amazon.com",
) -> None:
    if service_auth_path is None:
        service_auth_path = default_service_auth_path()
    if runtime_creds_path is None:
        runtime_creds_path = service_auth_path.parent / "runtime_credentials.json"

    with open(service_auth_path, "r") as f:
        refresh_token = json.load(f)["alexa"]["refresh_token"]
    if not refresh_token:
        raise ValueError(f"{service_auth_path}: alexa.refresh_token is empty")

    cookies = exchange_refresh_token(refresh_token, amazon_domain)

    runtime = {}
    if runtime_creds_path.exists():
        with open(runtime_creds_path, "r") as f:
            runtime = json.load(f)
    runtime["alexa"] = {"cookies": cookies}
    with open(runtime_creds_path, "w") as f:
        json.dump(runtime, f, indent=4)

    print(f"Alexa cookies written to {runtime_creds_path}")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="path to service_auth.json (default: ./config/service_auth.json)")
    parser.add_argument("--domain", default="amazon.com", choices=SUPPORTED_DOMAINS)
    args = parser.parse_args()
    main(service_auth_path=args.config, amazon_domain=args.domain)


if __name__ == "__main__":
    cli()
