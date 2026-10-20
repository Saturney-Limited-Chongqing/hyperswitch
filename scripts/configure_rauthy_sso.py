"""Configure an administrator-managed Rauthy login; never print client secrets."""
import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("connection_file", type=Path, help="Private JSON with issuer, client_id, client_secret, owner_id, owner_type, email_domain and subject_bindings")
    parser.add_argument("--update-id", help="Update this existing authentication method instead of creating one")
    args = parser.parse_args()
    connection = json.loads(args.connection_file.read_text())
    bindings = connection["subject_bindings"]
    if not isinstance(bindings, dict) or not bindings or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in bindings.items()):
        raise SystemExit("subject_bindings must map Rauthy subjects to existing local user IDs")
    issuer = urllib.parse.urlparse(connection["issuer"])
    if issuer.scheme != "https" and not (issuer.scheme == "http" and issuer.hostname in ("localhost", "127.0.0.1", "::1")):
        raise SystemExit("Use HTTPS for the Rauthy issuer outside loopback testing")
    auth_config = {
        "auth_type": "open_id_connect",
        "public_config": {"name": "rauthy"},
        "private_config": {
            "base_url": connection["issuer"],
            "client_id": connection["client_id"],
            "client_secret": connection["client_secret"],
            "private_key": None,
            "subject_bindings": bindings,
        },
    }
    if args.update_id:
        body = {"auth_method": {"id": args.update_id, "auth_config": auth_config}}
    else:
        body = {
            "owner_id": connection["owner_id"],
            "owner_type": connection["owner_type"],
            "email_domain": connection["email_domain"],
            "allow_signup": False,
            "auth_method": auth_config,
        }
    req = urllib.request.Request(
        os.environ.get("HYPERSWITCH_API_URL", "http://127.0.0.1:18080").rstrip("/") + "/user/auth",
        data=json.dumps(body).encode(),
        method="PUT" if args.update_id else "POST",
        headers={"Content-Type": "application/json", "api-key": os.environ["HYPERSWITCH_ADMIN_KEY"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            value = response.read()
    except urllib.error.HTTPError as error:
        # Server error bodies may echo configuration; return only the status.
        raise SystemExit(f"Authentication configuration failed: HTTP {error.code}") from None
    if args.update_id:
        print(json.dumps({"id": args.update_id, "updated": True}))
    else:
        result = json.loads(value)
        print(json.dumps({"id": result["id"], "auth_id": result["auth_id"]}))


if __name__ == "__main__":
    main()
