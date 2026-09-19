import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

import comfy.model_management
import folder_paths


DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "xhigh"
DEFAULT_INSTRUCTIONS = "你是短剧与视频创作助手。遵循用户要求，直接输出可用的正文，不添加开场白或过程说明。"
TEXT_INSTRUCTIONS = (
    "This is a text-generation step in a ComfyUI workflow. Generate the requested text only. "
    "Do not inspect files, execute commands, use tools, delegate work, or ask follow-up questions. "
    "Do not write files yourself; the CLI captures your final response. "
    "Use only the supplied input and your existing knowledge.\n\n"
)
DISABLED_FEATURES = (
    "shell_tool", "apps", "plugins", "hooks", "multi_agent", "multi_agent_v2",
    "browser_use", "computer_use", "image_generation", "code_mode", "memories", "skill_search",
)


def codex_environment():
    env = os.environ.copy()
    # Finder-launched ComfyUI may not inherit the terminal's npm / Node PATH.
    extra_paths = [
        Path.home() / ".npm-global/bin", Path.home() / ".local/bin/node/bin",
        Path.home() / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
    ]
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), *(str(path) for path in extra_paths)])
    requested = os.path.expanduser(env.get("CODEX_CLI_PATH", "codex"))
    binary = shutil.which(requested, path=env["PATH"])
    if binary is None:
        raise RuntimeError("找不到 Codex CLI；请安装 @openai/codex，或设置 CODEX_CLI_PATH 为可执行文件路径")
    return binary, env


def error_tail(handle):
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - 4096))
    detail = handle.read().decode("utf-8", errors="replace")
    detail = re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", detail)
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[已隐藏]", detail)
    return detail.strip()[-1800:]


class CodexTextGenerate:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {
                    "multiline": True,
                    "default": "为主题「雨夜重逢」写一段约 200 字的短剧解说，只输出正文。",
                }),
                "model": ("STRING", {
                    "default": DEFAULT_MODEL,
                    "tooltip": "Codex 模型标识，可编辑；默认 gpt-5.6-sol。需要当前账号有模型访问权限。",
                }),
                "reasoning_effort": (["low", "medium", "high", "xhigh", "max", "ultra"], {
                    "default": DEFAULT_EFFORT,
                    "tooltip": "low 低 / medium 中 / high 高 / xhigh 极高 / max 最大 / ultra。所选模型必须支持该档位。",
                }),
                "timeout_seconds": ("INT", {"default": 900, "min": 1, "max": 86400}),
            },
            "optional": {
                "instructions": ("STRING", {"multiline": True, "default": DEFAULT_INSTRUCTIONS}),
                "output_schema": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "留空输出普通文本；填写 JSON Schema 后通过 Codex --output-schema 约束最终 JSON。",
                }),
                "reuse_result": ("BOOLEAN", {"default": False, "tooltip": "复用相同输入的已保存结果，适合下游失败后续跑。"}),
                "generation_id": ("INT", {"default": 0, "min": 0, "max": 9007199254740991,
                    "tooltip": "启用复用时，修改编号生成新版本；不是模型随机种子。"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "generate"
    CATEGORY = "Codex/text"
    DESCRIPTION = (
        "通过本机 Codex CLI 生成文本，复用已保存的登录状态。默认 Sol + xhigh（极高），均可修改。"
        "默认每次运行重新生成；可开启结果复用，不自动重试。输出可连接文本预览、保存或图像/视频节点的提示词。"
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def generate(self, prompt, model, reasoning_effort, timeout_seconds,
                 instructions=DEFAULT_INSTRUCTIONS, output_schema="", reuse_result=False, generation_id=0):
        if not prompt.strip() or not model.strip():
            raise ValueError("请填写提示词和模型名")
        if timeout_seconds <= 0:
            raise ValueError("超时时间必须大于 0")
        schema = None
        if output_schema.strip():
            try:
                schema = json.loads(output_schema)
            except json.JSONDecodeError as error:
                raise ValueError(f"output_schema 不是有效 JSON：{error.msg}（第 {error.lineno} 行）") from None
            if not isinstance(schema, dict):
                raise ValueError("output_schema 必须是 JSON Schema 对象")

        cache_path = None
        if reuse_result:
            identity = [1, prompt, model.strip(), reasoning_effort, instructions, schema, generation_id]
            key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            cache_path = Path(folder_paths.get_user_directory()) / "codex_text/cache" / f"{key}.txt"
            if cache_path.is_file():
                return (cache_path.read_text(encoding="utf-8"),)

        binary, env = codex_environment()
        comfy.model_management.throw_exception_if_processing_interrupted()
        with tempfile.TemporaryDirectory(prefix="comfyui-codex-") as temporary:
            directory = Path(temporary)
            output_path = directory / "response.txt"
            command = [
                binary, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--color", "never", "--cd", str(directory),
                "--model", model.strip(), "--output-last-message", str(output_path),
                "-c", "approval_policy=\"never\"",
                "-c", "project_doc_max_bytes=0",
                "-c", "web_search=\"disabled\"",
                "-c", "model_reasoning_effort=" + json.dumps(reasoning_effort),
                "-c", "developer_instructions=" + json.dumps(TEXT_INSTRUCTIONS + instructions),
            ]
            for feature in DISABLED_FEATURES:
                command.extend(["--disable", feature])
            if schema is not None:
                schema_path = directory / "schema.json"
                schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
                command.extend(["--output-schema", str(schema_path)])
            command.append("-")

            # Pass long prompts via stdin, without shell interpolation or command-line exposure.
            with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as errors:
                source.write(prompt.encode("utf-8"))
                source.seek(0)
                process = subprocess.Popen(
                    command, stdin=source, stdout=subprocess.DEVNULL, stderr=errors,
                    cwd=directory, env=env, start_new_session=True,
                )
                deadline = time.monotonic() + timeout_seconds
                try:
                    while True:
                        comfy.model_management.throw_exception_if_processing_interrupted()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError(f"Codex 生成超过 {timeout_seconds} 秒；可提高超时时间或降低推理强度后重试")
                        try:
                            process.wait(timeout=min(0.2, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                    comfy.model_management.throw_exception_if_processing_interrupted()
                    if process.returncode:
                        raise RuntimeError(f"Codex 调用失败（退出码 {process.returncode}）：{error_tail(errors)}")
                finally:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()

            if not output_path.is_file():
                raise RuntimeError("Codex 未返回最终文本；请检查登录状态、模型访问权限和网络")
            text = output_path.read_text(encoding="utf-8").strip()
            if not text:
                raise RuntimeError("Codex 返回了空文本，请调整提示词后重试")
            if schema is not None:
                try:
                    text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
                except json.JSONDecodeError:
                    raise RuntimeError("Codex 最终输出不是有效 JSON；请检查 output_schema 和提示词") from None
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache_path.parent, delete=False) as handle:
                    handle.write(text)
                    temporary_path = Path(handle.name)
                os.replace(temporary_path, cache_path)
            return (text,)


NODE_CLASS_MAPPINGS = {"CodexTextGenerate": CodexTextGenerate}
NODE_DISPLAY_NAME_MAPPINGS = {"CodexTextGenerate": "Codex · 文本生成"}
