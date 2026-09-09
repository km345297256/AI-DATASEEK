# Generated from contracts/visualization-adapters.json; do not edit. Run node scripts/sync-visualization-contract.mjs.
from typing import Literal
import json

VISUALIZATION_CONTRACT_VERSION = 2
VisualizationAdapter = Literal["image", "tiff", "shapefile", "molecular", "obj", "html", "markdown", "text", "csv", "scientific-map", "scientific-series", "scientific-image", "scientific-quality", "plotly", "h5web", "vtk", "jsroot", "rdkit", "molstar", "nmrium", "openlayers", "maplibre", "cesium", "aladin", "metpy", "igv", "viv", "niivue", "fastqc", "pdfjs", "word", "excel", "powerpoint", "docx", "onlyoffice"]
VisualizationReader = Literal["binary", "csv", "excel", "fastq", "fastqc", "fits", "hdf5", "jcamp", "metpy", "molecular", "netcdf", "office", "office-viewer", "rdkit", "root", "shapefile", "tabular", "text"]
VisualizationOperation = Literal["bytes", "page", "preview", "prepare", "job"]
VisualizationInputMode = Literal["whole", "page", "prefix"]
VisualizationKind = Literal["image", "map", "series", "table", "text", "structure", "document"]
ADAPTER_CONTRACTS = json.loads(r'''{
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
        "bytes"
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
        "preview"
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
        "preview"
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
        "bytes"
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
  }
}''')
