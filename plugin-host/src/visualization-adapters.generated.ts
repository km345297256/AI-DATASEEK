// Generated from contracts/visualization-adapters.json; do not edit. Run node scripts/sync-visualization-contract.mjs.
export const VISUALIZATION_CONTRACT_VERSION = 2 as const;
export const VISUALIZATION_ADAPTERS = {
  "database-dump": {
    "readers": [
      "sql-dump",
      "pg-dump"
    ],
    "view_kind": "table",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "database-records": {
    "readers": [
      "bson",
      "redis-rdb"
    ],
    "view_kind": "tree",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "image": {
    "readers": [
      "binary"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": true
    }
  },
  "tiff": {
    "readers": [
      "binary"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "bytes",
        "prepare"
      ],
      "input_mode": "whole",
      "shared": true
    }
  },
  "shapefile": {
    "readers": [
      "shapefile"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": true
    }
  },
  "molecular": {
    "readers": [
      "molecular"
    ],
    "view_kind": "structure",
    "capabilities": {
      "operations": [
        "prepare",
        "bytes"
      ],
      "input_mode": "whole",
      "shared": true
    }
  },
  "obj": {
    "readers": [
      "binary"
    ],
    "view_kind": "structure",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": true
    }
  },
  "html": {
    "readers": [
      "binary"
    ],
    "view_kind": "document",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": true
    }
  },
  "markdown": {
    "readers": [
      "binary"
    ],
    "view_kind": "document",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": true
    }
  },
  "text": {
    "readers": [
      "text"
    ],
    "view_kind": "text",
    "capabilities": {
      "operations": [
        "page"
      ],
      "input_mode": "page",
      "shared": true
    }
  },
  "csv": {
    "readers": [
      "csv"
    ],
    "view_kind": "table",
    "capabilities": {
      "operations": [
        "page"
      ],
      "input_mode": "page",
      "shared": true
    }
  },
  "scientific-map": {
    "readers": [
      "netcdf"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "scientific-series": {
    "readers": [
      "netcdf",
      "fits"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "scientific-image": {
    "readers": [
      "fits"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "scientific-quality": {
    "readers": [
      "fastq"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "prefix",
      "shared": false
    }
  },
  "plotly": {
    "readers": [
      "tabular"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview",
        "prepare"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "h5web": {
    "readers": [
      "hdf5"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview",
        "prepare"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "vtk": {
    "readers": [
      "binary"
    ],
    "view_kind": "structure",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "jsroot": {
    "readers": [
      "root"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "rdkit": {
    "readers": [
      "rdkit"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "molstar": {
    "readers": [
      "binary"
    ],
    "view_kind": "structure",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "nmrium": {
    "readers": [
      "jcamp"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "openlayers": {
    "readers": [
      "binary"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "bytes",
        "prepare"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "maplibre": {
    "readers": [
      "binary"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "cesium": {
    "readers": [
      "binary"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "aladin": {
    "readers": [
      "binary"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "metpy": {
    "readers": [
      "metpy"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "igv": {
    "readers": [
      "binary"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "viv": {
    "readers": [
      "binary"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "bytes",
        "prepare"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "structured-tree": {
    "readers": [
      "structure"
    ],
    "view_kind": "tree",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "archive-directory": {
    "readers": [
      "archive"
    ],
    "view_kind": "table",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "archive-members": {
    "readers": [
      "archive-member"
    ],
    "view_kind": "text",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "signal-window": {
    "readers": [
      "edf"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "array-window": {
    "readers": [
      "array-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "columnar-window": {
    "readers": [
      "columnar-window"
    ],
    "view_kind": "table",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "nexus-window": {
    "readers": [
      "nexus-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "scientific-graph": {
    "readers": [
      "scientific-graph"
    ],
    "view_kind": "graph",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "phylogeny": {
    "readers": [
      "phylogeny"
    ],
    "view_kind": "tree",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "mass-spectrum": {
    "readers": [
      "mass-spectrum"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "diffraction": {
    "readers": [
      "diffraction"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "fcs-window": {
    "readers": [
      "fcs-window"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "ripple-window": {
    "readers": [
      "ripple-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "envi-window": {
    "readers": [
      "envi-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "grib-window": {
    "readers": [
      "grib-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "seismic-window": {
    "readers": [
      "seismic-window"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "czi-window": {
    "readers": [
      "czi-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "instrument-image": {
    "readers": [
      "instrument-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "ome-zarr": {
    "readers": [
      "ome-zarr"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "mca-spectrum": {
    "readers": [
      "mca"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "geoscience-formats": {
    "readers": [
      "geoformat"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "czi-image": {
    "readers": [
      "czi"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "video-player": {
    "readers": [
      "binary"
    ],
    "view_kind": "media",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "audio-waveform": {
    "readers": [
      "binary"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "niivue": {
    "readers": [
      "binary"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "fastqc": {
    "readers": [
      "fastqc"
    ],
    "view_kind": "table",
    "capabilities": {
      "operations": [
        "job"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "pdfjs": {
    "readers": [
      "binary"
    ],
    "view_kind": "document",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "word": {
    "readers": [
      "office"
    ],
    "view_kind": "document",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "excel": {
    "readers": [
      "excel"
    ],
    "view_kind": "table",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "powerpoint": {
    "readers": [
      "office"
    ],
    "view_kind": "document",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "docx": {
    "readers": [
      "binary"
    ],
    "view_kind": "document",
    "capabilities": {
      "operations": [
        "bytes"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "onlyoffice": {
    "readers": [
      "office-viewer"
    ],
    "view_kind": "document",
    "capabilities": {
      "operations": [
        "prepare"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "dicom-window": {
    "readers": [
      "dicom-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "spatial-window": {
    "readers": [
      "spatial-window"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "pointcloud-window": {
    "readers": [
      "pointcloud-window"
    ],
    "view_kind": "structure",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "gro-trajectory": {
    "readers": [
      "gro-trajectory"
    ],
    "view_kind": "structure",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "database-table": {
    "readers": [
      "database-table"
    ],
    "view_kind": "table",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "sqlite-table": {
    "readers": [
      "sqlite-table"
    ],
    "view_kind": "table",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "radar-window": {
    "readers": [
      "radar-window"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "ugrid-window": {
    "readers": [
      "ugrid-window"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "window",
      "shared": false
    }
  },
  "matrix-workbench": {
    "readers": [
      "matrix-workbench"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "astronomy-workbench": {
    "readers": [
      "astronomy-workbench"
    ],
    "view_kind": "image",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "alignment-browser": {
    "readers": [
      "alignment-browser"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "sequence-browser": {
    "readers": [
      "sequence-browser"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "genome-tracks": {
    "readers": [
      "genome-tracks"
    ],
    "view_kind": "map",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "blast-hits": {
    "readers": [
      "blast-hits"
    ],
    "view_kind": "series",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  },
  "simulation-mesh": {
    "readers": [
      "simulation-mesh"
    ],
    "view_kind": "structure",
    "capabilities": {
      "operations": [
        "preview"
      ],
      "input_mode": "whole",
      "shared": false
    }
  }
} as const;
export type VisualizationAdapter = keyof typeof VISUALIZATION_ADAPTERS;
export type VisualizationReader = "alignment-browser" | "archive" | "archive-member" | "array-window" | "astronomy-workbench" | "binary" | "blast-hits" | "bson" | "columnar-window" | "csv" | "czi" | "czi-window" | "database-table" | "dicom-window" | "diffraction" | "edf" | "envi-window" | "excel" | "fastq" | "fastqc" | "fcs-window" | "fits" | "genome-tracks" | "geoformat" | "grib-window" | "gro-trajectory" | "hdf5" | "instrument-window" | "jcamp" | "mass-spectrum" | "matrix-workbench" | "mca" | "metpy" | "molecular" | "netcdf" | "nexus-window" | "office" | "office-viewer" | "ome-zarr" | "pg-dump" | "phylogeny" | "pointcloud-window" | "radar-window" | "rdkit" | "redis-rdb" | "ripple-window" | "root" | "scientific-graph" | "seismic-window" | "sequence-browser" | "shapefile" | "simulation-mesh" | "spatial-window" | "sql-dump" | "sqlite-table" | "structure" | "tabular" | "text" | "ugrid-window";
export type VisualizationOperation = "bytes" | "page" | "preview" | "prepare" | "job";
export type VisualizationInputMode = "whole" | "page" | "prefix" | "window";
export type VisualizationKind = "image" | "map" | "series" | "table" | "text" | "structure" | "document" | "tree" | "media" | "graph";
