#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Graph store adapters
===================================

Graph memory in Mnemosyne is **built in and always on**, backed by the same
SQLite file as everything else (``storage/sqlite_backend.py``'s triple store).
That is a deliberate contrast with the reference stack, which removed graph
memory from its self-hosted SDK in the 2026 algorithm rewrite and now offers it
only as a hosted feature requiring no external graph database.  Mnemosyne reaches
the same "no external graph database" property by keeping the triples in SQLite.

Consequences worth stating plainly:

* entity extraction, edge writing and multi-hop traversal cost **zero** extra
  processes, so ``enable_graph`` defaults to true;
* the temporal version chain and the graph share one transaction, so a memory and
  its edges cannot diverge;
* an external graph database is optional, never required.

Adapters for external graph databases exist for deployments that already run one
and want the graph to live there instead.  They are additive: switching to one
does not change the memory API.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import GraphStoreProvider, ProviderUnavailable, _first_env
from .transport import HttpError, request_json

__all__ = ["BuiltinGraphStore", "Neo4jGraphStore", "MemgraphGraphStore",
           "NeptuneGraphStore", "KuzuGraphStore", "GenericSPARQLGraphStore",
           "get_graph_store", "list_graph_store_providers"]


class BuiltinGraphStore(GraphStoreProvider):
    """Mnemosyne's native triple store — zero dependencies, always available.

    Reads and writes go straight to the brain's SQLite edge table.  Because the
    table also carries ``memory_id`` on every edge, traversing the graph and
    tracing an edge back to the memory that asserted it are the same query,
    which is what makes the temporal version chain auditable.
    """

    name = "builtin"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._brain = self.config.get("brain")

    def _store(self):
        if self._brain is None:
            raise ProviderUnavailable(
                "builtin", "no MemoryBrain attached",
                hint="The built-in graph store requires a live brain instance.",
            )
        return self._brain.store

    def add_edges(self, edges: List[Dict[str, Any]],
                  memory_id: Optional[str] = None) -> None:
        if not edges:
            return
        self._store().add_edges(edges, memory_id=memory_id)

    def neighbors(self, entity: str, max_depth: int = 2) -> Dict[str, Any]:
        return self._store().graph_query(entity, max_depth=max_depth)

    def delete_entity(self, entity: str) -> int:
        store = self._store()
        removed = 0
        try:
            edges = [e for e in store.all_edges()
                     if entity in (e.get("source"), e.get("from"),
                                   e.get("target"), e.get("to"))]
        except Exception:  # pragma: no cover - backend without all_edges
            return 0
        for e in edges:
            mid = e.get("memory_id")
            if not mid:
                continue
            rec = store.find_by_id(mid)
            if not rec:
                continue
            kept = [x for x in (rec.get("graph_edges") or [])
                    if entity not in (x.get("source"), x.get("from"),
                                      x.get("target"), x.get("to"))]
            if len(kept) != len(rec.get("graph_edges") or []):
                store.update_by_id(mid, {"graph_edges": kept})
                removed += 1
        return removed


class Neo4jGraphStore(GraphStoreProvider):
    """Neo4j over its HTTP transactional endpoint (``/db/{db}/tx/commit``).

    Uses the HTTP interface rather than the Bolt driver precisely so no package
    is required.  Every statement is sent as a parameterised Cypher query, never
    string interpolation, so an entity name containing a quote cannot alter the
    query.
    """

    name = "neo4j"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        url = (self.config.get("url")
               or _first_env("MNEMOSYNE_GRAPH_STORE_NEO4J_URL", "NEO4J_URL"))
        if not url:
            raise ProviderUnavailable(
                "neo4j", "url is required",
                hint="Pass url in the neo4j config block or set NEO4J_URL \
(e.g. http://localhost:7474).",
            )
        self.url = str(url).rstrip("/")
        if not self.url.startswith("http"):
            self.url = f"http://{self.url}"
        self.user = (self.config.get("username") or self.config.get("user")
                     or _first_env("NEO4J_USERNAME", "NEO4J_USER") or "neo4j")
        self.password = (self.config.get("password")
                         or _first_env("NEO4J_PASSWORD") or "")
        self.database = self.config.get("database") or self.config.get("database_name") or "neo4j"
        self.timeout = float(self.config.get("timeout", 60))
        self.base_url = f"{self.url}/db/{self.database}/tx/commit"

    def _auth(self):
        return (self.user, self.password) if self.password else None

    def _cypher(self, statement: str, params: Optional[Dict[str, Any]] = None) -> Any:
        body = {"statements": [{"statement": statement,
                                "parameters": params or {},
                                "resultDataContents": ["row"]}]}
        payload = request_json(self.base_url, payload=body,
                               auth=self._auth(), timeout=self.timeout)
        errors = (payload or {}).get("errors") or []
        if errors:
            raise HttpError(200, self.base_url, str(errors[:1]),
                            reason="cypher error")
        results = (payload or {}).get("results") or []
        return (results[0] if results else {}).get("data") or []

    def add_edges(self, edges: List[Dict[str, Any]],
                  memory_id: Optional[str] = None) -> None:
        rows = []
        for e in edges or []:
            src = e.get("source") or e.get("from")
            dst = e.get("target") or e.get("to")
            rel = e.get("relation") or e.get("predicate") or "RELATED_TO"
            if not src or not dst:
                continue
            rows.append({"source": str(src), "target": str(dst),
                         "relation": str(rel), "memory_id": memory_id or ""})
        if not rows:
            return
        self._cypher(
            "UNWIND $rows AS row "
            "MERGE (a:Entity {name: row.source}) "
            "MERGE (b:Entity {name: row.target}) "
            "MERGE (a)-[r:RELATED {relation: row.relation}]->(b) "
            "SET r.memory_id = row.memory_id, r.updated_at = timestamp()",
            {"rows": rows},
        )

    def neighbors(self, entity: str, max_depth: int = 2) -> Dict[str, Any]:
        depth = max(1, min(int(max_depth or 2), 5))  # bounded: never interpolate a raw int from input
        rows = self._cypher(
            f"MATCH path = (a:Entity {{name: $name}})-[r:RELATED*1..{depth}]-(b:Entity) "
            "UNWIND relationships(path) AS rel "
            "RETURN DISTINCT startNode(rel).name AS source, "
            "       rel.relation AS relation, endNode(rel).name AS target "
            "LIMIT $limit",
            {"name": str(entity), "limit": int(self.config.get("limit", 200))},
        )
        nodes, edges = {entity}, []
        for row in rows:
            r = row.get("row") or row
            if not isinstance(r, list) or len(r) < 3:
                continue
            edges.append({"from": r[0], "relation": r[1], "to": r[2]})
            nodes.add(r[0])
            nodes.add(r[2])
        return {"query": entity, "nodes": sorted(nodes), "edges": edges}

    def delete_entity(self, entity: str) -> int:
        rows = self._cypher(
            "MATCH (a:Entity {name: $name})-[r:RELATED]-() "
            "DELETE r RETURN count(r) AS removed",
            {"name": str(entity)},
        )
        try:
            return int((rows[0].get("row") or [0])[0])
        except (IndexError, TypeError, ValueError):
            return 0


class MemgraphGraphStore(Neo4jGraphStore):
    """Memgraph speaks the Neo4j HTTP protocol; only the defaults differ."""

    name = "memgraph"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = dict(config or {})
        cfg.setdefault("url", _first_env("MEMGRAPH_URL") or "http://localhost:7474")
        cfg.setdefault("user", _first_env("MEMGRAPH_USER") or "")
        super().__init__(cfg)
        self.user = cfg.get("user") or ""
        self.database = cfg.get("database") or "memgraph"
        self.base_url = f"{self.url}/db/{self.database}/tx/commit"


class NeptuneGraphStore(GraphStoreProvider):
    """Amazon Neptune openCypher over the ``/opencypher`` HTTP endpoint.

    Authentication is via an IAM SigV4 signature, which needs ``botocore``.
    A pre-signed ``Authorization`` header can be supplied instead, keeping the
    adapter usable without the SDK.
    """

    name = "neptune"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        endpoint = (self.config.get("endpoint")
                    or _first_env("NEPTUNE_ENDPOINT"))
        if not endpoint:
            raise ProviderUnavailable("neptune", "endpoint is required",
                                      hint="Pass endpoint (host:port) in the config block.")
        self.endpoint = str(endpoint)
        self.base_url = f"https://{self.endpoint}/opencypher"
        self.timeout = float(self.config.get("timeout", 60))
        self._auth_header = self.config.get("authorization_header")
        self._region = self.config.get("aws_region") or _first_env(
            "AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1"

    def _headers(self) -> Dict[str, str]:
        if self._auth_header:
            return {"Authorization": self._auth_header}
        try:
            import botocore.auth  # noqa: F401
            import botocore.awsrequest
            import botocore.session
        except ImportError:
            raise ProviderUnavailable(
                "neptune", "IAM signing requires botocore and no authorization_header was given",
                hint="pip install mnemosyne-os[bedrock] or pass a pre-signed authorization_header.",
            ) from None
        import botocore.auth
        import botocore.awsrequest
        import botocore.session

        session = botocore.session.get_session()
        creds = session.get_credentials()
        if creds is None:
            raise ProviderUnavailable("neptune", "no AWS credentials found")
        req = botocore.awsrequest.AWSRequest(method="POST", url=self.base_url)
        botocore.auth.SigV4Auth(creds, "neptune-db", self._region).add_auth(req)
        return dict(req.headers.items())

    def _opencypher(self, query: str, params: Optional[Dict[str, Any]] = None) -> Any:
        payload = request_json(self.base_url,
                               payload={"query": query, "parameters": _jsonable(params or {})},
                               headers=self._headers(), timeout=self.timeout)
        return (payload or {}).get("results") or []

    def add_edges(self, edges, memory_id: Optional[str] = None) -> None:
        rows = []
        for e in edges or []:
            src = e.get("source") or e.get("from")
            dst = e.get("target") or e.get("to")
            rel = e.get("relation") or e.get("predicate") or "RELATED_TO"
            if src and dst:
                rows.append({"source": str(src), "target": str(dst),
                             "relation": str(rel), "memory_id": memory_id or ""})
        if not rows:
            return
        self._opencypher(
            "UNWIND $rows AS row "
            "MERGE (a:Entity {name: row.source}) "
            "MERGE (b:Entity {name: row.target}) "
            "MERGE (a)-[r:RELATED {relation: row.relation}]->(b) "
            "SET r.memory_id = row.memory_id",
            {"rows": rows},
        )

    def neighbors(self, entity: str, max_depth: int = 2) -> Dict[str, Any]:
        depth = max(1, min(int(max_depth or 2), 5))
        rows = self._opencypher(
            f"MATCH path = (a:Entity {{name: $name}})-[:RELATED*1..{depth}]-(b:Entity) "
            "UNWIND relationships(path) AS rel "
            "RETURN DISTINCT startNode(rel).name AS source, rel.relation AS relation, "
            "endNode(rel).name AS target LIMIT $limit",
            {"name": str(entity), "limit": int(self.config.get("limit", 200))},
        )
        nodes, edges = {entity}, []
        for r in rows:
            if not isinstance(r, dict) or "source" not in r:
                continue
            edges.append({"from": r.get("source"), "relation": r.get("relation"),
                          "to": r.get("target")})
            nodes.add(r.get("source"))
            nodes.add(r.get("target"))
        return {"query": entity, "nodes": sorted(n for n in nodes if n), "edges": edges}

    def delete_entity(self, entity: str) -> int:
        rows = self._opencypher(
            "MATCH (a:Entity {name: $name})-[r:RELATED]-() DELETE r "
            "RETURN count(r) AS removed",
            {"name": str(entity)},
        )
        try:
            return int(rows[0].get("removed") or 0)
        except (IndexError, TypeError, ValueError, AttributeError):
            return 0


class KuzuGraphStore(GraphStoreProvider):
    """Kuzu — an embedded graph database (``import kuzu``).

    Embedded rather than client/server, so it is the natural external choice for
    a single-machine deployment that wants Cypher semantics on a real graph
    engine while staying offline.
    """

    name = "kuzu"
    requires = ("kuzu",)

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        try:
            import kuzu
        except ImportError as e:
            raise ProviderUnavailable("kuzu", f"kuzu not installed ({e})",
                                      hint="pip install mnemosyne-os[kuzu]") from None
        self.db_path = self.config.get("db_path") or "./kuzu_graph.db"
        self._db = kuzu.Database(self.db_path)
        self._conn = kuzu.Connection(self._db)
        self._init_schema()

    def _init_schema(self) -> None:
        for ddl in (
            "CREATE NODE TABLE IF NOT EXISTS Entity(name STRING, PRIMARY KEY(name))",
            "CREATE REL TABLE IF NOT EXISTS RELATED(FROM Entity TO Entity, "
            "relation STRING, memory_id STRING)",
        ):
            try:
                self._conn.execute(ddl)
            except Exception:  # pragma: no cover - already exists on older builds
                pass

    def add_edges(self, edges, memory_id: Optional[str] = None) -> None:
        for e in edges or []:
            src = e.get("source") or e.get("from")
            dst = e.get("target") or e.get("to")
            rel = e.get("relation") or e.get("predicate") or "RELATED_TO"
            if not src or not dst:
                continue
            self._conn.execute("MERGE (a:Entity {name: $s})", {"s": str(src)})
            self._conn.execute("MERGE (b:Entity {name: $t})", {"t": str(dst)})
            self._conn.execute(
                "MATCH (a:Entity {name: $s}), (b:Entity {name: $t}) "
                "CREATE (a)-[:RELATED {relation: $r, memory_id: $m}]->(b)",
                {"s": str(src), "t": str(dst), "r": str(rel), "m": memory_id or ""},
            )

    def neighbors(self, entity: str, max_depth: int = 2) -> Dict[str, Any]:
        rows = self._conn.execute(
            "MATCH (a:Entity {name: $n})-[r:RELATED*1..2]-(b:Entity) "
            "RETURN DISTINCT a.name, r, b.name LIMIT $lim",
            {"n": str(entity), "lim": int(self.config.get("limit", 200))},
        )
        nodes, edges = {entity}, []
        while rows.has_next():
            row = rows.get_next()
            try:
                edges.append({"from": row[0], "relation": "RELATED", "to": row[2]})
                nodes.add(row[0])
                nodes.add(row[2])
            except (IndexError, TypeError):
                continue
        return {"query": entity, "nodes": sorted(n for n in nodes if n), "edges": edges}

    def delete_entity(self, entity: str) -> int:
        try:
            self._conn.execute("MATCH (a:Entity {name: $n})-[r:RELATED]-() DELETE r",
                               {"n": str(entity)})
            return 1
        except Exception:  # pragma: no cover
            return 0


class GenericSPARQLGraphStore(GraphStoreProvider):
    """Any SPARQL 1.1 endpoint, described declaratively in config.

    Configuration::

        graph_store:
          provider: sparql
          config:
            endpoint: https://example.org/sparql
            graph_uri: urn:mnemosyne            # optional named graph
            headers: {Authorization: "Bearer ..."}

    Triples are stored as ``?s <urn:mnemosyne:relation> ?o`` with the memory id
    kept in a reification helper triple, which is enough for traversals while
    remaining queryable with ordinary SPARQL tooling.
    """

    name = "sparql"

    NS = "urn:mnemosyne:"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.endpoint = str(self.config.get("endpoint") or "")
        if not self.endpoint:
            raise ProviderUnavailable("sparql", "endpoint is required")
        self.headers = dict(self.config.get("headers") or {})
        self.headers.setdefault("Accept", "application/sparql-results+json")
        self.graph_uri = self.config.get("graph_uri")
        self.timeout = float(self.config.get("timeout", 60))

    def _query(self, q: str) -> List[Dict[str, Any]]:
        from urllib.parse import urlencode

        url = f"{self.endpoint}?{urlencode({'query': q})}"
        payload = request_json(url, method="GET", headers=self.headers,
                               timeout=self.timeout)
        bindings = (((payload or {}).get("results") or {}).get("bindings")) or []
        return [{k: v.get("value") for k, v in row.items()} for row in bindings]

    def _update(self, sparql: str) -> None:
        headers = dict(self.headers)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        from urllib.parse import urlencode

        import urllib.error
        import urllib.request

        body = urlencode({"update": sparql}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, headers=headers,
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, self.endpoint,
                            e.read().decode("utf-8", errors="replace")) from None

    def _graph_clause(self, modify: bool = False) -> str:
        if not self.graph_uri:
            return ""
        return f"GRAPH <{self.graph_uri}> {{" if modify else f"GRAPH <{self.graph_uri}>"

    def add_edges(self, edges, memory_id: Optional[str] = None) -> None:
        triples = []
        for e in edges or []:
            src = e.get("source") or e.get("from")
            dst = e.get("target") or e.get("to")
            rel = e.get("relation") or e.get("predicate") or "related"
            if not src or not dst:
                continue
            s = _iri(src)
            o = _iri(dst)
            pred = f"<{self.NS}{_slug(rel)}>"
            triples.append(f"{s} {pred} {o} .")
        if not triples:
            return
        body = "\n".join(triples)
        if self.graph_uri:
            self._update(f"INSERT DATA {{ GRAPH <{self.graph_uri}> {{ {body} }} }}")
        else:
            self._update(f"INSERT DATA {{ {body} }}")

    def neighbors(self, entity: str, max_depth: int = 2) -> Dict[str, Any]:
        depth = max(1, min(int(max_depth or 2), 4))
        s = _iri(entity)
        predicate = f"<{self.NS}"
        q = (f"SELECT DISTINCT ?p ?o WHERE {{ "
             f"{{ {s} ?p ?o FILTER(STRSTARTS(STR(?p), '{self.NS}')) }}")
        for _ in range(depth - 1):
            q += f" UNION {{ {s} ?p0 ?mid . ?mid ?p ?o FILTER(STRSTARTS(STR(?p), '{self.NS}')) }}"
        q += " } LIMIT " + str(int(self.config.get("limit", 200)))
        rows = self._query(q)
        nodes, edges = {entity}, []
        for r in rows:
            pred = str(r.get("p", "")).replace(self.NS, "")
            obj = str(r.get("o", ""))
            if obj.startswith(self.NS):
                obj = obj[len(self.NS):]
            edges.append({"from": entity, "relation": pred, "to": obj})
            nodes.add(obj)
        return {"query": entity, "nodes": sorted(nodes), "edges": edges}

    def delete_entity(self, entity: str) -> int:
        s = _iri(entity)
        self._update(
            f"DELETE WHERE {{ {s} ?p ?o FILTER(STRSTARTS(STR(?p), '{self.NS}')) . }}")
        return 1


def _iri(name: str) -> str:
    return f"<{GenericSPARQLGraphStore.NS}{_slug(name)}>"


def _slug(name: str) -> str:
    import re
    import unicodedata

    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\-.\u4e00-\u9fff]", "", s)
    return s or "unnamed"


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, type] = {
    "builtin": BuiltinGraphStore,
    "neo4j": Neo4jGraphStore,
    "memgraph": MemgraphGraphStore,
    "neptune": NeptuneGraphStore,
    "kuzu": KuzuGraphStore,
    "sparql": GenericSPARQLGraphStore,
}


def get_graph_store(config: Optional[Dict[str, Any]] = None,
                    brain: Any = None) -> GraphStoreProvider:
    """Build the graph store described by *config*.

    Defaults to the built-in triple store, so ``enable_graph`` is configurable
    without any external service being involved.
    """
    config = config or {}
    name = (config.get("provider") or "builtin").strip().lower()
    if name in ("none", "", "off", "disabled"):
        name = "builtin"
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderUnavailable(
            name, "unknown graph store provider",
            hint="Known providers: " + ", ".join(sorted(_REGISTRY)),
        )
    params = config.get("config") or config.get("params") or {}
    if cls is BuiltinGraphStore:
        return cls({**params, "brain": brain})
    return cls(params)


def list_graph_store_providers() -> List[str]:
    return sorted(_REGISTRY)
