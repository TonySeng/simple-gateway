"""密钥文件守卫测试。

覆盖 resolve_secrets() 的 fail-fast 语义：
- 密钥文件存在但不可读（属主错误）→ 必须 RuntimeError，禁止生成临时密钥
- 密钥文件损坏 → 必须 RuntimeError
- 写入失败（数据卷不可写）→ 必须 RuntimeError
- 正常路径 → 生成/沿用，内容稳定

对应线上事故：deploy_restore.sh 以 root 写入 .secrets.json（属主 root:root 0600），
容器以 appuser(999) 运行读不到，旧实现静默改用临时密钥，导致此后录入的
厂商凭证全部不可解。
"""
from __future__ import annotations

import builtins
import json
import os
import tempfile

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings, resolve_secrets

PASSED = 0
FAILED = 0


def check(name: str, fn):
    global PASSED, FAILED
    try:
        fn()
        PASSED += 1
        print(f"  OK   {name}")
    except Exception as e:  # noqa: BLE001
        FAILED += 1
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


def _reset(secrets_file: str):
    settings.secrets_file = secrets_file
    settings.fernet_key = ""
    settings.jwt_secret = ""


class _patched_open:
    """临时替换内置 open，对目标路径的指定模式抛异常。"""

    def __init__(self, path: str, mode_kw: str, exc: Exception):
        self.path = os.path.abspath(path)
        self.mode_kw = mode_kw
        self.exc = exc

    def __enter__(self):
        self._orig = builtins.open

        def fake(file, mode="r", *a, **kw):
            try:
                same = os.path.abspath(str(file)) == self.path
            except Exception:  # noqa: BLE001
                same = False
            if same and self.mode_kw in mode:
                raise self.exc
            return self._orig(file, mode, *a, **kw)

        builtins.open = fake
        return self

    def __exit__(self, *exc_info):
        builtins.open = self._orig
        return False


def t_generate_when_missing():
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, ".secrets.json")
        _reset(sf)
        resolve_secrets()
        assert os.path.exists(sf), "应生成密钥文件"
        data = json.load(open(sf, encoding="utf-8"))
        assert data["fernet_key"] == settings.fernet_key
        assert data["jwt_secret"] == settings.jwt_secret
        Fernet(settings.fernet_key.encode())  # 合法性


def t_reuse_existing():
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, ".secrets.json")
        key = Fernet.generate_key().decode()
        with open(sf, "w", encoding="utf-8") as f:
            json.dump({"fernet_key": key, "jwt_secret": "j" * 64}, f)
        _reset(sf)
        resolve_secrets()
        assert settings.fernet_key == key, "应沿用既有密钥，不得重新生成"


def t_read_permission_denied_must_raise():
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, ".secrets.json")
        with open(sf, "w", encoding="utf-8") as f:
            json.dump({"fernet_key": Fernet.generate_key().decode(), "jwt_secret": "x"}, f)
        _reset(sf)
        try:
            with _patched_open(sf, "r", PermissionError(13, "Permission denied")):
                resolve_secrets()
        except RuntimeError as e:
            assert "无法读取" in str(e) or "属主" in str(e), str(e)
            assert settings.fernet_key == "", "不得生成临时密钥"
            return
        raise AssertionError("读不到密钥时必须 RuntimeError，而不是静默生成临时密钥")


def t_corrupt_json_must_raise():
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, ".secrets.json")
        with open(sf, "w", encoding="utf-8") as f:
            f.write("{not-json")
        _reset(sf)
        try:
            resolve_secrets()
        except RuntimeError as e:
            assert "损坏" in str(e), str(e)
            assert settings.fernet_key == ""
            return
        raise AssertionError("密钥文件损坏时必须 RuntimeError")


def t_write_failure_must_raise():
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, ".secrets.json")
        _reset(sf)
        try:
            with _patched_open(sf, "w", PermissionError(13, "Permission denied")):
                resolve_secrets()
        except RuntimeError as e:
            assert "写入失败" in str(e), str(e)
            return
        raise AssertionError("密钥无法持久化时必须 RuntimeError")


def t_invalid_fernet_key_must_raise():
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, ".secrets.json")
        with open(sf, "w", encoding="utf-8") as f:
            json.dump({"fernet_key": "not-a-fernet-key", "jwt_secret": "x"}, f)
        _reset(sf)
        try:
            resolve_secrets()
        except RuntimeError as e:
            assert "Fernet" in str(e), str(e)
            return
        raise AssertionError("非法 Fernet 密钥必须 RuntimeError")


def t_decrypt_fails_loudly_with_wrong_key():
    """反向佐证：换密钥后旧密文解不开（InvalidToken → decrypt 返回原串）。"""
    from app.core import crypto

    k1 = Fernet.generate_key().decode()
    k2 = Fernet.generate_key().decode()
    settings.fernet_key = k1
    crypto._fernet = None
    token = crypto.encrypt_json({"api_key": "sk-test"})
    settings.fernet_key = k2
    crypto._fernet = None
    assert crypto.decrypt_json(token) == {}, "错误密钥不应解出内容"
    settings.fernet_key = k1
    crypto._fernet = None
    assert crypto.decrypt_json(token) == {"api_key": "sk-test"}


def main():
    print("== 密钥文件守卫 ==")
    base = os.environ.get("SG_SECRETS_FILE", "")
    try:
        check("缺失时生成并持久化", t_generate_when_missing)
        check("已存在时沿用不覆盖", t_reuse_existing)
        check("读取无权限 → 拒绝启动", t_read_permission_denied_must_raise)
        check("JSON 损坏 → 拒绝启动", t_corrupt_json_must_raise)
        check("写入失败 → 拒绝启动", t_write_failure_must_raise)
        check("非法 Fernet 密钥 → 拒绝启动", t_invalid_fernet_key_must_raise)
        check("换密钥后旧密文不可解", t_decrypt_fails_loudly_with_wrong_key)
    finally:
        settings.secrets_file = base
    print(f"\n结果: {PASSED} 通过, {FAILED} 失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
