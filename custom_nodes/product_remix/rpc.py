"""Optional Euler boundary. No internal dependencies are imported in local mode."""
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from .runtime import input_path, text


def attribute(root, path):
    for name in path.split("."):
        if not name or name.startswith("_"):
            raise ValueError("类型路径只允许公开的 IDL 名称")
        root = getattr(root, name)
    return root


def convert(field_type, definition, value):
    if field_type == 12:
        return struct_from_dict(definition, value)
    if field_type in {14, 15}:
        if not isinstance(value, list):
            raise ValueError("Thrift list/set 需要 JSON 数组")
        result = [convert(definition[0], definition[1], item) for item in value]
        return set(result) if field_type == 14 else result
    if field_type == 13:
        if not isinstance(value, dict):
            raise ValueError("Thrift map 需要 JSON 对象")
        key_type, key_spec, value_type, value_spec = definition
        return {convert(key_type, key_spec, int(k) if key_type in {3, 6, 8, 10} else k):
                convert(value_type, value_spec, v) for k, v in value.items()}
    if field_type == 2 and type(value) is not bool:
        raise ValueError("Thrift bool 必须是布尔值")
    if field_type in {3, 6, 8, 10}:
        bits = {3: 8, 6: 16, 8: 32, 10: 64}[field_type]
        if type(value) is not int or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
            raise ValueError(f"Thrift i{bits} 必须是有效整数")
    if field_type == 11 and not isinstance(value, (str, bytes)):
        raise ValueError("Thrift string 必须是文本")
    return value


def struct_from_dict(cls, values):
    if not isinstance(values, dict):
        raise ValueError(f"{cls.__name__} 需要 JSON 对象")
    fields = {field[1]: field for field in cls.thrift_spec.values()}
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(f"{cls.__name__} 没有这些 IDL 字段：{sorted(unknown)}")
    result = cls()
    for name, value in values.items():
        field = fields[name]
        if value is not None:
            setattr(result, name, convert(field[0], field[2] if len(field) >= 4 else None, value))
    for name, field in fields.items():
        if field[-1] is True and getattr(result, name, None) is None:
            raise ValueError(f"IDL 必填字段缺失：{name}")
    return result


def plain(value):
    if hasattr(value, "thrift_spec"):
        return {field[1]: plain(getattr(value, field[1])) for field in value.thrift_spec.values()
                if getattr(value, field[1], None) is not None}
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(x) for x in value]
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def select_response(value, path):
    for component in path.split(".") if path else []:
        value = value[int(component)] if isinstance(value, list) else value[component]
    return value


def invoke(module, client, profile, data, gdpr_token=None):
    values = deepcopy(data)
    caller = text(profile.get("caller"), "profile.caller")
    base = values.setdefault("Base", {})
    if not isinstance(base, dict):
        raise ValueError("Base 必须是对象")
    base["Caller"] = caller
    if gdpr_token:
        base.setdefault("Extra", {})["gdpr-token"] = gdpr_token
    request = struct_from_dict(attribute(module, profile["request_type"]), values)
    method_name = text(profile.get("method"), "profile.method")
    if method_name.startswith("_"):
        raise ValueError("不能调用私有方法")
    # No automatic retry: a timeout does not establish that a mutating RPC failed.
    response = getattr(client, method_name)(request)
    result = plain(response)
    status = result.get("BaseResp", {})
    if status.get("StatusCode", 0) != 0:
        raise RuntimeError(f"RPC BaseResp.StatusCode={status['StatusCode']}；请检查服务日志")
    return select_response(result, profile.get("response_path", ""))


def call_euler(profile, data):
    try:
        import euler
        import thriftpy2
    except ImportError:
        raise RuntimeError("真实 RPC 模式需要本项目 ComfyUI/.venv 中的 bytedeuler、thriftpy2 以及真实 IDL；本地回放不需要安装") from None
    idl = input_path(text(profile.get("idl_file"), "profile.idl_file"))
    target = text(profile.get("target"), "profile.target")
    if not target.startswith(("sd://", "tcp://")):
        raise ValueError("target 需要显式的 sd:// 或 tcp:// 路由")
    module_name = "product_remix_" + hashlib.sha256(str(idl).encode()).hexdigest()[:12] + "_thrift"
    module = thriftpy2.load(str(idl), module_name, include_dirs=[str(idl.parent)])
    client = euler.Client(attribute(module, profile["service"]), target, timeout=profile.get("timeout_seconds", 30))
    token = None
    token_env = profile.get("gdpr_token_env")
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise ValueError(f"没有设置 GDPR 环境变量：{token_env}")
    elif profile.get("gdpr_from_runtime", False):
        try:
            import byteddps
        except ImportError:
            raise RuntimeError("当前环境没有 byteddps；请配置 gdpr_token_env 或在有身份的环境运行") from None
        token = byteddps.get_token()
    return invoke(module, client, profile, data, token)


class ProductRemixEulerRPC:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "mode": (["fixture", "euler"], {"default": "fixture"}),
            "profile_file": ("STRING", {"default": "product-remix/rpc-profile.json"}),
            "request_json": ("STRING", {"multiline": True, "default": "{}"}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("响应 JSON",)
    FUNCTION = "execute"
    CATEGORY = "Product Remix/商品混剪"
    DESCRIPTION = "fixture 只回放本地响应；euler 才实际调用配置的接口。真实 IDL/路由/Caller 必须自备。不会创建或部署服务。"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def execute(self, mode, profile_file, request_json):
        profile = json.loads(input_path(profile_file).read_text(encoding="utf-8"))
        data = json.loads(request_json)
        if not isinstance(data, dict):
            raise ValueError("RPC 请求必须是 JSON 对象")
        if mode == "fixture":
            fixture = json.loads(input_path(profile["fixture_file"]).read_text(encoding="utf-8"))
            if fixture.get("request") != data:
                raise ValueError("本地回放的请求与记录不一致")
            result = select_response(fixture["response"], profile.get("response_path", ""))
        elif mode == "euler":
            result = call_euler(profile, data)
        else:
            raise ValueError("未知 RPC 模式")
        return (json.dumps(result, ensure_ascii=False, indent=2),)
