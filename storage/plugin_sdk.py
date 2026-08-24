"""Plugin SDK for Mnemosyne OS — abstract base classes and loader.

Zero-dependency: only Python standard library.  Each plugin is an
ordinary Python file with a ``register(brain)`` function that returns
an instance of a plugin class.  Plugins are loaded from the
``mnemosyne_plugins/`` directory (or any user-supplied path).

Plugin contract
---------------
A plugin module must expose either:

1. A ``register(brain)`` callable that returns a plugin instance, **or**

2. A ``get_plugin_class()`` callable that returns a class, which is then
   instantiated with ``brain`` as the sole argument.

The instance must implement at least one of the plugin interfaces
below.  All interfaces are optional — the brain calls only what exists.

Plugin interfaces
-----------------
:class:`VectorBackendPlugin`  — alternative vector store / retrieval
:class:`CryptoPlugin`         — field-level encryption / decryption
:class:`RerankerPlugin`       — result re-ranking with a custom score

Example plugin::

    from storage.plugin_sdk import RerankerPlugin

    class MyReranker(RerankerPlugin):
        def rerank(self, query, results, **kwargs):
            ...

    def register(brain):
        return MyReranker(brain)
"""
import importlib
import importlib.util
import os
import sys

__all__ = [
    "VectorBackendPlugin",
    "CryptoPlugin",
    "RerankerPlugin",
    "PluginInfo",
    "load_plugins",
    "load_plugin",
]


class PluginInfo:
    """Metadata about a loaded plugin."""

    def __init__(self, name, path, cls, instance, enabled=True):
        self.name = name
        self.path = path
        self.cls = cls
        self.instance = instance
        self.enabled = enabled
        self.error = None

    def to_dict(self):
        return {
            "name": self.name,
            "path": self.path,
            "class": self.cls.__name__ if self.cls else None,
            "enabled": self.enabled,
            "error": self.error,
        }


class VectorBackendPlugin:
    """Abstract interface for alternative vector backends.

    A plugin that implements this interface can replace the built-in
    random-projection EmbeddingEngine for storage and retrieval of
    dense vectors.
    """

    name = "vector_backend"
    description = "Alternative vector storage and similarity backend"

    def __init__(self, brain=None):
        self.brain = brain

    def add(self, memory_id, vector, **kwargs):
        raise NotImplementedError

    def search(self, query_vector, top_k=5, **kwargs):
        raise NotImplementedError

    def encode(self, text):
        raise NotImplementedError

    def save(self, brain):
        pass

    def load(self, brain):
        pass


class CryptoPlugin:
    """Abstract interface for field-level encryption.

    When active, the brain calls ``encrypt(field, value)`` before
    storing a sensitive field and ``decrypt(field, value)`` after
    loading it, so the on-disk representation is encrypted.
    """

    name = "crypto"
    description = "Field-level encryption for sensitive memory fields"

    def __init__(self, brain=None):
        self.brain = brain

    def encrypt(self, field, value, **kwargs):
        raise NotImplementedError

    def decrypt(self, field, encrypted_value, **kwargs):
        raise NotImplementedError

    def get_key(self):
        raise NotImplementedError


class RerankerPlugin:
    """Abstract interface for result re-ranking.

    After the retrieval engine returns a list of (score, record, reasons)
    tuples, the reranker can re-score and re-order them using a custom
    formula.
    """

    name = "reranker"
    description = "Re-ranking of retrieval results"

    def __init__(self, brain=None):
        self.brain = brain

    def rerank(self, query, results, **kwargs):
        raise NotImplementedError


def load_plugin(file_path, brain=None):
    """Load a single plugin module from *file_path*.

    Returns ``(PluginInfo, instance_or_none)``.

    On failure the error is captured in ``PluginInfo.error`` and
    ``instance`` is ``None`` — the caller decides whether to skip.
    """
    name = os.path.splitext(os.path.basename(file_path))[0]
    try:
        spec = importlib.util.spec_from_file_location(
            f"_mnemosyne_plugin_{name}", file_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create spec for {file_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod.__name__] = mod
        spec.loader.exec_module(mod)
        cls = None
        instance = None
        if hasattr(mod, "register"):
            instance = mod.register(brain)
            cls = instance.__class__ if instance else None
        elif hasattr(mod, "get_plugin_class"):
            cls = mod.get_plugin_class()
            instance = cls(brain) if cls else None
        return PluginInfo(name, file_path, cls, instance), instance
    except Exception as exc:
        return PluginInfo(name, file_path, None, None), None


def load_plugins(plugins_dir, brain=None, pattern="*.py"):
    """Auto-discover and load all plugins in *plugins_dir*.

    Scans the directory (non-recursive) for ``plugin.py`` files or any
    ``*.py`` file matching *pattern*.  Each must expose ``register()``
    or ``get_plugin_class()``.

    Returns a list of :class:`PluginInfo` objects.  Failed plugins are
    included with ``error`` set and ``instance`` as ``None`` — they
    never crash the core.
    """
    infos = []
    if not plugins_dir or not os.path.isdir(plugins_dir):
        return infos
    import glob
    candidates = sorted(glob.glob(os.path.join(plugins_dir, pattern)))
    # Also look for plugin.py in immediate subdirectories
    subdirs = sorted(
        d for d in os.listdir(plugins_dir)
        if os.path.isdir(os.path.join(plugins_dir, d))
    )
    for subdir in subdirs:
        ppath = os.path.join(plugins_dir, subdir, "plugin.py")
        if os.path.isfile(ppath):
            candidates.append(ppath)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        rp = os.path.realpath(c)
        if rp not in seen and os.path.basename(c) == "plugin.py":
            seen.add(rp)
            unique.append(c)
    for fpath in unique:
        info, instance = load_plugin(fpath, brain)
        if instance is None and info.error is None:
            info.error = "Plugin produced no instance"
        if instance is None:
            if info.error is None:
                info.error = "Unknown load failure"
        # Use the plugin's declared name if available (more descriptive than "plugin")
        if instance is not None:
            declared = getattr(instance, "name", None) or getattr(instance.__class__, "name", None)
            if declared:
                info.name = declared
        info.enabled = instance is not None
        infos.append(info)
    return infos
