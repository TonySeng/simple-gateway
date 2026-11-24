"""各云厂商的签名/鉴权原语。

说明：以下实现严格按各厂商公开签名算法编写，但签名联调极易因细节（时间戳、编码、
header 顺序）出错。**generic / paddle 适配器为生产可用**；云厂商适配器需用真实密钥联调，
首次上线前请以官方示例请求逐字段比对。
"""
import base64
import datetime as dt
import hashlib
import hmac
import time
import uuid
from urllib.parse import quote


def hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def hmac_sha1(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha1).digest()


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def percent_encode(s: str) -> str:
    """Aliyun 风格百分号编码。"""
    return (
        quote(str(s), safe="", encoding="utf-8")
        .replace("+", "%20")
        .replace("*", "%2A")
        .replace("%7E", "~")
    )


# ---------------- 腾讯云 TC3-HMAC-SHA256 ----------------
def tencent_authorization(
    secret_id: str,
    secret_key: str,
    host: str,
    action: str,
    payload: str,
    service: str = "ocr",
    region: str = "ap-guangzhou",
    version: str = "2018-11-19",
) -> dict:
    algorithm = "TC3-HMAC-SHA256"
    now = dt.datetime.now(dt.timezone.utc)
    timestamp = int(now.timestamp())
    date = now.strftime("%Y-%m-%d")

    payload_hash = sha256_hex(payload)
    canonical_headers = (
        "content-type:application/json; charset=utf-8\n"
        f"host:{host}\n"
        f"x-tc-action:{action.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        [algorithm, str(timestamp), credential_scope, sha256_hex(canonical_request)]
    )
    secret_date = hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = hmac_sha256(secret_date, service)
    secret_signing = hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{algorithm} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
    }
    if region:
        headers["X-TC-Region"] = region
    return headers


# ---------------- 阿里云 RPC（HMAC-SHA1）----------------
def aliyun_signature(access_key_secret: str, params: dict) -> str:
    params = dict(params)
    params["SignatureNonce"] = params.get("SignatureNonce") or uuid.uuid4().hex
    params["Timestamp"] = params.get("Timestamp") or dt.datetime.now(
        dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    ordered = "&".join(
        f"{percent_encode(k)}={percent_encode(params[k])}" for k in sorted(params)
    )
    string_to_sign = "GET&%2F&" + percent_encode(ordered)
    sig = base64.b64encode(
        hmac_sha1((access_key_secret + "&").encode("utf-8"), string_to_sign)
    ).decode("utf-8")
    return sig


# ---------------- 讯飞（iFlytek）HMAC-SHA256 ----------------
def iflytek_authorization(api_key: str, api_secret: str, host: str, path: str) -> dict:
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    signature_origin = f"host: {host}\ndate: {date}\nPOST {path} HTTP/1.1"
    signature_sha = base64.b64encode(
        hmac_sha256(api_secret.encode("utf-8"), signature_origin)
    ).decode("utf-8")
    authorization = base64.b64encode(
        (
            f'api_key="{api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature_sha}"'
        ).encode("utf-8")
    ).decode("utf-8")
    return {
        "Authorization": authorization,
        "Date": date,
        "Host": host,
    }


def iflytek_private_authorization(
    api_key: str, api_secret: str, host: str, path: str, method: str = "POST"
) -> dict:
    """讯飞**私有 HTTP API**（如通用文字识别 /v1/private/{sid}）鉴权。

    与旧版 AIUI 接口的区别：**鉴权参数放在 URL 查询串**，而不是请求头。

        signature_origin = "host: $host\\ndate: $date\\n$request-line"
        request-line      = "POST /v1/private/xxxx HTTP/1.1"
        signature         = base64(HMAC-SHA256(apiSecret, signature_origin))
        authorization     = base64('api_key="..",algorithm="hmac-sha256",
                                    headers="host date request-line",signature=".."')
    """
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    request_line = f"{method} {path} HTTP/1.1"
    signature_origin = f"host: {host}\ndate: {date}\n{request_line}"
    signature_sha = base64.b64encode(
        hmac_sha256(api_secret.encode("utf-8"), signature_origin)
    ).decode("utf-8")
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature_sha}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
    return {"host": host, "date": date, "authorization": authorization}


# ---------------- 华为云 SIGv1（SDK-HMAC-SHA256）----------------
def huawei_authorization(
    ak: str,
    sk: str,
    method: str,
    uri: str,
    query_str: str,
    headers: dict,
    signed_headers: list[str],
    sdk_date: str,
    body: str = "",
) -> str:
    payload_hash = sha256_hex(body)
    canonical_headers = "".join(
        f"{h.lower()}:{headers[h].strip()}\n" for h in sorted(signed_headers)
    )
    sh = ";".join(sorted(h.lower() for h in signed_headers))
    canonical_request = "\n".join(
        [method.upper(), uri, query_str, canonical_headers, sh, payload_hash]
    )
    string_to_sign = "\n".join(
        ["SDK-HMAC-SHA256", sdk_date, sha256_hex(canonical_request)]
    )
    signature = hmac_sha256(sk.encode("utf-8"), string_to_sign).hex()
    return f"SDK-HMAC-SHA256 Access={ak}, SignedHeaders={sh}, Signature={signature}"
