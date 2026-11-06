"""Minimal EcoFlow IoT Open API client (HTTP, HMAC-SHA256 signed).

Docs: https://developer.ecoflow.com/us/document/generalInfo
"""
import hashlib
import hmac
import json
import random
import ssl
import time
import urllib.parse
import urllib.request


def flatten(prefix, value):
    """Expand nested params the way EcoFlow's signature expects: a.b=1, list[0]=2."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in flatten(f"{prefix}.{k}" if prefix else k, v)]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in flatten(f"{prefix}[{i}]", v)]
    if isinstance(value, bool):
        value = "true" if value else "false"
    return [f"{prefix}={value}"]


def sign(params, access_key, secret_key, nonce, timestamp):
    parts = sorted(flatten("", params or {}))
    parts += [f"accessKey={access_key}", f"nonce={nonce}", f"timestamp={timestamp}"]
    return hmac.new(secret_key.encode(), "&".join(parts).encode(), hashlib.sha256).hexdigest()


try:  # python.org macOS builds ship without root certs; certifi fixes that if installed
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()


class EcoFlow:
    def __init__(self, access_key, secret_key, host="https://api.ecoflow.com"):
        self.ak, self.sk, self.host = access_key, secret_key, host.rstrip("/")

    def _call(self, method, path, params=None):
        nonce, ts = str(random.randint(100000, 999999)), str(int(time.time() * 1000))
        headers = {"accessKey": self.ak, "nonce": nonce, "timestamp": ts,
                   "sign": sign(params, self.ak, self.sk, nonce, ts)}
        url, body = f"{self.host}/iot-open/sign{path}", None
        if method == "GET" and params:
            url += "?" + urllib.parse.urlencode(params)
        elif params is not None:
            body = json.dumps(params).encode()
            headers["Content-Type"] = "application/json;charset=UTF-8"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            res = json.load(r)
        if str(res.get("code")) != "0":
            raise RuntimeError(f"EcoFlow API error {res.get('code')}: {res.get('message')}")
        return res.get("data")

    def devices(self):
        return self._call("GET", "/device/list")

    def quota_all(self, sn):
        return self._call("GET", "/device/quota/all", {"sn": sn})

    def set_shp2(self, sn, params):
        """Send a Smart Home Panel 2 setting, e.g. {"ch1ForceCharge": "FORCE_CHARGE_ON"}."""
        return self._call("PUT", "/device/quota", {"sn": sn, "cmdCode": "PD303_APP_SET", "params": params})
