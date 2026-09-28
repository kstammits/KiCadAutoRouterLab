KiCad Command-Line Interface



# KiCad Command-Line Interface

The KiCad Team

Table of Contents

- [1. Introduction to the KiCad Command-Line Interface](#_introduction_to_the_kicad_command_line_interface)
- [2. Footprint commands](#fp)
  - [2.1. Footprint export](#_footprint_export)
  - [2.2. Footprint upgrade](#_footprint_upgrade)
- [3. Jobset commands](#jobset)
- [4. PCB commands](#pcb)
  - [4.1. PCB DRC](#_pcb_drc)
  - [4.2. PCB export: 3D PDF](#_pcb_export_3d_pdf)
  - [4.3. PCB export: BREP (OCCT)](#_pcb_export_brep_occt)
  - [4.4. PCB export: drill file](#_pcb_export_drill_file)
  - [4.5. PCB export: DXF](#_pcb_export_dxf)
  - [4.6. PCB export: GenCAD](#_pcb_export_gencad)
  - [4.7. PCB export: Gerber](#_pcb_export_gerber)
  - [4.8. PCB export: GLB](#_pcb_export_glb)
  - [4.9. PCB export: HPGL](#_pcb_export_hpgl)
  - [4.10. PCB export: IPC-2581](#_pcb_export_ipc_2581)
  - [4.11. PCB export: IPC-D-356](#_pcb_export_ipc_d_356)
  - [4.12. PCB export: ODB++](#_pcb_export_odb)
  - [4.13. PCB export: PDF](#_pcb_export_pdf)
  - [4.14. PCB export: PLY](#_pcb_export_ply)
  - [4.15. PCB export: position file](#_pcb_export_position_file)
  - [4.16. PCB export: PostScript](#_pcb_export_postscript)
  - [4.17. PCB export: statistics](#_pcb_export_statistics)
  - [4.18. PCB export: STEP](#_pcb_export_step)
  - [4.19. PCB export: STL](#_pcb_export_stl)
  - [4.20. PCB export: STEPZ](#_pcb_export_stepz)
  - [4.21. PCB export: U3D](#_pcb_export_u3d)
  - [4.22. PCB export: SVG](#_pcb_export_svg)
  - [4.23. PCB export: VRML](#_pcb_export_vrml)
  - [4.24. PCB export: XAO](#_pcb_export_xao)
  - [4.25. PCB import](#_pcb_import)
  - [4.26. PCB render](#_pcb_render)
  - [4.27. PCB upgrade](#_pcb_upgrade)
- [5. Schematic commands](#schematic)
  - [5.1. Schematic ERC](#_schematic_erc)
  - [5.2. Schematic export: bill of materials](#_schematic_export_bill_of_materials)
  - [5.3. Schematic export: DXF](#_schematic_export_dxf)
  - [5.4. Schematic export: HPGL](#_schematic_export_hpgl)
  - [5.5. Schematic export: netlist](#_schematic_export_netlist)
  - [5.6. Schematic export: PDF](#_schematic_export_pdf)
  - [5.7. Schematic export: PostScript](#_schematic_export_postscript)
  - [5.8. Schematic export: bill of materials (legacy BOM scripts)](#_schematic_export_bill_of_materials_legacy_bom_scripts)
  - [5.9. Schematic export: SVG](#_schematic_export_svg)
  - [5.10. Schematic upgrade](#_schematic_upgrade)
- [6. Symbol commands](#symbol)
  - [6.1. Symbol export](#_symbol_export)
  - [6.2. Symbol upgrade](#_symbol_upgrade)
- [7. Version commands](#version)

*KiCad 10.0 Reference Manual*

**Copyright**

This document is Copyright The KiCad Documentation Contributors.
You may distribute it and/or modify it under the terms of either
the GNU General Public License (<http://www.gnu.org/licenses/gpl.html>),
version 3 or later, or the Creative Commons Attribution License
(<http://creativecommons.org/licenses/by/3.0/>), version 3.0 or later.

All trademarks within this guide belong to their legitimate owners.

**Contributors**

Graham Keeth

**Feedback**

The KiCad project welcomes feedback, bug reports, and suggestions related to the software or its
documentation. For more information on how to submit feedback or report an issue, please see the
instructions at <https://www.kicad.org/help/report-an-issue/>

**Software and Documentation Version**

This user manual is based on KiCad 10.0.6. Functionality and
appearance may be different in other versions of KiCad.

Documentation revision: `d350b28a`.

## 1. Introduction to the KiCad Command-Line Interface

KiCad provides a command-line interface, which is available by running the
`kicad-cli` binary. With the command-line interface, you can perform a number of
actions on schematics, PCBs, symbols, and footprints in an automated fashion,
such as plotting Gerber files from a PCB design or upgrading a symbol library
from a legacy file format to a modern format.

|  |  |
| --- | --- |
| Note | On macOS, the `kicad-cli` executable is located at `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`. |

The `kicad-cli` command has 6 subcommands: `fp`, `jobset`, `pcb`, `sch`, `sym`,
and `version`. Each subcommand may have its own subcommands and arguments. For
example, to export Gerber files from a PCB you could run
`kicad-cli pcb export gerbers example.kicad_pcb`.

You can add the `--help` or `-h` flag to see information about each
subcommand. For example, running `kicad-cli pcb -h` prints usage information
about the `pcb` subcommand, and `kicad-cli pcb export gerbers -h` prints usage
information specifically for the `pcb export gerbers` subcommand.

## 2. Footprint commands

The `fp` subcommand exports footprints to another format or upgrades the
footprint libraries to the current version of the KiCad footprint file format.

### 2.1. Footprint export

The `fp export svg` command exports one or more footprints from the specified
library into SVG files.

Usage: `kicad-cli fp export svg [--help] [--output OUTPUT_DIR] [--layers LAYER_LIST] [--define-var KEY=VALUE]…​ [--theme VAR] [--footprint FOOTPRINT_NAME] [--sketch-pads-on-fab-layers] [--hide-DNP-footprints-on-fab-layers] [--sketch-DNP-footprints-on-fab-layers] [--crossout-DNP-footprints-on-fab-layers] [--black-and-white] INPUT_FILE_OR_DIR`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE_OR_DIR` | Footprint (`.kicad_mod`) or footprint library directory (`.pretty`) to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the footprint SVG export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder for the exported files. One file is output for each layer of each footprint in the library. When `--output` is not used, the files are exported to the current directory. |
| `-l <layer list>`, `--layers <layer list>` | A comma-separated list of layer names to export from the footprint, such as `F.Cu,B.Cu`. If no layers are given, all layers are exported. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the footprint editor’s currently selected theme is used. |
| `--fp <footprint>`, `--footprint <footprint>` | The name of the specific footprint to export from the library. When this argument is not used, all footprints in the library are exported. |
| `--sp`, `--sketch-pads-on-fab-layers` | Draw pad outlines and their numbers on front and back fab layers. |
| `--hdnp`, `--hide-DNP-footprints-on-fab-layers` | Don’t plot text and graphics of DNP footprints on fab layers. |
| `--sdnp`, `--sketch-DNP-footprints-on-fab-layers` | Plot graphics of DNP footprints in sketch mode on fab layers. |
| `--cdnp`, `--crossout-DNP-footprints-on-fab-layers` | Plot an "X" over the courtyard of DNP footprints on fab layers, and strikeout their reference designators. |
| `--black-and-white` | Export footprints in black and white. |

### 2.2. Footprint upgrade

The `fp upgrade` command converts the specified footprint library from a
legacy KiCad footprint format or a non-KiCad footprint format to the native
format for the current version of KiCad. If the input library is already in the
current file format, no action is taken.

Supported input footprint formats are:

- KiCad footprint library (`.pretty` folder with `.kicad_mod` files)
- KiCad (pre-5.0) footprint library (`.mod`, `.emp`)
- Altium footprint library (`.PcbLib`)
- Altium integrated library (`.IntLib`)
- CADSTAR PCB archive (`.cpa`)
- EAGLE XML library (`.lbr`)
- EasyEDA (JLCEDA) Std file (`.json`)
- EasyEDA (JLCEDA) Pro file (`.elibz`, `.epro`, `.zip`)
- GEDA/PCB library (folder with `.fp` files)

Usage: `kicad-cli fp upgrade [--help] [--output OUTPUT_DIR] [--force] INPUT_FILE_OR_DIR`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE_OR_DIR` | Footprint or footprint library directory to upgrade. For KiCad format footprint libraries, this can be a footprint (`.kicad_mod` file) or a footprint library (`.pretty` directory containing `.kicad_mod` files). |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the upgrade command. |
| `-o <output dir>`, `--output <output dir>` | The output directory for the upgraded footprints. When `--output` is not used, the upgraded footprints are saved over the original footprints. |
| `--force` | Re-save the input library even if it is already in the current file format. |

## 3. Jobset commands

The `jobset run` command runs a predefined [jobset](../kicad/kicad.html#jobsets).

Usage: `kicad-cli jobset run [--help] [--stop-on-error] [--file JOB_FILE] [--output OUTPUT] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Project file to use with the jobset. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the jobset command. |
| `--stop-on-error` | As jobs are executed in sequence, stop running after a job fails. If not given, jobs will continue executing after any job fails. |
| `-f <jobset file>`, `--file <jobset file>` | The jobset file (`.kicad_jobset`) to run. |
| `--output <destination description or ID>` | The jobset destination to generate. If no destination is specified, all destinations will be generated.  The destination is specified by its description or by its unique ID. The specified description must be unique; if the jobset contains more than one destination with the given description, none of them will be run.  IDs are inherently unique and can be used to refer to a destination even if the destination’s description is not unique. The ID for each destination is printed by the `jobset run` command when `--output` is not used. It can also be found in the `.kicad_jobset` file under the destination’s `id` key. |

## 4. PCB commands

The `pcb` command runs a design rule check or exports a board to various other
file formats, including fabrication and 3D files.

### 4.1. PCB DRC

The `pcb drc` command runs a design rule check on a board and generates a report.

Usage: `kicad-cli pcb drc [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--format FORMAT] [--all-track-errors] [--schematic-parity] [--units UNITS] [--severity-all] [--severity-error] [--severity-warning] [--severity-exclusions] [--exit-code-violations] [--refill-zones] [--save-board] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to run DRC on. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the DRC command. |
| `-o <output filename>`, `--output <output filename>` | Output filename for the generated DRC report. When `--output` is not used, the output filename will be the same as the input file, with the `.rpt` or `.json` file extension, depending on the selected format. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--format <format>` | Report file format. Options are `report` (default) or `json`. |
| `--all-track-errors` | Report all errors for each track. |
| `--schematic-parity` | Test for parity between PCB and schematic. |
| `--units <unit>` | Units to use in the report. Options are `mm` (default), `in`, or `mils`. |
| `--severity-all` | Report all DRC violations. This is equivalent to using all of the other DRC severity options. |
| `--severity-error` | Report all error-level DRC violations. This can be combined with the other DRC severity options. |
| `--severity-warning` | Report all warning-level DRC violations. This can be combined with the other DRC severity options. |
| `--severity-exclusions` | Report all excluded DRC violations. This can be combined with the other DRC severity options. |
| `--exit-code-violations` | Return an exit code depending on whether or not DRC violations exist. The exit code is 0 if no violations are found, and 5 if any violations are found. |
| `--refill-zones` | Refill all zones before running DRC. The board will not be saved after refilling zones unless `--save-board` is also used. |
| `--save-board` | Save the board after running DRC. The board will not be saved unless `--refill-zones` is also used. |

### 4.2. PCB export: 3D PDF

The `pcb export 3dpdf` command exports a board design to a PDF file containing an embedded 3D model of the board.

Usage: `kicad-cli pcb export 3dpdf [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the 3D PDF export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.pdf` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.3. PCB export: BREP (OCCT)

The `pcb export brep` command exports a board design to a BREP (OCCT-native
boundary representation) 3D model file.

Usage: `kicad-cli pcb export brep [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the BREP export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.brep` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.4. PCB export: drill file

The `pcb export drill` command exports a drill file from a board.

Usage: `kicad-cli pcb export drill [--help] [--output OUTPUT_DIR] [--format FORMAT] [--drill-origin DRILL_ORIGIN] [--excellon-zeros-format ZEROS_FORMAT] [--excellon-oval-format OVAL_FORMAT] [--excellon-units UNITS] [--excellon-mirror-y] [--excellon-min-header] [--excellon-separate-th] [--generate-map] [--generate-report] [--report-path REPORT_FILE] [--generate-tenting] [--map-format MAP_FORMAT] [--gerber-precision VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the drill file export command. |
| `-o <output dir>`, `--output <output dir>` | The output directory for the drill file(s). When `--output` is not used, the drill file(s) are saved in the current directory. |
| `--format <format>` | The drill file format. Options are `excellon` (default) or `gerber`. |
| `--drill-origin <origin>` | The coordinate origin for the drill file. Options are `absolute` (default) to use the board’s absolute origin or `plot` to use the board’s drill/placement origin. |
| `--excellon-zeros-format <format>` | The zeros format for the drill file. Options are `decimal` (default), `suppressleading`, `suppresstrailing`, or `keep`. Only applies to Excellon format drill files. |
| `--excellon-oval-format <format>` | Control the oval holes drill mode. Options are `route` and `alternate` (default). Only applies to Excellon format drill files. |
| `-u <units>`, `--excellon-units <units>` | The units for the drill file. Options are `mm` (default) or `in`. Only applies to Excellon format drill files. |
| `--excellon-mirror-y` | Mirror the drill file in the Y direction. Only applies to Excellon format drill files. |
| `--excellon-min-header` | Use a minimal header in the drill file. Only applies to Excellon format drill files. |
| `--excellon-separate-th` | Generate separate drill files for plated and non-plated through holes. Only applies to Excellon format drill files. |
| `--generate-map` | Generate a map file in addition to the drill file. |
| `--generate-report` | Generate a report file listing all drill hits. |
| `--report-path <report filename>` | The output filename for the drill report file. When `--report-path` is not used, the report filename will be the same as the input file, with the `-drill.rpt` suffix and file extension. |
| `--generate-tenting` | Generate separate drill files for tented drill hits. Only applies to Gerber X2 format drill files. |
| `--map-format <format>` | The map file format. Options are `pdf` (default), `gerberx2`, `ps`, `dxf`, or `svg`. |
| `--gerber-precision <precision>` | The precision (number of digits) for the drill file. Valid options are `5` or `6` (default). Only applies to Gerber format drill files. |

### 4.5. PCB export: DXF

The `pcb export dxf` command exports a board design to a DXF file.

Usage: `kicad-cli pcb export dxf [--help] [--output OUTPUT_DIR] [--layers LAYER_LIST] [--common-layers COMMON_LAYER_LIST] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--exclude-refdes] [--exclude-value] [--sketch-pads-on-fab-layers] [--hide-DNP-footprints-on-fab-layers] [--sketch-DNP-footprints-on-fab-layers] [--crossout-DNP-footprints-on-fab-layers] [--subtract-soldermask] [--use-contours] [--use-drill-origin] [--include-border-title] [--output-units UNITS] [--drill-shape-opt VAR] [--mode-single] [--mode-multi] [--scale SCALE] [--check-zones] [--variant VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the DXF export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder or filename for the exported files. When `--mode-single` is used, this is the output filename. If `--output` is not used, the output filename will be the same as the input file, with the `.pdf` file extension. When `--mode-multi` is used, this is the output directory. If `--output` is not used, the files are exported to the current directory. |
| `-l <layer list>`, `--layers <layer list>` | A comma-separated list of layer names to export from the footprint, such as `F.Cu,B.Cu`. At least one layer must be given. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--cl <layer list>`, `--common-layers <layer list>` | A comma-separated list of layer names to plot on all layers, such as `F.Cu,B.Cu`. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the board file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--erd`, `--exclude-refdes` | Exclude footprint reference designators from plot. |
| `--ev`, `--exclude-value` | Exclude footprint values from plot. |
| `--sp`, `--sketch-pads-on-fab-layers` | Draw pad outlines and their numbers on front and back fab layers. |
| `--hdnp`, `--hide-DNP-footprints-on-fab-layers` | Don’t plot text and graphics of DNP footprints on fab layers. |
| `--sdnp`, `--sketch-DNP-footprints-on-fab-layers` | Plot graphics of DNP footprints in sketch mode on fab layers. |
| `--cdnp`, `--crossout-DNP-footprints-on-fab-layers` | Plot an "X" over the courtyard of DNP footprints on fab layers, and strikeout their reference designators. |
| `--subtract-soldermask` | Remove silkscreen from areas without soldermask. |
| `--uc`, `--use-contours` | Plot graphic items using their contours. |
| `--udo`, `--use-drill-origin` | Plot using the drill/place file origin. |
| `--ibt`, `--include-border-title` | Include sheet border and title block in plot. |
| `--ou <unit>`, `--output-units <unit>` | Output units. Options are `mm` or `in` (default). |
| `--drill-shape-opt <shape>` | The shape of drill marks in the plot. Options are `0` for no drill marks, `1` for small marks, or `2` for actual size marks (default). |
| `--mode-single` | Generates a single file with the output arg path acting as the complete directory and filename path. `COMMON_LAYER_LIST` does not function in this mode. Instead `LAYER_LIST` controls all layers plotted. |
| `--mode-multi` | Plot the layers to one or more DXF files, with each file representing a single layer from `LAYER_LIST`. The output path specifies the directory in which the files will be written. |
| `--scale <scale>` | A scaling factor to use for plotting the PCB. The border and title block are not scaled. A scale factor of 0 autoscales the plot. |
| `--check-zones` | Check zone fills and refill zones, if required, prior to export. Any zone fill updates are not saved in the board file. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |

### 4.6. PCB export: GenCAD

The `pcb export gencad` command exports a board design to a GenCAD file.

Usage: `kicad-cli pcb export gencad [--help] [--output OUTPUT_DIR] [--define-var KEY=VALUE]…​ [--flip-bottom-pads] [--unique-pins] [--unique-footprints] [--use-drill-origin] [--store-origin-coord] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the DXF export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.cad` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--flip-bottom-pads` | Flip bottom footprint padstacks. |
| `--unique-pins` | Generate unique pin names. |
| `--unique-footprints` | Generate a new shape for each footprint instance (do not reuse shapes). |
| `--use-drill-origin` | Use drill/place file origin as origin. |
| `--store-origin-coord` | Save the origin coordinates in the file. |

### 4.7. PCB export: Gerber

The `pcb export gerbers` command exports a board design to Gerber files, with
one layer per file.

Usage: `kicad-cli pcb export gerbers [--help] [--output OUTPUT_DIR] [--layers LAYER_LIST] [--common-layers COMMON_LAYER_LIST] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--exclude-refdes] [--exclude-value] [--include-border-title] [--sketch-pads-on-fab-layers] [--hide-DNP-footprints-on-fab-layers] [--sketch-DNP-footprints-on-fab-layers] [--crossout-DNP-footprints-on-fab-layers] [--no-x2] [--no-netlist] [--subtract-soldermask] [--disable-aperture-macros] [--use-drill-file-origin] [--precision PRECISION] [--no-protel-ext] [--check-zones] [--variant VAR] [--board-plot-params] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the Gerber export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder for the exported files. One file is output for each layer. When `--output` is not used, the files are exported to the current directory. |
| `-l <layer list>`, `--layers <layer list>` | A comma-separated list of layer names to plot from the board, such as `F.Cu,B.Cu`. If this argument is not used, all layers will be plotted. A seperate output file is plotted for each layer. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--cl <layer list>`, `--common-layers <layer list>` | A comma-separated list of layer names to plot on all layers, such as `F.Cu,B.Cu`. Each layer specified is included in every output file. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the board file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--erd`, `--exclude-refdes` | Exclude footprint reference designators from plot. |
| `--ev`, `--exclude-value` | Exclude footprint values from plot. |
| `--ibt`, `--include-border-title` | Include the sheet border and title block. |
| `--sp`, `--sketch-pads-on-fab-layers` | Draw pad outlines and their numbers on front and back fab layers. |
| `--hdnp`, `--hide-DNP-footprints-on-fab-layers` | Don’t plot text and graphics of DNP footprints on fab layers. |
| `--sdnp`, `--sketch-DNP-footprints-on-fab-layers` | Plot graphics of DNP footprints in sketch mode on fab layers. |
| `--cdnp`, `--crossout-DNP-footprints-on-fab-layers` | Plot an "X" over the courtyard of DNP footprints on fab layers, and strikeout their reference designators. |
| `--no-x2` | Do not use the extended X2 format. |
| `--no-netlist` | Do not include netlist attributes. |
| `--subtract-soldermask` | Remove silkscreen from areas without soldermask. |
| `--disable-aperture-macros` | Disable aperture macros. |
| `--use-drill-file-origin` | Use drill/place file origin instead of absolute origin. |
| `--precision <precision>` | The precision (number of digits) for the Gerber files. Valid options are `5` or `6` (default). |
| `--no-protel-ext` | Use `.gbr` file extension instead of Protel file extensions (`.gbl`, `.gtl`, etc.). |
| `--check-zones` | Check zone fills and refill zones, if required, prior to export. Any zone fill updates are not saved in the board file. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--board-plot-params` | Use the Gerber plot settings already configured in the board file. |

### 4.8. PCB export: GLB

The `pcb export glb` command exports a board design to a GLB (binary glTF) 3D
model file.

Usage: `kicad-cli pcb export glb [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the GLB export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.glb` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.9. PCB export: HPGL

|  |  |
| --- | --- |
| Note | `kicad-cli pcb export hpgl` is not functional in KiCad 10.0. |

The `pcb export hpgl` command is not functional in KiCad 10.0 as KiCad no longer supports HPGL output.
In previous versions of KiCad it exported a board design to an HPGL file.
It is included as a non-functional command for compatibility reasons.
It will be removed in a future version of KiCad.

Usage: `kicad-cli pcb export hpgl [--help] [--output OUTPUT_DIR] INPUT_FILE`

### 4.10. PCB export: IPC-2581

The `pcb export ipc2581` command exports a board design in IPC-2581 format.

Usage: `kicad-cli pcb export ipc2581 [--help] [--output OUTPUT_FILE] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--precision PRECISION] [--compress] [--version VAR] [--units VAR] [--bom-col-int-id FIELD_NAME] [--bom-col-mfg-pn FIELD_NAME] [--bom-col-mfg FIELD_NAME] [--bom-col-dist-pn FIELD_NAME] [--bom-col-dist FIELD_NAME] [--bom-rev REVISION] [--variant VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the IPC-2581 export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.xml` file extension. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the board file. |
| `-D <variable name>=<value>`, `--define-var <variable name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--precision <precision>` | The precision (number of digits after the decimal separator) for the exported file. The default is `6`. |
| `--compress` | Compress output file as a ZIP file. |
| `--version <IPC-2581 standard version>` | IPC-2581 standard version to use. Options are `B` or `C` (default). |
| `--units <unit>` | Units to use in export. Options are `mm` (default) or `in`. |
| `--bom-col-int-id <field>` | Name of the part field to use for the Bill of Materials Internal ID column. This can be any footprint field, or blank to omit this column. |
| `--bom-col-mfg-pn <field>` | Name of the part field to use for the Bill of Materials Manufacturer Part Number column. This can be any footprint field, or blank to omit this column. |
| `--bom-col-mfg <field>` | Name of the part field to use for the Bill of Materials Manufacturer column. This can be any footprint field, or blank to omit this column. |
| `--bom-col-dist-pn <field>` | Name of the part field to use for the Bill of Materials Distributor Part Number column. This can be any footprint field, or blank to omit this column. |
| `--bom-col-dist <field>` | Name of the part field to use for the Bill of Materials Distributor column. This can be any footprint field, or blank to omit this column. |
| `--bom-rev <revision>` | Revision to use for the Bill of Materials Revision field. If not given, the `Revision` field from the schematic’s root sheet is used instead. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |

### 4.11. PCB export: IPC-D-356

The `pcb export ipcd356` command generates an IPC-D-356 netlist from the board design.

Usage: `kicad-cli pcb export ipcd356 [--help] [--output OUTPUT_FILE] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the IPC-D-356 export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.d356` file extension. |

### 4.12. PCB export: ODB++

The `pcb export odb` command exports a board design in ODB++ format.

Usage: `kicad-cli pcb export odb [--help] [--output OUTPUT_FILE] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--precision PRECISION] [--compression VAR] [--units VAR] [--variant VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the ODB++ export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename, or folder name if no compression is used. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the board file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--precision <precision>` | The precision (number of digits after the decimal separator) for the exported file. The default is `2`. |
| `--compression <mode>` | Compression mode. Options are `none`, `zip` (default), or `tgz`. |
| `--units <unit>` | Units to use in the output file. Options are `mm` (default) or `in`. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |

### 4.13. PCB export: PDF

The `pcb export pdf` command exports a board design to a PDF file. Each layer can be plotted as its own file or as a sheet within a single file.

Usage: `kicad-cli pcb export pdf [--help] [--output OUTPUT_DIR] [--layers LAYER_LIST] [--common-layers COMMON_LAYER_LIST] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--mirror] [--exclude-refdes] [--exclude-value] [--include-border-title] [--subtract-soldermask] [--sketch-pads-on-fab-layers] [--hide-DNP-footprints-on-fab-layers] [--sketch-DNP-footprints-on-fab-layers] [--crossout-DNP-footprints-on-fab-layers] [--negative] [--black-and-white] [--theme THEME_NAME] [--drill-shape-opt VAR] [--mode-single] [--mode-separate] [--mode-multipage] [--scale SCALE] [--bg-color COLOR] [--check-zones] [--variant VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the PDF export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder or filename for the exported files. When `--mode-single` or `--mode-multipage` is used, this is the output filename. If this argument is not used, the output filename will be the same as the input file, with the `.pdf` file extension. When `--mode-separate` is used, this is the output directory. If `--output` is not used, the files are exported to the current directory. |
| `-l <layer list>`, `--layers <layer list>` | A comma-separated list of layer names to export from the board, such as `F.Cu,B.Cu`. At least one layer must be given. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--cl <layer list>`, `--common-layers <layer list>` | A comma-separated list of layer names to plot on all layers, such as `F.Cu,B.Cu`. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the board file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-m`, `--mirror` | Mirror the board. This can be useful for showing bottom layers. |
| `--erd`, `--exclude-refdes` | Exclude footprint reference designators from plot. |
| `--ev`, `--exclude-value` | Exclude footprint values from plot. |
| `--ibt`, `--include-border-title` | Include the sheet border and title block. |
| `--subtract-soldermask` | Remove silkscreen from areas without soldermask. |
| `--sp`, `--sketch-pads-on-fab-layers` | Draw pad outlines and their numbers on front and back fab layers. |
| `--hdnp`, `--hide-DNP-footprints-on-fab-layers` | Don’t plot text and graphics of DNP footprints on fab layers. |
| `--sdnp`, `--sketch-DNP-footprints-on-fab-layers` | Plot graphics of DNP footprints in sketch mode on fab layers. |
| `--cdnp`, `--crossout-DNP-footprints-on-fab-layers` | Plot an "X" over the courtyard of DNP footprints on fab layers, and strikeout their reference designators. |
| `-n`, `--negative` | Plot in negative. |
| `--black-and-white` | Plot in black and white. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the board editor’s currently selected theme is used. |
| `--drill-shape-opt` | The shape of drill marks in the plot. Options are `0` for no drill marks, `1` for small marks, or `2` for actual size marks (default). |
| `--mode-single` | Generates a single file with the output arg path acting as the complete directory and filename path. `COMMON_LAYER_LIST` does not function in this mode. Instead `LAYER_LIST` controls all layers plotted. All specified layers are plotted on a single page. |
| `--mode-separate` | Plot the layers to one or more PDF files, with each file representing a single layer from `LAYER_LIST`. The output path specifies the directory in which the files will be written. |
| `--mode-multipage` | Plot the layers to a single PDF file with multiple pages, with each page representing a single layer from `LAYER_LIST`. The output path specifies the complete directory and filename path. |
| `--scale <scale>` | A scaling factor to use for plotting the PCB. The border and title block are not scaled. A scale factor of 0 autoscales the plot. |
| `--bg-color <color>` | A background color for the plot. The format can be hex (`#rrggbb` or `#rrggbbaa`) or CSS (`rgb(r,g,b)` or `rgba(r,g,b,a)`). |
| `--check-zones` | Check zone fills and refill zones, if required, prior to export. Any zone fill updates are not saved in the board file. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |

### 4.14. PCB export: PLY

The `pcb export ply` command exports a board design to a PLY 3D model file.

Usage: `kicad-cli pcb export ply [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the PLY export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.ply` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.15. PCB export: position file

The `pcb export pos` command exports a position file from a board design.

Usage: `kicad-cli pcb export pos [--help] [--output OUTPUT_FILE] [--side VAR] [--format FORMAT] [--units UNITS] [--bottom-negate-x] [--use-drill-file-origin] [--smd-only] [--exclude-fp-th] [--exclude-dnp] [--gerber-board-edge] [--variant VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the position file export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.pos` file extension. |
| `--side <side>` | The side of the board to export. Options are `front`, `back`, or `both` (default). Gerber format does not support `both`. |
| `--format <format>` | The position file format. Options are `ascii` (default), `csv`, or `gerber`. |
| `--units <unit>` | Units for position file. Options are `in` (default) or `mm`. This option has no effect for Gerber format. |
| `--bottom-negate-x` | Use negative X coordinates for footprints on the bottom layer. This option has no effect for Gerber format. |
| `--use-drill-file-origin` | Use drill/place file origin instead of absolute origin. This option has no effect for Gerber format. |
| `--smd-only` | Include only surface-mount components. This option has no effect for Gerber format. |
| `--exclude-fp-th` | Exclude all footprints with through-hole pads. This option has no effect for Gerber format. |
| `--exclude-dnp` | Exclude all footprints with "Do not populate" attribute. |
| `--gerber-board-edge` | Include board edge layer in export (Gerber format only). |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |

### 4.16. PCB export: PostScript

The `pcb export ps` command exports a board design to a PostScript file.

Usage: `kicad-cli pcb export ps [--help] [--output OUTPUT_DIR] [--layers LAYER_LIST] [--common-layers COMMON_LAYER_LIST] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--mirror] [--exclude-refdes] [--exclude-value] [--include-border-title] [--subtract-soldermask] [--sketch-pads-on-fab-layers] [--hide-DNP-footprints-on-fab-layers] [--sketch-DNP-footprints-on-fab-layers] [--crossout-DNP-footprints-on-fab-layers] [--negative] [--black-and-white] [--theme THEME_NAME] [--drill-shape-opt VAR] [--mode-single] [--mode-multi] [--track-width-correction TRACK_COR] [--x-scale-factor X_SCALE] [--y-scale-factor Y_SCALE] [--force-a4] [--scale SCALE] [--check-zones] [--variant VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the PS export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder or filename for the exported files. When `--mode-single` is used, this is the output filename. If `--output` is not used, the output filename will be the same as the input file, with the `.ps` file extension. When `--mode-multi` is used, this is the output directory. If `--output` is not used, the files are exported to the current directory. |
| `-l <layer list>`, `--layers <layer list>` | A comma-separated list of layer names to export from the board, such as `F.Cu,B.Cu`. At least one layer must be given. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--cl <layer list>`, `--common-layers <layer list>` | A comma-separated list of layer names to plot on all layers, such as `F.Cu,B.Cu`. Each layer specified is included in every output file. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the board file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-m`, `--mirror` | Mirror the board. This can be useful for showing bottom layers. |
| `--erd`, `--exclude-refdes` | Exclude footprint reference designators from plot. |
| `--ev`, `--exclude-value` | Exclude footprint values from plot. |
| `--ibt`, `--include-border-title` | Include the sheet border and title block. |
| `--subtract-soldermask` | Remove silkscreen from areas without soldermask. |
| `--sp`, `--sketch-pads-on-fab-layers` | Draw pad outlines and their numbers on front and back fab layers. |
| `--hdnp`, `--hide-DNP-footprints-on-fab-layers` | Don’t plot text and graphics of DNP footprints on fab layers. |
| `--sdnp`, `--sketch-DNP-footprints-on-fab-layers` | Plot graphics of DNP footprints in sketch mode on fab layers. |
| `--cdnp`, `--crossout-DNP-footprints-on-fab-layers` | Plot an "X" over the courtyard of DNP footprints on fab layers, and strikeout their reference designators. |
| `-n`, `--negative` | Plot in negative. |
| `--black-and-white` | Plot in black and white. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the board editor’s currently selected theme is used. |
| `--drill-shape-opt` | The shape of drill marks in the plot. Options are `0` for no drill marks, `1` for small marks, or `2` for actual size marks (default). |
| `--mode-single` | Generates a single file with the output arg path acting as the complete directory and filename path. `COMMON_LAYER_LIST` does not function in this mode. Instead `LAYER_LIST` controls all layers plotted. |
| `--mode-multi` | Plot the layers to one or more PS files, with each file representing a single layer from `LAYER_LIST`. The output path specifies the directory in which the files will be written. |
| `-C`, `--track-width-correction` | A global correction, in millimeters, that is added to the size of tracks, vias, and pads when plotted. This correction can be used to correct for errors in the PostScript output device to achieve an exact-scale output. |
| `-X`, `--x-scale-factor` | X scale adjust for exact scale. |
| `-Y`, `--y-scale-factor` | Y scale adjust for exact scale. |
| `-A`, `--force-a4` | Force A4 paper size. |
| `--scale <scale>` | A scaling factor to use for plotting the PCB. The border and title block are not scaled. A scale factor of 0 autoscales the plot. |
| `--check-zones` | Check zone fills and refill zones, if required, prior to export. Any zone fill updates are not saved in the board file. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |

### 4.17. PCB export: statistics

The `pcb export stats` command exports a report of statistics about the board design.

Usage: `kicad-cli pcb export stats [--help] [--output OUTPUT_FILE] [--format FORMAT] [--units UNITS] [--exclude-footprints-without-pads] [--subtract-holes-from-board] [--subtract-holes-from-copper] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export statistics from. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the statistics command. |
| `-o <output filename>`, `--output <output filename>` | Output filename for the generated statistics report. When `--output` is not used, the output filename will be the same as the input file, with a `_statistics` suffix and the `.rpt` or `.json` file extension, depending on the selected format. |
| `--format <format>` | Report file format. Options are `report` (default) or `json`. |
| `--units <unit>` | Units to use in the report. Options are `mm` (default) or `in`. |
| `--exclude-footprints-without-pads` | Exclude footprints that do not contain any pads from component counts. |
| `--subtract-holes-from-board` | Subtract the area of holes from the total board area. |
| `--subtract-holes-from-copper` | Subtract the area of holes from the total copper area. |

### 4.18. PCB export: STEP

The `pcb export step` command exports a board design to a STEP file.

Usage: `kicad-cli pcb export step [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--no-optimize-step] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the STEP file export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.step` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--no-optimize-step` | Do not optimize STEP file. This enables writing parametric curves, which reduces file sizes and write/read times, but may reduce compatibility with other software. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.19. PCB export: STL

The `pcb export stl` command exports a board design to an STL 3D model file.

Usage: `kicad-cli pcb export stl [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the STL export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.stl` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.20. PCB export: STEPZ

The `pcb export stpz` command exports a board design to a STEPZ (GZIP-compressed STEP) file.

Usage: `kicad-cli pcb export stpz [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--no-optimize-step] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the STEPZ file export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.stpz` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--no-optimize-step` | Do not optimize STEP file. This enables writing parametric curves, which reduces file sizes and write/read times, but may reduce compatibility with other software. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.21. PCB export: U3D

The `pcb export u3d` command exports a board design to a PDF file containing an embedded 3D model of the board.

Usage: `kicad-cli pcb export u3d [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the 3D PDF export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.pdf` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.22. PCB export: SVG

The `pcb export svg` command exports a board design to an SVG file.

Usage: `kicad-cli pcb export svg [--help] [--output OUTPUT_DIR] [--layers LAYER_LIST] [--common-layers COMMON_LAYER_LIST] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--subtract-soldermask] [--mirror] [--theme THEME_NAME] [--negative] [--black-and-white] [--sketch-pads-on-fab-layers] [--hide-DNP-footprints-on-fab-layers] [--sketch-DNP-footprints-on-fab-layers] [--crossout-DNP-footprints-on-fab-layers] [--page-size-mode MODE] [--fit-page-to-board] [--exclude-drawing-sheet] [--drill-shape-opt SHAPE_OPTION] [--mode-single] [--mode-multi] [--scale SCALE] [--check-zones] [--variant VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the SVG file export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder or filename for the exported files. When `--mode-single` is used, this is the output filename. If `--output` is not used, the output filename will be the same as the input file, with the `.pdf` file extension. When `--mode-multi` is used, this is the output directory. If `--output` is not used, the files are exported to the current directory. |
| `-l <layer list>`, `--layers <layer list>` | A comma-separated list of layer names to export from the board, such as `F.Cu,B.Cu`. At least one layer must be given. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--cl <layer list>`, `--common-layers <layer list>` | A comma-separated list of layer names to plot on all layers, such as `F.Cu,B.Cu`. Layer names can be specified as canonical layer names (`F.Cu`, `In.1`, `F.Fab`, etc.) or as user-defined (custom) layer names, but user-defined layer names are matched first. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the board file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--subtract-soldermask` | Remove silkscreen from areas without soldermask. |
| `-m`, `--mirror` | Mirror the board. This can be useful for showing bottom layers. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the board editor’s currently selected theme is used. |
| `-n`, `--negative` | Plot in negative. |
| `--black-and-white` | Plot in black and white. |
| `--sp`, `--sketch-pads-on-fab-layers` | Draw pad outlines and their numbers on front and back fab layers. |
| `--hdnp`, `--hide-DNP-footprints-on-fab-layers` | Don’t plot text and graphics of DNP footprints on fab layers. |
| `--sdnp`, `--sketch-DNP-footprints-on-fab-layers` | Plot graphics of DNP footprints in sketch mode on fab layers. |
| `--cdnp`, `--crossout-DNP-footprints-on-fab-layers` | Plot an "X" over the courtyard of DNP footprints on fab layers, and strikeout their reference designators. |
| `--page-size-mode <mode>` | Set page sizing mode. Options are `0` (default), `1`, or `2`. `0` sets the output page size to fit the entire sheet, including drawing sheet frame and title block. `1` sets the output page size to match the current page size. `2` sets the output page size to the size of the board itself. |
| `--fit-page-to-board` | Set the SVG size to match the board outline. This is equivalent to `--page-size-mode 2`. |
| `--exclude-drawing-sheet` | Plot SVG without a drawing sheet. |
| `--drill-shape-opt` | The shape of drill marks in the plot. Options are `0` for no drill marks, `1` for small marks, or `2` for actual size marks (default). |
| `--mode-single` | Generates a single file with the output arg path acting as the complete directory and filename path. `COMMON_LAYER_LIST` does not function in this mode. Instead `LAYER_LIST` controls all layers plotted. |
| `--mode-multi` | Plot the layers to one or more SVG files, with each file representing a single layer from `LAYER_LIST`. The output path specifies the directory in which the files will be written. |
| `--scale <scale>` | A scaling factor to use for plotting the PCB. The border and title block are not scaled. A scale factor of 0 autoscales the plot. |
| `--check-zones` | Check zone fills and refill zones, if required, prior to export. Any zone fill updates are not saved in the board file. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |

### 4.23. PCB export: VRML

The `pcb export vrml` command exports a board design to a VRML 3D model file.

Usage: `kicad-cli pcb export vrml [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--user-origin VAR] [--units VAR] [--models-dir VAR] [--models-relative] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the VRML export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.wrl` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. If this option is not given, the board center is used. |
| `--units <units>` | Units to use in the output file. Options are `mm`, `m`, `in` (default), or `tenths` (tenths of an inch). |
| `--models-dir <output model directory>` | Name of output directory to copy component models into. If not used, component models are embedded into the output file. |
| `--models-relative` | With `--models-dir`, use relative paths in the output file. |

### 4.24. PCB export: XAO

The `pcb export xao` command exports a board design to an XAO (SALOME/Gmsh) 3D
model file.

Usage: `kicad-cli pcb export xao [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--force] [--no-unspecified] [--no-dnp] [--variant VAR] [--grid-origin] [--drill-origin] [--subst-models] [--board-only] [--cut-vias-in-body] [--no-board-body] [--no-components] [--component-filter VAR] [--include-tracks] [--include-pads] [--include-zones] [--include-inner-copper] [--include-silkscreen] [--include-soldermask] [--fuse-shapes] [--fill-all-vias] [--no-extra-pad-thickness] [--min-distance MIN_DIST] [--net-filter VAR] [--user-origin VAR] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the XAO export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.xao` file extension. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `-f`, `--force` | Overwrite output file. |
| `--no-unspecified` | Exclude 3D models of components with "unspecified" footprint type. |
| `--no-dnp` | Exclude 3D models of components with "Do not populate" attribute. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--grid-origin` | Use grid origin as origin of output file. |
| `--drill-origin` | Use drill origin as origin of output file. |
| `--subst-models` | Replace VRML models in footprints with STEP or IGS models of the same name, if they exist. |
| `--board-only` | Only include the board itself in the generated model; exclude all component models. |
| `--cut-vias-in-body` | Cut via holes in board body even if conductor layers are not exported. |
| `--no-board-body` | Exclude board body. |
| `--no-components` | Exclude 3D models for components. |
| `--component-filter <reference designator list>` | Only include component 3D models matching this list of reference designators (comma-separated, wildcards supported) |
| `--include-tracks` | Include tracks and vias on outer conductor layers in export (time consuming). |
| `--include-pads` | Include pads in export (time consuming). |
| `--include-zones` | Include zones in export (time consuming). |
| `--include-inner-copper` | Include elements on inner conductor layers in export. |
| `--include-silkscreen` | Include silkscreen graphics in export as a set of flat faces. |
| `--include-soldermask` | Include solder mask layers in export as a set of flat faces. |
| `--fuse-shapes` | Fuse overlapping geometry together in export (time consuming). |
| `--fill-all-vias` | Don’t cut via holes in conductor layers. |
| `--no-extra-pad-thickness` | Disable adding additional metal thickness to pads. When not used, pads have 0.005mm added to their metal thickness, which causes pads to be separate faces in the exported model, distinct from the surrounding metal. |
| `--min-distance <min distance>` | Tolerance for considering two points to be in the same location. Default: `0.01mm`. |
| `--net-filter <net filter>` | Only include copper items belonging to nets matching this wildcard. |
| `--user-origin <output origin>` | Specify a custom origin for the output file, with X and Y coordinates. For example, `1x1in`, `1x1inch`, or `25.4x25.4mm`. The default unit is millimeters. |

### 4.25. PCB import

The `pcb import` command imports a non-KiCad PCB file to KiCad format.
Layers in the input board file are automatically mapped to KiCad layers.

Usage: `kicad-cli pcb import [--help] [--output OUTPUT_FILE] [--format FORMAT] [--report-format FORMAT] [--report-file FILE] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Non-KiCad format board file to import. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the import command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with the `.kicad_pcb` file extension. |
| `--format <format>` | The input board file format. Options are `auto` (default), `pads`, `altium`, `eagle`, `cadstar`, `fabmaster`, `pcad`, and `solidworks`. If the format is `auto`, or if no format is given, KiCad will attempt to automatically determine the input board file format. |
| `--report-format <format>` | Report file format. Options are `none` (default), `json`, or `text`. |
| `--report-file <report filename>` | Output filename for the generated import report. When `--report-file` is not used, the report is printed to stdout. |

### 4.26. PCB render

The `pcb render` command generates a raytraced rendering of the 3D model of the board and saves it to a PNG or JPEG file.

Usage: `kicad-cli pcb render [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--variant VAR] [--width WIDTH] [--height HEIGHT] [--side SIDE] [--background BG] [--quality QUALITY] [--preset PRESET] [--use-board-stackup-colors VAR] [--floor] [--perspective] [--zoom ZOOM] [--pan VECTOR] [--pivot PIVOT] [--rotate ANGLES] [--light-top COLOR] [--light-bottom COLOR] [--light-side COLOR] [--light-camera COLOR] [--light-side-elevation ANGLE] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to render. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the render command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. This argument must be given. The file extension given in this argument determines the output image file format. The filename must end with either `.png` (for PNG files) or `.jpg`/`.jpeg` (for JPG files). |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `-w <width>`, `--width <width>` | Image width in pixels. Default: `1600`. |
| `-h <height>`, `--height <height>` | Image height in pixels. Default: `900`. |
| `--side <side>` | The side of the board to render. Options are `top` (default), `bottom`, `left`, `right`, `front`, or `back`. |
| `--background <background>` | Image background. Options are `default` (default), `transparent`, or `opaque`. For PNG files, `default` is `transparent`. For JPG files, `default` is `opaque`. |
| `--quality <quality>` | Render quality. Options are `basic` (default), `high`, `user`. When `user` is specified, the render settings stored in the project are used. |
| `--preset <color preset>` | Color preset. Options are `follow_pcb_editor`, `follow_plot_settings` (default), or `legacy_preset_flag`. |
| `--use-board-stackup-colors` | Colors defined in the board stackup override colors from the preset. |
| `--floor` | Enables floor, shadows and post-processing, even if disabled in quality preset. |
| `--perspective` | Use perspective projection instead of orthogonal. |
| `--zoom <zoom level>` | Camera zoom factor as an integer. Default: `1`. |
| `--pan <camera pan>` | Set camera pan location, in millimeters, with the format `'X,Y,Z'`, e.g. `'3,0,0'`. |
| `--pivot <pivot>` | Set pivot point relative to the board center in centimeters, with the format `'X,Y,Z'` e.g. `'-10,2,0'`. |
| `--rotate <rotation>` | Set board rotation around pivot point, in degrees, with the format `'X,Y,Z'`, e.g. `'-45,0,45'` for isometric view. |
| `--light-top <intensity>` | Top light intensity, format `'R,G,B'` or a single number, range: 0-1. |
| `--light-bottom <intensity>` | Bottom light intensity, format `'R,G,B'` or a single number, range: 0-1. |
| `--light-side <intensity>` | Side lights intensity, format `'R,G,B'` or a single number, range: 0-1. |
| `--light-camera <intensity>` | Camera light intensity, format `'R,G,B'` or a single number, range: 0-1. |
| `--light-side-elevation <elevation>` | Side lights elevation angle in degrees, range: 0-90. |

### 4.27. PCB upgrade

The `pcb upgrade` command converts a KiCad board file from a previous KiCad board file format to the native format for the current version of KiCad.
If the input board file is already in the current file format, no action is taken.

Usage: `kicad-cli pcb upgrade [--help] [--force] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Board file to upgrade. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the upgrade command. |
| `--force` | Re-save the input board file even if it is already in the current file format. |

## 5. Schematic commands

The `sch` command runs an electrical rule check, exports a schematic to
various other file formats, or exports a bill of materials or netlist. Each
subcommand has its own options.

### 5.1. Schematic ERC

The `sch erc` command runs an electrical rule check on a schematic and generates
a report.

Usage: `kicad-cli sch erc [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE]…​ [--format VAR] [--units VAR] [--severity-all] [--severity-error] [--severity-warning] [--severity-exclusions] [--exit-code-violations] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to run ERC on. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the ERC command. |
| `-o <output filename>`, `--output <output filename>` | Output filename for the generated ERC report. When `--output` is not used, the output filename will be the same as the input file, with the `.rpt` or `.json` file extension, depending on the selected format. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--format <format>` | Report file format. Options are `report` (default) or `json`. |
| `--units <unit>` | Units to use in the report. Options are `mm` (default), `in`, or `mils`. |
| `--severity-all` | Report all ERC violations. This is equivalent to using all of the other ERC severity options. |
| `--severity-error` | Report all error-level ERC violations. This can be combined with the other ERC severity options. |
| `--severity-warning` | Report all warning-level ERC violations. This can be combined with the other ERC severity options. |
| `--severity-exclusions` | Report all excluded ERC violations. This can be combined with the other ERC severity options. |
| `--exit-code-violations` | Return an exit code depending on whether or not ERC violations exist. The exit code is 0 if no violations are found, and 5 if any violations are found. |

### 5.2. Schematic export: bill of materials

The `sch export bom` command exports a BOM from a schematic.
The BOM export has a number of options for controlling the format and included
fields. This export method is equivalent to
[exporting a BOM](../eeschema/eeschema.html#bom-export)
from the symbol fields table.

|  |  |
| --- | --- |
| Note | To export a BOM using the legacy XML and Python BOM script workflow, use the `sch export python-bom` command. |

Usage: `kicad-cli sch export bom [--help] [--output OUTPUT_FILE] [--variant VAR] [--preset PRESET] [--format-preset FMT_PRESET] [--fields FIELDS] [--labels LABELS] [--group-by GROUP_BY] [--sort-field SORT_BY] [--sort-asc VAR] [--filter FILTER] [--exclude-dnp] [--include-excluded-from-bom] [--field-delimiter FIELD_DELIM] [--string-delimiter STR_DELIM] [--ref-delimiter REF_DELIM] [--ref-range-delimiter REF_RANGE_DELIM] [--keep-tabs] [--keep-line-breaks] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Shows help message and exits |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with a `.csv` file extension. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--preset <preset>` | Use a named BOM preset setting from the schematic, e.g. `"Grouped By Value"`. |
| `--format-preset <format preset>` | Use a named BOM format preset setting from the schematic, e.g. `CSV`. |
| `--fields <fields>` | An ordered list of fields to export. `*` includes all fields. Virtual BOM symbol fields such as DNP or Exclude from board can be accessed with `${DNP}` or `${EXCLUDE_FROM_BOARD}`, respectively (see the [BOM export documentation](../eeschema/eeschema.html#bom-export) for a list of fields). These fields can be specified in this argument with or without the `${}` syntax. Default: "Reference,Value,Footprint,QUANTITY,DNP". |
| `--labels <labels>` | An ordered list of labels to apply the exported fields (default: `"Refs,Value,Footprint,Qty,DNP"`). |
| `--group-by <fields>` | Fields to group references by when field values match. |
| `--sort-field <fields>` | Field name to sort by (default: `"Reference"`). |
| `--sort-asc` | If given, sort in ascending order. If not given, sort in descending order. |
| `--filter <filter>` | If given, only components with reference designators that match the given filter string are included in the output. The filter supports wildcards: `*` matches any number of any characters, including none, and `?` matches any single character. |
| `--exclude-dnp` | Exclude symbols with the "Do not populate" attribute. |
| `--include-excluded-from-bom` | Include symbols marked "Exclude from BOM". This argument is deprecated as of KiCad 10.0 and has no effect. |
| `--field-delimiter <delimiter>` | Separator between output fields/columns (default: `","`). |
| `--string-delimiter <delimiter>` | Character to surround fields with (none by default). |
| `--ref-delimiter <delimiter>` | Character to place between individual references (default: `","`). |
| `--ref-range-delimiter <delimiter>` | Character to place in ranges of references (default: `"-"`). Leave blank for no ranges. |
| `--keep-tabs` | Keep tab characters from input fields. Stripped by default. |
| `--keep-line-breaks` | Keep line break characters from input fields. Stripped by default. |

### 5.3. Schematic export: DXF

The `sch export dxf` command exports a schematic to a DXF file. Each
sheet in the design is exported to its own file.

Usage: `kicad-cli sch export dxf [--help] [--output OUTPUT_DIR] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--variant VAR] [--theme THEME_NAME] [--black-and-white] [--exclude-drawing-sheet] [--default-font VAR] [--draw-hop-over] [--pages PAGE_LIST] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the DXF file export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder for the exported files. One file is output for each sheet. When `--output` is not used, the files are exported to the current directory. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the schematic file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the schematic editor’s currently selected theme is used. |
| `-b`, `--black-and-white` | Export schematic in black and white. |
| `-e`, `--exclude-drawing-sheet` | Plot DXF without a drawing sheet. |
| `--default-font <font name>` | Default font name. Default: `"KiCad Font"`. |
| `--draw-hop-over` | Draw hop-overs at wire crossings. |
| `--pages <page list>` | Comma-separated list of pages to export. Blank or unspecified means all pages. To plot specific pages, give the root sheet as `INPUT_FILE` and specify the desired output pages with the `--pages` argument. |

### 5.4. Schematic export: HPGL

|  |  |
| --- | --- |
| Note | `kicad-cli sch export hpgl` is not functional in KiCad 10.0. |

The `sch export hpgl` command is not functional in KiCad 10.0 as KiCad no longer supports HPGL output.
In previous versions of KiCad it exported a schematic to an HPGL file.
It is included as a non-functional command for compatibility reasons.
It will be removed in a future version of KiCad.

Usage: `kicad-cli sch export hpgl [--help] [--output OUTPUT_DIR] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--variant VAR] [--theme THEME_NAME] [--black-and-white] [--exclude-drawing-sheet] [--default-font VAR] [--draw-hop-over] [--pages PAGE_LIST] [--pen-size PEN_SIZE] [--origin ORIGIN] INPUT_FILE`

### 5.5. Schematic export: netlist

The `sch export netlist` command exports a netlist in
[various formats](../eeschema/eeschema.html#netlist-formats) from
a schematic.

Usage: `kicad-cli sch export netlist [--help] [--output OUTPUT_FILE] [--variant VAR] [--format FORMAT] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the netlist export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with a `.net` file extension. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `--format <format>` | The netlist output format. Options are `kicadsexpr` (default), `kicadxml`, `cadstar`, `orcadpcb2`, `spice`, `spicemodel`, `pads`, or `allegro`. |

### 5.6. Schematic export: PDF

The `sch export pdf` command exports a schematic to a PDF file. Each sheet in
the design is exported to its own page in the PDF file.

Usage: `kicad-cli sch export pdf [--help] [--output OUTPUT_FILE] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--variant VAR] [--theme THEME_NAME] [--black-and-white] [--exclude-drawing-sheet] [--default-font VAR] [--draw-hop-over] [--exclude-pdf-property-popups] [--exclude-pdf-hierarchical-links] [--exclude-pdf-metadata] [--no-background-color] [--pages PAGE_LIST] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the PDF file export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with a `.pdf` file extension. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the schematic file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the schematic editor’s currently selected theme is used. |
| `-b`, `--black-and-white` | Export schematic in black and white. |
| `-e`, `--exclude-drawing-sheet` | Plot PDF without a drawing sheet. |
| `--default-font <font name>` | Default font name. Default: `"KiCad Font"`. |
| `--draw-hop-over` | Draw hop-overs at wire crossings. |
| `--exclude-pdf-property-popups` | Do not generate property popups in PDF. |
| `--exclude-pdf-hierarchical-links` | Do not generate clickable links for hierarchical elements in PDF. |
| `--exclude-pdf-metadata` | Do not generate PDF metadata from AUTHOR and SUBJECT variables. |
| `-n`, `--no-background-color` | Export schematic without a background color, regardless of theme. |
| `--pages <page list>` | Comma-separated list of pages to export. Blank or unspecified means all pages. To plot specific pages, give the root sheet as `INPUT_FILE` and specify the desired output pages with the `--pages` argument. |

### 5.7. Schematic export: PostScript

The `sch export ps` command exports a schematic to a PostScript file. Each
sheet in the design is exported to its own file.

Usage: `kicad-cli sch export ps [--help] [--output OUTPUT_DIR] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--variant VAR] [--theme THEME_NAME] [--black-and-white] [--exclude-drawing-sheet] [--default-font VAR] [--draw-hop-over] [--no-background-color] [--pages PAGE_LIST] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the PS file export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder for the exported files. One file is output for each sheet. When `--output` is not used, the files are exported to the current directory. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the schematic file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the schematic editor’s currently selected theme is used. |
| `-b`, `--black-and-white` | Export schematic in black and white. |
| `-e`, `--exclude-drawing-sheet` | Plot PS without a drawing sheet. |
| `--default-font <font name>` | Default font name. Default: `"KiCad Font"`. |
| `--draw-hop-over` | Draw hop-overs at wire crossings. |
| `-n`, `--no-background-color` | Export schematic without a background color, regardless of theme. |
| `--pages <page list>` | Comma-separated list of pages to export. Blank or unspecified means all pages. To plot specific pages, give the root sheet as `INPUT_FILE` and specify the desired output pages with the `--pages` argument. |

### 5.8. Schematic export: bill of materials (legacy BOM scripts)

The `sch export python-bom` command exports an XML BOM file from a schematic.
The XML BOM file can then be processed into your desired BOM format using a
custom script or one of the scripts described in the
[schematic BOM
export documentation](../eeschema/eeschema.html#bom-export).

Usage: `kicad-cli sch export python-bom [--help] [--output OUTPUT_FILE] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the BOM export command. |
| `-o <output filename>`, `--output <output filename>` | The output filename. When `--output` is not used, the output filename will be the same as the input file, with a `-bom.xml` suffix and file extension. |

### 5.9. Schematic export: SVG

The `sch export svg` command export a schematic to an SVG file. Each sheet in
the design is exported to its own file.

Usage: `kicad-cli sch export svg [--help] [--output OUTPUT_DIR] [--drawing-sheet SHEET_PATH] [--define-var KEY=VALUE]…​ [--variant VAR] [--theme THEME_NAME] [--black-and-white] [--exclude-drawing-sheet] [--default-font VAR] [--draw-hop-over] [--no-background-color] [--pages PAGE_LIST] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the SVG file export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder for the exported files. When `--output` is not used, the files are exported to the current directory. |
| `--drawing-sheet <sheet path>` | Path to drawing sheet to use in plot, overriding the drawing sheet specified in the schematic file. |
| `-D <variable name>=<value>`, `--define-var <variable_name>=<value>` | Add or override project variable definitions. Can be used multiple times to define multiple variables. |
| `--variant <variant name>` | The name of the variant to output. You can use `${VARIANT}` in the output path to generate an output filename specific to the variant. When `--variant` is not used, the default variant is output. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the schematic editor’s currently selected theme is used. |
| `-b`, `--black-and-white` | Export schematic in black and white. |
| `-e`, `--exclude-drawing-sheet` | Plot SVG without a drawing sheet. |
| `--default-font <font name>` | Default font name. Default: `"KiCad Font"`. |
| `--draw-hop-over` | Draw hop-overs at wire crossings. |
| `-n`, `--no-background-color` | Export schematic without a background color, regardless of theme. |
| `--pages <page list>` | Comma-separated list of pages to export. Blank or unspecified means all pages. To plot specific pages, give the root sheet as `INPUT_FILE` and specify the desired output pages with the `--pages` argument. |

### 5.10. Schematic upgrade

The `sch upgrade` command converts a KiCad schematic file from a previous KiCad schematic file format to the native format for the current version of KiCad.
If the input schematic file is already in the current file format, no action is taken.

|  |  |
| --- | --- |
| Note | Only the specified schematic file is upgraded. If the schematic file contains any child sheets, the child sheets are not upgraded. |

Usage: `kicad-cli sch upgrade [--help] [--force] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Schematic file to upgrade. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the upgrade command. |
| `--force` | Re-save the input schematic file even if it is already in the current file format. |

## 6. Symbol commands

The `sym` subcommand exports symbols to another format or upgrades symbol
libraries to the current version of the KiCad symbol file format.

### 6.1. Symbol export

The `sym export svg` command exports one or more symbols from the specified
library into SVG files.

Usage: `kicad-cli sym export svg [--help] [--output OUTPUT_DIR] [--theme THEME_NAME] [--symbol SYMBOL] [--black-and-white] [--include-hidden-pins] [--include-hidden-fields] INPUT_FILE`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE` | Symbol library file to use for export. |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the symbol SVG export command. |
| `-o <output dir>`, `--output <output dir>` | The output folder for the exported files. Each symbol in the input library is output to a separate file. When `--output` is not used, the files are exported to the current directory. |
| `-t <theme name>`, `--theme <theme name>` | The name of the theme to use for export. If no theme is given, the symbol editor’s currently selected theme is used. |
| `-s <symbol name>`, `--symbol <symbol name>` | The specific symbol to export from the library. When this argument is not used, all symbols in the library are exported. |
| `--black-and-white` | Export symbols in black and white. |
| `--include-hidden-pins` | Export hidden pins in the exported SVG. |
| `--include-hidden-fields` | Export hidden symbol fields in the exported SVG. |

### 6.2. Symbol upgrade

The `sym upgrade` command converts the specified symbol library from a
legacy KiCad symbol format or a non-KiCad symbol format to the native format for
the current version of KiCad. If the input library is already in the current
file format, no action is taken.

Supported input symbol formats are:

- KiCad symbol library (`.kicad_sym`)
- KiCad (pre-6.0) symbol library (`.lib`)
- Altium schematic library (`.SchLib`)
- Altium integrated library (`.IntLib`)
- CADSTAR parts library (`.lib`)
- EAGLE XML library (`.lbr`)
- EasyEDA (JLCEDA) Std file (`.json`)
- EasyEDA (JLCEDA) Pro file (`.elibz`, `.epro`, `.zip`)

Usage: `kicad-cli sym upgrade [--help] [--output OUTPUT_FILE_OR_DIR] [--force] INPUT_FILE_OR_DIR`

Positional arguments:

|  |  |
| --- | --- |
| `INPUT_FILE_OR_DIR` | Symbol or symbol library to upgrade. This can be an unpacked symbol (`.kicad_sym` file containing a single symbol), an unpacked symbol library (folder containing `.kicad_sym` files), or a packed symbol library (`.kicad_sym` file containing multiple symbols). |

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the upgrade command. |
| `-o <output file or directory>`, `--output <output file or directory>` | The output file or directory for the upgraded symbol library. When the output path is a file, the symbols are saved as a single-file ("packed") `.kicad_sym` library. When the output path is a folder, the symbols are saved as individual ("unpacked") `.kicad_sym` files in the folder, with one file per symbol. When `--output` is not used, the upgraded symbol library is saved over the original library. |
| `--force` | Re-save the input library even if it is already in the current file format. |

## 7. Version commands

The `version` command prints the KiCad version. Without any arguments, it
simply prints the version number, for example `7.0.7`. You can print the version
in several other formats using the `--format` argument.

|  |  |
| --- | --- |
| Note | Use `kicad-cli version --format about` for version information to include when submitting bug reports or feature requests on Gitlab. |

Usage: `kicad-cli version [--help] [--format VAR]`

Optional arguments:

|  |  |
| --- | --- |
| `-h`, `--help` | Show help for the version command. |
| `--format <format>` | Format of the version number. Options are `plain` (default), `commit`, or `about`. `plain` prints the version number (e.g. `7.0.7`), which is the default if the `--format` argument is not used. `commit` prints the hash of the git commit for the build of KiCad you are using. `about` prints the full version information, including library versions and basic system information. You can use the `about` version information in bug reports. |

Last updated 2026-08-28 18:17:50 UTC