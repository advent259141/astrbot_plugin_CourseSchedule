# -*- coding: utf-8 -*-
"""WakeUp 6.x 分享口令客户端。

协议实现参考 WakeUpDecoder（Apache-2.0）：
https://github.com/airline233/WakeUpDecoder

这里只保留导入课表所需的最小实现，避免在插件中附带完整 APK。
"""

import base64
import hashlib
import json
import secrets
import time
from typing import Dict, List, Sequence, Tuple
from urllib.parse import quote_plus

import aiohttp


API_HOST = "https://api.wakeup.fun"
ANTISPAM_PATH = "/pluto/app/antispam"
SHARE_PATH = "/share_schedule/getv2"

# WakeUp 6.4.0（versionCode 530）的公开客户端常量。
APP_VERSION_CODE = 530
APP_VERSION_NAME = "6.4.0"
APP_PACKAGE = "com.suda.yzune.wakeupschedule"
APP_CHANNEL = "100271a"
APP_PUBLIC_TOKEN = "1_XPXQH3c5HRPtFHkSwi3sCCURmT25QfxM"
APP_SIGNATURE_CHARS_MD5 = "318c6d4f74655d4f032fb0466bcfdfbc"

# WakeUpDecoder 当前公开推荐的兼容设备 ID
ANDROID_ID = "0000000000000000"
MAGIC = "8&%d*"
SIGN_A_KEY = "@fG2SuLA"
KEY_SALT = "@#AIjd83#@6B"


class WakeUpClientError(Exception):
    """WakeUp 接口或分享数据错误。"""


def _ints(value: str) -> Tuple[int, ...]:
    return tuple(int(item) for item in value.split())


_IP = _ints(
    "57 49 41 33 25 17 9 1 59 51 43 35 27 19 11 3 "
    "61 53 45 37 29 21 13 5 63 55 47 39 31 23 15 7 "
    "56 48 40 32 24 16 8 0 58 50 42 34 26 18 10 2 "
    "60 52 44 36 28 20 12 4 62 54 46 38 30 22 14 6"
)
_FP = _ints(
    "39 7 47 15 55 23 63 31 38 6 46 14 54 22 62 30 "
    "37 5 45 13 53 21 61 29 36 4 44 12 52 20 60 28 "
    "35 3 43 11 51 19 59 27 34 2 42 10 50 18 58 26 "
    "33 1 41 9 49 17 57 25 32 0 40 8 48 16 56 24"
)
_E = _ints(
    "31 0 1 2 3 4 3 4 5 6 7 8 7 8 9 10 11 12 11 12 13 14 15 16 "
    "15 16 17 18 19 20 19 20 21 22 23 24 23 24 25 26 27 28 27 28 29 30 31 0"
)
_P = _ints(
    "15 6 19 20 28 11 27 16 0 14 22 25 4 17 30 9 "
    "1 7 23 13 31 26 2 8 18 12 29 5 21 10 3 24"
)
_PC1 = _ints(
    "56 48 40 32 24 16 8 0 57 49 41 33 25 17 9 1 "
    "58 50 42 34 26 18 10 2 59 51 43 35 62 54 46 38 "
    "30 22 14 6 61 53 45 37 29 21 13 5 60 52 44 36 28 20 12 4 27 19 11 3"
)
_PC2 = _ints(
    "13 16 10 23 0 4 2 27 14 5 20 9 22 18 11 3 25 7 15 6 26 19 12 1 "
    "40 51 30 36 46 54 29 39 50 44 32 46 43 48 38 55 33 52 45 41 49 35 28 31"
)
_SHIFTS = _ints("1 1 2 2 2 2 2 2 1 2 2 2 2 2 2 1")
_SBOX_ROWS = (
    ("e4d12fb83a6c5907", "0f74e2d1a6cb9538", "41e8d62bfc973a50", "fc8249175b3ea06d"),
    ("f18e6b34972dc05a", "3d47f28ec01a69b5", "0e7ba4d158c6932f", "d8a13f42b67c05e9"),
    ("a09e63f51dc7b428", "d709346a285ecbf1", "d6498f30b12c5ae7", "1ad069874fe3b52c"),
    ("7de3069a1285bc4f", "d8b56f03472c1ae9", "a690cb7df13e5284", "3f06a1d8945bc72e"),
    ("2c417ab6853fd0e9", "eb2c47d150fa3986", "421bad78f9c5630e", "b8c71e2d6f09a453"),
    ("c1af92680d34e75b", "af427c9561de0b38", "9ef528c3704a1db6", "432c95fabe17608d"),
    ("4b2ef08d3c975a61", "d0b7491ae35c2f86", "14bdc37eaf680592", "6bd814a7950fe23c"),
    ("d2846fb1a93e50c7", "1fd8a374c56b0e92", "7b419ce206adf358", "21e74a8dfc90356b"),
)
_SBOX = tuple(
    tuple(tuple(int(char, 16) for char in row) for row in box)
    for box in _SBOX_ROWS
)


def _md5_hex(value) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.md5(value).hexdigest()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _form_encode(items: Sequence[Tuple[str, object]]) -> str:
    return "&".join(f"{key}={quote_plus(str(value), safe='')}" for key, value in items)


def _cuid(android_id: str) -> str:
    return _md5_hex("com.baidu" + android_id).upper() + "|0"


def _adid(android_id: str) -> str:
    prefix = _md5_hex("alpha.beta" + android_id)
    words = [int(prefix[index:index + 8], 16) for index in range(0, 32, 8)]
    checksum = words[0] ^ words[1] ^ words[2] ^ words[3]
    return prefix + f"{checksum & 0xFFFFFFFF:08x}"


def _bits_from_bytes(value: bytes) -> List[int]:
    return [(byte >> bit) & 1 for byte in value for bit in range(8)]


def _bytes_from_bits(bits: Sequence[int]) -> bytes:
    result = bytearray()
    for offset in range(0, len(bits), 8):
        byte = sum((bits[offset + bit] & 1) << bit for bit in range(8))
        result.append(byte)
    return bytes(result)


def _permute(bits: Sequence[int], table: Sequence[int]) -> List[int]:
    return [bits[index] for index in table]


def _des_subkeys(key: str) -> List[List[int]]:
    bits = _permute(_bits_from_bytes(key.encode("utf-8")), _PC1)
    left, right = bits[:28], bits[28:]
    subkeys = []
    for shift in _SHIFTS:
        left = left[shift:] + left[:shift]
        right = right[shift:] + right[:shift]
        subkeys.append(_permute(left + right, _PC2))
    return subkeys


def _des_round(right: Sequence[int], subkey: Sequence[int]) -> List[int]:
    mixed = [a ^ b for a, b in zip(_permute(right, _E), subkey)]
    result = []
    for box_index in range(8):
        block = mixed[box_index * 6:(box_index + 1) * 6]
        row = block[0] * 2 + block[5]
        column = block[1] * 8 + block[2] * 4 + block[3] * 2 + block[4]
        value = _SBOX[box_index][row][column]
        result.extend(((value >> 3) & 1, (value >> 2) & 1, (value >> 1) & 1, value & 1))
    return _permute(result, _P)


def _des_block(block: bytes, subkeys: Sequence[Sequence[int]]) -> bytes:
    bits = _permute(_bits_from_bytes(block), _IP)
    left, right = bits[:32], bits[32:]
    for subkey in subkeys:
        left, right = right, [a ^ b for a, b in zip(left, _des_round(right, subkey))]
    return _bytes_from_bits(_permute(right + left, _FP))


def _des_encrypt(value: str, key: str) -> bytes:
    plain = value.encode("utf-8")
    padding = 8 - len(plain) % 8
    padded = plain + (b"\x00" * (padding - 1)) + bytes((padding,))
    subkeys = _des_subkeys(key)
    return b"".join(_des_block(padded[index:index + 8], subkeys) for index in range(0, len(padded), 8))


def _des_decrypt(value: bytes, key: str) -> bytes:
    subkeys = list(reversed(_des_subkeys(key)))
    plain = b"".join(_des_block(value[index:index + 8], subkeys) for index in range(0, len(value), 8))
    padding = plain[-1]
    if padding < 1 or padding > 8:
        raise WakeUpClientError("WakeUp 反作弊响应解密失败")
    return plain[:-padding]


def _reverse_nibble(value: int) -> int:
    return ((value & 1) << 3) | ((value & 2) << 1) | ((value & 4) >> 1) | ((value & 8) >> 3)


def _native_hex_encode(value: bytes) -> str:
    return "".join(
        f"{_reverse_nibble(byte & 0x0F):02x}{_reverse_nibble((byte >> 4) & 0x0F):02x}"
        for byte in value
    )


def _native_hex_decode(value: str) -> bytes:
    result = bytearray()
    for index in range(0, (len(value) // 4) * 4, 4):
        low = _reverse_nibble(int(value[index + 1], 16))
        high = _reverse_nibble(int(value[index + 3], 16))
        result.append(low | (high << 4))
    return bytes(result)


def _rc4(value: bytes, key: str) -> bytes:
    key_bytes = key.encode("utf-8")
    state = list(range(256))
    cursor = 0
    for index in range(256):
        cursor = (cursor + state[index] + key_bytes[index % len(key_bytes)]) & 0xFF
        state[index], state[cursor] = state[cursor], state[index]
    result = bytearray()
    left = cursor = 0
    for byte in value:
        left = (left + 1) & 0xFF
        cursor = (cursor + state[left]) & 0xFF
        state[left], state[cursor] = state[cursor], state[left]
        result.append(byte ^ state[(state[left] + state[cursor]) & 0xFF])
    return bytes(result)


def _make_sign_a(cuid: str) -> Tuple[str, str]:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    random_value = "".join(secrets.choice(alphabet) for _ in range(10))
    plain = f"{MAGIC}##{random_value}##{APP_SIGNATURE_CHARS_MD5}##{cuid}"
    return _native_hex_encode(_des_encrypt(plain, SIGN_A_KEY)), random_value


def _token_from_sign_b(sign_b: str, random_value: str) -> str:
    plain = _des_decrypt(_native_hex_decode(sign_b), random_value[:5] + "#G4")
    if len(plain) < 22 or plain[:10].decode("latin1") != random_value:
        raise WakeUpClientError("WakeUp 反作弊令牌校验失败")
    return plain[12:22].decode("latin1")


def _native_key(token: str) -> str:
    first = _md5_hex(KEY_SALT)
    second = _md5_hex(str(APP_VERSION_CODE))
    third = _md5_hex(f"[{token}]@")
    third = third[17:32][::-1] + third[15:17] + third[0:15][::-1]
    chars = list(first + second + third)
    for index in range(3):
        chars[index], chars[-1 - index] = chars[-1 - index], chars[index]
    mixed = "".join(chars)
    chars = list(mixed + _md5_hex(mixed))
    for index in range(60):
        chars[index], chars[-1 - index] = chars[-1 - index], chars[index]
    return "".join(chars)


def _native_sign(base64_params: str, token: str) -> str:
    return _md5_hex(f"{MAGIC}[{_md5_hex(token)}]@{base64_params}")


def _common_params(cuid: str, adid: str) -> List[Tuple[str, str]]:
    return [
        ("area", ""), ("screensize", "1080x2400"), ("cuid", cuid),
        ("os", "android"), ("city", ""), ("abis", "arm64-v8a"),
        ("channel", APP_CHANNEL), ("appBit", "64"), ("vc", str(APP_VERSION_CODE)),
        ("deviceId", ""), ("token", APP_PUBLIC_TOKEN), ("adid", adid),
        ("province", ""), ("pkgName", APP_PACKAGE), ("appId", "wakeup"),
        ("download_type", "1"), ("vcname", APP_VERSION_NAME), ("sdk", "35"),
        ("device", "Pixel 7"), ("brand", "google"), ("operatorid", ""),
    ]


def _headers(cuid: str, adid: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "okhttp/4.12.0",
        "na__zyb_source__": "wakeup",
        "zyb-cuid": cuid,
        "zyb-adid": adid,
    }


async def _post_json(session: aiohttp.ClientSession, path: str, body: str, headers: Dict[str, str]) -> Dict:
    async with session.post(API_HOST + path, data=body.encode("utf-8"), headers=headers) as response:
        text = await response.text()
        if response.status != 200:
            raise WakeUpClientError(f"WakeUp API HTTP {response.status}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise WakeUpClientError("WakeUp API 返回了无效 JSON") from error
        if not isinstance(payload, dict):
            raise WakeUpClientError("WakeUp API 响应格式错误")
        return payload


def _extract_sign_b(payload: Dict) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, str) or not data:
        message = payload.get("errstr") or payload.get("errMsg") or "未知错误"
        raise WakeUpClientError(f"WakeUp 反作弊验证失败：{message}")
    return data


def _build_share_request(token: str, code: str, common: List[Tuple[str, str]]) -> Tuple[str, str]:
    rc4_key = _native_key(token)
    plain = "key=" + quote_plus(code, safe="")
    encrypted = _b64(_rc4(plain.encode("utf-8"), rc4_key))
    timestamp = int(time.time() * 1000)
    monotonic = int(time.monotonic() * 1000)
    sign_items = [f"data={encrypted}"] + [f"{key}={value}" for key, value in common]
    sign_items.extend(("nt=wifi", f"_t_={timestamp}", f"kakorrhaphiophobia={monotonic}"))
    encoded = _b64("".join(sorted(sign_items)).encode("utf-8"))
    sign = _native_sign(encoded, token)
    body = "&" + _form_encode([("data", encrypted)] + common + [("nt", "wifi")])
    body += f"&sign={sign}&_t_={timestamp}&kakorrhaphiophobia={monotonic}"
    return body, rc4_key


def _parse_share_response(payload: Dict, rc4_key: str) -> List:
    if payload.get("errNo") != 0:
        message = payload.get("errstr") or payload.get("errMsg") or "未知错误"
        raise WakeUpClientError(f"WakeUp API 返回错误：{message}")
    data = payload.get("data")
    encrypted = data.get("data") if isinstance(data, dict) else data
    if not isinstance(encrypted, str) or not encrypted:
        raise WakeUpClientError("WakeUp API 未返回课程表数据")
    try:
        plain = _rc4(base64.b64decode(encrypted), rc4_key).decode("utf-8")
        decoded = json.loads(plain)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WakeUpClientError("WakeUp 课程表响应解密失败") from error
    share_data = decoded.get("shareData") if isinstance(decoded, dict) else None
    if not isinstance(share_data, str) or not share_data.strip():
        raise WakeUpClientError("WakeUp 分享口令已失效或课程表为空")
    try:
        parts = [json.loads(line) for line in share_data.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise WakeUpClientError("WakeUp 分享数据格式错误") from error
    if len(parts) < 5:
        raise WakeUpClientError(f"WakeUp 分享数据不完整（仅 {len(parts)} 段）")
    return parts


async def fetch_wakeup_schedule(code: str, timeout_seconds: float = 15.0) -> List:
    """通过 WakeUp 当前的 getv2 协议获取并解密五段课表数据。"""
    cuid = _cuid(ANDROID_ID)
    adid = _adid(ANDROID_ID)
    common = _common_params(cuid, adid)
    headers = _headers(cuid, adid)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        sign_a, random_value = _make_sign_a(cuid)
        antispam_body = _form_encode([("data", sign_a)] + common) + "&"
        antispam = await _post_json(session, ANTISPAM_PATH, antispam_body, headers)
        token = _token_from_sign_b(_extract_sign_b(antispam), random_value)

        share_body, rc4_key = _build_share_request(token, code, common)
        response = await _post_json(session, SHARE_PATH, share_body, headers)
        return _parse_share_response(response, rc4_key)
