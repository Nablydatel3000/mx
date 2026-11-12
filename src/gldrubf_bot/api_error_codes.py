"""Numeric allowlist from official investAPI 3eaf23a, src/docs/errors/api_errors.json."""
import json
import http.client


ERROR_BODY_LIMIT = 64 * 1024
KNOWN_API_CODES = frozenset({
    12001, 12002, 30001, 30002, 30003, 30004, 30005, 30006, 30007, 30008,
    30009, 30010, 30011, 30012, 30013, 30014, 30015, 30016, 30017, 30018,
    30019, 30020, 30021, 30022, 30023, 30025, 30026, 30027, 30028, 30029,
    30030, 30031, 30032, 30033, 30034, 30036, 30037, 30038, 30039, 30040,
    30041, 30042, 30043, 30044, 30045, 30047, 30048, 30049, 30050, 30051,
    30052, 30053, 30054, 30055, 30056, 30057, 30058, 30059, 30060, 30061,
    30062, 30063, 30064, 30065, 30066, 30067, 30068, 30069, 30070, 30077,
    30078, 30079, 30080, 30081, 30082, 30083, 30084, 30085, 30086, 30087,
    30088, 30089, 30090, 30091, 30092, 30093, 30094, 30095, 30096, 30097,
    30098, 30099, 30100, 30101, 30102, 30103, 30104, 30105, 30106, 30107,
    30108, 30109, 30210, 30211, 30212, 30213, 30214, 30215, 30216, 30217,
    30218, 30219, 30220, 30221, 30222, 30223, 30224, 30225, 30226, 30227,
    30228, 30229, 30230, 30231, 30232, 30233, 30234, 30235, 30236, 30237,
    30238, 30240, 30241, 30242, 30244, 30245, 30246, 30247, 30248, 30249,
    30250, 30251, 30252, 30254, 30255, 30256, 30257, 30258, 35001, 40002,
    40003, 40004, 50001, 50002, 50004, 50005, 50006, 50007, 50008, 50009,
    50010, 50011, 50012, 50013, 70001, 70002, 70003, 80001, 80002, 80003,
    80004, 80005, 90001, 90002, 90003,
})


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 5:
        return int(value)
    return None


def extract_codes(result):
    if not isinstance(result, dict):
        return None, None
    code = _integer(result.get("code"))
    grpc_code = code if code is not None and 0 <= code <= 16 else None
    description = _integer(result.get("description"))
    api_code = description if description in KNOWN_API_CODES else (code if code in KNOWN_API_CODES else None)
    return grpc_code, api_code


def read_error_codes(response, content_type):
    if content_type not in {"application/json", "application/problem+json"}:
        return None, None
    try:
        raw = response.read(ERROR_BODY_LIMIT + 1)
        if not isinstance(raw, bytes) or len(raw) > ERROR_BODY_LIMIT:
            return None, None
        return extract_codes(json.loads(raw))
    except (ValueError, UnicodeError, RecursionError, OSError, http.client.HTTPException):
        return None, None
