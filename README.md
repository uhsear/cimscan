# cimscan

Report every data source in a tree of .aprx, .lyrx, .mapx and .pagx files with no ArcGIS licence, and refuse to call an unreadable project a clean one.

An analyst leaves and nobody knows which of the projects on the shared drive still point at the
SDE instance you are decommissioning on Friday. In the author's experience the way to check is
opening each project in Pro and waiting for red exclamation marks. That needs a licensed seat and
cannot run on the file server where the projects live.

So nobody checks. On Monday the planning map book opens broken, with no record of what those layers
used to point at.

Esri's own answer is `arcpy.mp.ArcGISProject.listBrokenDataSources()`, which "returns a Python list
of Layer and/or Table objects that have broken connections to their original source data for all
maps in a project"
([Esri documentation](https://pro.arcgis.com/en/pro-app/latest/arcpy/mapping/arcgisproject-class.htm)).
It understands every layer type Pro understands and follows the format as it changes. It needs
arcpy and a licence on the machine that runs it. `cimscan` reads the project files directly, so it
runs where the projects live, and it lists every source and not only the broken ones. See
[Why not arcpy](#why-not-arcpy).

```
$ python cimscan.py --self-test
cimscan self-test: no arcpy, no licence, no network
--------------------------------------------------------------------
...
PASS  a stored portal TOKEN never survives redaction  <-- pinned defect
...
PASS  a token in a service URL query string is redacted too  <-- pinned defect
...
PASS  a password holding an ampersand is redacted whole  <-- pinned defect
PASS  a password holding a semicolon loses its tail too  <-- pinned defect
PASS  a quoted password holding a semicolon is one value  <-- pinned defect
...
PASS  a token in the query of a URL= property is redacted  <-- pinned defect
...
PASS  a password in URL userinfo is redacted, the user kept  <-- pinned defect
...
PASS  a joined layer reports its source and destination tables  <-- pinned defect
...
PASS  --filter finds the server hidden behind a join  <-- pinned defect
...
PASS  --diff sees a repoint of the joined table  <-- pinned defect
PASS  repointing the first of two same-named layers is reported  <-- pinned defect
...
PASS  --diff of two JSON files that are not CIM exits 1  <-- pinned defect
...
PASS  a drive path on a POSIX host is unknown, not MISSING  <-- pinned defect
PASS  a UNC path on a POSIX host is unknown even with --check-network  <-- pinned defect
...
PASS  a drive letter the session does not have is unknown, not MISSING  <-- pinned defect
...
PASS  --json on a folder with no documents is still JSON  <-- pinned defect
...
PASS  the footer counts every source and only the MISSING ones  <-- pinned defect
...
PASS  a layer the layers array does not list is still reported  <-- pinned defect
...
PASS  a zip of XML that names no CIM type is UNSUPPORTED, not OK  <-- pinned defect
...
PASS  a layer or map uri that is not a string still reports its layer  <-- pinned defect
...
PASS  a secret inside the resolved path is redacted too  <-- pinned defect
PASS  PW, PASS, SIG, KEY, SESSION and Authorization values are redacted  <-- pinned defect
...
PASS  a URL password holding an at sign is redacted whole  <-- pinned defect
...
PASS  every parser error exits 64  <-- pinned defect
...
PASS  the process itself exits 64 on --ap, not argparse's 2  <-- pinned defect
...
PASS  a unique prefix of --apply is refused by the parser  <-- pinned defect
PASS  no shorter spelling of --apply or --check-network is accepted  <-- pinned defect
PASS  the full flag names still parse
...
--------------------------------------------------------------------
280 assertions, 0 failed
```

## Requirements

Python 3.9 or newer, run on 3.9, 3.10, 3.12 and 3.13. Standard library only: `zipfile`, `xml.etree.ElementTree`, `json`, `os`, `re`,
`argparse`. No `arcpy`, no ArcGIS licence, no install step. It runs on ArcGIS Pro's Python and on a
plain `python3` on a file server.

```
git clone https://github.com/uhsear/cimscan.git
```

## Quick start

```
python cimscan.py --self-test
python cimscan.py \\fileserver\projects
```

## Usage

Scan a tree, grep it for the server you are retiring, then export the result.

```
python cimscan.py \\fileserver\projects
python cimscan.py \\fileserver\projects --filter sde:sqlserver:dbhost1
python cimscan.py \\fileserver\projects --json > sources.json
python cimscan.py --diff before.lyrx after.lyrx
```

| Flag | Default | What it does |
|---|---|---|
| `root` | none | Folder to walk, or one document. Required unless `--diff` or `--self-test`. An `.sde` is named and refused, see below. |
| `--filter` | none | Report only sources whose workspace, path, dataset or layer name contains this text. |
| `--json` | off | Emit the scan as JSON instead of the text report. |
| `--diff BEFORE AFTER` | none | Compare two layer documents semantically instead of scanning a tree. Takes no folder. |
| `--check-network` | off | Stat paths on UNC network shares. |
| `--apply` | off | The same authorisation as `--check-network`. Nothing is ever written. The name follows the write gate of the other tools in this portfolio, and here it gates one network stat because the tool has no write to gate. |
| `--self-test` | off | Run the offline assertions and exit. |

The UNC check is off by default because a stat against a server that is already switched off blocks
for the full SMB timeout. Three hundred layers pointed at a decommissioned box turn a scan into an
afternoon. Local paths are always checked; only the network round trip needs the flag. The share
host is read from the document, and Windows connects to it to answer the stat. Use
`--check-network` and `--apply` only on documents you trust.

The parser refuses abbreviated flags (`allow_abbrev=False`). `--ap` is an error and not `--apply`,
so the one flag that authorises an outbound action cannot be typed by accident. The parser exits 64
on that error and on every other bad command line.

The UNC check was run for real once, by hand, on Windows against the administrative share of the
machine itself, and not in the self-test, because a share is specific to a host. The tool has no
HTTP path, so there is no `http.server` stub to point it at:

```
$ python cimscan.py Loop.lyrx --check-network
== Loop.lyrx  [OK]
   present  (no map) > Here                    x                        \\localhost\C$\Windows
   MISSING  (no map) > Gone                    x                        \\localhost\C$\no-such-folder-cimscan

workspaces: 2 source(s) across 2 workspace(s)
      1  present  \\localhost\C$\Windows
      1  MISSING  \\localhost\C$\no-such-folder-cimscan

1 document(s), 1 OK, 0 UNSUPPORTED, 2 source(s), 1 missing, 0 unreadable directory(s)
```

Without the flag the same two rows read `unknown`.

## What it reports

For every layer and standalone table in every document:

- The map or layout that holds it, and its name.
- The connection type, for example `CIMStandardDataConnection` or `CIMSqlQueryDataConnection`.
- The workspace connection string, with every secret redacted.
- The dataset it asks for, or the query name when a query layer has no dataset.
- Whether the resolved path is on disk right now: `present`, `MISSING`, or `unknown`.

`unknown` is a third verdict on purpose. A feature service and a remote geodatabase have no local
path to check, and calling them `MISSING` would bury the file geodatabases that really are gone.
A Windows path (a drive letter or a UNC share) read on a Linux host is `unknown` too. The host
cannot evaluate it, so a layer on `C:\gis\parcels.gdb` is not `MISSING` just because the file
server has no `C:` drive. A drive letter the running Windows session does not have is `unknown`
for the same reason. A scheduled task does not see the drives a user mapped, and its `X:` root does
not exist, so every layer on it would otherwise read as gone. The same document gives the same verdict on every host that can answer,
and `unknown` on every host that cannot.

Sources are then grouped by workspace, because four layers on one `.sde` are one decommissioning
decision and not four. The fixture project the self-test builds is a zip of CIM XML parts, the way
an `.aprx` is. Scanned for real:

```
$ python cimscan.py p20/Planning.aprx
== p20/Planning.aprx  [OK]
   MISSING  Planning > Buried Parcels          Parcels                  DATABASE=..\commondata\parcels.gdb
   unknown  Planning > Zoning A                GIS.Zoning_a             SERVER=dbhost1;INSTANCE=sde:sqlserver:dbhost1;DATABASE=warehouse1;USER=viewer;PASSWORD=***REDACTED***;AUTHENTICATION_MODE=DBMS
   ...
   MISSING  Planning > Owner Lookup            Owners                   DATABASE=..\commondata\parcels.gdb

workspaces: 7 source(s) across 2 workspace(s)
      2  MISSING  DATABASE=..\commondata\parcels.gdb  -> ...\commondata\parcels.gdb
      5  unknown  SERVER=dbhost1;INSTANCE=sde:sqlserver:dbhost1;DATABASE=warehouse1;USER=viewer;PASSWORD=***REDACTED***;AUTHENTICATION_MODE=DBMS

1 document(s), 1 OK, 0 UNSUPPORTED, 7 source(s), 2 missing, 0 unreadable directory(s)
```

A `.mapx` and a `.pagx` are reported the same way. These are self-test fixtures built from the CIM
spec, because no `.mapx` or `.pagx` saved by Pro was kept for this repository:

```
$ python cimscan.py Zoning.mapx
== Zoning.mapx  [OK]
   MISSING  Zoning Review > Parcels            Parcels                  DATABASE=..\commondata\parcels.gdb
   unknown  Zoning Review > Owner Lookup       GIS.Owners               ENCRYPTED_PASSWORD=***REDACTED***;ENCRYPTED_PASSWORD_UTF8=***REDACTED***;SERVER=dbhost1;...
```

The standalone table is the part that is easy to lose. A `.mapx` holds it in
`standaloneTableDefinitions`, beside the layers and not inside the map. A reader that walks only
`layerDefinitions` drops it, and prints a clean report with one source missing.

A joined layer is reported by the tables it joins. The CIM specification gives
`CIMRelQueryTableDataConnection` a `sourceTable` and a `destinationTable`, each a data connection
of its own
([Esri cim-spec](https://github.com/Esri/cim-spec/blob/main/docs/v3/CIMVectorLayers.md#cimrelquerytabledataconnection)).
The join object holds no workspace itself. Reading only the first connection printed the layer as
`(no workspace)` and hid the server behind it, so a `--filter` for the host you were retiring found
nothing and the scan still exited 0. Each nested table is now its own row under the layer's name:

```
$ python cimscan.py Joined.lyrx --filter olddb
== Joined.lyrx  [OK]
   unknown  (no map) > Parcels joined          GIS.Owners               SERVER=olddb;INSTANCE=sde:sqlserver:olddb;DATABASE=warehouse1

workspaces: 1 source(s) across 1 workspace(s)
      1  unknown  SERVER=olddb;INSTANCE=sde:sqlserver:olddb;DATABASE=warehouse1
```

A plan row names the resolved path as well as the workspace string. Two projects both saying
`DATABASE=..\commondata\parcels.gdb` mean two different folders, so they stay two rows. Keying the
row on the text alone let a geodatabase that was gone hide behind a row reading `present`.

## What it refuses

It refuses to report an unreadable document as a clean one. A document whose container will not
open, whose parts will not parse, that holds no CIM parts at all, or whose root object, or every zipped part, names no CIM
type, is marked `UNSUPPORTED`, counted separately, and drives the exit code to 1.

```
$ python cimscan.py mixed
== mixed\Settings.lyrx  [UNSUPPORTED]
   ! the root object is not a CIM document
   (no data sources read)

== mixed\Truncated.aprx  [UNSUPPORTED]
   ! could not open: File is not a zip file
   (no data sources read)

== mixed\Zero.lyrx  [UNSUPPORTED]
   ! part Zero.lyrx did not parse: empty CIM part
   (no data sources read)
```

The first one is the quiet case. A file that is valid JSON and is not a CIM document parses without
an error and holds no layers, so a scanner that trusts the parse reports it as a document with
nothing to worry about. `--diff` makes the same check on both documents. Two settings files used to
compare equal and print `no semantic difference`; now they exit 1 and name the file.

This is the whole point. A scanner that returns zero sources for a file it could not open reports
the project as having nothing to worry about, which is the one answer that is certainly wrong. A
document with one bad part is `UNSUPPORTED` too, even though the other parts were read, because the
sources listed are then a floor and not the whole truth.

It refuses to walk past a folder it could not read, for the same reason. `os.walk` reports a
directory it cannot list by calling `onerror`, and does nothing at all when no `onerror` is passed.
Two sibling folders each holding a `proj.aprx` reported `2 document(s)`; with read denied on one of
them the same scan reported `1 document(s)` and exited on the documents it did find. A folder that
is off limits and a folder that is empty read the same. Both are now named, counted in the footer,
and drive the exit code to 1:

```
!! cannot list projects\archive: Access is denied
1 document(s), 0 OK, 1 UNSUPPORTED, 0 source(s), 0 missing, 1 unreadable directory(s)
```

`--json` carries the same list under `unreadable_directories`, so the two output modes cannot
disagree about what the scan covered. The documents inside such a folder were never opened, so
nothing is claimed about them: the count says only how much of the tree the report does not cover.

It also refuses to print a secret. An `.sde` connection string saved with a stored credential holds
`PASSWORD=` or `ENCRYPTED_PASSWORD=` in clear text inside the document. Any property whose name
contains `PASS`, `PW`, `TOKEN`, `SECRET`, `KEY`, `CREDENTIAL`, `SIG`, `SESSION`,
`AUTHORIZATION` or `BEARER`
loses its whole value before the record is built, so the text report, the `--json` output and the
`--diff` output are all covered by one guard instead of three. The rules are:

- The string splits into properties on `;`, except inside a quoted value. A secret property loses
  everything up to the next property, so a password holding `&` or `?` goes whole.
- A chunk with no `=` right after a secret property is the rest of a password that held a `;`, and
  goes too.
- In every other property, a `name=value` pair with a secret name is redacted wherever it sits: in
  the query string of a `URL=` property, or inside `CUSTOMPARAMETERS=token=...`. The other query
  parameters and the key are kept.
- A password in URL userinfo (`scheme://user:password@host`) is redacted and the user kept. The
  password runs to the last `@` before the path, so one that holds `@` goes whole.
- The match is on a substring and errs toward hiding. A harmless `KEYFILE` loses its value too.
- The resolved `path` field gets the same treatment, so a secret in a `DATABASE=` URL does not
  reach it.
- A URL of any scheme is never treated as a folder, so its userinfo cannot reach the `path` field.

Redacting the workspace and the path also means a scan of a shared drive can be committed or pasted into a
ticket. This is a real scan of two layers that hold a token and a password with an ampersand:

```
$ python cimscan.py Secrets.lyrx
== Secrets.lyrx  [OK]
   unknown  (no map) > Svc                     x                        URL=https://h/x/FeatureServer?token=***REDACTED***&f=json
   unknown  (no map) > Db                      y                        SERVER=a;DATABASE=b;PASSWORD=***REDACTED***;USER=u
```

## An .sde on the command line

An `.sde` is not a CIM document. The Create Database Connection tool summary reads "Creates a file
that ArcGIS uses to connect to a database, a cloud data warehouse, or an enterprise geodatabase".
Its `out_name` parameter says "The output file will have the .sde extension", and its output is a
"database connection file (.sde or .dbconn)"
([Esri documentation](https://pro.arcgis.com/en/pro-app/3.5/tool-reference/data-management/create-database-connection.htm)).
Before this note existed, an `.sde` handed to `cimscan` went to the XML parser and came back as
`part Prod.sde did not parse: not well-formed (invalid token)`. That was true, and it told the
reader nothing about what they had passed in. Now the report names the file type and the tool that
does read it:

```
$ python cimscan.py Prod.sde
== Prod.sde  [UNSUPPORTED]
   ! an .sde is an ArcGIS database connection file, not a CIM document, so cimscan does not open it. To find a stale host in .sde files, use stalehost: https://github.com/uhsear/stalehost
   (no data sources read)

workspaces: 0 source(s) across 0 workspace(s)

1 document(s), 0 OK, 1 UNSUPPORTED, 0 source(s), 0 missing, 0 unreadable directory(s)
```

The status, the count and the exit code are the ones the old message had: `UNSUPPORTED`, and exit 1.
`--json` carries the same text in `notes`, and `--diff` prints it for either document:

```
$ python cimscan.py --diff Prod.sde Prod.sde
error: Prod.sde is UNSUPPORTED: an .sde is an ArcGIS database connection file, not a CIM document, so cimscan does not open it. To find a stale host in .sde files, use stalehost: https://github.com/uhsear/stalehost
```

The decision is made on the extension, before the file is opened. An `.sde` can hold a saved user
name and password (the same page says the tool saves them "in the connection file" when that box is
checked, which is the default), so no byte of it is read and none can reach the report. The
self-test pins this with an `.sde` that does not exist: it gets the same note as one that does, which
only holds if nothing was opened. A directory walk still ignores `.sde` files, as before. The note
appears only when you name the file yourself, so a folder that holds only `.sde` files prints
`no .aprx, .lyrx, .mapx or .pagx documents under <dir>` and exits 0. Use stalehost for those.

## The .lyrx diff

A `.lyrx` is a whole JSON document for one layer, and a real one runs to hundreds of lines. Saving
the same layer again rewrites its uri, so a textual diff reports a change that means nothing. The
diff compares four things, which is what a layer review is about: source, definition query,
renderer field and connection type. A layer is matched by name. Two layers with the same name are
told apart by their order, the second being `Name (#2)`, so a repoint of the first is not lost
behind the second. A joined layer compares by all of its tables.

```
$ python cimscan.py --diff Events_before.lyrx Events_after.lyrx
source repointed: Events
   before: DATABASE=..\events.gdb|Events_2005_2018
   after:  DATABASE=..\events.gdb|Events_2019_2024
definition query changed: Events
   before: COUNTRY = 'A' And YEAR >= 2010
   after:  COUNTRY = 'A' And YEAR >= 2019
renderer field changed: Events
   before: FATALITIES
   after:  EVENTS

3 change(s)
```

Any stored password in a repointed connection is redacted here too. A `.lyrx` review is exactly
where a credential would otherwise reach a pull request. These files are self-test fixtures.

## Exit codes

0 every document parsed and every directory listed, 1 at least one `UNSUPPORTED` or at least one
directory that could not be listed, 2 the scan path does not exist, 64 usage error. Usage error
covers no root, `--diff` with a folder, and every argument the parser rejects: an unknown flag, a
prefix such as `--ap`, `--diff` without two documents. The parser's own default for these is 2, so
it is overridden and 2 always means a missing path. A root folder that exists and cannot be listed
exits 1, not 2.

## Why not arcpy

`arcpy.mp.ArcGISProject(path).listBrokenDataSources()` does this properly. It understands every
layer type Pro understands, it follows the format when the format changes, and it is written by the
people who own it. If you have a licensed seat and the projects are reachable from it, use arcpy.

The gap is where the projects live. arcpy needs a checked-out Pro licence on the machine running the
script, and that machine is never the file server holding the shared drive. So the inventory does
not get run, or it gets run once by hand on somebody's workstation against a subset. This reads the
CIM bytes directly, which means it runs on the server, in a scheduled task, or in CI, with nothing
installed. It knows less than arcpy does. It runs where arcpy cannot.

A search of GitHub for `lyrx` finds converters, not checkers:
[lyrx2sld](https://github.com/camptocamp/lyrx2sld),
[lyrxtoqml](https://github.com/arc2qgis/lyrxtoqml) and
[geostyler-lyrx-parser](https://github.com/geostyler/geostyler-lyrx-parser) read a `.lyrx` to
translate its styling for GeoServer, QGIS or GeoStyler. They do not report where a layer's data
lives, and they do not read an `.aprx`. A search for a broken-data-source checker for `.aprx` files
found nothing, which says only that the search found nothing.

Two format facts cost more time than anything else here, and both are pinned by assertions:

- An `.aprx` is a ZIP archive whose layer documents are CIM **XML**. A `.lyrx` is bare CIM **JSON**
  on disk and is not zipped at all. Code that handles one fails silently on the other. Both
  serialisations describe the same object model, and the member names differ only in case, so
  `cimscan` lower-cases every member name and reads both with one extractor. The case is not
  predictable on either side: a saved `.lyrx` can spell the layer uri `"uRI"`, and the fixtures do.
  The CIM specification describes the JSON side
  ([Esri cim-spec](https://github.com/Esri/cim-spec)); the XML parts inside an `.aprx` are not
  covered by it, so that half is read from saved files.
- One zip can hold `Map/<name>.xml` and `map/<name>.xml`, the same folder spelled two ways, with one
  map part referencing layers under both spellings. The self-test fixture does that. A
  case-sensitive lookup drops those layers and still prints a clean-looking report, which is worse
  than an error.

## Limits

- It reports what the document says, not what the database contains. A path that exists may still
  hold a table that was dropped last week.
- Layer types it has no rule for are reported by their connection type with whatever workspace and
  dataset members they carry. It does not pretend to know every CIM class Pro supports.
- `.mapx` and `.pagx` are read as CIM JSON like a `.lyrx`. A `.mapx` names its map `mapDefinition`
  and a `.pagx` names its layout `layoutDefinition`, both singular. Both keep their standalone tables
  in `standaloneTableDefinitions`. All three members are read, so a layer reports the map that holds
  it and a standalone table is not dropped. A layout element that carries its own source, instead of
  framing a map, is not specially handled.
- The diff compares source, definition query, renderer field and connection type. It says nothing
  about symbology, labelling, pop-ups or scale ranges. It ignores group nesting: a layer moved from
  one group to another is not a change.
- It never writes to a document and never repairs one. Repointing a broken layer is
  `updateConnectionProperties`, which needs arcpy.
- XML is parsed with the standard library's `ElementTree`. It does not fetch external entities, but
  a deliberately malicious document can still cost memory through nested internal entities. Scan
  trees of ordinary files you own: a document that is a FIFO or a device would block the read, and a
  huge or zip-bomb document is read fully into memory. Neither is guarded against.
- Findings never change the exit code. A scan full of `MISSING` sources exits 0, because exit 1 means
  only that something could not be read. There is no flag to fail a CI job on `MISSING`.
- An unreadable directory is reported once, by the path the walk was refused. Its subfolders are
  never reached, so one line can stand for a whole branch of the tree.
- Symlinked folders are never descended, and the footer does not count them.
- A folder with no documents exits 0. In text mode it prints one sentence. With `--json` it prints
  a payload with an empty document list. Either way a scheduled job pointed at an empty or
  unmounted share exits 0, so check the document count.
- Row counts, field lists and spatial references are out of scope. This answers "what does it point
  at", not "what is in it".
- Only `.sde` is named, and only when you name the file yourself. A tree walk never reports `.sde`
  files. A missing `.sde` named on the command line exits 2 with `no such path`, not the `.sde`
  note. The extension match is on the final extension only, so `Prod.sde.` or `Prod.sde::$DATA`,
  which Windows resolves to an `.sde`, is not recognised. The same Esri page gives "`.sde` or
  `.dbconn`" as the output of the Python tool, so `.dbconn` is a second connection file type.
  `cimscan` has no rule for it and stalehost is not documented to read it, so a `.dbconn` named on
  the command line gets the old parse error rather than a pointer that might be wrong.
- Only the first connection object of a layer is read, plus the tables nested inside it when it is a
  join. A relate to another table is not followed. A layer with no connection anywhere under it is
  skipped without a note. A layer nested more than 64 levels deep is dropped without a note.
- A zip with two members of one name keeps only the last of them.
- A workspace string with a `DATABASE` property and no `SERVER` or `INSTANCE`, for example a cloud
  warehouse connection, is treated as a folder path and reported `MISSING`. The real shape of such a
  string could not be checked offline.
- A drive letter mapped to a network share is treated as a local path and statted even without
  `--check-network`, because only `\\` and `//` are recognised as network paths.
- A scan whose root is a UNC share reports every relative workspace as `unknown` unless
  `--check-network` is passed, because the resolved path is itself a UNC path.
- On a case-sensitive Linux filesystem, a workspace path whose case differs from the folder on disk
  is reported `MISSING` although Windows would find it. A Windows-rooted path with no drive, such as
  `\data\x.gdb`, is statted as `/data/x.gdb` on Linux, so that verdict differs by host.
- For a folder-based workspace such as a shapefile or CSV folder, only the folder is checked. A
  deleted dataset file inside an existing folder is reported `present`.
- A service layer keeps its URL in a nested `serverConnection`, so its row shows `(no dataset)`:
  `objectName` is not read from the outer connection.
- A filtered scan hides an `[OK]` document that matched nothing. A `--filter` match also reads the
  resolved path, so a document stored in a folder whose name holds the filter text matches on every
  relative workspace.
- A directory walk ignores `.sde` files and exits 0 on a folder that holds only `.sde` files. The
  `.sde` note appears only when the file is named on the command line. `Prod.sde` with a trailing
  space or dot is not recognised, and a directory whose own name ends in `.sde` is walked as a
  folder and gets no note. With `--diff`, the note is printed for the first `.sde` argument only.
- Only `.aprx`, `.lyrx`, `.mapx` and `.pagx` are walked. A tree of ArcMap `.mxd` or `.lyr` files, or
  `.aptx` templates, prints `no ... documents under <dir>` and exits 0.
- Piping the output to a program that closes early, such as `head`, raises `BrokenPipeError` with a
  traceback and exits 1. An unexpected internal error exits 1 with a traceback, the same code as an
  `UNSUPPORTED` document.
- Redaction is a denylist of key-name fragments, the ten listed above. A secret stored under a name
  with none of them prints as stored. I found no list of the keys Esri writes into a workspace
  connection string, so the list cannot be checked against one. An `Authorization: Bearer ...`
  header written with a colon has no `=` and is not redacted. An unquoted password that holds both a
  `;` and an `=` leaks the part after the `=`, because that part reads as a property of its own.
- Only the workspace string and the resolved path are redacted. The dataset, layer, map and
  connection-type fields print as stored, so a dataset that is itself a URL with a token would show
  the token.
- Python 3.9 is the stated floor. The self-test ran green on 3.9.25, 3.10.20 and 3.13.2 on Windows
  and on 3.12.3 on Linux, each at the same assertion count, under `-W error`.
- Branch coverage is 100 percent only with the two hosts combined: 1204 statements and 280
  branches, measured with `coverage combine` over one run per host. Each host alone is 99 percent.
  `_deny_read`, the self-test helper that makes a folder unlistable, has an `icacls` branch for
  Windows and a `chmod` branch for POSIX. Windows never runs the `chmod` lines (1097-1098) and Linux
  never runs the `icacls` ones (1089-1096). Both hosts print the same assertion count, 280. The
  Linux run used `coverage` 7.16.1's pure-Python tracer, copied from the Windows environment,
  because a stock server `python3` lacks the module.
- The self-test writes temporary fixtures to the system temp folder, and runs `icacls` or `chmod`
  on its own temporary folder. It touches no other data. A scan writes nothing: an audit hook
  over `--json`, `--apply`, `--check-network` and `--diff` runs recorded no write open, no
  subprocess and no socket. Killing the self-test mid-run, for example with `taskkill`, can leave a
  locked `cimscan-test-*` folder in the temp directory.
- A layer name holding characters the console codepage cannot encode prints as `?`. The scan
  finishes; `--json` written to a file keeps the real name.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [agol-relink](https://github.com/uhsear/agol-relink) - the same dead service URL, hunted across Portal content instead of files on disk
- [gdbxray](https://github.com/uhsear/gdbxray) - what the geodatabase at the end of those data sources actually holds
- [stalehost](https://github.com/uhsear/stalehost) - find the same stale host in files cimscan cannot parse, by matching raw bytes
