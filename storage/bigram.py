"""Tokenization helpers for the storage backends.

This module re-exports _tokenize and _tf_vector from the parent
mnemosyne module via lazy import to avoid circular dependency issues
(mnemosyne.py imports storage at runtime, so storage/bigram cannot
import mnemosyne at module load time).
"""


def _get_mnemosyne():
    """Lazily import mnemosyne to break circular imports."""
    import mnemosyne
    return mnemosyne


def _tokenize(text):
    """Multi-language tokenizer (delegates to mnemosyne._tokenize)."""
    m = _get_mnemosyne()
    return m._tokenize(text)


def _tf_vector(tokens):
    """Build a term-frequency Counter (delegates to mnemosyne._tf_vector)."""
    m = _get_mnemosyne()
    return m._tf_vector(tokens)
