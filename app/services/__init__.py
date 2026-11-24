"""服务包：每个子包是一个可插拔的中转服务。

``load_services()`` 由 app.core.registry 实现，自动导入 ``app.services.<key>.service``，
各服务在导入时调用 ``register_service`` 完成注册。
"""
from app.core.registry import load_services  # noqa: F401
