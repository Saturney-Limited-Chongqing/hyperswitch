"""Configure a Logto OIDC login with explicit local RBAC bindings."""

import argparse
import json
import ipaddress
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


PLATFORM_ROLES = {"tenant_admin"}
MERCHANT_ROLES = {"collection_merchant_admin", "collection_merchant_view_only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("connection_file", type=Path)
    parser.add_argument("--update-id", help="Existing authentication method ID")
    parser.add_argument("--allow-http-private-dev", action="store_true", help="Allow an HTTP issuer on a private IP for isolated testing only")
    args = parser.parse_args()
    connection = json.loads(args.connection_file.read_text())
    issuer = urllib.parse.urlparse(connection["issuer"])
    loopback = issuer.hostname in ("localhost", "127.0.0.1", "::1")
    try:
        private_ip = ipaddress.ip_address(issuer.hostname or "").is_private
    except ValueError:
        private_ip = False
    if issuer.scheme != "https" and not (issuer.scheme == "http" and (loopback or (args.allow_http_private_dev and private_ip))):
        raise SystemExit("Use HTTPS for Logto outside loopback testing; isolated private-IP testing requires --allow-http-private-dev")
    bindings = connection["logto_bindings"]
    if not isinstance(bindings, dict) or not bindings:
        raise SystemExit("logto_bindings must be a nonempty subject map")
    for subject, binding in bindings.items():
        if not isinstance(subject, str) or not subject or not isinstance(binding, dict):
            raise SystemExit("Each Logto subject needs a binding")
        role = binding.get("role_id")
        if not isinstance(binding.get("user_id"), str) or not binding["user_id"]:
            raise SystemExit("Each binding needs an existing local user_id")
        if role in PLATFORM_ROLES:
            if binding.get("organization_id") is not None or binding.get("merchant_id") is not None:
                raise SystemExit("Platform roles must not have organization_id or merchant_id")
        elif role in MERCHANT_ROLES:
            if not binding.get("organization_id") or not binding.get("merchant_id"):
                raise SystemExit("Merchant roles need organization_id and merchant_id")
        else:
            raise SystemExit("Unsupported role_id in Logto binding")

    auth_config = {
        "auth_type": "open_id_connect",
        "public_config": {"name": "logto"},
        "private_config": {
            "base_url": connection["issuer"],
            "client_id": connection["client_id"],
            "client_secret": connection["client_secret"],
            "private_key": None,
            "logto_bindings": bindings,
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
    request = urllib.request.Request(
        os.environ.get("HYPERSWITCH_API_URL", "http://127.0.0.1:18080").rstrip("/") + "/user/auth",
        data=json.dumps(body).encode(),
        method="PUT" if args.update_id else "POST",
        headers={"Content-Type": "application/json", "api-key": os.environ["HYPERSWITCH_ADMIN_KEY"]},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = response.read()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Authentication configuration failed: HTTP {error.code}") from None
    if args.update_id:
        print(json.dumps({"id": args.update_id, "updated": True}))
    else:
        parsed = json.loads(result)
        print(json.dumps({"id": parsed["id"], "auth_id": parsed["auth_id"]}))


if __name__ == "__main__":
    main()
