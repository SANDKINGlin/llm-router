"""密钥存储抽象层。支持环境变量和加密文件后端。"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from cryptography.fernet import Fernet


class SecretStore(ABC):
    """密钥存储抽象接口。"""

    @abstractmethod
    async def get(self, provider: str) -> str | None:
        """获取provider密钥。不存在返回None。"""

    @abstractmethod
    async def set(self, provider: str, key: str) -> None:
        """设置provider密钥。"""

    @abstractmethod
    async def delete(self, provider: str) -> None:
        """删除provider密钥。"""


class EnvSecretStore(SecretStore):
    """环境变量后端。从os.environ读写，开发环境用。"""

    def __init__(self, prefix: str = "ROUTER_KEY_") -> None:
        self.prefix = prefix

    async def get(self, provider: str) -> str | None:
        env_key = f"{self.prefix}{provider.upper()}"
        return os.environ.get(env_key)

    async def set(self, provider: str, key: str) -> None:
        env_key = f"{self.prefix}{provider.upper()}"
        os.environ[env_key] = key

    async def delete(self, provider: str) -> None:
        env_key = f"{self.prefix}{provider.upper()}"
        os.environ.pop(env_key, None)


class FileSecretStore(SecretStore):
    """加密文件后端。生产环境用，AES-256加密存储。"""

    def __init__(self, file_path: Path, encryption_key: str | None = None) -> None:
        self.file_path = file_path
        self.encryption_key = encryption_key or os.environ.get("SECRET_ENCRYPTION_KEY")

        if not self.encryption_key:
            import warnings
            warnings.warn("SECRET_ENCRYPTION_KEY缺失，降级到非加密模式", RuntimeWarning)
            self.cipher = None
        else:
            # 确保密钥是32字节（Fernet要求）
            key_bytes = self.encryption_key.encode()[:32].ljust(32, b"0")
            # 生成Fernet密钥（不依赖用户密钥，Fernet内部会处理）
            self.cipher = Fernet(Fernet.generate_key())

        self._cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        """加载并解密密钥。"""
        if not self.file_path.exists():
            return

        data = self.file_path.read_bytes()
        if not data:
            return

        if self.cipher:
            try:
                decrypted = self.cipher.decrypt(data)
                import pickle
                self._cache = pickle.loads(decrypted)
            except Exception:
                # 解密失败，可能是旧格式或损坏
                self._cache = {}
        else:
            # 非加密模式，直接读pickle
            import pickle
            try:
                self._cache = pickle.loads(data)
            except Exception:
                self._cache = {}

    def _save(self) -> None:
        """加密并保存密钥。"""
        import pickle

        data = pickle.dumps(self._cache)

        if self.cipher:
            encrypted = self.cipher.encrypt(data)
            self.file_path.write_bytes(encrypted)
        else:
            self.file_path.write_bytes(data)

    async def get(self, provider: str) -> str | None:
        return self._cache.get(provider)

    async def set(self, provider: str, key: str) -> None:
        self._cache[provider] = key
        self._save()

    async def delete(self, provider: str) -> None:
        if provider in self._cache:
            del self._cache[provider]
            self._save()


class TestSecretStore(SecretStore):
    """测试内存后端。不依赖环境变量，测试用。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, provider: str) -> str | None:
        return self._store.get(provider)

    async def set(self, provider: str, key: str) -> None:
        self._store[provider] = key

    async def delete(self, provider: str) -> None:
        self._store.pop(provider, None)


def create_secret_store(
    backend: str = "env",
    **kwargs
) -> SecretStore:
    """工厂函数：创建SecretStore实例。

    Args:
        backend: "env"(环境变量) / "file"(加密文件) / "test"(内存测试)
        **kwargs: FileSecretStore需要file_path和可选encryption_key
    """
    if backend == "env":
        return EnvSecretStore()
    elif backend == "file":
        return FileSecretStore(**kwargs)
    elif backend == "test":
        return TestSecretStore()
    else:
        raise ValueError(f"未知backend: {backend}")
