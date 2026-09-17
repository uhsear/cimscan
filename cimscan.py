#!/usr/bin/env python
"""Report every data source in a tree of .aprx, .lyrx, .mapx and .pagx files with no ArcGIS licence.

Point it at a folder of ArcGIS Pro documents and it prints, for every layer and
standalone table, the workspace it reads from, the dataset it asks for, and
whether that path is on disk right now. A document it cannot parse is reported
UNSUPPORTED and counted, never folded into the clean pile. That second half
matters more than the first: a scanner that returns zero sources for a file it
could not open tells you the project is fine when it has no idea.

The obvious tool is arcpy. `arcpy.mp.ArcGISProject(path).listBrokenDataSources()`
does this properly, understands every layer type Pro understands, and is written
by the people who own the format. It also needs a licensed Pro seat and a
checked-out licence on the machine that runs it, which is never the file server
the projects actually live on. That is the gap. This reads the CIM bytes
directly, so it runs on the server, in a scheduled task, or in CI, on a bare
python3. It knows less than arcpy does. It runs where arcpy cannot.

    python cimscan.py --self-test
    python cimscan.py \\\\gis-fs\\projects
    python cimscan.py \\\\gis-fs\\projects --filter sde:sqlserver:gisdb01
    python cimscan.py \\\\gis-fs\\projects --json > sources.json
    python cimscan.py --diff before.lyrx after.lyrx

Exit codes: 0 every document parsed, 1 at least one UNSUPPORTED, 2 the scan path
could not be read, 64 usage error.
"""

from __future__ import print_function

import argparse
import json
import ntpath
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Document extensions walked when the target is a directory.
DOCUMENT_EXTENSIONS = (".aprx", ".lyrx", ".mapx", ".pagx")

# Entries inside a zipped document that are worth parsing as CIM.
CIM_PART_EXTENSIONS = (".xml", ".json")

# What a redacted secret is replaced with. This is what lands in the text
# report, in --json and in --diff.
REDACTION = "***REDACTED***"

# A connection-string key holding one of these substrings carries a secret. An
# .sde property set uses PASSWORD and ENCRYPTED_PASSWORD, so the match is on a
# substring rather than on a list of names Esri is free to add to. TOKEN,
# SECRET and the API key spellings are here because a portal or feature service
# connection stores its credential under one of those names, and a token in a
# ticket is as usable as a password until it expires.
SECRET_KEY_MARKERS = ("PASSWORD", "TOKEN", "SECRET", "APIKEY", "API_KEY",
                      "CREDENTIAL")

# The part name given to a document that holds its layer at the root instead of
# in a definitions array. Any name that no CIMPATH can spell would do; this one
# is readable when it reaches a report.
DOCUMENT_ROOT = "(document)"

# Depth cap on layer nesting. Parts already referenced are tracked and skipped,
# but an inline child object has no part name to track, so the cap stays.
MAX_NESTING_DEPTH = 64

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"

# Document status.
OK = "OK"
UNSUPPORTED = "UNSUPPORTED"

# Presence verdicts. UNKNOWN means not checked, which is not the same as absent.
PRESENT = "present"
MISSING = "MISSING"
UNKNOWN = "unknown"


class Source(object):
    """One data source, already redacted, with no file handle left open."""

    def __init__(self, document, container, layer, kind, dataset, dataset_type,
                 workspace, path, present):
        self.document = document
        self.container = container
        self.layer = layer
        self.kind = kind
        self.dataset = dataset
        self.dataset_type = dataset_type
        self.workspace = workspace          # redacted at construction
        self.path = path                    # resolved, or None when not a path
        self.present = present              # True, False, or None = not checked

    @property
    def state(self):
        if self.present is None:
            return UNKNOWN
        return PRESENT if self.present else MISSING

    def to_dict(self):
        return {
            "document": self.document,
            "container": self.container,
            "layer": self.layer,
            "connection": self.kind,
            "dataset": self.dataset,
            "datasetType": self.dataset_type,
            "workspace": self.workspace,
            "path": self.path,
            "state": self.state,
        }

    def __repr__(self):
        return "Source(%s/%s -> %s)" % (self.container, self.layer, self.dataset)


class Document(object):
    """The result for one file on disk, including why it could not be read."""

    def __init__(self, path, status, sources, notes):
        self.path = path
        self.status = status
        self.sources = sources
        self.notes = notes

    def to_dict(self):
        return {
            "document": self.path,
            "status": self.status,
            "notes": self.notes,
            "sources": [s.to_dict() for s in self.sources],
        }

    def __repr__(self):
        return "Document(%s, %s, %d source(s))" % (
            self.path, self.status, len(self.sources))


# ----------------------------------------------------------------- pure core
# Everything down to the "documents on disk" divider takes strings or bytes and
# returns data. No file is opened here, which is what lets the self-test cover
# the parser, the redaction and the grouping with nothing installed.

def _local(tag):
    """An element tag without the namespace, if a namespace survived the parse."""
    return tag.rsplit("}", 1)[-1]


def _from_xml(element):
    """Normalise a CIM XML element into the shape CIM JSON already has.

    CIM ships two serialisations of one object model. An .aprx holds XML parts,
    a .lyrx holds JSON, and the member names differ only in case:
    <DataConnection> in XML is "dataConnection" in JSON. Lower-casing every key
    here collapses that difference, so one extractor reads both instead of two
    that drift apart.
    """
    cim_type = (element.get(XSI_TYPE) or "").rsplit(":", 1)[-1]

    # ArrayOfString, ArrayOfCIMView and the rest are plain lists on the JSON
    # side. Tested before the leaf test so an empty array becomes [], not "".
    if cim_type.startswith("ArrayOf"):
        return [_from_xml(child) for child in element]

    if len(element) == 0:
        return element.text or ""

    node = {}
    for child in element:
        key = _local(child.tag).lower()
        value = _from_xml(child)
        if key in node:
            # Repeated sibling tags with no ArrayOf wrapper still mean a list.
            if not isinstance(node[key], list):
                node[key] = [node[key]]
            node[key].append(value)
        else:
            node[key] = value
    if cim_type:
        node["type"] = cim_type
    return node


def _lower_keys(obj):
    """Lower-case every key of decoded CIM JSON. Values are left alone."""
    if isinstance(obj, dict):
        return dict((k.lower(), _lower_keys(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_lower_keys(v) for v in obj]
    return obj


def parse_cim(data):
    """Parse one CIM part, XML or JSON, into the normalised dict shape.

    Dispatch is on the first byte, not on the file extension. A part inside an
    .aprx is XML, a .lyrx or .mapx on disk is JSON, and neither announces which.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    raw = raw.lstrip(b"\xef\xbb\xbf").lstrip()
    if not raw:
        raise ValueError("empty CIM part")
    if raw[:1] == b"{":
        return _lower_keys(json.loads(raw.decode("utf-8")))
    return _from_xml(ET.fromstring(raw))


def parse_properties(text):
    """Split an Esri connection string into its KEY=VALUE properties."""
    props = {}
    for chunk in (text or "").split(";"):
        key, sep, value = chunk.partition("=")
        if sep:
            props[key.strip().upper()] = value.strip()
    return props


def redact(text):
    """Replace every secret-bearing value in a connection string.

    An .sde connection saved with a stored credential carries PASSWORD=... or
    ENCRYPTED_PASSWORD=... in clear text inside the document. Scanning a shared
    drive and printing what you found is how that credential reaches a ticket, a
    chat log and a CI artefact. Redaction happens where the Source is built, so
    one call covers every output mode instead of each formatter remembering.

    Properties are split on ";" and again on "&", because a service workspace is
    a URL and a stored token sits in its query string. Splitting on ";" alone
    left "?token=..." inside a chunk whose key did not match, and printed the
    token. Both separators go back exactly as they were found, so a string with
    nothing to redact comes out unchanged.
    """
    if not text or "=" not in text:
        return text or ""

    def one(chunk):
        key, sep, _value = chunk.partition("=")
        if sep and any(m in key.strip().upper() for m in SECRET_KEY_MARKERS):
            return "%s=%s" % (key, REDACTION)
        return chunk

    return ";".join("&".join(one(p) for p in part.split("&"))
                    for part in text.split(";"))


def connection_facts(connection):
    """Pull (kind, workspace, dataset, dataset type) out of any CIM connection.

    Every lookup is a .get with a fallback. A CIMSqlQueryDataConnection has no
    Dataset member at all, because a query layer names a query and not a
    dataset. An extractor that indexes Dataset raises on the first query layer
    it meets and abandons the rest of the project.
    """
    if not isinstance(connection, dict):
        return ("unknown", "", "", "unknown")
    kind = connection.get("type") or "unknown"
    workspace = (connection.get("workspaceconnectionstring")
                 or connection.get("uri")
                 or connection.get("url")
                 or "")
    dataset = (connection.get("dataset")
               or connection.get("queryname")
               or connection.get("objectname")
               or "")
    dataset_type = (connection.get("datasettype")
                    or connection.get("objecttype")
                    or kind)
    return (kind,
            workspace if isinstance(workspace, str) else "",
            dataset if isinstance(dataset, str) else "",
            dataset_type if isinstance(dataset_type, str) else kind)


def workspace_path(workspace):
    """The filesystem path a workspace string points at, or None.

    A remote geodatabase carries a DATABASE property too, and there it is the
    name of a database on a server rather than a path. Treating it as a path
    reports every SDE layer in the county as MISSING, and the report becomes
    noise that nobody reads.
    """
    workspace = (workspace or "").strip()
    if not workspace:
        return None
    if "=" not in workspace:
        if workspace.lower().startswith(("http://", "https://")):
            return None
        return workspace
    props = parse_properties(workspace)
    if "SERVER" in props or "INSTANCE" in props:
        return None
    return props.get("DATABASE") or None


def is_windows_absolute(path):
    r"""True for a drive-letter or UNC path, whatever host is reading it.

    os.path.isabs answers for the HOST, not for the path. On Linux it calls
    "C:\gis\parcels.gdb" relative and joins it onto the project home, inventing
    a path that was never in the document. That matters here because scanning a
    shared drive full of Windows-authored projects FROM a Linux file server is
    the case this tool exists for.
    """
    if not path:
        return False
    if path[:2] in ("//", chr(92) * 2):
        return True
    return len(path) > 2 and path[1] == ":" and path[2] in ("/", chr(92))


def resolve(path, home):
    r"""Resolve a workspace path against the project home directory.

    Pro saves relative paths by default, so a workspace of
    "DATABASE=..\commondata\parcels.gdb" is normal and means nothing until you
    know which document it came out of.

    A Windows-absolute path is returned in Windows form on every host. It names
    a location on a Windows machine, and rewriting its separators to suit the
    reader would report a path that appears in no document.
    """
    if not path:
        return None
    # A drive prefix, with or without a separator after it. "C:parcels.gdb" is
    # drive-RELATIVE: it means parcels.gdb in whatever the current directory on
    # C: happens to be. Joining that onto the project home would assert a
    # location Windows itself does not promise, so it is left as written.
    if is_windows_absolute(path) or path[1:2] == ":":
        return ntpath.normpath(path)
    native = path.replace(chr(92), os.sep).replace("/", os.sep)
    if os.path.isabs(native):
        return os.path.normpath(native)
    return os.path.normpath(os.path.join(home, native))



def is_network_path(path):
    """True for a UNC path, whose existence check goes out over the wire."""
    if not path:
        return False
    return path.startswith("\\\\") or path.startswith("//")


def check_presence(path, check_network, exists=os.path.exists):
    """Presence for a resolved path: True, False, or None when not checked.

    A UNC stat against a server that is already switched off blocks for the full
    SMB timeout. Three hundred layers pointed at a decommissioned box turn a
    scan into an afternoon, so the network check is opt in.
    """
    if path is None:
        return None
    if is_network_path(path) and not check_network:
        return None
    return bool(exists(path))


def find_connection(node):
    """The first connection object inside a layer, not counting child layers.

    Descending into child layers here would credit a group layer with its first
    child's source, so the keys that hold child layers are skipped and the walk
    visits them separately.
    """
    if not isinstance(node, dict):
        return None
    for key in sorted(node.keys()):
        if key in ("layers", "standalonetables", "layerdefinitions"):
            continue
        value = node[key]
        if isinstance(value, dict):
            if "connection" in str(value.get("type", "")).lower():
                return value
            found = find_connection(value)
            if found is not None:
                return found
    return None


def _reference_name(text):
    """Strip the CIMPATH prefix a reference or a .lyrx uri may carry.

    A zip part is keyed by its entry name, "Map/a.xml", while a .lyrx keys the
    same layer by its uri, "CIMPATH=layer.xml". Both sides are stripped so one
    lookup serves both.
    """
    return text.rsplit("CIMPATH=", 1)[-1].lstrip("/")


def part_key(parts, reference):
    """Resolve a CIMPATH reference to a part name, ignoring case.

    Pro writes "Map/36ceb....xml" into one part of a project and "map/map.xml"
    into another, and a zip stores both spellings verbatim. A case-sensitive
    lookup finds neither half the time, drops those layers, and reports a
    project as holding fewer sources than it holds. That shortfall is worse than
    an error, because the report still looks like a report.
    """
    if not isinstance(reference, str) or not reference:
        return None
    if reference in parts:
        return reference
    name = _reference_name(reference)
    if name in parts:
        return name
    wanted = name.lower()
    matches = sorted(k for k in parts if _reference_name(k).lower() == wanted)
    return matches[0] if matches else None


def as_list(value):
    """CIM writes a one-element array as a bare value often enough to matter."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def document_parts(root):
    """Split one parsed JSON document into (parts, roots).

    A .lyrx keeps its layers in a flat layerDefinitions array and references
    them by uri, which is the same indirection the zip performs with CIMPATH.
    Both reduce to a name-to-node table plus a list of entry points, so one
    walker handles both.

    A .mapx names its map "mapDefinition" and a .pagx names its layout
    "layoutDefinition", both singular, and both keep their standalone tables in
    standaloneTableDefinitions. Indexing only the plural arrays left the map out
    of the part table, so every layer in a .mapx lost the map it belongs to and
    every standalone table it referenced was dropped from the report without a
    note. A source nobody printed is the failure this tool exists to prevent.
    """
    if not isinstance(root, dict):
        return ({}, [])
    parts = {}
    for key in ("layerdefinitions", "mapdefinitions",
                "standalonetabledefinitions"):
        for index, definition in enumerate(as_list(root.get(key))):
            if isinstance(definition, dict):
                name = definition.get("uri") or "%s#%d" % (key, index)
                parts[name] = definition
    for key in ("mapdefinition", "layoutdefinition"):
        definition = root.get(key)
        if isinstance(definition, dict):
            parts[definition.get("uri") or key] = definition
    if not parts:
        # The sentinel is named, not empty. part_key refuses an empty reference,
        # because a layer with no uri must not match a part, so an empty
        # sentinel resolved to nothing and a document holding its layer at the
        # root reported no sources at all.
        return ({DOCUMENT_ROOT: root}, [DOCUMENT_ROOT])
    roots = [r for r in as_list(root.get("layers")) if isinstance(r, str)]
    return (parts, roots or sorted(parts))


def walk_layers(parts, roots, visited=None):
    """Return (container, name, node) for every layer reachable from the roots.

    Recursion is what finds a layer buried three group layers deep. Both
    serialisations nest: XML references a child by CIMPATH, JSON references one
    by uri or embeds it inline, so a string is resolved and a dict is descended.

    The caller may hand in its own visited set to continue an earlier walk. A
    second walk that starts with an empty one reports every part the first walk
    already reached, which is how a layer held by a map and by an unreferenced
    group part was counted twice.
    """
    found = []
    if visited is None:
        visited = set()

    def visit(reference, container, depth):
        if depth > MAX_NESTING_DEPTH:
            return
        node = reference
        if isinstance(reference, str):
            key = part_key(parts, reference)
            if key is None or key in visited:
                return
            visited.add(key)
            node = parts[key]
        if not isinstance(node, dict):
            return
        cim_type = str(node.get("type", ""))
        name = node.get("name") or "(unnamed)"
        if not isinstance(name, str):
            name = "(unnamed)"
        if cim_type in ("CIMMap", "CIMLayout"):
            container = name
        else:
            found.append((container, name, node))
        for key in ("layers", "standalonetables"):
            for child in as_list(node.get(key)):
                visit(child, container, depth + 1)

    for root in roots:
        visit(root, "", 0)
    return found, visited


def collect_sources(document, parts, roots, home, check_network,
                    exists=os.path.exists):
    """Turn a parsed document into its Source records.

    Maps are walked first so every layer is attributed to the map that holds it,
    then any part left unvisited is swept up. An unreferenced layer part is
    still a live reference to a database, and a decommissioning plan that skips
    it is wrong.
    """
    sources = []
    map_roots = [n for n in sorted(parts)
                 if isinstance(parts[n], dict)
                 and str(parts[n].get("type", "")) in ("CIMMap", "CIMLayout")]
    layers, visited = walk_layers(parts, map_roots + list(roots))

    leftovers = [n for n in sorted(parts) if n not in visited]
    if leftovers:
        # The same visited set, or a layer that a map and an unreferenced group
        # part both reference comes back twice and inflates every count below.
        extra, _visited = walk_layers(parts, leftovers, visited)
        layers.extend(extra)

    for container, name, node in layers:
        connection = find_connection(node)
        if connection is None:
            continue
        kind, workspace, dataset, dataset_type = connection_facts(connection)
        path = resolve(workspace_path(workspace), home)
        sources.append(Source(
            document=document,
            container=container or "(no map)",
            layer=name,
            kind=kind,
            dataset=dataset,
            dataset_type=dataset_type,
            workspace=redact(workspace),
            path=path,
            present=check_presence(path, check_network, exists),
        ))
    return sources


def group_by_workspace(sources):
    """Collapse sources onto the workspaces they share, for the plan rows.

    Four layers on one .sde are one decommissioning decision, not four. The key
    is the redacted workspace string, so two connections differing only in a
    stored password still group together, which is what you want.

    The resolved path is part of the key as well. Two projects both saying
    "DATABASE=..\\commondata\\x.gdb" mean two different folders, and keying on
    the text alone merged them into one row carrying whichever verdict came
    first. A geodatabase that was gone then hid behind a row reading "present",
    which is the one answer this tool exists not to give.
    """
    groups = {}
    for source in sources:
        name = source.workspace or "(no workspace)"
        key = (name, source.path or "")
        entry = groups.get(key)
        if entry is None:
            entry = {"workspace": name, "path": source.path,
                     "state": source.state, "count": 0, "layers": []}
            groups[key] = entry
        entry["count"] += 1
        entry["layers"].append(source.layer)
    return [groups[k] for k in sorted(groups)]


def matches_filter(source, text):
    """Case-insensitive substring match over the fields worth grepping."""
    if not text:
        return True
    needle = text.lower()
    for field in (source.workspace, source.path, source.dataset,
                  source.layer, source.container, source.kind):
        if field and needle in str(field).lower():
            return True
    return False


def layer_signature(node):
    """The four things a .lyrx pull request is actually about."""
    kind, workspace, dataset, _type = connection_facts(find_connection(node))
    feature_table = node.get("featuretable")
    query = ""
    if isinstance(feature_table, dict):
        query = feature_table.get("definitionexpression") or ""
    renderer = node.get("renderer")
    field = ""
    if isinstance(renderer, dict):
        field = renderer.get("field") or ""
        if not field:
            field = ", ".join(str(f) for f in as_list(renderer.get("fields")))
    return {
        "source": "%s|%s" % (redact(workspace), dataset),
        "connection": kind,
        "definitionQuery": query,
        "rendererField": field,
    }


def diff_documents(before, after):
    """Semantic differences between two parsed layer documents.

    A .lyrx is a whole JSON document for one layer, and the real ones on this
    machine run to 986 lines each. Re-saving one in Pro rewrites its uri, so a
    textual diff reports a change even when nothing a reviewer cares about
    moved. These four fields are what the review is actually about.
    """
    def summaries(root):
        parts, roots = document_parts(root)
        layers, _visited = walk_layers(parts, roots)
        out = {}
        for _container, name, node in layers:
            out[name] = layer_signature(node)
        return out

    old, new = summaries(before), summaries(after)
    labels = [("source", "source repointed"),
              ("definitionQuery", "definition query changed"),
              ("rendererField", "renderer field changed"),
              ("connection", "connection type changed")]
    changes = []
    for name in sorted(set(old) | set(new)):
        if name not in new:
            changes.append(("layer removed", name, name, ""))
            continue
        if name not in old:
            changes.append(("layer added", name, "", name))
            continue
        for key, label in labels:
            if old[name][key] != new[name][key]:
                changes.append((label, name, old[name][key], new[name][key]))
    return changes


# ------------------------------------------------------------ documents on disk

def read_parts(path):
    """Return {part name: bytes} for one document. Raises on an unreadable one.

    A zipped document is recognised by its signature, not by its extension. An
    .aprx is a zip of CIM XML parts; a .lyrx, .mapx or .pagx is bare CIM JSON on
    disk and is not zipped at all. Assuming otherwise fails on half the tree.
    """
    with open(path, "rb") as handle:
        signature = handle.read(2)
    if signature == b"PK":
        with zipfile.ZipFile(path) as archive:
            return dict(
                (name, archive.read(name)) for name in archive.namelist()
                if name.lower().endswith(CIM_PART_EXTENSIONS))
    with open(path, "rb") as handle:
        return {os.path.basename(path): handle.read()}


def scan_document(path, check_network=False, exists=os.path.exists):
    """Scan one document. Never raises; an unreadable one becomes UNSUPPORTED."""
    home = os.path.dirname(os.path.abspath(path))
    try:
        raw_parts = read_parts(path)
    except Exception as exc:
        return Document(path, UNSUPPORTED, [], ["could not open: %s" % exc])

    notes = []
    parsed = {}
    for name in sorted(raw_parts):
        try:
            parsed[name] = parse_cim(raw_parts[name])
        except Exception as exc:
            notes.append("part %s did not parse: %s" % (name, exc))

    if not raw_parts:
        notes.append("no CIM parts found inside the document")

    if len(parsed) == 1 and not list(parsed)[0].lower().endswith(".xml"):
        # A single JSON document carries its own part table inside itself.
        root = list(parsed.values())[0]
        # Valid JSON is not the same thing as a CIM document. A settings file
        # renamed .lyrx parses, holds no layers, and used to be reported OK with
        # no sources, which reads exactly like a project with nothing broken.
        if not isinstance(root, dict) or not str(
                root.get("type", "")).startswith("CIM"):
            notes.append("the root object is not a CIM document")
        parts, roots = document_parts(root)
    else:
        parts, roots = parsed, sorted(parsed)

    sources = collect_sources(path, parts, roots, home, check_network, exists)

    # A document with an unparsed part is UNSUPPORTED even when some layers were
    # read, because the sources reported are a floor and not the whole truth.
    return Document(path, UNSUPPORTED if notes else OK, sources, notes)


def find_documents(root, unreadable=None):
    """Every ArcGIS Pro document under root, or root itself when it is a file.

    A folder the walk cannot list is appended to unreadable as the OSError the
    operating system raised for it. Without onerror= os.walk swallows that
    error, so a denied folder and an empty one look identical: fewer documents,
    no message, clean exit. That is the same lie as reporting zero sources for a
    document nobody could open, and the docstring at the top of this file says
    this tool refuses to tell it.
    """
    if os.path.isfile(root):
        return [root]
    if unreadable is None:
        unreadable = []
    found = []
    for folder, _dirs, files in os.walk(root, onerror=unreadable.append):
        for name in sorted(files):
            if name.lower().endswith(DOCUMENT_EXTENSIONS):
                found.append(os.path.join(folder, name))
    return sorted(found)


# ------------------------------------------------------------------- reporting

def report_lines(documents, text_filter=None, unreadable=()):
    """Render the scan as the lines the CLI prints."""
    lines = []
    total, missing, unsupported = 0, 0, 0
    everything = []

    for document in documents:
        shown = [s for s in document.sources if matches_filter(s, text_filter)]
        everything.extend(shown)
        if document.status == UNSUPPORTED:
            unsupported += 1
        elif not shown and text_filter:
            # A filtered-out document stays quiet. An UNSUPPORTED one never
            # does: you cannot grep a document that nobody could read.
            continue
        lines.append("== %s  [%s]" % (document.path, document.status))
        for note in document.notes:
            lines.append("   ! %s" % note)
        for source in shown:
            total += 1
            if source.state == MISSING:
                missing += 1
            lines.append("   %-8s %-34s %-24s %s" % (
                source.state,
                ("%s > %s" % (source.container, source.layer))[:34],
                (source.dataset or "(no dataset)")[:24],
                source.workspace or "(no workspace)"))
        if not shown:
            lines.append("   (no data sources read)")
        lines.append("")

    groups = group_by_workspace(everything)
    lines.append("workspaces: %d source(s) across %d workspace(s)"
                 % (len(everything), len(groups)))
    for group in groups:
        # A relative workspace reads the same in every project, so the resolved
        # path is what tells two rows apart now that they no longer merge.
        label = group["workspace"]
        if group["path"] and group["path"] != label:
            label = "%s  -> %s" % (label, group["path"])
        lines.append("   %4d  %-8s %s"
                     % (group["count"], group["state"], label))
    lines.append("")
    for error in unreadable:
        # Named, not only counted: what the scan does not cover is the part of
        # the tree you have to go and look at yourself.
        lines.append("!! cannot list %s: %s" % (error.filename, error.strerror))
    lines.append("%d document(s), %d OK, %d UNSUPPORTED, %d source(s), "
                 "%d missing, %d unreadable directory(s)"
                 % (len(documents), len(documents) - unsupported, unsupported,
                    total, missing, len(unreadable)))
    return lines


def json_report(documents, text_filter=None, root=None, unreadable=()):
    """The --json payload, filtered the way the text report is.

    --filter reached the workspace summary but not the document list, so the
    two output modes disagreed about what the filter meant. Shape and filtering
    both live here now, which is also what lets the self-test cover them.
    """
    shown = [s for d in documents for s in d.sources
             if matches_filter(s, text_filter)]
    payload = []
    for document in documents:
        entry = document.to_dict()
        entry["sources"] = [s.to_dict() for s in document.sources
                            if matches_filter(s, text_filter)]
        payload.append(entry)
    return {"root": root,
            "documents": payload,
            "workspaces": group_by_workspace(shown),
            "unreadable_directories": [e.filename for e in unreadable]}


def diff_lines(changes):
    """Render a semantic diff as the lines the CLI prints."""
    if not changes:
        return ["no semantic difference: source, definition query, renderer "
                "field and connection type all match"]
    lines = []
    for kind, layer, before, after in changes:
        lines.append("%s: %s" % (kind, layer))
        if before:
            lines.append("   before: %s" % before)
        if after:
            lines.append("   after:  %s" % after)
    lines.append("")
    lines.append("%d change(s)" % len(changes))
    return lines


# ------------------------------------------------------------------ self-test
# The fixtures below are written the way Pro writes them, including the case
# collision and the query layer with no Dataset. They build in a temp directory
# with zipfile, so the whole self-test runs on a bare python3.

FIXTURE_NS = ('xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
              'xmlns:typens="http://www.esri.com/schemas/ArcGIS/2.9.0"')

FIXTURE_SDE = ("SERVER=gisdb01;INSTANCE=sde:sqlserver:gisdb01;DATABASE=sdeprod;"
               "USER=viewer;PASSWORD=hunter2;AUTHENTICATION_MODE=DBMS")

FIXTURE_GDB = "DATABASE=..\\commondata\\parcels.gdb"

# The property set Pro writes into a document for a saved .sde connection that
# stores its credential: the whole set, in the order Pro emits it, with both
# password blobs. The short FIXTURE_SDE above is the readable one; this is the
# shape the redaction actually meets on a shared drive.
FIXTURE_SDE_REAL = (
    "ENCRYPTED_PASSWORD=00022e6844ca3f1ab7d59c0e18a5;"
    "ENCRYPTED_PASSWORD_UTF8=00022e68e1f40c7b25d3;"
    "SERVER=gisdb01;INSTANCE=sde:sqlserver:gisdb01\\SQLEXPRESS;"
    "DBCLIENT=sqlserver;DB_CONNECTION_PROPERTIES=gisdb01\\SQLEXPRESS;"
    "DATABASE=sdeprod;USER=gis_viewer;AUTHENTICATION_MODE=DBMS;"
    "BRANCH=sde.DEFAULT;VERSION=sde.DEFAULT")


def _fixture_layer_xml(name, dataset, workspace):
    return (
        '<CIMFeatureLayer xsi:type="typens:CIMFeatureLayer" %s>'
        '<Name>%s</Name>'
        '<FeatureTable xsi:type="typens:CIMFeatureTable">'
        '<DataConnection xsi:type="typens:CIMStandardDataConnection">'
        '<WorkspaceConnectionString>%s</WorkspaceConnectionString>'
        '<WorkspaceFactory>FileGDB</WorkspaceFactory>'
        '<Dataset>%s</Dataset>'
        '<DatasetType>esriDTFeatureClass</DatasetType>'
        '</DataConnection></FeatureTable></CIMFeatureLayer>'
        % (FIXTURE_NS, name, workspace, dataset))


def _fixture_group_xml(name, children):
    strings = "".join("<String>CIMPATH=%s</String>" % c for c in children)
    return (
        '<CIMGroupLayer xsi:type="typens:CIMGroupLayer" %s>'
        '<Name>%s</Name>'
        '<Layers xsi:type="typens:ArrayOfString">%s</Layers>'
        '</CIMGroupLayer>' % (FIXTURE_NS, name, strings))


def _build_fixture_aprx(target):
    """A real zip, built with zipfile, shaped like an .aprx Pro actually writes."""
    archive = zipfile.ZipFile(target, "w")
    try:
        # The case collision Pro really produces: map/ beside Map/.
        archive.writestr(
            "map/map.xml",
            '<CIMMap xsi:type="typens:CIMMap" %s><Name>Planning</Name>'
            '<Layers xsi:type="typens:ArrayOfString">'
            '<String>CIMPATH=Map/group1.xml</String>'
            '<String>CIMPATH=Map/query.xml</String>'
            '</Layers>'
            '<StandaloneTables xsi:type="typens:ArrayOfString">'
            '<String>CIMPATH=Map/table.xml</String>'
            '</StandaloneTables></CIMMap>' % FIXTURE_NS)
        archive.writestr("Map/group1.xml",
                         _fixture_group_xml("Level1", ["Map/group2.xml"]))
        archive.writestr("Map/group2.xml",
                         _fixture_group_xml("Level2", ["Map/group3.xml"]))
        archive.writestr("Map/group3.xml", _fixture_group_xml(
            "Level3", ["Map/deep.xml", "Map/sde_a.xml", "Map/sde_b.xml",
                       "Map/sde_c.xml", "Map/sde_d.xml"]))
        archive.writestr("Map/deep.xml", _fixture_layer_xml(
            "Buried Parcels", "Parcels", FIXTURE_GDB))
        for suffix in "abcd":
            archive.writestr("Map/sde_%s.xml" % suffix, _fixture_layer_xml(
                "Zoning %s" % suffix.upper(), "GIS.Zoning_%s" % suffix,
                FIXTURE_SDE))
        archive.writestr(
            "Map/query.xml",
            '<CIMFeatureLayer xsi:type="typens:CIMFeatureLayer" %s>'
            '<Name>Permits Query</Name>'
            '<FeatureTable xsi:type="typens:CIMFeatureTable">'
            '<DataConnection xsi:type="typens:CIMSqlQueryDataConnection">'
            '<WorkspaceConnectionString>%s</WorkspaceConnectionString>'
            '<QueryName>PermitsThisYear</QueryName>'
            '<SqlQuery>select * from permits</SqlQuery>'
            '</DataConnection></FeatureTable></CIMFeatureLayer>'
            % (FIXTURE_NS, FIXTURE_SDE))
        archive.writestr(
            "Map/table.xml",
            '<CIMStandaloneTable xsi:type="typens:CIMStandaloneTable" %s>'
            '<Name>Owner Lookup</Name>'
            '<DataConnection xsi:type="typens:CIMStandardDataConnection">'
            '<WorkspaceConnectionString>%s</WorkspaceConnectionString>'
            '<Dataset>Owners</Dataset>'
            '<DatasetType>esriDTTable</DatasetType>'
            '</DataConnection></CIMStandaloneTable>'
            % (FIXTURE_NS, FIXTURE_GDB))
    finally:
        archive.close()
    return target


def _deny_read(path):
    """Make path unlistable for this user and return the undo. Self-test only.

    The walk hardening cannot be asserted against a mock, because os.walk calls
    onerror= only when the operating system actually refuses. Windows needs an
    ACL, since a chmod there sets the read-only flag and the folder still lists;
    POSIX needs the chmod, since it has no icacls. Neither needs admin rights.

    The denied right is (RD), list-directory, and not the (RX) an ACL example
    usually reaches for. (RX) also denies READ_CONTROL, and the owner cannot
    then read the folder's own ACL back: icacls /remove:d returns 5, the undo
    below never runs, and the temporary folder outlives the self-test.
    """
    import getpass
    import subprocess

    if os.name == "nt":
        user = getpass.getuser()

        def icacls(*flags):
            subprocess.check_output(["icacls", path] + list(flags),
                                    stderr=subprocess.STDOUT)

        icacls("/deny", "%s:(RD)" % user)
        return lambda: icacls("/remove:d", user)
    os.chmod(path, 0o000)
    return lambda: os.chmod(path, 0o700)


def _fixture_lyrx(dataset, query, field, name="ACLED"):
    """Bare CIM JSON on disk, the shape a real .lyrx has. Not zipped."""
    return json.dumps({
        "type": "CIMLayerDocument",
        "version": "2.9.0",
        "layers": ["CIMPATH=layer.xml"],
        "layerDefinitions": [{
            "type": "CIMFeatureLayer",
            "name": name,
            "uri": "CIMPATH=layer.xml",
            "featureTable": {
                "type": "CIMFeatureTable",
                "definitionExpression": query,
                "dataConnection": {
                    "type": "CIMStandardDataConnection",
                    "workspaceConnectionString": "DATABASE=..\\conflict.gdb",
                    "workspaceFactory": "FileGDB",
                    "dataset": dataset,
                    "datasetType": "esriDTFeatureClass",
                },
            },
            "renderer": {"type": "CIMClassBreaksRenderer", "field": field},
        }],
    })


def _fixture_mapx():
    """A .mapx: CIM JSON whose map is one object, not an array.

    Written the way the CIM serialiser writes it, including the "uRI" spelling a
    real .lyrx on this machine uses, and a standalone table the map holds by
    reference. Both are what a .mapx has and a .lyrx does not.
    """
    return json.dumps({
        "type": "CIMMapDocument",
        "version": "3.5.0",
        "mapDefinition": {
            "type": "CIMMap",
            "name": "Zoning Review",
            "uRI": "CIMPATH=map/map.xml",
            "layers": ["CIMPATH=map/parcels.json"],
            "standaloneTables": ["CIMPATH=map/owners.json"],
        },
        "layerDefinitions": [{
            "type": "CIMFeatureLayer",
            "name": "Parcels",
            "uRI": "CIMPATH=map/parcels.json",
            "featureTable": {
                "type": "CIMFeatureTable",
                "dataConnection": {
                    "type": "CIMStandardDataConnection",
                    "workspaceConnectionString": FIXTURE_GDB,
                    "workspaceFactory": "FileGDB",
                    "dataset": "Parcels",
                    "datasetType": "esriDTFeatureClass",
                },
            },
        }],
        "standaloneTableDefinitions": [{
            "type": "CIMStandaloneTable",
            "name": "Owner Lookup",
            "uRI": "CIMPATH=map/owners.json",
            "dataConnection": {
                "type": "CIMStandardDataConnection",
                "workspaceConnectionString": FIXTURE_SDE_REAL,
                "dataset": "GIS.Owners",
                "datasetType": "esriDTTable",
            },
        }],
        "binaryReferences": [],
    })


def _fixture_pagx():
    """A .pagx: a layout document, whose maps are an array beside the layout."""
    return json.dumps({
        "type": "CIMLayoutDocument",
        "version": "3.5.0",
        "layoutDefinition": {
            "type": "CIMLayout",
            "name": "Zoning Board",
            "uRI": "CIMPATH=layout/layout.xml",
            "elements": [{
                "type": "CIMMapFrame",
                "name": "Map Frame",
                "view": {"type": "CIMMapView",
                         "map": {"uRI": "CIMPATH=map/map.xml"}},
            }],
        },
        "mapDefinitions": [{
            "type": "CIMMap",
            "name": "Inset",
            "uRI": "CIMPATH=map/map.xml",
            "layers": ["CIMPATH=map/roads.json"],
        }],
        "layerDefinitions": [{
            "type": "CIMFeatureLayer",
            "name": "Roads",
            "uRI": "CIMPATH=map/roads.json",
            "featureTable": {
                "type": "CIMFeatureTable",
                "dataConnection": {
                    "type": "CIMStandardDataConnection",
                    "workspaceConnectionString": FIXTURE_SDE_REAL,
                    "dataset": "GIS.Roads",
                    "datasetType": "esriDTFeatureClass",
                },
            },
        }],
        "binaryReferences": [],
    })


def self_test():
    """Assertions over the parser and the decision core. No arcpy, no network."""
    import io
    import shutil
    import tempfile

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def run(argv):
        """main() with both streams captured, returning (exit code, output)."""
        buffer = io.StringIO()
        saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = buffer, buffer
        try:
            code = main(argv)
        finally:
            sys.stdout, sys.stderr = saved
        return code, buffer.getvalue()

    print("cimscan self-test: no arcpy, no licence, no network")
    print("-" * 68)

    workdir = tempfile.mkdtemp(prefix="cimscan-test-")
    try:
        home = os.path.join(workdir, "p20")
        os.makedirs(home)
        aprx = _build_fixture_aprx(os.path.join(home, "Planning.aprx"))

        # ---- secret redaction, the security rule
        check("hunter2" not in redact(FIXTURE_SDE),
              "a stored password never survives redaction")
        check(REDACTION in redact(FIXTURE_SDE),
              "the redaction marker takes its place")
        check("SERVER=gisdb01" in redact(FIXTURE_SDE),
              "the server name is kept, it is the whole point of the scan")
        check("USER=viewer" in redact(FIXTURE_SDE), "the user name is kept")
        check(REDACTION in redact("ENCRYPTED_PASSWORD=abc123"),
              "ENCRYPTED_PASSWORD is redacted too, the match is on a substring")
        check(redact("DATABASE=x.gdb") == "DATABASE=x.gdb",
              "a connection string with no secret passes through unchanged")
        check(redact("") == "", "an empty workspace redacts to empty")
        check(redact("C:\\data\\parcels.gdb") == "C:\\data\\parcels.gdb",
              "a bare path holds no properties and is left alone")
        portal = redact("URL=https://portal/sharing;TOKEN=abc123;USER=viewer")
        check("abc123" not in portal,
              "a stored portal TOKEN never survives redaction  <-- pinned defect")
        check("USER=viewer" in portal, "and the rest of that connection is kept")
        service = redact("https://host/rest/services/x/FeatureServer"
                         "?token=SECRET123&f=json")
        check("SECRET123" not in service,
              "a token in a service URL query string is redacted too  "
              "<-- pinned defect")
        check(service.endswith("&f=json"),
              "the other query parameters survive, only the value goes")
        check(REDACTION in redact("DATABASE=x;CLIENT_SECRET=shhh"),
              "CLIENT_SECRET is redacted, OAuth stores a credential there")
        check(redact("SERVER=a;INSTANCE=b") == "SERVER=a;INSTANCE=b",
              "a connection with no secret is returned byte for byte")

        # ---- a whole .sde property set, the shape Pro really saves
        real = redact(FIXTURE_SDE_REAL)
        check("00022e68" not in real,
              "neither stored password blob survives a real .sde connection")
        check(real.count(REDACTION) == 2,
              "ENCRYPTED_PASSWORD and ENCRYPTED_PASSWORD_UTF8 both go")
        check("INSTANCE=sde:sqlserver:gisdb01\\SQLEXPRESS" in real,
              "the instance, which is what a decommissioning plan needs, stays")
        check("USER=gis_viewer" in real and "VERSION=sde.DEFAULT" in real,
              "the user and the version survive redaction")
        real_props = parse_properties(FIXTURE_SDE_REAL)
        check(real_props["INSTANCE"] == "sde:sqlserver:gisdb01\\SQLEXPRESS",
              "a named instance keeps the backslash and the instance name")
        check(real_props["AUTHENTICATION_MODE"] == "DBMS",
              "the authentication mode is read from the same property set")
        check(len(real_props) == 11,
              "every property of the real connection string is parsed")
        check(workspace_path(FIXTURE_SDE_REAL) is None,
              "a real .sde DATABASE is a database on a server, not a folder")

        # ---- connection string properties
        props = parse_properties(FIXTURE_SDE)
        check(props["SERVER"] == "gisdb01", "the server property is read")
        check(props["DATABASE"] == "sdeprod", "the database property is read")
        check(parse_properties("") == {}, "an empty string yields no properties")

        # ---- which workspaces are paths at all
        check(workspace_path(FIXTURE_GDB) == "..\\commondata\\parcels.gdb",
              "a file geodatabase workspace is a path")
        check(workspace_path(FIXTURE_SDE) is None,
              "a remote DATABASE is a database name, not a path")
        check(workspace_path("INSTANCE=sde:oracle11g:gisdb;DATABASE=sdeprod;"
                             "USER=viewer") is None,
              "a direct connect names an INSTANCE and no SERVER, and its "
              "DATABASE is still not a folder")
        check(workspace_path("https://services.arcgis.com/x/FeatureServer/0")
              is None, "a service url is not a path")
        check(workspace_path("C:\\gis\\parcels.gdb") == "C:\\gis\\parcels.gdb",
              "a bare path workspace is the path")
        check(workspace_path("") is None, "an empty workspace has no path")
        check(workspace_path("   ") is None,
              "a whitespace-only workspace is not a path either")

        # ---- resolving against the project home, and presence
        relative = "..%scommondata%sx.gdb" % (os.sep, os.sep)
        check(resolve(relative, home) == os.path.normpath(
            os.path.join(workdir, "commondata", "x.gdb")),
              "a relative path resolves against the project home")
        check(resolve(None, home) is None, "no path resolves to nothing")
        check(check_presence(None, False) is None,
              "a connection with no path is unknown, not missing")
        check(check_presence(os.path.join(workdir, "nope.gdb"), False) is False,
              "a resolved path that is not there is missing")
        check(check_presence(home, False) is True,
              "a path that is there is present")
        check(check_presence("\\\\deadserver\\gis\\x.gdb", False) is None,
              "a UNC path is left unchecked by default, a dead server hangs the stat")
        check(check_presence("\\\\deadserver\\gis\\x.gdb", True,
                             exists=lambda p: False) is False,
              "--check-network turns the UNC stat on")
        check(resolve("C:\\gis\\x.gdb", home) == "C:\\gis\\x.gdb",
              "an absolute workspace path ignores the project home")
        check(resolve("C:\\gis\\..\\gis\\x.gdb", home) == "C:\\gis\\x.gdb",
              "a windows absolute path normalises in windows form on any host"
              "  <-- pinned defect")
        check(resolve("C:/gis/x.gdb", home) == "C:\\gis\\x.gdb",
              "a forward-slash drive path is still windows absolute")
        check(is_windows_absolute("C:\\gis") is True,
              "a drive letter is windows absolute")
        check(is_windows_absolute("\\\\srv\\share") is True,
              "a UNC path is windows absolute")
        check(is_windows_absolute("..\\commondata\\x.gdb") is False,
              "a relative path is not windows absolute")
        check(is_windows_absolute("C:") is False,
              "a bare drive letter with no separator is not a path")
        check(is_windows_absolute("") is False,
              "an empty path is not windows absolute")
        check(is_network_path("\\\\gis-fs\\projects\\x.gdb") is True,
              "a UNC path is a network path")
        check(is_network_path("C:\\gis\\x.gdb") is False,
              "a local drive path is not")
        check(is_network_path("") is False,
              "and an empty path is not one either")
        check(is_network_path("//gis-fs/projects/x.gdb") is True,
              "a UNC path written with forward slashes is one too")
        check(resolve("C:parcels.gdb", home) == "C:parcels.gdb",
              "a drive-relative workspace is not joined onto the project home")
        check(resolve("C:parcels.gdb", "/srv/proj") == "C:parcels.gdb",
              "and it is left alone on a posix host too  <-- pinned defect")

        # ---- THE PINNED DEFECT: two serialisations of one object model
        xml_node = parse_cim(_fixture_layer_xml("L", "Parcels", "DATABASE=x.gdb"))
        json_node = parse_cim(_fixture_lyrx("ACLED_2005_2018", "", "FATALITIES"))
        check(xml_node["type"] == "CIMFeatureLayer",
              "an .aprx part is CIM XML and parses  <-- pinned defect")
        check(json_node["type"] == "CIMLayerDocument",
              "a .lyrx is bare CIM JSON and parses  <-- pinned defect")
        check(find_connection(xml_node)["dataset"] == "Parcels",
              "the XML spelling DataConnection is found")
        check(find_connection(json_node["layerdefinitions"][0])["dataset"]
              == "ACLED_2005_2018",
              "the JSON spelling dataConnection is found by the same code")
        check(parse_cim('<A xsi:type="typens:ArrayOfString" '
                        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>')
              == [], "an empty ArrayOfString becomes a list, not an empty string")
        raises(lambda: parse_cim(b""),
               "an empty part raises rather than parsing to nothing")
        repeated = parse_cim(
            '<CustomProperties %s><Key>a</Key><Key>b</Key><Key>c</Key>'
            '</CustomProperties>' % FIXTURE_NS)
        check(repeated["key"] == ["a", "b", "c"],
              "repeated sibling tags with no ArrayOf wrapper read as one list")
        check("type" not in repeated,
              "an element with no xsi:type carries no type member")
        check(find_connection("CIMPATH=map/map.xml") is None,
              "a reference string holds no connection")
        stepped = {"extent": {"type": "CIMExtent", "xmin": "0"},
                   "featuretable": {"type": "CIMFeatureTable",
                                    "dataconnection": {
                                        "type": "CIMStandardDataConnection",
                                        "dataset": "Roads"}}}
        check(find_connection(stepped)["dataset"] == "Roads",
              "a member with no connection under it is stepped over, not "
              "mistaken for one")
        # The child is a bare object, not an array of one. CIM writes a
        # one-element array that way often enough that as_list exists for it,
        # and it is the shape that reaches find_connection as a dict it would
        # happily descend into if the key were not skipped.
        inline_group = parse_cim(json.dumps({
            "type": "CIMGroupLayer", "name": "Utilities",
            "layers": {"type": "CIMFeatureLayer", "name": "Mains",
                       "featureTable": {"dataConnection": {
                           "type": "CIMStandardDataConnection",
                           "workspaceConnectionString": FIXTURE_GDB,
                           "dataset": "Mains"}}}}))
        check([s.layer for s in collect_sources(
            "Group.lyrx", {DOCUMENT_ROOT: inline_group}, [DOCUMENT_ROOT],
            home, False)] == ["Mains"],
              "a group layer is not credited with the source of the child "
              "layer written inline underneath it")
        check(part_key({"a.xml": 1}, None) is None,
              "a layer with no uri at all resolves to no part")
        check(as_list("CIMPATH=x") == ["CIMPATH=x"],
              "a one-element array written as a bare value reads as a list")
        check(as_list(None) == [], "and a member that is absent reads as empty")

        # ---- THE PINNED DEFECT: zip entries differing only by case
        parts = {"Map/a.xml": 1, "map/map.xml": 2}
        check(part_key(parts, "CIMPATH=Map/a.xml") == "Map/a.xml",
              "an exact CIMPATH match wins")
        check(part_key(parts, "CIMPATH=MAP/A.XML") == "Map/a.xml",
              "a case-colliding CIMPATH still resolves  <-- pinned defect")
        check(part_key(parts, "0|0|CIMPATH=map/map.xml") == "map/map.xml",
              "a view-prefixed CIMPATH resolves")
        check(part_key(parts, "CIMPATH=Map/gone.xml") is None,
              "a CIMPATH with no part behind it resolves to nothing")

        # ---- THE PINNED DEFECT: a query layer carries no Dataset
        query_connection = {"type": "CIMSqlQueryDataConnection",
                            "workspaceconnectionstring": FIXTURE_SDE,
                            "queryname": "PermitsThisYear"}
        kind, _workspace, dataset, _type = connection_facts(query_connection)
        check(kind == "CIMSqlQueryDataConnection",
              "a query connection with no Dataset does not raise  <-- pinned defect")
        check(dataset == "PermitsThisYear",
              "the query name stands in for the missing dataset")
        check(connection_facts({})[0] == "unknown",
              "an empty connection reads as unknown")
        check(connection_facts(None)[0] == "unknown",
              "a null connection reads as unknown")

        # ---- the real zip: nesting, standalone tables, attribution
        document = scan_document(aprx)
        by_layer = dict((s.layer, s) for s in document.sources)
        check(document.status == OK, "a well formed .aprx scans OK")
        check("Buried Parcels" in by_layer,
              "a layer three group layers deep is still found")
        check(by_layer["Buried Parcels"].container == "Planning",
              "the buried layer is attributed to the map that holds it")
        check("Owner Lookup" in by_layer, "a CIMStandaloneTable is found")
        check(by_layer["Owner Lookup"].dataset == "Owners",
              "the standalone table reports its dataset")
        check("Permits Query" in by_layer,
              "the query layer survives the scan of a whole project")
        check(len(document.sources) == 7,
              "all seven sources are reported, none lost to the case collision")
        check(repr(document) == "Document(%s, OK, 7 source(s))" % aprx,
              "a document prints its path, its status and its source count")
        check(repr(by_layer["Owner Lookup"])
              == "Source(Planning/Owner Lookup -> Owners)",
              "a source prints the map, the layer and the dataset")

        # ---- the relative database path that is not there
        buried = by_layer["Buried Parcels"]
        check(buried.path == os.path.normpath(
            os.path.join(workdir, "commondata", "parcels.gdb")),
              "the relative database path resolved against the project home")
        check(buried.present is False, "and the resolved path is reported missing")
        check(buried.state == MISSING, "its state reads MISSING")

        # ---- redaction reaches every output mode
        check("hunter2" not in json.dumps(document.to_dict()),
              "no password reaches the --json output")
        check("hunter2" not in "\n".join(report_lines([document])),
              "no password reaches the text report")
        check("hunter2" not in json.dumps(group_by_workspace(document.sources)),
              "no password reaches the workspace grouping")

        # ---- workspace grouping, the plan rows
        groups = group_by_workspace(document.sources)
        check(len(groups) == 2, "seven sources collapse onto two workspaces")
        sde_group = [g for g in groups if "gisdb01" in g["workspace"]][0]
        check(sde_group["count"] == 5,
              "four zoning layers and one query layer share a single .sde row")
        check(sde_group["state"] == UNKNOWN,
              "a remote workspace carries no presence verdict")
        check(len([g for g in groups if g["state"] == MISSING]) == 1,
              "the missing file geodatabase is one plan row, not two")

        # ---- a layer reachable twice is still one source
        shared = json.dumps({
            "type": "CIMLayerDocument",
            "layers": ["CIMPATH=g1"],
            "layerDefinitions": [
                {"type": "CIMGroupLayer", "name": "G1", "uri": "CIMPATH=g1",
                 "layers": ["CIMPATH=shared"]},
                {"type": "CIMFeatureLayer", "name": "Shared",
                 "uri": "CIMPATH=shared",
                 "featureTable": {"dataConnection": {
                     "type": "CIMStandardDataConnection",
                     "workspaceConnectionString": "DATABASE=x.gdb",
                     "dataset": "Shared"}}},
                {"type": "CIMGroupLayer", "name": "Orphan", "uri": "CIMPATH=g2",
                 "layers": ["CIMPATH=shared"]}]})
        shared_parts, shared_roots = document_parts(parse_cim(shared))
        twice = collect_sources("shared.lyrx", shared_parts, shared_roots, home,
                                False)
        check([s.layer for s in twice] == ["Shared"],
              "a layer a map and an unreferenced group both hold is reported "
              "once  <-- pinned defect")

        # ---- the shapes a CIM document can take that a .lyrx does not
        check(document_parts([1, 2]) == ({}, []),
              "a json document that is not an object holds no parts")
        bare_root = parse_cim(json.dumps({
            "type": "CIMFeatureLayer", "name": "Solo",
            "featureTable": {"dataConnection": {
                "type": "CIMStandardDataConnection",
                "workspaceConnectionString": "DATABASE=solo.gdb",
                "dataset": "Solo"}}}))
        bare_parts, bare_roots = document_parts(bare_root)
        check(bare_roots == [DOCUMENT_ROOT],
              "a document with no definitions array is its own single part")
        check([s.dataset for s in collect_sources(
            "Bare.lyrx", bare_parts, bare_roots, home, False)] == ["Solo"],
              "the layer such a document holds at its root is still reported  "
              "<-- pinned defect")
        mixed, _mixed_roots = document_parts(parse_cim(json.dumps({
            "layerDefinitions": ["CIMPATH=dangling",
                                 {"type": "CIMFeatureLayer", "name": "Real"}]})))
        check(len(mixed) == 1,
              "a definitions entry that is a reference and not an object is "
              "skipped")

        # ---- the nesting cap, and children that are not layers
        capped = {"type": "CIMGroupLayer", "name": "G0"}
        node = capped
        for depth in range(MAX_NESTING_DEPTH + 4):
            child = {"type": "CIMGroupLayer", "name": "G%d" % (depth + 1)}
            node["layers"] = [child]
            node = child
        nested, _seen = walk_layers({DOCUMENT_ROOT: capped}, [DOCUMENT_ROOT])
        check(len(nested) == MAX_NESTING_DEPTH + 1,
              "inline nesting deeper than the cap is not followed")
        # The cap itself is a tuning knob, so the assertion that matters is the
        # one below it: a depth a person could really build must never be cut
        # short. Sixty is a literal on purpose. Reading it off the constant
        # would make the assertion move whenever the constant moved.
        shallow = {"type": "CIMGroupLayer", "name": "S0"}
        node = shallow
        for depth in range(59):
            child = {"type": "CIMFeatureLayer", "name": "S%d" % (depth + 1)}
            node["layers"] = [child]
            node = child
        deep_enough, _deep_seen = walk_layers({DOCUMENT_ROOT: shallow},
                                              [DOCUMENT_ROOT])
        check(len(deep_enough) == 60,
              "a sixty-deep chain is walked to its end, the cap never "
              "truncates a project a person could build")
        odd = parse_cim(json.dumps({
            "type": "CIMMap", "name": "M", "layers": [
                42,
                {"type": "CIMFeatureLayer", "name": {"value": "not text"},
                 "featureTable": {"dataConnection": {
                     "type": "CIMStandardDataConnection", "dataset": "D"}}}]}))
        odd_layers, _seen = walk_layers({DOCUMENT_ROOT: odd}, [DOCUMENT_ROOT])
        check(len(odd_layers) == 1,
              "a child layer that is not an object at all is skipped")
        check(odd_layers[0][1] == "(unnamed)",
              "a layer name that is not text reads as unnamed, it does not raise")

        # ---- the four fields a .lyrx review is about
        multi = parse_cim(json.dumps({
            "type": "CIMFeatureLayer", "name": "Zoning",
            "renderer": {"type": "CIMUniqueValueRenderer",
                         "fields": ["ZONE", "CLASS"]}}))
        signature = layer_signature(multi)
        check(signature["rendererField"] == "ZONE, CLASS",
              "a unique value renderer reports every field it breaks on")
        check(signature["definitionQuery"] == "",
              "a layer with no feature table has no definition query")
        check(layer_signature({})["connection"] == "unknown",
              "a layer with no connection signs as unknown rather than raising")

        # ---- one workspace string, two projects, two plan rows
        gone = os.path.join(workdir, "p2", "common", "x.gdb")
        same_string = [
            Source("p1.aprx", "Map", "L1", "k", "X", "t",
                   "DATABASE=..\\common\\x.gdb",
                   os.path.join(workdir, "p1", "common", "x.gdb"), True),
            Source("p2.aprx", "Map", "L2", "k", "X", "t",
                   "DATABASE=..\\common\\x.gdb", gone, False)]
        rows = group_by_workspace(same_string)
        check(len(rows) == 2,
              "one relative workspace string in two projects is two plan rows  "
              "<-- pinned defect")
        check(sorted(r["state"] for r in rows) == [MISSING, PRESENT],
              "the project whose geodatabase is gone keeps its own MISSING verdict")
        check(any(gone in line for line in
                  report_lines([Document("d", OK, same_string, [])])),
              "the plan row names the resolved path that tells the two apart")

        # ---- filtering
        check(matches_filter(buried, "parcels"), "--filter matches a dataset name")
        check(matches_filter(by_layer["Zoning A"], "sde:sqlserver:gisdb01"),
              "--filter matches a server inside the connection string")
        check(not matches_filter(buried, "oracle"), "--filter rejects a miss")
        check(matches_filter(buried, None), "no --filter matches everything")

        # ---- THE REFUSAL: an unreadable document is never a clean one
        broken = os.path.join(home, "Broken.aprx")
        archive = zipfile.ZipFile(broken, "w")
        archive.writestr("map/map.xml", "<CIMMap><Name>truncated")
        archive.close()
        result = scan_document(broken)
        check(result.status == UNSUPPORTED,
              "a zip whose parts do not parse is UNSUPPORTED, not clean  <-- pinned defect")
        check(result.sources == [], "and it claims no sources of its own")
        check(any("did not parse" in n for n in result.notes),
              "the note names the part that failed")
        check("UNSUPPORTED" in "\n".join(report_lines([result])),
              "the report prints the UNSUPPORTED status")

        empty = os.path.join(home, "Empty.aprx")
        archive = zipfile.ZipFile(empty, "w")
        archive.writestr("readme.txt", "not a project")
        archive.close()
        check(scan_document(empty).status == UNSUPPORTED,
              "a zip with no CIM parts is UNSUPPORTED, not an empty clean project")

        notcim = os.path.join(home, "Notes.lyrx")
        handle = open(notcim, "w")
        handle.write("this is not CIM at all")
        handle.close()
        check(scan_document(notcim).status == UNSUPPORTED,
              "a .lyrx that is not JSON is UNSUPPORTED")
        check(scan_document(os.path.join(home, "gone.aprx")).status == UNSUPPORTED,
              "a document that cannot be opened at all is UNSUPPORTED")

        zero = os.path.join(home, "Zero.lyrx")
        handle = open(zero, "wb")
        handle.close()
        zero_doc = scan_document(zero)
        check(zero_doc.status == UNSUPPORTED,
              "a zero-byte document is UNSUPPORTED, not a project with no layers")
        check(any("empty CIM part" in n for n in zero_doc.notes),
              "and the note says the part was empty")

        settings = os.path.join(home, "Settings.lyrx")
        handle = open(settings, "w")
        handle.write('{"theme": "dark", "recent": ["a.aprx"]}')
        handle.close()
        settings_doc = scan_document(settings)
        check(settings_doc.status == UNSUPPORTED,
              "a .lyrx that is valid json but names no CIM type is UNSUPPORTED  "
              "<-- pinned defect")
        check(settings_doc.sources == [],
              "and it reports no sources, which is why it must not read OK")
        check(any("not a CIM document" in n for n in settings_doc.notes),
              "the note says the root object is not a CIM document")
        lying = os.path.join(home, "Zipped.lyrx")
        archive = zipfile.ZipFile(lying, "w")
        archive.writestr("thumbnail.png", "not CIM either")
        archive.close()
        check(scan_document(lying).status == UNSUPPORTED,
              "a .lyrx that is really a zip with no CIM parts is UNSUPPORTED")
        lying_json = os.path.join(home, "Actually.aprx")
        handle = open(lying_json, "w")
        handle.write(_fixture_lyrx("Roads", "", "SPEED"))
        handle.close()
        lying_doc = scan_document(lying_json)
        check(lying_doc.status == OK and lying_doc.sources[0].dataset == "Roads",
              "an .aprx that is really CIM json still scans, the dispatch is on "
              "the bytes and not on the extension")

        # ---- .mapx and .pagx, the two document types with no zip and no array
        mapx = os.path.join(home, "Zoning.mapx")
        handle = open(mapx, "w")
        handle.write(_fixture_mapx())
        handle.close()
        mapx_doc = scan_document(mapx)
        mapx_layers = dict((s.layer, s) for s in mapx_doc.sources)
        check(mapx_doc.status == OK, "a .mapx scans OK")
        check(sorted(mapx_layers) == ["Owner Lookup", "Parcels"],
              "a standalone table the .mapx map holds is reported, not dropped  "
              "<-- pinned defect")
        check(mapx_layers["Parcels"].container == "Zoning Review",
              "the .mapx layer is attributed to the map inside the document  "
              "<-- pinned defect")
        check(mapx_layers["Parcels"].state == MISSING,
              "its relative file geodatabase is resolved and checked like any "
              "other")
        check("00022e68" not in json.dumps(mapx_doc.to_dict()),
              "no stored .sde password reaches the .mapx output")
        check(mapx_layers["Owner Lookup"].dataset == "GIS.Owners",
              "the uRI spelling a real CIM document uses still resolves")

        pagx = os.path.join(home, "Board.pagx")
        handle = open(pagx, "w")
        handle.write(_fixture_pagx())
        handle.close()
        pagx_doc = scan_document(pagx)
        check(pagx_doc.status == OK, "a .pagx scans OK")
        check([s.layer for s in pagx_doc.sources] == ["Roads"],
              "the layer inside the layout's map frame is reported")
        check(pagx_doc.sources[0].container == "Inset",
              "and it is attributed to the map the layout frames")
        check(REDACTION in pagx_doc.sources[0].workspace,
              "the .sde credential in a .pagx is redacted like any other")
        # A layout template holds a layoutDefinition and nothing else. Leaving
        # that member out of the part table sent the whole document through the
        # root sentinel, and the document object itself came back as a layer
        # called "(unnamed)": a row in the report that names nothing at all.
        layout_only = parse_cim(json.dumps({
            "type": "CIMLayoutDocument", "version": "3.5.0",
            "layoutDefinition": {
                "type": "CIMLayout", "name": "Blank Board",
                "uRI": "CIMPATH=layout/layout.xml",
                "elements": [{"type": "CIMMapFrame", "name": "Frame"}]}}))
        only_parts, only_roots = document_parts(layout_only)
        check(only_roots == ["CIMPATH=layout/layout.xml"],
              "a .pagx holding only a layout indexes the layout as its part")
        only_layers, _only_seen = walk_layers(only_parts, only_roots)
        check(only_layers == [],
              "so the document itself is never reported as an unnamed layer  "
              "<-- pinned defect")

        # ---- the --json payload
        payload = json_report([document, result], "gisdb01", root=workdir)
        shown = [s["layer"] for d in payload["documents"] for s in d["sources"]]
        check(len(shown) == 5,
              "--json is filtered like the text report, not left unfiltered")
        check("Buried Parcels" not in shown,
              "a source the filter rejected is absent from the json documents")
        check("hunter2" not in json.dumps(payload),
              "no password reaches the json payload")
        check([d["status"] for d in payload["documents"]].count(UNSUPPORTED) == 1,
              "the json payload still carries the UNSUPPORTED document")
        check(payload["workspaces"][0]["count"] == 5,
              "the json workspace rows are filtered too")

        # ---- a layer name the console codepage cannot encode
        wide = os.path.join(home, "Wide.aprx")
        archive = zipfile.ZipFile(wide, "w")
        archive.writestr(
            "map/map.xml",
            '<CIMMap xsi:type="typens:CIMMap" %s><Name>Mapa</Name>'
            '<Layers xsi:type="typens:ArrayOfString">'
            '<String>CIMPATH=Map/w.xml</String></Layers></CIMMap>' % FIXTURE_NS)
        archive.writestr("Map/w.xml", _fixture_layer_xml(
            u"\u5730\u7c4d\u56fe\u5c42", "Parcels", FIXTURE_GDB))
        archive.close()
        narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        saved_stdout = sys.stdout
        sys.stdout = narrow
        try:
            code = main([wide])
        except UnicodeEncodeError:
            code = "UnicodeEncodeError"
        finally:
            sys.stdout = saved_stdout
        check(code == 0,
              "a layer name the console codepage cannot encode does not kill "
              "the scan  <-- pinned defect")

        # ---- the .lyrx path end to end, and the semantic diff
        lyrx = os.path.join(home, "Conflict.lyrx")
        handle = open(lyrx, "w")
        handle.write(_fixture_lyrx("ACLED_2005_2018", "YEAR >= 2010", "FATALITIES"))
        handle.close()
        lyrx_doc = scan_document(lyrx)
        check(lyrx_doc.status == OK, "an unzipped .lyrx scans OK")
        check(lyrx_doc.sources[0].dataset == "ACLED_2005_2018",
              "the .lyrx source is read without any zip involved")

        before = parse_cim(_fixture_lyrx("ACLED_2005_2018", "YEAR >= 2010",
                                         "FATALITIES"))
        after = parse_cim(_fixture_lyrx("ACLED_2019_2024", "YEAR >= 2019",
                                        "EVENTS"))
        kinds = [c[0] for c in diff_documents(before, after)]
        check("source repointed" in kinds, "the diff reports a repointed source")
        check("definition query changed" in kinds,
              "the diff reports a changed definition query")
        check("renderer field changed" in kinds,
              "the diff reports a changed renderer field")
        check(len(kinds) == 3, "an unchanged field is not reported")
        check(diff_documents(before, before) == [],
              "a document diffed against itself reports nothing")
        renamed = parse_cim(_fixture_lyrx("ACLED_2005_2018", "YEAR >= 2010",
                                          "FATALITIES", name="Renamed"))
        kinds = [c[0] for c in diff_documents(before, renamed)]
        check("layer removed" in kinds and "layer added" in kinds,
              "a renamed layer reads as one removed and one added")
        rendered = diff_lines(diff_documents(before, renamed))
        check(rendered[-1] == "2 change(s)",
              "the rendered diff counts the changes it printed")
        check("layer removed: ACLED" in rendered,
              "the removed layer is named")
        check("   before: ACLED" in rendered,
              "a removal prints a before side and no after side")
        check("   after:  Renamed" in rendered,
              "an addition prints an after side and no before side")
        check(len(rendered) == 6,
              "neither half of a rename prints an empty side")
        check(diff_lines([])[0].startswith("no semantic difference"),
              "two identical documents render as one line saying so")

        # ---- the command line, end to end
        handle = open(os.path.join(home, "Planning.aprx.bak"), "w")
        handle.write("the backup Pro leaves beside a project")
        handle.close()
        walked = find_documents(home)
        check(not any(p.endswith(".bak") for p in walked),
              "a backup file beside a project is not walked as a document")
        check(len(walked) == 12,
              "the directory walk finds every document in the tree")
        check(sorted(set(os.path.splitext(p)[1] for p in walked))
              == [".aprx", ".lyrx", ".mapx", ".pagx"],
              "all four extensions the README claims are walked")
        # ---- a directory the walk cannot list, against a real ACL
        blocked = os.path.join(workdir, "blocked")
        os.makedirs(os.path.join(blocked, "locked"))
        handle = open(os.path.join(blocked, "locked", "Hidden.lyrx"), "w")
        handle.write(_fixture_lyrx("Roads", "", "SPEED"))
        handle.close()
        undo = _deny_read(os.path.join(blocked, "locked"))
        try:
            denied = []
            check(find_documents(blocked, denied) == [] and len(denied) == 1,
                  "an unreadable subdirectory is counted, not walked past"
                  "  <-- pinned defect")
            code, out = run([blocked])
            check(code == 1,
                  "an unreadable directory reaches exit 1, as UNSUPPORTED does")
            check("1 unreadable directory(s)" in out and "locked" in out,
                  "and the report names the directory it could not list")
            code, out = run([blocked, "--json"])
            check(json.loads(out)["unreadable_directories"]
                  == [os.path.join(blocked, "locked")],
                  "--json names it too, so the two modes cannot disagree")
        finally:
            undo()
        code, out = run([blocked])
        check(code == 0 and "0 unreadable directory(s)" in out,
              "the same tree exits 0 once the folder can be listed again")
        readable = []
        find_documents(home, readable)
        check(readable == [], "a fully readable root reports none unreadable")

        code, out = run([home])
        check(code == 1, "a tree holding an unreadable document exits 1")
        check("Zoning.mapx" in out and "Board.pagx" in out,
              "the report names the .mapx and the .pagx it scanned")
        check("6 OK, 6 UNSUPPORTED" in out,
              "the footer separates the readable documents from the rest")
        code, out = run([home, "--filter", "gisdb01"])
        check("Zoning A" in out, "--filter keeps a layer on the matched server")
        check("Buried Parcels" not in out,
              "--filter drops a layer that does not match")
        check("Conflict.lyrx" not in out,
              "a document whose every source the filter rejected stays quiet")
        check("Notes.lyrx" in out,
              "an UNSUPPORTED document is printed whatever the filter says")
        code, out = run([home, "--json", "--filter", "GIS.Owners"])
        payload = json.loads(out)
        check(payload["root"] == home, "--json names the root it scanned")
        check([s["layer"] for d in payload["documents"] for s in d["sources"]]
              == ["Owner Lookup"],
              "--json --filter reports the one source that matched")
        # The authorisation flags, end to end, against a path that really is
        # stat-ed. "\\\\.\\" is the Win32 device namespace: it is shaped like a
        # UNC path, so it takes the network branch, and the object manager
        # resolves it locally. No server, no share, no SMB timeout. Elsewhere it
        # is an ordinary relative name that is also not there. Either way the
        # stat runs for real and answers False, so the flag moving the verdict
        # from unknown to MISSING is the stat happening and nothing else.
        unc = os.path.join(home, "Unc.lyrx")
        handle = open(unc, "w")
        handle.write(json.dumps({
            "type": "CIMLayerDocument", "version": "3.5.0",
            "layers": ["CIMPATH=unc.json"],
            "layerDefinitions": [{
                "type": "CIMFeatureLayer", "name": "UNC Parcels",
                "uRI": "CIMPATH=unc.json",
                "featureTable": {"type": "CIMFeatureTable", "dataConnection": {
                    "type": "CIMStandardDataConnection",
                    "workspaceConnectionString":
                        "DATABASE=\\\\.\\nosuchshare\\parcels.gdb",
                    "dataset": "Parcels"}}}]}))
        handle.close()
        code, out = run([unc])
        check(code == 0 and UNKNOWN in out and MISSING not in out,
              "a UNC source is unknown while the network check is off")
        code, out = run([unc, "--check-network"])
        check(code == 0 and MISSING in out,
              "--check-network stats it for real and calls it MISSING")
        code, out = run([unc, "--apply"])
        check(code == 0 and MISSING in out,
              "--apply authorises that same stat, which is all --apply does")
        code, out = run([mapx, "--apply"])
        check(code == 0 and "Owner Lookup" in out,
              "and it scans the document exactly as it would without the flag")
        code, out = run([])
        check(code == 64, "no argument at all is a usage error")
        check("--self-test" in out, "and the message says how to try the tool")
        code, out = run([os.path.join(home, "nowhere")])
        check(code == 2, "a scan path that does not exist exits 2")
        nothing = os.path.join(workdir, "no-documents")
        os.makedirs(nothing)
        code, out = run([nothing])
        check(code == 0 and "no .aprx, .lyrx, .mapx or .pagx" in out,
              "a folder holding no documents exits 0 and says so")

        shouty = os.path.join(workdir, "shouty")
        os.makedirs(shouty)
        handle = open(os.path.join(shouty, "Legacy.LYRX"), "w")
        handle.write(_fixture_lyrx("Roads", "", "SPEED"))
        handle.close()
        check([os.path.basename(p) for p in find_documents(shouty)]
              == ["Legacy.LYRX"],
              "an extension in capitals is still an ArcGIS document")
        code, out = run([shouty])
        check(code == 0 and "Roads" in out,
              "and the walk hands it to the scanner like any other")

        repointed = os.path.join(home, "Conflict2.lyrx")
        handle = open(repointed, "w")
        handle.write(_fixture_lyrx("ACLED_2019_2024", "YEAR >= 2019", "EVENTS"))
        handle.close()
        code, out = run(["--diff", lyrx, repointed])
        check(code == 0, "--diff on two readable documents exits 0")
        check("definition query changed: ACLED" in out and "3 change(s)" in out,
              "--diff prints the three fields that moved")
        code, out = run(["--diff", lyrx, repointed, "--json"])
        check([c["change"] for c in json.loads(out)] ==
              ["source repointed", "definition query changed",
               "renderer field changed"],
              "--diff --json emits the same three changes as data")
        code, out = run(["--diff", lyrx, os.path.join(home, "gone.lyrx")])
        check(code == 2, "--diff against a document that is not there exits 2")
        code, out = run(["--diff", lyrx, notcim])
        check(code == 1, "--diff on a document that will not parse exits 1")
        check(UNSUPPORTED in out, "and it says which document was unreadable")

        # ---- argument handling
        args = _parse([workdir])
        check(args.check_network is False, "--check-network defaults to OFF")
        check(args.apply is False, "--apply defaults to OFF")
        check(args.json is False, "--json defaults to OFF")
        check(args.filter is None, "--filter defaults to nothing")
        check(args.diff is None, "--diff defaults to nothing")
        check(args.root == workdir, "the scan root is read")
        check(_parse(["--self-test"]).self_test, "--self-test parses")
        check(_parse([workdir, "--json"]).json is True, "--json is read")
        check(_parse([workdir, "--filter", "gisdb01"]).filter == "gisdb01",
              "--filter is read")
        check(_parse([workdir, "--check-network"]).check_network is True,
              "--check-network is read")
        check(_parse([workdir, "--apply"]).apply is True, "--apply is read")
        check(_parse(["--diff", "a.lyrx", "b.lyrx"]).diff == ["a.lyrx", "b.lyrx"],
              "--diff takes exactly two documents")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="cimscan.py",
        description="Report every data source in a tree of .aprx, .lyrx, .mapx "
                    "and .pagx files with no ArcGIS licence, and refuse to call "
                    "an unreadable project a clean one.",
        epilog="cimscan never writes to a document. The only outbound action is "
               "checking whether a UNC path exists, and that is off until "
               "--apply or --check-network is passed.",
    )
    parser.add_argument("root", nargs="?",
                        help="folder to walk, or a single document")
    parser.add_argument("--filter", default=None,
                        help="report only sources whose workspace, path, "
                             "dataset or layer name contains this text")
    parser.add_argument("--json", action="store_true",
                        help="emit the scan as JSON instead of the text report")
    parser.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"),
                        default=None,
                        help="compare two layer documents semantically instead "
                             "of scanning a tree: source, definition query, "
                             "renderer field, connection type")
    parser.add_argument("--check-network", dest="check_network",
                        action="store_true",
                        help="stat paths on UNC network shares. Off by default "
                             "because a switched-off server blocks every stat "
                             "for the full SMB timeout.")
    parser.add_argument("--apply", action="store_true",
                        help="authorise the one outbound action, the same as "
                             "--check-network. Nothing is ever written.")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the offline assertions and exit")
    return parser.parse_args(argv)


def _run_diff(args):
    for path in args.diff:
        if not os.path.isfile(path):
            print("error: no such document: %s" % path, file=sys.stderr)
            return 2
    documents = []
    for path in args.diff:
        try:
            handle = open(path, "rb")
            try:
                documents.append(parse_cim(handle.read()))
            finally:
                handle.close()
        except Exception as exc:
            print("error: %s is %s: %s" % (path, UNSUPPORTED, exc),
                  file=sys.stderr)
            return 1
    changes = diff_documents(documents[0], documents[1])
    if args.json:
        print(json.dumps([{"change": c[0], "layer": c[1],
                           "before": c[2], "after": c[3]} for c in changes],
                         indent=2))
    else:
        for line in diff_lines(changes):
            print(line)
    return 0


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    # A layer name can hold characters the console codepage cannot encode. With
    # a cp1252 stdout, which is what a scheduled task on a Windows file server
    # gets, one such name ended the run with a UnicodeEncodeError and lost a
    # report that was already complete. Replace those characters instead.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    if args.self_test:
        return self_test()
    if args.diff:
        return _run_diff(args)

    if not args.root:
        print("error: a folder or a document is required. Use --self-test to "
              "verify the tool without any ArcGIS data.", file=sys.stderr)
        return 64
    if not os.path.exists(args.root):
        print("error: no such path: %s" % args.root, file=sys.stderr)
        return 2

    unreadable = []
    paths = find_documents(args.root, unreadable)
    if not paths and not unreadable:
        print("no .aprx, .lyrx, .mapx or .pagx documents under %s" % args.root)
        return 0

    check_network = args.check_network or args.apply
    documents = [scan_document(p, check_network) for p in paths]

    if args.json:
        print(json.dumps(json_report(documents, args.filter, args.root,
                                     unreadable), indent=2))
    else:
        for line in report_lines(documents, args.filter, unreadable):
            print(line)

    # An unreadable document is the failure this tool exists to surface, so it
    # has to reach the exit code. A pipeline that only reads stdout still stops.
    # A directory the walk could not list is that same failure one level up:
    # the documents inside it were never scanned and never counted.
    return 1 if unreadable or any(d.status == UNSUPPORTED
                                  for d in documents) else 0


if __name__ == "__main__":
    sys.exit(main())
