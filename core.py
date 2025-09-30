# repo-architecture-map — core engine
# Pure standard library. Heuristic, resilient, and fast.
#
# WHAT THIS DOES
# 1) Scans for:
#    - docker-compose*.yml|yaml at repo root
#    - Kubernetes YAMLs in common folders (k8s, kubernetes, deploy, manifests, charts)
#    - package.json (framework + potential PORT hints)
#    - .env / .env.* (DB/broker hints)
# 2) Builds a typed graph (compose / k8s / external) with edges for depends_on, ingress->service, service->workload, public exposure, and DB links.
# 3) Renders a rich Mermaid diagram with classes, theme presets, and a tiny legend.
#
# DESIGN PRINCIPLES
# - Zero dependencies. No YAML parser. Robust regex/indent heuristics for common cases.
# - "90% accurate in 1s" beats "99% accurate in 10s". It's a jumpstart, not a compiler.
# - Output is human-first: gorgeous by default, trivial to tweak in README.

import glob, json, os, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set

# ---------------------------- Data model ----------------------------

@dataclass
class Node:
    id: str
    label: str
    group: str  # "compose" | "k8s" | "external"
    tags: Set[str] = field(default_factory=set)  # e.g., {"db","public","svc","deploy"}
    ports: Set[str] = field(default_factory=set)
    meta: Dict[str, str] = field(default_factory=dict)

@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""

@dataclass
class Graph:
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)
    summary: str = ""

    def node(self, id, label=None, group="external", *, add_tag=None, ports=None, **meta):
        label = label or id
        if id not in self.nodes:
            self.nodes[id] = Node(id=id, label=label, group=group)
        n = self.nodes[id]
        n.label = n.label or label
        n.meta.update(meta)
        if add_tag:
            if isinstance(add_tag, (list, set, tuple)): n.tags.update(add_tag)
            else: n.tags.add(add_tag)
        if ports:
            n.ports.update(ports)
        return n

    def link(self, a, b, label=""):
        self.edges.append(Edge(src=a, dst=b, label=label))

# ---------------------------- Utilities ----------------------------

DB_HINTS = {
    "postgres": "Postgres", "postgresql": "Postgres", "psql": "Postgres",
    "redis": "Redis", "rediscache": "Redis",
    "mongo": "MongoDB", "mongodb": "MongoDB",
    "mysql": "MySQL", "mariadb": "MariaDB",
    "kafka": "Kafka", "rabbit": "RabbitMQ", "amqp": "RabbitMQ",
    "elasticsearch": "Elasticsearch", "opensearch": "OpenSearch"
}

FRAMEWORK_HINTS = ("express","fastify","nest","koa","next","sveltekit","django","flask","fastapi","rails")

def _safe_read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def _sanitize_id(prefix: str, name: str) -> str:
    # Mermaid node IDs must be unique and safe; keep readable.
    safe = re.sub(r"[^A-Za-z0-9:_\-/\.]", "_", name)
    return f"{prefix}:{safe}"

# ---------------------------- Compose scanner ----------------------------

def _parse_compose_services(text: str):
    """
    Naive YAML-ish parser tailored for docker-compose:
    - detects services by indentation under 'services:'
    - collects 'ports' (host:container[/proto]) and 'depends_on' (list or map)
    - collects 'networks' (names)
    Returns: dict[name] = {"ports":[(host,container)], "depends":[name], "nets":[...]}
    """
    lines = text.splitlines()
    services = {}
    in_services = False
    svc_name = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*services\s*:\s*$", ln):
            in_services = True; svc_name = None; continue
        if in_services:
            # New service (indented key)
            ms = re.match(r"^\s{2,}([A-Za-z0-9._-]+)\s*:\s*$", ln)
            if ms:
                svc_name = ms.group(1)
                services[svc_name] = {"ports": [], "depends": [], "nets": []}
                continue
            # Exit services block if dedented to root key
            if svc_name is None and re.match(r"^[A-Za-z].*:\s*$", ln):
                in_services = False
                continue
            if svc_name:
                # ports items
                pitem = re.match(r'^\s{6,}-\s*"?(\d+)\s*:\s*(\d+)(?:/(tcp|udp))?"?\s*$', ln)
                if pitem:
                    services[svc_name]["ports"].append((pitem.group(1), pitem.group(2)))
                # depends_on: list items
                ditem = re.match(r"^\s{6,}-\s*([A-Za-z0-9._-]+)\s*$", ln)
                if ditem and "depends_on" in (lines[i-1] if i>0 else ""):
                    services[svc_name]["depends"].append(ditem.group(1))
                # depends_on as map:
                dmap = re.match(r"^\s{6,}([A-Za-z0-9._-]+)\s*:\s*\{\s*condition\s*:\s*[A-Za-z_]+\s*\}\s*$", ln)
                if dmap and "depends_on" in (lines[i-1] if i>0 else ""):
                    services[svc_name]["depends"].append(dmap.group(1))
                # networks list
                nitem = re.match(r"^\s{6,}-\s*([A-Za-z0-9._-]+)\s*$", ln)
                if nitem and "networks" in (lines[i-1] if i>0 else ""):
                    services[svc_name]["nets"].append(nitem.group(1))
    return services

# ---------------------------- Kubernetes scanner ----------------------------

def _parse_k8s_units(text: str):
    """
    Minimal scanner for Kubernetes docs:
    - detects 'kind:' and first matching 'metadata: name:'
    - captures 'containerPort:' and Service 'port:' lines
    - captures 'type:' for Service (NodePort/LoadBalancer) and Ingress presence
    Returns list of dict(kind,name,ports,set('public'?) )
    """
    docs = re.split(r"\n---\s*\n", text) if "---" in text else [text]
    units = []
    for doc in docs:
        kind = None; name = None; ports = set(); svc_type = None; is_ingress = False
        for ln in doc.splitlines():
            m1 = re.match(r"^\s*kind\s*:\s*(Deployment|StatefulSet|DaemonSet|Service|Ingress)\s*$", ln)
            if m1:
                kind = m1.group(1); continue
            if name is None:
                m2 = re.match(r"^\s*name\s*:\s*([a-z0-9\-_.]+)\s*$", ln)
                if m2:
                    name = m2.group(1); continue
            # ports
            mcp = re.search(r"containerPort\s*:\s*(\d+)", ln)
            if mcp: ports.add(mcp.group(1))
            msp = re.search(r"\bport\s*:\s*(\d+)", ln)
            if msp and (kind == "Service"):
                ports.add(msp.group(1))
            # service type
            if kind == "Service":
                mt = re.search(r"\btype\s*:\s*(LoadBalancer|NodePort)\b", ln, flags=re.I)
                if mt: svc_type = mt.group(1)
            # ingress
            if kind == "Ingress":
                is_ingress = True
        if kind and name:
            units.append({"kind": kind, "name": name, "ports": ports, "svc_type": svc_type, "ingress": is_ingress})
    return units

# ---------------------------- package.json & .env sniffers ----------------------------

def _sniff_package_json(root: str):
    frameworks = []
    port = None
    p = os.path.join(root, "package.json")
    if os.path.isfile(p):
        try:
            data = json.loads(_safe_read(p))
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            for k in deps:
                if any(k.lower() == h for h in FRAMEWORK_HINTS):
                    frameworks.append(k)
            for s in (data.get("scripts") or {}).values():
                m = re.search(r"PORT\s*=\s*(\d+)", s) or re.search(r"--port\s+(\d+)", s)
                if m: port = m.group(1); break
        except Exception:
            pass
    return frameworks, port

def _sniff_env(root: str):
    # scan .env and .env.* (but not large binaries); collect DB/broker hints
    hints = set()
    candidates = [os.path.join(root, ".env")] + glob.glob(os.path.join(root, ".env.*"))
    for p in candidates[:20]:
        if not os.path.isfile(p): continue
        text = _safe_read(p).lower()
        for needle, label in DB_HINTS.items():
            if needle in text:
                hints.add(label)
    return hints

# ---------------------------- Graph assembly ----------------------------

def scan_repo(root: str) -> Graph:
    g = Graph()
    # compose files at root
    compose_files = glob.glob(os.path.join(root, "docker-compose*.y*ml"))
    # k8s manifests under common folders
    k8s_files = []
    for d in ("k8s","kubernetes","deploy","manifests","charts"):
        k8s_files += glob.glob(os.path.join(root, d, "**", "*.y*ml"), recursive=True)

    frameworks, pkg_port = _sniff_package_json(root)
    env_hints = _sniff_env(root)

    internet_needed = False

    # Compose nodes & edges
    for f in compose_files:
        services = _parse_compose_services(_safe_read(f))
        for name, info in services.items():
            nid = _sanitize_id("compose", name)
            ports = {h for (h, c) in info["ports"]}
            n = g.node(nid, label=f"{name}{(':'+','.join(sorted(ports))) if ports else ''}", group="compose", ports=ports)
            if ports: n.tags.add("public"); internet_needed = True
            for dep in info["depends"]:
                did = _sanitize_id("compose", dep)
                g.node(did, label=dep, group="compose")
                g.link(nid, did, "depends_on")

    # Kubernetes units & edges
    svc_names = set()
    dep_like_names = set()
    ingress_names = set()
    for f in k8s_files:
        for u in _parse_k8s_units(_safe_read(f)):
            kind, name = u["kind"], u["name"]
            nid = _sanitize_id("k8s", name)
            ports = set(u["ports"]) if u["ports"] else set()
            label_suffix = f":{','.join(sorted(ports))}" if ports else ""
            n = g.node(nid, label=f"{name}{label_suffix}", group="k8s", ports=ports, kind=kind)
            if kind == "Service":
                n.tags.update({"svc"})
                svc_names.add(name)
                if u["svc_type"]: n.tags.add("public"); internet_needed = True
            elif kind in ("Deployment","StatefulSet","DaemonSet"):
                n.tags.update({"workload"})
                dep_like_names.add(name)
            elif kind == "Ingress":
                n.tags.update({"ingress", "public"}); internet_needed = True
                ingress_names.add(name)

    # Heuristic links within K8s: Service -> Workload (same/base name), Ingress -> Service (same/base)
    def _variants(nm: str):
        # support common -svc,-service,-api patterns loosely
        base = re.sub(r"-(svc|service)$", "", nm)
        return {nm, base, base+"-svc", base+"-service", base+"-api", base+"-app"}

    for s in list(svc_names):
        sid = _sanitize_id("k8s", s)
        candidates = _variants(s)
        match = next((d for d in dep_like_names if d in candidates), None)
        if match:
            g.link(sid, _sanitize_id("k8s", match), "selects")

    for ig in list(ingress_names):
        iid = _sanitize_id("k8s", ig)
        candidates = _variants(ig)
        match = next((s for s in svc_names if s in candidates), None)
        if match:
            g.link(iid, _sanitize_id("k8s", match), "routes")

    # External DB nodes (from env hints)
    for label in sorted(env_hints):
        g.node(_sanitize_id("ext", label.lower()), label=label, group="external", add_tag="db")

    # Link runtime services to DBs (best-effort)
    if env_hints:
        target_ext = _sanitize_id("ext", sorted(env_hints)[0].lower())
        for n in list(g.nodes.values()):
            if n.group in ("compose","k8s") and (("svc" in n.tags) or ("workload" in n.tags) or (n.group=="compose")):
                g.link(n.id, target_ext, "uses")

    # Internet node if any public exposure
    if internet_needed:
        g.node("ext:internet", label="Internet", group="external", add_tag="public")
        for n in list(g.nodes.values()):
            if (("public" in n.tags) and n.id != "ext:internet"):
                g.link("ext:internet", n.id)

    # Summary text
    n_compose = len([n for n in g.nodes.values() if n.group=="compose"])
    n_k8s = len([n for n in g.nodes.values() if n.group=="k8s"])
    n_ext = len([n for n in g.nodes.values() if n.group=="external"])
    exposures = len([n for n in g.nodes.values() if "public" in n.tags])
    g.summary = (
        f"Compose: {n_compose} · K8s: {n_k8s} · External: {n_ext} · "
        f"Frameworks: {', '.join(frameworks) if frameworks else 'n/a'}"
        + (f" · App port: {pkg_port}" if pkg_port else "")
        + (f" · Public endpoints: {exposures}" if exposures else "")
    )
    return g

# ---------------------------- Mermaid rendering ----------------------------

def _theme_block(theme: str):
    # theme presets via Mermaid init directive (GitHub supports this)
    # 'auto' uses dark if GITHUB_DARK_MODE / terminal hint, else light.
    if theme == "auto":
        prefer_dark = any(os.getenv(k) for k in ("GITHUB_DARK_MODE","DARK","THEME_DARK"))
        theme = "dark" if prefer_dark else "light"
    if theme == "plain":
        return "%%{init: {'flowchart': {'curve': 'monotoneX'}}}%%"
    if theme == "dark":
        return ("%%{init: {'theme':'dark','flowchart':{'curve':'monotoneX'},"
                "'themeVariables':{'primaryColor':'#0ea5e9','primaryTextColor':'#ffffff','lineColor':'#38bdf8'}}}%%")
    if theme == "light":
        return ("%%{init: {'theme':'base','flowchart':{'curve':'monotoneX'},"
                "'themeVariables':{'primaryColor':'#0ea5e9','primaryTextColor':'#111827','lineColor':'#0ea5e9'}}}%%")
    return "%%{init: {'flowchart': {'curve': 'monotoneX'}}}%%"

def build_mermaid(g: Graph, *, theme="auto", style="fancy", include_legend=True) -> str:
    lines = []
    lines.append("```mermaid")
    lines.append(_theme_block(theme))
    lines.append("flowchart LR")

    # classes
    if style != "plain":
        lines.append("  classDef compose fill:#0ea5e9,stroke:#0369a1,color:#fff,stroke-width:1px;")
        lines.append("  classDef k8s fill:#22c55e,stroke:#166534,color:#062;")
        lines.append("  classDef external fill:#e2e8f0,stroke:#64748b,color:#111;")
        lines.append("  classDef public stroke-dasharray: 3 2,stroke-width:2px;")
        lines.append("  classDef db fill:#fde68a,stroke:#b45309,color:#3b2f00;")

    def subgraph(title, group):
        items = [n for n in g.nodes.values() if n.group==group]
        if not items: return
        lines.append(f"  subgraph {title}")
        for n in items:
            shape_open = "(" if "db" in n.tags else "["
            shape_close = ")" if "db" in n.tags else "]"
            lines.append(f"    \"{n.id}\"{shape_open}{_esc(n.label)}{shape_close}")
            if style != "plain":
                lines.append(f"    class \"{n.id}\" {group};")
                if "public" in n.tags:
                    lines.append(f"    class \"{n.id}\" public;")
                if "db" in n.tags:
                    lines.append(f"    class \"{n.id}\" db;")
        lines.append("  end")

    subgraph("Compose", "compose")
    subgraph("Kubernetes", "k8s")
    subgraph("External", "external")

    # Edges
    for e in g.edges:
        label = f" |{_esc(e.label)}|" if e.label else ""
        lines.append(f"  \"{e.src}\" -->{label} \"{e.dst}\"")

    # Legend
    if include_legend:
        lines.extend([
            "  %% Legend",
            "  subgraph Legend",
            "    legend_compose[Compose]:::compose",
            "    legend_k8s[Kubernetes]:::k8s",
            "    legend_ext[External]:::external",
            "    legend_pub[Public Exposure]:::public",
            "    legend_db(DB Service):::db",
            "  end"
        ])

    lines.append("```")
    return "\n".join(lines)

def _esc(s: str) -> str:
    return s.replace("\"","'")

def wrap_markdown(mermaid: str, summary: str) -> str:
    return f"""# System Architecture (auto-generated)

{mermaid}

<sub>{summary}</sub>

> Generated by **repo-architecture-map** (zero deps). Edit freely after generation.
"""
