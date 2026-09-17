# cimscan

Report every data source in a tree of .aprx, .lyrx, .mapx and .pagx files with no ArcGIS licence, and refuse to call an unreadable project a clean one.

An analyst leaves and nobody knows which of the 340 projects on the shared drive still point at the
SDE instance you are decommissioning on Friday. The only way anyone knows to check is opening each
project in Pro and waiting for red exclamation marks, which needs a licensed seat and cannot run on
the file server where the projects live.

So nobody checks. On Monday the planning map book opens broken, with no record of what those layers
used to point at.

```
$ python cimscan.py --self-test
cimscan self-test: no arcpy, no licence, no network
--------------------------------------------------------------------
PASS  a stored password never survives redaction
PASS  a stored portal TOKEN never survives redaction  <-- pinned defect
PASS  a token in a service URL query string is redacted too  <-- pinned defect
PASS  neither stored password blob survives a real .sde connection
...
PASS  an .aprx part is CIM XML and parses  <-- pinned defect
PASS  a .lyrx is bare CIM JSON and parses  <-- pinned defect
PASS  a case-colliding CIMPATH still resolves  <-- pinned defect
PASS  a query connection with no Dataset does not raise  <-- pinned defect
...
PASS  a layer three group layers deep is still found
PASS  all seven sources are reported, none lost to the case collision
PASS  a layer a map and an unreferenced group both hold is reported once  <-- pinned defect
PASS  the layer such a document holds at its root is still reported  <-- pinned defect
PASS  one relative workspace string in two projects is two plan rows  <-- pinned defect
...
PASS  a zip whose parts do not parse is UNSUPPORTED, not clean  <-- pinned defect
PASS  a zero-byte document is UNSUPPORTED, not a project with no layers
PASS  a .lyrx that is valid json but names no CIM type is UNSUPPORTED  <-- pinned defect
PASS  a standalone table the .mapx map holds is reported, not dropped  <-- pinned defect
PASS  the .mapx layer is attributed to the map inside the document  <-- pinned defect
PASS  a .pagx scans OK
PASS  so the document itself is never reported as an unnamed layer  <-- pinned defect
PASS  a layer name the console codepage cannot encode does not kill the scan  <-- pinned defect
...
PASS  an unreadable subdirectory is counted, not walked past  <-- pinned defect
PASS  an unreadable directory reaches exit 1, as UNSUPPORTED does
PASS  --filter drops a layer that does not match
PASS  --apply authorises that same stat, which is all --apply does
PASS  --diff --json emits the same three changes as data
--------------------------------------------------------------------
205 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Standard library only: `zipfile`, `xml.etree.ElementTree`, `json`, `os`,
`argparse`. No `arcpy`, no ArcGIS licence, no install step. It runs on ArcGIS Pro's Python and on a
plain `python3` on a file server.

```
git clone https://github.com/uhsear/cimscan.git
```

## Quick start

```
python cimscan.py --self-test
python cimscan.py \\gis-fs\projects
```

## Usage

Scan a tree, grep it for the server you are retiring, then export the result.

```
python cimscan.py \\gis-fs\projects
python cimscan.py \\gis-fs\projects --filter sde:sqlserver:gisdb01
python cimscan.py \\gis-fs\projects --json > sources.json
python cimscan.py --diff before.lyrx after.lyrx
```

| Flag | Default | What it does |
|---|---|---|
| `root` | none | Folder to walk, or one document. Required unless `--diff` or `--self-test`. |
| `--filter` | none | Report only sources whose workspace, path, dataset or layer name contains this text. |
| `--json` | off | Emit the scan as JSON instead of the text report. |
| `--diff BEFORE AFTER` | none | Compare two layer documents semantically instead of scanning a tree. |
| `--check-network` | off | Stat paths on UNC network shares. |
| `--apply` | off | The same authorisation as `--check-network`. Nothing is ever written. |
| `--self-test` | off | Run the offline assertions and exit. |

The UNC check is off by default because a stat against a server that is already switched off blocks
for the full SMB timeout. Three hundred layers pointed at a decommissioned box turn a scan into an
afternoon. Local paths are always checked; only the network round trip needs the flag.

## What it reports

For every layer and standalone table in every document:

- The map or layout that holds it, and its name.
- The connection type, for example `CIMStandardDataConnection` or `CIMSqlQueryDataConnection`.
- The workspace connection string, with every secret redacted.
- The dataset it asks for, or the query name when a query layer has no dataset.
- Whether the resolved path is on disk right now: `present`, `MISSING`, or `unknown`.

`unknown` is a third verdict on purpose. A feature service and a remote geodatabase have no local
path to check, and calling them `MISSING` would bury the file geodatabases that really are gone.

Sources are then grouped by workspace, because four layers on one `.sde` are one decommissioning
decision and not four. Against the sample projects on this machine:

```
$ python cimscan.py .../Packages/7A7B_NT_6a0a5f/p20/CrimeIncidents.aprx
   present  Scene > robbery_STC_3D             robbery_STC_3D           DATABASE=..\commondata\crimeincidents.gdb
   present  Map > crime                        crime                    DATABASE=..\commondata\crimeincidents.gdb
   unknown  Map > Topographic                  World_Topo_Map           http://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer
   ...

workspaces: 11 source(s) across 4 workspace(s)
      8  present  DATABASE=..\commondata\crimeincidents.gdb  -> C:\...\7A7B_NT_6a0a5f\commondata\crimeincidents.gdb
      1  unknown  http://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer
      ...

1 document(s), 1 OK, 0 UNSUPPORTED, 11 source(s), 0 missing, 0 unreadable directory(s)
```

A `.mapx` and a `.pagx` are reported the same way. This is the self-test fixture, built from the CIM
spec, because no `.mapx` saved by Pro was available here:

```
$ python cimscan.py Zoning.mapx
== Zoning.mapx  [OK]
   MISSING  Zoning Review > Parcels            Parcels                  DATABASE=..\commondata\parcels.gdb
   unknown  Zoning Review > Owner Lookup       GIS.Owners               ENCRYPTED_PASSWORD=***REDACTED***;ENCRYPTED_PASSWORD_UTF8=***REDACTED***;SERVER=gisdb01;...
```

The standalone table is the part that is easy to lose. A `.mapx` holds it in
`standaloneTableDefinitions`, beside the layers and not inside the map. A reader that walks only
`layerDefinitions` drops it, and prints a clean report with one source missing.

A plan row names the resolved path as well as the workspace string. Two projects both saying
`DATABASE=..\commondata\parcels.gdb` mean two different folders, so they stay two rows. Keying the
row on the text alone let a geodatabase that was gone hide behind a row reading `present`.

## What it refuses

It refuses to report an unreadable document as a clean one. A document whose container will not
open, whose parts will not parse, that holds no CIM parts at all, or whose root object names no CIM
type, is marked `UNSUPPORTED`, counted separately, and drives the exit code to 1.

```
== mixed\Truncated.aprx  [UNSUPPORTED]
   ! could not open: File is not a zip file
   (no data sources read)

== mixed\Settings.lyrx  [UNSUPPORTED]
   ! the root object is not a CIM document
   (no data sources read)

== mixed\Zero.lyrx  [UNSUPPORTED]
   ! part Zero.lyrx did not parse: empty CIM part
   (no data sources read)
```

The second one is the quiet case. A file that is valid JSON and is not a CIM document parses without
an error and holds no layers, so a scanner that trusts the parse reports it as a document with
nothing to worry about.

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
`PASSWORD=` or `ENCRYPTED_PASSWORD=` in clear text inside the document. Any key whose name contains
`PASSWORD`, `TOKEN`, `SECRET`, `APIKEY`, `API_KEY` or `CREDENTIAL` is replaced with `***REDACTED***`
before the record is built, so the text report, the `--json` output and the `--diff` output are all
covered by one guard instead of three.

A service workspace is a URL, and a stored token sits in its query string rather than in a `;`
separated property, so properties are split on `&` as well. The key and every other parameter are
kept: only the value goes. Redacting the workspace also means a scan of a shared drive can be
committed or pasted into a ticket, which is the only reason a report like this gets shared.

## The .lyrx diff

A `.lyrx` is a whole JSON document for one layer. The three real ones on this machine are 986 lines
each, and saving the same layer again rewrites its uri, so a textual diff reports a change that means
nothing:

```
$ diff ACLED_2010_2018_Nigeria.lyrx ACLED_2010_2018_Nigeria3.lyrx
4c4
<     "CIMPATH=internal_map/ACLED_2005_2018_Nigeria.json"
---
>     "CIMPATH=internal_map/ACLED_2005_2018_Nigeria3.json"
10c10
<       "uRI" : "CIMPATH=internal_map/ACLED_2005_2018_Nigeria.json",
---
>       "uRI" : "CIMPATH=internal_map/ACLED_2005_2018_Nigeria3.json",

$ python cimscan.py --diff ACLED_2010_2018_Nigeria.lyrx ACLED_2010_2018_Nigeria3.lyrx
no semantic difference: source, definition query, renderer field and connection type all match
```

The reverse case is the one that matters. Repointing that layer at an SDE, widening its definition
query and changing the renderer field produced a 1,972 line textual diff. `--diff` reports the three
things that changed:

```
$ python cimscan.py --diff ACLED_Nigeria.lyrx ACLED_Nigeria_after.lyrx
source repointed: ACLED_2005_2018_Nigeria
   before: DATABASE=..\conflict.gdb|ACLED_2005_2018
   after:  SERVER=gisdb01;INSTANCE=sde:sqlserver:gisdb01;DATABASE=sdeprod;USER=viewer;PASSWORD=***REDACTED***;AUTHENTICATION_MODE=DBMS|GIS.ACLED_2005_2018
definition query changed: ACLED_2005_2018_Nigeria
   before: COUNTRY = 'Nigeria' And YEAR >= 2010
   after:  COUNTRY = 'Nigeria' And YEAR >= 2015
renderer field changed: ACLED_2005_2018_Nigeria
   before: FATALITIES
   after:  EVENTS

3 change(s)
```

The stored password in that repointed connection is redacted here too. A `.lyrx` review is exactly
where a credential would otherwise reach a pull request.

## Exit codes

0 every document parsed and every directory listed, 1 at least one `UNSUPPORTED` or at least one
directory that could not be listed, 2 the scan path could not be read, 64 usage error.

## Why not arcpy

`arcpy.mp.ArcGISProject(path).listBrokenDataSources()` does this properly. It understands every
layer type Pro understands, it follows the format when the format changes, and it is written by the
people who own it. If you have a licensed seat and the projects are reachable from it, use arcpy.

The gap is where the projects live. arcpy needs a checked-out Pro licence on the machine running the
script, and that machine is never the file server holding the shared drive. So the inventory does
not get run, or it gets run once by hand on somebody's workstation against a subset. This reads the
CIM bytes directly, which means it runs on the server, in a scheduled task, or in CI, with nothing
installed. It knows less than arcpy does. It runs where arcpy cannot.

Two format facts cost more time than anything else here, and both are pinned by assertions:

- An `.aprx` is a ZIP archive whose layer documents are CIM **XML**. A `.lyrx` is bare CIM **JSON**
  on disk and is not zipped at all. Code that handles one fails silently on the other. Both
  serialisations describe the same object model, and the member names differ only in case, so
  `cimscan` lower-cases every member name and reads both with one extractor. The case is not
  predictable on either side: the real `.lyrx` files here spell the layer uri `"uRI"`.
- One zip holds `Map/dc792303c26fafd70a72894adc0b189e.xml` and `map/crime.xml`, the same folder
  spelled two ways. Both of the real projects tested here do that, and one map part references
  layers under both spellings. A case-sensitive lookup drops those layers and still prints a
  clean-looking report, which is worse than an error.

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
  about symbology, labelling, pop-ups or scale ranges.
- It never writes to a document and never repairs one. Repointing a broken layer is
  `updateConnectionProperties`, which needs arcpy.
- XML is parsed with the standard library's `ElementTree`. It does not fetch external entities, but
  a deliberately malicious document can still cost memory through nested internal entities. Scan
  documents you own.
- An unreadable directory is reported once, by the path the walk was refused. Its subfolders are
  never reached, so one line can stand for a whole branch of the tree.
- Row counts, field lists and spatial references are out of scope. This answers "what does it point
  at", not "what is in it".
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
