# 插件：crypto（字段级加密）

> 代码位置：`mnemosyne_plugins/crypto/plugin.py`（`CryptoPluginFernet`）。

## 简介

基于 Fernet 的字段级对称加密插件，默认加密 `content` 字段。依赖 `cryptography`
库（`try/except` 导入），密钥缺失时优雅降级（不加密），核心零依赖不受影响。

## 启用

```bash
export MNEMOSYNE_CRYPTO_KEY="<Fernet 密钥>"
```

```python
from mnemosyne import MemoryBrain
brain = MemoryBrain("./mem", plugins=["crypto"])
brain.retain("机密内容")          # 落盘前 content 被加密
results = brain.recall("机密")    # 读取时自动解密
```

## 密钥来源（`_resolve_key`）

1. 构造参数 `key`。
2. 环境变量 `MNEMOSYNE_CRYPTO_KEY`。
3. brain 配置 `config["crypto_key"]`。

## 核心 API

| 方法 | 说明 |
|---|---|
| `encrypt(field, value)` | 加密敏感字段（非敏感字段/无密钥返回原值） |
| `decrypt(field, encrypted_value)` | 解密（失败返回原值不崩溃） |
| `get_key()` | 返回密钥 |
| `available` | 是否可用（`_fernet is not None`） |

`sensitive_fields = ("content",)`。

## 大脑集成

`retain()` 写入前调用 `crypto_plugin.encrypt("content", ...)`，`recall()` 读取后调用
`decrypt`；插件不可用时跳过（`logger.debug` 记录降级）。

## 生成密钥

```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

## 测试

`tests/test_plugins.py`、`security/report.py` 的加密往返测试（需安装 `cryptography`）。
